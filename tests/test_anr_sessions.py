"""Device-free tests for the ANR session store (filesystem-backed).

The storage root is overridden via ``tmp_path`` — both through the
``SessionStore(base_dir=...)`` constructor and the ``ANDROID_EMU_ANR_HOME`` env
var — so the suite never touches the real home directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from common import anr_sessions
from common.anr_pipeline import SummaryBuilder


def _store(tmp_path) -> anr_sessions.SessionStore:
    return anr_sessions.SessionStore(base_dir=tmp_path / "anr-sessions")


def _summary(session_id: str):
    return SummaryBuilder(
        session_id=session_id, started_at="2026-06-17T14:30:00", duration_ms=1000
    ).build([])


# --- create / meta round-trip ----------------------------------------------


def test_session_id_format(tmp_path):
    store = _store(tmp_path)
    meta = store.create({"package": "com.myapp"})
    assert meta.session_id.startswith("anr-")
    # anr-YYYYMMDD-HHmmss-XXXX
    parts = meta.session_id.split("-")
    assert len(parts) == 4
    assert len(parts[3]) == 4


def test_create_writes_meta_and_events_file(tmp_path):
    store = _store(tmp_path)
    meta = store.create({"package": "com.myapp", "min_frames": 30})
    loaded = store.load_meta(meta.session_id)
    assert loaded.session_id == meta.session_id
    assert loaded.args["package"] == "com.myapp"
    assert loaded.status == "pending"
    assert store.events_path(meta.session_id).exists()


def test_meta_atomic_write_is_valid_json(tmp_path):
    store = _store(tmp_path)
    meta = store.create({})
    with open(store._meta_path(meta.session_id)) as handle:
        # Must be complete, parseable JSON (atomic .tmp + replace).
        payload = json.load(handle)
    assert payload["session_id"] == meta.session_id


# --- status transitions ----------------------------------------------------


def test_status_pending_to_running(tmp_path):
    store = _store(tmp_path)
    meta = store.create({})
    assert meta.status == "pending"
    claimed = store.claim_worker(meta.session_id, pid=4242)
    assert claimed.status == "running"
    assert claimed.pid == 4242


def test_status_running_to_stopped(tmp_path):
    store = _store(tmp_path)
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=4242)
    store.stop(meta.session_id, _summary(meta.session_id))
    reloaded = store.load_meta(meta.session_id)
    assert reloaded.status == "stopped"
    assert reloaded.stopped_at_ms is not None
    assert store.load_summary(meta.session_id) is not None


def test_status_running_to_crashed(tmp_path):
    store = _store(tmp_path)
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=4242)
    store.mark_crashed(meta.session_id)
    reloaded = store.load_meta(meta.session_id)
    assert reloaded.status == "crashed"
    assert reloaded.stopped_at_ms is not None


def test_persist_counters_preserves_terminal_status(tmp_path):
    store = _store(tmp_path)
    meta = store.create({})
    store.claim_worker(meta.session_id, pid=4242)
    store.mark_crashed(meta.session_id)
    # Worker shutdown flush must not clobber CRASHED back to RUNNING.
    store.persist_worker_counters(meta.session_id, {"total": 5})
    reloaded = store.load_meta(meta.session_id)
    assert reloaded.status == "crashed"
    assert reloaded.extras["line_counters"]["total"] == 5


# --- TTL prune -------------------------------------------------------------


def test_prune_expired_drops_old_sessions(tmp_path):
    store = _store(tmp_path)
    fresh = store.create({})
    old = store.create({})
    # Backdate the old session past the TTL.
    old_meta = store.load_meta(old.session_id)
    stale = datetime.now() - timedelta(hours=48)
    old_meta.started_at_ms = int(stale.timestamp() * 1000)
    store._write_meta(old_meta)

    deleted = store.prune_expired(ttl_hours=24)
    assert deleted == 1
    assert not store.session_dir(old.session_id).exists()
    assert store.session_dir(fresh.session_id).exists()


def test_prune_expired_respects_env_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_ANR_SESSION_TTL_HOURS", "1")
    store = _store(tmp_path)
    old = store.create({})
    old_meta = store.load_meta(old.session_id)
    stale = datetime.now() - timedelta(hours=2)
    old_meta.started_at_ms = int(stale.timestamp() * 1000)
    store._write_meta(old_meta)
    # Default ttl now resolves to 1h from the env var.
    assert store.prune_expired() == 1


# --- aggregate cap ---------------------------------------------------------


def test_prune_to_aggregate_cap_evicts_oldest(tmp_path):
    store = _store(tmp_path)
    sessions = []
    for i in range(3):
        meta = store.create({})
        # Stagger started_at_ms so eviction order is deterministic (oldest first).
        meta.started_at_ms = 1000 + i
        store._write_meta(meta)
        # Write some bytes into each session.
        with open(store.events_path(meta.session_id), "w") as handle:
            handle.write("x" * 1000)
        sessions.append(meta.session_id)

    # Cap below total -> oldest session(s) evicted.
    deleted = store.prune_to_aggregate_cap(max_bytes=1500)
    assert deleted >= 1
    # The oldest (sessions[0]) goes first.
    assert not store.session_dir(sessions[0]).exists()
    assert store.session_dir(sessions[-1]).exists()


def test_prune_to_aggregate_cap_noop_under_limit(tmp_path):
    store = _store(tmp_path)
    store.create({})
    assert store.prune_to_aggregate_cap(max_bytes=100 * 1024 * 1024) == 0


# --- env-var storage root override (tests must not touch real home) --------


def test_home_env_var_overrides_storage_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_ANR_HOME", str(tmp_path / "custom-home"))
    store = anr_sessions.SessionStore()
    meta = store.create({})
    expected = tmp_path / "custom-home" / "anr-sessions" / meta.session_id
    assert expected.exists()


# --- list / clear ----------------------------------------------------------


def test_list_sessions_newest_first(tmp_path):
    store = _store(tmp_path)
    first = store.create({})
    first_meta = store.load_meta(first.session_id)
    first_meta.started_at_ms = 1000
    store._write_meta(first_meta)
    second = store.create({})
    second_meta = store.load_meta(second.session_id)
    second_meta.started_at_ms = 2000
    store._write_meta(second_meta)

    metas = store.list_sessions()
    assert metas[0].session_id == second.session_id


def test_clear_all_sessions(tmp_path):
    store = _store(tmp_path)
    store.create({})
    store.create({})
    assert store.clear() == 2
    assert store.list_sessions() == []
