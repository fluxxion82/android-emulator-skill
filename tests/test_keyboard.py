"""Unit tests for keyboard.py feature deltas (--delay, --count, --dismiss).

These exercise the pure arg->adb-command mapping by monkeypatching the
module's ``subprocess.run`` (and ``time.sleep``) so no device is required.
Each test asserts the exact ``adb ... input ...`` argv that would be sent.
"""

from __future__ import annotations

import keyboard


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess used by check=True calls."""

    returncode = 0
    stdout = ""
    stderr = ""


def _patch_run(monkeypatch):
    """Record every argv passed to keyboard.subprocess.run; skip real sleeps."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(keyboard.subprocess, "run", fake_run)
    monkeypatch.setattr(keyboard.time, "sleep", fake_sleep)
    return calls


def test_type_text_single_shot_by_default(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.type_text("Hello World")

    assert success is True
    assert message == 'Typed: "Hello World"'
    # One input text call; spaces are escaped to %s.
    assert calls == [["adb", "-s", "emulator-5554", "shell", "input", "text", "Hello%sWorld"]]


def test_type_text_with_delay_is_per_character(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.type_text("ab c", delay=0.1)

    assert success is True
    assert message == 'Typed: "ab c" (slowly, 0.1s/char)'
    # One adb call per character; the space character is escaped to %s.
    assert calls == [
        ["adb", "-s", "emulator-5554", "shell", "input", "text", "a"],
        ["adb", "-s", "emulator-5554", "shell", "input", "text", "b"],
        ["adb", "-s", "emulator-5554", "shell", "input", "text", "%s"],
        ["adb", "-s", "emulator-5554", "shell", "input", "text", "c"],
    ]


def test_delay_sleeps_between_characters(monkeypatch):
    _patch_run(monkeypatch)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(keyboard.time, "sleep", fake_sleep)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    sim.type_text("abc", delay=0.2)

    assert sleeps == [0.2, 0.2, 0.2]


def test_press_key_default_count_once(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.press_key("enter")

    assert success is True
    assert message == "Pressed: KEYCODE_ENTER"
    assert calls == [["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "KEYCODE_ENTER"]]


def test_press_key_repeats_count_times(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.press_key("delete", count=3)

    assert success is True
    assert message == "Pressed: KEYCODE_DEL (3x)"
    delete_cmd = ["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "KEYCODE_DEL"]
    assert calls == [delete_cmd, delete_cmd, delete_cmd]


def test_press_key_count_below_one_presses_once(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.press_key("enter", count=0)

    assert success is True
    assert message == "Pressed: KEYCODE_ENTER"
    assert len(calls) == 1


def test_press_key_unknown_key_no_subprocess(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.press_key("notakey", count=5)

    assert success is False
    assert "Unknown key" in message
    assert calls == []


def test_dismiss_keyboard_sends_back(monkeypatch):
    calls = _patch_run(monkeypatch)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.dismiss_keyboard()

    assert success is True
    assert message == "Dismissed keyboard"
    assert calls == [["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "KEYCODE_BACK"]]
