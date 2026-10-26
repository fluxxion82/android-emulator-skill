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

    # Set time to 9:41 (H:MM or HH:MM; sent to SystemUI as 0941)
    python scripts/status_bar.py --time "9:41"

    # Hide the WiFi icon (bare --wifi shows it)
    python scripts/status_bar.py --wifi hide

    # Apply a coherent demo-mode preset (clean status bar for screenshots)
    python scripts/status_bar.py --preset clean

    # Reset to actual status
    python scripts/status_bar.py --reset
"""

import argparse
import functools
import re
import sys

from common import adb_exec, cli
from common.batch import Batch
from common.device_utils import quote_for_device_shell
from common.env_config import env_int

# Tunable battery levels for presets (overridable via ANDROID_EMU_* env vars).
# Kept as module-level tunables so demo presets stay coherent and adjustable
# without code edits (e.g. raising the "low battery" threshold for testing).
PRESET_LOW_BATTERY_LEVEL = env_int("ANDROID_EMU_PRESET_LOW_BATTERY_LEVEL", 15, min_value=0)
PRESET_TESTING_BATTERY_LEVEL = env_int("ANDROID_EMU_PRESET_TESTING_BATTERY_LEVEL", 50, min_value=0)

# H:MM or HH:MM. ASCII digits only: `\d` would also take other scripts' digits.
_TIME_PATTERN = re.compile(r"([0-9]{1,2}):([0-9]{2})")

# What to do about one operation that did not take; its message says which (a
# level out of range, SystemUI refusing the broadcast).
STATUS_BAR_REMEDY = (
    "Fix what the error names, then retry that operation; `--reset` leaves demo mode."
)

TIME_REMEDY = "Pass the time as H:MM or HH:MM on a 24-hour clock, e.g. --time 9:41 or --time 13:05."


def demo_clock_hhmm(time_str: str) -> str:
    """Turn ``H:MM`` / ``HH:MM`` into the four-digit ``hhmm`` SystemUI's demo clock takes.

    SystemUI ignores an ``hhmm`` that is not four digits -- no error, exit 0,
    ``Broadcast completed: result=0`` -- so ``"9:41"`` with its colon stripped
    (``941``) left the real time on screen while every check passed. Recorded:
    ``tests/fixtures/recorded/emulator-api35/demo_mode_clock_941.png`` shows the
    device's own clock, ``demo_mode_clock_0941.png`` shows 9:41.

    Colon-less input (``941``, ``0941``) is refused rather than guessed at: the
    colon is what says where the hour ends.

    Args:
        time_str: The time as given, e.g. ``"9:41"`` or ``"09:41"``.

    Returns:
        The zero-padded ``hhmm``, e.g. ``"0941"``.

    Raises:
        ValueError: ``time_str`` is not H:MM / HH:MM in 00:00-23:59.
    """
    match = _TIME_PATTERN.fullmatch(time_str)
    if match is not None:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            return f"{hour:02d}{minute:02d}"
    raise ValueError(
        f"--time {time_str!r} is not a time SystemUI's demo clock can show "
        "(expected H:MM or HH:MM, 00:00-23:59)"
    )


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
            time_str: Time as H:MM or HH:MM (e.g., "9:41"), sent as a
                four-digit hhmm (see :func:`demo_clock_hhmm`)

        Returns:
            (success, message) tuple; a malformed time is refused before any
            adb call
        """
        try:
            hhmm = demo_clock_hhmm(time_str)
        except ValueError as e:
            return False, str(e)

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
                quote_for_device_shell(hhmm),
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
            time: Time as H:MM or HH:MM (e.g., "9:41"); a malformed one is
                refused before any adb call
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

        hhmm = None
        if time is not None:
            try:
                hhmm = demo_clock_hhmm(time)
            except ValueError as e:
                return False, str(e)

        try:
            # Enter demo mode once for the whole atomic group.
            self._enter_demo_mode()

            applied = []

            if hhmm is not None:
                self._demo_broadcast(
                    "-e",
                    "command",
                    "clock",
                    "-e",
                    "hhmm",
                    quote_for_device_shell(hhmm),
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


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = cli.parser(
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

  # Hide the WiFi and mobile icons
  python scripts/status_bar.py --wifi hide --mobile hide

  # Set time for screenshots (H:MM or HH:MM; 9:41 is sent as 0941)
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
    parser.add_argument(
        "--wifi",
        nargs="?",
        const="show",
        choices=["show", "hide"],
        help="Show the WiFi indicator (bare --wifi or --wifi show), or hide it (--wifi hide)",
    )
    parser.add_argument("--wifi-level", type=int, default=4, help="WiFi signal level (0-4)")
    parser.add_argument(
        "--mobile",
        nargs="?",
        const="show",
        choices=["show", "hide"],
        help="Show the mobile-data indicator (bare --mobile or --mobile show), "
        "or hide it (--mobile hide)",
    )
    parser.add_argument("--mobile-level", type=int, default=4, help="Mobile signal level (0-4)")
    parser.add_argument("--mobile-type", default="lte", help="Mobile data type (lte, 3g, 4g, 5g)")
    parser.add_argument(
        "--time",
        help="Set time display as H:MM or HH:MM, 00:00-23:59 (e.g., 9:41); "
        "anything else is refused",
    )
    parser.add_argument("--reset", action="store_true", help="Reset to actual values")
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args(argv)

    # Refuse a malformed --time before anything reaches the device, so a
    # refused command changes nothing (not even the --battery beside it).
    if args.time is not None:
        try:
            demo_clock_hhmm(args.time)
        except ValueError as e:
            cli.fail(str(e), TIME_REMEDY)

    controller = StatusBarController(serial=args.device_serial)
    partial = functools.partial

    # One item per operation, in this order (common.batch). A device that goes
    # away (no device, offline, unauthorized) stops the batch at exit 3 with the
    # items so far; an operation that fails otherwise is that item's failure,
    # and the rest still run.
    steps = []
    if args.reset:
        steps.append(({"operation": "reset"}, controller.reset))
    if args.preset:
        # Atomic, coherent group via demo mode.
        steps.append(
            (
                {"operation": "preset", "preset": args.preset},
                partial(controller.apply_preset, args.preset),
            )
        )
    if args.battery is not None:
        steps.append(
            ({"operation": "battery"}, partial(controller.set_battery, args.battery, args.charging))
        )
    if args.wifi is not None:
        steps.append(
            (
                {"operation": "wifi"},
                partial(controller.set_wifi, args.wifi == "show", args.wifi_level),
            )
        )
    if args.mobile is not None:
        steps.append(
            (
                {"operation": "mobile"},
                partial(
                    controller.set_mobile_data,
                    args.mobile == "show",
                    args.mobile_level,
                    args.mobile_type,
                ),
            )
        )
    if args.time is not None:
        steps.append(({"operation": "time"}, partial(controller.set_time, args.time)))

    # No operation is a refusal: the help text went to stdout, the whole of
    # what a --json caller got.
    if not steps:
        if not args.json:
            parser.print_help(file=sys.stderr)
        cli.fail(
            "no status bar operation given",
            "Pass --preset, --battery, --wifi, --mobile, --time or --reset.",
        )

    batch = Batch("operation", "operation").run(steps, remedy=STATUS_BAR_REMEDY)
    if not args.json:
        for item in batch.items:
            if args.verbose or not item["success"]:
                print(item["message"])
    sys.exit(batch.finish(json_mode=args.json))


if __name__ == "__main__":
    cli.run(main)
