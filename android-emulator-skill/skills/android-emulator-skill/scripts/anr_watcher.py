#!/usr/bin/env python3
"""
Android ANR / jank watcher — featuring ANRBuster session mode.

The Android-native counterpart of the iOS HangBuster system. The clustering /
session / formatting machinery is ported from ios-simulator-skill; only the
capture + parse layer is rewritten for ``adb logcat``.

Two surfaces live in this file:

1. **AnrWatcher** (legacy, --watch / --since) — passive logcat ANR/jank stream.
2. **AnrBuster** (new, --start / --stop / --get-details / --list-sessions /
   --clear-sessions / --diff) — agent-native session recorder. Detaches a
   worker, normalises and thresholds events on the fly, clusters at stop time,
   emits a token-tight summary with progressive drill paths.

The shared filter pipeline lives in ``common/anr_pipeline.py``; session
storage in ``common/anr_sessions.py``.

Android signals recognised (see ``common.anr_pipeline.parse_logcat_anr``):

- Choreographer ``Skipped N frames!`` jank (duration ~= N * 16.7ms).
- ActivityManager ``ANR in <pkg> (<component>)`` hard ANRs.
- StrictMode / "Slow operation" hints (minor).

Environment variables (all ``ANDROID_EMU_ANR_`` prefixed):

- ``ANDROID_EMU_ANR_MIN_FRAMES``         Min skipped frames kept (default 30)
- ``ANDROID_EMU_ANR_SESSION_TTL_HOURS``  Session prune age (default 24)
- ``ANDROID_EMU_ANR_TOTAL_CAP_MB``       Aggregate disk cap in MB (default 100; 0 = none)
- ``ANDROID_EMU_ANR_MAX_RESTARTS``       Worker stream re-spawns on EOF (default 3; 0 = none)
- ``ANDROID_EMU_ANR_DEFAULT_TOP_N``      Default top-N for --stop (default 3; 0 = all)
- ``ANDROID_EMU_ANR_BUDGET_TOKENS``      Default token budget for --stop
- ``ANDROID_EMU_ANR_HOME``               Override storage root (tests)
"""

import argparse
import contextlib
import json
import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

# Resolve imports whether run from repo root or scripts/ directory.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from common import cli, log_lifecycle, logcat  # noqa: E402
from common.adb_exec import (  # noqa: E402
    AdbCommandError,
    AdbError,
    AdbTimeoutError,
    DeviceError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    MultipleDevicesError,
    classify_device_error,
    run_adb,
    run_argv,
)
from common.anr_pipeline import (  # noqa: E402
    VERIFICATION_UNKNOWN,
    AnrRecordAssembler,
    attribution_markers,
    build_normalised_event,
    cluster_to_json,
    compress_to_budget,
    diff_sessions,
    event_to_jsonl,
    format_cluster_detail,
    format_diff,
    format_l0,
    format_l2,
    summary_to_json,
)
from common.anr_sessions import WORKER_TOKEN_FLAG, SessionStore  # noqa: E402
from common.device_utils import (  # noqa: E402
    get_package_uids,
    get_system_server_pid,
    parse_ps_uids,
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_int  # noqa: E402

# === CONSTANTS ===

# Skipped-frame events below this frame count are dropped before clustering.
DEFAULT_MIN_FRAMES = 30
# How many times the worker re-spawns ``adb logcat`` after an EOF / subprocess
# death before giving up and marking the session crashed. 0 is a real setting:
# no restarts, so a dead stream marks the session crashed at once.
DEFAULT_MAX_STREAM_RESTARTS = 3
# Backoff between restart attempts. Short — logcat usually recovers fast.
RESTART_BACKOFF_SECONDS = 2.0
# How long a reader waits for a record before treating the stream as quiet:
# a held WindowManager report's quiet is counted, and the events file is synced.
STREAM_POLL_SECONDS = 0.25
# On a stop, how long the worker keeps reading what the stream has already
# delivered before it closes the session.
STOP_DRAIN_SECONDS = 0.5
# Bound on a `--since` dump. It is one `logcat -d` read, but of the whole
# buffer, which on a busy device is megabytes.
SINCE_TIMEOUT_SECONDS = 60
# Grace for a `--watch` stream that hit end-of-file to report its exit status.
STREAM_EXIT_TIMEOUT_SECONDS = 5
# Bound on each question the worker puts to the device while it reads: which
# pid system_server is, and which uids a package has (see _ProcessNames).
# Short, because --stop waits for a lookup under way: a 10 s lookup outlasted
# the stop's wait and the summary was saved before the worker had written
# (review of PR #26).
LOOKUP_TIMEOUT_SECONDS = 3
# How long --stop waits for the worker to finish before saving the summary: a
# lookup already under way, the stop drain, and a margin to write and exit.
WORKER_EXIT_TIMEOUT_SECONDS = LOOKUP_TIMEOUT_SECONDS + STOP_DRAIN_SECONDS + 2.0
# Bound on `adb get-state`, asked before any logcat read starts (see
# _require_device). The adb server answers it from its own table; the margin
# is for a server that has to start first.
GET_STATE_TIMEOUT_SECONDS = 10

_LOGCAT_REMEDY = (
    "Check the device is connected (`adb devices`) and, with more than one attached, "
    "pass --serial."
)


def _compute_start_timestamp(duration_str: str) -> str:
    """Parse a duration string and return a ``logcat -t`` start timestamp.

    logcat's ``-t`` accepts ``MM-DD HH:MM:SS.mmm`` and prints lines at or after
    it, then exits (no live follow). Grammar and formatting are shared with
    every other logcat reader in the skill via ``common.logcat``.

    Raises:
        ValueError: If the format is unrecognised.
    """
    return logcat.window_start_for(duration_str)


def matches_package(event: dict, package: str) -> bool:
    """Whether a parsed event is the target package's, by what the reader established.

    Applied post-parse so ANR/jank events from system tags (ActivityManager,
    Choreographer) still flow through the pipeline; ``--package`` narrows the
    final output rather than the logcat filter. Matched whole, against only
    the identity the reader established: the package system_server's
    ActivityManager named (``anr_package``), the package of the component it
    named, and the name the session worker verified for a jank record's pid
    (``app``) -- a jank record names no package, so without it ``--package``
    dropped every jank event.

    It used to match by substring, against the message and the tag too, and
    by the package's last segment alone: the recorded Choreographer text "The
    application may be doing too much work" passed ``--package
    com.example.app`` and ``com.example.application`` alike (wave 4).
    """
    component = event.get("component") or ""
    owners = (event.get("anr_package"), event.get("app"), component.partition("/")[0])
    return package in owners


def _raise_logcat_failure(returncode: int, stderr: str, serial: str | None) -> NoReturn:
    """Report a logcat read that adb ended with a failure status (X6).

    adb's own words decide the type, exactly as ``run_adb`` would have typed
    them: a device that is not there, offline or unauthorized is a device error
    (exit 3 at the CLI boundary); anything else is a failure naming the command.

    Classified as adb wrote it, line by line. adb's own errors are recognised
    only at the start of a line, so flattening first let anything adb wrote
    before one -- its daemon starting, "- waiting for device -" -- push it off
    the start of the one line left: exit 1 where run_adb, reading the same
    text, says exit 3 (B7). Flattened only to be shown.
    """
    device_error = classify_device_error(stderr, serial)
    if device_error is not None:
        raise device_error
    detail = " ".join(stderr.split())
    said = f": {detail}" if detail else ", and said nothing on stderr."
    cli.fail(f"`adb logcat` exited {returncode}{said}", _LOGCAT_REMEDY)


def _require_device(serial: str | None) -> None:
    """Refuse before any logcat read starts when adb has no device to give it (B4).

    `adb logcat` does not fail when its device is not there: it writes "-
    waiting for device -" and waits (recorded: adb_waiting_for_device),
    with -d as well. With no --serial nothing was asked first, so `--watch
    --duration N` exited 0 having printed nothing -- a quiet device -- and
    `--start` started a worker that would wait forever. `adb get-state` asks
    the adb server the same question and is answered at once (recorded:
    adb_get_state, adb_device_not_found). It is answered for the device
    logcat will go to, $ANDROID_SERIAL and all.

    Raises:
        DeviceError: adb names no available device (exit 3), or several
            (MultipleDevicesError, exit 1).
        DeviceOfflineError: adb names the device in a state other than
            ``device`` -- logcat would wait for it too.
        AdbError: adb could not be run, or did not answer in time.
    """
    result = run_adb("get-state", serial, timeout=GET_STATE_TIMEOUT_SECONDS, check=True)
    state = result.stdout.strip()
    if state != "device":
        remedy = "Wait for the device to finish booting or reconnect it, then retry."
        raise DeviceOfflineError(
            f"adb reports the device as {state or 'nothing'!r}, not 'device', so a logcat "
            f"read would wait for it rather than start. {remedy}",
            remedy=remedy,
        )


# === STREAM READING ===


class _FdRecords:
    """logd's records (``logcat -B``) from a child's stdout, read straight off its descriptor.

    Records, not text: a tag is the app's to choose and logcat prints it
    verbatim, so in text one app's record could print a whole line headed
    with system_server's pid and tag (recorded: logcat_tag_injection) -- an
    ANR, a duplicate verdict, a ``Start proc``, all "system_server's". A
    record carries the pid logd took from the writer's socket, which no tag
    or message can forge. Decoding is ``common.logcat``'s (``parse_records``,
    W3-L): this class only finds where each record ends.

    N16: the worker select()ed the fd, then readline()d the file object, and
    readline() read ahead into a buffer select() cannot see -- lines parked
    there until the next burst, or lost at a stop. ``os.read`` takes exactly
    what the fd holds, so what select() calls readable is what is read, and
    nothing waits anywhere select() cannot see except a partial record, which
    is not yet a record. Both live readers use it -- the session worker and
    ``--watch`` -- because "the stream has gone quiet" is what lets a held
    WindowManager report out, and a blocking read can never say so.
    """

    _CHUNK = 65536
    _SIZES = struct.Struct("<HH")

    def __init__(self, stream) -> None:
        self._fd = stream.fileno()
        self._pending = b""
        # Bytes of the stream already handed over as records: where the
        # pending ones start, for an error to say where the stream broke.
        self._consumed = 0

    def read(self, timeout: float) -> list[logcat.LogRecord] | None:
        """The whole records that arrive within ``timeout`` seconds.

        Returns:
            The records; ``[]`` when no whole record arrived in time (the
            stream is quiet); ``None`` at a clean end of stream.

        Raises:
            logcat.LogFramingError: The bytes are not logd's framing, or the
                stream ended inside a record (``truncated``).
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            # Bounded in the source, so the stream guard can read it off: a
            # wait never longer than the poll interval.
            ready, _, _ = select.select([self._fd], [], [], min(remaining, STREAM_POLL_SECONDS))
            if not ready:
                return []
            chunk = os.read(self._fd, self._CHUNK)
            if not chunk:
                if self._pending:
                    # Its sizes were checked as they arrived, so what is left
                    # is the start of a record the stream never finished. The
                    # error is its account; it is no longer pending.
                    self._pending = b""
                    raise logcat.LogFramingError(
                        "the stream ended inside a record", offset=self._consumed, truncated=True
                    )
                return None
            self._pending += chunk
            records = self._whole_records()
            if records:
                return records

    @property
    def partial(self) -> bool:
        """Whether part of a record has arrived and the rest has not.

        A reader that stops an open stream never reads its end, so it asks
        this instead: what is pending then is a record the stop cut.
        """
        return bool(self._pending)

    def _whole_records(self) -> list[logcat.LogRecord]:
        """Decode and remove every whole record at the front of what has arrived."""
        end = 0
        while len(self._pending) - end >= self._SIZES.size:
            length, header = self._SIZES.unpack_from(self._pending, end)
            if not (
                logcat.MIN_ENTRY_HEADER <= header <= logcat.MAX_ENTRY_HEADER
                and length <= logcat.MAX_ENTRY_PAYLOAD
            ):
                # Not logd's framing: let its reader refuse it, in its words.
                logcat.parse_records(self._pending[end:])
            if len(self._pending) - end < header + length:
                break
            end += header + length
        whole, self._pending = self._pending[:end], self._pending[end:]
        self._consumed += end
        return logcat.parse_records(whole) if whole else []


class _ProcessNames:
    """pid -> process name, for the records that name no process: jank above all.

    A Choreographer record says only its pid, and a pid does not outlive a
    restart, so jank keyed by pid read as "1 resolved, 1 new, regression"
    across one (review of PR #26). The one record that names a process
    together with its pid is system_server's ``Start proc <pid>:<name>/<uid>``,
    and a pid is named ONLY from such a record seen earlier in the same
    stream. The device is never asked who holds a pid now: that answer (``ps
    ... ELAPSED``) was judged against when a line was READ, and a line
    buffered through a stall was read late, so a recycled pid's new owner was
    accepted (B2).

    The sentences are read by ``common.log_lifecycle``, the reading
    ``log_monitor --app`` uses too (B1 and PR #34's review), from logd's
    records: written by system_server -- the record's pid, logd's, against
    system_server's pid asked of the device once, when the first sentence
    arrives (unknown, none counts) -- the exact opening of the message, and,
    before a name is given, a process that is exactly a package, running
    under one of its uids (asked once per package, when a record first needs
    the name; unknown, the pid stays unnamed). A private process
    (``<package>:<name>``) is therefore never named. A name ends where
    system_server says the process did (``Killing``, ``has died``), and
    :meth:`reset` forgets every one when a stream (re)starts: a pid can change
    hands in the gap, and ``-T 1`` does not replay the new owner's ``Start
    proc``.

    So only a process STARTED while the session ran is ever named: one
    already running when it began -- an app warm in the background -- is
    keyed by pid, identity "pid", and dropped under ``--package``. The pids
    left unnamed are listed in the summary (``unnamed_pids``).
    """

    def __init__(
        self,
        system_server_pid: Callable[[], str | None] = lambda: None,
        package_uids: Callable[[str], Iterable[int] | None] = lambda _package: None,
        pid_uid: Callable[[int], int | None] = lambda _pid: None,
    ) -> None:
        """Build a resolver over the device's answers to three questions.

        Args:
            system_server_pid: Asks the device for system_server's pid; None
                when it cannot say. The default knows nothing, so nothing is
                named.
            package_uids: Asks the device for a package's uids; None when it
                cannot say. Asked at most once per package for the session.
            pid_uid: Asks the device which uid a live process runs under
                (``ps -o PID,UID -p``); None when it cannot say, or the pid
                is gone. Asked at most once per pid for a stream, and only
                for an ANR report's pid that this stream gave no owner.
        """
        self._ask_system_server = system_server_pid
        self._answers: dict[str, Iterable[int] | None] = {}

        def known_uids(package: str) -> Iterable[int] | None:
            if package not in self._answers:
                self._answers[package] = package_uids(package)
            return self._answers[package]

        self._package_uids = known_uids
        self._ask_pid_uid = pid_uid
        self._pid_uids: dict[int, int | None] = {}
        self._reader = log_lifecycle.LifecycleReader(system_server_pid, known_uids)
        self._owners: dict[int, log_lifecycle.Sentence] = {}
        self._unnamed: set[int] = set()

    @property
    def unnamed_pids(self) -> list[int]:
        """The pids that had at least one record no owner could be found for."""
        return sorted(self._unnamed)

    def system_server_pid(self) -> str | None:
        """system_server's pid as this stream asked it (once): for the ANR assembler too."""
        return self._reader.system_server()

    def reset(self) -> None:
        """A new stream: forget every owner, and who system_server was."""
        self._owners.clear()
        self._pid_uids.clear()
        self._reader = log_lifecycle.LifecycleReader(self._ask_system_server, self._package_uids)

    def owns(self, pid: int, package: str) -> bool | None:
        """Whether process ``pid`` is ``package``'s: its uid one of the package's.

        For an ANR report, whose ``ANR in <name>`` names a process -- which any
        app may name after another package (``android:process``; recorded:
        logcat_anr_foreign_process_records). The uid comes from this
        stream's own evidence first: system_server's ``Start proc
        <pid>:<name>/<uid>`` for the pid, earlier in the stream and not ended
        since. Only without it is the device asked who runs as ``pid`` NOW
        (``ps``) -- which shows a live process's uid but cannot prove who
        held the pid when the record was written: a pid freed and reused
        since reads as its new owner. That is the cost of naming an ANR whose
        process started before the read; one already gone stays unattributed.

        Returns:
            True when the uid is one of the package's, False when it is
            shown not to be, None when nothing shows either.
        """
        owner = self._owners.get(pid)
        uid = owner.uid if owner is not None else self._live_uid(pid)
        if uid is None:
            return None
        uids = self._package_uids(package)
        if uids is None:
            # The device names no such package -- not installed, or it could
            # not say: nothing shows the process is that package's.
            return None
        return uid in log_lifecycle.uid_set(uids)

    def _live_uid(self, pid: int) -> int | None:
        if pid not in self._pid_uids:
            self._pid_uids[pid] = self._ask_pid_uid(pid)
        return self._pid_uids[pid]

    def observe(self, record: logcat.LogRecord) -> None:
        """Learn -- or forget -- a pid's owner from system_server's sentence about it."""
        _writer, sentence = self._reader.read(record)
        if sentence is None:
            return
        if sentence.what == log_lifecycle.STARTED:
            self._owners[int(sentence.pid)] = sentence
        else:
            self._owners.pop(int(sentence.pid), None)

    def name_of(self, pid: int) -> str | None:
        """The name of the process holding ``pid``, as this stream has shown it."""
        owner = self._owners.get(pid)
        if owner is not None and self._reader.is_package(owner):
            return owner.name
        self._unnamed.add(pid)
        return None


def _device_lookups(serial: str | None, may_ask: Callable[[], bool]) -> tuple[
    Callable[[], str | None],
    Callable[[str], frozenset[int] | None],
    Callable[[int], int | None],
]:
    """The three questions a reader puts to the device, bounded and failing closed.

    system_server's pid (for :class:`_ProcessNames` and for authenticating ANR
    reports), a package's uids, and the uid a live pid runs under (``ps -o
    PID,UID -p``, the recorded ps_pid_uid form: for an ANR report's process,
    :meth:`_ProcessNames.owns`). Each is bounded by ``LOOKUP_TIMEOUT_SECONDS``
    and asked only while ``may_ask()`` holds -- the worker stops asking once a
    stop is requested. A failure is an unknown answer, not an error: in the
    detached worker there is no caller to report to, and a session must not end
    because a process could not be NAMED. The cost is disclosed downstream:
    unnamed pids, unauthenticated ANR reports and unattributed ANRs are listed
    in the summary.
    """

    def system_server_pid() -> str | None:
        if not may_ask():
            return None
        try:
            return get_system_server_pid(serial, timeout=LOOKUP_TIMEOUT_SECONDS)
        except AdbError:
            return None

    def package_uids(package: str) -> frozenset[int] | None:
        if not may_ask():
            return None
        try:
            return get_package_uids(serial, package, timeout=LOOKUP_TIMEOUT_SECONDS)
        except AdbError:
            return None

    def pid_uid(pid: int) -> int | None:
        if not may_ask():
            return None
        try:
            result = run_adb(
                "shell",
                serial,
                "ps",
                "-o",
                "PID,UID",
                "-p",
                quote_for_device_shell(str(pid)),
                timeout=LOOKUP_TIMEOUT_SECONDS,
            )
        except AdbError:
            return None
        return parse_ps_uids(result.stdout).get(str(pid))

    return system_server_pid, package_uids, pid_uid


class _CaptureBoundary:
    """What a session has consumed, so a replayed record is never counted as its own (B5).

    A live stream starts with ``logcat -T 1``: the newest record already
    buffered, then what follows (``-B -T 1`` composes, measured by W3-L).
    ``-T 0`` would start with nothing, but logcat refuses it ("-T 0 invalid,
    setting to 1", recorded: logcat_tail_zero). So:

    - the first record the session reads is that replay -- the first
      stream's, or a restart's when the session had read nothing to hold it
      against -- and may predate the session. Nothing in a record can show
      it does not: its time is its writer's, and after ``logcat -c`` the
      first record is a live one. It was counted as the session's incident
      (wave 4), where setting it aside unsaid had lost a recorded ANR outright
      (review of PR #34). So it is :data:`AMBIGUOUS`: read, shown as
      ``ambiguous_tail`` in the summary, never counted, clustered or diffed;
    - a RESTARTED stream's leading records that this session already consumed
      are its replay, and are skipped -- a record is compared whole, writer,
      time and text. One it never consumed was logged while no stream was up,
      after the session's first record, and is read. Nothing is compared by
      timestamp alone, so no clock -- a year turning over, a clock stepped
      back -- can move the line.
    """

    # How many of the latest consumed records a restart's replay is held
    # against. The replay is one record, the newest consumed.
    REMEMBERED = 256

    # What admit() says of a record.
    READ = "read"
    AMBIGUOUS = "ambiguous"
    REPLAYED = "replayed"

    def __init__(self) -> None:
        self._consumed: deque[logcat.LogRecord] = deque(maxlen=self.REMEMBERED)
        self._skipping = False
        self._streams = 0

    def new_stream(self) -> None:
        """A stream (re)starts: on a restart, its leading replay is skipped."""
        self._streams += 1
        self._skipping = self._streams > 1

    def admit(self, record: logcat.LogRecord) -> str:
        """``READ``, ``AMBIGUOUS`` (it may predate the session) or ``REPLAYED`` (read already)."""
        if self._skipping:
            if record in self._consumed:
                return self.REPLAYED
            self._skipping = False
        first = not self._consumed
        self._consumed.append(record)
        return self.AMBIGUOUS if first else self.READ


# === LEGACY WATCHER ===


class AnrWatcher:
    """Watch for Android ANR/jank events via ``adb logcat``."""

    def __init__(self, serial: str | None = None):
        """Initialize ANR watcher.

        Args:
            serial: Device serial. Auto-detects default device if None.
        """
        self.serial = serial
        self.events: list[dict] = []
        # ANRs --package dropped for naming no package, to be said (wave 4).
        self.unattributed_anrs = 0
        # What the stream's first record showed: `-T 1`'s replay, which may
        # predate the watch -- printed, marked, and out of every total.
        self.ambiguous: list[dict] = []
        self.interrupted = False
        self._process: subprocess.Popen | None = None
        # One per watcher: whether a WindowManager report is an ANR of its own
        # depends on the records around it (D5). It authenticates ANR reports
        # against system_server's pid, asked of the device once, when the
        # first report arrives, and files one under its package only when
        # its process is shown to be that package's: by system_server's
        # `Start proc` for the pid earlier in what is read, or else by the
        # uid `ps` gives the pid now (_ProcessNames.owns).
        # Asked only until the watch is stopped, as the session worker asks:
        # after Ctrl-C a lookup under way finishes, and no other starts.
        self._names = _ProcessNames(*_device_lookups(serial, may_ask=lambda: not self.interrupted))
        self._assembler = AnrRecordAssembler(
            self._names.system_server_pid, subject_owner=self._names.owns
        )

    def build_logcat_command(self, since: str | None = None) -> list[str]:
        """Build the ``adb logcat -B`` command (pure arg→command map).

        A live stream starts at the END of the device's buffer: ``-T 1``.
        Without it `adb logcat` first replays the whole ring buffer, and every
        line of it reads as live. Measured on emulator-5554: a session started
        after a run of provoked ANRs reported every one of them, and every jank
        line still buffered, as happening in its first tenth of a second -- 33
        events for one ANR. ``-T 1`` is logcat's own "from now": it prints the
        single most recent record, then follows (measured; ``-T 0`` is refused
        and treated as 1, recorded: logcat_tail_zero). That one record may
        predate the read: it is shown, marked, and never counted
        (``_CaptureBoundary``).

        Records, not text (``-B``, ``common.logcat``): logcat prints a tag
        verbatim, so in text an app's record could print a line headed with
        system_server's pid -- a forged ANR, duplicate verdict or ``Start
        proc`` (recorded: logcat_tag_injection). A record carries the pid logd
        took from the writing socket. ``-B -T 1`` composes (measured by W3-L).

        Args:
            since: If set, a ``MM-DD HH:MM:SS.mmm`` timestamp for a historical
                dump-and-exit window (``-d -t``) instead of a live stream.

        Returns:
            Complete adb command list ready for subprocess.
        """
        if since is not None:
            return logcat.build_logcat_command(self.serial, since=since, binary=True)
        return logcat.build_logcat_command(self.serial, binary=True, tail=1)

    def watch(
        self,
        duration_seconds: int | None = None,
        package: str | None = None,
        json_mode: bool = False,
    ) -> bool:
        """Stream ANR/jank events live from the device.

        The stream's first record is `logcat -T 1`'s replay of the newest one
        already buffered, which may predate the watch: its events are printed
        with ``"ambiguous_tail": true`` (see ``_CaptureBoundary``).

        Raises:
            DeviceError: adb ended the stream because the device is not
                available, or the stream ended by itself and `adb get-state`
                then said so (exit 3 at the CLI boundary).
            cli.Failure: The stream ended by itself with the device still
                there: adb's failure status, or none -- a live read ends only
                when something ends it, so it is never a quiet success.
            logcat.LogFramingError: The stream was not logd's records, or
                ended inside one by itself while the device is still there.
                Cut by our own stop, the lost record is said on stderr instead.
        """
        cmd = self.build_logcat_command()
        if not json_mode:
            print("Watching for ANR/jank events", file=sys.stderr)
            if package:
                print(f"Post-parse filter: {package}", file=sys.stderr)

        self._register_signal_handler()
        stopped_by_us = False
        # adb's stderr goes to a file rather than a pipe: nothing reads it until
        # the stream has ended, and a pipe nobody reads blocks adb once it
        # fills. It is the only account of WHY a stream ended (X6).
        with tempfile.TemporaryFile() as adb_stderr:
            try:
                # Bytes, unbuffered, read through _FdRecords: the same reader
                # as the session worker. A blocking read could not notice the
                # stream going quiet, so a held report sat unprinted until the
                # next record or the end of the stream (review of PR #26); and
                # select() on a buffered pipe loses what it buffers (N16).
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=adb_stderr,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                )
                stream = _FdRecords(self._process.stdout)
                # Polling with a timeout also makes --duration exact on a quiet
                # device, which blocked in readline() before (A2).
                deadline = time.monotonic() + duration_seconds if duration_seconds else None
                last_record_at = time.monotonic()
                cut: logcat.LogFramingError | None = None
                first = True
                try:
                    while not self.interrupted:
                        if deadline is not None and time.monotonic() >= deadline:
                            stopped_by_us = True
                            break
                        try:
                            records = stream.read(timeout=STREAM_POLL_SECONDS)
                        except logcat.LogFramingError as error:
                            if not error.truncated:
                                # Not logd's records at all: refused.
                                raise
                            cut = error
                            break
                        if records is None:
                            break
                        if not records:
                            self._flush(package, json_mode, time.monotonic() - last_record_at)
                            continue
                        last_record_at = time.monotonic()
                        for record in records:
                            # `-T 1`'s replay: it may predate the watch.
                            self._handle_record(record, package, json_mode, ambiguous=first)
                            first = False
                finally:
                    # Whatever is still held when the stream ends -- or breaks
                    # -- was seen, and goes out before anything else is said.
                    self._close(package, json_mode)
                    self._disclose_unverified()
                    self._disclose_unattributed()

                # A deadline and Ctrl-C end the stream themselves, so how adb
                # exited then says nothing -- and a record cut by them is lost,
                # and said (common.logcat's rule for a truncated record).
                if stopped_by_us or self.interrupted:
                    # Cut at the stream's end, or still arriving when we stopped
                    # an open stream -- whose end we never read.
                    if cut is not None or stream.partial:
                        print(
                            "warning: the watch was stopped while a record was arriving; "
                            "that record is not shown.",
                            file=sys.stderr,
                        )
                    return True
                returncode, stderr = self._stream_exit(adb_stderr)
                if cut is not None:
                    # Ended inside a record by itself: if the device went, that
                    # is the answer (exit 3); otherwise the read is refused for
                    # its framing, with how adb exited added -- asked before the
                    # exit status is judged, which would otherwise answer first
                    # and skip both (Codex review of PR #34).
                    _require_device(self.serial)
                    raise _with_adb_exit(cut, returncode, stderr)
                self._refuse_unexpected_end(returncode, stderr)
            # No `except Exception` here any more: it printed and returned False,
            # so a missing adb or a vanished device exited 1 with no remedy and,
            # under --json, no refusal object after the records. Failures now
            # reach common.cli, whose refusal is the last line of stdout.
            finally:
                if self._process and self._process.poll() is None:
                    self._process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self._process.wait(timeout=2)
                if self._process and self._process.stdout:
                    self._process.stdout.close()

    def _stream_exit(self, adb_stderr) -> tuple[int | None, str]:
        """``(adb's exit status, its stderr)`` once the stream has ended on its own.

        The status is None when there is none to judge: no process, or its
        stdout closed but it has not exited (the finally in watch() ends it).
        """
        process = self._process
        if process is None:
            return None, ""
        try:
            returncode = process.wait(timeout=STREAM_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return None, ""
        adb_stderr.seek(0)
        return returncode, adb_stderr.read().decode("utf-8", errors="replace")

    def _refuse_unexpected_end(self, returncode: int | None, stderr: str) -> NoReturn:
        """Refuse a live stream that ended on its own -- however adb exited.

        `logcat -T 1` follows until it is stopped, so a stream that ends by
        itself ended because something went away. The read used to return True
        unless adb's status was a failure (X6), so adb exiting 0 -- or giving
        no status in time -- after the device went read as a watch that saw no
        ANRs: exit 0, empty stdout, the device asked only before it began
        (wave 4). adb's own words are typed first, as run_adb types them; then
        the device is asked again, bounded (gone: exit 3); and otherwise the
        watch is refused, saying how adb exited (exit 1).
        """
        if returncode:
            device_error = classify_device_error(stderr, self.serial)
            if device_error is not None:
                raise device_error
        _require_device(self.serial)
        if returncode:
            _raise_logcat_failure(returncode, stderr, self.serial)
        status = (
            "exit status 0"
            if returncode == 0
            else f"no exit status within {STREAM_EXIT_TIMEOUT_SECONDS}s"
        )
        cli.fail(
            f"`adb logcat` ended the live stream by itself ({status}) while the device is "
            f"still attached; a live read ends only when it is stopped, so what it printed "
            f"is not the whole watch.",
            "Retry; if it repeats, restart adb (`adb kill-server && adb start-server`).",
        )

    def show_since(
        self,
        since_duration: str,
        package: str | None = None,
        json_mode: bool = False,
    ) -> bool:
        """Show historical ANR/jank events using ``logcat -d -t``.

        Raises:
            DeviceError: adb could not reach the device (exit 3 at the CLI
                boundary).
            AdbCommandError: logcat exited with any other failure status.
            AdbTimeoutError: the dump took longer than ``SINCE_TIMEOUT_SECONDS``
                while the device is still there (asked again first: gone is
                the DeviceError above).
            logcat.LogFramingError: The dump is not whole records -- a dump
                never ends inside one, so a cut dump is incomplete: the device
                is asked again (gone: exit 3), and otherwise the read refused.
        """
        since = _compute_start_timestamp(since_duration)
        if not json_mode:
            print(f"Showing ANR/jank since {since}", file=sys.stderr)
        # logd's records, as bytes, through run_argv: bounded, adb's failures
        # typed. A failed read is a failure, where it was an empty dump read as
        # "no ANRs" at exit 0 (X6).
        command = self.build_logcat_command(since=since)
        try:
            completed = run_argv(command, serial=self.serial, timeout=SINCE_TIMEOUT_SECONDS)
        except AdbTimeoutError:
            # A dump whose device went does not fail: `adb logcat -d` prints
            # "- waiting for device -" and waits (recorded:
            # adb_waiting_for_device), so the bound is how it ends. The device
            # is asked, bounded, before the timeout is the answer: gone is
            # exit 3, not "retry" (wave 4, as log_monitor's reads).
            _require_device(self.serial)
            raise
        detail = (completed.stderr or b"").decode("utf-8", errors="replace")
        try:
            records = logcat.parse_records(completed.stdout or b"")
        except logcat.LogFramingError as error:
            # Before the exit status, as --watch: a dump cut short asks whether
            # the device went (exit 3), and is otherwise refused for its
            # framing, with how adb exited added.
            if error.truncated:
                _require_device(self.serial)
            raise _with_adb_exit(error, completed.returncode, detail) from None
        if completed.returncode != 0:
            raise AdbCommandError(
                f"`{' '.join(command)}` exited {completed.returncode}: "
                f"{detail.strip() or 'no output'}",
                command=command,
            )
        for record in records:
            self._handle_record(record, package, json_mode)
        self._close(package, json_mode)
        self._disclose_unverified()
        self._disclose_unattributed()
        return True

    def get_summary(self) -> str:
        """Return token-efficient summary of captured events."""
        total = len(self.events)
        if total == 0:
            dropped = (
                f" --package dropped {self.unattributed_anrs} ANR(s) naming no verified "
                f"package."
                if self.unattributed_anrs
                else ""
            )
            if self._assembler.unverified:
                return f"No verified ANR/jank events. {self._unverified_sentence()}{dropped}"
            return f"No ANR/jank events detected.{dropped}"
        tags: dict[str, int] = {}
        for event in self.events:
            tag = event.get("process", "unknown")
            tags[tag] = tags.get(tag, 0) + 1
        top = sorted(tags.items(), key=lambda x: x[1], reverse=True)[:5]
        top_str = ", ".join(f"{p}({c})" for p, c in top)
        # What the count includes that is not one verified incident each, so
        # the total cannot be read as N of the app's own ANRs.
        duplicates = sum(1 for event in self.events if event.get("possible_duplicate"))
        unverified = sum(1 for event in self.events if _unattributed(event))
        qualifiers = [
            (
                f"{duplicates} possible duplicate{'s' if duplicates != 1 else ''}"
                if duplicates
                else ""
            ),
            f"{unverified} unverified" if unverified else "",
        ]
        including = "; ".join(q for q in qualifiers if q)
        including = f", including {including}" if including else ""
        return f"ANR/jank events: {total}{including} | Tags: {top_str}"

    def get_json_output(self) -> dict:
        """Return full results as a JSON-serialisable dict."""
        return {
            "events": self.events,
            "ambiguous_tail": self.ambiguous,
            "summary": {
                "total": len(self.events),
                "tags": list({e.get("process") for e in self.events}),
            },
        }

    # === PRIVATE ===

    def _unverified_sentence(self) -> str:
        count = self._assembler.unverified
        plural = "s" if count != 1 else ""
        return (
            f"Unverified: {VERIFICATION_UNKNOWN} ({count} report record{plural} set aside, "
            f"not counted)."
        )

    def _disclose_unverified(self) -> None:
        """Say so when ANR reports were set aside unauthenticated: never a silent "no ANRs"."""
        if self._assembler.unverified:
            print(f"warning: {self._unverified_sentence()}", file=sys.stderr)

    def _disclose_unattributed(self) -> None:
        """Say so when --package dropped ANRs naming no verified package: never unsaid."""
        if self.unattributed_anrs:
            plural = "s" if self.unattributed_anrs != 1 else ""
            print(
                f"warning: --package dropped {self.unattributed_anrs} ANR{plural} naming no "
                f"verified package (a window's title is the app's to set, and a process may be "
                f"named after any package). Run without --package to see "
                f"{'them' if plural else 'it'}.",
                file=sys.stderr,
            )

    def _handle_record(
        self,
        record: logcat.LogRecord,
        package: str | None,
        json_mode: bool,
        *,
        ambiguous: bool = False,
    ) -> None:
        # system_server's word on who a pid is, before any report about it.
        self._names.observe(record)
        for event in self._assembler.feed(record, ambiguous=ambiguous):
            self._emit(event, package, json_mode)

    def _flush(self, package: str | None, json_mode: bool, quiet_seconds: float) -> None:
        """The stream is quiet: emit what quiet completes (a report nothing answered)."""
        for event in self._assembler.flush(quiet_seconds):
            self._emit(event, package, json_mode)

    def _close(self, package: str | None, json_mode: bool) -> None:
        """The input has ended: emit everything the assembler still holds."""
        for event in self._assembler.close():
            self._emit(event, package, json_mode)

    def _emit(self, event: dict, package: str | None, json_mode: bool) -> None:
        if package and not matches_package(event, package):
            if _unattributed(event):
                self.unattributed_anrs += 1
            return
        # Printed, marked; never in a total (_CaptureBoundary).
        (self.ambiguous if event.get("ambiguous_tail") else self.events).append(event)
        if json_mode:
            print(json.dumps(event))
            sys.stdout.flush()
        else:
            print(self._format_event(event))

    def _format_event(self, event: dict) -> str:
        ms = event.get("duration_ms", 0)
        dur = f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
        frames = event.get("frames")
        frame_str = f" {frames}frames" if frames is not None else ""
        reason = event.get("reason")
        reason_str = f" | Reason: {reason[:120]}" if reason else ""
        tail = (
            " [may predate the watch: logcat -T 1's replay]" if event.get("ambiguous_tail") else ""
        )
        # What JSON carries as fields, said on the line itself: an "ANR in X"
        # line read alone reads as X's (Codex review of PR #39).
        marks = attribution_markers(
            attribution=event.get("attribution"),
            process_name=event.get("process_name"),
            window=event.get("window"),
            possible_duplicate=bool(event.get("possible_duplicate")),
            possible_duplicate_of=event.get("possible_duplicate_of"),
        )
        marks = f" {marks}" if marks else ""
        return (
            f"{event['kind'].upper()} {event['timestamp']} | {event['process']} "
            f"(PID {event['pid']}) [{dur}{frame_str}] | {event['message'][:120]}{reason_str}"
            f"{marks}{tail}"
        )

    def _register_signal_handler(self) -> None:
        """Stop the stream on Ctrl-C, when we are in a position to ask for it.

        ``signal.signal`` raises ValueError outside the main thread, so calling
        ``watch()`` from any worker thread previously died here before the
        stream even started. Ctrl-C handling is a convenience for interactive
        use, not a correctness requirement -- the duration watchdog stops the
        stream either way -- so a thread that cannot register simply goes
        without it.
        """

        def handle_sigint(_sig, _frame):
            self.interrupted = True
            if self._process:
                self._process.terminate()

        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, handle_sigint)


# === ANRBUSTER (session mode) ===


class SessionError(RuntimeError):
    """A stored ANR session could not be read the way it was asked for.

    Unknown session, no summary yet, a cluster index past the end. All three
    used to be *returned as ordinary strings*, which ``main`` printed on stdout
    before exiting 0 (X5) -- so `--get-details $SID --json` answered
    ``Unknown session: ...`` where the caller was parsing JSON, and the exit
    status said it had worked.
    """


class AnrBuster:
    """Session-mode façade.

    Methods route to ``SessionStore`` + filter pipeline. The worker subprocess
    re-enters this class via ``run_worker()``.
    """

    def __init__(self, store: SessionStore | None = None):
        self.store = store or SessionStore()

    # === PUBLIC API ===

    def start(self, args: dict, serial: str | None) -> str:
        """Create session, detach worker, return session_id once worker registers."""
        self.store.prune_expired()
        # 0 disables the cap -- and env_int's default floor of 1 turned that
        # into a 1 MB cap that pruned sessions (the N14 shape).
        aggregate_cap_mb = env_int("ANDROID_EMU_ANR_TOTAL_CAP_MB", 100, min_value=0)
        if aggregate_cap_mb > 0:
            self.store.prune_to_aggregate_cap(aggregate_cap_mb * 1024 * 1024)
        resolved_serial = self._resolve_serial(serial)
        # Before the session exists: a worker started without a device waits
        # in `adb logcat` forever, and the id it was given names nothing (B4).
        _require_device(resolved_serial)
        meta = self.store.create({**args, "serial": resolved_serial})
        # The token on the worker's command line is how --stop knows the
        # process at the recorded pid is still this worker (anr_sessions).
        cmd = [
            sys.executable,
            __file__,
            "--worker-session-id",
            meta.session_id,
            WORKER_TOKEN_FLAG,
            meta.worker_token,
        ]
        # The detached worker survives parent exit. ``start_new_session=True``
        # calls setsid() so the process group is independent of the TTY.
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            self.store.wait_for_worker(meta.session_id, timeout_seconds=3.0)
        except TimeoutError:
            self.store.mark_crashed(meta.session_id)
            raise RuntimeError(f"Worker did not register within 3s for {meta.session_id}") from None
        return meta.session_id

    def stop(
        self,
        session_id: str,
        budget_tokens: int | None = None,
        top_n: int | None = None,
        terse: bool = False,
        json_mode: bool = False,
    ) -> str:
        """Signal worker, wait for it to finish, build summary, return formatted output.

        The summary is built only once the worker has exited, so it holds what
        the worker wrote on its way out -- a held WindowManager report, the
        record it was naming. If the worker outlives ``WORKER_EXIT_TIMEOUT_SECONDS`` the
        summary is still saved, marked ``incomplete``, and stderr says so: it
        used to be saved after a fixed 2 s with zero events while the worker
        was mid-lookup, and nothing said it was short (review of PR #26). It
        is marked so too when the worker crashed or vanished, the stream
        broke, a record was cut by the stop, the stream was restarted, or ANR
        reports could not be authenticated (``incomplete_reason``).

        The worker is signalled only once the process at its recorded pid is
        shown to still be it (``SessionStore.signal_worker``); otherwise stderr
        says "worker gone" and nothing is signalled.
        """
        status = self.store.load_meta(session_id).status
        delivered = self.store.signal_worker(session_id, signal.SIGTERM)
        finished = True
        if delivered:
            finished = self._wait_for_worker_exit(
                session_id, timeout_seconds=WORKER_EXIT_TIMEOUT_SECONDS
            )
        meta = self.store.load_meta(session_id)
        line_counters = meta.extras.get("line_counters", {})
        # Not signalled: the pid is no longer the worker's (anr_sessions).
        gone = None if delivered else meta.extras.pop("worker_gone", None)
        if gone:
            print(
                f"warning: worker gone for {session_id}: {gone}; nothing was signalled.",
                file=sys.stderr,
            )

        summary = self.store.build_summary(
            session_id,
            matched_lines=line_counters.get("matched", 0),
            total_lines=line_counters.get("total", 0),
            dropped_below_threshold=line_counters.get("dropped", 0),
        )
        # What the worker could not see, carried into the summary itself: ANR
        # reports it could not authenticate, pids it could not name, records a
        # stop cut, restarts' gaps, ANRs --package could not attribute, and the
        # record the session started on, which may predate it.
        unverified = int(line_counters.get("unverified_anr_lines", 0))
        unnamed = line_counters.get("unnamed_pids", [])
        failure = line_counters.get("stream_failure")
        cut = int(line_counters.get("cut_records", 0))
        restarts = int(line_counters.get("stream_restarts", 0))
        tail = line_counters.get("ambiguous_tail", [])
        summary = replace(
            summary,
            verification=VERIFICATION_UNKNOWN if unverified else None,
            unverified_anr_lines=unverified,
            unnamed_pids=sorted(unnamed) if isinstance(unnamed, list) else [],
            cut_records=cut,
            stream_failure=failure.get("error") if isinstance(failure, dict) else None,
            stream_restarts=restarts,
            ambiguous_tail=tail if isinstance(tail, list) else [],
            unattributed_anrs=int(line_counters.get("unattributed_anrs", 0)),
        )
        if not finished:
            print(
                f"warning: the worker for {session_id} was still running "
                f"{WORKER_EXIT_TIMEOUT_SECONDS:.1f}s after it was asked to stop; this "
                f"summary is marked incomplete and may miss what it wrote after that.",
                file=sys.stderr,
            )
        # Every way this capture can be missing events, so --diff concludes
        # nothing from what is absent (Codex review of PR #34: a broken or
        # unauthenticated capture was compared as complete -- "improvement:
        # 1 resolved"). A record cut by the stop and a restarted stream's gap
        # are losses too, and were saved complete (wave 4): `-T 1` replays
        # only the newest record logged while no stream was up.
        cut_plural = cut != 1
        reasons = [
            reason
            for reason, applies in (
                (
                    "saved before the session's worker finished, so events it wrote later "
                    "are missing",
                    not finished,
                ),
                (
                    f"the worker ended without being stopped ({gone}), so what it saw last "
                    f"is missing",
                    bool(gone) and status == "running",
                ),
                (
                    "the worker gave up on the stream before --stop (crashed), so nothing "
                    "after that is in here",
                    status == "crashed" and not isinstance(failure, dict),
                ),
                ("the stream broke", isinstance(failure, dict)),
                (
                    f"{cut} record{'s' if cut_plural else ''} arriving when it stopped "
                    f"{'were' if cut_plural else 'was'} cut off",
                    bool(cut),
                ),
                (
                    f"the stream restarted {restarts} time{'s' if restarts != 1 else ''}, and a "
                    f"restarted stream replays only the newest record logged while it was down",
                    bool(restarts),
                ),
                (VERIFICATION_UNKNOWN, bool(unverified)),
            )
            if applies
        ]
        if reasons:
            summary = replace(summary, incomplete=True, incomplete_reason="; ".join(reasons))
        # Persist the complete summary BEFORE any capping. Top-N is a display
        # concern; truncating first destroyed every cluster past N on disk, so
        # `--get-details --cluster 4` could never resolve and `--diff` compared
        # two truncated sets, reporting clusters as new or resolved purely
        # because they fell outside the cap.
        self.store.stop(session_id, summary)
        if finished:
            # It has exited: its pid is the OS's to reuse, and no later --stop
            # may signal it.
            self.store.forget_worker(session_id)
        if isinstance(failure, dict):
            _refuse_broken_stream(session_id, failure)

        # `top_n or default` treated both None (no flag) and 0 (--all, "no cap")
        # as "unset", so --all returned exactly the default it was meant to lift.
        # min_value=0: 0 is "no cap" here as it is for --all (the N14 shape).
        effective_top_n = (
            env_int("ANDROID_EMU_ANR_DEFAULT_TOP_N", 3, min_value=0) if top_n is None else top_n
        )

        view = summary
        if effective_top_n > 0:
            view = replace(summary, clusters=summary.clusters[:effective_top_n])

        if json_mode:
            return json.dumps(summary_to_json(view), indent=2)
        if terse:
            return format_l0(view)
        # min_value=0: 0 is "no budget", the default; floored to 1 it forced
        # the one-line L0 output (the N14 shape).
        budget = budget_tokens or env_int("ANDROID_EMU_ANR_BUDGET_TOKENS", 0, min_value=0) or None
        return compress_to_budget(
            view,
            max_tokens=budget,
            default_top_n=effective_top_n if effective_top_n > 0 else len(view.clusters),
        )

    def get_details(
        self,
        session_id: str,
        cluster: int | None = None,
        raw: bool = False,
        json_mode: bool = False,
    ) -> str:
        """Drill into a stored session. ``cluster`` is 1-indexed for human use.

        Raises:
            SessionError: The session is unknown, has no summary yet, or the
                requested cluster index does not exist. Each message names what
                to run instead.
            ValueError: ``session_id`` is not a well-formed session id. Raised
                by the store, which owns that grammar; the CLI rejects it as a
                usage error before reaching here.
        """
        try:
            self.store.load_meta(session_id)
        except FileNotFoundError as error:
            raise SessionError(
                f"Unknown session: {session_id}. Run --list-sessions to see the "
                f"sessions this host has stored."
            ) from error
        summary = self.store.load_summary(session_id)
        if summary is None:
            raise SessionError(
                f"No summary for {session_id}. Run --stop {session_id} first; a "
                f"session has no summary until it is stopped."
            )
        if raw:
            return self._dump_raw_events(session_id)
        if cluster is not None:
            index = cluster - 1
            if index < 0 or index >= len(summary.clusters):
                raise SessionError(
                    f"Cluster {cluster} out of range: this session has "
                    f"{len(summary.clusters)} cluster(s). Pass --cluster between "
                    f"1 and {len(summary.clusters)}, or omit it for the summary."
                )
            target = summary.clusters[index]
            events = [
                e for e in self.store.read_events(session_id) if e.fingerprint == target.fingerprint
            ]
            # The drill says what the summary says: a cluster of an incomplete
            # session may be missing events (B6).
            if json_mode:
                return json.dumps(
                    {
                        **cluster_to_json(target),
                        "incomplete": summary.incomplete,
                        "incomplete_reason": summary.incomplete_reason,
                    },
                    indent=2,
                )
            return format_cluster_detail(
                target,
                events,
                incomplete=summary.incomplete,
                incomplete_reason=summary.incomplete_reason,
            )
        if json_mode:
            return json.dumps(summary_to_json(summary), indent=2)
        return format_l2(summary)

    def list_sessions(self, json_mode: bool = False) -> str:
        metas = self.store.list_sessions()
        if json_mode:
            return json.dumps([m.to_json() for m in metas], indent=2)
        if not metas:
            return "No sessions stored."
        lines = [f"Sessions: {len(metas)}"]
        for meta in metas[:20]:
            stopped = meta.stopped_at or "-"
            counters = meta.extras.get("line_counters", {})
            restarts = counters.get("stream_restarts", 0)
            duration_s = (
                (meta.stopped_at_ms - meta.started_at_ms) / 1000.0 if meta.stopped_at_ms else None
            )
            duration_str = f"  capture={duration_s:.1f}s" if duration_s is not None else ""
            restart_str = f"  restarts={restarts}" if restarts else ""
            lines.append(
                f"  {meta.session_id}  {meta.status:8s}  started={meta.started_at}  "
                f"stopped={stopped}{duration_str}{restart_str}"
            )
        if len(metas) > 20:
            lines.append(f"  ... {len(metas) - 20} more")
        return "\n".join(lines)

    def clear_sessions(self, older_than: str | None = None, json_mode: bool = False) -> str:
        deleted = self.store.clear(older_than=older_than)
        if json_mode:
            return json.dumps({"deleted": deleted, "older_than": older_than})
        suffix = f" older than {older_than}" if older_than else ""
        return f"Cleared {deleted} session(s){suffix}."

    def diff(self, session_a: str, session_b: str, json_mode: bool = False) -> str:
        """Compare two stopped sessions.

        Raises:
            SessionError: Either session has no summary -- it was never stopped,
                or the id does not exist. Returned as text at exit 0 before X5,
                which is indistinguishable from a diff that found no changes.
            cli.Failure: The sessions were captured under different fingerprint
                versions, so the same stall carries a different fingerprint in
                each, and every cluster would read as resolved in one and new in
                the other.
        """
        summary_a = self.store.load_summary(session_a)
        summary_b = self.store.load_summary(session_b)
        if summary_a is None or summary_b is None:
            missing = [s for s, x in [(session_a, summary_a), (session_b, summary_b)] if x is None]
            raise SessionError(
                f"No summary.json for: {', '.join(missing)}. Run --stop <id> for "
                f"each session before --diff, and --list-sessions to see which "
                f"ids exist."
            )
        if summary_a.fingerprint_version != summary_b.fingerprint_version:
            # Refused rather than reported as a "skipped" diff at exit 0, which
            # read as "no change" to anything checking the status.
            cli.fail(
                f"Cannot compare {session_a} (fingerprint v{summary_a.fingerprint_version}) "
                f"with {session_b} (fingerprint v{summary_b.fingerprint_version}): the "
                f"same stall is fingerprinted differently under each, so every cluster "
                f"would read as resolved in one and new in the other.",
                "Record both sessions with the same version of the skill -- re-run the "
                "older scenario with this one -- and diff those.",
            )
        result = diff_sessions(summary_a, summary_b)
        if json_mode:
            return json.dumps(result, indent=2)
        return format_diff(result)

    # === WORKER ===

    def run_worker(self, session_id: str, token: str | None = None) -> int:
        """Long-running worker entrypoint. Returns exit code.

        Layout: claim meta → build ``adb logcat -B -T 1`` command → open
        events.jsonl line-buffered → for each restart attempt, spawn logcat and
        read records until EOF or subprocess death. SIGTERM flushes and exits
        cleanly. EOF / subprocess death triggers a bounded restart loop
        (``ANDROID_EMU_ANR_MAX_RESTARTS``); on exhaustion the session is marked
        ``crashed`` rather than left in stale ``running`` state.

        A stream that is not logd's records, or ends inside one by itself, is
        not restarted (common.logcat's rule for a ``LogFramingError``): the
        device is asked whether it is still there, the answer is kept in the
        counters (``stream_failure``), the session is marked crashed, and
        ``--stop`` refuses -- exit 3 for a device that went, 1 otherwise. A
        record cut by our own stop is lost and counted (``cut_records``).
        """
        if token is not None and token != self.store.load_meta(session_id).worker_token:
            # Not the worker this session's parent started: claiming would
            # hand --stop a process it cannot tell from the real one.
            return 2
        meta = self.store.claim_worker(session_id, pid=os.getpid())
        args = meta.args
        min_frames = int(
            args.get("min_frames", env_int("ANDROID_EMU_ANR_MIN_FRAMES", DEFAULT_MIN_FRAMES))
        )
        package = args.get("package")
        serial = args.get("serial")
        # min_value=0: env_int floors at 1 by default, which turned an explicit
        # "no restarts" into one restart (N14).
        max_restarts = env_int(
            "ANDROID_EMU_ANR_MAX_RESTARTS", DEFAULT_MAX_STREAM_RESTARTS, min_value=0
        )

        events_path = self.store.events_path(session_id)
        counters = {
            "total": 0,
            "matched": 0,
            "dropped": 0,
            "stream_restarts": 0,
            "unnamed_pids": [],
            "unverified_anr_lines": 0,
            "cut_records": 0,
            "unattributed_anrs": 0,
            "ambiguous_tail": [],
        }
        stop_flag = {"value": False}
        # One of each for the whole session. The resolver forgets its owners
        # on every stream (re)start but keeps what it learned of packages; the
        # boundary remembers what was consumed, which a restart's replay is
        # held against.
        process_names = _ProcessNames(
            *_device_lookups(serial, may_ask=lambda: not stop_flag["value"])
        )
        boundary = _CaptureBoundary()

        def _on_sigterm(_signum, _frame):
            stop_flag["value"] = True

        signal.signal(signal.SIGTERM, _on_sigterm)
        signal.signal(signal.SIGINT, _on_sigterm)

        cmd = AnrWatcher(serial=serial).build_logcat_command()

        def _spawn_logcat() -> subprocess.Popen:
            # Bytes, unbuffered: the reader takes records straight off the fd,
            # so there is no Python buffer for one to wait in (N16).
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )

        proc: subprocess.Popen | None = None
        crashed = False
        try:
            with open(events_path, "a", buffering=1) as out_handle:
                for attempt in range(max_restarts + 1):
                    if stop_flag["value"]:
                        break
                    if attempt > 0:
                        counters["stream_restarts"] = attempt
                        out_handle.write(
                            json.dumps(
                                {
                                    "event": "stream_restart",
                                    "attempt": attempt,
                                    "at_ms": int(time.time() * 1000),
                                }
                            )
                            + "\n"
                        )
                        out_handle.flush()
                        time.sleep(RESTART_BACKOFF_SECONDS)
                        if stop_flag["value"]:
                            break
                    try:
                        proc = _spawn_logcat()
                    except FileNotFoundError:
                        crashed = True
                        break

                    exit_code = self._read_stream_into_events(
                        proc=proc,
                        out_handle=out_handle,
                        stop_flag=stop_flag,
                        counters=counters,
                        package=package,
                        min_frames=min_frames,
                        session_start_ms=meta.started_at_ms,
                        process_names=process_names,
                        boundary=boundary,
                    )
                    # Read to its end or abandoned on a stop: either way this
                    # stream's pipe is done with, and a restart opens another.
                    if proc.stdout is not None:
                        proc.stdout.close()

                    failure = counters.get("stream_failure")
                    if failure is not None:
                        # Not records, or cut by itself: no restart can trust
                        # it. Whether the device is still there decides what
                        # --stop says (exit 3, or a refusal).
                        failure["device"] = _device_availability(serial)
                        out_handle.write(
                            json.dumps(
                                {
                                    "event": "stream_failed",
                                    **failure,
                                    "at_ms": int(time.time() * 1000),
                                }
                            )
                            + "\n"
                        )
                        out_handle.flush()
                        crashed = True
                        break

                    if stop_flag["value"]:
                        out_handle.write(
                            json.dumps({"event": "stream_ended", "at_ms": int(time.time() * 1000)})
                            + "\n"
                        )
                        out_handle.flush()
                        break

                    out_handle.write(
                        json.dumps(
                            {
                                "event": "stream_died",
                                "exit_code": exit_code,
                                "attempt": attempt,
                                "at_ms": int(time.time() * 1000),
                            }
                        )
                        + "\n"
                    )
                    out_handle.flush()
                else:
                    # for/else: ran every restart without a clean break — exhausted.
                    crashed = True

                with contextlib.suppress(OSError):
                    os.fsync(out_handle.fileno())
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # Counters first: marking the session crashed forgets this
            # worker's pid, and a --stop then reads the counters at once.
            self.store.persist_worker_counters(session_id, counters)
            if crashed and not stop_flag["value"]:
                self.store.mark_crashed(session_id)

        return 2 if crashed else 0

    def _read_stream_into_events(
        self,
        *,
        proc: subprocess.Popen,
        out_handle,
        stop_flag: dict,
        counters: dict,
        package: str | None,
        min_frames: int,
        session_start_ms: int,
        process_names: _ProcessNames | None = None,
        boundary: _CaptureBoundary | None = None,
    ) -> int | None:
        """Read logd's records until EOF / subprocess death / stop request.

        Returns the subprocess exit code. Does not emit ``stream_died`` /
        ``stream_ended`` markers itself — the caller decides which to write.

        ``process_names`` names the process behind a jank or StrictMode record
        (the worker passes one that asks the device who system_server is).
        Without one, nothing is named -- and no ANR report is authenticated,
        so none counts: the assembler asks the resolver who system_server is.
        ``boundary`` skips what a restarted stream replays of records the
        session already read, and marks the session's first record -- `-T
        1`'s replay, which may predate it -- whose events go to
        ``counters["ambiguous_tail"]``, not the events file (the worker passes
        the session's); without one, every record is read and counted.

        Under ``package``, an ANR of a window -- WindowManager's report alone,
        which names no package -- is dropped and counted
        (``unattributed_anrs``), never dropped unsaid.

        A ``LogFramingError`` ends the read (common.logcat's rule): cut by our
        own stop, the lost record is counted (``cut_records``); otherwise the
        failure is put in ``counters["stream_failure"]`` for the caller.
        """
        if proc.stdout is None:
            return proc.poll()
        stream = _FdRecords(proc.stdout)
        names = process_names or _ProcessNames()
        # A new stream: any pid may have changed hands while there was none,
        # and `-T 1` does not replay the new owner's Start proc record.
        names.reset()
        assembler = AnrRecordAssembler(names.system_server_pid, subject_owner=names.owns)
        if boundary is not None:
            boundary.new_stream()

        def record(raw_events: list[dict], *, just_read: bool = False) -> None:
            for raw_event in raw_events:
                # Both are logged from inside the app, so the record's pid is
                # the app's own -- and neither says which app that is. Named
                # only as the record is read, by what the stream said before it.
                if raw_event["kind"] in ("jank", "strictmode"):
                    raw_event["app"] = names.name_of(raw_event["pid"]) if just_read else None
                if package and not matches_package(raw_event, package):
                    if _unattributed(raw_event):
                        counters["unattributed_anrs"] = counters.get("unattributed_anrs", 0) + 1
                    continue
                frames = raw_event.get("frames")
                # Only Choreographer jank is frame-thresholded; ANRs (frames=None) pass.
                below = frames is not None and frames < min_frames
                if raw_event.get("ambiguous_tail"):
                    # Shown in the summary, kept out of every count, cluster
                    # and diff: nothing shows it was logged after --start.
                    if not below:
                        counters.setdefault("ambiguous_tail", []).append(_tail_evidence(raw_event))
                    continue
                counters["matched"] += 1
                if below:
                    counters["dropped"] += 1
                    continue
                normalised = build_normalised_event(
                    raw_event,
                    session_start_ms=session_start_ms,
                    current_ms=int(time.time() * 1000),
                )
                if normalised is not None:
                    out_handle.write(event_to_jsonl(normalised) + "\n")

        def take(records: list[logcat.LogRecord]) -> None:
            for log_record in records:
                verdict = _CaptureBoundary.READ if boundary is None else boundary.admit(log_record)
                if verdict == _CaptureBoundary.REPLAYED:
                    continue
                counters["total"] += 1
                names.observe(log_record)
                ambiguous = verdict == _CaptureBoundary.AMBIGUOUS
                record(assembler.feed(log_record, ambiguous=ambiguous), just_read=True)

        def read(timeout: float) -> list[logcat.LogRecord] | None:
            """The stream's next records; None once it has ended or broken."""
            try:
                return stream.read(timeout=timeout)
            except logcat.LogFramingError as error:
                if error.truncated and stop_flag["value"]:
                    counters["cut_records"] = counters.get("cut_records", 0) + 1
                else:
                    counters["stream_failure"] = {
                        "error": str(error),
                        "truncated": error.truncated,
                    }
                return None

        try:
            last_fsync = time.time()
            last_record_at = time.monotonic()
            while not stop_flag["value"]:
                records = read(STREAM_POLL_SECONDS)
                if records is None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=0.5)
                    return proc.poll()
                if records:
                    last_record_at = time.monotonic()
                    take(records)
                    continue
                # Quiet: a WindowManager report nothing answered for the whole
                # correlation window is an ANR of its own.
                record(assembler.flush(time.monotonic() - last_record_at))
                if time.time() - last_fsync > 1.0:
                    out_handle.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(out_handle.fileno())
                    last_fsync = time.time()
                exit_code = proc.poll()
                if exit_code is not None:
                    return exit_code

            # Stopped. Whatever the stream had already delivered is part of the
            # session: read it before closing, bounded, since logcat keeps writing.
            deadline = time.monotonic() + STOP_DRAIN_SECONDS
            while time.monotonic() < deadline:
                records = read(0)
                if not records:
                    break
                take(records)
            if stream.partial:
                # The rest of a record was still on its way when we stopped an
                # open stream, whose end we never read: lost, and counted.
                counters["cut_records"] = counters.get("cut_records", 0) + 1
            return proc.poll()
        finally:
            # However this stream ended, what the assembler still holds was
            # seen: a WindowManager report nothing answered.
            record(assembler.close())
            # What this stream could not see, for the summary to disclose.
            counters["unnamed_pids"] = names.unnamed_pids
            counters["unverified_anr_lines"] = (
                counters.get("unverified_anr_lines", 0) + assembler.unverified
            )

    # === PRIVATE ===

    def _wait_for_worker_exit(self, session_id: str, timeout_seconds: float) -> bool:
        """Whether the worker exited within ``timeout_seconds``."""
        meta = self.store.load_meta(session_id)
        if not meta.pid:
            return True
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                os.kill(meta.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def _dump_raw_events(self, session_id: str) -> str:
        path = self.store.events_path(session_id)
        if not path.exists():
            return ""
        with open(path) as handle:
            return handle.read()

    def _resolve_serial(self, serial: str | None) -> str | None:
        # Not re-wrapped: a bare RuntimeError lost DeviceNotFoundError, so an
        # unavailable device exited 1 here and 3 everywhere else.
        return resolve_device_identifier(serial)


def _unattributed(event: dict) -> bool:
    """An ANR filed under no package for want of evidence: of a window, or of a process."""
    return event["kind"] == "anr" and (
        event.get("window") is not None or event.get("process_name") is not None
    )


def _tail_evidence(event: dict) -> dict:
    """What a summary shows of an event the record a session started on made."""
    shown = (
        "kind",
        "timestamp",
        "process",
        "pid",
        "message",
        "reason",
        "frames",
        "duration_ms",
        "window",
        "process_name",
    )
    return {
        **{key: event.get(key) for key in shown},
        "app": event.get("app") or event.get("anr_package"),
    }


# The device errors a worker may have met asking `adb get-state` after its
# stream broke, by name, as it persisted them -- for --stop to raise again.
_DEVICE_ERRORS: dict[str, type[DeviceError]] = {
    cls.__name__: cls
    for cls in (
        DeviceNotFoundError,
        DeviceOfflineError,
        DeviceUnauthorizedError,
        MultipleDevicesError,
        DeviceError,
    )
}


def _refuse_broken_stream(session_id: str, failure: dict) -> NoReturn:
    """--stop's answer for a session whose stream broke (common.logcat's framing rule).

    The summary is saved first, so ``--get-details`` reads what was captured
    before the break; what --stop answers is the refusal. What `adb get-state`
    said when the worker asked is raised again as the error it was -- its
    class, words and remedy, persisted by the worker -- so it reaches the
    boundary as it would have met directly: a device gone is exit 3, several
    and no --serial exit 1 with the serials to pass (wave 4: every device
    error was rebuilt as "not found", exit 3, "Reconnect"). Anything else is
    exit 1, naming the framing error.
    """
    device = failure.get("device")
    error = failure.get("error", "the stream broke")
    saved = (
        f"The summary of what was read before the break is saved -- read it with "
        f"--get-details {session_id}."
    )
    if isinstance(device, dict) and device.get("error_class") in _DEVICE_ERRORS:
        said = device.get("message") or device["error_class"]
        remedy = device.get("remedy") or "Run `adb devices` to see what is attached."
        raise _DEVICE_ERRORS[device["error_class"]](
            f"Session {session_id}'s logcat stream broke ({error}); asked then, adb said: "
            f"{said}",
            remedy=f"{remedy} {saved}",
        )
    cli.fail(
        f"Session {session_id}'s logcat stream was not logd's records to its end ({error}); "
        f"the session stopped there, so it is incomplete. The device, asked then: "
        f"{_availability_text(device)}.",
        f"Read what was captured with --get-details {session_id}, and record the scenario "
        f"again; if it repeats, restart adb (`adb kill-server && adb start-server`).",
    )


def _availability_text(device) -> str:
    """A persisted availability answer, in words: the worker's dict, or an older string."""
    if isinstance(device, dict):
        state = str(device.get("state", "unknown"))
        return f"{state}: {device['message']}" if device.get("message") else state
    return str(device or "unknown")


def _with_adb_exit(
    error: logcat.LogFramingError, returncode: int | None, stderr: str
) -> logcat.LogFramingError:
    """``error``, saying how adb exited too when it exited with a failure."""
    if not returncode:
        return error
    said = " ".join(stderr.split()) or "nothing on stderr"
    return logcat.LogFramingError(
        f"{error}; `adb logcat` exited {returncode}: {said}",
        offset=error.offset,
        truncated=error.truncated,
    )


def _device_availability(serial: str | None) -> dict:
    """What `adb get-state` says of the device, asked once, kept whole for --stop.

    ``state`` is ``available``; ``unavailable`` only for what the boundary
    calls unavailable (``cli.DEVICE_UNAVAILABLE``: exit 3); ``error`` for
    another device error -- several devices and no serial, exit 1 -- and
    ``unknown`` when adb could not answer. A device error keeps its class
    name, words and remedy, which ``_refuse_broken_stream`` raises again: a
    ``MultipleDevicesError`` was labelled unavailable here and rebuilt as
    "not found", exit 3 and "Reconnect the device" (wave 4).
    """
    try:
        _require_device(serial)
    except AdbError as error:
        if isinstance(error, cli.DEVICE_UNAVAILABLE):
            state = "unavailable"
        elif isinstance(error, DeviceError):
            state = "error"
        else:
            state = "unknown"
        answer = {"state": state, "message": " ".join(str(error).split())}
        if isinstance(error, DeviceError):
            answer.update(error_class=type(error).__name__, remedy=error.remedy)
        return answer
    return {"state": "available"}


# === CLI ===


def _add_mode_args(parser: argparse.ArgumentParser) -> None:
    """Add mutually-exclusive mode flags (legacy + ANRBuster subcommands)."""
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--watch", action="store_true", help="Legacy live stream (until --duration / Ctrl-C)"
    )
    mode_group.add_argument(
        "--since",
        metavar="DURATION",
        help="Legacy historical query (e.g. 5m, 1h, 30s) via 'logcat -d -t'",
    )
    mode_group.add_argument(
        "--start",
        action="store_true",
        help="Start an ANRBuster session (detached worker, returns session ID)",
    )
    mode_group.add_argument(
        "--stop", metavar="SESSION_ID", help="Stop a session and emit the summary"
    )
    mode_group.add_argument(
        "--get-details",
        metavar="SESSION_ID",
        help="Drill into a stored session (combine with --cluster N or --raw)",
    )
    mode_group.add_argument(
        "--list-sessions", action="store_true", help="List stored ANRBuster sessions"
    )
    mode_group.add_argument("--clear-sessions", action="store_true", help="Delete stored sessions")
    mode_group.add_argument(
        "--diff", nargs=2, metavar=("SESSION_A", "SESSION_B"), help="Compare two sessions"
    )
    # Internal worker entry — hidden from --help.
    mode_group.add_argument("--worker-session-id", metavar="ID", help=argparse.SUPPRESS)
    parser.add_argument(WORKER_TOKEN_FLAG, metavar="TOKEN", help=argparse.SUPPRESS)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Watch for Android ANR/jank events via adb logcat. Use --watch/--since "
            "for the live stream, or --start/--stop for ANRBuster session mode."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Also reachable as `python scripts/logs.py anr ...` — the unified log entry
point, which routes here unchanged. Both invocations are supported.

Examples:
  # ANRBuster session mode (agent-friendly):
  SID=$(python scripts/anr_watcher.py --start --package com.myapp)
  # ... interact with the app ...
  python scripts/anr_watcher.py --stop $SID
  python scripts/anr_watcher.py --get-details $SID --cluster 1
  python scripts/anr_watcher.py --list-sessions
  python scripts/anr_watcher.py --diff $SID_A $SID_B
  python scripts/anr_watcher.py --clear-sessions --older-than 24h

  # Legacy:
  python scripts/anr_watcher.py --watch --duration 60
  python scripts/anr_watcher.py --since 5m --json

Environment variables:
  ANDROID_EMU_ANR_MIN_FRAMES         Min skipped frames kept (default 30)
  ANDROID_EMU_ANR_SESSION_TTL_HOURS  Session prune age (default 24)
  ANDROID_EMU_ANR_TOTAL_CAP_MB       Aggregate disk cap MB (default 100; 0 = none)
  ANDROID_EMU_ANR_MAX_RESTARTS       Worker stream re-spawns (default 3; 0 = none)
  ANDROID_EMU_ANR_DEFAULT_TOP_N      Default top-N for --stop (default 3; 0 = all)
  ANDROID_EMU_ANR_BUDGET_TOKENS      Default token budget for --stop
        """,
    )
    _add_mode_args(parser)

    # Filters / target
    parser.add_argument(
        "--package",
        help=(
            "Post-parse filter: keep only events whose package is exactly this one -- as "
            "ActivityManager names it, or as a session verified a jank record's process. "
            "Capture stays device-global (system tags are kept)."
        ),
    )
    parser.add_argument("--serial", help="Device serial (auto-detects default device if omitted)")

    # Legacy-only
    parser.add_argument(
        "--duration", type=int, metavar="SECONDS", help="Stop after N seconds (--watch only)"
    )

    # ANRBuster knobs
    parser.add_argument(
        "--min-frames",
        type=int,
        help=(
            "Drop skipped-frame events below this frame count "
            "(default 30 / env ANDROID_EMU_ANR_MIN_FRAMES)"
        ),
    )
    parser.add_argument("--top", type=int, dest="top_n", help="Top-N clusters to retain in summary")
    parser.add_argument(
        "--all", action="store_true", dest="all_clusters", help="Keep all clusters (no top-N cap)"
    )
    parser.add_argument(
        "--budget-tokens", type=int, help="Max tokens for --stop output (picks L0/L1/L2)"
    )
    parser.add_argument(
        "--cluster", type=int, help="Cluster index (1-based) for --get-details drill"
    )
    parser.add_argument("--raw", action="store_true", help="With --get-details: dump events.jsonl")
    parser.add_argument(
        "--older-than", help="With --clear-sessions: delete sessions older than e.g. 24h"
    )
    parser.add_argument("--terse", action="store_true", help="--stop: force L0 one-line output")

    # Output
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def _require_valid_session_ids(parser, store, session_ids) -> None:
    """Reject a malformed session id as a usage error (exit 2).

    The grammar belongs to ``SessionStore.session_dir``, which is where the
    path is built and where the traceback came from: `--diff a/b c` raised
    ValueError out of the store, through main, to the terminal (D7). Asking the
    store keeps one rule rather than a copy of it here; ``session_dir`` only
    validates and joins, it touches no filesystem.
    """
    for session_id in session_ids:
        try:
            store.session_dir(session_id)
        except ValueError as error:
            parser.error(str(error))


def main(argv: list[str] | None = None):
    """Main entry point — supports legacy + ANRBuster modes from one parser."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # === ANRBuster worker entry ===
    if args.worker_session_id:
        buster = AnrBuster()
        sys.exit(buster.run_worker(args.worker_session_id, token=args.worker_token))

    # === ANRBuster session subcommands ===
    if args.start:
        buster = AnrBuster()
        start_args = {
            "min_frames": (
                args.min_frames
                if args.min_frames is not None
                else env_int("ANDROID_EMU_ANR_MIN_FRAMES", DEFAULT_MIN_FRAMES)
            ),
            "package": args.package,
        }
        session_id = buster.start(start_args, serial=args.serial)
        # The bare id is the text form, so `SID=$(anr_watcher.py --start)` keeps
        # working. Under --json it was the same bare id: a JSON document, by
        # luck of being a token, but not an object an agent could read a field
        # from -- and not the shape every other mode answers in.
        if args.json:
            print(json.dumps({"success": True, "session_id": session_id}))
        else:
            print(session_id)
        sys.exit(0)

    if args.stop:
        buster = AnrBuster()
        # A malformed id is a usage error here as it is for --get-details and
        # --diff; it reached the script-local `_fail` below as a ValueError.
        _require_valid_session_ids(parser, buster.store, [args.stop])
        # 0 means no cap, matching the repo's '0 = disabled' convention.
        top_n = 0 if args.all_clusters else args.top_n
        try:
            out = buster.stop(
                args.stop,
                budget_tokens=args.budget_tokens,
                top_n=top_n,
                terse=args.terse,
                json_mode=args.json,
            )
        except ValueError as error:
            # The id is well-formed, so this is the session's own files failing
            # to parse. Reported through the boundary in its one shape, where a
            # script-local `_fail` printed {"error": ...} and exited 1 itself.
            cli.fail(
                f"Session {args.stop} could not be read: {error}",
                f"Its files under {buster.store.session_dir(args.stop)} are damaged. "
                f"Delete that directory, or run --clear-sessions, and record a new session.",
            )
        except FileNotFoundError as error:
            # Re-raised typed, with the same words get_details uses, rather than
            # reported here: common.cli is the one place a failure is printed.
            raise SessionError(
                f"Unknown session: {args.stop}. Run --list-sessions to see the "
                f"sessions this host has stored."
            ) from error
        print(out)
        sys.exit(0)

    if args.get_details:
        buster = AnrBuster()
        _require_valid_session_ids(parser, buster.store, [args.get_details])
        out = buster.get_details(
            args.get_details,
            cluster=args.cluster,
            raw=args.raw,
            json_mode=args.json,
        )
        print(out)
        sys.exit(0)

    if args.list_sessions:
        buster = AnrBuster()
        print(buster.list_sessions(json_mode=args.json))
        sys.exit(0)

    if args.clear_sessions:
        buster = AnrBuster()
        try:
            out = buster.clear_sessions(older_than=args.older_than, json_mode=args.json)
        except ValueError as error:
            # `--older-than 24hours` reached the terminal as a ValueError
            # traceback (D7). The duration grammar is the store's, and it is
            # resolved before anything is deleted, so this is a usage error and
            # nothing has been removed by the time it is raised.
            parser.error(str(error))
        print(out)
        sys.exit(0)

    if args.diff:
        buster = AnrBuster()
        _require_valid_session_ids(parser, buster.store, args.diff)
        out = buster.diff(args.diff[0], args.diff[1], json_mode=args.json)
        print(out)
        sys.exit(0)

    # === Legacy modes ===
    if args.since:
        try:
            _compute_start_timestamp(args.since)
        except ValueError as error:
            parser.error(str(error))

    # Resolved before adb is asked anything: `adb -s <unknown> logcat` does not
    # fail, it prints "- waiting for device -" and blocks (measured), so a
    # mistyped --serial would read as a quiet device for as long as --watch
    # ran. An unknown serial is DeviceNotFoundError here, exit 3. With no
    # --serial nothing is resolved, and `adb logcat` waits just the same for a
    # device that is not there -- `--since` too, until its bound -- so adb is
    # asked first, whichever device it would use (B4).
    watcher = AnrWatcher(serial=resolve_device_identifier(args.serial))
    _require_device(watcher.serial)
    if args.watch:
        success = watcher.watch(
            duration_seconds=args.duration,
            package=args.package,
            json_mode=args.json,
        )
    else:
        success = watcher.show_since(
            since_duration=args.since,
            package=args.package,
            json_mode=args.json,
        )
    if not success:
        sys.exit(1)
    if not args.json and not args.watch:
        print(f"\n{watcher.get_summary()}")
    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
