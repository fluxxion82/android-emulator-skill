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

import functools
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


def test_a_mismatched_version_cannot_ship_an_artifact():
    """Version consistency must GATE packaging, not merely report on it.

    `validate-version.yml` runs on the same `release: published` trigger as
    `release.yml`, but as a separate workflow -- so tagging v0.6.0 with the
    manifests still saying 0.5.0 produced a red check BESIDE a successfully
    uploaded zip. The check reported the problem after the artifact had
    shipped, which is the same shape as a test that runs after the thing it
    was meant to prevent.
    """
    import yaml

    release = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    needs = release["jobs"]["package"].get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs

    assert "version" in needs, (
        "packaging does not require the version check, so a tag that disagrees "
        "with the manifests can still upload a release asset"
    )

    steps = release["jobs"]["version"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    for manifest in ("SKILL.md", "plugin.json", "marketplace.json", "pyproject.toml"):
        assert manifest in commands, f"the version gate does not check {manifest}"


def test_the_release_zip_excludes_bytecode_directories():
    """`-x "__pycache__/*"` misses nested and empty __pycache__ entries.

    Not a CI blocker -- a fresh checkout has none -- but the exclusion did not
    do what it said, and a zip built on a developer machine carried empty
    `scripts/__pycache__/` entries.
    """
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert '-x "*/__pycache__/*"' in release, "nested __pycache__ is not excluded"


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


def test_commit_trailers_are_rejected_by_a_hook_not_just_a_style_note():
    """The no-trailers rule must stay enforced, not merely documented.

    Adding `Co-Authored-By` is a default in several tools, so the rule gets
    re-broken by things that are not paying attention -- it has already come
    back once mid-session after being agreed. A note in CLAUDE.md cannot stop
    that; a commit-msg hook can. Guarded here because the hook is a few lines
    of YAML away from being deleted.
    """
    import yaml

    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo.get("hooks", [])]
    trailer_hook = next((h for h in hooks if h.get("id") == "no-commit-trailers"), None)

    assert trailer_hook is not None, "the commit-msg trailer hook is gone"
    assert "commit-msg" in trailer_hook.get(
        "stages", []
    ), "the trailer hook no longer runs at the commit-msg stage, so it never fires"
    assert (REPO_ROOT / ".github" / "scripts" / "check-commit-trailers.py").exists()


def test_the_trailer_check_actually_rejects_a_trailer(tmp_path):
    """A hook that accepts everything would satisfy the test above."""
    import subprocess
    import sys

    script = REPO_ROOT / ".github" / "scripts" / "check-commit-trailers.py"

    bad = tmp_path / "bad"
    bad.write_text("feat: x\n\nBody.\n\nCo-Authored-By: A <a@b.c>\n", encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(script), str(bad)], capture_output=True, text=True, check=False
    )
    assert rejected.returncode != 0, "a Co-Authored-By trailer was accepted"

    good = tmp_path / "good"
    good.write_text("feat: x\n\nBody.\n", encoding="utf-8")
    accepted = subprocess.run(
        [sys.executable, str(script), str(good)], capture_output=True, text=True, check=False
    )
    assert accepted.returncode == 0, f"a clean message was rejected: {accepted.stderr}"


def _option_tokens(help_text: str) -> set[str]:
    """The flags argparse actually registered, from its own --help output.

    Only tokens in the leading option column of a line count; anything in a
    help *description* is prose, not a flag.

    Descriptions wrap, and a wrapped line can *begin* with something that looks
    like a flag -- `anr_watcher`'s reads "... via 'logcat\n-d -t'", and
    `test_recorder`'s reads "--clear: only delete sessions older than". Taking
    those would have put `-d` into the set of flags anr_watcher accepts, which
    it does not, so a doc claiming `-d` would have passed. argparse indents
    every option entry to the same column and every description deeper, so the
    entries are exactly the lines at the shallowest of those indents.
    """
    candidates: list[tuple[int, str]] = []
    for line in help_text.splitlines():
        if not line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        candidates.append((len(line) - len(line.lstrip()), stripped))
    if not candidates:
        return set()

    option_column = min(indent for indent, _ in candidates)
    tokens: set[str] = set()
    for indent, stripped in candidates:
        if indent != option_column:
            continue
        # "  --json, -j METAVAR   description..." -> the part before 2+ spaces.
        column = re.split(r"\s{2,}", stripped)[0]
        for piece in column.split(","):
            flag = piece.strip().split("=")[0].split(" ")[0]
            if flag.startswith("-"):
                tokens.add(flag)
    return tokens


@functools.cache
def _help_text(script: str) -> str:
    """`--help` for one script. Cached: several guards below ask for the same one."""
    import subprocess
    import sys

    helped = subprocess.run(
        [sys.executable, script, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert helped.returncode == 0, f"{Path(script).name} --help failed: {helped.stderr[:200]}"
    return helped.stdout


_SCRIPT_ENTRY = re.compile(r"^\d+\.\s+\*\*([A-Za-z_]\w*\.py)\*\*")
_BACKTICKED = re.compile(r"`([^`]*)`")
_FLAG = re.compile(r"--?[A-Za-z][\w-]*")
_OPTIONS_BULLET = "- Options:"


def _documented_options(markdown: str) -> dict[str, set[str]]:
    """{script: flags} from SKILL.md's `- Options:` bullets.

    Structural on the doc side as well as the argparse side, and the two rules
    that make it so are the whole point:

    * Only an `- Options:` bullet is read -- not the entry's prose bullets.
    * Within that bullet only *backticked* tokens count.

    Three entries currently explain a flag that deliberately does not exist
    ("there is no ``--list``", "There is no ``--text``", "the old
    ``--list-channels`` never worked"). A guard that scanned an entry for
    backticked flags, or the file for the string, would read those denials as
    claims and fail on documentation that is correct -- and the repair for that
    failure is to delete the honest sentence.
    """
    lines = markdown.splitlines()
    documented: dict[str, set[str]] = {}
    unattached: list[int] = []
    current: str | None = None

    index = 0
    while index < len(lines):
        line = lines[index]
        entry = _SCRIPT_ENTRY.match(line)
        if entry:
            current = entry.group(1)

        if not line.strip().startswith(_OPTIONS_BULLET):
            index += 1
            continue

        body = line.strip()[len(_OPTIONS_BULLET) :]
        indent = len(line) - len(line.lstrip())
        cursor = index + 1
        while cursor < len(lines):  # the bullet wraps; a wrap is indented deeper
            wrapped = lines[cursor]
            stripped = wrapped.strip()
            if not stripped or stripped.startswith("-"):
                break
            if len(wrapped) - len(wrapped.lstrip()) <= indent:
                break
            body += " " + stripped
            cursor += 1

        if current is None:
            unattached.append(index + 1)
        else:
            flags = {flag for chunk in _BACKTICKED.findall(body) for flag in _FLAG.findall(chunk)}
            documented.setdefault(current, set()).update(flags)
        index = cursor

    assert not unattached, f"SKILL.md has an Options: bullet under no script entry: {unattached}"
    return documented


def test_every_script_offers_the_documented_json_contract():
    """CLAUDE.md: "`--help` and `--json` on every script".

    That is the machine-readable contract an agent depends on, and it was true
    of every script but one: `visual_diff.py` reached the same output only as
    `--details`, whose help text reads like a `--verbose`. An agent following
    the documented contract would pass `--json` and get an argparse error.

    Checked by running each script's own `--help` and extracting the OPTION
    TOKENS, not by searching the help text for the string "--json". The first
    version of this test did the latter and was vacuous: `visual_diff`'s
    `--details` help reads "Deprecated alias for --json", so the prose satisfied
    the check and renaming the real flag left it green. That is the same
    substring-versus-structure mistake this repo keeps making -- a guard that
    matches its own documentation.
    """
    scripts = sorted((SKILL_ROOT / "scripts").glob("*.py"))
    assert scripts, "no scripts found"

    missing = [s.name for s in scripts if "--json" not in _option_tokens(_help_text(str(s)))]

    assert not missing, (
        f"these scripts do not offer --json, which CLAUDE.md states every script has: " f"{missing}"
    )


def test_every_flag_documented_in_skill_md_exists():
    """SKILL.md is the interface an agent reads; a flag it invents costs a turn.

    An agent does not run `--help` first -- it reads SKILL.md, picks the flag
    named there, and runs it. So a documented flag that argparse never
    registered is not a typo in a doc, it is a broken API, and the agent's
    reward is `unrecognized arguments` with no hint at the real spelling.

    Five were wrong at once, and each in a different way, which is why the
    check has to be mechanical rather than a careful read:

    * `screen_mapper --list` -- never existed; listing is the default output.
    * `keyboard --text` -- the flag is `--type`; `--text` belongs to a
      different script, so the guess is *plausible*, which is worse.
    * `push_notification --title/--message/--id/--data/--method` -- the script
      was rescoped to what adb can do, and its whole documented flag set was
      left behind describing the version that was removed.
    * `test_recorder --test-name/--output/--inline` -- likewise; it became
      session-based (`--start`/`--step`/`--stop`).
    * `visual_diff` -- documented as having no `--json` after it gained one.

    The last two show the failure mode: the rescoping was deliberate and the
    Status section even describes it, so nobody reading the file top to bottom
    sees a contradiction. Only argparse knows.

    Both sides are extracted structurally -- see `_documented_options` for why
    matching prose instead would fail on the sentences that are *correct*.
    """
    scripts = sorted((SKILL_ROOT / "scripts").glob("*.py"))
    assert scripts, "no scripts found"

    documented = _documented_options(SKILL_MD.read_text(encoding="utf-8"))

    # A script with no Options: bullet is not exempt from the guard, it is
    # invisible to it -- which is exactly how visual_diff and anr_watcher drifted.
    undocumented = [s.name for s in scripts if not documented.get(s.name)]
    assert not undocumented, (
        "these scripts have no `- Options:` bullet with backticked flags in "
        f"SKILL.md, so nothing checks their documented interface: {undocumented}"
    )

    stray = sorted(set(documented) - {s.name for s in scripts})
    assert not stray, f"SKILL.md documents options for scripts that do not exist: {stray}"

    wrong: dict[str, list[str]] = {}
    for script in scripts:
        real = _option_tokens(_help_text(str(script)))
        bogus = sorted(documented[script.name] - real)
        if bogus:
            wrong[script.name] = bogus

    assert not wrong, (
        "SKILL.md documents flags that argparse does not accept; an agent "
        f"following it gets 'unrecognized arguments': {wrong}"
    )
