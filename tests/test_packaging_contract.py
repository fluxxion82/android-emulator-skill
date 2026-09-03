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
# Installation instructions someone can actually run (P8).
#
# Every guard below states which STREAM of the Markdown it reads, because the
# two need different rules and conflating them is how a doc guard becomes a
# grep. `_code_block_lines` is the commands a reader copies: a `<placeholder>`
# there is a defect. `_prose_lines` is the claims the document makes: a
# sentence deferring the instructions is a defect there, and a `<placeholder>`
# is not. Neither guard reads the whole file, and no guard reads this one.
# ---------------------------------------------------------------------------

README = REPO_ROOT / "README.md"
PACKAGED_README = SKILL_ROOT / "README.md"


def _prose_lines(markdown: str) -> list[str]:
    """Lines OUTSIDE fenced code blocks -- what the document claims.

    The complement of :func:`_code_block_lines`. A claim about installation is
    prose, so a guard on it cannot read code blocks only; what it must not do
    is read the whole file, or a documented `# will be finalized` comment
    inside an example would satisfy it.
    """
    lines, inside = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside:
            lines.append(line)
    return lines


# A metavar a reader is meant to substitute is legitimate in a command; a
# `<repository-url>` nobody can resolve is not. Listed explicitly so the
# exemption cannot quietly grow to cover a real hole.
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9_-]*>")
SUBSTITUTABLE = frozenset({"<name>", "<serial>", "<id>", "<package>"})


@pytest.mark.parametrize(
    "doc", [README, PACKAGED_README, SKILL_MD], ids=["repo", "packaged", "skill"]
)
def test_no_documented_command_still_carries_a_placeholder(doc):
    """`git clone <repository-url>` is not a command; it is a TODO (P8).

    It shipped on a tagged release, where the reader has no way to work out
    what the URL should be. Only SKILL.md carried one -- the other two
    parametrisations were already clean, and are here as the false-positive
    control rather than as evidence.
    """
    offenders = [
        line.strip()
        for line in _code_block_lines(doc.read_text(encoding="utf-8"))
        if any(match not in SUBSTITUTABLE for match in PLACEHOLDER.findall(line))
    ]
    assert not offenders, f"{doc.name} documents commands nobody can run: {offenders[:5]}"


def test_the_repo_readme_does_not_defer_its_install_instructions():
    """ "will be finalized once feature parity work lands" (P8).

    Parity landed; the sentence outlived it and read as "not ready" on a tagged
    release. A claim, so this reads the prose stream. The packaged README never
    carried the sentence and is deliberately not parametrised in: a guard that
    was green before the change it claims to enforce is not evidence.
    """
    # Joined before matching, not scanned line by line: Markdown prose wraps,
    # and a sentence broken across two lines is the same sentence. A
    # line-at-a-time version of this guard passed against a README that had the
    # deferral in it, which is how it was caught.
    prose = " ".join(line.strip() for line in _prose_lines(README.read_text(encoding="utf-8")))
    deferral = re.search(
        r"[^.]*will be\s+(finalized|finalised|determined|decided)[^.]*\.", prose, re.IGNORECASE
    )
    assert not deferral, f"README defers its own instructions: {deferral.group(0).strip()!r}"


def test_the_readme_documents_both_halves_of_an_update():
    """`claude plugin update` alone does nothing, and fails silently.

    It moves the installed plugin to whatever the *marketplace* update fetched,
    so without the first command it looks exactly like "no update available".
    Both commands, in that order, read from the fenced blocks.
    """
    commands = [line.strip() for line in _code_block_lines(README.read_text(encoding="utf-8"))]
    marketplace = next(
        (
            i
            for i, line in enumerate(commands)
            if line.startswith("claude plugin marketplace update")
        ),
        None,
    )
    plugin = next(
        (i for i, line in enumerate(commands) if line.startswith("claude plugin update")), None
    )
    assert marketplace is not None, "README does not show `claude plugin marketplace update`"
    assert plugin is not None, "README does not show `claude plugin update`"
    assert marketplace < plugin, "the marketplace update must come first or it does nothing"


def _clone_installations(markdown: str) -> list[tuple[str, str]]:
    """(clone destination, installed source) pairs from a document's commands.

    Parsed out of the fenced blocks: a ``git clone <url> <dest>`` establishes
    where the repository lands, and each later ``ln -s``/``cp -R`` whose source
    starts with that destination says which directory inside the clone is being
    installed. Returning the pair is what lets the guard below resolve the
    source against this repository instead of trusting the prose around it.
    """
    destination = None
    installations = []
    for raw in _code_block_lines(markdown):
        parts = raw.strip().split()
        if parts[:2] == ["git", "clone"] and len(parts) >= 4:
            destination = parts[3]
        elif destination and parts and parts[0] in {"ln", "cp"}:
            sources = [part for part in parts[1:] if part.startswith(destination)]
            installations.extend((destination, source) for source in sources)
    return installations


@pytest.mark.parametrize("doc", [README, SKILL_MD], ids=["repo", "skill"])
def test_the_clone_fallback_installs_a_directory_that_is_actually_a_skill(doc):
    """A clone of the REPO ROOT is not an installable skill (R2).

    `SKILL.md` lives two levels down, at
    `android-emulator-skill/skills/android-emulator-skill/`, so
    `git clone <url> ~/.claude/skills/android-emulator-skill` produces a
    directory Claude Code cannot load. Both documents said exactly that.

    Checked by resolving the documented source path against this repository --
    strip the clone destination off the front of what is being linked, and what
    remains must name a directory here that holds a SKILL.md -- rather than by
    matching the string that happens to appear.
    """
    installations = _clone_installations(doc.read_text(encoding="utf-8"))
    assert installations, f"{doc.name} documents no clone-and-install fallback"

    for destination, source in installations:
        relative = source[len(destination) :].strip("/")
        resolved = REPO_ROOT / relative if relative else REPO_ROOT
        assert (resolved / "SKILL.md").is_file(), (
            f"{doc.name} installs {source!r}, which resolves to {relative or '.'!r} in this "
            f"repository -- and there is no SKILL.md there, so Claude Code loads nothing"
        )


def test_the_clone_url_points_at_this_repositorys_owner():
    """The URL is checked against marketplace.json, not hard-coded here."""
    clones = [
        line.strip()
        for line in _code_block_lines(README.read_text(encoding="utf-8"))
        if line.strip().startswith("git clone")
    ]
    assert clones, "README offers no git-clone fallback"
    repository = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    owner = repository["owner"]["name"] if isinstance(repository.get("owner"), dict) else None
    assert owner, "marketplace.json declares no owner to check the clone URL against"
    assert any(
        f"github.com/{owner}/android-emulator-skill" in line for line in clones
    ), f"the clone URL does not point at {owner}'s repository: {clones}"


def test_the_readme_names_the_emulator_path_trap():
    """The one prerequisite that costs hours, because the error misleads.

    `$ANDROID_HOME` on PATH makes `execve("emulator")` hit the SDK root's
    `emulator` DIRECTORY and raise PermissionError -- not the FileNotFoundError
    anyone would go looking for.

    Two streams, deliberately: the export a reader copies is a command, so it is
    checked in the fenced blocks; the warning about why is a claim, so it is
    checked in the prose. The remedy is also asserted to still exist in
    `sdk_tools.py`, so the README does not quietly become its only copy.
    """
    text = README.read_text(encoding="utf-8")
    exports = [line for line in _code_block_lines(text) if "ANDROID_HOME/emulator" in line]
    assert exports, "the README shows no export putting the emulator directory on PATH"

    prose = "\n".join(_prose_lines(text))
    assert "cmdline-tools" in prose, "the README does not mention the command-line tools"
    assert re.search(
        r"not\s+`?\$ANDROID_HOME`?[,.\s]", prose
    ), "the README does not warn against putting the SDK root on PATH"

    source = (SKILL_ROOT / "scripts" / "common" / "sdk_tools.py").read_text(encoding="utf-8")
    assert 'PATH="$PATH:$ANDROID_HOME/emulator"' in source, (
        "sdk_tools.py no longer states the emulator PATH remedy; the README's "
        "version is now the only copy and nothing checks it"
    )


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


# The status checks REQUIRED by branch protection on main, as
# workflow file -> the job `name:` that becomes the check's context.
#
# Hand-maintained on purpose: branch protection lives in GitHub's settings,
# which a test cannot read, so this is the local mirror of it. Change one and
# change the other. A job renamed here without renaming the protection rule
# leaves a required context that no workflow ever reports -- the same deadlock
# a path filter causes.
REQUIRED_CHECKS = {
    "lint.yml": "Run Black and Ruff",
    "test.yml": "Run pytest (mocked, no device required)",
    "emulator.yml": "pytest -m emulator (real AVD)",
}

# The same three contexts frozen as a set, so deleting an entry from the mirror
# above cannot silently shrink what is checked: an emptied REQUIRED_CHECKS
# makes the loop below iterate nothing and pass.
BRANCH_PROTECTION_CONTEXTS = frozenset(
    {
        "Run Black and Ruff",
        "Run pytest (mocked, no device required)",
        "pytest -m emulator (real AVD)",
    }
)

# Both spellings deadlock a required check, so neither may appear.
PATH_FILTER_KEYS = ("paths", "paths-ignore")


def _triggers(workflow: dict) -> dict:
    """The `on:` block. PyYAML parses a bare `on:` key as the boolean True."""
    return workflow.get("on", workflow.get(True))


def _path_filter_offenders(filename: str, workflow: dict) -> list[str]:
    """Path-filter keys under the PR/push triggers, as ``file: on.event.key``.

    Takes the parsed workflow rather than a path, so the detector can be
    exercised against a synthetic one with no file on disk.
    """
    triggers = _triggers(workflow)
    offenders: list[str] = []
    for event in ("pull_request", "push"):
        config = triggers.get(event) or {}
        offenders += [f"{filename}: on.{event}.{key}" for key in PATH_FILTER_KEYS if key in config]
    return offenders


def test_no_required_check_filters_by_path():
    """A REQUIRED status check must run on every PR, or it blocks them forever.

    GitHub does not report a filtered-out workflow as skipped: the required
    check stays "Expected" and the pull request is permanently BLOCKED. The
    v0.6.0 release PR, which touched only version manifests, hit exactly this
    on the emulator lane -- and then a docs-only PR hit it again on the other
    two, which kept their filters after the lane dropped its own. A PR touching
    only SKILL.md or a manifest matches neither `lint.yml`'s nor `test.yml`'s
    filter, so two of the three required checks never report.

    A path filter therefore does not save work on a required check, it
    deadlocks pull requests; these jobs take about thirty seconds, so runner
    cost is not an argument against running them always. The tempting
    alternative -- a companion job reporting the same check as passed when the
    paths do not match -- is worse: a green result from something that ran
    nothing, which is precisely what the lane script refuses to let happen
    inside the lane.

    Also guarded: the job `name:` still matches the required context. Renaming
    a job silently produces the same deadlock as filtering it out.
    """
    import yaml

    # Coverage first: this test iterates REQUIRED_CHECKS, so emptying it would
    # make everything below pass without inspecting a single workflow.
    assert set(REQUIRED_CHECKS.values()) == BRANCH_PROTECTION_CONTEXTS, (
        f"REQUIRED_CHECKS no longer mirrors branch protection; the difference "
        f"is {sorted(set(REQUIRED_CHECKS.values()) ^ BRANCH_PROTECTION_CONTEXTS)}. "
        f"Dropping an entry here shrinks this test's coverage silently."
    )

    offenders: list[str] = []
    for filename, job_name in REQUIRED_CHECKS.items():
        workflow = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))

        # Membership, not truthiness: a bare `pull_request:` parses to None
        # and means "every pull request", which is the ideal state here.
        assert "pull_request" in _triggers(workflow), (
            f"{filename} no longer runs on pull requests, so the required "
            f"check '{job_name}' can never report"
        )
        assert job_name in {job.get("name") for job in workflow["jobs"].values()}, (
            f"no job in {filename} is named '{job_name}'; branch protection "
            f"requires that context and nothing would ever report it"
        )

        offenders += _path_filter_offenders(filename, workflow)

    assert not offenders, (
        f"required checks filter by path: {offenders}. A PR that matches "
        f"neither filter -- docs, manifests -- leaves those checks at "
        f"'Expected' forever and can never be merged."
    )


def test_the_path_filter_detector_actually_detects():
    """Guard the guard: the test above is vacuous if the detector finds nothing.

    A detector that returns no offenders makes
    ``test_no_required_check_filters_by_path`` pass while inspecting nothing --
    the exact shape behind three capabilities that shipped inert here. So it is
    run against workflows written in this file, where the answer is known and
    no file on disk can change it.
    """
    filtered_pr = {"on": {"pull_request": {"branches": ["main"], "paths": ["scripts/**"]}}}
    assert _path_filter_offenders("lint.yml", filtered_pr) == ["lint.yml: on.pull_request.paths"]

    # A bare `on:` parses to the boolean True, and `paths-ignore` deadlocks a
    # required check exactly as `paths` does.
    ignored_push = {True: {"push": {"branches": ["main"], "paths-ignore": ["docs/**"]}}}
    assert _path_filter_offenders("test.yml", ignored_push) == ["test.yml: on.push.paths-ignore"]

    # An unfiltered workflow reports nothing, so the detector is not simply
    # always-on -- which would fail the guard above for the wrong reason.
    unfiltered = {"on": {"pull_request": {"branches": ["main"]}, "push": {"branches": ["main"]}}}
    assert _path_filter_offenders("emulator.yml", unfiltered) == []
