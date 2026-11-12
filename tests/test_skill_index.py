"""SKILL.md is an index an agent loads whole; these guards keep it one.

Until v0.10.0 SKILL.md was the complete reference: 156,541 bytes, loaded on
every task, more than a six-screen walk's raw dumps cost. Now it holds what an
agent needs before it has chosen a script -- the loop, one entry per script with
every flag, the failure contract -- and each script's detail is a file under
`references/`, opened when needed.

A split like that fails quietly. A flag moves to a reference file and the agent,
which picks its flag from the file it has loaded, never sees it. A link is
retargeted and still resolves. The Quick Start block moves next door and the
guard that reads it reads nothing. So each rule here is a function over the
documents' TEXT (`skill_md`, and `references` as ``{file name: text}``), and
each has a mutation: the documents with one thing broken, in memory, which the
rule must refuse. A rule that passes its mutation is the failure.

The rules about an index entry are a whole-line allowlist, not a reading of
Markdown. An entry is its numbered line and the lines under it up to the next
blank one, and every one of them must be, whole, one of five shapes. Anything
else there is refused, so nothing in an entry is skipped because a parser did
not recognise it. What that leaves unchecked is stated where it applies.
"""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_packaging_contract import (
    _BACKTICKED,
    REFERENCES,
    SKILL_MD,
    SKILL_ROOT,
    _accepted_but_undocumented,
    _documented_options,
    _global_flags,
    _help_text,
    _option_tokens,
    _relative_links,
    _scripts_on_disk,
)
from test_quick_start_contract import (
    PROSE,
    QUICK_START_HEADING,
    SCRIPT_SUFFIXES,
    _command,
    _quick_start_block,
    code_lines,
    markdown_lines,
    options_in,
    section,
)

# 32 KiB: about 8.2k tokens by the bytes / 4 estimate. Bytes are what is
# measured; no tokenizer is a dependency of this suite.
SKILL_MD_LIMIT = 32_768

KNOWN_GAPS = "known-gaps.md"
LOOP_TESTS = Path(__file__).resolve().parent / "test_agent_loop_from_output.py"


def _documents() -> tuple[str, dict[str, str]]:
    """SKILL.md's text, and every reference file's by file name."""
    references = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(REFERENCES.glob("*.md"))
    }
    return SKILL_MD.read_text(encoding="utf-8"), references


def _reference_name(script: str) -> str:
    """`navigator.py` -> `navigator.md`: the file that holds a script's detail."""
    return Path(script).stem + ".md"


# ---------------------------------------------------------------------------
# 1. The budget.
# ---------------------------------------------------------------------------


def budget_problems(skill_md: str) -> list[str]:
    size = len(skill_md.encode("utf-8"))
    if size <= SKILL_MD_LIMIT:
        return []
    return [
        f"SKILL.md is {size:,} bytes, {size - SKILL_MD_LIMIT:,} over the {SKILL_MD_LIMIT:,} an "
        "agent is asked to load on every task. Move explanation to the script's reference "
        "file; flags stay here."
    ]


def test_skill_md_fits_in_what_an_agent_loads_on_every_task():
    skill_md, _ = _documents()
    assert not budget_problems(skill_md)
    assert len(SKILL_MD.read_bytes()) <= SKILL_MD_LIMIT, "the file on disk, byte for byte"


# ---------------------------------------------------------------------------
# The frontmatter: what a host reads to decide whether to load the skill.
# ---------------------------------------------------------------------------

FRONTMATTER_KEYS = {"name", "version", "description"}
# The longest description a skill's frontmatter may carry.
DESCRIPTION_LIMIT = 1024


def frontmatter_problems(skill_md: str) -> list[str]:
    """SKILL.md opens with YAML a YAML parser reads: three strings, one line each.

    Parsed, not matched. The version guards read `version:` with a pattern and
    were satisfied by a block whose `description` held `shell: build`, which
    is a second mapping inside a plain scalar: no parser accepts the block,
    so a host would have had no name or description to load the skill by.

    The block is three lines, one per key, in any order, and what the parser
    makes of each value is one line of text: a quoted `"a\\rb"` is one line of
    the file and two of the value, and a key written twice is one key to the
    parser, so both are checked on the file and on the parse.
    """
    import yaml

    if not skill_md.startswith("---\n") or "\n---\n" not in skill_md[4:]:
        return ["SKILL.md does not open with a `---` frontmatter block"]
    block = skill_md[4 : skill_md.index("\n---\n", 4)]
    written = [line.split(":", 1)[0] for line in block.split("\n")]
    if sorted(written) != sorted(FRONTMATTER_KEYS):
        return [
            f"SKILL.md's frontmatter is not one line for each of {sorted(FRONTMATTER_KEYS)}: "
            f"its lines start {written}"
        ]
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as error:
        return [f"SKILL.md's frontmatter is not YAML: {' '.join(str(error).split())}"]
    if not isinstance(data, dict) or set(data) != FRONTMATTER_KEYS:
        return [f"SKILL.md's frontmatter does not parse to its three keys: {data!r}"]
    problems = [
        f"SKILL.md's frontmatter `{key}` is not one line of text: {value!r}"
        for key, value in data.items()
        if not isinstance(value, str) or not value.strip() or len(value.splitlines()) != 1
    ]
    if isinstance(data["description"], str) and len(data["description"]) > DESCRIPTION_LIMIT:
        problems.append(
            f"SKILL.md's description is {len(data['description'])} characters; the limit is "
            f"{DESCRIPTION_LIMIT}"
        )
    return problems


def test_the_frontmatter_is_yaml_with_a_name_a_version_and_a_description():
    skill_md, _ = _documents()
    assert not frontmatter_problems(skill_md)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("driven from the shell, such as", "driven from the shell: such as"),
        ("version: 0.9.0\n", "version: 0.9\n"),
        ("name: android-emulator-skill\n", ""),
        ("description: Use when", "description:\n  - Use when"),
        ("description: Use when", "summary: x\ndescription: Use when"),
        ("description: Use when", 'description: "first\\rsecond" #'),
        ("description: Use when", 'description: "first\\u2028second" #'),
        ("version: 0.9.0\n", "version: 0.9.0\nname: other\n"),
    ],
    ids=[
        "a colon in the description",
        "a version yaml reads as a number",
        "no name",
        "a list",
        "a fourth key",
        "a quoted carriage return",
        "a quoted line separator",
        "a key written twice",
    ],
)
def test_the_frontmatter_guard_refuses_a_block_a_parser_reads_otherwise(old, new):
    skill_md, _ = _documents()
    assert frontmatter_problems(_once(skill_md, old, new))


# ---------------------------------------------------------------------------
# 2. The index: every script once, each entry whole.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One script's entry in SKILL.md's index."""

    number: int
    name: str
    purpose: str
    run: tuple[str, ...]  # the Run line's arguments, after the script
    options: frozenset[str]
    notes: tuple[str, ...]


_SCRIPTS_HEADING = re.compile(r"## (Scripts \(v\d+\.\d+\.\d+\))")
_ENTRY_HEAD = re.compile(r"(\d+)\. \*\*([A-Za-z_]\w*\.(?:py|sh))\*\* - (\S.*)")
_ENTRY_LIKE = re.compile(r"\s*\d+[.)]\s+\*\*[^*]*\.(?:py|sh)\*\*")
_ENTRY_LABEL = re.compile(r"\s*[-*+]\s+(?:Run|Options|Detail):")
_RUN = re.compile(r'    - Run: `(python3|bash) "\$SKILL_DIR/scripts/([\w.]+)"((?: [^`]+)?)`')
_OPTIONS = re.compile(r"    - Options: (\S.*)")
_DETAIL = re.compile(r"    - Detail: \[([^\]]*)\]\(([^)]*)\)")
_NOTE = re.compile(r"    - (\S.*)")
_RESERVED_LABEL = re.compile(r"\s*(?:[-*+]\s*)?(?:Run|Options|Detail)\s*:", re.IGNORECASE)
# What an argument of a documented command is made of. Nothing the shell
# expands or redirects: a command shown to an agent is one it can run as written.
_PLAIN_ARGUMENTS = re.compile(r"[A-Za-z0-9 \"'._,/=:@+-]*")
_INTERPRETER = {".py": "python3", ".sh": "bash"}


def _flags_in(tokens: list[str] | tuple[str, ...]) -> set[str]:
    """The option tokens of an argument list: `--tap`, `-s`, the flag of `--x=1`.

    Every dash-led word that is not a number, whatever follows the dashes: a
    word this did not take for an option (`--1`) would be a word nothing
    checks, and argparse refuses it.
    """
    return set(options_in(tokens))


_SPELLING = re.compile(r"--?[A-Za-z][\w-]*")


def _declared_options(body: str, where: str) -> tuple[frozenset[str], list[str]]:
    """The flags an Options bullet declares, and any word in it that is not one.

    Inside each backticked span, every word that starts with a dash is a flag
    and must be one whole: `--tap.now` declares no flag argparse has, and
    reading the first five characters of it would pass it. Words are split
    at spaces and square brackets (`--list [--limit N]`); a metavar or a
    choice list (`{down,up}`) starts with no dash and is not read.
    """
    flags: set[str] = set()
    problems: list[str] = []
    for chunk in _BACKTICKED.findall(body):
        for word in re.split(r"[\s\[\]]+", chunk):
            if not word.startswith("-"):
                continue
            if _SPELLING.fullmatch(word):
                flags.add(word)
            else:
                problems.append(f"{where}: `{word}` in its Options is not a flag's spelling")
    return frozenset(flags), problems


def _arguments(text: str, where: str) -> tuple[list[str], list[str]]:
    """A documented command's arguments as words, or why they cannot be read."""
    if not _PLAIN_ARGUMENTS.fullmatch(text):
        strange = sorted(set(re.sub(_PLAIN_ARGUMENTS.pattern[:-1], "", text)))
        return [], [
            f"{where}: the arguments hold {strange}, which this guard does not read (a "
            "variable, a pipe, a substitution, a comment): show one script run as written"
        ]
    try:
        return shlex.split(text), []
    except ValueError as error:
        return [], [f"{where}: the arguments do not parse as shell words ({error})"]


def read_index(skill_md: str) -> tuple[list[Entry], list[str]]:
    """The entries of SKILL.md's index, and everything about them that is off.

    An entry starts at a line that is, from its first column, ``N. **name** -
    purpose`` and runs to the next blank line. Each line under it must be, whole:

    * ``    - Run: `python3 "$SKILL_DIR/scripts/<name>" <arguments>` `` (``bash``
      for a shell script), exactly once, naming the entry's own script;
    * ``    - Options: ...``, exactly once, on one line;
    * ``    - Detail: [references/<stem>.md](references/<stem>.md)``, exactly
      once, both halves the entry's own file;
    * ``    - <a note>``, any number, with no link in it.

    A numbered bold script name, or a ``Run:`` / ``Options:`` / ``Detail:``
    bullet, anywhere else in the file is refused: an entry is not half-written
    somewhere this reader does not look.

    Limit, stated: what a note or a purpose SAYS is not checked. A note may
    name a flag that does not exist ("there is no ``--text``"), which is why
    flags are read from the Options bullet and the Run line alone.
    """
    problems: list[str] = []
    lines = [line for line, _ in markdown_lines(skill_md)]
    kinds = [kind for _, kind in markdown_lines(skill_md)]

    headings = [
        m.group(1) for line in prose_only(skill_md) if (m := _SCRIPTS_HEADING.fullmatch(line))
    ]
    if len(headings) != 1:
        return [], [f"expected one `## Scripts (vX.Y.Z)` heading, found {len(headings)}"]
    start = next(
        i for i, line in enumerate(lines) if kinds[i] == PROSE and line == f"## {headings[0]}"
    )
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if kinds[i] == PROSE and lines[i].startswith("## ")
        ),
        len(lines),
    )

    entries: list[Entry] = []
    inside: set[int] = set()
    index = start + 1
    while index < end:
        head = _ENTRY_HEAD.fullmatch(lines[index]) if kinds[index] == PROSE else None
        if not head:
            index += 1
            continue
        number, name, purpose = int(head.group(1)), head.group(2), head.group(3)
        where = f"entry {number} ({name})"
        inside.add(index)
        runs: list[tuple[str, ...]] = []
        options: list[frozenset[str]] = []
        details: list[tuple[str, str]] = []
        notes: list[str] = []
        index += 1
        while index < end and lines[index].strip():
            line = lines[index]
            inside.add(index)
            index += 1
            if kinds[index - 1] != PROSE:
                problems.append(f"{where}: a fenced block inside an entry: {line.strip()!r}")
            elif run := _RUN.fullmatch(line):
                interpreter, script, arguments = run.groups()
                if script != name:
                    problems.append(f"{where}: its Run line runs {script}")
                if interpreter != _INTERPRETER[Path(name).suffix]:
                    problems.append(f"{where}: its Run line starts {interpreter}")
                words, unread = _arguments(arguments.strip(), where)
                problems.extend(unread)
                runs.append(tuple(words))
            elif declared := _OPTIONS.fullmatch(line):
                flags, misspelt = _declared_options(declared.group(1), where)
                problems.extend(misspelt)
                options.append(flags)
            elif detail := _DETAIL.fullmatch(line):
                details.append((detail.group(1), detail.group(2)))
            elif _RESERVED_LABEL.match(line):
                # Before the note: a second `- Run:` written another way is
                # not a note, and read as one it would leave "exactly one"
                # true of an entry that shows two commands.
                problems.append(
                    f"{where}: a Run, Options or Detail line not written the one way: "
                    f"{line.strip()!r}"
                )
            elif (note := _NOTE.fullmatch(line)) and "](" not in line:
                notes.append(note.group(1))
            else:
                problems.append(
                    f"{where}: a line that is none of Run, Options, Detail or a one-line "
                    f"note without a link: {line.strip()!r}"
                )

        for label, found in (("Run", runs), ("Options", options), ("Detail", details)):
            if len(found) != 1:
                problems.append(f"{where}: {len(found)} `- {label}:` lines; an entry has one")
        target = f"references/{_reference_name(name)}"
        if details and details[0] != (target, target):
            problems.append(f"{where}: its Detail link is {details[0]}, not [{target}]({target})")
        if options and not options[0]:
            problems.append(f"{where}: its Options bullet names no flag in backticks")
        entries.append(
            Entry(
                number=number,
                name=name,
                purpose=purpose,
                run=runs[0] if runs else (),
                options=options[0] if options else frozenset(),
                notes=tuple(notes),
            )
        )

    for position, line in enumerate(lines):
        if position in inside or kinds[position] != PROSE:
            continue
        if _ENTRY_LIKE.match(line):
            problems.append(f"line {position + 1}: a numbered script outside the index: {line!r}")
        if _ENTRY_LABEL.match(line):
            problems.append(f"line {position + 1}: an entry's bullet outside an entry: {line!r}")

    numbers = [entry.number for entry in entries]
    if numbers != list(range(1, len(entries) + 1)):
        problems.append(f"the entries are not numbered 1 to {len(entries)} in order: {numbers}")
    return entries, problems


def prose_only(markdown: str) -> list[str]:
    return [line for line, kind in markdown_lines(markdown) if kind == PROSE]


def _accepted(script: str, entries: dict[str, Entry]) -> set[str]:
    """Every flag a script takes.

    A Python script's are argparse's own, from its ``--help``. The shell health
    check has no argparse to ask, so its index entry is its interface: what the
    Options bullet names is what it takes.
    """
    if script.endswith(".py"):
        return _option_tokens(_help_text(str(SKILL_ROOT / "scripts" / script)))
    return set(entries[script].options) if script in entries else set()


def index_problems(skill_md: str, references: dict[str, str], scripts: list[str]) -> list[str]:
    """Every script on disk has one entry, whole, linking a file titled with its name."""
    entries, problems = read_index(skill_md)
    names = [entry.name for entry in entries]
    for script in scripts:
        if names.count(script) != 1:
            problems.append(f"{script} has {names.count(script)} entries in the index, not one")
    for name in sorted(set(names) - set(scripts)):
        problems.append(f"the index has an entry for {name}, which is not a script on disk")

    by_name = {entry.name: entry for entry in entries}
    global_flags = _global_flags(skill_md)
    for entry in entries:
        where = f"entry {entry.number} ({entry.name})"
        reference = references.get(_reference_name(entry.name))
        if reference is None:
            problems.append(f"{where}: references/{_reference_name(entry.name)} does not exist")
        elif reference.split("\n", 1)[0] != f"# {entry.name}":
            problems.append(
                f"{where}: references/{_reference_name(entry.name)} is titled "
                f"{reference.split(chr(10), 1)[0]!r}, not '# {entry.name}'"
            )
        undeclared = _flags_in(entry.run) - entry.options - global_flags
        if undeclared:
            problems.append(
                f"{where}: its Run line uses {sorted(undeclared)}, which its Options "
                "bullet does not name"
            )
        if entry.name in scripts:
            unaccepted = _flags_in(entry.run) - _accepted(entry.name, by_name)
            if unaccepted:
                problems.append(f"{where}: its Run line uses {sorted(unaccepted)}, not accepted")
    return problems


def test_every_script_has_one_whole_entry_linking_its_own_reference_file():
    skill_md, references = _documents()
    assert not index_problems(skill_md, references, _scripts_on_disk())


def test_the_index_reader_and_the_flag_guards_read_the_same_options():
    """Two readers of one bullet must not disagree about what it says.

    `_documented_options` (the forward and reverse flag guards) follows a
    wrapped bullet; this module's reader takes one line and refuses a wrap. On
    the file as it stands they return the same flags for every script.

    They did not, the first time this ran: `_documented_options` knew Python
    entries only, so the shell health check's bullet was read as a second
    bullet of the entry above it, and `push_notification.py` was credited
    with a documented `--help`.
    """
    skill_md, _ = _documents()
    entries, problems = read_index(skill_md)
    assert not problems
    assert {entry.name: set(entry.options) for entry in entries} == _documented_options(skill_md)


def test_the_index_reader_returns_the_entries_the_file_holds():
    """Anti-vacuity: 33 entries, the health check's flags, a Run line's words."""
    skill_md, _ = _documents()
    entries, _ = read_index(skill_md)
    by_name = {entry.name: entry for entry in entries}

    assert sorted(by_name) == _scripts_on_disk()
    assert by_name["android_health_check.sh"].options == {"--json", "--help"}
    assert by_name["navigator.py"].run == ("--find-text", "Login", "--tap")
    assert by_name["screen_mapper.py"].run == ()
    assert {"--find-text", "--tap", "--scroll-to-find"} <= by_name["navigator.py"].options
    assert all(entry.purpose for entry in entries)


# ---------------------------------------------------------------------------
# 3. No reference file is orphaned, and none is missing.
# ---------------------------------------------------------------------------


def orphan_problems(skill_md: str, references: dict[str, str]) -> list[str]:
    """`references/` holds one file per index entry, and the known-gaps file.

    Each script's file is linked by its own entry's Detail line and by no
    other entry (`read_index` holds a Detail line to the entry's own file), so
    "linked from exactly one index entry" is: the files are the entries' files.
    """
    entries, _ = read_index(skill_md)
    expected = {_reference_name(entry.name) for entry in entries} | {KNOWN_GAPS}
    problems = [
        f"references/{name} is linked from no index entry"
        for name in sorted(set(references) - expected)
    ]
    problems += [f"references/{name} is missing" for name in sorted(expected - set(references))]
    if f"references/{KNOWN_GAPS}" not in _relative_links(skill_md):
        problems.append(f"SKILL.md does not link references/{KNOWN_GAPS}")
    stems = [Path(entry.name).stem for entry in entries]
    for stem in sorted({stem for stem in stems if stems.count(stem) > 1}):
        problems.append(f"two scripts share the reference file {stem}.md")
    return problems


def test_no_reference_file_is_orphaned_and_none_is_missing():
    skill_md, references = _documents()
    assert not orphan_problems(skill_md, references)
    assert sorted(path.name for path in REFERENCES.iterdir()) == sorted(references)


# ---------------------------------------------------------------------------
# 4. Quick Start stays in the main file, and its flags are documented there.
# ---------------------------------------------------------------------------


def _undocumented_flags(
    commands: list[tuple[str, frozenset[str]]], skill_md: str, what: str
) -> list[str]:
    """Flags a section of SKILL.md uses that the index does not document.

    A flag is documented for a script when that script's Options bullet names
    it, or when the "All scripts support" sentence does and the script accepts
    it. The options mapping the flag guards use reads Python entries only;
    this one reads every entry, so the shell health check's counts.
    """
    entries = {entry.name: entry for entry in read_index(skill_md)[0]}
    everywhere = _global_flags(skill_md)
    problems = []
    for script, flags in commands:
        if script not in entries:
            problems.append(f"{what} runs {script}, which has no index entry")
            continue
        documented = set(entries[script].options) | (everywhere & _accepted(script, entries))
        if missing := sorted(flags - documented):
            problems.append(
                f"{what} runs {script} with {missing}, which its index entry does not document"
            )
    return problems


def _commands_in(lines: list[str]) -> list[tuple[str, frozenset[str]]]:
    return [command for command in map(_command, lines) if command is not None]


_SAYS_QUICK_START = re.compile(r"[\s>]*#*\s*quick\s+start\s*#*\s*", re.IGNORECASE)


def _says_quick_start(markdown: str) -> list[str]:
    """Every line outside a fenced block whose letters are "Quick Start" and no others.

    Read with everything but letters and digits taken out, in any case: heading
    marks before or after, indentation, a blockquote mark, emphasis, inline
    code. A heading is not recognised by its syntax here -- one space of
    indentation, closing `#`s, an underline on the next line and `**bold**`
    all make a heading that says Quick Start, and a reader of those is a
    Markdown parser -- so the line is found by what it says.

    This checks how a title is written, not how a page renders. Limits, stated:
    a title with other words in it ("Quick start guide") is another title; and
    one dressed in a link or an HTML tag carries the letters of its target or
    tag, and is not seen.
    """
    return [
        line
        for line in prose_only(markdown)
        if "".join(character for character in line.lower() if character.isalnum()) == "quickstart"
    ]


def quick_start_problems(skill_md: str, others: dict[str, str]) -> list[str]:
    """Quick Start is in SKILL.md, once, nowhere else, and its flags are in the index.

    ``others`` is every other Markdown document the skill ships, by name: the
    reference files, and the README beside SKILL.md. The Quick Start contract
    (`test_quick_start_contract`) reads SKILL.md's block and holds the e2e test
    to it; a second block in another file would be one no test runs.
    """
    problems = [
        f"{name} has a line that says only 'Quick Start' ({lines[0]!r}); there is one "
        "Quick Start, in SKILL.md"
        for name, text in sorted(others.items())
        if (lines := _says_quick_start(text))
    ]
    if _says_quick_start(skill_md) != [QUICK_START_HEADING]:
        problems.append(
            f"SKILL.md has {_says_quick_start(skill_md)} where one `{QUICK_START_HEADING}` "
            "line, from its first column, is expected"
        )
    try:
        commands = _commands_in(_quick_start_block(skill_md))
    except AssertionError as error:
        return [*problems, f"SKILL.md: {error}"]
    if not commands:
        problems.append("SKILL.md's Quick Start runs no script")
    return problems + _undocumented_flags(commands, skill_md, "Quick Start")


def _other_skill_documents() -> dict[str, str]:
    """Every Markdown file under the skill directory but SKILL.md, by relative path."""
    return {
        path.relative_to(SKILL_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SKILL_ROOT.rglob("*.md"))
        if path != SKILL_MD
    }


def test_quick_start_is_in_the_main_file_and_its_flags_are_documented_inline():
    skill_md, references = _documents()
    others = _other_skill_documents()

    assert {f"references/{name}" for name in references} < set(others)
    assert "README.md" in others
    assert not quick_start_problems(skill_md, others)


# ---------------------------------------------------------------------------
# 5. The loop: both handoffs, as commands, each run by a loop test.
# ---------------------------------------------------------------------------

LOOP_SECTION = "The loop"

# The two handoffs, each as the command that prints a value and the command
# that takes it: (script, flags it must carry). A name `screen_mapper` printed
# goes to `navigator --find-text ... --tap`; a build id `build_and_test`
# printed goes to `--get-errors`.
HANDOFFS = {
    "a printed name into navigator": (
        ("screen_mapper.py", frozenset()),
        ("navigator.py", frozenset({"--find-text", "--tap"})),
    ),
    "a printed build id into --get-errors": (
        ("build_and_test.py", frozenset({"--project"})),
        ("build_and_test.py", frozenset({"--get-errors"})),
    ),
}

# Every script invocation `test_agent_loop_from_output.py` makes with literal
# arguments, complete. A collector that stopped seeing one would make the loop
# section look uncovered, or, worse, covered by nothing; so it is held to this.
LOOP_TEST_BASELINE = (
    ("build_and_test.py", ("--get-errors",)),
    ("build_and_test.py", ("--get-errors",)),
    ("build_and_test.py", ("--project",)),
    ("navigator.py", ()),
    ("navigator.py", ("--find-text", "--tap")),
    ("screen_mapper.py", ()),
    ("screen_mapper.py", ()),
    ("screen_mapper.py", ("--json",)),
)


def loop_commands(skill_md: str) -> list[tuple[str, frozenset[str]]]:
    """Every command the loop section's fenced blocks show, in order."""
    return _commands_in(code_lines(section(skill_md, LOOP_SECTION)))


def loop_problems(skill_md: str) -> list[str]:
    """The loop section shows both handoffs as commands, with documented flags.

    A handoff is shown when its two commands follow one another, each with at
    least the flags it must carry. More flags are allowed -- a serial, say --
    and are then held to the loop tests like every other flag the section
    shows (`loop_coverage_problems`).
    """
    try:
        commands = loop_commands(skill_md)
    except AssertionError as error:
        return [f"SKILL.md: {error}"]
    problems = []
    for name, (first, second) in HANDOFFS.items():
        shown = any(
            commands[i][0] == first[0]
            and first[1] <= commands[i][1]
            and commands[i + 1][0] == second[0]
            and second[1] <= commands[i + 1][1]
            for i in range(len(commands) - 1)
        )
        if not shown:
            problems.append(
                f"the loop section does not show {name}: {first[0]} followed by "
                f"{second[0]} {' '.join(sorted(second[1]))}"
            )
    return problems + _undocumented_flags(commands, skill_md, "the loop section")


def loop_coverage_problems(
    skill_md: str, invocations: list[tuple[str, frozenset[str]]]
) -> list[str]:
    """Every command the loop section shows is run by a loop test.

    Same script, with at least the flags the section shows -- the rule Quick
    Start is held to against the e2e test. The loop tests seed each second
    command from what the first one PRINTED; that is theirs to assert, and
    what this checks is that they run what SKILL.md tells the agent to run.
    """
    return [
        f"the loop section shows `{' '.join([script, *sorted(flags)])}` and "
        f"{LOOP_TESTS.name} never runs it"
        for script, flags in loop_commands(skill_md)
        if not any(
            script == called and flags <= called_flags for called, called_flags in invocations
        )
    ]


def loop_test_invocations() -> list[tuple[str, frozenset[str]]]:
    """Every script the loop tests run, as ``(script, literal flags)``.

    Read from the test module's syntax tree: a call whose arguments, or whose
    list or tuple argument's elements, include a string literal ending in a
    script suffix. Only literals count; a flag computed at run time is not
    evidence that the documented one is exercised.
    """
    tree = ast.parse(LOOP_TESTS.read_text(encoding="utf-8"), filename=str(LOOP_TESTS))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        words: list[str] = []
        for argument in node.args:
            elements = argument.elts if isinstance(argument, ast.List | ast.Tuple) else [argument]
            words += [
                element.value
                for element in elements
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
        script = next((word for word in words if word.endswith(SCRIPT_SUFFIXES)), None)
        if script is not None:
            calls.append((script, options_in(words)))
    return calls


def _normalise(commands) -> list[tuple[str, tuple[str, ...]]]:
    return sorted((script, tuple(sorted(flags))) for script, flags in commands)


def test_the_loop_section_shows_both_handoffs_with_documented_flags():
    skill_md, _ = _documents()
    assert not loop_problems(skill_md)


def test_every_command_the_loop_section_shows_is_run_by_a_loop_test():
    """The documented loop and the tested loop are the same commands."""
    skill_md, _ = _documents()
    assert not loop_coverage_problems(skill_md, loop_test_invocations())


def test_a_loop_command_no_loop_test_runs_is_reported_and_one_it_runs_is_not():
    """The coverage rule itself, on a loop section that gained a short option.

    `screen_mapper.py -s emulator-5554` still shows the handoff, so the
    section's own rule passes it; no loop test passes `-s`, so coverage must
    not. With a test that does, it must.
    """
    skill_md, _ = _mutated(_map_the_screen_with_a_short_option)
    invocations = loop_test_invocations()
    with_one_that_does = [*invocations, ("screen_mapper.py", frozenset({"-s"}))]

    assert not loop_problems(skill_md)
    assert len(loop_coverage_problems(skill_md, invocations)) == 1
    assert not loop_coverage_problems(skill_md, with_one_that_does)


def test_the_loop_test_collector_returns_exactly_the_calls_in_the_module():
    assert _normalise(loop_test_invocations()) == list(LOOP_TEST_BASELINE)


def test_the_loop_section_extractor_returns_exactly_the_documented_commands():
    skill_md, _ = _documents()
    assert _normalise(loop_commands(skill_md)) == [
        ("build_and_test.py", ("--get-errors",)),
        ("build_and_test.py", ("--project",)),
        ("navigator.py", ("--find-text", "--tap")),
        ("screen_mapper.py", ()),
        ("screen_mapper.py", ()),
    ]


# ---------------------------------------------------------------------------
# The forward flag guard, extended: examples anywhere, declarations in a
# reference file.
# ---------------------------------------------------------------------------

_EXAMPLE = re.compile(r'(python3|bash) "\$SKILL_DIR/scripts/([\w.]+)"((?: .*)?)')


def _names_a_script(line: str, scripts: set[str]) -> bool:
    """Whether a fenced line mentions the scripts directory or any script's file name.

    Asked this way so the one form below cannot be stepped around by naming a
    script differently: `cd "$SKILL_DIR/scripts" && python3 navigator.py` names
    one, and so is refused rather than left unread.
    """
    return "scripts/" in line or any(
        re.search(rf"(?<![\w.-]){re.escape(script)}(?![\w-])", line) for script in scripts
    )


def _routes() -> dict[str, dict[str, str]]:
    """{router: {verb: the script it hands the rest of the line to}}."""
    import avd
    import logs

    return {
        "avd.py": {route.name: route.script for route in avd.ROUTES},
        "logs.py": {route.name: route.script for route in logs.ROUTES},
    }


def _logical_lines(lines: list[str]) -> list[str]:
    """Code lines with a trailing-backslash continuation joined to its next line.

    A line continues only when the backslash is its last character: a
    backslash with a space after it escapes the space, and the next line is a
    command of its own. A comment line is taken first and never continues:
    the shell ends a comment at its newline whatever its last character.
    """
    joined: list[str] = []
    pending = ""
    for line in lines:
        if not pending and line.strip().startswith("#"):
            joined.append(line.strip())
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        joined.append(pending + line.strip())
        pending = ""
    if pending:
        joined.append(pending.strip() + " \\")
    return joined


def example_problems(name: str, markdown: str, skill_md: str) -> list[str]:
    """Every fenced line that runs a script runs it one way, with flags it accepts.

    A fenced line that mentions ``scripts/`` or any script's file name must be,
    whole, ``python3 "$SKILL_DIR/scripts/<file>" <arguments>`` (``bash`` for a
    shell script),
    its arguments plain words; a line ending in a backslash continues on the
    next. Each flag is checked against the script the LINE names -- an example
    in one script's file may show another -- and, after a router's verb
    (``avd.py start --avd ...``), against the script that verb hands it to.

    Limits, stated: only fenced lines are read. Prose names flags that do not
    exist on purpose ("there is no ``--list``"), so a flag in a sentence or in
    inline code is not a claim this guard can hold anyone to; a line that
    captures or pipes a script's output is refused, not interpreted; and a
    line that runs a script without naming its file (through a variable that
    holds the whole path) is not seen at all.
    """
    entries = {entry.name: entry for entry in read_index(skill_md)[0]}
    scripts = set(_scripts_on_disk())
    routes = _routes()
    problems = []
    for line in _logical_lines(code_lines(markdown)):
        if line.startswith("#") or not _names_a_script(line, scripts):
            continue
        where = f"{name}: `{line}`"
        example = _EXAMPLE.fullmatch(line)
        if not example:
            problems.append(
                f'{where} is not `python3 "$SKILL_DIR/scripts/<file>" <arguments>` and '
                "nothing else on the line"
            )
            continue
        interpreter, script, rest = example.groups()
        if script not in scripts:
            problems.append(f"{where} runs a script that is not on disk")
            continue
        if interpreter != _INTERPRETER[Path(script).suffix]:
            problems.append(f"{where} starts {interpreter}")
        words, unread = _arguments(rest.strip(), where)
        problems.extend(unread)
        target = script
        if script in routes and words and words[0] in routes[script]:
            target, words = routes[script][words[0]], words[1:]
        if unaccepted := sorted(_flags_in(words) - _accepted(target, entries)):
            problems.append(f"{where} passes {unaccepted}, which {target} does not accept")
    return problems


def reference_options_problems(name: str, markdown: str) -> list[str]:
    """No reference file declares options: the label `Options:` appears nowhere in one.

    The index has every flag, and a flag declared in a reference file does not
    count as documented (`test_a_flag_documented_only_in_a_reference_is_undocumented`).
    A second declaration could only repeat the index or disagree with it, so
    none is allowed, and that is asked of the whole text -- any bullet marker,
    a blockquote, a fenced block, any case -- instead of finding where a
    declaration starts. `JAVA_TOOL_OPTIONS: ...` is not the label: the word is
    part of a longer name.
    """
    found = [
        line.strip()
        for line in markdown.split("\n")
        if re.search(r"(?<!\w)options\s*:", line, re.IGNORECASE)
    ]
    return [
        f"references/{name} has an `Options:` label ({line!r}); a script's flags are "
        "declared in its SKILL.md entry and nowhere else"
        for line in found
    ]


def _shipped() -> dict[str, str]:
    skill_md, references = _documents()
    return {
        "SKILL.md": skill_md,
        **{f"references/{name}": text for name, text in references.items()},
    }


@pytest.mark.parametrize("name", sorted(_shipped()))
def test_every_fenced_example_runs_a_script_the_one_way_with_flags_it_accepts(name):
    documents = _shipped()
    assert not example_problems(name, documents[name], documents["SKILL.md"])


@pytest.mark.parametrize("name", sorted(_documents()[1]))
def test_no_reference_file_declares_options(name):
    assert not reference_options_problems(name, _documents()[1][name])


def test_the_example_reader_reads_the_examples_the_documents_hold():
    """Anti-vacuity: it sees Quick Start, the loop, and a continued line."""
    documents = _shipped()
    seen = {
        name: [line for line in _logical_lines(code_lines(text)) if _EXAMPLE.fullmatch(line)]
        for name, text in documents.items()
    }
    assert len(seen["SKILL.md"]) >= 12
    assert any(
        "--step" in line and "--assert" in line for line in seen["references/test_recorder.md"]
    ), "the recorder's continued example is not read as one line"
    assert sum(len(lines) for lines in seen.values()) >= 25


def test_a_router_example_is_checked_against_the_script_its_verb_names():
    skill_md, _ = _documents()
    good = '```bash\npython3 "$SKILL_DIR/scripts/avd.py" start --avd Pixel_9 --wait-ready\n```\n'
    wrong = '```bash\npython3 "$SKILL_DIR/scripts/avd.py" start --find-text Login\n```\n'
    bare = '```bash\npython3 "$SKILL_DIR/scripts/avd.py" list --json\n```\n'

    assert not example_problems("x", good, skill_md)
    assert not example_problems("x", bare, skill_md)
    assert "emulator_boot.py does not accept" in example_problems("x", wrong, skill_md)[0]


# ---------------------------------------------------------------------------
# Mutation controls: each rule refuses the documents with one thing broken.
# ---------------------------------------------------------------------------


def _once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times; the mutation is stale"
    return text.replace(old, new)


_NAVIGATOR_DETAIL = "    - Detail: [references/navigator.md](references/navigator.md)\n"
_QUICK_START_EXAMPLE = (
    '```bash\npython3 "$SKILL_DIR/scripts/screen_mapper.py" --no-such-flag\n```\n'
)


def _delete_an_index_link(skill_md, references):
    return _once(skill_md, _NAVIGATOR_DETAIL, ""), references


def _point_a_link_at_another_scripts_file(skill_md, references):
    other = "    - Detail: [references/gesture.md](references/gesture.md)\n"
    return skill_md.replace(_NAVIGATOR_DETAIL, other), references


def _link_text_and_target_disagree(skill_md, references):
    other = "    - Detail: [references/navigator.md](references/gesture.md)\n"
    return _once(skill_md, _NAVIGATOR_DETAIL, other), references


def _retitle_a_reference_file(skill_md, references):
    retitled = _once(references["navigator.md"], "# navigator.py\n", "# gesture.py\n")
    return skill_md, {**references, "navigator.md": retitled}


def _drop_a_script_from_the_index(skill_md, references):
    entries = skill_md.split("\n\n")
    kept = [block for block in entries if "**sms.py**" not in block.split("\n", 1)[0]]
    assert len(kept) == len(entries) - 1
    return "\n\n".join(kept), references


def _list_a_script_twice(skill_md, references):
    blocks = skill_md.split("\n\n")
    (sms,) = [block for block in blocks if "**sms.py**" in block.split("\n", 1)[0]]
    return skill_md.replace(sms, sms + "\n\n" + sms), references


def _wrap_an_entry_line(skill_md, references):
    return _once(skill_md, "`--avd`, `--all`,", "`--avd`,\n      `--all`,"), references


def _run_another_script(skill_md, references):
    old = '    - Run: `python3 "$SKILL_DIR/scripts/gesture.py" --scroll down`'
    new = '    - Run: `python3 "$SKILL_DIR/scripts/keyboard.py" --scroll down`'
    return _once(skill_md, old, new), references


def _run_with_a_flag_the_entry_does_not_document(skill_md, references):
    old = '    - Run: `python3 "$SKILL_DIR/scripts/gesture.py" --scroll down`'
    return _once(skill_md, old, old[:-1] + " --serial-number x`"), references


def _run_through_a_pipe(skill_md, references):
    old = '    - Run: `python3 "$SKILL_DIR/scripts/device_list.py"`'
    return _once(skill_md, old, old[:-1] + " | head -1`"), references


def _an_entry_outside_the_index(skill_md, references):
    stray = "\n40. **navigator.py** - also here\n    - Options: `--no-such-flag`\n"
    return _once(skill_md, "## Requirements\n", f"## Requirements\n{stray}"), references


def _a_second_run_line_written_another_way(skill_md, references):
    old = '    - Run: `python3 "$SKILL_DIR/scripts/gesture.py" --scroll down`\n'
    return (
        _once(skill_md, old, old + "    - Run: python3 scripts/keyboard.py --teleport\n"),
        references,
    )


def _an_options_line_under_another_label_spelling(skill_md, references):
    old = '    - Run: `python3 "$SKILL_DIR/scripts/gesture.py" --scroll down`\n'
    return _once(skill_md, old, old + "    - options : `--teleport`\n"), references


def _a_flag_spelt_with_more_after_it(skill_md, references):
    return _once(skill_md, "`--index`, `--tap`,", "`--index`, `--tap.now`,"), references


INDEX_MUTATIONS = [
    _a_second_run_line_written_another_way,
    _an_options_line_under_another_label_spelling,
    _a_flag_spelt_with_more_after_it,
    _delete_an_index_link,
    _point_a_link_at_another_scripts_file,
    _link_text_and_target_disagree,
    _retitle_a_reference_file,
    _drop_a_script_from_the_index,
    _list_a_script_twice,
    _wrap_an_entry_line,
    _run_another_script,
    _run_with_a_flag_the_entry_does_not_document,
    _run_through_a_pipe,
    _an_entry_outside_the_index,
]


def _mutated(mutation) -> tuple[str, dict[str, str]]:
    skill_md, references = _documents()
    changed, changed_references = mutation(skill_md, dict(references))
    assert (changed, changed_references) != (skill_md, references), "the mutation changed nothing"
    return changed, changed_references


@pytest.mark.parametrize("mutation", INDEX_MUTATIONS, ids=lambda m: m.__name__)
def test_the_index_guard_refuses_an_index_with_one_thing_broken(mutation):
    skill_md, references = _mutated(mutation)
    assert index_problems(skill_md, references, _scripts_on_disk())


def _add_a_reference_nothing_links(skill_md, references):
    return skill_md, {**references, "notes.md": "# notes\n"}


def _delete_a_reference_file(skill_md, references):
    del references["navigator.md"]
    return skill_md, references


def _drop_the_known_gaps_link(skill_md, references):
    link = "[references/known-gaps.md](references/known-gaps.md)"
    assert link in skill_md
    return skill_md.replace(link, "`references/known-gaps.md`"), references


@pytest.mark.parametrize(
    "mutation",
    [_add_a_reference_nothing_links, _delete_a_reference_file, _drop_the_known_gaps_link],
    ids=lambda m: m.__name__,
)
def test_the_orphan_guard_refuses_a_file_too_many_or_too_few(mutation):
    skill_md, references = _mutated(mutation)
    assert orphan_problems(skill_md, references)


def _remove_a_quick_start_flag_from_its_options_bullet(skill_md, references):
    return (
        _once(skill_md, "`--index`, `--tap`, `--enter-text`,", "`--index`, `--enter-text`,"),
        references,
    )


def _remove_the_health_checks_flag_from_its_options_bullet(skill_md, references):
    return (
        _once(
            skill_md,
            "    - Options: `--json`, `--help`\n    - Detail: [references/android_health_check.md]",
            "    - Options: `--help`\n    - Detail: [references/android_health_check.md]",
        ),
        references,
    )


def _move_the_quick_start_block_into_a_reference_file(skill_md, references):
    start = skill_md.index(f"{QUICK_START_HEADING}\n")
    end = skill_md.index("## The loop\n")
    block = skill_md[start:end]
    moved = references["navigator.md"] + "\n" + block
    return skill_md[:start] + skill_md[end:], {**references, "navigator.md": moved}


def _copy_the_quick_start_block_into_a_reference_file(skill_md, references):
    start = skill_md.index(f"{QUICK_START_HEADING}\n")
    end = skill_md.index("## The loop\n")
    copied = references["navigator.md"] + "\n" + skill_md[start:end]
    return skill_md, {**references, "navigator.md": copied}


def _quick_start_twice_in_the_main_file(skill_md, references):
    start = skill_md.index(f"{QUICK_START_HEADING}\n")
    end = skill_md.index("## The loop\n")
    return skill_md[:end] + skill_md[start:end] + skill_md[end:], references


def _a_second_quick_start_heading_indented_one_space(skill_md, references):
    block = '\n ## Quick Start\n\n```bash\npython3 "$SKILL_DIR/scripts/screen_mapper.py"\n```\n'
    return skill_md + block, references


def _a_second_quick_start_heading_underlined(skill_md, references):
    return skill_md + "\nQuick Start\n-----------\n\nRun the mapper.\n", references


def _a_second_quick_start_heading_in_bold(skill_md, references):
    return skill_md + "\n## **Quick Start**\n\nRun the mapper.\n", references


def _a_second_quick_start_heading_in_a_blockquote(skill_md, references):
    return skill_md + "\n> ## `Quick Start`\n", references


def _a_quick_start_heading_of_another_level_in_a_reference(skill_md, references):
    added = references["navigator.md"] + "\n### Quick start ###\n\nRun the mapper.\n"
    return skill_md, {**references, "navigator.md": added}


@pytest.mark.parametrize(
    "mutation",
    [
        _a_second_quick_start_heading_indented_one_space,
        _a_second_quick_start_heading_underlined,
        _a_second_quick_start_heading_in_bold,
        _a_second_quick_start_heading_in_a_blockquote,
        _a_quick_start_heading_of_another_level_in_a_reference,
        _remove_a_quick_start_flag_from_its_options_bullet,
        _remove_the_health_checks_flag_from_its_options_bullet,
        _move_the_quick_start_block_into_a_reference_file,
        _copy_the_quick_start_block_into_a_reference_file,
        _quick_start_twice_in_the_main_file,
    ],
    ids=lambda m: m.__name__,
)
def test_the_quick_start_guard_refuses_a_block_moved_or_a_flag_undocumented(mutation):
    skill_md, references = _mutated(mutation)
    assert quick_start_problems(skill_md, references)


def test_a_flag_the_sentence_promises_counts_only_for_a_script_that_takes_it():
    """`--json` is documented for every script that registers it, and for no other."""
    skill_md, _ = _documents()
    assert not _undocumented_flags([("gesture.py", frozenset({"--help"}))], skill_md, "x")
    assert _undocumented_flags([("gesture.py", frozenset({"--find-text"}))], skill_md, "x")


def _lose_the_build_handoff(skill_md, references):
    line = 'python3 "$SKILL_DIR/scripts/build_and_test.py" --get-errors build-20251028-143052-1a2b3c4d\n'
    return _once(skill_md, line, ""), references


def _lose_the_tap(skill_md, references):
    old = 'python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Submit Order" --tap\n'
    return _once(skill_md, old, old.replace(" --tap", "")), references


def _show_the_handoff_in_prose_only(skill_md, references):
    start = skill_md.index("## The loop\n")
    end = skill_md.index("## Scripts (")
    loop = skill_md[start:end].replace("```bash\n", "").replace("```\n", "")
    return skill_md[:start] + loop + skill_md[end:], references


def _loop_with_a_flag_the_index_does_not_document(skill_md, references):
    old = 'python3 "$SKILL_DIR/scripts/build_and_test.py" --project .\n'
    return _once(skill_md, old, old.replace("--project .", "--project . --offline")), references


def _retitle_the_loop_section(skill_md, references):
    return _once(skill_md, "## The loop\n", "## Workflows\n"), references


def _map_the_screen_with_a_short_option(skill_md, references):
    old = (
        'python3 "$SKILL_DIR/scripts/screen_mapper.py"\n'
        'python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Submit Order" --tap\n'
    )
    new = old.replace('screen_mapper.py"', 'screen_mapper.py" -s emulator-5554', 1)
    return _once(skill_md, old, new), references


@pytest.mark.parametrize(
    "mutation",
    [
        _lose_the_build_handoff,
        _lose_the_tap,
        _show_the_handoff_in_prose_only,
        _loop_with_a_flag_the_index_does_not_document,
        _retitle_the_loop_section,
    ],
    ids=lambda m: m.__name__,
)
def test_the_loop_guard_refuses_a_section_that_lost_a_handoff(mutation):
    skill_md, _ = _mutated(mutation)
    assert loop_problems(skill_md)


def test_the_budget_guard_refuses_one_byte_too_many():
    skill_md, _ = _documents()
    room = SKILL_MD_LIMIT - len(skill_md.encode("utf-8"))

    assert not budget_problems(skill_md + "x" * room)
    assert budget_problems(skill_md + "x" * (room + 1))
    assert budget_problems(skill_md + "é" * (room // 2 + 1)), "the limit is bytes, not characters"


def test_an_options_declaration_in_a_reference_file_is_refused_however_it_is_written():
    """Mutation: a reference file declares flags, true ones or not."""
    _, references = _documents()
    navigator = references["navigator.md"]

    for declaration in (
        "- Options: `--find-text`, `--teleport`",
        "- Options: `--find-text`,\n  `--tap`",
        "> - Options: `--teleport`",
        "* options : `--teleport`",
        "```text\n- Options: `--teleport`\n```",
        "Options: `--teleport`",
    ):
        assert reference_options_problems("navigator.md", f"{navigator}\n{declaration}\n")
    assert not reference_options_problems("navigator.md", navigator)
    assert not reference_options_problems("x.md", "`Picked up JAVA_TOOL_OPTIONS: ...`\n")
    assert not reference_options_problems("x.md", "The options are listed in SKILL.md.\n")


def test_a_flag_documented_only_in_a_reference_is_undocumented():
    """Mutation: a flag leaves SKILL.md for its reference file; the link stays.

    The reverse flag guard reads SKILL.md alone, on purpose. `--scroll-to-find`
    is accepted by navigator and is not a global flag; moved out of the index
    and declared in `references/navigator.md` instead, it must be reported as
    undocumented, exactly as if it had been deleted.
    """
    skill_md, references = _documents()
    assert "navigator.py" not in _accepted_but_undocumented(skill_md)

    moved = _once(skill_md, " `--scroll-to-find`,", "")
    references = {
        **references,
        "navigator.md": references["navigator.md"] + "\n- Options: `--scroll-to-find`\n",
    }

    assert _NAVIGATOR_DETAIL in moved
    assert reference_options_problems("navigator.md", references["navigator.md"])
    assert _accepted_but_undocumented(moved).get("navigator.py") == ["--scroll-to-find"]


@pytest.mark.parametrize(
    ("example", "refused"),
    [
        ('python3 "$SKILL_DIR/scripts/screen_mapper.py" --list', "does not accept"),
        ('python3 "$SKILL_DIR/scripts/screen_mapper.py" --1', "does not accept"),
        ('python3 "$SKILL_DIR/scripts/screen_mapper.py" -x', "does not accept"),
        ('python3 "$SKILL_DIR/scripts/screen_mapper.py" \\ \n--json', "does not read"),
        (
            'python3 "$SKILL_DIR/scripts/navigator.py" --find-text=Login --teleport=1',
            "does not accept",
        ),
        (
            '# explanation \\\npython3 "$SKILL_DIR/scripts/screen_mapper.py" --teleport',
            "does not accept",
        ),
        ('python3 "$SKILL_DIR/scripts/keyboard.py" --text hello', "does not accept"),
        ('bash "$SKILL_DIR/scripts/android_health_check.sh" --verbose', "does not accept"),
        ('python3 "$SKILL_DIR/scripts/no_such_script.py"', "not on disk"),
        ('bash "$SKILL_DIR/scripts/screen_mapper.py"', "starts bash"),
        ("python3 scripts/screen_mapper.py", "nothing else on the line"),
        ('cd "$SKILL_DIR/scripts" && python3 navigator.py --teleport', "nothing else on the line"),
        ('python3 "$S/navigator.py" --teleport', "nothing else on the line"),
        ('"$SKILL_DIR/scripts/android_health_check.sh" --json', "nothing else on the line"),
        ("python3 $SKILL_DIR/scripts/screen_mapper.py", "nothing else on the line"),
        ('SID=$(python3 "$SKILL_DIR/scripts/anr_watcher.py" --start)', "nothing else on the line"),
        ('python3 "$SKILL_DIR/scripts/screen_mapper.py" | head', "does not read"),
        ('python3 "$SKILL_DIR/scripts/anr_watcher.py" --stop $SID', "does not read"),
        ('python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login', "do not parse"),
        (
            'python3 "$SKILL_DIR/scripts/navigator.py" --find-text Login \\\n    --teleport',
            "does not accept",
        ),
    ],
)
def test_an_example_the_reader_cannot_hold_to_argparse_is_refused(example, refused):
    skill_md, _ = _documents()
    problems = example_problems("x", f"```bash\n{example}\n```\n", skill_md)
    assert len(problems) == 1 and refused in problems[0], problems


def test_an_example_in_prose_or_a_comment_is_not_an_example():
    skill_md, _ = _documents()
    prose = "Never write `python3 scripts/screen_mapper.py --list`.\n"
    comment = '```bash\n# python3 "$SKILL_DIR/scripts/screen_mapper.py" --list\n```\n'

    assert not example_problems("x", prose, skill_md)
    assert not example_problems("x", comment, skill_md)


@pytest.mark.parametrize(
    "example",
    [
        'python3 "$SKILL_DIR/scripts/location.py" --lat -.12 --lng 0.5',
        'python3 "$SKILL_DIR/scripts/location.py" --lat 51.5074 --lng -0.1278',
        'python3 "$SKILL_DIR/scripts/navigator.py" --find-text=Login --tap',
        'python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Log in" \\\n    --tap',
        'python3 "$SKILL_DIR/scripts/screen_mapper.py" -s emulator-5554 -v',
        'bash "$SKILL_DIR/scripts/android_health_check.sh" --json',
        'python3 "$SKILL_DIR/scripts/logs.py" tail --app com.example.app --duration 30s',
    ],
)
def test_an_ordinary_example_is_read_and_passed(example):
    """The false-refusal control: what the next author will write is accepted."""
    skill_md, _ = _documents()
    assert not example_problems("x", f"```bash\n{example}\n```\n", skill_md)
