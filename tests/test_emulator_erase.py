"""Device-free tests for emulator_erase.

These mock the ``subprocess`` boundary underneath ``common.adb_exec`` (for the
running check) and point the AVD home at a ``tmp_path`` so no emulator/adb is
needed. They assert the pure logic for the feature deltas -- ``--all`` (batch
erase with structured counts), ``--verify`` (poll the AVD on disk until the wipe
lands), the env-configurable ``ANDROID_EMU_ERASE_TIMEOUT`` tunable -- plus the
error contract the adb_exec migration introduced: the running check must never
answer "not running" because adb failed to reach the device.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import emulator_erase
import pytest

from common import adb_exec


def _make_avd(avd_home: Path, name: str, with_userdata: bool = True) -> Path:
    """Create a fake ``<name>.avd`` dir with a config.ini and optional userdata."""
    avd_dir = avd_home / f"{name}.avd"
    avd_dir.mkdir(parents=True)
    (avd_dir / "config.ini").write_text("hw.device.name=pixel\n")
    if with_userdata:
        (avd_dir / "userdata-qemu.img").write_bytes(b"data")
        (avd_dir / "cache.img").write_bytes(b"data")
    return avd_dir


@pytest.fixture
def eraser(monkeypatch, tmp_path):
    """An eraser whose AVD home is an empty tmp dir and which is never 'running'."""
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    e = emulator_erase.EmulatorEraser()
    # Never shell out to adb during these tests.
    monkeypatch.setattr(e, "is_avd_running", lambda _name: False)
    monkeypatch.setattr(emulator_erase.time, "sleep", lambda _s: None)
    return e


def test_erase_deletes_userdata(eraser, tmp_path):
    avd_dir = _make_avd(tmp_path, "Pixel_5_API_33")

    success, message = eraser.erase("Pixel_5_API_33")

    assert success is True
    assert "AVD erased" in message
    assert not (avd_dir / "userdata-qemu.img").exists()
    assert not (avd_dir / "cache.img").exists()
    # Config is preserved (factory reset, not delete).
    assert (avd_dir / "config.ini").exists()


def test_erase_already_clean(eraser, tmp_path):
    _make_avd(tmp_path, "Pixel_5_API_33", with_userdata=False)

    success, message = eraser.erase("Pixel_5_API_33")

    assert success is True
    assert "already clean" in message


def test_erase_missing_avd(eraser):
    success, message = eraser.erase("DoesNotExist")
    assert success is False
    assert "not found" in message


def test_verify_succeeds_after_wipe(eraser, tmp_path):
    avd_dir = _make_avd(tmp_path, "Pixel_5_API_33")

    success, message = eraser.erase("Pixel_5_API_33", verify=True)

    assert success is True
    assert "verified clean" in message
    assert not (avd_dir / "userdata-qemu.img").exists()


def test_verify_times_out_when_userdata_lingers(eraser, tmp_path, monkeypatch):
    _make_avd(tmp_path, "Pixel_5_API_33")

    # Simulate a stuck wipe: unlink is a no-op so user data lingers on disk and
    # the verify poll never observes a clean dir.
    monkeypatch.setattr(emulator_erase.Path, "unlink", lambda _self, **_kw: None)

    # Tiny timeout so the test is fast.
    success, message = eraser.erase("Pixel_5_API_33", verify=True, timeout_seconds=0)

    assert success is False
    assert "verification timeout" in message


def test_erase_all_returns_structured_counts(eraser, tmp_path):
    _make_avd(tmp_path, "Pixel_5_API_33")
    _make_avd(tmp_path, "Pixel_7_API_34")

    succeeded, failed, results = eraser.erase_all()

    assert succeeded == 2
    assert failed == 0
    assert {r["avd"] for r in results} == {"Pixel_5_API_33", "Pixel_7_API_34"}
    assert all(r["success"] for r in results)


def test_erase_all_empty(eraser):
    succeeded, failed, results = eraser.erase_all()
    assert (succeeded, failed, results) == (0, 0, [])


def _fake_adb(monkeypatch, responses):
    """Answer adb calls at the subprocess boundary under common.adb_exec.

    ``responses`` maps a command prefix tuple to (returncode, stdout, stderr).
    Every call is recorded, argv and kwargs both, so the tests can assert the
    command mapping and that nothing goes out unbounded.
    """
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        for prefix, (returncode, stdout, stderr) in responses.items():
            if tuple(cmd[: len(prefix)]) == prefix or prefix == ():
                return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


def test_running_check_uses_adb_command_mapping(monkeypatch, tmp_path, recorded):
    """is_avd_running builds plain adb commands (no shell=True) and bounds them.

    The adb output is the recorded article rather than a hand-written stand-in:
    the real `adb devices` line carries trailing `product:`/`model:` fields, and
    the emulator console answers `avd name` with its own "OK" line.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    e = emulator_erase.EmulatorEraser()

    calls = _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, recorded.text("adb_devices_single"), ""),
            ("adb", "-s"): (0, recorded.text("emu_avd_name"), ""),
        },
    )

    assert e.is_avd_running("Pixel_9") is True
    assert calls[0]["cmd"] == ["adb", "devices"]
    assert calls[1]["cmd"] == ["adb", "-s", "emulator-5554", "emu", "avd", "name"]
    assert all(c["kwargs"].get("timeout") for c in calls), "an adb call went out unbounded"


def test_running_check_still_answers_no_on_a_plain_command_failure(monkeypatch, tmp_path):
    """A command that ran and failed is not evidence the AVD is running.

    This is the behaviour the pre-migration `except CalledProcessError` had, and
    it is preserved: only *device-level* failures are escalated.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    e = emulator_erase.EmulatorEraser()
    _fake_adb(monkeypatch, {("adb", "devices"): (1, "", "some other adb complaint\n")})

    assert e.is_avd_running("Pixel_9") is False


def test_a_device_error_is_not_answered_as_not_running(monkeypatch, tmp_path, recorded_anywhere):
    """The dangerous swallow: "adb could not reach the device" != "not running".

    Answering False here let an erase wipe the user data of an emulator that was
    actually live. The typed device error now reaches the caller instead, and
    the wipe does not happen.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    avd_dir = _make_avd(tmp_path, "Pixel_9")
    e = emulator_erase.EmulatorEraser()

    _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, "List of devices attached\nemulator-5554\tdevice\n", ""),
            ("adb", "-s"): (1, "", recorded_anywhere("adb_device_not_found")),
        },
    )

    with pytest.raises(adb_exec.DeviceNotFoundError):
        e.erase("Pixel_9")

    assert (avd_dir / "userdata-qemu.img").exists(), "user data was wiped on an unanswered check"


def test_cli_reports_an_adb_error_without_a_traceback(monkeypatch, tmp_path, capsys):
    """At the CLI boundary the agent gets the remedy, not a stack trace."""
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    _make_avd(tmp_path, "Pixel_9")
    _fake_adb(monkeypatch, {("adb",): (1, "", "error: device offline\n")})
    monkeypatch.setattr(emulator_erase.sys, "argv", ["emulator_erase.py", "--name", "Pixel_9"])

    with pytest.raises(SystemExit) as exc:
        emulator_erase.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "Traceback" not in captured.err
    message = captured.err.lower()
    assert "reconnect" in message or "kill-server" in message, "no remedy named"


def test_default_tunables():
    assert emulator_erase.DEFAULT_ERASE_TIMEOUT == 90
    assert emulator_erase.POLL_INTERVAL_SECONDS == 0.5


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_ERASE_TIMEOUT", "42")
    monkeypatch.setenv("ANDROID_EMU_POLL_INTERVAL", "1.5")
    reloaded = importlib.reload(emulator_erase)
    try:
        assert reloaded.DEFAULT_ERASE_TIMEOUT == 42
        assert reloaded.POLL_INTERVAL_SECONDS == 1.5
    finally:
        monkeypatch.delenv("ANDROID_EMU_ERASE_TIMEOUT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_POLL_INTERVAL", raising=False)
        importlib.reload(emulator_erase)
