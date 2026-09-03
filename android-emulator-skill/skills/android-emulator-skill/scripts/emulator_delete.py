#!/usr/bin/env python3
"""
Delete Android Virtual Devices (AVDs).

Remove AVDs when they're no longer needed. Useful for CI/CD cleanup
and managing AVD storage.

Usage Examples:
    # Delete single AVD (prompts for confirmation)
    python scripts/emulator_delete.py --name MyTestDevice

    # Delete without the confirmation prompt
    python scripts/emulator_delete.py --name MyTestDevice --yes

    # List all AVDs first
    python scripts/emulator_delete.py --list

    # Delete every AVD
    python scripts/emulator_delete.py --all --yes

    # Keep the 3 most-recently-modified AVDs, delete the rest
    python scripts/emulator_delete.py --old 3 --yes
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from common.env_config import env_int
from common.sdk_tools import (
    CMDLINE_TOOLS_REMEDY,
    CMDLINE_TOOLS_SUBDIRS,
    SdkToolError,
    run_sdk_tool,
    searched_locations,
)

# Default number of newest AVDs to keep when --old is given without a value.
DEFAULT_KEEP_COUNT = env_int("ANDROID_EMU_DELETE_KEEP", 3)

# avdmanager is an Android SDK tool, not adb, so it does not go through
# common.adb_exec. It still needs a ceiling: an unbounded call wedges the
# caller with no diagnosis. Both operations here are local to the SDK.
SDK_TOOL_TIMEOUT = 120


class EmulatorDeleter:
    """Delete Android AVDs."""

    def __init__(self):
        """Initialize emulator deleter."""
        pass

    def get_avdmanager_path(self) -> str | None:
        """
        Find avdmanager command.

        Returns:
            Path to avdmanager or None if not found
        """
        import shutil

        avdmanager = shutil.which("avdmanager")
        if avdmanager:
            return avdmanager

        # Try ANDROID_HOME
        android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if android_home:
            possible_paths = [
                Path(android_home) / "cmdline-tools" / "latest" / "bin" / "avdmanager",
                Path(android_home) / "tools" / "bin" / "avdmanager",
            ]
            for path in possible_paths:
                if path.exists():
                    return str(path)

        return None

    def require_avdmanager(self) -> str:
        """
        Resolved avdmanager, or :class:`SdkToolError` naming where it was sought.

        One message for every caller. ``--name`` reported a missing avdmanager
        and exited 1 while ``--all`` and ``--old`` turned the same condition
        into ``(0, 0, [])`` -- printed as "No AVDs deleted", exit 0, which reads
        as "there was nothing to delete" (L8).

        Raises:
            SdkToolError: avdmanager is not installed or not reachable.
        """
        path = self.get_avdmanager_path()
        if path:
            return path
        raise SdkToolError(
            f"avdmanager not found. Looked in: "
            f"{searched_locations('avdmanager', CMDLINE_TOOLS_SUBDIRS)}. "
            f"{CMDLINE_TOOLS_REMEDY}"
        )

    def get_avd_home(self) -> Path:
        """
        Get the AVD home directory.

        Returns:
            Path to AVD home (``$ANDROID_AVD_HOME`` or ``~/.android/avd``)
        """
        avd_home = os.environ.get("ANDROID_AVD_HOME")
        if avd_home:
            return Path(avd_home)
        return Path.home() / ".android" / "avd"

    def list_avds(self) -> list:
        """
        List available AVDs.

        Returns:
            List of AVD names. Empty means avdmanager ran and reported none.

        Raises:
            SdkToolError: avdmanager is absent, or it ran and failed. An empty
                list on failure is how a batch delete came to report "No AVDs
                deleted" and exit 0 on a host with no command-line tools (L8).
        """
        avdmanager = self.require_avdmanager()
        stdout = run_sdk_tool(
            [avdmanager, "list", "avd", "-c"],
            timeout=SDK_TOOL_TIMEOUT,
            remedy=CMDLINE_TOOLS_REMEDY,
        )
        return [line.strip() for line in stdout.split("\n") if line.strip()]

    def list_avds_by_recency(self) -> list:
        """
        List AVD names ordered newest-first by config modification time.

        AVD configs live at ``<avd_home>/<name>.avd``. The directory mtime is a
        reliable, Android-native proxy for how recently an AVD was created or
        used. AVDs whose ``.avd`` directory is missing sort oldest.

        Returns:
            List of AVD names, most-recently-modified first
        """
        avd_home = self.get_avd_home()
        names = self.list_avds()

        def _mtime(name: str) -> float:
            avd_dir = avd_home / f"{name}.avd"
            try:
                return avd_dir.stat().st_mtime
            except OSError:
                return 0.0

        return sorted(names, key=_mtime, reverse=True)

    def _confirm(self, prompt: str) -> bool:
        """
        Ask the user to confirm a destructive action.

        Args:
            prompt: Question to present (a ``(type 'yes' to confirm): `` suffix
                is appended automatically)

        Returns:
            True if the user typed ``yes``, False otherwise
        """
        try:
            response = input(f"{prompt} (type 'yes' to confirm): ")
        except (KeyboardInterrupt, EOFError):
            return False
        return response.strip().lower() == "yes"

    def _delete_one(self, name: str, avdmanager: str) -> tuple[bool, str]:
        """
        Delete a single AVD via avdmanager (no existence/confirm checks).

        Args:
            name: AVD name to delete
            avdmanager: Resolved path to the avdmanager binary

        Returns:
            (success, message) tuple
        """
        cmd = [avdmanager, "delete", "avd", "--name", name]
        try:
            subprocess.run(
                cmd, capture_output=True, text=True, timeout=SDK_TOOL_TIMEOUT, check=True
            )
            return True, f"AVD deleted: {name}"
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to delete AVD: {error_msg}"
        except subprocess.TimeoutExpired:
            return (
                False,
                f"avdmanager did not finish deleting {name} within {SDK_TOOL_TIMEOUT}s. "
                f"Check for a stale avdmanager process and retry.",
            )

    def delete(self, name: str, confirm: bool = False) -> tuple[bool, str]:
        """
        Delete an AVD.

        Args:
            name: AVD name to delete
            confirm: When True, skip the interactive confirmation prompt

        Returns:
            (success, message) tuple

        Raises:
            SdkToolError: avdmanager is absent or failed.
        """
        avdmanager = self.require_avdmanager()

        # Check if AVD exists
        avds = self.list_avds()
        if name not in avds:
            return False, f"AVD not found: {name}"

        if not confirm and not self._confirm(f"Permanently delete AVD {name}?"):
            return False, "Deletion cancelled by user"

        return self._delete_one(name, avdmanager)

    def delete_all(self, confirm: bool = False) -> tuple[int, int, list]:
        """
        Delete every defined AVD.

        Args:
            confirm: When True, skip the interactive confirmation prompt

        Returns:
            (succeeded, failed, results) tuple where ``results`` is a list of
            ``{"name", "success", "message"}`` dicts

        Raises:
            SdkToolError: avdmanager is absent or failed -- the same error the
                single-name path gives, rather than an empty batch (L8).
        """
        avdmanager = self.require_avdmanager()

        names = self.list_avds()
        return self._delete_many(
            names, avdmanager, confirm, f"Permanently delete ALL {len(names)} AVDs?"
        )

    def delete_old(
        self, keep_count: int = DEFAULT_KEEP_COUNT, confirm: bool = False
    ) -> tuple[int, int, list]:
        """
        Delete older AVDs, keeping the newest ``keep_count`` by config mtime.

        Args:
            keep_count: Number of most-recently-modified AVDs to keep
            confirm: When True, skip the interactive confirmation prompt

        Returns:
            (succeeded, failed, results) tuple

        Raises:
            SdkToolError: avdmanager is absent or failed.
        """
        avdmanager = self.require_avdmanager()

        keep_count = max(keep_count, 0)
        ordered = self.list_avds_by_recency()
        to_delete = ordered[keep_count:]
        prompt = f"Delete {len(to_delete)} older AVDs, keeping the newest {keep_count}?"
        return self._delete_many(to_delete, avdmanager, confirm, prompt)

    def _delete_many(
        self, names: list, avdmanager: str, confirm: bool, prompt: str
    ) -> tuple[int, int, list]:
        """
        Delete a batch of AVDs with a single up-front confirmation.

        Args:
            names: AVD names to delete
            avdmanager: Resolved path to the avdmanager binary
            confirm: When True, skip the interactive confirmation prompt
            prompt: Confirmation question shown when ``confirm`` is False

        Returns:
            (succeeded, failed, results) tuple
        """
        if not names:
            return 0, 0, []

        if not confirm and not self._confirm(prompt):
            return 0, 0, []

        succeeded = 0
        failed = 0
        results = []
        for name in names:
            success, message = self._delete_one(name, avdmanager)
            results.append({"name": name, "success": success, "message": message})
            if success:
                succeeded += 1
            else:
                failed += 1

        return succeeded, failed, results


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Delete Android Virtual Devices (AVDs)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Delete AVD (prompts for confirmation)
  python scripts/emulator_delete.py --name MyTestDevice

  # Delete without the confirmation prompt
  python scripts/emulator_delete.py --name MyTestDevice --yes

  # List all AVDs
  python scripts/emulator_delete.py --list

  # Delete every AVD
  python scripts/emulator_delete.py --all --yes

  # Keep the newest N AVDs, delete the rest
  python scripts/emulator_delete.py --old 3 --yes
        """,
    )

    parser.add_argument("--name", help="AVD name to delete")
    parser.add_argument("--all", action="store_true", help="Delete all AVDs")
    parser.add_argument(
        "--old",
        type=int,
        nargs="?",
        const=DEFAULT_KEEP_COUNT,
        metavar="KEEP_COUNT",
        help=(
            "Delete older AVDs, keeping the newest KEEP_COUNT by config mtime "
            f"(default: {DEFAULT_KEEP_COUNT}, override via ANDROID_EMU_DELETE_KEEP)"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt",
    )
    parser.add_argument("--list", action="store_true", help="List all AVDs")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    deleter = EmulatorDeleter()
    try:
        _dispatch(parser, deleter, args)
    except SdkToolError as error:
        # Every path reports a broken avdmanager the same way: the batch modes
        # used to answer "No AVDs deleted" and exit 0 here (L8).
        if args.json:
            print(json.dumps({"error": str(error)}, indent=2))
        else:
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


def _dispatch(parser: argparse.ArgumentParser, deleter: "EmulatorDeleter", args) -> None:
    """Run the requested mode. Exits the process; never returns normally."""
    # List operation
    if args.list:
        avds = deleter.list_avds()
        if args.json:
            print(json.dumps({"avds": avds}, indent=2))
        elif avds:
            print("Available AVDs:")
            for avd in avds:
                print(f"  {avd}")
        else:
            print("No AVDs found")
        sys.exit(0)

    # Batch: delete all
    if args.all:
        if args.verbose:
            print("Deleting all AVDs")
        succeeded, failed, results = deleter.delete_all(confirm=args.yes)
        _report_batch("delete_all", succeeded, failed, results, args)
        sys.exit(0 if failed == 0 else 1)

    # Batch: delete old (keep newest N)
    if args.old is not None:
        if args.verbose:
            print(f"Deleting older AVDs, keeping newest {args.old}")
        succeeded, failed, results = deleter.delete_old(keep_count=args.old, confirm=args.yes)
        _report_batch("delete_old", succeeded, failed, results, args, keep_count=args.old)
        sys.exit(0 if failed == 0 else 1)

    # Delete operation
    if not args.name:
        print("Error: specify --name, --all, or --old", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    if args.verbose:
        print(f"Deleting AVD: {args.name}")

    success, message = deleter.delete(args.name, confirm=args.yes)

    if args.json:
        print(json.dumps({"success": success, "message": message}, indent=2))
    else:
        print(message)

    sys.exit(0 if success else 1)


def _report_batch(
    action: str,
    succeeded: int,
    failed: int,
    results: list,
    args: argparse.Namespace,
    keep_count: int | None = None,
) -> None:
    """Print the result of a batch delete in the requested output mode."""
    total = succeeded + failed
    if args.json:
        payload = {
            "action": action,
            "succeeded": succeeded,
            "failed": failed,
            "total": total,
        }
        if keep_count is not None:
            payload["keep_count"] = keep_count
        if args.verbose:
            payload["results"] = results
        print(json.dumps(payload, indent=2))
        return

    if total == 0:
        print("No AVDs deleted")
    else:
        print(f"Delete summary: {succeeded}/{total} succeeded, {failed} failed")

    if args.verbose:
        for r in results:
            print(f"  {r['message']}")


if __name__ == "__main__":
    main()
