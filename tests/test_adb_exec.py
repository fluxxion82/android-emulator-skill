"""One bounded entry point for adb, with errors an agent can act on.

Two problems this replaces, measured with AST across the skill:

1. **69 unbounded `subprocess.run`/`Popen` calls in 17 files.** An unbounded adb
   call does not merely hang the caller — it wedges the adb connection for
   whatever runs next, which presents as a hang with no diagnosis. Both
   log_monitor's `--duration` (A2) and `dumpsys window` were instances.

2. **Raw adb errors reaching the agent.** For an agent, stderr *is* the retry
   prompt. "adb: more than one device/emulator" says nothing about what to do;
   "pass --serial emulator-5554" does. Every error raised here must name a
   remedy, and that is asserted below rather than left to good intentions.

Error strings are matched against recorded adb output where a fixture exists,
and the two profiles disagree about the prefix: `adb_shell_device_not_found`
(emulator-api35) is `adb: device '...' not found`, while
`adb_device_not_found` (pixel4xl-api33) is `error: ...`. The matcher
therefore accepts EITHER prefix -- anchoring it on one would silently stop
classifying the other profile's failure -- but only at the start of a line:
a command run on the device writes its stderr through adb's, and a file named
`unauthorized.db` is not an unauthorized device (Codex review of #29).

This docstring used to claim the fixture "proves the prefix is `error:` and not
`adb:`". It proves no such thing — the other profile shows the opposite. Which
is the point: prose asserting a precision the code does not have is the same
failure this suite exists to catch, just moved from the assertion into the
comment above it.
"""

from __future__ import annotations

import subprocess

import pytest
from fake_toolchain import (
    COLLIDING_FILE_NAMES,
    FakeToolchain,
    Rule,
    assert_refusal,
    recorded_missing_file,
)

from common import adb_exec, logcat


@pytest.fixture
def fake_run(monkeypatch):
    """Install a fake subprocess.run and capture how it was called."""
    captured: dict = {}

    def _install(stdout="", stderr="", returncode=0, raises=None):
        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(adb_exec.subprocess, "run", _run)
        return captured

    _install.captured = captured
    return _install


# ---------------------------------------------------------------------------
# Bounding.
# ---------------------------------------------------------------------------


def test_every_call_is_bounded_by_default(fake_run):
    """The default must be a timeout, not the absence of one."""
    fake_run()
    adb_exec.run_adb("shell", None, "echo", "hi")
    assert fake_run.captured["kwargs"].get("timeout"), "run_adb issued an unbounded call"


def test_explicit_timeout_is_honoured(fake_run):
    fake_run()
    adb_exec.run_adb("shell", None, "echo", "hi", timeout=7)
    assert fake_run.captured["kwargs"]["timeout"] == 7


def test_timeout_raises_a_typed_error_naming_the_command(fake_run):
    """A timeout must say what timed out, or it is unactionable."""
    fake_run(raises=subprocess.TimeoutExpired(cmd="adb", timeout=5))
    with pytest.raises(adb_exec.AdbTimeoutError) as excinfo:
        adb_exec.run_adb("shell", None, "dumpsys", "window", timeout=5)

    message = str(excinfo.value)
    assert "dumpsys" in message
    assert "5" in message


def test_never_uses_a_shell(fake_run):
    """CLAUDE.md rule 4, enforced at the one place every call now goes."""
    fake_run()
    adb_exec.run_adb("shell", None, "echo", "hi")
    assert fake_run.captured["kwargs"].get("shell") is not True


# ---------------------------------------------------------------------------
# Command construction.
# ---------------------------------------------------------------------------


def test_serial_is_threaded_into_the_command(fake_run):
    fake_run()
    adb_exec.run_adb("shell", "emulator-5554", "echo", "hi")
    assert fake_run.captured["cmd"][:3] == ["adb", "-s", "emulator-5554"]


def test_no_serial_omits_the_flag(fake_run):
    fake_run()
    adb_exec.run_adb("devices", None, "-l")
    assert "-s" not in fake_run.captured["cmd"]


# ---------------------------------------------------------------------------
# Results.
# ---------------------------------------------------------------------------


def test_result_carries_the_command_for_diagnosis(fake_run):
    fake_run(stdout="hi\n")
    result = adb_exec.run_adb("shell", None, "echo", "hi")
    assert result.ok
    assert result.stdout == "hi\n"
    assert result.command[0] == "adb"


def test_nonzero_exit_is_not_an_exception_by_default(fake_run):
    """Many adb commands report a real answer through a non-zero status."""
    fake_run(returncode=1, stderr="something")
    result = adb_exec.run_adb("shell", None, "false")
    assert not result.ok
    assert result.returncode == 1


def test_check_raises_on_nonzero(fake_run):
    fake_run(returncode=1, stderr="boom")
    with pytest.raises(adb_exec.AdbCommandError):
        adb_exec.run_adb("shell", None, "false", check=True)


# ---------------------------------------------------------------------------
# Error mapping. Each case must name a remedy, not just a diagnosis.
# ---------------------------------------------------------------------------


def test_multiple_devices_is_typed_and_names_the_remedy(fake_run):
    """The most common agent-facing adb failure."""
    fake_run(returncode=1, stderr="adb: more than one device/emulator\n")
    with pytest.raises(adb_exec.MultipleDevicesError) as excinfo:
        adb_exec.run_adb("shell", None, "echo", "hi", check=True)
    assert "--serial" in str(excinfo.value), "the error does not say what to do next"


def test_device_not_found_is_typed_and_names_the_remedy(fake_run, recorded_anywhere):
    """Matched against real adb output, without depending on the prefix.

    This fixture happens to carry `error:`; the emulator profile's
    `adb_shell_device_not_found` carries `adb:` for the same condition. The
    assertion below does not care, and must not: see
    `test_the_not_found_match_is_prefix_agnostic`, which runs both.

    This is host adb-client output, identical whatever device is attached,
    so it is looked up in whichever profile happens to hold it.
    """
    fixture = recorded_anywhere("adb_device_not_found")
    assert "not found" in fixture

    fake_run(returncode=1, stderr=fixture)
    with pytest.raises(adb_exec.DeviceNotFoundError) as excinfo:
        adb_exec.run_adb("shell", "no-such-serial-xyz", "echo", "hi", check=True)

    message = str(excinfo.value)
    assert "no-such-serial-xyz" in message
    assert "adb devices" in message, "the error should say how to see what is attached"


def test_no_devices_at_all_is_typed(fake_run):
    fake_run(returncode=1, stderr="adb: no devices/emulators found\n")
    with pytest.raises(adb_exec.DeviceNotFoundError) as excinfo:
        adb_exec.run_adb("shell", None, "echo", "hi", check=True)
    assert "emulator" in str(excinfo.value).lower()


def test_offline_device_is_typed_and_names_the_remedy(fake_run):
    fake_run(returncode=1, stderr="error: device offline\n")
    with pytest.raises(adb_exec.DeviceOfflineError) as excinfo:
        adb_exec.run_adb("shell", "emulator-5554", "echo", "hi", check=True)
    assert "reconnect" in str(excinfo.value).lower() or "adb kill-server" in str(excinfo.value)


def test_unauthorized_device_is_typed_and_names_the_remedy(fake_run):
    fake_run(returncode=1, stderr="error: device unauthorized.\n")
    with pytest.raises(adb_exec.DeviceUnauthorizedError) as excinfo:
        adb_exec.run_adb("shell", "ABC123", "echo", "hi", check=True)
    assert "authorize" in str(excinfo.value).lower() or "prompt" in str(excinfo.value).lower()


def test_missing_adb_is_typed_and_names_the_remedy(fake_run):
    fake_run(raises=FileNotFoundError("adb"))
    with pytest.raises(adb_exec.AdbNotInstalledError) as excinfo:
        adb_exec.run_adb("devices", None)
    assert "platform-tools" in str(excinfo.value)


def test_device_errors_are_detected_even_without_check(fake_run):
    """A device-level failure is a hard error regardless of `check`.

    'more than one device' means the command never ran, which is different from
    a command that ran and returned non-zero. Callers should not have to opt in
    to noticing that.
    """
    fake_run(returncode=1, stderr="adb: more than one device/emulator\n")
    with pytest.raises(adb_exec.MultipleDevicesError):
        adb_exec.run_adb("shell", None, "echo", "hi")


@pytest.mark.parametrize(
    "error_class",
    [
        adb_exec.AdbTimeoutError,
        adb_exec.MultipleDevicesError,
        adb_exec.DeviceNotFoundError,
        adb_exec.DeviceOfflineError,
        adb_exec.DeviceUnauthorizedError,
        adb_exec.AdbNotInstalledError,
        adb_exec.AdbCommandError,
    ],
)
def test_every_error_type_derives_from_one_base(error_class):
    """Callers must be able to catch the whole family in one except clause."""
    assert issubclass(error_class, adb_exec.AdbError)


# ---------------------------------------------------------------------------
# A plain non-zero exit must not be mistaken for a device problem.
# ---------------------------------------------------------------------------


def test_shell_exit_status_is_passed_through(fake_run):
    """`adb shell exit 7` propagates 7; that is the command's answer, not adb's.

    Verified on a real device: the device's exit status reaches the caller.
    """
    fake_run(returncode=7)
    result = adb_exec.run_adb("shell", "emulator-5554", "exit", "7")
    assert result.returncode == 7
    assert not result.ok


# ---------------------------------------------------------------------------
# Exceptions must carry structure, not just a formatted string.
# ---------------------------------------------------------------------------


def test_command_failure_carries_the_result(fake_run):
    """A caller that needs stderr should not have to re-parse the message.

    screenshot_utils wrote `except AdbCommandError as e: ... {e.stderr}` against
    an attribute that did not exist -- an AttributeError waiting on the failure
    path, which the sibling `except Exception` could not catch because it was
    raised from inside the handler.
    """
    fake_run(returncode=2, stdout="out", stderr="boom")
    with pytest.raises(adb_exec.AdbCommandError) as excinfo:
        adb_exec.run_adb("shell", None, "false", check=True)

    error = excinfo.value
    assert error.stderr == "boom"
    assert error.stdout == "out"
    assert error.returncode == 2
    assert error.result.command[0] == "adb"


def test_every_error_carries_the_command(fake_run):
    """Diagnosing a failure starts with knowing what was run."""
    fake_run(raises=subprocess.TimeoutExpired(cmd="adb", timeout=1))
    with pytest.raises(adb_exec.AdbTimeoutError) as excinfo:
        adb_exec.run_adb("shell", None, "sleep", "9", timeout=1)
    assert excinfo.value.command[:1] == ["adb"]


# ---------------------------------------------------------------------------
# The RuntimeError base is deliberate API, and load-bearing.
# ---------------------------------------------------------------------------


def test_adb_error_is_a_runtime_error():
    """`screen_mapper` relies on this to route remedies into its normal output.

    Several CLI boundaries pre-date adb_exec and catch RuntimeError from
    `resolve_device_identifier`; inheriting from it means an adb failure reaches
    those handlers already carrying its remedy. That is intentional, so it is
    pinned here -- otherwise a well-meaning refactor to `Exception` would
    silently stop those messages reaching the user.
    """
    assert issubclass(adb_exec.AdbError, RuntimeError)


# ---------------------------------------------------------------------------
# Classification must not be fooled by device-side output.
# ---------------------------------------------------------------------------


def test_device_output_cannot_forge_a_device_error(fake_run):
    """Only adb's own stderr may be classified, never the command's stdout.

    A shell command can legitimately print "device not found" -- a log line, a
    grep hit, an app's own error. Classifying stdout let device-generated text
    forge a host-level failure, which for an agent means a false remedy.
    """
    fake_run(returncode=1, stdout="E/Sensors: device not found\n", stderr="")
    result = adb_exec.run_adb("shell", "emulator-5554", "logcat", "-d")
    assert result.returncode == 1, "device output was misclassified as an adb device error"


def test_real_adb_device_error_is_still_classified(fake_run, recorded_anywhere):
    """Guard against over-correcting: adb's own stderr must still be read."""
    fake_run(returncode=1, stdout="", stderr=recorded_anywhere("adb_device_not_found"))
    with pytest.raises(adb_exec.DeviceNotFoundError):
        adb_exec.run_adb("get-state", "no-such-serial-xyz")


# ---------------------------------------------------------------------------
# The positional-serial footgun.
# ---------------------------------------------------------------------------


def test_a_flag_passed_as_the_serial_is_rejected(fake_run):
    """`run_adb("devices", "-l")` would silently become `adb -s -l devices`."""
    fake_run()
    with pytest.raises(ValueError, match="serial"):
        adb_exec.run_adb("devices", "-l")


# ---------------------------------------------------------------------------
# adb can fail to run in more ways than "missing".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "situation"),
    [
        (PermissionError(13, "Permission denied"), "a non-executable file named adb"),
        (IsADirectoryError(21, "Is a directory"), "the SDK root on PATH, so adb is a directory"),
        (NotADirectoryError(20, "Not a directory"), "a PATH entry that is not a directory"),
    ],
)
def test_adb_that_cannot_be_executed_is_an_adb_error(monkeypatch, error, situation):
    """Only FileNotFoundError was caught, so the rest escaped as tracebacks.

    Reproduced before fixing, with nothing but a `chmod 000` stub on PATH:

        PermissionError: [Errno 13] Permission denied: 'adb'

    reaching the user instead of a remedy. The directory case is the same shape
    as the `emulator` bug -- put the SDK ROOT on PATH rather than
    `platform-tools`, and the name resolves to a directory.

    Raised as AdbNotInstalledError rather than a new type: callers already
    handle it, and the remedy ("point PATH at platform-tools") is the same.
    """

    def _raise(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(adb_exec.subprocess, "run", _raise)

    with pytest.raises(adb_exec.AdbNotInstalledError) as excinfo:
        adb_exec.run_adb("devices", None)

    message = str(excinfo.value)
    assert "platform-tools" in message, f"the remedy is missing for {situation}"
    assert "not executable" in message or "not on PATH" in message


def test_the_not_found_match_is_prefix_agnostic(recorded_anywhere):
    """The two profiles disagree about the prefix, so neither ALONE may be anchored on.

    `adb -s <unknown> shell` prints `adb: device '...' not found` on
    emulator-api35 and `error: device '...' not found` on pixel4xl-api33. Both
    are recorded. A matcher anchored on one prefix silently stops classifying
    the other profile's failure, and the module docstring used to claim it was
    anchored on `error:` -- documentation asserting a precision the code never
    had. It is anchored on both now, at the start of a line.
    """
    for name in ("adb_shell_device_not_found", "adb_device_not_found"):
        text = recorded_anywhere(name)
        error = adb_exec._classify(text, "no-such-serial-xyz")
        assert isinstance(
            error, adb_exec.DeviceNotFoundError
        ), f"{name} ({text.strip()!r}) was not classified as a missing device"


# What every recorded adb device error must still be classified as, per name.
RECORDED_DEVICE_ERRORS = {
    "adb_shell_device_not_found": adb_exec.DeviceNotFoundError,
    "adb_device_not_found": adb_exec.DeviceNotFoundError,
    "adb_more_than_one_device": adb_exec.MultipleDevicesError,
}


def test_every_recorded_device_error_still_classifies_in_every_profile(any_profile):
    """The anchoring below must not cost a real adb error its type, in either profile.

    Every one this profile recorded is checked; across the profiles that is
    four recordings, two prefixes (`adb:` and `error:`) and two error types.
    """
    checked = [name for name in RECORDED_DEVICE_ERRORS if any_profile.has(name)]
    assert checked, "this profile recorded no adb device error at all"
    for name in checked:
        text = any_profile.text(name)
        error = adb_exec._classify(text, "no-such-serial-xyz")
        assert isinstance(
            error, RECORDED_DEVICE_ERRORS[name]
        ), f"{name}: {text.strip()!r} -> {error!r}"


# Each name's missing-file message matched one of _classify's checks when it
# looked for keywords anywhere (see fake_toolchain.COLLIDING_FILE_NAMES).
@pytest.mark.parametrize("name", COLLIDING_FILE_NAMES)
def test_a_device_commands_stderr_is_not_read_as_adbs(fake_run, recorded, name):
    """Codex review of #29: `cat: databases/unauthorized.db: No such file ...` exited 3.

    With file reads through run_argv, the REMOTE command's stderr reached
    `_classify`, which looked for "unauthorized" anywhere: model_inspector said
    the device had not authorised this computer, and "No such file or
    directory" was lost. adb's own errors start a line with `adb: ` or
    `error: ` (both recorded); a device command's do not.
    """
    stderr = recorded_missing_file(recorded, "databases", name)
    assert adb_exec.classify_device_error(stderr, "emulator-5554") is None

    fake_run(returncode=1, stderr=stderr.encode())
    cmd = ["adb", "-s", "emulator-5554", "shell", "-T", "run-as", "pkg", "cat", name]
    assert adb_exec.run_argv(cmd, serial="emulator-5554").returncode == 1

    fake_run(returncode=1, stderr=stderr)
    assert adb_exec.run_adb("shell", "emulator-5554", "cat", name).returncode == 1


def test_a_requested_serial_is_named_even_when_nothing_is_attached(monkeypatch):
    """Asking for a serial with no devices attached must still name the serial.

    The "not found" branch names it, but only when something IS attached, so
    the no-devices case dropped the one detail the caller supplied and
    answered "No devices connected". An agent's next move differs completely
    between "I named the wrong device" and "there is no device", and the
    message has to tell it which.

    Surfaced by test_agent_task_e2e's actionable-error test on a CI runner
    whose emulator had died mid-run -- the assertion that the error mentions
    the requested serial was right, and the code was not.
    """
    from common import device_utils

    monkeypatch.setattr(device_utils, "get_connected_devices", lambda: [])

    with pytest.raises(RuntimeError) as excinfo:
        device_utils.resolve_device_identifier("no-such-device")

    message = str(excinfo.value)
    assert "no-such-device" in message, f"the requested serial was dropped: {message}"
    assert "adb devices" in message, "the remedy is missing"


# ---------------------------------------------------------------------------
# run_argv: the byte reads run_adb cannot serve, failing the same way.
# ---------------------------------------------------------------------------


def test_run_argv_hands_back_the_bytes_untouched(fake_run, recorded):
    """A database is bytes; run_adb's raw_text would decode it. Recorded run-as read."""
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    stderr = recorded.text("run_as_cat_missing").encode()
    captured = fake_run(returncode=status["run_as_cat_missing"], stdout=b"", stderr=stderr)

    cmd = ["adb", "-s", "emulator-5554", "shell", "-T", "run-as", "pkg", "cat", "f"]
    result = adb_exec.run_argv(cmd, serial="emulator-5554")

    assert result.returncode == status["run_as_cat_missing"]
    assert result.stderr == stderr, "the device's own failure is the caller's to judge"
    assert captured["kwargs"]["text"] is False
    assert captured["kwargs"]["timeout"] == adb_exec.DEFAULT_TIMEOUT


def test_run_argv_types_adbs_own_failures_like_run_adb(fake_run, recorded_anywhere):
    """container.py answered these with "Command failed ...", exit 1, no remedy."""
    cmd = ["adb", "-s", "no-such-serial-xyz", "shell", "-T", "run-as", "pkg", "ls"]

    fake_run(returncode=1, stderr=recorded_anywhere("adb_shell_device_not_found").encode())
    with pytest.raises(adb_exec.DeviceNotFoundError):
        adb_exec.run_argv(cmd, serial="no-such-serial-xyz")

    fake_run(raises=FileNotFoundError("adb"))
    with pytest.raises(adb_exec.AdbNotInstalledError):
        adb_exec.run_argv(cmd, serial=None)

    fake_run(raises=subprocess.TimeoutExpired(cmd, 5))
    with pytest.raises(adb_exec.AdbTimeoutError):
        adb_exec.run_argv(cmd, serial=None, timeout=5)


@pytest.mark.parametrize(
    ("probe", "retains_error"),
    [
        pytest.param(
            (1, "", "error: device unauthorized.\n", None),
            True,
            id="nonzero-device-error",
        ),
        pytest.param((1, "", "ordinary failure\n", None), True, id="nonzero-other-error"),
        pytest.param(
            (None, "", "", subprocess.TimeoutExpired(cmd="adb get-state", timeout=30)),
            True,
            id="timeout",
        ),
        pytest.param((None, "", "", FileNotFoundError("adb")), True, id="adb-missing"),
        pytest.param((0, "device\n", "", None), False, id="device"),
        pytest.param((0, "offline\n", "", None), True, id="offline"),
        pytest.param((0, "", "", None), True, id="empty"),
        pytest.param(
            (0, "error: device offline\n", "", None),
            True,
            id="device-error-text",
        ),
    ],
)
def test_run_argv_only_suppresses_a_suspected_device_error_when_probe_reports_device(
    monkeypatch, probe, retains_error
):
    """The probe confirms reachability only with the documented ``device`` state.

    Asked for by the run-as readers alone (``confirm_device_errors``): theirs is
    the stderr a device command writes, which can carry a name an app chose.
    """
    calls: list[list[str]] = []
    probe_returncode, probe_stdout, probe_stderr, probe_exception = probe

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 1, b"", b"error: device offline\n")
        if probe_exception is not None:
            raise probe_exception
        return subprocess.CompletedProcess(cmd, probe_returncode, probe_stdout, probe_stderr)

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    cmd = ["adb", "-s", "emulator-5554", "shell", "-T", "run-as", "pkg", "cat", "f"]

    if retains_error:
        with pytest.raises(adb_exec.DeviceOfflineError) as excinfo:
            adb_exec.run_argv(cmd, serial="emulator-5554", confirm_device_errors=True)
        assert "reconnect" in str(excinfo.value).lower()
    else:
        result = adb_exec.run_argv(cmd, serial="emulator-5554", confirm_device_errors=True)
        assert result.returncode == 1
        assert result.stderr == b"error: device offline\n"

    assert len(calls) == 2
    assert calls[1][-1] == "get-state"


# ---------------------------------------------------------------------------
# Which stderr the get-state confirmation is for (final review, W4-D item 1).
#
# #32's confirmation exists because `adb shell` hands a REMOTE command's stderr
# on as adb's, and a file name can hold a newline and a whole `adb: ...` line.
# logcat's stderr holds no such name: a device error there is adb's own, and a
# probe answering `device` a moment later turned a real one into exit 1
# (anr_watcher --since) where log_monitor reports it as exit 3.
# ---------------------------------------------------------------------------


def _logcat_dump_command() -> list[str]:
    """The argv the --since/snapshot readers run: logd's records, dumped (common.logcat)."""
    return logcat.build_logcat_command("emulator-5554", since="01-01 00:00:00.000", binary=True)


def _adb_that_lost_the_device_then_found_it(monkeypatch, recorded, recorded_anywhere):
    """The first call fails with adb's recorded device error; get-state then answers `device`.

    Derived: `adb_shell_device_not_found` is adb's own stderr for a shell-service
    command whose device is not there, replayed as logcat's -- which adb error
    ends a dump mid-read is not recorded; the point is that the line is adb's.
    """
    calls: list[list[str]] = []
    lost = recorded_anywhere("adb_shell_device_not_found").encode()
    reachable = recorded.text("adb_get_state").encode()

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "get-state" in cmd:
            return subprocess.CompletedProcess(cmd, 0, reachable, b"")
        return subprocess.CompletedProcess(cmd, 1, b"", lost)

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


def _without_listing(calls: list[list[str]]) -> list[list[str]]:
    """The calls besides `adb devices`, which a device error's message asks to name serials."""
    return [call for call in calls if call != ["adb", "devices"]]


def test_a_logcat_device_error_is_the_devices_without_a_probe(
    monkeypatch, recorded, recorded_anywhere
):
    """Nothing on logcat's stderr is a name an app chose: adb's device error raises as is."""
    calls = _adb_that_lost_the_device_then_found_it(monkeypatch, recorded, recorded_anywhere)
    cmd = _logcat_dump_command()
    assert cmd[3] == "logcat", cmd

    with pytest.raises(adb_exec.DeviceNotFoundError):
        adb_exec.run_argv(cmd, serial="emulator-5554")

    # (`adb devices` is the error's message listing what is attached.)
    assert _without_listing(calls) == [cmd], "a logcat device error was put to a probe"


def test_a_run_as_read_still_confirms_a_device_error_it_was_asked_to(
    monkeypatch, recorded, recorded_anywhere
):
    """The run-as readers opt in, and for them `device` from get-state still decides."""
    calls = _adb_that_lost_the_device_then_found_it(monkeypatch, recorded, recorded_anywhere)
    cmd = ["adb", "-s", "emulator-5554", "shell", "-T", "run-as", "pkg", "cat", "f"]

    result = adb_exec.run_argv(cmd, serial="emulator-5554", confirm_device_errors=True)

    assert result.returncode == 1
    assert [call[-1] for call in _without_listing(calls)] == ["f", "get-state"]


def test_anr_since_reports_a_logcat_device_error_as_unavailable(tmp_path, recorded):
    """End to end, the reviewer's reproduction: exit 3, as log_monitor reports it.

    Before: the probe after the failed dump answered `device`, the error was
    dropped, and the dump's non-zero status became a generic failure at exit 1.
    """
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), stderr=recorded.text("adb_shell_device_not_found"), exit=1),
        ],
    )

    result = chain.run("anr_watcher.py", "--since", "1m", "--serial", "emulator-5554", "--json")

    assert_refusal(result, json_mode=True, naming=("not attached",), status=3)
    logcat_calls = [call for call in chain.calls("adb") if "logcat" in call]
    assert len(logcat_calls) == 1, chain.calls("adb")
