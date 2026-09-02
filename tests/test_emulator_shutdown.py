"""Device-free tests for emulator_shutdown.

These mock the module's ``subprocess`` and ``get_connected_devices`` so no
emulator/adb is needed. They assert the pure arg->command mapping for shutdown
(`adb emu kill` + fallback + stderr capture), AVD-name->serial resolution, the
default-verify / --no-verify CLI wiring, and the env-configurable tunables.
"""

from __future__ import annotations

import importlib

import emulator_shutdown
import pytest

from common import adb_exec


class _FakeResult:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _connected(monkeypatch, devices):
    monkeypatch.setattr(emulator_shutdown, "get_connected_devices", lambda: devices)


def test_default_tunables():
    # Defaults match the documented contract.
    assert emulator_shutdown.DEFAULT_SHUTDOWN_TIMEOUT == 30
    assert emulator_shutdown.POLL_INTERVAL_SECONDS == 0.5


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_SHUTDOWN_TIMEOUT", "12")
    monkeypatch.setenv("ANDROID_EMU_SHUTDOWN_POLL_INTERVAL", "0.25")
    reloaded = importlib.reload(emulator_shutdown)
    try:
        assert reloaded.DEFAULT_SHUTDOWN_TIMEOUT == 12
        assert reloaded.POLL_INTERVAL_SECONDS == 0.25
    finally:
        monkeypatch.delenv("ANDROID_EMU_SHUTDOWN_TIMEOUT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_SHUTDOWN_POLL_INTERVAL", raising=False)
        importlib.reload(emulator_shutdown)


def test_shutdown_uses_emu_kill_command(monkeypatch):
    _connected(monkeypatch, [{"serial": "emulator-5554", "state": "device", "type": "emulator"}])

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(emulator_shutdown.subprocess, "run", fake_run)

    shutdown = emulator_shutdown.EmulatorShutdown("emulator-5554")
    success, _message = shutdown.shutdown(verify=False)

    assert success is True
    # First (and only) command is the emulator-native console kill.
    assert calls == [["adb", "-s", "emulator-5554", "emu", "kill"]]


def test_shutdown_falls_back_to_reboot_power_off(monkeypatch):
    _connected(monkeypatch, [{"serial": "emulator-5554", "state": "device", "type": "emulator"}])

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        # emu kill fails, reboot -p succeeds.
        if cmd[-2:] == ["emu", "kill"]:
            return _FakeResult(returncode=1, stderr="console refused")
        return _FakeResult(returncode=0)

    monkeypatch.setattr(emulator_shutdown.subprocess, "run", fake_run)

    shutdown = emulator_shutdown.EmulatorShutdown("emulator-5554")
    success, _message = shutdown.shutdown(verify=False)

    assert success is True
    assert calls == [
        ["adb", "-s", "emulator-5554", "emu", "kill"],
        ["adb", "-s", "emulator-5554", "shell", "reboot", "-p"],
    ]


def test_shutdown_failure_captures_adb_stderr(monkeypatch):
    _connected(monkeypatch, [{"serial": "emulator-5554", "state": "device", "type": "emulator"}])

    def fake_run(cmd, **_kwargs):
        if cmd[-2:] == ["emu", "kill"]:
            return _FakeResult(returncode=1, stderr="kill refused")
        return _FakeResult(returncode=1, stderr="device offline")

    monkeypatch.setattr(emulator_shutdown.subprocess, "run", fake_run)

    shutdown = emulator_shutdown.EmulatorShutdown("emulator-5554")
    success, message = shutdown.shutdown(verify=False)

    assert success is False
    # Surfaces the fallback path's real adb stderr.
    assert "device offline" in message


def test_resolve_serial_by_avd_name_matches_running_emulator(monkeypatch):
    _connected(
        monkeypatch,
        [
            {"serial": "emulator-5554", "state": "device", "type": "emulator"},
            {"serial": "emulator-5556", "state": "device", "type": "emulator"},
        ],
    )

    def fake_run(cmd, **_kwargs):
        # cmd: ["adb", "-s", <serial>, "emu", "avd", "name"]
        serial = cmd[2]
        names = {"emulator-5554": "Pixel_5_API_33", "emulator-5556": "Pixel_7_API_34"}
        return _FakeResult(returncode=0, stdout=f"{names[serial]}\nOK\n")

    monkeypatch.setattr(emulator_shutdown.subprocess, "run", fake_run)

    assert emulator_shutdown.resolve_serial_by_avd_name("Pixel_7_API_34") == "emulator-5556"
    assert emulator_shutdown.resolve_serial_by_avd_name("No_Such_AVD") is None


def test_get_avd_name_skips_ok_status_line(monkeypatch):
    monkeypatch.setattr(
        emulator_shutdown.subprocess,
        "run",
        lambda *_a, **_k: _FakeResult(returncode=0, stdout="\nPixel_5_API_33\nOK\n"),
    )
    assert emulator_shutdown.get_avd_name_for_serial("emulator-5554") == "Pixel_5_API_33"


def test_get_avd_name_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(
        emulator_shutdown.subprocess,
        "run",
        lambda *_a, **_k: _FakeResult(returncode=1, stderr="no console"),
    )
    assert emulator_shutdown.get_avd_name_for_serial("emulator-5554") is None


@pytest.mark.parametrize(
    "argv, expected_verify",
    [
        (["--serial", "emulator-5554"], True),  # default: verify on
        (["--serial", "emulator-5554", "--verify"], True),
        (["--serial", "emulator-5554", "--no-verify"], False),
    ],
)
def test_cli_verify_default_and_opt_out(monkeypatch, argv, expected_verify):
    captured = {}

    def fake_shutdown(self, verify=True, timeout_seconds=30):
        captured["verify"] = verify
        return True, "ok"

    monkeypatch.setattr(emulator_shutdown.EmulatorShutdown, "shutdown", fake_shutdown)
    monkeypatch.setattr(emulator_shutdown.sys, "argv", ["emulator_shutdown.py", *argv])

    with pytest.raises(SystemExit) as exc:
        emulator_shutdown.main()

    assert exc.value.code == 0
    assert captured["verify"] is expected_verify


@pytest.mark.parametrize(
    "argv",
    [
        ["--all"],
        ["--name", "Pixel_9"],
        ["--serial", "emulator-5554"],
        ["--all", "--json"],
    ],
    ids=["all", "by-name", "by-serial", "all-json"],
)
def test_cli_reports_an_adb_error_without_a_traceback(monkeypatch, capsys, argv):
    """At the CLI boundary the agent gets the remedy, not a stack trace.

    All four device-query call sites in this module are deliberately unguarded
    and reach ``main``; this covers each path that reaches one.
    """

    def _raise():
        raise adb_exec.MultipleDevicesError(
            "More than one device is attached, so adb could not choose one. "
            "Pass --serial with one of: emulator-5554, emulator-5556."
        )

    monkeypatch.setattr(emulator_shutdown, "get_connected_devices", _raise)
    monkeypatch.setattr(emulator_shutdown.sys, "argv", ["emulator_shutdown.py", *argv])

    with pytest.raises(SystemExit) as exc:
        emulator_shutdown.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "Traceback" not in captured.err
    assert "--serial" in captured.err, "no remedy named"


def test_cli_surfaces_a_missing_adb_rather_than_an_oserror(monkeypatch, capsys):
    """The original defect: adb off PATH escaped as a raw OSError traceback.

    Driven through the real ``run_adb`` rather than a stubbed device query, so
    it pins the whole chain: OSError -> AdbNotInstalledError -> CLI remedy.
    """

    def _no_adb(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "adb")

    monkeypatch.setattr(adb_exec.subprocess, "run", _no_adb)
    monkeypatch.setattr(emulator_shutdown.sys, "argv", ["emulator_shutdown.py", "--all"])

    with pytest.raises(SystemExit) as exc:
        emulator_shutdown.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    # Assert the type's own remedy reaches the boundary intact, not its current
    # wording -- the wording is adb_exec's to change.
    with pytest.raises(adb_exec.AdbNotInstalledError) as raised:
        adb_exec.run_adb("devices", None, "-l")
    assert captured.err == f"Error: {raised.value}\n"
