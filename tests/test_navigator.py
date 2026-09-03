"""Device-free tests for navigator feature deltas.

Covers the three curated deltas, with adb and ``time.sleep`` mocked so nothing
touches a real device. navigator reaches adb only through
``adb_exec.run_adb``, so the fake goes under that; patching
``navigator.subprocess`` would stop intercepting and let these tests drive a
real device:

1. ``--find-exact`` performs exact (non-fuzzy) text matching.
2. Each tap is followed by an ``ANDROID_EMU_TAP_SETTLE_MS`` settle delay.
3. ``--list`` caps output at ``ANDROID_EMU_MAX_ELEMENTS`` with an overflow hint.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import navigator
import pytest
from navigator import Element, Navigator

from common import adb_exec, hierarchy


def _fake_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Stand-in for subprocess.CompletedProcess."""

    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


SAMPLE_XML = """
<hierarchy>
  <node class="android.widget.Button" text="Sign In" bounds="[0,0][200,100]"
        clickable="true" enabled="true" />
  <node class="android.widget.Button" text="Sign In Now" bounds="[0,100][200,200]"
        clickable="true" enabled="true" />
  <node class="android.widget.TextView" text="Welcome" bounds="[0,200][200,300]"
        clickable="false" enabled="true" />
</hierarchy>
"""


def _stub_hierarchy(nav: Navigator, monkeypatch, xml: str = SAMPLE_XML) -> None:
    def _get(force_refresh: bool = False) -> ET.Element:
        return _root(xml)

    monkeypatch.setattr(nav, "get_ui_hierarchy", _get)


def _stub_list_elements(monkeypatch, elements: list[Element]) -> None:
    def _list(self, interactive_only: bool = True):
        return elements

    monkeypatch.setattr(Navigator, "list_elements", _list)


def _stub_resolve(monkeypatch, serial: str = "emulator-5554") -> None:
    def _resolve(arg):
        return serial

    monkeypatch.setattr(navigator, "resolve_device_identifier", _resolve)


# --- Delta 1: --find-exact / exact matching --------------------------------


def test_find_exact_matches_only_exact_text(monkeypatch):
    nav = Navigator(serial="emulator-5554")
    _stub_hierarchy(nav, monkeypatch)

    # Exact match must select the element whose text is exactly "Sign In",
    # not the fuzzy-superset "Sign In Now".
    elem = nav.find_element(text="Sign In", fuzzy=False)
    assert elem is not None
    assert elem.text == "Sign In"


def test_fuzzy_match_is_substring(monkeypatch):
    nav = Navigator(serial="emulator-5554")
    _stub_hierarchy(nav, monkeypatch)

    # Fuzzy match on "sign in" (case-insensitive substring) matches the first
    # of both "Sign In" elements.
    elem = nav.find_element(text="sign in", fuzzy=True)
    assert elem is not None
    assert elem.text == "Sign In"


def test_exact_match_no_partial(monkeypatch):
    nav = Navigator(serial="emulator-5554")
    _stub_hierarchy(nav, monkeypatch)

    # "Sign" is a substring but never an exact text -> no exact match.
    assert nav.find_element(text="Sign", fuzzy=False) is None
    # ... while fuzzy still finds it.
    assert nav.find_element(text="Sign", fuzzy=True) is not None


# --- Delta 2: post-tap settle delay ----------------------------------------


def test_tap_at_sleeps_for_settle(monkeypatch):
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.5)
    slept: list[float] = []

    def _sleep(seconds):
        slept.append(seconds)

    def _run(cmd, **kwargs):
        return _fake_result()

    monkeypatch.setattr(navigator.time, "sleep", _sleep)
    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    nav = Navigator(serial="emulator-5554")
    success, _ = nav.tap_at(10, 20)

    assert success is True
    assert slept == [0.5]


def test_tap_at_no_sleep_when_settle_zero(monkeypatch):
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)
    slept: list[float] = []

    def _sleep(seconds):
        slept.append(seconds)

    def _run(cmd, **kwargs):
        return _fake_result()

    monkeypatch.setattr(navigator.time, "sleep", _sleep)
    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    nav = Navigator(serial="emulator-5554")
    success, _ = nav.tap_at(10, 20)

    assert success is True
    assert slept == []


def test_tap_passes_serial_to_adb(monkeypatch):
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)
    captured: list[list[str]] = []
    budgets: list[object] = []

    def _run(cmd, **kwargs):
        captured.append(list(cmd))
        # run_adb enforces the non-zero check itself, so the child is run with
        # check=False; what must survive the move is the time budget.
        budgets.append(kwargs.get("timeout"))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    nav = Navigator(serial="emulator-5554")
    nav.tap_at(10, 20)

    assert captured
    cmd = captured[0]
    assert cmd[0] == "adb"
    assert "-s" in cmd and "emulator-5554" in cmd
    assert cmd[-3:] == ["tap", "10", "20"]
    assert all(b for b in budgets), f"unbounded adb call: {budgets}"


# --- Delta 3: element cap + overflow hint ----------------------------------


def _interactive(n: int) -> list[Element]:
    return [
        Element(
            type="Button",
            text=f"Item {i}",
            content_desc=None,
            resource_id=None,
            bounds=(0, i * 10, 100, i * 10 + 10),
            clickable=True,
            enabled=True,
        )
        for i in range(n)
    ]


def test_list_truncates_and_hints(monkeypatch, capsys):
    monkeypatch.setattr(navigator, "MAX_ELEMENTS_LISTED", 3)
    _stub_resolve(monkeypatch)
    _stub_list_elements(monkeypatch, _interactive(10))
    monkeypatch.setattr(navigator.sys, "argv", ["navigator.py", "--list"])

    with pytest.raises(SystemExit) as exc:
        navigator.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    # Total count is reported even though only MAX are shown.
    assert "Interactive elements (10):" in out
    # Only 3 numbered rows are printed (indices 0,1,2).
    assert "  2. " in out
    assert "  3. " not in out
    # Overflow hint mentions the remaining count and the env var.
    assert "... and 7 more" in out
    assert "ANDROID_EMU_MAX_ELEMENTS" in out


def test_list_no_hint_when_under_cap(monkeypatch, capsys):
    monkeypatch.setattr(navigator, "MAX_ELEMENTS_LISTED", 25)
    _stub_resolve(monkeypatch)
    _stub_list_elements(monkeypatch, _interactive(2))
    monkeypatch.setattr(navigator.sys, "argv", ["navigator.py", "--list"])

    with pytest.raises(SystemExit):
        navigator.main()

    out = capsys.readouterr().out
    assert "Interactive elements (2):" in out
    assert "more" not in out


def test_list_json_reports_total_and_truncation(monkeypatch, capsys):
    monkeypatch.setattr(navigator, "MAX_ELEMENTS_LISTED", 3)
    _stub_resolve(monkeypatch)
    _stub_list_elements(monkeypatch, _interactive(10))
    monkeypatch.setattr(navigator.sys, "argv", ["navigator.py", "--list", "--json"])

    with pytest.raises(SystemExit):
        navigator.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 10
    assert payload["shown"] == 3
    assert payload["truncated"] == 7
    assert len(payload["elements"]) == 3


# --- An element the skill cannot locate is not acted on --------------------


COMPOSE = "uiautomator_compose_default.xml"
SETTINGS = "uiautomator_settings_top.xml"

# The recorded search bar: interactive, and the one control on that screen with
# a resource id an agent would name. Selected by id, not by bounds -- the
# CardView wrapping it reports the same rectangle and comes first.
SEARCH_BAR_ID = "com.android.settings:id/search_action_bar"


def _screen_with_unreadable_bounds(xml: str) -> ET.Element:
    """A recorded screen with ONE attribute corrupted: the search bar's bounds.

    Derived rather than recorded, and deliberately so. uiautomator on API 35
    clips every node's rectangle to the display -- the recording unit tried
    eight recipes for an off-screen node (a half-row swipe, a mid-fling dump, a
    half-pulled shade, the task switcher mid-animation) and got min_left=0 /
    max_right=1080 / max_bottom=2424 every time -- so there is no recorded dump
    on this API level in which `bounds` cannot be read, and inventing a whole
    one would be exactly the imagined-tool-output bug this suite exists to
    prevent. What IS real is that `bounds` is a string this skill parses, and
    that a value it cannot parse must not become a tap. One attribute of a real
    dump is changed; every other byte is the device's.

    The lookup is by resource id, because an element whose bounds do not parse
    is no longer eligible as a control (`hierarchy.is_interactive`) and so
    cannot be reached by name -- `--find-id` is the route that still gets an
    agent to it, and therefore the route on which the refusal has to hold.
    """
    screen = ET.fromstring(xml)
    search_bar = next(node for node in screen.iter() if node.get("resource-id") == SEARCH_BAR_ID)
    assert search_bar.get("bounds") == "[42,605][1038,742]", "the fixture moved; re-derive"
    search_bar.set("bounds", "[42,605]")  # truncated: no second corner
    return screen


def test_an_element_whose_bounds_will_not_parse_is_refused_not_tapped(
    monkeypatch, recorded, capsys
):
    """C5: the fallback for unreadable bounds used to be a tap at (0, 0).

    `_parse_bounds` returned `(0, 0, 0, 0)` for anything its grammar missed, and
    the centre of that rectangle is the top-left pixel of the screen -- a real,
    tappable point, in whatever happens to occupy the corner. The refusal has to
    name the element and a way forward, because "cannot act" that does not say
    what to do next is only marginally better than tapping the wrong thing.
    """
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)
    issued: list[list[str]] = []

    def _run(cmd, **kwargs):
        issued.append(list(cmd))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    monkeypatch.setattr(
        navigator,
        "capture_hierarchy",
        lambda serial=None, **kwargs: _screen_with_unreadable_bounds(recorded.text(SETTINGS)),
    )
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        navigator.sys, "argv", ["navigator.py", "--find-id", "search_action_bar", "--tap"]
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code != 0, "acting on an element of unknown position reported success"
    assert not [cmd for cmd in issued if "tap" in cmd], f"a tap was issued anyway: {issued}"

    out = capsys.readouterr().out
    assert "no usable bounds" in out, out
    assert "--tap-at" in out, f"the refusal does not say what to do instead: {out}"


def test_a_name_that_is_only_a_passive_label_is_refused(monkeypatch, recorded, capsys):
    """INC1-04's safety catch: ownership ignores what a caption says, so this must not.

    "Fixture Screen" is the recorded Compose screen's heading -- a TextView with
    no interactive ancestor and no control beside it. Resolution finds no owner,
    and the match is then a label an agent could tap forever with no effect. The
    refusal says which name, and where to look for a real one.
    """
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)
    issued: list[list[str]] = []

    def _run(cmd, **kwargs):
        issued.append(list(cmd))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    monkeypatch.setattr(
        navigator,
        "capture_hierarchy",
        lambda serial=None, **kwargs: ET.fromstring(recorded.text(COMPOSE)),
    )
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        navigator.sys, "argv", ["navigator.py", "--find-text", "Fixture Screen", "--tap"]
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code != 0
    assert not [cmd for cmd in issued if "tap" in cmd], f"a passive label was tapped: {issued}"
    out = capsys.readouterr().out
    assert "passive label" in out, out
    assert "screen_mapper.py" in out, f"the refusal does not say where to look: {out}"


def test_a_caption_inside_a_control_with_an_id_still_resolves(monkeypatch, recorded):
    """The case that made "the owner must answer to the name" wrong.

    On the recorded Settings screen "Search settings" is a TextView inside
    `com.android.settings:id/search_action_bar`. The enclosing ViewGroup has a
    resource id, so it recovers no caption of its own and never "answered to"
    the name -- with the name test in place, the tap went back to the passive
    TextView. Ownership is structural now, so it resolves to the ViewGroup.
    """
    nav = Navigator()
    screen = ET.fromstring(recorded.text(SETTINGS))
    monkeypatch.setattr(nav, "get_ui_hierarchy", lambda force_refresh=False: screen)

    for fuzzy in (True, False):
        found = nav.find_element(text="Search settings", fuzzy=fuzzy)
        assert found is not None
        assert found.bounds == (42, 605, 1038, 742), f"fuzzy={fuzzy}: {found}"
        assert found.label == "search_action_bar", f"fuzzy={fuzzy}: {found.label}"


# --- A scroll container is not what a caption inside it names ---------------


# The recorded Battery row: a clickable LinearLayout wrapping its two captions.
BATTERY_ROW_BOUNDS = "[0,1708][1080,1939]"

# The scrollable container that encloses every row on that screen. It is the
# first INTERACTIVE ancestor of any caption once its row stops being clickable,
# and its centre is the middle of the screen.
SCROLL_CONTAINER_BOUNDS = "[0,784][1080,2361]"


def _settings_with_an_unclickable_row(xml: str) -> ET.Element:
    """The recorded Settings screen with ONE attribute cleared: the row's `clickable`.

    Derived, and the reason is the same as for the unreadable-bounds case: this
    is a layout, not a device state, so no dump of this screen can be recorded
    in which the Battery row is not clickable. What it stands in for is real and
    common -- a list whose rows are not themselves clickable, where the only
    interactive ancestor a caption has is the scrolling container around it.
    Every other byte is the device's.
    """
    screen = ET.fromstring(xml)
    row = next(node for node in screen.iter() if node.get("bounds") == BATTERY_ROW_BOUNDS)
    assert row.get("clickable") == "true", "the fixture moved; re-derive from the recording"
    row.set("clickable", "false")
    return screen


def test_a_caption_resolves_to_its_row_when_the_row_is_tappable(monkeypatch, recorded):
    """The control case, unmodified: the caption belongs to the row, and lands on it."""
    nav = Navigator()
    screen = ET.fromstring(recorded.text(SETTINGS))
    monkeypatch.setattr(nav, "get_ui_hierarchy", lambda force_refresh=False: screen)

    found = nav.find_element(text="Battery")

    assert found is not None
    assert found.bounds == (0, 1708, 1080, 1939), f"resolved to {found}"


def test_a_caption_owned_only_by_a_scroll_container_is_refused(monkeypatch, recorded, capsys):
    """A ScrollView is interactive, but it is not what a caption inside it names.

    `is_interactive` counts `scrollable`, and a scroll container encloses nearly
    the whole screen -- so with the row's `clickable` gone it becomes the first
    interactive ancestor of the "Battery" caption. Resolving to it taps
    (540, 1572), the middle of the screen, and reports `Tapped: ... "Battery"`:
    a success naming a control that was never touched, which is the same shape
    as C2's tap at (0, 0).

    An owner must be tappable (`is_actionable`: clickable / long-clickable /
    checkable), so the container is passed over, nothing else owns the caption,
    and the lookup refuses.
    """
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)
    issued: list[list[str]] = []

    def _run(cmd, **kwargs):
        issued.append(list(cmd))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    monkeypatch.setattr(
        navigator,
        "capture_hierarchy",
        lambda serial=None, **kwargs: _settings_with_an_unclickable_row(recorded.text(SETTINGS)),
    )
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        navigator.sys, "argv", ["navigator.py", "--find-text", "Battery", "--tap", "--json"]
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code != 0, "a caption owned by nothing tappable reported success"
    assert not [cmd for cmd in issued if "tap" in cmd], f"the screen was tapped anyway: {issued}"

    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload, f"the failure contract moved: {payload}"
    assert "passive label" in payload["error"], payload["error"]
    assert "screen_mapper.py" in payload["error"], "the refusal does not say where to look"


def test_the_scroll_container_is_still_interactive_and_still_findable(monkeypatch, recorded):
    """The narrowing is for ownership only; a container is still something to act on.

    It is enumerated (`is_interactive` keeps `scrollable`) and it is still
    reachable by its own name, because an agent scrolls it. What it cannot be is
    the answer to a caption that merely sits inside it.
    """
    screen = _settings_with_an_unclickable_row(recorded.text(SETTINGS))
    container = next(
        node for node in screen.iter() if node.get("bounds") == SCROLL_CONTAINER_BOUNDS
    )
    assert hierarchy.is_interactive(container), "the container stopped being enumerable"
    assert not hierarchy.is_actionable(container), "a scroll container is not a tap target"

    nav = Navigator()
    monkeypatch.setattr(nav, "get_ui_hierarchy", lambda force_refresh=False: screen)
    found = nav.find_element(text="main_content_scrollable_container")
    assert found is not None, "the container is no longer findable by its own name"
    assert found.bounds == (0, 784, 1080, 2361)


def test_the_compose_sibling_captions_are_unaffected(monkeypatch, recorded):
    """Checkbox and Switch resolve through the row rule, which is unchanged.

    Both controls are `checkable`, so they are tappable owners; the narrowing
    touches neither. Restated here because they are the cases the sibling rule
    exists for, and a change to ownership is exactly what would break them.
    """
    nav = Navigator()
    screen = ET.fromstring(recorded.text(COMPOSE))
    monkeypatch.setattr(nav, "get_ui_hierarchy", lambda force_refresh=False: screen)

    assert nav.find_element(text="Remember me").bounds == (33, 754, 159, 880)
    assert nav.find_element(text="Dark theme").bounds == (32, 890, 169, 1016)
    assert nav.find_element(text="Company logo").bounds == (33, 1028, 159, 1154)


# --- Device errors reach the agent with a remedy, not a traceback ----------


def test_unknown_serial_raises_rather_than_reporting_a_tap(monkeypatch, recorded_anywhere):
    """The tap never reached a device, so it must not look like success."""
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)

    def _run(cmd, **kwargs):
        return _fake_result(returncode=1, stderr=recorded_anywhere("adb_device_not_found"))

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    nav = Navigator(serial="no-such-serial-xyz")
    with pytest.raises(adb_exec.DeviceNotFoundError):
        nav.tap_at(10, 20)


def test_main_reports_an_unknown_serial_without_a_traceback(monkeypatch, capsys, recorded_anywhere):
    """Exit 1 with the remedy on stderr; a traceback would bury it."""
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)

    def _run(cmd, **kwargs):
        return _fake_result(returncode=1, stderr=recorded_anywhere("adb_device_not_found"))

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    _stub_resolve(monkeypatch, serial="no-such-serial-xyz")
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--serial", "no-such-serial-xyz", "--tap-at", "10,20"],
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "no-such-serial-xyz" in err
    assert "adb devices" in err, "the error does not say how to see what is attached"


def test_main_reports_multiple_devices_when_listing(monkeypatch, capsys):
    """--list dumps the hierarchy, so it hits the same ambiguity."""

    def _run(cmd, **kwargs):
        return _fake_result(returncode=1, stderr="adb: more than one device/emulator\n")

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    _stub_resolve(monkeypatch, serial=None)
    monkeypatch.setattr(navigator.sys, "argv", ["navigator.py", "--list"])

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 1
    assert "--serial" in capsys.readouterr().err


def test_typed_text_is_still_quoted_and_space_encoded(monkeypatch):
    """R7/R8: quoting for the device shell survives the move to run_adb."""
    captured: list[list[str]] = []

    def _run(cmd, **kwargs):
        captured.append(list(cmd))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    nav = Navigator(serial="emulator-5554")
    nav.type_text("x;id now")

    payload = captured[0][-1]
    assert not payload.startswith("x;"), f"unquoted payload reached the device: {payload!r}"
    assert "%s" in payload, f"space no longer encoded for `input text`: {payload!r}"
