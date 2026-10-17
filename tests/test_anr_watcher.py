"""Device-free tests for anr_watcher's command construction and session flow.

All adb/subprocess interaction is mocked. Session storage is redirected to
``tmp_path`` via ``SessionStore(base_dir=...)`` so nothing touches the real home.

Every logcat line fed to the parser here is a RECORDED one -- see
``tests/fixtures/recorded/`` and the module docstring of ``test_anr_pipeline``.
The lines these tests used to build by hand claimed ActivityManager writes
``ANR in com.example.app (com.example.app/.Main)``; a real broadcast ANR
carries no parenthesised component at all, so the ``--package`` filter was only
ever exercised against a ``component`` field the device does not populate.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

import anr_watcher
import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal

from common import cli, env_config
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
    # threadtime format is mandatory for the parse layer.
    assert cmd[cmd.index("-v") + 1] == "threadtime"


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
    recorded Choreographer line at 5 and 80 frames, then the recorded broadcast
    ANR, whose four lines are ONE ActivityManager record (D5). The lines used
    to be typed here -- ``ANR in com.x (com.x/.A)``, a component a broadcast ANR
    does not carry -- and fed through a fake ``readline`` that the worker no
    longer calls (N16).
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=1)
    buster = anr_watcher.AnrBuster(store=store)

    jank = _recorded_jank_burst(recorded, (5, 80))
    anr = recorded.lines("logcat_anr_broadcast")
    read_end, write_end = os.pipe()
    os.write(write_end, "".join(line + "\n" for line in [*jank, *anr]).encode())
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
            )
    finally:
        proc.stdout.close()

    assert counters["total"] == len(jank) + len(anr)
    assert counters["matched"] == 3, "two jank lines and one ANR record"
    assert counters["dropped"] == 1
    events = store.read_events(meta.session_id)
    # 80-frame jank + ANR survive; 5-frame jank dropped.
    assert sorted(e.kind for e in events) == ["anr", "jank"]
    (anr_event,) = [e for e in events if e.kind == "anr"]
    stated_pid = int(next(line for line in anr if "PID: " in line).split("PID: ")[1])
    assert anr_event.app_pid == stated_pid, "the record's PID: line was not attached"
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


def test_n16_a_stop_while_the_stream_is_open_keeps_every_line_already_delivered(
    recorded, tmp_path, monkeypatch
):
    """N16: the worker select()ed the fd, then readline()d the file object.

    readline() reads ahead into Python's buffer, so a burst arrives as one line
    returned, the rest parked in that buffer, and the fd empty. select() then
    reports nothing to read, and the parked lines wait for the NEXT burst --
    or, when --stop comes first, are never read at all. Reviewer-reproduced:
    four lines written, one kept.

    Deterministic, no sleeps. The stream has finished its one write before the
    reader starts (the writer says so on stderr), and the stop is set at the
    moment the reader's own select() first reports the stream idle: the point
    where it believes it has read everything. Anything not processed by then
    is lost, which is exactly the defect.
    """
    burst = _recorded_jank_burst(recorded, (31, 45, 52, 120))
    burst_file = tmp_path / "burst.txt"
    burst_file.write_text("".join(line + "\n" for line in burst), encoding="utf-8")

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

    # Spawned the way the worker spawned logcat when this was found.
    proc = subprocess.Popen(
        [sys.executable, "-c", _QUIET_STREAM, str(burst_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        if proc.stderr.readline().strip() != "written":
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

    assert counters["total"] == len(burst), (
        f"{len(burst)} lines were delivered before the stop and " f"{counters['total']} were read"
    )
    assert len(store.read_events(meta.session_id)) == len(burst)


# ---------------------------------------------------------------------------
# N14: ANDROID_EMU_ANR_MAX_RESTARTS=0 means no restarts
# ---------------------------------------------------------------------------


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

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _StreamThatEnds)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"serial": "emulator-5554"})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for pipe in pipes:
            pipe.close()

    assert len(spawned) == 1, f"MAX_RESTARTS=0 spawned logcat {len(spawned)} times"
    assert store.load_meta(meta.session_id).status == "crashed"


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
    """D5 end to end, over everything a live reader sees of one broadcast ANR.

    ``--watch`` here reads a stream that ends, exit 0 -- a stream adb ended
    cleanly is not a failure, so this also pins that X6's check is on the
    status, not on the stream merely ending.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("logcat",), stdout=recorded.text("logcat_anr_broadcast_stream")),
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
            Rule(("logcat",), stdout=recorded.text("logcat_anr_input_stream")),
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


def test_worker_writes_an_anr_record_when_the_stream_goes_quiet(recorded, tmp_path, monkeypatch):
    """An ANR record is complete only when something else follows it.

    On a live stream that may be nothing at all for a while, so the worker
    treats a quiet stream as the end of the record and writes the event then --
    not at the next unrelated line, and not only at --stop. Checked before any
    stop is asked for: the reader's SECOND quiet select() comes after its first
    quiet pass, which is where the record is written.
    """
    burst_file = tmp_path / "burst.txt"
    burst_file.write_text(recorded.text("logcat_anr_broadcast"), encoding="utf-8")
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
    """A logcat whose stdout is a file of recorded lines: readable, then EOF.

    A file rather than a pipe so a recording larger than a pipe's buffer can be
    handed over whole before the reader starts.
    """

    def __init__(self, path):
        self.stdout = open(path, "rb", buffering=0)  # noqa: SIM115 -- closed by _run_worker_over

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _run_worker_over(store, session_id, lines, tmp_path, **kwargs) -> dict:
    stream = tmp_path / f"{session_id}.logcat"
    stream.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
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
        raise AssertionError(f"the worker asked the device: adb {args}")

    monkeypatch.setattr(anr_watcher, "run_adb", _refuse)
    return asked


def test_worker_names_a_restarted_apps_jank_from_the_stream(recorded, tmp_path, no_device):
    """logcat_app_restart: one app, three processes, each janking once.

    ActivityManager's ``Start proc <pid>:<name>/`` lines in the same stream
    name every one of them, so the session holds ONE jank site -- and the
    device is never asked.
    """
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(store, meta.session_id, recorded.lines("logcat_app_restart"), tmp_path)

    jank = [
        c for c in store.build_summary(meta.session_id).clusters if c.sample_event.kind == "jank"
    ]
    assert [(c.count, c.identity) for c in jank] == [(3, "package")], jank
    assert no_device == []


def test_package_keeps_the_packages_own_jank_in_a_session(recorded, tmp_path, no_device):
    """``--package`` used to drop every jank line: none names a package."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"package": "com.example.composefixture"})

    _run_worker_over(
        store,
        meta.session_id,
        recorded.lines("logcat_app_restart"),
        tmp_path,
        package="com.example.composefixture",
    )

    assert [e.kind for e in store.read_events(meta.session_id)].count("jank") == 3


def _age_answer(recorded, fixture: str, pid: int, exit_status: int):
    """The recorded ``ps -o PID,NAME,ELAPSED`` answer, with its row's pid set to ``pid``."""
    from common.adb_exec import AdbResult

    lines = recorded.text(fixture).splitlines(keepends=True)
    rows = [lines[0], *(re.sub(r"^(\s*)\d+", rf"\g<1>{pid}", line) for line in lines[1:])]
    return AdbResult(returncode=exit_status, stdout="".join(rows), stderr="")


def _ps_call(pid: int) -> tuple[tuple, dict]:
    """The worker's one device call per pid, as run_adb receives it -- every argument."""
    return (
        ("shell", "emulator-5554", "ps", "-o", "PID,NAME,ELAPSED", "-p", str(pid)),
        {"timeout": anr_watcher.PS_TIMEOUT},
    )


class _Clock:
    """A monotonic clock the test reads out by hand: one reading per call."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0)


@pytest.mark.parametrize(
    ("fixture", "exit_status", "read_before_lookup", "named"),
    [
        ("ps_lifetime", 0, 60.0, "com.android.systemui"),
        ("ps_lifetime_just_started", 0, 30.0, None),
        ("ps_lifetime_just_started", 0, 0.5, "com.example.composefixture"),
        ("ps_lifetime_not_running", 1, 0.5, None),
    ],
    ids=[
        "owner-hours-old-line-a-minute-old",
        "owner-seconds-old-line-older",
        "owner-seconds-old-line-just-read",
        "pid-held-by-nothing",
    ],
)
def test_a_pid_is_named_only_if_its_owner_is_older_than_the_line(
    fixture, exit_status, read_before_lookup, named, recorded, monkeypatch
):
    """A pid is recycled; its CURRENT owner is not evidence about an older line.

    Review of PR #26: the owner ps reported was trusted as the process that
    logged the jank, so a recycled pid gave the new owner's name. The name now
    counts only if the owner's AGE covers the time since the line was read --
    durations on the host's monotonic clock, injected here: the lookup starts at
    1000.0 and returns at 1000.2. ``ps_lifetime_just_started``'s owner is a
    few seconds old: too young for a line read 30 s earlier, old enough for one
    read half a second earlier.
    """
    pid = 6113
    asked: list[tuple[tuple, dict]] = []

    def _device(*args, **kwargs):
        asked.append((args, kwargs))
        return _age_answer(recorded, fixture, pid, exit_status)

    monkeypatch.setattr(anr_watcher, "run_adb", _device)
    names = anr_watcher._ProcessNames("emulator-5554", clock=_Clock(1000.0, 1000.2))

    assert names.name_of(pid, read_at=1000.0 - read_before_lookup) == named
    assert asked == [_ps_call(pid)]


def test_a_slow_lookup_widens_the_slack_by_its_own_latency(recorded, monkeypatch):
    """ps measured the age somewhere inside the call; the call's length is slack.

    Here the call takes 10 s (1000.0 -> 1010.0), and the owner is a few
    seconds old by the end of it: it may have existed when the line was read,
    0.5 s before the call began, so it is not refused.
    """
    pid = 6113
    monkeypatch.setattr(
        anr_watcher,
        "run_adb",
        lambda *_a, **_k: _age_answer(recorded, "ps_lifetime_just_started", pid, 0),
    )
    names = anr_watcher._ProcessNames("emulator-5554", clock=_Clock(1000.0, 1010.0))

    assert names.name_of(pid, read_at=999.5) == "com.example.composefixture"


def test_one_lookup_judges_every_later_line_of_that_pid(recorded, monkeypatch):
    """The owner is looked up once; each line is still held against its age."""
    pid = 6113
    asked: list[tuple[tuple, dict]] = []

    def _device(*args, **kwargs):
        asked.append((args, kwargs))
        return _age_answer(recorded, "ps_lifetime_just_started", pid, 0)

    monkeypatch.setattr(anr_watcher, "run_adb", _device)
    names = anr_watcher._ProcessNames("emulator-5554", clock=_Clock(1000.0, 1000.2))

    assert names.name_of(pid, read_at=970.0) is None
    assert names.name_of(pid, read_at=999.9) == "com.example.composefixture"
    assert asked == [_ps_call(pid)]
    assert names.unnamed == 1


def test_worker_names_the_janking_process_with_one_full_lookup(recorded, tmp_path, monkeypatch):
    """The worker's wiring: every argument of the call, once for two lines of one pid."""
    jank = _recorded_jank_burst(recorded, (40, 80))
    pid = int(jank[0].split()[2])
    asked: list[tuple[tuple, dict]] = []

    def _device(*args, **kwargs):
        asked.append((args, kwargs))
        return _age_answer(recorded, "ps_lifetime", pid, 0)

    monkeypatch.setattr(anr_watcher, "run_adb", _device)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    _run_worker_over(
        store,
        meta.session_id,
        jank,
        tmp_path,
        process_names=anr_watcher._ProcessNames("emulator-5554"),
    )

    assert asked == [_ps_call(pid)]
    events = store.read_events(meta.session_id)
    assert {(e.app, e.identity) for e in events} == {("com.android.systemui", "package")}


def test_worker_keeps_reading_when_the_device_cannot_name_a_process(
    recorded, tmp_path, monkeypatch, recorded_anywhere
):
    """A lookup that fails leaves the jank keyed by pid; it does not end the session."""
    from common.adb_exec import DeviceNotFoundError

    def _gone(*args, **kwargs):
        raise DeviceNotFoundError(recorded_anywhere("adb_shell_device_not_found").strip())

    monkeypatch.setattr(anr_watcher, "run_adb", _gone)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})

    counters = _run_worker_over(
        store,
        meta.session_id,
        _recorded_jank_burst(recorded, (40, 80)),
        tmp_path,
        process_names=anr_watcher._ProcessNames("emulator-5554"),
    )

    assert [e.identity for e in store.read_events(meta.session_id)] == ["pid", "pid"]
    assert counters["unnamed_pids"] == 1


# ---------------------------------------------------------------------------
# Review of PR #26: --stop persists the summary only once the worker is done
# ---------------------------------------------------------------------------

# An adb that replays recorded text, with the two behaviours this needs: its
# logcat stays open after replaying (a live stream gone quiet -- the worker
# ends it), and its ps is SLOW, saying on a marker file when it was asked.
_SLOW_DEVICE = """#!{python}
import json, sys, time
with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]

def replay(name):
    with open(spec[name], encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
    sys.stdout.flush()

if "devices" in argv:
    replay("devices")
    sys.exit(0)
if "logcat" in argv:
    replay("logcat")
    time.sleep(120)
    sys.exit(0)
if "ps" in argv:
    with open(spec["asked"], "a", encoding="utf-8") as asked:
        asked.write(argv[-1] + "\\n")
    time.sleep(spec["delay"])
    replay("ps")
    sys.exit(0)
sys.exit(1)
"""


def test_stop_waits_for_a_worker_still_naming_a_process(recorded, tmp_path):
    """Review of PR #26: the summary was saved while the worker was mid-lookup.

    ``--stop`` gave the worker 2 s; a `ps` lookup could take 10. The summary was
    persisted with zero events, and the worker then wrote its jank and the
    held ANR after it -- a silently empty summary. Here the device's ps takes
    2.5 s (slower than the old wait, inside the new 3 s lookup bound) and
    --stop is asked the moment ps is. Two processes jank; the second is not
    looked up at all, because the worker stops asking once told to stop.
    """
    toolchain = FakeToolchain(tmp_path / "toolchain")
    toolchain.assert_isolated()
    jank = _recorded_jank_burst(recorded, (80,))[0]
    pid = int(jank.split()[2])
    other = re.sub(r"^(\S+ \S+\s+)\d+(\s+)\d+", rf"\g<1>{pid + 1}\g<2>{pid + 1}", jank)
    staged = {
        "devices": recorded.text("adb_devices_single"),
        "logcat": "\n".join([jank, other, *recorded.lines("logcat_anr_broadcast")]) + "\n",
        "ps": _age_answer(recorded, "ps_lifetime", pid, 0).stdout,
    }
    spec = {"asked": str(tmp_path / "ps-asked"), "delay": 2.5}
    for name, text in staged.items():
        path = tmp_path / f"{name}.txt"
        path.write_text(text, encoding="utf-8")
        spec[name] = str(path)
    spec_path = tmp_path / "slow-device.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    adb = toolchain.bin / "adb"
    adb.write_text(_SLOW_DEVICE.format(python=sys.executable, spec=str(spec_path)))
    adb.chmod(0o755)

    started = toolchain.run("anr_watcher.py", "--start", "--serial", "emulator-5554")
    session_id = started.stdout.strip()
    assert started.returncode == 0, started.stderr
    store = SessionStore(base_dir=toolchain.home / "anr-sessions")
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ps-asked").exists():
            if time.monotonic() > deadline:
                pytest.fail("the worker never asked the device to name the janking pid")
            time.sleep(0.05)

        stopped = toolchain.run("anr_watcher.py", "--stop", session_id, "--json")
    finally:
        worker = store.load_meta(session_id).pid
        if worker:
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker, signal.SIGKILL)

    assert stopped.returncode == 0, stopped.stderr
    payload = json.loads(stopped.stdout)
    assert payload["event_count"] == 3, payload
    assert payload["incomplete"] is False
    assert sorted(c["sample_event"]["kind"] for c in payload["clusters"]) == [
        "anr",
        "jank",
        "jank",
    ]
    asked = (tmp_path / "ps-asked").read_text(encoding="utf-8").split()
    assert asked == [str(pid)], f"looked up after the stop: {asked}"


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
            Rule(("logcat",), stdout=recorded.text("logcat_anr_broadcast")),
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


def test_a_stream_restart_forgets_who_held_each_pid(recorded, tmp_path, monkeypatch):
    """Review of PR #26: a pid's owner was cached across a stream restart.

    The first stream names pid P from its own ``Start proc`` line (recorded:
    logcat_app_restart). The stream dies; while the worker backs off, P gets a
    new owner, whose ``Start proc`` falls in the gap -- ``-T 1`` does not
    replay it. The second stream's jank from P must be looked up afresh, once,
    and named for the new owner (the recorded ``ps`` row, pid set to P), not
    for the one that was cached.
    """
    lines = recorded.lines("logcat_app_restart")
    start_proc = next(line for line in lines if "Start proc 14407:" in line)
    jank = next(line for line in lines if "Choreographer: Skipped" in line)
    pid = int(jank.split()[2])
    streams = [[start_proc, jank], [jank]]
    for index, stream in enumerate(streams):
        (tmp_path / f"stream-{index}.logcat").write_text(
            "".join(line + "\n" for line in stream), encoding="utf-8"
        )
    spawned: list[_FileLogcat] = []

    def _next_stream(cmd, **_kwargs):
        stream = _FileLogcat(tmp_path / f"stream-{len(spawned)}.logcat")
        spawned.append(stream)
        return stream

    asked: list[tuple] = []

    def _device(*args, **kwargs):
        asked.append(args)
        return _age_answer(recorded, "ps_lifetime", pid, 0)

    monkeypatch.setenv("ANDROID_EMU_ANR_MAX_RESTARTS", "1")
    monkeypatch.setattr(anr_watcher, "RESTART_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(anr_watcher.signal, "signal", lambda *_a, **_k: None)
    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _next_stream)
    monkeypatch.setattr(anr_watcher, "run_adb", _device)
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"serial": "emulator-5554"})
    try:
        anr_watcher.AnrBuster(store=store).run_worker(meta.session_id)
    finally:
        for stream in spawned:
            stream.stdout.close()

    assert len(spawned) == 2
    apps = [e.app for e in store.read_events(meta.session_id) if e.kind == "jank"]
    assert apps == ["com.example.composefixture", "com.android.systemui"], apps
    assert len(asked) == 1, asked


def test_no_lookup_starts_once_a_stop_is_requested(no_device):
    """The worker's resolver asks ``may_ask`` before every device call."""
    names = anr_watcher._ProcessNames("emulator-5554", may_ask=lambda: False)

    assert names.name_of(4242, "10-17 00:53:28.000") is None
    assert no_device == []
    assert names.unnamed == 1


def test_stop_says_so_when_the_worker_will_not_finish(store, monkeypatch, capsys):
    """If the worker outlives the wait, the summary is marked, never silently short."""
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert stubborn.stdout.readline().strip() == "ready"
        meta = store.create({})
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
        _run_worker_over(store, meta.session_id, _recorded_jank_burst(recorded, (80,)), tmp_path)

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
        _run_worker_over(store, meta.session_id, _recorded_jank_burst(recorded, (80,)), tmp_path)
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
    """--watch blocked in readline() after a complete record and printed nothing.

    The stand-in stream writes the recorded broadcast ANR record and holds its
    stdout open, as a live logcat does between bursts. The event must come out
    while the stream is open -- not when it ends. Waited on with a bound, as
    the failure is "never"; on success it arrives a quarter-second in.
    """
    burst = tmp_path / "burst.txt"
    burst.write_text(recorded.text("logcat_anr_broadcast"), encoding="utf-8")
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


def test_fd_lines_frames_what_arrives_in_pieces():
    """Split lines, a UTF-8 character split across reads, a bad byte, CRLF, a tail."""
    read_end, write_end = os.pipe()
    with os.fdopen(read_end, "rb", buffering=0) as stream:
        lines = anr_watcher._FdLines(stream)
        os.write(write_end, b"abc")
        assert lines.read(timeout=0) == [], "half a line is not a line"
        os.write(write_end, b"def\ncaf\xc3")
        assert lines.read(timeout=0) == ["abcdef"]
        os.write(write_end, b"\xa9\r\nbad \xff\nno newline")
        assert lines.read(timeout=0) == ["café", "bad �"]
        os.close(write_end)
        assert lines.read(timeout=0) == ["no newline"]
        assert lines.read(timeout=0) is None


def test_worker_stopped_before_the_stream_goes_quiet_keeps_the_open_record(
    recorded, tmp_path, monkeypatch
):
    """The stop arrives right after the record is read, before any quiet pass.

    The record is still open in the assembler then; the stop path must close
    it, with its PID and Reason, rather than lose it.
    """
    burst = tmp_path / "burst.txt"
    burst.write_text(recorded.text("logcat_anr_broadcast"), encoding="utf-8")
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
            )
    finally:
        _reap(proc)

    (event,) = store.read_events(meta.session_id)
    assert event.kind == "anr" and event.reason and event.app_pid
