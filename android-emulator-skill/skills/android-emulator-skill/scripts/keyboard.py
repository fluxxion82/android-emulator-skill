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

    # Hide keyboard
    python scripts/keyboard.py --hide-keyboard

Output Format:
    Typed: "Hello World"
    Pressed: KEYCODE_ENTER
    Button: back
"""

import argparse
import subprocess
import sys
import time

from common.device_utils import build_adb_command, resolve_device_identifier
from common.env_config import env_float, env_int

# Tunable defaults (override via ANDROID_EMU_* env vars).
DEFAULT_TYPE_DELAY = env_float("ANDROID_EMU_KEYBOARD_TYPE_DELAY", 0.0)
DEFAULT_KEY_COUNT = env_int("ANDROID_EMU_KEYBOARD_KEY_COUNT", 1)
# Small pause between repeated keyevents so the IME/app can register each one.
KEY_REPEAT_DELAY = env_float("ANDROID_EMU_KEYBOARD_KEY_REPEAT_DELAY", 0.05)


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
    }

    def __init__(self, serial: str | None = None):
        """Initialize keyboard simulator."""
        self.serial = serial

    @staticmethod
    def _escape_text(text: str) -> str:
        """Escape text for `adb shell input text` (space -> %s, etc.)."""
        return (
            text.replace("\\", "\\\\")
            .replace(" ", "%s")
            .replace('"', '\\"')
            .replace("'", "\\'")
            .replace("$", "\\$")
            .replace("`", "\\`")
        )

    def _input_text(self, text: str) -> tuple:
        """Send a single `input text` chunk; returns (success, message)."""
        try:
            cmd = build_adb_command("shell", self.serial, "input", "text", self._escape_text(text))
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, ""
        except subprocess.CalledProcessError as e:
            return False, f"Type failed: {e.stderr}"

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
            cmd = build_adb_command("shell", self.serial, "input", "keyevent", keycode)
            for i in range(repeats):
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                if i < repeats - 1 and KEY_REPEAT_DELAY > 0:
                    time.sleep(KEY_REPEAT_DELAY)
        except subprocess.CalledProcessError as e:
            return False, f"Key press failed: {e.stderr}"

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

    def show_keyboard(self) -> tuple:
        """
        Show soft keyboard.

        Returns:
            (success, message) tuple
        """
        try:
            # Toggle IME visibility - show
            cmd = build_adb_command(
                "shell",
                self.serial,
                "am",
                "broadcast",
                "-a",
                "android.intent.action.INPUT_METHOD_CHANGED",
            )
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, "Keyboard shown"
        except subprocess.CalledProcessError as e:
            return False, f"Show keyboard failed: {e.stderr}"

    def hide_keyboard(self) -> tuple:
        """
        Hide soft keyboard.

        Returns:
            (success, message) tuple
        """
        try:
            # Press back to hide keyboard
            cmd = build_adb_command("shell", self.serial, "input", "keyevent", "KEYCODE_BACK")
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, "Keyboard hidden"
        except subprocess.CalledProcessError as e:
            return False, f"Hide keyboard failed: {e.stderr}"

    def dismiss_keyboard(self) -> tuple:
        """
        Dismiss the soft keyboard by pressing the BACK key.

        On Android, BACK closes an open IME without leaving the current screen,
        which is the native equivalent of dismissing the keyboard.

        Returns:
            (success, message) tuple
        """
        success, message = self.press_key("back")
        if not success:
            return False, f"Dismiss keyboard failed: {message}"
        return True, "Dismissed keyboard"

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
    parser.add_argument("--show-keyboard", action="store_true", help="Show soft keyboard")
    parser.add_argument("--hide-keyboard", action="store_true", help="Hide soft keyboard")
    parser.add_argument("--dismiss", action="store_true", help="Dismiss the keyboard (press BACK)")
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
    elif args.show_keyboard:
        success, message = keyboard.show_keyboard()
    elif args.hide_keyboard:
        success, message = keyboard.hide_keyboard()
    elif args.dismiss:
        success, message = keyboard.dismiss_keyboard()
    else:
        parser.print_help()
        sys.exit(1)

    if args.json:
        import json

        print(json.dumps({"success": success, "message": message}, indent=2))
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
