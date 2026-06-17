#!/usr/bin/env python3
"""
Android Status Bar Controller

Control status bar appearance for consistent screenshots and testing.
Modify battery level, signal strength, time, and other status indicators.

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
import subprocess
import sys

from common.device_utils import build_adb_command
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

    def _demo_broadcast(self, *extras: str) -> list:
        """
        Build a SystemUI demo-mode broadcast command.

        Args:
            *extras: Additional ``-e key value`` arguments for the broadcast

        Returns:
            Complete adb command list ready for subprocess.run()
        """
        return build_adb_command(
            "shell",
            self.serial,
            "am",
            "broadcast",
            "-a",
            "com.android.systemui.demo",
            *extras,
        )

    def _enter_demo_mode(self) -> None:
        """Allow and enter SystemUI demo mode (idempotent)."""
        allow = build_adb_command(
            "shell", self.serial, "settings", "put", "global", "sysui_demo_allowed", "1"
        )
        subprocess.run(allow, capture_output=True, text=True, check=True)

        enter = self._demo_broadcast("-e", "command", "enter")
        subprocess.run(enter, capture_output=True, text=True, check=True)

    def set_battery(self, level: int, charging: bool = False) -> tuple:
        """
        Set battery level display.

        Args:
            level: Battery level (0-100)
            charging: Show charging indicator

        Returns:
            (success, message) tuple
        """
        if not 0 <= level <= 100:
            return False, "Battery level must be between 0 and 100"

        # Disable real battery status
        cmd1 = build_adb_command(
            "shell", self.serial, "cmd", "statusbar", "battery-level", str(level)
        )

        try:
            subprocess.run(cmd1, capture_output=True, text=True, check=True)

            # Set charging status
            if charging:
                cmd2 = build_adb_command(
                    "shell", self.serial, "cmd", "statusbar", "battery-charging", "true"
                )
                subprocess.run(cmd2, capture_output=True, text=True, check=True)

            return True, f"Battery set to {level}%{' (charging)' if charging else ''}"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to set battery: {error_msg}"

    def set_wifi(self, enabled: bool = True, level: int = 4) -> tuple:
        """
        Set WiFi indicator.

        Args:
            enabled: Show WiFi enabled
            level: Signal level (0-4)

        Returns:
            (success, message) tuple
        """
        cmd = build_adb_command(
            "shell",
            self.serial,
            "cmd",
            "statusbar",
            "wifi-enabled",
            "true" if enabled else "false",
        )

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)

            if enabled and 0 <= level <= 4:
                cmd2 = build_adb_command(
                    "shell", self.serial, "cmd", "statusbar", "wifi-level", str(level)
                )
                subprocess.run(cmd2, capture_output=True, text=True, check=True)

            return True, f"WiFi {'enabled' if enabled else 'disabled'} (level: {level})"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to set WiFi: {error_msg}"

    def set_mobile_data(
        self, enabled: bool = True, level: int = 4, data_type: str = "lte"
    ) -> tuple:
        """
        Set mobile data indicator.

        Args:
            enabled: Show mobile data enabled
            level: Signal level (0-4)
            data_type: Data type (lte, 3g, 4g, 5g)

        Returns:
            (success, message) tuple
        """
        cmd = build_adb_command(
            "shell",
            self.serial,
            "cmd",
            "statusbar",
            "mobile-enabled",
            "true" if enabled else "false",
        )

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)

            if enabled:
                # Set signal level
                cmd2 = build_adb_command(
                    "shell", self.serial, "cmd", "statusbar", "mobile-level", str(level)
                )
                subprocess.run(cmd2, capture_output=True, text=True, check=True)

                # Set data type
                cmd3 = build_adb_command(
                    "shell", self.serial, "cmd", "statusbar", "mobile-datatype", data_type
                )
                subprocess.run(cmd3, capture_output=True, text=True, check=True)

            return (
                True,
                f"Mobile data {'enabled' if enabled else 'disabled'} ({data_type}, level: {level})",
            )

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to set mobile data: {error_msg}"

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
            cmd3 = self._demo_broadcast(
                "-e",
                "command",
                "clock",
                "-e",
                "hhmm",
                time_str.replace(":", ""),
            )
            subprocess.run(cmd3, capture_output=True, text=True, check=True)

            return True, f"Time set to {time_str} (demo mode)"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to set time: {error_msg}"

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
                clock = self._demo_broadcast(
                    "-e", "command", "clock", "-e", "hhmm", time.replace(":", "")
                )
                subprocess.run(clock, capture_output=True, text=True, check=True)
                applied.append(f"time={time}")

            if battery is not None:
                batt = self._demo_broadcast(
                    "-e",
                    "command",
                    "battery",
                    "-e",
                    "level",
                    str(battery),
                    "-e",
                    "plugged",
                    "true" if charging else "false",
                )
                subprocess.run(batt, capture_output=True, text=True, check=True)
                applied.append(f"battery={battery}%{' charging' if charging else ''}")

            # Airplane mode is mutually exclusive with live radios; apply it
            # before the per-radio toggles so the final icon state is coherent.
            if airplane:
                air = self._demo_broadcast("-e", "command", "network", "-e", "airplane", "show")
                subprocess.run(air, capture_output=True, text=True, check=True)
                applied.append("airplane=on")

            if wifi is not None:
                if wifi:
                    wifi_cmd = self._demo_broadcast(
                        "-e",
                        "command",
                        "network",
                        "-e",
                        "wifi",
                        "show",
                        "-e",
                        "level",
                        str(wifi_level),
                    )
                else:
                    wifi_cmd = self._demo_broadcast(
                        "-e", "command", "network", "-e", "wifi", "hide"
                    )
                subprocess.run(wifi_cmd, capture_output=True, text=True, check=True)
                applied.append(f"wifi={'on' if wifi else 'off'}")

            if mobile is not None:
                if mobile:
                    mobile_cmd = self._demo_broadcast(
                        "-e",
                        "command",
                        "network",
                        "-e",
                        "mobile",
                        "show",
                        "-e",
                        "level",
                        str(mobile_level),
                        "-e",
                        "datatype",
                        mobile_type,
                    )
                else:
                    mobile_cmd = self._demo_broadcast(
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
                subprocess.run(mobile_cmd, capture_output=True, text=True, check=True)
                applied.append(f"mobile={'on' if mobile else 'off'}")

            summary = ", ".join(applied) if applied else "no changes"
            return True, f"Status bar override applied ({summary})"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to apply override: {error_msg}"

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
        # Exit demo mode
        cmd = self._demo_broadcast("-e", "command", "exit")

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Reset statusbar settings
            cmd2 = build_adb_command(
                "shell", self.serial, "settings", "put", "global", "sysui_demo_allowed", "0"
            )
            subprocess.run(cmd2, capture_output=True, text=True, check=True)

            return True, "Status bar reset to actual values"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to reset: {error_msg}"


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

    # Reset operation
    if args.reset:
        success, message = controller.reset()
        results.append({"operation": "reset", "success": success, "message": message})

    # Preset operation (atomic, coherent group via demo mode)
    if args.preset:
        success, message = controller.apply_preset(args.preset)
        results.append(
            {"operation": "preset", "preset": args.preset, "success": success, "message": message}
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
