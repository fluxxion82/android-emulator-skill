"""A command that prints agent text back on stderr must not read as a lost device.

``adb_exec.run_adb`` types a device error from any stderr line that starts with
``adb: `` / ``error: `` and names a device problem -- and an ``adb shell``
command's own stderr arrives on that same pipe (``shell -T`` separates stdout
from stderr, not adb's words from the command's). So a command that echoes the
text an agent handed it can print a line that reads as adb's, and the script
reports the device unavailable (exit 3) while it answers.

Which callers do that was recorded, not guessed: ``tests/record_fixtures.py``'s
``*_echo`` fixtures (profile emulator-api33) run each ``run_adb`` caller whose
argv carries agent text with that text holding a newline and a forged
``adb: device 'aes-spoof' not found`` line, and the manifest says which stream
brought it back. Three print it back on stderr where the classifier reads it:

- ``pm grant`` / ``pm revoke`` (privacy_manager): ``Unknown permission: <name>``
  (pm_grant_permission_echo, pm_revoke_permission_echo);
- ``setprop persist.sys.locale`` (appearance --locale): ``Failed to set property
  ... to '<value>'`` (setprop_locale_echo);
- ``adb install <path>`` (app_launcher --install): adb's OWN ``adb: failed to stat
  <path>`` line carries the path, so no newline is needed
  (adb_install_missing_apk_echo_stderr).

Those three ask ``run_adb`` to confirm (``confirm_device_errors=True``): one
``adb get-state`` before a device error is raised, dropped when it answers
``device``. Each spec below runs the real CLI against a fake adb replaying the
recording: the echo is the command's failure, not exit 3, while the device
answers -- and a genuine device error (recorded) is still exit 3. The probe
cannot tell a spoof from a real loss followed by a recovery before it; that
tradeoff is pinned in test_adb_exec.py, and is why nothing else opts in.
"""

from __future__ import annotations

import json

import pytest
import record_fixtures as rf
from conftest import RECORDED_ROOT, RecordedFixtures
from fake_toolchain import FakeToolchain, Rule

API33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")

DEVICE_UNAVAILABLE = 3
FAILURE = 1


def _exit_status(name: str) -> int:
    """The exit status the recording carries, from its manifest entry."""
    for entry in API33.manifest["fixtures"]:
        if entry["name"] == name:
            return entry["exit_status"]
    pytest.fail(f"{name} is not in the emulator-api33 manifest")


def _serial(recorded) -> str:
    """The one serial the recorded single-device listing names."""
    rows = [line.split() for line in recorded.lines("adb_devices_single")[1:] if line.strip()]
    assert len(rows) == 1, rows
    return rows[0][0]


class Caller:
    """One opted-in caller: its argv, the adb call it makes, and its recording."""

    def __init__(self, script, argv, match, stderr, stdout="", fixture=None, says=()):
        self.script = script
        self.argv = argv
        self.match = match
        self.stderr = stderr
        self.stdout = stdout
        self.fixture = fixture or stderr
        self.says = says

    def __repr__(self) -> str:
        return self.script


CALLERS = [
    Caller(
        "privacy_manager.py",
        ("--grant", f"android.permission.{rf.ECHO_TEXT}", "--package", "com.android.settings"),
        ("pm", "grant"),
        "pm_grant_permission_echo",
        says=("Unknown permission",),
    ),
    Caller(
        "privacy_manager.py",
        ("--revoke", f"android.permission.{rf.ECHO_TEXT}", "--package", "com.android.settings"),
        ("pm", "revoke"),
        "pm_revoke_permission_echo",
        says=("Unknown permission",),
    ),
    Caller(
        "appearance.py",
        ("--locale", rf.ECHO_TEXT),
        ("setprop", "persist.sys.locale"),
        "setprop_locale_echo",
        says=("Failed to set property",),
    ),
    Caller(
        "app_launcher.py",
        ("--install", rf.ECHO_APK),
        ("install",),
        "adb_install_missing_apk_echo_stderr",
        stdout="adb_install_missing_apk_echo",
        fixture="adb_install_missing_apk_echo",
        says=("failed to stat",),
    ),
]


def _chain(tmp_path, recorded, caller: Caller, *, genuine: bool) -> FakeToolchain:
    """A device that echoes (the recording), or one that is really gone."""
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    if genuine:
        command = Rule(caller.match, stderr=recorded.text("adb_shell_device_not_found"), exit=1)
        state = Rule(("get-state",), stderr=recorded.text("adb_device_not_found"), exit=1)
    else:
        command = Rule(
            caller.match,
            stdout=API33.text(caller.stdout) if caller.stdout else "",
            stderr=API33.text(caller.stderr),
            exit=_exit_status(caller.fixture),
        )
        state = Rule(("get-state",), stdout=recorded.text("adb_get_state"))
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            command,
            state,
        ],
    )
    return chain


def _run(chain: FakeToolchain, caller: Caller, serial: str, *extra: str):
    return chain.run(caller.script, *caller.argv, "--serial", serial, *extra)


def test_the_recordings_carry_the_forged_line_where_the_classifier_reads_it():
    """The premise, from the recordings: each opted-in call's stderr starts a line forged."""
    for caller in CALLERS:
        lines = API33.text(caller.stderr).split("\n")
        if caller.script == "app_launcher.py":
            # adb's own line: starts `adb: ` and carries the path's words.
            assert lines[0].startswith("adb: failed to stat"), lines
            assert "not found" in lines[0] and "device" in lines[0], lines
        else:
            assert any(line.startswith(rf.ECHO_FORGED) for line in lines), (caller, lines)


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: f"{c.script} {c.argv[0]}")
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_an_echo_on_stderr_is_the_commands_failure_not_exit_3(
    caller: Caller, json_mode: bool, tmp_path, recorded
):
    """The device answers ``get-state``: the echo is the command's failure, exit 1."""
    chain = _chain(tmp_path, recorded, caller, genuine=False)
    serial = _serial(recorded)
    result = _run(chain, caller, serial, *(("--json",) if json_mode else ()))
    shown = f"\nstdout: {result.stdout[-800:]!r}\nstderr: {result.stderr[-800:]!r}"

    assert "Traceback" not in result.stdout + result.stderr, shown
    assert result.returncode == FAILURE, f"exit {result.returncode}, not {FAILURE}:{shown}"
    said = result.stdout + result.stderr
    assert any(words in said for words in caller.says), f"the command's reason is lost:{shown}"
    assert "is not attached" not in said, f"still reported as a lost device:{shown}"
    assert ["-s", serial, "get-state"] in chain.calls(
        "adb"
    ), f"the device error was never put to a probe: {chain.calls('adb')}"
    if json_mode:
        payload = json.loads(result.stdout)
        assert payload["success"] is False, payload


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: f"{c.script} {c.argv[0]}")
def test_a_genuine_device_error_is_still_exit_3(caller: Caller, tmp_path, recorded):
    """adb's own device-not-found, and a probe that cannot find it either: exit 3."""
    chain = _chain(tmp_path, recorded, caller, genuine=True)
    result = _run(chain, caller, _serial(recorded), "--json")
    shown = f"\nstdout: {result.stdout[-800:]!r}\nstderr: {result.stderr[-800:]!r}"
    assert "Traceback" not in result.stdout + result.stderr, shown
    assert result.returncode == DEVICE_UNAVAILABLE, shown
    payload = json.loads(result.stdout)
    assert payload["success"] is False, payload
