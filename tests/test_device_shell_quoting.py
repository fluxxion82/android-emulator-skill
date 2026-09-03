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

import shlex
import subprocess

import pytest

from common import adb_exec
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

    # keyboard reaches adb through adb_exec.run_adb, so the fake goes there.
    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
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

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
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
        adb_exec.subprocess, "run", lambda cmd, **_k: (captured.update(cmd=cmd), _Result())[1]
    )
    keyboard.KeyboardSimulator()._input_text("hello world")

    payload = captured["cmd"][-1]
    assert "%s" in payload, f"space no longer encoded for `input text`: {payload!r}"
    assert " " not in payload.strip("'\""), "raw space would split into two argv entries"


# ---------------------------------------------------------------------------
# C3 / X2 -- the five files that interpolated a value straight into an
# `adb shell` argv.
# ---------------------------------------------------------------------------
#
# These do not assert "the value was handed to quote_for_device_shell", which
# would restate the fix. They re-parse the argv the way the device does and
# assert the value arrives as ONE word with no control operator beside it --
# what the agent experiences, and what was broken.

# The operators `sh` acts on. `?` and `*` only glob, so they are not here.
SHELL_OPERATORS = {"&", "&&", ";", ";;", "|", "||", "(", ")", "<", ">"}


def _device_shell_words(argv: list[str]) -> list[str]:
    """What the device's ``sh -c`` parses out of an ``adb shell`` argv.

    ``adb shell a b c`` joins its tail with spaces and hands the result to the
    shell running on the device, which re-splits it and acts on the operators
    above (``common/device_utils.py``:84-91). ``shlex`` with
    ``punctuation_chars`` models that grammar, so an operator that survived
    quoting shows up here as a word of its own.
    """
    tail = argv[argv.index("shell") + 1 :]
    lexer = shlex.shlex(" ".join(tail), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _assert_single_word(argv: list[str], value: str, allowed: set[str] | None = None) -> None:
    """``value`` reached the device as one word, and no operator came with it.

    ``allowed`` names the operators the *source* spells as literals -- only
    ``app_state_capture``'s ``dumpsys package ... | grep versionName``, whose
    pipe is a deliberate device-side filter. Everything else is an operator
    that arrived inside an interpolated value, which is the defect.
    """
    words = _device_shell_words(argv)
    assert value in words, (
        f"the device shell parsed {words!r} out of {' '.join(argv)!r}; "
        f"{value!r} did not survive as one argument"
    )
    injected = sorted(SHELL_OPERATORS.intersection(words) - (allowed or set()))
    assert not injected, (
        f"{injected} reached the device shell as an operator: {words!r}. "
        f"Everything after the first one runs as a separate command."
    )


class _Captured:
    stdout = ""
    stderr = ""
    returncode = 0


def _capture_adb(monkeypatch, module, stdout: str = "") -> list[list[str]]:
    """Record every argv ``common.adb_exec`` would hand to the host subprocess.

    ``stdout`` is what the fake device answers. It matters for the `am start`
    paths, which now read their verdict out of the report rather than assuming
    silence means success -- so a test that wants those to succeed has to hand
    them a real one.
    """
    seen: list[list[str]] = []

    def _run(cmd, **_kwargs):
        seen.append(list(cmd))
        captured = _Captured()
        captured.stdout = stdout
        return captured

    monkeypatch.setattr(module.adb_exec.subprocess, "run", _run)
    return seen


def test_open_url_sends_a_query_string_as_one_argument(monkeypatch, recorded):
    """`--open-url 'https://x/?a=1&b=2'` opened `?a=1` and backgrounded `am start`.

    The `&` was an operator on the device: the intent received the truncated
    URL, `am start` ran in the background, and `open_url` reported the whole
    URL as opened regardless.
    """
    import app_launcher

    # The recorded `am start -W` report, because `open_url` now reads its
    # verdict from `Status: ok` rather than from the absence of an error.
    seen = _capture_adb(monkeypatch, app_launcher, stdout=recorded.text("am_start_wait_settings"))
    url = "https://example.com/?a=1&b=2"

    success, message = app_launcher.AppLauncher(serial="emulator-5554").open_url(url)

    assert success is True, message
    assert url in message
    _assert_single_word(seen[0], url)


def test_terminate_cannot_append_a_second_command(monkeypatch):
    """A package name harvested from earlier tool output is not trusted input."""
    import app_launcher

    seen = _capture_adb(monkeypatch, app_launcher)

    app_launcher.AppLauncher(serial="emulator-5554").terminate("com.foo;reboot")

    words = _device_shell_words(seen[0])
    _assert_single_word(seen[0], "com.foo;reboot")
    assert "reboot" not in words, f"`reboot` reached the device as a command: {words!r}"


def test_launch_extras_survive_a_space(monkeypatch):
    """`--args note=hello world` reached `am start` as two arguments."""
    import app_launcher

    seen = _capture_adb(monkeypatch, app_launcher)

    app_launcher.AppLauncher(serial="emulator-5554").launch(
        "com.example.app", activity=".MainActivity", extras={"note": "hello world"}
    )

    words = _device_shell_words(seen[0])
    start = words.index("--es")
    assert words[start + 1 : start + 3] == ["note", "hello world"], words


def test_app_state_capture_probes_one_package(monkeypatch):
    """Both `dumpsys package <pkg> | grep versionName` and `pidof <pkg>` carry it.

    The pipe in the first is written as a literal and is the point of the
    command, so it is allowed; the package name must not add a second one.
    """
    import app_state_capture

    seen = _capture_adb(monkeypatch, app_state_capture)

    app_state_capture.AppStateCapture(package="com.foo;id", serial="emulator-5554")._get_app_info()

    carrying = [argv for argv in seen if "com.foo;id" in " ".join(argv)]
    assert len(carrying) == 2, f"expected the package in two probes, saw {seen}"
    for argv in carrying:
        _assert_single_word(argv, "com.foo;id", allowed={"|"} if "dumpsys" in argv else set())


def test_privacy_manager_grants_to_one_package(monkeypatch):
    """`pm grant <pkg> <permission>`, with a package that carries a separator."""
    import privacy_manager

    seen: list[list[str]] = []

    def _run(cmd, **_kwargs):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(privacy_manager.subprocess, "run", _run)

    privacy_manager.PrivacyManager(serial="emulator-5554").grant_permission("com.foo;id", "camera")

    grant = next(argv for argv in seen if "grant" in argv)
    _assert_single_word(grant, "com.foo;id")
    _assert_single_word(grant, "android.permission.CAMERA")


def test_status_bar_mobile_type_cannot_open_a_second_command(monkeypatch):
    """`--mobile-type` is free text on the CLI and lands in a demo broadcast."""
    import status_bar

    seen = _capture_adb(monkeypatch, status_bar)

    success, _ = status_bar.StatusBarController(serial="emulator-5554").set_mobile_data(
        enabled=True, level=3, datatype="lte;id"
    )

    assert success is True
    broadcast = next(argv for argv in seen if "datatype" in argv)
    _assert_single_word(broadcast, "lte;id")


def test_appearance_locale_is_one_setprop_value(monkeypatch):
    """`setprop persist.sys.locale <tag>` must write exactly one value."""
    import appearance

    seen = _capture_adb(monkeypatch, appearance)

    appearance.AppearanceController(serial="emulator-5554").set_locale("fr-FR;id")

    _assert_single_word(seen[0], "fr-FR;id")
