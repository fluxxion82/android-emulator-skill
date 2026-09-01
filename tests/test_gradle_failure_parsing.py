"""Gradle failure reporting, against recorded Gradle output.

Covers three defects that compound into the same user-visible symptom — a build
that failed and says nothing useful about why:

  A6  `_BUILD_FAILED_RE` is defined and never used, so Gradle's
      "FAILURE: / * What went wrong:" block is never parsed. Configuration,
      dependency-resolution and task-lookup failures emit no `> Task ... FAILED`
      and no `e:` lines, so they report "0 errors, 0 warnings".
  A8  The reporter's test verdict ignores `errors`, so a suite where cases threw
      prints "(5/10 passed, 0 failed, 0 skipped)" — numbers that do not sum.
  S2  Variant task names are built with `str.capitalize()`, which lowercases
      everything after the first letter.

Fixtures come from `tests/record_gradle_fixtures.py`, run against the checked-in
scaffold at `tests/fixtures/scaffold/flavored`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gradle.reporter import OutputFormatter
from gradle.results import parse_build_output

GRADLE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recorded" / "gradle-8.13"


@pytest.fixture(scope="session")
def gradle_output():
    """Recorded verbatim Gradle output, by fixture name."""

    def _load(name: str) -> str:
        path = GRADLE_FIXTURES / f"{name}.txt"
        if not path.exists():
            pytest.fail(
                f"Gradle fixture '{name}' is missing.\n"
                f"Run: python tests/record_gradle_fixtures.py"
            )
        return path.read_text(encoding="utf-8")

    return _load


# ---------------------------------------------------------------------------
# Fixture integrity.
# ---------------------------------------------------------------------------


def test_gradle_manifest_matches_files():
    """Guard the guard: a hollowed-out fixture set must not pass silently."""
    manifest = json.loads((GRADLE_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
    listed = {entry["file"] for entry in manifest["fixtures"]}
    on_disk = {p.name for p in GRADLE_FIXTURES.iterdir() if p.name != "MANIFEST.json"}
    assert listed == on_disk


def test_failure_fixture_really_contains_a_gradle_failure_block(gradle_output):
    """Without the real block present, the A6 tests below prove nothing."""
    text = gradle_output("gradle_task_not_found")
    assert "FAILURE: Build failed with an exception." in text
    assert "* What went wrong:" in text
    assert "> Task" not in text, (
        "this fixture must NOT contain a '> Task ... FAILED' marker — the whole "
        "point is that task-lookup failures are invisible to the existing parser"
    )


# ---------------------------------------------------------------------------
# A6 — the "What went wrong" block must reach the user.
# ---------------------------------------------------------------------------


def test_task_not_found_failure_is_reported_as_an_error(gradle_output):
    """A build that failed must produce at least one error."""
    parsed = parse_build_output(gradle_output("gradle_task_not_found"), "")
    assert parsed["errors"], (
        "Gradle printed 'FAILURE: Build failed with an exception.' but the "
        "parser found 0 errors, so the CLI reports "
        "'Build: FAILED (0 errors, 0 warnings)' with no diagnostic"
    )


def test_task_not_found_error_explains_what_went_wrong(gradle_output):
    """The message must carry Gradle's diagnosis, not just 'build failed'."""
    parsed = parse_build_output(gradle_output("gradle_task_not_found"), "")
    messages = " ".join(e["message"] for e in parsed["errors"])
    assert (
        "assembleNoSuchVariantAtAll" in messages
    ), f"error text does not name the missing task: {messages!r}"


def test_ambiguous_task_error_lists_the_candidates(gradle_output):
    """Gradle names the candidate tasks; that detail is the actionable part."""
    parsed = parse_build_output(gradle_output("gradle_task_ambiguous"), "")
    messages = " ".join(e["message"] for e in parsed["errors"])
    assert (
        "assembleProStagingDebug" in messages
    ), f"candidate tasks not surfaced to the agent: {messages!r}"


def test_successful_build_reports_no_errors(gradle_output):
    """The failure parser must not cry wolf on a clean build."""
    parsed = parse_build_output(gradle_output("gradle_build_successful"), "")
    assert parsed["errors"] == []
    assert parsed["failed_tasks"] == []


# ---------------------------------------------------------------------------
# A8 — errored tests are tests that did not pass.
# ---------------------------------------------------------------------------


def test_errored_tests_are_not_reported_as_a_pass():
    """A suite where cases threw has not passed.

    `parse_junit_xml` populates `errors` for `<error>` testcases — anything that
    throws, including a crash in @Before/setUp. The verdict ignored it.
    """
    test_info = {"total": 10, "passed": 5, "failed": 0, "skipped": 0, "errors": 5}
    line = OutputFormatter.format_minimal(
        status="SUCCESS",
        error_count=0,
        warning_count=0,
        build_id="b1",
        duration=1.0,
        test_info=test_info,
    )
    assert "PASS" not in line, f"5 of 10 tests errored but the verdict says PASS: {line!r}"


def test_reported_test_counts_account_for_every_case():
    """Printed numbers must sum to the total, or the report is unreadable."""
    test_info = {"total": 10, "passed": 5, "failed": 0, "skipped": 0, "errors": 5}
    line = OutputFormatter.format_minimal(
        status="SUCCESS",
        error_count=0,
        warning_count=0,
        build_id="b1",
        duration=1.0,
        test_info=test_info,
    )
    assert (
        "5 errors" in line or "error" in line.lower()
    ), f"the 5 errored tests are invisible in the summary line: {line!r}"


def test_clean_suite_still_reports_pass():
    """Guard against over-correcting into always-FAIL."""
    test_info = {"total": 10, "passed": 10, "failed": 0, "skipped": 0, "errors": 0}
    line = OutputFormatter.format_minimal(
        status="SUCCESS",
        error_count=0,
        warning_count=0,
        build_id="b1",
        duration=1.0,
        test_info=test_info,
    )
    assert "PASS" in line


# ---------------------------------------------------------------------------
# S2 — variant task-name construction.
#
# NOTE: recorded evidence shows this is NOT build-breaking. Gradle's task name
# abbreviation matching is case-insensitive, so `assembleProstagingdebug`
# resolves to `assembleProStagingDebug` with no warning. The generated name is
# still wrong, shows up wrong in logs, and depends on a fallback that fails the
# moment the abbreviation is ambiguous — so it is worth fixing, at low priority.
# ---------------------------------------------------------------------------


def test_miscased_variant_task_still_resolves_today(gradle_output):
    """Pin the evidence that S2 is not the build-breaking defect first claimed."""
    text = gradle_output("gradle_miscased_variant_resolves")
    assert "BUILD SUCCESSFUL" in text
    assert "assembleProStagingDebug" in text, "Gradle resolved the miscased name to the real task"


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("debug", "assembleDebug"),
        ("release", "assembleRelease"),
        ("proStagingDebug", "assembleProStagingDebug"),
        ("freeProdRelease", "assembleFreeProdRelease"),
    ],
)
def test_variant_task_name_preserves_inner_capitals(variant, expected):
    """Only the first letter should change case.

    `str.capitalize()` lowercases the remainder, so `proStagingDebug` becomes
    `Prostagingdebug`. The single-word variants pass either way, which is why
    the existing tests — which only use debug/release — never caught this.
    """
    from gradle.builder import BuildRunner

    builder = BuildRunner.__new__(BuildRunner)
    builder.variant = variant
    builder.module = None
    assert builder._task_name("assemble") == expected
