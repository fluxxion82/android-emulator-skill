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

The coverage assertion cannot police its own extractors: one that quietly
stopped seeing `app_launcher.py --launch` on either side would make the two
sides agree about nothing at all, and the assertion would pass. So both
extractors are held to a **complete literal baseline** of what they return
today. A mutation to either one shows up as a baseline mismatch rather than as
silence.

Inc 1 extended `test_agent_task_e2e.py` to walk all five Quick Start commands --
including step 3, `navigator --find-text --tap`, seeded from what step 2 printed
-- so the coverage assertion is green and its `xfail(strict=True)` marker is
gone. The e2e baseline below was updated in that same commit.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "SKILL.md"
E2E_TEST = Path(__file__).resolve().parent / "test_agent_task_e2e.py"

QUICK_START_HEADING = "## Quick Start"

# Every command SKILL.md's Quick Start block documents, normalised. Complete,
# not a count: a count survives an extractor that drops `--launch` from
# app_launcher and picks up a stray line elsewhere, and that mutation is exactly
# what would make the coverage assertion below quietly stop checking step 1.
QUICK_START_BASELINE = (
    ("accessibility_audit.py", ()),
    ("app_launcher.py", ("--launch",)),
    ("navigator.py", ("--enter-text", "--find-type")),
    ("navigator.py", ("--find-text", "--tap")),
    ("screen_mapper.py", ()),
)

# Every `run_skill(...)` call in test_agent_task_e2e.py, normalised. Duplicates
# are kept -- the e2e test maps the screen twice, once by default output for the
# name it will tap and once as JSON to confirm the field filled -- so a collector
# that deduplicated or skipped a call fails here. When that test grows a step,
# this baseline is updated in the same commit, which is the point of stating it.
E2E_BASELINE = (
    ("accessibility_audit.py", ()),
    ("app_launcher.py", ("--json", "--launch")),
    ("log_monitor.py", ("--duration", "--json")),
    ("navigator.py", ("--enter-text", "--find-type")),
    ("navigator.py", ("--find-text", "--json", "--tap")),
    ("screen_mapper.py", ()),
    ("screen_mapper.py", ("--json",)),
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


def _normalise(commands) -> list[tuple[str, tuple[str, ...]]]:
    """Commands in a comparable, order-independent form, duplicates preserved."""
    return sorted((script, tuple(sorted(flags))) for script, flags in commands)


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


def test_the_quick_start_extractor_returns_exactly_the_documented_commands():
    """Every Quick Start command, complete, with flags and without their values.

    A count would let an extractor lose `app_launcher.py --launch` and gain
    something else. The literal also pins that `--find-text "Login"` yields the
    flag and not the search term: no baseline entry carries a value.
    """
    assert _normalise(quick_start_commands()) == list(QUICK_START_BASELINE), (
        f"the Quick Start extractor no longer returns the documented set.\n"
        f"  parsed:   {_normalise(quick_start_commands())}\n"
        f"  baseline: {list(QUICK_START_BASELINE)}\n"
        f"If SKILL.md changed, update the baseline deliberately; otherwise the "
        f"extractor broke and the coverage assertion above is checking less "
        f"than it claims."
    )


def test_the_e2e_collector_returns_exactly_the_calls_in_the_test():
    """Every `run_skill` invocation the e2e test makes, duplicates included.

    A collector that silently matched nothing, or dropped one call, would make
    the Quick Start commands look covered -- or make an already-covered one look
    missing -- and the xfail above would hide either.
    """
    assert _normalise(e2e_invocations()) == list(E2E_BASELINE), (
        f"the run_skill collector no longer returns the calls in "
        f"{E2E_TEST.name}.\n"
        f"  parsed:   {_normalise(e2e_invocations())}\n"
        f"  baseline: {list(E2E_BASELINE)}\n"
        f"If the e2e test grew a step, update the baseline in that commit."
    )
