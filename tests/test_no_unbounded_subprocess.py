"""Every subprocess call in the skill is bounded, or explicitly exempt.

An unbounded adb call does not merely hang its caller: it wedges the adb
connection for whatever runs next, which presents as a hang with no diagnosis.
An AST sweep found 69 such calls across 17 scripts; `common/adb_exec.run_adb`
bounds everything routed through it, and this test stops the count creeping back
up.

Exemptions are listed by name below rather than inferred, so adding one is a
deliberate act that shows up in review. There are exactly two legitimate shapes:

- **Detached process launches.** Starting the `emulator` binary is meant to
  outlive the caller; a timeout would kill the emulator being started.
- **Streaming readers.** `adb logcat` is read line by line for as long as the
  caller wants. These are bounded by their own deadline mechanism — a watchdog
  timer that terminates the child — not by `subprocess`'s timeout, which only
  applies to a call that runs to completion.

A streaming exemption is only honest if the module really does enforce a
deadline, so that is asserted too -- and, where the deadline is a timer, by
running each mode that is meant to end (a ``--duration``, a ``--last`` dump)
against a device that never writes: a timer bounds only the runs that arm it,
and only if it is started and its callback stops the read.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_toolchain import FakeToolchain, Rule

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "android-emulator-skill"
    / "skills"
    / "android-emulator-skill"
    / "scripts"
)

# (module, callee) pairs allowed to run unbounded, each with the reason it is
# not a defect. Keyed loosely on the module so a moved line does not break it.
EXEMPT: dict[str, str] = {
    "emulator_boot.py": "launches the emulator binary detached; it must outlive the caller",
    "emulator_selector.py": "delegates to the emulator binary the same way emulator_boot does",
    "log_monitor.py": "streams `adb logcat`; bounded by its own deadline timer, not subprocess",
    "anr_watcher.py": "streams `adb logcat` in a detached worker with its own restart budget",
}

# Modules claiming a streaming exemption must actually enforce a deadline.
STREAMING = ["log_monitor.py", "anr_watcher.py"]


SUBPROCESS_CALLEES = ("run", "Popen")


def _subprocess_calls(tree: ast.Module) -> list[tuple[ast.Call, str]]:
    """Every call into ``subprocess.run`` / ``subprocess.Popen``, however imported.

    Bound to the module, not to the attribute name. Matching any ``.run`` or
    ``.Popen`` -- what this file did first -- counted ``cli.run(main)`` as a
    subprocess bypass in every script the moment the shared CLI boundary landed:
    a guard keyed on a spelling rather than on what the spelling refers to.

    Resolves ``import subprocess``, ``import subprocess as sp`` and
    ``from subprocess import run [as r]``.

    Returns:
        ``(call, callee)`` pairs, ``callee`` being ``"run"`` or ``"Popen"``.
    """
    modules: set[str] = set()
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess" and not node.level:
            for alias in node.names:
                if alias.name in SUBPROCESS_CALLEES:
                    functions[alias.asname or alias.name] = alias.name

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in SUBPROCESS_CALLEES
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            found.append((node, func.attr))
        elif isinstance(func, ast.Name) and func.id in functions:
            found.append((node, functions[func.id]))
    return found


def _unbounded_calls(path: Path) -> list[tuple[int, str]]:
    """Line numbers of subprocess run/Popen calls with no ``timeout=``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.lineno, callee)
        for node, callee in _subprocess_calls(tree)
        if not any(keyword.arg == "timeout" for keyword in node.keywords)
    ]


def _script_files() -> list[Path]:
    return sorted(SCRIPTS.rglob("*.py"))


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_no_unbounded_subprocess_calls(path: Path):
    """Any new unbounded call must either gain a timeout or an explicit exemption."""
    unbounded = _unbounded_calls(path)
    if not unbounded:
        return

    reason = EXEMPT.get(path.name)
    assert reason, (
        f"{path.name} has unbounded subprocess calls at lines "
        f"{[line for line, _ in unbounded]}. Route it through "
        f"common.adb_exec.run_adb, pass an explicit timeout=, or add an "
        f"exemption to EXEMPT in this test saying why it cannot be bounded."
    )

    # A launch exemption covers Popen only; a bare `run` is never long-lived.
    bare_runs = [line for line, callee in unbounded if callee == "run"]
    assert not bare_runs, (
        f"{path.name} is exempt for {reason}, but that covers detached or "
        f"streaming Popen calls. subprocess.run at lines {bare_runs} runs to "
        f"completion and must be bounded."
    )


def _arms_a_watchdog_timer(tree: ast.Module) -> bool:
    """A ``threading.Timer`` (or bare ``Timer``) is constructed somewhere."""
    return any(
        isinstance(node, ast.Call)
        and (
            getattr(node.func, "attr", None) == "Timer" or getattr(node.func, "id", None) == "Timer"
        )
        for node in ast.walk(tree)
    )


def _positive_constants(tree: ast.Module) -> dict[str, float]:
    """Module-level ``NAME = <positive int or float literal>`` bindings."""
    constants: dict[str, float] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) in (int, float)
            and node.value.value > 0
        ):
            constants[node.targets[0].id] = node.value.value
    return constants


def _statically_bounded(node: ast.expr | None, constants: dict[str, float]) -> bool:
    """A select timeout with a positive upper bound readable off the source.

    A positive numeric literal; a module constant bound to one; or ``min(...)``
    with such a bound among its arguments -- ``min(remaining, POLL)`` is at most
    POLL. ``None``, a missing argument, or any other expression is not.
    """
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float) and node.value > 0
    if isinstance(node, ast.Name):
        return node.id in constants
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "min":
        return any(_statically_bounded(arg, constants) for arg in node.args)
    return False


def _bounds_its_reads(tree: ast.Module) -> bool:
    """Some function selects with a bounded timeout and then reads with ``os.read``.

    Both halves matter, and the first version of this guard checked neither
    (review of PR #26): ``select(..., 0.25)`` followed by ``stream.readline()``
    blocks in readline() whenever a line is partial -- and buffers ahead of the
    fd, which is N16 -- and ``select(..., None)`` blocks forever. The read in
    the selecting function must be ``os.read``, which returns what the readable
    fd holds, and no ``readline()`` may appear there at all.
    """
    constants = _positive_constants(tree)
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        selects = [
            call
            for call in calls
            if getattr(call.func, "attr", None) == "select"
            and getattr(getattr(call.func, "value", None), "id", None) == "select"
        ]
        if not selects:
            continue
        bounded = all(
            _statically_bounded(
                (
                    call.args[3]
                    if len(call.args) >= 4
                    else next((k.value for k in call.keywords if k.arg == "timeout"), None)
                ),
                constants,
            )
            for call in selects
        )
        reads_the_fd = any(
            getattr(call.func, "attr", None) == "read"
            and getattr(getattr(call.func, "value", None), "id", None) == "os"
            for call in calls
        )
        reads_lines = any(getattr(call.func, "attr", None) == "readline" for call in calls)
        if bounded and reads_the_fd and not reads_lines:
            return True
    return False


@pytest.mark.parametrize("module", STREAMING)
def test_streaming_modules_arm_a_watchdog_timer(module: str):
    """A streaming exemption is only honest if a deadline is actually enforced.

    `subprocess`'s own timeout cannot bound a stream read incrementally, so
    these modules must stop the child themselves.

    The first version of this test substring-matched ``terminate()`` and
    ``kill()``. Both appear in unrelated cleanup code, so it passed for
    `anr_watcher` while ``AnrWatcher.watch(duration_seconds=...)`` still blocked
    forever on a silent device -- a guard against vacuous guards that was itself
    vacuous, and it blessed a real hang.

    Two mechanisms actually work. A watchdog *timer* (log_monitor): an in-loop
    clock check cannot fire while ``readline()`` is blocked, because the loop
    body never runs. Or a read that cannot block: ``select()`` with a timeout
    bounded in the source, then ``os.read`` of what the fd holds (anr_watcher
    since PR #26, whose reader had to notice a QUIET stream anyway). See
    :func:`_bounds_its_reads` for what counts, and the negative controls below
    for two shapes that once passed and must not. The behavioural proof that
    ``watch()`` returns on a device emitting nothing lives in
    tests/test_review_findings.py.
    """
    tree = ast.parse((SCRIPTS / module).read_text(encoding="utf-8"), filename=module)

    assert _arms_a_watchdog_timer(tree) or _bounds_its_reads(tree), (
        f"{module} claims a streaming exemption but arms no watchdog timer and "
        f"bounds no read. An in-loop duration check cannot fire while a read is "
        f"blocked, so the stream hangs on a device that logs nothing."
    )


# Shapes the guard must refuse. Each passed the version of it that only asked
# whether a select had a fourth argument (both demonstrated in the review of
# PR #26). Python source, not tool output: the guard reads code.
_READS_THAT_CAN_STILL_BLOCK = {
    "readline-after-a-bounded-select": (
        "import select\n"
        "def read(stream):\n"
        "    ready, _, _ = select.select([stream], [], [], 0.25)\n"
        "    return stream.readline()\n"
    ),
    "select-with-no-timeout": (
        "import os, select\n"
        "def read(fd):\n"
        "    ready, _, _ = select.select([fd], [], [], None)\n"
        "    return os.read(fd, 65536)\n"
    ),
    "buffered-read-after-a-bounded-select": (
        "import select\n"
        "def read(stream):\n"
        "    ready, _, _ = select.select([stream], [], [], 0.25)\n"
        "    return stream.read(65536)\n"
    ),
    "os-read-beside-a-readline": (
        "import os, select\n"
        "def read(fd, stream):\n"
        "    ready, _, _ = select.select([fd], [], [], 0.25)\n"
        "    return os.read(fd, 1) + stream.readline()\n"
    ),
    "select-timeout-not-bounded-in-the-source": (
        "import os, select\n"
        "def read(fd, timeout):\n"
        "    ready, _, _ = select.select([fd], [], [], timeout)\n"
        "    return os.read(fd, 65536)\n"
    ),
}


@pytest.mark.parametrize(
    "source", list(_READS_THAT_CAN_STILL_BLOCK.values()), ids=list(_READS_THAT_CAN_STILL_BLOCK)
)
def test_the_watchdog_guard_refuses_a_read_that_can_still_block(source: str):
    tree = ast.parse(source)
    assert not _arms_a_watchdog_timer(tree)
    assert not _bounds_its_reads(tree)


def test_the_watchdog_guard_accepts_a_bounded_unbuffered_read():
    """The positive control: the shape anr_watcher's reader has."""
    tree = ast.parse(
        "import os, select, time\n"
        "POLL = 0.25\n"
        "def read(fd, deadline):\n"
        "    remaining = max(0.0, deadline - time.monotonic())\n"
        "    ready, _, _ = select.select([fd], [], [], min(remaining, POLL))\n"
        "    return os.read(fd, 65536) if ready else b''\n"
    )
    assert _bounds_its_reads(tree)


# The modes of a timer-bounded streaming module: what bounds each, and how it ends.
#
# A Timer is armed per run, by the mode the run is in, so "the module constructs
# a Timer" -- the check above -- says nothing about the next mode: log_monitor
# passed it while `--last` armed none, and a dump whose pipe stalled (three bytes
# of a record's header, then nothing) blocked until something outside closed it
# (review 2 of v0.8.0, item 10). Nor does finding the mode's bound in some
# Timer's interval prove the timer works: an unreachable branch, a timer never
# started and a callback that does nothing all pass that (review of PR #36). So
# each mode -- each flag of the module's mutually exclusive mode group -- is
# listed here and RUN: the real CLI, with its bound shortened, over a device
# that answers `get-state` and then writes nothing, must end within HUNG_AFTER
# times that bound, as the mode ends there. A mode with no bound is listed in
# UNTIMED_MODES with why.
#
# A module whose reads cannot block (anr_watcher: select() bounded in the
# source, then os.read; see _bounds_its_reads) is bounded per read, in every
# mode that reads, so it is not listed; its `--since` dump goes through
# common.adb_exec.run_argv, bounded by subprocess's own timeout.


@dataclass(frozen=True)
class TimedMode:
    """One mode of a timer-bounded stream: what bounds it, and how it ends at its bound.

    Attributes:
        bound: The name its Timer's interval is computed from (the secondary,
            AST check).
        argv: Arguments that select the mode with a short bound.
        seconds: The bound ``argv`` and ``env`` give.
        status: The exit status the mode ends with at that bound, over a device
            that answers ``get-state`` and then writes nothing.
        says: Text its stdout carries then.
        env: Environment that shortens the bound.
    """

    bound: str
    argv: tuple[str, ...]
    seconds: float
    status: int
    says: str
    env: dict[str, str] = field(default_factory=dict)


TIMED_MODES: dict[str, dict[str, TimedMode]] = {
    "log_monitor.py": {
        # The caller's own deadline: the capture ends there, empty, at exit 0.
        "--duration": TimedMode(
            bound="duration",
            argv=("--duration", "2s", "--json"),
            seconds=2,
            status=0,
            says='"total_lines": 0',
        ),
        # A dump's: twice ANDROID_EMU_ADB_TIMEOUT. It ends by itself, or it is
        # refused once the device is asked again.
        "--last": TimedMode(
            bound="DUMP_TIMEOUT",
            argv=("--last", "5m", "--json"),
            seconds=2,
            status=1,
            says="did not finish within 2s",
            env={"ANDROID_EMU_ADB_TIMEOUT": "1"},
        ),
    },
}
UNTIMED_MODES: dict[str, dict[str, str]] = {
    "log_monitor.py": {
        # As does a run with no mode flag at all.
        "--follow": "streams until the caller interrupts it; no end was asked for",
    },
}

# A mode's run counts as hung past this many times its bound: room for the
# interpreter and the stubs to start, and for the stopped read to be reaped.
HUNG_AFTER = 3


def _mode_flags(tree: ast.Module) -> set[str]:
    """The flags added to any ``add_mutually_exclusive_group()`` group in ``tree``.

    Through the name the group is bound to: ``time_group =
    parser.add_mutually_exclusive_group()``, then ``time_group.add_argument(...)``.
    """
    groups = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "attr", None) == "add_mutually_exclusive_group"
    }
    return {
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "add_argument"
        and getattr(getattr(node.func, "value", None), "id", None) in groups
        for arg in node.args
        if isinstance(arg, ast.Constant) and str(arg.value).startswith("--")
    }


def _timer_interval_names(tree: ast.Module) -> set[str]:
    """Every name some constructed ``Timer``'s interval is computed from.

    ``threading.Timer(float(DUMP_TIMEOUT), stop)`` gives ``{"float", "DUMP_TIMEOUT"}``.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or "Timer" not in (
            getattr(node.func, "attr", None),
            getattr(node.func, "id", None),
        ):
            continue
        interval = (
            node.args[0]
            if node.args
            else next((k.value for k in node.keywords if k.arg == "interval"), None)
        )
        if interval is not None:
            names.update(name.id for name in ast.walk(interval) if isinstance(name, ast.Name))
    return names


def _bindings(tree: ast.Module) -> set[str]:
    """Module-level assignment targets and every function parameter in ``tree``."""
    module_level = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    parameters = {
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    }
    return module_level | parameters


def _parse(module: str) -> ast.Module:
    return ast.parse((SCRIPTS / module).read_text(encoding="utf-8"), filename=module)


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _why_it_did_not_end(
    toolchain: FakeToolchain, recorded, script: Path, mode: TimedMode, **env: str
) -> str | None:
    """Run ``script`` in ``mode`` over a device that never writes; why it did not end on time.

    The device answers ``get-state`` as recorded (adb_get_state), and its
    ``logcat`` prints adb_waiting_for_device's banner and never exits (``hang``),
    so only the mode's own deadline can end the read.

    Returns:
        None when the run ended within ``HUNG_AFTER`` times the mode's bound, with
        the mode's status and words; otherwise what it did instead.
    """
    toolchain.install(
        "adb",
        [
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
        ],
    )
    limit = HUNG_AFTER * mode.seconds
    try:
        result = toolchain.run(script, *mode.argv, timeout=limit, **mode.env, **env)
    except subprocess.TimeoutExpired:
        return f"still running {limit}s into its {mode.seconds}s bound"
    shown = f"stdout {result.stdout[-300:]!r}, stderr {result.stderr[-300:]!r}"
    if result.returncode != mode.status:
        return f"exit {result.returncode}, not {mode.status}; {shown}"
    if mode.says not in result.stdout:
        return f"stdout does not say {mode.says!r}; {shown}"
    return None


_TIMED = [(module, mode) for module, modes in TIMED_MODES.items() for mode in modes]


@pytest.mark.parametrize(("module", "mode"), _TIMED, ids=[f"{m}{mode}" for m, mode in _TIMED])
def test_each_timed_mode_ends_at_its_bound(module: str, mode: str, toolchain, recorded):
    """The guard proper: the real CLI in that mode, over a device that never writes, ends on time."""
    spec = TIMED_MODES[module][mode]
    reason = _why_it_did_not_end(toolchain, recorded, SCRIPTS / module, spec)
    assert reason is None, (
        f"{module} {mode} did not end at its {spec.seconds}s bound: {reason}. Its own timer "
        f"must be armed, started and able to stop the read; another mode's does not bound it."
    )


# Three ways a deadline can be there in name and not in fact. The review of PR
# #36 passed each through the name check below; the run must refuse every one.
# Applied to a copy of log_monitor.py, which imports `common` from the real
# scripts/ through PYTHONPATH -- and the copy unchanged must still end on time,
# so a control cannot pass because the copy cannot run. Each replaced text must
# be in log_monitor.py exactly once: a control that no longer matches fails
# rather than going quietly vacuous. Python source, not tool output.
_DEAD_DEADLINES: dict[str, tuple[str, str]] = {
    "unchanged": ("", ""),
    "unreachable-branch": (
        "            if last_minutes is not None:\n"
        "                deadline_timer = threading.Timer(float(DUMP_TIMEOUT),",
        "            if last_minutes is not None and False:\n"
        "                deadline_timer = threading.Timer(float(DUMP_TIMEOUT),",
    ),
    "never-started": ("                deadline_timer.start()\n", ""),
    "no-op-callback": (
        "threading.Timer(float(DUMP_TIMEOUT), _stop_overrun_dump)",
        "threading.Timer(float(DUMP_TIMEOUT), lambda: None)",
    ),
}


@pytest.mark.parametrize("mutation", list(_DEAD_DEADLINES))
def test_the_run_refuses_a_deadline_there_only_in_name(
    mutation: str, toolchain, recorded, tmp_path
):
    old, new = _DEAD_DEADLINES[mutation]
    source = (SCRIPTS / "log_monitor.py").read_text(encoding="utf-8")
    if old:
        assert source.count(old) == 1, f"{mutation}: its text is not in log_monitor.py once"
    copy = tmp_path / "log_monitor.py"
    copy.write_text(source.replace(old, new) if old else source, encoding="utf-8")
    tree = ast.parse(copy.read_text(encoding="utf-8"))
    assert "DUMP_TIMEOUT" in _timer_interval_names(tree), "the name check passes every one"

    reason = _why_it_did_not_end(
        toolchain,
        recorded,
        copy,
        TIMED_MODES["log_monitor.py"]["--last"],
        PYTHONPATH=str(SCRIPTS),
    )
    if old:
        assert reason is not None, f"{mutation} ended at its bound: the run cannot see it"
    else:
        assert reason is None, f"the unchanged copy did not end at its bound: {reason}"


@pytest.mark.parametrize(
    ("module", "mode", "bound"),
    [
        (module, mode, spec.bound)
        for module, modes in TIMED_MODES.items()
        for mode, spec in modes.items()
    ],
    ids=lambda value: value,
)
def test_each_timed_mode_names_its_bound_in_a_timer(module: str, mode: str, bound: str):
    """Secondary to the run: the mode's bound is the interval of a Timer the module builds.

    Cheap and specific when it fails, but a name proves nothing about the timer
    working (the controls above), so the run is the guard.
    """
    tree = _parse(module)
    assert bound in _bindings(tree), (
        f"TIMED_MODES says {module} {mode} is bounded by {bound!r}, which {module} does not "
        f"define (a module constant or a parameter): the entry guards nothing."
    )
    assert bound in _timer_interval_names(tree), (
        f"{module} {mode} arms no watchdog: no Timer's interval is computed from {bound!r}. "
        f"Another mode's timer does not bound this one's read, which blocks until something "
        f"outside closes the pipe."
    )


@pytest.mark.parametrize("module", sorted(TIMED_MODES))
def test_every_mode_of_a_timed_stream_is_listed(module: str):
    """Exact, both ways: a new mode must say how it ends, and a removed one leaves the list."""
    flags = _mode_flags(_parse(module))
    assert flags, f"found no mode group in {module}: this guard would read nothing"
    listed = set(TIMED_MODES[module]) | set(UNTIMED_MODES.get(module, {}))
    assert flags == listed, (
        f"{module}'s modes are {sorted(flags)}; TIMED_MODES / UNTIMED_MODES list "
        f"{sorted(listed)}. Give each new mode its own bound (or the reason it has none) "
        f"and delete the entries of modes that are gone."
    )


@pytest.mark.parametrize("module", STREAMING)
def test_a_stream_bounded_by_a_timer_lists_its_modes(module: str):
    """A Timer bounds only the modes that arm it, so a module that uses one is listed by mode."""
    if _arms_a_watchdog_timer(_parse(module)):
        assert module in TIMED_MODES, (
            f"{module} bounds its stream with a Timer: list each of its modes in TIMED_MODES "
            f"(or UNTIMED_MODES, with the reason) so a mode that arms none is caught."
        )


# The shape log_monitor had: `--duration` armed a Timer and `--last` none. Python
# source, not tool output: the guard reads code.
_ONE_MODE_ARMED = (
    "import threading\n"
    "DUMP_TIMEOUT = 60\n"
    "def stream(duration, last_minutes, stop):\n"
    "    if duration:\n"
    "        threading.Timer(float(duration), stop).start()\n"
)


def test_the_per_mode_guard_refuses_what_the_module_check_passed():
    tree = ast.parse(_ONE_MODE_ARMED)
    assert _arms_a_watchdog_timer(tree), "the module-level check passes this shape"
    assert "duration" in _timer_interval_names(tree)
    assert "DUMP_TIMEOUT" not in _timer_interval_names(tree)


def test_the_mode_reader_reads_the_group_and_nothing_else():
    tree = ast.parse(
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "modes = parser.add_mutually_exclusive_group()\n"
        "modes.add_argument('--follow', action='store_true')\n"
        "modes.add_argument('--last', dest='window')\n"
        "parser.add_argument('--json', action='store_true')\n"
    )
    assert _mode_flags(tree) == {"--follow", "--last"}


# The modules allowed to call subprocess directly, by path under scripts/.
# Exact: a new bypass fails, and so does a stale entry -- a module that moves
# onto run_adb deletes its line here in the same change. A count (`<= 15`, what
# this was) let two new bypasses in while it still had headroom.
#
# common/adb_exec.py is not listed: it is the runner everything else goes
# through. privacy_manager.py left this list in Inc -1; container.py and
# model_inspector.py in Inc 2 (N5), when their byte reads moved to
# adb_exec.run_argv.
DIRECT_SUBPROCESS_MODULES: frozenset[str] = frozenset(
    {
        "anr_watcher.py",  # streams `adb logcat` in a detached worker
        "common/anr_sessions.py",  # asks the host's `ps` who holds a worker pid, not adb
        "common/sdk_tools.py",  # runs SDK binaries (emulator, avdmanager), not adb
        "emulator_boot.py",  # launches the emulator binary detached
        "emulator_create.py",  # avdmanager / sdkmanager
        "emulator_delete.py",  # avdmanager
        "emulator_selector.py",  # launches the emulator binary detached
        "gradle/builder.py",  # the Gradle wrapper
        "log_monitor.py",  # streams `adb logcat`
        "push_notification.py",  # its own bounded _shell runner (debt)
        "test_recorder.py",  # emits a logcat marker
    }
)


def test_adb_goes_through_the_shared_runner():
    """Exactly the listed modules call subprocess directly; everything else
    reaches adb through common.adb_exec.run_adb."""
    direct = {
        path.relative_to(SCRIPTS).as_posix()
        for path in _script_files()
        if path.name != "adb_exec.py"
        and _subprocess_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    }
    assert direct - DIRECT_SUBPROCESS_MODULES == set(), (
        f"{sorted(direct - DIRECT_SUBPROCESS_MODULES)} call subprocess directly. Route adb "
        f"through common.adb_exec.run_adb; if it genuinely cannot, add it to "
        f"DIRECT_SUBPROCESS_MODULES with the reason."
    )
    assert DIRECT_SUBPROCESS_MODULES - direct == set(), (
        f"{sorted(DIRECT_SUBPROCESS_MODULES - direct)} no longer call subprocess. Delete "
        f"them from DIRECT_SUBPROCESS_MODULES -- the bypass is paid off."
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import subprocess\nsubprocess.run(['adb'])\n", [(2, "run")]),
        ("import subprocess as sp\nsp.Popen(['adb'])\n", [(2, "Popen")]),
        ("from subprocess import run\nrun(['adb'])\n", [(2, "run")]),
        ("from subprocess import Popen as P\nP(['adb'])\n", [(2, "Popen")]),
        # The shared CLI boundary, and anything else that merely has a .run:
        ("from common import cli\ncli.run(main)\n", []),
        ("import asyncio\nasyncio.run(main())\n", []),
        ("import subprocess\nsubprocess.run(['adb'], timeout=5)\n", []),
    ],
    ids=["module", "module-alias", "from-import", "from-import-alias", "cli", "asyncio", "bounded"],
)
def test_the_matcher_is_bound_to_the_subprocess_module(tmp_path, source, expected):
    """Both directions: every spelling of subprocess counts, and nothing else does."""
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    assert _unbounded_calls(path) == expected
