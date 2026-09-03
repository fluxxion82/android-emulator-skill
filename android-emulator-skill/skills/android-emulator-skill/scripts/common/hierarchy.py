#!/usr/bin/env python3
"""One way to capture the device's UI hierarchy.

Three implementations used to exist -- in ``device_utils``, ``screen_mapper``
and ``navigator`` -- each dumping to ``/sdcard/window_dump.xml`` and pulling to
its own fixed host path under ``/tmp``. Two concurrent invocations, or one run
against two devices, silently read each other's screen (defect R4); with
parallel agents that is the normal case. They also returned two different
shapes, so callers could not move between them.

Two things make this module the single implementation:

- **No files.** ``adb exec-out uiautomator dump /dev/tty`` returns the XML on
  stdout, so there is no path to collide on. The collision is removed rather
  than worked around with unique names.
- **``exec-out``, not ``shell``.** Over ``adb shell`` the device allocates a pty
  and uiautomator writes only its status line ("UI hierchary dumped to:
  /dev/tty" -- Android's typo, not ours); the XML never reaches the host. This
  was measured, and it is why the older implementations needed the file.

The dump also fails transiently while the screen is animating, with
``ERROR: could not get idle state``. Every script that reads the screen was
flaky because of it, so a bounded retry lives here instead of in each caller.

Two questions every consumer of a dump asks -- *where is this node* and *can it
be operated* -- are answered here too, by :func:`parse_bounds` and
:func:`is_interactive`. They used to be answered three and two times
respectively, in navigator, screen_mapper and accessibility_audit, and the
copies disagreed: two of the three bounds grammars rejected the negative
coordinate a partially off-screen view reports and silently returned
``(0, 0, 0, 0)``, which is a tappable point at the top-left corner (C5/C7).
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from .adb_exec import AdbError, run_adb
from .env_config import env_int

# uiautomator waits for the UI to go idle before dumping, which on a busy screen
# takes considerably longer than an ordinary adb command.
CAPTURE_TIMEOUT = env_int("ANDROID_EMU_UI_DUMP_TIMEOUT", 60, min_value=5)

# Attempts, not retries: 1 means try once and do not retry.
CAPTURE_ATTEMPTS = env_int("ANDROID_EMU_UI_DUMP_ATTEMPTS", 3, min_value=1)

# Pause between attempts, long enough for a short animation to finish.
RETRY_DELAY_SECONDS = 0.5

_IDLE_ERROR = "could not get idle state"

# The other transient uiautomator failure, and the one that only showed up in
# CI: `ERROR: null root node returned by UiTestAutomationBridge.` It means no
# window was focused yet -- the app had been launched but had not drawn. A
# developer machine rarely produces it because the screen is already settled;
# a headless runner produces it readily. Retried for the same reason as the
# idle error: waiting a moment is the whole fix.
_NO_ROOT_ERROR = "null root node"

_TRANSIENT_ERRORS = (_IDLE_ERROR, _NO_ROOT_ERROR)

# `bounds="[left,top][right,bottom]"`. The coordinates are SIGNED: a view scrolled
# half off the left edge, or one laid out above the status bar, reports a
# negative left or top. The two grammars this replaces used `\d+`, so they did
# not match such a node at all and their callers fell back to `(0, 0, 0, 0)` --
# a rectangle whose centre is a real, tappable pixel (C5).
_BOUNDS_PATTERN = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# The four properties by which uiautomator says an element can be operated.
# `focusable` is deliberately absent: focusable containers are everywhere, and
# including it reports the whole screen as interactive.
INTERACTIVE_ATTRIBUTES = ("clickable", "long-clickable", "checkable", "scrollable")


def _is_transient(payload: str) -> bool:
    """Whether a failed dump is worth retrying."""
    lowered = payload.lower()
    return any(marker in lowered for marker in _TRANSIENT_ERRORS)


class HierarchyError(RuntimeError):
    """The UI hierarchy could not be captured or parsed.

    Subclasses RuntimeError so the CLI boundaries that already catch it keep
    working, as :class:`common.adb_exec.AdbError` does.
    """


def parse_bounds(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse a uiautomator ``bounds`` string into ``(left, top, right, bottom)``.

    The one grammar. Signed, because a partially off-screen view reports a
    negative coordinate, and returning None rather than a zero rectangle,
    because ``(0, 0, 0, 0)`` is indistinguishable from a real element in the
    corner: every caller that treated it as one issued ``input tap 0 0``.

    Args:
        value: The raw ``bounds`` attribute, e.g. ``"[0,142][1080,2361]"``.

    Returns:
        The four coordinates, or None when the value is absent or malformed.
        None means "unknown", and a caller that cannot act without coordinates
        must refuse rather than substitute any.

    Example:
        >>> parse_bounds("[-12,50][200,150]")
        (-12, 50, 200, 150)
        >>> parse_bounds("not-bounds") is None
        True
    """
    match = _BOUNDS_PATTERN.match(value or "")
    if match is None:
        return None
    left, top, right, bottom = (int(group) for group in match.groups())
    return (left, top, right, bottom)


def node_attributes(node: ET.Element | Mapping) -> Mapping[str, str]:
    """The raw attribute mapping of a hierarchy node, whatever shape it arrives in.

    Three shapes are in use across the skill and all three reach these
    predicates: the parsed ``ET.Element`` (navigator, screen_mapper), the
    documented dict shape ``{"tag", "attributes", "children"}`` that
    ``get_ui_hierarchy`` returns (accessibility_audit), and a bare attribute
    mapping. Normalising here is what lets one eligibility rule serve all of
    them.

    Args:
        node: An element, a hierarchy dict, or an attribute mapping.

    Returns:
        The node's attributes, with uiautomator's string values unchanged.
    """
    if isinstance(node, ET.Element):
        return node.attrib
    if isinstance(node, Mapping):
        attributes = node.get("attributes")
        return attributes if isinstance(attributes, Mapping) else node
    raise TypeError(f"not a hierarchy node: {type(node).__name__}")


def is_interactive(node: ET.Element | Mapping) -> bool:
    """Whether an agent can operate this node.

    The one eligibility rule, and it is decided by PROPERTIES, not class names:
    Jetpack Compose renders its controls as plain ``android.view.View``, so a
    whitelist of widget classes matches almost nothing on a Compose screen
    (defect R11).

    Three conditions, all required:

    - ``enabled`` -- a disabled control does nothing when tapped.
    - a rectangle that is not collapsed. uiautomator emits no visibility
      attribute, so a zero or negative area is the only signal that a flagged
      node cannot be touched; the recorded Settings dump ends with exactly such
      a row, ``[0,2401][1080,2361]``. A node with no parseable bounds is *not*
      excluded here -- that is a missing signal, not a collapsed rectangle -- but
      nothing can be tapped on it either, so acting on it is refused separately.
    - at least one of :data:`INTERACTIVE_ATTRIBUTES`.

    Args:
        node: An element, a hierarchy dict, or an attribute mapping.

    Returns:
        True when the node is a control an agent can act on.
    """
    attributes = node_attributes(node)
    if attributes.get("enabled", "true") != "true":
        return False

    box = parse_bounds(attributes.get("bounds"))
    if box is not None and (box[2] <= box[0] or box[3] <= box[1]):
        return False

    return any(attributes.get(name, "false") == "true" for name in INTERACTIVE_ATTRIBUTES)


def _extract_xml(payload: str) -> str | None:
    """Pull the XML document out of uiautomator's stdout (the status line comes too)."""
    start = payload.find("<?xml")
    if start == -1:
        start = payload.find("<hierarchy")
    if start == -1:
        return None
    end = payload.rfind("</hierarchy>")
    if end == -1:
        return None
    return payload[start : end + len("</hierarchy>")]


def capture_hierarchy(
    serial: str | None = None,
    *,
    timeout: int | None = None,
    retries: int | None = None,
) -> ET.Element:
    """Capture the current screen's UI hierarchy.

    Args:
        serial: Device serial; the default device is used when None.
        timeout: Seconds to allow per attempt. Defaults to
            ``ANDROID_EMU_UI_DUMP_TIMEOUT`` (60).
        retries: Total attempts, not retries. Defaults to
            ``ANDROID_EMU_UI_DUMP_ATTEMPTS`` (3). A dump fails transiently while
            the screen is animating.

    Returns:
        The parsed ``<hierarchy>`` root element.

    Raises:
        HierarchyError: If no dump could be obtained or it could not be parsed.
        AdbError: For device-level failures, which carry their own remedy.

    Example:
        >>> root = capture_hierarchy("emulator-5554")
        >>> root.tag
        'hierarchy'
    """
    attempts = CAPTURE_ATTEMPTS if retries is None else max(1, retries)
    budget = CAPTURE_TIMEOUT if timeout is None else timeout

    # No `--display`: the parameter existed, was never passed by any caller, and
    # was never recorded against a device, so the one thing nobody could say was
    # whether it worked (C11). Deleted rather than left as an untested option.
    args: list[object] = ["uiautomator", "dump", "/dev/tty"]

    last_payload = ""
    for attempt in range(attempts):
        result = run_adb("exec-out", serial, *args, timeout=budget)
        last_payload = result.output

        document = _extract_xml(last_payload)
        if document is not None:
            try:
                return ET.fromstring(document)
            except ET.ParseError as exc:
                raise HierarchyError(
                    f"uiautomator returned a document that would not parse: {exc}"
                ) from exc

        # Only the idle-state failure is worth retrying; anything else will not
        # fix itself and retrying just delays the error.
        if not _is_transient(last_payload):
            break
        if attempt < attempts - 1:
            time.sleep(RETRY_DELAY_SECONDS)

    if _NO_ROOT_ERROR in last_payload.lower():
        raise HierarchyError(
            f"uiautomator found no focused window after {attempts} attempts "
            f'("null root node returned by UiTestAutomationBridge"). Nothing is '
            f"drawn yet, or the display is off: launch the app first, and check "
            f"`adb shell dumpsys window | grep mCurrentFocus`."
        )
    if _IDLE_ERROR in last_payload.lower():
        raise HierarchyError(
            f"uiautomator could not get an idle state after {attempts} attempts. "
            f"The screen is still animating; wait for it to settle, or disable "
            f"animations (window_animation_scale / transition_animation_scale / "
            f"animator_duration_scale set to 0)."
        )
    raise HierarchyError(f"No UI hierarchy in uiautomator output: {last_payload.strip()[:200]!r}")


def element_to_dict(element: ET.Element) -> dict:
    """Convert a hierarchy element to the documented dict shape.

    The contract, per CLAUDE.md: ``{"tag": str, "attributes": {...}, "children":
    [...]}``, with every attribute value left as the string uiautomator emitted.
    Consumers parse their own types -- ``bounds`` is ``"[l,t][r,b]"``, booleans
    are ``"true"``/``"false"``.

    Args:
        element: A parsed hierarchy element.

    Returns:
        The nested dict form.
    """
    return {
        "tag": element.tag,
        "attributes": dict(element.attrib),
        "children": [element_to_dict(child) for child in element],
    }


def capture_hierarchy_dict(serial: str | None = None, **kwargs) -> dict:
    """Capture the hierarchy and return it in the documented dict shape.

    Args:
        serial: Device serial; the default device is used when None.
        **kwargs: Passed through to :func:`capture_hierarchy`.

    Returns:
        The hierarchy as nested dicts.
    """
    return element_to_dict(capture_hierarchy(serial, **kwargs))


__all__ = [
    "INTERACTIVE_ATTRIBUTES",
    "AdbError",
    "HierarchyError",
    "capture_hierarchy",
    "capture_hierarchy_dict",
    "element_to_dict",
    "is_interactive",
    "node_attributes",
    "parse_bounds",
]
