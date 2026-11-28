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
    python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap

    # Find something below the fold, scrolling until it appears
    python3 "$SKILL_DIR/scripts/navigator.py" --find-text "About phone" --scroll-to-find --tap

    # Enter text into first EditText
    python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --index 0 --enter-text "username"

    # Tap element by resource ID
    python3 "$SKILL_DIR/scripts/navigator.py" --find-id "submitButton" --tap

    # List all interactive elements
    python3 "$SKILL_DIR/scripts/navigator.py" --list

    # Tap at specific coordinates (fallback)
    python3 "$SKILL_DIR/scripts/navigator.py" --tap-at 200,400

    # Wait for text to appear, without touching anything
    python3 "$SKILL_DIR/scripts/navigator.py" --wait-for "Order confirmed"

    # Tap, wait for what the tap should bring, and get the screen that followed
    python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Place order" --tap --expect "Order placed"

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

Waiting
-------
``--wait-for TEXT`` reads the screen until TEXT is on it, and ``--tap --expect
TEXT`` does the same after one tap. One waiter, with one deadline over
everything it does (``--timeout``, default 30 seconds): reading the screen to
find the target, the tap, the settle pause, every dump and every pause between
dumps. It scrolls nothing, taps nothing more and changes no setting.

Each dump is given what is left of the deadline and is stopped ON THE DEVICE
when that is up (:func:`common.hierarchy.capture_hierarchy_within`), not from
here: a dump whose adb client is killed can stay registered on the device for
a moment, and the next dump anyone asks for -- the agent's next command -- is
then answered ``Killed``. The host's own timeout is set a few seconds past the
device's and ends a dump only when adb itself has stopped answering.

Three things can run past the deadline, each by a stated amount: the tap
command is given at least ``TAP_COMMAND_FLOOR_SECONDS``; an adb that does not
answer is given up on ``HOST_GRACE_SECONDS`` (and a rounding to whole seconds)
after the dump's limit; and when adb says the device is not there, or that
there are several, ``adb_exec`` asks adb once what is attached, to word the
error, with ten seconds of its own.

Once the tap has been sent, a wait that ran out says so, and so does an adb
failure on a read after it (the device gone, adb not answering) -- because the
one thing the caller must not do on a bare "adb failed, retry" is tap again.
An interrupt is not covered: it is ``Interrupted.`` and nothing more.

It has three answers, and the last capture that FINISHED decides between the
two failures: a screen without the text is "not observed"; anything else, or
nothing, is "could not read the screen". The second is not a statement about
the text. A screen that never goes idle cannot be dumped at all, and a wait on
it ends that way however long it is given. Recorded: a ``TextView`` whose text
changes every 100 ms is such a screen; Compose texts changing as often are not.

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
import math
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field

# Sibling script, resolved the same way build_and_test.py imports gradle: the
# scripts directory is on sys.path whether navigator is run directly or
# imported by the tests.
from gesture import GestureSimulator

# Sibling script, imported the same way. `screen_mapper` already knows how to
# recover a Compose control's caption from its subtree or its row; duplicating
# that here is how the two files would drift into disagreeing about what is on
# the screen.
from screen_mapper import ScreenMapper

from common import adb_exec, cli
from common.device_utils import (
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_float, env_int
from common.hierarchy import (
    CAPTURE_ATTEMPTS,
    CaptureCutOff,
    HierarchyError,
    bare_resource_id,
    capture_hierarchy,
    capture_hierarchy_within,
    is_actionable,
    is_interactive,
    parse_bounds,
)

# Tunable defaults (overridable via ANDROID_EMU_* env vars; see SKILL.md).
MAX_ELEMENTS_LISTED = env_int("ANDROID_EMU_MAX_ELEMENTS", 25)
TAP_SETTLE_SECONDS = env_float("ANDROID_EMU_TAP_SETTLE_MS", 500.0) / 1000.0

# Hard ceiling on --max-scrolls, whatever the caller asks for. Each scroll costs
# a swipe, a settle and a hierarchy dump -- roughly a second and a half on the
# recorded emulator -- so a four-digit budget is not a longer search, it is a
# hang with a plausible explanation (C15).
MAX_SCROLLS_CEILING = 50


def _bounded_scroll_budget(value: object, source: str) -> int:
    """The one place a scroll budget is checked, whatever it came from.

    Args:
        value: The requested budget, as typed or as read from the environment.
        source: What to name in the error -- a flag or an env var.

    Returns:
        The budget, guaranteed to be within 1..MAX_SCROLLS_CEILING.

    Raises:
        ValueError: When it is not a whole number in range.
    """
    try:
        budget = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{source}={value!r} is not a whole number of scrolls") from None
    if budget < 1:
        raise ValueError(
            f"{source} must be at least 1; omit --scroll-to-find to search only "
            f"the visible screen"
        )
    if budget > MAX_SCROLLS_CEILING:
        raise ValueError(
            f"{source} must be at most {MAX_SCROLLS_CEILING}; a search that needs more "
            f"scrolls than that is looking for something the screen does not have -- "
            f"narrow it with --find-id or --find-type instead"
        )
    return budget


def _default_max_scrolls() -> int:
    """The default budget, held to the same ceiling as the flag.

    `env_int` clamps the floor and nothing clamped the ceiling, so
    `ANDROID_EMU_MAX_SCROLLS=5000` walked straight past the argparse validator
    that exists to prevent exactly that -- the flag was checked and its default
    was not.
    """
    try:
        return _bounded_scroll_budget(
            env_int("ANDROID_EMU_MAX_SCROLLS", 10, min_value=1), "ANDROID_EMU_MAX_SCROLLS"
        )
    except ValueError as error:
        print(f"warning: {error}; using {MAX_SCROLLS_CEILING}", file=sys.stderr)
        return MAX_SCROLLS_CEILING


# Ceiling on --scroll-to-find. A scroll search that never terminates is worse
# than one that gives up with a count, so this is a hard bound even when the
# screen keeps changing (an infinite list does exactly that).
MAX_SCROLLS = _default_max_scrolls()

# Refusal for an element whose bounds uiautomator did not report in a form this
# skill can read. Naming the element and the remedy matters more than usual
# here: the alternative this replaces was a silent tap at (0, 0), which looks
# like success and lands on whatever occupies the corner.
_NO_BOUNDS_MESSAGE = (
    "Cannot act: element has no usable bounds -- {description} reported none this skill "
    "could parse. Re-read the screen with screen_mapper.py, or target the control "
    "directly with --tap-at x,y."
)

# Wall-clock budget for one --scroll-to-find, across every scroll it makes.
# The scroll count alone does not bound the time: a dump on a busy screen
# retries for up to ANDROID_EMU_UI_DUMP_TIMEOUT (60s) each, so ten scrolls can
# take ten minutes and the agent has no way to tell that from a hang.
SCROLL_SEARCH_DEADLINE_SECONDS = env_float("ANDROID_EMU_SCROLL_SEARCH_DEADLINE", 120.0)

# Pause between the scroll swipe and the next hierarchy dump. A fling keeps
# moving after the finger lifts; uiautomator then either refuses to dump
# ("could not get idle state", retried inside capture_hierarchy) or captures a
# mid-flight screen whose bounds are stale.
SCROLL_SETTLE_SECONDS = env_float("ANDROID_EMU_SCROLL_SETTLE_MS", 600.0) / 1000.0


# Default --timeout for --wait-for and --expect, in seconds. A dump of a settled
# screen took about two seconds on a warm emulator (up to seventeen on one that
# had just booted) and one of a screen that never goes idle gives up after about
# eleven, so this is a dozen reads of the first kind and two full answers of the
# second -- enough to be told which it is.
WAIT_TIMEOUT_SECONDS = 30.0

# The range --timeout accepts. The floor is a choice, not a measurement: a dump
# of a settled screen took about two seconds on a warm emulator, and a timeout
# of one second started no dump at all (none is started with under
# MIN_CAPTURE_SECONDS left). The ceiling is the same argument as
# MAX_SCROLLS_CEILING: past it this is not a longer wait but a hang with a
# plausible explanation.
WAIT_TIMEOUT_FLOOR = 3.0
WAIT_TIMEOUT_CEILING = 600.0

# A dump is not started with less than this left. uiautomator waits for a full
# second without an accessibility event before it reads anything, so a dump
# given less cannot answer: it would only be started to be stopped.
MIN_CAPTURE_SECONDS = 1.0

# Pause between one capture and the next while waiting. Short: the dump's own
# idle wait already spaces the reads out.
WAIT_POLL_SECONDS = 0.3

# The least the tap command itself is given under a deadline. `input tap` is
# never cut shorter than this, because a tap that was killed half way is a tap
# nobody can say happened; so a stalled adb can overrun the deadline by this
# much, and says so when it does.
TAP_COMMAND_FLOOR_SECONDS = 5

# How many of the screen's texts a failed wait quotes.
SCREEN_TEXTS_SHOWN = 8
SCREEN_TEXT_WIDTH = 40


def _now() -> float:
    """The waiter's clock. One function so a test can own the time."""
    return time.monotonic()


def _pause(seconds: float) -> None:
    """The waiter's sleep, for the same reason."""
    time.sleep(seconds)


class Deadline:
    """One monotonic deadline for a whole wait.

    Everything a wait does is paid for from here -- dumps, the tap, pauses --
    so the time each step may take is what is left, not a share of the total
    and not a default of its own. The scroll search's budget divides what
    remains across the capture layer's retries with a one-second floor, and
    the capture layer's default is three attempts of sixty seconds; neither is
    a bound on the whole, and this copies neither.
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._started = _now()

    @property
    def elapsed(self) -> float:
        """Seconds since the wait began."""
        return _now() - self._started

    @property
    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self.seconds - self.elapsed)

    def pause(self, seconds: float) -> None:
        """Sleep for ``seconds`` or for what is left, whichever is shorter."""
        seconds = min(seconds, self.remaining)
        if seconds > 0:
            _pause(seconds)


@dataclass
class Wait:
    """What a wait did, and how it ended.

    Attributes:
        text: The text waited for; None when any readable screen would do
            (reading the screen to find a tap's target).
        timeout: The whole budget, in seconds.
        found: The wait ended on a capture that satisfied it.
        element: The node that carried the text, when there was a text.
        root: The last capture that was a screen, or None.
        read_at: Seconds into the wait when that capture arrived.
        readable: Captures that were a screen.
        failed: Captures that answered and were not a screen.
        unfinished: Captures the device stopped at the deadline.
        last_failure: What the last capture that was not a screen said.
        last_was_screen: Whether the last capture that FINISHED was a screen.
            This, not the count, decides between "not observed" and "could not
            read": a screen read at two seconds says nothing about a screen
            that has answered `could not get idle state` ever since.
        elapsed: Seconds the wait took.
    """

    text: str | None
    timeout: float
    found: bool = False
    element: "Element | None" = None
    root: ET.Element | None = None
    read_at: float | None = None
    readable: int = 0
    failed: int = 0
    unfinished: int = 0
    last_failure: str | None = None
    last_was_screen: bool = False
    elapsed: float = 0.0

    @property
    def outcome(self) -> str:
        """``found``, ``not_observed`` or ``unreadable``."""
        if self.found:
            return "found"
        return "not_observed" if self.last_was_screen else "unreadable"

    @property
    def stale(self) -> bool:
        """Whether the screen in :attr:`root` was followed by a capture that was not one."""
        return self.root is not None and not self.last_was_screen


@dataclass
class Element:
    """Represents a UI element from Android UI hierarchy."""

    type: str  # Class name (Button, EditText, TextView, etc.)
    text: str | None
    content_desc: str | None
    resource_id: str | None
    # (x1, y1, x2, y2), or None when uiautomator reported no parseable bounds.
    # None means "where this is, is unknown" and every action refuses it; the
    # previous `(0, 0, 0, 0)` fallback meant "the top-left corner", which is a
    # real pixel and was duly tapped (C5).
    bounds: tuple[int, int, int, int] | None
    clickable: bool
    enabled: bool
    # Compose drives Checkbox, Switch and list rows through these rather than
    # through `clickable` alone, so reading only `clickable` misses controls
    # that are plainly operable. Defaulted so existing constructions still work.
    checkable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    # A caption borrowed from the subtree or a row-adjacent sibling, for a
    # control that carries no name of its own (every interactive Compose node).
    # Deliberately NOT folded into `content_desc`: a container row would then
    # match a --find-text for the text of its own child and be returned instead
    # of it, moving the tap from the label to the whole row.
    recovered_label: str | None = None
    # The raw uiautomator attributes this element was built from, so that
    # `interactive` can be answered by the one shared rule rather than by a
    # second local copy of it (C7). Synthesised from the typed fields when an
    # Element is constructed directly.
    attributes: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    # The node and its parent, kept so a match on a caption can be resolved to
    # the control that caption names (C1). Never serialised.
    node: ET.Element | None = field(default=None, repr=False, compare=False)
    parent: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Back-fill `attributes` for an Element built from typed fields alone."""
        if self.attributes:
            return
        self.attributes = {
            "enabled": "true" if self.enabled else "false",
            "clickable": "true" if self.clickable else "false",
            "checkable": "true" if self.checkable else "false",
            "long-clickable": "true" if self.long_clickable else "false",
            "scrollable": "true" if self.scrollable else "false",
        }
        if self.bounds is not None:
            left, top, right, bottom = self.bounds
            self.attributes["bounds"] = f"[{left},{top}][{right},{bottom}]"

    @property
    def center(self) -> tuple[int, int] | None:
        """Centre point for tapping, or None when the bounds are unknown."""
        if self.bounds is None:
            return None
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def interactive(self) -> bool:
        """Whether this element can be operated at all.

        Answered by :func:`common.hierarchy.is_interactive`, which is also what
        `screen_mapper` and `accessibility_audit` ask. Three files deciding the
        same question three ways is how navigator came to list a control that
        the screen report had already ruled out, and vice versa (C7).
        """
        return is_interactive(self.attributes)

    @property
    def names(self) -> tuple[str, ...]:
        """The human-readable names this element answers to.

        Its own ``text`` and ``content-desc``, plus the caption recovered from
        its subtree or its row -- which is what ``--list`` and `screen_mapper`
        print for a control with no text of its own, and therefore what an agent
        has to hand to ``--find-text`` (C1). A resource id is deliberately not
        here: an id is matched whole (see :meth:`Navigator._matches_text`),
        because ``com.android.settings:id/...`` makes a fuzzy search for
        "settings" match every node on the screen.
        """
        return tuple(name for name in (self.text, self.content_desc, self.recovered_label) if name)

    @property
    def label(self) -> str:
        """Get best label for this element.

        Same precedence as `screen_mapper`'s, and the same BARE resource id --
        `com.android.settings:id/search_action_bar` prints as
        `search_action_bar`. The two scripts print one name for one control, so
        a name the screen report showed is a name ``--find-text`` accepts, and
        the bare form is the one ``--find-id`` has always taken. Compose's
        ``testTagsAsResourceId`` already emits an unqualified tag, so this is
        also the only choice under which both toolkits name things alike.
        """
        return (
            self.text
            or self.content_desc
            or bare_resource_id(self.resource_id)
            or self.recovered_label
            or "Unnamed"
        )

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
        hit_deadline: The wall-clock budget ran out. Distinct from hit_limit:
            the scrolls were not used up, the time was.
        failure: Message from a scroll gesture that failed, else None.
    """

    element: Element | None
    screens_searched: int
    scrolls: int
    scrollable: bool
    stopped_unchanged: bool = False
    hit_limit: bool = False
    hit_deadline: bool = False
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
        if self.hit_deadline:
            return (
                f"searched {screens}, then ran out of time after "
                f"{SCROLL_SEARCH_DEADLINE_SECONDS:.0f}s; raise "
                f"ANDROID_EMU_SCROLL_SEARCH_DEADLINE to look longer"
            )
        if self.hit_limit:
            return (
                f"searched {screens}, stopped at the {self.scrolls}-scroll limit "
                f"and the screen was still moving; raise --max-scrolls to look further"
            )
        return f"searched {screens}"


class Navigator:
    """Navigates Android apps using UI hierarchy data."""

    # One shared labeller; `_recover_label` is pure tree-walking and holds no
    # per-device state.
    _labeller = ScreenMapper()

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

    def get_ui_hierarchy(
        self, force_refresh: bool = False, timeout: int | None = None
    ) -> ET.Element:
        """
        Get the UI hierarchy, cached for the lifetime of this navigator.

        Delegates to :func:`common.hierarchy.capture_hierarchy`, which captures
        via ``adb exec-out`` and writes no file on either side -- this used to
        pull to a fixed ``/tmp`` path shared with screen_mapper and
        device_utils, where concurrent runs read each other's screen.

        Args:
            force_refresh: Re-capture even if a tree is already cached.
            timeout: Seconds to allow per dump attempt. None uses
                ``ANDROID_EMU_UI_DUMP_TIMEOUT``. A scroll search passes what is
                left of its deadline, because a dump on a screen that will not
                settle is the single longest thing this script does.

        Returns:
            XML root element.

        Raises:
            RuntimeError: If the hierarchy could not be captured.
        """
        if self._tree_cache is not None and not force_refresh:
            return self._tree_cache

        self._tree_cache = capture_hierarchy(self.serial, timeout=timeout)
        return self._tree_cache

    def _flatten_tree(
        self,
        node: ET.Element,
        elements: list | None = None,
        parent: ET.Element | None = None,
    ) -> list:
        """
        Flatten UI hierarchy into list of elements.

        The ``<hierarchy>`` root is walked but never emitted: it carries no
        ``class``, no bounds and no label, yet it satisfied every criterion an
        empty search applies, so it was `matches[0]` for a bare ``--tap`` and
        the tap went to (0, 0) (C2). `screen_mapper` has always gated on the
        same attribute.

        Args:
            node: Current XML element
            elements: List to accumulate elements
            parent: The node's parent, needed to recover a caption from a
                row-adjacent sibling and to resolve a caption to its control.

        Returns:
            List of Element objects
        """
        if elements is None:
            elements = []

        elem_class = node.get("class", "")
        if elem_class:
            text = node.get("text", "")
            content_desc = node.get("content-desc", "")
            resource_id = node.get("resource-id", "")

            # Compose controls carry no name of their own, so borrow the caption
            # that describes them -- from the subtree for a Button or Card, from
            # a row-adjacent sibling for a Checkbox or Switch.
            #
            # Gated exactly as `screen_mapper` gates it: only for a control, and
            # only when it has no name of its own. Both halves matter. A caption
            # recovered for a passive container is the concatenation of every
            # caption below it, so the root FrameLayout would answer to "Submit
            # Order" -- and, being first in document order, would take the tap
            # to the middle of the screen.
            recovered_label = None
            if not (text or content_desc or resource_id) and is_interactive(node):
                recovered_label = self._labeller._recover_label(node, parent) or None

            elements.append(
                Element(
                    type=elem_class.split(".")[-1],
                    text=text or None,
                    content_desc=content_desc or None,
                    resource_id=resource_id or None,
                    bounds=parse_bounds(node.get("bounds")),
                    clickable=node.get("clickable", "false") == "true",
                    enabled=node.get("enabled", "true") == "true",
                    checkable=node.get("checkable", "false") == "true",
                    long_clickable=node.get("long-clickable", "false") == "true",
                    scrollable=node.get("scrollable", "false") == "true",
                    recovered_label=recovered_label,
                    attributes=dict(node.attrib),
                    node=node,
                    parent=parent,
                )
            )

        # Recurse to children
        for child in node:
            self._flatten_tree(child, elements, parent=node)

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
        by_node = {id(element.node): element for element in elements}
        parent_of = {id(child): node for node in root.iter() for child in node}

        matches: list[Element] = []
        seen: set[int] = set()

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

            # Check text (own text, content-desc, recovered caption, or a
            # resource id given in full).
            if text and not self._matches_text(elem, text, fuzzy):
                continue

            # A caption is not a control. Hand back the thing the caption names
            # so that `center` -- and therefore the tap -- belongs to the
            # control and not to the label beside it (C1).
            #
            # Only for a search by name alone. `--find-id` and `--find-type`
            # point at a node, and the control that owns it carries neither that
            # id nor that class, so resolving there would answer with something
            # that does not satisfy the criterion the caller gave.
            resolved = elem
            if text and not (element_type or resource_id) and not elem.interactive:
                owner = self._owning_control(elem, by_node, parent_of)
                # No owner means the name belongs to a passive label and to
                # nothing operable. Keeping the label in the results would let
                # the caller "find" it and then tap a no-op; `_run_action`
                # refuses on `interactive` being False, so it is kept here and
                # rejected there, where the message can say so.
                resolved = owner or elem

            if id(resolved.node) in seen:
                continue
            seen.add(id(resolved.node))
            matches.append(resolved)

        # Bounded at both ends: `--index -1` has always meant the last match,
        # and `--index -99` was an IndexError traceback from five matches.
        if matches and -len(matches) <= index < len(matches):
            return matches[index]

        return None

    def _matches_text(self, elem: Element, text: str, fuzzy: bool) -> bool:
        """Whether ``elem`` answers to ``text``.

        The names are the ones the agent was shown -- ``--list`` and
        `screen_mapper` print :attr:`Element.label`, and a control with no text
        of its own is printed under its recovered caption -- so every printed
        name is findable. That is the whole of C1: the caption was printed and
        then not searchable, which left two of the seven labels on a Compose
        screen answering "Not found".

        A resource id matches only whole, in either the bare form this script
        prints (``search_action_bar``) or the qualified form uiautomator
        reports (``com.android.settings:id/search_action_bar``) -- never as a
        substring, because ids embed the package and a fuzzy search for
        "settings" would otherwise match every node in the Settings app. Both
        forms are accepted so that a name copied from an older report, or from
        a dump read directly, still resolves.
        """
        if fuzzy:
            lowered = text.lower()
            if any(lowered in name.lower() for name in elem.names):
                return True
        elif text in elem.names:
            return True

        if elem.resource_id:
            return text in (elem.resource_id, bare_resource_id(elem.resource_id))
        return False

    def _owning_control(
        self,
        caption: Element,
        by_node: dict[int, Element],
        parent_of: dict[int, ET.Element],
    ) -> Element | None:
        """The control a caption describes, or None when the caption stands alone.

        Ownership is STRUCTURAL -- it asks where the caption sits, never what it
        says. Two placements, both measured on recorded dumps:

        - **An ancestor.** A Compose Button's "Submit Order" and a Settings
          row's "Battery 100%" sit *inside* the control, so the nearest
          TAPPABLE ancestor owns them.
        - **A row-adjacent sibling.** A Compose Checkbox's "Remember me" and a
          Switch's "Dark theme" are siblings of the control, not ancestors of
          it -- so a "nearest ancestor" rule alone taps the caption and misses
          the Checkbox by 143px, which is exactly what v0.6.0 did. The row is
          established by overlapping vertical bounds, not by parentage; the NEXT
          sibling is tried before the previous one, because a caption follows
          its control in both Compose layouts recorded here and the reverse
          order would make "Dark theme" resolve to the Checkbox above it.

        An owner must be **tappable**, not merely interactive: `is_actionable`,
        so `scrollable` alone does not qualify. A scroll container encloses most
        of a screen, so it is very often the first interactive ancestor of a
        passive label -- and resolving to it moves the tap to the centre of the
        screen while the message still names the label, which is the "success
        that is indistinguishable from a miss" this whole increment is about. A
        scrollable-only ancestor is therefore passed over: the search continues
        upward, then tries the row, and failing both the caller refuses.

        The owner does NOT have to answer to the caption's name. It did in the
        first version of this, and that requirement silently un-fixed C1 for
        every control whose own name is a resource id: `search_action_bar`
        carries an id, so it recovers no caption, so it "did not answer to"
        `Search settings` and the tap went back to the passive TextView inside
        it. What keeps the structural rule safe is the other end -- a match that
        resolves to nothing tappable is refused rather than tapped.
        """
        ancestor = parent_of.get(id(caption.node))
        while ancestor is not None:
            owner = by_node.get(id(ancestor))
            if owner is not None and is_actionable(owner.attributes):
                return owner
            ancestor = parent_of.get(id(ancestor))

        parent = caption.parent
        if parent is None or caption.bounds is None:
            return None
        siblings = list(parent)
        position = next(
            (index for index, node in enumerate(siblings) if node is caption.node), None
        )
        if position is None:
            return None

        _, caption_top, _, caption_bottom = caption.bounds
        for offset in (1, -1):
            neighbour = position + offset
            if not 0 <= neighbour < len(siblings):
                continue
            candidate = by_node.get(id(siblings[neighbour]))
            if candidate is None or candidate.bounds is None:
                continue
            if not is_actionable(candidate.attributes):
                continue
            # Same row: the vertical spans must overlap.
            if not (caption_top < candidate.bounds[3] and candidate.bounds[1] < caption_bottom):
                continue
            return candidate
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

        The search is bounded three times over. ``max_scrolls`` is the hard
        ceiling on gestures, and it stops earlier when a scroll leaves the
        screen unchanged -- the end of a list. Without that second bound the
        loop would keep swiping at a list that is already at its end, reporting
        a confident "searched 10 screens" after searching one screen ten times.

        The third bound is wall-clock: ``ANDROID_EMU_SCROLL_SEARCH_DEADLINE``
        (default 120s). A scroll count is not a time bound, because each dump
        retries for up to ``ANDROID_EMU_UI_DUMP_TIMEOUT`` on a screen that will
        not settle -- which is precisely the screen a scroll search creates. The
        deadline is checked before each scroll, so the search never starts work
        it has no time to finish (C15).

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

        # The clock starts HERE, before the first capture, because the first
        # capture is inside the search: a dump on a screen that will not settle
        # retries for up to ANDROID_EMU_UI_DUMP_TIMEOUT per attempt, and a
        # deadline that started afterwards would have been measuring everything
        # except the slowest thing it was meant to bound (C15).
        deadline = time.monotonic() + SCROLL_SEARCH_DEADLINE_SECONDS

        def out_of_time() -> bool:
            return SCROLL_SEARCH_DEADLINE_SECONDS > 0 and time.monotonic() >= deadline

        def capture() -> ET.Element:
            """Dump the screen, giving it no more than the search has left."""
            timeout = None
            if SCROLL_SEARCH_DEADLINE_SECONDS > 0:
                # `capture_hierarchy` retries, and `timeout` bounds one attempt,
                # so the remaining budget is split across the attempts it may
                # make. Otherwise a single dump could spend the whole deadline
                # and the retries would run past it.
                remaining = deadline - time.monotonic()
                timeout = max(1, int(remaining / CAPTURE_ATTEMPTS))
            return self.get_ui_hierarchy(force_refresh=True, timeout=timeout)

        root = capture()
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
            if out_of_time():
                note(
                    f"the {SCROLL_SEARCH_DEADLINE_SECONDS:.0f}s search deadline passed after "
                    f"{scrolls} scrolls; stopping"
                )
                return ScrollSearch(None, screens, scrolls, True, hit_deadline=True)

            success, message = self.gestures().scroll(direction)
            if not success:
                note(f"scroll failed: {message}")
                return ScrollSearch(None, screens, scrolls, True, failure=message)
            scrolls += 1
            # Said now, not after the dump that follows. The dump is the slow
            # part -- settle, then up to three attempts -- and a caller watching
            # stderr needs to see that the scroll happened while it is waiting,
            # not once the waiting is over.
            note(f"scrolled {direction} ({scrolls}); reading the screen")
            if SCROLL_SETTLE_SECONDS > 0:
                time.sleep(SCROLL_SETTLE_SECONDS)

            if out_of_time():
                note(
                    f"the {SCROLL_SEARCH_DEADLINE_SECONDS:.0f}s search deadline passed after "
                    f"{scrolls} scrolls; stopping"
                )
                return ScrollSearch(None, screens, scrolls, True, hit_deadline=True)

            root = capture()
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

    def find_visible(self, root: ET.Element, text: str, fuzzy: bool = True) -> Element | None:
        """The first node on a captured screen that answers to ``text``, operable or not.

        What a wait matches. The names are the ones :meth:`_matches_text` knows
        -- a node's text or content description, a control's recovered caption,
        a resource id given whole -- so any name the screen report prints can
        be waited for, and so can a line of plain text.

        Deliberately not :meth:`_find_in`: that resolves a caption to the
        control that owns it and its caller refuses a passive label, because
        its answer is about to be tapped. A wait observes. "Order confirmed"
        is a label and nothing else, and it is exactly what is waited for.
        Disabled nodes are not skipped either: their text is on the screen.

        Args:
            root: A captured hierarchy root.
            text: The text to look for.
            fuzzy: Case-insensitive substring match; False for the whole text.

        Returns:
            The first match in document order, or None.
        """
        for element in self._flatten_tree(root):
            if self._matches_text(element, text, fuzzy):
                return element
        return None

    def watch(
        self,
        deadline: Deadline,
        text: str | None = None,
        fuzzy: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> Wait:
        """Read the screen until ``text`` is on it, or the deadline passes.

        With no ``text``, until the screen can be read at all: what a tap's
        targeting needs, and the retry of a just-launched app's ``null root
        node`` that the capture layer would otherwise make with a budget of its
        own.

        Only reads. Each dump is given what the deadline has left and is
        stopped on the device when that is up; a capture that answers and is
        not a screen is counted and the next one asked for, because the
        recorded ways that happens (the screen not idle, no window focused,
        another session registered) can pass by themselves. When one does not,
        the deadline ends the asking.

        Args:
            deadline: The wait's one deadline, already running.
            text: Text to wait for, or None for any readable screen.
            fuzzy: Case-insensitive substring match; False for the whole text.
            progress: Optional callback given one line per capture.

        Returns:
            A :class:`Wait`. A device that goes away raises instead
            (:class:`common.adb_exec.DeviceError`), as everywhere else.
        """
        wait = Wait(text=text, timeout=deadline.seconds)

        def note(outcome: str) -> None:
            if progress is not None:
                number = wait.readable + wait.failed + wait.unfinished
                progress(f"capture {number} at {deadline.elapsed:.1f}s: {outcome}")

        while deadline.remaining >= MIN_CAPTURE_SECONDS:
            try:
                root = capture_hierarchy_within(self.serial, deadline.remaining)
            except CaptureCutOff:
                # Nothing was learned about the screen; the capture before
                # this one, if any, is still the last word on it.
                wait.unfinished += 1
                note("did not finish in the time left")
            except HierarchyError as error:
                wait.failed += 1
                wait.last_failure = str(error)
                wait.last_was_screen = False
                note(f"not a screen ({str(error).split(':', 1)[0].split('.', 1)[0]})")
            else:
                wait.readable += 1
                wait.root = root
                wait.read_at = deadline.elapsed
                wait.last_was_screen = True
                self._tree_cache = root
                if text is None:
                    wait.found = True
                    note("a screen")
                else:
                    wait.element = self.find_visible(root, text, fuzzy)
                    wait.found = wait.element is not None
                    note("match" if wait.found else "no match")
                if wait.found:
                    break
            deadline.pause(WAIT_POLL_SECONDS)

        wait.elapsed = deadline.elapsed
        return wait

    def tap(self, element: Element, deadline: Deadline | None = None) -> tuple:
        """
        Tap on an element.

        Args:
            element: Element to tap
            deadline: A wait this tap is part of; see :meth:`tap_at`.

        Returns:
            (success, message) tuple
        """
        centre = element.center
        if centre is None:
            return False, _NO_BOUNDS_MESSAGE.format(description=element.description)
        x, y = centre
        success, message = self.tap_at(x, y, deadline)
        if success:
            return True, f"Tapped: {element.description} at ({x}, {y})"
        return False, message

    def tap_at(self, x: int, y: int, deadline: Deadline | None = None) -> tuple:
        """
        Tap at specific coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
            deadline: A wait this tap is part of. The tap is then not sent at
                all once the time is up, the command gets what is left, in
                whole seconds rounded up (never less than
                ``TAP_COMMAND_FLOOR_SECONDS``), and the settle pause is paid
                for from the deadline.

        Returns:
            (success, message) tuple
        """
        limits = {}
        if deadline is not None:
            if deadline.remaining <= 0:
                return False, (
                    f"Ran out of time before the tap ({deadline.seconds:g}s), so nothing "
                    f"was tapped"
                )
            limits["timeout"] = max(math.ceil(deadline.remaining), TAP_COMMAND_FLOOR_SECONDS)
        try:
            adb_exec.run_adb(
                "shell", self.serial, "input", "tap", str(x), str(y), check=True, **limits
            )
            # Let the UI settle after the tap (animations, transitions, focus).
            if deadline is not None:
                deadline.pause(TAP_SETTLE_SECONDS)
            elif TAP_SETTLE_SECONDS > 0:
                time.sleep(TAP_SETTLE_SECONDS)
            return True, f"Tapped at ({x}, {y})"
        except adb_exec.AdbCommandError as e:
            return False, f"Tap failed: {e}"
        except adb_exec.AdbTimeoutError:
            if deadline is None:
                raise
            return False, (
                f"The tap command did not finish within {limits['timeout']}s; {TAP_UNVOUCHED}"
            )

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
            message = f"Type failed: {e}"
            if not text.isascii():
                # Measured on API 35: `input text 'héllo'` throws a
                # NullPointerException and exits 255 with nothing typed. The
                # failure is real, but the cause is not visible in the stack
                # trace adb hands back, so name it here.
                message += (
                    " -- `input text` accepts ASCII only, and this string is not ASCII. "
                    "Set the text through the app, or use an IME that accepts it."
                )
            return False, message

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
            # `e.label != "Unnamed"` used to be part of this filter, and it made
            # `--list` return ZERO on any Compose screen: every interactive
            # Compose node has empty text and content-desc, which is measured
            # and pinned in tests/test_compose_visibility.py. An unlabelled
            # control is still tappable -- dropping it hides the only thing an
            # agent could act on. This is defect R11 surviving in navigator
            # after it was fixed in screen_mapper.
            return [e for e in elements if e.interactive]

        return elements


# What to do when a lookup finds nothing, or finds only a label. The message
# already says which, and how far the search went.
NOT_FOUND_REMEDY = (
    "Run screen_mapper.py and search for a name it prints; on a screen that scrolls, "
    "add --scroll-to-find. To act on a spot with no name, use --tap-at x,y."
)

# What to do when the element was found and the tap or text entry failed.
ACTION_REMEDY = (
    "Fix what the error names, then retry; screen_mapper.py shows the screen as it is "
    "now, and --tap-at x,y acts on coordinates directly."
)


# What to do when a wait read the screen and the text was not on it.
NOT_OBSERVED_REMEDY = (
    "The screen was read and the text was not on it. Compare the wording with the text "
    "quoted (without --exact, case is ignored and part of a text is enough; a resource "
    "id is matched whole, as written), allow longer with --timeout, or run "
    "screen_mapper.py for the controls."
)

# What to do when a wait could not say, because the screen would not be read.
UNREADABLE_REMEDY = (
    "This is not an answer about the text. If no app is in front or the display is off, "
    "fix that; if the screen never goes idle (some views that update several times a "
    "second do this) it cannot be dumped at any timeout, so read it from a screenshot; "
    "otherwise retry with a longer --timeout."
)

# How a tap that may or may not have happened ends its message, and what to do then:
# the usual "fix it and retry" would be a second tap on top of an unknown first.
TAP_UNVOUCHED = "whether the tap reached the screen is not known"
TAP_UNVOUCHED_REMEDY = (
    "Run screen_mapper.py to see whether the tap landed before tapping again; if adb "
    "keeps stalling, reset it with `adb kill-server && adb start-server`."
)


def _tap_remedy(message: str) -> str:
    """The remedy for a failed tap: look first when nobody can say it was not sent."""
    return TAP_UNVOUCHED_REMEDY if message.endswith(TAP_UNVOUCHED) else ACTION_REMEDY


# Said first when the wait came after a tap, whichever way it failed.
TAP_WAS_SENT = "The tap was sent, so do not repeat it before looking at the screen. "


def _screen_summary(root: ET.Element) -> dict:
    """A captured screen as counts and control names, with no call to the device.

    The names are `screen_mapper`'s, from the same function, so one copied
    from here is one ``--find-text`` accepts.
    """
    analysis = Navigator._labeller.analyze_captured(root)
    return {
        "total_elements": analysis["total_elements"],
        "interactive_elements": analysis["interactive_elements"],
        "controls": dict(ScreenMapper.interactive_names(analysis)),
        "lines": [
            f"Screen: {analysis['total_elements']} elements, "
            f"{analysis['interactive_elements']} interactive",
            *ScreenMapper.control_lines(analysis),
        ],
    }


def _screen_texts(root: ET.Element) -> list[str]:
    """The first few texts on a captured screen, in document order, each once.

    What a failed wait quotes: the control names say what can be done on the
    screen, and these say what it reads -- which is what the text waited for
    has to be compared with.
    """
    texts: list[str] = []
    for node in root.iter("node"):
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text or text in texts:
            continue
        if len(text) > SCREEN_TEXT_WIDTH:
            text = text[: SCREEN_TEXT_WIDTH - 3] + "..."
        texts.append(text)
        if len(texts) == SCREEN_TEXTS_SHOWN:
            break
    return texts


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _screen_sentence(wait: Wait) -> str:
    """The last readable screen on one line: counts, control names, texts."""
    summary = _screen_summary(wait.root)
    parts = [summary["lines"][0].removeprefix("Screen: "), *summary["lines"][1:]]
    texts = _screen_texts(wait.root)
    if texts:
        parts.append('text: "' + '", "'.join(texts) + '"')
    return "; ".join(parts)


def _wait_error(wait: Wait, *, to_find_target: bool = False) -> str:
    """One line saying how a wait that did not find its text ended."""
    budget = f"{wait.timeout:g}s"
    others = []
    if wait.failed:
        others.append(f"{_plural(wait.failed, 'capture')} answered without a screen")
    if wait.unfinished:
        others.append(f"{_plural(wait.unfinished, 'capture')} did not finish in the time left")

    if wait.outcome == "not_observed":
        read = f"{_plural(wait.readable, 'screen')} read, the last at {wait.read_at:.1f}s"
        return (
            f"Not observed: text='{wait.text}' did not appear within {budget} "
            f"({'; '.join([read, *others])}). Last screen: {_screen_sentence(wait)}."
        )

    purpose = " to find the target, so nothing was tapped" if to_find_target else ""
    if wait.failed:
        detail = f"{'; '.join(others)}. The last said: {wait.last_failure}"
    elif wait.unfinished:
        detail = (
            f"{'; '.join(others)}, and none answered within {budget}. A dump of a settled "
            f"screen took about two seconds on a warm emulator and longer on one just "
            f"booted; one of a screen that never goes idle gives up after about eleven."
        )
    else:
        detail = f"no time was left to read it within {budget}."
    message = f"Could not read the screen{purpose}: {detail}"
    if wait.root is not None:
        message += (
            f" An earlier capture read it at {wait.read_at:.1f}s; that reading is stale: "
            f"{_screen_sentence(wait)}."
        )
    return message


def _wait_json(wait: Wait) -> dict:
    """Machine-readable form of what a wait did."""
    summary = _screen_summary(wait.root) if wait.root is not None else None
    if summary is not None:
        del summary["lines"]
    return {
        "outcome": wait.outcome,
        "waited_for": wait.text,
        "elapsed_seconds": round(wait.elapsed, 1),
        "timeout_seconds": wait.timeout,
        "captures": {
            "readable": wait.readable,
            "failed": wait.failed,
            "unfinished": wait.unfinished,
        },
        "last_read_seconds": None if wait.read_at is None else round(wait.read_at, 1),
        "screen": summary,
        "screen_stale": wait.stale,
        "texts": _screen_texts(wait.root) if wait.root is not None else [],
        "last_failure": wait.last_failure,
    }


def _element_json(element: Element) -> dict:
    return {
        "type": element.type,
        "label": element.label,
        "bounds": element.bounds,
        "center": element.center,
    }


def _report_wait(
    wait: Wait,
    args: argparse.Namespace,
    *,
    tapped: str | None = None,
    tapped_at: list[int] | None = None,
) -> None:
    """Print a wait's answer and exit 0, or raise its failure.

    Args:
        wait: The finished wait.
        args: Parsed arguments (for ``--json``).
        tapped: The tap's own message, when the wait followed a tap.
        tapped_at: Where that tap went.
    """
    extra = {} if tapped_at is None else {"tapped": tapped, "tapped_at": tapped_at}
    if not wait.found:
        error = _wait_error(wait)
        remedy = NOT_OBSERVED_REMEDY if wait.outcome == "not_observed" else UNREADABLE_REMEDY
        if tapped is not None:
            error = f"{tapped}, then: {error}"
            remedy = TAP_WAS_SENT + remedy
        cli.fail(error, remedy, details={"wait": _wait_json(wait), **extra})

    summary = _screen_summary(wait.root)
    found = f"Found: text='{wait.text}' after {wait.elapsed:.1f}s ({wait.element.description})"
    if args.json:
        result = {
            "success": True,
            "message": found,
            "waited_for": wait.text,
            "elapsed_seconds": round(wait.elapsed, 1),
            "timeout_seconds": wait.timeout,
            "captures": {
                "readable": wait.readable,
                "failed": wait.failed,
                "unfinished": wait.unfinished,
            },
            "element": _element_json(wait.element),
            "screen": {key: value for key, value in summary.items() if key != "lines"},
            **extra,
        }
        print(json_lib.dumps(result, indent=2))
    else:
        if tapped is not None:
            print(tapped)
        print(found)
        print("\n".join(summary["lines"]))
    sys.exit(0)


def _wait_budget(value: str) -> float:
    """argparse ``type`` for ``--timeout``: seconds, finite, within the range."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of seconds") from None
    if not math.isfinite(seconds) or not WAIT_TIMEOUT_FLOOR <= seconds <= WAIT_TIMEOUT_CEILING:
        raise argparse.ArgumentTypeError(
            f"must be between {WAIT_TIMEOUT_FLOOR:g} and {WAIT_TIMEOUT_CEILING:g} seconds, "
            f"got {value!r}"
        )
    return seconds


def _scroll_budget(value: str) -> int:
    """argparse ``type`` for ``--max-scrolls``: the shared validator, re-worded.

    Both ends are usage errors rather than surprises. A budget of zero would
    report "stopped at the 0-scroll limit" from a search that never scrolled;
    a budget of 500 is not a longer search but a twenty-minute one, and the
    agent that typed it has no way to see the difference from a hang (C15).
    """
    try:
        return _bounded_scroll_budget(value, "--max-scrolls")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error).replace("--max-scrolls ", "")) from None


def main(argv: list[str] | None = None):
    # cli.parser: `--tap --json` with no criterion -- the very thing C2 added --
    # is a usage error, and under --json it is the refusal object at exit 2
    # rather than prose behind an empty stdout.
    parser = cli.parser(
        description="Android semantic element navigation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Find and tap button by text (fuzzy)
  python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap

  # Find by exact text (non-fuzzy) and tap
  python3 "$SKILL_DIR/scripts/navigator.py" --find-exact "Sign In" --tap

  # Reach an item below the fold (scrolls; off by default because it moves
  # the app's scroll position, which a plain lookup must not do)
  python3 "$SKILL_DIR/scripts/navigator.py" --find-text "About phone" --scroll-to-find --tap

  # Scroll back up to find something above the current position
  python3 "$SKILL_DIR/scripts/navigator.py" --find-id "header" --scroll-to-find --scroll-direction up

  # Find EditText and enter text
  python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --enter-text "user@example.com"

  # Find by resource ID and tap
  python3 "$SKILL_DIR/scripts/navigator.py" --find-id "submitButton" --tap

  # Find second button (0-indexed)
  python3 "$SKILL_DIR/scripts/navigator.py" --find-type Button --index 1 --tap

  # List all interactive elements
  python3 "$SKILL_DIR/scripts/navigator.py" --list

  # Tap at coordinates (fallback)
  python3 "$SKILL_DIR/scripts/navigator.py" --tap-at 200,400

  # Wait for text to appear; reads the screen and nothing else
  python3 "$SKILL_DIR/scripts/navigator.py" --wait-for "Order confirmed" --timeout 20

  # Tap, wait for what the tap should bring, and print the screen that followed
  python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Place order" --tap --expect "Order placed"

Environment overrides:
  ANDROID_EMU_TAP_SETTLE_MS     Settle delay after each tap, ms (default: 500)
  ANDROID_EMU_MAX_ELEMENTS      Max elements shown by --list (default: 25)
  ANDROID_EMU_MAX_SCROLLS       Default --max-scrolls (default: 10, max: 50)
  ANDROID_EMU_SCROLL_SETTLE_MS  Settle delay after each scroll, ms (default: 600)
  ANDROID_EMU_SCROLL_SEARCH_DEADLINE
                                Wall-clock budget for one --scroll-to-find,
                                seconds (default: 120). Bounds the whole search,
                                which --max-scrolls alone does not: a dump on an
                                unsettled screen can take a minute by itself.
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
        help="Match the whole text, not part of it: --find-text, --wait-for and --expect",
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
        type=_scroll_budget,
        default=MAX_SCROLLS,
        help=(
            f"Scroll budget for --scroll-to-find "
            f"(default: {MAX_SCROLLS}, maximum: {MAX_SCROLLS_CEILING})"
        ),
    )
    parser.add_argument("--tap", action="store_true", help="Tap the found element")
    parser.add_argument("--enter-text", help="Enter text into found element")
    parser.add_argument("--tap-at", help="Tap at coordinates (format: x,y)")
    parser.add_argument(
        "--wait-for",
        metavar="TEXT",
        help=(
            "Read the screen until TEXT is on it (any text, label or name the screen "
            "report prints; fuzzy unless --exact). Reads only: no scroll, no tap"
        ),
    )
    parser.add_argument(
        "--expect",
        metavar="TEXT",
        help=(
            "With --tap or --tap-at: after the tap, wait for TEXT as --wait-for does "
            "and print the screen that followed"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=_wait_budget,
        metavar="S",
        help=(
            f"Seconds --wait-for or --expect may take in all, finding the target and "
            f"the tap included (default: {WAIT_TIMEOUT_SECONDS:g}, "
            f"{WAIT_TIMEOUT_FLOOR:g} to {WAIT_TIMEOUT_CEILING:g})"
        ),
    )
    parser.add_argument("--list", action="store_true", help="List all interactive elements")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Report each screen searched and each scroll issued",
    )

    args = parser.parse_args(argv)

    # A wait is one thing or the other: watching, or watching after one tap.
    # Refused here, before anything reaches the device, like the check below.
    if args.wait_for is not None:
        others = [
            flag
            for flag, given in (
                # By presence, not truth: `--find-text ""` is still a second mode.
                ("--find-text", args.find_text is not None),
                ("--find-exact", args.find_exact is not None),
                ("--find-type", args.find_type is not None),
                ("--find-id", args.find_id is not None),
                ("--tap", args.tap),
                ("--enter-text", args.enter_text is not None),
                ("--tap-at", args.tap_at is not None),
                ("--list", args.list),
                ("--scroll-to-find", args.scroll_to_find),
                ("--expect", args.expect is not None),
            )
            if given
        ]
        if others:
            parser.error(
                f"--wait-for only watches the screen and does not go with {', '.join(others)}. "
                f"To act and then wait, use --tap --expect TEXT."
            )
        if not args.wait_for.strip():
            parser.error("--wait-for needs the text to wait for; it was given none")
    if args.expect is not None:
        if args.enter_text is not None or args.list or not (args.tap or args.tap_at):
            parser.error(
                "--expect waits for text after one tap: it goes with --tap (and a target) or "
                "--tap-at x,y, and with nothing else that acts. To wait without acting, use "
                "--wait-for TEXT."
            )
        if args.scroll_to_find:
            parser.error(
                "--expect does not go with --scroll-to-find: a scroll search has a budget of "
                "its own. Run the --scroll-to-find lookup first, then --tap --expect on the "
                "screen it leaves."
            )
        if not args.expect.strip():
            parser.error("--expect needs the text to wait for; it was given none")
    # A selector given an empty value is not "no selector": `--find-exact ""`
    # beside `--find-text "Place order"` took precedence over it and matched
    # every node, and the tap went to the first of them.
    for flag, value in (
        ("--find-text", args.find_text),
        ("--find-exact", args.find_exact),
        ("--find-type", args.find_type),
        ("--find-id", args.find_id),
    ):
        if value is not None and not value.strip():
            parser.error(f"{flag} needs a value that is not blank")
    if args.timeout is not None and args.wait_for is None and args.expect is None:
        parser.error("--timeout bounds --wait-for and --expect; there is neither here")
    if args.timeout is None:
        args.timeout = WAIT_TIMEOUT_SECONDS

    # An action needs a target (C2).
    #
    # Without this, `--tap` with no criterion matched every enabled node, and
    # the first of those was the `<hierarchy>` root: `input tap 0 0` was issued,
    # the exit status was 0, and the message named a real on-screen element that
    # had never been touched. That is the worst available answer, because it is
    # indistinguishable from success. Refused here, before anything reaches the
    # device.
    if (args.tap or args.enter_text) and not any(
        (args.find_text, args.find_exact, args.find_type, args.find_id, args.tap_at)
    ):
        action = "--tap" if args.tap else "--enter-text"
        parser.error(
            f"{action} needs a target: name one with --find-text, --find-exact, --find-type "
            f"or --find-id, or give coordinates with --tap-at x,y. Run --list (or "
            f"screen_mapper.py) to see what is on the screen."
        )

    # A device that cannot be resolved, a command that never reached it, or a
    # screen that would not hold still long enough to dump: all three raise, and
    # common.cli reports them. HierarchyError reaches it often, now that a scroll
    # search dumps the screen once per scroll, each dump landing after a fling.
    serial = resolve_device_identifier(args.serial)
    _run_action(Navigator(serial), args)


def _watch_after_tap(
    navigator: Navigator,
    deadline: Deadline,
    args: argparse.Namespace,
    tapped: str,
    tapped_at: list[int],
) -> Wait:
    """The wait that follows a tap; if adb fails under it, the answer still names the tap.

    A wait that runs out is reported by :func:`_report_wait`, which says so.
    This is for another way it can end: adb failing on a read after the tap
    -- the device gone, adb not answering. Left to the boundary, that is "the
    device is not attached; run `adb devices`" or "try `adb kill-server` and
    retry", with no word of the tap, and retrying the command taps again. The
    adb error keeps its exit status (3 for a device that is unavailable) and
    its remedy; the tap is put in front of both.
    """
    try:
        return navigator.watch(
            deadline, args.expect, fuzzy=not args.exact, progress=_wait_progress(args)
        )
    except adb_exec.AdbError as error:
        what, remedy = cli.describe(error)
        cli.fail(
            f"{tapped}, then the screen could not be read: {what}",
            TAP_WAS_SENT + remedy,
            details={"tapped": tapped, "tapped_at": tapped_at},
            exit_status=cli.exit_code_for(error),
        )


def _wait_progress(args: argparse.Namespace) -> Callable[[str], None] | None:
    """Where a wait's per-capture lines go: stderr under --verbose, nowhere otherwise."""
    if not args.verbose:
        return None
    return lambda line: print(f"  {line}", file=sys.stderr)


def _run_action(navigator: Navigator, args: argparse.Namespace) -> None:
    """Perform the requested action, exiting with its status."""
    # Wait mode: read until the text is there. Nothing else happens.
    if args.wait_for is not None:
        wait = navigator.watch(
            Deadline(args.timeout),
            args.wait_for,
            fuzzy=not args.exact,
            progress=_wait_progress(args),
        )
        _report_wait(wait, args)

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
                centre = elem.center
                where = f"({centre[0]}, {centre[1]})" if centre else "(bounds not reported)"
                line = f"  {i}. {elem.description} at {where}"
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
            x, y = (int(part) for part in args.tap_at.split(","))
        except ValueError:
            cli.fail(
                f"--tap-at requires two whole numbers as 'x,y'; got {args.tap_at!r}",
                "Pass the coordinates as x,y, e.g. --tap-at 200,400.",
            )

        # With --expect the clock starts here: the tap and its settle pause
        # are paid for from the same deadline as the wait that follows.
        deadline = Deadline(args.timeout) if args.expect is not None else None
        success, message = navigator.tap_at(x, y, deadline)
        if not success:
            cli.fail(message, _tap_remedy(message))
        if deadline is not None:
            wait = _watch_after_tap(navigator, deadline, args, message, [x, y])
            _report_wait(wait, args, tapped=message, tapped_at=[x, y])
        if args.json:
            print(
                json_lib.dumps({"success": True, "message": message, "tapped_at": [x, y]}, indent=2)
            )
        else:
            print(message)
        sys.exit(0)

    # Resolve search text: --find-exact forces exact matching; --find-text is
    # fuzzy unless --exact is also passed. --find-exact takes precedence.
    if args.find_exact is not None:
        search_text = args.find_exact
        fuzzy = False
    else:
        search_text = args.find_text
        fuzzy = not args.exact

    # Find element mode. Every path produces a ScrollSearch so the reporting
    # below is identical: the difference is only whether anything moved.
    deadline = None
    if args.expect is not None:
        # The clock starts before the screen is read to find the target. That
        # read goes through the waiter too, so it is bounded by the deadline
        # and not by the capture layer's own three attempts of sixty seconds.
        deadline = Deadline(args.timeout)
        reading = navigator.watch(deadline, progress=_wait_progress(args))
        if not reading.found:
            cli.fail(
                _wait_error(reading, to_find_target=True),
                UNREADABLE_REMEDY,
                details={"wait": _wait_json(reading)},
            )
        search = ScrollSearch(
            element=navigator._find_in(
                reading.root,
                text=search_text,
                element_type=args.find_type,
                resource_id=args.find_id,
                index=args.index,
                fuzzy=fuzzy,
            ),
            screens_searched=1,
            scrolls=0,
            scrollable=navigator.screen_scrolls(),
        )
    elif args.scroll_to_find:
        search = navigator.find_element_scrolling(
            text=search_text,
            element_type=args.find_type,
            resource_id=args.find_id,
            index=args.index,
            fuzzy=fuzzy,
            direction=args.scroll_direction,
            max_scrolls=args.max_scrolls,
            # Always reported, not only under --verbose: a scroll search can
            # take a minute or more, and a silent minute is indistinguishable
            # from a hang. It goes to stderr, so nothing parsing stdout sees it.
            progress=lambda line: print(f"  {line}", file=sys.stderr),
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
        # Every failure here goes to common.cli. The text form used to be the
        # message alone on stdout, and the JSON `{"error": ...}` with no remedy;
        # the search detail still rides in the object (cli.fail's details).
        message = f"Not found: {', '.join(criteria)} ({detail})"
        cli.fail(message, NOT_FOUND_REMEDY, details={"search": _search_json(search)})

    # A name that matched only a passive label is not an answer.
    #
    # Ownership is structural (`_owning_control`), so a caption inside or beside
    # a control resolves to that control. When nothing tappable owns it, the
    # match is a label and nothing else: tapping it does nothing, and reporting
    # it as found invites exactly that tap. This is the safety catch that lets
    # ownership ignore what the caption says (INC1-04) -- and the reason a
    # scrollable-only ancestor is not allowed to stand in for one.
    if search_text and not (args.find_type or args.find_id) and not element.interactive:
        cli.fail(
            f'Not actionable: "{element.label}" is a passive label with no control -- '
            f"nothing on this screen that answers to that name can be tapped. Run "
            f"screen_mapper.py to see the controls, or give coordinates with --tap-at x,y.",
            "Run screen_mapper.py to see the controls, or give coordinates with --tap-at x,y.",
            details={"search": _search_json(search)},
        )

    # Perform action on found element. Its bounds come from the screen as it
    # is now -- after any scrolling -- so a tap lands where the element ended
    # up rather than where it started.
    if args.tap:
        success, message = navigator.tap(element, deadline)
    elif args.enter_text:
        success, message = navigator.enter_text(element, args.enter_text)
    elif element.center is None:
        # Reporting a match this skill cannot point at is still an answer, but
        # it is not a successful one: the agent's next step would be a tap.
        success = False
        message = _NO_BOUNDS_MESSAGE.format(description=element.description)
    else:
        # Just found the element, report it
        x, y = element.center
        success = True
        message = f"Found: {element.description} at ({x}, {y})"

    if search.scrolls:
        message = f"{message} ({search.detail})"
    if not success:
        cli.fail(message, _tap_remedy(message), details={"search": _search_json(search)})
    if deadline is not None:
        # One tap was sent. What is left of the deadline is the wait's.
        tapped_at = list(element.center)
        wait = _watch_after_tap(navigator, deadline, args, message, tapped_at)
        _report_wait(wait, args, tapped=message, tapped_at=tapped_at)
    if args.verbose:
        print(
            f"  bounds={element.bounds} clickable={element.clickable} "
            f"screens_searched={search.screens_searched} scrolls={search.scrolls}",
            file=sys.stderr,
        )

    if args.json:
        result = {
            "success": True,
            "message": message,
            "element": {
                "type": element.type,
                "label": element.label,
                "bounds": element.bounds,
                "center": element.center,
            },
            "search": _search_json(search),
        }
        if args.tap or args.enter_text:
            # Where the tap actually went. Without this a caller checking that a
            # name landed on the right control has only navigator's own prose to
            # go on, which is the one source that cannot disagree with it.
            result["tapped_at"] = list(element.center)
        print(json_lib.dumps(result, indent=2))
    else:
        print(message)

    sys.exit(0)


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
        "hit_search_deadline": search.hit_deadline,
        "scroll_failure": search.failure,
        "detail": search.detail,
    }


if __name__ == "__main__":
    cli.run(main)
