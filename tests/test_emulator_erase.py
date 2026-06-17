"""Device-free tests for emulator_erase.

These mock the module's ``subprocess`` (for the running check) and point the AVD
home at a ``tmp_path`` so no emulator/adb is needed. They assert the pure logic
for the new feature deltas: ``--all`` (batch erase with structured counts),
``--verify`` (poll the AVD on disk until the wipe lands), and the
env-configurable ``ANDROID_EMU_ERASE_TIMEOUT`` tunable.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import emulator_erase
import pytest


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


def test_running_check_uses_adb_command_mapping(monkeypatch, tmp_path):
    """is_avd_running builds plain adb commands (no shell=True)."""
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    e = emulator_erase.EmulatorEraser()

    calls: list[list[str]] = []

    class _Result:
        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["adb", "devices"]:
            return _Result("List of devices attached\nemulator-5554\tdevice\n")
        # adb -s emulator-5554 emu avd name
        return _Result("Pixel_5_API_33\nOK\n")

    monkeypatch.setattr(emulator_erase.subprocess, "run", fake_run)

    assert e.is_avd_running("Pixel_5_API_33") is True
    assert calls[0] == ["adb", "devices"]
    assert calls[1] == ["adb", "-s", "emulator-5554", "emu", "avd", "name"]


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
