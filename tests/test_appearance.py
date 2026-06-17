"""Device-free tests for appearance.py pure logic.

These exercise the pure arg->command mapping and alias->font_scale resolution
by monkeypatching the module's ``subprocess.run`` so no adb / emulator is
required. We assert:

* friendly text-size aliases map to the expected font_scale values,
* theme / font-scale / locale adb commands are constructed correctly
  (and target the requested serial), and
* the locale path degrades gracefully (best-effort) rather than pretending
  it always works.
"""

from __future__ import annotations

import subprocess

import appearance
import pytest
from appearance import AppearanceController


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def captured_cmds(monkeypatch):
    """Capture every adb command list passed to the module's subprocess.run."""
    cmds: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        cmds.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(appearance.subprocess, "run", fake_run)
    return cmds


# === alias -> font_scale mapping (pure) ===


def test_text_size_aliases_cover_expected_names():
    assert set(AppearanceController().text_size_aliases) == {
        "small",
        "default",
        "large",
        "xl",
    }


def test_alias_maps_to_module_tunables():
    aliases = AppearanceController().text_size_aliases
    assert aliases["small"] == appearance.TEXT_SIZE_SMALL_SCALE
    assert aliases["default"] == appearance.TEXT_SIZE_DEFAULT_SCALE
    assert aliases["large"] == appearance.TEXT_SIZE_LARGE_SCALE
    assert aliases["xl"] == appearance.TEXT_SIZE_XL_SCALE


def test_resolve_font_scale_defaults():
    ctrl = AppearanceController()
    assert ctrl.resolve_font_scale("default") == 1.0
    assert ctrl.resolve_font_scale("small") == 0.85
    assert ctrl.resolve_font_scale("large") == 1.15
    assert ctrl.resolve_font_scale("xl") == 1.3


def test_resolve_font_scale_is_case_insensitive():
    assert AppearanceController().resolve_font_scale("LARGE") == 1.15


def test_resolve_font_scale_unknown_returns_none():
    assert AppearanceController().resolve_font_scale("gigantic") is None


# === adb command construction (pure) ===


def test_build_theme_command_dark():
    cmd = AppearanceController().build_theme_command("dark")
    assert cmd == ["adb", "shell", "cmd", "uimode", "night", "yes"]


def test_build_theme_command_light():
    cmd = AppearanceController().build_theme_command("light")
    assert cmd == ["adb", "shell", "cmd", "uimode", "night", "no"]


def test_build_font_scale_command():
    cmd = AppearanceController().build_font_scale_command(1.3)
    assert cmd == ["adb", "shell", "settings", "put", "system", "font_scale", "1.3"]


def test_build_locale_command():
    cmd = AppearanceController().build_locale_command("fr-FR")
    assert cmd == ["adb", "shell", "setprop", "persist.sys.locale", "fr-FR"]


def test_build_commands_thread_serial():
    ctrl = AppearanceController(serial="emulator-5554")
    for cmd in (
        ctrl.build_theme_command("dark"),
        ctrl.build_font_scale_command(1.0),
        ctrl.build_locale_command("ja-JP"),
    ):
        assert cmd[0] == "adb"
        assert cmd[1:3] == ["-s", "emulator-5554"]


# === device operations via mocked subprocess ===


def test_set_theme_runs_uimode_command(captured_cmds):
    ok, msg = AppearanceController().set_theme("dark")
    assert ok is True
    assert "dark" in msg
    assert captured_cmds == [["adb", "shell", "cmd", "uimode", "night", "yes"]]


def test_set_text_size_resolves_alias_to_font_scale(captured_cmds):
    ok, msg = AppearanceController().set_text_size("large")
    assert ok is True
    assert "large" in msg
    assert "1.15" in msg
    assert captured_cmds == [["adb", "shell", "settings", "put", "system", "font_scale", "1.15"]]


def test_set_text_size_unknown_alias_no_adb_call(captured_cmds):
    ok, msg = AppearanceController().set_text_size("huge")
    assert ok is False
    assert "Unknown text size" in msg
    assert captured_cmds == []


def test_set_font_scale_rejects_nonpositive(captured_cmds):
    ok, msg = AppearanceController().set_font_scale(0)
    assert ok is False
    assert "greater than 0" in msg
    # Validation happens before any adb call.
    assert captured_cmds == []


def test_reset_sets_light_theme_and_default_scale(captured_cmds):
    ok, msg = AppearanceController().reset()
    assert ok is True
    assert "light" in msg
    assert captured_cmds == [
        ["adb", "shell", "cmd", "uimode", "night", "no"],
        ["adb", "shell", "settings", "put", "system", "font_scale", "1.0"],
    ]


def test_reset_does_not_touch_locale(captured_cmds):
    AppearanceController().reset()
    assert not any("setprop" in c for c in captured_cmds)


# === locale degrades gracefully (does not pretend it always works) ===


def test_set_locale_success_message_is_best_effort(captured_cmds):
    ok, msg = AppearanceController().set_locale("de-DE")
    assert ok is True
    assert "best-effort" in msg
    assert "reboot" in msg
    assert captured_cmds == [["adb", "shell", "setprop", "persist.sys.locale", "de-DE"]]


def test_set_locale_failure_explains_privilege_requirement(monkeypatch):
    def boom(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="Permission denied")

    monkeypatch.setattr(appearance.subprocess, "run", boom)
    ok, msg = AppearanceController().set_locale("de-DE")
    assert ok is False
    assert "root" in msg or "privileged" in msg
    assert "reboot" in msg


def test_theme_failure_returns_message(monkeypatch):
    def boom(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(appearance.subprocess, "run", boom)
    ok, msg = AppearanceController().set_theme("dark")
    assert ok is False
    assert "boom" in msg
