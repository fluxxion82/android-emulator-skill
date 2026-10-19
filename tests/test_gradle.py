"""Device-free tests for the gradle/ build subpackage.

These exercise the PURE logic only — JUnit XML parsing, Gradle build-output
parsing, output formatting, config round-trip, and the cache wrapper. No real
gradlew is invoked and no device is required. subprocess is monkeypatched where a
build run is involved.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from xml.etree import ElementTree

import pytest

from gradle import (
    BuildResultCache,
    OutputFormatter,
    aggregate_test_results,
    find_test_result_files,
    parse_build_output,
    parse_junit_xml,
)
from gradle.config import Config

# --- JUnit XML parsing -------------------------------------------------------
#
# Every input here is a RECORDED Gradle report, written by a real `test` task
# run against tests/fixtures/scaffold/compile/junit-error. Three suites come out
# of that one run, each carrying a different shape:
#
#   junit_xml_error_case    2 tests, both failed -- their @Before threw
#   junit_xml_passing_case  1 test, green, a bare self-closing <testcase>
#   junit_xml_skipped_case  2 tests, 1 skipped via @Ignore, 1 failed
#
# They replace two hand-written literals (JUNIT_XML, JUNIT_XML_TESTSUITES), and
# recording them corrected an assumption both encoded: the literal modelled a
# non-assertion exception as `<error message="ArithmeticException: / by zero">`
# and asserted `errors == 1`. Gradle's XML writer does not produce that element.
# Measured on 8.13 by two independent routes -- an IllegalStateException thrown
# from @Before, and an UnsupportedOperationException thrown from a test body,
# neither an AssertionError -- and both are written as `<failure type="...">`
# with `errors="0"` on the suite. No route tried here makes Gradle emit an
# `<error>` element at all.


def test_parse_junit_xml_counts(recorded_gradle):
    """Counts, against a suite whose every test failed in setup."""
    result = parse_junit_xml(recorded_gradle("junit_xml_error_case"))
    assert result["total"] == 2
    assert result["passed"] == 0
    assert result["failed"] == 2
    assert result["skipped"] == 0
    # Not a stand-in for "untested": Gradle wrote errors="0" here, and the test
    # below shows it does so even for a throw that is not an assertion failure.
    assert result["errors"] == 0


def test_parse_junit_xml_counts_a_green_suite(recorded_gradle):
    """A parser that called everything failed would pass the tests above."""
    result = parse_junit_xml(recorded_gradle("junit_xml_passing_case"))
    assert result["total"] == 1
    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["failed_tests"] == []


def test_parse_junit_xml_counts_a_skipped_test(recorded_gradle):
    """A real <skipped/>, from an @Ignore'd test rather than a typed tag."""
    result = parse_junit_xml(recorded_gradle("junit_xml_skipped_case"))
    assert result["total"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 1
    assert result["passed"] == 0


def test_gradle_writes_failure_even_for_a_non_assertion_exception(recorded_gradle):
    """The assumption the deleted literal encoded, corrected against reality.

    `JUNIT_XML` modelled an ArithmeticException as an `<error>` element and
    asserted the parser counted one error. The recorded suite throws an
    UnsupportedOperationException straight out of a test body -- as clearly
    "not an assertion failure" as the literal's case was -- and Gradle writes a
    `<failure>`, leaving `errors="0"` on the suite.
    """
    xml = recorded_gradle("junit_xml_skipped_case")
    assert "UnsupportedOperationException" in xml
    assert "<failure" in xml
    assert "<error" not in xml
    assert 'errors="0"' in xml


def test_parse_junit_xml_failed_names_and_messages(recorded_gradle):
    """Failed-test names are class-qualified and carry the exception message."""
    result = parse_junit_xml(recorded_gradle("junit_xml_error_case"))
    names = {t["test_name"] for t in result["failed_tests"]}
    assert names == {
        "com.example.BeforeThrowsTest.erroredBySetup",
        "com.example.BeforeThrowsTest.alsoErroredBySetup",
    }

    by_name = {t["test_name"]: t["failure_message"] for t in result["failed_tests"]}
    assert (
        by_name["com.example.BeforeThrowsTest.erroredBySetup"]
        == "java.lang.IllegalStateException: fixture setup failed on purpose"
    )


def test_parse_junit_xml_testsuites_root(recorded_gradle):
    """The multi-suite root, built by wrapping two RECORDED suites.

    Gradle writes one `<testsuite>` document per test class and never emits a
    `<testsuites>` wrapper, so this root cannot be recorded from it. The branch
    still needs covering -- `parse_junit_xml` handles the shape, which other
    runners and CI aggregators do emit.

    So the two suite elements are real, lifted verbatim out of the recordings;
    only the wrapper tag is added, and its spelling comes from the JUnit schema
    rather than from a guess about what some tool prints. This is CLAUDE.md's
    documented "text built by transforming recorded lines", not a transcript
    somebody typed.
    """

    def _suite(name: str) -> str:
        body = recorded_gradle(name)
        return body[body.index("<testsuite ") :]

    # Only the wrapper tags are written here, and they are deliberately the
    # whole of what is written: 12 and 13 characters, well under the ratchet's
    # transcript threshold, because nothing longer than a tag name is being
    # asserted about. The XML declaration is dropped rather than retyped --
    # ElementTree does not need one, and reproducing it would have meant
    # hand-writing a line no tool emitted in this position.
    document = (
        "<testsuites>"
        + _suite("junit_xml_passing_case")
        + _suite("junit_xml_error_case")
        + "</testsuites>"
    )

    result = parse_junit_xml(document)
    assert result["total"] == 3, "both suites must be walked, not just the first"
    assert result["passed"] == 1
    assert result["failed"] == 2
    assert {t["test_name"] for t in result["failed_tests"]} == {
        "com.example.BeforeThrowsTest.erroredBySetup",
        "com.example.BeforeThrowsTest.alsoErroredBySetup",
    }


def test_the_error_branch_works_for_xml_gradle_does_not_write(recorded_gradle):
    """`parse_junit_xml` counts <error> elements. Nothing here produces one.

    Gradle's writer represents both a throwing @Before and a non-assertion
    exception as `<failure>`, leaving `errors="0"` (the two tests above measure
    that). So the parser's `errors` branch describes JUnit XML from some OTHER
    producer, and no recording in this repo can exercise it.

    Rather than leave the branch untested or hand-write a whole document, the
    element is grafted onto a REAL recorded suite: everything around it is
    Gradle's own output, and the single substituted tag is the thing under test.
    The distinction matters for anyone reading this later -- an `<error>` in the
    corpus would otherwise look like evidence that Gradle emits one.
    """
    recorded = recorded_gradle("junit_xml_skipped_case")
    assert "<error" not in recorded, "Gradle started emitting <error>; re-check F8"

    foreign = recorded.replace("<failure ", "<error ").replace("</failure>", "</error>")
    result = parse_junit_xml(foreign)

    assert result["errors"] == 1, "the <error> branch does not count what it claims to"
    assert result["total"] == 2
    assert result["skipped"] == 1
    assert len(result["failed_tests"]) == 1, "an errored test is still a failed test"


def test_parse_junit_xml_empty_and_malformed():
    assert parse_junit_xml("")["total"] == 0
    assert parse_junit_xml("not xml <<<")["total"] == 0


# --- the same parser, against XML a real Gradle run produced -----------------
#
# The literals above are hand-written, and recording the real thing showed one
# of their assumptions to be wrong: they model a setup exception as an
# `<error>` element, and Gradle does not emit one. See
# tests/fixtures/scaffold/compile/junit-error, whose @Before throws.


def test_parse_junit_xml_reads_a_real_gradle_report(recorded_gradle):
    """The counts must come out right on XML nobody wrote by hand."""
    result = parse_junit_xml(recorded_gradle("junit_xml_error_case"))

    assert result["total"] == 2, "both testcases are listed even though neither body ran"
    assert result["failed"] == 2
    assert result["passed"] == 0


def test_a_throwing_setup_is_reported_as_failure_not_error(recorded_gradle):
    """MEASURED: Gradle writes <failure> for a @Before that throws.

    The obvious guess — that an exception outside an assertion becomes
    `<error>` — is wrong, and the hand-written sample in this file encodes the
    guess. A parser that located setup failures by looking for `<error>` would
    find none here and report the class as green.
    """
    xml = recorded_gradle("junit_xml_error_case")
    assert 'errors="0"' in xml, "the recording no longer supports this claim"
    assert "<failure" in xml
    assert "<error" not in xml

    result = parse_junit_xml(xml)
    assert result["errors"] == 0
    names = {t["test_name"] for t in result["failed_tests"]}
    assert names == {
        "com.example.BeforeThrowsTest.erroredBySetup",
        "com.example.BeforeThrowsTest.alsoErroredBySetup",
    }
    for test in result["failed_tests"]:
        assert "fixture setup failed on purpose" in test["failure_message"]


def test_ignore_failures_makes_the_build_log_claim_success(recorded_gradle):
    """Why the XML has to be read at all: the log says the build succeeded.

    With `ignoreFailures = true` the same run that produced three failing tests
    prints BUILD SUCCESSFUL and exits 0. Anything deciding test outcome from
    the build log or the exit status is told everything passed.
    """
    log = recorded_gradle("gradle_junit_error_ignorefailures")
    assert "BUILD SUCCESSFUL" in log
    assert "5 tests completed, 3 failed, 1 skipped" in log
    assert "BUILD FAILED" not in log

    # Two tests failed, and the build-output parser reports no failed task and
    # no error, because from the log's point of view nothing went wrong. The
    # test outcome is recoverable only from the JUnit XML.
    parsed = parse_build_output(log, "")
    assert parsed["failed_tasks"] == []
    assert parsed["errors"] == []


def test_a_configuration_failure_reports_a_file_and_line(recorded_gradle):
    """D8: `* Where:` is the only Gradle failure block that carries a location.

    Every other recorded failure has no `* Where:` section at all, so a parser
    that only knows `* What went wrong:` discards file and line on exactly the
    failures that have them.
    """
    log = recorded_gradle("gradle_where_block")
    assert "* Where:" in log
    assert "build.gradle' line: 11" in log
    assert "Could not find method implementaion()" in log

    for other in ("gradle_javac_compile_error", "gradle_task_not_found"):
        assert "* Where:" not in recorded_gradle(other), (
            f"{other} now has a `* Where:` block too, which changes what this "
            f"fixture is contrasting against"
        )


def test_find_and_aggregate_test_results(tmp_path: Path, recorded_gradle):
    """Aggregation across the three suites one real `test` task produced.

    The files are laid out in the standard Gradle location and their CONTENT is
    verbatim recorded output, so the totals below are the run's real totals:
    5 tests, 1 passed, 3 failed, 1 skipped.
    """
    results_dir = tmp_path / "app" / "build" / "test-results" / "testDebugUnitTest"
    results_dir.mkdir(parents=True)
    for class_name, fixture in (
        ("com.example.BeforeThrowsTest", "junit_xml_error_case"),
        ("com.example.PassingTest", "junit_xml_passing_case"),
        ("com.example.SkippedAndErrorTest", "junit_xml_skipped_case"),
    ):
        (results_dir / f"TEST-{class_name}.xml").write_text(
            recorded_gradle(fixture), encoding="utf-8"
        )

    files = find_test_result_files(tmp_path)
    assert len(files) == 3

    aggregate = aggregate_test_results(files)
    assert aggregate["total"] == 5
    assert aggregate["passed"] == 1
    assert aggregate["failed"] == 3
    assert aggregate["skipped"] == 1
    assert aggregate["errors"] == 0
    assert len(aggregate["failed_tests"]) == 3


def test_find_test_result_files_missing_path(tmp_path: Path):
    assert find_test_result_files(tmp_path / "does-not-exist") == []


# --- Which reports are THIS run's (N6, D9) -----------------------------------
#
# Measured on tests/fixtures/scaffold/compose, Gradle 8.13: a second identical
# `:app:testDebugUnitTest` run is UP-TO-DATE, exits 0 and leaves the previous
# report's mtime untouched. The directory outlives the run, so "every XML in
# it" is the previous run's verdict, reported as this one's.
#
# "This run's" is exact: BuildRunner deletes the task's reports before Gradle
# starts, and everything in the directory afterwards was written by the run.
# Two heuristics came first and both had holes (review of PR #23): an mtime
# cutoff, defeated by a filesystem that rounds or lags, and an (mtime, size)
# snapshot, which dropped a failing report Gradle rewrote byte for byte. The
# builder tests below use exactly the inputs each of those got wrong.

TASK = "testDebugUnitTest"


def _report(directory: Path, class_name: str, text: str, mtime: float) -> Path:
    """Write a recorded JUnit suite as Gradle names it, with a chosen mtime."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"TEST-{class_name}.xml"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_another_tasks_reports_are_not_this_tasks(tmp_path: Path, recorded_gradle):
    """`testReleaseUnitTest` writes beside `testDebugUnitTest`; a debug run reads only its own."""
    build = tmp_path / "app" / "build" / "test-results"
    mine = _report(
        build / TASK,
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        time.time(),
    )
    _report(
        build / "testReleaseUnitTest",
        "com.example.BeforeThrowsTest",
        recorded_gradle("junit_xml_error_case"),
        time.time(),
    )

    assert find_test_result_files(tmp_path, task=TASK) == [mine]


def _gradles_spelling_of(requested: str, recorded_gradle) -> str:
    """The variant as Gradle resolved ``requested`` in the recorded miscased build.

    gradle_miscased_variant_resolves ran ``:app:assembleProstagingdebug``; the
    log names the task Gradle actually ran. Read from the recording, not typed.
    """
    log = recorded_gradle("gradle_miscased_variant_resolves")
    match = re.search(r"^> Task :app:assemble(\w+)$", log, re.MULTILINE)
    if match is None:
        pytest.fail("the recorded log no longer names the task Gradle ran")
    resolved = match.group(1)
    assert (
        resolved.casefold() == requested.casefold() and resolved != requested
    ), f"the recording no longer shows {requested!r} resolving to another spelling: {resolved!r}"
    return resolved


def test_the_task_directory_is_matched_in_gradles_spelling(tmp_path: Path, recorded_gradle):
    """`--variant prostagingdebug` asks for testProstagingdebugUnitTest.

    Gradle resolves that to testProStagingDebugUnitTest and writes the reports
    under that name; an exact-case glob found nothing and refused a run that
    had results.
    """
    resolved = _gradles_spelling_of("Prostagingdebug", recorded_gradle)
    report = _report(
        tmp_path / "app" / "build" / "test-results" / f"test{resolved}UnitTest",
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        time.time(),
    )

    found = find_test_result_files(tmp_path, task="testProstagingdebugUnitTest")
    assert found == [report]


def test_d9_an_xml_file_gradle_did_not_name_as_a_report_is_not_one(tmp_path: Path, recorded_gradle):
    """D9: `*.xml` returned every top-level XML file, whatever it was.

    The stray file here is a real JUnit suite, copied somewhere Gradle does not
    write and under a name Gradle does not use -- the case where the old glob
    did harm, inflating the count with two failures no run of this task
    produced. Gradle names its reports ``TEST-<class>.xml``.
    """
    results = tmp_path / "app" / "build" / "test-results" / "testDebugUnitTest"
    report = _report(
        results, "com.example.PassingTest", recorded_gradle("junit_xml_passing_case"), time.time()
    )
    (tmp_path / "copied-report.xml").write_text(
        recorded_gradle("junit_xml_error_case"), encoding="utf-8"
    )

    assert find_test_result_files(tmp_path) == [report]


# --- Build output parsing ----------------------------------------------------
#
# Every input here is a RECORDED Gradle run (tests/record_gradle_fixtures.py
# against tests/fixtures/scaffold/compile), never a hand-typed log. Recording
# them found two defects that the previous hand-typed log could not have shown,
# because it was written in the shape the parser already expected:
#
#   * Kotlin K2 prints `e: file:///abs/path.kt:5:13 Message.`, not Kotlin 1.x's
#     `e: file: /path.kt: (5, 13): message`. Every Kotlin diagnostic since
#     Kotlin 2.0 parsed to file/line/column all None.
#   * Gradle reprints the compiler output indented by two spaces inside its
#     "* What went wrong:" block, and the javac pattern's `[^:]+` file group
#     matched the indent, so each javac diagnostic was counted twice over.


def test_parse_build_output_finds_the_kotlin_error_and_its_task(recorded_gradle):
    parsed = parse_build_output(recorded_gradle("gradle_kotlin_compile_error"), "")
    messages = [e["message"] for e in parsed["errors"]]

    assert any("Unresolved reference" in m for m in messages)
    assert any("compileKotlin FAILED" in m for m in messages)
    assert ":kotlin-error:compileKotlin" in parsed["failed_tasks"]


def test_parse_build_output_locates_a_k2_kotlin_error(recorded_gradle):
    """The K2 `file://` URI form, which used to parse to no location at all."""
    parsed = parse_build_output(recorded_gradle("gradle_kotlin_compile_error"), "")
    err = next(e for e in parsed["errors"] if e["type"] == "kotlin")

    assert err["location"]["file"].endswith("/com/example/Broken.kt")
    assert err["location"]["line"] == 5
    assert err["location"]["column"] == 13
    # The URI scheme belongs to the compiler's output, not to the answer.
    assert not err["location"]["file"].startswith("file://")
    assert err["message"] == "Unresolved reference 'missingSymbol'."


def test_parse_build_output_locates_a_kotlin_warning(recorded_gradle):
    """Warnings take the same K2 shape, and are recorded separately.

    A compiler that has errored stops reporting warnings, so one build log
    carrying both an `e:` and a `w:` from the same module is a shape neither
    kotlinc nor javac actually produces.
    """
    parsed = parse_build_output(recorded_gradle("gradle_kotlin_compile_warning"), "")
    located = [w for w in parsed["warnings"] if w["location"]["file"]]

    assert len(located) == 1
    assert located[0]["location"]["file"].endswith("/com/example/Warned.kt")
    assert located[0]["location"]["line"] == 7
    assert located[0]["location"]["column"] == 21
    assert "is deprecated" in located[0]["message"]
    assert parsed["errors"] == []


def test_a_plugin_warning_without_a_location_is_still_a_warning(recorded_gradle):
    """`w: ⚠️ Deprecated Gradle Version` — a `w:` whose body is prose.

    The Kotlin Gradle plugin emits this, and its continuation lines carry no
    `w:` prefix at all. Recorded proof that a `w:` line is not always a
    file/line/column diagnostic.
    """
    parsed = parse_build_output(recorded_gradle("gradle_kotlin_compile_warning"), "")
    prose = [w for w in parsed["warnings"] if w["location"]["file"] is None]

    assert prose, "the plugin's own w: line was dropped"
    assert "Deprecated Gradle Version" in prose[0]["message"]


def test_parse_build_output_locates_a_javac_error(recorded_gradle):
    parsed = parse_build_output(recorded_gradle("gradle_javac_compile_error"), "")
    err = next(e for e in parsed["errors"] if e["type"] == "javac")

    assert err["location"]["file"].endswith("/com/example/Broken.java")
    assert err["location"]["line"] == 6
    assert err["message"] == "cannot find symbol"
    assert ":java-error:compileJava" in parsed["failed_tasks"]


def test_gradles_indented_reprint_is_not_counted_again(recorded_gradle):
    """Gradle repeats the whole compiler output inside '* What went wrong:'.

    Indented by two spaces. Counting those copies made one missing type into
    four javac errors. javac's own duplicate — it reports this mistake twice,
    once per caret position, and says so with a trailing '2 errors' — is real
    output and is left alone.
    """
    text = recorded_gradle("gradle_javac_compile_error")
    assert "  /scaffold/" in text, "fixture no longer carries the indented reprint"

    parsed = parse_build_output(text, "")
    javac = [e for e in parsed["errors"] if e["type"] == "javac"]

    assert len(javac) == text.count("2 errors")


def test_parse_build_output_locates_a_javac_warning(recorded_gradle):
    parsed = parse_build_output(recorded_gradle("gradle_javac_compile_warning"), "")
    warn = next(w for w in parsed["warnings"] if w["type"] == "javac")

    assert warn["location"]["file"].endswith("/com/example/Warned.java")
    assert warn["location"]["line"] == 6
    # The lint category rides inside the message; a hand-written sample said
    # "deprecated API usage", which javac never prints.
    assert warn["message"].startswith("[deprecation]")
    assert parsed["errors"] == []


def test_parse_build_output_reads_stderr_too(recorded_gradle):
    """Same recorded text, handed in on the other stream."""
    text = recorded_gradle("gradle_kotlin_compile_error")
    from_stdout = parse_build_output(text, "")
    from_stderr = parse_build_output("", text)

    assert from_stderr == from_stdout
    assert from_stderr["errors"]


def test_parse_build_output_clean_log(recorded_gradle):
    """A successful build must not cry wolf."""
    parsed = parse_build_output(recorded_gradle("gradle_build_successful"), "")
    assert parsed["errors"] == []
    assert parsed["warnings"] == []
    assert parsed["failed_tasks"] == []


# --- Formatting --------------------------------------------------------------


def test_format_minimal_build_success():
    out = OutputFormatter.format_minimal(
        status="SUCCESS",
        error_count=0,
        warning_count=2,
        build_id="build-x",
        duration=92.0,
    )
    assert out == "Build: SUCCESS (0 errors, 2 warnings) [1:32] [build-x]"


def test_format_minimal_build_failure_surfaces_errors():
    errors = [{"message": "boom", "location": {"file": "A.kt", "line": 1, "column": 2}}]
    out = OutputFormatter.format_minimal(
        status="FAILED",
        error_count=1,
        warning_count=0,
        build_id="build-y",
        duration=5.0,
        errors=errors,
    )
    assert "Build: FAILED" in out
    assert "boom" in out
    assert "A.kt:1:2" in out


def test_format_minimal_test_summary():
    test_info = {"total": 10, "passed": 8, "failed": 2, "skipped": 0, "failed_tests": []}
    out = OutputFormatter.format_minimal(
        status="FAILED",
        error_count=0,
        warning_count=0,
        build_id="build-z",
        duration=3.0,
        test_info=test_info,
    )
    assert "Tests: FAIL (8/10 passed, 2 failed, 0 skipped) [build-z]" in out


def test_format_test_failures():
    failed = [{"test_name": "a.b.testX", "failure_message": "expected 1 got 2"}]
    out = OutputFormatter.format_test_failures(failed)
    assert "a.b.testX" in out
    assert "expected 1 got 2" in out


def _recorded_run_counts(tmp_path: Path, recorded_gradle) -> dict:
    """The aggregate of the three suites one recorded `test` run wrote."""
    results = tmp_path / "app" / "build" / "test-results" / "testDebugUnitTest"
    for class_name, fixture in (
        ("com.example.BeforeThrowsTest", "junit_xml_error_case"),
        ("com.example.PassingTest", "junit_xml_passing_case"),
        ("com.example.SkippedAndErrorTest", "junit_xml_skipped_case"),
    ):
        _report(results, class_name, recorded_gradle(fixture), time.time())
    return aggregate_test_results(find_test_result_files(tmp_path))


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
def test_minimal_and_verbose_open_with_the_same_verdict_line(status, tmp_path, recorded_gradle):
    """X7: the text modes must not disagree about one run.

    Verbose judged by ``failed`` alone and printed its counts one per line;
    minimal also counted ``errors``. Both now open with one shared line.
    """
    test_info = _recorded_run_counts(tmp_path, recorded_gradle)
    common = {"error_count": 0, "warning_count": 0, "build_id": "b1", "duration": 1.0}
    minimal = OutputFormatter.format_minimal(status=status, test_info=test_info, **common)
    verbose = OutputFormatter.format_verbose(status=status, test_info=test_info, **common)

    assert minimal.splitlines()[0] == f"{verbose.splitlines()[0]} [b1]"
    assert verbose.splitlines()[0] == "Tests: FAIL (1/5 passed, 3 failed, 1 skipped)"


def test_verbose_counts_errored_tests_as_not_passing(recorded_gradle):
    """An `<error>` testcase failed the minimal verdict and passed the verbose one.

    The element is grafted onto a real recorded suite, as in
    test_the_error_branch_works_for_xml_gradle_does_not_write: Gradle itself
    writes `<failure>` for every throw this repo could provoke.
    """
    foreign = (
        recorded_gradle("junit_xml_skipped_case")
        .replace("<failure ", "<error ")
        .replace("</failure>", "</error>")
    )
    test_info = parse_junit_xml(foreign)
    assert (test_info["failed"], test_info["errors"]) == (0, 1)

    verbose = OutputFormatter.format_verbose(
        status="SUCCESS",
        error_count=0,
        warning_count=0,
        build_id="b1",
        duration=1.0,
        test_info=test_info,
    )
    assert verbose.splitlines()[0].startswith("Tests: FAIL"), verbose
    assert "1 errors" in verbose.splitlines()[0], verbose


# --- Config round-trip -------------------------------------------------------


def test_config_round_trip(tmp_path: Path):
    config = Config.load(project_dir=tmp_path)
    # Fresh config: nothing learned yet.
    assert config.get_preferred_module() is None
    assert config.get_preferred_variant() is None

    config.update_last_used(":app", "release")
    config.save()

    # File written under .claude/skills/<skill>/config.json.
    assert config.config_path.exists()

    reloaded = Config.load(project_dir=tmp_path)
    assert reloaded.get_preferred_module() == ":app"
    assert reloaded.get_preferred_variant() == "release"


def test_config_manual_preference_wins(tmp_path: Path):
    config = Config.load(project_dir=tmp_path)
    config.data["build"]["preferred_module"] = ":lib"
    config.data["build"]["last_used_module"] = ":app"
    assert config.get_preferred_module() == ":lib"


def test_config_malformed_json_falls_back(tmp_path: Path):
    # Write a malformed config at the exact location load() will look.
    config = Config.load(project_dir=tmp_path)
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text("{ not valid json", encoding="utf-8")

    reloaded = Config.load(project_dir=tmp_path)
    # Falls back to defaults, does not raise.
    assert reloaded.get_preferred_variant() is None


# --- Cache -------------------------------------------------------------------


def test_cache_round_trip(tmp_path: Path):
    cache = BuildResultCache(cache_dir=str(tmp_path / "cache"))
    build_id = cache.save(
        success=False,
        stdout="some stdout",
        stderr="e: /A.kt: (1,1): boom",
        errors=[{"message": "boom", "location": {"file": "/A.kt", "line": 1, "column": 1}}],
        warnings=[],
        failed_tasks=[":app:compileDebugKotlin"],
        test_results=None,
        variant="debug",
        module=":app",
        tasks=[":app:assembleDebug"],
        duration=12.3,
    )
    assert build_id.startswith("build-")

    payload = cache.get(build_id)
    assert payload is not None
    assert payload["success"] is False
    assert payload["errors"][0]["message"] == "boom"

    log = cache.get_log(build_id)
    assert "some stdout" in log
    assert "boom" in log


def test_cache_missing_id(tmp_path: Path):
    cache = BuildResultCache(cache_dir=str(tmp_path / "cache"))
    assert cache.get("build-does-not-exist") is None
    assert cache.get_log("build-does-not-exist") is None


# --- BuildRunner (subprocess mocked) ----------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    return project


def test_build_runner_requires_gradlew(tmp_path: Path):
    from gradle import BuildRunner

    project = tmp_path / "no-wrapper"
    project.mkdir()
    with pytest.raises(ValueError, match="gradlew not found"):
        BuildRunner(str(project))


def test_build_runner_task_construction(tmp_path: Path, monkeypatch):
    import gradle.builder as builder_mod
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = "BUILD SUCCESSFUL"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)

    runner = BuildRunner(str(project), module=":app", variant="release")
    result = runner.build(clean=True)

    assert result.success is True
    # Never shell=True; gradlew invoked with explicit module-scoped task + clean.
    assert captured["kwargs"].get("shell", False) is False
    assert captured["cmd"][0].endswith("gradlew")
    assert "clean" in captured["cmd"]
    assert ":app:assembleRelease" in captured["cmd"]
    assert "-p" in captured["cmd"]


def test_build_runner_test_task_and_suite(tmp_path: Path, monkeypatch):
    import gradle.builder as builder_mod
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    captured: dict = {}

    class _Completed:
        returncode = 1
        stdout = "> Task :app:testDebugUnitTest FAILED"
        stderr = ""

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)

    runner = BuildRunner(str(project), variant="debug")
    result = runner.test(suite="com.example.MyTest")

    assert result.success is False
    assert "testDebugUnitTest" in captured["cmd"]
    assert "--tests" in captured["cmd"]
    assert "com.example.MyTest" in captured["cmd"]
    assert ":app:testDebugUnitTest" in result.failed_tasks


@pytest.mark.parametrize("suite", [None, "com.example.MyTest"])
def test_the_test_task_is_always_rerun(suite, tmp_path: Path, monkeypatch):
    """N6: an UP-TO-DATE test task exits 0 and writes no report.

    `--rerun` is a task option and binds to the task named before it, so it has
    to follow the test task itself -- not `--tests`' value, not the end of argv.
    """
    import gradle.builder as builder_mod
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    captured: dict = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)
    BuildRunner(str(project), module=":app").test(suite=suite)

    cmd = captured["cmd"]
    assert cmd[cmd.index(":app:testDebugUnitTest") + 1] == "--rerun", cmd
    if suite:
        assert cmd[cmd.index("--tests") + 1] == suite, cmd


def _gradle_that_writes(monkeypatch, returncode: int, log: str, writes=(), lag: float = 0.0):
    """Mock gradlew: write ``writes`` (path, recorded text) DURING the run, then exit.

    Gradle writes its JUnit XML while the test task runs, after the builder's
    snapshot -- so a report written here is this run's result. ``lag`` stamps
    each written report that many seconds in the past: a filesystem clock
    behind the host's, or one that rounds.
    """
    import gradle.builder as builder_mod

    def fake_run(cmd, **_kwargs):
        for path, text in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if lag:
                stamp = time.time() - lag
                os.utime(path, (stamp, stamp))
        return subprocess.CompletedProcess(cmd, returncode, stdout=log, stderr="")

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)


def _results(project: Path, module: str = "app") -> Path:
    return project / module / "build" / "test-results" / "testDebugUnitTest"


def test_x7_failing_tests_fail_the_run_when_gradle_exits_zero(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """X7: `ignoreFailures = true` -- BUILD SUCCESSFUL, exit 0, two failures in the XML."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_junit_error_ignorefailures"),
        [
            (
                _results(project) / "TEST-com.example.BeforeThrowsTest.xml",
                recorded_gradle("junit_xml_error_case"),
            )
        ],
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.returncode == 0
    assert result.test_results["failed"] == 2
    assert result.success is False


def test_x7_an_errored_test_fails_the_run_too(tmp_path: Path, monkeypatch, recorded_gradle):
    """`<error>` counts as well as `<failure>` (grafted: Gradle writes only `<failure>`)."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    foreign = (
        recorded_gradle("junit_xml_skipped_case")
        .replace("<failure ", "<error ")
        .replace("</failure>", "</error>")
    )
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_junit_error_ignorefailures"),
        [(_results(project) / "TEST-com.example.SkippedAndErrorTest.xml", foreign)],
    )
    result = BuildRunner(str(project), module=":app").test()

    assert (result.test_results["failed"], result.test_results["errors"]) == (0, 1)
    assert result.success is False


def test_a_green_run_still_passes(tmp_path: Path, monkeypatch, recorded_gradle):
    """Guard against over-correcting X7 into always-FAIL."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_build_successful"),
        [
            (
                _results(project) / "TEST-com.example.PassingTest.xml",
                recorded_gradle("junit_xml_passing_case"),
            )
        ],
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.success is True
    assert result.no_test_results is False
    assert result.test_results["passed"] == 1


def test_an_empty_suite_beside_a_passing_report_still_passes(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """A well-formed empty suite contributes zero without hiding a real passing result."""
    from gradle import BuildRunner

    passing = recorded_gradle("junit_xml_passing_case")
    empty_suite = ElementTree.fromstring(passing)
    for case in empty_suite.findall("testcase"):
        empty_suite.remove(case)
    empty = ElementTree.tostring(empty_suite, encoding="unicode")
    project = _make_project(tmp_path)
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_build_successful"),
        [
            (_results(project) / "TEST-com.example.EmptyTest.xml", empty),
            (_results(project) / "TEST-com.example.PassingTest.xml", passing),
        ],
    )

    result = BuildRunner(str(project), module=":app").test()

    assert result.success is True
    assert result.no_test_results is False
    assert result.test_results["total"] == 1
    assert result.test_results["passed"] == 1


def test_n6_no_report_from_this_run_is_not_a_pass(tmp_path: Path, monkeypatch, recorded_gradle):
    """Gradle exited 0 and wrote nothing; an older passing report sits in the directory."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _report(
        _results(project),
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        time.time() - 3600,
    )
    _gradle_that_writes(monkeypatch, 0, recorded_gradle("gradle_build_successful"))
    result = BuildRunner(str(project), module=":app").test()

    assert result.test_results is None
    assert result.no_test_results is True
    assert result.success is False
    assert result.results_dir == str(_results(project))


def test_a_gradle_failure_without_reports_is_a_build_failure(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """Gradle's own failure is the verdict; it is not the no-results case."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _gradle_that_writes(monkeypatch, 1, recorded_gradle("gradle_task_not_found"))
    result = BuildRunner(str(project), module=":app").test()

    assert result.success is False
    assert result.no_test_results is False
    assert result.rerun_unsupported is False, "a failure that is not about --rerun"
    assert result.test_results is None
    assert result.errors, "the recorded failure's reason must still be reported"


def test_a_report_left_just_before_the_run_is_not_its_result(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """Half a second old and untouched by the run: a clock cutoff called this fresh."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _report(
        _results(project),
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        time.time() - 0.5,
    )
    _gradle_that_writes(monkeypatch, 0, recorded_gradle("gradle_build_successful"))
    result = BuildRunner(str(project), module=":app").test()

    assert result.no_test_results is True
    assert result.success is False


def test_a_report_the_run_wrote_counts_however_far_behind_its_clock_is(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """The run rewrote the report; the filesystem stamped it an hour ago. Still this run's."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    report = _report(
        _results(project),
        "com.example.BeforeThrowsTest",
        recorded_gradle("junit_xml_error_case"),
        time.time() - 7200,
    )
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_junit_error_ignorefailures"),
        [(report, recorded_gradle("junit_xml_error_case"))],
        lag=3600,
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.no_test_results is False
    assert result.test_results["failed"] == 2
    assert result.success is False


def test_the_run_reads_reports_under_gradles_spelling_of_the_task(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """`--variant prostagingdebug`: Gradle runs and writes testProStagingDebugUnitTest."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    resolved = _gradles_spelling_of("Prostagingdebug", recorded_gradle)
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_miscased_variant_resolves"),
        [
            (
                project
                / "app"
                / "build"
                / "test-results"
                / f"test{resolved}UnitTest"
                / "TEST-com.example.PassingTest.xml",
                recorded_gradle("junit_xml_passing_case"),
            )
        ],
    )
    result = BuildRunner(str(project), module=":app", variant="prostagingdebug").test()

    assert result.tasks[0] == ":app:testProstagingdebugUnitTest", "the caller's spelling is sent"
    assert result.no_test_results is False
    assert result.test_results["passed"] == 1
    assert result.success is True


def test_a_gradle_older_than_7_6_is_named_as_the_cause(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """Gradle 7.5.1's answer to --rerun, recorded: the run is refused for that reason."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _gradle_that_writes(
        monkeypatch, 1, recorded_gradle("gradle_rerun_unknown_option", version="7.5.1")
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.rerun_unsupported is True
    assert result.no_test_results is False
    assert result.success is False


def test_an_assemble_is_never_a_missing_test_result(tmp_path: Path, monkeypatch, recorded_gradle):
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    _gradle_that_writes(monkeypatch, 0, recorded_gradle("gradle_build_successful"))
    result = BuildRunner(str(project), module=":app").build()

    assert result.success is True
    assert result.no_test_results is False


def test_a_module_run_reads_only_that_modules_reports(tmp_path: Path, monkeypatch, recorded_gradle):
    """`--module :app` runs `:app:testDebugUnitTest`; a `lib/` report is not its result."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    for module in ("app", "lib"):
        (project / module).mkdir()
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_build_successful"),
        [
            (
                _results(project) / "TEST-com.example.PassingTest.xml",
                recorded_gradle("junit_xml_passing_case"),
            ),
            (
                _results(project, "lib") / "TEST-com.example.BeforeThrowsTest.xml",
                recorded_gradle("junit_xml_error_case"),
            ),
        ],
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.test_results["total"] == 1
    assert result.success is True


def _keep_mtime(path: Path, text: str):
    """A write that leaves ``path`` byte-identical with its old mtime.

    Gradle rewriting an unchanged report on a filesystem whose clock has not
    moved on. The (mtime, size) snapshot could not tell this from "untouched".
    """
    stat = path.stat()

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    return write


def _gradle_that_runs(monkeypatch, returncode: int, log: str, actions=(), seen=None):
    """Mock gradlew that performs ``actions`` (callables) during the run.

    ``seen``, if given, receives every file under the project at the moment
    Gradle starts -- what the builder left in place for it.
    """
    import gradle.builder as builder_mod

    def fake_run(cmd, cwd, **_kwargs):
        if seen is not None:
            seen.update(p for p in Path(cwd).rglob("*") if p.is_file())
        for action in actions:
            action()
        return subprocess.CompletedProcess(cmd, returncode, stdout=log, stderr="")

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)


def test_a_sole_report_rewritten_byte_for_byte_is_counted(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """The run rewrote its one failing report identically, same mtime: still the result."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    text = recorded_gradle("junit_xml_error_case")
    report = _report(_results(project), "com.example.BeforeThrowsTest", text, time.time() - 60)
    _gradle_that_runs(
        monkeypatch,
        0,
        recorded_gradle("gradle_junit_error_ignorefailures"),
        [_keep_mtime(report, text)],
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.no_test_results is False
    assert result.test_results["failed"] == 2
    assert result.success is False


def test_an_identical_failing_rewrite_beside_a_new_passing_report_fails(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """The false pass the snapshot produced: one failure rewritten identically, one new pass."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    failing = recorded_gradle("junit_xml_skipped_case")
    report = _report(
        _results(project), "com.example.SkippedAndErrorTest", failing, time.time() - 60
    )
    passing = _results(project) / "TEST-com.example.PassingTest.xml"
    passing_text = recorded_gradle("junit_xml_passing_case")
    _gradle_that_runs(
        monkeypatch,
        0,
        recorded_gradle("gradle_junit_error_ignorefailures"),
        [
            _keep_mtime(report, failing),
            lambda: passing.write_text(passing_text, encoding="utf-8"),
        ],
    )
    result = BuildRunner(str(project), module=":app").test()

    assert result.test_results["failed"] == 1
    assert result.test_results["passed"] == 1
    assert result.success is False


def test_a_report_outside_the_tasks_directory_is_neither_deleted_nor_collected(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """D9 and the delete's reach: another task's report and a stray copy are left alone."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    other_task = _report(
        project / "app" / "build" / "test-results" / "testReleaseUnitTest",
        "com.example.BeforeThrowsTest",
        recorded_gradle("junit_xml_error_case"),
        time.time() - 60,
    )
    stray = project / "app" / "copied-report.xml"
    stray.write_text(recorded_gradle("junit_xml_error_case"), encoding="utf-8")
    _gradle_that_runs(monkeypatch, 0, recorded_gradle("gradle_build_successful"))
    result = BuildRunner(str(project), module=":app").test()

    assert other_task.exists() and stray.exists(), "a file outside the task's reports was deleted"
    assert result.no_test_results is True, "a report outside the task's directory was collected"


def test_the_pre_run_delete_touches_only_this_tasks_reports(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """What Gradle finds when it starts: the task's TEST-*.xml gone, everything else in place.

    Including the task's own `binary/` results (measured: Gradle keeps them
    beside the XML) and the same task's reports in a module `--module` did not
    name.
    """
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    (project / "lib").mkdir()
    task_dir = _results(project)
    stale = _report(
        task_dir, "com.example.PassingTest", recorded_gradle("junit_xml_passing_case"), 0
    )
    binary = task_dir / "binary" / "results.bin"
    binary.parent.mkdir(parents=True)
    binary.touch()
    other_module = _report(
        _results(project, "lib"),
        "com.example.BeforeThrowsTest",
        recorded_gradle("junit_xml_error_case"),
        0,
    )
    seen: set[Path] = set()
    _gradle_that_runs(monkeypatch, 0, recorded_gradle("gradle_build_successful"), seen=seen)
    BuildRunner(str(project), module=":app").test()

    assert stale not in seen, "this task's old report survived into the run"
    assert {binary, other_module, project / "gradlew"} <= seen, sorted(map(str, seen))


def test_report_cleanup_refuses_a_build_symlink_outside_the_project(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """C1: an app/build symlink must never turn cleanup into an outside deletion."""
    from common import cli
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    (project / "app").mkdir()
    outside_build = tmp_path / "outside" / "build"
    sentinel = _report(
        outside_build / "test-results" / TASK,
        "sentinel",
        recorded_gradle("junit_xml_passing_case"),
        0,
    )
    (project / "app" / "build").symlink_to(outside_build, target_is_directory=True)
    gradle_started = False

    def fake_run(cmd, **_kwargs):
        nonlocal gradle_started
        gradle_started = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    import gradle.builder as builder_mod

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)
    refusal = None
    try:
        BuildRunner(str(project), module=":app").test()
    except cli.Failure as error:
        refusal = error

    assert sentinel.exists(), "cleanup deleted a JUnit report outside the project"
    assert refusal is not None, "the outside report directory was not refused"
    assert sentinel.name in str(refusal)
    assert gradle_started is False


def test_report_cleanup_refuses_an_in_project_build_redirect(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """A selected module's build symlink cannot redirect cleanup into its child module."""
    from common import cli
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    module = project / "app"
    module.mkdir()
    sentinel = _report(
        module / "child" / "build" / "test-results" / TASK,
        "sentinel",
        recorded_gradle("junit_xml_passing_case"),
        0,
    )
    (module / "build").symlink_to("child/build", target_is_directory=True)
    gradle_started = False

    def fake_run(cmd, **_kwargs):
        nonlocal gradle_started
        gradle_started = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    import gradle.builder as builder_mod

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)
    refusal = None
    try:
        BuildRunner(str(project), module=":app").test()
    except cli.Failure as error:
        refusal = error

    assert sentinel.exists(), "cleanup followed app/build into the child module"
    assert refusal is not None, "the in-project build redirect was not refused"
    assert gradle_started is False


def test_report_cleanup_refuses_a_task_directory_redirect(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """A task directory symlink cannot redirect cleanup to another task's reports."""
    from common import cli
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    result_root = project / "app" / "build" / "test-results"
    sentinel = _report(
        result_root.parent / "testReleaseUnitTest",
        "sentinel",
        recorded_gradle("junit_xml_passing_case"),
        0,
    )
    result_root.mkdir(parents=True)
    (result_root / TASK).symlink_to("../testReleaseUnitTest", target_is_directory=True)
    gradle_started = False

    def fake_run(cmd, **_kwargs):
        nonlocal gradle_started
        gradle_started = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    import gradle.builder as builder_mod

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)
    refusal = None
    try:
        BuildRunner(str(project), module=":app").test()
    except cli.Failure as error:
        refusal = error

    assert sentinel.exists(), "cleanup followed the task redirect into another task"
    assert refusal is not None, "the task-directory redirect was not refused"
    assert gradle_started is False


def test_report_cleanup_does_not_cross_into_a_nested_module(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """C4: :app cleanup is exact; app/child is a different module boundary."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    exact = _report(
        _results(project),
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        0,
    )
    nested = _report(
        project / "app" / "child" / "build" / "test-results" / TASK,
        "com.example.BeforeThrowsTest",
        recorded_gradle("junit_xml_error_case"),
        0,
    )
    seen: set[Path] = set()
    _gradle_that_runs(monkeypatch, 1, recorded_gradle("gradle_task_not_found"), seen=seen)

    BuildRunner(str(project), module=":app").test()

    assert exact not in seen, "the selected module's stale report survived cleanup"
    assert nested in seen, "cleanup crossed into the selected module's nested child"


def test_report_cleanup_refuses_ambiguous_task_directory_case(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """C4: two case variants do not identify one safe task directory to delete."""
    from common import cli
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    exact = _report(
        _results(project),
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        0,
    )
    variant = _report(
        project / "app" / "build" / "test-results" / "testDEBUGUnitTest",
        "com.example.BeforeThrowsTest",
        recorded_gradle("junit_xml_error_case"),
        0,
    )
    result_root = project / "app" / "build" / "test-results"
    exact_dir = result_root / TASK
    variant_dir = result_root / "testDEBUGUnitTest"
    original_iterdir = Path.iterdir

    def colliding_task_directories(path):
        # pytest's macOS temp volume is case-insensitive, so both spellings
        # address one on-disk directory. Substitute only the directory entries
        # to reproduce the case-sensitive listing the review observed.
        if path == result_root:
            return iter((exact_dir, variant_dir))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", colliding_task_directories)
    gradle_started = False

    def fake_run(cmd, **_kwargs):
        nonlocal gradle_started
        gradle_started = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    import gradle.builder as builder_mod

    monkeypatch.setattr(builder_mod.subprocess, "run", fake_run)
    refusal = None
    try:
        BuildRunner(str(project), module=":app").test()
    except cli.Failure as error:
        refusal = error

    assert exact.exists() and variant.exists(), "ambiguous cleanup deleted reports"
    assert refusal is not None, "case-colliding task directories were not refused"
    assert "ambiguous" in str(refusal).lower()
    assert gradle_started is False


def test_report_cleanup_uses_the_only_case_variant(tmp_path: Path, monkeypatch, recorded_gradle):
    """C4: recorded Gradle behavior permits one resolved spelling of a miscased task."""
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    resolved = _gradles_spelling_of("Prostagingdebug", recorded_gradle)
    stale = _report(
        project / "app" / "build" / "test-results" / f"test{resolved}UnitTest",
        "com.example.PassingTest",
        recorded_gradle("junit_xml_passing_case"),
        0,
    )
    seen: set[Path] = set()
    _gradle_that_runs(monkeypatch, 1, recorded_gradle("gradle_task_not_found"), seen=seen)

    BuildRunner(str(project), module=":app", variant="prostagingdebug").test()

    assert stale not in seen, "the sole Gradle-resolved case variant was not cleaned"


def test_malformed_report_refuses_a_partial_aggregate(tmp_path: Path, monkeypatch, recorded_gradle):
    """C3: truncating the recorded failing XML cannot leave its passing peer as PASS."""
    from common import cli
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    failing = recorded_gradle("junit_xml_error_case")
    # Derived from the recording by removing only its closing testsuite tag,
    # reproducing a write truncated before completion without inventing XML.
    truncated = failing[: failing.rindex("</testsuite>")]
    bad_report = _results(project) / "TEST-com.example.BeforeThrowsTest.xml"
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_junit_error_ignorefailures"),
        [
            (bad_report, truncated),
            (
                _results(project) / "TEST-com.example.PassingTest.xml",
                recorded_gradle("junit_xml_passing_case"),
            ),
        ],
    )

    with pytest.raises(cli.Failure, match=re.escape(str(bad_report))):
        BuildRunner(str(project), module=":app").test()


def test_unreadable_report_refuses_a_partial_aggregate(
    tmp_path: Path, monkeypatch, recorded_gradle
):
    """C3: a report that disappears between discovery and reading is not skipped."""
    from common import cli
    from gradle import BuildRunner

    project = _make_project(tmp_path)
    report = _results(project) / "TEST-com.example.PassingTest.xml"
    _gradle_that_writes(
        monkeypatch,
        0,
        recorded_gradle("gradle_build_successful"),
        [(report, recorded_gradle("junit_xml_passing_case"))],
    )
    original_read_text = Path.read_text

    def fail_report_read(path, *args, **kwargs):
        if path == report:
            raise OSError("simulated report read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_report_read)

    with pytest.raises(cli.Failure, match=re.escape(str(report))):
        BuildRunner(str(project), module=":app").test()
