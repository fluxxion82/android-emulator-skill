#!/usr/bin/env python3
"""Record verbatim Gradle output into tests/fixtures/recorded/gradle-<version>/.

Companion to ``record_fixtures.py`` (which records adb output). Same rule:
**parser tests consume these files; they never inline Gradle output as a string
literal.**

Gradle failure text is the thing the skill most needs to parse and currently
does not — ``gradle/results.py`` defines ``_BUILD_FAILED_RE`` and never uses it,
so a configuration or task-lookup failure is reported as
``Build: FAILED (0 errors, 0 warnings)`` with no diagnostic at all.

Fixtures are produced from ``tests/fixtures/scaffold/flavored``, a checked-in
project whose task names mimic AGP's ``buildType x productFlavor`` camelCase
(``assembleProStagingDebug``). Real AGP is not needed: the failures recorded
here come from Gradle's own task lookup and error reporting, so plain Gradle
reproduces them exactly and the scaffold stays buildable anywhere.

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

SCAFFOLD = Path(__file__).resolve().parent / "fixtures" / "scaffold" / "flavored"
RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"

GRADLE_TIMEOUT = 300


@dataclass(frozen=True)
class GradleFixture:
    """One recorded Gradle invocation."""

    name: str
    args: list[str]
    description: str
    catches: tuple[str, ...] = ()


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


def _run(gradle: str, args: list[str]) -> str:
    """Run Gradle in the scaffold and return combined output.

    Failure output is the point of most of these fixtures, so a non-zero exit is
    expected and not an error. ``--console=plain`` matches what the skill itself
    passes, so the recorded text is what the parser would really see.
    """
    result = subprocess.run(
        [gradle, "--console=plain", *args],
        cwd=SCAFFOLD,
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
    """
    text = re.sub(r"in \d+m?s\b", "in 0s", text)
    text = re.sub(r"\d+ actionable task", "1 actionable task", text)
    return text.replace(str(SCAFFOLD), "/scaffold")


def record(gradle: str) -> int:
    """Record every Gradle fixture and write the manifest."""
    if not SCAFFOLD.exists():
        print(f"scaffold missing: {SCAFFOLD}", file=sys.stderr)
        return 1

    version = _gradle_version(gradle)
    out_dir = RECORDED_ROOT / f"gradle-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for fixture in FIXTURES:
        text = _scrub(_run(gradle, fixture.args))
        path = out_dir / f"{fixture.name}.txt"
        path.write_text(text, encoding="utf-8")
        entries.append(
            {
                "name": fixture.name,
                "file": path.name,
                "command": "gradle --console=plain " + " ".join(fixture.args),
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
        "scaffold": "tests/fixtures/scaffold/flavored",
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
