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
import contextlib
import json
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from common.adb_exec import AdbCommandError, AdbError, run_adb
from common.device_utils import build_adb_command, get_connected_devices, quote_for_device_shell
from common.env_config import env_int

# Output caps (env-configurable via ANDROID_EMU_LOG_*). Defaults preserve the
# historical behavior of this script.
LOG_LINE_MAX = env_int("ANDROID_EMU_LOG_LINE_MAX", 120)
LOG_TAIL = env_int("ANDROID_EMU_LOG_TAIL", 50)
LOG_TEXT_SUMMARY_CAP = env_int("ANDROID_EMU_LOG_TEXT_SUMMARY", 5)
LOG_JSON_CAP = env_int("ANDROID_EMU_LOG_JSON_CAP", 20)
LOG_INFO_CAP = env_int("ANDROID_EMU_LOG_INFO_CAP", 20)
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
        """
        self.app_package = app_package
        self.device_serial = device_serial
        self.severity_filter = severity_filter or ["error", "warning", "info", "debug"]

        # Log storage
        self.log_lines = []
        self.errors = []
        self.warnings = []
        self.info_messages = []

        # Statistics
        self.error_count = 0
        self.warning_count = 0
        self.info_count = 0
        self.debug_count = 0
        self.verbose_count = 0
        self.total_lines = 0

        # Deduplication
        self.seen_messages = set()

        # Process control
        self.log_process = None
        self.interrupted = False
        self._stderr_lines: list[str] = []

    def parse_time_duration(self, duration_str: str) -> float:
        """
        Parse duration string to seconds.

        Args:
            duration_str: Duration like "30s", "5m", "1h"

        Returns:
            Duration in seconds
        """
        match = re.match(r"(\d+)([smh])", duration_str.lower())
        if not match:
            raise ValueError(
                f"Invalid duration format: {duration_str}. Use format like '30s', '5m', '1h'"
            )

        value, unit = match.groups()
        value = int(value)

        if unit == "s":
            return value
        if unit == "m":
            return value * 60
        if unit == "h":
            return value * 3600

        return 0

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
        pattern = r"(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]+):\s*(.*)"
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

    def deduplicate_message(self, message: str, tag: str) -> bool:
        """
        Check if message is duplicate.

        Args:
            message: Log message
            tag: Log tag

        Returns:
            True if this is a new message, False if duplicate
        """
        # Create signature from tag and message (without timestamps/PIDs)
        signature = f"{tag}:{message}"
        signature = re.sub(r"\d+", "", signature)  # Remove numbers
        signature = re.sub(r"\s+", " ", signature).strip()

        if signature in self.seen_messages:
            return False

        self.seen_messages.add(signature)
        return True

    def process_log_line(self, line: str):
        """
        Process a single log line.

        Args:
            line: Log line to process
        """
        if not line.strip():
            return

        # Parse logcat format
        parsed = self.parse_logcat_line(line)
        if not parsed:
            # Store unparsed line as-is
            self.log_lines.append(line)
            self.total_lines += 1
            return

        self.total_lines += 1
        self.log_lines.append(parsed["raw"])

        # Classify severity
        severity = self.classify_log_line(parsed)

        # Skip if not in filter
        if severity not in self.severity_filter:
            return

        # Deduplicate (for errors and warnings)
        if severity in ["error", "warning"]:
            if not self.deduplicate_message(parsed["message"], parsed["tag"]):
                return

        # Store by severity
        if severity == "error":
            self.error_count += 1
            self.errors.append(f"[{parsed['tag']}] {parsed['message']}")
        elif severity == "warning":
            self.warning_count += 1
            self.warnings.append(f"[{parsed['tag']}] {parsed['message']}")
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
        if not self.severity_filter:
            return None

        priority_letters = []
        for sev in self.severity_filter:
            if sev == "error":
                priority_letters.extend(["E", "F"])
            elif sev == "warning":
                priority_letters.append("W")
            elif sev == "info":
                priority_letters.append("I")
            elif sev == "debug":
                priority_letters.append("D")
            elif sev == "verbose":
                priority_letters.append("V")

        if not priority_letters:
            return None

        priority_order = ["V", "D", "I", "W", "E", "F"]
        return priority_order[min(priority_order.index(p) for p in priority_letters)]

    def build_logcat_command(
        self,
        pid: str | None = None,
        last_minutes: float | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """
        Build the ``adb logcat`` command for streaming or a historical window.

        This is a pure arg->command mapping (no device I/O), kept separate so the
        PID-resolution and process plumbing in ``stream_logs`` stays testable.

        Args:
            pid: App PID to filter by (omitted if None/empty)
            last_minutes: If set, dump a historical window with ``-d -t`` instead
                of streaming live. The window starts at ``now - last_minutes``.
            now: Reference time for the historical window (defaults to current
                time; injectable for deterministic tests).

        Returns:
            Complete adb command list ready for subprocess.
        """
        cmd = build_adb_command("logcat", self.device_serial)

        # Historical window: dump-and-exit (-d) since a computed timestamp (-t).
        # logcat's -t accepts a "MM-DD HH:MM:SS.mmm" timestamp and prints lines
        # at or after it, then exits (no live follow).
        if last_minutes is not None:
            reference = now or datetime.now()
            start_time = reference - timedelta(minutes=last_minutes)
            cmd.append("-d")
            cmd.append("-t")
            cmd.append(start_time.strftime("%m-%d %H:%M:%S.000"))

        # `threadtime` is mandatory, not cosmetic: parse_logcat_line expects
        # "MM-DD HH:MM:SS.mmm PID TID P Tag: msg". The `time` format omits the
        # PID/TID columns and writes "P/Tag( PID):" instead, which that regex
        # cannot match -- so every line landed in the unparsed branch, severity
        # counts stayed at zero and --follow printed nothing at all.
        cmd.append("-v")
        cmd.append("threadtime")

        # Add app package filter if a PID was resolved
        if pid:
            cmd.append(f"--pid={pid}")

        # Add severity filter (single minimum priority, shows that level and up)
        min_priority = self._severity_min_priority()
        if min_priority:
            cmd.append(f"*:{min_priority}")

        return cmd

    def _resolve_app_pid(self) -> str | None:
        """Look up the running PID for ``self.app_package`` (None if not running)."""
        if not self.app_package:
            return None
        try:
            result = run_adb(
                "shell",
                self.device_serial,
                "pidof",
                quote_for_device_shell(self.app_package),
                check=True,
            )
        except AdbCommandError:
            # `pidof` exits non-zero when the app is not running, which is an
            # answer rather than a failure: continue without a PID filter.
            return None
        return result.stdout.strip() or None

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
        """
        if not self._verify_device_available():
            return False

        # Clear logcat if requested (ignored for historical windows, which read
        # the existing buffer rather than streaming new lines).
        if clear_first and last_minutes is None:
            # Ignore clear errors: an unclearable buffer is not worth failing
            # the capture over. Device-level errors still propagate.
            with contextlib.suppress(AdbCommandError):
                run_adb("logcat", self.device_serial, "-c", check=True)

        # Resolve app PID (device call) then build the command (pure mapping).
        pid = self._resolve_app_pid()
        cmd = self.build_logcat_command(pid=pid, last_minutes=last_minutes)

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
            self.log_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
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

            for line in iter(self.log_process.stdout.readline, ""):
                if not line:
                    break

                self.process_log_line(line.rstrip())

                if follow:
                    parsed = self.parse_logcat_line(line.rstrip())
                    if parsed:
                        severity = self.classify_log_line(parsed)
                        if severity in self.severity_filter:
                            print(line.rstrip())

                if duration and (datetime.now() - start_time).total_seconds() >= duration:
                    stopped_by_us = True
                    break

                if self.interrupted:
                    stopped_by_us = True
                    break

            return self._finish_stream(stopped_by_us)

        except Exception as e:
            print(f"Error streaming logs: {e}", file=sys.stderr)
            return False

        finally:
            if deadline_timer:
                deadline_timer.cancel()
            if self.log_process and self.log_process.poll() is None:
                self.log_process.terminate()

    def _verify_device_available(self) -> bool:
        """Check an explicitly requested serial is actually attached.

        ``adb -s <serial> logcat`` does not fail when the serial is unknown --
        it blocks waiting for that device to appear. With ``--duration`` set,
        that looks exactly like a device which simply logged nothing: the
        capture ends on schedule and reports success with zero lines. Exit
        status cannot distinguish the two, so the check has to happen up front.

        Returns:
            True if no specific serial was requested, or it is attached.
        """
        if not self.device_serial:
            return True

        try:
            attached = [
                device["serial"]
                for device in get_connected_devices()
                if device.get("state") == "device"
            ]
        except (RuntimeError, subprocess.SubprocessError) as exc:
            print(f"Could not list devices: {exc}", file=sys.stderr)
            return False

        if self.device_serial in attached:
            return True

        available = ", ".join(attached) if attached else "none"
        print(
            f"Device '{self.device_serial}' is not attached. Available: {available}.",
            file=sys.stderr,
        )
        return False

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
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    self._stderr_lines.append(line.rstrip())

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()

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
            detail = " ".join(self._stderr_lines).strip()
            message = f"adb logcat exited with status {returncode}."
            if detail:
                message += f" {detail}"
            else:
                message += (
                    " Check the device is connected and, with more than one "
                    "attached, pass --serial."
                )
            print(message, file=sys.stderr)
            return False
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
            lines.append(f"Logs for: {self.app_package}")
        else:
            lines.append("Logs for: All processes")

        # Statistics
        lines.append(f"Total lines: {self.total_lines}")
        lines.append(
            f"Errors: {self.error_count}, Warnings: {self.warning_count}, Info: {self.info_count}"
        )

        # Top issues
        if self.errors:
            lines.append(f"\nTop Errors ({len(self.errors)}):")
            for error in self.errors[:LOG_TEXT_SUMMARY_CAP]:
                lines.append(f"  ❌ {error[:LOG_LINE_MAX]}")  # Truncate long lines

        if self.warnings:
            lines.append(f"\nTop Warnings ({len(self.warnings)}):")
            for warning in self.warnings[:LOG_TEXT_SUMMARY_CAP]:
                lines.append(f"  ⚠️  {warning[:LOG_LINE_MAX]}")

        # Verbose output
        if verbose and self.log_lines:
            lines.append("\n=== Recent Log Lines ===")
            for line in self.log_lines[-LOG_TAIL:]:
                lines.append(line)

        return "\n".join(lines)

    def get_json_output(self) -> dict:
        """Get log results as JSON."""
        return {
            "app_package": self.app_package,
            "device_serial": self.device_serial,
            "statistics": {
                "total_lines": self.total_lines,
                "errors": self.error_count,
                "warnings": self.warning_count,
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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Monitor and analyze Android device/emulator logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
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
        "--severity", help="Comma-separated severity levels (error,warning,info,debug,verbose)"
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

    args = parser.parse_args()

    # Parse severity filter
    severity_filter = None
    if args.severity:
        severity_filter = [s.strip().lower() for s in args.severity.split(",")]

    # Initialize monitor
    monitor = LogMonitor(
        app_package=args.app_package,
        device_serial=args.device_serial,
        severity_filter=severity_filter,
    )

    # Parse duration
    duration = None
    if args.duration:
        duration = monitor.parse_time_duration(args.duration)

    # Parse historical window (--last); convert seconds -> minutes for logcat -t
    last_minutes = None
    if args.last_window:
        last_minutes = monitor.parse_time_duration(args.last_window) / 60

    # Stream logs
    if last_minutes is not None:
        print("Reading historical logs...", file=sys.stderr)
    else:
        print("Monitoring logs...", file=sys.stderr)
    if args.app_package:
        print(f"App: {args.app_package}", file=sys.stderr)

    try:
        success = monitor.stream_logs(
            follow=args.follow,
            duration=duration,
            clear_first=args.clear,
            last_minutes=last_minutes,
        )
    except AdbError as error:
        # Device-level failures (ambiguous serial, offline, adb missing) carry a
        # remedy; a traceback does not.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

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
    main()
