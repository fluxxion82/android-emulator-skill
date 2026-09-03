"""Unit tests for keyboard.py feature deltas (--delay, --count, --dismiss).

These exercise the pure arg->adb-command mapping by monkeypatching the
subprocess call underneath ``common.adb_exec`` (and ``time.sleep``) so no
device is required. Each test asserts the exact ``adb ... input ...`` argv that
would be sent.

keyboard reaches adb only through ``adb_exec.run_adb`` now, so the fake goes
there; patching ``keyboard.subprocess`` would stop intercepting and let these
tests drive a real device.

On the IME state (C8)
---------------------
``--hide-keyboard`` / ``--dismiss`` now read ``dumpsys input_method`` before
pressing BACK, and the double answers that call with a single
``mInputShown=<bool>`` line. That is a *value*, not a transcript: no
``dumpsys input_method`` recording exists in the corpus yet (Inc 0's recording
PR captures ``dumpsys_input_method_shown``), and inventing the surrounding
several hundred lines of service state is exactly what the fixture policy
forbids. What is asserted here is the decision -- press BACK, or do not -- for
each of the three states the field can be in, including absent. When the
recording lands, ``keyboard.parse_ime_shown`` is the single function to point
at it, and these tests keep their meaning.
"""

from __future__ import annotations

import keyboard
import pytest

from common import adb_exec


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess used by check=True calls."""

    returncode = 0
    stderr = ""

    def __init__(self, stdout: str = ""):
        self.stdout = stdout


# The one field `dumpsys input_method` is read for. Kept as a pair of values
# rather than a fabricated dump -- see the module docstring.
IME_SHOWN = f"{keyboard.IME_SHOWN_FIELD}=true"
IME_HIDDEN = f"{keyboard.IME_SHOWN_FIELD}=false"


def _patch_run(monkeypatch, ime_state: str = IME_SHOWN):
    """Record every argv adb_exec would run; skip real sleeps.

    ``ime_state`` is what ``dumpsys input_method`` answers: IME_SHOWN,
    IME_HIDDEN, or "" for a service that reports the field not at all.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "dumpsys" in cmd:
            return _FakeCompleted(stdout=ime_state)
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


def test_dismiss_keyboard_sends_back_when_one_is_shown(monkeypatch):
    calls = _patch_run(monkeypatch, ime_state=IME_SHOWN)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = sim.dismiss_keyboard()

    assert success is True
    assert message == "Dismissed keyboard"
    assert calls[-1] == ["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "KEYCODE_BACK"]


# ---------------------------------------------------------------------------
# C8: BACK is not a "hide keyboard" key.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["hide_keyboard", "dismiss_keyboard"], ids=["hide", "dismiss"])
def test_no_key_event_is_sent_when_no_keyboard_is_shown(monkeypatch, action):
    """The finding: with no IME up, BACK pops the activity.

    Both spellings used to press it unconditionally and report "Keyboard
    hidden" / "Dismissed keyboard" -- so the caller was told its keyboard was
    away while it was actually one screen back from where it had been, and
    whatever it ran next failed somewhere unrelated.
    """
    calls = _patch_run(monkeypatch, ime_state=IME_HIDDEN)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = getattr(sim, action)()

    assert success is True, "asking for a keyboard to be away is satisfied when none is up"
    assert "No keyboard shown" in message
    assert not [c for c in calls if "keyevent" in c], f"a key event was sent anyway: {calls}"


@pytest.mark.parametrize("action", ["hide_keyboard", "dismiss_keyboard"], ids=["hide", "dismiss"])
def test_an_unreadable_ime_state_does_not_press_back(monkeypatch, action):
    """Absent field: "I could not tell", which must not be read as "no keyboard".

    Pressing BACK on a guess is the destructive direction -- it can leave the
    screen -- so this reports the failure and names the flag that presses BACK
    on purpose.
    """
    calls = _patch_run(monkeypatch, ime_state="")
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = getattr(sim, action)()

    assert success is False
    assert keyboard.IME_SHOWN_FIELD in message
    assert "--button back" in message, "the failure names no remedy"
    assert not [c for c in calls if "keyevent" in c], f"a key event was sent anyway: {calls}"


def test_the_ime_state_is_read_before_any_key_event(monkeypatch):
    """Order matters: a check after the press would be decoration."""
    calls = _patch_run(monkeypatch, ime_state=IME_SHOWN)
    keyboard.KeyboardSimulator(serial="emulator-5554").hide_keyboard()

    assert calls[0][3:] == ["shell", "dumpsys", "input_method"], f"first call was {calls[0]}"
    assert len(calls) == 2, f"expected the check then the key event, got {calls}"


@pytest.mark.parametrize(
    ("state", "expected"),
    [(IME_SHOWN, True), (IME_HIDDEN, False), ("", None), ("mInputShown=TRUE", True)],
    ids=["true", "false", "absent", "uppercase"],
)
def test_parse_ime_shown_reads_the_field(state, expected):
    """Three states, and the third is not the second.

    None means the field was not there at all. A parser that folded that into
    False would put the unconditional BACK press straight back.
    """
    assert keyboard.parse_ime_shown(state) is expected


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
    assert calls[-1] == ["adb", "-s", "emulator-5554", "shell", "input", "keyevent", "KEYCODE_BACK"]
    assert "Keyboard hidden" in capsys.readouterr().out


def test_the_cli_reports_a_no_op_hide_as_success(monkeypatch, capsys):
    """C8 at the CLI: nothing to hide is not a failure, and issues nothing."""
    calls = _patch_run(monkeypatch, ime_state=IME_HIDDEN)
    monkeypatch.setattr(keyboard, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(
        keyboard.sys, "argv", ["keyboard.py", "--serial", "emulator-5554", "--hide-keyboard"]
    )

    with pytest.raises(SystemExit) as exc:
        keyboard.main()

    assert exc.value.code == 0
    assert "No keyboard shown" in capsys.readouterr().out
    assert not [c for c in calls if "keyevent" in c], f"a key event was sent anyway: {calls}"


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
