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
``common.adb_exec`` and ``ET.parse`` to prove command construction never shells
out to a device.

screen_mapper reaches adb only through ``adb_exec.run_adb`` now, so the fake
goes there; patching ``screen_mapper.subprocess`` would stop intercepting and
let these tests dump a real device's screen.
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


# A hierarchy with one plain EditText and one password EditText so the secure
# count is independently observable from the total EditText count.
HIERARCHY_WITH_SECURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][1080,2400]">
    <node index="0" text="" resource-id="com.example.app:id/email"
          class="android.widget.EditText" content-desc="" clickable="true"
          enabled="true" focusable="true" password="false" bounds="[40,360][1040,460]" />
    <node index="1" text="" resource-id="com.example.app:id/password"
          class="android.widget.EditText" content-desc="" clickable="true"
          enabled="true" focusable="true" password="true" bounds="[40,520][1040,620]" />
    <node index="2" text="Log In" resource-id="com.example.app:id/login_button"
          class="android.widget.Button" content-desc="Log in" clickable="true"
          enabled="true" bounds="[40,680][1040,800]" />
  </node>
</hierarchy>"""


def _analyze(xml: str) -> dict:
    mapper = ScreenMapper()
    return mapper.analyze_tree(ET.fromstring(xml))


def _buttons_hierarchy(count: int) -> str:
    nodes = "".join(
        f'<node index="{i}" text="Btn{i}" class="android.widget.Button" '
        f'content-desc="" clickable="true" enabled="true" '
        f'bounds="[0,{i},10][10,{i}]" />'
        for i in range(count)
    )
    return (
        '<hierarchy rotation="0">'
        '<node index="0" class="android.widget.FrameLayout" enabled="true" '
        f'bounds="[0,0][1080,2400]">{nodes}</node>'
        "</hierarchy>"
    )


# --- Delta 2: secure/password field tracking -------------------------------


def test_secure_field_counted_separately():
    analysis = _analyze(HIERARCHY_WITH_SECURE)
    # Two EditTexts total, exactly one of them secure.
    assert len(analysis["edit_texts"]) == 2
    assert analysis["secure_fields"] == 1
    secure = [et for et in analysis["edit_texts"] if et["secure"]]
    assert len(secure) == 1
    assert secure[0]["label"] == "com.example.app:id/password"


def test_no_secure_fields_when_none_marked():
    xml = """<hierarchy rotation="0">
      <node index="0" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][1,1]">
        <node index="0" class="android.widget.EditText" enabled="true"
              clickable="true" focusable="true" bounds="[0,0][1,1]" />
      </node>
    </hierarchy>"""
    analysis = _analyze(xml)
    assert analysis["secure_fields"] == 0
    assert all(not et["secure"] for et in analysis["edit_texts"])


def test_summary_reports_secure_count():
    analysis = _analyze(HIERARCHY_WITH_SECURE)
    summary = ScreenMapper().format_summary(analysis)
    assert "EditTexts: 2 (0 filled) [1 secure]" in summary


def test_summary_omits_secure_marker_when_zero():
    xml = """<hierarchy rotation="0">
      <node index="0" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][1,1]">
        <node index="0" class="android.widget.EditText" enabled="true"
              clickable="true" focusable="true" bounds="[0,0][1,1]" />
      </node>
    </hierarchy>"""
    summary = ScreenMapper().format_summary(_analyze(xml))
    assert "secure" not in summary


# --- Delta 1: env-configurable preview limits ------------------------------


def test_buttons_preview_limit_truncates(monkeypatch):
    monkeypatch.setattr(screen_mapper, "BUTTONS_PREVIEW", 3)
    analysis = _analyze(_buttons_hierarchy(5))
    summary = ScreenMapper().format_summary(analysis)
    # 5 buttons total, preview capped at 3 -> truncation marker with total.
    assert '"Btn0", "Btn1", "Btn2"' in summary
    assert "(5 total)" in summary
    assert "Btn3" not in summary


def test_buttons_preview_no_truncation_under_limit(monkeypatch):
    monkeypatch.setattr(screen_mapper, "BUTTONS_PREVIEW", 15)
    analysis = _analyze(_buttons_hierarchy(2))
    summary = ScreenMapper().format_summary(analysis)
    assert "total)" not in summary
    assert '"Btn0", "Btn1"' in summary


def test_section_items_preview_limit_truncates_verbose(monkeypatch):
    monkeypatch.setattr(screen_mapper, "SECTION_ITEMS_PREVIEW", 2)
    analysis = _analyze(_buttons_hierarchy(5))
    summary = ScreenMapper().format_summary(analysis, verbose=True)
    assert "... and 3 more" in summary


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


def test_get_ui_hierarchy_builds_adb_without_shell(monkeypatch):
    calls, budgets = _patch_adb(monkeypatch)
    monkeypatch.setattr(
        ET, "parse", lambda _f: ET.ElementTree(ET.fromstring(HIERARCHY_WITH_SECURE))
    )

    root = ScreenMapper(serial="emulator-5554").get_ui_hierarchy()
    analysis = ScreenMapper().analyze_tree(root)
    assert analysis["secure_fields"] == 1

    # First call dumps the hierarchy via adb shell uiautomator dump.
    dump_cmd = calls[0]
    assert "adb" in dump_cmd[0]
    assert "uiautomator" in dump_cmd
    assert "dump" in dump_cmd
    assert "emulator-5554" in dump_cmd
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


def test_main_exits_zero_when_the_screen_is_read(monkeypatch, capsys):
    _patch_adb(monkeypatch)
    monkeypatch.setattr(
        ET, "parse", lambda _f: ET.ElementTree(ET.fromstring(HIERARCHY_WITH_SECURE))
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
