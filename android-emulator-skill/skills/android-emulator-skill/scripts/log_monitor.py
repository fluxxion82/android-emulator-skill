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
import json
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from common import cli, logcat
from common.adb_exec import (
    AdbCommandError,
    DeviceNotFoundError,
    classify_device_error,
    run_adb,
)
from common.device_utils import (
    get_connected_devices,
    get_package_uids,
    get_pid_uids,
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

        ``-v threadtime`` (the shared default) is mandatory, not cosmetic:
        ``parse_logcat_line`` expects "MM-DD HH:MM:SS.mmm PID TID P Tag: msg".
        The ``time`` format omits the PID/TID columns and writes "P/Tag( PID):"
        instead, which that regex cannot match -- so every line landed in the
        unparsed branch, severity counts stayed at zero and ``--follow`` printed
        nothing at all.

        No ``--pid``: ``--app`` is filtered here, on the host (``_is_apps``).
        One ``--pid`` is one process, so after a crash and restart it kept only
        the new process and dropped the one that crashed (D4). Under a severity
        floor above I, ``ActivityManager:I`` is kept so the lines that name the
        app's processes still arrive.

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

        min_priority = self._severity_min_priority()
        tag_priorities: tuple[tuple[str, str], ...] = ()
        if (
            self.app_package
            and min_priority is not None
            and logcat.PRIORITY_ORDER.index(min_priority)
            > logcat.PRIORITY_ORDER.index(logcat.PROCESS_TAG_PRIORITY)
        ):
            tag_priorities = ((logcat.PROCESS_TAG, logcat.PROCESS_TAG_PRIORITY),)

        # `-v usec` on every read, so a line's header tells a continuation of
        # a multi-line message apart from a new message (common.logcat
        # USEC_FORMAT) -- and so one read has one format, with --app or not.
        # A live read starts at the end of the buffer (`-T 1`, as anr_watcher's
        # live stream does): without it `adb logcat` first replays the whole
        # ring buffer, and an --app read opened the running pid's life before
        # that history, so an EARLIER owner of the pid had its lines kept
        # (security review of #27). History is --last's job.
        return logcat.build_logcat_command(
            self.device_serial,
            since=since,
            min_priority=min_priority,
            tag_priorities=tag_priorities,
            usec=True,
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

    def _take(self, line: str, echo: bool) -> None:
        """Take one line of a live read into the capture.

        Without ``--app`` every line is the capture's. With it, the live
        reader (``common.logcat.AppWindowStream``) decides: a line from one of
        the app's processes, inside that process's life, is kept; a line from
        a pid not yet decided is held back and kept later if the pid is named
        (a process already running when the read began is named only when it
        is killed). Held lines arrive late and are stored at their place in
        input order; ``--follow`` prints them when they are decided, marked.
        """
        if self._stream is None:
            self._keep(line, echo)
            return
        for sequence, kept, replayed in self._stream.feed(line):
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

    def _settle_attribution(self, held: list[str] | None) -> None:
        """Decide a held (historical) window, and record what decided the window.

        Sets ``app_pids`` and ``attribution``. The live counts are reported on
        stderr when nonzero: lines of the app that the live hold dropped at its
        cap before the app's process was named (``pending_evicted``), and lines
        still held when the read ended, from processes it never decided
        (``unattributed_at_end`` -- mostly other processes' lines; it says how
        much the read could not decide, not how much of the app was lost).
        """
        evicted = unattributed = 0
        if held is not None:
            window = logcat.app_window(
                "\n".join(held),
                self.app_package,
                system_server_pid=self._system_server_pid,
                package_uids=self._package_uid_lookup,
                running=self._running,
            )
            for line in window.lines:
                self._keep(line, echo=False)
            self.app_pids, from_log = window.pids, window.pids_from_log
        elif self._stream is not None:
            self.app_pids, from_log = set(self._stream.pids), self._stream.pids_from_log
            evicted, unattributed = self._stream.pending_evicted, self._stream.unattributed
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
        }
        if evicted:
            print(
                f"{evicted} line(s) of {self.app_package}'s processes were dropped before the "
                f"process was named: the live hold keeps {LOG_PENDING_CAP} lines "
                "(ANDROID_EMU_LOG_PENDING_CAP).",
                file=sys.stderr,
            )
        if unattributed:
            print(
                f"{unattributed} line(s) were still held when the read ended, from processes "
                f"never shown to be {self.app_package}'s; not shown.",
                file=sys.stderr,
            )

    def _keep(
        self, line: str, echo: bool, sequence: int | None = None, replayed: bool = False
    ) -> None:
        """Process one line of the capture, and print it in follow mode."""
        self.process_log_line(line, sequence)
        if echo:
            parsed = self.parse_logcat_line(line)
            if parsed and self.classify_log_line(parsed) in self.severity_filter:
                # A held line is printed when it is decided, after newer ones.
                print(f"[held back] {line}" if replayed else line)

    def stream_logs(
        self,
        follow: bool = False,
        duration: float | None = None,
        clear_first: bool = False,
        last_minutes: float | None = None,
    ) -> bool:
        """
        Stream logs from device/emulator (or dump a historical window).

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
                -- not by ``pidof``, and not in any line read. The capture is
                refused rather than reported as an empty (or, before D4, a
                whole-device) window.
        """
        self._verify_device_available()

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
        # system_server's `Killing` (recorded: logcat_app_restart_usec), after every
        # line it logged. A live stream cannot wait for the end; it learns pids
        # as lines arrive and holds back the lines of pids not yet named, up to
        # LOG_PENDING_CAP, to keep them if one is (see _take).
        held: list[str] | None = [] if self.app_package and last_minutes is not None else None
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

        # Setup signal handler for graceful interruption
        def signal_handler(sig, frame):
            self.interrupted = True
            if self.log_process:
                self.log_process.terminate()

        signal.signal(signal.SIGINT, signal_handler)

        stopped_by_us = False
        deadline_timer: threading.Timer | None = None

        try:
            # stderr gets its own pipe *and* a reader. Leaving it unread
            # deadlocks the child once the buffer fills (~64KB); merging it into
            # stdout instead would feed adb's own error text through the log
            # parser and count it as device output. Draining it on a thread
            # avoids both, and keeps the text for diagnosis on failure.
            # Bytes, split on b"\n" only (readline on a binary pipe): a text
            # pipe also breaks lines at "\r", which logcat leaves raw inside an
            # app's message -- an app could append "\r<forged header>" and have
            # it read as a line of its own (security review of #27).
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

            # A live `adb logcat` never closes its pipe, so readline() blocks
            # indefinitely on a quiet device. Checking the clock inside the loop
            # only helps while lines keep arriving; the timer is what guarantees
            # --duration actually returns.
            if duration:
                deadline_timer = threading.Timer(float(duration), _stop_at_deadline)
                deadline_timer.daemon = True
                deadline_timer.start()

            start_time = datetime.now()

            for raw in iter(self.log_process.stdout.readline, b""):
                if not raw:
                    break
                line = logcat.decode_log_line(raw).rstrip()

                if held is not None:
                    held.append(line)
                else:
                    self._take(line, echo=follow)

                if duration and (datetime.now() - start_time).total_seconds() >= duration:
                    stopped_by_us = True
                    break

                if self.interrupted:
                    stopped_by_us = True
                    break

            finished = self._finish_stream(stopped_by_us)

        # No `except Exception` here any more: it printed the error and returned
        # False, so a device that vanished mid-stream exited 1 with no remedy
        # and no JSON. Whatever goes wrong now reaches common.cli.
        finally:
            if deadline_timer:
                deadline_timer.cancel()
            if self.log_process and self.log_process.poll() is None:
                self.log_process.terminate()

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
                f"line read names one of its processes.",
                "Launch the app and retry (or widen --last to when it ran); drop --app to "
                "read every process.",
            )
        return finished

    def _verify_device_available(self) -> None:
        """Check an explicitly requested serial is actually attached.

        ``adb -s <serial> logcat`` does not fail when the serial is unknown --
        it blocks waiting for that device to appear. With ``--duration`` set,
        that looks exactly like a device which simply logged nothing: the
        capture ends on schedule and reports success with zero lines. Exit
        status cannot distinguish the two, so the check has to happen up front.

        Raises:
            DeviceNotFoundError: The serial is not attached -- exit 3 at the CLI
                boundary, like an unavailable device in every other script.
                (This printed a line and returned False, which exited 1.)
            RuntimeError: The device list itself could not be read.
        """
        if not self.device_serial:
            return

        attached = [
            device["serial"]
            for device in get_connected_devices()
            if device.get("state") == "device"
        ]
        if self.device_serial in attached:
            return

        available = ", ".join(attached) if attached else "none"
        raise DeviceNotFoundError(
            f"Device '{self.device_serial}' is not attached. Available: {available}.",
            remedy="Pass --serial with one of the available devices, or start the one you named.",
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

    def _finish_stream(self, stopped_by_us: bool) -> bool:
        """Reap the logcat process and decide whether the capture succeeded.

        Args:
            stopped_by_us: True if the duration elapsed or the user interrupted,
                in which case a terminated process is the expected outcome.

        Returns:
            True if the capture completed as intended.

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

        # We terminated it deliberately, so its exit status says nothing about
        # whether the capture worked.
        if stopped_by_us:
            return True

        if returncode:
            # The drain thread may still be reading what adb said on the way out.
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=STREAM_SHUTDOWN_TIMEOUT)
            detail = " ".join(self._stderr_lines).strip()
            # adb's own words, typed as run_adb would type them: a device that
            # is not there is exit 3 at the boundary here too.
            device_error = classify_device_error(detail, self.device_serial)
            if device_error is not None:
                raise device_error
            # Raised, not printed: under --json this exited 1 with no object.
            message = f"adb logcat exited with status {returncode}."
            if detail:
                message += f" {detail}"
            cli.fail(
                message,
                "Check the device is connected (`adb devices`) and, with more than one "
                "attached, pass --serial.",
            )
        return True

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
        from ``pidof`` (uid-checked) and which from the log, and how many lines
        a live read dropped or never decided. Null without ``--app``.
        """
        return {
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
            },
            "errors": self.errors[:LOG_JSON_CAP],
            "warnings": self.warnings[:LOG_JSON_CAP],
            "sample_logs": self.log_lines[-LOG_TAIL:],
        }

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
