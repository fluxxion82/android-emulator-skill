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
import subprocess
import sys
import time

from common.device_utils import build_adb_command, get_connected_devices
from common.env_config import env_float, env_int

# Tunable defaults (override via the ANDROID_EMU_ prefix).
DEFAULT_BOOT_TIMEOUT = env_int("ANDROID_EMU_BOOT_TIMEOUT", 300)
POLL_INTERVAL_SECONDS = env_float("ANDROID_EMU_POLL_INTERVAL", 0.5, min_value=0.05)


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

        # Check if already booted
        devices = get_connected_devices()
        emulators = [d for d in devices if d["type"] == "emulator" and d["state"] == "device"]
        if emulators:
            # Check if this AVD is already running by checking emulator name
            for emu in emulators:
                emu_avd = self._get_avd_name_for_serial(emu["serial"])
                if emu_avd == self.avd_name:
                    elapsed = time.time() - start_time
                    return True, (
                        f"Emulator already booted: {self.avd_name} "
                        f"({emu['serial']}) [checked in {elapsed:.1f}s]"
                    )

        # Build emulator command
        cmd = ["emulator", "-avd", self.avd_name]
        if headless:
            cmd.append("-no-window")

        # Execute boot command in background
        try:
            # Start emulator in background
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

        except FileNotFoundError:
            return False, (
                "Error: 'emulator' command not found. "
                "Make sure Android SDK is installed and emulator is in PATH:\n"
                "  export ANDROID_HOME=$HOME/Library/Android/sdk\n"
                "  export PATH=$PATH:$ANDROID_HOME/emulator"
            )
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
            cmd = build_adb_command("shell", serial, "getprop", "sys.boot_completed")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
            return result.stdout.strip() == "1"
        except Exception:
            return False

    def _get_avd_name_for_serial(self, serial: str) -> str | None:
        """
        Get AVD name for emulator serial.

        Args:
            serial: Emulator serial (e.g., "emulator-5554")

        Returns:
            AVD name, or None if not found
        """
        try:
            cmd = build_adb_command("emu", serial, "avd", "name")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
            return result.stdout.strip()
        except Exception:
            return None

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

    Returns:
        List of AVD dicts with name and target info
    """
    try:
        cmd = ["emulator", "-list-avds"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        avds = []
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line:
                avds.append({"name": line})

        return avds

    except FileNotFoundError:
        print(
            "Error: 'emulator' command not found. "
            "Make sure Android SDK is installed and emulator is in PATH",
            file=sys.stderr,
        )
        return []
    except subprocess.CalledProcessError:
        return []


def main():
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

    args = parser.parse_args()

    # List AVDs mode
    if args.list_avds:
        avds = list_avds()
        if args.json:
            import json

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
            import json

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

    # Boot mode
    if not args.avd:
        parser.print_help()
        print("\nError: --avd is required", file=sys.stderr)
        sys.exit(1)

    booter = EmulatorBooter(args.avd)
    success, message = booter.boot(
        wait_ready=args.wait_ready, timeout_seconds=args.timeout, headless=args.headless
    )

    if args.json:
        import json

        print(
            json.dumps(
                {"success": success, "message": message, "avd": args.avd, "action": "boot"},
                indent=2,
            )
        )
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
