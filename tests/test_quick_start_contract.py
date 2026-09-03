"""Quick Start is the contract; the e2e test is the proof it holds.

SKILL.md's Quick Start block is the whole navigation strategy an agent is given:
five commands, in order, that it will run before it runs anything else. Nothing
verified that any of them worked as documented.

The v0.6.0 e2e test looked like it did. It reads `screen_mapper --json`, checks
that "Submit Order" is among the labels, then acts with `--find-type EditText`,
which is Quick Start step 4. Steps 3 and 5 -- `navigator --find-text --tap` and
`accessibility_audit.py` -- were never invoked, and both are broken on a Compose
screen (C1: the tap lands on the caption beside the control; L3: the audit's
`critical` gate cannot fire on any recorded screen). "Implemented but
unreachable" is the second failure mode the review named, and this is its
guard: **a capability counts only when SKILL.md tells the agent how to reach it
and a test reaches it that way.**

So: parse the commands out of Quick Start, parse the `run_skill(...)` calls out
of `test_agent_task_e2e.py`, and require the second to cover the first.

Both sides are parsed, not grepped -- fenced-block extraction for the Markdown
(the same reason `test_packaging_contract._code_block_lines` exists: prose that
*describes* a command is not a command), and `ast` for the Python (a substring
search for "navigator.py" would be satisfied by a comment mentioning it).

The coverage assertion is `xfail(strict=True)`; Inc 1 extends
`test_agent_task_e2e.py` and deletes the marker in the same commit.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "SKILL.md"
E2E_TEST = Path(__file__).resolve().parent / "test_agent_task_e2e.py"

QUICK_START_HEADING = "## Quick Start"

# Quick Start documents five commands today. Pinned so the guard cannot pass by
# finding nothing -- a heading rename or a fence that stops closing would
# otherwise turn this file into a no-op that still reports green.
QUICK_START_COMMAND_COUNT = 5

# The e2e test drives five `run_skill` calls. A floor rather than an equality:
# the test is expected to grow, and it must, but a collector that silently
# matched nothing would satisfy any superset check vacuously.
MINIMUM_E2E_INVOCATIONS = 3

XFAIL_REASON = (
    "QS: Quick Start step(s) not exercised by the e2e test; Inc 1 extends test_agent_task_e2e.py"
)


# ---------------------------------------------------------------------------
# What SKILL.md tells the agent to run.
# ---------------------------------------------------------------------------


def _quick_start_block(markdown: str) -> list[str]:
    """Lines inside the fenced blocks under `## Quick Start`, and nowhere else.

    Bounded by the next heading of the same level, so a later section's examples
    are not mistaken for the opening instructions. Prose is excluded for the
    reason `test_packaging_contract._code_block_lines` gives: a sentence
    describing a command is not a command.
    """
    lines: list[str] = []
    in_section = False
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break
            in_section = stripped == QUICK_START_HEADING
            continue
        if not in_section:
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line)
    return lines


def _command(line: str) -> tuple[str, frozenset[str]] | None:
    """A documented invocation as ``(script basename, flags)``.

    Values are dropped -- ``--find-text "Login"`` is a demonstration of the flag,
    not a demand that the e2e test search for "Login" -- and so is the
    ``$SKILL_DIR`` rooting, which is packaging, not behaviour.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None

    script = next((Path(token).name for token in tokens if token.endswith(".py")), None)
    if script is None:
        return None
    return script, frozenset(token for token in tokens if token.startswith("--"))


def quick_start_commands() -> list[tuple[str, frozenset[str]]]:
    """Every command Quick Start tells the agent to run, in order."""
    return [
        command
        for command in (_command(line) for line in _quick_start_block(SKILL_MD.read_text("utf-8")))
        if command is not None
    ]


# ---------------------------------------------------------------------------
# What the e2e test actually invokes.
# ---------------------------------------------------------------------------


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", None)


def e2e_invocations() -> list[tuple[str, frozenset[str]]]:
    """Every ``run_skill(...)`` call in the e2e test, as ``(script, flags)``.

    Only string literals count. An argument computed at runtime is not evidence
    that a documented flag is exercised, and treating it as such is how a guard
    starts agreeing with the thing it guards.
    """
    tree = ast.parse(E2E_TEST.read_text("utf-8"), filename=str(E2E_TEST))
    calls: list[tuple[str, frozenset[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) != "run_skill":
            continue
        literals = [
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ]
        script = next((value for value in literals if value.endswith(".py")), None)
        if script is None:
            continue
        calls.append((script, frozenset(value for value in literals if value.startswith("--"))))
    return calls


def _render(command: tuple[str, frozenset[str]]) -> str:
    """One command as it would be typed, for an assertion message."""
    script, flags = command
    return f"  {script} {' '.join(sorted(flags))}".rstrip()


def _uncovered() -> list[tuple[str, frozenset[str]]]:
    """Quick Start commands no ``run_skill`` call covers."""
    invocations = e2e_invocations()
    return [
        (script, flags)
        for script, flags in quick_start_commands()
        if not any(
            script == called and flags <= called_flags for called, called_flags in invocations
        )
    ]


# ---------------------------------------------------------------------------
# The guard.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=XFAIL_REASON)
def test_every_quick_start_command_is_exercised_by_the_e2e_test():
    """The documented path and the verified path must be the same path.

    Not "a similar command was run": the same script, with at least the flags
    Quick Start names. Step 3's ``--find-text ... --tap`` is not covered by step
    4's ``--find-type ... --enter-text``, and that gap is exactly where C1 and
    C2 lived through a green suite.
    """
    missing = _uncovered()
    report = "\n".join(
        [
            "SKILL.md tells the agent to run these, and test_agent_task_e2e.py never does:",
            *(_render(command) for command in missing),
            "The e2e test currently invokes:",
            *(_render(command) for command in sorted(e2e_invocations())),
        ]
    )
    assert not missing, report


# ---------------------------------------------------------------------------
# The guard cannot pass by parsing nothing.
# ---------------------------------------------------------------------------


def test_the_quick_start_extractor_finds_every_documented_command():
    """A superset check over an empty list is green and worthless."""
    commands = quick_start_commands()
    assert len(commands) == QUICK_START_COMMAND_COUNT, (
        f"Quick Start parsed to {len(commands)} commands, not "
        f"{QUICK_START_COMMAND_COUNT}: {commands}. Either the section changed "
        f"-- update the count deliberately -- or the extractor stopped seeing "
        f"the fence."
    )
    assert all(
        script.endswith(".py") for script, _ in commands
    ), f"a parsed command names no script: {commands}"


def test_the_extractor_reads_flags_and_not_their_values():
    """`--find-text "Login"` is a flag the agent must exercise, not a search term."""
    by_script: dict[str, set[str]] = {}
    for script, flags in quick_start_commands():
        by_script.setdefault(script, set()).update(flags)

    assert "navigator.py" in by_script, f"Quick Start no longer drives navigator: {by_script}"
    assert {"--find-text", "--tap"} <= by_script["navigator.py"]
    assert not any(
        re.search(r"Login|EditText|example", flag) for flag in by_script["navigator.py"]
    ), f"values leaked into the flag set: {by_script['navigator.py']}"


def test_the_e2e_collector_finds_the_calls_that_are_there():
    """A collector that matched nothing would make every command look covered."""
    invocations = e2e_invocations()
    assert len(invocations) >= MINIMUM_E2E_INVOCATIONS, (
        f"only {len(invocations)} run_skill calls were parsed out of "
        f"{E2E_TEST.name}: {invocations}. The collector, not the e2e test, is "
        f"probably what broke."
    )
    scripts = {script for script, _ in invocations}
    assert "screen_mapper.py" in scripts, f"the e2e test no longer maps the screen: {scripts}"
