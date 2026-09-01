"""Device-free tests for the status_bar demo-mode override + preset logic.

These exercise the pure arg->command mapping by monkeypatching the
``subprocess.run`` inside ``common.adb_exec`` -- the single place status_bar
now reaches adb through -- so no adb / emulator is required. We assert:

* presets map to coherent override field groups,
* ``override`` enters demo mode once then emits the expected demo broadcasts
  atomically (clock / battery / network),
* the individual setters (``set_battery`` etc.) go through demo mode too, and
* every call is bounded, and a device-level failure reaches the user as an
  actionable message with a non-zero exit rather than a traceback.

This file previously declared the individual setters out of scope and
asserted they used ``cmd statusbar battery-level``. That subcommand does
not exist -- see tests/test_invented_commands_removed.py -- so the test was
pinning a broken path as intended behaviour.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
import status_bar
from status_bar import StatusBarController

from common import adb_exec


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def captured_cmds(monkeypatch):
    """Capture every adb command list status_bar issues, in order."""
    cmds: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        cmds.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return cmds


@pytest.fixture
def captured_calls(monkeypatch):
    """Capture ``(cmd, kwargs)`` pairs, for assertions about how adb was run."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append((list(cmd), kwargs))
        return _FakeCompleted()

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def failing_adb(monkeypatch):
    """Install an adb whose every call fails the way the caller asks for."""

    def _install(returncode=1, stdout="", stderr="", raises=None):
        def fake_run(cmd, *args, **kwargs):
            if raises is not None:
                raise raises
            return _FakeCompleted(returncode, stdout, stderr)

        monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)

    return _install


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


def test_set_battery_uses_demo_mode(captured_cmds):
    """The individual setter now takes the same path as override().

    It previously issued `cmd statusbar battery-level`, which is not a real
    subcommand; "battery-level" is a demo-mode broadcast extra.
    """
    ok, msg = StatusBarController().set_battery(42)
    assert ok is True
    assert "42%" in msg

    assert not any(
        "statusbar" in c for c in captured_cmds
    ), "still issuing a `cmd statusbar` subcommand"
    assert any("com.android.systemui.demo" in c for c in captured_cmds)
    # Demo mode must be entered, or the broadcast is ignored.
    assert any(c[-2:] == ["command", "enter"] for c in captured_cmds)


def test_override_failure_returns_message(failing_adb):
    failing_adb(returncode=1, stderr="boom")
    ok, msg = StatusBarController().override(time="9:41")
    assert ok is False
    assert "boom" in msg
    assert msg.startswith("Failed to apply override: ")


@pytest.mark.parametrize(
    ("call", "prefix"),
    [
        (lambda c: c.set_battery(50), "Failed to set battery: "),
        (lambda c: c.set_wifi(True, 3), "Failed to set wifi: "),
        (lambda c: c.set_mobile_data(True, 3, "lte"), "Failed to set mobile data: "),
        (lambda c: c.set_time("9:41"), "Failed to set time: "),
        (lambda c: c.reset(), "Failed to reset: "),
    ],
)
def test_command_failures_keep_their_tuple_shape(failing_adb, call, prefix):
    """A non-zero adb exit is still reported as (False, "Failed to ...")."""
    failing_adb(returncode=1, stderr="boom")
    ok, msg = call(StatusBarController())
    assert ok is False
    assert msg.startswith(prefix)
    assert "boom" in msg


# ---------------------------------------------------------------------------
# Bounding: an unbounded adb call wedges the connection for whatever runs next.
# ---------------------------------------------------------------------------


def test_every_adb_call_is_bounded(captured_calls):
    """Every call must carry a timeout, not merely most of them."""
    StatusBarController().apply_preset("clean")
    assert captured_calls
    for cmd, kwargs in captured_calls:
        assert kwargs.get("timeout"), f"unbounded adb call: {' '.join(cmd)}"


def test_module_makes_no_direct_subprocess_calls():
    """All adb traffic goes through run_adb, so bounding cannot be bypassed."""
    source = Path(status_bar.__file__).read_text(encoding="utf-8")
    offenders = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and getattr(node.func.value, "id", None) == "subprocess"
    ]
    assert not offenders, f"status_bar bypasses run_adb: {offenders}"


# ---------------------------------------------------------------------------
# Device errors: the command never ran, so it raises rather than returning
# (False, message) — and the CLI turns that into an actionable line, not a
# traceback.
# ---------------------------------------------------------------------------


def test_unknown_serial_raises_rather_than_reporting_a_command_failure(failing_adb):
    """A missing device means nothing ran; a (False, msg) tuple would hide that."""
    failing_adb(returncode=1, stderr="error: device 'no-such-serial' not found\n")
    with pytest.raises(adb_exec.DeviceNotFoundError):
        StatusBarController(serial="no-such-serial").set_battery(50)


def test_multiple_devices_raises_from_a_setter(failing_adb):
    failing_adb(returncode=1, stderr="adb: more than one device/emulator\n")
    with pytest.raises(adb_exec.MultipleDevicesError):
        StatusBarController().override(time="9:41")


def test_cli_reports_an_unknown_serial_and_exits_one(failing_adb, monkeypatch, capsys):
    """No traceback: an actionable message on stderr and exit status 1."""
    failing_adb(returncode=1, stderr="error: device 'no-such-serial' not found\n")
    monkeypatch.setattr(
        "sys.argv", ["status_bar.py", "--serial", "no-such-serial", "--battery", "50"]
    )

    with pytest.raises(SystemExit) as excinfo:
        status_bar.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "no-such-serial" in captured.err
    assert "adb devices" in captured.err, "the error must say how to see what is attached"
    assert "Traceback" not in captured.err


def test_cli_reports_a_timeout_and_exits_one(failing_adb, monkeypatch, capsys):
    """A wedged adb connection surfaces as an error, not an apparent stall."""
    failing_adb(raises=subprocess.TimeoutExpired(cmd="adb", timeout=30))
    monkeypatch.setattr("sys.argv", ["status_bar.py", "--preset", "clean"])

    with pytest.raises(SystemExit) as excinfo:
        status_bar.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "kill-server" in captured.err


def test_cli_reports_missing_adb_and_exits_one(failing_adb, monkeypatch, capsys):
    failing_adb(raises=FileNotFoundError("adb"))
    monkeypatch.setattr("sys.argv", ["status_bar.py", "--reset"])

    with pytest.raises(SystemExit) as excinfo:
        status_bar.main()

    assert excinfo.value.code == 1
    assert "platform-tools" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Demo mode is entered once per controller, and re-entered after a reset.
# ---------------------------------------------------------------------------


def _enter_count(cmds) -> int:
    return len([c for c in cmds if c[-2:] == ["command", "enter"]])


def test_demo_mode_is_entered_once_across_several_setters(captured_cmds):
    """Re-entering an open gate costs two round trips and changes nothing."""
    controller = StatusBarController()
    controller.set_battery(50)
    controller.set_wifi(True, 3)
    controller.set_time("9:41")

    assert _enter_count(captured_cmds) == 1
    allows = [c for c in captured_cmds if "sysui_demo_allowed" in c and c[-1] == "1"]
    assert len(allows) == 1


def test_reset_reopens_the_gate_for_a_later_setter(captured_cmds):
    """reset() closes demo mode, so the next setter must open it again.

    Broadcasting into a closed gate is silently ignored, which is the failure
    mode caching must not introduce.
    """
    controller = StatusBarController()
    controller.set_battery(50)
    controller.reset()
    controller.set_battery(60)

    assert _enter_count(captured_cmds) == 2
    allows = [c for c in captured_cmds if "sysui_demo_allowed" in c and c[-1] == "1"]
    assert len(allows) == 2


def test_a_failed_enter_is_not_remembered_as_entered(monkeypatch):
    """If entering demo mode fails, the next attempt must try again."""
    calls: list[list[str]] = []
    outcomes = iter([1, 0, 0, 0, 0, 0, 0])

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(next(outcomes, 0), "", "boom")

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)

    controller = StatusBarController()
    ok, _msg = controller.set_battery(50)
    assert ok is False

    ok, _msg = controller.set_battery(50)
    assert ok is True
    assert _enter_count(calls) == 1, "the retry never re-entered demo mode"
