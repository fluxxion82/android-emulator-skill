#!/usr/bin/env python3
"""
Emulator state snapshots -- list, save, load, delete -- through the console.

Why this needs a script at all
------------------------------
**`adb emu` exits 0 when the command fails.** Measured on emulator-5554
(API 35), and re-measured while writing this file:

    $ adb emu avd snapshot load no_such_snapshot_xyz >out 2>err ; echo $?
    0
    $ cat out
    KO: Device 'encrypt' does not have the requested snapshot 'no_such_snapshot_xyz'
    KO: Snapshot load failure: snapshot doesn't exist
    $ cat err
    (empty -- the rejection arrives on stdout, inside the reply body)

Recorded: ``tests/fixtures/recorded/emulator-api35/emu_avd_snapshot_load_missing.txt``

So a harness that restores a snapshot and checks the exit status is told the
restore worked when no such snapshot exists, and every assertion after it runs
against whatever state the emulator happened to be in -- green, and meaningless.
This is the same trap as ``am broadcast`` always printing ``result=0`` (see
``push_notification.py``).

Every console call in this file goes through :func:`common.emu_console.run_emu`,
which reads the reply rather than the exit status and raises ``EmuConsoleError``
on a ``KO``. Nothing here builds an ``adb emu`` command itself, and nothing
passes ``check=False``; both are pinned by tests.

What this tool does NOT do, and why
-----------------------------------
1. **It cannot tell you what a ``--load`` restored.** ``load`` answers ``OK``
   and nothing else; the reply does not describe the state that came back. The
   console does have ``avd snapshot get`` ("print the name of the snapshot
   loaded into the current session"), which is the obvious read-back -- but the
   only value measured for it is the literal ``(null)``, on a session that had
   loaded nothing. What it prints *after* a successful load is UNVERIFIED, and
   measuring it means loading a snapshot over a developer's running emulator.
   Rather than guess, ``--load`` reports that the console accepted the command
   and says plainly that nothing was verified. Record ``adb emu avd snapshot
   get`` right after a real load and this can become a genuine read-back.
2. **``--save`` proves an entry exists, not that it is any good.** Verification
   is a read-back of ``avd snapshot list``: after the save, a row with that TAG
   is present. That is strictly more than the ``OK`` and strictly less than a
   promise the guest was quiescent when its RAM was written.
3. **It cannot touch a physical device, or an emulator that is not running.**
   ``adb emu`` is the *running* emulator's console; a phone has none, and
   ``run_emu`` maps that failure to a message that says so.
4. **It does not convert VM SIZE.** The listing says ``138M``; that string is
   carried through verbatim. Turning it into bytes would invent precision the
   console never gave.

Measured on emulator-5554 / API 35
----------------------------------
- ``avd snapshot list`` prints a title line, a header line and one fixed-width
  row per snapshot, terminated by the console's ``OK`` (recorded:
  ``emu_avd_snapshot_list.txt``). The **ID column is literally ``--``** for the
  boot snapshot, so a parser keying on ID must tolerate it. The values do not
  line up under their headers -- ``VM SIZE`` is right-aligned and ends a column
  past the end of its header -- so rows are parsed by token from the right,
  anchored on the VM CLOCK and the date, never by header offset.
- ``avd snapshot save`` took ~2.9s for a 75M snapshot on an idle emulator, and
  ``load`` ~1.4s: far slower than an ordinary adb call. These are bounded by
  ``common.emu_console.CONSOLE_TIMEOUT`` (120s, ``ANDROID_EMU_CONSOLE_TIMEOUT``)
  rather than the 30s adb default, and ``--timeout`` raises it for a large AVD.
- ``adb emu avd snapshot`` with no sub-command prints the real sub-command list
  -- list, save, load, del, delete, remap, get, pull, push -- and then
  ``KO: missing sub-command``. ``delete`` is real and is what this script
  issues. Whether ``delete`` of a name that does not exist answers ``KO`` or
  ``OK`` was NOT measured (deleting things on a developer's emulator to find
  out was not a test this had licence to run), which is exactly why
  ``--delete`` verifies by read-back instead of trusting the reply.
- The console has no quoting: ``adb emu avd snapshot load a b`` and
  ``... load "a b"`` reach it as the same line, and it answers
  ``KO: ... requested snapshot 'a b'``. See ``SNAPSHOT_NAME_RULE`` for what
  that means for names.

Usage Examples:
    # What can be restored?
    python snapshot.py --list

    # Take a checkpoint before a destructive test, then put it back
    python snapshot.py --save before_upgrade
    python snapshot.py --load before_upgrade

    # An agent branches on the exit code, which is 1 for a KO
    python snapshot.py --load typo_in_the_name --json ; echo $?

    python snapshot.py --delete before_upgrade
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass

from common.adb_exec import AdbError
from common.device_utils import resolve_device_identifier
from common.emu_console import CONSOLE_TIMEOUT, EmuReply, run_emu
from common.env_config import env_int

# A save returns before nothing at all is left to do, but whether the new row is
# in `avd snapshot list` the instant the console says OK has NOT been measured
# (unlike `cmd notification post`, where the ~2s lag was). So the read-back
# polls rather than reading once: a false failure here would be worse than the
# extra call, and the deadline keeps a genuinely missing snapshot from hanging.
VERIFY_TIMEOUT_SECONDS = env_int("ANDROID_EMU_SNAPSHOT_VERIFY_TIMEOUT", 10, min_value=0)
VERIFY_POLL_SECONDS = 0.5

# Snapshot names reach the emulator console as bare text and become a directory
# under the AVD's snapshots path (`adb emu avd snapshotspath` ->
# /Users/<user>/.android/avd/<AVD>.avd/snapshots). Both facts drive this rule:
#
#   * The console has no quoting. `snapshot load a b` and `snapshot load "a b"`
#     arrive as the same line (measured: both answer "requested snapshot 'a b'"),
#     so a name containing whitespace cannot be told apart from two arguments,
#     and cannot be round-tripped through the listing either -- the TAG column is
#     whitespace-delimited. Reject whitespace.
#   * The name is used as a path component, so `/`, `\`, `..` and friends would
#     write outside the snapshots directory. Requiring the first character to be
#     alphanumeric makes `.`, `..` and a leading `-` (which some argument parser
#     between here and the console may yet read as a flag) impossible, and
#     restricting the rest to [A-Za-z0-9._-] leaves nothing a shell, a path or a
#     line-oriented protocol can reinterpret.
#
# This is deliberately a conservative subset, NOT a measurement of what the
# console rejects: no name was pushed at a real emulator to find its true limit,
# because that means writing snapshots on someone's machine. The 64-character cap
# is likewise a usability bound, not a measured one. The emulator's own
# `default_boot` passes, which is the case that has to work -- pinned by a test
# against the recorded listing.
SNAPSHOT_NAME_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_RULE_TEXT = (
    "must start with a letter or digit and contain only letters, digits, "
    "'.', '_' or '-' (max 64 characters)"
)

# Row anchors. The table is fixed-width but the values do not sit under their
# headers, so the row is read from the right: VM CLOCK last, then the date and
# time, then VM SIZE. Everything between the ID and VM SIZE is the TAG.
_VM_CLOCK = re.compile(r"^\d+:\d{2}:\d{2}(?:\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")

# Fields a data row must have: ID, TAG, VM SIZE, DATE, TIME, VM CLOCK.
_ROW_FIELDS = 6

# Lines the listing frames its table with, none of which are snapshots.
_TITLE_PREFIX = "List of snapshots"
_HEADER_TOKENS = ["ID", "TAG"]


@dataclass(frozen=True)
class Snapshot:
    """One row of ``adb emu avd snapshot list``.

    Attributes:
        name: The TAG column -- the name ``load``/``delete`` take.
        snapshot_id: The ID column, verbatim. Literally ``--`` for the boot
            snapshot, which is why it is a string and never an int.
        vm_size: The VM SIZE column verbatim, e.g. ``138M``. Not converted.
        created: DATE and time joined, e.g. ``2026-08-22 14:25:48``.
        vm_clock: The VM CLOCK column, e.g. ``04:33:18.874`` -- guest uptime at
            the moment the snapshot was taken, not a wall-clock time.
        raw: The row exactly as the console printed it.
    """

    name: str
    snapshot_id: str
    vm_size: str
    created: str
    vm_clock: str
    raw: str


def validate_snapshot_name(name: str) -> str | None:
    """Check a snapshot name against :data:`SNAPSHOT_NAME_RULE`.

    Args:
        name: Candidate snapshot name.

    Returns:
        None when the name is acceptable, else the reason it is not.
    """
    if not name:
        return f"snapshot name is empty; it {NAME_RULE_TEXT}"
    if not SNAPSHOT_NAME_RULE.match(name):
        return (
            f"snapshot name {name!r} is not usable: it {NAME_RULE_TEXT}. "
            f"The name reaches the emulator console unquoted and becomes a "
            f"directory under the AVD's snapshots path."
        )
    return None


def _is_framing(line: str) -> bool:
    """Whether a line is table framing rather than a candidate row."""
    stripped = line.strip()
    if not stripped or stripped == "OK" or stripped.startswith("KO"):
        return True
    if stripped.startswith(_TITLE_PREFIX):
        return True
    return stripped.split()[: len(_HEADER_TOKENS)] == _HEADER_TOKENS


def _parse_row(line: str) -> Snapshot | None:
    """Parse one table row, or None when the line is not one.

    Fields are taken from the right because the columns do not align with their
    headers (VM SIZE is right-aligned and overruns its header), so slicing by
    header offset mis-cuts. A line whose anchors do not match is skipped rather
    than guessed at -- the same discipline as ``push_notification``'s key parser.
    """
    tokens = line.split()
    if len(tokens) < _ROW_FIELDS:
        return None
    if not _VM_CLOCK.match(tokens[-1]):
        return None
    if not (_DATE.match(tokens[-3]) and _TIME.match(tokens[-2])):
        return None
    name = " ".join(tokens[1:-4])
    if not name:
        return None
    return Snapshot(
        name=name,
        snapshot_id=tokens[0],
        vm_size=tokens[-4],
        created=f"{tokens[-3]} {tokens[-2]}",
        vm_clock=tokens[-1],
        raw=line.rstrip(),
    )


def parse_snapshot_table(text: str) -> list[Snapshot]:
    """Parse ``adb emu avd snapshot list`` output into snapshot records.

    Tolerates the console framing (title, header, trailing ``OK``) so the raw
    reply and the unframed payload both parse to the same rows.

    Args:
        text: The listing, framed or unframed.

    Returns:
        One record per parsed row, in the order printed.
    """
    rows = (_parse_row(line) for line in text.splitlines() if not _is_framing(line))
    return [row for row in rows if row is not None]


def unrecognised_lines(text: str) -> list[str]:
    """Listing lines that are neither known framing nor a parsable row.

    Reported rather than dropped: if the table format ever shifts, "0 snapshots"
    would be a confident wrong answer, which is the failure mode this whole
    skill is being corrected for.

    Args:
        text: The listing, framed or unframed.

    Returns:
        The offending lines, stripped, in order.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if not _is_framing(line) and _parse_row(line) is None
    ]


class SnapshotManager:
    """List, save, load and delete emulator snapshots via the console."""

    def __init__(self, serial: str | None = None, timeout: int | None = None):
        """Initialize the manager.

        Args:
            serial: Emulator serial; the default device is used when None.
            timeout: Seconds to allow one console command. None uses
                ``common.emu_console.CONSOLE_TIMEOUT``.
        """
        self.serial = serial
        self.timeout = timeout

    # === PUBLIC API ===

    def list_snapshots(self) -> tuple[bool, dict]:
        """List the snapshots on this emulator's disks.

        Returns:
            (success, result_dict). ``snapshots`` holds the parsed rows;
            ``unrecognised`` and ``reply`` appear when a line could not be
            parsed, so an unexpected format shows up as text rather than as a
            confident empty list.
        """
        reply, error = self._console("avd", "snapshot", "list")
        if reply is None:
            return False, {"action": "list", "serial": self.serial, "error": error}

        snapshots = parse_snapshot_table(reply.payload)
        result = {
            "action": "list",
            "serial": self.serial,
            "count": len(snapshots),
            "snapshots": [asdict(item) for item in snapshots],
        }
        leftovers = unrecognised_lines(reply.payload)
        if leftovers or not snapshots:
            result["reply"] = reply.payload
        if leftovers:
            result["unrecognised"] = leftovers
        return True, result

    def save(self, name: str, verify: bool = True) -> tuple[bool, dict]:
        """Save the running emulator's state as ``name``.

        Args:
            name: Snapshot name; see :data:`SNAPSHOT_NAME_RULE`.
            verify: Read ``avd snapshot list`` back and require a row with that
                name before reporting success.

        Returns:
            (success, result_dict).
        """
        result: dict = {"action": "save", "serial": self.serial, "name": name, "verified": False}
        rejection = validate_snapshot_name(name)
        if rejection:
            result["error"] = rejection
            return False, result

        started = time.monotonic()
        reply, error = self._console("avd", "snapshot", "save", name)
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        if reply is None:
            result["error"] = error
            return False, result

        if not verify:
            result["note"] = (
                "Not verified (--no-verify): the console accepted the command, "
                "which is all that was checked."
            )
            return True, result

        present, listing = self._await_listing(name, want_present=True)
        result["snapshots"] = listing.get("snapshots", [])
        if not present:
            result["error"] = (
                f"the console accepted `snapshot save {name}` but no snapshot named "
                f"{name!r} appeared in `adb emu avd snapshot list` within "
                f"{VERIFY_TIMEOUT_SECONDS}s. Either the save did not happen, or the "
                f"listing lags the save -- which has not been measured. Re-run with "
                f"--no-verify and check `--list` by hand before trusting it."
            )
            return False, result

        result["verified"] = True
        return True, result

    def load(self, name: str) -> tuple[bool, dict]:
        """Restore the emulator to the state stored in ``name``.

        A ``KO`` reply is a failure even though ``adb emu`` exits 0; that is the
        entire reason this method exists rather than a raw console call.

        Args:
            name: Snapshot to restore.

        Returns:
            (success, result_dict). ``verified`` is always False: see this
            module's docstring for why a load cannot currently be read back.
        """
        result: dict = {"action": "load", "serial": self.serial, "name": name, "verified": False}
        rejection = validate_snapshot_name(name)
        if rejection:
            result["error"] = rejection
            return False, result

        started = time.monotonic()
        reply, error = self._console("avd", "snapshot", "load", name)
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        if reply is None:
            result["error"] = f"{error}{self._available_hint(name)}"
            return False, result

        result["note"] = (
            "The console accepted the load. Nothing read back proves what was "
            "restored -- `avd snapshot get` would, but its post-load value has "
            "not been measured, so it is not claimed here."
        )
        return True, result

    def delete(self, name: str, verify: bool = True) -> tuple[bool, dict]:
        """Delete the snapshot named ``name``.

        Args:
            name: Snapshot to delete.
            verify: Read ``avd snapshot list`` back and require the name to be
                gone. On by default because whether the console answers ``KO``
                for a name that does not exist has not been measured.

        Returns:
            (success, result_dict).
        """
        result: dict = {"action": "delete", "serial": self.serial, "name": name, "verified": False}
        rejection = validate_snapshot_name(name)
        if rejection:
            result["error"] = rejection
            return False, result

        reply, error = self._console("avd", "snapshot", "delete", name)
        if reply is None:
            result["error"] = f"{error}{self._available_hint(name)}"
            return False, result

        if not verify:
            result["note"] = (
                "Not verified (--no-verify): the console accepted the command, "
                "which is all that was checked."
            )
            return True, result

        gone, listing = self._await_listing(name, want_present=False)
        result["snapshots"] = listing.get("snapshots", [])
        if not gone:
            result["error"] = (
                f"the console accepted `snapshot delete {name}` but {name!r} is still "
                f"listed by `adb emu avd snapshot list` after {VERIFY_TIMEOUT_SECONDS}s."
            )
            return False, result

        result["verified"] = True
        return True, result

    # === INTERNAL ===

    def _console(self, *args: str) -> tuple[EmuReply | None, str]:
        """Run one console command through :func:`run_emu`.

        ``run_emu`` defaults to ``check=True`` and is never called otherwise
        here: a ``KO`` must raise, because the exit status will not show it.

        Args:
            *args: Console command and arguments.

        Returns:
            (reply, "") on success, or (None, error_message) on any adb or
            console failure.
        """
        try:
            reply = run_emu(*args, serial=self.serial, timeout=self.timeout)
        except AdbError as exc:
            return None, str(exc)
        return reply, ""

    def _await_listing(self, name: str, want_present: bool) -> tuple[bool, dict]:
        """Poll the listing until ``name``'s presence matches ``want_present``.

        Args:
            name: Snapshot name to look for.
            want_present: True to wait for it to appear, False to wait for it to go.

        Returns:
            (matched, last_listing_result).
        """
        deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
        while True:
            listed, listing = self.list_snapshots()
            if listed:
                names = {item["name"] for item in listing["snapshots"]}
                if (name in names) == want_present:
                    return True, listing
            if time.monotonic() >= deadline:
                return False, listing
            time.sleep(VERIFY_POLL_SECONDS)

    def _available_hint(self, name: str) -> str:
        """A remedy naming the snapshots that do exist, when they can be read.

        Best effort: this runs while building a failure message, so a listing
        that also fails degrades to no hint rather than replacing the real error.
        """
        listed, listing = self.list_snapshots()
        if not listed:
            return ""
        names = [item["name"] for item in listing["snapshots"]]
        if not names:
            return f"\nNo snapshots exist on this emulator, so {name!r} cannot be loaded."
        return f"\nSnapshots that do exist: {', '.join(names)}."


def _print_list(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--list`` report."""
    if not success:
        print(f"x {result.get('error', 'unknown error')}", file=sys.stderr)
        return

    target = result.get("serial") or "the default emulator"
    snapshots = result["snapshots"]
    if not snapshots:
        # Never announce "0 snapshots" without showing what the console said:
        # a shifted format would otherwise read as an empty emulator.
        print(f"No snapshots parsed on {target}. The console replied:")
        for line in (result.get("reply") or "(nothing)").splitlines() or ["(nothing)"]:
            print(f"  {line}")
        return

    print(f"{len(snapshots)} snapshot(s) on {target}:")
    for item in snapshots:
        line = f"  {item['name']}  {item['vm_size']}  {item['created']}"
        if verbose:
            line += f"  id={item['snapshot_id']}  vm clock {item['vm_clock']}"
        print(line)
    if result.get("unrecognised"):
        print(f"  ! {len(result['unrecognised'])} line(s) not understood (--json to see them)")


def _print_save(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--save`` report."""
    if not success:
        print(f"x Save failed: {result.get('error', 'unknown error')}", file=sys.stderr)
        return
    seconds = result.get("duration_seconds")
    print(f"+ Saved snapshot {result['name']!r} in {seconds}s")
    if result["verified"]:
        print("  Verified: it is in `adb emu avd snapshot list`.")
    else:
        print(f"  {result.get('note', 'Not verified.')}")
    if verbose:
        print(f"  Restore it with: --load {result['name']}")


def _print_load(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--load`` report."""
    if not success:
        print(f"x Load failed: {result.get('error', 'unknown error')}", file=sys.stderr)
        return
    seconds = result.get("duration_seconds")
    print(f"+ Loaded snapshot {result['name']!r} in {seconds}s")
    print(f"  {result.get('note', '')}")
    if verbose:
        print("  A failed load exits 0 at the adb level; this exit code reflects the reply.")


def _print_delete(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--delete`` report."""
    if not success:
        print(f"x Delete failed: {result.get('error', 'unknown error')}", file=sys.stderr)
        return
    print(f"+ Deleted snapshot {result['name']!r}")
    if result["verified"]:
        print("  Verified: it is gone from `adb emu avd snapshot list`.")
    else:
        print(f"  {result.get('note', 'Not verified.')}")
    if verbose:
        remaining = [item["name"] for item in result.get("snapshots", [])]
        print(f"  Remaining: {', '.join(remaining) or 'none'}")


_PRINTERS = {
    "list": _print_list,
    "save": _print_save,
    "load": _print_load,
    "delete": _print_delete,
}


def _emit(success: bool, result: dict, args: argparse.Namespace) -> None:
    """Render a result as JSON or as the concise human report."""
    if args.json:
        print(json.dumps({"success": success, **result}, indent=2))
        return
    printer = _PRINTERS.get(str(result.get("action")), _print_list)
    printer(success, result, args.verbose)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(
        description=(
            "List, save, load and delete Android emulator snapshots through the "
            "emulator console, reporting failure that `adb emu` hides."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The trap this exists for:
  `adb emu` EXITS 0 WHEN IT FAILS. A load of a snapshot that does not exist
  answers "KO: ... snapshot doesn't exist" on stdout and still exits 0
  (measured on API 35). Anything checking the exit status of a raw
  `adb emu avd snapshot load` is told the restore worked, and the tests after
  it run against unknown state. This script exits 1 on a KO.

What is and is not verified:
  --save    verified by read-back: a row with that name is in the listing.
  --delete  verified by read-back: the name is gone from the listing.
  --load    NOT verified. The console says OK and nothing describes what came
            back. `avd snapshot get` would be the read-back, but its value
            after a load has not been measured, so nothing is claimed.

Emulators only. `adb emu` is the running emulator's console; a physical device
does not have one.

Examples:
  python snapshot.py --list
  python snapshot.py --list --json
  python snapshot.py --save before_upgrade
  python snapshot.py --load before_upgrade --serial emulator-5554
  python snapshot.py --delete before_upgrade

Exit status:
  0  the console accepted the command (and, for --save/--delete, the read-back
     agreed)
  1  it did not
        """,
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--list",
        dest="list_snapshots",
        action="store_true",
        help="List the snapshots stored for this emulator",
    )
    action.add_argument("--save", metavar="NAME", help="Save the current state as NAME")
    action.add_argument("--load", metavar="NAME", help="Restore the state stored in NAME")
    action.add_argument("--delete", metavar="NAME", help="Delete the snapshot NAME")

    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="With --save/--delete: skip the listing read-back and trust the console reply",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            f"Seconds to allow one console command "
            f"(default: {CONSOLE_TIMEOUT}, override via ANDROID_EMU_CONSOLE_TIMEOUT)"
        ),
    )
    parser.add_argument("--serial", help="Emulator serial (auto-detects if omitted)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show extended detail")
    return parser


def main() -> None:
    """Parse arguments, run one snapshot action, and exit with its real status."""
    parser = _build_parser()
    args = parser.parse_args()

    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    manager = SnapshotManager(serial=serial, timeout=args.timeout)

    if args.list_snapshots:
        success, result = manager.list_snapshots()
    elif args.save:
        success, result = manager.save(args.save, verify=not args.no_verify)
    elif args.load:
        success, result = manager.load(args.load)
    else:
        success, result = manager.delete(args.delete, verify=not args.no_verify)

    _emit(success, result, args)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
