"""Device-free tests for navigator's scroll-into-view search.

The defect these pin, measured on the emulator's Settings app::

    navigator.py --find-text "Notifications"  -> Found (visible)
    navigator.py --find-text "About phone"    -> Not found: text='About phone'

The second item exists; it is one scroll below the fold. "Not found" reads as
"does not exist", so an agent stops there. Everything below is about keeping
those two answers distinguishable.

Every screen served here is a recorded dump under
``tests/fixtures/recorded/emulator-api35/`` -- never an inline literal, per the
rule in CLAUDE.md. Four of them are successive states of ONE real Settings
list, and the traversal was checked on the device:

===========================  =============================================
uiautomator_settings_top     fresh launch. 'Notifications' visible,
                             'About emulated device' not on screen.
uiautomator_settings_half    after a half-height swipe. The list moved a
                             long way -- 'Network & internet' left the top,
                             'Accessibility' arrived -- and the target is
                             STILL not there.
uiautomator_settings_scrolled  after a full swipe from there (verified: the
                             bytes match). The target is now on screen and
                             this short list is at its end.
uiautomator_settings_scrolled_again  one more swipe at the end. Byte-identical
                             to the previous dump; that identity is the only
                             signal that scrolling further is pointless,
                             because the swipe still succeeds and exits 0.
===========================  =============================================

``uiautomator_dialer_keypad`` is the fifth: a real screen with no
``scrollable="true"`` node at all. It had to be hunted for -- Settings home,
date, Wi-Fi and input-method pages and the launcher all report at least one
scrollable container -- which is exactly why "this screen does not scroll" is
asserted against a recording rather than assumed.

adb is faked underneath ``common.adb_exec``, so no swipe reaches a device and
the tests can assert on the argv that *would* have been sent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import gesture
import navigator
import pytest
from navigator import Navigator

from common import adb_exec

TOP = "uiautomator_settings_top"
HALF = "uiautomator_settings_half"
SCROLLED = "uiautomator_settings_scrolled"
SCROLLED_AGAIN = "uiautomator_settings_scrolled_again"
NO_SCROLL = "uiautomator_dialer_keypad"

# On this profile the label is 'About emulated device'; the same row is 'About
# phone' on a handset. Either way it is the last row of the list.
TARGET = "About emulated device"
VISIBLE = "Notifications"
ABSENT = "Wormhole Calibration"


def _fake_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Stand-in for subprocess.CompletedProcess."""

    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _harness(monkeypatch, recorded, screens: list[str]):
    """A Navigator that is served ``screens`` in order, and the adb argv it sent.

    The hierarchy sequence is what makes a scroll observable without a device:
    each capture returns the next recorded screen, exactly as a real scroll
    would. Running past the end of the sequence is a failure, not a silent
    repeat -- a test that quietly re-served the last screen would "prove" the
    early exit by accident.
    """
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        if "size" in cmd:
            return _fake_result(stdout=recorded.text("wm_size_physical"))
        return _fake_result()

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(navigator.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(gesture.time, "sleep", lambda seconds: None)

    pending = [ET.fromstring(recorded.text(name)) for name in screens]

    def fake_capture(serial=None, **kwargs):
        if not pending:
            pytest.fail(
                f"the search captured more than the {len(screens)} screens it was "
                f"given; it did not stop when the screen stopped changing"
            )
        return pending.pop(0)

    monkeypatch.setattr(navigator, "capture_hierarchy", fake_capture)
    return Navigator(serial="emulator-5554"), commands


def _swipes(commands: list[list[str]]) -> list[list[str]]:
    return [c for c in commands if "swipe" in c]


def _bounds_string(xml: str, text: str) -> str:
    """The ``bounds`` attribute of the node carrying ``text`` in a dump."""
    for node in ET.fromstring(xml).iter():
        if node.get("text") == text:
            return node.get("bounds", "")
    raise AssertionError(f"{text!r} is not in this recorded screen")


def _stub_resolve(monkeypatch, serial: str = "emulator-5554") -> None:
    monkeypatch.setattr(navigator, "resolve_device_identifier", lambda arg: serial)


# ---------------------------------------------------------------------------
# The corpus itself. These guard the guards: if the recorded screens stopped
# carrying the facts below, every test under them would still pass while
# proving nothing.
# ---------------------------------------------------------------------------


def test_recorded_screens_are_successive_states_of_one_list(recorded):
    """The premise: the target is absent from the first screen and present later."""
    top = recorded.text(TOP)
    half = recorded.text(HALF)
    scrolled = recorded.text(SCROLLED)

    assert VISIBLE in top and TARGET not in top, "the top screen no longer sets up the defect"
    assert TARGET in scrolled, "the scrolled screen no longer contains the target"
    assert VISIBLE not in scrolled, "the list did not actually move between these dumps"
    # The middle screen moved a long way and still does not contain the target,
    # which is why a miss must not end the search.
    assert TARGET not in half and "Network" not in half and VISIBLE in half


def test_recorded_end_of_list_dumps_are_identical(recorded):
    """Scrolling a list that is at its end changes nothing -- and still exits 0.

    This byte-identity is the entire basis for the early exit. If a re-record
    ever makes these differ, the early exit is unfalsifiable and the search
    would swipe its full budget at a stationary screen.
    """
    assert recorded.text(SCROLLED) == recorded.text(SCROLLED_AGAIN)


def test_recorded_settings_screens_have_scrollable_containers(recorded):
    """Two nested ScrollViews, so 'the one scrollable container' is a wrong model."""
    assert recorded.text(TOP).count('scrollable="true"') == 2


def test_recorded_dialer_screen_has_no_scrollable_container(recorded):
    """A screen that genuinely cannot scroll, recorded rather than imagined."""
    assert 'scrollable="true"' not in recorded.text(NO_SCROLL)


# ---------------------------------------------------------------------------
# The search itself.
# ---------------------------------------------------------------------------


def test_visible_element_is_found_without_scrolling(monkeypatch, recorded):
    """Requirement: search the current screen before anything moves."""
    nav, commands = _harness(monkeypatch, recorded, [TOP])

    result = nav.find_element_scrolling(text=VISIBLE)

    assert result.element is not None
    assert result.element.text == VISIBLE
    assert result.scrolls == 0
    assert result.screens_searched == 1
    assert not _swipes(commands), "scrolled for an element that was already on screen"


def test_element_below_the_fold_is_found_after_scrolling(monkeypatch, recorded):
    """The defect, inverted: the item is reached instead of reported missing."""
    nav, commands = _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED])

    result = nav.find_element_scrolling(text=TARGET)

    assert result.element is not None
    assert result.scrolls == 2
    assert result.screens_searched == 3
    assert len(_swipes(commands)) == 2
    assert "after 2 scrolls" in result.detail


def test_found_element_carries_coordinates_from_the_final_screen(monkeypatch, recorded):
    """A tap has to land where the element ended up, not where it started.

    Bounds taken from the pre-scroll dump would aim at a row that has since
    moved -- a confident tap on the wrong setting.
    """
    nav, _commands = _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED])

    result = nav.find_element_scrolling(text=TARGET)

    x1, y1, x2, y2 = result.element.bounds
    assert f"[{x1},{y1}][{x2},{y2}]" == _bounds_string(recorded.text(SCROLLED), TARGET)


def test_scroll_search_drives_the_content_down_not_the_finger(monkeypatch, recorded):
    """It must go through GestureSimulator.scroll, which owns the inversion.

    ``swipe`` names what the finger does and ``scroll`` names what the content
    does. A search that called ``swipe("down")`` would drag the list back
    toward its top and never reach anything (defect fixed in 6edba2d).
    """
    nav, commands = _harness(monkeypatch, recorded, [TOP, SCROLLED])

    nav.find_element_scrolling(text=TARGET, direction="down")

    swipe = _swipes(commands)[0]
    coords = swipe[swipe.index("swipe") + 1 :]
    start_y, end_y = int(coords[1]), int(coords[3])
    assert start_y > end_y, (
        f"scrolling down swiped the finger from y={start_y} to y={end_y}, which "
        f"drags the list back toward its top"
    )


def test_search_stops_when_the_screen_stops_changing(monkeypatch, recorded):
    """The bound that matters: a list at its end keeps reporting success.

    ``input swipe`` on a stationary list exits 0 and says nothing. Without the
    unchanged-screen check the search would spend its whole budget re-reading
    one screen and then claim to have searched ten.
    """
    nav, commands = _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED, SCROLLED_AGAIN])

    result = nav.find_element_scrolling(text=ABSENT, max_scrolls=10)

    assert result.element is None
    assert result.stopped_unchanged is True
    assert result.hit_limit is False
    assert result.scrolls == 3
    assert result.screens_searched == 4
    assert len(_swipes(commands)) == 3, "kept swiping after the list stopped moving"
    assert "scrolled to the end" in result.detail
    assert "stopped changing" in result.detail


def test_search_gives_up_at_the_limit_and_says_the_screen_was_still_moving(monkeypatch, recorded):
    """'Gave up' and 'absent' are different answers and must read differently."""
    nav, commands = _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED])

    result = nav.find_element_scrolling(text=ABSENT, max_scrolls=2)

    assert result.element is None
    assert result.hit_limit is True
    assert result.stopped_unchanged is False
    assert result.scrolls == 2
    assert result.screens_searched == 3
    assert len(_swipes(commands)) == 2
    assert "2-scroll limit" in result.detail
    assert "--max-scrolls" in result.detail


def test_screen_without_a_scrollable_container_is_not_swiped(monkeypatch, recorded):
    """Requirement 5: report that the screen does not scroll, do not swipe blindly."""
    nav, commands = _harness(monkeypatch, recorded, [NO_SCROLL])

    result = nav.find_element_scrolling(text=ABSENT)

    assert result.element is None
    assert result.scrollable is False
    assert result.scrolls == 0
    assert not _swipes(commands), "swiped a screen that cannot scroll"
    assert not commands, "queried the device at all for a screen it could not scroll"
    assert "nothing on it scrolls" in result.detail


def test_screen_signature_ignores_chrome_outside_the_scrolling_region(recorded):
    """Two dumps of the same list state compare equal; different states do not.

    The comparison is deliberately scoped to the scrollable subtrees. A window
    dump can contain something that changes on its own -- a status-bar clock is
    the usual one -- and comparing whole documents would report a stuck list as
    "still moving" and never take the early exit.
    """
    end = ET.fromstring(recorded.text(SCROLLED))
    end_again = ET.fromstring(recorded.text(SCROLLED_AGAIN))
    moved = ET.fromstring(recorded.text(HALF))

    assert Navigator._screen_signature(end) == Navigator._screen_signature(end_again)
    assert Navigator._screen_signature(end) != Navigator._screen_signature(moved)


def test_a_failed_scroll_is_reported_rather_than_counted(monkeypatch, recorded):
    """A swipe that never ran must not look like a screen that was searched."""
    nav, _commands = _harness(monkeypatch, recorded, [TOP])
    monkeypatch.setattr(
        gesture.GestureSimulator,
        "scroll",
        lambda self, direction, count=1, duration_ms=300: (False, "Swipe failed: boom"),
    )

    result = nav.find_element_scrolling(text=TARGET)

    assert result.element is None
    assert result.scrolls == 0
    assert result.screens_searched == 1
    assert "the scroll failed" in result.detail


def test_a_screen_that_will_not_hold_still_is_an_error_not_a_traceback(
    monkeypatch, recorded, capsys
):
    """Measured live: tapping into a Settings sub-page and immediately dumping
    raised ``HierarchyError`` ("could not get idle state") straight through
    ``main()`` as a traceback, burying the remedy the message carries. A scroll
    search dumps once per scroll, each dump landing right after a fling, so
    this path went from rare to routine.
    """
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP])

    def _refuse(serial=None, **kwargs):
        raise navigator.HierarchyError(
            "uiautomator could not get an idle state; disable animations"
        )

    monkeypatch.setattr(navigator, "capture_hierarchy", _refuse)
    monkeypatch.setattr(
        navigator.sys, "argv", ["navigator.py", "--find-text", TARGET, "--scroll-to-find"]
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "idle state" in err, "the remedy the error carries did not reach the agent"


# ---------------------------------------------------------------------------
# The CLI, which is what an agent actually reads.
# ---------------------------------------------------------------------------


def test_cli_not_found_on_a_scrolling_screen_names_the_flag(monkeypatch, recorded, capsys):
    """Default mode does not scroll -- but it must not read as 'does not exist'."""
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP])
    monkeypatch.setattr(navigator.sys, "argv", ["navigator.py", "--find-text", TARGET])

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Not found" in out
    assert "this screen scrolls" in out
    assert "--scroll-to-find" in out


def test_cli_default_does_not_scroll(monkeypatch, recorded, capsys):
    """Scrolling has side effects, so a plain lookup must leave the app alone."""
    _stub_resolve(monkeypatch)
    _nav, commands = _harness(monkeypatch, recorded, [TOP])
    monkeypatch.setattr(navigator.sys, "argv", ["navigator.py", "--find-text", TARGET])

    with pytest.raises(SystemExit):
        navigator.main()

    assert not _swipes(commands), "a lookup without --scroll-to-find moved the app"


def test_cli_scroll_search_finds_below_the_fold(monkeypatch, recorded, capsys):
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED])
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--find-text", TARGET, "--scroll-to-find", "--json"],
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["element"]["label"] == TARGET
    assert payload["search"]["scrolls"] == 2
    assert payload["search"]["screens_searched"] == 3


def test_cli_absent_text_says_what_was_tried(monkeypatch, recorded, capsys):
    """Requirement 4: 'searched N screens, scrolled to the end' -- not bare 'Not found'."""
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED, SCROLLED_AGAIN])
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--find-text", ABSENT, "--scroll-to-find"],
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "searched 4 screens" in out
    assert "scrolled to the end" in out


def test_cli_json_carries_the_search_detail_when_nothing_matched(monkeypatch, recorded, capsys):
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED, SCROLLED_AGAIN])
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--find-text", ABSENT, "--scroll-to-find", "--json"],
    )

    with pytest.raises(SystemExit):
        navigator.main()

    payload = json.loads(capsys.readouterr().out)
    search = payload["search"]
    assert search["screens_searched"] == 4
    assert search["scrolls"] == 3
    assert search["stopped_unchanged"] is True
    assert search["hit_scroll_limit"] is False
    assert "scrolled to the end" in search["detail"]


def test_cli_rejects_a_scroll_budget_of_zero(monkeypatch, recorded, capsys):
    """Better a usage error than a search that reports a 0-scroll limit."""
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP])
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--find-text", TARGET, "--scroll-to-find", "--max-scrolls", "0"],
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 2
    assert "--max-scrolls" in capsys.readouterr().err


def test_cli_reports_a_screen_that_does_not_scroll(monkeypatch, recorded, capsys):
    _stub_resolve(monkeypatch)
    _nav, commands = _harness(monkeypatch, recorded, [NO_SCROLL])
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--find-text", ABSENT, "--scroll-to-find"],
    )

    with pytest.raises(SystemExit) as exc:
        navigator.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "nothing on it scrolls" in out
    assert not _swipes(commands)


def test_cli_verbose_reports_each_screen(monkeypatch, recorded, capsys):
    """--verbose must actually say something, on every mode that takes it."""
    _stub_resolve(monkeypatch)
    _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED])
    monkeypatch.setattr(
        navigator.sys,
        "argv",
        ["navigator.py", "--find-text", TARGET, "--scroll-to-find", "--verbose"],
    )

    with pytest.raises(SystemExit):
        navigator.main()

    err = capsys.readouterr().err
    assert "screen 1: no match" in err
    assert "scrolled down (2); screen 3: match" in err


def test_cli_find_id_also_scrolls(monkeypatch, recorded):
    """The flag belongs to the finders, not to --find-text alone."""
    _stub_resolve(monkeypatch)
    nav, _commands = _harness(monkeypatch, recorded, [TOP, HALF, SCROLLED])

    result = nav.find_element_scrolling(resource_id="title", text=TARGET)

    assert result.element is not None
    assert result.element.resource_id == "title"


# ---------------------------------------------------------------------------
# Live device. Semantic floors only: did the agent get a usable answer.
# ---------------------------------------------------------------------------


def _navigator_cli(adb_path: str, serial: str, args: list[str]) -> subprocess.CompletedProcess:
    script = (
        Path(__file__).resolve().parents[1]
        / "android-emulator-skill"
        / "skills"
        / "android-emulator-skill"
        / "scripts"
        / "navigator.py"
    )
    subprocess.run(
        [adb_path, "-s", serial, "shell", "am", "force-stop", "com.android.settings"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    subprocess.run(
        [adb_path, "-s", serial, "shell", "am", "start", "-a", "android.settings.SETTINGS"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return subprocess.run(
        [sys.executable, str(script), "--serial", serial, *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.emulator
def test_live_scroll_search_reaches_an_item_below_the_fold(adb, emulator_only_device):
    """'About …' is the last row of Settings on every Android build we have seen."""
    result = _navigator_cli(
        adb, emulator_only_device, ["--find-text", "About", "--scroll-to-find", "--json"]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["search"]["screens_searched"] >= 1
    assert payload["search"]["scrolls"] <= 10


@pytest.mark.emulator
def test_live_absent_text_reports_the_search_rather_than_a_bare_miss(adb, emulator_only_device):
    result = _navigator_cli(
        adb,
        emulator_only_device,
        ["--find-text", "Wormhole Calibration", "--scroll-to-find", "--json"],
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "searched" in payload["message"]
    detail = payload["search"]["detail"]
    assert "scrolled to the end" in detail or "nothing on it scrolls" in detail
