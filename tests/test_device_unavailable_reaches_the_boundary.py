"""A device that is not there reaches common.cli from single-target operations.

Each operation below used to catch the whole adb family and hand back
``(False, message)`` -- so "no such device" exited 1 in its own shape, where
every other script exits 3 through the boundary. Each now lets
``adb_exec.DeviceError`` through and keeps its own handling for the rest.

Every device error here is built by adb_exec from adb's recorded words
(``adb_shell_device_not_found``), through the real ``run_adb`` seam: the fake
sits where adb_exec reaches the host, nowhere closer to the code under test.

Deliberately NOT changed, and so not tested here (see SKILL.md's named
exceptions): boot-completion polling, the best-effort activity lookup, the
emulator-console survey predicates, and the partial-capture policy of
test_recorder and app_state_capture.
"""

from __future__ import annotations

import json
import subprocess
import sys

import accessibility_audit
import emulator_shutdown
import location
import pytest
import sms
import snapshot

from common import adb_exec, cli
from common.emu_console import EmulatorProbe, EmulatorSurvey

GONE = "emulator-5556"


@pytest.fixture
def adb_says_not_found(monkeypatch, recorded_anywhere):
    """Every adb call fails the way adb itself fails for an unknown serial."""
    not_found = recorded_anywhere("adb_shell_device_not_found")

    def _run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", not_found)

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])


def test_sms_inbox_read(adb_says_not_found):
    with pytest.raises(adb_exec.DeviceNotFoundError):
        sms.SmsTester(serial=GONE).list_inbox()


def test_sms_send(adb_says_not_found):
    with pytest.raises(adb_exec.DeviceNotFoundError):
        sms.SmsTester(serial=GONE).send("5551234", "hi", verify=False)


def test_snapshot_console(adb_says_not_found):
    with pytest.raises(adb_exec.DeviceNotFoundError):
        snapshot.SnapshotManager(serial=GONE).list_snapshots()


def test_location_geo_fix(adb_says_not_found):
    with pytest.raises(adb_exec.DeviceNotFoundError):
        location.LocationManager(serial=GONE).set_coordinate(51.5, -0.12)


def test_accessibility_audit(adb_says_not_found):
    with pytest.raises(adb_exec.DeviceNotFoundError):
        accessibility_audit.AccessibilityAuditor(serial=GONE).audit()


def test_accessibility_density_no_longer_guesses_for_a_gone_device(adb_says_not_found):
    """It fell back to mdpi and audited on; a result for an unmeasured screen."""
    with pytest.raises(adb_exec.DeviceNotFoundError):
        accessibility_audit.AccessibilityAuditor(serial=GONE)._resolve_density()


def test_accessibility_density_still_falls_back_when_the_command_failed(monkeypatch):
    """The fallback that stays: `wm density` ran on the device and failed."""

    def _run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    auditor = accessibility_audit.AccessibilityAuditor(serial="emulator-5554")
    assert auditor._resolve_density() == auditor.DEFAULT_DENSITY_DPI


def test_emulator_shutdown_single_target(adb_says_not_found, monkeypatch):
    monkeypatch.setattr(
        emulator_shutdown,
        "get_connected_devices",
        lambda: [{"serial": GONE, "state": "device", "type": "emulator"}],
    )
    with pytest.raises(adb_exec.DeviceNotFoundError):
        emulator_shutdown.EmulatorShutdown(GONE).shutdown(verify=False)


def test_emulator_shutdown_all_goes_on_past_one_that_is_gone(monkeypatch):
    """The batch, not the single-target operation, decides to continue."""
    probes = (
        EmulatorProbe(serial=GONE, state="device", avd_name="a", reason=None),
        EmulatorProbe(serial="emulator-5558", state="device", avd_name="b", reason=None),
    )
    monkeypatch.setattr(
        emulator_shutdown,
        "survey_emulators",
        lambda _lister: EmulatorSurvey(probes=probes, unavailable=None),
    )

    def _shutdown(self, verify=False, timeout_seconds=None):
        if self.serial == GONE:
            raise adb_exec.DeviceNotFoundError(f"Device {GONE!r} is not attached.")
        return True, "stopped"

    monkeypatch.setattr(emulator_shutdown.EmulatorShutdown, "shutdown", _shutdown)

    assert emulator_shutdown.shutdown_all_emulators() == (1, 1)


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (sms, ["--serial", GONE, "--list", "--json"]),
        (snapshot, ["--serial", GONE, "--list", "--json"]),
    ],
    ids=["sms --list", "snapshot --list"],
)
def test_the_cli_says_exit_3_and_one_object(module, argv, adb_says_not_found, monkeypatch, capsys):
    """Both are exit-code-sweep modes: they answered their own JSON at exit 1."""
    monkeypatch.setattr(module, "resolve_device_identifier", lambda value: value)
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", *argv])

    with pytest.raises(SystemExit) as exc:
        cli.run(module.main)

    captured = capsys.readouterr()
    assert exc.value.code == 3
    assert json.loads(captured.out)["success"] is False
    assert "Traceback" not in captured.err
