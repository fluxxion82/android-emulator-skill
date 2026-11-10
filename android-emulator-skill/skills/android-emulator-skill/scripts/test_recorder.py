#!/usr/bin/env python3
"""Command-driven test recorder: one session, many process invocations.

An agent drives a device one tool call at a time, and each call is a *separate
process*. The previous version of this file could only be driven from inside a
single Python process (``recorder.step(...)`` on a live object), so its CLI had
nothing to do: ``main()`` built a ``TestRecorder``, threw it away, and printed
instructions. Nothing was ever recorded.

The rebuild moves the recording state onto disk, so the unit of work is a
command rather than a method call:

    SID=$(python test_recorder.py --start "Login flow" --app-name MyApp)
    # ... drive the app with navigator.py / gesture.py ...
    python test_recorder.py --step "Open the app"
    python test_recorder.py --step "Tap sign in" --assert "Login form shown"
    python test_recorder.py --stop
    python test_recorder.py --list
    python test_recorder.py --get-details "$SID"

Session storage follows ``common/anr_sessions.py``: a directory per session
under ``~/.android-emulator-skill/test-sessions/<id>/`` holding ``meta.json``
(atomic tmp+replace writes), an append-only ``steps.jsonl``, and the per-step
artifacts. ``common/cache_utils.py`` is deliberately *not* reused — it stores
one flat file per entry and expires it, where a session is a directory that
grows step by step and must outlive the cache's TTL. User state never goes next to the
script: the skill ships as a plugin, and writing into the installed package was
a real bug (see ``emulator_selector``'s config path).

Two deviations from the surface sketched in the brief, both deliberate:

* **``--session ID`` is optional everywhere.** ``--step`` and ``--stop`` default
  to the newest session still in ``recording`` state, so the common flow needs
  no id plumbing at all; ``--start`` still prints the id for scripts that want
  to pin it, and ``--session`` targets a specific one when several are open.
* **No ``--inline`` base64 mode.** A session is file-backed by definition, and
  a CLI whose whole point is 3-line output has nowhere to put a megabyte of
  base64 — it would only bloat ``steps.jsonl``. Screenshots are written as PNG
  files and their paths are printed; an agent that wants to *see* one reads the
  file directly, which is cheaper than round-tripping it through this script.

``--clear`` was added because a session directory holds PNGs: without it the
only cleanup is the TTL sweep that runs on ``--start``.

Contracts this module is careful about, each of which has burned the repo
before:

* ``get_ui_hierarchy()`` returns ``{"tag", "attributes": {...}, "children": []}``
  and every UI field lives under ``attributes`` as a **string**.
* ``capture_screenshot()`` returns ``{"mode", "file_path", ...}`` and **raises**
  on failure; there is no ``success`` key to test.
* Anything crossing into the device shell is quoted with
  ``quote_for_device_shell`` — the per-step logcat marker carries a free-text
  description straight from the command line.

Exit status: ``--step`` fails only when a step captured *nothing* (both the
screenshot and the hierarchy failed), so a partial capture is still recorded
rather than silently lost. ``--stop --failed`` exits 1 so a CI wrapper can key
off it, and so does ``--stop`` on an incomplete recording.

A damaged ``steps.jsonl`` is disclosed, not refused. ``common/cache_utils.py``
refuses a corrupt entry because its remedy is cheap: delete the file and re-run
the command. A recording has no re-run -- the device has moved on -- and the
readable steps, with every step's screenshot and UI dump, are still worth
having; a refusal would also leave the session unstoppable until someone
hand-edited the file. So what reads is kept and what does not is counted
(:class:`StepLog`, :func:`_integrity`) and said by ``--stop``, ``--get-details``
and ``--list`` in text and JSON, in ``report.md`` and ``manifest.json``, and by
``--stop``'s exit status. Exit 0 from ``--stop`` means "passed, and every
recorded step is in the report".
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from common import cli
from common.device_utils import (
    build_adb_command,
    get_current_activity,
    get_ui_hierarchy,
    quote_for_device_shell,
    resolve_device_identifier,
)
from common.env_config import env_int
from common.screenshot_utils import capture_screenshot

# === CONSTANTS ===

# Max chars of a step description used when building artifact filenames.
STEP_NAME_MAXLEN = env_int("ANDROID_EMU_STEP_NAME_MAXLEN", 30)

# Labels kept on the step record for the report. The full hierarchy is on disk
# in the step's ui-dump, so this is a preview, not the data.
MAX_STEP_LABELS = 8

# The most steps one session holds. A step number is read back from a file
# anyone can edit and becomes the next number and its artifact names, so it has
# a ceiling: past it, a line is not a step and `--step` is refused.
MAX_STEP_NUMBER = 999_999

STATUS_RECORDING = "recording"
STATUS_STOPPED = "stopped"

SESSION_ID_PREFIX = "rec"
# rec-YYYYMMDD-HHMMSS-XXXX. Also the traversal guard: a session id arrives from
# the command line and is joined onto the storage root.
SESSION_ID_RE = re.compile(rf"^{SESSION_ID_PREFIX}-\d{{8}}-\d{{6}}-[0-9a-f]{{4}}$")

_DURATION_RE = re.compile(r"(\d+)([smhd])$")

# Logcat marker: lets a log_monitor/anr_watcher capture be lined up with the
# recorded steps afterwards. Best-effort — a device without `log` just records
# marker=False.
MARKER_TAG = "AndroidEmuRecorder"
MARKER_TIMEOUT = 10

SCREENSHOT_SIZES = ("full", "half", "quarter", "thumb")


class RecorderError(RuntimeError):
    """A recording command that cannot be carried out (bad id, wrong state)."""


def _default_sessions_dir() -> Path:
    """Storage root, honouring ``ANDROID_EMU_RECORDER_HOME``.

    Read lazily so a test (or a caller) that sets the env var after import
    still takes effect.
    """
    override = os.environ.get("ANDROID_EMU_RECORDER_HOME", "").strip()
    if override:
        return Path(override).expanduser() / "test-sessions"
    return Path("~/.android-emulator-skill/test-sessions").expanduser()


def _default_ttl_hours() -> int:
    """Session prune age. Sessions hold PNGs, so they do not live forever."""
    return env_int("ANDROID_EMU_RECORDER_SESSION_TTL_HOURS", 72)


# === TYPES ===


@dataclass
class SessionMeta:
    """Everything about a recording that is not a step."""

    session_id: str
    name: str
    started_at: str
    started_at_ms: int
    serial: str | None = None
    app_name: str | None = None
    screenshot_size: str = "half"
    status: str = STATUS_RECORDING
    step_count: int = 0
    capture_failures: int = 0
    passed: bool | None = None
    stopped_at: str | None = None
    stopped_at_ms: int | None = None
    extras: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "started_at": self.started_at,
            "started_at_ms": self.started_at_ms,
            "serial": self.serial,
            "app_name": self.app_name,
            "screenshot_size": self.screenshot_size,
            "status": self.status,
            "step_count": self.step_count,
            "capture_failures": self.capture_failures,
            "passed": self.passed,
            "stopped_at": self.stopped_at,
            "stopped_at_ms": self.stopped_at_ms,
            "extras": self.extras,
        }

    @classmethod
    def from_json(cls, payload: dict) -> SessionMeta:
        return cls(
            session_id=payload["session_id"],
            name=payload.get("name", ""),
            started_at=payload["started_at"],
            started_at_ms=payload["started_at_ms"],
            serial=payload.get("serial"),
            app_name=payload.get("app_name"),
            screenshot_size=payload.get("screenshot_size", "half"),
            status=payload.get("status", STATUS_RECORDING),
            step_count=payload.get("step_count", 0),
            capture_failures=payload.get("capture_failures", 0),
            passed=payload.get("passed"),
            stopped_at=payload.get("stopped_at"),
            stopped_at_ms=payload.get("stopped_at_ms"),
            extras=payload.get("extras", {}),
        )

    @property
    def duration_seconds(self) -> float:
        """Wall-clock length of the recording, live sessions measured to now."""
        end_ms = self.stopped_at_ms or int(datetime.now().timestamp() * 1000)
        return max(0.0, (end_ms - self.started_at_ms) / 1000.0)


@dataclass(frozen=True)
class StepLog:
    """What ``steps.jsonl`` holds, including what could not be read from it.

    Attributes:
        path: The steps file.
        steps: The step records that read, in file order.
        unreadable_lines: Non-blank lines that gave no step: damaged ones
            (cut short, not UTF-8, not JSON, or not a record the recorder
            could have written -- see :func:`_parse_step_line`) and repeats.
        repeated_lines: The part of ``unreadable_lines`` that is a readable
            record whose step number an earlier line already holds.
    """

    path: Path
    steps: list[dict]
    unreadable_lines: int
    repeated_lines: int

    def recorded(self, meta: SessionMeta) -> int:
        """How many steps this session has recorded: its highest step number.

        The file and meta each know part of it. Meta is written after the
        append, so it lags a step that landed and then lost its process; the
        file loses lines, or all of them. Meta and the highest number that
        reads can only undercount, so the largest claim wins.

        A damaged line counts as a step that was recorded, because a record
        cut short or corrupted is what one usually is; a line of junk that
        never was a step is counted the same way and cannot be told apart. A
        repeat is different: the step it names is already counted, so it adds
        nothing -- two readable rows that are both step 1 are one step, and a
        row pasted in twice must not raise the count ``--stop`` keeps.
        """
        highest = max((step["number"] for step in self.steps), default=0)
        damaged = self.unreadable_lines - self.repeated_lines
        return max(meta.step_count, highest, len(self.steps) + damaged)


def _integrity(meta: SessionMeta, log: StepLog) -> dict:
    """The disclosure every reading command carries, text and JSON alike.

    ``lost_steps`` is steps recorded but not readable; ``unreadable_lines`` is
    how many non-blank lines of the file gave no step. They differ when lines
    are gone altogether (a file emptied after two steps: 2 lost, 0 unreadable)
    and when a line repeats a step that is still there (0 lost, 1 unreadable).
    Either one makes the recording incomplete: a file holding a line the
    recorder did not write is not one whose step list can be vouched for.
    """
    lost = log.recorded(meta) - len(log.steps)
    return {
        "incomplete": lost > 0 or log.unreadable_lines > 0,
        "unreadable_lines": log.unreadable_lines,
        "lost_steps": lost,
        "steps_file": str(log.path),
    }


def _incomplete_note(integrity: dict, steps: int) -> str:
    """One line saying what is missing and which file to look at."""
    return (
        f"{integrity['lost_steps']} of {steps + integrity['lost_steps']} recorded step(s) lost; "
        f"{integrity['unreadable_lines']} unreadable line(s) in {integrity['steps_file']}"
    )


def _is_text(value: object) -> bool:
    """A string that can be written out: a lone surrogate parses and cannot."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_text_or_none(value: object) -> bool:
    return value is None or _is_text(value)


def _is_texts(value: object) -> bool:
    return isinstance(value, list) and all(_is_text(item) for item in value)


def _is_flag(value: object) -> bool:
    return isinstance(value, bool)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_step_number(value: object) -> bool:
    return _is_count(value) and 1 <= value <= MAX_STEP_NUMBER


def _is_seconds(value: object) -> bool:
    """A length of time a float holds: not NaN, not infinite, not negative."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return 0 <= float(value) < float("inf")
    except OverflowError:  # an integer of hundreds of digits
        return False


# What `summarize_hierarchy` writes under a step's `screen`.
_SCREEN_FIELDS = {
    "elements": _is_count,
    "clickable": _is_count,
    "with_text": _is_count,
    "labels": _is_texts,
    "label_total": _is_count,
    "package": _is_text_or_none,
}


def _is_screen(value: object) -> bool:
    return isinstance(value, dict) and all(
        key in _SCREEN_FIELDS and _SCREEN_FIELDS[key](item) for key, item in value.items()
    )


# Every field `TestRecorder.step` writes, and what it writes there. The table
# is closed: a field that is not in it, or a value its check refuses, makes the
# line unreadable. None is accepted only where the recorder writes None.
_STEP_FIELDS = {
    "number": _is_step_number,
    "description": _is_text,
    "recorded_at": _is_text,
    "elapsed_seconds": _is_seconds,
    "screenshot": _is_text_or_none,
    "ui_dump": _is_text_or_none,
    "screen": _is_screen,
    "activity": _is_text_or_none,
    "marker_logged": _is_flag,
    "errors": _is_texts,
    "captured": _is_flag,
    "screen_name": _is_text,
    "state": _is_text,
    "assertion": _is_text,
    "assertion_passed": _is_flag,
}
_REQUIRED_STEP_FIELDS = ("number", "description")


def _bad_field(record: dict) -> str | None:
    """The first field that keeps a dict from being a step record, or None."""
    for key, value in record.items():
        if key not in _STEP_FIELDS or not _STEP_FIELDS[key](value):
            return key
    return next((key for key in _REQUIRED_STEP_FIELDS if key not in record), None)


def _parse_step_line(raw: bytes) -> dict | None:
    """A step record, or None for a line that is not one.

    A line is read as bytes so that one that is not UTF-8 is one unreadable
    line rather than a decode error for the whole file. JSON that is not a
    step is unreadable too: ``null``, a list, an object without a step number
    and a description, or one holding anything :data:`_STEP_FIELDS` does not
    describe.

    The renderers are not what decides. Checking the fields the report
    happened to index left the next one unchecked each time -- a string no
    file can hold, a number no float can -- and ``--stop`` ended in a
    traceback. A record that passes here is one the recorder could have
    written, and those it can render.
    """
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # ValueError: not UTF-8, not JSON, or an integer too long to convert.
        # RecursionError: nested deeper than the parser goes.
        return None
    if not isinstance(record, dict) or _bad_field(record) is not None:
        return None
    return record


# === SESSION STORE ===


class RecorderSessionStore:
    """Filesystem-backed recording sessions, shared across process invocations.

    Layout, one directory per session::

        <base_dir>/<session-id>/meta.json        config + status + counters
                               /steps.jsonl      append-only, one step per line
                               /screenshots/NNN-slug.png
                               /ui-dumps/NNN-slug.json
                               /report.md        written by stop()
                               /manifest.json    written by stop()
    """

    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir).expanduser() if base_dir else _default_sessions_dir()
        # Every artifact path a step records starts here, and a record holds
        # only text that encodes (`_STEP_FIELDS`). Refused where the root is
        # read, so no command gets as far as a path it cannot write down.
        if not _is_text(str(self.base_dir)):
            raise RecorderError(
                f"The recorder's storage root {str(self.base_dir)!a} is not a path "
                "that encodes as UTF-8, so no step could record where its files are. "
                "Point ANDROID_EMU_RECORDER_HOME at a directory whose path does."
            )
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # === PUBLIC API ===

    def create(self, meta_fields: dict) -> SessionMeta:
        """Allocate an id, build the session tree, and write the initial meta.

        Raises:
            RecorderError: a field is text the report could not hold. meta.json
                would take it, as an escape; report.md is UTF-8 and would not,
                and that is found out at ``--stop``, with the steps taken.
        """
        for key, value in meta_fields.items():
            if isinstance(value, str) and not _is_text(value):
                raise RecorderError(
                    f"The session's {key.replace('_', ' ')} is not text that can be "
                    "stored: it does not encode as UTF-8."
                )
        session_id = _generate_session_id()
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=False)
        (session_dir / "screenshots").mkdir()
        (session_dir / "ui-dumps").mkdir()
        # Empty steps file so a later append (and a read before any step) is clean.
        self.steps_path(session_id).touch()

        now = datetime.now()
        meta = SessionMeta(
            session_id=session_id,
            started_at=now.isoformat(),
            started_at_ms=int(now.timestamp() * 1000),
            **meta_fields,
        )
        self.write_meta(meta)
        return meta

    def load_meta(self, session_id: str) -> SessionMeta:
        path = self._meta_path(session_id)
        if not path.exists():
            raise RecorderError(f"No such session: {session_id}")
        with open(path, encoding="utf-8") as handle:
            return SessionMeta.from_json(json.load(handle))

    def write_meta(self, meta: SessionMeta) -> None:
        """Atomic tmp+replace write, so a concurrent reader never sees a half-file."""
        path = self._meta_path(meta.session_id)
        _atomic_write_json(path, meta.to_json())

    def append_step(self, session_id: str, step: dict) -> None:
        """Append one step record, flushed and fsynced before returning.

        Durability matters more than throughput here: the next invocation is a
        different process, and a step that is only in a buffer is a step that
        never happened.

        A file that does not end in a newline ends in a record cut short. The
        new record starts on a line of its own, so the fragment stays one
        unreadable line instead of swallowing this step as well.

        This is the one place a step is written, so it is where the writer is
        held to the reader's table: a record `_parse_step_line` would not read
        back is refused, not appended as a line that is lost on arrival.

        Raises:
            RecorderError: the record holds a field the reader refuses.
        """
        bad = _bad_field(step)
        if bad is not None:
            raise RecorderError(
                f"Step {step.get('number')} was not recorded: its {bad.replace('_', ' ')} "
                f"is {ascii(step.get(bad))[:120]}, which the recorder could not read back."
            )
        line = json.dumps(step, ensure_ascii=False) + "\n"
        with open(self.steps_path(session_id), "a+b") as handle:
            if handle.seek(0, os.SEEK_END) > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    line = "\n" + line
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def read_steps(self, session_id: str) -> StepLog:
        """The steps file as it is: the records that read, and a count of the rest.

        Returns a :class:`StepLog`, never a bare list: a list of what parsed
        cannot say that anything did not, which is how a damaged file used to
        stop as a clean, shorter recording. A missing file reads as no lines;
        whether that is a loss is :func:`_integrity`'s call, against meta.
        """
        path = self.steps_path(session_id)
        steps: list[dict] = []
        numbers: set[int] = set()
        line_count = 0
        repeated = 0
        if path.exists():
            for raw in path.read_bytes().split(b"\n"):
                if not raw.strip():
                    continue
                line_count += 1
                record = _parse_step_line(raw)
                if record is None:
                    continue
                # A step number is handed out once, so a second line carrying
                # one is not a second step. The first is kept: an append never
                # lands before the line it copies.
                if record["number"] in numbers:
                    repeated += 1
                    continue
                numbers.add(record["number"])
                steps.append(record)
        return StepLog(
            path=path,
            steps=steps,
            unreadable_lines=line_count - len(steps),
            repeated_lines=repeated,
        )

    def list_sessions(self) -> list[SessionMeta]:
        """All sessions, newest first.

        Two sessions started in the same millisecond tie on ``started_at_ms``,
        and "newest first" is what ``--step`` resolves against — so the
        microsecond-precision ISO timestamp breaks the tie. It is fixed-width
        and zero-padded, so lexicographic order is chronological order.
        """
        metas: list[SessionMeta] = []
        for entry in self.base_dir.iterdir():
            if not entry.is_dir() or not SESSION_ID_RE.match(entry.name):
                continue
            try:
                metas.append(self.load_meta(entry.name))
            except (RecorderError, json.JSONDecodeError, KeyError):
                continue
        metas.sort(key=lambda m: (m.started_at_ms, m.started_at), reverse=True)
        return metas

    def active_session_id(self) -> str | None:
        """Newest session still recording, or None."""
        for meta in self.list_sessions():
            if meta.status == STATUS_RECORDING:
                return meta.session_id
        return None

    def resolve_session_id(self, session_id: str | None) -> str:
        """Explicit id if given, else the newest recording session."""
        if session_id:
            return _validate_session_id(session_id)
        active = self.active_session_id()
        if not active:
            raise RecorderError(
                "No recording session. Start one first:\n"
                '  python test_recorder.py --start "My test"'
            )
        return active

    def clear(self, older_than: str | None = None) -> int:
        """Delete session directories. ``older_than`` is e.g. ``24h``."""
        cutoff_ms = _resolve_cutoff_ms(older_than) if older_than else None
        return self._clear_older_than_ms(cutoff_ms)

    def prune_expired(self, ttl_hours: int | None = None) -> int:
        """Drop sessions past the TTL. Called on every ``create``."""
        ttl = ttl_hours if ttl_hours is not None else _default_ttl_hours()
        cutoff = int((datetime.now() - timedelta(hours=ttl)).timestamp() * 1000)
        return self._clear_older_than_ms(cutoff)

    # === PATHS ===

    def session_dir(self, session_id: str) -> Path:
        return self.base_dir / _validate_session_id(session_id)

    def steps_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "steps.jsonl"

    def screenshots_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "screenshots"

    def ui_dumps_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "ui-dumps"

    def report_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "report.md"

    def manifest_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "manifest.json"

    # === PRIVATE ===

    def _meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def _clear_older_than_ms(self, cutoff_ms: int | None) -> int:
        deleted = 0
        for entry in self.base_dir.iterdir():
            if not entry.is_dir() or not SESSION_ID_RE.match(entry.name):
                continue
            try:
                meta = self.load_meta(entry.name)
            except (RecorderError, json.JSONDecodeError, KeyError):
                _remove_tree(entry)
                deleted += 1
                continue
            if cutoff_ms is None or meta.started_at_ms <= cutoff_ms:
                _remove_tree(entry)
                deleted += 1
        return deleted


# === RECORDER ===


class TestRecorder:
    """Drives one recording command against a stored session."""

    __test__ = False  # production class, not a pytest test case

    def __init__(self, store: RecorderSessionStore | None = None):
        self.store = store or RecorderSessionStore()

    # === COMMANDS ===

    def start(
        self,
        name: str,
        serial: str | None = None,
        app_name: str | None = None,
        screenshot_size: str = "half",
    ) -> SessionMeta:
        """Open a session and return its meta. Prunes expired sessions first."""
        self.store.prune_expired()
        resolved = resolve_device_identifier(serial)
        return self.store.create(
            {
                "name": name,
                "serial": resolved,
                "app_name": app_name,
                "screenshot_size": screenshot_size,
            }
        )

    def step(
        self,
        description: str,
        session_id: str | None = None,
        screen_name: str | None = None,
        state: str | None = None,
        assertion: str | None = None,
        assertion_passed: bool = True,
        serial: str | None = None,
    ) -> dict:
        """Capture the current screen and append it to the session as a step.

        Returns the step record. A capture that fails is recorded as an error on
        the step rather than aborting the session; ``captured`` is False only
        when *neither* artifact landed.
        """
        session_id = self.store.resolve_session_id(session_id)
        meta = self.store.load_meta(session_id)
        if meta.status != STATUS_RECORDING:
            raise RecorderError(
                f"Session {session_id} is {meta.status}; start a new one to record more steps."
            )

        # One past the highest number this session has handed out, by whichever
        # of the file and meta knows more: a step that landed before its
        # invocation died is counted (meta lags), and so is a line that no
        # longer reads or is gone (the file lost it). Counting only the lines
        # that parsed gave a lost step's number, and its artifact names, away
        # again.
        number = self.store.read_steps(session_id).recorded(meta) + 1
        # Refused before anything is captured, for what is known before it:
        # the step's own text and its number. What the capture brings back is
        # held to the same table where it is written (`append_step`).
        asked = {"screen_name": screen_name, "state": state, "assertion": assertion}
        said = {key: value for key, value in asked.items() if value}
        bad = _bad_field({"number": number, "description": description, **said})
        if bad == "number":
            raise RecorderError(
                f"Session {session_id} is at step {number - 1}; a session holds at most "
                f"{MAX_STEP_NUMBER}. Stop it and start a new one."
            )
        if bad:
            raise RecorderError(
                f"The step's {bad.replace('_', ' ')} is not text that can be stored: "
                "it does not encode as UTF-8."
            )
        slug = self._slugify(description)
        target_serial = serial or meta.serial
        errors: list[str] = []

        screenshot = self._capture_screenshot(
            session_id, number, slug, meta.screenshot_size, target_serial, errors
        )
        hierarchy_file, summary = self._capture_hierarchy(
            session_id, number, slug, target_serial, errors
        )

        step: dict = {
            "number": number,
            "description": description,
            "recorded_at": datetime.now().isoformat(),
            "elapsed_seconds": round(meta.duration_seconds, 2),
            "screenshot": screenshot,
            "ui_dump": hierarchy_file,
            "screen": summary,
            "activity": get_current_activity(target_serial),
            "marker_logged": self._emit_logcat_marker(
                target_serial, session_id, number, description
            ),
            "errors": errors,
            "captured": bool(screenshot or hierarchy_file),
        }
        if screen_name:
            step["screen_name"] = screen_name
        if state:
            step["state"] = state
        if assertion:
            step["assertion"] = assertion
            step["assertion_passed"] = bool(assertion_passed)

        self.store.append_step(session_id, step)
        meta.step_count = number
        if errors:
            meta.capture_failures += 1
        self.store.write_meta(meta)
        return step

    def stop(self, session_id: str | None = None, passed: bool = True) -> dict:
        """Finalise a session: write ``report.md`` + ``manifest.json``."""
        session_id = self.store.resolve_session_id(session_id)
        meta = self.store.load_meta(session_id)
        if meta.status != STATUS_RECORDING:
            raise RecorderError(f"Session {session_id} is already {meta.status}.")

        now = datetime.now()
        meta.status = STATUS_STOPPED
        meta.stopped_at = now.isoformat()
        meta.stopped_at_ms = int(now.timestamp() * 1000)
        meta.passed = passed
        log = self.store.read_steps(session_id)
        steps = log.steps
        integrity = _integrity(meta, log)
        # Steps recorded, not steps readable: a stopped session read back by
        # --list / --get-details must still be able to tell that some are gone.
        meta.step_count = log.recorded(meta)

        # Meta is written last. A stop that dies writing its report must leave
        # the session recording, so that --stop can be run again: a session
        # already marked stopped is refused, and would never get a report.
        report_path = self.store.report_path(session_id)
        manifest_path = self.store.manifest_path(session_id)
        report_path.write_text(self.render_report(meta, steps, integrity), encoding="utf-8")
        _atomic_write_json(
            manifest_path,
            {
                "session": meta.to_json(),
                **integrity,
                "steps": steps,
                "artifacts": {
                    "report": str(report_path),
                    "manifest": str(manifest_path),
                    "screenshots_dir": str(self.store.screenshots_dir(session_id)),
                    "ui_dumps_dir": str(self.store.ui_dumps_dir(session_id)),
                },
            },
        )
        self.store.write_meta(meta)
        return {
            "session_id": session_id,
            "name": meta.name,
            "passed": passed,
            "steps": len(steps),
            **integrity,
            "capture_failures": meta.capture_failures,
            "duration_seconds": round(meta.duration_seconds, 2),
            "report": str(report_path),
            "manifest": str(manifest_path),
        }

    def list_sessions(self) -> list[dict]:
        """Session summaries, newest first.

        ``steps`` is counted from each session's steps file, not taken from
        meta, so it is the number ``--get-details`` would show and it sits
        beside the count of what could not be read.
        """
        summaries = []
        for meta in self.store.list_sessions():
            log = self.store.read_steps(meta.session_id)
            integrity = _integrity(meta, log)
            summaries.append(
                {
                    "session_id": meta.session_id,
                    "name": meta.name,
                    "status": meta.status,
                    "steps": len(log.steps),
                    "incomplete": integrity["incomplete"],
                    "unreadable_lines": integrity["unreadable_lines"],
                    "lost_steps": integrity["lost_steps"],
                    "passed": meta.passed,
                    "started_at": meta.started_at,
                    "duration_seconds": round(meta.duration_seconds, 2),
                }
            )
        return summaries

    def integrity(self, session_id: str) -> dict:
        """Whether every step a session recorded can still be read back."""
        meta = self.store.load_meta(_validate_session_id(session_id))
        return _integrity(meta, self.store.read_steps(meta.session_id))

    def get_details(self, session_id: str) -> dict:
        """Full stored state for one session."""
        meta = self.store.load_meta(_validate_session_id(session_id))
        log = self.store.read_steps(meta.session_id)
        details = {"session": meta.to_json(), **_integrity(meta, log), "steps": log.steps}
        report = self.store.report_path(meta.session_id)
        if report.exists():
            details["report"] = str(report)
        return details

    def clear(self, older_than: str | None = None) -> int:
        return self.store.clear(older_than=older_than)

    # === CAPTURE HELPERS ===

    def _capture_screenshot(
        self,
        session_id: str,
        number: int,
        slug: str,
        size: str,
        serial: str | None,
        errors: list[str],
    ) -> str | None:
        """PNG for this step, or None with the reason appended to ``errors``.

        ``capture_screenshot`` raises on failure and returns a dict with no
        ``success`` key — testing for one was a real defect (S3), so the result
        is used directly.
        """
        path = self.store.screenshots_dir(session_id) / f"{number:03d}-{slug}.png"
        try:
            result = capture_screenshot(serial, output_path=str(path), size=size, inline=False)
        except (RuntimeError, OSError) as error:
            errors.append(f"screenshot: {error}")
            return None
        return result.get("file_path", str(path))

    def _capture_hierarchy(
        self,
        session_id: str,
        number: int,
        slug: str,
        serial: str | None,
        errors: list[str],
    ) -> tuple[str | None, dict]:
        """Dump the UI hierarchy to JSON and summarise it."""
        path = self.store.ui_dumps_dir(session_id) / f"{number:03d}-{slug}.json"
        try:
            hierarchy = get_ui_hierarchy(serial)
        except (RuntimeError, OSError) as error:
            errors.append(f"ui-hierarchy: {error}")
            return None, self.summarize_hierarchy(None)
        try:
            _atomic_write_json(path, hierarchy)
        except OSError as error:
            errors.append(f"ui-dump-write: {error}")
            return None, self.summarize_hierarchy(hierarchy)
        return str(path), self.summarize_hierarchy(hierarchy)

    def _emit_logcat_marker(
        self, serial: str | None, session_id: str, number: int, description: str
    ) -> bool:
        """Write a step marker into logcat so device logs can be aligned later.

        Best-effort and never parsed: the return value only says whether adb
        reported success. The description is free text straight off the command
        line, so it is quoted for the *device's* shell — ``adb shell`` joins its
        arguments and the device re-parses them, which is how ``x;id`` used to
        become a second command elsewhere in this repo.
        """
        message = f"{session_id} step={number} {description}"
        cmd = build_adb_command(
            "shell",
            serial,
            "log",
            "-p",
            "i",
            "-t",
            MARKER_TAG,  # module constant, not caller input
            quote_for_device_shell(message),
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=MARKER_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return result.returncode == 0

    # === PURE HELPERS ===

    @staticmethod
    def summarize_hierarchy(node: dict | None) -> dict:
        """Element counts and visible labels from a uiautomator hierarchy.

        Every UI field lives under ``node["attributes"]`` as a *string*; reading
        them off the node itself silently yields nothing, which was a real bug.
        The ``<hierarchy>`` root is a wrapper, so only ``node`` elements count.
        """
        summary = {"elements": 0, "clickable": 0, "with_text": 0, "labels": [], "package": None}
        if not node:
            return summary

        packages: dict[str, int] = {}
        labels: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if not isinstance(current, dict):
                continue
            stack.extend(reversed(current.get("children") or []))
            if current.get("tag") != "node":
                continue
            attributes = current.get("attributes") or {}
            summary["elements"] += 1
            if attributes.get("clickable") == "true":
                summary["clickable"] += 1
            text = (attributes.get("text") or "").strip()
            label = text or (attributes.get("content-desc") or "").strip()
            if text:
                summary["with_text"] += 1
            if label and label not in labels:
                labels.append(label)
            package = attributes.get("package")
            if package:
                packages[package] = packages.get(package, 0) + 1

        summary["labels"] = labels[:MAX_STEP_LABELS]
        summary["label_total"] = len(labels)
        if packages:
            summary["package"] = max(packages.items(), key=lambda item: item[1])[0]
        return summary

    @staticmethod
    def _slugify(description: str) -> str:
        """Filesystem-safe, length-capped slug for artifact filenames.

        Descriptions come from the command line and are joined onto a directory
        path, so everything outside ``[a-z0-9-]`` is dropped rather than merely
        having spaces replaced — ``../../etc/passwd`` used to survive intact.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")
        return slug[:STEP_NAME_MAXLEN].strip("-") or "step"

    @staticmethod
    def _assertion_symbol(step: dict) -> str:
        """``✓``/``✗`` for a step carrying an assertion, else ``''``."""
        if "assertion" not in step:
            return ""
        return "✓" if step.get("assertion_passed") else "✗"

    @classmethod
    def render_report(
        cls, meta: SessionMeta, steps: list[dict], integrity: dict | None = None
    ) -> str:
        """Markdown report: the human-readable half of a finished session.

        ``integrity`` is :func:`_integrity`'s answer for the session; when it
        says steps were lost the report says so under its status, and never
        "no steps were recorded" for steps that were recorded and are gone.
        """
        incomplete = bool(integrity and integrity["incomplete"])
        status = "✓ PASSED" if meta.passed else "✗ FAILED"
        lines = [
            f"# Test Report: {meta.name}",
            "",
            f"- **Session:** `{meta.session_id}`",
            f"- **Status:** {status}",
            *(
                [f"- **Recording:** INCOMPLETE — {_incomplete_note(integrity, len(steps))}"]
                if incomplete
                else []
            ),
            f"- **Steps:** {len(steps)}",
            f"- **Duration:** {meta.duration_seconds:.1f}s",
            f"- **Device:** {meta.serial or 'default'}",
            f"- **Started:** {meta.started_at}",
        ]
        if meta.app_name:
            lines.append(f"- **App:** {meta.app_name}")
        if meta.capture_failures:
            lines.append(f"- **Capture failures:** {meta.capture_failures}")
        lines += ["", "## Steps", ""]

        if not steps:
            lines += [
                "_No recorded step could be read._" if incomplete else "_No steps were recorded._",
                "",
            ]

        for step in steps:
            lines.append(f"### {step['number']}. {step['description']}")
            detail = [f"+{step.get('elapsed_seconds', 0):.1f}s"]
            if step.get("activity"):
                detail.append(f"`{step['activity']}`")
            lines.append(f"- **At:** {' · '.join(detail)}")

            screen = step.get("screen") or {}
            if screen.get("elements"):
                lines.append(
                    f"- **Screen:** {screen['elements']} elements, "
                    f"{screen.get('clickable', 0)} clickable"
                )
            if screen.get("labels"):
                lines.append(f"- **Labels:** {' · '.join(screen['labels'])}")
            if step.get("screenshot"):
                name = Path(step["screenshot"]).name
                lines.append(f"- **Screenshot:** ![{step['description']}](screenshots/{name})")
            if step.get("ui_dump"):
                lines.append(f"- **UI dump:** `ui-dumps/{Path(step['ui_dump']).name}`")
            if step.get("assertion"):
                lines.append(f"- **Assertion:** {cls._assertion_symbol(step)} {step['assertion']}")
            for error in step.get("errors") or []:
                lines.append(f"- **Capture error:** {error}")
            lines.append("")

        return "\n".join(lines)


# === MODULE-LEVEL HELPERS ===


def _generate_session_id() -> str:
    """``rec-YYYYMMDD-HHMMSS-XXXX`` — the hex suffix avoids same-second collisions."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{SESSION_ID_PREFIX}-{timestamp}-{secrets.token_hex(2)}"


def _validate_session_id(session_id: str) -> str:
    """Reject anything that is not a generated id.

    The id reaches this from ``--session`` / ``--get-details`` and is joined
    onto the storage root, so ``../..`` must not address anything outside it.
    """
    if not SESSION_ID_RE.match(session_id or ""):
        raise RecorderError(
            f"Invalid session id: {session_id!r}. Expected {SESSION_ID_PREFIX}-YYYYMMDD-HHMMSS-XXXX"
        )
    return session_id


def _atomic_write_json(path: Path, payload: object) -> None:
    """tmp+fsync+replace, so a reader never sees a partially written file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _resolve_cutoff_ms(duration_str: str) -> int:
    """Parse e.g. ``24h``, ``30m`` and return the epoch-ms threshold."""
    match = _DURATION_RE.match(duration_str.strip().lower())
    if not match:
        raise RecorderError(f"Invalid duration: {duration_str!r}. Use 30s/5m/24h/7d.")
    value, unit = int(match.group(1)), match.group(2)
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return int((datetime.now() - timedelta(seconds=seconds)).timestamp() * 1000)


def _remove_tree(path: Path) -> None:
    """rm -rf, for session-directory cleanup."""
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            with contextlib.suppress(FileNotFoundError):
                child.unlink()
    with contextlib.suppress(OSError):
        path.rmdir()


# === CLI ===


def _build_parser() -> argparse.ArgumentParser:
    parser = cli.parser(
        description="Record an Android test run: screenshots + UI hierarchy per step.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # One session, one process per step (how an agent drives it):
  SID=$(python test_recorder.py --start "Login flow" --app-name MyApp)
  python test_recorder.py --step "Open the app"
  python test_recorder.py --step "Tap sign in" --assert "Login form shown"
  python test_recorder.py --step "Bad password" --assert "Error shown" --assert-failed
  python test_recorder.py --stop            # or --stop --failed

  # Retrieval:
  python test_recorder.py --list
  python test_recorder.py --get-details "$SID" --json
  python test_recorder.py --clear --older-than 24h

--step and --stop default to the newest recording session; pass --session ID to
target a specific one. Sessions live under ~/.android-emulator-skill/.

Environment variables:
  ANDROID_EMU_RECORDER_HOME                Storage root (default ~/.android-emulator-skill)
  ANDROID_EMU_RECORDER_SESSION_TTL_HOURS   Session prune age (default 72)
  ANDROID_EMU_STEP_NAME_MAXLEN             Artifact filename slug length (default 30)
        """,
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", metavar="NAME", help="Start a session; prints the session id")
    mode.add_argument("--step", metavar="DESCRIPTION", help="Capture the current screen as a step")
    mode.add_argument(
        "--stop", action="store_true", help="Finish a session and write report.md + manifest.json"
    )
    mode.add_argument("--list", action="store_true", help="List stored sessions, newest first")
    mode.add_argument("--get-details", metavar="ID", help="Show one session's stored state")
    mode.add_argument("--clear", action="store_true", help="Delete stored sessions")

    parser.add_argument(
        "--session", metavar="ID", help="Target session (default: newest recording)"
    )
    parser.add_argument("--serial", help="Device serial (auto-detects default device if omitted)")
    parser.add_argument("--app-name", help="App under test, recorded in the report")
    parser.add_argument(
        "--size",
        default="half",
        choices=list(SCREENSHOT_SIZES),
        help="Screenshot size preset (default: half)",
    )
    parser.add_argument("--screen", help="--step: screen name for the report")
    parser.add_argument("--state", help="--step: state description for the report")
    parser.add_argument("--assert", dest="assertion", help="--step: assertion checked at this step")
    parser.add_argument(
        "--assert-failed",
        action="store_true",
        help="--step: record the assertion as failed (default: passed)",
    )
    parser.add_argument("--failed", action="store_true", help="--stop: mark the run as failed")
    parser.add_argument(
        "--older-than", metavar="DURATION", help="--clear: only delete sessions older than e.g. 24h"
    )
    parser.add_argument("--verbose", action="store_true", help="Human-readable detail")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def _run_start(recorder: TestRecorder, args: argparse.Namespace) -> int:
    meta = recorder.start(
        args.start, serial=args.serial, app_name=args.app_name, screenshot_size=args.size
    )
    if args.json:
        print(json.dumps(meta.to_json(), indent=2))
    elif args.verbose:
        print(meta.session_id)
        print(f"Recording: {meta.name}")
        print(f"Directory: {recorder.store.session_dir(meta.session_id)}")
    else:
        # Bare id so `SID=$(... --start NAME)` works.
        print(meta.session_id)
    return 0


def _run_step(recorder: TestRecorder, args: argparse.Namespace) -> int:
    session_id = recorder.store.resolve_session_id(args.session)
    step = recorder.step(
        args.step,
        session_id=session_id,
        screen_name=args.screen,
        state=args.state,
        assertion=args.assertion,
        assertion_passed=not args.assert_failed,
        serial=args.serial,
    )
    if args.json:
        print(json.dumps(step, indent=2))
    else:
        screen = step.get("screen") or {}
        symbol = TestRecorder._assertion_symbol(step)
        prefix = f"{symbol} " if symbol else ""
        print(
            f"{prefix}[{step['number']}] {step['description']} "
            f"({screen.get('elements', 0)} elements, {screen.get('clickable', 0)} clickable)"
        )
        if args.verbose:
            if step.get("screenshot"):
                print(f"  screenshot: {step['screenshot']}")
            if step.get("ui_dump"):
                print(f"  ui dump:    {step['ui_dump']}")
            if step.get("activity"):
                print(f"  activity:   {step['activity']}")
            if screen.get("labels"):
                print(f"  labels:     {' · '.join(screen['labels'])}")
    for error in step.get("errors") or []:
        print(f"Warning: {error}", file=sys.stderr)
    integrity = recorder.integrity(session_id)
    if integrity["incomplete"]:
        note = _incomplete_note(integrity, step["number"] - integrity["lost_steps"])
        print(f"Warning: recording is incomplete: {note}", file=sys.stderr)
    # Only a step that captured nothing at all is a failure.
    return 0 if step["captured"] else 1


def _run_stop(recorder: TestRecorder, args: argparse.Namespace) -> int:
    result = recorder.stop(session_id=args.session, passed=not args.failed)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "✓ PASSED" if result["passed"] else "✗ FAILED"
        if result["incomplete"]:
            status += ", INCOMPLETE RECORDING"
        print(
            f"{status}: {result['name']} — {result['steps']} steps in {result['duration_seconds']}s"
        )
        if result["incomplete"]:
            print(f"Incomplete: {_incomplete_note(result, result['steps'])}")
        print(f"Report:   {result['report']}")
        print(f"Manifest: {result['manifest']}")
        if result["capture_failures"]:
            print(f"Capture failures: {result['capture_failures']}", file=sys.stderr)
    # Non-zero on a failed run so a CI wrapper can key off it -- and on an
    # incomplete recording, so a caller reading only the status is not told a
    # run passed on a report that is missing steps.
    return 0 if result["passed"] and not result["incomplete"] else 1


def _run_list(recorder: TestRecorder, args: argparse.Namespace) -> int:
    sessions = recorder.list_sessions()
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("No recorded sessions.")
        return 0
    for entry in sessions:
        line = (
            f"{entry['session_id']}  {entry['status']:<9}  "
            f"{entry['steps']:>3} steps  {entry['name']}"
        )
        if entry["incomplete"]:
            line += (
                f"  INCOMPLETE: {entry['lost_steps']} lost, "
                f"{entry['unreadable_lines']} unreadable line(s)"
            )
        print(line)
    return 0


def _run_get_details(recorder: TestRecorder, args: argparse.Namespace) -> int:
    details = recorder.get_details(args.get_details)
    if args.json:
        print(json.dumps(details, indent=2))
        return 0
    session = details["session"]
    print(f"{session['session_id']}  {session['status']}  {session['name']}")
    print(f"Steps: {len(details['steps'])}  Device: {session['serial'] or 'default'}")
    if details["incomplete"]:
        print(f"Incomplete: {_incomplete_note(details, len(details['steps']))}")
    for step in details["steps"]:
        screen = step.get("screen") or {}
        symbol = TestRecorder._assertion_symbol(step)
        prefix = f"{symbol} " if symbol else ""
        print(
            f"  {prefix}[{step['number']}] {step['description']} ({screen.get('elements', 0)} el)"
        )
    if "report" in details:
        print(f"Report: {details['report']}")
    return 0


def _run_clear(recorder: TestRecorder, args: argparse.Namespace) -> int:
    deleted = recorder.clear(older_than=args.older_than)
    if args.json:
        print(json.dumps({"deleted": deleted}, indent=2))
    else:
        print(f"Deleted {deleted} session(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run one command, return the exit status."""
    args = _build_parser().parse_args(argv)
    recorder = TestRecorder()
    # RecorderError, a device RuntimeError and an OSError from the session
    # store all reach common.cli, which reports them in the one shape.
    if args.start is not None:
        return _run_start(recorder, args)
    if args.step is not None:
        return _run_step(recorder, args)
    if args.get_details is not None:
        return _run_get_details(recorder, args)
    if args.stop:
        return _run_stop(recorder, args)
    if args.list:
        return _run_list(recorder, args)
    return _run_clear(recorder, args)


if __name__ == "__main__":
    cli.run(main)
