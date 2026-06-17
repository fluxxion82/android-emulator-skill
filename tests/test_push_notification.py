"""Device-free tests for push_notification badge/sound extras mapping.

The new --badge / --no-sound flags map to Android broadcast extras (--ei badge,
--ez sound). These tests mock the module's subprocess.run, run send_notification,
and assert the resulting adb command carries the expected extras. No emulator or
device is required.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import push_notification


def _capture_cmd(monkeypatch) -> list[list[str]]:
    """Patch the module's subprocess.run and return a list captured calls write to."""
    calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        calls.append(cmd)
        # Stdout contains "Broadcast" so the success heuristic returns True.
        return SimpleNamespace(returncode=0, stdout="Broadcast completed: result=0", stderr="")

    monkeypatch.setattr(push_notification.subprocess, "run", fake_run)
    return calls


def _extras(cmd: list[str], flag: str, key: str) -> str | None:
    """Return the value following the (flag, key) pair in an adb command, if present."""
    for i in range(len(cmd) - 2):
        if cmd[i] == flag and cmd[i + 1] == key:
            return cmd[i + 2]
    return None


def test_sound_enabled_by_default(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    sim = push_notification.PushNotificationSimulator()
    ok, _ = sim.send_notification("com.myapp", "T", "M")
    assert ok is True
    assert _extras(calls[0], "--ez", "sound") == "true"


def test_no_sound_sets_false(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    sim = push_notification.PushNotificationSimulator()
    sim.send_notification("com.myapp", "T", "M", sound=False)
    assert _extras(calls[0], "--ez", "sound") == "false"


def test_badge_emitted_when_positive(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    sim = push_notification.PushNotificationSimulator()
    sim.send_notification("com.myapp", "T", "M", badge=3)
    assert _extras(calls[0], "--ei", "badge") == "3"


def test_badge_omitted_when_zero_or_none(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    sim = push_notification.PushNotificationSimulator()
    sim.send_notification("com.myapp", "T", "M", badge=0)
    sim.send_notification("com.myapp", "T", "M", badge=None)
    assert _extras(calls[0], "--ei", "badge") is None
    assert _extras(calls[1], "--ei", "badge") is None


def test_existing_title_message_extras_preserved(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    sim = push_notification.PushNotificationSimulator()
    sim.send_notification("com.myapp", "Hello", "World", data={"k": "v"})
    cmd = calls[0]
    assert _extras(cmd, "--es", "title") == '"Hello"'
    assert _extras(cmd, "--es", "message") == '"World"'
    assert _extras(cmd, "--es", "k") == '"v"'
    # Default contract: no shell=True, explicit command list begins with adb.
    assert cmd[0] == "adb"


def test_no_shell_true(monkeypatch):
    """send_notification must never invoke subprocess.run with shell=True."""
    seen_kwargs: dict = {}

    def fake_run(_cmd, *_args, **kwargs):
        seen_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="Broadcast result=0", stderr="")

    monkeypatch.setattr(push_notification.subprocess, "run", fake_run)
    push_notification.PushNotificationSimulator().send_notification("com.myapp", "T", "M")
    assert seen_kwargs.get("shell") in (None, False)
    assert seen_kwargs.get("check") is True


def test_failure_path_on_called_process_error(monkeypatch):
    def fake_run(cmd, *_args, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(push_notification.subprocess, "run", fake_run)
    ok, msg = push_notification.PushNotificationSimulator().send_notification("com.myapp", "T", "M")
    assert ok is False
    assert "boom" in msg
