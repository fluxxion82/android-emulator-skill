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
rectangle is computed *here*, from the recorded hierarchy, and never read off
navigator's own ``Tapped:`` line: a test that trusts the tool's report of where
it tapped is structurally unable to see the tool tapping the wrong thing. That
is the same mistake one layer up -- ``test_compose_visibility.py`` asserts the
label is *produced*, never that it is *usable*.

Two recorded screens, because they fail differently. The Compose fixture carries
unlabelled controls whose captions are row-adjacent siblings; the Settings
fixture is View-based, with genuine ``text`` attributes and resource-id labels.

Everything runs in-process against ``tests/fixtures/recorded/``:
``capture_hierarchy`` is stubbed to the recorded XML and every adb call is
recorded rather than issued, so this belongs to the required mocked check and
touches no device. The emulator lane repeats the same walk live.

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
from pathlib import Path
from types import SimpleNamespace

import navigator
import pytest
import screen_mapper

from common import adb_exec

RECORDED_DIR = Path(__file__).resolve().parent / "fixtures" / "recorded" / "emulator-api35"

COMPOSE = "uiautomator_compose_default.xml"
SETTINGS = "uiautomator_settings_top.xml"
SCREENS = (COMPOSE, SETTINGS)

# A screen an agent can act on names at least this many of its controls. The
# plan's floor for C4, applied to both toolkits: the Compose fixture has seven
# interactive controls and the Settings fixture eleven.
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
# The defect register.
# ---------------------------------------------------------------------------
#
# Every pair below is a control name `screen_mapper` prints that
# `navigator --find-text <name> --tap` does not land on today. Measured against
# the recorded XML by driving both CLIs, not predicted:
#
#   Compose, tap issued but outside the control
#     "Remember me"  -> (302, 816), the caption TextView; the CheckBox is
#                       [33,754][159,880], so the tap misses it by 143px.
#     "Dark theme"   -> (282, 953); the Switch is [32,890][169,1016].
#     "Company logo" -> (221, 1090); the icon button is [33,1028][159,1154].
#   Compose, nothing found at all (exit 1, no argv issued)
#     "Order #4821 2 items Ships tomorrow", "List item 0 List item 1 List item 2"
#   Settings, nothing found at all
#     the three resource-id captions, and all seven row captions, none of which
#     exists as any single node's `text` or `content-desc`.
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


def _refuse_adb(*args, **kwargs):
    """Stand-in for `run_adb` while a hierarchy is analysed at collection time."""
    raise AssertionError(f"run_adb reached a device during collection: {args!r} {kwargs!r}")


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


def _screen(name: str) -> ET.Element:
    """A recorded screen, for parametrisation at collection time.

    Test bodies take the same file through the ``recorded`` fixture; a
    parametrisation cannot, because it is built before fixtures exist.
    """
    return ET.fromstring((RECORDED_DIR / name).read_text(encoding="utf-8"))


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
    """The rectangle a tap for ``label`` has to land in.

    Resolved from the hierarchy, never from navigator's ``Tapped:`` output.
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
# The labels the agent is handed.
# ---------------------------------------------------------------------------


def _mapper_analysis(screen: ET.Element) -> dict:
    """``screen_mapper --json``'s payload for one recorded screen.

    ``analyze_tree`` asks the device for the focused activity, which at
    collection time would run adb against whatever is plugged in. The refusal
    goes in first; ``_detect_screen_name`` swallows it and leaves the screen
    name unset, which is all this needs.
    """
    real_run_adb = adb_exec.run_adb
    adb_exec.run_adb = _refuse_adb
    try:
        return screen_mapper.ScreenMapper().analyze_tree(screen)
    finally:
        adb_exec.run_adb = real_run_adb


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


def _seed_labels(screen_name: str) -> list[str]:
    """The names an agent would pick a target from.

    Seeded from ``--json``'s ``elements_by_type`` because of C4: today the
    default text report names nothing at all, so seeding from the documented
    path would parametrise over an empty list and prove nothing. Once C4 is
    fixed this seed becomes ``_names_printed(<default report>)`` -- the report
    Quick Start step 2 actually produces -- and this comment goes away.
    """
    return _interactive_labels(_mapper_analysis(_screen(screen_name)))


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
    """One case per (screen, label the report printed)."""
    params = []
    for screen_name in SCREENS:
        for label in _seed_labels(screen_name):
            marks = (
                [pytest.mark.xfail(strict=True, reason=C1_REASON)]
                if (screen_name, label) in C1_RED
                else []
            )
            params.append(
                pytest.param(
                    screen_name,
                    label,
                    marks=marks,
                    id=f"{_screen_id(screen_name)}-{_slug(label)}",
                )
            )
    return params


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


@pytest.mark.parametrize(("screen_name", "label"), _c1_params())
def test_a_reported_name_taps_the_control_it_names(recorded, monkeypatch, screen_name, label):
    """The loop, closed: a name the screen report produced goes back in as a tap.

    The expected rectangle comes from the hierarchy, so a tap on the caption
    beside a checkbox fails here even though navigator reports it as a success
    naming the right thing.
    """
    screen = ET.fromstring(recorded.text(screen_name))
    expected = _expected_rect(screen, label)

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


@pytest.mark.parametrize("screen_name", SCREENS, ids=[_screen_id(name) for name in SCREENS])
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
# The register cannot rot, and the cases cannot be vacuous.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_name", SCREENS, ids=[_screen_id(name) for name in SCREENS])
def test_every_screen_seeds_enough_labels_to_be_worth_asserting(screen_name):
    """A parametrisation over an empty list passes and proves nothing."""
    labels = _seed_labels(screen_name)
    assert len(labels) >= MINIMUM_NAMED_CONTROLS, (
        f"{screen_name} seeds only {len(labels)} labels ({labels}); the loop "
        f"cases above would be near-vacuous"
    )


def test_the_red_register_names_only_cases_that_exist():
    """A stale xfail entry hides a defect that was already fixed.

    Same shape as ``test_fixture_policy.test_the_debt_list_does_not_rot``: the
    frozen set is the copy that fails when it is wrong.
    """
    seeded = {
        (screen_name, label) for screen_name in SCREENS for label in _seed_labels(screen_name)
    }
    stale = C1_RED - seeded
    assert not stale, (
        f"{sorted(stale)} are pinned as C1 failures but are no longer labels "
        f"any screen report produces. Delete them, or re-record the fixture "
        f"they came from."
    )


def test_the_expectation_is_not_read_from_the_tool_under_test():
    """Guard on this module's own method.

    The resolver has to disagree with navigator somewhere, or it is only
    restating what navigator did. On the Compose fixture it does: the caption
    "Remember me" resolves to the CheckBox, while navigator taps the TextView.
    """
    screen = _screen(COMPOSE)
    assert _expected_rect(screen, "Remember me") == (33, 754, 159, 880)
    assert _expected_rect(screen, "Dark theme") == (32, 890, 169, 1016)
    assert _expected_rect(screen, "Submit Order") == (32, 217, 375, 343)
