#!/usr/bin/env python3
"""
Android Appearance Controller

Control device appearance for consistent testing and screenshots: dark/light
theme (UI night mode), font scale (the Android analog of iOS Dynamic Type),
and — best-effort — system locale.

This is the Android-native counterpart of the iOS Simulator ``appearance.py``.
The *intent* mirrors iOS (theme + text size + locale + reset), but the
mechanics are Android-native: UI night mode is driven via ``cmd uimode night``,
text size via the ``font_scale`` system setting, and locale via the
``persist.sys.locale`` property (see the locale caveat below).

Usage examples:
    # Switch to dark mode
    python scripts/appearance.py --theme dark

    # Larger text (Dynamic Type analog) via friendly alias
    python scripts/appearance.py --text-size large

    # Exact font scale
    python scripts/appearance.py --font-scale 1.3

    # Best-effort locale change (see caveat below)
    python scripts/appearance.py --locale fr-FR

    # Restore defaults (light theme + font_scale 1.0)
    python scripts/appearance.py --reset

Locale caveat:
    Reliably changing the *global* system locale on Android requires privileged
    (root) access or a reboot to fully take effect. This script exposes
    ``--locale`` on a best-effort basis and reports honestly whether the change
    was applied — it does NOT pretend the locale always switches.
"""

import argparse
import json
import sys

from common import adb_exec
from common.device_utils import (
    build_adb_command,
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_float

# Tunable default font scales for friendly text-size aliases (overridable via
# the ANDROID_EMU_ prefix). Kept as module-level tunables so the "small/large"
# rungs can be adjusted for a given test matrix without code edits — mirroring
# the iOS Dynamic Type ladder in spirit.
TEXT_SIZE_SMALL_SCALE = env_float("ANDROID_EMU_TEXT_SIZE_SMALL_SCALE", 0.85)
TEXT_SIZE_DEFAULT_SCALE = env_float("ANDROID_EMU_TEXT_SIZE_DEFAULT_SCALE", 1.0)
TEXT_SIZE_LARGE_SCALE = env_float("ANDROID_EMU_TEXT_SIZE_LARGE_SCALE", 1.15)
TEXT_SIZE_XL_SCALE = env_float("ANDROID_EMU_TEXT_SIZE_XL_SCALE", 1.3)

# Default appearance values used by --reset.
DEFAULT_FONT_SCALE = 1.0


class AppearanceController:
    """Controls Android device appearance: theme, font scale, and locale."""

    def __init__(self, serial: str | None = None):
        """
        Initialize appearance controller.

        Args:
            serial: Optional device serial (auto-detects if None)
        """
        self.serial = serial

    # === PURE LOGIC (device-free, unit-tested) ===

    @property
    def text_size_aliases(self) -> dict[str, float]:
        """Map friendly text-size aliases to font_scale float values.

        This is the Android analog of the iOS Dynamic Type ladder: instead of
        named content-size tokens, Android scales text via a single
        ``font_scale`` multiplier. Built as a property so it always reflects the
        current (possibly env-overridden) module tunables.
        """
        return {
            "small": TEXT_SIZE_SMALL_SCALE,
            "default": TEXT_SIZE_DEFAULT_SCALE,
            "large": TEXT_SIZE_LARGE_SCALE,
            "xl": TEXT_SIZE_XL_SCALE,
        }

    def resolve_font_scale(self, alias: str) -> float | None:
        """Resolve a friendly text-size alias to its font_scale value.

        Args:
            alias: One of small / default / large / xl (case-insensitive)

        Returns:
            The font_scale float, or None if the alias is unknown.
        """
        return self.text_size_aliases.get(alias.lower())

    def build_theme_command(self, theme: str) -> list:
        """Build the adb command that sets UI night (dark) mode.

        Android exposes dark mode through ``cmd uimode night yes|no``. This is
        the Android-native equivalent of ``simctl ui appearance``.

        Args:
            theme: 'dark' or 'light'

        Returns:
            Complete adb command list ready for subprocess.run()
        """
        night = "yes" if theme == "dark" else "no"
        return build_adb_command("shell", self.serial, "cmd", "uimode", "night", night)

    def build_font_scale_command(self, scale: float) -> list:
        """Build the adb command that sets the system font scale.

        Args:
            scale: Font scale multiplier (e.g., 1.0, 1.3)

        Returns:
            Complete adb command list ready for subprocess.run()
        """
        return build_adb_command(
            "shell", self.serial, "settings", "put", "system", "font_scale", str(scale)
        )

    def build_locale_command(self, locale: str) -> list:
        """Build the (best-effort) adb command that sets the system locale.

        Writes ``persist.sys.locale`` via setprop. On non-rooted devices this
        property write is typically rejected or ignored without a reboot; the
        runtime path reports honestly whether it took effect.

        Args:
            locale: BCP-47 locale tag (e.g., 'fr-FR', 'ja-JP')

        Returns:
            Complete adb command list ready for subprocess.run()
        """
        return build_adb_command(
            "shell",
            self.serial,
            "setprop",
            "persist.sys.locale",
            quote_for_device_shell(locale),
        )

    # === DEVICE OPERATIONS ===

    def _run_built(self, cmd: list) -> None:
        """Run an argv produced by one of the ``build_*_command`` helpers.

        Those helpers are the tested source of truth for these commands, so
        rather than restating their arguments here we hand the device-side tail
        (everything after ``shell``) to :func:`run_adb`, which re-adds the
        bounded ``adb [-s SERIAL] shell`` prefix. Every call is therefore
        bounded, and a device-level failure raises rather than being reported
        as "the setting did not apply".

        Args:
            cmd: Complete adb argv from a ``build_*_command`` helper.

        Raises:
            adb_exec.AdbCommandError: the command ran and returned non-zero.
            adb_exec.AdbError: adb never reached a device (surfaces at main()).
        """
        adb_exec.run_adb("shell", self.serial, *cmd[cmd.index("shell") + 1 :], check=True)

    def set_theme(self, theme: str) -> tuple:
        """
        Switch device between light and dark UI night mode.

        Args:
            theme: 'dark' or 'light'

        Returns:
            (success, message) tuple
        """
        cmd = self.build_theme_command(theme)
        try:
            self._run_built(cmd)
            return True, f"Theme set: {theme}"
        except adb_exec.AdbCommandError as e:
            return False, f"Failed to set theme: {e}"

    def set_font_scale(self, scale: float) -> tuple:
        """
        Set the system font scale (Dynamic Type analog).

        Args:
            scale: Font scale multiplier (must be positive)

        Returns:
            (success, message) tuple
        """
        if scale <= 0:
            return False, "Font scale must be greater than 0"

        cmd = self.build_font_scale_command(scale)
        try:
            self._run_built(cmd)
            return True, f"Font scale set: {scale}"
        except adb_exec.AdbCommandError as e:
            return False, f"Failed to set font scale: {e}"

    def set_text_size(self, alias: str) -> tuple:
        """
        Set font scale via a friendly text-size alias.

        Args:
            alias: One of small / default / large / xl

        Returns:
            (success, message) tuple
        """
        scale = self.resolve_font_scale(alias)
        if scale is None:
            valid = ", ".join(self.text_size_aliases.keys())
            return False, f"Unknown text size '{alias}'. Valid: {valid}"

        success, message = self.set_font_scale(scale)
        if success:
            return True, f"Text size set: {alias} (font_scale {scale})"
        return False, message

    def set_locale(self, locale: str) -> tuple:
        """
        Best-effort: set the system locale.

        IMPORTANT: changing the global locale reliably needs privileged/root
        access or a reboot. On a standard (non-rooted) device the underlying
        ``setprop persist.sys.locale`` is commonly rejected or only takes effect
        after a reboot. We attempt it and report honestly — success here means
        the property write returned 0, not that every app re-localized.

        Args:
            locale: BCP-47 locale tag (e.g., 'fr-FR', 'ja-JP', 'de-DE')

        Returns:
            (success, message) tuple
        """
        cmd = self.build_locale_command(locale)
        try:
            self._run_built(cmd)
            return True, (
                f"Locale write attempted: {locale} (best-effort — a reboot may be "
                "required, and unprivileged devices may ignore this)"
            )
        except adb_exec.AdbCommandError as e:
            return False, (
                f"Locale not changed: {e}. Changing the global locale "
                "reliably needs root/privileged access or a reboot."
            )

    def reset(self) -> tuple:
        """
        Reset appearance to defaults: light theme + font_scale 1.0.

        Locale is intentionally NOT reset here — there is no reliable
        unprivileged way to restore it, so we avoid pretending we did.

        Returns:
            (success, message) tuple
        """
        results: list[str] = []
        errors: list[str] = []

        ok, msg = self.set_theme("light")
        (results if ok else errors).append(msg)

        ok, msg = self.set_font_scale(DEFAULT_FONT_SCALE)
        (results if ok else errors).append(msg)

        if errors:
            return False, f"Reset partial — errors: {'; '.join(errors)}"

        return True, f"Appearance reset to defaults (light theme, font_scale {DEFAULT_FONT_SCALE})"


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Control Android device appearance (theme, font scale, locale)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dark / light theme (UI night mode)
  python scripts/appearance.py --theme dark
  python scripts/appearance.py --theme light

  # Text size via friendly alias (Dynamic Type analog)
  python scripts/appearance.py --text-size large

  # Exact font scale
  python scripts/appearance.py --font-scale 1.3

  # Best-effort locale change (see note below)
  python scripts/appearance.py --locale fr-FR

  # Restore defaults (light theme, font_scale 1.0)
  python scripts/appearance.py --reset

Text sizes: small (0.85) default (1.0) large (1.15) xl (1.3)

Locale note: changing the GLOBAL system locale reliably requires privileged/
root access or a reboot. --locale is best-effort and reports honestly whether
the property write succeeded; it does not guarantee apps re-localize.
        """,
    )

    parser.add_argument(
        "--theme",
        choices=["light", "dark"],
        help="Set light or dark UI night mode",
    )
    parser.add_argument(
        "--text-size",
        choices=["small", "default", "large", "xl"],
        help="Set font scale via friendly alias (Dynamic Type analog)",
    )
    parser.add_argument(
        "--font-scale",
        type=float,
        help="Set exact font scale multiplier (e.g., 1.0, 1.3)",
    )
    parser.add_argument(
        "--locale",
        metavar="TAG",
        help="Best-effort BCP-47 locale (e.g., fr-FR); needs root/reboot to be reliable",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset to defaults (light theme, font_scale 1.0)",
    )
    parser.add_argument("--serial", "-s", help="Device serial number (auto-detects if omitted)")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Guard: require at least one action.
    if not any([args.theme, args.text_size, args.font_scale is not None, args.locale, args.reset]):
        parser.print_help()
        sys.exit(1)

    # Guard: --reset is incompatible with explicit appearance flags.
    if args.reset and any([args.theme, args.text_size, args.font_scale is not None, args.locale]):
        print(
            "Error: --reset cannot be combined with --theme, --text-size, "
            "--font-scale, or --locale",
            file=sys.stderr,
        )
        sys.exit(1)

    # Guard: --text-size and --font-scale both set font_scale; reject ambiguity.
    if args.text_size and args.font_scale is not None:
        print("Error: --text-size and --font-scale are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    # Resolve device.
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    controller = AppearanceController(serial=serial)
    results: list[dict] = []

    # CLI boundary: a device-level adb failure (ambiguous target, wrong serial,
    # offline, unauthorized) means the command never ran. Report the remedy the
    # error already carries rather than letting a traceback reach the user.
    try:
        if args.reset:
            success, message = controller.reset()
            results.append({"operation": "reset", "success": success, "message": message})
        else:
            if args.theme:
                success, message = controller.set_theme(args.theme)
                results.append({"operation": "theme", "success": success, "message": message})

            if args.text_size:
                success, message = controller.set_text_size(args.text_size)
                results.append({"operation": "text_size", "success": success, "message": message})

            if args.font_scale is not None:
                success, message = controller.set_font_scale(args.font_scale)
                results.append({"operation": "font_scale", "success": success, "message": message})

            if args.locale:
                success, message = controller.set_locale(args.locale)
                results.append({"operation": "locale", "success": success, "message": message})
    except adb_exec.AdbError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"results": results}, indent=2))
    elif args.verbose:
        device_label = serial if serial else "default device"
        print(f"Device: {device_label}")
        for result in results:
            status = "OK" if result["success"] else "FAIL"
            print(f"  [{status}] {result['operation']}: {result['message']}")
    else:
        for result in results:
            print(result["message"])

    all_success = all(r["success"] for r in results)
    sys.exit(0 if all_success else 1)


if __name__ == "__main__":
    main()
