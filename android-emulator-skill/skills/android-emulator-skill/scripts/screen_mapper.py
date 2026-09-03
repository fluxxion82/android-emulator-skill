#!/usr/bin/env python3
"""
Android Screen Mapper - Current Screen Analyzer

Maps the current screen's UI elements for navigation decisions.
Provides token-efficient summaries of available interactions.

This script analyzes the Android screen using uiautomator dump
and provides a compact, actionable summary of what's currently visible and
interactive on the screen. Perfect for AI agents making navigation decisions.

Key Features:
- Token-efficient output (5-7 lines by default)
- Identifies buttons, text fields, navigation elements
- Counts interactive and focusable elements
- Progressive detail with --verbose flag
- Navigation hints with --hints flag

Usage Examples:
    # Quick summary (default)
    python scripts/screen_mapper.py

    # Specific device
    python scripts/screen_mapper.py --serial emulator-5554

    # Detailed element breakdown
    python scripts/screen_mapper.py --verbose

    # Include navigation suggestions
    python scripts/screen_mapper.py --hints

    # Full JSON output for parsing
    python scripts/screen_mapper.py --json

Output Format (default):
    Screen: com.example.app/.MainActivity (45 elements, 7 interactive)
    Button: "Login", "Cancel", "Forgot Password"
    Control: "Remember me", "Dark theme"
    EditText: "Email address"
    EditTexts: 2 (0 filled) [1 secure]
    Focusable: 7 elements

    Every interactive element is NAMED, not just counted, and the names are the
    ones `navigator.py --find-text` accepts -- the default report is what Quick
    Start step 2 produces and step 3 consumes. Counting seven controls without
    naming them left the agent with nothing to hand to the next command (C4).

Technical Details:
- Uses uiautomator dump via `adb shell uiautomator dump`
- Parses XML hierarchy with accessibility attributes
- Identifies element types: Button, EditText, TextView, ImageView, etc.
- Extracts labels from content-desc, text, and resource-id attributes
- Reports secure/password inputs (password="true") separately

Configuration (env overrides, ANDROID_EMU_ prefix):
- ANDROID_EMU_SCREEN_BUTTONS_PREVIEW (default 15): button labels on summary line
- ANDROID_EMU_SCREEN_SECTION_ITEMS (default 10): items per type in --verbose
"""

import argparse
import json as json_lib
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

from common import adb_exec
from common.device_utils import get_current_activity, resolve_device_identifier
from common.env_config import env_int
from common.hierarchy import INTERACTIVE_ATTRIBUTES as _INTERACTIVE_ATTRIBUTES
from common.hierarchy import capture_hierarchy, is_interactive, parse_bounds

# Preview limits (env-configurable; see SKILL.md -> Configuration).
# BUTTONS_PREVIEW caps how many button labels render on the summary line.
# SECTION_ITEMS_PREVIEW caps how many items render per type in --verbose mode.
BUTTONS_PREVIEW = env_int("ANDROID_EMU_SCREEN_BUTTONS_PREVIEW", 15)
SECTION_ITEMS_PREVIEW = env_int("ANDROID_EMU_SCREEN_SECTION_ITEMS", 10)


class ScreenMapper:
    """
    Analyzes current screen for navigation decisions.

    This class fetches the Android UI hierarchy from uiautomator and analyzes it
    to provide actionable summaries for navigation. It categorizes elements
    by type, counts interactive elements, and identifies key UI patterns.

    Attributes:
        serial: Device serial number, or None for default device
        INTERACTIVE_TYPES: Element types that users can interact with

    Design Philosophy:
        - Token efficiency: Provide minimal but complete information
        - Progressive disclosure: Summary by default, details on request
        - Navigation-focused: Highlight elements relevant for automation
    """

    # Interactivity is decided by an element's PROPERTIES, not its class name,
    # and the rule itself lives in `common.hierarchy.is_interactive` so that
    # navigator, accessibility_audit and this file cannot answer the same
    # question differently (C7). Re-exported here because the class-level name
    # is what the older comments and tests refer to.
    #
    # Measured on a recorded Compose dump: of seven controls, five report class
    # `android.view.View`; a Switch produces no `android.widget.Switch` at all.
    # Only Checkbox, TextField and Image map to widget classes -- which is why a
    # whitelist of View class names reported ~0 interactive elements (R11).
    INTERACTIVE_ATTRIBUTES = _INTERACTIVE_ATTRIBUTES

    # Class names still used for *labelling* an element, never for eligibility.
    KNOWN_WIDGET_CLASSES = {
        "Button",
        "ImageButton",
        "EditText",
        "CheckBox",
        "RadioButton",
        "Switch",
        "ToggleButton",
        "SeekBar",
        "Spinner",
    }

    # Cap on text gathered from a control's subtree, so a long list does not
    # turn one entry into a wall of text.
    MAX_RECOVERED_LABEL_PARTS = 3

    def __init__(self, serial: str | None = None):
        """
        Initialize screen mapper.

        Args:
            serial: Optional device serial. If None, uses default device.

        Example:
            mapper = ScreenMapper(serial="emulator-5554")
            mapper = ScreenMapper()  # Uses default device
        """
        self.serial = serial

    def get_ui_hierarchy(self) -> ET.Element:
        """
        Fetch the current UI hierarchy.

        Delegates to :func:`common.hierarchy.capture_hierarchy`, which captures
        via ``adb exec-out`` and so writes no file on either the device or the
        host -- this used to pull to a fixed ``/tmp`` path shared with navigator
        and device_utils, where concurrent runs read each other's screen.

        Returns:
            XML root element.

        Raises:
            RuntimeError: If the hierarchy could not be captured.
        """
        return capture_hierarchy(self.serial)

    def analyze_tree(self, root: ET.Element) -> dict:
        """
        Analyze UI hierarchy for navigation info.

        Args:
            root: XML root element from uiautomator dump

        Returns:
            Analysis dict with element counts and summaries
        """
        analysis = {
            "elements_by_type": defaultdict(list),
            "total_elements": 0,
            "interactive_elements": 0,
            "edit_texts": [],
            "buttons": [],
            "text_views": [],
            "screen_name": None,
            "focusable": 0,
            "secure_fields": 0,
        }

        self._analyze_recursive(root, analysis, parent=None)

        # Post-process for clean output
        analysis["elements_by_type"] = dict(analysis["elements_by_type"])

        # Try to determine screen name from activity
        self._detect_screen_name(analysis)

        return analysis

    def _analyze_recursive(
        self, node: ET.Element, analysis: dict, parent: ET.Element | None = None
    ):
        """
        Recursively analyze XML nodes.

        Args:
            node: Current XML element
            analysis: Analysis dict to populate
            parent: The node's parent, needed to recover a label from a
                row-adjacent sibling (a Compose Checkbox or Switch carries no
                text of its own; its caption is the next sibling).
        """
        # Get element attributes
        elem_class = node.get("class", "")
        text = node.get("text", "")
        content_desc = node.get("content-desc", "")
        resource_id = node.get("resource-id", "")
        focusable = node.get("focusable", "false") == "true"
        enabled = node.get("enabled", "true") == "true"
        # Android marks secure/password inputs with password="true".
        is_secure = node.get("password", "false") == "true"

        # Extract simple class name (e.g., "android.widget.Button" -> "Button")
        simple_class = elem_class.split(".")[-1] if elem_class else "Unknown"

        # Count element
        if elem_class:
            analysis["total_elements"] += 1

            # Determine label for this element
            label = text or content_desc or resource_id or None

            # Track interactive elements. The rule is `hierarchy.is_interactive`
            # and nothing local: enabled, an uncollapsed rectangle, and at least
            # one interaction property.
            if is_interactive(node):
                analysis["interactive_elements"] += 1

                # Compose controls carry no text of their own; recover it.
                recovered = label or self._recover_label(node, parent)
                bucket = simple_class if simple_class in self.KNOWN_WIDGET_CLASSES else "Control"
                if recovered:
                    analysis["elements_by_type"][bucket].append(recovered)

                if simple_class in ("Button", "ImageButton") and recovered:
                    analysis["buttons"].append(recovered)
                elif simple_class == "EditText":
                    analysis["edit_texts"].append(
                        {
                            "label": recovered or content_desc or resource_id or "Unnamed",
                            "filled": bool(text),
                            "secure": is_secure,
                        }
                    )
                elif simple_class == "TextView" and recovered:
                    analysis["text_views"].append(recovered)

            elif label and simple_class == "TextView":
                # Passive labels still describe the screen, so keep listing them.
                analysis["elements_by_type"]["TextView"].append(label)

            # Count focusable
            if focusable and enabled:
                analysis["focusable"] += 1

            # Count secure/password fields separately (security-relevant inputs).
            if is_secure:
                analysis["secure_fields"] += 1

        # Recurse to children
        for child in node:
            self._analyze_recursive(child, analysis, parent=node)

    @staticmethod
    def _own_label(node: ET.Element) -> str:
        return (node.get("text") or "").strip() or (node.get("content-desc") or "").strip()

    def _recover_label(self, node: ET.Element, parent: ET.Element | None) -> str | None:
        """Find text describing a control that carries none of its own.

        Compose controls are unlabelled: measured on a real dump, all seven
        interactive nodes had empty ``text`` and ``content-desc``. Their captions
        sit in two different places, so both are searched:

        - **Descendants** -- a Button's "Submit Order", a Card's item lines, a
          list's rows. uiautomator dumps the *unmerged* semantics tree, so
          ``mergeDescendants`` does not fold these into the parent and there is
          no concatenated label to read.
        - **A row-adjacent sibling** -- a Checkbox's "Remember me" and a Switch's
          "Dark theme" are siblings, not children. Only an immediate sibling
          whose bounds overlap vertically is used; scanning all siblings would
          pull in the whole screen, since these controls share one flat parent.
        """
        parts = [self._own_label(child) for child in node.iter() if child is not node]
        parts = [p for p in parts if p]
        if parts:
            return " ".join(parts[: self.MAX_RECOVERED_LABEL_PARTS])

        if parent is None:
            return None

        siblings = list(parent)
        try:
            position = siblings.index(node)
        except ValueError:
            return None

        own = parse_bounds(node.get("bounds"))
        for offset in (1, -1):
            index = position + offset
            if not 0 <= index < len(siblings):
                continue
            sibling = siblings[index]
            if own is not None:
                box = parse_bounds(sibling.get("bounds"))
                # Same row: vertical spans must overlap.
                if box is not None and not (box[1] < own[3] and own[1] < box[3]):
                    continue
            candidate = self._own_label(sibling) or next(
                (self._own_label(c) for c in sibling.iter() if self._own_label(c)), ""
            )
            if candidate:
                return candidate
        return None

    def _detect_screen_name(self, analysis: dict):
        """Name the screen from the device's focused activity.

        Asked through :func:`common.device_utils.get_current_activity`, which is
        the one place that runs ``dumpsys window`` and parses its focus lines
        against recorded output. This file used to carry a second parser with a
        different grammar -- it matched ``([A-Za-z0-9_]+Activity)``, so it
        reported a bare ``NexusLauncherActivity`` where the shared parser
        reports the full ``package/activity`` component, and it found nothing at
        all for an activity whose class name does not end in "Activity" (C9).

        Args:
            analysis: Analysis dict to update.
        """
        try:
            analysis["screen_name"] = get_current_activity(self.serial)
        except Exception:
            # A screen with no name is still a screen worth reporting, so this
            # never fails the map -- the header falls back to "Unknown Screen".
            analysis["screen_name"] = None

    @staticmethod
    def interactive_names(analysis: dict) -> list[tuple[str, list[str]]]:
        """The control names the report offers, per bucket, in reporting order.

        Every bucket except the passive ``TextView`` one holds controls an agent
        can operate; each is capped at ``BUTTONS_PREVIEW`` so one crowded screen
        cannot turn the summary into a wall. Buckets are alphabetical, which is
        also the order ``--json`` yields, so the two reports agree.

        Args:
            analysis: Analysis dict from :meth:`analyze_tree`.

        Returns:
            ``(bucket, names)`` pairs, buckets with no names omitted.
        """
        return [
            (bucket, labels[:BUTTONS_PREVIEW])
            for bucket, labels in sorted(analysis["elements_by_type"].items())
            if bucket != "TextView" and labels
        ]

    def format_summary(self, analysis: dict, verbose: bool = False, hints: bool = False) -> str:
        """
        Format analysis as human-readable summary.

        Args:
            analysis: Analysis dict from analyze_tree()
            verbose: Include detailed element listings
            hints: Include navigation suggestions

        Returns:
            Formatted summary string
        """
        lines = []

        # Header line
        screen = analysis.get("screen_name") or "Unknown Screen"
        total = analysis["total_elements"]
        interactive = analysis["interactive_elements"]
        lines.append(f"Screen: {screen} ({total} elements, {interactive} interactive)")

        # Every interactive control, BY NAME.
        #
        # This is C4. The default report used to name only the `Button` and
        # clickable-`TextView` buckets, and on a Compose screen neither exists:
        # every control lands in `Control`, which was printed under --verbose or
        # --json and nowhere else. Quick Start step 2 runs this command with
        # neither flag and step 3 asks the agent to feed a name from it into
        # `navigator --find-text`, so the documented path handed the agent a
        # count and no names.
        for bucket, names in self.interactive_names(analysis):
            available = len(analysis["elements_by_type"][bucket])
            rendered = '", "'.join(names)
            suffix = f", ... ({available} total)" if available > len(names) else ""
            lines.append(f'{bucket}: "{rendered}"{suffix}')

        # EditTexts: the names are on the bucket line above; this line carries
        # what a name cannot say -- how many are filled, and how many are secure.
        if analysis["edit_texts"]:
            filled_count = sum(1 for et in analysis["edit_texts"] if et["filled"])
            edit_line = f"EditTexts: {len(analysis['edit_texts'])} ({filled_count} filled)"
            secure_count = analysis.get("secure_fields", 0)
            if secure_count:
                edit_line += f" [{secure_count} secure]"
            lines.append(edit_line)

        # Focusable count
        focusable = analysis["focusable"]
        lines.append(f"Focusable: {focusable} elements")

        # Verbose mode: Show all element types
        if verbose:
            lines.append("\n--- Detailed Element Breakdown ---")
            for elem_type, elements in sorted(analysis["elements_by_type"].items()):
                lines.append(f"\n{elem_type} ({len(elements)}):")
                for i, elem in enumerate(elements[:SECTION_ITEMS_PREVIEW]):
                    lines.append(f"  {i+1}. {elem}")
                if len(elements) > SECTION_ITEMS_PREVIEW:
                    lines.append(f"  ... and {len(elements) - SECTION_ITEMS_PREVIEW} more")

        # Hints mode: the next command, ready to run.
        #
        # Seeded from the same named buckets as the summary, so a hint can only
        # ever name a control the report just printed -- and it is printed as
        # the navigator invocation Quick Start step 3 documents, rather than as
        # a noun phrase the agent has to translate.
        if hints:
            lines.append("\n--- Navigation Hints ---")
            for _bucket, names in self.interactive_names(analysis)[:3]:
                lines.append(f'• navigator.py --find-text "{names[0]}" --tap')
            unfilled = [et for et in analysis["edit_texts"] if not et["filled"]]
            if unfilled:
                lines.append(
                    f'• navigator.py --find-text "{unfilled[0]["label"]}" '
                    f'--enter-text "your text"'
                )

        return "\n".join(lines)

    def map_screen(
        self, verbose: bool = False, hints: bool = False, json_output: bool = False
    ) -> tuple[str, bool]:
        """
        Main entry point: Map current screen and return formatted output.

        Args:
            verbose: Include detailed element listings
            hints: Include navigation suggestions
            json_output: Return JSON instead of formatted text

        Returns:
            ``(output, ok)``. The output format is unchanged; ``ok`` is False
            when the screen could not be read, so ``main()`` can exit non-zero
            instead of reporting success while serialising an error (R2).
        """
        try:
            root = self.get_ui_hierarchy()
            analysis = self.analyze_tree(root)

            if json_output:
                return json_lib.dumps(analysis, indent=2), True
            return self.format_summary(analysis, verbose, hints), True

        except RuntimeError as e:
            # adb_exec's device errors subclass RuntimeError, so "more than one
            # device" arrives here already carrying its remedy.
            if json_output:
                return json_lib.dumps({"error": str(e)}, indent=2), False
            return f"Error: {e}", False


def main():
    parser = argparse.ArgumentParser(
        description="Analyze current Android screen for navigation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick summary (default)
  python screen_mapper.py

  # Specific device
  python screen_mapper.py --serial emulator-5554

  # Detailed breakdown
  python screen_mapper.py --verbose

  # With navigation hints
  python screen_mapper.py --hints

  # JSON output
  python screen_mapper.py --json
        """,
    )

    parser.add_argument("--serial", "-s", help="Device serial number (auto-detects if omitted)")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed element breakdown"
    )
    parser.add_argument("--hints", action="store_true", help="Include navigation suggestions")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    args = parser.parse_args()

    # Resolve device
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Map screen
    mapper = ScreenMapper(serial)
    try:
        output, ok = mapper.map_screen(
            verbose=args.verbose, hints=args.hints, json_output=args.json
        )
    except adb_exec.AdbError as error:
        # Anything that escaped map_screen's own handling; the message names a
        # remedy, so print it rather than letting a traceback bury it.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    print(output)
    # R2: exiting 0 after serialising an error made the status code useless to
    # a caller that only checks it.
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
