"""Device-free tests for emulator_shutdown.

These mock the module's ``subprocess`` and ``get_connected_devices`` so no
emulator/adb is needed. They assert the pure arg->command mapping for shutdown
(`adb emu kill`, and the failure report when it is refused), the refusal of a
physical-device serial, AVD-name->serial resolution, the default-verify /
--no-verify CLI wiring, and the env-configurable tunables.
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


# A serial with no ``emulator-`` prefix, which is the only thing
# ``get_connected_devices`` derives ``type`` from. Synthetic on purpose: a real
# handset serial must not be committed.
PHYSICAL_SERIAL = "R3CX90ABCDEF"


def _devices_listing_a_phone(recorded) -> str:
    """Real ``adb devices -l`` output with one row retargeted to a handset.

    No profile has recorded ``adb devices -l`` with a physical device attached,
    and recording one would publish a developer's phone serial (CLAUDE.md
    forbids committing personal data from the recorder). So the *shape* is
    ground truth -- header, state column and trailing ``product:``/``model:``/
    ``transport_id:`` fields come from ``adb_devices_multiple`` verbatim -- and
    only the serial field of the last row is substituted, keeping its column
    width. That field is precisely what the classification under test reads.
    """
    lines = [line for line in recorded.lines("adb_devices_multiple") if line.strip()]
    header, rows = lines[0], lines[1:]
    serial, _, rest = rows[-1].partition(" ")
    return "\n".join([header, *rows[:-1], PHYSICAL_SERIAL.ljust(len(serial)) + rest]) + "\n"


def _real_device_query(monkeypatch, recorded) -> None:
    """Let the real ``get_connected_devices`` parse recorded adb output.

    Stubbing the query's *return value* would let the test agree with a wrong
    idea of how ``type`` is computed. Patching one level lower keeps the
    production parser -- and its ``serial.startswith("emulator-")`` rule -- in
    the path being asserted.
    """
    from common import device_utils

    listing = _devices_listing_a_phone(recorded)
    monkeypatch.setattr(
        device_utils, "run_adb", lambda *_a, **_k: _FakeResult(returncode=0, stdout=listing)
    )


def _recording_run(calls: list[list[str]], result: _FakeResult | None = None):
    """A ``subprocess.run`` double that records every argv it is handed."""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return result if result is not None else _FakeResult(returncode=0)

    return fake_run


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


def test_shutdown_refuses_a_physical_device_serial(monkeypatch, recorded):
    """L1: ``--serial <phone>`` used to power the attached phone off.

    ``adb emu kill`` is an emulator-console command; a handset has no console,
    so it always failed there -- and the failure branch ran
    ``adb shell reboot -p``. ``--all`` and ``--name`` filter on ``type``;
    ``--serial`` was the one path that did not, so it powered off whatever was
    plugged in. The refusal must land BEFORE any adb command is issued, which
    is asserted on the recorded argv list rather than on nothing having raised.
    """
    _real_device_query(monkeypatch, recorded)

    calls: list[list[str]] = []
    monkeypatch.setattr(emulator_shutdown.subprocess, "run", _recording_run(calls))

    shutdown = emulator_shutdown.EmulatorShutdown(PHYSICAL_SERIAL)
    success, message = shutdown.shutdown(verify=False)

    assert success is False
    assert PHYSICAL_SERIAL in message, "the refusal does not name the serial"
    assert "physical device" in message.lower(), "the refusal does not name the type"
    # The remedy: this script stops emulators only, so disconnect the handset
    # or target an emulator serial.
    assert "disconnect" in message.lower(), "the refusal does not name a remedy"
    assert "emulator-" in message, "the remedy does not show the serial shape to pass"

    assert calls == [], f"an adb command was issued before the refusal: {calls}"
    assert not [
        cmd for cmd in calls if "shell" in cmd
    ], f"a device shell command reached a physical device: {calls}"


def test_cli_exits_non_zero_when_refusing_a_physical_device(monkeypatch, capsys, recorded):
    """The refusal has to reach the caller as a failure, not a printed note."""
    _real_device_query(monkeypatch, recorded)

    calls: list[list[str]] = []
    monkeypatch.setattr(emulator_shutdown.subprocess, "run", _recording_run(calls))
    monkeypatch.setattr(
        emulator_shutdown.sys, "argv", ["emulator_shutdown.py", "--serial", PHYSICAL_SERIAL]
    )

    with pytest.raises(SystemExit) as exc:
        emulator_shutdown.main()

    assert exc.value.code == 1
    assert calls == [], f"an adb command was issued before the refusal: {calls}"
    assert PHYSICAL_SERIAL in capsys.readouterr().out


def test_emu_kill_failure_on_an_emulator_is_reported_not_worked_around(monkeypatch, recorded):
    """A refused console kill is a failure with adb's own stderr -- nothing else.

    There is deliberately no second attempt. The old fallback (`reboot -p`)
    existed for "the rare device that rejects the console kill", and the rare
    device that rejects it is a phone.
    """
    _real_device_query(monkeypatch, recorded)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        emulator_shutdown.subprocess,
        "run",
        _recording_run(calls, _FakeResult(returncode=1, stderr="console refused")),
    )

    shutdown = emulator_shutdown.EmulatorShutdown("emulator-5554")
    success, message = shutdown.shutdown(verify=False)

    assert success is False
    assert "console refused" in message, "adb's own stderr is not surfaced"
    assert calls == [
        ["adb", "-s", "emulator-5554", "emu", "kill"]
    ], f"a second command was issued after the console kill failed: {calls}"


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
