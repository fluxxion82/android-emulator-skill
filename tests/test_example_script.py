"""The example script that ships with the skill is run, start to finish.

``examples/login_flow_example.sh`` called itself "a complete login workflow".
It slept for guessed times, decided whether a lookup had worked by grepping its
prose for ``Found:``, ignored every failure, and ended by printing commands
that are file-not-found from anywhere but the scripts directory. It is now the
loop SKILL.md gives an agent -- launch, wait, act, check -- written around
``--wait-for`` and ``--tap --expect``, and this module is what "exercised by a
test" means for it:

- it is run as a user runs it, under bash, with a fake ``adb`` on PATH that
  answers from recordings: Settings' launch, its home screen, and the Apps
  screen a tap on "Apps" opens (``uiautomator_settings_apps``, recorded by
  making that tap);
- the one tap it sends has to land in the row the name belongs to, by a
  rectangle read from the recording and not from the script's output;
- a step that fails has to end the run, with that step's status and nothing
  sent to the device after it -- each of the five steps in turn;
- it must not wait by the clock or read a script's prose: `sleep` and `grep`
  are on its PATH as stubs that record being called, and are not called;
- every command it prints for the reader is, whole line, the rooted form
  SKILL.md writes, with flags the script accepts (the rule the reference
  files are held to; a flag a script accepts and the index leaves out, as it
  does on purpose for two, is not refused here).

Not covered here: a real device. The script was run once by hand against an
API 35 emulator when it was written; nothing repeats that.
"""

from __future__ import annotations

import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fake_toolchain import FakeToolchain, Rule
from test_agent_loop_from_output import _expected_rect, _inside
from test_skill_index import _commands_in, command_line_problems

SKILL = Path(__file__).resolve().parents[1] / "android-emulator-skill/skills/android-emulator-skill"
EXAMPLE = SKILL / "examples" / "login_flow_example.sh"
SKILL_MD = SKILL / "SKILL.md"

HOME = "uiautomator_settings_top"
APPS = "uiautomator_settings_apps"
# The name the screen report gives the row that "Apps" sits in.
APPS_ROW = "Apps Assistant, recent apps, default apps"

DUMP = ("uiautomator", "dump")
# How the launcher starts Settings: it knows that package's launcher activity
# and does not ask the device which it is.
LAUNCH = ("am", "start", "-W", "-n", "com.android.settings/.Settings")


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    """A fake adb, and the interpreter the suite runs on as ``python3``.

    The stub PATH is ``<stubs>:/usr/bin:/bin``, and ``/usr/bin/python3`` is
    whatever the host ships -- older than these scripts need on the machine
    this was written on. The example asks for ``python3`` by name, as a user's
    shell would resolve it, so the name is pointed at this interpreter.
    """
    chain = FakeToolchain(tmp_path / "toolchain")
    shim = chain.bin / "python3"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return chain


def _settings(recorded, *screens: str) -> list[Rule]:
    """adb as Settings answers it: the launch, then each dump in the order given."""
    dumps = [Rule(DUMP, stdout=recorded.text(name), once=True) for name in screens[:-1]]
    return [
        Rule(("force-stop",)),
        Rule(LAUNCH, stdout=recorded.text("am_start_wait_settings")),
        *dumps,
        Rule(DUMP, stdout=recorded.text(screens[-1])),
        Rule(("input", "tap")),
        Rule(("input", "keyevent")),
    ]


def _device_calls(chain: FakeToolchain) -> list[list[str]]:
    """What reached adb, less the dumps' own time limits (they depend on the clock)."""
    calls = []
    for argv in chain.calls("adb"):
        if "uiautomator" in argv:
            calls.append(["dump"])
        else:
            calls.append([word for word in argv if word != "shell"])
    return calls


def test_the_example_runs_to_the_end_and_taps_the_row_it_names(toolchain, recorded):
    toolchain.install("adb", _settings(recorded, HOME, HOME, APPS, HOME))

    result = toolchain.run(EXAMPLE)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = _device_calls(toolchain)
    assert [call[0] for call in calls] == [
        "am",  # force-stop
        "am",  # start it, and wait for it
        "dump",  # wait for the first screen
        "dump",  # find "Apps"
        "input",  # tap it
        "dump",  # the screen that followed
        "input",  # back
        "dump",  # the first screen again
    ], calls
    (tap,) = [call for call in calls if call[:2] == ["input", "tap"]]
    row = _expected_rect(ET.fromstring(recorded.text(HOME)), APPS_ROW)
    assert _inside(row, int(tap[2]), int(tap[3])), f"tapped {tap[2:]}, outside the Apps row {row}"
    assert [call for call in calls if call[:2] == ["input", "keyevent"]] == [
        ["input", "keyevent", "KEYCODE_BACK"]
    ]
    assert "Found: text='Default apps'" in result.stdout
    assert result.stdout.count("Found: text='Connected devices'") == 2


def test_the_text_it_waits_for_after_the_tap_is_on_one_recording_and_not_the_other(recorded):
    """Or the wait in the middle of the example would prove nothing about the tap."""
    assert "Default apps" in recorded.text(APPS)
    assert "Default apps" not in recorded.text(HOME)
    assert "Connected devices" in recorded.text(HOME)
    assert "Connected devices" not in recorded.text(APPS)


def _recorded_status(recorded, name: str) -> int:
    (entry,) = [entry for entry in recorded.manifest["fixtures"] if entry["name"] == name]
    return entry["exit_status"]


def test_a_launch_that_is_refused_ends_the_run_with_its_status_and_touches_nothing_more(
    toolchain, recorded
):
    """The launch is answered as `am start` answers a component that does not exist.

    The recording (`am_start_wait_missing`) is of another component's refusal,
    with the exit status it was recorded at. It stands in for Settings'
    launch here: what this needs is a launch the device refused, and the
    launcher reads the refusal, not the name in it.
    """
    refused = Rule(
        LAUNCH,
        stdout=recorded.text("am_start_wait_missing"),
        exit=_recorded_status(recorded, "am_start_wait_missing"),
    )
    assert refused.exit == 1, "the recording's exit status changed; so does what this replays"
    rules = [refused if rule.match == LAUNCH else rule for rule in _settings(recorded, HOME)]
    assert refused in rules, "the launch was not replaced"
    toolchain.install("adb", rules)

    result = toolchain.run(EXAMPLE)

    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "Stopped: that command exited 1." in result.stderr
    assert "Error: " in result.stderr and "Remedy: " in result.stderr
    assert [call[:2] for call in _device_calls(toolchain)] == [
        ["am", "force-stop"],
        ["am", "start"],
    ], "the run went on after a step failed"
    assert "Done." not in result.stdout


def _gone(recorded_anywhere, *match: str) -> Rule:
    """A stand-in for a device that went away, answering whatever matches.

    What is replayed is adb's recorded diagnostic for a serial that is not
    attached (`adb_device_not_found`, recorded from `adb -s <serial>
    get-state`), at exit 1. It was not recorded from a dump or a key press
    whose device vanished mid-run, and the example here is run without a
    serial: this is a failure put there on purpose, in adb's real words for
    the nearest thing, so that a step exits 3 at a chosen point.
    """
    return Rule(match, stderr=recorded_anywhere("adb_device_not_found"), exit=1)


# Each step after the launch, failed in turn: (what the device stops answering, how many
# dumps it answers first, what reached it before the run stopped).
LATER_STEPS = {
    "2: the first wait": (DUMP, 0, ["am", "am", "dump"]),
    "3: the tap": (("input", "tap"), 2, ["am", "am", "dump", "dump", "input"]),
    "3: the wait after the tap": (DUMP, 2, ["am", "am", "dump", "dump", "input", "dump"]),
    "4: the key press": (
        ("input", "keyevent"),
        3,
        ["am", "am", "dump", "dump", "input", "dump", "input"],
    ),
    "5: the last wait": (
        DUMP,
        3,
        ["am", "am", "dump", "dump", "input", "dump", "input", "dump"],
    ),
}


@pytest.mark.parametrize("step", LATER_STEPS)
def test_a_later_step_that_fails_ends_the_run_too(toolchain, recorded, recorded_anywhere, step):
    """Whichever step it is, and whichever script: the run stops there, at that status."""
    failing, dumps_first, reached = LATER_STEPS[step]
    screens = [HOME, HOME, APPS][:dumps_first]
    rules = [
        Rule(("force-stop",)),
        Rule(LAUNCH, stdout=recorded.text("am_start_wait_settings")),
        *(Rule(DUMP, stdout=recorded.text(name), once=True) for name in screens),
        _gone(recorded_anywhere, *failing),
        Rule(("input", "tap")),
        Rule(("input", "keyevent")),
        Rule(("devices",), stdout=recorded_anywhere("adb_devices_single")),
    ]
    toolchain.install("adb", rules)

    result = toolchain.run(EXAMPLE)

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert "Stopped: that command exited 3." in result.stderr
    calls = [call[0] for call in _device_calls(toolchain) if call != ["devices"]]
    assert calls == reached, calls
    assert "Done." not in result.stdout


def test_the_example_finds_its_scripts_whatever_cdpath_is(toolchain, recorded):
    """Run by a relative path with CDPATH set, `cd` prints where it went: into SKILL_DIR."""
    toolchain.install("adb", _settings(recorded, HOME, HOME, APPS, HOME))
    repository = SKILL.parents[2]

    result = toolchain.run(EXAMPLE.relative_to(repository), cwd=repository, CDPATH=".")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "Done." in result.stdout


def test_a_device_that_is_not_there_ends_the_run_at_exit_3(toolchain, recorded_anywhere):
    toolchain.install("adb", [_gone(recorded_anywhere)])

    result = toolchain.run(EXAMPLE, "no-such-serial-xyz")

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert "Stopped: that command exited 3." in result.stderr
    assert "--serial no-such-serial-xyz" in result.stdout, "the serial was not shown in the command"


def _printed_lines(stdout: str) -> list[str]:
    """Everything the example prints, each line as the reader would paste it."""
    return [line.removeprefix("$ ") for line in stdout.splitlines()]


def test_every_command_it_prints_is_the_one_form_with_flags_the_script_accepts(toolchain, recorded):
    """Every printed line that names a script, whole, by the rule the reference files are held to."""
    toolchain.install("adb", _settings(recorded, HOME, HOME, APPS, HOME))
    skill_md = SKILL_MD.read_text(encoding="utf-8")

    printed = _printed_lines(toolchain.run(EXAMPLE).stdout)

    assert command_line_problems(EXAMPLE.name, printed, skill_md) == []
    commands = _commands_in([line for line in printed if "$SKILL_DIR/scripts/" in line])
    assert len(commands) == 9, commands  # five steps run, four in the closing recipe
    assert ("navigator.py", frozenset({"--find-exact", "--tap", "--expect"})) in commands
    assert ("navigator.py", frozenset({"--wait-for"})) in commands

    for stray, words in (
        ("python navigator.py --list", "nothing else on the line"),
        ("cd scripts && ./screen_mapper.py", "nothing else on the line"),
        ("python3 '$SKILL_DIR/scripts/app_launcher.py' --list", "nothing else on the line"),
        ('python3 "$SKILL_DIR/scripts/navigator.py" --teleport', "passes ['--teleport']"),
    ):
        problems = command_line_problems(EXAMPLE.name, [*printed, stray], skill_md)
        assert any(words in problem for problem in problems), (stray, problems)


def test_the_example_waits_for_text_and_never_for_a_guessed_time(toolchain, recorded):
    """`sleep` and `grep` are on its PATH as stubs that record being called; neither is.

    Asked of what it runs, not of how it is spelled: a check of the script's
    words missed `command "sleep" 0.01`. The scripts it starts are Python and
    pause with `time.sleep`, which is not this.
    """
    toolchain.install("adb", _settings(recorded, HOME, HOME, APPS, HOME))
    toolchain.install("sleep", [Rule(())], default_exit=0)
    toolchain.install("grep", [Rule(())], default_exit=0)

    result = toolchain.run(EXAMPLE)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert toolchain.calls("sleep") == [], "the example waits by the clock"
    assert toolchain.calls("grep") == [], "the example reads a script's prose"
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "--wait-for" in text and "--expect" in text
