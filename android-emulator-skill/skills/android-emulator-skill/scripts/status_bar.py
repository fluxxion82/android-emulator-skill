#!/usr/bin/env python3
"""
Android Status Bar Controller

Control status bar appearance for consistent screenshots and testing.
Modify battery level, signal strength, time, and other status indicators.

Every adb call goes through :func:`common.adb_exec.run_adb`, so each one is
bounded and each device-level failure ("more than one device", "device
offline", ...) arrives as a typed error whose message names a remedy. An
unbounded adb call does not merely hang this script: it wedges the adb
connection for whatever runs next.

Usage Examples:
    # Set battery to 50%
    python scripts/status_bar.py --battery 50

    # Set full signal
    python scripts/status_bar.py --signal full

    # Set time to 9:41 AM
    python scripts/status_bar.py --time "9:41"

    # Apply a coherent demo-mode preset (clean status bar for screenshots)
    python scripts/status_bar.py --preset clean

    # Reset to actual status
    python scripts/status_bar.py --reset
"""

import argparse
import json
import sys

from common import adb_exec
from common.device_utils import quote_for_device_shell
from common.env_config import env_int

# Tunable battery levels for presets (overridable via ANDROID_EMU_* env vars).
# Kept as module-level tunables so demo presets stay coherent and adjustable
# without code edits (e.g. raising the "low battery" threshold for testing).
PRESET_LOW_BATTERY_LEVEL = env_int("ANDROID_EMU_PRESET_LOW_BATTERY_LEVEL", 15, min_value=0)
PRESET_TESTING_BATTERY_LEVEL = env_int("ANDROID_EMU_PRESET_TESTING_BATTERY_LEVEL", 50, min_value=0)


class StatusBarController:
    """Controls Android status bar appearance."""

    # Coherent demo-mode preset groups. Each maps to override() keyword args.
    # Android-native equivalent of iOS `simctl status_bar override` presets:
    # all values are pushed atomically through SystemUI demo mode.
    PRESETS = {
        "clean": {
            "time": "9:41",
            "battery": 100,
            "charging": False,
            "wifi": True,
            "wifi_level": 4,
            "mobile": True,
            "mobile_level": 4,
            "mobile_type": "5g",
            "airplane": False,
        },
        "testing": {
            "time": "11:11",
            "battery": PRESET_TESTING_BATTERY_LEVEL,
            "charging": False,
            "wifi": True,
            "wifi_level": 3,
            "mobile": True,
            "mobile_level": 3,
            "mobile_type": "lte",
            "airplane": False,
        },
        "low-battery": {
            "time": "9:41",
            "battery": PRESET_LOW_BATTERY_LEVEL,
            "charging": False,
            "wifi": True,
            "wifi_level": 4,
            "mobile": True,
            "mobile_level": 4,
            "mobile_type": "5g",
            "airplane": False,
        },
        "airplane": {
            "time": "9:41",
            "battery": 100,
            "charging": True,
            "wifi": False,
            "mobile": False,
            "airplane": True,
        },
    }

    def __init__(self, serial: str | None = None):
        """
        Initialize status bar controller.

        Args:
            serial: Optional device serial (auto-detects if None)
        """
        self.serial = serial
        # Whether this controller has already opened the demo-mode gate on the
        # device. See _enter_demo_mode().
        self._demo_mode_entered = False

    def _demo_broadcast(self, *extras: str) -> adb_exec.AdbResult:
        """
        Send a SystemUI demo-mode broadcast.

        The extras are forwarded to the device shell as they arrive, so each
        caller quotes its own non-literal values (``quote_for_device_shell``)
        before handing them over -- adb joins the host argv into one string and
        the device's ``sh -c`` re-parses it, which is what let a mobile
        ``--datatype`` or a ``--time`` string carrying ``;`` run a second
        command. Callers wrap the numeric levels travelling in the same argv
        too: ``shlex.quote("4")`` is ``4``, and quoting the whole group keeps
        the rule "wrap what you interpolate" instead of asking each reader to
        re-derive which of two adjacent values can hold a metacharacter.

        Args:
            *extras: Additional ``-e key value`` arguments for the broadcast

        Returns:
            The :class:`~common.adb_exec.AdbResult` for the broadcast

        Raises:
            adb_exec.AdbCommandError: the broadcast exited non-zero
            adb_exec.DeviceError: the command never reached a device
        """
        return adb_exec.run_adb(
            "shell",
            self.serial,
            "am",
            "broadcast",
            "-a",
            "com.android.systemui.demo",
            *extras,
            check=True,
        )

    def _enter_demo_mode(self) -> None:
        """Allow and enter SystemUI demo mode (idempotent).

        Demo broadcasts are silently ignored unless ``sysui_demo_allowed`` is
        set *and* the ``enter`` command has been sent, so this precedes every
        one of them.

        The two calls are made at most once per controller: a single CLI
        invocation can apply several settings, and re-entering an already
        entered demo mode costs two adb round trips per setter without changing
        the device state. The flag is per-process and short-lived, and the one
        thing that closes the gate again -- :meth:`reset` -- clears it, so a
        later setter re-enters rather than broadcasting into a closed gate.
        """
        if self._demo_mode_entered:
            return

        adb_exec.run_adb(
            "shell",
            self.serial,
            "settings",
            "put",
            "global",
            "sysui_demo_allowed",
            "1",
            check=True,
        )
        self._demo_broadcast("-e", "command", "enter")
        self._demo_mode_entered = True

    def set_battery(self, level: int, charging: bool = False) -> tuple:
        """
        Set the battery indicator via SystemUI demo mode.

        Args:
            level: Battery level (0-100)
            charging: Show the charging indicator

        Returns:
            (success, message) tuple

        This previously issued ``cmd statusbar battery-level``, which does not
        exist -- recorded `cmd statusbar` help on API 33 and 35 lists only
        expand-notifications / collapse / add-tile / set-tiles / click-tile and
        friends. "battery-level" is a demo-mode broadcast extra.
        """
        if not 0 <= level <= 100:
            return False, "Battery level must be between 0 and 100"

        try:
            self._enter_demo_mode()
            self._demo_broadcast(
                "-e",
                "command",
                "battery",
                "-e",
                "level",
                str(level),
                "-e",
                "plugged",
                "true" if charging else "false",
            )
            return True, f"Battery set to {level}%{' (charging)' if charging else ''}"
        except adb_exec.AdbCommandError as e:
            return False, f"Failed to set battery: {e}"

    def set_wifi(self, enabled: bool = True, level: int = 4) -> tuple:
        """
        Set the wifi indicator via SystemUI demo mode.

        Args:
            enabled: Show the wifi icon
            level: Signal bars (0-4)

        Returns:
            (success, message) tuple
        """
        if not 0 <= level <= 4:
            return False, "WiFi level must be between 0 and 4"

        try:
            self._enter_demo_mode()
            self._demo_broadcast(
                "-e",
                "command",
                "network",
                "-e",
                "wifi",
                "show" if enabled else "hide",
                "-e",
                "level",
                str(level),
            )
            state = f"shown at level {level}" if enabled else "hidden"
            return True, f"WiFi {state}"
        except adb_exec.AdbCommandError as e:
            return False, f"Failed to set wifi: {e}"

    def set_mobile_data(self, enabled: bool = True, level: int = 4, datatype: str = "lte") -> tuple:
        """
        Set the mobile-data indicator via SystemUI demo mode.

        Args:
            enabled: Show the mobile icon
            level: Signal bars (0-4)
            datatype: Network type shown (lte, 4g, 3g, e, g, hspa, ...)

        Returns:
            (success, message) tuple
        """
        if not 0 <= level <= 4:
            return False, "Mobile level must be between 0 and 4"

        try:
            self._enter_demo_mode()
            self._demo_broadcast(
                "-e",
                "command",
                "network",
                "-e",
                "mobile",
                "show" if enabled else "hide",
                "-e",
                "level",
                quote_for_device_shell(str(level)),
                "-e",
                "datatype",
                quote_for_device_shell(datatype),
            )
            state = f"shown at level {level} ({datatype})" if enabled else "hidden"
            return True, f"Mobile data {state}"
        except adb_exec.AdbCommandError as e:
            return False, f"Failed to set mobile data: {e}"

    def set_time(self, time_str: str) -> tuple:
        """
        Set time display (for screenshots).

        Args:
            time_str: Time string (e.g., "9:41")

        Returns:
            (success, message) tuple
        """
        # Note: This requires custom implementation as Android doesn't have
        # a built-in command for this. Using demo mode on supported devices.

        try:
            # Enable + enter demo mode
            self._enter_demo_mode()

            # Set time
            self._demo_broadcast(
                "-e",
                "command",
                "clock",
                "-e",
                "hhmm",
                quote_for_device_shell(time_str.replace(":", "")),
            )

            return True, f"Time set to {time_str} (demo mode)"

        except adb_exec.AdbCommandError as e:
            return False, f"Failed to set time: {e}"

    def override(
        self,
        time: str | None = None,
        battery: int | None = None,
        charging: bool = False,
        wifi: bool | None = None,
        wifi_level: int = 4,
        mobile: bool | None = None,
        mobile_level: int = 4,
        mobile_type: str = "lte",
        airplane: bool = False,
    ) -> tuple:
        """
        Atomically push a coherent group of demo-mode status bar values.

        This is the Android-native equivalent of ``simctl status_bar override``:
        it enters SystemUI demo mode once, then issues each demo broadcast in a
        single path so the values stay consistent. Used by presets, and usable
        directly for ad-hoc multi-field overrides.

        Args:
            time: Time string in H:MM form (e.g., "9:41")
            battery: Battery level (0-100)
            charging: Show the battery as charging/plugged in
            wifi: Show (True) or hide (False) the WiFi icon; None leaves it alone
            wifi_level: WiFi signal level (0-4)
            mobile: Show (True) or hide (False) the mobile icon; None leaves it
            mobile_level: Mobile signal level (0-4)
            mobile_type: Mobile data type (lte, 3g, 4g, 5g, ...)
            airplane: Show the airplane-mode icon

        Returns:
            (success, message) tuple
        """
        if battery is not None and not 0 <= battery <= 100:
            return False, "Battery level must be between 0 and 100"

        try:
            # Enter demo mode once for the whole atomic group.
            self._enter_demo_mode()

            applied = []

            if time:
                self._demo_broadcast(
                    "-e",
                    "command",
                    "clock",
                    "-e",
                    "hhmm",
                    quote_for_device_shell(time.replace(":", "")),
                )
                applied.append(f"time={time}")

            if battery is not None:
                self._demo_broadcast(
                    "-e",
                    "command",
                    "battery",
                    "-e",
                    "level",
                    quote_for_device_shell(str(battery)),
                    "-e",
                    "plugged",
                    "true" if charging else "false",
                )
                applied.append(f"battery={battery}%{' charging' if charging else ''}")

            # Airplane mode is mutually exclusive with live radios; apply it
            # before the per-radio toggles so the final icon state is coherent.
            if airplane:
                self._demo_broadcast("-e", "command", "network", "-e", "airplane", "show")
                applied.append("airplane=on")

            if wifi is not None:
                if wifi:
                    self._demo_broadcast(
                        "-e",
                        "command",
                        "network",
                        "-e",
                        "wifi",
                        "show",
                        "-e",
                        "level",
                        quote_for_device_shell(str(wifi_level)),
                    )
                else:
                    self._demo_broadcast("-e", "command", "network", "-e", "wifi", "hide")
                applied.append(f"wifi={'on' if wifi else 'off'}")

            if mobile is not None:
                if mobile:
                    self._demo_broadcast(
                        "-e",
                        "command",
                        "network",
                        "-e",
                        "mobile",
                        "show",
                        "-e",
                        "level",
                        quote_for_device_shell(str(mobile_level)),
                        "-e",
                        "datatype",
                        quote_for_device_shell(mobile_type),
                    )
                else:
                    self._demo_broadcast(
                        "-e",
                        "command",
                        "network",
                        "-e",
                        "mobile",
                        "hide",
                        "-e",
                        "datatype",
                        "none",
                    )
                applied.append(f"mobile={'on' if mobile else 'off'}")

            summary = ", ".join(applied) if applied else "no changes"
            return True, f"Status bar override applied ({summary})"

        except adb_exec.AdbCommandError as e:
            return False, f"Failed to apply override: {e}"

    def apply_preset(self, name: str) -> tuple:
        """
        Apply a named coherent demo-mode preset atomically.

        Args:
            name: One of the keys in :data:`StatusBarController.PRESETS`

        Returns:
            (success, message) tuple
        """
        preset = self.PRESETS.get(name)
        if preset is None:
            return False, f"Unknown preset: {name}"

        success, message = self.override(**preset)
        if success:
            return True, f"Preset '{name}' applied: {message}"
        return False, f"Preset '{name}' failed: {message}"

    def reset(self) -> tuple:
        """
        Reset status bar to actual system values.

        Returns:
            (success, message) tuple
        """
        # The gate is about to be closed, so any later setter on this controller
        # must open it again rather than broadcast into a closed one.
        self._demo_mode_entered = False

        try:
            # Exit demo mode.
            self._demo_broadcast("-e", "command", "exit")

            # Reset statusbar settings.
            adb_exec.run_adb(
                "shell",
                self.serial,
                "settings",
                "put",
                "global",
                "sysui_demo_allowed",
                "0",
                check=True,
            )

            return True, "Status bar reset to actual values"

        except adb_exec.AdbCommandError as e:
            return False, f"Failed to reset: {e}"


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Control Android status bar appearance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Set battery to 50%
  python scripts/status_bar.py --battery 50

  # Set battery charging
  python scripts/status_bar.py --battery 75 --charging

  # Set full WiFi signal
  python scripts/status_bar.py --wifi --wifi-level 4

  # Set time for screenshots
  python scripts/status_bar.py --time "9:41"

  # Apply a coherent demo-mode preset
  python scripts/status_bar.py --preset clean
  python scripts/status_bar.py --preset low-battery

  # Reset to actual status
  python scripts/status_bar.py --reset
        """,
    )

    parser.add_argument(
        "--preset",
        choices=list(StatusBarController.PRESETS.keys()),
        help="Apply a coherent demo-mode group atomically "
        "(clean, testing, low-battery, airplane)",
    )
    parser.add_argument("--battery", type=int, help="Battery level (0-100)")
    parser.add_argument("--charging", action="store_true", help="Show charging indicator")
    parser.add_argument("--wifi", action="store_true", help="Enable WiFi indicator")
    parser.add_argument("--wifi-level", type=int, default=4, help="WiFi signal level (0-4)")
    parser.add_argument("--mobile", action="store_true", help="Enable mobile data")
    parser.add_argument("--mobile-level", type=int, default=4, help="Mobile signal level (0-4)")
    parser.add_argument("--mobile-type", default="lte", help="Mobile data type (lte, 3g, 4g, 5g)")
    parser.add_argument("--time", help="Set time display (e.g., 9:41)")
    parser.add_argument("--reset", action="store_true", help="Reset to actual values")
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    controller = StatusBarController(serial=args.device_serial)
    results = []

    # A device-level failure (no device, several devices, offline, unauthorized)
    # means the command never ran at all, so it is raised rather than returned.
    # Its message already names the remedy; a traceback would bury it.
    try:
        # Reset operation
        if args.reset:
            success, message = controller.reset()
            results.append({"operation": "reset", "success": success, "message": message})

        # Preset operation (atomic, coherent group via demo mode)
        if args.preset:
            success, message = controller.apply_preset(args.preset)
            results.append(
                {
                    "operation": "preset",
                    "preset": args.preset,
                    "success": success,
                    "message": message,
                }
            )

        # Battery operation
        if args.battery is not None:
            success, message = controller.set_battery(args.battery, args.charging)
            results.append({"operation": "battery", "success": success, "message": message})

        # WiFi operation
        if args.wifi:
            success, message = controller.set_wifi(True, args.wifi_level)
            results.append({"operation": "wifi", "success": success, "message": message})

        # Mobile data operation
        if args.mobile:
            success, message = controller.set_mobile_data(True, args.mobile_level, args.mobile_type)
            results.append({"operation": "mobile", "success": success, "message": message})

        # Time operation
        if args.time:
            success, message = controller.set_time(args.time)
            results.append({"operation": "time", "success": success, "message": message})
    except adb_exec.AdbError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    # Output results
    if not results:
        parser.print_help()
        sys.exit(1)

    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        for result in results:
            if args.verbose or not result["success"]:
                print(result["message"])

    # Exit with error if any failed
    all_success = all(r["success"] for r in results)
    sys.exit(0 if all_success else 1)


if __name__ == "__main__":
    main()
