"""Every documented mode, with adb failing: does the CLI say so in its exit status?

The finding this pins (C6, X5, X8, X3, L8) is one shape in four places: a
failure converted into an empty result, printed calmly, and reported as
success. ``app_launcher --list`` answers ``Installed packages (0)`` when adb
never ran; ``device_list --json`` answers ``{"devices": [], "avds": []}`` when
the SDK is not installed. An agent cannot tell "nothing is there" from "I could
not look", and it has no second signal to consult -- exit status is the signal.

**Why this is a runtime sweep and not another AST guard.** The obvious static
rule -- "a function that builds a dict with an ``error`` key must not fall
through to ``sys.exit(0)``" -- was written down, costed, and dropped. It is not
expressible: the dict is built in one function, returned through two more, and
the exit is decided by a ``main()`` that never sees the key. Both the
adjudicating reviewer and the plan critic rejected it as inexpressible before a
line of it existed. What *is* checkable is the thing the agent experiences, so
this runs the real CLI, as a subprocess, against an adb that fails.

**The fake adb is a fault injector, not a simulator.** It answers every
invocation the same way -- the failure this sweep is about is "adb did not
work", and which subcommand was asked does not change that. The one piece of
tool *text* it emits is read from ``tests/fixtures/recorded/``
(``adb_shell_device_not_found``), not written here: CLAUDE.md's rule applies to
a test double's output as much as to a parser's input, and this file would
otherwise have hard-coded ``error: device ... not found`` -- which is the wrong
prefix. Real adb says ``adb:``; ``error:`` belongs to ``adb get-state``. That
exact mistake is already recorded in ``test_fixture_policy``'s debt notes.

``emulator``, ``avdmanager`` and ``sdkmanager`` are faked as exit 127, which is
the "SDK tool is not installed" case X3 and L8 are about.

Each mode also asserts that a fake tool was *invoked*, so a script that answers
without ever looking cannot pass the sweep vacuously. The two modes that
legitimately touch no tool say so in the table.

Modes that are red today carry ``xfail(strict=True)`` individually, named for
the finding, so the sweep merges green while stating exactly which eight of the
twenty-three modes lie about their exit status.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"

# The fake tools exit immediately, so a mode that takes longer than this is
# hung rather than slow -- which is itself a defect worth failing on.
MODE_TIMEOUT_SECONDS = 60

# Where each fake appends a line naming itself. Read back per mode to prove the
# script actually reached for a tool.
TOOL_LOG_VAR = "SWEEP_TOOL_LOG"

# Substituted with the mode's own working directory before the script runs, so
# nothing writes into the repo.
WORKDIR = "<workdir>"

_FAKE_ADB = """#!/bin/sh
printf 'adb %s\\n' "$*" >> "$SWEEP_TOOL_LOG"
cat <<'RECORDED' >&2
{recorded}
RECORDED
exit 1
"""

_FAKE_MISSING_TOOL = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$SWEEP_TOOL_LOG"
echo "$(basename "$0"): command not found" >&2
exit 127
"""

MISSING_TOOLS = ("emulator", "avdmanager", "sdkmanager")


class Mode(NamedTuple):
    """One documented invocation, and what it must do when the tool fails."""

    script: str
    argv: tuple[str, ...]
    tool: str | None  # the fake that must be invoked; None with a reason below
    reason: str  # why no tool, or what the mode is for
    defect: str | None = None  # finding ID when this mode is red today

    @property
    def ident(self) -> str:
        return f"{self.script.removesuffix('.py')} {' '.join(self.argv)}".strip()


# The sweep table. `defect` is filled from an unmarked run, not from a guess:
# every entry below was observed, and the eight marked ones printed a zero exit
# status with an empty-looking answer.
MODES: tuple[Mode, ...] = (
    # --- C6: app_launcher ---------------------------------------------------
    Mode("app_launcher.py", ("--list",), "adb", "package listing", defect="C6"),
    Mode("app_launcher.py", ("--list", "--json"), "adb", "package listing", defect="C6"),
    # --state and --launch already exit 1 here: the injected failure is a
    # device error, which main() maps before the state path can swallow it.
    # C6's second half (an adb call that RAN and failed) needs a recorded
    # "command failed on a live device" fixture, which the corpus lacks.
    Mode("app_launcher.py", ("--state", "com.example.app"), "adb", "foreground check"),
    Mode("app_launcher.py", ("--state", "com.example.app", "--json"), "adb", "foreground check"),
    Mode("app_launcher.py", ("--launch", "com.example.app"), "adb", "activity launch"),
    Mode("app_launcher.py", ("--launch", "com.example.app", "--json"), "adb", "activity launch"),
    # --- the core loop ------------------------------------------------------
    Mode("screen_mapper.py", (), "adb", "default text output, Quick Start step 2"),
    Mode("screen_mapper.py", ("--json",), "adb", "machine-readable screen map"),
    Mode("navigator.py", ("--find-text", "x", "--json"), "adb", "Quick Start step 3"),
    # --- X5: anr_watcher session store --------------------------------------
    Mode(
        "anr_watcher.py",
        ("--get-details", "no-such-session", "--json"),
        None,
        "reads the on-disk session store under $HOME; no device is involved",
        defect="X5",
    ),
    Mode(
        "anr_watcher.py",
        ("--diff", "a", "b", "--json"),
        None,
        "reads the on-disk session store under $HOME; no device is involved",
        defect="X5",
    ),
    # --- X8: app_state_capture ----------------------------------------------
    # Exits 1 here because the whole capture fails at the device level. X8 is
    # the PARTIAL capture -- a snapshot that succeeded except for the logs it
    # was asked for -- which needs a device that answers. Named so the gap is
    # visible rather than assumed covered.
    Mode(
        "app_state_capture.py",
        ("--package", "com.example.app", "--output", f"{WORKDIR}/snapshots", "--json"),
        "adb",
        "whole-capture failure; the partial-capture case (X8) is out of reach here",
    ),
    # --- X3 / L8: the lifecycle scripts with no SDK -------------------------
    Mode("device_list.py", ("--json",), "adb", "device and AVD inventory", defect="X3"),
    Mode("emulator_boot.py", ("--list-avds", "--json"), "emulator", "AVD listing", defect="X3"),
    Mode(
        "emulator_selector.py",
        ("--suggest", "--json"),
        "emulator",
        "AVD suggestion",
        defect="X3",
    ),
    Mode(
        "emulator_delete.py",
        ("--all", "--yes", "--json"),
        "avdmanager",
        "batch delete with no avdmanager",
        defect="L8",
    ),
    # --- diagnostics --------------------------------------------------------
    Mode("log_monitor.py", ("--last", "1m", "--json"), "adb", "historical log window"),
    Mode("crash_triage.py", ("--json",), "adb", "crash buffer triage"),
    # --- emulator console and device services -------------------------------
    Mode("snapshot.py", ("--list", "--json"), "adb", "snapshot listing via the console"),
    Mode("sms.py", ("--list", "--json"), "adb", "inbox read"),
    Mode(
        "privacy_manager.py",
        ("--list", "--package", "com.example.app", "--json"),
        "adb",
        "permission listing",
    ),
    Mode("status_bar.py", ("--reset", "--json"), "adb", "demo-mode reset"),
    Mode(
        "container.py",
        ("--package", "com.example.app", "--ls", "--json"),
        "adb",
        "app sandbox listing",
    ),
)


class FakeSdk(NamedTuple):
    """A PATH where every Android tool fails, and an SDK root with nothing in it."""

    bin_dir: Path
    sdk_root: Path
    home: Path


@pytest.fixture(scope="session")
def fake_sdk(tmp_path_factory, recorded_anywhere) -> FakeSdk:
    """An Android toolchain that is present, reachable, and broken.

    ``adb`` fails every call; the three SDK binaries answer 127. Both are put
    ahead of anything real on PATH, and ``ANDROID_HOME`` points at an empty
    directory, so ``common.sdk_tools.resolve_sdk_tool`` -- PATH first, then the
    SDK subdirectories -- cannot reach a real installation from here.
    """
    root = tmp_path_factory.mktemp("fake-sdk")
    bin_dir = root / "bin"
    bin_dir.mkdir()

    device_not_found = recorded_anywhere("adb_shell_device_not_found").rstrip("\n")
    (bin_dir / "adb").write_text(_FAKE_ADB.format(recorded=device_not_found), encoding="utf-8")
    for tool in MISSING_TOOLS:
        (bin_dir / tool).write_text(_FAKE_MISSING_TOOL, encoding="utf-8")
    for entry in bin_dir.iterdir():
        entry.chmod(0o755)

    sdk_root = root / "sdk"
    sdk_root.mkdir()
    home = root / "home"
    home.mkdir()
    return FakeSdk(bin_dir=bin_dir, sdk_root=sdk_root, home=home)


class Outcome(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    tools: tuple[str, ...]


def _invoke(mode: Mode, fake: FakeSdk, workdir: Path) -> Outcome:
    """Run one mode with the fake toolchain and nothing else on PATH."""
    tool_log = workdir / "tools.log"
    tool_log.touch()

    env = {
        "PATH": os.pathsep.join([str(fake.bin_dir), "/usr/bin", "/bin"]),
        "HOME": str(fake.home),
        "ANDROID_HOME": str(fake.sdk_root),
        "ANDROID_SDK_ROOT": str(fake.sdk_root),
        "ANDROID_SDK_HOME": str(fake.home),
        "TMPDIR": str(workdir),
        TOOL_LOG_VAR: str(tool_log),
    }
    argv = [arg.replace(WORKDIR, str(workdir)) for arg in mode.argv]

    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / mode.script), *argv],
        env=env,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=MODE_TIMEOUT_SECONDS,
        check=False,
    )
    invoked = tuple(
        sorted(
            {
                line.split()[0]
                for line in tool_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        )
    )
    return Outcome(completed.returncode, completed.stdout, completed.stderr, invoked)


def _params() -> list:
    marked = []
    for mode in MODES:
        marks = []
        if mode.defect:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=f"{mode.defect}: {mode.ident} reports success when the tool failed",
                )
            )
        marked.append(pytest.param(mode, id=mode.ident, marks=marks))
    return marked


@pytest.mark.parametrize("mode", _params())
def test_a_failing_tool_produces_a_failing_exit_status(mode: Mode, fake_sdk: FakeSdk, tmp_path):
    """Exit non-zero, and do not do it by crashing.

    Two assertions, because either one alone is satisfiable by the wrong fix.
    A non-zero status reached through an unhandled exception is not a fixed
    contract -- it hands the agent a Python traceback where a remedy belongs,
    and the repo has shipped that twice (`emulator_shutdown`, `uiautomator`).
    """
    outcome = _invoke(mode, fake_sdk, tmp_path)

    assert "Traceback" not in outcome.stderr, (
        f"{mode.ident} failed by crashing:\n{outcome.stderr[-1500:]}\n"
        f"Report the failure; do not raise through main()."
    )
    assert outcome.returncode != 0, (
        f"{mode.ident} exited 0 with the toolchain broken.\n"
        f"stdout: {outcome.stdout[:400]!r}\n"
        f"An agent reading this cannot tell an empty result from a failed "
        f"lookup, and exit status is the only other signal it has."
    )


@pytest.mark.parametrize("mode", [pytest.param(m, id=m.ident) for m in MODES if m.tool])
def test_the_mode_actually_reaches_for_a_tool(mode: Mode, fake_sdk: FakeSdk, tmp_path):
    """Anti-vacuity, per mode: a script that never ran adb proves nothing.

    Without this a mode could pass the sweep by rejecting its own arguments
    before touching the toolchain -- exit non-zero, no traceback, and no
    evidence at all about the failure this file is written to catch. The three
    two modes with ``tool=None`` say in the table why they touch nothing.
    """
    outcome = _invoke(mode, fake_sdk, tmp_path)
    assert mode.tool in outcome.tools, (
        f"{mode.ident} never invoked the fake {mode.tool} (saw {outcome.tools or 'nothing'}). "
        f"The mode's exit status therefore says nothing about tool failure."
    )


def test_the_fake_toolchain_shadows_any_real_one(fake_sdk: FakeSdk):
    """The harness must not be able to reach the machine's real adb.

    A phone is attached to the machine this suite is developed on. The sweep
    runs the whole table twice, and every one of those invocations would
    otherwise drive it.
    """
    path = os.pathsep.join([str(fake_sdk.bin_dir), "/usr/bin", "/bin"])
    for tool in ("adb", *MISSING_TOOLS):
        resolved = shutil.which(tool, path=path)
        assert resolved is not None, f"the fake {tool} is not executable"
        assert Path(resolved).parent == fake_sdk.bin_dir, f"{tool} resolves outside the fake bin"

    assert not any(fake_sdk.sdk_root.iterdir()), "ANDROID_HOME must be empty"


def test_the_fake_adb_speaks_recorded_output(fake_sdk: FakeSdk, tmp_path, recorded_anywhere):
    """The double's own output is recorded too, not invented.

    Writing ``error: device 'emulator-5554' not found`` here would have been
    the repo's founding mistake in miniature: real adb prefixes that message
    ``adb:``, and a script matching on the wrong prefix would be "verified"
    against a string nobody ever saw a tool print.
    """
    tool_log = tmp_path / "tools.log"
    tool_log.touch()
    completed = subprocess.run(
        [str(fake_sdk.bin_dir / "adb"), "-s", "emulator-5554", "shell", "true"],
        env={"PATH": "/usr/bin:/bin", TOOL_LOG_VAR: str(tool_log)},
        capture_output=True,
        text=True,
        timeout=MODE_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr.strip() == recorded_anywhere("adb_shell_device_not_found").strip()
    assert tool_log.read_text(encoding="utf-8").startswith("adb ")


def test_every_swept_script_exists():
    """A typo in the table would silently sweep nothing."""
    missing = sorted({mode.script for mode in MODES if not (SCRIPTS / mode.script).exists()})
    assert not missing, f"the sweep names scripts that do not exist: {missing}"
