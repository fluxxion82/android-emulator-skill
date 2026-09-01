"""What the released package actually contains, and what the docs claim.

`release.yml` zips only `android-emulator-skill/`, so anything outside that
directory — the repo README, CLAUDE.md, `references/` — does not reach a
consumer. Docs that point at those from inside the package are broken links for
everyone who installs it.

The invocation problem was worse: every SKILL.md example read
`python scripts/foo.py`, which is a file-not-found unless the current directory
happens to be the skill root. An agent working in a user's project got an error
on the first command. Verified by extracting the release zip and running from a
separate directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "android-emulator-skill"
SKILL_ROOT = PACKAGE_ROOT / "skills" / "android-emulator-skill"
SKILL_MD = SKILL_ROOT / "SKILL.md"


# ---------------------------------------------------------------------------
# Invocation.
# ---------------------------------------------------------------------------


def _code_block_lines(markdown: str) -> list[str]:
    """Lines inside fenced code blocks — the parts a reader will actually run.

    Prose that *describes* the wrong invocation (including the sentence
    explaining this very rule) is not a defect, and a whole-file scan cannot
    tell the two apart.
    """
    lines, inside = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


def test_no_bare_relative_script_invocations_in_skill_md():
    """`python scripts/foo.py` only works if cwd is the skill root."""
    offenders = [
        line.strip()
        for line in _code_block_lines(SKILL_MD.read_text(encoding="utf-8"))
        if re.search(r"python3?\s+scripts/", line)
    ]
    assert not offenders, (
        "SKILL.md documents bare relative invocations, which fail for an "
        f"installed plugin: {offenders[:5]}"
    )


def test_skill_md_shows_how_to_root_the_path():
    """An agent needs to be told what the path is relative to."""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "SKILL_DIR" in text, "no guidance on rooting script paths"


# ---------------------------------------------------------------------------
# Docs must only point at things the consumer receives.
# ---------------------------------------------------------------------------


def test_skill_md_lists_every_script_and_counts_them_correctly():
    """The script inventory must match the filesystem, not a stale memory of it.

    Both numbers in SKILL.md had already drifted: it announced "29 scripts"
    while 27 existed, and "Core Utilities (6 modules)" while `common/` held 9.
    A reader trusting either count reaches for a script that is not there, or
    misses one that is -- and the count is the first thing an agent reads to
    decide what this skill can do.

    Counted from disk rather than from the heading, so adding a script without
    documenting it fails here.
    """
    text = SKILL_MD.read_text(encoding="utf-8")

    scripts = sorted(p.name for p in (SKILL_ROOT / "scripts").glob("*.py"))
    common = sorted(
        p.name for p in (SKILL_ROOT / "scripts" / "common").glob("*.py") if p.name != "__init__.py"
    )

    undocumented = [name for name in scripts if f"**{name}**" not in text]
    assert not undocumented, f"scripts missing from SKILL.md: {undocumented}"

    claimed = re.search(r"### Implemented \((\d+) scripts\)", text)
    assert claimed, "SKILL.md no longer announces a script count"
    assert int(claimed.group(1)) == len(
        scripts
    ), f"SKILL.md claims {claimed.group(1)} scripts; {len(scripts)} exist on disk"

    claimed_common = re.search(r"#### Core Utilities \((\d+) modules", text)
    assert claimed_common, "SKILL.md no longer announces a common-module count"
    assert int(claimed_common.group(1)) == len(
        common
    ), f"SKILL.md claims {claimed_common.group(1)} common modules; {len(common)} exist"


WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _yaml(name: str):
    """Parse a workflow.

    Deliberately NOT `pytest.importorskip("yaml")`: these guards protect the
    release gate, and a guard that skips when a dependency is missing is worth
    nothing. CI installs pyyaml precisely so these run.
    """
    import yaml

    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_a_release_cannot_be_cut_without_running_the_tests():
    """The release used to run zero tests.

    It checked that three files existed, zipped, and uploaded. So the checks
    that decide whether this skill works were not on the path that ships --
    the same process shape that once let three advertised capabilities ship
    completely inert behind 470 passing tests. Guarding it here because the
    gate is one `needs:` line away from being deleted by someone in a hurry.
    """
    release = _yaml("release.yml")

    package = release["jobs"]["package"]
    needs = package.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs

    assert "verify" in needs, "packaging no longer requires lint and unit tests"
    assert "emulator" in needs, "packaging no longer requires the device-backed lane"


def test_the_release_gate_actually_runs_the_suites_it_claims_to():
    """`needs:` on a job that tests nothing would satisfy the test above."""
    release = _yaml("release.yml")

    steps = release["jobs"]["verify"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "pytest" in commands, "the verify job does not run pytest"
    assert "ruff" in commands and "black" in commands, "the verify job does not lint"

    assert release["jobs"]["emulator"]["uses"].endswith(
        "emulator.yml"
    ), "the emulator gate no longer calls the emulator workflow"


def test_the_emulator_workflow_is_callable_as_a_gate():
    """Without `workflow_call` the release cannot require it."""
    emulator = _yaml("emulator.yml")

    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = emulator.get("on", emulator.get(True))
    assert triggers is not None, "emulator.yml has no triggers"
    assert "workflow_call" in triggers, "emulator.yml cannot be required by release.yml"


def test_the_emulator_lane_fails_when_every_test_skips():
    """A lane where everything skipped is not a lane that passed.

    Every emulator test skips politely when no device or fixture app is
    present -- correct on a laptop, useless as a gate. The script must notice.
    """
    script = (REPO_ROOT / ".github" / "scripts" / "run-emulator-lane.sh").read_text(
        encoding="utf-8"
    )
    assert (
        "passed" in script and "exit 1" in script
    ), "the lane script no longer fails when no test actually ran"
    assert "pm list packages" in script, (
        "the lane no longer asserts the fixture app installed; the end-to-end "
        "agent test would skip and the gate would pass having tested nothing"
    )


def test_skill_md_does_not_link_nonexistent_docs():
    """STATUS.md and TESTING.md have never existed in this repo."""
    text = SKILL_MD.read_text(encoding="utf-8")
    for missing in ("STATUS.md", "TESTING.md"):
        assert missing not in text, f"SKILL.md still references {missing}, which does not exist"


def test_package_contains_what_the_docs_promise():
    """`examples/` is promised and ships; `references/` does not ship."""
    assert (SKILL_ROOT / "examples").is_dir(), "examples/ is documented but missing"
    assert not (
        SKILL_ROOT / "references"
    ).exists(), "references/ now ships; SKILL.md says it is repo-only and should be updated"


def test_shipped_readme_is_not_stale():
    """The inner README is the consumer's first impression.

    It sat at "Initial Development (v0.1.0)" with a Planned Scripts list of
    twelve scripts that all existed.
    """
    text = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")
    assert "v0.1.0" not in text
    assert "Planned Scripts" not in text
    assert "Python 3.8" not in text


# ---------------------------------------------------------------------------
# Version, in one place per manifest and all agreeing.
# ---------------------------------------------------------------------------


def _skill_md_version() -> str:
    match = re.search(r"^version:\s*(\S+)", SKILL_MD.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "SKILL.md frontmatter has no version"
    return match.group(1)


def _pyproject_version() -> str:
    match = re.search(
        r'^version\s*=\s*"([^"]+)"',
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "pyproject.toml has no version"
    return match.group(1)


@pytest.mark.parametrize(
    ("label", "getter"),
    [
        (
            "plugin.json",
            lambda: json.loads(
                (PACKAGE_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
            ).get("version"),
        ),
        (
            "marketplace.json",
            lambda: json.loads(
                (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
            )["plugins"][0].get("version"),
        ),
        ("SKILL.md", _skill_md_version),
    ],
)
def test_every_manifest_declares_the_same_version_as_pyproject(label, getter):
    """Both plugin manifests previously had no version at all, so CI could not
    detect drift -- it only checked pyproject.toml and *warned* on SKILL.md."""
    declared = getter()
    assert declared, f"{label} declares no version"
    assert (
        declared == _pyproject_version()
    ), f"{label} says {declared}, pyproject.toml says {_pyproject_version()}"


def test_release_validation_fails_rather_than_warns():
    """A check that only warns cannot keep versions in step."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "validate-version.yml").read_text(
        encoding="utf-8"
    )
    for manifest in ("plugin.json", "marketplace.json", "SKILL.md"):
        assert manifest in workflow, f"release validation does not check {manifest}"
    assert workflow.count("exit 1") >= 4, "version drift should fail the release, not warn"
