"""Device-free tests for anr_watcher's command construction and session flow.

All adb/subprocess interaction is mocked. Session storage is redirected to
``tmp_path`` via ``SessionStore(base_dir=...)`` so nothing touches the real home.
"""

from __future__ import annotations

import json

import anr_watcher

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
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

            class _Out:
                def readline(self):
                    return ""

            self.stdout = _Out()

        def poll(self):
            return 0

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _FakePopen)
    ok = anr_watcher.AnrWatcher().watch(json_mode=True)
    assert ok is True
    # House rule: never shell=True.
    assert captured["kwargs"].get("shell") is not True
    assert "shell" not in captured["kwargs"]
    assert captured["cmd"][0] == "adb"


def test_show_since_runs_dump_command(monkeypatch):
    captured: dict = {}

    class _Result:
        stdout = ""
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


# --- post-parse --package filter -------------------------------------------


def test_matches_package_by_anr_package():
    event = parse_logcat_anr(
        "06-17 14:31:00.000 2 2 E ActivityManager: ANR in com.example.app (com.example.app/.Main)"
    )
    assert anr_watcher.matches_package(event, "com.example.app") is True
    assert anr_watcher.matches_package(event, "com.other.thing") is False


def test_matches_package_by_short_name():
    event = parse_logcat_anr(
        "06-17 14:31:00.000 2 2 E ActivityManager: ANR in com.example.app (com.example.app/.Main)"
    )
    # Short suffix match keeps it lenient for the tag-only jank case.
    assert anr_watcher.matches_package(event, "com.foo.app") is True


# --- --stop summary over a seeded events.jsonl -----------------------------


def _seed_session(tmp_path, frames_list, package=None):
    """Create a session dir + seed events.jsonl with normalised jank events."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({"package": package} if package else {})
    store.claim_worker(meta.session_id, pid=99999)
    path = store.events_path(meta.session_id)
    with open(path, "a") as handle:
        for i, frames in enumerate(frames_list):
            raw = parse_logcat_anr(
                f"06-17 14:30:5{i % 9}.123  1234  1234 I Choreographer: Skipped {frames} frames!"
            )
            event = build_normalised_event(raw, session_start_ms=0, current_ms=(i + 1) * 100)
            handle.write(event_to_jsonl(event) + "\n")
    return store, meta.session_id


def test_stop_builds_summary_over_seeded_events(tmp_path, monkeypatch):
    store, session_id = _seed_session(tmp_path, [35, 65, 150])
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


def test_stop_terse_is_one_line(tmp_path, monkeypatch):
    store, session_id = _seed_session(tmp_path, [120])
    buster = anr_watcher.AnrBuster(store=store)
    monkeypatch.setattr(store, "signal_worker", lambda *_a, **_k: False)
    out = buster.stop(session_id, terse=True)
    assert "\n" not in out


def test_stop_json_mode(tmp_path, monkeypatch):
    store, session_id = _seed_session(tmp_path, [35, 150])
    buster = anr_watcher.AnrBuster(store=store)
    monkeypatch.setattr(store, "signal_worker", lambda *_a, **_k: False)
    out = buster.stop(session_id, json_mode=True)
    payload = json.loads(out)
    assert payload["session_id"] == session_id
    assert payload["event_count"] == 2


# --- worker stream parsing (frame threshold + package filter) --------------


def test_worker_drops_subthreshold_and_keeps_anr(tmp_path, monkeypatch):
    """The worker's read loop drops low-frame jank but keeps ANRs."""
    store = SessionStore(base_dir=tmp_path / "anr-sessions")
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=1)
    buster = anr_watcher.AnrBuster(store=store)

    lines = [
        "06-17 14:30:50.000  1 1 I Choreographer: Skipped 5 frames!\n",  # dropped
        "06-17 14:30:51.000  1 1 I Choreographer: Skipped 80 frames!\n",  # kept
        "06-17 14:30:52.000  2 2 E ActivityManager: ANR in com.x (com.x/.A)\n",  # kept
        "",  # EOF
    ]

    class _FakeProc:
        def __init__(self):
            self._lines = iter(lines)
            self.stdout = self

        def readline(self):
            return next(self._lines, "")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
    stop_flag = {"value": False}
    proc = _FakeProc()

    # select.select always reports "ready" so the loop drains via readline().
    monkeypatch.setattr(anr_watcher.select, "select", lambda *_a, **_k: ([proc.stdout], [], []))

    with open(store.events_path(meta.session_id), "a", buffering=1) as out_handle:
        buster._read_stream_into_events(
            proc=proc,
            out_handle=out_handle,
            stop_flag=stop_flag,
            counters=counters,
            package=None,
            min_frames=30,
            session_start_ms=0,
        )

    assert counters["total"] == 3
    assert counters["matched"] == 3
    assert counters["dropped"] == 1
    events = store.read_events(meta.session_id)
    # 80-frame jank + ANR survive; 5-frame jank dropped.
    assert len(events) == 2
    kinds = sorted(e.kind for e in events)
    assert kinds == ["anr", "jank"]
