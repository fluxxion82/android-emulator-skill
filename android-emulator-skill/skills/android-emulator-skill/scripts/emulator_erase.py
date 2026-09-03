#!/usr/bin/env python3
"""
Erase/Reset Android Virtual Devices (AVDs) to factory state.

Wipes all user data from AVD while preserving the AVD configuration.
Useful for getting a clean state between test runs.

Snapshots are deliberately kept: a saved snapshot is a whole guest machine, not
user data, and deleting one silently would throw away state somebody recorded
on purpose. It does mean a later `snapshot.py --load` can restore the state this
erase cleared, so every successful erase says so (`snapshot.py --delete <name>`
removes one).

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
import sys
import time
from pathlib import Path

from common import adb_exec
from common.emu_console import RunningVerdict, avd_running
from common.env_config import env_float, env_int
from common.sdk_tools import resolve_avd_home

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

# Snapshots are NOT part of an erase (L9). A saved snapshot holds a whole guest
# machine image, so deleting one here would throw away state the user recorded
# deliberately -- but leaving it unmentioned lets "factory state" be undone by
# the next `snapshot.py --load`, which is the half of the contract that was
# missing. Said once, next to the wipe, rather than silently either way.
SNAPSHOT_NOTE = "snapshots kept; use snapshot.py --delete <name> to remove them"


class EmulatorEraser:
    """Erase/reset Android AVDs to factory state."""

    def __init__(self):
        """Initialize emulator eraser."""
        pass

    def get_avd_home(self) -> Path:
        """
        Get AVD home directory, resolved the way ``avdmanager`` resolves it.

        The order matters and it is not obvious: ``ANDROID_AVD_HOME`` points at
        the directory of ``.avd`` directories, while ``ANDROID_SDK_HOME`` points
        at the *parent of* ``.android``. Ignoring the second one sent this
        script to ``~/.android/avd`` on hosts that deliberately relocate it --
        typically CI images -- where it found nothing (L5).

        Returns:
            Path to AVD home
        """
        return resolve_avd_home()

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

    def running_check(self, name: str) -> str | None:
        """
        Why an erase of ``name`` must not proceed, or None when it may.

        The answer is tri-state, and the third state is the whole finding (L4).
        ``common.emu_console.avd_running`` reports RUNNING, NOT_RUNNING or
        UNKNOWN; only NOT_RUNNING permits a wipe, and it is only returned when
        *every* attached emulator said which AVD it is.

        Four things used to make this answer False without knowing:

        - The whole scan sat inside one ``try``, so the *first* failing console
          query returned False with later emulators never examined.
        - Only the literal state ``device`` counted, so a second emulator still
          booting (``offline``) was read as absent.
        - A failed ``adb devices`` was read as "nothing is running".
        - The name test was ``name in reply.payload``, a substring, so it also
          reported "running" for AVDs the console never named -- ``--name
          Pixel`` could not be erased while an unrelated ``Pixel_9`` was up.
          Equality on the unframed payload is what "this emulator IS that AVD"
          means, and it lives in the shared probe now.

        Args:
            name: AVD name

        Returns:
            A refusal naming its remedy, or None when no emulator is this AVD
            and every emulator was identified.

        A failed listing is one of the refusals rather than an exception: it is
        the same class of answer, and an erase that stops has one shape.
        """
        answer = avd_running(name)

        if answer.unavailable:
            # Not even the list of emulators is known, so nothing at all has
            # been established about this AVD (F2). Distinct from an emulator
            # that WAS listed and would not answer, and said differently.
            return (
                f"Refusing to erase {name}: {answer.describe_unknown()}. Then "
                f"retry, or pass --force to erase without the running check."
            )
        if answer.verdict is RunningVerdict.RUNNING:
            return (
                f"Refusing to erase {name}: it is running on {answer.serial}. "
                f"Shut it down (emulator_shutdown.py --serial {answer.serial}) "
                f"and retry, or pass --force to erase without the check."
            )
        if answer.verdict is RunningVerdict.UNKNOWN:
            # The remedy order matters and was wrong: an emulator that is
            # `offline` cannot be console-killed, so `emulator_shutdown --all`
            # is not the first thing to try -- it is the thing that will not
            # work. Restarting the emulator or the adb connection is.
            return (
                f"Refusing to erase {name}: an emulator is attached that could "
                f"not be identified, so it may be this AVD. "
                f"{answer.describe_unknown()}. Then retry, or pass --force to "
                f"erase without the check."
            )
        return None

    def is_avd_running(self, name: str) -> bool:
        """
        Whether an erase of ``name`` must be refused because an emulator is up.

        The boolean face of :meth:`running_check`: True means "do not wipe",
        for the AVD being up *or* for an emulator that would not identify
        itself. Callers wanting to tell a reader which of those it was should
        use :meth:`running_check`, whose string says so.

        Args:
            name: AVD name

        Returns:
            True when the erase must not proceed.
        """
        return self.running_check(name) is not None

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

        # Check if running. The refusal says WHICH of the two it is -- the AVD
        # is up on a named serial, or an emulator would not identify itself --
        # because claiming the first when only the second is known is how this
        # script came to assert things it had not established (L4).
        if not force:
            refusal = self.running_check(name)
            if refusal:
                return False, refusal

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
    """Main entry point: run the CLI, reporting adb failures without a traceback."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        _run(parser, args)
    except adb_exec.AdbError as error:
        # run_adb raises errors whose message already names a remedy. Print it
        # rather than a traceback -- for an agent, stderr is the retry prompt.
        #
        # Parsed BEFORE the try so this handler knows whether --json was asked
        # for. It did not, and a `--json` run that hit a RunningCheckError got
        # exit 1 with prose on stderr and an EMPTY stdout -- an agent parsing
        # stdout saw nothing at all where the contract promises {"error": ...}.
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
    return parser


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
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
            payload = {
                "action": "erase_all",
                "succeeded": succeeded,
                "failed": failed,
                "total": total,
                "results": results,
            }
            if succeeded:
                payload["snapshots"] = SNAPSHOT_NOTE
            if failed:
                # The batch keeps its per-AVD report AND carries the documented
                # error key, so "every failing mode answers {"error": ...}" is
                # true of this one too without losing what it found (F5).
                payload["error"] = f"{failed} of {total} AVDs were not erased"
            print(json.dumps(payload, indent=2))
        elif total == 0:
            print("No AVDs found")
        else:
            print(f"Erase summary: {succeeded}/{total} succeeded, {failed} failed")
            if args.verbose:
                for result in results:
                    print(f"  {result['message']}")
            if succeeded:
                print(SNAPSHOT_NOTE)
        sys.exit(0 if failed == 0 else 1)

    # Erase operation
    if not args.name:
        if not args.json:
            parser.print_help(file=sys.stderr)
        _fail(
            "--name is required (or --all to erase every AVD, --list to see them)",
            json_mode=args.json,
        )

    if args.verbose:
        print(f"Erasing AVD: {args.name}")

    success, message = eraser.erase(
        args.name, force=args.force, verify=args.verify, timeout_seconds=args.timeout
    )

    if not success:
        # Every failing mode answers the same way (F5). This path emitted
        # `{"success": false, "message": ...}`, so a caller checking for the
        # documented `error` key found none and read a refusal as a result.
        _fail(message, json_mode=args.json)

    if args.json:
        print(
            json.dumps({"success": True, "message": message, "snapshots": SNAPSHOT_NOTE}, indent=2)
        )
    else:
        print(message)
        print(SNAPSHOT_NOTE)

    sys.exit(0)


if __name__ == "__main__":
    main()
