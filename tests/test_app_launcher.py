"""Unit tests for app_launcher restart + intent-extra (``--args``) deltas.

These exercise pure arg->command mapping by mocking the module's
``subprocess.run`` so no device or emulator is required.
"""

from __future__ import annotations

import subprocess

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

    monkeypatch.setattr(app_launcher.subprocess, "run", fake_run)

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

    monkeypatch.setattr(app_launcher.subprocess, "run", fake_run)

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
    monkeypatch.setattr(app_launcher.subprocess, "run", fake_run)
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

    monkeypatch.setattr(app_launcher.subprocess, "run", fake_run)
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
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        if "start" in cmd:
            started["launch"] = True
        return _ok()

    monkeypatch.setattr(app_launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(app_launcher.time, "sleep", _noop_sleep)

    launcher = AppLauncher(serial="emulator-5554")
    success, _ = launcher.restart("com.example.app")

    assert success is False
    assert started["launch"] is False
