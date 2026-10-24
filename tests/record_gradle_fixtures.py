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
    python tests/record_gradle_fixtures.py --gradle /path/to/gradle-7.5.1 \
        --only gradle_rerun_unknown_option

``--only`` records the named fixtures and merges them into that Gradle
version's existing manifest instead of replacing it. A fixture with
``gradle_below`` set is about an OLDER Gradle's answer; a full recording skips
it unless the Gradle being run is older than that version.

Connected (instrumented) fixtures
---------------------------------
The ``connected_android_test_*`` fixtures run ``connectedDebugAndroidTest`` in
a fresh copy of ``tests/fixtures/scaffold/compose`` (AGP, so ANDROID_HOME must
be set) and need a running emulator, named with ``--serial``::

    ANDROID_HOME=~/Library/Android/sdk python tests/record_gradle_fixtures.py \
        --serial emulator-5554 --only connected_android_test_success

The serial reaches Gradle as ``ANDROID_SERIAL`` in the child's environment,
set here rather than inherited from the shell, and anything that is not an
``emulator-NNNN`` is refused: with several devices attached a connected task
without ANDROID_SERIAL runs on EVERY one of them -- a plugged-in phone
included -- and uninstalls the APKs from each when it finishes.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCAFFOLDS = Path(__file__).resolve().parent / "fixtures" / "scaffold"
FLAVORED = SCAFFOLDS / "flavored"
COMPILE = SCAFFOLDS / "compile"
WHERE_BLOCK = SCAFFOLDS / "where-block"
COMPOSE = SCAFFOLDS / "compose"
ALL_SCAFFOLDS = (FLAVORED, COMPILE, WHERE_BLOCK, COMPOSE)
RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"

GRADLE_TIMEOUT = 300
# A connected run in a fresh copy compiles the Compose app and its test APK,
# installs both and runs instrumentation: minutes, not seconds.
CONNECTED_TIMEOUT = 1800

# ``GradleFixture.android_serial`` value meaning "the emulator named by
# --serial". Any other value is passed to Gradle verbatim.
SERIAL_FROM_ARGS = "<--serial>"

_INSTRUMENTED_CLASS_ARG = "-Pandroid.testInstrumentationRunnerArguments.class="
_INSTRUMENTED_PACKAGE = "com.example.composefixture"

# What a fixture's ``args`` may contain, enumerated rather than screened.
# Gradle expands camelCase abbreviations (``cDAT`` is connectedDebugAndroidTest)
# and resolves miscased names, so no pattern over task names can say which ones
# reach a device; a closed list can. _args_refusal() refuses, quoting it, any
# element not listed here, before Gradle runs at all. A new fixture adds its
# task here, fully spelled and module-qualified exactly as it appears in args.
#
# Some entries are deliberately not real tasks -- a missing variant, an
# ambiguous abbreviation, a miscased name -- because Gradle's answer to them is
# the recording. They run against the flavored scaffold, which applies no
# Android plugin and so has no task that can reach a device.
KNOWN_TASKS = frozenset(
    {
        ":app:assembleNoSuchVariantAtAll",
        ":app:assembleProstaging",
        ":app:assembleProstagingdebug",
        ":app:assembleProStagingDebug",
        ":kotlin-error:compileKotlin",
        ":kotlin-warning:compileKotlin",
        ":java-error:compileJava",
        ":java-warning:compileJava",
        ":junit-error:test",
        "help",
        ":app:testProStagingDebugUnitTest",
        ":app:properties",
        ":nope:properties",
        ":app:connectedDebugAndroidTest",
    }
)
# The known tasks that install on or run against a device: a fixture running
# one MUST pin ANDROID_SERIAL.
DEVICE_TASKS = frozenset({":app:connectedDebugAndroidTest"})
# Flags taking no value, and flags whose NEXT element is their value. The
# recorder's own --console=plain and --rerun-tasks are added by _run(), not
# taken from args.
KNOWN_FLAGS = frozenset({"-q", "--rerun"})
KNOWN_VALUE_FLAGS = frozenset({"--property"})
# -P project properties, by the text up to and including '='. A property cannot
# select a task, but "-P..." would also admit AGP's injected options, some of
# which change what a device run does, so these are enumerated too.
KNOWN_PROPERTY_PREFIXES = (_INSTRUMENTED_CLASS_ARG,)
assert DEVICE_TASKS <= KNOWN_TASKS


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
        gradle_below: Only a Gradle older than this version produces the
            output this fixture is about (``"7.6"`` for an option 7.6 added).
            Skipped by a recording made with a newer Gradle.
        settings_append: Text appended to ``settings.gradle`` before the run.
            When set (or ``make_dirs`` is), the run happens in a throwaway COPY
            of the scaffold -- the checked-in scaffold is never edited -- and
            the copy's path is scrubbed to ``/scaffold`` like the scaffold's.
        make_dirs: Directories, relative to the copy, created before the run.
        fresh_copy: Run in a throwaway copy even with no settings edit, so
            nothing a previous build left in the scaffold (a results file, an
            up-to-date task) can stand in for this run's output.
        android_serial: ``ANDROID_SERIAL`` for the run, set in the child's
            environment. ``SERIAL_FROM_ARGS`` takes the recorder's ``--serial``
            (refused unless an ``emulator-NNNN``); any other value is used
            verbatim. Mandatory for a task that reaches a device.
        timeout: Seconds before the run is killed.
        expect_success: When not None, the exit status the run must have for
            the capture to count -- a "passing" run that failed because the
            emulator went away is not a recording of a passing run.
        results_glob: Glob, relative to the copy, for the JUnit XML the run
            wrote. Deliberately a search, not a path: where AGP writes it is
            part of what is being recorded. Exactly one match is required; it
            is recorded as ``results_fixture`` and its relative path goes into
            the manifest as ``results_path``.
        results_fixture: Name the ``results_glob`` match is recorded under.
        tree_root: Directory, relative to the copy, whose listing is recorded
            as ``tree_fixture`` so the output layout is ground truth too.
        tree_fixture: Name the ``tree_root`` listing is recorded under.
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
    gradle_below: str | None = None
    settings_append: str | None = None
    make_dirs: tuple[str, ...] = ()
    fresh_copy: bool = False
    android_serial: str | None = None
    timeout: int = GRADLE_TIMEOUT
    expect_success: bool | None = None
    results_glob: str | None = None
    results_fixture: str | None = None
    tree_root: str | None = None
    tree_fixture: str | None = None


# build_and_test asks Gradle where a --module lives instead of reading the
# settings script (a scanner lost to Groovy/Kotlin syntax four rounds running).
# These record the one question it asks, as the builder asks it (``-q``), in
# every shape the answer comes in.
_PROJECT_DIR_QUERY = ["-q", ":app:properties", "--property", "projectDir"]

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
    GradleFixture(
        name="gradle_rerun_unknown_option",
        args=[":app:testProStagingDebugUnitTest", "--rerun"],
        requires_in_log="Unknown command-line option '--rerun'",
        gradle_below="7.6",
        description=(
            "Gradle older than 7.6 asked for a task's --rerun, which build_and_test "
            "passes to every test task so that an UP-TO-DATE task cannot leave "
            "an older run's reports to be read as this run's (N6). The option "
            "arrived in 7.6; before it Gradle stops in the '* What went wrong:' "
            "block with 'Problem configuring task ... from command line.' and "
            "'> Unknown command-line option '--rerun'.', exit 1. Recorded so the "
            "skill can say why, instead of a bare Build: FAILED."
        ),
        catches=("N6",),
    ),
    GradleFixture(
        name="gradle_properties_project_dir",
        args=_PROJECT_DIR_QUERY,
        requires_in_log="projectDir: ",
        description=(
            "The module-directory question build_and_test asks for --module, against "
            "a settings script with no projectDir override. Even under -q Gradle "
            "prints a dashed `Project ':app'` banner first, then exactly one "
            "`projectDir: <absolute path>` line -- the conventional app/ directory."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="gradle_properties_project_dir_remapped",
        args=_PROJECT_DIR_QUERY,
        requires_in_log="projectDir: ",
        settings_append="project(':app').projectDir = file('mobile')\n",
        make_dirs=("mobile",),
        description=(
            "The same question on a copy of the scaffold whose settings.gradle ends "
            "with `project(':app').projectDir = file('mobile')` and has a mobile/ "
            "directory. Gradle answers mobile/: the directory the test task writes "
            "its reports under, and so the one cleanup and collection must use."
        ),
        catches=("W5-G",),
    ),
    # The four settings constructs the PR #41 review used to defeat the static
    # scanner, each appended to a copy of the scaffold. Gradle's answer is the
    # ground truth a scanner kept getting wrong.
    GradleFixture(
        name="gradle_properties_slashy_dead_branch",
        args=_PROJECT_DIR_QUERY,
        requires_in_log="projectDir: ",
        settings_append=(
            "if (false) {\n"
            "    def ignored = /}/\n"
            "    project(':app').projectDir = file('mobile')\n"
            "}\n"
        ),
        make_dirs=("mobile",),
        description=(
            "A remap inside `if (false) { ... }` behind a slashy string `/}/` that "
            "desynchronised the scanner's brace depth. Never executed: Gradle "
            "answers app/."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="gradle_properties_unbraced_dead_branch",
        args=_PROJECT_DIR_QUERY,
        requires_in_log="projectDir: ",
        settings_append="if (false)\n    project(':app').projectDir = file('mobile')\n",
        make_dirs=("mobile",),
        description=(
            "A remap as the unbraced body of `if (false)`: at brace depth zero, "
            "yet never executed. Gradle answers app/."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="gradle_properties_interpolated_module",
        args=_PROJECT_DIR_QUERY,
        requires_in_log="projectDir: ",
        settings_append='def name = \'app\'\nproject(":$name").projectDir = file("mobile")\n',
        make_dirs=("mobile",),
        description=(
            'A remap whose module path is a GString, `project(":$name")` with '
            "name = 'app'. The scanner stored ':$name' and kept app/; Gradle "
            "answers mobile/."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="gradle_properties_set_project_dir",
        args=_PROJECT_DIR_QUERY,
        requires_in_log="projectDir: ",
        settings_append="project(':app').setProjectDir(file('mobile'))\n",
        make_dirs=("mobile",),
        description=(
            "A remap through the setter, `setProjectDir(...)`, which has no "
            "`projectDir` token for a scanner to find. Gradle answers mobile/."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="gradle_properties_unknown_project",
        args=["-q", ":nope:properties", "--property", "projectDir"],
        requires_in_log="not found in root project",
        description=(
            "The question asked about a module the build does not have: exit 1 and "
            "a '* What went wrong:' block, no projectDir line. The wording differs "
            'by version -- 8.13 says "Cannot locate tasks that match '
            "':nope:properties' as project 'nope' not found in root project ...\", "
            "7.5.1 says \"Project 'nope' not found in root project ...\"."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="gradle_properties_no_such_property",
        args=["-q", ":app:properties", "--property", "noSuchPropertyAnywhere"],
        requires_in_log="noSuchPropertyAnywhere: null",
        description=(
            "A property filter that matches nothing still exits 0, prints the "
            "banner and `noSuchPropertyAnywhere: null` -- an honest successful "
            "answer with no projectDir line in it, which the builder must refuse "
            "rather than fall back to a guessed directory."
        ),
        catches=("W5-G",),
    ),
    GradleFixture(
        name="connected_android_test_success",
        args=[
            ":app:connectedDebugAndroidTest",
            f"{_INSTRUMENTED_CLASS_ARG}{_INSTRUMENTED_PACKAGE}.PassingInstrumentedTest",
        ],
        scaffold=COMPOSE,
        fresh_copy=True,
        android_serial=SERIAL_FROM_ARGS,
        timeout=CONNECTED_TIMEOUT,
        expect_success=True,
        requires_in_log="> Task :app:connectedDebugAndroidTest",
        results_glob="app/build/**/TEST-*.xml",
        results_fixture="junit_xml_instrumented_passing",
        tree_root="app/build/outputs/androidTest-results",
        tree_fixture="instrumented_results_tree",
        description=(
            "connectedDebugAndroidTest on one emulator, filtered by the AGP "
            "runner argument to the one-test passing class. The console output "
            "of a green instrumented run, from a cold build of the scaffold."
        ),
    ),
    GradleFixture(
        name="connected_android_test_failure",
        args=[
            ":app:connectedDebugAndroidTest",
            f"{_INSTRUMENTED_CLASS_ARG}{_INSTRUMENTED_PACKAGE}.FailingInstrumentedTest",
        ],
        scaffold=COMPOSE,
        fresh_copy=True,
        android_serial=SERIAL_FROM_ARGS,
        timeout=CONNECTED_TIMEOUT,
        expect_success=False,
        requires_in_log="> Task :app:connectedDebugAndroidTest",
        results_glob="app/build/**/TEST-*.xml",
        results_fixture="junit_xml_instrumented_failing",
        description=(
            "connectedDebugAndroidTest filtered to the mixed class: one test "
            'passes, one calls fail("recorded failure"). The console output '
            "of a red instrumented run."
        ),
    ),
    GradleFixture(
        name="connected_android_test_no_device",
        args=[
            ":app:connectedDebugAndroidTest",
            f"{_INSTRUMENTED_CLASS_ARG}{_INSTRUMENTED_PACKAGE}.PassingInstrumentedTest",
        ],
        scaffold=COMPOSE,
        fresh_copy=True,
        android_serial="no-such-serial-xyz",
        timeout=CONNECTED_TIMEOUT,
        expect_success=False,
        requires_in_log="> Task :app:connectedDebugAndroidTest",
        description=(
            "The same task with ANDROID_SERIAL naming a device that does not "
            "exist: what an agent sees when the emulator it asked for is gone. "
            "Needs no emulator."
        ),
    ),
]


def _version_tuple(version: str) -> tuple[int, ...]:
    """'7.5.1' -> (7, 5, 1); a suffix such as '-rc-1' is ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", maxsplit=1)[0]))


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


def _prepare_copy(fixture: GradleFixture, workdir: Path) -> Path:
    """Copy the scaffold into ``workdir`` and apply the fixture's settings edit."""
    copy = workdir / fixture.scaffold.name
    shutil.copytree(
        fixture.scaffold, copy, ignore=shutil.ignore_patterns(".gradle", "build", ".kotlin")
    )
    if fixture.settings_append is not None:
        settings = copy / "settings.gradle"
        settings.write_text(
            settings.read_text(encoding="utf-8") + fixture.settings_append, encoding="utf-8"
        )
    for directory in fixture.make_dirs:
        (copy / directory).mkdir(parents=True, exist_ok=True)
    return copy


def _args_refusal(fixture: GradleFixture) -> str | None:
    """Why this fixture's ``args`` must not reach Gradle, or None when they may.

    Every element must be a known flag, the value following a known value flag,
    a known ``-P`` property, or a task in ``KNOWN_TASKS``; a task in
    ``DEVICE_TASKS`` additionally needs ``android_serial``. Anything else is
    refused with its text quoted -- an abbreviation of a known task included,
    which Gradle would otherwise expand and run.
    """
    awaiting_value_of: str | None = None
    for arg in fixture.args:
        if awaiting_value_of is not None:
            awaiting_value_of = None
        elif arg in KNOWN_VALUE_FLAGS:
            awaiting_value_of = arg
        elif arg in KNOWN_FLAGS or arg.startswith(KNOWN_PROPERTY_PREFIXES):
            continue
        elif arg not in KNOWN_TASKS:
            return (
                f"{fixture.name}: {arg!r} is not a known task, flag or property "
                f"(a new task goes into KNOWN_TASKS, fully spelled)"
            )
        elif arg in DEVICE_TASKS and fixture.android_serial is None:
            return (
                f"{fixture.name}: {arg!r} reaches a device and no android_serial "
                f"is pinned, so it would run on every attached device"
            )
    if awaiting_value_of is not None:
        return f"{fixture.name}: {awaiting_value_of!r} is missing its value"
    return None


def _args_refusals(fixtures: list[GradleFixture]) -> list[str]:
    """``_args_refusal`` for every fixture that has one."""
    return [why for why in map(_args_refusal, fixtures) if why is not None]


def _serial_refusal(serial: str | None, selected: list[GradleFixture]) -> str | None:
    """Why this run must not start, or None when it may.

    Mirrors ``record_fixtures._physical_target_refusal`` but is stricter in
    one respect: an ``ANDROID_SERIAL`` already in the shell does not count.
    The serial is required on the command line, so what a run targets is in
    the command that started it rather than in whatever terminal it came from.
    """
    needing = sorted(f.name for f in selected if f.android_serial == SERIAL_FROM_ARGS)
    if not needing:
        return None
    if serial and serial.startswith("emulator-"):
        return None
    shown = repr(serial) if serial else "not given"
    return (
        f"Refusing to record: --serial is {shown}, not an emulator, and "
        f"{len(needing)} selected fixture(s) install APKs on the device and run "
        f"instrumentation there ({', '.join(needing)}). Pass --serial "
        f"emulator-NNNN, or select only fixtures that need no device with --only."
    )


def _environment(fixture: GradleFixture, serial: str | None) -> dict[str, str] | None:
    """The child's environment: inherited, plus ANDROID_SERIAL when pinned."""
    if fixture.android_serial is None:
        return None
    value = serial if fixture.android_serial == SERIAL_FROM_ARGS else fixture.android_serial
    if not value:
        raise RuntimeError(f"{fixture.name}: no serial to pin (refusal check skipped?)")
    return {**os.environ, "ANDROID_SERIAL": value}


def _run(
    gradle: str,
    fixture: GradleFixture,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Run one fixture's Gradle invocation; return combined output and exit code.

    Failure output is the point of most of these fixtures, so a non-zero exit is
    expected and not an error. ``--console=plain`` matches what the skill itself
    passes, so the recorded text is what the parser would really see.
    """
    flags = ["--console=plain"]
    if fixture.rerun:
        flags.append("--rerun-tasks")
    result = subprocess.run(
        [gradle, *flags, *fixture.args],
        cwd=cwd or fixture.scaffold,
        env=env,
        capture_output=True,
        text=True,
        timeout=fixture.timeout,
        check=False,
    )
    return result.stdout + result.stderr, result.returncode


def _tree_listing(copy: Path, root: Path) -> str:
    """Every path under ``root``, relative to ``copy``; directories end in '/'."""
    lines = [f"{root.relative_to(copy).as_posix()}/"]
    for path in sorted(root.rglob("*")):
        suffix = "/" if path.is_dir() else ""
        lines.append(f"{path.relative_to(copy).as_posix()}{suffix}")
    return "\n".join(lines) + "\n"


def _collect_outputs(fixture: GradleFixture, copy: Path) -> dict[str, tuple[str, str]]:
    """Files the run wrote that are recorded alongside its log.

    Returns ``{"results": (relative path, text), "tree": (root, listing)}``,
    either key absent when the fixture does not ask for it. Raises ValueError
    when what was asked for is not there, or is not exactly one file.
    """
    outputs: dict[str, tuple[str, str]] = {}
    if fixture.results_glob is not None:
        found = sorted(path for path in copy.glob(fixture.results_glob) if path.is_file())
        if len(found) != 1:
            listed = [path.relative_to(copy).as_posix() for path in found]
            raise ValueError(f"{fixture.results_glob} matched {len(found)} file(s): {listed}")
        outputs["results"] = (
            found[0].relative_to(copy).as_posix(),
            found[0].read_text(encoding="utf-8"),
        )
    if fixture.tree_root is not None:
        root = copy / fixture.tree_root
        if not root.is_dir():
            raise ValueError(f"{fixture.tree_root} was not created")
        outputs["tree"] = (fixture.tree_root, _tree_listing(copy, root))
    return outputs


def _scrub(text: str, extra_roots: tuple[Path, ...] = ()) -> str:
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

    A connected run is the first to take over a minute (``in 2m 5s``) and the
    first to print paths outside the scaffold (the Android SDK's), so any
    remaining home-directory prefix is reduced to ``~`` last of all.
    """
    text = re.sub(r"in \d+m \d+s\b", "in 0s", text)
    text = re.sub(r"in \d+m?s\b", "in 0s", text)
    text = re.sub(r"\d+ actionable task", "1 actionable task", text)
    text = re.sub(r'hostname="[^"]*"', 'hostname="build-host"', text)
    text = re.sub(r'timestamp="[^"]*"', 'timestamp="2020-01-01T00:00:00Z"', text)
    text = re.sub(r'time="[\d.]+"', 'time="0.0"', text)
    # A throwaway copy is scrubbed under both spellings: Gradle prints the
    # canonical path, which on macOS is /private/var/... for a /var/... tempdir.
    # Longest first, so /var/... cannot rewrite the middle of /private/var/...
    roots = {str(root) for root in extra_roots} | {str(root.resolve()) for root in extra_roots}
    for root in (*sorted(roots, key=len, reverse=True), *map(str, ALL_SCAFFOLDS)):
        text = text.replace(root, "/scaffold")
    return text.replace(str(Path.home()), "~")


def _sidecar_parents() -> dict[str, str]:
    """Recorded names that are written by another fixture's run, to that fixture."""
    parents = {}
    for fixture in FIXTURES:
        for sidecar in (fixture.results_fixture, fixture.tree_fixture):
            if sidecar is not None:
                parents[sidecar] = fixture.name
    return parents


def _ordered_names() -> list[str]:
    """Manifest order: FIXTURES order, each run's sidecars right after it."""
    order = []
    for fixture in FIXTURES:
        order.append(fixture.name)
        order.extend(s for s in (fixture.results_fixture, fixture.tree_fixture) if s is not None)
    return order


def _fail(name: str, why: str, log: str) -> int:
    print(f"  FAIL  {name}: {why}. The build printed:\n{log[-2000:]}", file=sys.stderr)
    return 1


def record(gradle: str, only: set[str] | None = None, serial: str | None = None) -> int:
    """Record the Gradle fixtures (all, or ``only`` those) and write the manifest."""
    for scaffold in ALL_SCAFFOLDS:
        if not scaffold.exists():
            print(f"scaffold missing: {scaffold}", file=sys.stderr)
            return 1

    parents = _sidecar_parents()
    known = {fixture.name for fixture in FIXTURES} | set(parents)
    if only is not None and only - known:
        print(f"unknown fixture(s): {sorted(only - known)}", file=sys.stderr)
        return 1
    if only is not None:
        # A sidecar is written by its parent's run, so asking for one runs that.
        only = {parents.get(name, name) for name in only}

    # Every fixture, not only the selected ones: a bad entry is a bug in this
    # file whichever fixture is asked for. Nothing has been run yet.
    refusals = _args_refusals(FIXTURES)
    if refusals:
        print("Refusing to record:\n  " + "\n  ".join(refusals), file=sys.stderr)
        return 2

    version = _gradle_version(gradle)
    out_dir = RECORDED_ROOT / f"gradle-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = []
    for fixture in FIXTURES:
        if only is not None and fixture.name not in only:
            continue
        if fixture.gradle_below is not None and _version_tuple(version) >= _version_tuple(
            fixture.gradle_below
        ):
            if only is not None:
                print(
                    f"  FAIL  {fixture.name} needs a Gradle older than "
                    f"{fixture.gradle_below}; this is {version}",
                    file=sys.stderr,
                )
                return 1
            print(f"  skip  {fixture.name} (needs Gradle < {fixture.gradle_below})")
            continue
        selected.append(fixture)

    refusal = _serial_refusal(serial, selected)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    if any(f.scaffold == COMPOSE for f in selected) and not (
        os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    ):
        print("The compose scaffold builds with AGP: set ANDROID_HOME.", file=sys.stderr)
        return 1

    entries = []
    for fixture in selected:
        produced = _artifact_path(fixture)
        # Delete the target BEFORE the build. Otherwise a build that dies during
        # configuration leaves the previous run's file in place and this
        # recorder publishes stale bytes as if the run had just written them --
        # a fixture that looks like ground truth and is not, which is the one
        # thing this tool exists to prevent.
        if produced is not None and produced.exists():
            produced.unlink()
        env = _environment(fixture, serial)
        started_at = time.time()
        copies: tuple[Path, ...] = ()
        outputs: dict[str, tuple[str, str]] = {}
        collect_error: str | None = None
        if fixture.settings_append is not None or fixture.make_dirs or fixture.fresh_copy:
            with tempfile.TemporaryDirectory(prefix="gradle-fixture-") as workdir:
                copy = _prepare_copy(fixture, Path(workdir))
                copies = (copy,)
                raw, exit_code = _run(gradle, fixture, cwd=copy, env=env)
                wall_seconds = round(time.time() - started_at, 1)
                log = _scrub(raw, copies)
                # Read before the copy is deleted; judged after the log is.
                try:
                    outputs = _collect_outputs(fixture, copy)
                except ValueError as exc:
                    collect_error = str(exc)
        else:
            log, exit_code = _run(gradle, fixture, env=env)
            wall_seconds = round(time.time() - started_at, 1)

        if fixture.requires_in_log is not None and fixture.requires_in_log not in log:
            return _fail(
                fixture.name,
                f"the build output does not contain {fixture.requires_in_log!r}, "
                f"so the producing task did not run",
                log,
            )
        if fixture.expect_success is not None and (exit_code == 0) != fixture.expect_success:
            wanted = "succeed" if fixture.expect_success else "fail"
            return _fail(fixture.name, f"expected the run to {wanted}, exit {exit_code}", log)
        if collect_error is not None:
            return _fail(fixture.name, collect_error, log)

        if produced is None:
            text = _scrub(log, copies)
        else:
            # The build had to run to produce it, but what gets recorded is the
            # file the build WROTE, not what it printed.
            if not produced.exists():
                return _fail(fixture.name, f"{fixture.artifact} was not produced", log)
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
        common = {
            "command": (
                "gradle --console=plain "
                + ("--rerun-tasks " if fixture.rerun else "")
                + " ".join(fixture.args)
            ),
            "scaffold": f"tests/fixtures/scaffold/{fixture.scaffold.name}",
            **(
                {
                    "setup": (
                        "run in a temporary copy of the scaffold"
                        + (
                            f"; appended to settings.gradle: {fixture.settings_append!r}"
                            if fixture.settings_append is not None
                            else ""
                        )
                        + (
                            f"; created: {', '.join(fixture.make_dirs)}"
                            if fixture.make_dirs
                            else ""
                        )
                    )
                }
                if copies
                else {}
            ),
            # The lane budget for a device run is set from this, so only those
            # carry it; elsewhere it would only make every re-record a diff.
            **(
                {
                    "environment": {"ANDROID_SERIAL": env["ANDROID_SERIAL"]},
                    "wall_seconds": wall_seconds,
                }
                if env is not None
                else {}
            ),
        }
        entries.append(
            {
                "name": fixture.name,
                "file": path.name,
                **common,
                "exit_code": exit_code,
                "produced_artifact": fixture.artifact,
                **({"results_path": outputs["results"][0]} if "results" in outputs else {}),
                "description": fixture.description,
                "catches": list(fixture.catches),
                "bytes": len(text.encode("utf-8")),
            }
        )
        print(f"  ok    {path.name} ({len(text.encode('utf-8'))} bytes, exit {exit_code})")

        sidecars = []
        if "results" in outputs:
            results_path, xml = outputs["results"]
            sidecars.append(
                (
                    fixture.results_fixture,
                    "xml",
                    _scrub(xml, copies),
                    {"results_path": results_path},
                    f"The JUnit XML {fixture.name} wrote. Found by searching "
                    f"{fixture.results_glob!r} after the run, not by assuming a "
                    f"path: results_path is where AGP put it.",
                )
            )
        if "tree" in outputs:
            tree_root, listing = outputs["tree"]
            sidecars.append(
                (
                    fixture.tree_fixture,
                    "txt",
                    listing,
                    {"tree_root": tree_root},
                    f"Every path under {tree_root}/ after {fixture.name}, so the "
                    f"layout AGP writes is ground truth rather than an assumption.",
                )
            )
        for name, ext, body, extra, description in sidecars:
            side_path = out_dir / f"{name}.{ext}"
            side_path.write_text(body, encoding="utf-8")
            entries.append(
                {
                    "name": name,
                    "file": side_path.name,
                    **common,
                    "produced_by": fixture.name,
                    **extra,
                    "description": description,
                    "catches": [],
                    "bytes": len(body.encode("utf-8")),
                }
            )
            print(f"  ok    {side_path.name} ({len(body.encode('utf-8'))} bytes)")

    if only is not None and (out_dir / "MANIFEST.json").exists():
        # Merge: what was not re-recorded keeps its entry, in FIXTURES order.
        previous = json.loads((out_dir / "MANIFEST.json").read_text(encoding="utf-8"))
        recorded = {entry["name"]: entry for entry in previous.get("fixtures", [])}
        recorded.update({entry["name"]: entry for entry in entries})
        order = _ordered_names()
        entries = sorted(
            recorded.values(),
            key=lambda e: order.index(e["name"]) if e["name"] in order else len(order),
        )

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
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="Record only this fixture (repeatable); merges into the existing manifest",
    )
    parser.add_argument(
        "--serial",
        help=(
            "Emulator for the connected fixtures (emulator-NNNN; anything else is "
            "refused). Passed to Gradle as ANDROID_SERIAL."
        ),
    )
    args = parser.parse_args()

    if args.list:
        width = max(len(f.name) for f in FIXTURES)
        for fixture in FIXTURES:
            catches = f"  [catches {', '.join(fixture.catches)}]" if fixture.catches else ""
            device = "  [needs --serial]" if fixture.android_serial == SERIAL_FROM_ARGS else ""
            print(f"{fixture.name:<{width}}  gradle {' '.join(fixture.args)}{catches}{device}")
            for sidecar in (fixture.results_fixture, fixture.tree_fixture):
                if sidecar is not None:
                    print(f"  {sidecar}  (written by {fixture.name})")
        return

    sys.exit(record(args.gradle, set(args.only) if args.only else None, args.serial))


if __name__ == "__main__":
    main()
