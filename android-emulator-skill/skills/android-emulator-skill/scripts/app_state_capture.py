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
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common.device_utils import build_adb_command, get_ui_hierarchy
from common.env_config import env_int
from common.screenshot_utils import capture_screenshot

# Tunable defaults (override via ANDROID_EMU_* env vars).
DEFAULT_LOG_LINES = env_int("ANDROID_EMU_STATE_LOG_LINES", 200)


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
            if screenshot_result.get("success"):
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

        except Exception as e:
            return False, f"Failed to capture state: {e}", None

    def _get_app_info(self) -> dict:
        """
        Get app information.

        Returns:
            App info dict
        """
        info = {"package": self.package}

        # Get version
        try:
            cmd = build_adb_command(
                "shell", self.serial, "dumpsys", "package", self.package, "|", "grep", "versionName"
            )
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            for line in result.stdout.split("\n"):
                if "versionName" in line:
                    info["version"] = line.split("=")[1].strip()
                    break
        except Exception:
            pass

        # Get PID
        try:
            cmd = build_adb_command("shell", self.serial, "pidof", self.package)
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info["pid"] = result.stdout.strip()
        except Exception:
            info["pid"] = None

        # Get current activity
        try:
            cmd = build_adb_command(
                "shell",
                self.serial,
                "dumpsys",
                "activity",
                "activities",
                "|",
                "grep",
                "mResumedActivity",
            )
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.stdout:
                info["current_activity"] = result.stdout.strip()
        except Exception:
            pass

        return info

    def _get_device_info(self) -> dict:
        """
        Get device information: model, SDK level, and display density.

        Returns:
            Device info dict with ``model``, ``sdk``, and ``density`` keys
            (values are None when a probe fails).
        """
        info: dict = {"model": None, "sdk": None, "density": None}

        # ro.product.model
        try:
            cmd = build_adb_command("shell", self.serial, "getprop", "ro.product.model")
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            value = result.stdout.strip()
            if value:
                info["model"] = value
        except Exception:
            pass

        # ro.build.version.sdk
        try:
            cmd = build_adb_command("shell", self.serial, "getprop", "ro.build.version.sdk")
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            value = result.stdout.strip()
            if value:
                try:
                    info["sdk"] = int(value)
                except ValueError:
                    info["sdk"] = value
        except Exception:
            pass

        # Display density (adb shell wm density)
        try:
            cmd = build_adb_command("shell", self.serial, "wm", "density")
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            # Format: "Physical density: 420" (and optionally "Override density: N")
            match = re.search(r"density:\s*(\d+)", result.stdout)
            if match:
                info["density"] = int(match.group(1))
        except Exception:
            pass

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
        # Parse duration
        match = re.match(r"(\d+)([sm])", duration)
        if not match:
            with open(output_path, "w") as f:
                f.write(f"Invalid log duration: {duration!r}\n")
            return {"captured": False, "reason": f"Invalid log duration: {duration!r}"}

        value, unit = match.groups()
        value = int(value)

        if unit == "m":
            value *= 60

        # Build logcat command
        cmd = build_adb_command("logcat", self.serial, "-d", "-t", f"{value}")

        # Add package filter if PID available
        pid_cmd = build_adb_command("shell", self.serial, "pidof", self.package)
        try:
            result = subprocess.run(pid_cmd, capture_output=True, text=True, check=True)
            pid = result.stdout.strip()
            if pid:
                cmd.append(f"--pid={pid}")
        except Exception:
            pass

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
    parser.add_argument("--logs", default="30s", help="Duration of logs to capture (e.g., 30s, 1m)")
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

    success, message, snapshot_path = capturer.capture(
        output_dir=args.output,
        include_logs=not args.no_logs,
        log_duration=args.logs,
        log_lines=args.log_lines,
        screenshot_size=args.screenshot_size,
    )

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
