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
    recorded_ps_answer,
    recorded_zone,
    uid_rules,
)

from common import cli, env_config, logcat
from common.anr_pipeline import build_normalised_event, event_to_jsonl, parse_logcat_anr
from common.anr_sessions import SessionStore
from common.device_utils import parse_package_uids, parse_ps_uids

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
        """A logcat whose stdout is a pipe still open: quiet until the duration ends it."""

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            read_end, captured["write_end"] = os.pipe()
            self.stdout = os.fdopen(read_end, "rb", buffering=0)
            captured["stdout"] = self.stdout

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _FakePopen)
    try:
        ok = anr_watcher.AnrWatcher().watch(duration_seconds=1, json_mode=True)
    finally:
        captured["stdout"].close()
        os.close(captured["write_end"])
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
# The recorded window's title: the fixture activity's default, its component.
WINDOW_TITLE = f"{PACKAGE}/{PACKAGE}.DefaultActivity"


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
    """A resolver answered as the device answered, knowing system_server as measured with ``name``.

    Whose process a pid is: ``ps -o PID,UID -p <pid>`` answers with the
    recorded ps_pid_uid, its app row's pid substituted (the fixture app's
    process, alive under its uid), and a package's uids are package_list_uid's.
    It names a jank record's pid only from a `Start proc` in the stream, as
    the worker does.
    """
    system_server = _system_server(recorded, name)
    return anr_watcher._ProcessNames(
        system_server_pid=lambda: system_server,
        package_uids=lambda package: parse_package_uids(recorded.text("package_list_uid"), package),
        pid_uid=lambda pid: parse_ps_uids(recorded_ps_answer(recorded, str(pid))).get(str(pid)),
    )


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


def test_matches_package_takes_no_other_package_ending_the_same_way(recorded):
    """The last dotted segment used to match on its own (wave 4).

    So ``com.somebody.else.composefixture`` matched the fixture app's
    recorded ANR, and a prefix of the package matched too. A package is
    matched whole, or not at all.
    """
    event = _recorded_anr(recorded)

    for other in ("com.somebody.else.composefixture", "composefixture", "com.example"):
        assert anr_watcher.matches_package(event, other) is False, other


@pytest.mark.parametrize("package", ["com.example.app", "com.example.application"])
def test_package_does_not_match_jank_by_the_words_of_its_message(package, recorded):
    """The reviewer's replay: the recorded Choreographer record, unchanged.

    Its message says "The application may be doing too much work…", so the
    substring filter kept it for ``com.example.app`` (the short name ``app``)
    and ``com.example.application`` alike -- jank naming no package passed
    ``--package``. Only the identity the reader established counts: ``app``
    (a session's verified name for the pid), ``anr_package``, and the package
    of a whole component.
    """
    jank = parse_logcat_anr(_recorded_jank_burst(recorded, (80,))[0])
    assert jank is not None and "application" in jank["message"]

    assert anr_watcher.matches_package(jank, package) is False
    assert anr_watcher.matches_package({**jank, "app": package}, package) is True


def test_package_matches_a_components_package_whole(recorded):
    """The recorded input ANR's report names its component: its package counts, whole."""
    from common.anr_pipeline import event_from_record

    report = event_from_record(logcat.parse_records(_anr_record(recorded, INPUT).raw)[0])
    assert report is not None and report.get("component", "").startswith(f"{PACKAGE}/")

    assert anr_watcher.matches_package({**report, "anr_package": None}, PACKAGE) is True
    assert anr_watcher.matches_package({**report, "anr_package": None}, "com.example") is False


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
    # The device answers `device` whenever the worker asks -- it asks when a
    # stream ends by itself (review of PR #42): these streams end, it stays.
    monkeypatch.setattr(anr_watcher, "_require_device", lambda _serial: None)
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


@pytest.mark.parametrize(
    ("name", "reason"),
    [(BROADCAST, "Broadcast of Intent"), (INPUT, "Input dispatching timed out")],
    ids=["broadcast", "input"],
)
@pytest.mark.parametrize(
    "mode", [("--since", "1m"), ("--watch", "--duration", "2")], ids=["since", "watch"]
)
def test_one_stall_in_the_stream_is_one_printed_event(mode, name, reason, toolchain, recorded):
    """D5 end to end, over every record a reader sees of one recorded ANR.

    The broadcast records: ActivityManager's report (one record), then
    WindowManager's report and ActivityManager's "Skipping duplicate"
    verdict; the input records: WindowManager's report, then
    ActivityManager's. ``--watch`` reads a stream that stays open, as a live
    one does, until its duration ends it (wave 4: one that ends by itself is
    refused), after the one already-buffered record `-T 1` replays. The
    report's process is shown to be the package's as the device shows it: the
    recorded uid listing, and ps_pid_uid for the report's own PID: (substituted).
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, name) + "\n"),
            *uid_rules(recorded, PACKAGE, _stated_pid(_anr_record(recorded, name))),
            Rule(
                ("logcat",),
                buffered=framed([_history(recorded)]),
                stdout=recorded.data(f"{name}.bin"),
                hang=mode[0] == "--watch",
            ),
        ],
    )

    result = toolchain.run("anr_watcher.py", *mode, "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(event["kind"], event["anr_package"]) for event in events] == [("anr", PACKAGE)], events
    assert events[0]["reason"].startswith(reason)
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
    system_server, package_uids, pid_uid = anr_watcher._device_lookups(
        "emulator-5554", may_ask=lambda: False
    )

    assert system_server() is None
    assert package_uids(PACKAGE) is None
    assert pid_uid(4242) is None
    assert no_device == []


def test_the_device_lookups_are_bounded(monkeypatch, recorded):
    """All three questions carry the worker's short bound, which --stop's wait covers.

    The uid question is `ps -o PID,UID -p <pid>`, answered with the recorded
    ps_pid_uid (its app row's pid substituted).
    """
    from common.adb_exec import AdbResult

    asked: list[tuple] = []

    def _answer(*args, **kwargs):
        asked.append((args, kwargs))

    def _ps(*args, **kwargs):
        asked.append((args, kwargs))
        return AdbResult(0, recorded_ps_answer(recorded, "4242"), "")

    monkeypatch.setattr(anr_watcher, "get_system_server_pid", _answer)
    monkeypatch.setattr(anr_watcher, "get_package_uids", _answer)
    monkeypatch.setattr(anr_watcher, "run_adb", _ps)
    system_server, package_uids, pid_uid = anr_watcher._device_lookups(
        "emulator-5554", may_ask=lambda: True
    )

    system_server()
    package_uids(PACKAGE)
    uid = pid_uid(4242)

    bound = {"timeout": anr_watcher.LOOKUP_TIMEOUT_SECONDS}
    assert asked == [
        (("emulator-5554",), bound),
        (("emulator-5554", PACKAGE), bound),
        (("shell", "emulator-5554", "ps", "-o", "PID,UID", "-p", "4242"), bound),
    ]
    assert (
        uid
        == parse_ps_uids(recorded.text("ps_pid_uid"))[_entry(recorded, "ps_pid_uid")["pids"]["app"]]
    )
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
import json, os, sys, time
with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
for index, answer in enumerate(spec["answers"]):
    if not all(token in argv for token in answer["match"]):
        continue
    if answer["once"]:
        used = spec["log"] + ".used-" + str(index)
        if os.path.exists(used):
            continue
        open(used, "w").close()
    if answer["asked"]:
        with open(answer["asked"], "a", encoding="utf-8") as marker:
            marker.write(argv[-1] + "\\n")
    time.sleep(answer["delay"])
    streams = [("stdout", sys.stdout.buffer), ("stderr", sys.stderr.buffer)]
    for name, stream in (streams[::-1] if answer["stderr_first"] else streams):
        if name == "stderr" and answer["stderr_after"]:
            time.sleep(answer["stderr_after"])
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
    # Write stderr before stdout: what adb said is there before its stdout is read.
    stderr_first: bool = False
    # Answer the first matching call only; later ones go to the next answer.
    once: bool = False
    # Seconds between writing stdout and writing stderr.
    stderr_after: float = 0.0


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
        *([lookups["ps"]] if "ps" in lookups else []),
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
    # The report stands as an ANR of its window, its title kept as text (wave 4).
    anrs = [
        (e["process"], e.get("anr_package"), e.get("window")) for e in events if e["kind"] == "anr"
    ]
    assert anrs == ([] if claim == "an ANR" else [("WindowManager", None, WINDOW_TITLE)]), events


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
    # The device answers `device` whenever the worker asks -- it asks when a
    # stream ends by itself (review of PR #42): these streams end, it stays.
    monkeypatch.setattr(anr_watcher, "_require_device", lambda _serial: None)
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
    # The device answers `device` whenever the worker asks -- it asks when a
    # stream ends by itself (review of PR #42): these streams end, it stays.
    monkeypatch.setattr(anr_watcher, "_require_device", lambda _serial: None)
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
    monkeypatch.setattr(anr_watcher, "run_adb", _ps_only(recorded))


def _no_device_call(*args, **_kwargs):
    raise AssertionError(f"the worker asked the device: {args}")


def _ps_only(recorded):
    """A run_adb that answers `ps -o PID,UID -p <pid>` as the device did, and nothing else.

    The answer is the recorded ps_pid_uid with its app row's pid substituted:
    the fixture app's process, alive under its uid.
    """
    from common.adb_exec import AdbResult

    def run_adb(*args, **_kwargs):
        if args[:1] == ("shell",) and "ps" in args:
            return AdbResult(0, recorded_ps_answer(recorded, str(args[-1]).strip("'")), "")
        return _no_device_call(*args)

    return run_adb


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


def test_watch_prints_the_record_it_starts_on_marked_as_possibly_older(
    recorded, monkeypatch, capsys
):
    """--watch prints its stream's first record -- marked: `-T 1` replays it, it may predate.

    Into an empty buffer that record is a live one, so it is not dropped; but
    nothing in it shows it was logged after the watch began, so it carries
    ``"ambiguous_tail": true`` (wave 4, the same shape as a session's). The
    records after it are the watch's own. The stream stays open until the
    duration ends it.
    """
    jank = _jank_records(recorded, (80,))[0]
    later = _moved(jank, 5)
    stream = _OpenLogcat(framed([jank, later]))
    monkeypatch.setattr(anr_watcher.subprocess, "Popen", lambda *_a, **_k: stream)
    watcher = anr_watcher.AnrWatcher(serial="emulator-5554")

    try:
        assert watcher.watch(duration_seconds=1, json_mode=True) is True
    finally:
        stream.close()

    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["timestamp"], e.get("ambiguous_tail", False)) for e in printed] == [
        (_printed(jank)[:21], True),
        (_printed(later)[:21], False),
    ]
    # Shown, never in a total (Codex review of PR #39: get_summary and
    # get_json_output still counted it).
    output = watcher.get_json_output()
    assert output["summary"]["total"] == 1, output
    assert [e["timestamp"] for e in output["ambiguous_tail"]] == [_printed(jank)[:21]]
    assert watcher.get_summary().startswith("ANR/jank events: 1 ")


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
    from common.anr_pipeline import FINGERPRINT_VERSION

    assert "v1" in payload["error"] and f"v{FINGERPRINT_VERSION}" in payload["error"], payload


def test_a_v2_window_fallback_is_not_compared_with_a_v3_session(recorded, store, tmp_path):
    """Codex review of PR #39: v2 filed a WindowManager-only ANR under its title's package.

    So a v2 summary holds that fallback as a comparable, package-named
    cluster (``identity`` null), and against a v3 session that lacks it the
    diff said "improvement: 1 resolved". The version is now 3, a session keeps
    the version it was captured with, and --diff refuses the pair. The v2
    session is the recorded input ANR cut before ActivityManager's report, its
    summary stored as v2 stored it: the cluster's identity null.
    """
    from common.anr_pipeline import FINGERPRINT_VERSION

    old, new = store.create({}), store.create({})
    buster = anr_watcher.AnrBuster(store=store)
    records = _frames(recorded, INPUT)
    report = next(i for i, f in enumerate(records) if f.message.startswith(f"ANR in {PACKAGE}"))
    _run_worker_over(
        store, old.session_id, records[:report], tmp_path, process_names=_oracle(recorded, INPUT)
    )
    buster.stop(old.session_id)
    path = store.session_dir(old.session_id) / "summary.json"
    v2 = json.loads(path.read_text(encoding="utf-8"))
    v2["fingerprint_version"] = 2
    for cluster in v2["clusters"]:
        cluster["identity"] = None
        cluster["sample_event"]["identity"] = None
    path.write_text(json.dumps(v2), encoding="utf-8")
    buster.stop(new.session_id)

    with pytest.raises(cli.Failure) as refused:
        buster.diff(old.session_id, new.session_id, json_mode=True)
    assert "v2" in str(refused.value) and "v3" in str(refused.value)
    assert store.load_summary(new.session_id).fingerprint_version == FINGERPRINT_VERSION == 3


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
    # Whose process the report's pid is, as the device answered (recorded).
    monkeypatch.setattr(
        anr_watcher,
        "get_package_uids",
        lambda _s, package, **_k: parse_package_uids(recorded.text("package_list_uid"), package),
    )
    monkeypatch.setattr(anr_watcher, "run_adb", _ps_only(recorded))
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
    assert [(e["kind"], e["anr_package"]) for e in watcher.events] == [("anr", PACKAGE)]


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
    ("device", "status"),
    [("gone", 3), ("still there", 1), ("one of several", 1)],
    ids=["gone", "still-there", "several"],
)
def test_a_stream_cut_by_itself_stops_the_session_and_stop_refuses(
    device, status, recorded, recorded_anywhere, tmp_path, monkeypatch, capsys
):
    """A stream that ends inside a record BY ITSELF is not a session to trust.

    The worker does not restart it: it asks whether the device is there
    (`adb get-state`, the recorded answers), marks the session crashed, and
    --stop refuses after saving what was read -- exit 3 for a device that
    went, 1 otherwise -- never the summary as if nothing happened.

    Wave 4: adb answering "more than one device" (recorded:
    adb_more_than_one_device, typed by adb_exec as it types it everywhere)
    was persisted as "unavailable" and rebuilt as DeviceNotFoundError: exit
    3 and "Reconnect the device", where the same answer met directly is
    exit 1 and "Pass --serial". The worker keeps the error's class, words and
    remedy, and --stop raises that error again.
    """
    from common import adb_exec
    from common.adb_exec import AdbResult, DeviceNotFoundError

    jank = _jank_records(recorded, (80,))[0]
    stream_file = tmp_path / "cut.logcat"
    stream_file.write_bytes(jank.raw + _anr_record(recorded).raw[:40])
    spawned: list[_FileLogcat] = []

    def _stream(cmd, **_kwargs):
        spawned.append(_FileLogcat(stream_file))
        return spawned[-1]

    # The serials the multiple-device remedy lists, without asking adb.
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554", "emulator-5556"])

    def _get_state(*_args, **_kwargs):
        if device == "gone":
            raise DeviceNotFoundError(recorded_anywhere("adb_device_not_found").strip())
        if device == "one of several":
            raise adb_exec._classify(recorded_anywhere("adb_more_than_one_device"), None)
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
    if device == "one of several":
        assert "More than one device" in refusal["error"], refusal
        assert "--serial" in refusal["remedy"] and "emulator-5556" in refusal["remedy"], refusal
        assert "Reconnect" not in refusal["remedy"], refusal
    summary = store.load_summary(meta.session_id)
    assert summary is not None and summary.stream_failure, "the summary was not saved"
    assert f"--get-details {meta.session_id}" in refusal["remedy"], refusal


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

    # The one record that arrived whole is the stream's first: shown, not counted.
    assert ([e["kind"] for e in watcher.ambiguous], watcher.events) == (["jank"], [])
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


@pytest.mark.parametrize(
    ("loss", "said"),
    [
        ("stream broke", "the stream broke"),
        ("unverified", "could not be authenticated"),
        ("crashed", "crashed"),
        ("cut record", "1 record arriving when it stopped was cut off"),
        ("stream restarted", "the stream restarted 1 time"),
    ],
)
def test_diff_concludes_nothing_from_what_a_damaged_capture_lacks(
    loss, said, recorded, store, tmp_path
):
    """A broken, unauthenticated, crashed, cut or restarted capture was saved complete.

    So `--diff` read a cluster it lacked as resolved: "improvement: 1
    resolved" against a complete session (the reviewer's reproduction). The
    complete session holds the recorded broadcast ANR report (system_server's
    pid as measured in its run); the damaged one holds nothing, and says why.
    Wave 4: a record cut by the stop, and a stream restart -- whose `-T 1`
    replays only the newest record logged while no stream was up -- are
    losses too, and were saved ``incomplete: false``.
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
    if loss == "cut record":
        counters["cut_records"] = 1
    if loss == "stream restarted":
        counters["stream_restarts"] = 1
    store.persist_worker_counters(damaged.session_id, counters)
    if loss in ("stream broke", "crashed"):
        store.mark_crashed(damaged.session_id)

    if loss == "stream broke":
        with pytest.raises(cli.Failure):
            buster.stop(damaged.session_id)
    else:
        buster.stop(damaged.session_id)
    result = json.loads(buster.diff(complete.session_id, damaged.session_id, json_mode=True))

    saved = store.load_summary(damaged.session_id)
    assert saved.incomplete is True
    assert said in (saved.incomplete_reason or ""), saved.incomplete_reason
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

    # The report stands as an ANR of its window: its title names no package (wave 4).
    anrs = [(e.process, e.app) for e in store.read_events(meta.session_id) if e.kind == "anr"]
    assert anrs == ([] if claim == "an ANR" else [("WindowManager", None)]), anrs


# ---------------------------------------------------------------------------
# Wave 4: a live stream that ends by itself is never a quiet success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device", "status"), [("gone", 3), ("still there", 1)], ids=["gone", "still-there"]
)
def test_a_watch_whose_stream_ends_by_itself_asks_the_device_and_never_succeeds(
    device, status, toolchain, recorded, recorded_anywhere
):
    """The reviewer's replay: preflight answered, then the live logcat closes and exits 0.

    `logcat -T 1` follows until it is stopped, so a stream that ends on its
    own ended because something went away. With adb exiting 0 the watch
    returned success -- exit 0, empty stdout -- having asked the device only
    before it began. It asks `adb get-state` again now (the recorded answers:
    `device` once, then -- for "gone" -- the recorded not-found refusal), and
    refuses: exit 3 when the device went, 1 otherwise. The stream is the one
    recorded record a fresh `-T 1` replays, then nothing.
    """
    get_state = [Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=device == "gone")]
    if device == "gone":
        get_state.append(
            Rule(("get-state",), stderr=recorded_anywhere("adb_device_not_found"), exit=1)
        )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *get_state,
            Rule(("logcat",), stdout=framed([_history(recorded)]), exit=0),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--watch", "--duration", "30", "--json")

    assert_refusal(
        result,
        json_mode=True,
        naming=("not attached",) if device == "gone" else ("ended",),
        status=status,
        streaming=True,
    )
    asked = [call for call in toolchain.calls("adb") if "get-state" in call]
    assert len(asked) == 2, f"asked the device {len(asked)} time(s): {asked}"


def test_a_watch_whose_stream_gives_no_exit_status_asks_the_device(
    recorded, recorded_anywhere, monkeypatch
):
    """The other half of the replay: the stream closes, and adb gives no exit status in time.

    The same guard accepted ``None`` (a wait that timed out) as success. The
    stand-in's stdout is at its end and its wait times out; `adb get-state`
    answers with the recorded not-found refusal, typed as run_adb types it.
    """
    from common.adb_exec import DeviceNotFoundError

    class _ClosedNoStatus:
        def __init__(self, *_args, **_kwargs):
            read_end, write_end = os.pipe()
            os.close(write_end)
            self.stdout = os.fdopen(read_end, "rb", buffering=0)

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("adb logcat", timeout)

        def terminate(self):
            return None

    asked: list[tuple] = []

    def _gone(*args, **_kwargs):
        asked.append(args)
        raise DeviceNotFoundError(recorded_anywhere("adb_device_not_found").strip())

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _ClosedNoStatus)
    monkeypatch.setattr(anr_watcher, "run_adb", _gone)
    monkeypatch.setattr(anr_watcher, "STREAM_EXIT_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(DeviceNotFoundError):
        anr_watcher.AnrWatcher(serial="emulator-5554").watch(json_mode=True)

    assert [args[0] for args in asked] == ["get-state"]


# ---------------------------------------------------------------------------
# Wave 4: the record a session starts on is shown, not counted
# ---------------------------------------------------------------------------


def _session_starting_on(store, records: list[Frame], tmp_path, **kwargs) -> str:
    """A session whose stream began with ``records``, the first being `-T 1`'s replay.

    Read by the worker's reader with the session's capture boundary, as
    run_worker reads the first stream, and its counters handed over as the
    worker hands them to --stop.
    """
    meta = store.create({})
    counters = _run_worker_over(
        store,
        meta.session_id,
        records,
        tmp_path,
        boundary=anr_watcher._CaptureBoundary(),
        **kwargs,
    )
    store.persist_worker_counters(meta.session_id, counters)
    return meta.session_id


@pytest.mark.parametrize("first", ["anr", "jank"])
def test_the_record_a_session_starts_on_is_shown_not_counted(first, recorded, store, tmp_path):
    """B5's other half: `-T 1`'s one replayed record may predate the session.

    The reviewer's replay: the recorded ANR report (or jank record) is the
    newest record buffered when the session starts, and nothing new happens.
    It was counted as the session's incident, its delta taken from when it
    was READ. Nothing in a record can show it was logged after --start, so
    it is kept as ``ambiguous_tail`` evidence: in the summary, in JSON and in
    text, and out of the counts and clusters.
    """
    record = _anr_record(recorded) if first == "anr" else _jank_records(recorded, (80,))[0]
    session_id = _session_starting_on(
        store, [record], tmp_path, process_names=_oracle(recorded, BROADCAST)
    )
    buster = anr_watcher.AnrBuster(store=store)

    payload = json.loads(buster.stop(session_id, json_mode=True))

    assert (payload["event_count"], payload["clusters"]) == (0, []), payload
    (tail,) = payload["ambiguous_tail"]
    assert (tail["kind"], tail["timestamp"]) == (first, _printed(record)[:21]), tail
    assert store.read_events(session_id) == []
    details = buster.get_details(session_id)
    assert "Ambiguous:" in details and "not counted" in details, details
    assert "may predate" in buster.stop(session_id, terse=True)


def test_a_record_that_may_predate_the_session_is_not_compared(recorded, store, tmp_path):
    """--diff reads only counted clusters: the ambiguous record resolves nothing.

    Session A started on the recorded ANR report as its replayed record;
    session B on a record that is no event. Counted, the report made "A had
    an ANR, B has none": improvement: 1 resolved. A's uncounted evidence is
    named, so the verdict cannot be a plain "no change" either.
    """
    buster = anr_watcher.AnrBuster(store=store)
    names = {"process_names": _oracle(recorded, BROADCAST)}
    before = _session_starting_on(store, [_anr_record(recorded)], tmp_path, **names)
    after = _session_starting_on(store, [_history(recorded)], tmp_path, **names)
    buster.stop(before)
    buster.stop(after)

    result = json.loads(buster.diff(before, after, json_mode=True))

    assert result["resolved_clusters"] == [], result["verdict"]
    assert result["verdict"].startswith("inconclusive: "), result["verdict"]
    assert f"{before} shows a record it started on but did not count" in result["verdict"]


@pytest.mark.parametrize("held_back", ["ambiguous_tail", "unattributed_anrs"])
@pytest.mark.parametrize("absent_from", ["second", "first"])
def test_evidence_a_session_did_not_count_makes_absence_unconfirmed(
    held_back, absent_from, recorded, store, tmp_path
):
    """Codex review of PR #39: excluded evidence vanished from comparisons.

    One session counts the recorded broadcast ANR. The other holds the same
    ANR only as evidence it did not count -- the record it started on
    (``ambiguous_tail``), or an ANR ``--package`` dropped for naming no
    verified package (``unattributed_anrs: 1``, the recorded report read
    without its process shown to be the package's). Both summaries are
    complete, and the diff said "improvement: 1 resolved" one way and
    "regression: 1 new critical" the other. The ANR is unconfirmed, and the
    verdict says what was not counted.
    """
    buster = anr_watcher.AnrBuster(store=store)
    counted = store.create({})
    _run_worker_over(
        store,
        counted.session_id,
        [_anr_record(recorded)],
        tmp_path,
        process_names=_oracle(recorded, BROADCAST),
    )
    buster.stop(counted.session_id)
    if held_back == "ambiguous_tail":
        held = _session_starting_on(
            store, [_anr_record(recorded)], tmp_path, process_names=_oracle(recorded, BROADCAST)
        )
    else:
        meta = store.create({"package": PACKAGE})
        counters = _run_worker_over(
            store,
            meta.session_id,
            [_anr_record(recorded)],
            tmp_path,
            package=PACKAGE,
            process_names=anr_watcher._ProcessNames(
                system_server_pid=lambda: _system_server(recorded, BROADCAST)
            ),
        )
        store.persist_worker_counters(meta.session_id, counters)
        held = meta.session_id
    buster.stop(held)
    first, second = (
        (counted.session_id, held) if absent_from == "second" else (held, counted.session_id)
    )

    result = json.loads(buster.diff(first, second, json_mode=True))

    summary = store.load_summary(held)
    assert summary.incomplete is False and summary.event_count == 0
    assert (result["resolved_clusters"], result["new_clusters"]) == ([], []), result["verdict"]
    assert [c["absent_from"] for c in result["unconfirmed"]] == [held], result
    assert result["verdict"].startswith("inconclusive: "), result["verdict"]
    said = {
        "ambiguous_tail": "shows a record it started on but did not count",
        "unattributed_anrs": "dropped 1 ANR --package could not attribute",
    }[held_back]
    assert f"{held} {said}" in result["verdict"], result["verdict"]


@pytest.mark.parametrize(
    "streams",
    [
        pytest.param(lambda anr, history: [[anr]], id="first-stream"),
        pytest.param(lambda anr, history: [[], [anr]], id="restart-before-anything-was-read"),
    ],
)
def test_the_worker_keeps_the_replayed_record_out_of_its_events(
    streams, recorded, tmp_path, monkeypatch, system_server_is_known
):
    """run_worker end to end: the first record the session reads is the ambiguous one.

    Whether the first stream delivers it, or a restart does after the first
    stream delivered nothing -- a restart's `-T 1` replay is just as old when
    the session has read nothing to hold it against. Review of PR #34 found
    the recorded ANR into an empty buffer (after `logcat -c`) came out as
    ZERO events with no word said; it is now disclosed, not lost.
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, streams(_anr_record(recorded), _history(recorded)), tmp_path, monkeypatch
    )

    assert store.read_events(session_id) == []
    counters = store.load_meta(session_id).extras["line_counters"]
    assert [(t["kind"], t["app"]) for t in counters["ambiguous_tail"]] == [("anr", PACKAGE)]
    assert counters["matched"] == 0


def test_a_session_counts_what_follows_the_record_it_starts_on(
    recorded, tmp_path, monkeypatch, system_server_is_known
):
    """Only the one replayed record is set aside: the recorded ANR after it is the session's."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")

    session_id = _worker_over_streams(
        store, [[_history(recorded), _anr_record(recorded)]], tmp_path, monkeypatch
    )

    assert [(e.kind, e.app) for e in store.read_events(session_id)] == [("anr", PACKAGE)]
    assert store.load_meta(session_id).extras["line_counters"]["ambiguous_tail"] == []


# ---------------------------------------------------------------------------
# Wave 4: --package keeps what it can attribute, and says what it cannot
# ---------------------------------------------------------------------------


def test_package_drops_an_anr_of_a_window_and_says_so(recorded, store, tmp_path):
    """A WindowManager-only ANR names no package, so ``--package`` cannot keep it.

    It is not dropped silently: the summary counts it (``unattributed_anrs``)
    and says so in text. The recorded input ANR cut after WindowManager's
    report -- its window titled as recorded, the fixture app's own component.
    """
    records = _frames(recorded, INPUT)
    report = next(i for i, f in enumerate(records) if f.message.startswith(f"ANR in {PACKAGE}"))
    meta = store.create({"package": PACKAGE})
    counters = _run_worker_over(
        store,
        meta.session_id,
        records[:report],
        tmp_path,
        package=PACKAGE,
        process_names=_oracle(recorded, INPUT),
    )
    store.persist_worker_counters(meta.session_id, counters)
    buster = anr_watcher.AnrBuster(store=store)

    payload = json.loads(buster.stop(meta.session_id, json_mode=True))

    assert (payload["event_count"], payload["unattributed_anrs"]) == (0, 1), payload
    assert "Unattributed: 1" in buster.get_details(meta.session_id)


# ---------------------------------------------------------------------------
# Acceptance: each recorded ANR is one event, through every reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [BROADCAST, INPUT])
def test_a_session_reads_each_recorded_anr_as_one_event(name, recorded, tmp_path):
    """--start, the recorded ANR's records after a replayed one, --stop --json: one ANR.

    The report's process is shown to be the package's as the device shows it:
    `ps -o PID,UID -p <its PID:>` answered with the recorded ps_pid_uid (that
    pid substituted), and the package's uid listing as recorded.
    """
    stated = _stated_pid(_anr_record(recorded, name))
    device = _LiveDevice(
        tmp_path,
        _device_answers(
            recorded,
            [_history(recorded), *_frames(recorded, name)],
            system_server=_Answer(
                ("pidof", "system_server"), stdout=_system_server(recorded, name) + "\n"
            ),
            ps=_Answer(("shell", "ps"), stdout=recorded_ps_answer(recorded, stated)),
        ),
    )

    summary = _session_summary(device, events=1)

    anrs = [c for c in summary["clusters"] if c["sample_event"]["kind"] == "anr"]
    assert [(c["count"], c["sample_event"]["app"]) for c in anrs] == [(1, PACKAGE)], anrs
    assert summary["event_count"] == 1, summary


@pytest.mark.parametrize(
    "title",
    ["com.example.victim/com.example.victim.DefaultActivity", "Skipped 80 frames!"],
    ids=["another-apps-component", "jank-words"],
)
def test_since_reads_a_windows_title_as_text(title, toolchain, recorded):
    """Both of the reviewer's replays, as an agent reads them: `--since --json`.

    The recorded input ANR's records with only the window's title replaced,
    wherever it is printed (WindowManager's report and the reason
    ActivityManager quotes). It printed an ANR of the victim beside the real
    one, or 80 frames of jank; it is one ANR, the app's that ActivityManager
    names.
    """
    records = [
        f.replace(message=f.message.replace(WINDOW_TITLE, title)) for f in _frames(recorded, INPUT)
    ]
    if records == _frames(recorded, INPUT):
        pytest.fail("the recorded records no longer print the window's title")
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, INPUT) + "\n"),
            *uid_rules(recorded, PACKAGE, _stated_pid(_anr_record(recorded, INPUT))),
            Rule(("logcat",), stdout=framed(records)),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m", "--json")

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(e["kind"], e["process"], e.get("anr_package")) for e in events] == [
        ("anr", "ActivityManager", PACKAGE)
    ], events


@pytest.mark.parametrize(
    ("device", "status"), [("gone", 3), ("still there", 1)], ids=["gone", "still-there"]
)
def test_a_since_dump_that_times_out_asks_the_device(
    device, status, toolchain, recorded, recorded_anywhere, monkeypatch, capsys
):
    """A dump whose device went does not fail: it waits, and the bound ends it (wave 4).

    `adb logcat -d -B` aimed at a device that is not there prints "- waiting
    for device -" and never exits (recorded: adb_waiting_for_device; W4-L
    measured it for `-d -B`), so a timed-out `--since` read is how a dump
    whose device went ends -- and it exited 1, "retry", without asking. adb
    here answers `get-state` with the recorded `device` once (the preflight),
    then -- for "gone" -- with the recorded not-found refusal, and its logcat
    writes the recorded banner and hangs. Run in-process, against the stub
    adb, so the dump's bound can be shortened.
    """
    get_state = [Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=device == "gone")]
    if device == "gone":
        get_state.append(
            Rule(("get-state",), stderr=recorded_anywhere("adb_device_not_found"), exit=1)
        )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *get_state,
            Rule(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
        ],
    )
    toolchain.apply(monkeypatch)
    monkeypatch.setattr(anr_watcher, "SINCE_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(anr_watcher.sys, "argv", ["anr_watcher.py", "--since", "1m", "--json"])

    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)

    assert exc.value.code == status
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["success"] is False
    assert ("not attached" if device == "gone" else "did not finish") in refusal["error"], refusal
    asked = [call for call in toolchain.calls("adb") if "get-state" in call]
    assert len(asked) == 2, f"asked the device {len(asked)} time(s): {asked}"


# ---------------------------------------------------------------------------
# W4-N (Codex review of PR #39): ActivityManager names a process, not a package
# ---------------------------------------------------------------------------

FOREIGN = "logcat_anr_foreign_process_records"
FOREIGN_PROCESS = "com.example.victim"


def _foreign_listing(recorded) -> str:
    """Both packages' uid listings as recorded in the foreign-process run."""
    entry = _entry(recorded, FOREIGN)
    return entry["package_uid_listing"]["output"] + entry["claimed_package_uid_listing"]["output"]


def test_since_does_not_file_an_anr_under_the_package_its_process_is_named_after(
    toolchain, recorded
):
    """The reviewer's case end to end: `--since --package com.example.victim`.

    The recording: the fixture app's own stall, in a process it named
    com.example.victim. ActivityManager reports "ANR in com.example.victim",
    and that passed `--package com.example.victim` as the victim's ANR. adb
    answers as the device did in that run: `pidof system_server`, each
    package's uid listing (com.example.victim has none) and `ps` for the
    stalled pid while it lived. system_server's own `Start proc` for the pid,
    in the dump, gives its uid -- the fixture app's -- so ps is not needed.
    """
    entry = _entry(recorded, FOREIGN)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, FOREIGN) + "\n"),
            Rule(("cmd", "package"), stdout=_foreign_listing(recorded)),
            Rule(("shell", "ps"), stdout=entry["ps_alive"]["output"]),
            Rule(("logcat",), stdout=recorded.data(f"{FOREIGN}.bin")),
        ],
    )

    filtered = toolchain.run(
        "anr_watcher.py", "--since", "1m", "--package", FOREIGN_PROCESS, "--json"
    )
    everything = toolchain.run("anr_watcher.py", "--since", "1m", "--json")

    assert filtered.returncode == 0, filtered.stderr
    assert filtered.stdout.strip() == "", filtered.stdout
    assert "--package dropped 1 ANR naming no verified package" in filtered.stderr
    (event,) = [json.loads(line) for line in everything.stdout.splitlines()]
    assert (event["anr_package"], event["process_name"]) == (None, FOREIGN_PROCESS), event
    assert event["attribution"] == "unverified"


@pytest.mark.parametrize("named", [FOREIGN_PROCESS, PACKAGE], ids=["impostor", "the-package"])
def test_a_session_takes_the_pids_uid_from_system_servers_start_proc(named, recorded, tmp_path):
    """Lifecycle evidence first: the stream's own `Start proc <pid>:<name>/<uid>`.

    The recorded foreign-process records, as recorded ("impostor"), or with
    only the process name -- the app's to choose -- set to the package's own
    ("the-package"), as when the receiver runs in the app's default process.
    Either way the pid's uid is the one system_server stated when it started
    the process, and `ps` -- which cannot show who held a pid in the past --
    is never asked. The impostor is no package's; the package's is filed.
    """
    records = [
        f.replace(message=f.message.replace(FOREIGN_PROCESS, named))
        for f in _frames(recorded, FOREIGN)
    ]
    asked_ps: list[int] = []
    names = anr_watcher._ProcessNames(
        system_server_pid=lambda: _system_server(recorded, FOREIGN),
        package_uids=lambda package: parse_package_uids(_foreign_listing(recorded), package),
        pid_uid=lambda pid: asked_ps.append(pid),
    )
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(store, meta.session_id, records, tmp_path, process_names=names)

    (event,) = store.read_events(meta.session_id)
    expected = (PACKAGE, None) if named == PACKAGE else (None, "process")
    assert (event.app, event.identity) == expected, event
    assert asked_ps == []
    # The attribution survives into the stored event, and --get-details says it.
    assert event.attribution == ("verified" if named == PACKAGE else "unverified")
    buster = anr_watcher.AnrBuster(store=store)
    buster.stop(meta.session_id)
    detail = buster.get_details(meta.session_id, cluster=1)
    said = f"[unverified: process {FOREIGN_PROCESS} names no verified package]"
    assert (said in detail) == (named != PACKAGE), detail


def test_since_text_says_what_it_could_not_attribute(toolchain, recorded):
    """Codex review of PR #39: the text form printed `ANR in com.example.victim` unqualified.

    JSON says `attribution: unverified`; a line of text read alone said the
    victim's ANR. The recorded foreign-process records, adb answering as the
    device did in that run: the line says the process names no verified
    package, and the summary that one of its events is unverified.
    """
    entry = _entry(recorded, FOREIGN)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, FOREIGN) + "\n"),
            Rule(("cmd", "package"), stdout=_foreign_listing(recorded)),
            Rule(("shell", "ps"), stdout=entry["ps_alive"]["output"]),
            Rule(("logcat",), stdout=recorded.data(f"{FOREIGN}.bin")),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m")

    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("ANR ")]
    assert f"[unverified: process {FOREIGN_PROCESS} names no verified package]" in line, line
    assert "ANR/jank events: 1, including 1 unverified |" in result.stdout, result.stdout


def test_since_text_says_a_window_report_may_be_a_duplicate(toolchain, recorded):
    """Codex review of PR #39: two reports printed, "ANR/jank events: 2", nothing more said.

    The recorded broadcast ANR cut after WindowManager's report, before the
    verdict that settles it by hash: ActivityManager's report (its process
    shown to be the package's, as the device answered) and an ANR of a
    window. The window's line says it may be a duplicate of the report, by the
    report's time, and the summary counts it so.
    """
    records = _frames(recorded, BROADCAST)
    window = next(i for i, f in enumerate(records) if f.tag == "WindowManager")
    report = _anr_record(recorded)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("pidof", "system_server"), stdout=_system_server(recorded, BROADCAST) + "\n"),
            *uid_rules(recorded, PACKAGE, _stated_pid(report)),
            Rule(("logcat",), stdout=framed(records[: window + 1])),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m")

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("ANR ")]
    assert len(lines) == 2, result.stdout
    assert "possible duplicate" not in lines[0], lines[0]
    assert f"[possible duplicate of the ANR at {_printed(report)[:21]}]" in lines[1], lines[1]
    assert "[an ANR of a window: its title names no package]" in lines[1], lines[1]
    assert (
        "ANR/jank events: 2, including 1 possible duplicate; 1 unverified |" in result.stdout
    ), result.stdout


def test_an_interrupted_watch_asks_the_device_nothing_more(recorded, monkeypatch):
    """Codex review of PR #39: the legacy watcher's lookups ignored Ctrl-C.

    The interrupt arrives while the first question (system_server's pid) is
    under way. That one finishes; no other starts -- not the `ps` for the
    report's pid, nor a package's uids -- as in the session worker. The
    recorded broadcast ANR report, handed to the watcher as its stream would.
    """
    watcher = anr_watcher.AnrWatcher(serial="emulator-5554")
    asked: list[str] = []

    def _system_server_then_interrupted(*_args, **_kwargs):
        asked.append("pidof system_server")
        watcher.interrupted = True
        return _system_server(recorded, BROADCAST)

    def _noted(*args, **_kwargs):
        """Note the question; answer with nothing (no uid, no ps row)."""
        from common.adb_exec import AdbResult

        asked.append(" ".join(str(arg) for arg in args))
        return AdbResult(1, "", "") if args[:1] == ("shell",) else None

    monkeypatch.setattr(anr_watcher, "get_system_server_pid", _system_server_then_interrupted)
    monkeypatch.setattr(anr_watcher, "get_package_uids", _noted)
    monkeypatch.setattr(anr_watcher, "run_adb", _noted)

    watcher._handle_record(logcat.parse_records(_anr_record(recorded).raw)[0], None, json_mode=True)

    assert asked == ["pidof system_server"], asked
    (event,) = watcher.events
    assert (event["anr_package"], event["attribution"]) == (None, "unverified")


# ---------------------------------------------------------------------------
# Closure review of v0.8.0: jank --package dropped unnamed, and a cut stream's adb words
# ---------------------------------------------------------------------------

FOREIGN = "logcat_anr_foreign_process_records"
CRASH_DUMP = "logcat_crash_buffer_records"
# DeviceNotFoundError's remedy, as adb_exec types the recorded not-found text.
NOT_FOUND_REMEDY = "Run `adb devices` to see what is available."


def _neutral(recorded) -> Frame:
    """A recorded record that is no event and names no process: the reviewer's first record.

    logcat_anr_foreign_process_records' first ``Broadcasting:`` record, read
    first so that the jank after it is read, not `-T 1`'s replay.
    """
    return next(f for f in _frames(recorded, FOREIGN) if f.message.startswith("Broadcasting:"))


def _launch(recorded, index: int) -> tuple[Frame, Frame]:
    """The restart recording's ``index``th process: its `Start proc` record and its jank."""
    pid = _entry(recorded, RESTART)["pids"][index]
    records = _frames(recorded, RESTART)
    start = next(f for f in records if f.message.startswith(f"Start proc {pid}:"))
    jank = next(f for f in records if f.tag == "Choreographer" and f.pid == pid)
    return start, jank


def _package_session(store, records: list[Frame], recorded, tmp_path) -> str:
    """A ``--package`` session over ``records``, read as the worker reads its first stream.

    Its meta records the package, as --start records it.
    """
    meta = store.create({"package": PACKAGE})
    counters = _run_worker_over(
        store,
        meta.session_id,
        records,
        tmp_path,
        boundary=anr_watcher._CaptureBoundary(),
        package=PACKAGE,
        process_names=_names(recorded),
    )
    store.persist_worker_counters(meta.session_id, counters)
    return meta.session_id


@pytest.mark.parametrize("absent_from", ["second", "first"])
def test_jank_package_dropped_unnamed_is_never_read_as_resolved(
    absent_from, recorded, store, tmp_path, no_device
):
    """Closure review of v0.8.0, REQUIRED 1: the reviewer's replay, through the worker's reader.

    Session A reads the recorded `Start proc` of the fixture app's first
    process, then that process's recorded jank. Session B reads the same jank
    with the `Start proc` moved out of its stream -- the process started
    before the capture -- after the reviewer's neutral first record. Under
    --package, B dropped the jank for naming no package and kept only its pid
    (``unnamed_pids``), so `--diff A B` said "improvement: 1 resolved" with
    nothing unconfirmed (and B A "regression: 1 new"). B holds evidence it
    did not count: the jank is unconfirmed, and the verdict says what B
    dropped.
    """
    start, jank = _launch(recorded, 0)
    buster = anr_watcher.AnrBuster(store=store)
    named = _package_session(store, [start, jank], recorded, tmp_path)
    unnamed = _package_session(store, [_neutral(recorded), jank], recorded, tmp_path)
    buster.stop(named)
    buster.stop(unnamed)
    first, second = (named, unnamed) if absent_from == "second" else (unnamed, named)

    result = json.loads(buster.diff(first, second, json_mode=True))

    assert (result["resolved_clusters"], result["new_clusters"]) == ([], []), result["verdict"]
    assert [c["absent_from"] for c in result["unconfirmed"]] == [unnamed], result
    assert result["verdict"].startswith("inconclusive: "), result["verdict"]
    assert f"{unnamed} dropped 1 jank/StrictMode record" in result["verdict"], result["verdict"]
    text = buster.diff(first, second)
    assert "improvement" not in text and "regression" not in text, text
    assert f"absent from {unnamed}" in text, text


def test_jank_package_dropped_unnamed_makes_a_shared_maximum_a_lower_bound(
    recorded, store, tmp_path, no_device
):
    """Absence is one conclusion it cannot support; a maximum is the other.

    Both sessions read the first process's recorded `Start proc` and jank (33
    frames). B also reads the second process's recorded jank (50 frames) with
    its `Start proc` moved out of the stream, so --package dropped it -- a
    jank of the package's, uncounted. A cluster both hold compared as
    "stable" on B's 33 frames, a maximum B was missing a record above.
    """
    start, jank = _launch(recorded, 0)
    _, other_jank = _launch(recorded, 1)
    buster = anr_watcher.AnrBuster(store=store)
    before = _package_session(store, [_neutral(recorded), start, jank], recorded, tmp_path)
    after = _package_session(
        store, [_neutral(recorded), start, jank, other_jank], recorded, tmp_path
    )
    buster.stop(before)
    buster.stop(after)

    result = json.loads(buster.diff(before, after, json_mode=True))

    assert (result["drift"], result["stable_count"]) == ([], 0), result
    (shared,) = result["not_compared"]
    assert f"{after} dropped 1 jank/StrictMode record" in shared["reason"], shared
    assert result["verdict"].startswith("inconclusive: "), result["verdict"]


def test_a_session_discloses_the_jank_package_dropped_unnamed(recorded, store, tmp_path, no_device):
    """--stop says what --package dropped for want of a name: JSON, text and the one-line form.

    The reviewer's session B: the recorded jank of a process whose `Start
    proc` the stream never showed. One below the frame threshold -- the
    same record, its count set to 5 -- would have been dropped named or
    not, so it is not counted as lost. Without --package nothing is dropped:
    the jank is kept, keyed by pid.
    """
    _, jank = _launch(recorded, 0)
    (small,) = _jank_records(recorded, (5,), pid=jank.pid)
    buster = anr_watcher.AnrBuster(store=store)
    session = _package_session(store, [_neutral(recorded), jank, small], recorded, tmp_path)
    everything = _session_starting_on(
        store, [_neutral(recorded), jank], tmp_path, process_names=_names(recorded)
    )

    payload = json.loads(buster.stop(session, json_mode=True))

    assert payload["unnamed_dropped"] == {"records": 1, "pids": [int(jank.pid)]}, payload
    assert payload["incomplete"] is False, payload
    details = buster.get_details(session)
    assert f"--package dropped 1 jank/StrictMode record of pid {jank.pid}" in details, details
    terse = buster.stop(session, terse=True)
    assert "1 jank/StrictMode record dropped unnamed" in terse, terse
    unfiltered = json.loads(buster.stop(everything, json_mode=True))
    assert unfiltered["unnamed_dropped"] == {"records": 0, "pids": []}, unfiltered


def test_since_with_package_says_what_it_dropped_unnamed(toolchain, recorded):
    """The same shape in the legacy reader: --package dropped jank, and said nothing.

    --watch and --since name no process, so under --package every jank and
    StrictMode record is dropped -- documented, but the summary read "No
    ANR/jank events detected." with no word of it. The dump is the recorded
    Choreographer record; it is dropped, and counted, and said.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), stdout=framed(_jank_records(recorded, (80,)))),
        ],
    )

    result = toolchain.run("anr_watcher.py", "--since", "1m", "--package", PACKAGE)

    assert result.returncode == 0, result.stderr
    assert (
        "No ANR/jank events detected. --package dropped 1 jank/StrictMode record(s)"
        in result.stdout
    ), result.stdout
    assert "warning: --package dropped 1 jank/StrictMode record:" in result.stderr, result.stderr


def _cut_stream(toolchain, recorded, recorded_anywhere, adb_exit: int, said: str) -> None:
    """The reviewer's replay as an adb: the recorded crash dump less one byte, and adb's words.

    The recorded binary crash dump less its last byte -- a stream that ends
    inside its second record, by itself -- after which adb writes ``said``
    (the recorded device-not-found text, or nothing) and exits ``adb_exit``;
    `adb get-state` answers the recorded `device`, the device back by the
    time it is asked.
    """
    stderr = recorded_anywhere("adb_device_not_found") if said == "device not found" else ""
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(
                ("logcat",),
                stdout=recorded.data(f"{CRASH_DUMP}.bin")[:-1],
                stderr=stderr,
                exit=adb_exit,
            ),
        ],
    )


@pytest.mark.parametrize("adb_exit", [1, 255])
@pytest.mark.parametrize(
    "mode", [("--watch", "--duration", "30"), ("--since", "1m")], ids=["watch", "since"]
)
def test_a_read_cut_by_itself_keeps_the_device_error_adb_wrote(
    mode, adb_exit, toolchain, recorded, recorded_anywhere
):
    """Closure review of v0.8.0, REQUIRED 2: adb's own words come before the framing refusal.

    adb exits 255 as the reviewer ran it, or 1, the recorded refusal's own
    status. --watch asked the device, was told it was there, and refused the
    framing: exit 1, LogFramingError, where log_monitor in the same place
    keeps DeviceNotFoundError, exit 3. adb's stderr is typed first now, as
    _refuse_unexpected_end and log_monitor type it. --since already did
    (run_argv types adb's words before the dump is parsed): pinned with it.
    """
    _cut_stream(toolchain, recorded, recorded_anywhere, adb_exit, "device not found")

    result = toolchain.run("anr_watcher.py", *mode, "--json")

    refusal = assert_refusal(
        result,
        json_mode=True,
        naming=("not attached",),
        status=3,
        streaming=mode[0] == "--watch",
    )
    assert refusal["remedy"] == NOT_FOUND_REMEDY, refusal


@pytest.mark.parametrize("adb_exit", [1, 255])
@pytest.mark.parametrize(
    ("said", "status"), [("device not found", 3), ("nothing", 1)], ids=["not-found", "nothing"]
)
def test_a_session_cut_by_itself_keeps_what_adb_said(
    said, status, adb_exit, toolchain, recorded, recorded_anywhere
):
    """REQUIRED 2's detached path: the worker kept no stderr, so --stop could not say it.

    The same replay for a session, end to end: the id --start printed is
    handed to --stop once the worker has marked the session crashed. The
    worker's logcat wrote its stderr to /dev/null, and the worker asked `adb
    get-state`, which answered `device`: --stop refused the framing, exit 1.
    The device error adb wrote as the stream ended is kept now, and raised
    again: exit 3, with its remedy and the way to read what was saved. With
    nothing on stderr the refusal stays exit 1, and says how adb exited, as
    --watch says it.
    """
    _cut_stream(toolchain, recorded, recorded_anywhere, adb_exit, said)
    store = SessionStore(base_dir=toolchain.home / "anr-sessions")

    session_id, stopped = _session_until_crashed(
        lambda *argv: toolchain.run("anr_watcher.py", *argv), store
    )

    naming = ("not attached",) if said == "device not found" else ("ended inside a record",)
    refusal = assert_refusal(stopped, json_mode=True, naming=naming, status=status)
    assert f"--get-details {session_id}" in refusal["remedy"], refusal
    if said == "device not found":
        assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal
        assert "as it ended, `adb logcat` said" in refusal["error"], refusal
    else:
        assert f"`adb logcat` exited {adb_exit}: nothing on stderr" in refusal["error"], refusal
    assert store.load_summary(session_id).stream_failure, "the summary was not saved"


# ---------------------------------------------------------------------------
# Codex review of PR #42: a session's stream that ends by itself, older summaries,
# and what adb said before it exited
# ---------------------------------------------------------------------------


def _session_until_crashed(run, store) -> tuple[str, subprocess.CompletedProcess]:
    """--start, wait until the worker has marked the session crashed, then --stop --json.

    The id handed to --stop is the one --start printed. ``run`` runs
    anr_watcher.py with the argv it is given.
    """
    started = run("--start", "--serial", "emulator-5554", "--json")
    assert started.returncode == 0, started.stdout + started.stderr
    session_id = json.loads(started.stdout)["session_id"]
    try:
        deadline = time.monotonic() + 30
        while store.load_meta(session_id).status != "crashed":
            if time.monotonic() > deadline:
                pytest.fail(f"the worker never marked the session crashed: {session_id}")
            time.sleep(0.05)
        stopped = run("--stop", session_id, "--json")
    finally:
        worker = store.load_meta(session_id).pid
        if worker:
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker, signal.SIGKILL)
    return session_id, stopped


def _logcat_spawns(toolchain) -> int:
    return len([call for call in toolchain.calls("adb") if "logcat" in call])


@pytest.mark.parametrize("adb_exit", [1, 255])
def test_a_session_whose_stream_ends_saying_the_device_went_refuses_with_it(
    adb_exit, toolchain, recorded, recorded_anywhere
):
    """Codex review of PR #42, REQUIRED 1 (P1): a stream that ENDS by itself, cleanly.

    The reviewer's replay through the real binary: every logcat the worker
    starts replays the recorded crash dump -- whole records, a clean end --
    and adb writes the recorded device-not-found text and exits 1 or 255.
    Only a framing failure was classified, so the worker restarted the
    stream until no restart was left, the session was marked crashed with no
    stream failure, and --stop answered with the summary: exit 0. adb's
    words now end the session as the error they name, at the first end:
    --stop refuses, exit 3, with its remedy, and the stream is not restarted.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(
                ("logcat",),
                stdout=recorded.data(f"{CRASH_DUMP}.bin"),
                stderr=recorded_anywhere("adb_device_not_found"),
                exit=adb_exit,
            ),
        ],
    )
    store = SessionStore(base_dir=toolchain.home / "anr-sessions")

    session_id, stopped = _session_until_crashed(
        lambda *argv: toolchain.run("anr_watcher.py", *argv, ANDROID_EMU_ANR_MAX_RESTARTS="1"),
        store,
    )

    refusal = assert_refusal(stopped, json_mode=True, naming=("not attached",), status=3)
    assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal
    assert f"--get-details {session_id}" in refusal["remedy"], refusal
    assert "ended by itself" in refusal["error"], refusal
    assert _logcat_spawns(toolchain) == 1, "a stream adb said had lost its device was restarted"


@pytest.mark.parametrize(
    ("device", "status", "spawns"), [("stays", 1, 2), ("goes", 3, 1)], ids=["stays", "goes"]
)
def test_a_session_whose_stream_keeps_ending_by_itself_is_never_a_quiet_success(
    device, status, spawns, toolchain, recorded, recorded_anywhere
):
    """REQUIRED 1's other half: adb ends the stream cleanly and says nothing.

    Each logcat replays the recorded crash dump and exits 0, silent. The
    worker asks `adb get-state` when a stream ends: answered `device` (the
    recorded answer) it restarts it, and once no restart is left (one,
    here) --stop refuses, exit 1, saying how adb ended it; answered with the
    recorded not-found refusal after the preflight, it restarts nothing and
    --stop refuses, exit 3. Both used to be an incomplete summary at exit 0.
    """
    get_state = [Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=device == "goes")]
    if device == "goes":
        get_state.append(
            Rule(("get-state",), stderr=recorded_anywhere("adb_device_not_found"), exit=1)
        )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *get_state,
            Rule(("logcat",), stdout=recorded.data(f"{CRASH_DUMP}.bin"), exit=0),
        ],
    )
    store = SessionStore(base_dir=toolchain.home / "anr-sessions")

    session_id, stopped = _session_until_crashed(
        lambda *argv: toolchain.run("anr_watcher.py", *argv, ANDROID_EMU_ANR_MAX_RESTARTS="1"),
        store,
    )

    naming = ("not attached",) if device == "goes" else ("ended by itself",)
    refusal = assert_refusal(stopped, json_mode=True, naming=naming, status=status)
    assert f"--get-details {session_id}" in refusal["remedy"], refusal
    if device == "stays":
        assert "`adb logcat` exited 0" in refusal["error"], refusal
    assert _logcat_spawns(toolchain) == spawns
    summary = store.load_summary(session_id)
    assert summary.incomplete and summary.stream_failure, "the summary was not saved"


def test_a_session_keeps_what_adb_said_before_it_exited(recorded, recorded_anywhere, tmp_path):
    """Codex review of PR #42, REQUIRED 3: adb's words are read whether or not it has exited.

    A stream that is not logd's records -- derived: the recorded crash dump's
    first record with only its header-size field set to 0, which logd never
    writes -- from an adb that has already written the recorded not-found
    text and has not exited (it holds until killed). The worker does not wait
    for an open stream's exit, and so read none of what it said: `adb
    get-state` answered `device`, and --stop refused the framing, exit 1.
    """
    raw = bytearray(frames(recorded.data(f"{CRASH_DUMP}.bin"))[0].raw)
    raw[2:4] = (0).to_bytes(2, "little")
    device = _LiveDevice(
        tmp_path,
        [
            _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
            _Answer(("get-state",), stdout=recorded.text("adb_get_state")),
            _Answer(
                ("logcat",),
                stdout=bytes(raw),
                stderr=recorded_anywhere("adb_device_not_found"),
                hold=True,
                stderr_first=True,
            ),
        ],
    )
    try:
        session_id, stopped = _session_until_crashed(device.run, device.store)
    finally:
        device.kill_workers()

    refusal = assert_refusal(stopped, json_mode=True, naming=("not attached",), status=3)
    assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal
    assert f"--get-details {session_id}" in refusal["remedy"], refusal
    assert "as it ended, `adb logcat` said" in refusal["error"], refusal


def _forget_drop_metadata(store, session_id: str) -> None:
    """Delete ``unnamed_dropped`` from a saved summary.json: the shape main b159fe0 saved.

    That version wrote no such field, so a summary it saved -- still
    fingerprint version 3, so compared -- reads as if nothing was dropped.
    """
    path = store.session_dir(session_id) / "summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "unnamed_dropped" in payload, "the summary no longer has the field this derives"
    del payload["unnamed_dropped"]
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("shape", ["absence", "maximum"])
def test_a_package_summary_saved_without_drop_metadata_shows_nothing_about_jank(
    shape, recorded, store, tmp_path, no_device
):
    """Codex review of PR #42, REQUIRED 2: an older summary has no ``unnamed_dropped``.

    Derived from the reviewer's sessions: B is the replay session B (or, for
    the maximum, the session that also read the second process's jank),
    stopped, then saved again with only ``unnamed_dropped`` deleted from its
    summary.json -- what main saved for it. Read as "nothing dropped", the
    replay said "improvement: 1 resolved" and the shared cluster "stable".
    What it dropped is unknown, so it shows neither, and the reason names the
    missing field.
    """
    start, jank = _launch(recorded, 0)
    _, other_jank = _launch(recorded, 1)
    buster = anr_watcher.AnrBuster(store=store)
    if shape == "absence":
        a = _package_session(store, [start, jank], recorded, tmp_path)
        b = _package_session(store, [_neutral(recorded), jank], recorded, tmp_path)
    else:
        a = _package_session(store, [_neutral(recorded), start, jank], recorded, tmp_path)
        b = _package_session(
            store, [_neutral(recorded), start, jank, other_jank], recorded, tmp_path
        )
    buster.stop(a)
    buster.stop(b)
    _forget_drop_metadata(store, b)

    result = json.loads(buster.diff(a, b, json_mode=True))

    said = f"{b} does not record what --package dropped"
    assert said in result["verdict"], result["verdict"]
    assert (result["resolved_clusters"], result["drift"], result["stable_count"]) == ([], [], 0)
    if shape == "absence":
        assert [c["absent_from"] for c in result["unconfirmed"]] == [b], result
    else:
        (shared,) = result["not_compared"]
        assert said in shared["reason"], shared
    assert "Dropped: not recorded" in buster.get_details(b)


def test_stopping_an_older_workers_package_session_records_drops_as_unknown(
    recorded, store, tmp_path, no_device
):
    """REQUIRED 2's other half: --stop of a session whose worker counted no drops.

    An earlier version's worker persisted counters without ``unnamed_dropped``
    -- derived: the replay session B's counters with only that key removed.
    --stop read that as zero; it is unknown now (``null``), and --diff shows
    no jank absence from it. A's worker, this version's, dropped nothing and
    says so: zero, not unknown.
    """
    start, jank = _launch(recorded, 0)
    buster = anr_watcher.AnrBuster(store=store)
    a = _package_session(store, [start, jank], recorded, tmp_path)
    b = _package_session(store, [_neutral(recorded), jank], recorded, tmp_path)
    counters = store.load_meta(b).extras["line_counters"]
    del counters["unnamed_dropped"]
    store.persist_worker_counters(b, counters)

    counted = json.loads(buster.stop(a, json_mode=True))
    payload = json.loads(buster.stop(b, json_mode=True))
    result = json.loads(buster.diff(a, b, json_mode=True))

    assert counted["unnamed_dropped"] == {"records": 0, "pids": []}, counted
    assert payload["unnamed_dropped"] is None, payload
    assert result["resolved_clusters"] == [], result["verdict"]
    assert [c["absent_from"] for c in result["unconfirmed"]] == [b], result


def test_an_unfiltered_summary_without_drop_metadata_still_compares(
    recorded, store, tmp_path, no_device
):
    """The control: a session without --package dropped nothing, recorded or not.

    The same two streams read without --package (its meta names no
    package), B's summary saved without ``unnamed_dropped``: B's jank is kept,
    keyed by pid, so nothing is unconfirmed for want of the field.
    """
    start, jank = _launch(recorded, 0)
    buster = anr_watcher.AnrBuster(store=store)
    a = _unfiltered_session(store, [start, jank], recorded, tmp_path)
    b = _unfiltered_session(store, [_neutral(recorded), jank], recorded, tmp_path)
    buster.stop(a)
    buster.stop(b)
    _forget_drop_metadata(store, b)

    result = json.loads(buster.diff(a, b, json_mode=True))

    assert "does not record what --package dropped" not in result["verdict"], result["verdict"]
    assert result["unconfirmed"] == [], result
    assert "Dropped:" not in buster.get_details(b)


def test_a_stream_whose_process_outlives_it_is_reaped_before_the_next_starts(
    recorded, tmp_path, monkeypatch
):
    """Codex review of PR #42, OPTIONAL: each stream is closed, and its process reaped, in turn.

    A logcat whose stdout reaches its end while the process lives on (the
    recorded history record, then EOF; alive until terminated). Every
    stream's stderr file stayed open for the whole session, and the next
    stream's process replaced this one's, which nothing then ended: only the
    last was terminated. Each is now ended before the next starts.
    """
    stream_file = tmp_path / "history.logcat"
    stream_file.write_bytes(framed([_history(recorded)]))
    events: list[tuple[str, int]] = []

    class _Lingering:
        def __init__(self, cmd, **_kwargs):
            self.index = len([e for e in events if e[0] == "spawn"])
            events.append(("spawn", self.index))
            # The worker closes it, once the stream has ended.
            self.stdout = open(stream_file, "rb", buffering=0)  # noqa: SIM115
            self.alive = True

        def poll(self):
            return None if self.alive else -signal.SIGTERM

        def wait(self, timeout=None):
            if self.alive:
                raise subprocess.TimeoutExpired("adb logcat", timeout)
            return -signal.SIGTERM

        def terminate(self):
            if self.alive:
                events.append(("terminate", self.index))
            self.alive = False

        def kill(self):
            self.terminate()

    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "1")
    monkeypatch.setattr(anr_watcher, "RESTART_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(anr_watcher, "STREAM_EXIT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    _logcat_through(monkeypatch, _Lingering)
    monkeypatch.setattr(anr_watcher, "_require_device", lambda _serial: None)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"serial": "emulator-5554"})

    anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)

    assert events == [("spawn", 0), ("terminate", 0), ("spawn", 1), ("terminate", 1)], events


# ---------------------------------------------------------------------------
# Second review of PR #42: a stop that arrives while the worker decides, long stderr
# ---------------------------------------------------------------------------


def _unfiltered_session(store, records: list[Frame], recorded, tmp_path) -> str:
    """A session without --package over ``records``: its meta says so, as --start writes it."""
    meta = store.create({"package": None})
    counters = _run_worker_over(
        store,
        meta.session_id,
        records,
        tmp_path,
        boundary=anr_watcher._CaptureBoundary(),
        process_names=_names(recorded),
    )
    store.persist_worker_counters(meta.session_id, counters)
    return meta.session_id


def _wait_for_marker(marker, what: str) -> None:
    deadline = time.monotonic() + 30
    while not marker.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"the worker never {what}")
        time.sleep(0.05)


@pytest.mark.parametrize(("device", "status"), [("goes", 3), ("stays", 0)], ids=["goes", "stays"])
def test_a_stop_while_the_worker_decides_how_its_stream_ended_waits_for_the_answer(
    device, status, recorded, recorded_anywhere, tmp_path
):
    """Second review of PR #42, REQUIRED 1 (P1): --stop answered before the worker had.

    The reviewer's replay through the real binary: the stream -- the recorded
    crash dump, whole, adb silent at exit 0 -- ends by itself, and the
    worker asks `adb get-state`, which takes 7 s to answer (the recorded
    not-found refusal, or the recorded `device`). --stop is run the moment
    that question is asked. It waited 5.5 s for the worker, saved the
    summary incomplete with no stream failure, and exited 0; the worker then
    persisted DeviceNotFoundError, which nothing read. --stop now waits for
    a worker deciding how its stream ended, bounded, and answers with what
    it decided: exit 3 for a device that went; for one still there, the
    stream was not restarted before the stop, so the summary says what it
    lacks.
    """
    asked = tmp_path / "get-state-asked"
    late = (
        _Answer(
            ("get-state",),
            stderr=recorded_anywhere("adb_device_not_found"),
            exit=1,
            delay=7,
            asked=str(asked),
        )
        if device == "goes"
        else _Answer(
            ("get-state",), stdout=recorded.text("adb_get_state"), delay=7, asked=str(asked)
        )
    )
    live = _LiveDevice(
        tmp_path,
        [
            _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
            _Answer(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
            late,
            _Answer(("logcat",), stdout=recorded.data(f"{CRASH_DUMP}.bin")),
        ],
    )
    started = live.run("--start", "--serial", "emulator-5554", "--json")
    assert started.returncode == 0, started.stdout + started.stderr
    session_id = json.loads(started.stdout)["session_id"]
    try:
        _wait_for_marker(asked, "asked adb get-state after its stream ended")
        stopped = live.run("--stop", session_id, "--json")
    finally:
        live.kill_workers()

    if device == "goes":
        refusal = assert_refusal(stopped, json_mode=True, naming=("not attached",), status=3)
        assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal
    else:
        assert stopped.returncode == status, stopped.stdout + stopped.stderr
        payload = json.loads(stopped.stdout)
        assert payload["incomplete"] is True, payload
        assert "not restarted" in payload["incomplete_reason"], payload
        assert "still running" not in stopped.stderr, stopped.stderr
    assert live.store.load_summary(session_id) is not None, "the summary was not saved"


def _long_stderr(recorded_anywhere, kept: int) -> str:
    """The recorded not-found text, then recorded adb chatter past ``kept`` times what is kept.

    Derived: adb_device_not_found's line, followed by adb_waiting_for_device's
    "- waiting for device -" line (recorded adb stderr, 23 bytes) repeated
    until the whole is past ``kept`` times ``STDERR_KEPT_BYTES``. Once past is
    the reviewer's 65,802-byte shape; twice past is read as a head and a tail,
    not whole. adb's error is in its first line, far from its last.
    """
    chatter = recorded_anywhere("adb_waiting_for_device")
    said = recorded_anywhere("adb_device_not_found")
    repeats = kept * anr_watcher.STDERR_KEPT_BYTES // len(chatter) + 10
    return said + chatter * repeats


@pytest.mark.parametrize("kept", [1, 2], ids=["past-a-tail", "past-head-and-tail"])
@pytest.mark.parametrize("mode", ["watch", "session"])
def test_a_device_error_adb_wrote_first_survives_a_long_stderr(
    mode, kept, toolchain, recorded, recorded_anywhere
):
    """Second review of PR #42, REQUIRED 2: only the last 64 KiB of stderr was read.

    The recorded crash dump, whole, then adb exits 255 having written the
    recorded not-found text and then over 64 KiB (or 128 KiB) of recorded
    chatter (``_long_stderr``). Its error was cut off with the head: `adb
    get-state` answered `device`, and the read was refused for how adb exited
    -- exit 1 where it is 3. What adb said first is kept as well as what it
    said last.
    """
    long_said = _long_stderr(recorded_anywhere, kept)
    assert len(long_said.encode()) > kept * anr_watcher.STDERR_KEPT_BYTES
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(
                ("logcat",),
                stdout=recorded.data(f"{CRASH_DUMP}.bin"),
                stderr=long_said,
                exit=255,
            ),
        ],
    )

    if mode == "watch":
        result = toolchain.run("anr_watcher.py", "--watch", "--duration", "30", "--json")
        refusal = assert_refusal(
            result, json_mode=True, naming=("not attached",), status=3, streaming=True
        )
    else:
        store = SessionStore(base_dir=toolchain.home / "anr-sessions")
        _session_id, result = _session_until_crashed(
            lambda *argv: toolchain.run("anr_watcher.py", *argv, ANDROID_EMU_ANR_MAX_RESTARTS="0"),
            store,
        )
        refusal = assert_refusal(result, json_mode=True, naming=("not attached",), status=3)
    assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal
    assert len(refusal["error"]) < 2000, "the refusal printed adb's chatter whole"


def test_a_watch_of_bytes_that_are_not_records_keeps_the_device_error_adb_wrote(
    recorded, recorded_anywhere, tmp_path
):
    """Second review of PR #42, OPTIONAL: --watch parity with the session worker.

    The same derived stream as the worker's test (the recorded record with
    only its header-size field set to 0) from an adb that has already
    written the recorded not-found text and has not exited. --watch refused
    the framing at once, exit 1, without reading what adb had said.
    """
    raw = bytearray(frames(recorded.data(f"{CRASH_DUMP}.bin"))[0].raw)
    raw[2:4] = (0).to_bytes(2, "little")
    live = _LiveDevice(
        tmp_path,
        [
            _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
            _Answer(("get-state",), stdout=recorded.text("adb_get_state")),
            _Answer(
                ("logcat",),
                stdout=bytes(raw),
                stderr=recorded_anywhere("adb_device_not_found"),
                hold=True,
                stderr_first=True,
            ),
        ],
    )

    result = live.run("--watch", "--duration", "30", "--json")

    refusal = assert_refusal(
        result, json_mode=True, naming=("not attached",), status=3, streaming=True
    )
    assert refusal["remedy"] == NOT_FOUND_REMEDY, refusal


def test_a_summary_whose_meta_names_no_package_field_reads_drops_as_unknown(
    recorded, store, tmp_path, no_device
):
    """Second review of PR #42, OPTIONAL: a meta without ``package`` at all proves nothing.

    --start always writes ``package`` (None without --package). A meta
    without the key -- derived: the replay session B's meta with only that
    key removed -- did not say the session was unfiltered, and its missing
    drop count was read as zero.
    """
    start, jank = _launch(recorded, 0)
    buster = anr_watcher.AnrBuster(store=store)
    a = _package_session(store, [start, jank], recorded, tmp_path)
    b = _package_session(store, [_neutral(recorded), jank], recorded, tmp_path)
    buster.stop(a)
    buster.stop(b)
    _forget_drop_metadata(store, b)
    path = store.session_dir(b) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    del meta["args"]["package"]
    path.write_text(json.dumps(meta), encoding="utf-8")

    result = json.loads(buster.diff(a, b, json_mode=True))

    assert result["resolved_clusters"] == [], result["verdict"]
    assert f"{b} does not record what --package dropped" in result["verdict"], result


# ---------------------------------------------------------------------------
# --start and a worker that registers and ends before it looks (CI on PR #42)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "status", "naming"),
    [
        ("device not found", 3, "not attached"),
        ("nothing", 1, "ended by itself"),
        ("unsaid", 1, "without saying why"),
    ],
    ids=["device-gone", "silent", "unsaid"],
)
def test_start_answers_for_a_worker_that_registered_and_ended_before_it_looked(
    said, status, naming, recorded, recorded_anywhere, store, tmp_path, monkeypatch, capsys
):
    """The --start registration race, replayed deterministically: the CI flake on PR #42.

    --start polls the session every 50 ms for its worker, and accepted only
    one still running. A worker that claimed the session and was marked
    crashed between two polls read as one that never started: "Worker did not
    register within 3s", exit 1. Here the real worker runs to its end in place
    of the spawn, before --start looks once: its stream is the recorded crash
    dump, whole, after which adb exits 255 having written the recorded
    device-not-found text, or nothing (no restart allowed). --start answers
    with what ended it -- exit 3 for the device gone, 1 otherwise -- through
    the CLI boundary, and the summary is saved. A worker that claimed and was
    marked crashed with no failure persisted ("unsaid": no worker this version
    runs ends so, but one that died outright would) is refused for that, exit 1.
    """
    from common import adb_exec

    stream_file = tmp_path / "crash.logcat"
    stream_file.write_bytes(recorded.data(f"{CRASH_DUMP}.bin"))
    real_popen = subprocess.Popen
    streams: list = []

    class _Logcat:
        """The recorded dump, whole; adb writes what it said to its stderr and exits 255."""

        def __init__(self, _cmd, **kwargs):
            self.stdout = open(stream_file, "rb", buffering=0)  # noqa: SIM115
            streams.append(self.stdout)
            if said == "device not found":
                kwargs["stderr"].write(recorded_anywhere("adb_device_not_found").encode())
                kwargs["stderr"].flush()

        def poll(self):
            return 255

        def wait(self, timeout=None):
            return 255

        def terminate(self):
            return None

        def kill(self):
            return None

    def popen(cmd, *args, **kwargs):
        if cmd[0] == "ps":
            return real_popen(cmd, *args, **kwargs)
        if "logcat" in cmd:
            return _Logcat(cmd, **kwargs)
        if "--worker-session-id" in cmd:
            # The real worker, run to its end before --start polls once.
            session_id = cmd[cmd.index("--worker-session-id") + 1]
            token = cmd[cmd.index(anr_watcher.WORKER_TOKEN_FLAG) + 1]
            if said == "unsaid":
                store.claim_worker(session_id, pid=os.getpid())
                store.mark_crashed(session_id)
            else:
                anr_watcher.AnrBuster().run_worker(session_id, token=token)
            return None
        raise AssertionError(f"the test ran {cmd}")

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", popen)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])
    # The device is attached and answers `device` when asked.
    monkeypatch.setattr(anr_watcher, "resolve_device_identifier", lambda serial: serial)
    monkeypatch.setattr(anr_watcher, "_require_device", lambda _serial: None)
    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "0")
    monkeypatch.setattr(
        anr_watcher.sys,
        "argv",
        ["anr_watcher.py", "--start", "--serial", "emulator-5554", "--json"],
    )

    try:
        with pytest.raises(SystemExit) as exc:
            cli.run(anr_watcher.main)
    finally:
        for stream in streams:
            stream.close()

    assert exc.value.code == status
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["success"] is False and naming in refusal["error"], refusal
    assert "did not register" not in refusal["error"], refusal
    (session,) = store.list_sessions()
    assert f"--get-details {session.session_id}" in refusal["remedy"], refusal
    if said == "device not found":
        assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal
    summary = store.load_summary(session.session_id)
    assert summary is not None and summary.incomplete, "the summary was not saved"
    assert bool(summary.stream_failure) == (said != "unsaid"), summary.stream_failure


# ---------------------------------------------------------------------------
# Third review of PR #42: a stop during a decision, a worker --stop cannot tell,
# an unforeseen worker error, --watch's grace, and a repeated --stop
# ---------------------------------------------------------------------------


def _crash_dump_logcat(tmp_path, recorded, recorded_anywhere, said: str):
    """A logcat stand-in replaying the recorded crash dump, whole; adb exits 255 having said ``said``.

    ``said`` is "device not found" (the recorded text) or "nothing". Returns
    the stand-in's class and the list of stdouts it opened, for the test to close.
    """
    stream_file = tmp_path / "crash.logcat"
    stream_file.write_bytes(recorded.data(f"{CRASH_DUMP}.bin"))
    opened: list = []

    class _Logcat:
        def __init__(self, _cmd, **kwargs):
            self.stdout = open(stream_file, "rb", buffering=0)  # noqa: SIM115
            opened.append(self.stdout)
            if said == "device not found":
                kwargs["stderr"].write(recorded_anywhere("adb_device_not_found").encode())
                kwargs["stderr"].flush()

        def poll(self):
            return 255

        def wait(self, timeout=None):
            return 255

        def terminate(self):
            return None

        def kill(self):
            return None

    return _Logcat, opened


def _stop_through_the_cli(monkeypatch, capsys, session_id: str) -> tuple[int, dict]:
    """`anr_watcher.py --stop <id> --json` through main and the CLI boundary: (status, JSON)."""
    monkeypatch.setattr(anr_watcher.sys, "argv", ["anr_watcher.py", "--stop", session_id, "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.run(anr_watcher.main)
    return exc.value.code, json.loads(capsys.readouterr().out)


def test_a_stop_that_comes_once_the_decision_has_begun_does_not_skip_it(
    recorded, recorded_anywhere, store, tmp_path, monkeypatch, capsys
):
    """Third review of PR #42, REQUIRED 1 (P1): the decision read the stop flag twice.

    The stream -- the recorded crash dump, whole; adb exits 255 having written
    the recorded not-found text -- ends by itself, and the worker says in its
    meta that it is deciding how. --stop's SIGTERM arrives right then: the
    worker's own handler is called as that meta write returns. The second
    read of the stop flag skipped the decision: worker exit 0, --stop exit 0,
    no stream failure, the summary complete. A decision once begun is
    finished: --stop answers with the device error, exit 3.

    Replayed in process, with the real worker and --stop's CLI boundary: the
    window is between two statements of the worker, which no separate
    process can be made to hit every time.
    """
    from common import adb_exec

    handlers: dict = {}
    monkeypatch.setattr(
        anr_watcher.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler)
    )
    logcat_class, opened = _crash_dump_logcat(
        tmp_path, recorded, recorded_anywhere, "device not found"
    )
    _logcat_through(monkeypatch, logcat_class)
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])
    monkeypatch.setattr(anr_watcher, "_require_device", lambda _serial: None)
    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "0")
    persist = store.persist_worker_counters

    def persist_then_sigterm(session_id, counters):
        persist(session_id, counters)
        if counters.get("deciding"):
            # --stop's SIGTERM, just after the meta says the worker is deciding.
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(store, "persist_worker_counters", persist_then_sigterm)
    meta = store.create({"serial": "emulator-5554", "package": None})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for stdout in opened:
            stdout.close()

    status, refusal = _stop_through_the_cli(monkeypatch, capsys, meta.session_id)

    assert status == 3, refusal
    assert refusal["success"] is False and "not attached" in refusal["error"], refusal
    assert refusal["remedy"].startswith(NOT_FOUND_REMEDY), refusal


def test_a_stop_that_cannot_tell_the_worker_from_another_process_refuses(
    store, monkeypatch, capsys
):
    """Third review of PR #42, REQUIRED 2 (P1): not delivered was read as "the worker ended".

    The session's pid is a live process carrying the session's worker token
    -- it may be the worker -- and `ps` cannot be run, so it can be shown to
    be the worker no more than shown not to be. signal_worker kept the pid
    and sent nothing; --stop then took the worker for finished, saved a
    summary at exit 0 and forgot the pid, while the worker went on. --stop
    now refuses, exit 1, naming the pid and why; nothing is signalled, no
    summary is saved, and the pid is kept for the next --stop.
    """
    from common import anr_sessions

    meta = store.create({})
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
            anr_watcher.WORKER_TOKEN_FLAG,
            meta.worker_token,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    real_kill = os.kill
    try:
        assert holder.stdout.readline().strip() == "ready"
        _hold_pid(store, meta.session_id, holder.pid, "running")

        def no_ps(*_args, **_kwargs):
            raise FileNotFoundError("ps")

        monkeypatch.setattr(anr_sessions, "_ps", no_ps)
        signalled: list[int] = []

        def kill(pid, sig):
            if pid == holder.pid and sig != 0:
                signalled.append(sig)
                return
            real_kill(pid, sig)

        monkeypatch.setattr(anr_sessions.os, "kill", kill)

        status, refusal = _stop_through_the_cli(monkeypatch, capsys, meta.session_id)

        alive = holder.poll() is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            real_kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=30)
        holder.stdout.close()

    assert status == 1, refusal
    assert str(holder.pid) in refusal["error"] and "could not say" in refusal["error"], refusal
    assert f"--stop {meta.session_id} again" in refusal["remedy"], refusal
    assert (signalled, alive) == ([], True)
    assert store.load_meta(meta.session_id).pid == holder.pid, "the pid was forgotten"
    assert store.load_summary(meta.session_id) is None, "a summary was saved as the answer"


def test_a_worker_that_meets_an_unforeseen_error_ends_the_session_as_a_failure(
    recorded, recorded_anywhere, store, tmp_path, monkeypatch, capsys
):
    """Third review of PR #42, REQUIRED 3: an exception left the session `running`, unsaid.

    Any error nothing foresaw -- here one raised in place of reading the
    stream -- propagated out of the worker with its counters persisted and
    the session still `running`: --start saw a worker at work, and --stop a
    worker that had ended without being stopped, at exit 0. It is persisted
    as the session's stream failure and the session marked crashed; --stop
    refuses with it, exit 1. The error still reaches the worker's caller.
    """
    logcat_class, opened = _crash_dump_logcat(tmp_path, recorded, recorded_anywhere, "nothing")

    def unforeseen(self, **_kwargs):
        raise RuntimeError("an error nothing foresaw")

    monkeypatch.setattr(anr_watcher.AnrBuster, "_read_stream_into_events", unforeseen)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    _logcat_through(monkeypatch, logcat_class)
    meta = store.create({"serial": "emulator-5554", "package": None})
    try:
        with pytest.raises(RuntimeError):
            anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for stdout in opened:
            stdout.close()

    after = store.load_meta(meta.session_id)
    assert after.status == "crashed", after.status
    failure = after.extras["line_counters"]["stream_failure"]
    assert "RuntimeError: an error nothing foresaw" in failure["error"], failure
    status, refusal = _stop_through_the_cli(monkeypatch, capsys, meta.session_id)
    assert status == 1, refusal
    assert "abandoned by its worker" in refusal["error"], refusal


def test_a_watch_gives_adb_the_workers_grace_before_reading_what_it_said(
    recorded, recorded_anywhere, tmp_path
):
    """Third review of PR #42, OPTIONAL: --watch read adb's stderr at once, the worker after 0.5 s.

    The same derived stream as the worker's (the recorded record with only
    its header-size field set to 0), from an adb that writes the recorded
    not-found text 0.1 s later and then holds. The worker waits for adb up to
    0.5 s before reading what it said: exit 3. --watch read at once: exit 1.
    """
    raw = bytearray(frames(recorded.data(f"{CRASH_DUMP}.bin"))[0].raw)
    raw[2:4] = (0).to_bytes(2, "little")
    live = _LiveDevice(
        tmp_path,
        [
            _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
            _Answer(("get-state",), stdout=recorded.text("adb_get_state")),
            _Answer(
                ("logcat",),
                stdout=bytes(raw),
                stderr=recorded_anywhere("adb_device_not_found"),
                stderr_after=0.1,
                hold=True,
            ),
        ],
    )

    result = live.run("--watch", "--duration", "30", "--json")

    refusal = assert_refusal(
        result, json_mode=True, naming=("not attached",), status=3, streaming=True
    )
    assert refusal["remedy"] == NOT_FOUND_REMEDY, refusal


def test_a_repeated_stop_keeps_what_the_first_said_was_missing(recorded, tmp_path, monkeypatch):
    """Third review of PR #42, OPTIONAL: a second --stop turned incomplete into complete.

    The first --stop signals the worker, which is still running when its wait
    ends: the summary is saved incomplete. By the second, the worker has
    exited (nothing to signal), and the summary was rebuilt complete -- as if
    what it may not have written had come back.
    """
    store, session_id = _seed_session(recorded, tmp_path, [80])
    buster = anr_watcher.AnrBuster(store=store)
    delivered = iter([True, False])
    monkeypatch.setattr(store, "signal_worker", lambda *_a, **_k: next(delivered))
    monkeypatch.setattr(buster, "_wait_for_worker_exit", lambda *_a, **_k: False)

    first = json.loads(buster.stop(session_id, json_mode=True))
    second = json.loads(buster.stop(session_id, json_mode=True))

    assert first["incomplete"] is True, first
    assert (second["incomplete"], second["incomplete_reason"]) == (
        True,
        first["incomplete_reason"],
    ), second
