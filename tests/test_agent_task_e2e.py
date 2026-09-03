"""One whole agent task, driven through the skill's own CLIs on a real device.

Every other test here checks a part. This checks that the parts compose into the
loop the skill exists to serve:

    see the screen -> act on it -> verify the act landed -> read diagnostics

It runs the scripts as an agent would — as subprocesses, reading what they
print — rather than importing them, because the CLI contract is what an agent
actually consumes. A script that works when imported but not when invoked is
still broken.

Every command in SKILL.md's Quick Start block is walked here, literally, in
order, and `test_quick_start_contract.py` enforces that: the documented path and
the verified path have to be the same path. In particular step 3 takes its
target from what step 2 *printed* in its default output, not from `--json` and
not from a literal in this file — that is the loop, and it is the one the agent
is told to run.

The target is the Compose fixture app (`tests/fixtures/scaffold/compose/`),
deliberately: Jetpack Compose is the default Android UI toolkit, and until R11
was fixed `screen_mapper` reported ~0 interactive elements on it, so this task
was impossible to complete. The app must be installed:

    cd tests/fixtures/scaffold/compose && gradle :app:installDebug

The device-backed tests are marked `emulator`, so they are deselected unless
run with `-m emulator`. The mark is per test rather than on the module, so the
one piece of judgement in the fixture -- how two spellings of one component are
compared -- is checked by the mocked lane, where a mistake in it is cheap to
find.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The independent resolver from the fixture-driven loop spec, imported rather
# than re-implemented: the rectangle a tap must land in is computed from the
# hierarchy by code that knows nothing about navigator, and there must be
# exactly one such resolver or the live lane and the mocked one can disagree
# about what "the right control" means.
from test_agent_loop_from_output import _expected_rect

from common.device_utils import parse_focused_activity
from common.hierarchy import capture_hierarchy

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "android-emulator-skill"
    / "skills"
    / "android-emulator-skill"
    / "scripts"
)

APP = "com.example.composefixture"
ACTIVITY = f"{APP}/.DefaultActivity"
STEP_TIMEOUT = 120

# How long to wait for the launched activity to actually reach the front.
# `app_launcher --launch` now waits for it (E1, via `am start -W`), so this
# should return on the first poll; it stays because "displayed" and "focused"
# are not the same instant, and because a lane that mislabels a timing race as a
# regression in the mapper costs more to diagnose than this loop costs to run.
FOCUS_TIMEOUT = 30
FOCUS_POLL_SECONDS = 0.5

# Names the screen report prints are quoted; the header line carries counts.
_QUOTED = re.compile(r'"([^"]*)"')


@pytest.fixture(scope="session")
def compose_device(adb: str) -> str:
    """The attached device that has the fixture app.

    Deliberately searches rather than taking the first device: a developer
    machine often has both an emulator and a physical phone attached, and the
    app is only on one of them. Taking `live_device` here made this test skip
    while the app sat installed on the emulator.
    """
    listing = subprocess.run(
        [adb, "devices"], capture_output=True, text=True, timeout=20, check=False
    )
    serials = [
        line.split()[0]
        for line in listing.stdout.splitlines()[1:]
        if line.strip() and line.split()[-1] == "device"
    ]
    if not serials:
        pytest.skip("no booted device; start one and re-run with -m emulator")

    for serial in serials:
        packages = subprocess.run(
            [adb, "-s", serial, "shell", "pm", "list", "packages", APP],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if APP in packages.stdout:
            return serial

    pytest.skip(
        f"{APP} is not installed on any attached device ({', '.join(serials)}). "
        f"Build and install it with:\n"
        f"  cd tests/fixtures/scaffold/compose && gradle :app:installDebug"
    )


@pytest.fixture
def run_skill(compose_device: str):
    """Invoke one of the skill's scripts the way an agent would."""

    def _run(script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), "--serial", compose_device, *args],
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT,
            check=False,
        )

    return _run


def _expand_component(component: str | None) -> str | None:
    """A ``package/class`` string with the class spelled in full.

    The two sources disagree about how to write the same component, measured on
    the lane: ``am start -W`` echoes the class as it was passed --
    ``com.example.composefixture/.DefaultActivity`` -- while ``dumpsys window``
    prints ``com.example.composefixture/com.example.composefixture.DefaultActivity``.
    A leading dot is Android's shorthand for "in this package", and a name with
    no dot at all is the same shorthand written without it, so both are expanded
    before anything is compared. Comparing the raw strings would report a
    correctly launched activity as the wrong screen.
    """
    if not component or "/" not in component:
        return component
    package, _, class_name = component.partition("/")
    if class_name.startswith("."):
        class_name = package + class_name
    elif "." not in class_name:
        class_name = f"{package}.{class_name}"
    return f"{package}/{class_name}"


def _focused_activity(adb: str, serial: str) -> str | None:
    """What the device says is in front, read through the shared parser.

    `dumpsys window` is asked directly rather than through a skill script
    because this is the test's own instrument: if the mapper is blind, the
    diagnosis needs a source the mapper does not touch.
    """
    dumped = subprocess.run(
        [adb, "-s", serial, "shell", "dumpsys", "window"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return parse_focused_activity(dumped.stdout)


@pytest.fixture
def compose_app(run_skill, adb: str, compose_device: str):
    """Launch the fixture app and wait until it is the focused activity.

    `--launch` waits for the activity to be displayed, and this polls until the
    window manager agrees it is focused. Without the wait, `screen_mapper` ran
    against whatever was still in front -- on a slow hosted runner, the launcher
    or a splash -- and the failure was reported as "R11 has regressed", a
    diagnosis the evidence did not support (E1).

    Returns the package, and the focused activity as the test last saw it, so a
    shortfall can say which screen was actually mapped.
    """
    # Force-stop first. The lane boots from a saved snapshot, so whatever state
    # the app was left in at the end of the previous run comes back with it --
    # including an ANR dialog, whose "Close app" / "Wait" buttons are the only
    # two controls the mapper can then see. Terminating is how the run starts
    # from a screen this test is entitled to make claims about.
    run_skill("app_launcher.py", "--terminate", APP)

    launched = run_skill("app_launcher.py", "--launch", ACTIVITY, "--json")
    assert launched.returncode == 0, f"could not launch: {launched.stderr}"

    # The activity `am start -W` RESOLVED, not the one that was asked for. They
    # differ whenever the request names an alias -- the recorded capture shows
    # `.Settings` resolving to `.homepage.SettingsHomepageActivity` -- so the
    # requested string is the wrong thing to wait for, and "any activity in the
    # package" is too weak to catch a launch that landed on the wrong screen.
    started = json.loads(launched.stdout)
    resolved = started.get("activity")
    assert resolved, f"--launch --json did not report the activity it started: {started}"

    wanted = _expand_component(resolved)
    deadline = time.monotonic() + FOCUS_TIMEOUT
    focused = _focused_activity(adb, compose_device)
    while _expand_component(focused) != wanted and time.monotonic() < deadline:
        time.sleep(FOCUS_POLL_SECONDS)
        focused = _focused_activity(adb, compose_device)

    assert _expand_component(focused) == wanted, (
        f"am start reported {resolved!r} as displayed, but {focused!r} is focused "
        f"after {FOCUS_TIMEOUT}s. Nothing below this point would be measuring the "
        f"fixture app."
    )
    return focused


def _names_printed(report: str) -> list[str]:
    """The control names the default screen report puts in front of the agent.

    The report's grammar is ``Section: "name", "name"``; the header line carries
    counts rather than names, so it is excluded. This is deliberately a parser
    over the DEFAULT output and not a read of ``--json``: Quick Start step 2
    passes no flags, and a test that seeds step 3 from JSON certifies a path the
    agent was never told to take.
    """
    return [
        name
        for line in report.splitlines()
        if not line.startswith("Screen:")
        for name in _QUOTED.findall(line)
        if name and not name.startswith("...")
    ]


@pytest.mark.emulator
def test_an_agent_can_complete_a_task_on_a_compose_screen(run_skill, compose_app, compose_device):
    """Quick Start, run as written, in the order an agent would run it.

    Each step asserts on what the previous one produced, so a break anywhere is
    attributed to the step that caused it rather than surfacing as a vague
    end-of-test failure. Step 3 in particular consumes a name step 2 printed:
    that hand-off is the loop, and it is where C1 and C2 lived through a green
    suite.
    """
    focused = compose_app

    # --- 2. See the screen (Quick Start step 2: no flags) ------------------
    mapped = run_skill("screen_mapper.py")
    assert mapped.returncode == 0, f"screen_mapper failed: {mapped.stderr}"

    printed = _names_printed(mapped.stdout)
    assert len(printed) >= 6, (
        f"the default screen report names {len(printed)} controls on a Compose "
        f"screen with 7. Focused activity: {focused!r} -- if that is not "
        f"{ACTIVITY} the wrong screen was in front and this is a launch race "
        f"(E1), not the mapper. Report was:\n{mapped.stdout.strip()}"
    )

    target = next((name for name in printed if "Submit Order" in name), None)
    assert target is not None, (
        f"the button an agent would tap is not among the names the report " f"printed: {printed}"
    )

    # --- 3. Tap what the report named (Quick Start step 3) -----------------
    #
    # The name goes back in verbatim, and the tap is checked against the
    # hierarchy rather than against navigator's own account of it. The screen is
    # captured the way the skill captures it, the target's rectangle is derived
    # from that capture by the independent resolver, and the coordinates come
    # from `--json`. A test that read the `Tapped:` line for both the claim and
    # the check could not see the tool tapping the wrong thing.
    before = capture_hierarchy(compose_device)
    expected = _expected_rect(before, target)

    tapped = run_skill("navigator.py", "--find-text", target, "--tap", "--json")
    assert tapped.returncode == 0, (
        f"a name the screen report printed could not be tapped: {target!r}\n"
        f"  stdout: {tapped.stdout.strip()}\n  stderr: {tapped.stderr.strip()}"
    )

    result = json.loads(tapped.stdout)
    assert target in result["message"], (
        f"the tap reported a different control than the one named: "
        f"asked for {target!r}, got {result['message']!r}"
    )

    x, y = result["tapped_at"]
    left, top, right, bottom = expected
    assert left <= x <= right and top <= y <= bottom, (
        f"the tap landed outside the control the name belongs to.\n"
        f"  name:    {target!r}\n"
        f"  tapped:  ({x}, {y})\n"
        f"  control: {expected}\n"
        f"  said:    {result['message']!r}"
    )

    # --- 4. Enter text (Quick Start step 4) --------------------------------
    typed = run_skill(
        "navigator.py", "--find-type", "EditText", "--enter-text", "agent@example.com"
    )
    assert typed.returncode == 0, f"could not type into the field: {typed.stderr}"

    # --- Verify the act landed ---------------------------------------------
    after = json.loads(run_skill("screen_mapper.py", "--json").stdout)
    filled = [field for field in after["edit_texts"] if field["filled"]]
    assert (
        filled
    ), f"typed into the field but no EditText reports being filled: {after['edit_texts']}"

    # --- 5. Accessibility audit (Quick Start step 5) -----------------------
    #
    # Exit 1 is the documented CI gate for "there are criticals", so the floor
    # here is a usable verdict rather than a zero status. A Compose screen used
    # to produce zero criticals structurally, because the check gated on widget
    # class names that Compose never emits (L3); now the number means something,
    # whichever number it is.
    audited = run_skill("accessibility_audit.py")
    combined = audited.stdout + audited.stderr
    assert "Traceback" not in combined, f"a stack trace is not an audit:\n{combined}"
    assert audited.returncode in (0, 1), f"unexpected exit {audited.returncode}: {combined}"
    assert "Accessibility:" in audited.stdout, f"no audit verdict was printed:\n{combined}"

    # --- 6. Read diagnostics -----------------------------------------------
    logs = run_skill("log_monitor.py", "--duration", "3s", "--json")
    assert logs.returncode == 0, f"log_monitor failed: {logs.stderr}"

    parsed = json.loads(logs.stdout)
    statistics = parsed["statistics"]
    assert (
        statistics["total_lines"] > 0
    ), "log_monitor parsed zero lines from a live device -- A1 has regressed"
    # Counts only move off zero if lines were actually parsed, not merely read.
    assert (
        statistics["errors"] + statistics["warnings"] + statistics["info"] > 0
    ), f"lines were read but none classified, which is the A1 signature: {statistics}"


def test_the_two_spellings_of_one_component_compare_equal():
    """The comparison above, held to the pair the lane produced.

    Not a device test: it is the one line of judgement in the fixture, and it
    got the lane wrong once already by comparing the strings as written.
    """
    reported = "com.example.composefixture/.DefaultActivity"
    focused = "com.example.composefixture/com.example.composefixture.DefaultActivity"
    assert _expand_component(reported) == _expand_component(focused)

    # An alias resolves to a genuinely different class, and must NOT compare
    # equal to the name that was requested -- that is what the wait is for.
    assert _expand_component("com.android.settings/.Settings") != _expand_component(
        "com.android.settings/.homepage.SettingsHomepageActivity"
    )


@pytest.mark.emulator
def test_the_agent_gets_an_actionable_error_when_it_targets_nothing(run_skill):
    """The other half of usable: failing in a way the agent can act on."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "screen_mapper.py"), "--serial", "no-such-device"],
        capture_output=True,
        text=True,
        timeout=STEP_TIMEOUT,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, "a failed screen read reported success"
    assert "Traceback" not in combined, f"a stack trace is not actionable:\n{combined}"
    assert "no-such-device" in combined
