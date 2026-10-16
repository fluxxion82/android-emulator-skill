#!/usr/bin/env python3
"""ANR/jank filter pipeline — pure functions, no I/O.

Stages: parse → assemble → normalise → threshold → bucket → cluster → aggregate
→ rank → format.

Each function is independently testable; the worker and the ``--stop`` path both
compose them. The clustering / session / formatting machinery is ported nearly
verbatim from the iOS HangBuster pipeline (``hang_pipeline.py``); only the
*parse* layer (``parse_logcat_anr`` for one line, ``AnrRecordAssembler`` for
the stream) and the severity bands are Android-native.

Every reader of a log goes through ``AnrRecordAssembler`` (or
``parse_logcat_stream`` for a finite dump), never ``parse_logcat_anr`` line by
line: one stall is several lines in two subsystems' words, and only the
assembler knows which of them are the same incident (D5).

Token budgets are enforced via a documented char/4 heuristic
(``estimate_tokens``) — accurate to within ~10% of real tokenizers and
dependency-free.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

# === CONSTANTS ===

FINGERPRINT_VERSION = 2
"""Bump when normalise_message, compute_fingerprint, or severity boundaries change.

A session records the version it was CAPTURED with (``SessionMeta``), and its
summary keeps that version however late it is stopped, because the events'
fingerprints were computed then. ``--diff`` refuses two sessions whose versions
differ rather than compare fingerprints computed two different ways.

v1 (2026-06): initial Android ANR/jank fingerprint. compute_fingerprint() hashes
its input with sha256[:16]; symbol (component/tag) wins when present, otherwise the
normalised message prefix is the hash input.

v2 (2026-10, A5): a jank event's identity is its PROCESS, not its frame count.
``Skipped N frames!`` kept N in the prefix, so one janking screen fingerprinted
once per distinct count; N is now redacted. A Choreographer line names no app,
so the process is hashed in: by NAME when the session worker could name the pid
(``identity: "package"``), else by the pid itself (``identity: "pid"``), which
``--diff`` will not compare across sessions -- a restarted app has a new pid."""

# Each dropped frame on a 60Hz display is ~16.7ms of main-thread stall.
FRAME_MS = 16.7

# Skipped-frame severity boundaries (frame counts). Android Choreographer only
# warns once N skipped frames cross a threshold; we scale severity from there.
WARN_FRAMES = 30
CRITICAL_FRAMES = 100

_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]{4,}")
_PID_REF = re.compile(r"\bpid[:= ]\s*\d+\b", re.IGNORECASE)
_BARE_INT = re.compile(r"\b\d{4,}\b")
_WHITESPACE = re.compile(r"\s+")
_BOILERPLATE_PREFIXES = (
    "The application may be doing too much work on its main thread.",
    "Input dispatching timed out",
)
_SYMBOL_PATTERNS = [
    # Fully-qualified component: com.app/.MainActivity or com.app/com.app.Act
    re.compile(r"\b([a-zA-Z][\w.]*/[\w.$]+)"),
    # Java/Kotlin method ref: ClassName.method or Class$Inner.method
    re.compile(r"\b([A-Z][A-Za-z0-9_$]+\.[A-Za-z_][\w$]+)\b"),
]

# logcat threadtime format:
#   MM-DD HH:MM:SS.mmm  PID  TID  PRIORITY  TAG: message
_LOG_LINE_PATTERN = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})"
    r"\s+(\d+)"  # pid
    r"\s+(\d+)"  # tid
    r"\s+([VDIWEF])"  # priority
    r"\s+([^:]+):"  # tag
    r"\s*(.*)"  # message
)

# "Skipped 47 frames!  The application may be doing too much work..."
_SKIPPED_FRAMES_RE = re.compile(r"Skipped\s+(\d+)\s+frames", re.IGNORECASE)

# The same, for redaction: the count is a MEASUREMENT of the stall (it is kept
# in ``frames``), not part of what stalled. _BARE_INT only takes 4+ digits, so a
# two- or three-digit count survived into the prefix and one janking screen
# fingerprinted once per count it happened to drop (A5).
_FRAME_COUNT = re.compile(r"(Skipped\s+)\d+(\s+frames)", re.IGNORECASE)

# ActivityManager writes one ANR as ONE log record of many lines -- "ANR in",
# "PID:", "Reason:", "ErrorId:", "Frozen:", load, pressure, CPU usage -- and
# logcat prints every line of a record under the identical header: timestamp,
# pid, tid, priority and tag (recorded: logcat_anr_broadcast_stream). That
# header is the only thing that says the lines belong together.
_ANR_RECORD_PID_RE = re.compile(r"^PID:\s*(\d+)\s*$")
_ANR_RECORD_REASON_RE = re.compile(r"^Reason:\s*(.+?)\s*$")

# Tags whose "ANR in ..." line is a report TO ActivityManager. WindowManager
# logs "ANR in Window{...}. Reason:Input dispatching timed out" and hands the
# window to ActivityManager, which then either writes its own full record (a new
# ANR: logcat_anr_input_stream, 0.35 s after WindowManager's line) or says
# "Skipping duplicate ANR" (a stall it already recorded:
# logcat_anr_broadcast_stream, where WindowManager reported 5.06 s AFTER the
# broadcast ANR's record). Counting both made one stall two ANRs. But dropping
# WindowManager's line outright made a stream cut between the two -- a stop, a
# --since window -- ZERO ANRs, and ActivityManager has other early returns
# (crashing, killed, shutting down) where it records nothing. So the line is
# held as a FALLBACK: it is dropped when ActivityManager accounts for the same
# package within ANR_CORRELATION_MS, either side, and is an event otherwise.
_ANR_PRECURSOR_TAGS = frozenset({"WindowManager"})

# How far apart, by logcat timestamp, WindowManager's report and
# ActivityManager's record of one package's ANR may be and still be one
# incident. Twice the 5.06 s measured for the duplicate case above.
ANR_CORRELATION_MS = 10_000

# ActivityManager's verdict on a report for a stall it already recorded.
_ANR_DUPLICATE_PREFIX = "Skipping duplicate ANR:"

# What WindowManager's line says timed out: "...}. Reason:Input dispatching ..."
_WINDOW_REASON_RE = re.compile(r"\.\s*Reason:\s*(.+?)\s*$")

# "ActivityManager: Start proc 14407:com.example.composefixture/u0a205 for ..."
# (recorded: logcat_app_restart) -- a pid and its process name, together.
_START_PROC_RE = re.compile(r"\bStart proc (\d+):([^/\s]+)/")

# A package as a whole token followed by its component's slash -- "(62056f6
# com.example.composefixture/com.example..." in a duplicate verdict. Bounded on
# the left so "example.composefixture" is not found inside
# "com.example.composefixture" (review of PR #26: a substring match let one
# app's verdict settle another app's ANR).
_PACKAGE_TOKEN_RE = re.compile(r"(?<![\w.])([A-Za-z]\w*(?:\.\w+)+)/")

# ps's ELAPSED, in the two shapes recorded: MM:SS (ps_lifetime_just_started)
# and HH:MM:SS (ps_lifetime). toybox documents a days form too; none has been
# seen, so it is not read -- an age that cannot be read is no evidence.
_ELAPSED_RE = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})$")

# One stalled app is reported by TWO subsystems in two different shapes. Both
# are recorded: logcat_anr_broadcast and logcat_anr_input_dispatch.
#
# WindowManager writes the input-dispatch form FIRST in the line, and its
# package sits inside the braces:
#   "ANR in Window{67a8d77 u0 com.pkg/com.pkg.MainActivity}. Reason:Input ..."
# Matched before the generic form, which would otherwise read the literal word
# "Window" as the package name and report a fault in an app called Window.
_ANR_IN_WINDOW_RE = re.compile(
    r"ANR in Window\{\S+\s+\S+\s+(?P<package>[\w.]+)/(?P<component>[\w.$]+)\}",
    re.IGNORECASE,
)

# ActivityManager's form is the bare package: "ANR in com.example.app". The
# parenthesised component is optional because -- contrary to every hand-typed
# sample this repo used to test against -- a broadcast ANR does not carry one.
_ANR_IN_RE = re.compile(r"ANR in ([\w.]+)(?:\s+\(([^)]+)\))?", re.IGNORECASE)

# "StrictMode policy violation; ~duration=27 ms: android.os.strictmode.DiskReadViolation"
_STRICTMODE_DURATION_RE = re.compile(r"~duration=(\d+)\s*ms", re.IGNORECASE)


# === TYPES ===


class Severity(StrEnum):
    """ANR/jank severity bucket. String-valued for stable JSON serialisation."""

    MINOR = "minor"
    WARN = "warn"
    CRITICAL = "critical"
    FROZEN = "frozen"


_SEVERITY_WEIGHT = {
    Severity.MINOR: 1,
    Severity.WARN: 2,
    Severity.CRITICAL: 4,
    Severity.FROZEN: 8,
}


@dataclass
class NormalisedEvent:
    """A single ANR/jank event after parse + normalise + bucket."""

    delta_ms: int
    process: str
    pid: int
    duration_ms: float
    severity: Severity
    symbol: str | None
    message_prefix: str
    fingerprint: str
    raw_message: str = ""
    frames: int | None = None
    kind: str = "jank"
    # From an ActivityManager ANR record's own lines (AnrRecordAssembler): what
    # timed out, and the stalled app's pid -- ``pid`` above is the logcat
    # column, which for an ANR is system_server's.
    reason: str | None = None
    app_pid: int | None = None
    # The process the event belongs to, by name, when known: the ANR's package,
    # or the name the session worker found for a jank line's pid.
    app: str | None = None
    # How a jank event's site is keyed: "package" (by ``app``) or "pid" (no
    # name was found). None for kinds whose text names their site.
    identity: str | None = None


@dataclass
class Cluster:
    """A group of NormalisedEvents sharing a fingerprint."""

    fingerprint: str
    count: int
    max_duration_ms: float
    total_duration_ms: float
    first_delta_ms: int
    severity: Severity
    symbol_or_prefix: str
    sample_event: NormalisedEvent
    # The events' ``identity`` (they share a fingerprint, so they share it).
    # "pid" clusters are not compared across sessions by --diff.
    identity: str | None = None


@dataclass
class SessionSummary:
    """End-state summary of a session. Persisted to summary.json."""

    session_id: str
    started_at: str
    duration_ms: int
    event_count: int
    dropped_below_threshold: int
    matched_lines: int
    total_lines: int
    clusters: list[Cluster]
    aggregates: dict
    fingerprint_version: int = FINGERPRINT_VERSION
    # True when --stop gave up waiting for the worker: whatever it wrote after
    # that is not in here (review of PR #26 -- never a silently short summary).
    incomplete: bool = False


# === STAGE 1: PARSE ===


def parse_logcat_anr(line: str) -> dict | None:
    """Classify ONE ``adb logcat -v threadtime`` line as an ANR/jank signal.

    Recognises three Android signals:

    1. **Choreographer jank** — ``Skipped N frames!`` from the ``Choreographer``
       tag. Duration is estimated as ``N * 16.7ms`` (one dropped frame ≈ one
       60Hz refresh of blocked main thread).
    2. **Hard ANR** — ActivityManager's ``ANR in <pkg>[ (<component>)]`` and
       WindowManager's ``ANR in Window{... <pkg>/<component>}``. These are hard
       ANRs (5s+ unresponsive), bucketed FROZEN.
    3. **StrictMode / slow operations** (optional, minor) — ``Slow operation``
       or StrictMode dialog lines, treated as low-severity jank hints.

    This is a line classifier, not a reader. ActivityManager's ``PID:`` and
    ``Reason:`` lines belong to the ``ANR in`` record above them and are not
    signals on their own -- a Reason naming an action such as ``PROVOKE_ANR``
    used to read as a second ANR (D5). Attaching them, and knowing that
    WindowManager's line and ActivityManager's are one stall, takes the
    stream: use :class:`AnrRecordAssembler`.

    Returns ``None`` for non-log lines or lines that don't describe ANR/jank.
    """
    if not line.strip():
        return None
    match = _LOG_LINE_PATTERN.match(line)
    if not match:
        return None
    timestamp_str, pid_str, _tid_str, _priority, tag, message = match.groups()
    tag = tag.strip()
    message = message.strip()

    classified = _classify_anr(tag, message)
    if classified is None:
        return None

    event: dict = {
        "timestamp": timestamp_str.strip(),
        "pid": int(pid_str),
        "process": tag,
        "message": message,
        "kind": classified["kind"],
        "duration_ms": classified["duration_ms"],
    }
    if classified.get("frames") is not None:
        event["frames"] = classified["frames"]
    if classified.get("component"):
        event["component"] = classified["component"]
    if classified.get("anr_package"):
        event["anr_package"] = classified["anr_package"]
    return event


def _classify_anr(tag: str, message: str) -> dict | None:
    """Map a (tag, message) pair to ANR/jank metadata, or None if irrelevant."""
    lower = message.lower()

    # 1. Choreographer "Skipped N frames!" jank.
    if "choreographer" in tag.lower() or "skipped" in lower:
        frames_match = _SKIPPED_FRAMES_RE.search(message)
        if frames_match:
            frames = int(frames_match.group(1))
            return {
                "kind": "jank",
                "frames": frames,
                "duration_ms": round(frames * FRAME_MS, 1),
                "component": None,
                "anr_package": None,
            }

    # 2a. WindowManager's input-dispatch ANR, whose package is inside the
    # braces. Checked before the generic form: "ANR in Window{...}" matches
    # that too, and reports a fault in a package called "Window".
    window_match = _ANR_IN_WINDOW_RE.search(message)
    if window_match:
        package = window_match.group("package")
        return {
            "kind": "anr",
            "frames": None,
            "duration_ms": 5000.0,
            "component": f"{package}/{window_match.group('component')}",
            "anr_package": package,
        }

    # 2b. ActivityManager hard ANR.
    anr_match = _ANR_IN_RE.search(message)
    if anr_match:
        pkg = anr_match.group(1)
        component = anr_match.group(2)
        return {
            "kind": "anr",
            "frames": None,
            # Hard ANR threshold is the input-dispatch timeout (~5s). Use it as a
            # lower-bound duration so these always bucket FROZEN.
            "duration_ms": 5000.0,
            "component": component,
            "anr_package": pkg,
        }

    # There used to be a branch here classifying "follow-on" lines -- anything
    # holding "input dispatching timed out", or "anr" and "reason:" -- as ANRs
    # of their own. In a recorded stream it matched nothing that was one: the
    # Reason line of an ANR already counted (D5), and ActivityManager's
    # "Skipping duplicate ANR: ... Input dispatching timed out", which is the
    # system saying in words that this is NOT a new ANR
    # (logcat_anr_broadcast_stream). AnrRecordAssembler attaches the first.

    # 3. StrictMode / slow operation hints (minor jank).
    #
    # StrictMode logs a violation as a HEADER line followed by its whole stack
    # trace, every frame under the same tag -- see logcat_strictmode_violation,
    # where two file operations produce ten headers and roughly three hundred
    # tagged lines. Matching on the tag alone made each of those frames a
    # separate event, so one violation arrived as thirty stalls.
    if "strictmode" in tag.lower() or "slow operation" in lower:
        if message.lstrip().startswith(("at ", "... ")):
            return None
        # The violation states its own cost ("~duration=27 ms"). The old
        # constant below is a placeholder for when it does not; using it while
        # the real number was right there in the line was simply throwing the
        # measurement away.
        duration = _STRICTMODE_DURATION_RE.search(message)
        return {
            "kind": "strictmode",
            "frames": None,
            "duration_ms": (
                float(duration.group(1)) if duration else float(WARN_FRAMES * FRAME_MS)
            ),
            "component": None,
            "anr_package": None,
        }

    return None


def is_anr_message(tag: str, message: str) -> bool:
    """Return True if a (tag, message) pair describes an ANR/jank event."""
    return _classify_anr(tag, message) is not None


# === STAGE 1b: ASSEMBLE (lines -> incidents) ===


class AnrRecordAssembler:
    """Turn a logcat stream into ANR/jank events, one per incident.

    ``parse_logcat_anr`` answers "what does this line say"; this answers "which
    lines are the same stall", which no single line can:

    - **One ANR is one record.** ActivityManager's ``ANR in`` line opens it, and
      every following line under the identical header (timestamp, pid, tid,
      priority, tag) is the same log record -- ``PID:`` is the stalled app's
      own pid (the header's pid is system_server's), ``Reason:`` is the only
      statement of what timed out. They are attached to the event instead of
      becoming events (D5). So an ANR is held until the header changes, or
      until the stream goes quiet (:meth:`flush`) or ends (:meth:`close`).
    - **WindowManager's ANR line is a fallback.** It is a report to
      ActivityManager, which normally records the incident itself, before or
      after it (see ``_ANR_PRECURSOR_TAGS``). It is dropped when ActivityManager
      records -- or calls a duplicate -- an ANR of the same package within
      ``ANR_CORRELATION_MS`` of it, by logcat time; otherwise it is the event,
      emitted once that window has passed (later lines, or a quiet stream) or
      the stream ends. Emitted late, so it can follow events logged after it.

    Jank and StrictMode lines are single-line incidents and come straight out.
    Every reader -- ``--watch``, ``--since`` and the session worker -- feeds its
    lines through one of these, so they agree on what one ANR is.
    """

    def __init__(self) -> None:
        self._pending: dict | None = None
        self._header: tuple[str, ...] | None = None
        # WindowManager reports waiting for ActivityManager: (logcat ms, event).
        self._held: list[tuple[int, dict]] = []
        # package -> logcat ms of ActivityManager's latest record for it.
        self._recorded: dict[str, int] = {}

    def feed(self, line: str) -> list[dict]:
        """Take one line; return the events it completes (often none)."""
        match = _LOG_LINE_PATTERN.match(line.rstrip("\r\n"))
        header = match.groups()[:5] if match else None
        message = match.group(6).strip() if match else ""
        completed: list[dict] = []
        if self._pending is not None:
            if header == self._header and not message.startswith("ANR in "):
                self._attach(message)
                return completed
            completed.extend(self._close_record())
        if match is None:
            return completed

        at_ms = _timestamp_to_ms(match.group(1))
        completed.extend(self._release(lambda held_ms: at_ms - held_ms > ANR_CORRELATION_MS))
        tag = match.group(5).strip()
        if tag == "ActivityManager" and message.startswith(_ANR_DUPLICATE_PREFIX):
            # ActivityManager's own word that a report belongs to a stall it
            # has already recorded. It settles a held report only for a
            # package it names exactly, and only within the window.
            named = set(_PACKAGE_TOKEN_RE.findall(message))
            self._held = [
                (ms, e)
                for ms, e in self._held
                if e.get("anr_package") not in named or abs(at_ms - ms) > ANR_CORRELATION_MS
            ]
            return completed

        event = parse_logcat_anr(line)
        if event is None:
            return completed
        if event["kind"] != "anr":
            completed.append(event)
            return completed
        package = event.get("anr_package") or ""
        if event["process"] in _ANR_PRECURSOR_TAGS:
            recorded_ms = self._recorded.get(package)
            if recorded_ms is None or abs(at_ms - recorded_ms) > ANR_CORRELATION_MS:
                reason = _WINDOW_REASON_RE.search(message)
                if reason:
                    event["reason"] = reason.group(1)
                self._held.append((at_ms, event))
            return completed
        self._recorded[package] = at_ms
        self._held = [
            (ms, e)
            for ms, e in self._held
            if e.get("anr_package") != package or abs(at_ms - ms) > ANR_CORRELATION_MS
        ]
        self._pending, self._header = event, header
        return completed

    def flush(self, quiet_seconds: float = 0.0) -> list[dict]:
        """The stream has gone quiet: emit what quiet completes.

        An open ActivityManager record is complete (it arrives as one burst).
        A held WindowManager report is not, unless the stream has been quiet for
        the whole correlation window -- ActivityManager logs in real time, so
        a record that has not come by then is not coming.

        Args:
            quiet_seconds: How long the stream has delivered nothing.
        """
        completed = self._close_record()
        if quiet_seconds * 1000 >= ANR_CORRELATION_MS:
            completed.extend(self._release(lambda _held_ms: True))
        return completed

    def close(self) -> list[dict]:
        """The stream has ended: emit everything still held."""
        return [*self._close_record(), *self._release(lambda _held_ms: True)]

    def _close_record(self) -> list[dict]:
        if self._pending is None:
            return []
        event, self._pending, self._header = self._pending, None, None
        return [event]

    def _release(self, due) -> list[dict]:
        """Emit, as events, the held WindowManager reports ``due`` says are due."""
        released = [event for held_ms, event in self._held if due(held_ms)]
        self._held = [(ms, event) for ms, event in self._held if not due(ms)]
        return released

    def _attach(self, message: str) -> None:
        """Fold one line of the open ANR record into its event.

        Only the fields worth carrying are kept; ``ErrorId:``, ``Frozen:``, the
        load and CPU tables are the same record and are simply not events.
        """
        pending = self._pending
        if pending is None:
            return
        pid = _ANR_RECORD_PID_RE.match(message)
        if pid and "app_pid" not in pending:
            pending["app_pid"] = int(pid.group(1))
            return
        reason = _ANR_RECORD_REASON_RE.match(message)
        if reason and "reason" not in pending:
            pending["reason"] = reason.group(1)


def parse_logcat_stream(lines: Iterable[str]) -> list[dict]:
    """Every ANR/jank incident in a finite run of logcat lines (a ``-d`` dump)."""
    assembler = AnrRecordAssembler()
    events: list[dict] = []
    for line in lines:
        events.extend(assembler.feed(line))
    events.extend(assembler.close())
    return events


def parse_start_proc(line: str) -> tuple[int, str] | None:
    """``(pid, process name)`` from ActivityManager's ``Start proc`` line, else None.

    Recorded in logcat_app_restart: ``Start proc 14407:com.example.composefixture/
    u0a205 for next-top-activity {...}``. The one line that names a process and
    its pid together as the process starts, so a session sees it for every app
    (re)started while it runs.
    """
    match = _LOG_LINE_PATTERN.match(line.rstrip("\r\n"))
    if not match or match.group(5).strip() != "ActivityManager":
        return None
    started = _START_PROC_RE.search(match.group(6))
    return (int(started.group(1)), started.group(2)) if started else None


@dataclass(frozen=True)
class ProcessAge:
    """Who holds a pid now, and for how long they have held it."""

    name: str
    age: timedelta


def parse_process_age(text: str, pid: int) -> ProcessAge | None:
    """Read ``ps -o PID,NAME,ELAPSED -p <pid>``.

    Recorded as ``ps_lifetime`` (ELAPSED as HH:MM:SS), ``ps_lifetime_just_started``
    (MM:SS) and ``ps_lifetime_not_running`` (the header alone, exit 1). ELAPSED
    is the age of the pid's CURRENT owner -- a duration, so comparing it needs
    no clock on the device and no wall time on either side. Only a row whose
    PID is ``pid`` counts, so the header is never a process. None when nothing
    holds the pid, or its age is in a shape not recorded (days).
    """
    for row in text.splitlines():
        fields = row.split()
        if len(fields) < 3 or fields[0] != str(pid):
            continue
        elapsed = _ELAPSED_RE.match(fields[-1])
        if elapsed is None:
            return None
        hours, minutes, seconds = (int(part or 0) for part in elapsed.groups())
        return ProcessAge(
            name=" ".join(fields[1:-1]),
            age=timedelta(hours=hours, minutes=minutes, seconds=seconds),
        )
    return None


# === STAGE 2: NORMALISE ===


def normalise_message(message: str, max_len: int = 40) -> str:
    """Strip boilerplate, redact volatile tokens, truncate to ``max_len``."""
    text = message
    for prefix in _BOILERPLATE_PREFIXES:
        idx = text.find(prefix)
        if idx != -1:
            text = (text[:idx]).strip() or text
            break
    text = _FRAME_COUNT.sub(r"\1<n>\2", text)
    text = _HEX_ADDR.sub("<addr>", text)
    text = _PID_REF.sub("<pid>", text)
    text = _BARE_INT.sub("<n>", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def extract_symbol(message: str) -> str | None:
    """Return the first Android component / Java symbol mention if present."""
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.search(message)
        if match:
            return match.group(1)
    return None


# === STAGE 3: THRESHOLD ===


def above_threshold(frames: int | None, min_frames: int) -> bool:
    """Drop skipped-frame events below the minimum frame threshold.

    Non-frame events (hard ANRs, StrictMode) carry ``frames=None`` and always
    pass — only Choreographer jank is frame-thresholded.
    """
    return frames is None or frames >= min_frames


# === STAGE 4: SEVERITY BUCKET ===


def bucket_severity(frames: int | None, kind: str, duration_ms: float) -> Severity:
    """Map an Android event to a severity band.

    Hard ANRs are always FROZEN. Skipped-frame jank scales by frame count:
    < 30 MINOR, < 100 WARN/CRITICAL, 100+ CRITICAL. StrictMode hints are MINOR.
    """
    if kind == "anr":
        return Severity.FROZEN
    if kind == "strictmode":
        return Severity.MINOR
    # Choreographer jank, by skipped frame count.
    if frames is None:
        # Fall back to duration if frames are unknown.
        frames = int(duration_ms / FRAME_MS) if duration_ms else 0
    if frames < WARN_FRAMES:
        return Severity.MINOR
    if frames < 60:
        return Severity.WARN
    if frames < CRITICAL_FRAMES:
        return Severity.CRITICAL
    return Severity.FROZEN


# === STAGE 5: NORMALISED EVENT + FINGERPRINT ===


def build_normalised_event(
    raw_event: dict, session_start_ms: int, current_ms: int | None = None
) -> NormalisedEvent | None:
    """Combine stages 2 + 4 + fingerprint into one ``NormalisedEvent``.

    Returns ``None`` if duration is missing — threshold filtering should have
    dropped these already, but we guard for safety.
    """
    duration = raw_event.get("duration_ms")
    if duration is None:
        return None
    if current_ms is None:
        current_ms = _timestamp_to_ms(raw_event.get("timestamp", ""))
    delta_ms = max(0, current_ms - session_start_ms) if current_ms else 0
    message = raw_event.get("message", "")
    kind = raw_event.get("kind", "jank")
    frames = raw_event.get("frames")
    pid = int(raw_event.get("pid", 0))
    # Prefer the explicit component (high signal) over a regex-scraped symbol.
    symbol = raw_event.get("component") or extract_symbol(message)
    prefix = normalise_message(message)
    app = raw_event.get("app") or raw_event.get("anr_package")
    # A Choreographer line names no app: every app's jank reads "Skipped <n>
    # frames!" under the same tag. Without a site, redacting the count (A5)
    # would merge every app's jank into one cluster. The site is the process:
    # its NAME when the session worker found one, which survives a restart; else
    # the logcat pid -- the janking process itself, unlike an ANR's, which is
    # system_server's -- which does not, and is marked so --diff will not
    # compare it (a restarted app keyed by pid read as "1 resolved, 1 new").
    identity: str | None = None
    site: str | None = None
    if kind == "jank":
        identity = "package" if raw_event.get("app") else "pid"
        site = f"app:{raw_event['app']}" if identity == "package" else f"pid:{pid}"
    fingerprint = compute_fingerprint(symbol, prefix, kind, site=site)
    app_pid = raw_event.get("app_pid")
    return NormalisedEvent(
        delta_ms=delta_ms,
        process=raw_event.get("process", "unknown"),
        pid=pid,
        duration_ms=float(duration),
        severity=bucket_severity(frames, kind, float(duration)),
        symbol=symbol,
        message_prefix=prefix,
        fingerprint=fingerprint,
        raw_message=message,
        frames=frames,
        kind=kind,
        reason=raw_event.get("reason"),
        app_pid=int(app_pid) if app_pid is not None else None,
        app=app,
        identity=identity,
    )


def compute_fingerprint(
    symbol: str | None, message_prefix: str, kind: str = "jank", site: str | None = None
) -> str:
    """Stable identity hash for clustering and diff.

    Hashed (sha256[:16]) so distinct messages with overlapping normalised
    prefixes don't collide into the same cluster. Symbol when present (high
    signal); otherwise normalised message prefix is the hash input. The event
    ``kind`` is mixed in so a jank and an ANR for the same component stay
    distinct clusters, and ``site`` (``app:<name>`` or ``pid:<n>``) -- the
    process, for a signal whose text does not name one -- so two apps'
    identical messages do too.

    The human-readable label lives in ``Cluster.symbol_or_prefix`` — the
    fingerprint is purely an identity key.
    """
    inner = f"sym:{symbol}" if symbol else f"msg:{message_prefix}"
    key = f"{kind}|{inner}" if site is None else f"{kind}|{site}|{inner}"
    return f"fp:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def _timestamp_to_ms(ts: str) -> int:
    """Parse a logcat timestamp like '06-17 14:30:52.123' to ms epoch.

    logcat threadtime omits the year, so we assume the current year. This only
    affects ``delta_ms`` display ordering, never fingerprinting.
    """
    if not ts:
        return 0
    year = datetime.now().year
    try:
        dt = datetime.strptime(f"{year}-{ts}", "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(f"{year}-{ts.split('.', maxsplit=1)[0]}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
    return int(dt.timestamp() * 1000)


# === STAGE 6: CLUSTER ===


def cluster_events(events: list[NormalisedEvent]) -> list[Cluster]:
    """Group events by fingerprint, aggregating count + duration stats."""
    by_fp: dict[str, list[NormalisedEvent]] = {}
    for event in events:
        by_fp.setdefault(event.fingerprint, []).append(event)
    clusters: list[Cluster] = []
    for fingerprint, group in by_fp.items():
        durations = [e.duration_ms for e in group]
        deltas = [e.delta_ms for e in group]
        max_severity = max(group, key=lambda e: _SEVERITY_WEIGHT[e.severity]).severity
        sample = max(group, key=lambda e: e.duration_ms)
        clusters.append(
            Cluster(
                fingerprint=fingerprint,
                count=len(group),
                max_duration_ms=max(durations),
                total_duration_ms=sum(durations),
                first_delta_ms=min(deltas),
                severity=max_severity,
                symbol_or_prefix=sample.symbol or sample.message_prefix,
                sample_event=sample,
                identity=sample.identity,
            )
        )
    return clusters


# === STAGE 7: AGGREGATE ===


def detect_temporal_bursts(
    events: list[NormalisedEvent], window_ms: int = 1000, min_count: int = 3
) -> list[dict]:
    """Find windows containing ``min_count`` or more events within ``window_ms``."""
    if not events:
        return []
    sorted_events = sorted(events, key=lambda e: e.delta_ms)
    bursts: list[dict] = []
    i = 0
    while i < len(sorted_events):
        window_start = sorted_events[i].delta_ms
        j = i
        while j < len(sorted_events) and sorted_events[j].delta_ms - window_start <= window_ms:
            j += 1
        burst_size = j - i
        if burst_size >= min_count:
            bursts.append(
                {
                    "starts_at_ms": window_start,
                    "ends_at_ms": sorted_events[j - 1].delta_ms,
                    "count": burst_size,
                }
            )
            i = j
        else:
            i += 1
    return bursts


def detect_quiet_periods(events: list[NormalisedEvent], threshold_ms: int = 5000) -> list[dict]:
    """Find gaps between adjacent events that exceed ``threshold_ms``."""
    if len(events) < 2:
        return []
    sorted_events = sorted(events, key=lambda e: e.delta_ms)
    periods: list[dict] = []
    for prev, curr in itertools.pairwise(sorted_events):
        gap = curr.delta_ms - prev.delta_ms
        if gap >= threshold_ms:
            periods.append({"from_ms": prev.delta_ms, "to_ms": curr.delta_ms, "gap_ms": gap})
    return periods


def process_distribution(events: list[NormalisedEvent]) -> dict[str, int]:
    """Count events per process / tag name."""
    dist: dict[str, int] = {}
    for event in events:
        dist[event.process] = dist.get(event.process, 0) + 1
    return dist


# === STAGE 8: RANK ===


def rank_clusters(clusters: list[Cluster], top_n: int | None = None) -> list[Cluster]:
    """Sort by severity_weight * max_duration_ms * log(count + 1), descending."""

    def score(cluster: Cluster) -> float:
        weight = _SEVERITY_WEIGHT[cluster.severity]
        return weight * cluster.max_duration_ms * math.log(cluster.count + 1)

    ranked = sorted(clusters, key=score, reverse=True)
    return ranked if top_n is None else ranked[:top_n]


# === STAGE 9: FORMAT ===


def format_l0(summary: SessionSummary) -> str:
    """Single-line status (~20 tokens). Cache-friendly for agent context."""
    if not summary.clusters:
        return f"Session {summary.session_id}: no ANR/jank above threshold."
    top = summary.clusters[0]
    critical = sum(
        1 for c in summary.clusters if c.severity in (Severity.CRITICAL, Severity.FROZEN)
    )
    return (
        f"Session {summary.session_id}: {summary.duration_ms / 1000:.1f}s, "
        f"{summary.event_count} events ({critical} critical), top: "
        f"{top.symbol_or_prefix} {top.max_duration_ms:.0f}ms ×{top.count}"
    )


def format_l1(summary: SessionSummary, top_n: int = 3) -> str:
    """Default ~80-120 token output: header + top-N clusters + drill hint."""
    if not summary.clusters:
        return (
            f"Session {summary.session_id}: {summary.duration_ms / 1000:.1f}s, "
            f"no ANR/jank ≥ threshold (scanned {summary.matched_lines}/{summary.total_lines} lines).\n"
            f"Drill: anr_watcher.py --get-details {summary.session_id}"
        )
    lines = [
        f"Session {summary.session_id}: {summary.duration_ms / 1000:.1f}s captured, "
        f"{len(summary.clusters)} clusters ({summary.event_count} events)"
    ]
    icons = {
        Severity.MINOR: "·",
        Severity.WARN: "⚠",
        Severity.CRITICAL: "‼",
        Severity.FROZEN: "🛑",
    }
    for cluster in summary.clusters[:top_n]:
        icon = icons[cluster.severity]
        at = f"{cluster.first_delta_ms / 1000:.1f}s"
        lines.append(
            f"{icon} {cluster.max_duration_ms:.0f}ms × {cluster.count} — "
            f"{cluster.symbol_or_prefix} at {at}"
        )
    lines.append(f"Drill: anr_watcher.py --get-details {summary.session_id} [--cluster N]")
    return "\n".join(lines)


def format_l2(summary: SessionSummary) -> str:
    """Expanded ~300 token output: all clusters + aggregates."""
    parts = [format_l1(summary, top_n=len(summary.clusters))]
    sev_hist = _severity_histogram(summary.clusters)
    parts.append("Severity: " + ", ".join(f"{k}={v}" for k, v in sev_hist.items() if v))
    aggregates = summary.aggregates or {}
    bursts = aggregates.get("bursts", [])
    if bursts:
        burst_str = "; ".join(
            f"{b['count']} in {(b['ends_at_ms'] - b['starts_at_ms'])}ms @ {b['starts_at_ms'] / 1000:.1f}s"
            for b in bursts[:3]
        )
        parts.append(f"Bursts: {burst_str}")
    quiet = aggregates.get("quiet_periods", [])
    if quiet:
        parts.append(f"Quiet periods: {len(quiet)} (longest {max(q['gap_ms'] for q in quiet)}ms)")
    proc = aggregates.get("process_distribution", {})
    if len(proc) > 1:
        top_proc = sorted(proc.items(), key=lambda kv: kv[1], reverse=True)[:3]
        parts.append("Tags: " + ", ".join(f"{p}({c})" for p, c in top_proc))
    parts.append(
        f"Lines: matched {summary.matched_lines}/{summary.total_lines}, "
        f"dropped {summary.dropped_below_threshold} sub-threshold"
    )
    return "\n".join(parts)


def format_cluster_detail(cluster: Cluster, events: list[NormalisedEvent]) -> str:
    """L3: per-event detail for a single cluster."""
    lines = [
        f"Cluster: {cluster.symbol_or_prefix}",
        f"  fingerprint={cluster.fingerprint} severity={cluster.severity.value} "
        f"kind={cluster.sample_event.kind}",
        f"  count={cluster.count} max={cluster.max_duration_ms:.0f}ms "
        f"total={cluster.total_duration_ms:.0f}ms first@{cluster.first_delta_ms}ms",
    ]
    for event in events[:20]:
        frame_str = f" frames={event.frames}" if event.frames is not None else ""
        lines.append(
            f"  · t={event.delta_ms}ms duration={event.duration_ms:.0f}ms{frame_str} "
            f"tag={event.process} pid={event.pid}"
        )
        if event.raw_message:
            lines.append(f"      msg: {event.raw_message[:120]}")
        if event.reason:
            lines.append(f"      reason: {event.reason[:120]}")
    return "\n".join(lines)


def format_diff(diff: dict) -> str:
    """Render a diff_sessions() result for human + agent consumption."""
    if diff.get("version_mismatch"):
        return (
            f"⚠ fingerprint_version mismatch: A={diff['fingerprint_version_a']} "
            f"B={diff['fingerprint_version_b']}. Structural compare skipped."
        )
    new = diff.get("new_clusters", [])
    resolved = diff.get("resolved_clusters", [])
    drift = diff.get("drift", [])
    stable = diff.get("stable_count", 0)
    verdict = diff.get("verdict", "no change")
    lines = [f"Diff {diff['session_a']} → {diff['session_b']}: {verdict}"]
    if new:
        lines.append(f"New ({len(new)}):")
        for cluster in new[:5]:
            lines.append(
                f"  + {cluster['severity']} {cluster['max_duration_ms']:.0f}ms × "
                f"{cluster['count']} — {cluster['symbol_or_prefix']}"
            )
    if resolved:
        lines.append(f"Resolved ({len(resolved)}):")
        for cluster in resolved[:5]:
            lines.append(
                f"  - {cluster['severity']} {cluster['max_duration_ms']:.0f}ms × "
                f"{cluster['count']} — {cluster['symbol_or_prefix']}"
            )
    if drift:
        lines.append(f"Drift ({len(drift)}):")
        for entry in drift[:5]:
            # 0 → N has no percentage; see diff_sessions for the shape.
            delta = entry["delta_pct"]
            delta_str = "from 0" if entry.get("from_zero") else f"{delta:+.0f}%"
            lines.append(
                f"  ~ {entry['symbol_or_prefix']}: "
                f"{entry['max_duration_ms_a']:.0f} → {entry['max_duration_ms_b']:.0f}ms "
                f"({delta_str})"
            )
    if stable:
        lines.append(f"Stable: {stable} cluster(s) unchanged")
    not_comparable = diff.get("not_comparable", [])
    if not_comparable:
        lines.append(
            f"Not comparable ({len(not_comparable)}): jank whose process was never named, "
            f"so only its pid identifies it, and a pid does not survive a restart:"
        )
        for cluster in not_comparable[:5]:
            lines.append(
                f"  ? {cluster['session']}: {cluster['severity']} "
                f"{cluster['max_duration_ms']:.0f}ms × {cluster['count']} — "
                f"{cluster['symbol_or_prefix']}"
            )
    return "\n".join(lines)


def _severity_histogram(clusters: list[Cluster]) -> dict[str, int]:
    """Total event count per severity band across clusters."""
    hist = {s.value: 0 for s in Severity}
    for cluster in clusters:
        hist[cluster.severity.value] += cluster.count
    return hist


# === STAGE 10: TOKEN BUDGET ===


def estimate_tokens(text: str) -> int:
    """Documented char/4 heuristic. Real tokenizers differ ~10%; tests use this estimator."""
    return len(text) // 4


def compress_to_budget(
    summary: SessionSummary, max_tokens: int | None, default_top_n: int = 3
) -> str:
    """Pick the densest level that fits ``max_tokens``.

    Order: L2 (full) → L1 (top-N) → L0 (one-liner). When ``max_tokens`` is
    ``None`` we return L1 unconditionally.
    """
    if max_tokens is None:
        return format_l1(summary, top_n=default_top_n)
    if max_tokens >= 200:
        candidate = format_l2(summary)
        if estimate_tokens(candidate) <= max_tokens:
            return candidate
    if max_tokens >= 60:
        candidate = format_l1(summary, top_n=default_top_n)
        if estimate_tokens(candidate) <= max_tokens:
            return candidate
        # Shrink top-N until it fits, never below 1.
        for n in (2, 1):
            candidate = format_l1(summary, top_n=n)
            if estimate_tokens(candidate) <= max_tokens:
                return candidate
    return format_l0(summary)


# === DIFF ===


def diff_sessions(
    summary_a: SessionSummary, summary_b: SessionSummary, drift_threshold_pct: float = 20.0
) -> dict:
    """Compare two SessionSummary instances. Returns a dict structured for format_diff.

    Each ``drift`` entry is ``{fingerprint, symbol_or_prefix, max_duration_ms_a,
    max_duration_ms_b, delta_pct, from_zero}``. ``delta_pct`` is a finite
    percentage, or ``null`` exactly when ``from_zero`` is true: the cluster's
    max duration was 0 in A, so no percentage exists. It used to be
    ``float("inf")``, which ``json.dumps`` writes as the bare token
    ``Infinity`` -- not JSON, so ``--diff --json`` handed a strict parser a
    document it could not read, at precisely the moment something got worse
    (N9). A 0 → N cluster counts as worsened.
    """
    if summary_a.fingerprint_version != summary_b.fingerprint_version:
        return {
            "session_a": summary_a.session_id,
            "session_b": summary_b.session_id,
            "version_mismatch": True,
            "fingerprint_version_a": summary_a.fingerprint_version,
            "fingerprint_version_b": summary_b.fingerprint_version,
            "verdict": "skipped (version mismatch)",
        }
    # A pid-keyed cluster has no identity outside its own session: the same
    # app restarted gets a new pid, and an old pid can be another process.
    # Reporting it as resolved here and new there was a regression invented by
    # a restart, so it is listed as not comparable instead.
    not_comparable = [
        {**_cluster_to_dict(cluster), "session": summary.session_id}
        for summary in (summary_a, summary_b)
        for cluster in summary.clusters
        if cluster.identity == "pid"
    ]
    a_map = {c.fingerprint: c for c in summary_a.clusters if c.identity != "pid"}
    b_map = {c.fingerprint: c for c in summary_b.clusters if c.identity != "pid"}
    new_keys = b_map.keys() - a_map.keys()
    resolved_keys = a_map.keys() - b_map.keys()
    shared_keys = a_map.keys() & b_map.keys()
    drift: list[dict] = []
    stable = 0
    for key in shared_keys:
        ca, cb = a_map[key], b_map[key]
        if ca.max_duration_ms == 0 and cb.max_duration_ms == 0:
            stable += 1
            continue
        # 0 → N: a previously-silent cluster now stalls. No percentage exists,
        # and it always counts as drift.
        from_zero = ca.max_duration_ms == 0
        delta_pct: float | None
        if from_zero:
            delta_pct = None
        elif cb.max_duration_ms == 0:
            # N → 0: cluster present in A but flat in B; fully improved.
            delta_pct = -100.0
        else:
            delta_pct = (cb.max_duration_ms - ca.max_duration_ms) / ca.max_duration_ms * 100
        if delta_pct is None or abs(delta_pct) >= drift_threshold_pct:
            drift.append(
                {
                    "fingerprint": key,
                    "symbol_or_prefix": cb.symbol_or_prefix,
                    "max_duration_ms_a": ca.max_duration_ms,
                    "max_duration_ms_b": cb.max_duration_ms,
                    "delta_pct": delta_pct,
                    "from_zero": from_zero,
                }
            )
        else:
            stable += 1
    # Unequal naming coverage: a named cluster with no counterpart, while the
    # other session holds a pid-keyed cluster of the same signature, may BE
    # that counterpart under a pid nobody could name. Calling it resolved or
    # new concluded absence from a failed identification -- "improvement" one
    # way round, "regression" the other (review of PR #26).
    unnamed_a = {_signature(c) for c in summary_a.clusters if c.identity == "pid"}
    unnamed_b = {_signature(c) for c in summary_b.clusters if c.identity == "pid"}
    uncertain_new = {k for k in new_keys if _signature(b_map[k]) in unnamed_a}
    uncertain_resolved = {k for k in resolved_keys if _signature(a_map[k]) in unnamed_b}
    not_comparable.extend(
        {**_cluster_to_dict(cluster), "session": session}
        for keys, cluster_map, session in (
            (uncertain_resolved, a_map, summary_a.session_id),
            (uncertain_new, b_map, summary_b.session_id),
        )
        for cluster in (cluster_map[k] for k in sorted(keys))
    )
    new_clusters = [_cluster_to_dict(b_map[k]) for k in new_keys - uncertain_new]
    resolved_clusters = [_cluster_to_dict(a_map[k]) for k in resolved_keys - uncertain_resolved]
    new_critical = sum(
        1 for c in new_clusters if c["severity"] in (Severity.CRITICAL.value, Severity.FROZEN.value)
    )
    if new_critical:
        verdict = f"regression: {new_critical} new critical"
    elif new_clusters:
        verdict = f"regression: {len(new_clusters)} new minor"
    elif resolved_clusters and not drift:
        verdict = f"improvement: {len(resolved_clusters)} resolved"
    elif drift:
        worsened = sum(1 for d in drift if d["from_zero"] or d["delta_pct"] > 0)
        verdict = f"drift: {worsened} worsened, {len(drift) - worsened} improved"
    else:
        verdict = "no change"
    # What the comparison could not see. "no change; 2 not comparable" read as
    # no change while a set-aside cluster could be a new app janking -- the
    # signature is only kind and message, and every Choreographer line reads
    # the same (review of PR #26). A summary saved before its worker finished
    # may be missing events the same way. Nothing else changed: inconclusive.
    # Something did: the verdict stands, and names what it stands on.
    caveats = [
        f"{summary.session_id} is incomplete"
        for summary in (summary_a, summary_b)
        if summary.incomplete
    ]
    if not_comparable:
        plural = "s" if len(not_comparable) != 1 else ""
        caveats.insert(0, f"{len(not_comparable)} cluster{plural} could not be compared")
    if caveats:
        verdict = (
            "inconclusive: " + "; ".join(dict.fromkeys(caveats))
            if verdict == "no change"
            else f"{verdict}; " + "; ".join(dict.fromkeys(caveats))
        )
    return {
        "session_a": summary_a.session_id,
        "session_b": summary_b.session_id,
        "version_mismatch": False,
        "new_clusters": new_clusters,
        "resolved_clusters": resolved_clusters,
        "drift": drift,
        "stable_count": stable,
        "not_comparable": not_comparable,
        "incomplete": [s.session_id for s in (summary_a, summary_b) if s.incomplete],
        "verdict": verdict,
    }


def _signature(cluster: Cluster) -> str:
    """The cluster's fingerprint with its site left out: what it is, not whose."""
    sample = cluster.sample_event
    return compute_fingerprint(sample.symbol, sample.message_prefix, sample.kind)


def _cluster_to_dict(cluster: Cluster) -> dict:
    """Lightweight dict view for diff output (skips full sample_event for token economy)."""
    return {
        "fingerprint": cluster.fingerprint,
        "symbol_or_prefix": cluster.symbol_or_prefix,
        "severity": cluster.severity.value,
        "count": cluster.count,
        "max_duration_ms": cluster.max_duration_ms,
        "first_delta_ms": cluster.first_delta_ms,
        "identity": cluster.identity,
    }


# === SERIALISATION HELPERS ===


def cluster_to_json(cluster: Cluster) -> dict:
    """JSON-serialisable representation of a Cluster (handles enum + nested dataclass)."""
    # asdict() already serialises Severity (StrEnum) members via their string value.
    return asdict(cluster)


def summary_to_json(summary: SessionSummary) -> dict:
    """JSON-serialisable representation of a SessionSummary."""
    return {
        "session_id": summary.session_id,
        "started_at": summary.started_at,
        "duration_ms": summary.duration_ms,
        "event_count": summary.event_count,
        "dropped_below_threshold": summary.dropped_below_threshold,
        "matched_lines": summary.matched_lines,
        "total_lines": summary.total_lines,
        "fingerprint_version": summary.fingerprint_version,
        "incomplete": summary.incomplete,
        "clusters": [cluster_to_json(c) for c in summary.clusters],
        "aggregates": summary.aggregates,
    }


def summary_from_json(payload: dict) -> SessionSummary:
    """Rehydrate a SessionSummary from disk JSON."""
    clusters = [_cluster_from_json(c) for c in payload.get("clusters", [])]
    return SessionSummary(
        session_id=payload["session_id"],
        started_at=payload["started_at"],
        duration_ms=payload["duration_ms"],
        event_count=payload["event_count"],
        dropped_below_threshold=payload.get("dropped_below_threshold", 0),
        matched_lines=payload.get("matched_lines", 0),
        total_lines=payload.get("total_lines", 0),
        clusters=clusters,
        aggregates=payload.get("aggregates", {}),
        fingerprint_version=payload.get("fingerprint_version", 1),
        incomplete=payload.get("incomplete", False),
    )


def _cluster_from_json(payload: dict) -> Cluster:
    sample = _event_from_dict(payload["sample_event"])
    return Cluster(
        fingerprint=payload["fingerprint"],
        count=payload["count"],
        max_duration_ms=payload["max_duration_ms"],
        total_duration_ms=payload["total_duration_ms"],
        first_delta_ms=payload["first_delta_ms"],
        severity=Severity(payload["severity"]),
        symbol_or_prefix=payload["symbol_or_prefix"],
        sample_event=sample,
        identity=payload.get("identity"),
    )


def _event_from_dict(payload: dict) -> NormalisedEvent:
    return NormalisedEvent(
        delta_ms=payload["delta_ms"],
        process=payload["process"],
        pid=payload["pid"],
        duration_ms=payload["duration_ms"],
        severity=Severity(payload["severity"]),
        symbol=payload.get("symbol"),
        message_prefix=payload["message_prefix"],
        fingerprint=payload["fingerprint"],
        raw_message=payload.get("raw_message", ""),
        frames=payload.get("frames"),
        kind=payload.get("kind", "jank"),
        reason=payload.get("reason"),
        app_pid=payload.get("app_pid"),
        app=payload.get("app"),
        identity=payload.get("identity"),
    )


def event_to_jsonl(event: NormalisedEvent) -> str:
    """Encode one normalised event as a single JSONL line."""
    return json.dumps(asdict(event), separators=(",", ":"))


def event_from_jsonl(line: str) -> NormalisedEvent:
    """Decode a single JSONL line back to NormalisedEvent."""
    return _event_from_dict(json.loads(line))


# === BUILDERS ===


@dataclass
class SummaryBuilder:
    """Compose clusters + aggregates into a SessionSummary in one place."""

    session_id: str
    started_at: str
    duration_ms: int
    matched_lines: int = 0
    total_lines: int = 0
    dropped_below_threshold: int = 0
    extras: dict = field(default_factory=dict)
    # The version the events were fingerprinted under: the session's, as
    # recorded when it was captured -- not this module's, which may be newer.
    fingerprint_version: int = FINGERPRINT_VERSION

    def build(
        self,
        events: list[NormalisedEvent],
        top_n: int | None = None,
    ) -> SessionSummary:
        """Cluster, aggregate, rank, and emit a SessionSummary."""
        clusters = rank_clusters(cluster_events(events), top_n=top_n)
        aggregates = {
            "bursts": detect_temporal_bursts(events),
            "quiet_periods": detect_quiet_periods(events),
            "process_distribution": process_distribution(events),
        }
        aggregates.update(self.extras)
        return SessionSummary(
            session_id=self.session_id,
            started_at=self.started_at,
            duration_ms=self.duration_ms,
            event_count=len(events),
            dropped_below_threshold=self.dropped_below_threshold,
            matched_lines=self.matched_lines,
            total_lines=self.total_lines,
            clusters=clusters,
            aggregates=aggregates,
            fingerprint_version=self.fingerprint_version,
        )
