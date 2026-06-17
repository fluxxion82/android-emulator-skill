"""Device-free tests for the status_bar demo-mode override + preset logic.

These exercise the pure arg->command mapping by monkeypatching the module's
``subprocess.run`` so no adb / emulator is required. We assert:

* presets map to coherent override field groups,
* ``override`` enters demo mode once then emits the expected demo broadcasts
  atomically (clock / battery / network), and
* existing individual setters (``set_battery``) are untouched.
"""

from __future__ import annotations

import subprocess

import pytest
import status_bar
from status_bar import StatusBarController


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

    monkeypatch.setattr(status_bar.subprocess, "run", fake_run)
    return cmds


def _network_cmds(cmds):
    """Return demo broadcasts that are network commands."""
    return [c for c in cmds if "network" in c]


def test_presets_cover_expected_names():
    assert set(StatusBarController.PRESETS) == {
        "clean",
        "testing",
        "low-battery",
        "airplane",
    }


def test_preset_clean_fields():
    clean = StatusBarController.PRESETS["clean"]
    assert clean["time"] == "9:41"
    assert clean["battery"] == 100
    assert clean["wifi"] is True
    assert clean["mobile"] is True
    assert clean["airplane"] is False


def test_preset_airplane_disables_radios():
    air = StatusBarController.PRESETS["airplane"]
    assert air["airplane"] is True
    assert air["wifi"] is False
    assert air["mobile"] is False


def test_low_battery_uses_tunable_level():
    # The preset battery level is sourced from the module-level tunable.
    assert (
        StatusBarController.PRESETS["low-battery"]["battery"] == status_bar.PRESET_LOW_BATTERY_LEVEL
    )


def test_override_enters_demo_mode_once(captured_cmds):
    ok, _ = StatusBarController().override(time="9:41", battery=80)
    assert ok is True

    # Exactly one "enter" demo broadcast for the whole atomic group.
    enters = [c for c in captured_cmds if c[-2:] == ["command", "enter"]]
    assert len(enters) == 1

    # And the demo-allowed gate is opened exactly once.
    allows = [c for c in captured_cmds if "sysui_demo_allowed" in c and c[-1] == "1"]
    assert len(allows) == 1


def test_override_clock_and_battery_commands(captured_cmds):
    ok, msg = StatusBarController().override(time="9:41", battery=20, charging=False)
    assert ok is True
    assert "battery=20%" in msg

    clock = [c for c in captured_cmds if "clock" in c]
    assert len(clock) == 1
    assert clock[0][-3:] == ["-e", "hhmm", "941"]

    battery = [c for c in captured_cmds if "battery" in c]
    assert len(battery) == 1
    assert "level" in battery[0]
    assert battery[0][battery[0].index("level") + 1] == "20"
    # Not charging -> plugged false.
    assert battery[0][battery[0].index("plugged") + 1] == "false"


def test_override_charging_sets_plugged_true(captured_cmds):
    StatusBarController().override(battery=100, charging=True)
    battery = [c for c in captured_cmds if "battery" in c]
    assert battery[0][battery[0].index("plugged") + 1] == "true"


def test_override_wifi_show_includes_level(captured_cmds):
    StatusBarController().override(wifi=True, wifi_level=2)
    wifi = [c for c in _network_cmds(captured_cmds) if "wifi" in c]
    assert len(wifi) == 1
    assert "show" in wifi[0]
    assert wifi[0][wifi[0].index("level") + 1] == "2"


def test_override_wifi_hide(captured_cmds):
    StatusBarController().override(wifi=False)
    wifi = [c for c in _network_cmds(captured_cmds) if "wifi" in c]
    assert len(wifi) == 1
    assert "hide" in wifi[0]
    assert "level" not in wifi[0]


def test_override_mobile_show_includes_datatype(captured_cmds):
    StatusBarController().override(mobile=True, mobile_level=3, mobile_type="5g")
    mobile = [c for c in _network_cmds(captured_cmds) if "mobile" in c]
    assert len(mobile) == 1
    assert mobile[0][mobile[0].index("datatype") + 1] == "5g"
    assert mobile[0][mobile[0].index("level") + 1] == "3"


def test_override_rejects_out_of_range_battery(captured_cmds):
    ok, msg = StatusBarController().override(battery=150)
    assert ok is False
    assert "between 0 and 100" in msg
    # Validation happens before any adb call.
    assert captured_cmds == []


def test_override_none_radios_emit_no_network_cmd(captured_cmds):
    # Leaving wifi/mobile as None must not touch the network at all.
    StatusBarController().override(time="9:41")
    assert _network_cmds(captured_cmds) == []


def test_apply_preset_airplane_shows_airplane_hides_radios(captured_cmds):
    ok, msg = StatusBarController().apply_preset("airplane")
    assert ok is True
    assert "airplane" in msg

    net = _network_cmds(captured_cmds)
    airplane = [c for c in net if "airplane" in c]
    assert airplane and "show" in airplane[0]

    wifi = [c for c in net if "wifi" in c]
    assert wifi and "hide" in wifi[0]

    mobile = [c for c in net if "mobile" in c]
    assert mobile and "hide" in mobile[0]


def test_apply_preset_unknown_name():
    ok, msg = StatusBarController().apply_preset("nope")
    assert ok is False
    assert "Unknown preset" in msg


def test_serial_threaded_into_commands(captured_cmds):
    StatusBarController(serial="emulator-5554").override(time="9:41")
    # Every adb invocation must target the requested serial.
    assert captured_cmds
    for cmd in captured_cmds:
        assert cmd[0] == "adb"
        assert cmd[1:3] == ["-s", "emulator-5554"]


def test_set_battery_unchanged_uses_statusbar_cmd(captured_cmds):
    # Existing individual flag path is preserved (cmd statusbar, not demo mode).
    ok, msg = StatusBarController().set_battery(42)
    assert ok is True
    assert "42%" in msg
    assert any("statusbar" in c and "battery-level" in c for c in captured_cmds)
    # set_battery must not enter demo mode.
    assert not any(c[-2:] == ["command", "enter"] for c in captured_cmds)


def test_override_failure_returns_message(monkeypatch):
    def boom(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(status_bar.subprocess, "run", boom)
    ok, msg = StatusBarController().override(time="9:41")
    assert ok is False
    assert "boom" in msg
