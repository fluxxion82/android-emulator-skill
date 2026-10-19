"""log_monitor's streaming lifecycle, against recorded logcat output.

Four defects live in one block of `stream_logs`, and they compound: the tool
parses nothing, cannot be stopped, can deadlock, and reports success either way.

  A1  `build_logcat_command` requested `-v time` while `parse_logcat_line`
      expects threadtime, so every line fell into the unparsed branch and
      `--follow` printed nothing at all. (It now reads logd's records, `-B`,
      and prints each as threadtime: the format question is the printer's.)
  A2  `--duration` never returns: the loop breaks, then `.wait()` blocks on a
      streaming `adb logcat` that never exits. The duration check also only runs
      after a read returns, so a quiet device never trips it either.
  R3  `Popen(stderr=PIPE)` is never read, so ~64KB on stderr blocks the child.
  --  `stream_logs` returns True unconditionally, so a failed logcat is
      indistinguishable from a quiet device.

The fake-process harness below drives the loop without a device: its stdout
serves recorded ``logcat -B`` records and then ends, and `poll()` reports the
exit code only once drained, mirroring a real pipe. Its pipes carry bytes, as
the real `Popen` (opened without `text=`) does. Every read also asks adb
``get-state`` first (A4); that is answered as recorded for an attached device.
"""

from __future__ import annotations

import io
import subprocess
from datetime import timedelta

import log_monitor
import pytest
from fake_toolchain import frames

from common import adb_exec, cli, logcat
from common.adb_exec import DeviceNotFoundError

RECORDS = "logcat_app_restart_records.bin"


def _real_records(recorded, limit: int = 40) -> list[bytes]:
    """Real logd records, raw, as `adb logcat -B` streams them."""
    return [frame.raw for frame in frames(recorded.data(RECORDS))[:limit]]


def _shown(records: list[bytes]) -> int:
    """Lines log_monitor's default filter (floor D) keeps of ``records``."""
    order = logcat.PRIORITY_ORDER
    return sum(
        len(record.lines())
        for record in logcat.parse_records(b"".join(records))
        if record.priority in order and order.index(record.priority) >= order.index("D")
    )


@pytest.fixture(autouse=True)
def attached(monkeypatch, recorded):
    """adb answering ``get-state`` as recorded for an attached device (A4's preflight)."""
    asked: list[list[str]] = []

    def run(cmd, **_kwargs):
        asked.append(cmd)
        if "get-state" in cmd:
            return subprocess.CompletedProcess(cmd, 0, recorded.text("adb_get_state"), "")
        raise AssertionError(f"unexpected adb call {cmd}")

    monkeypatch.setattr(adb_exec.subprocess, "run", run)
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])
    return asked


class FakeProc:
    """A stand-in for a streaming `adb logcat -B` process.

    Deliberately models the two behaviours that broke the real code: the stream
    does not end on its own while records remain, and the process does not exit
    until it is terminated.
    """

    # Safety cap for the never-ending stream. A correct duration check breaks
    # out long before this; hitting it means the duration was never enforced.
    MAX_RECORDS = 5000

    def __init__(
        self,
        records: list[bytes],
        returncode: int = 0,
        never_ends: bool = False,
        stderr_text: str = "",
    ):
        self._stream = io.BytesIO(b"".join(records))
        self._filler = records[0] if records else b""
        self._returncode = returncode
        self._never_ends = never_ends
        self._emitted = 0
        self.terminated = False
        self.killed = False
        self.waited_without_timeout = False
        self.stdout = self
        self.stderr = io.BytesIO(stderr_text.encode())
        self.returncode = None

    def read(self, size: int = -1) -> bytes:
        if self.terminated:
            return b""
        chunk = self._stream.read(size)
        if chunk or not self._never_ends:
            return chunk
        # A busy device streams continuously and the pipe never closes.
        # Blocking for real would hang the suite, so emit endlessly instead and
        # let the safety cap catch a missing duration check.
        self._emitted += 1
        if self._emitted > self.MAX_RECORDS:
            raise AssertionError(
                f"stream produced {self.MAX_RECORDS} records without stopping: "
                "the duration deadline is never enforced"
            )
        self._stream = io.BytesIO(self._filler)
        return self._stream.read(size)

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

    def _install(records, returncode=0, never_ends=False, stderr_text=""):
        proc = FakeProc(
            records, returncode=returncode, never_ends=never_ends, stderr_text=stderr_text
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


def _stopped_when_drained(proc: FakeProc, monitor: log_monitor.LogMonitor) -> None:
    """The caller's stop (Ctrl-C) arrives once the stream's records are read.

    A live read ends only when it is stopped -- one that ends by itself is a
    refusal (wave 4) -- so a spec about what a capture counts stops it. The
    interrupt terminates logcat, which ends the read before the loop sees the
    flag: still a stop.
    """
    read = proc.read

    def read_then_stop(size: int = -1) -> bytes:
        chunk = read(size)
        if not chunk:
            monitor.interrupted = True
        return chunk

    proc.read = read_then_stop


# ---------------------------------------------------------------------------
# A1 — the requested format must be one the parser understands.
# ---------------------------------------------------------------------------


def test_command_requests_records_and_the_parser_reads_how_they_print(recorded):
    """`-B`, and `parse_logcat_line` reads what a record prints as."""
    monitor = log_monitor.LogMonitor()
    assert logcat.BINARY_FLAG in monitor.build_logcat_command()

    records = logcat.parse_records(recorded.data(RECORDS))
    lines = [line for record in records[:40] for line in record.lines()]
    parsed = [monitor.parse_logcat_line(line) for line in lines]
    assert len([p for p in parsed if p is not None]) == len(
        lines
    ), "parse_logcat_line does not read every line a recorded record prints as"


def test_severity_counts_reflect_real_output(fake_popen, recorded):
    """The whole point of the tool: real logs must produce real counts."""
    proc = fake_popen(_real_records(recorded))

    monitor = log_monitor.LogMonitor()
    _stopped_when_drained(proc, monitor)
    monitor.stream_logs()
    counted = monitor.error_count + monitor.warning_count + monitor.info_count
    assert counted > 0, "streamed real device output and classified nothing"


# ---------------------------------------------------------------------------
# A2 — duration must terminate, and must not block on a stream that never ends.
# ---------------------------------------------------------------------------


def test_duration_stops_a_stream_that_never_ends(fake_popen, monkeypatch, recorded):
    """A live logcat never closes its pipe; the duration must stop it.

    The fake clock jumps past the deadline immediately, so a correct
    implementation exits at once. Without a working deadline the fake stream
    runs to its safety cap and fails the test.
    """
    proc = fake_popen(_real_records(recorded, limit=3), never_ends=True)

    real_datetime = log_monitor.datetime

    class _Clock(real_datetime):
        _t = real_datetime(2026, 1, 1, 0, 0, 0)

        @classmethod
        def now(cls, tz=None):
            # timedelta from the stdlib: log_monitor never imported it, and the
            # AttributeError this raised was swallowed by the `except Exception`
            # in stream_logs -- which is the only reason the test ever passed.
            cls._t = cls._t + timedelta(seconds=5)
            return cls._t

    monkeypatch.setattr(log_monitor, "datetime", _Clock)

    log_monitor.LogMonitor().stream_logs(duration=1)
    assert proc.terminated, "process was never stopped; --duration would hang"


def test_never_waits_on_a_streaming_process_without_a_timeout(fake_popen, recorded):
    """`.wait()` with no timeout on a live logcat is the A2 hang itself."""
    proc = fake_popen(_real_records(recorded, limit=3))

    monitor = log_monitor.LogMonitor()
    _stopped_when_drained(proc, monitor)
    monitor.stream_logs()

    assert not proc.waited_without_timeout, (
        "called wait() with no timeout on a streaming process; if the stream "
        "has not ended this blocks forever"
    )


# ---------------------------------------------------------------------------
# R3 — an unread stderr pipe deadlocks the child.
# ---------------------------------------------------------------------------


def test_adb_errors_are_not_counted_as_device_log_lines(fake_popen, recorded):
    """adb's own stderr must not be ingested as device output.

    Merging stderr into stdout avoids the deadlock but feeds text like
    "device 'x' not found" through the log reader, which then reports it as a
    captured log line. Draining stderr separately keeps the two apart.
    """
    records = _real_records(recorded, limit=3)
    proc = fake_popen(records, stderr_text=recorded.text("adb_shell_device_not_found"))
    monitor = log_monitor.LogMonitor()
    _stopped_when_drained(proc, monitor)
    monitor.stream_logs()

    assert monitor.total_lines == _shown(records), (
        f"expected only the device's {_shown(records)} lines, got {monitor.total_lines}; "
        f"adb's stderr is being counted as log output"
    )


def test_adb_error_text_is_surfaced_on_failure(fake_popen, recorded):
    """A failure must say what adb reported, not just that it failed.

    adb's recorded words for an unknown serial, classified the way run_adb
    classifies them: a streaming logcat cannot go through run_adb, but a device
    that is not there is the same typed error -- exit 3 at the boundary.
    """
    fake_popen([], returncode=1, stderr_text=recorded.text("adb_shell_device_not_found"))

    with pytest.raises(DeviceNotFoundError) as raised:
        log_monitor.LogMonitor().stream_logs()

    assert "is not attached" in str(raised.value)
    assert "adb devices" in raised.value.remedy


# ---------------------------------------------------------------------------
# Exit status must distinguish failure from a quiet device.
# ---------------------------------------------------------------------------


def test_failed_logcat_is_reported_as_failure(fake_popen, attached):
    """`adb logcat` exiting non-zero must not read as success.

    Raised as cli.Failure, where it was printed and returned False: under
    --json that exited 1 with nothing on stdout. The device is asked again
    first (A4) and is still attached, so it is logcat's failure, not the
    device's.
    """
    fake_popen([], returncode=1)
    with pytest.raises(cli.Failure) as raised:
        log_monitor.LogMonitor().stream_logs()

    assert "exited with status 1" in str(raised.value)
    assert "--serial" in raised.value.remedy
    assert [cmd[-1] for cmd in attached] == ["get-state", "get-state"]


def test_a_read_we_stopped_inside_a_record_loses_it_and_says_so(fake_popen, recorded, capsys):
    """A stop of ours (an interrupt, the deadline) can cut the record arriving.

    That is the one truncation that is not a failure -- and it is disclosed:
    ``statistics.cut_records``, a stderr note, a line in the summary (review of
    PR #35). Derived: three recorded records, then the first 10 bytes of a
    fourth; the interrupt arrives as those bytes do.
    """
    records = _real_records(recorded, limit=4)
    complete = b"".join(records[:3])
    proc = fake_popen([*records[:3], records[3][:10]])
    monitor = log_monitor.LogMonitor()
    read = proc.read

    def interrupted_read(size=-1):
        if proc._stream.tell() >= len(complete):
            monitor.interrupted = True
        return read(size)

    proc.read = interrupted_read

    assert monitor.stream_logs() is True
    assert monitor.cut_records == 1
    assert monitor.total_lines == _shown(records[:3])
    assert "stopped while a record was arriving" in capsys.readouterr().err
    assert monitor.get_json_output()["statistics"]["cut_records"] == 1
    assert "INCOMPLETE" in monitor.get_summary()


def test_successful_logcat_is_reported_as_success(fake_popen, recorded):
    """Guard against over-correcting into always-False: a read the caller stopped."""
    proc = fake_popen(_real_records(recorded, limit=3), returncode=0)
    monitor = log_monitor.LogMonitor()
    _stopped_when_drained(proc, monitor)
    assert monitor.stream_logs() is True


def test_a_live_read_that_ends_by_itself_is_refused_with_what_it_read(
    fake_popen, attached, recorded
):
    """Nobody stopped it and adb exited 0, the device still there: a refusal, not the capture.

    It returned True with a note on stderr. A live read ends only when it is
    stopped, so an end of its own cannot show the observation asked for was
    made (wave 4's rule): the device is asked again, and -- still there --
    the read is refused, with what it read under ``partial``.
    """
    records = _real_records(recorded, limit=3)
    fake_popen(records, returncode=0)
    monitor = log_monitor.LogMonitor()
    with pytest.raises(cli.Failure) as raised:
        monitor.stream_logs()

    assert "ended the live read by itself (exit status 0)" in str(raised.value)
    assert f"It had read {_shown(records)} line(s)" in str(raised.value)
    assert raised.value.details["partial"]["statistics"]["total_lines"] == _shown(records)
    assert [cmd[-1] for cmd in attached] == ["get-state", "get-state"]


# ---------------------------------------------------------------------------
# An unknown serial must fail fast, not look like a quiet device.
# ---------------------------------------------------------------------------


def test_unknown_serial_fails_before_streaming(monkeypatch, recorded):
    """`adb -s <unknown> logcat` blocks waiting for the device to appear.

    With --duration set that is indistinguishable from a device that logged
    nothing: the capture ends on time and reports success with zero lines. Exit
    status cannot tell them apart, so the device is asked first: `get-state`,
    which answers at once (recorded: adb_device_not_found, exit 1).
    """
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "adb_device_not_found")
    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(
            cmd, entry["exit_status"], "", recorded.text("adb_device_not_found")
        ),
    )

    def _no_popen(*_a, **_k):
        raise AssertionError("streamed anyway despite an unknown serial")

    monkeypatch.setattr(log_monitor.subprocess, "Popen", _no_popen)

    monitor = log_monitor.LogMonitor(device_serial="no-such-device")
    # Raised, not printed-and-False: an unavailable device is exit 3 at the CLI
    # boundary, the same as in every other script.
    with pytest.raises(DeviceNotFoundError) as raised:
        monitor.stream_logs(duration=1)

    assert "not attached" in str(raised.value)
    assert "emulator-5554" in str(raised.value), "the error should name what IS available"


def test_known_serial_proceeds(fake_popen, recorded):
    """Guard against the check rejecting a device that is present."""
    proc = fake_popen(_real_records(recorded, limit=3))

    monitor = log_monitor.LogMonitor(device_serial="emulator-5554")
    _stopped_when_drained(proc, monitor)
    assert monitor.stream_logs() is True
