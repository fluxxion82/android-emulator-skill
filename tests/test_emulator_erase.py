"""Device-free tests for emulator_erase.

These mock the ``subprocess`` boundary underneath ``common.adb_exec`` (for the
running check) and point the AVD home at a ``tmp_path`` so no emulator/adb is
needed. They assert the pure logic for the feature deltas -- ``--all`` (batch
erase with structured counts), ``--verify`` (poll the AVD on disk until the wipe
lands), the env-configurable ``ANDROID_EMU_ERASE_TIMEOUT`` tunable -- plus the
error contract the adb_exec migration introduced: the running check must never
answer "not running" because adb failed to reach the device.

The L4 block near the bottom is the one that matters most. Every test there
asserts the same safety property from a different starting point: **the wipe
does not happen**. They check the userdata file still exists rather than only
the returned tuple, because the tuple is a claim and the file is the fact --
and the defect being pinned is a script asserting a state it had not
established.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
from pathlib import Path

import emulator_erase
import pytest

from common import adb_exec


def _make_avd(avd_home: Path, name: str, with_userdata: bool = True) -> Path:
    """Create a fake ``<name>.avd`` dir with a config.ini and optional userdata."""
    avd_dir = avd_home / f"{name}.avd"
    avd_dir.mkdir(parents=True)
    (avd_dir / "config.ini").write_text("hw.device.name=pixel\n")
    if with_userdata:
        (avd_dir / "userdata-qemu.img").write_bytes(b"data")
        (avd_dir / "cache.img").write_bytes(b"data")
    return avd_dir


@pytest.fixture
def eraser(monkeypatch, tmp_path):
    """An eraser whose AVD home is an empty tmp dir and which is never 'running'."""
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    e = emulator_erase.EmulatorEraser()
    # Never shell out to adb during these tests.
    monkeypatch.setattr(e, "running_check", lambda _name: None)
    monkeypatch.setattr(emulator_erase.time, "sleep", lambda _s: None)
    return e


def test_erase_deletes_userdata(eraser, tmp_path):
    avd_dir = _make_avd(tmp_path, "Pixel_5_API_33")

    success, message = eraser.erase("Pixel_5_API_33")

    assert success is True
    assert "AVD erased" in message
    assert not (avd_dir / "userdata-qemu.img").exists()
    assert not (avd_dir / "cache.img").exists()
    # Config is preserved (factory reset, not delete).
    assert (avd_dir / "config.ini").exists()


def test_erase_already_clean(eraser, tmp_path):
    _make_avd(tmp_path, "Pixel_5_API_33", with_userdata=False)

    success, message = eraser.erase("Pixel_5_API_33")

    assert success is True
    assert "already clean" in message


def test_erase_missing_avd(eraser):
    success, message = eraser.erase("DoesNotExist")
    assert success is False
    assert "not found" in message


def test_verify_succeeds_after_wipe(eraser, tmp_path):
    avd_dir = _make_avd(tmp_path, "Pixel_5_API_33")

    success, message = eraser.erase("Pixel_5_API_33", verify=True)

    assert success is True
    assert "verified clean" in message
    assert not (avd_dir / "userdata-qemu.img").exists()


def test_verify_times_out_when_userdata_lingers(eraser, tmp_path, monkeypatch):
    _make_avd(tmp_path, "Pixel_5_API_33")

    # Simulate a stuck wipe: unlink is a no-op so user data lingers on disk and
    # the verify poll never observes a clean dir.
    monkeypatch.setattr(emulator_erase.Path, "unlink", lambda _self, **_kw: None)

    # Tiny timeout so the test is fast.
    success, message = eraser.erase("Pixel_5_API_33", verify=True, timeout_seconds=0)

    assert success is False
    assert "verification timeout" in message


def test_erase_all_returns_structured_counts(eraser, tmp_path):
    _make_avd(tmp_path, "Pixel_5_API_33")
    _make_avd(tmp_path, "Pixel_7_API_34")

    succeeded, failed, results = eraser.erase_all()

    assert succeeded == 2
    assert failed == 0
    assert {r["avd"] for r in results} == {"Pixel_5_API_33", "Pixel_7_API_34"}
    assert all(r["success"] for r in results)


def test_erase_all_empty(eraser):
    succeeded, failed, results = eraser.erase_all()
    assert (succeeded, failed, results) == (0, 0, [])


def _fake_adb(monkeypatch, responses):
    """Answer adb calls at the subprocess boundary under common.adb_exec.

    ``responses`` maps a command prefix tuple to (returncode, stdout, stderr).
    Every call is recorded, argv and kwargs both, so the tests can assert the
    command mapping and that nothing goes out unbounded.
    """
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        for prefix, (returncode, stdout, stderr) in responses.items():
            if tuple(cmd[: len(prefix)]) == prefix or prefix == ():
                return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", fake_run)
    return calls


def test_running_check_uses_adb_command_mapping(monkeypatch, tmp_path, recorded):
    """is_avd_running builds plain adb commands (no shell=True) and bounds them.

    The adb output is the recorded article rather than a hand-written stand-in:
    the real `adb devices` line carries trailing `product:`/`model:` fields, and
    the emulator console answers `avd name` with its own "OK" line.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    e = emulator_erase.EmulatorEraser()

    calls = _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, recorded.text("adb_devices_single"), ""),
            ("adb", "-s"): (0, recorded.text("emu_avd_name"), ""),
        },
    )

    assert e.is_avd_running("Pixel_9") is True
    # `-l` because the listing goes through device_utils.get_connected_devices
    # now: one row parser for the whole skill, rather than this script's own.
    assert calls[0]["cmd"] == ["adb", "devices", "-l"]
    assert calls[1]["cmd"] == ["adb", "-s", "emulator-5554", "emu", "avd", "name"]
    assert all(c["kwargs"].get("timeout") for c in calls), "an adb call went out unbounded"


def test_a_failed_device_listing_stops_the_erase(monkeypatch, tmp_path, recorded):
    """`adb devices` failed, so not even the list of emulators is known.

    This used to answer False -- "not running" -- and the wipe went ahead. It
    was justified as "the command ran and failed, which is not evidence the AVD
    is running", but the absence of evidence is the point: the check never
    produced an answer, and an erase is irreversible (L4).
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    avd_dir = _make_avd(tmp_path, "Pixel_9")
    e = emulator_erase.EmulatorEraser()
    _fake_adb(monkeypatch, {("adb", "devices"): (1, "", "some other adb complaint\n")})

    with pytest.raises(emulator_erase.RunningCheckError) as excinfo:
        e.erase("Pixel_9")

    assert (avd_dir / "userdata-qemu.img").exists(), "user data was wiped on an unanswered check"
    message = str(excinfo.value)
    assert "--force" in message and "kill-server" in message, f"no remedy named: {message}"
    # An erase that was asked to skip the check still may.
    assert e.erase("Pixel_9", force=True)[0] is True
    assert not (avd_dir / "userdata-qemu.img").exists()

    # The recording is here to keep the header/serial shapes honest for the
    # rest of this block, which does parse them.
    assert recorded.text("adb_devices_multiple").startswith("List of devices attached")


def test_a_device_error_is_not_answered_as_not_running(
    monkeypatch, tmp_path, recorded, recorded_anywhere
):
    """The dangerous swallow: "adb could not reach the device" != "not running".

    Answering False here let an erase wipe the user data of an emulator that was
    actually live. The device error now reaches the caller as an UNKNOWN
    verdict rather than a raise -- the emulator is named, adb's own message is
    quoted as the reason, and the wipe does not happen. That is strictly more
    than the typed exception carried: an agent is told which serial went
    unanswered, not only that something did.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    avd_dir = _make_avd(tmp_path, "Pixel_9")
    e = emulator_erase.EmulatorEraser()

    _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, recorded.text("adb_devices_single"), ""),
            ("adb", "-s"): (1, "", recorded_anywhere("adb_device_not_found")),
        },
    )

    success, message = e.erase("Pixel_9")

    assert success is False
    assert (avd_dir / "userdata-qemu.img").exists(), "user data was wiped on an unanswered check"
    assert "emulator-5554" in message, f"the refusal does not say which emulator: {message}"
    assert "kill-server" in message and "--force" in message, f"no remedy named: {message}"


# --- L4: three ways the running check said "no" without knowing --------------
# Each of these ran an erase to completion on a host where the check had not
# established that the AVD was idle. The recorded `adb devices -l` listing is
# the input in every one: two emulators, which is what the old single-`try`
# scan could not survive.


def _two_emulator_serials(recorded) -> tuple[str, str]:
    """The two emulator serials in the recorded two-device listing."""
    serials = [
        line.split()[0]
        for line in recorded.text("adb_devices_multiple").splitlines()
        if line.split() and line.split()[0].startswith("emulator-")
    ]
    assert len(serials) == 2, f"the recording no longer holds two emulators: {serials}"
    return serials[0], serials[1]


def test_one_failing_console_query_does_not_end_the_scan(monkeypatch, tmp_path, recorded):
    """L4(a): the `try` wrapped the whole loop, so the first failure was final.

    Two emulators are up. The console query against the first fails; the second
    would have named the AVD being erased. The old code returned False from the
    `except` without ever issuing the second query, and wiped a live AVD.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    avd_dir = _make_avd(tmp_path, "Pixel_9")
    first, second = _two_emulator_serials(recorded)
    e = emulator_erase.EmulatorEraser()

    calls = _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, recorded.text("adb_devices_multiple"), ""),
            ("adb", "-s", first, "emu"): (1, "", "console write failed\n"),
            ("adb", "-s", second, "emu"): (0, recorded.text("emu_avd_name"), ""),
        },
    )

    success, message = e.erase("Pixel_9")

    queried = [c["cmd"] for c in calls if "emu" in c["cmd"]]
    assert len(queried) == 2, f"the scan stopped at the first failure: {queried}"
    assert success is False, message
    assert (avd_dir / "userdata-qemu.img").exists(), "a live AVD was wiped"


def test_a_console_query_that_failed_is_not_an_answer(monkeypatch, tmp_path, recorded):
    """L4(a), the half that has no second opinion.

    Same failure, but no other emulator identifies itself as this AVD either.
    "I could not look" is still not "it is not running", so the erase refuses
    rather than falling through to the wipe.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    avd_dir = _make_avd(tmp_path, "Pixel_9")
    first, second = _two_emulator_serials(recorded)
    e = emulator_erase.EmulatorEraser()

    _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, recorded.text("adb_devices_multiple"), ""),
            ("adb", "-s", first, "emu"): (1, "", "console write failed\n"),
            ("adb", "-s", second, "emu"): (
                0,
                recorded.text("emu_avd_name").replace("Pixel_9", "Some_Other_AVD"),
                "",
            ),
        },
    )

    success, message = e.erase("Pixel_9")

    assert success is False
    assert "--force" in message, f"the refusal names no way forward: {message}"
    assert (avd_dir / "userdata-qemu.img").exists(), "an unidentified emulator was erased anyway"


def test_an_emulator_that_is_not_yet_in_state_device_counts_as_running(
    monkeypatch, tmp_path, recorded
):
    """L4(b): `offline` is an emulator mid-boot, not an absent one.

    The old test was `"device" in line`, so a still-booting emulator was
    invisible and its AVD was wiped underneath it. Its console cannot be asked
    which AVD it is either, so the only safe reading is "it might be this one".

    The `offline` listing is the recorded one with the state column rewritten:
    no profile has recorded a device in that state (see the `parse_adb_devices`
    entry in tests/test_fixture_policy.py), and provoking one means killing an
    emulator mid-boot on the recording host.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    avd_dir = _make_avd(tmp_path, "Pixel_9")
    first, _second = _two_emulator_serials(recorded)

    booting = "\n".join(
        f"{first}\toffline" if line.split() and line.split()[0] == first else line
        for line in recorded.text("adb_devices_multiple").splitlines()
    )
    e = emulator_erase.EmulatorEraser()

    calls = _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, booting, ""),
            ("adb", "-s"): (0, recorded.text("emu_avd_name").replace("Pixel_9", "Other"), ""),
        },
    )

    success, message = e.erase("Pixel_9")

    assert success is False, message
    assert (avd_dir / "userdata-qemu.img").exists(), "a booting emulator's AVD was wiped"
    assert not any(
        c["cmd"][:3] == ["adb", "-s", first] for c in calls
    ), "an offline emulator's console was queried; it cannot answer"


def test_the_avd_name_is_matched_whole_not_as_a_substring(monkeypatch, tmp_path, recorded):
    """L4(c): `name in reply.payload` refused erases it had no business refusing.

    The console says `Pixel_9`. An AVD called `Pixel` is a different AVD and is
    not running, but the substring test matched and answered "running", so
    `--name Pixel` could never be erased while any `Pixel_*` emulator was up.
    Equality on the unframed payload is the whole fix -- and the true match
    below must keep refusing, or the fix is just a hole in the other direction.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    short = _make_avd(tmp_path, "Pixel")
    exact = _make_avd(tmp_path, "Pixel_9")
    e = emulator_erase.EmulatorEraser()

    _fake_adb(
        monkeypatch,
        {
            ("adb", "devices"): (0, recorded.text("adb_devices_single"), ""),
            ("adb", "-s"): (0, recorded.text("emu_avd_name"), ""),
        },
    )

    success, message = e.erase("Pixel")
    assert success is True, message
    assert not (short / "userdata-qemu.img").exists()

    running, _message = e.erase("Pixel_9")
    assert running is False
    assert (exact / "userdata-qemu.img").exists(), "the AVD the console named was erased"


def test_a_successful_erase_says_snapshots_were_kept(monkeypatch, tmp_path, capsys):
    """L9: "factory state" that a `snapshot.py --load` can undo, said out loud.

    The wipe list is six image files; a saved snapshot is a whole guest machine
    and is deliberately left alone. That is the right call and a surprise, so
    the erase names it and names the command that removes one.
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    _make_avd(tmp_path, "Pixel_9")
    # running_check, not is_avd_running: erase() consults the former, and
    # patching the boolean wrapper left the real probe running -- which passed
    # locally, where adb is on PATH, and failed on a runner where it is not. A
    # unit test reaching adb at all is the bug; the exit status only showed it.
    monkeypatch.setattr(emulator_erase.EmulatorEraser, "running_check", lambda _self, _name: None)

    def _no_adb(*_args, **_kwargs):
        raise AssertionError("a unit test reached the adb boundary")

    monkeypatch.setattr(adb_exec.subprocess, "run", _no_adb)
    monkeypatch.setattr(emulator_erase.sys, "argv", ["emulator_erase.py", "--name", "Pixel_9"])

    with pytest.raises(SystemExit) as exc:
        emulator_erase.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "snapshot.py --delete" in out, f"the erase does not mention snapshots: {out}"


def test_the_snapshot_note_names_a_flag_snapshot_py_actually_has(scripts_dir):
    """The note is only useful if the command in it exists.

    Documented flags that do not exist is a defect class this repo has shipped
    before, so the note is checked against snapshot.py's own argparse rather
    than against memory.
    """
    source = (scripts_dir / "snapshot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="snapshot.py")
    flags = {
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _attribute_name(node) == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    assert "--delete" in flags, f"snapshot.py has no --delete; the erase note lies: {sorted(flags)}"
    assert "snapshot.py --delete" in emulator_erase.SNAPSHOT_NOTE


def _attribute_name(call: ast.Call) -> str | None:
    """The attribute a call targets, e.g. `parser.add_argument` -> add_argument."""
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def test_cli_reports_an_adb_error_without_a_traceback(monkeypatch, tmp_path, capsys):
    """At the CLI boundary the agent gets the remedy, not a stack trace."""
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    _make_avd(tmp_path, "Pixel_9")
    _fake_adb(monkeypatch, {("adb",): (1, "", "error: device offline\n")})
    monkeypatch.setattr(emulator_erase.sys, "argv", ["emulator_erase.py", "--name", "Pixel_9"])

    with pytest.raises(SystemExit) as exc:
        emulator_erase.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "Traceback" not in captured.err
    message = captured.err.lower()
    assert "reconnect" in message or "kill-server" in message, "no remedy named"


@pytest.mark.parametrize(
    "argv",
    [
        ["--name", "Pixel_9", "--json"],
        ["--all", "--json"],
        ["--json"],
    ],
    ids=["name", "all", "no-target"],
)
def test_every_failing_json_mode_prints_an_error_document(monkeypatch, tmp_path, capsys, argv):
    """R1: `--json` promised `{"error": ...}` and delivered an empty stdout.

    The CLI boundary caught AdbError but was outside the arg parsing, so it had
    no idea `--json` had been asked for: a RunningCheckError produced exit 1,
    prose on stderr, and nothing at all on the stream an agent parses. Argument
    parsing now happens first, so the handler can answer in the mode it was
    asked in -- checked for the usage error too, since that had the same shape
    (argparse help text, no JSON).
    """
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    _make_avd(tmp_path, "Pixel_9")
    _fake_adb(monkeypatch, {("adb", "devices"): (1, "", "some other adb complaint\n")})
    monkeypatch.setattr(emulator_erase.sys, "argv", ["emulator_erase.py", *argv])

    with pytest.raises(SystemExit) as exc:
        emulator_erase.main()

    assert exc.value.code != 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "error" in payload, f"--json reported no error document: {payload}"
    assert payload["error"], "the error document is empty"
    assert "Traceback" not in captured.err


def test_default_tunables():
    assert emulator_erase.DEFAULT_ERASE_TIMEOUT == 90
    assert emulator_erase.POLL_INTERVAL_SECONDS == 0.5


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_ERASE_TIMEOUT", "42")
    monkeypatch.setenv("ANDROID_EMU_POLL_INTERVAL", "1.5")
    reloaded = importlib.reload(emulator_erase)
    try:
        assert reloaded.DEFAULT_ERASE_TIMEOUT == 42
        assert reloaded.POLL_INTERVAL_SECONDS == 1.5
    finally:
        monkeypatch.delenv("ANDROID_EMU_ERASE_TIMEOUT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_POLL_INTERVAL", raising=False)
        importlib.reload(emulator_erase)
