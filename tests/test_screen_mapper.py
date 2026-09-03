"""Unit tests for screen_mapper feature deltas (device-free).

Covers the two curated deltas:
1. Env-configurable preview limits (ANDROID_EMU_SCREEN_BUTTONS_PREVIEW /
   ANDROID_EMU_SCREEN_SECTION_ITEMS) drive truncation in the summary/verbose
   output.
2. Secure/password fields (attribute ``password="true"``) are counted and
   reported separately.

All logic here is pure (XML -> analysis dict -> formatted string), so no adb or
emulator is required. The one place subprocess would be touched
(``get_ui_hierarchy``) is exercised by monkeypatching the subprocess call under
``common.adb_exec`` to prove command construction never shells out to a device.

The dump now comes back on stdout (``adb exec-out uiautomator dump /dev/tty``),
so the fake supplies the XML as the command's output. It used to arrive via a
pulled file, which is why these tests once patched ``ET.parse``.

screen_mapper reaches adb only through ``adb_exec.run_adb`` now, so the fake
goes there; patching ``screen_mapper.subprocess`` would stop intercepting and
let these tests dump a real device's screen.

**Every hierarchy here is a recorded one** (T2). It used to be four
hand-written ``<hierarchy>`` blocks, and `analyze_tree` matched neither of the
fixture policy's name rules, so the file testing "see the screen" was outside
the policy entirely. The dumps used, and why:

- ``uiautomator_compose_default`` — a Compose screen with no
  testTagsAsResourceId: one EditText, one CheckBox, five nodes bucketed as
  Control and no ``android.widget.Button`` at all.
- ``uiautomator_dialer_keypad`` — an AOSP-widget screen that does have Buttons
  (three of them), which is what the preview-truncation deltas need.

Where a scenario appears in no recording (a ``password="true"`` field; a
zero-area node), the test derives it by editing ONE attribute of a recorded
dump with ElementTree, and says so. The base is always ground truth.
"""

from __future__ import annotations

import copy
import json as json_lib
import re
import xml.etree.ElementTree as ET

import pytest
import screen_mapper
from screen_mapper import ScreenMapper

from common import adb_exec


def _fake_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Stand-in for subprocess.CompletedProcess."""

    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _analyze(xml: str) -> dict:
    mapper = ScreenMapper()
    return mapper.analyze_tree(ET.fromstring(xml))


def _compose_with_a_password_field(recorded) -> str:
    """The recorded Compose screen, given a SECOND field that is secure.

    No recorded dump has a password field -- the fixture app has no password
    input -- so the login screen it needs is derived from the recorded one.
    Two edits, both on a copy of the recorded EditText node so the whole
    subtree (the caption TextView inside it, the empty resource-id, the
    unmerged semantics wrapper) is what the device dumped:

    1. ``password="true"`` on the copy -- the attribute the delta reads;
    2. the copy's caption changed to "Password" and its bounds moved down by
       its own height, because two fields cannot occupy one rectangle.

    A second field matters: with one field, ``secure_fields`` and
    ``len(edit_texts)`` are both 1, so `secure_fields = len(edit_texts)` would
    pass. Two fields and one secure count makes the two independently
    observable, which is what the original hand-written hierarchy was for.
    """
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    parent = next(node for node in root.iter("node") if _edit_texts(node))
    fields = _edit_texts(parent)
    assert len(fields) == 1, f"fixture no longer has exactly one EditText: {len(fields)}"

    plain = fields[0]
    secure = copy.deepcopy(plain)
    secure.set("password", "true")

    left, top, right, bottom = _bounds(plain)
    height = bottom - top
    _set_bounds(secure, (left, top + height, right, bottom + height))
    for caption in secure.iter("node"):
        if caption.get("text"):
            caption.set("text", "Password")
            _set_bounds(caption, _shifted(_bounds(caption), height))

    parent.insert(list(parent).index(plain) + 1, secure)
    return ET.tostring(root, encoding="unicode")


def _edit_texts(node: ET.Element) -> list[ET.Element]:
    return [child for child in node if child.get("class", "").endswith(".EditText")]


def _bounds(node: ET.Element) -> tuple[int, int, int, int]:
    left, top, right, bottom = re.findall(r"-?\d+", node.get("bounds", ""))
    return int(left), int(top), int(right), int(bottom)


def _shifted(box: tuple[int, int, int, int], dy: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    return left, top + dy, right, bottom + dy


def _set_bounds(node: ET.Element, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    node.set("bounds", f"[{left},{top}][{right},{bottom}]")


# --- Delta 2: secure/password field tracking -------------------------------


def test_secure_field_counted_separately(recorded):
    """Two fields, exactly one secure -- so the counts cannot be the same number."""
    analysis = _analyze(_compose_with_a_password_field(recorded))

    assert len(analysis["edit_texts"]) == 2
    assert analysis["secure_fields"] == 1

    secure = [et for et in analysis["edit_texts"] if et["secure"]]
    plain = [et for et in analysis["edit_texts"] if not et["secure"]]
    assert len(secure) == 1 and len(plain) == 1

    # Compose emits no resource-id, so each label is recovered from that
    # field's own subtree -- the caption TextView sitting inside it.
    assert plain[0]["label"] == "Email address"
    assert secure[0]["label"] == "Password"


def test_no_secure_fields_when_none_marked(recorded):
    """The same screen unmodified: the field is there, the secure count is not."""
    analysis = _analyze(recorded.text("uiautomator_compose_default"))
    assert analysis["edit_texts"], "fixture no longer has a field to count"
    assert analysis["secure_fields"] == 0
    assert all(not et["secure"] for et in analysis["edit_texts"])


def test_summary_reports_secure_count(recorded):
    analysis = _analyze(_compose_with_a_password_field(recorded))
    summary = ScreenMapper().format_summary(analysis)
    assert "EditTexts: 2 (0 filled) [1 secure]" in summary


def test_summary_omits_secure_marker_when_zero(recorded):
    summary = ScreenMapper().format_summary(_analyze(recorded.text("uiautomator_compose_default")))
    assert "secure" not in summary


# --- Delta 1: env-configurable preview limits ------------------------------
#
# The dialer keypad, because it is the recorded screen with the most controls:
# 17 in the `Control` bucket, "Call" in `Button`, and "More options" and
# "backspace" in `ImageButton`. Since C4 the cap applies PER BUCKET -- a screen
# with twenty dial keys must not push the one Button off the report -- so the
# truncation is shown on the bucket that overflows and the completeness on the
# ones that do not.


def test_buttons_preview_limit_truncates(monkeypatch, recorded):
    monkeypatch.setattr(screen_mapper, "BUTTONS_PREVIEW", 2)
    analysis = _analyze(recorded.text("uiautomator_dialer_keypad"))
    assert len(analysis["elements_by_type"]["Control"]) == 17, "fixture changed"

    summary = ScreenMapper().format_summary(analysis)
    controls = next(line for line in summary.splitlines() if line.startswith("Control:"))
    assert controls.count('"') == 4, f"more than two names survived the cap: {controls}"
    assert "(17 total)" in controls, controls
    # The cap is per bucket, so a small bucket keeps all of its names.
    assert 'ImageButton: "More options", "backspace"' in summary


def test_buttons_preview_no_truncation_under_limit(monkeypatch, recorded):
    monkeypatch.setattr(screen_mapper, "BUTTONS_PREVIEW", 20)
    analysis = _analyze(recorded.text("uiautomator_dialer_keypad"))
    summary = ScreenMapper().format_summary(analysis)
    assert "total)" not in summary
    assert 'Button: "Call"' in summary
    assert 'ImageButton: "More options", "backspace"' in summary


def test_section_items_preview_limit_truncates_verbose(monkeypatch, recorded):
    monkeypatch.setattr(screen_mapper, "SECTION_ITEMS_PREVIEW", 2)
    analysis = _analyze(recorded.text("uiautomator_compose_default"))
    # 13 TextViews on this screen, previewed at 2.
    assert len(analysis["elements_by_type"]["TextView"]) == 13
    summary = ScreenMapper().format_summary(analysis, verbose=True)
    assert "... and 11 more" in summary


def test_env_int_overrides_at_import(monkeypatch):
    # The module reads env at import time via env_int; reloading honors override.
    import importlib

    monkeypatch.setenv("ANDROID_EMU_SCREEN_BUTTONS_PREVIEW", "7")
    monkeypatch.setenv("ANDROID_EMU_SCREEN_SECTION_ITEMS", "4")
    reloaded = importlib.reload(screen_mapper)
    try:
        assert reloaded.BUTTONS_PREVIEW == 7
        assert reloaded.SECTION_ITEMS_PREVIEW == 4
    finally:
        monkeypatch.delenv("ANDROID_EMU_SCREEN_BUTTONS_PREVIEW", raising=False)
        monkeypatch.delenv("ANDROID_EMU_SCREEN_SECTION_ITEMS", raising=False)
        importlib.reload(screen_mapper)


# --- Command construction never shells out to a real device ----------------


def _patch_adb(monkeypatch, result_for=None):
    """Intercept every adb call; return (commands, timeouts)."""
    calls: list[list[str]] = []
    budgets: list[object] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        budgets.append(kwargs.get("timeout"))
        # Must be an arg list (never shell=True).
        assert isinstance(cmd, list)
        assert kwargs.get("shell", False) is False
        return _fake_result() if result_for is None else result_for(cmd)

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls, budgets


def _dump_result_for(xml: str):
    """Serve the hierarchy on stdout, the way `exec-out` delivers it."""

    def _serve(_cmd):
        return _fake_result(stdout=xml)

    return _serve


def test_get_ui_hierarchy_builds_adb_without_shell(monkeypatch, recorded):
    calls, budgets = _patch_adb(
        monkeypatch, result_for=_dump_result_for(_compose_with_a_password_field(recorded))
    )

    root = ScreenMapper(serial="emulator-5554").get_ui_hierarchy()
    analysis = ScreenMapper().analyze_tree(root)
    assert analysis["secure_fields"] == 1

    dump_cmd = calls[0]
    assert "adb" in dump_cmd[0]
    assert "uiautomator" in dump_cmd
    assert "dump" in dump_cmd
    assert "emulator-5554" in dump_cmd
    # exec-out, not shell: over `adb shell` the device allocates a pty and only
    # uiautomator's status line comes back, so the XML never reaches the host.
    assert "exec-out" in dump_cmd, f"a pty would swallow the dump: {dump_cmd}"
    # An unbounded call would wedge adb for whatever runs next.
    assert all(b for b in budgets), f"unbounded adb call: {budgets}"


# --- R2: the exit code must carry the outcome ------------------------------


def test_main_exits_non_zero_when_the_screen_cannot_be_read(monkeypatch, capsys):
    """R2: exiting 0 while serialising an error made the status useless."""
    _patch_adb(
        monkeypatch,
        result_for=lambda _cmd: _fake_result(
            returncode=1, stderr="adb: more than one device/emulator\n"
        ),
    )
    monkeypatch.setattr(screen_mapper, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(screen_mapper.sys, "argv", ["screen_mapper.py"])

    with pytest.raises(SystemExit) as exc:
        screen_mapper.main()

    assert exc.value.code != 0, "a failed screen read still reported success"
    out = capsys.readouterr().out
    # Output format is unchanged: still one "Error: ..." line on stdout.
    assert out.startswith("Error: ")
    assert "--serial" in out, "the error does not say what to do next"


def test_main_json_error_payload_is_preserved_and_exits_non_zero(monkeypatch, capsys):
    _patch_adb(
        monkeypatch,
        result_for=lambda _cmd: _fake_result(
            returncode=1, stderr="adb: more than one device/emulator\n"
        ),
    )
    monkeypatch.setattr(screen_mapper, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(screen_mapper.sys, "argv", ["screen_mapper.py", "--json"])

    with pytest.raises(SystemExit) as exc:
        screen_mapper.main()

    assert exc.value.code != 0
    payload = json_lib.loads(capsys.readouterr().out)
    assert "error" in payload, "the JSON error contract changed"


def test_main_exits_zero_when_the_screen_is_read(monkeypatch, capsys, recorded):
    _patch_adb(
        monkeypatch,
        result_for=_dump_result_for(recorded.text("uiautomator_compose_default")),
    )
    monkeypatch.setattr(screen_mapper, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(
        screen_mapper.sys, "argv", ["screen_mapper.py", "--serial", "emulator-5554"]
    )

    with pytest.raises(SystemExit) as exc:
        screen_mapper.main()

    assert exc.value.code == 0
    assert "Screen:" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
