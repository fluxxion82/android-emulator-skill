"""D11: creating and pruning ANR sessions, serialised -- and the lock outside what is deleted.

Device-free. Creation made a session's directory and its events file before it
could take the session's lock, which lived inside that directory
(``<session>/lock``); every prune removed a directory with no meta, and any
session by age alone -- running, deciding, pending -- and ``clear()`` all of
them. So a prune running beside a creation deleted the session being created,
and deleting a session unlinked the very lock file another process held a
``flock`` on: the next opener got another inode, and the two no longer
excluded each other.

The lock now lives in ``<root>/locks/<id>.lock``. Every opener takes the base
guard (``locks/.base``) before it opens and locks a session's lock file;
creation holds the session lock across making the directory and writing the
meta; sweeps take the base guard, then each session's lock without waiting,
and remove only sessions that have ended.

The interleavings below are forced with hooks and pipes, never with sleeps:
each child process says where it has stopped and waits to be told to go on.
A bound (``_WAIT``) only turns a deadlock into a failure.
"""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from common import anr_sessions

_SPAWN = multiprocessing.get_context("spawn")
# No step takes this long; a step that does is a deadlock, and fails the test.
_WAIT = 60


def _store(tmp_path) -> anr_sessions.SessionStore:
    return anr_sessions.SessionStore(base_dir=tmp_path / "anr-sessions")


def _rewrite(store, meta) -> None:
    """Put ``meta`` back as the session's meta, under its lock (a test backdating it)."""
    with store.transaction(meta.session_id):
        store._write_meta(meta)


def _backdate(store, session_id: str, hours: int = 48):
    meta = store.load_meta(session_id)
    meta.started_at_ms = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
    _rewrite(store, meta)
    return meta


def _ended(store, session_id: str) -> str:
    """End a pending session as --stop does (cancelled): a session that may be pruned."""
    with store.transaction(session_id) as meta:
        store.mark_cancelled(meta)
    return session_id


def _lock_file(store, session_id: str) -> Path:
    return store.base_dir / "locks" / f"{session_id}.lock"


def _refused_while_held(store, session_id: str) -> None:
    """Another writer of ``session_id`` is kept out: the lock it holds is the one the path names."""
    with (
        pytest.raises(anr_sessions.SessionBusyError),
        store.transaction(session_id, timeout=0.3),
    ):
        pass


# --- the other processes -----------------------------------------------------
#
# Top-level, so a spawned child can import them by name.


def _create_pausing_at(base_dir: str, pause_at: tuple[str, ...], conn) -> None:
    """``SessionStore.create`` in a process of its own, stopping at each of ``pause_at``.

    ``lock-opened``: the session's lock file is open and not yet locked.
    ``before-mkdir``: about to make the session's directory. ``after-mkdir``:
    has just made it. At each, the child sends ``(step, session_id)`` and waits
    for ``"go"``; it ends with ``("created", session_id)`` or ``("error", ...)``.
    """
    base = Path(base_dir)
    real_mkdir = Path.mkdir
    real_open = os.open
    paused: set[str] = set()

    def pause(step: str, session_id: str) -> None:
        if step in pause_at and step not in paused:
            paused.add(step)
            conn.send((step, session_id))
            if conn.recv() != "go":
                raise RuntimeError("told something other than go")

    def mkdir(self, *args, **kwargs):
        session = self.parent == base and self.name.startswith("anr-")
        if session:
            pause("before-mkdir", self.name)
        real_mkdir(self, *args, **kwargs)
        if session:
            pause("after-mkdir", self.name)

    def open_(path, flags, mode=0o777, *args, **kwargs):
        fd = real_open(path, flags, mode, *args, **kwargs)
        name = os.path.basename(os.fspath(path))
        if name.startswith("anr-") and name.endswith(".lock"):
            pause("lock-opened", name.removesuffix(".lock"))
        return fd

    Path.mkdir = mkdir
    os.open = open_
    try:
        meta = anr_sessions.SessionStore(base_dir=base).create({})
    except BaseException as error:
        conn.send(("error", repr(error)))
    else:
        conn.send(("created", meta.session_id))


def _hold_transaction(base_dir: str, session_id: str, conn) -> None:
    """Hold ``session_id``'s lock (a --get-details reading it) until told ``"release"``."""
    store = anr_sessions.SessionStore(base_dir=Path(base_dir))
    try:
        with store.transaction(session_id, timeout=_WAIT):
            conn.send(("held", session_id))
            if conn.recv() != "release":
                raise RuntimeError("told something other than release")
    except BaseException as error:
        conn.send(("error", repr(error)))
    else:
        conn.send(("released", session_id))


def _prune_saying_when_it_waits(base_dir: str, conn) -> None:
    """``prune_expired`` in a process of its own, saying ``("blocked", None)`` the first time
    a lock it asks for is held -- so the parent knows it is waiting, without a sleep."""
    real_flock = fcntl.flock
    said: list[bool] = []

    def flock(fd, operation):
        try:
            return real_flock(fd, operation)
        except BlockingIOError:
            if not said:
                said.append(True)
                conn.send(("blocked", None))
            raise

    fcntl.flock = flock
    store = anr_sessions.SessionStore(base_dir=Path(base_dir))
    try:
        deleted = store.prune_expired(ttl_hours=24)
    except BaseException as error:
        conn.send(("error", repr(error)))
    else:
        conn.send(("done", deleted))


class _Child:
    def __init__(self, target, *args) -> None:
        self.conn, theirs = _SPAWN.Pipe()
        self.process = _SPAWN.Process(target=target, args=(*args, theirs), daemon=True)
        self.process.start()
        theirs.close()

    def expect(self, *kinds: str) -> tuple:
        assert self.conn.poll(_WAIT), f"no word from the child in {_WAIT}s, waiting for {kinds}"
        message = self.conn.recv()
        assert message[0] in kinds, f"expected {kinds}, the child said {message}"
        return message

    def send(self, word: str) -> None:
        self.conn.send(word)

    def close(self) -> None:
        self.process.join(_WAIT)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(_WAIT)
        self.conn.close()


@contextmanager
def _child(target, *args) -> Iterator[_Child]:
    child = _Child(target, *args)
    try:
        yield child
    except BaseException:
        # A failed step leaves the child waiting for a word that will not come.
        child.process.terminate()
        raise
    finally:
        child.close()


# --- (a) a prune beside a creation, in one process ---------------------------


@pytest.mark.parametrize("step", ["before-mkdir", "after-mkdir"])
def test_a_prune_during_a_creation_never_removes_the_session_being_created(
    step, tmp_path, monkeypatch
):
    """A prune runs (on another thread) as the creator is about to make, or has just made,
    the session's directory. The directory has no meta yet; it must still not be deleted."""
    store = _store(tmp_path)
    real_mkdir = Path.mkdir
    pruned: dict = {}

    def prune_beside() -> None:
        def run() -> None:
            try:
                pruned["deleted"] = (store.prune_expired(ttl_hours=0), store.clear())
            except BaseException as error:  # reported below, not lost on the thread
                pruned["error"] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(_WAIT)
        assert not thread.is_alive(), "the prune never finished"

    def mkdir_with_a_prune(self, *args, **kwargs):
        session = self.parent == store.base_dir and self.name.startswith("anr-")
        if session and step == "before-mkdir":
            prune_beside()
        real_mkdir(self, *args, **kwargs)
        if session and step == "after-mkdir":
            prune_beside()

    monkeypatch.setattr(Path, "mkdir", mkdir_with_a_prune)
    meta = store.create({})

    assert "error" not in pruned, pruned.get("error")
    assert pruned["deleted"] == (0, 0)
    assert store.load_meta(meta.session_id).status == "pending"
    assert store.events_path(meta.session_id).exists()


# --- (b) what a prune may never remove ---------------------------------------


@pytest.mark.parametrize("state", ["pending", "running", "deciding", "legacy-unresolved"])
def test_a_prune_never_removes_a_session_whose_writer_may_be_alive(state, tmp_path, monkeypatch):
    """However old: a pending session (its worker may yet claim it, or --stop cancel it), one a
    worker is at work in, and one whose worker cannot be shown gone (UNRESOLVED -- here an
    earlier version's, whose ``stopped`` was never a promise its worker had stopped)."""
    store = _store(tmp_path)
    session_id = store.create({}).session_id
    if state in ("running", "deciding"):
        store.claim_worker(session_id, pid=4242, token=store.load_meta(session_id).worker_token)
    if state == "deciding":
        store.worker_deciding(session_id, 4242, {})
    meta = _backdate(store, session_id)
    if state == "legacy-unresolved":
        meta.lifecycle, meta.status, meta.pid = None, "stopped", os.getpid()
        _rewrite(store, meta)

        def no_ps(_pid, _field):
            raise OSError("ps is not installed")

        monkeypatch.setattr(anr_sessions, "_ps", no_ps)
        assert anr_sessions.worker_identity(meta)[0] is anr_sessions.Identity.UNRESOLVED
    status = store.load_meta(session_id).status

    assert store.prune_expired(ttl_hours=24) == 0
    assert store.clear() == 0
    assert store.prune_to_aggregate_cap(1).deleted == 0

    assert store.load_meta(session_id).status == status


def test_an_earlier_versions_session_whose_worker_is_gone_is_pruned(tmp_path, monkeypatch):
    """The other side of UNRESOLVED: shown gone, nothing writes it, and it ages out."""
    store = _store(tmp_path)
    meta = _backdate(store, store.create({}).session_id)
    meta.lifecycle, meta.status, meta.pid = None, "stopped", 4242
    _rewrite(store, meta)
    monkeypatch.setattr(anr_sessions, "_ps", lambda _pid, _field: None)
    monkeypatch.setattr(anr_sessions, "_exists", lambda _pid: False)

    assert store.prune_expired(ttl_hours=24) == 1


# --- (c) two processes: nothing deleted from under a lock --------------------


def test_deleting_sessions_never_unlinks_a_lock_another_process_holds(tmp_path):
    """A --get-details in another process holds an ended session's lock. ``clear()`` used
    to delete the directory -- and the lock file in it -- from under it."""
    store = _store(tmp_path)
    ended = _ended(store, store.create({}).session_id)
    with _child(_hold_transaction, str(store.base_dir), ended) as reader:
        reader.expect("held")

        assert store.clear() == 0
        assert store.prune_expired(ttl_hours=0) == 0
        assert store.session_dir(ended).is_dir()
        _refused_while_held(store, ended)

        reader.send("release")
        reader.expect("released")

    assert store.clear() == 1
    assert not store.session_dir(ended).exists()
    assert not _lock_file(store, ended).exists()


@pytest.mark.parametrize("step", ["before-mkdir", "after-mkdir"])
def test_a_session_another_process_is_creating_survives_every_sweep(step, tmp_path):
    """The creator, in its own process, stops just before or just after making the
    directory. Its lock is held across both, so the sweeps skip it and no other writer
    gets in; told to go on, it finishes, and the session is there, pending."""
    store = _store(tmp_path)
    with _child(_create_pausing_at, str(store.base_dir), (step,)) as creator:
        _, session_id = creator.expect(step)

        assert store.prune_expired(ttl_hours=0) == 0
        assert store.clear() == 0
        assert store.session_dir(session_id).is_dir() == (step == "after-mkdir")
        _refused_while_held(store, session_id)
        assert store.prune_to_aggregate_cap(1).deleted == 0

        creator.send("go")
        creator.expect("created")

    assert store.load_meta(session_id).status == "pending"


# --- (d) interrupted creation -------------------------------------------------


def test_a_creation_that_died_before_its_meta_is_removed_by_the_next_prune(tmp_path, monkeypatch):
    """A directory with no meta whose lock is free has no creator left: it is removed,
    with its lock file -- by age it could never be judged, having no meta to say it."""
    store = _store(tmp_path)

    class _CreatorDiedError(Exception):
        pass

    def die(_self, _meta):
        raise _CreatorDiedError

    with monkeypatch.context() as patch:
        patch.setattr(anr_sessions.SessionStore, "_write_meta", die)
        with pytest.raises(_CreatorDiedError):
            store.create({})
    (session,) = [p for p in store.base_dir.iterdir() if p.name.startswith("anr-")]
    assert not (session / "meta.json").exists()

    assert store.prune_expired(ttl_hours=24) == 1

    assert not session.exists()
    assert not _lock_file(store, session.name).exists()


# --- (e) a pending orphan ------------------------------------------------------


def test_a_pending_orphan_is_kept_until_stop_cancels_it_then_pruned(tmp_path):
    """--start died between creating the session and spawning its worker: the session is
    pending with no worker coming. No age proves that, so it is kept -- until --stop
    cancels it as it cancels any pending session, and then it ages out."""
    import anr_watcher

    from common import cli

    store = _store(tmp_path)
    orphan = _backdate(store, store.create({}).session_id).session_id

    assert store.prune_expired(ttl_hours=24) == 0
    assert store.prune_to_aggregate_cap(1).deleted == 0
    assert store.clear() == 0
    assert store.load_meta(orphan).status == "pending"

    with pytest.raises(cli.Failure, match="cancelled"):
        anr_watcher.AnrBuster(store=store).stop(orphan)
    assert store.load_meta(orphan).status == "cancelled"

    assert store.prune_expired(ttl_hours=24) == 1
    assert not store.session_dir(orphan).exists()


# --- (f) the aggregate cap, best effort -----------------------------------------


def _fill(store, session_id: str, size: int) -> None:
    with open(store.events_path(session_id), "w") as handle:
        handle.write("x" * size)


def test_the_cap_evicts_ended_sessions_and_discloses_what_it_could_not(tmp_path):
    """Oldest ended sessions go first; sessions that have not ended stay even when they
    alone exceed the cap, and the result says by how much, and how many it kept."""
    store = _store(tmp_path)
    ended = _ended(store, store.create({}).session_id)
    pending = store.create({}).session_id
    running = store.create({}).session_id
    store.claim_worker(running, pid=4242, token=store.load_meta(running).worker_token)
    for started, session_id in enumerate((ended, pending, running)):
        meta = store.load_meta(session_id)
        meta.started_at_ms = 1000 + started
        _rewrite(store, meta)
        _fill(store, session_id, 3000)
    kept = store.session_total_bytes(pending) + store.session_total_bytes(running)

    capped = store.prune_to_aggregate_cap(2000)

    assert {store.load_meta(pending).status, store.load_meta(running).status} == {
        "pending",
        "running",
    }
    assert not store.session_dir(ended).exists()
    assert capped.deleted == 1
    assert capped.over_cap == {"bytes_over": kept - 2000, "protected": 2}


def test_a_cap_under_its_limit_discloses_nothing(tmp_path):
    store = _store(tmp_path)
    store.create({})

    capped = store.prune_to_aggregate_cap(100 * 1024 * 1024)

    assert (capped.deleted, capped.over_cap) == (0, None)


def test_start_warns_when_sessions_it_may_not_prune_exceed_the_cap(tmp_path, monkeypatch, capsys):
    """--start is not refused over the cap -- a --start that cannot free space is worse
    than an overrun -- but it says so, on stderr, so stdout stays the bare id."""
    import anr_watcher

    monkeypatch.setenv("ANDROID_EMU_ANR_TOTAL_CAP_MB", "1")
    store = _store(tmp_path)
    running = store.create({}).session_id
    store.claim_worker(running, pid=4242, token=store.load_meta(running).worker_token)
    _fill(store, running, 1024 * 1024 + 4096)
    over = store.session_total_bytes(running) - 1024 * 1024

    class _StoppedBeforeTheDeviceError(Exception):
        pass

    def stop_before_the_device(_serial):
        raise _StoppedBeforeTheDeviceError

    buster = anr_watcher.AnrBuster(store=store)
    monkeypatch.setattr(buster, "_resolve_serial", stop_before_the_device)
    with pytest.raises(_StoppedBeforeTheDeviceError):
        buster.start({}, serial=None)

    err = capsys.readouterr().err
    assert store.load_meta(running).status == "running"
    assert f"{over} bytes over" in err, err
    assert "1 session" in err, err


# --- (g) locks/ is no session -------------------------------------------------------


def test_the_locks_directory_is_no_session_to_any_walker(tmp_path):
    """Listing, TTL, the cap and clear() walk sessions only: not ``locks/`` (a directory
    with no meta, which a prune would have taken for an interrupted creation), and not a
    directory create() could not have made."""
    store = _store(tmp_path)
    locks = store.base_dir / "locks"
    locks.mkdir(exist_ok=True)
    ended = _ended(store, store.create({}).session_id)
    pending = store.create({}).session_id
    foreign = store.base_dir / "kept by hand"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine", encoding="utf-8")

    assert sorted(m.session_id for m in store.list_sessions()) == sorted([ended, pending])
    assert store.prune_to_aggregate_cap(1).deleted == 1
    assert store.prune_expired(ttl_hours=0) == 0
    assert store.clear() == 0

    assert [m.session_id for m in store.list_sessions()] == [pending]
    assert (locks / ".base").is_file()
    assert _lock_file(store, pending).is_file()
    assert (foreign / "notes.txt").is_file()


# --- (h) the lock-file sweep ---------------------------------------------------------


def test_the_lock_sweep_unlinks_only_orphaned_unheld_lock_files(tmp_path):
    """A lock file with no session directory and no holder is unlinked; one whose session
    exists, one held by a creator that has not made its directory yet, and anything that
    is not a session's lock file, are not."""
    store = _store(tmp_path)
    locks = store.base_dir / "locks"
    locks.mkdir(exist_ok=True)
    live = store.create({}).session_id
    (locks / "anr-20260101-000000-dead.lock").touch()
    (locks / "README").touch()
    with _child(_create_pausing_at, str(store.base_dir), ("before-mkdir",)) as creator:
        _, held = creator.expect("before-mkdir")

        store.prune_expired(ttl_hours=24)

        assert sorted(p.name for p in locks.iterdir()) == sorted(
            [".base", "README", f"{live}.lock", f"{held}.lock"]
        )
        _refused_while_held(store, held)
        creator.send("go")
        creator.expect("created")


def test_a_sweep_waits_for_the_base_guard_an_opener_holds(tmp_path):
    """Lock order is base -> session for every opener. The creator has opened its lock
    file (under the base guard) and not yet locked it; no directory exists. A sweep that
    did not wait for the base guard would find that file unheld with no directory and
    unlink it, and the creator would then lock an inode no other opener can reach."""
    store = _store(tmp_path)
    pause_at = ("lock-opened", "before-mkdir")
    with _child(_create_pausing_at, str(store.base_dir), pause_at) as creator:
        _, session_id = creator.expect("lock-opened")
        lock = _lock_file(store, session_id)
        assert lock.exists()
        with _child(_prune_saying_when_it_waits, str(store.base_dir)) as sweeper:
            # Blocked on the base guard, or done: either way the sweep has had its chance.
            first = sweeper.expect("blocked", "done")
            creator.send("go")
            creator.expect("before-mkdir")
            if first[0] == "blocked":
                sweeper.expect("done")

            assert lock.exists(), "the sweep unlinked the lock file a creator had open"
            _refused_while_held(store, session_id)

        creator.send("go")
        creator.expect("created")

    assert store.load_meta(session_id).status == "pending"


def test_no_session_lock_is_taken_while_the_base_guard_is_held(tmp_path):
    """Base -> session for every opener: while a sweep holds the base guard, a transaction
    waits for it -- on a session whose own lock is free -- and is refused past its bound."""
    store = _store(tmp_path)
    session_id = store.create({}).session_id

    with store._base_guard():
        _refused_while_held(store, session_id)

    with store.transaction(session_id) as meta:
        assert meta.status == "pending"


def test_a_locks_directory_an_earlier_versions_prune_deleted_is_made_again(tmp_path):
    """An earlier version's prune takes ``locks/`` for a session with no meta and deletes
    it (a live test of fb5de9c's --start found this): the next opener makes it again."""
    store = _store(tmp_path)
    ended = _ended(store, store.create({}).session_id)
    shutil.rmtree(store.base_dir / "locks")

    pending = store.create({}).session_id

    assert store.clear() == 1
    assert not store.session_dir(ended).exists()
    assert [meta.session_id for meta in store.list_sessions()] == [pending]
