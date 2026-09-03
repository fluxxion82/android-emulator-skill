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

import json
import re

import anr_watcher
import pytest

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
        anr_watcher.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out)
    assert "error" in payload, f"--json reported no error: {payload}"


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
