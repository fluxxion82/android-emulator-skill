"""Device-free tests for the gradle/ build subpackage.

These exercise the PURE logic only — JUnit XML parsing, Gradle build-output
parsing, output formatting, config round-trip, and the cache wrapper. No real
gradlew is invoked and no device is required. subprocess is monkeypatched where a
build run is involved.
"""

from __future__ import annotations

from pathlib import Path

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
