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


def _unbounded_calls(path: Path) -> list[tuple[int, str]]:
    """Line numbers of subprocess run/Popen calls with no ``timeout=``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = getattr(node.func, "attr", None)
        if callee not in ("run", "Popen"):
            continue
        if not any(keyword.arg == "timeout" for keyword in node.keywords):
            found.append((node.lineno, callee))
    return found


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


def test_adb_goes_through_the_shared_runner():
    """Scripts should not reach adb directly now that run_adb exists.

    Not a hard rule for every module yet -- common/ and the streaming modules
    still build their own commands -- so this asserts the direction of travel by
    pinning how many modules still call subprocess directly. Ratcheting this
    number down is the migration; it must never go up.
    """
    direct = []
    for path in _script_files():
        if path.name in ("adb_exec.py",):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("run", "Popen"):
                direct.append(path.name)
                break

    # Ratchet. Lower this as modules migrate; never raise it.
    assert len(set(direct)) <= 15, (
        f"{len(set(direct))} modules still call subprocess directly: "
        f"{sorted(set(direct))}. Route new work through common.adb_exec.run_adb."
    )
