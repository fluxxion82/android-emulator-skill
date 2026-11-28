"""The README and the site say only what something checks.

A stranger meets this project through two pages: ``README.md`` and the site
(``site/index.html``). v0.10.0's claim is that what they say can be re-run.

How this module holds them to it changed once, and the reason is the design.
The first version READ the pages: it parsed Markdown tables, grouped fenced
lines under headings, walked the HTML, and compared what it found. Two reviews
each showed pages that were wrong and that it passed -- a table indented into
a code block, a heading one level deeper, a block under the other agent's
name, a quoted ``'$SKILL_DIR'``, an element marked ``hidden``. A reader of
Markdown, HTML and shell is three parsers, and none of them was ever going to
be finished.

So nothing here interprets a page any more. Each rule builds the text that
must be on the page -- from the measurement script, the install check's own
table, the pins file -- and compares it WHOLE with a section of the README or
a fragment of the site's source. A page that is wrong in any way that changes
those characters fails, whatever a renderer would have made of it.

1. **The measurement.** README's ``## What it costs`` is, character for
   character, ``python tests/measure_costs.py --readme``. The script's rows
   are named here, so a row cannot leave the script unnoticed.
2. **The support matrix.** README's ``## Supported agents`` is the text built
   from ``tests/verify_install.py``'s agents and ``agent-cli-pins.txt``.
3. **The install commands.** README's ``## Install`` opens with the text built
   from the commands that check runs; the site's install section is the
   markup built from the same.
4. **The commands shown** are, each whole line, the one form SKILL.md writes,
   with flags the script accepts (``test_skill_index.command_line_problems``,
   the rule the reference files are held to).
5. **The site's own statements** are fragments of its source built from the
   same facts: how many agents, which, the one figure it quotes.
6. **The publishing path.** ``pages.yml`` is compared whole with a mirror
   written out here, and its ``check`` job with ``test.yml``'s.

Limits, stated:

- **Source, not rendering.** Whether a browser shows an element (an ancestor
  marked ``hidden``, a stylesheet) is not computed. Each of the site's
  elements is compared with its own opening tag, which is as far as that goes.
- **Whole, less the trailing empty lines of a section.** A README section is
  compared from its heading to its last line that is not blank.
- **Commands are the ones in code blocks.** Every fenced line of the README
  and every ``<pre>`` of the site is written out here. A command quoted in a
  sentence is prose.
- **Named sections and fragments only.** README's Status section, the site's
  other feature cards and the sentences between sections are prose written
  against the workflows and the code; nothing re-reads them.
- **The mirror of pages.yml is by hand.** An edit to the workflow is an edit
  to it; that is the point, and also all it proves.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import measure_costs as mc
import pytest
import verify_install as vi
from test_packaging_contract import _yaml, _yaml_with_the_keys_github_reads
from test_quick_start_contract import code_lines
from test_skill_index import _commands_in, command_line_problems

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
SITE = REPO / "site" / "index.html"
SKILL_MD = mc.SKILL_MD
RECORDED = REPO / "tests" / "fixtures" / "recorded"
WORKFLOWS = REPO / ".github" / "workflows"

# The rows the script's two tables must have, in order. Named here and not read
# from the script: a screen dropped from its case list would otherwise leave
# the README, which is its output, agreeing with it.
SCREEN_ROWS = (
    "walk 1 catalog",
    "walk 2 product",
    "walk 3 cart",
    "walk 4 checkout",
    "walk 5 confirmation",
    "walk 6 settings",
    "compose single screen",
    "settings home",
    "dialer keypad",
    "**walk, 6 screens**",
    "share of the raw dump",
)
SKILL_ROWS = (
    "`SKILL.md`",
    "`SKILL.md` plus its largest reference file",
    "`SKILL.md` at v0.9.0, with no reference files beside it",
)
# What the tables do not compare: printed with them, or the numbers travel alone.
CAVEATS = (
    "not the cost of a task",
    "do not carry the same information",
    "testTagsAsResourceId",
    "an estimate; no tokenizer is run",
)

# The name each agent goes by in prose, in the order the pages list them.
AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}


@pytest.fixture(scope="module")
def measured() -> dict:
    return mc.measure()


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def site() -> str:
    return SITE.read_text(encoding="utf-8")


def _reported(problems: list[str], words: str) -> bool:
    """Whether one of the problems is the one a control is there to produce."""
    return any(words in problem for problem in problems)


# ---------------------------------------------------------------------------
# Taking a page apart, without reading it.
# ---------------------------------------------------------------------------


def _raw_section(text: str, heading: str) -> str | None:
    """From the one line that is ``heading`` to the line before the next ``## `` line.

    Lines, not Markdown: a ``## `` inside a fenced block ends the section too.
    That is deliberate. Whatever is returned is compared whole with a text
    that has no such line in it, so a page where it matters does not compare
    equal. None when the heading is not there exactly once.
    """
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line == heading]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).rstrip("\n") + "\n"


def _fragment(html: str, opening: str, closing: str) -> str | None:
    """The source from the one ``opening`` through the first ``closing`` after it."""
    if html.count(opening) != 1:
        return None
    start = html.index(opening)
    end = html.find(closing, start)
    return None if end == -1 else html[start : end + len(closing)]


def _pins() -> dict[str, str]:
    return vi.read_pins(vi.PINS.read_text(encoding="utf-8"))


def _agents() -> list:
    """The install check's agents, in the order the pages list them."""
    by_key = {agent.key: agent for agent in vi.AGENTS}
    return [by_key[key] for key in AGENT_LABELS]


def _repository_slug() -> str:
    """``owner/name``, from the plugin manifest's repository URL."""
    manifest = json.loads(
        (REPO / "android-emulator-skill" / ".claude-plugin" / "plugin.json").read_text("utf-8")
    )
    return manifest["repository"].removeprefix("https://github.com/").strip("/")


def install_commands() -> dict[str, list[str]]:
    """What a user types, per agent: what the install check runs, aimed at the repository."""
    source = _repository_slug()
    return {
        agent.key: [
            " ".join([agent.cli, *(part.format(source=source) for part in step)])
            for step in agent.install
        ]
        for agent in _agents()
    }


def test_the_agents_named_here_are_the_ones_the_install_check_installs_with():
    assert set(AGENT_LABELS) == {
        agent.key for agent in vi.AGENTS
    }, "an agent was added to (or removed from) the install check; name it here and on the pages"
    assert set(_pins()) == {agent.package for agent in vi.AGENTS}
    assert install_commands() == {
        "claude": [
            "claude plugin marketplace add fluxxion82/android-emulator-skill",
            "claude plugin install android-emulator-skill@fluxxion82",
        ],
        "codex": [
            "codex plugin marketplace add fluxxion82/android-emulator-skill",
            "codex plugin add android-emulator-skill@fluxxion82",
        ],
    }, "the install check's commands changed; the README and the site are built from them"


# ---------------------------------------------------------------------------
# 1. The measurement.
# ---------------------------------------------------------------------------


def measurement_problems(readme: str, measured: dict) -> list[str]:
    """Why the README's ``## What it costs`` is not the script's output. Empty: it is."""
    if _raw_section(readme, mc.README_HEADING) != mc.readme_section(measured):
        return [
            f"README.md's `{mc.README_HEADING}` is not `python tests/measure_costs.py --readme`, "
            "character for character, once"
        ]
    return []


def _first_cells(markdown: str) -> list[str]:
    """The first cell of each data row the script printed (its own output, its own format)."""
    return [
        line.split("|")[1].strip()
        for line in markdown.splitlines()
        if line.startswith("| ") and not line.startswith(("| Screen |", "| What the agent loads |"))
    ]


def script_problems(measured: dict) -> list[str]:
    """Why the section the script prints is not the one this release promised."""
    printed = mc.readme_section(measured)
    problems = []
    if _first_cells(printed) != [*SCREEN_ROWS, *SKILL_ROWS]:
        problems.append(f"the script's rows are {_first_cells(printed)}")
    for caveat in CAVEATS:
        if caveat not in printed:
            problems.append(f"the script prints its numbers without {caveat!r}")
    if f"```bash\n{mc.REPRODUCE}\n```" not in printed:
        problems.append("the script's section does not show the command that reproduces it")
    return problems


def test_the_readme_measurement_is_the_scripts(readme, measured):
    assert (
        measurement_problems(readme, measured) == []
    ), "replace the section with the output of: python tests/measure_costs.py --readme"
    assert script_problems(measured) == []


def test_the_command_the_readme_gives_is_the_one_the_script_documents():
    assert mc.REPRODUCE in mc.__doc__
    assert mc.REPRODUCE.endswith("python tests/measure_costs.py")


def _cell(readme: str) -> str:
    cell = "| walk 1 catalog | 8,586 (~2,147) |"
    assert cell in readme, "the control's anchor is gone; pick another cell"
    return cell


MEASUREMENT_MUTATIONS = {
    "a cell is changed": lambda text: text.replace(
        _cell(text), "| walk 1 catalog | 8,585 (~2,147) |"
    ),
    "a row is removed": lambda text: "\n".join(
        line for line in text.split("\n") if not line.startswith("| dialer keypad |")
    ),
    "a caveat is cut": lambda text: text.replace("not the cost of a task", "a fair comparison"),
    "the estimate is not called one": lambda text: text.replace(
        "divided by four, an estimate", "divided by four"
    ),
    "the table is indented into a code block": lambda text: text.replace(
        "| Screen | raw dump |", "    | Screen | raw dump |"
    ),
    "a heading goes one level deeper": lambda text: text.replace(
        "### What one screen costs to read", "#### What one screen costs to read"
    ),
    "the reproduce command is run from another directory": lambda text: text.replace(
        f"```bash\n{mc.REPRODUCE}", f"```bash\ncd tests\n{mc.REPRODUCE}"
    ),
    "the introduction says it is the cost of a task": lambda text: text.replace(
        "The numbers below are printed by a script",
        "The numbers below are the full cost of a task, printed by a script",
    ),
    "the section is renamed": lambda text: text.replace("## What it costs", "## Benchmarks"),
    "the section appears twice": lambda text: text + "\n## What it costs\n\nAlso this.\n",
}


@pytest.mark.parametrize("name", MEASUREMENT_MUTATIONS)
def test_a_readme_that_departs_from_the_measurement_is_reported(readme, measured, name):
    changed = MEASUREMENT_MUTATIONS[name](readme)

    assert changed != readme, "the control changed nothing"
    assert measurement_problems(changed, measured), name


def _without_a_screen(measured: dict) -> dict:
    less = copy.deepcopy(measured)
    del less["screens"][-1]
    return less


def test_a_script_that_measures_less_than_promised_is_reported(measured, monkeypatch):
    """The README is the script's output, so the script is held to its rows and caveats by name."""
    assert _reported(script_problems(_without_a_screen(measured)), "the script's rows are ")

    monkeypatch.setattr(mc, "README_INTRODUCTION", ("Numbers.",))
    assert _reported(script_problems(measured), "without 'an estimate; no tokenizer is run'")
    monkeypatch.setattr(mc, "REPRODUCE", "python tests/measure_costs.py")
    assert not _reported(
        script_problems(measured), "does not show the command"
    ), "whatever command the script states, its section must show that one"


# ---------------------------------------------------------------------------
# 2. The support matrix.
# ---------------------------------------------------------------------------


def supported_agents_section(pins: dict[str, str]) -> str:
    """README's ``## Supported agents``, whole: one row per agent the install check installs with."""
    rows = "\n".join(
        f"| {AGENT_LABELS[agent.key]} | `{agent.package}@{pins[agent.package]}` | "
        f"`python tests/verify_install.py --agent {agent.key}` |"
        for agent in _agents()
    )
    return f"""## Supported agents

One row per agent whose own CLI installs this plugin into an empty home and lists the skill
afterwards, in CI, on every pull request to `main`. The check is one command from a checkout.

| Agent | Checked with | The check |
|---|---|---|
{rows}

The check installs from a local checkout and refuses a CLI at any other version. Other
agents may well be able to load the skill directory; none is listed until it has a check.
"""


def matrix_problems(readme: str, pins: dict[str, str]) -> list[str]:
    """Why the README's matrix is not the install check's agents. Empty: it is."""
    if _raw_section(readme, "## Supported agents") != supported_agents_section(pins):
        return [
            "README.md's `## Supported agents` is not, character for character, one row per "
            "agent tests/verify_install.py installs with, at the pinned version"
        ]
    return []


def test_the_matrix_has_one_row_per_agent_with_an_install_check(readme):
    assert matrix_problems(readme, _pins()) == []


MATRIX_MUTATIONS = {
    "an agent with no check is listed": lambda text: text.replace(
        "| Codex | ", "| Cursor | `cursor@1.0.0` | `npx skills add` |\n| Codex | "
    ),
    "a checked agent is left out": lambda text: "\n".join(
        line for line in text.split("\n") if not line.startswith("| Codex |")
    ),
    "a row names the other agent's check": lambda text: text.replace(
        "verify_install.py --agent codex", "verify_install.py --agent claude"
    ),
    "a row names no check": lambda text: text.replace(
        "`python tests/verify_install.py --agent claude`", "tested by hand"
    ),
    "the matrix is indented into a code block": lambda text: text.replace(
        "| Agent | Checked with |", "    | Agent | Checked with |"
    ),
    "the matrix loses its delimiter row": lambda text: text.replace(
        "|---|---|---|\n| Claude Code |", "| Claude Code |"
    ),
    "the sentence about other agents goes": lambda text: text.replace(
        "none is listed until it has a check", "all of them work"
    ),
}


@pytest.mark.parametrize("name", MATRIX_MUTATIONS)
def test_a_matrix_that_claims_more_or_less_than_the_check_is_reported(readme, name):
    changed = MATRIX_MUTATIONS[name](readme)

    assert changed != readme, "the control changed nothing"
    assert matrix_problems(changed, _pins()), name


def test_a_pin_that_moves_without_the_readme_is_reported(readme):
    moved = {**_pins(), "@openai/codex": "0.171.0"}

    assert matrix_problems(readme, moved)


# ---------------------------------------------------------------------------
# 3. The install commands, in both places.
# ---------------------------------------------------------------------------

# The `### ` headings README's Install section has, in order. One beside these
# is a place for commands this module did not build.
INSTALL_SUBSECTIONS = (
    "### Claude Code",
    "### Codex",
    "### Updating Claude Code's copy is two commands",
    "### Without the plugin system",
)
# Every line of the README that runs either CLI's plugin commands, after the
# install commands: the update pair, which tests/test_packaging_contract.py reads.
README_UPDATE_COMMANDS = (
    "claude plugin marketplace update fluxxion82      # git-pull the marketplace repo",
    "claude plugin update android-emulator-skill      # apply it (restart Claude Code afterwards)",
)
_PLUGIN_COMMAND = re.compile(r"\b(?:claude|codex)\s+plugin\b")
# Every other line the README puts in a fenced block, in order. With the install
# and update commands before them, these are all the lines a reader can copy:
# a pattern for "a plugin command" was shown `"codex" plugin install wrong@wrong`,
# which a shell runs and the pattern did not see. A list has no spelling to get round.
README_OTHER_CODE = (
    "git clone https://github.com/fluxxion82/android-emulator-skill ~/src/android-emulator-skill",
    "",
    "# Personal: available in every project",
    "ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill "
    "~/.claude/skills/android-emulator-skill",
    "",
    "# Project-local: commit it, or add it to .gitignore",
    "ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill "
    ".claude/skills/android-emulator-skill",
    "export ANDROID_HOME=$HOME/Library/Android/sdk        # macOS example",
    'export PATH="$PATH:$ANDROID_HOME/platform-tools"',
    'export PATH="$PATH:$ANDROID_HOME/emulator"           # the emulator DIRECTORY, not the SDK root',
    'export PATH="$PATH:$ANDROID_HOME/cmdline-tools/latest/bin"',
    'python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app',
    'python3 "$SKILL_DIR/scripts/screen_mapper.py"',
    'python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap --expect "Welcome"',
)


def readme_code() -> list[str]:
    """Every fenced line of README.md, in order."""
    install = [line for lines in install_commands().values() for line in lines]
    return [*install, *README_UPDATE_COMMANDS, *README_OTHER_CODE, mc.REPRODUCE]


def install_section_opening() -> str:
    """README's ``## Install`` from its heading through the note under the two agents' blocks."""
    blocks = "\n".join(
        f"### {AGENT_LABELS[key]}\n\n```bash\n" + "\n".join(commands) + "\n```\n"
        for key, commands in install_commands().items()
    )
    return f"""## Install

Both agents take the same two steps: add this repository as a marketplace, then install
the plugin from it.

{blocks}
`fluxxion82` after the `@` is the marketplace's name, the `name` field of its manifest, not a
GitHub username; they coincide here. The install check runs these commands with a local path
where they name the repository; installing from GitHub is not part of it. Codex's help names
`owner/repo` as a source, and Claude Code's says "a URL, path, or GitHub repo".

"""


def readme_install_problems(readme: str) -> list[str]:
    """Why the README's install instructions are not the ones the check runs."""
    install = _raw_section(readme, "## Install")
    if install is None:
        return ["README.md does not have the one `## Install` section"]
    problems = []
    if not install.startswith(install_section_opening()):
        problems.append(
            "README.md's `## Install` does not open, character for character, with each "
            "agent's commands as the install check runs them"
        )
    headings = tuple(line for line in install.split("\n") if line.startswith("###"))
    if headings != INSTALL_SUBSECTIONS:
        problems.append(f"`## Install` has the subsections {list(headings)}")
    expected = [line for lines in install_commands().values() for line in lines]
    shown = [line.strip() for line in readme.split("\n") if _PLUGIN_COMMAND.search(line)]
    if shown != [*expected, *README_UPDATE_COMMANDS]:
        problems.append(f"README.md runs the agents' plugin commands on the lines {shown}")
    fenced = code_lines(readme)
    if fenced != readme_code():
        extra = [line for line in fenced if line not in readme_code()]
        problems.append(
            f"README.md's fenced lines are not the ones written out as readme_code(): {extra}"
        )
    return problems


_SITE_INSTALL_OPENING = '<section id="install" class="section">'
_SITE_BLOCK = """    <div class="codeblock">
      <div class="codeblock-head">
        <span>{label}</span>
      </div>
<pre><code>{commands}</code></pre>
    </div>
"""
OS_SENTENCE = "Developed on macOS; CI runs on Linux."


def site_install_section() -> str:
    """The site's install section, whole, as markup."""
    blocks = "\n".join(
        _SITE_BLOCK.format(label=AGENT_LABELS[key], commands="\n".join(commands))
        for key, commands in install_commands().items()
    )
    return f"""{_SITE_INSTALL_OPENING}
  <div class="container">
    <div class="section-head">
      <span class="section-num">03 / INSTALL</span>
      <h2 class="section-title">Install as a plugin.</h2>
      <p class="section-sub">Two commands per agent: add the marketplace, install the plugin.</p>
    </div>

{blocks}    <p class="install-prereqs">Prerequisites: Android SDK with platform-tools &amp; emulator · Python 3.12+ · ADB · Gradle (optional) · Pillow for screenshot resizing (optional). {OS_SENTENCE}</p>
  </div>
</section>"""


# The site's third code block, whole. With the two of the install section these
# are all its `<pre>` elements: a block added with another opening tag
# (`<pre><code class="...">`) is one more `<pre`, whatever it holds.
SITE_QUICK_START = """<pre><code>SKILL_DIR=/path/to/skills/android-emulator-skill   # by path, not relative to the cwd
python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app
python3 "$SKILL_DIR/scripts/screen_mapper.py"
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --enter-text "user@example.com"
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap --expect "Welcome"
python3 "$SKILL_DIR/scripts/accessibility_audit.py"</code></pre>"""


def site_code_problems(html: str) -> list[str]:
    """Why the site has a code block that is not one of the three written out here."""
    problems = []
    if html.count(SITE_QUICK_START) != 1:
        problems.append(
            "the site's quick-start block is not, character for character, SITE_QUICK_START"
        )
    blocks = len(install_commands()) + 1
    found = len(re.findall(r"<pre\b", html, flags=re.IGNORECASE))
    if found != blocks:
        problems.append(f"the site has {found} `<pre` elements, not the {blocks} written out")
    return problems


def site_install_problems(html: str) -> list[str]:
    """Why the site's install section is not the commands the check runs, under their names."""
    problems = site_code_problems(html)
    if _fragment(html, _SITE_INSTALL_OPENING, "</section>") != site_install_section():
        problems.append(
            "the site's install section is not, character for character, each agent's "
            "commands under its own name"
        )
    expected = [line for lines in install_commands().values() for line in lines]
    shown = [match.group(0) for match in re.finditer(r"(?:claude|codex)\s+plugin\b[^<\n]*", html)]
    if shown != expected:
        problems.append(f"the site's source runs the agents' plugin commands as {shown}")
    return problems


def test_the_install_commands_are_the_ones_the_check_runs(readme, site):
    assert readme_install_problems(readme) == []
    assert site_install_problems(site) == []


def _swap(text: str, first: str, second: str) -> str:
    return text.replace(first, "\0").replace(second, first).replace("\0", second)


# (the edit, whether it applies to the README, whether it applies to the site)
INSTALL_MUTATIONS = {
    "codex installs with claude's verb": lambda text: text.replace(
        "codex plugin add android-emulator-skill@fluxxion82",
        "codex plugin install android-emulator-skill@fluxxion82",
    ),
    "the marketplace step is dropped": lambda text: text.replace(
        "claude plugin marketplace add fluxxion82/android-emulator-skill\n", ""
    ),
    "another repository is named": lambda text: text.replace(
        "codex plugin marketplace add fluxxion82/android-emulator-skill",
        "codex plugin marketplace add someone/android-emulator-skill",
    ),
    "the agents' blocks carry each other's names": lambda text: _swap(
        _swap(text, "### Claude Code\n", "### Codex\n"),
        "<span>Claude Code</span>",
        "<span>Codex</span>",
    ),
}


@pytest.mark.parametrize("name", INSTALL_MUTATIONS)
def test_install_commands_the_check_does_not_run_are_reported(readme, site, name):
    changed_readme = INSTALL_MUTATIONS[name](readme)
    changed_site = INSTALL_MUTATIONS[name](site)

    assert changed_readme != readme and changed_site != site, "the control changed nothing"
    assert _reported(readme_install_problems(changed_readme), "does not open, character for")
    assert _reported(site_install_problems(changed_site), "is not, character for character")


README_INSTALL_MUTATIONS = {
    "a third subsection gives other commands": (
        lambda text: text.replace(
            "### Updating Claude Code's copy",
            "### Linux users\n\n```bash\ncodex plugin install wrong@wrong\n```\n\n"
            "### Updating Claude Code's copy",
        ),
        "`## Install` has the subsections ",
    ),
    "an install command appears somewhere else": (
        lambda text: text.replace(
            "## License\n", "## License\n\n    claude plugin install something-else@somewhere\n"
        ),
        "README.md runs the agents' plugin commands on the lines ",
    ),
    "the agents' sections leave Install": (
        lambda text: text.replace("## Install\n", "## Getting it\n").replace(
            "## Prerequisites\n", "## Install\n\nSee above.\n\n## Prerequisites\n"
        ),
        "does not open, character for character",
    ),
    "a command the pattern cannot spell is put in a fence": (
        lambda text: text.replace(
            "codex plugin add android-emulator-skill@fluxxion82\n```",
            'codex plugin add android-emulator-skill@fluxxion82\n```\n\n```bash\n"codex" plugin '
            "install wrong@wrong\n```",
        ),
        "README.md's fenced lines are not the ones written out",
    ),
    "what the check does not exercise is no longer said": (
        lambda text: text.replace(" installing from GitHub is not part of it.", ""),
        "does not open, character for character",
    ),
}


@pytest.mark.parametrize("name", README_INSTALL_MUTATIONS)
def test_a_readme_that_installs_another_way_is_reported(readme, name):
    change, words = README_INSTALL_MUTATIONS[name]
    changed = change(readme)

    assert changed != readme, "the control changed nothing"
    problems = readme_install_problems(changed)
    assert _reported(problems, words), problems


SITE_INSTALL_MUTATIONS = {
    "the install section is hidden": (
        lambda html: html.replace(
            _SITE_INSTALL_OPENING, '<section id="install" class="section" hidden>'
        ),
        "is not, character for character",
    ),
    "a second install block is added elsewhere": (
        lambda html: html.replace(
            "</main>", "<pre><code>codex plugin install wrong@wrong</code></pre>\n</main>"
        ),
        "the site's source runs the agents' plugin commands as ",
    ),
    "a block with another opening tag gives other commands": (
        lambda html: html.replace(
            "</main>",
            '<pre><code class="language-bash">"codex" plugin install wrong@wrong</code></pre>\n</main>',
        ),
        "`<pre` elements, not the 3 written out",
    ),
    "a block in capitals gives other commands": (
        lambda html: html.replace(
            "</main>",
            '<PRE><CODE>python3 "$SKILL_DIR/scripts/screen_mapper.py" --teleport</CODE></PRE>\n</main>',
        ),
        "the site has 4 `<pre` elements",
    ),
    "the quick start runs another command": (
        lambda html: html.replace(
            'python3 "$SKILL_DIR/scripts/accessibility_audit.py"</code>',
            'python3 "$SKILL_DIR/scripts/accessibility_audit.py" --teleport</code>',
        ),
        "is not, character for character, SITE_QUICK_START",
    ),
    "a block is commented out": (
        lambda html: html.replace(
            "<pre><code>codex plugin marketplace add", "<!--<pre><code>codex plugin marketplace add"
        ),
        "is not, character for character",
    ),
}


@pytest.mark.parametrize("name", SITE_INSTALL_MUTATIONS)
def test_a_site_that_installs_another_way_is_reported(site, name):
    change, words = SITE_INSTALL_MUTATIONS[name]
    changed = change(site)

    assert changed != site, "the control changed nothing"
    problems = site_install_problems(changed)
    assert _reported(problems, words), problems


# ---------------------------------------------------------------------------
# 4. The commands shown: one form, whole line, flags the script accepts.
# ---------------------------------------------------------------------------


def _site_code_lines(html: str) -> list[str]:
    """The lines inside the site's ``<pre><code>`` blocks, as source.

    `site_code_problems` holds that these are all of the site's `<pre>` elements.
    """
    return [
        line
        for block in re.findall(r"<pre><code>(.*?)</code></pre>", html, flags=re.S)
        for line in block.split("\n")
    ]


def _page_lines(page: str, readme: str, site: str) -> list[str]:
    return code_lines(readme) if page == "README.md" else _site_code_lines(site)


@pytest.mark.parametrize("page", ["README.md", "site/index.html"])
def test_the_commands_a_page_shows_are_the_one_form_with_flags_the_script_accepts(
    readme, site, page
):
    skill_md = SKILL_MD.read_text(encoding="utf-8")
    lines = _page_lines(page, readme, site)

    assert (
        "navigator.py",
        frozenset({"--find-text", "--tap", "--expect"}),
    ) in _commands_in(lines), f"{page} no longer shows the tap-and-expect command"
    assert command_line_problems(page, lines, skill_md) == []


COMMAND_MUTATIONS = {
    "a flag the script does not accept": (
        lambda line: line.replace("--tap --expect", "--tap --teleport"),
        "passes ['--teleport'], which navigator.py does not accept",
    ),
    "a command that is not rooted": (
        lambda line: line.replace('"$SKILL_DIR/scripts/screen_mapper.py"', "screen_mapper.py"),
        "nothing else on the line",
    ),
    "a root the shell will not expand": (
        lambda line: line.replace(
            '"$SKILL_DIR/scripts/screen_mapper.py"', "'$SKILL_DIR/scripts/screen_mapper.py'"
        ),
        "nothing else on the line",
    ),
    "a second command chained on": (
        lambda line: line.replace(
            '"$SKILL_DIR/scripts/screen_mapper.py"',
            '"$SKILL_DIR/scripts/navigator.py" --list && python3 '
            '"$SKILL_DIR/scripts/screen_mapper.py" --list',
        ),
        "which this guard does not read",
    ),
}


@pytest.mark.parametrize("page", ["README.md", "site/index.html"])
@pytest.mark.parametrize("name", COMMAND_MUTATIONS)
def test_a_command_a_reader_could_not_run_is_reported(readme, site, page, name):
    change, words = COMMAND_MUTATIONS[name]
    lines = _page_lines(page, readme, site)
    changed = [change(line) for line in lines]

    assert changed != lines, "the control changed nothing"
    problems = command_line_problems(page, changed, SKILL_MD.read_text(encoding="utf-8"))
    assert _reported(problems, words), problems


# ---------------------------------------------------------------------------
# 5. What the site states of its own.
# ---------------------------------------------------------------------------

_COUNT_WORDS = {6: "six"}


def _names(labels: list[str]) -> str:
    return " and ".join(labels)


def site_fragments(measured: dict) -> dict[str, str]:
    """Each statement the site makes of its own, as the WHOLE element that states it.

    Whole, opening tag and every sentence: a fragment that began after the
    tag passed a paragraph marked `hidden`, and one that held only the claim
    passed "The following claim is false:" written in front of it.
    """
    labels = [AGENT_LABELS[agent.key] for agent in _agents()]
    walk = measured["walk"]
    screens = _COUNT_WORDS.get(len(walk["screens"]), str(len(walk["screens"])))
    share = walk["percent_of_raw_dump"]["screen_mapper"]
    return {
        "which agents": (
            '    <p class="lede">A skill of 33 scripts wrapping <code>adb</code>, '
            "<code>uiautomator</code>, <code>avdmanager</code>, and the Gradle wrapper behind "
            "commands that find things on the screen by name and answer in a few lines. "
            f"It installs as a plugin in {_names(labels)}.</p>\n"
        ),
        "how many agents": (
            '      <div class="stat">\n'
            '        <div class="stat-num">/03</div>\n'
            f'        <div class="stat-value">{len(labels)}</div>\n'
            '        <div class="stat-label">Agents with an install check</div>\n'
            "      </div>\n"
        ),
        "the one figure": (
            '      <article class="feature-card">\n'
            '        <span class="feature-num">A</span>\n'
            "        <h3>A few lines by default</h3>\n"
            "        <p>Default output is a few lines; <code>--verbose</code> and "
            f"<code>--json</code> give the rest. Over {screens} recorded screens of one app, "
            f"what <code>screen_mapper.py</code> printed was {share}% of the bytes of the raw "
            "<code>uiautomator</code> dumps. The README has the table and the script that "
            "prints it.</p>\n"
            "      </article>\n"
        ),
    }


def site_statement_problems(html: str, measured: dict) -> list[str]:
    """Why the site's own statements are not the repository's. Empty: they are."""
    problems = [
        f"the site's source does not state {what}, once, as: {fragment}"
        for what, fragment in site_fragments(measured).items()
        if html.count(fragment) != 1
    ]
    if len(re.findall(r"\d\s*%", html)) != 1 or re.search(r"per\s*cent", html, re.IGNORECASE):
        problems.append("the site quotes a percentage besides the one that is measured")
    if "Windows" in html:
        problems.append("the site names Windows; nothing here runs on it")
    if html.count(f" {OS_SENTENCE}</p>") != 1:
        problems.append(f"the site's source does not say, once, {OS_SENTENCE!r}")
    return problems


def test_the_sites_own_statements_are_the_repositorys(site, measured):
    assert site_statement_problems(site, measured) == []


def test_where_it_was_developed_and_tested_is_read_off_the_evidence():
    """macOS: every device profile was recorded from one. Linux: every CI job runs on it."""
    recorded_on = {}
    for path in sorted(RECORDED.glob("*/MANIFEST.json")):
        host = json.loads(path.read_text("utf-8")).get("recorded_on")
        if host is not None:  # the profiles of host tools (Gradle, the CLIs) do not say
            recorded_on[path.parent.name] = host
    assert (
        "emulator-api35" in recorded_on
    ), "the primary profile no longer says where it was recorded"
    assert all(host.startswith("macOS") for host in recorded_on.values()), recorded_on

    runners = {
        f"{path.name}:{name}": job.get("runs-on")
        for path in sorted((REPO / ".github" / "workflows").glob("*.yml"))
        for name, job in _yaml(path.name)["jobs"].items()
        if "uses" not in job
    }
    assert runners, "no workflow job names a runner"
    assert set(runners.values()) == {"ubuntu-latest"}, runners


SITE_MUTATIONS = {
    "an agent is dropped from the sentence that names them": (
        lambda html: html.replace(
            "It installs as a plugin in Claude Code and Codex.",
            "It installs as a plugin in Claude Code.",
        ),
        "does not state which agents",
    ),
    "an agent is named only where nobody sees it": (
        lambda html: html.replace(
            "It installs as a plugin in Claude Code and Codex.",
            "It installs as a plugin in Claude Code.<span hidden> and Codex.</span>",
        ),
        "does not state which agents",
    ),
    "the agent count is wrong": (
        lambda html: html.replace(
            '<div class="stat-value">2</div>\n        <div class="stat-label">Agents',
            '<div class="stat-value">3</div>\n        <div class="stat-label">Agents',
        ),
        "does not state how many agents",
    ),
    "the share is rounded in its favour": (
        lambda html: html.replace("was 3.1% of", "was 3% of"),
        "does not state the one figure",
    ),
    "the share loses its scope": (
        lambda html: html.replace("Over six recorded screens of one app", "On average"),
        "does not state the one figure",
    ),
    "the share is given to another command": (
        lambda html: html.replace(
            "what <code>screen_mapper.py</code> printed was 3.1%",
            "what <code>navigator.py --find-text</code> printed was 3.1%",
        ),
        "does not state the one figure",
    ),
    "the paragraph that names the agents is hidden": (
        lambda html: html.replace('<p class="lede">', '<p class="lede" hidden>'),
        "does not state which agents",
    ),
    "the figure is called false in its own paragraph": (
        lambda html: html.replace(
            "<p>Default output is a few lines;",
            "<p>The following claim is false: Default output is a few lines;",
        ),
        "does not state the one figure",
    ),
    "a second percentage is quoted": (
        lambda html: html.replace(
            "Multiple emulators are supported",
            "Lookups were 0.7%. Multiple emulators are supported",
        ),
        "a percentage besides the one that is measured",
    ),
    "a second percentage is quoted with a space": (
        lambda html: html.replace(
            "Multiple emulators are supported",
            "Savings reach 80 %. Multiple emulators are supported",
        ),
        "a percentage besides the one that is measured",
    ),
    "a second percentage is spelled out": (
        lambda html: html.replace(
            "Multiple emulators are supported",
            "Savings reach 80 percent. Multiple emulators are supported",
        ),
        "a percentage besides the one that is measured",
    ),
    "windows is claimed": (
        lambda html: html.replace(
            "Multiple emulators are supported",
            "Windows works too. Multiple emulators are supported",
        ),
        "the site names Windows",
    ),
    "where it was tested is no longer said": (
        lambda html: html.replace(" " + OS_SENTENCE, ""),
        "does not say, once, 'Developed on macOS; CI runs on Linux.'",
    ),
}


@pytest.mark.parametrize("name", SITE_MUTATIONS)
def test_a_site_that_says_something_else_is_reported(site, measured, name):
    change, words = SITE_MUTATIONS[name]
    changed = change(site)

    assert changed != site, "the control changed nothing"
    problems = site_statement_problems(changed, measured)
    assert _reported(problems, words), problems
    assert len(problems) == 1, f"reported for more than the one thing it changed: {problems}"


# ---------------------------------------------------------------------------
# 6. The site is not published without the suite.
# ---------------------------------------------------------------------------

# The steps that run the suite: test.yml's `test` job, and pages.yml's `check`.
# Written out, not read from either, or the two could change together -- to
# `pytest tests/ --collect-only`, say -- and still agree.
SUITE_STEPS = [
    {"name": "Checkout repository", "uses": "actions/checkout@v4"},
    {
        "name": "Set up Python",
        "uses": "actions/setup-python@v5",
        "with": {
            "python-version": "3.12",
            "cache": "pip",
            "cache-dependency-path": "dev-constraints.txt",
        },
    },
    {
        "name": "Install test dependencies",
        "run": (
            "python -m pip install -c dev-constraints.txt pip\n"
            "pip install -c dev-constraints.txt pytest pillow pyyaml\n"
        ),
    },
    {
        "name": "Run pytest",
        "run": 'echo "🧪 Running unit tests (adb/subprocess mocked)..."\npytest tests/\n',
    },
]

# pages.yml, whole, read as GitHub reads it: `on` is the word, and only `true`
# and `false` are booleans (PyYAML's own reading takes `on:` and `true:` for one
# key, and `enablement: yes` for `true`, which the action does not). The
# `check` job is the suite; `deploy` needs it, checks out the same commit (no
# `ref`), and publishes `./site` of that checkout.
PAGES = {
    "name": "Deploy GitHub Pages",
    "on": {
        "push": {"branches": ["main"], "paths": ["site/**", ".github/workflows/pages.yml"]},
        "workflow_dispatch": None,
    },
    "permissions": {"contents": "read"},
    "concurrency": {"group": "pages", "cancel-in-progress": False},
    "jobs": {
        "check": {
            "name": "Run pytest before the site is published",
            "runs-on": "ubuntu-latest",
            "steps": SUITE_STEPS,
        },
        "deploy": {
            "needs": "check",
            "permissions": {"contents": "read", "pages": "write", "id-token": "write"},
            "environment": {
                "name": "github-pages",
                "url": "${{ steps.deployment.outputs.page_url }}",
            },
            "runs-on": "ubuntu-latest",
            "steps": [
                {"name": "Checkout", "uses": "actions/checkout@v4"},
                {
                    "name": "Configure Pages",
                    "uses": "actions/configure-pages@v5",
                    "with": {"enablement": True},
                },
                {
                    "name": "Upload site artifact",
                    "uses": "actions/upload-pages-artifact@v3",
                    "with": {"path": "./site"},
                },
                {
                    "name": "Deploy to GitHub Pages",
                    "id": "deployment",
                    "uses": "actions/deploy-pages@v4",
                },
            ],
        },
    },
}


def _same(read: object, written: object) -> bool:
    """Equal, and of the same types throughout: YAML's `true` is not the string."""
    if type(read) is not type(written):
        return False
    if isinstance(read, dict):
        return list(read) == list(written) and all(_same(read[k], written[k]) for k in read)
    if isinstance(read, list):
        return len(read) == len(written) and all(map(_same, read, written))
    return read == written


def publish_problems(pages: dict, tests: dict) -> list[str]:
    """Why the site could be published without the suite having run and passed."""
    problems = []
    if not _same(pages, PAGES):
        problems.append(
            "pages.yml is not the workflow written out in tests/test_public_face.py; "
            "an edit to one is an edit to both"
        )
    if not _same(tests.get("jobs", {}).get("test", {}).get("steps"), SUITE_STEPS):
        problems.append("test.yml's `test` job does not run the steps written out as SUITE_STEPS")
    return problems


def _workflow(name: str) -> dict:
    return _yaml_with_the_keys_github_reads((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_the_site_is_published_only_after_the_suite_passed():
    assert publish_problems(_workflow("pages.yml"), _workflow("test.yml")) == []


def _edited(name: str, edit) -> dict:
    workflow = _workflow(name)
    edit(workflow)
    return workflow


PAGES_MUTATIONS = {
    "deploy waits for nothing": lambda pages: pages["jobs"]["deploy"].pop("needs"),
    "deploy runs whatever the check said": lambda pages: pages["jobs"]["deploy"].update(
        {"if": "always()"}
    ),
    "the check may fail": lambda pages: pages["jobs"]["check"].update({"continue-on-error": True}),
    "the check and so the deploy never run": lambda pages: pages["jobs"]["check"].update(
        {"if": "false"}
    ),
    "the check runs a selection": lambda pages: pages["jobs"]["check"]["steps"][-1].update(
        {"run": "pytest tests/ -k site"}
    ),
    "the check only collects": lambda pages: pages["jobs"]["check"]["steps"][-1].update(
        {
            "run": 'echo "🧪 Running unit tests (adb/subprocess mocked)..."\npytest tests/ --collect-only\n'
        }
    ),
    "pytest is told to collect and not run": lambda pages: pages.update(
        {"env": {"PYTEST_ADDOPTS": "--collect-only"}}
    ),
    "deploy publishes another commit": lambda pages: pages["jobs"]["deploy"]["steps"][0].update(
        {"with": {"ref": "HEAD~1"}}
    ),
    "deploy publishes another directory": lambda pages: pages["jobs"]["deploy"]["steps"][2].update(
        {"with": {"path": "./docs"}}
    ),
    "deploy needs a job that is not the suite": lambda pages: pages["jobs"]["deploy"].update(
        {"needs": "lint"}
    ),
    "a third job publishes": lambda pages: pages["jobs"].update(
        {"quick": dict(pages["jobs"]["deploy"], needs=[])}
    ),
    "every job may publish again": lambda pages: pages["permissions"].update({"pages": "write"}),
    "the triggers are under a key GitHub does not read": lambda pages: pages.update(
        {True: pages.pop("on")}
    ),
    "pages are enabled by a word the action does not take for true": lambda pages: pages["jobs"][
        "deploy"
    ]["steps"][1]["with"].update({"enablement": "yes"}),
}


@pytest.mark.parametrize("name", PAGES_MUTATIONS)
def test_a_publishing_path_that_can_skip_the_suite_is_reported(name):
    pages = _edited("pages.yml", PAGES_MUTATIONS[name])

    assert _reported(
        publish_problems(pages, _workflow("test.yml")), "pages.yml is not the workflow"
    )


def test_a_suite_that_stops_running_its_tests_in_both_workflows_is_reported():
    """The mirrored edit: test.yml and pages.yml changed alike still agree with each other."""

    def collect_only(workflow: dict) -> None:
        job = "check" if "check" in workflow["jobs"] else "test"
        workflow["jobs"][job]["steps"][-1]["run"] = "pytest tests/ --collect-only\n"

    problems = publish_problems(
        _edited("pages.yml", collect_only), _edited("test.yml", collect_only)
    )

    assert _reported(problems, "pages.yml is not the workflow")
    assert _reported(problems, "test.yml's `test` job does not run the steps")
