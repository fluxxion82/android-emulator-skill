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
``--hide-keyboard`` / ``--dismiss`` read ``dumpsys input_method`` before
pressing BACK, and the double answers that call with RECORDED output --
``dumpsys_input_method_shown``, captured on ``emulator-api35`` with a keyboard
up and on ``pixel4xl-api33`` with none. The corpus therefore supplies both
states the decision turns on, and neither is written here::

    emulator-api35    mInputShown=true
                      mIsInputViewShown=true mStatusIcon=0

    pixel4xl-api33    mShowRequested=false mShowExplicitlyRequested=false
                        mShowForced=false mInputShown=false
                      mIsInputViewShown=false mStatusIcon=0

That API 33 line is why these read the corpus rather than a one-line
stand-in: three other ``mShow*`` fields precede ``mInputShown`` on it, so a
parser taking the line's first token answers a different question -- and a
hand-written fixture would never have shown that the fields share a line.
``mImeWindowVis``, which the register first suggested keying on, appears on
NEITHER profile.

Whether a given recording means "shown" is decided here by a rule of the
test's own, ``ime_state_of`` -- not by asking the function under test -- so the
two can disagree and be caught.
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


IME_FIXTURE = "dumpsys_input_method_shown"

# A service answering with nothing this parser recognises. Empty output is a
# value, not invented tool output, and it stands for the third state -- "I
# could not tell" -- which is the one that must not press BACK.
IME_UNREADABLE = ""


def ime_state_of(text: str) -> bool:
    """Does this recording show a keyboard? The test's own rule, not the parser's.

    Deliberately a *different* rule from ``keyboard.parse_ime_shown`` -- a plain
    substring, against a word-boundary regex over two fields in preference
    order -- so "the parser reads the recording the way the recording reads" is
    an assertion rather than a tautology.

    The field name is spelled out here rather than read from
    ``keyboard.IME_SHOWN_FIELD`` for the same reason: an oracle that imports
    the constant under test moves with it, and re-pointing that constant is one
    of the mutations these tests exist to catch. It is 17 characters read off
    two recordings, not a transcript.
    """
    return "mInputShown=true" in text


def _recorded_ime(shown: bool) -> str:
    """The recorded `dumpsys input_method` of a profile in the wanted state.

    Profiles are searched by what they show rather than named, so re-recording
    on other devices keeps these tests meaningful; if no profile is in that
    state any more, this fails loudly instead of quietly testing one branch
    twice.
    """
    for profile in _profiles_with_the_ime_fixture():
        text = profile.text(IME_FIXTURE)
        if ime_state_of(text) is shown:
            return text
    pytest.fail(
        f"No recorded profile has {IME_FIXTURE} with a keyboard "
        f"{'shown' if shown else 'hidden'}. Record one: "
        f"python tests/record_fixtures.py --only {IME_FIXTURE}"
    )


def _profiles_with_the_ime_fixture() -> list:
    """Every recorded profile that captured the IME dump."""
    from conftest import RECORDED_ROOT, RecordedFixtures, _available_profiles

    accessors = [RecordedFixtures(RECORDED_ROOT / name) for name in _available_profiles()]
    return [accessor for accessor in accessors if accessor.has(IME_FIXTURE)]


def _patch_run(monkeypatch, ime_state: str = IME_UNREADABLE):
    """Record every argv adb_exec would run; skip real sleeps.

    ``ime_state`` is what ``dumpsys input_method`` answers. It defaults to the
    unreadable state so that a test which does not care about the IME cannot
    accidentally depend on one -- the tests that do care pass a recording.
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
    calls = _patch_run(monkeypatch, ime_state=_recorded_ime(shown=True))
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

    The "no keyboard" state is a real device's, not a written one: it is the
    profile whose recording says so.
    """
    calls = _patch_run(monkeypatch, ime_state=_recorded_ime(shown=False))
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
    calls = _patch_run(monkeypatch, ime_state=IME_UNREADABLE)
    sim = keyboard.KeyboardSimulator(serial="emulator-5554")

    success, message = getattr(sim, action)()

    assert success is False
    assert keyboard.IME_SHOWN_FIELD in message
    assert "--button back" in message, "the failure names no remedy"
    assert not [c for c in calls if "keyevent" in c], f"a key event was sent anyway: {calls}"


def test_the_ime_state_is_read_before_any_key_event(monkeypatch):
    """Order matters: a check after the press would be decoration."""
    calls = _patch_run(monkeypatch, ime_state=_recorded_ime(shown=True))
    keyboard.KeyboardSimulator(serial="emulator-5554").hide_keyboard()

    assert calls[0][3:] == ["shell", "dumpsys", "input_method"], f"first call was {calls[0]}"
    assert len(calls) == 2, f"expected the check then the key event, got {calls}"


# ---------------------------------------------------------------------------
# The parser, against every profile that recorded the dump.
# ---------------------------------------------------------------------------


def test_the_ime_dump_is_recorded_on_every_profile(any_profile):
    """The fixture the decision rests on, present wherever a device was recorded.

    Not a skip-if-absent: this dump is two filtered lines, it carries no
    private content, and a profile without it means the next re-recording
    silently narrowed what C8 is checked against.
    """
    assert any_profile.has(IME_FIXTURE), (
        f"{any_profile.name} has no {IME_FIXTURE}. Record it: "
        f"python tests/record_fixtures.py --profile {any_profile.name} "
        f"--only {IME_FIXTURE}"
    )


def test_the_parser_answers_every_recorded_profile(any_profile):
    """An invariant across API levels: the field we key on is there, and read.

    API 33 and API 35 print it differently -- alone on its line on 35, and
    fourth on a line of `mShow*` fields on 33 -- so "the parser returns a
    decision" has to hold on both. None here would mean the CLI refuses to hide
    a keyboard on that Android version.
    """
    text = any_profile.text(IME_FIXTURE)
    parsed = keyboard.parse_ime_shown(text)

    assert parsed is not None, (
        f"{any_profile.name} recorded the dump, and the parser found neither "
        f"{' nor '.join(keyboard.IME_SHOWN_FIELDS)} in it:\n{text}"
    )
    assert parsed is ime_state_of(
        text
    ), f"the parser and the recording disagree on {any_profile.name}:\n{text}"


def test_every_field_the_parser_keys_on_is_in_the_recording(any_profile):
    """The primary field must be present, not merely covered by the fallback.

    ``parse_ime_shown`` falls back to ``mIsInputViewShown``, and on these two
    recordings the two fields agree -- so re-pointing the primary key at
    something that does not exist still produces the right answers, and every
    behavioural test stays green. Measured, by mutation. This is the assertion
    that catches it: both names are in both recordings, and a key nobody can
    find is a defect whether or not the fallback saves it today.
    """
    dump = any_profile.text(IME_FIXTURE)
    missing = [field for field in keyboard.IME_SHOWN_FIELDS if field not in dump]
    assert not missing, (
        f"{any_profile.name} does not print {missing}. Either the field was "
        f"renamed on that API level -- in which case the parser needs the new "
        f"name, not a fallback that happens to agree -- or the constant is "
        f"pointing at something nobody has observed:\n{dump}"
    )


def test_the_field_the_register_suggested_is_on_no_profile(any_profile):
    """`mImeWindowVis` was the plausible name, and it is on neither device.

    Pinned so a later "improvement" that keys on it is a red test rather than a
    keyboard that is never seen. This is the repo's founding bug class in one
    line: a field name nobody looked at.
    """
    assert "mImeWindowVis" not in any_profile.text(IME_FIXTURE)


def test_the_corpus_holds_both_ime_states():
    """Anti-vacuity for the two behaviour tests above.

    They ask the corpus for a shown state and a hidden one. If a re-recording
    left every profile in the same state, `_recorded_ime` would fail -- but it
    would fail inside a test whose subject is something else, so this says it
    plainly instead.
    """
    states = {ime_state_of(p.text(IME_FIXTURE)) for p in _profiles_with_the_ime_fixture()}
    assert states == {True, False}, (
        f"the recorded profiles only show {states or 'nothing'}; C8's decision "
        f"has two branches and the corpus must exercise both"
    )


def test_the_parser_reads_its_own_field_and_not_a_neighbour(any_profile):
    """Flip only `mInputShown` in the recording, and the answer must follow it.

    API 33 puts three other `mShow*` flags on the same line, ahead of it. As
    captured they all read false, so a parser taking the line's first field
    gets the right answer for the wrong reason and every state test still
    passes -- measured, by mutation. Flipping just this field is what makes the
    two readings disagree.

    The dump is transformed, not written: `<recording>.replace(...)`, which is
    the derivation the fixture policy allows and the reason it insists the
    recording is the subject.
    """
    dump = any_profile.text(IME_FIXTURE)
    field = keyboard.IME_SHOWN_FIELD
    inverted = dump.replace(f"{field}=false", f"{field}=SWAP").replace(
        f"{field}=true", f"{field}=false"
    )
    inverted = inverted.replace(f"{field}=SWAP", f"{field}=true")

    assert inverted != dump, f"{any_profile.name}: the recording has no {field} to flip"
    assert keyboard.parse_ime_shown(inverted) is (not ime_state_of(dump)), (
        f"{any_profile.name}: flipping {field} did not change the answer, so the "
        f"parser is reading something else:\n{inverted}"
    )


def test_at_least_one_profile_puts_other_fields_on_that_line(any_profile):
    """Anti-vacuity for the test above: the neighbours have to exist somewhere.

    If every recorded profile printed `mInputShown` alone on its line, the flip
    test could not tell a field-anchored parser from a line-anchored one, and
    the API 33 hazard would be untested rather than tested-and-passing. Written
    per profile so the failure names the one that lost them.
    """
    crowded = [
        profile.name
        for profile in _profiles_with_the_ime_fixture()
        if any(
            keyboard.IME_SHOWN_FIELD in line and line.strip().count("=") > 1
            for line in profile.text(IME_FIXTURE).splitlines()
        )
    ]
    assert crowded, (
        f"no recorded profile prints another field on the {keyboard.IME_SHOWN_FIELD} "
        f"line any more (checked while reading {any_profile.name}); the "
        f"neighbouring-field test can no longer discriminate"
    )


def test_an_absent_field_is_not_a_hidden_keyboard():
    """Three states, and the third is not the second.

    None means neither field was there. A parser that folded that into False
    would put the unconditional BACK press straight back.
    """
    assert keyboard.parse_ime_shown(IME_UNREADABLE) is None


def test_the_second_field_answers_when_the_first_is_absent(any_profile):
    """`mIsInputViewShown` exists on both profiles and is the documented backup.

    Built by dropping the `mInputShown` line from a recording rather than by
    writing a dump: what is under test is the fallback, and the line it falls
    back to is the recorded one.
    """
    dump = any_profile.text(IME_FIXTURE)
    without_primary = "\n".join(
        line for line in dump.splitlines() if keyboard.IME_SHOWN_FIELD not in line
    )

    assert keyboard.IME_VIEW_SHOWN_FIELD in without_primary, (
        f"{any_profile.name} does not record {keyboard.IME_VIEW_SHOWN_FIELD}, "
        f"so the fallback has no evidence behind it"
    )
    assert keyboard.parse_ime_shown(without_primary) is ime_state_of(dump), (
        f"{any_profile.name}: the backup field disagrees with the primary one "
        f"on the same recording:\n{dump}"
    )


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
    calls = _patch_run(monkeypatch, ime_state=_recorded_ime(shown=True))
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
    calls = _patch_run(monkeypatch, ime_state=_recorded_ime(shown=False))
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
    _patch_run(monkeypatch, ime_state=_recorded_ime(shown=True))
    monkeypatch.setattr(keyboard, "resolve_device_identifier", lambda arg: arg)
    monkeypatch.setattr(
        keyboard.sys, "argv", ["keyboard.py", "--serial", "emulator-5554", "--dismiss"]
    )

    with pytest.raises(SystemExit) as exc:
        keyboard.main()

    assert exc.value.code == 0
    assert "Dismissed keyboard" in capsys.readouterr().out
