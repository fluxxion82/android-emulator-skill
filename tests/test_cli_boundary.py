"""Every script hands its failures to one boundary: ``common.cli.run``.

Why this exists
---------------
"Fixed in one file, alive in another" is the dominant defect class in two
consecutive retrospectives, and this is what generated it: every script owned
its own error boundary. Fifteen called ``resolve_device_identifier`` inside
their own ``main()`` behind divergent ``except`` clauses, so the same failure --
no device, a missing ``adb``, a screen that would not dump -- answered in four
shapes depending on which script the agent happened to run. Fixing the shape in
one ``main()`` fixed one of thirty-two copies.

``common/adb_exec.py`` made an unbounded adb call impossible by giving it one
place to live. This file does the same for a failure: the typed family
(``AdbError`` and its subclasses, ``HierarchyError``, the ``RuntimeError`` from
device resolution, ``OSError`` from a tool that will not run) is reported by
``common.cli.run`` and nowhere else.

What is asserted, and how
-------------------------
All of it is read from the AST, never by substring: a substring check on a
source file matches its own docstrings and comments, and this repo has shipped
a guard that blessed the defect it was written for that way.

(a) Each script's ``if __name__ == "__main__":`` block is exactly
    ``cli.run(main)`` (or ``common.cli.run(main)``), with ``cli`` bound by the
    import that names the real module.
(b) No ``except`` handler that catches the typed family ends the process with
    ``sys.exit`` (or ``exit``, ``quit``, ``os._exit``, ``raise SystemExit``).
    Catching *and continuing* is still allowed -- a batch loop that records one
    device's failure and moves on to the next is not a boundary.
(c) The same thing spelled another way is held to an exact, frozen list of
    known debt: a helper that exits (``_fail(error)``); any ``return`` from a
    handler inside ``main()`` (``return 1`` is a second boundary, a bare
    ``return`` is a failure turned into exit 0); and ``return`` / ``return
    None`` / ``return False`` from a handler in a module-level helper that
    ``main`` reaches, which hands ``main`` a falsy "it worked". (b) alone would
    be satisfied by moving the ``sys.exit`` one function away, which is
    precisely the shape seven scripts already had. Methods and imported helpers
    are outside this analysis -- see the PR that introduced it.
(d) ``main`` honours the argv it is handed, because ``cli.run`` hands it one:
    ``--help`` is run for every script in a fresh interpreter, with no Android
    tool reachable and none invoked.
(e) Exactly one library module ends the process: ``common/cli.py``.

The script set is written out below rather than globbed, so a new script has
to be added here by hand and an empty glob cannot satisfy any of it.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest

from common import cli

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "android-emulator-skill"
    / "skills"
    / "android-emulator-skill"
    / "scripts"
)

# Every script, by name. Positive on purpose: "no script bypasses the boundary"
# is also what a glob that matched nothing would report.
EXPECTED_SCRIPTS: frozenset[str] = frozenset(
    {
        "accessibility_audit.py",
        "anr_watcher.py",
        "app_launcher.py",
        "app_state_capture.py",
        "appearance.py",
        "avd.py",
        "build_and_test.py",
        "container.py",
        "crash_triage.py",
        "device_list.py",
        "emulator_boot.py",
        "emulator_create.py",
        "emulator_delete.py",
        "emulator_erase.py",
        "emulator_selector.py",
        "emulator_shutdown.py",
        "gesture.py",
        "keyboard.py",
        "localization_audit.py",
        "location.py",
        "log_monitor.py",
        "logs.py",
        "model_inspector.py",
        "navigator.py",
        "privacy_manager.py",
        "push_notification.py",
        "screen_mapper.py",
        "sms.py",
        "snapshot.py",
        "status_bar.py",
        "test_recorder.py",
        "visual_diff.py",
    }
)

SCRIPT_NAMES = sorted(EXPECTED_SCRIPTS)

# The one library module allowed to end the process.
BOUNDARY_MODULE = "common/cli.py"


def _tree(relative: str) -> ast.Module:
    path = SCRIPTS / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_enumeration_is_every_script_on_disk():
    """32 names, and exactly the 32 that exist -- in both directions."""
    on_disk = frozenset(path.name for path in SCRIPTS.glob("*.py"))
    assert len(EXPECTED_SCRIPTS) == 32
    assert on_disk == EXPECTED_SCRIPTS, (
        f"scripts on disk but not enumerated: {sorted(on_disk - EXPECTED_SCRIPTS)}; "
        f"enumerated but missing: {sorted(EXPECTED_SCRIPTS - on_disk)}. A new script "
        f"is added to EXPECTED_SCRIPTS by hand, and its __main__ is cli.run(main)."
    )


# ===========================================================================
# (a) the __main__ block
# ===========================================================================


def _is_main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":`` spelled the one conventional way."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _imports_cli_from_common(tree: ast.Module) -> bool:
    """``from common import cli`` at module level, unaliased."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "common"
        and node.level == 0
        and any(alias.name == "cli" and alias.asname is None for alias in node.names)
        for node in tree.body
    )


def _imports_common_cli(tree: ast.Module) -> bool:
    """``import common.cli`` at module level, unaliased."""
    return any(
        isinstance(node, ast.Import)
        and any(alias.name == "common.cli" and alias.asname is None for alias in node.names)
        for node in tree.body
    )


@pytest.mark.parametrize("script", SCRIPT_NAMES)
def test_the_main_block_is_only_cli_run_main(script: str):
    tree = _tree(script)
    blocks = [node for node in tree.body if _is_main_guard(node)]
    assert len(blocks) == 1, f'{script} has {len(blocks)} `if __name__ == "__main__":` blocks'
    block = blocks[0]
    assert not block.orelse, f"{script}'s __main__ block has an else branch"

    body = [ast.unparse(statement) for statement in block.body]
    assert len(block.body) == 1, f"{script}'s __main__ block is {body}, not one cli.run(main)"

    statement = block.body[0]
    call = statement.value if isinstance(statement, ast.Expr) else None
    assert isinstance(call, ast.Call), f"{script}'s __main__ block is {body}, not cli.run(main)"

    callee = ast.unparse(call.func)
    assert callee in ("cli.run", "common.cli.run"), (
        f"{script}'s __main__ block calls {callee}(), not cli.run(main). Every script "
        f"reports its failures through the one boundary; see common/cli.py."
    )
    assert [ast.unparse(arg) for arg in call.args] == [
        "main"
    ], f"{script} passes {[ast.unparse(arg) for arg in call.args]} to {callee}, not main"
    for keyword in call.keywords:
        assert (
            keyword.arg == "json_flags"
        ), f"{script} passes {keyword.arg}= to {callee}; only json_flags= is allowed"
        flags = ast.literal_eval(keyword.value)
        assert (
            isinstance(flags, tuple) and "--json" in flags
        ), f"{script}'s json_flags must be a tuple that still includes --json: {flags!r}"

    bound = _imports_cli_from_common(tree) if callee == "cli.run" else _imports_common_cli(tree)
    assert bound, (
        f"{script} calls {callee} but does not import it from the shared module "
        f"(`from common import cli` / `import common.cli`)"
    )
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body
    ), f"{script} has no module-level main() for cli.run to call"


# ===========================================================================
# (b) / (c) no handler for the typed family ends the process
# ===========================================================================

# What cli.run reports, read from cli.run itself rather than copied: a copy is
# how `cli.Failure` was left out, so `except cli.Failure: return 1` -- a second
# boundary for the one class only the boundary should ever catch -- produced no
# finding. A handler that catches any of these, or anything they inherit from,
# is a second boundary if it then ends the process.
FAMILY_ROOTS: tuple[type[BaseException], ...] = cli.TYPED_FAMILY

# Family roots that are defined under scripts/ themselves (cli.Failure), by
# name, so `except Failure` after `from common.cli import Failure` -- and any
# class built on it -- resolves through the AST class map as well.
_DEFINED_ROOTS = frozenset(
    root.__name__ for root in FAMILY_ROOTS if root.__module__.startswith("common.")
)

# Modules whose exception classes the scripts name in `except` clauses, by the
# alias the scripts use. A name that resolves nowhere fails the test rather than
# being assumed harmless: an unresolvable name is a blind spot.
_MODULES = {
    "subprocess": subprocess,
    "json": json,
    "sqlite3": sqlite3,
    "ET": ET,
    "cli": cli,
    "common.cli": cli,
}


@cache
def _defined_classes() -> dict[str, tuple[str, ...]]:
    """Every class defined under scripts/, mapped to the last name of each base.

    A name defined twice (``RunAsDeniedError`` lives in two scripts) keeps the
    bases of both, which only ever widens what counts as the family.
    """
    bases: dict[str, set[str]] = {}
    for path in SCRIPTS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names = bases.setdefault(node.name, set())
                for base in node.bases:
                    names.add(ast.unparse(base).rsplit(".", 1)[-1])
    return {name: tuple(sorted(found)) for name, found in bases.items()}


def _runtime_class(spelling: str) -> type | None:
    """The real class a spelling names, for builtins and the modules above."""
    if "." in spelling:
        module, _, attr = spelling.rpartition(".")
        owner = _MODULES.get(module)
        value = getattr(owner, attr, None) if owner is not None else None
    else:
        value = getattr(builtins, spelling, None)
    return value if isinstance(value, type) and issubclass(value, BaseException) else None


def _inherits_family(spelling: str, seen: frozenset[str] = frozenset()) -> bool:
    """Whether catching ``spelling`` catches a member of the family by inheritance."""
    runtime = _runtime_class(spelling)
    if runtime is not None:
        return issubclass(runtime, FAMILY_ROOTS)
    name = spelling.rsplit(".", 1)[-1]
    if name in _DEFINED_ROOTS:
        return True
    defined = _defined_classes()
    if name not in defined:
        raise LookupError(spelling)
    if name in seen:
        return False
    return any(_inherits_family(base, seen | {name}) for base in defined[name])


def _catches_family(spelling: str) -> bool:
    """Whether an ``except`` naming ``spelling`` catches some of the family.

    Either the class is in the family (``SdkToolError`` is a RuntimeError), or
    the family is in the class (``except Exception`` catches every one of them).
    """
    runtime = _runtime_class(spelling)
    if runtime is not None and any(issubclass(root, runtime) for root in FAMILY_ROOTS):
        return True
    return _inherits_family(spelling)


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("cli.Failure", True),
        ("common.cli.Failure", True),
        ("Failure", True),  # from common.cli import Failure
        ("SdkToolError", True),
        ("Exception", True),
        ("ValueError", False),
        ("KeyboardInterrupt", False),
        ("json.JSONDecodeError", False),
    ],
)
def test_the_family_resolver_covers_what_cli_run_catches(spelling, expected):
    assert _catches_family(spelling) is expected


def _caught(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["BaseException"]
    elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [ast.unparse(element) for element in elements]


DIRECT_EXITS = frozenset({"sys.exit", "exit", "quit", "os._exit"})


def _is_direct_exit(node: ast.AST) -> bool:
    """``sys.exit(...)`` and its spellings, or ``raise SystemExit``."""
    if isinstance(node, ast.Call):
        return ast.unparse(node.func) in DIRECT_EXITS
    if isinstance(node, ast.Raise) and node.exc is not None:
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        return ast.unparse(raised) == "SystemExit"
    return False


def _exiting_helpers(tree: ast.Module) -> set[str]:
    """Module-level functions that end the process, directly or through another."""
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    calls = {
        name: {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id in functions
        }
        for name, node in functions.items()
    }
    exiting = {
        name for name, node in functions.items() if any(map(_is_direct_exit, ast.walk(node)))
    }
    grew = True
    while grew:
        grew = False
        for name, callees in calls.items():
            if name not in exiting and callees & exiting:
                exiting.add(name)
                grew = True
    return exiting


class Handler(NamedTuple):
    """One ``except`` clause that catches the family and ends the process."""

    script: str
    function: str  # the enclosing function, "<module>" at top level
    caught: str  # the except clause's type, as written
    via: str  # "sys.exit" (and spellings), a helper's name, or "return"
    line: int

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.script, self.function, self.caught, self.via)

    def __str__(self) -> str:
        return f"{self.script}:{self.line} {self.function}() except {self.caught} -> {self.via}"


def _reachable_from_main(tree: ast.Module) -> set[str]:
    """Module-level functions ``main`` calls by name, transitively (``main`` excluded)."""
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "main" not in functions:
        return set()
    reached: set[str] = set()
    frontier = ["main"]
    while frontier:
        node = functions[frontier.pop()]
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id in functions
                and inner.func.id not in reached
                and inner.func.id != "main"
            ):
                reached.add(inner.func.id)
                frontier.append(inner.func.id)
    return reached


def _terminating_handlers(script: str) -> list[Handler]:
    """Every handler in ``script`` that catches the family and ends the process."""
    tree = _tree(script)
    helpers = _exiting_helpers(tree)
    reachable = _reachable_from_main(tree)
    found: list[Handler] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.ExceptHandler):
                caught = _caught(child)
                unresolved = []
                hits = []
                for spelling in caught:
                    try:
                        if _catches_family(spelling):
                            hits.append(spelling)
                    except LookupError:
                        unresolved.append(spelling)
                assert not unresolved, (
                    f"{script}:{child.lineno} catches {unresolved}, which the guard cannot "
                    f"resolve to a class. Teach _MODULES the module, or define the class "
                    f"under scripts/ -- an unresolved name is a blind spot."
                )
                if hits:
                    found.extend(_terminations(script, function, child, helpers, reachable))
            visit(child, function)

    visit(tree, "<module>")
    return found


def _is_falsy_return(node: ast.Return) -> bool:
    """``return``, ``return None`` or ``return False``."""
    return node.value is None or (
        isinstance(node.value, ast.Constant) and node.value.value in (None, False)
    )


def _terminations(
    script: str,
    function: str,
    handler: ast.ExceptHandler,
    helpers: set[str],
    reachable: set[str],
) -> list[Handler]:
    """How ``handler`` ends the run, or turns the failure into an answer.

    - ``sys.exit`` and its spellings: a second boundary.
    - a call to a module-level helper that exits: the same, one hop away.
    - any ``return`` in ``main``: ``return 1`` is a second boundary, and a bare
      ``return`` is a failure that ``cli.run`` will report as exit 0.
    - ``return`` / ``return None`` / ``return False`` in a helper ``main``
      reaches: a falsy "nothing here" handed back in place of the failure.
    """
    caught = ast.unparse(handler.type) if handler.type is not None else "<bare>"
    out = []
    for node in ast.walk(handler):
        if _is_direct_exit(node):
            out.append(Handler(script, function, caught, "sys.exit", node.lineno))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in helpers
        ):
            out.append(Handler(script, function, caught, node.func.id, node.lineno))
        elif isinstance(node, ast.Return) and function == "main":
            out.append(Handler(script, function, caught, "return", node.lineno))
        elif isinstance(node, ast.Return) and function in reachable and _is_falsy_return(node):
            out.append(Handler(script, function, caught, "return-falsy", node.lineno))
    return out


@pytest.mark.parametrize("script", SCRIPT_NAMES)
def test_no_handler_for_the_typed_family_calls_sys_exit(script: str):
    """(b): the boundary is cli.run. A handler that exits is a second one."""
    direct = [str(h) for h in _terminating_handlers(script) if h.via == "sys.exit"]
    assert not direct, (
        f"{script} ends the process from an except clause that catches the typed "
        f"family: {direct}. Delete the handler and let the error reach cli.run, which "
        f"reports it in the one shape. Catch-and-continue (record it, move on) is fine."
    )


class Debt(NamedTuple):
    """A helper-routed boundary that is known, frozen, and has a reason."""

    script: str
    function: str
    caught: str
    via: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.script, self.function, self.caught, self.via)


# (c)'s debt. Exact, as a multiset: a new entry fails, and so does a stale one,
# so paying one off cannot silently re-open the door. Read the current debt off
# this tuple, not off any prose about it.
#
# Empty, and meant to stay so. The last entry was emulator_selector._run's
# `except (SdkToolError, adb_exec.AdbError)` -> `_fail`, printing
# `{"error": ...}` at exit 1; it was deleted in Inc 2 and the error now reaches
# cli.run. A new entry is a second boundary being let back in.
KNOWN_HELPER_BOUNDARIES: tuple[Debt, ...] = ()


def test_helper_routed_boundaries_are_the_known_debt():
    """(c): ``_fail(error)`` is ``sys.exit`` one function away."""
    found = [h for script in SCRIPT_NAMES for h in _terminating_handlers(script)]
    routed = [h for h in found if h.via != "sys.exit"]

    wanted = Counter(debt.key for debt in KNOWN_HELPER_BOUNDARIES)
    actual = Counter(h.key for h in routed)

    new = [str(h) for h in routed if actual[h.key] > wanted[h.key]]
    stale = [debt for debt in KNOWN_HELPER_BOUNDARIES if wanted[debt.key] > actual[debt.key]]
    assert not new, (
        f"handlers that catch the typed family and end the process through a helper "
        f"or a return: {new}. That is a second boundary with a different shape. Let "
        f"the error reach cli.run instead."
    )
    assert not stale, (
        f"KNOWN_HELPER_BOUNDARIES lists {[d.key for d in stale]}, which no longer "
        f"exist. Delete the entries -- the debt is paid."
    )


# ===========================================================================
# (d) main honours the argv cli.run hands it
# ===========================================================================


# Every Android tool a script could reach for. Each fake records that it was
# run and fails, so a script that does work before parsing is caught twice: by
# the log, and by not getting the answer it wanted.
_FAKE_TOOLS = ("adb", "emulator", "avdmanager", "sdkmanager", "gradle", "java")

_FAKE_TOOL = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$CLI_BOUNDARY_TOOL_LOG"
exit 1
"""

# Run in a fresh interpreter: an in-process import may already be cached from
# another test, and would then prove nothing about what importing does.
_HELP_PROBE = """
import importlib, sys
sys.path.insert(0, sys.argv[1])
module = importlib.import_module(sys.argv[2])
sys.argv = [sys.argv[2] + ".py", "--not-a-flag-of-any-script"]
module.main(["--help"])
"""


@pytest.fixture(scope="module")
def no_toolchain(tmp_path_factory) -> dict[str, str]:
    """An environment where every Android tool is a fake that logs and fails.

    ``PATH`` holds only the fakes; ``ANDROID_HOME`` / ``ANDROID_SDK_ROOT`` point
    at an empty directory, because ``common.sdk_tools`` falls back to the SDK
    root when ``PATH`` has nothing; ``HOME`` is empty too, so nothing under the
    real home (``~/Library/Android/sdk``, ``~/.android``) is reachable.
    """
    root = tmp_path_factory.mktemp("no-toolchain")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for tool in _FAKE_TOOLS:
        fake = bin_dir / tool
        fake.write_text(_FAKE_TOOL, encoding="utf-8")
        fake.chmod(0o755)
    for name in ("sdk", "home", "tmp"):
        (root / name).mkdir()
    return {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(root / "home"),
        "ANDROID_HOME": str(root / "sdk"),
        "ANDROID_SDK_ROOT": str(root / "sdk"),
        "ANDROID_SDK_HOME": str(root / "home"),
        "ANDROID_SERIAL": "emulator-5554",
        "TMPDIR": str(root / "tmp"),
    }


@pytest.mark.parametrize("script", SCRIPT_NAMES)
def test_main_parses_the_argv_it_is_handed(script: str, no_toolchain, tmp_path):
    """``cli.run`` calls ``main(argv)``; a main that reads ``sys.argv`` instead
    would answer a different command line from the one it was given.

    ``sys.argv`` in the probe is a flag no script defines, so a main that
    ignores its argument fails with a usage error rather than passing by
    coincidence. ``--help`` touches no tool; the log proves it.
    """
    tool_log = tmp_path / "tools.log"
    tool_log.touch()
    completed = subprocess.run(
        [sys.executable, "-c", _HELP_PROBE, str(SCRIPTS), script.removesuffix(".py")],
        env={**no_toolchain, "CLI_BOUNDARY_TOOL_LOG": str(tool_log)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert (
        completed.returncode == 0
    ), f"{script} main(['--help']) exited {completed.returncode}:\n{completed.stderr[-800:]}"
    assert "usage:" in completed.stdout
    invoked = tool_log.read_text(encoding="utf-8")
    assert invoked == "", f"{script} --help ran a tool:\n{invoked}"


# ===========================================================================
# (e) libraries do not own the process
# ===========================================================================


def test_only_the_boundary_module_ends_the_process():
    """``common/`` and ``gradle/`` raise; exactly one module turns that into an exit."""
    exiting = set()
    for path in sorted([*SCRIPTS.glob("common/*.py"), *SCRIPTS.glob("gradle/*.py")]):
        relative = path.relative_to(SCRIPTS).as_posix()
        if any(map(_is_direct_exit, ast.walk(_tree(relative)))):
            exiting.add(relative)
    assert exiting == {BOUNDARY_MODULE}, (
        f"library modules that end the process: {sorted(exiting)}. Only "
        f"{BOUNDARY_MODULE} may; everything under common/ and gradle/ raises."
    )


# ===========================================================================
# (f) every parser comes from cli.parser
# ===========================================================================
#
# A usage error is reported by the parser, before main() has anything to
# raise, so cli.run cannot shape it. cli.parser builds a parser that does --
# one object under --json, argparse's prose otherwise -- and this holds every
# script to it, positively: each script must construct its parser through the
# factory, and nothing under scripts/ but common/cli.py may construct, subclass
# or import argparse.ArgumentParser itself. navigator's hand-rolled
# `_JsonAwareParser` was the one script that got this right, in its own shape.

_FACTORY_CALLEES = frozenset({"cli.parser", "common.cli.parser"})


def _parser_constructions(tree: ast.Module) -> tuple[list[ast.Call], list[str]]:
    """(calls to the factory, every direct reference to argparse.ArgumentParser).

    Read from the AST, so a docstring or comment that names the class is not a
    use, and neither is an annotation (``-> argparse.ArgumentParser`` describes
    what cli.parser returns). Everything else is: any ``ArgumentParser`` name
    or attribute -- called, subclassed, aliased, handed on as
    ``parser_class=``, or reached through it (``.__call__``) -- a
    ``getattr(..., "ArgumentParser")``, and ``from argparse import
    ArgumentParser``. Constructing is not the only way to get a parser that
    answers in prose, so referring to the class at all is what is refused.
    """
    annotations: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
            every += [arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None]
            hints = [arg.annotation for arg in every] + [node.returns]
        elif isinstance(node, ast.AnnAssign):
            hints = [node.annotation]
        else:
            continue
        for hint in hints:
            if hint is not None:
                annotations.update(id(sub) for sub in ast.walk(hint))

    factory: list[ast.Call] = []
    direct: list[str] = []
    for node in ast.walk(tree):
        if id(node) in annotations:
            continue
        if isinstance(node, ast.Call):
            if ast.unparse(node.func) in _FACTORY_CALLEES:
                factory.append(node)
            elif (
                ast.unparse(node.func) == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "ArgumentParser"
            ):
                direct.append(f"line {node.lineno}: {ast.unparse(node)}")
        elif (isinstance(node, ast.Name) and node.id == "ArgumentParser") or (
            isinstance(node, ast.Attribute) and node.attr == "ArgumentParser"
        ):
            direct.append(f"line {node.lineno}: {ast.unparse(node)}")
        elif isinstance(node, ast.ImportFrom) and node.module == "argparse":
            for alias in node.names:
                if alias.name == "ArgumentParser":
                    direct.append(f"line {node.lineno}: from argparse import ArgumentParser")
    return factory, direct


def _run_json_flags(tree: ast.Module) -> tuple[str, ...]:
    """The json_flags the script hands cli.run (the default when it hands none)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in ("cli.run", "common.cli.run"):
            for keyword in node.keywords:
                if keyword.arg == "json_flags":
                    return tuple(ast.literal_eval(keyword.value))
    return cli.JSON_FLAGS


@pytest.mark.parametrize("script", SCRIPT_NAMES)
def test_every_script_builds_its_parser_through_cli_parser(script: str):
    tree = _tree(script)
    factory, direct = _parser_constructions(tree)
    assert not direct, (
        f"{script} builds an argparse parser itself: {direct}. Use cli.parser(...), "
        f"which answers a usage error in the refusal shape under --json."
    )
    assert factory, f"{script} never calls cli.parser(...): where does its parser come from?"

    # The parser and the boundary must agree on what "answer in JSON" is spelled,
    # or an alias (visual_diff's --details) gets JSON for a failure and prose
    # for a usage error.
    expected = _run_json_flags(tree)
    for call in factory:
        given = cli.JSON_FLAGS
        for keyword in call.keywords:
            if keyword.arg == "json_flags":
                given = tuple(ast.literal_eval(keyword.value))
        assert given == expected, (
            f"{script} line {call.lineno}: cli.parser(json_flags={given}) but "
            f"cli.run(json_flags={expected})"
        )


def _parser_offenders(root: Path) -> dict[str, list[str]]:
    """Every module under ``root``, at any depth, that uses ArgumentParser but the factory's."""
    offenders = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative == BOUNDARY_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _, direct = _parser_constructions(tree)
        if direct:
            offenders[relative] = direct
    return offenders


def test_no_module_builds_a_parser_around_the_factory():
    """Nothing anywhere under scripts/ builds a parser, except the factory itself."""
    offenders = _parser_offenders(SCRIPTS)
    assert not offenders, f"argparse parsers built outside cli.parser: {offenders}"


def test_the_module_scan_reaches_every_file_but_the_factory(tmp_path):
    """Recursive, and the one exemption is common/cli.py by path, not by directory."""
    direct = "import argparse\nparser = argparse.ArgumentParser()\n"
    for relative in ("common/cli.py", "common/helpers.py", "gradle/deep/nested.py", "tool.py"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(direct, encoding="utf-8")
    assert sorted(_parser_offenders(tmp_path)) == [
        "common/helpers.py",
        "gradle/deep/nested.py",
        "tool.py",
    ]


@pytest.mark.parametrize(
    ("source", "factory_calls", "direct_uses"),
    [
        ('"""argparse.ArgumentParser( in a docstring."""\nparser = cli.parser()', 1, 0),
        ("# argparse.ArgumentParser( in a comment\nparser = cli.parser()", 1, 0),
        ("def f() -> argparse.ArgumentParser:\n    return cli.parser()", 1, 0),
        ("parser = argparse.ArgumentParser()", 0, 1),
        ("import argparse as ap\nparser = ap.ArgumentParser()", 0, 1),
        ("from argparse import ArgumentParser\nparser = ArgumentParser()", 0, 2),
        ("class P(argparse.ArgumentParser):\n    pass", 0, 1),
        ("parser = common.cli.parser(prog='x')", 1, 0),
        ("x: argparse.ArgumentParser = cli.parser()", 1, 0),
        ("def f(p: argparse.ArgumentParser) -> None:\n    pass", 0, 0),
        ('parser = getattr(argparse, "ArgumentParser")()', 0, 1),
        ("parser = argparse.ArgumentParser.__call__(argparse)", 0, 1),
        ("make = argparse.ArgumentParser\nparser = make()", 0, 1),
        ("cli.parser().add_subparsers(parser_class=argparse.ArgumentParser)", 1, 1),
    ],
)
def test_the_parser_guard_reads_code_not_text(source, factory_calls, direct_uses):
    """A guard that grepped would flag its own docstring, and miss an alias."""
    factory, direct = _parser_constructions(ast.parse(source))
    assert (len(factory), len(direct)) == (factory_calls, direct_uses), direct
