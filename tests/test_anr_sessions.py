"""Device-free tests for the ANR session store (filesystem-backed).

The storage root is overridden via ``tmp_path`` — both through the
``SessionStore(base_dir=...)`` constructor and the ``ANDROID_EMU_ANR_HOME`` env
var — so the suite never touches the real home directory.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

from common import anr_sessions
from common.anr_pipeline import SummaryBuilder


def _store(tmp_path) -> anr_sessions.SessionStore:
    return anr_sessions.SessionStore(base_dir=tmp_path / "anr-sessions")


def _summary(session_id: str):
    return SummaryBuilder(
        session_id=session_id, started_at="2026-06-17T14:30:00", duration_ms=1000
    ).build([])


def _claim(store, session_id: str, pid: int = 4242):
    """A worker's claim of ``session_id``, presenting its token as the worker does."""
    return store.claim_worker(session_id, pid=pid, token=store.load_meta(session_id).worker_token)


def _rewrite(store, meta) -> None:
    """Put ``meta`` back as the session's meta, under its lock (a test backdating it)."""
    with store.transaction(meta.session_id):
        store._write_meta(meta)


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


def test_a_session_records_the_fingerprint_version_it_is_captured_under(tmp_path):
    """Provenance survives every later write of meta.json (claim, stop, crash)."""
    from common.anr_pipeline import FINGERPRINT_VERSION

    store = _store(tmp_path)
    meta = store.create({})
    _claim(store, meta.session_id, pid=1)
    store.finish_worker(meta.session_id, 1, {})
    store.stop(meta.session_id, store.build_summary(meta.session_id))

    assert store.load_meta(meta.session_id).fingerprint_version == FINGERPRINT_VERSION
    assert store.load_summary(meta.session_id).fingerprint_version == FINGERPRINT_VERSION


def test_a_session_without_a_recorded_version_is_version_one(tmp_path):
    """Sessions written before provenance was kept: v1 was the only version."""
    store = _store(tmp_path)
    meta = store.create({})
    path = store._meta_path(meta.session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["fingerprint_version"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.load_meta(meta.session_id).fingerprint_version == 1
    assert store.build_summary(meta.session_id).fingerprint_version == 1


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
    claimed = _claim(store, meta.session_id)
    assert claimed.status == "running"
    assert claimed.pid == 4242
    assert claimed.claimed_at_ms is not None


def test_status_running_to_stopped(tmp_path):
    store = _store(tmp_path)
    meta = store.create({})
    _claim(store, meta.session_id)
    store.finish_worker(meta.session_id, 4242, {"total": 3})
    store.stop(meta.session_id, _summary(meta.session_id))
    reloaded = store.load_meta(meta.session_id)
    assert reloaded.status == "stopped"
    assert reloaded.stopped_at_ms is not None
    assert reloaded.pid is None, "an ended worker's pid was kept"
    assert reloaded.extras["line_counters"] == {"total": 3}
    assert store.load_summary(meta.session_id) is not None


def test_status_running_to_crashed(tmp_path):
    """A worker ends crashed exactly when its counters hold a stream failure -- one write."""
    store = _store(tmp_path)
    meta = store.create({})
    _claim(store, meta.session_id)
    failure = {"error": "the stream broke", "truncated": False}
    store.finish_worker(meta.session_id, 4242, {"stream_failure": failure})
    reloaded = store.load_meta(meta.session_id)
    assert reloaded.status == "crashed"
    assert reloaded.stopped_at_ms is not None
    assert reloaded.extras["line_counters"]["stream_failure"] == failure


@pytest.mark.parametrize("end", ["stopped", "crashed", "cancelled", "abandoned"])
def test_nothing_a_worker_writes_after_the_session_ended_is_applied(end, tmp_path):
    """A session that has ended is written by no worker: each write is refused, unapplied.

    It used to be re-read and written over (``persist_worker_counters`` kept
    a terminal status, and wrote the rest): a worker's late write landed in a
    session --stop had already answered for.
    """
    store = _store(tmp_path)
    meta = store.create({})
    if end == "cancelled":
        with store.transaction(meta.session_id) as held:
            store.mark_cancelled(held)
    else:
        _claim(store, meta.session_id)
        if end == "abandoned":
            with store.transaction(meta.session_id) as held:
                store.mark_abandoned(held, "a test gives up on it")
        else:
            failure = {"stream_failure": {"error": "x"}} if end == "crashed" else {}
            store.finish_worker(meta.session_id, 4242, failure)
    path = store.session_dir(meta.session_id) / "meta.json"
    before = path.read_bytes()

    for write in (
        store.persist_worker_counters,
        store.worker_deciding,
        store.worker_resumed,
        store.finish_worker,
    ):
        with pytest.raises(anr_sessions.TransitionRefusedError):
            write(meta.session_id, 4242, {"total": 5})
    with pytest.raises(anr_sessions.TransitionRefusedError):
        _claim(store, meta.session_id)

    assert store.load_meta(meta.session_id).status == end
    assert path.read_bytes() == before


def test_a_worker_write_from_another_pid_is_refused(tmp_path):
    """The session's worker is the pid that claimed it; another process writes nothing."""
    store = _store(tmp_path)
    meta = store.create({})
    _claim(store, meta.session_id, pid=4242)

    with pytest.raises(anr_sessions.TransitionRefusedError):
        store.finish_worker(meta.session_id, 4243, {})

    assert store.load_meta(meta.session_id).status == "running"


@pytest.mark.parametrize("token", [None, "0" * 16], ids=["no-token", "another-token"])
def test_a_claim_without_the_sessions_token_is_refused(token, tmp_path):
    store = _store(tmp_path)
    meta = store.create({})

    with pytest.raises(anr_sessions.TransitionRefusedError):
        store.claim_worker(meta.session_id, pid=4242, token=token)

    assert store.load_meta(meta.session_id).status == "pending"


@pytest.mark.parametrize(
    "transition",
    ["cancel-running", "abandon-pending", "abandon-stopped", "summary-running"],
)
def test_the_clis_transitions_apply_only_from_the_state_they_expect(transition, tmp_path):
    """The CLI owns cancel (of a pending session), abandon (of a worker at work) and
    the summary (of an ended session); from any other state each is refused, unapplied."""
    store = _store(tmp_path)
    meta = store.create({})
    if transition != "abandon-pending":
        _claim(store, meta.session_id)
    if transition == "abandon-stopped":
        store.finish_worker(meta.session_id, 4242, {})
    path = store.session_dir(meta.session_id) / "meta.json"
    before = path.read_bytes()

    with (
        store.transaction(meta.session_id) as held,
        pytest.raises(anr_sessions.TransitionRefusedError),
    ):
        if transition == "cancel-running":
            store.mark_cancelled(held)
        elif transition.startswith("abandon"):
            store.mark_abandoned(held, "a test gives up on it")
        else:
            store.save_summary(held, _summary(meta.session_id))

    assert path.read_bytes() == before
    assert store.load_summary(meta.session_id) is None


# --- TTL prune -------------------------------------------------------------


def test_prune_expired_drops_old_sessions(tmp_path):
    store = _store(tmp_path)
    fresh = store.create({})
    old = store.create({})
    # Backdate the old session past the TTL.
    old_meta = store.load_meta(old.session_id)
    stale = datetime.now() - timedelta(hours=48)
    old_meta.started_at_ms = int(stale.timestamp() * 1000)
    _rewrite(store, old_meta)

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
    _rewrite(store, old_meta)
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
        _rewrite(store, meta)
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
    _rewrite(store, first_meta)
    second = store.create({})
    second_meta = store.load_meta(second.session_id)
    second_meta.started_at_ms = 2000
    _rewrite(store, second_meta)

    metas = store.list_sessions()
    assert metas[0].session_id == second.session_id


def test_clear_all_sessions(tmp_path):
    store = _store(tmp_path)
    store.create({})
    store.create({})
    assert store.clear() == 2
    assert store.list_sessions() == []


# --- --start's wait for its worker -------------------------------------------


def test_a_worker_that_registers_and_crashes_between_polls_is_found_registered(tmp_path):
    """A worker can claim its session and end it crashed between two of --start's polls.

    ``wait_for_worker`` accepted only a running worker with a pid, so one
    already done read as one that never started -- "did not register within
    3s", exit 1, where its own failure was the answer (CI on PR #42). Its
    claim recorded when it claimed (``claimed_at_ms``), which its end keeps.
    """
    store = _store(tmp_path)
    meta = store.create({})
    _claim(store, meta.session_id, pid=os.getpid())
    store.finish_worker(meta.session_id, os.getpid(), {"stream_failure": {"error": "x"}})

    found = store.wait_for_worker(meta.session_id, timeout_seconds=0.3)

    assert (found.status, found.pid) == ("crashed", None)
    assert found.claimed_at_ms is not None


def test_a_cancelled_session_is_not_a_registered_worker(tmp_path):
    """The other side: ended, but nothing ever claimed it -- still not registered."""
    store = _store(tmp_path)
    meta = store.create({})
    with store.transaction(meta.session_id) as held:
        store.mark_cancelled(held)

    with pytest.raises(TimeoutError):
        store.wait_for_worker(meta.session_id, timeout_seconds=0.2)


def test_a_claim_ps_could_not_time_is_still_a_registration(tmp_path, monkeypatch):
    """``worker_started`` is best effort: `ps` may not answer when the worker claims.

    The claim leaves no start time then -- which --stop reads as identity it
    cannot resolve -- but the claim itself happened, and is the registration.
    """
    monkeypatch.setattr(anr_sessions, "process_started", lambda _pid: None)
    store = _store(tmp_path)
    meta = store.create({})
    _claim(store, meta.session_id, pid=os.getpid())

    found = store.wait_for_worker(meta.session_id, timeout_seconds=0.3)

    assert (found.status, found.worker_started) == ("running", None)


# --- fourth review of PR #42: the lifecycle's writes ---------------------------


def test_counters_left_without_a_claim_are_not_a_registered_worker(tmp_path):
    """--start's evidence of a worker must be this session's claim, not counters lying there.

    ``wait_for_worker`` took ``line_counters`` (or a start time) beside a
    ``crashed`` status as proof the worker had registered -- whatever wrote
    them: a worker run by hand on a reused id, or counters an earlier writer
    left. Only a claim -- the pending session taken by a worker presenting
    its token, which records when (``claimed_at_ms``) -- is.
    """
    store = _store(tmp_path)
    meta = store.create({})
    path = store.session_dir(meta.session_id) / "meta.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        status="crashed",
        worker_started="Thu Jan  1 00:00:00 1970",
        extras={"line_counters": {"total": 0}},
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TimeoutError):
        store.wait_for_worker(meta.session_id, timeout_seconds=0.2)


def test_two_writers_of_one_session_never_share_a_temporary_file(tmp_path, monkeypatch):
    """A fixed ``meta.json.tmp`` let a second writer truncate and rename the first's file.

    The second write lands while the first has its temporary file open (here
    from inside the first's ``fsync``). With one name for both, the first's
    rename found its file gone (or renamed the second's content). Each write
    has a temporary file of its own: both complete, and the last one written
    is what meta.json holds.
    """
    store = _store(tmp_path)
    meta = store.create({})
    first = anr_sessions.SessionMeta.from_json({**meta.to_json(), "args": {"writer": "first"}})
    second = anr_sessions.SessionMeta.from_json({**meta.to_json(), "args": {"writer": "second"}})
    real_fsync = os.fsync
    nested = []

    def fsync_with_a_second_writer(fd):
        real_fsync(fd)
        if not nested:
            nested.append(fd)
            store._write_meta(second)

    monkeypatch.setattr(anr_sessions.os, "fsync", fsync_with_a_second_writer)
    with store.transaction(meta.session_id):
        store._write_meta(first)

    assert store.load_meta(meta.session_id).args == {"writer": "first"}
    leftovers = [p.name for p in store.session_dir(meta.session_id).iterdir() if "tmp" in p.name]
    assert leftovers == []


def test_no_write_is_made_without_the_sessions_lock(tmp_path):
    """Every write of a session's meta or summary is made under its lock, by construction.

    ``_write_meta`` and ``_write_summary`` refuse a caller that does not hold
    it, so no new path can write a meta it read without the lock -- the
    stale write that erased a persisted device loss.
    """
    store = _store(tmp_path)
    meta = store.create({})
    path = store.session_dir(meta.session_id) / "meta.json"
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="without its lock"):
        store._write_meta(meta)
    with pytest.raises(RuntimeError, match="without its lock"):
        store._write_summary(meta.session_id, _summary(meta.session_id))

    assert path.read_bytes() == before


def test_a_lock_held_past_its_bound_is_a_typed_refusal(tmp_path):
    """Acquisition is bounded: a writer that cannot have the lock in time writes nothing."""
    store = _store(tmp_path)
    meta = store.create({})
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store.transaction(meta.session_id):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        with (
            pytest.raises(anr_sessions.SessionBusyError) as busy,
            store.transaction(meta.session_id, timeout=0.3) as late,
        ):
            store.mark_cancelled(late)
        waited = time.monotonic() - started
    finally:
        release.set()
        holder.join(10)

    assert 0.3 <= waited < 5, waited
    assert "Run the same command again" in busy.value.remedy
    assert store.load_meta(meta.session_id).status == "pending"


def test_a_claim_racing_a_cancel_is_serialised(tmp_path, monkeypatch):
    """--stop cancels a pending session while its worker claims it: one wins, whole.

    Forced interleaving: --stop has read the session pending and is about to
    write ``cancelled`` when the worker's claim runs. Without the lock the
    claim read pending too and wrote ``running``, and --stop then wrote its
    cancellation over it -- a worker recording into a cancelled session, the
    late claim and the stale write at once. Under the lock the claim waits,
    finds the session cancelled and is refused; nothing it would have written
    is there.
    """
    store = _store(tmp_path)
    meta = store.create({})
    real_write = anr_sessions.SessionStore._write_meta
    claims: list = []

    def claim() -> None:
        try:
            claims.append(_claim(store, meta.session_id))
        except anr_sessions.TransitionRefusedError as refused:
            claims.append(refused)

    def write_after_a_claim_tries(self, written):
        if written.status == "cancelled" and not claims:
            racer = threading.Thread(target=claim, daemon=True)
            racer.start()
            racer.join(1.0)
            write_after_a_claim_tries.racer = racer
        real_write(self, written)

    monkeypatch.setattr(anr_sessions.SessionStore, "_write_meta", write_after_a_claim_tries)
    with store.transaction(meta.session_id) as held:
        store.mark_cancelled(held)
    write_after_a_claim_tries.racer.join(10)

    (outcome,) = claims
    assert isinstance(outcome, anr_sessions.TransitionRefusedError), outcome
    after = store.load_meta(meta.session_id)
    assert (after.status, after.pid, after.claimed_at_ms) == ("cancelled", None, None)
