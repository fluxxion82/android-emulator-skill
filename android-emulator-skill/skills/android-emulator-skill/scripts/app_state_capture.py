#!/usr/bin/env python3
"""
Android App State Capture

Captures complete app state for debugging: screenshot + UI hierarchy + logs.
Creates a debugging snapshot that can be analyzed later.

Usage Examples:
    # Capture current state (into ~/.android-emulator-skill/cache/app-state-snapshots/)
    python scripts/app_state_capture.py --package com.myapp

    # Capture into a directory of your choosing
    python scripts/app_state_capture.py --package com.myapp --output snapshots/

    # Capture with logs from last 30 seconds
    python scripts/app_state_capture.py --package com.myapp --logs 30s

    # Limit how many log lines are kept in the snapshot
    python scripts/app_state_capture.py --package com.myapp --log-lines 500
"""

import argparse
import contextlib
import functools
import json
import sys
from datetime import datetime
from pathlib import Path

from common import adb_exec, cli, logcat
from common.cache_utils import default_cache_dir
from common.device_utils import (
    get_package_uids,
    get_pid_uids,
    get_system_server_pid,
    get_ui_hierarchy,
    parse_display_density,
    quote_for_device_shell,
)
from common.env_config import env_int

# One app's log window is read the same way here and in log_monitor, through
# common.logcat. These two are re-exported under the names this module has
# always offered (parse_package_pids is no longer called here).
from common.logcat import parse_package_pids, parse_threadtime  # noqa: F401
from common.screenshot_utils import capture_screenshot

# Tunable defaults (override via ANDROID_EMU_* env vars).
DEFAULT_LOG_LINES = env_int("ANDROID_EMU_STATE_LOG_LINES", 200)

# Where a snapshot goes when --output is not given, under the skill's cache
# root. The default used to be the relative `app-state-snapshots`, i.e. the
# directory the agent happened to be standing in -- a probe of this very
# repository left one in its root, untracked and not gitignored (N12).
SNAPSHOTS_DIRNAME = "app-state-snapshots"


def default_snapshot_dir() -> Path:
    """``~/.android-emulator-skill/cache/app-state-snapshots``, resolved per call."""
    return default_cache_dir() / SNAPSHOTS_DIRNAME


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
    """Count a threadtime log's lines, errors and warnings by PRIORITY.

    Read from the priority column only. This used to count any line whose text
    said "error" or "exception" as an error whatever its priority (D3), so a V
    line such as ``startWCT=... errorCallbackToken=null`` (logcat_threadtime)
    was an error, and a snapshot could report three where its own log held one.

    Args:
        text: ``logcat -v threadtime`` output -- the format ``_capture_logs``
            asks for, so the column this reads is the column it got.

    Returns:
        Dict with ``lines`` (non-blank lines), ``errors`` (E and F: a fatal is
        an error the agent wants to see) and ``warnings`` (W).
    """
    priorities = [
        parsed[1] for parsed in map(parse_threadtime, text.splitlines()) if parsed is not None
    ]
    return {
        "lines": sum(1 for line in text.splitlines() if line.strip()),
        "errors": sum(1 for p in priorities if p in ("E", "F")),
        "warnings": sum(1 for p in priorities if p == "W"),
    }


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

        A component the caller *asked for* and did not get makes the snapshot a
        failure (X8): an invalid ``--logs`` window, a log capture that failed
        on a device that was otherwise answering, a screenshot or a hierarchy
        dump that raised. All of those used to end up either as
        ``captured: false`` underneath the words "State captured" and an exit
        status of 0, or -- for anything that raised -- as a discarded snapshot
        whose artifacts were already on disk.

        Every component is therefore collected independently and every one of
        their failures is recorded rather than thrown away. The artifacts that
        *were* collected are still written and still returned: a partial
        snapshot is worth keeping, it is just not a success.

        Args:
            output_dir: Directory to save artifacts
            include_logs: Include app logs
            log_duration: Duration of logs to capture (e.g., "30s", "1m")
            log_lines: Max number of log lines to retain in the snapshot
            screenshot_size: Screenshot size (full, half, quarter)

        Returns:
            ``(success, message, output_path, partial)``. ``partial`` is the
            snapshot summary as far as it got, and lists what failed under
            ``failures``.
        """
        # Create output directory with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        app_name = self.package.split(".")[-1]
        snapshot_dir = Path(output_dir) / f"{app_name}-{timestamp}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Each component is collected on its own, and what it produced is kept
        # whether or not the next one works. Wrapping the whole sequence in one
        # `try` meant a device error during the logs discarded the screenshot
        # and the hierarchy already written to disk, and reported the snapshot
        # as though nothing had been captured at all.
        artifacts: list[str] = []
        failures: list[str] = []
        # What to do about a failure that knows its own remedy (a cli.Failure);
        # said ahead of the generic one.
        remedies: list[str] = []
        element_count = 0
        app_info: dict = {}
        device_info: dict = {}
        log_stats: dict | None = None

        # 1. Capture screenshot
        try:
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
        except Exception as exc:
            failures.append(f"the screenshot failed ({exc})")

        # 2. Capture UI hierarchy
        try:
            ui_path = snapshot_dir / "ui-hierarchy.json"
            hierarchy = get_ui_hierarchy(self.serial)
            with open(ui_path, "w") as f:
                json.dump(hierarchy, f, indent=2)
            artifacts.append("ui-hierarchy.json")
            element_count = count_ui_elements(hierarchy)
        except Exception as exc:
            failures.append(f"the UI hierarchy failed ({exc})")

        # 3. Capture app info
        try:
            app_info = self._get_app_info()
            app_info_path = snapshot_dir / "app-info.json"
            with open(app_info_path, "w") as f:
                json.dump(app_info, f, indent=2)
            artifacts.append("app-info.json")
        except Exception as exc:
            failures.append(f"reading the app info failed ({exc})")

        # 4. Capture device info
        try:
            device_info = self._get_device_info()
            device_info_path = snapshot_dir / "device-info.json"
            with open(device_info_path, "w") as f:
                json.dump(device_info, f, indent=2)
            artifacts.append("device-info.json")
        except Exception as exc:
            failures.append(f"reading the device info failed ({exc})")

        # 5. Capture logs if requested
        if include_logs:
            log_path = snapshot_dir / "app-logs.txt"
            try:
                log_stats = self._capture_logs(log_path, log_duration, log_lines)
                artifacts.append("app-logs.txt")
            except cli.Failure as exc:
                # A refusal (no process of the app to read): the rest of the
                # snapshot still stands, and the refusal's remedy is kept.
                log_stats = {"captured": False, "reason": str(exc)}
                remedies.append(exc.remedy)
            except Exception as exc:
                # `_capture_logs` re-raises a device error rather than writing
                # "logs unavailable" into the summary. That is right at the
                # method level and wrong here: the artifacts collected before
                # it are on disk and still worth handing over.
                log_stats = {"captured": False, "error": str(exc)}
            if not log_stats.get("captured"):
                reason = log_stats.get("reason") or log_stats.get("error") or "log capture failed"
                failures.append(f"logs were requested and {reason}")

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
        if failures:
            summary["failures"] = failures

        try:
            summary_path = snapshot_dir / "snapshot-summary.json"
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2)

            # 7. Human-readable markdown summary
            self._write_summary_md(snapshot_dir, summary)
        except OSError as exc:
            failures.append(f"writing the snapshot manifest failed ({exc})")

        # 8. A component that was requested and did not arrive is a failed
        #    snapshot, not a footnote inside a successful one.
        if failures:
            return (
                False,
                (
                    f"Snapshot incomplete: {'; '.join(failures)}. "
                    f"What was captured is in {snapshot_dir}/. "
                    + "".join(f"{remedy} " for remedy in remedies)
                    + "Check the device is reachable with `adb devices`; for logs "
                    "pass a valid window (e.g. --logs 30s) or --no-logs to skip them."
                ),
                str(snapshot_dir),
                summary,
            )

        return (
            True,
            f"State captured: {snapshot_dir}/",
            str(snapshot_dir),
            summary,
        )

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
            quote_for_device_shell(self.package),
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
        result = adb_exec.run_adb(
            "shell", self.serial, "pidof", quote_for_device_shell(self.package)
        )
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
        Capture the app's logs over a window and count its errors/warnings.

        The app's logs are the lines of every process it had in the window:
        the one ``pidof`` finds running, and each earlier one the window ties
        to the package (:func:`parse_package_pids`).

        Args:
            output_path: Path to save logs
            duration: Duration string (e.g., "30s", "1m")
            log_lines: Max number of log lines to retain

        Returns:
            Dict with ``captured`` plus ``lines``/``errors``/``warnings`` and
            the ``pids`` read on success, or a ``reason``/``error`` on failure.

        Raises:
            cli.Failure: The app had no process in the window -- none running
                and none named in it. Its logs cannot be told apart from the
                rest of the device's, and the whole device is not the answer.
            adb_exec.DeviceError: The device could not be reached.
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

        # Which processes are the app's. `pidof` names the one alive now; it
        # exits 1 and prints nothing when there is none (pidof_not_running).
        # That answer used to drop the filter altogether, so a snapshot of an
        # app that had just crashed held every process on the device; and when
        # it did answer, `--pid=<that one>` cut away the process that crashed
        # before the restart (D4). So the window is read whole and filtered
        # here, to the running process and every process the window itself
        # says was the app's.
        pid_result = adb_exec.run_adb(
            "shell", self.serial, "pidof", quote_for_device_shell(self.package)
        )
        candidates = (
            {token for token in pid_result.stdout.split() if token.isdigit()}
            if pid_result.ok
            else set()
        )
        # `pidof` matches a process NAME, which another app can give its own
        # process (`android:process`): keep only pids whose uid `ps` reports
        # as one of the package's (it has one per user it is installed for).
        # They are asked of the device once, and only when something needs them.
        package_uids = functools.cache(lambda: get_package_uids(self.serial, self.package))
        running: set[str] = set()
        if candidates and package_uids():
            owners = get_pid_uids(self.serial, candidates)
            running = {pid for pid in candidates if owners.get(pid) in package_uids()}

        try:
            # threadtime, explicitly: the PID and priority columns read below
            # are the ones it prints, whatever the device's default format is.
            # Microsecond headers tell a continuation line from a new message
            # (common.logcat.USEC_FORMAT).
            # Bytes, split on "\n" only: text mode would turn an app's raw "\r"
            # into a line break and let it forge a line (common.logcat.app_window).
            result = adb_exec.run_adb(
                "logcat",
                self.serial,
                *logcat.logcat_args(since=since, usec=True),
                timeout=LOGCAT_TIMEOUT_SECONDS,
                check=True,
                raw_text=True,
            )
        except adb_exec.DeviceError:
            # Nothing reached the device. A snapshot that merely notes "logs
            # unavailable" would hide a problem main() can state with a remedy.
            raise
        except Exception as e:
            with open(output_path, "w") as f:
                f.write(f"Error capturing logs: {e}\n")
            return {"captured": False, "error": str(e)}

        # The app's lines: those of each process the device vouches for (its
        # uid the package's), inside that process's life -- the same rule
        # log_monitor --app reads by (common.logcat.app_window). system_server's
        # pid is asked of the device only if the window holds a sentence.
        window = logcat.app_window(
            result.stdout,
            self.package,
            system_server_pid=lambda: get_system_server_pid(self.serial, not_one_of=candidates),
            package_uids=package_uids,
            running=running,
        )
        pids = window.pids
        if not pids:
            # Not "no logs": the device's whole window is not the app's, and an
            # empty one would read as an app that ran and logged nothing.
            cli.fail(
                f"{self.package} had no process in the last {duration}: `pidof` found "
                f"none running, and no line in the window names one",
                "Launch the app and capture again, widen the window (e.g. --logs 5m) "
                "if it ran earlier, or pass --no-logs.",
            )

        lines = window.lines
        # Cap retained lines (keep the most recent) for token efficiency.
        if log_lines > 0 and len(lines) > log_lines:
            lines = lines[-log_lines:]

        text = "".join(f"{line}\n" for line in lines)
        with open(output_path, "w") as f:
            f.write(text)

        stats = analyze_logcat(text)
        stats["captured"] = True
        stats["pids"] = sorted(pids, key=int)
        return stats

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
                if logs.get("pids"):
                    f.write(f"- Processes: {', '.join(logs['pids'])}\n")
                f.write(f"- Lines: {logs.get('lines', 0)}\n")
                f.write(f"- Errors: {logs.get('errors', 0)}\n")
                f.write(f"- Warnings: {logs.get('warnings', 0)}\n\n")
            else:
                f.write(f"- {logs.get('reason', logs.get('error', 'Not captured'))}\n\n")

            f.write("## Files\n")
            for artifact in summary.get("artifacts", []):
                f.write(f"- `{artifact}`\n")
            f.write("- `snapshot-summary.json`\n")


# What to do about a capture that did not complete. The error names each
# component that failed; what did arrive is in the snapshot directory.
CAPTURE_REMEDY = (
    "Fix what the error names (the device reachable, the app installed), then capture "
    "again; what did arrive is listed under `partial` (--json) and kept on disk."
)


def _report_failure(message: str, partial: dict) -> None:
    """Report a failed capture through common.cli (exit 1), handing over what arrived.

    It printed ``{"error", "partial"}`` itself -- no ``success``, no remedy --
    and exited; the exit-code sweep's payload check (N5) found it. ``partial``
    still rides in the refusal object, so the agent can use the artifacts that
    did arrive.

    Args:
        message: What failed, already carrying its remedy.
        partial: The snapshot summary as far as it got; ``{}`` when nothing was
            written.

    Raises:
        cli.Failure: always.
    """
    cli.fail(message, CAPTURE_REMEDY, details={"partial": partial})


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Capture complete Android app state for debugging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Capture current state; the snapshot's path is printed
  python scripts/app_state_capture.py --package com.myapp

  # Capture into a directory of your choosing
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
        help=(
            "Directory to write the snapshot into "
            f"(default: ~/.android-emulator-skill/cache/{SNAPSHOTS_DIRNAME})"
        ),
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

    args = parser.parse_args(argv)

    capturer = AppStateCapture(package=args.package, serial=args.device_serial)

    if args.verbose:
        # To stderr under --json, where stdout is the one object.
        print(
            f"Capturing state for: {args.package}",
            file=sys.stderr if args.json else sys.stdout,
        )

    # capture() maps every component failure to a partial result, so nothing
    # inside it is expected to raise. Anything that does reaches common.cli.
    success, message, snapshot_path, partial = capturer.capture(
        # Never the caller's cwd by default (N12); resolved here, at run time,
        # so it follows HOME as this process sees it.
        output_dir=args.output or str(default_snapshot_dir()),
        include_logs=not args.no_logs,
        log_duration=args.logs,
        log_lines=args.log_lines,
        screenshot_size=args.screenshot_size,
    )

    if not success:
        # X8: a missing component used to be reported under "State captured"
        # with exit 0. What WAS captured is still handed over, under `partial`,
        # so the agent can use the screenshot it did get -- but the run failed.
        _report_failure(message, partial)

    if args.json:
        print(
            json.dumps(
                {"success": success, "message": message, "snapshot_path": snapshot_path},
                indent=2,
            )
        )
    else:
        print(message)

    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
