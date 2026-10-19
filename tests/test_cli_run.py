"""``common.cli.run`` -- the one failure shape, exercised in-process.

Each test hands ``run`` a small ``main`` and reads what reached the process
boundary: the exit status, stdout and stderr. The errors that come from adb are
built the way adb_exec builds them -- from recorded adb stderr, or by running
``run_adb`` with no adb on PATH -- so the remedy is checked against the messages
the skill really produces, not against sentences written here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import anr_watcher
import pytest

from common import adb_exec, cli
from common.device_utils import resolve_device_identifier
from common.hierarchy import HierarchyError
from common.sdk_tools import missing_emulator_error

KEYS = {"success", "error", "remedy"}


def _raising(error: BaseException):
    def main(argv):
        raise error

    return main


def _run(main, argv, capsys) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        cli.run(main, argv)
    captured = capsys.readouterr()
    code = exc.value.code
    return (0 if code is None else code), captured.out, captured.err


# ---------------------------------------------------------------------------
# What main returns, and what it raises on purpose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("returned", "status"), [(None, 0), (0, 0), (1, 1), (4, 4)])
def test_the_exit_status_is_what_main_returns(returned, status, capsys):
    code, out, err = _run(lambda argv: returned, [], capsys)
    assert code == status
    assert out == err == ""


def test_main_is_handed_the_argv(capsys):
    seen = []
    _run(lambda argv: seen.append(argv), ["--serial", "emulator-5554"], capsys)
    assert seen == [["--serial", "emulator-5554"]]


def test_argv_defaults_to_the_command_line(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr("sys.argv", ["prog.py", "--json", "--list"])
    with pytest.raises(SystemExit):
        cli.run(lambda argv: seen.append(argv))
    assert seen == [["--json", "--list"]]


@pytest.mark.parametrize("status", [0, 1, 2])
def test_system_exit_passes_through_untouched(status, capsys):
    """A script's own sys.exit -- success, its own failure, usage -- is not ours."""
    code, out, err = _run(_raising(SystemExit(status)), ["--json"], capsys)
    assert code == status
    assert out == err == "", "the boundary added output to an exit it does not own"


def test_an_argparse_usage_error_keeps_argparses_exit_2(capsys):
    def main(argv):
        parser = argparse.ArgumentParser(prog="script.py")
        parser.add_argument("--count", type=int)
        parser.parse_args(argv)

    code, out, err = _run(main, ["--count", "many"], capsys)
    assert code == 2
    assert out == ""
    assert "invalid int value" in err


def test_keyboard_interrupt_exits_130_and_prints_nothing_on_stdout(capsys):
    code, out, err = _run(_raising(KeyboardInterrupt()), ["--json"], capsys)
    assert code == 130
    assert out == ""
    assert err.strip() == "Interrupted."


# ---------------------------------------------------------------------------
# The typed family: one shape, and the exit status that says which kind
# ---------------------------------------------------------------------------

TYPED = [
    HierarchyError("uiautomator could not get an idle state after 3 attempts."),
    RuntimeError("the session store is unreadable"),
    FileNotFoundError(2, "No such file or directory", "missing.png"),
    subprocess.TimeoutExpired(["adb", "shell", "true"], 30),
    adb_exec.AdbCommandError("`adb shell pm grant x y` exited 255: SecurityException"),
    missing_emulator_error(),
]


@pytest.mark.parametrize("error", TYPED, ids=lambda e: type(e).__name__)
def test_json_mode_puts_the_shape_and_nothing_else_on_stdout(error, capsys):
    code, out, err = _run(_raising(error), ["--list", "--json"], capsys)

    assert code == 1
    payload = json.loads(out)  # the whole of stdout parses: nothing before or after
    # One line, so a streaming mode's refusal is its last line and parses alone.
    assert out.count("\n") == 1 and out.endswith("\n")
    assert set(payload) == KEYS
    assert payload["success"] is False
    assert payload["error"]
    assert payload["remedy"]
    assert err == "", "a JSON caller's failure leaked onto stderr as well"
    assert "Traceback" not in out + err


@pytest.mark.parametrize("error", TYPED, ids=lambda e: type(e).__name__)
def test_text_mode_is_exactly_two_lines_on_stderr(error, capsys):
    code, out, err = _run(_raising(error), ["--list"], capsys)

    assert code == 1
    assert out == ""
    lines = err.splitlines()
    assert len(lines) == 2, f"expected Error/Remedy, got {lines}"
    assert lines[0].startswith("Error: ")
    assert lines[1].startswith("Remedy: ")


def test_a_multi_line_message_still_reports_two_lines(monkeypatch, capsys):
    """resolve_device_identifier lists the attached devices one per line."""
    monkeypatch.setattr(
        "common.device_utils.get_connected_devices",
        lambda: [{"serial": "emulator-5554", "state": "device", "type": "emulator"}],
    )

    def main(argv):
        resolve_device_identifier("no-such-serial")

    code, _, err = _run(main, [], capsys)
    assert code == 3
    assert len(err.splitlines()) == 2
    assert "no-such-serial" in err and "emulator-5554" in err


def test_device_resolution_kept_its_message_and_gained_a_type(monkeypatch):
    """str(error) is what every existing caller prints; only the class is new.

    The expected text is the message this function raised before the boundary
    existed, written out so a rewording fails here rather than in an agent's
    retry prompt.
    """
    monkeypatch.setattr("common.device_utils.get_connected_devices", lambda: [])

    with pytest.raises(adb_exec.DeviceNotFoundError) as raised:
        resolve_device_identifier("emulator-5556")

    assert str(raised.value) == (
        "Device 'emulator-5556' was requested, but no devices are connected. "
        "Start an emulator or connect a device:\n"
        "  emulator -avd <device-name>\n"
        "  adb devices"
    )
    assert raised.value.remedy and "adb devices" in raised.value.remedy


@pytest.mark.parametrize(
    "error",
    [
        adb_exec.DeviceNotFoundError("Device 'emulator-5556' is not attached."),
        adb_exec.DeviceOfflineError("Device emulator-5554 is attached but offline."),
        adb_exec.DeviceUnauthorizedError("Device emulator-5554 has not authorized this computer."),
    ],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_an_unavailable_device_exits_3(error, json_mode, capsys):
    """The agent's next move is a different device, not a different command."""
    code, out, _ = _run(_raising(error), ["--json"] if json_mode else [], capsys)
    assert code == 3
    if json_mode:
        assert json.loads(out)["success"] is False


def test_a_main_that_raises_device_not_found_exits_3(monkeypatch, capsys, recorded_anywhere):
    """The real message, from adb's real words: adb_exec classifies it, run maps it."""
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: [])
    error = adb_exec._classify(recorded_anywhere("adb_shell_device_not_found"), "emulator-5556")
    assert isinstance(error, adb_exec.DeviceNotFoundError)

    code, out, _ = _run(_raising(error), ["--json"], capsys)
    assert code == 3
    payload = json.loads(out)
    assert "emulator-5556" in payload["error"]
    assert "adb devices" in payload["remedy"]


def test_several_devices_is_a_failure_not_an_unavailable_device(
    monkeypatch, capsys, recorded_anywhere
):
    """Nothing was lost; the caller has to choose. Exit 1, and say which serials."""
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554", "emulator-5556"])
    error = adb_exec._classify(recorded_anywhere("adb_more_than_one_device"), None)
    assert isinstance(error, adb_exec.MultipleDevicesError)

    code, out, _ = _run(_raising(error), ["--json"], capsys)
    assert code == 1
    assert "emulator-5556" in json.loads(out)["remedy"]


# ---------------------------------------------------------------------------
# The remedy: the error's own when it has one
# ---------------------------------------------------------------------------


def test_a_missing_adb_is_reported_with_its_remedy_and_without_a_traceback(
    monkeypatch, tmp_path, capsys
):
    """The real path: run_adb with nothing on PATH, so no device can be reached."""
    monkeypatch.setenv("PATH", str(tmp_path))

    def main(argv):
        adb_exec.run_adb("devices")

    code, out, err = _run(main, ["--json"], capsys)
    assert code == 1
    payload = json.loads(out)
    assert payload["error"].startswith("adb could not be run")
    assert "platform-tools" in payload["remedy"]
    assert err == ""


def test_the_error_is_the_whole_message_and_the_remedy_is_the_errors_own(
    monkeypatch, recorded_anywhere
):
    """``error`` is what every other caller sees; ``remedy`` is the error's own.

    The remedy is usually the tail of the message too, and it is accepted that
    it appears in both: cutting it out of ``error`` could empty the field.
    """
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])
    errors = [
        adb_exec._classify(recorded_anywhere("adb_shell_device_not_found"), "emulator-5556"),
        adb_exec._classify(recorded_anywhere("adb_more_than_one_device"), None),
        missing_emulator_error(),
    ]
    for error in errors:
        what, remedy = cli.describe(error)
        assert error.remedy, f"{type(error).__name__} carries no remedy"
        assert what == " ".join(str(error).split())
        assert remedy.lower() == " ".join(error.remedy.split()).lower()


def test_the_serial_list_is_read_once_for_message_and_remedy(monkeypatch, recorded_anywhere):
    """Two reads of the inventory could disagree, and so would message and remedy."""
    inventories = iter([["emulator-5554"], ["emulator-5554", "emulator-5556"]])
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: next(inventories))
    error = adb_exec._classify(recorded_anywhere("adb_more_than_one_device"), None)
    assert error.remedy in str(error)


def test_an_error_without_a_remedy_gets_its_class_default():
    what, remedy = cli.describe(adb_exec.AdbCommandError("`adb shell x` exited 1: boom"))
    assert what == "`adb shell x` exited 1: boom"
    assert remedy == cli.DEFAULT_REMEDIES[adb_exec.AdbCommandError]


# ---------------------------------------------------------------------------
# cli.fail: a failure the script detected itself
# ---------------------------------------------------------------------------


def test_fail_is_reported_in_the_shape_with_its_remedy(capsys):
    def main(argv):
        cli.fail("pm list packages printed nothing", "Check the device is booted.")

    code, out, err = _run(main, ["--json"], capsys)
    assert code == 1
    assert json.loads(out) == {
        "success": False,
        "error": "pm list packages printed nothing",
        "remedy": "Check the device is booted.",
    }
    assert err == ""


def test_fail_neither_prints_nor_exits_on_its_own(capsys):
    with pytest.raises(cli.Failure) as raised:
        cli.fail("nothing listed", "look again")
    assert raised.value.remedy == "look again"
    assert capsys.readouterr() == ("", "")


def test_a_scripts_own_runtime_error_handler_does_not_swallow_fail(capsys):
    """Failure is not a RuntimeError, so `except RuntimeError` lets it through."""

    def main(argv):
        try:
            cli.fail("detected", "Look again.")
        except RuntimeError:
            return 0
        return 0

    code, _, err = _run(main, [], capsys)
    assert code == 1
    assert err.splitlines() == ["Error: detected", "Remedy: Look again."]


def test_details_ride_after_the_contract_keys_and_never_replace_them(capsys):
    """A refusal can carry what it found (a search's screens, a listing's
    entries); a detail named like a contract key is dropped, not obeyed."""

    def main(argv):
        cli.fail(
            "Not found: text='x'",
            "Map the screen first.",
            details={"search": {"scrolls": 2}, "success": True, "error": "", "remedy": ""},
        )

    code, out, err = _run(main, ["--json"], capsys)
    assert code == 1
    assert out.count("\n") == 1, "the refusal is one line, details and all"
    assert json.loads(out) == {
        "success": False,
        "error": "Not found: text='x'",
        "remedy": "Map the screen first.",
        "search": {"scrolls": 2},
    }
    assert err == ""


def test_details_are_not_printed_in_text_mode(capsys):
    def main(argv):
        cli.fail("detected", "Look again.", details={"search": {"scrolls": 2}})

    code, out, err = _run(main, [], capsys)
    assert code == 1
    assert out == ""
    assert err.splitlines() == ["Error: detected", "Remedy: Look again."]


def test_a_detail_that_is_not_json_still_leaves_one_object(capsys):
    """json.dumps would raise TypeError on a Path; the refusal must still parse."""

    def main(argv):
        cli.fail("detected", "Look again.", details={"where": os.path.curdir.encode()})

    code, out, _ = _run(main, ["--json"], capsys)
    assert code == 1
    assert json.loads(out)["where"] == str(os.path.curdir.encode())


# ---------------------------------------------------------------------------
# Which argv asks for JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--json"], True),
        (["--list", "--json"], True),
        (["--", "--json"], False),  # after `--`, --json is a value (a file name)
        (["a.png", "--", "--json", "b.png"], False),
        (["--jsonx"], False),
        ([], False),
    ],
)
def test_json_is_detected_up_to_the_end_of_options(argv, expected):
    assert cli.wants_json(argv) is expected


def test_a_script_names_its_alias_for_json(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.run(
            _raising(RuntimeError("cannot read baseline.png")),
            ["--details"],
            json_flags=("--json", "--details"),
        )
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "cannot read baseline.png"


def test_every_default_remedy_belongs_to_the_family():
    """A default for a class the boundary does not catch would never be used."""
    for cls in cli.DEFAULT_REMEDIES:
        assert issubclass(cls, cli.TYPED_FAMILY), cls


# ---------------------------------------------------------------------------
# Anything else is a defect: still the shape, and the traceback on stderr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [KeyError("serial"), NotImplementedError("not yet"), AttributeError("no attribute x")],
    ids=lambda e: type(e).__name__,
)
def test_an_unexpected_exception_is_a_defect_in_the_same_shape(error, capsys):
    code, out, err = _run(_raising(error), ["--json"], capsys)

    assert code == 1
    payload = json.loads(out)  # still parseable: the traceback went elsewhere
    assert set(payload) == KEYS
    assert payload["error"].startswith(f"{type(error).__name__}: ")
    assert payload["remedy"] == cli.DEFECT_REMEDY
    assert "Traceback" in err, "the traceback is the only evidence of where the defect is"
    assert "Traceback" not in out


class _RemedyThatRaisesError(RuntimeError):
    """A typed error whose `remedy` getter is itself broken."""

    @property
    def remedy(self):
        raise ValueError("remedy lookup broke")


class _MessageThatRaisesError(RuntimeError):
    """A typed error whose message cannot be produced."""

    def __str__(self):
        raise ValueError("message formatting broke")


@pytest.mark.parametrize("error", [_RemedyThatRaisesError("x"), _MessageThatRaisesError("x")])
def test_an_error_that_breaks_while_being_described_is_reported_as_a_defect(error, capsys):
    """describe() reading str(error) or error.remedy must not let the failure
    escape without an object: the code review reproduced a ValueError from a
    remedy property doing exactly that."""
    code, out, err = _run(_raising(error), ["--json"], capsys)

    assert code == 1
    payload = json.loads(out)
    assert set(payload) == KEYS
    assert payload["remedy"] == cli.DEFECT_REMEDY
    assert payload["error"].startswith("ValueError: ")
    assert "Traceback" in err
    assert "Traceback" not in out


def test_a_defect_whose_message_cannot_be_read_still_gets_the_shape(capsys):
    class _UnprintableError(Exception):
        def __str__(self):
            raise ValueError("nope")

    code, out, _ = _run(_raising(_UnprintableError()), ["--json"], capsys)

    assert code == 1
    assert json.loads(out)["error"] == "_UnprintableError: <the message could not be read>"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "error",
    [KeyError("serial"), adb_exec.AdbError("device offline")],
    ids=["defect", "typed"],
)
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_closed_stream_still_exits_with_the_status(stream, error, json_mode, monkeypatch):
    """A closed stdout raised ValueError out of _report_defect (the review
    reproduced it): the report could not be written, and the exit status went
    with it. Nobody is reading -- the status is the one thing left to say."""
    import io

    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, stream, closed)

    with pytest.raises(SystemExit) as exc:
        cli.run(_raising(error), ["--json"] if json_mode else [])

    assert exc.value.code == 1


def test_a_real_closed_stdout_pipe_keeps_the_device_exit_status():
    """Interpreter finalization must not replace exit 3 with its broken-pipe status 120."""
    scripts = Path(cli.__file__).resolve().parents[1]
    program = f"""
import sys
sys.path.insert(0, {str(scripts)!r})
from common import adb_exec, cli

def main(argv):
    raise adb_exec.DeviceOfflineError("device offline")

cli.run(main, ["--json"])
"""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(write_end)

    assert completed.returncode == 3, completed.stderr


@pytest.mark.parametrize(
    ("raised", "argv", "status"),
    [
        ('adb_exec.DeviceOfflineError("device offline")', [], 3),
        ('KeyError("serial")', [], 1),
        ('KeyError("serial")', ["--json"], 1),
        ("KeyboardInterrupt()", [], 130),
    ],
    ids=["typed-text", "defect-text", "defect-json", "interrupt"],
)
def test_a_real_closed_stderr_pipe_keeps_the_exit_status(raised, argv, status):
    """The stream that broke is the one neutralised (final review, W4-D item 4).

    A text refusal goes to stderr, a defect's traceback does in either mode,
    and so does "Interrupted.". Written to a real stderr pipe nobody reads,
    each failed, the handler pointed STDOUT at the null device, and the
    interpreter's final flush of stderr's pending bytes failed again and
    exited 120 -- for the reviewer's case, a device-offline refusal that is 3.
    """
    scripts = Path(cli.__file__).resolve().parents[1]
    program = f"""
import sys
sys.path.insert(0, {str(scripts)!r})
from common import adb_exec, cli

def main(argv):
    raise {raised}

cli.run(main, {argv!r})
"""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program],
            stdout=subprocess.PIPE,
            stderr=write_end,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(write_end)

    assert completed.returncode == status, completed.stdout
    if "--json" in argv:
        assert json.loads(completed.stdout)["success"] is False, completed.stdout


def test_an_interrupt_with_a_closed_stderr_still_exits_130(monkeypatch):
    import io

    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)

    with pytest.raises(SystemExit) as exc:
        cli.run(_raising(KeyboardInterrupt()), [])

    assert exc.value.code == 130


def test_a_defect_in_text_mode_ends_with_the_two_lines(capsys):
    code, out, err = _run(_raising(KeyError("serial")), [], capsys)
    assert code == 1
    assert out == ""
    lines = err.splitlines()
    assert lines[0].startswith("Traceback")
    assert lines[-2] == "Error: KeyError: 'serial'"
    assert lines[-1] == f"Remedy: {cli.DEFECT_REMEDY}"


# ---------------------------------------------------------------------------
# A streaming mode: the refusal is the last line, and parses on its own
# ---------------------------------------------------------------------------


def test_a_streaming_failure_ends_stdout_with_the_refusal_on_its_own_line(
    monkeypatch, capsys, recorded
):
    """``anr_watcher --watch --json`` prints one JSON record per event as it goes.

    A failure after some have gone out cannot retract them, so the single-object
    promise does not apply to a streaming mode. What does: every line before the
    last is a record, and the last line is the refusal, parseable on its own --
    which is why cli.report writes it on one line.

    The stream is the recorded broadcast ANR as logd's records (`logcat -B`),
    one event. The failure is the pipe from adb breaking after them (EIO on
    read), the one mid-stream failure ``watch`` can meet that is not the
    stream simply ending. Before this change ``watch`` caught it, printed a
    sentence on stderr and returned False: exit 1, and no refusal on stdout at
    all.

    The pipe is a real one: since PR #26 ``watch`` reads the descriptor itself
    (``anr_watcher._FdRecords``, records since PR #34) rather than calling
    ``readline()`` on the file, so the break is injected where that reader
    reports the end of the stream.
    """
    name = "logcat_anr_broadcast_records"
    stream = recorded.data(f"{name}.bin")
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == name)

    read_end, write_end = os.pipe()
    os.write(write_end, stream)
    os.close(write_end)
    pipe = os.fdopen(read_end, "rb", buffering=0)
    real_read = anr_watcher._FdRecords.read

    def read_until_the_pipe_breaks(self, timeout):
        records = real_read(self, timeout)
        if records is None:
            raise OSError(5, "Input/output error")
        return records

    monkeypatch.setattr(anr_watcher._FdRecords, "read", read_until_the_pipe_breaks)

    class _Logcat:
        def __init__(self, cmd, **kwargs):
            self.stdout = pipe

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(anr_watcher.subprocess, "Popen", _Logcat)
    # --watch asks `adb get-state` before streaming (B4): the device is up. And
    # an ANR report counts only as system_server wrote it: its pid as `pidof`
    # answered in the recording's own run.
    monkeypatch.setattr(
        anr_watcher,
        "run_adb",
        lambda *_a, **_k: adb_exec.AdbResult(0, recorded.text("adb_get_state"), ""),
    )
    system_server = str(entry["system_server_pid"])
    monkeypatch.setattr(anr_watcher, "get_system_server_pid", lambda *_a, **_k: system_server)

    try:
        code, out, _ = _run(anr_watcher.main, ["--watch", "--json"], capsys)
    finally:
        pipe.close()

    assert code == 1
    lines = out.splitlines()
    records, refusal = lines[:-1], json.loads(lines[-1])
    assert len(records) == 1, "the event streamed before the failure is kept"
    assert all(json.loads(record)["kind"] for record in records)
    assert refusal["success"] is False
    assert set(refusal) == KEYS
    assert "Input/output error" in refusal["error"]
