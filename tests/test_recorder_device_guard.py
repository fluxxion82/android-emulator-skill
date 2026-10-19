"""The recorder refuses to change a device it cannot prove is an emulator.

`adb devices` lists a physical phone before `emulator-5554`, and the recorder's
serial is optional: with none given, adb uses $ANDROID_SERIAL or the only device
attached. Fixtures that change the device they run on -- launch, tap, grant,
broadcast, override the display, write files -- are marked ``emulator_only``,
and `record()` refuses the whole run, before any adb call, unless the target is
provably an emulator. The rule is the one tests/conftest.py's
``emulator_only_device`` applies to live tests: the serial starts `emulator-`.

Every adb call here is faked to raise, so a refusal that came too late -- after
the getprop that opens a run -- fails loudly instead of reaching a device. The
"phone" is a made-up serial in the shape of a USB one; no real device's serial
appears in this file.
"""

from __future__ import annotations

import shlex

import pytest
import record_fixtures as rf

PHONE_SHAPED = "98ZZ1FFBA00000"
EMULATOR = "emulator-5554"

MUTATING = sorted(f.name for f in rf.FIXTURES if f.emulator_only)
READ_ONLY = "cmd_statusbar_help"

# Fixtures that run no device command at all: the throwaway-AVD lifecycle works
# on the host, in a private AVD home, and the Java sdkmanager listings read the
# host's SDK. Named so the rule below cannot be satisfied by hiding a device
# call inside a capture callable.
HOST_ONLY = frozenset(
    {
        "avdmanager_create",
        "avd_config_ini",
        "avdmanager_delete",
        "avdmanager_delete_missing",
        "sdkmanager_java_list_installed",
        "sdkmanager_java_list",
        # Host adb aimed at a serial that is not attached: it reaches no device.
        "adb_waiting_for_device",
    }
)

# Token sequences that change a device. A `wm size` / `wm density` with a
# further argument (a value, or `reset`) is handled separately: the bare
# command only reads.
CHANGES_THE_DEVICE = (
    ("am", "start"),
    ("am", "broadcast"),
    ("am", "profile"),
    ("am", "force-stop"),
    ("pm", "grant"),
    ("pm", "revoke"),
    ("pm", "clear"),
    ("settings", "put"),
    ("logcat", "-c"),
    ("emu", "sms", "send"),
    ("emu", "avd", "snapshot", "load"),
    ("emu", "avd", "snapshot", "save"),
    ("emu", "avd", "snapshot", "delete"),
    ("cmd", "notification", "post"),
    ("input",),
    ("mkdir",),
    ("ln",),
    ("rm",),
    ("touch",),
)


class _AdbReachedError(Exception):
    """Raised by the fake adb: the run got as far as talking to a device."""


@pytest.fixture
def adb_calls(monkeypatch, tmp_path):
    """Fake every subprocess call to raise, and keep the recorder off the real corpus."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        raise _AdbReachedError(cmd)

    monkeypatch.setattr(rf.subprocess, "run", fake_run)
    monkeypatch.setattr(rf, "RECORDED_ROOT", tmp_path)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    return calls


@pytest.mark.parametrize("name", MUTATING)
def test_a_fixture_that_changes_the_device_is_refused_for_a_phone(adb_calls, tmp_path, name):
    assert rf.record(PHONE_SHAPED, {name}, "guard") == 2
    assert adb_calls == [], f"{name}: adb was called before the refusal"
    assert list(tmp_path.iterdir()) == [], "the refused run created a profile directory"


def test_the_refusal_says_why_and_what_to_do(adb_calls, capsys):
    rf.record(PHONE_SHAPED, {"run_as_ls_symlink_space"}, "guard")
    err = capsys.readouterr().err
    assert repr(PHONE_SHAPED) in err
    assert "not an emulator" in err
    assert "--serial emulator-" in err


def test_no_serial_is_not_proof_of_an_emulator(adb_calls):
    """adb would pick the only device attached -- which may be a phone."""
    assert rf.record(None, {"am_profile_hang"}, "guard") == 2
    assert adb_calls == []


def test_android_serial_naming_a_phone_is_refused(adb_calls, monkeypatch):
    """With no --serial, adb uses $ANDROID_SERIAL, so that is what is checked."""
    monkeypatch.setenv("ANDROID_SERIAL", PHONE_SHAPED)
    assert rf.record(None, {"am_profile_hang"}, "guard") == 2
    assert adb_calls == []


def test_a_mixed_selection_is_refused_whole(adb_calls):
    """One mutating fixture in the selection stops the read-only ones too."""
    assert rf.record(PHONE_SHAPED, {READ_ONLY, "input_text_nonascii"}, "guard") == 2
    assert adb_calls == []


def test_an_emulator_serial_gets_past_the_guard(adb_calls):
    with pytest.raises(_AdbReachedError):
        rf.record(EMULATOR, {"run_as_ls_symlink_space"}, "guard")
    assert adb_calls[0][:3] == ["adb", "-s", EMULATOR]


def test_android_serial_naming_an_emulator_gets_past_the_guard(adb_calls, monkeypatch):
    monkeypatch.setenv("ANDROID_SERIAL", EMULATOR)
    with pytest.raises(_AdbReachedError):
        rf.record(None, {"run_as_ls_symlink_space"}, "guard")


def test_a_read_only_fixture_is_not_refused(adb_calls):
    """The guard is about changing a device, not about reading one."""
    assert not rf.FIXTURES[[f.name for f in rf.FIXTURES].index(READ_ONLY)].emulator_only
    with pytest.raises(_AdbReachedError):
        rf.record(PHONE_SHAPED, {READ_ONLY}, "guard")


def _tokens(argv: list[str]) -> list[str]:
    """Shell words in ``argv``, with quoted inner commands (`sh -c '...'`) split too."""
    out: list[str] = []
    for arg in argv:
        words = shlex.split(arg) if any(c.isspace() for c in arg) else [arg]
        for word in words:
            out.extend(_tokens([word]) if any(c.isspace() for c in word) else [word])
    return out


def _changes_the_device(argv: list[str]) -> bool:
    words = _tokens(argv)
    for at in range(len(words)):
        for sequence in CHANGES_THE_DEVICE:
            if tuple(words[at : at + len(sequence)]) == sequence:
                return True
        if words[at] == "wm" and at + 2 < len(words) and words[at + 1] in ("size", "density"):
            return True
    return False


def test_every_fixture_that_changes_the_device_is_marked_emulator_only():
    """The ratchet: a new fixture that changes the device must say so.

    A fixture changes the device if it has setup or teardown steps (they exist
    to put the device into a state and back), runs a capture callable that
    drives it, or its own command contains one of CHANGES_THE_DEVICE.
    """
    unmarked = []
    for fixture in rf.FIXTURES:
        if fixture.emulator_only or fixture.name in HOST_ONLY:
            continue
        callable_capture = fixture.capture_with or fixture.capture_bytes
        if (
            fixture.setup
            or fixture.teardown
            or callable_capture
            or _changes_the_device(fixture.args)
        ):
            unmarked.append(fixture.name)
    assert not unmarked, f"these change the device but are not emulator_only: {unmarked}"


def test_the_host_only_exemptions_run_no_device_command():
    for fixture in rf.FIXTURES:
        if fixture.name in HOST_ONLY:
            assert not (fixture.args or fixture.setup or fixture.teardown), fixture.name


def test_the_word_rule_sees_into_quoted_inner_commands():
    """`run-as <pkg> sh -c 'mkdir ... && ln -s ...'` is one argv entry to adb."""
    inner = "run-as com.example.composefixture sh -c 'mkdir -p d && ln -s a b && ls -la'"
    assert _changes_the_device(["shell", inner])
    assert not _changes_the_device(["shell", "run-as com.example.composefixture ls -la"])
    assert not _changes_the_device(["shell", "wm", "size"])
    assert _changes_the_device(["shell", "wm", "size", "reset"])
    assert not _changes_the_device(["shell", "dumpsys", "input_method"])
