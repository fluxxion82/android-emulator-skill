"""Device-free tests for navigator feature deltas.

Covers the three curated deltas, with adb/subprocess and ``time.sleep`` mocked
so nothing touches a real device:

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
        return None

    monkeypatch.setattr(navigator.time, "sleep", _sleep)
    monkeypatch.setattr(navigator.subprocess, "run", _run)

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
        return None

    monkeypatch.setattr(navigator.time, "sleep", _sleep)
    monkeypatch.setattr(navigator.subprocess, "run", _run)

    nav = Navigator(serial="emulator-5554")
    success, _ = nav.tap_at(10, 20)

    assert success is True
    assert slept == []


def test_tap_passes_serial_to_adb(monkeypatch):
    monkeypatch.setattr(navigator, "TAP_SETTLE_SECONDS", 0.0)
    captured: list[list[str]] = []

    def _run(cmd, **kwargs):
        captured.append(cmd)
        assert kwargs.get("check") is True

    monkeypatch.setattr(navigator.subprocess, "run", _run)

    nav = Navigator(serial="emulator-5554")
    nav.tap_at(10, 20)

    assert captured
    cmd = captured[0]
    assert cmd[0] == "adb"
    assert "-s" in cmd and "emulator-5554" in cmd
    assert cmd[-3:] == ["tap", "10", "20"]


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
