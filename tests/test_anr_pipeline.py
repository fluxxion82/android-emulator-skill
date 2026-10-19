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
from dataclasses import replace
from datetime import datetime

import pytest
from fake_toolchain import recorded_ps_answer

from common import anr_pipeline as p
from common import log_lifecycle, logcat
from common.device_utils import parse_package_uids, parse_ps_uids
from common.logcat import LogRecord

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


BROADCAST = "logcat_anr_broadcast_records"
INPUT = "logcat_anr_input_records"
RESTART = "logcat_app_restart_records"
INJECTION = "logcat_tag_injection_records"
PACKAGE = "com.example.composefixture"


def _records(recorded, name: str) -> list[LogRecord]:
    """A recorded ``logcat -B`` dump, read by common.logcat -- what every ANR reader reads."""
    return logcat.parse_records(recorded.data(f"{name}.bin"))


def _entry(recorded, name: str) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == name)


def _system_server(recorded, name: str) -> str:
    """system_server's pid as `pidof system_server` answered in ``name``'s own run.

    Checked, not assumed: every ActivityManager and WindowManager record in
    the two ANR recordings must have been written by it.
    """
    measured = str(_entry(recorded, name).get("system_server_pid", ""))
    if not measured.isdigit():
        pytest.fail(f"{name} records no system_server pid; re-record it")
    if name in (BROADCAST, INPUT):
        writers = {
            str(r.pid)
            for r in _records(recorded, name)
            if r.tag in ("ActivityManager", "WindowManager")
        }
        if writers != {measured}:
            pytest.fail(f"{name} was written by {writers}, not system_server {measured}")
    return measured


def _owner(recorded):
    """Whose process a pid is, as the device answers: ``(pid, package) -> bool | None``.

    The recorded answers: ``ps -o PID,UID -p <pid>`` is ps_pid_uid with only
    its app row's pid substituted (fake_toolchain.recorded_ps_answer: the
    fixture app's process, alive under its uid), and the package's uids are
    package_list_uid's. A package that listing does not name has no uid, so
    nothing is shown -- None.
    """

    def owns(pid: int, package: str) -> bool | None:
        uids = parse_package_uids(recorded.text("package_list_uid"), package)
        uid = parse_ps_uids(recorded_ps_answer(recorded, str(pid))).get(str(pid))
        return None if uids is None or uid is None else uid in uids

    return owns


def _stream(recorded, records: list[LogRecord], name: str = BROADCAST, owner=_owner) -> list[dict]:
    """``parse_logcat_stream`` over records, system_server as measured with ``name``.

    ``owner`` answers whose process a report's pid is (default: the recorded
    answers, :func:`_owner`); None asks nothing, so no ANR is filed under a
    package.
    """
    return p.parse_logcat_stream(
        records,
        system_server_pid=_system_server(recorded, name),
        subject_owner=owner(recorded) if owner else None,
    )


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


def test_parse_window_anr_names_neither_the_word_window_nor_its_title(recorded):
    r"""WindowManager's form: what follows the window's hash is its TITLE.

    ``ANR in Window{67a8d77 u0 com.pkg/com.pkg.MainActivity}`` also matches the
    generic ``ANR in ([\w.]+)`` pattern, which read the package as "Window".
    Read off the braces instead, the title made the package -- and the title
    is the app's to set (wave 4). It is kept as display text, ``window``.
    """
    raw = p.parse_logcat_anr(_window_anr_line(recorded))

    assert raw is not None
    assert raw["kind"] == "anr"
    assert (raw.get("anr_package"), raw.get("component")) == (None, None)
    assert raw["window"] == (
        "com.example.composefixture/com.example.composefixture.DefaultActivity"
    )


def test_parse_window_anr_takes_no_symbol_from_its_title(recorded):
    event = _norm(_window_anr_line(recorded), current_ms=5000)
    assert (event.symbol, event.identity, event.app) == (None, "window", None)
    assert event.message_prefix.startswith("ANR in a window titled ")


def test_one_stall_is_reported_by_two_subsystems(recorded):
    """Both recordings are of the same 40s block, under different tags.

    Anything counting ANRs has to know that, or one hang is two incidents.
    ActivityManager names the package; WindowManager names a window, whose
    title here is that package's activity -- as the app left it.
    """
    am = p.parse_logcat_anr(_anr_line(recorded))
    wm = p.parse_logcat_anr(_window_anr_line(recorded))

    assert am is not None and wm is not None
    assert am["process"] == "ActivityManager"
    assert wm["process"] == "WindowManager"
    assert wm["window"].startswith(f"{am['anr_package']}/")


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
# The two *_records recordings are every ActivityManager and WindowManager
# record logged from the provocation on, as logd's records (`logcat -B`) --
# what every ANR reader reads since PR #34 adopted common.logcat's reader --
# so "how many ANRs is this?" is asked of what a reader actually sees.


def _report(recorded, name: str = BROADCAST) -> LogRecord:
    """ActivityManager's ANR report: ONE record, its PID and Reason lines in its message."""
    return next(
        r
        for r in _records(recorded, name)
        if r.tag == "ActivityManager" and r.message.startswith("ANR in ")
    )


def _window(recorded, name: str = BROADCAST) -> LogRecord:
    """WindowManager's report of the same stall, one record."""
    return next(r for r in _records(recorded, name) if r.tag == "WindowManager")


def _verdict(recorded) -> LogRecord:
    """ActivityManager's "Skipping duplicate ANR" verdict on WindowManager's report."""
    return next(
        r for r in _records(recorded, BROADCAST) if r.message.startswith("Skipping duplicate ANR")
    )


def _stated(record: LogRecord, field: str) -> str:
    """The value the report states for ``field`` (``PID``, ``Reason``), on its own line."""
    match = re.search(rf"\n{field}: ([^\n]*)", record.message)
    if match is None:
        pytest.fail(f"the recorded report states no {field}; the fixture has changed shape")
    return match.group(1)


def _later(record: LogRecord, seconds: float) -> LogRecord:
    """The recorded record with only its time (logd's sec/nsec) moved ``seconds`` later."""
    total = record.sec * 1_000_000_000 + record.nsec + round(seconds * 1_000_000_000)
    return replace(record, sec=total // 1_000_000_000, nsec=total % 1_000_000_000)


def test_a_broadcast_anr_record_is_one_event_with_its_pid_and_reason(recorded):
    report = _report(recorded)

    events = _stream(recorded, [report])

    assert len(events) == 1, [e["message"] for e in events]
    (event,) = events
    assert event["anr_package"] == PACKAGE
    assert event["app_pid"] == int(_stated(report, "PID"))
    assert event["app_pid"] != event["pid"], "the record's pid is system_server's"
    assert event["reason"] == _stated(report, "Reason").strip()


def test_one_broadcast_anr_as_the_stream_carries_it_is_one_event(recorded):
    """ActivityManager's report, WindowManager's report, and "Skipping duplicate".

    Three places a record-at-a-time reader found an ANR, and one stall.
    """
    records = _records(recorded, BROADCAST)
    assert _window(recorded) in records and _verdict(recorded) in records

    events = _stream(recorded, records)

    assert [e["kind"] for e in events] == ["anr"], [e["message"] for e in events]
    assert events[0]["process"] == "ActivityManager"
    assert "Broadcast of Intent" in events[0]["reason"]
    assert events[0]["app_pid"] == int(_stated(_report(recorded), "PID"))


def test_an_input_anr_is_one_event_and_comes_from_activitymanager(recorded):
    """WindowManager reports first here, and ActivityManager's report follows.

    Dropping WindowManager's report is only safe because ActivityManager
    always records the incident itself; this is the recording that shows it
    does when the ANR is new, as the broadcast recording shows the duplicate.
    """
    records = _records(recorded, INPUT)
    window, report = _window(recorded, INPUT), _report(recorded, INPUT)
    assert records.index(window) < records.index(report), "WindowManager no longer reports first"

    events = _stream(recorded, records, INPUT)

    assert len(events) == 1, [e["message"] for e in events]
    (event,) = events
    assert event["process"] == "ActivityManager"
    assert event["component"] == "com.example.composefixture/.DefaultActivity"
    assert event["reason"].startswith("Input dispatching timed out")


def _input_cut_before_the_report(recorded) -> list[LogRecord]:
    records = _records(recorded, INPUT)
    return records[: records.index(_report(recorded, INPUT))]


def test_an_input_anr_cut_off_before_activitymanager_reports_still_counts(recorded):
    """WindowManager's report is a fallback, not noise (review of PR #26).

    The recorded input-only ANR, cut where a stop or a --since window can cut
    it: after WindowManager's report, before ActivityManager's. Dropping
    WindowManager's report unconditionally made that ZERO ANRs -- a missed
    ANR, which is worse than a duplicate.
    """
    cut = _input_cut_before_the_report(recorded)
    assert _window(recorded, INPUT) in cut

    events = _stream(recorded, cut, INPUT)

    assert [e["kind"] for e in events] == ["anr"], events
    # An ANR of a window: its title is the app's text, so it names no package (wave 4).
    assert (events[0]["anr_package"], events[0]["window"]) == (None, RECORDED_TITLE)
    assert events[0]["reason"].startswith("Input dispatching timed out")


def test_a_broadcast_anr_cut_off_after_windowmanagers_report_says_it_may_be_one(recorded):
    """The duplicate case, cut before ActivityManager's "Skipping duplicate" verdict.

    WindowManager reported ~5 s after ActivityManager's report of the
    broadcast ANR (recorded), and that report names no window; only the
    window's title -- the app's text -- could tie the two. It used to settle
    the report by that title, which let any title make a separate ANR vanish
    (Codex review of PR #39). Both are kept; the window's says it may be the
    same stall. The verdict, recorded 2 ms later, settles it by hash.
    """
    records = _records(recorded, BROADCAST)

    events = _stream(recorded, records[: records.index(_window(recorded)) + 1])

    assert [(e["process"], e.get("possible_duplicate", False)) for e in events] == [
        ("ActivityManager", False),
        ("WindowManager", True),
    ], events
    assert _stream(recorded, records[: records.index(_verdict(recorded)) + 1]) == events[:1]


def test_a_title_naming_the_reported_package_settles_nothing(recorded):
    """The reviewer's replay: an ANR of B, then a separate window whose title names B.

    The recorded broadcast ANR's report (an ANR of the fixture app), then
    WindowManager's recorded report with only its window hash changed -- a
    different window -- and its title still the fixture app's activity. It
    vanished, settled by the title's package; a plain title kept it. It is
    kept either way, marked a possible duplicate.
    """
    window = _window(recorded)
    other = replace(window, message=window.message.replace("3e3de09", "5a5a5a5"))
    if other == window:
        pytest.fail("the recorded report no longer names window 3e3de09")
    plain = _retitled([other], "plain title")[0]

    for report in (other, plain):
        events = _stream(recorded, [_report(recorded), report])
        assert [e["process"] for e in events] == ["ActivityManager", "WindowManager"], events


def test_only_a_reports_opening_is_an_anr(recorded):
    """``PID:`` and ``Reason:`` are lines INSIDE the report; nothing else here is an ANR.

    In text they were lines of their own, and the removed follow-on branch
    made each a second ANR (D5). As records they cannot arrive alone: they
    are the report's own message. Every other record of the stall --
    "Broadcasting", "Enqueued", "Completed ANR" and the duplicate verdict --
    is no event.
    """
    report = _report(recorded)
    assert "\nPID: " in report.message and "\nReason: " in report.message
    others = [r for r in _records(recorded, BROADCAST) if not r.message.startswith("ANR in ")]
    assert len(others) >= 4, "the recording no longer holds the stall's other records"

    assert _stream(recorded, others) == []


def test_a_report_activitymanager_calls_a_duplicate_is_not_counted_however_late(recorded):
    """The user taps a still-blocked app long after its ANR was recorded.

    WindowManager reports again, past the correlation window, and
    ActivityManager answers "Skipping duplicate ANR" naming the window. That
    verdict, not the clock, is what settles it. Derived from the recorded
    broadcast records with only the times of those two moved 20 s on.
    """
    records = _records(recorded, BROADCAST)
    window, verdict = _window(recorded), _verdict(recorded)
    assert records.index(window) < records.index(verdict)
    stream = [*records[: records.index(window)], _later(window, 20), _later(verdict, 20)]

    events = _stream(recorded, stream)

    assert [e["process"] for e in events] == ["ActivityManager"], events


def test_a_held_report_is_released_by_log_time_not_only_at_the_end(recorded):
    """A WindowManager report nothing answers is an event once 10 s of log pass.

    The record that passes them: the recorded "Broadcasting" record with only
    its time set 11 s after WindowManager's report.
    """
    cut = _input_cut_before_the_report(recorded)
    window = _window(recorded, INPUT)
    assembler = p.AnrRecordAssembler(_system_server(recorded, INPUT))
    assert [event for record in cut for event in assembler.feed(record)] == []
    later = replace(cut[0], sec=window.sec + p.ANR_CORRELATION_MS // 1000 + 1, nsec=window.nsec)

    released = assembler.feed(later)

    assert [(e["process"], e["kind"]) for e in released] == [("WindowManager", "anr")]
    assert assembler.close() == []


def test_a_duplicate_verdict_settles_the_report_it_names(recorded):
    assert _stream(recorded, [_window(recorded), _verdict(recorded)]) == []


def test_a_duplicate_verdict_past_the_window_settles_nothing(recorded):
    """The 10 s bound holds on the verdict path too."""
    late = _later(_verdict(recorded), p.ANR_CORRELATION_MS / 1000 + 1)

    events = _stream(recorded, [_window(recorded), late])

    assert [e["process"] for e in events] == ["WindowManager"]


def test_a_duplicate_verdict_logged_long_before_the_report_settles_nothing(recorded):
    """The bound is checked on the verdict path itself, in both directions.

    A verdict LATER than the window never reaches it -- the report is released
    first -- so the test that can tell is one logged earlier: logcat
    interleaves buffers, and a record can arrive after one stamped later. Here
    by 11 s, derived from the recorded verdict with only its time moved.
    """
    window = _window(recorded)
    early = replace(
        _verdict(recorded), sec=window.sec - p.ANR_CORRELATION_MS // 1000 - 1, nsec=window.nsec
    )

    events = _stream(recorded, [window, early])

    assert [e["process"] for e in events] == ["WindowManager"]


def test_a_held_report_waits_out_a_short_quiet_and_not_a_long_one(recorded):
    """ActivityManager's report can follow WindowManager's by a quiet moment.

    Recorded: 0.46 s. A quarter-second of quiet must not release the report
    as an ANR of its own; a quiet as long as the whole correlation window
    means no report is coming.
    """
    assembler = p.AnrRecordAssembler(_system_server(recorded, INPUT))
    for record in _input_cut_before_the_report(recorded):
        assembler.feed(record)

    assert assembler.flush(quiet_seconds=0.25) == []
    released = assembler.flush(quiet_seconds=p.ANR_CORRELATION_MS / 1000)
    assert [e["process"] for e in released] == ["WindowManager"]


def test_an_anr_report_is_an_event_the_moment_it_is_read(recorded):
    """One record, so nothing waits for a header to change, or for quiet.

    In text a report was many lines, complete only when something else
    followed -- every reader had to flush on quiet. A jank record that follows
    is its own event, on its own.
    """
    jank = next(
        r
        for r in _records(recorded, RESTART)
        if r.tag == "Choreographer" and r.message.startswith("Skipped")
    )
    assembler = p.AnrRecordAssembler(_system_server(recorded, BROADCAST))

    assert [e["kind"] for e in assembler.feed(_report(recorded))] == ["anr"]
    assert [e["kind"] for e in assembler.feed(jank)] == ["jank"]
    assert assembler.flush(quiet_seconds=p.ANR_CORRELATION_MS / 1000) == []
    assert assembler.close() == []


def test_the_reports_reason_reaches_the_normalised_event(recorded):
    (raw,) = _stream(recorded, [_report(recorded)])
    event = p.build_normalised_event(raw, session_start_ms=0, current_ms=5000)

    assert event is not None
    assert event.reason == raw["reason"]
    assert event.app_pid == raw["app_pid"]
    restored = p.event_from_jsonl(p.event_to_jsonl(event))
    assert (restored.reason, restored.app_pid) == (event.reason, event.app_pid)


def test_an_events_time_is_the_records_own_to_the_microsecond(recorded):
    """The timestamp is logd's sec/nsec as ``-v usec`` prints it, never a parsed header."""
    report = _report(recorded)

    (event,) = _stream(recorded, [report])

    stamp = datetime.fromtimestamp(report.sec)
    assert event["timestamp"] == f"{stamp:%m-%d %H:%M:%S}.{report.nsec // 1000:06d}"


# --- assemble: an ANR report counts only as system_server wrote it ----------
#
# Review of PR #34: any app may log under ActivityManager's or WindowManager's
# tag, and the assembler took an app's word for an ANR -- and for the verdict
# that settles one. Each forgery below is the recorded record with only the
# field an app controls changed; system_server's pid is the device's answer.
# The writer is the record's pid: logd's, from the writing socket.


def _app_pid(recorded) -> int:
    """The stalled app's own pid, as the report states it (its ``PID:`` line)."""
    return int(_stated(_report(recorded), "PID"))


def test_system_servers_own_record_counts(recorded):
    """The control: the recorded report, written by system_server, is one ANR."""
    events = _stream(recorded, [_report(recorded)])

    assert [e["anr_package"] for e in events] == [PACKAGE]


def test_an_anr_record_written_by_an_app_is_not_counted(recorded):
    """The reviewer's forgery: an app writes ``ANR in <another package>`` itself.

    Derived: the recorded report with only its writer -- the record's pid --
    changed to the stalled app's own pid.
    """
    forged = replace(_report(recorded), pid=_app_pid(recorded))

    assert _stream(recorded, [forged]) == []


def test_an_app_written_duplicate_verdict_does_not_settle_a_real_report(recorded):
    """The reviewer's second forgery: a fake "Skipping duplicate ANR" made an ANR vanish.

    WindowManager's recorded report, with no ActivityManager report behind
    it, is an ANR of its own. Followed by ActivityManager's recorded verdict,
    it is settled; followed by the same verdict written by the app, it must
    not be.
    """
    window, verdict = _window(recorded), _verdict(recorded)

    genuine = _stream(recorded, [window, verdict])
    forged = _stream(recorded, [window, replace(verdict, pid=_app_pid(recorded))])

    assert genuine == []
    assert [e["process"] for e in forged] == ["WindowManager"]


def test_a_report_counts_only_under_activity_or_window_managers_tag(recorded):
    """system_server writes under many tags; a report is ActivityManager's or WindowManager's.

    Derived: ActivityManager's recorded report with only its tag changed to
    another of system_server's, ActivityTaskManager.
    """
    other_tag = replace(_report(recorded), tag="ActivityTaskManager")

    assert _stream(recorded, [other_tag]) == []


def test_a_duplicate_verdict_counts_only_under_activitymanagers_tag(recorded):
    """The verdict is ActivityManager's, not any system_server record that reads like one.

    Derived: ActivityManager's recorded verdict with only its tag changed to
    WindowManager's. The report it would have settled stands.
    """
    other_tag = replace(_verdict(recorded), tag="WindowManager")

    events = _stream(recorded, [_window(recorded), other_tag])

    assert [e["process"] for e in events] == ["WindowManager"]


def test_an_anr_phrase_inside_a_system_server_message_is_not_a_report(recorded):
    """Only the opening counts: system_server's messages carry an app's text further on.

    Derived: system_server's recorded ``Broadcasting:`` record with only the
    broadcast's action -- the app's own string -- replaced by an ANR phrase.
    """
    record = next(r for r in _records(recorded, BROADCAST) if r.message.startswith("Broadcasting:"))
    action = re.search(r"act=(\S+)", record.message).group(1)
    forged = replace(record, message=record.message.replace(action, "x ANR in com.example.victim"))

    assert _stream(recorded, [forged]) == []


def test_a_report_in_a_later_line_of_a_message_is_not_a_report(recorded):
    """A later line of a system_server message starts with whatever an app put in it.

    An app's broadcast action with a newline in it puts a line of the app's
    choosing inside system_server's report. Derived: the recorded report with
    one more line after its ``Reason:`` line -- ``ANR in com.example.victim``.
    One ANR, the real one.
    """
    report = _report(recorded)
    lines = report.message.split("\n")
    reason = next(i for i, line in enumerate(lines) if line.startswith("Reason: "))
    lines.insert(reason + 1, "ANR in com.example.victim")
    forged = replace(report, message="\n".join(lines))

    events = _stream(recorded, [forged])

    assert [e["anr_package"] for e in events] == [PACKAGE]


def _forging(recorded) -> LogRecord:
    """The recorded tag-injection record: the shell's, a system_server header in its TAG.

    logcat_tag_injection_records: its tag is ``x:``, a newline, and a
    threadtime header claiming system_server's pid and the tag ActivityManager.
    Printed as text, that is a whole line of system_server's (the text twin,
    logcat_tag_injection, shows it so).
    """
    forger = int(_entry(recorded, INJECTION)["injection"]["forger_pid"])
    forging = next(r for r in _records(recorded, INJECTION) if r.pid == forger)
    if "\n" not in forging.tag or "ActivityManager" not in forging.tag:
        pytest.fail("the recorded forging record no longer carries its header in its tag")
    return forging


def test_a_system_server_header_forged_in_a_tag_is_not_an_anr(recorded):
    """The tag-injection regression for ANR events (A1, adopted from W3-L).

    Derived: the recorded forging record with only its message -- the
    forger's own text -- replaced by an ANR report for the fixture package.
    The record's tag is not ActivityManager, and its writer is the shell.
    """
    forged = replace(_forging(recorded), message=f"ANR in {PACKAGE}")

    assert _stream(recorded, [forged], INJECTION) == []


def test_a_duplicate_verdict_forged_in_a_tag_settles_nothing(recorded):
    """The tag-injection regression for duplicate verdicts (A1, adopted from W3-L).

    WindowManager's recorded report, moved to a second before the forgery so
    the verdict falls inside its window, then the recorded forging record with
    only its message replaced by ActivityManager's recorded verdict. The
    report stands as the ANR.
    """
    forging = _forging(recorded)
    held = replace(_window(recorded), sec=forging.sec - 1, nsec=forging.nsec)
    forged = replace(forging, message=_verdict(recorded).message)

    events = _stream(recorded, [held, forged], INJECTION)

    # It stands as an ANR of its window, which names no package (wave 4).
    assert [(e["process"], e["anr_package"]) for e in events] == [("WindowManager", None)]


def test_with_system_servers_pid_unknown_no_report_counts_and_it_says_so(recorded):
    """Fail closed -- and countably: ``unverified`` is what a reader discloses."""
    assembler = p.AnrRecordAssembler(lambda: None)

    records = _records(recorded, BROADCAST)
    events = [event for r in records for event in assembler.feed(r)] + assembler.close()

    assert [e for e in events if e["kind"] == "anr"] == []
    assert assembler.unverified == 3, "the report, WindowManager's and the verdict were set aside"


def test_system_servers_pid_is_asked_only_when_a_report_arrives(recorded):
    """ActivityManager's other records -- Start proc, Killing, has died -- ask nothing."""
    asked: list[str] = []
    assembler = p.AnrRecordAssembler(lambda: asked.append("asked") or None)
    records = _records(recorded, RESTART)
    assert any(r.tag == "ActivityManager" for r in records)

    for record in records:
        assembler.feed(record)
    assembler.close()

    assert asked == []


# --- assemble: a window's title is the app's text (final review, wave 4) -----
#
# WindowManager's report prints the window as WindowState does: "Window{<hash>
# u<user> <title>}". The hash and user are system_server's; the TITLE is the
# window's, which the app sets (an activity's defaults to its component, which
# is what the recordings show). The same title is in the input channel's name,
# so every "Input dispatching timed out (<hash> <title> (server) ..." reason
# carries it too -- WindowManager's own, ActivityManager's report's and its
# duplicate verdict's. Each attack below is the recorded records with ONLY
# that title replaced, wherever it is printed (``_retitled``).

# The window's title as recorded: the fixture activity's default, its component.
RECORDED_TITLE = f"{PACKAGE}/{PACKAGE}.DefaultActivity"
VICTIM_TITLE = "com.example.victim/com.example.victim.DefaultActivity"


def _retitled(records: list[LogRecord], title: str) -> list[LogRecord]:
    """The records with only the window's title -- the app's own text -- replaced."""
    if not any(RECORDED_TITLE in r.message for r in records):
        pytest.fail("the recorded records no longer print the window's title")
    return [replace(r, message=r.message.replace(RECORDED_TITLE, title)) for r in records]


def _named(events: list[dict]) -> list[tuple[str, str | None]]:
    """``(tag, package)`` of each event -- the package being what it would be filed under."""
    return [(e["process"], e.get("anr_package")) for e in events]


def test_a_title_naming_another_app_does_not_make_an_anr_of_it(recorded):
    """The reviewer's first replay: the app titles its window as another app's activity.

    The recorded input ANR -- WindowManager's report, then ActivityManager's
    -- with only the title replaced by ``com.example.victim/…``. It came out
    as TWO ANRs: the real app's, and one of the victim's. The title names
    nothing; ActivityManager's report names the window it timed out on by
    its hash, so the two reports are one incident, the real app's.
    """
    records = _retitled(_records(recorded, INPUT), VICTIM_TITLE)

    events = _stream(recorded, records, INPUT)

    assert _named(events) == [("ActivityManager", PACKAGE)], events
    assert not any("victim" in str(e.get("component")) for e in events), events


def test_a_title_reading_like_jank_does_not_make_the_report_jank(recorded):
    """The reviewer's second replay: a title holding ``Skipped 80 frames!``.

    The jank classifier matched it anywhere in WindowManager's message, and
    the report came out as 80 frames of jank -- beside the real ANR. A report
    is read for its opening only; nothing after it is classified.
    """
    records = _retitled(_records(recorded, INPUT), "Skipped 80 frames!")

    events = _stream(recorded, records, INPUT)

    assert [(e["process"], e["kind"]) for e in events] == [("ActivityManager", "anr")], events


@pytest.mark.parametrize("title", [RECORDED_TITLE, VICTIM_TITLE, "Skipped 80 frames!"])
def test_a_report_nothing_answers_is_an_anr_of_a_window_not_of_a_package(title, recorded):
    """The fallback names no package: only ActivityManager's own record could, and none came.

    The recorded input ANR cut after WindowManager's report, as a stop or a
    --since window can cut it, its title as recorded, a victim's component,
    or jank-like text. Each is one ANR of a WINDOW: ``anr_package`` null,
    ``identity: "window"``, the title kept as display text, and no symbol
    taken from it.
    """
    cut = _retitled(_input_cut_before_the_report(recorded), title)

    events = _stream(recorded, cut, INPUT)

    assert [(e["kind"], e.get("anr_package"), e.get("window")) for e in events] == [
        ("anr", None, title)
    ], events
    event = p.build_normalised_event(events[0], session_start_ms=0, current_ms=5000)
    assert (event.app, event.identity, event.window, event.symbol) == (None, "window", title, None)


def test_a_verdict_settles_only_the_window_it_names(recorded):
    """A duplicate verdict names the window by its hash: another window's report stands.

    Derived: WindowManager's recorded report with only its window replaced by
    another -- a different identity hash, as another window has -- then the
    recorded verdict on the original window. The other window's report is an
    ANR of its own.
    """
    window = _window(recorded)
    other = replace(window, message=window.message.replace("3e3de09", "5a5a5a5"))
    if other == window:
        pytest.fail("the recorded report no longer names window 3e3de09")

    events = _stream(recorded, [other, _verdict(recorded)])

    assert [(e["process"], e.get("anr_package")) for e in events] == [("WindowManager", None)]


# --- assemble: ActivityManager names a process, not a package (W4-N) --------
#
# logcat_anr_foreign_process_records: the fixture app's own stall, in a
# process it named com.example.victim (android:process), under its own uid.
# ActivityManager reports "ANR in com.example.victim" -- mApp.processName --
# and its PID: line is that process. The device's answers in the same run:
# `ps -o PID,UID -p <pid>` while it lived, and each package's uid listing (the
# claimed one is not installed).

FOREIGN = "logcat_anr_foreign_process_records"
FOREIGN_PROCESS = "com.example.victim"


def _recorded_owner(recorded, name: str = FOREIGN):
    """``(pid, package) -> bool | None`` from the answers recorded in ``name``'s own run."""
    entry = _entry(recorded, name)
    listing = (
        entry["package_uid_listing"]["output"] + entry["claimed_package_uid_listing"]["output"]
    )
    uids_alive = parse_ps_uids(entry["ps_alive"]["output"])

    def owns(pid: int, package: str) -> bool | None:
        uids, uid = parse_package_uids(listing, package), uids_alive.get(str(pid))
        return None if uids is None or uid is None else uid in uids

    return owns


def test_an_anr_of_a_process_named_after_another_package_is_not_filed_under_it(recorded):
    """The process ActivityManager names is the app's to choose: shown, never filed under.

    It was ``anr_package: com.example.victim`` -- an ANR of a package that
    did not stall, which passed ``--package com.example.victim``. The pid's
    uid (ps, while it lived) is the fixture app's, and the claimed package
    has none: unverified, kept as an ANR of a process.
    """
    stalled = _entry(recorded, FOREIGN)["stalled_pid"]

    events = _stream(recorded, _records(recorded, FOREIGN), FOREIGN, lambda r: _recorded_owner(r))

    (event,) = events
    assert (event["anr_package"], event.get("component")) == (None, None), event
    assert (event["process_name"], event["attribution"]) == (FOREIGN_PROCESS, "unverified")
    assert event["app_pid"] == int(stalled)
    normalised = p.build_normalised_event(event, session_start_ms=0, current_ms=5000)
    assert (normalised.app, normalised.identity, normalised.symbol) == (None, "process", None)


def test_a_process_shown_to_be_its_packages_is_filed_under_it(recorded):
    """The control: the recorded broadcast ANR, its pid's uid the package's -- verified."""
    (event,) = _stream(recorded, [_report(recorded)])

    assert (event["anr_package"], event["attribution"]) == (PACKAGE, "verified")


def test_with_no_owner_shown_no_anr_is_filed_under_a_package(recorded):
    """Fail closed: a reader that can show nothing files nothing under a package."""
    (event,) = _stream(recorded, [_report(recorded)], owner=None)

    assert (event["anr_package"], event["process_name"]) == (None, PACKAGE)
    assert event["attribution"] == "unverified"


# --- assemble: only system_server's records move the log clock --------------


def test_an_apps_timestamp_does_not_flush_a_held_report(recorded):
    """A record's time is the WRITER's: an app stamping one 60 s ahead released a held report.

    logd takes the time from the client's header (AOSP LogListener), unlike
    the pid. The reviewer's replay: WindowManager's recorded report, then an
    app's record dated 60 s after it -- the recorded jank record with only its
    time set so -- then ActivityManager's recorded report. One incident was
    counted twice. Only a record system_server wrote moves the clock that
    releases a held report.
    """
    cut = _input_cut_before_the_report(recorded)
    window = _window(recorded, INPUT)
    jank = next(r for r in _records(recorded, RESTART) if r.tag == "Choreographer")
    ahead = replace(jank, sec=window.sec + 60, nsec=window.nsec)

    events = _stream(recorded, [*cut, ahead, _report(recorded, INPUT)], INPUT)

    assert sorted(e["kind"] for e in events) == ["anr", "jank"], _named(events)
    assert [e["process"] for e in events if e["kind"] == "anr"] == ["ActivityManager"]


def test_system_servers_own_later_record_still_releases_a_held_report(recorded):
    """The clock still moves: system_server's own record 11 s on releases the report.

    The recorded "Broadcasting" record -- system_server's -- with only its
    time set 11 s after WindowManager's report; and the same record with its
    writer set to the stalled app's pid first, which must move nothing.
    """
    cut = _input_cut_before_the_report(recorded)
    window = _window(recorded, INPUT)
    assembler = p.AnrRecordAssembler(_system_server(recorded, INPUT))
    assert [event for record in cut for event in assembler.feed(record)] == []
    later = replace(cut[0], sec=window.sec + p.ANR_CORRELATION_MS // 1000 + 1, nsec=window.nsec)
    app_later = replace(later, pid=_app_pid(recorded))

    assert assembler.feed(app_later) == [], "an app's record moved the clock"
    released = assembler.feed(later)

    assert [(e["process"], e["kind"]) for e in released] == [("WindowManager", "anr")]


# --- diff: a window is not an identity --------------------------------------


def test_an_anr_known_only_by_its_window_is_not_compared_across_sessions(recorded):
    """A WindowManager-only ANR names no package, so a diff cannot place it.

    Session A holds the recorded broadcast ANR (ActivityManager's record);
    session B the recorded input ANR cut before ActivityManager reported it.
    The window ANR may be A's app again: A's ANR is neither resolved nor B's
    window a new one, and the verdict says the diff could not decide.
    """
    named = p.build_normalised_event(
        _stream(recorded, [_report(recorded)])[0], session_start_ms=0, current_ms=5000
    )
    (window_raw,) = _stream(recorded, _input_cut_before_the_report(recorded), INPUT)
    window = p.build_normalised_event(window_raw, session_start_ms=0, current_ms=5000)

    result = p.diff_sessions(_summary([named]), _summary([window]))

    assert result["new_clusters"] == [] and result["resolved_clusters"] == [], result["verdict"]
    assert sorted(str(c["identity"]) for c in result["not_comparable"]) == ["None", "window"]
    assert result["verdict"].startswith("inconclusive"), result["verdict"]
    assert "window" in p.format_diff(result)


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
    """The three jank records of logcat_app_restart_records: one app, three processes."""
    raws = [
        p.event_from_record(r)
        for r in _records(recorded, RESTART)
        if r.tag == "Choreographer" and r.message.startswith("Skipped")
    ]
    if len({raw["pid"] for raw in raws if raw}) != 3:
        pytest.fail("logcat_app_restart_records no longer holds one jank record per process")
    return raws


def _restart_names(recorded) -> dict[int, str]:
    """pid -> process name, from system_server's `Start proc` records in the recording.

    Read by common.log_lifecycle, the session worker's reader, with
    system_server's pid as the device answered it in that run.
    """
    reader = log_lifecycle.LifecycleReader(_system_server(recorded, RESTART), lambda _p: None)
    names = {}
    for record in _records(recorded, RESTART):
        _writer, sentence = reader.read(record)
        if sentence is not None and sentence.what == log_lifecycle.STARTED:
            names[int(sentence.pid)] = sentence.name
    return names


def test_start_proc_names_each_process_of_the_restarted_app(recorded):
    names = _restart_names(recorded)

    assert {names[raw["pid"]] for raw in _restart_jank(recorded)} == {"com.example.composefixture"}


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


def _incomplete(summary: p.SessionSummary) -> p.SessionSummary:
    summary.incomplete = True
    return summary


def test_a_diff_against_an_incomplete_session_says_so(recorded):
    """A summary saved before its worker finished may be missing events.

    Nothing changed: the diff cannot call that "no change".
    """
    anr = _norm(_anr_line(recorded), current_ms=5000)
    short = _incomplete(_summary([anr]))

    unchanged = p.diff_sessions(short, _summary([anr]))

    assert unchanged["verdict"] == (
        f"inconclusive: 1 shared cluster not comparable: {short.session_id} is incomplete"
    )

    nothing = p.diff_sessions(_incomplete(_summary([])), _summary([]))

    assert nothing["verdict"] == f"inconclusive: {short.session_id} is incomplete"


@pytest.mark.parametrize("incomplete_side", ["first", "second"])
def test_absence_from_an_incomplete_session_is_never_resolved_or_new(incomplete_side, recorded):
    """B3: a cluster missing from a partial capture may simply be unwritten.

    The diff said "improvement: 1 resolved; B is incomplete" for an ANR that
    the complete session A saw and the incomplete session B did not -- and
    this test used to assert exactly that. Absence is the one thing an
    incomplete summary cannot show, so a cluster absent from the incomplete
    side is listed as unconfirmed: neither resolved (B incomplete) nor new
    (A incomplete), and the verdict is inconclusive.
    """
    anr = _norm(_anr_line(recorded), current_ms=5000)
    has, lacks = _summary([anr]), _incomplete(_summary([]))
    a, b = (has, lacks) if incomplete_side == "second" else (lacks, has)

    result = p.diff_sessions(a, b)

    assert result["resolved_clusters"] == [] and result["new_clusters"] == [], result["verdict"]
    assert [c["fingerprint"] for c in result["unconfirmed"]] == [anr.fingerprint]
    assert result["verdict"].startswith("inconclusive: "), result["verdict"]
    assert "Unconfirmed (1)" in p.format_diff(result)


def _named_jank(recorded, frames: int) -> p.NormalisedEvent:
    """The recorded jank line at ``frames``, named for the restarted app (as a session names it)."""
    (app,) = set(_restart_names(recorded).values())
    raw = p.parse_logcat_anr(_jank_line(recorded, frames))
    return p.build_normalised_event({**raw, "app": app}, 0, current_ms=1000)


def test_what_a_complete_session_lacks_is_still_resolved(recorded):
    """Presence is evidence even in a partial capture; only absence is not.

    A cluster the incomplete session A holds and the complete session B does
    not is still resolved -- B, which is complete, really lacks it.
    """
    anr = _norm(_anr_line(recorded), current_ms=5000)
    a = _incomplete(_summary([anr]))

    result = p.diff_sessions(a, _summary([]))

    assert [c["fingerprint"] for c in result["resolved_clusters"]] == [anr.fingerprint]
    assert result["unconfirmed"] == []


@pytest.mark.parametrize(
    ("frames_a", "frames_b", "incomplete_side"),
    [(120, 35, "second"), (35, 120, "first")],
    ids=["improved-because-b-is-short", "worsened-because-a-is-short"],
)
def test_a_shared_cluster_does_not_drift_against_an_incomplete_side(
    frames_a, frames_b, incomplete_side, recorded
):
    """Review of PR #34: an incomplete summary's maximum is only a lower bound.

    The events it is missing could raise it, so comparing it read as
    "improved" when the incomplete side was B and "worsened" when it was A --
    the reviewer's two reproductions, here with the recorded jank line at two
    substituted frame counts. With either side incomplete no shared cluster
    drifts or counts as stable; each is listed as not compared, and why.
    """
    a, b = _summary([_named_jank(recorded, frames_a)]), _summary([_named_jank(recorded, frames_b)])
    short = _incomplete(b if incomplete_side == "second" else a)

    result = p.diff_sessions(a, b)

    assert result["drift"] == [] and result["stable_count"] == 0, result["verdict"]
    (entry,) = result["not_compared"]
    assert entry["reason"] == f"not comparable: {short.session_id} is incomplete"
    assert result["verdict"] == (
        f"inconclusive: 1 shared cluster not comparable: {short.session_id} is incomplete"
    )
    assert "Not compared (1)" in p.format_diff(result)


def _dropped_unnamed(summary: p.SessionSummary, pid: int) -> p.SessionSummary:
    """``summary`` as a --package session that dropped one jank record of ``pid``, unnamed."""
    return replace(
        summary, session_id="anr-test-0002", unnamed_dropped=1, unnamed_dropped_pids=[pid]
    )


def test_jank_dropped_unnamed_leaves_an_anrs_absence_and_maximum_shown(recorded):
    """Closure review of v0.8.0: what --package drops for want of a name is jank or StrictMode.

    Session B dropped one recorded jank record whose process no `Start proc`
    named (``unnamed_dropped``), so a jank cluster's absence from B, or its
    maximum there, is not shown -- the dropped record may be it. An ANR is not
    such a record: the recorded broadcast ANR A holds and B lacks is still
    resolved, and an ANR both hold still compares.
    """
    (raw,) = _restart_jank(recorded)[:1]
    anr = _norm(_anr_line(recorded), current_ms=5000)
    a = _summary([anr, _named_jank(recorded, 120)])
    b = _dropped_unnamed(_summary([_named_jank(recorded, 35)]), raw["pid"])

    result = p.diff_sessions(a, b)

    assert [c["fingerprint"] for c in result["resolved_clusters"]] == [anr.fingerprint]
    assert (result["drift"], result["unconfirmed"]) == ([], [])
    (shared,) = result["not_compared"]
    assert shared["reason"] == (
        f"not comparable: {b.session_id} dropped 1 jank/StrictMode record of 1 pid it never "
        f"named, which --package could not attribute"
    )
    assert result["verdict"].startswith("improvement: 1 resolved; "), result["verdict"]

    both = p.diff_sessions(_summary([anr]), _dropped_unnamed(_summary([anr]), raw["pid"]))

    assert (both["stable_count"], both["not_compared"]) == (1, []), both
    assert both["verdict"] == (
        f"inconclusive: {b.session_id} dropped 1 jank/StrictMode record of 1 pid it never "
        f"named, which --package could not attribute"
    )
    assert "Not compared (1)" in p.format_diff(result)


def test_a_summary_with_jank_dropped_unnamed_round_trips(recorded):
    """``unnamed_dropped`` survives summary.json: --diff reads it back from disk."""
    (raw,) = _restart_jank(recorded)[:1]
    summary = _dropped_unnamed(_summary([]), raw["pid"])

    back = p.summary_from_json(p.summary_to_json(summary))

    assert (back.unnamed_dropped, back.unnamed_dropped_pids) == (1, [raw["pid"]])


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


_FORMATS = {
    "l0": p.format_l0,
    "l1": p.format_l1,
    "l2": p.format_l2,
    "budget-60": lambda s: p.compress_to_budget(s, max_tokens=60),
}


@pytest.mark.parametrize("fmt", list(_FORMATS))
@pytest.mark.parametrize("empty", [True, False], ids=["empty", "events"])
def test_every_text_format_says_the_summary_is_incomplete(fmt, empty, recorded):
    """B6: `incomplete: true` was in the JSON and nowhere in the text.

    `--get-details ID` on a summary saved before its worker finished printed
    "no ANR/jank ≥ threshold" and never the flag; the warning that said so
    went to stderr once, at the stop, and is not there when it is read later.
    """
    events = [] if empty else [_norm(_jank_line(recorded, 120))]
    summary = _incomplete(_summary(events))

    out = _FORMATS[fmt](summary)

    assert "incomplete" in out.lower(), out
    assert "incomplete" not in _FORMATS[fmt](_summary(events)).lower()


@pytest.mark.parametrize("fmt", ["l0", "l1", "l2"])
def test_an_empty_incomplete_summary_does_not_say_there_was_nothing(fmt):
    """ "No ANR/jank" about a partial capture is a claim about what it missed.

    The empty statement is qualified as an observation of the part captured.
    """
    out = _FORMATS[fmt](_incomplete(_summary([])))

    assert "in the part captured" in out, out


@pytest.mark.parametrize("fmt", ["l0", "budget-60", "budget-10"])
@pytest.mark.parametrize("empty", [True, False], ids=["empty", "events"])
def test_no_budget_cuts_what_the_summary_could_not_see(fmt, empty, recorded):
    """Codex review of PR #34: L0, and a budget that falls back to it, dropped every disclosure.

    With a record cut, a pid unnamed, ANR reports unverified and the stream
    broken, the one-liner printed only "no ANR/jank above threshold". The
    budget cuts clusters; each disclosure survives, as a sentence or a marker.
    """
    events = [] if empty else [_norm(_jank_line(recorded, 120))]
    summary = replace(
        _summary(events),
        cut_records=1,
        unnamed_pids=[4242],
        verification=p.VERIFICATION_UNKNOWN,
        unverified_anr_lines=2,
        stream_failure="the stream ended inside a record",
        # Wave 4: a restart's gap, an ANR --package could not attribute, and
        # the record the session started on (the recorded jank line, parsed).
        stream_restarts=1,
        unattributed_anrs=1,
        ambiguous_tail=[p.parse_logcat_anr(_jank_line(recorded, 120))],
    )
    render = {
        "l0": p.format_l0,
        "budget-60": lambda s: p.compress_to_budget(s, max_tokens=60),
        "budget-10": lambda s: p.compress_to_budget(s, max_tokens=10),
    }[fmt]

    out = render(summary).lower()

    for disclosed in (
        "cut",
        "unnamed",
        "unverified",
        "broke",
        "restart",
        "unattributed",
        "predate",
    ):
        assert disclosed in out, (disclosed, out)
    restored = p.summary_from_json(p.summary_to_json(summary))
    assert (restored.stream_restarts, restored.unattributed_anrs) == (1, 1)
    assert restored.ambiguous_tail == summary.ambiguous_tail


def test_an_incomplete_summary_says_why(recorded):
    """The reason the capture may be missing events is in the text, not only the flag."""
    summary = replace(_summary([]), incomplete=True, incomplete_reason="the stream broke")

    out = p.format_l1(summary)

    assert "Incomplete: the stream broke" in out, out
    assert p.summary_from_json(p.summary_to_json(summary)).incomplete_reason == "the stream broke"


def test_the_cluster_drill_says_the_session_is_incomplete(recorded):
    events = [_norm(_jank_line(recorded, 120)), _norm(_jank_line(recorded, 125))]
    (cluster,) = p.cluster_events(events)

    detail = p.format_cluster_detail(cluster, events, incomplete=True)

    assert "incomplete" in detail.lower(), detail
    assert "incomplete" not in p.format_cluster_detail(cluster, events).lower()


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
