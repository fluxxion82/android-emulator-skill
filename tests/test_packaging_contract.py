"""What the released package actually contains, and what the docs claim.

`release.yml` zips only `android-emulator-skill/`, so anything outside that
directory — the repo README, CLAUDE.md, CHANGELOG.md, `docs/` — does not reach a
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
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
import test_instrumented_live
from fake_toolchain import FakeToolchain, Rule
from test_quick_start_contract import (
    code_lines,
    fence_problems,
    flush_prose_lines,
    markdown_lines,
    prose_lines,
    section,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "android-emulator-skill"
SKILL_ROOT = PACKAGE_ROOT / "skills" / "android-emulator-skill"
SKILL_MD = SKILL_ROOT / "SKILL.md"
REFERENCES = SKILL_ROOT / "references"
README = REPO_ROOT / "README.md"
PACKAGED_README = SKILL_ROOT / "README.md"


def _shipped_docs() -> list[Path]:
    """SKILL.md and each reference file: what an installed agent can open.

    A guard on "the documentation" reads all of these. A rule that held for
    SKILL.md alone would stop holding the day a paragraph moved next door.
    """
    return [SKILL_MD, *sorted(REFERENCES.glob("*.md"))]


def _packaged_markdown() -> list[Path]:
    """Every Markdown file the release zips, at any depth: the README too."""
    return sorted(PACKAGE_ROOT.rglob("*.md"))


def _doc_id(doc: Path) -> str:
    root = SKILL_ROOT if SKILL_ROOT in doc.parents else PACKAGE_ROOT
    return doc.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Invocation.
# ---------------------------------------------------------------------------


def _code_block_lines(markdown: str) -> list[str]:
    """Lines inside fenced code blocks — the parts a reader will actually run.

    Prose that *describes* the wrong invocation (including the sentence
    explaining this very rule) is not a defect, and a whole-file scan cannot
    tell the two apart.
    """
    return code_lines(markdown)


def _bare_invocations(markdown: str) -> list[str]:
    return [
        line.strip()
        for line in _code_block_lines(markdown)
        if re.search(r"python3?\s+scripts/", line)
    ]


@pytest.mark.parametrize("doc", _shipped_docs(), ids=_doc_id)
def test_no_bare_relative_script_invocations_in_a_shipped_doc(doc):
    """`python scripts/foo.py` only works if cwd is the skill root."""
    offenders = _bare_invocations(doc.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{_doc_id(doc)} documents bare relative invocations, which fail for an "
        f"installed plugin: {offenders[:5]}"
    )


def test_the_bare_invocation_guard_reads_a_reference_file_as_it_reads_skill_md():
    """The rule moved with the text: an example is an example in either file."""
    example = "```bash\npython scripts/screen_mapper.py --json\n```\n"
    reference = REFERENCES / "known-gaps.md"

    assert _bare_invocations(reference.read_text(encoding="utf-8") + example)
    assert not _bare_invocations("Never write `python scripts/foo.py`.\n")


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
    documenting it fails here. The shell health check is a script too: SKILL.md
    numbers it among the others, and counting only `*.py` left the heading one
    short of the categories under it (32 against 33).
    """
    text = SKILL_MD.read_text(encoding="utf-8")

    scripts = _scripts_on_disk()
    common = _common_modules_on_disk()

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


def _scripts_on_disk() -> list[str]:
    """Every script an agent can run: the Python scripts and the shell health check."""
    return sorted(p.name for p in (SKILL_ROOT / "scripts").iterdir() if p.suffix in (".py", ".sh"))


def _common_modules_on_disk() -> list[str]:
    return sorted(
        p.name for p in (SKILL_ROOT / "scripts" / "common").glob("*.py") if p.name != "__init__.py"
    )


_CATEGORY_HEADING = re.compile(r"^#### (.+?) \((\d+) scripts?\)")
_ANY_HEADING = re.compile(r"^#{1,4} ")
_NUMBERED_SCRIPT = re.compile(r"^\d+\.\s+\*\*([\w.]+\.(?:py|sh))\*\*")


def _skill_md_categories() -> dict[str, tuple[int, list[str]]]:
    """{category: (count its heading claims, scripts numbered under it)}.

    A category runs from its `#### Name (N scripts)` heading to the next
    heading of level four or above; the scripts in it are its numbered
    `N. **name**` entries, the shape the inventory guard above reads.
    """
    categories: dict[str, tuple[int, list[str]]] = {}
    current: str | None = None
    for line in SKILL_MD.read_text(encoding="utf-8").splitlines():
        if heading := _CATEGORY_HEADING.match(line):
            current = heading.group(1)
            categories[current] = (int(heading.group(2)), [])
        elif _ANY_HEADING.match(line):
            current = None
        elif current and (entry := _NUMBERED_SCRIPT.match(line)):
            categories[current][1].append(entry.group(1))
    return categories


def test_skill_md_category_counts_add_up_to_the_scripts_heading():
    """Each `#### Category (N scripts)` holds N entries, and the Ns sum to the total.

    The heading said 32 while the categories under it held 33: the shell health
    check was numbered in a category and left out of the total.
    """
    categories = _skill_md_categories()
    assert categories, "SKILL.md has no `#### Category (N scripts)` headings"
    wrong = {
        name: (claimed, len(entries))
        for name, (claimed, entries) in categories.items()
        if claimed != len(entries)
    }
    assert (
        not wrong
    ), f"category headings whose count is not their entries' (claimed, held): {wrong}"

    listed = sorted(name for _, entries in categories.values() for name in entries)
    assert listed == _scripts_on_disk(), (
        "the categories do not list each script on disk exactly once: "
        f"{sorted(set(listed) ^ set(_scripts_on_disk()))} differ, "
        f"{sorted({n for n in listed if listed.count(n) > 1})} repeat"
    )
    total = re.search(r"### Implemented \((\d+) scripts\)", SKILL_MD.read_text(encoding="utf-8"))
    assert total, "SKILL.md no longer announces a script count"
    assert int(total.group(1)) == sum(
        claimed for claimed, _ in categories.values()
    ), f"`Implemented ({total.group(1)} scripts)` != the categories' sum"


# ---------------------------------------------------------------------------
# The docs site says what the package holds.
# ---------------------------------------------------------------------------

SITE_HTML = REPO_ROOT / "site" / "index.html"


class _SiteReading(HTMLParser):
    """The site as a reader meets it: its text, its meta descriptions, its catalog.

    ``text``: every text node and every ``<meta>`` description, in order.
    ``catalog``: per catalog ``<li>``, the script its first ``<code>`` names and
    the flags its ``<em>`` lists. ``groups``: the number of catalog groups.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.catalog: dict[str, set[str]] = {}
        self.groups = 0
        self._li: str | None = None
        self._in_li = self._in_code = self._in_em = False

    def handle_starttag(self, tag, attrs):
        named = dict(attrs)
        classes = (named.get("class") or "").split()
        if tag == "meta" and "description" in (named.get("name") or named.get("property") or ""):
            self.text.append(named.get("content") or "")
        elif tag == "div" and "catalog-group" in classes:
            self.groups += 1
        elif tag == "li":
            self._in_li, self._li = True, None
        elif tag == "code":
            self._in_code = True
        elif tag == "em":
            self._in_em = True

    def handle_endtag(self, tag):
        if tag == "li":
            self._in_li, self._li = False, None
        elif tag == "code":
            self._in_code = False
        elif tag == "em":
            self._in_em = False

    def handle_data(self, data):
        self.text.append(data)
        if not self._in_li:
            return
        if self._li is None and self._in_code and data.strip().endswith((".py", ".sh")):
            self._li = data.strip()
            self.catalog.setdefault(self._li, set())
        elif self._li and self._in_em:
            self.catalog[self._li] |= set(_FLAG.findall(data))


@functools.cache
def _site() -> _SiteReading:
    reading = _SiteReading()
    reading.feed(SITE_HTML.read_text(encoding="utf-8"))
    reading.close()
    return reading


def _site_numbers(pattern: str) -> list[int]:
    """Every number the site's text states in ``pattern`` (one group), whitespace folded."""
    folded = " ".join(" ".join(_site().text).split())
    return [int(n) for n in re.findall(pattern, folded, re.IGNORECASE)]


def test_the_site_states_the_number_of_scripts_on_disk():
    """The site said "29 scripts" in five places while 33 existed (32 Python, one shell)."""
    stated = _site_numbers(r"\b(\d+) (?:production )?scripts\b")
    assert stated, "site/index.html no longer states how many scripts there are"
    on_disk = len(_scripts_on_disk())
    assert set(stated) == {on_disk}, f"the site says {stated} scripts; {on_disk} exist on disk"


def test_the_site_states_its_category_and_shared_module_counts():
    """`N categories` is the catalog's group count; `N shared modules` is common/'s."""
    categories = _site_numbers(r"\b(\d+) categories\b")
    assert categories, "the site no longer states its category count"
    assert set(categories) == {
        _site().groups
    }, f"the site says {categories} categories; its catalog has {_site().groups} groups"

    modules = _site_numbers(r"\b(\d+) shared modules\b")
    assert modules, "the site no longer states how many shared modules back the scripts"
    common = len(_common_modules_on_disk())
    assert set(modules) == {common}, f"the site says {modules} shared modules; common/ has {common}"


def test_the_site_catalog_lists_every_script_with_flags_it_accepts():
    """The catalog's flags are argparse's, as SKILL.md's are (see the guard below).

    Five catalog entries still described scripts as they were before a rescope
    -- `keyboard --text`, `screen_mapper --list`, `visual_diff --image1`,
    `test_recorder --test-name`, `push_notification --title` -- and five
    scripts were not in the catalog at all.
    """
    catalog = _site().catalog
    assert sorted(catalog) == _scripts_on_disk(), (
        "the site catalog and the scripts on disk differ: "
        f"{sorted(set(catalog) ^ set(_scripts_on_disk()))}"
    )
    wrong = {
        name: sorted(flags - _option_tokens(_help_text(str(SKILL_ROOT / "scripts" / name))))
        for name, flags in catalog.items()
        if name.endswith(".py") and flags
    }
    wrong = {name: bogus for name, bogus in wrong.items() if bogus}
    assert not wrong, f"the site catalog names flags argparse does not accept: {wrong}"


WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _yaml(name: str):
    """Parse a workflow.

    Deliberately NOT `pytest.importorskip("yaml")`: these guards protect the
    release gate, and a guard that skips when a dependency is missing is worth
    nothing. CI installs pyyaml precisely so these run.

    A mapping with two keys the loader reads as one is refused. PyYAML keeps
    the last and drops the other without a word, and it reads YAML 1.1, where
    `yes`, `on` and `true` are one key; GitHub reads 1.2, where they are
    three. What it dropped would be a string no guard here ever saw. (A merge
    key that an own key overrides is refused the same way; no workflow has
    one.)
    """
    import yaml

    class RefusingEqualKeys(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            mapping = super().construct_mapping(node, deep=deep)
            assert len(mapping) == len(node.value), (
                f"{name}:{node.start_mark.line + 1}: this mapping has {len(node.value)} keys "
                f"and PyYAML reads as one key what the file writes as several, leaving "
                f"{len(mapping)}"
            )
            return mapping

    loader = RefusingEqualKeys((WORKFLOWS / name).read_text(encoding="utf-8"))
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


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


def _is_pytest_attribute(node: ast.expr, *path: str) -> bool:
    """True if ``node`` is ``pytest.<path[0]>.<path[1]>...``."""
    for name in reversed(path):
        if not (isinstance(node, ast.Attribute) and node.attr == name):
            return False
        node = node.value
    return isinstance(node, ast.Name) and node.id == "pytest"


def _mark_names(node: ast.expr) -> set[str]:
    """The marks an expression is: ``pytest.mark.<name>``, a call of one, or a list of them."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return {name for element in node.elts for name in _mark_names(element)}
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute) and _is_pytest_attribute(node.value, "mark"):
        return {node.attr}
    return set()


def _decorator_marks(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    """Marks a test's decorators apply, including ``marks=`` on a ``pytest.param`` in them."""
    used: set[str] = set()
    for decorator in node.decorator_list:
        used |= _mark_names(decorator)
        for call in ast.walk(decorator):
            if isinstance(call, ast.Call) and _is_pytest_attribute(call.func, "param"):
                for keyword in call.keywords:
                    if keyword.arg == "marks":
                        used |= _mark_names(keyword.value)
    return used


def _marks_applied_in(body: list[ast.stmt]) -> set[str]:
    """Marks that reach a test from these statements: a module's, or a test class's.

    Applied means a decorator on a ``test*`` function or ``Test*`` class, or a
    ``pytestmark`` assignment. A mark merely named -- ``unused_marker =
    pytest.mark.two_devices`` -- applies to nothing, and an exclusion of it
    excludes nothing. The final v0.8.0 review replaced the two-device test's
    decorator with exactly that line and this guard, then counting every
    ``pytest.mark.<name>`` anywhere in the file, still passed.
    """
    used: set[str] = set()
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(
                isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets
            ):
                used |= _mark_names(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                used |= _decorator_marks(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            used |= _decorator_marks(node) | _marks_applied_in(node.body)
    return used


def _marks_in_tests() -> set[str]:
    """Every pytest mark applied to a test under tests/, read from the AST."""
    used: set[str] = set()
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        used |= _marks_applied_in(ast.parse(path.read_text(encoding="utf-8")).body)
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


SMOKE_TESTS = REPO_ROOT / "tests" / "test_emulator_smoke.py"
TWO_DEVICES_DECORATOR = "@pytest.mark.two_devices"


def _replace_two_devices_decorator(source: str, *replacement: str) -> str:
    lines = source.splitlines()
    (at,) = [index for index, line in enumerate(lines) if line == TWO_DEVICES_DECORATOR]
    return "\n".join([*lines[:at], *replacement, *lines[at + 1 :]]) + "\n"


MARKER_MUTATIONS = {
    # The final v0.8.0 review's: the mark is still in the file, on nothing.
    "decorator replaced by an unused name": lambda source: _replace_two_devices_decorator(
        source, "unused_marker = pytest.mark.two_devices"
    ),
    "decorator moved onto a helper": lambda source: _replace_two_devices_decorator(source)
    + f"\n\n{TWO_DEVICES_DECORATOR}\ndef _helper():\n    pass\n",
}


@pytest.mark.parametrize("mutation", list(MARKER_MUTATIONS))
def test_the_marker_guard_counts_only_a_mark_that_reaches_a_test(mutation):
    """Each way of keeping `two_devices` in the file while no test carries it.

    Applied to the real file's text, so it follows the file as it changes.
    """
    source = SMOKE_TESTS.read_text(encoding="utf-8")
    assert "two_devices" in _marks_applied_in(ast.parse(source).body), "the real test lost it"
    mutated = MARKER_MUTATIONS[mutation](source)
    assert "pytest.mark.two_devices" in mutated, f"{mutation}: the mark left the file"
    assert "two_devices" not in _marks_applied_in(ast.parse(mutated).body), mutation


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
INSTRUMENTED_REPORT_VARIABLE = test_instrumented_live.REPORT_VARIABLE
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
    # As tests/test_instrumented_live.py reports an outcome: a line appended
    # to the file the lane names in the environment.
    "observe": (
        "",
        f"open(__import__('os').environ[{INSTRUMENTED_REPORT_VARIABLE!r}], 'a')"
        ".write('- an instrumented outcome, observed live\\n')",
    ),
}


def _pm_path_rule(recorded, *, installed: bool) -> Rule:
    """`pm path <fixture app>` answered as the device answered it, installed or not.

    Both are recorded: pm_path_installed (`package:<apk>` on stdout, exit 0)
    and pm_path_absent (nothing on either stream, exit 1). The absent one was
    asked of a package that is not installed, and named nothing, so it stands
    for the fixture app unchanged.
    """
    name = "pm_path_installed" if installed else "pm_path_absent"
    entry = {f["name"]: f for f in recorded.manifest["fixtures"]}[name]
    assert entry["stderr_bytes"] == 0, entry
    stdout = recorded.text(name)
    assert entry["stdout_bytes"] == len(stdout.encode()), entry
    return Rule(("pm", "path", FIXTURE_PACKAGE), stdout=stdout, exit=entry["exit_status"])


def _run_lane(
    tmp_path: Path,
    outcomes: tuple[str, ...],
    recorded,
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
            # (exit 1). `pm path` is recorded (see _pm_path_rule); the lane
            # reads only its status.
            Rule(
                ("get-state",),
                stdout="device\n" if device_after else "",
                exit=0 if device_after else 1,
            ),
            _pm_path_rule(recorded, installed=installed),
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
def test_the_lane_script_under_stub_tools(tmp_path, recorded, scenario):
    """T11's acceptance, run: each outcome exits with the status the workflow reads.

    A device gone after pytest is 3 even when tests failed -- those failures are
    what a lost device looks like, and 1 would never be retried.
    """
    outcomes, device_after, status, says = LANE_SCENARIOS[scenario]
    result, repo, toolchain = _run_lane(tmp_path, outcomes, recorded, device_after=device_after)
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


def test_the_lane_refuses_to_run_without_the_fixture_app(tmp_path, recorded):
    """No app, no lane: exit 1 saying so, before pytest is run at all."""
    result, _, toolchain = _run_lane(
        tmp_path, ("pass",), recorded, device_after=True, installed=False
    )

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


def _first_attempt(tmp_path: Path, outcomes, recorded, *, device_after: bool) -> tuple[str, bool]:
    """Run the lane once, then the workflow's reader and its retry condition on it.

    Returns (the exit status the reader handed on, whether the retry runs).
    """
    _, repo, _ = _run_lane(tmp_path, outcomes, recorded, device_after=device_after)
    _, outputs, _ = _run_step(_step("first-exit"), tmp_path, repo)
    condition = RETRY_CONDITION.fullmatch(_step("attempt2")["if"])
    assert condition and condition.group(1) == "first-exit"
    return outputs.get(condition.group(2), ""), outputs.get(condition.group(2)) == "3"


def test_a_lost_device_is_retried_once_and_counted(tmp_path, recorded):
    """The lane's exit 3, through the workflow's own steps: one retry, `retries: 1`.

    The retry (attempt 2, run here too) decides the job: green if it passed,
    red if it did not -- and the summary names its exit status, so a second
    lost device (3) is told apart from a test that failed once the device held.
    """
    code, retried = _first_attempt(tmp_path, ("pass", "pass"), recorded, device_after=False)
    assert (code, retried) == ("3", True)

    for device_after, retry_outcome, verdict in ((True, "success", 0), (False, "failure", 1)):
        root = tmp_path / f"retry-{retry_outcome}"
        result, repo, _ = _run_lane(
            root, ("pass",), recorded, device_after=device_after, attempt="2"
        )
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


def test_a_failing_test_is_never_retried(tmp_path, recorded):
    """The lane's exit 1, through the workflow's own steps: no retry, a red job."""
    code, retried = _first_attempt(tmp_path, ("pass", "fail"), recorded, device_after=True)
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


def test_the_summary_states_the_instrumented_outcomes_the_lane_observed(tmp_path, recorded):
    """Lane script -> the instrumented test's report -> the verdict step's summary.

    The scenario test writes its outcome where the lane script's environment
    says, as the live test does; the workflow's own verdict step must print it.
    """
    result, repo, _ = _run_lane(tmp_path, ("observe", "pass"), recorded, device_after=True)
    assert result.returncode == 0, result.stdout + result.stderr

    status, _, summary = _run_step(_step("verdict"), tmp_path, repo, FIRST_OUTCOME="success")

    lines = summary.splitlines()
    assert status == 0
    assert "### Instrumented tests (build_and_test --instrumented)" in lines
    assert "Observed live in attempt 1:" in lines, lines
    assert "- an instrumented outcome, observed live" in lines, lines


def test_after_a_retry_the_summary_reads_the_retrys_outcomes(tmp_path, recorded):
    """A lost device's retry decides the job, so its outcomes are the ones stated."""
    root = tmp_path / "retry"
    result, repo, _ = _run_lane(root, ("observe", "pass"), recorded, device_after=True, attempt="2")
    assert result.returncode == 0, result.stdout + result.stderr

    status, _, summary = _run_step(
        _step("verdict"),
        root,
        repo,
        FIRST_OUTCOME="failure",
        FIRST_EXIT="3",
        RETRY_OUTCOME="success",
    )
    assert status == 0
    assert "Observed live in attempt 2:" in summary.splitlines(), summary


PRE_BOOT_TASKS = (":app:assembleDebug", ":app:assembleDebugAndroidTest")


def _commands(script: str) -> list[list[str]]:
    """A `run:` script's commands: comments and blank lines dropped, each line split.

    A line continued with a trailing backslash is joined to the next first. A
    line shlex cannot split (an unbalanced quote inside a pipeline) is kept as
    one opaque word, which no check below mistakes for a gradlew command.
    """
    commands = []
    for line in script.replace("\\\n", " ").splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            words = [line]
        if words:
            commands.append(words)
    return commands


def _check_pre_boot_build(workflow: dict) -> None:
    """Raise AssertionError unless both APKs are built by the wrapper before the boot.

    Parsed, not grepped: an `echo` of the command, or the command commented
    out beside a `true`, carries every token and builds nothing.
    """
    steps = _lane_steps(workflow)
    boot = next(
        i for i, step in enumerate(steps) if "android-emulator-runner" in str(step.get("uses"))
    )
    builds = [
        (index, words)
        for index, step in enumerate(steps)
        for words in _commands(str(step.get("run") or ""))
        if words[0] == "./gradlew" or words[0].endswith("/gradlew")
    ]
    complete = [index for index, words in builds if all(task in words for task in PRE_BOOT_TASKS)]
    assert complete, f"no ./gradlew command builds {PRE_BOOT_TASKS}: {builds}"
    assert min(complete) < boot, "the APKs are built beside the booted emulator"
    options = shlex.split(str(workflow["jobs"]["emulator"].get("env", {}).get("GRADLE_OPTS", "")))
    assert "-Dorg.gradle.caching=true" in options, f"the build cache is not on: {options}"


def test_the_pre_boot_build_reaches_the_instrumented_tests_copy():
    """Both APKs are built before the emulator boots, into the job's build cache.

    The live instrumented test builds in a temporary copy of the scaffold, so
    the scaffold's own build outputs never reach it; the build cache does.
    """
    _check_pre_boot_build(_yaml("emulator.yml"))


def _pre_boot_step(workflow: dict) -> dict:
    (step,) = [s for s in _lane_steps(workflow) if "gradlew" in str(s.get("run", ""))]
    return step


def _echoed(workflow: dict) -> None:
    step = _pre_boot_step(workflow)
    step["run"] = "echo gradlew " + " ".join(PRE_BOOT_TASKS)


def _commented_out(workflow: dict) -> None:
    step = _pre_boot_step(workflow)
    step["run"] = "# " + step["run"].strip() + "\ntrue\n"


def _cache_misspelled(workflow: dict) -> None:
    workflow["jobs"]["emulator"]["env"]["GRADLE_OPTS"] = "-Dorg.gradle.caching=trueX"


@pytest.mark.parametrize(
    "mutation", [_echoed, _commented_out, _cache_misspelled], ids=lambda m: m.__name__
)
def test_the_pre_boot_guard_fails_a_build_that_does_not_run(mutation):
    workflow = _yaml("emulator.yml")
    mutation(workflow)

    with pytest.raises(AssertionError):
        _check_pre_boot_build(workflow)


def test_no_instrumented_outcome_is_said_not_left_out(tmp_path):
    status, _, summary = _run_step(_step("verdict"), tmp_path, tmp_path, FIRST_OUTCOME="success")

    assert status == 0
    assert "None observed in attempt 1: the instrumented test did not run" in summary


@pytest.mark.parametrize("doc", _shipped_docs(), ids=_doc_id)
def test_no_shipped_doc_names_docs_that_never_existed(doc):
    """STATUS.md and TESTING.md have never existed in this repo."""
    text = doc.read_text(encoding="utf-8")
    for missing in ("STATUS.md", "TESTING.md"):
        assert missing not in text, f"{_doc_id(doc)} names {missing}, which does not exist"


# ---------------------------------------------------------------------------
# What ships beside SKILL.md, and what stays in the repository.
#
# v0.10.0 took the release history and the known gaps out of SKILL.md. The
# gaps went to `references/known-gaps.md`, inside the package, because an
# installed agent has to be able to open them; the history went to the
# repository's CHANGELOG.md, because it does not. The four topic documents that
# used to be the repository's `references/` are `docs/` now, so "references"
# names one directory and it is the one that ships.
# ---------------------------------------------------------------------------

# Links in a packaged document: an allowlist, not a parser.
#
# Markdown has more ways to write a link than a guard can follow -- a title
# after the target, a target in angle brackets or on the next line, a reference
# defined elsewhere, an HTML anchor, escapes -- and a guard that tries to
# recognise them is one construct behind for ever (three review rounds each
# found more). So a packaged document may write a link ONE way, and what else
# could make one is refused by rules of the form "this appears nowhere":
#
# * the form is `[text](target)` (or `![text](target)`), the text holding no
#   `]` and the target following its bracket directly;
# * a target is an http(s) URL, an in-page `#anchor`, or a relative path:
#   optional leading `../` steps, then names made of letters, digits and
#   `_ . -` (not starting with a dot) joined by single `/`, then an optional
#   `#anchor`. So no spaces, titles, angle brackets, parentheses, backslashes,
#   drive letters, query strings, percent-escapes, absolute paths, `.` or
#   empty steps, trailing `/`, or `..` after a name;
# * every `](` in the file belongs to such a link;
# * `]:` appears nowhere (it is how a reference is defined, on however many
#   lines and inside whatever container);
# * `href=`, `src=` and `srcset=` appear nowhere, in any case, with or without
#   whitespace or a line break before the `=`.
#
# The whole file is read, fenced blocks and inline code included: nothing has
# to be decided about where prose ends, and the price is that a packaged
# document cannot illustrate link syntax or show a JSON key that ends `]:`.
# None does. Not covered, and said here: HTML that names a file by some other
# attribute. An agent reads these files as text, where that is not a link.
_URL = r"https?://[^()\s<>\"'\\]+"
_ANCHOR = r"#[A-Za-z0-9_-]+"
_NAME = r"[A-Za-z0-9_-][A-Za-z0-9_.-]*"
_FILE = rf"(?:\.\./)*{_NAME}(?:/{_NAME})*(?:{_ANCHOR})?"
_PLAIN_LINK = re.compile(rf"!?\[[^\]]*\]\(({_URL}|{_ANCHOR}|{_FILE})\)")
_HTML_LINK = re.compile(r"\b(?:href|src|srcset)\s*=", re.IGNORECASE)


def _unreadable_links(markdown: str) -> list[str]:
    """Everything in a document that could be a link and is not in the one form.

    Each finding says which rule it broke and what to do, because two of the
    rules refuse text that is not a link at all (`items[0]:` in an example).
    """

    def around(at: int) -> str:
        return repr(markdown[max(0, at - 30) : at + 40])

    read = [match.span() for match in _PLAIN_LINK.finditer(markdown)]
    unreadable = [
        f"`](` that is not `[text](relative/path)` or `[text](https://...)`: {around(at.start())}"
        for at in re.finditer(r"\]\(", markdown)
        if not any(start <= at.start() < end for start, end in read)
    ]
    unreadable += [
        "`]:`, which is how Markdown defines a link by reference; not allowed anywhere, "
        f"code examples included, so reword the example: {around(at.start())}"
        for at in re.finditer(r"\]:", markdown)
    ]
    unreadable += [
        f"an HTML `href`, `src` or `srcset`; not allowed anywhere: {around(at.start())}"
        for at in _HTML_LINK.finditer(markdown)
    ]
    return unreadable


def _relative_links(markdown: str) -> list[str]:
    """Targets of a document's links that name a file, anywhere in it.

    A URL or an in-page anchor names no file and is left out. Complete only for
    a document `_unreadable_links` finds nothing in.
    """
    return [
        target
        for target in _PLAIN_LINK.findall(markdown)
        if not target.startswith(("http://", "https://", "#"))
    ]


def _symlinks_under(root: Path) -> list[Path]:
    found = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        found += [
            Path(directory, name)
            for name in (*dirnames, *filenames)
            if Path(directory, name).is_symlink()
        ]
    return sorted(found)


def _installed_root(doc: Path) -> Path:
    """The directory that arrives together with ``doc`` however it is installed.

    A plugin install places the plugin directory; a clone install links the
    skill directory alone (SKILL.md, "As Claude Code Skill"). So a document
    inside the skill can count on the skill directory and nothing above it.
    """
    return SKILL_ROOT if SKILL_ROOT in doc.parents else PACKAGE_ROOT


def _links_that_do_not_ship(doc: Path, markdown: str) -> list[str]:
    """Relative links in ``doc`` that an installed copy could not follow.

    Followed one step at a time from the document's own directory, as an
    installed copy on any filesystem would have to: `..` may not climb above
    the installed root (a path that leaves and comes back does so by the
    checkout's directory names, which an install does not keep); every name
    must be an entry of the directory it is looked up in, spelled exactly as
    the entry is (this checkout may sit on a filesystem that ignores case; an
    install need not); and the last name must be a file. The package holds no
    symlinks (`test_the_package_holds_no_symlinks`), so following the text of a
    path is following the path.
    """
    root = _installed_root(doc)
    broken = []
    for target in _relative_links(markdown):
        at = doc.parent
        for step in target.partition("#")[0].split("/"):
            if step == ".." and at != root:
                at = at.parent
            elif step != ".." and at.is_dir() and step in {entry.name for entry in at.iterdir()}:
                at = at / step
            else:
                broken.append(target)
                break
        else:
            if not at.is_file():
                broken.append(target)
    return broken


def test_package_contains_what_the_docs_promise():
    """`examples/` and `references/` are promised, and both ship."""
    assert (SKILL_ROOT / "examples").is_dir(), "examples/ is documented but missing"
    assert REFERENCES.is_dir(), "references/ is documented but missing"


def test_references_holds_one_file_per_script_and_the_known_gaps_file():
    """What `references/` holds, exactly: nothing else is put there unnoticed.

    One Markdown file per runnable script, named after it, and `known-gaps.md`.
    Which index entry links which file, and what each file's title says, is
    `test_skill_index`'s to check; this is the directory's inventory.
    """
    expected = sorted([*(Path(name).stem + ".md" for name in _scripts_on_disk()), "known-gaps.md"])
    assert sorted(path.name for path in REFERENCES.iterdir()) == expected


def test_skill_md_links_the_known_gaps_file():
    """Moved out is not dropped: the main file says where the gaps went, as a link."""
    links = _relative_links(SKILL_MD.read_text(encoding="utf-8"))
    assert "references/known-gaps.md" in links, links


def test_the_package_holds_no_symlinks():
    """What the guards walk is what ships, file for file.

    The release's `zip -r` follows a symlink and packs what it points at; a
    directory walk does not descend into one, and a path through one does not
    lead where its text says. With none in the package, the inventory below is
    complete and a link can be followed by reading it.
    """
    assert _symlinks_under(PACKAGE_ROOT) == []


def test_a_symlink_in_a_package_is_found(tmp_path):
    (tmp_path / "skill" / "examples").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "notes.md").write_text("[x](missing.md)\n", encoding="utf-8")
    (tmp_path / "skill" / "examples" / "more").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "skill" / "notes.md").symlink_to(tmp_path / "elsewhere" / "notes.md")

    assert [path.name for path in _symlinks_under(tmp_path / "skill")] == ["more", "notes.md"]


def test_the_link_guard_reads_every_markdown_file_the_package_holds():
    """SKILL.md, each reference, and the README beside them: none is left out."""
    packaged = _packaged_markdown()
    assert set(_shipped_docs()) < set(packaged)
    assert PACKAGED_README in packaged
    references = [doc.name for doc in packaged if doc.parent == REFERENCES]
    assert "known-gaps.md" in references
    assert len(references) == len(_scripts_on_disk()) + 1


@pytest.mark.parametrize("doc", _packaged_markdown(), ids=_doc_id)
def test_every_link_in_a_packaged_doc_is_written_the_one_way(doc):
    unreadable = _unreadable_links(doc.read_text(encoding="utf-8"))
    assert not unreadable, (
        f"{_doc_id(doc)} breaks the link rules for packaged documents "
        f"(see the comment above `_URL`): {unreadable}"
    )


@pytest.mark.parametrize("doc", _packaged_markdown(), ids=_doc_id)
def test_every_relative_link_in_a_packaged_doc_lands_inside_the_package(doc):
    broken = _links_that_do_not_ship(doc, doc.read_text(encoding="utf-8"))
    assert not broken, f"{_doc_id(doc)} links files an installed agent does not have: {broken}"


@pytest.mark.parametrize(
    "doc",
    [README, REPO_ROOT / "DEV.md", REPO_ROOT / "CHANGELOG.md", *_packaged_markdown()],
    ids=lambda doc: doc.relative_to(REPO_ROOT).as_posix(),
)
def test_every_fenced_block_in_a_document_is_one_the_reader_reads(doc):
    """Otherwise every guard that reads this document's prose or code refuses it."""
    assert fence_problems(doc.read_text(encoding="utf-8")) == []


def _link_to_a_file_that_is_not_there(skill_md: str) -> str:
    return skill_md.replace("(references/known-gaps.md)", "(references/known-gap.md)")


def _link_to_the_repository_changelog(skill_md: str) -> str:
    return skill_md.replace("(references/known-gaps.md)", "(../../../CHANGELOG.md)")


def _link_to_the_repository_docs(skill_md: str) -> str:
    return skill_md.replace("(references/known-gaps.md)", "(../../../docs/adb_quick.md#see-also)")


def _link_out_of_the_skill_and_back_by_the_checkout_names(skill_md: str) -> str:
    back = "../../../android-emulator-skill/skills/android-emulator-skill/references/known-gaps.md"
    return skill_md.replace("(references/known-gaps.md)", f"({back})")


def _link_to_a_directory(skill_md: str) -> str:
    return skill_md.replace("(references/known-gaps.md)", "(references)")


def _link_through_a_file_as_if_it_were_a_directory(skill_md: str) -> str:
    return skill_md.replace("(references/known-gaps.md)", "(README.md/known-gaps.md)")


def _link_spelled_in_another_case(skill_md: str) -> str:
    """Followed happily by the filesystem this was written on, and by no other."""
    return skill_md.replace("(references/known-gaps.md)", "(references/Known-Gaps.md)")


def _link_through_a_directory_spelled_in_another_case(skill_md: str) -> str:
    return skill_md.replace("(references/known-gaps.md)", "(References/known-gaps.md)")


def _link_in_inline_code_to_a_file_that_is_not_there(skill_md: str) -> str:
    return skill_md.replace(
        "[references/known-gaps.md](references/known-gaps.md)",
        "`[references/known-gaps.md](references/missing.md)`",
    )


def _link_in_a_fenced_block_to_a_file_that_is_not_there(skill_md: str) -> str:
    return skill_md + "\n```markdown\n[the gaps](references/missing.md)\n```\n"


@pytest.mark.parametrize(
    "mutation",
    [
        _link_to_a_file_that_is_not_there,
        _link_to_the_repository_changelog,
        _link_to_the_repository_docs,
        _link_out_of_the_skill_and_back_by_the_checkout_names,
        _link_to_a_directory,
        _link_through_a_file_as_if_it_were_a_directory,
        _link_spelled_in_another_case,
        _link_through_a_directory_spelled_in_another_case,
        _link_in_inline_code_to_a_file_that_is_not_there,
        _link_in_a_fenced_block_to_a_file_that_is_not_there,
    ],
    ids=lambda m: m.__name__,
)
def test_the_link_guard_fails_a_link_an_installed_agent_cannot_follow(mutation):
    """Each is written the one way, so it is read; none leads to a packaged file."""
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    mutated = mutation(skill_md)
    assert mutated != skill_md
    assert not _unreadable_links(mutated), "the mutation must be a link the guard reads"

    assert _links_that_do_not_ship(SKILL_MD, mutated)


def test_a_link_may_climb_out_of_its_own_directory_while_it_stays_inside_the_skill():
    reference = REFERENCES / "known-gaps.md"

    assert not _links_that_do_not_ship(reference, "See [the reference](../SKILL.md#failures).\n")
    assert not _links_that_do_not_ship(reference, "See [itself](known-gaps.md).\n")
    assert not _links_that_do_not_ship(reference, "See [itself](../references/known-gaps.md).\n")
    assert not _links_that_do_not_ship(
        SKILL_MD, "See [an example](examples/login_flow_example.sh).\n"
    )
    assert _links_that_do_not_ship(reference, "See [the repository](../../README.md).\n")
    assert _links_that_do_not_ship(SKILL_MD, "See [the plugin](../README.md).\n")


def test_the_packaged_readme_is_read_like_any_other_packaged_document():
    """It sits in the skill directory, so it is held to the skill directory."""
    readme = PACKAGED_README.read_text(encoding="utf-8")

    assert _links_that_do_not_ship(PACKAGED_README, readme + "\nSee [this](missing.md).\n")
    assert _links_that_do_not_ship(PACKAGED_README, readme + "\nSee [this](../../../README.md).\n")
    assert not _links_that_do_not_ship(PACKAGED_README, readme + "\nSee [this](SKILL.md).\n")


_THE_LINK = "[references/known-gaps.md](references/known-gaps.md)"

# Ways to write a link to a file that is not there which are NOT the one form.
# Each value replaces the real link; `\n` + text is appended where a second
# line is needed. Collected from two review rounds: every one of these was, at
# some point, a broken link the guard passed without reading.
OTHER_LINK_FORMS = {
    "titled": '[gaps](references/missing.md "the gaps")',
    "in angle brackets": "[gaps](<references/missing.md>)",
    "target on the next line": "[gaps](\n  references/missing.md)",
    "space in the target": "[gaps](references/missing file.md)",
    "escaped parenthesis": "[gaps](references/missing\\).md)",
    "label with an escaped bracket": "[the \\] gaps](references/missing.md)",
    "query string": "[gaps](references/missing.md?raw=1)",
    "percent-escape": "[gaps](references/%6dissing.md)",
    "drive letter, backslashes": "[gaps](c:\\outside\\missing.md)",
    "drive letter, slashes": "[gaps](c:/outside/missing.md)",
    "upper-case drive letter": "[gaps](C:/outside/missing.md)",
    "network path, backslashes": "[gaps](\\\\host\\share\\missing.md)",
    "another scheme": "[gaps](file:///outside/missing.md)",
    "absolute path": "[gaps](/references/missing.md)",
    "network path, slashes": "[gaps](//host/share/missing.md)",
    "trailing slash": "[gaps](references/)",
    "file with a trailing slash": "[gaps](SKILL.md/)",
    "file with a trailing dot step": "[gaps](SKILL.md/.)",
    "leading dot step": "[gaps](./references/missing.md)",
    "empty step": "[gaps](references//missing.md)",
    "climbing after a name": "[gaps](examples/../references/missing.md)",
    "climbing through a file": "[gaps](README.md/../references/missing.md)",
    "hidden file": "[gaps](.missing.md)",
    "by reference": "[gaps][g]\n\n[g]: references/missing.md",
    "by reference, target on the next line": "[gaps][g]\n\n[g]:\n  references/missing.md",
    "by reference, in a blockquote": "> [gaps][g]\n>\n> [g]: references/missing.md",
    "by reference, in a list item": "- [gaps][g]\n\n  [g]: references/missing.md",
    "by reference, label with an escaped bracket": "[gaps][a\\]b]\n\n[a\\]b]: references/missing.md",
    "by reference, label on two lines": "[gaps][a b]\n\n[a\nb]: references/missing.md",
    "by reference, after a tab": "[gaps][g]\n\n\t[g]: references/missing.md",
    "by reference, in a fenced block": "```markdown\n[g]: references/missing.md\n```",
    "html anchor": '<a href="references/missing.md">gaps</a>',
    "html anchor, upper case": '<A HREF="references/missing.md">gaps</A>',
    "html anchor, spaced": "<a href = 'references/missing.md'>gaps</a>",
    "html image": '<img SRC="references/missing.png">',
    "html anchor, attribute on two lines": '<a href\n="references/missing.md">gaps</a>',
    "html image, attribute on two lines": '<img src\n  = "references/missing.png">',
    "html image, srcset": '<img srcset="references/missing.png 1x">',
}


@pytest.mark.parametrize("form", OTHER_LINK_FORMS.values(), ids=OTHER_LINK_FORMS.keys())
def test_a_link_written_any_other_way_is_refused_rather_than_skipped(form):
    """Skipping what it cannot read is how a link guard passes a broken link."""
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    assert skill_md.count(_THE_LINK) == 1
    rewritten = skill_md.replace(_THE_LINK, form)

    assert _unreadable_links(rewritten)


def test_the_one_link_form_is_read_wherever_it_is_written():
    """Prose, a wrapped label, a list item, a table cell, a blockquote, a fenced block."""
    markdown = (
        "See [the site](https://example.com/a_(b).md), [below](#failures), [a](one.md) and\n"
        "[b](two/three.md#part); ![a picture](four.png).\n"
        "- Then [the\n  gaps](five.md).\n"
        "| cell | [six](six.md) |\n"
        "> quoted: [seven](seven.md)\n"
        "```\n[eight](eight.md)\n```\n"
        "Inline `[nine](nine.md)`.\n"
    )
    wanted = [f"{name}.md" for name in ("one", "five", "six", "seven", "eight", "nine")]
    wanted[1:1] = ["two/three.md#part", "four.png"]

    # The URL holds parentheses, which the one form does not allow.
    assert len(_unreadable_links(markdown)) == 1
    markdown = markdown.replace("a_(b).md", "a_b.md")
    assert _unreadable_links(markdown) == []
    assert _relative_links(markdown) == wanted


# The reader of fenced blocks itself (`test_quick_start_contract.markdown_lines`).


def test_a_heading_inside_any_fenced_block_is_not_a_heading():
    """Tildes fence a block too, and a longer fence holds a shorter one."""
    for opening, inner, closing in (
        ("```", "~~~", "```"),
        ("~~~", "```", "~~~"),
        ("````", "```", "````"),
        ("~~~~", "~~~", "~~~~~"),
    ):
        markdown = (
            f"## One\n\nfirst\n\n{opening}text\n## Not a heading\n{inner}\n{closing}\n"
            "\nlast\n\n## Two\n\nsecond\n"
        )
        assert code_lines(markdown) == ["## Not a heading", inner], opening
        assert prose_lines(section(markdown, "One")) == ["", "first", "", "", "last"], opening
        assert section(markdown, "Two").strip() == "second", opening
        assert fence_problems(markdown) == [], opening


def test_the_reader_agrees_with_itself_about_every_document_it_guards():
    """Prose, fence and code lines are the document, in order, nothing dropped."""
    for doc in (README, REPO_ROOT / "DEV.md", CHANGELOG, *_packaged_markdown()):
        text = doc.read_text(encoding="utf-8")
        assert [line for line, _ in markdown_lines(text)] == text.splitlines(), doc.name


def test_lines_end_at_a_newline_or_a_crlf_pair_and_nowhere_else():
    assert [line for line, _ in markdown_lines("a\r\nb\n\nc")] == ["a", "b", "", "c"]
    assert [line for line, _ in markdown_lines("a\n")] == ["a"]
    assert markdown_lines("") == []


def test_a_block_fenced_inside_a_list_item_is_read():
    markdown = "1. Step:\n\n   ```bash\n   echo hi\n\n     indented more\n   ```\n\n2. Next\n"

    assert code_lines(markdown) == ["   echo hi", "", "     indented more"]
    assert prose_lines(markdown) == ["1. Step:", "", "", "2. Next"]


def test_three_backticks_around_a_word_do_not_open_a_block():
    markdown = "```not a fence``` is inline code\n> ```nor this``` in a quote\n## Real\n\nbody\n"

    assert [kind for _, kind in markdown_lines(markdown)] == ["prose"] * 5
    assert section(markdown, "Real").strip() == "body"


def test_a_block_made_by_indentation_is_left_out_of_flush_prose():
    """`prose_lines` cannot tell it from a list's continuation; `flush_prose_lines` need not."""
    markdown = "Said:\n\n    not said, shown\n\n- item\n  continued\n    - nested item\n"

    assert "    not said, shown" in prose_lines(markdown)
    assert flush_prose_lines(markdown) == ["Said:", "", "", "- item", "  continued"]


# Documents the fence reader refuses, each for a reason its docstring gives.
UNREAD_FENCES = {
    "never closed": "## One\n\n```bash\necho hi\n\n## Two\n\nprose\n",
    "inside a blockquote": "> ```text\n> quoted\n> ```\n",
    "inside a nested blockquote": "> > ~~~\n> > quoted\n> > ~~~\n",
    "closed at another indentation": "- item\n\n  ```\n  code\n```\n",
    "left by a line indented less": "- item\n\n  ```\n  code\nprose again\n\n```\nmore\n```\n",
    "opened on a list marker's line": "1. ```bash\n   echo hi\n   ```\n",
    "opened on a list marker's line, never closed": "1. ```bash\n   echo hi\n",
    "opened on a nested list marker's line": "- 2) ~~~\n     echo hi\n",
    "closing fence followed by a line separator": "```text\ncode\n```\u2028prose\n",
    "closing fence followed by a paragraph separator": "```text\ncode\n```\u2029prose\n",
    "closing fence followed by a form feed": "```text\ncode\n```\x0cprose\n",
    "closing fence followed by a vertical tab": "```text\ncode\n```\x0bprose\n",
    "closing fence followed by a next-line character": "```text\ncode\n```\x85prose\n",
    "closing fence followed by a bare carriage return": "```text\ncode\n```\rprose\n",
    "closing fence followed by a file separator": "```text\ncode\n```\x1cprose\n",
    "closing fence followed by a no-break space": "```text\ncode\n``` \nprose\n",
    "closing fence followed by a tab": "```text\ncode\n```\t\nprose\n",
    "fence indented by a tab": "\t```text\n\tcode\n\t```\n",
    "a tab in prose": "one\ttwo\n",
    "an html pre block": "<pre>\nshown as code\n</pre>\n",
    "an html pre block, upper case": "text <PRE class='x'>\nshown as code\n</PRE>\n",
    "an html comment": "<!--\nnot shown\n-->\n",
    "an html script block": "<script>\nnot shown\n</script>\n",
}


@pytest.mark.parametrize("markdown", UNREAD_FENCES.values(), ids=UNREAD_FENCES.keys())
def test_a_fenced_block_the_reader_cannot_place_is_refused_not_guessed_at(markdown):
    assert fence_problems(markdown)
    for read in (markdown_lines, prose_lines, code_lines):
        with pytest.raises(AssertionError):
            read(markdown)
    with pytest.raises(AssertionError):
        section(markdown, "One")


def test_a_section_is_found_once_or_not_at_all():
    markdown = "## One\n\na\n\n### Under one\n\nb\n\n## Two\r\n\r\nc\r\n\n## One more\n"

    assert section(markdown, "One").split() == ["a", "###", "Under", "one", "b"]
    assert section(markdown, "Two").strip() == "c"
    for title in ("Three", "One mor", "Under one"):
        with pytest.raises(AssertionError):
            section(markdown, title)
    with pytest.raises(AssertionError):
        section(markdown + "\n## One\n", "One")


def _documentation_names(skill_md: str) -> tuple[list[str], list[str]]:
    """The bold names in SKILL.md's Documentation section: shipped, repository-only.

    The section is two bullet lists with one sentence between them, which says
    the second list is in the source repository only.
    """
    shipped: list[str] = []
    repository: list[str] = []
    into = shipped
    for line in _prose_lines(section(skill_md, "Documentation")):
        if line.startswith("In the source repository only"):
            into = repository
        elif match := re.match(r"- \*\*([^*]+)\*\*", line):
            into.append(match.group(1))
    return shipped, repository


def test_the_documentation_section_says_where_each_document_is():
    """Each name it lists as shipped is in the package; each repository-only one is not.

    The section said `references/` was repository-only while a guard asserted
    the directory was absent from the package. Both moved in v0.10.0; this
    reads the section instead of restating it.
    """
    shipped, repository = _documentation_names(SKILL_MD.read_text(encoding="utf-8"))

    assert shipped == ["SKILL.md", "README.md", "examples/", "references/"]
    assert repository == ["CLAUDE.md", "CHANGELOG.md", "docs/"]
    for name in shipped:
        assert (SKILL_ROOT / name).exists(), f"{name} is listed as shipped and is not there"
    for name in repository:
        assert (REPO_ROOT / name).exists(), f"{name} is listed and is not in the repository"
        assert not (SKILL_ROOT / name).exists(), f"{name} is listed as repository-only and ships"
        assert not (PACKAGE_ROOT / name).exists(), f"{name} is listed as repository-only and ships"


def test_a_documentation_list_inside_a_fenced_block_is_not_the_claim():
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    start = skill_md.index("## Documentation\n") + len("## Documentation\n")
    end = skill_md.index("## Platform limitations worth knowing")
    fenced = skill_md[:start] + "\n```text" + skill_md[start:end] + "```\n\n" + skill_md[end:]

    assert _documentation_names(fenced) == ([], [])


def test_references_means_one_directory():
    """The repository's four topic documents are `docs/`; `references/` is the skill's."""
    assert not (REPO_ROOT / "references").exists()
    assert sorted(path.name for path in (REPO_ROOT / "docs").glob("*.md")) == [
        "accessibility_checklist.md",
        "adb_quick.md",
        "test_patterns.md",
        "troubleshooting.md",
    ]


CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The two entries as SKILL.md held them at v0.9.0 (f9ab727), each without its
# surrounding blank lines and with one final newline: byte count and SHA-256.
# A released entry is not edited afterwards; correcting one means changing its
# line here, on purpose.
MOVED_RELEASE_NOTES = {
    "v0.9.0": (4687, "5e7ec0714850bf7ec4bb6e5e4bfc20dd068f72569a7c8f4300cfefaffb78b6ed"),
    "v0.8.0": (6843, "e2894fb8c7e20f839cc223e0ad753253ee4ff6ccbac7a2e3e31268d5fbe721a4"),
}
V0_9_0_COMMIT = "f9ab727"


def _release_body(changelog: str, release: str) -> bytes:
    return (section(changelog, release).strip("\n") + "\n").encode("utf-8")


def test_the_changelog_holds_the_releases_that_left_skill_md():
    """History moved; it was not deleted, shortened or retyped on the way."""
    changelog = CHANGELOG.read_text(encoding="utf-8")
    headings = [line for line in prose_lines(changelog) if line.startswith("## ")]
    assert headings == [f"## {release}" for release in MOVED_RELEASE_NOTES]

    for release, (size, digest) in MOVED_RELEASE_NOTES.items():
        body = _release_body(changelog, release)
        assert len(body) == size, release
        assert hashlib.sha256(body).hexdigest() == digest, release


def test_the_moved_release_notes_are_the_ones_skill_md_held_at_v0_9_0():
    """The digests above, checked against the file they were taken from.

    Where the repository's history holds the v0.9.0 commit. A shallow CI clone
    does not, and there the pinned digests stand alone.
    """
    blob = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{V0_9_0_COMMIT}:{SKILL_MD.relative_to(REPO_ROOT)}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if blob.returncode != 0:
        pytest.skip(f"{V0_9_0_COMMIT} is not in this clone's history")
    released = blob.stdout.decode("utf-8")

    changelog = CHANGELOG.read_text(encoding="utf-8")
    for release in MOVED_RELEASE_NOTES:
        assert _release_body(changelog, release).decode("utf-8") in released, release


_LIST_MARKER = re.compile(r"(?:[-*+]|\d+[.)])\s+")


def _release_note_units(changelog: str) -> list[str]:
    """Each paragraph and each list item of the moved entries: its words, folded.

    A unit is a whole thought as the release notes put it. One of its lines is
    not: current documentation may say again, in the same words, something a
    release note said, and a guard on single lines failed exactly that. The
    item's own marker is left off, so the same item under `*` or `1.`, or as
    a paragraph, is the same unit.
    """
    units = []
    for release in MOVED_RELEASE_NOTES:
        for block in re.split(r"\n\s*\n", section(changelog, release)):
            for item in re.split(r"\n(?=- )", block):
                folded = _LIST_MARKER.sub("", " ".join(item.split()), count=1)
                if len(folded) >= 80:
                    units.append(folded)
    return units


def _release_notes_still_in(skill_md: str) -> list[str]:
    """The moved entries' paragraphs and list items that a SKILL.md holds whole.

    Compared with whitespace folded, so re-wrapping, re-indenting or
    re-bulleting a copied item does not hide it.
    """
    folded = " ".join(skill_md.split())
    units = _release_note_units(CHANGELOG.read_text(encoding="utf-8"))
    return [unit[:70] for unit in units if unit in folded]


def test_skill_md_keeps_none_of_the_release_notes_it_handed_over():
    """Moved, not copied: the file an agent loads does not carry the history as well."""
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    assert len(_release_note_units(CHANGELOG.read_text(encoding="utf-8"))) > 20
    assert _release_notes_still_in(skill_md) == []

    sections = [line for line in prose_lines(skill_md) if line.startswith("## ")]
    assert "## Failures" in sections
    assert "## Known gaps and release history" in sections
    assert "## Status & Roadmap" not in sections
    assert not [line for line in sections if re.match(r"## v\d", line)]


def test_a_release_note_copied_back_into_skill_md_is_found_however_it_is_wrapped():
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    entry = section(CHANGELOG.read_text(encoding="utf-8"), "v0.9.0")
    item = next(item for item in re.split(r"\n(?=- )", entry) if item.startswith("- **A per-item"))
    rewrapped = "\n".join("    " + line.strip() for line in item.splitlines())

    assert _release_notes_still_in(skill_md + "\n## Status\n\n" + item + "\n")
    assert _release_notes_still_in(skill_md + "\n## Status\n\n" + rewrapped + "\n")
    for marker in ("* ", "1. ", "7) ", ""):
        again = marker + item.removeprefix("- ")
        assert _release_notes_still_in(skill_md + "\n## Status\n\n" + again + "\n"), marker


def test_one_line_a_release_note_also_used_is_not_a_release_note():
    """Current documentation restating a contract in the release note's words."""
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    entry = section(CHANGELOG.read_text(encoding="utf-8"), "v0.8.0")
    line = next(line for line in entry.splitlines() if "answers with a summary only once" in line)
    summary = f"- ANR session contract:\n{line}\n  (otherwise the command refuses).\n"

    assert _release_notes_still_in(skill_md + "\n" + summary) == []


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


def _prose_lines(markdown: str) -> list[str]:
    """Lines OUTSIDE fenced code blocks -- what the document claims.

    The complement of :func:`_code_block_lines`. A claim about installation is
    prose, so a guard on it cannot read code blocks only; what it must not do
    is read the whole file, or a documented `# will be finalized` comment
    inside an example would satisfy it.
    """
    return prose_lines(markdown)


# A metavar a reader is meant to substitute is legitimate in a command; a
# `<repository-url>` nobody can resolve is not. Listed explicitly so the
# exemption cannot quietly grow to cover a real hole.
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9_-]*>")
SUBSTITUTABLE = frozenset({"<name>", "<serial>", "<id>", "<package>"})


@pytest.mark.parametrize(
    "doc",
    [README, PACKAGED_README, *_shipped_docs()],
    ids=["repo", "packaged", *(_doc_id(doc) for doc in _shipped_docs())],
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


def _site_version() -> str:
    """The version in the docs site's hero tag (`vX.Y.Z · Plugin`).

    Hand-written like the `## Scripts` heading, and it had been left at v0.4.0
    through four releases.
    """
    match = re.search(r"\bv(\d+\.\d+\.\d+) · Plugin\b", " ".join(_site().text))
    assert match, "site/index.html has no `vX.Y.Z · Plugin` tag"
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
        ("site/index.html hero tag", _site_version),
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


def _lint_roots(tool: str) -> list[str]:
    """The paths `lint.yml` hands `tool`, read from the workflow's `run:` steps."""
    import yaml

    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "lint.yml").read_text(encoding="utf-8")
    )
    return _lint_roots_in(workflow, tool)


def _lint_roots_in(workflow: dict, tool: str) -> list[str]:
    """The paths a parsed workflow hands `tool` on the one `run:` line that starts with it.

    The line is split as a shell would (`comments=True`), so an inline
    `# comment` is not mistaken for two more roots.
    """
    commands = [
        shlex.split(line, comments=True)
        for job in workflow["jobs"].values()
        for step in job["steps"]
        for line in str(step.get("run", "")).splitlines()
        if line.strip().startswith(f"{tool} ")
    ]
    assert len(commands) == 1, f"lint.yml should run {tool} exactly once: {commands}"
    # `black --check --diff A B` / `ruff check A B`: the operands that are paths.
    return [arg for arg in commands[0][1:] if not arg.startswith("-") and arg != "check"]


def test_pre_commit_lints_the_same_roots_as_ci():
    """T12: Black and Ruff in pre-commit covered `scripts/` only; CI lints `tests/` too.

    So a test file that CI would fail passed every local hook, and the first
    anyone heard of it was a red required check. Both hooks' `files` regexes
    are compiled and asked about real paths, and the roots they must cover are
    read from `lint.yml`'s own command lines rather than typed here.
    """
    import yaml

    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo.get("hooks", [])}

    black_roots, ruff_roots = _lint_roots("black"), _lint_roots("ruff")
    assert black_roots == ruff_roots, f"lint.yml: black {black_roots} vs ruff {ruff_roots}"
    assert len(black_roots) == 2, f"lint.yml lints {black_roots}"

    for hook_id in ("black", "ruff"):
        assert hook_id in hooks, f"pre-commit has no {hook_id} hook"
        files = re.compile(hooks[hook_id].get("files", ""))
        for root in black_roots:
            for path in (f"{root.rstrip('/')}/x.py", f"{root.rstrip('/')}/deeper/x.py"):
                assert files.search(path), (
                    f"pre-commit's {hook_id} hook skips {path}, which lint.yml lints: "
                    f"files: {files.pattern}"
                )
        for path in ("tests/test_x.py", f"{SKILL_ROOT.relative_to(REPO_ROOT)}/scripts/x.py"):
            assert files.search(path), f"{hook_id}: {files.pattern} does not match {path}"
        for path in ("tests/fixtures/recorded/x.txt", "x.py", "site/x.py"):
            assert not files.search(path), f"{hook_id}: {files.pattern} matches {path}"


@pytest.mark.parametrize(
    ("run", "roots"),
    [
        ("black --check --diff a/ tests/", ["a/", "tests/"]),
        ("black --check --diff a/ tests/  # coverage", ["a/", "tests/"]),
        ("echo black --check a/ tests/", None),
        ("# black --check a/ tests/", None),
    ],
)
def test_lint_roots_reads_the_command_not_its_comment_or_an_echo(run, roots):
    """The root extractor's own floor: a comment adds no root, an echo or a comment is no run."""
    workflow = {"jobs": {"lint": {"steps": [{"run": f"set -e\n{run}\n"}]}}}
    if roots is None:
        with pytest.raises(AssertionError, match="exactly once"):
            _lint_roots_in(workflow, "black")
    else:
        assert _lint_roots_in(workflow, "black") == roots


def test_rewriting_hooks_leave_byte_exact_fixtures_alone():
    """`pre-commit run --all-files` must be runnable over the repo without rewriting fixtures.

    Three hooks REWRITE files. Over the committed tree they "fixed" the legacy
    ANR session fixtures (a previous version's store, compared byte for byte)
    and the scaffold's `gradlew.bat` (CRLF is that file's format), so the run
    failed on main for files nobody had touched. Each hook's `exclude` is
    compiled and asked about those paths, and must still leave code alone.
    """
    import yaml

    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo.get("hooks", [])}

    for hook_id in ("trailing-whitespace", "end-of-file-fixer", "mixed-line-ending"):
        assert hook_id in hooks, f"pre-commit has no {hook_id} hook"
        exclude = re.compile(hooks[hook_id].get("exclude", "(?!)"))
        for path in (
            "tests/fixtures/anr_legacy_sessions/x/meta.json",
            "tests/fixtures/scaffold/compose/gradlew.bat",
            "tests/fixtures/recorded/x.txt",
            # Room ends an exported schema without a newline.
            "tests/fixtures/recorded/gradle-8.13/room_schema_v1.json",
        ):
            assert exclude.search(
                path
            ), f"pre-commit's {hook_id} hook rewrites {path}: exclude: {exclude.pattern}"
        for path in (
            "tests/test_x.py",
            f"{SKILL_ROOT.relative_to(REPO_ROOT)}/scripts/x.py",
            # Written by the recorders, not recorded: it stays under every hook.
            "tests/fixtures/recorded/gradle-8.13/MANIFEST.json",
        ):
            assert not exclude.search(path), f"{hook_id}: {exclude.pattern} excludes {path}"


def _option_tokens(help_text: str) -> set[str]:
    """The flags argparse actually registered, from its own --help output.

    Every spelling of every option, flattened; see :func:`_option_entries`.
    """
    return {flag for entry in _option_entries(help_text) for flag in entry}


def _option_entries(help_text: str) -> list[frozenset[str]]:
    """The options argparse registered, one set of spellings per option.

    ``-s SERIAL, --serial SERIAL`` is one entry, ``{"-s", "--serial"}``: the
    spellings an option's help line names are aliases of one another, so a doc
    naming any of them documents the option.

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
        return []

    option_column = min(indent for indent, _ in candidates)
    entries: list[frozenset[str]] = []
    for indent, stripped in candidates:
        if indent != option_column:
            continue
        # "  --json, -j METAVAR   description..." -> the part before 2+ spaces.
        column = re.split(r"\s{2,}", stripped)[0]
        spellings = set()
        for piece in column.split(","):
            flag = piece.strip().split("=")[0].split(" ")[0]
            if flag.startswith("-"):
                spellings.add(flag)
        if spellings:
            entries.append(frozenset(spellings))
    return entries


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


# Any script's entry, the shell health check's too: with `.py` alone its Options
# bullet was read as one more bullet of the Python entry above it, and that
# script was credited with documenting the health check's flags.
_SCRIPT_ENTRY = re.compile(r"^\d+\.\s+\*\*([A-Za-z_]\w*\.(?:py|sh))\*\*")
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
    Status section even described it (SKILL.md had one until v0.10.0), so nobody
    reading the file top to bottom saw a contradiction. Only argparse knows.

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

    stray = sorted(set(documented) - set(_scripts_on_disk()))
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


# Flags a script accepts that its SKILL.md `- Options:` bullet deliberately
# leaves out, script -> flags (any one spelling of an option names it). Debt or
# a reason, never a place to hide an interface: NEVER_UNDOCUMENTED below may not
# appear here, and an entry naming a script or flag that no longer exists fails,
# so the set can only be paid down, not left stale.
UNDOCUMENTED_OK: dict[str, frozenset[str]] = {
    # Audit labels carried over from the iOS skill: echoed into the operation's
    # output as a test trail, they change nothing on the device or in the verdict.
    "privacy_manager.py": frozenset({"--scenario", "--step"}),
}

# The ceiling: a script's main verbs. An agent that cannot find these in
# SKILL.md cannot use the script at all, so they are documented, never excused.
NEVER_UNDOCUMENTED: frozenset[str] = frozenset(
    {
        # Named by the release plan (T3).
        "--tap-at",
        "--drag",
        "--find-exact",
        "--index",
        "--restart",
        "--args",
        "--all",
        "--old",
        "--yes",
        # The verbs the scripts are named for.
        "--launch",
        "--terminate",
        "--install",
        "--uninstall",
        "--open-url",
        "--swipe",
        "--swipe-path",
        "--scroll",
        "--long-press",
        "--refresh",
        "--find-text",
        "--find-id",
        "--find-type",
        "--tap",
        "--enter-text",
        "--list",
    }
)


def _global_flags(skill_md: str | None = None) -> frozenset[str]:
    """Flags documented once for every script rather than per entry.

    Two sources, both read rather than typed: what ``common.cli.parser``
    registers by itself (its own ``--help``), and the backticked flags of
    SKILL.md's "All scripts support ..." sentence, the one place the doc
    promises a flag to every script. ``skill_md`` is the file's text when a
    caller holds a copy of it; the file on disk otherwise.
    """
    from common import cli

    if skill_md is None:
        skill_md = SKILL_MD.read_text(encoding="utf-8")
    flags = set(_option_tokens(cli.parser(prog="any").format_help()))
    promise = [line for line in skill_md.splitlines() if line.startswith("All scripts support")]
    assert len(promise) == 1, f"SKILL.md's 'All scripts support' sentence: {promise}"
    flags |= {flag for chunk in _BACKTICKED.findall(promise[0]) for flag in _FLAG.findall(chunk)}
    return frozenset(flags)


def _accepted_but_undocumented(skill_md: str | None = None) -> dict[str, list[str]]:
    """{script: options} argparse accepts that neither SKILL.md nor the globals name.

    One spelling per option (the longest), so `-s/--serial` is one finding.
    Only SKILL.md is read, by design: an agent picks its flag from the file it
    has loaded, so a flag a reference file documents is, here, undocumented
    (`test_skill_index.test_a_flag_documented_only_in_a_reference_is_undocumented`).
    """
    if skill_md is None:
        skill_md = SKILL_MD.read_text(encoding="utf-8")
    documented = _documented_options(skill_md)
    global_flags = _global_flags(skill_md)
    missing: dict[str, list[str]] = {}
    for script in sorted((SKILL_ROOT / "scripts").glob("*.py")):
        named = documented.get(script.name, set()) | global_flags
        gaps = sorted(
            max(sorted(entry), key=len)
            for entry in _option_entries(_help_text(str(script)))
            if not entry & named
        )
        if gaps:
            missing[script.name] = gaps
    return missing


def test_every_flag_a_script_accepts_is_documented_or_excused():
    """The reverse of the guard above: accepted minus documented, per script.

    The forward guard catches a flag SKILL.md invents; nothing caught a flag
    SKILL.md never mentions, and an agent reads SKILL.md, not `--help`. So a
    script's interface that is missing from its `- Options:` bullet is, to the
    agent, an interface that does not exist. Each such flag is either
    documented or listed in UNDOCUMENTED_OK with the reason; the core verbs in
    NEVER_UNDOCUMENTED may only be documented.

    The count is computed here, never written down: a number in a plan or a
    doc ("29 undocumented flags") is stale the day after it is counted.
    """
    missing = _accepted_but_undocumented()
    unexcused = {
        script: sorted(set(flags) - UNDOCUMENTED_OK.get(script, frozenset()))
        for script, flags in missing.items()
    }
    unexcused = {script: flags for script, flags in unexcused.items() if flags}
    count = sum(len(flags) for flags in unexcused.values())
    assert not unexcused, (
        f"{count} flag(s) are accepted by argparse but named neither in the script's "
        "SKILL.md `- Options:` bullet nor in UNDOCUMENTED_OK; document them (or, if "
        f"they are not a core verb, excuse them with a reason): {unexcused}"
    )


def test_undocumented_ok_holds_no_core_verb_and_nothing_stale():
    """The ceiling, and the ratchet that keeps the excuse list honest."""
    core = {
        script: sorted(flags & NEVER_UNDOCUMENTED)
        for script, flags in UNDOCUMENTED_OK.items()
        if flags & NEVER_UNDOCUMENTED
    }
    assert not core, f"core action flags may be documented, never excused: {core}"

    scripts = {s.name: s for s in (SKILL_ROOT / "scripts").glob("*.py")}
    gone = sorted(set(UNDOCUMENTED_OK) - set(scripts))
    assert not gone, f"UNDOCUMENTED_OK names scripts that no longer exist: {gone}"

    missing = _accepted_but_undocumented()
    stale = {
        script: sorted(flags - set(missing.get(script, [])))
        for script, flags in UNDOCUMENTED_OK.items()
        if flags - set(missing.get(script, []))
    }
    assert not stale, (
        "UNDOCUMENTED_OK excuses flags that are now documented or no longer accepted; "
        f"delete them: {stale}"
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


# ---------------------------------------------------------------------------
# Toolchain pins (S5): one file states the versions, every install reads it.
# ---------------------------------------------------------------------------

CONSTRAINTS = REPO_ROOT / "dev-constraints.txt"

# What each workflow installs with pip, by hand: the mirror that keeps the scan
# below from passing over nothing, and from passing a test workflow that quietly
# stopped installing Pillow (the image tests would skip, not fail). A workflow
# not named here installs nothing.
WORKFLOW_INSTALLS = {
    "test.yml": {"pip", "pytest", "pillow", "pyyaml"},
    "emulator.yml": {"pip", "pytest", "pillow", "pyyaml"},
    "lint.yml": {"pip", "black", "ruff"},
    "release.yml": {"pip", "pytest", "pillow", "pyyaml", "black", "ruff"},
}

# The one way a workflow installs: a line that is this text and nothing else,
# matched whole. An earlier reader understood shell (words, quotes, `;`,
# comments, continuations, `$(...)`), and four reviews each found a spelling it
# read one way and bash another. A line with nothing in it to interpret has no
# second reading, so the form is one spelling and everything else is refused.
PINNED_NAME = r"[a-z0-9][a-z0-9-]*"
INSTALL_LINE = re.compile(
    rf"(?:python -m )?pip install -c dev-constraints\.txt(?P<names>(?: {PINNED_NAME})+)"
)
INSTALL_FORM = "pip install -c dev-constraints.txt <names pinned there>"
# A step that installs has these keys and no others: `shell:`,
# `working-directory:` and `env:` each change what the same line does.
INSTALL_STEP_KEYS = frozenset({"name", "run"})
# The one action that may be handed the word `pip` and the file's name: it
# reads them as a cache key.
SETUP_PYTHON = re.compile(r"actions/setup-python@[0-9A-Za-z.]+")
SETUP_PYTHON_INPUTS = frozenset(
    {(("with", "cache"), "pip"), (("with", "cache-dependency-path"), "dev-constraints.txt")}
)

# What a code block of CLAUDE.md or DEV.md may show that names an installer.
# Whole lines again; the words after `uvx`'s tool are the tool's own, so the
# tool is not an installer (`uvx -c dev-constraints.txt pip install requests`
# pins pip and nothing pip installs): `tool` names none.
DOCUMENTED_FORMS = (
    INSTALL_LINE,
    re.compile(r"uv venv --python 3\.12"),
    re.compile(rf"uv pip install -c dev-constraints\.txt(?P<names>(?: {PINNED_NAME})+)"),
    re.compile(
        r"uvx --python 3\.12 -c dev-constraints\.txt"
        rf"(?P<tool>(?P<names>(?: --with {PINNED_NAME})* {PINNED_NAME})(?: [A-Za-z0-9._/=:-]+)*)"
    ),
)

# A line of CLAUDE.md or DEV.md that fences a block: three backticks, and a
# language to open one. Spaces before it are a list's indentation.
DOCUMENT_FENCE = "`" * 3
DOCUMENT_FENCE_LINE = re.compile(rf"(?P<indent> *){DOCUMENT_FENCE}(?P<language>[a-z]*)")

# The letters of a tool that installs packages. `pipx` and `uvx` stand before
# `pip` and `uv` only so that a message names the longer one.
INSTALLER_NAMES = ("pipx", "pip", "uvx", "uv", "poetry", "pdm", "conda", "easy_install")
# The one word CI spells with those letters that is not an installer.
NOT_AN_INSTALLER = "pipefail"

THIRD_PARTY_ACTIONS = frozenset(
    {
        "gradle/actions/setup-gradle",
        "reactivecircus/android-emulator-runner",
        "softprops/action-gh-release",
    }
)
COMMIT_PINNED_ACTION = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")


def _shared_pins(text: str | None = None) -> dict[str, str]:
    """``{canonical name: version}`` from dev-constraints.txt, which must hold exact pins only.

    Read with `packaging` (pytest depends on it), so a specifier is a parsed
    operator and version rather than a substring: `pytest>=9`, `pillow==13.*`
    and `pytest==9.2.0; python_version < "3.13"` are each refused.
    """
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    text = CONSTRAINTS.read_text(encoding="utf-8") if text is None else text
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        exact = (
            len(specifiers) == 1
            and specifiers[0].operator == "=="
            and "*" not in specifiers[0].version
            and requirement.url is None
            and requirement.marker is None
            and not requirement.extras
        )
        assert exact, f"dev-constraints.txt: `{line}` is not an exact `name==version` pin"
        name = canonicalize_name(requirement.name)
        assert name not in pins, f"dev-constraints.txt pins {name} twice"
        pins[name] = specifiers[0].version
    return pins


def _installer_named(text: str) -> str | None:
    """The installer whose letters stand anywhere in a string, whatever the case.

    A substring, with no word boundary to be on the right side of: `-mpip`,
    `PIP`, `"pip"` and `pip3.12` all name pip. So do `pipeline` and `--pipe`;
    that is the price, and the message says which letters were found.
    `pipefail` is taken out by name first, replaced by a space so that what
    stood on either side of it cannot join into a name or hide one.
    """
    text = text.casefold().replace(NOT_AN_INSTALLER, " ")
    return next((name for name in INSTALLER_NAMES if name in text), None)


def _named(text: str) -> str | None:
    """What a string names that only an install may: an installer, or the constraints file.

    The file by its letters too, so that a step writing to it
    (`printf ... > dev-constraints.txt`) is read as an install step and refused.
    """
    return _installer_named(text) or (
        CONSTRAINTS.stem if CONSTRAINTS.stem in text.casefold() else None
    )


def _install_lines(script: str, pins: dict[str, str]) -> tuple[set[str], list[str]]:
    """``(names installed, offenders)`` for a script that must be install lines only.

    Split at `\n` and nowhere else: a carriage return or a form feed is not a
    line end to bash, so it stays in its line and the line does not match. One
    empty piece at the end is the newline a YAML block ends in.
    """
    lines = script.split("\n")
    if lines[-1] == "":
        lines.pop()
    names: set[str] = set()
    offenders: list[str] = []
    for line in lines:
        match = INSTALL_LINE.fullmatch(line)
        if match is None:
            offenders.append(
                f"{line!r} is not `{INSTALL_FORM}` (or `python -m pip ...`). A step that names "
                "an installer or the constraints file holds such lines and nothing else: no "
                "comment, quote, option or other command. Put those in a step of their own."
            )
            continue
        for name in match["names"].split():
            if name in pins:
                names.add(name)
            else:
                offenders.append(f"{line!r}: {name} has no pin in dev-constraints.txt")
    return names, offenders


def _strings(node: object, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str, bool]]:
    """``(path, string, is a key)`` for every string in the mappings and lists of parsed YAML.

    A mapping's keys are strings of it too (`env: PIP_INDEX_URL:`), found at
    the mapping's own path and marked: `run: {"pip install ...": null}` has a
    key at the path a `run:` script has, and it is not one. What YAML holds
    besides mappings, lists and plain scalars is `_unwalked`'s to name, and
    what PyYAML would drop for an equal key is `_yaml`'s to refuse.
    """
    if isinstance(node, dict):
        keys = [(path, key, True) for key in node if isinstance(key, str)]
        return keys + [
            found for key, value in node.items() for found in _strings(value, (*path, str(key)))
        ]
    if isinstance(node, list):
        return [
            found
            for index, value in enumerate(node)
            for found in _strings(value, (*path, str(index)))
        ]
    return [(path, node, False)] if isinstance(node, str) else []


PLAIN_SCALARS = (str, int, float, bool, type(None))


def _unwalked(node: object, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    """``(path, type)`` for every key and value of parsed YAML that `_strings` does not walk.

    `!!set`, `!!pairs`, `!!binary` (a value or a key) and a date reach
    Python as a set, tuples, bytes and a `date`, and the first three can hold
    an installer's name where no string of `_strings` shows it. No workflow
    needs one, so each is refused by its type, whatever it holds.
    """
    if isinstance(node, dict):
        keys = [(path, type(key).__name__) for key in node if not isinstance(key, PLAIN_SCALARS)]
        return keys + [
            found for key, value in node.items() for found in _unwalked(value, (*path, str(key)))
        ]
    if isinstance(node, list):
        return [
            found
            for index, value in enumerate(node)
            for found in _unwalked(value, (*path, str(index)))
        ]
    return [] if isinstance(node, PLAIN_SCALARS) else [(path, type(node).__name__)]


def _script_file_offenders(root: Path) -> list[str]:
    """Every line under a directory of CI scripts that names an installer.

    These files are not parsed: shell, Python, YAML, at any depth, comments
    included, and no directory is passed over by its name. An install belongs
    in a workflow step, where `_workflow_installs` reads it; so does every
    mention of the constraints file.
    """
    offenders = []
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            offenders.append(f"{name}: not a regular file")
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            offenders.append(f"{name}: not text this guard can read")
            continue
        for number, line in enumerate(lines, 1):
            named = _named(line)
            if named:
                offenders.append(
                    f"{name}:{number}: names `{named}`; installs belong in a workflow step"
                )
    return offenders


def _unparsed_ci_file_offenders() -> list[str]:
    """The same, for the two places under `.github/` that hold code a workflow runs.

    `scripts/` by path from a `run:`, `actions/` through `uses: ./...`.
    """
    return [
        f".github/{directory}/{reason}"
        for directory in ("actions", "scripts")
        for reason in _script_file_offenders(REPO_ROOT / ".github" / directory)
    ]


def _workflow_installs(workflow: dict, pins: dict[str, str]) -> tuple[set[str], list[str]]:
    """``(names installed, offenders)`` for a parsed workflow.

    Every string of the workflow that names an installer or the constraints
    file is one of three things, and a mapping key that names one is always
    the third:

    - the `run:` of an install step: a step with `name` and `run` only, in a
      job and a workflow that set no `defaults`, whose every line is
      `INSTALL_LINE` naming pinned packages;
    - `cache: pip` or `cache-dependency-path: dev-constraints.txt` handed to
      `actions/setup-python`, and to that action only;
    - an offender.

    Nothing is parsed as shell, so there is no spelling to read differently
    from bash: `sudo pip`, `bash -c "pip ..."`, `$(...)`, a comment, a
    continuation and `cd tests &&` are all simply not the line.

    The strings are those of the mappings and lists `_yaml` returns.
    Anything else YAML can hold (`_unwalked`) is an offender by its type, and
    a mapping in which the loader would drop a value for a key it reads as
    equal to another never gets here: `_yaml` refuses it.
    """
    steps = {
        ("jobs", str(name), "steps", str(index)): (job, step)
        for name, job in (workflow.get("jobs") or {}).items()
        if isinstance(job, dict)
        for index, step in enumerate(job.get("steps") or [])
        if isinstance(step, dict)
    }
    names: set[str] = set()
    offenders = [
        f"{'.'.join(path)}: holds a `{kind}`; this guard reads mappings, lists and plain "
        "scalars only"
        for path, kind in _unwalked(workflow)
    ]
    for path, value, is_key in _strings(workflow):
        named = _named(value)
        if named is None:
            continue
        where = ".".join(path)
        job, step = (None, None) if is_key else steps.get(path[:4], (None, None))
        if step is not None and path[4:] == ("run",):
            others = sorted(str(key) for key in step if key not in INSTALL_STEP_KEYS)
            if others:
                offenders.append(
                    f"{where}: a step that installs has `name` and `run` only, not {others}"
                )
            offenders += [
                f"{where}: {owner} sets `defaults`, which can change the shell or the "
                "directory an install runs in"
                for owner, scope in (("the workflow", workflow), (f"job {path[1]}", job))
                if "defaults" in scope
            ]
            installed, wrong = _install_lines(value, pins)
            names |= installed
            offenders += [f"{where}: {reason}" for reason in wrong]
        elif (
            step is not None
            and SETUP_PYTHON.fullmatch(str(step.get("uses", "")))
            and (path[4:], value) in SETUP_PYTHON_INPUTS
        ):
            continue
        else:
            offenders.append(f"{where}: names `{named}` outside an install step")
    return names, offenders


def test_the_shared_pins_are_exact_and_cover_the_toolchain():
    """S5: `pip install pytest pillow pyyaml` took whatever was newest that morning.

    Black, Ruff, Gradle, AGP and Kotlin were pinned; the three packages the
    suite itself runs on were not, and neither was pip. Pillow 14 (2027-10-15)
    removes `Image.getdata`, which has already forced one rework here, so an
    unpinned install is a red main on a date nobody chose.
    """
    pins = _shared_pins()
    for name in sorted(set().union(*WORKFLOW_INSTALLS.values())):
        assert name in pins, f"dev-constraints.txt has no pin for {name}"


@pytest.mark.parametrize(
    "line",
    ["pytest", "pytest>=9.2.0", "pillow==13.*", "pytest~=9.2", "pytest==9.2.0,<10"],
)
def test_the_shared_pins_refuse_anything_but_an_exact_version(line):
    with pytest.raises(AssertionError, match="not an exact"):
        _shared_pins(f"pyyaml==6.0.3\n{line}\n")


def test_every_workflow_install_takes_its_version_from_the_shared_pins():
    """Every install a workflow writes is `pip install -c dev-constraints.txt <pinned names>`.

    What is read: every string in `.github/workflows/` (parsed with pyyaml),
    and every line of every file under `.github/scripts/` and
    `.github/actions/`. A workflow names an installer or the constraints file
    in an install step, written the one way `_workflow_installs` describes,
    or not at all.

    The limit: this is a check of how the workflows are written, not of what
    a job does when it runs. A step that rewrites the constraints file under
    a name it builds, puts another `pip` first on PATH, or runs an installer
    this module has no name for (`$TOOL install`, a download piped to a
    shell) is past it. So are the job's own `env:`, `container:` and runner,
    and whatever the suite, the skill's scripts or an action from another
    repository does.
    """
    pins = _shared_pins()
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        names, wrong = _workflow_installs(_yaml(path.name), pins)
        offenders += [f"{path.name}: {reason}" for reason in wrong]
        expected = WORKFLOW_INSTALLS.get(path.name, set())
        assert names == expected, (
            f"{path.name} installs {sorted(names)} with pip; WORKFLOW_INSTALLS says "
            f"{sorted(expected)}. Change the mirror only with the workflow."
        )
    offenders += _unparsed_ci_file_offenders()
    assert not offenders, "installs that do not take their version from dev-constraints.txt:\n" + (
        "\n".join(offenders)
    )


CONSTRAINED = "pip install -c dev-constraints.txt"
NOT_THE_LINE = f"is not `{INSTALL_FORM}`"


def _one_step(script: str, **keys: object) -> dict:
    return {"jobs": {"test": {"steps": [{"run": script, **keys}]}}}


def _offenders(workflow: dict) -> list[str]:
    return _workflow_installs(workflow, _shared_pins())[1]


@pytest.mark.parametrize(
    ("script", "names"),
    [
        (f"{CONSTRAINED} pytest pillow pyyaml", {"pytest", "pillow", "pyyaml"}),
        # As the workflows write it: a YAML block, which ends in one newline.
        (f"python -m {CONSTRAINED} pip\n{CONSTRAINED} black ruff\n", {"pip", "black", "ruff"}),
        # A step that names neither an installer nor the file is not read.
        ("pytest tests/ -q", set()),
        ("set -euo pipefail\npytest tests/  # the whole suite", set()),
        ("cd tests && ls", set()),
    ],
)
def test_the_pin_guard_passes_the_install_line_and_steps_that_install_nothing(script, names):
    """...and is not simply always red, which would fail the workflows for the wrong reason."""
    assert _workflow_installs(_one_step(script), _shared_pins()) == (names, [])
    named = _one_step(script, name="Install test dependencies")
    assert _workflow_installs(named, _shared_pins()) == (names, [])


@pytest.mark.parametrize(
    "script",
    [
        # The four lines S5 named, as they stood on main.
        "pip install pytest pillow pyyaml",
        "python -m pip install --upgrade pip",
        "pip install pytest 'black==26.5.1'",
        f"{CONSTRAINED} 'black==26.5.1'",
        # A package that states its own version, an option, a variable.
        f"{CONSTRAINED} 'pillow>=13'",
        f"{CONSTRAINED} black==26.5.1",
        f"{CONSTRAINED} -r requirements.txt",
        f"{CONSTRAINED} --pre pytest",
        f"{CONSTRAINED} $PACKAGES",
        f"""{CONSTRAINED} 'pytest; python_version < "3.0"'""",
        f"{CONSTRAINED} 'pytest[dev]'",
        f"{CONSTRAINED} pytest[dev]",
        f"{CONSTRAINED}",
        # Another constraints file, or the same name read another way.
        "pip install -c other.txt pytest",
        "pip install --constraint=other.txt pytest",
        "pip install -c ../dev-constraints.txt pytest",
        "pip install -c tests/dev-constraints.txt pytest",
        "pip install -c dev-constraintsxtxt pytest",
        # pip 26.3 reads the file name here as `=dev-constraints.txt`.
        "pip install -c=dev-constraints.txt pytest",
        # Something before pip, or another pip.
        "sudo pip3 install pytest",
        "/usr/bin/pip3.12 install pytest",
        f"sudo {CONSTRAINED} pytest",
        f"uv {CONSTRAINED} pytest",
        f"env -C tests {CONSTRAINED} pytest",
        f"PIP_INDEX_URL=https://example.invalid/simple {CONSTRAINED} pytest",
        f"then {CONSTRAINED} pytest",
        f"echo {CONSTRAINED} pytest",
        f"python3 -m {CONSTRAINED} pytest",
        f"/usr/bin/{CONSTRAINED} pytest",
        # Two commands on a line, a continuation, a comment.
        "true && pip install pytest",
        f"{CONSTRAINED} pytest; pip install pillow",
        f"{CONSTRAINED} pytest && pip install pillow",
        "pip install \\\n  pytest",
        f"{CONSTRAINED} \\\n  pytest",
        "true x#; pip install requests",
        "echo a#b && pip install requests",
        "# pip install pytest",
        f"# why\n{CONSTRAINED} pytest",
        f"{CONSTRAINED} ruff  # lint",
        "pytest tests/  # pip install pytest",
        f"{CONSTRAINED} pytest \\\n# requests",
        # A comment that ends in a backslash: an earlier reader joined the next
        # line onto it and never saw the `cd`; bash runs it.
        f"# prepare \\\ncd tests\n{CONSTRAINED} pytest",
        # A step that installs does nothing else, before or after.
        f"cd tests && {CONSTRAINED} pytest",
        f"cd tests\n{CONSTRAINED} pytest",
        f"{CONSTRAINED} pytest\ncd tests",
        f"pushd tests\n{CONSTRAINED} pytest",
        f"export CONSTRAINTS=other.txt\n{CONSTRAINED} pytest",
        f"source ci/setup.sh\n{CONSTRAINED} pytest",
        f"{CONSTRAINED} pytest\npytest tests/",
        f"set -euo pipefail\n{CONSTRAINED} pytest",
        # White space that is not one space, and line ends that are not `\n`.
        f"{CONSTRAINED}  pytest",
        f"{CONSTRAINED}\tpytest",
        f" {CONSTRAINED} pytest",
        f"{CONSTRAINED} pytest ",
        f"\n{CONSTRAINED} pytest",
        f"{CONSTRAINED} pytest\n\n",
        f"{CONSTRAINED} pytest\n\n{CONSTRAINED} pillow",
        f"{CONSTRAINED} pytest\rrequests",
        f"{CONSTRAINED} pytest\r{CONSTRAINED} pillow",
        f"{CONSTRAINED} pytest\r\n",
        f"{CONSTRAINED} pytest\x0c{CONSTRAINED} pillow",
        "true\x0c# x; pip install requests",
        "\x0c# x || pip install requests",
        f"\x0c# x; cd tests\n{CONSTRAINED} pytest",
        f"{CONSTRAINED} pytest requests",
        f"{CONSTRAINED} pytest {CONSTRAINED} pillow",
        # A word the shell runs or expands is not the word that was read.
        'pip install -c "$(pip install requests >&2; echo x)/../dev-constraints.txt" pytest',
        'pip install -c "`pip install requests >&2`x/../dev-constraints.txt" pytest',
        '"$(pip install requests >&2; echo /usr/bin)/pip" install -c dev-constraints.txt pytest',
        f'"$(pip install requests >&2; echo /usr/bin)/python" -m {CONSTRAINED} pytest',
        'pip install -c "$d/../dev-constraints.txt" pytest',
        'pip install -c "`pwd`/../dev-constraints.txt" pytest',
        'pip install -c "é/../dev-constraints.txt" pytest',
        f"{CONSTRAINED} pytest${{EXTRA}}",
        f"{CONSTRAINED} pytest ';'",
        f"{CONSTRAINED} 'pytest'",
        # Another installer, or pip not as `pip install`.
        "uvx --with pillow pytest tests/",
        "true; pipx install black",
        "sudo pipx install black",
        "python3 -m pipx install black",
        "if true; then uvx black; fi",
        "python3 -m pip --disable-pip-version-check install pytest",
        "pip download pytest",
        # No word boundary to slip past, and no case: `-mpip` is `-m pip` to
        # Python, and `PIP` is pip wherever the file system folds case.
        "python -mpip install pytest",
        "python3 -mpipx run black",
        "PIP install pytest",
        "Python -m Pip install pytest",
        f"PIP{CONSTRAINED[3:]} pytest",
        "pip.exe install pytest",
        # pip's environment is pip's configuration.
        "export PIP_CONSTRAINT=other.txt",
        # An install inside a quoted word, and prose: nothing tells `echo` from `bash -c`.
        'bash -c "pip install pytest"',
        'value="$(pip install pytest)"',
        "eval 'pip install pytest'",
        'python -c \'import subprocess; subprocess.run(["pip", "install", "pytest"])\'',
        'echo "pip install pytest"',
        # The constraints file is named to be read by an install, and not to be written.
        "printf 'pytest==9.1.1\\n' > dev-constraints.txt",
        "printf 'pytest==9.1.1\\n' > =dev-constraints.txt",
        "cp other.txt DEV-CONSTRAINTS.txt",
    ],
)
def test_the_pin_guard_refuses_every_line_that_is_not_the_install_line(script):
    """Guard the guard: one form is let through, so each of these is refused for not being it.

    Among them every spelling the earlier, shell-reading guard was shown to
    misread, and every one it refused.
    """
    offenders = _offenders(_one_step(script))
    assert any(NOT_THE_LINE in offender for offender in offenders), offenders


@pytest.mark.parametrize(
    "script",
    [
        # Each was let through by the reader that understood pip's options and
        # the shell's words. All still install from the pins; none is the line.
        f"python -m {CONSTRAINED} --upgrade pip",
        "pip install --constraint dev-constraints.txt pillow pyyaml",
        "pip install --constraint=dev-constraints.txt -q ruff",
        f"{CONSTRAINED} Pillow PyYAML",
        f"python3.12 -m {CONSTRAINED} pytest",
        "/usr/bin/pip3.12 install -c dev-constraints.txt pytest",
        f"  # why\n{CONSTRAINED} pytest",
    ],
)
def test_the_install_line_has_one_spelling(script):
    """The price of reading no shell, paid on purpose: a harmless variant is refused too."""
    offenders = _offenders(_one_step(script))
    assert any(NOT_THE_LINE in offender for offender in offenders), offenders
    assert any("Put those in a step of their own" in offender for offender in offenders)


def test_the_pin_guard_refuses_a_package_the_shared_pins_do_not_name():
    assert _workflow_installs(_one_step(f"{CONSTRAINED} pytest requests"), _shared_pins()) == (
        {"pytest"},
        [
            f"jobs.test.steps.0.run: '{CONSTRAINED} pytest requests': "
            "requests has no pin in dev-constraints.txt"
        ],
    )


@pytest.mark.parametrize(
    "keys",
    [
        {"working-directory": "tests"},
        # Normalising this path cancels the expression; the runner does not.
        {"working-directory": "${{ runner.temp }}/.."},
        {"shell": 'bash -c "cd tests; exec bash {0}"'},
        {"shell": "bash"},
        {"env": {"HOME": "/tmp/elsewhere"}},
        {"if": "always()"},
        {"continue-on-error": True},
        {"uses": "actions/setup-python@v5"},
    ],
)
def test_a_step_that_installs_has_a_name_and_a_run_and_nothing_else(keys):
    """`shell:` and `working-directory:` each make the same line read another file.

    `-c dev-constraints.txt` from `tests/` names a file that is not the pins,
    and a custom shell can change directory before the script starts. Rather
    than read each key for what it changes, an install step has none.
    """
    offenders = _offenders(_one_step(f"{CONSTRAINED} pytest", **keys))
    assert any("has `name` and `run` only" in offender for offender in offenders), offenders


@pytest.mark.parametrize(
    "defaults",
    [
        {"run": {"shell": 'bash -c "cd tests; exec bash {0}"'}},
        {"run": {"working-directory": "tests"}},
        {},
    ],
)
def test_no_defaults_stand_over_a_step_that_installs(defaults):
    """`defaults.run` sets the shell and directory of every step that names neither."""
    workflow = _one_step(f"{CONSTRAINED} pytest")
    assert _offenders({**workflow, "defaults": defaults}) == [
        "jobs.test.steps.0.run: the workflow sets `defaults`, which can change the shell or "
        "the directory an install runs in"
    ]
    workflow["jobs"]["test"]["defaults"] = defaults
    assert _offenders(workflow) == [
        "jobs.test.steps.0.run: job test sets `defaults`, which can change the shell or "
        "the directory an install runs in"
    ]

    # A job that installs nothing may set its own.
    workflow = _one_step(f"{CONSTRAINED} pytest")
    workflow["jobs"]["other"] = {"defaults": defaults, "steps": [{"run": "pytest tests/"}]}
    assert _offenders(workflow) == []


def test_an_installer_named_outside_an_install_step_is_refused():
    """The emulator runner's `with: script:` is a shell script, and not a `run:`."""
    pins = _shared_pins()
    runner = {"uses": "reactivecircus/android-emulator-runner@v2"}
    workflow = {"jobs": {"lane": {"steps": [{**runner, "with": {"script": "pip install pytest"}}]}}}
    assert _workflow_installs(workflow, pins) == (
        set(),
        ["jobs.lane.steps.0.with.script: names `pip` outside an install step"],
    )

    workflow = {"env": {"RUNNER": "uvx"}, "jobs": {"lane": {"steps": [{"run": "$RUNNER pytest"}]}}}
    assert _workflow_installs(workflow, pins) == (
        set(),
        ["env.RUNNER: names `uvx` outside an install step"],
    )

    workflow = {"env": {"SETUP": "python -mpip install requests"}, "jobs": {}}
    assert _workflow_installs(workflow, pins) == (
        set(),
        ["env.SETUP: names `pip` outside an install step"],
    )

    # A key is a string too: pip reads its configuration from the environment.
    workflow = {"jobs": {"test": {"env": {"PIP_INDEX_URL": "https://example.invalid/simple"}}}}
    assert _workflow_installs(workflow, pins) == (
        set(),
        ["jobs.test.env: names `pip` outside an install step"],
    )

    # A `run:` that is not text is not a script this guard reads.
    workflow = {"jobs": {"test": {"steps": [{"run": [f"{CONSTRAINED} pytest"]}]}}}
    assert _workflow_installs(workflow, pins) == (
        set(),
        ["jobs.test.steps.0.run.0: names `pip` outside an install step"],
    )

    # The constraints file is not handed to anything but an install.
    upload = {"uses": "actions/upload-artifact@v4", "with": {"path": "dev-constraints.txt"}}
    assert _workflow_installs({"jobs": {"test": {"steps": [upload]}}}, pins) == (
        set(),
        ["jobs.test.steps.0.with.path: names `dev-constraints` outside an install step"],
    )


def test_setup_python_is_handed_the_pip_cache_and_the_file_it_is_keyed_on():
    """The two mentions that install nothing, as the four workflows make them."""
    setup = {
        "uses": "actions/setup-python@v5",
        "with": {
            "python-version": "3.12",
            "cache": "pip",
            "cache-dependency-path": "dev-constraints.txt",
        },
    }
    assert _workflow_installs({"jobs": {"test": {"steps": [setup]}}}, _shared_pins()) == (set(), [])

    for key, value in (
        ("cache", "pipx"),
        ("cache-dependency-path", "tests/dev-constraints.txt"),
        ("cache-dependency-path", "${{ hashFiles('dev-constraints.txt') }}"),
        ("python-version", "pip"),
    ):
        changed = {**setup, "with": {**setup["with"], key: value}}
        assert _offenders({"jobs": {"test": {"steps": [changed]}}}) == [
            f"jobs.test.steps.0.with.{key}: names `{_named(value)}` outside an install step"
        ]


@pytest.mark.parametrize(
    "uses",
    [
        "./.github/actions/install",
        "someone/setup-python@v5",
        "actions/setup-python-and-more@v5",
        "actions/setup-python/sub@v5",
        "actions/setup-python",
        "actions/setup-python@",
        "actions/setup-python@v5@extra",
        "actions/setup-python@v5\n",
        None,
    ],
)
@pytest.mark.parametrize(
    ("key", "value", "named"),
    [("cache", "pip", "pip"), ("cache-dependency-path", "dev-constraints.txt", "dev-constraints")],
)
def test_the_cache_inputs_are_let_through_for_setup_python_and_no_other_action(
    uses, key, value, named
):
    """`with: cache: pip` is a word an action receives; what it does with it is the action's.

    A local action whose step is `${{ inputs.cache }} install requests` gets
    its installer from this input, so the exception names the one action known
    to treat it as a cache key, by its whole reference.
    """
    step = {"with": {key: value}} if uses is None else {"uses": uses, "with": {key: value}}
    assert _workflow_installs({"jobs": {"test": {"steps": [step]}}}, _shared_pins()) == (
        set(),
        [f"jobs.test.steps.0.with.{key}: names `{named}` outside an install step"],
    )


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("pip", "pip"),
        ("/usr/bin/pip3.12", "pip"),
        ("-mpip", "pip"),
        ("PIP", "pip"),
        ("pipx", "pipx"),
        ("$(uvx black)", "uvx"),
        ("uv", "uv"),
        ("micromamba-conda", "conda"),
        ("pipefailpip", "pip"),
        ("pipefail pipefail", None),
        ("pipipefailp", None),
        ("set -euo pipefail", None),
        ("pytest tests/", None),
        # The price of having no boundary, paid on purpose: a word that merely
        # holds the letters is refused, and the message says which letters.
        ("pipeline", "pip"),
        ("maneuver", "uv"),
    ],
)
def test_an_installer_is_named_by_its_letters_wherever_they_stand(text, name):
    """A substring, whatever the case: a boundary is a thing to be on the right side of.

    A whole-word pattern read `-mpip` as some other word. The one word let
    through is `pipefail`, by name, and taking it out cannot hide what stands
    beside it.
    """
    assert _installer_named(text) == name


def test_no_file_under_github_scripts_names_an_installer(tmp_path):
    """Installs belong in a workflow step, where they are read; these files are not parsed.

    Any file, at any depth: `setup/install.sh` and `install.py` run as readily
    as a `*.sh` beside the lane script.
    """
    assert _script_file_offenders(REPO_ROOT / ".github" / "scripts") == []

    (tmp_path / "setup").mkdir()
    (tmp_path / "setup" / "install.sh").write_text("#!/bin/bash\npip install pytest\n")
    (tmp_path / "install.py").write_text('import subprocess\nsubprocess.run(["uvx", "black"])\n')
    (tmp_path / "lane.sh").write_text("set -euo pipefail\npytest -m emulator tests/\n")
    (tmp_path / "pins.sh").write_text("echo ok\nsed -i s/9.2.0/9.1.1/ dev-constraints.txt\n")
    (tmp_path / "blob").write_bytes(b"\xff\xfe pip")
    # No directory is passed over by name: `bash .github/scripts/__pycache__/install.sh` runs.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "install.sh").write_text("python -mpip install requests\n")
    (tmp_path / "__pycache__" / "lane.cpython-312.pyc").write_bytes(b"\xcb\r\r\n\x00\xff")
    assert _script_file_offenders(tmp_path) == [
        "__pycache__/install.sh:1: names `pip`; installs belong in a workflow step",
        "__pycache__/lane.cpython-312.pyc: not text this guard can read",
        "blob: not text this guard can read",
        "install.py:2: names `uvx`; installs belong in a workflow step",
        "pins.sh:2: names `dev-constraints`; installs belong in a workflow step",
        "setup/install.sh:2: names `pip`; installs belong in a workflow step",
    ]
    assert _script_file_offenders(tmp_path / "no-such-directory") == []


def test_a_local_action_is_read_like_a_script(monkeypatch, tmp_path):
    """`.github/actions/` holds steps a workflow runs through `uses: ./...`, and no `run:` reader."""
    action = tmp_path / ".github" / "actions" / "install" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  using: composite\n  steps:\n    - run: pip install requests\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    assert _unparsed_ci_file_offenders() == [
        ".github/actions/install/action.yml:4: names `pip`; installs belong in a workflow step"
    ]


def test_pre_commit_runs_the_black_and_ruff_the_shared_pins_name():
    """pre-commit cannot read a constraints file, so its two `rev:`s are compared instead."""
    import yaml

    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    revs = {
        hook["id"]: repo.get("rev") for repo in config["repos"] for hook in repo.get("hooks", [])
    }
    pins = _shared_pins()
    assert revs["black"] == pins["black"], "pre-commit and dev-constraints.txt disagree on Black"
    assert revs["ruff"] == f"v{pins['ruff']}", "pre-commit and dev-constraints.txt disagree on Ruff"


def _fenced_lines(markdown: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """``(lines inside backtick fences, fence lines this reader cannot pair)``.

    Markdown is not parsed. A line with three backticks in a row is a fence
    line, and it is `DOCUMENT_FENCE_LINE` whole: with or without a language it
    opens a block, and alone, as far in as the fence that opened it, it closes
    one. Every other place the backticks stand is refused, since each is a way
    for a block to go on, or to exist, where this reader sees none: a fence
    with a language inside a block (it does not close one), four backticks
    (three do not close them), a closing fence at another indentation or a
    line left of its block's fence (in a list either ends the block early), a
    fence in a quote or behind a tab, words after it, a block left open. So
    is a line with `~~~` anywhere in it, which fences a block in a quote or
    a list item as it does at the margin. A refused line changes nothing:
    what follows is read as it was being read.

    The limit: fences are paired by counting, which is what Markdown does
    for a document whose three-backtick lines are all fences. A line of
    three backticks that Markdown takes for something else (inside an HTML
    block or a comment, or indented as code outside a list) is counted like
    any other, so a document built around such lines can show a block this
    reader pairs wrongly. That is past it: this checks that the commands a
    plainly written document shows are the pinned ones.

    Lines end at a newline only, as bash ends them; `splitlines()` would cut
    a command in two at a form feed.
    """
    lines: list[str] = []
    refused: list[tuple[str, list[str]]] = []
    opened: re.Match[str] | None = None
    for line in markdown.split("\n"):
        if "~~~" in line:
            refused.append(
                (line, ["has `~~~`, which fences a block this guard does not read; use backticks"])
            )
        elif DOCUMENT_FENCE in line:
            fence = DOCUMENT_FENCE_LINE.fullmatch(line)
            if fence is None or (
                opened is not None and (fence["language"] or fence["indent"] != opened["indent"])
            ):
                refused.append(
                    (
                        line,
                        [
                            "is a fence this guard cannot pair. Three backticks and a language "
                            "open a block, three alone at the same indentation close it, and "
                            "they stand nowhere else"
                        ],
                    )
                )
            else:
                opened = fence if opened is None else None
        elif opened is not None:
            if line.strip(" ") and not line.startswith(opened["indent"]):
                refused.append(
                    (
                        line,
                        [
                            "stands left of the fence of the block it is in; in a list that "
                            "ends the block"
                        ],
                    )
                )
            lines.append(line)
    if opened is not None:
        refused.append((opened.string, ["opens a block that nothing closes"]))
    return lines, refused


def _documented_installs(markdown: str) -> list[tuple[str, list[str]]]:
    """``(line, offenders)`` for each line of a document's code blocks that names an installer.

    Such a line is one of `DOCUMENTED_FORMS`, whole, and every package it
    names has a pin. Named by its letters, as in a workflow: a line of a tree
    or of sample output that has them is refused too, and is reworded.
    Indentation is the document's (a block inside a list), so spaces at
    either end are not the command's.

    The limit: a block is what `_fenced_lines` pairs, with the limit stated
    there. A command in running prose (`inline code`), in an HTML element or
    in a block made by indentation alone is not read.
    """
    pins = _shared_pins()
    fenced, found = _fenced_lines(markdown)
    for raw in fenced:
        line = raw.strip(" ")
        if _installer_named(line) is None:
            continue
        match = next((match for form in DOCUMENTED_FORMS if (match := form.fullmatch(line))), None)
        if match is None:
            shown = "; ".join(f"`{form.pattern}`" for form in DOCUMENTED_FORMS)
            found.append(
                (
                    line,
                    [
                        f"is not a command this guard reads. It reads, whole: {shown}. "
                        "Reword a line that is not a command."
                    ],
                )
            )
            continue
        second = _installer_named(match.groupdict().get("tool") or "")
        if second is not None:
            found.append(
                (
                    line,
                    [
                        f"names `{second}` after the constraints file; `uvx` runs a tool "
                        "there, and what an installer run that way installs has no pin"
                    ],
                )
            )
            continue
        names = [word for word in match.groupdict().get("names", "").split() if word != "--with"]
        found.append((line, [f"{name} has no pin" for name in names if name not in pins]))
    return found


@pytest.mark.parametrize("doc", ["CLAUDE.md", "DEV.md"])
def test_the_documented_install_commands_use_the_shared_pins(doc):
    """The command a contributor copies must take the versions CI tests with."""
    found = _documented_installs((REPO_ROOT / doc).read_text(encoding="utf-8"))
    assert found, f"{doc} documents no install command; this test is reading nothing"
    wrong = [(line, offenders) for line, offenders in found if offenders]
    assert not wrong, f"{doc} documents installs that ignore dev-constraints.txt: {wrong}"


def _documented(command: str) -> list[tuple[str, list[str]]]:
    fence = "```"
    return _documented_installs(f"Run:\n\n{fence}bash\n{command}\n{fence}\n")


@pytest.mark.parametrize(
    "command",
    [
        # No constraints file, another one, or the name read another way.
        "uvx --python 3.12 --with pillow --with pyyaml pytest tests/",
        "env uvx pytest tests/",
        "uvx --constraints=other.txt pytest tests/",
        "uvx --python 3.12 -c other.txt pytest tests/",
        "uvx --python 3.12 -c=dev-constraints.txt pytest tests/",
        "uvx --python 3.12 -c dev-constraintsxtxt pytest tests/",
        "uvx --python 3x12 -c dev-constraints.txt pytest tests/",
        "uv pip install -c dev-constraintsxtxt pytest",
        "uv venv --python 3x12",
        # A package from somewhere the pins do not name.
        "uvx --python 3.12 -c dev-constraints.txt --from pytest==9.0 pytest tests/",
        "uvx --python 3.12 -c dev-constraints.txt --from=pytest==9.0 pytest tests/",
        "uvx --python 3.12 -c dev-constraints.txt --with-requirements r.txt pytest",
        "uvx --python 3.12 -c dev-constraints.txt --index https://example.invalid pytest",
        "uvx --python 3.12 -c dev-constraints.txt --with 'pillow>=13' pytest tests/",
        "uvx --python 3.12 -c dev-constraints.txt --with pillow>=13 pytest tests/",
        "uvx --python 3.12 -c dev-constraints.txt --with=requests pytest",
        "uvx --python 3.12 -c dev-constraints.txt pytest@9.0 tests/",
        "uvx --python 3.12 -c dev-constraints.txt 'pytest==9.0' tests/",
        # `--with` and no name it can be: the tool would be read as `--with`.
        "uvx --python 3.12 -c dev-constraints.txt --with Pillow pytest tests/",
        "uvx --python 3.12 -c dev-constraints.txt --with pillow",
        "uvx --python 3.12 -c dev-constraints.txt",
        # Two commands, a comment, a substitution.
        "uvx --python 3.12 -c dev-constraints.txt pytest tests/ && uvx black",
        "uvx --python 3.12 -c dev-constraints.txt pytest tests/ # the suite",
        "uvx --python 3.12 -c dev-constraints.txt pytest $(uvx black)",
        "uvx --python 3.12 -c dev-constraints.txt pytest\ttests/",
        'pip install -c "$(pip install requests >&2; echo x)/../dev-constraints.txt" pytest',
        "uv pip install black==26.5.1 pytest pillow",
        "uv pip install pytest pillow",
        "uv venv --python 3.12 && uv pip install requests",
        "uv tool install black",
        "pipx install black",
        # The spelling counts here too.
        "uvx -p 3.12 --constraints=dev-constraints.txt -w pillow pytest tests/",
        "UVX --python 3.12 -c dev-constraints.txt pytest tests/",
    ],
)
def test_a_documented_command_that_names_an_installer_is_one_this_guard_reads(command):
    """`--from pytest==9.0`, an unpinned tool and `$(...)` each ran what the pins do not name."""
    ((line, offenders),) = _documented(command)
    assert line == command
    assert len(offenders) == 1 and "is not a command this guard reads" in offenders[0], offenders


@pytest.mark.parametrize(
    ("command", "unpinned"),
    [
        ("uvx --python 3.12 -c dev-constraints.txt requests", "requests"),
        ("uvx --python 3.12 -c dev-constraints.txt --with requests pytest tests/", "requests"),
        ("uvx --python 3.12 -c dev-constraints.txt --with pillow tox", "tox"),
        ("uv pip install -c dev-constraints.txt pytest requests", "requests"),
        (f"{CONSTRAINED} requests", "requests"),
    ],
)
def test_a_documented_command_names_only_pinned_packages(command, unpinned):
    assert _documented(command) == [(command, [f"{unpinned} has no pin"])]


@pytest.mark.parametrize(
    "command",
    [
        "uvx --python 3.12 -c dev-constraints.txt --with pillow --with pyyaml pytest tests/",
        "uvx --python 3.12 -c dev-constraints.txt black --check --diff tests/",
        # Words after the tool are the tool's own arguments, not uvx's.
        "uvx --python 3.12 -c dev-constraints.txt pytest --with anything -x -k name=value",
        "uv venv --python 3.12",
        "uv pip install -c dev-constraints.txt black ruff pytest pillow pyyaml pre-commit",
        f"python -m {CONSTRAINED} pip",
        # Inside a list, a block is indented with the list.
        "   uvx --python 3.12 -c dev-constraints.txt pytest tests/",
    ],
)
def test_the_documented_reader_passes_a_pinned_command(command):
    assert _documented(command) == [(command.strip(" "), [])]


TILDE_REFUSAL = "has `~~~`, which fences a block this guard does not read; use backticks"


def test_a_block_fenced_with_tildes_is_refused_and_not_passed_over():
    assert _documented_installs("Run:\n\n~~~bash\npip install requests\n~~~\n") == [
        ("~~~bash", [TILDE_REFUSAL]),
        ("~~~", [TILDE_REFUSAL]),
    ]


@pytest.mark.parametrize(
    "markdown",
    [
        "> ~~~bash\n> pip install requests\n> ~~~\n",
        "- ~~~bash\n  pip install requests\n  ~~~\n",
        "1. ~~~\n   pip install requests\n   ~~~\n",
    ],
)
def test_a_tilde_fence_is_refused_wherever_in_its_line_it_stands(markdown):
    """Only a line that began with one was refused: in a quote or a list item it opened unseen."""
    found = _documented_installs(markdown)
    assert [reasons for _, reasons in found] == [[TILDE_REFUSAL], [TILDE_REFUSAL]], found


@pytest.mark.parametrize(
    "document",
    [
        "env: !!set {pip: null}\njobs: {}\n",
        "env: !!pairs [{X: pip}]\njobs: {}\n",
        "env: {X: !!binary cGlw}\njobs: {}\n",
        "? !!binary cGlw\n: x\njobs: {}\n",
        "env: {X: 2026-11-09}\njobs: {}\n",
        "jobs: {a: {steps: [{env: !!set {pip: null}}]}}\n",
    ],
)
def test_a_workflow_holding_what_the_guard_does_not_walk_is_refused(document):
    """YAML's set, pairs, binary (as a value or a key) and date are types `_strings` passes."""
    import yaml

    names, offenders = _workflow_installs(yaml.safe_load(document), _shared_pins())
    assert names == set()
    assert (
        len(offenders) == 1 and "mappings, lists and plain scalars only" in offenders[0]
    ), offenders


@pytest.mark.parametrize(
    "document",
    [
        "env: {yes: pip, on: ok}\njobs: {}\n",
        "env:\n  true: pip\n  on: ok\njobs: {}\n",
        "env: {X: pip, X: ok}\njobs: {}\n",
        "jobs: {a: {steps: [{run: pip install requests, run: echo ok}]}}\n",
        # A merged key the mapping writes again: counted after the merge is laid out.
        "env: {<<: {X: pip, Y: ok}, X: ok}\njobs: {}\n",
    ],
)
def test_a_workflow_with_two_keys_the_loader_reads_as_one_is_refused(
    document, monkeypatch, tmp_path
):
    """PyYAML kept the last and dropped the other: `yes` and `on` are both `True` to it."""
    (tmp_path / "test.yml").write_text(document, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOWS", tmp_path)
    with pytest.raises(AssertionError, match="reads as one key"):
        _yaml("test.yml")


def test_the_documented_reader_passes_over_what_names_no_installer():
    assert _documented("source .venv/bin/activate\npre-commit install\npytest tests/ -q") == []
    # Prose is not a command a reader copies.
    assert _documented_installs("Never run `pip install pytest` by hand.\n") == []


@pytest.mark.parametrize(
    "command",
    [
        "uvx --python 3.12 -c dev-constraints.txt pip install requests",
        "uvx --python 3.12 -c dev-constraints.txt pip install -c other.txt pytest",
        "uvx --python 3.12 -c dev-constraints.txt --with pillow uv pip install requests",
        "uvx --python 3.12 -c dev-constraints.txt pytest tests/ -p pipx",
    ],
)
def test_a_documented_uvx_command_runs_a_tool_and_not_a_second_installer(command):
    """`uvx -c dev-constraints.txt pip install requests` pinned pip and nothing pip installed."""
    ((line, offenders),) = _documented(command)
    assert line == command
    assert len(offenders) == 1 and "after the constraints file" in offenders[0], offenders


@pytest.mark.parametrize("end", ["\x0b", "\x0c", "\r", "\x1c", "\x85", "\u2028"])
def test_a_documented_line_ends_at_a_newline_and_at_nothing_else(end):
    """bash reads a form feed as part of a word; `splitlines()` read two good commands."""
    ((line, offenders),) = _documented(f"{CONSTRAINED} pytest{end}{CONSTRAINED} pillow")
    assert end in line
    assert len(offenders) == 1 and "is not a command this guard reads" in offenders[0], offenders


FENCE = "`" * 3


@pytest.mark.parametrize(
    ("markdown", "refused"),
    [
        # A fence with a language does not close a block: the second command is inside it.
        (
            f"{FENCE}bash\n{CONSTRAINED} pytest\n{FENCE}bash\npip install requests\n{FENCE}\n",
            f"{FENCE}bash",
        ),
        # Four backticks are closed by four, so three would not end the block.
        (f"{FENCE}`bash\npip install requests\n{FENCE}\n", f"{FENCE}`bash"),
        (f"{FENCE}bash\npip install requests\n{FENCE}`\n", f"{FENCE}`"),
        # A block in a quote, behind a tab, or with words after the fence.
        (f"> {FENCE}bash\n> pip install requests\n> {FENCE}\n", f"> {FENCE}bash"),
        (f"\t{FENCE}bash\n\tpip install requests\n\t{FENCE}\n", f"\t{FENCE}bash"),
        (f"{FENCE}bash title=x\npip install requests\n{FENCE}\n", f"{FENCE}bash title=x"),
        (f"{FENCE}bash\npip install requests\n{FENCE} \n", f"{FENCE} "),
        # In a list, a fence at the margin ends the item and opens a block of its own.
        (
            f"1. Step:\n\n   {FENCE}bash\n   {CONSTRAINED} pytest\n{FENCE}\npip install requests\n",
            FENCE,
        ),
        (
            f"Run {FENCE}pip install requests{FENCE} now.\n",
            f"Run {FENCE}pip install requests{FENCE} now.",
        ),
    ],
)
def test_a_fence_this_reader_cannot_pair_is_refused(markdown, refused):
    """Reading a block as closed while Markdown still shows it passes over its commands."""
    found = dict(_documented_installs(markdown))
    assert refused in found, found
    assert any("is a fence this guard cannot pair" in reason for reason in found[refused])


def test_a_line_left_of_the_fence_of_its_block_is_refused():
    """In a list such a line ends the item, and the block with it: the next fence opens one."""
    found = _documented_installs(
        f"1. Step:\n\n   {FENCE}bash\n{CONSTRAINED} pytest\n\n   \n   {FENCE}\n"
    )
    left = "stands left of the fence of the block it is in; in a list that ends the block"
    assert found == [(f"{CONSTRAINED} pytest", [left]), (f"{CONSTRAINED} pytest", [])]


def test_a_block_left_open_is_refused():
    found = _documented_installs(f"Run:\n\n{FENCE}bash\n{CONSTRAINED} pytest\n")
    assert (f"{FENCE}bash", ["opens a block that nothing closes"]) in found


def test_the_documented_reader_pairs_a_plain_fence_and_one_inside_a_list():
    markdown = (
        f"{FENCE}\n{CONSTRAINED} pytest\n{FENCE}\n\n"
        f"1. Then:\n\n   {FENCE}bash\n   {CONSTRAINED} pillow\n   {FENCE}\n\npip install requests\n"
    )
    assert _documented_installs(markdown) == [
        (f"{CONSTRAINED} pytest", []),
        (f"{CONSTRAINED} pillow", []),
    ]


@pytest.mark.parametrize(
    "step",
    [
        {"run": {f"{CONSTRAINED} pytest": None}},
        {"name": "Install", "run": {f"{CONSTRAINED} pytest": "x"}},
        {"uses": "actions/setup-python@v6", "with": {"cache": {"pip": None}}},
        {
            "uses": "actions/setup-python@v6",
            "with": {"cache-dependency-path": {"dev-constraints.txt": None}},
        },
    ],
)
def test_a_mapping_key_is_never_the_install_line_or_a_cache_input(step):
    """A key was read at its mapping's path, where `run: {"pip install ...": null}` is `run`."""
    names, offenders = _workflow_installs({"jobs": {"test": {"steps": [step]}}}, _shared_pins())
    assert names == set()
    assert len(offenders) == 1 and "outside an install step" in offenders[0], offenders


def test_the_workflow_guard_counts_what_the_script_files_name(monkeypatch):
    refusal = ".github/scripts/x.sh:1: names `pip`; installs belong in a workflow step"
    monkeypatch.setattr(sys.modules[__name__], "_unparsed_ci_file_offenders", lambda: [refusal])
    with pytest.raises(AssertionError, match=r"scripts/x\.sh:1"):
        test_every_workflow_install_takes_its_version_from_the_shared_pins()


def test_the_workflow_guard_holds_each_workflow_to_its_mirror(monkeypatch, tmp_path):
    (tmp_path / "test.yml").write_text("jobs: {}\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOWS", tmp_path)
    with pytest.raises(AssertionError, match="WORKFLOW_INSTALLS says"):
        test_every_workflow_install_takes_its_version_from_the_shared_pins()


def _action_offenders(workflow: dict) -> tuple[set[str], list[str]]:
    """``(third-party actions used, the ones not pinned to a commit)``.

    `actions/*` is GitHub's own and stays on its major tags; a local `./` path
    is this repository. Everything else runs someone else's code with this
    repository's token, and a tag there can be moved.
    """
    used: set[str] = set()
    offenders: list[str] = []
    for job in workflow["jobs"].values():
        for item in [job, *(job.get("steps") or [])]:
            uses = str(item.get("uses", ""))
            if not uses or uses.startswith(("./", "actions/")):
                continue
            used.add(uses.split("@", 1)[0])
            if not COMMIT_PINNED_ACTION.fullmatch(uses):
                offenders.append(uses)
    return used, offenders


def test_third_party_actions_are_pinned_to_a_commit():
    used: set[str] = set()
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        seen, wrong = _action_offenders(_yaml(path.name))
        used |= seen
        offenders += [f"{path.name}: {uses}" for uses in wrong]
    assert (
        used == THIRD_PARTY_ACTIONS
    ), f"third-party actions in use changed: {sorted(used ^ THIRD_PARTY_ACTIONS)}"
    assert not offenders, f"third-party actions on a tag that can be moved: {offenders}"


def test_the_action_pin_reader_tells_a_tag_from_a_commit():
    sha = "a" * 40
    steps = [
        {"uses": "actions/checkout@v4"},
        {"uses": "gradle/actions/setup-gradle@v4"},
        {"uses": f"gradle/actions/setup-gradle@{sha}"},
        {"uses": f"owner/action@{sha[:7]}"},
        {"run": "echo owner/action@v1"},
    ]
    workflow = {"jobs": {"a": {"steps": steps}, "b": {"uses": "owner/repo/.github/w.yml@main"}}}
    used, offenders = _action_offenders(workflow)
    assert used == {"gradle/actions/setup-gradle", "owner/action", "owner/repo/.github/w.yml"}
    assert offenders == [
        "gradle/actions/setup-gradle@v4",
        f"owner/action@{sha[:7]}",
        "owner/repo/.github/w.yml@main",
    ]
