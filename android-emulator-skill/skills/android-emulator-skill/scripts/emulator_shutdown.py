#!/usr/bin/env python3
"""
Gracefully shutdown Android emulators.

This script shuts down one or more emulators and optionally verifies shutdown completion.

Key features:
- Shutdown by serial number or AVD name
- Emulators only: a serial that is not an `emulator-NNNN` is refused before any
  adb command is issued, so this can never power off an attached handset
- Verification of shutdown completion (on by default; opt out with --no-verify)
- Batch shutdown operations (all emulators)

Tunables (env, ANDROID_EMU_ prefix):
    ANDROID_EMU_SHUTDOWN_TIMEOUT      Verification timeout, seconds (default 30)
    ANDROID_EMU_SHUTDOWN_POLL_INTERVAL  Poll interval while verifying, seconds (default 0.5)
    ANDROID_EMU_SHUTDOWN_KILL_TIMEOUT   `emu kill` budget, seconds (default 10)
    ANDROID_EMU_SHUTDOWN_NAME_TIMEOUT   `emu avd name` budget, seconds (default 5)
"""

import argparse
import json
import sys
import time

from common import adb_exec
from common.device_utils import get_connected_devices
from common.emu_console import (
    EmulatorProbeError,
    RunningVerdict,
    avd_name_for_serial,
    avd_running,
    run_emu,
    survey_emulators,
)
from common.env_config import env_float, env_int

# Tunable defaults (override via the ANDROID_EMU_ prefix).
DEFAULT_SHUTDOWN_TIMEOUT = env_int("ANDROID_EMU_SHUTDOWN_TIMEOUT", 30)
POLL_INTERVAL_SECONDS = env_float("ANDROID_EMU_SHUTDOWN_POLL_INTERVAL", 0.5, min_value=0.05)
# Console-call budgets, unchanged from the hand-rolled subprocess calls these
# replaced. Both are far below emu_console's own 120s default, which is sized
# for a snapshot save rather than for a kill or a name lookup.
CONSOLE_KILL_TIMEOUT = env_int("ANDROID_EMU_SHUTDOWN_KILL_TIMEOUT", 10, min_value=1)
CONSOLE_NAME_TIMEOUT = env_int("ANDROID_EMU_SHUTDOWN_NAME_TIMEOUT", 5, min_value=1)


class EmulatorShutdown:
    """Shutdown Android emulators with optional verification."""

    def __init__(self, serial: str | None = None):
        """Initialize with optional device serial."""
        self.serial = serial

    def shutdown(
        self, verify: bool = True, timeout_seconds: int = DEFAULT_SHUTDOWN_TIMEOUT
    ) -> tuple:
        """
        Shutdown emulator and optionally verify completion.

        Args:
            verify: Verify shutdown completion (default True)
            timeout_seconds: Maximum seconds to wait for shutdown

        Returns:
            (success, message) tuple
        """
        if not self.serial:
            return False, "Device serial not specified"

        start_time = time.time()

        # Check if device is connected
        devices = get_connected_devices()
        device = next((d for d in devices if d["serial"] == self.serial), None)
        if not device:
            return False, f"Device {self.serial} not found"

        # Refuse a handset BEFORE issuing anything. `--all` and `--name` already
        # filter on type; `--serial` did not, and `adb emu kill` fails on a
        # device with no emulator console -- which used to fall through to
        # `adb shell reboot -p` and power off whatever phone was plugged in.
        if device["type"] != "emulator":
            return False, (
                f"{self.serial} is a physical device, not an emulator. "
                f"This script only stops emulators and will not power off a "
                f"handset. Disconnect the device, or pass an emulator serial "
                f"(emulator-NNNN)."
            )

        # Execute shutdown command: `adb emu kill` is the emulator-native
        # graceful stop. There is deliberately no fallback -- the only device
        # that rejects the console kill is one that has no console, i.e. not an
        # emulator, and that case is refused above.
        #
        # Through run_emu, which owns the three traps of this protocol: a `KO`
        # arrives at exit status 0, replies are framed with a trailing `OK`, and
        # a physical device answers nothing at all. A kill adb itself reports as
        # failed raises AdbCommandError, which is the check this call used to
        # spell by hand.
        try:
            run_emu("kill", serial=self.serial, timeout=CONSOLE_KILL_TIMEOUT)
        except adb_exec.AdbError as error:
            return False, f"Shutdown failed: {self.serial}: {error}"
        except Exception as e:
            return False, f"Shutdown error: {e}"

        # Optionally verify shutdown
        if verify:
            verified, message = self._wait_for_shutdown(timeout_seconds)
            elapsed = time.time() - start_time
            if verified:
                return True, f"Emulator shutdown: {self.serial} [{elapsed:.1f}s]"
            return False, message

        elapsed = time.time() - start_time
        return True, (
            f"Emulator shutdown initiated: {self.serial} [{elapsed:.1f}s] "
            "(use --verify to wait for confirmation)"
        )

    def _wait_for_shutdown(self, timeout_seconds: int = DEFAULT_SHUTDOWN_TIMEOUT) -> tuple:
        """
        Wait for emulator to fully shutdown.

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
                    f"Timeout waiting for shutdown after {timeout_seconds}s ({checks} checks)"
                )

            # Check if device is still connected
            devices = get_connected_devices()
            device = next((d for d in devices if d["serial"] == self.serial), None)
            if not device:
                return True, (f"Emulator shutdown verified after {elapsed:.1f}s ({checks} checks)")

            # Wait before next check
            time.sleep(poll_interval)


def get_avd_name_for_serial(serial: str) -> str | None:
    """
    Get the AVD name for a running emulator serial.

    Delegates to the one probe in :mod:`common.emu_console`. This function held
    its own console call and caught bare ``Exception``; three sibling scripts
    held their own, and the four disagreed about what a failure means (R3).

    Args:
        serial: Emulator serial (e.g., "emulator-5554")

    Returns:
        AVD name, or None if it cannot be determined. Callers that must not act
        on "cannot be determined" should use :func:`resolve_target_by_avd_name`,
        which keeps the reason.
    """
    return avd_name_for_serial(serial, timeout=CONSOLE_NAME_TIMEOUT)


def resolve_serial_by_avd_name(avd_name: str) -> str | None:
    """
    Resolve a running emulator to its serial by AVD name.

    Args:
        avd_name: AVD name (e.g., "Pixel_5_API_33")

    Returns:
        The matching emulator serial, or None if no running emulator uses it --
        which does NOT distinguish "no emulator is that AVD" from "an emulator
        would not say". :func:`resolve_target_by_avd_name` does.

    Raises:
        adb_exec.AdbError: If adb cannot be run or the device query fails. Left
            to propagate to ``main``, which prints the error's own remedy.
    """
    return resolve_target_by_avd_name(avd_name)[0]


def resolve_target_by_avd_name(avd_name: str) -> tuple[str | None, str | None]:
    """
    Resolve ``avd_name`` to a serial, or say why it could not be resolved.

    A shutdown is destructive, so "I did not find it" and "I could not look"
    must not be the same answer (R3). They were: every emulator that would not
    answer its console was skipped, so `--name X` on a host with a wedged
    emulator reported "no running emulator found for AVD name X" -- a confident
    negative over a device nobody had managed to ask.

    Args:
        avd_name: AVD name (e.g., "Pixel_5_API_33")

    Returns:
        ``(serial, None)`` when an emulator identified itself as this AVD;
        ``(None, None)`` when every emulator answered and none of them is it;
        ``(None, reason)`` when at least one emulator could not be identified,
        so the negative cannot be trusted.

    """
    # The listing stays on this module's own seam, which is where its tests
    # stub it -- passed as a CALLABLE so a failure comes back as a value
    # instead of escaping as an uncaught RuntimeError (F2).
    answer = avd_running(avd_name, get_connected_devices, timeout=CONSOLE_NAME_TIMEOUT)
    if answer.verdict is RunningVerdict.RUNNING:
        return answer.serial, None
    if answer.verdict is RunningVerdict.UNKNOWN:
        return None, answer.describe_unknown()
    return None, None


def shutdown_all_emulators(verify: bool = False) -> tuple:
    """
    Shutdown all running emulators.

    Args:
        verify: Verify shutdown completion

    Returns:
        (success_count, fail_count) tuple

    Raises:
        adb_exec.AdbError: If adb cannot be run or the device query fails. Left
            to propagate to ``main``, which prints the error's own remedy.
    """
    # Through the survey, so a listing failure is a typed AdbError rather than
    # the BARE RuntimeError get_connected_devices re-wraps it as -- which is not
    # an AdbError, so it escaped main's handler and reached the user as a
    # traceback, from the module family whose job is turning adb failures into
    # remedies (F2).
    survey = survey_emulators(get_connected_devices)
    if survey.unavailable:
        raise EmulatorProbeError(survey.unavailable)
    emulators = [{"serial": probe.serial} for probe in survey.probes]

    success_count = 0
    fail_count = 0

    for emu in emulators:
        shutdown = EmulatorShutdown(emu["serial"])
        success, _ = shutdown.shutdown(verify=verify)
        if success:
            success_count += 1
        else:
            fail_count += 1

    return (success_count, fail_count)


def main():
    """Main entry point: run the CLI, reporting adb failures without a traceback."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        _run(parser, args)
    except adb_exec.AdbError as error:
        # run_adb raises errors whose message already names a remedy ("pass
        # --serial ...", "start an emulator ..."). That message is the point.
        #
        # Parsed BEFORE the try so this handler knows whether --json was asked
        # for. It did not, so `--all --json` against a broken adb exited 1 with
        # prose on stderr and an EMPTY stdout -- the same defect R1 fixed in
        # emulator_erase, still live here (F1).
        _fail(error, json_mode=args.json)


def _fail(error: object, *, json_mode: bool) -> None:
    """Report a failure the way the caller asked to be spoken to, and exit 1.

    ``{"error": ...}`` on stdout under ``--json``: a caller that asked for JSON
    and got a sentence on stderr has an empty stdout to parse. Same signature as
    `emulator_boot` and `emulator_selector`, which have had it since #15.
    """
    if json_mode:
        print(json.dumps({"error": str(error)}, indent=2))
    else:
        print(f"Error: {error}", file=sys.stderr)
    sys.exit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shutdown Android emulators",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Shutdown specific emulator (verifies by default)
  python emulator_shutdown.py --serial emulator-5554

  # Shutdown by AVD name
  python emulator_shutdown.py --name Pixel_5_API_33

  # Shutdown without waiting for confirmation
  python emulator_shutdown.py --serial emulator-5554 --no-verify

  # Shutdown all emulators
  python emulator_shutdown.py --all
        """,
    )

    parser.add_argument("--serial", help="Device serial number")
    parser.add_argument("--name", help="AVD name of a running emulator (resolved to its serial)")
    # Verification is on by default; --no-verify opts out. --verify is kept as an
    # explicit (no-op-by-default) override for backward compatibility.
    parser.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        default=True,
        help="Verify shutdown completion (default: on)",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="Do not wait for shutdown confirmation",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_SHUTDOWN_TIMEOUT,
        help=(
            f"Timeout in seconds for verification "
            f"(default: {DEFAULT_SHUTDOWN_TIMEOUT}, override via ANDROID_EMU_SHUTDOWN_TIMEOUT)"
        ),
    )
    parser.add_argument("--all", action="store_true", help="Shutdown all emulators")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    return parser


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Run the requested mode. Exits the process; never returns normally."""
    # Shutdown all mode
    if args.all:
        success_count, fail_count = shutdown_all_emulators(verify=args.verify)
        if args.json:
            print(
                json.dumps(
                    {
                        "action": "shutdown_all",
                        "success_count": success_count,
                        "fail_count": fail_count,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"Shutdown complete: {success_count} succeeded, {fail_count} failed "
                f"(total: {success_count + fail_count})"
            )
        sys.exit(0 if fail_count == 0 else 1)

    # Resolve target serial: explicit --serial wins; otherwise resolve --name.
    serial = args.serial
    if not serial and args.name:
        serial, unknown = resolve_target_by_avd_name(args.name)
        if not serial:
            # "I could not look" is reported as itself, never as a confident
            # "nothing to shut down" (R3).
            # Through the one failure helper, like every other failing mode:
            # this path answered `{"success": false, "message": ...}`, so a
            # caller checking for the documented `error` key found none (F5).
            _fail(
                (
                    f"cannot tell whether '{args.name}' is running: {unknown}"
                    if unknown
                    else f"No running emulator found for AVD name '{args.name}'"
                ),
                json_mode=args.json,
            )

    # Single device mode
    if not serial:
        if not args.json:
            parser.print_help(file=sys.stderr)
        _fail("--serial or --name is required (or use --all)", json_mode=args.json)

    shutdown = EmulatorShutdown(serial)
    success, message = shutdown.shutdown(verify=args.verify, timeout_seconds=args.timeout)

    if not success:
        # One failure shape for every mode (F5).
        _fail(message, json_mode=args.json)

    if args.json:
        payload = {
            "success": True,
            "message": message,
            "serial": serial,
            "action": "shutdown",
        }
        if args.name:
            payload["name"] = args.name
        print(json.dumps(payload, indent=2))
    else:
        print(message)

    sys.exit(0)


if __name__ == "__main__":
    main()
