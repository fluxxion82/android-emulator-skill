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

Note on --reset:
    Unlike iOS (xcrun simctl privacy reset), Android has no command that returns
    a runtime permission to the original "ask on next use" prompt state. The
    closest device-native operation is ``pm revoke``, which sets the permission
    back to denied. ``--reset`` is therefore implemented as an explicit alias for
    revoke so test trails can express intent symmetrically with iOS; the recorded
    output marks the reset as revoke-based for transparency.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime

from common.device_utils import (
    build_adb_command,
    parse_package_permissions,
    permission_state,
    quote_for_device_shell,
)
from common.env_config import env_int

# Subprocess timeout (seconds) for adb calls. Permission ops are fast; dumpsys
# package can take longer on a busy device, so allow an override.
ADB_TIMEOUT_SECONDS = env_int("ANDROID_EMU_PRIVACY_ADB_TIMEOUT", 30, min_value=1)


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

    def __init__(self, serial: str | None = None):
        """
        Initialize privacy manager.

        Args:
            serial: Optional device serial (auto-detects if None)
        """
        self.serial = serial

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

        cmd = build_adb_command(
            "shell",
            self.serial,
            "pm",
            "grant",
            quote_for_device_shell(package),
            quote_for_device_shell(full_permission),
        )

        try:
            subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=ADB_TIMEOUT_SECONDS
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            # Check common errors
            if "unknown package" in error_msg.lower():
                return False, f"Package not found: {package}"
            return False, f"Failed to grant permission: {error_msg}"

        confirmed, detail = self.confirm_permission(package, full_permission, expect_granted=True)
        if not confirmed:
            return False, f"Grant of {permission} to {package} did not take effect: {detail}"
        return True, f"Granted {permission} to {package} (verified)"

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

        cmd = build_adb_command(
            "shell",
            self.serial,
            "pm",
            "revoke",
            quote_for_device_shell(package),
            quote_for_device_shell(full_permission),
        )

        try:
            subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=ADB_TIMEOUT_SECONDS
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to revoke permission: {error_msg}"

        confirmed, detail = self.confirm_permission(package, full_permission, expect_granted=False)
        if not confirmed:
            return False, f"Revoke of {permission} from {package} did not take effect: {detail}"
        return True, f"Revoked {permission} from {package} (verified){detail}"

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
        cmd = build_adb_command(
            "shell", self.serial, "dumpsys", "package", quote_for_device_shell(package)
        )
        try:
            dump = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=ADB_TIMEOUT_SECONDS
            ).stdout
        except subprocess.CalledProcessError as e:
            return False, f"could not read {package} back to check: {e.stderr or e}"

        state = permission_state(dump, full_permission)
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
        cmd = build_adb_command(
            "shell", self.serial, "dumpsys", "package", quote_for_device_shell(package)
        )

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=ADB_TIMEOUT_SECONDS
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to list permissions: {error_msg}", None

        permissions_data = {"package": package, **parse_package_permissions(result.stdout)}
        if not permissions_data["found"]:
            # `dumpsys package <unknown>` prints "Unable to find package: X"
            # and exits 0, so an empty result must not read as "no permissions".
            return False, f"Package not installed on this device: {package}", permissions_data
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
    print(f"{package}: {len(runtime)} runtime, {len(install)} install-time ({held} granted)")

    if runtime:
        print("Runtime:")
        for entry in runtime:
            symbol = "✓" if entry["granted"] else "✗"
            print(f"  {symbol} {entry['permission']}")
    else:
        print("Runtime: none (nothing here can be granted or revoked at runtime)")

    if not verbose:
        return

    print(f"Install-time ({len(install)}):")
    for entry in install:
        print(f"  {'✓' if entry['granted'] else '✗'} {entry['permission']}")

    stated = {entry["permission"] for entry in install + runtime}
    unstated = [name for name in data["requested"] if name not in stated]
    if unstated:
        print(f"Requested, no state reported ({len(unstated)}):")
        for name in unstated:
            print(f"  ? {name}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
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

    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    manager = PrivacyManager(serial=args.device_serial)

    # List available permissions
    if args.list_permissions:
        if args.json:
            print(json.dumps({"permissions": manager.SUPPORTED_PERMISSIONS}, indent=2))
        else:
            print("Available Permissions:")
            for short_name, full_name in sorted(manager.SUPPORTED_PERMISSIONS.items()):
                print(f"  {short_name:20} -> {full_name}")
        sys.exit(0)

    # Require package for other operations
    if not args.package:
        print("Error: --package is required", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    # List app permissions
    if args.list:
        success, message, perms_data = manager.list_app_permissions(args.package)

        if args.json:
            if success:
                print(json.dumps(perms_data, indent=2))
            else:
                print(json.dumps({"success": False, "message": message}, indent=2))
        elif success:
            print_permissions(args.package, perms_data, args.verbose)
        else:
            print(message, file=sys.stderr)

        sys.exit(0 if success else 1)

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
        results = []

        for permission in permissions:
            success, message = action_fn(args.package, permission)
            results.append({"permission": permission, "success": success, "message": message})

            if args.verbose or not success:
                print(message)

        test_trail = build_test_trail(args.scenario, args.step)

        if args.json:
            output: dict = {"action": action, "results": results}
            if test_trail is not None:
                output["test_trail"] = test_trail
            print(json.dumps(output, indent=2))
        elif test_trail is not None and args.verbose:
            location = f" (step {args.step})" if args.step is not None else ""
            scenario_info = f" in {args.scenario}" if args.scenario is not None else ""
            print(f"[Test trail] {test_trail['timestamp']}: {action}{scenario_info}{location}")

        # Exit with error if any failed
        all_success = all(r["success"] for r in results)
        sys.exit(0 if all_success else 1)

    # No operation specified
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
