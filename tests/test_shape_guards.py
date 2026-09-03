"""Three shapes that were fixed in one file and left alive in another.

The dominant defect class in this repo is no longer "a wrong line". It is a
*shape*: a correct fix applied at the site somebody happened to be reading,
while every other site keeping the same shape stays wrong. The 2026-09-02
review counted eleven instances of it. Three are pinned here, each as an AST
invariant that enumerates every site rather than checking the one that was
noticed:

**Quoting (C3 / X2) -- green since Inc 1.** ``adb shell a b c`` joins its
arguments into one string and the *device's* ``sh -c`` re-parses the result --
``common/device_utils.py`` says so at :84-91, right above
``quote_for_device_shell``. Five files interpolated a package name, a URL, a
locale or a permission into a device-shell argv without it, so ``--open-url
'https://x/?a=1&b=2'`` opened ``?a=1`` and backgrounded ``am start``. All
nineteen sites are wrapped; the invariant no longer carries an xfail, and it is
enumerated from both sides -- see ``KNOWN_UNQUOTED`` (now empty) and
``KNOWN_QUOTED``.

**Emulator console (L7).** ``common/emu_console.run_emu`` exists because the
console answers ``KO`` at exit status 0, frames its replies with ``OK``, and is
absent on a physical device. Five scripts still spell ``adb emu`` themselves and
each carries its own partial handling of those three facts.

**Bounds grammar (C5 / C7).** uiautomator writes ``bounds="[l,t][r,b]"`` and
those numbers go negative for a partially off-screen view. The repo parses that
string with three different regexes in three files; two of them reject a
negative coordinate and substitute ``(0,0,0,0)``, which navigator then offers as
a tappable rectangle.

The console and bounds invariants are still red, so each carries
``xfail(strict=True)`` naming its finding: the guard states the defect without
blocking the PR, and the fix must delete the marker. Quoting was red the same
way and its marker is gone. What is *not* xfail is the evidence that each guard
works -- every one has a self-test that injects a synthetic violation and a
self-test that enumerates the real sites as an exact multiset of file,
function, kind and what was found there. A guard nobody has seen fail is a
guard nobody has seen.

An emptied enumeration needs one more thing, because "no site reports a
violation" is also what a detector that has gone blind produces, and what
deleting the calls produces. So the fixed shape is enumerated *positively* as
well: the quoting scan reports the wrapped arguments on request, and
``KNOWN_QUOTED`` pins them. Removing one wrapper moves a site from one list to
the other and fails both.

Two failure modes of a guard like this are specifically defended against, both
found by review rather than by imagination:

* **Evasion by renaming.** Keying on the terminal spelling of a call means
  ``from common.adb_exec import run_adb as adb`` walks straight past. Import
  aliases are resolved per module, and a *census* test asserts the guards still
  see roughly as many device-shell and console calls as exist today -- so a
  refactor that empties the result set fails instead of reporting "all clear".
* **Evasion by counting.** Comparing sets keyed on file and function lets a
  second finding inside an already-listed function disappear silently. The
  enumerations are multisets, matched one-for-one.
* **False alarms from unrelated edits.** Line numbers are reported and never
  matched. A PR merging ahead of this one moved a console call fifteen lines
  and turned the enumeration red in CI while it was green on the branch --
  which says nothing about the code and trains people to loosen the guard.

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
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"


class Site(NamedTuple):
    """One place a shape occurs, as the guard found it.

    ``line`` is reported, never matched. It moved out of the identity after a
    PR that merged ahead of this one shifted ``emulator_shutdown``'s console
    call by fifteen lines: the guard reported the same call at a new line and
    the enumeration called it both missing and untriaged, which is a false
    alarm about the *test*, not a finding about the code. Line numbers are for
    the human reading the failure.
    """

    file: str
    function: str
    line: int
    kind: str
    detail: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.file, self.function, self.kind, self.detail)

    def __str__(self) -> str:
        suffix = f" {self.detail}" if self.detail else ""
        return f"{self.file}:{self.line} ({self.function}) [{self.kind}]{suffix}"


class Expectation(NamedTuple):
    """One site the guard is known to report, with no line number in it.

    Same four fields as :attr:`Site.key`. The line each one sits at today is
    kept as a trailing comment in the lists below -- useful when reading, and
    unable to break a build when an unrelated edit moves it.
    """

    file: str
    function: str
    kind: str
    detail: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.file, self.function, self.kind, self.detail)

    def __str__(self) -> str:
        suffix = f" {self.detail}" if self.detail else ""
        return f"{self.file} ({self.function}) [{self.kind}]{suffix}"


def _script_files() -> list[Path]:
    return sorted(SCRIPTS.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(SCRIPTS).as_posix()


def _called_name(node: ast.Call) -> str | None:
    """``foo(...)`` and ``x.foo(...)`` both answer ``foo``.

    Used for *method* dispatch (``self._demo_broadcast(...)``), where the
    spelling really is the identity. Module-level functions that build an adb
    argv are resolved through the import table instead -- see
    :func:`_sink_lookup`.
    """
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


# ---------------------------------------------------------------------------
# Which calls build an adb argv, whatever the local code calls them
# ---------------------------------------------------------------------------

# The functions that build an `adb <operation> ...` argv. All three take
# (operation, serial, *args), so the device-side argv starts at index 2.
SINK_FUNCTIONS = frozenset({"run_adb", "build_adb_command", "build_command"})

# The modules those live in, by tail name, so `from common import adb_exec`,
# `from .adb_exec import run_adb` and `import common.adb_exec as ax` all resolve.
SINK_MODULES = frozenset({"adb_exec", "device_utils"})


class SinkLookup(NamedTuple):
    """How one module spells the argv builders it imported."""

    direct: dict[str, str]  # local name -> canonical function
    modules: dict[str, str]  # local module alias -> module tail name

    def canonical(self, node: ast.Call) -> str | None:
        """The argv builder this call reaches, or None."""
        func = node.func
        if isinstance(func, ast.Name):
            return self.direct.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in SINK_FUNCTIONS:
            base = func.value
            if isinstance(base, ast.Name) and base.id in self.modules:
                return func.attr
            # An attribute spelled like a sink but reached through something
            # this module did not import -- kept rather than dropped, because
            # a missed site is a defect and a spurious one is a review comment.
            return func.attr
        return None


def _sink_lookup(tree: ast.Module) -> SinkLookup:
    """Resolve import aliases so a rename cannot walk past the guards.

    ``from common.adb_exec import run_adb as adb`` was confirmed by review to
    make the earlier version of this file report nothing at all. Four spellings
    are in use across the skill today -- a relative ``from .adb_exec import
    run_adb``, an absolute ``from common.adb_exec import ...``, a module import
    ``from common import adb_exec`` used as ``adb_exec.run_adb(...)``, and
    ``from common.device_utils import build_adb_command`` -- and all four, plus
    any ``as`` alias of them, resolve here.
    """
    direct: dict[str, str] = {}
    modules: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            tail = (node.module or "").rsplit(".", 1)[-1]
            for alias in node.names:
                if alias.name in SINK_FUNCTIONS and tail in SINK_MODULES:
                    direct[alias.asname or alias.name] = alias.name
                if alias.name in SINK_MODULES:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                tail = alias.name.rsplit(".", 1)[-1]
                if tail in SINK_MODULES:
                    modules[alias.asname or tail] = tail

    # A module that *defines* a sink calls it by bare name (adb_exec's own
    # build_command), with no import to resolve.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in SINK_FUNCTIONS:
            direct.setdefault(node.name, node.name)

    return SinkLookup(direct=direct, modules=modules)


def _sink_calls(tree: ast.Module, operation: str) -> list[ast.Call]:
    """Every call building ``adb <operation> ...``, alias-resolved."""
    lookup = _sink_lookup(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if lookup.canonical(node) is None:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == operation:
            found.append(node)
    return found


def _census(operation: str) -> int:
    total = 0
    for path in _script_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += len(_sink_calls(tree, operation))
    return total


def _run_emu_names(tree: ast.Module) -> set[str]:
    """Every local spelling of ``run_emu`` in one module, ``as`` aliases included.

    Keying on the terminal name alone is the evasion the sink resolver was
    reviewed for: ``from common.emu_console import run_emu as console`` walks
    straight past a census that only looks for the word.
    """
    names = {"run_emu"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("emu_console"):
            for alias in node.names:
                if alias.name == "run_emu":
                    names.add(alias.asname or alias.name)
    return names


CONSOLE_CALL = "run-emu-call"


def run_emu_call_sites() -> list[Site]:
    """Every ``run_emu(...)`` call in the skill, alias-resolved.

    The positive counterpart to :func:`emu_console_bypasses`, and the reason it
    exists: after L7 the negative guard's answer is an empty list, and an empty
    list is also what a detector that has stopped working returns. This one
    names the sites that must be there.

    A count alone was not enough -- reviewed and rejected. Nine found against a
    floor of six accepts three of them vanishing silently, which is exactly the
    "a capability was migrated and then quietly dropped" case the enumeration
    style exists for. So this is compared as an exact multiset, like the bypass
    enumeration.
    """
    sites: list[Site] = []
    for path in _script_files():
        relative = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        local = _run_emu_names(tree)
        enclosing = _functions_by_node(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in local:
                sites.append(
                    Site(relative, enclosing.get(id(node), "<module>"), node.lineno, CONSOLE_CALL)
                )
    return sorted(sites, key=lambda site: (site.file, site.line))


# Every console call in the skill, by file and function. The first six after
# emu_console's own are the L7 sites -- the ones this PR migrated, and the ones
# a later refactor could quietly drop while the bypass guard still reported
# "all clear". sms and snapshot were already correct and are the
# false-positive control: the fix must not look like the defect, and the
# sanctioned call must not go missing either.
KNOWN_CONSOLE_CALLS: tuple[Expectation, ...] = (
    Expectation("common/emu_console.py", "console_available", CONSOLE_CALL),  # :198
    Expectation("emulator_boot.py", "_get_avd_name_for_serial", CONSOLE_CALL),  # :226
    Expectation("emulator_erase.py", "is_avd_running", CONSOLE_CALL),  # :112
    Expectation("emulator_selector.py", "_avd_name_for_serial", CONSOLE_CALL),  # :341
    Expectation("emulator_shutdown.py", "shutdown", CONSOLE_CALL),  # :94
    Expectation("emulator_shutdown.py", "get_avd_name_for_serial", CONSOLE_CALL),  # :162
    Expectation("location.py", "_run_geo_fix", CONSOLE_CALL),  # :261
    Expectation("sms.py", "send", CONSOLE_CALL),  # :329
    Expectation("snapshot.py", "_console", CONSOLE_CALL),  # :402
)


# Today's counts are 47 device-shell calls and 1 `adb emu` argv construction
# (in common/emu_console.py, the only file allowed one). The shell floor sits
# below the count: the point is not to pin the number, it is that a refactor
# which renames the builder -- or an edit to this file that breaks resolution
# -- must not leave the guard reporting "all clear" over a corpus it can no
# longer see. The console side is pinned exactly instead, by
# KNOWN_CONSOLE_CALLS above.
SHELL_CALL_FLOOR = 40
EMU_CALL_FLOOR = 1


def test_the_guards_still_see_the_production_code():
    """Independent floor: a guard reading nothing passes every other test here.

    The enumeration tests below assert *which* sites are reported. They cannot
    tell "the defect was fixed" from "the detector went blind", because both
    look like an empty result. This can: it counts the calls the resolver
    finds, not the violations, so it stays meaningful after Inc 1 lands.
    """
    shell_calls = _census("shell")
    emu_calls = _census("emu")

    assert shell_calls >= SHELL_CALL_FLOOR, (
        f"the sink resolver finds only {shell_calls} `adb shell` argv "
        f"constructions across scripts/ (floor {SHELL_CALL_FLOOR}). Either the "
        f"skill was restructured, or _sink_lookup stopped resolving how it "
        f"imports run_adb / build_adb_command -- in which case the quoting "
        f"guard is now vacuous."
    )
    assert emu_calls >= EMU_CALL_FLOOR, (
        f"the sink resolver finds only {emu_calls} `adb emu` argv constructions "
        f"across scripts/ (floor {EMU_CALL_FLOOR}); the console guard cannot "
        f"see the code it is guarding. One is expected and required -- the call "
        f"inside common/emu_console.py that every other caller now goes "
        f"through. Zero means the resolver stopped resolving."
    )


def test_every_console_call_site_is_where_it_should_be():
    """The positive enumeration: the nine `run_emu` calls, by file and function.

    An empty bypass list means "nobody speaks the console protocol by hand". It
    does NOT mean the console is still being spoken to -- deleting all nine
    calls would satisfy it perfectly. This is the half that notices, and it is
    an exact multiset rather than a floor, because a floor of six under a count
    of nine accepts three sites disappearing without a word.
    """
    _assert_enumeration(KNOWN_CONSOLE_CALLS, run_emu_call_sites(), "KNOWN_CONSOLE_CALLS")


def test_the_console_call_enumeration_resolves_an_alias():
    """The blind spot this enumeration could have: ``import run_emu as X``.

    Asserted on a synthetic module rather than on production code, so it keeps
    testing the resolver after the production spelling changes.
    """
    aliased = ast.parse(
        "from common.emu_console import run_emu as console\n"
        "def kill(serial):\n"
        "    return console('kill', serial=serial)\n"
    )
    assert _run_emu_names(aliased) == {"run_emu", "console"}
    calls = [
        node
        for node in ast.walk(aliased)
        if isinstance(node, ast.Call) and _called_name(node) in _run_emu_names(aliased)
    ]
    assert len(calls) == 1


def test_every_file_that_imports_run_emu_appears_in_the_enumeration():
    """Cross-check: importing the console entry point and calling nothing.

    A site can leave KNOWN_CONSOLE_CALLS two ways: deleted from the code, or
    spelled so the scan stops seeing it. The enumeration catches the first.
    This catches the second -- a file that imports run_emu and contributes no
    enumerated call is either dead code or evading the resolver.
    """
    importers = {
        _relative(path)
        for path in _script_files()
        if any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("emu_console")
            and any(alias.name == "run_emu" for alias in node.names)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }

    enumerated = {item.file for item in KNOWN_CONSOLE_CALLS}
    assert importers <= enumerated, (
        f"{sorted(importers - enumerated)} import run_emu but contribute no "
        f"enumerated call site; either the call was removed and the import left "
        f"behind, or the scan cannot see how it is spelled"
    )


# ---------------------------------------------------------------------------
# Enumeration: an exact multiset, not a set of keys
# ---------------------------------------------------------------------------


def _reconcile(
    expected: tuple[Expectation, ...], actual: list[Site]
) -> tuple[list[Expectation], list[Site]]:
    """Compare the two as multisets of (file, function, kind, detail).

    Counting, not set membership. A *set* comparison keyed on file and function
    -- what this file did before review -- accepts the removal of a second
    finding inside an already-listed function: delete the bounds regex in
    ``screen_mapper._bounds`` and the function finding in the same place still
    satisfies both entries. Here the count for
    ``(screen_mapper.py, _bounds, regex, unsigned)`` drops from one to zero and
    the entry is reported missing, whatever line anything sits at.

    Duplicates are the reason this counts rather than sets: two of the quoting
    sites are the same interpolation in the same function
    (``app_state_capture._get_app_info`` reads ``self.package`` twice), and
    losing one of them must be visible.
    """
    wanted = Counter(item.key for item in expected)

    # Expected more copies of a key than the guard found: report the shortfall,
    # one entry per missing copy.
    deficit = Counter(wanted)
    deficit.subtract(site.key for site in actual)
    missing: list[Expectation] = []
    for item in expected:
        if deficit[item.key] > 0:
            missing.append(item)
            deficit[item.key] -= 1

    # Found more copies of a key than expected: the first N are the matched
    # ones, anything after that is untriaged.
    seen: Counter[tuple[str, str, str, str]] = Counter()
    unexpected: list[Site] = []
    for site in actual:
        seen[site.key] += 1
        if seen[site.key] > wanted[site.key]:
            unexpected.append(site)

    return missing, unexpected


def _assert_enumeration(
    expected: tuple[Expectation, ...], actual: list[Site], listname: str
) -> None:
    missing, unexpected = _reconcile(expected, actual)
    assert not missing, (
        f"the guard no longer reports {[str(item) for item in missing]}.\n"
        f"Everything it did report, with the line it is on now: "
        f"{[str(site) for site in actual]}.\n"
        f"If the fix landed, delete those entries from {listname} and the xfail "
        f"marker in the same commit. If it did not, the detector stopped seeing them."
    )
    assert not unexpected, (
        f"the guard reports sites nobody has triaged: "
        f"{[str(site) for site in unexpected]}. Read each one, then add it to "
        f"{listname} or exempt it with a reason."
    )


# ===========================================================================
# (a) Quoting — C3 / X2
# ===========================================================================

QUOTER = "quote_for_device_shell"

UNQUOTED = "unquoted"

QUOTED = "quoted"

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

# What the guard reports today. Empty since Inc 1 wrapped all nineteen sites
# C3 and X2 named; a site appearing here again is reported as untriaged rather
# than silently accepted. Codex's evidence for the finding cited, per file:
#   C3  app_launcher.py:103,147,251 · app_state_capture.py:231-260,351
#   X2  privacy_manager.py:123,159,199,268 · status_bar.py:284,341,395
#       · appearance.py:128-162
# Two of those citations name the line of the *argument* rather than of the
# call that carries it (status_bar :395 was `mobile_type` inside the call
# opened at :384; app_launcher :103 was the extras list built for the call at
# :108). The nineteen are now enumerated positively in KNOWN_QUOTED below --
# an empty negative list on its own would be satisfied by a detector that had
# gone blind, and by a fix that deleted the calls instead of quoting them.
KNOWN_UNQUOTED: tuple[Expectation, ...] = ()

# The five files C3/X2 named, enumerated the other way round: every argument
# that now *is* wrapped, as an exact multiset. Scoped to those five so a
# concurrent change to navigator or container -- other people's files, other
# findings -- cannot fail this test; the whole corpus is still covered by
# KNOWN_UNQUOTED and by the call-count floor above.
QUOTED_ENUMERATION_FILES = frozenset(
    {
        "app_launcher.py",
        "app_state_capture.py",
        "appearance.py",
        "privacy_manager.py",
        "status_bar.py",
    }
)

# Read off the fixed tree, one entry per call. `str(level)` and friends are
# wrapped alongside the free-text argument they travel with: the exemption
# mechanism is per file::function, so exempting the number would have exempted
# `datatype` and `mobile_type` in the same call -- which is the user-supplied
# text this finding is about. status_bar's set_battery / set_wifi keep their
# numeric exemptions; nothing about them changed.
KNOWN_QUOTED: tuple[Expectation, ...] = (
    Expectation(
        "app_launcher.py", "launch", QUOTED, "quote_for_device_shell(component), *extra_args"
    ),
    Expectation("app_launcher.py", "terminate", QUOTED, "quote_for_device_shell(package_name)"),
    Expectation("app_launcher.py", "open_url", QUOTED, "quote_for_device_shell(url)"),
    Expectation("app_launcher.py", "get_state", QUOTED, "quote_for_device_shell(package_name)"),
    Expectation(
        "app_launcher.py", "_get_launcher_activity", QUOTED, "quote_for_device_shell(package_name)"
    ),
    # Twice in one function -- `dumpsys package <pkg>` and `pidof <pkg>`. The
    # comparison counts, so losing one of the pair is still reported.
    Expectation(
        "app_state_capture.py", "_get_app_info", QUOTED, "quote_for_device_shell(self.package)"
    ),
    Expectation(
        "app_state_capture.py", "_get_app_info", QUOTED, "quote_for_device_shell(self.package)"
    ),
    Expectation(
        "app_state_capture.py", "_capture_logs", QUOTED, "quote_for_device_shell(self.package)"
    ),
    Expectation("appearance.py", "build_locale_command", QUOTED, "quote_for_device_shell(locale)"),
    Expectation(
        "privacy_manager.py",
        "grant_permission",
        QUOTED,
        "quote_for_device_shell(package), quote_for_device_shell(full_permission)",
    ),
    Expectation(
        "privacy_manager.py",
        "revoke_permission",
        QUOTED,
        "quote_for_device_shell(package), quote_for_device_shell(full_permission)",
    ),
    Expectation(
        "privacy_manager.py", "confirm_permission", QUOTED, "quote_for_device_shell(package)"
    ),
    Expectation(
        "privacy_manager.py", "list_app_permissions", QUOTED, "quote_for_device_shell(package)"
    ),
    Expectation(
        "status_bar.py",
        "set_mobile_data",
        QUOTED,
        "quote_for_device_shell(str(level)), quote_for_device_shell(datatype)",
    ),
    Expectation(
        "status_bar.py", "set_time", QUOTED, "quote_for_device_shell(time_str.replace(':', ''))"
    ),
    Expectation(
        "status_bar.py", "override", QUOTED, "quote_for_device_shell(time.replace(':', ''))"
    ),
    Expectation("status_bar.py", "override", QUOTED, "quote_for_device_shell(str(battery))"),
    Expectation("status_bar.py", "override", QUOTED, "quote_for_device_shell(str(wifi_level))"),
    Expectation(
        "status_bar.py",
        "override",
        QUOTED,
        "quote_for_device_shell(str(mobile_level)), quote_for_device_shell(mobile_type)",
    ),
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

    def is_quoted(self, node: ast.AST) -> bool:
        """Whether this argument was *deliberately wrapped* for the device shell.

        Narrower than :meth:`is_safe` on purpose. A string literal is safe and
        is not quoted, and the difference is what lets the positive
        enumeration below say "this call still wraps its package name" rather
        than the far weaker "this call has nothing wrong with it".
        """
        if self._is_quoter_call(node):
            return True
        return isinstance(node, ast.Name) and node.id in self._local_quoted

    def is_safe(self, node: ast.AST) -> bool:
        """Whether this argument may cross the device shell as it stands."""
        return self._is_literal(node) or self.is_quoted(node)


def _module_literal_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _quoter_names(tree: ast.AST) -> set[str]:
    """``quote_for_device_shell`` plus any local alias that only returns it."""
    names = {QUOTER}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == QUOTER:
                    names.add(alias.asname or alias.name)
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        returns = [n for n in ast.walk(func) if isinstance(n, ast.Return) and n.value is not None]
        if returns and all(
            isinstance(r.value, ast.Call) and _called_name(r.value) in names for r in returns
        ):
            names.add(func.name)
    return names


def scan_quoting(filename: str, source: str, *, report: str = UNQUOTED) -> list[Site]:
    """Device-shell arguments that are neither literal nor quoted.

    A ``*args`` splat is a forwarder, not an interpolation: the values came
    from the caller. So a function that splats its own ``*args`` straight into
    a sink (``status_bar._demo_broadcast``, ``push_notification._shell``) is
    resolved one level out and its *call sites* are checked instead. Without
    that, three quarters of the status-bar defect is invisible.

    With ``report=QUOTED`` the same walk reports the opposite population: the
    arguments that *are* wrapped. One traversal answers both questions, so the
    two enumerations below cannot drift into disagreeing about which calls
    reach the device shell at all -- and removing a wrapper moves a site from
    one list to the other, failing both.
    """
    tree = ast.parse(source, filename=filename)
    module_literals = _module_literal_names(tree)
    quoters = _quoter_names(tree)
    enclosing = _functions_by_node(tree)
    owner: dict[int, ast.AST] = {}
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(func):
                owner.setdefault(id(node), func)

    forwarders: set[str] = set()
    sites: list[Site] = []

    def classify(args: list[ast.expr], func: ast.AST | None) -> list[str]:
        """The arguments of one call that fall into the population asked for."""
        scope = _QuotingScope(module_literals, quoters, func)
        found: list[str] = []
        for arg in args:
            value = arg.value if isinstance(arg, ast.Starred) else arg
            if isinstance(arg, ast.Starred):
                vararg = getattr(getattr(func, "args", None), "vararg", None)
                if isinstance(value, ast.Name) and vararg is not None and value.id == vararg.arg:
                    forwarders.add(func.name)
                    continue
            if report == QUOTED:
                if scope.is_quoted(value):
                    found.append(ast.unparse(arg))
                continue
            if not scope.is_safe(value):
                found.append(ast.unparse(arg))
        return found

    def record(node: ast.Call, detail: list[str]) -> None:
        sites.append(
            Site(
                filename,
                enclosing.get(id(node), "<module>"),
                node.lineno,
                report,
                ", ".join(detail),
            )
        )

    # Direct sinks: run_adb("shell", serial, ...) and friends, under whatever
    # name this module imported them.
    for node in _sink_calls(tree, "shell"):
        detail = classify(node.args[2:], owner.get(id(node)))
        if detail:
            record(node, detail)

    # Call sites of the forwarders discovered above.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) not in forwarders:
            continue
        func = owner.get(id(node))
        if func is not None and func.name in forwarders:
            continue  # the definition forwarding to itself
        detail = classify(node.args, func)
        if detail:
            record(node, detail)

    return sorted(sites, key=lambda site: site.line)


def unquoted_device_shell_arguments(*, apply_exemptions: bool = True) -> list[Site]:
    found: list[Site] = []
    for path in _script_files():
        for site in scan_quoting(_relative(path), path.read_text(encoding="utf-8")):
            if apply_exemptions and f"{site.file}::{site.function}" in QUOTING_EXEMPT:
                continue
            found.append(site)
    return found


def quoted_device_shell_arguments() -> list[Site]:
    """The wrapped arguments in the five files C3/X2 named."""
    found: list[Site] = []
    for path in _script_files():
        name = _relative(path)
        if name not in QUOTED_ENUMERATION_FILES:
            continue
        found.extend(scan_quoting(name, path.read_text(encoding="utf-8"), report=QUOTED))
    return found


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

# The evasion review found: an alias the earlier version of this guard could
# not follow, so it reported nothing at all for the whole module.
_SYNTHETIC_ALIASED = """
from common.adb_exec import run_adb as adb


def wipe(serial, package):
    return adb("shell", serial, "pm", "clear", package)
"""

_SYNTHETIC_MODULE_ALIASED = """
from common import adb_exec as ax


def wipe(serial, package):
    return ax.run_adb("shell", serial, "pm", "clear", package)
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


@pytest.mark.parametrize(
    ("label", "source"),
    [("import alias", _SYNTHETIC_ALIASED), ("module alias", _SYNTHETIC_MODULE_ALIASED)],
)
def test_the_quoting_guard_is_not_evaded_by_renaming(label: str, source: str):
    """Keying on the call's spelling made `run_adb as adb` invisible.

    Confirmed by review against the previous version of this file: it returned
    an empty list for a module doing exactly this, which reads as "clean".
    """
    found = scan_quoting("synthetic.py", source)
    assert [site.function for site in found] == ["wipe"], f"{label} evaded the guard: {found}"


def test_the_quoting_guard_enumerates_todays_sites():
    """The evidence that the guard is reading the real code, not agreeing with itself.

    Matched as a multiset of (file, function, kind, argument text), so the
    *count* inside a function is asserted too and a rename cannot pass as the
    same site. Lines appear in the failure message and in the comments beside
    each entry; they are not compared. ``KNOWN_UNQUOTED`` is empty since Inc 1,
    so this now says "nothing untriaged reaches the device shell"; the claim
    that the nineteen were *fixed* rather than *lost* is the next test.
    """
    _assert_enumeration(KNOWN_UNQUOTED, unquoted_device_shell_arguments(), "KNOWN_UNQUOTED")


def test_the_five_named_files_wrap_every_value_they_interpolate():
    """The positive half, and the one an emptied negative list cannot supply.

    "No unquoted arguments" is satisfied three ways: the values are wrapped,
    the detector went blind, or the calls were deleted. Only the first is the
    fix, so the nineteen sites are enumerated again from the other side --
    same traversal, same multiset comparison, reporting the arguments that
    *are* wrapped. Deleting one ``quote_for_device_shell`` makes this test
    report it missing and the test above report it untriaged.
    """
    _assert_enumeration(KNOWN_QUOTED, quoted_device_shell_arguments(), "KNOWN_QUOTED")


def test_the_quoting_exemptions_do_not_rot():
    """An exemption that no longer exempts anything is a door left open.

    Same failure as a stale ``KNOWN_VIOLATIONS`` entry in
    ``test_fixture_policy``: the code was fixed or deleted, the exemption
    outlived it, and it now silently blesses whatever takes that name next.
    """
    all_keys = {
        f"{site.file}::{site.function}"
        for site in unquoted_device_shell_arguments(apply_exemptions=False)
    }
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

EMU_ARGV = "emu-argv"

# Empty since L7: every one of the six sites below now calls
# common.emu_console.run_emu, and the guard reports nothing outside
# common/emu_console.py. Kept as a comment rather than deleted, because the
# list is the record of what the fix had to reach -- three of the six were in
# files nobody had connected to the finding, which is the whole reason the
# guard enumerates rather than spot-checks:
#
#   emulator_boot.py      _get_avd_name_for_serial   run_adb            (:211)
#   emulator_erase.py     is_avd_running             run_adb            (:108)
#   emulator_selector.py  _avd_name_for_serial       run_adb            (:333)
#   emulator_shutdown.py  shutdown                   build_adb_command  (:81)
#   emulator_shutdown.py  get_avd_name_for_serial    build_adb_command  (:156)
#   location.py           build_geo_fix_command      build_adb_command  (:144)
#
# The line numbers are why lines are not matched data: PR #8 landed the L1
# physical-device refusal above the shutdown call and moved it from :66 to :81,
# which failed the enumeration in CI while passing on the branch. The plan cites
# location.py:273, which is `_run_geo_fix` -- it executed an argv it did not
# build; the literal "emu" was in `build_geo_fix_command`, now
# `build_geo_fix_args`, which builds console arguments and no adb argv at all.
KNOWN_EMU_BYPASSES: tuple[Expectation, ...] = ()

# Scripts that reach the console the sanctioned way. Named here so the guard
# is checked for false positives as well as false negatives: run_emu() must
# never be reported, or the fix would look like the defect.
EMU_CONSOLE_CLIENTS = ("sms.py", "snapshot.py")


def scan_emu_console(filename: str, source: str) -> list[Site]:
    """Every place an ``adb emu`` argv is constructed.

    Two shapes, because the second is how the first gets around a guard that
    only knows the first: the operation passed to an argv builder (under
    whatever name the module imported it), and a literal argv list handed to
    ``subprocess``.
    """
    tree = ast.parse(source, filename=filename)
    enclosing = _functions_by_node(tree)
    lookup = _sink_lookup(tree)
    sites: list[Site] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        detail = None

        canonical = lookup.canonical(node)
        if canonical and isinstance(first, ast.Constant) and first.value == "emu":
            detail = canonical
        elif _called_name(node) in ("run", "Popen") and isinstance(first, ast.List | ast.Tuple):
            values = [e.value for e in first.elts if isinstance(e, ast.Constant)]
            if "emu" in values and "adb" in values:
                detail = "subprocess"

        if detail:
            sites.append(
                Site(filename, enclosing.get(id(node), "<module>"), node.lineno, EMU_ARGV, detail)
            )

    return sorted(sites, key=lambda site: site.line)


def emu_console_bypasses() -> list[Site]:
    found: list[Site] = []
    for path in _script_files():
        relative = _relative(path)
        if relative == EMU_CONSOLE:
            continue
        found.extend(scan_emu_console(relative, path.read_text(encoding="utf-8")))
    return found


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

_SYNTHETIC_EMU_ALIASED = """
from common.adb_exec import run_adb as adb


def avd_name(serial):
    return adb("emu", serial, "avd", "name").stdout
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


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("plain", _SYNTHETIC_EMU),
        ("aliased", _SYNTHETIC_EMU_ALIASED),
    ],
)
def test_the_emu_guard_flags_a_synthetic_builder_call(label: str, source: str):
    """Anti-vacuity, and the same alias evasion the quoting guard had."""
    found = scan_emu_console("synthetic.py", source)
    assert [site.function for site in found] == ["avd_name"], f"{label}: {found}"


def test_the_emu_guard_flags_a_raw_subprocess_argv():
    """The second shape: never build the argv by hand either."""
    found = scan_emu_console("synthetic.py", _SYNTHETIC_EMU_SUBPROCESS)
    assert [site.function for site in found] == ["kill"], found


def test_the_emu_guard_does_not_flag_run_emu():
    """The negative control -- and the shape the fix will take."""
    assert scan_emu_console("synthetic.py", _SYNTHETIC_RUN_EMU) == []


def test_the_emu_guard_enumerates_todays_sites():
    """The five bypassing scripts, by file, function and which builder they call."""
    _assert_enumeration(KNOWN_EMU_BYPASSES, emu_console_bypasses(), "KNOWN_EMU_BYPASSES")


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

BOUNDS_FUNCTION = "function"
BOUNDS_REGEX = "regex"

# A regex literal that takes apart uiautomator's bounds string: an escaped
# opening bracket, then a digit class (optionally signed) before the comma.
# Matched against the literal's *text*, so `\[(\d+),` and `\[(-?\d+),` both
# answer, and `flags=\[\s*(?P<flags>[^\]]*?)\]` -- the one other bracket regex
# in the skill -- does not. This is the general net; the name rule below is
# only the belt to its braces.
_BOUNDS_GRAMMAR = re.compile(r"\\\[[^\]]*-?\\d")

# Exact names, not a suffix rule. `bounds$` after underscore-stripping would
# also flag `get_bounds` and `calculate_bounds`, which read a rectangle rather
# than parse the attribute -- and a guard that fires on correct code gets
# repaired by weakening it.
_BOUNDS_PARSER_NAMES = frozenset({"_parse_bounds", "_bounds"})

KNOWN_BOUNDS_SITES: tuple[Expectation, ...] = (
    Expectation("accessibility_audit.py", "_parse_bounds", BOUNDS_FUNCTION),  # :70
    Expectation("accessibility_audit.py", "_parse_bounds", BOUNDS_REGEX, "signed"),  # :72
    Expectation("navigator.py", "_parse_bounds", BOUNDS_FUNCTION),  # :290
    Expectation("navigator.py", "_parse_bounds", BOUNDS_REGEX, "unsigned"),  # :301
    Expectation("screen_mapper.py", "_bounds", BOUNDS_FUNCTION),  # :268
    Expectation("screen_mapper.py", "_bounds", BOUNDS_REGEX, "unsigned"),  # :270
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
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in _BOUNDS_PARSER_NAMES:
                sites.append(Site(filename, node.name, node.lineno, BOUNDS_FUNCTION))
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
                    BOUNDS_REGEX,
                    "signed" if "-?" in node.value else "unsigned",
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

# Reading a rectangle is not parsing the attribute. A suffix rule flagged both.
_SYNTHETIC_BOUNDS_LOOKALIKE_NAMES = """
def get_bounds(element):
    return element.rect


def calculate_bounds(nodes):
    return [get_bounds(node) for node in nodes]
"""


def test_the_bounds_guard_flags_a_synthetic_violation():
    """Anti-vacuity: both the regex and the function name are reported."""
    found = scan_bounds("synthetic.py", _SYNTHETIC_BOUNDS)
    assert [site.kind for site in found] == [BOUNDS_FUNCTION, BOUNDS_REGEX], found


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


def test_the_bounds_guard_ignores_functions_that_only_read_a_rectangle():
    """`get_bounds` and `calculate_bounds` are not copies of the parser."""
    assert scan_bounds("synthetic.py", _SYNTHETIC_BOUNDS_LOOKALIKE_NAMES) == []


def test_the_bounds_guard_enumerates_todays_sites():
    """Three files, three grammars, each with its function, matched one-for-one."""
    _assert_enumeration(
        KNOWN_BOUNDS_SITES, bounds_grammars_outside_hierarchy(), "KNOWN_BOUNDS_SITES"
    )


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


def test_the_enumeration_comparison_counts_rather_than_keys():
    """The set-versus-multiset bug this file shipped with, pinned -- without lines.

    Deleting the bounds regex from ``screen_mapper._bounds`` used to leave the
    enumeration green, because the function finding in the same place satisfied
    a set keyed on file and function. ``_reconcile`` compares counts per
    (file, function, kind, detail), so the regex entry has nothing to match and
    is reported missing. No line number takes part: the survivor is placed at a
    line neither expected entry ever carried, and the answer is the same.
    """
    expected = (
        Expectation("screen_mapper.py", "_bounds", BOUNDS_FUNCTION),
        Expectation("screen_mapper.py", "_bounds", BOUNDS_REGEX, "unsigned"),
    )

    survivor = [Site("screen_mapper.py", "_bounds", 999, BOUNDS_FUNCTION)]
    missing, unexpected = _reconcile(expected, survivor)
    assert [item.kind for item in missing] == [BOUNDS_REGEX]
    assert unexpected == []

    # An extra finding of an already-listed kind is reported, again regardless
    # of where it sits.
    extra = [*survivor, Site("screen_mapper.py", "_bounds", 12, BOUNDS_FUNCTION)]
    missing, unexpected = _reconcile(expected, extra)
    assert [item.kind for item in missing] == [BOUNDS_REGEX]
    assert [site.line for site in unexpected] == [12]

    # Both present, at lines nothing predicted: clean.
    complete = [
        Site("screen_mapper.py", "_bounds", 1, BOUNDS_FUNCTION),
        Site("screen_mapper.py", "_bounds", 2, BOUNDS_REGEX, "unsigned"),
    ]
    assert _reconcile(expected, complete) == ([], [])


def test_the_enumeration_counts_duplicate_findings_in_one_function():
    """Two identical interpolations in one function are two entries, not one.

    ``app_state_capture._get_app_info`` reads ``self.package`` into two separate
    device-shell commands. A comparison that deduplicated would accept a fix to
    one of them as a fix to both.
    """
    expected = (
        Expectation("app_state_capture.py", "_get_app_info", UNQUOTED, "self.package"),
        Expectation("app_state_capture.py", "_get_app_info", UNQUOTED, "self.package"),
    )
    one_fixed = [Site("app_state_capture.py", "_get_app_info", 248, UNQUOTED, "self.package")]

    missing, unexpected = _reconcile(expected, one_fixed)
    assert len(missing) == 1
    assert unexpected == []


def test_a_moved_call_is_not_reported_as_a_change():
    """The CI failure that produced this design, as a test.

    PR #8 inserted the physical-device refusal above ``emulator_shutdown``'s
    console call and moved it from line 66 to line 81. The guard reported the
    same call in the same function; only the line differed. That must be
    silent.
    """
    expected = (Expectation("emulator_shutdown.py", "shutdown", EMU_ARGV, "build_adb_command"),)
    before = [Site("emulator_shutdown.py", "shutdown", 66, EMU_ARGV, "build_adb_command")]
    after = [Site("emulator_shutdown.py", "shutdown", 81, EMU_ARGV, "build_adb_command")]

    assert _reconcile(expected, before) == ([], [])
    assert _reconcile(expected, after) == ([], [])


def test_every_script_basename_is_unique_enough_to_key_on():
    """The site keys are paths relative to scripts/, so `common/` never collides."""
    relatives = [_relative(path) for path in _script_files()]
    assert len(relatives) == len(set(relatives))
    assert EMU_CONSOLE in relatives
    assert HIERARCHY in relatives
