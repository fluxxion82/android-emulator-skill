"""One whole agent task, driven through the skill's own CLIs on a real device.

Every other test here checks a part. This checks that the parts compose into the
loop the skill exists to serve:

    see the screen -> act on it -> verify the act landed -> read diagnostics

It runs the scripts as an agent would — as subprocesses, parsing their `--json`
output — rather than importing them, because the CLI contract is what an agent
actually consumes. A script that works when imported but not when invoked is
still broken.

The target is the Compose fixture app (`tests/fixtures/scaffold/compose/`),
deliberately: Jetpack Compose is the default Android UI toolkit, and until R11
was fixed `screen_mapper` reported ~0 interactive elements on it, so this task
was impossible to complete. The app must be installed:

    cd tests/fixtures/scaffold/compose && gradle :app:installDebug

Marked `emulator`, so it is deselected unless run with `-m emulator`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.emulator

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


@pytest.fixture
def compose_app(run_skill):
    """Launch the fixture app on the device that has it."""
    launched = run_skill("app_launcher.py", "--launch", ACTIVITY)
    assert launched.returncode == 0, f"could not launch: {launched.stderr}"
    return APP


def test_an_agent_can_complete_a_task_on_a_compose_screen(run_skill, compose_app):
    """The whole loop, in the order an agent would perform it.

    Each step asserts on what the previous one produced, so a break anywhere is
    attributed to the step that caused it rather than surfacing as a vague
    end-of-test failure.
    """
    # --- 1. See the screen -------------------------------------------------
    mapped = run_skill("screen_mapper.py", "--json")
    assert mapped.returncode == 0, f"screen_mapper failed: {mapped.stderr}"

    screen = json.loads(mapped.stdout)
    assert "error" not in screen, screen.get("error")
    assert screen["interactive_elements"] >= 6, (
        f"only {screen['interactive_elements']} interactive elements on a Compose "
        f"screen with 7 controls -- R11 has regressed and the agent is blind"
    )

    # The agent picks its target from the labels the mapper reported.
    labels = [
        label
        for cls, values in screen["elements_by_type"].items()
        if cls != "TextView"
        for label in values
    ]
    assert any(
        "Submit Order" in label for label in labels
    ), f"the button an agent would tap is not among the reported controls: {labels}"

    # --- 2. Act on it ------------------------------------------------------
    typed = run_skill(
        "navigator.py", "--find-type", "EditText", "--enter-text", "agent@example.com"
    )
    assert typed.returncode == 0, f"could not type into the field: {typed.stderr}"

    # --- 3. Verify the act landed ------------------------------------------
    after = json.loads(run_skill("screen_mapper.py", "--json").stdout)
    filled = [field for field in after["edit_texts"] if field["filled"]]
    assert (
        filled
    ), f"typed into the field but no EditText reports being filled: {after['edit_texts']}"

    # --- 4. Read diagnostics -----------------------------------------------
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
