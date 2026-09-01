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

Error strings are matched against recorded adb output where a fixture exists —
`adb_device_not_found.txt` proves the prefix is `error:` and not `adb:`, which
is the kind of detail that gets hardcoded wrong.
"""

from __future__ import annotations

import subprocess

import pytest

from common import adb_exec


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
    """Matched against real adb output: the prefix is 'error:', not 'adb:'.

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
