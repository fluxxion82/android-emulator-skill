"""Device-free tests for anr_watcher's command construction and session flow.

All adb/subprocess interaction is mocked. Session storage is redirected to
``tmp_path`` via ``SessionStore(base_dir=...)`` so nothing touches the real home.

Every stream fed to a reader here is RECORDED: logd's records as ``logcat -B``
hands them over (``tests/fixtures/recorded/*_records.bin``), the format every
ANR reader reads since PR #34 adopted common.logcat's record reader (W3-L) --
or a recorded record with one field substituted (``fake_toolchain.Frame
.replace``), said where it is done. Text fixtures still feed the line
classifier where events are built directly (``parse_logcat_anr``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import replace
from zoneinfo import ZoneInfo

import anr_watcher
import pytest
from fake_toolchain import (
    FakeToolchain,
    Frame,
    Rule,
    assert_refusal,
    framed,
    frames,
    recorded_zone,
)

from common import cli, env_config, logcat
from common.anr_pipeline import build_normalised_event, event_to_jsonl, parse_logcat_anr
from common.anr_sessions import SessionStore

# --- logcat command construction (no shell=True) ---------------------------


def test_build_logcat_command_streaming():
    cmd = anr_watcher.AnrWatcher().build_logcat_command()
    assert cmd[0] == "adb"
    assert cmd[1] == "logcat"
    # Live stream: no dump flag.
    assert "-d" not in cmd
    assert "-t" not in cmd
    # logd's records, not text: each carries the pid logd took from the
    # writing socket, which no tag can forge (A1, common.logcat).
    assert "-B" in cmd
    assert "-v" not in cmd


def test_a_live_stream_starts_at_the_end_of_the_buffer():
    """Without ``-T``, `adb logcat` replays the whole ring buffer first.

    Found by the U6 acceptance run on emulator-5554: one provoked ANR, and the
    session reported 33 events -- every ANR and jank line still buffered from
    earlier, all stamped as the session's first tenth of a second. ``-T 1``
    starts at the end of the buffer. The historical dump keeps its own window.
    """
    live = anr_watcher.AnrWatcher(serial="emulator-5554").build_logcat_command()
    assert live[live.index("-T") + 1] == "1"

    dump = anr_watcher.AnrWatcher().build_logcat_command(since="06-17 12:25:00.000")
    assert "-T" not in dump


def test_build_logcat_command_includes_serial():
    cmd = anr_watcher.AnrWatcher(serial="emulator-5554").build_logcat_command()
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]


def test_build_logcat_command_since_historical_window():
    cmd = anr_watcher.AnrWatcher().build_logcat_command(since="06-17 12:25:00.000")
    # Historical dump-and-exit.
    assert "-d" in cmd
    t_index = cmd.index("-t")
    assert cmd[t_index + 1] == "06-17 12:25:00.000"
    assert "-B" in cmd


def test_since_timestamp_offset():
    # _compute_start_timestamp returns a "MM-DD HH:MM:SS.000" form.
    ts = anr_watcher._compute_start_timestamp("5m")
    assert len(ts) == len("06-17 12:25:00.000")
    assert ts.endswith(".000")


def test_watch_uses_subprocess_without_shell(monkeypatch):
    captured: dict = {}

    class _FakePopen:
        """A logcat whose stdout is a pipe that is already at its end."""

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            read_end, write_end = os.pipe()
            os.close(write_end)
            self.stdout = os.fdopen(read_end, "rb", buffering=0)
            captured["stdout"] = self.stdout

        def poll(self):
            return 0

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _FakePopen)
    try:
        ok = anr_watcher.AnrWatcher().watch(json_mode=True)
    finally:
        captured["stdout"].close()
    assert ok is True
    # House rule: never shell=True.
    assert captured["kwargs"].get("shell") is not True
    assert "shell" not in captured["kwargs"]
    assert captured["cmd"][0] == "adb"


def test_show_since_runs_dump_command(monkeypatch):
    """--since is one bounded `logcat -d -t` read, through adb_exec.run_adb."""
    captured: dict = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(anr_watcher.subprocess, "run", _fake_run)
    ok = anr_watcher.AnrWatcher().show_since("5m", json_mode=True)
    assert ok is True
    assert "-d" in captured["cmd"]
    assert "-t" in captured["cmd"]
    assert captured["kwargs"].get("shell") is not True
    assert captured["kwargs"].get("timeout"), "a logcat dump can hang without a timeout"


# --- post-parse --package filter -------------------------------------------


def _recorded_anr(recorded) -> dict:
    """The recorded ActivityManager ANR line, parsed.

    Note what it does NOT have: a component. That is measured, not a
    simplification -- a broadcast ANR names the package and nothing else.
    """
    line = next(ln for ln in recorded.lines("logcat_anr_broadcast") if "ANR in " in ln)
    event = parse_logcat_anr(line)
    assert event is not None
    return event


# --- logd's records, as recorded ----------------------------------------------

RESTART = "logcat_app_restart_records"
BROADCAST = "logcat_anr_broadcast_records"
INPUT = "logcat_anr_input_records"
INJECTION = "logcat_tag_injection_records"
PACKAGE = "com.example.composefixture"


def _frames(recorded, name: str) -> list[Frame]:
    """A recorded ``logcat -B`` dump's records, raw."""
    return frames(recorded.data(f"{name}.bin"))


def _entry(recorded, name: str) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == name)


def _system_server(recorded, name: str) -> str:
    """system_server's pid as `pidof system_server` answered in ``name``'s own run."""
    pid = str(_entry(recorded, name).get("system_server_pid", ""))
    if not pid.isdigit():
        pytest.fail(f"{name} records no system_server pid; re-record it")
    return pid


def _oracle(recorded, name: str) -> anr_watcher._ProcessNames:
    """A resolver that knows system_server as measured with ``name``, and names nothing."""
    system_server = _system_server(recorded, name)
    return anr_watcher._ProcessNames(system_server_pid=lambda: system_server)


def _moved(frame: Frame, seconds: float) -> Frame:
    """The recorded record with only its time moved ``seconds`` (its sec/nsec fields)."""
    raw = bytearray(frame.raw)
    sec, nsec = struct.unpack_from("<II", raw, 12)
    total = sec * 1_000_000_000 + nsec + round(seconds * 1_000_000_000)
    struct.pack_into("<II", raw, 12, total // 1_000_000_000, total % 1_000_000_000)
    return Frame(bytes(raw))


def _jank_records(recorded, frame_counts, pid: str | None = None) -> list[Frame]:
    """The first recorded Choreographer record once per count, only the count (and pid) changed."""
    template = next(f for f in _frames(recorded, RESTART) if f.tag == "Choreographer")
    out = []
    for count in frame_counts:
        frame = template.replace(
            message=re.sub(r"Skipped \d+ frames", f"Skipped {count} frames", template.message)
        )
        out.append(frame.replace(pid=pid) if pid else frame)
    return out


def _history(recorded) -> Frame:
    """A record already in the buffer -- what `logcat -T 1` replays first: the recording's first."""
    return _frames(recorded, RESTART)[0]


def _anr_record(recorded, name: str = BROADCAST) -> Frame:
    """ActivityManager's ANR report: ONE record, its PID and Reason lines in its message."""
    return next(
        f
        for f in _frames(recorded, name)
        if f.tag == "ActivityManager" and f.message.startswith("ANR in ")
    )


def _stated_pid(record: Frame) -> str:
    """The stalled app's pid, as the report's own ``PID:`` line states it."""
    return re.search(r"\nPID: (\d+)", record.message).group(1)


def _printed(record: Frame) -> str:
    """The record as LogRecord.lines prints it (its first line)."""
    return logcat.parse_records(record.raw)[0].lines()[0]


def test_matches_package_by_anr_package(recorded):
    event = _recorded_anr(recorded)
    assert event.get("component") is None, "the recording grew a component; revisit this test"

    assert anr_watcher.matches_package(event, "com.example.composefixture") is True
    assert anr_watcher.matches_package(event, "com.other.thing") is False


def test_matches_package_by_short_name(recorded):
    """Short-suffix matching, and how lenient it really is.

    The filter also matches on the last dotted segment, so an unrelated package
    that happens to end the same way matches too. Kept because the tag-only
    jank case has no package to match on at all -- but worth seeing against a
    real event rather than a typed one.
    """
    event = _recorded_anr(recorded)
    assert anr_watcher.matches_package(event, "com.somebody.else.composefixture") is True


# --- --stop summary over a seeded events.jsonl -----------------------------


def _seed_session(recorded, tmp_path, frames_list, package=None):
    """Create a session dir + seed events.jsonl with normalised jank events.

    Each seeded line is the RECORDED Choreographer line with only its frame
    count substituted. The counts have to vary -- these tests are about
    severity ranking -- but nothing else about the line is invented.
    """
    template = next(ln for ln in recorded.lines("logcat_choreographer_jank") if "Skipped" in ln)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"package": package} if package else {})
    store.claim_worker(meta.session_id, pid=99999)
    path = store.events_path(meta.session_id)
    with open(path, "a") as handle:
        for i, frames in enumerate(frames_list):
            raw = parse_logcat_anr(
                re.sub(r"Skipped \d+ frames", f"Skipped {frames} frames", template)
            )
            assert raw is not None
            event = build_normalised_event(raw, session_start_ms=0, current_ms=(i + 1) * 100)
            handle.write(event_to_jsonl(event) + "\n")
    return store, meta.session_id


def test_stop_builds_summary_over_seeded_events(recorded, tmp_path, monkeypatch):
    store, session_id = _seed_session(recorded, tmp_path, [35, 65, 150])
    buster = anr_watcher.AnrBuster(store=store)
    # signal_worker would try os.kill the fake pid; stub it out.
    monkeypatch.setattr(store, "signal_worker", lambda *_a, **_k: False)

    out = buster.stop(session_id)
    # L1 default output: header + drill hint over the seeded clusters.
    assert session_id in out
    assert "Drill:" in out
    # Stored summary reflects the seeded events.
    summary = store.load_summary(session_id)
    assert summary is not None
    assert summary.event_count == 3


def test_stop_terse_is_one_line(recorded, tmp_path, monkeypatch):
    store, session_id = _seed_session(recorded, tmp_path, [120])
    buster = anr_watcher.AnrBuster(store=store)
    monkeypatch.setattr(store, "signal_worker", lambda *_a, **_k: False)
    out = buster.stop(session_id, terse=True)
    assert "\n" not in out


def test_stop_json_mode(recorded, tmp_path, monkeypatch):
    store, session_id = _seed_session(recorded, tmp_path, [35, 150])
    buster = anr_watcher.AnrBuster(store=store)
    monkeypatch.setattr(store, "signal_worker", lambda *_a, **_k: False)
    out = buster.stop(session_id, json_mode=True)
    payload = json.loads(out)
    assert payload["session_id"] == session_id
    assert payload["event_count"] == 2


# --- worker stream parsing (frame threshold + package filter) --------------


def test_worker_drops_subthreshold_and_keeps_anr(recorded, tmp_path):
    """The worker's read loop drops low-frame jank but keeps ANRs -- one per ANR.

    Fed through a real pipe that ends, the way logcat's stdout reaches it: the
    recorded Choreographer record at 5 and 80 frames, then the recorded
    broadcast ANR's report -- ONE record, its PID and Reason lines inside it
    (D5). system_server's pid is the one measured with that recording.
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=1)
    buster = anr_watcher.AnrBuster(store=store)

    jank = _jank_records(recorded, (5, 80))
    anr = _anr_record(recorded)
    read_end, write_end = os.pipe()
    os.write(write_end, framed([*jank, anr]))
    os.close(write_end)

    class _EndedLogcat:
        def __init__(self):
            self.stdout = os.fdopen(read_end, "rb", buffering=0)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
    proc = _EndedLogcat()
    try:
        with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
            buster._read_stream_into_events(
                proc=proc,
                out_handle=out_handle,
                stop_flag={"value": False},
                counters=counters,
                package=None,
                min_frames=30,
                session_start_ms=0,
                process_names=_oracle(recorded, BROADCAST),
            )
    finally:
        proc.stdout.close()

    assert counters["total"] == 3
    assert counters["matched"] == 3, "two jank records and one ANR report"
    assert counters["dropped"] == 1
    events = store.read_events(meta.session_id)
    assert sorted(e.kind for e in events) == ["anr", "jank"]
    (anr_event,) = [e for e in events if e.kind == "anr"]
    assert anr_event.app_pid == int(_stated_pid(anr)), "the report's PID: line was not read"
    assert anr_event.reason and "PROVOKE_ANR" in anr_event.reason


# ---------------------------------------------------------------------------
# N16: what select() calls readable is what the worker reads
# ---------------------------------------------------------------------------

# A logcat that has gone quiet: it writes the burst it is handed in ONE write,
# says so on stderr, then holds its stdout open until the test closes its
# stdin -- which is what a live `adb logcat` looks like between bursts.
_QUIET_STREAM = (
    "import sys\n"
    "with open(sys.argv[1], 'rb') as burst:\n"
    "    sys.stdout.buffer.write(burst.read())\n"
    "sys.stdout.buffer.flush()\n"
    "sys.stderr.write('written\\n')\n"
    "sys.stderr.flush()\n"
    "sys.stdin.read()\n"
)


def _reap(proc: subprocess.Popen) -> None:
    """Let a stand-in stream end, wait for it, and close every pipe it had."""
    proc.stdin.close()
    proc.wait(timeout=30)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _recorded_jank_burst(recorded, frame_counts) -> list[str]:
    """The recorded Choreographer line once per count, only the count changed."""
    template = next(ln for ln in recorded.lines("logcat_choreographer_jank") if "Skipped" in ln)
    return [re.sub(r"Skipped \d+ frames", f"Skipped {n} frames", template) for n in frame_counts]


def test_n16_a_stop_while_the_stream_is_open_keeps_every_record_already_delivered(
    recorded, tmp_path, monkeypatch
):
    """N16: the worker select()ed the fd, then read from a buffered file object.

    A buffered read takes more than select() said was there and parks the
    rest where select() cannot see it, so a burst arrives as one item
    returned, the rest parked, and the fd empty: select() reports nothing to
    read, and the parked items wait for the NEXT burst -- or, when --stop
    comes first, are never read at all. Reviewer-reproduced: four written,
    one kept.

    Deterministic, no sleeps. The stream has finished its one write before the
    reader starts (the writer says so on stderr), and the stop is set at the
    moment the reader's own select() first reports the stream idle: the point
    where it believes it has read everything. Anything not processed by then
    is lost, which is exactly the defect. Four recorded jank records.
    """
    burst = _jank_records(recorded, (31, 45, 52, 120))
    burst_file = tmp_path / "burst.bin"
    burst_file.write_bytes(framed(burst))

    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=1)
    buster = anr_watcher.AnrBuster(store=store)
    counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
    stop_flag = {"value": False}

    real_select = select.select
    idle = threading.Event()

    def select_noting_idle(rlist, wlist, xlist, timeout=None):
        ready = real_select(rlist, wlist, xlist, timeout)
        if not ready[0]:
            idle.set()
        return ready

    monkeypatch.setattr(anr_watcher.select, "select", select_noting_idle)

    proc = subprocess.Popen(
        [sys.executable, "-c", _QUIET_STREAM, str(burst_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        bufsize=0,
    )
    try:
        if proc.stderr.readline().strip() != b"written":
            pytest.fail("the stand-in stream did not write its burst")
        with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
            reader = threading.Thread(
                target=buster._read_stream_into_events,
                kwargs={
                    "proc": proc,
                    "out_handle": out_handle,
                    "stop_flag": stop_flag,
                    "counters": counters,
                    "package": None,
                    "min_frames": 30,
                    "session_start_ms": 0,
                },
                daemon=True,
            )
            reader.start()
            if not idle.wait(timeout=30):
                pytest.fail("the reader never reported the stream idle")
            stop_flag["value"] = True
            reader.join(timeout=30)
            if reader.is_alive():
                pytest.fail("the reader did not stop when asked")
    finally:
        stop_flag["value"] = True
        _reap(proc)

    assert counters["total"] == len(
        burst
    ), f"{len(burst)} records were delivered before the stop and {counters['total']} were read"
    assert len(store.read_events(meta.session_id)) == len(burst)


# ---------------------------------------------------------------------------
# N14: ANDROID_EMU_ANR_MAX_RESTARTS=0 means no restarts
# ---------------------------------------------------------------------------


def _logcat_through(monkeypatch, fake) -> None:
    """Answer the worker's `adb logcat` with ``fake``, run the host's `ps`, refuse the rest.

    The worker asks the host's `ps` about its own pid when it claims its
    session (common.anr_sessions), and ``subprocess.run`` is built on Popen --
    which these tests replace for the one module both share. Nothing else is
    run for real: no adb call can leave the test.
    """
    real = subprocess.Popen

    def popen(cmd, *args, **kwargs):
        if cmd[0] == "ps":
            return real(cmd, *args, **kwargs)
        if "logcat" in cmd:
            return fake(cmd, **kwargs)
        raise AssertionError(f"the test's worker ran {cmd}")

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", popen)


def test_n14_max_restarts_zero_spawns_the_stream_once(tmp_path, monkeypatch):
    """N14: ``env_int`` floors at 1 by default, so an explicit 0 became 1.

    "Do not restart the stream" is a real setting -- a caller who wants a dead
    device to show as a crashed session at once, not after a retry -- and the
    worker read it with the default floor and restarted anyway.
    """
    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "0")
    monkeypatch.setattr(anr_watcher, "RESTART_BACKOFF_SECONDS", 0.0)
    # run_worker installs SIGTERM/SIGINT handlers; keep them off the test runner.
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)

    spawned: list[list[str]] = []
    pipes = []

    class _StreamThatEnds:
        """A logcat whose stream ends at once: a pipe with its write end closed."""

        def __init__(self, cmd, **_kwargs):
            spawned.append(cmd)
            read_end, write_end = os.pipe()
            os.close(write_end)
            self.stdout = os.fdopen(read_end, "rb", buffering=0)
            pipes.append(self.stdout)

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            return None

        def kill(self):
            return None

    _logcat_through(monkeypatch, _StreamThatEnds)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"serial": "emulator-5554"})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for pipe in pipes:
            pipe.close()

    assert len(spawned) == 1, f"MAX_RESTARTS=0 spawned logcat {len(spawned)} times"
    assert store.load_meta(meta.session_id).status == "crashed"
    assert store.load_meta(meta.session_id).pid is None, "a crashed worker's pid was kept"


# ---------------------------------------------------------------------------
# X5: an unreadable session is a failure, not a printed sentence at exit 0
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A session store rooted in tmp_path, so nothing reads the real home.

    The env override is set as well as the constructor argument, because the
    CLI tests below run ``main()``, which builds its own ``AnrBuster`` -- and a
    stubbed-out class would be a test of the stub. ``ANDROID_EMU_ANR_HOME``
    exists for exactly this, and is read per call rather than at import.
    """
    monkeypatch.setenv("ANDROID_EMU_ANR_HOME", str(tmp_path))
    return SessionStore(base_dir=tmp_path / "anr-sessions")


def _buster(store):
    return anr_watcher.AnrBuster(store=store)


def test_get_details_on_an_unknown_session_raises_with_a_remedy(store):
    """It returned the sentence as the answer, and main printed it and exited 0."""
    with pytest.raises(anr_watcher.SessionError) as excinfo:
        _buster(store).get_details("anr-20260101-000000-dead")

    message = str(excinfo.value)
    assert "Unknown session" in message
    assert "--list-sessions" in message, "the failure names no remedy"


def test_get_details_without_a_summary_raises_with_a_remedy(store):
    """A session that was started and never stopped has no summary to drill into."""
    meta = store.create({"package": "com.example.app"})

    with pytest.raises(anr_watcher.SessionError) as excinfo:
        _buster(store).get_details(meta.session_id)

    assert "--stop" in str(excinfo.value), "the failure names no remedy"


def test_diff_without_summaries_raises_and_names_both_sessions(store):
    with pytest.raises(anr_watcher.SessionError) as excinfo:
        _buster(store).diff("anr-20260101-000000-aaaa", "anr-20260101-000000-bbbb")

    message = str(excinfo.value)
    assert "anr-20260101-000000-aaaa" in message
    assert "anr-20260101-000000-bbbb" in message
    assert "--stop" in str(excinfo.value), "the failure names no remedy"


@pytest.mark.parametrize(
    "argv",
    [
        ["--get-details", "anr-20260101-000000-dead", "--json"],
        ["--diff", "anr-20260101-000000-aaaa", "anr-20260101-000000-bbbb", "--json"],
    ],
    ids=["get-details", "diff"],
)
def test_the_cli_exits_non_zero_and_puts_the_error_in_the_json(monkeypatch, capsys, argv, store):
    """The shape the contract asks for: exit != 0, and {"error": ...} on stdout.

    Printing the message on stderr instead would leave a caller that asked for
    JSON parsing an empty stdout; printing it on stdout at exit 0, which is
    what X5 was, leaves it parsing prose and calling it success.
    """
    monkeypatch.setattr(anr_watcher.sys, "argv", ["anr_watcher.py", *argv])

    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert payload["success"] is False, f"--json reported no failure: {payload}"
    assert "anr-20260101-000000" in payload["error"]


def test_a_cluster_index_past_the_end_is_an_error(store, monkeypatch):
    """`--cluster 9` on a two-cluster session answered with a sentence at exit 0."""
    meta = store.create({"package": "com.example.app"})
    summary = store.build_summary(meta.session_id)
    store.stop(meta.session_id, summary)

    with pytest.raises(anr_watcher.SessionError) as excinfo:
        _buster(store).get_details(meta.session_id, cluster=9)

    assert "--cluster" in str(excinfo.value), "the failure names no remedy"


# ---------------------------------------------------------------------------
# D7: malformed arguments are usage errors (exit 2), not tracebacks
# ---------------------------------------------------------------------------


def test_a_bad_older_than_exits_two_and_names_the_accepted_forms(monkeypatch, capsys, store):
    """`--clear-sessions --older-than 24hours` raised out of the store.

    The plural is the natural thing to type and the one thing the grammar does
    not take. Nothing is deleted on the way to the error: the cutoff is
    resolved before the store walks the directory.
    """
    monkeypatch.setattr(
        anr_watcher.sys,
        "argv",
        ["anr_watcher.py", "--clear-sessions", "--older-than", "24hours"],
    )

    with pytest.raises(SystemExit) as exc:
        anr_watcher.main()

    assert exc.value.code == 2, "a bad flag value is a usage error"
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "24h" in stderr, "the message does not name an accepted form"


def test_a_bad_older_than_deletes_nothing(monkeypatch, store):
    """The exit status is not the only thing at stake: this flag removes files."""
    meta = store.create({"package": "com.example.app"})
    monkeypatch.setattr(
        anr_watcher.sys,
        "argv",
        ["anr_watcher.py", "--clear-sessions", "--older-than", "24hours"],
    )

    with pytest.raises(SystemExit):
        anr_watcher.main()

    assert store.session_dir(meta.session_id).exists(), "a session was deleted before the error"


@pytest.mark.parametrize(
    "argv",
    [
        ["--diff", "a/b", "c"],
        ["--diff", "ok-id", "../escape"],
        ["--get-details", "a/b"],
    ],
    ids=["diff-first", "diff-second", "get-details"],
)
def test_a_malformed_session_id_exits_two_and_shows_the_shape(monkeypatch, capsys, argv, store):
    """`--diff a/b c` reached the terminal as a ValueError traceback (D7).

    A session id is also a path segment, so the store refuses anything that
    could leave its directory. That refusal already spells out the accepted
    shape; it just had no boundary to be reported at.
    """
    monkeypatch.setattr(anr_watcher.sys, "argv", ["anr_watcher.py", *argv])

    with pytest.raises(SystemExit) as exc:
        anr_watcher.main()

    assert exc.value.code == 2, "a malformed id is a usage error"
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "Invalid session id" in stderr
    assert "anr-" in stderr, "the message does not show the accepted shape"


# ---------------------------------------------------------------------------
# --stop: the last script-local failure shape is gone
# ---------------------------------------------------------------------------


def test_stop_with_a_malformed_id_is_a_usage_error_like_the_other_modes(monkeypatch, capsys, store):
    """--stop routed the store's ValueError through a script-local ``_fail``.

    That printed ``{"error": ...}`` and exited 1 itself -- the one shape left in
    this file that was not the boundary's. A malformed id is now what it is for
    --get-details and --diff: a usage error, before anything is signalled.
    """
    monkeypatch.setattr(anr_watcher.sys, "argv", ["anr_watcher.py", "--stop", "a/b", "--json"])

    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Invalid session id" in captured.err
    assert captured.out == "", "a usage error printed a JSON object"


def test_stop_on_a_damaged_session_is_the_boundarys_refusal(monkeypatch, capsys, store):
    """The ValueError left once the id is valid: a meta.json that does not parse."""
    meta = store.create({})
    (store.session_dir(meta.session_id) / "meta.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        anr_watcher.sys, "argv", ["anr_watcher.py", "--stop", meta.session_id, "--json"]
    )

    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"success", "error", "remedy"}
    assert meta.session_id in payload["error"]
    assert "--clear-sessions" in payload["remedy"]


# ---------------------------------------------------------------------------
# Legacy modes through the CLI, against a fake adb that replays recordings
# ---------------------------------------------------------------------------


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


@pytest.mark.parametrize("mode", [("--since", "1m"), ("--watch",)], ids=["since", "watch"])
def test_one_stall_in_the_stream_is_one_printed_event(mode, toolchain, recorded):
    """D5 end to end, over every record a reader sees of one broadcast ANR.

    The recorded records: ActivityManager's report (one record), then
    WindowManager's report and ActivityManager's "Skipping duplicate" verdict.
    ``--watch`` here reads a stream that ends, exit 0 -- a stream adb ended
    cleanly is not a failure, so this also pins that X6's check is on the
    status, not on the stream merely ending.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, BROADCAST) + "\n"),
            Rule(
                ("logcat",),
                buffered=framed([_history(recorded)]),
                stdout=recorded.data(f"{BROADCAST}.bin"),
            ),
        ],
    )

    result = toolchain.run("anr_watcher.py", *mode, "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["kind"] for event in events] == ["anr"], events
    assert "Broadcast of Intent" in events[0]["reason"]
    assert isinstance(events[0]["app_pid"], int)


def test_text_mode_shows_what_timed_out(toolchain, recorded):
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, INPUT) + "\n"),
            Rule(("logcat",), stdout=recorded.data(f"{INPUT}.bin")),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m")

    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("ANR ")]
    assert "Reason: Input dispatching timed out" in line


def test_watch_on_a_serial_that_is_not_attached_refuses_before_streaming(toolchain, recorded):
    """`adb -s <unknown> logcat` does not fail: it waits for the device.

    Measured on this host: "- waiting for device -" on stderr, and no exit. So
    --watch on a mistyped serial read as a device with no ANRs for as long as
    it ran. The serial is resolved first, and an unknown one is exit 3.
    """
    toolchain.install("adb", [Rule(("devices",), stdout=recorded.text("adb_devices_single"))])

    result = toolchain.run(
        "anr_watcher.py", "--watch", "--duration", "5", "--serial", "emulator-9999", "--json"
    )

    assert_refusal(result, json_mode=True, naming=("emulator-9999",), status=3, streaming=True)
    assert not any("logcat" in call for call in toolchain.calls("adb"))


@pytest.mark.parametrize(
    "argv",
    [("--watch", "--duration", "5"), ("--since", "1m")],
    ids=["watch", "since"],
)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_logcat_failing_for_a_reason_adb_does_not_type_is_still_a_failure(
    argv, json_mode, toolchain, recorded
):
    """X6's other half: a non-zero exit that is not a device error is exit 1.

    The stub's logcat says nothing and exits 1 -- no words are invented for it.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), exit=1),
        ],
    )

    result = toolchain.run("anr_watcher.py", *argv, *(["--json"] if json_mode else []))

    assert_refusal(
        result, json_mode=json_mode, naming=("logcat",), status=1, streaming=argv[0] == "--watch"
    )


# ---------------------------------------------------------------------------
# The worker writes an ANR once the stream goes quiet
# ---------------------------------------------------------------------------


def test_worker_writes_an_anr_record_when_it_arrives(recorded, tmp_path, monkeypatch):
    """An ANR report is ONE record, so it is an event the moment it is read.

    In text it was many lines, complete only when something else followed --
    the worker waited for the stream to go quiet. Read as a record it needs no
    wait: written before any stop is asked for, by the reader's second quiet
    select(), and not written a second time by the stop.
    """
    burst_file = tmp_path / "burst.bin"
    burst_file.write_bytes(framed([_anr_record(recorded)]))
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=1)
    counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
    stop_flag = {"value": False}

    real_select = select.select
    quiet_passes = []
    second_quiet = threading.Event()

    def select_counting_quiet(rlist, wlist, xlist, timeout=None):
        ready = real_select(rlist, wlist, xlist, timeout)
        if not ready[0]:
            quiet_passes.append(timeout)
            if len(quiet_passes) == 2:
                second_quiet.set()
        return ready

    monkeypatch.setattr(anr_watcher.select, "select", select_counting_quiet)
    proc = subprocess.Popen(
        [sys.executable, "-c", _QUIET_STREAM, str(burst_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        bufsize=0,
    )
    try:
        if proc.stderr.readline().strip() != b"written":
            pytest.fail("the stand-in stream did not write its burst")
        with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
            reader = threading.Thread(
                target=anr_watcher.AnrBuster(store=store)._read_stream_into_events,
                kwargs={
                    "proc": proc,
                    "out_handle": out_handle,
                    "stop_flag": stop_flag,
                    "counters": counters,
                    "package": None,
                    "min_frames": 30,
                    "session_start_ms": 0,
                    "process_names": _oracle(recorded, BROADCAST),
                },
                daemon=True,
            )
            reader.start()
            if not second_quiet.wait(timeout=30):
                pytest.fail("the reader never went quiet twice")
            written_while_running = store.read_events(meta.session_id)
            stop_flag["value"] = True
            reader.join(timeout=30)
    finally:
        stop_flag["value"] = True
        _reap(proc)

    assert [event.kind for event in written_while_running] == ["anr"]
    assert len(store.read_events(meta.session_id)) == 1, "the stop wrote the ANR a second time"


# ---------------------------------------------------------------------------
# N14's shape elsewhere in this file: a 0 that means "off"
# ---------------------------------------------------------------------------


def test_env_int_says_when_it_raises_a_value(monkeypatch, capsys):
    """The clamp was silent, which is how N14 hid: now it says so."""
    monkeypatch.setenv("ANDROID_EMU_TEST_FLOOR", "0")

    assert env_config.env_int("ANDROID_EMU_TEST_FLOOR", 3) == 1
    assert "ANDROID_EMU_TEST_FLOOR=0 is below the minimum 1" in capsys.readouterr().err
    assert env_config.env_int("ANDROID_EMU_TEST_FLOOR", 3, min_value=0) == 0
    assert capsys.readouterr().err == ""


def test_default_top_n_zero_means_every_cluster(recorded, tmp_path, monkeypatch):
    """0 is "no cap" for --all; read with the default floor it was a cap of 1."""
    monkeypatch.setenv("ANDROID_EMU_ANR_DEFAULT_TOP_N", "0")
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    with open(store.events_path(meta.session_id), "w", encoding="utf-8") as handle:
        for pid in (1001, 1002, 1003, 1004):
            line = re.sub(
                r"^(\S+ \S+\s+)\d+(\s+)\d+",
                rf"\g<1>{pid}\g<2>{pid}",
                _recorded_jank_burst(recorded, (80,))[0],
            )
            raw = parse_logcat_anr(line)
            assert raw is not None
            event = build_normalised_event(raw, session_start_ms=0, current_ms=1000)
            handle.write(event_to_jsonl(event) + "\n")

    payload = json.loads(anr_watcher.AnrBuster(store=store).stop(meta.session_id, json_mode=True))

    assert len(payload["clusters"]) == 4


def test_budget_tokens_zero_means_no_budget(recorded, tmp_path, monkeypatch):
    """0 is the default, "no budget"; floored to 1 it forced the one-line L0."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    (raw,) = [parse_logcat_anr(line) for line in _recorded_jank_burst(recorded, (80,))]
    with open(store.events_path(meta.session_id), "w", encoding="utf-8") as handle:
        event = build_normalised_event(raw, session_start_ms=0, current_ms=1000)
        handle.write(event_to_jsonl(event) + "\n")
    monkeypatch.setenv("ANDROID_EMU_ANR_BUDGET_TOKENS", "0")

    out = anr_watcher.AnrBuster(store=store).stop(meta.session_id)

    assert "Drill:" in out and "\n" in out, f"a zero budget gave the one-line L0: {out!r}"


def test_total_cap_zero_prunes_nothing(tmp_path, monkeypatch):
    """0 disables the aggregate cap; floored to 1 it pruned to 1 MB."""
    monkeypatch.setenv("ANDROID_EMU_ANR_TOTAL_CAP_MB", "0")
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    pruned_to: list[int] = []
    monkeypatch.setattr(store, "prune_to_aggregate_cap", pruned_to.append)
    monkeypatch.setattr(store, "prune_expired", lambda *_a, **_k: 0)

    class _StoppedBeforeSpawnError(Exception):
        pass

    def _stop_before_spawning(_serial):
        raise _StoppedBeforeSpawnError

    buster = anr_watcher.AnrBuster(store=store)
    monkeypatch.setattr(buster, "_resolve_serial", _stop_before_spawning)
    with pytest.raises(_StoppedBeforeSpawnError):
        buster.start({}, serial=None)

    assert pruned_to == [], f"a cap of 0 pruned to {pruned_to}"


# ---------------------------------------------------------------------------
# Review of PR #26: the worker names the process behind a jank line
# ---------------------------------------------------------------------------


class _FileLogcat:
    """A logcat whose stdout is a file of recorded records: readable, then EOF.

    A file rather than a pipe so a recording larger than a pipe's buffer can be
    handed over whole before the reader starts.
    """

    def __init__(self, path):
        self.stdout = open(path, "rb", buffering=0)  # noqa: SIM115 -- closed by _run_worker_over

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


def _run_worker_over(store, session_id, records: list[Frame], tmp_path, **kwargs) -> dict:
    stream = tmp_path / f"{session_id}.logcat"
    stream.write_bytes(framed(records))
    counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
    proc = _FileLogcat(stream)
    try:
        with open(store.events_path(session_id), "a", buffering=1) as out_handle:
            anr_watcher.AnrBuster(store=store)._read_stream_into_events(
                proc=proc,
                out_handle=out_handle,
                stop_flag={"value": False},
                counters=counters,
                min_frames=30,
                session_start_ms=0,
                **{"package": None, **kwargs},
            )
    finally:
        proc.stdout.close()
    return counters


@pytest.fixture
def no_device(monkeypatch):
    """Fail the test if the worker asks the device anything."""
    asked: list[tuple] = []

    def _refuse(*args, **kwargs):
        asked.append(args)
        raise AssertionError(f"the worker asked the device: {args}")

    monkeypatch.setattr(anr_watcher, "run_adb", _refuse)
    for lookup in ("get_system_server_pid", "get_package_uids"):
        monkeypatch.setattr(anr_watcher, lookup, _refuse, raising=False)
    return asked


@pytest.fixture
def unreachable_lookups(monkeypatch, recorded_anywhere):
    """Every question the worker could put to the device fails, as adb fails it.

    For tests about something else, so that whatever the worker would ask --
    system_server's pid, a package's uid -- it is told the device is not there
    (the recorded refusal) and goes on with the jank keyed by pid.
    """
    from common.adb_exec import DeviceNotFoundError

    def _gone(*_args, **_kwargs):
        raise DeviceNotFoundError(recorded_anywhere("adb_shell_device_not_found").strip())

    monkeypatch.setattr(anr_watcher, "run_adb", _gone)
    for lookup in ("get_system_server_pid", "get_package_uids"):
        monkeypatch.setattr(anr_watcher, lookup, _gone, raising=False)


def _restart_oracles(recorded) -> tuple[str, str]:
    """``(system_server's pid, the package's uid listing)``, measured with the restart recording.

    `pidof system_server` and `cmd package list packages -U` in the same run
    as logcat_app_restart_records.
    """
    return (
        _system_server(recorded, RESTART),
        _entry(recorded, RESTART)["package_uid_listing"]["output"],
    )


def _names(recorded) -> anr_watcher._ProcessNames:
    """The worker's resolver, answered by the device's recorded words instead of the device."""
    from common.device_utils import parse_package_uids

    system_server, listing = _restart_oracles(recorded)
    return anr_watcher._ProcessNames(
        system_server_pid=lambda: system_server,
        package_uids=lambda package: parse_package_uids(listing, package),
    )


def _first_launch(recorded) -> tuple[Frame, Frame]:
    """The first process's recorded `Start proc` record and its recorded jank record."""
    pid = _entry(recorded, RESTART)["pids"][0]
    records = _frames(recorded, RESTART)
    start = next(f for f in records if f.message.startswith(f"Start proc {pid}:"))
    jank = next(f for f in records if f.tag == "Choreographer" and f.pid == pid)
    return start, jank


def _jank_sites(store, session_id) -> list[tuple[str | None, str | None]]:
    return [(e.app, e.identity) for e in store.read_events(session_id) if e.kind == "jank"]


def test_worker_names_a_restarted_apps_jank_from_the_stream(recorded, tmp_path, no_device):
    """logcat_app_restart_records: one app, three processes, each janking once.

    system_server's ``Start proc <pid>:<name>/<uid>`` records in the same
    stream name every one of them, so the session holds ONE jank site.
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(
        store,
        meta.session_id,
        _frames(recorded, RESTART),
        tmp_path,
        process_names=_names(recorded),
    )

    jank = [
        c for c in store.build_summary(meta.session_id).clusters if c.sample_event.kind == "jank"
    ]
    assert [(c.count, c.identity) for c in jank] == [(3, "package")], jank
    assert no_device == []


def test_package_keeps_the_packages_own_jank_in_a_session(recorded, tmp_path, no_device):
    """``--package`` used to drop every jank record: none names a package."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"package": PACKAGE})

    _run_worker_over(
        store,
        meta.session_id,
        _frames(recorded, RESTART),
        tmp_path,
        package=PACKAGE,
        process_names=_names(recorded),
    )

    assert [e.kind for e in store.read_events(meta.session_id)].count("jank") == 3


def _forged_start(recorded, forged: str) -> tuple[Frame, Frame]:
    """The first launch's `Start proc` record, one field forged, and its jank record.

    - written by the app: the record's pid -- logd's, the writer's -- is the
      janking app's own, as when an app logs under ActivityManager's tag (the
      reviewer's reproduction; logd sets it, so a test has to substitute it);
    - under another uid: system_server's own record with the uid u0a205 ->
      u0a999, as system_server really logs it for an app that names its
      process after another package (``android:process``).
    """
    start, jank = _first_launch(recorded)
    forgeries = {
        "as recorded": start,
        "written by the app": start.replace(pid=jank.pid),
        "under another uid": start.replace(message=start.message.replace("/u0a205 ", "/u0a999 ")),
    }
    line = forgeries[forged]
    if forged != "as recorded" and line == start:
        pytest.fail("the recorded Start proc record no longer has the field this derives")
    return line, jank


@pytest.mark.parametrize(
    ("forged", "named"),
    [("as recorded", True), ("written by the app", False), ("under another uid", False)],
)
def test_only_system_servers_start_proc_for_the_packages_uid_names_a_pid(
    forged, named, recorded, tmp_path, no_device
):
    """B1: a ``Start proc`` from ANY writer named a pid (see ``_forged_start``)."""
    line, jank = _forged_start(recorded, forged)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(store, meta.session_id, [line, jank], tmp_path, process_names=_names(recorded))

    assert _jank_sites(store, meta.session_id) == (
        [(PACKAGE, "package")] if named else [(None, "pid")]
    )


def _injection(recorded) -> tuple[Frame, Frame]:
    """The recorded tag-injection pair: the forging record and the victim's.

    logcat_tag_injection_records: ONE record from the shell (pid and uid the
    shell's), its TAG ``x:``, a newline and a threadtime header claiming
    system_server's pid and the tag ActivityManager, its message system_server's
    ``Start proc <victim>:<package>/<package uid>``; then the victim's own
    record. In text that tag printed a whole line of system_server's.
    """
    injection = _entry(recorded, INJECTION)["injection"]
    records = _frames(recorded, INJECTION)
    forging = next(f for f in records if f.pid == injection["forger_pid"])
    victim = next(f for f in records if f.pid == injection["victim_pid"])
    if "\n" not in forging.tag or "ActivityManager" not in forging.tag:
        pytest.fail("the recorded forging record no longer carries its header in its tag")
    return forging, victim


def test_a_system_server_header_forged_in_a_tag_names_nothing(recorded, tmp_path, no_device):
    """The tag-injection regression for naming (A1, adopted from W3-L).

    The recorded forging record claims, in its tag, system_server's
    ``Start proc`` for the victim pid; the victim's jank follows -- derived:
    the recorded jank record with only its pid set to the victim's. Read as
    records, the forging record's writer is the shell and its tag is not
    ActivityManager: the victim's jank stays keyed by pid.
    """
    forging, victim = _injection(recorded)
    jank = _jank_records(recorded, (80,), pid=victim.pid)[0]
    system_server = _system_server(recorded, INJECTION)
    listing = _entry(recorded, INJECTION)["package_uid_listing"]["output"]
    from common.device_utils import parse_package_uids

    names = anr_watcher._ProcessNames(
        system_server_pid=lambda: system_server,
        package_uids=lambda package: parse_package_uids(listing, package),
    )
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(store, meta.session_id, [forging, victim, jank], tmp_path, process_names=names)

    assert _jank_sites(store, meta.session_id) == [(None, "pid")]


def test_a_name_ends_when_system_server_says_the_process_did(recorded, tmp_path, no_device):
    """A pid outlives its process; the name does not.

    The crashed process's recorded jank record, then system_server's recorded
    `has died` for it, then the same jank record with only its time moved past
    the death: a pid whose process is gone, and whose next holder no `Start
    proc` has named, is keyed by pid.
    """
    start, jank = _first_launch(recorded)
    died = next(
        f
        for f in _frames(recorded, RESTART)
        if f.message.startswith("Process ") and f"(pid {jank.pid}) has died" in f.message
    )
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(
        store,
        meta.session_id,
        [start, jank, died, _moved(jank, 60)],
        tmp_path,
        process_names=_names(recorded),
    )

    assert _jank_sites(store, meta.session_id) == [(PACKAGE, "package"), (None, "pid")]


def test_with_system_servers_pid_unknown_nothing_is_named(recorded, tmp_path):
    """Fail closed: no system_server pid, no `Start proc` counts -- and it is asked once."""
    from common.device_utils import parse_package_uids

    _, listing = _restart_oracles(recorded)
    asked: list[str] = []
    names = anr_watcher._ProcessNames(
        system_server_pid=lambda: asked.append("system_server") or None,
        package_uids=lambda package: parse_package_uids(listing, package),
    )
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    counters = _run_worker_over(
        store, meta.session_id, _frames(recorded, RESTART), tmp_path, process_names=names
    )

    assert set(_jank_sites(store, meta.session_id)) == {(None, "pid")}
    assert asked == ["system_server"]
    assert len(counters["unnamed_pids"]) == 3


def test_system_server_is_asked_only_when_a_process_sentence_arrives(recorded, tmp_path):
    """Lazy: a stream with no ActivityManager sentence costs no device call."""
    asked: list[str] = []
    names = anr_watcher._ProcessNames(
        system_server_pid=lambda: asked.append("system_server") or None,
        package_uids=lambda package: asked.append(package) or None,
    )
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(
        store, meta.session_id, _jank_records(recorded, (40, 80)), tmp_path, process_names=names
    )

    assert asked == []
    assert _jank_sites(store, meta.session_id) == [(None, "pid"), (None, "pid")]


def test_worker_keeps_reading_when_the_device_cannot_say_who_system_server_is(
    recorded, tmp_path, unreachable_lookups
):
    """A lookup that fails leaves the jank keyed by pid; it does not end the session."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    counters = _run_worker_over(
        store,
        meta.session_id,
        _frames(recorded, RESTART),
        tmp_path,
        process_names=anr_watcher._ProcessNames(
            *anr_watcher._device_lookups("emulator-5554", may_ask=lambda: True)
        ),
    )

    assert set(_jank_sites(store, meta.session_id)) == {(None, "pid")}
    assert len(counters["unnamed_pids"]) == 3


def test_no_lookup_starts_once_a_stop_is_requested(no_device):
    """The worker's lookups ask ``may_ask`` before every device call."""
    system_server, package_uids = anr_watcher._device_lookups(
        "emulator-5554", may_ask=lambda: False
    )

    assert system_server() is None
    assert package_uids(PACKAGE) is None
    assert no_device == []


def test_the_device_lookups_are_bounded(monkeypatch):
    """Both questions carry the worker's short bound, which --stop's wait covers."""
    asked: list[tuple] = []

    def _answer(*args, **kwargs):
        asked.append((args, kwargs))

    monkeypatch.setattr(anr_watcher, "get_system_server_pid", _answer)
    monkeypatch.setattr(anr_watcher, "get_package_uids", _answer)
    system_server, package_uids = anr_watcher._device_lookups("emulator-5554", may_ask=lambda: True)

    system_server()
    package_uids(PACKAGE)

    bound = {"timeout": anr_watcher.LOOKUP_TIMEOUT_SECONDS}
    assert asked == [(("emulator-5554",), bound), (("emulator-5554", PACKAGE), bound)]
    assert anr_watcher.WORKER_EXIT_TIMEOUT_SECONDS > anr_watcher.LOOKUP_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# A device that answers live: what --start and --watch see end to end
# ---------------------------------------------------------------------------

# An adb that answers each question from recorded text or records, as
# tests/fake_toolchain does, plus three things that tool's stubs do not: a
# logcat that stays open after replaying (a live stream gone quiet -- the
# worker ends it), an answer that takes its time, and a marker file written
# the moment a question is asked.
_LIVE_DEVICE = """#!{python}
import json, sys, time
with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
for answer in spec["answers"]:
    if not all(token in argv for token in answer["match"]):
        continue
    if answer["asked"]:
        with open(answer["asked"], "a", encoding="utf-8") as marker:
            marker.write(argv[-1] + "\\n")
    time.sleep(answer["delay"])
    for name, stream in (("stdout", sys.stdout.buffer), ("stderr", sys.stderr.buffer)):
        if answer[name]:
            with open(answer[name], "rb") as handle:
                stream.write(handle.read())
            stream.flush()
    if answer["hold"]:
        time.sleep(60)
    sys.exit(answer["exit"])
sys.exit(1)
"""


@dataclasses.dataclass(frozen=True)
class _Answer:
    """One question the live device answers: every token of ``match`` in argv."""

    match: tuple[str, ...]
    stdout: str | bytes = ""
    stderr: str = ""
    exit: int = 0
    delay: float = 0.0
    hold: bool = False
    asked: str = ""


class _LiveDevice:
    """A FakeToolchain whose adb is ``_LIVE_DEVICE``, answering with recorded output."""

    def __init__(self, root, answers: list[_Answer]) -> None:
        self.toolchain = FakeToolchain(root / "toolchain")
        self.toolchain.assert_isolated()
        self.store = SessionStore(base_dir=self.toolchain.home / "anr-sessions")
        self._log = root / "live-device.log"
        self._log.touch()
        staged = []
        for index, answer in enumerate(answers):
            entry = dataclasses.asdict(answer)
            for stream in ("stdout", "stderr"):
                if entry[stream]:
                    path = root / f"answer-{index}.{stream}"
                    if isinstance(entry[stream], bytes):
                        path.write_bytes(entry[stream])
                    else:
                        path.write_text(entry[stream], encoding="utf-8")
                    entry[stream] = str(path)
            staged.append(entry)
        spec = root / "live-device.json"
        spec.write_text(json.dumps({"log": str(self._log), "answers": staged}), encoding="utf-8")
        adb = self.toolchain.bin / "adb"
        adb.write_text(_LIVE_DEVICE.format(python=sys.executable, spec=str(spec)))
        adb.chmod(0o755)

    def run(self, *argv: str, timeout: float = 90) -> subprocess.CompletedProcess:
        return self.toolchain.run("anr_watcher.py", *argv, timeout=timeout)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self._log.read_text(encoding="utf-8").splitlines()]

    def kill_workers(self) -> None:
        if not self.store.base_dir.exists():
            return
        for meta in self.store.list_sessions():
            if meta.pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(meta.pid, signal.SIGKILL)


def _device_answers(recorded, logcat_records: list[Frame], **lookups: _Answer) -> list[_Answer]:
    """The recorded answers of an attached emulator, and a logcat replaying those records."""
    system_server, listing = _restart_oracles(recorded)
    return [
        _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
        _Answer(("get-state",), stdout=recorded.text("adb_get_state")),
        _Answer(("logcat",), stdout=framed(logcat_records), hold=True),
        lookups.get(
            "system_server", _Answer(("pidof", "system_server"), stdout=system_server + "\n")
        ),
        lookups.get("uids", _Answer(("cmd", "package"), stdout=listing)),
    ]


def _session_summary(device: _LiveDevice, *, events: int) -> dict:
    """--start, wait until the worker has written ``events`` events, then --stop --json."""
    started = device.run("--start", "--serial", "emulator-5554", "--json")
    assert started.returncode == 0, started.stdout + started.stderr
    session_id = json.loads(started.stdout)["session_id"]
    try:
        deadline = time.monotonic() + 30
        while len(device.store.read_events(session_id)) < events:
            if time.monotonic() > deadline:
                pytest.fail(f"the worker wrote {device.store.read_events(session_id)}")
            time.sleep(0.05)
        stopped = device.run("--stop", session_id, "--json")
    finally:
        device.kill_workers()
    assert stopped.returncode == 0, stopped.stderr
    return json.loads(stopped.stdout)


def _jank_clusters(summary: dict) -> list[tuple[str | None, str | None]]:
    return [
        (c["sample_event"]["app"], c["identity"])
        for c in summary["clusters"]
        if c["sample_event"]["kind"] == "jank"
    ]


@pytest.mark.parametrize(
    ("forged", "identity"),
    [("as recorded", "package"), ("written by the app", "pid"), ("under another uid", "pid")],
)
def test_a_session_names_jank_only_from_system_servers_start_proc(
    forged, identity, recorded, tmp_path
):
    """B1 end to end: --start, the stream, --stop --json -- what an agent reads.

    The reviewer's reproduction, derived from recorded records (``_forged_start``):
    the app writes a `Start proc` record for its own pid under
    ActivityManager's tag, then janks. It came out as the named package's
    jank, ``identity: "package"``. system_server's pid and the package's uid
    are the device's recorded answers.
    """
    line, jank = _forged_start(recorded, forged)
    device = _LiveDevice(tmp_path, _device_answers(recorded, [_history(recorded), line, jank]))

    summary = _session_summary(device, events=1)

    sites = _jank_clusters(summary)
    assert sites == [(PACKAGE if identity == "package" else None, identity)], sites


def test_a_session_does_not_name_a_pid_from_a_header_forged_in_a_tag(recorded, tmp_path):
    """The tag-injection regression for naming, end to end (A1, adopted from W3-L).

    adb answers ``logcat -B`` with records -- the recorded forging record and
    the victim's own record, then the victim's jank (derived: the recorded
    jank record with only its pid set to the victim's and its time a second
    after the victim's record) -- and any other logcat with the device's own
    text for them: the recorded text twin, logcat_tag_injection, and the jank
    record as logcat prints it. Read as text, the forged line is
    system_server's ``Start proc`` for the victim, and the victim's jank came
    out as the package's; read as records the forging record is the shell's,
    and the jank stays keyed by pid. system_server's pid and the package's uid
    are the answers recorded in that run.
    """
    forging, victim = _injection(recorded)
    jank = _jank_records(recorded, (80,), pid=victim.pid)[0]
    jank = _moved(jank, _seconds(victim) - _seconds(jank) + 1)
    zone = ZoneInfo(recorded_zone(recorded, INJECTION))
    printed = "".join(f"{line}\n" for line in logcat.parse_records(jank.raw)[0].lines(zone))
    listing = _entry(recorded, INJECTION)["package_uid_listing"]["output"]
    device = _LiveDevice(
        tmp_path,
        [
            _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
            _Answer(("get-state",), stdout=recorded.text("adb_get_state")),
            _Answer(
                ("logcat", "-B"),
                stdout=framed([_history(recorded), forging, victim, jank]),
                hold=True,
            ),
            _Answer(("logcat",), stdout=recorded.text("logcat_tag_injection") + printed, hold=True),
            _Answer(("pidof", "system_server"), stdout=_system_server(recorded, INJECTION) + "\n"),
            _Answer(("cmd", "package"), stdout=listing),
        ],
    )

    summary = _session_summary(device, events=1)

    victims = [
        (c["sample_event"]["app"], c["identity"])
        for c in summary["clusters"]
        if c["sample_event"]["kind"] == "jank" and c["sample_event"]["pid"] == int(victim.pid)
    ]
    assert victims == [(None, "pid")], summary["clusters"]


def test_a_session_never_asks_the_device_who_holds_a_pid_now(recorded, tmp_path):
    """B2: the `ps ... ELAPSED` lookup is gone; a pid is named by the stream or not at all.

    It asked the device for a pid's current owner and its age, and trusted
    the name when the age covered the time since the line was READ -- but a
    line is read when its chunk arrives, so a line buffered through a stall
    looked recent, and a recycled pid's new owner was accepted for it. A jank
    record with no `Start proc` before it in the stream stays keyed by pid.
    """
    jank = _jank_records(recorded, (80,))[0]
    device = _LiveDevice(tmp_path, _device_answers(recorded, [_history(recorded), jank]))

    summary = _session_summary(device, events=1)

    assert _jank_clusters(summary) == [(None, "pid")], _jank_clusters(summary)
    asked_ps = [" ".join(call) for call in device.calls() if "ps" in call]
    assert asked_ps == [], asked_ps


def test_a_session_lists_the_pids_it_could_not_name(recorded, tmp_path):
    """Review of PR #34: a warm app's jank was keyed by pid -- and only metadata said so.

    Only a process whose verified `Start proc` the session saw is named; one
    already running when it began is not, and `--package` drops its jank. The
    summary now lists those pids, in JSON and in one sentence of text. The
    recorded jank record, its process started before the stream.
    """
    jank = _jank_records(recorded, (80,))[0]
    pid = int(jank.pid)
    device = _LiveDevice(tmp_path, _device_answers(recorded, [_history(recorded), jank]))

    started = device.run("--start", "--serial", "emulator-5554", "--json")
    session_id = json.loads(started.stdout)["session_id"]
    try:
        deadline = time.monotonic() + 30
        while not device.store.read_events(session_id):
            if time.monotonic() > deadline:
                pytest.fail("the worker wrote nothing")
            time.sleep(0.05)
        stopped = device.run("--stop", session_id, "--json")
        details = device.run("--get-details", session_id)
    finally:
        device.kill_workers()

    assert json.loads(stopped.stdout)["unnamed_pids"] == {"count": 1, "pids": [pid]}
    (sentence,) = [line for line in details.stdout.splitlines() if line.startswith("Unnamed:")]
    assert f"1 pid ({pid})" in sentence and "--package drops them" in sentence, sentence


def test_a_session_that_cannot_authenticate_an_anr_says_so(recorded, tmp_path):
    """Review of PR #34: fail closed, never a silent "no ANRs".

    The device will not say who system_server is (`pidof` answers nothing,
    exit 1), so the recorded broadcast ANR report cannot be authenticated: it
    is not counted, and the summary says why, in JSON and in text. The
    recorded jank record follows it, so the worker is seen to have read past it.
    """
    jank = _jank_records(recorded, (80,))[0]
    logcat_records = [_history(recorded), _anr_record(recorded), jank]
    device = _LiveDevice(
        tmp_path,
        _device_answers(
            recorded, logcat_records, system_server=_Answer(("pidof", "system_server"), exit=1)
        ),
    )

    started = device.run("--start", "--serial", "emulator-5554", "--json")
    session_id = json.loads(started.stdout)["session_id"]
    try:
        deadline = time.monotonic() + 30
        while not device.store.read_events(session_id):
            if time.monotonic() > deadline:
                pytest.fail("the worker wrote nothing")
            time.sleep(0.05)
        stopped = device.run("--stop", session_id, "--json")
        details = device.run("--get-details", session_id)
    finally:
        device.kill_workers()

    payload = json.loads(stopped.stdout)
    assert payload["verification"] == anr_watcher.VERIFICATION_UNKNOWN
    assert payload["unverified_anr_lines"] >= 1
    assert [c["sample_event"]["kind"] for c in payload["clusters"]] == ["jank"]
    assert "Unverified: system_server pid unknown" in details.stdout, details.stdout


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_since_says_when_it_could_not_authenticate_an_anr(json_mode, toolchain, recorded):
    """The legacy reads disclose it too: on stderr, and in the text summary."""
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), stdout=recorded.data(f"{BROADCAST}.bin")),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m", *(["--json"] if json_mode else []))

    assert result.returncode == 0, result.stderr
    assert "warning: Unverified: system_server pid unknown" in result.stderr
    if json_mode:
        assert result.stdout.strip() == ""
    else:
        assert "No verified ANR/jank events." in result.stdout, result.stdout


def test_since_does_not_count_an_anr_an_app_wrote(toolchain, recorded):
    """Review of PR #34, end to end: an app's own ``ANR in`` record is not an ANR.

    Derived: the recorded broadcast report with only its writer -- the
    record's pid -- changed to the stalled app's own pid; system_server's pid
    is the device's recorded answer.
    """
    anr = _anr_record(recorded)
    forged = anr.replace(pid=_stated_pid(anr))
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, BROADCAST) + "\n"),
            Rule(("logcat",), stdout=framed([forged])),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m", "--json")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


def _seconds(frame: Frame) -> float:
    """The record's time (its sec/nsec fields), in seconds."""
    sec, nsec = struct.unpack_from("<II", frame.raw, 12)
    return sec + nsec / 1e9


@pytest.mark.parametrize("claim", ["an ANR", "a duplicate verdict"])
def test_since_is_not_fooled_by_a_header_forged_in_a_tag(claim, toolchain, recorded):
    """The tag-injection regression for ANRs (A1, adopted from W3-L), end to end.

    The recorded forging record carries a header claiming system_server's pid
    and ActivityManager's tag in its TAG. Printed as text that is a line of
    system_server's own -- what --since read before PR #34 adopted the record
    reader -- and read as a record it is the shell's. Derived, in both forms:
    the forging record with only its message (the forger's own text) replaced
    by an ANR report for the fixture package, or by ActivityManager's recorded
    duplicate verdict -- which must not settle WindowManager's recorded report
    for that package, moved to a second before the forgery so the verdict
    falls in its window. adb answers ``-B`` with the records and anything else
    with the device's recorded text, so a text reader is seen to be fooled.
    """
    forging, _victim = _injection(recorded)
    broadcast = _frames(recorded, BROADCAST)
    if claim == "an ANR":
        message, before = f"ANR in {PACKAGE}", []
    else:
        verdict = next(f for f in broadcast if f.message.startswith("Skipping duplicate ANR"))
        report = next(f for f in broadcast if f.tag == "WindowManager")
        message = verdict.message
        before = [_moved(report, _seconds(forging) - _seconds(report) - 1)]
    text = recorded.text("logcat_tag_injection")
    if forging.message not in text:
        pytest.fail("the recorded text no longer carries the forging record's message")
    zone = ZoneInfo(recorded_zone(recorded, INJECTION))
    printed = "".join(
        f"{line}\n" for f in before for line in logcat.parse_records(f.raw)[0].lines(zone)
    )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, INJECTION) + "\n"),
            Rule(("logcat", "-B"), stdout=framed([*before, forging.replace(message=message)])),
            Rule(("logcat",), stdout=printed + text.replace(forging.message, message)),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m", "--json")

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    anrs = [(e["process"], e.get("anr_package")) for e in events if e["kind"] == "anr"]
    assert anrs == ([] if claim == "an ANR" else [("WindowManager", PACKAGE)]), events


def test_stop_waits_for_a_worker_still_asking_the_device(recorded, tmp_path):
    """Review of PR #26: the summary was saved while the worker was mid-lookup.

    ``--stop`` gave the worker 2 s; a lookup could take longer, the summary
    was saved with zero events, and the worker wrote its jank and the held
    ANR after it -- a silently empty summary. Here system_server's pid takes
    2.5 s to come back (slower than the old wait, inside the lookup bound) and
    --stop is asked the moment it is asked. After the stop the worker asks
    nothing more: the package's uid is never asked, so the jank stays keyed
    by pid -- but nothing the stream delivered is lost.
    """
    start, jank = _first_launch(recorded)
    other = jank.replace(pid=str(int(jank.pid) + 1))
    marker = tmp_path / "system-server-asked"
    uids_marker = tmp_path / "uids-asked"
    system_server, listing = _restart_oracles(recorded)
    logcat_records = [_history(recorded), start, jank, other, _anr_record(recorded)]
    device = _LiveDevice(
        tmp_path,
        _device_answers(
            recorded,
            logcat_records,
            system_server=_Answer(
                ("pidof", "system_server"),
                stdout=system_server + "\n",
                delay=2.5,
                asked=str(marker),
            ),
            uids=_Answer(("cmd", "package"), stdout=listing, asked=str(uids_marker)),
        ),
    )
    started = device.run("--start", "--serial", "emulator-5554")
    session_id = started.stdout.strip()
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            if time.monotonic() > deadline:
                pytest.fail("the worker never asked the device for system_server's pid")
            time.sleep(0.05)
        stopped = device.run("--stop", session_id, "--json")
        # The worker has exited: no later --stop may signal its pid.
        forgotten = device.store.load_meta(session_id).pid is None
    finally:
        device.kill_workers()

    assert stopped.returncode == 0, stopped.stderr
    assert forgotten, "a stopped worker's pid was kept"
    payload = json.loads(stopped.stdout)
    assert payload["event_count"] == 3, payload
    assert payload["incomplete"] is False
    assert sorted(c["sample_event"]["kind"] for c in payload["clusters"]) == [
        "anr",
        "jank",
        "jank",
    ]
    assert marker.read_text(encoding="utf-8").split() == ["system_server"]
    assert not uids_marker.exists(), "a lookup started after the stop"


def test_a_stream_restart_forgets_who_held_each_pid(
    recorded, tmp_path, monkeypatch, unreachable_lookups
):
    """Review of PR #26 and B2: a pid's owner was cached across a stream restart.

    The first stream names pid P from system_server's own ``Start proc``
    record. The stream dies; while the worker backs off, P can change hands,
    and the new owner's ``Start proc`` falls in the gap -- ``-T 1`` does not
    replay it. So the second stream's jank from P is keyed by pid: nothing in
    THAT stream named it, and nothing else may. The second stream opens with
    its ``-T 1`` replay, the last record the first one delivered.
    """
    from common.device_utils import parse_package_uids

    start, jank = _first_launch(recorded)
    system_server, listing = _restart_oracles(recorded)
    streams = [[_history(recorded), start, jank], [jank, _moved(jank, 30)]]
    for index, stream in enumerate(streams):
        (tmp_path / f"stream-{index}.logcat").write_bytes(framed(stream))
    spawned: list[_FileLogcat] = []

    def _next_stream(cmd, **_kwargs):
        stream = _FileLogcat(tmp_path / f"stream-{len(spawned)}.logcat")
        spawned.append(stream)
        return stream

    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "1")
    monkeypatch.setattr(anr_watcher, "RESTART_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    _logcat_through(monkeypatch, _next_stream)
    monkeypatch.setattr(anr_watcher, "get_system_server_pid", lambda *_a, **_k: system_server)
    monkeypatch.setattr(
        anr_watcher,
        "get_package_uids",
        lambda _serial, package, **_k: parse_package_uids(listing, package),
    )
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"serial": "emulator-5554"})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for stream in spawned:
            stream.stdout.close()

    assert len(spawned) == 2
    assert _jank_sites(store, meta.session_id) == [(PACKAGE, "package"), (None, "pid")]


def test_start_json_answers_an_object_whose_session_id_stop_accepts(recorded, tmp_path):
    """`--start --json` printed the bare session id: a token, not an object.

    Now ``{"success": true, "session_id": ...}``, like every other mode. The
    loop is the proof: the id is read out of what --start PRINTED and handed to
    --stop, which must accept it as the session it started. (The text form is
    still the bare id, for `SID=$(... --start)`; the test above reads it.)
    """
    toolchain = FakeToolchain(tmp_path / "toolchain")
    toolchain.assert_isolated()
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), stdout=recorded.data(f"{BROADCAST}.bin")),
        ],
    )

    started = toolchain.run("anr_watcher.py", "--start", "--serial", "emulator-5554", "--json")
    assert started.returncode == 0, started.stderr
    payload = json.loads(started.stdout)
    assert payload["success"] is True, payload
    session_id = payload["session_id"]
    store = SessionStore(base_dir=toolchain.home / "anr-sessions")
    try:
        stopped = toolchain.run("anr_watcher.py", "--stop", session_id, "--json")
    finally:
        worker = store.load_meta(session_id).pid
        if worker:
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker, signal.SIGKILL)

    assert stopped.returncode == 0, stopped.stderr
    assert json.loads(stopped.stdout)["session_id"] == session_id


# ---------------------------------------------------------------------------
# B4: no device is a refusal before any stream starts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [("--watch", "--duration", "1"), ("--start",), ("--since", "1m")],
    ids=["watch", "start", "since"],
)
def test_no_device_is_refused_before_any_logcat_read_starts(argv, recorded, tmp_path):
    """B4: `adb logcat` with no device does not fail -- it waits.

    Recorded (adb_waiting_for_device): "- waiting for device -", and
    no exit. With no --serial nothing was asked first, so `--watch --duration
    1` exited 0 with nothing on stdout -- a quiet device -- `--start` reported
    a session whose worker waited forever, and `--since` waited out its 60 s
    bound and called that a timeout. `adb get-state` answers the same question
    at once: here with the recorded refusal for a device that is not there.
    """
    device = _LiveDevice(
        tmp_path,
        [
            _Answer(("get-state",), stderr=recorded.text("adb_device_not_found"), exit=1),
            _Answer(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hold=True),
        ],
    )
    try:
        result = device.run(*argv, "--json")
    finally:
        device.kill_workers()

    assert_refusal(
        result, json_mode=True, naming=("not attached",), status=3, streaming=argv[0] == "--watch"
    )
    assert [call for call in device.calls() if "logcat" in call] == []
    assert not device.store.base_dir.exists() or device.store.list_sessions() == []


def test_a_device_that_is_up_is_asked_once_and_then_read(recorded, tmp_path):
    """The preflight's other half: `device` from get-state, and the stream is read."""
    jank = _jank_records(recorded, (80,))[0]
    device = _LiveDevice(tmp_path, _device_answers(recorded, [_history(recorded), jank]))

    result = device.run("--watch", "--duration", "1", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    assert [json.loads(line)["kind"] for line in result.stdout.splitlines()] == ["jank"]
    operations = [call[0] for call in device.calls()]
    assert operations.index("get-state") < operations.index("logcat")


# ---------------------------------------------------------------------------
# B5: a restarted stream does not read twice what the session already read
# ---------------------------------------------------------------------------


def _worker_over_streams(store, streams: list[list[Frame]], tmp_path, monkeypatch) -> str:
    """run_worker over ``streams``, one per spawn of logcat, each ending in EOF.

    ``ANDROID_EMU_ANR_MAX_RESTARTS`` is set so every stream is spawned once.
    Returns the session id.
    """
    for index, stream in enumerate(streams):
        (tmp_path / f"stream-{index}.logcat").write_bytes(framed(stream))
    spawned: list[_FileLogcat] = []

    def _next_stream(cmd, **_kwargs):
        stream = _FileLogcat(tmp_path / f"stream-{len(spawned)}.logcat")
        spawned.append(stream)
        return stream

    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", str(len(streams) - 1))
    monkeypatch.setattr(anr_watcher, "RESTART_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    _logcat_through(monkeypatch, _next_stream)
    meta = store.create({"serial": "emulator-5554"})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for stream in spawned:
            stream.stdout.close()
    assert len(spawned) == len(streams)
    return meta.session_id


@pytest.fixture
def system_server_is_known(monkeypatch, recorded):
    """The worker's device lookups answered as the device did with the restart recording."""
    from common.device_utils import parse_package_uids

    system_server, listing = _restart_oracles(recorded)
    monkeypatch.setattr(anr_watcher, "get_system_server_pid", lambda *_a, **_k: system_server)
    monkeypatch.setattr(
        anr_watcher,
        "get_package_uids",
        lambda _serial, package, **_k: parse_package_uids(listing, package),
    )
    monkeypatch.setattr(anr_watcher, "run_adb", _no_device_call)


def _no_device_call(*args, **_kwargs):
    raise AssertionError(f"the worker asked the device: {args}")


def test_an_anr_into_an_empty_buffer_is_one_event(
    recorded, tmp_path, monkeypatch, system_server_is_known
):
    """Review of PR #34: the first stream's first record was set aside as `-T 1`'s replay.

    After `logcat -c` there is nothing to replay, so the first record a
    stream delivers can be the session's first live one -- and the recorded
    ANR report, arriving into an empty buffer, came out as ZERO events.
    `-T 0` cannot start a read with nothing replayed (recorded:
    logcat_tail_zero), so the first stream reads everything it is given.
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(store, [[_anr_record(recorded)]], tmp_path, monkeypatch)

    events = store.read_events(session_id)
    assert [(e.kind, e.app) for e in events] == [("anr", PACKAGE)], events


def test_the_one_record_a_session_starts_on_is_read_and_disclosed(
    recorded, tmp_path, monkeypatch, unreachable_lookups
):
    """The price, accepted and disclosed (SKILL.md): `-T 1`'s one record may predate the session.

    The recorded jank as the newest record already buffered when the session
    began: it is read like the rest. One possibly-old record is the cost of
    never losing the first live one.
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, [_jank_records(recorded, (80,))], tmp_path, monkeypatch
    )

    assert [e.kind for e in store.read_events(session_id)] == ["jank"]


def test_a_restarted_stream_does_not_count_its_replay_again(
    recorded, tmp_path, monkeypatch, unreachable_lookups
):
    """B5: a restarted `-T 1` stream replays the last record the dead one delivered.

    The reviewer's reproduction: one jank, the stream dies, the restart's
    replay is that jank, and the session held two.
    """
    jank = _jank_records(recorded, (80,))[0]
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, [[_history(recorded), jank], [jank]], tmp_path, monkeypatch
    )

    assert [e.kind for e in store.read_events(session_id)] == ["jank"]


def test_a_record_logged_while_the_stream_was_down_is_kept(
    recorded, tmp_path, monkeypatch, unreachable_lookups
):
    """Only what was consumed is skipped: a replay the session never read is new.

    The restart's replay here is a jank logged during the gap -- the recorded
    record with only its time moved after the one consumed.
    """
    jank = _jank_records(recorded, (80,))[0]
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, [[_history(recorded), jank], [_moved(jank, 5)]], tmp_path, monkeypatch
    )

    assert [e.kind for e in store.read_events(session_id)] == ["jank", "jank"]


def test_no_clock_moves_the_line_between_consumed_and_new(
    recorded, tmp_path, monkeypatch, unreachable_lookups
):
    """Nothing is compared by time alone, so a clock stepped back cannot hide a record.

    A timestamp cursor read anything stamped before the last consumed record
    as consumed: after the device's clock stepped back -- or a year turned
    over in a year-less header -- a restart's replay, logged while the stream
    was down, was set aside as already read (review of PR #34). Derived: the
    recorded jank record, consumed, then a record the session never read with
    only its time moved an hour EARLIER, replayed by the restart.
    """
    jank = _jank_records(recorded, (80,))[0]
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, [[_history(recorded), jank], [_moved(jank, -3600)]], tmp_path, monkeypatch
    )

    assert [e.kind for e in store.read_events(session_id)] == ["jank", "jank"]


def test_a_replayed_record_is_skipped_whole(
    recorded, tmp_path, monkeypatch, system_server_is_known
):
    """`-T 1` replays one RECORD, and an ANR report is one record of many lines.

    The first stream read the recorded report; the restart replays it. One
    ANR, and the replay is not counted as read.
    """
    record = _anr_record(recorded)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, [[_history(recorded), record], [record]], tmp_path, monkeypatch
    )

    assert [e.kind for e in store.read_events(session_id)] == ["anr"]
    assert store.load_meta(session_id).extras["line_counters"]["total"] == 2


def test_watch_prints_the_record_it_starts_on(recorded, monkeypatch):
    """--watch reads its one stream from its first record: into an empty buffer, a live one."""
    jank = _jank_records(recorded, (80,))[0]
    later = _moved(jank, 5)
    read_end, write_end = os.pipe()
    os.write(write_end, framed([jank, later]))
    os.close(write_end)

    class _EndedLogcat:
        def __init__(self, *_args, **_kwargs):
            self.stdout = os.fdopen(read_end, "rb", buffering=0)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _EndedLogcat)
    watcher = anr_watcher.AnrWatcher(serial="emulator-5554")

    watcher.watch(json_mode=True)

    assert [e["timestamp"] for e in watcher.events] == [
        _printed(jank)[:21],
        _printed(later)[:21],
    ]


# ---------------------------------------------------------------------------
# B6: an incomplete summary says so in every text format
# ---------------------------------------------------------------------------


def _incomplete_session(store, recorded, tmp_path, counts=()) -> str:
    meta = store.create({})
    if counts:
        _run_worker_over(store, meta.session_id, _jank_records(recorded, counts), tmp_path)
    store.stop(meta.session_id, replace(store.build_summary(meta.session_id), incomplete=True))
    return meta.session_id


def test_get_details_says_an_empty_summary_came_from_an_incomplete_capture(
    store, recorded, tmp_path, monkeypatch, capsys
):
    """B6: it printed "no ANR/jank ≥ threshold" and never the flag.

    The reviewer's reproduction: an empty summary saved with
    ``incomplete: true``, read back with ``--get-details ID`` in text.
    """
    session_id = _incomplete_session(store, recorded, tmp_path)
    monkeypatch.setattr(anr_watcher.sys, "argv", ["anr_watcher.py", "--get-details", session_id])

    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "incomplete" in out.lower(), out
    assert "in the part captured" in out, out


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_the_cluster_drill_of_an_incomplete_session_says_so(json_mode, store, recorded, tmp_path):
    session_id = _incomplete_session(store, recorded, tmp_path, counts=(80,))

    out = anr_watcher.AnrBuster(store=store).get_details(session_id, cluster=1, json_mode=json_mode)

    if json_mode:
        assert json.loads(out)["incomplete"] is True
    else:
        assert "incomplete" in out.lower(), out


# ---------------------------------------------------------------------------
# B7: adb's stderr is classified as adb wrote it
# ---------------------------------------------------------------------------


def test_a_logcat_failure_is_typed_whatever_adb_wrote_before_it(
    toolchain, recorded, recorded_anywhere
):
    """B7: the stream's stderr was flattened onto one line BEFORE it was classified.

    adb's own error lines are recognised only at the start of a line (a
    device command's words must not forge them), so anything adb wrote
    first -- its daemon starting, "- waiting for device -" -- pushed the
    error off the start of the one line left: exit 1 where run_adb, reading
    the same text, says the device is not there (exit 3). The stderr here is
    two recorded adb outputs in the order adb writes them: logcat's waiting
    line (adb_waiting_for_device), then the device-not-found refusal
    (adb_shell_device_not_found). A real run giving exactly this pair could
    not be recorded without driving an untargeted adb; what is pinned is that
    the stream's reader types a text as run_adb types it.
    """
    stderr = recorded.text("adb_waiting_for_device") + recorded_anywhere(
        "adb_shell_device_not_found"
    )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), stderr=stderr, exit=1),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--watch", "--duration", "5", "--json")

    assert_refusal(result, json_mode=True, naming=("not attached",), status=3, streaming=True)


def test_stop_says_so_when_the_worker_will_not_finish(store, monkeypatch, capsys):
    """If the worker outlives the wait, the summary is marked, never silently short.

    The stand-in worker ignores SIGTERM; it carries the session's worker token
    on its command line, as a worker --start launched does, and claims its pid.
    """
    meta = store.create({})
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n",
            anr_watcher.WORKER_TOKEN_FLAG,
            meta.worker_token,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert stubborn.stdout.readline().strip() == "ready"
        store.claim_worker(meta.session_id, pid=stubborn.pid)
        monkeypatch.setattr(anr_watcher, "WORKER_EXIT_TIMEOUT_SECONDS", 0.3)

        out = anr_watcher.AnrBuster(store=store).stop(meta.session_id, json_mode=True)
    finally:
        stubborn.kill()
        stubborn.wait(timeout=30)
        stubborn.stdout.close()

    assert json.loads(out)["incomplete"] is True
    assert store.load_summary(meta.session_id).incomplete is True
    assert "incomplete" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Review of PR #26: a session keeps the fingerprint version it was captured with
# ---------------------------------------------------------------------------


def _forget_version(store, session_id: str) -> None:
    """Make a session look like one recorded before versions were kept."""
    path = store.session_dir(session_id) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.pop("fingerprint_version", None)
    path.write_text(json.dumps(meta), encoding="utf-8")


def test_stopping_an_old_session_keeps_its_old_fingerprint_version(recorded, store, tmp_path):
    """``--stop OLD_ID`` rebuilt a v1 session's v1 fingerprints under a v2 stamp."""
    from common.anr_pipeline import FINGERPRINT_VERSION

    old = store.create({})
    _forget_version(store, old.session_id)
    new = store.create({})
    for meta in (old, new):
        _run_worker_over(store, meta.session_id, _jank_records(recorded, (80,)), tmp_path)

    buster = anr_watcher.AnrBuster(store=store)
    buster.stop(old.session_id)
    buster.stop(new.session_id)

    assert store.load_summary(old.session_id).fingerprint_version == 1
    assert store.load_summary(new.session_id).fingerprint_version == FINGERPRINT_VERSION


def test_diff_refuses_sessions_captured_under_different_versions(
    recorded, store, tmp_path, monkeypatch, capsys
):
    old = store.create({})
    _forget_version(store, old.session_id)
    new = store.create({})
    buster = anr_watcher.AnrBuster(store=store)
    for meta in (old, new):
        _run_worker_over(store, meta.session_id, _jank_records(recorded, (80,)), tmp_path)
        buster.stop(meta.session_id)
    monkeypatch.setattr(
        anr_watcher.sys,
        "argv",
        ["anr_watcher.py", "--diff", old.session_id, new.session_id, "--json"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "v1" in payload["error"] and "v2" in payload["error"], payload


# ---------------------------------------------------------------------------
# Review of PR #26: --watch writes an ANR once the stream goes quiet
# ---------------------------------------------------------------------------


def test_watch_prints_an_anr_record_while_the_stream_is_still_open(recorded, tmp_path, monkeypatch):
    """--watch blocked after a complete record and printed nothing.

    The stand-in stream writes a recorded record already in the buffer and the
    recorded broadcast ANR report, and holds its stdout open, as a live logcat
    does between bursts. The event must come out while the stream is open --
    not when it ends. Waited on with a bound, as the failure is "never".
    """
    burst = tmp_path / "burst.bin"
    burst.write_bytes(framed([_history(recorded), _anr_record(recorded)]))
    real_popen = subprocess.Popen
    streams: list[subprocess.Popen] = []

    def _stand_in(cmd, **kwargs):
        proc = real_popen(
            [sys.executable, "-c", _QUIET_STREAM, str(burst)],
            **{**kwargs, "stderr": subprocess.PIPE, "stdin": subprocess.PIPE},
        )
        streams.append(proc)
        return proc

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _stand_in)
    system_server = _system_server(recorded, BROADCAST)
    monkeypatch.setattr(anr_watcher, "get_system_server_pid", lambda *_a, **_k: system_server)
    watcher = anr_watcher.AnrWatcher(serial="emulator-5554")
    emitted = threading.Event()
    emit = watcher._emit

    def _emit_noting(event, package, json_mode):
        emit(event, package, json_mode)
        emitted.set()

    monkeypatch.setattr(watcher, "_emit", _emit_noting)
    reader = threading.Thread(target=watcher.watch, kwargs={"json_mode": True}, daemon=True)
    reader.start()
    try:
        printed_while_open = emitted.wait(timeout=10)
    finally:
        watcher.interrupted = True
        for proc in streams:
            _reap(proc)
        reader.join(timeout=30)

    assert printed_while_open, "no event while the stream was open"
    assert [e["kind"] for e in watcher.events] == ["anr"]


# ---------------------------------------------------------------------------
# Review of PR #26 (optional): the probes, kept
# ---------------------------------------------------------------------------


def test_fd_records_reads_a_record_that_arrives_in_pieces(recorded):
    """Pieces of one record, then a record and part of the next, then a cut end.

    The recorded ANR report -- one record of many lines -- written to a pipe a
    few bytes at a time: nothing is a record until all of it has arrived, and
    a stream that ends inside one is ``truncated`` (common.logcat's rule).
    """
    record = _anr_record(recorded)
    raw = record.raw
    read_end, write_end = os.pipe()
    with os.fdopen(read_end, "rb", buffering=0) as stream:
        reader = anr_watcher._FdRecords(stream)
        os.write(write_end, raw[:3])
        assert reader.read(timeout=0) == [], "three bytes are not even a record's sizes"
        os.write(write_end, raw[3:40])
        assert reader.read(timeout=0) == [], "a header is not a record"
        os.write(write_end, raw[40:] + raw[:10])
        (got,) = reader.read(timeout=0)
        assert got == logcat.parse_records(raw)[0]
        os.close(write_end)
        with pytest.raises(logcat.LogFramingError) as cut:
            reader.read(timeout=0)
        assert cut.value.truncated
        assert cut.value.offset == len(raw), "the cut record starts after the one read"


def test_fd_records_refuses_what_is_not_records(recorded):
    """A header size no logd record has: not records at all, refused (not truncated).

    Derived: the recorded report with only its header-size field set to 0.
    """
    raw = bytearray(_anr_record(recorded).raw)
    struct.pack_into("<H", raw, 2, 0)
    read_end, write_end = os.pipe()
    with os.fdopen(read_end, "rb", buffering=0) as stream:
        reader = anr_watcher._FdRecords(stream)
        os.write(write_end, bytes(raw))
        os.close(write_end)
        with pytest.raises(logcat.LogFramingError) as refused:
            reader.read(timeout=0)
    assert not refused.value.truncated


def test_worker_stopped_right_after_a_report_keeps_it(recorded, tmp_path, monkeypatch):
    """The stop arrives right after the report is read, before any quiet pass.

    The report is one record, so it is an event as it is read, with its PID
    and Reason; the stop path must not lose it.
    """
    burst = tmp_path / "burst.bin"
    burst.write_bytes(framed([_anr_record(recorded)]))
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    stop_flag = {"value": False}
    real_select = select.select

    def select_stopping_once_read(rlist, wlist, xlist, timeout=None):
        ready = real_select(rlist, wlist, xlist, timeout)
        if ready[0]:
            stop_flag["value"] = True
        return ready

    monkeypatch.setattr(anr_watcher.select, "select", select_stopping_once_read)
    proc = subprocess.Popen(
        [sys.executable, "-c", _QUIET_STREAM, str(burst)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        bufsize=0,
    )
    try:
        if proc.stderr.readline().strip() != b"written":
            pytest.fail("the stand-in stream did not write its burst")
        with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
            anr_watcher.AnrBuster(store=store)._read_stream_into_events(
                proc=proc,
                out_handle=out_handle,
                stop_flag=stop_flag,
                counters={"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0},
                package=None,
                min_frames=30,
                session_start_ms=0,
                process_names=_oracle(recorded, BROADCAST),
            )
    finally:
        _reap(proc)

    (event,) = store.read_events(meta.session_id)
    assert event.kind == "anr" and event.reason and event.app_pid


# ---------------------------------------------------------------------------
# A stream that is not logd's records to its end (common.logcat's framing rule)
# ---------------------------------------------------------------------------


class _OpenLogcat:
    """A logcat whose stdout is a pipe still open for writing: no end to read."""

    def __init__(self, delivered: bytes) -> None:
        read_end, self._write_end = os.pipe()
        os.write(self._write_end, delivered)
        self.stdout = os.fdopen(read_end, "rb", buffering=0)

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None

    def close(self) -> None:
        self.stdout.close()
        with contextlib.suppress(OSError):
            os.close(self._write_end)


@pytest.mark.parametrize("stream", ["still open", "ended"])
def test_a_record_cut_by_the_stop_is_counted_not_lost_silently(stream, recorded, tmp_path):
    """Truncation after OUR stop is tolerated -- and disclosed (``cut_records``), once.

    The stream has delivered one recorded jank record and the first 40 bytes
    of the recorded ANR report, and is still OPEN (Codex review of PR #34: the
    cut was seen only at the stream's end -- which a stop never reads, so a
    live stream's cut record was lost unsaid) or has ended there. The stop is
    already asked for, so the reader drains what was delivered. Either way
    the one cut record is counted once.
    """
    jank = _jank_records(recorded, (80,))[0]
    delivered = jank.raw + _anr_record(recorded).raw[:40]
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
    if stream == "still open":
        proc = _OpenLogcat(delivered)
    else:
        (tmp_path / "cut.logcat").write_bytes(delivered)
        proc = _FileLogcat(tmp_path / "cut.logcat")
        proc.close = proc.stdout.close
    try:
        with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
            anr_watcher.AnrBuster(store=store)._read_stream_into_events(
                proc=proc,
                out_handle=out_handle,
                stop_flag={"value": True},
                counters=counters,
                package=None,
                min_frames=30,
                session_start_ms=0,
            )
    finally:
        proc.close()

    assert counters["cut_records"] == 1
    assert "stream_failure" not in counters
    assert [e.kind for e in store.read_events(meta.session_id)] == ["jank"]
    # What the worker hands --stop on its way out: the summary says it, both ways.
    store.persist_worker_counters(meta.session_id, counters)
    buster = anr_watcher.AnrBuster(store=store)
    payload = json.loads(buster.stop(meta.session_id, json_mode=True))
    assert (payload["cut_records"], payload["stream_failure"]) == (1, None)
    details = buster.get_details(meta.session_id)
    assert "Cut: 1 record arriving when the session stopped is not in here." in details, details


@pytest.mark.parametrize(
    ("device", "status"), [("gone", 3), ("still there", 1)], ids=["gone", "still-there"]
)
def test_a_stream_cut_by_itself_stops_the_session_and_stop_refuses(
    device, status, recorded, recorded_anywhere, tmp_path, monkeypatch, capsys
):
    """A stream that ends inside a record BY ITSELF is not a session to trust.

    The worker does not restart it: it asks whether the device is there
    (`adb get-state`, the recorded answers), marks the session crashed, and
    --stop refuses after saving what was read -- exit 3 for a device that
    went, 1 otherwise -- never the summary as if nothing happened.
    """
    from common.adb_exec import AdbResult, DeviceNotFoundError

    jank = _jank_records(recorded, (80,))[0]
    stream_file = tmp_path / "cut.logcat"
    stream_file.write_bytes(jank.raw + _anr_record(recorded).raw[:40])
    spawned: list[_FileLogcat] = []

    def _stream(cmd, **_kwargs):
        spawned.append(_FileLogcat(stream_file))
        return spawned[-1]

    def _get_state(*_args, **_kwargs):
        if device == "gone":
            raise DeviceNotFoundError(recorded_anywhere("adb_device_not_found").strip())
        return AdbResult(0, recorded.text("adb_get_state"), "")

    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "3")
    monkeypatch.setattr(anr_watcher, "RESTART_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    _logcat_through(monkeypatch, _stream)
    monkeypatch.setattr(anr_watcher, "run_adb", _get_state)
    monkeypatch.setenv("ANDROID_EMU_ANR_HOME", str(tmp_path))
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"serial": "emulator-5554"})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for stream in spawned:
            stream.stdout.close()

    assert len(spawned) == 1, "a broken stream was restarted"
    assert store.load_meta(meta.session_id).status == "crashed"
    monkeypatch.setattr(
        anr_watcher.sys, "argv", ["anr_watcher.py", "--stop", meta.session_id, "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == status
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["success"] is False and "ended inside a record" in refusal["error"], refusal
    summary = store.load_summary(meta.session_id)
    assert summary is not None and summary.stream_failure, "the summary was not saved"


@pytest.mark.parametrize("adb_exit", [0, 1], ids=["adb-exit-0", "adb-exit-1"])
@pytest.mark.parametrize("mode", [("--watch", "--duration", "5"), ("--since", "1m")])
@pytest.mark.parametrize(
    ("device", "status"), [("gone", 3), ("still there", 1)], ids=["gone", "still-there"]
)
def test_a_read_cut_by_itself_asks_the_device_then_refuses(
    mode, device, status, adb_exit, toolchain, recorded, recorded_anywhere
):
    """--watch and --since: a read that ends inside a record by itself is incomplete.

    adb answers `get-state` once as the recorded `device` (the preflight),
    then -- for "gone" -- as the recorded not-found refusal. The read itself
    is one recorded jank record and the first bytes of the recorded report,
    after which logcat exits 0 or 1 (saying nothing, so no words are invented).
    Codex review of PR #34: with exit 1 the exit status was judged first --
    no device re-check, and the framing refusal lost. The truncation decides
    first now, and the refusal says how adb exited too.
    """
    get_state = [Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=device == "gone")]
    if device == "gone":
        get_state.append(
            Rule(("get-state",), stderr=recorded_anywhere("adb_device_not_found"), exit=1)
        )
    jank = _jank_records(recorded, (80,))[0]
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *get_state,
            Rule(("logcat",), stdout=jank.raw + _anr_record(recorded).raw[:40], exit=adb_exit),
        ],
    )

    result = toolchain.run("anr_watcher.py", *mode, "--json")

    assert_refusal(
        result,
        json_mode=True,
        naming=("not attached",) if device == "gone" else ("ended inside a record",),
        status=status,
        streaming=mode[0] == "--watch",
    )
    if device == "still there" and adb_exit:
        refusal = json.loads(result.stdout.splitlines()[-1])
        assert "exited 1" in refusal["error"], refusal


def test_watch_stopped_while_a_record_arrives_on_an_open_stream_says_so(
    recorded, monkeypatch, capsys
):
    """--watch --duration ends an open stream: a record half-delivered then is said.

    Codex review of PR #34: the cut was seen only at the stream's end, which
    a deadline never reads, so `--watch --duration` returned success with no
    warning. The stream: one recorded jank record and the first 40 bytes of
    the recorded ANR report, still open when the deadline passes.
    """
    jank = _jank_records(recorded, (80,))[0]
    stream = _OpenLogcat(jank.raw + _anr_record(recorded).raw[:40])
    monkeypatch.setattr(anr_watcher.subprocess, "Popen", lambda *_a, **_k: stream)
    watcher = anr_watcher.AnrWatcher(serial="emulator-5554")
    try:
        assert watcher.watch(duration_seconds=1, json_mode=True) is True
    finally:
        stream.close()

    assert [e["kind"] for e in watcher.events] == ["jank"]
    assert "warning: the watch was stopped while a record was arriving" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The worker's identity (Codex review of PR #34)
# ---------------------------------------------------------------------------


def _hold_pid(store, session_id: str, pid: int, status: str) -> None:
    """The session's meta as it was left: ``pid`` recorded for its worker, in ``status``.

    As a session crashed before this fix left it (``mark_crashed`` kept the
    pid), or one whose worker was killed outright (still ``running``).
    """
    store.claim_worker(session_id, pid=pid)
    path = store.session_dir(session_id) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["status"] = status
    path.write_text(json.dumps(meta), encoding="utf-8")


@pytest.mark.parametrize("holder", ["another process", "a later process with the token", "nothing"])
@pytest.mark.parametrize("status", ["crashed", "running"])
def test_stop_never_signals_a_pid_that_is_no_longer_the_worker(
    holder, status, store, monkeypatch, capsys
):
    """A worker's pid outlives it; --stop signalled whatever held it by then.

    The session records a pid that is now another process -- a live one
    without the session's worker token on its command line, or one carrying
    it that started at another time than the worker claimed -- or no process
    at all. --stop sends it nothing (``os.kill`` recorded), says "worker
    gone", and saves the summary marked incomplete; the other process lives.
    """
    from common import anr_sessions

    meta = store.create({})
    token = (
        [anr_watcher.WORKER_TOKEN_FLAG, meta.worker_token]
        if holder == "a later process with the token"
        else []
    )
    stranger = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
            *token,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert stranger.stdout.readline().strip() == "ready"
        if holder == "nothing":
            stranger.kill()
            stranger.wait(timeout=30)
        _hold_pid(store, meta.session_id, stranger.pid, status)
        if holder == "a later process with the token":
            # The worker claimed the pid at another time than this process began.
            path = store.session_dir(meta.session_id) / "meta.json"
            held = json.loads(path.read_text(encoding="utf-8"))
            held["worker_started"] = "Thu Jan  1 00:00:00 1970"
            path.write_text(json.dumps(held), encoding="utf-8")
        signalled: list[tuple[int, int]] = []
        real_kill = os.kill

        def kill(pid, sig):
            # Any signal to the stranger's pid is recorded, not sent.
            if pid == stranger.pid:
                signalled.append((pid, sig))
                return
            real_kill(pid, sig)

        monkeypatch.setattr(anr_sessions.os, "kill", kill)
        monkeypatch.setattr(
            anr_watcher.sys, "argv", ["anr_watcher.py", "--stop", meta.session_id, "--json"]
        )

        with pytest.raises(SystemExit) as exc:
            cli.run(anr_watcher.main)

        alive = stranger.poll() is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            real_kill(stranger.pid, signal.SIGKILL)
        stranger.wait(timeout=30)
        stranger.stdout.close()

    assert signalled == [], "a process that is not the worker was signalled"
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert f"worker gone for {meta.session_id}" in captured.err, captured.err
    assert json.loads(captured.out)["incomplete"] is True
    assert alive == (holder != "nothing")
    assert store.load_meta(meta.session_id).pid is None, "the stale pid was kept"


def test_a_worker_with_the_wrong_token_does_not_claim_the_session(store, monkeypatch):
    """Only the worker its parent started -- with the session's token -- claims it.

    Hermetic even if the check is lost: logcat is a stand-in that is recorded,
    not run, and the session names emulator-5554 only.
    """
    spawned: list[list[str]] = []

    def _no_stream(cmd, **_kwargs):
        spawned.append(cmd)
        raise AssertionError(f"the worker started a stream: {cmd}")

    _logcat_through(monkeypatch, _no_stream)
    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "0")
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    meta = store.create({"serial": "emulator-5554"})

    assert anr_watcher.AnrBuster(store=store).run_worker(meta.session_id, token="0" * 16) == 2

    after = store.load_meta(meta.session_id)
    assert (after.pid, after.status) == (None, "pending")
    assert spawned == []


# ---------------------------------------------------------------------------
# A capture that may be missing events is compared as one (Codex review of PR #34)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loss", ["stream broke", "unverified", "crashed"])
def test_diff_concludes_nothing_from_what_a_damaged_capture_lacks(loss, recorded, store, tmp_path):
    """A broken, unauthenticated or crashed capture was saved complete.

    So `--diff` read a cluster it lacked as resolved: "improvement: 1
    resolved" against a complete session (the reviewer's reproduction). The
    complete session holds the recorded broadcast ANR report (system_server's
    pid as measured in its run); the damaged one holds nothing, and says why.
    """
    buster = anr_watcher.AnrBuster(store=store)
    complete = store.create({})
    _run_worker_over(
        store,
        complete.session_id,
        [_anr_record(recorded)],
        tmp_path,
        process_names=_oracle(recorded, BROADCAST),
    )
    buster.stop(complete.session_id)
    damaged = store.create({})
    counters = {"total": 0, "matched": 0, "dropped": 0}
    if loss == "stream broke":
        counters["stream_failure"] = {
            "error": "the stream ended inside a record",
            "truncated": True,
            "device": "available",
        }
    if loss == "unverified":
        counters["unverified_anr_lines"] = 1
    store.persist_worker_counters(damaged.session_id, counters)
    if loss in ("stream broke", "crashed"):
        store.mark_crashed(damaged.session_id)

    if loss == "stream broke":
        with pytest.raises(cli.Failure):
            buster.stop(damaged.session_id)
    else:
        buster.stop(damaged.session_id)
    result = json.loads(buster.diff(complete.session_id, damaged.session_id, json_mode=True))

    assert store.load_summary(damaged.session_id).incomplete is True
    assert result["resolved_clusters"] == [], result["verdict"]
    assert len(result["unconfirmed"]) == 1
    assert result["verdict"].startswith("inconclusive"), result["verdict"]


# ---------------------------------------------------------------------------
# The tag injection, through the worker's own pipe reader (optional, review)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claim", ["an ANR", "a duplicate verdict"])
def test_the_worker_is_not_fooled_by_a_header_forged_in_a_tag(claim, recorded, tmp_path):
    """The ANR and duplicate tag-injection regressions, read off a pipe by ``_FdRecords``.

    The same derivation as the --since test above (the recorded forging
    record, only its message replaced; for the verdict, WindowManager's
    recorded report a second before it), written to a real pipe and read by
    the session worker, system_server's pid as measured in the forger's run.
    """
    forging, _victim = _injection(recorded)
    broadcast = _frames(recorded, BROADCAST)
    if claim == "an ANR":
        message, before = f"ANR in {PACKAGE}", []
    else:
        verdict = next(f for f in broadcast if f.message.startswith("Skipping duplicate ANR"))
        report = next(f for f in broadcast if f.tag == "WindowManager")
        message = verdict.message
        before = [_moved(report, _seconds(forging) - _seconds(report) - 1)]
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    read_end, write_end = os.pipe()
    os.write(write_end, framed([*before, forging.replace(message=message)]))
    os.close(write_end)

    class _EndedLogcat:
        stdout = os.fdopen(read_end, "rb", buffering=0)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    proc = _EndedLogcat()
    try:
        with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
            anr_watcher.AnrBuster(store=store)._read_stream_into_events(
                proc=proc,
                out_handle=out_handle,
                stop_flag={"value": False},
                counters={"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0},
                package=None,
                min_frames=30,
                session_start_ms=0,
                process_names=_oracle(recorded, INJECTION),
            )
    finally:
        proc.stdout.close()

    anrs = [(e.process, e.app) for e in store.read_events(meta.session_id) if e.kind == "anr"]
    assert anrs == ([] if claim == "an ANR" else [("WindowManager", PACKAGE)]), anrs
