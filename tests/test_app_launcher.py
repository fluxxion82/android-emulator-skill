"""Unit tests for app_launcher restart + intent-extra (``--args``) deltas.

These exercise pure arg->command mapping by mocking the ``subprocess.run``
that ``common.adb_exec`` calls, so no device or emulator is required. Every adb
call now goes through ``adb_exec.run_adb`` -- which is where the time bound and
the typed, remedy-naming errors live -- so the fakes below stand in for adb
itself rather than for a per-module ``subprocess``.
"""

from __future__ import annotations

import json as json_lib
import subprocess
import sys

import app_launcher
import pytest
from app_launcher import AppLauncher, parse_extras


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _am_start_ok(recorded) -> str:
    """The recorded stdout of a successful `am start -W`.

    A launch is confirmed by `Status: ok` in this report and by nothing else,
    so the fake adb has to produce the report. It used to produce an empty
    string and the launch was called a success -- which is the same "absence of
    a problem is a result" shape the whole increment is about, sitting in the
    test that was supposed to prove the opposite.
    """
    return recorded.text("am_start_wait_settings")


def _launch_aware(recorded, other: str = ""):
    """A fake `subprocess.run` that answers `am start` with the recorded report."""

    def fake_run(cmd, **kwargs):
        return _ok(_am_start_ok(recorded) if "start" in cmd else other)

    return fake_run


def _noop_sleep(_seconds: float) -> None:
    """No-op replacement for time.sleep so restart tests don't actually wait."""


# --- parse_extras (pure) ---------------------------------------------------


def test_parse_extras_none_returns_empty():
    assert parse_extras(None) == {}


def test_parse_extras_multiple_pairs():
    assert parse_extras(["mode=test", "env=ci"]) == {"mode": "test", "env": "ci"}


def test_parse_extras_value_with_equals_preserved():
    assert parse_extras(["url=http://x?a=b"]) == {"url": "http://x?a=b"}


def test_parse_extras_empty_value_allowed():
    assert parse_extras(["flag="]) == {"flag": ""}


@pytest.mark.parametrize("bad", ["noequals", "=value"])
def test_parse_extras_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_extras([bad])


# --- launch with extras maps to --es KEY VALUE -----------------------------


def test_launch_appends_es_extras(monkeypatch, recorded):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok(_am_start_ok(recorded))

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    launcher = AppLauncher(serial="emulator-5554")
    success, _message, _report = launcher.launch(
        "com.example.app", activity=".MainActivity", extras={"mode": "test", "env": "ci"}
    )

    assert success is True
    cmd = captured["cmd"]
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]
    assert "am" in cmd and "start" in cmd
    assert "-n" in cmd and "com.example.app/.MainActivity" in cmd
    # Each extra rendered as the contiguous triple "--es KEY VALUE".
    i = cmd.index("--es")
    assert cmd[i : i + 3] == ["--es", "mode", "test"]
    j = cmd.index("--es", i + 3)
    assert cmd[j : j + 3] == ["--es", "env", "ci"]


def test_launch_without_extras_has_no_es(monkeypatch, recorded):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok(_am_start_ok(recorded))

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    launcher = AppLauncher(serial="emulator-5554")
    launcher.launch("com.example.app", activity=".MainActivity")

    assert "--es" not in captured["cmd"]


# --- restart = terminate then launch with a sleep between ------------------


def test_restart_terminates_then_launches_with_delay(monkeypatch, recorded):
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        # force-stop is the terminate path; am start is the launch path.
        if "force-stop" in cmd:
            calls.append("terminate")
        elif "start" in cmd:
            calls.append("launch")
            return _ok(_am_start_ok(recorded))
        return _ok()

    sleeps: list[float] = []
    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher.time, "sleep", sleeps.append)

    launcher = AppLauncher(serial="emulator-5554")
    success, message = launcher.restart(
        "com.example.app", activity=".MainActivity", extras={"mode": "test"}
    )

    assert success is True
    assert message == "Restarted: com.example.app"
    # Terminate must happen before launch, with exactly one sleep between.
    assert calls == ["terminate", "launch"]
    assert sleeps == [app_launcher.RELAUNCH_DELAY_SECONDS]


def test_restart_forwards_extras_to_launch(monkeypatch, recorded):
    launch_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if "start" in cmd:
            launch_cmds.append(cmd)
            return _ok(_am_start_ok(recorded))
        return _ok()

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher.time, "sleep", _noop_sleep)

    launcher = AppLauncher(serial="emulator-5554")
    launcher.restart("com.example.app", activity=".MainActivity", extras={"k": "v"})

    assert len(launch_cmds) == 1
    cmd = launch_cmds[0]
    i = cmd.index("--es")
    assert cmd[i : i + 3] == ["--es", "k", "v"]


def test_restart_aborts_when_terminate_fails(monkeypatch):
    started = {"launch": False}

    def fake_run(cmd, **kwargs):
        if "force-stop" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if "start" in cmd:
            started["launch"] = True
        return _ok()

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher.time, "sleep", _noop_sleep)

    launcher = AppLauncher(serial="emulator-5554")
    success, message = launcher.restart("com.example.app")

    assert success is False
    assert "boom" in message
    assert started["launch"] is False


# --- bounding: no adb call may go out unbounded ----------------------------


def test_every_lifecycle_call_is_bounded(monkeypatch):
    """An unbounded adb call wedges the connection for whatever runs next."""
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        return _ok("Success\n")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    launcher = AppLauncher(serial="emulator-5554")
    launcher.launch("com.example.app", activity=".MainActivity")
    launcher.terminate("com.example.app")
    launcher.install("/tmp/app.apk")
    launcher.uninstall("com.example.app")
    launcher.open_url("myapp://main")

    assert len(seen) == 5
    assert all(kwargs.get("timeout") for kwargs in seen), "an adb call went out unbounded"


def test_install_gets_its_own_budget_rather_than_the_default(monkeypatch):
    """Streaming and verifying an APK routinely outlasts the 30s default."""
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        return _ok("Success\n")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    AppLauncher(serial="emulator-5554").install("/tmp/app.apk")

    assert seen[0]["timeout"] == app_launcher.INSTALL_TIMEOUT_SECONDS
    assert app_launcher.INSTALL_TIMEOUT_SECONDS > app_launcher.adb_exec.DEFAULT_TIMEOUT


# --- launcher activity resolution ------------------------------------------


def test_launcher_activity_parsed_from_recorded_resolve_activity(recorded):
    """The answer is the single component on the last non-blank line.

    Asserted against recorded `cmd package resolve-activity --brief` output
    rather than an imagined format.
    """
    fixture = recorded.text("resolve_activity_launcher")
    assert AppLauncher._parse_resolved_component(fixture) == "com.android.settings/.Settings"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "No activity found\n",
        "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n",
    ],
)
def test_launcher_activity_none_when_nothing_resolves(output):
    """Anything that is not a bare component must not be passed off as one."""
    assert AppLauncher._parse_resolved_component(output) is None


def test_launcher_activity_asks_the_package_manager_not_pm_dump(monkeypatch, recorded):
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return _ok(recorded.text("resolve_activity_launcher"))

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    activity = AppLauncher(serial="emulator-5554")._get_launcher_activity("com.example.app")

    assert activity == "com.android.settings/.Settings"
    cmd = captured[0]
    assert "resolve-activity" in cmd and "--brief" in cmd
    assert "android.intent.category.LAUNCHER" in cmd
    assert "dump" not in cmd, "reverted to grepping `pm dump`"


# --- device-level failures reach the CLI boundary, not the user's terminal --


def test_device_error_is_not_reported_as_a_failed_launch(monkeypatch):
    """ "more than one device" means the command never ran at all."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "adb: more than one device/emulator\n")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    with pytest.raises(app_launcher.adb_exec.MultipleDevicesError):
        AppLauncher().launch("com.example.app", activity=".MainActivity")


def test_unknown_serial_exits_one_with_an_actionable_message(
    monkeypatch, capsys, recorded_anywhere
):
    """A wrong --serial must yield a remedy and exit 1, never a traceback."""
    fixture = recorded_anywhere("adb_device_not_found")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", fixture)

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher, "resolve_device_identifier", lambda value: value)
    monkeypatch.setattr(
        sys,
        "argv",
        ["app_launcher.py", "--serial", "no-such-serial-xyz", "--terminate", "com.example.app"],
    )

    with pytest.raises(SystemExit) as excinfo:
        app_launcher.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "no-such-serial-xyz" in captured.err
    assert "adb devices" in captured.err, "the error does not say how to see what is attached"
    assert "Traceback" not in captured.err


def test_ambiguous_device_tells_the_agent_which_flag_to_pass(monkeypatch, capsys):
    """For an agent, stderr is the retry prompt: it must name --serial."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "adb: more than one device/emulator\n")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher, "resolve_device_identifier", lambda value: value)
    monkeypatch.setattr(sys, "argv", ["app_launcher.py", "--open-url", "myapp://main"])

    with pytest.raises(SystemExit) as excinfo:
        app_launcher.main()

    assert excinfo.value.code == 1
    assert "--serial" in capsys.readouterr().err


# --- C6: every mode reports a failure the same way -------------------------
#
# Six modes answered `{"success": false, "message": ...}` while --list and
# --state answered `{"error": ...}`. The runtime sweep cannot see the
# difference: it breaks adb at the device level, which main() maps before any
# of these branches decides anything. So the failed *command* is injected here.


ACTION_MODES = [
    ("launch", ["--launch", "com.example.app", "--activity", ".MainActivity"]),
    ("restart", ["--restart", "com.example.app", "--activity", ".MainActivity"]),
    ("terminate", ["--terminate", "com.example.app"]),
    ("install", ["--install", "/tmp/app.apk"]),
    ("uninstall", ["--uninstall", "com.example.app"]),
    ("open_url", ["--open-url", "myapp://main"]),
]


@pytest.mark.parametrize(("action", "argv"), ACTION_MODES, ids=[m[0] for m in ACTION_MODES])
@pytest.mark.parametrize("json_mode", [False, True])
def test_every_action_mode_reports_a_failed_command_the_same_way(
    monkeypatch, capsys, action, argv, json_mode
):
    """adb ran, the command failed: exit 1, and `{"error": ...}` under --json."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher.time, "sleep", _noop_sleep)

    code = _run_cli(monkeypatch, [*argv, *(["--json"] if json_mode else [])])
    captured = capsys.readouterr()

    assert code == 1, f"{action} reported success after the command failed"
    assert "Traceback" not in captured.err
    if json_mode:
        payload = json_lib.loads(captured.out)
        assert set(payload) == {"error"}, f"{action} answered {payload}"
        assert payload["error"], "the error payload is empty"
    else:
        assert captured.out == "", f"{action} printed an answer on failure: {captured.out!r}"
        assert captured.err.startswith("Error: ")


@pytest.mark.parametrize(("action", "argv"), ACTION_MODES, ids=[m[0] for m in ACTION_MODES])
def test_the_success_shape_of_every_action_mode_is_unchanged(
    monkeypatch, capsys, recorded, action, argv
):
    """The negative control: the JSON a working run prints must not have moved."""
    monkeypatch.setattr(
        app_launcher.adb_exec.subprocess, "run", _launch_aware(recorded, other="Success\n")
    )
    monkeypatch.setattr(app_launcher.time, "sleep", _noop_sleep)

    code = _run_cli(monkeypatch, [*argv, "--json"])
    payload = json_lib.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["success"] is True
    assert payload["action"] == action
    assert payload["message"]


# --- E1: a launch is confirmed by the report, not by the absence of trouble --


def test_a_launch_is_confirmed_by_status_ok_and_reports_the_resolved_activity(
    monkeypatch, recorded
):
    """The success capture, and the alias trap it carries.

    `am start -W -n com.android.settings/.Settings` answers
    `Activity: com.android.settings/.homepage.SettingsHomepageActivity` -- the
    alias resolves, so the component that comes up is NOT the string that was
    asked for. A launcher that confirmed the launch by comparing those two
    would call every alias a failure; the confirmation is `Status: ok`, and the
    resolved component is information to hand on.
    """
    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", _launch_aware(recorded))

    success, message, report = AppLauncher(serial="emulator-5554").launch(
        "com.android.settings", activity=".Settings"
    )

    assert success is True, message
    assert report["status"] == "ok"
    assert report["activity"] == "com.android.settings/.homepage.SettingsHomepageActivity"
    assert report["activity"] in message, "the resolved activity is not reported"
    assert "575" in message, f"the launch time is not reported: {message}"


def test_a_launch_that_did_not_start_is_a_failure_naming_the_remedy(monkeypatch, recorded):
    """The failure capture: exit 1, everything on stdout, and NO `Status:` line.

    This is why "no Status line" cannot mean success. The whole diagnostic --
    `Error type 3`, `Error: Activity class {...} does not exist.` -- is on
    stdout, with stderr empty (stderr_bytes: 0 in the manifest), so a launcher
    reading stderr for the reason finds nothing and one waiting for a non-`ok`
    status finds nothing either.
    """
    failure = recorded.text("am_start_wait_missing")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=failure, stderr="")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    success, message, _report = AppLauncher(serial="emulator-5554").launch(
        "com.android.settings", activity=".NoSuchActivity"
    )

    assert success is False
    assert "does not exist" in message, message
    assert "resolve-activity" in message, f"the failure names no remedy: {message}"


def test_a_report_with_no_status_line_is_not_a_launch(monkeypatch, recorded):
    """The same rule where adb itself exits 0: the report decides, not the status code.

    Derived from the recorded failure by dropping the exit status to 0, which is
    the one thing about it that cannot be recorded on demand -- some devices
    answer a bad component with `Error:` and exit 0. Every byte of the output is
    the device's.
    """

    def fake_run(cmd, **kwargs):
        return _ok(recorded.text("am_start_wait_missing"))

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    success, message, _report = AppLauncher(serial="emulator-5554").launch(
        "com.android.settings", activity=".NoSuchActivity"
    )

    assert success is False, "a launch with no `Status: ok` was reported as one"
    assert "does not exist" in message, message


def test_parse_am_start_reads_the_recorded_report(recorded):
    """The parser against both captures, as they were written to stdout."""
    ok = app_launcher.parse_am_start(recorded.text("am_start_wait_settings"))
    assert ok["status"] == "ok"
    assert ok["launchstate"] == "COLD"
    assert ok["totaltime"] == "575"
    assert app_launcher.am_start_error(recorded.text("am_start_wait_settings")) is None

    failed = app_launcher.parse_am_start(recorded.text("am_start_wait_missing"))
    assert "status" not in failed, "the failure capture has no Status line to read"
    assert app_launcher.am_start_error(recorded.text("am_start_wait_missing")) == (
        "Activity class {com.android.settings/com.android.settings.NoSuchActivity} "
        "does not exist."
    )


# --- C6: a failed lookup is not an empty answer ----------------------------
#
# `--list` used to catch every exception, answer `[]`, print
# "Installed packages (0):" and exit 0. "Nothing is installed" and "I could
# not look" are different answers and the agent has no second signal.


def _run_cli(monkeypatch, argv: list[str]):
    """Run main() with argv, returning the SystemExit code."""
    monkeypatch.setattr(app_launcher, "resolve_device_identifier", lambda value: value)
    monkeypatch.setattr(sys, "argv", ["app_launcher.py", *argv])
    with pytest.raises(SystemExit) as excinfo:
        app_launcher.main()
    return excinfo.value.code


def test_list_packages_raises_rather_than_answering_with_an_empty_list(monkeypatch):
    """The method-level half of C6: the failure must reach the caller."""

    def boom(_serial):
        raise app_launcher.adb_exec.DeviceNotFoundError("adb: device 'x' not found")

    monkeypatch.setattr(app_launcher, "list_installed_packages", boom)

    with pytest.raises(app_launcher.adb_exec.DeviceNotFoundError):
        AppLauncher(serial="emulator-5554").list_packages()


@pytest.mark.parametrize("json_mode", [False, True])
def test_list_exits_non_zero_when_the_device_cannot_be_reached(monkeypatch, capsys, json_mode):
    """Both output modes, because C6 was one contract kept in neither."""

    def boom(_serial):
        raise app_launcher.adb_exec.DeviceNotFoundError(
            "device 'no-such-serial-xyz' not found; run `adb devices` to see what is attached"
        )

    monkeypatch.setattr(app_launcher, "list_installed_packages", boom)

    code = _run_cli(monkeypatch, ["--list", *(["--json"] if json_mode else [])])
    captured = capsys.readouterr()

    assert code == 1
    assert "Traceback" not in captured.err
    assert "Installed packages" not in captured.out, "a failed lookup printed an answer"
    if json_mode:
        payload = json_lib.loads(captured.out)
        assert set(payload) == {"error"}, payload
        assert "adb devices" in payload["error"], "the error does not name its remedy"
    else:
        assert "adb devices" in captured.err, "the error does not name its remedy"


def test_a_failed_pm_list_is_not_a_traceback(monkeypatch, capsys):
    """`list_installed_packages` re-raises a non-zero `pm list packages` as a
    plain RuntimeError, not an AdbError.

    Catching only ``adb_exec.AdbError`` at the CLI boundary was survivable
    while ``list_packages`` swallowed everything; the moment it stopped, this
    became the traceback path.
    """

    def boom(_serial):
        raise RuntimeError("Failed to list packages: pm exited 1; check the device is unlocked")

    monkeypatch.setattr(app_launcher, "list_installed_packages", boom)

    code = _run_cli(monkeypatch, ["--list"])
    captured = capsys.readouterr()

    assert code == 1
    assert "Traceback" not in captured.err
    assert captured.err.startswith("Error: ")
    assert "pm exited 1" in captured.err


def test_state_text_mode_does_not_index_an_error_dict(monkeypatch, capsys):
    """`--state` printed `state['package']` on whatever get_state returned.

    Reported as a failure, that dict has no ``package`` key, so the CLI died
    with a KeyError traceback. get_state now raises instead of returning one,
    and this pins the branch that keeps a future failure return off the
    indexing path.
    """
    monkeypatch.setattr(
        AppLauncher,
        "get_state",
        lambda self, package: (False, {"error": "could not read app state; run `adb devices`"}),
    )

    code = _run_cli(monkeypatch, ["--state", "com.example.app"])
    captured = capsys.readouterr()

    assert code == 1
    assert "Traceback" not in captured.err
    assert "adb devices" in captured.err


def test_state_json_mode_reports_the_error_and_exits_non_zero(monkeypatch, capsys):
    """The other half: `--state --json` printed the error dict and exited 0."""
    monkeypatch.setattr(
        AppLauncher,
        "get_state",
        lambda self, package: (False, {"error": "could not read app state; run `adb devices`"}),
    )

    code = _run_cli(monkeypatch, ["--state", "com.example.app", "--json"])
    payload = json_lib.loads(capsys.readouterr().out)

    assert code == 1
    assert set(payload) == {"error"}, payload


@pytest.mark.parametrize("json_mode", [False, True])
def test_state_fails_when_only_the_activity_lookup_cannot_reach_the_device(
    monkeypatch, capsys, recorded_anywhere, json_mode
):
    """The staged failure: the package and PID probes answer, `dumpsys window` does not.

    `get_current_activity` maps an adb failure to None by default, so
    `foreground` came back None and `--state` reported success having never
    learned the answer. get_state now asks in strict mode, so the device error
    reaches the CLI boundary with its remedy.
    """
    not_found = recorded_anywhere("adb_shell_device_not_found")

    def fake_run(cmd, **kwargs):
        if "dumpsys" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", not_found)
        if "pidof" in cmd:
            return _ok("4242\n")
        return _ok("package:com.example.app\n")

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    argv = ["--serial", "no-such-serial-xyz", "--state", "com.example.app"]
    code = _run_cli(monkeypatch, [*argv, *(["--json"] if json_mode else [])])
    captured = capsys.readouterr()

    assert code == 1
    assert "Traceback" not in captured.err
    if json_mode:
        payload = json_lib.loads(captured.out)
        assert set(payload) == {"error"}, payload
        assert "no-such-serial-xyz" in payload["error"]
    else:
        assert "Foreground" not in captured.out, "reported a foreground it never read"
        assert "no-such-serial-xyz" in captured.err


def test_the_default_activity_lookup_still_tolerates_a_failure(monkeypatch, recorded_anywhere):
    """The negative control, and the reason strict is a parameter.

    `test_recorder` labels a recording with whatever activity it can see and
    must not fail the recording over it. Changing the default would have
    changed that caller silently.
    """
    from common import device_utils

    monkeypatch.setattr(
        app_launcher.adb_exec.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(
            cmd, 1, "", recorded_anywhere("adb_shell_device_not_found")
        ),
    )

    assert device_utils.get_current_activity("no-such-serial-xyz") is None
    with pytest.raises(device_utils.AdbError):
        device_utils.get_current_activity("no-such-serial-xyz", strict=True)


def test_state_still_answers_for_an_uninstalled_package(monkeypatch, capsys):
    """The negative control: "not installed" is an answer, not a failure."""
    monkeypatch.setattr(app_launcher, "list_installed_packages", lambda _serial: ["com.other"])

    code = _run_cli(monkeypatch, ["--state", "com.example.app"])
    captured = capsys.readouterr()

    assert code == 0
    assert "Installed: No" in captured.out
