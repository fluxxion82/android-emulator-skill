"""Every documented mode, with the toolchain broken or gone: is the refusal the contract?

The finding this pins (C6, X5, X8, X3, L8) is one shape in four places: a
failure converted into an empty result, printed calmly, and reported as
success. ``app_launcher --list`` answered ``Installed packages (0)`` when adb
never ran, and ``device_list --json`` answered ``{"devices": [], "avds": []}``
when the SDK was not installed. An agent cannot tell "nothing is there" from
"I could not look", and it has no second signal to consult.

**Two passes, because a broken tool and a missing one are different failures
(N5).** Until v0.8.0 this sweep faked every tool as PRESENT and failing (adb
exit 1, the SDK binaries exit 127) and asserted only "exit != 0". That is why
``privacy_manager``'s traceback and ``emulator_create``'s ``[]`` survived a
release: a script that resolves a tool by path, or catches the exec failure
itself, never meets a stub that is there. So every mode runs twice:

- **broken**: ``adb`` on PATH answering every call with the RECORDED
  device-not-found stderr, exit 1; ``emulator`` / ``avdmanager`` /
  ``sdkmanager`` on PATH, exit 127.
- **absent**: PATH is ``/usr/bin:/bin`` -- no Android tool anywhere -- and
  HOME, ANDROID_HOME and ANDROID_SDK_ROOT are empty temp directories.
  ``ANDROID_AVD_HOME`` names ``.android/avd`` inside that empty HOME, which
  does not exist: a host that never had an SDK has no AVD home, and an
  existing empty one is a legitimate "no AVDs", which is not this sweep's
  question. The interpreter is the test's own, by absolute path -- stripping
  PATH otherwise hands ``python3`` to the system 3.9, which cannot import
  these scripts.

**The payload, not just the status.** Each mode's refusal is checked with
``fake_toolchain.assert_refusal``, the checker every refusal spec uses:
``--json`` -> stdout is exactly one ``{"success": false, "error", "remedy"}``
object whose ``error`` names the tool or operation; text -> ``Error:`` then
``Remedy:`` on stderr and no JSON on stdout. The exit status is exact: 3 when
the device is unavailable (the broken adb's stderr is a device error), 1
otherwise -- every mode in the absent pass, where adb could not be run at all.
"exit != 0" alone was satisfied by a traceback.

**Why this is a runtime sweep and not another AST guard.** The obvious static
rule -- "a function that builds a dict with an ``error`` key must not fall
through to ``sys.exit(0)``" -- was written down, costed, and dropped. It is not
expressible: the dict is built in one function, returned through two more, and
the exit is decided by a ``main()`` that never sees the key. What *is*
checkable is the thing the agent experiences, so this runs the real CLI, as a
subprocess, against a toolchain that fails.

**The fake adb is a fault injector, not a simulator.** It answers every
invocation the same way. The one piece of tool *text* it emits is read from
``tests/fixtures/recorded/`` (``adb_shell_device_not_found``), not written
here: real adb says ``adb:``; ``error:`` belongs to ``adb get-state``.

**Three tests per mode and pass, and only one of them may carry an xfail.**
``xfail(strict=True)`` rejects an unexpected *pass*, never an unexpected reason
for failing, so a single combined test would accept a hang, a fresh traceback
or a vanished tool call as "the expected failure" for a mode still pinned red.
So completion and the absence of a traceback are asserted unmarked for every
mode; the tool-invocation floor is asserted unmarked for every mode that
touches a tool (broken: the fake logged a call; absent: the output names the
tool it could not find); and the ``xfail`` covers exactly one assertion, the
payload. A mode that sets ``defect`` is red today and says why.

Each mode's subprocess runs once per pass and the outcome is shared between
its three tests, so all three describe the same run.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from fake_toolchain import ANDROID_TOOLS, assert_refusal

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

SYSTEM_PATH = ("/usr/bin", "/bin")

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

PASSES = ("broken", "absent")

# The device is unavailable: what the broken adb's recorded stderr says.
DEVICE_UNAVAILABLE = 3
FAILURE = 1


class Mode(NamedTuple):
    """One documented invocation, and what its refusal must be."""

    script: str
    argv: tuple[str, ...]
    tool: str | None  # the tool the mode needs; None with a reason below
    reason: str  # why no tool, or what the mode is for
    naming: tuple[str, ...]  # any one must appear in the error (case-insensitive)
    broken_status: int  # the exit status in the broken pass; the absent pass is always 1
    defect: str | None = None  # "<ID>: why" when this mode's payload is red today
    streaming: bool = False  # the refusal is the last stdout line, not all of it

    @property
    def ident(self) -> str:
        return f"{self.script.removesuffix('.py')} {' '.join(self.argv)}".strip()

    @property
    def json_mode(self) -> bool:
        return "--json" in self.argv

    def status(self, toolchain: str) -> int:
        """The exact exit status this mode's refusal carries in ``toolchain``."""
        return self.broken_status if toolchain == "broken" else FAILURE


def _adb(script: str, argv: tuple[str, ...], reason: str, **kw) -> Mode:
    """A mode whose first move is adb: device unavailable (3) when adb answers."""
    kw.setdefault("naming", ("adb",))
    kw.setdefault("broken_status", DEVICE_UNAVAILABLE)
    return Mode(script, argv, "adb", reason, **kw)


def _sdk(script: str, argv: tuple[str, ...], tool: str, reason: str) -> Mode:
    """A mode whose first move is an SDK binary: exit 127, or not there at all."""
    return Mode(script, argv, tool, reason, naming=(tool,), broken_status=FAILURE)


# The sweep table. `defect` is filled from an unmarked run, not from a guess.
MODES: tuple[Mode, ...] = (
    # --- Quick Start step 0: the environment check --------------------------
    # Not a device refusal: adb failing or missing is a failed check, and the
    # report is the refusal -- one object, success false, the first failed
    # check as the error -- at exit 1 in both passes. adb is its first probe.
    Mode(
        "android_health_check.sh",
        ("--json",),
        "adb",
        "Quick Start step 0",
        naming=("adb",),
        broken_status=FAILURE,
    ),
    # --- C6: app_launcher, Quick Start step 1 -------------------------------
    _adb("app_launcher.py", ("--list",), "package listing"),
    _adb("app_launcher.py", ("--list", "--json"), "package listing"),
    _adb("app_launcher.py", ("--state", "com.example.app"), "foreground check"),
    _adb("app_launcher.py", ("--state", "com.example.app", "--json"), "foreground check"),
    _adb("app_launcher.py", ("--launch", "com.example.app"), "Quick Start step 1"),
    _adb("app_launcher.py", ("--launch", "com.example.app", "--json"), "activity launch"),
    # --- the core loop: Quick Start steps 2-5 -------------------------------
    _adb("screen_mapper.py", (), "default text output, Quick Start step 2"),
    _adb("screen_mapper.py", ("--json",), "machine-readable screen map"),
    _adb("navigator.py", ("--find-text", "x", "--json"), "lookup"),
    _adb("navigator.py", ("--find-text", "x", "--tap"), "Quick Start step 3"),
    _adb(
        "navigator.py",
        ("--find-type", "EditText", "--enter-text", "x"),
        "Quick Start step 4",
    ),
    _adb("accessibility_audit.py", (), "Quick Start step 5"),
    _adb("accessibility_audit.py", ("--json",), "the audit as JSON"),
    # --- X5: anr_watcher session store --------------------------------------
    Mode(
        "anr_watcher.py",
        ("--get-details", "no-such-session", "--json"),
        None,
        "reads the on-disk session store under $HOME; no device is involved",
        naming=("session",),
        broken_status=FAILURE,
    ),
    Mode(
        "anr_watcher.py",
        ("--diff", "a", "b", "--json"),
        None,
        "reads the on-disk session store under $HOME; no device is involved",
        naming=("summary.json", "session"),
        broken_status=FAILURE,
    ),
    # --- X8: app_state_capture ----------------------------------------------
    # The partial-capture policy catches a device error on purpose (common/cli's
    # list of deliberate catches) to keep what arrived, and its refusal is still
    # the device error: exit 3 when adb says the device is not there (W3-L, A7;
    # it was exit 1). Red when N5 first ran it: its own `{"error", "partial"}`
    # object, no `success`, no remedy.
    _adb(
        "app_state_capture.py",
        ("--package", "com.example.app", "--output", f"{WORKDIR}/snapshots", "--json"),
        "whole-capture failure; the partial-capture case (X8) is out of reach here",
        naming=("adb", "device"),
    ),
    # --- X3 / L8 / N1: the lifecycle scripts with no SDK ---------------------
    _adb("device_list.py", ("--json",), "device and AVD inventory"),
    _sdk("emulator_boot.py", ("--list-avds", "--json"), "emulator", "AVD listing"),
    _sdk("emulator_selector.py", ("--suggest", "--json"), "emulator", "AVD suggestion"),
    _sdk(
        "emulator_delete.py",
        ("--all", "--yes", "--json"),
        "avdmanager",
        "batch delete with no avdmanager",
    ),
    _sdk("emulator_create.py", ("--list-images",), "sdkmanager", "system image listing"),
    _sdk("emulator_create.py", ("--list-images", "--json"), "sdkmanager", "the same, as JSON"),
    _sdk("emulator_create.py", ("--list-devices", "--json"), "avdmanager", "device definitions"),
    _sdk(
        "emulator_create.py",
        ("--device", "pixel_7", "--api", "34", "--name", "probe"),
        "avdmanager",
        "a create: the device definition is resolved first",
    ),
    _sdk(
        "emulator_create.py",
        ("--device", "pixel_7", "--api", "34", "--name", "probe", "--json"),
        "avdmanager",
        "the same create, as JSON",
    ),
    *(
        Mode(
            "emulator_erase.py",
            argv,
            None,
            "reads the AVD home on disk, which this host does not have; no SDK tool "
            "is involved in listing or erasing",
            naming=("avd",),
            broken_status=FAILURE,
        )
        for argv in (("--list",), ("--list", "--json"), ("--all",), ("--all", "--json"))
    ),
    # --- diagnostics --------------------------------------------------------
    _adb("log_monitor.py", ("--last", "1m", "--json"), "historical log window"),
    _adb("crash_triage.py", ("--json",), "crash buffer triage"),
    # --- emulator console and device services -------------------------------
    _adb("snapshot.py", ("--list", "--json"), "snapshot listing via the console"),
    _adb("sms.py", ("--list", "--json"), "inbox read"),
    _adb(
        "privacy_manager.py",
        ("--list", "--package", "com.example.app", "--json"),
        "permission listing",
    ),
    _adb("status_bar.py", ("--reset", "--json"), "demo-mode reset"),
    # Red when added (Codex review of #29): `{"success": false, ...}` with no
    # remedy, `x adb not found` in text, and a device error at exit 1.
    _adb("push_notification.py", ("--list", "--json"), "posted-notification read-back"),
    _adb("push_notification.py", ("--list",), "the same, as text"),
    _adb(
        "push_notification.py",
        ("--post", "--tag", "t", "--text", "x", "--json"),
        "post into the shade",
    ),
    _adb(
        "container.py",
        ("--package", "com.example.app", "--ls", "--json"),
        "app sandbox listing",
    ),
)


class Toolchain(NamedTuple):
    """One pass's toolchain: where the tools are (or are not), and an empty SDK."""

    name: str
    path: str
    sdk_root: Path


@pytest.fixture(scope="session")
def toolchains(tmp_path_factory, recorded_anywhere) -> dict[str, Toolchain]:
    """The broken toolchain and the absent one.

    broken: ``adb`` fails every call; the three SDK binaries answer 127. Both
    are put ahead of anything real on PATH. absent: nothing but the system
    directories. In both, ``ANDROID_HOME`` is an empty directory, so
    ``common.sdk_tools.resolve_sdk_tool`` -- PATH first, then the SDK
    subdirectories -- cannot reach a real installation.
    """
    built: dict[str, Toolchain] = {}
    for name in PASSES:
        root = tmp_path_factory.mktemp(f"toolchain-{name}")
        sdk_root = root / "sdk"
        sdk_root.mkdir()
        dirs = list(SYSTEM_PATH)
        if name == "broken":
            bin_dir = root / "bin"
            bin_dir.mkdir()
            device_not_found = recorded_anywhere("adb_shell_device_not_found").rstrip("\n")
            (bin_dir / "adb").write_text(
                _FAKE_ADB.format(recorded=device_not_found), encoding="utf-8"
            )
            for tool in MISSING_TOOLS:
                (bin_dir / tool).write_text(_FAKE_MISSING_TOOL, encoding="utf-8")
            for entry in bin_dir.iterdir():
                entry.chmod(0o755)
            dirs.insert(0, str(bin_dir))
        built[name] = Toolchain(name, os.pathsep.join(dirs), sdk_root)
    return built


# Stands in for the exit status of a run that never finished. Not 1 or 3 on
# purpose: a hung mode must fail the completion test loudly, and can never
# satisfy the exact status the payload test asks for.
TIMED_OUT_STATUS = -1


class Outcome(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    tools: tuple[str, ...]
    timed_out: bool = False

    def completed(self, argv: list[str]) -> subprocess.CompletedProcess:
        """The run, in the shape ``assert_refusal`` reads."""
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _env(chain: Toolchain, workdir: Path, tool_log: Path) -> dict[str, str]:
    """A from-scratch environment: nothing inherited, nothing real reachable.

    HOME is a new empty directory per run, so what one mode leaves in it (a
    session store, a cache) cannot become the next mode's answer.
    """
    home = workdir / "home"
    home.mkdir()
    return {
        "PATH": chain.path,
        "HOME": str(home),
        "ANDROID_HOME": str(chain.sdk_root),
        "ANDROID_SDK_ROOT": str(chain.sdk_root),
        "ANDROID_SDK_HOME": str(home),
        "ANDROID_AVD_HOME": str(home / ".android" / "avd"),
        "TMPDIR": str(workdir),
        TOOL_LOG_VAR: str(tool_log),
    }


def _invoke(mode: Mode, chain: Toolchain, workdir: Path) -> Outcome:
    """Run one mode in one toolchain.

    The interpreter is this test's own (``sys.executable``, an absolute path),
    never ``python3`` looked up on the reduced PATH; a ``.sh`` script runs
    under bash, as SKILL.md writes it. A timeout is captured rather than
    raised, so the three tests sharing this outcome each get to say their own
    thing about it.
    """
    tool_log = workdir / "tools.log"
    tool_log.touch()
    argv = [arg.replace(WORKDIR, str(workdir)) for arg in mode.argv]
    interpreter = "/bin/bash" if mode.script.endswith(".sh") else sys.executable

    timed_out = False
    try:
        completed = subprocess.run(
            [interpreter, str(SCRIPTS / mode.script), *argv],
            env=_env(chain, workdir, tool_log),
            cwd=workdir,
            stdin=subprocess.DEVNULL,
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
def swept(toolchains, tmp_path_factory):
    """One subprocess per mode and pass, memoised for the three tests that read it.

    Sharing the run matters as well as saving it: "no traceback", "a tool was
    reached for" and "the refusal is the contract" are three statements about
    the same invocation, and running the script three times would let them
    describe three different ones.
    """
    seen: dict[tuple[str, str], Outcome] = {}

    def _outcome(toolchain: str, mode: Mode) -> Outcome:
        key = (toolchain, mode.ident)
        if key not in seen:
            workdir = tmp_path_factory.mktemp("mode")
            seen[key] = _invoke(mode, toolchains[toolchain], workdir)
        return seen[key]

    return _outcome


def _cases(*, needs_tool: bool = False, marked: bool = False) -> list:
    """(pass, mode) parameters; the payload test's carry each red mode's xfail."""
    cases = []
    for toolchain in PASSES:
        for mode in MODES:
            if needs_tool and mode.tool is None:
                continue
            marks = []
            if marked and mode.defect:
                marks.append(
                    pytest.mark.xfail(strict=True, raises=AssertionError, reason=mode.defect)
                )
            cases.append(
                pytest.param(toolchain, mode, id=f"{toolchain}: {mode.ident}", marks=marks)
            )
    return cases


@pytest.mark.parametrize(("toolchain", "mode"), _cases())
def test_the_mode_finishes_without_crashing(toolchain: str, mode: Mode, swept):
    """Unmarked, and covering every mode in both passes.

    Deliberately outside the ``xfail``. A hang and a Python traceback are both
    *worse* than a malformed refusal, and strict xfail only rejects an
    unexpected pass -- so folded into the marked test they would be swallowed
    as "still red, as expected".
    """
    outcome = swept(toolchain, mode)

    assert not outcome.timed_out, (
        f"{mode.ident} did not finish within {MODE_TIMEOUT_SECONDS}s. Every fake "
        f"tool exits immediately, so this is a hang, not slowness.\n"
        f"partial stdout: {outcome.stdout[:400]!r}"
    )
    assert "Traceback" not in outcome.stderr + outcome.stdout, (
        f"{mode.ident} failed by crashing:\n{outcome.stderr[-1500:]}\n"
        f"Report the failure; do not raise through main()."
    )


@pytest.mark.parametrize(("toolchain", "mode"), _cases(needs_tool=True))
def test_the_mode_actually_reaches_for_a_tool(toolchain: str, mode: Mode, swept):
    """Anti-vacuity, per mode and pass, and unmarked for the same reason as above.

    Without this a mode could pass the sweep by rejecting its own arguments
    before touching the toolchain -- a well-formed refusal, and no evidence at
    all about the failure this file is written to catch. broken: the fake
    logged a call. absent: there is nothing to log a call, so the evidence is
    that the refusal names the tool it went looking for. The modes with
    ``tool=None`` say in the table why they touch nothing.
    """
    outcome = swept(toolchain, mode)
    if toolchain == "broken":
        assert mode.tool in outcome.tools, (
            f"{mode.ident} never invoked the fake {mode.tool} (saw "
            f"{outcome.tools or 'nothing'}). Its refusal therefore says nothing "
            f"about tool failure."
        )
    else:
        said = (outcome.stdout + outcome.stderr).lower()
        assert mode.tool in said, (
            f"{mode.ident} with no {mode.tool} on PATH never said so:\n"
            f"stdout: {outcome.stdout[-400:]!r}\nstderr: {outcome.stderr[-400:]!r}"
        )


@pytest.mark.parametrize(("toolchain", "mode"), _cases(marked=True))
def test_a_failing_toolchain_is_refused_in_the_contracts_shape(toolchain: str, mode: Mode, swept):
    """The one assertion an xfail may cover: the refusal itself.

    One object (``--json``) or ``Error:`` / ``Remedy:`` (text), an error that
    names the tool or operation, and the exact exit status. Nothing else lives
    in here, so an ``xfail`` on this test can only ever be excusing the
    payload -- never a hang, a crash, or a mode that never reached for a tool.
    """
    outcome = swept(toolchain, mode)
    assert_refusal(
        outcome.completed([mode.script, *mode.argv]),
        json_mode=mode.json_mode,
        naming=mode.naming,
        status=mode.status(toolchain),
        streaming=mode.streaming,
    )


def test_the_broken_toolchain_shadows_any_real_one(toolchains):
    """The harness must not be able to reach the machine's real adb.

    A phone is attached to the machine this suite is developed on, and every
    invocation in the table would otherwise drive it.
    """
    broken = toolchains["broken"]
    bin_dir = Path(broken.path.split(os.pathsep)[0])
    for tool in ANDROID_TOOLS:
        resolved = shutil.which(tool, path=broken.path)
        assert resolved is not None, f"the fake {tool} is not executable"
        assert Path(resolved).parent == bin_dir, f"{tool} resolves outside the fake bin"
    assert not any(broken.sdk_root.iterdir()), "ANDROID_HOME must be empty"


def test_the_absent_toolchain_has_no_android_tool_at_all(toolchains, tmp_path):
    """The absent pass is only worth anything if nothing is there.

    pytest.fail, not assert: an Android tool in /usr/bin or /bin would make
    every absent-pass result a statement about that tool instead.
    """
    absent = toolchains["absent"]
    assert absent.path == os.pathsep.join(SYSTEM_PATH)
    for tool in ANDROID_TOOLS:
        found = shutil.which(tool, path=absent.path)
        if found is not None:
            pytest.fail(f"{tool} resolves to {found} on the absent PATH")
    assert not any(absent.sdk_root.iterdir()), "ANDROID_HOME must be empty"

    env = _env(absent, tmp_path, tmp_path / "tools.log")
    assert not any(Path(env["HOME"]).iterdir()), "HOME must be empty"
    assert not Path(env["ANDROID_AVD_HOME"]).exists(), "the AVD home must not exist"


def test_the_absent_pass_runs_the_tests_interpreter_not_the_systems(toolchains, tmp_path):
    """zsh-probe trap: with PATH stripped, `python3` is the system 3.9.

    That interpreter cannot import these scripts (`str | None` at runtime),
    and every mode would "refuse" with a SyntaxError-shaped traceback that says
    nothing about the toolchain. The sweep runs ``sys.executable``.
    """
    assert Path(sys.executable).is_absolute()
    probe = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.version_info[:2] >= (3, 10))"],
        env={"PATH": toolchains["absent"].path},
        capture_output=True,
        text=True,
        timeout=MODE_TIMEOUT_SECONDS,
        check=False,
    )
    assert probe.stdout.strip() == "True", probe


def test_the_fake_adb_speaks_recorded_output(toolchains, tmp_path, recorded_anywhere):
    """The double's own output is recorded too, not invented.

    Writing ``error: device 'emulator-5554' not found`` here would have been
    the repo's founding mistake in miniature: real adb prefixes that message
    ``adb:``, and a script matching on the wrong prefix would be "verified"
    against a string nobody ever saw a tool print.
    """
    tool_log = tmp_path / "tools.log"
    tool_log.touch()
    adb = shutil.which("adb", path=toolchains["broken"].path)
    completed = subprocess.run(
        [adb, "-s", "emulator-5554", "shell", "true"],
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


def test_the_sweep_covers_every_quick_start_command():
    """N5: steps 4 and 5 (`--enter-text`, `accessibility_audit`) were not swept.

    Read off SKILL.md by the Quick Start guard's own extractor, so a step added
    there is a step this table has to grow. Same rule as that guard: the same
    script, with at least the flags Quick Start names.
    """
    from test_quick_start_contract import quick_start_commands

    swept = [(mode.script, frozenset(a for a in mode.argv if a.startswith("--"))) for mode in MODES]
    documented = quick_start_commands()
    assert documented, "the Quick Start extractor returned nothing"
    missing = [
        (script, sorted(flags))
        for script, flags in documented
        if not any(script == name and flags <= used for name, used in swept)
    ]
    assert not missing, f"Quick Start commands the sweep never runs: {missing}"


def test_the_sweep_covers_the_scripts_n5_named():
    """emulator_create and emulator_erase: absent from the sweep that shipped their lies."""
    swept = {mode.ident for mode in MODES}
    for wanted in (
        "emulator_create --list-images --json",
        "emulator_create --list-devices --json",
        "emulator_erase --list --json",
        "emulator_erase --all --json",
    ):
        assert wanted in swept, f"{wanted} is not in the sweep"
    assert any(
        mode.script == "emulator_create.py" and "--device" in mode.argv for mode in MODES
    ), "no create is swept"


# ===========================================================================
# Usage errors: in the schema too (exit 2, one object under --json)
# ===========================================================================
#
# argparse's own error path prints prose to stderr and exits 2. Under --json
# that left a caller parsing stdout with an empty document behind a status it
# could see but not read. Every script builds its parser through
# ``common.cli.parser`` (held there by test_cli_boundary's guard), which keeps
# exit 2 and answers in the shared refusal shape when --json was asked for.
# These run the real CLIs, in the broken toolchain, and require that no tool
# was reached: a rejected command line must not have touched a device.

USAGE = 2

# Defined by no script: argparse's "unrecognized arguments" (or, where the
# script has a required argument, the missing-argument error argparse reports
# first -- still a usage error).
UNKNOWN_FLAG = "--no-such-flag-anywhere"

# A value the flag's own type rejects, for every script with a typed flag that
# can reject one. Kept honest by the test at the end of this file, which reads
# each script's AST for such flags and requires an entry here or a reason in
# TYPED_EXEMPT.
TYPED_USAGE_ERRORS: dict[str, tuple[str, ...]] = {
    "anr_watcher.py": ("--top", "many"),
    "app_state_capture.py": ("--log-lines", "many"),
    "appearance.py": ("--font-scale", "big"),
    "emulator_boot.py": ("--timeout", "soon"),
    "emulator_create.py": ("--api", "latest"),
    "emulator_delete.py": ("--old", "some"),
    "emulator_erase.py": ("--timeout", "soon"),
    "emulator_selector.py": ("--count", "many"),
    "emulator_shutdown.py": ("--timeout", "soon"),
    "gesture.py": ("--swipe", "sideways"),
    "keyboard.py": ("--count", "many"),
    "location.py": ("--lat", "north"),
    "log_monitor.py": ("--severity", "loud"),
    "navigator.py": ("--max-scrolls", "0"),
    "privacy_manager.py": ("--step", "big"),
    "sms.py": ("--limit", "many"),
    "snapshot.py": ("--timeout", "soon"),
    "status_bar.py": ("--battery", "full"),
    "test_recorder.py": ("--size", "huge"),
    "visual_diff.py": ("--threshold", "low"),
}

TYPED_EXEMPT: dict[str, str] = {
    "localization_audit.py": "--res and --source are type=Path, which accepts any string",
    "avd.py": "its one typed argument is the verb, routed before argparse: see the verb cases",
    "logs.py": "its one typed argument is the verb, routed before argparse: see the verb cases",
}

# A router hands everything after the verb to the delegate's own parser, so a
# usage error inside a verb is the delegate's -- and must still be JSON.
SUBCOMMAND_USAGE_ERRORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("avd.py", ("list", UNKNOWN_FLAG)),
    ("avd.py", ("start", "--timeout", "soon")),
    ("logs.py", ("tail", UNKNOWN_FLAG)),
    ("logs.py", ("tail", "--severity", "loud")),
)


def _python_scripts() -> list[str]:
    """Every Python script, from the boundary guard's positive enumeration."""
    from test_cli_boundary import SCRIPT_NAMES

    return list(SCRIPT_NAMES)


def _usage_cases() -> list:
    def _param(script: str, argv: tuple[str, ...], what: str):
        return pytest.param(script, argv, id=f"{script.removesuffix('.py')} {what}")

    cases = [_param(script, (UNKNOWN_FLAG,), "unknown flag") for script in _python_scripts()]
    cases += [
        _param(script, argv, " ".join(argv)) for script, argv in sorted(TYPED_USAGE_ERRORS.items())
    ]
    cases += [_param(script, argv, " ".join(argv)) for script, argv in SUBCOMMAND_USAGE_ERRORS]
    return cases


def _run_usage(script: str, argv: tuple[str, ...], toolchains, tmp_path: Path) -> Outcome:
    """Run one command line in the broken toolchain, in a fresh working directory."""
    workdir = tmp_path / f"run-{len(list(tmp_path.iterdir()))}"
    workdir.mkdir()
    mode = Mode(script, argv, None, "usage error", (), USAGE)
    return _invoke(mode, toolchains["broken"], workdir)


def _fold(text: str) -> str:
    return " ".join(text.split())


def _argparse_message(stderr: str) -> str:
    """The message from argparse's ``<prog>: error: <message>`` line."""
    assert ": error: " in stderr, f"no argparse error line on stderr: {stderr[-600:]!r}"
    return _fold(stderr.split(": error: ", 1)[1])


def _assert_one_usage_object(outcome: Outcome, label: str) -> dict:
    """Exit 2, and stdout is one line holding exactly the three contract keys."""
    shown = f"\n{label}\nstdout: {outcome.stdout[-600:]!r}\nstderr: {outcome.stderr[-600:]!r}"
    assert "Traceback" not in outcome.stdout + outcome.stderr, f"crashed:{shown}"
    assert outcome.returncode == USAGE, f"exit {outcome.returncode}, not {USAGE}:{shown}"
    lines = outcome.stdout.splitlines()
    assert len(lines) == 1, f"stdout is {len(lines)} lines, not one JSON object:{shown}"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict), f"not an object:{shown}"
    assert set(payload) == {"success", "error", "remedy"}, f"keys {sorted(payload)}:{shown}"
    assert payload["success"] is False, shown
    assert isinstance(payload["error"], str) and payload["error"].strip(), shown
    assert isinstance(payload["remedy"], str) and payload["remedy"].strip(), shown
    assert outcome.tools == (), f"a rejected command line reached {outcome.tools}:{shown}"
    assert outcome.stderr == "", f"the JSON usage error also wrote stderr:{shown}"
    return payload


@pytest.mark.parametrize(("script", "argv"), _usage_cases())
def test_a_usage_error_under_json_is_one_refusal_object_at_exit_2(
    script: str, argv: tuple[str, ...], toolchains, tmp_path
):
    """The same command line twice: without --json it is prose, with it the object.

    The object's ``error`` is argparse's own message -- the one the prose run
    prints after ``error:`` -- so the two modes cannot disagree about what was
    wrong, and its ``remedy`` carries the usage line.
    """
    label = f"{script} {' '.join(argv)}"
    text = _run_usage(script, argv, toolchains, tmp_path)
    assert "Traceback" not in text.stdout + text.stderr, f"{label} crashed:\n{text.stderr[-800:]}"
    assert text.returncode == USAGE, f"{label}: exit {text.returncode}\n{text.stderr[-600:]}"
    assert text.stdout == "", f"{label}: a text-mode usage error wrote stdout: {text.stdout!r}"
    assert text.stderr.startswith("usage:"), f"{label}: not argparse prose: {text.stderr!r}"
    assert text.tools == (), f"{label}: a rejected command line reached {text.tools}"
    message = _argparse_message(text.stderr)

    payload = _assert_one_usage_object(
        _run_usage(script, (*argv, "--json"), toolchains, tmp_path), f"{label} --json"
    )
    assert (
        payload["error"] == message
    ), f"{label}: --json says {payload['error']!r}, the prose says {message!r}"
    assert "usage:" in payload["remedy"], f"{label}: the remedy has no usage line: {payload}"


def test_a_scripts_alias_for_json_gets_the_object_too(toolchains, tmp_path):
    """visual_diff's ``--details`` is its published alias for ``--json``."""
    _assert_one_usage_object(
        _run_usage("visual_diff.py", (UNKNOWN_FLAG, "--details"), toolchains, tmp_path),
        "visual_diff --details",
    )


@pytest.mark.parametrize("router", ["avd.py", "logs.py"])
def test_an_unknown_verb_under_json_is_the_object(router: str, toolchains, tmp_path):
    """The router's own usage error: the verb table in prose, or one object."""
    text = _run_usage(router, ("frobnicate",), toolchains, tmp_path)
    assert text.returncode == USAGE and text.stdout == "", text
    assert "Unknown verb 'frobnicate'" in text.stderr, text.stderr

    payload = _assert_one_usage_object(
        _run_usage(router, ("frobnicate", "--json"), toolchains, tmp_path),
        f"{router} frobnicate --json",
    )
    assert "frobnicate" in payload["error"], payload


def test_json_after_the_end_of_options_is_a_value_not_the_flag(toolchains, tmp_path):
    """``--`` ends the options: a ``--json`` after it is a positional (a file), so prose."""
    argv = (UNKNOWN_FLAG, "--", "--json", "b.png")
    outcome = _run_usage("visual_diff.py", argv, toolchains, tmp_path)
    assert outcome.returncode == USAGE, outcome
    assert outcome.stdout == "", f"`-- --json` was read as the flag: {outcome.stdout!r}"
    assert outcome.stderr.startswith("usage:"), outcome.stderr
    assert _argparse_message(outcome.stderr) == f"unrecognized arguments: {UNKNOWN_FLAG}"


def _fallible_typed_flags(script: str) -> list[str]:
    """Flags whose ``type=`` (other than str / Path) or ``choices=`` can reject a value."""
    tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
        if "choices" in keywords or keywords.get("type", "str") not in ("str", "Path"):
            found.append(ast.unparse(node.args[0]) if node.args else "?")
    return found


def test_every_script_with_a_typed_flag_has_a_bad_value_case():
    """Derived from the scripts: a new typed flag on an uncovered script fails here."""
    uncovered = {
        script: flags
        for script in _python_scripts()
        if (flags := _fallible_typed_flags(script))
        and script not in TYPED_USAGE_ERRORS
        and script not in TYPED_EXEMPT
    }
    assert not uncovered, f"typed flags with no bad-value usage case: {uncovered}"
    covered = {*TYPED_USAGE_ERRORS, *TYPED_EXEMPT} - {"localization_audit.py"}
    stale = sorted(script for script in covered if not _fallible_typed_flags(script))
    assert not stale, f"bad-value cases for scripts with no fallible typed flag: {stale}"
