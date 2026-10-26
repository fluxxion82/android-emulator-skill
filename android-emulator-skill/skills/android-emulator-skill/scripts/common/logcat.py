#!/usr/bin/env python3
"""The one place that knows how to ask ``adb`` for logs.

Four scripts in this skill read logcat, and before this module each of them
built its own ``adb logcat`` argv and parsed its own duration strings. That is
how ``app_state_capture`` ended up accepting ``30s`` and ``5m`` but rejecting
``1h``, while ``log_monitor`` and ``anr_watcher`` accepted all three -- the same
flag meaning different things depending on which script the agent reached for.
It is also how ``log_monitor`` once shipped ``-v time`` against a parser that
only understands ``-v threadtime``, matching zero lines while every test passed.

So the argv shape and the duration grammar live here, once, and the four callers
select options rather than re-deriving them:

    log_monitor        -> stream / historical window on the main buffers
    anr_watcher        -> stream / historical window, ANR+jank parse on top
    crash_triage       -> ``-b crash`` dump, a *different buffer*
    app_state_capture  -> historical window as one artifact of a snapshot

**Argument order is part of the contract.** Every caller emits

    adb [-s SERIAL] logcat [-b BUF] [-d] [-t TS] [-v FMT] [-v usec] [-T N] [--pid=N]
        [TAG:P ...] [*:PRIORITY]

which is the union of what the four produced before, in the order they produced
it, so the migration changed no command any of them sends. (``-v usec``, ``-T``
and ``TAG:P`` came later, for the app-window readers: see :func:`app_window`.)
A binary read (``-B``, :func:`logcat_args` ``binary=True``) takes the place of
the ``-v`` terms and drops the filterspecs, which ``-B`` ignores (measured).

**One app's window is read from logd's records, not from text.** Text has no
framing an app cannot forge: logcat prints a tag verbatim, so a tag of
``"x:\\n<a system_server header> I ActivityManager"`` makes one record print as
two lines, the second one system_server's to any text parser (recorded:
logcat_tag_injection). ``logcat -B`` hands over each record whole, with the
pid and uid logd took from the writing socket's credentials -- the only
metadata an app cannot write (the tid and the time come from the writer's own
header; AOSP logd LogListener). :func:`read_record` / :func:`parse_records`
read it; :meth:`LogRecord.lines` prints a record as ``-v threadtime -v usec``
would, with the control characters of its tag and message escaped.
``log_monitor`` and ``app_state_capture`` read this way; **the ANR readers are
meant to adopt the same reader** for their streams rather than grow their own.

For a reader adopting it, two rules:

- ``main``, ``system``, ``crash`` (and ``radio``, ``kernel``) hold text
  records: a priority byte, a tag and a message. The ``events``, ``stats`` and
  ``security`` buffers hold BINARY payloads (an event tag number and typed
  values), which :class:`LogRecord`'s ``priority`` / ``tag`` / ``message``
  do not describe; read those with another decoder, or not at all.
- A :class:`LogFramingError` with ``truncated`` set is the stream ending
  inside a record. It is tolerable only when the caller stopped the stream
  itself (a deadline, an interrupt) -- and then the lost record is disclosed,
  not silently dropped. On a dump, or a stream that ended by itself, it means
  the capture is incomplete: re-check the device (gone: exit 3) and refuse
  otherwise. Any other framing error is not records at all: refuse.

On Android 13 (API 33) a live ``-B`` stream holds records back and loses them
when it is stopped, so a live read there is made of dumps that each continue
from a cursor: :func:`read_after` (the live read on that level of log_monitor
and of anr_watcher's --watch and session worker, made by common.dump_read,
which runs the dumps). Like every logd reader, stream or
dump, it reads what logd DELIVERED: logd may drop records it prunes while a
reader is behind, and nothing a reader does can certify a window complete.

This module is pure: it builds argv and parses bytes and strings, and never
touches a device (:func:`read_after` is handed the dump to run). Execution
stays with ``common.adb_exec`` (bounded, typed errors) or, for the two
streaming readers, their own watchdog-terminated ``Popen``.
"""

from __future__ import annotations

import re
import struct
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import BinaryIO

from common import log_lifecycle
from common.device_utils import build_adb_command

# `logcat -t` accepts a "MM-DD HH:MM:SS.mmm" timestamp and prints lines at or
# after it. Milliseconds are always zeroed: the window is a coarse "since
# roughly then", and a spurious sub-second precision would imply otherwise.
TIMESTAMP_FORMAT = "%m-%d %H:%M:%S.000"

# `threadtime` is the default because it is the only format the parsers in this
# skill can read: "MM-DD HH:MM:SS.mmm PID TID P Tag: msg". The `time` format
# omits the PID/TID columns and writes "P/Tag( PID):" instead. Passing it
# explicitly also pins the format rather than inheriting a device's log
# settings, which is why crash_triage passes it even though the emulator's
# default happened to match.
DEFAULT_FORMAT = "threadtime"

# Deliberately un-anchored at the end, matching the grammar log_monitor and
# anr_watcher have always accepted (so "30sec" still means 30 seconds). The
# `[smh]` set is the union: app_state_capture accepted only `[sm]`, which made
# `--logs 1h` a silent "invalid duration" there and nowhere else.
_DURATION = re.compile(r"(\d+)([smh])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# Ascending logcat priority. `*:<priority>` shows that level *and everything
# above it*, so a severity set resolves to its lowest member.
PRIORITY_ORDER = ("V", "D", "I", "W", "E", "F")

# Severity names used by this skill's CLIs -> the logcat priority letters they
# cover. "error" covers F as well: a fatal is an error the agent wants to see.
SEVERITY_PRIORITIES = {
    "verbose": ("V",),
    "debug": ("D",),
    "info": ("I",),
    "warning": ("W",),
    "error": ("E", "F"),
}

# The accepted names, in the order `--help` lists them. Closed: a name outside
# it is refused rather than ignored. Ignoring one is how `--severity warn`
# selected nothing and `error,warn` selected errors only, each reported as a
# calm, successful capture (D1).
SEVERITY_NAMES = ("error", "warning", "info", "debug", "verbose")

# `logcat -v threadtime`: "MM-DD HH:MM:SS.mmm  PID  TID P Tag: message", one
# header per line (a multi-line message is several headed lines). With
# `-v usec` the fraction has six digits. What :meth:`LogRecord.lines` prints,
# so the text a snapshot saves is read back by the same pattern (D3).
# Recorded: logcat_threadtime (ms), logcat_app_restart_usec.
_THREADTIME = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}(?:\d{3})?\s+(?P<pid>\d+)\s+\d+\s+"
    r"(?P<priority>[VDIWEF])\s+(?P<tag>[^:]*?)\s*:(?P<message>.*)$"
)

# Microsecond headers, for a TEXT reader: the lines of ONE multi-line message
# share their header to the microsecond, while two messages written in one
# millisecond need not differ at millisecond precision -- recorded:
# system_server's `Force stopping` and `Killing` lines once shared
# `16:14:16.651`. The app-window readers no longer need it: they read records
# (`-B`), which say where a message ends without any header comparison.
USEC_FORMAT = "usec"

# The tag whose records name an app's processes (measured, API 35: `I
# ActivityManager: Start proc <pid>:<pkg>/<user>`, `Killing ...`, `Process
# <pkg> (pid N) has died`; recorded in logcat_app_restart_records). They are
# I, so a `*:W` filter would drop them; a binary read has no filter (``-B``
# ignores filterspecs, measured), and the readers apply their floor on the
# host after attribution. Defined once, in common.log_lifecycle, which reads them.
PROCESS_TAG = log_lifecycle.PROCESS_TAG

# `logcat -B`: logd's records, unformatted. Each is `struct logger_entry` --
# u16 payload length, u16 header size, i32 pid, u32 tid, u32 sec, u32 nsec,
# u32 log id, u32 uid -- then the payload, `<priority byte><tag>\0<message>\0`.
# Measured on the API 35 emulator (logcat_tag_injection, logcat_crash_tail):
# header size 28, every record. AOSP logcat refuses a header smaller than
# that struct, and so does this reader: without the uid there is nothing to
# check a record's writer against. The upper bounds keep one corrupt length
# from asking the pipe for 64 KiB that are not a record: logd's records are at
# most LOGGER_ENTRY_MAX_LEN (5 KiB).
BINARY_FLAG = "-B"
_ENTRY_SIZES = struct.Struct("<HH")
_ENTRY_FIELDS = struct.Struct("<iIIIII")
MIN_ENTRY_HEADER = 28
MAX_ENTRY_HEADER = 64
MAX_ENTRY_PAYLOAD = 5 * 1024
# android_LogPriority, as logcat prints it (VERBOSE=2 .. FATAL=7, SILENT=8);
# anything else prints "?".
_PRIORITY_LETTERS = {2: "V", 3: "D", 4: "I", 5: "W", 6: "E", 7: "F", 8: "S"}


class LogFramingError(RuntimeError):
    """The bytes are not logd's record framing, so no record after them can be trusted.

    A length-framed stream cannot resynchronise: one bad header and every
    later boundary is a guess, so the read is refused rather than guessed at.

    Attributes:
        offset: Where in the stream the bad record began.
        truncated: The stream ended inside a record. A live read that was
            stopped mid-record (its deadline, an interrupt) ends this way and
            loses only that record; anything else is a refusal.
    """

    remedy = (
        "Retry. If it repeats, this device's `logcat -B` does not write logd's records; "
        "report the device and its API level."
    )

    def __init__(self, message: str, *, offset: int, truncated: bool = False) -> None:
        super().__init__(message)
        self.offset = offset
        self.truncated = truncated


def _printable(text: str) -> str:
    """``text`` with every non-printable character escaped (``\\n`` -> ``\\\\n``).

    For the TAG. A tag is the app's to choose and logcat prints it verbatim,
    newline and all -- the injection logcat_tag_injection records. Escaped, a
    tag can no longer end its line.
    """
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def _line_safe(text: str) -> str:
    """One line of a message with every character that could end or rewrite a line escaped.

    Control characters (Unicode category Cc) other than TAB -- among them
    CR, VT, FF, the file/group/record separators, NEL, ESC and DEL -- and the
    line and paragraph separators U+2028/U+2029. ``str.splitlines``, a
    text-mode file and most line readers break a line at one of those, so a
    message holding ``\\r<an E header>`` re-read from a saved log was two
    lines, the second a forged error (review of PR #35); ESC rewrites a
    terminal. TAB stays: crash traces indent their frames with it.
    """
    return "".join(
        (
            ascii(ch)[1:-1]
            if (ch != "\t" and unicodedata.category(ch) == "Cc") or ch in "\u2028\u2029"
            else ch
        )
        for ch in text
    )


@dataclass(frozen=True)
class LogRecord:
    """One record logd stored, as ``logcat -B`` hands it over.

    ``pid`` and ``uid`` are logd's, taken from the writing socket's
    credentials (SO_PEERCRED): an app cannot choose them. ``tid``, the time
    (``sec`` / ``nsec``) and ``log_id`` come from the header the writer sent
    (AOSP logd LogListener), and ``tag`` and ``message`` are the writer's,
    verbatim.
    """

    pid: int
    tid: int
    uid: int
    log_id: int
    sec: int
    nsec: int
    priority: str
    tag: str
    message: str

    def lines(self, tz: tzinfo | None = None) -> list[str]:
        """The record as ``logcat -v threadtime -v usec`` prints it, its controls escaped.

        Measured against the device's own text for the same records
        (logcat_app_restart_records, logcat_crash_tail): the header is
        ``MM-DD HH:MM:SS.uuuuuu PID TID P TAG: `` with the pid and tid right-
        aligned in five columns and the tag left-aligned in eight; the message
        is split at every ``\\n`` and each piece printed under the same
        header; a final ``\\n`` adds no line, and an empty message is one line.

        Where it departs from logcat, on purpose: a tag's non-printable
        characters are escaped (:func:`_printable`), and so are a message's
        line-ending and terminal controls (:func:`_line_safe`; ``\\r`` prints
        as ``\\\\r``). logcat prints both raw, so its text can be made to hold
        lines nobody logged; these lines cannot, whoever reads them back.

        Args:
            tz: The zone to print the time in. Default: this host's local
                zone -- the same clock :func:`window_start` asks ``-t`` from,
                so the skill already assumes the device's zone is the host's
                (an emulator takes the host's).
        """
        stamp = datetime.fromtimestamp(self.sec, tz)
        prefix = (
            f"{stamp:%m-%d %H:%M:%S}.{self.nsec // 1000:06d} {self.pid:5d} {self.tid:5d} "
            f"{self.priority} {_printable(self.tag):<8}: "
        )
        pieces = self.message.split("\n")
        if len(pieces) > 1 and pieces[-1] == "":
            pieces.pop()
        return [prefix + _line_safe(piece) for piece in pieces]


def _decode_record(header: bytes, payload: bytes) -> LogRecord:
    """A record from its header and payload bytes (sizes already checked).

    The payload is split as AOSP liblog splits it: the tag runs to the first
    NUL after the priority byte, the message to the next NUL or the end.
    """
    pid, tid, sec, nsec, log_id, uid = _ENTRY_FIELDS.unpack_from(header, _ENTRY_SIZES.size)
    tag, _, rest = payload[1:].partition(b"\0")
    message = rest.split(b"\0", 1)[0]
    return LogRecord(
        pid=pid,
        tid=tid,
        uid=uid,
        log_id=log_id,
        sec=sec,
        nsec=nsec,
        priority=_PRIORITY_LETTERS.get(payload[0] if payload else -1, "?"),
        tag=tag.decode("utf-8", errors="replace"),
        message=message.decode("utf-8", errors="replace"),
    )


def _check_sizes(length: int, header_size: int, offset: int) -> None:
    if not MIN_ENTRY_HEADER <= header_size <= MAX_ENTRY_HEADER or length > MAX_ENTRY_PAYLOAD:
        raise LogFramingError(
            f"`logcat -B` output is not logd's record framing at byte {offset}: header "
            f"size {header_size}, payload {length} (expected a header of "
            f"{MIN_ENTRY_HEADER}..{MAX_ENTRY_HEADER} bytes and at most "
            f"{MAX_ENTRY_PAYLOAD} of payload).",
            offset=offset,
        )


def _read_exactly(stream: BinaryIO, count: int) -> bytes:
    """``count`` bytes of ``stream``, or fewer only at its end.

    A pipe read returns what has arrived, not what was asked for: an
    unbuffered pipe (``bufsize=0``) delivered a valid header in two pieces
    and a single ``read`` took the first for the stream's end (review of PR
    #35). Read again until the count is met or a read returns nothing.
    """
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _read_entry(stream: BinaryIO, offset: int) -> tuple[LogRecord, int] | None:
    """The next record and its size in bytes; None at a clean end."""
    sizes = _read_exactly(stream, _ENTRY_SIZES.size)
    if not sizes:
        return None
    if len(sizes) < _ENTRY_SIZES.size:
        raise LogFramingError(
            "the stream ended inside a record header", offset=offset, truncated=True
        )
    length, header_size = _ENTRY_SIZES.unpack(sizes)
    _check_sizes(length, header_size, offset)
    wanted = header_size - _ENTRY_SIZES.size + length
    rest = _read_exactly(stream, wanted)
    if len(rest) < wanted:
        raise LogFramingError("the stream ended inside a record", offset=offset, truncated=True)
    split = header_size - _ENTRY_SIZES.size
    return _decode_record(sizes + rest[:split], rest[split:]), header_size + length


def read_record(stream: BinaryIO) -> LogRecord | None:
    """The next record of a binary ``logcat -B`` stream; None at its clean end.

    Blocks like ``readline`` does, until a whole record or the end arrives
    -- however many pieces the pipe delivers it in -- and reads at most one
    bounded record. For a live read (``log_monitor``; the ANR readers are
    meant to use it too). :func:`iter_records` is the same over a whole
    stream, with stream offsets in its errors.

    Args:
        stream: A blocking binary file object -- a ``Popen`` stdout opened
            without ``text=`` (buffered or not), or ``io.BytesIO``.

    Raises:
        LogFramingError: A header outside the bounds, or the stream ended
            inside a record (``truncated``). See the module docstring for
            when a truncation may be tolerated: only when the caller stopped
            the stream, and disclosed.
    """
    entry = _read_entry(stream, 0)
    return entry[0] if entry is not None else None


def iter_records(stream: BinaryIO) -> Iterator[LogRecord]:
    """Every record of a binary stream, in order, until its clean end.

    Raises:
        LogFramingError: As :func:`read_record`, with the record's offset.
    """
    offset = 0
    while (entry := _read_entry(stream, offset)) is not None:
        offset += entry[1]
        yield entry[0]


def parse_records(data: bytes) -> list[LogRecord]:
    """Every record of a whole ``logcat -d -B`` dump.

    Raises:
        LogFramingError: The bytes are not whole records -- including a dump
            that ends inside one, which a complete dump never does.
    """
    return [record for _raw, record in parse_frames(data)]


def parse_frames(data: bytes) -> list[tuple[bytes, LogRecord]]:
    """:func:`parse_records`, each record beside its frame: the bytes logd sent, whole.

    For a reader that must tell one record from another (:func:`read_after`):
    two records can decode alike -- a payload that is not UTF-8 decodes with
    replacement characters -- and only their bytes are theirs.

    Raises:
        LogFramingError: As :func:`parse_records`.
    """
    records: list[tuple[bytes, LogRecord]] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < _ENTRY_SIZES.size:
            raise LogFramingError(
                "the dump ended inside a record header", offset=offset, truncated=True
            )
        length, header_size = _ENTRY_SIZES.unpack_from(data, offset)
        _check_sizes(length, header_size, offset)
        end = offset + header_size + length
        if end > len(data):
            raise LogFramingError("the dump ended inside a record", offset=offset, truncated=True)
        records.append(
            (
                data[offset:end],
                _decode_record(
                    data[offset : offset + header_size], data[offset + header_size : end]
                ),
            )
        )
        offset = end
    return records


# --- a live read made of dumps: the cursor read --------------------------------
#
# A live ``logcat -B`` is stopped by terminating it, and whatever logcat had
# not yet written dies with it. Android 13's logcat (API 33) writes ``-B``
# records through stdio and flushes only after a TEXT record (AOSP
# system/logging logcat.cpp, android13-release: the binary branch is an
# ``fwrite`` with no ``fflush``; Android 12 wrote each record with ``write``,
# 14 and 15 flush after either branch), so its records wait in a 4 KiB buffer.
# Recorded on the API 33 emulator: a 5 s stream delivered no byte while its
# window held records (logcat_stream_quiet_records, logcat_stream_marks_records),
# or whole 4096-byte blocks only (logcat_stream_block_records). API 35 delivered
# each record as it was logged.
#
# A dump ends by itself and writes everything it read, so on that release a
# live read is made of dumps instead, each continuing from a cursor: the newest
# record a system process wrote that the read has reached -- an identity no app
# can forge; when the buffer holds none, a marker the read writes as shell --
# kept as its frame, byte for byte, with the frames that followed it, and found
# again in a count-based tail of logd's order. Timestamps position nothing.
#
# What a cursor read proves, and what it does not: finding the cursor's frame
# and what followed it in the dump proves logd still held the window's start
# when the dump began.
# It does not prove the window is complete. A capture is what logd delivered,
# and logd may drop records it prunes while any reader is behind -- Android
# 13's SerializedLogBuffer prunes while it flushes to a slow reader without
# disconnecting it, a property of every logd reader, streams included, on
# every level. So no completeness is claimed, and none is to be added.

# The one release whose logcat holds ``-B`` records back while it streams.
BUFFERED_BINARY_SDK = 33


# A record's uid is logd's, from the writing socket's credentials (SO_PEERCRED;
# AOSP logd LogListener), and the one part of a frame an app cannot choose:
# below AID_APP_START within its user, the writer is the platform's -- root,
# system_server, the daemons, and shell (2000), which is what `adb shell log`
# writes as. Apps, isolated processes, app zygotes and SDK sandboxes are all at
# or above it (AOSP android_filesystem_config.h). Only such a record identifies
# where a read is: an app can send the same datagram twice, time, tid and
# payload included, and logd stores two byte-identical frames.
AID_APP_START = 10_000
AID_USER_OFFSET = 100_000


def system_written(record: LogRecord) -> bool:
    """Whether a system process wrote ``record``: its uid below AID_APP_START in its user."""
    return record.uid % AID_USER_OFFSET < AID_APP_START


@dataclass(frozen=True)
class Cursor:
    """Where a dump-made read continues: the newest system record read, and what followed it.

    There is always such a record: a read whose buffer holds none writes one
    (:func:`find_anchor`), so no read ever continues from app-written bytes
    alone -- an app's copies of its own records can reproduce them exactly.

    Attributes:
        raw: That record's frame, as logd sent it; an app cannot write one
            like it (:func:`system_written`).
        record: The same record, decoded (for what a refusal says of it).
        suffix: The frames that followed it in logd's order, all already
            read, byte for byte -- an app's, none of them a system record.
    """

    raw: bytes
    record: LogRecord
    suffix: tuple[bytes, ...] = ()


class CursorLostError(RuntimeError):
    """The dump does not hold the read's place: logd's buffer turned over past it.

    The cursor's record is gone, or a record read after it is: the window's
    start is gone, and records logged after it may have gone with it, so the
    read is refused rather than reported from a later point.
    """

    remedy = (
        "Capture for less time, or enlarge the device's log buffer (`adb logcat -G 16M`) and "
        "retry."
    )


class AmbiguousCursorError(RuntimeError):
    """The dump holds the cursor's frame more than once, so where the read resumes is a guess.

    The cursor is a system process's record, and system writers do not send
    one datagram twice; were one to, which copy is the cursor cannot be told,
    and no copy is picked.
    """

    remedy = (
        "Retry: the next read starts from a newer record. If it repeats, a system process "
        "on the device is logging byte-identical records; read the window with --last instead."
    )


class AnchorNotFoundError(RuntimeError):
    """The marker the read wrote to anchor itself is not in the device's log buffer."""

    remedy = (
        "Retry. If it repeats, check that `adb shell log -t aes-log-monitor test` shows up in "
        "`adb logcat -d -s aes-log-monitor`."
    )


class CursorBeyondCapError(RuntimeError):
    """The cursor is not in the largest tail the byte cap allows: what followed it is too much."""

    remedy = (
        "Capture for less time, or raise ANDROID_EMU_LOG_DUMP_CAP_BYTES; on a busy device, a "
        "shorter ANDROID_EMU_LOG_POLL_SECONDS keeps each read small."
    )


# The read is positioned by logd's own order, never by time. A record's time is
# its WRITER's (AOSP logd LogListener takes `realtime` from the header a process
# sends to /dev/socket/logdw), on a clock that can step (NTP, a manual set), so
# `-t <time>` -- logd filtering by those stamps -- could be moved past real
# records by one record stamped in the future, or by a clock stepping back;
# four rounds of review found a new such interleaving each time. A count-based
# tail, `-d -B -T N`, is the newest N records in logd's order (measured on API
# 33: exactly N whole frames, logcat_tail_records), whatever their stamps: a
# suffix of the sequence the cursor sits in.

# The first tail a read asks for, and the floor of every later one.
INITIAL_TAIL = 64


def tail_args(count: int) -> list[str]:
    """``-d -B -T <count>``: the newest ``count`` records, as a dump that ends by itself."""
    return logcat_args(dump=True, binary=True, tail=count)


def newest_args() -> list[str]:
    """``-d -B -T 1``: the newest record the device holds, where the opening search starts."""
    return tail_args(1)


def cursor_from(data: bytes) -> Cursor | None:
    """The cursor a dump places: its newest system record and the frames after it; None if none.

    Raises:
        LogFramingError: The dump is not whole records.
    """
    return _placed(parse_frames(data))


def _placed(frames: list[tuple[bytes, LogRecord]]) -> Cursor | None:
    for index in range(len(frames) - 1, -1, -1):
        raw, record = frames[index]
        if system_written(record):
            return Cursor(raw, record, tuple(later for later, _r in frames[index + 1 :]))
    return None


def _cap_check(count: int, data: bytes, max_bytes: int | None, missing: str) -> None:
    if max_bytes is not None and 2 * len(data) > max_bytes:
        raise CursorBeyondCapError(
            f"the newest {count} records ({len(data)} bytes) {missing}, and twice as many "
            f"would pass the {max_bytes}-byte cap."
        )


def open_cursor(
    dump: Callable[[list[str]], bytes], *, max_bytes: int | None = None
) -> Cursor | None:
    """Where a read opens: the newest system record the device holds, and what followed it.

    Asks for the newest record (``-d -B -T 1``) and doubles the tail until it
    holds a system record (:func:`system_written`); the records after it were
    logged before the read opened and are its suffix, never the window's. A
    tail that comes back short is the whole buffer: with no system record in
    it -- empty, as after ``--clear``, or only apps' -- there is nothing to
    open on (None), and the caller writes one (:func:`find_anchor`). Apps'
    records alone never place a read: copies of one datagram can reproduce
    them byte for byte after the originals turn over.

    Raises:
        CursorBeyondCapError: No system record before the tail would pass
            ``max_bytes``.
        LogFramingError: A dump is not whole records.
    """
    count = 1
    while True:
        data = dump(tail_args(count))
        frames = parse_frames(data)
        cursor = _placed(frames)
        if cursor is not None:
            return cursor
        if len(frames) < count:
            return None
        _cap_check(count, data, max_bytes, "hold no record a system process wrote")
        count *= 2


# The tag of the marker a read writes when the buffer holds no system record.
ANCHOR_TAG = "aes-log-monitor"


def find_anchor(
    dump: Callable[[list[str]], bytes], message: str, *, max_bytes: int | None = None
) -> Cursor:
    """The marker the caller just wrote, as the read's cursor: what follows it is the window.

    The caller wrote ``message`` under :data:`ANCHOR_TAG` with ``adb shell
    log``, as shell (uid 2000: a system writer, :func:`system_written`) -- a
    frame no app can write, and ``message`` holds a nonce no one else has. The
    tail is doubled from the newest record until it holds that frame; the
    records after it were logged after it was, and are the window's (an empty
    suffix), those before it not.

    Raises:
        AnchorNotFoundError: The whole buffer does not hold it.
        AmbiguousCursorError: A tail holds it more than once.
        CursorBeyondCapError: Not found before the tail would pass ``max_bytes``.
        LogFramingError: A dump is not whole records.
    """
    count = 1
    while True:
        data = dump(tail_args(count))
        frames = parse_frames(data)
        found = [
            (raw, record)
            for raw, record in frames
            if system_written(record) and record.tag == ANCHOR_TAG and record.message == message
        ]
        if len(found) > 1:
            raise AmbiguousCursorError(
                f"the dump holds the marker the read wrote ({ANCHOR_TAG}: {message}) "
                f"{len(found)} times, so where the read starts cannot be told."
            )
        if found:
            return Cursor(*found[0])
        if len(frames) < count:
            raise AnchorNotFoundError(
                f"the marker the read wrote to anchor itself ({ANCHOR_TAG}: {message}) is not "
                f"in the device's log buffer ({len(frames)} records), so where the window "
                "begins cannot be told."
            )
        _cap_check(count, data, max_bytes, "do not reach the marker the read wrote")
        count *= 2


def read_after(
    cursor: Cursor,
    dump: Callable[[list[str]], bytes],
    *,
    tail: int = INITIAL_TAIL,
    max_bytes: int | None = None,
) -> tuple[list[LogRecord], Cursor]:
    """The records logd delivered after ``cursor``, found in a tail of its order; and the next cursor.

    Asks for the newest ``tail`` records (:func:`tail_args`). A tail is a
    suffix of logd's order, so a tail that holds the cursor's record holds
    everything after it. That record must be there exactly once, byte for
    byte; twice or more, where to resume is a guess
    (:class:`AmbiguousCursorError`). Absent from a tail of ``tail`` records,
    more than that followed it: the tail is doubled and asked again. Absent
    from a tail that came back short -- the whole buffer -- logd's buffer
    turned over past it (:class:`CursorLostError`). Right after it, the
    cursor's suffix must follow, frame for frame (logd's order does not
    change); a frame of it missing or different is the same turnover
    (:class:`CursorLostError`). What follows the suffix is the read's.
    Without a new system record, the remembered suffix and the required dump
    grow until turnover or the byte cap refuses continuation.

    The cursor is a system process's record, the one identity an app cannot
    forge (:func:`system_written`): an app resending its own records -- the
    cursor's neighbours, or all of them -- adds records; it cannot move the
    read. Trusted writers must not replay complete frames; duplicate
    detection covers copies present in the searched dump. No record's
    timestamp positions anything, so a stamp a writer
    forged, ahead or behind, or a clock that stepped, can neither move the
    read nor hide what follows the cursor.

    What this proves, and what it does not: the cursor's record and its suffix
    prove logd still held the window's start when the dump began. It does NOT
    prove the window complete. The records returned are what logd delivered,
    and logd may drop records it prunes while a reader is behind. Zero records
    means logd delivered none after the cursor.

    Args:
        cursor: Where the read is (:func:`open_cursor`, :func:`find_anchor`,
            or a previous call's).
        dump: Runs ``adb logcat <args>`` to its end and returns what it wrote.
            The caller's, so its bounds and its failures are the caller's, and
            this module stays one that never touches a device.
        tail: The first tail to ask for (at least ``INITIAL_TAIL``).
        max_bytes: Stop doubling before a tail of about twice the last one's
            bytes would pass this (:class:`CursorBeyondCapError`).

    Returns:
        The records after the cursor and its suffix, in logd's order, and the
        cursor to read on from: the newest system record of the dump with the
        frames after it -- or, with none among the new records, the same
        record with the new frames added to its suffix.

    Raises:
        CursorLostError: The whole buffer does not hold the cursor's record,
            or a frame of its suffix is not right after it.
        AmbiguousCursorError: A tail holds the cursor's frame more than once.
        CursorBeyondCapError: The tail that would hold it would pass ``max_bytes``.
        LogFramingError: A dump is not whole records.
    """
    count = max(INITIAL_TAIL, tail)
    while True:
        data = dump(tail_args(count))
        frames = parse_frames(data)
        start = _suffix_at(cursor, frames, whole=len(frames) < count)
        if start is not None:
            end = start + len(cursor.suffix)
            if tuple(raw for raw, _record in frames[start:end]) != cursor.suffix:
                raise CursorLostError(
                    "the device's log buffer no longer holds every record the read took after "
                    f"{_named(cursor)}: one is gone, so what was logged after it may have gone "
                    "with it."
                )
            after = frames[end:]
            placed = _placed(after) or Cursor(
                cursor.raw, cursor.record, tuple(raw for raw, _record in frames[start:])
            )
            return [record for _raw, record in after], placed
        _cap_check(count, data, max_bytes, "do not reach the record the read continues after")
        count *= 2


def _named(cursor: Cursor) -> str:
    return (
        f"the record the read continues from (stamped {cursor.record.sec}.{cursor.record.nsec:09d})"
    )


def _suffix_at(cursor: Cursor, frames: list[tuple[bytes, LogRecord]], whole: bool) -> int | None:
    """Where ``cursor``'s suffix starts in ``frames``; None when the tail must double."""
    found = [index for index, (raw, _record) in enumerate(frames) if raw == cursor.raw]
    if len(found) > 1:
        raise AmbiguousCursorError(
            f"the dump holds {_named(cursor)} {len(found)} times, byte for byte, so where the "
            "read resumes cannot be told."
        )
    if not found and whole:
        raise CursorLostError(
            f"the device's log buffer no longer holds {_named(cursor)}: it turned over, and what "
            "was logged after that record may have gone with it."
        )
    return found[0] + 1 if found else None


def records_text(records: Iterable[LogRecord], tz: tzinfo | None = None) -> str:
    """Records printed as ``-v threadtime -v usec`` text, one ``\\n`` after each line."""
    return "".join(f"{line}\n" for record in records for line in record.lines(tz))


def parse_duration(text: str) -> float:
    """Parse a duration string like ``30s``, ``5m`` or ``1h`` into seconds.

    Args:
        text: Duration string. Case-insensitive.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: If the string carries no recognisable ``<int><s|m|h>``.
    """
    match = _DURATION.match(text.lower().strip()) if text else None
    if not match:
        raise ValueError(f"Invalid duration format: {text!r}. Use format like '30s', '5m', '1h'.")
    value, unit = match.groups()
    return int(value) * _UNIT_SECONDS[unit]


def window_start(seconds: float, now: datetime | None = None) -> str:
    """Return the ``logcat -t`` timestamp for a window ``seconds`` wide.

    Args:
        seconds: Width of the historical window, ending at ``now``.
        now: Reference time; defaults to the current time. Injectable so tests
            can assert an exact timestamp rather than a moving one.

    Returns:
        A ``"MM-DD HH:MM:SS.000"`` string.
    """
    reference = now or datetime.now()
    return (reference - timedelta(seconds=seconds)).strftime(TIMESTAMP_FORMAT)


def window_start_for(duration: str, now: datetime | None = None) -> str:
    """``parse_duration`` then ``window_start``, for the common one-step case.

    Raises:
        ValueError: If ``duration`` is unparseable.
    """
    return window_start(parse_duration(duration), now=now)


def parse_severities(text: str) -> list[str]:
    """Parse a comma-separated severity list, refusing any name outside the set.

    Args:
        text: e.g. ``"error,warning"``. Case and surrounding spaces are ignored;
            a repeated name counts once.

    Returns:
        The names, in the order given.

    Raises:
        ValueError: A name is not one of :data:`SEVERITY_NAMES`, or the list
            names none. The message lists the accepted set.
    """
    accepted = ", ".join(SEVERITY_NAMES)
    names: list[str] = []
    for raw in text.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in SEVERITY_PRIORITIES:
            raise ValueError(f"unknown severity {raw.strip()!r}; accepted: {accepted}")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError(f"no severity named; accepted: {accepted}")
    return names


def min_priority_for(severities: list[str] | None) -> str | None:
    """Resolve a severity filter to the single lowest logcat priority letter.

    ``*:W`` shows warnings *and* errors, so a filter of several severities
    collapses to its lowest member rather than to several filter terms. The
    device-side filter is exact from that member up: on the API 35 emulator,
    ``*:W`` returned the same 884 W/E/F lines a host-side filter kept from an
    unfiltered read of the same window -- 887 lines in all, against 2929.

    Args:
        severities: Severity names from :data:`SEVERITY_NAMES`.

    Returns:
        One priority letter, or None when no filter was given.

    Raises:
        ValueError: A name is not a known severity. It used to be skipped, so
            ``["warn"]`` resolved to no filter at all and ``["error", "warn"]``
            to ``E`` -- the warnings silently gone (D1).
    """
    if not severities:
        return None
    unknown = [severity for severity in severities if severity not in SEVERITY_PRIORITIES]
    if unknown:
        raise ValueError(f"unknown severity {unknown[0]!r}; accepted: {', '.join(SEVERITY_NAMES)}")
    letters = [letter for severity in severities for letter in SEVERITY_PRIORITIES[severity]]
    return PRIORITY_ORDER[min(PRIORITY_ORDER.index(letter) for letter in letters)]


def parse_threadtime(line: str) -> tuple[str, str] | None:
    """``(pid, priority)`` of one threadtime line; None for anything else.

    "Anything else" is the reader's ``--------- beginning of main`` separator
    in practice: in threadtime every line of a message carries its own header.

    Args:
        line: One line of ``logcat -v threadtime`` output.
    """
    match = _THREADTIME.match(line)
    return (match["pid"], match["priority"]) if match else None


# --- one app's window: which records are its processes' ---------------------
#
# A record is the app's only when (i) the device vouches that its writer's pid
# was the app's process -- its uid the package's -- and (ii) the record falls
# inside that pid's life as the app's process, and (iii) logd stamped the
# record with one of the package's uids. The evidence, all recorded in
# logcat_app_restart_records (an app crashed, was relaunched, force-stopped
# and relaunched):
#
#     ActivityManager: Start proc 21149:<package>/u0a205 for next-top-activity {...}
#     ActivityManager: Process <package> (pid 21149) has died: fg  TOP
#     ActivityManager: Killing 21205:<package>/u0a205 (adj 0): stop <package> ...
#
# plus `pidof <package>` for the process alive now, its uid checked with `ps`.
# What does NOT count, and why (security reviews of #27 and v0.8.0):
#
# - the package name on its own: most records naming it are system_server's,
#   the launcher's and the IME's;
# - the tag: any app may log under ActivityManager -- a sentence counts only
#   when system_server WROTE it (the record's pid, which logd sets, is
#   system_server's pid, asked of the device; unknown, nothing counts);
# - a sentence anywhere but the very start of the record's message:
#   system_server's messages carry app-controlled text further on (a
#   component, an intent), including whole further lines;
# - anything a text read would call a line of its own: logcat prints a tag
#   verbatim, so a tag holding "\n<header>" printed a forged system_server
#   line (logcat_tag_injection). Records are read whole (`-B`), so a record is
#   one writer's whatever its tag or message holds;
# - the process name: an app may name its own process after another package
#   (`android:process`), and system_server then really logs `Start proc
#   N:<that package>/<the impostor's uid>`. Start proc and Killing carry the
#   uid; it must be the package's (asked of the device; unknown, nothing
#   counts). `has died` carries none, so it only ends a life already
#   confirmed;
# - a crash's own `AndroidRuntime: Process: <package>, PID: N`: any app can
#   write it for another package through the public Log API (ruling A).
#
# What is NOT shown, and is still reported: a record logd stamped with the
# package's uid whose pid the window never ties to the package -- a process
# whose `Start proc` is older than the window and that is gone by its end
# (a crash read too late). Its lines are left out, counted, and the window is
# marked incomplete (`unresolved`), rather than dropped as if the app had
# logged nothing. A record under any other uid is another app's: proven, not
# unresolved.

# The sentences themselves -- who wrote them, their exact opening, the uid --
# are read by common.log_lifecycle, the one reading the ANR session worker
# uses too (PR #34). What a sentence means for ONE package's window is decided
# here.

# Kept importable from here, where it was first defined.
uid_from_formatted = log_lifecycle.uid_from_formatted

# What a sentence says about the pid it names.
_STARTED = "started"  # an app process began (uid checked)
_KILLED = "killed"  # an app process was killed (uid checked)
_DIED_APP = "died"  # a process named like the app died (no uid: ends only)
_OTHER = "other"  # the pid is (now) some other process's


class _Reader:
    """Reads records in order: which pid wrote each, and what a sentence says of the package."""

    def __init__(self, package: str, system_server_pid, package_uids) -> None:
        self.package = package
        self.package_uids = log_lifecycle.Lazy(package_uids)
        self._lifecycle = log_lifecycle.LifecycleReader(
            system_server_pid, lambda name: self.package_uids() if name == package else None
        )

    def uids(self) -> frozenset[int]:
        """The package's uids (asked of the device once); empty when unknown."""
        return log_lifecycle.uid_set(self.package_uids())

    def read(self, record: LogRecord) -> tuple[str, tuple[str, str] | None]:
        """``(pid that wrote record, (what, pid) said by it or None)``."""
        writer, sentence = self._lifecycle.read(record)
        if sentence is None:
            return writer, None
        if sentence.name != self.package:
            return writer, (_OTHER, sentence.pid)
        if sentence.what == log_lifecycle.DIED:
            return writer, (_DIED_APP, sentence.pid)
        # Any of the package's uids: it has one per user it is installed for
        # (recorded: package_list_uid_two_users).
        if not self._lifecycle.is_package(sentence):
            return writer, (_OTHER, sentence.pid)
        return writer, (
            _STARTED if sentence.what == log_lifecycle.STARTED else _KILLED,
            sentence.pid,
        )


def _spans(events: list[tuple[int, str]], end: int, alive: bool) -> list[tuple[int, int]]:
    """The index ranges in which one pid was the app's process.

    A life runs from ``Start proc`` to ``Killing`` / ``has died`` / the pid's
    next owner. With no start in the window, a uid-checked ``Killing`` -- or
    ``pidof`` and ``ps`` for a process alive at the end -- opens it at the
    window's start. ``has died`` alone opens nothing: it carries no uid.
    """
    spans: list[tuple[int, int]] = []
    opened: int | None = None
    seen = False
    for index, what in events:
        if what == _STARTED:
            if opened is not None:
                spans.append((opened, index))
            opened = index
        else:
            if opened is not None:
                spans.append((opened, index))
            elif what == _KILLED and not seen:
                spans.append((0, index))
            opened = None
        seen = True
    if opened is not None:
        spans.append((opened, end))
    elif alive and not seen:
        spans.append((0, end))
    return spans


@dataclass
class AppWindow:
    """One app's records of a logcat window, and what decided them.

    Attributes:
        records: The app's records, in input order.
        pids: Pids with a life in the window.
        pids_from_log: Those of them the log itself named.
        unresolved_lines: Lines of records logd stamped with the package's
            uid that are NOT in ``records``: their pid's life as the app's is
            not in evidence (see the section comment). Nonzero, the window is
            incomplete.
        unresolved_pids: The pids that wrote them.
    """

    records: list[LogRecord]
    pids: set[str]
    pids_from_log: set[str]
    unresolved_lines: int = 0
    unresolved_pids: set[str] | None = None

    def lines(self, tz: tzinfo | None = None) -> list[str]:
        """The window as threadtime text lines (see :meth:`LogRecord.lines`)."""
        return [line for record in self.records for line in record.lines(tz)]

    @property
    def complete(self) -> bool:
        """Whether no record of the package's uid was left out for lack of evidence."""
        return not self.unresolved_lines


def split_log_lines(text: str) -> list[str]:
    """logcat text as lines, split on ``\\n`` ONLY.

    logcat frames each line with ``\\n`` and leaves a ``\\r`` an app wrote
    inside its message raw (AOSP liblog logprint), as :meth:`LogRecord.lines`
    does. ``str.splitlines`` -- and a text-mode pipe -- also break on
    ``\\r``, which let an app append ``\\r<a forged header>`` to its own line
    and have it read as a line of its own (security reviews of #27 and
    v0.8.0). A single ``\\r`` ending a line is dropped, as a line terminator;
    one inside stays.
    """
    return [line.removesuffix("\r") for line in text.split("\n")]


def app_window(
    records: Sequence[LogRecord],
    package: str,
    *,
    system_server_pid: str | Callable[[], str | None] | None = None,
    package_uids: int | Iterable[int] | Callable[[], Iterable[int] | None] | None = None,
    running: Iterable[str] = (),
) -> AppWindow:
    """The records of a whole (historical) window that were ``package``'s processes'.

    Args:
        records: ``logcat -B`` records (:func:`parse_records`), in order.
        package: The application id.
        system_server_pid: system_server's pid, or a function returning it
            (None when the device cannot say); asked only if the window holds
            an ActivityManager sentence. Unknown: no sentence counts.
        package_uids: The package's uids -- one per user it is installed for --
            or a function returning them; asked once, when the window holds a
            record. A sentence's uid, and a kept record's, must be one of them.
            Unknown: nothing is the app's.
        running: Pids alive now whose uid the caller checked is the package's
            (``pidof`` then ``ps``). Each one's life runs to the window's end.

    Returns:
        The app's records in input order, the pids with a life in the window,
        those of them the log itself named, and what of the package's uid was
        left out unresolved.
    """
    reader = _Reader(package, system_server_pid, package_uids)
    writers: list[str] = []
    events: dict[str, list[tuple[int, str]]] = {}
    for index, record in enumerate(records):
        writer, sentence = reader.read(record)
        writers.append(writer)
        if sentence is not None:
            events.setdefault(sentence[1], []).append((index, sentence[0]))
    alive = set(running)
    spans = {
        pid: found
        for pid in set(events) | alive
        if (found := _spans(events.get(pid, []), len(records), pid in alive))
    }
    uids = reader.uids() if records else frozenset()
    kept: list[LogRecord] = []
    unresolved_lines = 0
    unresolved_pids: set[str] = set()
    for index, (record, writer) in enumerate(zip(records, writers, strict=True)):
        if record.uid not in uids:
            continue
        if writer in spans and any(start <= index <= end for start, end in spans[writer]):
            kept.append(record)
        else:
            unresolved_lines += len(record.lines())
            unresolved_pids.add(writer)
    from_log = {
        pid for pid in spans if any(w in (_STARTED, _KILLED) for _, w in events.get(pid, []))
    }
    return AppWindow(kept, set(spans), from_log, unresolved_lines, unresolved_pids)


def parse_package_pids(
    records: Sequence[LogRecord],
    package: str,
    *,
    system_server_pid: str | Callable[[], str | None] | None = None,
    package_uids: int | Iterable[int] | Callable[[], Iterable[int] | None] | None = None,
) -> set[str]:
    """The pids a logcat window itself says were ``package``'s processes.

    The pids with a life in :func:`app_window` from the log alone. Both
    ``system_server_pid`` and ``package_uids`` must be known for any sentence
    to count; without them, none does (fail closed).
    """
    return app_window(
        records, package, system_server_pid=system_server_pid, package_uids=package_uids
    ).pids


class AppWindowStream:
    """:func:`app_window` for a live read: decides each record as it arrives.

    A live read cannot see ahead, and a process already running when it began
    is named only when it is killed. So a record of the package's uid from a
    pid not yet decided is held -- up to ``pending_cap`` lines, oldest dropped
    first -- and returned when a uid-checked ``Killing`` makes the pid the
    app's. A record under any other uid is never the app's and is never held.
    Every record is numbered in input order, so a caller can store what it
    keeps in that order whatever the order of decision.

    Attributes:
        pids: Pids with a life in the read so far.
        pids_from_log: Those of them the log named.
        pending_evicted: Lines of a pid later made the app's that the hold had
            already dropped at its cap -- lines missing from the window.
    """

    def __init__(
        self,
        package: str,
        *,
        system_server_pid: str | Callable[[], str | None] | None = None,
        package_uids: int | Iterable[int] | Callable[[], Iterable[int] | None] | None = None,
        running: Iterable[str] = (),
        pending_cap: int | None = None,
    ) -> None:
        self._reader = _Reader(package, system_server_pid, package_uids)
        self._open: set[str] = set(running)
        self._decided: set[str] = set(running)
        self.pids: set[str] = set(running)
        self.pids_from_log: set[str] = set()
        self._cap = pending_cap
        # (sequence, pid, record, its line count), oldest first.
        self._pending: deque[tuple[int, str, LogRecord, int]] = deque()
        self._held_lines = 0
        self._evicted: dict[str, int] = {}
        self.pending_evicted = 0
        self._unresolved: dict[str, int] = {}
        self._sequence = 0

    @property
    def unattributed(self) -> int:
        """Lines held now: the package's uid, from pids not (yet) shown to be the app's."""
        return self._held_lines

    @property
    def unresolved_lines(self) -> int:
        """Lines of the package's uid left out so far, held ones included.

        A pid the read decided was not the app's, or never decided -- its
        held lines and the ones the cap dropped. Nonzero: incomplete.
        """
        return sum(self._unresolved.values()) + sum(self._evicted.values()) + self._held_lines

    @property
    def unresolved_pids(self) -> set[str]:
        """The pids whose lines :attr:`unresolved_lines` counts."""
        return set(self._unresolved) | set(self._evicted) | {pid for _, pid, _, _ in self._pending}

    def feed(self, record: LogRecord) -> list[tuple[int, LogRecord, bool]]:
        """Read one record; return ``(sequence, record, replayed)`` for what is now the app's."""
        sequence = self._sequence
        self._sequence += 1
        writer, sentence = self._reader.read(record)
        kept = self._apply(sentence) if sentence is not None else []
        if record.uid not in self._reader.uids():
            return kept
        if writer in self._open:
            kept.append((sequence, record, False))
        elif writer not in self._decided:
            self._hold(sequence, writer, record)
        else:
            self._unresolved[writer] = self._unresolved.get(writer, 0) + len(record.lines())
        return kept

    def _apply(self, sentence: tuple[str, str]) -> list[tuple[int, LogRecord, bool]]:
        what, pid = sentence
        replayed: list[tuple[int, LogRecord, bool]] = []
        if what == _STARTED:
            # Anything pid logged before its start was an earlier process's.
            self._settle_unresolved(pid)
            self._open.add(pid)
            self.pids.add(pid)
            self.pids_from_log.add(pid)
        elif what == _KILLED and pid not in self._decided:
            replayed = [(seq, record, True) for seq, record in self._release(pid)]
            self.pending_evicted += self._evicted.pop(pid, 0)
            self.pids.add(pid)
            self.pids_from_log.add(pid)
            self._open.discard(pid)
        else:
            self._settle_unresolved(pid)
            self._open.discard(pid)
        self._decided.add(pid)
        return replayed

    def _settle_unresolved(self, pid: str) -> None:
        """``pid``'s held and dropped lines stay out of the window, as unresolved."""
        lines = sum(count for _, count in self._release_counted(pid)) + self._evicted.pop(pid, 0)
        if lines:
            self._unresolved[pid] = self._unresolved.get(pid, 0) + lines

    def _hold(self, sequence: int, pid: str, record: LogRecord) -> None:
        count = len(record.lines())
        self._pending.append((sequence, pid, record, count))
        self._held_lines += count
        while self._cap is not None and self._held_lines > self._cap and self._pending:
            _, dropped, _, dropped_count = self._pending.popleft()
            self._held_lines -= dropped_count
            self._evicted[dropped] = self._evicted.get(dropped, 0) + dropped_count

    def _release_counted(self, pid: str) -> list[tuple[tuple[int, LogRecord], int]]:
        """Take ``pid``'s held records out of the hold, in arrival order, with line counts."""
        if not any(held == pid for _, held, _, _ in self._pending):
            return []
        mine = [((seq, rec), count) for seq, held, rec, count in self._pending if held == pid]
        self._pending = deque(entry for entry in self._pending if entry[1] != pid)
        self._held_lines -= sum(count for _, count in mine)
        return mine

    def _release(self, pid: str) -> list[tuple[int, LogRecord]]:
        """Take ``pid``'s held records out of the hold, in arrival order."""
        return [entry for entry, _ in self._release_counted(pid)]


def logcat_args(
    *,
    buffer_name: str | None = None,
    dump: bool = False,
    clear: bool = False,
    since: str | None = None,
    fmt: str | None = DEFAULT_FORMAT,
    pid: str | int | None = None,
    min_priority: str | None = None,
    tag_priorities: Sequence[tuple[str, str]] = (),
    usec: bool = False,
    tail: int | None = None,
    binary: bool = False,
) -> list[str]:
    """Build the argument tail that follows ``adb [-s S] logcat``.

    Returned separately from the full command because half the callers run
    through ``run_adb("logcat", serial, *logcat_args(...))`` (bounded, typed
    errors) and half need a bare argv for a streaming ``Popen``.

    Args:
        buffer_name: ``-b`` buffer, e.g. ``crash``. Default (None) leaves the
            device's default buffer set.
        dump: Add ``-d`` -- print what is buffered and exit instead of
            following. Implied by ``since``, which is meaningless while
            following.
        clear: Add ``-c`` -- clear the buffer instead of reading it.
        since: A ``"MM-DD HH:MM:SS.mmm"`` timestamp for ``-t``. Note that
            ``-t N`` with a bare integer means the last N *lines*, not a
            duration; that confusion silently turned "one minute of logs" into
            "sixty lines" here once, hence the timestamp-only signature.
        fmt: ``-v`` format. Defaults to ``threadtime``; pass None to inherit
            the device's format (only safe when nothing parses the output).
        usec: Add ``-v usec``: microsecond headers, which a text reader needs
            to tell a continuation line (see USEC_FORMAT).
        tail: For a live read, ``-T <tail>``: start at the END of the buffer
            (the most recent ``tail`` records, then follow) instead of
            replaying it all. Measured: placed before filterspecs, ``-T 1``
            prints the newest record -- every line of it -- and then follows.
        pid: Restrict to one process via ``--pid=``. Falsy values are omitted,
            which reads EVERY process: a caller that means one app must not
            reach here with an unresolved pid, or its "app" window is the whole
            device (D4). One pid is also one process, not an app that
            restarted; see :func:`parse_package_pids`.
        min_priority: A single letter for the trailing ``*:<priority>`` term.
        tag_priorities: ``(tag, letter)`` terms placed before ``*:<priority>``,
            so one tag is read below the global floor (logcat applies the most
            specific term).
        binary: ``-B`` in place of every ``-v`` term: logd's records whole
            (:func:`parse_records`). ``fmt``, ``usec``, ``min_priority`` and
            ``tag_priorities`` are then left out: measured on API 35, ``-B``
            ignores filterspecs (``*:E`` still returned I and D records), so
            sending them would only suggest a filter that is not applied. A
            binary reader filters on the host. ``-d``, ``-t``, ``-T`` and
            ``--pid`` are logd's own and still apply; ``-T N`` and ``-t N``
            count RECORDS (measured: ``-T 1`` returned one 13-line crash).

    Returns:
        The argument list, in the fixed order documented at module level.
    """
    args: list[str] = []
    if buffer_name:
        args.extend(["-b", buffer_name])
    if clear:
        args.append("-c")
        # A clear neither reads nor formats anything; the remaining options
        # would be accepted by adb and mean nothing.
        return args
    if dump or since is not None:
        args.append("-d")
    if since is not None:
        args.extend(["-t", since])
    if binary:
        args.append(BINARY_FLAG)
    else:
        if fmt:
            args.extend(["-v", fmt])
        if usec:
            args.extend(["-v", USEC_FORMAT])
    if tail is not None:
        args.extend(["-T", str(tail)])
    if pid:
        args.append(f"--pid={pid}")
    if not binary:
        args.extend(f"{tag}:{letter}" for tag, letter in tag_priorities)
        if min_priority:
            args.append(f"*:{min_priority}")
    return args


def build_logcat_command(serial: str | None = None, **kwargs) -> list[str]:
    """Full ``adb`` argv for a logcat read: ``build_adb_command`` + ``logcat_args``.

    Args:
        serial: Device serial; ``-s`` is omitted when None.
        **kwargs: Passed straight to :func:`logcat_args`.

    Returns:
        Complete argv, ready for subprocess. Never a shell string.
    """
    return build_adb_command("logcat", serial, *logcat_args(**kwargs))
