#!/usr/bin/env python3
"""
Android Emulator/Device Log Monitoring and Analysis

Real-time log streaming from Android devices/emulators with intelligent filtering,
error detection, and token-efficient summarization.

Features:
- Real-time log streaming from devices/emulators
- Smart filtering by app package, tag, severity
- Error/warning classification and deduplication
- Duration-based or continuous follow mode
- Token-efficient summaries with full logs saved to file
- Integration with test_recorder and app_state_capture

Usage Examples:
    # Monitor app logs in real-time (follow mode)
    python scripts/log_monitor.py --app com.myapp --follow

    # Capture logs for specific duration
    python scripts/log_monitor.py --app com.myapp --duration 30s

    # Extract errors and warnings only
    python scripts/log_monitor.py --severity error,warning --duration 1m

    # Inspect the last 5 minutes of history (dump-and-exit, no live follow)
    python scripts/log_monitor.py --app com.myapp --last 5m

    # Save logs to file
    python scripts/log_monitor.py --app com.myapp --duration 1m --output logs/

    # Verbose output with full log lines
    python scripts/log_monitor.py --app com.myapp --verbose
"""

import argparse
import bisect
import contextlib
import functools
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from common import cli, logcat
from common.adb_exec import (
    DEFAULT_TIMEOUT,
    AdbCommandError,
    AdbError,
    AdbTimeoutError,
    DeviceError,
    DeviceOfflineError,
    classify_device_error,
    run_adb,
)
from common.device_utils import (
    UnknownSdkLevelError,
    build_adb_command,
    get_package_uids,
    get_pid_uids,
    get_sdk_level,
    get_system_server_pid,
    quote_for_device_shell,
)
from common.env_config import env_int

# Output caps (env-configurable via ANDROID_EMU_LOG_*). Defaults preserve the
# historical behavior of this script.
LOG_LINE_MAX = env_int("ANDROID_EMU_LOG_LINE_MAX", 120)
LOG_TAIL = env_int("ANDROID_EMU_LOG_TAIL", 50)
LOG_TEXT_SUMMARY_CAP = env_int("ANDROID_EMU_LOG_TEXT_SUMMARY", 5)
LOG_JSON_CAP = env_int("ANDROID_EMU_LOG_JSON_CAP", 20)
LOG_INFO_CAP = env_int("ANDROID_EMU_LOG_INFO_CAP", 20)
# Lines a live `--app` read holds from pids not yet known to be the app's, in
# case a later line names one (see LogMonitor._take). Bounded because every
# other process's lines pass through it on a busy device and a --follow can
# run for hours. 2000 lines is about seven minutes of the API 35 emulator
# (2929 lines in a measured ten). The gap it exists for: a process already
# running when the read began is named only by system_server's uid-carrying
# `Killing` line, after all of its own -- in logcat_app_restart_usec the
# force-stopped process logged for 3.6 s before it. A line older than the cap
# is dropped, and counted (`attribution.pending_evicted`).
LOG_PENDING_CAP = env_int("ANDROID_EMU_LOG_PENDING_CAP", 2000)
# Grace period for a terminated logcat to exit before it is killed. Bounded
# so shutdown can never become the hang it is meant to prevent.
STREAM_SHUTDOWN_TIMEOUT = env_int("ANDROID_EMU_LOG_SHUTDOWN_TIMEOUT", 5)
# Budget for `adb get-state`, asked before every read and again when a live
# read ends by itself. It answers at once either way (measured: `device`, or
# `error: device '...' not found` exit 1); the bound is for a wedged server.
STATE_TIMEOUT = env_int("ANDROID_EMU_LOG_STATE_TIMEOUT", 15)
# Deadline for a `--last` dump. A dump ends by itself, so it is bounded
# whatever the caller asked -- the one mode with no --duration to arm a timer
# had none, and a read blocked on a stalled pipe (three bytes of a record's
# header, then nothing) waited until something outside closed it (review 2 of
# v0.8.0, item 10). Twice adb's per-call budget (ANDROID_EMU_ADB_TIMEOUT, 30 s):
# the 60 s app_state_capture and anr_watcher --since give the same `logcat -d`,
# raised with that variable. Measured on the API 35 emulator: its whole
# 12.9 MB buffer dumps as records in 0.2 s.
DUMP_TIMEOUT = 2 * DEFAULT_TIMEOUT
# A live read on API 33 is a series of dumps, nominally one poll apart
# (LogMonitor._read_by_dumps; a dump that takes longer than a poll is followed
# at once by a catch-up one). Its bounds, each separate:
# - each dump of the opening search for the newest system record:
#   NEWEST_TIMEOUT, and never more than a --duration;
# - each dump's own deadline: DUMP_FLOOR seconds plus the seconds since the
#   last read (about one poll interval), never more than DUMP_TIMEOUT, the
#   same for every doubled tail within one read;
# - a dump still running when it ends -- its deadline, the byte cap, an output
#   closed early -- gets KILL_REAP_TIMEOUT more to be reaped: it runs in a
#   session of its own, so one left running would outlive the CLI;
# - after each dump, whatever its outcome, and before any verdict on it:
#   `adb get-state`, bounded by STATE_TIMEOUT. A device error in adb's own
#   stderr is exit 3 before that, after the bounded `adb devices` its message
#   lists the attached devices from (common.adb_exec).
DUMP_FLOOR = 5
NEWEST_TIMEOUT = STATE_TIMEOUT
KILL_REAP_TIMEOUT = 2
# How often a dump-made read asks again, whatever its mode.
POLL_SECONDS = env_int("ANDROID_EMU_LOG_POLL_SECONDS", 1)
# How often a running dump is checked against its deadline and the byte cap.
DUMP_WAIT_SECONDS = 0.05
# The output size past which a dump is stopped and refused, unread: the sum of a
# device's log buffers bounds a window, and 32 MiB is past the API 33
# emulator's four 2 MiB rings many times over (measured: `logcat -g`). A
# DETECTION threshold, not a maximum: it is checked every DUMP_WAIT_SECONDS, so
# the file can overshoot it by what adb writes in that time and before the kill
# lands. A read also stops doubling its tail before one of about twice the
# last dump's bytes would pass it (common.logcat.read_after). Raise it for a
# device whose rings are larger (`adb logcat -g`).
DUMP_CAP_BYTES = env_int("ANDROID_EMU_LOG_DUMP_CAP_BYTES", 32 * 1024 * 1024)
# The most of a dump's stderr that is read: its last 64 KiB, which holds adb's
# own final words, whatever the child wrote before them.
SAID_TAIL_BYTES = 64 * 1024


@contextlib.contextmanager
def _sigint_held():
    """Hold SIGINT off while the block runs, where the platform can (POSIX ``pthread_sigmask``).

    A Ctrl-C meanwhile is delivered when the block ends; without
    ``pthread_sigmask`` (Windows) the block runs unshielded.
    """
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        yield
        return
    held = mask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        mask(signal.SIG_SETMASK, held)


def _tail(file, limit: int) -> str:
    """The last ``limit`` bytes of an open binary ``file``, decoded: never more, however large."""
    size = os.fstat(file.fileno()).st_size
    file.seek(max(0, size - limit))
    return file.read(limit).decode("utf-8", errors="replace")


@functools.cache
def _device_refusal_type(device_error: type[DeviceError]) -> type[cli.Failure]:
    """A refusal that is both a ``cli.Failure`` and ``device_error`` (exit 3, ``partial``)."""
    return type(
        f"PartialCapture{device_error.__name__}",
        (cli.Failure, device_error),
        {"__doc__": f"A dump-made read refused because of a {device_error.__name__}."},
    )


class LogMonitor:
    """Monitor and analyze Android device/emulator logs with intelligent filtering."""

    # Android logcat priority levels
    PRIORITY_MAP = {
        "V": "verbose",
        "D": "debug",
        "I": "info",
        "W": "warning",
        "E": "error",
        "F": "fatal",
    }

    def __init__(
        self,
        app_package: str | None = None,
        device_serial: str | None = None,
        severity_filter: list | None = None,
    ):
        """
        Initialize log monitor.

        Args:
            app_package: Filter logs by app package name
            device_serial: Device serial (auto-detects if None)
            severity_filter: List of severities to include (error, warning, info, debug, verbose)

        Raises:
            ValueError: A severity is not one of ``common.logcat.SEVERITY_NAMES``.
                An unknown one used to be kept and matched nothing (D1).
        """
        self.app_package = app_package
        self.device_serial = device_serial
        self.severity_filter = severity_filter or ["error", "warning", "info", "debug"]
        unknown = [name for name in self.severity_filter if name not in logcat.SEVERITY_NAMES]
        if unknown:
            raise ValueError(
                f"unknown severity {unknown[0]!r}; accepted: {', '.join(logcat.SEVERITY_NAMES)}"
            )
        # The floor a `*:<P>` filterspec used to apply on the device. A binary
        # read cannot take one (`-B` ignores filterspecs, measured), so it is
        # applied here -- after attribution, which needs ActivityManager's I
        # records whatever the floor.
        self._floor = logcat.min_priority_for(self.severity_filter)

        # Log storage
        self.log_lines = []
        self.errors = []
        self.warnings = []
        self.info_messages = []

        # Statistics. error_count / warning_count are LINES, the same thing a
        # reader counts down the priority column; the unique counts come from
        # the dedup signatures below. They were one number, the signature count,
        # so six recorded W lines reported "warnings: 2" (N15).
        self.error_count = 0
        self.warning_count = 0
        self.info_count = 0
        self.debug_count = 0
        self.verbose_count = 0
        self.total_lines = 0
        # Records a stopped read cut in half: the read was ended by us (its
        # deadline, an interrupt) while a record was arriving. Disclosed, not
        # silently dropped; a cut the stream made by itself fails the capture.
        self.cut_records = 0

        # The dump-made read (API 33): the tail its next read asks for first
        # (common.logcat.read_after) -- twice the records the last read found,
        # never under INITIAL_TAIL.
        self._tail = logcat.INITIAL_TAIL
        # Whether the dump-made read wrote its own anchor (``_write_anchor``):
        # None for any other read, so its JSON has no such key.
        self.anchor_written: bool | None = None

        # Deduplication, per severity, so a warning cannot hide an error with
        # the same text.
        self.seen_messages: dict[str, set[str]] = {"error": set(), "warning": set()}

        # Every pid of app_package with a life in this capture: the one `pidof`
        # named (its uid checked), and any the log itself names as the app's (D4).
        self.app_pids: set[str] = set()
        self._pidof_failure: str | None = None
        self._pidof_pids: set[str] = set()
        self._pidof_rejected: set[str] = set()
        self._running: set[str] = set()
        self._system_server: str | None = None
        self._system_server_asked = False
        self._package_uids: frozenset[int] | None = None
        self._package_uid_asked = False
        # The live --app reader (common.logcat.AppWindowStream); None otherwise.
        self._stream: logcat.AppWindowStream | None = None
        # What decided the window, for the JSON's `attribution` (None without --app).
        self.attribution: dict | None = None
        # Input order of each stored line, parallel to log_lines: a live read
        # decides some lines late and must still store them in order.
        self._log_order: list[int] = []

        # Process control
        self.log_process = None
        self.interrupted = False
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def parse_time_duration(self, duration_str: str) -> float:
        """
        Parse duration string to seconds.

        Delegates to ``common.logcat.parse_duration`` so ``--duration 1h`` means
        the same thing in every script in this skill.

        Args:
            duration_str: Duration like "30s", "5m", "1h"

        Returns:
            Duration in seconds

        Raises:
            ValueError: If the format is unrecognised.
        """
        return logcat.parse_duration(duration_str)

    def parse_logcat_line(self, line: str) -> dict | None:
        """
        Parse logcat line.

        Android logcat format: date time PID TID Priority Tag: Message
        Example: 12-11 18:30:45.123  1234  5678 E ActivityManager: Error message

        Args:
            line: Logcat line to parse

        Returns:
            Dict with parsed fields or None if invalid
        """
        # Match logcat format
        # The fraction is 3 digits, or 6 with `-v usec` (which the command asks
        # for: see build_logcat_command).
        pattern = (
            r"(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}(?:\d{3})?)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+"
            r"([^:]+):\s*(.*)"
        )
        match = re.match(pattern, line)

        if not match:
            return None

        timestamp, pid, tid, priority, tag, message = match.groups()

        return {
            "timestamp": timestamp,
            "pid": pid,
            "tid": tid,
            "priority": priority,
            "severity": self.PRIORITY_MAP.get(priority, "unknown"),
            "tag": tag.strip(),
            "message": message.strip(),
            "raw": line,
        }

    def classify_log_line(self, parsed: dict) -> str:
        """
        Get severity from parsed log line.

        Args:
            parsed: Parsed log dict

        Returns:
            Severity level (error, warning, info, debug, verbose)
        """
        priority = parsed.get("priority", "I")

        if priority in ["E", "F"]:
            return "error"
        if priority == "W":
            return "warning"
        if priority == "I":
            return "info"
        if priority == "D":
            return "debug"
        # V
        return "verbose"

    def deduplicate_message(self, message: str, tag: str, severity: str = "warning") -> bool:
        """
        Check if message is duplicate.

        Args:
            message: Log message
            tag: Log tag
            severity: ``error`` or ``warning`` -- each has its own signatures.

        Returns:
            True if this is a new message, False if duplicate
        """
        # Create signature from tag and message (without timestamps/PIDs)
        signature = f"{tag}:{message}"
        signature = re.sub(r"\d+", "", signature)  # Remove numbers
        signature = re.sub(r"\s+", " ", signature).strip()

        seen = self.seen_messages[severity]
        if signature in seen:
            return False

        seen.add(signature)
        return True

    def _store(self, line: str, sequence: int | None) -> None:
        """Keep ``line`` in ``log_lines`` at its place in input order."""
        if sequence is None:
            sequence = self._log_order[-1] + 1 if self._log_order else 0
        at = bisect.bisect_right(self._log_order, sequence)
        self._log_order.insert(at, sequence)
        self.log_lines.insert(at, line)

    def process_log_line(self, line: str, sequence: int | None = None):
        """
        Process a single log line.

        Args:
            line: Log line to process
            sequence: Its position in the input, when it is decided out of
                order (a live --app read replays held lines); stored there.
        """
        if not line.strip():
            return

        # Parse logcat format
        parsed = self.parse_logcat_line(line)
        if not parsed:
            # Store unparsed line as-is
            self._store(line, sequence)
            self.total_lines += 1
            return

        self.total_lines += 1
        self._store(parsed["raw"], sequence)

        # Classify severity
        severity = self.classify_log_line(parsed)

        # Skip if not in filter
        if severity not in self.severity_filter:
            return

        # Errors and warnings: every line counts; only a new message is listed.
        # The count used to sit behind the dedup `return`, so it counted
        # messages, not lines (N15).
        if severity in ("error", "warning"):
            if severity == "error":
                self.error_count += 1
            else:
                self.warning_count += 1
            if self.deduplicate_message(parsed["message"], parsed["tag"], severity):
                listed = self.errors if severity == "error" else self.warnings
                listed.append(f"[{parsed['tag']}] {parsed['message']}")
        elif severity == "info":
            self.info_count += 1
            if len(self.info_messages) < LOG_INFO_CAP:  # Keep only recent info
                self.info_messages.append(f"[{parsed['tag']}] {parsed['message']}")
        elif severity == "debug":
            self.debug_count += 1
        else:  # verbose
            self.verbose_count += 1

    def _severity_min_priority(self) -> str | None:
        """
        Resolve the active severity filter to a single minimum logcat priority.

        logcat's ``*:<priority>`` form shows that level and everything above it
        (V < D < I < W < E < F), so we pick the lowest level the filter requests.

        Returns:
            A single priority letter (e.g. "W"), or None if no filter applies.
        """
        return logcat.min_priority_for(self.severity_filter)

    def build_logcat_command(
        self,
        last_minutes: float | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """
        Build the ``adb logcat`` command for streaming or a historical window.

        This is a pure arg->command mapping (no device I/O), kept separate so the
        PID-resolution and process plumbing in ``stream_logs`` stays testable.
        The argv shape itself comes from ``common.logcat``, which every logcat
        reader in this skill shares.

        ``-B``: logd's records whole (``common.logcat.read_record``), never
        text. A tag is the app's to choose and logcat prints it verbatim, so in
        text one app's record could print a second line headed with
        system_server's pid -- a forged ``Start proc`` that put another
        process's lines in the app's window (logcat_tag_injection). Each
        record is printed back as ``-v threadtime -v usec`` would print it
        (``LogRecord.lines``), the layout ``parse_logcat_line`` reads; the
        ``time`` layout it once asked for matched nothing (A1).

        No ``--pid``: ``--app`` is filtered here, on the host. One ``--pid`` is
        one process, so after a crash and restart it kept only the new process
        and dropped the one that crashed (D4). No ``*:<P>`` either: ``-B``
        ignores filterspecs (measured), so the severity floor is applied on the
        host, after the records that name the app's processes were read.

        Args:
            last_minutes: If set, dump a historical window with ``-d -t`` instead
                of streaming live. The window starts at ``now - last_minutes``.
            now: Reference time for the historical window (defaults to current
                time; injectable for deterministic tests).

        Returns:
            Complete adb command list ready for subprocess.
        """
        since = None
        if last_minutes is not None:
            since = logcat.window_start(last_minutes * 60, now=now)

        # One format for every read, with --app or not. A live read starts at
        # the end of the buffer (`-T 1`, as anr_watcher's live stream does):
        # without it `adb logcat` first replays the whole ring buffer, and an
        # --app read opened the running pid's life before that history, so an
        # EARLIER owner of the pid had its lines kept (security review of #27).
        # History is --last's job.
        return logcat.build_logcat_command(
            self.device_serial,
            since=since,
            binary=True,
            tail=1 if last_minutes is None else None,
        )

    def _resolve_app_pids(self) -> set[str]:
        """The pids ``pidof`` reports for ``self.app_package`` now.

        No answer is not a refusal on its own: an app that crashed and was not
        restarted has no process now, and its window is the one most worth
        reading. The capture is refused only when no pid of the app can be
        found at all (``stream_logs``) -- the rule app_state_capture follows,
        so the two read one app's window the same way.

        Returns:
            The pids; empty when ``pidof`` names none. The recorded answer for
            an app that is not running (pidof_not_running) is no output, exit 1.
            Any other failure is kept in ``self._pidof_failure`` for the
            refusal to quote, should one follow.
        """
        if not self.app_package:
            return set()
        try:
            result = run_adb(
                "shell",
                self.device_serial,
                "pidof",
                quote_for_device_shell(self.app_package),
                check=True,
            )
        except AdbCommandError as error:
            if error.returncode != 1 or error.stdout.strip() or error.stderr.strip():
                self._pidof_failure = str(error)
            return set()
        return {token for token in result.stdout.split() if token.isdigit()}

    def _take(self, record: logcat.LogRecord, echo: bool) -> None:
        """Take one record of a live read into the capture.

        Without ``--app`` every record is the capture's. With it, the live
        reader (``common.logcat.AppWindowStream``) decides: a record of one of
        the app's processes, inside that process's life, is kept; one of the
        package's uid from a pid not yet decided is held back and kept later
        if the pid is named (a process already running when the read began is
        named only when it is killed). Held records arrive late and are stored
        at their place in input order; ``--follow`` prints them when they are
        decided, marked.
        """
        if self._stream is None:
            self._keep(record, echo)
            return
        for sequence, kept, replayed in self._stream.feed(record):
            self._keep(kept, echo, sequence=sequence, replayed=replayed)

    def _system_server_pid(self) -> str | None:
        """system_server's pid, asked of the device once, when first needed.

        Only system_server may say which pids were the app's (an app can log
        under its tag). The question is put only when a line about some
        process's start or death arrives, so a capture that never sees one
        makes no extra adb call.
        """
        if not self._system_server_asked:
            self._system_server_asked = True
            self._system_server = get_system_server_pid(
                self.device_serial, not_one_of=self._pidof_pids
            )
            if self._system_server is None:
                print(
                    "system_server's pid is unknown, so ActivityManager's lines about "
                    f"{self.app_package}'s processes cannot be verified and are not used: "
                    "the window holds only the process `pidof` names.",
                    file=sys.stderr,
                )
        return self._system_server

    def _package_uid_lookup(self) -> int | None:
        """The package's uid, asked of the device once, when first needed.

        A pid is the app's only when its uid is the package's: any app may
        name its own process after another package (``android:process``).
        Unknown, no process can be confirmed (fail closed).
        """
        if not self._package_uid_asked:
            self._package_uid_asked = True
            self._package_uids = get_package_uids(self.device_serial, self.app_package)
            if self._package_uids is None:
                print(
                    f"{self.app_package}'s uid is unknown (`cmd package list packages -U` "
                    "named none), so no process can be confirmed as its own.",
                    file=sys.stderr,
                )
        return self._package_uids

    def _verify_running(self, pids: set[str]) -> set[str]:
        """The pids of ``pids`` whose uid ``ps`` reports as one of the package's.

        ``pidof`` matches a process NAME, which another app can give its own
        process. One bounded ``ps -o PID,UID`` call checks them all. The
        package has one uid per user it is installed for; any of them counts.
        """
        if not pids:
            return set()
        uids = self._package_uid_lookup()
        owners = get_pid_uids(self.device_serial, pids) if uids else {}
        verified = {pid for pid in pids if uids and owners.get(pid) in uids}
        self._pidof_rejected = pids - verified
        if self._pidof_rejected and uids:
            print(
                f"`pidof {self.app_package}` named {sorted(self._pidof_rejected, key=int)}, "
                "whose uid is not the package's; left out.",
                file=sys.stderr,
            )
        return verified

    def _settle_attribution(self, held: list[logcat.LogRecord] | None) -> None:
        """Decide a held (historical) window, and record what decided the window.

        Sets ``app_pids`` and ``attribution``. Reported on stderr when nonzero:
        lines of the app that the live hold dropped at its cap before the
        app's process was named (``pending_evicted``), and lines logd stamped
        with the package's uid that were left out because nothing in the
        window ties their process to the package (``unresolved_lines``; of
        them, ``unattributed_at_end`` were still held when a live read ended).
        Either makes the window incomplete (``complete: false``): its counts
        are of what it shows, and the app logged more.
        """
        evicted = unattributed = unresolved = 0
        cut = self.cut_records
        unresolved_pids: set[str] = set()
        if held is not None:
            window = logcat.app_window(
                held,
                self.app_package,
                system_server_pid=self._system_server_pid,
                package_uids=self._package_uid_lookup,
                running=self._running,
            )
            for record in window.records:
                self._keep(record, echo=False)
            self.app_pids, from_log = window.pids, window.pids_from_log
            unresolved, unresolved_pids = window.unresolved_lines, window.unresolved_pids or set()
        elif self._stream is not None:
            self.app_pids, from_log = set(self._stream.pids), self._stream.pids_from_log
            evicted, unattributed = self._stream.pending_evicted, self._stream.unattributed
            unresolved, unresolved_pids = (
                self._stream.unresolved_lines,
                self._stream.unresolved_pids,
            )
        else:
            from_log = set()

        def status(asked: bool, value) -> str:
            return "not needed" if not asked else ("known" if value is not None else "unknown")

        self.attribution = {
            "system_server_pid": self._system_server,
            "system_server": status(self._system_server_asked, self._system_server),
            "package_uids": sorted(self._package_uids) if self._package_uids else None,
            "package_uids_status": status(self._package_uid_asked, self._package_uids),
            "pids_from_pidof": sorted(self._running, key=int),
            "pidof_rejected": sorted(self._pidof_rejected, key=int),
            "pids_from_log": sorted(from_log, key=int),
            "pending_evicted": evicted,
            "unattributed_at_end": unattributed,
            "unresolved_lines": unresolved,
            "unresolved_pids": sorted(unresolved_pids, key=int),
            "complete": not (evicted or unresolved or cut),
        }
        if evicted:
            print(
                f"{evicted} line(s) of {self.app_package}'s processes were dropped before the "
                f"process was named: the live hold keeps {LOG_PENDING_CAP} lines "
                "(ANDROID_EMU_LOG_PENDING_CAP).",
                file=sys.stderr,
            )
        if unresolved:
            held_note = (
                f", {unattributed} of them still held when the read ended" if unattributed else ""
            )
            print(
                f"Incomplete: {unresolved} line(s) logged under {self.app_package}'s uid by "
                f"pid(s) {', '.join(sorted(unresolved_pids, key=int))} are left out{held_note}. "
                "Nothing in the window shows those processes were the app's -- a process "
                "started before the window opened and gone by its end, such as a crash read "
                "too late. The counts are of what is shown; widen --last to include its start.",
                file=sys.stderr,
            )

    def _keep(
        self,
        record: logcat.LogRecord,
        echo: bool,
        sequence: int | None = None,
        replayed: bool = False,
    ) -> None:
        """Process one record of the capture, a line at a time, and print it in follow mode.

        A record below the severity floor is not the capture's: the floor a
        ``*:<P>`` filterspec applied on the device before reads went binary.
        """
        if not self._above_floor(record.priority):
            return
        for line in record.lines():
            self.process_log_line(line, sequence)
            if echo:
                parsed = self.parse_logcat_line(line)
                if parsed and self.classify_log_line(parsed) in self.severity_filter:
                    # A held record is printed when it is decided, after newer ones.
                    print(f"[held back] {line}" if replayed else line)

    def _above_floor(self, priority: str) -> bool:
        """Whether ``*:<floor>`` would have shown a record of ``priority``.

        logcat's order is V < D < I < W < E < F < S; a priority it cannot name
        (printed ``?``) is below every floor.
        """
        if self._floor is None:
            return True
        if priority == "S":
            return True
        if priority not in logcat.PRIORITY_ORDER:
            return False
        return logcat.PRIORITY_ORDER.index(priority) >= logcat.PRIORITY_ORDER.index(self._floor)

    def stream_logs(
        self,
        follow: bool = False,
        duration: float | None = None,
        clear_first: bool = False,
        last_minutes: float | None = None,
    ) -> bool:
        """
        Stream logs from device/emulator (or dump a historical window).

        On API 33, whose logcat holds ``-B`` records back while it streams, a
        live read is made of dumps instead (``_read_by_dumps``); every other
        level streams. The level is asked once.

        Args:
            follow: Follow mode (continuous streaming)
            duration: Capture duration in seconds
            clear_first: Clear logcat buffer before streaming
            last_minutes: Dump logs from the last N minutes (historical window via
                ``logcat -d -t``) instead of streaming live

        Returns:
            True if successful

        Raises:
            cli.Failure: ``--app`` was given and no process of the app was found
                -- not by ``pidof``, and not in any record read. The capture is
                refused rather than reported as an empty (or, before D4, a
                whole-device) window.
            adb_exec.DeviceError: The device is not available -- before the
                read, or when a live read ended by itself or a dump did not
                end in ``DUMP_TIMEOUT`` (exit 3 at the boundary, never an
                empty capture).
            cli.Failure: A dump did not end in ``DUMP_TIMEOUT``, or a live read
                ended without being stopped, and the device is still there:
                what it read is not the window (a live read hands it over under
                ``partial``).
            common.logcat.LogFramingError: The stream is not logd's records.
        """
        self._verify_device_available()
        # The level, before anything touches the log: a device whose level is
        # unknown is refused before --clear could erase what it holds.
        dump_read = last_minutes is None and self._holds_binary_back()

        # Clear logcat if requested (ignored for historical windows, which read
        # the existing buffer rather than streaming new lines).
        if clear_first and last_minutes is None:
            # Ignore clear errors: an unclearable buffer is not worth failing
            # the capture over. Device-level errors still propagate.
            with contextlib.suppress(AdbCommandError):
                run_adb("logcat", self.device_serial, *logcat.logcat_args(clear=True), check=True)

        # Resolve the app's pids (device calls: pidof, then its uid checked)
        # then build the command (pure mapping).
        self._pidof_pids = self._resolve_app_pids()
        self._running = self._verify_running(self._pidof_pids)
        self.app_pids = set(self._running)
        if self.app_package and not self._running:
            state = "`pidof` failed" if self._pidof_failure else "none running is the package's"
            print(
                f"No process of {self.app_package} now ({state}); keeping the lines of any "
                f"of its processes the log names.",
                file=sys.stderr,
            )
        cmd = self.build_logcat_command(last_minutes=last_minutes)

        # A historical window is read whole before it is filtered: a process
        # already running when the window opens is named only at its end, by
        # system_server's `Killing` (recorded: logcat_app_restart_records),
        # after every record it logged. A live stream cannot wait for the end;
        # it learns pids as records arrive and holds back the package uid's
        # records of pids not yet named, up to LOG_PENDING_CAP lines, to keep
        # them if one is (see _take).
        held: list[logcat.LogRecord] | None = (
            [] if self.app_package and last_minutes is not None else None
        )
        self._stream = (
            logcat.AppWindowStream(
                self.app_package,
                system_server_pid=self._system_server_pid,
                package_uids=self._package_uid_lookup,
                running=self._running,
                pending_cap=LOG_PENDING_CAP,
            )
            if self.app_package and held is None
            else None
        )

        if dump_read:
            # Android 13's logcat: a stream would lose what it holds back.
            return self._conclude(held, self._read_by_dumps(follow=follow, duration=duration))

        # Setup signal handler for graceful interruption
        def signal_handler(sig, frame):
            self.interrupted = True
            if self.log_process:
                self.log_process.terminate()

        signal.signal(signal.SIGINT, signal_handler)

        stopped_by_us = False
        # A dump that its deadline stopped (_stop_overrun_dump). Not a stop of
        # the caller's asking: what it read is refused, not reported.
        overran = False
        deadline_timer: threading.Timer | None = None

        try:
            # stderr gets its own pipe *and* a reader. Leaving it unread
            # deadlocks the child once the buffer fills (~64KB); merging it into
            # stdout instead would feed adb's own error text through the log
            # parser and count it as device output. Draining it on a thread
            # avoids both, and keeps the text for diagnosis on failure.
            # stdout is bytes: logd's records, read a whole record at a time
            # (common.logcat.read_record), never split as text.
            self.log_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._stderr_lines = []
            self._start_stderr_drain()

            def _stop_at_deadline() -> None:
                nonlocal stopped_by_us
                stopped_by_us = True
                self.interrupted = True
                if self.log_process:
                    self.log_process.terminate()

            def _stop_overrun_dump() -> None:
                # Stopping the child closes the pipe the read is blocked on.
                # One that ignores SIGTERM is killed, so the read ends either
                # way. A dump that already exited is left alone: the read is
                # about to reach the end it wrote.
                nonlocal overran
                process = self.log_process
                if process is None or process.poll() is not None:
                    return
                overran = True
                process.terminate()
                try:
                    process.wait(timeout=STREAM_SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()

            # A live `adb logcat` never closes its pipe, so a read blocks
            # indefinitely on a quiet device. Checking the clock inside the loop
            # only helps while lines keep arriving; the timer is what guarantees
            # --duration actually returns. A dump closes its pipe when it is
            # done, unless adb waits for a device that went or the stream
            # stalls mid-record; its own deadline is what guarantees --last
            # returns. --follow (and no mode at all) runs until interrupted.
            if last_minutes is not None:
                deadline_timer = threading.Timer(float(DUMP_TIMEOUT), _stop_overrun_dump)
            elif duration:
                deadline_timer = threading.Timer(float(duration), _stop_at_deadline)
            if deadline_timer is not None:
                deadline_timer.daemon = True
                deadline_timer.start()

            start_time = datetime.now()

            cut: logcat.LogFramingError | None = None
            while True:
                try:
                    record = logcat.read_record(self.log_process.stdout)
                except logcat.LogFramingError as error:
                    # Ended inside a record: judged in _finish_stream -- a
                    # stop of ours loses that record, said so; anything else
                    # fails the capture. Not records at all: refused here.
                    if not error.truncated:
                        raise
                    cut = error
                    break
                if record is None:
                    break

                if held is not None:
                    held.append(record)
                else:
                    self._take(record, echo=follow)

                if duration and (datetime.now() - start_time).total_seconds() >= duration:
                    stopped_by_us = True
                    break

                if self.interrupted:
                    stopped_by_us = True
                    break

            # An interrupt terminates logcat, which can end the read before the
            # loop sees the flag: still a stop, not a stream that ended itself.
            stopped_by_us = stopped_by_us or self.interrupted
            finished = self._finish_stream(
                stopped_by_us, live=last_minutes is None, cut=cut, overran=overran
            )

        # No `except Exception` here any more: it printed the error and returned
        # False, so a device that vanished mid-stream exited 1 with no remedy
        # and no JSON. Whatever goes wrong now reaches common.cli.
        finally:
            if deadline_timer:
                deadline_timer.cancel()
            if self.log_process and self.log_process.poll() is None:
                self.log_process.terminate()

        return self._conclude(held, finished)

    def _conclude(self, held: list[logcat.LogRecord] | None, finished: bool) -> bool:
        """Settle ``--app``'s attribution once the read is over, and refuse a window without the app."""
        if self.app_package:
            self._settle_attribution(held)

        if self.app_package and not self.app_pids:
            # Not "no logs": the device's whole window is not the app's, and an
            # empty one would read as an app that ran and logged nothing.
            running = (
                f"`pidof` failed ({self._pidof_failure})"
                if self._pidof_failure
                else "`pidof` found none running"
            )
            cli.fail(
                f"{self.app_package} had no process in this capture: {running}, and no "
                f"record read names one of its processes.",
                "Launch the app and retry (or widen --last to when it ran); drop --app to "
                "read every process.",
            )
        return finished

    def _holds_binary_back(self) -> bool:
        """Whether the device's logcat is Android 13's, which holds ``-B`` records back.

        ``getprop ro.build.version.sdk``, asked once (recorded: getprop_sdk).
        A device that does not say -- getprop failing, or answering an empty
        line as it does for a property it lacks (getprop_unset) -- is refused:
        guessed to stream, an API 33 device read quietly as zero lines, the
        very success this read exists to prevent.

        Raises:
            cli.Failure: The level is unknown, or getprop did not answer in
                ``STATE_TIMEOUT`` (exit 1), with ``partial``.
            adb_exec.DeviceError: As getprop raised it, or -- after a timeout --
                as ``get-state`` then says (exit 3).
        """
        try:
            level = get_sdk_level(self.device_serial, timeout=STATE_TIMEOUT)
        except AdbTimeoutError as error:
            self._require_device()
            self._refuse(str(error), error.remedy, live=True)
        except UnknownSdkLevelError as error:
            self._refuse(
                f"{error} log_monitor reads Android 13's logcat differently from every other "
                "release's (it holds records back while it streams), so it does not read a "
                "device whose level it cannot tell.",
                error.remedy,
                live=True,
            )
        return level == logcat.BUFFERED_BINARY_SDK

    def _read_by_dumps(self, follow: bool, duration: float | None) -> bool:
        """A live read on API 33: a series of dumps one poll apart, never a stream.

        That release's logcat holds ``-B`` records back while it streams and
        loses them when it is stopped (common.logcat, ``read_after``; recorded:
        a 5 s stream delivered no byte while its window held records). A dump
        ends by itself and writes everything it read. So the read opens on the
        newest record a system process wrote (``-d -B -T 1``, doubled until it
        holds one: the cursor, ``common.logcat.open_cursor``; a buffer with
        none gets a marker line, ``_write_anchor``) and, every
        ``POLL_SECONDS`` whatever the mode, takes what logd delivered after the
        cursor and the records already read after it, and moves the cursor on
        -- printing as it goes under --follow -- then once more at the deadline
        or the interrupt. Each read follows logd's own order: a count-based
        tail (``-d -B -T N``), doubled until it reaches the cursor
        (``common.logcat.read_after``); no timestamp positions it, and no app's
        record does -- an app can resend a frame byte for byte, so the cursor
        is always a record a system process wrote, whose uid logd took from
        the socket. Each read covers about one poll interval, so the cursor
        stays inside logd's buffer while the window grows, and no dump grows
        with the window. No stream, no thread, no timer: each dump is bounded
        on this thread (``_dump``).

        What a capture is: what logd delivered. The cursor and the records
        after it found in a dump prove logd still held the window's start when
        that dump began; it
        does not prove the window complete -- logd may drop records it prunes
        while any reader is behind, as with any logd reader on any level. Zero
        lines means no record logd delivered after the cursor passed the
        filters (``--app``, ``--severity``).

        Returns:
            True: the read ended at its deadline or its interrupt.

        Raises:
            cli.Failure: A dump failed, overran its budget or its byte cap, or
                was not whole records; the cursor's record was in the whole
                buffer not at all, in a tail more than once, or only past
                what the byte cap allows -- refused, with what was read
                before it under ``partial`` (empty counts before the first
                read). One whose device went is also that device error (exit
                3).
        """
        stop = threading.Event()

        def interrupt(_signum, _frame) -> None:
            self.interrupted = True
            stop.set()

        signal.signal(signal.SIGINT, interrupt)
        self.anchor_written = False
        opened_within = min(NEWEST_TIMEOUT, duration) if duration else NEWEST_TIMEOUT
        cursor = self._open(opened_within)
        opened = last_read = time.monotonic()
        deadline = opened + duration if duration else None
        polls = 0
        while not stop.is_set():
            # Polls on a fixed schedule from the opening, so a slow dump does
            # not push every later one back.
            polls += 1
            due = opened + polls * POLL_SECONDS
            if deadline is not None and due >= deadline:
                due = deadline
            if stop.wait(max(0.0, due - time.monotonic())):
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            cursor, last_read = self._read_window(cursor, last_read, echo=follow)
        self._read_window(cursor, last_read, echo=follow)
        return True

    def _open(self, budget: float) -> logcat.Cursor:
        """The read's opening cursor (``common.logcat.open_cursor``), each dump within ``budget``.

        ``-d -B -T 1``, doubled until it holds a record a system process wrote;
        what followed that record was logged before the read opened. A buffer
        with no such record (empty, as after ``--clear``, or only apps') gets
        one: ``_write_anchor``, then found by its nonce
        (``common.logcat.find_anchor``). Refused, with ``partial``, if a dump
        is not records, the search would pass the byte cap, or the anchor is
        not found once.
        """

        def dump(args: list[str]) -> bytes:
            return self._dump(args, budget)

        try:
            cursor = logcat.open_cursor(dump, max_bytes=DUMP_CAP_BYTES)
            if cursor is None:
                cursor = logcat.find_anchor(dump, self._write_anchor(), max_bytes=DUMP_CAP_BYTES)
            return cursor
        except (
            logcat.CursorBeyondCapError,
            logcat.AnchorNotFoundError,
            logcat.AmbiguousCursorError,
        ) as error:
            self._refuse(str(error), error.remedy, live=True)
        except logcat.LogFramingError as error:
            if error.truncated:
                self._require_device()
            self._refuse(
                f"`adb logcat {' '.join(logcat.newest_args())}` (or a longer tail of it) did not "
                f"answer whole records ({error}).",
                logcat.LogFramingError.remedy,
                live=True,
            )

    def _write_anchor(self) -> str:
        """Write one marker line to the device's log, the read's anchor; return its message.

        ``adb shell log -t aes-log-monitor anchor-<128-bit hex>``: written as
        shell (uid 2000), a system writer no app can impersonate, with a nonce
        no one else has, so the frame is unique. Only when the buffer holds no
        system record to open on -- mostly after ``--clear``. Said on stderr,
        in the text summary and as ``anchor_written`` in the JSON.

        Raises:
            cli.Failure: The write failed or did not answer within
                ``STATE_TIMEOUT`` (exit 1), with ``partial``; one whose device
                went is also that device error (exit 3).
        """
        message = f"anchor-{secrets.token_hex(16)}"
        try:
            run_adb(
                "shell",
                self.device_serial,
                "log",
                "-t",
                quote_for_device_shell(logcat.ANCHOR_TAG),
                quote_for_device_shell(message),
                check=True,
                timeout=STATE_TIMEOUT,
            )
        except DeviceError as error:
            self._device_gone(error)
        except AdbError as error:
            self._refuse(
                f"The log held no record a system process wrote to start from, and `adb shell "
                f"log` could not write one ({error}).",
                getattr(error, "remedy", None) or "Check adb and the device, then retry.",
                live=True,
            )
        self.anchor_written = True
        print(
            f"The log held no record a system process wrote to start from: wrote one marker "
            f"line ({logcat.ANCHOR_TAG}: {message}).",
            file=sys.stderr,
        )
        return message

    def _read_window(
        self, cursor: logcat.Cursor, last_read: float, echo: bool
    ) -> tuple[logcat.Cursor, float]:
        """What logd delivered after ``cursor``, taken into the capture in order.

        ``common.logcat.read_after`` finds the cursor in a tail of logd's order:
        first the larger of ``INITIAL_TAIL`` and twice the records the previous
        read took, doubled while it does not reach the cursor. Each dump's
        budget is ``DUMP_FLOOR`` plus the seconds since the last read, never
        more than ``DUMP_TIMEOUT``. Every failure is a refusal carrying what
        was read before it (``partial``), or exit 3 for a device gone. A dump
        reaches ``read_after``'s judgement only after the device has answered
        (``_dump``), so a vanished device is exit 3, never a cursor "lost" to
        a buffer that turned over.
        """
        now = time.monotonic()
        budget = min(DUMP_TIMEOUT, DUMP_FLOOR + (now - last_read))
        try:
            records, cursor = logcat.read_after(
                cursor,
                lambda args: self._dump(args, budget),
                tail=self._tail,
                max_bytes=DUMP_CAP_BYTES,
            )
        except (
            logcat.CursorLostError,
            logcat.AmbiguousCursorError,
            logcat.CursorBeyondCapError,
        ) as error:
            self._refuse(str(error), error.remedy, live=True)
        except logcat.LogFramingError as error:
            if error.truncated:
                self._require_device()
            self._refuse(
                f"`adb logcat` did not answer whole records ({error}), so what it read is not "
                "the window.",
                logcat.LogFramingError.remedy,
                live=True,
            )
        # The next tail must reach the cursor past its suffix, then about as
        # many new records as this read took, twice over.
        self._tail = max(logcat.INITIAL_TAIL, 1 + len(cursor.suffix) + 2 * len(records))
        for record in records:
            self._take(record, echo)
        return cursor, now

    def _dump(self, args: list[str], timeout: float) -> bytes:
        """Run ``adb logcat <args>`` -- a dump, which ends by itself -- and return what it wrote.

        Its output and stderr go to temporary files, and this thread waits
        for it to exit in ``DUMP_WAIT_SECONDS`` steps, the deadline checked
        first, then its exit, then the output's size against
        ``DUMP_CAP_BYTES``: no pipe to block on, no ``select`` (which takes no
        pipe on Windows), no timer. Its own session, so a Ctrl-C meant for the
        read does not cut the dump that ends it.

        However the wait ends, the same steps follow, in this order, none
        skipped:

        1. Stop: still running (its deadline, the cap, an output closed early,
           the wait itself failing) -> killed, then reaped within
           ``KILL_REAP_TIMEOUT``, with SIGINT held off meanwhile where the
           platform can (``_sigint_held``). Whatever raises between the start
           and here, the stop still runs: an ``OSError`` of the wait is this
           dump's outcome (the wait failed); anything else (Ctrl-C) is raised
           again once the child is stopped -- or, if it could not be, once
           that is said on stderr, still under the mask.
        2. Read bounded: stderr's last ``SAID_TAIL_BYTES``; the output only for
           a dump that exited 0 in time under the cap. A read that fails is a
           fact of the ending, not an exception out of it.
        3. adb's own words: a device error in that stderr, whatever the exit
           status -> exit 3, with ``partial``. Skipped, and said, when stderr
           could not be read.
        4. ``get-state`` (``_require_device``): gone -> exit 3; any other
           failure of it -> a refusal at exit 1; both with ``partial``.
        5. Verdict: could not be stopped, the wait failed, stderr unreadable,
           cap exceeded, deadline passed (by the time its exit was seen), a
           non-zero exit, output unreadable -> refused, with ``partial`` and
           every fact gathered; otherwise its output is the dump's.
        """
        command = build_adb_command("logcat", self.device_serial, *args)
        shown = " ".join(command[command.index("logcat") :])
        deadline = time.monotonic() + timeout
        returncode: int | None = None
        exited_in_time = capped = False
        wait_failed: OSError | None = None
        said_failed: OSError | None = None
        out_failed: OSError | None = None
        said = ""
        data = b""
        with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as said_file:
            process = subprocess.Popen(
                command,
                stdout=out_file,
                stderr=said_file,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        returncode = process.wait(timeout=min(DUMP_WAIT_SECONDS, left))
                    exited_in_time = returncode is not None and time.monotonic() <= deadline
                    if os.fstat(out_file.fileno()).st_size > DUMP_CAP_BYTES:
                        capped = True
                        break
                    if returncode is not None:
                        break
            except OSError as error:
                wait_failed = error
            except BaseException:
                # 1. Stop, on the way out of an interruption -- and say so,
                # still with SIGINT held, if it failed: a second Ctrl-C would
                # otherwise arrive first and the line would never be printed.
                with _sigint_held():
                    stopped, gone = self._stop(process)
                    if not gone:
                        print(
                            f"`adb {shown}` could not be stopped (pid {process.pid}: {stopped}).",
                            file=sys.stderr,
                        )
                raise
            # 1. Stop.
            with _sigint_held():
                stopped, gone = self._stop(process)
            # 2. Read bounded; the output only for a dump that can be accepted.
            try:
                said = _tail(said_file, SAID_TAIL_BYTES)
            except OSError as error:
                said_failed = error
            if exited_in_time and returncode == 0 and not capped and wait_failed is None:
                try:
                    if os.fstat(out_file.fileno()).st_size > DUMP_CAP_BYTES:
                        capped = True
                    else:
                        out_file.seek(0)
                        data = out_file.read()
                except OSError as error:
                    out_failed = error
        detail = " ".join(said.split())[-300:]
        facts = "" if gone else f" `adb {shown}` {stopped}."
        if wait_failed is not None:
            facts += f" Waiting for `adb {shown}` failed ({wait_failed})."
        if said_failed is not None:
            facts += (
                f" Its stderr could not be read ({said_failed}), so adb's own words were not "
                "checked."
            )
        if out_failed is not None:
            facts += f" It exited 0, but its output could not be read ({out_failed})."
        # 3. adb's own words, whatever the exit status -- when there are words.
        if said_failed is None:
            device_error = classify_device_error(said, self.device_serial)
            if device_error is not None:
                self._device_gone(device_error, note=facts)
        # 4. The device, whatever the outcome.
        self._require_device(note=facts)
        # 5. The verdict.
        retry = "Retry; if it repeats, restart adb (`adb kill-server && adb start-server`)."
        if facts:
            self._refuse(
                facts.strip() + " What it read is not reported as the window.", retry, live=True
            )
        if capped:
            self._refuse(
                f"`adb {shown}` wrote more than {DUMP_CAP_BYTES} bytes and {stopped}; it is not "
                "read, and not reported as the window.",
                "Capture for less time, or raise ANDROID_EMU_LOG_DUMP_CAP_BYTES.",
                live=True,
            )
        if not exited_in_time:
            self._refuse(
                f"`adb {shown}` did not finish within {timeout:g}s and {stopped}, so what it "
                "read is not reported as the window." + (f" adb said: {detail}" if detail else ""),
                retry,
                live=True,
            )
        if returncode:
            self._refuse(
                f"`adb {shown}` exited with status {returncode}."
                + (f" adb said: {detail}" if detail else ""),
                "Check the device is connected (`adb devices`) and retry.",
                live=True,
            )
        return data

    @staticmethod
    def _stop(process: subprocess.Popen) -> tuple[str, bool]:
        """Stop a dump that is still running; how it ended, and whether it is gone.

        Never raises an ``OSError``: a failed ``poll``, ``kill`` or reap is
        part of the answer.

        Returns:
            ``("exited (exit N)", True)`` by itself; ``("was killed (exit N)",
            True)``, the status the reap returned; or ``("could not be stopped
            ...", False)`` -- the kill failed, the reap failed, or it was still
            running after the kill.
        """
        with contextlib.suppress(OSError):
            status = process.poll()
            if status is not None:
                return f"exited (exit {status})", True
        try:
            process.kill()
        except OSError as error:
            return f"could not be stopped ({error})", False
        try:
            return f"was killed (exit {process.wait(timeout=KILL_REAP_TIMEOUT)})", True
        except subprocess.TimeoutExpired:
            return "could not be stopped: still running after it was killed", False
        except OSError as error:
            return f"could not be stopped: its reap failed ({error})", False

    def _require_device(self, note: str = "") -> None:
        """``get-state`` for the dump-made read, every failure of it a refusal with ``partial``.

        Gone (``DeviceError``) is exit 3; any other failure of the question
        itself -- it timed out, adb failed -- is a refusal at exit 1. The one
        place the dump-made read asks, so no caller can ask it unwrapped.
        """
        try:
            self._verify_device_available()
        except DeviceError as error:
            self._device_gone(error, note=note)
        except AdbError as error:
            self._refuse(
                f"`adb get-state` failed after a dump ({error}), so the dump is not reported "
                f"as the window.{note}",
                getattr(error, "remedy", None) or "Check adb and the device, then retry.",
                live=True,
            )

    def _device_gone(self, error: DeviceError, note: str = "") -> NoReturn:
        """Raise ``error`` as a refusal that is also that device error: exit 3, with ``partial``.

        ``common.cli`` hands ``details`` over only for a ``cli.Failure`` and
        exits 3 only for a device-unavailable error; this is both, as
        app_state_capture's partial snapshot is.
        """
        if self.app_package and self.attribution is None:
            self._settle_attribution(None)
        raise _device_refusal_type(type(error))(
            f"{error}{note} It had read {self.total_lines} line(s): under `partial` (--json), "
            "not reported as the capture.",
            getattr(error, "remedy", None) or "Reconnect the device and retry.",
            {"partial": self.get_json_output()},
        ) from error

    def _verify_device_available(self) -> None:
        """Ask adb whether the device this read targets is there, before trusting a read.

        A streaming ``adb logcat`` aimed at a device that is not attached does
        not fail: it prints ``- waiting for device -`` and waits (recorded:
        adb_waiting_for_device). With ``--duration`` that looked exactly like
        a device that logged nothing -- the deadline stopped it and the capture
        reported success with zero lines, with or without ``--serial``. So
        every mode asks ``adb get-state`` first (bounded), and asks again when
        a live read ends by itself: older adb exits 0 when the transport goes,
        current adb 255.

        Raises:
            DeviceNotFoundError, DeviceOfflineError, DeviceUnauthorizedError:
                Typed from adb's own words by ``run_adb`` -- exit 3 at the CLI
                boundary (recorded: adb_device_not_found). A state other than
                ``device`` (recovery, bootloader) is offline for logcat's
                purposes.
            MultipleDevicesError: Several attached and no serial (exit 1).
            AdbCommandError: get-state failed in a way adb did not name.
        """
        result = run_adb("get-state", self.device_serial, check=True, timeout=STATE_TIMEOUT)
        state = result.stdout.strip()
        if state != "device":
            target = f"Device {self.device_serial!r}" if self.device_serial else "The device"
            raise DeviceOfflineError(
                f"{target} is in state {state or 'unknown'!r}, not `device`; logcat cannot "
                f"read it.",
                remedy="Wait for it to boot into Android (`adb wait-for-device`), then retry.",
            )

    def _start_stderr_drain(self) -> None:
        """Continuously read the child's stderr on a daemon thread.

        An unread ``stderr=PIPE`` blocks the child as soon as the OS pipe buffer
        fills, which on a long capture looks like a hang with no explanation.
        The text is retained so a failure can say what adb actually reported.
        """
        stream = self.log_process.stderr if self.log_process else None
        if stream is None:
            return

        def _drain() -> None:
            with contextlib.suppress(ValueError, OSError):
                for line in iter(stream.readline, b""):
                    if not line:
                        break
                    self._stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()
        self._stderr_thread = thread

    def _finish_stream(
        self,
        stopped_by_us: bool,
        live: bool = True,
        cut: logcat.LogFramingError | None = None,
        overran: bool = False,
    ) -> bool:
        """Reap the logcat process and decide whether the capture succeeded.

        Args:
            stopped_by_us: True if the duration elapsed or the user interrupted,
                in which case a terminated process is the expected outcome.
            live: A following read (``-T``), which never ends by itself while
                its device is there. A dump (``--last``) always does. A live
                read that ends without being stopped -- however adb exited, and
                cut or not -- is asked about its device (gone: exit 3) and is
                otherwise refused, with what it read under ``partial``: it
                cannot show it observed what was asked. It used to be the
                capture, with a note on stderr.
            cut: The stream ended inside a record. After a stop of ours that
                record is lost and the loss disclosed (``cut_records``). A dump
                or a stream that ended by itself never cuts a record: the
                device is asked again (gone: exit 3), and otherwise the
                capture is refused -- the recorded two-crash dump short by one
                byte was a success with half its errors (review of PR #35).
            overran: A dump was stopped by its own deadline (``DUMP_TIMEOUT``),
                not by the caller. What it read is not the window, cut or not:
                the device is asked again (gone: exit 3), and otherwise the
                dump is refused.

        Returns:
            True if the capture completed as intended.

        Raises:
            adb_exec.DeviceError: adb's stderr names a device problem, or --
                for a read that ended by itself or a dump that overran --
                ``get-state`` says the device is no longer available. Exit 3,
                never a quiet capture.
            cli.Failure: logcat exited non-zero, a dump overran, or a live read
                ended by itself, and the device is still there. A live read's
                refusal carries what it read (``details["partial"]``).

        Never waits without a timeout: a streaming `adb logcat` does not exit on
        its own, so an unbounded ``wait()`` here is an unconditional hang.
        """
        process = self.log_process
        if process is None:
            return False

        if stopped_by_us and process.poll() is None:
            process.terminate()

        try:
            returncode = process.wait(timeout=STREAM_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=STREAM_SHUTDOWN_TIMEOUT)
            returncode = process.poll()

        if overran:
            # Its exit status is our stop's, and what it read is not the
            # window. A dump waits for a device that is not there, as a live
            # read does (measured: `adb -s <unknown> logcat -d -B` prints
            # adb_waiting_for_device's banner and does not exit), so whether
            # the device went is asked first: if it did, that is the answer.
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=STREAM_SHUTDOWN_TIMEOUT)
            self._verify_device_available()
            said = " ".join("\n".join(self._stderr_lines).split())
            cli.fail(
                f"`adb logcat -d` did not finish within {DUMP_TIMEOUT}s and was stopped, so "
                "what it read is not reported as the window."
                + (f" adb said: {said}" if said else ""),
                "Retry, or narrow --last; if it repeats, restart adb (`adb kill-server && adb "
                "start-server`), or raise ANDROID_EMU_ADB_TIMEOUT (the dump gets twice it).",
            )

        # We terminated it deliberately, so its exit status says nothing about
        # whether the capture worked; a record it was sending is lost, and said.
        if stopped_by_us:
            if cut is not None:
                self.cut_records += 1
                print(
                    "The read was stopped while a record was arriving; that record is not "
                    "in the capture.",
                    file=sys.stderr,
                )
            return True

        # The drain thread may still be reading what adb said on the way out.
        if (returncode or live) and self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_SHUTDOWN_TIMEOUT)
        # adb's own words, typed as run_adb would type them: a device that is
        # not there is exit 3 at the boundary here too. Classified with its
        # line breaks: adb's error is recognised only at the start of a line,
        # and a banner before it (`- waiting for device -`) moved it off the
        # start when the lines were joined with spaces first (A5).
        stderr = "\n".join(self._stderr_lines)
        if returncode:
            device_error = classify_device_error(stderr, self.device_serial)
            if device_error is not None:
                raise device_error
        if returncode or live or cut is not None:
            # Ended by itself: a dump that failed or was cut, or a live read,
            # which ends only when something went away. If the device did,
            # that is the answer (exit 3), not an empty or partial capture.
            self._verify_device_available()
        if returncode:
            detail = " ".join(stderr.split())
            # Raised, not printed: under --json this exited 1 with no object.
            message = f"adb logcat exited with status {returncode}."
            if detail:
                message += f" {detail}"
            self._refuse(
                message,
                "Check the device is connected (`adb devices`) and, with more than one "
                "attached, pass --serial.",
                live=live,
            )
        if cut is not None:
            self._refuse(
                f"adb logcat's output ended inside a record ({cut}), so the capture is "
                "incomplete: what it read is not reported as the window.",
                "Retry; if it repeats, the adb connection is cutting the stream -- restart it "
                "with `adb kill-server && adb start-server`.",
                live=live,
            )
        if live:
            # Ended by itself, no failure status, the device still there. That used
            # to be the capture -- success, with a note on stderr -- but a live
            # read ends only when it is stopped, so an end of its own cannot
            # show the observation asked for was made: a refusal, as
            # anr_watcher --watch refuses the same end (wave 4's rule).
            status = (
                "exit status 0"
                if returncode == 0
                else f"no exit status within {STREAM_SHUTDOWN_TIMEOUT}s"
            )
            self._refuse(
                f"adb logcat ended the live read by itself ({status}) while the device is "
                "still attached. A live read ends only when it is stopped (--duration, an "
                "interrupt), so what it read is not the capture asked for.",
                "Retry; if it repeats, restart adb (`adb kill-server && adb start-server`).",
                live=True,
            )
        return True

    def _refuse(self, error: str, remedy: str, *, live: bool) -> NoReturn:
        """Refuse the capture -- a live read handing over what it read, under ``partial``.

        A live read that ended by itself cannot show it observed what was asked,
        but what it did read is evidence and is kept: the JSON summary, its
        attribution settled, in the refusal object (``--json``), and the count
        in the error itself, since the text form prints no details. A dump's
        refusal is its own words alone: what it read is not the window.
        """
        if not live:
            cli.fail(error, remedy)
        if self.app_package and self.attribution is None:
            self._settle_attribution(None)
        cli.fail(
            f"{error} It had read {self.total_lines} line(s): under `partial` (--json), "
            "not reported as the capture.",
            remedy,
            details={"partial": self.get_json_output()},
        )

    def get_summary(self, verbose: bool = False) -> str:
        """
        Get log summary.

        Args:
            verbose: Include full log details

        Returns:
            Formatted summary string
        """
        lines = []

        # Header
        if self.app_package:
            lines.append(f"Logs for: {self.app_package} (pids: {', '.join(self._app_pid_list())})")
        else:
            lines.append("Logs for: All processes")

        # Statistics
        lines.append(f"Total lines: {self.total_lines}")
        lines.append(
            f"Errors: {self.error_count} ({len(self.errors)} unique), "
            f"Warnings: {self.warning_count} ({len(self.warnings)} unique), "
            f"Info: {self.info_count}"
        )
        # In the summary itself, not only on stderr: counts that leave the app's
        # lines out must not read as the app's whole window (A3).
        left_out = self.attribution and (
            self.attribution["unresolved_lines"] + self.attribution["pending_evicted"]
        )
        if left_out:
            pids = ", ".join(self.attribution["unresolved_pids"]) or "the held ones"
            lines.append(
                f"Attribution: INCOMPLETE -- {left_out} line(s) of the app's uid left out "
                f"(pids: {pids}); the counts above are of what is shown."
            )
        if self.cut_records:
            lines.append(
                f"INCOMPLETE: the read was stopped inside {self.cut_records} record(s), "
                "which are not counted."
            )
        if self.anchor_written:
            lines.append(
                f"Anchor written: the log held no record a system process wrote to start from, "
                f"so one marker line ({logcat.ANCHOR_TAG}) was written to the device's log."
            )

        # Top issues
        if self.errors:
            lines.append(f"\nTop Errors ({len(self.errors)} unique):")
            for error in self.errors[:LOG_TEXT_SUMMARY_CAP]:
                lines.append(f"  ❌ {error[:LOG_LINE_MAX]}")  # Truncate long lines

        if self.warnings:
            lines.append(f"\nTop Warnings ({len(self.warnings)} unique):")
            for warning in self.warnings[:LOG_TEXT_SUMMARY_CAP]:
                lines.append(f"  ⚠️  {warning[:LOG_LINE_MAX]}")

        # Verbose output
        if verbose and self.log_lines:
            lines.append("\n=== Recent Log Lines ===")
            for line in self.log_lines[-LOG_TAIL:]:
                lines.append(line)

        return "\n".join(lines)

    def _app_pid_list(self) -> list[str]:
        """The app's pids in this capture, oldest process first."""
        return sorted(self.app_pids, key=int)

    def get_json_output(self) -> dict:
        """Get log results as JSON.

        ``errors`` / ``warnings`` under ``statistics`` count lines; the
        ``unique_*`` counts are the distinct messages, which the top-level
        ``errors`` / ``warnings`` lists hold (capped at ``LOG_JSON_CAP``).
        ``app_pids`` is every process of ``app_package`` in the window -- more
        than one after a restart -- and null without ``--app``. ``attribution``
        says what decided it (see ``_settle_attribution``): whether
        system_server's pid and the package's uid were known, which pids came
        from ``pidof`` (uid-checked) and which from the log, how many lines a
        live read dropped, and how many lines of the package's uid were left
        out for want of evidence (``unresolved_lines`` / ``unresolved_pids``).
        ``complete`` is false when either is nonzero, or when the read was
        stopped inside a record (``statistics.cut_records``, in every mode).
        Null without ``--app``. ``anchor_written``, only on API 33's dump-made
        read: whether the read wrote its own marker line to the device's log
        (``_write_anchor``).
        """
        output = {
            "app_package": self.app_package,
            "app_pids": self._app_pid_list() if self.app_package else None,
            "attribution": self.attribution,
            "device_serial": self.device_serial,
            "statistics": {
                "total_lines": self.total_lines,
                "errors": self.error_count,
                "unique_errors": len(self.errors),
                "warnings": self.warning_count,
                "unique_warnings": len(self.warnings),
                "info": self.info_count,
                "debug": self.debug_count,
                "verbose": self.verbose_count,
                "cut_records": self.cut_records,
            },
            "errors": self.errors[:LOG_JSON_CAP],
            "warnings": self.warnings[:LOG_JSON_CAP],
            "sample_logs": self.log_lines[-LOG_TAIL:],
        }
        if self.anchor_written is not None:
            output["anchor_written"] = self.anchor_written
        return output

    def save_logs(self, output_dir: str) -> str:
        """
        Save logs to file.

        Args:
            output_dir: Directory to save logs

        Returns:
            Path to saved log file
        """
        # Create output directory
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        app_name = self.app_package.split(".")[-1] if self.app_package else "device"
        log_file = output_path / f"{app_name}-{timestamp}.log"

        # Write all log lines
        with open(log_file, "w") as f:
            f.write("\n".join(self.log_lines))

        # Also save JSON summary
        json_file = output_path / f"{app_name}-{timestamp}-summary.json"
        with open(json_file, "w") as f:
            json.dump(self.get_json_output(), f, indent=2)

        return str(log_file)


def _severity_list(text: str) -> list[str]:
    """``--severity``'s type: a usage error (exit 2) for a name outside the set.

    Checked by argparse, so a refused filter never reaches the device. An
    unknown name used to be passed through and match nothing (D1).
    """
    try:
        return logcat.parse_severities(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Monitor and analyze Android device/emulator logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Also reachable as `python scripts/logs.py tail ...` — the unified log entry
point, which routes here unchanged. Both invocations are supported.

Examples:
  # Monitor app in real-time
  python scripts/log_monitor.py --app com.myapp --follow

  # Capture logs for 30 seconds
  python scripts/log_monitor.py --app com.myapp --duration 30s

  # Show errors/warnings only
  python scripts/log_monitor.py --severity error,warning --duration 1m

  # Inspect the last 5 minutes of history (logcat -d -t)
  python scripts/log_monitor.py --app com.myapp --last 5m

  # Save logs to file
  python scripts/log_monitor.py --app com.myapp --duration 1m --output logs/

  # Clear logs first, then monitor
  python scripts/log_monitor.py --app com.myapp --clear --follow
        """,
    )

    # Filtering options
    parser.add_argument(
        "--app", dest="app_package", help="App package name to filter logs (e.g., com.myapp)"
    )
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument(
        "--severity",
        type=_severity_list,
        help=(
            "Comma-separated severities to keep, from: "
            f"{','.join(logcat.SEVERITY_NAMES)} (default: error,warning,info,debug). "
            "Any other name is a usage error"
        ),
    )

    # Time options
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--follow", action="store_true", help="Follow mode (continuous streaming)"
    )
    time_group.add_argument("--duration", help="Capture duration (e.g., 30s, 5m, 1h)")
    time_group.add_argument(
        "--last",
        dest="last_window",
        help="Dump a historical window from the last N (e.g., 5m, 1h) via 'logcat -d -t'",
    )

    # Output options
    parser.add_argument("--output", help="Save logs to directory")
    parser.add_argument("--verbose", action="store_true", help="Show detailed output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--clear", action="store_true", help="Clear logcat buffer before streaming")

    args = parser.parse_args(argv)

    # Initialize monitor
    monitor = LogMonitor(
        app_package=args.app_package,
        device_serial=args.device_serial,
        severity_filter=args.severity,
    )

    # Parse duration. `--duration 30` (no unit) raised ValueError out of
    # parse_duration and reached the terminal as a traceback (D7); it is a bad
    # argument, so it is argparse's to report -- exit 2, with the accepted
    # forms in the message, which the parser's own error already names.
    duration = None
    if args.duration:
        try:
            duration = monitor.parse_time_duration(args.duration)
        except ValueError as error:
            parser.error(f"--duration: {error}")

    # Parse historical window (--last); convert seconds -> minutes for logcat -t
    last_minutes = None
    if args.last_window:
        try:
            last_minutes = monitor.parse_time_duration(args.last_window) / 60
        except ValueError as error:
            parser.error(f"--last: {error}")

    # Stream logs
    if last_minutes is not None:
        print("Reading historical logs...", file=sys.stderr)
    else:
        print("Monitoring logs...", file=sys.stderr)
    if args.app_package:
        print(f"App: {args.app_package}", file=sys.stderr)

    # Device-level failures (ambiguous serial, offline, adb missing) raise and
    # reach common.cli with their remedy. False is left for one case: logcat
    # ran and exited non-zero, which _finish_stream reports on stderr.
    success = monitor.stream_logs(
        follow=args.follow,
        duration=duration,
        clear_first=args.clear,
        last_minutes=last_minutes,
    )
    if not success:
        sys.exit(1)

    # Save logs if requested
    if args.output:
        log_file = monitor.save_logs(args.output)
        print(f"\nLogs saved to: {log_file}", file=sys.stderr)

    # Output results
    if not args.follow:  # Don't show summary in follow mode
        if args.json:
            print(json.dumps(monitor.get_json_output(), indent=2))
        else:
            print("\n" + monitor.get_summary(verbose=args.verbose))

    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
