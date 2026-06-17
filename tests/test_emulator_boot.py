"""Device-free tests for emulator_boot.

These mock the module's ``subprocess`` so no emulator/adb is needed. They assert
the pure arg->command mapping for ``--all`` (batch boot) and the env-configurable
boot timeout / poll interval tunables.
"""

from __future__ import annotations

import importlib

import emulator_boot
import pytest


class _FakeProcess:
    """Stand-in for subprocess.Popen that reports a still-running emulator."""

    def __init__(self, returncode: int | None = None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _no_connected_devices(monkeypatch):
    """Pretend no emulators are currently connected (so boot proceeds)."""
    monkeypatch.setattr(emulator_boot, "get_connected_devices", lambda: [])


def test_boot_all_boots_every_defined_avd(monkeypatch):
    _no_connected_devices(monkeypatch)
    monkeypatch.setattr(
        emulator_boot,
        "list_avds",
        lambda: [{"name": "Pixel_5_API_33"}, {"name": "Pixel_7_API_34"}],
    )
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    succeeded, failed, results = emulator_boot.EmulatorBooter.boot_all()

    assert succeeded == 2
    assert failed == 0
    assert [r["avd"] for r in results] == ["Pixel_5_API_33", "Pixel_7_API_34"]
    # Each AVD launched via `emulator -avd <name>` (no -no-window without headless).
    assert launched == [
        ["emulator", "-avd", "Pixel_5_API_33"],
        ["emulator", "-avd", "Pixel_7_API_34"],
    ]


def test_boot_all_headless_appends_no_window(monkeypatch):
    _no_connected_devices(monkeypatch)
    monkeypatch.setattr(emulator_boot, "list_avds", lambda: [{"name": "Pixel_5_API_33"}])
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    succeeded, failed, _results = emulator_boot.EmulatorBooter.boot_all(headless=True)

    assert (succeeded, failed) == (1, 0)
    assert launched == [["emulator", "-avd", "Pixel_5_API_33", "-no-window"]]


def test_boot_all_counts_failures(monkeypatch):
    _no_connected_devices(monkeypatch)
    monkeypatch.setattr(emulator_boot, "list_avds", lambda: [{"name": "Broken_AVD"}])
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    def fake_popen(_cmd, **_kwargs):
        # Process already exited -> boot reports failure.
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    succeeded, failed, results = emulator_boot.EmulatorBooter.boot_all()

    assert (succeeded, failed) == (0, 1)
    assert results[0]["success"] is False


def test_boot_all_empty_avds(monkeypatch):
    monkeypatch.setattr(emulator_boot, "list_avds", lambda: [])
    succeeded, failed, results = emulator_boot.EmulatorBooter.boot_all()
    assert (succeeded, failed, results) == (0, 0, [])


def test_default_tunables():
    # Defaults match the documented contract.
    assert emulator_boot.DEFAULT_BOOT_TIMEOUT == 300
    assert emulator_boot.POLL_INTERVAL_SECONDS == 0.5


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_BOOT_TIMEOUT", "42")
    monkeypatch.setenv("ANDROID_EMU_POLL_INTERVAL", "1.5")
    reloaded = importlib.reload(emulator_boot)
    try:
        assert reloaded.DEFAULT_BOOT_TIMEOUT == 42
        assert reloaded.POLL_INTERVAL_SECONDS == 1.5
    finally:
        monkeypatch.delenv("ANDROID_EMU_BOOT_TIMEOUT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_POLL_INTERVAL", raising=False)
        importlib.reload(emulator_boot)


@pytest.mark.parametrize("headless", [False, True])
def test_single_boot_command_mapping(monkeypatch, headless):
    _no_connected_devices(monkeypatch)
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    booter = emulator_boot.EmulatorBooter("Pixel_5_API_33")
    success, _message = booter.boot(wait_ready=False, headless=headless)

    assert success is True
    expected = ["emulator", "-avd", "Pixel_5_API_33"]
    if headless:
        expected.append("-no-window")
    assert launched == [expected]
