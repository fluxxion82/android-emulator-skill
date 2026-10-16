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
deadline, so that is asserted too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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

    The mechanism that actually works is a watchdog *timer*: an in-loop clock
    check cannot fire while ``readline()`` is blocked, because the loop body
    never runs. That is what is asserted here; the behavioural proof that
    ``watch()`` returns on a device emitting nothing lives in
    tests/test_review_findings.py.
    """
    tree = ast.parse((SCRIPTS / module).read_text(encoding="utf-8"), filename=module)

    arms_timer = any(
        isinstance(node, ast.Call)
        and (
            getattr(node.func, "attr", None) == "Timer" or getattr(node.func, "id", None) == "Timer"
        )
        for node in ast.walk(tree)
    )
    assert arms_timer, (
        f"{module} claims a streaming exemption but arms no watchdog timer. An "
        f"in-loop duration check cannot fire while readline() is blocked, so "
        f"without a timer the stream hangs on a device that logs nothing."
    )


# The modules allowed to call subprocess directly, by path under scripts/.
# Exact: a new bypass fails, and so does a stale entry -- a module that moves
# onto run_adb deletes its line here in the same change. A count (`<= 15`, what
# this was) let two new bypasses in while it still had headroom.
#
# common/adb_exec.py is not listed: it is the runner everything else goes
# through. privacy_manager.py left this list in Inc -1.
DIRECT_SUBPROCESS_MODULES: frozenset[str] = frozenset(
    {
        "anr_watcher.py",  # streams `adb logcat` in a detached worker
        "common/sdk_tools.py",  # runs SDK binaries (emulator, avdmanager), not adb
        "container.py",  # `adb exec-out run-as`: binary-safe bytes, not text
        "emulator_boot.py",  # launches the emulator binary detached
        "emulator_create.py",  # avdmanager / sdkmanager
        "emulator_delete.py",  # avdmanager
        "emulator_selector.py",  # launches the emulator binary detached
        "gradle/builder.py",  # the Gradle wrapper
        "log_monitor.py",  # streams `adb logcat`
        "model_inspector.py",  # `adb exec-out run-as`: binary-safe bytes
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
