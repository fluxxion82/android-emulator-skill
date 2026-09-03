"""One way to capture the UI hierarchy.

Three implementations existed — `common.device_utils.get_ui_hierarchy`,
`screen_mapper.get_ui_hierarchy` and `navigator.get_ui_hierarchy` — each dumping
to a fixed device path and pulling to a fixed host path:

    /sdcard/window_dump.xml -> /tmp/window_dump.xml
    /sdcard/window_dump.xml -> /tmp/android_window_dump.xml
    /sdcard/window_dump.xml -> /tmp/android_navigator_dump.xml

Two concurrent invocations, or one run against two devices, silently read each
other's screen (R4). Parallel agents make that the normal case, not an edge one.

`adb exec-out uiautomator dump /dev/tty` removes the problem structurally rather
than working around it: no device file and no host file means no path to collide
on. It also has to be `exec-out` — over `adb shell` the device allocates a pty
and uiautomator writes only its status line, so the XML never reaches the host.
That was measured, not assumed.

The three implementations also returned two different shapes (a dict, and an
`ET.Element`), so callers could not be moved between them.
"""

from __future__ import annotations

import ast
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from common import adb_exec, hierarchy

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "emulator-api35"
SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "android-emulator-skill"
    / "skills"
    / "android-emulator-skill"
    / "scripts"
)


@pytest.fixture
def fake_dump(monkeypatch):
    """Serve a recorded dump in place of a device, capturing the command."""
    calls: list[list[str]] = []

    def _install(*payloads: str, returncode: int = 0):
        queue = list(payloads)

        def _run(cmd, **_kwargs):
            calls.append(cmd)
            out = queue.pop(0) if queue else (payloads[-1] if payloads else "")
            return subprocess.CompletedProcess(cmd, returncode, out, "")

        monkeypatch.setattr(adb_exec.subprocess, "run", _run)
        return calls

    _install.calls = calls
    return _install


def _recorded(name: str) -> str:
    return (RECORDED / f"{name}.xml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# No temp files, on either side.
# ---------------------------------------------------------------------------


def test_capture_uses_exec_out_not_shell(fake_dump):
    """Over `adb shell` the XML never reaches the host; only the status line does."""
    fake_dump(_recorded("uiautomator_compose_default"))
    hierarchy.capture_hierarchy("emulator-5554")

    command = fake_dump.calls[-1]
    assert "exec-out" in command, f"not using exec-out: {command}"
    assert "shell" not in command


def test_capture_writes_no_file_on_the_device_or_the_host(fake_dump):
    """The R4 collision cannot happen if there is no path to collide on."""
    fake_dump(_recorded("uiautomator_compose_default"))
    hierarchy.capture_hierarchy("emulator-5554")

    joined = " ".join(fake_dump.calls[-1])
    assert "/sdcard" not in joined, f"still writing to the device: {joined}"
    assert "/tmp" not in joined, f"still writing to the host: {joined}"
    assert "pull" not in joined, "no pull is needed when the dump comes back on stdout"


def _docstrings(tree: ast.AST) -> set[int]:
    """Line numbers of docstring constants, so prose is not mistaken for code."""
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                lines.add(node.body[0].lineno)
    return lines


def test_no_script_still_pulls_a_dump_to_a_fixed_path():
    """The three hand-rolled implementations must be gone, not merely bypassed.

    Parsed rather than grepped, and docstrings excluded: the migrated modules
    now *explain* the old ``/tmp/window_dump.xml`` path in their docstrings, and
    a substring search cannot tell an explanation from a use. That mistake has
    been made repeatedly in this repo -- a guard that fires on the documentation
    of its own fix.
    """
    offenders = []
    for path in SCRIPTS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc_lines = _docstrings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.lineno in doc_lines:
                continue
            if "window_dump" in node.value or "android_navigator_dump" in node.value:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        f"{offenders} still use a fixed dump path as a value; capture goes "
        f"through common.hierarchy, which writes no files at all"
    )


# ---------------------------------------------------------------------------
# Parsing real output.
# ---------------------------------------------------------------------------


def test_capture_returns_the_parsed_root(fake_dump):
    fake_dump(_recorded("uiautomator_compose_default"))
    root = hierarchy.capture_hierarchy("emulator-5554")
    assert root.tag == "hierarchy"
    assert len(list(root.iter("node"))) == 32


def test_capture_tolerates_the_status_line_around_the_xml(fake_dump):
    """uiautomator prints its own line; the XML must be extracted from it.

    Reads the raw dump rather than appending the status line by hand. Written
    by hand it was `xml + "\\nUI hierchary dumped to: /dev/tty"` -- and the
    device emits **no newline** there, appending the status line straight onto
    `</hierarchy>`. Small, but it is guessed tool output in the one repo whose
    defining bug is guessed tool output.
    """
    fake_dump(_recorded("uiautomator_dump_raw"))
    assert hierarchy.capture_hierarchy("emulator-5554").tag == "hierarchy"


def test_the_recorded_dump_really_has_no_newline_before_its_status_line():
    """Pins the premise of the test above, so it cannot silently stop holding."""
    raw = _recorded("uiautomator_dump_raw")
    assert (
        "</hierarchy>UI hierchary dumped to:" in raw
    ), "the status line is no longer glued to the XML; re-check the extraction"


def test_a_dump_with_no_xml_is_an_error_not_an_empty_tree(fake_dump):
    """Returning an empty hierarchy would read as 'the screen has nothing on it'."""
    fake_dump("UI hierchary dumped to: /dev/tty")
    with pytest.raises(hierarchy.HierarchyError):
        hierarchy.capture_hierarchy("emulator-5554")


# ---------------------------------------------------------------------------
# The idle-state failure is transient and must be retried.
# ---------------------------------------------------------------------------


def test_capture_retries_when_the_ui_is_not_idle(fake_dump):
    """`ERROR: could not get idle state` is the common animating-screen failure.

    Every script that reads the screen was flaky because of it; a single retry
    turns most of those into a success.
    """
    fake_dump(
        "ERROR: could not get idle state.",
        _recorded("uiautomator_compose_default"),
    )
    root = hierarchy.capture_hierarchy("emulator-5554")
    assert root.tag == "hierarchy"
    assert len(fake_dump.calls) == 2, "did not retry a transient idle-state failure"


def test_capture_gives_up_with_an_actionable_error(fake_dump):
    fake_dump("ERROR: could not get idle state.")
    with pytest.raises(hierarchy.HierarchyError) as excinfo:
        hierarchy.capture_hierarchy("emulator-5554", retries=2)

    message = str(excinfo.value)
    assert "idle" in message.lower()
    assert "animat" in message.lower(), "the error should say what to do about it"


# ---------------------------------------------------------------------------
# One shape, documented.
# ---------------------------------------------------------------------------


def test_element_to_dict_matches_the_documented_contract(recorded):
    """CLAUDE.md: {"tag", "attributes": {...}, "children": [...]}, all strings.

    Read off a recorded dump rather than a two-node sample: the contract is
    about what a real screen converts to, and the recorded EditText carries its
    caption in a child, which is the nesting the shape has to survive.
    """
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    converted = hierarchy.element_to_dict(root)

    assert set(converted) == {"tag", "attributes", "children"}
    assert converted["tag"] == "hierarchy"

    def _walk(node):
        yield node
        for child in node["children"]:
            yield from _walk(child)

    nodes = list(_walk(converted))
    assert all(set(n) == {"tag", "attributes", "children"} for n in nodes)
    assert all(
        isinstance(value, str) for n in nodes for value in n["attributes"].values()
    ), "attribute values must stay strings"

    field = next(n for n in nodes if n["attributes"].get("class", "").endswith(".EditText"))
    assert field["attributes"]["clickable"] == "true"
    captions = [c["attributes"].get("text") for c in field["children"]]
    assert "Email address" in captions, captions


def test_dict_and_element_capture_describe_the_same_tree(fake_dump):
    fake_dump(_recorded("uiautomator_compose_default"), _recorded("uiautomator_compose_default"))

    element = hierarchy.capture_hierarchy("emulator-5554")
    as_dict = hierarchy.capture_hierarchy_dict("emulator-5554")

    def count(node: dict) -> int:
        return 1 + sum(count(child) for child in node["children"])

    assert count(as_dict) == len(list(element.iter()))


# ---------------------------------------------------------------------------
# Everyone uses it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["screen_mapper.py", "navigator.py", "common/device_utils.py"])
def test_capture_is_not_reimplemented(module: str):
    """Each of these had its own dump-and-pull; none should build one now."""
    tree = ast.parse((SCRIPTS / module).read_text(encoding="utf-8"), filename=module)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [a.value for a in node.args if isinstance(a, ast.Constant)]
        assert "uiautomator" not in args, (
            f"{module} still issues its own uiautomator dump at line {node.lineno}; "
            f"call common.hierarchy.capture_hierarchy instead"
        )


def test_a_null_root_node_is_retried(fake_dump):
    """The transient failure that only CI produced.

    `ERROR: null root node returned by UiTestAutomationBridge.` means no window
    was focused yet -- the app had launched but had not drawn. A developer
    machine rarely sees it because the screen is already settled; a headless
    runner sees it readily, and it failed the end-to-end agent test there while
    every local run passed.

    Retried for the same reason as the idle error: waiting is the entire fix.
    """
    fake_dump(
        "ERROR: null root node returned by UiTestAutomationBridge.",
        _recorded("uiautomator_compose_default"),
    )
    assert hierarchy.capture_hierarchy("emulator-5554").tag == "hierarchy"
    assert len(fake_dump.calls) == 2, "a null root node was not retried"


def test_a_persistent_null_root_node_says_what_to_check(fake_dump):
    """Giving up must still be actionable, and must not blame the wrong thing."""
    fake_dump("ERROR: null root node returned by UiTestAutomationBridge.")

    with pytest.raises(hierarchy.HierarchyError) as excinfo:
        hierarchy.capture_hierarchy("emulator-5554", retries=2)

    message = str(excinfo.value)
    assert "mCurrentFocus" in message, "the error does not say how to check the focus"
    assert "animat" not in message.lower(), (
        "a null root node is being reported as the idle-state/animation failure, "
        "which sends the reader after the wrong cause"
    )


# ---------------------------------------------------------------------------
# The two questions every consumer of a dump asks (C5 / C7)
# ---------------------------------------------------------------------------
#
# `parse_bounds` and `is_interactive` replaced three bounds grammars and two
# eligibility rules spread across navigator, screen_mapper and
# accessibility_audit. Their tests live here, with the implementation, rather
# than once per consumer.


def test_bounds_are_read_off_the_recorded_screen(recorded):
    """Every node in a real dump parses, and to the numbers the dump states."""
    root = ET.fromstring(recorded.text("uiautomator_settings_top"))
    boxes = {
        node.get("bounds"): hierarchy.parse_bounds(node.get("bounds"))
        for node in root.iter()
        if node.get("bounds")
    }

    assert boxes, "the recorded screen carries no bounds at all"
    unparsed = [raw for raw, box in boxes.items() if box is None]
    assert not unparsed, f"the shared grammar cannot read a recorded value: {unparsed}"
    assert boxes["[0,142][1080,2361]"] == (0, 142, 1080, 2361)


def test_an_unreadable_bounds_value_is_none_and_not_a_rectangle():
    """None means "unknown"; `(0, 0, 0, 0)` would mean "the top-left corner".

    The distinction is the whole of C5. Two of the three grammars this replaced
    returned the corner for anything they could not read, and navigator duly
    offered it as a tappable point.
    """
    assert hierarchy.parse_bounds("") is None
    assert hierarchy.parse_bounds(None) is None
    assert hierarchy.parse_bounds("not-bounds") is None
    assert hierarchy.parse_bounds("[33,754]") is None, "half a rectangle is not a rectangle"


def test_the_grammar_is_signed():
    """Kept as precaution, not as observation.

    uiautomator on API 35 clips every rectangle to the display -- eight recipes
    for an off-screen node all came back clipped, so no recorded dump has a
    negative bound. The signed grammar is retained because
    `accessibility_audit`'s already was, because older API levels are not known
    to clip, and because it costs nothing.
    """
    assert hierarchy.parse_bounds("[-12,-4][200,150]") == (-12, -4, 200, 150)


def test_a_control_is_eligible_by_its_properties_not_its_class(recorded):
    """Compose renders controls as `android.view.View`; a class whitelist misses them."""
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    controls = [node for node in root.iter() if hierarchy.is_interactive(node)]

    assert len(controls) == 7, f"expected the fixture's seven controls, got {len(controls)}"
    assert any(
        node.get("class") == "android.view.View" for node in controls
    ), "no plain View was found eligible, which is R11 all over again"


def test_a_collapsed_rectangle_is_not_operable(recorded):
    """The recorded Settings screen ends with `[0,2401][1080,2361]` -- bottom above top.

    uiautomator emits no visibility attribute, so a collapsed rectangle is the
    only signal that a flagged node cannot be touched.
    """
    root = ET.fromstring(recorded.text("uiautomator_settings_top"))
    collapsed = [node for node in root.iter() if node.get("bounds") == "[0,2401][1080,2361]"]

    assert collapsed, "the fixture no longer carries a collapsed row; find another"
    assert collapsed[0].get("clickable") == "true", "the row is flagged clickable"
    assert not hierarchy.is_interactive(collapsed[0])


def test_eligibility_reads_the_dict_shape_too(recorded):
    """accessibility_audit holds the hierarchy as dicts, and asks the same rule."""
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    checkbox = next(node for node in root.iter() if node.get("bounds") == "[33,754][159,880]")

    assert hierarchy.is_interactive(checkbox)
    assert hierarchy.is_interactive(hierarchy.element_to_dict(checkbox))


def test_focusable_alone_does_not_make_a_control(recorded):
    """Focusable containers are everywhere; counting them reports the screen as one control."""
    root = ET.fromstring(recorded.text("uiautomator_settings_top"))
    focusable_only = [
        node
        for node in root.iter()
        if node.get("focusable") == "true"
        and not any(node.get(name) == "true" for name in hierarchy.INTERACTIVE_ATTRIBUTES)
    ]

    assert focusable_only, "the fixture no longer has a focusable non-control"
    assert not any(hierarchy.is_interactive(node) for node in focusable_only)


def test_a_disabled_control_is_not_operable(recorded):
    """A recorded control with ONE attribute flipped.

    No recorded screen carries `enabled="false"`: the recorder captured live
    screens, and a disabled control is a state an app has to be driven into.
    So the case is made by changing that one attribute on a real node -- every
    other byte is the device's -- rather than by inventing a dump.
    """
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    checkbox = next(node for node in root.iter() if node.get("bounds") == "[33,754][159,880]")
    assert hierarchy.is_interactive(checkbox), "the recorded control is operable to begin with"

    checkbox.set("enabled", "false")
    assert not hierarchy.is_interactive(checkbox)
