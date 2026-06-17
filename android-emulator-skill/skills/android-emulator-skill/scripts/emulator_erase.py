#!/usr/bin/env python3
"""
Erase/Reset Android Virtual Devices (AVDs) to factory state.

Wipes all user data from AVD while preserving the AVD configuration.
Useful for getting a clean state between test runs.

Usage Examples:
    # Erase single AVD (must be shutdown first)
    python scripts/emulator_erase.py --name MyTestDevice

    # Erase and verify the wipe landed on disk
    python scripts/emulator_erase.py --name MyTestDevice --verify

    # Erase every AVD on this host
    python scripts/emulator_erase.py --all

    # List AVDs
    python scripts/emulator_erase.py --list
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from common.device_utils import build_adb_command
from common.env_config import env_float, env_int

# Tunable defaults (override via the ANDROID_EMU_ prefix).
DEFAULT_ERASE_TIMEOUT = env_int("ANDROID_EMU_ERASE_TIMEOUT", 90)
POLL_INTERVAL_SECONDS = env_float("ANDROID_EMU_POLL_INTERVAL", 0.5, min_value=0.05)

# User-data artefacts removed during an erase. Deleting these returns the AVD
# to factory state on next boot while preserving config.ini / hardware-qemu.ini.
USERDATA_FILES = [
    "userdata-qemu.img",
    "userdata-qemu.img.qcow2",
    "cache.img",
    "cache.img.qcow2",
    "sdcard.img",
    "sdcard.img.qcow2",
]


class EmulatorEraser:
    """Erase/reset Android AVDs to factory state."""

    def __init__(self):
        """Initialize emulator eraser."""
        pass

    def get_avd_home(self) -> Path:
        """
        Get AVD home directory.

        Returns:
            Path to AVD home
        """
        # Check ANDROID_AVD_HOME first
        avd_home = os.environ.get("ANDROID_AVD_HOME")
        if avd_home:
            return Path(avd_home)

        # Default to ~/.android/avd
        return Path.home() / ".android" / "avd"

    def list_avds(self) -> list:
        """
        List available AVDs.

        Returns:
            List of AVD names
        """
        avd_home = self.get_avd_home()
        if not avd_home.exists():
            return []

        avds = []
        for item in avd_home.iterdir():
            if item.is_dir() and item.name.endswith(".avd"):
                avd_name = item.name[:-4]  # Remove .avd extension
                avds.append(avd_name)

        return avds

    def is_avd_running(self, name: str) -> bool:
        """
        Check if AVD is currently running.

        Args:
            name: AVD name

        Returns:
            True if running
        """
        try:
            result = subprocess.run(
                build_adb_command("devices"),
                capture_output=True,
                text=True,
                check=True,
            )

            # Check if any emulator is running with this AVD
            for line in result.stdout.split("\n"):
                if "emulator" in line and "device" in line:
                    # Get emulator serial
                    serial = line.split()[0]
                    # Query AVD name
                    avd_result = subprocess.run(
                        build_adb_command("emu", serial, "avd", "name"),
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    if name in avd_result.stdout:
                        return True

            return False

        except subprocess.CalledProcessError:
            return False

    def erase(
        self,
        name: str,
        force: bool = False,
        verify: bool = False,
        timeout_seconds: int = DEFAULT_ERASE_TIMEOUT,
    ) -> tuple:
        """
        Erase an AVD (wipe user data).

        Args:
            name: AVD name to erase
            force: Skip running check
            verify: Poll the AVD on disk until the wipe is observed (or timeout)
            timeout_seconds: Maximum seconds to poll when ``verify`` is set

        Returns:
            (success, message) tuple
        """
        # Check if AVD exists
        avds = self.list_avds()
        if name not in avds:
            return False, f"AVD not found: {name}"

        # Check if running
        if not force and self.is_avd_running(name):
            return (
                False,
                f"AVD is currently running: {name}. Shut it down first or use --force.",
            )

        # Get AVD directory
        avd_home = self.get_avd_home()
        avd_dir = avd_home / f"{name}.avd"

        # Delete userdata files
        deleted_files = []
        for filename in USERDATA_FILES:
            file_path = avd_dir / filename
            if file_path.exists():
                try:
                    file_path.unlink()
                    deleted_files.append(filename)
                except OSError as e:
                    return False, f"Failed to delete {filename}: {e}"

        if deleted_files:
            base_message = f"AVD erased: {name} (deleted {len(deleted_files)} files)"
        else:
            base_message = f"AVD already clean: {name}"

        # Optionally verify the wipe actually landed on disk.
        if verify:
            ready, verify_message = self._verify_erase(avd_dir, timeout_seconds)
            if ready:
                return True, f"{base_message} [{verify_message}]"
            return False, verify_message

        return True, base_message

    def _verify_erase(self, avd_dir: Path, timeout_seconds: int = DEFAULT_ERASE_TIMEOUT) -> tuple:
        """
        Verify an erase has landed on disk.

        Polls the AVD directory until no user-data artefacts remain (the wipe is
        flushed) while the AVD config is still present, confirming the AVD is
        ready for a clean boot. This is the Android-native, device-free
        equivalent of waiting for readiness after a reset.

        Args:
            avd_dir: Path to the ``<name>.avd`` directory
            timeout_seconds: Maximum seconds to poll

        Returns:
            (success, message) tuple
        """
        start_time = time.time()
        poll_interval = POLL_INTERVAL_SECONDS
        checks = 0

        while time.time() - start_time < timeout_seconds:
            checks += 1
            remaining = [f for f in USERDATA_FILES if (avd_dir / f).exists()]
            config_present = (avd_dir / "config.ini").exists()
            if not remaining and config_present:
                elapsed = time.time() - start_time
                return True, f"verified clean in {elapsed:.1f}s ({checks} checks)"

            time.sleep(poll_interval)

        elapsed = time.time() - start_time
        still_present = [f for f in USERDATA_FILES if (avd_dir / f).exists()]
        return False, (
            f"Erase verification timeout after {elapsed:.1f}s ({checks} checks); "
            f"user data still present: {', '.join(still_present) or 'none'}"
        )

    def erase_all(
        self,
        force: bool = False,
        verify: bool = False,
        timeout_seconds: int = DEFAULT_ERASE_TIMEOUT,
    ) -> tuple[int, int, list[dict]]:
        """
        Erase every AVD defined on this host.

        Args:
            force: Skip the running check for each AVD
            verify: Poll each AVD on disk until the wipe is observed
            timeout_seconds: Maximum seconds to poll per AVD when ``verify`` is set

        Returns:
            (succeeded, failed, results) tuple where ``results`` is a list of
            ``{"avd": name, "success": bool, "message": str}`` dicts.
        """
        succeeded = 0
        failed = 0
        results: list[dict] = []

        for name in self.list_avds():
            success, message = self.erase(
                name,
                force=force,
                verify=verify,
                timeout_seconds=timeout_seconds,
            )
            if success:
                succeeded += 1
            else:
                failed += 1
            results.append({"avd": name, "success": success, "message": message})

        return succeeded, failed, results


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Erase/Reset Android Virtual Devices to factory state",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Erase AVD (must be shutdown first)
  python scripts/emulator_erase.py --name MyTestDevice

  # Erase and verify the wipe landed on disk
  python scripts/emulator_erase.py --name MyTestDevice --verify

  # Force erase (even if running - may cause issues)
  python scripts/emulator_erase.py --name MyTestDevice --force

  # Erase every AVD on this host
  python scripts/emulator_erase.py --all

  # List all AVDs
  python scripts/emulator_erase.py --list
        """,
    )

    parser.add_argument("--name", help="AVD name to erase")
    parser.add_argument("--list", action="store_true", help="List all AVDs")
    parser.add_argument("--all", action="store_true", help="Erase every AVD on this host")
    parser.add_argument(
        "--force", action="store_true", help="Force erase even if running (not recommended)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Poll the AVD on disk until the wipe is observed (or timeout)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_ERASE_TIMEOUT,
        help=(
            f"Timeout in seconds for --verify "
            f"(default: {DEFAULT_ERASE_TIMEOUT}, override via ANDROID_EMU_ERASE_TIMEOUT)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    eraser = EmulatorEraser()

    # List operation
    if args.list:
        avds = eraser.list_avds()
        if args.json:
            print(json.dumps({"avds": avds}, indent=2))
        elif avds:
            print("Available AVDs:")
            for avd in avds:
                print(f"  {avd}")
        else:
            print("No AVDs found")
        sys.exit(0)

    # Erase-all operation
    if args.all:
        if args.verbose:
            print("Erasing all AVDs")
        succeeded, failed, results = eraser.erase_all(
            force=args.force, verify=args.verify, timeout_seconds=args.timeout
        )
        total = succeeded + failed
        if args.json:
            print(
                json.dumps(
                    {
                        "action": "erase_all",
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
            print(f"Erase summary: {succeeded}/{total} succeeded, {failed} failed")
            if args.verbose:
                for result in results:
                    print(f"  {result['message']}")
        sys.exit(0 if failed == 0 else 1)

    # Erase operation
    if not args.name:
        print("Error: --name is required", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    if args.verbose:
        print(f"Erasing AVD: {args.name}")

    success, message = eraser.erase(
        args.name, force=args.force, verify=args.verify, timeout_seconds=args.timeout
    )

    if args.json:
        print(json.dumps({"success": success, "message": message}, indent=2))
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
