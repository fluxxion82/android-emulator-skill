#!/usr/bin/env python3
"""
Android App State Capture

Captures complete app state for debugging: screenshot + UI hierarchy + logs.
Creates a debugging snapshot that can be analyzed later.

Usage Examples:
    # Capture current state
    python scripts/app_state_capture.py --package com.myapp --output snapshots/

    # Capture with logs from last 30 seconds
    python scripts/app_state_capture.py --package com.myapp --logs 30s

    # Limit how many log lines are kept in the snapshot
    python scripts/app_state_capture.py --package com.myapp --log-lines 500
"""

import argparse
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from common import adb_exec, logcat
from common.device_utils import get_ui_hierarchy, parse_display_density
from common.env_config import env_int
from common.screenshot_utils import capture_screenshot

# Tunable defaults (override via ANDROID_EMU_* env vars).
DEFAULT_LOG_LINES = env_int("ANDROID_EMU_STATE_LOG_LINES", 200)

# A snapshot is several dumps back to back, and it is taken of a device that is
# by definition in a state worth debugging. `dumpsys package` and `dumpsys
# activity activities` walk live system state and can run past the 30s
# adb_exec default on a loaded emulator, and `logcat -d` over a wide window
# drains the whole ring buffer. Both get their own budget here rather than
# raising the module-wide default for every adb call in the skill.
DUMPSYS_TIMEOUT_SECONDS = 60
LOGCAT_TIMEOUT_SECONDS = 60


def count_ui_elements(node: dict | None) -> int:
    """Count every node in a UI hierarchy tree.

    The hierarchy is shaped as ``{"tag", "attributes", "children": [...]}``
    (see ``get_ui_hierarchy``). This counts the node itself plus all
    descendants. A falsy/None node counts as zero.

    Args:
        node: Root node of the hierarchy (or any subtree)

    Returns:
        Total number of nodes in the tree
    """
    if not node:
        return 0
    total = 1
    for child in node.get("children", []) or []:
        total += count_ui_elements(child)
    return total


def analyze_logcat(text: str) -> dict:
    """Parse logcat text for error/warning counts.

    Recognizes both the standard logcat priority column (e.g. ``E/Tag`` or
    `` E ``) and free-text mentions of "error"/"warning" so it works across
    brief/threadtime/raw formats.

    Args:
        text: Captured logcat output

    Returns:
        Dict with ``lines``, ``errors``, ``warnings``
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    error_count = 0
    warning_count = 0

    # Logcat priority column: "<date> <time> <pid> <tid> E Tag: msg" (threadtime)
    # or "E/Tag( pid): msg" (brief). Match a standalone E/W priority token.
    err_priority = re.compile(r"(?:^|\s)E[/\s]")
    warn_priority = re.compile(r"(?:^|\s)W[/\s]")

    for line in lines:
        lower = line.lower()
        if err_priority.search(line) or "error" in lower or "exception" in lower:
            error_count += 1
        elif warn_priority.search(line) or "warning" in lower or "warn" in lower:
            warning_count += 1

    return {"lines": len(lines), "errors": error_count, "warnings": warning_count}


class AppStateCapture:
    """Captures complete app state for debugging."""

    def __init__(self, package: str, serial: str | None = None):
        """
        Initialize app state capture.

        Args:
            package: App package name
            serial: Optional device serial
        """
        self.package = package
        self.serial = serial

    def capture(
        self,
        output_dir: str,
        include_logs: bool = True,
        log_duration: str = "30s",
        log_lines: int = DEFAULT_LOG_LINES,
        screenshot_size: str = "full",
    ) -> tuple:
        """
        Capture complete app state.

        Args:
            output_dir: Directory to save artifacts
            include_logs: Include app logs
            log_duration: Duration of logs to capture (e.g., "30s", "1m")
            log_lines: Max number of log lines to retain in the snapshot
            screenshot_size: Screenshot size (full, half, quarter)

        Returns:
            (success, message, output_path) tuple
        """
        # Create output directory with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        app_name = self.package.split(".")[-1]
        snapshot_dir = Path(output_dir) / f"{app_name}-{timestamp}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        artifacts = []

        try:
            # 1. Capture screenshot
            screenshot_path = snapshot_dir / "screenshot.png"
            screenshot_result = capture_screenshot(
                self.serial,
                output_path=str(screenshot_path),
                size=screenshot_size,
            )
            # capture_screenshot() raises on failure and otherwise returns a
            # descriptor dict -- there is no "success" key, so the previous
            # check was always falsy and the PNG was written to disk but never
            # listed in the manifest.
            if screenshot_result.get("file_path"):
                artifacts.append("screenshot.png")

            # 2. Capture UI hierarchy
            ui_path = snapshot_dir / "ui-hierarchy.json"
            hierarchy = get_ui_hierarchy(self.serial)
            with open(ui_path, "w") as f:
                json.dump(hierarchy, f, indent=2)
            artifacts.append("ui-hierarchy.json")
            element_count = count_ui_elements(hierarchy)

            # 3. Capture app info
            app_info_path = snapshot_dir / "app-info.json"
            app_info = self._get_app_info()
            with open(app_info_path, "w") as f:
                json.dump(app_info, f, indent=2)
            artifacts.append("app-info.json")

            # 4. Capture device info
            device_info = self._get_device_info()
            device_info_path = snapshot_dir / "device-info.json"
            with open(device_info_path, "w") as f:
                json.dump(device_info, f, indent=2)
            artifacts.append("device-info.json")

            # 5. Capture logs if requested
            log_stats = None
            if include_logs:
                log_path = snapshot_dir / "app-logs.txt"
                log_stats = self._capture_logs(log_path, log_duration, log_lines)
                artifacts.append("app-logs.txt")

            # 6. Create summary
            summary = {
                "timestamp": timestamp,
                "package": self.package,
                "device_serial": self.serial,
                "artifacts": artifacts,
                "app_info": app_info,
                "device_info": device_info,
                "ui_element_count": element_count,
            }
            if log_stats is not None:
                summary["logs"] = log_stats

            summary_path = snapshot_dir / "snapshot-summary.json"
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2)

            # 7. Human-readable markdown summary
            self._write_summary_md(snapshot_dir, summary)

            return (
                True,
                f"State captured: {snapshot_dir}/",
                str(snapshot_dir),
            )

        except adb_exec.AdbError:
            # adb never reached a device, so there is no state to capture and
            # nothing partial worth reporting. main() prints the remedy the
            # error already names.
            raise
        except Exception as e:
            return False, f"Failed to capture state: {e}", None

    def _get_app_info(self) -> dict:
        """
        Get app information.

        Returns:
            App info dict
        """
        info = {"package": self.package}

        # Get version. The pipe runs in the device's own shell; a package with
        # no versionName line just makes grep exit non-zero, which is an answer
        # rather than a failure, so this does not pass check=True.
        result = adb_exec.run_adb(
            "shell",
            self.serial,
            "dumpsys",
            "package",
            self.package,
            "|",
            "grep",
            "versionName",
            timeout=DUMPSYS_TIMEOUT_SECONDS,
        )
        for line in result.stdout.split("\n"):
            if "versionName" in line and "=" in line:
                info["version"] = line.split("=")[1].strip()
                break

        # Get PID. `pidof` exits non-zero when the app is not running.
        result = adb_exec.run_adb("shell", self.serial, "pidof", self.package)
        info["pid"] = result.stdout.strip() if result.ok else None

        # Get current activity
        result = adb_exec.run_adb(
            "shell",
            self.serial,
            "dumpsys",
            "activity",
            "activities",
            "|",
            "grep",
            "mResumedActivity",
            timeout=DUMPSYS_TIMEOUT_SECONDS,
        )
        if result.stdout:
            info["current_activity"] = result.stdout.strip()

        return info

    def _get_device_info(self) -> dict:
        """
        Get device information: model, SDK level, and display density.

        Returns:
            Device info dict with ``model``, ``sdk``, and ``density`` keys
            (values are None when a probe fails).
        """
        info: dict = {"model": None, "sdk": None, "density": None}

        # These are cheap property reads; the default adb_exec budget is ample.
        # A probe that answers non-zero simply leaves its field None, but a
        # device-level failure raises and reaches main().

        # ro.product.model
        value = adb_exec.run_adb("shell", self.serial, "getprop", "ro.product.model").stdout.strip()
        if value:
            info["model"] = value

        # ro.build.version.sdk
        value = adb_exec.run_adb(
            "shell", self.serial, "getprop", "ro.build.version.sdk"
        ).stdout.strip()
        if value:
            try:
                info["sdk"] = int(value)
            except ValueError:
                info["sdk"] = value

        # Display density (adb shell wm density).
        #
        # Through the shared parser, which prefers the Override line. Taking the
        # FIRST `density:` match reports the PHYSICAL density, and when an
        # override is active -- `wm density 560` on a 420dpi device, or any
        # emulator started with a different density -- that is silently the
        # wrong number, written into every snapshot's device-info.json with
        # nothing to say it is stale. Recorded: wm_density_override.txt is
        # `Physical density: 420` / `Override density: 560`; the first match is
        # 420 and the effective answer is 560.
        #
        # This is the third time a defect class fixed in one file survived in
        # another (see R4 in screenshots, R11 in navigator), so it goes through
        # common.device_utils rather than growing a third density parser.
        result = adb_exec.run_adb("shell", self.serial, "wm", "density")
        # The shared parser raises when it cannot find a density; the old regex
        # simply skipped. Keep the tolerance: a snapshot that captured the
        # screen, the hierarchy and the logs should not be discarded because one
        # optional field was unreadable. The key is omitted, not guessed.
        with contextlib.suppress(RuntimeError):
            info["density"] = parse_display_density(result.stdout)

        return info

    def _capture_logs(
        self, output_path: Path, duration: str, log_lines: int = DEFAULT_LOG_LINES
    ) -> dict:
        """
        Capture app logs and analyze them for errors/warnings.

        Args:
            output_path: Path to save logs
            duration: Duration string (e.g., "30s", "1m")
            log_lines: Max number of log lines to retain

        Returns:
            Dict with ``captured`` plus ``lines``/``errors``/``warnings`` on
            success, or a ``reason``/``error`` on failure.
        """
        # `logcat -t N` means the last N LINES; a bare duration passed here
        # silently became a line count, so "--logs 1m" returned 60 lines. The
        # time-window form takes a "MM-DD HH:MM:SS.mmm" timestamp, which is what
        # common.logcat builds -- the same grammar and the same argv order every
        # other logcat reader in the skill uses. Routing through it is also what
        # made `--logs 1h` work here: this parser accepted only `[sm]`.
        try:
            since = logcat.window_start_for(duration)
        except ValueError:
            with open(output_path, "w") as f:
                f.write(f"Invalid log duration: {duration!r}\n")
            return {"captured": False, "reason": f"Invalid log duration: {duration!r}"}

        # Add package filter if PID available. `pidof` exits non-zero when the
        # app is not running; the snapshot then keeps the unfiltered window.
        pid_result = adb_exec.run_adb("shell", self.serial, "pidof", self.package)
        pid = pid_result.stdout.strip() if pid_result.ok else ""

        try:
            result = adb_exec.run_adb(
                "logcat",
                self.serial,
                *logcat.logcat_args(since=since, fmt=None, pid=pid),
                timeout=LOGCAT_TIMEOUT_SECONDS,
                check=True,
            )
            lines = result.stdout.split("\n")

            # Cap retained lines (keep the most recent) for token efficiency.
            if log_lines > 0 and len(lines) > log_lines:
                lines = lines[-log_lines:]

            text = "\n".join(lines)
            with open(output_path, "w") as f:
                f.write(text)

            stats = analyze_logcat(text)
            stats["captured"] = True
            return stats
        except adb_exec.DeviceError:
            # Nothing reached the device. A snapshot that merely notes "logs
            # unavailable" would hide a problem main() can state with a remedy.
            raise
        except Exception as e:
            with open(output_path, "w") as f:
                f.write(f"Error capturing logs: {e}\n")
            return {"captured": False, "error": str(e)}

    def _write_summary_md(self, snapshot_dir: Path, summary: dict) -> None:
        """Write a human-readable ``summary.md`` alongside the JSON artifacts."""
        md_path = snapshot_dir / "summary.md"
        device = summary.get("device_info", {})
        app_info = summary.get("app_info", {})
        logs = summary.get("logs")

        with open(md_path, "w") as f:
            f.write("# App State Capture\n\n")
            f.write(f"- **Package:** {summary.get('package', 'Unknown')}\n")
            f.write(f"- **Timestamp:** {summary.get('timestamp', 'Unknown')}\n")
            f.write(f"- **Device serial:** {summary.get('device_serial') or 'default'}\n\n")

            f.write("## Device\n")
            f.write(f"- Model: {device.get('model') or 'Unknown'}\n")
            f.write(f"- SDK: {device.get('sdk') if device.get('sdk') is not None else 'Unknown'}\n")
            f.write(
                "- Density: "
                f"{device.get('density') if device.get('density') is not None else 'Unknown'}\n\n"
            )

            f.write("## App\n")
            f.write(f"- Version: {app_info.get('version', 'Unknown')}\n")
            f.write(f"- PID: {app_info.get('pid') or 'not running'}\n")
            if app_info.get("current_activity"):
                f.write(f"- Activity: {app_info['current_activity']}\n")
            f.write("\n")

            f.write("## UI Hierarchy\n")
            f.write(f"- Elements: {summary.get('ui_element_count', 0)}\n\n")

            f.write("## Logs\n")
            if logs is None:
                f.write("- Not captured\n\n")
            elif logs.get("captured"):
                f.write(f"- Lines: {logs.get('lines', 0)}\n")
                f.write(f"- Errors: {logs.get('errors', 0)}\n")
                f.write(f"- Warnings: {logs.get('warnings', 0)}\n\n")
            else:
                f.write(f"- {logs.get('reason', logs.get('error', 'Not captured'))}\n\n")

            f.write("## Files\n")
            for artifact in summary.get("artifacts", []):
                f.write(f"- `{artifact}`\n")
            f.write("- `snapshot-summary.json`\n")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Capture complete Android app state for debugging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Capture current state
  python scripts/app_state_capture.py --package com.myapp --output snapshots/

  # Capture with 1 minute of logs
  python scripts/app_state_capture.py --package com.myapp --logs 1m

  # Capture without logs
  python scripts/app_state_capture.py --package com.myapp --no-logs

  # Keep up to 500 log lines in the snapshot
  python scripts/app_state_capture.py --package com.myapp --log-lines 500
        """,
    )

    parser.add_argument("--package", required=True, help="App package name")
    parser.add_argument(
        "--output",
        default="app-state-snapshots",
        help="Output directory (default: app-state-snapshots)",
    )
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument(
        "--logs", default="30s", help="Duration of logs to capture (e.g., 30s, 5m, 1h)"
    )
    parser.add_argument("--no-logs", action="store_true", help="Don't capture logs")
    parser.add_argument(
        "--log-lines",
        type=int,
        default=DEFAULT_LOG_LINES,
        help=f"Max log lines to retain in the snapshot (default: {DEFAULT_LOG_LINES})",
    )
    parser.add_argument(
        "--screenshot-size",
        default="full",
        choices=["full", "half", "quarter"],
        help="Screenshot size (default: full)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    capturer = AppStateCapture(package=args.package, serial=args.device_serial)

    if args.verbose:
        print(f"Capturing state for: {args.package}")

    # CLI boundary: a device-level adb failure (ambiguous target, wrong serial,
    # offline, unauthorized) means no command ever ran. Print the remedy the
    # error already carries rather than letting a traceback reach the user.
    try:
        success, message, snapshot_path = capturer.capture(
            output_dir=args.output,
            include_logs=not args.no_logs,
            log_duration=args.logs,
            log_lines=args.log_lines,
            screenshot_size=args.screenshot_size,
        )
    except adb_exec.AdbError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(
            json.dumps(
                {"success": success, "message": message, "snapshot_path": snapshot_path},
                indent=2,
            )
        )
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
