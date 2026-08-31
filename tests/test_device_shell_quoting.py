"""Quoting for arguments that cross into the *device* shell.

R7 / R8 / A16. `build_adb_command` correctly avoids ``shell=True`` on the host,
but that only addresses the host side. ``adb shell a b c`` joins the arguments
into one string and the **device's** ``sh -c`` re-parses it, so every argument
still needs quoting for that shell.

Before this, escaping was ad hoc and incomplete:

  - `keyboard._escape_text` escaped ``\\ " ' $ ` `` and mapped space to ``%s``,
    leaving ``& ; | < > ( ) *`` and newline unescaped.
  - `navigator` escaped only space and ``'``.
  - `model_inspector` and `container` interpolated a package name and a path
    into a ``run-as`` command line with no escaping at all -- reachable with
    values an agent harvested from earlier tool output.

So ``x;id`` ran ``id`` on the device as the app's uid.

The on-device round-trip lives in the ``-m emulator`` lane; these tests pin the
construction, which is where the defect was.
"""

from __future__ import annotations

import pytest

from common.device_utils import quote_for_device_shell

# Metacharacters the device shell acts on. Each of these was previously passed
# through unescaped by at least one call site.
DANGEROUS = ["&", ";", "|", "<", ">", "(", ")", "*", "`", "$", "'", '"', "\n", "\\"]


@pytest.mark.parametrize("char", DANGEROUS)
def test_metacharacters_are_neutralised(char):
    """A quoted argument must not leave the metacharacter shell-active."""
    quoted = quote_for_device_shell(f"a{char}b")
    assert quoted != f"a{char}b", f"{char!r} passed through unquoted"


def test_command_separator_cannot_start_a_second_command():
    """The concrete exploit: `x;id` ran `id` on the device."""
    quoted = quote_for_device_shell("x;id")
    assert not quoted.startswith("x;"), f"still injectable: {quoted}"
    assert "id" in quoted  # the literal text is preserved, just inert


def test_plain_text_is_not_mangled():
    """Ordinary values should stay readable rather than being over-quoted."""
    assert quote_for_device_shell("com.example.app") == "com.example.app"
    assert quote_for_device_shell("databases/app.db") == "databases/app.db"


def test_empty_string_is_quoted_not_dropped():
    """An unquoted empty argument would vanish from the device's argv."""
    assert quote_for_device_shell("") == "''"


# ---------------------------------------------------------------------------
# Call sites must actually use it.
# ---------------------------------------------------------------------------


def test_keyboard_text_is_quoted_before_reaching_the_device(monkeypatch):
    """`input text` args cross the device shell like any other."""
    import keyboard

    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(keyboard.subprocess, "run", _run)
    keyboard.KeyboardSimulator()._input_text("x;id")

    payload = captured["cmd"][-1]
    assert not payload.startswith("x;"), f"unquoted payload reached the device: {payload!r}"


def test_navigator_text_is_quoted_before_reaching_the_device(monkeypatch):
    """navigator escaped only space and a single quote."""
    import navigator

    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(navigator.subprocess, "run", _run)
    monkeypatch.setattr(navigator.time, "sleep", lambda _s: None)
    element = navigator.Element(
        type="EditText",
        text=None,
        content_desc=None,
        resource_id="com.example.app:id/field",
        bounds=(0, 0, 100, 50),
        clickable=True,
        enabled=True,
    )
    nav = navigator.Navigator()
    nav.enter_text(element, "x;id")

    payload = captured["cmd"][-1]
    assert not payload.startswith("x;"), f"unquoted payload reached the device: {payload!r}"


def test_run_as_arguments_are_quoted(monkeypatch):
    """A package or path from earlier tool output must not become a command."""
    import model_inspector

    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(model_inspector.subprocess, "run", _run)
    inspector = model_inspector.LiveInspector()
    inspector._run_as("com.example.app", ["sqlite3", "a;id", ".schema"])

    joined = " ".join(captured["cmd"])
    assert " a;id " not in joined, f"unquoted run-as argument: {joined}"


# ---------------------------------------------------------------------------
# Spaces must still reach `input text` correctly.
# ---------------------------------------------------------------------------


def test_space_handling_is_preserved(monkeypatch):
    """`input text` maps %s to a space; quoting must not break that.

    Kept as-is deliberately: changing the space mechanism at the same time as
    the quoting would make a regression impossible to attribute.
    """
    import keyboard

    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        keyboard.subprocess, "run", lambda cmd, **_k: (captured.update(cmd=cmd), _Result())[1]
    )
    keyboard.KeyboardSimulator()._input_text("hello world")

    payload = captured["cmd"][-1]
    assert "%s" in payload, f"space no longer encoded for `input text`: {payload!r}"
    assert " " not in payload.strip("'\""), "raw space would split into two argv entries"
