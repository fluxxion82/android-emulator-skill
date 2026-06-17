#!/usr/bin/env python3
"""
Android Clipboard Manager

Copy text to device/emulator clipboard for testing paste flows.
Optimized for minimal token output.

Usage Examples:
    # Copy text to clipboard
    python scripts/clipboard.py --copy "text to copy"

    # Copy with verbose output
    python scripts/clipboard.py --copy "Hello World" --verbose

    # Copy and verify it round-trips back from the device clipboard
    python scripts/clipboard.py --copy "Hello World" --expected "Hello World"

    # Label a copy for test tracking
    python scripts/clipboard.py --copy "OTP 123456" --test-name "paste-otp"
"""

import argparse
import json
import re
import subprocess
import sys

from common.device_utils import build_adb_command
from common.env_config import env_int

# Number of hex words to scan when decoding the clipboard Parcel dump. The
# Parcel contains a length-prefixed UTF-16LE string; very long clipboard
# contents would otherwise be truncated. Tunable via env for unusual payloads.
MAX_PARCEL_WORDS = env_int("ANDROID_EMU_CLIPBOARD_PARCEL_WORDS", 4096, min_value=1)

# One-line guidance printed after a successful copy. Paste is an Android key
# event (KEYCODE_PASTE = 279) sent to the focused text field.
NEXT_STEPS_HINT = (
    "Next: focus a field (navigator.py --find-type EditText --tap), "
    "then paste with: adb shell input keyevent 279"
)


def parse_clipboard_parcel(raw: str) -> str | None:
    """Decode the clipboard text from a ``service call clipboard`` Parcel dump.

    ``adb shell service call clipboard <code>`` prints the marshalled Parcel as
    rows of big-endian 32-bit hex words, e.g.::

        Result: Parcel(
          0x00000000: 00000000 00000005 00650048 006c006c 006f0000 '....H.e.l.l.o...')
        )

    The payload is a length-prefixed UTF-16LE string: one word holding the
    character count, followed by the UTF-16 code units packed two-per-word.
    This is a pure function so it is unit-testable without a device.

    Args:
        raw: Raw stdout from the ``service call clipboard`` invocation.

    Returns:
        The decoded clipboard string, or ``None`` if no text could be parsed
        (empty clipboard or an unrecognised dump).
    """
    # Collect every 8-digit hex word in order of appearance.
    words = re.findall(r"\b([0-9a-fA-F]{8})\b", raw)
    if not words:
        return None

    values = [int(w, 16) for w in words[:MAX_PARCEL_WORDS]]

    # Locate the length word: the first non-zero word is the character count.
    # Leading zero words are the Parcel's null-interface / error header.
    idx = 0
    while idx < len(values) and values[idx] == 0:
        idx += 1
    if idx >= len(values):
        return None

    char_count = values[idx]
    idx += 1
    if char_count <= 0:
        return None

    # Each subsequent 32-bit word packs two little-endian UTF-16 code units:
    # the low 16 bits come first, then the high 16 bits.
    code_units: list[int] = []
    for word in values[idx:]:
        code_units.append(word & 0xFFFF)
        code_units.append((word >> 16) & 0xFFFF)
        if len(code_units) >= char_count:
            break

    code_units = code_units[:char_count]
    if not code_units:
        return None

    try:
        text = "".join(chr(cu) for cu in code_units)
    except ValueError:
        return None

    # Trim any trailing NUL padding that the Parcel may include.
    return text.rstrip("\x00")


class ClipboardManager:
    """Manages clipboard operations on Android device/emulator."""

    def __init__(self, serial: str | None = None):
        """
        Initialize clipboard manager.

        Args:
            serial: Optional device serial (auto-detects if None)
        """
        self.serial = serial

    def copy(self, text: str) -> tuple:
        """
        Copy text to device clipboard.

        Args:
            text: Text to copy to clipboard

        Returns:
            (success, message) tuple
        """
        # Escape special characters for shell
        escaped_text = text.replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")

        # Use input command to set clipboard
        cmd = build_adb_command(
            "shell",
            self.serial,
            "input",
            "text",
            f'"{escaped_text}"',
        )

        # Alternative: use am to broadcast clipboard intent
        # This is more reliable for complex text
        cmd = build_adb_command(
            "shell",
            self.serial,
            "am",
            "broadcast",
            "-a",
            "clipper.set",
            "-e",
            "text",
            f'"{escaped_text}"',
        )

        # Best approach: use service call to ClipboardService
        # This works on all Android versions
        cmd = build_adb_command(
            "shell",
            self.serial,
            "service",
            "call",
            "clipboard",
            "1",
            "i32",
            "0",
            "s16",
            f'"{escaped_text}"',
        )

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, f"Copied to clipboard: {text[:50]}{'...' if len(text) > 50 else ''}"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to copy to clipboard: {error_msg}"

    def paste(self) -> tuple:
        """
        Get text from device clipboard.

        Returns:
            (success, message, text) tuple
        """
        # Get clipboard content using service call
        cmd = build_adb_command(
            "shell",
            self.serial,
            "service",
            "call",
            "clipboard",
            "4",
            "i32",
            "0",
        )

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            # Parse the service call output
            # Format: Result: Parcel(00000000 00000011 00000074 00000065 ...)
            # This is complex to parse, so return raw output
            return True, "Clipboard content retrieved", result.stdout

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to get clipboard: {error_msg}", None

    def read_text(self) -> tuple:
        """
        Read and decode the current clipboard text from the device.

        Wraps :meth:`paste` and decodes the raw Parcel dump into a string.

        Returns:
            (success, message, text) tuple where ``text`` is the decoded
            clipboard contents (or ``None`` if it could not be read/decoded).
        """
        success, message, raw = self.paste()
        if not success:
            return False, message, None

        decoded = parse_clipboard_parcel(raw) if raw else None
        return True, "Clipboard content read", decoded


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manage Android device/emulator clipboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Copy text to clipboard
  python scripts/clipboard.py --copy "Hello, World!"

  # Copy with verbose output
  python scripts/clipboard.py --copy "Test text" --verbose

  # Copy and verify the device clipboard matches what we expect
  python scripts/clipboard.py --copy "Hello, World!" --expected "Hello, World!"

  # Label a copy for test tracking
  python scripts/clipboard.py --copy "OTP 123456" --test-name "paste-otp"
        """,
    )

    parser.add_argument("--copy", help="Copy text to clipboard")
    parser.add_argument(
        "--paste", action="store_true", help="Get text from clipboard (experimental)"
    )
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument("--test-name", dest="test_name", help="Test scenario label for tracking")
    parser.add_argument(
        "--expected",
        help="Read the clipboard back after copying and report whether it matches this value",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    manager = ClipboardManager(serial=args.device_serial)

    # Copy operation
    if args.copy:
        success, message = manager.copy(args.copy)

        # Optionally read the clipboard back to verify the copy round-trips.
        verified: bool | None = None
        actual: str | None = None
        if success and args.expected is not None:
            read_ok, _read_msg, actual = manager.read_text()
            if read_ok:
                verified = actual == args.expected
            # A failed read leaves verified=None (unknown); the copy still
            # succeeded, so we do not flip overall success.

        if args.json:
            payload = {"success": success, "message": message}
            if args.test_name:
                payload["test_name"] = args.test_name
            if args.expected is not None:
                payload["expected"] = args.expected
                payload["actual"] = actual
                payload["match"] = verified
            print(json.dumps(payload, indent=2))
        elif args.verbose or not success:
            print(message)
            if args.test_name:
                print(f"Test: {args.test_name}")
            if args.expected is not None:
                if verified is True:
                    print(f"Match: clipboard == expected ({args.expected!r})")
                elif verified is False:
                    print(f"Mismatch: expected {args.expected!r}, got {actual!r}")
                else:
                    print("Match: unknown (could not read clipboard back)")
            if success:
                print(NEXT_STEPS_HINT)
        elif success:
            # Minimal default output. Surface verification result if requested,
            # otherwise the historical single-character success marker.
            if args.expected is not None:
                if verified is True:
                    print("✓ match")
                elif verified is False:
                    print(f"✗ mismatch (got {actual!r})")
                else:
                    print("✓ (verify unknown)")
            else:
                print("✓")  # Minimal output
            print(NEXT_STEPS_HINT)

        # A requested verification that explicitly mismatched is a failure.
        exit_ok = success and verified is not False
        sys.exit(0 if exit_ok else 1)

    # Paste operation
    if args.paste:
        success, message, content = manager.paste()

        if args.json:
            print(
                json.dumps({"success": success, "message": message, "content": content}, indent=2)
            )
        elif success:
            print(message)
            if args.verbose:
                print(f"Content: {content}")
        else:
            print(message, file=sys.stderr)

        sys.exit(0 if success else 1)

    # No operation specified
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
