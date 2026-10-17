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

This module is pure: it builds argv and parses strings, and never touches a
device. Execution stays with ``common.adb_exec.run_adb`` (bounded, typed errors)
or, for the two streaming readers, their own watchdog-terminated ``Popen``.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

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
# `-v usec` the fraction has six digits. app_state_capture and log_monitor read
# one app's window through this and app_window / AppWindowStream, so the two
# agree on what that window is (v0.8.0 D4). Recorded: logcat_threadtime (ms),
# logcat_app_restart_usec.
_THREADTIME = re.compile(
    r"^(?P<head>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}(?:\d{3})?\s+(?P<pid>\d+)\s+\d+\s+"
    r"(?P<priority>[VDIWEF])\s+(?P<tag>[^:]*?)\s*):(?P<message>.*)$"
)

# The extra format term the app-window readers ask for: microsecond headers.
# The lines of ONE multi-line message share their header to the microsecond;
# two messages written in one millisecond need not differ at millisecond
# precision -- recorded: system_server's `Force stopping` and `Killing` lines
# once shared `16:14:16.651`. A continuation is told apart by its header only
# at microsecond precision (measured: a crash trace's lines all `.125686`).
USEC_FORMAT = "usec"

# The tag whose lines name an app's processes, and the priority it logs them
# at (measured, API 35: `I ActivityManager: Start proc <pid>:<pkg>/<user>`,
# `Killing ...`, `Process <pkg> (pid N) has died`; recorded in
# logcat_app_restart_usec). A `*:W` filter drops them, so a reader that needs them
# under one adds this term.
PROCESS_TAG = "ActivityManager"
PROCESS_TAG_PRIORITY = "I"


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


# --- one app's window: which lines are its processes' -----------------------
#
# A pid is the app's only when (i) the device vouches that its uid is the
# package's, and (ii) the line falls inside that pid's life as the app's
# process. The evidence, all recorded in logcat_app_restart_usec (an app crashed,
# was relaunched, force-stopped and relaunched):
#
#     ActivityManager: Start proc 21149:<package>/u0a205 for next-top-activity {...}
#     ActivityManager: Process <package> (pid 21149) has died: fg  TOP
#     ActivityManager: Killing 21205:<package>/u0a205 (adj 0): stop <package> ...
#
# plus `pidof <package>` for the process alive now, its uid checked with `ps`.
# What does NOT count, and why (security reviews of #27):
#
# - the package name on its own: most lines naming it are system_server's,
#   the launcher's and the IME's;
# - the tag: any app may log under ActivityManager -- a sentence counts only
#   when system_server WROTE it (the PID column is system_server's pid, asked
#   of the device; unknown, nothing counts);
# - a sentence anywhere but the very start of the message: system_server's
#   messages carry app-controlled text further on (a component, an intent);
# - a continuation line: a multi-line message is printed as several lines
#   with one header, so a later line of it can start with any text the app
#   put there. Only the first line of a run sharing the full header -- to the
#   microsecond, see USEC_FORMAT -- may hold a sentence;
# - the process name: an app may name its own process after another package
#   (`android:process`), and system_server then really logs `Start proc
#   N:<that package>/<the impostor's uid>`. Start proc and Killing carry the
#   uid; it must be the package's (asked of the device; unknown, nothing
#   counts). `has died` carries none, so it only ends a life already
#   confirmed;
# - a crash's own `AndroidRuntime: Process: <package>, PID: N`: any app can
#   write it for another package through the public Log API (ruling A).

# AOSP's UserHandle.formatUid, as system_server prints the uid after a process
# name: below FIRST_APPLICATION_UID the bare number, else u<user> followed by
# a<n> (an app; recorded u0a205), i<n> (isolated), ai<n> (app-zygote
# isolated) or s<appid> (a shared system id).
_PER_USER_RANGE = 100_000
_UID_BASES = {"a": 10_000, "ai": 90_000, "i": 99_000, "s": 0}
_FORMATTED_UID = re.compile(r"u(?P<user>\d+)(?P<kind>ai|a|i|s)(?P<n>\d+)")

# The three sentences, as the whole opening of the message -- right after
# "ActivityManager: " -- for ANY process name; whose process it is comes after.
_START = re.compile(r" Start proc (?P<pid>\d+):(?P<name>[^/\s]+)/(?P<uid>\S+) ")
_KILLING = re.compile(r" Killing (?P<pid>\d+):(?P<name>[^/\s]+)/(?P<uid>\S+) \(adj -?\d+\): ")
_DIED = re.compile(r" Process (?P<name>\S+) \(pid (?P<pid>\d+)\) has died\b")

# What a sentence says about the pid it names.
_STARTED = "started"  # an app process began (uid checked)
_KILLED = "killed"  # an app process was killed (uid checked)
_DIED_APP = "died"  # a process named like the app died (no uid: ends only)
_OTHER = "other"  # the pid is (now) some other process's


def uid_from_formatted(text: str) -> int | None:
    """The numeric uid ``UserHandle.formatUid`` printed as ``text``.

    ``u0a205`` -> 10205, ``u10a205`` -> 1010205, ``1000`` -> 1000; None for
    anything else.
    """
    if text.isdigit():
        return int(text)
    match = _FORMATTED_UID.fullmatch(text)
    if match is None:
        return None
    return int(match["user"]) * _PER_USER_RANGE + _UID_BASES[match["kind"]] + int(match["n"])


class _Lazy:
    """A value, or a function giving it, asked for at most once."""

    def __init__(self, value):
        self._value = value
        self._asked = not callable(value)

    def __call__(self):
        if not self._asked:
            self._asked = True
            self._value = self._value()
        return self._value


class _Reader:
    """Reads lines in order: which pid wrote each, and what a sentence says."""

    def __init__(self, package: str, system_server_pid, package_uids) -> None:
        self.package = package
        self.system_server = _Lazy(system_server_pid)
        self.package_uids = _Lazy(package_uids)
        self._previous_head: str | None = None

    def read(self, line: str) -> tuple[str | None, tuple[str, str] | None]:
        """``(pid that wrote line, (what, pid) said by it)``; None where there is none."""
        match = _THREADTIME.match(line)
        if match is None:
            self._previous_head = None
            return None, None
        first = match["head"] != self._previous_head
        self._previous_head = match["head"]
        if not first or match["tag"] != PROCESS_TAG:
            return match["pid"], None
        return match["pid"], self._sentence(match["pid"], match["message"])

    def _sentence(self, writer: str, message: str) -> tuple[str, str] | None:
        found, pattern = next(
            ((m, p) for p in (_START, _KILLING, _DIED) if (m := p.match(message))), (None, None)
        )
        if found is None or writer != self.system_server():
            return None
        pid = found["pid"]
        if found["name"] != self.package:
            return _OTHER, pid
        if pattern is _DIED:
            return _DIED_APP, pid
        # Any of the package's uids: it has one per user it is installed for
        # (recorded: package_list_uid_two_users).
        uids = _uid_set(self.package_uids())
        if not uids or uid_from_formatted(found["uid"]) not in uids:
            return _OTHER, pid
        return (_STARTED if pattern is _START else _KILLED), pid


def _uid_set(uids: int | Iterable[int] | None) -> frozenset[int]:
    """One uid, several, or none, as a set."""
    if uids is None:
        return frozenset()
    return frozenset([uids]) if isinstance(uids, int) else frozenset(uids)


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
    """One app's lines of a logcat window, and which pids they came from."""

    lines: list[str]
    pids: set[str]
    pids_from_log: set[str]


def split_log_lines(text: str) -> list[str]:
    """logcat output as lines, split on ``\\n`` ONLY.

    logcat frames each line with ``\\n`` and leaves a ``\\r`` an app wrote
    inside its message raw (AOSP liblog logprint). ``str.splitlines`` -- and
    a text-mode pipe -- also break on ``\\r``, which let an app append
    ``\\r<a forged system_server header> Start proc ...`` to its own line and
    have it read as a line of its own (security review of #27). A single
    ``\\r`` ending a line is dropped, as a line terminator; one inside stays.
    """
    return [line.removesuffix("\r") for line in text.split("\n")]


def decode_log_line(raw: bytes) -> str:
    """One ``\\n``-delimited line of logcat bytes, as text (``\\r`` kept inside).

    Splitting on ``b"\\n"`` before decoding is safe: in UTF-8 that byte is
    never part of a multi-byte character.
    """
    return raw.decode("utf-8", errors="replace").removesuffix("\n").removesuffix("\r")


def app_window(
    text: str,
    package: str,
    *,
    system_server_pid: str | Callable[[], str | None] | None = None,
    package_uids: int | Iterable[int] | Callable[[], Iterable[int] | None] | None = None,
    running: Iterable[str] = (),
) -> AppWindow:
    """The lines of a whole (historical) window that were ``package``'s processes'.

    Args:
        text: ``logcat -v threadtime [-v usec]`` output.
        package: The application id.
        system_server_pid: system_server's pid, or a function returning it
            (None when the device cannot say); asked only if the window holds
            an ActivityManager sentence. Unknown: no sentence counts.
        package_uids: The package's uids -- one per user it is installed for --
            or a function returning them; asked only if a sentence names the
            package. A sentence's uid must be one of them. Unknown: no
            sentence counts.
        running: Pids alive now whose uid the caller checked is the package's
            (``pidof`` then ``ps``). Each one's life runs to the window's end.

    Returns:
        The lines in input order, the pids with a life in the window, and
        those of them the log itself named.
    """
    lines = split_log_lines(text)
    reader = _Reader(package, system_server_pid, package_uids)
    writers: list[str | None] = []
    events: dict[str, list[tuple[int, str]]] = {}
    for index, line in enumerate(lines):
        writer, sentence = reader.read(line)
        writers.append(writer)
        if sentence is not None:
            events.setdefault(sentence[1], []).append((index, sentence[0]))
    alive = set(running)
    spans = {
        pid: found
        for pid in set(events) | alive
        if (found := _spans(events.get(pid, []), len(lines), pid in alive))
    }
    kept = [
        line
        for index, (line, writer) in enumerate(zip(lines, writers, strict=True))
        if writer in spans and any(start <= index <= end for start, end in spans[writer])
    ]
    from_log = {
        pid for pid in spans if any(w in (_STARTED, _KILLED) for _, w in events.get(pid, []))
    }
    return AppWindow(kept, set(spans), from_log)


def parse_package_pids(
    text: str,
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
        text, package, system_server_pid=system_server_pid, package_uids=package_uids
    ).pids


class AppWindowStream:
    """:func:`app_window` for a live read: decides each line as it arrives.

    A live read cannot see ahead, and a process already running when it began
    is named only when it is killed. So a line from a pid not yet decided is
    held -- up to ``pending_cap`` lines, oldest dropped first -- and returned
    when a uid-checked ``Killing`` makes the pid the app's. Every line is
    numbered in input order, so a caller can store what it keeps in that order
    whatever the order of decision.

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
        self._pending: deque[tuple[int, str, str]] = deque(maxlen=pending_cap)
        self._evicted: dict[str, int] = {}
        self.pending_evicted = 0
        self._sequence = 0

    @property
    def unattributed(self) -> int:
        """Lines held now, from pids the read has not (yet) shown to be the app's."""
        return len(self._pending)

    def feed(self, line: str) -> list[tuple[int, str, bool]]:
        """Read one line; return ``(sequence, line, replayed)`` for what is now the app's."""
        sequence = self._sequence
        self._sequence += 1
        writer, sentence = self._reader.read(line)
        kept = self._apply(sentence) if sentence is not None else []
        if writer is None:
            return kept
        if writer in self._open:
            kept.append((sequence, line, False))
        elif writer not in self._decided:
            self._hold(sequence, writer, line)
        return kept

    def _apply(self, sentence: tuple[str, str]) -> list[tuple[int, str, bool]]:
        what, pid = sentence
        replayed: list[tuple[int, str, bool]] = []
        if what == _STARTED:
            # Anything pid logged before its start was an earlier process's.
            self._release(pid)
            self._open.add(pid)
            self.pids.add(pid)
            self.pids_from_log.add(pid)
        elif what == _KILLED and pid not in self._decided:
            replayed = [(seq, line, True) for seq, line in self._release(pid)]
            self.pending_evicted += self._evicted.pop(pid, 0)
            self.pids.add(pid)
            self.pids_from_log.add(pid)
            self._open.discard(pid)
        else:
            self._release(pid)
            self._open.discard(pid)
        self._evicted.pop(pid, None)
        self._decided.add(pid)
        return replayed

    def _hold(self, sequence: int, pid: str, line: str) -> None:
        if self._pending.maxlen is not None and len(self._pending) == self._pending.maxlen:
            dropped = self._pending[0][1]
            self._evicted[dropped] = self._evicted.get(dropped, 0) + 1
        self._pending.append((sequence, pid, line))

    def _release(self, pid: str) -> list[tuple[int, str]]:
        """Take ``pid``'s held lines out of the hold, in arrival order."""
        if not any(held == pid for _, held, _ in self._pending):
            return []
        mine = [(seq, line) for seq, held, line in self._pending if held == pid]
        self._pending = deque(
            ((seq, held, line) for seq, held, line in self._pending if held != pid),
            maxlen=self._pending.maxlen,
        )
        return mine


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
        usec: Add ``-v usec``: microsecond headers, which the app-window
            readers need to tell a continuation line (see USEC_FORMAT).
        tail: For a live read, ``-T <tail>``: start at the END of the buffer
            (the most recent ``tail`` lines, then follow) instead of replaying
            it all. Measured: placed before filterspecs, ``-T 1`` prints the
            newest line and then follows.
        pid: Restrict to one process via ``--pid=``. Falsy values are omitted,
            which reads EVERY process: a caller that means one app must not
            reach here with an unresolved pid, or its "app" window is the whole
            device (D4). One pid is also one process, not an app that
            restarted; see :func:`parse_package_pids`.
        min_priority: A single letter for the trailing ``*:<priority>`` term.
        tag_priorities: ``(tag, letter)`` terms placed before ``*:<priority>``,
            so one tag is read below the global floor (logcat applies the most
            specific term). ``log_monitor --app`` keeps ``ActivityManager:I``
            under ``*:W`` this way.

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
    if fmt:
        args.extend(["-v", fmt])
    if usec:
        args.extend(["-v", USEC_FORMAT])
    if tail is not None:
        args.extend(["-T", str(tail)])
    if pid:
        args.append(f"--pid={pid}")
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
