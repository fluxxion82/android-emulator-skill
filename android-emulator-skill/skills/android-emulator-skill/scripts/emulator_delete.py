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
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

from common import cli
from common.batch import Batch
from common.env_config import env_int
from common.sdk_tools import (
    AVD_HOME_REMEDY,
    CMDLINE_TOOLS_REMEDY,
    CMDLINE_TOOLS_SUBDIRS,
    SdkToolError,
    resolve_avd_home,
    run_sdk_tool,
    searched_locations,
    without_null_lines,
)

# Default number of newest AVDs to keep when --old is given without a value.
DEFAULT_KEEP_COUNT = env_int("ANDROID_EMU_DELETE_KEEP", 3)

# avdmanager is an Android SDK tool, not adb, so it does not go through
# common.adb_exec. It still needs a ceiling: an unbounded call wedges the
# caller with no diagnosis. Both operations here are local to the SDK.
SDK_TOOL_TIMEOUT = 120

# What to do about a declined prompt -- the same for every mode that asks.
CANCELLED_REMEDY = "Answer `yes` at the prompt, or pass --yes to delete without asking."

# What to do about an AVD avdmanager would not delete.
DELETE_REMEDY = "Run --list to verify the AVD name; resolve the reported failure before retrying."


class AvdHomeError(SdkToolError):
    """A listed AVD has no ``.avd`` directory under the resolved AVD home.

    Subclasses :class:`common.sdk_tools.SdkToolError` so that every CLI mode
    reports it the one way the L8 fix established -- ``{"error": ...}`` under
    ``--json``, the message on stderr otherwise, exit 1 -- instead of needing a
    second ``except`` at ``main()`` that a later mode could forget.
    """


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
        Get the AVD home directory, resolved the way ``avdmanager`` resolves it.

        Returns:
            Path to AVD home: ``$ANDROID_AVD_HOME``, else
            ``$ANDROID_SDK_HOME/.android/avd``, else ``~/.android/avd``.
            Reading only the first of those is half of L5 -- see
            :func:`common.sdk_tools.resolve_avd_home`.
        """
        return resolve_avd_home()

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
        used.

        An AVD whose ``.avd`` directory cannot be stat'd is a **failure**, not
        a timestamp of ``0.0`` (L5). That fallback sorted every unresolvable
        AVD to the end of a newest-first list -- which is the end ``--old``
        deletes -- so on a host whose AVD home is somewhere this script did not
        look, ``--old 3`` proposed deleting the lot and said nothing about it.
        The ranking either has real mtimes for every name or it has no answer.

        Returns:
            List of AVD names, most-recently-modified first

        Raises:
            AvdHomeError: The AVD home holds no ``.avd`` directory for some
                listed AVD, so the AVDs cannot be ranked by age at all.
        """
        avd_home = self.get_avd_home()
        names = self.list_avds()

        def _mtime(name: str) -> float:
            avd_dir = avd_home / f"{name}.avd"
            try:
                return avd_dir.stat().st_mtime
            except OSError as error:
                raise AvdHomeError(
                    f"cannot rank AVDs by age: {name}.avd not found under "
                    f"{avd_home} ({error.strerror}); {AVD_HOME_REMEDY}. To "
                    f"delete without ranking, name the AVD: --name {name}."
                ) from error

        return sorted(names, key=_mtime, reverse=True)

    def _confirm(self, prompt: str) -> bool:
        """
        Ask the user to confirm a destructive action: question on stderr, answer from stdin.

        Not ``input(prompt)``, which writes the question to stdout. Under
        ``--json`` stdout is the one answer object, and the prompt landed in
        front of it -- ``Permanently delete ALL 1 AVDs? (type 'yes' to
        confirm): {...`` -- so a caller parsing the JSON got neither (N4).

        Args:
            prompt: Question to present (a ``(type 'yes' to confirm): `` suffix
                is appended automatically)

        Returns:
            True if the user typed ``yes``; False for anything else, including
            end of input and Ctrl-C.
        """
        print(f"{prompt} (type 'yes' to confirm): ", end="", file=sys.stderr, flush=True)
        stdin = sys.stdin
        try:
            answer = stdin.readline() if stdin is not None else ""
        except KeyboardInterrupt:
            answer = ""
        if stdin is None or not stdin.isatty() or not answer.endswith("\n"):
            # No terminal echoed a newline after the question, so end its line
            # here: whatever follows (`Error: ...`) must start a line of its own.
            print(file=sys.stderr)
        return answer.strip().lower() == "yes"

    def _require_confirmation(self, prompt: str, confirm: bool) -> None:
        """
        Return only if ``confirm`` is set or the user typed ``yes``; otherwise refuse.

        One answer to a declined prompt, whichever mode asked. ``--name``
        answered "Deletion cancelled by user" and exit 1, while ``--all`` and
        ``--old`` returned an empty batch, printed "No AVDs deleted" and exited
        0 -- reporting a refused request as a successful one with nothing to do
        (N4).

        Args:
            prompt: The question, which the refusal quotes.
            confirm: ``--yes`` was given, so there is nothing to ask.

        Raises:
            cli.Failure: The user did not type ``yes``. Nothing was deleted.
        """
        if confirm or self._confirm(prompt):
            return
        cli.fail(
            f"deletion cancelled: {prompt!r} was not answered `yes`, so nothing was deleted",
            CANCELLED_REMEDY,
        )

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
            # Minus the bare `null` line avdmanager prints after its reason (N13).
            error_msg = without_null_lines(e.stderr or "") or str(e)
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
            cli.Failure: The user declined the prompt.
        """
        avdmanager = self.require_avdmanager()

        # Check if AVD exists
        avds = self.list_avds()
        if name not in avds:
            return False, f"AVD not found: {name}"

        self._require_confirmation(f"Permanently delete AVD {name}?", confirm)
        return self._delete_one(name, avdmanager)

    def delete_all(self, confirm: bool = False) -> tuple[int, int, list]:
        """
        Delete every defined AVD.

        Args:
            confirm: When True, skip the interactive confirmation prompt

        Returns:
            (succeeded, failed, items) tuple where ``items`` is a list of
            ``{"name", "success", "message", "error", "remedy"}`` dicts

        Raises:
            SdkToolError: avdmanager is absent or failed -- the same error the
                single-name path gives, rather than an empty batch (L8).
            cli.Failure: The user declined the prompt.
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
            (succeeded, failed, items) tuple

        Raises:
            SdkToolError: avdmanager is absent or failed.
            cli.Failure: The user declined the prompt.
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
            (succeeded, failed, items) tuple, ``items`` one ``{"name", "success",
            "message", "error", "remedy"}`` per AVD (common.batch). ``(0, 0,
            [])`` means there was nothing to delete -- never that the prompt
            was declined.

        Raises:
            cli.Failure: The user declined the prompt.
        """
        if not names:
            return 0, 0, []

        self._require_confirmation(prompt, confirm)

        batch = Batch("AVD", "name").run(
            (
                ({"name": name}, functools.partial(self._delete_one, name, avdmanager))
                for name in names
            ),
            remedy=DELETE_REMEDY,
            one_device=False,
        )
        return batch.succeeded, batch.failed, batch.items


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = cli.parser(
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
            f"(default: {DEFAULT_KEEP_COUNT}, override via ANDROID_EMU_DELETE_KEEP). "
            "KEEP_COUNT must be at least 1; use --all to delete every AVD"
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

    args = parser.parse_args(argv)

    # A broken avdmanager raises SdkToolError on every path, and common.cli
    # reports it the same way: the batch modes used to answer "No AVDs
    # deleted" and exit 0 here (L8).
    _dispatch(parser, EmulatorDeleter(), args)


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
    # Progress is for a reader; under --json stdout is the one object, so it
    # goes to stderr there. (It was printed ahead of the JSON.)
    progress = sys.stderr if args.json else sys.stdout

    if args.all:
        if args.verbose:
            print("Deleting all AVDs", file=progress)
        succeeded, failed, items = deleter.delete_all(confirm=args.yes)
        sys.exit(_report_batch("delete_all", succeeded, failed, items, args))

    # Batch: delete old (keep newest N)
    if args.old is not None:
        if args.old < 1:
            # `--old 0` keeps nothing, so it silently means `--all` -- reached
            # by a typo, by an ANDROID_EMU_DELETE_KEEP of 0, or by reading
            # "--old 0" as "0 days old". Two flags for the same irreversible
            # act, one of them by accident, is not a spelling worth keeping
            # (part of L5's evidence). A usage error through the parser: under
            # --json the shared refusal object, at exit 2.
            parser.error(
                f"--old {args.old} keeps no AVDs, which deletes every one of "
                f"them. Use `--all --yes` if that is what you mean, or pass "
                f"--old N with N >= 1.",
            )
        if args.verbose:
            print(f"Deleting older AVDs, keeping newest {args.old}", file=progress)
        succeeded, failed, items = deleter.delete_old(keep_count=args.old, confirm=args.yes)
        sys.exit(_report_batch("delete_old", succeeded, failed, items, args, keep_count=args.old))

    # Delete operation
    if not args.name:
        # Through common.cli: this printed the help text to stdout, which under
        # --json was the answer.
        cli.fail(
            "no AVD named: nothing to delete",
            "Pass --name <avd>, --all, or --old N (with --yes to skip the prompt).",
        )

    if args.verbose:
        print(f"Deleting AVD: {args.name}", file=progress)

    success, message = deleter.delete(args.name, confirm=args.yes)
    if not success:
        # Through common.cli: a name avdmanager does not list, and a delete it
        # refused or never finished, answered {"success": false, "message"} --
        # the one failure here with no `error` and no remedy.
        cli.fail(message, DELETE_REMEDY)

    if args.json:
        print(json.dumps({"success": success, "message": message}, indent=2))
    else:
        print(message)

    sys.exit(0)


def _report_batch(
    action: str,
    succeeded: int,
    failed: int,
    items: list,
    args: argparse.Namespace,
    keep_count: int | None = None,
) -> int:
    """Report a batch delete in the requested output mode; the exit status (common.batch).

    Every AVD is in ``items`` under --json (it was there only with --verbose),
    and a batch in which one failed is the refusal object.
    """
    total = succeeded + failed
    extra: dict = {"action": action, "succeeded": succeeded, "failed": failed, "total": total}
    if keep_count is not None:
        extra["keep_count"] = keep_count
    if not args.json:
        if total == 0:
            # Only when nothing matched: a declined prompt is a refusal (N4).
            print("No AVDs to delete")
        else:
            print(f"Delete summary: {succeeded}/{total} succeeded, {failed} failed")
        if args.verbose:
            for item in items:
                print(f"  {item['message']}")
    return Batch("AVD", "name", items).finish(json_mode=args.json, extra=extra)


if __name__ == "__main__":
    cli.run(main)
