"""Red specifications for v0.8.0 Inc 1 -- "diagnostics stop lying".

Each test here states what an agent must see once a finding is fixed, and each
was run unmarked against ``main`` at c2c7a8a and watched fail before it was
pinned ``xfail(strict=True)`` under its finding ID. The fix for a finding
deletes its marker in the same commit; strict mode turns a fixed-but-still-
marked spec red, so a marker cannot outlive its defect.

Every marker also carries ``raises=AssertionError``. Strict mode rejects an
unexpected PASS but accepts any way of failing, so on its own it would excuse
a JSON decode error or a missing snapshot file as "still the pinned defect".
Narrowed to the assertion -- and with every harness prerequisite (an artifact
that must exist, a tool that must have been asked, a recording that must still
hold what the spec needs) failing through ``pytest.fail`` instead -- a spec
that breaks some other way fails loudly. The unmarked controls beside each
group check the harness itself.

**What these assert, and what they deliberately do not.** The specs drive the
real CLI as a subprocess, or the ANR pipeline over recorded lines where that
is the only deterministic path (D5, A5), and read what an agent would read:
exit status, stdout, the JSON, the files a snapshot leaves behind. A refusal
is checked against the shared boundary's contract (PR #18), through
``fake_toolchain.assert_refusal``. Wording and implementation are left to the
fix -- the test is the contract, not the code. The one place a command sent to
the device is checked is D1's unmarked floor: the fake ignores ``*:P``
priority filters, so only the command can show a device dropping warnings.

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


def _recorded_counts(recorded_gradle) -> tuple[int, int]:
    """(failed, passed) across the run's recorded JUnit suites, read off the XML.

    From the suites' own attributes, not typed here: the error case alone is
    failures="2"; the run's three suites together are 3 failed, 1 passed,
    1 skipped -- the same "5 tests completed, 3 failed, 1 skipped" the
    recorded log prints.
    """
    import xml.etree.ElementTree as ET

    failed = passed = 0
    for _, fixture in JUNIT_SUITES:
        suite = ET.fromstring(recorded_gradle(fixture))
        tests, skipped = int(suite.get("tests", 0)), int(suite.get("skipped", 0))
        bad = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        failed += bad
        passed += tests - bad - skipped
    return failed, passed


def _count(text: str, word: str) -> int | None:
    """The first count printed for ``word``: "3 failed", "1/5 passed" or "Failed: 3"."""
    match = re.search(rf"(\d+)(?:/\d+)?\s+{word}\b|\b{word}:\s*(\d+)", text, re.IGNORECASE)
    return int(match.group(1) or match.group(2)) if match else None


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
    agent drills into later, and in text and verbose a verdict line that says
    FAIL/FAILED with the failed and passed counts the recorded XML holds. A
    per-test breakdown listing PASSED tests beside FAILED ones is correct
    output, so "PASS" appearing somewhere is not prohibited (review of PR #20).
    """
    project = _ignore_failures_project(toolchain, tmp_path, recorded_gradle)
    result = toolchain.run(
        "build_and_test.py", "--project", str(project), "--test", *X7_MODES[mode]
    )

    assert (
        result.returncode != 0
    ), f"{mode}: three JUnit failures and exit {result.returncode}.\n{result.stdout[-600:]}"
    failed, passed = _recorded_counts(recorded_gradle)
    if mode == "json":
        payload = json.loads(result.stdout)
        assert payload["success"] is False, payload
        assert payload["test_results"]["failed"] == failed, payload["test_results"]
        assert payload["test_results"]["passed"] == passed, payload["test_results"]
        from gradle import BuildResultCache

        cache = BuildResultCache(
            cache_dir=str(toolchain.home / ".android-emulator-skill" / "cache")
        )
        cached = cache.get(payload["build_id"])
        if cached is None:
            pytest.fail(f"the build id printed under --json does not resolve: {payload}")
        assert cached["success"] is False, "--get-errors would drill into a build cached as passed"
    else:
        verdict = next((line for line in result.stdout.splitlines() if line.strip()), "")
        assert re.search(
            r"\bfail(ed)?\b", verdict, re.IGNORECASE
        ), f"the verdict line does not say FAILED: {verdict!r}"
        assert _count(result.stdout, "failed") == failed, result.stdout
        assert _count(result.stdout, "passed") == passed, result.stdout


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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "N6: gradle/results.py:43-51 -- with no report from this run, the verdict comes "
        "from an old one, or from nothing"
    ),
)
def test_n6_a_run_that_wrote_no_results_is_not_a_pass(toolchain, tmp_path, recorded_gradle):
    """N6, the no-results case: filtering everything out must not read as a pass.

    Gradle exits 0 and writes no JUnit XML this run -- what an UP-TO-DATE test
    task does -- while a passing report from an hour ago (recorded
    junit_xml_passing_case) sits in the results directory. Today that old
    report is the verdict: one test, passed. With N6's filter alone, the
    verdict would come from nothing: "0 tests, 0 failed", success -- X7's
    shape, a pass no test produced (review of PR #20). Either way the run has
    no results of its own, and must not report success.
    """
    results = _results_dir(tmp_path / "proj")
    stale = results / "TEST-com.example.PassingTest.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text(recorded_gradle("junit_xml_passing_case"), encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(stale, (an_hour_ago, an_hour_ago))

    project = _gradle_project(toolchain, tmp_path, Rule(("testDebugUnitTest",), exit=0))
    result = toolchain.run("build_and_test.py", "--project", str(project), "--test", "--json")
    if not toolchain.calls("gradlew"):
        pytest.fail("gradlew was never run")

    document = result.stdout.strip()
    assert document.startswith("{"), f"--json printed no object: {document[:200]!r}"
    payload = json.loads(document)
    assert payload.get("success") is False, f"a run with no test results passed: {payload}"
    assert result.returncode != 0, f"a run with no test results exited 0: {payload}"


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
@pytest.mark.parametrize("severity", ["warn", "error,warn", "bogus"])
def test_d1_an_unknown_severity_name_is_a_usage_error(severity, toolchain, recorded):
    """D1: ``log_monitor.py:679-681`` / ``common/logcat.py:117-137``.

    ``warn`` is not a severity this CLI knows (its help lists error, warning,
    info, debug, verbose). ``min_priority_for`` drops an unknown name, so
    ``warn`` alone sends no ``*:`` filter at all and ``error,warn`` sends
    ``*:E``; then the post-parse check at ``log_monitor.py:238`` discards every
    line whose severity is not literally in the list. Both report a calm,
    successful capture with the warnings silently gone. An unknown name must be
    refused as a usage error (exit 2) naming the accepted set -- and before the
    device is asked for logs.

    The vocabulary is the documented one: ``--help`` lists
    ``error,warning,info,debug,verbose``, so ``warning`` is canonical and
    ``warn`` is unknown, as the review of PR #20 confirmed. Accepting ``warn``
    as an alias would be a deliberate change to this spec, not a way to pass
    it. ``bogus`` is the case no vocabulary decision can rescue.
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

    # Every recorded W line, as log_monitor prints a warning: "[Tag] message".
    # Identical messages are deduplicated by design, so this is a set; what it
    # must not do is drop a message (review of PR #20: tags alone let one of
    # two messages under a tag go missing unnoticed).
    expected = set()
    for line in recorded.lines("logcat_threadtime"):
        m = _THREADTIME.match(line)
        if m and m["priority"] == "W":
            tag, _, message = line[m.end() :].partition(":")
            expected.add(f"[{tag.strip()}] {message.strip()}")
    if not expected:
        pytest.fail("the recording has no W lines; this floor would be vacuous")
    missing = sorted(expected - set(payload["warnings"]))
    assert not missing, f"warnings were dropped: {missing}"

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


# The recorded `adb exec-out screencap -p` of the emulator-api35 screen, which
# a stub `adb pull` hands back so that a snapshot can succeed end to end.
RECORDED_SCREENSHOT = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "recorded"
    / "emulator-api35"
    / "demo_mode_clock_0941.png"
)


def _snapshot_device(toolchain, recorded, logcat: str, pid_answer: str) -> None:
    """A device on which a whole app_state_capture snapshot succeeds.

    Each component is answered from the corpus: the screenshot is the recorded
    ``screencap -p`` PNG delivered through ``pull``, the hierarchy is
    ``uiautomator_dump_raw`` (``exec-out uiautomator dump /dev/tty`` exactly as
    the device frames it), density is ``wm_density_physical``, and ``logcat``
    is the transcript the spec chooses, filtered by ``--pid`` as the device
    filters it. ``dumpsys`` and ``getprop`` get no rule and answer with
    silence: the snapshot records those fields as unknown, which is not a
    failure. ``pidof`` answers ``pid_answer``, a pid taken from the recording
    the spec is reading.
    """
    if not RECORDED_SCREENSHOT.is_file():
        pytest.fail(f"recorded screenshot missing: {RECORDED_SCREENSHOT}")
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("exec-out", "uiautomator"), stdout=recorded.text("uiautomator_dump_raw")),
            Rule(("shell", "screencap"), exit=0),
            Rule(("pull",), pull_from=str(RECORDED_SCREENSHOT)),
            Rule(("shell", "pidof"), stdout=pid_answer),
            Rule(("shell", "wm", "density"), stdout=recorded.text("wm_density_physical")),
            Rule(("logcat",), stdout=logcat, filter_pid=True),
        ],
    )


def _snapshot_artifact(output: Path, name: str) -> Path:
    """The one ``name`` a capture left under ``output`` -- a harness prerequisite.

    ``pytest.fail``, not an assertion: a capture that left no artifact is not
    the defect a D3/D4 spec pins, and ``raises=AssertionError`` must not be
    able to excuse it as one.
    """
    found = sorted(output.rglob(name))
    if len(found) != 1:
        pytest.fail(f"expected one {name} under {output}, found {found}")
    return found[0]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D3: app_state_capture.py:85-91 counts errors from prose, not the priority column",
)
def test_d3_error_count_is_the_priority_column(toolchain, tmp_path, recorded):
    """D3: ``app_state_capture.py:85-91`` counts errors from prose.

    A line counts as an error if its text contains "error" or "exception",
    whatever its priority. The count in the snapshot must equal the number of
    E/F lines in the log the snapshot itself saved -- the window it read, as
    recorded. How the fix gets there (which ``-v`` format it asks for, which
    parser reads it) is not pinned: the review of PR #20 rejected the first
    version's exact ``-v threadtime`` argv check as an implementation
    constraint. ``:420`` passing ``fmt=None`` remains the likely cause.
    """
    _snapshot_device(toolchain, recorded, recorded.text("logcat_threadtime"), pid_answer=D3_PID)
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
    if expected < 1:
        pytest.fail("the captured window holds no E line; this spec would be vacuous")

    summary = json.loads(_snapshot_artifact(output, "snapshot-summary.json").read_text())
    assert summary["logs"]["errors"] == expected, (
        f"snapshot reports {summary['logs']['errors']} errors; the log it saved has "
        f"{expected} E/F lines"
    )


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
    if len(pids) < 2:
        pytest.fail("the crash loop recording no longer holds a restart")
    return pids


def _device_buffer(recorded) -> tuple[str, set[str]]:
    """What the device's buffer holds: other processes' logging, then the crash loop.

    Two recordings, concatenated in time order (logcat_threadtime is 08-31,
    logcat_crash_loop 09-01), so the window an agent asks for holds both the
    app's three crashed processes and system processes that are not the app.
    Returns the transcript and the pids that are NOT the app.
    """
    unrelated = {m["pid"] for m in _threadtime_lines(recorded.text("logcat_threadtime"))}
    app = set(_crash_loop_pids(recorded))
    if not unrelated or unrelated & app:
        pytest.fail(f"the two recordings no longer separate app {app} from others {unrelated}")
    return recorded.text("logcat_threadtime") + recorded.text("logcat_crash_loop"), unrelated


def _assert_the_apps_window(seen: set[str], pids: list[str], unrelated: set[str]) -> None:
    """The crashed process is in; nothing that is not the app is."""
    assert (
        pids[0] in seen
    ), f"the crashed process {pids[0]} is missing from the window; saw only {sorted(seen)}"
    leaked = sorted(seen & unrelated)
    assert not leaked, f"processes that are not the app leaked into its window: {leaked}"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D4: log_monitor.py:316-333,366-367 filters to the restarted app's new pid only",
)
def test_d4_log_monitor_keeps_the_crashed_pid_of_a_restarted_app(toolchain, tmp_path, recorded):
    """D4: ``log_monitor.py:316-333,366-367``.

    ``--app`` resolves ONE pid with ``pidof`` and passes ``--pid=`` to logcat.
    After a crash and restart that is the new process, so the crash an agent
    is looking for -- logged by the process that died -- is filtered out of the
    very window it asked for. Recorded: logcat_crash_loop, three crashes under
    three pids.

    Both directions are pinned. Dropping the package filter altogether would
    bring the crashed process back, along with every other process on the
    device; the window must hold the app's processes, old and new, and none
    of the others (review of PR #20).
    """
    pids = _crash_loop_pids(recorded)
    buffer, unrelated = _device_buffer(recorded)
    _snapshot_device(toolchain, recorded, buffer, pid_answer=pids[-1])
    saved = tmp_path / "logs"
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--output", str(saved), "--json"
    )
    if result.returncode != 0:
        pytest.fail(f"log_monitor failed outright:\n{result.stderr}")
    logs = sorted(saved.glob("*.log"))
    if len(logs) != 1:
        pytest.fail(f"expected one saved log under {saved}, found {logs}")
    seen = {m["pid"] for m in _threadtime_lines(logs[0].read_text(encoding="utf-8"))}
    _assert_the_apps_window(seen, pids, unrelated)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D4: app_state_capture.py:410-414 filters to the restarted app's new pid only",
)
def test_d4_app_state_capture_keeps_the_crashed_pid_of_a_restarted_app(
    toolchain, tmp_path, recorded
):
    """D4, the same shape in ``app_state_capture.py:410-414``, both directions."""
    pids = _crash_loop_pids(recorded)
    buffer, unrelated = _device_buffer(recorded)
    _snapshot_device(toolchain, recorded, buffer, pid_answer=pids[-1])
    output = tmp_path / "snapshots"
    toolchain.run(
        "app_state_capture.py",
        "--package",
        PACKAGE,
        "--output",
        str(output),
        "--log-lines",
        "100000",
        "--json",
    )
    saved = _snapshot_artifact(output, "app-logs.txt").read_text(encoding="utf-8")
    seen = {m["pid"] for m in _threadtime_lines(saved)}
    _assert_the_apps_window(seen, pids, unrelated)


def test_d4_the_fake_device_filters_by_pid_like_a_real_one(toolchain, recorded):
    """Anti-vacuity, unmarked: the stub honours ``--pid=`` the way logcat does.

    Without this the two D4 specs would pass against a fake that ignored the
    filter -- which is the bug they pin. Asked directly, so the control stays
    true whatever Inc 1 changes about how the scripts filter. Unfiltered, the
    buffer holds the app AND the unrelated processes the exclusion half needs.
    """
    import subprocess

    pids = _crash_loop_pids(recorded)
    buffer, unrelated = _device_buffer(recorded)
    _snapshot_device(toolchain, recorded, buffer, pid_answer=pids[-1])

    def ask(*argv: str) -> set[str]:
        answer = subprocess.run(
            [str(toolchain.bin / "adb"), "logcat", "-d", *argv],
            env=toolchain.env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return {m["pid"] for m in _threadtime_lines(answer.stdout)}

    assert ask(f"--pid={pids[-1]}") == {pids[-1]}
    assert ask() == set(pids) | unrelated


# ===========================================================================
# D5 + A5 -- one stall is one event and one cluster.
#
# Checked where it is deterministic: the pipeline over recorded lines, and the
# CLI through a FINITE mode (`--since`, a `logcat -d` dump read whole). The
# first version drove the session worker over a paced live stream; the review
# of PR #20 showed the worker's select()-on-a-buffered-pipe read
# (anr_watcher.py:659-670) keeps lines back depending on timing -- stopping
# while the writer is open retained one line of four -- so no sleep can make
# that path isolate a classification defect. That read is its own defect.
# ===========================================================================


def _pipeline(lines: list[str]):
    """Recorded lines -> (events, clusters), the composition both readers use.

    ``anr_watcher``'s ``--since``/``--watch`` reader (``_handle_line``) and its
    session worker (``_read_stream_into_events``) both run each line through
    ``parse_logcat_anr`` and then ``build_normalised_event``; ``--stop``
    clusters with ``cluster_events``. If D5's fix introduces a multi-line
    entry point, both readers must go through it, and this helper must be
    pointed at it too -- the assertions below do not change.
    """
    from common import anr_pipeline as p

    events = []
    for line in lines:
        raw = p.parse_logcat_anr(line)
        if raw is None:
            continue
        event = p.build_normalised_event(raw, session_start_ms=0, current_ms=1000)
        if event is not None:
            events.append(event)
    return events, p.cluster_events(events)


def _jank_lines(recorded, frame_counts, pid: str | None = None) -> list[str]:
    """The recorded Choreographer line once per count, with only the count changed.

    ``pid`` substitutes the PID and TID columns as well -- a second process
    janking, which is a different site. Everything else stays the recording.
    """
    template = next(
        (line for line in recorded.lines("logcat_choreographer_jank") if "Skipped" in line), None
    )
    if template is None:
        pytest.fail("logcat_choreographer_jank holds no Skipped line")
    lines = []
    for n in frame_counts:
        line = re.sub(r"Skipped \d+ frames", f"Skipped {n} frames", template)
        if pid is not None:
            line = re.sub(r"^(\S+ \S+\s+)\d+(\s+)\d+", rf"\g<1>{pid}\g<2>{pid}", line)
        lines.append(line)
    return lines


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D5: common/anr_pipeline.py:258-266 -- the Reason: line becomes a second ANR",
)
def test_d5_a_broadcast_anr_is_one_event_and_one_cluster(recorded):
    """D5: ``common/anr_pipeline.py:258-266``.

    ActivityManager reports one ANR as four lines under one tag: ``ANR in``,
    ``PID:``, ``Reason:``, ``ErrorId:`` (recorded: logcat_anr_broadcast). The
    follow-on branch classifies a line as an ANR if it holds "anr" and
    "reason:" -- and this Reason line names the intent action ``PROVOKE_ANR``
    -- so one stall arrives as two events in two clusters.
    """
    events, clusters = _pipeline(recorded.lines("logcat_anr_broadcast"))
    assert len(events) == 1, [e.raw_message for e in events]
    assert len(clusters) == 1, [c.symbol_or_prefix for c in clusters]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="D5: common/anr_pipeline.py:258-266 -- --since prints one broadcast ANR twice",
)
def test_d5_since_reports_a_broadcast_anr_once(toolchain, recorded):
    """D5 through the CLI, in the finite mode: ``--since 1m --json``.

    ``--since`` reads a ``logcat -d`` dump to the end and prints one JSON line
    per event, so the count an agent sees is the count of lines.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("logcat",), stdout=recorded.text("logcat_anr_broadcast")),
        ],
    )
    result = toolchain.run("anr_watcher.py", "--since", "1m", "--json")
    if result.returncode != 0 or not any("logcat" in c for c in toolchain.calls("adb")):
        pytest.fail(f"--since did not read the dump:\n{result.stdout}{result.stderr}")

    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(events) == 1, [event.get("message") for event in events]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A5: common/anr_pipeline.py:47 -- the frame count stays in the jank fingerprint",
)
def test_a5_one_janking_screen_is_one_cluster_whatever_it_dropped(recorded):
    """A5: ``common/anr_pipeline.py:47`` redacts only integers of 4+ digits.

    So the frame count of ``Skipped N frames!`` stays in the normalised prefix,
    and the same process dropping 31, 45, 52 and 120 frames fingerprints four
    ways. The lines are the recorded Choreographer line with only the count
    substituted (counts cannot be recorded to order; everything else can).
    """
    events, clusters = _pipeline(_jank_lines(recorded, (31, 45, 52, 120)))
    assert len(events) == 4, [e.raw_message for e in events]
    assert len(clusters) == 1, [c.symbol_or_prefix for c in clusters]


def test_a5_two_janking_processes_stay_two_sites(recorded):
    """A5's other direction, unmarked: unrelated jank is not one cluster.

    Collapsing EVERY Choreographer line into one fingerprint also satisfies
    the spec above, and would report two apps' jank as one site (review of
    PR #20). Here two processes -- the recorded line, and the same line with
    its PID/TID columns substituted -- jank twice each; no fingerprint may
    hold both. Green today only because the counts differ; the A5 fix must
    keep it green by telling the processes apart, not by keeping the counts.
    """
    first = _jank_lines(recorded, (31, 45))
    pid = first[0].split()[2]
    other = str(int(pid) + 1)
    events, _ = _pipeline(first + _jank_lines(recorded, (52, 120), pid=other))
    if len(events) != 4:
        pytest.fail(f"expected four jank events, got {len(events)}")

    sites: dict[str, set[int]] = {}
    for event in events:
        sites.setdefault(event.fingerprint, set()).add(event.pid)
    merged = {fp: pids for fp, pids in sites.items() if len(pids) > 1}
    assert not merged, f"jank from different processes shares a cluster: {merged}"


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
    if raw is None or raw["duration_ms"] <= 0:
        pytest.fail(f"the recorded StrictMode header no longer parses with a duration: {raw}")
    silent = build_normalised_event({**raw, "duration_ms": 0.0}, 0, current_ms=1000)
    loud = build_normalised_event(raw, 0, current_ms=1000)
    if silent.fingerprint != loud.fingerprint:
        pytest.fail("the two events no longer share a cluster; the 0 -> N case is not built")

    store = SessionStore(base_dir=toolchain.home / "anr-sessions")
    before = _seed_session(store, [silent])
    after = _seed_session(store, [loud])

    result = toolchain.run("anr_watcher.py", "--diff", before, after, "--json")
    if result.returncode != 0:
        pytest.fail(f"--diff did not run:\n{result.stderr}")
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


SNAPSHOT_ARTIFACTS = {"screenshot.png", "ui-hierarchy.json", "app-logs.txt"}


def _captured_snapshot(result, cwd: Path) -> Path:
    """The snapshot directory a successful ``--json`` capture reported, resolved."""
    assert result.returncode == 0, f"the capture failed:\n{result.stdout}{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload.get("success") is True, payload
    snapshot = Path(payload["snapshot_path"])
    return (snapshot if snapshot.is_absolute() else cwd / snapshot).resolve()


def _assert_complete_snapshot(snapshot: Path) -> None:
    assert snapshot.is_dir(), f"the reported snapshot path does not exist: {snapshot}"
    summary = json.loads((snapshot / "snapshot-summary.json").read_text(encoding="utf-8"))
    missing = [name for name in summary["artifacts"] if not (snapshot / name).is_file()]
    assert not missing, f"artifacts listed but absent: {missing}"
    assert set(summary["artifacts"]) >= SNAPSHOT_ARTIFACTS, summary["artifacts"]


def test_the_snapshot_device_supports_a_complete_capture(toolchain, tmp_path, recorded):
    """Control, unmarked: with ``--output`` given, the capture succeeds whole.

    So the N12 spec below fails only on WHERE the snapshot goes. Every
    artifact comes from the corpus; see ``_snapshot_device``.
    """
    _snapshot_device(toolchain, recorded, recorded.text("logcat_threadtime"), pid_answer=D3_PID)
    cwd = tmp_path / "agent-cwd"
    cwd.mkdir()
    output = tmp_path / "explicit"
    result = toolchain.run(
        "app_state_capture.py", "--package", PACKAGE, "--output", str(output), "--json", cwd=cwd
    )
    snapshot = _captured_snapshot(result, cwd)
    _assert_complete_snapshot(snapshot)
    assert output.resolve() in snapshot.parents
    assert not list(cwd.iterdir())


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="N12: app_state_capture.py:534 -- the default --output is the caller's cwd",
)
def test_n12_a_default_snapshot_succeeds_outside_the_callers_cwd(toolchain, tmp_path, recorded):
    """N12: ``app_state_capture.py:534`` (and SKILL.md:525).

    ``--output`` defaults to the relative ``app-state-snapshots``, so every run
    without it writes into whatever directory the agent happens to be in --
    the planning probe left one in this repository's root, where it is not
    gitignored. Where the default SHOULD be is Inc 1's decision. What it must
    be: a capture that still SUCCEEDS, reports where it went, and put its
    artifacts there, outside the caller's cwd -- which stays empty. A fix
    that simply refuses to capture without ``--output`` writes nothing into
    cwd too, and must not pass (review of PR #20).
    """
    _snapshot_device(toolchain, recorded, recorded.text("logcat_threadtime"), pid_answer=D3_PID)
    cwd = tmp_path / "agent-cwd"
    cwd.mkdir()
    result = toolchain.run("app_state_capture.py", "--package", PACKAGE, "--json", cwd=cwd)

    snapshot = _captured_snapshot(result, cwd)
    _assert_complete_snapshot(snapshot)
    assert (
        cwd.resolve() not in snapshot.parents
    ), f"the snapshot went into the caller's working directory: {snapshot}"
    left = sorted(p.name for p in cwd.iterdir())
    assert not left, f"app_state_capture wrote {left} into the caller's working directory"
