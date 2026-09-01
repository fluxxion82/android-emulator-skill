#!/usr/bin/env python3
"""One way to capture the device's UI hierarchy.

Three implementations used to exist -- in ``device_utils``, ``screen_mapper``
and ``navigator`` -- each dumping to a fixed device path and pulling to a fixed
host path::

    /sdcard/window_dump.xml -> /tmp/window_dump.xml
    /sdcard/window_dump.xml -> /tmp/android_window_dump.xml
    /sdcard/window_dump.xml -> /tmp/android_navigator_dump.xml

Two concurrent invocations, or one run against two devices, silently read each
other's screen (defect R4). With parallel agents that is the normal case rather
than an edge one. They also returned two different shapes, so callers could not
move between them.

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
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from .adb_exec import AdbError, run_adb
from .env_config import env_int

# uiautomator waits for the UI to go idle before dumping, which on a busy or
# animating screen takes considerably longer than an ordinary adb command.
CAPTURE_TIMEOUT = env_int("ANDROID_EMU_UI_DUMP_TIMEOUT", 60, min_value=5)

# Attempts, not retries: 1 means try once and do not retry.
CAPTURE_ATTEMPTS = env_int("ANDROID_EMU_UI_DUMP_ATTEMPTS", 3, min_value=1)

# Pause between attempts, long enough for a short animation to finish.
RETRY_DELAY_SECONDS = 0.5

_IDLE_ERROR = "could not get idle state"


class HierarchyError(RuntimeError):
    """The UI hierarchy could not be captured or parsed.

    Subclasses RuntimeError so the CLI boundaries that already catch it keep
    working, consistent with :class:`common.adb_exec.AdbError`.
    """


def _extract_xml(payload: str) -> str | None:
    """Pull the XML document out of uiautomator's stdout.

    uiautomator prints its own status line alongside the document, and with
    ``exec-out`` both arrive together.
    """
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
    display: int | None = None,
) -> ET.Element:
    """Capture the current screen's UI hierarchy.

    Args:
        serial: Device serial; the default device is used when None.
        timeout: Seconds to allow per attempt. Defaults to
            ``ANDROID_EMU_UI_DUMP_TIMEOUT`` (60).
        retries: Total attempts, not retries. Defaults to
            ``ANDROID_EMU_UI_DUMP_ATTEMPTS`` (3). A dump fails transiently while
            the screen is animating.
        display: Display id for a multi-display device. Omitted when None,
            which targets the default display.

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

    args: list[object] = ["uiautomator", "dump"]
    if display is not None:
        args.extend(["--display", display])
    args.append("/dev/tty")

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
        if _IDLE_ERROR not in last_payload.lower():
            break
        if attempt < attempts - 1:
            time.sleep(RETRY_DELAY_SECONDS)

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
    Consumers parse their own types -- ``bounds`` is ``"[l,t][r,b]"`` and
    booleans are ``"true"``/``"false"`` -- because silently coercing them is how
    a caller ends up reading a field that is not there.

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
    "AdbError",
    "HierarchyError",
    "capture_hierarchy",
    "capture_hierarchy_dict",
    "element_to_dict",
]
