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

from common import adb_exec


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

# The recorded CheckBox, whose caption "Remember me" is its next sibling.
CHECKBOX_BOUNDS = "[33,754][159,880]"


def _screen_with_unreadable_bounds(xml: str) -> ET.Element:
    """A recorded screen with ONE attribute corrupted: the CheckBox's bounds.

    Derived rather than recorded, and deliberately so. uiautomator on API 35
    clips every node's rectangle to the display -- the recording unit tried eight
    recipes for an off-screen node (a half-row swipe, a mid-fling dump, a
    half-pulled shade, the task switcher mid-animation) and got min_left=0 /
    max_right=1080 / max_bottom=2424 every time -- so there is no recorded dump
    on this API level in which `bounds` cannot be read, and inventing a whole
    one would be exactly the imagined-tool-output bug this suite exists to
    prevent. What IS real is that `bounds` is a string this skill parses, and
    that a value it cannot parse must not become a tap. One attribute of a real
    dump is changed; every other byte is the device's.
    """
    screen = ET.fromstring(xml)
    checkbox = next(node for node in screen.iter() if node.get("bounds") == CHECKBOX_BOUNDS)
    checkbox.set("bounds", "[33,754]")  # truncated: no second corner
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
        lambda serial=None, **kwargs: _screen_with_unreadable_bounds(recorded.text(COMPOSE)),
    )
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        navigator.sys, "argv", ["navigator.py", "--find-text", "Remember me", "--tap"]
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code != 0, "acting on an element of unknown position reported success"
    assert not [cmd for cmd in issued if "tap" in cmd], f"a tap was issued anyway: {issued}"

    out = capsys.readouterr().out
    assert "no usable bounds" in out, out
    assert "--tap-at" in out, f"the refusal does not say what to do instead: {out}"


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
