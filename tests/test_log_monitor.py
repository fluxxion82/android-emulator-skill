"""Device-free tests for log_monitor's pure arg->command mapping and caps.

Covers the two curated deltas:
  1. ``--last DURATION`` historical window built via ``logcat -d -t "<timestamp>"``.
  2. Output caps made env-configurable via ``ANDROID_EMU_LOG_*`` (defaults preserved).

And, at the end, the v0.8.0 fixes driven through the real CLI against a fake
adb that answers with recorded output: D1 (``--severity``), N15 (line counts
beside unique counts), D4 (an app filter that survives a restart), and the
review of v0.8.0: A3 (a window that leaves the app's lines out says so), A4 (a
device that is not there is never a quiet capture), A5 (adb's error is read
line by line) and A8 (the fake device tails records, as logd does).

log_monitor reads logd's records (``logcat -B``), so the fake device replays
recorded records; a window the device did not record is derived from a
recorded one a record at a time (``fake_toolchain.Frame``).

All adb/subprocess interaction is mocked, so these run without a device.
"""

from __future__ import annotations

import importlib
import io
import json
import re
import subprocess
from datetime import datetime

import log_monitor
import pytest
from fake_toolchain import (
    FakeToolchain,
    Frame,
    Rule,
    assert_refusal,
    framed,
    frames,
    uid_rules,
)

from common import logcat
from common.device_utils import parse_package_uids


def _monitor(**kwargs) -> log_monitor.LogMonitor:
    return log_monitor.LogMonitor(**kwargs)


# --- build_logcat_command: base streaming mapping -------------------------------


def test_build_command_base_streaming():
    cmd = _monitor().build_logcat_command()
    # No serial -> no -s; streaming form has neither -d nor -t.
    assert cmd[0] == "adb"
    assert "-d" not in cmd
    assert "-t" not in cmd
    # logd's records, not text: a tag can forge a whole line of text (A1).
    # (The text layout the parser reads is what LogRecord.lines prints; the
    # `-v time` layout this once asked for matched nothing.)
    assert logcat.BINARY_FLAG in cmd
    assert "-v" not in cmd
    assert cmd.index("logcat") == 1
    # A live read starts at the end of the buffer.
    assert cmd[cmd.index("-T") + 1] == "1"


def test_build_command_includes_serial():
    cmd = _monitor(device_serial="emulator-5554").build_logcat_command()
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]


@pytest.mark.parametrize(
    "severities",
    [["error"], ["error", "warning"], None, ["info"]],
    ids=["error", "error,warning", "default", "info"],
)
def test_no_filterspec_is_sent_with_a_binary_read(severities):
    """``-B`` ignores filterspecs (measured: ``*:E`` still returned I and D records).

    Sending one would only suggest a device-side filter that is not applied;
    the floor is applied on the host (``_above_floor``), after attribution.
    """
    cmd = _monitor(app_package="com.myapp", severity_filter=severities).build_logcat_command()
    assert not [part for part in cmd if ":" in part and not part.startswith("-")], cmd


@pytest.mark.parametrize(
    ("severities", "kept", "dropped"),
    [
        (["error"], ["E", "F", "S"], ["W", "I", "D", "V", "?"]),
        (["error", "warning"], ["W", "E"], ["I", "V"]),
        (None, ["D", "I", "W", "E"], ["V", "?"]),
        (["verbose"], ["V", "D", "E"], ["?"]),
    ],
    ids=["error", "error,warning", "default", "verbose"],
)
def test_the_floor_is_the_one_a_filterspec_applied(severities, kept, dropped):
    """``*:<P>`` showed P and everything above it; so does the host floor."""
    monitor = _monitor(severity_filter=severities)
    assert all(monitor._above_floor(p) for p in kept)
    assert not any(monitor._above_floor(p) for p in dropped)


# --- delta 1: --last historical window (logcat -d -t) ---------------------------


def test_build_command_last_window_uses_dump_and_tail():
    now = datetime(2026, 6, 17, 12, 30, 0)
    cmd = _monitor().build_logcat_command(last_minutes=5, now=now)
    # Historical window must dump-and-exit (-d) and tail from a timestamp (-t).
    assert "-d" in cmd
    t_index = cmd.index("-t")
    # Timestamp is "MM-DD HH:MM:SS.000" for now - 5 minutes => 12:25:00.
    assert cmd[t_index + 1] == "06-17 12:25:00.000"
    assert "-T" not in cmd


def test_build_command_last_window_hour_offset():
    now = datetime(2026, 6, 17, 12, 30, 0)
    cmd = _monitor().build_logcat_command(last_minutes=60, now=now)
    t_index = cmd.index("-t")
    assert cmd[t_index + 1] == "06-17 11:30:00.000"


def test_streaming_command_has_no_dump_flag():
    # Live streaming (last_minutes=None) must not request a one-shot dump.
    cmd = _monitor().build_logcat_command(last_minutes=None)
    assert "-d" not in cmd


# --- severity -> minimum logcat priority ----------------------------------------


def test_severity_min_priority_default():
    # Default filter is error,warning,info,debug -> minimum letter is D.
    assert _monitor()._severity_min_priority() == "D"


def test_severity_min_priority_errors_only():
    assert _monitor(severity_filter=["error"])._severity_min_priority() == "E"


def test_severity_min_priority_warning_and_error():
    assert _monitor(severity_filter=["warning", "error"])._severity_min_priority() == "W"


def test_severity_min_priority_verbose():
    assert _monitor(severity_filter=["verbose"])._severity_min_priority() == "V"


# --- stream_logs integration: --last builds the historical command --------------


def _state_answers(monkeypatch, recorded) -> list[list[str]]:
    """adb (through adb_exec) answering `get-state` as recorded for an attached device."""
    from common import adb_exec

    asked: list[list[str]] = []

    def run(cmd, **_kwargs):
        asked.append(cmd)
        if "get-state" in cmd:
            return subprocess.CompletedProcess(cmd, 0, recorded.text("adb_get_state"), "")
        raise AssertionError(f"unexpected adb call {cmd}")

    monkeypatch.setattr(adb_exec.subprocess, "run", run)
    return asked


def test_stream_logs_last_window_runs_dump_command(monkeypatch, recorded):
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured["cmd"] = cmd
            # Empty stream: the first read returns b"" immediately (EOF).
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            # Accepts a timeout because stream_logs must never wait unbounded
            # on a process that may not exit (A2).
            return 0

        def poll(self):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    # No app package -> no pidof; the device is asked whether it is there.
    asked = _state_answers(monkeypatch, recorded)
    monkeypatch.setattr(log_monitor.subprocess, "Popen", _FakePopen)

    ok = _monitor().stream_logs(last_minutes=5)
    assert ok is True
    assert "-d" in captured["cmd"]
    assert "-t" in captured["cmd"]
    assert [cmd[-1] for cmd in asked] == ["get-state"], "a dump that ends is not re-checked"


def _pidof_answers(monkeypatch, returncode: int, stdout: str) -> None:
    # adb now goes through common.adb_exec, so that is where the fake belongs.
    from common import adb_exec

    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, returncode, stdout, ""),
    )


def test_resolve_app_pids_returns_pid(monkeypatch):
    _pidof_answers(monkeypatch, 0, "  4242 \n")
    assert _monitor(app_package="com.myapp")._resolve_app_pids() == {"4242"}


def _recorded_exit(recorded, name: str) -> int:
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == name)
    return entry["exit_status"]


def test_resolve_app_pids_handles_not_running(monkeypatch, recorded):
    """`pidof` for an app that is not running: the recorded answer is no output, exit 1.

    That is an answer, not a failure -- the app's processes may still be named
    in the window (a crash that was not restarted).
    """
    _pidof_answers(
        monkeypatch,
        _recorded_exit(recorded, "pidof_not_running"),
        recorded.text("pidof_not_running"),
    )
    monitor = _monitor(app_package="com.myapp")
    assert monitor._resolve_app_pids() == set()
    assert monitor._pidof_failure is None, "the recorded not-running answer is not a failure"


def test_a_pidof_that_fails_otherwise_is_kept_for_the_refusal(monkeypatch):
    """Exit 127 with nothing printed: what a shell without `pidof` exits.

    Not a refusal on its own -- the window may still name the app's processes
    (the D4 tests below) -- but if a refusal follows, it must say `pidof`
    failed rather than that the app was not running. Nothing is printed here
    because anything printed would be invented.
    """
    _pidof_answers(monkeypatch, 127, "")
    monitor = _monitor(app_package="com.myapp")
    assert monitor._resolve_app_pids() == set()
    assert monitor._pidof_failure and "127" in monitor._pidof_failure


# --- delta 2: env-configurable output caps (ANDROID_EMU_LOG_*) ------------------


def test_caps_default_to_historical_values():
    # Defaults must match the pre-delta hardcoded behavior.
    assert log_monitor.LOG_LINE_MAX == 120
    assert log_monitor.LOG_TAIL == 50
    assert log_monitor.LOG_TEXT_SUMMARY_CAP == 5
    assert log_monitor.LOG_JSON_CAP == 20
    assert log_monitor.LOG_INFO_CAP == 20


def test_caps_are_env_configurable(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_LOG_LINE_MAX", "200")
    monkeypatch.setenv("ANDROID_EMU_LOG_TAIL", "10")
    monkeypatch.setenv("ANDROID_EMU_LOG_TEXT_SUMMARY", "3")
    monkeypatch.setenv("ANDROID_EMU_LOG_JSON_CAP", "7")
    monkeypatch.setenv("ANDROID_EMU_LOG_INFO_CAP", "2")
    reloaded = importlib.reload(log_monitor)
    try:
        assert reloaded.LOG_LINE_MAX == 200
        assert reloaded.LOG_TAIL == 10
        assert reloaded.LOG_TEXT_SUMMARY_CAP == 3
        assert reloaded.LOG_JSON_CAP == 7
        assert reloaded.LOG_INFO_CAP == 2
    finally:
        # Restore module-level defaults for any later tests in the session.
        monkeypatch.undo()
        importlib.reload(log_monitor)


def test_json_output_respects_json_cap(monkeypatch):
    monkeypatch.setattr(log_monitor, "LOG_JSON_CAP", 2)
    monitor = _monitor()
    monitor.errors = ["e1", "e2", "e3", "e4"]
    monitor.warnings = ["w1", "w2", "w3"]
    out = monitor.get_json_output()
    assert out["errors"] == ["e1", "e2"]
    assert out["warnings"] == ["w1", "w2"]


# --- D7: a bad duration is a usage error, not a traceback -----------------------


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--duration", "30"), ("--last", "5"), ("--duration", "half an hour")],
    ids=["duration-no-unit", "last-no-unit", "duration-prose"],
)
def test_a_bad_duration_exits_two_and_names_the_accepted_forms(monkeypatch, capsys, flag, value):
    """`--duration 30` reached the terminal as a ValueError traceback.

    The unit is not optional, and forgetting it is the obvious mistake -- `30`
    reads as seconds to everyone who has not read common/logcat.py. The reply
    has to say what it does accept, and it has to be argparse's exit status (2,
    "you called it wrong"), not 1, which this script uses for "the device
    said no".
    """
    monkeypatch.setattr(log_monitor.sys, "argv", ["log_monitor.py", flag, value])

    with pytest.raises(SystemExit) as exc:
        log_monitor.main()

    assert exc.value.code == 2, "a bad flag value is a usage error"
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert flag in stderr, "the message does not say which flag was wrong"
    for accepted in ("30s", "5m", "1h"):
        assert accepted in stderr, f"the message does not name {accepted} as accepted"


def test_a_bad_duration_never_reaches_the_device(monkeypatch):
    """Rejected before anything is spawned: argparse exits, adb is never called."""

    def unexpected(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("a subprocess was started for an invalid duration")

    monkeypatch.setattr(log_monitor.subprocess, "Popen", unexpected)
    monkeypatch.setattr(log_monitor.subprocess, "run", unexpected)
    monkeypatch.setattr(log_monitor.sys, "argv", ["log_monitor.py", "--duration", "30"])

    with pytest.raises(SystemExit) as exc:
        log_monitor.main()

    assert exc.value.code == 2


# =============================================================================
# The CLI against a fake adb that answers with recorded output only.
# =============================================================================

PACKAGE = "com.example.composefixture"
RECORDS = "logcat_app_restart_records"

# The threadtime layout, read for its PRIORITY and PID columns only -- what
# log_monitor prints a record as (common.logcat.LogRecord.lines).
_THREADTIME = re.compile(r"^\d{2}-\d{2}\s+\S+\s+(?P<pid>\d+)\s+\d+\s+(?P<priority>[VDIWEF])\s")


def _column(lines, name: str) -> list[str]:
    return [m[name] for m in map(_THREADTIME.match, lines) if m]


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _device(
    toolchain,
    recorded,
    window: bytes,
    *,
    pidof: Rule | None = None,
    system_server: str | None = None,
    uids: bool = True,
    buffered: bytes = b"",
) -> None:
    """A device that is attached, answers ``pidof`` as told, and replays ``window``.

    ``window`` is recorded ``logcat -B`` records. ``buffered`` is what the
    device already held when a read began: a dump gets it whole before
    ``window``, a live read under ``-T N`` only its last N records
    (fake_toolchain's ``-T`` simulation).

    ``get-state`` answers as recorded for an attached device (adb_get_state):
    log_monitor asks before every read. ``system_server`` answers ``pidof
    system_server``; its rule goes first, as the stub takes the first rule
    whose tokens all appear. Without it, that question gets the app's
    ``pidof`` answer like any other. With ``uids``, the package's uid and the
    ``pidof`` pid's uid are the recorded package uid (fake_toolchain.uid_rules);
    without, those questions get no answer.
    """
    rules = [
        Rule(("devices",), stdout=recorded.text("adb_devices_single")),
        Rule(("get-state",), stdout=recorded.text("adb_get_state")),
    ]
    if system_server is not None:
        rules.append(Rule(("shell", "pidof", "system_server"), stdout=system_server))
    if uids:
        answered = pidof.stdout.strip() if pidof is not None else ""
        rules += uid_rules(recorded, PACKAGE, answered if answered.isdigit() else None)
    if pidof is not None:
        rules.append(pidof)
    rules.append(Rule(("logcat",), stdout=window, buffered=buffered, filter_pid=True))
    toolchain.install("adb", rules)


def _not_running(recorded) -> Rule:
    """``pidof`` exactly as recorded for an app that is not running."""
    return Rule(
        ("shell", "pidof"),
        stdout=recorded.text("pidof_not_running"),
        exit=_recorded_exit(recorded, "pidof_not_running"),
    )


# How each process in the restart window ended, as its MANIFEST entry words it.
CRASHED, STOPPED, ALIVE = "am crash", "am force-stop", "running when the window was read"


def _entry(recorded) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == RECORDS)


def _restart_pids(recorded) -> dict[str, str]:
    """``pidof`` for each process in the restart window, keyed by how it ended.

    The recording stores the pids in launch order under ``pids`` and their
    endings under ``pid_endings``.
    """
    entry = _entry(recorded)
    pids = dict(zip(entry["pid_endings"], entry["pids"], strict=True))
    if set(pids) != {CRASHED, STOPPED, ALIVE} or len(set(pids.values())) != 3:
        pytest.fail(f"{RECORDS} no longer records a crash and a restart: {pids}")
    return pids


def _system_server(recorded) -> str:
    """``pidof system_server``, read in the run that recorded the restart window."""
    entry = _entry(recorded)
    if not str(entry.get("system_server_pid", "")).isdigit():
        pytest.fail(f"{RECORDS} no longer records system_server's pid; re-record it")
    return entry["system_server_pid"]


def _oracles(recorded) -> dict:
    """system_server's pid and the package's uid, both recorded with the window."""
    uid = parse_package_uids(_entry(recorded)["package_uid_listing"]["output"], PACKAGE)
    if uid is None:
        pytest.fail(f"{RECORDS} no longer records the package's uid; re-record it")
    return {"system_server_pid": _system_server(recorded), "package_uids": uid}


def _window(recorded) -> list[Frame]:
    return frames(recorded.data(f"{RECORDS}.bin"))


def _records(window) -> list[logcat.LogRecord]:
    return logcat.parse_records(window if isinstance(window, bytes) else framed(window))


def _lines(window, pids: set[str] | None = None, floor: str = "D") -> list[str]:
    """The lines log_monitor prints for the records of ``pids`` at or above ``floor``.

    ``floor`` is the default severity filter's (error,warning,info,debug ->
    D): a ``*:D`` filterspec never let V through, and the host floor that
    replaced it does not either. Printed by common.logcat, whose printing is
    held against the device's own text in test_log_records.py.
    """
    order = logcat.PRIORITY_ORDER
    return [
        line
        for record in _records(window)
        if (pids is None or str(record.pid) in pids)
        and record.priority in order
        and order.index(record.priority) >= order.index(floor)
        for line in record.lines()
    ]


# --- D1: --severity is a closed vocabulary --------------------------------------


@pytest.mark.parametrize("text", ["warn", "error,warn", "bogus", "", " , "])
def test_d1_parse_severities_refuses_and_names_the_set(text):
    with pytest.raises(ValueError) as error:
        logcat.parse_severities(text)
    for name in logcat.SEVERITY_NAMES:
        assert name in str(error.value)


def test_d1_parse_severities_reads_the_documented_names():
    assert logcat.parse_severities(" Error , WARNING,error ") == ["error", "warning"]


def test_d1_min_priority_refuses_an_unknown_name_instead_of_skipping_it():
    """``["error", "warn"]`` resolved to ``E``: the warnings, silently gone."""
    with pytest.raises(ValueError, match="warn"):
        logcat.min_priority_for(["error", "warn"])


def test_d1_a_monitor_refuses_an_unknown_name():
    with pytest.raises(ValueError, match="warn"):
        _monitor(severity_filter=["warn"])


# --- N15: warnings count lines; unique_warnings counts messages -----------------


def _counts_in_text(stdout: str) -> dict[str, tuple[int, int]]:
    """``Errors: 7 (2 unique), Warnings: ...`` -> {"errors": (7, 2), ...}."""
    return {
        word.lower() + "s": (int(lines), int(unique))
        for word, lines, unique in re.findall(r"(Error|Warning)s: (\d+) \((\d+) unique\)", stdout)
    }


@pytest.mark.parametrize("mode", ["json", "text"])
def test_n15_warnings_count_lines_and_unique_warnings_count_messages(mode, toolchain, recorded):
    """N15: six recorded W lines reported ``"warnings": 2`` -- the signatures.

    The priority column is the ground truth: a reader counting W down it gets
    one number, and ``warnings`` must be that number. The deduplicated count
    is still worth having, so it is ``unique_warnings``, equal to the length of
    the deduplicated list -- which is the recording's W messages, read off its
    lines, the same message counted once whatever numbers it carries (a pid, a
    count), in the order they first appear.
    """
    window = recorded.data(f"{RECORDS}.bin")
    lines = _lines(window, floor="W")
    priorities = _column(lines, "priority")
    warned = priorities.count("W")
    listed: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = _THREADTIME.match(line)
        if not m or m["priority"] != "W":
            continue
        tag, _, message = line[m.end() :].partition(":")
        # The same message whatever its numbers, and however it is spaced.
        signature = " ".join(re.sub(r"\d+", "", f"{tag.strip()}:{message.strip()}").split())
        if signature not in seen:
            seen.add(signature)
            listed.append(f"[{tag.strip()}] {message.strip()}")
    if warned <= len(listed):
        pytest.fail("the recording has no repeated warning; N15 would be vacuous")

    _device(toolchain, recorded, window)
    argv = ["--severity", "error,warning", "--last", "1m"] + (["--json"] if mode == "json" else [])
    result = toolchain.run("log_monitor.py", *argv, ANDROID_EMU_LOG_JSON_CAP="1000")
    assert result.returncode == 0, result.stderr

    if mode == "json":
        payload = json.loads(result.stdout)
        stats = payload["statistics"]
        assert stats["warnings"] == warned
        assert stats["unique_warnings"] == len(listed) < warned
        assert payload["warnings"] == listed
        assert stats["errors"] == priorities.count("E") + priorities.count("F")
        assert stats["unique_errors"] == len(payload["errors"])
    else:
        counts = _counts_in_text(result.stdout)
        assert counts["warnings"][0] == warned, result.stdout
        assert counts["warnings"] == (warned, len(listed)), result.stdout
        assert counts["errors"][0] == priorities.count("E") + priorities.count("F")


def test_n15_errors_count_every_line_of_a_crash_loop(toolchain, recorded):
    """The same shape for errors: two crashes of many E lines are that many errors.

    Only the line count can be compared with what ``app_state_capture`` (D3)
    and a grep of the priority column report; the unique count says how many
    distinct messages that was. Recorded: logcat_crash_buffer_records, two
    crashes of the fixture app, each one record.
    """
    window = recorded.data("logcat_crash_buffer_records.bin")
    errors = sum(1 for p in _column(_lines(window, floor="E"), "priority") if p in "EF")
    _device(toolchain, recorded, window)

    result = toolchain.run("log_monitor.py", "--severity", "error", "--last", "5m", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["statistics"]["errors"] == errors
    assert payload["statistics"]["unique_errors"] == len(payload["errors"]) < errors


# --- D4: the app is every process it had in the window --------------------------


def test_parse_package_pids_reads_the_apps_processes_from_the_log(recorded):
    """Each pid `pidof` read while that process was alive, and no other.

    The force-stopped process is the case a crash record cannot cover: only
    ActivityManager's `Start proc` / `Killing` records say it was the app's.
    """
    pids = set(_restart_pids(recorded).values())
    checked = logcat.parse_package_pids(_records(_window(recorded)), PACKAGE, **_oracles(recorded))
    assert checked == pids, "system_server's own records must pass the producer check"


@pytest.mark.parametrize(
    ("statement", "ending"),
    [("Start proc ", ALIVE), ("Killing ", STOPPED)],
    ids=["start", "force-stop"],
)
def test_each_statement_names_the_pid_on_its_own(statement, ending, recorded):
    """Each of the two uid-carrying ways system_server names an app's process, alone.

    Every launch logs `Start proc`; the force-stop logs `Killing`. (`has died`
    carries no uid and only ends a life already confirmed; the crash's own
    AndroidRuntime `Process:` record names nothing alone -- both in
    tests/test_app_window_producer.py.)
    """
    chosen = [
        f
        for f in _window(recorded)
        if f.tag == "ActivityManager" and f.message.startswith(statement)
    ]
    if not chosen:
        pytest.fail(f"the recording has no {statement.strip()!r} record")
    pids = _restart_pids(recorded)
    found = logcat.parse_package_pids(_records(chosen), PACKAGE, **_oracles(recorded))
    assert pids[ending] in found, [f.message for f in chosen]
    assert found <= set(pids.values()), found


def test_parse_package_pids_finds_nothing_in_crash_records_alone(recorded):
    """The crash buffer has no system_server record, and a crash record is no source.

    Any app can write AndroidRuntime's `Process: <pkg>, PID: <its pid>` for
    another package (Codex review of #27, ruling A); a real crash is named by
    system_server's `Start proc` instead.
    """
    crash = _records(recorded.data("logcat_crash_buffer_records.bin"))
    if not crash:
        pytest.fail("the crash buffer recording no longer holds a crash")
    assert logcat.parse_package_pids(crash, PACKAGE, **_oracles(recorded)) == set()


def test_parse_package_pids_does_not_claim_a_longer_package_name(recorded):
    """``com.example.compose`` is not ``com.example.composefixture``."""
    found = logcat.parse_package_pids(
        _records(_window(recorded)), PACKAGE[:-7], **_oracles(recorded)
    )
    assert found == set()


def test_parse_package_pids_reads_only_the_tag_that_owns_the_statement(recorded):
    """The same words under another tag are an app's prose, not system_server's record.

    Derived from the recording by substituting the tag for another one the
    same process logs under in it.
    """
    window = _window(recorded)
    start = next(
        (f for f in window if f.tag == "ActivityManager" and f.message.startswith("Start proc")),
        None,
    )
    if start is None or not any(f.tag == "ActivityTaskManager" for f in window):
        pytest.fail("the recording no longer has the two tags this substitution needs")
    assert logcat.parse_package_pids(_records([start]), PACKAGE, **_oracles(recorded))
    forged = start.replace(tag="ActivityTaskManager")
    assert logcat.parse_package_pids(_records([forged]), PACKAGE, **_oracles(recorded)) == set()


def test_parse_threadtime_reads_pid_and_priority_and_skips_separators(recorded):
    lines = recorded.lines("logcat_app_restart_usec")
    expected = [(m["pid"], m["priority"]) for m in map(_THREADTIME.match, lines) if m]
    assert [p for line in lines if (p := logcat.parse_threadtime(line))] == expected
    separators = [line for line in lines if line.startswith("--------- beginning of")]
    if not separators:
        pytest.fail("the recording no longer holds a buffer separator")
    assert all(logcat.parse_threadtime(line) is None for line in separators)


def test_d4_an_app_filter_is_never_a_device_side_pid(toolchain, recorded):
    """``--pid`` is one process; ``--app`` is every process the app had.

    It sent the one pid ``pidof`` named, which after a restart is the NEW
    process. Asked of the command actually sent, with ``pidof`` answering.
    """
    alive = _restart_pids(recorded)[ALIVE]
    _device(
        toolchain,
        recorded,
        recorded.data(f"{RECORDS}.bin"),
        pidof=Rule(("shell", "pidof"), stdout=alive),
        system_server=_system_server(recorded),
    )
    result = toolchain.run("log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json")
    assert result.returncode == 0, result.stderr
    sent = [call for call in toolchain.calls("adb") if "logcat" in call]
    assert sent, "the device was never asked for logs"
    assert not [arg for call in sent for arg in call if arg.startswith("--pid")], sent


def test_d4_a_force_stopped_process_stays_in_the_apps_window(toolchain, tmp_path, recorded):
    """D4 beyond a crash: a force-stopped process leaves no crash record.

    The window -- the JSON's ``sample_logs`` and the saved log alike -- is
    every record of every process `pidof` saw, crashed, stopped and alive, in
    order, and nothing else: not system_server or the other processes whose
    records name the package. The same window app_state_capture saves for
    this recording.
    """
    pids = set(_restart_pids(recorded).values())
    window = recorded.data(f"{RECORDS}.bin")
    unrelated = {str(record.pid) for record in _records(window)} - pids
    if not unrelated:
        pytest.fail("the window has no record from another process; exclusion is untested")
    alive = _restart_pids(recorded)[ALIVE]
    _device(
        toolchain,
        recorded,
        window,
        pidof=Rule(("shell", "pidof"), stdout=alive),
        system_server=_system_server(recorded),
    )

    saved = tmp_path / "logs"
    result = toolchain.run(
        "log_monitor.py",
        "--app",
        PACKAGE,
        "--last",
        "5m",
        "--output",
        str(saved),
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["sample_logs"] == _lines(window, pids)
    assert payload["app_pids"] == sorted(pids, key=int)
    logs = sorted(saved.glob("*.log"))
    if len(logs) != 1:
        pytest.fail(f"expected one saved log under {saved}, found {logs}")
    assert logs[0].read_bytes().decode("utf-8").split("\n") == _lines(window, pids)


def _without_start_of(recorded, pid: str) -> list[Frame]:
    """The restart window as if ``pid`` had started before the window opened."""
    window = _window(recorded)
    starts = [f for f in window if f.message.startswith(f"Start proc {pid}:")]
    if len(starts) != 1:
        pytest.fail(f"the recording no longer starts {pid} exactly once")
    return [f for f in window if f not in starts]


def test_d4_a_process_already_running_when_the_window_opened_is_kept_whole(toolchain, recorded):
    """Its only naming record, `Killing`, comes AFTER all of its own records.

    A window that opens mid-life has no `Start proc` for that process. Derived
    from the recording by deleting the force-stopped process's `Start proc`
    record, so system_server's `Killing <pid>` is the one record that names
    it, and every record of the process precedes it. A window filtered as it
    is read -- or a held window parsed without system_server's pid -- loses
    them all.

    Run with the live hold capped at 5 lines: a historical read is not a live
    one, holds the whole window, and must keep all of them whatever that cap.
    """
    pids = _restart_pids(recorded)
    stopped = pids[STOPPED]
    window = _without_start_of(recorded, stopped)
    killing = next(i for i, f in enumerate(window) if f.message.startswith(f"Killing {stopped}:"))
    first_own = next(i for i, f in enumerate(window) if f.pid == stopped)
    if first_own > killing:
        pytest.fail("the stopped process logged nothing before its Killing record")
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )

    result = toolchain.run(
        "log_monitor.py",
        "--app",
        PACKAGE,
        "--last",
        "5m",
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
        ANDROID_EMU_LOG_PENDING_CAP="5",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _lines(window, set(pids.values()))


def _historical_and_live(toolchain, *app_args: str) -> tuple[dict, dict]:
    """The same device read with ``--last`` and with a live ``--duration``.

    The stub's logcat prints its recording and exits, so the live read ends at
    the end of the recording -- the same records a historical read sees. (A
    live read that ends by itself asks ``get-state`` again; the device still
    answers ``device``, so what it read is the capture.)
    """
    runs = []
    for window in (("--last", "5m"), ("--duration", "30s")):
        result = toolchain.run(
            "log_monitor.py", *app_args, *window, "--json", ANDROID_EMU_LOG_TAIL="1000"
        )
        assert result.returncode == 0, f"{window}: {result.stderr}"
        runs.append(json.loads(result.stdout))
    return runs[0], runs[1]


def _until_the_crash_is_over(recorded) -> list[Frame]:
    """The restart window up to the crashed process's `has died`: no relaunch yet.

    A prefix of the recording, so every record is as recorded; it ends where
    an agent looking at a crash that was not restarted would read.
    """
    crashed = _restart_pids(recorded)[CRASHED]
    window = _window(recorded)
    died = next(
        (
            i
            for i, f in enumerate(window)
            if f.message.startswith(f"Process {PACKAGE} (pid {crashed}) has died")
        ),
        None,
    )
    if died is None:
        pytest.fail("the recording no longer holds the crashed process's `has died`")
    return window[: died + 1]


def test_d4_parity_a_crash_read_live_counts_what_last_counts(toolchain, recorded):
    """The recorded crash, read live and with ``--last``: the same errors.

    Codex measured 36 errors live where ``--last`` counted 39 (review of #27,
    on the crash loop, when a crash line could still name its pid). Under
    ruling A a crash is named by system_server's uid-carrying `Start proc` for
    its pid, recorded here before the crash, so nothing waits: both reads keep
    the whole trace. (A crash whose start predates the read is named by no
    uid-carrying record -- `has died` has none -- and is in neither read; A3
    below: it is reported as left out.)
    """
    pids = _restart_pids(recorded)
    window = recorded.data(f"{RECORDS}.bin")
    _device(
        toolchain,
        recorded,
        window,
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    historical, live = _historical_and_live(toolchain, "--app", PACKAGE, "--severity", "error")

    app = set(pids.values())
    expected = sum(1 for p in _column(_lines(window, app, "E"), "priority") if p in "EF")
    if not expected:
        pytest.fail("the window holds no E record of the app; parity would be vacuous")
    assert historical["statistics"]["errors"] == expected
    assert live["statistics"]["errors"] == expected
    assert sorted(live["sample_logs"]) == sorted(historical["sample_logs"])


def test_d4_parity_a_process_named_only_when_it_dies(toolchain, recorded):
    """A process already running when the read began is named only by `Killing`.

    Derived from the recording by deleting its `Start proc` record. Every one
    of its records arrives before the record that names it, so a live read
    holds them all back and replays them.
    """
    pids = _restart_pids(recorded)
    window = _without_start_of(recorded, pids[STOPPED])
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    historical, live = _historical_and_live(toolchain, "--app", PACKAGE)

    assert historical["sample_logs"] == _lines(window, set(pids.values()))
    assert sorted(live["sample_logs"]) == sorted(historical["sample_logs"])
    assert live["statistics"] == historical["statistics"]


def test_d4_the_live_hold_is_bounded(toolchain, recorded):
    """Lines older than ``ANDROID_EMU_LOG_PENDING_CAP`` are dropped, not held without limit.

    The window of the test above: every record of the stopped process waits
    for its `Killing` record. Only records of the package's uid are held (one
    under any other uid can never be the app's), so the hold is the stopped
    process's own. With a cap of 5 lines, at most five of them can still be
    waiting when `Killing` arrives. Read at ``--severity verbose``, so every
    line of the process that survives the hold is in the output, where it is
    counted -- independently of what the reader says it dropped.
    """
    pids = _restart_pids(recorded)
    stopped = pids[STOPPED]
    window = _without_start_of(recorded, stopped)
    own = len(_lines(window, {stopped}, floor="V"))
    if own <= 5:
        pytest.fail(f"the stopped process logged only {own} lines; the cap is untested")
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py",
        "--app",
        PACKAGE,
        "--severity",
        "verbose",
        "--duration",
        "30s",
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
        ANDROID_EMU_LOG_PENDING_CAP="5",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    kept = _column(payload["sample_logs"], "pid").count(stopped)
    assert 0 < kept <= 5 < own, f"{kept} of the stopped process's {own} lines survived a cap of 5"
    # They are its newest: the hold drops the oldest first.
    assert [line for line in payload["sample_logs"] if line.split()[2] == stopped] == _lines(
        window, {stopped}, floor="V"
    )[-kept:]

    # ...and the loss is counted, in the JSON and on stderr (Codex review v2),
    # and the window says it is incomplete (A3).
    attribution = payload["attribution"]
    assert attribution["pending_evicted"] == own - kept
    assert f"{own - kept} line(s) of {PACKAGE}'s processes were dropped" in result.stderr
    assert attribution["complete"] is False
    assert attribution["unattributed_at_end"] == 0


def test_d4_a_historical_read_reports_what_decided_it(toolchain, recorded):
    """The JSON's `attribution` block: what the window rests on (Codex review v2).

    Everything known, on the recorded window: system_server's pid and the
    package's uid as recorded, the live pid from `pidof` (uid-checked), all
    three processes named by the log, nothing held back, nothing of the
    package's uid left out -- complete.
    """
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        recorded.data(f"{RECORDS}.bin"),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    result = toolchain.run("log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json")
    assert result.returncode == 0, result.stderr
    attribution = json.loads(result.stdout)["attribution"]
    oracles = _oracles(recorded)
    assert attribution == {
        "system_server_pid": oracles["system_server_pid"],
        "system_server": "known",
        "package_uids": sorted(oracles["package_uids"]),
        "package_uids_status": "known",
        "pids_from_pidof": [pids[ALIVE]],
        "pidof_rejected": [],
        "pids_from_log": sorted(pids.values(), key=int),
        "pending_evicted": 0,
        "unattributed_at_end": 0,
        "unresolved_lines": 0,
        "unresolved_pids": [],
        "complete": True,
    }


def _another_process_first(recorded, ending: str = STOPPED) -> tuple[list[Frame], list[str], str]:
    """The restart window where one app process's pid was another process's first.

    Derived: the records of one unrelated process logged before the `Start
    proc` of the app process that ended as ``ending`` get that process's pid
    as their writer (pid reuse: the pid was that process's, then the app's).
    Returns the window, the substituted records' lines, and the pid.
    """
    pid = _restart_pids(recorded)[ending]
    window = _window(recorded)
    start = next(i for i, f in enumerate(window) if f.message.startswith(f"Start proc {pid}:"))
    app = set(_restart_pids(recorded).values())
    other = next(
        (f.pid for f in window[:start] if f.pid not in app and f.pid != _system_server(recorded)),
        None,
    )
    if other is None:
        pytest.fail("no other process logs before the app process starts")
    for index in range(start):
        if window[index].pid == other:
            window[index] = window[index].replace(pid=pid)
    reused = [
        line for f in window[:start] if f.pid == pid for line in _lines(framed([f]), floor="V")
    ]
    return window, reused, pid


def _split_after_start(window: list[Frame], pid: str) -> tuple[bytes, bytes]:
    """``window`` cut after ``pid``'s `Start proc`: what was buffered, what came after.

    A process ``pidof`` names when a live read begins was started before it,
    so its `Start proc` is already in the device's buffer.
    """
    start = next(i for i, f in enumerate(window) if f.message.startswith(f"Start proc {pid}:"))
    return framed(window[: start + 1]), framed(window[start + 1 :])


@pytest.mark.parametrize("ending", [STOPPED, ALIVE], ids=["stopped", "running"])
@pytest.mark.parametrize("read", [("--last", "5m"), ("--duration", "30s")], ids=["last", "live"])
def test_d4_a_reused_pid_is_the_apps_only_inside_its_life(read, ending, toolchain, recorded):
    """One pid, first another process's, then the app's: only the app's life counts.

    The pid of an app process logs as another process before its `Start
    proc`; those records are not the app's, in a historical read and live.
    The process running when the read began is the hard case live (Codex
    review v3): a live read keeps its records from the first one it sees, so
    it must not see the buffer's older records at all -- it starts at the end
    (`-T 1`). For it, the device holds everything up to its `Start proc` in
    its buffer; a dump reads that whole, a live read only from the end.
    """
    window, reused, pid = _another_process_first(recorded, ending)
    pids = _restart_pids(recorded)
    if ending == ALIVE:
        buffered, after = _split_after_start(window, pid)
    else:
        # The read began before this process started: all of it arrives live.
        buffered, after = b"", framed(window)
    _device(
        toolchain,
        recorded,
        after,
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
        buffered=buffered,
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, *read, "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    kept = json.loads(result.stdout)["sample_logs"]
    assert not set(reused) & set(kept), "records of the pid's earlier owner are in the window"
    assert pid in _column(kept, "pid"), "the app's own life under that pid is missing"
    reads = [call for call in toolchain.calls("adb") if "logcat" in call]
    tails = [call[call.index("-T") + 1] for call in reads if "-T" in call]
    assert tails == (["1"] if read[0] == "--duration" else [])


def _killed_last(recorded) -> list[Frame]:
    """A window where the stopped process is named only at its very end.

    Derived: its `Start proc` deleted (it was running when the read began) and
    its `Killing` record moved to the end, after the live process's records --
    so a live read keeps the live process's records first and the stopped
    process's, which are OLDER, only when the window ends.
    """
    stopped = _restart_pids(recorded)[STOPPED]
    window = _without_start_of(recorded, stopped)
    killing = [f for f in window if f.message.startswith(f"Killing {stopped}:")]
    if len(killing) != 1:
        pytest.fail("the recording no longer kills the stopped process once")
    return [f for f in window if f not in killing] + killing


def test_d4_a_live_read_stores_lines_in_input_order(toolchain, tmp_path, recorded):
    """Held records are decided late; the saved log keeps the device's order.

    Codex (review v2) reproduced replayed lines stored after newer ones, which
    also changed what survived the `sample_logs` tail. The saved log of a live
    read must equal the saved log of ``--last`` over the same window.
    """
    window = _killed_last(recorded)
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    saved = {}
    for name, read in (("last", ("--last", "5m")), ("live", ("--duration", "30s"))):
        out = tmp_path / name
        result = toolchain.run(
            "log_monitor.py", "--app", PACKAGE, *read, "--output", str(out), "--json"
        )
        assert result.returncode == 0, result.stderr
        logs = sorted(out.glob("*.log"))
        if len(logs) != 1:
            pytest.fail(f"expected one saved log under {out}, found {logs}")
        saved[name] = logs[0].read_text(encoding="utf-8").splitlines()
    if pids[STOPPED] not in _column(saved["last"], "pid"):
        pytest.fail("the stopped process is not in the --last window; ordering is untested")
    assert saved["live"] == saved["last"]


def test_d4_follow_marks_a_held_line_when_it_prints_it(toolchain, recorded):
    """`--follow` prints a held record when it is decided -- after newer ones -- marked."""
    window = _killed_last(recorded)
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    result = toolchain.run("log_monitor.py", "--app", PACKAGE, "--follow")
    assert result.returncode == 0, result.stderr
    marked = [line for line in result.stdout.splitlines() if line.startswith("[held back] ")]
    unmarked = [line for line in result.stdout.splitlines() if not line.startswith("[held back] ")]
    assert marked, "no held line was marked"
    assert set(_column([m.removeprefix("[held back] ") for m in marked], "pid")) == {pids[STOPPED]}
    assert pids[STOPPED] not in _column(unmarked, "pid")


def test_d4_a_live_capture_follows_the_app_through_its_restarts(toolchain, recorded):
    """Streaming: not running at the start, three processes by the end.

    Each start is announced before the new process's first record
    (recorded), so a live read learns every pid in time. ``pidof`` answers as
    recorded for an app that is not running; the stub's logcat then exits,
    ending the read.
    """
    pids = _restart_pids(recorded)
    window = recorded.data(f"{RECORDS}.bin")
    _device(
        toolchain,
        recorded,
        window,
        pidof=_not_running(recorded),
        system_server=_system_server(recorded),
    )

    result = toolchain.run(
        "log_monitor.py",
        "--app",
        PACKAGE,
        "--duration",
        "30s",
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _lines(window, set(pids.values()))


def test_d4_follow_prints_only_the_apps_lines(toolchain, recorded):
    pids = set(_restart_pids(recorded).values())
    _device(
        toolchain,
        recorded,
        recorded.data(f"{RECORDS}.bin"),
        pidof=_not_running(recorded),
        system_server=_system_server(recorded),
    )
    result = toolchain.run("log_monitor.py", "--app", PACKAGE, "--follow")
    assert result.returncode == 0, result.stderr
    printed = set(_column(result.stdout.splitlines(), "pid"))
    assert printed and printed <= pids, printed


def test_d4_a_crashed_app_that_is_not_running_keeps_its_window(toolchain, recorded):
    """The case an agent reaches for after a crash: nothing is running any more.

    Refusing here would hide the crash; widening would bury it in every other
    process. The window is the restart recording up to the crashed process's
    `has died` -- the moment after the crash, before any relaunch -- and
    system_server's `Start proc` / `has died` name it. The whole crash, every
    line in order, is the assertion.
    """
    window = _until_the_crash_is_over(recorded)
    crashed = _restart_pids(recorded)[CRASHED]
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=_not_running(recorded),
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _lines(window, {crashed})


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_d4_an_app_with_no_process_in_the_window_is_refused(json_mode, toolchain, recorded):
    """Not running, and nothing in the window names it: refused, not widened.

    Before D4 this read every process on the device and called it the app's.
    The window is the crash buffer: the app's own crashes, which name nothing.
    """
    _device(
        toolchain,
        recorded,
        recorded.data("logcat_crash_buffer_records.bin"),
        pidof=_not_running(recorded),
    )
    argv = ["--app", PACKAGE, "--last", "1m"] + (["--json"] if json_mode else [])
    result = toolchain.run("log_monitor.py", *argv)
    assert_refusal(result, json_mode=json_mode, naming=(PACKAGE,), status=1)


# Exit 127, nothing printed: what a shell without `pidof` exits. Not the
# recorded "not running" answer (exit 1), and nothing is printed because
# anything printed would be invented.
_PIDOF_CANNOT_RUN = Rule(("shell", "pidof"), exit=127)


def test_d4_a_failed_pidof_alone_is_not_a_refusal(toolchain, recorded):
    """The window names the app's crashed process, so it is read.

    The rule app_state_capture follows too: `pidof` is one witness, and
    system_server's records are another; only when neither names a process is
    the capture refused.
    """
    window = _until_the_crash_is_over(recorded)
    crashed = _restart_pids(recorded)[CRASHED]
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=_PIDOF_CANNOT_RUN,
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _lines(window, {crashed})


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_d4_a_failed_pidof_and_a_window_without_the_app_is_refused(json_mode, toolchain, recorded):
    """...and the refusal says `pidof` failed, not that the app was not running."""
    _device(
        toolchain,
        recorded,
        recorded.data("logcat_crash_buffer_records.bin"),
        pidof=_PIDOF_CANNOT_RUN,
    )
    argv = ["--app", PACKAGE, "--last", "1m"] + (["--json"] if json_mode else [])
    result = toolchain.run("log_monitor.py", *argv)
    assert_refusal(result, json_mode=json_mode, naming=("`pidof` failed",), status=1)


def test_d4_the_floor_still_reads_the_records_that_name_the_apps_processes(toolchain, recorded):
    """``--severity error``: the I records naming the app's processes still decide.

    ``*:E`` on the device used to need ``ActivityManager:I`` beside it, or the
    `Start proc` / `Killing` records never arrived and the crashed and stopped
    processes were lost. The floor is now applied on the host, after
    attribution: all three processes are named, and only E/F lines are kept.
    """
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        recorded.data(f"{RECORDS}.bin"),
        pidof=_not_running(recorded),
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--severity", "error", "--last", "5m", "--json"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["attribution"]["pids_from_log"] == sorted(pids.values(), key=int)
    assert set(_column(payload["sample_logs"], "priority")) <= {"E", "F"}
    assert payload["sample_logs"], "the crash's E lines are missing"


# --- A3: a window that leaves the app's lines out says so -----------------------


@pytest.mark.parametrize("read", [("--last", "5m"), ("--duration", "30s")], ids=["last", "live"])
def test_a3_a_crash_whose_start_predates_the_window_is_reported(read, toolchain, recorded):
    """The crash is left out -- and the result says so, with how much.

    Derived: the restart window without the crashed process's `Start proc`
    (the window opened after the process started). Nothing uid-carrying names
    it -- `has died` carries no uid -- so its records stay out of the window.
    They were silently gone: ``errors: 0``, every oracle ``known``, every loss
    counter zero (review of v0.8.0, A3). logd stamped them with the package's
    uid, so the result now counts them, names their pid, and calls itself
    incomplete, in the JSON, on stderr and in the text summary.
    """
    pids = _restart_pids(recorded)
    crashed = pids[CRASHED]
    window = _without_start_of(recorded, crashed)
    left_out = len(_lines(window, {crashed}, floor="V"))
    if not left_out or "E" not in _column(_lines(window, {crashed}), "priority"):
        pytest.fail("the crashed process logged no error; the omission would be vacuous")
    _device(
        toolchain,
        recorded,
        framed(window),
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, *read, "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    attribution = payload["attribution"]
    assert crashed not in payload["app_pids"]
    assert attribution.get("complete") is False, attribution
    assert attribution.get("unresolved_pids") == [crashed]
    assert attribution.get("unresolved_lines") == left_out
    assert f"Incomplete: {left_out} line(s)" in result.stderr

    text = toolchain.run("log_monitor.py", "--app", PACKAGE, *read, ANDROID_EMU_LOG_TAIL="1000")
    assert text.returncode == 0, text.stderr
    assert "Attribution: INCOMPLETE" in text.stdout, text.stdout


# --- A4: a device that is not there is never a quiet capture -------------------


def _absent_device(toolchain, recorded, *, logcat: Rule) -> None:
    """adb aimed at a serial that is not attached: get-state says so, logcat as given.

    ``get-state`` answers as recorded for that serial (adb_device_not_found:
    `error: device '...' not found`, exit 1).
    """
    toolchain.install(
        "adb",
        [
            Rule(
                ("get-state",),
                stderr=recorded.text("adb_device_not_found"),
                exit=_recorded_exit(recorded, "adb_device_not_found"),
            ),
            logcat,
        ],
    )


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a4_a_live_read_of_a_device_that_is_not_there_is_refused(json_mode, toolchain, recorded):
    """No ``--serial``, no device: adb waits, and the deadline made it a quiet capture.

    ``adb logcat`` aimed at a device that is not attached prints ``- waiting
    for device -`` and waits (recorded: adb_waiting_for_device; the stub
    hangs as it does). With no --serial nothing was checked first, so
    ``--duration`` ended on schedule and reported success with zero lines.
    The serial adb targets comes from ANDROID_SERIAL here, as it would for a
    caller that passed none. Now ``get-state`` is asked first: exit 3, and
    logcat is never started.
    """
    _absent_device(
        toolchain,
        recorded,
        logcat=Rule(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
    )
    argv = ["--duration", "2s"] + (["--json"] if json_mode else [])
    result = toolchain.run("log_monitor.py", *argv, ANDROID_SERIAL="no-such-serial-xyz")
    assert_refusal(result, json_mode=json_mode, naming=("not attached", "device"), status=3)
    assert not [call for call in toolchain.calls("adb") if "logcat" in call]


def test_a4_every_mode_asks_first(toolchain, recorded):
    """``--last`` and ``--follow`` too: the same question before the same read."""
    _absent_device(
        toolchain,
        recorded,
        logcat=Rule(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
    )
    for mode in (["--last", "1m"], ["--follow"], ["--app", PACKAGE, "--duration", "2s"]):
        result = toolchain.run("log_monitor.py", *mode, "--json", ANDROID_SERIAL="no-such-xyz")
        assert_refusal(result, json_mode=True, naming=("not attached",), status=3)


def _gone_after_one_read(toolchain, recorded, logcat: Rule) -> None:
    """A device that answers get-state once, streams, and is then gone.

    The first ``get-state`` is the recorded attached answer; every later one
    is the recorded answer for a serial that is not attached.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
            Rule(
                ("get-state",),
                stderr=recorded.text("adb_device_not_found"),
                exit=_recorded_exit(recorded, "adb_device_not_found"),
            ),
            logcat,
        ],
    )


@pytest.mark.parametrize("status", [0, 255], ids=["older-adb-exit-0", "current-adb-exit-255"])
def test_a4_a_live_read_that_ends_by_itself_asks_whether_the_device_went(
    status, toolchain, recorded
):
    """A live logcat ends only when something went away; if it was the device, say so.

    The stream delivers records, then ends by itself -- exit 0 (older adb on a
    transport EOF) or 255 (current adb), nothing on stderr. It reported
    success, or a generic exit 1 with no device check. ``get-state`` is asked
    again, the device is gone: exit 3.
    """
    _gone_after_one_read(
        toolchain,
        recorded,
        Rule(("logcat",), stdout=recorded.data(f"{RECORDS}.bin"), exit=status),
    )
    result = toolchain.run("log_monitor.py", "--duration", "30s", "--json")
    assert_refusal(result, json_mode=True, naming=("not attached",), status=3)
    assert [call[-1] for call in toolchain.calls("adb") if "get-state" in call] == [
        "get-state",
        "get-state",
    ]


def test_a4_a_live_read_that_ends_while_the_device_is_still_there_is_the_capture(
    toolchain, recorded
):
    """The control: asked again, the device answers -- what was read stands, and is said."""
    _device(toolchain, recorded, recorded.data(f"{RECORDS}.bin"))
    result = toolchain.run("log_monitor.py", "--duration", "30s", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["statistics"]["total_lines"] > 0
    assert "ended by itself" in result.stderr


# --- a stream cut inside a record is not a complete capture (review of PR #35) ---


def test_a_dump_cut_inside_a_record_is_refused_not_half_counted(toolchain, recorded):
    """The recorded two-crash dump, short by its last byte: refused, not 13 errors of 26.

    A dump always ends at a record boundary, so one that does not is a
    capture that lost data on the way; every truncation used to be read as a
    clean end. Derived: logcat_crash_buffer_records without its final byte.
    The device is still attached (asked again), so it is exit 1.
    """
    window = recorded.data("logcat_crash_buffer_records.bin")
    _device(toolchain, recorded, window[:-1])
    result = toolchain.run("log_monitor.py", "--severity", "error", "--last", "5m", "--json")
    assert_refusal(result, json_mode=True, naming=("inside a record",), status=1)
    asked = [call for call in toolchain.calls("adb") if "get-state" in call]
    assert len(asked) == 2, "the device was not asked again after the cut"


def test_a_live_read_cut_by_itself_with_the_device_gone_is_exit_three(toolchain, recorded):
    """The stream ends inside a record and the device is gone: that is the answer."""
    window = recorded.data("logcat_crash_buffer_records.bin")
    _gone_after_one_read(toolchain, recorded, Rule(("logcat",), stdout=window[:-1]))
    result = toolchain.run("log_monitor.py", "--duration", "30s", "--json")
    assert_refusal(result, json_mode=True, naming=("not attached",), status=3)


def test_a_live_read_cut_by_itself_with_the_device_there_is_refused(toolchain, recorded):
    window = recorded.data("logcat_crash_buffer_records.bin")
    _device(toolchain, recorded, window[:-1])
    result = toolchain.run("log_monitor.py", "--app", PACKAGE, "--duration", "30s")
    assert_refusal(result, json_mode=False, naming=("inside a record",), status=1)


# --- A5: adb's error is read line by line ---------------------------------------


def test_a5_adbs_error_after_a_banner_is_still_a_device_error(toolchain, recorded):
    """A banner first, then adb's own error: still the device, still exit 3.

    The stream's stderr was joined with spaces before it was classified, so
    the error no longer began a line and the anchored classifier (#29) missed
    it: a generic exit 1. Two recorded adb stderr outputs in sequence -- the
    waiting banner, then `adb: device '...' not found` -- with the stream's
    exit 1.
    """
    stderr = recorded.text("adb_waiting_for_device") + recorded.text("adb_shell_device_not_found")
    toolchain.install(
        "adb",
        [
            Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
            Rule(("logcat",), stderr=stderr, exit=1),
        ],
    )
    result = toolchain.run("log_monitor.py", "--duration", "30s", "--json")
    assert_refusal(result, json_mode=True, naming=("not attached",), status=3)


# --- A8: the fake device tails records, as logd does ----------------------------


def _ask_the_fake(toolchain, *argv: str) -> bytes:
    return subprocess.run(
        [str(toolchain.bin / "adb"), *argv],
        env=toolchain.env(),
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout


def test_a8_the_fake_tails_text_by_record(toolchain, recorded):
    """`-T 1` over a buffer whose last record is a crash: every line of it.

    Recorded in one run: the crash buffer (two crashes) and the device's own
    `-T 1` of it. The fake tailed physical lines, so it answered the crash's
    last line only.
    """
    toolchain.install("adb", [Rule(("logcat",), buffered=recorded.text("logcat_crash_buffer"))])
    tail = _ask_the_fake(toolchain, "logcat", "-d", "-T", "1")
    assert tail.decode("utf-8") == recorded.text("logcat_crash_tail")
    whole = _ask_the_fake(toolchain, "logcat", "-d")
    assert whole.decode("utf-8") == recorded.text("logcat_crash_buffer")


def test_a8_the_fake_tails_records_by_record(toolchain, recorded):
    """The same over records: `-T 1` is the buffer's last record, byte for byte."""
    toolchain.install(
        "adb",
        [Rule(("logcat",), buffered=recorded.data("logcat_crash_buffer_records.bin"))],
    )
    tail = _ask_the_fake(toolchain, "logcat", "-d", "-B", "-T", "1")
    assert tail == recorded.data("logcat_crash_tail_records.bin")
    assert len(frames(tail)) == 1 and len(_lines(tail, floor="V")) > 1
