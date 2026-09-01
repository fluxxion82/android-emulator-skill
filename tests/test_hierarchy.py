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
    """uiautomator prints its own line; the XML must be extracted from it."""
    payload = _recorded("uiautomator_compose_default") + "\nUI hierchary dumped to: /dev/tty"
    fake_dump(payload)
    assert hierarchy.capture_hierarchy("emulator-5554").tag == "hierarchy"


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


def test_element_to_dict_matches_the_documented_contract():
    """CLAUDE.md: {"tag", "attributes": {...}, "children": [...]}, all strings."""
    root = ET.fromstring(
        '<hierarchy rotation="0"><node class="android.widget.Button" text="Go" '
        'clickable="true" bounds="[0,0][10,10]"><node class="X" text="child"/></node></hierarchy>'
    )
    converted = hierarchy.element_to_dict(root)

    assert set(converted) == {"tag", "attributes", "children"}
    button = converted["children"][0]
    assert button["attributes"]["class"] == "android.widget.Button"
    assert button["attributes"]["clickable"] == "true", "attribute values stay strings"
    assert button["children"][0]["attributes"]["text"] == "child"


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
