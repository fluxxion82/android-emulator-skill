"""Unit tests for app_launcher restart + intent-extra (``--args``) deltas.

These exercise pure arg->command mapping by mocking the ``subprocess.run``
that ``common.adb_exec`` calls, so no device or emulator is required. Every adb
call now goes through ``adb_exec.run_adb`` -- which is where the time bound and
the typed, remedy-naming errors live -- so the fakes below stand in for adb
itself rather than for a per-module ``subprocess``.
"""

from __future__ import annotations

import subprocess
import sys

import app_launcher
import pytest
from app_launcher import AppLauncher, parse_extras


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


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


def test_launch_appends_es_extras(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok()

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    launcher = AppLauncher(serial="emulator-5554")
    success, _ = launcher.launch(
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


def test_launch_without_extras_has_no_es(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok()

    monkeypatch.setattr(app_launcher.adb_exec.subprocess, "run", fake_run)

    launcher = AppLauncher(serial="emulator-5554")
    launcher.launch("com.example.app", activity=".MainActivity")

    assert "--es" not in captured["cmd"]


# --- restart = terminate then launch with a sleep between ------------------


def test_restart_terminates_then_launches_with_delay(monkeypatch):
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        # force-stop is the terminate path; am start is the launch path.
        if "force-stop" in cmd:
            calls.append("terminate")
        elif "start" in cmd:
            calls.append("launch")
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


def test_restart_forwards_extras_to_launch(monkeypatch):
    launch_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if "start" in cmd:
            launch_cmds.append(cmd)
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
