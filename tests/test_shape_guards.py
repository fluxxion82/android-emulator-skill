"""Three shapes that were fixed in one file and left alive in another.

The dominant defect class in this repo is no longer "a wrong line". It is a
*shape*: a correct fix applied at the site somebody happened to be reading,
while every other site keeping the same shape stays wrong. The 2026-09-02
review counted eleven instances of it. Three are pinned here, each as an AST
invariant that enumerates every site rather than checking the one that was
noticed:

**Quoting (C3 / X2).** ``adb shell a b c`` joins its arguments into one string
and the *device's* ``sh -c`` re-parses the result -- ``common/device_utils.py``
says so at :84-91, right above ``quote_for_device_shell``. Five files
interpolate a package name, a URL, a locale or a permission into a device-shell
argv without it, so ``--open-url 'https://x/?a=1&b=2'`` opens ``?a=1`` and
backgrounds ``am start``.

**Emulator console (L7).** ``common/emu_console.run_emu`` exists because the
console answers ``KO`` at exit status 0, frames its replies with ``OK``, and is
absent on a physical device. Five scripts still spell ``adb emu`` themselves and
each carries its own partial handling of those three facts.

**Bounds grammar (C5 / C7).** uiautomator writes ``bounds="[l,t][r,b]"`` and
those numbers go negative for a partially off-screen view. The repo parses that
string with three different regexes in three files; two of them reject a
negative coordinate and substitute ``(0,0,0,0)``, which navigator then offers as
a tappable rectangle.

Each invariant is red today, so it carries ``xfail(strict=True)`` naming the
finding: the guard states the defect without blocking the PR, and the fix must
delete the marker. What is *not* xfail is the evidence that each guard works --
every one has a self-test that injects a synthetic violation and a self-test
that enumerates the real sites by file, function and line. A guard nobody has
seen fail is a guard nobody has seen.

**These guards parse; they do not grep.** That rule is written down because the
repo has been bitten by its opposite five times: a substring guard flags the
docstring that explains it, so the repair for the failure is to delete the
honest sentence. This module reads Python's own AST, and the one place a string
literal is examined at all -- the bounds grammar -- skips docstrings explicitly.
Every paragraph above may safely name ``adb emu``, ``quote_for_device_shell``
and ``[l,t][r,b]``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"

# How far a recorded line number may drift before the enumeration self-tests
# stop believing they are looking at the same call. The sites are keyed on
# file + function, which survives an edit above them; the line is carried as
# corroboration, not as identity.
LINE_DRIFT = 10


class Site(NamedTuple):
    """One place a shape occurs, keyed on something more stable than a line."""

    file: str
    function: str
    line: int
    detail: str

    @property
    def key(self) -> str:
        return f"{self.file}::{self.function}"

    def __str__(self) -> str:
        return f"{self.file}:{self.line} ({self.function}) {self.detail}"


def _script_files() -> list[Path]:
    return sorted(SCRIPTS.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(SCRIPTS).as_posix()


def _called_name(node: ast.Call) -> str | None:
    """``foo(...)`` and ``x.foo(...)`` both answer ``foo``."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", None)


def _functions_by_node(tree: ast.AST) -> dict[int, str]:
    """Map every node to the innermost function enclosing it."""
    enclosing: dict[int, str] = {}
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(func):
                enclosing.setdefault(id(node), func.name)
    return enclosing


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Ids of the string constants that are docstrings.

    Needed only by the bounds guard, which is the one guard that looks at
    string *contents*. Skipping them is what stops this file's own explanation
    of the bounds grammar from being reported as a violation of it.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


# ===========================================================================
# (a) Quoting — C3 / X2
# ===========================================================================

# The functions that build an `adb ... shell` argv. All three take
# (operation, serial, *args), so the device-side argv starts at index 2.
SHELL_SINKS = frozenset({"run_adb", "build_adb_command", "build_command"})

QUOTER = "quote_for_device_shell"

# Sites where a non-literal reaches the device shell unquoted and that is not a
# defect, keyed by file::function so a moved line does not silently re-open one.
# Every entry names the reason the value cannot carry a shell metacharacter.
# CLAUDE.md's standing rule applies: this is debt-with-a-reason, not permission.
QUOTING_EXEMPT: dict[str, str] = {
    "gesture.py::swipe_path": "str() of coordinates argparse already parsed as int",
    "gesture.py::long_press": "str() of coordinates argparse already parsed as int",
    "navigator.py::tap_at": "str() of coordinates argparse already parsed as int",
    "status_bar.py::set_battery": "str() of a battery level range-checked to 0-100",
    "status_bar.py::set_wifi": "str() of a signal level argparse parsed as int",
    "appearance.py::build_font_scale_command": "str() of a float argparse parsed",
    "keyboard.py::press_key": "keycode read out of the KEY_CODES table after a membership test",
    "common/screenshot_utils.py::capture_screenshot": (
        "device path is an f-string of uuid4().hex under /sdcard; no caller value reaches it"
    ),
    "sms.py::list_inbox": "URI and projection are module-level string constants",
    "appearance.py::_run_built": (
        "forwards the argv tail built by the build_*_command helpers, which are "
        "themselves checked above; quoting belongs there, not here"
    ),
}

# What the guard reports today, confirmed by reading each call. The fixing PR
# empties this list. Codex's evidence for the finding cited, per file:
#   C3  app_launcher.py:103,147,251 · app_state_capture.py:231-260,351
#   X2  privacy_manager.py:123,159,199,268 · status_bar.py:284,341,395
#       · appearance.py:128-162
# Two of those citations name the line of the *argument* rather than of the
# call that carries it (status_bar :395 is `mobile_type` inside the call opened
# at :384; app_launcher :103 is the extras list built for the call at :108), so
# the lines below are the call sites this guard reports.
KNOWN_UNQUOTED: tuple[Site, ...] = (
    Site("app_launcher.py", "launch", 108, "component, *extra_args"),
    Site("app_launcher.py", "terminate", 147, "package_name"),
    Site("app_launcher.py", "open_url", 251, "url"),
    Site("app_launcher.py", "get_state", 313, "package_name"),
    Site("app_launcher.py", "_get_launcher_activity", 360, "package_name"),
    Site("app_state_capture.py", "_get_app_info", 231, "self.package"),
    Site("app_state_capture.py", "_get_app_info", 248, "self.package"),
    Site("app_state_capture.py", "_capture_logs", 351, "self.package"),
    Site("appearance.py", "build_locale_command", 141, "locale"),
    Site("privacy_manager.py", "grant_permission", 123, "package, full_permission"),
    Site("privacy_manager.py", "revoke_permission", 159, "package, full_permission"),
    Site("privacy_manager.py", "confirm_permission", 199, "package"),
    Site("privacy_manager.py", "list_app_permissions", 268, "package"),
    Site("status_bar.py", "set_mobile_data", 247, "datatype"),
    Site("status_bar.py", "set_time", 284, "time_str"),
    Site("status_bar.py", "override", 342, "time"),
    Site("status_bar.py", "override", 346, "battery"),
    Site("status_bar.py", "override", 367, "wifi_level"),
    Site("status_bar.py", "override", 384, "mobile_level, mobile_type"),
)


class _QuotingScope:
    """What a name means inside one function of one module.

    Three resolutions, and each earns its place by removing a hand-written
    exemption that would otherwise have to be trusted:

    * a *literal* is a string constant, a conditional between two literals
      (``"grant" if granted else "revoke"``), a module-level constant, or a
      local assigned once from any of those;
    * a *quoter* is ``quote_for_device_shell`` or a same-module function whose
      every return is a call to it (``keyboard._escape_text``);
    * a value is *quoted* if it is a quoter call, or a local assigned once from
      one, or from a comprehension mapping one over a sequence
      (``[quote_for_device_shell(a) for a in argv]``).
    """

    def __init__(self, module_literals: set[str], quoters: set[str], func: ast.AST | None) -> None:
        self._module_literals = module_literals
        self._quoters = quoters
        self._local_literals: set[str] = set()
        self._local_quoted: set[str] = set()
        if func is None:
            return

        counts: dict[str, int] = {}
        literals: set[str] = set()
        quoted: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                counts[target.id] = counts.get(target.id, 0) + 1
                if self._is_literal(node.value, locals_too=False):
                    literals.add(target.id)
                if self._is_quoter_call(node.value):
                    quoted.add(target.id)
        # A name rebound more than once is not resolvable by inspection.
        self._local_literals = {name for name in literals if counts.get(name) == 1}
        self._local_quoted = {name for name in quoted if counts.get(name) == 1}

    def _is_quoter_call(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            return _called_name(node) in self._quoters
        if isinstance(node, ast.ListComp | ast.GeneratorExp):
            return isinstance(node.elt, ast.Call) and _called_name(node.elt) in self._quoters
        return False

    def _is_literal(self, node: ast.AST, locals_too: bool = True) -> bool:
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.IfExp):
            return self._is_literal(node.body, locals_too) and self._is_literal(
                node.orelse, locals_too
            )
        if isinstance(node, ast.Name):
            if node.id in self._module_literals:
                return True
            return locals_too and node.id in self._local_literals
        return False

    def is_safe(self, node: ast.AST) -> bool:
        """Whether this argument may cross the device shell as it stands."""
        if self._is_literal(node) or self._is_quoter_call(node):
            return True
        return isinstance(node, ast.Name) and node.id in self._local_quoted


def _module_literal_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _quoter_names(tree: ast.AST) -> set[str]:
    """``quote_for_device_shell`` plus any local alias that only returns it."""
    names = {QUOTER}
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        returns = [n for n in ast.walk(func) if isinstance(n, ast.Return) and n.value is not None]
        if returns and all(
            isinstance(r.value, ast.Call) and _called_name(r.value) == QUOTER for r in returns
        ):
            names.add(func.name)
    return names


def scan_quoting(filename: str, source: str) -> list[Site]:
    """Device-shell arguments that are neither literal nor quoted.

    A ``*args`` splat is a forwarder, not an interpolation: the values came
    from the caller. So a function that splats its own ``*args`` straight into
    a sink (``status_bar._demo_broadcast``, ``push_notification._shell``) is
    resolved one level out and its *call sites* are checked instead. Without
    that, three quarters of the status-bar defect is invisible.
    """
    tree = ast.parse(source, filename=filename)
    module_literals = _module_literal_names(tree)
    quoters = _quoter_names(tree)
    enclosing = _functions_by_node(tree)
    functions = {
        id(node): node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    owner: dict[int, ast.AST] = {}
    for func in functions.values():
        for node in ast.walk(func):
            owner.setdefault(id(node), func)

    forwarders: set[str] = set()
    sites: list[Site] = []

    def unsafe(args: list[ast.expr], func: ast.AST | None) -> list[str]:
        scope = _QuotingScope(module_literals, quoters, func)
        bad: list[str] = []
        for arg in args:
            if isinstance(arg, ast.Starred):
                value = arg.value
                vararg = getattr(getattr(func, "args", None), "vararg", None)
                if isinstance(value, ast.Name) and vararg is not None and value.id == vararg.arg:
                    forwarders.add(func.name)
                    continue
                if scope.is_safe(value):
                    continue
                bad.append(ast.unparse(arg))
                continue
            if scope.is_safe(arg):
                continue
            bad.append(ast.unparse(arg))
        return bad

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    # Direct sinks: run_adb("shell", serial, ...) and friends.
    for node in calls:
        if _called_name(node) not in SHELL_SINKS or not node.args:
            continue
        operation = node.args[0]
        if not (isinstance(operation, ast.Constant) and operation.value == "shell"):
            continue
        func = owner.get(id(node))
        bad = unsafe(node.args[2:], func)
        if bad:
            sites.append(
                Site(filename, enclosing.get(id(node), "<module>"), node.lineno, ", ".join(bad))
            )

    # Call sites of the forwarders discovered above.
    for node in calls:
        name = _called_name(node)
        if name not in forwarders:
            continue
        func = owner.get(id(node))
        if func is not None and func.name in forwarders:
            continue  # the definition forwarding to itself
        bad = unsafe(node.args, func)
        if bad:
            sites.append(
                Site(filename, enclosing.get(id(node), "<module>"), node.lineno, ", ".join(bad))
            )

    return sorted(sites, key=lambda site: site.line)


def unquoted_device_shell_arguments(*, apply_exemptions: bool = True) -> list[Site]:
    found: list[Site] = []
    for path in _script_files():
        for site in scan_quoting(_relative(path), path.read_text(encoding="utf-8")):
            if apply_exemptions and site.key in QUOTING_EXEMPT:
                continue
            found.append(site)
    return found


@pytest.mark.xfail(
    strict=True,
    reason=(
        "C3/X2: package names, URLs, locales and permissions still reach the "
        "device shell unquoted in five files. Inc 1 wraps them."
    ),
)
def test_every_device_shell_argument_is_quoted():
    """A value crossing into ``sh -c`` on the device must be quoted for it.

    Not "must be safe" -- must be *quoted*. The judgement "this particular
    value cannot contain a semicolon" is the reasoning that produced the
    defect: ``--open-url`` was written by somebody who knew a URL is a URL, and
    a query string truncates the command at its first ``&``.
    """
    offenders = unquoted_device_shell_arguments()
    assert not offenders, (
        "these arguments reach the device shell without quote_for_device_shell:\n  "
        + "\n  ".join(str(site) for site in offenders)
        + "\ncommon/device_utils.py:84-91 explains why adb's separate host argv "
        "elements do not survive as separate device argv elements. Wrap each "
        "value, or -- if it provably cannot carry a metacharacter -- add the "
        "file::function to QUOTING_EXEMPT with the reason."
    )


_SYNTHETIC_UNQUOTED = """
from common.adb_exec import run_adb


def wipe(serial, package):
    return run_adb("shell", serial, "pm", "clear", package)
"""

_SYNTHETIC_QUOTED = """
from common.adb_exec import run_adb
from common.device_utils import quote_for_device_shell


def wipe(serial, package):
    return run_adb("shell", serial, "pm", "clear", quote_for_device_shell(package))
"""

_SYNTHETIC_FORWARDER = """
from common.adb_exec import run_adb


def _shell(self, *args):
    return run_adb("shell", self.serial, *args)


def clear(self, package):
    return self._shell("pm", "clear", package)
"""


def test_the_quoting_guard_flags_a_synthetic_violation():
    """Anti-vacuity: prove the detector fires before trusting that it is quiet."""
    found = scan_quoting("synthetic.py", _SYNTHETIC_UNQUOTED)
    assert [site.function for site in found] == ["wipe"], found
    assert found[0].detail == "package"


def test_the_quoting_guard_accepts_a_quoted_call():
    """The negative control: a guard that flags everything proves nothing."""
    assert scan_quoting("synthetic.py", _SYNTHETIC_QUOTED) == []


def test_the_quoting_guard_follows_a_vararg_forwarder():
    """The status-bar shape: the sink is one call away from the interpolation."""
    found = scan_quoting("synthetic.py", _SYNTHETIC_FORWARDER)
    assert [site.function for site in found] == ["clear"], found


def test_the_quoting_guard_enumerates_todays_sites():
    """The evidence that the guard is reading the real code, not agreeing with itself.

    Keyed on file + function with the line as corroboration, because an edit
    above a call moves its line without moving the defect. The fixing PR
    empties ``KNOWN_UNQUOTED``; until then this is what "C3/X2" means in
    file-and-line terms.
    """
    found = unquoted_device_shell_arguments()
    by_key: dict[str, list[Site]] = {}
    for site in found:
        by_key.setdefault(site.key, []).append(site)

    missing = [site for site in KNOWN_UNQUOTED if site.key not in by_key]
    assert not missing, (
        f"the guard no longer sees {[str(s) for s in missing]}. Either the fix "
        f"landed -- in which case delete those entries and the xfail marker -- "
        f"or the detector stopped working."
    )

    for site in KNOWN_UNQUOTED:
        lines = [candidate.line for candidate in by_key[site.key]]
        assert any(abs(line - site.line) <= LINE_DRIFT for line in lines), (
            f"{site.key} is still reported, but at {lines} rather than near "
            f"{site.line}. Confirm it is the same call and update KNOWN_UNQUOTED."
        )

    unexpected = sorted({site.key for site in found} - {site.key for site in KNOWN_UNQUOTED})
    assert not unexpected, (
        f"the guard reports sites nobody has looked at yet: {unexpected}. Read "
        f"each one, then either add it to KNOWN_UNQUOTED or exempt it with a reason."
    )


def test_the_quoting_exemptions_do_not_rot():
    """An exemption that no longer exempts anything is a door left open.

    Same failure as a stale ``KNOWN_VIOLATIONS`` entry in
    ``test_fixture_policy``: the code was fixed or deleted, the exemption
    outlived it, and it now silently blesses whatever takes that name next.
    """
    all_keys = {site.key for site in unquoted_device_shell_arguments(apply_exemptions=False)}
    stale = sorted(set(QUOTING_EXEMPT) - all_keys)
    assert not stale, (
        f"{stale} are exempted from the quoting guard but no longer reach the "
        f"device shell with a non-literal at all. Delete them."
    )


# ===========================================================================
# (b) Emulator console — L7
# ===========================================================================

# The one module allowed to speak the console protocol.
EMU_CONSOLE = "common/emu_console.py"

# Confirmed by reading each call. Codex cites emulator_shutdown.py:66 as the
# representative and names shutdown, boot, selector, erase and location.
KNOWN_EMU_BYPASSES: tuple[Site, ...] = (
    Site("emulator_boot.py", "_get_avd_name_for_serial", 211, 'run_adb("emu", ...)'),
    Site("emulator_erase.py", "is_avd_running", 108, 'run_adb("emu", ...)'),
    Site("emulator_selector.py", "_avd_name_for_serial", 333, 'run_adb("emu", ...)'),
    Site("emulator_shutdown.py", "shutdown", 66, 'build_adb_command("emu", ...)'),
    Site("emulator_shutdown.py", "get_avd_name_for_serial", 151, 'build_adb_command("emu", ...)'),
    # The plan cites location.py:273; that is `_run_geo_fix`, which executes an
    # argv it does not build. The literal "emu" is in build_geo_fix_command.
    Site("location.py", "build_geo_fix_command", 144, 'build_adb_command("emu", ...)'),
)

# Scripts that reach the console the sanctioned way. Named here so the guard
# is checked for false positives as well as false negatives: run_emu() must
# never be reported, or the fix would look like the defect.
EMU_CONSOLE_CLIENTS = ("sms.py", "snapshot.py")


def scan_emu_console(filename: str, source: str) -> list[Site]:
    """Every place an ``adb emu`` argv is constructed.

    Two shapes, because the second is how the first gets around a guard that
    only knows the first: the operation passed to ``run_adb`` /
    ``build_adb_command``, and a literal argv list handed to ``subprocess``.
    """
    tree = ast.parse(source, filename=filename)
    enclosing = _functions_by_node(tree)
    sites: list[Site] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = _called_name(node)
        first = node.args[0]
        detail = None

        if name in SHELL_SINKS and isinstance(first, ast.Constant) and first.value == "emu":
            detail = f'{name}("emu", ...)'
        elif name in ("run", "Popen") and isinstance(first, ast.List | ast.Tuple):
            values = [e.value for e in first.elts if isinstance(e, ast.Constant)]
            if "emu" in values and "adb" in values:
                detail = "subprocess argv containing adb ... emu"

        if detail:
            sites.append(Site(filename, enclosing.get(id(node), "<module>"), node.lineno, detail))

    return sorted(sites, key=lambda site: site.line)


def emu_console_bypasses() -> list[Site]:
    found: list[Site] = []
    for path in _script_files():
        relative = _relative(path)
        if relative == EMU_CONSOLE:
            continue
        found.extend(scan_emu_console(relative, path.read_text(encoding="utf-8")))
    return found


@pytest.mark.xfail(
    strict=True,
    reason=(
        "L7: five scripts still build their own `adb emu` argv instead of "
        "going through common/emu_console.run_emu. Inc 2 routes them."
    ),
)
def test_only_emu_console_speaks_to_the_emulator_console():
    """``adb emu`` is not an adb command; it is a protocol with three traps.

    The console answers ``KO`` and *exits 0*, so a caller reading the exit
    status reports a rejection as a success. It frames every reply with a
    trailing ``OK``, so ``adb emu avd name`` yields ``"Pixel_9\\r\\nOK\\r\\n"``
    and a name comparison fails against the AVD that is running (defect S5).
    And a physical device has no console at all. ``run_emu`` handles all three,
    measured rather than guessed; a hand-rolled call site handles whichever the
    author remembered.
    """
    offenders = emu_console_bypasses()
    assert not offenders, (
        "these build an `adb emu` argv outside common/emu_console.py:\n  "
        + "\n  ".join(str(site) for site in offenders)
        + "\nCall common.emu_console.run_emu instead; it strips the OK framing, "
        "raises on KO, and reports a physical device as having no console."
    )


_SYNTHETIC_EMU = """
from common.adb_exec import run_adb


def avd_name(serial):
    return run_adb("emu", serial, "avd", "name").stdout
"""

_SYNTHETIC_EMU_SUBPROCESS = """
import subprocess


def kill(serial):
    return subprocess.run(["adb", "-s", serial, "emu", "kill"], check=False)
"""

_SYNTHETIC_RUN_EMU = """
from common.emu_console import run_emu


def avd_name(serial):
    return run_emu("avd", "name", serial=serial).payload
"""


def test_the_emu_guard_flags_a_synthetic_violation():
    """Anti-vacuity, both shapes."""
    direct = scan_emu_console("synthetic.py", _SYNTHETIC_EMU)
    assert [site.function for site in direct] == ["avd_name"], direct

    raw = scan_emu_console("synthetic.py", _SYNTHETIC_EMU_SUBPROCESS)
    assert [site.function for site in raw] == ["kill"], raw


def test_the_emu_guard_does_not_flag_run_emu():
    """The negative control -- and the shape the fix will take."""
    assert scan_emu_console("synthetic.py", _SYNTHETIC_RUN_EMU) == []


def test_the_emu_guard_enumerates_todays_sites():
    """The five bypassing scripts, by file, function and line."""
    found = emu_console_bypasses()
    by_key = {site.key: site for site in found}

    missing = [site for site in KNOWN_EMU_BYPASSES if site.key not in by_key]
    assert not missing, (
        f"the guard no longer sees {[str(s) for s in missing]}. If they were "
        f"routed through run_emu, delete the entries and the xfail marker."
    )
    for site in KNOWN_EMU_BYPASSES:
        assert abs(by_key[site.key].line - site.line) <= LINE_DRIFT, (
            f"{site.key} moved from {site.line} to {by_key[site.key].line}; "
            f"confirm it is the same call and update KNOWN_EMU_BYPASSES."
        )

    unexpected = sorted(set(by_key) - {site.key for site in KNOWN_EMU_BYPASSES})
    assert not unexpected, f"unreviewed `adb emu` sites: {unexpected}"


@pytest.mark.parametrize("script", EMU_CONSOLE_CLIENTS)
def test_scripts_using_run_emu_are_not_reported(script: str):
    """False-positive check: the correct call must not look like the defect."""
    source = (SCRIPTS / script).read_text(encoding="utf-8")
    assert "run_emu" in source, f"{script} no longer uses run_emu; pick another witness"
    assert scan_emu_console(script, source) == []


# ===========================================================================
# (c) Bounds grammar — C5 / C7
# ===========================================================================

# The module that should own the single parser. Inc 1 puts parse_bounds() here;
# this guard asserts its ABSENCE everywhere else, which is checkable today.
HIERARCHY = "common/hierarchy.py"

# A regex literal that takes apart uiautomator's bounds string: an escaped
# opening bracket, then a digit class (optionally signed) before the comma.
# Matched against the literal's *text*, so `\[(\d+),` and `\[(-?\d+),` both
# answer, and `flags=\[\s*(?P<flags>[^\]]*?)\]` -- the one other bracket regex
# in the skill -- does not.
_BOUNDS_GRAMMAR = re.compile(r"\\\[[^\]]*-?\\d")

_BOUNDS_FUNCTION = re.compile(r"bounds$")

KNOWN_BOUNDS_SITES: tuple[Site, ...] = (
    Site("accessibility_audit.py", "_parse_bounds", 70, "function"),
    Site("accessibility_audit.py", "_parse_bounds", 72, "regex (signed)"),
    Site("navigator.py", "_parse_bounds", 290, "function"),
    Site("navigator.py", "_parse_bounds", 301, "regex (unsigned)"),
    Site("screen_mapper.py", "_bounds", 268, "function"),
    Site("screen_mapper.py", "_bounds", 270, "regex (unsigned)"),
)


def scan_bounds(filename: str, source: str) -> list[Site]:
    """Bounds grammars and bounds parsers, wherever they are spelled.

    Docstrings are excluded before any string is examined. That is not a
    nicety: the docstring of the shared parser will describe the very grammar
    this looks for, and a guard whose repair is "delete the explanation" is
    worse than no guard.
    """
    tree = ast.parse(source, filename=filename)
    enclosing = _functions_by_node(tree)
    docstrings = _docstring_ids(tree)
    sites: list[Site] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _BOUNDS_FUNCTION.search(
            node.name.strip("_").lower()
        ):
            sites.append(Site(filename, node.name, node.lineno, "function"))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and "," in node.value
            and _BOUNDS_GRAMMAR.search(node.value)
        ):
            sites.append(
                Site(
                    filename,
                    enclosing.get(id(node), "<module>"),
                    node.lineno,
                    f"regex {node.value!r}",
                )
            )

    return sorted(sites, key=lambda site: site.line)


def bounds_grammars_outside_hierarchy() -> list[Site]:
    found: list[Site] = []
    for path in _script_files():
        relative = _relative(path)
        if relative == HIERARCHY:
            continue
        found.extend(scan_bounds(relative, path.read_text(encoding="utf-8")))
    return found


@pytest.mark.xfail(
    strict=True,
    reason=(
        "C5/C7: three bounds grammars in three files, two of them unsigned. "
        "Inc 1 replaces them with one parse_bounds() in common/hierarchy.py."
    ),
)
def test_bounds_are_parsed_in_one_place():
    """One grammar, or the three disagree about a view that is half off-screen.

    ``navigator._parse_bounds`` and ``screen_mapper._bounds`` both match
    ``\\d+``, so a bounds string with a negative coordinate -- what uiautomator
    writes for a partially scrolled-off view -- fails to match. Navigator then
    returns ``(0, 0, 0, 0)`` and offers the element as tappable at the top-left
    corner of the screen; screen_mapper reads None and its area check says
    "do not exclude on a missing signal", so the same element counts as
    interactive. ``accessibility_audit._parse_bounds`` already has the signed
    grammar, which is how the disagreement is provable rather than theoretical.
    """
    offenders = bounds_grammars_outside_hierarchy()
    assert not offenders, (
        "bounds is parsed outside common/hierarchy.py:\n  "
        + "\n  ".join(str(site) for site in offenders)
        + "\nCall the shared parse_bounds() instead. Three grammars means three "
        "answers for the same element."
    )


_SYNTHETIC_BOUNDS = r'''
import re


def _parse_bounds(text):
    """Parse the bounds attribute."""
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", text)
    return match.groups() if match else None
'''

_SYNTHETIC_BOUNDS_DOCSTRING_ONLY = r'''
r"""Bounds look like ``[l,t][r,b]`` and are parsed by ``\[(-?\d+),(-?\d+)\]``.

This module only talks about them; it does not parse them.
"""


def describe():
    r"""Explain the ``\[(\d+),(\d+)\]`` grammar without using it."""
    return "see common/hierarchy.py"
'''

_SYNTHETIC_OTHER_BRACKET_REGEX = r"""
import re

PERMISSION = re.compile(r"^(?P<name>\S+?):\s*granted=(?:true|false)(?:,\s*flags=\[\s*(?P<f>[^\]]*?)\s*\])?$")
"""


def test_the_bounds_guard_flags_a_synthetic_violation():
    """Anti-vacuity: both the regex and the function name are reported."""
    found = scan_bounds("synthetic.py", _SYNTHETIC_BOUNDS)
    details = [site.detail.split()[0] for site in found]
    assert details == ["function", "regex"], found


def test_the_bounds_guard_ignores_docstrings():
    """The rule this repo has broken five times, pinned as a test.

    A module that only *describes* the grammar -- including the shared parser's
    own docstring, and the ones in this file -- must not be reported. A guard
    whose repair is deleting the sentence that explains it is a guard that
    makes the codebase worse.
    """
    assert scan_bounds("synthetic.py", _SYNTHETIC_BOUNDS_DOCSTRING_ONLY) == []


def test_the_bounds_guard_ignores_unrelated_bracket_regexes():
    """The negative control: `flags=\\[...\\]` in device_utils is not bounds."""
    assert scan_bounds("synthetic.py", _SYNTHETIC_OTHER_BRACKET_REGEX) == []


def test_the_bounds_guard_enumerates_todays_sites():
    """Three files, three grammars, listed."""
    found = bounds_grammars_outside_hierarchy()
    pairs = [(site.file, site.function, site.line) for site in found]

    for site in KNOWN_BOUNDS_SITES:
        near = [
            line
            for file, function, line in pairs
            if file == site.file
            and function == site.function
            and abs(line - site.line) <= LINE_DRIFT
        ]
        assert near, f"{site} is no longer reported; found {pairs}"

    unexpected = sorted({site.key for site in found} - {site.key for site in KNOWN_BOUNDS_SITES})
    assert not unexpected, f"unreviewed bounds parsers: {unexpected}"


def test_the_shared_parser_home_exists():
    """The guard asserts absence elsewhere; that only means something with a there.

    Inc 1 adds ``parse_bounds()`` to ``common/hierarchy.py``. Today the module
    exists and the function does not, which is stated rather than asserted --
    the guard above is about the three copies, not about this file.
    """
    assert (SCRIPTS / HIERARCHY).exists(), "the intended home for the shared parser is gone"


# ===========================================================================
# Shared plumbing
# ===========================================================================


def test_every_script_basename_is_unique_enough_to_key_on():
    """The site keys are paths relative to scripts/, so `common/` never collides."""
    relatives = [_relative(path) for path in _script_files()]
    assert len(relatives) == len(set(relatives))
    assert EMU_CONSOLE in relatives
    assert HIERARCHY in relatives
