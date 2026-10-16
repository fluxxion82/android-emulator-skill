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


# --- assemble: one stall is one event (D5) ----------------------------------
#
# The two *_stream recordings are everything ActivityManager and WindowManager
# logged from the provocation on, so "how many ANRs is this?" is asked of what
# a live reader actually sees -- not of a line picked out of it.


def _stated(lines: list[str], field: str) -> str:
    """The value ActivityManager's record states for ``field`` (``PID``, ``Reason``)."""
    return _first("\n".join(lines), f"ActivityManager: {field}: ").split(f"{field}: ", 1)[1]


def test_a_broadcast_anr_record_is_one_event_with_its_pid_and_reason(recorded):
    lines = recorded.lines("logcat_anr_broadcast")

    events = p.parse_logcat_stream(lines)

    assert len(events) == 1, [e["message"] for e in events]
    (event,) = events
    assert event["anr_package"] == "com.example.composefixture"
    assert event["app_pid"] == int(_stated(lines, "PID"))
    assert event["app_pid"] != event["pid"], "the header pid is system_server's"
    assert event["reason"] == _stated(lines, "Reason").strip()


def test_one_broadcast_anr_as_the_stream_carries_it_is_one_event(recorded):
    """ActivityManager's record, WindowManager's report, and "Skipping duplicate".

    Three places a line-at-a-time reader found an ANR, and one stall.
    """
    lines = recorded.lines("logcat_anr_broadcast_stream")
    assert any("ANR in Window{" in line for line in lines), "WindowManager's report is gone"
    assert any("Skipping duplicate ANR" in line for line in lines), "no duplicate verdict"

    events = p.parse_logcat_stream(lines)

    assert [e["kind"] for e in events] == ["anr"], [e["message"] for e in events]
    assert events[0]["process"] == "ActivityManager"
    assert "Broadcast of Intent" in events[0]["reason"]
    assert events[0]["app_pid"] == int(_stated(lines, "PID"))


def test_an_input_anr_is_one_event_and_comes_from_activitymanager(recorded):
    """WindowManager reports first here, and ActivityManager's record follows.

    Dropping WindowManager's line is only safe because ActivityManager always
    logs the incident itself; this is the recording that shows it does when
    the ANR is new, as logcat_anr_broadcast_stream shows the duplicate case.
    """
    lines = recorded.lines("logcat_anr_input_stream")
    window = next(i for i, line in enumerate(lines) if "WindowManager: ANR in Window{" in line)
    record = next(i for i, line in enumerate(lines) if "ActivityManager: ANR in " in line)
    assert window < record, "the recording no longer has WindowManager reporting first"

    events = p.parse_logcat_stream(lines)

    assert len(events) == 1, [e["message"] for e in events]
    (event,) = events
    assert event["process"] == "ActivityManager"
    assert event["component"] == "com.example.composefixture/.DefaultActivity"
    assert event["reason"].startswith("Input dispatching timed out")


def test_an_input_anr_cut_off_before_activitymanager_reports_still_counts(recorded):
    """WindowManager's report is a fallback, not noise (review of PR #26).

    The recorded input-only ANR, cut where a stop or a --since window can cut
    it: after WindowManager's line, before ActivityManager's record. Dropping
    WindowManager's line unconditionally made that ZERO ANRs -- a missed ANR,
    which is worse than a duplicate.
    """
    lines = recorded.lines("logcat_anr_input_stream")
    record = next(i for i, line in enumerate(lines) if "ActivityManager: ANR in " in line)
    cut = lines[:record]
    assert any("WindowManager: ANR in Window{" in line for line in cut)

    events = p.parse_logcat_stream(cut)

    assert [e["kind"] for e in events] == ["anr"], events
    assert events[0]["anr_package"] == "com.example.composefixture"
    assert events[0]["reason"].startswith("Input dispatching timed out")


def test_a_broadcast_anr_cut_off_after_windowmanagers_report_is_still_one(recorded):
    """The duplicate case, cut before ActivityManager's "Skipping duplicate" verdict.

    WindowManager reported 5.06 s after ActivityManager's record (recorded);
    the correlation window reaches back far enough to know it is that stall.
    """
    lines = recorded.lines("logcat_anr_broadcast_stream")
    window = next(i for i, line in enumerate(lines) if "WindowManager: ANR in Window{" in line)

    events = p.parse_logcat_stream(lines[: window + 1])

    assert len(events) == 1, [e["message"] for e in events]
    assert events[0]["process"] == "ActivityManager"


def test_an_anr_records_own_lines_are_not_anrs_on_their_own(recorded):
    """``PID:``, ``Reason:`` and "Skipping duplicate ANR" are never an ANR alone.

    The removed follow-on branch made each a second ANR (D5). The assembler
    attaches the first two and settles on the third, so in a whole stream the
    branch would hide -- but a stream that starts after a record's head (a
    recording cut, a reader joining mid-burst) hands it the tail alone.
    """
    text = recorded.text("logcat_anr_broadcast_stream")
    for needle in ("ActivityManager: PID: ", "ActivityManager: Reason: ", "Skipping duplicate ANR"):
        line = _first(text, needle)
        assert p.parse_logcat_anr(line) is None, line

    lines = text.splitlines()
    head = next(i for i, line in enumerate(lines) if "ActivityManager: ANR in " in line)
    assert p.parse_logcat_stream(lines[head + 1 : head + 4]) == []


def _later(line: str, seconds: float) -> str:
    """The recorded line with only its timestamp moved ``seconds`` later."""
    from datetime import datetime, timedelta

    stamp = line[:18]
    moved = datetime.strptime(f"2026-{stamp}", "%Y-%m-%d %H:%M:%S.%f") + timedelta(seconds=seconds)
    return moved.strftime("%m-%d %H:%M:%S.%f")[:-3] + line[18:]


def _input_stream_cut_before_the_record(recorded) -> list[str]:
    lines = recorded.lines("logcat_anr_input_stream")
    return lines[: next(i for i, line in enumerate(lines) if "ActivityManager: ANR in " in line)]


def test_a_report_activitymanager_calls_a_duplicate_is_not_counted_however_late(recorded):
    """The user taps a still-blocked app long after its ANR was recorded.

    WindowManager reports again, past the correlation window, and
    ActivityManager answers "Skipping duplicate ANR" naming the window. That
    verdict, not the clock, is what settles it. Derived from the recorded
    broadcast stream with only the timestamps of those two lines moved 20 s on.
    """
    lines = recorded.lines("logcat_anr_broadcast_stream")
    report = next(i for i, line in enumerate(lines) if "WindowManager: ANR in Window{" in line)
    verdict = next(i for i, line in enumerate(lines) if "Skipping duplicate ANR" in line)
    assert report < verdict
    stream = [*lines[:report], _later(lines[report], 20), _later(lines[verdict], 20)]

    events = p.parse_logcat_stream(stream)

    assert [e["process"] for e in events] == ["ActivityManager"], events


def test_a_held_report_is_released_by_log_time_not_only_at_the_end(recorded):
    """A WindowManager report nothing answers is an event once 10 s of log pass."""
    cut = _input_stream_cut_before_the_record(recorded)
    assembler = p.AnrRecordAssembler()
    assert [event for line in cut for event in assembler.feed(line)] == []

    released = assembler.feed(_later(cut[-1], p.ANR_CORRELATION_MS / 1000 + 1))

    assert [(e["process"], e["kind"]) for e in released] == [("WindowManager", "anr")]
    assert assembler.close() == []


def _report_and_verdict(recorded) -> tuple[str, str]:
    """WindowManager's report and ActivityManager's duplicate verdict, as recorded."""
    text = recorded.text("logcat_anr_broadcast_stream")
    return _first(text, "WindowManager: ANR in Window{"), _first(text, "Skipping duplicate ANR")


def test_a_duplicate_verdict_settles_only_the_package_it_names(recorded):
    """The verdict names an app EXACTLY (review of PR #26).

    Matched by substring, a verdict naming ``com.example.composefixture``
    settled a held report for a different app, ``example.composefixture``, and
    that ANR vanished. The recorded report with only its package changed.
    """
    report, verdict = _report_and_verdict(recorded)
    other_app = report.replace("com.example.composefixture", "example.composefixture")

    events = p.parse_logcat_stream([other_app, verdict])

    assert [(e["process"], e["anr_package"]) for e in events] == [
        ("WindowManager", "example.composefixture")
    ]


def test_a_duplicate_verdict_settles_the_report_it_names(recorded):
    report, verdict = _report_and_verdict(recorded)

    assert p.parse_logcat_stream([report, verdict]) == []


def test_a_duplicate_verdict_past_the_window_settles_nothing(recorded):
    """The 10 s bound holds on the verdict path too."""
    report, verdict = _report_and_verdict(recorded)
    late = _later(verdict, p.ANR_CORRELATION_MS / 1000 + 1)

    events = p.parse_logcat_stream([report, late])

    assert [e["process"] for e in events] == ["WindowManager"]


def test_a_duplicate_verdict_logged_long_before_the_report_settles_nothing(recorded):
    """The bound is checked on the verdict path itself, in both directions.

    A verdict LATER than the window never reaches it -- the report is released
    first -- so the test that can tell is one logged earlier: logcat interleaves
    buffers, and a line can arrive after one stamped later (seen in this
    session's own dumps, by milliseconds). Here by 11 s, derived from the
    recorded verdict with only its timestamp moved.
    """
    report, verdict = _report_and_verdict(recorded)
    early = _later(report, -(p.ANR_CORRELATION_MS / 1000 + 1))
    early_verdict = early[:18] + verdict[18:]

    events = p.parse_logcat_stream([report, early_verdict])

    assert [e["process"] for e in events] == ["WindowManager"]


def test_a_held_report_waits_out_a_short_quiet_and_not_a_long_one(recorded):
    """ActivityManager's record can follow WindowManager's by a quiet moment.

    Measured 0.35 s, with the stack dump between them. A quarter-second of
    quiet must not release the report as an ANR of its own; a quiet as long
    as the whole correlation window means no record is coming.
    """
    assembler = p.AnrRecordAssembler()
    for line in _input_stream_cut_before_the_record(recorded):
        assembler.feed(line)

    assert assembler.flush(quiet_seconds=0.25) == []
    released = assembler.flush(quiet_seconds=p.ANR_CORRELATION_MS / 1000)
    assert [e["process"] for e in released] == ["WindowManager"]


def test_an_open_anr_record_is_emitted_only_when_it_ends(recorded):
    """The assembler cannot know a record is complete until its header changes.

    That is why every live reader flushes when the stream goes quiet.
    """
    lines = recorded.lines("logcat_anr_broadcast")
    assembler = p.AnrRecordAssembler()

    assert [event for line in lines for event in assembler.feed(line)] == []
    flushed = assembler.flush()
    assert len(flushed) == 1
    assert assembler.flush() == [], "a flushed record is not emitted twice"


def test_a_jank_line_ends_an_open_anr_record(recorded):
    lines = recorded.lines("logcat_anr_broadcast")
    jank = _jank_line(recorded, 80)
    assembler = p.AnrRecordAssembler()
    for line in lines:
        assembler.feed(line)

    completed = assembler.feed(jank)

    assert [e["kind"] for e in completed] == ["anr", "jank"]
    assert assembler.flush() == []


def test_the_assembled_reason_reaches_the_normalised_event(recorded):
    (raw,) = p.parse_logcat_stream(recorded.lines("logcat_anr_broadcast"))
    event = p.build_normalised_event(raw, session_start_ms=0, current_ms=5000)

    assert event is not None
    assert event.reason == raw["reason"]
    assert event.app_pid == raw["app_pid"]
    restored = p.event_from_jsonl(p.event_to_jsonl(event))
    assert (restored.reason, restored.app_pid) == (event.reason, event.app_pid)


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


def test_jank_fingerprint_is_the_process_not_the_frame_count(recorded):
    """A5: one janking process is one site, whatever it dropped and whenever.

    This test used to assert the opposite of half of that -- that the pid was a
    volatile token, so two processes janking alike shared a fingerprint. A
    Choreographer line names no app; the pid column is the only site it has,
    so pid-blind jank clustering reported every app's jank as one site (the
    unmarked A5 control in test_diagnostics_specs). What stays volatile is the
    count and the timestamp.
    """
    a = _norm(_jank_line(recorded, 31, pid=4242), current_ms=1000)
    b = _norm(_jank_line(recorded, 120, pid=4242), current_ms=9000)
    other = _norm(_jank_line(recorded, 31, pid=9999), current_ms=1000)

    assert a.fingerprint == b.fingerprint
    assert a.frames != b.frames, "the measurement itself is kept on the event"
    assert a.fingerprint != other.fingerprint


def _restart_jank(recorded) -> list[dict]:
    """The three jank lines of logcat_app_restart: one app, three processes."""
    raws = [
        p.parse_logcat_anr(line)
        for line in recorded.lines("logcat_app_restart")
        if "Choreographer: Skipped" in line
    ]
    if len({raw["pid"] for raw in raws if raw}) != 3:
        pytest.fail("logcat_app_restart no longer holds one jank line per process")
    return raws


def _restart_names(recorded) -> dict[int, str]:
    """pid -> process name, from the recording's own ActivityManager lines."""
    return dict(
        named
        for named in (p.parse_start_proc(line) for line in recorded.lines("logcat_app_restart"))
        if named
    )


def test_start_proc_names_each_process_of_the_restarted_app(recorded):
    names = _restart_names(recorded)

    assert {names[raw["pid"]] for raw in _restart_jank(recorded)} == {"com.example.composefixture"}


def test_ps_age_gives_the_owners_name_and_age(recorded):
    """ps_lifetime: a header, then the row; ELAPSED as HH:MM:SS."""
    text = recorded.text("ps_lifetime")
    row = text.splitlines()[1].split()
    hours, minutes, seconds = (int(part) for part in row[-1].split(":"))

    owner = p.parse_process_age(text, int(row[0]))

    assert owner.name == "com.android.systemui"
    assert owner.age.total_seconds() == hours * 3600 + minutes * 60 + seconds
    assert p.parse_process_age(text, int(row[0]) + 1) is None


def test_ps_age_reads_the_short_form_of_a_young_process(recorded):
    """ps_lifetime_just_started: under an hour old, ELAPSED is MM:SS."""
    text = recorded.text("ps_lifetime_just_started")
    row = text.splitlines()[1].split()
    minutes, seconds = (int(part) for part in row[-1].split(":"))

    owner = p.parse_process_age(text, int(row[0]))

    assert owner.name == "com.example.composefixture"
    assert owner.age.total_seconds() == minutes * 60 + seconds


def test_ps_age_for_a_pid_nothing_holds_is_nothing(recorded):
    """Header only (exit 1): the header is not a process called NAME."""
    assert p.parse_process_age(recorded.text("ps_lifetime_not_running"), 999999) is None


def test_one_apps_jank_is_one_site_across_its_restarts(recorded):
    """Named by its process, a restarted app's jank is one site (review of PR #26).

    Keyed by pid, the same app restarted was a new site every time, and
    ``--diff`` across the restart reported "1 resolved, 1 new, regression".
    """
    names = _restart_names(recorded)
    events = [
        p.build_normalised_event({**raw, "app": names[raw["pid"]]}, 0, current_ms=1000)
        for raw in _restart_jank(recorded)
    ]

    assert len({event.fingerprint for event in events}) == 1
    assert {event.identity for event in events} == {"package"}
    (cluster,) = p.cluster_events(events)
    assert cluster.identity == "package"


def test_a_restarted_apps_named_jank_is_no_regression(recorded):
    names = _restart_names(recorded)
    first, _, last = (
        p.build_normalised_event({**raw, "app": names[raw["pid"]]}, 0, current_ms=1000)
        for raw in _restart_jank(recorded)
    )

    result = p.diff_sessions(_summary([first]), _summary([last]))

    assert result["new_clusters"] == [] and result["resolved_clusters"] == [], result
    assert "regression" not in result["verdict"]


def test_jank_known_only_by_pid_is_not_compared_across_sessions(recorded):
    """No process name, no identity across sessions: say so, do not guess.

    A pid is reused and renumbered across restarts, so a pid-keyed cluster in
    one session and one in another can be neither the same site nor a
    different one as far as anything here knows.
    """
    first, _, last = (
        p.build_normalised_event(raw, 0, current_ms=1000) for raw in _restart_jank(recorded)
    )
    assert first.identity == "pid"

    result = p.diff_sessions(_summary([first]), _summary([last]))

    assert result["new_clusters"] == [] and result["resolved_clusters"] == [], result
    assert "regression" not in result["verdict"]
    assert len(result["not_comparable"]) == 2
    assert result["verdict"] == "inconclusive: 2 clusters could not be compared"
    assert "Not comparable (2)" in p.format_diff(result)


def _same_jank_named_and_unnamed(recorded):
    """One recorded jank line, as a session that named its process and one that could not."""
    names = _restart_names(recorded)
    raw = _restart_jank(recorded)[0]
    named = p.build_normalised_event({**raw, "app": names[raw["pid"]]}, 0, current_ms=1000)
    unnamed = p.build_normalised_event(raw, 0, current_ms=1000)
    assert (named.identity, unnamed.identity) == ("package", "pid")
    return named, unnamed


@pytest.mark.parametrize("named_in", ["first", "second"])
def test_jank_named_in_only_one_session_is_neither_resolved_nor_new(named_in, recorded):
    """Unequal naming coverage must not manufacture a verdict (review of PR #26).

    The same jank, named in one session and only pid-keyed in the other, read
    as "improvement: 1 resolved" one way round and "regression: 1 new
    critical" the other: absence concluded from a failed identification. The
    pid-keyed counterpart has the same identity-free signature, so the named
    cluster could be it -- both are not comparable.
    """
    named, unnamed = _same_jank_named_and_unnamed(recorded)
    a, b = (named, unnamed) if named_in == "first" else (unnamed, named)

    result = p.diff_sessions(_summary([a]), _summary([b]))

    assert result["new_clusters"] == [] and result["resolved_clusters"] == [], result["verdict"]
    assert not result["verdict"].startswith(("improvement", "regression")), result["verdict"]
    assert sorted(c["identity"] for c in result["not_comparable"]) == ["package", "pid"]


def test_a_diff_that_could_not_compare_says_inconclusive_not_no_change(recorded):
    """Review of PR #26: "no change; 2 not comparable" hid a genuinely new site.

    An unnamed jank cluster in the first session and a NAMED one in the second
    share a signature (kind and message only -- every Choreographer line reads
    "Skipped <n> frames!"), so the named one is set aside as not comparable.
    It may be a new app janking; nothing here can say it is not. When that is
    all there is, the verdict is that the diff could not decide.
    """
    named, unnamed = _same_jank_named_and_unnamed(recorded)

    result = p.diff_sessions(_summary([unnamed]), _summary([named]))

    assert result["verdict"] == "inconclusive: 2 clusters could not be compared"
    assert "inconclusive" in p.format_diff(result)


def test_a_diff_against_an_incomplete_session_says_so(recorded):
    """A summary saved before its worker finished may be missing events.

    Nothing changed: the diff cannot call that "no change". Something changed:
    the verdict stands, and says what it stands on.
    """
    anr = _norm(_anr_line(recorded), current_ms=5000)
    short = _summary([anr])
    short.incomplete = True
    empty_short = _summary([])
    empty_short.incomplete = True

    unchanged = p.diff_sessions(short, _summary([anr]))
    changed = p.diff_sessions(_summary([anr]), empty_short)

    assert unchanged["verdict"] == f"inconclusive: {short.session_id} is incomplete"
    assert changed["verdict"] == (
        f"improvement: 1 resolved; {empty_short.session_id} is incomplete"
    )


def test_a_named_cluster_with_no_unnamed_lookalike_is_still_new(recorded):
    """The rule is narrow: an unrelated pid-keyed cluster does not hide a new one."""
    named, _ = _same_jank_named_and_unnamed(recorded)
    anr = _norm(_anr_line(recorded), current_ms=5000)

    result = p.diff_sessions(_summary([anr]), _summary([anr, named]))

    assert [c["identity"] for c in result["new_clusters"]] == ["package"]
    assert result["verdict"].startswith("regression")


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
    # Three processes: one process's jank is one cluster, whatever its counts.
    events = [
        _norm(_jank_line(recorded, 10, pid=1001)),  # minor
        _norm(_jank_line(recorded, 150, pid=1002)),  # frozen
        _norm(_jank_line(recorded, 40, pid=1003)),  # warn
    ]
    ranked = p.rank_clusters(p.cluster_events(events))
    assert ranked[0].severity is p.Severity.FROZEN
    assert ranked[-1].severity is p.Severity.MINOR


def test_rank_top_n_caps_results(recorded):
    events = [_norm(_jank_line(recorded, n, pid=1000 + n)) for n in (35, 65, 120)]
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


def _zero_to_n_diff(recorded) -> dict:
    """A cluster whose max duration is 0 in A and real in B (N9's 0 -> N case)."""
    raw = p.parse_logcat_anr(_strictmode_line(recorded))
    assert raw is not None and raw["duration_ms"] > 0
    silent = p.build_normalised_event({**raw, "duration_ms": 0.0}, 0, current_ms=1000)
    loud = p.build_normalised_event(raw, 0, current_ms=1000)
    assert silent is not None and loud is not None
    assert silent.fingerprint == loud.fingerprint
    return p.diff_sessions(_summary([silent]), _summary([loud]))


def test_diff_from_zero_is_null_plus_a_marker_not_infinity(recorded):
    """N9: ``float("inf")`` became the bare token ``Infinity``, which is not JSON."""
    import json

    result = _zero_to_n_diff(recorded)

    (entry,) = result["drift"]
    assert entry["from_zero"] is True
    assert entry["delta_pct"] is None
    json.loads(json.dumps(result), parse_constant=lambda name: pytest.fail(f"bare {name}"))
    assert json.dumps(result, allow_nan=False)


def test_diff_from_zero_counts_as_worsened_and_says_so(recorded):
    result = _zero_to_n_diff(recorded)

    assert result["verdict"] == "drift: 1 worsened, 0 improved"
    assert "(from 0)" in p.format_diff(result)


def test_diff_finite_drift_carries_the_marker_false(recorded):
    """Every drift entry has the same keys, so a reader never guesses.

    The jank is named (as the session worker names it) so it is comparable:
    pid-keyed jank is listed as not comparable instead of drifting.
    """
    (app,) = set(_restart_names(recorded).values())

    def named(frames):
        raw = p.parse_logcat_anr(_jank_line(recorded, frames))
        return p.build_normalised_event({**raw, "app": app}, 0, current_ms=1000)

    base = _summary([named(35)])
    worse = _summary([named(120)])

    (entry,) = p.diff_sessions(base, worse)["drift"]

    assert entry["from_zero"] is False
    assert entry["delta_pct"] > 0


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
