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

JUNIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.CalculatorTest" tests="4" failures="1" errors="1" skipped="1">
  <testcase classname="com.example.CalculatorTest" name="testAdd" time="0.01"/>
  <testcase classname="com.example.CalculatorTest" name="testSubtract" time="0.02">
    <failure message="expected:&lt;5&gt; but was:&lt;4&gt;">java.lang.AssertionError</failure>
  </testcase>
  <testcase classname="com.example.CalculatorTest" name="testDivide" time="0.0">
    <error message="ArithmeticException: / by zero">stack trace here</error>
  </testcase>
  <testcase classname="com.example.CalculatorTest" name="testIgnored" time="0.0">
    <skipped/>
  </testcase>
</testsuite>
"""

JUNIT_XML_TESTSUITES = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="SuiteA">
    <testcase classname="a.A" name="passes"/>
  </testsuite>
  <testsuite name="SuiteB">
    <testcase classname="b.B" name="fails">
      <failure message="boom">trace</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_xml_counts():
    result = parse_junit_xml(JUNIT_XML)
    assert result["total"] == 4
    assert result["passed"] == 1
    assert result["failed"] == 1
    assert result["errors"] == 1
    assert result["skipped"] == 1


def test_parse_junit_xml_failed_names_and_messages():
    result = parse_junit_xml(JUNIT_XML)
    names = {t["test_name"] for t in result["failed_tests"]}
    assert "com.example.CalculatorTest.testSubtract" in names
    assert "com.example.CalculatorTest.testDivide" in names

    by_name = {t["test_name"]: t["failure_message"] for t in result["failed_tests"]}
    assert by_name["com.example.CalculatorTest.testSubtract"] == "expected:<5> but was:<4>"
    assert "ArithmeticException" in by_name["com.example.CalculatorTest.testDivide"]


def test_parse_junit_xml_testsuites_root():
    result = parse_junit_xml(JUNIT_XML_TESTSUITES)
    assert result["total"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1
    assert result["failed_tests"][0]["test_name"] == "b.B.fails"


def test_parse_junit_xml_empty_and_malformed():
    assert parse_junit_xml("")["total"] == 0
    assert parse_junit_xml("not xml <<<")["total"] == 0


def test_find_and_aggregate_test_results(tmp_path: Path):
    # Standard Gradle layout: <module>/build/test-results/<task>/TEST-*.xml
    results_dir = tmp_path / "app" / "build" / "test-results" / "testDebugUnitTest"
    results_dir.mkdir(parents=True)
    (results_dir / "TEST-com.example.CalculatorTest.xml").write_text(JUNIT_XML, encoding="utf-8")
    (results_dir / "TEST-Suites.xml").write_text(JUNIT_XML_TESTSUITES, encoding="utf-8")

    files = find_test_result_files(tmp_path)
    assert len(files) == 2

    aggregate = aggregate_test_results(files)
    assert aggregate["total"] == 6  # 4 + 2
    assert aggregate["passed"] == 2  # 1 + 1
    assert aggregate["failed"] == 2  # 1 + 1
    assert aggregate["skipped"] == 1
    assert aggregate["errors"] == 1
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
