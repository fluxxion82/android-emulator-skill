#!/usr/bin/env python3
"""ANR/jank filter pipeline — pure functions, no I/O.

Stages: parse → normalise → threshold → bucket → cluster → aggregate → rank → format.

Each function is independently testable; the worker and the ``--stop`` path both
compose them. The clustering / session / formatting machinery is ported nearly
verbatim from the iOS HangBuster pipeline (``hang_pipeline.py``); only the
*parse* layer (``parse_logcat_anr``) and the severity bands are Android-native.

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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum

# === CONSTANTS ===

FINGERPRINT_VERSION = 1
"""Bump when normalise_message, compute_fingerprint, or severity boundaries change.
``--diff`` skips structural comparison across mismatched versions.

v1 (2026-06): initial Android ANR/jank fingerprint. compute_fingerprint() hashes
its input with sha256[:16]; symbol (component/tag) wins when present, otherwise the
normalised message prefix is the hash input."""

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
# "ANR in com.example.app (com.example.app/.MainActivity)"
_ANR_IN_RE = re.compile(r"ANR in ([\w.]+)(?:\s+\(([^)]+)\))?", re.IGNORECASE)


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


# === STAGE 1: PARSE ===


def parse_logcat_anr(line: str) -> dict | None:
    """Parse one ``adb logcat -v threadtime`` line into a raw ANR/jank event dict.

    Recognises three Android signals:

    1. **Choreographer jank** — ``Skipped N frames!`` from the ``Choreographer``
       tag. Duration is estimated as ``N * 16.7ms`` (one dropped frame ≈ one
       60Hz refresh of blocked main thread).
    2. **ActivityManager ANR** — ``ANR in <pkg> (<component>)`` plus the
       follow-on ``Reason:`` / ``Input dispatching timed out`` lines. These are
       hard ANRs (5s+ unresponsive), bucketed FROZEN.
    3. **StrictMode / slow operations** (optional, minor) — ``Slow operation``
       or StrictMode dialog lines, treated as low-severity jank hints.

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

    # ANR follow-on lines carry the reason/timeout but not the "ANR in" prefix.
    if "input dispatching timed out" in lower or ("anr" in lower and "reason:" in lower):
        return {
            "kind": "anr",
            "frames": None,
            "duration_ms": 5000.0,
            "component": None,
            "anr_package": None,
        }

    # 3. StrictMode / slow operation hints (minor jank).
    if "strictmode" in tag.lower() or "slow operation" in lower:
        return {
            "kind": "strictmode",
            "frames": None,
            "duration_ms": float(WARN_FRAMES * FRAME_MS),
            "component": None,
            "anr_package": None,
        }

    return None


def is_anr_message(tag: str, message: str) -> bool:
    """Return True if a (tag, message) pair describes an ANR/jank event."""
    return _classify_anr(tag, message) is not None


# === STAGE 2: NORMALISE ===


def normalise_message(message: str, max_len: int = 40) -> str:
    """Strip boilerplate, redact volatile tokens, truncate to ``max_len``."""
    text = message
    for prefix in _BOILERPLATE_PREFIXES:
        idx = text.find(prefix)
        if idx != -1:
            text = (text[:idx]).strip() or text
            break
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
    # Prefer the explicit component (high signal) over a regex-scraped symbol.
    symbol = raw_event.get("component") or extract_symbol(message)
    prefix = normalise_message(message)
    fingerprint = compute_fingerprint(symbol, prefix, kind)
    return NormalisedEvent(
        delta_ms=delta_ms,
        process=raw_event.get("process", "unknown"),
        pid=int(raw_event.get("pid", 0)),
        duration_ms=float(duration),
        severity=bucket_severity(frames, kind, float(duration)),
        symbol=symbol,
        message_prefix=prefix,
        fingerprint=fingerprint,
        raw_message=message,
        frames=frames,
        kind=kind,
    )


def compute_fingerprint(symbol: str | None, message_prefix: str, kind: str = "jank") -> str:
    """Stable identity hash for clustering and diff.

    Hashed (sha256[:16]) so distinct messages with overlapping normalised
    prefixes don't collide into the same cluster. Symbol when present (high
    signal); otherwise normalised message prefix is the hash input. The event
    ``kind`` is mixed in so a jank and an ANR for the same component stay
    distinct clusters.

    The human-readable label lives in ``Cluster.symbol_or_prefix`` — the
    fingerprint is purely an identity key.
    """
    inner = f"sym:{symbol}" if symbol else f"msg:{message_prefix}"
    key = f"{kind}|{inner}"
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
            # inf delta (0 → N) renders as "new"; finite deltas keep the % suffix.
            delta = entry["delta_pct"]
            delta_str = "new" if delta == float("inf") else f"{delta:+.0f}%"
            lines.append(
                f"  ~ {entry['symbol_or_prefix']}: "
                f"{entry['max_duration_ms_a']:.0f} → {entry['max_duration_ms_b']:.0f}ms "
                f"({delta_str})"
            )
    if stable:
        lines.append(f"Stable: {stable} cluster(s) unchanged")
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
    """Compare two SessionSummary instances. Returns a dict structured for format_diff."""
    if summary_a.fingerprint_version != summary_b.fingerprint_version:
        return {
            "session_a": summary_a.session_id,
            "session_b": summary_b.session_id,
            "version_mismatch": True,
            "fingerprint_version_a": summary_a.fingerprint_version,
            "fingerprint_version_b": summary_b.fingerprint_version,
            "verdict": "skipped (version mismatch)",
        }
    a_map = {c.fingerprint: c for c in summary_a.clusters}
    b_map = {c.fingerprint: c for c in summary_b.clusters}
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
        if ca.max_duration_ms == 0:
            # 0 → N: a previously-silent cluster now janks; treat as max worsening.
            delta_pct: float = float("inf")
        elif cb.max_duration_ms == 0:
            # N → 0: cluster present in A but flat in B; fully improved.
            delta_pct = -100.0
        else:
            delta_pct = (cb.max_duration_ms - ca.max_duration_ms) / ca.max_duration_ms * 100
        if delta_pct == float("inf") or abs(delta_pct) >= drift_threshold_pct:
            drift.append(
                {
                    "fingerprint": key,
                    "symbol_or_prefix": cb.symbol_or_prefix,
                    "max_duration_ms_a": ca.max_duration_ms,
                    "max_duration_ms_b": cb.max_duration_ms,
                    "delta_pct": delta_pct,
                }
            )
        else:
            stable += 1
    new_clusters = [_cluster_to_dict(b_map[k]) for k in new_keys]
    resolved_clusters = [_cluster_to_dict(a_map[k]) for k in resolved_keys]
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
        worsened = sum(1 for d in drift if d["delta_pct"] > 0)
        verdict = f"drift: {worsened} worsened, {len(drift) - worsened} improved"
    else:
        verdict = "no change"
    return {
        "session_a": summary_a.session_id,
        "session_b": summary_b.session_id,
        "version_mismatch": False,
        "new_clusters": new_clusters,
        "resolved_clusters": resolved_clusters,
        "drift": drift,
        "stable_count": stable,
        "verdict": verdict,
    }


def _cluster_to_dict(cluster: Cluster) -> dict:
    """Lightweight dict view for diff output (skips full sample_event for token economy)."""
    return {
        "fingerprint": cluster.fingerprint,
        "symbol_or_prefix": cluster.symbol_or_prefix,
        "severity": cluster.severity.value,
        "count": cluster.count,
        "max_duration_ms": cluster.max_duration_ms,
        "first_delta_ms": cluster.first_delta_ms,
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
        )
