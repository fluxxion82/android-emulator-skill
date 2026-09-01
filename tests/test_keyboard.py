"""Unit tests for keyboard.py feature deltas (--delay, --count, --dismiss).

These exercise the pure arg->adb-command mapping by monkeypatching the
subprocess call underneath ``common.adb_exec`` (and ``time.sleep``) so no
device is required. Each test asserts the exact ``adb ... input ...`` argv that
would be sent.

keyboard reaches adb only through ``adb_exec.run_adb`` now, so the fake goes
there; patching ``keyboard.subprocess`` would stop intercepting and let these
tests drive a real device.
"""

from __future__ import annotations

import keyboard
import pytest

from common import adb_exec


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess used by check=True calls."""

    returncode = 0
    stdout = ""
    stderr = ""


def _patch_run(monkeypatch):
    """Record every argv adb_exec would run; skip real sleeps."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted()

    def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(keyboard.time, "sleep", fake_sleep)
    return calls


def _patch_failing_run(monkeypatch, stdout="", stderr="", returncode=1):
    """Make every adb call report the given failure."""

    def fake_run(cmd, *args, **kwargs):
        class _Result:
            pass

        result = _Result()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(keyboard.time, "sleep", lambda _s: None)


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


# ---------------------------------------------------------------------------
# Bounded calls and device errors an agent can act on.
# ---------------------------------------------------------------------------


def test_every_keyboard_adb_call_is_bounded(monkeypatch):
    """An unbounded adb call wedges the connection for whatever runs next."""
    budgets: list[object] = []

    def fake_run(cmd, *args, **kwargs):
        budgets.append(kwargs.get("timeout"))
        return _FakeCompleted()

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(keyboard.time, "sleep", lambda _s: None)

    sim = keyboard.KeyboardSimulator(serial="emulator-5554")
    sim.type_text("hi")
    sim.press_key("enter", count=2)
    sim.hide_keyboard()

    assert budgets, "no adb call was made"
    assert all(b for b in budgets), f"unbounded adb call: {budgets}"


def test_unknown_serial_raises_rather_than_reporting_a_typed_key(monkeypatch, recorded_anywhere):
    """The keystroke never reached a device, so it must not look like success."""
    _patch_failing_run(monkeypatch, stderr=recorded_anywhere("adb_device_not_found"))

    sim = keyboard.KeyboardSimulator(serial="no-such-serial-xyz")
    with pytest.raises(adb_exec.DeviceNotFoundError):
        sim.press_key("enter")


def test_main_reports_an_unknown_serial_without_a_traceback(monkeypatch, capsys, recorded_anywhere):
    """Exit 1 with the remedy on stderr; a traceback would bury it."""
    _patch_failing_run(monkeypatch, stderr=recorded_anywhere("adb_device_not_found"))
    monkeypatch.setattr(keyboard, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(
        keyboard.sys, "argv", ["keyboard.py", "--serial", "no-such-serial-xyz", "--key", "enter"]
    )

    with pytest.raises(SystemExit) as exc:
        keyboard.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "no-such-serial-xyz" in err
    assert "adb devices" in err, "the error does not say how to see what is attached"


def test_main_reports_multiple_devices_with_the_serial_remedy(monkeypatch, capsys):
    """The most common agent-facing failure: two emulators, no --serial."""
    _patch_failing_run(monkeypatch, stderr="adb: more than one device/emulator\n")
    monkeypatch.setattr(keyboard, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(keyboard.sys, "argv", ["keyboard.py", "--type", "hello"])

    with pytest.raises(SystemExit) as exc:
        keyboard.main()

    assert exc.value.code == 1
    assert "--serial" in capsys.readouterr().err


def test_hide_keyboard_flag_is_accepted(monkeypatch, capsys):
    """--hide-keyboard is documented and dispatched; it must also parse.

    Without the declaration, argparse produced no ``hide_keyboard`` attribute
    and every invocation reaching that branch (notably --dismiss) died with
    AttributeError.
    """
    calls = _patch_run(monkeypatch)
    monkeypatch.setattr(keyboard, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(
        keyboard.sys, "argv", ["keyboard.py", "--serial", "emulator-5554", "--hide-keyboard"]
    )

    with pytest.raises(SystemExit) as exc:
        keyboard.main()

    assert exc.value.code == 0
    assert calls == [["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "KEYCODE_BACK"]]
    assert "Keyboard hidden" in capsys.readouterr().out


def test_dismiss_does_not_crash_on_the_undeclared_flag(monkeypatch, capsys):
    """--dismiss falls through the --hide-keyboard branch on its way down."""
    _patch_run(monkeypatch)
    monkeypatch.setattr(keyboard, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(
        keyboard.sys, "argv", ["keyboard.py", "--serial", "emulator-5554", "--dismiss"]
    )

    with pytest.raises(SystemExit) as exc:
        keyboard.main()

    assert exc.value.code == 0
    assert "Dismissed keyboard" in capsys.readouterr().out
