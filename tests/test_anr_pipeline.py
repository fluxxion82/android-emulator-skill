"""Device-free tests for the ANR/jank filter pipeline (pure functions).

Covers the Android-native parse layer plus the ported clustering / ranking /
formatting / token-budget / diff machinery. No subprocess or device needed —
the pipeline is pure functions over synthetic logcat fixtures.
"""

from __future__ import annotations

from common import anr_pipeline as p

# --- helpers ---------------------------------------------------------------


def _choreographer(frames: int, ts: str = "06-17 14:30:52.123", pid: int = 1234) -> str:
    return (
        f"{ts}  {pid}  {pid} I Choreographer: Skipped {frames} frames!  "
        "The application may be doing too much work on its main thread."
    )


def _norm(line: str, current_ms: int = 1000) -> p.NormalisedEvent:
    raw = p.parse_logcat_anr(line)
    assert raw is not None
    event = p.build_normalised_event(raw, session_start_ms=0, current_ms=current_ms)
    assert event is not None
    return event


# --- parse: Choreographer "Skipped N frames" -------------------------------


def test_parse_choreographer_extracts_frames_and_duration():
    raw = p.parse_logcat_anr(_choreographer(47))
    assert raw is not None
    assert raw["kind"] == "jank"
    assert raw["frames"] == 47
    # 47 * 16.7 = 784.9ms
    assert abs(raw["duration_ms"] - 784.9) < 0.05
    assert raw["pid"] == 1234
    assert raw["process"] == "Choreographer"


def test_parse_choreographer_below_warn_is_minor():
    assert _norm(_choreographer(10)).severity is p.Severity.MINOR


def test_parse_choreographer_warn_boundary():
    # 30 frames is the WARN floor.
    assert _norm(_choreographer(29)).severity is p.Severity.MINOR
    assert _norm(_choreographer(30)).severity is p.Severity.WARN
    assert _norm(_choreographer(59)).severity is p.Severity.WARN


def test_parse_choreographer_critical_boundary():
    assert _norm(_choreographer(60)).severity is p.Severity.CRITICAL
    assert _norm(_choreographer(99)).severity is p.Severity.CRITICAL


def test_parse_choreographer_frozen_boundary():
    # 100+ skipped frames is frozen-grade jank.
    assert _norm(_choreographer(100)).severity is p.Severity.FROZEN
    assert _norm(_choreographer(250)).severity is p.Severity.FROZEN


# --- parse: ActivityManager "ANR in" blocks --------------------------------


def test_parse_anr_in_block():
    line = (
        "06-17 14:31:00.000  2000  2000 E ActivityManager: "
        "ANR in com.example.app (com.example.app/.MainActivity)"
    )
    raw = p.parse_logcat_anr(line)
    assert raw is not None
    assert raw["kind"] == "anr"
    assert raw["anr_package"] == "com.example.app"
    assert raw["component"] == "com.example.app/.MainActivity"
    # Hard ANRs always bucket FROZEN.
    assert _norm(line, current_ms=5000).severity is p.Severity.FROZEN


def test_parse_anr_uses_component_as_symbol():
    line = (
        "06-17 14:31:00.000  2000  2000 E ActivityManager: "
        "ANR in com.example.app (com.example.app/.MainActivity)"
    )
    event = _norm(line, current_ms=5000)
    assert event.symbol == "com.example.app/.MainActivity"


def test_parse_anr_input_dispatch_followon():
    line = "06-17 14:31:00.500  2000  2000 E ActivityManager: Reason: Input dispatching timed out"
    raw = p.parse_logcat_anr(line)
    assert raw is not None
    assert raw["kind"] == "anr"


def test_parse_strictmode_is_minor():
    line = "06-17 14:31:05.000  3000  3000 D StrictMode: Slow operation detected on main thread"
    raw = p.parse_logcat_anr(line)
    assert raw is not None
    assert raw["kind"] == "strictmode"
    assert _norm(line).severity is p.Severity.MINOR


def test_parse_ignores_unrelated_lines():
    assert p.parse_logcat_anr("06-17 14:31:05.000 3000 3000 I ActivityManager: Start proc") is None
    assert p.parse_logcat_anr("garbage not a logcat line") is None
    assert p.parse_logcat_anr("") is None


# --- threshold -------------------------------------------------------------


def test_above_threshold_drops_low_frame_jank():
    assert p.above_threshold(10, min_frames=30) is False
    assert p.above_threshold(30, min_frames=30) is True


def test_above_threshold_passes_non_frame_events():
    # Hard ANRs / StrictMode carry frames=None and must never be frame-dropped.
    assert p.above_threshold(None, min_frames=30) is True


# --- fingerprint stability & normalisation ---------------------------------


def test_normalise_redacts_volatile_tokens():
    a = p.normalise_message("Skipped 50 frames at 0xdeadbeef pid=4242 count 99999")
    b = p.normalise_message("Skipped 50 frames at 0xcafef00d pid=1111 count 88888")
    assert a == b
    assert "<addr>" in a
    assert "<pid>" in a


def test_fingerprint_stable_across_volatile_tokens():
    a = _norm(_choreographer(50, pid=4242))
    b = _norm(_choreographer(50, pid=9999))
    # Same jank pattern -> same fingerprint regardless of pid/timestamp.
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_kinds():
    jank = _norm(_choreographer(50))
    anr = _norm(
        "06-17 14:31:00.000  2 2 E ActivityManager: ANR in com.x (com.x/.A)", current_ms=5000
    )
    assert jank.fingerprint != anr.fingerprint


def test_fingerprint_is_hashed_form():
    fp = p.compute_fingerprint("Sym.method", "prefix", "jank")
    assert fp.startswith("fp:")
    assert len(fp) == len("fp:") + 16


# --- cluster + rank --------------------------------------------------------


def test_cluster_groups_by_fingerprint():
    events = [_norm(_choreographer(50)) for _ in range(3)]
    clusters = p.cluster_events(events)
    assert len(clusters) == 1
    assert clusters[0].count == 3


def test_rank_orders_frozen_before_minor():
    events = [
        _norm(_choreographer(10)),  # minor
        _norm(_choreographer(150)),  # frozen
        _norm(_choreographer(40)),  # warn
    ]
    ranked = p.rank_clusters(p.cluster_events(events))
    assert ranked[0].severity is p.Severity.FROZEN
    assert ranked[-1].severity is p.Severity.MINOR


def test_rank_top_n_caps_results():
    events = [_norm(_choreographer(n)) for n in (35, 65, 120)]
    ranked = p.rank_clusters(p.cluster_events(events), top_n=2)
    assert len(ranked) == 2


# --- formatting & token budget ---------------------------------------------


def _summary(events: list[p.NormalisedEvent], top_n: int | None = None) -> p.SessionSummary:
    builder = p.SummaryBuilder(
        session_id="anr-test-0001",
        started_at="2026-06-17T14:30:00",
        duration_ms=10_000,
        matched_lines=len(events),
        total_lines=len(events) + 5,
    )
    return builder.build(events, top_n=top_n)


def test_format_l0_single_line():
    summary = _summary([_norm(_choreographer(120)), _norm(_choreographer(40))])
    out = p.format_l0(summary)
    assert "\n" not in out
    assert "anr-test-0001" in out


def test_format_l0_empty():
    summary = _summary([])
    assert "no ANR/jank" in p.format_l0(summary)


def test_format_l1_includes_drill_hint():
    summary = _summary([_norm(_choreographer(120))])
    out = p.format_l1(summary)
    assert "Drill: anr_watcher.py --get-details anr-test-0001" in out


def test_format_l2_includes_severity_histogram():
    summary = _summary([_norm(_choreographer(120)), _norm(_choreographer(40))])
    out = p.format_l2(summary)
    assert "Severity:" in out
    assert "Lines: matched" in out


def test_compress_to_budget_picks_levels():
    summary = _summary([_norm(_choreographer(n)) for n in (35, 65, 120)])
    # No budget -> L1.
    l1 = p.compress_to_budget(summary, max_tokens=None)
    assert "Drill:" in l1
    # Tiny budget -> L0 one-liner.
    l0 = p.compress_to_budget(summary, max_tokens=10)
    assert "\n" not in l0
    # Generous budget -> L2 (has Severity histogram).
    l2 = p.compress_to_budget(summary, max_tokens=500)
    assert "Severity:" in l2


def test_estimate_tokens_char_over_four():
    assert p.estimate_tokens("a" * 40) == 10


# --- cluster detail --------------------------------------------------------


def test_format_cluster_detail_lists_events():
    events = [_norm(_choreographer(120)), _norm(_choreographer(125))]
    clusters = p.rank_clusters(p.cluster_events(events))
    detail = p.format_cluster_detail(clusters[0], events)
    assert "Cluster:" in detail
    assert "frames=" in detail


# --- diff_sessions ---------------------------------------------------------


def test_diff_detects_new_critical_regression():
    base = _summary([_norm(_choreographer(35))])
    regressed = _summary(
        [
            _norm(_choreographer(35)),
            _norm("06-17 14:31:00.000 2 2 E ActivityManager: ANR in com.x (com.x/.A)", 5000),
        ]
    )
    result = p.diff_sessions(base, regressed)
    assert result["version_mismatch"] is False
    assert "regression" in result["verdict"]
    assert len(result["new_clusters"]) == 1


def test_diff_detects_improvement():
    base = _summary(
        [
            _norm(_choreographer(35)),
            _norm("06-17 14:31:00.000 2 2 E ActivityManager: ANR in com.x (com.x/.A)", 5000),
        ]
    )
    fixed = _summary([_norm(_choreographer(35))])
    result = p.diff_sessions(base, fixed)
    assert "improvement" in result["verdict"]
    assert len(result["resolved_clusters"]) == 1


def test_diff_version_mismatch_short_circuits():
    a = _summary([_norm(_choreographer(35))])
    b = _summary([_norm(_choreographer(35))])
    b.fingerprint_version = a.fingerprint_version + 1
    result = p.diff_sessions(a, b)
    assert result["version_mismatch"] is True
    rendered = p.format_diff(result)
    assert "mismatch" in rendered


def test_summary_json_roundtrip():
    summary = _summary([_norm(_choreographer(120)), _norm(_choreographer(40))])
    payload = p.summary_to_json(summary)
    restored = p.summary_from_json(payload)
    assert restored.session_id == summary.session_id
    assert len(restored.clusters) == len(summary.clusters)
    assert restored.clusters[0].severity == summary.clusters[0].severity
