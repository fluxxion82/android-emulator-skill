"""Device-free tests for the ANR/jank filter pipeline (pure functions).

Covers the Android-native parse layer plus the ported clustering / ranking /
formatting / token-budget / diff machinery. No subprocess or device needed.

**Every logcat line that reaches the parser here is a RECORDED one.** These
tests used to build them from f-strings, which proved only that the parser
matched the f-string. Recording the real thing found three defects that could
not have surfaced any other way:

* ActivityManager's hard-ANR line is ``ANR in com.example.app`` -- the package
  ALONE. The parenthesised component every hand-typed sample carried is not
  something a broadcast ANR prints.
* WindowManager reports the SAME stall a second time as ``ANR in Window{...}``,
  so the token after "ANR in" is the literal word ``Window`` and the fault was
  being attributed to a package of that name.
* StrictMode prints a header and then its whole stack trace under the same tag,
  so a tag-only match turned one violation into one event per stack frame; and
  the header states its own duration, which the pipeline replaced with a
  constant.

Frame COUNTS still have to be varied, because nothing drops exactly 29 frames
to order. They are varied by substitution into the recorded line
(``_jank_line``), so everything except the number under test stays measured.
"""

from __future__ import annotations

import re

import pytest

from common import anr_pipeline as p

# --- helpers ---------------------------------------------------------------


def _first(text: str, needle: str) -> str:
    """First recorded line containing ``needle``, or fail loudly."""
    for line in text.splitlines():
        if needle in line:
            return line
    pytest.fail(f"no recorded line contains {needle!r}; the fixture has changed shape")
    raise AssertionError  # unreachable; pytest.fail raises


def _jank_line(recorded, frames: int, pid: int | None = None) -> str:
    """The recorded Choreographer line with its frame count (and pid) changed.

    Severity boundaries are a property of the pipeline, not of any device, so
    they cannot be recorded. Deriving them from the real line keeps everything
    else -- the two spaces after "frames!", the tag, the column widths --
    measured rather than imagined.
    """
    line = _first(recorded.text("logcat_choreographer_jank"), "Skipped")
    line = re.sub(r"Skipped \d+ frames", f"Skipped {frames} frames", line)
    if pid is not None:
        line = re.sub(r"^(\S+ \S+\s+)\d+(\s+)\d+", rf"\g<1>{pid}\g<2>{pid}", line)
    return line


def _anr_line(recorded) -> str:
    """ActivityManager's hard-ANR line: the bare package, no component."""
    return _first(recorded.text("logcat_anr_broadcast"), "ANR in ")


def _window_anr_line(recorded) -> str:
    """WindowManager's input-dispatch report of the same stall."""
    return _first(recorded.text("logcat_anr_input_dispatch"), "ANR in Window")


def _strictmode_line(recorded) -> str:
    """The StrictMode violation HEADER, not one of its stack frames."""
    return _first(recorded.text("logcat_strictmode_violation"), "policy violation")


def _norm(line: str, current_ms: int = 1000) -> p.NormalisedEvent:
    raw = p.parse_logcat_anr(line)
    assert raw is not None
    event = p.build_normalised_event(raw, session_start_ms=0, current_ms=current_ms)
    assert event is not None
    return event


# --- parse: Choreographer "Skipped N frames" -------------------------------


def test_parse_choreographer_reads_the_recorded_line(recorded):
    """The line as the device wrote it, frame count and all."""
    line = _first(recorded.text("logcat_choreographer_jank"), "Skipped")
    raw = p.parse_logcat_anr(line)

    assert raw is not None
    assert raw["kind"] == "jank"
    assert raw["process"] == "Choreographer"
    assert raw["frames"] == int(re.search(r"Skipped (\d+) frames", line).group(1))
    assert raw["duration_ms"] == round(raw["frames"] * p.FRAME_MS, 1)


def test_frame_count_tracks_the_stall_it_measures(recorded):
    """A rare chance to check the 16.7ms-per-frame heuristic against a clock.

    The recording was provoked by a main thread blocked for a KNOWN 40s (the
    fixture app's AnrReceiver), so the frames-to-milliseconds conversion has
    something to be right or wrong about, instead of only being self-consistent.
    """
    raw = p.parse_logcat_anr(_first(recorded.text("logcat_choreographer_jank"), "Skipped"))
    assert raw is not None
    assert abs(raw["duration_ms"] - 40_000) < 1_000


def test_parse_choreographer_below_warn_is_minor(recorded):
    assert _norm(_jank_line(recorded, 10)).severity is p.Severity.MINOR


def test_parse_choreographer_warn_boundary(recorded):
    # 30 frames is the WARN floor.
    assert _norm(_jank_line(recorded, 29)).severity is p.Severity.MINOR
    assert _norm(_jank_line(recorded, 30)).severity is p.Severity.WARN
    assert _norm(_jank_line(recorded, 59)).severity is p.Severity.WARN


def test_parse_choreographer_critical_boundary(recorded):
    assert _norm(_jank_line(recorded, 60)).severity is p.Severity.CRITICAL
    assert _norm(_jank_line(recorded, 99)).severity is p.Severity.CRITICAL


def test_parse_choreographer_frozen_boundary(recorded):
    # 100+ skipped frames is frozen-grade jank.
    assert _norm(_jank_line(recorded, 100)).severity is p.Severity.FROZEN
    assert _norm(_jank_line(recorded, 250)).severity is p.Severity.FROZEN


# --- parse: ActivityManager "ANR in" blocks --------------------------------


def test_parse_anr_in_block_has_no_component(recorded):
    """The shape a hand-typed sample got wrong, in both directions.

    ActivityManager writes the package alone. The old tests fed
    ``ANR in com.example.app (com.example.app/.MainActivity)`` and asserted the
    component came back -- an assertion about output Android does not produce
    for a broadcast ANR.
    """
    line = _anr_line(recorded)
    assert "(" not in line.split("ANR in ", 1)[1]

    raw = p.parse_logcat_anr(line)
    assert raw is not None
    assert raw["kind"] == "anr"
    assert raw["anr_package"] == "com.example.composefixture"
    assert raw.get("component") is None
    # Hard ANRs always bucket FROZEN.
    assert _norm(line, current_ms=5000).severity is p.Severity.FROZEN


def test_the_anr_pid_column_is_not_the_stalled_apps_pid(recorded):
    """``PID: <n>`` is a separate line, and the columns belong to system_server.

    Worth pinning because it is the sort of thing an invented line gets right
    by accident: the parser takes its pid from the logcat column, which for an
    ActivityManager ANR is the reporter, not the app that stalled.
    """
    text = recorded.text("logcat_anr_broadcast")
    reported = int(_first(text, "PID: ").split("PID: ")[1].strip())
    parsed = p.parse_logcat_anr(_anr_line(recorded))

    assert parsed is not None
    assert parsed["pid"] != reported


def test_parse_window_anr_names_the_app_and_not_the_word_window(recorded):
    r"""WindowManager's form, where the package lives inside the braces.

    ``ANR in Window{67a8d77 u0 com.pkg/com.pkg.MainActivity}`` also matches the
    generic ``ANR in ([\w.]+)`` pattern, which read the package as "Window".
    """
    raw = p.parse_logcat_anr(_window_anr_line(recorded))

    assert raw is not None
    assert raw["kind"] == "anr"
    assert raw["anr_package"] == "com.example.composefixture"
    assert raw["component"] == (
        "com.example.composefixture/com.example.composefixture.DefaultActivity"
    )


def test_parse_window_anr_uses_its_component_as_the_symbol(recorded):
    event = _norm(_window_anr_line(recorded), current_ms=5000)
    assert event.symbol == "com.example.composefixture/com.example.composefixture.DefaultActivity"


def test_one_stall_is_reported_by_two_subsystems(recorded):
    """Both recordings are of the same 40s block, under different tags.

    Anything counting ANRs has to know that, or one hang is two incidents.
    """
    am = p.parse_logcat_anr(_anr_line(recorded))
    wm = p.parse_logcat_anr(_window_anr_line(recorded))

    assert am is not None and wm is not None
    assert am["process"] == "ActivityManager"
    assert wm["process"] == "WindowManager"
    assert am["anr_package"] == wm["anr_package"]


def test_parse_strictmode_reads_the_violations_own_duration(recorded):
    """StrictMode states its cost; the pipeline used to substitute a constant."""
    line = _strictmode_line(recorded)
    stated = float(re.search(r"~duration=(\d+)\s*ms", line).group(1))

    raw = p.parse_logcat_anr(line)
    assert raw is not None
    assert raw["kind"] == "strictmode"
    assert raw["duration_ms"] == stated
    assert _norm(line).severity is p.Severity.MINOR


def test_a_strictmode_stack_frame_is_not_a_separate_violation(recorded):
    """Every frame of the trace carries the same tag.

    Ten violations, a few hundred tagged lines. A tag-only match reported all
    of them as separate stalls.
    """
    text = recorded.text("logcat_strictmode_violation")
    headers = text.count("StrictMode policy violation")
    events = [p.parse_logcat_anr(line) for line in text.splitlines()]

    assert headers >= 2, "fixture no longer carries repeated violations"
    assert len(text.splitlines()) > headers * 5, "fixture no longer carries the stack traces"
    assert len([e for e in events if e]) == headers


def test_parse_ignores_unrelated_lines(recorded):
    """Ordinary logcat traffic must not be read as jank.

    Drawn from the recorded 200-line dump rather than from one typed line, so
    "unrelated" means what a device actually logs.
    """
    lines = recorded.lines("logcat_threadtime")
    assert lines, "logcat_threadtime is empty"

    unrelated = [
        line
        for line in lines
        if not any(token in line for token in ("Skipped", "ANR in", "StrictMode"))
    ]
    assert len(unrelated) > 100, "the dump is not the ordinary traffic this test needs"
    assert [line for line in unrelated if p.parse_logcat_anr(line) is not None] == []

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


def test_fingerprint_stable_across_volatile_tokens(recorded):
    a = _norm(_jank_line(recorded, 50, pid=4242))
    b = _norm(_jank_line(recorded, 50, pid=9999))
    # Same jank pattern -> same fingerprint regardless of pid/timestamp.
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_kinds(recorded):
    jank = _norm(_jank_line(recorded, 50))
    anr = _norm(_anr_line(recorded), current_ms=5000)
    assert jank.fingerprint != anr.fingerprint


def test_fingerprint_is_hashed_form():
    fp = p.compute_fingerprint("Sym.method", "prefix", "jank")
    assert fp.startswith("fp:")
    assert len(fp) == len("fp:") + 16


# --- cluster + rank --------------------------------------------------------


def test_cluster_groups_by_fingerprint(recorded):
    events = [_norm(_jank_line(recorded, 50)) for _ in range(3)]
    clusters = p.cluster_events(events)
    assert len(clusters) == 1
    assert clusters[0].count == 3


def test_rank_orders_frozen_before_minor(recorded):
    events = [
        _norm(_jank_line(recorded, 10)),  # minor
        _norm(_jank_line(recorded, 150)),  # frozen
        _norm(_jank_line(recorded, 40)),  # warn
    ]
    ranked = p.rank_clusters(p.cluster_events(events))
    assert ranked[0].severity is p.Severity.FROZEN
    assert ranked[-1].severity is p.Severity.MINOR


def test_rank_top_n_caps_results(recorded):
    events = [_norm(_jank_line(recorded, n)) for n in (35, 65, 120)]
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


def test_format_l0_single_line(recorded):
    summary = _summary([_norm(_jank_line(recorded, 120)), _norm(_jank_line(recorded, 40))])
    out = p.format_l0(summary)
    assert "\n" not in out
    assert "anr-test-0001" in out


def test_format_l0_empty():
    summary = _summary([])
    assert "no ANR/jank" in p.format_l0(summary)


def test_format_l1_includes_drill_hint(recorded):
    summary = _summary([_norm(_jank_line(recorded, 120))])
    out = p.format_l1(summary)
    assert "Drill: anr_watcher.py --get-details anr-test-0001" in out


def test_format_l2_includes_severity_histogram(recorded):
    summary = _summary([_norm(_jank_line(recorded, 120)), _norm(_jank_line(recorded, 40))])
    out = p.format_l2(summary)
    assert "Severity:" in out
    assert "Lines: matched" in out


def test_compress_to_budget_picks_levels(recorded):
    summary = _summary([_norm(_jank_line(recorded, n)) for n in (35, 65, 120)])
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


def test_format_cluster_detail_lists_events(recorded):
    events = [_norm(_jank_line(recorded, 120)), _norm(_jank_line(recorded, 125))]
    clusters = p.rank_clusters(p.cluster_events(events))
    detail = p.format_cluster_detail(clusters[0], events)
    assert "Cluster:" in detail
    assert "frames=" in detail


# --- diff_sessions ---------------------------------------------------------


def test_diff_detects_new_critical_regression(recorded):
    base = _summary([_norm(_jank_line(recorded, 35))])
    regressed = _summary(
        [
            _norm(_jank_line(recorded, 35)),
            _norm(_anr_line(recorded), 5000),
        ]
    )
    result = p.diff_sessions(base, regressed)
    assert result["version_mismatch"] is False
    assert "regression" in result["verdict"]
    assert len(result["new_clusters"]) == 1


def test_diff_detects_improvement(recorded):
    base = _summary(
        [
            _norm(_jank_line(recorded, 35)),
            _norm(_anr_line(recorded), 5000),
        ]
    )
    fixed = _summary([_norm(_jank_line(recorded, 35))])
    result = p.diff_sessions(base, fixed)
    assert "improvement" in result["verdict"]
    assert len(result["resolved_clusters"]) == 1


def test_diff_version_mismatch_short_circuits(recorded):
    a = _summary([_norm(_jank_line(recorded, 35))])
    b = _summary([_norm(_jank_line(recorded, 35))])
    b.fingerprint_version = a.fingerprint_version + 1
    result = p.diff_sessions(a, b)
    assert result["version_mismatch"] is True
    rendered = p.format_diff(result)
    assert "mismatch" in rendered


def test_summary_json_roundtrip(recorded):
    summary = _summary([_norm(_jank_line(recorded, 120)), _norm(_jank_line(recorded, 40))])
    payload = p.summary_to_json(summary)
    restored = p.summary_from_json(payload)
    assert restored.session_id == summary.session_id
    assert len(restored.clusters) == len(summary.clusters)
    assert restored.clusters[0].severity == summary.clusters[0].severity
