"""The agent's documented path, driven end to end against recorded screens.

The thesis of the v0.7.0 plan, in one line: **the unit of verification is the
agent's documented path, not the function.**

v0.6.0 verified fixes at the line and capabilities at the function. Both stop
one layer short of where the agent stands. ``test_agent_task_e2e.py`` passes: it
reads ``screen_mapper --json``, checks that "Submit Order" is *among* the
reported labels, and then acts with ``--find-type EditText``. It never feeds a
printed label back into ``navigator --find-text --tap`` -- which is Quick Start
step 3, the thing SKILL.md actually tells the agent to do. The loop it certifies
therefore closes on a path the agent is not told to take.

This module drives the documented path instead:

    screen_mapper (the report an agent reads)
      -> a control name from that report
      -> navigator --find-text <name> --tap
      -> the `input tap x y` argv that actually reached adb

and asserts the tap landed inside the control that name belongs to. The expected
rectangle is a literal in the inventory below, checked against the recorded
hierarchy by ``test_the_case_inventory_matches_the_recorded_screen``, and never
read off navigator's own ``Tapped:`` line: a test that trusts the tool's report
of where it tapped is structurally unable to see the tool tapping the wrong
thing. That is the same mistake one layer up -- ``test_compose_visibility.py``
asserts the label is *produced*, never that it is *usable*.

Two recorded screens, because they fail differently. The Compose fixture carries
unlabelled controls whose captions are row-adjacent siblings; the Settings
fixture is View-based, with genuine ``text`` attributes and resource-id labels.

Everything runs in-process against ``tests/fixtures/recorded/``:
``capture_hierarchy`` is stubbed to the recorded XML and every adb call is
recorded rather than issued, so this belongs to the required mocked check and
touches no device. The emulator lane repeats the same walk live. Collection
parses no XML and runs no production code -- the case list is static, and the
fixture proves it right from inside the harness.

Red cases are pinned ``xfail(strict=True)`` against C1, C2 and C4. ``strict``
means fixing one turns its case red until the marker is deleted in the same
commit -- the defect register stays executable rather than aspirational.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from types import SimpleNamespace

import navigator
import pytest
import screen_mapper

from common import adb_exec

COMPOSE = "uiautomator_compose_default.xml"
SETTINGS = "uiautomator_settings_top.xml"
SCREENS = (COMPOSE, SETTINGS)

# A screen an agent can act on names at least this many of its controls. The
# plan's floor for C4, applied to both toolkits.
MINIMUM_NAMED_CONTROLS = 5

# `elements_by_type` collects passive captions under this key; every other key
# holds a control the agent can operate. An interactive TextView does not land
# here -- `TextView` is absent from `KNOWN_WIDGET_CLASSES`, so it is bucketed as
# `Control` like any other unlabelled interactive node.
PASSIVE_BUCKET = "TextView"

# The four properties uiautomator uses to say an element can be operated, matching
# `screen_mapper.INTERACTIVE_ATTRIBUTES`. Re-stated rather than imported: this
# module is the independent second opinion, and importing the thing under test
# to compute the expectation is how a test comes to agree with a bug.
INTERACTIVE_ATTRIBUTES = ("clickable", "checkable", "long-clickable", "scrollable")

# Cap on the caption recovered from a control's subtree, matching
# `screen_mapper.MAX_RECOVERED_LABEL_PARTS`. Re-stated for the same reason.
MAX_CAPTION_PARTS = 3

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_QUOTED = re.compile(r'"([^"]*)"')


# ---------------------------------------------------------------------------
# The case inventory.
# ---------------------------------------------------------------------------
#
# Each entry is a control name the screen report hands the agent, paired with
# the rectangle a tap for that name has to land in. Static and explicit, in the
# order the report produces them, so collection reads no XML and executes none
# of the code under test.
#
# It cannot rot: `test_the_case_inventory_matches_the_recorded_screen` drives
# `screen_mapper --json` against the recorded dump inside the harness and
# requires exactly these names in exactly this order, then re-derives every
# rectangle from the hierarchy.

COMPOSE_CONTROLS: tuple[tuple[str, tuple[int, int, int, int]], ...] = (
    ("Remember me", (33, 754, 159, 880)),
    ("Submit Order", (32, 217, 375, 343)),
    ("Order #4821 2 items Ships tomorrow", (32, 353, 1048, 584)),
    ("Dark theme", (32, 890, 169, 1016)),
    ("Company logo", (33, 1028, 159, 1154)),
    ("List item 0 List item 1 List item 2", (32, 1164, 1048, 1637)),
    ("Email address", (32, 595, 1048, 742)),
)

SETTINGS_CONTROLS: tuple[tuple[str, tuple[int, int, int, int]], ...] = (
    ("com.android.settings:id/settings_homepage_container", (0, 142, 1080, 2361)),
    ("Profile picture, double tap to open Google Account", (891, 289, 1017, 415)),
    ("com.android.settings:id/search_action_bar", (42, 605, 1038, 742)),
    ("com.android.settings:id/main_content_scrollable_container", (0, 784, 1080, 2361)),
    ("Network & internet Mobile, Wi‑Fi, hotspot", (0, 784, 1080, 1015)),
    ("Connected devices Bluetooth, pairing", (0, 1015, 1080, 1246)),
    ("Apps Assistant, recent apps, default apps", (0, 1246, 1080, 1477)),
    ("Notifications Notification history, conversations", (0, 1477, 1080, 1708)),
    ("Battery 100%", (0, 1708, 1080, 1939)),
    ("Storage 86% used - 1.08 GB free", (0, 1939, 1080, 2170)),
    ("Sound & vibration Volume, haptics, Do Not Disturb", (0, 2170, 1080, 2361)),
)

SCREEN_CONTROLS = {COMPOSE: COMPOSE_CONTROLS, SETTINGS: SETTINGS_CONTROLS}


# ---------------------------------------------------------------------------
# The defect register.
# ---------------------------------------------------------------------------
#
# Names above that `navigator --find-text <name> --tap` does not land on today.
# Measured by driving both CLIs against the recorded dumps, not predicted:
#
#   Tap issued but outside the control
#     "Remember me"  -> (302, 816), the caption TextView; the CheckBox is
#                       [33,754][159,880], so the tap misses it by 143px.
#     "Dark theme"   -> (282, 953); the Switch is [32,890][169,1016].
#     "Company logo" -> (221, 1090); the icon button is [33,1028][159,1154].
#   Nothing found at all (exit 1, no argv issued)
#     both Compose recovered captions, all seven Settings row captions, and the
#     three Settings resource-id captions -- none of which exists as any single
#     node's `text` or `content-desc`.
#
# C1 is why: `_find_in` (navigator.py:449) matches `text + content_desc` only,
# so `recovered_label` -- the caption `--list` and the mapper both print
# (navigator.py:343) -- is not searchable, and a match on the caption returns
# the caption's own non-interactive node instead of the control it describes.
C1_RED = frozenset(
    {
        (COMPOSE, "Order #4821 2 items Ships tomorrow"),
        (COMPOSE, "Dark theme"),
        (COMPOSE, "Company logo"),
        (COMPOSE, "List item 0 List item 1 List item 2"),
        (COMPOSE, "Remember me"),
        (SETTINGS, "com.android.settings:id/settings_homepage_container"),
        (SETTINGS, "com.android.settings:id/search_action_bar"),
        (SETTINGS, "com.android.settings:id/main_content_scrollable_container"),
        (SETTINGS, "Network & internet Mobile, Wi‑Fi, hotspot"),
        (SETTINGS, "Connected devices Bluetooth, pairing"),
        (SETTINGS, "Apps Assistant, recent apps, default apps"),
        (SETTINGS, "Notifications Notification history, conversations"),
        (SETTINGS, "Battery 100%"),
        (SETTINGS, "Storage 86% used - 1.08 GB free"),
        (SETTINGS, "Sound & vibration Volume, haptics, Do Not Disturb"),
    }
)

# Screens whose DEFAULT (non-verbose, non-JSON) report names no control at all.
# Both of them, measured: the Compose report is three lines -- a header, an
# EditText count and a focusable count -- and the Settings report two. The names
# exist; they are reachable only under `--verbose` or `--json`, neither of which
# Quick Start step 2 tells the agent to pass.
C4_RED = frozenset(SCREENS)

C1_REASON = (
    "C1: navigator._find_in matches text + content_desc only, so a label the "
    "screen report printed is either unfindable or resolves to the caption "
    "node instead of the control it names (navigator.py:343,449)"
)
C2_REASON = (
    "C2: --tap/--enter-text with no --find-*/--tap-at matches every enabled "
    "node, so matches[0] is the <hierarchy> root at (0,0,0,0); `input tap 0 0` "
    "is issued, the exit status is 0 and the message names a real element "
    "(navigator.py:431,896)"
)
C4_REASON = (
    "C4: the default screen report names no interactive control -- they land in "
    "the `Control` bucket, printed only under --verbose or --json, while Quick "
    "Start step 2 runs screen_mapper with neither"
)


# ---------------------------------------------------------------------------
# Driving a CLI in-process, with nothing reaching a device.
# ---------------------------------------------------------------------------


def _refuse_subprocess(*args, **kwargs):
    """Last line of defence: no test here may spawn adb."""
    raise AssertionError(
        f"a subprocess escaped the stubs and would have run against an attached "
        f"device: {args!r} {kwargs!r}"
    )


# `common.adb_exec` reaches the outside world only through its module-global
# `subprocess`. Replacing that binding (not `subprocess.run` itself, which is
# global to the interpreter) means every adb path in the package -- including
# `device_utils`, which binds `run_adb` directly and so is not covered by
# stubbing `adb_exec.run_adb` -- fails loudly instead of driving the phone.
_NO_SUBPROCESS = SimpleNamespace(
    run=_refuse_subprocess,
    Popen=_refuse_subprocess,
    SubprocessError=subprocess.SubprocessError,
    TimeoutExpired=subprocess.TimeoutExpired,
    CalledProcessError=subprocess.CalledProcessError,
    PIPE=subprocess.PIPE,
    DEVNULL=subprocess.DEVNULL,
    STDOUT=subprocess.STDOUT,
)


class _AdbRecorder:
    """Stands in for ``adb_exec.run_adb`` and keeps every argv it was handed.

    The point of the whole module is *what argv reached adb*, so the recording
    is the assertion surface. Nothing is executed.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, tuple[str, ...]]] = []

    def __call__(self, operation, serial=None, *args, timeout=None, check=False):
        self.calls.append((operation, serial, tuple(str(arg) for arg in args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="", output="", ok=True)

    def shell_inputs(self, verb: str) -> list[tuple[str, ...]]:
        """Every ``adb shell input <verb> ...`` argv issued, in order."""
        return [
            args
            for operation, _serial, args in self.calls
            if operation == "shell" and args[:2] == ("input", verb)
        ]


@dataclass
class _Run:
    """What one CLI invocation produced."""

    status: object
    stdout: str
    stderr: str
    adb: _AdbRecorder


def _run_cli(module, argv: list[str], screen: ET.Element, monkeypatch) -> _Run:
    """Run a script's ``main()`` against a recorded screen, recording adb.

    The seams are the ones the scripts genuinely use: both read the screen
    through the module-global ``capture_hierarchy`` they imported from
    ``common.hierarchy``, and both reach adb through ``adb_exec.run_adb``. No
    ``--serial`` is passed, so ``resolve_device_identifier(None)`` returns None
    without consulting adb at all.
    """
    adb = _AdbRecorder()
    monkeypatch.setattr(adb_exec, "run_adb", adb)
    monkeypatch.setattr(adb_exec, "subprocess", _NO_SUBPROCESS)
    monkeypatch.setattr(navigator, "capture_hierarchy", lambda serial=None, **kwargs: screen)
    monkeypatch.setattr(screen_mapper, "capture_hierarchy", lambda serial=None, **kwargs: screen)
    # A real settle delay would add half a second per tap for no signal here.
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0)
    monkeypatch.setattr(sys, "argv", list(argv))

    out, err = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        pytest.raises(SystemExit) as exit_info,
    ):
        module.main()

    code = exit_info.value.code
    return _Run(
        status=0 if code is None else code,
        stdout=out.getvalue(),
        stderr=err.getvalue(),
        adb=adb,
    )


# ---------------------------------------------------------------------------
# Reading the hierarchy independently of the scripts under test.
# ---------------------------------------------------------------------------


def _bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    """Parse ``bounds="[l,t][r,b]"``; None when absent or malformed."""
    match = _BOUNDS.match(node.get("bounds", ""))
    return tuple(int(group) for group in match.groups()) if match else None


def _is_interactive(node: ET.Element) -> bool:
    """Whether this node is a control an agent can operate.

    Enabled, non-zero area, and at least one of the four interaction
    properties. Area matters because uiautomator emits no visibility attribute,
    so a collapsed rectangle is the only signal that a flagged node cannot be
    touched -- the Settings fixture ends with exactly such a row,
    ``[0,2401][1080,2361]``.
    """
    box = _bounds(node)
    if box is None or box[2] <= box[0] or box[3] <= box[1]:
        return False
    if node.get("enabled", "true") != "true":
        return False
    return any(node.get(attribute) == "true" for attribute in INTERACTIVE_ATTRIBUTES)


def _text_of(node: ET.Element) -> str:
    """The caption a node carries in its own right."""
    return (node.get("text") or "").strip() or (node.get("content-desc") or "").strip()


def _captions(node: ET.Element, parent: ET.Element | None) -> set[str]:
    """Every name under which a control could be reported to the agent.

    Derived from the XML, in the order the evidence supports:

    1. Its own ``text`` / ``content-desc`` / ``resource-id``.
    2. Failing that, the captions of its subtree -- a Compose Button's "Submit
       Order", a Card's item lines -- joined, because uiautomator dumps the
       unmerged semantics tree and there is no folded label to read.
    3. Failing that, a **row-adjacent sibling**. This is the case a "nearest
       interactive ancestor" rule gets wrong: a Compose CheckBox's "Remember me"
       and a Switch's "Dark theme" are siblings of the control, not ancestors or
       descendants of it, so the row is resolved by vertical bounds overlap and
       not by parentage.
    """
    own = _text_of(node) or (node.get("resource-id") or "").strip()
    if own:
        return {own}

    parts = [_text_of(descendant) for descendant in node.iter() if descendant is not node]
    parts = [part for part in parts if part]
    if parts:
        return {" ".join(parts[:MAX_CAPTION_PARTS])}

    if parent is None:
        return set()

    siblings = list(parent)
    position = siblings.index(node)
    box = _bounds(node)
    for offset in (1, -1):
        index = position + offset
        if not 0 <= index < len(siblings):
            continue
        sibling = siblings[index]
        sibling_box = _bounds(sibling)
        if box is not None and sibling_box is not None:
            # Same row: the vertical spans must overlap.
            if not (sibling_box[1] < box[3] and box[1] < sibling_box[3]):
                continue
        candidate = _text_of(sibling) or next(
            (_text_of(child) for child in sibling.iter() if _text_of(child)), ""
        )
        if candidate:
            return {candidate}
    return set()


def _controls(root: ET.Element) -> list[tuple[ET.Element, set[str]]]:
    """Every operable control on a screen, with the names it answers to."""
    found: list[tuple[ET.Element, set[str]]] = []

    def walk(node: ET.Element, parent: ET.Element | None) -> None:
        if _is_interactive(node):
            found.append((node, _captions(node, parent)))
        for child in node:
            walk(child, node)

    walk(root, None)
    return found


def _expected_rect(root: ET.Element, label: str) -> tuple[int, int, int, int]:
    """The rectangle a tap for ``label`` has to land in, re-derived from the XML.

    Ambiguity fails loudly rather than picking one: two controls answering to
    the same name is a finding in its own right, not something to average over.
    """
    matches = [node for node, captions in _controls(root) if label in captions]
    if not matches:
        pytest.fail(
            f"no interactive node on this screen is named {label!r}, so the "
            f"expectation cannot be computed. Either the screen report is "
            f"naming something that is not a control, or this resolver and the "
            f"report disagree about captions."
        )
    if len(matches) > 1:
        pytest.fail(
            f"{len(matches)} controls answer to {label!r} "
            f"({[_bounds(node) for node in matches]}); an agent handed that "
            f"name has no way to say which one it meant."
        )
    box = _bounds(matches[0])
    assert box is not None
    return box


def _inside(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


# ---------------------------------------------------------------------------
# Reading the report.
# ---------------------------------------------------------------------------


def _interactive_labels(analysis: dict) -> list[str]:
    """Control names the report offers, in the order and number it offers them.

    Capped per bucket at ``BUTTONS_PREVIEW``, which is what the summary line is
    allowed to elide; a name beyond the cap is deliberately not shown and must
    not be demanded of the text output.
    """
    return [
        label
        for bucket, labels in sorted(analysis["elements_by_type"].items())
        if bucket != PASSIVE_BUCKET
        for label in labels[: screen_mapper.BUTTONS_PREVIEW]
    ]


def _names_printed(report: str) -> list[str]:
    """Control names the report puts in front of the agent, parsed from it.

    The report's grammar is ``Section: "name", "name"``; the header line carries
    counts rather than names, so it is excluded. Quoted names are read wherever
    they appear, so a new ``Controls:`` line satisfies this without the parser
    needing to learn about it.
    """
    return [
        name
        for line in report.splitlines()
        if not line.startswith("Screen:")
        for name in _QUOTED.findall(line)
        if name and not name.startswith("...")
    ]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "empty"


def _screen_id(screen_name: str) -> str:
    return screen_name.removeprefix("uiautomator_").removesuffix(".xml")


def _c1_params() -> list:
    """One case per inventory entry: the name, and the rectangle it must hit."""
    return [
        pytest.param(
            screen_name,
            label,
            rect,
            marks=(
                [pytest.mark.xfail(strict=True, reason=C1_REASON)]
                if (screen_name, label) in C1_RED
                else []
            ),
            id=f"{_screen_id(screen_name)}-{_slug(label)}",
        )
        for screen_name in SCREENS
        for label, rect in SCREEN_CONTROLS[screen_name]
    ]


def _c4_params() -> list:
    return [
        pytest.param(
            screen_name,
            marks=(
                [pytest.mark.xfail(strict=True, reason=C4_REASON)] if screen_name in C4_RED else []
            ),
            id=_screen_id(screen_name),
        )
        for screen_name in SCREENS
    ]


_SCREEN_IDS = [_screen_id(name) for name in SCREENS]


# ---------------------------------------------------------------------------
# Step 2 of Quick Start: see the screen.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_name", _c4_params())
def test_the_default_screen_report_names_the_controls_it_counts(recorded, monkeypatch, screen_name):
    """Counting seven controls and naming none of them leaves the agent blind.

    Quick Start step 2 runs ``screen_mapper.py`` with no flags, and step 3 asks
    the agent to feed a name from it into ``--find-text``. A report that says
    "7 interactive" without saying which seven cannot serve step 3, whatever
    ``--json`` happens to contain.
    """
    screen = ET.fromstring(recorded.text(screen_name))

    report = _run_cli(screen_mapper, ["screen_mapper.py"], screen, monkeypatch)
    assert report.status == 0, f"screen_mapper failed: {report.stderr.strip()}"

    printed = _names_printed(report.stdout)
    assert len(printed) >= MINIMUM_NAMED_CONTROLS, (
        f"the default report names {len(printed)} controls, below the floor of "
        f"{MINIMUM_NAMED_CONTROLS}. An agent following Quick Start reads this "
        f"and has nothing to hand step 3:\n{report.stdout.strip()}"
    )

    payload = json.loads(
        _run_cli(screen_mapper, ["screen_mapper.py", "--json"], screen, monkeypatch).stdout
    )
    missing = [label for label in _interactive_labels(payload) if label not in printed]
    assert not missing, (
        f"--json reports these controls but the default report does not name "
        f"them, so they are reachable only by an agent that knew to ask for "
        f"JSON: {missing}\nDefault report was:\n{report.stdout.strip()}"
    )


# ---------------------------------------------------------------------------
# Step 3 of Quick Start: act on what step 2 printed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("screen_name", "label", "expected"), _c1_params())
def test_a_reported_name_taps_the_control_it_names(
    recorded, monkeypatch, screen_name, label, expected
):
    """The loop, closed: a name the screen report produced goes back in as a tap.

    The expected rectangle comes from the inventory, which the fixture-backed
    inventory test holds to the hierarchy. So a tap on the caption beside a
    checkbox fails here even though navigator reports it as a success naming the
    right thing.
    """
    screen = ET.fromstring(recorded.text(screen_name))

    run = _run_cli(navigator, ["navigator.py", "--find-text", label, "--tap"], screen, monkeypatch)
    taps = run.adb.shell_inputs("tap")

    assert len(taps) == 1, (
        f"a name the screen report printed produced {len(taps)} taps, not one.\n"
        f"  name:     {label!r}\n"
        f"  control:  {expected}\n"
        f"  exit:     {run.status}\n"
        f"  said:     {run.stdout.strip()!r}"
    )

    x, y = int(taps[0][2]), int(taps[0][3])
    assert _inside(expected, x, y), (
        f"the tap landed outside the control the name belongs to.\n"
        f"  name:     {label!r}\n"
        f"  tapped:   ({x}, {y})\n"
        f"  control:  {expected}\n"
        f"  said:     {run.stdout.strip()!r}"
    )


@pytest.mark.parametrize("screen_name", SCREENS, ids=_SCREEN_IDS)
@pytest.mark.parametrize("action", [["--tap"], ["--enter-text", "x"]], ids=["tap", "enter-text"])
@pytest.mark.xfail(strict=True, reason=C2_REASON)
def test_an_action_with_no_criterion_is_refused(recorded, monkeypatch, screen_name, action):
    """Acting on nothing must fail, and must not touch the screen on its way.

    With no ``--find-*`` every enabled node matches, and the first of those is
    the ``<hierarchy>`` root, whose bounds are ``[0,0][0,0]``. The agent gets a
    zero exit status and a message naming a real on-screen element that was
    never touched -- the worst available answer, because it is indistinguishable
    from success.
    """
    screen = ET.fromstring(recorded.text(screen_name))

    run = _run_cli(navigator, ["navigator.py", *action], screen, monkeypatch)

    issued = run.adb.shell_inputs("tap") + run.adb.shell_inputs("text")
    assert run.status != 0, (
        f"navigator {' '.join(action)} with no --find-* reported success "
        f"(exit {run.status}) and said: {run.stdout.strip()!r}"
    )
    assert not issued, f"an action with no criterion still drove the screen: {issued}"


# ---------------------------------------------------------------------------
# The inventory and the register cannot rot.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_name", SCREENS, ids=_SCREEN_IDS)
def test_the_case_inventory_matches_the_recorded_screen(recorded, monkeypatch, screen_name):
    """The static case list is exactly what the recorded screen produces.

    This is what lets the cases above be literals. Both halves are checked:
    the names, against ``screen_mapper --json`` driven through the harness; and
    the rectangles, re-derived from the recorded hierarchy. A re-recorded
    fixture that moves a control, or a mapper that starts reporting a different
    set, fails here rather than quietly making every case above assert against
    a stale rectangle.
    """
    inventory = SCREEN_CONTROLS[screen_name]
    assert (
        len(inventory) >= MINIMUM_NAMED_CONTROLS
    ), f"{screen_name} has only {len(inventory)} cases; the loop tests would be near-vacuous"

    screen = ET.fromstring(recorded.text(screen_name))
    payload = json.loads(
        _run_cli(screen_mapper, ["screen_mapper.py", "--json"], screen, monkeypatch).stdout
    )

    assert _interactive_labels(payload) == [label for label, _ in inventory], (
        f"the names screen_mapper reports for {screen_name} are no longer the "
        f"names this module has cases for. Update the inventory deliberately."
    )

    drifted = {
        label: (rect, _expected_rect(screen, label))
        for label, rect in inventory
        if _expected_rect(screen, label) != rect
    }
    assert not drifted, (
        f"inventory rectangles disagree with the recorded hierarchy "
        f"(name: inventory vs hierarchy): {drifted}"
    )


def test_the_red_register_names_only_cases_that_exist():
    """A stale xfail entry hides a defect that was already fixed.

    Same shape as ``test_fixture_policy.test_the_debt_list_does_not_rot``: the
    frozen set is the copy that fails when it is wrong.
    """
    known = {
        (screen_name, label) for screen_name in SCREENS for label, _ in SCREEN_CONTROLS[screen_name]
    }
    stale = C1_RED - known
    assert not stale, (
        f"{sorted(stale)} are pinned as C1 failures but are no longer names any "
        f"screen report produces. Delete them, or update the inventory."
    )


def test_the_expectation_is_not_read_from_the_tool_under_test(recorded):
    """Guard on this module's own method.

    The resolver has to disagree with navigator somewhere, or it is only
    restating what navigator did. On the Compose fixture it does: the caption
    "Remember me" resolves to the CheckBox, while navigator taps the TextView.
    """
    screen = ET.fromstring(recorded.text(COMPOSE))
    assert _expected_rect(screen, "Remember me") == (33, 754, 159, 880)
    assert _expected_rect(screen, "Dark theme") == (32, 890, 169, 1016)
    assert _expected_rect(screen, "Submit Order") == (32, 217, 375, 343)
