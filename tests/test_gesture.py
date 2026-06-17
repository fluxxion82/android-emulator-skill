"""Device-free tests for the gesture script's pull-to-refresh mapping.

``refresh`` is pure arg->command logic: given a screen size it computes the
swipe coordinates and builds an ``adb shell input swipe`` command. These tests
mock ``subprocess.run`` and the screen-size lookup so they run without a device.
"""

from __future__ import annotations

import gesture


def _make_simulator(monkeypatch, width: int, height: int):
    """Build a GestureSimulator with a stubbed screen size and captured adb calls."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(gesture.subprocess, "run", fake_run)

    sim = gesture.GestureSimulator(serial="emulator-5554")
    sim._screen_size = (width, height)
    return sim, calls


def test_refresh_builds_pull_down_swipe(monkeypatch):
    sim, calls = _make_simulator(monkeypatch, 1080, 2400)

    success, message = sim.refresh()

    assert success is True
    # One adb swipe command issued.
    assert len(calls) == 1
    cmd = calls[0]
    # Targets the explicit serial via build_adb_command and uses input swipe.
    assert cmd[:6] == ["adb", "-s", "emulator-5554", "shell", "input", "swipe"]
    # center_x = 1080 // 2; start_y = 30% * 2400; end_y = 80% * 2400.
    assert cmd[6:] == ["540", "720", "540", "1920", str(gesture.REFRESH_DURATION_MS)]
    assert "Pulled to refresh" in message


def test_refresh_default_duration_is_600ms():
    # The curated delta specifies a ~600ms pull-to-refresh by default.
    assert gesture.REFRESH_DURATION_MS == 600


def test_refresh_swipes_downward(monkeypatch):
    # Pull-to-refresh must travel downward: end_y strictly below start_y.
    sim, calls = _make_simulator(monkeypatch, 720, 1280)
    sim.refresh()
    cmd = calls[0]
    start_y = int(cmd[7])
    end_y = int(cmd[9])
    assert end_y > start_y


def test_refresh_honors_explicit_duration(monkeypatch):
    sim, calls = _make_simulator(monkeypatch, 1080, 2400)
    sim.refresh(duration_ms=900)
    assert calls[0][-1] == "900"


def test_refresh_reports_failure(monkeypatch):
    def boom(cmd, *args, **kwargs):
        raise gesture.subprocess.CalledProcessError(1, cmd, stderr="device offline")

    monkeypatch.setattr(gesture.subprocess, "run", boom)
    sim = gesture.GestureSimulator(serial="emulator-5554")
    sim._screen_size = (1080, 2400)

    success, message = sim.refresh()
    assert success is False
    assert "failed" in message.lower()
