"""tests/verify_install.py: what it refuses, and what it makes of an installed tree.

The check itself needs each agent's CLI, which this suite does not have: the
`Plugin Install Check` workflow runs it for real on every change, and that run
is the proof that a plugin installs. What is tested here is the part that must
hold when things are wrong: a CLI that is absent, at another version, or that
fails a command; a tree that is not the source's; a listing without the skill.

The CLIs' own output comes from `tests/fixtures/recorded/<cli>-<version>/`,
written by `python tests/verify_install.py --record`. A pin that moves has no
recording until the check has passed against the new version, so the tests
below fail on a pin nobody ran.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path

import pytest
import verify_install
from fake_toolchain import FakeToolchain, Rule
from test_packaging_contract import PACKAGE_ROOT, REPO_ROOT
from verify_install import AGENTS, PLUGIN, SKILL, Agent

OTHER_VERSION = "99.98.97"
BY_KEY = {agent.key: agent for agent in AGENTS}
IDS = [agent.key for agent in AGENTS]


def _pins() -> dict[str, str]:
    return verify_install.read_pins(verify_install.PINS.read_text(encoding="utf-8"))


def _recorded(agent: Agent, name: str) -> str:
    """One recorded answer of the pinned version of an agent's CLI, verbatim."""
    profile = f"{agent.profile}-{_pins()[agent.package]}"
    path = verify_install.RECORDED_ROOT / profile / f"{name}.txt"
    if not path.exists():
        pytest.fail(
            f"No recording of `{agent.cli}` at the pinned version: {profile}/{name}.txt is "
            "missing.\nInstall that version and run: python tests/verify_install.py --record\n"
            "Do not substitute a hand-written literal."
        )
    return path.read_text(encoding="utf-8")


# --- the pins ----------------------------------------------------------------


def test_every_agent_has_a_pin_and_every_pin_an_agent():
    assert set(_pins()) == {agent.package for agent in AGENTS}


@pytest.mark.parametrize(
    "line",
    [
        "@openai/codex",
        "@openai/codex@latest",
        "@openai/codex@0.170",
        "@openai/codex@^0.170.0",
        "@openai/codex@0.170.0-alpha.1",
        "@openai/codex@0.170.0 # the CLI",
        " @openai/codex@0.170.0",
        "@openai/codex@0.170.0 ",
        "@openai/codex@0.170.0\r",
        "@openai/codex@0.170.0 @openai/codex@0.170.1",
        "  # an indented comment",
        "codex==0.170.0",
        "@OpenAI/codex@0.170.0",
        # A number npm reads as a tag, digits npm does not read, a leading zero.
        "@openai/codex@99999999999999999.170.0",
        "@openai/codex@1234567890.1.1",
        "@openai/codex@\u0661.\u0662.\u0663",
        "@openai/codex@0.0170.0",
    ],
)
def test_the_pins_file_holds_exact_pins_and_comments_and_nothing_else(line):
    """A line this reader let through and `npm install -g` read another way would be two pins."""
    with pytest.raises(ValueError, match=re.escape("is not `name@X.Y.Z`")):
        verify_install.read_pins(f"# a comment\n\nskills@1.2.3\n{line}\n")


def test_a_package_pinned_twice_is_refused():
    with pytest.raises(ValueError, match="pinned twice"):
        verify_install.read_pins("skills@1.2.3\nskills@1.2.4\n")


def test_the_ids_the_check_installs_are_the_ones_the_marketplace_files_state():
    for relative in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
        marketplace = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        assert marketplace["name"] == verify_install.MARKETPLACE, relative
        assert [plugin["name"] for plugin in marketplace["plugins"]] == [PLUGIN], relative
    assert (PACKAGE_ROOT / "skills" / SKILL / "SKILL.md").is_file()


def test_the_python_the_check_asks_for_is_the_one_the_project_states():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    floor = ".".join(str(part) for part in verify_install.MINIMUM_PYTHON)
    assert project["requires-python"] == f">={floor}"


# --- reading a CLI's answers ---------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_the_recorded_version_answer_states_the_pinned_version(agent):
    pin = _pins()[agent.package]
    answer = _recorded(agent, "version")
    assert verify_install.stated_version(answer) == pin


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_an_answer_that_states_another_version_or_none_is_not_the_pin(agent):
    """Each is the recorded answer with only its version changed."""
    pin = _pins()[agent.package]
    answer = _recorded(agent, "version")
    assert answer.count(pin) == 1
    assert verify_install.stated_version(answer.replace(pin, OTHER_VERSION)) == OTHER_VERSION
    # A pre-release or a build of the pinned version is another version.
    assert verify_install.stated_version(answer.replace(pin, f"{pin}-alpha.1")) is None
    assert verify_install.stated_version(answer.replace(pin, f"{pin}+build.5")) is None
    assert verify_install.stated_version(answer.replace(pin, f"{pin}.1")) is None
    assert verify_install.stated_version(answer.replace(pin, "")) is None
    # The pinned numbers as the tail of another version: a boundary read these as the pin.
    assert verify_install.stated_version(answer.replace(pin, f"{OTHER_VERSION}-{pin}")) is None
    assert verify_install.stated_version(answer.replace(pin, f"{OTHER_VERSION}+{pin}")) is None
    assert verify_install.stated_version(answer.replace(pin, f"v{pin}")) is None
    assert verify_install.stated_version(answer.replace(pin, f"{pin},")) is None
    # A second word that is a version in another script's digits, beside the pin.
    assert verify_install.stated_version(f"{answer} \u0661.\u0662.\u0663") is None
    # Two versions in one answer: which is the CLI's is not this reader's to guess.
    assert verify_install.stated_version(answer + answer.replace(pin, OTHER_VERSION)) is None


def test_claude_codes_recorded_inventory_counts_the_one_skill():
    details = _recorded(BY_KEY["claude"], "plugin_details")
    assert verify_install.claude_lists(details, "")

    line = next(line for line in details.splitlines() if SKILL in line and "Skills" in line)
    assert not verify_install.claude_lists(details.replace(line, ""), "")
    assert not verify_install.claude_lists(details.replace(line, line.replace(SKILL, "other")), "")
    assert not verify_install.claude_lists(details.replace(line, line.replace("(1)", "(0)")), "")
    assert not verify_install.claude_lists(details.replace(line, f"{line} another-skill"), "")


def test_an_answer_that_is_not_json_lists_nothing_for_codex():
    """That Codex's own answer holds the description is the live run's to show.

    The rendered prompt is some 70 KB of Codex's text and is not kept here, and
    a JSON document written by hand in its place would be this test's idea of
    Codex's answer. What can be said without one: an answer that is not JSON
    lists nothing, and the search reaches a string at any depth of any JSON.
    """
    description = verify_install.skill_description(PACKAGE_ROOT / "skills" / SKILL)
    assert description
    assert not verify_install.codex_lists(
        _recorded(BY_KEY["claude"], "plugin_details"), description
    )
    assert not verify_install.codex_lists("", description)

    nested = {"a": [1, None, {"b": ["x", {"c": "y"}]}], "d": "z", "e": 2.5, "f": True}
    assert sorted(verify_install._strings(nested)) == ["x", "y", "z"]
    assert verify_install._strings("alone") == ["alone"] and verify_install._strings(7) == []


# --- reading an installed tree -------------------------------------------------


def _plugin(destination: Path) -> Path:
    shutil.copytree(PACKAGE_ROOT, destination, ignore=shutil.ignore_patterns("__pycache__"))
    return destination


def test_an_installed_copy_of_the_plugin_is_identical_to_it(tmp_path):
    source, installed = _plugin(tmp_path / "source"), _plugin(tmp_path / "installed")
    assert verify_install.tree_problems(source, installed) == []
    assert len(verify_install.files_under(source)) > 70


def _remove(relative: str):
    return lambda installed: (installed / relative).unlink()


def _append(relative: str):
    def change(installed: Path) -> None:
        with open(installed / relative, "ab") as handle:
            handle.write(b"\n")

    return change


def _add(relative: str):
    return lambda installed: (installed / relative).write_text("x", encoding="utf-8")


def _link(relative: str):
    def change(installed: Path) -> None:
        target = installed / relative
        kept = installed.parent / "kept-elsewhere.md"
        target.rename(kept)
        target.symlink_to(kept)

    return change


SKILL_DIR = f"skills/{SKILL}"
TREE_DIFFERENCES = {
    "a reference is missing": (_remove(f"{SKILL_DIR}/references/known-gaps.md"), "is missing"),
    "SKILL.md is missing": (_remove(f"{SKILL_DIR}/SKILL.md"), "is missing"),
    "a manifest is missing": (_remove(".codex-plugin/plugin.json"), "is missing"),
    "a script was changed": (_append(f"{SKILL_DIR}/scripts/navigator.py"), "holds other bytes"),
    "a file was added": (_add(f"{SKILL_DIR}/scripts/extra.py"), "though the source does not"),
    "a file became a link": (_link(f"{SKILL_DIR}/SKILL.md"), "holds other bytes"),
}


@pytest.mark.parametrize("name", TREE_DIFFERENCES)
def test_an_installed_tree_that_differs_from_the_plugin_is_reported(name, tmp_path):
    change, said = TREE_DIFFERENCES[name]
    source, installed = _plugin(tmp_path / "source"), _plugin(tmp_path / "installed")
    change(installed)
    (problem,) = verify_install.tree_problems(source, installed)
    assert said in problem and "1 file(s)" in problem, problem


def test_the_skill_is_found_wherever_a_home_holds_it_and_counted(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert verify_install.installed_skills(home) == []

    cache = home / "plugins" / "cache" / "market" / PLUGIN / "1.2.3"
    _plugin(cache)
    # Another skill's file, and a directory of this name that is not a skill.
    (home / "skills" / ".system" / "other").mkdir(parents=True)
    (home / "skills" / ".system" / "other" / "SKILL.md").write_text("x", encoding="utf-8")
    (home / "logs" / SKILL).mkdir(parents=True)
    assert verify_install.installed_skills(home) == [cache / "skills" / SKILL]

    _plugin(home / "plugins" / "marketplaces" / "market" / PLUGIN)
    assert len(verify_install.installed_skills(home)) == 2


def test_a_script_that_does_not_answer_help_is_reported(tmp_path):
    skill = tmp_path / SKILL
    (skill / "scripts").mkdir(parents=True)
    assert verify_install.script_problems(skill, tmp_path, {})[1] == [
        f"{skill / 'scripts'} holds no Python script"
    ]
    (skill / "scripts" / "broken.py").write_text("import no_such_module\n", encoding="utf-8")
    (skill / "scripts" / "silent.py").write_text("", encoding="utf-8")
    ran, problems = verify_install.script_problems(skill, tmp_path, {})
    assert ran == 2
    assert [problem.split(" ", 1)[0] for problem in problems] == ["broken.py", "silent.py"]


# --- the refusals, with a stub CLI on PATH ---------------------------------------


@pytest.fixture
def fake(tmp_path, monkeypatch) -> FakeToolchain:
    toolchain = FakeToolchain(tmp_path / "toolchain")
    toolchain.apply(monkeypatch)
    return toolchain


def _check(agent: Agent, pin: str | None, tmp_path: Path) -> verify_install.Outcome:
    source = tmp_path / "source"
    if not source.exists():
        _plugin(source / PLUGIN)
    return verify_install.check(agent, pin, source, tmp_path / "scratch" / agent.key)


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_cli_that_is_not_installed_fails_the_row_and_names_the_pin(agent, fake, tmp_path):
    outcome = _check(agent, "1.2.3", tmp_path)
    assert outcome.problems == [
        f"`{agent.cli}` is not on PATH. Install the pinned one: {agent.package}@1.2.3"
    ]


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_cli_at_another_version_is_refused_before_anything_is_installed(agent, fake, tmp_path):
    """Exit criterion 5's second half: the recorded answer, against a pin it does not state."""
    fake.install(agent.cli, [Rule(match=("--version",), stdout=_recorded(agent, "version"))])
    outcome = _check(agent, OTHER_VERSION, tmp_path)

    (problem,) = outcome.problems
    assert f"is {_pins()[agent.package]}, and this check is pinned to {OTHER_VERSION}" in problem
    assert "Nothing was installed." in problem
    assert fake.calls(agent.cli) == [["--version"]]


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_cli_whose_version_cannot_be_read_is_refused(agent, fake, tmp_path):
    fake.install(agent.cli, [], default_exit=3)
    outcome = _check(agent, _pins()[agent.package], tmp_path)
    assert f"`{agent.cli} --version` is exit 3" in outcome.problems[0]
    assert fake.calls(agent.cli) == [["--version"]]


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_an_install_command_that_fails_ends_the_row_there(agent, fake, tmp_path):
    """The first command the agent refuses is reported with its status; the next is not run."""
    fake.install(
        agent.cli,
        [Rule(match=("--version",), stdout=_recorded(agent, "version"))],
        default_exit=1,
    )
    outcome = _check(agent, _pins()[agent.package], tmp_path)

    first = " ".join([agent.cli, *agent.install[0]]).format(source="<source>")
    assert outcome.problems[0].startswith(f"`{first}` exited 1")
    assert len(fake.calls(agent.cli)) == 2
    assert fake.calls(agent.cli)[1][:3] == list(agent.install[0][:3])


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_commands_that_succeed_and_install_nothing_fail_the_row(agent, fake, tmp_path):
    """Exit 0 from every command is not an install: the home is looked at."""
    fake.install(
        agent.cli,
        [Rule(match=("--version",), stdout=_recorded(agent, "version")), Rule(match=())],
    )
    outcome = _check(agent, _pins()[agent.package], tmp_path)
    assert outcome.problems == ["the home holds the skill 0 times, not once: []"]


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_cli_is_handed_an_empty_home_and_nothing_of_the_callers(
    agent, fake, tmp_path, monkeypatch
):
    """Read where the CLI is started: the environment `check` hands each of its commands."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-key")
    monkeypatch.setenv(agent.home_variable, str(tmp_path / "the-callers-own-home"))
    fake.install(
        agent.cli,
        [Rule(match=("--version",), stdout=_recorded(agent, "version")), Rule(match=())],
    )
    handed: list[dict[str, str]] = []
    run = verify_install._run

    def recording(argv, cwd, env):
        handed.append(dict(env))
        return run(argv, cwd, env)

    monkeypatch.setattr(verify_install, "_run", recording)
    _check(agent, _pins()[agent.package], tmp_path)

    home = tmp_path / "scratch" / agent.key / "home"
    assert len(handed) == 1 + len(agent.install), "the version and every install command"
    for env in handed:
        assert set(env) == {"PATH", "HOME", "TMPDIR", agent.home_variable} | {
            name for name, _ in agent.environment
        }
        assert env["HOME"] == str(home) and env[agent.home_variable] == str(home / agent.key)
    assert (home / agent.key).is_dir() and not any((home / agent.key).iterdir())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory whatever its mode")
@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_home_that_cannot_all_be_read_fails_the_row(agent, fake, tmp_path):
    """A directory nobody could list might hold a second copy, or the files that differ."""
    fake.install(
        agent.cli,
        [Rule(match=("--version",), stdout=_recorded(agent, "version")), Rule(match=())],
    )
    locked = tmp_path / "scratch" / agent.key / "home" / agent.key / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0o000)
    try:
        outcome = _check(agent, _pins()[agent.package], tmp_path)
        with pytest.raises(PermissionError):
            verify_install.files_under(locked.parent)
    finally:
        locked.chmod(0o700)
    (problem,) = outcome.problems
    assert problem.startswith("what was installed could not all be read"), problem


def test_a_tree_that_is_not_there_is_an_error_and_not_an_empty_tree(tmp_path):
    with pytest.raises(FileNotFoundError):
        verify_install.files_under(tmp_path / "no-such-directory")
    with pytest.raises(FileNotFoundError):
        verify_install.installed_skills(tmp_path / "no-such-directory")


def _miniature(root: Path, *, broken: bool) -> Path:
    """A plugin of one skill and one or two scripts: enough for `check` to compare and start."""
    skill = root / PLUGIN / "skills" / SKILL
    (skill / "scripts").mkdir(parents=True)
    real = (PACKAGE_ROOT / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    (skill / "SKILL.md").write_text(real, encoding="utf-8")
    (skill / "scripts" / "ok.py").write_text('print("usage: ok")\n', encoding="utf-8")
    if broken:
        (skill / "scripts" / "broken.py").write_text("import no_such_module\n", encoding="utf-8")
    return root


def _check_with_the_plugin_already_in_the_home(agent, fake, tmp_path, *, broken, listing=""):
    """`check` against a stub CLI whose commands all succeed, and a home this test filled.

    A stub cannot install anything, so the copy the agent would have made is
    made here, where `check` will look for it: somewhere under the home.
    """
    source = _miniature(tmp_path / "source", broken=broken)
    home = tmp_path / "scratch" / agent.key / "home" / agent.key
    shutil.copytree(source / PLUGIN, home / "plugins" / "cache" / "market" / PLUGIN / "1.2.3")
    fake.install(
        agent.cli,
        [
            Rule(match=("--version",), stdout=_recorded(agent, "version")),
            Rule(match=agent.listing, stdout=listing),
            Rule(match=()),
        ],
    )
    return verify_install.check(
        agent, _pins()[agent.package], source, tmp_path / "scratch" / agent.key
    )


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_script_that_does_not_start_ends_the_row_before_the_listing(agent, fake, tmp_path):
    outcome = _check_with_the_plugin_already_in_the_home(agent, fake, tmp_path, broken=True)
    (problem,) = outcome.problems
    assert problem.startswith("broken.py --help exited 1 from the installed copy"), problem
    assert outcome.scripts == 2
    assert list(agent.listing) not in fake.calls(agent.cli)


@pytest.mark.parametrize("agent", AGENTS, ids=IDS)
def test_a_listing_without_the_skill_fails_the_row(agent, fake, tmp_path):
    """The plugin is in the home, whole, and its scripts start: the agent still has to list it."""
    outcome = _check_with_the_plugin_already_in_the_home(agent, fake, tmp_path, broken=False)
    (problem,) = outcome.problems
    assert "does not show the skill" in problem, problem
    assert list(agent.listing) in fake.calls(agent.cli)


def test_a_row_passes_when_the_recorded_listing_is_what_the_cli_answers(fake, tmp_path):
    """Claude Code's row end to end, on its recorded answers. Codex's listing is not recorded."""
    agent = BY_KEY["claude"]
    outcome = _check_with_the_plugin_already_in_the_home(
        agent, fake, tmp_path, broken=False, listing=_recorded(agent, "plugin_details")
    )
    assert outcome.problems == []
    assert outcome.version == _pins()[agent.package] and outcome.scripts == 1
    assert set(outcome.printed) == {"version", "plugin_details"}


def test_a_package_with_no_pin_runs_nothing(fake, tmp_path):
    agent = AGENTS[0]
    fake.install(agent.cli, [Rule(match=())])
    outcome = _check(agent, None, tmp_path)
    assert "pins no version" in outcome.problems[0]
    assert fake.calls(agent.cli) == []


# --- the exit status -----------------------------------------------------------


def test_the_check_exits_1_when_the_cli_is_at_another_version(fake, capsys):
    agent = BY_KEY["codex"]
    pin = _pins()[agent.package]
    answer = _recorded(agent, "version").replace(pin, OTHER_VERSION)
    fake.install(agent.cli, [Rule(match=("--version",), stdout=answer)])

    assert verify_install.main(["--agent", agent.key]) == 1
    printed = capsys.readouterr().out
    assert f"{agent.key}: FAILED" in printed
    assert f"is {OTHER_VERSION}, and this check is pinned to {pin}" in printed
    assert fake.calls(agent.cli) == [["--version"]]


def test_the_check_exits_1_when_a_cli_is_missing(fake, capsys):
    assert verify_install.main([]) == 1
    printed = capsys.readouterr().out
    for agent in AGENTS:
        assert f"{agent.key}: FAILED" in printed


@pytest.mark.parametrize("arguments", [["--agent", "skills"], ["--no-such-option"]])
def test_an_argument_the_check_does_not_know_exits_2(arguments):
    with pytest.raises(SystemExit) as stopped:
        verify_install.main(arguments)
    assert stopped.value.code == 2


def test_recording_from_the_working_tree_is_refused():
    assert verify_install.main(["--record", "--working-tree"]) == 2
