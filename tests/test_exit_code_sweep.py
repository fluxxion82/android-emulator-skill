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
    # list of deliberate catches), so the refusal is exit 1 even when adb says
    # the device is not there. Red when N5 first ran it: its own
    # `{"error", "partial"}` object, no `success`, no remedy.
    _adb(
        "app_state_capture.py",
        ("--package", "com.example.app", "--output", f"{WORKDIR}/snapshots", "--json"),
        "whole-capture failure; the partial-capture case (X8) is out of reach here",
        naming=("adb", "device"),
        broken_status=FAILURE,
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
    never ``python3`` looked up on the reduced PATH. A timeout is captured
    rather than raised, so the three tests sharing this outcome each get to
    say their own thing about it.
    """
    tool_log = workdir / "tools.log"
    tool_log.touch()
    argv = [arg.replace(WORKDIR, str(workdir)) for arg in mode.argv]

    timed_out = False
    try:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / mode.script), *argv],
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
