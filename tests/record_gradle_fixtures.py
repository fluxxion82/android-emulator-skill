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
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCAFFOLDS = Path(__file__).resolve().parent / "fixtures" / "scaffold"
FLAVORED = SCAFFOLDS / "flavored"
COMPILE = SCAFFOLDS / "compile"
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
    """

    name: str
    args: list[str]
    description: str
    catches: tuple[str, ...] = ()
    scaffold: Path = FLAVORED
    rerun: bool = False


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
    Both scaffold roots are replaced, which also normalises the ``file://`` URI
    K2 emits — ``file:///Users/<someone>/…`` becomes ``file:///scaffold/…`` —
    so a developer's account name never reaches this public repository.
    """
    text = re.sub(r"in \d+m?s\b", "in 0s", text)
    text = re.sub(r"\d+ actionable task", "1 actionable task", text)
    for scaffold in (FLAVORED, COMPILE):
        text = text.replace(str(scaffold), "/scaffold")
    return text


def record(gradle: str) -> int:
    """Record every Gradle fixture and write the manifest."""
    for scaffold in (FLAVORED, COMPILE):
        if not scaffold.exists():
            print(f"scaffold missing: {scaffold}", file=sys.stderr)
            return 1

    version = _gradle_version(gradle)
    out_dir = RECORDED_ROOT / f"gradle-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for fixture in FIXTURES:
        text = _scrub(_run(gradle, fixture))
        path = out_dir / f"{fixture.name}.txt"
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
        "scaffolds": ["tests/fixtures/scaffold/flavored", "tests/fixtures/scaffold/compile"],
        "note": (
            "Durations and absolute paths are normalised so a re-record diffs "
            "only on real format changes."
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
