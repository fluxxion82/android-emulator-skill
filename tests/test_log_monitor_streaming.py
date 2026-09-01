"""log_monitor's streaming lifecycle, against recorded logcat output.

Four defects live in one block of `stream_logs`, and they compound: the tool
parses nothing, cannot be stopped, can deadlock, and reports success either way.

  A1  `build_logcat_command` requests `-v time` while `parse_logcat_line`
      expects threadtime, so every line falls into the unparsed branch and
      `--follow` prints nothing at all.
  A2  `--duration` never returns: the loop breaks, then `.wait()` blocks on a
      streaming `adb logcat` that never exits. The duration check also only runs
      after `readline()` returns, so a quiet device never trips it either.
  R3  `Popen(stderr=PIPE)` is never read, so ~64KB on stderr blocks the child.
  --  `stream_logs` returns True unconditionally, so a failed logcat is
      indistinguishable from a quiet device.

The fake-process harness below drives the loop without a device: `readline()`
drains a scripted list then returns "", and `poll()` reports the exit code only
once drained, mirroring a real pipe.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import log_monitor
import pytest

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "emulator-api35"


def _real_log_lines(fmt: str, limit: int = 40) -> list[str]:
    """Real logcat body lines in the given format."""
    text = (RECORDED / f"logcat_{fmt}.txt").read_text(encoding="utf-8")
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.startswith("--------- beginning of")
    ][:limit]


class FakeProc:
    """A stand-in for a streaming `adb logcat` process.

    Deliberately models the two behaviours that broke the real code: the stream
    does not end on its own while lines remain, and the process does not exit
    until it is terminated.
    """

    def __init__(
        self,
        lines: list[str],
        returncode: int = 0,
        never_ends: bool = False,
        stderr_text: str = "",
    ):
        self._lines = list(lines)
        self._returncode = returncode
        self._never_ends = never_ends
        self._emitted = 0
        self._filler = (lines[0] if lines else "01-01 00:00:00.000 1 1 I Tag: x") + "\n"
        self.terminated = False
        self.killed = False
        self.waited_without_timeout = False
        self.stdout = self
        self.stderr = io.StringIO(stderr_text)
        self.returncode = None

    # Safety cap for the never-ending stream. A correct duration check breaks
    # out long before this; hitting it means the duration was never enforced.
    MAX_LINES = 5000

    def readline(self) -> str:
        if self.terminated:
            return ""
        if self._lines:
            return self._lines.pop(0) + "\n"
        if self._never_ends:
            # A busy device streams continuously and the pipe never closes.
            # Blocking for real would hang the suite, so emit endlessly instead
            # and let the safety cap catch a missing duration check.
            self._emitted += 1
            if self._emitted > self.MAX_LINES:
                raise AssertionError(
                    f"stream produced {self.MAX_LINES} lines without stopping: "
                    "the duration deadline is never enforced"
                )
            return self._filler
        return ""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if timeout is None:
            self.waited_without_timeout = True
        if self._never_ends and not self.terminated:
            raise subprocess.TimeoutExpired(cmd="adb logcat", timeout=timeout or 0)
        self.returncode = self._returncode
        return self._returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._returncode

    def kill(self):
        self.killed = True
        self.terminated = True
        self.returncode = self._returncode


@pytest.fixture
def fake_popen(monkeypatch):
    """Install a FakeProc factory and expose the instance it creates."""
    created = {}

    def _install(lines, returncode=0, never_ends=False, stderr_text=""):
        proc = FakeProc(
            lines, returncode=returncode, never_ends=never_ends, stderr_text=stderr_text
        )
        created["proc"] = proc
        created["kwargs"] = {}

        def _popen(cmd, **kwargs):
            created["cmd"] = cmd
            created["kwargs"] = kwargs
            return proc

        monkeypatch.setattr(log_monitor.subprocess, "Popen", _popen)
        return proc

    _install.created = created
    return _install


# ---------------------------------------------------------------------------
# A1 — the requested format must be the one the parser understands.
# ---------------------------------------------------------------------------


def test_command_requests_the_format_the_parser_understands():
    """The `-v` argument and `parse_logcat_line` must agree."""
    monitor = log_monitor.LogMonitor()
    cmd = monitor.build_logcat_command()
    requested = cmd[cmd.index("-v") + 1]

    lines = _real_log_lines(requested)
    parsed = [monitor.parse_logcat_line(line) for line in lines]
    assert any(p is not None for p in parsed), (
        f"command requests '-v {requested}' but the parser matched none of "
        f"{len(lines)} real lines in that format"
    )


def test_severity_counts_reflect_real_output(fake_popen):
    """The whole point of the tool: real logs must produce real counts."""
    monitor = log_monitor.LogMonitor()
    requested = monitor.build_logcat_command()[monitor.build_logcat_command().index("-v") + 1]
    fake_popen(_real_log_lines(requested))

    monitor.stream_logs()
    counted = monitor.error_count + monitor.warning_count + monitor.info_count
    assert counted > 0, "streamed real device output and classified nothing"


# ---------------------------------------------------------------------------
# A2 — duration must terminate, and must not block on a stream that never ends.
# ---------------------------------------------------------------------------


def test_duration_stops_a_stream_that_never_ends(fake_popen, monkeypatch):
    """A live logcat never closes its pipe; the duration must stop it.

    The fake clock jumps past the deadline immediately, so a correct
    implementation exits at once. Without a working deadline the fake stream
    runs to its safety cap and fails the test.
    """
    proc = fake_popen(_real_log_lines("threadtime", limit=3), never_ends=True)

    real_datetime = log_monitor.datetime

    class _Clock(real_datetime):
        _t = real_datetime(2026, 1, 1, 0, 0, 0)

        @classmethod
        def now(cls, tz=None):
            cls._t = cls._t + log_monitor.timedelta(seconds=5)
            return cls._t

    monkeypatch.setattr(log_monitor, "datetime", _Clock)

    log_monitor.LogMonitor().stream_logs(duration=1)
    assert proc.terminated, "process was never stopped; --duration would hang"


def test_never_waits_on_a_streaming_process_without_a_timeout(fake_popen):
    """`.wait()` with no timeout on a live logcat is the A2 hang itself."""
    proc = fake_popen(_real_log_lines("threadtime", limit=3))

    log_monitor.LogMonitor().stream_logs()

    assert not proc.waited_without_timeout, (
        "called wait() with no timeout on a streaming process; if the stream "
        "has not ended this blocks forever"
    )


# ---------------------------------------------------------------------------
# R3 — an unread stderr pipe deadlocks the child.
# ---------------------------------------------------------------------------


def test_adb_errors_are_not_counted_as_device_log_lines(fake_popen):
    """adb's own stderr must not be ingested as device output.

    Merging stderr into stdout avoids the deadlock but feeds text like
    "device 'x' not found" through the log parser, which then reports it as a
    captured log line. Draining stderr separately keeps the two apart.
    """
    fake_popen(
        _real_log_lines("threadtime", limit=3),
        stderr_text="adb: device 'no-such-device' not found\n",
    )
    monitor = log_monitor.LogMonitor()
    monitor.stream_logs()

    assert monitor.total_lines == 3, (
        f"expected only the 3 device log lines, got {monitor.total_lines}; "
        f"adb's stderr is being counted as log output"
    )


def test_adb_error_text_is_surfaced_on_failure(fake_popen, capsys):
    """A failure must say what adb reported, not just that it failed."""
    fake_popen([], returncode=1, stderr_text="adb: device 'no-such-device' not found\n")
    log_monitor.LogMonitor().stream_logs()

    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Exit status must distinguish failure from a quiet device.
# ---------------------------------------------------------------------------


def test_failed_logcat_is_reported_as_failure(fake_popen):
    """`adb logcat` exiting non-zero must not read as success."""
    fake_popen([], returncode=1)
    assert log_monitor.LogMonitor().stream_logs() is False, (
        "logcat exited 1 but stream_logs reported success, so 'no devices' is "
        "indistinguishable from a quiet device"
    )


def test_successful_logcat_is_reported_as_success(fake_popen):
    """Guard against over-correcting into always-False."""
    fake_popen(_real_log_lines("threadtime", limit=3), returncode=0)
    assert log_monitor.LogMonitor().stream_logs() is True


# ---------------------------------------------------------------------------
# An unknown serial must fail fast, not look like a quiet device.
# ---------------------------------------------------------------------------


def test_unknown_serial_fails_before_streaming(monkeypatch, capsys):
    """`adb -s <unknown> logcat` blocks waiting for the device to appear.

    With --duration set that is indistinguishable from a device that logged
    nothing: the capture ends on time and reports success with zero lines. Exit
    status cannot tell them apart, so the serial is checked up front.
    """
    monkeypatch.setattr(
        log_monitor,
        "get_connected_devices",
        lambda: [{"serial": "emulator-5554", "state": "device"}],
    )

    def _no_popen(*_a, **_k):
        raise AssertionError("streamed anyway despite an unknown serial")

    monkeypatch.setattr(log_monitor.subprocess, "Popen", _no_popen)

    monitor = log_monitor.LogMonitor(device_serial="no-such-device")
    assert monitor.stream_logs(duration=1) is False

    err = capsys.readouterr().err
    assert "not attached" in err
    assert "emulator-5554" in err, "the error should name what IS available"


def test_known_serial_proceeds(monkeypatch, fake_popen):
    """Guard against the check rejecting a device that is present."""
    monkeypatch.setattr(
        log_monitor,
        "get_connected_devices",
        lambda: [{"serial": "emulator-5554", "state": "device"}],
    )
    fake_popen(_real_log_lines("threadtime", limit=3))

    monitor = log_monitor.LogMonitor(device_serial="emulator-5554")
    assert monitor.stream_logs() is True
