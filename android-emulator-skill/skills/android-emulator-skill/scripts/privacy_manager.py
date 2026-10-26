#!/usr/bin/env python3
"""
Android Privacy & Permissions Manager

Grant/revoke app permissions for testing permission flows.
Supports common Android permissions with audit trail tracking.

Usage Examples:
    # Grant camera permission
    python scripts/privacy_manager.py --grant camera --package com.myapp

    # Revoke location permission
    python scripts/privacy_manager.py --revoke location --package com.myapp

    # Reset a permission (Android: alias for revoke; see note below)
    python scripts/privacy_manager.py --reset camera --package com.myapp

    # List app permissions
    python scripts/privacy_manager.py --list --package com.myapp

    # Grant multiple permissions
    python scripts/privacy_manager.py --grant camera,location,storage --package com.myapp

    # Record a test-trail scenario/step alongside the operation
    python scripts/privacy_manager.py --revoke camera --package com.myapp \
        --scenario onboarding --step 2

    # Act on (and read back) a secondary user's permissions
    python scripts/privacy_manager.py --grant camera --package com.myapp --user 11

Note on --user:
    Runtime permissions are per user. ``--user <id>`` goes into the ``pm``
    command AND selects that user's ``User <id>:`` section of ``dumpsys
    package`` for the read-back and ``--list``; without it both name user 0,
    the system user (``pm grant``'s own default). The id must be one ``pm list
    users`` names -- a grant for a user the device does not have exits 0 and
    prints nothing -- and ``all`` / ``current`` are refused: one read-back
    cannot verify several users.

Note on --reset:
    Unlike iOS (xcrun simctl privacy reset), Android has no command that returns
    a runtime permission to the original "ask on next use" prompt state. The
    closest device-native operation is ``pm revoke``, which sets the permission
    back to denied. ``--reset`` is therefore implemented as an explicit alias for
    revoke so test trails can express intent symmetrically with iOS; the recorded
    output marks the reset as revoke-based for transparency.
"""

import argparse
import functools
import json
import re
import sys
from datetime import datetime

from common import adb_exec, cli
from common.batch import Batch
from common.device_utils import (
    UserNotInstalledError,
    parse_package_permissions,
    parse_user_ids,
    quote_for_device_shell,
    user_state_error,
)
from common.env_config import env_int

# Timeout (seconds) for adb calls. Permission ops are fast; dumpsys package can
# take longer on a busy device, so allow an override.
ADB_TIMEOUT_SECONDS = env_int("ANDROID_EMU_PRIVACY_ADB_TIMEOUT", 30, min_value=1)

# Every adb call goes through common.adb_exec.run_adb (it used to be four bare
# subprocess.run calls). What that changes on failure, deliberately:
#
# - `pm` refusing (a SecurityException for an install-time permission, an
#   unknown package) is AdbCommandError. It is caught where CalledProcessError
#   was, reported as that permission's failure, and a batch goes on to the next.
# - A device that is gone (not found, offline, unauthorized), or several devices
#   and no --serial: these raise, and the batch (common.batch) stops at the
#   first one -- continuing against a device that is not there only repeats the
#   error -- and reports the permissions so far: exit 3 for an unavailable
#   device, else 1. A missing adb or a timeout is that permission's failure.
# - `pm grant` / `pm revoke` print an unknown permission's name back on stderr
#   (recorded: pm_grant_permission_echo, pm_revoke_permission_echo), so a name
#   holding a newline and an `adb: device ... not found` line reads as adb's
#   own. Those two calls ask run_adb to confirm a device error with one
#   `get-state` first (confirm_device_errors).

# What to do about one permission that was not changed; its message says why.
PERMISSION_REMEDY = (
    "Fix what the error names -- an unknown permission (`--list-permissions` lists the "
    "short names), a package that is not installed, or one that does not request the "
    "permission -- then retry it."
)

# "java.lang.SecurityException: Package X has not requested permission Y" --
# the line of `pm`'s stack trace that says why (recorded: pm_grant_not_changeable
# on API 35, pm_grant_user_not_requested on API 33).
_PLATFORM_EXCEPTION_RE = re.compile(r"^\s*(?:[\w$]+\.)*(\w*Exception): (.+)$", re.MULTILINE)

# An Android user id is a Java int: at most 2147483647, ten digits.
_MAX_USER_ID = 2**31 - 1
_MAX_USER_DIGITS = len(str(_MAX_USER_ID))


def platform_refusal(output: str) -> str | None:
    """``"SecurityException: <why>"`` from ``pm``'s output, without the stack trace."""
    match = _PLATFORM_EXCEPTION_RE.search(output)
    return f"{match.group(1)}: {match.group(2).strip()}" if match else None


class PrivacyManager:
    """Manages Android app permissions."""

    # Supported permissions (common ones)
    SUPPORTED_PERMISSIONS = {
        "camera": "android.permission.CAMERA",
        "location": "android.permission.ACCESS_FINE_LOCATION",
        "location_coarse": "android.permission.ACCESS_COARSE_LOCATION",
        "storage": "android.permission.READ_EXTERNAL_STORAGE",
        "write_storage": "android.permission.WRITE_EXTERNAL_STORAGE",
        "contacts": "android.permission.READ_CONTACTS",
        "write_contacts": "android.permission.WRITE_CONTACTS",
        "phone": "android.permission.READ_PHONE_STATE",
        "call_phone": "android.permission.CALL_PHONE",
        "sms": "android.permission.READ_SMS",
        "send_sms": "android.permission.SEND_SMS",
        "calendar": "android.permission.READ_CALENDAR",
        "write_calendar": "android.permission.WRITE_CALENDAR",
        "microphone": "android.permission.RECORD_AUDIO",
        "body_sensors": "android.permission.BODY_SENSORS",
        "activity_recognition": "android.permission.ACTIVITY_RECOGNITION",
        "background_location": "android.permission.ACCESS_BACKGROUND_LOCATION",
        "media_images": "android.permission.READ_MEDIA_IMAGES",
        "media_video": "android.permission.READ_MEDIA_VIDEO",
        "media_audio": "android.permission.READ_MEDIA_AUDIO",
        "notification": "android.permission.POST_NOTIFICATIONS",
    }

    def __init__(self, serial: str | None = None, user: int = 0):
        """
        Initialize privacy manager.

        Args:
            serial: Optional device serial (auto-detects if None)
            user: The Android user whose permissions are changed and read
                back. 0, the system user, is ``pm grant``'s own default.
        """
        self.serial = serial
        self.user = user

    @property
    def _for_user(self) -> str:
        """`` for user N`` in a message, empty for the default user."""
        return f" for user {self.user}" if self.user else ""

    def device_user_ids(self) -> frozenset[int]:
        """The user ids ``pm list users`` names on the device."""
        listing = adb_exec.run_adb(
            "shell", self.serial, "pm", "list", "users", timeout=ADB_TIMEOUT_SECONDS, check=True
        )
        return parse_user_ids(listing.stdout)

    def get_permission_name(self, permission: str) -> str | None:
        """
        Get full permission name from short name.

        Args:
            permission: Short name (e.g., "camera") or full name

        Returns:
            Full permission name or None if not found
        """
        # Check if already full name
        if permission.startswith("android.permission."):
            return permission

        # Look up short name
        return self.SUPPORTED_PERMISSIONS.get(permission.lower())

    def grant_permission(self, package: str, permission: str) -> tuple:
        """
        Grant permission to app.

        Args:
            package: App package name
            permission: Permission to grant (short or full name)

        Returns:
            (success, message) tuple
        """
        full_permission = self.get_permission_name(permission)
        if not full_permission:
            return (
                False,
                f"Unknown permission: {permission}. Use --list-permissions to see available.",
            )

        try:
            adb_exec.run_adb(
                "shell",
                self.serial,
                "pm",
                "grant",
                "--user",
                quote_for_device_shell(str(self.user)),
                quote_for_device_shell(package),
                quote_for_device_shell(full_permission),
                timeout=ADB_TIMEOUT_SECONDS,
                check=True,
                confirm_device_errors=True,
            )
        except adb_exec.AdbCommandError as e:
            error_msg = e.stderr if e.stderr else str(e)
            # Check common errors
            if "unknown package" in error_msg.lower():
                return False, f"Package not found: {package}"
            refusal = platform_refusal(e.stderr or e.stdout)
            if refusal is not None:
                return False, f"Failed to grant permission{self._for_user}: {refusal}"
            return False, f"Failed to grant permission: {error_msg}"

        confirmed, detail = self.confirm_permission(package, full_permission, expect_granted=True)
        if not confirmed:
            return (
                False,
                f"Grant of {permission} to {package}{self._for_user} did not take effect: "
                f"{detail}",
            )
        return True, f"Granted {permission} to {package}{self._for_user} (verified)"

    def revoke_permission(self, package: str, permission: str) -> tuple:
        """
        Revoke permission from app.

        Args:
            package: App package name
            permission: Permission to revoke (short or full name)

        Returns:
            (success, message) tuple
        """
        full_permission = self.get_permission_name(permission)
        if not full_permission:
            return (
                False,
                f"Unknown permission: {permission}. Use --list-permissions to see available.",
            )

        try:
            adb_exec.run_adb(
                "shell",
                self.serial,
                "pm",
                "revoke",
                "--user",
                quote_for_device_shell(str(self.user)),
                quote_for_device_shell(package),
                quote_for_device_shell(full_permission),
                timeout=ADB_TIMEOUT_SECONDS,
                check=True,
                confirm_device_errors=True,
            )
        except adb_exec.AdbCommandError as e:
            error_msg = e.stderr if e.stderr else str(e)
            refusal = platform_refusal(e.stderr or e.stdout)
            if refusal is not None:
                return False, f"Failed to revoke permission{self._for_user}: {refusal}"
            return False, f"Failed to revoke permission: {error_msg}"

        confirmed, detail = self.confirm_permission(package, full_permission, expect_granted=False)
        if not confirmed:
            return (
                False,
                f"Revoke of {permission} from {package}{self._for_user} did not take effect: "
                f"{detail}",
            )
        return True, f"Revoked {permission} from {package}{self._for_user} (verified){detail}"

    def confirm_permission(
        self, package: str, full_permission: str, expect_granted: bool
    ) -> tuple[bool, str]:
        """Read a permission's state back from the device and judge the outcome.

        This is the check ``pm grant`` and ``pm revoke`` cannot supply
        themselves. Granting a permission the app never requested is a silent
        no-op: no output on either stream, exit status 0, and the permission
        still not held -- recorded as ``pm_grant_not_requested``. "Exit 0 and
        nothing printed" is therefore what BOTH a working grant and a no-op
        look like, so the only honest evidence is the state afterwards.

        Args:
            package: App package name.
            full_permission: Full permission name, e.g. ``android.permission.CAMERA``.
            expect_granted: The state the caller's command was supposed to
                produce.

        Returns:
            (confirmed, detail). ``detail`` explains the disagreement when
            ``confirmed`` is False, and is empty or a parenthetical note
            otherwise. Revoking is judged on "not held", so a permission the
            package does not even request counts as revoked -- with a note,
            because it usually means a typo.
        """
        try:
            dump = adb_exec.run_adb(
                "shell",
                self.serial,
                "dumpsys",
                "package",
                quote_for_device_shell(package),
                timeout=ADB_TIMEOUT_SECONDS,
                check=True,
            ).stdout
        except adb_exec.AdbCommandError as e:
            return False, f"could not read {package} back to check: {e.stderr or e}"

        parsed = parse_package_permissions(dump, user=self.user)
        problem = self.user_state_problem(package, parsed)
        if problem is not None:
            return False, problem
        state = next(
            (
                bool(entry["granted"])
                for entry in parsed["install"] + parsed["runtime"]
                if entry["permission"] == full_permission
            ),
            None,
        )
        if expect_granted:
            if state is True:
                return True, ""
            if state is None:
                return False, (
                    f"{package} does not request {full_permission}, so `pm grant` "
                    f"exited 0 without changing anything"
                )
            return False, f"{full_permission} is still denied for {package}"

        if state is True:
            return False, f"{full_permission} is still granted for {package}"
        if state is None:
            return True, " (package does not request it)"
        return True, ""

    def user_state_problem(self, package: str, parsed: dict) -> str | None:
        """Why ``parsed`` cannot answer for :attr:`user`, or None when it can.

        Never falls back to another user's section: a read-back that cannot
        find the one asked for is a failure, because the only alternative is
        reporting a state nobody checked. The rule itself is
        :func:`common.device_utils.user_state_error`, the same one
        ``permission_state`` (and so push_notification) goes through.
        """
        if not parsed["found"]:
            return f"{package} is not installed on this device"
        unreadable = user_state_error(parsed)
        if unreadable is None:
            return None
        if isinstance(unreadable, UserNotInstalledError):
            return (
                f"{unreadable.describe(package)}; install it with "
                f"`{unreadable.install_command(package)}`"
            )
        return unreadable.describe(package)

    def reset_permission(self, package: str, permission: str) -> tuple:
        """
        Reset permission to its default state.

        Android-native note: there is no adb/pm command that returns a runtime
        permission to the original "ask on next use" prompt state (unlike iOS's
        ``xcrun simctl privacy reset``). The closest native operation is
        ``pm revoke``, which sets the permission back to denied. This method is
        therefore an explicit alias for :meth:`revoke_permission`; the returned
        message records that the reset was performed via revoke for transparency.

        Args:
            package: App package name
            permission: Permission to reset (short or full name)

        Returns:
            (success, message) tuple
        """
        success, message = self.revoke_permission(package, permission)
        if success:
            return (
                True,
                f"Reset {permission} for {package} (via revoke; Android has no prompt reset)",
            )
        return success, message

    def list_app_permissions(self, package: str) -> tuple:
        """
        List all permissions for an app, split by when they are decided.

        The previous implementation looked for a ``granted permissions:``
        header. ``dumpsys package`` has no such section, so ``--list`` reported
        an empty result for every package on every device while exiting 0. See
        :mod:`common.device_utils` for the sections that do exist and for the
        two places the dump repeats them.

        Args:
            package: App package name

        Returns:
            (success, message, permissions_dict) tuple. The dict carries
            ``install`` and ``runtime`` entries with their real ``granted``
            state, plus flattened ``granted`` / ``denied`` name lists.
        """
        try:
            result = adb_exec.run_adb(
                "shell",
                self.serial,
                "dumpsys",
                "package",
                quote_for_device_shell(package),
                timeout=ADB_TIMEOUT_SECONDS,
                check=True,
            )
        except adb_exec.AdbCommandError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to list permissions: {error_msg}", None

        permissions_data = {
            "package": package,
            **parse_package_permissions(result.stdout, user=self.user),
        }
        if not permissions_data["found"]:
            # `dumpsys package <unknown>` prints "Unable to find package: X"
            # and exits 0, so an empty result must not read as "no permissions".
            return False, f"Package not installed on this device: {package}", permissions_data
        problem = self.user_state_problem(package, permissions_data)
        if problem is not None:
            return False, problem, permissions_data
        # Always true by now; the user's id stays, as what the list is for.
        del permissions_data["user_found"], permissions_data["user_installed"]
        return True, "Permissions retrieved", permissions_data


def build_test_trail(scenario: str | None, step: int | None) -> dict | None:
    """
    Build optional test-trail metadata recorded alongside an operation.

    Mirrors the iOS privacy manager's scenario/step audit fields so test trails
    are symmetric across platforms. Returns ``None`` when no metadata was
    supplied so the operation output stays unchanged for the common case.

    Args:
        scenario: Test scenario name (or None).
        step: Step number within the scenario (or None).

    Returns:
        A dict with a timestamp plus any provided fields, or None if neither
        ``scenario`` nor ``step`` was given.
    """
    if scenario is None and step is None:
        return None

    trail: dict = {"timestamp": datetime.now().isoformat()}
    if scenario is not None:
        trail["scenario"] = scenario
    if step is not None:
        trail["step"] = step
    return trail


def print_permissions(package: str, data: dict, verbose: bool) -> None:
    """Print the ``--list`` report.

    Runtime permissions come first and are always listed: they are the only
    ones a permission-flow test can move, and there are rarely more than a
    handful. Install-time permissions are counted by default and listed under
    ``--verbose``, because a system app has close to two hundred of them and
    none of them can change.

    Args:
        package: App package name.
        data: Result dict from :meth:`PrivacyManager.list_app_permissions`.
        verbose: Also list install-time and requested-only permissions.
    """
    runtime = data["runtime"]
    install = data["install"]
    held = sum(1 for entry in install if entry["granted"])
    user = f" (user {data['user']})" if data.get("user") else ""
    print(f"{package}{user}: {len(runtime)} runtime, {len(install)} install-time ({held} granted)")

    stated = {entry["permission"] for entry in install + runtime}
    unstated = [name for name in data["requested"] if name not in stated]

    if runtime:
        print("Runtime:")
        for entry in runtime:
            symbol = "✓" if entry["granted"] else "✗"
            print(f"  {symbol} {entry['permission']}")
    elif unstated:
        # Not "nothing can be granted": a requested permission the dump gives
        # no state for may still be a runtime one.
        print(
            f"Runtime: none listed ({len(unstated)} requested with no state reported; "
            f"--verbose lists them)"
        )
    else:
        print("Runtime: none (nothing here can be granted or revoked at runtime)")

    if not verbose:
        return

    print(f"Install-time ({len(install)}):")
    for entry in install:
        print(f"  {'✓' if entry['granted'] else '✗'} {entry['permission']}")

    if unstated:
        print(f"Requested, no state reported ({len(unstated)}):")
        for name in unstated:
            print(f"  ? {name}")


def parse_user_arg(value: str | None) -> int:
    """``--user``'s value as one user id; refused through common.cli otherwise.

    ``pm`` itself accepts ``all`` and ``current``, but a grant for several
    users cannot be verified by reading one user's section back (and on API 33
    ``pm grant --user all`` throws ``IllegalArgumentException``), and
    ``current`` is a different user depending on when it is asked.
    """
    if value is None:
        return 0
    if value.strip().lower() in {"all", "current", "cur"}:
        cli.fail(
            f"--user {value} is not supported: a grant is verified by reading one user's "
            f"state back, so --user takes one numeric user id",
            "Pass --user <id> (from `adb shell pm list users`), once per user to change.",
        )
    # ASCII digits only, nothing around them. `str.isdigit()` accepts "²",
    # which int() then rejects with a traceback, and int() itself reads "٣"
    # as 3 and "1_0" as 10. Leading zeros are kept: "005" is user 5.
    if re.fullmatch(r"[0-9]+", value) is None:
        cli.fail(
            f"--user {value!r} is not a user id",
            "Pass --user <id>, a non-negative number from `adb shell pm list users`.",
        )
    # Bounded BEFORE int(): past 4300 digits int() raises (Python's integer
    # string-length limit), and an Android user id is a 32-bit int anyway.
    if len(value) > _MAX_USER_DIGITS or int(value) > _MAX_USER_ID:
        shown = value if len(value) <= 20 else f"{value[:12]}... ({len(value)} digits)"
        cli.fail(
            f"--user {shown} is not a user id: Android user ids are at most {_MAX_USER_ID}",
            "Pass --user <id>, a number from `adb shell pm list users`.",
        )
    return int(value)


def require_user_on_device(manager: PrivacyManager) -> None:
    """Refuse a ``--user`` the device does not have, before anything is changed.

    Checked against ``pm list users`` because ``pm grant --user <missing id>``
    prints nothing and exits 0 (recorded: pm_grant_user_missing); without this
    the only sign would be the read-back's missing section, after the fact.
    """
    try:
        users = manager.device_user_ids()
    except adb_exec.AdbCommandError as e:
        cli.fail(
            f"could not list the device's users to check user {manager.user}: {e}",
            "Check the device answers `adb shell pm list users`, then retry.",
        )
    if not users:
        cli.fail(
            f"`pm list users` named no users, so user {manager.user} could not be checked",
            "Run `adb shell pm list users` and check its output, then retry.",
        )
    if manager.user not in users:
        listed = ", ".join(str(u) for u in sorted(users))
        cli.fail(
            f"user {manager.user} does not exist on this device (`pm list users` names {listed})",
            f"Pass one of --user {listed}, or create the user first "
            f"(`adb shell pm create-user <name>`).",
        )


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = cli.parser(
        description="Manage Android app permissions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Grant camera permission
  python scripts/privacy_manager.py --grant camera --package com.myapp

  # Revoke location
  python scripts/privacy_manager.py --revoke location --package com.myapp

  # Reset a permission (Android: alias for revoke; no prompt-state reset exists)
  python scripts/privacy_manager.py --reset camera --package com.myapp

  # Grant multiple permissions
  python scripts/privacy_manager.py --grant camera,location,storage --package com.myapp

  # Record a test-trail scenario/step with the operation
  python scripts/privacy_manager.py --revoke camera --package com.myapp \\
      --scenario onboarding --step 2

  # List app permissions
  python scripts/privacy_manager.py --list --package com.myapp

  # List available permission names
  python scripts/privacy_manager.py --list-permissions

  # Grant, verify and list for a secondary user (default: user 0)
  python scripts/privacy_manager.py --grant camera --package com.myapp --user 11
        """,
    )

    parser.add_argument("--package", help="App package name")
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )

    # Operations
    op_group = parser.add_mutually_exclusive_group()
    op_group.add_argument("--grant", help="Grant permission(s) (comma-separated)")
    op_group.add_argument("--revoke", help="Revoke permission(s) (comma-separated)")
    op_group.add_argument(
        "--reset",
        help=(
            "Reset permission(s) (comma-separated). Android has no prompt-state "
            "reset, so this is an alias for revoke."
        ),
    )
    op_group.add_argument("--list", action="store_true", help="List app permissions")
    op_group.add_argument(
        "--list-permissions", action="store_true", help="List available permission names"
    )

    # Test-trail metadata (optional; recorded in operation output for parity)
    parser.add_argument("--scenario", help="Test scenario name to record in operation output")
    parser.add_argument(
        "--step", type=int, help="Step number within the test scenario to record in output"
    )

    parser.add_argument(
        "--user",
        help=(
            "Android user id to grant/revoke for and read back (default 0, the system "
            "user). Must be listed by `pm list users`; 'all' and 'current' are refused."
        ),
    )

    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args(argv)

    manager = PrivacyManager(serial=args.device_serial, user=parse_user_arg(args.user))

    # List available permissions
    if args.list_permissions:
        if args.json:
            print(json.dumps({"permissions": manager.SUPPORTED_PERMISSIONS}, indent=2))
        else:
            print("Available Permissions:")
            for short_name, full_name in sorted(manager.SUPPORTED_PERMISSIONS.items()):
                print(f"  {short_name:20} -> {full_name}")
        sys.exit(0)

    # Require package for other operations. Reported through common.cli: this
    # printed the whole help text to stdout, which under --json was the answer.
    if not args.package:
        cli.fail(
            "--package is required for --grant, --revoke, --reset and --list",
            "Pass --package <application id>, e.g. --package com.example.app.",
        )

    if manager.user != 0:
        require_user_on_device(manager)

    # List app permissions
    if args.list:
        success, message, perms_data = manager.list_app_permissions(args.package)

        if not success:
            # Through common.cli: under --json this was `{"success": false,
            # "message": ...}` -- no `error`, no remedy -- and the message alone
            # on stderr otherwise.
            cli.fail(
                message,
                "Check the package is installed (`app_launcher.py --list`) and the device "
                "answers (`adb devices`), then retry.",
            )
        if args.json:
            print(json.dumps(perms_data, indent=2))
        else:
            print_permissions(args.package, perms_data, args.verbose)
        sys.exit(0)

    # Grant / revoke / reset permissions (shared handling)
    op_map = {
        "grant": (args.grant, manager.grant_permission),
        "revoke": (args.revoke, manager.revoke_permission),
        "reset": (args.reset, manager.reset_permission),
    }
    for action, (raw, action_fn) in op_map.items():
        if not raw:
            continue

        permissions = [p.strip() for p in raw.split(",")]
        batch = Batch("permission", "permission").run(
            (
                ({"permission": permission}, functools.partial(action_fn, args.package, permission))
                for permission in permissions
            ),
            remedy=PERMISSION_REMEDY,
        )

        # Not under --json: the message is in `items`, and a line printed ahead
        # of the JSON object made stdout unparseable.
        if not args.json:
            for item in batch.items:
                if args.verbose or not item["success"]:
                    print(item["message"])

        test_trail = build_test_trail(args.scenario, args.step)
        extra: dict = {"action": action}
        if test_trail is not None:
            extra["test_trail"] = test_trail
        if not args.json and test_trail is not None and args.verbose:
            location = f" (step {args.step})" if args.step is not None else ""
            scenario_info = f" in {args.scenario}" if args.scenario is not None else ""
            print(f"[Test trail] {test_trail['timestamp']}: {action}{scenario_info}{location}")

        sys.exit(batch.finish(json_mode=args.json, extra=extra))

    # No operation specified. cli.fail rather than a usage error: argparse's
    # exit 2 is prose on stderr and nothing on stdout, and under --json this
    # printed the help text to stdout as the answer.
    cli.fail(
        "no operation given",
        "Pass one of --grant, --revoke or --reset (each with permission names), "
        "--list, or --list-permissions.",
    )


if __name__ == "__main__":
    cli.run(main)
