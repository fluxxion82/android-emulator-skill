"""Device-free tests for the gesture script's pull-to-refresh mapping.

``refresh`` is pure arg->command logic: given a screen size it computes the
swipe coordinates and builds an ``adb shell input swipe`` command. These tests
mock the subprocess call underneath ``common.adb_exec`` and the screen-size
lookup so they run without a device.

gesture reaches adb only through ``adb_exec.run_adb`` now, so the fake goes
there; patching ``gesture.subprocess`` would stop intercepting and let these
tests drive a real device.
"""

from __future__ import annotations

import gesture
import pytest

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


def _make_simulator(monkeypatch, width: int, height: int):
    """Build a GestureSimulator with a stubbed screen size and captured adb calls."""
    calls: list[list[str]] = []
    budgets: list[object] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        budgets.append(kwargs.get("timeout"))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)

    sim = gesture.GestureSimulator(serial="emulator-5554")
    sim._screen_size = (width, height)
    sim._timeouts = budgets
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
    """A command that ran and failed is reported, not raised."""

    def failed(cmd, *args, **kwargs):
        return _fake_result(returncode=1, stderr="/system/bin/sh: input: inaccessible\n")

    monkeypatch.setattr(adb_exec.subprocess, "run", failed)
    sim = gesture.GestureSimulator(serial="emulator-5554")
    sim._screen_size = (1080, 2400)

    success, message = sim.refresh()
    assert success is False
    assert "failed" in message.lower()


# ---------------------------------------------------------------------------
# Bounded calls and device errors an agent can act on.
# ---------------------------------------------------------------------------


def test_every_gesture_adb_call_is_bounded(monkeypatch):
    """An unbounded adb call wedges the connection for whatever runs next."""
    sim, _calls = _make_simulator(monkeypatch, 1080, 2400)

    sim.swipe_path(0, 0, 10, 10)
    sim.long_press(5, 5)

    assert sim._timeouts, "no adb call was made"
    assert all(b for b in sim._timeouts), f"unbounded adb call: {sim._timeouts}"


def test_long_gesture_gets_a_budget_that_clears_its_own_duration(monkeypatch):
    """`input swipe` blocks for the gesture; a 60s drag must not self-timeout."""
    sim, _calls = _make_simulator(monkeypatch, 1080, 2400)

    sim.swipe_path(0, 0, 10, 10, duration_ms=60_000)

    assert sim._timeouts[-1] > 60, f"budget {sim._timeouts[-1]}s is shorter than the gesture"


def test_unknown_serial_raises_rather_than_reporting_a_swipe(monkeypatch, recorded_anywhere):
    """The gesture never reached a device, so it must not look like success."""

    def not_found(cmd, *args, **kwargs):
        return _fake_result(returncode=1, stderr=recorded_anywhere("adb_device_not_found"))

    monkeypatch.setattr(adb_exec.subprocess, "run", not_found)
    sim = gesture.GestureSimulator(serial="no-such-serial-xyz")

    with pytest.raises(adb_exec.DeviceNotFoundError):
        sim.swipe_path(0, 0, 10, 10)


def test_main_reports_multiple_devices_without_a_traceback(monkeypatch, capsys):
    """Two emulators and no --serial: exit 1 with the remedy on stderr."""

    def ambiguous(cmd, *args, **kwargs):
        return _fake_result(returncode=1, stderr="adb: more than one device/emulator\n")

    monkeypatch.setattr(adb_exec.subprocess, "run", ambiguous)
    monkeypatch.setattr(gesture, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(gesture.sys, "argv", ["gesture.py", "--long-press", "10,20"])

    with pytest.raises(SystemExit) as exc:
        gesture.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "--serial" in err, "the error does not say what to do next"


# ---------------------------------------------------------------------------
# `--scroll` moved the list the wrong way.
# ---------------------------------------------------------------------------


def _swipe_ys(calls: list[list[str]]) -> tuple[int, int]:
    """(start_y, end_y) of the last `input swipe` command."""
    swipe = next(c for c in reversed(calls) if "swipe" in c)
    coords = swipe[swipe.index("swipe") + 1 :]
    return int(coords[1]), int(coords[3])


def test_scroll_down_reveals_content_below(monkeypatch):
    """Measured on a real Settings screen: `--scroll down` did not move it.

    `scroll()` passed its direction straight to `swipe()`, but the two use
    opposite conventions -- `swipe("down")` means "the finger travels down",
    which drags the list *up* toward its start. So `--scroll down` scrolled
    the list up, and on a screen already at the top it did nothing at all
    while reporting `Scrolled: down (1 swipes)` and exiting 0.

    That is the inert-capability shape this whole effort exists to remove: an
    agent asked to scroll down to find an item gets success, no movement, and
    then a "Not found" from `navigator --find-text` -- which reads as "the item
    does not exist" rather than "it is below the fold".

    Scrolling down means revealing what is below, so the finger must travel
    up: start_y > end_y.
    """
    sim, calls = _make_simulator(monkeypatch, 1080, 2400)

    success, _ = sim.scroll("down")

    assert success
    start_y, end_y = _swipe_ys(calls)
    assert start_y > end_y, (
        f"scroll('down') swiped from y={start_y} to y={end_y}, which drags the "
        f"list back toward its top; it must reveal content below"
    )


def test_scroll_up_reveals_content_above(monkeypatch):
    """The mirror image, so the fix cannot be an inversion of the same bug."""
    sim, calls = _make_simulator(monkeypatch, 1080, 2400)

    sim.scroll("up")

    start_y, end_y = _swipe_ys(calls)
    assert start_y < end_y, (
        f"scroll('up') swiped from y={start_y} to y={end_y}; it must reveal " f"content above"
    )


def test_scroll_and_swipe_stay_opposite(monkeypatch):
    """Pins the convention rather than the numbers.

    `swipe` names what the finger does; `scroll` names what the content does.
    They are opposites, and collapsing them is what caused the defect.
    """
    sim, scroll_calls = _make_simulator(monkeypatch, 1080, 2400)
    sim.scroll("down")
    scroll_start, scroll_end = _swipe_ys(scroll_calls)

    sim2, swipe_calls = _make_simulator(monkeypatch, 1080, 2400)
    sim2.swipe("down")
    swipe_start, swipe_end = _swipe_ys(swipe_calls)

    assert (scroll_start > scroll_end) is not (swipe_start > swipe_end), (
        "scroll('down') and swipe('down') moved the finger the same way; "
        "one names the content's direction, the other the finger's"
    )


def test_every_scroll_swipe_is_issued(monkeypatch):
    """`--count 3` must produce three swipes, not one and a cheerful message."""
    sim, calls = _make_simulator(monkeypatch, 1080, 2400)

    sim.scroll("down", count=3)

    assert sum(1 for c in calls if "swipe" in c) == 3
