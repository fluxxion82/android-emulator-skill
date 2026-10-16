"""Red specifications for v0.8.0 Inc 1 -- "diagnostics stop lying".

Each test here states what an agent must see once a finding is fixed, and each
was run unmarked against ``main`` at c2c7a8a and watched fail before it was
pinned ``xfail(strict=True)`` under its finding ID. The fix for a finding
deletes its marker in the same commit; strict mode turns a fixed-but-still-
marked spec red, so a marker cannot outlive its defect.

Every marker also carries ``raises=AssertionError``. Strict mode rejects an
unexpected PASS but accepts any way of failing, so on its own it would excuse
a hung worker, a JSON decode error or a missing snapshot file as "still the
pinned defect". Narrowed to the assertion, a spec that breaks some other way
fails loudly. The unmarked controls beside each group check the harness itself.

**What these assert, and what they deliberately do not.** Every spec drives
the real CLI as a subprocess (or ``main()`` where a host fact has to be
patched) and reads what an agent would read: exit status, stdout, the JSON, the
files a snapshot leaves behind. None of them names a function the fix has to
keep, so Inc 1 can restructure freely -- the test is the contract, not the
implementation. Where a spec also checks the command a script sent, that is
because the device would answer differently to a different command and the fake
cannot (see D1 and D3).

**Every byte a fake tool prints is recorded.** The toolchain comes from
``tests/fake_toolchain.py``; each rule is handed text read through the
``recorded`` / ``recorded_gradle`` fixtures, or derived from it by substitution
the way CLAUDE.md allows (a frame count, a duration). Nothing here is a
transcript somebody typed.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal

# The threadtime layout, read for its PRIORITY and PID columns only:
#   MM-DD HH:MM:SS.mmm  PID  TID  P  Tag: message
_THREADTIME = re.compile(r"^\d{2}-\d{2}\s+\S+\s+(?P<pid>\d+)\s+\d+\s+(?P<priority>[VDIWEF])\s")


def _threadtime_lines(text: str) -> list[re.Match]:
    return [m for m in (_THREADTIME.match(line) for line in text.splitlines()) if m]


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


# ===========================================================================
# X7 + N6 -- the build verdict, read from THIS run's JUnit XML.
# ===========================================================================

JUNIT_SUITES = (
    ("com.example.BeforeThrowsTest", "junit_xml_error_case"),
    ("com.example.PassingTest", "junit_xml_passing_case"),
    ("com.example.SkippedAndErrorTest", "junit_xml_skipped_case"),
)


def _results_dir(project: Path) -> Path:
    return project / "app" / "build" / "test-results" / "testDebugUnitTest"


def _gradle_project(toolchain: FakeToolchain, root: Path, rule: Rule) -> Path:
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    toolchain.install("gradlew", [rule], directory=project)
    return project


def _ignore_failures_project(toolchain, tmp_path, recorded_gradle) -> Path:
    """A `test { ignoreFailures = true }` run: three failures, exit 0.

    The stub prints the recorded log of that run and writes the three JUnit
    XML files that same run produced, at the moment it "runs" -- so they are
    this run's results under any reading of N6.
    """
    results = _results_dir(tmp_path / "proj")
    rule = Rule(
        ("testDebugUnitTest",),
        stdout=recorded_gradle("gradle_junit_error_ignorefailures"),
        exit=0,
        writes=tuple(
            (str(results / f"TEST-{name}.xml"), recorded_gradle(fixture))
            for name, fixture in JUNIT_SUITES
        ),
    )
    return _gradle_project(toolchain, tmp_path, rule)


X7_MODES = {
    "text": (),
    "verbose": ("--verbose",),
    "json": ("--json",),
}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X7: gradle/builder.py:225 -- success is Gradle's exit status, not the tests'",
)
@pytest.mark.parametrize("mode", list(X7_MODES))
def test_x7_failing_tests_fail_the_build_whatever_gradle_exits(
    mode, toolchain, tmp_path, recorded_gradle
):
    """X7: ``gradle/builder.py:225`` -- ``success = returncode == 0``.

    With ``ignoreFailures = true`` Gradle prints BUILD SUCCESSFUL and exits 0
    over three failing tests (recorded: gradle_junit_error_ignorefailures).
    The verdict must come from the tests, and every output mode must say the
    same thing: exit non-zero, ``success: false`` in JSON and in the cache an
    agent drills into later, FAIL in text and verbose.
    """
    project = _ignore_failures_project(toolchain, tmp_path, recorded_gradle)
    result = toolchain.run(
        "build_and_test.py", "--project", str(project), "--test", *X7_MODES[mode]
    )

    assert (
        result.returncode != 0
    ), f"{mode}: three JUnit failures and exit {result.returncode}.\n{result.stdout[-600:]}"
    if mode == "json":
        payload = json.loads(result.stdout)
        assert payload["success"] is False, payload
        from gradle import BuildResultCache

        cache = BuildResultCache(
            cache_dir=str(toolchain.home / ".android-emulator-skill" / "cache")
        )
        cached = cache.get(payload["build_id"])
        assert cached is not None, "the build id printed under --json does not resolve"
        assert cached["success"] is False, "--get-errors would drill into a build cached as passed"
    else:
        assert "FAIL" in result.stdout, result.stdout
        assert "SUCCESS" not in result.stdout and "PASS" not in result.stdout, result.stdout


def test_x7_the_fixture_build_really_reads_three_failures(toolchain, tmp_path, recorded_gradle):
    """Anti-vacuity for X7, unmarked: the stub ran and its XML was read.

    Without this the spec above could be "green" because gradlew was never
    invoked, or because the XML landed somewhere the script does not look.
    """
    project = _ignore_failures_project(toolchain, tmp_path, recorded_gradle)
    result = toolchain.run("build_and_test.py", "--project", str(project), "--test", "--json")
    payload = json.loads(result.stdout)
    assert payload["test_results"]["failed"] == 3, payload
    assert any("testDebugUnitTest" in call for call in toolchain.calls("gradlew"))


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N6: gradle/results.py:43-51 -- every XML under build/test-results counts, however old",
)
def test_n6_a_result_file_older_than_the_run_is_not_this_runs_result(
    toolchain, tmp_path, recorded_gradle
):
    """N6: ``gradle/results.py:43-51`` globs every ``**/build/test-results/**``.

    ``is_file()`` is the only filter, so a failing report left by an earlier
    run is counted into this one. Here the earlier report (recorded
    junit_xml_error_case, two failures) sits in the task's own results
    directory with an mtime an hour before the run; this run writes only the
    recorded passing suite. The run passed one test, and that is all it may
    report.
    """
    results = _results_dir(tmp_path / "proj")
    stale = results / "TEST-com.example.BeforeThrowsTest.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text(recorded_gradle("junit_xml_error_case"), encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(stale, (an_hour_ago, an_hour_ago))

    rule = Rule(
        ("testDebugUnitTest",),
        exit=0,
        writes=(
            (
                str(results / "TEST-com.example.PassingTest.xml"),
                recorded_gradle("junit_xml_passing_case"),
            ),
        ),
    )
    project = _gradle_project(toolchain, tmp_path, rule)
    result = toolchain.run("build_and_test.py", "--project", str(project), "--test", "--json")

    payload = json.loads(result.stdout)
    assert (
        payload["test_results"]["total"] == 1
    ), f"a report from before this run was counted: {payload['test_results']}"
    assert payload["test_results"]["failed"] == 0, payload["test_results"]
    assert result.returncode == 0, result.stdout


# ===========================================================================
# D1 -- log_monitor --severity: an unknown name is a usage error.
# ===========================================================================

ACCEPTED_SEVERITIES = ("error", "warning", "info", "debug", "verbose")


def _logcat_device(toolchain: FakeToolchain, recorded, fixture: str) -> None:
    """An adb whose device answers ``devices`` and ``logcat`` from recordings."""
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("logcat",), stdout=recorded.text(fixture), filter_pid=True),
        ],
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D1: log_monitor.py:679-681 accepts an unknown --severity and filters everything out",
)
@pytest.mark.parametrize("severity", ["warn", "error,warn"])
def test_d1_an_unknown_severity_name_is_a_usage_error(severity, toolchain, recorded):
    """D1: ``log_monitor.py:679-681`` / ``common/logcat.py:117-137``.

    ``warn`` is not a severity this CLI knows (its help lists error, warning,
    info, debug, verbose). ``min_priority_for`` drops an unknown name, so
    ``warn`` alone sends no ``*:`` filter at all and ``error,warn`` sends
    ``*:E``; then the post-parse check at ``log_monitor.py:238`` discards every
    line whose severity is not literally in the list. Both report a calm,
    successful capture with the warnings silently gone. An unknown name must be
    refused as a usage error (exit 2) naming the accepted set -- and before the
    device is asked anything.
    """
    _logcat_device(toolchain, recorded, "logcat_threadtime")
    result = toolchain.run("log_monitor.py", "--severity", severity, "--last", "1m", "--json")

    assert (
        result.returncode == 2
    ), f"--severity {severity} exited {result.returncode}:\n{result.stdout[:400]}"
    for name in ACCEPTED_SEVERITIES:
        assert name in result.stderr, f"the usage error does not name {name!r}:\n{result.stderr}"
    logcat_calls = [call for call in toolchain.calls("adb") if "logcat" in call]
    assert not logcat_calls, f"logs were read under a filter that was refused: {logcat_calls}"


def test_d1_a_known_severity_list_keeps_every_warning(toolchain, recorded):
    """D1's floor, unmarked: ``error,warning`` keeps what the device logged.

    Expected warnings are read off the recording's priority column, not typed
    here. And the ``*:P`` term sent to the device must not sit above W: the
    fake ignores priority filters, so the answer alone cannot show a device
    dropping warnings -- the command it was sent can.
    """
    _logcat_device(toolchain, recorded, "logcat_threadtime")
    result = toolchain.run(
        "log_monitor.py", "--severity", "error,warning", "--last", "1m", "--json"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    text = recorded.text("logcat_threadtime")
    warning_tags = {
        line.split()[5].rstrip(":")
        for line in text.splitlines()
        if (m := _THREADTIME.match(line)) and m["priority"] == "W"
    }
    assert warning_tags, "the recording has no W lines; this floor would be vacuous"
    reported = " ".join(payload["warnings"])
    missing = sorted(tag for tag in warning_tags if f"[{tag}]" not in reported)
    assert not missing, f"warnings from {missing} were dropped: {payload['warnings']}"

    logcat_calls = [call for call in toolchain.calls("adb") if "logcat" in call]
    assert logcat_calls, "log_monitor never asked the device for logs"
    for call in logcat_calls:
        priorities = [arg[2:] for arg in call if arg.startswith("*:")]
        assert all(p in "VDIW" for p in priorities), f"device filter drops warnings: {call}"


# ===========================================================================
# D3 -- app_state_capture counts errors by priority, in the format it asked for.
# ===========================================================================

# The process whose lines the D3 snapshot reads. 551 is system_server in the
# recording: it logged the one E line, and also a V line whose text contains
# "errorCallbackToken" -- prose that a word-match counts as an error.
D3_PID = "551"


def _snapshot_device(toolchain, recorded, logcat_fixture: str, pid_answer: str) -> None:
    """A device that answers pidof and logcat; every other probe fails quietly.

    The screenshot and hierarchy therefore fail and the run exits 1 -- that is
    X8 working, and not what these specs read. The log component still runs,
    and what it captured is on disk.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("shell", "pidof"), stdout=pid_answer),
            Rule(("logcat",), stdout=recorded.text(logcat_fixture), filter_pid=True),
        ],
    )


def _snapshot_artifact(output: Path, name: str) -> Path:
    found = sorted(output.rglob(name))
    assert len(found) == 1, f"expected one {name} under {output}, found {found}"
    return found[0]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D3: app_state_capture.py:85-91 counts errors from prose; :420 requests no format",
)
def test_d3_error_count_is_the_priority_column(toolchain, tmp_path, recorded):
    """D3: ``app_state_capture.py:85-91`` counts errors from prose.

    A line counts as an error if its text contains "error" or "exception",
    whatever its priority. The count in the snapshot must equal the number of
    E/F lines in the log the snapshot itself saved -- the window it read, as
    recorded -- and the logcat request must ask for the format that count
    parses (``:420`` passes ``fmt=None``, inheriting the device's default).
    """
    _snapshot_device(toolchain, recorded, "logcat_threadtime", pid_answer=D3_PID)
    output = tmp_path / "snapshots"
    toolchain.run(
        "app_state_capture.py",
        "--package",
        "com.example.composefixture",
        "--output",
        str(output),
        "--log-lines",
        "100000",
        "--json",
    )

    saved = _snapshot_artifact(output, "app-logs.txt").read_text(encoding="utf-8")
    expected = sum(1 for m in _threadtime_lines(saved) if m["priority"] in "EF")
    assert expected >= 1, "the captured window holds no E line; this spec would be vacuous"

    summary = json.loads(_snapshot_artifact(output, "snapshot-summary.json").read_text())
    assert summary["logs"]["errors"] == expected, (
        f"snapshot reports {summary['logs']['errors']} errors; the log it saved has "
        f"{expected} E/F lines"
    )

    logcat_calls = [call for call in toolchain.calls("adb") if "logcat" in call]
    assert any(
        call[i : i + 2] == ["-v", "threadtime"] for call in logcat_calls for i in range(len(call))
    ), f"the snapshot parses threadtime but did not ask for it: {logcat_calls}"


# ===========================================================================
# D4 -- a restarted app's crashed process is still the app.
# ===========================================================================

PACKAGE = "com.example.composefixture"


def _crash_loop_pids(recorded) -> list[str]:
    """PIDs of the three crashes, in order. The last is the one alive now."""
    pids: list[str] = []
    for m in _threadtime_lines(recorded.text("logcat_crash_loop")):
        if m["pid"] not in pids:
            pids.append(m["pid"])
    assert len(pids) >= 2, "the crash loop recording no longer holds a restart"
    return pids


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D4: log_monitor.py:316-333,366-367 filters to the restarted app's new pid only",
)
def test_d4_log_monitor_keeps_the_crashed_pid_of_a_restarted_app(toolchain, recorded):
    """D4: ``log_monitor.py:316-333,366-367``.

    ``--app`` resolves ONE pid with ``pidof`` and passes ``--pid=`` to logcat.
    After a crash and restart that is the new process, so the crash an agent
    is looking for -- logged by the process that died -- is filtered out of the
    very window it asked for. Recorded: logcat_crash_loop, three crashes under
    three pids.
    """
    pids = _crash_loop_pids(recorded)
    _snapshot_device(toolchain, recorded, "logcat_crash_loop", pid_answer=pids[-1])
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="500"
    )
    assert result.returncode == 0, result.stderr
    seen = {
        m["pid"] for m in _threadtime_lines("\n".join(json.loads(result.stdout)["sample_logs"]))
    }
    assert (
        pids[0] in seen
    ), f"the crashed process {pids[0]} is missing from the window; saw only {sorted(seen)}"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D4: app_state_capture.py:410-414 filters to the restarted app's new pid only",
)
def test_d4_app_state_capture_keeps_the_crashed_pid_of_a_restarted_app(
    toolchain, tmp_path, recorded
):
    """D4, the same shape in ``app_state_capture.py:410-414``."""
    pids = _crash_loop_pids(recorded)
    _snapshot_device(toolchain, recorded, "logcat_crash_loop", pid_answer=pids[-1])
    output = tmp_path / "snapshots"
    toolchain.run("app_state_capture.py", "--package", PACKAGE, "--output", str(output), "--json")
    saved = _snapshot_artifact(output, "app-logs.txt").read_text(encoding="utf-8")
    seen = {m["pid"] for m in _threadtime_lines(saved)}
    assert (
        pids[0] in seen
    ), f"the crashed process {pids[0]} is missing from the snapshot; saw only {sorted(seen)}"


def test_d4_the_fake_device_filters_by_pid_like_a_real_one(toolchain, recorded):
    """Anti-vacuity, unmarked: the stub honours ``--pid=`` the way logcat does.

    Without this the two D4 specs would pass against a fake that ignored the
    filter -- which is the bug they pin. Asked directly, so the control stays
    true whatever Inc 1 changes about how the scripts filter.
    """
    import subprocess

    pids = _crash_loop_pids(recorded)
    _snapshot_device(toolchain, recorded, "logcat_crash_loop", pid_answer=pids[-1])
    answer = subprocess.run(
        [str(toolchain.bin / "adb"), "logcat", "-d", f"--pid={pids[-1]}"],
        env=toolchain.env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert {m["pid"] for m in _threadtime_lines(answer.stdout)} == {pids[-1]}
    unfiltered = subprocess.run(
        [str(toolchain.bin / "adb"), "logcat", "-d"],
        env=toolchain.env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert {m["pid"] for m in _threadtime_lines(unfiltered.stdout)} == set(pids)


# ===========================================================================
# D5 + A5 -- one stall is one event and one cluster (session mode).
# ===========================================================================

SESSION_TIMEOUT_SECONDS = 20


def _session_summary(toolchain: FakeToolchain, recorded, stream: str) -> dict:
    """``--start`` -> stream drains -> ``--stop --json``, as an agent runs it.

    The stub logcat holds its stream open briefly after the recorded lines, as
    a live ``adb logcat`` does, so ``--start`` sees its worker register; then
    the stream ends, the worker drains it to EOF, finds no stream on its one
    re-spawn and exits, and ``--stop`` summarises everything it read. Step N's
    printed session id is step N+1's argument.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            # The recording is streamed ONCE. The worker re-spawns a stream
            # that ends, and a re-spawn reaching the same lines would count
            # one ANR per attempt; the re-spawn instead finds no stream at all
            # (default exit 1, silence), so the worker gives up and exits.
            # Paced one line per write, as a live stream arrives: unpaced,
            # the worker's select-on-a-buffered-pipe race drops the tail of
            # the recording on Linux, and D5 "passed" in CI for that reason.
            Rule(("logcat",), stdout=stream, hold=1.0, once=True, pace=0.2),
        ],
    )
    # 1 is the floor `env_int` allows here; 0 is clamped up to it.
    env = {"ANDROID_EMU_ANR_MAX_RESTARTS": "1"}
    started = toolchain.run("anr_watcher.py", "--start", **env)
    assert started.returncode == 0, started.stderr
    session_id = started.stdout.strip()

    deadline = time.time() + SESSION_TIMEOUT_SECONDS
    while time.time() < deadline:
        listing = toolchain.run("anr_watcher.py", "--list-sessions", "--json", **env)
        status = {s["session_id"]: s["status"] for s in json.loads(listing.stdout)}
        if status.get(session_id) not in ("pending", "running"):
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"worker for {session_id} never drained its stream")

    stopped = toolchain.run("anr_watcher.py", "--stop", session_id, "--all", "--json", **env)
    assert stopped.returncode == 0, stopped.stderr
    return json.loads(stopped.stdout)


def _jank_stream(recorded, frame_counts) -> str:
    """The recorded Choreographer line, once per count, with only the count changed."""
    template = next(
        line for line in recorded.lines("logcat_choreographer_jank") if "Skipped" in line
    )
    return "".join(
        re.sub(r"Skipped \d+ frames", f"Skipped {n} frames", template) + "\n" for n in frame_counts
    )


def test_the_session_harness_neither_drops_nor_duplicates_lines(toolchain, recorded):
    """Anti-vacuity, unmarked: N recorded lines in, N events out.

    Four lines, not one: both ways this harness has gone wrong lose or
    multiply lines, and neither shows on a single line. The first draft let
    the worker re-spawn its stream onto the same recording and counted every
    event twice; the second delivered the recording in one pipe write, and on
    Linux the worker dropped all but the first line -- which made D5 pass in
    CI for the wrong reason. Clusters are not asserted here: that is A5.
    """
    summary = _session_summary(toolchain, recorded, _jank_stream(recorded, (31, 45, 52, 120)))
    assert summary["event_count"] == 4, summary


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D5: common/anr_pipeline.py:258-266 -- the Reason: line becomes a second ANR",
)
def test_d5_a_broadcast_anr_is_one_event_and_one_cluster(toolchain, recorded):
    """D5: ``common/anr_pipeline.py:258-266``.

    ActivityManager reports one ANR as four lines under one tag: ``ANR in``,
    ``PID:``, ``Reason:``, ``ErrorId:`` (recorded: logcat_anr_broadcast). The
    follow-on branch classifies a line as an ANR if it holds "anr" and
    "reason:" -- and this Reason line names the intent action ``PROVOKE_ANR``
    -- so one stall arrives as two events in two clusters.
    """
    summary = _session_summary(toolchain, recorded, recorded.text("logcat_anr_broadcast"))
    assert summary["event_count"] == 1, [c["symbol_or_prefix"] for c in summary["clusters"]]
    assert len(summary["clusters"]) == 1, [c["symbol_or_prefix"] for c in summary["clusters"]]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A5: common/anr_pipeline.py:47 -- the frame count stays in the jank fingerprint",
)
def test_a5_one_janking_screen_is_one_cluster_whatever_it_dropped(toolchain, recorded):
    """A5: ``common/anr_pipeline.py:47`` redacts only integers of 4+ digits.

    So the frame count of ``Skipped N frames!`` stays in the normalised prefix,
    and the same screen dropping 31, 45, 52 and 120 frames fingerprints four
    ways. The lines are the recorded Choreographer line with only the count
    substituted (counts cannot be recorded to order; everything else can).
    """
    summary = _session_summary(toolchain, recorded, _jank_stream(recorded, (31, 45, 52, 120)))
    assert summary["event_count"] == 4, summary
    assert len(summary["clusters"]) == 1, [c["symbol_or_prefix"] for c in summary["clusters"]]


# ===========================================================================
# X6 -- anr_watcher's legacy modes report a failed adb as a failure.
# ===========================================================================


X6_MODES = {
    # argv: (json_mode, streaming)
    ("--watch", "--duration", "5"): (False, True),
    ("--watch", "--duration", "5", "--json"): (True, True),
    ("--since", "1m"): (False, False),
    ("--since", "1m", "--json"): (True, False),
}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "X6: anr_watcher.py:184-203,222-232 report success when adb fails; "
        "a device-not-found answer is device-unavailable, exit 3"
    ),
)
@pytest.mark.parametrize("argv", list(X6_MODES), ids=" ".join)
def test_x6_a_device_that_is_not_there_is_not_a_quiet_watch(
    argv, toolchain, recorded, recorded_anywhere
):
    """X6: ``anr_watcher.py:184-203`` (--watch) and ``:222-232`` (--since).

    Both read adb's stdout to EOF and return True without looking at how adb
    exited, so a device that is not there reads as a device that had no ANRs:
    exit 0, "No ANR/jank events detected." Here ``adb devices`` lists the
    recorded emulator, and every other call -- the logcat read -- answers with
    the recorded device-not-found message and exit 1.

    Under the shared boundary that is a refusal with exit 3, device
    unavailable: ``adb_exec`` classifies exactly this text as
    ``DeviceNotFoundError``. ``--watch`` is a streaming mode, so under
    ``--json`` the refusal object is the last line of stdout; ``--since`` is
    not, so the object is the whole of it.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule((), stderr=recorded_anywhere("adb_shell_device_not_found"), exit=1),
        ],
    )
    result = toolchain.run("anr_watcher.py", *argv)

    if not any("logcat" in call for call in toolchain.calls("adb")):
        pytest.fail(f"anr_watcher never asked for logcat: {toolchain.calls('adb')}")
    json_mode, streaming = X6_MODES[argv]
    assert_refusal(
        result, json_mode=json_mode, naming=("adb", "logcat"), status=3, streaming=streaming
    )


# ===========================================================================
# N9 -- --diff --json is JSON.
# ===========================================================================


def _never_a_constant(name: str):
    raise AssertionError(f"--diff --json emitted bare {name}, which is not JSON")


def _seed_session(store, events) -> str:
    from anr_watcher import AnrBuster

    from common.anr_pipeline import event_to_jsonl

    meta = store.create({})
    with open(store.events_path(meta.session_id), "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event_to_jsonl(event) + "\n")
    AnrBuster(store=store).stop(meta.session_id)
    return meta.session_id


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N9: common/anr_pipeline.py:742-749 -- --diff --json emits bare Infinity",
)
def test_n9_a_zero_duration_cluster_diffs_to_finite_json(toolchain, recorded):
    """N9: ``common/anr_pipeline.py:742-749`` -- a 0 -> N cluster is ``inf``.

    ``json.dumps`` writes ``float("inf")`` as the bare token ``Infinity``,
    which no strict JSON parser accepts, so ``--diff --json`` hands an agent a
    document it cannot read -- exactly when something got worse.

    The cluster is the recorded StrictMode violation header in both sessions.
    Its duration is zeroed on the PARSED event in the first: a line carrying
    ``~duration=0 ms`` fingerprints apart from one carrying 26 ms today (the
    first digit sits inside the 40-character prefix), so no line reaches this
    branch yet -- but a summary on disk can, and ``--diff`` reads summaries.
    """
    from common.anr_pipeline import build_normalised_event, parse_logcat_anr
    from common.anr_sessions import SessionStore

    header = next(
        line for line in recorded.lines("logcat_strictmode_violation") if "policy violation" in line
    )
    raw = parse_logcat_anr(header)
    assert raw is not None and raw["duration_ms"] > 0
    silent = build_normalised_event({**raw, "duration_ms": 0.0}, 0, current_ms=1000)
    loud = build_normalised_event(raw, 0, current_ms=1000)
    assert silent.fingerprint == loud.fingerprint

    store = SessionStore(base_dir=toolchain.home / "anr-sessions")
    before = _seed_session(store, [silent])
    after = _seed_session(store, [loud])

    result = toolchain.run("anr_watcher.py", "--diff", before, after, "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout, parse_constant=_never_a_constant)

    def numbers(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from numbers(value)
        elif isinstance(node, list):
            for value in node:
                yield from numbers(value)
        elif isinstance(node, float):
            yield node

    assert all(math.isfinite(n) for n in numbers(payload)), payload
    assert silent.fingerprint in result.stdout, "the 0 -> N cluster vanished from the diff"

    text = toolchain.run("anr_watcher.py", "--diff", before, after)
    assert text.returncode == 0, text.stderr


# ===========================================================================
# N12 -- a snapshot does not land in the agent's working directory.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N12: app_state_capture.py:534 -- the default --output is the caller's cwd",
)
def test_n12_app_state_capture_writes_nothing_into_the_cwd(toolchain, tmp_path, recorded):
    """N12: ``app_state_capture.py:534`` (and SKILL.md:525).

    ``--output`` defaults to the relative ``app-state-snapshots``, so every run
    without it writes into whatever directory the agent happens to be in --
    the planning probe left one in this repository's root, where it is not
    gitignored. Where the default SHOULD be is Inc 1's decision; this asserts
    only that it is not the caller's cwd.
    """
    _snapshot_device(toolchain, recorded, "logcat_threadtime", pid_answer=D3_PID)
    cwd = tmp_path / "agent-cwd"
    cwd.mkdir()
    toolchain.run("app_state_capture.py", "--package", PACKAGE, "--json", cwd=cwd)

    assert toolchain.calls("adb"), "the capture never reached the device"
    left = sorted(p.name for p in cwd.iterdir())
    assert not left, f"app_state_capture wrote {left} into the caller's working directory"
