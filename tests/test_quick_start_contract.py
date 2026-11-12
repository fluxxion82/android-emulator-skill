"""Quick Start is the contract; the e2e test is the proof it holds.

SKILL.md's Quick Start block is the whole navigation strategy an agent is given:
six commands, steps 0 to 5, in order, that it will run before it runs anything
else. Nothing verified that any of them worked as documented.

The v0.6.0 e2e test looked like it did. It reads `screen_mapper --json`, checks
that "Submit Order" is among the labels, then acts with `--find-type EditText`,
which is Quick Start step 4. Steps 3 and 5 -- `navigator --find-text --tap` and
`accessibility_audit.py` -- were never invoked, and both were broken on a
Compose screen (C1: the tap landed on the caption beside the control; L3: the
audit's `critical` gate could not fire on any recorded screen). Inc 1 fixed
both, and the e2e now walks all five. "Implemented but
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

Step 0, `bash ... android_health_check.sh --json`, was added later, and the
guard could not see it: both extractors kept only `.py` tokens, so the new
line parsed to nothing and the coverage assertion stayed green with the e2e
test never running the script. Both now take `SCRIPT_SUFFIXES`.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "SKILL.md"
E2E_TEST = Path(__file__).resolve().parent / "test_agent_task_e2e.py"

QUICK_START_HEADING = "## Quick Start"

# What a script token ends in, on both sides. Step 0 is `bash ...
# android_health_check.sh --json`, and with `.py` alone both extractors were
# blind to it: the Quick Start line parsed to nothing, so the coverage
# assertion passed while the e2e test never ran the script. A suffix added
# here must be added to both sides at once -- it is one constant for that.
SCRIPT_SUFFIXES = (".py", ".sh")

# Every command SKILL.md's Quick Start block documents, normalised. Complete,
# not a count: a count survives an extractor that drops `--launch` from
# app_launcher and picks up a stray line elsewhere, and that mutation is exactly
# what would make the coverage assertion below quietly stop checking step 1.
QUICK_START_BASELINE = (
    ("accessibility_audit.py", ()),
    ("android_health_check.sh", ("--json",)),
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
    ("android_health_check.sh", ("--json",)),
    ("app_launcher.py", ("--json", "--launch")),
    ("app_launcher.py", ("--json", "--terminate")),
    ("log_monitor.py", ("--duration", "--json")),
    ("navigator.py", ("--enter-text", "--find-type")),
    ("navigator.py", ("--find-text", "--json", "--tap")),
    ("screen_mapper.py", ()),
    ("screen_mapper.py", ("--json",)),
)


# ---------------------------------------------------------------------------
# What SKILL.md tells the agent to run.
# ---------------------------------------------------------------------------


PROSE, FENCE, CODE = "prose", "fence", "code"
_FENCE_RUN = re.compile(r"([ ]*)(`{3,}|~{3,})(.*)$")
_QUOTED_FENCE_RUN = re.compile(r"[ ]*>[ >]*(`{3,}|~{3,})(.*)$")
_MARKER_FENCE_RUN = re.compile(r"[ ]*(?:(?:[-*+]|\d+[.)])[ ]+)+(`{3,}|~{3,})(.*)$")
# Whitespace a renderer and `str` methods do not agree about: a tab is columns,
# not a character, to a renderer; a no-break space is not a space to one.
_OTHER_WHITESPACE = re.compile(r"[^\S \n]")
# HTML whose content a renderer shows as code, or does not show.
_HTML_BLOCK = re.compile(r"<(?:pre|script|style|textarea)\b|<!--", re.IGNORECASE)


def _opens_a_block(run: str, rest: str) -> bool:
    """A run of backticks followed by more backticks on its line is inline code."""
    return bool(run) and not (run[0] == "`" and "`" in rest)


def _read_fences(markdown: str) -> tuple[list[tuple[str, str]], list[str]]:
    lines: list[tuple[str, str]] = []
    problems: list[str] = []
    opened: tuple[str, str, int] | None = None
    # Lines end at a newline and nowhere else. `str.splitlines` also breaks at
    # U+2028, a form feed and others, where a renderer does not: a fence
    # followed by one of those would close here and stay open there. So the
    # text is split on "\n" alone (a CRLF pair counted as one), and any other
    # whitespace than U+0020 is refused below, those characters included.
    text = markdown.replace("\r\n", "\n")
    for number, line in enumerate(text.removesuffix("\n").split("\n") if text else [], 1):
        if _OTHER_WHITESPACE.search(line):
            problems.append(
                f"line {number}: a tab, or whitespace other than U+0020 and a newline "
                "(write spaces; show a tab as \\t)"
            )
        match = _FENCE_RUN.match(line)
        indent, run, rest = match.groups() if match else ("", "", "")
        if opened is None:
            quoted = _QUOTED_FENCE_RUN.match(line)
            if quoted and _opens_a_block(*quoted.groups()):
                problems.append(f"line {number}: a fenced block inside a blockquote")
            marked = _MARKER_FENCE_RUN.match(line)
            if marked and _opens_a_block(*marked.groups()):
                problems.append(
                    f"line {number}: a fence on a list marker's line "
                    "(write it on a line of its own, under the item)"
                )
            if _opens_a_block(run, rest):
                opened = (indent, run, number)
                lines.append((line, FENCE))
                continue
            if _HTML_BLOCK.search(line):
                problems.append(
                    f"line {number}: <pre>, <script>, <style>, <textarea> or an HTML comment "
                    "outside a fenced block (remove it, or show it inside one)"
                )
            lines.append((line, PROSE))
            continue
        open_indent, open_run, open_number = opened
        if run and run[0] == open_run[0] and len(run) >= len(open_run) and not rest.strip(" "):
            if indent != open_indent:
                problems.append(
                    f"line {number}: closes the block opened on line {open_number} "
                    "at another indentation"
                )
            opened = None
            lines.append((line, FENCE))
            continue
        if line.strip() and not line.startswith(open_indent):
            problems.append(
                f"line {number}: indented less than the block opened on line {open_number}, "
                "which is not closed before it"
            )
        lines.append((line, CODE))
    if opened is not None:
        problems.append(f"line {opened[2]}: opens a block that is never closed")
    return lines, problems


def fence_problems(markdown: str) -> list[str]:
    """What in a document `markdown_lines` will not read. Empty when nothing."""
    return _read_fences(markdown)[1]


def markdown_lines(markdown: str) -> list[tuple[str, str]]:
    """Each line of a document with what it is: PROSE, a FENCE line, or CODE.

    The one reader of fenced blocks; every Markdown guard in the suite takes its
    lines from here. A block opens on a line that is a run of three or more
    backticks or tildes (after spaces) and closes on a run of the same
    character at least as long with nothing after it, so a tilde fence is a
    fence, and three backticks inside a four-backtick block are code. A line
    that opens with backticks and holds more of them later (three backticks
    around a word) is inline code.

    This is not a Markdown parser, and it does not guess at what it cannot
    read. The fence, whitespace and HTML forms below are refused
    (``AssertionError``); every other line outside a fenced block is PROSE:

    * a fenced block inside a blockquote -- its fence lines begin with ``>``,
      and here they would be prose;
    * a block whose closing fence is indented differently from its opening
      one, or that holds a line indented less than it -- a renderer ends a
      block fenced inside a list item where the item ends, and here it would
      run on;
    * a block that is never closed, and a fence that shares its line with a
      list marker (``1. ```bash``), which is not read as a fence at all: write
      the fence on a line of its own, under the item;
    * a tab, or any whitespace other than U+0020 and a newline, anywhere -- a
      fence followed by a no-break space does not close its block, a tab is
      not one column, and a line separator ends a line for `str.splitlines`
      and for no renderer;
    * outside fenced blocks, ``<pre>``, ``<script>``, ``<style>``,
      ``<textarea>`` or an HTML comment -- what they hold is shown as code, or
      not shown.

    What "prose" means here is "outside a fenced block", and no more. A block
    made by indentation alone, inline code, and what a renderer shows inside
    other HTML are all PROSE: telling them apart is parsing Markdown.
    `flush_prose_lines` leaves out the first of those where it can.
    """
    lines, problems = _read_fences(markdown)
    assert not problems, f"this document is not one the fence reader reads: {problems}"
    return lines


def prose_lines(markdown: str) -> list[str]:
    """Lines outside fenced blocks: what a document claims."""
    return [line for line, kind in markdown_lines(markdown) if kind == PROSE]


def flush_prose_lines(markdown: str) -> list[str]:
    """Prose lines indented fewer than four columns.

    Keeps the lines outside fenced blocks that begin with fewer than four
    U+0020 spaces, which leaves out an ordinary block made by indentation.
    Indentation after a container's marker (``>``, ``1.``) is not interpreted.
    """
    return [line for line in prose_lines(markdown) if len(line) - len(line.lstrip(" ")) < 4]


def code_lines(markdown: str) -> list[str]:
    """Lines inside fenced blocks: what a reader copies and runs."""
    return [line for line, kind in markdown_lines(markdown) if kind == CODE]


def _quick_start_block(markdown: str) -> list[str]:
    """Lines inside the fenced blocks under `## Quick Start`, and nowhere else.

    Bounded by the next heading of the same level, so a later section's examples
    are not mistaken for the opening instructions. Prose is excluded for the
    reason `test_packaging_contract._code_block_lines` gives: a sentence
    describing a command is not a command.
    """
    return code_lines(section(markdown, QUICK_START_HEADING.removeprefix("## ")))


def section(markdown: str, title: str) -> str:
    """Everything under the one ``## title`` heading, up to the next ``## ``.

    A line inside a fenced block is never a heading, so a ``## `` in an example
    does not end a section. Refuses a title that is missing or that two sections
    carry: a guard reading "the Failures section" must not be reading the first
    of two.
    """
    heading = f"## {title}"
    found: list[list[str]] = []
    current: list[str] | None = None
    for line, kind in markdown_lines(markdown):
        if kind == PROSE and line.startswith("## "):
            current = None
            if line.strip() == heading:
                current = []
                found.append(current)
            continue
        if current is not None:
            current.append(line)
    assert len(found) == 1, f"expected one {heading!r} section, found {len(found)}"
    return "\n".join(found[0])


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

    script = next((Path(token).name for token in tokens if token.endswith(SCRIPT_SUFFIXES)), None)
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
        script = next((value for value in literals if value.endswith(SCRIPT_SUFFIXES)), None)
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
