#!/usr/bin/env python3
"""Record verbatim Gradle output into tests/fixtures/recorded/gradle-<version>/.

Companion to ``record_fixtures.py`` (which records adb output). Same rule:
**parser tests consume these files; they never inline Gradle output as a string
literal.**

Gradle failure text is the thing the skill most needs to parse and currently
does not — ``gradle/results.py`` defines ``_BUILD_FAILED_RE`` and never uses it,
so a configuration or task-lookup failure is reported as
``Build: FAILED (0 errors, 0 warnings)`` with no diagnostic at all.

Fixtures come from two checked-in scaffolds, neither of which needs AGP or an
Android SDK:

* ``tests/fixtures/scaffold/flavored`` — task names that mimic AGP's
  ``buildType x productFlavor`` camelCase (``assembleProStagingDebug``). The
  failures recorded from it come from Gradle's own task lookup and error
  reporting, so plain Gradle reproduces them exactly.
* ``tests/fixtures/scaffold/compile`` — one subproject per compiler diagnostic,
  because AGP only *runs* kotlinc and javac; the diagnostic text belongs to the
  compilers. Recording these found that the shape ``gradle/results.py`` parses
  is Kotlin 1.x's (``e: /path.kt: (10, 5): message``) while K2 emits
  ``e: file:///path.kt:10:5 Message.`` — so a Kotlin error has parsed to no
  location at all since Kotlin 2.0.

Usage
-----
    python tests/record_gradle_fixtures.py --list
    python tests/record_gradle_fixtures.py
    python tests/record_gradle_fixtures.py --gradle /path/to/gradle
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCAFFOLDS = Path(__file__).resolve().parent / "fixtures" / "scaffold"
FLAVORED = SCAFFOLDS / "flavored"
COMPILE = SCAFFOLDS / "compile"
WHERE_BLOCK = SCAFFOLDS / "where-block"
ALL_SCAFFOLDS = (FLAVORED, COMPILE, WHERE_BLOCK)
RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"

GRADLE_TIMEOUT = 300


@dataclass(frozen=True)
class GradleFixture:
    """One recorded Gradle invocation.

    Attributes:
        name: Basename written under ``recorded/gradle-<version>/``.
        args: Arguments after ``gradle --console=plain``.
        description: What a parser test uses this for.
        catches: Defect IDs this fixture would have caught.
        scaffold: Project to run in. Defaults to the flavored scaffold.
        rerun: Pass ``--rerun-tasks``. Mandatory for anything whose point is
            compiler output: an UP-TO-DATE task recompiles nothing and so
            prints no diagnostic, and a fixture recorded from that run would
            be an empty file that looks like a clean build.
        artifact: Optional path, relative to ``scaffold``, of a file the build
            *produces*. When set, that file is recorded instead of the build
            log. Needed because the JUnit XML a results parser consumes is
            written to disk by the test runner and never printed, so no amount
            of capturing stdout can reach it.
        requires_in_log: Text that must appear in the build output for the
            capture to count. For an artifact fixture this is the proof the
            producing task actually ran: without it, a build that failed during
            configuration leaves the PREVIOUS run's file on disk and the
            recorder would happily publish it as this run's output.
        ext: Extension for the recorded file. ``xml`` for produced artifacts.
    """

    name: str
    args: list[str]
    description: str
    catches: tuple[str, ...] = ()
    scaffold: Path = FLAVORED
    rerun: bool = False
    artifact: str | None = None
    requires_in_log: str | None = None
    ext: str = "txt"


FIXTURES: list[GradleFixture] = [
    GradleFixture(
        name="gradle_task_not_found",
        args=[":app:assembleNoSuchVariantAtAll"],
        description=(
            "A task that genuinely does not exist. Produces Gradle's "
            "'FAILURE: Build failed with an exception.' / '* What went wrong:' "
            "block. Nothing in gradle/results.py parses this, so the skill "
            "reports 'Build: FAILED (0 errors, 0 warnings)' with no diagnostic."
        ),
        catches=("A6",),
    ),
    GradleFixture(
        name="gradle_task_ambiguous",
        args=[":app:assembleProstaging"],
        description=(
            "An abbreviated task name matching more than one task. Gradle lists "
            "the candidates in the '* What went wrong:' block — exactly the "
            "detail an agent needs and currently never sees."
        ),
        catches=("A6",),
    ),
    GradleFixture(
        name="gradle_miscased_variant_resolves",
        args=[":app:assembleProstagingdebug"],
        description=(
            "Evidence that S2 is NOT build-breaking. str.capitalize() turns "
            "'proStagingDebug' into 'Prostagingdebug', but Gradle's name "
            "abbreviation matching is case-insensitive and resolves it to "
            "assembleProStagingDebug. BUILD SUCCESSFUL, no deprecation warning. "
            "The generated name is still wrong and should be fixed, but it does "
            "not fail the build."
        ),
        catches=("S2",),
    ),
    GradleFixture(
        name="gradle_build_successful",
        args=[":app:assembleProStagingDebug"],
        description="Baseline success output, for asserting the parser does not cry wolf.",
    ),
    GradleFixture(
        name="gradle_kotlin_compile_error",
        args=[":kotlin-error:compileKotlin"],
        scaffold=COMPILE,
        rerun=True,
        description=(
            "A real Kotlin compile error, and the reason this fixture had to "
            "exist. K2 prints "
            "`e: file:///abs/path/Broken.kt:5:13 Unresolved reference 'x'.` — a "
            "file:// URI, colon-separated line:column, no parentheses, no colon "
            "before the message, and a trailing full stop. results.py's "
            "_split_kotlin_body matches Kotlin 1.x's "
            "`e: /path.kt: (5, 13): unresolved reference: x` instead, so on any "
            "Kotlin 2.x build the error is reported with file, line and column "
            "all None. Note too that the `> Task ... FAILED` marker and the "
            "`e:` line both appear, so one mistake yields two errors."
        ),
    ),
    GradleFixture(
        name="gradle_kotlin_compile_warning",
        args=[":kotlin-warning:compileKotlin"],
        scaffold=COMPILE,
        rerun=True,
        description=(
            "A real Kotlin warning, in the same K2 shape as the error: "
            "`w: file:///abs/path.kt:7:21 'fun retired(): Int' is deprecated. …`. "
            "Recorded separately from the error because a compiler that has "
            "errored stops reporting warnings — measured: a module holding both "
            "prints only the error — so 'errors and warnings in one log' is not "
            "a shape either compiler produces from one module. This capture also "
            "carries the Kotlin Gradle plugin's own multi-line `w:` block, whose "
            "body is prose rather than a location and whose continuation lines "
            "carry no `w:` prefix at all."
        ),
    ),
    GradleFixture(
        name="gradle_javac_compile_error",
        args=[":java-error:compileJava"],
        scaffold=COMPILE,
        rerun=True,
        description=(
            "A real javac error. The `path.java:LINE: error: message` shape is "
            "the one thing the hand-written sample got right, but it omits "
            "everything around it: the source line and caret, the indented "
            "`symbol:`/`location:` continuation lines, a trailing `N errors` "
            "count, one mistake reported TWICE by javac itself, and then the "
            "whole block repeated a third and fourth time — indented by two "
            "spaces — inside Gradle's `* What went wrong:` section. "
            "_JAVAC_ERROR_RE's `[^:]+` file group matches leading spaces, so it "
            "re-matches the indented copies and one missing type is counted "
            "four times."
        ),
    ),
    GradleFixture(
        name="gradle_javac_compile_warning",
        args=[":java-warning:compileJava"],
        scaffold=COMPILE,
        rerun=True,
        description=(
            "A real javac warning: "
            "`path.java:6: warning: [deprecation] retired() in Retired has been "
            "deprecated`, followed by the source line, a caret and `1 warning`. "
            "The `[deprecation]` lint-category tag rides inside the message, "
            "which no hand-written sample had. Requires `-Xlint:deprecation` and "
            "a caller in a different class — javac says nothing when a class "
            "deprecates and calls its own method."
        ),
    ),
    GradleFixture(
        name="gradle_junit_error_ignorefailures",
        requires_in_log="> Task :junit-error:test",
        args=[":junit-error:test"],
        scaffold=COMPILE,
        rerun=True,
        description=(
            "A test run with three failing tests, one skipped and one passing, "
            "under `test { ignoreFailures = true }`. The log says "
            "'5 tests completed, 3 failed, 1 skipped' and then "
            "**BUILD SUCCESSFUL**, and "
            "gradle exits 0. That pairing is the fixture's point: a caller that "
            "reads the exit status, or greps for 'BUILD FAILED', concludes the "
            "suite passed. Only the JUnit XML (junit_xml_error_case.xml, "
            "recorded from the same run) carries the truth."
        ),
        catches=("X7", "D2"),
    ),
    GradleFixture(
        name="junit_xml_error_case",
        requires_in_log="> Task :junit-error:test",
        args=[":junit-error:test"],
        scaffold=COMPILE,
        rerun=True,
        artifact="junit-error/build/test-results/test/TEST-com.example.BeforeThrowsTest.xml",
        ext="xml",
        description=(
            "The JUnit XML for a class whose @Before throws — the real thing, "
            "which no test in this repo had. Measured shape, and it contradicts "
            'the obvious guess: the suite carries failures="2" errors="0" and '
            'each testcase holds a <failure type="java.lang.IllegalStateException"> '
            "element. There is no <error> element, even though the exception came "
            "from setup rather than from an assertion, so a parser keying on "
            "<error> to find setup failures finds none. Both testcases are still "
            "listed by name although neither body ran, and each carries the same "
            "stack trace rooted at setUp. hostname/timestamp/time are normalised "
            "by the recorder (see _scrub): Gradle writes the recording machine's "
            "hostname into every testsuite element."
        ),
        catches=("X7", "D2"),
    ),
    GradleFixture(
        name="junit_xml_passing_case",
        requires_in_log="> Task :junit-error:test",
        args=[":junit-error:test"],
        scaffold=COMPILE,
        rerun=True,
        artifact="junit-error/build/test-results/test/TEST-com.example.PassingTest.xml",
        ext="xml",
        description=(
            'The green half of the same run: tests="1" skipped="0" '
            'failures="0" errors="0" and a bare self-closing <testcase>. '
            "Recorded because a parser that reports everything as failed and a "
            "parser that reports nothing at all look identical against a corpus "
            "in which everything failed. Also the second document that "
            "aggregate_test_results merges."
        ),
        catches=("X7", "D2"),
    ),
    GradleFixture(
        name="junit_xml_skipped_case",
        requires_in_log="> Task :junit-error:test",
        args=[":junit-error:test"],
        scaffold=COMPILE,
        rerun=True,
        artifact="junit-error/build/test-results/test/TEST-com.example.SkippedAndErrorTest.xml",
        ext="xml",
        description=(
            "A real <skipped/> child, from an @Ignore'd test -- the only way a "
            "Gradle report gets one, and parse_junit_xml counts them. The suite "
            'reads tests="2" skipped="1" failures="1" errors="0". '
            "The second method exists to answer the obvious question about the "
            "<error> element and settles it: it throws an "
            "UnsupportedOperationException, which is NOT an AssertionError, and "
            'Gradle still writes <failure type="...">. Taken with '
            "junit_xml_error_case (an exception from @Before, also <failure>), "
            "no route tried here makes Gradle's writer emit <error> at all."
        ),
        catches=("X7", "D2"),
    ),
    GradleFixture(
        name="gradle_where_block",
        args=["help"],
        scaffold=WHERE_BLOCK,
        description=(
            "A configuration-time failure, which is the only Gradle failure "
            "shape that reports a LOCATION. `implementaion` (a typo for "
            "`implementation`) makes Gradle emit a `* Where:` section naming the "
            "build file and the line number, above the usual `* What went "
            "wrong:` block. Every other recorded failure here has no `* Where:` "
            "at all, so a parser that only knows the `* What went wrong:` shape "
            "throws away the file and line on precisely the failures that have "
            "one."
        ),
        catches=("D8",),
    ),
]


def _gradle_version(gradle: str) -> str:
    """Return the Gradle version string, e.g. '8.13'."""
    result = subprocess.run(
        [gradle, "--version"], capture_output=True, text=True, timeout=120, check=False
    )
    match = re.search(r"^Gradle\s+(\S+)", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not determine Gradle version from: {result.stdout[:200]!r}")
    return match.group(1)


def _artifact_path(fixture: GradleFixture) -> pathlib.Path | None:
    """Absolute path of the file this fixture records, if it records one."""
    return None if fixture.artifact is None else fixture.scaffold / fixture.artifact


def _run(gradle: str, fixture: GradleFixture) -> str:
    """Run one fixture's Gradle invocation and return combined output.

    Failure output is the point of most of these fixtures, so a non-zero exit is
    expected and not an error. ``--console=plain`` matches what the skill itself
    passes, so the recorded text is what the parser would really see.
    """
    flags = ["--console=plain"]
    if fixture.rerun:
        flags.append("--rerun-tasks")
    result = subprocess.run(
        [gradle, *flags, *fixture.args],
        cwd=fixture.scaffold,
        capture_output=True,
        text=True,
        timeout=GRADLE_TIMEOUT,
        check=False,
    )
    return result.stdout + result.stderr


def _scrub(text: str) -> str:
    """Remove machine-specific and run-specific detail.

    Durations and absolute paths change every run and would make every
    re-record a noisy diff, hiding the format changes that actually matter.
    Every scaffold root is replaced, which also normalises the ``file://`` URI
    K2 emits — ``file:///Users/<someone>/…`` becomes ``file:///scaffold/…`` —
    so a developer's account name never reaches this public repository.

    The JUnit XML added three more that MUST be scrubbed, and the first is not
    cosmetic: Gradle stamps every ``<testsuite>`` with
    ``hostname="<machine>.local"``, which on a personal laptop is the owner's
    name. It is written into the file by the test runner, so the package-name
    redaction that guards the adb corpus never sees it. ``timestamp`` and the
    per-test ``time`` are wall-clock values that would otherwise change on
    every re-record.
    """
    text = re.sub(r"in \d+m?s\b", "in 0s", text)
    text = re.sub(r"\d+ actionable task", "1 actionable task", text)
    text = re.sub(r'hostname="[^"]*"', 'hostname="build-host"', text)
    text = re.sub(r'timestamp="[^"]*"', 'timestamp="2020-01-01T00:00:00Z"', text)
    text = re.sub(r'time="[\d.]+"', 'time="0.0"', text)
    for scaffold in ALL_SCAFFOLDS:
        text = text.replace(str(scaffold), "/scaffold")
    return text


def record(gradle: str) -> int:
    """Record every Gradle fixture and write the manifest."""
    for scaffold in ALL_SCAFFOLDS:
        if not scaffold.exists():
            print(f"scaffold missing: {scaffold}", file=sys.stderr)
            return 1

    version = _gradle_version(gradle)
    out_dir = RECORDED_ROOT / f"gradle-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for fixture in FIXTURES:
        produced = _artifact_path(fixture)
        # Delete the target BEFORE the build. Otherwise a build that dies during
        # configuration leaves the previous run's file in place and this
        # recorder publishes stale bytes as if the run had just written them --
        # a fixture that looks like ground truth and is not, which is the one
        # thing this tool exists to prevent.
        if produced is not None and produced.exists():
            produced.unlink()
        started_at = time.time()
        log = _run(gradle, fixture)

        if fixture.requires_in_log is not None and fixture.requires_in_log not in log:
            print(
                f"  FAIL  {fixture.name}: the build output does not contain "
                f"{fixture.requires_in_log!r}, so the producing task did not "
                f"run. The build printed:\n{log[-2000:]}",
                file=sys.stderr,
            )
            return 1

        if produced is None:
            text = _scrub(log)
        else:
            # The build had to run to produce it, but what gets recorded is the
            # file the build WROTE, not what it printed.
            if not produced.exists():
                print(
                    f"  FAIL  {fixture.name}: {fixture.artifact} was not "
                    f"produced. The build printed:\n{log[-2000:]}",
                    file=sys.stderr,
                )
                return 1
            # Belt and braces after the pre-delete: the file must also be newer
            # than the invocation that was supposed to create it.
            if produced.stat().st_mtime < started_at:
                print(
                    f"  FAIL  {fixture.name}: {fixture.artifact} predates this "
                    f"build, so it is a leftover rather than this run's output.",
                    file=sys.stderr,
                )
                return 1
            text = _scrub(produced.read_text(encoding="utf-8"))
        path = out_dir / f"{fixture.name}.{fixture.ext}"
        path.write_text(text, encoding="utf-8")
        entries.append(
            {
                "name": fixture.name,
                "file": path.name,
                "command": (
                    "gradle --console=plain "
                    + ("--rerun-tasks " if fixture.rerun else "")
                    + " ".join(fixture.args)
                ),
                "scaffold": f"tests/fixtures/scaffold/{fixture.scaffold.name}",
                "produced_artifact": fixture.artifact,
                "description": fixture.description,
                "catches": list(fixture.catches),
                "bytes": len(text.encode("utf-8")),
            }
        )
        print(f"  ok    {path.name} ({len(text.encode('utf-8'))} bytes)")

    manifest = {
        "profile": f"gradle-{version}",
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gradle_version": version,
        "scaffolds": [f"tests/fixtures/scaffold/{s.name}" for s in ALL_SCAFFOLDS],
        "note": (
            "Durations and absolute paths are normalised so a re-record diffs "
            "only on real format changes. The JUnit XML additionally has its "
            "hostname, timestamp and per-test time normalised: Gradle writes "
            "the recording machine's hostname into every testsuite element."
        ),
        "fixtures": entries,
    }
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(entries)} fixture(s) -> {out_dir}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record verbatim Gradle output as test fixtures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--gradle", default="gradle", help="Gradle executable (default: gradle)")
    parser.add_argument("--list", action="store_true", help="List fixtures and exit")
    args = parser.parse_args()

    if args.list:
        width = max(len(f.name) for f in FIXTURES)
        for fixture in FIXTURES:
            catches = f"  [catches {', '.join(fixture.catches)}]" if fixture.catches else ""
            print(f"{fixture.name:<{width}}  gradle {' '.join(fixture.args)}{catches}")
        return

    sys.exit(record(args.gradle))


if __name__ == "__main__":
    main()
