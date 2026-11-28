#!/usr/bin/env python3
"""Install this plugin with each agent's own CLI, in an empty home, and look at what arrived.

    python tests/verify_install.py                 # every agent below
    python tests/verify_install.py --agent codex
    python tests/verify_install.py --working-tree  # the checkout as it stands
    python tests/verify_install.py --record        # and keep what the CLIs printed

For each agent, in order, stopping at the first step that fails:

1. The CLI is on PATH and its `--version` is the version agent-cli-pins.txt
   pins. Anything else is a refusal: no install command is run.
2. The agent's own install commands exit 0, run from an empty directory with a
   new, empty home. Nothing of the caller's environment is handed over but PATH.
3. The home then holds this skill exactly once, and the plugin directory around
   it is the source's plugin directory, file for file and byte for byte.
4. Every Python script of the installed copy answers `--help`, started by its
   absolute path from another directory.
5. The agent's own listing shows the skill: Codex renders the prompt a session
   would start with, and it holds the description in SKILL.md's frontmatter;
   Claude Code's inventory of the plugin counts one skill, by this name.

What is installed is the commit HEAD names, exported clean, because a folder
install copies the directory it is pointed at: uncommitted files and local
`__pycache__` directories included. `--working-tree` installs the checkout as it
stands instead, and says so.

Exit status: 0 when every agent asked for passed, 1 when one did not (a CLI that
is missing or at another version is one that did not), 2 for bad arguments or a
Python older than the scripts need.

Limits. Both agents are installed from a folder; an install from GitHub
(`owner/repo`), which clones, is not covered. Step 5 shows that the agent lists
the skill, not that a model then uses it. Run against the pinned versions only:
what another version does is unknown until the pin is moved and this passes.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PINS = REPO_ROOT / "agent-cli-pins.txt"
RECORDED_ROOT = REPO_ROOT / "tests" / "fixtures" / "recorded"

PLUGIN = "android-emulator-skill"
SKILL = "android-emulator-skill"
MARKETPLACE = "fluxxion82"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"

COMMAND_TIMEOUT = 180
MINIMUM_PYTHON = (3, 12)

# One number of a version: ASCII digits, no leading zero, nine digits at most.
# `\d` would take any script's digits, and npm reads a number past its bound
# as a tag, which moves; both are lines npm would not read as this reader did.
_NUMBER = r"(?:0|[1-9][0-9]{0,8})"
EXACT_VERSION = rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}"
# A pin, whole: npm's `name@version` with an optional scope and an exact version.
PIN = re.compile(
    rf"(?P<name>(?:@[a-z0-9][a-z0-9-]*/)?[a-z0-9][a-z0-9-]*)@(?P<version>{EXACT_VERSION})"
)
# A word of a `--version` answer that is a version and nothing else, and what
# marks any other word as something built on a version. The second is `\d` on
# purpose: digits of any script make a word one this does not read.
VERSION_WORD = re.compile(EXACT_VERSION)
VERSION_LIKE = re.compile(r"\d\.\d")
# Claude Code's inventory line for a plugin holding this one skill.
ONE_SKILL = re.compile(rf"\s*Skills \(1\)\s+{re.escape(SKILL)}\s*")


def read_pins(text: str) -> dict[str, str]:
    """``{npm name: version}`` from agent-cli-pins.txt.

    A line is empty, starts with `#`, or is one pin and nothing else. Anything
    more permissive would be a line this reads one way and `npm install` another.
    """
    pins: dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), 1):
        if line == "" or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"line {number}: {line!r} is not `name@X.Y.Z`")
        if match["name"] in pins:
            raise ValueError(f"line {number}: {match['name']} is pinned twice")
        pins[match["name"]] = match["version"]
    return pins


def stated_version(output: str) -> str | None:
    """The one version a `--version` answer states, or None.

    The answer is read as words. A version is a word that is `X.Y.Z` and
    nothing else. Any other word with a number, a dot and a number in it
    (`0.171.0-alpha.1`, `99.98.97-0.170.0`, `v0.170.0`) is a version this does
    not read, and an answer holding one states none: an earlier reader looked
    for the pin between boundaries, and found it inside the second of those.
    """
    words = output.split()
    if any(VERSION_LIKE.search(word) and not VERSION_WORD.fullmatch(word) for word in words):
        return None
    found = {word for word in words if VERSION_WORD.fullmatch(word)}
    return found.pop() if len(found) == 1 else None


def _strings(node: object) -> list[str]:
    if isinstance(node, dict):
        return [found for value in node.values() for found in _strings(value)]
    if isinstance(node, list):
        return [found for value in node for found in _strings(value)]
    return [node] if isinstance(node, str) else []


def codex_lists(output: str, description: str) -> bool:
    """Whether the prompt `codex debug prompt-input` rendered holds the skill's description."""
    try:
        rendered = json.loads(output)
    except ValueError:
        return False
    return any(description in text for text in _strings(rendered))


def claude_lists(output: str, _description: str) -> bool:
    """Whether `claude plugin details` counts one skill in the plugin, by this skill's name."""
    return any(ONE_SKILL.fullmatch(line) for line in output.splitlines())


@dataclasses.dataclass(frozen=True)
class Agent:
    """One row: a CLI, the home it is isolated by, and the commands a user would type."""

    key: str
    cli: str
    package: str
    home_variable: str
    install: tuple[tuple[str, ...], ...]
    listing: tuple[str, ...]
    lists: Callable[[str, str], bool]
    listing_says: str
    # Where `--record` keeps this CLI's output: recorded/<profile>-<version>/.
    profile: str
    # Besides `--version`: the fixture name the listing's output is kept under,
    # when that output is small and this repository's own to publish.
    listing_fixture: str | None
    environment: tuple[tuple[str, str], ...] = ()


AGENTS = (
    Agent(
        key="codex",
        cli="codex",
        package="@openai/codex",
        home_variable="CODEX_HOME",
        install=(
            ("plugin", "marketplace", "add", "{source}"),
            ("plugin", "add", PLUGIN_ID),
        ),
        listing=("debug", "prompt-input", "hello"),
        lists=codex_lists,
        listing_says="the prompt a session starts with holds the skill's description",
        profile="codex-cli",
        # The rendered prompt is some 70 KB of Codex's own instructions.
        listing_fixture=None,
    ),
    Agent(
        key="claude",
        cli="claude",
        package="@anthropic-ai/claude-code",
        home_variable="CLAUDE_CONFIG_DIR",
        install=(
            ("plugin", "marketplace", "add", "{source}"),
            ("plugin", "install", PLUGIN_ID),
        ),
        listing=("plugin", "details", PLUGIN_ID),
        lists=claude_lists,
        listing_says="the plugin's inventory counts one skill, by this name",
        profile="claude-code",
        listing_fixture="plugin_details",
        # A CLI that replaced itself mid-check would no longer be the pinned one.
        environment=(("DISABLE_AUTOUPDATER", "1"),),
    ),
)


@dataclasses.dataclass
class Outcome:
    """What one agent's row came to. No problems is a pass."""

    agent: Agent
    version: str | None = None
    problems: list[str] = dataclasses.field(default_factory=list)
    scripts: int = 0
    # What the CLI printed, by fixture name: kept for `--record`.
    printed: dict[str, str] = dataclasses.field(default_factory=dict)


def _environment(agent: Agent, home: Path) -> dict[str, str]:
    """Built from nothing: PATH, a home under the scratch directory, and the agent's own.

    The agent's home is made, empty: Codex refuses a CODEX_HOME that does not exist.
    """
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    (home / agent.key).mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        agent.home_variable: str(home / agent.key),
        **dict(agent.environment),
    }


def _tail(text: str, lines: int = 6) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    return "\n".join(f"    {line}" for line in kept)


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
    try:
        ran = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer in {COMMAND_TIMEOUT} s"
    except OSError as error:
        return 127, "", str(error)
    return ran.returncode, ran.stdout, ran.stderr


def _walk(root: Path):
    """`os.walk`, raising where it would pass over a directory it could not list.

    Left to itself it skips such a directory without a word, and a tree
    compared file for file would then be compared less the files nobody saw.
    """

    def refuse(error: OSError) -> None:
        raise error

    return os.walk(root, followlinks=False, onerror=refuse)


def files_under(root: Path) -> dict[str, str]:
    """``{relative path: sha256}`` for every file under a directory; a symlink is named as one.

    Raises OSError for a directory or a file it cannot read, the root included.
    """
    found: dict[str, str] = {}
    for directory, dirnames, filenames in _walk(root):
        for name in (*dirnames, *filenames):
            path = Path(directory, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[relative] = f"symlink to {path.readlink()}"
            elif path.is_file():
                found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def tree_problems(source: Path, installed: Path) -> list[str]:
    """How an installed plugin directory differs from the one it was installed from."""
    want, have = files_under(source), files_under(installed)
    differences = (
        ("is missing", sorted(want.keys() - have.keys())),
        ("holds, though the source does not,", sorted(have.keys() - want.keys())),
        (
            "holds other bytes for",
            sorted(n for n in want.keys() & have.keys() if want[n] != have[n]),
        ),
    )
    return [
        f"the installed plugin {what} {len(names)} file(s): "
        + ", ".join(names[:5])
        + (" ..." if len(names) > 5 else "")
        for what, names in differences
        if names
    ]


def installed_skills(home: Path) -> list[Path]:
    """Every directory under a home that is this skill: named for it and holding a SKILL.md.

    Raises OSError for a directory it cannot list: a second copy could be in it.
    """
    return sorted(
        Path(directory)
        for directory, _dirnames, filenames in _walk(home)
        if Path(directory).name == SKILL and "SKILL.md" in filenames
    )


def skill_description(skill: Path) -> str | None:
    """The `description` of a SKILL.md's frontmatter, parsed as the YAML it is."""
    import yaml

    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return None
    frontmatter = yaml.safe_load(text[4 : text.index("\n---\n", 4)])
    description = frontmatter.get("description") if isinstance(frontmatter, dict) else None
    return description if isinstance(description, str) and description.strip() else None


def script_problems(
    skill: Path, cwd: Path, env: dict[str, str], copy: str = "installed"
) -> tuple[int, list[str]]:
    """``(scripts run, the ones that did not answer --help)``, each by its absolute path.

    Each is given `env` and `PYTHONDONTWRITEBYTECODE=1`. `copy` is the word a
    problem has for the copy the scripts are in: the release's check starts
    an extracted one.
    """
    scripts = sorted((skill / "scripts").glob("*.py"))
    problems = []
    for script in scripts:
        status, stdout, stderr = _run(
            [sys.executable, str(script), "--help"], cwd, {**env, "PYTHONDONTWRITEBYTECODE": "1"}
        )
        if status != 0 or not stdout.strip():
            problems.append(
                f"{script.name} --help exited {status} from the {copy} copy:\n{_tail(stderr)}"
            )
    if not scripts:
        problems.append(f"{skill / 'scripts'} holds no Python script")
    return len(scripts), problems


def check(agent: Agent, pin: str | None, source: Path, scratch: Path) -> Outcome:
    """Run one agent's row. `source` is a marketplace root; `scratch` is this row's to fill."""
    outcome = Outcome(agent)
    problems = outcome.problems
    if pin is None:
        problems.append(f"{PINS.name} pins no version of {agent.package}; nothing was run")
        return outcome
    cli = shutil.which(agent.cli)
    if cli is None:
        problems.append(
            f"`{agent.cli}` is not on PATH. Install the pinned one: {agent.package}@{pin}"
        )
        return outcome

    home, cwd = scratch / "home", scratch / "cwd"
    cwd.mkdir(parents=True)
    env = _environment(agent, home)

    status, stdout, stderr = _run([cli, "--version"], cwd, env)
    outcome.version = stated_version(stdout) if status == 0 else None
    if outcome.version != pin:
        said = outcome.version or f"exit {status}: {(stdout + stderr).strip()[:120]!r}"
        problems.append(
            f"`{agent.cli} --version` is {said}, and this check is pinned to {pin} "
            f"({PINS.name}). Nothing was installed."
        )
        return outcome
    outcome.printed["version"] = stdout

    for arguments in agent.install:
        argv = [cli, *(word.format(source=source) for word in arguments)]
        status, stdout, stderr = _run(argv, cwd, env)
        if status != 0:
            shown = " ".join([agent.cli, *arguments]).format(source="<source>")
            problems.append(f"`{shown}` exited {status}:\n{_tail(stdout + stderr)}")
            return outcome

    try:
        skills = installed_skills(home)
        if len(skills) != 1:
            places = [path.relative_to(home).as_posix() for path in skills]
            problems.append(f"the home holds the skill {len(skills)} times, not once: {places}")
            return outcome
        skill = skills[0]
        if skill.parent.name != "skills":
            problems.append(f"the skill was installed outside a `skills` directory: {skill}")
            return outcome
        problems += tree_problems(source / PLUGIN, skill.parent.parent)
    except OSError as error:
        problems.append(
            f"what was installed could not all be read, so it was not compared: {error}"
        )
    if problems:
        return outcome

    outcome.scripts, wrong = script_problems(skill, cwd, env)
    problems += wrong
    if problems:
        return outcome

    description = skill_description(skill)
    if description is None:
        problems.append("the installed SKILL.md has no frontmatter description to look for")
        return outcome
    status, stdout, stderr = _run([cli, *agent.listing], cwd, env)
    shown = " ".join([agent.cli, *agent.listing])
    if status != 0:
        problems.append(f"`{shown}` exited {status}:\n{_tail(stdout + stderr)}")
    elif not agent.lists(stdout, description):
        problems.append(f"`{shown}` does not show the skill: {agent.listing_says} was not so")
    elif agent.listing_fixture:
        outcome.printed[agent.listing_fixture] = stdout
    return outcome


def _git(*arguments: str) -> str:
    ran = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *arguments], capture_output=True, text=True, check=True
    )
    return ran.stdout


def export_head(destination: Path) -> str:
    """Write the tree of HEAD to a directory, as a clone would hold it; the commit's name."""
    commit = _git("rev-parse", "HEAD").strip()
    archive = destination.with_suffix(".tar")
    _git("archive", "--format=tar", "-o", str(archive), "HEAD")
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    archive.unlink()
    return commit


def record(outcome: Outcome, commit: str) -> Path:
    """Keep what a passing row's CLI printed, under recorded/<profile>-<version>/."""
    directory = RECORDED_ROOT / f"{outcome.agent.profile}-{outcome.version}"
    directory.mkdir(parents=True, exist_ok=True)
    fixtures = []
    for name, text in sorted(outcome.printed.items()):
        data = text.encode("utf-8")
        (directory / f"{name}.txt").write_bytes(data)
        command = ["--version"] if name == "version" else list(outcome.agent.listing)
        fixtures.append(
            {
                "name": name,
                "file": f"{name}.txt",
                "command": " ".join([outcome.agent.cli, *command]),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    manifest = {
        "profile": directory.name,
        "recorded_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "package": outcome.agent.package,
        "cli_version": outcome.version,
        "platform": sys.platform,
        "command": "python tests/verify_install.py --record",
        "source_commit": commit,
        "note": (
            "Standard output of the agent's own CLI, verbatim, from a passing run of "
            "tests/verify_install.py: an empty home, this plugin installed from a clean "
            "export of source_commit. A tool corpus, not a device profile."
        ),
        "fixtures": fixtures,
    }
    (directory / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install this plugin with each agent's CLI in an empty home and check it.",
        epilog="Exit 0: every agent passed. 1: one did not. 2: bad arguments or an old Python.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        choices=[agent.key for agent in AGENTS],
        help="Check this agent only (repeatable). Default: all of them.",
    )
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="Install the checkout as it stands, not a clean export of HEAD.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Write what each passing CLI printed to tests/fixtures/recorded/.",
    )
    arguments = parser.parse_args(argv)
    if sys.version_info < MINIMUM_PYTHON:
        print(f"The scripts this runs need Python {'.'.join(map(str, MINIMUM_PYTHON))}+.")
        return 2
    if arguments.record and arguments.working_tree:
        print("--record keeps what a clean export printed; it does not go with --working-tree.")
        return 2

    pins = read_pins(PINS.read_text(encoding="utf-8"))
    chosen = [agent for agent in AGENTS if not arguments.agent or agent.key in arguments.agent]
    with tempfile.TemporaryDirectory(prefix="verify-install-") as scratch_name:
        scratch = Path(scratch_name).resolve()
        if arguments.working_tree:
            source, commit = REPO_ROOT, ""
            print("Source: the checkout as it stands, uncommitted and ignored files included.")
        else:
            source = scratch / "source"
            commit = export_head(source)
            print(f"Source: commit {commit[:7]}, exported clean.")
            changed = len(_git("status", "--porcelain").splitlines())
            if changed:
                print(f"  {changed} path(s) differ from that commit and are not in this check.")

        failed = False
        for agent in chosen:
            outcome = check(agent, pins.get(agent.package), source, scratch / agent.key)
            if outcome.problems:
                failed = True
                print(f"{agent.key}: FAILED")
                for problem in outcome.problems:
                    print(f"  {problem}")
                continue
            print(
                f"{agent.key} {outcome.version}: installed, identical to the source, "
                f"{outcome.scripts} scripts answer --help, skill listed."
            )
            if arguments.record:
                print(f"  recorded: {record(outcome, commit).relative_to(REPO_ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
