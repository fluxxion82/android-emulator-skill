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
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

# Resolve imports whether run from repo root or scripts/ directory.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from common import cli, log_lifecycle, logcat  # noqa: E402
from common.adb_exec import (  # noqa: E402
    _ADB_OWN_PREFIXES,
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
    PID_NAMED_KINDS,
    VERIFICATION_UNKNOWN,
    AnrRecordAssembler,
    SessionSummary,
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
from common.anr_sessions import (  # noqa: E402
    ABANDONED,
    CANCELLED,
    CRASHED,
    DECIDING,
    LIVE_STATES,
    PENDING,
    RUNNING,
    WORKER_END_LOCK_TIMEOUT_SECONDS,
    WORKER_TOKEN_FLAG,
    Identity,
    SessionBusyError,
    SessionMeta,
    SessionStore,
    TransitionRefusedError,
    worker_identity,
)
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
# death, the device still there, before giving up: the session is marked
# crashed, and --stop refuses it. 0 is a real setting: no restarts, so a dead
# stream ends the session at once.
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
# How long a reader whose stream broke gives adb to finish, and to say why on
# stderr, before it reads what adb said: the session worker's wait, now
# --watch's too (an error written 0.1 s after the bad bytes was exit 1 in
# --watch and 3 in a session; third review of PR #42).
STREAM_END_GRACE_SECONDS = 0.5
# How much of what adb wrote on stderr is read back, from its start and from
# its end: adb's error may come first and chatter follow it, and a tail alone
# lost it (second review of PR #42). A stream's stderr is a file of its own.
# What lies between the two is scanned, not kept (_adb_own_lines).
STDERR_KEPT_BYTES = 64 * 1024
# The lines adb_exec._classify reads -- adb's own, by their start -- as bytes:
# all a scan of stderr between its kept head and tail keeps.
_ADB_OWN_LINE_STARTS = tuple(prefix.encode() for prefix in _ADB_OWN_PREFIXES)
# How much of it a message shows, first and last: the rest is in no refusal.
STDERR_SHOWN_CHARS = 200
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
# Bound on the `adb devices` a device error's message lists the serials from
# (adb_exec._attached_serials), asked once for each such error.
DEVICE_LIST_TIMEOUT_SECONDS = 10
# How long a worker may take to decide how a stream that ended by itself ended
# (_ended_by_itself): adb's exit, then `adb get-state`, each device error's
# serial list, and ending the stream's process -- each bounded. --stop waits
# this long for a worker it finds deciding: answering before it, it saved a
# summary with no stream failure and exited 0 while the worker went on to
# find the device gone (second review of PR #42).
DECIDING_TIMEOUT_SECONDS = (
    STREAM_EXIT_TIMEOUT_SECONDS
    + GET_STATE_TIMEOUT_SECONDS
    + DEVICE_LIST_TIMEOUT_SECONDS
    + 2 * 2.0  # _reap: terminate, then kill
    + 2.0
)
# How often a session's worker writes its counters while its stream is quiet:
# what a session --stop --abandon saves then is that recent, and a worker whose
# write is refused -- its session abandoned or deleted -- finds out and stops.
COUNTERS_PERSIST_SECONDS = 5.0
# How long --start waits for its worker to claim the session before it cancels it.
REGISTRATION_TIMEOUT_SECONDS = 3.0

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
    detail = _shown(stderr)
    said = f": {detail}" if detail else ", and said nothing on stderr."
    cli.fail(f"`adb logcat` exited {returncode}{said}", _LOGCAT_REMEDY)


def _raise_device_error_said(returncode: int | None, stderr: str, serial: str | None) -> None:
    """Raise the device error adb's own stderr names, when adb exited with a failure status.

    The first question about a live read that ended by itself, before the
    device is asked again or the read refused for how it ended: a device that
    went and is back by the time `adb get-state` is asked would otherwise read
    as a read refused for its framing, exit 1, where adb had said why (closure
    review of v0.8.0; log_monitor types it first too). Not after a success,
    as run_adb types adb's words; but an adb that has not exited yet
    (``returncode`` None) has said what it said, and is read too (review of
    PR #42: a worker that did not wait for an open stream read none of it).
    """
    if returncode != 0:
        device_error = classify_device_error(stderr, serial)
        if device_error is not None:
            raise device_error


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
    left unnamed are listed in the summary (``unnamed_pids``), and what
    ``--package`` dropped of them is counted (``unnamed_dropped``): it may be
    the package's, so ``--diff`` concludes nothing from its absence.
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
        # Jank/StrictMode --package dropped: this reader names no process, so
        # each may be the package's -- said, never a silent "no jank".
        self.unnamed_dropped = 0
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
                                # Not logd's records at all: refused -- after
                                # what adb has said, read without waiting for
                                # an open stream to end, as the session worker
                                # reads it (second review of PR #42).
                                returncode, said = _exit_and_stderr(
                                    self._process, adb_stderr, timeout=STREAM_END_GRACE_SECONDS
                                )
                                _raise_device_error_said(returncode, said, self.serial)
                                raise _with_adb_exit(error, returncode, said) from None
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
                    self._disclose_unnamed_dropped()

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
                    # is the answer (exit 3) -- as adb said it, then as `adb
                    # get-state` says it now; otherwise the read is refused for
                    # its framing, with how adb exited added. adb's words come
                    # first: a device back by the time it is asked made a
                    # device error adb had written exit 1 (closure review of
                    # v0.8.0). Its other failure statuses are judged after the
                    # framing, which would otherwise answer first and skip it
                    # (Codex review of PR #34).
                    _raise_device_error_said(returncode, stderr, self.serial)
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
        if self._process is None:
            return None, ""
        return _exit_and_stderr(self._process, adb_stderr)

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
        _raise_device_error_said(returncode, stderr, self.serial)
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
        self._disclose_unnamed_dropped()
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
            ) + (
                f" --package dropped {self.unnamed_dropped} jank/StrictMode record(s) of a "
                f"process this read does not name."
                if self.unnamed_dropped
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

    def _disclose_unnamed_dropped(self) -> None:
        """Say so when --package dropped jank/StrictMode it could not attribute: never unsaid."""
        if self.unnamed_dropped:
            plural = "s" if self.unnamed_dropped != 1 else ""
            print(
                f"warning: --package dropped {self.unnamed_dropped} jank/StrictMode record{plural}: "
                f"--watch and --since do not name the process behind one, so "
                f"{'they' if plural else 'it'} may be the package's. A session (--start/--stop) "
                f"names a process it sees start.",
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
            elif event["kind"] in PID_NAMED_KINDS:
                self.unnamed_dropped += 1
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


class _ReadEnd(StrEnum):
    """Why a session stream's reading ended, recorded as it ended (``_read_stream_into_events``)."""

    # The stream ended by itself: its end of file, or its process gone.
    ENDED = "ended"
    # It was not logd's records, or ended inside one: ``counters["stream_failure"]``.
    BROKEN = "broken"
    # A stop came while it was open; what it had delivered was drained.
    STOPPED = "stopped"


class _Disowned(Exception):  # noqa: N818 -- a signal between two worker frames, never reported
    """A write of the worker's was refused: the session is no longer its own to write."""


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
        """Create session, detach worker, return session_id once its worker is recording.

        Succeeds only on a worker that claimed the session and is ``running``:
        a failure is published only with the session's end, in one write
        (``SessionStore.finish_worker``), so a running session holds none. A
        worker deciding how its stream ended is waited for, to that
        decision's bound; one that has already crashed is answered for as
        --stop answers -- the summary saved, refused by its failure's class
        (exit 3 for a device that went) -- and one that ended otherwise
        (stopped, cancelled or abandoned by another command meanwhile) is
        refused, exit 1, without a summary. A worker that has not claimed the
        session in ``REGISTRATION_TIMEOUT_SECONDS`` is given up on: the
        session is cancelled, so a worker that starts late records nothing.
        """
        self.store.prune_expired()
        # 0 disables the cap -- and env_int's default floor of 1 turned that
        # into a 1 MB cap that pruned sessions (the N14 shape).
        aggregate_cap_mb = env_int("ANDROID_EMU_ANR_TOTAL_CAP_MB", 100, min_value=0)
        if aggregate_cap_mb > 0:
            capped = self.store.prune_to_aggregate_cap(aggregate_cap_mb * 1024 * 1024)
            over = capped.over_cap
            if over is not None:
                # Best effort, never a refusal: a --start that cannot free space
                # is worse than an overrun (D11). On stderr: stdout is the id.
                print(
                    f"warning: ANR sessions hold {over['bytes_over']} bytes over "
                    f"ANDROID_EMU_ANR_TOTAL_CAP_MB ({aggregate_cap_mb} MB): the "
                    f"{over['protected']} session(s) left have not ended, or another command "
                    f"holds them, so none could be pruned. --stop them (--stop --abandon one "
                    f"whose worker is gone) to let the cap reclaim them.",
                    file=sys.stderr,
                )
        resolved_serial = self._resolve_serial(serial)
        # Before the session exists: a worker started without a device waits
        # in `adb logcat` forever, and the id it was given names nothing (B4).
        _require_device(resolved_serial)
        meta = self.store.create({**args, "serial": resolved_serial})
        session_id = meta.session_id
        # The token on the worker's command line is how --stop knows the
        # process at the recorded pid is still this worker (anr_sessions).
        cmd = [
            sys.executable,
            __file__,
            "--worker-session-id",
            session_id,
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
            registered = self.store.wait_for_worker(
                session_id, timeout_seconds=REGISTRATION_TIMEOUT_SECONDS
            )
        except TimeoutError:
            registered = self._give_up_on_registration(session_id)
        settled = self._decided(registered)
        if settled.status != RUNNING or settled.counters.get("stream_failure"):
            self._refuse_unstarted(settled)
        return session_id

    def _refuse_unstarted(self, settled: SessionMeta) -> NoReturn:
        """--start's answer for a worker that registered and is not recording."""
        session_id = settled.session_id
        failure = settled.counters.get("stream_failure")
        if settled.status == RUNNING and isinstance(failure, dict):
            # Never left so by a worker of this version; refused by the
            # failure's class all the same, as --stop raises it -- but nothing
            # is saved for a session still running.
            _refuse_broken_stream(session_id, failure, summary_saved=False)
        if settled.status in LIVE_STATES:
            cli.fail(
                f"The worker for {session_id} was still deciding how its logcat stream ended "
                f"{DECIDING_TIMEOUT_SECONDS:.0f}s after it began to, so --start cannot say "
                f"whether the session is recording.",
                f"Run --stop {session_id}: it answers with what the worker decides.",
            )
        if settled.status == CRASHED:
            # It registered and has already ended: what ended it is the answer,
            # never "did not register" (CI on PR #42). Stopped here as --stop
            # stops it, so the summary is saved and the refusal is --stop's:
            # the stream failure's own error, exit 3 for a device that went.
            self.stop(session_id)
            cli.fail(
                f"The worker for {session_id} started and ended at once without saying "
                f"why, so there is no session to record into.",
                f"Read what it saved with --get-details {session_id}, and --start again.",
            )
        cli.fail(
            f"Session {session_id} ended ({settled.status}) before --start could answer: "
            f"something stopped it as it started.",
            f"Read it with --get-details {session_id}, and --start again.",
        )

    def _give_up_on_registration(self, session_id: str) -> SessionMeta:
        """Cancel a session no worker claimed in time; the claimed meta when one just did."""
        with self.store.transaction(session_id) as meta:
            if meta.status == PENDING:
                # Cancelled, not marked crashed: a worker that claims it after
                # this is refused, where one that did went on to record into a
                # session --start had refused (fourth review of PR #42).
                self.store.mark_cancelled(meta)
        if meta.status == CANCELLED:
            cli.fail(
                f"The worker for {session_id} did not register within "
                f"{REGISTRATION_TIMEOUT_SECONDS:g}s, so the session is cancelled: a worker "
                f"that starts late records nothing into it.",
                "Run --start again; if it repeats, the host is too loaded to start Python in "
                "time.",
            )
        return meta

    def _decided(self, meta: SessionMeta) -> SessionMeta:
        """``meta`` once its worker is recording with no failure, or has ended (bounded).

        A worker deciding how its stream ended is waited for, and so is a
        ``running`` session that holds a failure -- which this version's worker
        never writes (its failure comes with its end), but whose ``running``
        must never read as started: --start answered `{"success": true}` over
        a DeviceNotFoundError already persisted (fourth review of PR #42).
        """
        first_seen = time.time()
        while meta.status == DECIDING or (
            meta.status == RUNNING and meta.counters.get("stream_failure")
        ):
            since = _deciding_since(meta) or first_seen
            if time.time() > since + DECIDING_TIMEOUT_SECONDS:
                break
            time.sleep(0.05)
            meta = self.store.load_meta(meta.session_id)
        return meta

    def stop(
        self,
        session_id: str,
        budget_tokens: int | None = None,
        top_n: int | None = None,
        terse: bool = False,
        json_mode: bool = False,
        abandon: bool = False,
    ) -> str:
        """Ask the worker to stop, wait for the end it publishes, save the summary, return it.

        Three steps, the first and last under the session's lock:

        1. A pending session -- no worker has claimed it -- is cancelled: none
           may claim it now (a late claim recorded a device loss into a
           session --stop had answered for; fourth review of PR #42). A
           worker at work is signalled only once the process at its recorded
           pid is shown to still be it (``anr_sessions.worker_identity``).
        2. Unlocked, the signalled worker is waited for to publish how it
           ended: ``WORKER_EXIT_TIMEOUT_SECONDS``, and for a worker deciding
           how its stream ended, that decision's own bound.
        3. The answer is read from the meta as it is then. A session that has
           ended -- the worker published ``stopped`` or ``crashed``, or it was
           cancelled or abandoned -- has its summary saved, and is answered
           with it, or refused by the failure it ended with (exit 3 for a
           device that went), or, cancelled, refused (nothing was recorded).
           A worker still at work, or one gone without saying how it ended,
           or a pid that cannot be shown to be the worker or not, is never
           answered with a summary: a missing marker cannot prove no decision
           remains (fourth review of PR #42). --stop refuses (exit 1) and
           saves nothing -- unless ``abandon``, which marks the session
           abandoned (no meta write of the worker's is applied after it; its
           event appends are not locked, and the answers are frozen at what
           the events file held then) and saves the summary of what was read,
           incomplete.

        A session an earlier version recorded (no ``lifecycle``) whose
        worker's pid is recorded is not trusted, whatever its status says,
        while that pid may be the worker: that version's worker takes no lock
        and checks no state -- it wrote a device loss into a session this
        --stop had answered `stopped` at exit 0, and set an abandoned one
        `running` again (review of PR #44). It is identified as a worker of
        this version is, signalled, and waited for until it is gone; while it
        lives, or cannot be told from another process, --stop refuses (exit
        1), --abandon included, which could fence nothing it ignores.

        The summary is marked ``incomplete``, with ``incomplete_reason``,
        whenever the capture may be missing events: the stream broke or ended
        by itself unrestarted, a record was cut by the stop, the stream was
        restarted, ANR reports could not be authenticated, the session was
        abandoned or cancelled.
        """
        signalled = self._ask_to_stop(session_id)
        if signalled is not None:
            self._wait_for_end(session_id, signalled)
        with self.store.transaction(session_id) as meta:
            if _legacy_worker(meta):
                identity, why = worker_identity(meta)
                if identity is not Identity.GONE:
                    _refuse_legacy_worker(meta, identity, why)
                # Gone: nothing writes the session any more. Read again -- its
                # last write may have come after this transaction began.
                meta = self.store.load_meta(session_id)
            if meta.status in LIVE_STATES:
                identity, why = worker_identity(meta)
                if not abandon:
                    _refuse_unconcluded(meta, identity, why, signalled is not None)
                self.store.mark_abandoned(meta, _unconcluded(meta, identity, why))
            summary = self._summary_of(meta)
            # Persist the complete summary BEFORE any capping. Top-N is a
            # display concern; truncating first destroyed every cluster past N
            # on disk, so `--get-details --cluster 4` could never resolve and
            # `--diff` compared two truncated sets.
            self.store.save_summary(meta, summary)
        failure = meta.counters.get("stream_failure")
        if isinstance(failure, dict):
            _refuse_broken_stream(session_id, failure)
        if meta.status == CANCELLED:
            cli.fail(
                f"Session {session_id} had no worker recording yet when --stop came (it was "
                f"still pending), so it is cancelled and holds nothing: no worker may start "
                f"recording into it now.",
                "Run --start again, and --stop the id it prints once it has printed it.",
            )
        if meta.status == ABANDONED:
            print(
                f"warning: session {session_id} is abandoned ({meta.extras.get('abandoned')}); "
                f"its summary is marked incomplete.",
                file=sys.stderr,
            )

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

    def _ask_to_stop(self, session_id: str) -> int | None:
        """--stop's first step, under the lock: cancel a pending session, or signal its worker.

        Returns the pid signalled, None when nothing was. Writes nothing but a
        cancellation: what the worker writes is the worker's.
        """
        with self.store.transaction(session_id) as meta:
            if meta.status == PENDING:
                self.store.mark_cancelled(meta)
                return None
            if meta.status not in LIVE_STATES and not _legacy_worker(meta):
                return None
            identity, _why = worker_identity(meta)
            if identity is not Identity.WORKER:
                return None
            try:
                os.kill(meta.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return None
            return meta.pid

    def _wait_for_end(self, session_id: str, pid: int) -> None:
        """Wait for the signalled worker to publish how it ended, or to be gone without doing so.

        Up to ``WORKER_EXIT_TIMEOUT_SECONDS``: a lookup under way, the stop
        drain, a margin -- and an earlier version's worker until it is gone,
        to the same bound. A worker deciding how its stream ended is waited for
        to that decision's own bound (``DECIDING_TIMEOUT_SECONDS`` from when
        it began): answering before it saved a summary at exit 0 while the
        worker went on to find the device gone (second review of PR #42).
        """
        deadline = time.monotonic() + WORKER_EXIT_TIMEOUT_SECONDS
        while True:
            meta = self.store.load_meta(session_id)
            # An earlier version's worker says nothing of its end: it is
            # waited for until it is gone, whatever its meta says.
            ended = meta.status not in LIVE_STATES and not _legacy_worker(meta)
            if ended or not _alive(pid):
                return
            limit = deadline
            since = _deciding_since(meta)
            if since is not None:
                limit = max(
                    limit, time.monotonic() + since + DECIDING_TIMEOUT_SECONDS - time.time()
                )
            if time.monotonic() >= limit:
                return
            time.sleep(0.05)

    def _summary_of(self, meta: SessionMeta) -> SessionSummary:
        """The summary of a session that has ended, from its events and its worker's counters."""
        session_id = meta.session_id
        line_counters = meta.counters
        summary = self.store.build_summary(
            session_id,
            matched_lines=line_counters.get("matched", 0),
            total_lines=line_counters.get("total", 0),
            dropped_below_threshold=line_counters.get("dropped", 0),
        )
        # What the worker could not see, carried into the summary itself: ANR
        # reports it could not authenticate, pids it could not name, records a
        # stop cut, restarts' gaps, ANRs --package could not attribute, the
        # jank/StrictMode it dropped for want of a name, and the record the
        # session started on, which may predate it.
        unverified = int(line_counters.get("unverified_anr_lines", 0))
        unnamed = line_counters.get("unnamed_pids", [])
        failure = line_counters.get("stream_failure")
        cut = int(line_counters.get("cut_records", 0))
        restarts = int(line_counters.get("stream_restarts", 0))
        tail = line_counters.get("ambiguous_tail", [])
        dropped = line_counters.get("unnamed_dropped")
        if isinstance(dropped, dict):
            unnamed_dropped = int(dropped.get("records", 0))
            dropped_pids = sorted(int(pid) for pid in dropped.get("pids", []))
        else:
            # Not counted by the worker that wrote these counters -- an
            # earlier version's, or none wrote any. Under --package that is
            # unknown, not zero (review of PR #42); without it nothing is
            # ever dropped so. A meta that does not say is unknown too.
            unnamed_dropped = 0 if _unfiltered(meta.args) else None
            dropped_pids = []
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
            unnamed_dropped=unnamed_dropped,
            unnamed_dropped_pids=dropped_pids,
        )
        # Every way this capture can be missing events, so --diff concludes
        # nothing from what is absent (Codex review of PR #34: a broken or
        # unauthenticated capture was compared as complete -- "improvement:
        # 1 resolved"). A record cut by the stop and a restarted stream's gap
        # are losses too, and were saved complete (wave 4): `-T 1` replays
        # only the newest record logged while no stream was up.
        cut_plural = cut != 1
        # How the stream failed, if it did: "broke", "ended by itself", "could not start".
        failed = failure.get("what", "broke") if isinstance(failure, dict) else None
        reasons = [
            reason
            for reason, applies in (
                (
                    "no worker recorded into it: it was cancelled before its worker started, "
                    "so nothing was captured",
                    meta.status == CANCELLED,
                ),
                (
                    f"abandoned with --abandon ({meta.extras.get('abandoned')}), so what its "
                    f"worker read after that, and how its stream ended, are not in here",
                    meta.status == ABANDONED,
                ),
                (
                    "the worker gave up on the stream before --stop (crashed), so nothing "
                    "after that is in here",
                    meta.status == CRASHED and not isinstance(failure, dict),
                ),
                (f"the stream {failed}", failed is not None),
                (
                    "the stream ended by itself before the stop and was not restarted, so "
                    "what was logged after it ended is missing",
                    bool(line_counters.get("ended_unrestarted")),
                ),
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
        # A --stop repeated on a session keeps what the first saved as
        # missing: nothing since brings back what was not written (third
        # review of PR #42) -- and a summary an earlier version saved may say
        # more than these counters do.
        earlier = self.store.load_summary(session_id)
        if earlier is not None and earlier.incomplete and earlier.incomplete_reason:
            reasons = [
                earlier.incomplete_reason,
                *(reason for reason in reasons if reason not in earlier.incomplete_reason),
            ]
        if reasons:
            summary = replace(summary, incomplete=True, incomplete_reason="; ".join(reasons))
        return summary

    def get_details(
        self,
        session_id: str,
        cluster: int | None = None,
        raw: bool = False,
        json_mode: bool = False,
    ) -> str:
        """Drill into a stored session. ``cluster`` is 1-indexed for human use.

        Read under the session's lock, so the summary and the events it is
        drilled into are one --stop's, never one half-way through another's.
        A session an earlier version's worker may still write is refused, as
        --stop refuses it (``_refuse_reading_a_live_legacy_session``). So is
        one with no summary whose worker is gone without having published how
        its capture ended (``_unpublished_end``), with --stop's answer for it:
        it used to be sent to `--stop <id> first`, which refuses it.

        Raises:
            SessionError: The session is unknown, has no summary yet, or the
                requested cluster index does not exist. Each message names what
                to run instead.
            ValueError: ``session_id`` is not a well-formed session id. Raised
                by the store, which owns that grammar; the CLI rejects it as a
                usage error before reaching here.
        """
        try:
            with self.store.transaction(session_id) as meta:
                _refuse_reading_a_live_legacy_session(meta, "--get-details")
                unpublished = _unpublished_end(meta)
                if unpublished is not None and self._load_summary(session_id) is None:
                    _refuse_details_of_an_unpublished_end(meta, unpublished)
                return self._details(session_id, cluster, raw, json_mode)
        except FileNotFoundError as error:
            raise SessionError(
                f"Unknown session: {session_id}. Run --list-sessions to see the "
                f"sessions this host has stored."
            ) from error

    def _details(self, session_id: str, cluster: int | None, raw: bool, json_mode: bool) -> str:
        summary = self._load_summary(session_id)
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
        """Every stored session, newest first.

        A session an earlier version's worker may still write is listed as
        ``provisional`` (with ``provisional_reason``; a mark in text): what
        its meta says may still change. The other modes refuse it (review of
        PR #44); a listing of every session cannot.

        A session whose meta says its worker is at work (``running`` /
        ``deciding``) while that worker is gone -- by --stop's test,
        ``worker_identity``, not pid liveness -- never published how its
        capture ended: killed, or out of time for its session's lock. Its
        persisted ``status`` is kept, and qualified: ``unpublished_end`` true,
        with ``unpublished_end_reason``; in text its status reads ``gone``,
        and the line says the worker is gone and its capture end unpublished.
        It was listed ``running``, a worker --stop refuses as gone.
        """
        metas = self.store.list_sessions()
        unpublished: dict[str, str] = {}
        for index, meta in enumerate(metas):
            why = _unpublished_end(meta)
            if why is None:
                continue
            # Read again once its worker is shown gone: it may have published
            # its end, and exited, after the listing read it. Gone, it writes
            # nothing more.
            with contextlib.suppress(FileNotFoundError, ValueError):
                metas[index] = meta = self.store.load_meta(meta.session_id)
            if meta.status in LIVE_STATES:
                unpublished[meta.session_id] = why
        provisional = {meta.session_id: _provisional(meta) for meta in metas}
        if json_mode:
            return json.dumps(
                [
                    {
                        **m.to_json(),
                        "provisional": provisional[m.session_id] is not None,
                        "provisional_reason": provisional[m.session_id],
                        "unpublished_end": m.session_id in unpublished,
                        "unpublished_end_reason": (
                            _unpublished_reason(unpublished[m.session_id])
                            if m.session_id in unpublished
                            else None
                        ),
                    }
                    for m in metas
                ],
                indent=2,
            )
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
            reason = provisional[meta.session_id]
            provisional_str = f"  provisional: {reason}" if reason else ""
            status = meta.status
            gone_str = ""
            if meta.session_id in unpublished:
                status = "gone"
                gone_str = (
                    f"  worker gone, capture end unpublished: "
                    f"{unpublished[meta.session_id]} (its meta says {meta.status})"
                )
            lines.append(
                f"  {meta.session_id}  {status:8s}  started={meta.started_at}  "
                f"stopped={stopped}{duration_str}{restart_str}{provisional_str}{gone_str}"
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
        summary_a = self._load_summary(session_a)
        summary_b = self._load_summary(session_b)
        for session_id, summary in ((session_a, summary_a), (session_b, summary_b)):
            if summary is not None:
                _refuse_reading_a_live_legacy_session(self.store.load_meta(session_id), "--diff")
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
        cleanly. A stream that ends by itself is asked about as --watch asks
        (``_ended_by_itself``): a device error in adb's words, or from `adb
        get-state`, ends the session as that error; with the device there, it
        is restarted (``ANDROID_EMU_ANR_MAX_RESTARTS``), and once no restart is
        left the session ends as a stream failure. Either way it ends
        ``crashed`` -- never left in a stale ``running`` state -- and ``--stop``
        refuses it (review of PR #42: it answered with the summary, exit 0).

        A stream that is not logd's records, or ends inside one by itself, is
        not restarted (common.logcat's rule for a ``LogFramingError``): the
        device error adb wrote as the stream ended is the answer, and only
        without one is the device asked whether it is still there; the answer
        is kept in the counters (``stream_failure``), the session ends
        crashed, and ``--stop`` refuses -- exit 3 for a device that went, 1
        otherwise. A record cut by our own stop is lost and counted
        (``cut_records``).

        The lifecycle (``common.anr_sessions``): the worker claims the pending
        session -- refused, it exits 2 having written nothing: the session was
        cancelled, is claimed already, or the token is not its own -- and,
        before it exits, publishes how it ended, ``stopped`` or ``crashed``
        with its failure, in one write, on every path: a stop, a device lost,
        restarts exhausted, a stream that broke, an error nothing foresaw --
        unless the session's lock cannot be had within
        ``WORKER_END_LOCK_TIMEOUT_SECONDS``, or the session was deleted, when
        it exits unpublished and --stop finds it gone. A SIGTERM meanwhile
        only asks for a stop. Once a meta write of its own is refused -- the
        session abandoned or deleted -- it stops, writing nothing more to the
        meta (exit 2); its event appends, which take no lock, may land until
        then, and the session's answers ignore them.
        """
        stop_flag = {"value": False}

        def _on_sigterm(_signum, _frame):
            stop_flag["value"] = True

        # Before the claim: once it has claimed, --stop may signal this pid,
        # and a SIGTERM's default action would end the worker before it had
        # published how -- a session --stop could then only refuse.
        signal.signal(signal.SIGTERM, _on_sigterm)
        signal.signal(signal.SIGINT, _on_sigterm)
        pid = os.getpid()
        try:
            meta = self.store.claim_worker(session_id, pid=pid, token=token)
        except (TransitionRefusedError, SessionBusyError, FileNotFoundError, ValueError):
            # Not this worker's to record: cancelled by a --stop, or by a
            # --start that gave up on it; claimed already; not its token.
            # Nothing is written (fourth review of PR #42: a late claim
            # recorded a device loss into a session --stop had answered for).
            return 2
        counters = {
            "total": 0,
            "matched": 0,
            "dropped": 0,
            "stream_restarts": 0,
            "unnamed_pids": [],
            "unverified_anr_lines": 0,
            "cut_records": 0,
            "unattributed_anrs": 0,
            "unnamed_dropped": {"records": 0, "pids": []},
            "ambiguous_tail": [],
        }

        def own(write) -> None:
            """One of this worker's writes; refused, the session is no longer its own."""
            try:
                write(session_id, pid, counters)
            except (TransitionRefusedError, FileNotFoundError) as error:
                raise _Disowned(str(error)) from error

        def checkpoint() -> None:
            """The periodic counter write: the lock taken briefly, and skipped when busy."""
            try:
                self.store.persist_worker_counters(session_id, pid, counters, timeout=0)
            except SessionBusyError:
                return
            except (TransitionRefusedError, FileNotFoundError) as error:
                raise _Disowned(str(error)) from error

        proc: subprocess.Popen | None = None
        disowned = False
        # A stream ended by itself and is to be restarted, which has not happened yet.
        restart_due = False
        try:
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
            # One of each for the whole session. The resolver forgets its owners
            # on every stream (re)start but keeps what it learned of packages; the
            # boundary remembers what was consumed, which a restart's replay is
            # held against.
            process_names = _ProcessNames(
                *_device_lookups(serial, may_ask=lambda: not stop_flag["value"])
            )
            boundary = _CaptureBoundary()
            cmd = AnrWatcher(serial=serial).build_logcat_command()

            def _spawn_logcat(adb_stderr) -> subprocess.Popen:
                # Bytes, unbuffered: the reader takes records straight off the fd,
                # so there is no Python buffer for one to wait in (N16). adb's
                # stderr goes to a file, as --watch's does: nothing reads it until
                # the stream has ended, and then it is the one account of why --
                # a device error above all. It went to /dev/null, so a device
                # error adb wrote as the stream broke was lost, and --stop said
                # what `adb get-state` said a moment later instead (closure review
                # of v0.8.0).
                return subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=adb_stderr,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                )

            with open(events_path, "a", buffering=1) as out_handle:

                def note(event: str, **fields) -> None:
                    record = {"event": event, **fields, "at_ms": int(time.time() * 1000)}
                    out_handle.write(json.dumps(record) + "\n")
                    out_handle.flush()

                def deciding(now: bool) -> None:
                    # In the meta, for --stop: a worker deciding how its
                    # stream ended is waited for, not answered for.
                    if now:
                        counters["deciding"] = int(time.time() * 1000)
                        own(self.store.worker_deciding)
                    else:
                        counters.pop("deciding", None)
                        own(self.store.worker_resumed)

                def stopped() -> bool:
                    # A stop before the restart a stream that ended was due:
                    # what was logged after it ended is missing, and said.
                    if stop_flag["value"] and restart_due:
                        counters["ended_unrestarted"] = True
                    return stop_flag["value"]

                for attempt in range(max_restarts + 1):
                    if stopped():
                        break
                    if attempt > 0:
                        counters["stream_restarts"] = attempt
                        note("stream_restart", attempt=attempt)
                        time.sleep(RESTART_BACKOFF_SECONDS)
                        if stopped():
                            break
                    restart_due = False
                    # One per stream, closed -- and its process reaped -- before
                    # the next starts: what an earlier stream said is not this
                    # one's, and no earlier process outlives its stream (review
                    # of PR #42: every file stayed open, and a process still
                    # alive after its stream ended was dropped for the next).
                    with tempfile.TemporaryFile() as adb_stderr:
                        try:
                            proc = _spawn_logcat(adb_stderr)
                        except FileNotFoundError as error:
                            counters["stream_failure"] = _unstartable(error)
                            proc = None
                        if proc is not None:
                            try:
                                end = self._read_stream_into_events(
                                    proc=proc,
                                    out_handle=out_handle,
                                    stop_flag=stop_flag,
                                    counters=counters,
                                    package=package,
                                    min_frames=min_frames,
                                    session_start_ms=meta.started_at_ms,
                                    process_names=process_names,
                                    boundary=boundary,
                                    checkpoint=checkpoint,
                                )
                                # Read to its end or abandoned on a stop: either
                                # way this stream's pipe is done with.
                                if proc.stdout is not None:
                                    proc.stdout.close()
                                if end is not _ReadEnd.STOPPED:
                                    # Decided on how READING ended, fixed when it
                                    # did -- never on the stop flag, read since: a
                                    # stop that came during the reader's grace
                                    # skipped the decision, and adb's device error
                                    # with it (fourth review of PR #42).
                                    deciding(True)
                                if end is _ReadEnd.BROKEN:
                                    # Not records, or cut by itself: no restart
                                    # can trust it. Whether the device is still
                                    # there decides what --stop says (exit 3, or
                                    # a refusal): as adb said it, else as `adb
                                    # get-state` says it now -- the order --watch
                                    # asks in. Only a stream that ended is waited
                                    # for: one that is not records may still be
                                    # open, and a stop waits for this worker; what
                                    # it has said is read either way.
                                    failure = counters["stream_failure"]
                                    returncode, said = _exit_and_stderr(
                                        proc,
                                        adb_stderr,
                                        timeout=(
                                            STREAM_EXIT_TIMEOUT_SECONDS
                                            if failure["truncated"]
                                            else 0
                                        ),
                                    )
                                    failure["device"] = _device_answer_after(
                                        returncode, said, serial
                                    )
                                    if failure["device"].get("source") != "logcat":
                                        failure["error"] = _adb_exit_added(
                                            failure["error"], returncode, said
                                        )
                                elif end is _ReadEnd.ENDED:
                                    # Ended by itself, whole: a live read ends
                                    # only when it is stopped (_ended_by_itself).
                                    returncode, said = _exit_and_stderr(proc, adb_stderr)
                                    note("stream_died", exit_code=returncode, attempt=attempt)
                                    ended = _ended_by_itself(
                                        returncode, said, serial, attempt, max_restarts
                                    )
                                    if ended is not None:
                                        counters["stream_failure"] = ended
                                    else:
                                        restart_due = True
                            finally:
                                _reap(proc)

                    failure = counters.get("stream_failure")
                    if failure is not None:
                        note("stream_failed", **failure)
                        break
                    if stopped():
                        note("stream_ended")
                        break
                    if counters.get("deciding"):
                        # Decided, and going on: the stream is restarted.
                        deciding(False)

                with contextlib.suppress(OSError):
                    os.fsync(out_handle.fileno())
        except _Disowned:
            # The session is no longer this worker's -- abandoned, or deleted:
            # nothing more is written to it.
            disowned = True
        except BaseException as error:
            # Anything unforeseen ends the session as a failure of its own,
            # published before the worker exits: it left the session
            # `running` with nothing said, so --start and --stop read a worker
            # that had died as one still at work (third review of PR #42).
            _record_worker_error(counters, error)
            raise
        finally:
            if proc is not None:
                _reap(proc)
            # Never left saying it is deciding: it no longer is.
            counters.pop("deciding", None)
            if not disowned:
                # The worker's last write: how the session ended, with its
                # counters and any failure, before it exits.
                with contextlib.suppress(
                    TransitionRefusedError, SessionBusyError, FileNotFoundError
                ):
                    self.store.finish_worker(
                        session_id, pid, counters, timeout=WORKER_END_LOCK_TIMEOUT_SECONDS
                    )

        return 2 if disowned or counters.get("stream_failure") else 0

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
        checkpoint: Callable[[], None] | None = None,
    ) -> _ReadEnd:
        """Read logd's records until EOF / subprocess death / stop request.

        Returns why reading ended (:class:`_ReadEnd`), fixed the moment it
        did: the stream ended by itself (``ENDED`` -- at its end of file, its
        process gone, or reaching its end while a stop drained it), broke
        (``BROKEN``: ``counters["stream_failure"]``), or a stop came while it
        was open (``STOPPED``). A stop that arrives after the end -- during
        the grace given adb to finish -- does not make an end a stop: the
        worker decided on the stop flag read then, skipped deciding how the
        stream ended, and lost adb's device error (fourth review of PR #42).
        Does not emit ``stream_died`` / ``stream_ended`` markers itself — the
        caller decides which to write.

        ``process_names`` names the process behind a jank or StrictMode record
        (the worker passes one that asks the device who system_server is).
        Without one, nothing is named -- and no ANR report is authenticated,
        so none counts: the assembler asks the resolver who system_server is.
        ``boundary`` skips what a restarted stream replays of records the
        session already read, and marks the session's first record -- `-T
        1`'s replay, which may predate it -- whose events go to
        ``counters["ambiguous_tail"]``, not the events file (the worker passes
        the session's); without one, every record is read and counted.
        ``checkpoint`` is called every ``COUNTERS_PERSIST_SECONDS`` the stream
        is quiet (the worker persists its counters).

        Under ``package``, an ANR of a window -- WindowManager's report alone,
        which names no package -- is dropped and counted
        (``unattributed_anrs``), never dropped unsaid; so is a jank or
        StrictMode record whose process the stream never named, at or above
        the frame threshold (``unnamed_dropped``, with its pid).

        A ``LogFramingError`` ends the read (common.logcat's rule): cut by our
        own stop, the lost record is counted (``cut_records``); otherwise the
        failure is put in ``counters["stream_failure"]`` for the caller.
        """
        if proc.stdout is None:
            return _ReadEnd.ENDED
        # Declared, not only grown: --stop reads a missing count as one this
        # worker did not keep (an earlier version's), not as zero.
        counters.setdefault("unnamed_dropped", {"records": 0, "pids": []})
        stream = _FdRecords(proc.stdout)
        names = process_names or _ProcessNames()
        # A new stream: any pid may have changed hands while there was none,
        # and `-T 1` does not replay the new owner's Start proc record.
        names.reset()
        assembler = AnrRecordAssembler(names.system_server_pid, subject_owner=names.owns)
        if boundary is not None:
            boundary.new_stream()
        broken = False

        def record(raw_events: list[dict], *, just_read: bool = False) -> None:
            for raw_event in raw_events:
                # Both are logged from inside the app, so the record's pid is
                # the app's own -- and neither says which app that is. Named
                # only as the record is read, by what the stream said before it.
                if raw_event["kind"] in PID_NAMED_KINDS:
                    raw_event["app"] = names.name_of(raw_event["pid"]) if just_read else None
                frames = raw_event.get("frames")
                # Only Choreographer jank is frame-thresholded; ANRs (frames=None) pass.
                below = frames is not None and frames < min_frames
                if package and not matches_package(raw_event, package):
                    if _unattributed(raw_event):
                        counters["unattributed_anrs"] = counters.get("unattributed_anrs", 0) + 1
                    elif (
                        raw_event["kind"] in PID_NAMED_KINDS and not raw_event["app"] and not below
                    ):
                        # No `Start proc` named its process, so it may be the
                        # package's: dropped, and counted, so --diff concludes
                        # nothing from its absence (closure review of v0.8.0).
                        # One below the threshold would have been dropped anyway.
                        _count_unnamed_drop(counters, raw_event["pid"])
                    continue
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
            nonlocal broken
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
                    broken = True
                return None

        def ended() -> _ReadEnd:
            return _ReadEnd.BROKEN if broken else _ReadEnd.ENDED

        try:
            last_fsync = time.time()
            last_checkpoint = time.monotonic()
            last_record_at = time.monotonic()
            while not stop_flag["value"]:
                records = read(STREAM_POLL_SECONDS)
                if records is None:
                    # Why reading ended is fixed now, before adb's grace: a
                    # stop during it must not turn an end into a stop.
                    end = ended()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=STREAM_END_GRACE_SECONDS)
                    return end
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
                if (
                    checkpoint is not None
                    and time.monotonic() - last_checkpoint >= COUNTERS_PERSIST_SECONDS
                ):
                    checkpoint()
                    last_checkpoint = time.monotonic()
                if proc.poll() is not None:
                    return _ReadEnd.ENDED

            # Stopped. Whatever the stream had already delivered is part of the
            # session: read it before closing, bounded, since logcat keeps writing.
            deadline = time.monotonic() + STOP_DRAIN_SECONDS
            while time.monotonic() < deadline:
                records = read(0)
                if records is None:
                    # Its end was there to read: the stream ended by itself,
                    # whatever the stop -- the end may have been waiting in the
                    # pipe since before it came.
                    return ended()
                if not records:
                    break
                take(records)
            if stream.partial:
                # The rest of a record was still on its way when we stopped an
                # open stream, whose end we never read: lost, and counted.
                counters["cut_records"] = counters.get("cut_records", 0) + 1
            return _ReadEnd.STOPPED
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

    def _dump_raw_events(self, session_id: str) -> str:
        """events.jsonl as the session's answers read it: an abandoned one's up to its fence."""
        return self.store.read_raw_events(session_id)

    def _load_summary(self, session_id: str) -> SessionSummary | None:
        """The stored summary, its unrecorded drop count read as what it can be.

        A summary saved before drops were counted says nothing of them
        (``unnamed_dropped`` None: unknown). A session that did not filter by
        package dropped none, so for it that is 0; under --package it stays
        unknown, and --diff concludes nothing about jank from it.
        """
        summary = self.store.load_summary(session_id)
        if summary is None or summary.unnamed_dropped is not None:
            return summary
        try:
            unfiltered = _unfiltered(self.store.load_meta(session_id).args)
        except (FileNotFoundError, ValueError):
            unfiltered = False
        return replace(summary, unnamed_dropped=0) if unfiltered else summary

    def _resolve_serial(self, serial: str | None) -> str | None:
        # Not re-wrapped: a bare RuntimeError lost DeviceNotFoundError, so an
        # unavailable device exited 1 here and 3 everywhere else.
        return resolve_device_identifier(serial)


def _unattributed(event: dict) -> bool:
    """An ANR filed under no package for want of evidence: of a window, or of a process."""
    return event["kind"] == "anr" and (
        event.get("window") is not None or event.get("process_name") is not None
    )


def _count_unnamed_drop(counters: dict, pid: int) -> None:
    """Count one jank/StrictMode record --package dropped for want of a name, and its pid."""
    dropped = counters.setdefault("unnamed_dropped", {"records": 0, "pids": []})
    dropped["records"] += 1
    if pid not in dropped["pids"]:
        dropped["pids"] = sorted([*dropped["pids"], pid])


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


def _record_worker_error(counters: dict, error: BaseException) -> None:
    """An error nothing foresaw, kept as the session's failure -- beside one already there.

    After a stream failure it is added to that failure's words: ``setdefault``
    kept the stream's failure and dropped the error, so nothing said the
    worker had then failed too (fourth review of PR #42).
    """
    failure = counters.get("stream_failure")
    if isinstance(failure, dict):
        failure["error"] = (
            f"{failure.get('error', 'the stream failed')}; then the worker failed: "
            f"{type(error).__name__}: {error}"
        )
    else:
        counters["stream_failure"] = _worker_failed(error)


def _deciding_since(meta: SessionMeta) -> float | None:
    """When the worker began deciding how its stream ended (epoch s), if it is deciding.

    Read from its counters, where a worker of v0.8.0 main also put it (with
    its status left ``running``).
    """
    deciding = meta.counters.get("deciding")
    return deciding / 1000 if isinstance(deciding, int | float) else None


def _legacy_worker(meta: SessionMeta) -> bool:
    """Whether a session an earlier version recorded names a worker pid that may still be it.

    That version's worker (no ``lifecycle`` in the meta it wrote) takes no
    lock and checks no state, and its --stop could save ``stopped`` while it
    ran on, its pid kept: its status is no promise that nothing more is
    written (review of PR #44).
    """
    return meta.lifecycle is None and bool(meta.pid)


def _legacy_writer(meta: SessionMeta) -> tuple[Identity, str] | None:
    """For a session an earlier version's worker may still write: that pid's identity, and why.

    None when the session is not an earlier version's, names no pid, or the
    pid is shown not to be the worker (it has gone): nothing more will change it.
    """
    if not _legacy_worker(meta):
        return None
    identity, why = worker_identity(meta)
    return None if identity is Identity.GONE else (identity, why)


def _provisional(meta: SessionMeta) -> str | None:
    """Why --list-sessions marks a session provisional, or None."""
    held = _legacy_writer(meta)
    if held is None:
        return None
    identity, why = held
    if identity is Identity.WORKER:
        return f"an earlier version's worker, pid {meta.pid}, is still running and may change it"
    return f"pid {meta.pid} may be an earlier version's worker, still writing it ({why})"


def _unpublished_end(meta: SessionMeta) -> str | None:
    """Why a session's worker reads as gone without having published its capture's end, or None.

    Its meta says a worker is at work (``LIVE_STATES``), and the process at
    the recorded pid is shown not to be that worker (``worker_identity``: the
    token on its command line and the start time recorded at its claim, not
    pid liveness). A worker publishes its end before it exits -- unless it is
    killed, or cannot take the session's lock in
    ``WORKER_END_LOCK_TIMEOUT_SECONDS`` -- so nothing will now. --stop refuses
    such a session (``_refuse_unconcluded``); its readers say the same.
    """
    if meta.status not in LIVE_STATES:
        return None
    identity, why = worker_identity(meta)
    if identity is not Identity.GONE:
        return None
    return why


def _unpublished_reason(why: str) -> str:
    """``_unpublished_end``'s answer as a clause: the worker gone, and why it reads so."""
    return f"its worker is gone ({why}) and its capture end is unpublished"


def _refuse_details_of_an_unpublished_end(meta: SessionMeta, why: str) -> NoReturn:
    """--get-details' refusal for a session with no summary whose worker is gone unpublished.

    --stop's answer for it (``_refuse_unconcluded``), not a pointer to that
    --stop: `Run --stop <id> first` sent the caller to a refusal.
    """
    session_id = meta.session_id
    cli.fail(
        f"Session {session_id} has no summary: {_unpublished_reason(why)}. The worker was "
        f"killed, or died before it could say how its capture ended, so whether its stream "
        f"failed is unknown, and --stop refuses the session for that reason: stopping it "
        f"saves no summary either.",
        f"--stop {session_id} --abandon saves what was read, marked incomplete and abandoned; "
        f"then --get-details {session_id} reads it. Or --start a new session.",
    )


def _refuse_reading_a_live_legacy_session(meta: SessionMeta, mode: str) -> None:
    """A reader's refusal while an earlier version's worker may still write the session.

    --stop refuses such a session (``_refuse_legacy_worker``); --get-details
    and --diff answered 0 from the same snapshot, and after the old worker
    persisted a device loss --get-details still showed the summary without it
    (second review of PR #44). Refused, not marked provisional: an exit 0 is
    what an agent acts on, and only data nothing can change any more earns it.
    """
    held = _legacy_writer(meta)
    if held is None:
        return
    identity, why = held
    state = (
        f"its worker, pid {meta.pid}, is still running"
        if identity is Identity.WORKER
        else f"pid {meta.pid} may be its worker ({why})"
    )
    cli.fail(
        f"Session {meta.session_id} was recorded by an earlier version, and {state}. That "
        f"version's worker takes none of this version's locks, so what the session holds -- "
        f"`{meta.status}`, and its summary -- may still change, a device loss included: "
        f"{mode} answers nothing from it yet.",
        f"Run --stop {meta.session_id}: it asks that worker to stop, waits for it to end, and "
        f"then answers. If it never ends, end pid {meta.pid} yourself once you know it is the "
        f"worker.",
    )


def _refuse_legacy_worker(meta: SessionMeta, identity: Identity, why: str) -> NoReturn:
    """--stop's refusal while an earlier version's worker may still write its session: no summary."""
    session_id = meta.session_id
    if identity is Identity.WORKER:
        state = (
            f"its worker, pid {meta.pid}, is still running (it was asked to stop, and had not "
            f"ended {WORKER_EXIT_TIMEOUT_SECONDS:.1f}s later)"
        )
    else:
        state = f"pid {meta.pid} may be its worker: {why}"
    cli.fail(
        f"Session {session_id} was recorded by an earlier version, and {state}. That version's "
        f"worker takes none of this version's locks, so whatever the session says now -- "
        f"`{meta.status}` -- it may still change, a device loss included: nothing is concluded, "
        f"no summary is saved, and --abandon cannot fence it.",
        f"Run --stop {session_id} again once the worker has ended; if it never does, end pid "
        f"{meta.pid} yourself once you know it is the worker, then --stop {session_id}. If "
        f"`ps` cannot say what pid {meta.pid} is and it stays, nothing here can tell: restore "
        f"`ps`, or delete the session's directory once you have established yourself that the "
        f"worker has ended.",
    )


def _unconcluded(meta: SessionMeta, identity: Identity, why: str) -> str:
    """Why --stop could not conclude a session whose worker has not said how it ended."""
    if identity is Identity.WORKER:
        return f"its worker, pid {meta.pid}, had not finished"
    if identity is Identity.UNRESOLVED:
        return f"its worker could not be identified: {why}"
    return f"its worker was gone without saying how it ended: {why}"


def _refuse_unconcluded(
    meta: SessionMeta, identity: Identity, why: str, signalled: bool
) -> NoReturn:
    """--stop's refusal for a session whose worker has not said how it ended: no summary.

    A summary is an answer only once the worker has published its end, or
    the session ended otherwise; a missing marker cannot prove no decision
    remains (fourth review of PR #42: it saved one at exit 0, and the worker
    then persisted a device loss). Exit 1, nothing saved; ``--abandon`` is
    the way out when it never will.
    """
    session_id = meta.session_id
    abandon = (
        f"--stop {session_id} --abandon saves what was read, marked incomplete and abandoned "
        f"(no meta write of the worker's is applied after that; events it still appends "
        f"are ignored)"
    )
    if identity is Identity.WORKER:
        asked = (
            f"{WORKER_EXIT_TIMEOUT_SECONDS:.1f}s after it was asked to stop"
            if signalled
            else "and was not asked to stop (it could not be shown to be the worker then)"
        )
        deciding = ", still deciding how its logcat stream ended" if meta.status == DECIDING else ""
        cli.fail(
            f"The worker for {session_id} (pid {meta.pid}) had not finished {asked}"
            f"{deciding}, so nothing is concluded from this session yet, and no summary is "
            f"saved.",
            f"Run --stop {session_id} again: it answers with what the worker publishes once "
            f"it has finished. If it never finishes, {abandon}.",
        )
    if identity is Identity.UNRESOLVED:
        cli.fail(
            f"Could not stop session {session_id}'s worker: {why}. It may be the worker, "
            f"still recording, so nothing was signalled, and no summary is saved.",
            f"Run --stop {session_id} again; if it repeats, check that `ps` works on this "
            f"host. If pid {meta.pid} is not the worker, {abandon}.",
        )
    earlier = (
        " (an earlier version recorded it, whose worker never said how a session ended)"
        if meta.lifecycle is None
        else ""
    )
    cli.fail(
        f"Session {session_id}'s worker is gone ({why}) without having said how its capture "
        f"ended{earlier}: it was killed, or died before it could, so whether its stream "
        f"failed is unknown, and no summary is saved.",
        f"{abandon[:1].upper()}{abandon[1:]}; or --start a new session.",
    )


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


def _refuse_broken_stream(
    session_id: str, failure: dict, *, summary_saved: bool = True
) -> NoReturn:
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
    # "broke" (not logd's records), "ended by itself", or "could not start";
    # a failure persisted before the field was kept broke.
    what = failure.get("what", "broke")
    saved = (
        f"The summary of what was read before the break is saved -- read it with "
        f"--get-details {session_id}."
        if summary_saved
        else f"No summary is saved yet: run --stop {session_id} once its worker has ended."
    )
    if isinstance(device, dict) and device.get("error_class") in _DEVICE_ERRORS:
        said = device.get("message") or device["error_class"]
        remedy = device.get("remedy") or "Run `adb devices` to see what is attached."
        when = (
            "as it ended, `adb logcat` said"
            if device.get("source") == "logcat"
            else "asked then, adb said"
        )
        raise _DEVICE_ERRORS[device["error_class"]](
            f"Session {session_id}'s logcat stream {what} ({error}); {when}: {said}",
            remedy=f"{remedy} {saved}",
        )
    how = "was not logd's records to its end" if what == "broke" else what
    cli.fail(
        f"Session {session_id}'s logcat stream {how} ({error}); the session stopped there, "
        f"so it is incomplete. The device, asked then: {_availability_text(device)}.",
        f"Read what was captured with --get-details {session_id}, and record the scenario "
        f"again; if it repeats, restart adb (`adb kill-server && adb start-server`).",
    )


def _availability_text(device) -> str:
    """A persisted availability answer, in words: the worker's dict, or an older string."""
    if isinstance(device, dict):
        state = str(device.get("state", "unknown"))
        return f"{state}: {device['message']}" if device.get("message") else state
    return str(device or "unknown")


def _exit_and_stderr(
    process, adb_stderr, timeout: float = STREAM_EXIT_TIMEOUT_SECONDS
) -> tuple[int | None, str]:
    """``(adb's exit status, what it has said on stderr)``, waiting ``timeout`` at most.

    The status is None when adb has not exited by then. What it has said is
    read either way: the file is there, and an adb still exiting has already
    written its error -- read only after an exit, it was lost (review of PR
    #42). Read with ``pread``, which leaves the file's offset -- shared with
    adb, still writing at it -- where it is: its first and its last
    ``STDERR_KEPT_BYTES``, in whole lines. Its last alone lost an error adb
    wrote first and then chattered past (second review of PR #42).

    What lies between the two is scanned line by line as it is read, and
    only adb's own lines in it are kept (``_adb_own_lines``, up to a
    ``STDERR_KEPT_BYTES`` budget of distinct lines), placed between head and
    tail: the device-error classification reads what this returns. A device
    error there is typed unless more than that budget of distinct
    adb-prefixed lines, or a single line over ``STDERR_KEPT_BYTES``, precedes
    it. One adb wrote only there was never seen -- `adb
    get-state` then answered `device`, and the read was refused for how adb
    exited, exit 1 where adb had said why (v0.9.0 planning adjudication).
    """
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        returncode = None
    fd = adb_stderr.fileno()
    size = os.fstat(fd).st_size
    if size <= 2 * STDERR_KEPT_BYTES:
        said = os.pread(fd, size, 0)
    else:
        head, newline, _cut = os.pread(fd, STDERR_KEPT_BYTES, 0).rpartition(b"\n")
        middle_start = len(head) + 1 if newline else 0
        _cut, newline, tail = os.pread(fd, STDERR_KEPT_BYTES, size - STDERR_KEPT_BYTES).partition(
            b"\n"
        )
        middle_end = size - len(tail) if newline else size
        said = head + b"\n" + _adb_own_lines(fd, middle_start, middle_end) + tail
    return returncode, said.decode("utf-8", errors="replace")


def _adb_own_lines(fd: int, start: int, end: int) -> bytes:
    """adb's own lines among stderr's bytes ``start`` to ``end``, each with its newline.

    Read a block at a time with ``pread``; every line is looked at as it
    arrives and dropped unless it starts as adb's own do (``adb: ``,
    ``error: ``) -- the only lines ``adb_exec._classify`` reads. Bounded
    memory: the line being read is kept to its first ``STDERR_KEPT_BYTES``,
    and the lines kept, each distinct one once, to ``STDERR_KEPT_BYTES``. So
    what is kept classifies as the whole stream does unless more than that
    budget of distinct adb-prefixed lines, or a single line over
    ``STDERR_KEPT_BYTES``, comes before a device error, which is then missed
    (as it was before the middle was scanned); neither is adb's real output.
    """
    kept: list[bytes] = []
    seen: set[bytes] = set()
    budget = STDERR_KEPT_BYTES
    line = b""
    offset = start

    def keep(candidate: bytes) -> None:
        nonlocal budget
        if (
            candidate.startswith(_ADB_OWN_LINE_STARTS)
            and candidate not in seen
            and len(candidate) < budget
        ):
            kept.append(candidate)
            seen.add(candidate)
            budget -= len(candidate) + 1

    while offset < end:
        block = os.pread(fd, min(STDERR_KEPT_BYTES, end - offset), offset)
        if not block:
            break
        offset += len(block)
        *complete, line = (line + block).split(b"\n")
        line = line[:STDERR_KEPT_BYTES]
        for candidate in complete:
            keep(candidate)
    keep(line)
    return b"".join(candidate + b"\n" for candidate in kept)


def _shown(stderr: str) -> str:
    """adb's stderr on one line for a message: its start and its end, when it is long."""
    said = " ".join(stderr.split())
    if len(said) <= 2 * STDERR_SHOWN_CHARS:
        return said
    return f"{said[:STDERR_SHOWN_CHARS]} … {said[-STDERR_SHOWN_CHARS:]}"


def _adb_exit_added(error: str, returncode: int | None, stderr: str) -> str:
    """``error``, saying how adb ended too when that says anything: a failure, or its words."""
    said = _shown(stderr)
    if returncode is None:
        return f"{error}; `adb logcat` had not exited, and had said: {said}" if said else error
    if returncode == 0:
        return error
    return f"{error}; `adb logcat` exited {returncode}: {said or 'nothing on stderr'}"


def _with_adb_exit(
    error: logcat.LogFramingError, returncode: int | None, stderr: str
) -> logcat.LogFramingError:
    """``error``, saying how adb ended too when that says anything (``_adb_exit_added``)."""
    said = _adb_exit_added(str(error), returncode, stderr)
    if said == str(error):
        return error
    return logcat.LogFramingError(said, offset=error.offset, truncated=error.truncated)


def _how_adb_ended(returncode: int | None, stderr: str) -> str:
    """How adb ended a stream that ended by itself, in words: its status, and what it said."""
    said = _shown(stderr)
    if returncode is None:
        ended = f"`adb logcat` gave no exit status within {STREAM_EXIT_TIMEOUT_SECONDS}s"
        return f"{ended}, and had said: {said}" if said else ended
    return f"`adb logcat` exited {returncode}" + (f": {said}" if said else "")


def _ended_by_itself(
    returncode: int | None, stderr: str, serial: str | None, attempt: int, max_restarts: int
) -> dict | None:
    """A session stream that ended by itself, whole: the session's end, or None to restart it.

    A live read ends only when it is stopped (wave 4's rule), so the worker
    asks what --watch asks: adb's own words first, then `adb get-state`. A
    device error either way ends the session as that error -- --stop
    raises it again, exit 3 for a device that went -- where the stream used to
    be restarted until no restart was left and --stop answered with the
    summary, exit 0 (review of PR #42). With the device there it is
    restarted while a restart is left (``ANDROID_EMU_ANR_MAX_RESTARTS``);
    after the last, the session ends as a stream failure --stop refuses
    (exit 1), saying how adb ended it.
    """
    device = _device_answer_after(returncode, stderr, serial)
    gone = "error_class" in device
    if not gone and attempt < max_restarts:
        return None
    how = _how_adb_ended(returncode, stderr)
    times = attempt + 1
    error = (
        f"the stream ended by itself; {how}"
        if gone
        else f"the stream ended by itself {times} time{'s' if times != 1 else ''}, with no "
        f"restart left (ANDROID_EMU_ANR_MAX_RESTARTS={max_restarts}); the last time {how}"
    )
    return {"error": error, "truncated": False, "what": "ended by itself", "device": device}


def _unfiltered(args: dict) -> bool:
    """Whether a session's args say it did not filter by package.

    --start always writes ``package`` -- None without --package -- so args
    without it do not say, and what the session dropped stays unknown.
    """
    return "package" in args and not args["package"]


def _worker_failed(error: Exception) -> dict:
    """The stream failure of a worker that met an error nothing foresaw."""
    return {
        "error": f"the worker failed: {type(error).__name__}: {error}",
        "truncated": False,
        "what": "was abandoned by its worker",
        "device": {"state": "not asked"},
    }


def _alive(pid: int) -> bool:
    """Whether host process ``pid`` exists (one that may not be signalled counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unstartable(error: OSError) -> dict:
    """The stream failure of a worker that could not run `adb logcat` at all."""
    return {
        "error": f"`adb logcat` could not be run: {error}",
        "truncated": False,
        "what": "could not start",
        "device": {"state": "unknown", "message": "adb could not be run"},
    }


def _reap(process) -> None:
    """End a stream's process if it outlived its stream, and collect it -- bounded."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


def _device_answer_after(returncode: int | None, stderr: str, serial: str | None) -> dict:
    """A broken stream's device answer, kept whole for --stop: adb's own words, else get-state.

    The worker's half of --watch's order (``_raise_device_error_said``): a
    device error adb wrote as the stream ended is the answer, kept with
    ``"source": "logcat"``; only without one is the device asked
    (:func:`_device_availability`).
    """
    try:
        _raise_device_error_said(returncode, stderr, serial)
    except DeviceError as error:
        return {**_device_answer(error), "source": "logcat"}
    return _device_availability(serial)


def _device_answer(error: AdbError) -> dict:
    """An adb failure as a persisted availability answer: its state, words, class and remedy."""
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
        return _device_answer(error)
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
    parser.add_argument(
        "--abandon",
        action="store_true",
        help=(
            "With --stop: when the worker has not said how it ended -- still running, "
            "gone, or not identifiable -- save what was read, marked incomplete and "
            "abandoned, instead of refusing"
        ),
    )

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

    if args.abandon and not args.stop:
        parser.error("--abandon goes with --stop SESSION_ID")

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
                abandon=args.abandon,
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
