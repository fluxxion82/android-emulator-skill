#!/usr/bin/env python3
"""ANR watcher session storage — own dir layout, no ProgressiveCache reuse.

Ported from the iOS HangBuster session store (``hang_sessions.py``); the
filesystem machinery is platform-agnostic, only the storage root, env-var
prefix, and session-id prefix are retargeted for Android.

Each session is a directory under
``~/.android-emulator-skill/anr-sessions/<id>/`` containing ``meta.json``
(config + pid + status), ``events.jsonl`` (append-only normalised events) and
``summary.json`` (post-stop). Its lock is ``anr-sessions/locks/<id>.lock``,
outside the directory a prune deletes (D11, below).

The lifecycle (fourth review of PR #42: four rounds of closing one timing
window at a time did not converge, so it is serialised instead)::

    pending --claim (worker)--> running <--> deciding (worker)
       |                          |  \\          |
       | cancel (--stop,          |   \\---------+--> stopped  (worker: stopped on request)
       |  or --start giving up)   |              \\--> crashed  (worker: its stream failed)
       v                          |
    cancelled                     +-- abandon (--stop --abandon) --> abandoned

Every read-modify-write of a session's meta.json and summary.json happens
under its lock (``fcntl.flock`` on ``locks/<id>.lock``; how long a writer
waits for it is bounded, not how long it is held), by exactly one owner per
transition, and only from the state the transition expects -- anything else
is refused (:class:`TransitionRefusedError`), not applied. The worker owns
its claim, ``deciding``, its counters and its end (``stopped`` / ``crashed``,
which it publishes before it exits, with the counters and any failure in the
same write -- unless the lock cannot be had in
``WORKER_END_LOCK_TIMEOUT_SECONDS`` or the session was deleted); the CLI
owns ``cancelled``, ``abandoned`` and ``summary.json``, and writes no field
the worker owns. Those writes go to a temporary file of their own
(``mkstemp``) and are renamed into place. ``events.jsonl`` is the
exception: the worker appends to it without the lock, so an abandoned worker
may still append; the abandonment records how much of it the session's
answers read (``extras["abandoned_events_bytes"]``), and they read no more.

``flock`` needs a filesystem that implements it: a local one. A home on a
network filesystem serialises only as far as its server and mount options
honour ``flock``. Telling the worker from another process needs ``ps``.

A session an earlier version wrote has no ``lifecycle`` key: its worker took
no lock and checked no state, and its --stop could save ``stopped`` while
the worker ran on. --stop (anr_watcher) fences such a worker before trusting
what the session says.

Creating and pruning are serialised too (D11). A session's lock used to live
in its own directory, so creation could take it only after making the
directory, and a prune that took that meta-less directory for an abandoned one
deleted the session being created -- and with it the lock file another
process held, so the next opener locked another inode. Now:

- Every opener takes the base guard (``locks/.base``) first, opens and locks
  the session's lock file under it, and lets the guard go: base -> session,
  always. A waiter keeps no descriptor between attempts.
- ``create`` holds the session's lock from before it makes the directory until
  its meta (written first) and events file exist: a directory with no meta
  whose lock is free has no creator left.
- A sweep (TTL, the aggregate cap, ``clear``) holds the base guard throughout,
  takes each session's lock without waiting -- one that is held is skipped --
  reads it again under the lock, and removes only a session that has ended
  (``TERMINAL_STATES``; an earlier version's only once its worker is shown
  gone), or a directory with no meta (an interrupted creation). Never by age
  alone: a pending session whose worker never came stays until --stop cancels
  it. A lock file is unlinked only by a sweep holding both the base guard and
  that lock, with its directory gone -- never one someone holds.

The parent creates the directory and writes initial meta, with a random
``worker_token`` it hands the worker on its command line. ``--stop``
SIGTERMs the worker -- only once the process at that pid has been shown to
still be it -- waits for the end it publishes, builds a ``SessionSummary``,
and writes ``summary.json``.

Storage root is overridable for tests via the ``ANDROID_EMU_ANR_HOME`` env var
or the ``SessionStore(base_dir=...)`` constructor argument, so the suite never
touches the real home directory.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from common.anr_pipeline import (
    FINGERPRINT_VERSION,
    SessionSummary,
    SummaryBuilder,
    event_from_jsonl,
    summary_from_json,
    summary_to_json,
)
from common.env_config import env_int

# === CONSTANTS ===

PENDING = "pending"
RUNNING = "running"
DECIDING = "deciding"
STOPPED = "stopped"
CRASHED = "crashed"
CANCELLED = "cancelled"
ABANDONED = "abandoned"
# A worker is at work: it may still write, and how it ends is not yet known.
LIVE_STATES = frozenset({RUNNING, DECIDING})
# Nothing writes the session's meta again (a --stop only saves its summary).
TERMINAL_STATES = frozenset({STOPPED, CRASHED, CANCELLED, ABANDONED})

# The lifecycle above. Sessions written before it (v0.8.0 main) have no
# ``lifecycle`` key: their worker never said how it ended, and their CLI wrote
# ``stopped`` itself.
LIFECYCLE_VERSION = 2

_DURATION_RE = re.compile(r"(\d+)([smhd])$")

# How the worker's command line carries its token (see ``SessionMeta``).
WORKER_TOKEN_FLAG = "--worker-token"
# One `ps` question about the host's process table.
PS_TIMEOUT_SECONDS = 5
# How long the CLI waits for a session's lock -- the wait, not the hold: a
# --stop holds it while it asks `ps` twice (2 x PS_TIMEOUT_SECONDS) and while
# it builds the summary from the events file, a --get-details while it reads;
# a worker's writes are milliseconds.
CLI_LOCK_TIMEOUT_SECONDS = 30
# How long the worker waits for it: past any --stop's hold.
WORKER_LOCK_TIMEOUT_SECONDS = 60
# And for its last write, how the session ended: longer still, since without
# it the session can only be refused, or abandoned (review of PR #44).
WORKER_END_LOCK_TIMEOUT_SECONDS = 300
_LOCK_POLL_SECONDS = 0.02

# Under the storage root, beside the sessions and never one of them: the
# session locks (``<id>.lock``) and the base guard every opener takes first.
LOCKS_DIRNAME = "locks"
_BASE_GUARD_NAME = ".base"


def _default_sessions_dir() -> Path:
    """Resolve the storage root, honouring the ``ANDROID_EMU_ANR_HOME`` override.

    Read lazily (not at import time) so tests that set the env var in a fixture
    take effect, and so each ``SessionStore()`` picks up the current value.
    """
    override = os.environ.get("ANDROID_EMU_ANR_HOME", "").strip()
    if override:
        return Path(override).expanduser() / "anr-sessions"
    return Path("~/.android-emulator-skill/anr-sessions").expanduser()


def _default_ttl_hours() -> int:
    """Session prune age, read lazily so env overrides apply per call."""
    return env_int("ANDROID_EMU_ANR_SESSION_TTL_HOURS", 24)


def _now_ms() -> int:
    return int(time.time() * 1000)


# === ERRORS ===


class SessionBusyError(RuntimeError):
    """The session's lock was held past the bound by another writer; nothing was written."""

    def __init__(self, session_id: str, seconds: float) -> None:
        super().__init__(
            f"Session {session_id} is being written by another process: its lock was "
            f"held for {seconds:g}s."
        )
        self.remedy = (
            "Run the same command again; the other writer -- a --stop, or the session's "
            "worker -- finishes in seconds."
        )


class StoreBusyError(SessionBusyError):
    """The store's base guard was held past the bound (a sweep under way); nothing was removed."""

    def __init__(self, seconds: float) -> None:
        RuntimeError.__init__(
            self,
            f"The ANR session store is being swept by another process: its guard was held "
            f"for {seconds:g}s.",
        )
        self.remedy = "Run the same command again; the other command's sweep finishes in seconds."


class TransitionRefusedError(RuntimeError):
    """The session is not in a state the transition starts from, so it was not applied.

    Attributes:
        found: The session's status as it was found, under the lock.
    """

    def __init__(self, session_id: str, transition: str, found: str, why: str = "") -> None:
        reason = why or f"it is {found}"
        super().__init__(f"Session {session_id} was not {transition}: {reason}.")
        self.found = found


# === TYPES ===


@dataclass(frozen=True)
class CapPrune:
    """What one aggregate-cap prune did, and what it could not (best effort, D11).

    Attributes:
        deleted: Sessions removed.
        total_bytes: What the sessions hold afterwards.
        max_bytes: The cap.
        protected: Sessions it had to keep: not ended, or held by another command.
    """

    deleted: int = 0
    total_bytes: int = 0
    max_bytes: int = 0
    protected: int = 0

    @property
    def over_cap(self) -> dict | None:
        """``{"bytes_over", "protected"}`` when the sessions it may not remove exceed the cap."""
        if self.max_bytes <= 0 or self.total_bytes <= self.max_bytes:
            return None
        return {"bytes_over": self.total_bytes - self.max_bytes, "protected": self.protected}


@dataclass
class SessionMeta:
    """Parent + worker writes to meta.json."""

    session_id: str
    started_at: str
    started_at_ms: int
    args: dict
    pid: int | None = None
    status: str = PENDING
    stopped_at: str | None = None
    stopped_at_ms: int | None = None
    extras: dict = field(default_factory=dict)
    # The fingerprint version the session's events are captured under. A
    # session recorded before this was kept is v1, the only version there was.
    fingerprint_version: int = 1
    # Who the worker is, beyond a pid the OS reuses: a random token the parent
    # puts on the worker's command line, and the start time `ps` gave for the
    # pid when the worker claimed it. A pid that no longer shows both is not
    # this session's worker, and is never signalled.
    worker_token: str | None = None
    worker_started: str | None = None
    # The lifecycle the session's writers follow (``LIFECYCLE_VERSION``);
    # None for a session written before there was one.
    lifecycle: int | None = None
    # When a worker presenting the session's token claimed it: --start's
    # evidence that its worker registered, and nothing else is.
    claimed_at_ms: int | None = None

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "started_at_ms": self.started_at_ms,
            "args": self.args,
            "pid": self.pid,
            "status": self.status,
            "stopped_at": self.stopped_at,
            "stopped_at_ms": self.stopped_at_ms,
            "extras": self.extras,
            "fingerprint_version": self.fingerprint_version,
            "worker_token": self.worker_token,
            "worker_started": self.worker_started,
            "lifecycle": self.lifecycle,
            "claimed_at_ms": self.claimed_at_ms,
        }

    @classmethod
    def from_json(cls, payload: dict) -> SessionMeta:
        return cls(
            session_id=payload["session_id"],
            started_at=payload["started_at"],
            started_at_ms=payload["started_at_ms"],
            args=payload.get("args", {}),
            pid=payload.get("pid"),
            status=payload.get("status", PENDING),
            stopped_at=payload.get("stopped_at"),
            stopped_at_ms=payload.get("stopped_at_ms"),
            extras=payload.get("extras", {}),
            fingerprint_version=payload.get("fingerprint_version", 1),
            worker_token=payload.get("worker_token"),
            worker_started=payload.get("worker_started"),
            lifecycle=payload.get("lifecycle"),
            claimed_at_ms=payload.get("claimed_at_ms"),
        )

    @property
    def counters(self) -> dict:
        """The worker's line counters, as it last wrote them ({} before it has)."""
        counters = self.extras.get("line_counters", {})
        return counters if isinstance(counters, dict) else {}


def _ps(pid: int, field: str) -> str | None:
    """One ``ps`` field of host process ``pid``; None when ``ps`` lists no such process.

    In the C locale and UTC, so the start time reads the same whoever asks --
    the worker when it claims its pid, a later `--stop` in another shell.

    Raises:
        OSError: ``ps`` could not be run.
        subprocess.TimeoutExpired: It did not answer in ``PS_TIMEOUT_SECONDS``.
    """
    result = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=PS_TIMEOUT_SECONDS,
        check=False,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )
    answer = result.stdout.strip()
    return answer if result.returncode == 0 and answer else None


def process_started(pid: int) -> str | None:
    """When host process ``pid`` started (``ps -o lstart``); None when unknown."""
    try:
        return _ps(pid, "lstart")
    except (OSError, subprocess.SubprocessError):
        return None


def _exists(pid: int) -> bool:
    """Whether host process ``pid`` exists (one this user may not signal counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Identity(StrEnum):
    """What the process at a session's recorded pid is."""

    # Shown to be the worker: its token on its command line, started when it claimed.
    WORKER = "worker"
    # Shown not to be: no such process, or one that is not the worker. The
    # worker has exited -- a pid is reused only after its process has gone.
    GONE = "gone"
    # Neither: it may be the worker, still at work, and must not be taken for gone.
    UNRESOLVED = "unresolved"


def worker_identity(meta: SessionMeta) -> tuple[Identity, str]:
    """Whether the process at ``meta.pid`` is the session's worker, and why it reads so.

    The worker is shown only by a live process whose command line carries the
    session's ``worker_token`` and that started when the worker claimed the
    pid. A crashed or killed worker's pid is the OS's to give to anything;
    signalling it without this check sent SIGTERM to whatever held the pid by
    then (Codex review of PR #34).

    A start time the claim could not record (`ps` did not answer then) proves
    nothing either way: a live process with the token is UNRESOLVED, not a
    later process -- taking it for one, --stop forgot a live worker and
    saved a summary at exit 0 (fourth review of PR #42).
    """
    pid = meta.pid
    if not pid:
        return Identity.GONE, "no worker pid is recorded"
    try:
        command = _ps(pid, "command")
        started = _ps(pid, "lstart")
    except (OSError, subprocess.SubprocessError) as error:
        if not _exists(pid):
            return Identity.GONE, f"pid {pid} has exited"
        return Identity.UNRESOLVED, f"`ps` could not say what pid {pid} is ({error})"
    if command is None or started is None:
        if _exists(pid):
            return Identity.UNRESOLVED, f"`ps` lists no pid {pid}, yet a process has it"
        return Identity.GONE, f"pid {pid} has exited"
    if not meta.worker_token or f"{WORKER_TOKEN_FLAG} {meta.worker_token}" not in command:
        return Identity.GONE, (
            f"pid {pid} is another process now (its command line has no "
            f"{WORKER_TOKEN_FLAG} for this session)"
        )
    if meta.worker_started is None:
        return Identity.UNRESOLVED, (
            f"pid {pid} carries this session's worker token, but the worker's start time "
            f"was not recorded when it claimed the session (`ps` did not answer then), so "
            f"it cannot be told from a later process"
        )
    if started != meta.worker_started:
        return Identity.GONE, (
            f"pid {pid} started at {started}, not when this session's worker did "
            f"({meta.worker_started})"
        )
    return Identity.WORKER, f"pid {pid} is the session's worker"


# === SESSION STORE ===


class SessionStore:
    """Filesystem-backed session repository."""

    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir).expanduser() if base_dir else _default_sessions_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Outside every session's directory: deleting a session never unlinks
        # a lock someone holds (D11).
        self.locks_dir = self.base_dir / LOCKS_DIRNAME
        self.locks_dir.mkdir(exist_ok=True)
        # (thread, session id) for each lock this store holds: a write checks
        # its writer holds the session's lock (``_require_lock``).
        self._held: set[tuple[int, str]] = set()

    # === PUBLIC API ===

    def create(self, args: dict) -> SessionMeta:
        """Generate id + dir + initial meta.json. Caller detaches the worker next.

        All of it under the session's lock, taken before the directory exists
        (D11): a sweep finds a directory with no meta only while its creator
        holds the lock -- and skips it -- or once the creator has died, when
        it removes it. Meta is written first, so the directory is a session
        from the first moment anything without the lock can see it.
        """
        session_id = _generate_session_id()
        session_dir = self.session_dir(session_id)
        now = datetime.now()
        meta = SessionMeta(
            session_id=session_id,
            started_at=now.isoformat(),
            started_at_ms=int(now.timestamp() * 1000),
            args=args,
            status=PENDING,
            # The worker this session's events come from is spawned from the
            # same code, so this is the version they are fingerprinted under.
            fingerprint_version=FINGERPRINT_VERSION,
            worker_token=secrets.token_hex(8),
            lifecycle=LIFECYCLE_VERSION,
        )
        with self._lock(session_id, CLI_LOCK_TIMEOUT_SECONDS):
            session_dir.mkdir(parents=True, exist_ok=False)
            self._write_meta(meta)
            # Empty events file so the worker can `open(..., 'a')` cleanly.
            (session_dir / "events.jsonl").touch()
        return meta

    @contextlib.contextmanager
    def transaction(
        self, session_id: str, timeout: float = CLI_LOCK_TIMEOUT_SECONDS
    ) -> Iterator[SessionMeta]:
        """Hold the session's lock, and yield its meta as it is now.

        Every read-modify-write of a session goes through here: what is read
        inside is what is written over, so no writer puts back a meta another
        has changed since it read it (fourth review of PR #42: --stop wrote
        the meta it had loaded after the worker had persisted a device loss,
        erasing it). The lock is not re-entrant: nothing inside takes it again.

        Raises:
            SessionBusyError: The lock was held past ``timeout`` seconds.
            FileNotFoundError: The session does not exist.
            ValueError: ``session_id`` is not a plain session id.
        """
        with self._lock(session_id, timeout):
            yield self.load_meta(session_id)

    def wait_for_worker(self, session_id: str, timeout_seconds: float = 2.0) -> SessionMeta:
        """Poll meta.json until a worker has claimed the session, or time out. Raises on timeout.

        Claimed is ``claimed_at_ms``, which only ``claim_worker`` writes -- of a
        pending session, for a worker presenting the session's token, in the
        same write that makes it running -- so the evidence is this session's
        own worker, whatever it has done since (it may have ended already:
        CI on PR #42). Counters or a start time lying in the meta are not:
        they were evidence before, whoever wrote them (fourth review of PR #42).
        """
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            meta = self.load_meta(session_id)
            if meta.claimed_at_ms is not None and meta.status not in (PENDING, CANCELLED):
                return meta
            time.sleep(0.05)
        raise TimeoutError(f"Worker for {session_id} did not register within {timeout_seconds}s")

    # --- the worker's transitions --------------------------------------------

    def claim_worker(self, session_id: str, pid: int, token: str | None) -> SessionMeta:
        """pending -> running, for the worker presenting the session's token.

        Records the pid, its start time and when it claimed. A session that is
        not pending -- cancelled by a --stop or a --start that gave up on its
        worker, or claimed already -- is refused, as is a token that is not
        the session's: a worker that claimed a session already stopped went
        on to persist a device loss into it (fourth review of PR #42).

        Raises:
            TransitionRefusedError: Not pending, or not this session's token.
        """
        # Asked before the lock: `ps` can take its bound, and the worker's
        # start time is its own whenever it is asked.
        started = process_started(pid)
        with self.transaction(session_id, WORKER_LOCK_TIMEOUT_SECONDS) as meta:
            if not token or token != meta.worker_token:
                raise TransitionRefusedError(
                    session_id, "claimed", meta.status, "the worker's token is not the session's"
                )
            if meta.status != PENDING:
                raise TransitionRefusedError(session_id, "claimed", meta.status)
            meta.pid = pid
            meta.worker_started = started
            meta.claimed_at_ms = _now_ms()
            meta.status = RUNNING
            self._write_meta(meta)
            return meta

    def persist_worker_counters(
        self,
        session_id: str,
        pid: int,
        counters: dict,
        timeout: float = WORKER_LOCK_TIMEOUT_SECONDS,
    ) -> SessionMeta:
        """The worker's counters, while it is at work: written, its status unchanged.

        Raises:
            TransitionRefusedError: The session is no longer this worker's at work
                (it was abandoned, or its pid is not ``pid``).
            SessionBusyError: The lock was held past ``timeout``.
        """
        return self._worker_write(session_id, pid, counters, LIVE_STATES, None, timeout)

    def worker_deciding(self, session_id: str, pid: int, counters: dict) -> SessionMeta:
        """running -> deciding: the worker is deciding how its stream ended.

        --stop waits for a deciding worker to its own bound: the answer may be
        the session's refusal (second review of PR #42).
        """
        return self._worker_write(session_id, pid, counters, {RUNNING}, DECIDING)

    def worker_resumed(self, session_id: str, pid: int, counters: dict) -> SessionMeta:
        """deciding -> running: decided, and the stream is restarted."""
        return self._worker_write(session_id, pid, counters, {DECIDING}, RUNNING)

    def finish_worker(
        self,
        session_id: str,
        pid: int,
        counters: dict,
        timeout: float = WORKER_LOCK_TIMEOUT_SECONDS,
    ) -> SessionMeta:
        """running | deciding -> crashed (counters hold a stream failure) | stopped.

        The worker's last write, before it exits, on every path that can take
        the lock (``timeout``: the worker passes
        ``WORKER_END_LOCK_TIMEOUT_SECONDS``): the end, its
        counters and any failure in one write, so nothing ever reads a failure
        under a status that says the session is still recording (fourth review
        of PR #42: --start answered `{"success": true}` between the two writes
        that published them). Its pid is cleared: the OS may reuse it now.
        """
        end = CRASHED if counters.get("stream_failure") else STOPPED
        return self._worker_write(session_id, pid, counters, LIVE_STATES, end, timeout)

    # --- the CLI's transitions (inside a ``transaction``) --------------------

    def mark_cancelled(self, meta: SessionMeta) -> None:
        """pending -> cancelled: no worker claimed it, and none may now.

        Raises:
            TransitionRefusedError: The session is not pending (a worker claimed it).
        """
        if meta.status != PENDING:
            raise TransitionRefusedError(meta.session_id, "cancelled", meta.status)
        meta.status = CANCELLED
        self._mark_ended(meta)
        self._write_meta(meta)

    def mark_abandoned(self, meta: SessionMeta, why: str) -> None:
        """running | deciding -> abandoned: --stop --abandon gives up on the worker.

        Whatever meta write the worker would still make is refused from now
        on. Its event appends are not locked, so it may still append; the
        session's events are fixed at what the file held now, to its last
        whole line (``abandoned_events_bytes``), and every later answer reads
        that much only -- every --stop rebuilt the summary from the file as
        it had grown (review of PR #44).

        Raises:
            TransitionRefusedError: The worker is not at work (it has ended, or never began).
        """
        if meta.status not in LIVE_STATES:
            raise TransitionRefusedError(meta.session_id, "abandoned", meta.status)
        meta.status = ABANDONED
        meta.extras["abandoned"] = why
        meta.extras["abandoned_events_bytes"] = _whole_lines(self._events_path(meta.session_id))
        self._mark_ended(meta)
        self._write_meta(meta)

    def save_summary(self, meta: SessionMeta, summary: SessionSummary) -> None:
        """Persist the summary --stop computed, for a session that has ended.

        Raises:
            TransitionRefusedError: The session has not ended: its worker may still
                write what the summary would miss.
        """
        if meta.status not in TERMINAL_STATES:
            raise TransitionRefusedError(meta.session_id, "summarised", meta.status)
        self._write_summary(meta.session_id, summary)

    def stop(self, session_id: str, summary: SessionSummary) -> SessionMeta:
        """Persist ``summary`` for a session that has ended (:meth:`save_summary`), under its lock."""
        with self.transaction(session_id) as meta:
            self.save_summary(meta, summary)
            return meta

    # --- reading --------------------------------------------------------------

    def load_meta(self, session_id: str) -> SessionMeta:
        """meta.json as it is now. A whole file always: every write is a rename into place."""
        path = self._meta_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"No meta.json for session {session_id}")
        with open(path) as handle:
            return SessionMeta.from_json(json.load(handle))

    def load_summary(self, session_id: str) -> SessionSummary | None:
        path = self._summary_path(session_id)
        if not path.exists():
            return None
        with open(path) as handle:
            return summary_from_json(json.load(handle))

    def read_raw_events(self, session_id: str) -> str:
        """events.jsonl as the session's answers read it: all of it, or an abandoned one's prefix.

        An abandoned session's events end where its abandonment fixed them
        (``abandoned_events_bytes``): its worker may still append, unlocked.
        """
        path = self._events_path(session_id)
        if not path.exists():
            return ""
        with open(path, "rb") as handle:
            data = handle.read(self._events_limit(session_id))
        return data.decode("utf-8", errors="replace")

    def read_events(self, session_id: str) -> list:
        """Read events.jsonl lines, returning NormalisedEvent instances.

        Skips non-event sentinel lines (e.g. ``{"event": "stream_ended"}``),
        and reads an abandoned session's events only as far as its
        abandonment fixed them (:meth:`read_raw_events`).
        """
        events = []
        for raw in self.read_raw_events(session_id).splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip non-event sentinel lines (e.g. {"event": "stream_ended"}).
            if "event" in payload and "fingerprint" not in payload:
                continue
            try:
                events.append(event_from_jsonl(line))
            except (json.JSONDecodeError, KeyError):
                continue
        return events

    def events_path(self, session_id: str) -> Path:
        """Worker writes here. Public so the worker can open it line-buffered."""
        return self._events_path(session_id)

    def session_dir(self, session_id: str) -> Path:
        """Directory for one session, validating the id first.

        Session ids reach this from argv -- ``anr_watcher --get-details SID``
        and ``--stop SID`` -- and joining an unvalidated string onto the storage
        root lets a traversing id read ``<root>/../../x/meta.json`` and, on the
        stop path, *write* meta.json and summary.json there.

        Args:
            session_id: Caller-supplied session id.

        Returns:
            The session directory, always directly under the storage root.

        Raises:
            ValueError: If the id is not a plain session id.
        """
        if (
            not session_id
            or not _SAFE_SESSION_ID_RE.fullmatch(session_id)
            or session_id == LOCKS_DIRNAME
        ):
            raise ValueError(
                f"Invalid session id {session_id!r}. Expected a plain id like "
                f"'anr-20260617-143000-a1b2' (letters, digits, dash, underscore)."
            )
        return self.base_dir / session_id

    def session_total_bytes(self, session_id: str) -> int:
        """Sum of all files under a session dir. Used by aggregate-cap pruning."""
        total = 0
        session_path = self.session_dir(session_id)
        if not session_path.exists():
            return 0
        for path in session_path.rglob("*"):
            if path.is_file():
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
        return total

    def prune_to_aggregate_cap(self, max_bytes: int) -> CapPrune:
        """Drop the oldest ended sessions until total bytes <= max_bytes: best effort.

        Pairs with ``prune_expired``: TTL handles age, this handles disk usage
        when activity outpaces TTL. Both are called on every --start, so the
        user never has to clean up manually. Only what a sweep may remove goes
        (``_evict``); when the sessions it may not -- pending, running,
        deciding, or held by another command -- exceed the cap on their own,
        it stops there and says so (``CapPrune.over_cap``). It never refuses
        anything: a --start that cannot free space is worse than an overrun.
        """
        if max_bytes <= 0:
            return CapPrune(max_bytes=max_bytes)
        with self._base_guard():
            # (started_at_ms, session_id, bytes); a directory with no meta
            # sorts oldest: an interrupted creation, if its lock is free.
            entries: list[tuple[int, str, int]] = []
            total = 0
            for session_id in self._session_ids():
                size = self.session_total_bytes(session_id)
                total += size
                try:
                    started = self.load_meta(session_id).started_at_ms
                except (FileNotFoundError, json.JSONDecodeError):
                    started = 0
                entries.append((started, session_id, size))
            deleted = 0
            protected = 0
            entries.sort(key=lambda e: e[0])  # oldest first
            for _, session_id, size in entries:
                if total <= max_bytes:
                    break
                if self._evict(session_id, lambda _meta: True):
                    total -= size
                    deleted += 1
                else:
                    protected += 1
            self._sweep_orphaned_locks()
        return CapPrune(
            deleted=deleted, total_bytes=total, max_bytes=max_bytes, protected=protected
        )

    def list_sessions(self) -> list[SessionMeta]:
        """All session metas, newest first."""
        metas: list[SessionMeta] = []
        for session_id in self._session_ids():
            try:
                metas.append(self.load_meta(session_id))
            except (FileNotFoundError, json.JSONDecodeError):
                continue
        metas.sort(key=lambda m: m.started_at_ms, reverse=True)
        return metas

    def clear(self, older_than: str | None = None) -> int:
        """Delete ended sessions. ``older_than`` is a duration string like ``24h``.

        Only what a sweep may remove (``_evict``): a pending session is
        --stop's to cancel, and a running one's worker may still write it.
        """
        cutoff_ms = _resolve_cutoff_ms(older_than) if older_than else None
        return self._sweep(cutoff_ms)

    def prune_expired(self, ttl_hours: int | None = None) -> int:
        """Remove ended sessions older than ttl. Called on every --start."""
        ttl = ttl_hours if ttl_hours is not None else _default_ttl_hours()
        cutoff = int((datetime.now() - timedelta(hours=ttl)).timestamp() * 1000)
        return self._sweep(cutoff)

    # === SUMMARY HELPERS ===

    def build_summary(
        self,
        session_id: str,
        matched_lines: int = 0,
        total_lines: int = 0,
        dropped_below_threshold: int = 0,
        extras: dict | None = None,
        top_n: int | None = None,
    ) -> SessionSummary:
        """Convenience: read events.jsonl and run the pipeline through SummaryBuilder.

        Duration prefers ``meta.stopped_at_ms`` (set when the session ended:
        by its worker, or by the CLI that cancelled or abandoned it) so
        summaries reflect the actual capture window, not the time of
        inspection. Live sessions without ``stopped_at_ms`` fall back to
        ``now`` as before.
        """
        meta = self.load_meta(session_id)
        events = self.read_events(session_id)
        end_ms = meta.stopped_at_ms or int(datetime.now().timestamp() * 1000)
        duration_ms = end_ms - meta.started_at_ms
        builder = SummaryBuilder(
            session_id=session_id,
            started_at=meta.started_at,
            duration_ms=max(0, duration_ms),
            matched_lines=matched_lines,
            total_lines=total_lines,
            dropped_below_threshold=dropped_below_threshold,
            extras=extras or {},
            # The capture's version, not this code's: `--stop` on a session
            # recorded before a fingerprint change must not stamp its old
            # fingerprints with the new version (review of PR #26).
            fingerprint_version=meta.fingerprint_version,
        )
        return builder.build(events, top_n=top_n)

    # === PRIVATE ===

    def _meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def _events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def _summary_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "summary.json"

    def _events_limit(self, session_id: str) -> int:
        """How many bytes of events.jsonl the session's answers read: -1 for all of it."""
        try:
            meta = self.load_meta(session_id)
        except (FileNotFoundError, json.JSONDecodeError):
            return -1
        limit = meta.extras.get("abandoned_events_bytes")
        return limit if meta.status == ABANDONED and isinstance(limit, int) else -1

    def _lock_path(self, session_id: str) -> Path:
        """A session's lock file: ``locks/<id>.lock``, outside the directory a sweep deletes.

        The one place it is named (D11): creation, every transaction and every
        sweep reach the lock through here.

        Raises:
            ValueError: ``session_id`` is not a plain session id.
        """
        self.session_dir(session_id)
        return self.locks_dir / f"{session_id}.lock"

    @property
    def _base_guard_path(self) -> Path:
        return self.locks_dir / _BASE_GUARD_NAME

    @contextlib.contextmanager
    def _lock(self, session_id: str, timeout: float) -> Iterator[None]:
        """Hold the session's lock exclusively, waiting at most ``timeout`` seconds.

        ``flock`` belongs to the open file, so two holders conflict whether
        they are two processes or two threads of one; it is released when the
        file is closed -- here, or by the kernel when a holder dies.

        Base -> session (D11): each attempt takes the base guard, opens the
        lock file and tries it under the guard, and lets the guard go. A sweep
        unlinks a lock file only holding the guard, so the file an attempt
        opens is the one its path names until the attempt holds it; and a
        waiter keeps no descriptor between attempts, which a sweep could
        otherwise leave naming an unlinked file no later opener reaches.
        """
        path = self._lock_path(session_id)
        key = (threading.get_ident(), session_id)
        deadline = time.monotonic() + timeout
        while (fd := self._attempt(path)) is None:
            if time.monotonic() >= deadline:
                raise SessionBusyError(session_id, timeout)
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            self._held.add(key)
            try:
                yield
            finally:
                self._held.discard(key)
        finally:
            os.close(fd)

    def _attempt(self, path: Path) -> int | None:
        """One try at a session's lock, under the base guard: its descriptor, or None (busy)."""
        guard = _try_flock(self._base_guard_path)
        if guard is None:
            return None
        try:
            return _try_flock(path)
        finally:
            os.close(guard)

    @contextlib.contextmanager
    def _base_guard(self, timeout: float = CLI_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Hold the base guard for a sweep: while it is held, no session lock is newly taken.

        Raises:
            StoreBusyError: It was held past ``timeout`` seconds.
        """
        deadline = time.monotonic() + timeout
        while (fd := _try_flock(self._base_guard_path)) is None:
            if time.monotonic() >= deadline:
                raise StoreBusyError(timeout)
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            os.close(fd)

    def _require_lock(self, session_id: str) -> None:
        """A write is only ever made by a holder of the session's lock (``transaction``)."""
        if (threading.get_ident(), session_id) not in self._held:
            raise RuntimeError(
                f"session {session_id} written without its lock; this is a defect in the skill"
            )

    def _worker_write(
        self,
        session_id: str,
        pid: int,
        counters: dict,
        expected: Iterable[str],
        to: str | None,
        timeout: float = WORKER_LOCK_TIMEOUT_SECONDS,
    ) -> SessionMeta:
        """One write of the worker's: its counters, and ``to`` (None: the status kept).

        Applied only while the session is in one of ``expected`` and still
        claimed by ``pid``; refused otherwise, so a worker the CLI has given up
        on, or one that is not the session's, changes nothing.
        """
        with self.transaction(session_id, timeout) as meta:
            if meta.status not in expected or meta.pid != pid:
                why = (
                    f"it is {meta.status}"
                    if meta.status not in expected
                    else f"pid {pid} is not its worker"
                )
                raise TransitionRefusedError(
                    session_id, f"written by its worker ({to})", meta.status, why
                )
            meta.extras["line_counters"] = counters
            if to is not None:
                meta.status = to
                if to in TERMINAL_STATES:
                    self._mark_ended(meta)
                    # The worker is exiting: its pid is about to be the OS's to reuse.
                    meta.pid = None
            self._write_meta(meta)
            return meta

    @staticmethod
    def _mark_ended(meta: SessionMeta) -> None:
        """When capture ended, for the summary's duration and --list-sessions."""
        now = datetime.now()
        meta.stopped_at = now.isoformat()
        meta.stopped_at_ms = int(now.timestamp() * 1000)

    def _write_meta(self, meta: SessionMeta) -> None:
        self._require_lock(meta.session_id)
        self._replace(self._meta_path(meta.session_id), meta.to_json())

    def _write_summary(self, session_id: str, summary: SessionSummary) -> None:
        self._require_lock(session_id)
        self._replace(self._summary_path(session_id), summary_to_json(summary))

    @staticmethod
    def _replace(path: Path, payload: dict) -> None:
        """Write ``payload`` to ``path`` atomically, through a temporary file of its own.

        Concurrent readers (--start polling, --list-sessions) never see a
        half-file. The temporary name is unique to the write (``mkstemp``): a
        fixed ``<name>.tmp`` let a second writer truncate and rename the first's
        file. fsync before the rename makes the new contents durable, not just
        atomically renamed.
        """
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise

    def _session_ids(self) -> list[str]:
        """The sessions under the root: directories named as ``create`` names them.

        Not ``locks/``, and not whatever else is kept here: every walker took
        every directory for a session -- a prune removed it for having no meta,
        and a listing raised on a name no session id can be (D11).
        """
        return [
            entry.name
            for entry in self.base_dir.iterdir()
            if _GENERATED_SESSION_ID_RE.fullmatch(entry.name)
            and entry.is_dir()
            and not entry.is_symlink()
        ]

    def _sweep(self, cutoff_ms: int | None) -> int:
        """Remove what may go (``_evict``) and started at or before ``cutoff_ms`` (None: any)."""
        deleted = 0
        with self._base_guard():
            for session_id in self._session_ids():
                if self._evict(
                    session_id, lambda meta: cutoff_ms is None or meta.started_at_ms <= cutoff_ms
                ):
                    deleted += 1
            self._sweep_orphaned_locks()
        return deleted

    def _evict(self, session_id: str, wanted: Callable[[SessionMeta], bool]) -> bool:
        """Remove one session if, under its lock, it may go and is ``wanted``. Hold the base guard.

        The lock is tried without waiting: one that is held is a writer at
        work, or a creator between its directory and its meta, and the
        session is skipped. Under it the meta is read again. A session goes
        when nothing can write it again (``_ended_for_good``); a directory with
        no meta goes too -- its lock was free, so its creator died before
        writing it. Its lock file is unlinked last, while this sweep holds
        both it and the base guard: no one else has it open.
        """
        fd = _try_flock(self._lock_path(session_id))
        if fd is None:
            return False
        try:
            session_dir = self.session_dir(session_id)
            if not session_dir.is_dir():
                return False
            try:
                meta = self.load_meta(session_id)
            except (FileNotFoundError, json.JSONDecodeError):
                meta = None
            if meta is not None and not (_ended_for_good(meta) and wanted(meta)):
                return False
            _remove_tree(session_dir)
            with contextlib.suppress(FileNotFoundError):
                self._lock_path(session_id).unlink()
            return True
        finally:
            os.close(fd)

    def _sweep_orphaned_locks(self) -> int:
        """Unlink the lock files whose session directory is gone. Hold the base guard.

        Each only while holding it too, its directory still absent then:
        never a lock someone holds -- a creator holds its own before its
        directory exists -- and never one whose session exists.
        """
        unlinked = 0
        for path in self.locks_dir.glob("*.lock"):
            session_id = path.name.removesuffix(".lock")
            if not _SAFE_SESSION_ID_RE.fullmatch(session_id) or session_id == LOCKS_DIRNAME:
                continue
            if self.session_dir(session_id).exists():
                continue
            fd = _try_flock(path)
            if fd is None:
                continue
            try:
                if not self.session_dir(session_id).exists():
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                    unlinked += 1
            finally:
                os.close(fd)
        return unlinked


# Session ids are generated as "anr-<YYYYmmdd>-<HHMMSS>-<4 hex>". Anything
# outside this alphabet cannot be one, and a separator or ".." would let an
# id address paths outside the storage root.
_SAFE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# What ``_generate_session_id`` makes: the only directories a walker takes for sessions.
_GENERATED_SESSION_ID_RE = re.compile(r"anr-\d{8}-\d{6}-[0-9a-f]{4}")


# === MODULE-LEVEL HELPERS ===


def _generate_session_id() -> str:
    """``anr-YYYYMMDD-HHmmss-XXXX`` — random hex suffix avoids same-second collisions."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"anr-{timestamp}-{suffix}"


def _resolve_cutoff_ms(duration_str: str) -> int:
    """Parse e.g. ``24h``, ``30m`` and return the epoch-ms threshold."""
    match = _DURATION_RE.match(duration_str.strip().lower())
    if not match:
        raise ValueError(f"Invalid duration: {duration_str!r}. Use 30s/5m/24h/7d.")
    value, unit = int(match.group(1)), match.group(2)
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    cutoff = datetime.now() - timedelta(seconds=seconds)
    return int(cutoff.timestamp() * 1000)


def _ended_for_good(meta: SessionMeta) -> bool:
    """Whether nothing can write ``meta``'s session again, so a sweep may remove it.

    A session that has ended (``TERMINAL_STATES``), of this lifecycle: its
    worker published its end, or the CLI cancelled or abandoned it. Never
    one pending (a worker may yet claim it; --stop cancels it), running or
    deciding. An earlier version's (no ``lifecycle``) ended status is no
    promise -- its --stop could save ``stopped`` while its worker ran on, its
    pid kept -- so such a session goes only once that pid is shown not to be
    its worker: never while UNRESOLVED.
    """
    if meta.status not in TERMINAL_STATES:
        return False
    if meta.lifecycle is None and meta.pid:
        return worker_identity(meta)[0] is Identity.GONE
    return True


def _try_flock(path: Path) -> int | None:
    """Open ``path`` (made if absent) and lock it without waiting: its descriptor, or None (held).

    ``locks/`` itself is made again when it is gone: an earlier version's
    prune takes it for a session with no meta and deletes it (whatever it held
    then excludes no one -- the reason an upgrade is made quiescently).
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _whole_lines(path: Path) -> int:
    """The length of ``path`` up to and including its last newline (0 when there is none).

    A line still being appended -- unlocked, by a worker -- is not a whole one yet.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return 0
    return data.rfind(b"\n") + 1


def _remove_tree(path: Path) -> None:
    """rm -rf path. Used for session-dir cleanup."""
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            with contextlib.suppress(FileNotFoundError):
                child.unlink()
    with contextlib.suppress(OSError):
        path.rmdir()
