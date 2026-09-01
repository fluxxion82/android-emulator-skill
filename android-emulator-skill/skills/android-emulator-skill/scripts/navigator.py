#!/usr/bin/env python3
"""
Android Navigator - Smart Element Finder and Interactor

Finds and interacts with UI elements using accessibility data.
Prioritizes structured navigation over pixel-based interaction.

This script is the core automation tool for Android navigation. It finds
UI elements by text, type, or resource ID and performs actions on them
(tap, enter text). Uses semantic element finding instead of fragile pixel coordinates.

Key Features:
- Find elements by text (fuzzy or exact matching)
- Find elements by type (Button, EditText, etc.)
- Find elements by resource ID
- Optional scroll-into-view search for elements below the fold
- Tap elements at their center point
- Enter text into text fields
- List all interactive elements on screen
- Automatic element caching for performance

Usage Examples:
    # Find and tap a button by text
    python scripts/navigator.py --find-text "Login" --tap

    # Find something below the fold, scrolling until it appears
    python scripts/navigator.py --find-text "About phone" --scroll-to-find --tap

    # Enter text into first EditText
    python scripts/navigator.py --find-type EditText --index 0 --enter-text "username"

    # Tap element by resource ID
    python scripts/navigator.py --find-id "submitButton" --tap

    # List all interactive elements
    python scripts/navigator.py --list

    # Tap at specific coordinates (fallback)
    python scripts/navigator.py --tap-at 200,400

Output Format:
    Tapped: Button "Login" at (320, 450)
    Entered text in: EditText "Username"
    Not found: text='Submit' (searched 1 screen; this screen scrolls -- retry
      with --scroll-to-find)
    Not found: text='Submit' (searched 4 screens, scrolled to the end: the
      screen stopped changing after 3 scrolls)

Why --scroll-to-find is opt-in
------------------------------
A lookup that scrolls is not a read: it leaves the app at a different scroll
offset, and on a real screen a swipe can fling a list, trigger pull-to-refresh,
page a ViewPager, or load more rows. A test that ran ``--find-text`` to assert
"the Save button is visible" would silently start passing for a Save button
three screens down, and every later coordinate the agent recorded would be
stale. So the finders search the visible screen by default and scroll only when
asked.

The default still has to answer the question that made this necessary: when
nothing matches and the screen *does* have a scrollable container, the failure
says so and names the flag, so "not found" is never mistaken for "not there".

Navigation Priority (best to worst):
    1. Find by text (content-desc or text attribute - most reliable)
    2. Find by element type + index (good for forms)
    3. Find by resource ID (precise but app-specific)
    4. Tap at coordinates (last resort, fragile)

Technical Details:
- Uses uiautomator dump via `adb shell uiautomator dump`
- Parses XML hierarchy with element bounds and attributes
- Finds elements by parsing tree recursively
- Calculates tap coordinates from element bounds center
- Uses `adb shell input tap` for tapping, `adb shell input text` for text entry
- Extracts data from text, content-desc, and resource-id attributes
"""

import argparse
import json as json_lib
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

# Sibling script, resolved the same way build_and_test.py imports gradle: the
# scripts directory is on sys.path whether navigator is run directly or
# imported by the tests.
from gesture import GestureSimulator

from common import adb_exec
from common.device_utils import (
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_float, env_int
from common.hierarchy import HierarchyError, capture_hierarchy

# Tunable defaults (overridable via ANDROID_EMU_* env vars; see SKILL.md).
MAX_ELEMENTS_LISTED = env_int("ANDROID_EMU_MAX_ELEMENTS", 25)
TAP_SETTLE_SECONDS = env_float("ANDROID_EMU_TAP_SETTLE_MS", 500.0) / 1000.0

# Ceiling on --scroll-to-find. A scroll search that never terminates is worse
# than one that gives up with a count, so this is a hard bound even when the
# screen keeps changing (an infinite list does exactly that).
MAX_SCROLLS = env_int("ANDROID_EMU_MAX_SCROLLS", 10, min_value=1)

# Pause between the scroll swipe and the next hierarchy dump. A fling keeps
# moving after the finger lifts; uiautomator then either refuses to dump
# ("could not get idle state", retried inside capture_hierarchy) or captures a
# mid-flight screen whose bounds are stale.
SCROLL_SETTLE_SECONDS = env_float("ANDROID_EMU_SCROLL_SETTLE_MS", 600.0) / 1000.0


@dataclass
class Element:
    """Represents a UI element from Android UI hierarchy."""

    type: str  # Class name (Button, EditText, TextView, etc.)
    text: str | None
    content_desc: str | None
    resource_id: str | None
    bounds: tuple  # (x1, y1, x2, y2)
    clickable: bool
    enabled: bool

    @property
    def center(self) -> tuple:
        """Calculate center point for tapping."""
        x1, y1, x2, y2 = self.bounds
        x = (x1 + x2) // 2
        y = (y1 + y2) // 2
        return (x, y)

    @property
    def label(self) -> str:
        """Get best label for this element."""
        return self.text or self.content_desc or self.resource_id or "Unnamed"

    @property
    def description(self) -> str:
        """Human-readable description."""
        return f'{self.type} "{self.label}"'


@dataclass
class ScrollSearch:
    """What a scroll-into-view search looked at, and how it ended.

    The element alone is not enough for an agent to act on. "Not found" has at
    least four distinct meanings and they call for different next steps:

    - the screen does not scroll, so what is on it is all there is;
    - the list was scrolled to its end and the text is genuinely absent;
    - the scroll budget ran out while the screen was still moving, so more
      content may lie below;
    - a swipe itself failed, so nothing was actually searched past screen one.

    Every field here exists so :attr:`detail` can say which one happened.

    Attributes:
        element: The match, or None.
        screens_searched: Hierarchy dumps examined, including the first.
        scrolls: Scroll gestures issued.
        scrollable: Whether the first screen had a ``scrollable="true"`` node.
        stopped_unchanged: A scroll left the screen identical, so the list is
            at its end. Measured on Settings/API 35: from the end of the list,
            successive dumps across a swipe are byte-identical.
        hit_limit: The scroll budget was exhausted while still moving.
        failure: Message from a scroll gesture that failed, else None.
    """

    element: Element | None
    screens_searched: int
    scrolls: int
    scrollable: bool
    stopped_unchanged: bool = False
    hit_limit: bool = False
    failure: str | None = None

    @property
    def detail(self) -> str:
        """One parenthetical phrase naming what was searched and how it ended."""
        screens = f"{self.screens_searched} screen" + ("s" if self.screens_searched != 1 else "")
        if self.element is not None:
            if self.scrolls == 0:
                return "on the visible screen"
            return f"after {self.scrolls} scroll" + ("s" if self.scrolls != 1 else "")
        if self.failure:
            return f"searched {screens}, then the scroll failed: {self.failure}"
        if not self.scrollable:
            return f"searched {screens}; nothing on it scrolls, so there is no content below"
        if self.stopped_unchanged:
            scrolls = f"{self.scrolls} scroll" + ("s" if self.scrolls != 1 else "")
            return (
                f"searched {screens}, scrolled to the end: the screen stopped "
                f"changing after {scrolls}"
            )
        if self.hit_limit:
            return (
                f"searched {screens}, stopped at the {self.scrolls}-scroll limit "
                f"and the screen was still moving; raise --max-scrolls to look further"
            )
        return f"searched {screens}"


class Navigator:
    """Navigates Android apps using UI hierarchy data."""

    def __init__(self, serial: str | None = None):
        """Initialize navigator with optional device serial."""
        self.serial = serial
        self._tree_cache = None
        self._gestures: GestureSimulator | None = None

    def gestures(self) -> GestureSimulator:
        """The gesture simulator used for scroll searches, created on demand.

        Scrolling is :meth:`gesture.GestureSimulator.scroll` and nothing else.
        That method owns a correction worth not re-deriving: ``swipe`` names
        what the *finger* does and ``scroll`` names what the *content* does, so
        ``scroll("down")`` swipes the finger up. A second implementation here
        would be a second chance to get that backwards.
        """
        if self._gestures is None:
            self._gestures = GestureSimulator(self.serial)
        return self._gestures

    def get_ui_hierarchy(self, force_refresh: bool = False) -> ET.Element:
        """
        Get the UI hierarchy, cached for the lifetime of this navigator.

        Delegates to :func:`common.hierarchy.capture_hierarchy`, which captures
        via ``adb exec-out`` and writes no file on either side -- this used to
        pull to a fixed ``/tmp`` path shared with screen_mapper and
        device_utils, where concurrent runs read each other's screen.

        Args:
            force_refresh: Re-capture even if a tree is already cached.

        Returns:
            XML root element.

        Raises:
            RuntimeError: If the hierarchy could not be captured.
        """
        if self._tree_cache is not None and not force_refresh:
            return self._tree_cache

        self._tree_cache = capture_hierarchy(self.serial)
        return self._tree_cache

    def _parse_bounds(self, bounds_str: str) -> tuple:
        """
        Parse bounds string to coordinates.

        Args:
            bounds_str: Bounds string like "[0,0][1080,1920]"

        Returns:
            Tuple of (x1, y1, x2, y2)
        """
        # Format: [x1,y1][x2,y2]
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if match:
            return tuple(int(x) for x in match.groups())
        return (0, 0, 0, 0)

    def _flatten_tree(self, node: ET.Element, elements: list | None = None) -> list:
        """
        Flatten UI hierarchy into list of elements.

        Args:
            node: Current XML element
            elements: List to accumulate elements

        Returns:
            List of Element objects
        """
        if elements is None:
            elements = []

        # Get element attributes
        elem_class = node.get("class", "")
        text = node.get("text", "")
        content_desc = node.get("content-desc", "")
        resource_id = node.get("resource-id", "")
        bounds_str = node.get("bounds", "[0,0][0,0]")
        clickable = node.get("clickable", "false") == "true"
        enabled = node.get("enabled", "true") == "true"

        # Extract simple class name
        simple_class = elem_class.split(".")[-1] if elem_class else "Unknown"

        # Parse bounds
        bounds = self._parse_bounds(bounds_str)

        # Create element
        element = Element(
            type=simple_class,
            text=text if text else None,
            content_desc=content_desc if content_desc else None,
            resource_id=(
                resource_id.split("/")[-1] if resource_id else None
            ),  # Extract ID from full path
            bounds=bounds,
            clickable=clickable,
            enabled=enabled,
        )
        elements.append(element)

        # Recurse to children
        for child in node:
            self._flatten_tree(child, elements)

        return elements

    def find_element(
        self,
        text: str | None = None,
        element_type: str | None = None,
        resource_id: str | None = None,
        index: int = 0,
        fuzzy: bool = True,
    ) -> Element | None:
        """
        Find element by various criteria.

        Args:
            text: Text to search in text/content-desc
            element_type: Type of element (Button, EditText, etc.)
            resource_id: Resource ID (without package prefix)
            index: Which matching element to return (0-based)
            fuzzy: Use fuzzy matching for text (case-insensitive substring)

        Returns:
            Element if found, None otherwise
        """
        return self._find_in(
            self.get_ui_hierarchy(),
            text=text,
            element_type=element_type,
            resource_id=resource_id,
            index=index,
            fuzzy=fuzzy,
        )

    def _find_in(
        self,
        root: ET.Element,
        text: str | None = None,
        element_type: str | None = None,
        resource_id: str | None = None,
        index: int = 0,
        fuzzy: bool = True,
    ) -> Element | None:
        """Apply :meth:`find_element`'s criteria to an already-captured tree.

        Split out so a scroll search can re-run the identical matching against
        each new screen without re-capturing, and without the two paths drifting
        apart.

        Args:
            root: A captured hierarchy root.
            text: Text to search in text/content-desc.
            element_type: Type of element (Button, EditText, etc.).
            resource_id: Resource ID (without package prefix).
            index: Which matching element to return (0-based).
            fuzzy: Use fuzzy matching for text (case-insensitive substring).

        Returns:
            Element if found, None otherwise.
        """
        elements = self._flatten_tree(root)

        matches = []

        for elem in elements:
            # Skip disabled elements
            if not elem.enabled:
                continue

            # Check type
            if element_type and elem.type != element_type:
                continue

            # Check resource ID (partial match)
            if resource_id:
                if not elem.resource_id or resource_id not in elem.resource_id:
                    continue

            # Check text (in text or content_desc)
            if text:
                elem_text = (elem.text or "") + " " + (elem.content_desc or "")
                if fuzzy:
                    if text.lower() not in elem_text.lower():
                        continue
                elif text not in (elem.text, elem.content_desc):
                    continue

            matches.append(elem)

        if matches and index < len(matches):
            return matches[index]

        return None

    @staticmethod
    def _scrollable_nodes(root: ET.Element) -> list[ET.Element]:
        """Nodes advertising ``scrollable="true"``.

        uiautomator sets this from AccessibilityNodeInfo, which reports it only
        when the container can actually scroll *now* -- a list whose content
        fits reports false. So this is the honest answer to "is there anything
        below the fold", and it is why a screen with none of these is reported
        as unscrollable instead of being swiped at hopefully.
        """
        return [node for node in root.iter() if node.get("scrollable") == "true"]

    def screen_scrolls(self) -> bool:
        """Whether the current (cached) screen has a scrollable container.

        Reads the hierarchy already captured for the search, so asking costs no
        extra adb call.
        """
        return bool(self._scrollable_nodes(self.get_ui_hierarchy()))

    @staticmethod
    def _screen_signature(root: ET.Element) -> tuple:
        """A comparable fingerprint of the scrolling region's contents.

        Detecting "the scroll did nothing" needs a comparison that is sensitive
        to movement and insensitive to everything else. Two decisions:

        - Only the subtrees under ``scrollable="true"`` nodes count. A window
          dump can include chrome that changes on its own -- a status-bar clock
          is the obvious one -- and comparing whole dumps would call a stuck
          list "still moving" once a minute, so the early exit would never fire.
        - ``bounds`` is part of the fingerprint. A short scroll may reveal no
          new rows while still moving every row it shows; treating that as "no
          change" would stop the search early with the target one swipe away.

        Falls back to the whole tree when nothing is scrollable, so the function
        is still meaningful for a caller that compares two static screens.
        """
        containers = Navigator._scrollable_nodes(root) or [root]
        return tuple(
            (
                node.get("class", ""),
                node.get("resource-id", ""),
                node.get("text", ""),
                node.get("content-desc", ""),
                node.get("bounds", ""),
            )
            for container in containers
            for node in container.iter()
        )

    def find_element_scrolling(
        self,
        text: str | None = None,
        element_type: str | None = None,
        resource_id: str | None = None,
        index: int = 0,
        fuzzy: bool = True,
        direction: str = "down",
        max_scrolls: int = MAX_SCROLLS,
        progress: Callable[[str], None] | None = None,
    ) -> ScrollSearch:
        """Find an element, scrolling the screen until it comes into view.

        The visible screen is searched first, so an element already on screen
        costs exactly one hierarchy dump and no gesture. Only when that misses
        does anything move.

        The search is bounded twice over. ``max_scrolls`` is the hard ceiling,
        and it stops earlier when a scroll leaves the screen unchanged -- the
        end of a list. Without that second bound the loop would keep swiping at
        a list that is already at its end, reporting a confident "searched 10
        screens" after searching one screen ten times.

        Args:
            text: Text to search in text/content-desc.
            element_type: Type of element (Button, EditText, etc.).
            resource_id: Resource ID (without package prefix).
            index: Which matching element to return (0-based), per screen.
            fuzzy: Use fuzzy matching for text (case-insensitive substring).
            direction: Content direction to scroll, 'down' (reveal what is
                below, the default) or 'up'.
            max_scrolls: Maximum scroll gestures before giving up.
            progress: Optional callback given one human-readable line per step,
                for ``--verbose``.

        Returns:
            A :class:`ScrollSearch` describing the outcome and what was tried.
            ``result.element`` is None when nothing matched, and
            ``result.detail`` says whether that means "absent" or "gave up".
        """

        def note(message: str) -> None:
            if progress is not None:
                progress(message)

        root = self.get_ui_hierarchy(force_refresh=True)
        screens = 1
        criteria = {
            "text": text,
            "element_type": element_type,
            "resource_id": resource_id,
            "index": index,
            "fuzzy": fuzzy,
        }

        element = self._find_in(root, **criteria)
        scrollable = bool(self._scrollable_nodes(root))
        note(f"screen 1: {'match' if element else 'no match'}")
        if element is not None:
            return ScrollSearch(element, screens, 0, scrollable)

        if not scrollable:
            note("no scrollable container on screen; not scrolling")
            return ScrollSearch(None, screens, 0, False)

        signature = self._screen_signature(root)
        scrolls = 0

        for _ in range(max_scrolls):
            success, message = self.gestures().scroll(direction)
            if not success:
                note(f"scroll failed: {message}")
                return ScrollSearch(None, screens, scrolls, True, failure=message)
            scrolls += 1
            if SCROLL_SETTLE_SECONDS > 0:
                time.sleep(SCROLL_SETTLE_SECONDS)

            root = self.get_ui_hierarchy(force_refresh=True)
            screens += 1
            element = self._find_in(root, **criteria)
            note(
                f"scrolled {direction} ({scrolls}); screen {screens}: "
                f"{'match' if element else 'no match'}"
            )
            if element is not None:
                return ScrollSearch(element, screens, scrolls, True)

            new_signature = self._screen_signature(root)
            if new_signature == signature:
                note("screen did not change; this is the end of the list")
                return ScrollSearch(None, screens, scrolls, True, stopped_unchanged=True)
            signature = new_signature

        note(f"reached the {max_scrolls}-scroll limit with the screen still changing")
        return ScrollSearch(None, screens, scrolls, True, hit_limit=True)

    def tap(self, element: Element) -> tuple:
        """
        Tap on an element.

        Args:
            element: Element to tap

        Returns:
            (success, message) tuple
        """
        x, y = element.center
        success, message = self.tap_at(x, y)
        if success:
            return True, f"Tapped: {element.description} at ({x}, {y})"
        return False, message

    def tap_at(self, x: int, y: int) -> tuple:
        """
        Tap at specific coordinates.

        Args:
            x: X coordinate
            y: Y coordinate

        Returns:
            (success, message) tuple
        """
        try:
            adb_exec.run_adb("shell", self.serial, "input", "tap", str(x), str(y), check=True)
            # Let the UI settle after the tap (animations, transitions, focus).
            if TAP_SETTLE_SECONDS > 0:
                time.sleep(TAP_SETTLE_SECONDS)
            return True, f"Tapped at ({x}, {y})"
        except adb_exec.AdbCommandError as e:
            return False, f"Tap failed: {e}"

    def enter_text(self, element: Element, text: str) -> tuple:
        """
        Enter text into an element (usually EditText).

        Args:
            element: Element to type into
            text: Text to enter

        Returns:
            (success, message) tuple
        """
        # First tap the element to focus it
        tap_success, _ = self.tap(element)
        if not tap_success:
            return False, f"Failed to focus {element.description}"

        # Enter text
        success, message = self.type_text(text)
        if success:
            return True, f"Entered text in: {element.description}"
        return False, message

    def type_text(self, text: str) -> tuple:
        """
        Type text at current cursor position.

        Args:
            text: Text to type (spaces must be escaped as %s)

        Returns:
            (success, message) tuple
        """
        try:
            # `input text` maps %s to a space; the argument then crosses the
            # device shell, which re-parses it, so it must be quoted too.
            escaped_text = quote_for_device_shell(text.replace(" ", "%s"))

            adb_exec.run_adb("shell", self.serial, "input", "text", escaped_text, check=True)
            return True, f"Typed: {text}"
        except adb_exec.AdbCommandError as e:
            return False, f"Type failed: {e}"

    def list_elements(self, interactive_only: bool = True) -> list:
        """
        List all elements on screen.

        Args:
            interactive_only: Only return clickable/focusable elements

        Returns:
            List of elements
        """
        root = self.get_ui_hierarchy(force_refresh=True)
        elements = self._flatten_tree(root)

        if interactive_only:
            # Filter to clickable and enabled elements
            return [e for e in elements if e.clickable and e.enabled and e.label != "Unnamed"]

        return elements


def main():
    parser = argparse.ArgumentParser(
        description="Android semantic element navigation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find and tap button by text (fuzzy)
  python navigator.py --find-text "Login" --tap

  # Find by exact text (non-fuzzy) and tap
  python navigator.py --find-exact "Sign In" --tap

  # Reach an item below the fold (scrolls; off by default because it moves
  # the app's scroll position, which a plain lookup must not do)
  python navigator.py --find-text "About phone" --scroll-to-find --tap

  # Scroll back up to find something above the current position
  python navigator.py --find-id "header" --scroll-to-find --scroll-direction up

  # Find EditText and enter text
  python navigator.py --find-type EditText --enter-text "user@example.com"

  # Find by resource ID and tap
  python navigator.py --find-id "submitButton" --tap

  # Find second button (0-indexed)
  python navigator.py --find-type Button --index 1 --tap

  # List all interactive elements
  python navigator.py --list

  # Tap at coordinates (fallback)
  python navigator.py --tap-at 200,400

Environment overrides:
  ANDROID_EMU_TAP_SETTLE_MS     Settle delay after each tap, ms (default: 500)
  ANDROID_EMU_MAX_ELEMENTS      Max elements shown by --list (default: 25)
  ANDROID_EMU_MAX_SCROLLS       Default --max-scrolls (default: 10)
  ANDROID_EMU_SCROLL_SETTLE_MS  Settle delay after each scroll, ms (default: 600)
        """,
    )

    parser.add_argument("--serial", "-s", help="Device serial number (auto-detects if omitted)")
    parser.add_argument("--find-text", help="Find element by text (fuzzy match)")
    parser.add_argument("--find-exact", help="Find element by exact text (non-fuzzy match)")
    parser.add_argument("--find-type", help="Find element by type (Button, EditText, etc.)")
    parser.add_argument("--find-id", help="Find element by resource ID")
    parser.add_argument(
        "--index", type=int, default=0, help="Index of matching element (default: 0)"
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help="Use exact text matching for --find-text (not fuzzy)",
    )
    parser.add_argument(
        "--scroll-to-find",
        action="store_true",
        help=(
            "Scroll until the element comes into view. Off by default: a swipe "
            "moves the app's scroll position and can fling, refresh or page it, "
            "so a plain lookup stays a read of the visible screen"
        ),
    )
    parser.add_argument(
        "--scroll-direction",
        choices=["down", "up"],
        default="down",
        help="Direction the CONTENT moves during --scroll-to-find (default: down)",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=MAX_SCROLLS,
        help=f"Scroll budget for --scroll-to-find (default: {MAX_SCROLLS})",
    )
    parser.add_argument("--tap", action="store_true", help="Tap the found element")
    parser.add_argument("--enter-text", help="Enter text into found element")
    parser.add_argument("--tap-at", help="Tap at coordinates (format: x,y)")
    parser.add_argument("--list", action="store_true", help="List all interactive elements")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Report each screen searched and each scroll issued",
    )

    args = parser.parse_args()

    if args.max_scrolls < 1:
        # A budget of zero would report "stopped at the 0-scroll limit" from a
        # search that never scrolled, which is a worse answer than a usage
        # error. Omit --scroll-to-find to search only the visible screen.
        parser.error("--max-scrolls must be at least 1")

    # Resolve device
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    navigator = Navigator(serial)

    try:
        _run_action(navigator, args)
    except (adb_exec.AdbError, HierarchyError) as error:
        # Either the command never reached a device, or the screen would not
        # hold still long enough to dump. Both errors name their remedy, so
        # print it rather than letting a traceback bury it. HierarchyError
        # reaches here often now that a scroll search dumps the screen once per
        # scroll, each dump landing just after a fling.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


def _run_action(navigator: Navigator, args: argparse.Namespace) -> None:
    """Perform the requested action, exiting with its status.

    Split out of ``main()`` only so that every adb failure raised anywhere in
    here lands in the single ``except adb_exec.AdbError`` above.
    """
    # List mode
    if args.list:
        elements = navigator.list_elements()
        total = len(elements)
        shown = elements[:MAX_ELEMENTS_LISTED]
        truncated = total - len(shown)
        if args.json:
            elements_data = [
                {
                    "type": e.type,
                    "label": e.label,
                    "bounds": e.bounds,
                    "center": e.center,
                }
                for e in shown
            ]
            print(
                json_lib.dumps(
                    {
                        "elements": elements_data,
                        "count": total,
                        "shown": len(shown),
                        "truncated": truncated,
                    },
                    indent=2,
                )
            )
        else:
            print(f"Interactive elements ({total}):")
            for i, elem in enumerate(shown):
                x, y = elem.center
                line = f"  {i}. {elem.description} at ({x}, {y})"
                if args.verbose:
                    line += f" bounds={elem.bounds}"
                print(line)
            if truncated > 0:
                print(
                    f"  ... and {truncated} more "
                    f"(showing first {MAX_ELEMENTS_LISTED}; "
                    f"raise ANDROID_EMU_MAX_ELEMENTS to see more)"
                )
        sys.exit(0)

    # Tap at coordinates mode
    if args.tap_at:
        try:
            x, y = map(int, args.tap_at.split(","))
            success, message = navigator.tap_at(x, y)
            if args.json:
                print(json_lib.dumps({"success": success, "message": message}, indent=2))
            else:
                print(message)
            sys.exit(0 if success else 1)
        except ValueError:
            print("Error: --tap-at requires format 'x,y' (e.g., 200,400)", file=sys.stderr)
            sys.exit(1)

    # Resolve search text: --find-exact forces exact matching; --find-text is
    # fuzzy unless --exact is also passed. --find-exact takes precedence.
    if args.find_exact is not None:
        search_text = args.find_exact
        fuzzy = False
    else:
        search_text = args.find_text
        fuzzy = not args.exact

    # Find element mode. Both paths produce a ScrollSearch so the reporting
    # below is identical: the difference is only whether anything moved.
    if args.scroll_to_find:
        search = navigator.find_element_scrolling(
            text=search_text,
            element_type=args.find_type,
            resource_id=args.find_id,
            index=args.index,
            fuzzy=fuzzy,
            direction=args.scroll_direction,
            max_scrolls=args.max_scrolls,
            progress=(lambda line: print(f"  {line}", file=sys.stderr)) if args.verbose else None,
        )
    else:
        element = navigator.find_element(
            text=search_text,
            element_type=args.find_type,
            resource_id=args.find_id,
            index=args.index,
            fuzzy=fuzzy,
        )
        search = ScrollSearch(
            element=element,
            screens_searched=1,
            scrolls=0,
            scrollable=navigator.screen_scrolls(),
        )

    element = search.element

    if not element:
        criteria = []
        if search_text:
            criteria.append(f"text='{search_text}'")
        if args.find_type:
            criteria.append(f"type={args.find_type}")
        if args.find_id:
            criteria.append(f"id={args.find_id}")
        detail = search.detail
        if not args.scroll_to_find and search.scrollable:
            # The whole point: "not found" here means "not on this screen", and
            # the screen has more below. Saying so is what stops an agent
            # reading this as "the element does not exist".
            detail = "searched 1 screen; this screen scrolls -- retry with --scroll-to-find"
        message = f"Not found: {', '.join(criteria)} ({detail})"

        if args.json:
            print(
                json_lib.dumps(
                    {"success": False, "message": message, "search": _search_json(search)},
                    indent=2,
                )
            )
        else:
            print(message)
        sys.exit(1)

    # Perform action on found element. Its bounds come from the screen as it
    # is now -- after any scrolling -- so a tap lands where the element ended
    # up rather than where it started.
    if args.tap:
        success, message = navigator.tap(element)
    elif args.enter_text:
        success, message = navigator.enter_text(element, args.enter_text)
    else:
        # Just found the element, report it
        x, y = element.center
        success = True
        message = f"Found: {element.description} at ({x}, {y})"

    if search.scrolls:
        message = f"{message} ({search.detail})"
    if args.verbose:
        print(
            f"  bounds={element.bounds} clickable={element.clickable} "
            f"screens_searched={search.screens_searched} scrolls={search.scrolls}",
            file=sys.stderr,
        )

    if args.json:
        result = {
            "success": success,
            "message": message,
            "element": {
                "type": element.type,
                "label": element.label,
                "bounds": element.bounds,
                "center": element.center,
            },
            "search": _search_json(search),
        }
        print(json_lib.dumps(result, indent=2))
    else:
        print(message)

    sys.exit(0 if success else 1)


def _search_json(search: ScrollSearch) -> dict:
    """Machine-readable form of what the search tried.

    ``detail`` is the same sentence the text output prints, so a caller reading
    JSON does not have to reconstruct the distinction between "absent" and
    "gave up" from the flags.
    """
    return {
        "screens_searched": search.screens_searched,
        "scrolls": search.scrolls,
        "screen_scrollable": search.scrollable,
        "stopped_unchanged": search.stopped_unchanged,
        "hit_scroll_limit": search.hit_limit,
        "scroll_failure": search.failure,
        "detail": search.detail,
    }


if __name__ == "__main__":
    main()
