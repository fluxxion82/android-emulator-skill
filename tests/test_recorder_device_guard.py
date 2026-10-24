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
        # Host adb with no target, run only while two or more devices are
        # attached and with ANDROID_SERIAL removed: adb refuses it unaddressed.
        "adb_more_than_one_device",
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
    # The env var is resolved into the argv, not left for adb to read.
    assert adb_calls[0][:3] == ["adb", "-s", EMULATOR]


def test_no_serial_at_all_is_refused_even_for_a_read_only_fixture(adb_calls, capsys):
    """Every run reads getprop first; with no serial adb would pick the device."""
    assert rf.record(None, {READ_ONLY}, "guard") == 2
    assert adb_calls == []
    assert "no target serial" in capsys.readouterr().err


def test_a_device_argv_cannot_be_built_without_a_serial():
    for missing in (None, ""):
        with pytest.raises(ValueError):
            rf._adb_argv(missing, ["shell", "true"])
    with pytest.raises(ValueError):
        rf._run_host(["adb", "shell", "true"])
    with pytest.raises(ValueError):
        rf._run_host_adb(["shell", "true"])


class _FakeClock:
    """``time`` for the recorder: sleeping advances the clock, so polls end at once."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class _FakeChild:
    """A Popen child that never exits on its own, like `input` into a stalled window."""

    def __init__(self, argv, calls, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        self.argv = argv
        self.text = kwargs.get("text", False)
        self.stopped = False
        self.returncode = 0

    def _stop(self) -> None:
        self.stopped = True

    kill = terminate = _stop

    def wait(self, timeout=None):
        if not self.stopped:
            raise rf.subprocess.TimeoutExpired(self.argv, timeout)
        return 0

    def communicate(self, timeout=None):
        self.wait(timeout)
        return ("", "") if self.text else (b"", b"")


# A run names no device but the resolved one, except these, each by design:
# host-scope adb (no device at all), and adb_waiting_for_device, whose
# ANDROID_SERIAL pins a serial that is not attached.
NOT_ATTACHED = "no-such-serial-xyz"


def test_every_adb_call_of_a_full_run_names_the_resolved_serial(monkeypatch, tmp_path):
    """R1: the whole recorder, every device fixture, the serial from $ANDROID_SERIAL alone.

    Each subprocess answers exit 0 with empty output (getprop says 33, so the run
    starts); most captures then fail, after issuing the commands that lead up
    to them -- which is what is checked. The steps that write (reboot,
    create-user, the key event, the window mark) are required to appear, so a
    run that died early cannot pass by issuing nothing.
    """
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        out = "33\n" if "getprop" in argv else ""
        if not kwargs.get("text"):
            return rf.subprocess.CompletedProcess(argv, 0, out.encode(), b"")
        return rf.subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(rf.subprocess, "run", fake_run)
    monkeypatch.setattr(rf.subprocess, "Popen", lambda argv, **kw: _FakeChild(argv, calls, **kw))
    monkeypatch.setattr(rf, "time", _FakeClock())
    monkeypatch.setattr(rf, "RECORDED_ROOT", tmp_path)
    monkeypatch.setenv("ANDROID_SERIAL", EMULATOR)

    # Every fixture but the SDK-tool ones, which run no adb and read real files
    # off the host; the two host adb ones stay in, to hold their exemptions.
    sdk_tools = HOST_ONLY - {"adb_waiting_for_device", "adb_more_than_one_device"}
    rf.record(None, rf._known_names() - sdk_tools, "full")

    adb = [(argv, env) for argv, env in calls if argv and argv[0] == "adb"]
    unnamed = [
        argv
        for argv, env in adb
        if argv[1:3] != ["-s", EMULATOR]
        and argv[1] not in rf._HOST_ADB_SUBCOMMANDS
        and not (env and env.get("ANDROID_SERIAL") == NOT_ATTACHED)
    ]
    assert not unnamed, f"adb argv without -s {EMULATOR}: {unnamed}"

    issued = [" ".join(argv) for argv, _env in adb]
    for step in (
        f"adb -s {EMULATOR} reboot",
        f"adb -s {EMULATOR} shell pm create-user",
        f"adb -s {EMULATOR} shell input keyevent",
        f"adb -s {EMULATOR} shell log -t aes-recorder",
    ):
        assert any(line.startswith(step) for line in issued), f"never issued: {step}"


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
