"""Device-free tests for emulator_boot.

These mock the module's ``subprocess`` so no emulator/adb is needed. They assert
the pure arg->command mapping for ``--all`` (batch boot), the env-configurable
boot timeout / poll interval tunables, and the error contract of the adb_exec
migration: the readiness probes swallow adb failures on purpose (a booting
device is legitimately unreachable), while anything that escapes reaches the
user as a remedy rather than a traceback.
"""

from __future__ import annotations

import importlib
import json
import subprocess

import emulator_boot
import pytest

from common import adb_exec
from common.sdk_tools import SdkToolError


class _FakeProcess:
    """Stand-in for subprocess.Popen that reports a still-running emulator."""

    def __init__(self, returncode: int | None = None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _no_connected_devices(monkeypatch):
    """Pretend no emulators are currently connected (so boot proceeds)."""
    monkeypatch.setattr(emulator_boot, "get_connected_devices", lambda: [])


def _fake_emulator_on_path(monkeypatch):
    """Pin emulator resolution to the bare name so argv assertions stay stable."""
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: "emulator")


def test_boot_all_boots_every_defined_avd(monkeypatch):
    _no_connected_devices(monkeypatch)
    _fake_emulator_on_path(monkeypatch)
    monkeypatch.setattr(
        emulator_boot,
        "list_avds",
        lambda: [{"name": "Pixel_5_API_33"}, {"name": "Pixel_7_API_34"}],
    )
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    succeeded, failed, results = emulator_boot.EmulatorBooter.boot_all()

    assert succeeded == 2
    assert failed == 0
    assert [r["avd"] for r in results] == ["Pixel_5_API_33", "Pixel_7_API_34"]
    # Each AVD launched via `emulator -avd <name>` (no -no-window without headless).
    assert launched == [
        ["emulator", "-avd", "Pixel_5_API_33"],
        ["emulator", "-avd", "Pixel_7_API_34"],
    ]


def test_boot_all_headless_appends_no_window(monkeypatch):
    _no_connected_devices(monkeypatch)
    _fake_emulator_on_path(monkeypatch)
    monkeypatch.setattr(emulator_boot, "list_avds", lambda: [{"name": "Pixel_5_API_33"}])
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    succeeded, failed, _results = emulator_boot.EmulatorBooter.boot_all(headless=True)

    assert (succeeded, failed) == (1, 0)
    assert launched == [["emulator", "-avd", "Pixel_5_API_33", "-no-window"]]


def test_boot_all_counts_failures(monkeypatch):
    _no_connected_devices(monkeypatch)
    _fake_emulator_on_path(monkeypatch)
    monkeypatch.setattr(emulator_boot, "list_avds", lambda: [{"name": "Broken_AVD"}])
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    def fake_popen(_cmd, **_kwargs):
        # Process already exited -> boot reports failure.
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    succeeded, failed, results = emulator_boot.EmulatorBooter.boot_all()

    assert (succeeded, failed) == (0, 1)
    assert results[0]["success"] is False


def test_boot_all_empty_avds(monkeypatch):
    monkeypatch.setattr(emulator_boot, "list_avds", lambda: [])
    succeeded, failed, results = emulator_boot.EmulatorBooter.boot_all()
    assert (succeeded, failed, results) == (0, 0, [])


def test_default_tunables():
    # Defaults match the documented contract.
    assert emulator_boot.DEFAULT_BOOT_TIMEOUT == 300
    assert emulator_boot.POLL_INTERVAL_SECONDS == 0.5


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_BOOT_TIMEOUT", "42")
    monkeypatch.setenv("ANDROID_EMU_POLL_INTERVAL", "1.5")
    reloaded = importlib.reload(emulator_boot)
    try:
        assert reloaded.DEFAULT_BOOT_TIMEOUT == 42
        assert reloaded.POLL_INTERVAL_SECONDS == 1.5
    finally:
        monkeypatch.delenv("ANDROID_EMU_BOOT_TIMEOUT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_POLL_INTERVAL", raising=False)
        importlib.reload(emulator_boot)


@pytest.mark.parametrize("headless", [False, True])
def test_single_boot_command_mapping(monkeypatch, headless):
    _no_connected_devices(monkeypatch)
    _fake_emulator_on_path(monkeypatch)
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    booter = emulator_boot.EmulatorBooter("Pixel_5_API_33")
    success, _message = booter.boot(wait_ready=False, headless=headless)

    assert success is True
    expected = ["emulator", "-avd", "Pixel_5_API_33"]
    if headless:
        expected.append("-no-window")
    assert launched == [expected]


# ---------------------------------------------------------------------------
# Readiness probes go through common.adb_exec: bounded, and deliberately
# tolerant of a device that has not finished booting.
# ---------------------------------------------------------------------------
def _fake_adb(monkeypatch, responses):
    """Answer adb / emulator calls at the subprocess boundary under adb_exec.

    ``responses`` maps a command prefix tuple to (returncode, stdout, stderr),
    or to an exception instance to raise.
    """
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        for prefix, response in responses.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                if isinstance(response, BaseException):
                    raise response
                returncode, stdout, stderr = response
                return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


def test_boot_completed_probe_is_bounded(monkeypatch):
    calls = _fake_adb(monkeypatch, {("adb",): (0, "1\n", "")})
    booter = emulator_boot.EmulatorBooter("Pixel_9")

    assert booter._is_boot_completed("emulator-5554") is True
    assert calls[0]["cmd"] == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "getprop",
        "sys.boot_completed",
    ]
    assert calls[0]["kwargs"].get("timeout"), "the readiness probe went out unbounded"


def test_boot_completed_probe_treats_an_unreachable_device_as_not_ready(monkeypatch):
    """The one place a device error must NOT escalate.

    An emulator mid-boot restarts adbd, so `adb shell` answers "device offline"
    or "not found" for a window. That is precisely the state --wait-ready exists
    to wait out; raising would abort the wait at the moment it is working.
    """
    _fake_adb(monkeypatch, {("adb",): (1, "", "error: device offline\n")})
    booter = emulator_boot.EmulatorBooter("Pixel_9")

    assert booter._is_boot_completed("emulator-5554") is False


def test_avd_name_probe_treats_an_unreachable_device_as_unknown(monkeypatch):
    """Same reasoning: an emulator that cannot answer its console yet is not fatal."""
    _fake_adb(monkeypatch, {("adb",): (1, "", "error: device offline\n")})
    booter = emulator_boot.EmulatorBooter("Pixel_9")

    assert booter._get_avd_name_for_serial("emulator-5554") is None


# --- R3: an emulator that will not identify itself stops the boot ----------
# Filtering it out of the "already booted" check is indistinguishable from it
# not being there, and launching a second instance of one AVD corrupts its
# userdata -- which is what the check exists to prevent (S5/L4).


def _recorded_emulators(recorded, states=("device", "device")):
    """The recorded two-emulator listing, as get_connected_devices reports it.

    States are substituted per row so an emulator mid-boot can be represented:
    no profile has recorded a device in a state other than `device` (see the
    `parse_adb_devices` entry in tests/test_fixture_policy.py), and provoking
    one means killing an emulator mid-boot on the recording host.
    """
    serials = [
        line.split()[0]
        for line in recorded.text("adb_devices_multiple").splitlines()
        if line.split() and line.split()[0].startswith("emulator-")
    ]
    assert len(serials) == len(states), f"the recording no longer holds two emulators: {serials}"
    return [
        {"serial": serial, "state": state, "type": "emulator"}
        for serial, state in zip(serials, states, strict=True)
    ]


def test_boot_refuses_while_an_emulator_cannot_be_identified(monkeypatch, recorded):
    """R3: an offline emulator might BE this AVD, so booting is not safe."""
    monkeypatch.setattr(
        emulator_boot,
        "get_connected_devices",
        lambda: _recorded_emulators(recorded, ("offline", "device")),
    )
    _fake_emulator_on_path(monkeypatch)
    _fake_adb(monkeypatch, {("adb", "-s"): (0, recorded.text("emu_avd_name"), "")})

    def _must_not_launch(*_a, **_k):
        raise AssertionError("a second emulator was launched over an unidentified one")

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", _must_not_launch)

    success, message = emulator_boot.EmulatorBooter("Some_Other_AVD").boot()

    assert success is False
    assert "emulator-5554" in message and "offline" in message, message
    assert "kill-server" in message, f"no remedy named: {message}"
    assert "Terminate the stale emulator process" in message, (
        "the remedy still leads with shutting the emulator down, which needs "
        f"the console that is not answering: {message}"
    )


def test_boot_refuses_when_the_emulators_cannot_be_listed(monkeypatch, capsys, recorded_anywhere):
    """F2: a failed listing escaped as a bare RuntimeError, i.e. a traceback.

    `get_connected_devices` re-wraps a failed listing as a plain RuntimeError,
    which is not an AdbError -- so `main`'s handler never saw it, and the one
    module family whose job is turning adb failures into remedies produced a
    stack trace instead. It is a refused boot now: if what is running cannot be
    listed, a boot may start a second instance of an AVD that is already up.
    """
    _fake_emulator_on_path(monkeypatch)
    _fake_adb(monkeypatch, {("adb", "devices"): (1, "", recorded_anywhere("adb_device_not_found"))})

    def _must_not_launch(*_a, **_k):
        raise AssertionError("an emulator was launched over an unreadable device list")

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", _must_not_launch)
    monkeypatch.setattr(
        emulator_boot.sys, "argv", ["emulator_boot.py", "--avd", "Pixel_9", "--json"]
    )

    with pytest.raises(SystemExit) as exc:
        emulator_boot.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert "running state unavailable" in payload["error"], payload


def test_boot_still_short_circuits_when_the_avd_is_positively_running(monkeypatch, recorded):
    """The false-positive control: a known-running AVD is still recognised."""
    monkeypatch.setattr(
        emulator_boot, "get_connected_devices", lambda: _recorded_emulators(recorded)
    )
    _fake_adb(monkeypatch, {("adb", "-s"): (0, recorded.text("emu_avd_name"), "")})

    def _must_not_launch(*_a, **_k):
        raise AssertionError("a second copy of a running AVD was launched")

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", _must_not_launch)

    success, message = emulator_boot.EmulatorBooter("Pixel_9").boot()

    assert success is True
    assert "already booted" in message


def test_avd_name_probe_is_bounded(monkeypatch, recorded):
    calls = _fake_adb(monkeypatch, {("adb",): (0, recorded.text("emu_avd_name"), "")})
    booter = emulator_boot.EmulatorBooter("Pixel_9")

    assert booter._get_avd_name_for_serial("emulator-5554") == "Pixel_9"
    assert calls[0]["kwargs"].get("timeout"), "the AVD-name probe went out unbounded"


def test_avd_listing_is_bounded(monkeypatch):
    """`emulator -list-avds` is an SDK tool, not adb -- but still not unbounded."""
    _fake_emulator_on_path(monkeypatch)
    calls = _fake_adb(monkeypatch, {("emulator",): (0, "Pixel_9\nPixel_5_API_33\n", "")})

    assert emulator_boot.list_avds() == [{"name": "Pixel_9"}, {"name": "Pixel_5_API_33"}]
    assert calls[0]["cmd"] == ["emulator", "-list-avds"]
    assert calls[0]["kwargs"].get("timeout"), "`emulator -list-avds` went out unbounded"


def test_avd_listing_reports_its_own_timeout(monkeypatch):
    """A bounded call raises TimeoutExpired: a remedy, not a traceback, and not [].

    X3: the empty list this used to assert is the answer for "this host defines
    no AVDs", so returning it for "the listing timed out" left `--list-avds`
    saying "No AVDs found" at exit 0.
    """
    _fake_emulator_on_path(monkeypatch)
    _fake_adb(
        monkeypatch,
        {("emulator",): subprocess.TimeoutExpired(cmd="emulator", timeout=30)},
    )
    with pytest.raises(SdkToolError) as excinfo:
        emulator_boot.list_avds()
    assert "retry" in str(excinfo.value), "the timeout names no remedy"


def test_cli_reports_an_adb_error_without_a_traceback(monkeypatch, capsys):
    """At the CLI boundary the agent gets the remedy, not a stack trace.

    Since F2 the listing failure is a refused boot rather than an escaping
    exception -- `get_connected_devices` re-wraps a failed listing as a BARE
    RuntimeError, which is not an AdbError and so reached the user as a
    traceback. It is reported through the same `_fail` as every other failure,
    so the remedy still arrives; what changed is that it arrives having said
    which operation could not be completed.
    """

    def _raise():
        raise adb_exec.MultipleDevicesError(
            "More than one device is attached, so adb could not choose one. "
            "Pass --serial with one of: emulator-5554, emulator-5556."
        )

    monkeypatch.setattr(emulator_boot, "get_connected_devices", _raise)
    monkeypatch.setattr(emulator_boot.sys, "argv", ["emulator_boot.py", "--avd", "Pixel_9"])

    with pytest.raises(SystemExit) as exc:
        emulator_boot.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "Traceback" not in captured.err
    assert "--serial" in captured.err, "no remedy named"


# ---------------------------------------------------------------------------
# Emulator resolution (SDK-root-on-PATH regression)
# ---------------------------------------------------------------------------
def test_list_avds_reports_a_permission_error_from_a_directory_argv0(monkeypatch):
    """`emulator` resolving to a directory raises PermissionError, not ENOENT."""
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: "/sdk/emulator/emulator")

    def boom(_cmd, **_kwargs):
        raise PermissionError(13, "Permission denied", "emulator")

    monkeypatch.setattr(emulator_boot.subprocess, "run", boom)

    with pytest.raises(SdkToolError) as excinfo:
        emulator_boot.list_avds()
    assert "$ANDROID_HOME/emulator" in str(excinfo.value), "no remedy for the SDK-root PATH"


def test_list_avds_reports_actionable_hint_when_unresolvable(monkeypatch):
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: None)

    def unexpected(_cmd, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("must not exec an unresolved emulator")

    monkeypatch.setattr(emulator_boot.subprocess, "run", unexpected)

    with pytest.raises(SdkToolError) as excinfo:
        emulator_boot.list_avds()
    message = str(excinfo.value)
    assert "Looked in" in message, "the failure does not say where it looked"
    assert "$ANDROID_HOME/emulator" in message


@pytest.mark.parametrize(
    "argv",
    [["--list-avds", "--json"], ["--all", "--json"]],
    ids=["list-avds", "boot-all"],
)
def test_the_cli_exits_non_zero_when_avd_discovery_fails(monkeypatch, capsys, argv):
    """X3, as the agent meets it: a host with no SDK, asking for JSON.

    Both modes read the same listing, so both must report the same way. The
    failure is caught where `--json` is known -- caught in `main()` it would
    exit 1 with an empty stdout, which is a decode error for whoever asked for
    JSON.
    """
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: None)
    monkeypatch.setattr(emulator_boot.sys, "argv", ["emulator_boot.py", *argv])

    with pytest.raises(SystemExit) as exc:
        emulator_boot.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert "error" in payload, f"--json reported no error: {payload}"
    assert "$ANDROID_HOME/emulator" in payload["error"], "the JSON error names no remedy"
    assert payload.get("avds") is None, "an empty AVD list was printed alongside the error"
    assert payload.get("succeeded") is None, "a boot summary was printed alongside the error"


def test_list_avds_parses_recorded_emulator_output(monkeypatch, recorded):
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: "/sdk/emulator/emulator")

    seen: list[list[str]] = []

    class _Result:
        stdout = recorded.text("emulator_list_avds")
        stderr = ""
        returncode = 0

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return _Result()

    monkeypatch.setattr(emulator_boot.subprocess, "run", fake_run)

    expected = [ln.strip() for ln in recorded.lines("emulator_list_avds") if ln.strip()]
    assert [a["name"] for a in emulator_boot.list_avds()] == expected
    assert seen == [["/sdk/emulator/emulator", "-list-avds"]]


def test_boot_uses_the_resolved_emulator_path(monkeypatch):
    _no_connected_devices(monkeypatch)
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: "/sdk/emulator/emulator")
    monkeypatch.setattr(emulator_boot.time, "sleep", lambda _s: None)

    launched: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", fake_popen)

    success, _message = emulator_boot.EmulatorBooter("Pixel_9").boot()

    assert success is True
    assert launched == [["/sdk/emulator/emulator", "-avd", "Pixel_9"]]


def test_boot_reports_actionable_hint_when_unresolvable(monkeypatch):
    _no_connected_devices(monkeypatch)
    monkeypatch.setattr(emulator_boot, "get_emulator_path", lambda: None)

    def unexpected(_cmd, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("must not exec an unresolved emulator")

    monkeypatch.setattr(emulator_boot.subprocess, "Popen", unexpected)

    success, message = emulator_boot.EmulatorBooter("Pixel_9").boot()

    assert success is False
    assert "$ANDROID_HOME/emulator" in message
