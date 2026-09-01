"""Device-free tests for log_monitor's pure arg->command mapping and caps.

Covers the two curated deltas:
  1. ``--last DURATION`` historical window built via ``logcat -d -t "<timestamp>"``.
  2. Output caps made env-configurable via ``ANDROID_EMU_LOG_*`` (defaults preserved).

All adb/subprocess interaction is mocked, so these run without a device.
"""

from __future__ import annotations

import importlib
import io
from datetime import datetime

import log_monitor


def _monitor(**kwargs) -> log_monitor.LogMonitor:
    return log_monitor.LogMonitor(**kwargs)


# --- build_logcat_command: base streaming mapping -------------------------------


def test_build_command_base_streaming():
    cmd = _monitor().build_logcat_command()
    # No serial -> no -s; streaming form has neither -d nor -t.
    assert cmd[0] == "adb"
    assert "-d" not in cmd
    assert "-t" not in cmd
    # threadtime, not time: parse_logcat_line needs the PID/TID columns.
    # This previously asserted "time", locking in a parser that matched
    # nothing (A1).
    assert cmd[cmd.index("-v") + 1] == "threadtime"
    assert cmd.index("logcat") == 1


def test_build_command_includes_serial():
    cmd = _monitor(device_serial="emulator-5554").build_logcat_command()
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]


def test_build_command_includes_pid_filter():
    cmd = _monitor(app_package="com.myapp").build_logcat_command(pid="4242")
    assert "--pid=4242" in cmd


def test_build_command_omits_pid_when_none():
    cmd = _monitor(app_package="com.myapp").build_logcat_command(pid=None)
    assert not any(part.startswith("--pid=") for part in cmd)


# --- delta 1: --last historical window (logcat -d -t) ---------------------------


def test_build_command_last_window_uses_dump_and_tail():
    now = datetime(2026, 6, 17, 12, 30, 0)
    cmd = _monitor().build_logcat_command(last_minutes=5, now=now)
    # Historical window must dump-and-exit (-d) and tail from a timestamp (-t).
    assert "-d" in cmd
    t_index = cmd.index("-t")
    # Timestamp is "MM-DD HH:MM:SS.000" for now - 5 minutes => 12:25:00.
    assert cmd[t_index + 1] == "06-17 12:25:00.000"


def test_build_command_last_window_hour_offset():
    now = datetime(2026, 6, 17, 12, 30, 0)
    cmd = _monitor().build_logcat_command(last_minutes=60, now=now)
    t_index = cmd.index("-t")
    assert cmd[t_index + 1] == "06-17 11:30:00.000"


def test_streaming_command_has_no_dump_flag():
    # Live streaming (last_minutes=None) must not request a one-shot dump.
    cmd = _monitor().build_logcat_command(last_minutes=None)
    assert "-d" not in cmd


# --- severity -> minimum logcat priority ----------------------------------------


def test_severity_min_priority_default():
    # Default filter is error,warning,info,debug -> minimum letter is D.
    assert _monitor()._severity_min_priority() == "D"


def test_severity_min_priority_errors_only():
    assert _monitor(severity_filter=["error"])._severity_min_priority() == "E"


def test_severity_min_priority_warning_and_error():
    assert _monitor(severity_filter=["warning", "error"])._severity_min_priority() == "W"


def test_severity_min_priority_verbose():
    assert _monitor(severity_filter=["verbose"])._severity_min_priority() == "V"


def test_severity_filter_appended_to_command():
    cmd = _monitor(severity_filter=["error"]).build_logcat_command()
    assert "*:E" in cmd


# --- stream_logs integration: --last builds the historical command --------------


def test_stream_logs_last_window_runs_dump_command(monkeypatch):
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured["cmd"] = cmd
            # Empty stream: readline() returns "" immediately (EOF).
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            # Accepts a timeout because stream_logs must never wait unbounded
            # on a process that may not exit (A2).
            return 0

        def poll(self):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    # No app package -> _resolve_app_pid returns None without calling adb.
    monkeypatch.setattr(log_monitor.subprocess, "Popen", _FakePopen)

    ok = _monitor().stream_logs(last_minutes=5)
    assert ok is True
    assert "-d" in captured["cmd"]
    assert "-t" in captured["cmd"]


def test_resolve_app_pid_returns_pid(monkeypatch):
    # adb now goes through common.adb_exec, so that is where the fake belongs.
    import subprocess as real_subprocess

    from common import adb_exec

    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: real_subprocess.CompletedProcess(cmd, 0, "  4242 \n", ""),
    )
    assert _monitor(app_package="com.myapp")._resolve_app_pid() == "4242"


def test_resolve_app_pid_handles_not_running(monkeypatch):
    """`pidof` exits non-zero when the app is not running: an answer, not a failure."""
    import subprocess as real_subprocess

    from common import adb_exec

    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: real_subprocess.CompletedProcess(cmd, 1, "", ""),
    )
    assert _monitor(app_package="com.myapp")._resolve_app_pid() is None


# --- delta 2: env-configurable output caps (ANDROID_EMU_LOG_*) ------------------


def test_caps_default_to_historical_values():
    # Defaults must match the pre-delta hardcoded behavior.
    assert log_monitor.LOG_LINE_MAX == 120
    assert log_monitor.LOG_TAIL == 50
    assert log_monitor.LOG_TEXT_SUMMARY_CAP == 5
    assert log_monitor.LOG_JSON_CAP == 20
    assert log_monitor.LOG_INFO_CAP == 20


def test_caps_are_env_configurable(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_LOG_LINE_MAX", "200")
    monkeypatch.setenv("ANDROID_EMU_LOG_TAIL", "10")
    monkeypatch.setenv("ANDROID_EMU_LOG_TEXT_SUMMARY", "3")
    monkeypatch.setenv("ANDROID_EMU_LOG_JSON_CAP", "7")
    monkeypatch.setenv("ANDROID_EMU_LOG_INFO_CAP", "2")
    reloaded = importlib.reload(log_monitor)
    try:
        assert reloaded.LOG_LINE_MAX == 200
        assert reloaded.LOG_TAIL == 10
        assert reloaded.LOG_TEXT_SUMMARY_CAP == 3
        assert reloaded.LOG_JSON_CAP == 7
        assert reloaded.LOG_INFO_CAP == 2
    finally:
        # Restore module-level defaults for any later tests in the session.
        monkeypatch.undo()
        importlib.reload(log_monitor)


def test_json_output_respects_json_cap(monkeypatch):
    monkeypatch.setattr(log_monitor, "LOG_JSON_CAP", 2)
    monitor = _monitor()
    monitor.errors = ["e1", "e2", "e3", "e4"]
    monitor.warnings = ["w1", "w2", "w3"]
    out = monitor.get_json_output()
    assert out["errors"] == ["e1", "e2"]
    assert out["warnings"] == ["w1", "w2"]
