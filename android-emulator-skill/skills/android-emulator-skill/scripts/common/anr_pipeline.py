#!/usr/bin/env python3
"""ANR/jank filter pipeline — pure functions, no I/O.

Stages: parse → assemble → normalise → threshold → bucket → cluster → aggregate
→ rank → format.

Each function is independently testable; the worker and the ``--stop`` path both
compose them. The clustering / session / formatting machinery is ported nearly
verbatim from the iOS HangBuster pipeline (``hang_pipeline.py``); only the
*parse* layer (``parse_logcat_anr`` for one line of text, ``event_from_record``
for one of logd's records, ``AnrRecordAssembler`` for the stream) and the
severity bands are Android-native.

Every reader of a log reads logd's records (``logcat -B``, ``common.logcat``)
and goes through ``AnrRecordAssembler`` (or ``parse_logcat_stream`` for a
finite dump): one stall is reported by two subsystems, and only the assembler
knows which reports are the same incident (D5) -- and which were written by
system_server at all.

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
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, tzinfo
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.logcat import LogRecord

# === CONSTANTS ===

FINGERPRINT_VERSION = 3
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
``--diff`` will not compare across sessions -- a restarted app has a new pid.

v3 (2026-10, wave 4): an ANR is filed under a package only on evidence. One
WindowManager alone reported is an ANR of a WINDOW (``identity: "window"``,
hashed by its title, no symbol scraped from it); one whose process
ActivityManager named but no uid confirmed as that package's is an ANR of a
PROCESS (``identity: "process"``). v2 filed both under the package their text
claimed, so a v2 fallback reads back as a comparable, package-named cluster:
compared with a v3 session it was "resolved" (Codex review of PR #39)."""

# Each dropped frame on a 60Hz display is ~16.7ms of main-thread stall.
FRAME_MS = 16.7

# Event identities that hold only inside one session (``NormalisedEvent
# .identity``): a pid, which a restart renumbers; a window, whose title is the
# app's text; and a process name no uid confirmed, which any app may choose
# (android:process). ``--diff`` compares none of them.
_NO_IDENTITY = frozenset({"pid", "window", "process"})

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

# logcat threadtime format, at millisecond or (``-v usec``, what the live
# readers ask for) microsecond precision:
#   MM-DD HH:MM:SS.mmm[uuu]  PID  TID  PRIORITY  TAG: message
_LOG_LINE_PATTERN = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}(?:\d{3})?)"
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
# "PID:", "Reason:", "ErrorId:", "Frozen:", load, pressure, CPU usage -- one
# message to logd (recorded: logcat_anr_broadcast_records), which logcat's text
# prints as many lines under one header (logcat_anr_broadcast_stream).
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
# held as a FALLBACK: it is dropped when ActivityManager names the same WINDOW
# within ANR_CORRELATION_MS, either side, and is an event otherwise -- an ANR
# of a window, which names no package (see _WINDOW_REPORT_RE).
_ANR_PRECURSOR_TAGS = frozenset({"WindowManager"})

# How far apart, by logcat timestamp, WindowManager's report and
# ActivityManager's record of one package's ANR may be and still be one
# incident. Twice the 5.06 s measured for the duplicate case above.
ANR_CORRELATION_MS = 10_000

# ActivityManager's verdict on a report for a stall it already recorded.
_ANR_DUPLICATE_PREFIX = "Skipping duplicate ANR:"

# What WindowManager's line says timed out: "...}. Reason:Input dispatching ..."
_WINDOW_REASON_RE = re.compile(r"\.\s*Reason:\s*(.+?)\s*$")

# WindowManager's report prints the window as WindowState.toString does:
#   "ANR in Window{9274f0b u0 com.pkg/com.pkg.DefaultActivity}. Reason:Input ..."
# (recorded: logcat_anr_input_records, logcat_anr_broadcast_records). Only the
# OPENING is system_server's own -- the window's identity hash
# (System.identityHashCode, in hex) and its user. What follows is the window's
# TITLE, which the app sets (WindowManager.LayoutParams.setTitle; an activity's
# defaults to its component, which is what the recordings show). So the title
# is display text: it names no package, and nothing in it is classified. Read
# as an identity, a title "com.example.victim/com.example.victim.Default
# Activity" made an ANR of that app beside the real one, and "Skipped 80
# frames!" made the report jank (final review of v0.8.0, wave 4).
_WINDOW_REPORT_RE = re.compile(r"ANR in Window\{(?P<hash>[0-9a-f]+) u\d+ ")

# The window an input timeout names: the input channel's name, which is the
# same identity hash and the same title -- "Input dispatching timed out
# (9274f0b com.pkg/... (server) is not responding". Recorded in two places:
# the Reason: line of ActivityManager's report of an input ANR
# (logcat_anr_input_records), and ActivityManager's duplicate verdict on a
# window report after a broadcast ANR (logcat_anr_broadcast_records), after
# the error state it names. A broadcast ANR's own report names NO window
# (its Reason is the broadcast). The hash, not the title, says which window
# a report and a record share: a title any app may copy, so a title never
# settles a report (Codex review of PR #39: an ANR of B, then a separate
# window titled as B's, and the second vanished).
_TIMED_OUT_WINDOW_RE = re.compile(r"Input dispatching timed out \((?P<hash>[0-9a-f]+) ")
_VERDICT_WINDOW_RE = re.compile(
    rf"{re.escape(_ANR_DUPLICATE_PREFIX)} \S+ Input dispatching timed out \((?P<hash>[0-9a-f]+) "
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
    # How the event's site is keyed: for jank "package" (by ``app``) or "pid"
    # (no name was found); for an ANR WindowManager alone reported, "window"
    # (no package: a window's title is the app's text). None for an event
    # whose record names its site.
    identity: str | None = None
    # The title of the window an ANR of a window was reported for: display
    # text, never an identity (_WINDOW_REPORT_RE).
    window: str | None = None
    # The process an ANR of a process was reported for, by the name
    # ActivityManager gave it, which no uid showed to be a package's.
    process_name: str | None = None
    # An ANR of a window that ActivityManager's report of a broadcast ANR,
    # within the correlation window, may already account for: counted, said.
    possible_duplicate: bool = False
    # For ActivityManager's ANR: whether its process was shown to be the
    # package's -- "verified", "unverified" or "contradicted" (the assembler's
    # ``attribution``), kept so --get-details says it too.
    attribution: str | None = None


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
    # True when the capture may be missing events: --stop gave up waiting for
    # the worker (review of PR #26 -- never a silently short summary), the
    # worker crashed or vanished, the stream broke, or ANR reports could not be
    # authenticated (Codex review of PR #34). ``--diff`` then concludes nothing
    # from what is absent. ``incomplete_reason`` says which, for the text forms.
    incomplete: bool = False
    incomplete_reason: str | None = None
    # Set when ANR reports could not be authenticated (system_server's pid was
    # unknown) and were set aside: ``VERIFICATION_UNKNOWN``, with the count.
    verification: str | None = None
    unverified_anr_lines: int = 0
    # Pids with records no process name could be given to: keyed by pid, and
    # dropped under --package (review of PR #34).
    unnamed_pids: list[int] = field(default_factory=list)
    # Records a stop cut off mid-arrival (lost, and said), and the framing
    # error a stream broke on, if one did (common.logcat's LogFramingError).
    cut_records: int = 0
    stream_failure: str | None = None
    # Times the worker restarted a dead stream. `-T 1` replays only the newest
    # record logged while none was up, so each restart may have lost the rest
    # (wave 4: it was not counted as a loss).
    stream_restarts: int = 0
    # What the record the session started on showed: `logcat -T 1` replays the
    # newest record already buffered, which may predate the session, and
    # nothing in a record can show it does not. Shown, never counted,
    # clustered or compared (wave 4).
    ambiguous_tail: list[dict] = field(default_factory=list)
    # ANRs ``--package`` dropped because they name no package -- of a window,
    # or of a process no uid confirmed -- so it can neither keep them nor say
    # they were someone else's (wave 4).
    unattributed_anrs: int = 0
    # ANRs of a window that a broadcast ANR's report nearby may already
    # account for: counted, and said (``NormalisedEvent.possible_duplicate``).
    possible_duplicates: int = 0


# What a summary says when its ANR reports could not be authenticated.
VERIFICATION_UNKNOWN = "system_server pid unknown — ANR records could not be authenticated"


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
    if classified.get("window") is not None:
        event["window"] = classified["window"]
    return event


def _classify_anr(tag: str, message: str) -> dict | None:
    """Map a (tag, message) pair to ANR/jank metadata, or None if irrelevant."""
    lower = message.lower()

    # 0. WindowManager's report of an unresponsive window, read for its
    # opening alone and before anything else: the rest is the window's title,
    # the app's text (_WINDOW_REPORT_RE), so it is kept as ``window`` -- to be
    # shown, never to name a package or to be classified. "ANR in Window{...}"
    # also matches the generic form below, which read the word "Window" as a
    # package; the title then made the package (wave 4).
    window = _WINDOW_REPORT_RE.match(message)
    if window:
        return {
            "kind": "anr",
            "frames": None,
            "duration_ms": 5000.0,
            "component": None,
            "anr_package": None,
            "window": _window_title(message[window.end() :]),
        }

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

    # 2. ActivityManager hard ANR.
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


def _window_title(rest: str) -> str:
    """The window's title: what WindowManager's report prints after its opening.

    Up to the first "}. Reason:", which closes the window as recorded. The
    title is the app's text and may hold that too, so this is a boundary for
    display, nothing more.
    """
    end = rest.find("}. Reason:")
    return rest[:end] if end != -1 else rest


def is_anr_message(tag: str, message: str) -> bool:
    """Return True if a (tag, message) pair describes an ANR/jank event."""
    return _classify_anr(tag, message) is not None


# === STAGE 1b: ASSEMBLE (lines -> incidents) ===


def _printable_tag(tag: str) -> str:
    """A tag with its non-printable characters escaped, as ``LogRecord.lines`` prints it."""
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in tag)


def event_from_record(record: LogRecord, tz: tzinfo | None = None) -> dict | None:
    """Classify ONE of logd's records (``logcat -B``) as an ANR/jank signal, else None.

    The record's text is taken from :meth:`LogRecord.lines` -- printed as
    ``-v threadtime -v usec`` would, its control characters escaped -- so what
    an event stores and prints is what a reader of the log can see, and no
    message can carry a line of its own. The classification is
    :func:`parse_logcat_anr`'s, on the record's first line. A record is one
    log message whole, so an ActivityManager ANR report arrives as ONE record:
    its ``PID:`` and ``Reason:`` lines (recorded: logcat_anr_broadcast_records)
    are read off it here, not assembled from later lines.

    Who wrote the record is ``record.pid`` -- logd's, from the socket's
    credentials -- never text. Whether an ANR report counts is
    :class:`AnrRecordAssembler`'s to decide.

    Args:
        record: One record.
        tz: The zone to print its time in (default: this host's, as
            ``LogRecord.lines``).
    """
    prefix = replace(record, message="").lines(tz)[0]
    pieces = [line[len(prefix) :] for line in record.lines(tz)]
    tag = _printable_tag(record.tag)
    classified = _classify_anr(tag, pieces[0].strip())
    if classified is None:
        return None
    event: dict = {
        # "MM-DD HH:MM:SS.uuuuuu": the head of the printed header.
        "timestamp": prefix[:21],
        "pid": record.pid,
        "process": tag,
        "message": pieces[0].strip(),
        "kind": classified["kind"],
        "duration_ms": classified["duration_ms"],
    }
    for key in ("frames", "component", "anr_package", "window"):
        if classified.get(key) is not None:
            event[key] = classified[key]
    if classified["kind"] == "anr":
        for piece in pieces[1:]:
            pid = _ANR_RECORD_PID_RE.match(piece)
            if pid and "app_pid" not in event:
                event["app_pid"] = int(pid.group(1))
            reason = _ANR_RECORD_REASON_RE.match(piece)
            if reason and "reason" not in event:
                event["reason"] = reason.group(1)
    return event


class AnrRecordAssembler:
    """Turn logd's records into ANR/jank events, one per incident.

    ``event_from_record`` answers "what does this record say"; this answers
    "which records are the same stall", which no single record can:

    - **One ANR is one record.** ActivityManager writes its report -- ``ANR
      in``, ``PID:`` (the stalled app's own pid; the record's pid is
      system_server's), ``Reason:`` (the only statement of what timed out),
      and the load and CPU tables -- as ONE log message (recorded:
      logcat_anr_broadcast_records), so it is one event with ``app_pid`` and
      ``reason`` (D5).
    - **WindowManager's ANR report is a fallback.** It is a report to
      ActivityManager, which normally records the incident itself, before or
      after it (see ``_ANR_PRECURSOR_TAGS``). It is dropped when ActivityManager
      names the same WINDOW -- its identity hash, in the input timeout both
      quote -- within ``ANR_CORRELATION_MS``, by log time: in its report of an
      input ANR, or in its duplicate verdict. Otherwise it is the event,
      emitted once that window has passed (later records of system_server's,
      or a quiet stream) or the stream ends. Emitted late, so it can follow
      events logged after it. A report ActivityManager's report of a
      broadcast ANR (which names no window) may answer is kept, with
      ``possible_duplicate``: the window's title could say whose window it is,
      but the title is the app's (Codex review of PR #39).
    - **A window names no package.** What follows the window's hash is its
      title, the app's to set (``_WINDOW_REPORT_RE``). A report nothing
      answers is an ANR of a window: ``anr_package`` None, the title kept as
      ``window``, display text, which neither names nor settles anything.
    - **ActivityManager names a process, not a package.** ``ANR in <name>``
      is the stalled process's name (``mApp.processName``), and an app may
      name its process after another package (``android:process``; recorded:
      logcat_anr_foreign_process_records, "ANR in com.example.victim" from the
      fixture app's own uid). So the package stands only when
      ``subject_owner`` shows the report's ``PID:`` is that package's
      process; otherwise the ANR is kept as an ANR of a PROCESS --
      ``anr_package`` None, the name kept as ``process_name``, and
      ``attribution`` saying why.
    - **Only system_server's records move the log clock.** A record's time is
      its writer's (logd takes it from the client's header, unlike the pid),
      so an app's record dated a minute ahead released a held report before
      ActivityManager's record of the same stall came: one ANR counted twice.

    Jank and StrictMode records are single incidents and come straight out.
    Every reader -- ``--watch``, ``--since`` and the session worker -- feeds its
    records through one of these, so they agree on what one ANR is.

    **An ANR report counts only as system_server wrote it** (review of PR #34).
    Any app may log under ActivityManager's or WindowManager's tag: one wrote
    ``ANR in <another package>`` and was counted, and one wrote ``Skipping
    duplicate ANR: <package>/…`` and settled a real WindowManager report, so
    that ANR vanished. An ``ANR in`` record and a duplicate verdict count only
    with all of: the tag exactly, the sentence as the exact opening of the
    record's message (a later line of a system_server message is whatever an
    app put there -- a broadcast's action), and the record's pid -- logd's,
    from the writing socket, which no tag or message can forge (recorded:
    logcat_tag_injection_records) -- being system_server's
    (``system_server_pid``, asked once, when the first such record arrives).
    When that pid is unknown nothing is authenticated, so nothing counts --
    and :attr:`unverified` says how many such records were set aside, for the
    reader to disclose: a reader that could not authenticate has not found
    "no ANRs".

    Args:
        system_server_pid: system_server's pid, or a function returning it
            (None when the device cannot say). None: no report counts.
        subject_owner: ``(pid, package) -> True`` when the process ``pid``
            is shown to be ``package``'s, False when shown not to be, None
            when nothing shows either; asked only for an authenticated
            report. None (the default): nothing is shown, so no ANR is filed
            under a package.
    """

    def __init__(
        self,
        system_server_pid: str | Callable[[], str | None] | None = None,
        subject_owner: Callable[[int, str], bool | None] | None = None,
    ) -> None:
        # WindowManager reports waiting for ActivityManager.
        self._held: list[_HeldReport] = []
        # Log ms of ActivityManager's latest report naming a window, by that
        # window; and of every report of its, with its printed time, for a
        # released report to say which one it may be a duplicate of.
        self._recorded_windows: dict[str, int] = {}
        self._reported_at: list[tuple[int, str]] = []
        self._system_server = system_server_pid
        self._system_server_asked = not callable(system_server_pid)
        self._subject_owner = subject_owner
        # Report records set aside because system_server's pid was unknown.
        self.unverified = 0

    def _written_by_system_server(self, writer: int) -> bool | None:
        """True or False -- or None when system_server's pid is unknown."""
        if not self._system_server_asked:
            self._system_server_asked = True
            self._system_server = self._system_server()
        if self._system_server is None:
            self.unverified += 1
            return None
        return str(writer) == str(self._system_server)

    def _known_to_be_system_servers(self, writer: int) -> bool:
        """Whether system_server wrote it, as far as already known -- asking nothing.

        A report is held only once system_server's pid is known, so while one
        is held this is a true answer; with none held it is never needed.
        """
        known = self._system_server if self._system_server_asked else None
        return known is not None and str(writer) == str(known)

    def feed(self, record: LogRecord, *, ambiguous: bool = False) -> list[dict]:
        """Take one record; return the events it completes (often none).

        Args:
            record: The next record.
            ambiguous: The record may predate the read (`logcat -T 1`'s
                replay): every event it makes carries ``ambiguous_tail``, for
                the reader to show and not count.
        """
        at_ms = record.sec * 1000 + record.nsec // 1_000_000
        completed: list[dict] = []
        if self._held and self._known_to_be_system_servers(record.pid):
            completed = self._release(lambda held_ms: at_ms - held_ms > ANR_CORRELATION_MS)
        tag = record.tag
        if tag == "ActivityManager" and record.message.startswith(_ANR_DUPLICATE_PREFIX):
            if not self._written_by_system_server(record.pid):
                return completed
            # ActivityManager's own word that a report belongs to a stall it
            # has already recorded. It settles a held report only for the
            # window it names, by hash, and only within the window of time.
            named = _VERDICT_WINDOW_RE.match(record.message)
            if named:
                self._settle(at_ms, named["hash"])
            return completed

        event = event_from_record(record)
        if event is None:
            return completed
        if ambiguous:
            event["ambiguous_tail"] = True
        if event["kind"] != "anr":
            completed.append(event)
            return completed
        if not (
            tag in ("ActivityManager", *_ANR_PRECURSOR_TAGS)
            and record.message.startswith("ANR in ")
            and self._written_by_system_server(record.pid)
        ):
            return completed
        if tag in _ANR_PRECURSOR_TAGS:
            opening = _WINDOW_REPORT_RE.match(record.message)
            report = _HeldReport(
                at_ms=at_ms,
                window=opening["hash"] if opening else None,
                # An ANR of a window, which names no package (_WINDOW_REPORT_RE).
                event={
                    **event,
                    "anr_package": None,
                    "component": None,
                    "window": event["window"] if "window" in event else event.get("anr_package"),
                },
            )
            if not self._accounted_for(report):
                reason = _WINDOW_REASON_RE.search(event["message"])
                if reason:
                    report.event["reason"] = reason.group(1)
                self._held.append(report)
            return completed
        self._attribute(event)
        self._reported_at.append((at_ms, event["timestamp"]))
        timed_out = _TIMED_OUT_WINDOW_RE.match(event.get("reason") or "")
        if timed_out:
            self._recorded_windows[timed_out["hash"]] = at_ms
            self._settle(at_ms, timed_out["hash"])
        completed.append(event)
        return completed

    def _attribute(self, event: dict) -> None:
        """File ActivityManager's report under its package only when its process is shown to be it.

        ``ANR in <name>`` names a PROCESS (``mApp.processName``), which any
        app may name after another package; ``PID:`` is that process. The
        name stands as the package only when ``subject_owner`` shows the pid
        is the package's. Otherwise the ANR is one of a process: no package,
        no component, the name kept as ``process_name``.
        """
        claimed, pid = event.get("anr_package"), event.get("app_pid")
        owned = None
        if claimed and pid is not None and self._subject_owner is not None:
            owned = self._subject_owner(int(pid), claimed)
        if owned:
            event["attribution"] = "verified"
            return
        event["attribution"] = "contradicted" if owned is False else "unverified"
        event["process_name"] = claimed
        event["anr_package"] = None
        event["component"] = None

    def _accounted_for(self, report: _HeldReport) -> bool:
        """Whether ActivityManager has already named the window a report is of."""
        recorded_ms = self._recorded_windows.get(report.window) if report.window else None
        return recorded_ms is not None and abs(report.at_ms - recorded_ms) <= ANR_CORRELATION_MS

    def _settle(self, at_ms: int, window: str) -> None:
        """Drop the held reports of ``window`` logged near ``at_ms``."""
        self._held = [
            held
            for held in self._held
            if held.window != window or abs(at_ms - held.at_ms) > ANR_CORRELATION_MS
        ]

    def flush(self, quiet_seconds: float = 0.0) -> list[dict]:
        """The stream has gone quiet: emit what quiet completes.

        A held WindowManager report is complete only once the stream has been
        quiet for the whole correlation window -- ActivityManager logs in real
        time, so a record that has not come by then is not coming.

        Args:
            quiet_seconds: How long the stream has delivered nothing.
        """
        if quiet_seconds * 1000 >= ANR_CORRELATION_MS:
            return self._release(lambda _held_ms: True)
        return []

    def close(self) -> list[dict]:
        """The stream has ended: emit everything still held."""
        return self._release(lambda _held_ms: True)

    def _release(self, due) -> list[dict]:
        """Emit, as events, the held WindowManager reports ``due`` says are due.

        One ActivityManager reported near -- a broadcast ANR's report, which
        names no window -- may be the same stall; nothing but the title, the
        app's, could say so, so the report is kept and says it may be, and of
        which (``possible_duplicate_of``: that report's printed time).
        """
        released = []
        for held in self._held:
            if not due(held.at_ms):
                continue
            near = [
                stamp
                for ms, stamp in self._reported_at
                if abs(held.at_ms - ms) <= ANR_CORRELATION_MS
            ]
            if near:
                held.event["possible_duplicate"] = True
                held.event["possible_duplicate_of"] = near[-1]
            released.append(held.event)
        self._held = [held for held in self._held if not due(held.at_ms)]
        return released


@dataclass
class _HeldReport:
    """A WindowManager report waiting for ActivityManager to account for it.

    ``window`` is the window's identity hash (system_server's); ``event`` the
    ANR of a window it becomes if nothing does.
    """

    at_ms: int
    window: str | None
    event: dict


def parse_logcat_stream(
    records: Iterable[LogRecord],
    system_server_pid: str | Callable[[], str | None] | None = None,
    subject_owner: Callable[[int, str], bool | None] | None = None,
) -> list[dict]:
    """Every ANR/jank incident in a finite run of records (a ``logcat -d -B`` dump).

    ``system_server_pid`` authenticates the ANR reports, and ``subject_owner``
    their packages (see :class:`AnrRecordAssembler`); without the first none
    counts, and without the second none is filed under a package.
    """
    assembler = AnrRecordAssembler(system_server_pid, subject_owner)
    events: list[dict] = []
    for record in records:
        events.extend(assembler.feed(record))
    events.extend(assembler.close())
    return events


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
    window = raw_event.get("window")
    # Prefer the explicit component (high signal) over a regex-scraped symbol.
    symbol = raw_event.get("component") or extract_symbol(message)
    prefix = normalise_message(message)
    app = raw_event.get("app") or raw_event.get("anr_package")
    process_name = raw_event.get("process_name")
    if kind == "anr" and window is not None and not app:
        # An ANR of a window: its title is the app's text, so no symbol is
        # scraped from it -- a title reading as another app's component made
        # the cluster that app's. Its label says it is a title, and its
        # window's hash (a new one per window) is left out of the prefix.
        symbol = None
        prefix = normalise_message(f"ANR in a window titled {window}")
    elif kind == "anr" and process_name is not None and not app:
        # An ANR of a process no uid showed to be the package it is named
        # after (android:process): labelled so, with nothing scraped from it.
        symbol = None
        prefix = normalise_message(f"ANR in a process named {process_name}")
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
    elif kind == "anr" and window is not None and not app:
        # Not compared across sessions, as a pid is not (diff_sessions).
        identity = site = "window"
    elif kind == "anr" and process_name is not None and not app:
        identity = site = "process"
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
        window=window if identity == "window" else None,
        process_name=process_name if identity == "process" else None,
        possible_duplicate=bool(raw_event.get("possible_duplicate")),
        attribution=raw_event.get("attribution"),
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


# What every text form says of a summary saved before its worker finished
# (``SessionSummary.incomplete``). It was in the JSON alone: `--get-details`
# read such a summary back as "no ANR/jank ≥ threshold", and the warning that
# said otherwise went to stderr once, at the stop (B6).
INCOMPLETE_NOTE = (
    "Incomplete: saved before the session's worker finished, so events it wrote "
    "later are missing -- what is absent here may simply be unwritten."
)


def _session_label(summary: SessionSummary) -> str:
    """``Session <id>``, marked when the summary is incomplete."""
    marker = " (incomplete)" if summary.incomplete else ""
    return f"Session {summary.session_id}{marker}"


def _nothing_found(summary: SessionSummary, threshold: str) -> str:
    """'No ANR/jank', qualified by what the summary could and could not see."""
    verified = " verified" if summary.verification else ""
    where = " in the part captured" if summary.incomplete else ""
    return f"no{verified} ANR/jank {threshold} threshold{where}"


def _unverified_note(summary: SessionSummary) -> str:
    """The disclosure for ANR reports set aside unauthenticated (empty when none were)."""
    if not summary.verification:
        return ""
    plural = "s" if summary.unverified_anr_lines != 1 else ""
    return (
        f"Unverified: {summary.verification} ({summary.unverified_anr_lines} report "
        f"record{plural} set aside, not counted)."
    )


def _unnamed_note(summary: SessionSummary) -> str:
    """One sentence on the pids no process name was found for (empty when none)."""
    pids = summary.unnamed_pids
    if not pids:
        return ""
    shown = ", ".join(str(pid) for pid in pids[:8]) + (", …" if len(pids) > 8 else "")
    plural = "s" if len(pids) != 1 else ""
    return (
        f"Unnamed: {len(pids)} pid{plural} ({shown}) -- only a process whose verified "
        f"Start proc this session saw is named; the rest are keyed by pid, and "
        f"--package drops them."
    )


def _framing_note(summary: SessionSummary) -> str:
    """What the stream itself lost: a record cut by the stop, or a break (empty when neither)."""
    notes = []
    if summary.stream_failure:
        notes.append(
            f"Broken: the stream stopped being logd's records ({summary.stream_failure}); "
            f"nothing after that is in here."
        )
    if summary.cut_records:
        plural = "s" if summary.cut_records != 1 else ""
        notes.append(
            f"Cut: {summary.cut_records} record{plural} arriving when the session stopped "
            f"{'are' if plural else 'is'} not in here."
        )
    if summary.stream_restarts:
        plural = "s" if summary.stream_restarts != 1 else ""
        notes.append(
            f"Restarted: the stream died and was restarted {summary.stream_restarts} "
            f"time{plural}; of what was logged while it was down, only the newest record "
            f"is in here."
        )
    return " ".join(notes)


def _ambiguous_note(summary: SessionSummary) -> str:
    """The record the session started on, shown and not counted (empty when it was no event)."""
    if not summary.ambiguous_tail:
        return ""
    shown = "; ".join(
        f"{tail.get('kind')} at {tail.get('timestamp')}, "
        f"{tail.get('app') or tail.get('process')}: {str(tail.get('message', ''))[:80]}"
        for tail in summary.ambiguous_tail
    )
    return (
        f"Ambiguous: the record the session started on may predate it (`logcat -T 1` "
        f"first replays the newest one already buffered) -- {shown}; shown, not counted."
    )


def _unattributed_note(summary: SessionSummary) -> str:
    """The ANRs --package dropped for naming no package (empty when none were)."""
    count = summary.unattributed_anrs
    if not count:
        return ""
    plural = "s" if count != 1 else ""
    return (
        f"Unattributed: {count} ANR{plural} named no package anything verified -- a "
        f"window's title is the app's to set, and a process may be named after any "
        f"package -- so --package dropped {'them' if plural else 'it'}."
    )


def _duplicate_note(summary: SessionSummary) -> str:
    """The ANRs of a window that may be a broadcast ANR's duplicate (empty when none are)."""
    count = summary.possible_duplicates
    if not count:
        return ""
    plural = "s" if count != 1 else ""
    return (
        f"Possible duplicate{plural}: {count} ANR{plural} of a window, reported within 10 s "
        f"of an ANR ActivityManager reported without naming its window; only the window's "
        f"title, the app's text, could tie {'them' if plural else 'it'} to it, so "
        f"{'both are' if not plural else 'each is'} counted."
    )


def _incomplete_note(reason: str | None) -> str:
    """The Incomplete: line, saying why when the summary recorded why."""
    if not reason:
        return INCOMPLETE_NOTE
    return f"Incomplete: {reason} -- what is absent here may simply be unseen."


def _disclosures(summary: SessionSummary) -> list[str]:
    """What the summary could not see, one line each, for the text forms."""
    notes = [_incomplete_note(summary.incomplete_reason)] if summary.incomplete else []
    found = (
        _framing_note(summary),
        _unverified_note(summary),
        _unnamed_note(summary),
        _unattributed_note(summary),
        _duplicate_note(summary),
        _ambiguous_note(summary),
    )
    return [*notes, *(note for note in found if note)]


def _markers(summary: SessionSummary) -> str:
    """The disclosures as one bracketed clause, for the one-line form.

    L0 is what a tight token budget falls back to, so it is the form a
    disclosure must survive in: a budget cuts clusters, never what the
    summary could not see (Codex review of PR #34).
    """

    def counted(count: int, noun: str, what: str) -> str:
        return f"{count} {noun}{'s' if count != 1 else ''} {what}"

    marks = []
    if summary.stream_failure:
        marks.append("stream broken")
    if summary.cut_records:
        marks.append(counted(summary.cut_records, "record", "cut by the stop"))
    if summary.stream_restarts:
        marks.append(counted(summary.stream_restarts, "stream restart", "losing a gap"))
    if summary.verification:
        marks.append(counted(summary.unverified_anr_lines, "ANR record", "unverified"))
    if summary.unnamed_pids:
        marks.append(counted(len(summary.unnamed_pids), "pid", "unnamed"))
    if summary.unattributed_anrs:
        marks.append(counted(summary.unattributed_anrs, "ANR", "unattributed"))
    if summary.possible_duplicates:
        marks.append(counted(summary.possible_duplicates, "window ANR", "possibly a duplicate"))
    if summary.ambiguous_tail:
        marks.append("1 record may predate the session, not counted")
    return f" [{'; '.join(marks)}]" if marks else ""


def format_l0(summary: SessionSummary) -> str:
    """Single-line status (~20 tokens). Cache-friendly for agent context."""
    marks = _markers(summary)
    if not summary.clusters:
        return f"{_session_label(summary)}: {_nothing_found(summary, 'above')}.{marks}"
    top = summary.clusters[0]
    critical = sum(
        1 for c in summary.clusters if c.severity in (Severity.CRITICAL, Severity.FROZEN)
    )
    return (
        f"{_session_label(summary)}: {summary.duration_ms / 1000:.1f}s, "
        f"{summary.event_count} events ({critical} critical), top: "
        f"{top.symbol_or_prefix} {top.max_duration_ms:.0f}ms ×{top.count}{marks}"
    )


def format_l1(summary: SessionSummary, top_n: int = 3) -> str:
    """Default ~80-120 token output: header + top-N clusters + drill hint."""
    note = _disclosures(summary)
    if not summary.clusters:
        return "\n".join(
            [
                f"{_session_label(summary)}: {summary.duration_ms / 1000:.1f}s, "
                f"{_nothing_found(summary, '≥')} "
                f"(scanned {summary.matched_lines}/{summary.total_lines} lines).",
                *note,
                f"Drill: anr_watcher.py --get-details {summary.session_id}",
            ]
        )
    lines = [
        f"{_session_label(summary)}: {summary.duration_ms / 1000:.1f}s captured, "
        f"{len(summary.clusters)} clusters ({summary.event_count} events)",
        *note,
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


def format_cluster_detail(
    cluster: Cluster,
    events: list[NormalisedEvent],
    incomplete: bool = False,
    incomplete_reason: str | None = None,
) -> str:
    """L3: per-event detail for a single cluster.

    Args:
        cluster: The cluster.
        events: Its events, as the session stored them.
        incomplete: The session's summary was saved incomplete, so the
            cluster may be missing events and its counts be low.
        incomplete_reason: Why, as the summary recorded it.
    """
    lines = [
        f"Cluster: {cluster.symbol_or_prefix}",
        f"  fingerprint={cluster.fingerprint} severity={cluster.severity.value} "
        f"kind={cluster.sample_event.kind}",
        f"  count={cluster.count} max={cluster.max_duration_ms:.0f}ms "
        f"total={cluster.total_duration_ms:.0f}ms first@{cluster.first_delta_ms}ms",
    ]
    if incomplete:
        lines.append(f"  {_incomplete_note(incomplete_reason)}")
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
        said = attribution_markers(
            attribution=event.attribution,
            process_name=event.process_name,
            window=event.window,
            possible_duplicate=event.possible_duplicate,
        )
        if said:
            lines.append(f"      {said}")
    return "\n".join(lines)


def attribution_markers(
    *,
    attribution: str | None,
    process_name: str | None,
    window: str | None,
    possible_duplicate: bool = False,
    possible_duplicate_of: str | None = None,
) -> str:
    """What a text line says beside an ANR it could not tie to a package, or to one incident.

    JSON carries ``attribution``, ``process_name``, ``window`` and
    ``possible_duplicate``; a line of text read alone said ``ANR in
    com.example.victim`` as if it were that package's (Codex review of PR
    #39). Empty when there is nothing to qualify.
    """
    marks = []
    if process_name is not None:
        if attribution == "contradicted":
            marks.append(f"[contradicted: process {process_name} runs under another package's uid]")
        else:
            marks.append(f"[unverified: process {process_name} names no verified package]")
    if window is not None:
        marks.append("[an ANR of a window: its title names no package]")
    if possible_duplicate_of:
        marks.append(f"[possible duplicate of the ANR at {possible_duplicate_of}]")
    elif possible_duplicate:
        marks.append("[possible duplicate of an ANR reported within 10 s that names no window]")
    return " ".join(marks)


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
            f"Not comparable ({len(not_comparable)}): what no package identifies -- jank "
            f"whose process was never named (a pid does not survive a restart), or an ANR "
            f"of a window (its title is the app's to set) -- and what either may be:"
        )
        for cluster in not_comparable[:5]:
            lines.append(
                f"  ? {cluster['session']}: {cluster['severity']} "
                f"{cluster['max_duration_ms']:.0f}ms × {cluster['count']} — "
                f"{cluster['symbol_or_prefix']}"
            )
    unconfirmed = diff.get("unconfirmed", [])
    if unconfirmed:
        lines.append(
            f"Unconfirmed ({len(unconfirmed)}): missing only from a session that is "
            f"incomplete or holds evidence it did not count, which may be them:"
        )
        for cluster in unconfirmed[:5]:
            lines.append(
                f"  ? {cluster['session']}, absent from {cluster['absent_from']}: "
                f"{cluster['severity']} {cluster['max_duration_ms']:.0f}ms × "
                f"{cluster['count']} — {cluster['symbol_or_prefix']}"
            )
    not_compared = diff.get("not_compared", [])
    if not_compared:
        lines.append(
            f"Not compared ({len(not_compared)}): in both sessions, but a maximum from an "
            f"incomplete one is only a lower bound -- {not_compared[0]['reason']}:"
        )
        for entry in not_compared[:5]:
            lines.append(
                f"  ? {entry['symbol_or_prefix']}: {entry['max_duration_ms_a']:.0f} / "
                f"{entry['max_duration_ms_b']:.0f}ms"
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

    ``unconfirmed`` holds the clusters one session has and the other lacks
    while it was saved ``incomplete`` or holds evidence it did not count (an
    ``ambiguous_tail``, ``unattributed_anrs``) -- each with ``session``
    (where it is) and ``absent_from`` -- in place of calling them resolved or
    new; the verdict names what was not counted. With either
    side incomplete, ``not_compared`` holds the clusters both have, in place of
    a drift or stability verdict: an incomplete side's maximum is only a lower
    bound.
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
    # a restart, so it is listed as not comparable instead. So is an ANR of a
    # window: it names no package, only a title any app may set (wave 4).
    not_comparable = [
        {**_cluster_to_dict(cluster), "session": summary.session_id}
        for summary in (summary_a, summary_b)
        for cluster in summary.clusters
        if cluster.identity in _NO_IDENTITY
    ]
    a_map = {c.fingerprint: c for c in summary_a.clusters if c.identity not in _NO_IDENTITY}
    b_map = {c.fingerprint: c for c in summary_b.clusters if c.identity not in _NO_IDENTITY}
    new_keys = b_map.keys() - a_map.keys()
    resolved_keys = a_map.keys() - b_map.keys()
    shared_keys = a_map.keys() & b_map.keys()
    drift: list[dict] = []
    stable = 0
    # An incomplete summary's maximum is only a lower bound: the events it is
    # missing could raise it. Compared anyway, it read as "improved" one way
    # round and "worsened" the other (review of PR #34). So with either side
    # incomplete, no shared cluster drifts or holds stable; each is listed as
    # not compared, and why.
    incomplete_ids = list(
        dict.fromkeys(s.session_id for s in (summary_a, summary_b) if s.incomplete)
    )
    because = " and ".join(f"{sid} is incomplete" for sid in incomplete_ids)
    not_compared = [
        {
            "fingerprint": key,
            "symbol_or_prefix": b_map[key].symbol_or_prefix,
            "max_duration_ms_a": a_map[key].max_duration_ms,
            "max_duration_ms_b": b_map[key].max_duration_ms,
            "reason": f"not comparable: {because}",
        }
        for key in (sorted(shared_keys) if incomplete_ids else [])
    ]
    for key in shared_keys if not incomplete_ids else ():
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
    # An ANR of a window may be any app's ANR, so a named ANR with no
    # counterpart while the other session holds one is uncertain the same way.
    unnamed_a = {_signature(c) for c in summary_a.clusters if c.identity in _NO_IDENTITY}
    unnamed_b = {_signature(c) for c in summary_b.clusters if c.identity in _NO_IDENTITY}
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
    # Absence is the one thing an incomplete summary cannot show: a cluster
    # missing from a session saved before its worker finished may simply be
    # unwritten. "improvement: 1 resolved; B is incomplete" concluded it
    # anyway (B3). So a cluster absent from the incomplete side is neither
    # resolved (B incomplete) nor new (A incomplete) but unconfirmed; what both
    # sides hold still compares, and so does what the complete side lacks.
    # So, too, a side holding evidence it did not count -- the record it
    # started on, or ANRs --package could not attribute: the missing cluster
    # may be right there, uncounted (Codex review of PR #39: "improvement: 1
    # resolved" against the same ANR kept only as ambiguous_tail).
    uncounted = {s.session_id: _uncounted(s) for s in (summary_a, summary_b) if _uncounted(s)}
    doubtful_a = summary_a.incomplete or summary_a.session_id in uncounted
    doubtful_b = summary_b.incomplete or summary_b.session_id in uncounted
    unconfirmed_resolved = resolved_keys - uncertain_resolved if doubtful_b else set()
    unconfirmed_new = new_keys - uncertain_new if doubtful_a else set()
    unconfirmed = [
        {
            **_cluster_to_dict(cluster_map[k]),
            "session": present.session_id,
            "absent_from": absent.session_id,
        }
        for keys, cluster_map, present, absent in (
            (unconfirmed_resolved, a_map, summary_a, summary_b),
            (unconfirmed_new, b_map, summary_b, summary_a),
        )
        for k in sorted(keys)
    ]
    new_clusters = [_cluster_to_dict(b_map[k]) for k in new_keys - uncertain_new - unconfirmed_new]
    resolved_clusters = [
        _cluster_to_dict(a_map[k])
        for k in resolved_keys - uncertain_resolved - unconfirmed_resolved
    ]
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
    caveats = []
    if not_comparable:
        plural = "s" if len(not_comparable) != 1 else ""
        caveats.append(f"{len(not_comparable)} cluster{plural} could not be compared")
    if unconfirmed:
        plural = "s" if len(unconfirmed) != 1 else ""
        caveats.append(
            f"{len(unconfirmed)} cluster{plural} missing only from an incomplete session"
            if not uncounted
            else f"{len(unconfirmed)} cluster{plural} missing only from a session that is "
            f"incomplete or holds evidence it did not count"
        )
    caveats.extend(f"{sid} {why}" for sid, why in uncounted.items())
    if not_compared:
        plural = "s" if len(not_compared) != 1 else ""
        caveats.append(f"{len(not_compared)} shared cluster{plural} not comparable: {because}")
    elif incomplete_ids:
        caveats.append(because)
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
        "unconfirmed": unconfirmed,
        "not_compared": not_compared,
        "incomplete": [s.session_id for s in (summary_a, summary_b) if s.incomplete],
        "verdict": verdict,
    }


def _uncounted(summary: SessionSummary) -> str:
    """What a summary holds but did not count, in words (empty when nothing)."""
    held = []
    if summary.ambiguous_tail:
        held.append("shows a record it started on but did not count (it may predate it)")
    if summary.unattributed_anrs:
        plural = "s" if summary.unattributed_anrs != 1 else ""
        held.append(
            f"dropped {summary.unattributed_anrs} ANR{plural} --package could not attribute"
        )
    return " and ".join(held)


def _signature(cluster: Cluster) -> str:
    """The cluster's fingerprint with its site left out: what it is, not whose.

    An ANR's is its kind alone: an ANR of a window and an app's ANR read
    differently (a title, a package) and may be the same stall.
    """
    sample = cluster.sample_event
    if sample.kind == "anr":
        return compute_fingerprint(None, "", sample.kind)
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
        "incomplete_reason": summary.incomplete_reason,
        "verification": summary.verification,
        "unverified_anr_lines": summary.unverified_anr_lines,
        "unnamed_pids": {"count": len(summary.unnamed_pids), "pids": summary.unnamed_pids},
        "cut_records": summary.cut_records,
        "stream_failure": summary.stream_failure,
        "stream_restarts": summary.stream_restarts,
        "ambiguous_tail": summary.ambiguous_tail,
        "unattributed_anrs": summary.unattributed_anrs,
        "possible_duplicates": summary.possible_duplicates,
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
        incomplete_reason=payload.get("incomplete_reason"),
        verification=payload.get("verification"),
        unverified_anr_lines=payload.get("unverified_anr_lines", 0),
        unnamed_pids=list((payload.get("unnamed_pids") or {}).get("pids", [])),
        cut_records=payload.get("cut_records", 0),
        stream_failure=payload.get("stream_failure"),
        stream_restarts=payload.get("stream_restarts", 0),
        ambiguous_tail=list(payload.get("ambiguous_tail") or []),
        unattributed_anrs=payload.get("unattributed_anrs", 0),
        possible_duplicates=payload.get("possible_duplicates", 0),
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
        window=payload.get("window"),
        process_name=payload.get("process_name"),
        possible_duplicate=bool(payload.get("possible_duplicate", False)),
        attribution=payload.get("attribution"),
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
            possible_duplicates=sum(1 for event in events if event.possible_duplicate),
        )
