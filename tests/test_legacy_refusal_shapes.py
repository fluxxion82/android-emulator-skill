"""The refusals that used to print their own shape now print common.cli's.

Before v0.8.0 Inc 2, a dozen scripts printed a failure themselves and exited:
``{"error": ...}`` from a script-local ``_fail`` (emulator_boot,
emulator_erase, emulator_shutdown, navigator, app_launcher), ``{"success":
false, "message": ...}`` (emulator_create, gesture, keyboard, location,
privacy_manager, model_inspector's LIVE mode), ``Error: ...`` alone on stderr
(model_inspector ``--raw``, location's GPX, app_launcher's ``--args``,
appearance's contradictory flags, build_and_test's unknown build id), and the
whole ``--help`` text on STDOUT when
no action was given (app_launcher, appearance, emulator_boot, emulator_create,
gesture, keyboard, status_bar) -- which, under ``--json``, was the entire
answer. None carried a remedy, and an agent had to know which script it ran to
know where the reason was written.

Each case here is reachable with no device and no Android tool at all, so it
runs as the real CLI in an empty toolchain (``fake_toolchain``, nothing
installed): the refusal is decided before anything is asked of a device. The
device-dependent refusals are pinned beside their scripts (test_app_launcher,
test_navigator*, test_privacy_manager, test_model_inspector, test_snapshot,
test_sms) and swept with a broken and an absent toolchain in
test_exit_code_sweep.

Every case is checked in both output modes with ``assert_refusal`` -- one
``{"success": false, "error", "remedy"}`` object, or ``Error:`` then
``Remedy:`` on stderr with no JSON on stdout -- and none may carry the legacy
``message`` key.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest
from fake_toolchain import FakeToolchain, assert_refusal


class Case(NamedTuple):
    script: str
    argv: tuple[str, ...]
    naming: tuple[str, ...]
    why: str
    # Builds whatever the case needs on disk; returns extra env vars.
    setup: Callable[[Path], dict[str, str]] | None = None

    @property
    def ident(self) -> str:
        return f"{self.script.removesuffix('.py')} {' '.join(self.argv)}".strip()


def _empty_avd_home(root: Path) -> dict[str, str]:
    """An AVD home that exists and holds no AVD: a name in it is "not found"."""
    home = root / "avd-home"
    home.mkdir()
    return {"ANDROID_AVD_HOME": str(home)}


CASES = (
    Case("emulator_boot.py", (), ("--avd",), "no AVD named: help text was stdout"),
    Case("emulator_erase.py", (), ("--name",), "no AVD named: `{error}` from _fail"),
    Case(
        "emulator_erase.py",
        ("--name", "NoSuchAvd"),
        ("not found",),
        "an AVD this host does not have: `{success: false, message}`, then `{error}`",
        _empty_avd_home,
    ),
    # The handset refusal needs `adb devices` to say what the serial is; it is
    # pinned through cli.run in test_emulator_shutdown.
    Case("emulator_shutdown.py", (), ("--serial",), "no target: `{error}` from _fail"),
    Case("emulator_create.py", (), ("--device",), "no --device: help text was stdout"),
    Case("gesture.py", (), ("gesture",), "no gesture: help text was stdout"),
    Case(
        "gesture.py",
        ("--long-press", "abc"),
        ("--long-press",),
        "malformed coordinates: `{success: false, message}`",
    ),
    Case("keyboard.py", (), ("keyboard",), "no action: help text was stdout"),
    Case(
        "keyboard.py",
        ("--button", "nosuch"),
        ("unknown key",),
        "an unknown button: `{success: false, message}`",
    ),
    Case("app_launcher.py", (), ("no operation",), "no operation: help text was stdout"),
    Case(
        "app_launcher.py",
        ("--launch", "com.example.app", "--args", "bad"),
        ("--args",),
        "a malformed intent extra: `Error:` alone, nothing under --json",
    ),
    Case("appearance.py", (), ("appearance",), "no change: help text was stdout"),
    Case(
        "appearance.py",
        ("--reset", "--theme", "dark"),
        ("--reset",),
        "contradictory flags: `Error:` alone, nothing under --json",
    ),
    Case(
        "appearance.py",
        ("--text-size", "large", "--font-scale", "1.2"),
        ("mutually exclusive",),
        "the same",
    ),
    Case("status_bar.py", (), ("status bar",), "no operation: help text was stdout"),
    Case(
        "build_and_test.py",
        ("--get-errors", "nosuchid"),
        ("not found",),
        "an unknown build id: `Error:` and a hint on stderr, nothing under --json",
    ),
    Case(
        "build_and_test.py",
        ("--get-log", "nosuchid"),
        ("no build log",),
        "the same for --get-log, without even the `Error:` prefix",
    ),
    Case(
        "location.py",
        ("--gpx", "missing.gpx"),
        ("gpx",),
        "an unreadable GPX file: `Error:` alone, nothing under --json",
    ),
    Case(
        "navigator.py",
        ("--tap-at", "abc"),
        ("--tap-at",),
        "malformed coordinates: `{error}` from _fail, the text form on stdout",
    ),
    Case(
        "model_inspector.py",
        ("--source", "no-such-dir"),
        ("not found",),
        "SOURCE with nothing to read: `{success: false, error}` from _fail, no remedy",
    ),
    Case(
        "model_inspector.py",
        ("--schema", "no-such.json"),
        ("not found",),
        "SCHEMA with nothing to read: the same",
    ),
    Case(
        "model_inspector.py",
        ("--schema", "no-such.json", "--raw", "users"),
        ("not found",),
        "--raw: `Error:` alone on stderr, nothing under --json",
    ),
)


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.ident)
def test_the_refusal_is_the_shared_shape(case: Case, json_mode: bool, tmp_path):
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    extra = case.setup(tmp_path) if case.setup else {}
    argv = [*case.argv, *(["--json"] if json_mode else [])]

    result = chain.run(case.script, *argv, cwd=tmp_path, **extra)

    payload = assert_refusal(result, json_mode=json_mode, naming=case.naming, status=1)
    if payload is not None:
        assert "message" not in payload, f"the legacy `message` key is back: {payload}"
    assert not chain.calls(), f"{case.ident} reached for a tool: {chain.calls()}"


@pytest.mark.parametrize(
    "script", sorted({case.script for case in CASES if not case.argv}), ids=str
)
def test_help_text_goes_to_stderr_in_text_mode_and_nowhere_under_json(script: str, tmp_path):
    """The no-action refusals printed `--help` to stdout. Text mode keeps it, on stderr."""
    chain = FakeToolchain(tmp_path / "toolchain")

    text = chain.run(script, cwd=tmp_path)
    assert text.stdout == "", text.stdout
    assert "usage:" in text.stderr, text.stderr

    as_json = chain.run(script, "--json", cwd=tmp_path)
    assert "usage:" not in as_json.stdout + as_json.stderr
    assert set(json.loads(as_json.stdout)) == {"success", "error", "remedy"}


# ---------------------------------------------------------------------------
# No script prints a refusal itself.
# ---------------------------------------------------------------------------
#
# The shape every case above used to have, outside common.cli. Read from the
# AST, never by substring (a docstring that mentions `sys.exit(1)` is not a
# call). A function is flagged when it:
#
#   1. prints something SHAPED like a refusal, wherever it is -- `main`
#      included: `{"error": ...}` or `{"success": False, ...}` (as a literal,
#      or the literal handed to json.dumps), or text that starts `Error:`;
#   2. prints, and ends the process non-zero by itself (`sys.exit(1)`,
#      `raise SystemExit(2)`) -- any function but `main`; or
#   3. prints, and RETURNS a non-zero int to `main`, which exits with it --
#      `main` itself, or any function main reaches by name, however many hops.
#
# What 2 leaves to the runtime checks: `main` printing its answer and then
# exiting with a finding's status (accessibility_audit's critical gate,
# localization_audit --strict). Telling that from a refusal is the
# inexpressible rule test_exit_code_sweep.py's docstring describes; the sweep
# and the cases above check main's refusals by running them. Rule 1 and 3 are
# the holes the Codex review of #29 reproduced: `main` printing `{"error": ...}`
# and returning 1, and a printer one hop from main.

SCRIPTS = Path(__file__).resolve().parents[1] / "android-emulator-skill" / "skills"
SCRIPTS = SCRIPTS / "android-emulator-skill" / "scripts"
BOUNDARY = "common/cli.py"

_EXITS = frozenset({"sys.exit", "exit", "quit", "os._exit"})

# Exact. A new entry fails, and so does a stale one. The first four are usage
# errors (exit 2), which common.cli passes through untouched and has no form
# for yet; the routers' `_normalise_exit` is the interpreter's own convention.
KNOWN_FAILURE_PRINTERS: dict[tuple[str, str], str] = {
    ("emulator_delete.py", "_fail_usage"): "--old 0 and friends: `{error}` / `Error:`, exit 2",
    ("navigator.py", "error"): "ArgumentParser.error under --json: `{error}`, exit 2",
    ("avd.py", "main"): "an unknown or missing verb: the route guide on stderr, exit 2",
    ("logs.py", "main"): "an unknown or missing verb: the route guide on stderr, exit 2",
    ("avd.py", "_normalise_exit"): (
        "a delegate's sys.exit('message') printed and made 1, as the interpreter does; "
        "every delegate refuses through common.cli, so nothing takes this path today"
    ),
    ("logs.py", "_normalise_exit"): "the same router convention as avd.py",
    ("test_recorder.py", "_run_step"): (
        "a step that captured nothing: the printed step is the result, and exit 1 is its finding"
    ),
    ("test_recorder.py", "_run_stop"): (
        "a recorded test marked failed: the printed session is the result, and exit 1 is its finding"
    ),
}


def _non_zero_int(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value != 0
    )


def _exits_non_zero(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and ast.unparse(node.func) in _EXITS:
        return _non_zero_int(node.args[0] if node.args else None)
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        if ast.unparse(node.exc.func) == "SystemExit" and node.exc.args:
            return _non_zero_int(node.exc.args[0])
    return False


def _returns_non_zero(node: ast.AST) -> bool:
    if not isinstance(node, ast.Return):
        return False

    def can_be_non_zero(value: ast.AST | None) -> bool:
        if _non_zero_int(value):
            return True
        if isinstance(value, ast.IfExp):
            return can_be_non_zero(value.body) or can_be_non_zero(value.orelse)
        return False

    return can_be_non_zero(node.value)


def _prints(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = ast.unparse(node.func)
    return name == "print" or name.endswith(".print_help")


def _refusal_shaped(node: ast.AST, assignments: dict[str, ast.AST] | None = None) -> bool:
    """A print of `{"error": ...}`, `{"success": False, ...}`, or text starting `Error:`."""
    if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "print" and node.args):
        return False
    shown = node.args[0]
    if isinstance(shown, ast.Call) and ast.unparse(shown.func).endswith("dumps") and shown.args:
        shown = shown.args[0]
    seen: set[str] = set()
    while isinstance(shown, ast.Name) and assignments and shown.id not in seen:
        seen.add(shown.id)
        replacement = assignments.get(shown.id)
        if replacement is None:
            break
        shown = replacement
    if isinstance(shown, ast.Dict):
        for key, value in zip(shown.keys, shown.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "error":
                return True
            if (
                isinstance(key, ast.Constant)
                and key.value == "success"
                and isinstance(value, ast.Constant)
                and value.value is False
            ):
                return True
    if isinstance(shown, ast.JoinedStr) and shown.values:
        shown = shown.values[0]
    return (
        isinstance(shown, ast.Constant)
        and isinstance(shown.value, str)
        and shown.value.lower().startswith("error:")
    )


def _reached_from_main(tree: ast.Module) -> set[str]:
    """Module-level functions `main` calls by name, transitively, and `main`."""
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "main" not in functions:
        return set()
    reached, frontier = {"main"}, ["main"]
    while frontier:
        for inner in ast.walk(functions[frontier.pop()]):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id in functions
                and inner.func.id not in reached
            ):
                reached.add(inner.func.id)
                frontier.append(inner.func.id)
    return reached


def failure_printers(source: str) -> set[str]:
    """Names of the functions that print a refusal themselves (rules 1-3 above)."""
    tree = ast.parse(source)
    reached = _reached_from_main(tree)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        inner = list(ast.walk(node))
        assigned: dict[str, list[ast.AST]] = {}
        for statement in inner:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(statement.value)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                if statement.value is not None:
                    assigned.setdefault(statement.target.id, []).append(statement.value)
        assignments = {name: values[0] for name, values in assigned.items() if len(values) == 1}
        prints = any(map(_prints, inner))
        if (
            any(_refusal_shaped(item, assignments) for item in inner)
            or (node.name != "main" and prints and any(map(_exits_non_zero, inner)))
            or (node.name in reached and prints and any(map(_returns_non_zero, inner)))
        ):
            found.add(node.name)
    return found


def test_no_script_prints_a_refusal_itself():
    found = {
        (path.relative_to(SCRIPTS).as_posix(), name)
        for path in sorted(SCRIPTS.rglob("*.py"))
        if path.relative_to(SCRIPTS).as_posix() != BOUNDARY
        for name in failure_printers(path.read_text(encoding="utf-8"))
    }
    new = sorted(found - set(KNOWN_FAILURE_PRINTERS))
    stale = sorted(set(KNOWN_FAILURE_PRINTERS) - found)
    assert not new, (
        f"{new} print a refusal themselves. Raise instead -- cli.fail(error, remedy) -- "
        f"so common.cli reports it in the one shape."
    )
    assert not stale, f"KNOWN_FAILURE_PRINTERS lists {stale}, which are gone: delete them."


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import sys\ndef _fail(m):\n    print(m)\n    sys.exit(1)\n", {"_fail"}),
        ("def _usage(p):\n    p.print_help()\n    raise SystemExit(2)\n", {"_usage"}),
        # The two holes the review reproduced.
        (
            "import json\ndef main():\n    print(json.dumps({'error': 'x'}))\n    return 1\n",
            {"main"},
        ),
        (
            "def _show():\n    print('no build log')\n    return 1\n"
            "def main():\n    return _show()\n",
            {"_show"},
        ),
        (
            "import json\ndef main():\n    payload = {'success': False}\n"
            "    print(json.dumps(payload))\n    return 0\n",
            {"main"},
        ),
        (
            "def main(ok):\n    print('build result')\n    return 0 if ok else 1\n",
            {"main"},
        ),
        # Shaped like a refusal wherever it is, whatever happens next.
        ("import sys\ndef _say(m):\n    print(f'Error: {m}', file=sys.stderr)\n", {"_say"}),
        ("def _say(m):\n    print({'success': False, 'message': m})\n", {"_say"}),
        # Not a refusal: a success that exits 0, a raise, a docstring, main's
        # finding gate, a print-and-return-1 nobody reaches from main, a True.
        ("import sys\ndef done():\n    print('ok')\n    sys.exit(0)\n", set()),
        ("from common import cli\ndef _f(m):\n    cli.fail(m, 'r')\n", set()),
        ('def _f():\n    """print({"error": 1}); sys.exit(1)"""\n', set()),
        ("import sys\ndef main():\n    print('3 critical')\n    sys.exit(1)\n", set()),
        ("def _orphan():\n    print('x')\n    return 1\ndef main():\n    return 0\n", set()),
        ("def _ok():\n    print('x')\n    return True\ndef main():\n    _ok()\n", set()),
    ],
    ids=[
        "fail-helper",
        "usage-helper",
        "main-prints-error-returns-1",
        "printer-one-hop-from-main",
        "variable-built-failure-payload",
        "conditional-nonzero-return",
        "text-error-anywhere",
        "success-false-anywhere",
        "success",
        "raises",
        "docstring",
        "main-finding-gate",
        "unreached-helper",
        "returns-true",
    ],
)
def test_the_printer_matcher(source, expected):
    """Both directions: the shapes it must see, and the ones it must not."""
    assert failure_printers(source) == expected
