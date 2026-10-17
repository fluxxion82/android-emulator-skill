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

import ast
import functools
import json
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from fake_toolchain import FakeToolchain, Rule

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


LANE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "run-emulator-lane.sh"

# What the floor's grep patterns must make of the terms in pytest's summary
# line. The patterns are checked as regexes against these, not compared as
# strings, so a respelling that means the same thing passes and one that lets
# a skip through does not. "23 passed" and "2171 deselected" are a green lane's
# own words (run 38983850283).
FLOOR_PASSED_TERMS = ("1 passed", "23 passed")
FLOOR_FAILING_TERMS = ("1 skipped", "2 skipped", "1 failed", "1 error", "2 errors")
FLOOR_NEUTRAL_TERMS = ("23 passed", "2171 deselected", "1 xfailed", "3 warnings")


def _shell_commands(script: str) -> list[tuple[int, list[str]]]:
    """``(line number, shell words)`` for every line that is not a comment.

    Tokenised with ``shlex`` in POSIX mode, so a quoted pattern is one word, a
    ``#`` comment produces nothing, and ``;`` / ``|`` are words of their own.
    The point is that ``exit 1`` inside an ``echo "..."`` string, or the word
    ``passed`` in a comment, is not mistaken for a command -- which is exactly
    how the first version of this guard passed with the check deleted (N7).
    """
    commands = []
    for number, line in enumerate(script.splitlines(), start=1):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        words = list(lexer)
        if words:
            commands.append((number, words))
    return commands


def _then_branch(commands: list[tuple[int, list[str]]], start: int) -> list[list[str]]:
    """The commands the ``if`` at ``commands[start]`` runs when its test holds.

    Only the ``then`` branch, and only its direct commands. An ``else`` or
    ``elif`` at this block's depth ends the branch -- an ``exit 1`` moved into
    ``else`` runs when the test FAILS, which for the floor means "exit 1 when
    the lane was clean" -- and nested ``if ... fi`` blocks are skipped over,
    because an exit under an inner condition is not this block's exit. The
    first version of this helper treated every line before ``fi`` as the body,
    and the ``else`` mutation passed it (review of PR #20).
    """
    branch: list[list[str]] = []
    depth = 0
    in_then = True
    for _, words in commands[start:]:
        if words[0] == "if":
            depth += 1
            continue
        if words[0] == "fi":
            depth -= 1
            if depth == 0:
                return branch
            continue
        if depth == 1 and words[0] in ("else", "elif"):
            in_then = False
            continue
        if depth == 1 and words[0] == "then":
            continue
        if depth == 1 and in_then:
            branch.append(words)
    raise AssertionError(f"the `if` on line {commands[start][0]} is never closed by `fi`")


def _condition(words: list[str]) -> list[str]:
    """The words of an ``if`` line between ``if`` and ``then``."""
    end = words.index("then") if "then" in words else len(words)
    return [word for word in words[1:end] if word != ";"]


def _clauses(condition: list[str]) -> tuple[list[str], list[list[str]]]:
    """A condition split on ``||`` / ``&&``: the operators, and the clauses between."""
    operators: list[str] = []
    clauses: list[list[str]] = [[]]
    for word in condition:
        if word in ("||", "&&"):
            operators.append(word)
            clauses.append([])
        else:
            clauses[-1].append(word)
    return operators, clauses


def _grep_clause(clause: list[str]) -> tuple[bool, str, str] | None:
    """``(negated, pattern, file)`` for ``[!] grep -FLAGS PATTERN FILE``, else None."""
    negated = clause[:1] == ["!"]
    words = clause[1:] if negated else clause
    if words[:1] != ["grep"]:
        return None
    operands = [word for word in words[1:] if not word.startswith("-")]
    if len(operands) != 2:
        return None
    return negated, operands[0], operands[1]


def _searches(pattern: str, term: str) -> bool:
    """Whether the ERE ``pattern`` finds ``term``; an unparsable pattern finds nothing."""
    try:
        return re.search(pattern, term) is not None
    except re.error:
        return False


def _counts_passed(pattern: str) -> bool:
    """A pattern that finds a positive count of passed tests, and not a zero one."""
    return all(_searches(pattern, term) for term in FLOOR_PASSED_TERMS) and not _searches(
        pattern, "0 passed"
    )


def _lane_floor(commands: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    """Every ``if`` whose condition greps for a count of passed tests, with its index."""
    floors = []
    for index, (_, words) in enumerate(commands):
        if words[0] != "if":
            continue
        greps = [_grep_clause(clause) for clause in _clauses(_condition(words))[1]]
        if any(grep and _counts_passed(grep[1]) for grep in greps):
            floors.append((index, words))
    return floors


def _device_checks(commands: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    """Every ``if`` whose condition asks ``adb get-state``, with its index.

    The question sits inside a ``"$(...)"``, which is one shell word; its own
    words are compared, so ``adb  get-state`` and ``adb get-state`` are the same.
    """
    return [
        (index, words)
        for index, (_, words) in enumerate(commands)
        if words[0] == "if" and any("adb get-state" in " ".join(word.split()) for word in words)
    ]


# The lane's marker expression: the emulator tests, minus whole markers. Never
# minus node ids -- `--deselect` of a test that no longer exists is silent.
LANE_MARKER_EXPRESSION = re.compile(r"emulator((?: and not [a-z_]+)*)")


def _lane_marker_expression(words: list[str]) -> str | None:
    """The ``-m`` expression of a pytest command, when it selects the emulator lane."""
    for position, word in enumerate(words[:-1]):
        if word == "-m" and LANE_MARKER_EXPRESSION.fullmatch(words[position + 1]):
            return words[position + 1]
    return None


def _lane_excluded_markers(script: str) -> list[str]:
    """The markers the lane's ``-m`` expression subtracts (``and not <marker>``)."""
    for _, words in _shell_commands(script):
        expression = _lane_marker_expression(words) if "pytest" in words else None
        if expression is not None:
            return re.findall(r"and not ([a-z_]+)", expression)
    return []


def _check_lane_floor(script: str) -> None:
    """Raise AssertionError unless ``script`` holds the lane to its verdict.

    T11: pytest's status is kept (not left to ``set -e``); a device that is
    gone after pytest exits 3; and otherwise the lane exits 1 unless the
    summary says N passed (N >= 1) with no skipped, failed or error term --
    read from the summary of the log pytest tees to, and in that order.
    """
    commands = _shell_commands(script)

    pytest_runs = [
        (index, words)
        for index, (_, words) in enumerate(commands)
        if "pytest" in words and _lane_marker_expression(words) is not None
    ]
    assert len(pytest_runs) == 1, (
        f"expected exactly one `pytest ... -m emulator` command in the lane "
        f'(`-m emulator`, or `-m "emulator and not <marker>..."`), found {len(pytest_runs)}'
    )
    pytest_index, pytest_words = pytest_runs[0]
    assert "tee" in pytest_words, "the lane no longer tees pytest's output to a log"
    tee_position = pytest_words.index("tee")
    assert tee_position + 1 < len(pytest_words), "`tee` is given no log file"
    lane_log = pytest_words[tee_position + 1]
    captured = pytest_words[tee_position + 2 :]
    assert len(captured) == 2 and captured[0] == "||" and re.fullmatch(r"\w+=\$\?", captured[1]), (
        "pytest's status is not captured (`pytest ... | tee <log> || status=$?`): "
        "under `set -e` a failing run ends the script there, so tests that failed "
        "because the device went away exit 1 and never reach the device check"
    )

    summaries = [
        (index, words)
        for index, (_, words) in enumerate(commands)
        if index > pytest_index and lane_log in words and ">" in words
    ]
    assert len(summaries) == 1, (
        f"expected one command after pytest that writes the summary of {lane_log!r} "
        f"to a file (`> <file>`), found {len(summaries)}: the floor reads no summary "
        f"of this run"
    )
    summary_index, summary_words = summaries[0]
    summary_file = summary_words[summary_words.index(">") + 1]

    floors = _lane_floor(commands)
    assert len(floors) == 1, (
        f"expected one `if` that greps the summary for a passed count, found "
        f"{len(floors)}: the lane no longer fails when no test ran"
    )
    floor_index, floor_words = floors[0]
    assert floor_index > pytest_index, (
        "the floor runs before `pytest -m emulator`, so it reads a log this run has "
        "not written yet"
    )
    assert floor_index > summary_index, "the floor runs before its summary is written"

    operators, clauses = _clauses(_condition(floor_words))
    greps = [_grep_clause(clause) for clause in clauses]
    assert all(greps), f"the floor's condition is not only greps of the summary: {clauses}"
    assert set(operators) <= {"||"}, (
        "the floor's greps are not joined by `||`: any ONE of them must fail the lane, "
        "and `&&` lets through a run that trips only one"
    )
    for _negated, _pattern, read in greps:
        assert read == summary_file, (
            f"the floor greps {read!r} but the summary is written to {summary_file!r}; "
            f"it is checking a file this run did not write"
        )
    passed = [grep for grep in greps if _counts_passed(grep[1])]
    assert len(passed) == 1 and passed[0][0], (
        "the passed count is not negated (`! grep ... passed`): the floor must fail "
        "the lane when NO test passed"
    )
    failing = [grep[1] for grep in greps if not grep[0]]
    for term in FLOOR_FAILING_TERMS:
        assert any(_searches(pattern, term) for pattern in failing), (
            f"the floor lets {term!r} through. A skip in the gated lane means a "
            f"precondition failed; the floor is ZERO skipped, failed or errored"
        )
    for term in FLOOR_NEUTRAL_TERMS:
        assert not any(
            _searches(pattern, term) for pattern in failing
        ), f"the floor fails the lane on {term!r}, which a green lane prints"
    assert ["exit", "1"] in [words[:2] for words in _then_branch(commands, floor_index)], (
        "the floor no longer exits 1; the lane goes green having skipped, failed or "
        "exercised no device"
    )

    checks = _device_checks(commands)
    assert len(checks) == 1, (
        f"expected one `if` on `adb get-state` after pytest, found {len(checks)}: "
        f"a device that died mid-run reads as failing tests, never retried"
    )
    check_index, _ = checks[0]
    assert check_index > pytest_index, "the device check runs before pytest"
    assert check_index < floor_index, (
        "the device check runs after the floor: tests that failed because the device "
        "went away exit 1 there, and a lost device is never retried"
    )
    branch = _then_branch(commands, check_index)
    assert ["exit", "3"] in [
        words[:2] for words in branch
    ], "a lost device no longer exits 3, the one status the workflow retries"
    assert any(
        words[0] == "echo" and "DEVICE LOST after" in " ".join(words[1:]) for words in branch
    ), "a lost device is not named (`DEVICE LOST after N passed`)"

    passed_through = [
        words for _, words in commands if words[0] == "exit" and "$" in "".join(words)
    ]
    assert not passed_through, (
        f"the lane exits with a status it did not choose ({passed_through}); pytest's "
        f"own 3 is 'internal error', which would read as a lost device and be retried"
    )


def test_the_emulator_lane_fails_when_every_test_skips():
    """A lane where anything skipped is not a lane that passed.

    Every emulator test skips politely when no device or fixture app is
    present -- correct on a laptop, useless as a gate. The script must notice.

    N7: this guard used to assert the SUBSTRINGS "passed" and "exit 1" occur
    somewhere in the script, which comments and echoes also satisfied; it then
    read the script's commands for a ``grep -qE '[0-9]+ passed'`` floor.

    T11 (Inc 2): the floor became ZERO skipped, because ``live_device`` resolves
    once per session -- a device that dies after the first pass made the rest
    skip or fail, and "at least one passed" read that as green. The guard now
    pins the whole verdict: pytest's status captured, the summary of the log it
    tees, a device check exiting 3 between pytest and the floor, and the floor
    exiting 1 unless N passed (N >= 1) with no skipped, failed or error term.
    """
    _check_lane_floor(LANE_SCRIPT.read_text(encoding="utf-8"))


def _if_block(lines: list[str], find) -> tuple[int, int]:
    """0-based (``if``, ``fi``) line indexes of the one ``if`` ``find`` returns."""
    commands = _shell_commands("\n".join(lines))
    ((index, _),) = find(commands)
    depth = 0
    for number, words in commands[index:]:
        if words[0] == "if":
            depth += 1
        elif words[0] == "fi":
            depth -= 1
            if depth == 0:
                return commands[index][0] - 1, number - 1
    raise AssertionError("the real block has changed shape")


def _line_in(lines: list[str], block: tuple[int, int], prefix: str) -> int:
    """The first line of ``block`` that starts (after indentation) with ``prefix``."""
    start, end = block
    return next(i for i in range(start, end + 1) if lines[i].strip().startswith(prefix))


def _line_starting(lines: list[str], prefix: str) -> int:
    return next(i for i, line in enumerate(lines) if line.startswith(prefix))


def _delete_floor(lines: list[str]) -> list[str]:
    start, end = _if_block(lines, _lane_floor)
    return lines[:start] + lines[end + 1 :]


def _delete_exit(lines: list[str]) -> list[str]:
    exit_line = _line_in(lines, _if_block(lines, _lane_floor), "exit 1")
    return lines[:exit_line] + lines[exit_line + 1 :]


def _exit_into_else(lines: list[str]) -> list[str]:
    exit_line = _line_in(lines, _if_block(lines, _lane_floor), "exit 1")
    return lines[:exit_line] + ["else"] + lines[exit_line:]


def _floor_before_pytest(lines: list[str]) -> list[str]:
    start, end = _if_block(lines, _lane_floor)
    block = lines[start : end + 1]
    rest = lines[:start] + lines[end + 1 :]
    pytest_line = _line_starting(rest, "pytest ")
    return rest[:pytest_line] + block + rest[pytest_line:]


def _floor_reads_another_file(lines: list[str]) -> list[str]:
    start, _ = _if_block(lines, _lane_floor)
    return [
        line.replace("emulator-lane.summary", "some-other.summary") if i == start else line
        for i, line in enumerate(lines)
    ]


def _summary_reads_another_log(lines: list[str]) -> list[str]:
    summary = _line_starting(lines, "grep -E")
    return [
        line.replace("emulator-lane.log", "some-other.log") if i == summary else line
        for i, line in enumerate(lines)
    ]


def _skips_allowed(lines: list[str]) -> list[str]:
    start, _ = _if_block(lines, _lane_floor)
    return [line.replace("skipped|", "") if i == start else line for i, line in enumerate(lines)]


def _delete_device_check(lines: list[str]) -> list[str]:
    start, end = _if_block(lines, _device_checks)
    return lines[:start] + lines[end + 1 :]


def _device_check_after_floor(lines: list[str]) -> list[str]:
    start, end = _if_block(lines, _device_checks)
    block = lines[start : end + 1]
    rest = lines[:start] + lines[end + 1 :]
    _, floor_end = _if_block(rest, _lane_floor)
    return rest[: floor_end + 1] + block + rest[floor_end + 1 :]


def _device_loss_exits_1(lines: list[str]) -> list[str]:
    exit_line = _line_in(lines, _if_block(lines, _device_checks), "exit 3")
    return [
        line.replace("exit 3", "exit 1") if i == exit_line else line for i, line in enumerate(lines)
    ]


def _pytest_status_uncaptured(lines: list[str]) -> list[str]:
    pytest_line = _line_starting(lines, "pytest ")
    return [
        line.replace(" || pytest_status=$?", "") if i == pytest_line else line
        for i, line in enumerate(lines)
    ]


LANE_MUTATIONS = {
    "floor deleted": _delete_floor,
    "exit 1 deleted": _delete_exit,
    "exit 1 moved into else": _exit_into_else,
    "floor moved before pytest": _floor_before_pytest,
    "floor reads another file": _floor_reads_another_file,
    "summary reads another log": _summary_reads_another_log,
    "skips allowed": _skips_allowed,
    "get-state check deleted": _delete_device_check,
    "device check moved after the floor": _device_check_after_floor,
    "device loss exits 1": _device_loss_exits_1,
    "pytest status left to set -e": _pytest_status_uncaptured,
}


@pytest.mark.parametrize("mutation", list(LANE_MUTATIONS))
def test_the_lane_guard_rejects_each_way_of_losing_the_floor(mutation):
    """Each way of losing the verdict, kept as a regression test.

    Each is applied to the REAL script's text, so they follow the script as
    it changes, and each must make the guard fail. The ``else`` case is the
    review of PR #20's: the first structural guard collected every line before
    ``fi`` and accepted an ``exit 1`` that only runs when the lane was clean.
    """
    lines = LANE_SCRIPT.read_text(encoding="utf-8").splitlines()
    mutated = "\n".join(LANE_MUTATIONS[mutation](lines))
    assert mutated != "\n".join(lines), f"{mutation}: the mutation changed nothing"
    with pytest.raises(AssertionError):
        _check_lane_floor(mutated)


def test_the_emulator_lane_asserts_the_fixture_app_installed():
    """Without the app, the end-to-end agent test skips and the lane fails on it.

    The skip would now fail the floor anyway; this check is what says WHY.
    """
    commands = _shell_commands(LANE_SCRIPT.read_text(encoding="utf-8"))
    checks = [
        index
        for index, (_, words) in enumerate(commands)
        if words[0] == "if" and any(words[i : i + 2] == ["pm", "path"] for i in range(len(words)))
    ]
    assert len(checks) == 1, (
        "the lane no longer asserts the fixture app installed; the end-to-end "
        "agent test would skip, and the lane would not say why"
    )
    assert ["exit", "1"] in [words[:2] for words in _then_branch(commands, checks[0])]


def _marks_in_tests() -> set[str]:
    """Every ``pytest.mark.<name>`` applied anywhere under tests/, read from the AST."""
    used: set[str] = set()
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "pytest"
            ):
                used.add(node.attr)
    return used


def test_every_marker_the_lane_excludes_is_registered_and_still_on_a_test():
    """A lane exclusion must not outlive what it excludes.

    The two-device test was deselected by node id until a review showed pytest
    accepts `--deselect` of a test that does not exist, exit 0: rename the test
    and the stale line excludes nothing, says nothing, and stays. A marker
    excludes whatever carries it -- and this checks that something still does,
    and that pyproject.toml registers it.
    """
    excluded = _lane_excluded_markers(LANE_SCRIPT.read_text(encoding="utf-8"))
    assert excluded, "the lane excludes no marker; if that is deliberate, delete this test"
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    registered = {
        line.split(":", 1)[0].strip()
        for line in pyproject["tool"]["pytest"]["ini_options"]["markers"]
    }
    used = _marks_in_tests()
    for marker in excluded:
        assert marker in registered, f"the lane excludes `{marker}`, which is not registered"
        assert marker in used, f"the lane excludes `{marker}`, which no test carries any more"


# ---------------------------------------------------------------------------
# The retry (T11d): exit 3 only, once, counted.
# ---------------------------------------------------------------------------

RUNNER_ACTION = "reactivecircus/android-emulator-runner@"

# The one condition the retry may run on: a step output equal to '3'.
RETRY_CONDITION = re.compile(r"\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*==\s*'3'\s*")

FULL_SHA_PIN = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")


def _lane_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["emulator"]["steps"]


def _check_lane_retry(workflow: dict) -> None:
    """Raise AssertionError unless the lane is retried on exit 3 only, and once."""
    steps = _lane_steps(workflow)
    runs = [
        (index, step)
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith(RUNNER_ACTION)
    ]
    assert (
        len(runs) == 2
    ), f"expected the lane and ONE retry, found {len(runs)} emulator-runner steps"
    (first_index, first), (retry_index, retry) = runs
    assert first.get("continue-on-error") is True and retry.get("continue-on-error") is True, (
        "an attempt fails the job by itself; the verdict step must be the one place "
        "that decides, or a retried flake is red and nothing counts the retry"
    )
    assert retry.get("with") == first.get("with"), "the retry does not run the same lane"
    attempts = [str((step.get("env") or {}).get("LANE_ATTEMPT")) for step in (first, retry)]
    assert attempts == ["1", "2"], (
        f"LANE_ATTEMPT is {attempts}, not ['1', '2']: the retry writes its diagnostics "
        f"over the evidence of the loss it retried"
    )

    condition = RETRY_CONDITION.fullmatch(str(retry.get("if", "")))
    assert condition, (
        f"the retry runs on {retry.get('if')!r}; it must run on exit status 3 alone "
        f"(`steps.<id>.outputs.<name> == '3'`) -- a failing test is never retried"
    )
    readers = [index for index, step in enumerate(steps) if step.get("id") == condition.group(1)]
    assert (
        len(readers) == 1 and first_index < readers[0] < retry_index
    ), "the step whose output the retry reads is not between the two attempts"

    verdicts = [
        index
        for index, step in enumerate(steps)
        if step.get("id") == "verdict" and step.get("if") == "always()"
    ]
    assert len(verdicts) == 1 and verdicts[0] > retry_index, (
        "no `verdict` step runs always() after the retry: with both attempts "
        "continuing on error, nothing would fail the job"
    )
    uploads = [
        step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1 and FULL_SHA_PIN.fullmatch(
        uploads[0]["uses"]
    ), "the lane's diagnostics are not uploaded by an upload-artifact pinned to a commit"
    assert "failure()" in str(uploads[0].get("if", "")), "diagnostics are not uploaded on failure"


def test_the_lane_is_retried_on_a_lost_device_only_and_once():
    """T11(d): retry ONLY exit 3, ONLY once. A required check at ~25% flake is not
    acceptable, and un-requiring it is worse -- six v0.7.0 defects only the lane
    found -- so a lost device gets one more emulator and a failing test none."""
    _check_lane_retry(_yaml("emulator.yml"))


def _retry_on_any_exit(text: str) -> str:
    return text.replace(
        "if: steps.first-exit.outputs.code == '3'", "if: steps.attempt1.outcome == 'failure'"
    )


def _second_retry(text: str) -> str:
    start = text.index("      - name: Retry the emulator lane")
    end = text.index("      - name: Lane verdict")
    retry = text[start:end].replace("id: attempt2", "id: attempt3")
    return text[:end] + retry + text[end:]


def _first_attempt_fails_the_job(text: str) -> str:
    return text.replace("        continue-on-error: true\n", "", 1)


RETRY_MUTATIONS = {
    "retry on any exit": _retry_on_any_exit,
    "a second retry": _second_retry,
    "first attempt fails the job": _first_attempt_fails_the_job,
}


@pytest.mark.parametrize("mutation", list(RETRY_MUTATIONS))
def test_the_retry_guard_rejects_each_way_of_widening_the_retry(mutation):
    """Applied to the real workflow's text; each must make the guard fail."""
    import yaml

    text = (WORKFLOWS / "emulator.yml").read_text(encoding="utf-8")
    mutated = RETRY_MUTATIONS[mutation](text)
    assert mutated != text, f"{mutation}: the mutation changed nothing"
    with pytest.raises(AssertionError):
        _check_lane_retry(yaml.safe_load(mutated))


# ---------------------------------------------------------------------------
# The lane, run. The guards above read the script; these run it, against stub
# tools on PATH, and feed what it wrote into the workflow's own steps -- the
# same chain a CI run goes through, minus the emulator.
# ---------------------------------------------------------------------------

FIXTURE_PACKAGE = "com.example.composefixture"
FIXTURE_APK = "tests/fixtures/scaffold/compose/app/build/outputs/apk/debug/app-debug.apk"

# A stand-in pytest that runs the REAL pytest over a scenario module, so the
# summary line the lane reads is pytest's own output, never a transcript.
_FAKE_PYTEST = """#!{python}
import json
import subprocess
import sys

with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(["pytest", *sys.argv[1:]]) + "\\n")
command = [sys.executable, "-m", "pytest", {scenario!r}, "-v", "--color=no", "-p", "no:cacheprovider"]
sys.exit(subprocess.run(command, timeout=120, check=False).returncode)
"""

# Host tools the lane's diagnostics touch. None of them may reach the host:
# `sudo` and `dmesg` especially.
_HOST_STUBS = {
    "timeout": 'shift\nexec "$@"\n',
    "sudo": '[ "$1" = "-n" ] && shift\nexec "$@"\n',
    "dmesg": "exit 0\n",
    "pgrep": "exit 1\n",
}

# Each outcome as a test that produces it, (decorator, body): the scenario
# module is run by the real pytest, so every summary term the floor reads --
# xfailed, xpassed, warnings -- is pytest's own spelling.
_SCENARIO_TESTS = {
    "pass": ("", "pass"),
    "skip": ("", "pytest.skip('precondition missing')"),
    "fail": ("", "assert False, 'a failing test'"),
    "xfail": ("@pytest.mark.xfail(reason='a pinned defect')", "assert False"),
    "xpass": ("@pytest.mark.xfail(reason='a pinned defect')", "pass"),
    "strict_xpass": ("@pytest.mark.xfail(strict=True, reason='a fixed defect')", "pass"),
    "warn": ("", "warnings.warn(UserWarning('deprecated'))"),
}


def _run_lane(
    tmp_path: Path,
    outcomes: tuple[str, ...],
    *,
    device_after: bool,
    installed: bool = True,
    attempt: str = "1",
):
    """Run a copy of the lane script; return (result, repo copy, toolchain)."""
    repo = tmp_path / "repo"
    script = repo / ".github" / "scripts" / LANE_SCRIPT.name
    script.parent.mkdir(parents=True)
    script.write_text(LANE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    apk = repo / FIXTURE_APK
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"")

    scenario = tmp_path / "scenario" / "test_scenario.py"
    scenario.parent.mkdir()
    body = ["import warnings", "", "import pytest", ""]
    for number, outcome in enumerate(outcomes):
        decorator, statement = _SCENARIO_TESTS[outcome]
        body += [decorator, f"def test_{number}_{outcome}():", f"    {statement}", ""]
    scenario.write_text("\n".join(body), encoding="utf-8")

    toolchain = FakeToolchain(tmp_path / "toolchain")
    toolchain.install(
        "adb",
        [
            # As emulator-5554 answers: `adb get-state` prints `device` (exit
            # 0), and for a device that is not there prints nothing on stdout
            # (exit 1); `pm path` exits 0 for an installed package and 1 for
            # one that is not, and the lane reads only that status.
            Rule(
                ("get-state",),
                stdout="device\n" if device_after else "",
                exit=0 if device_after else 1,
            ),
            Rule(("pm", "path", FIXTURE_PACKAGE), exit=0 if installed else 1),
        ],
        default_exit=0,
    )
    fake_pytest = toolchain.bin / "pytest"
    fake_pytest.write_text(
        _FAKE_PYTEST.format(
            python=sys.executable, log=str(toolchain.log), scenario=str(scenario.parent)
        )
    )
    fake_pytest.chmod(0o755)
    for name, stub_body in _HOST_STUBS.items():
        stub = toolchain.bin / name
        stub.write_text("#!/bin/sh\n" + stub_body)
        stub.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(script)],
        env=toolchain.env(LANE_ATTEMPT=attempt),
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return result, repo, toolchain


LANE_SCENARIOS = {
    # name: (outcomes, device still there after pytest, exit status, what it says)
    "all passed": (("pass", "pass"), True, 0, "2 passed"),
    "one skipped": (("pass", "skip"), True, 1, "nothing skipped, failed or errored"),
    "a test failed": (("pass", "fail"), True, 1, "nothing skipped, failed or errored"),
    "nothing ran": ((), True, 1, "nothing skipped, failed or errored"),
    "device gone after pytest": (("pass", "pass"), False, 3, "DEVICE LOST after 2 passed"),
    "device gone and tests failed": (("pass", "fail"), False, 3, "DEVICE LOST after 1 passed"),
    # The summary terms that are not skips, failures or errors. An xfail is a
    # pinned defect, so it may sit beside passes -- but alone it is no pass.
    "only xfailed": (("xfail",), True, 1, "nothing skipped, failed or errored"),
    "passed beside an xfail": (("pass", "xfail"), True, 0, "1 xfailed"),
    "passed beside a non-strict xpass": (("pass", "xpass"), True, 0, "1 xpassed"),
    "a strict xpass": (("pass", "strict_xpass"), True, 1, "nothing skipped, failed or errored"),
    "passed with a warning": (("warn",), True, 0, "1 warning"),
}


@pytest.mark.parametrize("scenario", list(LANE_SCENARIOS))
def test_the_lane_script_under_stub_tools(tmp_path, scenario):
    """T11's acceptance, run: each outcome exits with the status the workflow reads.

    A device gone after pytest is 3 even when tests failed -- those failures are
    what a lost device looks like, and 1 would never be retried.
    """
    outcomes, device_after, status, says = LANE_SCENARIOS[scenario]
    result, repo, toolchain = _run_lane(tmp_path, outcomes, device_after=device_after)
    shown = f"\nstdout:\n{result.stdout[-1500:]}\nstderr:\n{result.stderr[-1500:]}"

    assert result.returncode == status, f"exit {result.returncode}, not {status}{shown}"
    assert says in result.stdout, f"the lane does not say {says!r}{shown}"

    diagnostics = repo / "emulator-lane-diagnostics" / "attempt-1"
    assert (diagnostics / "exit-code").read_text().strip() == str(status)
    captured = sorted(path.name for path in diagnostics.iterdir())
    if status:
        expected = {"emulator-lane.log", "adb-devices.txt", "pgrep-emulator.txt", "dmesg-tail.txt"}
        assert expected <= set(captured), f"a failure left no diagnostics: {captured}"
    else:
        assert captured == ["exit-code"], f"a clean run left diagnostics behind: {captured}"

    adb = toolchain.calls("adb")
    assert ["install", "-r", str(repo / FIXTURE_APK)] in adb, "the built APK was not installed"
    assert ["get-state"] in adb, "the lane never asked whether the device was still there"
    (pytest_argv,) = toolchain.calls("pytest")
    assert pytest_argv[:3] == ["tests/", "-m", "emulator and not two_devices"], pytest_argv


def test_the_lane_refuses_to_run_without_the_fixture_app(tmp_path):
    """No app, no lane: exit 1 saying so, before pytest is run at all."""
    result, _, toolchain = _run_lane(tmp_path, ("pass",), device_after=True, installed=False)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{FIXTURE_PACKAGE} is not installed" in result.stdout
    assert toolchain.calls("pytest") == [], "pytest ran without the app it needs"


def _run_step(step: dict, tmp_path: Path, cwd: Path, **env: str) -> tuple[int, dict, str]:
    """Run a workflow step's ``run:`` as GitHub's ``shell: bash`` would.

    Returns (exit status, the outputs it wrote, the step summary it wrote).
    """
    assert step.get("shell") == "bash", f"{step.get('id')} does not declare `shell: bash`"
    assert "${{" not in step["run"], f"{step.get('id')} interpolates an expression into shell"
    body = tmp_path / f"{step['id']}.sh"
    body.write_text(step["run"], encoding="utf-8")
    outputs = tmp_path / f"{step['id']}.outputs"
    summary = tmp_path / f"{step['id']}.summary"
    outputs.write_text("")
    summary.write_text("")
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", str(body)],
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_STEP_SUMMARY": str(summary),
            **env,
        },
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    written = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    return result.returncode, written, summary.read_text()


def _step(step_id: str) -> dict:
    (step,) = [step for step in _lane_steps(_yaml("emulator.yml")) if step.get("id") == step_id]
    return step


def _first_attempt(tmp_path: Path, outcomes, *, device_after: bool) -> tuple[str, bool]:
    """Run the lane once, then the workflow's reader and its retry condition on it.

    Returns (the exit status the reader handed on, whether the retry runs).
    """
    _, repo, _ = _run_lane(tmp_path, outcomes, device_after=device_after)
    _, outputs, _ = _run_step(_step("first-exit"), tmp_path, repo)
    condition = RETRY_CONDITION.fullmatch(_step("attempt2")["if"])
    assert condition and condition.group(1) == "first-exit"
    return outputs.get(condition.group(2), ""), outputs.get(condition.group(2)) == "3"


def test_a_lost_device_is_retried_once_and_counted(tmp_path):
    """The lane's exit 3, through the workflow's own steps: one retry, `retries: 1`.

    The retry (attempt 2, run here too) decides the job: green if it passed,
    red if it did not -- and the summary names its exit status, so a second
    lost device (3) is told apart from a test that failed once the device held.
    """
    code, retried = _first_attempt(tmp_path, ("pass", "pass"), device_after=False)
    assert (code, retried) == ("3", True)

    for device_after, retry_outcome, verdict in ((True, "success", 0), (False, "failure", 1)):
        root = tmp_path / f"retry-{retry_outcome}"
        result, repo, _ = _run_lane(root, ("pass",), device_after=device_after, attempt="2")
        assert (result.returncode == 0) == (retry_outcome == "success"), result.stdout

        status, _, summary = _run_step(
            _step("verdict"),
            root,
            repo,
            FIRST_OUTCOME="failure",
            FIRST_EXIT=code,
            RETRY_OUTCOME=retry_outcome,
        )
        lines = summary.splitlines()
        assert status == verdict, f"a retry that ended {retry_outcome} gave the job {status}"
        assert "retries: 1" in lines
        assert f"Retry: {retry_outcome} (exit status {result.returncode})." in lines, lines


def test_a_failing_test_is_never_retried(tmp_path):
    """The lane's exit 1, through the workflow's own steps: no retry, a red job."""
    code, retried = _first_attempt(tmp_path, ("pass", "fail"), device_after=True)
    assert (code, retried) == ("1", False)

    status, _, summary = _run_step(
        _step("verdict"), tmp_path, tmp_path, FIRST_OUTCOME="failure", FIRST_EXIT=code
    )
    assert status == 1
    assert "retries: 0" in summary.splitlines()


def test_a_lane_that_never_ran_is_not_retried(tmp_path):
    """The runner failed before the script ran: no exit-code file, so `none`, not 3."""
    _, outputs, _ = _run_step(_step("first-exit"), tmp_path, tmp_path)
    assert outputs == {"code": "none"}

    status, _, summary = _run_step(
        _step("verdict"), tmp_path, tmp_path, FIRST_OUTCOME="failure", FIRST_EXIT="none"
    )
    assert status == 1
    assert "retries: 0" in summary.splitlines()


def test_a_clean_first_attempt_is_green_with_no_retry(tmp_path):
    status, _, summary = _run_step(_step("verdict"), tmp_path, tmp_path, FIRST_OUTCOME="success")
    assert status == 0
    assert "retries: 0" in summary.splitlines()


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


def _skill_md_scripts_heading_version() -> str:
    """The version in SKILL.md's ``## Scripts (vX.Y.Z)`` heading.

    A second place the version is written by hand, and the one nothing was
    watching: `_skill_md_version` above reads the frontmatter, and so do
    `validate-version.yml` and the `version` job in `release.yml`. The heading
    could therefore drift a whole release behind while every guard stayed
    green -- and it is the line a reader actually sees above the script table.
    """
    match = re.search(
        r"^##\s+Scripts\s+\(v(\S+?)\)\s*$",
        SKILL_MD.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "SKILL.md has no `## Scripts (vX.Y.Z)` heading"
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
        ("SKILL.md `## Scripts` heading", _skill_md_scripts_heading_version),
    ],
)
def test_every_manifest_declares_the_same_version_as_pyproject(label, getter):
    """Both plugin manifests previously had no version at all, so CI could not
    detect drift -- it only checked pyproject.toml and *warned* on SKILL.md.

    The `## Scripts (vX.Y.Z)` heading is here for the same reason one step on:
    every automated check reads SKILL.md's FRONTMATTER, so the heading was a
    hand-maintained version nothing verified.
    """
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
