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

import json as json_lib
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
    """The recorded Compose screen with its EditText marked ``password="true"``.

    No recorded dump has a password field -- the fixture app has no password
    input -- and the attribute is what the delta reads, so it is the one thing
    substituted. Everything else (the unmerged semantics tree, the empty
    resource-ids, the label living in a child TextView) is as the device
    dumped it.
    """
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    fields = [n for n in root.iter("node") if n.get("class", "").endswith(".EditText")]
    assert len(fields) == 1, f"fixture no longer has exactly one EditText: {len(fields)}"
    fields[0].set("password", "true")
    return ET.tostring(root, encoding="unicode")


# --- Delta 2: secure/password field tracking -------------------------------


def test_secure_field_counted_separately(recorded):
    analysis = _analyze(_compose_with_a_password_field(recorded))

    assert len(analysis["edit_texts"]) == 1
    assert analysis["secure_fields"] == 1
    secure = [et for et in analysis["edit_texts"] if et["secure"]]
    assert len(secure) == 1
    # Compose emits no resource-id, so the label is recovered from the field's
    # own subtree -- the caption TextView sitting inside it.
    assert secure[0]["label"] == "Email address"


def test_no_secure_fields_when_none_marked(recorded):
    """The same screen unmodified: the field is there, the secure count is not."""
    analysis = _analyze(recorded.text("uiautomator_compose_default"))
    assert analysis["edit_texts"], "fixture no longer has a field to count"
    assert analysis["secure_fields"] == 0
    assert all(not et["secure"] for et in analysis["edit_texts"])


def test_summary_reports_secure_count(recorded):
    analysis = _analyze(_compose_with_a_password_field(recorded))
    summary = ScreenMapper().format_summary(analysis)
    assert "EditTexts: 1 (0 filled) [1 secure]" in summary


def test_summary_omits_secure_marker_when_zero(recorded):
    summary = ScreenMapper().format_summary(_analyze(recorded.text("uiautomator_compose_default")))
    assert "secure" not in summary


# --- Delta 1: env-configurable preview limits ------------------------------
#
# The dialer keypad, because it is the recorded screen that HAS buttons:
# "More options", "backspace" and "Call", in that order. The Compose screen has
# none at all -- its clickable nodes report android.view.View -- which is C4's
# whole point and why the truncation deltas cannot be shown there.


def test_buttons_preview_limit_truncates(monkeypatch, recorded):
    monkeypatch.setattr(screen_mapper, "BUTTONS_PREVIEW", 2)
    analysis = _analyze(recorded.text("uiautomator_dialer_keypad"))
    assert len(analysis["buttons"]) == 3, f"fixture changed: {analysis['buttons']}"

    summary = ScreenMapper().format_summary(analysis)
    assert '"More options", "backspace"' in summary
    assert "(3 total)" in summary
    assert "Call" not in summary


def test_buttons_preview_no_truncation_under_limit(monkeypatch, recorded):
    monkeypatch.setattr(screen_mapper, "BUTTONS_PREVIEW", 15)
    analysis = _analyze(recorded.text("uiautomator_dialer_keypad"))
    summary = ScreenMapper().format_summary(analysis)
    assert "total)" not in summary
    assert '"More options", "backspace", "Call"' in summary


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
