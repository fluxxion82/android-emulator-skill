"""Device-free tests for the privacy_manager feature deltas.

Covers two additive behaviors:

1. ``--reset SERVICE`` is an alias for revoke (Android has no prompt-state
   reset), so it must build the same ``pm revoke`` adb command as ``--revoke``.
2. ``--scenario`` / ``--step`` test-trail metadata is built purely and only when
   at least one value is supplied.

All adb calls are exercised by monkeypatching ``privacy_manager.subprocess.run``,
so no device or emulator is required.
"""

from __future__ import annotations

import subprocess
import types

import privacy_manager
from privacy_manager import PrivacyManager, build_test_trail


def _fake_run_recorder():
    """Return (recorder, fake_run) where fake_run records argv and succeeds."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        # The script passes check=True and an explicit timeout; assert contract.
        assert kwargs.get("check") is True
        assert "timeout" in kwargs
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, fake_run


def test_reset_builds_same_revoke_command(monkeypatch):
    """--reset must map to `pm revoke <pkg> <perm>` (revoke alias)."""
    calls, fake_run = _fake_run_recorder()
    monkeypatch.setattr(privacy_manager.subprocess, "run", fake_run)

    mgr = PrivacyManager(serial="emulator-5554")
    reset_ok, reset_msg = mgr.reset_permission("com.myapp", "camera")

    assert reset_ok is True
    # Records that reset went through revoke for transparency.
    assert "via revoke" in reset_msg
    assert len(calls) == 1
    cmd = calls[0]
    assert "revoke" in cmd
    assert "grant" not in cmd
    assert "com.myapp" in cmd
    assert "android.permission.CAMERA" in cmd


def test_reset_matches_revoke_command_exactly(monkeypatch):
    """reset_permission and revoke_permission must build identical adb argv."""
    calls, fake_run = _fake_run_recorder()
    monkeypatch.setattr(privacy_manager.subprocess, "run", fake_run)

    mgr = PrivacyManager(serial="emulator-5554")
    mgr.revoke_permission("com.myapp", "location")
    mgr.reset_permission("com.myapp", "location")

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert "android.permission.ACCESS_FINE_LOCATION" in calls[0]


def test_reset_unknown_permission_short_circuits(monkeypatch):
    """Unknown permission fails before any adb call and is not reported as reset."""
    calls, fake_run = _fake_run_recorder()
    monkeypatch.setattr(privacy_manager.subprocess, "run", fake_run)

    mgr = PrivacyManager(serial="emulator-5554")
    ok, msg = mgr.reset_permission("com.myapp", "not_a_real_perm")

    assert ok is False
    assert "Unknown permission" in msg
    assert calls == []


def test_reset_propagates_revoke_failure(monkeypatch):
    """When revoke fails, reset returns the underlying failure unchanged."""

    def failing_run(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(privacy_manager.subprocess, "run", failing_run)

    mgr = PrivacyManager(serial="emulator-5554")
    ok, msg = mgr.reset_permission("com.myapp", "camera")

    assert ok is False
    assert "via revoke" not in msg
    assert "Failed to revoke permission" in msg


def test_grant_command_mapping(monkeypatch):
    """Sanity check the existing grant path still maps to `pm grant`."""
    calls, fake_run = _fake_run_recorder()
    monkeypatch.setattr(privacy_manager.subprocess, "run", fake_run)

    mgr = PrivacyManager(serial="emulator-5554")
    ok, _ = mgr.grant_permission("com.myapp", "microphone")

    assert ok is True
    assert "grant" in calls[0]
    assert "android.permission.RECORD_AUDIO" in calls[0]


def test_build_test_trail_none_when_absent():
    assert build_test_trail(None, None) is None


def test_build_test_trail_scenario_only():
    trail = build_test_trail("onboarding", None)
    assert trail is not None
    assert trail["scenario"] == "onboarding"
    assert "step" not in trail
    assert "timestamp" in trail


def test_build_test_trail_step_only():
    trail = build_test_trail(None, 3)
    assert trail is not None
    assert trail["step"] == 3
    assert "scenario" not in trail
    assert "timestamp" in trail


def test_build_test_trail_step_zero_is_recorded():
    # step 0 is a real value, not "absent" -> must be recorded.
    trail = build_test_trail(None, 0)
    assert trail is not None
    assert trail["step"] == 0


def test_build_test_trail_both():
    trail = build_test_trail("login", 2)
    assert trail is not None
    assert trail["scenario"] == "login"
    assert trail["step"] == 2
    assert "timestamp" in trail
