"""The commands the scripts print themselves are commands that run when installed.

SKILL.md has said since v0.6.0 that `python scripts/foo.py` is a file-not-found
for an installed plugin, and a guard has held its own examples to a rooted
path ever since. Nothing held the scripts to it. Thirty-one of the thirty-two
Python scripts printed `python scripts/<name>.py ...` or `python <name>.py ...`
in their `--help`, the health check printed `bash scripts/...`, and
`screen_mapper --hints` printed `navigator.py --find-text "..." --tap` as "the
next command to run": no interpreter, no path. SKILL.md tells an agent every
script has `--help`, so those were the examples an agent that asked got.

So a script's `--help` is held to a rule that asks where words appear, not
what a line means: **an interpreter's name (`python`, `python3`, `bash`, `sh`,
`zsh`, as a word of its own) and the string `scripts/` appear in help text only
on a line that is, whole, `python3 "$SKILL_DIR/scripts/<file>" <arguments>`**
(`bash` for a shell script), with flags that script accepts. That line is
checked by `test_skill_index.command_line_problems`, as the fenced examples of
SKILL.md and the reference files are. A `#` line of an epilog is no exception:
help text is not shell, so it is read like any other line. Two earlier versions tried to recognise "a
line that invokes a script" and each was wrong in both directions.

What that costs, on purpose: help prose cannot use those five words in lower
case ("a template for bash scripts" is refused; write Bash). What it does not
see, stated: a line that names a script with no interpreter before it --
argparse's own `usage: navigator.py [-h] ...`, a router's table of verbs, and
equally `./navigator.py --tap` -- is taken for prose; a script's module
docstring is not printed by `--help` and is not checked; and error and remedy
messages are not read: inside a sentence they name a script and a flag as a
pointer (`keyboard.py --button back`), not as a line to run.

Four places print a whole command for the agent to run next. Each is built by
`common.cli.invocation`, and each is run or read as printed by a test: the
hint lines of `screen_mapper --hints` (`test_agent_loop_from_output.py` feeds
each to `navigator`), the `Drill:` line of an ANR summary
(`test_anr_watcher.py` feeds it to `--get-details`), the `example` of each
verb in a router's `--json` table, and the recorder's "start one first" line
(both below). A router that does not know a verb suggests the verb, not a
command.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys

import pytest
from test_packaging_contract import SKILL_MD, SKILL_ROOT, _scripts_on_disk
from test_skill_index import _EXAMPLE, _help_of, _logical_lines, command_line_problems

SCRIPTS = _scripts_on_disk()


_INTERPRETER = re.compile(r"(?<![\w./-])(?:python3?|bash|sh|zsh)(?!\w)")
HELP_RULE = (
    "in help text an interpreter's name (python, python3, bash, sh, zsh) and `scripts/` "
    "appear only on a line that is one rooted example; in prose write Python or Bash"
)


def _names_an_interpreter(line: str, scripts: set[str]) -> bool:
    """Whether a line holds an interpreter's name as a word of its own, or `scripts/`.

    A word of its own: not inside a longer word, and not straight after a
    `.`, `/` or `-`, so `android_health_check.sh`, `/bin/sh` and the option
    `--python` are not occurrences; `"python3"` in quotes is one. Nothing
    else about the line is asked -- not whether a file name follows, not
    whether it looks like a command.
    """
    return "scripts/" in line or bool(_INTERPRETER.search(line))


def help_problems(script: str, help_text: str) -> list[str]:
    """Where a script's `--help` names an interpreter or `scripts/`, the line is a rooted example."""
    problems = command_line_problems(
        f"{script} --help",
        help_text.split("\n"),
        SKILL_MD.read_text(encoding="utf-8"),
        names_a_command=_names_an_interpreter,
        comments_are_read=True,
    )
    return [f"{problem} ({HELP_RULE})" for problem in problems]


def help_examples(help_text: str) -> list[str]:
    """The invocations a help text shows, continuations joined."""
    return [line for line in _logical_lines(help_text.split("\n")) if _EXAMPLE.fullmatch(line)]


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_invocation_a_script_prints_in_its_help_runs_when_installed(script):
    assert not help_problems(script, _help_of(script))


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_script_shows_one_command_for_itself_in_its_help(script):
    """An agent that asks a script for help gets at least one line it can run.

    Also what keeps the guard above from passing on a help text it reads
    nothing from.
    """
    own = [line for line in help_examples(_help_of(script)) if f"/scripts/{script}" in line]
    assert own, f"{script} --help shows no rooted example of {script}"


def test_the_help_reader_reads_the_examples_the_scripts_print():
    """Anti-vacuity, in numbers: every script, and a continued line as one."""
    counts = {script: len(help_examples(_help_of(script))) for script in SCRIPTS}

    assert min(counts.values()) >= 1, counts
    assert sum(counts.values()) >= 150, sum(counts.values())
    assert any(
        "--scenario" in line and "--step" in line
        for line in help_examples(_help_of("privacy_manager.py"))
    ), "privacy_manager's continued example is not read as one line"


@pytest.mark.parametrize("router", ["avd.py", "logs.py"])
def test_every_example_in_a_routers_routing_table_runs_when_installed(router):
    """`avd.py --json` and `logs.py --json` answer with one example per verb; each is a command.

    They were `avd.py list --get-details`: a script's name and its arguments,
    presented as the invocation, with no interpreter and no path.
    """
    printed = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "scripts" / router), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert printed.returncode == 0, printed.stderr
    examples = [route["example"] for route in json.loads(printed.stdout)["routes"]]

    assert len(examples) >= 3, examples
    for example in examples:
        assert example.startswith(f'python3 "$SKILL_DIR/scripts/{router}" '), example
        assert _EXAMPLE.fullmatch(example), example
        assert not command_line_problems(f"{router} --json", [example], ""), example


def test_the_recorders_start_one_first_line_is_a_command_that_runs_when_installed(tmp_path):
    """With no session recording, `--step` answers with the command that starts one."""
    import test_recorder

    store = test_recorder.RecorderSessionStore(base_dir=tmp_path / "sessions")
    with pytest.raises(test_recorder.RecorderError) as refusal:
        store.resolve_session_id(None)
    command = str(refusal.value).splitlines()[-1].strip()

    assert _EXAMPLE.fullmatch(command), command
    assert shlex.split(command)[1:] == [
        "$SKILL_DIR/scripts/test_recorder.py",
        "--start",
        "My test",
    ]
    assert not command_line_problems("test_recorder.py", [command], ""), command


@pytest.mark.parametrize(
    ("router", "typed", "verb"), [("avd.py", "ls", "list"), ("logs.py", "crash", "crashes")]
)
def test_a_router_that_does_not_know_a_verb_suggests_the_verb_not_a_command(router, typed, verb):
    """It printed `Did you mean: avd.py list`: a replacement invocation, bare."""
    answered = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "scripts" / router), typed],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    (suggestion,) = [line for line in answered.stderr.splitlines() if "Did you mean" in line]

    assert answered.returncode == 2
    assert suggestion == f"Did you mean the verb `{verb}`?"


_ROOTED = 'python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap'


@pytest.mark.parametrize(
    ("line", "refused"),
    [
        ('  python scripts/navigator.py --find-text "Login" --tap', "nothing else on the line"),
        ('  python navigator.py --find-text "Login" --tap', "nothing else on the line"),
        ('  python3 navigator.py --find-text "Login" --tap', "nothing else on the line"),
        ("  python3 navigtor.py --tap", "nothing else on the line"),
        ('  python3 "$SKILL_DIR/scripts/navigtor.py" --tap', "not on disk"),
        ("  See scripts/ for the installed command files.", "nothing else on the line"),
        ('  "python3" navigator.py --tap', "nothing else on the line"),
        ("  /usr/bin/env python3 navigator.py --tap", "nothing else on the line"),
        ("  sh android_health_check.sh --json", "nothing else on the line"),
        (
            "  --template FILE    Template for bash scripts (setup.sh.in)",
            "nothing else on the line",
        ),
        ("  Needs python 3.12 or newer.", "nothing else on the line"),
        ("  # Requires python3.", "nothing else on the line"),
        ("  # See scripts/ for commands.", "nothing else on the line"),
        (f"  # {_ROOTED}", "nothing else on the line"),
        ("  bash scripts/android_health_check.sh --json", "nothing else on the line"),
        ("Usage: bash scripts/android_health_check.sh [options]", "nothing else on the line"),
        ("Also reachable as `python scripts/logs.py tail ...`.", "nothing else on the line"),
        (f"  {_ROOTED}   # taps it", "does not read"),
        (f"  SID=$({_ROOTED})", "nothing else on the line"),
        (f"  {_ROOTED} --teleport", "does not accept"),
        ('  python3 "$SKILL_DIR/scripts/keyboard.py" --text hello', "does not accept"),
        ('  python3 "$SKILL_DIR/scripts/avd.py" start --find-text x', "does not accept"),
    ],
)
def test_a_help_line_that_would_not_run_when_installed_is_refused(line, refused):
    """Mutation: navigator's real help with one line added, in each form it had."""
    help_text = _help_of("navigator.py") + "\n" + line + "\n"
    problems = help_problems("navigator.py", help_text)
    assert len(problems) == 1 and refused in problems[0], problems


@pytest.mark.parametrize(
    "line",
    [
        "usage: navigator.py [-h] [--find-text TEXT]",
        "  avd.py list --help         -> device_list.py",
        "  # Boot a chosen AVD (delegates to emulator_boot.py)",
        "  - Python 3.12+ (for the skill's Python scripts) and Pillow importability",
        "  --python PATH     Interpreter used for navigator.py",
        "  Run screen_mapper.py first; navigator.py takes a name it printed.",
        "  --template FILE    Template for Bash scripts (setup.sh.in)",
        "  --shell PATH       The shell to use (default: /bin/sh)",
        "  Needs Python 3.12 or newer; pushes a file to /data/local/tmp/x.sh.",
        f"  {_ROOTED}",
        '  python3 "$SKILL_DIR/scripts/avd.py" start --avd Pixel_9 --wait-ready',
        '  bash "$SKILL_DIR/scripts/android_health_check.sh" --json',
    ],
)
def test_a_help_line_that_is_prose_or_a_rooted_command_is_passed(line):
    """The false-refusal control: what help texts ordinarily hold."""
    assert not help_problems("navigator.py", _help_of("navigator.py") + "\n" + line + "\n")
