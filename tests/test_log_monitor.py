"""Device-free tests for log_monitor's pure arg->command mapping and caps.

Covers the two curated deltas:
  1. ``--last DURATION`` historical window built via ``logcat -d -t "<timestamp>"``.
  2. Output caps made env-configurable via ``ANDROID_EMU_LOG_*`` (defaults preserved).

And, at the end, the v0.8.0 Inc 1 fixes driven through the real CLI against a
fake adb that answers with recorded output: D1 (``--severity``), N15 (line
counts beside unique counts) and D4 (an app filter that survives a restart).

All adb/subprocess interaction is mocked, so these run without a device.
"""

from __future__ import annotations

import importlib
import io
import json
import re
from datetime import datetime

import log_monitor
import pytest
from fake_toolchain import FakeToolchain, Rule, assert_refusal, uid_rules

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
    # threadtime, not time: parse_logcat_line needs the PID/TID columns.
    # This previously asserted "time", locking in a parser that matched
    # nothing (A1).
    assert cmd[cmd.index("-v") + 1] == "threadtime"
    assert cmd.index("logcat") == 1


def test_build_command_includes_serial():
    cmd = _monitor(device_serial="emulator-5554").build_logcat_command()
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]


@pytest.mark.parametrize(
    ("severities", "kept"),
    [(["error"], True), (["error", "warning"], True), (None, False), (["info"], False)],
    ids=["error", "error,warning", "default", "info"],
)
def test_an_app_filter_keeps_the_lines_that_name_its_processes(severities, kept):
    """``ActivityManager:I`` rides under a floor above I, ahead of ``*:P``.

    The lines that say which pids were the app's are I (recorded:
    logcat_app_restart_usec), so ``*:W`` alone would drop them and a killed
    process's lines would go with them. At I and below they already arrive.
    """
    cmd = _monitor(app_package="com.myapp", severity_filter=severities).build_logcat_command()
    floor = next(part for part in cmd if part.startswith("*:"))
    if kept:
        assert cmd.index("ActivityManager:I") < cmd.index(floor), cmd
    else:
        assert "ActivityManager:I" not in cmd, cmd


def test_no_process_term_without_an_app():
    cmd = _monitor(severity_filter=["error"]).build_logcat_command()
    assert [part for part in cmd if ":" in part and not part.startswith("-")] == ["*:E"]


# --- delta 1: --last historical window (logcat -d -t) ---------------------------


def test_build_command_last_window_uses_dump_and_tail():
    now = datetime(2026, 6, 17, 12, 30, 0)
    cmd = _monitor().build_logcat_command(last_minutes=5, now=now)
    # Historical window must dump-and-exit (-d) and tail from a timestamp (-t).
    assert "-d" in cmd
    t_index = cmd.index("-t")
    # Timestamp is "MM-DD HH:MM:SS.000" for now - 5 minutes => 12:25:00.
    assert cmd[t_index + 1] == "06-17 12:25:00.000"


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


def test_severity_filter_appended_to_command():
    cmd = _monitor(severity_filter=["error"]).build_logcat_command()
    assert "*:E" in cmd


# --- stream_logs integration: --last builds the historical command --------------


def test_stream_logs_last_window_runs_dump_command(monkeypatch):
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured["cmd"] = cmd
            # Empty stream: readline() returns b"" immediately (EOF).
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

    # No app package -> _resolve_app_pid returns None without calling adb.
    monkeypatch.setattr(log_monitor.subprocess, "Popen", _FakePopen)

    ok = _monitor().stream_logs(last_minutes=5)
    assert ok is True
    assert "-d" in captured["cmd"]
    assert "-t" in captured["cmd"]


def _pidof_answers(monkeypatch, returncode: int, stdout: str) -> None:
    # adb now goes through common.adb_exec, so that is where the fake belongs.
    import subprocess as real_subprocess

    from common import adb_exec

    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: real_subprocess.CompletedProcess(cmd, returncode, stdout, ""),
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

# The threadtime layout, read for its PRIORITY and PID columns only.
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
    logcat_text: str,
    *,
    pidof: Rule | None = None,
    system_server: str | None = None,
    uids: bool = True,
    buffered: str = "",
) -> None:
    """A device that lists itself, answers ``pidof`` as told, and replays ``logcat_text``.

    ``buffered`` is what the device already held when a read began: a dump
    gets it whole before ``logcat_text``, a live read under ``-T N`` only its
    last N lines (fake_toolchain's ``-T`` simulation).

    ``system_server`` answers ``pidof system_server``; its rule goes first, as
    the stub takes the first rule whose tokens all appear. Without it, that
    question gets the app's ``pidof`` answer like any other. With ``uids``,
    the package's uid and the ``pidof`` pid's uid are the recorded package uid
    (fake_toolchain.uid_rules); without, those questions get no answer.
    """
    rules = [Rule(("devices",), stdout=recorded.text("adb_devices_single"))]
    if system_server is not None:
        rules.append(Rule(("shell", "pidof", "system_server"), stdout=system_server))
    if uids:
        answered = pidof.stdout.strip() if pidof is not None else ""
        rules += uid_rules(recorded, PACKAGE, answered if answered.isdigit() else None)
    if pidof is not None:
        rules.append(pidof)
    rules.append(Rule(("logcat",), stdout=logcat_text, buffered=buffered, filter_pid=True))
    toolchain.install("adb", rules)


def _not_running(recorded) -> Rule:
    """``pidof`` exactly as recorded for an app that is not running."""
    return Rule(
        ("shell", "pidof"),
        stdout=recorded.text("pidof_not_running"),
        exit=_recorded_exit(recorded, "pidof_not_running"),
    )


# How each process in logcat_app_restart_usec ended, as its MANIFEST entry words it.
CRASHED, STOPPED, ALIVE = "am crash", "am force-stop", "running when the window was read"


def _restart_pids(recorded) -> dict[str, str]:
    """``pidof`` for each process in logcat_app_restart_usec, keyed by how it ended.

    The recording (made for app_state_capture's half of D4) stores the pids in
    launch order under ``pids`` and their endings under ``pid_endings``.
    """
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "logcat_app_restart_usec")
    pids = dict(zip(entry["pid_endings"], entry["pids"], strict=True))
    if set(pids) != {CRASHED, STOPPED, ALIVE} or len(set(pids.values())) != 3:
        pytest.fail(f"logcat_app_restart_usec no longer records a crash and a restart: {pids}")
    return pids


def _system_server(recorded) -> str:
    """``pidof system_server``, read in the run that recorded logcat_app_restart_usec."""
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "logcat_app_restart_usec")
    if not str(entry.get("system_server_pid", "")).isdigit():
        pytest.fail("logcat_app_restart_usec no longer records system_server's pid; re-record it")
    return entry["system_server_pid"]


def _oracles(recorded) -> dict:
    """system_server's pid and the package's uid, both recorded with the window."""
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "logcat_app_restart_usec")
    uid = parse_package_uids(entry["package_uid_listing"]["output"], PACKAGE)
    if uid is None:
        pytest.fail("logcat_app_restart_usec no longer records the package's uid; re-record it")
    return {"system_server_pid": _system_server(recorded), "package_uids": uid}


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
    the deduplicated list.
    """
    lines = recorded.lines("logcat_threadtime")
    priorities = _column(lines, "priority")
    warned = priorities.count("W")
    distinct = {line[m.end() :] for line in lines if (m := _THREADTIME.match(line)) and m[2] == "W"}
    if warned <= len(distinct):
        pytest.fail("the recording has no repeated warning; N15 would be vacuous")

    _device(toolchain, recorded, recorded.text("logcat_threadtime"))
    argv = ["--severity", "error,warning", "--last", "1m"] + (["--json"] if mode == "json" else [])
    result = toolchain.run("log_monitor.py", *argv)
    assert result.returncode == 0, result.stderr

    if mode == "json":
        payload = json.loads(result.stdout)
        stats = payload["statistics"]
        assert stats["warnings"] == warned
        assert stats["unique_warnings"] == len(payload["warnings"]) == len(distinct)
        assert stats["errors"] == priorities.count("E") + priorities.count("F")
        assert stats["unique_errors"] == len(payload["errors"])
    else:
        counts = _counts_in_text(result.stdout)
        assert counts["warnings"] == (warned, len(distinct)), result.stdout
        assert counts["errors"][0] == priorities.count("E") + priorities.count("F")


def test_n15_errors_count_every_line_of_a_crash_loop(toolchain, recorded):
    """The same shape for errors: three crashes of 13 E lines are 39 errors.

    Only the line count can be compared with what ``app_state_capture`` (D3) and
    a grep of the priority column report; the unique count says how many
    distinct messages that was.
    """
    lines = recorded.lines("logcat_crash_loop")
    errors = sum(1 for p in _column(lines, "priority") if p in "EF")
    _device(toolchain, recorded, recorded.text("logcat_crash_loop"))

    result = toolchain.run("log_monitor.py", "--severity", "error", "--last", "5m", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["statistics"]["errors"] == errors
    assert payload["statistics"]["unique_errors"] == len(payload["errors"]) < errors


# --- D4: the app is every process it had in the window --------------------------


def test_parse_package_pids_reads_the_apps_processes_from_the_log(recorded):
    """Each pid `pidof` read while that process was alive, and no other.

    The force-stopped process is the case a crash line cannot cover: only
    ActivityManager's `Start proc` / `Killing` lines say it was the app's.
    """
    pids = set(_restart_pids(recorded).values())
    text = recorded.text("logcat_app_restart_usec")
    checked = logcat.parse_package_pids(text, PACKAGE, **_oracles(recorded))
    assert checked == pids, "system_server's own lines must pass the producer check"


@pytest.mark.parametrize(
    ("statement", "ending"),
    [
        (" ActivityManager: Start proc ", ALIVE),
        (" ActivityManager: Killing ", STOPPED),
    ],
    ids=["start", "force-stop"],
)
def test_each_statement_names_the_pid_on_its_own(statement, ending, recorded):
    """Each of the two uid-carrying ways system_server names an app's process, alone.

    Every launch logs `Start proc`; the force-stop logs `Killing`. (`has died`
    carries no uid and only ends a life already confirmed; the crash's own
    AndroidRuntime `Process:` line names nothing alone -- both in
    tests/test_app_window_producer.py.)
    """
    lines = [line for line in recorded.lines("logcat_app_restart_usec") if statement in line]
    if not lines:
        pytest.fail(f"the recording has no {statement.strip()!r} line")
    pids = _restart_pids(recorded)
    found = logcat.parse_package_pids("\n".join(lines), PACKAGE, **_oracles(recorded))
    assert pids[ending] in found, lines
    assert found <= set(pids.values()), found


def test_parse_package_pids_finds_nothing_in_crash_lines_alone(recorded):
    """The crash buffer has no system_server line, and a crash line is no source.

    Any app can write AndroidRuntime's `Process: <pkg>, PID: <its pid>` for
    another package (Codex review of #27, ruling A); a real crash is named by
    system_server's `Start proc` / `has died` instead.
    """
    crash = recorded.text("logcat_crash_loop")
    if not _column(crash.splitlines(), "pid"):
        pytest.fail("the crash loop recording no longer holds a crash")
    found = logcat.parse_package_pids(crash, PACKAGE, **_oracles(recorded))
    assert found == set()


def test_parse_package_pids_does_not_claim_a_longer_package_name(recorded):
    """``com.example.compose`` is not ``com.example.composefixture``."""
    found = logcat.parse_package_pids(
        recorded.text("logcat_app_restart_usec"),
        PACKAGE[:-7],
        **_oracles(recorded),
    )
    assert found == set()


def test_parse_package_pids_reads_only_the_tag_that_owns_the_statement(recorded):
    """The same words under another tag are an app's prose, not system_server's record.

    Derived from the recording by substituting the tag for another one the
    same process logs under in it.
    """
    lines = recorded.lines("logcat_app_restart_usec")
    start = next((line for line in lines if " I ActivityManager: Start proc " in line), None)
    if start is None or not any(" ActivityTaskManager: " in line for line in lines):
        pytest.fail("the recording no longer has the two tags this substitution needs")
    forged = start.replace(" ActivityManager: ", " ActivityTaskManager: ")
    assert logcat.parse_package_pids(start, PACKAGE, **_oracles(recorded))
    assert logcat.parse_package_pids(forged, PACKAGE, **_oracles(recorded)) == set()


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
        recorded.text("logcat_app_restart_usec"),
        pidof=Rule(("shell", "pidof"), stdout=alive),
        system_server=_system_server(recorded),
    )
    result = toolchain.run("log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json")
    assert result.returncode == 0, result.stderr
    sent = [call for call in toolchain.calls("adb") if "logcat" in call]
    assert sent, "the device was never asked for logs"
    assert not [arg for call in sent for arg in call if arg.startswith("--pid")], sent


def _apps_lines(text: str, pids: set[str]) -> list[str]:
    """The lines of ``text`` from ``pids``, as log_monitor keeps them.

    rstrip: some device lines end in spaces, and log_monitor strips each line.
    """
    return [
        line.rstrip()
        for line in text.splitlines()
        if (m := _THREADTIME.match(line)) and m[1] in pids
    ]


def test_d4_a_force_stopped_process_stays_in_the_apps_window(toolchain, tmp_path, recorded):
    """D4 beyond a crash: a force-stopped process leaves no crash line.

    The window -- the JSON's ``sample_logs`` and the saved log alike -- is
    every line of every process `pidof` saw, crashed, stopped and alive, in
    order, and nothing else: not system_server or the other processes whose
    lines name the package, not a buffer separator. The same window
    app_state_capture saves for this recording.
    """
    pids = set(_restart_pids(recorded).values())
    text = recorded.text("logcat_threadtime") + recorded.text("logcat_app_restart_usec")
    unrelated = set(_column(text.splitlines(), "pid")) - pids
    if not unrelated:
        pytest.fail("the window has no line from another process; exclusion is untested")
    alive = _restart_pids(recorded)[ALIVE]
    _device(
        toolchain,
        recorded,
        text,
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
    assert payload["sample_logs"] == _apps_lines(text, pids)
    assert payload["app_pids"] == sorted(pids, key=int)
    logs = sorted(saved.glob("*.log"))
    if len(logs) != 1:
        pytest.fail(f"expected one saved log under {saved}, found {logs}")
    assert logs[0].read_text(encoding="utf-8").splitlines() == _apps_lines(text, pids)


def test_d4_a_process_already_running_when_the_window_opened_is_kept_whole(toolchain, recorded):
    """Its only naming line, `Killing`, comes AFTER all of its own lines.

    A window that opens mid-life has no `Start proc` for that process. Derived
    from the recording by deleting the force-stopped process's `Start proc`
    line, so system_server's `Killing <pid>` is the one line that names it,
    and every line of the process precedes it. A window filtered as it is read
    -- or a held window parsed without system_server's pid -- loses them all.

    Run with the live hold capped at 5 lines: a historical read is not a live
    one, holds the whole window, and must keep all of them whatever that cap.
    """
    pids = _restart_pids(recorded)
    stopped = pids[STOPPED]
    lines = recorded.text("logcat_app_restart_usec").splitlines(keepends=True)
    start = [line for line in lines if f"ActivityManager: Start proc {stopped}:" in line]
    killing = [line for line in lines if f"ActivityManager: Killing {stopped}:" in line]
    if len(start) != 1 or len(killing) != 1:
        pytest.fail("the recording no longer starts and kills the stopped process once each")
    window = "".join(line for line in lines if line not in start)
    first_own = next(
        i for i, line in enumerate(window.splitlines()) if line.split()[2:3] == [stopped]
    )
    if first_own > window.splitlines().index(killing[0].rstrip("\n")):
        pytest.fail("the stopped process logged nothing before its Killing line")
    _device(
        toolchain,
        recorded,
        window,
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
    assert json.loads(result.stdout)["sample_logs"] == _apps_lines(window, set(pids.values()))


def _historical_and_live(toolchain, *app_args: str) -> tuple[dict, dict]:
    """The same device read with ``--last`` and with a live ``--duration``.

    The stub's logcat prints its recording and exits, so the live read ends at
    the end of the recording -- the same lines a historical read sees.
    """
    runs = []
    for window in (("--last", "5m"), ("--duration", "30s")):
        result = toolchain.run(
            "log_monitor.py", *app_args, *window, "--json", ANDROID_EMU_LOG_TAIL="1000"
        )
        assert result.returncode == 0, f"{window}: {result.stderr}"
        runs.append(json.loads(result.stdout))
    return runs[0], runs[1]


def _without_start_of(recorded, pid: str) -> str:
    """logcat_app_restart_usec as if ``pid`` had started before the window opened."""
    lines = recorded.text("logcat_app_restart_usec").splitlines(keepends=True)
    starts = [line for line in lines if f"ActivityManager: Start proc {pid}:" in line]
    if len(starts) != 1:
        pytest.fail(f"the recording no longer starts {pid} exactly once")
    return "".join(line for line in lines if line not in starts)


def _until_the_crash_is_over(recorded) -> str:
    """logcat_app_restart_usec up to the crashed process's `has died`: no relaunch yet.

    A prefix of the recording, so every line is as recorded; it ends where an
    agent looking at a crash that was not restarted would read.
    """
    crashed = _restart_pids(recorded)[CRASHED]
    lines = recorded.text("logcat_app_restart_usec").splitlines(keepends=True)
    died = next(
        (
            i
            for i, line in enumerate(lines)
            if f"ActivityManager: Process {PACKAGE} (pid {crashed}) has died" in line
        ),
        None,
    )
    if died is None:
        pytest.fail("the recording no longer holds the crashed process's `has died`")
    return "".join(lines[: died + 1])


def test_d4_parity_a_crash_read_live_counts_what_last_counts(toolchain, recorded):
    """The recorded crash, read live and with ``--last``: the same errors.

    Codex measured 36 errors live where ``--last`` counted 39 (review of #27,
    on the crash loop, when a crash line could still name its pid). Under
    ruling A a crash is named by system_server's uid-carrying `Start proc` for
    its pid, recorded here before the crash, so nothing waits: both reads keep
    the whole trace. (A crash whose start predates the read is named by no
    uid-carrying line -- `has died` has none -- and is in neither read.)
    """
    pids = _restart_pids(recorded)
    window = recorded.text("logcat_app_restart_usec")
    _device(
        toolchain,
        recorded,
        window,
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    historical, live = _historical_and_live(toolchain, "--app", PACKAGE, "--severity", "error")

    app = set(pids.values())
    expected = sum(
        1 for line in _apps_lines(window, app) if _THREADTIME.match(line)["priority"] in "EF"
    )
    if not expected:
        pytest.fail("the derived window holds no E line of the app; parity would be vacuous")
    assert historical["statistics"]["errors"] == expected
    assert live["statistics"]["errors"] == expected
    assert sorted(live["sample_logs"]) == sorted(historical["sample_logs"])


def test_d4_parity_a_process_named_only_when_it_dies(toolchain, recorded):
    """A process already running when the read began is named only by `Killing`.

    Derived from the recording by deleting its `Start proc` line. Every one of
    its lines arrives before the line that names it, so a live read holds them
    all back and replays them.
    """
    pids = _restart_pids(recorded)
    stopped = pids[STOPPED]
    lines = recorded.text("logcat_app_restart_usec").splitlines(keepends=True)
    window = "".join(
        line for line in lines if f"ActivityManager: Start proc {stopped}:" not in line
    )
    _device(
        toolchain,
        recorded,
        window,
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
        system_server=_system_server(recorded),
    )
    historical, live = _historical_and_live(toolchain, "--app", PACKAGE)

    assert historical["sample_logs"] == _apps_lines(window, set(pids.values()))
    assert sorted(live["sample_logs"]) == sorted(historical["sample_logs"])
    assert live["statistics"] == historical["statistics"]


def test_d4_the_live_hold_is_bounded(toolchain, recorded):
    """Lines older than ``ANDROID_EMU_LOG_PENDING_CAP`` are dropped, not held without limit.

    The window of the test above: every line of the stopped process waits for
    its `Killing` line, among every other process's unattributed lines. With
    a cap of 5, at most five of its lines can still be waiting there.
    """
    pids = _restart_pids(recorded)
    stopped = pids[STOPPED]
    lines = recorded.text("logcat_app_restart_usec").splitlines(keepends=True)
    window = "".join(
        line for line in lines if f"ActivityManager: Start proc {stopped}:" not in line
    )
    own = len(_apps_lines(window, {stopped}))
    if own <= 5:
        pytest.fail(f"the stopped process logged only {own} lines; the cap is untested")
    _device(
        toolchain,
        recorded,
        window,
        pidof=Rule(("shell", "pidof"), stdout=pids[ALIVE]),
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
        ANDROID_EMU_LOG_PENDING_CAP="5",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    kept = _column(payload["sample_logs"], "pid").count(stopped)
    # Other processes' lines share the hold, so on this recording none of the
    # stopped process's survive; the guard is the upper bound.
    assert kept <= 5 < own, f"{kept} of the stopped process's {own} lines survived a cap of 5"

    # ...and the loss is counted, in the JSON and on stderr (Codex review v2):
    # every one of the stopped process's lines that is missing was evicted.
    assert payload["attribution"]["pending_evicted"] == own - kept
    assert f"{own - kept} line(s) of {PACKAGE}'s processes were dropped" in result.stderr
    assert payload["attribution"]["unattributed_at_end"] > 0
    assert "were still held when the read ended" in result.stderr


def test_d4_a_historical_read_reports_what_decided_it(toolchain, recorded):
    """The JSON's `attribution` block: what the window rests on (Codex review v2).

    Everything known, on the recorded window: system_server's pid and the
    package's uid as recorded, the live pid from `pidof` (uid-checked), all
    three processes named by the log, nothing held back.
    """
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        recorded.text("logcat_app_restart_usec"),
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
    }


def _another_process_first(recorded, ending: str = STOPPED) -> tuple[str, list[str], str]:
    """logcat_app_restart_usec where one app process's pid was another process's first.

    Derived: the lines of one unrelated process logged before the `Start proc`
    of the app process that ended as ``ending`` get that process's pid in their
    PID column (pid reuse: the pid was that process's, then the app's). Returns
    the window, the substituted lines, and the pid.
    """
    stopped = _restart_pids(recorded)[ending]
    lines = recorded.text("logcat_app_restart_usec").splitlines()
    start = next(i for i, line in enumerate(lines) if f"Start proc {stopped}:" in line)
    app = set(_restart_pids(recorded).values())
    others = [m["pid"] for m in map(_THREADTIME.match, lines[:start]) if m]
    other = next(
        (pid for pid in others if pid not in app and pid != _system_server(recorded)), None
    )
    if other is None:
        pytest.fail("no other process logs before the stopped process starts")
    reused: list[str] = []
    for index, line in enumerate(lines[:start]):
        match = _THREADTIME.match(line)
        if match and match["pid"] == other:
            lines[index] = line[: match.start("pid")] + stopped + line[match.end("pid") :]
            reused.append(lines[index].rstrip())
    return "\n".join(lines) + "\n", reused, stopped


def _split_after_start(window: str, pid: str) -> tuple[str, str]:
    """``window`` cut after ``pid``'s `Start proc`: what was buffered, what came after.

    A process ``pidof`` names when a live read begins was started before it,
    so its `Start proc` is already in the device's buffer.
    """
    lines = window.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if f"Start proc {pid}:" in line)
    return "".join(lines[: start + 1]), "".join(lines[start + 1 :])


@pytest.mark.parametrize("ending", [STOPPED, ALIVE], ids=["stopped", "running"])
@pytest.mark.parametrize("read", [("--last", "5m"), ("--duration", "30s")], ids=["last", "live"])
def test_d4_a_reused_pid_is_the_apps_only_inside_its_life(read, ending, toolchain, recorded):
    """One pid, first another process's, then the app's: only the app's life counts.

    The pid of an app process logs as another process before its `Start
    proc`; those lines are not the app's, in a historical read and live. The
    process running when the read began is the hard case live (Codex review
    v3): a live read keeps its lines from the first one it sees, so it must
    not see the buffer's older lines at all -- it starts at the end (`-T 1`).
    For it, the device holds everything up to its `Start proc` in its buffer;
    a dump reads that whole, a live read only from the end.
    """
    window, reused, pid = _another_process_first(recorded, ending)
    pids = _restart_pids(recorded)
    if ending == ALIVE:
        buffered, after = _split_after_start(window, pid)
    else:
        # The read began before this process started: all of it arrives live.
        buffered, after = "", window
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
    assert not set(reused) & set(kept), "lines of the pid's earlier owner are in the window"
    assert pid in _column(kept, "pid"), "the app's own life under that pid is missing"
    reads = [call for call in toolchain.calls("adb") if "logcat" in call]
    tails = [call[call.index("-T") + 1] for call in reads if "-T" in call]
    assert tails == (["1"] if read[0] == "--duration" else [])


def _killed_last(recorded) -> str:
    """A window where the stopped process is named only at its very end.

    Derived: its `Start proc` deleted (it was running when the read began) and
    its `Killing` line moved to the end, after the live process's lines -- so a
    live read keeps the live process's lines first and the stopped process's,
    which are OLDER, only when the window ends.
    """
    stopped = _restart_pids(recorded)[STOPPED]
    lines = _without_start_of(recorded, stopped).splitlines(keepends=True)
    killing = [line for line in lines if f"ActivityManager: Killing {stopped}:" in line]
    if len(killing) != 1:
        pytest.fail("the recording no longer kills the stopped process once")
    return "".join(line for line in lines if line not in killing) + killing[0]


def test_d4_a_live_read_stores_lines_in_input_order(toolchain, tmp_path, recorded):
    """Held lines are decided late; the saved log keeps the device's order.

    Codex (review v2) reproduced replayed lines stored after newer ones, which
    also changed what survived the `sample_logs` tail. The saved log of a live
    read must equal the saved log of ``--last`` over the same window.
    """
    window = _killed_last(recorded)
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        window,
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
    """`--follow` prints a held line when it is decided -- after newer ones -- marked."""
    window = _killed_last(recorded)
    pids = _restart_pids(recorded)
    _device(
        toolchain,
        recorded,
        window,
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

    Each start is announced before the new process's first line (recorded),
    so a live read learns every pid in time. ``pidof`` answers as recorded for
    an app that is not running; the stub's logcat then exits, ending the read.
    """
    pids = _restart_pids(recorded)
    text = recorded.text("logcat_app_restart_usec")
    _device(
        toolchain,
        recorded,
        text,
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
    assert json.loads(result.stdout)["sample_logs"] == _apps_lines(text, set(pids.values()))


def test_d4_follow_prints_only_the_apps_lines(toolchain, recorded):
    pids = set(_restart_pids(recorded).values())
    _device(
        toolchain,
        recorded,
        recorded.text("logcat_app_restart_usec"),
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
    process. The window is logcat_app_restart_usec up to the crashed process's
    `has died` -- the moment after the crash, before any relaunch -- and
    system_server's `Start proc` / `has died` name it. The whole crash, every
    line in order, is the assertion.
    """
    window = _until_the_crash_is_over(recorded)
    crashed = _restart_pids(recorded)[CRASHED]
    _device(
        toolchain,
        recorded,
        window,
        pidof=_not_running(recorded),
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _apps_lines(window, {crashed})


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_d4_an_app_with_no_process_in_the_window_is_refused(json_mode, toolchain, recorded):
    """Not running, and nothing in the window names it: refused, not widened.

    Before D4 this read every process on the device and called it the app's.
    """
    _device(toolchain, recorded, recorded.text("logcat_threadtime"), pidof=_not_running(recorded))
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
    system_server's lines are another; only when neither names a process is the
    capture refused.
    """
    window = _until_the_crash_is_over(recorded)
    crashed = _restart_pids(recorded)[CRASHED]
    _device(
        toolchain,
        recorded,
        window,
        pidof=_PIDOF_CANNOT_RUN,
        system_server=_system_server(recorded),
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _apps_lines(window, {crashed})


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_d4_a_failed_pidof_and_a_window_without_the_app_is_refused(json_mode, toolchain, recorded):
    """...and the refusal says `pidof` failed, not that the app was not running."""
    _device(toolchain, recorded, recorded.text("logcat_threadtime"), pidof=_PIDOF_CANNOT_RUN)
    argv = ["--app", PACKAGE, "--last", "1m"] + (["--json"] if json_mode else [])
    result = toolchain.run("log_monitor.py", *argv)
    assert_refusal(result, json_mode=json_mode, naming=("`pidof` failed",), status=1)
