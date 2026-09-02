#!/usr/bin/env python3
"""
Notification testing from adb: post to the shade, verify what the app posted,
and toggle POST_NOTIFICATIONS.

What this tool does NOT do, and why
-----------------------------------
It cannot deliver a payload into the app's own notification code. The previous
version claimed to "simulate push notifications for testing notification
handling"; no adb path reaches that, and the implementation invented the pieces
it needed:

  S4   It broadcast to ``{package}/.NotificationReceiver`` -- a class this skill
       made up, which no real app implements -- and then called the attempt a
       success if stdout contained ``result=``. ``am broadcast`` ALWAYS prints
       "Broadcast completed: result=0" and exits 0, even for a receiver class
       that does not exist (recorded:
       ``tests/fixtures/recorded/*/am_broadcast_missing_receiver.txt``), so the
       failure branch was unreachable. A second method hardcoded
       ``{package}/.MainActivity``.

  S14  ``--list-channels`` ran ``cmd notification list channels <pkg>``. There is
       no ``list channels`` subcommand; ``cmd notification`` silently ignores the
       extra arguments, runs bare ``list`` and exits 0, so the answer was always
       "no channels found" (recorded: ``cmd_notification_help.txt``).

FCM is out of reach on purpose: the ``com.google.android.c2dm.intent.RECEIVE``
receiver is protected by ``com.google.android.c2dm.permission.SEND``, which Google
Play services holds and the shell user (uid 2000) does not. To exercise a real
FirebaseMessagingService, send through FCM itself.

Reading an app's notification *channel* configuration is deferred:
``dumpsys notification --noredact`` carries app-internal channel ids and
human-readable channel names, so it is deliberately not in the fixture set.

What it does do
---------------
1. ``--post``  Posts a notification into the shade via ``cmd notification post``.
   The notification belongs to **com.android.shell** on channel **shell_cmd** --
   NOT to the app under test. It therefore exercises a NotificationListenerService,
   the shade UI, or an agent reacting to a notification; it does NOT exercise the
   app's own channels, its receiver, or its rendering.
2. ``--list`` / ``--expect-package``  Reads back what is actually posted, from
   ``cmd notification list``. Keys are ``userId|package|id|tag|uid``. This is the
   check to run after driving the app: did package X actually post something?
   ``--expect-package`` exits non-zero when it did not, so an agent can branch.
3. ``--grant-permission`` / ``--revoke-permission``  Flips
   ``android.permission.POST_NOTIFICATIONS`` (API 33+) with ``pm grant`` /
   ``pm revoke``, for testing an app's blocked-notifications path.

Success is never inferred from a substring of stdout. It comes from the process
exit status and, for ``--post``, from the notification actually appearing in
``cmd notification list``.

Usage Examples:
    # Post into the shade (as com.android.shell) and verify it landed
    python push_notification.py --post --tag order-42 --text "Your order shipped"

    # Did the app under test post anything? Exit code answers it
    python push_notification.py --list --expect-package com.example.app

    # Exercise the notifications-blocked path
    python push_notification.py --revoke-permission --package com.example.app
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

from common.device_utils import (
    build_adb_command,
    permission_state,
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_int

# `cmd notification post` posts on behalf of the shell, not the target app.
# Verified on API 33 and 35: the notification is owned by com.android.shell and
# lands on the shell's own channel.
SHELL_PACKAGE = "com.android.shell"
SHELL_CHANNEL = "shell_cmd"

# Real, grantable, and only defined from API 33 (Android 13) onward. On older
# devices `pm grant` fails with "Unknown permission", which surfaces as a normal
# non-zero exit rather than a silent success.
POST_NOTIFICATIONS = "android.permission.POST_NOTIFICATIONS"

# Every adb call is bounded; an unbounded one wedges the connection for whatever
# runs next.
ADB_TIMEOUT = env_int("ANDROID_EMU_NOTIFICATION_TIMEOUT", 20, min_value=5)
# `cmd notification post` returns before NotificationManagerService registers
# the notification, so the read-back polls rather than reading once. Measured
# on API 35: absent immediately, present within ~2s.
VERIFY_TIMEOUT_SECONDS = env_int("ANDROID_EMU_NOTIFICATION_VERIFY_TIMEOUT", 10, min_value=1)
VERIFY_POLL_SECONDS = 0.25

# Repeated verbatim in every user-facing surface. The whole point of the rewrite
# is that nobody reads "notification posted" and believes their app posted it.
SHELL_POST_CAVEAT = (
    f"Posted as {SHELL_PACKAGE} on channel {SHELL_CHANNEL} - NOT as the target app. "
    "The app's own channels, receiver and rendering are not exercised."
)

# Exit codes used when adb never produced one of its own.
_EXIT_TIMEOUT = 124
_EXIT_ADB_MISSING = 127

# Fields in a notification key: userId|package|id|tag|uid
_KEY_FIELDS = 5


@dataclass(frozen=True)
class PostedNotification:
    """One line of ``cmd notification list`` -- a posted notification's key.

    Attributes:
        user_id: Android user id (``-1`` for a notification posted for all users).
        package: Owning package.
        notification_id: The app-chosen notification id.
        tag: The app-chosen tag, or None when the key carries the literal ``null``.
        uid: Owning uid.
        key: The verbatim line, for reuse with ``cmd notification get/snooze``.
    """

    user_id: int
    package: str
    notification_id: int
    tag: str | None
    uid: int
    key: str


def _is_int(value: str) -> bool:
    """Whether a key field is a (possibly signed) integer."""
    if not value:
        return False
    body = value[1:] if value[0] in "+-" else value
    return body.isdigit()


def parse_notification_keys(output: str) -> list[PostedNotification]:
    """Parse ``cmd notification list`` output into posted-notification records.

    The command emits one notification *key* per line, pipe-delimited as
    ``userId|package|id|tag|uid``. A tag is free text chosen by the app, so it may
    itself contain a pipe; the three leading fields and the trailing uid are fixed,
    and everything between them is the tag. Lines whose fixed fields are not
    integers are not keys and are skipped rather than guessed at.

    Args:
        output: Raw stdout of ``adb shell cmd notification list``.

    Returns:
        One record per parsed key, in the order emitted.
    """
    notifications: list[PostedNotification] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        parts = line.split("|")
        if len(parts) < _KEY_FIELDS:
            continue
        user_id, package, notification_id, uid = parts[0], parts[1], parts[2], parts[-1]
        if not (_is_int(user_id) and _is_int(notification_id) and _is_int(uid)):
            continue
        tag = "|".join(parts[3:-1])
        notifications.append(
            PostedNotification(
                user_id=int(user_id),
                package=package,
                notification_id=int(notification_id),
                tag=None if tag == "null" else tag,
                uid=int(uid),
                key=line,
            )
        )
    return notifications


class NotificationTester:
    """Post shell notifications, read back what is posted, and set POST_NOTIFICATIONS."""

    def __init__(self, serial: str | None = None):
        """Initialize the tester.

        Args:
            serial: Device serial (uses the default device when None).
        """
        self.serial = serial

    # === PUBLIC API ===

    def post(self, tag: str, text: str, verify: bool = True) -> tuple[bool, dict]:
        """Post a notification into the shade **as com.android.shell**.

        Wraps ``cmd notification post TAG TEXT`` -- the two positional arguments
        the platform documents (``post [--help | flags] TAG TEXT``). The tag is
        what the shade shows as the title and is also carried in the notification
        key, which is what makes the read-back check below possible.

        Args:
            tag: Notification tag; appears in the shade and in the key.
            text: Body text.
            verify: Read ``cmd notification list`` back and require the
                notification to be present before reporting success.

        Returns:
            (success, result_dict).
        """
        code, stdout, stderr = self._shell(
            "cmd",
            "notification",
            "post",
            quote_for_device_shell(tag),
            quote_for_device_shell(text),
        )
        result = {
            "action": "post",
            "posted_as": SHELL_PACKAGE,
            "channel": SHELL_CHANNEL,
            "tag": tag,
            "text": text,
            "caveat": SHELL_POST_CAVEAT,
            "exit_code": code,
            "verified": False,
        }

        if code != 0:
            result["error"] = (stderr or stdout).strip() or f"cmd notification post exited {code}"
            return False, result

        if not verify:
            # Exit status is the only signal left -- and it carries almost
            # nothing. Every way `cmd notification post` was made to fail on
            # API 35 (no arguments, an unknown option, a missing argument, a
            # bad icon, a bad style, a bad picture spec) exited 0, so a
            # rejection is visible ONLY in the text. Two wordings were
            # recorded: the usage block (cmd_notification_post_usage) and
            # "error: <reason>" / "Error occurred. Check logcat for details."
            # (cmd_notification_post_rejected). Checking only for the usage
            # block let a rejected icon through as a successful post.
            printed = f"{stdout}\n{stderr}".lower()
            refusals = ("usage: cmd notification", "error occurred.", "error:")
            if any(marker in printed for marker in refusals):
                result["error"] = (
                    "cmd notification post exited 0 but printed a refusal, so "
                    f"nothing was posted: {(stdout or stderr).strip()}"
                )
                return False, result
            result["note"] = "Not verified (--no-verify): exit status only."
            return True, result

        # `cmd notification post` returns before NotificationManagerService has
        # registered the notification: measured on API 35, an immediate
        # `cmd notification list` does not show it, and it appears within about
        # two seconds. Polling rather than sleeping keeps the fast path fast and
        # still gives a slow device room.
        matches: list[dict] = []
        listing: dict = {}
        deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
        while True:
            listed, listing = self.list_posted()
            if listed:
                matches = [
                    item
                    for item in listing["notifications"]
                    if item["package"] == SHELL_PACKAGE and item["tag"] == tag
                ]
                if matches:
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(VERIFY_POLL_SECONDS)

        if not listed:
            result["error"] = (
                "posted, but the read-back failed so it cannot be confirmed: "
                f"{listing.get('error', 'unknown error')}"
            )
            return False, result

        result["matches"] = matches
        if not matches:
            result["error"] = (
                f"cmd notification post exited 0 but no {SHELL_PACKAGE} notification "
                f"tagged {tag!r} appeared in `cmd notification list` within "
                f"{VERIFY_TIMEOUT_SECONDS}s. Exit 0 alone does not mean a "
                "notification exists."
            )
            return False, result

        result["verified"] = True
        result["key"] = matches[0]["key"]
        return True, result

    def list_posted(self) -> tuple[bool, dict]:
        """List every notification currently posted, from ``cmd notification list``.

        Returns:
            (success, result_dict) with parsed ``notifications`` on success.
        """
        code, stdout, stderr = self._shell("cmd", "notification", "list")
        if code != 0:
            return False, {
                "action": "list",
                "exit_code": code,
                "error": (stderr or stdout).strip() or f"cmd notification list exited {code}",
            }

        notifications = [asdict(item) for item in parse_notification_keys(stdout)]
        return True, {
            "action": "list",
            "exit_code": 0,
            "count": len(notifications),
            "packages": sorted({item["package"] for item in notifications}),
            "notifications": notifications,
        }

    def expect_package(self, package: str) -> tuple[bool, dict]:
        """Assert that ``package`` currently has at least one posted notification.

        The check an agent runs after driving the app: it answers "did the app
        actually post anything" from the platform's own record, and reports
        failure (non-zero exit from the CLI) when it did not.

        Args:
            package: Package expected to have posted a notification.

        Returns:
            (found, result_dict).
        """
        listed, listing = self.list_posted()
        if not listed:
            return False, listing

        matches = [item for item in listing["notifications"] if item["package"] == package]
        result = {
            "action": "expect-package",
            "exit_code": 0,
            "expect_package": package,
            "found": bool(matches),
            "count": len(matches),
            "notifications": matches,
            "packages_present": listing["packages"],
        }
        if not matches:
            present = ", ".join(listing["packages"]) or "none"
            result["error"] = (
                f"no notification posted by {package} (packages currently posting: {present})"
            )
        return bool(matches), result

    def set_post_permission(self, package: str, granted: bool) -> tuple[bool, dict]:
        """Grant or revoke ``android.permission.POST_NOTIFICATIONS`` for a package.

        The outcome is proved by reading the permission back out of
        ``dumpsys package``, the same way ``--post`` proves a post: ``pm grant``
        of a permission the app never requested prints nothing on either stream
        and exits 0 (recorded as ``pm_grant_not_requested``), which is
        indistinguishable from a grant that worked. Exit status alone reported
        a no-op as a success.

        Args:
            package: Target package.
            granted: True to ``pm grant``, False to ``pm revoke``.

        Returns:
            (success, result_dict). ``verified`` says whether the device's own
            record was read back and agreed.
        """
        verb = "grant" if granted else "revoke"
        code, stdout, stderr = self._shell(
            "pm", verb, quote_for_device_shell(package), POST_NOTIFICATIONS
        )
        result = {
            "action": f"{verb}-permission",
            "package": package,
            "permission": POST_NOTIFICATIONS,
            "granted": granted,
            "exit_code": code,
            "verified": False,
        }

        # `pm grant`/`pm revoke` print nothing when they work. Anything on either
        # stream is the platform explaining a refusal ("Operation not allowed",
        # "Unknown permission" below API 33), which on some builds still exits 0.
        complaint = (stderr or stdout).strip()
        if code != 0 or complaint:
            result["error"] = complaint or f"pm {verb} exited {code}"
            return False, result

        dump_code, dump, dump_err = self._shell(
            "dumpsys", "package", quote_for_device_shell(package)
        )
        if dump_code != 0:
            result["error"] = (
                f"pm {verb} exited 0, but {package} could not be read back to "
                f"confirm it: {(dump_err or dump).strip()}"
            )
            return False, result

        state = permission_state(dump, POST_NOTIFICATIONS)
        result["state"] = state
        if state is granted or (not granted and state is None):
            result["verified"] = True
            return True, result
        if state is None:
            result["error"] = (
                f"{package} does not request {POST_NOTIFICATIONS}, so `pm grant` "
                f"exited 0 without granting it"
            )
        else:
            result["error"] = (
                f"pm {verb} exited 0 but {POST_NOTIFICATIONS} is still "
                f"granted={str(state).lower()} for {package}"
            )
        return False, result

    # === INTERNAL ===

    def _shell(self, *args: str) -> tuple[int, str, str]:
        """Run one ``adb shell`` command, bounded, without a host shell.

        Args:
            *args: Device-side command and arguments, already quoted for the
                device shell where they carry untrusted text.

        Returns:
            (exit_code, stdout, stderr). adb missing or hung is reported as a
            non-zero code, never as a success.
        """
        cmd = build_adb_command("shell", self.serial, *args)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=ADB_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return _EXIT_TIMEOUT, "", f"adb timed out after {ADB_TIMEOUT}s: {' '.join(cmd)}"
        except FileNotFoundError:
            return _EXIT_ADB_MISSING, "", "adb not found on PATH"
        return completed.returncode, completed.stdout, completed.stderr


def _print_post(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--post`` report."""
    if not success:
        print(f"x Post failed: {result.get('error', 'unknown error')}", file=sys.stderr)
        return
    print(f"+ Posted tag {result['tag']!r} as {SHELL_PACKAGE} (channel {SHELL_CHANNEL})")
    print(f"  {SHELL_POST_CAVEAT}")
    if result["verified"]:
        print(f"  Verified in `cmd notification list`: {result['key']}")
    else:
        print(f"  {result.get('note', 'Not verified.')}")
    if verbose:
        print(f"  Body: {result['text']}")
        print("  Read back later with: --list --expect-package " + SHELL_PACKAGE)


def _print_list(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--list`` report."""
    if not success:
        print(f"x {result.get('error', 'unknown error')}", file=sys.stderr)
        return
    packages = ", ".join(result["packages"]) or "none"
    print(f"{result['count']} notification(s) posted by: {packages}")
    if verbose:
        for item in result["notifications"]:
            print(f"  {item['key']}")
    elif result["notifications"]:
        print("  (--verbose for keys)")


def _print_expect(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--expect-package`` report."""
    package = result.get("expect_package", "?")
    if not success:
        print(f"x {result.get('error', 'unknown error')}", file=sys.stderr)
        return
    print(f"+ {package} has {result['count']} posted notification(s)")
    if verbose:
        for item in result["notifications"]:
            print(f"  {item['key']}")


def _print_permission(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise permission report."""
    granted = bool(result.get("granted"))
    if not success:
        attempt = "grant" if granted else "revoke"
        print(
            f"x Failed to {attempt} {POST_NOTIFICATIONS}: {result.get('error', 'unknown error')}",
            file=sys.stderr,
        )
        return
    print(f"+ {'Granted' if granted else 'Revoked'} {POST_NOTIFICATIONS} for {result['package']}")
    if result.get("verified"):
        print(f"  Verified in `dumpsys package`: granted={result.get('state')}")
    if verbose:
        print("  Only meaningful on API 33+; the app must also declare the permission.")


_PRINTERS = {
    "post": _print_post,
    "list": _print_list,
    "expect-package": _print_expect,
    "grant-permission": _print_permission,
    "revoke-permission": _print_permission,
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
            "Post a notification into the shade as the system shell, verify what an "
            "app actually posted, and toggle POST_NOTIFICATIONS."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
What --post really does:
  `cmd notification post` posts as {SHELL_PACKAGE} on channel {SHELL_CHANNEL}.
  The notification does NOT belong to the app under test, so the app's own
  channels, its receiver and its rendering are NOT exercised. It is still useful
  for driving a NotificationListenerService, the shade UI, or an agent that
  reacts to a notification.

  Delivering a payload into an app's own FirebaseMessagingService is not
  possible from adb: the c2dm RECEIVE receiver is protected by a permission held
  by Google Play services, not by the shell user. Send through FCM instead.

Examples:
  python push_notification.py --post --tag order-42 --text "Your order shipped"
  python push_notification.py --post --tag t1 --text hi --no-verify --json
  python push_notification.py --list --verbose
  python push_notification.py --list --expect-package com.example.app
  python push_notification.py --grant-permission --package com.example.app
  python push_notification.py --revoke-permission --package com.example.app --json

Exit status:
  0  the action succeeded (for --post: the notification was found in
     `cmd notification list`; for --expect-package: the package has one)
  1  it did not
        """,
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--post",
        action="store_true",
        help=f"Post a notification into the shade as {SHELL_PACKAGE} (not as the app)",
    )
    action.add_argument(
        "--list",
        dest="list_posted",
        action="store_true",
        help="List the notifications currently posted (any package)",
    )
    action.add_argument(
        "--grant-permission",
        action="store_true",
        help=f"pm grant {POST_NOTIFICATIONS} to --package (API 33+)",
    )
    action.add_argument(
        "--revoke-permission",
        action="store_true",
        help=f"pm revoke {POST_NOTIFICATIONS} from --package (API 33+)",
    )

    parser.add_argument("--tag", help="With --post: tag, shown as the title and kept in the key")
    parser.add_argument("--text", help="With --post: notification body text")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="With --post: skip the read-back check and trust the exit status alone",
    )
    parser.add_argument(
        "--expect-package",
        metavar="PACKAGE",
        help="With --list: exit non-zero unless PACKAGE has a posted notification",
    )
    parser.add_argument(
        "--package",
        help="Target package for --grant-permission / --revoke-permission",
    )
    parser.add_argument("--serial", help="Device serial (auto-detects if omitted)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show extended detail")
    return parser


def main() -> None:
    """Parse arguments, dispatch one action, and exit with its real status."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.post and not (args.tag and args.text):
        parser.error("--post requires --tag and --text")
    if (args.grant_permission or args.revoke_permission) and not args.package:
        parser.error("--grant-permission/--revoke-permission require --package")
    if args.expect_package and not args.list_posted:
        parser.error("--expect-package is used with --list")

    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    tester = NotificationTester(serial=serial)

    if args.post:
        success, result = tester.post(args.tag, args.text, verify=not args.no_verify)
    elif args.list_posted:
        if args.expect_package:
            success, result = tester.expect_package(args.expect_package)
        else:
            success, result = tester.list_posted()
    else:
        success, result = tester.set_post_permission(args.package, granted=args.grant_permission)

    _emit(success, result, args)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
