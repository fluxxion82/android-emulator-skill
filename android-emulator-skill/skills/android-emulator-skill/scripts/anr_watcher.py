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

On API 33 (Android 13), whose logcat holds ``-B`` records back while it
streams and loses them when it is stopped, both live readers -- --watch and
the session worker -- read by dumps instead, as log_monitor does there
(``common/dump_read.py``; ``ANDROID_EMU_LOG_POLL_SECONDS``,
``ANDROID_EMU_LOG_DUMP_CAP_BYTES`` and ``ANDROID_EMU_LOG_STATE_TIMEOUT`` bound
that read). Every other level streams.

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
import base64
import contextlib
import json
import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

# Resolve imports whether run from repo root or scripts/ directory.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from common import cli, dump_read, log_lifecycle, logcat  # noqa: E402
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
    UID_ATTRIBUTED,
    UID_ISOLATED,
    UID_PACKAGE,
    UID_PACKAGE_UIDS_UNKNOWN,
    UID_SDK_SANDBOX,
    UID_UNFILTERED,
    UID_UNKNOWN,
    VERIFICATION_UNKNOWN,
    AnrRecordAssembler,
    SessionSummary,
    attribution_markers,
    build_normalised_event,
    classify_uid,
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
    OPENING,
    PENDING,
    RUNNING,
    STOPPED_WHILE_OPENING,
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
    parse_uid_table,
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
# The claim is the worker's first act: the device is asked nothing before it.
REGISTRATION_TIMEOUT_SECONDS = 3.0
# The bound a worker keeps on opening its read, from its claim (the session is
# ``opening`` meanwhile): the device's level, and on API 33 its boot id and the
# search for the newest record a system process wrote. One deadline for all of
# it, passed down: every question inside -- each dump, the `get-state` after a
# dump or a getprop that timed out, the anchor's write, the failure's own
# question of the device -- gets only what is left, and none is asked once
# nothing is. Past it, the opening can still take a stopped dump's reap
# (KILL_REAP_TIMEOUT), adb's device listing in a refusal naming several devices
# (DEVICE_LIST_TIMEOUT_SECONDS, common.adb_exec), and the write that publishes
# how it ended (PUBLICATION_SECONDS: the write; a lock another command holds
# longer delays it). That is OPENING_OVERRUN_SECONDS, and --start and --stop
# wait for an opening session exactly OPENING_TIMEOUT_SECONDS +
# OPENING_OVERRUN_SECONDS after it began (``_opening_until``).
OPENING_TIMEOUT_SECONDS = dump_read.NEWEST_TIMEOUT
PUBLICATION_SECONDS = 2.0
OPENING_OVERRUN_SECONDS = (
    dump_read.KILL_REAP_TIMEOUT + DEVICE_LIST_TIMEOUT_SECONDS + PUBLICATION_SECONDS
)
# How often --start, waiting on an opening, asks `ps` whether its worker is still there.
OPENING_WORKER_CHECK_SECONDS = 0.5

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
    named, and ``app`` for a jank or StrictMode record: the package, when the
    record's uid is one of its uids (``_attribute_by_uid``, both readers), or
    the name a session verified for the pid from a `Start proc` -- a jank
    record names no package, so without it ``--package`` dropped every jank
    event.

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


def _require_device(serial: str | None, timeout: float = GET_STATE_TIMEOUT_SECONDS) -> None:
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
        AdbError: adb could not be run, or did not answer in time (``timeout``).
    """
    result = run_adb("get-state", serial, timeout=timeout, check=True)
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
    keyed by pid, identity "pid", and its pid listed in the summary
    (``unnamed_pids``). A name is a label. Whose a jank or StrictMode record
    is under ``--package`` is its uid's -- logd's, the writing process's
    own -- against the package's uids (:meth:`package_uids`,
    ``_attribute_by_uid``); the whole uid table (:meth:`shared_uid`) says
    which other packages share them.
    """

    def __init__(
        self,
        system_server_pid: Callable[[], str | None] = lambda: None,
        package_uids: Callable[[str], Iterable[int] | None] = lambda _package: None,
        pid_uid: Callable[[int], int | None] = lambda _pid: None,
        uid_table: Callable[[], dict[str, frozenset[int]] | None] = lambda: None,
    ) -> None:
        """Build a resolver over the device's answers to four questions.

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
            uid_table: Asks the device for every package's uids (``cmd
                package list packages -U``, unfiltered); None when it cannot
                say. Asked at most once for the session, and only when a
                record is first attributed to a package by its uid.
        """
        self._ask_system_server = system_server_pid
        self._ask_uid_table = uid_table
        self._uid_table: dict[str, frozenset[int]] | None = None
        self._uid_table_asked = False
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

    def forget_unanswered(self) -> None:
        """A read that recovered: every question the device did not answer is asked again.

        system_server's pid, a package's uids, the whole uid table, who a pid
        ran as -- each only when the answer was "cannot say". What it answered,
        and every owner learned from the log, is kept: a read made of dumps is
        one continuous read, so no pid changed hands unseen.
        """
        self._reader.forget_unanswered()
        self._answers = {name: uids for name, uids in self._answers.items() if uids is not None}
        self._pid_uids = {pid: uid for pid, uid in self._pid_uids.items() if uid is not None}
        if self._uid_table_asked and self._uid_table is None:
            self._uid_table_asked = False

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

    def package_uids(self, package: str) -> frozenset[int] | None:
        """The package's uids, one per user it is installed for, as the device gave them (once)."""
        uids = self._package_uids(package)
        return None if uids is None else log_lifecycle.uid_set(uids)

    def shared_uid(self, package: str) -> list[str] | None:
        """The other packages the device's whole uid table gives one of ``package``'s uids.

        sharedUserId: their processes run under the same uid, so a record of
        it may be theirs. Empty when none does; None when the table, or the
        package's uids, could not be had.
        """
        if not self._uid_table_asked:
            self._uid_table_asked = True
            self._uid_table = self._ask_uid_table()
        uids = self.package_uids(package)
        if self._uid_table is None or uids is None:
            return None
        return sorted(
            name for name, theirs in self._uid_table.items() if name != package and theirs & uids
        )

    def observe(self, record: logcat.LogRecord) -> log_lifecycle.Sentence | None:
        """Learn -- or forget -- a pid's owner from system_server's sentence about it.

        Returns:
            The sentence system_server wrote, if the record is one.
        """
        _writer, sentence = self._reader.read(record)
        if sentence is None:
            return None
        if sentence.what == log_lifecycle.STARTED:
            self._owners[int(sentence.pid)] = sentence
        else:
            self._owners.pop(int(sentence.pid), None)
        return sentence

    def changed_uid(self, package: str, sentence: log_lifecycle.Sentence | None) -> int | None:
        """The uid ``sentence`` starts a process named ``package`` under, when this read's answer lacks it.

        The package's uids are asked once per read and kept: a reinstall, or
        an install for another user, gives it a uid the answer does not hold,
        and its records then classify as another uid's. system_server's
        ``Start proc <pid>:<package>/<uid>`` under such a uid says so -- or
        that another app named its process after the package, which the
        sentence cannot rule out. Only once the package's uids were asked:
        before that, asking gets the current answer.
        """
        if sentence is None or sentence.what != log_lifecycle.STARTED or sentence.uid is None:
            return None
        if sentence.name != package or self._answers.get(package) is None:
            return None
        return (
            None if sentence.uid in log_lifecycle.uid_set(self._answers[package]) else sentence.uid
        )

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


def _uid_table_lookup(
    serial: str | None, may_ask: Callable[[], bool]
) -> Callable[[], dict[str, frozenset[int]] | None]:
    """The device's whole uid table (``cmd package list packages -U``), as ``_device_lookups`` asks.

    Bounded by ``LOOKUP_TIMEOUT_SECONDS``, asked only while ``may_ask()``
    holds, and failing closed: None when adb fails, the command fails, or it
    lists no package -- a device always has some, so an empty table is no
    answer. Unfiltered, unlike the per-package question, which filters by
    substring and so cannot show another package on the same uid.
    """

    def uid_table() -> dict[str, frozenset[int]] | None:
        if not may_ask():
            return None
        try:
            result = run_adb(
                "shell",
                serial,
                "cmd",
                "package",
                "list",
                "packages",
                "-U",
                timeout=LOOKUP_TIMEOUT_SECONDS,
            )
        except AdbError:
            return None
        table = parse_uid_table(result.stdout) if result.ok else {}
        return table or None

    return uid_table


def _attribute_by_uid(event: dict, package: str, names: _ProcessNames) -> str:
    """Whose a jank or StrictMode event is under ``--package``, by its record's uid.

    The record's uid is logd's -- the writing process's own -- and Choreographer
    and StrictMode write from inside the app, so the event is the package's
    exactly when its uid is one of the package's uids (each user's whole).
    Then ``app`` becomes the package, and ``shared_uid`` lists the other
    packages the whole uid table gives that uid (sharedUserId; None: the
    table could not be read), so a shared uid is never attributed silently.
    Otherwise ``app`` is cleared: shown to be another uid's, or an isolated
    process's, a `Start proc` name for its pid -- a pid reused since -- does
    not make it the package's.

    Returns:
        ``classify_uid``'s verdict: ``UID_PACKAGE``, ``UID_ISOLATED``,
        ``UID_OTHER`` or ``UID_UNKNOWN`` (the package's uids unknown: the
        caller falls back to the name, and counts what it drops).
    """
    verdict = classify_uid(event.get("uid"), names.package_uids(package))
    if verdict == UID_PACKAGE:
        event["app"] = package
        shared = names.shared_uid(package)
        if shared != []:
            event["shared_uid"] = shared
    elif verdict != UID_UNKNOWN:
        event["app"] = None
    return verdict


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

    A session read by dumps (API 33) has none: its cursor already excludes
    what it consumed, and an app may send one datagram twice, so skipping a
    record because it equals one consumed would drop a new one.
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


class _SessionRecords:
    """A session's records -> its events: the one pipeline both of its readers feed.

    The stream (every level but API 33, ``_read_stream_into_events``) and the
    dumps (API 33, ``_read_dumps_into_events``) hand records here in logd's
    order. What happens to each -- the capture boundary, the names
    system_server's sentences give, uid attribution under ``--package``, the
    frame threshold, the events file -- is the same whichever read them.
    """

    def __init__(
        self,
        *,
        out_handle,
        counters: dict,
        package: str | None,
        min_frames: int,
        session_start_ms: int,
        names: _ProcessNames,
        boundary: _CaptureBoundary | None,
        assembler: AnrRecordAssembler,
    ) -> None:
        # Declared, not only grown: --stop reads a missing count as one this
        # worker did not keep (an earlier version's), not as zero.
        counters.setdefault("unnamed_dropped", {"records": 0, "pids": []})
        counters.setdefault("uid_attribution", UID_ATTRIBUTED if package else UID_UNFILTERED)
        counters.setdefault("shared_uid", [])
        counters.setdefault("isolated_unattributed", {"records": 0, "uids": []})
        counters.setdefault("sdk_sandbox_unattributed", {"records": 0, "uids": []})
        counters.setdefault("package_uid_changed", [])
        self._out_handle = out_handle
        self._counters = counters
        self._package = package
        self._min_frames = min_frames
        self._session_start_ms = session_start_ms
        self._names = names
        self._boundary = boundary
        self._assembler = assembler
        # Held reports released from the hold whose event could not be written.
        self._lost = 0

    def record(self, raw_events: list[dict], *, just_read: bool = False) -> None:
        """Count, threshold and write the events the assembler completed: the one way one is written.

        Every event -- completed as a record is read (``feed``), released from
        the hold by quiet (``flush``) or at the read's end (``close``) -- is
        written here and nowhere else (``_record_one``). A held WindowManager
        report leaves the hold before its event is written, so a write that
        fails counts what it had not written: the reports of this batch the
        assembler released from its hold, from the one that failed on, and
        those it still holds, into ``counters["held_unwritten"]``. Then the
        error goes on, and ends the session as any does. An earlier failure
        is not counted twice: the lost are kept, the held recounted (a later
        ``close`` that writes them leaves only the lost). A report released by
        quiet and then unwritable ended a session with zero events and no
        count (Codex, final gate of c832cdf).
        """
        done = 0
        try:
            for raw_event in raw_events:
                self._record_one(raw_event, just_read=just_read)
                done += 1
        except BaseException:
            assembler = self._assembler
            self._lost += sum(1 for event in raw_events[done:] if assembler.was_held(event))
            self._say_unwritten()
            raise

    def _say_unwritten(self) -> None:
        """``held_unwritten``: the held reports lost to a failed write, and those still held."""
        unwritten = self._lost + self._assembler.held
        if unwritten:
            self._counters["held_unwritten"] = unwritten
        else:
            self._counters.pop("held_unwritten", None)

    def _record_one(self, raw_event: dict, *, just_read: bool) -> None:
        """One event: counted, thresholded, and -- if it is kept -- written. Only ``record`` calls it."""
        names, package, counters = self._names, self._package, self._counters
        min_frames, out_handle = self._min_frames, self._out_handle
        session_start_ms = self._session_start_ms
        verdict = None
        # Both are logged from inside the app, so the record's pid and
        # uid are the app process's own -- and neither text says which
        # app that is. The pid is named only as the record is read, by
        # what the stream said before it: a label. Under --package the
        # uid says whose it is.
        if raw_event["kind"] in PID_NAMED_KINDS:
            raw_event["app"] = names.name_of(raw_event["pid"]) if just_read else None
            if package:
                verdict = _attribute_by_uid(raw_event, package, names)
                _note_uid_attribution(counters, raw_event, verdict, package, names)
        frames = raw_event.get("frames")
        # Only Choreographer jank is frame-thresholded; ANRs (frames=None) pass.
        below = frames is not None and frames < min_frames
        if package and not matches_package(raw_event, package):
            if _unattributed(raw_event):
                counters["unattributed_anrs"] = counters.get("unattributed_anrs", 0) + 1
            elif verdict == UID_ISOLATED and not below:
                # An isolated process's uid is no package's: it may be
                # the package's or another app's. Never counted, and
                # listed, so --diff concludes nothing from its absence.
                _count_uncounted(counters, "isolated_unattributed", raw_event["uid"])
            elif verdict == UID_SDK_SANDBOX and not below:
                # The package's SDK sandbox: its SDKs' records, not the
                # app's. Never counted, and listed, likewise.
                _count_uncounted(counters, "sdk_sandbox_unattributed", raw_event["uid"])
            elif verdict == UID_UNKNOWN and not raw_event["app"] and not below:
                # Nothing showed whose it is -- the package's uids
                # unknown, and no `Start proc` named its process -- so
                # it may be the package's: dropped, and counted, so
                # --diff concludes nothing from its absence (closure
                # review of v0.8.0). One below the threshold would
                # have been dropped anyway.
                _count_unnamed_drop(counters, raw_event["pid"])
            return
        if raw_event.get("ambiguous_tail"):
            # Shown in the summary, kept out of every count, cluster
            # and diff: nothing shows it was logged after --start.
            if not below:
                counters.setdefault("ambiguous_tail", []).append(_tail_evidence(raw_event))
            return
        counters["matched"] += 1
        if below:
            counters["dropped"] += 1
            return
        normalised = build_normalised_event(
            raw_event,
            session_start_ms=session_start_ms,
            current_ms=int(time.time() * 1000),
        )
        if normalised is not None:
            out_handle.write(event_to_jsonl(normalised) + "\n")

    def take(self, records: list[logcat.LogRecord]) -> None:
        """Read records in logd's order: admitted, named, assembled into events."""
        boundary, counters, names = self._boundary, self._counters, self._names
        package, assembler, record = self._package, self._assembler, self.record
        for log_record in records:
            verdict = _CaptureBoundary.READ if boundary is None else boundary.admit(log_record)
            if verdict == _CaptureBoundary.REPLAYED:
                continue
            counters["total"] += 1
            sentence = names.observe(log_record)
            changed = names.changed_uid(package, sentence) if package else None
            if changed is not None and changed not in counters["package_uid_changed"]:
                # The package's uids were asked when the session began;
                # what follows under this uid goes uncounted, and is said.
                counters["package_uid_changed"] = [*counters["package_uid_changed"], changed]
            ambiguous = verdict == _CaptureBoundary.AMBIGUOUS
            record(assembler.feed(log_record, ambiguous=ambiguous), just_read=True)

    def close(self) -> None:
        """The read has ended: what the assembler still holds was seen; and what it could not see.

        A WindowManager report nothing answered goes out, and the pids no
        owner was found for and the ANR records set aside unauthenticated are
        kept for the summary to disclose. A held report lives in memory until
        its event is written, through ``record`` as every event is: what it
        cannot write it counts (``counters["held_unwritten"]``) before the
        error goes on, so none is ever dropped unsaid by a worker that ends
        itself.
        """
        self.record(self._assembler.close())
        # Written now, the held are no longer counted: only those lost stay.
        self._say_unwritten()
        self._counters["unnamed_pids"] = self._names.unnamed_pids
        self._counters["unverified_anr_lines"] = (
            self._counters.get("unverified_anr_lines", 0) + self._assembler.unverified
        )


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
        # Jank/StrictMode --package dropped because nothing showed whose they
        # were -- the device did not give the package's uids -- so each may
        # be the package's: said, never a silent "no jank".
        self.unnamed_dropped = 0
        # Jank/StrictMode of isolated processes in the package's users, and
        # their uids: never counted (no package's listing names such a uid),
        # and said.
        self.isolated_unattributed = 0
        self.isolated_uids: list[int] = []
        # The same for the package's own SDK sandbox (its uid + 10000), whose
        # records its SDKs write: never counted, and said.
        self.sdk_sandbox_unattributed = 0
        self.sdk_sandbox_uids: list[int] = []
        # Uids a process named the package started under that the package's
        # uids, asked once at the start, lack: said on stderr as seen.
        self.package_uid_changed: list[int] = []
        # How many jank/StrictMode records --package counted by a uid the
        # whole uid table gives other packages too (sharedUserId), and which
        # (None: the table could not be read): said.
        self.shared_counted = 0
        self.shared_uid: list[str] | None = []
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
        # uid `ps` gives the pid now (_ProcessNames.owns). Under --package a
        # jank or StrictMode record is the package's by its own uid, held
        # against the package's uids, with the whole uid table asked once for
        # who else shares them (_attribute_by_uid).
        # Asked only until the watch is stopped, as the session worker asks:
        # after Ctrl-C a lookup under way finishes, and no other starts --
        # except on API 33, until the read's last poll is over: that poll
        # comes after the Ctrl-C, and reads what was logged since the one
        # before it (_watch_by_dumps).
        self._reading_by_dumps = False
        self._names = _ProcessNames(
            *_device_lookups(serial, may_ask=self._may_ask),
            uid_table=_uid_table_lookup(serial, may_ask=self._may_ask),
        )
        self._assembler = AnrRecordAssembler(
            self._names.system_server_pid, subject_owner=self._names.owns
        )

    def _may_ask(self) -> bool:
        """Whether the device may be asked who a pid is: until the watch stops, or its last read ends."""
        return not self.interrupted or self._reading_by_dumps

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

        On API 33 the read is made of dumps instead (``_watch_by_dumps``): that
        release's logcat holds ``-B`` records back while it streams and loses
        them when it is stopped. The level is asked once; one the device does
        not say is refused, never guessed to stream.

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
        if self._holds_binary_back():
            return self._watch_by_dumps(duration_seconds, package, json_mode)
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
                    self._disclose_jank_attribution()

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

    def _holds_binary_back(self) -> bool:
        """Whether the device's logcat is Android 13's: asked once (``common.dump_read``).

        Raises:
            cli.Failure: The level is unknown, or getprop did not answer and
                the device is still there (exit 1), with ``partial``.
            adb_exec.DeviceError: As getprop raised it, or -- after a timeout
                -- as ``get-state`` then says (exit 3).
        """
        try:
            return dump_read.holds_binary_back(self.serial, "anr_watcher")
        except (dump_read.ReadRefusedError, dump_read.DeviceGoneError) as error:
            self._refuse_read(error)

    def _watch_by_dumps(
        self, duration_seconds: int | None, package: str | None, json_mode: bool
    ) -> bool:
        """--watch on API 33: a series of dumps one poll apart, never a stream.

        That release's logcat holds ``-B`` records back while it streams and
        loses them when it is stopped (recorded: logcat_stream_jank_records,
        the fixture app's jank withheld by a stream stopped just after it, and
        in the window dumped back). So the watch reads as log_monitor does on
        that level (``common.dump_read``): it opens on the newest record a
        system process wrote (a buffer with none gets a marker line, said on
        stderr) and, every ``ANDROID_EMU_LOG_POLL_SECONDS`` (1 s), takes what
        logd delivered after the records it has read -- found by logd's order,
        never by a timestamp or an app's record -- then once more at
        ``--duration`` and at Ctrl-C (that read may still ask the device who a
        pid is, as any poll does). Each poll's records go through the same
        record -> event pipeline as a streamed one (``_handle_record``: the
        assembler, uid attribution under ``--package``); a poll that brings
        none is the read going quiet (``_flush``). An event is printed about
        one poll after it was logged.

        Nothing it reads may predate the watch -- it reads only what follows
        its opening record -- so nothing is ``ambiguous_tail``. What it shows
        is what logd delivered: logd may drop records it prunes while a reader
        is behind, as with any reader on any level.

        Raises:
            cli.Failure: A dump failed, overran its budget or its byte cap, was
                not whole records, or the read lost its place (its record
                gone from the buffer, held twice, past the cap) -- refused,
                with the events already shown under ``partial``. One whose
                device went is also that device error (exit 3).
        """
        if not json_mode:
            print("Watching for ANR/jank events", file=sys.stderr)
            if package:
                print(f"Post-parse filter: {package}", file=sys.stderr)
        stop = threading.Event()

        def interrupt(_signum, _frame) -> None:
            self.interrupted = True
            stop.set()

        # As _register_signal_handler: outside the main thread, no Ctrl-C.
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, interrupt)
        reader = dump_read.DumpRead(self.serial)
        last_record_at = time.monotonic()

        def take(records: list[logcat.LogRecord], _cursor: logcat.Cursor) -> None:
            nonlocal last_record_at
            if not records:
                # Quiet for a poll: a held report nothing answered may be due.
                self._flush(package, json_mode, time.monotonic() - last_record_at)
                return
            last_record_at = time.monotonic()
            for record in records:
                self._handle_record(record, package, json_mode)

        failure: dump_read.ReadRefusedError | dump_read.DeviceGoneError | None = None
        # Lookups go on through the last read, which comes after a Ctrl-C.
        self._reading_by_dumps = True
        try:
            try:
                opening = (
                    min(dump_read.NEWEST_TIMEOUT, duration_seconds)
                    if duration_seconds
                    else dump_read.NEWEST_TIMEOUT
                )
                cursor = reader.open(opening)
                deadline = time.monotonic() + duration_seconds if duration_seconds else None
                dump_read.poll(reader, cursor, stop=stop, deadline=deadline, on_read=take)
            except (dump_read.ReadRefusedError, dump_read.DeviceGoneError) as error:
                failure = error
        finally:
            self._reading_by_dumps = False
            # Whatever is still held when the read ends -- or breaks -- was
            # seen, and goes out before anything else is said.
            self._close(package, json_mode)
            self._disclose_unverified()
            self._disclose_unattributed()
            self._disclose_jank_attribution()
        if failure is not None:
            self._refuse_read(failure)
        return True

    def _refuse_read(
        self, error: dump_read.ReadRefusedError | dump_read.DeviceGoneError
    ) -> NoReturn:
        """Refuse a dump-made watch, the events it had shown under ``partial`` (exit 3 or 1)."""
        shown = len(self.events)
        dump_read.refuse(
            error,
            f" The watch had shown {shown} event{'s' if shown != 1 else ''}: under `partial` "
            "(--json), not the whole watch.",
            {"partial": self.get_json_output()},
        )

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
        self._disclose_jank_attribution()
        return True

    def get_summary(self) -> str:
        """Return token-efficient summary of captured events."""
        total = len(self.events)
        if total == 0:
            dropped = (
                (
                    f" --package dropped {self.unattributed_anrs} ANR(s) naming no verified "
                    f"package."
                    if self.unattributed_anrs
                    else ""
                )
                + (
                    f" --package dropped {self.unnamed_dropped} jank/StrictMode record(s) whose "
                    f"uid it could not hold against the package's (its uids unknown)."
                    if self.unnamed_dropped
                    else ""
                )
                + (
                    f" --package did not count {self.isolated_unattributed} jank/StrictMode "
                    f"record(s) from isolated processes."
                    if self.isolated_unattributed
                    else ""
                )
                + (
                    f" --package did not count {self.sdk_sandbox_unattributed} jank/StrictMode "
                    f"record(s) from the package's SDK sandbox."
                    if self.sdk_sandbox_unattributed
                    else ""
                )
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
            f"{self.shared_counted} by a shared uid" if self.shared_counted else "",
        ]
        including = "; ".join(q for q in qualifiers if q)
        including = f", including {including}" if including else ""
        isolated = (
            f" Not counted: {self.isolated_unattributed} jank/StrictMode record(s) from "
            f"isolated processes."
            if self.isolated_unattributed
            else ""
        ) + (
            f" Not counted: {self.sdk_sandbox_unattributed} jank/StrictMode record(s) from "
            f"the package's SDK sandbox."
            if self.sdk_sandbox_unattributed
            else ""
        )
        return f"ANR/jank events: {total}{including} | Tags: {top_str}{isolated}"

    def get_json_output(self) -> dict:
        """Return full results as a JSON-serialisable dict."""
        return {
            "events": self.events,
            "ambiguous_tail": self.ambiguous,
            "summary": {
                "total": len(self.events),
                "tags": list({e.get("process") for e in self.events}),
                "isolated_unattributed": {
                    "records": self.isolated_unattributed,
                    "uids": self.isolated_uids,
                },
                "sdk_sandbox_unattributed": {
                    "records": self.sdk_sandbox_unattributed,
                    "uids": self.sdk_sandbox_uids,
                },
                "package_uid_changed": self.package_uid_changed,
                "shared_uid": self.shared_uid,
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

    def _disclose_jank_attribution(self) -> None:
        """Say what --package could not attribute of jank/StrictMode, or attributed with a doubt."""
        if self.unnamed_dropped:
            plural = "s" if self.unnamed_dropped != 1 else ""
            print(
                f"warning: --package dropped {self.unnamed_dropped} jank/StrictMode record{plural}: "
                f"the device did not give the package's uids, so no record's uid could show "
                f"whose {'they are' if plural else 'it is'} -- {'they' if plural else 'it'} may be "
                f"the package's.",
                file=sys.stderr,
            )
        if self.isolated_unattributed:
            count = self.isolated_unattributed
            plural = "s" if count != 1 else ""
            uids = ", ".join(str(uid) for uid in self.isolated_uids)
            print(
                f"warning: --package did not count {count} jank/StrictMode record{plural} from "
                f"isolated processes (uid {uids}): an isolated process runs under a uid no "
                f"package's listing names, so {'they' if plural else 'it'} may be the package's "
                f"or another app's.",
                file=sys.stderr,
            )
        if self.sdk_sandbox_unattributed:
            count = self.sdk_sandbox_unattributed
            plural = "s" if count != 1 else ""
            uids = ", ".join(str(uid) for uid in self.sdk_sandbox_uids)
            print(
                f"warning: --package did not count {count} jank/StrictMode record{plural} from "
                f"the package's SDK sandbox (uid {uids}): the sandbox runs the app's SDKs under a "
                f"uid of its own (the app's + 10000), so {'they are' if plural else 'it is'} the "
                f"SDKs' records, not the app's own.",
                file=sys.stderr,
            )
        if self.shared_counted:
            count = self.shared_counted
            plural = "s" if count != 1 else ""
            pronoun = "they" if plural else "it"
            whose = (
                f"whether another package shares it is unknown (the device's uid table could not "
                f"be read): {pronoun} may be another package's"
                if self.shared_uid is None
                else f"the device gives it to {', '.join(self.shared_uid)} too (sharedUserId): "
                f"{pronoun} may be theirs"
            )
            print(
                f"warning: --package counted {count} jank/StrictMode record{plural} by the "
                f"package's uid, and {whose}.",
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
        sentence = self._names.observe(record)
        changed = self._names.changed_uid(package, sentence) if package else None
        if changed is not None and changed not in self.package_uid_changed:
            self.package_uid_changed.append(changed)
            # Said as it is seen: what follows under that uid goes uncounted.
            print(
                f"warning: system_server started a process named {package} under uid {changed}, "
                f"which the package's uids as this read asked them at its start do not include: "
                f"reinstalled or installed for another user since (or another app named its "
                f"process after it), so its jank/StrictMode under that uid goes uncounted -- "
                f"start a new read.",
                file=sys.stderr,
            )
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
        verdict = None
        if package and event["kind"] in PID_NAMED_KINDS:
            # Written by the app's own process: its uid says whose it is.
            verdict = _attribute_by_uid(event, package, self._names)
            if verdict == UID_PACKAGE and event.get("shared_uid", []) != []:
                self.shared_counted += 1
                self.shared_uid = event["shared_uid"]
        if package and not matches_package(event, package):
            if _unattributed(event):
                self.unattributed_anrs += 1
            elif verdict == UID_ISOLATED:
                self.isolated_unattributed += 1
                if event["uid"] not in self.isolated_uids:
                    self.isolated_uids = sorted([*self.isolated_uids, event["uid"]])
            elif verdict == UID_SDK_SANDBOX:
                self.sdk_sandbox_unattributed += 1
                if event["uid"] not in self.sdk_sandbox_uids:
                    self.sdk_sandbox_uids = sorted([*self.sdk_sandbox_uids, event["uid"]])
            elif verdict == UID_UNKNOWN:
                self.unnamed_dropped += 1
            # UID_OTHER: shown to be another uid's -- not the package's, as
            # another package's ANR is not.
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
        # Counted for the package by a uid others may run under too.
        if "shared_uid" in event:
            shared = event["shared_uid"]
            marks += (
                " [shared uid unknown: the device's uid table could not be read]"
                if shared is None
                else f" [uid shared with {', '.join(shared)}]"
            )
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

        Succeeds only on a worker that claimed the session and opened its
        read: ``running``. A worker still ``opening`` is waited for exactly to
        the bound it published with its claim (``_opening_until``); past it,
        the session is stopped -- the worker cancels its opening and publishes
        ``cancelled`` -- and --start refuses (exit 1), naming the opening and
        how long it ran. A worker gone while it opened is answered as --stop
        answers a worker gone without publishing its end. A failure is
        published only with the session's end, in one write
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
        if settled.status == OPENING:
            settled = self._give_up_on_opening(settled)
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

    def _give_up_on_opening(self, waited: SessionMeta) -> SessionMeta:
        """--start's answer for a worker still ``opening`` once its wait is over; the meta if not.

        Under the session's lock. Opened, or ended, meanwhile: that meta,
        answered as any other. Gone -- killed while it opened -- it is --stop's
        answer for a worker gone without publishing its end
        (``_refuse_unconcluded``). Otherwise the stop is recorded in the meta
        (``SessionStore.mark_stop_requested``) and the worker signalled: it
        cancels its opening, a dump under way stopped and reaped, and
        publishes ``cancelled`` -- read under the lock, so even a read already
        open is never published ``running``; --start refuses, exit 1, naming the opening
        and how long it ran. A session that is not recording is never
        answered as started: --start answered success over an opening with no
        cursor (Codex, second review of the opening).
        """
        identity, why, signalled = Identity.UNRESOLVED, "", False
        with self.store.transaction(waited.session_id) as meta:
            if meta.status == OPENING:
                # Recorded before the signal, under the lock the worker's
                # publication that its read is open takes: whenever it handles
                # the signal, it publishes ``cancelled``, never ``running``.
                self.store.mark_stop_requested(meta)
                identity, why = worker_identity(meta)
                if identity is Identity.WORKER:
                    try:
                        os.kill(meta.pid, signal.SIGTERM)
                        signalled = True
                    except (ProcessLookupError, PermissionError):
                        identity, why = worker_identity(meta)
        if meta.status == OPENING:
            _refuse_opening(meta, identity, why, signalled)
        return meta

    def _give_up_on_registration(self, session_id: str) -> SessionMeta:
        """Cancel a session no worker claimed in time; the claimed meta when one just did."""
        with self.store.transaction(session_id) as meta:
            if meta.status == PENDING:
                # Cancelled, not marked crashed: a worker that claims it after
                # this is refused, where one that did went on to record into a
                # session --start had refused (fourth review of PR #42).
                self.store.mark_cancelled(meta)
        if meta.status == CANCELLED and meta.claimed_at_ms is None:
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
        a DeviceNotFoundError already persisted (fourth review of PR #42). A
        worker ``opening`` its read is waited for to the bound it published
        (``_opening_until``), so whatever is logged once --start has answered
        follows its opening -- unless it is gone (``worker_identity``, every
        ``OPENING_WORKER_CHECK_SECONDS``: --start's own child, killed, lingers
        as a zombie a signal still reaches).
        """
        first_seen = time.time()
        asked_at = time.monotonic()
        while True:
            if meta.status == DECIDING or (
                meta.status == RUNNING and meta.counters.get("stream_failure")
            ):
                since = _deciding_since(meta) or first_seen
                if time.time() > since + DECIDING_TIMEOUT_SECONDS:
                    break
            elif meta.status == OPENING:
                if time.time() > _opening_until(meta):
                    break
                if time.monotonic() - asked_at >= OPENING_WORKER_CHECK_SECONDS:
                    asked_at = time.monotonic()
                    if worker_identity(meta)[0] is Identity.GONE:
                        # It may have published its end just before it went.
                        meta = self.store.load_meta(meta.session_id)
                        break
            else:
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
        if meta.status == CANCELLED and meta.claimed_at_ms is not None:
            cli.fail(
                f"Session {session_id}'s worker was still opening its read when it was asked "
                f"to stop, so it cancelled the opening and recorded nothing: the session is "
                f"cancelled.",
                "Run --start again, and --stop the id it prints once it has printed it.",
            )
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
        cancellation, or, for a session of this lifecycle at work, that a stop
        was asked (``SessionStore.mark_stop_requested``) -- before the signal,
        so the worker's next publication, under this lock, reads it: an
        opening asked to stop ends ``cancelled``, never ``running`` (third
        review of the opening). What the worker writes is the worker's.
        """
        with self.store.transaction(session_id) as meta:
            if meta.status == PENDING:
                self.store.mark_cancelled(meta)
                return None
            if meta.status not in LIVE_STATES and not _legacy_worker(meta):
                return None
            if meta.status in LIVE_STATES and meta.lifecycle is not None:
                self.store.mark_stop_requested(meta)
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
        worker went on to find the device gone (second review of PR #42). A
        worker ``opening`` its read exactly to the end its opening published
        (``_opening_until``), never a stop allowance on top: a stop asked 1 s
        before that end answered 5.5 s after it (third review of the opening).
        It cancels at once between its questions, and one under way is
        bounded by what is left of the opening.
        """
        deadline = time.monotonic() + WORKER_EXIT_TIMEOUT_SECONDS
        while True:
            meta = self.store.load_meta(session_id)
            # An earlier version's worker says nothing of its end: it is
            # waited for until it is gone, whatever its meta says.
            ended = meta.status not in LIVE_STATES and not _legacy_worker(meta)
            if ended or not _alive(pid):
                return
            if meta.status == OPENING:
                # Opening its read: to the end the opening published, no later.
                limit = time.monotonic() + _opening_until(meta) - time.time()
            else:
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
        # How --package's jank/StrictMode was attributed, as the worker kept
        # it. Not kept -- an earlier version's worker, which attributed none by
        # uid (and wrote events with no uid), or none wrote any -- is unknown
        # under --package, and nothing to attribute without it.
        attributed = line_counters.get("uid_attribution")
        if not isinstance(attributed, str):
            attributed = UID_UNFILTERED if _unfiltered(meta.args) else None
        shared = line_counters.get("shared_uid", [])
        isolated = line_counters.get("isolated_unattributed")
        isolated = isolated if isinstance(isolated, dict) else {}
        sandbox = line_counters.get("sdk_sandbox_unattributed")
        sandbox = sandbox if isinstance(sandbox, dict) else {}
        changed = line_counters.get("package_uid_changed")
        # WindowManager reports held in memory that the worker could not write
        # as its read ended (_SessionRecords.close).
        held = int(line_counters.get("held_unwritten", 0))
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
            uid_attribution=attributed,
            shared_uid=list(shared) if isinstance(shared, list) else None,
            isolated_unattributed=int(isolated.get("records", 0)),
            isolated_unattributed_uids=sorted(int(uid) for uid in isolated.get("uids", [])),
            sdk_sandbox_unattributed=int(sandbox.get("records", 0)),
            sdk_sandbox_unattributed_uids=sorted(int(uid) for uid in sandbox.get("uids", [])),
            package_uid_changed=(
                sorted(int(uid) for uid in changed) if isinstance(changed, list) else []
            ),
            # Said only by a read made of dumps (API 33), which may write one.
            anchor_written=(
                anchor if isinstance(anchor := line_counters.get("anchor_written"), bool) else None
            ),
            held_unwritten=held,
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
                    meta.status == CANCELLED and meta.claimed_at_ms is None,
                ),
                (
                    "no worker recorded into it: it was cancelled while its worker was still "
                    "opening its read, so nothing was captured",
                    meta.status == CANCELLED and meta.claimed_at_ms is not None,
                ),
                (
                    f"abandoned with --abandon ({meta.extras.get('abandoned')}), so what its "
                    f"worker read after that, and how its stream ended, are not in here; "
                    f"{HELD_UNKNOWN}",
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
                (
                    f"{_held_unwritten_text(held)} (WindowManager ANR reports held in memory "
                    f"for ActivityManager's when the worker ended)",
                    bool(held),
                ),
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

        A session whose meta says its worker is at work (``opening`` /
        ``running`` / ``deciding``) while that worker is gone -- by --stop's test,
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
            if counters.get("anchor_written") is True:
                restart_str += "  anchor line written to the device's log"
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
        for session_id, summary in ((session_a, summary_a), (session_b, summary_b)):
            if summary is None:
                # Not `Run --stop <id>` for a worker gone unpublished: that
                # --stop refuses it (#50), as --get-details says.
                with contextlib.suppress(FileNotFoundError, ValueError):
                    meta = self.store.load_meta(session_id)
                    unpublished = _unpublished_end(meta)
                    if unpublished is not None:
                        _refuse_details_of_an_unpublished_end(meta, unpublished)
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

        Layout: claim meta (``opening``, with the bound the opening keeps) →
        open the read (``_open_session_read``: the device's level, and on API
        33 where the read starts) → ``running`` → build ``adb logcat -B -T 1``
        command → open events.jsonl line-buffered → for each restart
        attempt, spawn logcat and read records until EOF or subprocess death.
        On API 33 the worker reads by dumps instead of the loop of streams
        (``_read_session_by_dumps``), and a level the device does not say ends
        the session with that reason. SIGTERM flushes and exits cleanly. A stream that ends by itself is asked about as --watch asks
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
        # The same request, for a read that waits between its polls (API 33).
        stopping = threading.Event()

        def _on_sigterm(_signum, _frame):
            stop_flag["value"] = True
            stopping.set()

        # Before the claim: once it has claimed, --stop may signal this pid,
        # and a SIGTERM's default action would end the worker before it had
        # published how -- a session --stop could then only refuse.
        signal.signal(signal.SIGTERM, _on_sigterm)
        signal.signal(signal.SIGINT, _on_sigterm)
        pid = os.getpid()
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
        # Published with the claim: the session is ``opening``, within this
        # bound -- one deadline, every question of the opening given only what
        # is left of it. --start and --stop wait for it (_opening_until).
        bound = {"since_ms": int(time.time() * 1000), "within_s": OPENING_TIMEOUT_SECONDS}
        opening_deadline = time.monotonic() + OPENING_TIMEOUT_SECONDS
        try:
            meta = self.store.claim_worker(
                session_id, pid=pid, token=token, counters=counters, opening=bound
            )
        except (TransitionRefusedError, SessionBusyError, FileNotFoundError, ValueError):
            # Not this worker's to record: cancelled by a --stop, or by a
            # --start that gave up on it; claimed already; not its token.
            # Nothing is written (fourth review of PR #42: a late claim
            # recorded a device loss into a session --stop had answered for).
            return 2

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
        # Its end published already: a stop recorded before the read could be
        # published open made that write ``cancelled`` (worker_opened).
        published = False
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
            # The read's opening, the session now this worker's and
            # ``opening``: the device's level, and on API 33 its boot id and
            # where the read opens. --start waits for it; whatever is logged
            # once --start has answered follows the opening.
            opening = self._open_session_read(serial, opening_deadline, stopping)
            if opening.reader is not None:
                # A read made of dumps says whether it wrote a marker line into
                # the device's log -- in the meta now, whatever becomes of the
                # session. A stream writes none, and says nothing of one.
                counters["anchor_written"] = opening.reader.anchor_written
            if opening.cursor is not None:
                # The read's place, in the same write that says it is open.
                counters["cursor"] = _checkpoint(opening.cursor, serial, opening.boot_id)
            if opening.cancelled:
                # A stop came before the read opened: nothing is recorded, and
                # the end below publishes ``cancelled`` (finish_worker).
                counters["cancelled"] = STOPPED_WHILE_OPENING
                return 0
            if opening.failure is None:
                # Under the lock a stop is recorded under: a stop recorded
                # first makes this write ``cancelled`` instead of ``running``.
                try:
                    opened = self.store.worker_opened(session_id, pid, counters)
                except (TransitionRefusedError, FileNotFoundError) as error:
                    raise _Disowned(str(error)) from error
                if opened.status != RUNNING:
                    published = True
                    return 0
            # The device is asked who a pid is until a stop is requested -- or,
            # on API 33, until the read's last poll is over: that poll comes
            # after the stop, and reads what was logged since the one before it.
            reading_after_stop = {"value": False}

            def may_ask() -> bool:
                return not stop_flag["value"] or reading_after_stop["value"]

            # One of each for the whole session. The resolver forgets its owners
            # on every stream (re)start but keeps what it learned of packages; the
            # boundary remembers what was consumed, which a restart's replay is
            # held against.
            process_names = _ProcessNames(
                *_device_lookups(serial, may_ask=may_ask),
                uid_table=_uid_table_lookup(serial, may_ask=may_ask),
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

                # API 33 reads by dumps, never a stream (_read_session_by_dumps),
                # and a level that could not be told, or a read that could not
                # open, ends the session as it was found. Every other level
                # streams, as it always has: the loop below, run there only.
                dumps = opening.reader is not None or opening.failure is not None
                if opening.failure is not None:
                    counters["stream_failure"] = opening.failure
                    note("stream_failed", **opening.failure)
                elif dumps:
                    reading_after_stop["value"] = True
                    try:
                        self._read_session_by_dumps(
                            opening=opening,
                            session_id=session_id,
                            serial=serial,
                            counters=counters,
                            out_handle=out_handle,
                            note=note,
                            deciding=deciding,
                            checkpoint=checkpoint,
                            stopping=stopping,
                            package=package,
                            min_frames=min_frames,
                            session_start_ms=meta.started_at_ms,
                            process_names=process_names,
                            max_restarts=max_restarts,
                        )
                    finally:
                        reading_after_stop["value"] = False
                    failure = counters.get("stream_failure")
                    if failure is not None:
                        note("stream_failed", **failure)
                    else:
                        note("stream_ended")

                for attempt in range(0 if dumps else max_restarts + 1):
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
            if not disowned and not published:
                # The worker's last write: how the session ended, with its
                # counters and any failure, before it exits.
                with contextlib.suppress(
                    TransitionRefusedError, SessionBusyError, FileNotFoundError
                ):
                    self.store.finish_worker(
                        session_id, pid, counters, timeout=WORKER_END_LOCK_TIMEOUT_SECONDS
                    )

        return 2 if disowned or counters.get("stream_failure") else 0

    def _open_session_read(
        self, serial: str | None, deadline: float, stopping: threading.Event
    ) -> "_Opening":
        """After the claim: the device's level and, on API 33, where the session's read opens.

        The level is asked once (``getprop``); one the device does not say
        ends the session with that reason, never a stream. On API 33 the
        device's boot id is read too (``/proc/sys/kernel/random/boot_id``) and
        the opening placed (``common.dump_read.DumpRead.open``: the newest
        record a system process wrote, or a marker line the read writes). All
        of it within ``deadline`` (``OPENING_TIMEOUT_SECONDS`` from the claim),
        passed down: every question -- a `get-state` after a getprop or dump,
        and the failure's own question of the device (``_read_failure``)
        included -- gets no more than what is left, and none is asked once
        nothing is, so the opening ends within its stated bound. A failure is
        kept for the worker to publish. A stop (``stopping``) ends it between
        questions, and a dump under way is stopped and reaped: ``cancelled``.
        """

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise dump_read.ReadRefusedError(
                    f"the read's opening did not finish within its {OPENING_TIMEOUT_SECONDS:g}s.",
                    "Check the device answers `adb shell getprop` and `adb logcat -d` promptly, "
                    "then retry.",
                )
            return remaining

        def go_on() -> None:
            if stopping.is_set():
                raise dump_read.ReadCancelledError("the read was asked to stop before it opened.")

        reader: dump_read.DumpRead | None = None
        try:
            go_on()
            if not dump_read.holds_binary_back(
                serial,
                "anr_watcher",
                timeout=min(dump_read.STATE_TIMEOUT, left()),
                deadline=deadline,
            ):
                return _Opening()
            go_on()
            reader = dump_read.DumpRead(serial)
            boot = dump_read.boot_id(serial, timeout=min(dump_read.STATE_TIMEOUT, left()))
            go_on()
            cursor = reader.open(
                min(dump_read.NEWEST_TIMEOUT, left()), deadline=deadline, stop=stopping
            )
        except dump_read.ReadCancelledError:
            return _Opening(reader=reader, cancelled=True)
        except (dump_read.ReadRefusedError, dump_read.DeviceGoneError, AdbError) as error:
            if stopping.is_set():
                # A stop came first, or as the question failed: the stop is
                # the answer. A worker held past its bound (a host too loaded
                # to run it) resumes into a question already timed out, and
                # --start, which asked it to stop, said it ends cancelled.
                return _Opening(reader=reader, cancelled=True)
            return _Opening(
                reader=reader,
                failure=_read_failure(error, serial, "could not start", deadline=deadline),
            )
        if stopping.is_set():
            # Opened, but the stop came before the read was published open:
            # nothing is recorded into it, as for any stop during the opening.
            return _Opening(reader=reader, cancelled=True)
        return _Opening(reader=reader, cursor=cursor, boot_id=boot)

    def _read_session_by_dumps(
        self,
        *,
        opening: "_Opening",
        session_id: str,
        serial: str | None,
        counters: dict,
        out_handle,
        note: Callable[..., None],
        deciding: Callable[[bool], None],
        checkpoint: Callable[[], None],
        stopping: threading.Event,
        package: str | None,
        min_frames: int,
        session_start_ms: int,
        process_names: _ProcessNames,
        max_restarts: int,
    ) -> None:
        """The session's read on API 33: dumps one poll apart from its cursor, never a stream.

        That release's logcat holds ``-B`` records back while it streams and
        loses them when it is stopped: a session's stream stopped by --stop
        lost the jank logged just before it (recorded:
        logcat_stream_jank_records). So the worker reads as log_monitor does
        on that level (``common.dump_read``): from the cursor it opened after
        its claim, every ``ANDROID_EMU_LOG_POLL_SECONDS`` (1 s) it takes what
        logd delivered after the records it has read, through the same
        pipeline as a streamed record (``_read_dumps_into_events``) -- and at
        --stop's SIGTERM, once more before it publishes ``stopped``. That last
        read may still ask the device who a pid is (its lookups, each bounded
        at ``LOOKUP_TIMEOUT_SECONDS``): it reads what was logged since the poll
        before it, as any poll does; a stream's drain asks nothing.

        **One logical read.** A failed dump is not the end of a stream: the
        assembler, its held WindowManager reports and every correlation, the
        names learned from the log, and the pipeline outlive it, and are closed
        once, at the read's real end -- a stop, or what ends the session. Only
        an answer the device could not give is asked again after a restart
        (``_ProcessNames.forget_unanswered``, the assembler's likewise). No
        replay is suppressed: the cursor already excludes what was read.

        **The checkpoint.** The read's place -- the cursor: a system
        process's frame and the frames read after it, base64 -- is kept in
        the worker's counters (``counters["cursor"]``, ``_checkpoint``) with
        the device's serial and the boot id it was opened in, and is written
        to the session's meta with every write of the worker's, under the
        session's lock: the periodic counter write, ``deciding``, and its end.
        It covers the records read and processed: it moves on once every
        record before it has gone through the pipeline, and before any write
        publishes it the events file is synced -- a sync that fails puts the
        checkpoint back to the last one whose events were synced and ends the
        session, incomplete, saying so (``_kept_durable``). A WindowManager
        report held for ActivityManager's lives in memory, in the assembler,
        until its event is written, so a checkpoint may be past it. The
        checkpoint resumes only this worker's own read (a restart below),
        where the assembler and its held reports survive; a crashed or killed
        worker's session is never resumed from it. Every event is written by
        one function (``_SessionRecords.record``), which counts the held
        reports it could not write: when the worker itself ends -- any failure
        it can observe -- the session ends ``crashed``, incomplete, saying
        exactly how many (``held_unwritten``). When it is killed, or the
        session abandoned, reports it held in memory, if any, are lost and
        their number is unknown (``HELD_UNKNOWN``, said in the gone-worker
        answer, the --abandon remedy and the abandoned session's reason).

        **A restart.** A dump that fails with the device still there -- it
        did not finish, exited non-zero, passed the cap, `get-state` itself
        failed -- ends that attempt; the worker publishes ``deciding`` (the
        checkpoint with it), asks the device, and restarts the read up to
        ``ANDROID_EMU_ANR_MAX_RESTARTS`` times, from the checkpoint its meta
        holds, never from memory alone (``_resume``): on the same device and
        boot, or not at all. What was logged while it was down follows the
        cursor, and is read. A restart loses nothing logd still holds, so it
        does not make the summary incomplete; each is counted
        (``dump_restarts``) and noted in the events file.

        **Its end.** A stop, after its final read: ``stopped``. Otherwise the
        session ends ``crashed`` with ``counters["stream_failure"]`` --
        incomplete, with the reason, and --stop refuses it (exit 3 for a
        device gone): the read lost its place (the cursor turned over, is held
        twice, or is past the cap), a dump was not records, the device went,
        the restarts ran out, a restart found another boot or no checkpoint,
        or the events file could not be synced.
        """
        # The opening's checkpoint is in the meta already: the worker wrote it
        # with the end of its opening (run_worker).
        reader, cursor, boot = opening.reader, opening.cursor, opening.boot_id
        assembler = AnrRecordAssembler(
            process_names.system_server_pid, subject_owner=process_names.owns
        )
        pipeline = _SessionRecords(
            out_handle=out_handle,
            counters=counters,
            package=package,
            min_frames=min_frames,
            session_start_ms=session_start_ms,
            names=process_names,
            boundary=None,
            assembler=assembler,
        )
        now = time.monotonic()
        progress = _DumpProgress(last_record_at=now, last_written=now, durable=counters["cursor"])

        def decide() -> dict | None:
            """``deciding``, its checkpoint durable first; the sync's failure, if it failed."""
            unsynced = _kept_durable(out_handle, counters, progress)
            deciding(True)
            return unsynced

        failure: dict | None = None
        try:
            for attempt in range(max_restarts + 1):
                what = "lost its place in logd's buffer"
                try:
                    if attempt > 0:
                        counters["dump_restarts"] = attempt
                        note("dump_restart", attempt=attempt)
                        # A stop cuts the wait short; the read below still runs
                        # once, from the checkpoint, before the worker ends.
                        stopping.wait(RESTART_BACKOFF_SECONDS)
                        what = "could not resume"
                        cursor = self._resume(session_id, serial)
                        what = "lost its place in logd's buffer"
                        process_names.forget_unanswered()
                        assembler.forget_unanswered()
                    self._read_dumps_into_events(
                        reader=reader,
                        cursor=cursor,
                        stopping=stopping,
                        pipeline=pipeline,
                        assembler=assembler,
                        progress=progress,
                        out_handle=out_handle,
                        counters=counters,
                        checkpoint=checkpoint,
                        serial=serial,
                        boot_id=boot,
                    )
                    break
                except _NotDurable as error:
                    deciding(True)
                    failure = error.failure
                    break
                except dump_read.DeviceGoneError as error:
                    failure = _with_unsynced(
                        decide(), _read_failure(error, serial, "lost its device")
                    )
                    break
                except dump_read.NotRecordsError as error:
                    failure = _with_unsynced(decide(), _read_failure(error, serial, "broke"))
                    break
                except dump_read.PlaceLostError as error:
                    failure = _with_unsynced(decide(), _read_failure(error, serial, what))
                    break
                except dump_read.ReadRefusedError as error:
                    # A dump failed with the device still there: asked again, as
                    # a stream that ended by itself is, and restarted while a
                    # restart is left.
                    unsynced = decide()
                    if unsynced is not None:
                        failure = unsynced
                        break
                    device = _device_availability(serial)
                    said = " ".join(str(error).split())
                    if "error_class" in device:
                        failure = {
                            "error": said,
                            "truncated": False,
                            "what": "lost its device",
                            "device": device,
                        }
                        break
                    if attempt == max_restarts:
                        times = attempt + 1
                        failure = {
                            "error": f"its dumps failed {times} time{'s' if times != 1 else ''} "
                            f"with the device there, with no restart left "
                            f"(ANDROID_EMU_ANR_MAX_RESTARTS={max_restarts}); the last time: "
                            f"{said}",
                            "truncated": False,
                            "what": "failed",
                            "device": device,
                        }
                        break
                    deciding(False)
        finally:
            # The read's real end, however it came: what the assembler still
            # holds was seen, and goes out now -- once. Then what closing
            # wrote, and everything before it, is synced before the end is
            # published with the checkpoint -- on every path, an error nothing
            # foresaw included (it published a checkpoint no sync had kept:
            # Codex, second review of the opening) -- unless a sync has failed
            # already. The checkpoint published is the last one synced. A
            # closure that fails -- a held report's event not written (ENOSPC)
            # -- puts the checkpoint back to the last one synced instead, and
            # the session ends with its error: it raised past the sync, and an
            # advanced cursor was published unsynced (third review).
            try:
                pipeline.close()
            except Exception as error:
                counters["cursor"] = progress.durable
                failure = _with_unsynced(
                    _unclosed(error, int(counters.get("held_unwritten", 0))), failure
                )
            else:
                if failure is None or failure.get("what") != _NOT_DURABLE:
                    failure = _with_unsynced(_kept_durable(out_handle, counters, progress), failure)
            if failure is not None:
                counters["stream_failure"] = failure

    def _resume(self, session_id: str, serial: str | None) -> logcat.Cursor:
        """Where a restarted read continues: the cursor the session's meta holds, in the same boot.

        Read back from the meta -- written under the session's lock by the
        worker's ``deciding`` as it decided to restart -- never from memory
        alone, so a restart reads on from what the session says was
        consumed. The checkpoint must be this session's device's (its serial)
        and the boot it was taken in (``/proc/sys/kernel/random/boot_id``): a
        device that rebooted since holds another boot's log, where the read's
        place is not to be looked for.

        Raises:
            dump_read.PlaceLostError: No checkpoint, one that does not decode,
                or another device or boot.
            dump_read.ReadRefusedError, dump_read.DeviceGoneError: The boot id
                could not be read.
        """
        checkpoint = self.store.load_meta(session_id).counters.get("cursor")
        try:
            cursor, taken_on, boot = _cursor_of(checkpoint)
        except (KeyError, TypeError, ValueError, logcat.LogFramingError) as error:
            raise dump_read.PlaceLostError(
                f"the session's meta holds no checkpoint of its read to resume from "
                f"({type(error).__name__}: {error}).",
                "Record the scenario again.",
            ) from error
        if taken_on != serial:
            raise dump_read.PlaceLostError(
                f"the read's checkpoint is of device {taken_on!r}, not {serial!r}.",
                "Record the scenario again.",
            )
        now = dump_read.boot_id(serial)
        if now != boot:
            raise dump_read.PlaceLostError(
                f"the device's boot id is {now}, not {boot} as when the read's checkpoint was "
                "taken: it rebooted, and the log it holds is another boot's, so what was logged "
                "between that checkpoint and the reboot is gone.",
                "Record the scenario again once the device has booted.",
            )
        return cursor

    def _read_dumps_into_events(
        self,
        *,
        reader: dump_read.DumpRead,
        cursor: logcat.Cursor,
        stopping: threading.Event,
        pipeline: _SessionRecords,
        assembler: AnrRecordAssembler,
        progress: "_DumpProgress",
        out_handle,
        counters: dict,
        checkpoint: Callable[[], None],
        serial: str | None,
        boot_id: str | None,
    ) -> None:
        """Read by dumps from ``cursor`` until a stop, then once more (``common.dump_read.poll``).

        Each poll's records go through the session's pipeline
        (``_SessionRecords``), as a streamed record does; a poll that brings
        none is the read going quiet (a held WindowManager report may be
        due). After each, the checkpoint moves to the cursor after those
        records, and every ``COUNTERS_PERSIST_SECONDS`` the events file is
        synced and the counters, checkpoint with them, written to the meta.
        The pipeline is the caller's, and outlives a failed dump.

        Raises:
            dump_read.ReadRefusedError, dump_read.DeviceGoneError: A read
                failed (``common.dump_read.DumpRead.read``).
            _NotDurable: The events file could not be synced: the checkpoint is
                back at the last one whose events were.
            _Disowned: A counter write was refused: the session is not this
                worker's any more.
        """

        def on_read(records: list[logcat.LogRecord], after: logcat.Cursor) -> None:
            if records:
                progress.last_record_at = time.monotonic()
                pipeline.take(records)
            else:
                pipeline.record(assembler.flush(time.monotonic() - progress.last_record_at))
            # Only now, every record before it through the pipeline.
            counters["cursor"] = _checkpoint(after, serial, boot_id)
            if time.monotonic() - progress.last_written >= COUNTERS_PERSIST_SECONDS:
                unsynced = _kept_durable(out_handle, counters, progress)
                if unsynced is not None:
                    raise _NotDurable(unsynced)
                checkpoint()
                progress.last_written = time.monotonic()

        dump_read.poll(reader, cursor, stop=stopping, deadline=None, on_read=on_read)

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
        (``unattributed_anrs``), never dropped unsaid. A jank or StrictMode
        record is the package's when its uid is one of the package's
        (``_attribute_by_uid``; ``shared_uid`` when other packages share it);
        one of an isolated process's uid is not counted but listed
        (``isolated_unattributed``); one another uid wrote is not the
        package's; and one nothing can place -- the package's uids unknown,
        and no `Start proc` naming its process -- is dropped and counted
        (``unnamed_dropped``, with its pid). Each at or above the frame
        threshold.

        A ``LogFramingError`` ends the read (common.logcat's rule): cut by our
        own stop, the lost record is counted (``cut_records``); otherwise the
        failure is put in ``counters["stream_failure"]`` for the caller.
        """
        if proc.stdout is None:
            return _ReadEnd.ENDED
        stream = _FdRecords(proc.stdout)
        names = process_names or _ProcessNames()
        # A new stream: any pid may have changed hands while there was none,
        # and `-T 1` does not replay the new owner's Start proc record.
        names.reset()
        assembler = AnrRecordAssembler(names.system_server_pid, subject_owner=names.owns)
        if boundary is not None:
            boundary.new_stream()
        broken = False

        # Every record goes through the session's one pipeline, the one API
        # 33's dumps feed too (_SessionRecords).
        pipeline = _SessionRecords(
            out_handle=out_handle,
            counters=counters,
            package=package,
            min_frames=min_frames,
            session_start_ms=session_start_ms,
            names=names,
            boundary=boundary,
            assembler=assembler,
        )
        record, take = pipeline.record, pipeline.take

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
            # seen: a WindowManager report nothing answered. And what this
            # stream could not see, for the summary to disclose.
            pipeline.close()

    # === PRIVATE ===

    def _dump_raw_events(self, session_id: str) -> str:
        """events.jsonl as the session's answers read it: an abandoned one's up to its fence."""
        return self.store.read_raw_events(session_id)

    def _load_summary(self, session_id: str) -> SessionSummary | None:
        """The stored summary, what it did not record read as what it can be.

        A summary saved before drops were counted says nothing of them
        (``unnamed_dropped`` None: unknown), and one saved before uid
        attribution nothing of that (``uid_attribution`` None). A session that
        did not filter by package dropped none and had nothing to attribute,
        so for it those are 0 and ``UID_UNFILTERED``; under --package they stay
        unknown, and --diff says so.
        """
        summary = self.store.load_summary(session_id)
        if summary is None or (
            summary.unnamed_dropped is not None and summary.uid_attribution is not None
        ):
            return summary
        try:
            unfiltered = _unfiltered(self.store.load_meta(session_id).args)
        except (FileNotFoundError, ValueError):
            unfiltered = False
        if not unfiltered:
            return summary
        return replace(
            summary,
            unnamed_dropped=0 if summary.unnamed_dropped is None else summary.unnamed_dropped,
            uid_attribution=summary.uid_attribution or UID_UNFILTERED,
        )

    def _resolve_serial(self, serial: str | None) -> str | None:
        # Not re-wrapped: a bare RuntimeError lost DeviceNotFoundError, so an
        # unavailable device exited 1 here and 3 everywhere else.
        return resolve_device_identifier(serial)


@dataclass(frozen=True)
class _Opening:
    """What a session's worker found opening its read, after its claim: how it will read.

    ``reader``, ``cursor`` and ``boot_id`` on API 33 (a read made of dumps);
    ``failure`` when the level, the boot id or the opening could not be had --
    the session ends with it, never streams; ``cancelled`` when a stop came
    first; all unset: the level streams.
    """

    reader: dump_read.DumpRead | None = None
    cursor: logcat.Cursor | None = None
    boot_id: str | None = None
    failure: dict | None = None
    cancelled: bool = False


def _checkpoint(cursor: logcat.Cursor, serial: str | None, boot_id: str | None) -> dict:
    """The read's place as the session's meta keeps it: the cursor, tied to its device and boot.

    The system record's frame and the frames read after it (its suffix), each
    base64 of logd's own bytes, with the serial the session reads and the boot
    id the read was opened in.
    """
    return {
        "serial": serial,
        "boot_id": boot_id,
        "frame": base64.b64encode(cursor.raw).decode("ascii"),
        "suffix": [base64.b64encode(raw).decode("ascii") for raw in cursor.suffix],
    }


def _cursor_of(checkpoint) -> tuple[logcat.Cursor, str | None, str | None]:
    """A checkpoint read back: ``(cursor, serial, boot id)``.

    Raises:
        KeyError, TypeError, ValueError: Not a checkpoint a worker wrote --
            missing, not base64, or its frame not one record a system process
            wrote.
        logcat.LogFramingError: Its frame is not logd's framing.
    """
    frames = logcat.parse_frames(base64.b64decode(checkpoint["frame"], validate=True))
    if len(frames) != 1 or not logcat.system_written(frames[0][1]):
        raise ValueError("its frame is not one record a system process wrote")
    suffix = tuple(base64.b64decode(item, validate=True) for item in checkpoint["suffix"])
    raw, record = frames[0]
    return logcat.Cursor(raw, record, suffix), checkpoint["serial"], checkpoint["boot_id"]


@dataclass
class _DumpProgress:
    """A session's read made of dumps between its polls, kept across a restart.

    Attributes:
        last_record_at: When a poll last brought a record (``time.monotonic``):
            the quiet a held WindowManager report waits out.
        last_written: When the counters were last written to the meta.
        durable: The last checkpoint whose events the events file holds synced.
    """

    last_record_at: float
    last_written: float
    durable: dict


# How a session that could not sync its events file says it ended.
_NOT_DURABLE = "could not keep its events"


class _NotDurable(Exception):  # noqa: N818 -- a signal out of the read, carrying its failure
    """The events file could not be synced: the read ends, its checkpoint put back."""

    def __init__(self, failure: dict) -> None:
        super().__init__(failure["error"])
        self.failure = failure


def _kept_durable(out_handle, counters: dict, progress: _DumpProgress) -> dict | None:
    """Sync the events file; the checkpoint then stands -- else it goes back, and why.

    A checkpoint is published only with every write of the worker's, so it is
    never ahead of the events of the records it covers, read and processed:
    synced, it becomes the durable one; not, ``counters["cursor"]`` is put
    back to the last durable one and the failure returned, for the session
    to end with (incomplete, said). A WindowManager report held for
    ActivityManager's is not yet an event, and stays in memory: a durable
    checkpoint may be past it. Only this worker resumes from the checkpoint,
    its held reports with it; one that ends itself without writing them says
    exactly how many (``held_unwritten``). One killed, or abandoned, cannot:
    their number is unknown (``HELD_UNKNOWN``).
    """
    try:
        out_handle.flush()
        os.fsync(out_handle.fileno())
    except OSError as error:
        counters["cursor"] = progress.durable
        return {
            "error": f"the events file could not be synced ({error}), so the read's place in "
            "the meta is kept at the last records whose events were, and what was read after "
            "them may not be on disk",
            "truncated": False,
            "what": _NOT_DURABLE,
            "device": {"state": "not asked"},
        }
    progress.durable = counters["cursor"]
    return None


def _unclosed(error: Exception, held: int) -> dict:
    """The failure of a read whose closure could not write what it still held."""
    return {
        "error": f"the read's last events could not be written as it closed "
        f"({type(error).__name__}: {error}): {_held_unwritten_text(held)}, and the read's "
        "place in the meta is kept at the last records whose events were synced",
        "truncated": False,
        "what": _NOT_DURABLE,
        "device": {"state": "not asked"},
    }


# What a session whose worker was killed, or abandoned, says of the reports it
# held for correlation in memory: lost, and their number unknown. The count
# (``held_unwritten``) is exact only when the worker itself ends.
HELD_UNKNOWN = (
    "reports the worker held for correlation, if any, were not written; their number is unknown"
)


def _held_unwritten_text(held: int) -> str:
    """``N held report(s) were not written``, in words: WindowManager reports lost at the end."""
    if held == 1:
        return "1 held report was not written"
    return f"{held} held reports were not written"


def _with_unsynced(unsynced: dict | None, failure: dict | None) -> dict | None:
    """A read's failure with a failed sync said beside it; either alone; or neither."""
    if unsynced is None:
        return failure
    if failure is None:
        return unsynced
    return {**failure, "error": f"{failure['error']}; then {unsynced['error']}"}


def _opening_since(meta: SessionMeta) -> float | None:
    """When (epoch s) the worker began opening its read: the bound published with its claim."""
    opening = meta.extras.get("opening")
    since = opening.get("since_ms") if isinstance(opening, dict) else None
    if not isinstance(since, int | float):
        since = meta.claimed_at_ms
    return since / 1000 if isinstance(since, int | float) else None


def _opening_within(meta: SessionMeta) -> float:
    """The bound (s) the worker published for its opening; this version's when it did not."""
    opening = meta.extras.get("opening")
    within = opening.get("within_s") if isinstance(opening, dict) else None
    return within if isinstance(within, int | float) else OPENING_TIMEOUT_SECONDS


def _opening_until(meta: SessionMeta) -> float:
    """Until when (epoch s) a session ``opening`` its read is waited for, by --start and --stop.

    The bound its worker published with its claim (``extras["opening"]``:
    ``since_ms``, ``within_s``), and what the opening can still take past it
    (``OPENING_OVERRUN_SECONDS``): exactly how long it can stay ``opening``
    with its worker running.
    """
    since = _opening_since(meta)
    if since is None:
        return 0.0
    return since + _opening_within(meta) + OPENING_OVERRUN_SECONDS


def _read_failure(
    error: BaseException, serial: str | None, what: str, *, deadline: float | None = None
) -> dict:
    """A dump-made read's failure as the session's ``stream_failure``, for --stop to answer with.

    A device gone -- adb's words as a dump ended (``"source": "logcat"``), or
    adb's answer when asked -- is kept as that error: --stop raises it again
    (exit 3). Anything else asks the device once more, as a broken stream's
    failure does (``_device_availability``), and keeps the read's remedy.
    Within ``deadline`` (an opening's, ``time.monotonic()``) that question
    gets only what is left, and is not asked once nothing is: it asked with
    a fresh ``GET_STATE_TIMEOUT_SECONDS`` of its own, past the opening's bound.
    """
    if isinstance(error, dump_read.DeviceGoneError):
        device = _device_answer(error.error)
        if error.source == "logcat":
            device["source"] = "logcat"
    elif isinstance(error, DeviceError):
        device = _device_answer(error)
    elif deadline is None:
        device = _device_availability(serial)
    elif (left := deadline - time.monotonic()) > 0:
        device = _device_availability(serial, timeout=min(GET_STATE_TIMEOUT_SECONDS, left))
    else:
        device = {"state": "not asked", "message": "the opening's bound had passed"}
    failure = {
        "error": " ".join(str(error).split()),
        "truncated": False,
        "what": what,
        "device": device,
    }
    remedy = getattr(error, "remedy", None)
    if remedy and "error_class" not in device:
        failure["remedy"] = remedy
    return failure


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


def _count_uncounted(counters: dict, key: str, uid: int) -> None:
    """Count one jank/StrictMode record --package did not count under ``key``, and its uid.

    ``key``: ``isolated_unattributed`` (an isolated or app-zygote process's
    uid) or ``sdk_sandbox_unattributed`` (the package's SDK sandbox's).
    """
    uncounted = counters.setdefault(key, {"records": 0, "uids": []})
    uncounted["records"] += 1
    if uid not in uncounted["uids"]:
        uncounted["uids"] = sorted([*uncounted["uids"], uid])


def _note_uid_attribution(
    counters: dict, event: dict, verdict: str, package: str, names: _ProcessNames
) -> None:
    """Keep, for the summary, what attributing ``event`` by its uid showed about the session.

    A record counted by a uid other packages share (``shared_uid``, None:
    the uid table could not be read), and a session that could attribute
    nothing by uid because the device did not give the package's uids.
    """
    if verdict == UID_PACKAGE and event.get("shared_uid", []) != []:
        counters["shared_uid"] = event["shared_uid"]
    elif (
        verdict == UID_UNKNOWN
        and event.get("uid") is not None
        and names.package_uids(package) is None
    ):
        counters["uid_attribution"] = UID_PACKAGE_UIDS_UNKNOWN


def _tail_evidence(event: dict) -> dict:
    """What a summary shows of an event the record a session started on made."""
    shown = (
        "kind",
        "timestamp",
        "process",
        "pid",
        "uid",
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
        counters["stream_failure"] = failure = _worker_failed(error)
    # Reports the read's closure could not write: said with the failure.
    held = int(counters.get("held_unwritten", 0))
    said = _held_unwritten_text(held)
    if held and said not in failure.get("error", ""):
        failure["error"] = f"{failure.get('error', 'the worker failed')}; {said}"


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
    """--get-details' (and --diff's) refusal for a session with no summary, its worker gone.

    --stop's answer for it (``_refuse_unconcluded``), not a pointer to that
    --stop: `Run --stop <id> first` sent the caller to a refusal.
    """
    session_id = meta.session_id
    cli.fail(
        f"Session {session_id} has no summary: {_unpublished_reason(why)}. The worker was "
        f"killed, or died before it could say how its capture ended, so whether its stream "
        f"failed is unknown, {HELD_UNKNOWN}, and --stop refuses the session for that reason: "
        f"stopping it saves no summary either.",
        f"--stop {session_id} --abandon saves what its events file holds, marked incomplete "
        f"and abandoned ({HELD_UNKNOWN}); then --get-details {session_id} reads it. Or "
        f"--start a new session.",
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


def _refuse_opening(meta: SessionMeta, identity: Identity, why: str, signalled: bool) -> NoReturn:
    """--start's refusal for a session still ``opening`` once its wait is over (exit 1).

    Its worker gone: --stop's answer for a worker gone without publishing its
    end (``_refuse_unconcluded``). Otherwise the refusal names the opening and
    how long it ran, and whether the worker was asked to stop.
    """
    if identity is Identity.GONE:
        _refuse_unconcluded(meta, identity, why, signalled=False)
    session_id = meta.session_id
    serial = meta.args.get("serial") or "the device"
    since = _opening_since(meta)
    ran = f"{time.time() - since:.0f}s" if since is not None else "past its bound"
    asked = (
        "it is asked to stop: it cancels its opening and ends cancelled, nothing recorded into it"
        if signalled
        else f"it could not be asked to stop ({why})"
    )
    cli.fail(
        f"The worker for {session_id} was still opening its read of {serial}'s log {ran} after "
        f"it began -- the device's API level, its boot id and the newest record a system "
        f"process wrote, within {_opening_within(meta):g}s, with {OPENING_OVERRUN_SECONDS:g}s "
        f"more for its last step and its publication -- so the session is not recording, and "
        f"{asked}.",
        f"Check the device answers `adb -s {serial} logcat -d -B -T 1` promptly, then --start "
        f"again; --stop {session_id} answers how this one ended.",
    )


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
        f"--stop {session_id} --abandon saves what its events file holds, marked incomplete "
        f"and abandoned (no meta write of the worker's is applied after that; events it "
        f"still appends are ignored; {HELD_UNKNOWN})"
    )
    if identity is Identity.WORKER:
        asked = (
            f"{WORKER_EXIT_TIMEOUT_SECONDS:.1f}s after it was asked to stop"
            if signalled
            else "and was not asked to stop (it could not be shown to be the worker then)"
        )
        if signalled and meta.status == OPENING:
            asked = (
                f"by the end its opening published "
                f"({_opening_within(meta) + OPENING_OVERRUN_SECONDS:g}s after it began), though "
                f"asked to stop"
            )
        deciding = {
            DECIDING: ", still deciding how its logcat stream ended",
            OPENING: ", still opening its read",
        }.get(meta.status, "")
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
        f"failed is unknown, {HELD_UNKNOWN}, and no summary is saved.",
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
    # A read made of dumps (API 33) keeps its own remedy -- a lost place
    # wants a shorter capture or a larger buffer -- said first.
    remedy = failure.get("remedy")
    cli.fail(
        f"Session {session_id}'s logcat stream {how} ({error}); the session stopped there, "
        f"so it is incomplete. The device, asked then: {_availability_text(device)}.",
        (f"{remedy} " if remedy else "")
        + f"Read what was captured with --get-details {session_id}, and record the scenario "
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


def _device_availability(serial: str | None, timeout: float = GET_STATE_TIMEOUT_SECONDS) -> dict:
    """What `adb get-state` says of the device, asked once (within ``timeout``), kept for --stop.

    ``state`` is ``available``; ``unavailable`` only for what the boundary
    calls unavailable (``cli.DEVICE_UNAVAILABLE``: exit 3); ``error`` for
    another device error -- several devices and no serial, exit 1 -- and
    ``unknown`` when adb could not answer. A device error keeps its class
    name, words and remedy, which ``_refuse_broken_stream`` raises again: a
    ``MultipleDevicesError`` was labelled unavailable here and rebuilt as
    "not found", exit 3 and "Reconnect the device" (wave 4).
    """
    try:
        _require_device(serial, timeout=timeout)
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
    parser = cli.parser(
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
