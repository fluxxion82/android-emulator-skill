"""Commands the code invented, and the honest replacements.

S12  Three of five status-bar setters issued `cmd statusbar battery-level`,
     `wifi-enabled`, `mobile-datatype` and friends. Recorded `cmd statusbar`
     help on API 33 and 35 shows none of them exist -- they are SystemUI
     *demo-mode broadcast extras*, not statusbar subcommands. The file already
     contained the correct mechanism (`_demo_broadcast`, used by `override()`);
     only the CLI dispatch routed to the invented path.

S11  `show_keyboard()` broadcast `INPUT_METHOD_CHANGED`, which does not show the
     IME and needs system privileges, then returned (True, "Keyboard shown")
     regardless. A no-op that reports success is worse than an absent feature.

Also: `press_button("recent_apps")` was documented while KEYCODE_APP_SWITCH was
missing from the key map, so the call always errored.
"""

from __future__ import annotations

import ast
from pathlib import Path

import keyboard
import pytest
import status_bar


def _demo_extras(cmd: list[str]) -> dict[str, str]:
    """Pull the ``-e key value`` pairs out of an am broadcast command."""
    extras = {}
    for index, token in enumerate(cmd):
        if token == "-e" and index + 2 < len(cmd):
            extras[cmd[index + 1]] = cmd[index + 2]
    return extras


@pytest.fixture
def capture_adb(monkeypatch):
    """Record every adb command status_bar issues."""
    commands: list[list[str]] = []

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **_kwargs):
        commands.append(cmd)
        return _Result()

    monkeypatch.setattr(status_bar.subprocess, "run", _run)
    return commands


# ---------------------------------------------------------------------------
# S12 — the invented subcommands must be gone.
# ---------------------------------------------------------------------------

INVENTED = [
    "battery-level",
    "battery-charging",
    "wifi-enabled",
    "wifi-level",
    "mobile-enabled",
    "mobile-level",
    "mobile-datatype",
]


def _statusbar_call_args(source: str) -> list[list[str]]:
    """Constant string args of every build_adb_command call naming `statusbar`.

    Parsed, not grepped: the module docstrings now explain *why* these
    subcommands were wrong, and a substring search cannot tell an explanation
    apart from a call.
    """
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "build_adb_command":
            continue
        args = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if "statusbar" in args:
            calls.append(args)
    return calls


@pytest.mark.parametrize("subcommand", INVENTED)
def test_no_invented_statusbar_subcommand_is_issued(subcommand):
    """None of these appear in `cmd statusbar` help on any recorded profile."""
    source = Path(status_bar.__file__).read_text(encoding="utf-8")
    for args in _statusbar_call_args(source):
        assert subcommand not in args, (
            f"status_bar still issues `cmd statusbar {subcommand}`, which does "
            f"not exist: {args}"
        )


def test_battery_uses_the_demo_mode_broadcast(capture_adb):
    """The mechanism that actually works, and was already in the file."""
    ok, _message = status_bar.StatusBarController().set_battery(42, charging=True)
    assert ok

    broadcasts = [c for c in capture_adb if "com.android.systemui.demo" in c]
    assert broadcasts, f"no demo-mode broadcast issued: {capture_adb}"

    extras = _demo_extras(broadcasts[-1])
    assert extras.get("command") == "battery"
    assert extras.get("level") == "42"


def test_wifi_uses_the_demo_mode_broadcast(capture_adb):
    ok, _message = status_bar.StatusBarController().set_wifi(True, 3)
    assert ok

    broadcasts = [c for c in capture_adb if "com.android.systemui.demo" in c]
    extras = _demo_extras(broadcasts[-1])
    assert extras.get("command") == "network"
    assert extras.get("wifi") == "show"
    assert extras.get("level") == "3"


def test_mobile_uses_the_demo_mode_broadcast(capture_adb):
    ok, _message = status_bar.StatusBarController().set_mobile_data(True, 4, "lte")
    assert ok

    broadcasts = [c for c in capture_adb if "com.android.systemui.demo" in c]
    extras = _demo_extras(broadcasts[-1])
    assert extras.get("command") == "network"
    assert extras.get("mobile") == "show"
    assert extras.get("datatype") == "lte"


def test_setters_enter_demo_mode_first(capture_adb):
    """Demo mode must be allowed and entered or the broadcasts do nothing."""
    status_bar.StatusBarController().set_battery(50)

    joined = [" ".join(c) for c in capture_adb]
    assert any("sysui_demo_allowed" in c for c in joined), "demo mode never allowed"
    assert any("command enter" in c for c in joined), "demo mode never entered"


# ---------------------------------------------------------------------------
# S11 — a no-op that reports success is worse than no feature.
# ---------------------------------------------------------------------------


def test_show_keyboard_is_gone():
    """It broadcast an intent that does not show the IME, then returned True."""
    assert not hasattr(keyboard.KeyboardSimulator, "show_keyboard"), (
        "show_keyboard still exists; it cannot work from adb and always " "reported success"
    )


def test_show_keyboard_flag_is_gone():
    """The CLI must not advertise it either."""
    source = Path(keyboard.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    flags = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "--show-keyboard" not in flags


# ---------------------------------------------------------------------------
# Documented buttons must exist.
# ---------------------------------------------------------------------------


def test_every_documented_button_is_mapped():
    """`recent_apps` was in the docstring but absent from the key map."""
    simulator = keyboard.KeyboardSimulator()
    _ok, message = simulator.press_button("recent_apps")
    assert "Unknown" not in message, f"documented button not mapped: {message}"
