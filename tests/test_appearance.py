"""Device-free tests for appearance.py pure logic.

These exercise the pure arg->command mapping and alias->font_scale resolution
by monkeypatching the ``subprocess.run`` that ``common.adb_exec`` calls, so no
adb / emulator is required. Every device operation now goes through
``adb_exec.run_adb``, which is where the bound and the typed errors live; the
fakes below therefore stand in for adb itself rather than for a per-module
``subprocess``. We assert:

* friendly text-size aliases map to the expected font_scale values,
* theme / font-scale / locale adb commands are constructed correctly
  (and target the requested serial),
* no call goes out unbounded, and a device-level failure exits 1 with a
  remedy rather than a traceback, and
* the locale path degrades gracefully (best-effort) rather than pretending
  it always works.
"""

from __future__ import annotations

import subprocess
import sys

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
    """Capture every adb command list that reaches subprocess.run."""
    cmds: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        cmds.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(appearance.adb_exec.subprocess, "run", fake_run)
    return cmds


@pytest.fixture
def failing_adb(monkeypatch):
    """Make every adb call answer with a given status and stderr."""

    def _install(stderr: str, returncode: int = 1):
        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode, "", stderr)

        monkeypatch.setattr(appearance.adb_exec.subprocess, "run", fake_run)

    return _install


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


def test_set_locale_failure_explains_privilege_requirement(failing_adb):
    failing_adb("Permission denied")
    ok, msg = AppearanceController().set_locale("de-DE")
    assert ok is False
    assert "root" in msg or "privileged" in msg
    assert "reboot" in msg


def test_theme_failure_returns_message(failing_adb):
    failing_adb("boom")
    ok, msg = AppearanceController().set_theme("dark")
    assert ok is False
    assert "boom" in msg


# === bounding: no appearance call may go out unbounded ===


def test_every_appearance_call_is_bounded(monkeypatch):
    """An unbounded adb call wedges the connection for whatever runs next."""
    seen: list[dict] = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(appearance.adb_exec.subprocess, "run", fake_run)

    controller = AppearanceController(serial="emulator-5554")
    controller.set_theme("dark")
    controller.set_font_scale(1.15)
    controller.set_locale("fr-FR")

    assert len(seen) == 3
    assert all(kwargs.get("timeout") for kwargs in seen), "an adb call went out unbounded"


# === device-level failures reach the CLI boundary, not the user's terminal ===


def test_device_error_is_raised_rather_than_reported_as_a_failed_setting(failing_adb):
    """ "more than one device" means the command never ran; that is not a
    "failed to set theme"."""
    failing_adb("adb: more than one device/emulator\n")
    with pytest.raises(appearance.adb_exec.MultipleDevicesError):
        AppearanceController().set_theme("dark")


def test_unknown_serial_exits_one_with_an_actionable_message(
    monkeypatch, capsys, failing_adb, recorded_anywhere
):
    """A wrong --serial must yield a remedy and exit 1, never a traceback."""
    failing_adb(recorded_anywhere("adb_device_not_found"))
    monkeypatch.setattr(appearance, "resolve_device_identifier", lambda value: value)
    monkeypatch.setattr(
        sys, "argv", ["appearance.py", "--serial", "no-such-serial-xyz", "--theme", "dark"]
    )

    with pytest.raises(SystemExit) as excinfo:
        appearance.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "adb devices" in captured.err, "the error does not say how to see what is attached"
    assert "Traceback" not in captured.err
