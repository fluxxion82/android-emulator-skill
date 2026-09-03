"""Every documented mode, with adb failing: does the CLI say so in its exit status?

The finding this pins (C6, X5, X8, X3, L8) is one shape in four places: a
failure converted into an empty result, printed calmly, and reported as
success. ``app_launcher --list`` answered ``Installed packages (0)`` when adb
never ran, and ``device_list --json`` answered ``{"devices": [], "avds": []}``
when the SDK was not installed. Both were fixed during v0.7.0 and are green
here unmarked; the shape survives in ``emulator_create``, which this sweep does
not cover. An agent cannot tell "nothing is there" from "I could not look", and
it has no second signal to consult -- exit status is the signal.

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

Each mode is checked by three separate tests, and only one of them can carry
an ``xfail``. That split is not tidiness. ``xfail(strict=True)`` rejects an
unexpected *pass*, never an unexpected reason for failing, so a single combined
test would accept a hang, a fresh Python traceback or a vanished marker file as
"the expected failure" for any mode still pinned red -- the sweep would go on
reporting the defect it already knows about while a new one hid behind it.
Every mode is green today -- v0.7.0 closed the eight that were red -- so no
mode sets ``defect`` and nothing is marked; the split stands for the next one.
So:
completion and the absence of a traceback are asserted unmarked for all
twenty-three modes, the tool-invocation floor is asserted unmarked for the
twenty-one that touch a tool, and the ``xfail`` covers exactly one assertion,
``exit status != 0``.

Each mode's subprocess runs once and the outcome is shared between its three
tests, so all three describe the same run rather than three attempts at it.
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
# every entry below was observed, and the eight that were marked printed a zero
# exit status with an empty-looking answer. All eight are fixed; no entry sets
# `defect` today.
MODES: tuple[Mode, ...] = (
    # --- C6: app_launcher ---------------------------------------------------
    # Green since Inc 1: list_packages no longer answers a failed lookup with
    # an empty list, and main() reports the failure in both output modes.
    Mode("app_launcher.py", ("--list",), "adb", "package listing"),
    Mode("app_launcher.py", ("--list", "--json"), "adb", "package listing"),
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
    ),
    Mode(
        "anr_watcher.py",
        ("--diff", "a", "b", "--json"),
        None,
        "reads the on-disk session store under $HOME; no device is involved",
    ),
    # --- X8: app_state_capture ----------------------------------------------
    # Exits 1 here because the whole capture fails at the device level. X8 is
    # the PARTIAL capture -- a snapshot that succeeded except for the logs it
    # was asked for -- which needs a device that answers. Named so the gap is
    # visible rather than assumed covered; Inc 1 fixed it and asserts the exit
    # status by injecting the component failure directly, in
    # test_app_state_capture.py ("X8: a component that was asked for").
    Mode(
        "app_state_capture.py",
        ("--package", "com.example.app", "--output", f"{WORKDIR}/snapshots", "--json"),
        "adb",
        "whole-capture failure; the partial-capture case (X8) is out of reach here",
    ),
    # --- X3 / L8: the lifecycle scripts with no SDK -------------------------
    Mode("device_list.py", ("--json",), "adb", "device and AVD inventory"),
    Mode("emulator_boot.py", ("--list-avds", "--json"), "emulator", "AVD listing"),
    Mode("emulator_selector.py", ("--suggest", "--json"), "emulator", "AVD suggestion"),
    Mode(
        "emulator_delete.py",
        ("--all", "--yes", "--json"),
        "avdmanager",
        "batch delete with no avdmanager",
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


# Stands in for the exit status of a run that never finished. Non-zero on
# purpose: a hung mode must fail the completion test loudly rather than quietly
# satisfying the exit-status test, and for a mode carrying an xfail it turns
# into an unexpected pass, which strict mode also reports.
TIMED_OUT_STATUS = -1


class Outcome(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    tools: tuple[str, ...]
    timed_out: bool = False


def _invoke(mode: Mode, fake: FakeSdk, workdir: Path) -> Outcome:
    """Run one mode with the fake toolchain and nothing else on PATH.

    A timeout is captured rather than raised, so the three tests sharing this
    outcome each get to say their own thing about it.
    """
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

    timed_out = False
    try:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / mode.script), *argv],
            env=env,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=MODE_TIMEOUT_SECONDS,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        returncode = TIMED_OUT_STATUS
        stdout = (expired.stdout or b"").decode(errors="replace")
        stderr = (expired.stderr or b"").decode(errors="replace")

    invoked = tuple(
        sorted(
            {
                line.split()[0]
                for line in tool_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        )
    )
    return Outcome(returncode, stdout, stderr, invoked, timed_out)


@pytest.fixture(scope="session")
def swept(fake_sdk: FakeSdk, tmp_path_factory):
    """One subprocess per mode, memoised for the three tests that read it.

    Sharing the run matters as well as saving it: "no traceback", "a tool was
    invoked" and "the exit status was non-zero" are three statements about the
    same invocation, and running the script three times would let them describe
    three different ones.
    """
    seen: dict[str, Outcome] = {}

    def _outcome(mode: Mode) -> Outcome:
        if mode.ident not in seen:
            workdir = tmp_path_factory.mktemp("mode")
            seen[mode.ident] = _invoke(mode, fake_sdk, workdir)
        return seen[mode.ident]

    return _outcome


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


@pytest.mark.parametrize("mode", [pytest.param(m, id=m.ident) for m in MODES])
def test_the_mode_finishes_without_crashing(mode: Mode, swept):
    """Unmarked, and covering every mode including the two local ANR ones.

    Deliberately outside the ``xfail``. A hang and a Python traceback are both
    *worse* than the exit-status defect this file pins, and strict xfail only
    rejects an unexpected pass -- so folded into the marked test they would be
    swallowed as "still red, as expected". A non-zero status reached by raising
    through ``main()`` is not the contract either: it hands the agent a
    traceback where a remedy belongs, which this repo has shipped twice.
    """
    outcome = swept(mode)

    assert not outcome.timed_out, (
        f"{mode.ident} did not finish within {MODE_TIMEOUT_SECONDS}s. Every fake "
        f"tool exits immediately, so this is a hang, not slowness.\n"
        f"partial stdout: {outcome.stdout[:400]!r}"
    )
    assert "Traceback" not in outcome.stderr, (
        f"{mode.ident} failed by crashing:\n{outcome.stderr[-1500:]}\n"
        f"Report the failure; do not raise through main()."
    )


@pytest.mark.parametrize("mode", [pytest.param(m, id=m.ident) for m in MODES if m.tool])
def test_the_mode_actually_reaches_for_a_tool(mode: Mode, swept):
    """Anti-vacuity, per mode, and unmarked for the same reason as above.

    Without this a mode could pass the sweep by rejecting its own arguments
    before touching the toolchain -- exit non-zero, no traceback, and no
    evidence at all about the failure this file is written to catch. Keeping it
    out of the ``xfail`` means a marker file that stops being written fails
    here instead of disappearing into an expected failure. The two modes with
    ``tool=None`` say in the table why they touch nothing.
    """
    outcome = swept(mode)
    assert mode.tool in outcome.tools, (
        f"{mode.ident} never invoked the fake {mode.tool} (saw {outcome.tools or 'nothing'}). "
        f"The mode's exit status therefore says nothing about tool failure."
    )


@pytest.mark.parametrize("mode", _params())
def test_a_failing_tool_produces_a_failing_exit_status(mode: Mode, swept):
    """The one assertion the xfail covers: exit non-zero when the tool failed.

    Nothing else lives in here, so an ``xfail`` on this test can only ever be
    excusing the exit status -- never a hang, a crash, or a mode that never
    reached for a tool at all.
    """
    outcome = swept(mode)
    assert outcome.returncode != 0, (
        f"{mode.ident} exited 0 with the toolchain broken.\n"
        f"stdout: {outcome.stdout[:400]!r}\n"
        f"An agent reading this cannot tell an empty result from a failed "
        f"lookup, and exit status is the only other signal it has."
    )


def test_the_fake_toolchain_shadows_any_real_one(fake_sdk: FakeSdk):
    """The harness must not be able to reach the machine's real adb.

    A phone is attached to the machine this suite is developed on, and every
    invocation in the table would otherwise drive it.
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
