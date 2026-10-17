#!/usr/bin/env python3
"""
Boot Android emulators and wait for readiness.

This script boots one or more emulators and optionally waits for them to reach
a ready state. It measures boot time and provides progress feedback.

Key features:
- Boot by AVD name
- Wait for device readiness with configurable timeout
- Measure boot performance
- Batch boot operations (boot all AVDs)
- Progress reporting for CI/CD pipelines
"""

import argparse
import json
import subprocess
import sys
import time

from common import adb_exec, cli
from common.device_utils import get_connected_devices
from common.emu_console import avd_name_for_serial, survey_emulators
from common.env_config import env_float, env_int
from common.sdk_tools import (
    EMULATOR_NOT_FOUND_MESSAGE,
    EMULATOR_NOT_FOUND_REMEDY,
    get_emulator_path,
    missing_emulator_error,
    run_sdk_tool,
)

# Tunable defaults (override via the ANDROID_EMU_ prefix).
DEFAULT_BOOT_TIMEOUT = env_int("ANDROID_EMU_BOOT_TIMEOUT", 300)
POLL_INTERVAL_SECONDS = env_float("ANDROID_EMU_POLL_INTERVAL", 0.5, min_value=0.05)

# Ceiling for a single probe inside the poll loop. Deliberately short: a probe
# that stalls should be retried on the next poll rather than eat the boot budget.
PROBE_TIMEOUT_SECONDS = 5

# `emulator` is an Android SDK tool, not adb, so it does not go through
# common.adb_exec -- but it still needs a ceiling.
EMULATOR_TOOL_TIMEOUT = 30


class EmulatorBooter:
    """Boot Android emulators with optional readiness waiting."""

    def __init__(self, avd_name: str | None = None):
        """Initialize booter with optional AVD name."""
        self.avd_name = avd_name

    def boot(
        self,
        wait_ready: bool = False,
        timeout_seconds: int = DEFAULT_BOOT_TIMEOUT,
        headless: bool = False,
    ) -> tuple:
        """
        Boot emulator and optionally wait for readiness.

        Args:
            wait_ready: Wait for device to be ready before returning
            timeout_seconds: Maximum seconds to wait for readiness
            headless: Boot in headless mode (no GUI)

        Returns:
            (success, message) tuple
        """
        if not self.avd_name:
            return False, "Error: AVD name not specified"

        start_time = time.time()

        # Check if already booted. The probe is tri-state (L4/R3): an emulator
        # that will not say which AVD it is used to be filtered out here --
        # non-`device` state, or a console query that failed -- and filtering
        # it out is indistinguishable from it not being there. Launching a
        # second instance of an AVD that is already up corrupts its userdata,
        # which is the defect the already-booted short-circuit exists to
        # prevent, so an unidentified emulator stops the boot instead.
        # The listing stays on this module's own seam (get_connected_devices);
        # only the console question is centralised. It is passed as a CALLABLE
        # so a failed listing comes back as a value rather than escaping as an
        # uncaught RuntimeError -- which is what it did, from the one module
        # family whose job is turning adb failures into remedies (F2).
        survey = survey_emulators(get_connected_devices, timeout=PROBE_TIMEOUT_SECONDS)
        if survey.unavailable:
            return False, (
                f"Refusing to boot {self.avd_name}: {survey.describe_gap()}. Until "
                f"the running emulators can be listed, a boot may start a second "
                f"instance of an AVD that is already up, which corrupts its userdata."
            )

        for probe in survey.probes:
            if probe.avd_name == self.avd_name:
                elapsed = time.time() - start_time
                return True, (
                    f"Emulator already booted: {self.avd_name} "
                    f"({probe.serial}) [checked in {elapsed:.1f}s]"
                )

        unidentified = survey.unidentified
        if unidentified:
            # The remedy order is the shared one, and it matters (F3): the
            # first version said "shut that emulator down" first, which for a
            # stale `offline` emulator needs the very console that is not
            # answering. Killing the process is the thing that actually works.
            #
            # Returned only, not also warned on stderr: every failure goes out
            # through common.cli, so a second copy on stderr was the same
            # sentence twice.
            return False, (
                f"Refusing to boot {self.avd_name}: {survey.describe_gap()}. "
                f"Terminate the stale emulator process itself before retrying "
                f"(`pkill -f 'qemu-system.*{self.avd_name}'`, or quit it from "
                f"Android Studio's Device Manager) -- `emulator_shutdown` cannot "
                f"reach a console that is not answering. It may already be this "
                f"AVD, and a second instance of one AVD corrupts its userdata."
            )

        # Resolve the emulator binary. Exec'ing the bare name is unsafe: with
        # the SDK root on PATH it hits the <sdk>/emulator directory and raises
        # PermissionError instead of FileNotFoundError.
        emulator = get_emulator_path()
        if not emulator:
            return False, EMULATOR_NOT_FOUND_MESSAGE

        # Build emulator command
        cmd = [emulator, "-avd", self.avd_name]
        if headless:
            cmd.append("-no-window")

        # Execute boot command in background
        try:
            # Start emulator in background. Exempt from run_adb/timeout: this is
            # not an adb call, and the emulator process is meant to outlive this
            # script -- bounding it would kill the emulator we just launched.
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            # Give it a moment to start
            time.sleep(2)

            # Check if process is still running
            if process.poll() is not None:
                return False, f"Emulator failed to start (exit code: {process.returncode})"

        except OSError as e:
            return False, f"{EMULATOR_NOT_FOUND_MESSAGE}\n  (exec of {emulator!r} failed: {e})"
        except Exception as e:
            return False, f"Boot error: {e}"

        # Optionally wait for readiness
        if wait_ready:
            ready, wait_message = self._wait_for_ready(timeout_seconds)
            elapsed = time.time() - start_time
            if ready:
                return True, (
                    f"Emulator booted and ready: {self.avd_name} " f"[{elapsed:.1f}s total]"
                )
            return False, wait_message

        elapsed = time.time() - start_time
        return True, (
            f"Emulator booting: {self.avd_name} [started in {elapsed:.1f}s] "
            "(use --wait-ready to wait for availability)"
        )

    def _wait_for_ready(self, timeout_seconds: int = DEFAULT_BOOT_TIMEOUT) -> tuple:
        """
        Wait for emulator to reach ready state.

        Args:
            timeout_seconds: Maximum seconds to wait

        Returns:
            (success, message) tuple
        """
        start_time = time.time()
        poll_interval = POLL_INTERVAL_SECONDS
        checks = 0

        while True:
            checks += 1
            elapsed = time.time() - start_time

            # Check timeout
            if elapsed > timeout_seconds:
                return False, (
                    f"Timeout waiting for emulator readiness after {timeout_seconds}s "
                    f"({checks} checks)"
                )

            # Check if emulator is connected
            devices = get_connected_devices()
            emulators = [d for d in devices if d["type"] == "emulator" and d["state"] == "device"]

            if emulators:
                # Found an emulator - check if it's our AVD
                for emu in emulators:
                    emu_avd = self._get_avd_name_for_serial(emu["serial"])
                    if emu_avd == self.avd_name:
                        # Check if boot completed
                        if self._is_boot_completed(emu["serial"]):
                            return True, (
                                f"Emulator ready: {self.avd_name} ({emu['serial']}) "
                                f"after {elapsed:.1f}s ({checks} checks)"
                            )

            # Wait before next check
            time.sleep(poll_interval)

    def _is_boot_completed(self, serial: str) -> bool:
        """
        Check if device boot is completed.

        Args:
            serial: Device serial number

        Returns:
            True if boot completed
        """
        try:
            result = adb_exec.run_adb(
                "shell", serial, "getprop", "sys.boot_completed", timeout=PROBE_TIMEOUT_SECONDS
            )
        except adb_exec.AdbError:
            # A device that is still booting is legitimately offline, or briefly
            # absent from `adb devices`. That is exactly the state this poll
            # exists to wait out, so a failed probe means "not ready yet" rather
            # than a failure to report.
            return False
        return result.stdout.strip() == "1"

    def _get_avd_name_for_serial(self, serial: str) -> str | None:
        """
        Get AVD name for emulator serial.

        Args:
            serial: Emulator serial (e.g., "emulator-5554")

        Returns:
            AVD name, or None if not found
        """
        # Delegates to the one probe in common/emu_console.py. This function
        # used to hold its own copy of the console call, its own timeout and
        # its own idea of what a failure means -- as did the same function in
        # emulator_erase, emulator_selector and emulator_shutdown, and the four
        # disagreed. The lossy None is kept here because callers of THIS
        # helper only want a name; boot() itself reads the probe, so it can
        # tell "not this AVD" from "would not say".
        return avd_name_for_serial(serial, timeout=PROBE_TIMEOUT_SECONDS)

    @staticmethod
    def boot_all(headless: bool = False) -> tuple[int, int, list[dict]]:
        """
        Boot every AVD defined on this host.

        Each AVD is launched in the background (no readiness wait), mirroring the
        single-AVD non-blocking boot path so batch boots stay fast.

        Args:
            headless: Boot each AVD in headless mode (no GUI)

        Returns:
            (succeeded, failed, results) tuple where ``results`` is a list of
            ``{"avd": name, "success": bool, "message": str}`` dicts.
        """
        avds = list_avds()
        succeeded = 0
        failed = 0
        results: list[dict] = []

        for avd in avds:
            name = avd["name"]
            booter = EmulatorBooter(name)
            success, message = booter.boot(wait_ready=False, headless=headless)
            if success:
                succeeded += 1
            else:
                failed += 1
            results.append({"avd": name, "success": success, "message": message})

        return succeeded, failed, results


def list_avds() -> list:
    """
    List available AVDs.

    The emulator binary is resolved explicitly (see :mod:`common.sdk_tools`)
    rather than exec'd by bare name, which raises ``PermissionError`` when PATH
    holds the SDK root instead of ``$ANDROID_HOME/emulator``.

    An empty list means the emulator ran and reported no AVDs. A missing or
    failing emulator raises instead (X3): the two used to be the same answer,
    so ``--list-avds`` printed "No AVDs found" and exited 0 on a host with no
    Android SDK at all, and nothing an agent could read said otherwise.

    Returns:
        List of AVD dicts with name and target info. Empty means no AVDs exist.

    Raises:
        SdkToolError: The emulator binary is absent, or it ran and failed.
    """
    emulator = get_emulator_path()
    if not emulator:
        raise missing_emulator_error()
    stdout = run_sdk_tool(
        [emulator, "-list-avds"],
        timeout=EMULATOR_TOOL_TIMEOUT,
        remedy=EMULATOR_NOT_FOUND_REMEDY,
    )

    avds = []
    for line in stdout.split("\n"):
        name = line.strip()
        if name:
            avds.append({"name": name})

    return avds


# What to do about a refused or failed boot. The error already says which: a
# name this host has no AVD for, an emulator that would not start, a survey
# that could not tell what is running, or readiness that did not arrive.
BOOT_REMEDY = (
    "Fix what the error names, then retry. `emulator_boot.py --list-avds` lists the "
    "AVDs this host has; a boot that only needs longer takes --timeout."
)


def main(argv: list[str] | None = None):
    """Main entry point.

    AVD discovery that could not run (SdkToolError) is a failure, never an
    empty listing -- an agent reading "No AVDs found" cannot tell that from a
    host where the SDK is not installed (X3) -- and an adb error carries its
    own remedy. Both raise, and common.cli reports them in the shape the caller
    asked for, which is what the two handlers here and the one in ``_run``
    each did a copy of.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    _dispatch(parser, args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Boot Android emulators",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Boot AVD
  python emulator_boot.py --avd Pixel_5_API_33

  # Boot and wait for readiness
  python emulator_boot.py --avd Pixel_5_API_33 --wait-ready

  # Boot in headless mode
  python emulator_boot.py --avd Pixel_5_API_33 --headless

  # Boot all defined AVDs
  python emulator_boot.py --all

  # List available AVDs
  python emulator_boot.py --list-avds
        """,
    )

    parser.add_argument("--avd", help="AVD name to boot")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Boot all defined AVDs (background boot, no readiness wait)",
    )
    parser.add_argument(
        "--wait-ready", action="store_true", help="Wait for emulator to be ready before returning"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_BOOT_TIMEOUT,
        help=(
            f"Timeout in seconds for readiness check "
            f"(default: {DEFAULT_BOOT_TIMEOUT}, override via ANDROID_EMU_BOOT_TIMEOUT)"
        ),
    )
    parser.add_argument("--headless", action="store_true", help="Boot in headless mode (no GUI)")
    parser.add_argument("--list-avds", action="store_true", help="List available AVDs")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    return parser


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Run the requested mode. Exits the process; never returns normally."""
    # List AVDs mode
    if args.list_avds:
        avds = list_avds()
        if args.json:
            print(json.dumps({"avds": avds}, indent=2))
        elif avds:
            print(f"Available AVDs ({len(avds)}):")
            for avd in avds:
                print(f"  - {avd['name']}")
        else:
            print("No AVDs found")
        sys.exit(0)

    # Boot-all mode
    if args.all:
        succeeded, failed, results = EmulatorBooter.boot_all(headless=args.headless)
        total = succeeded + failed
        if args.json:
            print(
                json.dumps(
                    {
                        "action": "boot_all",
                        "succeeded": succeeded,
                        "failed": failed,
                        "total": total,
                        "results": results,
                    },
                    indent=2,
                )
            )
        elif total == 0:
            print("No AVDs found")
        else:
            print(f"Boot summary: {succeeded}/{total} succeeded, {failed} failed")
        sys.exit(0 if failed == 0 else 1)

    # Boot mode. The help text goes to stderr, and only in text mode: printed
    # to stdout it was the whole answer a --json caller got.
    if not args.avd:
        if not args.json:
            parser.print_help(file=sys.stderr)
        cli.fail(
            "--avd is required (or --all to boot every AVD, --list-avds to see them)",
            "Pass --avd <name>; `emulator_boot.py --list-avds` lists the names.",
        )

    booter = EmulatorBooter(args.avd)
    success, message = booter.boot(
        wait_ready=args.wait_ready, timeout_seconds=args.timeout, headless=args.headless
    )

    if not success:
        # A refused boot is a failing mode like any other (F2/F5), reported by
        # common.cli in its one shape. A script-local `_fail` printed
        # `{"error": ...}` here, with no `success` and no remedy.
        cli.fail(message, BOOT_REMEDY)

    if args.json:
        print(
            json.dumps(
                {"success": True, "message": message, "avd": args.avd, "action": "boot"},
                indent=2,
            )
        )
    else:
        print(message)

    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
