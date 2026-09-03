#!/usr/bin/env python3
"""
Android Keyboard Simulator

Text input and hardware button control for Android devices/emulators.

Key Features:
- Type text (with proper escaping)
- Special keys (enter, delete, tab, space)
- Hardware buttons (home, back, recent apps)
- Key combinations
- Clear text fields
- Show/hide soft keyboard

Usage Examples:
    # Type text
    python scripts/keyboard.py --type "Hello World"

    # Press Enter key
    python scripts/keyboard.py --key enter

    # Press Back button
    python scripts/keyboard.py --button back

    # Clear text (5 deletes)
    python scripts/keyboard.py --clear 5

    # Hide keyboard (checks `dumpsys input_method` first; BACK would otherwise
    # leave the screen when no keyboard is up)
    python scripts/keyboard.py --hide-keyboard

Output Format:
    Typed: "Hello World"
    Pressed: KEYCODE_ENTER
    Button: back
"""

import argparse
import re
import sys
import time

from common import adb_exec
from common.device_utils import quote_for_device_shell, resolve_device_identifier
from common.env_config import env_float, env_int

# Tunable defaults (override via ANDROID_EMU_* env vars).
DEFAULT_TYPE_DELAY = env_float("ANDROID_EMU_KEYBOARD_TYPE_DELAY", 0.0)
DEFAULT_KEY_COUNT = env_int("ANDROID_EMU_KEYBOARD_KEY_COUNT", 1)
# Small pause between repeated keyevents so the IME/app can register each one.
KEY_REPEAT_DELAY = env_float("ANDROID_EMU_KEYBOARD_KEY_REPEAT_DELAY", 0.05)

# The field in `dumpsys input_method` that says whether the IME is on screen.
# InputMethodManagerService prints it as `mInputShown=true` / `mInputShown=false`.
IME_SHOWN_FIELD = "mInputShown"

_IME_SHOWN_RE = re.compile(rf"\b{IME_SHOWN_FIELD}\s*=\s*(true|false)\b", re.IGNORECASE)


def parse_ime_shown(dumpsys_output: str) -> bool | None:
    """Whether an IME is currently shown, from ``dumpsys input_method``.

    Args:
        dumpsys_output: Raw output of ``adb shell dumpsys input_method``.

    Returns:
        True or False from the ``mInputShown`` field, or None when the field is
        absent -- which is not the same as False, and callers must not treat it
        as False: "there is no keyboard up" and "I could not tell" lead to
        different actions.

    Note:
        The corpus has no ``dumpsys input_method`` recording yet (Inc 0's
        recording PR captures ``dumpsys_input_method_shown``). The field name is
        the one the platform's InputMethodManagerService has printed for years,
        and when the recording lands this function is what it should be pointed
        at -- the grammar lives here, in one place, for that reason.
    """
    match = _IME_SHOWN_RE.search(dumpsys_output)
    if match is None:
        return None
    return match.group(1).lower() == "true"


class KeyboardSimulator:
    """Simulates keyboard input and hardware buttons on Android."""

    # Android keycodes
    # https://developer.android.com/reference/android/view/KeyEvent
    KEY_CODES = {
        "enter": "KEYCODE_ENTER",
        "return": "KEYCODE_ENTER",
        "delete": "KEYCODE_DEL",
        "backspace": "KEYCODE_DEL",
        "tab": "KEYCODE_TAB",
        "space": "KEYCODE_SPACE",
        "escape": "KEYCODE_ESCAPE",
        "esc": "KEYCODE_ESCAPE",
        "up": "KEYCODE_DPAD_UP",
        "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT",
        "home": "KEYCODE_HOME",
        "back": "KEYCODE_BACK",
        "menu": "KEYCODE_MENU",
        "search": "KEYCODE_SEARCH",
        "volume_up": "KEYCODE_VOLUME_UP",
        "volume_down": "KEYCODE_VOLUME_DOWN",
        "power": "KEYCODE_POWER",
        "camera": "KEYCODE_CAMERA",
        # Documented in press_button() but previously unmapped, so the call
        # always returned "Unknown key".
        "recent_apps": "KEYCODE_APP_SWITCH",
        "app_switch": "KEYCODE_APP_SWITCH",
    }

    def __init__(self, serial: str | None = None):
        """Initialize keyboard simulator."""
        self.serial = serial

    @staticmethod
    def _escape_text(text: str) -> str:
        """Prepare text for ``adb shell input text``.

        Two separate concerns, previously conflated into one hand-rolled escape
        that missed ``& ; | < > ( ) *`` and newline:

        1. ``input text`` treats ``%s`` as a space, so spaces are encoded that
           way rather than relying on argv splitting.
        2. The argument still crosses the *device* shell, which re-parses it --
           so the result is quoted. Without this, ``x;id`` ran ``id`` on the
           device.
        """
        return quote_for_device_shell(text.replace(" ", "%s"))

    def _input_text(self, text: str) -> tuple:
        """Send a single `input text` chunk; returns (success, message).

        Device-level failures (no device, wrong serial, offline) are *not*
        caught here: they mean the keystroke never reached a device at all, and
        their message names the remedy. ``main()`` reports them.
        """
        try:
            adb_exec.run_adb(
                "shell", self.serial, "input", "text", self._escape_text(text), check=True
            )
            return True, ""
        except adb_exec.AdbCommandError as e:
            return False, f"Type failed: {e}"

    def type_text(self, text: str, delay: float = 0.0) -> tuple:
        """
        Type text at current cursor position.

        Args:
            text: Text to type
            delay: Seconds to pause between characters for slow typing. When 0
                (default) the whole string is sent in a single `input text`.

        Returns:
            (success, message) tuple
        """
        if delay > 0:
            # Type character by character with a delay (useful for fields that
            # debounce input or trigger per-keystroke animations).
            for char in text:
                success, message = self._input_text(char)
                if not success:
                    return False, message
                time.sleep(delay)
            return True, f'Typed: "{text}" (slowly, {delay}s/char)'

        success, message = self._input_text(text)
        if not success:
            return False, message
        return True, f'Typed: "{text}"'

    def press_key(self, key: str, count: int = 1) -> tuple:
        """
        Press a special key.

        Args:
            key: Key name (enter, delete, tab, etc.)
            count: Number of times to press the key (defaults to 1).

        Returns:
            (success, message) tuple
        """
        key_lower = key.lower()
        if key_lower not in self.KEY_CODES:
            available = ", ".join(sorted(self.KEY_CODES.keys()))
            return False, f"Unknown key: {key}. Available: {available}"

        keycode = self.KEY_CODES[key_lower]
        repeats = max(count, 1)

        try:
            for i in range(repeats):
                adb_exec.run_adb("shell", self.serial, "input", "keyevent", keycode, check=True)
                if i < repeats - 1 and KEY_REPEAT_DELAY > 0:
                    time.sleep(KEY_REPEAT_DELAY)
        except adb_exec.AdbCommandError as e:
            return False, f"Key press failed: {e}"

        if repeats > 1:
            return True, f"Pressed: {keycode} ({repeats}x)"
        return True, f"Pressed: {keycode}"

    def press_button(self, button: str) -> tuple:
        """
        Press a hardware button.

        Args:
            button: Button name (home, back, recent_apps, etc.)

        Returns:
            (success, message) tuple
        """
        # Buttons are just special keys
        return self.press_key(button)

    def clear_text(self, count: int = 10) -> tuple:
        """
        Clear text by pressing delete multiple times.

        Args:
            count: Number of deletes to press

        Returns:
            (success, message) tuple
        """
        for i in range(count):
            success, message = self.press_key("delete")
            if not success:
                return False, f"Clear failed on delete {i+1}: {message}"

        return True, f"Cleared: {count} characters"

    def keyboard_is_shown(self) -> bool | None:
        """
        Whether the soft keyboard is currently up, per ``dumpsys input_method``.

        Returns:
            True, False, or None when the service did not report the field at
            all -- "I could not tell", which is not "no".

        Raises:
            AdbError: The device could not be reached.
        """
        result = adb_exec.run_adb("shell", self.serial, "dumpsys", "input_method", check=True)
        return parse_ime_shown(result.stdout)

    def hide_keyboard(self) -> tuple:
        """
        Hide the soft keyboard, pressing BACK only if one is actually up.

        BACK is not a "hide keyboard" key. It closes an open IME, and with no
        IME open it is the *navigation* Back: it pops the activity. So the old
        unconditional press navigated the caller off the screen it was working
        on and reported "Keyboard hidden" (C8) -- a wrong answer that also
        moved the device, so the next step failed somewhere else entirely.

        Returns:
            (success, message) tuple. Reporting "no keyboard shown" is a
            success: the caller asked for the keyboard to be away, and it is.
        """
        try:
            shown = self.keyboard_is_shown()
        except adb_exec.AdbCommandError as e:
            return False, (
                f"Could not read the IME state from `dumpsys input_method`: {e}. "
                f"BACK was not pressed, because with no keyboard up it leaves "
                f"the current screen; press it deliberately with "
                f"`keyboard.py --button back` if that is what you want."
            )

        if shown is None:
            return False, (
                f"Could not tell whether a keyboard is shown: `dumpsys "
                f"input_method` reported no {IME_SHOWN_FIELD} field. BACK was "
                f"not pressed, because with no keyboard up it leaves the "
                f"current screen; press it deliberately with "
                f"`keyboard.py --button back` if that is what you want."
            )
        if not shown:
            return True, "No keyboard shown; nothing to hide"

        try:
            adb_exec.run_adb("shell", self.serial, "input", "keyevent", "KEYCODE_BACK", check=True)
            return True, "Keyboard hidden"
        except adb_exec.AdbCommandError as e:
            return False, f"Hide keyboard failed: {e}"

    def dismiss_keyboard(self) -> tuple:
        """
        Dismiss the soft keyboard.

        The same operation as :meth:`hide_keyboard`, under the name the CLI's
        ``--dismiss`` uses, and gated the same way -- two spellings of one
        action must not disagree about whether they check first.

        Returns:
            (success, message) tuple
        """
        success, message = self.hide_keyboard()
        if not success:
            return False, message
        return True, message.replace("Keyboard hidden", "Dismissed keyboard")

    def key_combination(self, keys: list) -> tuple:
        """
        Press multiple keys in sequence.

        Args:
            keys: List of key names

        Returns:
            (success, message) tuple
        """
        for key in keys:
            success, message = self.press_key(key)
            if not success:
                return False, f"Key combination failed: {message}"

        return True, f"Pressed keys: {', '.join(keys)}"


def main():
    parser = argparse.ArgumentParser(
        description="Android keyboard and button simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Type text
  python keyboard.py --type "Hello World"

  # Type slowly (0.1s between characters)
  python keyboard.py --type "slow typing" --delay 0.1

  # Press special key
  python keyboard.py --key enter
  python keyboard.py --key delete
  python keyboard.py --key tab

  # Press delete 5 times
  python keyboard.py --key delete --count 5

  # Press hardware button
  python keyboard.py --button back
  python keyboard.py --button home

  # Clear text field
  python keyboard.py --clear 10

  # Hide / dismiss keyboard
  python keyboard.py --hide-keyboard
  python keyboard.py --dismiss

  # Key combination
  python keyboard.py --keys enter,back

Available Keys:
  Text: enter/return, delete/backspace, tab, space, escape/esc
  Navigation: up, down, left, right
  Hardware: home, back, menu, search
  Media: volume_up, volume_down, power, camera
        """,
    )

    parser.add_argument("--serial", "-s", help="Device serial number (auto-detects if omitted)")
    parser.add_argument("--type", help="Type text")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_TYPE_DELAY,
        metavar="SECONDS",
        help="Seconds to pause between characters when typing (slow typing)",
    )
    parser.add_argument("--key", help="Press special key")
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_KEY_COUNT,
        metavar="N",
        help="Repeat the --key keyevent N times (default 1)",
    )
    parser.add_argument("--button", help="Press hardware button")
    parser.add_argument("--keys", help="Press multiple keys (comma-separated)")
    parser.add_argument("--clear", type=int, metavar="COUNT", help="Clear text (delete N times)")
    # Documented in the module docstring and the epilog, and dispatched below,
    # but never declared -- so any invocation that fell through to it (notably
    # --dismiss) died with AttributeError instead of running.
    parser.add_argument(
        "--hide-keyboard",
        action="store_true",
        help="Hide the soft keyboard (presses BACK only if an IME is shown)",
    )
    parser.add_argument(
        "--dismiss",
        action="store_true",
        help="Dismiss the keyboard (presses BACK only if an IME is shown)",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    args = parser.parse_args()

    # Resolve device
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    keyboard = KeyboardSimulator(serial)

    # Execute action
    success = False
    message = ""

    try:
        if args.type:
            success, message = keyboard.type_text(args.type, args.delay)
        elif args.key:
            success, message = keyboard.press_key(args.key, args.count)
        elif args.button:
            success, message = keyboard.press_button(args.button)
        elif args.keys:
            keys = [k.strip() for k in args.keys.split(",")]
            success, message = keyboard.key_combination(keys)
        elif args.clear is not None:
            success, message = keyboard.clear_text(args.clear)
        elif args.hide_keyboard:
            success, message = keyboard.hide_keyboard()
        elif args.dismiss:
            success, message = keyboard.dismiss_keyboard()
        else:
            parser.print_help()
            sys.exit(1)
    except adb_exec.AdbError as error:
        # The command never reached a device. The error names the remedy, so
        # print it rather than letting a traceback bury it.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        import json

        print(json.dumps({"success": success, "message": message}, indent=2))
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
