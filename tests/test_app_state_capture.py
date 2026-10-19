"""Device-free tests for app_state_capture feature deltas.

Covers the pure helpers (UI element counting, logcat error/warning parsing)
and the arg->adb-command mapping for device-info probes and the log-lines cap,
with the ``subprocess.run`` that ``common.adb_exec`` calls monkeypatched so no
device is needed. Every adb call now goes through ``adb_exec.run_adb`` -- which
is where the time bound and the typed, remedy-naming errors live -- so the
fakes below stand in for adb itself rather than for a per-module
``subprocess``.

A snapshot is several dumps back to back, so this file also pins the bound:
nothing goes out unbounded, and the two dumps that legitimately outlast the
default carry their own budget instead of raising it for the whole skill.

And the log window's two lies: errors counted from prose instead of the
priority column (D3), and a window that was either the restarted app's newest
process alone or, with the app not running, every process on the device (D4).
Both are read against recordings -- logcat_app_restart_records holds three
processes of one app in one window, as logd's records (`logcat -B`, what the
capture reads), with the pids `pidof` gave for each.

And the review of v0.8.0: a carriage return inside a line counted as a second
line and an error (A2); a window that left the app's lines out reported a
complete, error-free capture (A3); and a device that went away during the
capture exited 1, where the same fact exits 3 everywhere else (A7).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import app_state_capture
import pytest
from app_state_capture import (
    AppStateCapture,
    analyze_logcat,
    count_ui_elements,
    parse_package_pids,
)
from fake_toolchain import framed, frames, recorded_package_listing, recorded_ps_answer

from common import adb_exec, cli, logcat
from common.device_utils import parse_package_uids

RECORDS = "logcat_app_restart_records"


def _node(tag: str, *children: dict) -> dict:
    return {"tag": tag, "attributes": {}, "children": list(children)}


def _device(
    *, pidof: tuple[str, int], window: bytes, system_server: str | None = None, recorded=None
):
    """A stand-in for adb: ``pidof`` answers ``(stdout, status)``, ``logcat`` the window.

    ``window`` is recorded ``logcat -B`` records: what the capture reads.

    Both handed in by the test from the recorded corpus. Everything else
    answers with silence, which the snapshot records as unknown.

    ``pidof system_server`` answers ``system_server`` -- the pid recorded with
    the window it belongs to -- or, without one, as ``pidof`` answers for a
    process that is not there (no output, exit 1). Only system_server's own
    ActivityManager lines may say which pids were the app's.

    With ``recorded`` (#27), the uid questions are answered too, from the
    recorded answers: the package's uid (package_list_uid, the package name
    substituted) and the ``pidof`` pid's (ps_pid_uid, the pid substituted). A
    pid is the app's only when its uid is the package's, so without them the
    fake would say "this is the app's process" and then fail to back it up.
    """

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd and "system_server" in cmd:
            if system_server is None:
                return SimpleNamespace(stdout="", stderr="", returncode=1)
            return SimpleNamespace(stdout=system_server, stderr="", returncode=0)
        if "pidof" in cmd:
            return SimpleNamespace(stdout=pidof[0], stderr="", returncode=pidof[1])
        if recorded is not None and "package" in cmd and "-U" in cmd:
            listing = recorded_package_listing(recorded, cmd[-1])
            return SimpleNamespace(stdout=listing, stderr="", returncode=0)
        if recorded is not None and "ps" in cmd and pidof[0].strip().isdigit():
            answer = recorded_ps_answer(recorded, pidof[0].strip())
            return SimpleNamespace(stdout=answer, stderr="", returncode=0)
        if "logcat" in cmd:
            return SimpleNamespace(stdout=window, stderr=b"", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    return fake_run


def _saved(path: Path) -> list[str]:
    """The lines a capture saved, split where logcat splits them: at "\\n" only."""
    return path.read_bytes().decode("utf-8").split("\n")[:-1]


def _rendered(window: bytes, pids: set[str]) -> list[str]:
    """The lines of ``pids``' records, as the capture prints them (common.logcat)."""
    return [
        line
        for record in logcat.parse_records(window)
        if str(record.pid) in pids
        for line in record.lines()
    ]


def test_count_ui_elements_counts_all_nodes():
    tree = _node("root", _node("a", _node("a1")), _node("b"))
    # root + a + a1 + b == 4
    assert count_ui_elements(tree) == 4


def test_count_ui_elements_single_node():
    assert count_ui_elements(_node("only")) == 1


def test_count_ui_elements_handles_none_and_empty():
    assert count_ui_elements(None) == 0
    assert count_ui_elements({}) == 0


def _columns(text: str) -> list[list[str]]:
    """Whitespace-split fields of each dated line: [date, time, pid, tid, priority, ...].

    Deliberately not the module's regex: the oracle a parser is checked
    against must not be the parser.
    """
    return [
        fields
        for fields in (line.split() for line in text.splitlines())
        if len(fields) > 4 and re.fullmatch(r"\d{2}-\d{2}", fields[0])
    ]


def test_d3_errors_are_counted_from_the_priority_column_not_the_prose(recorded):
    """D3: a line was an error when its TEXT said "error" or "exception".

    logcat_threadtime holds one E line, and V/D/I lines whose prose says
    "error" (`errorCallbackToken=null`); the old count read three.
    """
    text = recorded.text("logcat_threadtime")
    priorities = [fields[4] for fields in _columns(text)]
    prose = [
        fields
        for fields in _columns(text)
        if fields[4] not in ("E", "F") and re.search("error|exception", " ".join(fields), re.I)
    ]
    assert prose, "premise: the recording must hold error prose on a line that is not an error"

    stats = analyze_logcat(text)

    assert stats["errors"] == sum(1 for p in priorities if p in ("E", "F")) >= 1
    assert stats["warnings"] == priorities.count("W")
    assert stats["lines"] == len(text.strip().splitlines())


def test_a_fatal_crash_block_counts_every_line_and_not_the_separator(recorded):
    """The crash buffer: a `beginning of crash` separator, then E lines only."""
    text = recorded.text("logcat_crash_loop")
    assert not _columns(text.splitlines()[0]), "premise: the dump opens with a separator"

    stats = analyze_logcat(text)

    assert stats["errors"] == len(_columns(text))
    assert stats["warnings"] == 0


def test_analyze_logcat_empty():
    stats = analyze_logcat("")
    assert stats == {"lines": 0, "errors": 0, "warnings": 0}


def test_a2_a_carriage_return_inside_a_line_is_not_a_second_line(recorded):
    """A2: ``splitlines`` broke at "\\r", so one I line counted as two and an error.

    logcat ends a line only at "\\n" and leaves an app's "\\r" raw, so an app
    can end its own I line with "\\r" and an E header of its choosing (review
    of v0.8.0). Derived: a recorded I line of the app, "\\r", and a recorded E
    line appended -- on the device, ONE line, an I.
    """
    lines = recorded.lines("logcat_app_restart_usec")
    pids = set(_manifest_entry(recorded, RECORDS)["pids"])
    info = next(
        (f for f in map(str.split, lines) if len(f) > 4 and f[2] in pids and f[4] == "I"), None
    )
    error = next((line for line in lines if len(line.split()) > 4 and line.split()[4] == "E"), None)
    if info is None or error is None:
        pytest.fail("the recording no longer holds an I line of the app and an E line")
    line = next(line for line in lines if line.split() == info)
    stats = analyze_logcat(f"{line}\r{error}\n")
    assert stats == {"lines": 1, "errors": 0, "warnings": 0}


def test_device_info_builds_expected_adb_commands(monkeypatch):
    """model/sdk/density probes map to the right adb shell commands."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        assert kwargs.get("check") is False  # explicit check= required
        if "ro.product.model" in cmd:
            out = "Pixel 7\n"
        elif "ro.build.version.sdk" in cmd:
            out = "34\n"
        elif "density" in cmd:
            out = "Physical density: 420\n"
        else:
            out = ""
        return SimpleNamespace(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    info = capturer._get_device_info()

    assert info == {"model": "Pixel 7", "sdk": 34, "density": 420}

    # Verify the adb command shapes (serial targeting + getprop/wm density).
    joined = [" ".join(c) for c in calls]
    assert any(c == "adb -s emulator-5554 shell getprop ro.product.model" for c in joined), joined
    assert any(
        c == "adb -s emulator-5554 shell getprop ro.build.version.sdk" for c in joined
    ), joined
    assert any(c == "adb -s emulator-5554 shell wm density" for c in joined), joined


def test_capture_logs_respects_log_lines_cap_and_counts(monkeypatch, tmp_path, recorded):
    """--log-lines caps retained lines (keeping the newest) and stats reflect the cap.

    Driven by a real logcat window rather than five invented numbered lines:
    the recording's crash sits outside its tail, so capping demonstrably drops
    its errors instead of a hand-placed `E Tag: line0 error` doing so by
    construction. The tail's counts are read off its priority column.
    """
    window = recorded.data(f"{RECORDS}.bin")
    entry = _manifest_entry(recorded, RECORDS)
    pids = set(entry["pids"])
    tail = _rendered(window, pids)[-3:]
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        _device(
            pidof=(entry["pids"][-1], 0),
            window=window,
            system_server=entry["system_server_pid"],
            recorded=recorded,
        ),
    )

    capturer = AppStateCapture(package=APP, serial="emulator-5554")
    uncapped = capturer._capture_logs(tmp_path / "all.txt", "30s", log_lines=0)
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=3)

    # The newest lines are the ones kept.
    assert _saved(tmp_path / "app-logs.txt") == tail
    assert stats["captured"] is True
    assert stats["lines"] == 3 < uncapped["lines"]

    # And the counts follow the cap rather than the whole window.
    priorities = [fields[4] for fields in _columns("\n".join(tail))]
    assert uncapped["errors"] > sum(1 for p in priorities if p in "EF")
    assert stats["errors"] == sum(1 for p in priorities if p in "EF")
    assert stats["warnings"] == priorities.count("W")


def test_capture_logs_invalid_duration_reports_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    capturer = AppStateCapture(package="com.example.app")
    out_path = tmp_path / "app-logs.txt"
    stats = capturer._capture_logs(out_path, "bogus")
    assert stats["captured"] is False
    assert "Invalid log duration" in stats["reason"]


def test_write_summary_md(tmp_path: Path):
    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    summary = {
        "timestamp": "20260617-100000",
        "package": "com.example.app",
        "device_serial": "emulator-5554",
        "artifacts": ["screenshot.png", "ui-hierarchy.json"],
        "app_info": {"version": "1.2.3", "pid": "4242"},
        "device_info": {"model": "Pixel 7", "sdk": 34, "density": 420},
        "ui_element_count": 42,
        "logs": {"captured": True, "lines": 10, "errors": 2, "warnings": 1},
    }
    capturer._write_summary_md(tmp_path, summary)
    md = (tmp_path / "summary.md").read_text()
    assert "# App State Capture" in md
    assert "Model: Pixel 7" in md
    assert "SDK: 34" in md
    assert "Density: 420" in md
    assert "Elements: 42" in md
    assert "Errors: 2" in md
    assert "Warnings: 1" in md


# === bounding: a snapshot is several dumps, and none may be unbounded ===


def test_no_snapshot_probe_goes_out_unbounded(monkeypatch, tmp_path):
    """An unbounded adb call wedges the connection for whatever runs next."""
    seen: list[dict] = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    capturer._get_app_info()
    capturer._get_device_info()
    # A silent device: no process of the app to read, so the logs refuse --
    # after both of their calls have gone out.
    with pytest.raises(app_state_capture.cli.Failure):
        capturer._capture_logs(tmp_path / "logs.txt", "30s", 200)

    assert len(seen) == 8, f"expected 8 adb calls, saw {len(seen)}"
    assert all(kwargs.get("timeout") for kwargs in seen), "an adb call went out unbounded"


def test_the_long_dumps_carry_their_own_budget(monkeypatch):
    """`dumpsys` outlasts the default on a loaded emulator; the default stays put."""
    seen: list[tuple[list[str], dict]] = []

    def fake_run(cmd, *args, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)

    AppStateCapture(package="com.example.app", serial="emulator-5554")._get_app_info()

    budgets = {" ".join(cmd): kwargs["timeout"] for cmd, kwargs in seen if "dumpsys" in cmd}
    assert budgets, "no dumpsys call issued"
    assert all(
        value == app_state_capture.DUMPSYS_TIMEOUT_SECONDS for value in budgets.values()
    ), budgets
    assert app_state_capture.DUMPSYS_TIMEOUT_SECONDS > app_state_capture.adb_exec.DEFAULT_TIMEOUT

    # The cheap property reads keep the module-wide default.
    getprop = [kwargs["timeout"] for cmd, kwargs in seen if "pidof" in cmd]
    assert getprop == [app_state_capture.adb_exec.DEFAULT_TIMEOUT]


def test_logcat_window_carries_its_own_budget(monkeypatch, tmp_path):
    """A wide `logcat -d` window drains the whole ring buffer."""
    seen: list[tuple[list[str], dict]] = []

    def fake_run(cmd, *args, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    with pytest.raises(app_state_capture.cli.Failure):  # a silent device; see above
        capturer._capture_logs(tmp_path / "logs.txt", "5m", 200)

    logcat = [kwargs for cmd, kwargs in seen if "logcat" in cmd]
    assert logcat, "no logcat command issued"
    assert logcat[0]["timeout"] == app_state_capture.LOGCAT_TIMEOUT_SECONDS


# === device-level failures reach the CLI boundary, not the user's terminal ===


def test_device_error_is_not_recorded_as_a_partial_snapshot(monkeypatch, tmp_path):
    """ "more than one device" means nothing was captured, not "logs missing"."""

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "adb: more than one device/emulator\n")

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app")
    with pytest.raises(app_state_capture.adb_exec.MultipleDevicesError):
        capturer._capture_logs(tmp_path / "logs.txt", "30s", 200)


def test_unknown_serial_exits_three_with_an_actionable_message(
    monkeypatch, capsys, tmp_path, recorded_anywhere
):
    """A wrong --serial must yield a remedy and exit 3, never a traceback.

    Exit 3 -- the device is unavailable -- as everywhere else (A7). It was
    exit 1: the partial-capture path turned the typed error into text.
    """
    fixture = recorded_anywhere("adb_device_not_found")

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", fixture)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app_state_capture.py",
            "--package",
            "com.example.app",
            "--serial",
            "no-such-serial-xyz",
            "--output",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)

    assert excinfo.value.code == cli.EXIT_DEVICE_UNAVAILABLE
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "no-such-serial-xyz" in captured.err
    assert "adb devices" in captured.err, "the error does not say how to see what is attached"
    assert "Traceback" not in captured.err


# === X8: a component that was asked for and did not arrive =================
#
# The runtime sweep cannot reach this: it breaks adb entirely, so the whole
# capture fails at the device level and main() maps that before the partial
# path runs. These inject the component failure directly, which is the only
# way to assert the exit status the sweep's app_state_capture mode is silent
# about.


def _snapshot_cli(monkeypatch, tmp_path, argv: list[str], run=None):
    """Run main() over a device that answers, with everything but logs stubbed.

    ``run`` stands in for adb when given; otherwise every question is answered
    with silence, exit 0.
    """
    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        run or (lambda *_a, **_k: SimpleNamespace(stdout="", stderr="", returncode=0)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["app_state_capture.py", "--package", "com.example.app", "--output", str(tmp_path), *argv],
    )
    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)
    return excinfo.value.code


def test_an_invalid_log_window_fails_the_snapshot(monkeypatch, tmp_path, capsys):
    """`--logs 1x` wrote "Invalid log duration" into the summary and exited 0.

    The logs were explicitly requested, so their absence is the answer to the
    question the agent asked -- not a footnote under "State captured".
    """
    code = _snapshot_cli(monkeypatch, tmp_path, ["--logs", "1x"])
    captured = capsys.readouterr()

    assert code == 1
    assert "State captured" not in captured.out + captured.err
    assert "Traceback" not in captured.err
    assert "--logs" in captured.err, "the error does not name its remedy"


def test_a_failed_log_capture_fails_the_snapshot(monkeypatch, tmp_path, capsys):
    """The other half of X8: the window parsed, the capture itself failed."""
    monkeypatch.setattr(
        app_state_capture.AppStateCapture,
        "_capture_logs",
        lambda self, path, duration, log_lines=200: {"captured": False, "error": "logcat exited 1"},
    )

    code = _snapshot_cli(monkeypatch, tmp_path, [])
    captured = capsys.readouterr()

    assert code == 1
    assert "State captured" not in captured.out + captured.err
    assert "logcat exited 1" in captured.err


def test_a_partial_snapshot_reports_json_as_an_error_with_what_it_kept(
    monkeypatch, tmp_path, capsys
):
    """`--json` on failure is common.cli's refusal carrying `partial` -- and the
    partial half is what makes keeping the artifacts worth anything. (It was
    `{"error", "partial"}`, with no `success` and no remedy, until N5.)"""
    code = _snapshot_cli(monkeypatch, tmp_path, ["--logs", "1x", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert set(payload) == {"success", "error", "remedy", "partial"}, payload
    assert payload["success"] is False and payload["remedy"], payload
    assert "screenshot.png" in payload["partial"]["artifacts"]
    assert payload["partial"]["logs"]["captured"] is False


def test_a_screenshot_that_raises_fails_the_snapshot(monkeypatch, tmp_path, capsys):
    """`capture_screenshot` raises on failure, and the whole capture used to go
    with it -- including the artifacts collected after it would have been.

    The failure is the device not being there, so the refusal is exit 3 (A7),
    and still carries what did arrive."""

    def boom(*_a, **_k):
        raise app_state_capture.adb_exec.DeviceNotFoundError(
            "device 'no-such-serial-xyz' not found; run `adb devices` to see what is attached"
        )

    monkeypatch.setattr(app_state_capture, "capture_screenshot", boom)
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app_state_capture.py",
            "--package",
            "com.example.app",
            "--output",
            str(tmp_path),
            "--no-logs",
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)
    payload = json.loads(capsys.readouterr().out)

    assert excinfo.value.code == cli.EXIT_DEVICE_UNAVAILABLE
    assert set(payload) == {"success", "error", "remedy", "partial"}, payload
    assert "screenshot" in payload["error"]
    assert "adb devices" in payload["error"], "the error does not name its remedy"
    # The hierarchy came after the screenshot and is still there.
    assert "screenshot.png" not in payload["partial"]["artifacts"]
    assert "ui-hierarchy.json" in payload["partial"]["artifacts"]


def test_a_late_device_error_keeps_the_artifacts_already_written(
    monkeypatch, tmp_path, capsys, recorded_anywhere
):
    """The logs fail at the device level *after* four artifacts are on disk.

    `_capture_logs` re-raises a DeviceError rather than writing "logs
    unavailable" into the summary, and `capture()` let it out -- so a snapshot
    that had already written the screenshot, the hierarchy and both info files
    was reported as if nothing had happened, and `--json` printed nothing at
    all. Then it was kept, at exit 1; the device not being there is exit 3
    (A7), with the artifacts still handed over.
    """
    not_found = recorded_anywhere("adb_shell_device_not_found")

    def fake_run(cmd, *_a, **_k):
        if "logcat" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", not_found)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app_state_capture.py",
            "--package",
            "com.example.app",
            "--serial",
            "no-such-serial-xyz",
            "--output",
            str(tmp_path),
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert excinfo.value.code == cli.EXIT_DEVICE_UNAVAILABLE
    assert "Traceback" not in captured.err
    assert set(payload) == {"success", "error", "remedy", "partial"}, payload
    assert "logs" in payload["error"]
    assert sorted(payload["partial"]["artifacts"]) == [
        "app-info.json",
        "device-info.json",
        "screenshot.png",
        "ui-hierarchy.json",
    ]
    assert payload["partial"]["logs"]["captured"] is False
    # The manifest on disk records the same partial result.
    summaries = list(tmp_path.rglob("snapshot-summary.json"))
    assert summaries, "no snapshot-summary.json written for a partial capture"
    assert json.loads(summaries[0].read_text(encoding="utf-8"))["failures"]


@pytest.mark.parametrize(
    "error",
    [adb_exec.DeviceOfflineError, adb_exec.DeviceNotFoundError, adb_exec.DeviceUnauthorizedError],
)
def test_a7_a_device_gone_during_the_logs_is_exit_three_with_what_arrived(
    error, monkeypatch, tmp_path, capsys
):
    """A7: the logs meet a device that is unavailable after four artifacts arrived.

    It was exit 1 through the partial-report path, where the same fact through
    log_monitor is exit 3. The refusal is now that device error -- exit 3, its
    remedy first -- and still hands over ``partial``. Injected as the typed
    error adb_exec raises; no tool output is involved.
    """
    remedy = "Reconnect it, or restart the adb server."

    def gone(self, *_a, **_k):
        raise error(f"Device emulator-5554 is gone ({error.__name__}).", remedy=remedy)

    monkeypatch.setattr(AppStateCapture, "_capture_logs", gone)
    code = _snapshot_cli(monkeypatch, tmp_path, ["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_DEVICE_UNAVAILABLE
    assert set(payload) == {"success", "error", "remedy", "partial"}, payload
    assert payload["remedy"].startswith(remedy)
    assert sorted(payload["partial"]["artifacts"]) == [
        "app-info.json",
        "device-info.json",
        "screenshot.png",
        "ui-hierarchy.json",
    ]


def test_a7_several_devices_is_still_exit_one(monkeypatch, tmp_path, capsys):
    """The control: "more than one device" makes the caller choose; nothing is unavailable."""

    def several(self, *_a, **_k):
        raise adb_exec.MultipleDevicesError("More than one device.", remedy="Pass --serial.")

    monkeypatch.setattr(AppStateCapture, "_capture_logs", several)
    code = _snapshot_cli(monkeypatch, tmp_path, ["--json"])
    assert code == cli.EXIT_FAILURE
    assert json.loads(capsys.readouterr().out)["partial"]["artifacts"]


# === a dump cut short or late asks whether the device went (review 2 of v0.8.0) ===
#
# log_monitor and anr_watcher --since answer a log dump that ends inside a record
# by asking `adb get-state` again: gone is exit 3. The snapshot folded the same
# dump into "the logs failed" at exit 1 without asking, and judged adb's status
# before the framing, so the cut a transport loss leaves (adb exiting 255) read
# as "logcat exited 255" (item 7; review 1, item 3).

CRASH_DUMP = "logcat_crash_buffer_records.bin"
FOUR_ARTIFACTS = ["app-info.json", "device-info.json", "screenshot.png", "ui-hierarchy.json"]


def _dump_device(recorded, *, logcat, gone: bool | None):
    """adb for a snapshot whose log dump goes wrong as ``logcat`` says, then asked again.

    ``logcat(cmd, timeout)`` answers the dump. ``get-state`` answers as
    recorded: adb_device_not_found (with its recorded exit status) when the
    device is ``gone``, adb_get_state when it is not, and -- ``gone=None`` --
    not at all: the call times out. ``pidof`` and everything else get silence,
    exit 0 (no process running, which the dump's failure comes before).

    Returns:
        The stand-in for ``subprocess.run``, and the get-state calls it is asked.
    """
    asked: list[list[str]] = []
    not_found = next(
        e for e in recorded.manifest["fixtures"] if e["name"] == "adb_device_not_found"
    )

    def run(cmd, *_a, **kwargs):
        if "get-state" in cmd:
            asked.append(cmd)
            if gone is None:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            if gone:
                return subprocess.CompletedProcess(
                    cmd, not_found["exit_status"], "", recorded.text("adb_device_not_found")
                )
            return subprocess.CompletedProcess(cmd, 0, recorded.text("adb_get_state"), "")
        if "logcat" in cmd:
            return logcat(cmd, kwargs.get("timeout"))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    return run, asked


def _cut_dump(recorded, status: int):
    """The recorded two-crash dump less its final byte, adb exiting ``status``, stderr empty.

    Derived: logcat_crash_buffer_records without its last byte, a dump cut
    inside its second record. 0 is how older adb exits when the transport goes
    and 255 current adb (log_monitor's live-read specs pin both).
    """
    window = recorded.data(CRASH_DUMP)[:-1]
    return lambda cmd, _timeout: subprocess.CompletedProcess(cmd, status, window, b"")


def _late_dump(cmd, timeout):
    """A dump that does not finish in its budget: the bounded subprocess call times out."""
    raise subprocess.TimeoutExpired(cmd, timeout)


@pytest.mark.parametrize("status", [0, 255], ids=["older-adb-exit-0", "current-adb-exit-255"])
def test_a_cut_log_dump_whose_device_went_is_exit_three_with_what_arrived(
    status, monkeypatch, tmp_path, capsys, recorded
):
    """The dump ends inside a record and the device is gone: exit 3, the artifacts kept.

    It was exit 1 at either status, and get-state was never asked: at 0 the
    framing error became the logs' failure, at 255 the status was judged first.
    """
    run, asked = _dump_device(recorded, logcat=_cut_dump(recorded, status), gone=True)
    code = _snapshot_cli(monkeypatch, tmp_path, ["--json"], run=run)
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_DEVICE_UNAVAILABLE, payload
    assert len(asked) == 1, "the device was not asked again after the cut"
    assert set(payload) == {"success", "error", "remedy", "partial"}, payload
    assert "not attached" in payload["error"]
    assert sorted(payload["partial"]["artifacts"]) == FOUR_ARTIFACTS
    assert payload["partial"]["logs"]["captured"] is False


@pytest.mark.parametrize("status", [0, 255], ids=["older-adb-exit-0", "current-adb-exit-255"])
def test_a_cut_log_dump_of_a_device_still_there_fails_the_logs_for_the_cut(
    status, monkeypatch, tmp_path, capsys, recorded
):
    """The control: asked again, the device answers. The logs fail for the cut, exit 1.

    The cut is what is named -- at 255 it was "exited 255: no output" -- with
    how adb exited added when that was a failure.
    """
    run, asked = _dump_device(recorded, logcat=_cut_dump(recorded, status), gone=False)
    code = _snapshot_cli(monkeypatch, tmp_path, ["--json"], run=run)
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_FAILURE, payload
    assert "inside a record" in payload["error"]
    assert len(asked) == 1, "the device was not asked again after the cut"
    assert ("exited 255" in payload["error"]) is bool(status)
    assert payload["partial"]["logs"]["captured"] is False
    # app-logs.txt is there too, holding the failure: a failed capture writes
    # its reason into the file it would have filled.
    assert set(FOUR_ARTIFACTS) <= set(payload["partial"]["artifacts"])


def test_a_cut_log_dump_whose_device_cannot_be_asked_says_so(
    monkeypatch, tmp_path, capsys, recorded
):
    """get-state itself does not answer: the cut is still the refusal, the device unknown."""
    run, asked = _dump_device(recorded, logcat=_cut_dump(recorded, 0), gone=None)
    code = _snapshot_cli(monkeypatch, tmp_path, ["--json"], run=run)
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_FAILURE, payload
    assert "inside a record" in payload["error"]
    assert "still there is unknown" in payload["error"]
    assert len(asked) == 1


@pytest.mark.parametrize("gone", [True, False], ids=["device-gone", "device-there"])
def test_a_late_log_dump_asks_whether_the_device_went(
    gone, monkeypatch, tmp_path, capsys, recorded
):
    """A dump past its budget is the same question: gone is exit 3; there, a timeout (exit 1).

    A dump of a device that is not there does not fail, it waits (measured:
    `adb -s <unknown> logcat -d -B` prints adb_waiting_for_device's banner and
    does not exit), so the budget is how that snapshot ends. It was exit 1.
    """
    run, asked = _dump_device(recorded, logcat=_late_dump, gone=gone)
    code = _snapshot_cli(monkeypatch, tmp_path, ["--json"], run=run)
    payload = json.loads(capsys.readouterr().out)

    assert code == (cli.EXIT_DEVICE_UNAVAILABLE if gone else cli.EXIT_FAILURE), payload
    assert ("not attached" if gone else "did not finish") in payload["error"]
    assert len(asked) == 1, "the device was not asked again after the dump overran"
    assert set(FOUR_ARTIFACTS) <= set(payload["partial"]["artifacts"])
    assert payload["partial"]["logs"]["captured"] is False


def test_a_snapshot_that_was_not_asked_for_logs_still_succeeds(monkeypatch, tmp_path, capsys):
    """The negative control: --no-logs must not read as a missing component."""
    code = _snapshot_cli(monkeypatch, tmp_path, ["--no-logs"])

    assert code == 0
    assert "State captured" in capsys.readouterr().out


def test_device_info_records_the_effective_density_not_the_physical_one(monkeypatch, recorded):
    """An override makes the physical density the wrong answer.

    `wm density` prints `Physical density: 420` and, when an override is
    active, `Override density: 560`. This took the FIRST `density:` match and
    so recorded 420 into every snapshot's device-info.json -- silently wrong,
    with nothing to say it was stale, on any device whose density has been
    overridden (`wm density 560`, or an emulator started at another density).

    The repo already had both the override fixture and a shared parser that
    prefers the Override line; only this file did not use them. That makes it
    the third defect class fixed in one file and left standing in another,
    after R4 in screenshots and R11 in navigator.
    """
    from common import adb_exec

    override = recorded.text("wm_density_override")
    assert "Override density" in override, "fixture no longer exercises an override"

    def _run(operation, serial=None, *args, **kwargs):
        joined = " ".join(str(a) for a in args)
        stdout = override if "density" in joined else ""
        return adb_exec.AdbResult(returncode=0, stdout=stdout, stderr="", command=["adb"])

    monkeypatch.setattr(app_state_capture.adb_exec, "run_adb", _run)

    capture = app_state_capture.AppStateCapture("com.example.app", serial="emulator-5554")
    info = capture._get_device_info()

    # Read out of the fixture rather than written here. The literal used to be
    # 560, which silently tied this test to one recording of one device: 560 is
    # also the Pixel 4 XL's *physical* density, so the same assertion would
    # have passed against that profile for exactly the wrong reason.
    physical = int(re.search(r"Physical density: (\d+)", override).group(1))
    effective = int(re.search(r"Override density: (\d+)", override).group(1))
    assert physical != effective, "fixture needs distinct values to prove anything"

    assert (
        info["density"] == effective
    ), f"recorded the physical density instead of the effective one: {info}"


# === D4: the app's window is every process it had, and nothing else ========
#
# logcat_app_restart_usec is one window holding three processes of the fixture app
# -- crashed, force-stopped, running -- and its manifest entry holds the pids
# `pidof` reported while each was alive. That is the oracle: what the device
# said at the time, not this parser's own reading of the same text.

APP = "com.example.composefixture"


def _manifest_entry(recorded, name: str) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == name)


def _app_pids(recorded) -> list[str]:
    pids = _manifest_entry(recorded, RECORDS)["pids"]
    if len(set(pids)) < 2:
        pytest.fail(f"{RECORDS} no longer records a restart; re-record it")
    return pids


def _window(recorded) -> bytes:
    return recorded.data(f"{RECORDS}.bin")


def _records(recorded) -> list[logcat.LogRecord]:
    return logcat.parse_records(_window(recorded))


def _not_running(recorded) -> tuple[str, int]:
    """`pidof` for an app that is not running, as recorded: no output, exit 1."""
    status = _manifest_entry(recorded, "pidof_not_running")["exit_status"]
    return recorded.text("pidof_not_running"), status


def _oracles(recorded) -> dict:
    """system_server's pid and the package's uid, recorded beside the window (#27)."""
    entry = _manifest_entry(recorded, RECORDS)
    listing = entry["package_uid_listing"]["output"]
    return {
        "system_server_pid": entry["system_server_pid"],
        "package_uids": parse_package_uids(listing, APP),
    }


def test_every_process_the_window_ties_to_the_package_is_found(recorded):
    """Started, crashed, killed: each way the device names a process of the app.

    With system_server's pid as recorded beside the window: ActivityManager's
    lines count only from it (the killed process is named by nothing else).
    And with the package's uid (#27): a start or kill counts only for it.
    """
    assert parse_package_pids(_records(recorded), APP, **_oracles(recorded)) == set(
        _app_pids(recorded)
    )


def test_naming_the_package_does_not_make_a_line_the_apps(recorded):
    """system_server, the launcher and the IME all write the package name.

    Their pids sit in the same window, on lines that name the package -- and
    some of those lines carry another number beside it (a task's uid
    `A=10205:<pkg>`, the shell's `from pid N`). None of them is the app.
    """
    namers = {f.pid for f in frames(_window(recorded)) if APP in f.message}
    others = namers - set(_app_pids(recorded))
    assert others, "premise: processes other than the app name it in this window"

    assert not parse_package_pids(_records(recorded), APP, **_oracles(recorded)) & others


def test_a_package_is_matched_whole_not_as_a_prefix(recorded):
    """`com.example.compose` is not `com.example.composefixture`."""
    assert parse_package_pids(_records(recorded), APP[:-7], **_oracles(recorded)) == set()


def test_crash_lines_alone_attribute_nothing(recorded):
    """AndroidRuntime's `Process: <pkg>, PID: N` confirms; it never names a pid alone.

    Flipped in #27 (Codex review, ruling A). That line is written by the dying
    process itself, and any app can write the same line -- naming ANOTHER
    package and its own pid -- through the public `android.util.Log` API, which
    put the attacker's own lines into the victim's window. A real crash is
    attributed anyway, by system_server's `Start proc` / `has died` for that
    pid; the crash buffer on its own, which has no system_server line, names
    nothing.
    """
    crashes = logcat.parse_records(recorded.data("logcat_crash_buffer_records.bin"))
    if not crashes:
        pytest.fail("the crash buffer recording no longer holds a crash")
    assert parse_package_pids(crashes, APP, **_oracles(recorded)) == set()


@pytest.mark.parametrize("alive", ["none", "latest"])
def test_the_saved_log_holds_every_process_of_the_app_and_no_other(
    monkeypatch, tmp_path, recorded, alive
):
    """D4: the crashed process was cut away by `--pid=<the new one>`, and with
    the app not running at all the snapshot held every process on the device.

    The window is the restart recording, which holds other processes beside
    the app's three; `pidof` answers the live process, or nothing.
    """
    pids = _app_pids(recorded)
    window = _window(recorded)
    unrelated = {str(record.pid) for record in _records(recorded)} - set(pids)
    if not unrelated:
        pytest.fail("the recording no longer holds another process beside the app")
    pidof = _not_running(recorded) if alive == "none" else (pids[-1], 0)
    system_server = _manifest_entry(recorded, RECORDS)["system_server_pid"]
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        _device(pidof=pidof, window=window, system_server=system_server, recorded=recorded),
    )

    capturer = AppStateCapture(package=APP, serial="emulator-5554")
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)
    saved = _saved(tmp_path / "app-logs.txt")

    assert saved == _rendered(window, set(pids))
    assert stats["pids"] == sorted(pids, key=int)
    assert stats["lines"] == len(saved)
    assert stats["attribution"] == {"complete": True, "unresolved_lines": 0, "unresolved_pids": []}


def test_the_capture_asks_for_the_format_it_parses(monkeypatch, tmp_path, recorded):
    """D3's other half: `fmt=None` inherited the device's format, unparsed.

    Now it asks for logd's records (`-B`) -- one format, whatever the device's
    default, and the one no tag can forge a line in (A1) -- and prints each
    as threadtime for the counts to read.

    `pidof` answers the live process (#27): with no system_server answer and
    no running process, the capture refuses before this spec's assertion, as
    an app with no attributable process now does.
    """
    seen: list[list[str]] = []
    fake = _device(
        pidof=(_app_pids(recorded)[-1], 0),
        window=_window(recorded),
        recorded=recorded,
    )

    def recording_run(cmd, *args, **kwargs):
        seen.append(cmd)
        return fake(cmd, *args, **kwargs)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", recording_run)
    AppStateCapture(package=APP)._capture_logs(tmp_path / "app-logs.txt", "30s")

    command = next(cmd for cmd in seen if "logcat" in cmd)
    assert logcat.BINARY_FLAG in command, f"not records: the device's text gets parsed: {command}"


def test_an_app_with_no_process_in_the_window_is_refused_not_widened(
    monkeypatch, tmp_path, recorded
):
    """Not running, and nowhere in the window: the old answer was the whole device."""
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        _device(
            pidof=_not_running(recorded),
            window=recorded.data("logcat_crash_buffer_records.bin"),
        ),
    )

    with pytest.raises(app_state_capture.cli.Failure) as refused:
        AppStateCapture(package=APP)._capture_logs(tmp_path / "app-logs.txt", "30s")

    assert APP in str(refused.value)
    assert "--no-logs" in refused.value.remedy
    assert not (tmp_path / "app-logs.txt").exists(), "a refusal left a log behind"


def test_the_refusal_fails_the_snapshot_and_keeps_its_remedy(
    monkeypatch, tmp_path, capsys, recorded
):
    """Through the X8 partial path: exit 1, the rest of the snapshot handed over."""
    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        _device(
            pidof=_not_running(recorded),
            window=recorded.data("logcat_crash_buffer_records.bin"),
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["app_state_capture.py", "--package", APP, "--output", str(tmp_path), "--json"],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)
    payload = json.loads(capsys.readouterr().out)

    assert excinfo.value.code == 1
    assert set(payload) == {"success", "error", "remedy", "partial"}, payload
    assert APP in payload["error"] and "--no-logs" in payload["error"]
    assert payload["partial"]["logs"]["captured"] is False
    assert "app-logs.txt" not in payload["partial"]["artifacts"]
    assert "ui-hierarchy.json" in payload["partial"]["artifacts"]


# === A3: a snapshot whose logs leave the app's lines out says so ============


def _without_the_crashed_start(recorded) -> tuple[bytes, str]:
    """The restart window as if the crashed process had started before it opened."""
    crashed = _app_pids(recorded)[0]
    window = frames(_window(recorded))
    kept = [f for f in window if not f.message.startswith(f"Start proc {crashed}:")]
    if len(kept) != len(window) - 1:
        pytest.fail("the recording no longer starts the crashed process exactly once")
    return framed(kept), crashed


def test_the_saved_log_reads_back_as_the_lines_it_counted(monkeypatch, tmp_path, recorded):
    """A saved log is safe for an ordinary reader: read back in text mode, the same lines.

    Derived: the restart window with the live process's first I record given
    a carriage return and a recorded E line (header and all). The counts were
    right (LF-only, A2), but a text-mode reread of app-logs.txt turned that
    record into two lines, the second a forged E (review of PR #35). The
    message's control characters are printed escaped now.
    """
    entry = _manifest_entry(recorded, RECORDS)
    alive = entry["pids"][-1]
    window = frames(_window(recorded))
    at = next(i for i, f in enumerate(window) if f.pid == alive and f.priority == "I")
    error = next(
        (line for line in recorded.lines("logcat_app_restart_usec") if line.split()[4:5] == ["E"]),
        None,
    )
    if error is None:
        pytest.fail("the recording no longer holds an E line")
    window[at] = window[at].replace(message=f"{window[at].message}\r{error}")
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        _device(
            pidof=(alive, 0),
            window=framed(window),
            system_server=entry["system_server_pid"],
            recorded=recorded,
        ),
    )
    stats = AppStateCapture(package=APP, serial="emulator-5554")._capture_logs(
        tmp_path / "app-logs.txt", "30s", log_lines=0
    )
    reread = (tmp_path / "app-logs.txt").read_text(encoding="utf-8")
    assert len(reread.splitlines()) == stats["lines"]
    assert analyze_logcat(reread) == {k: stats[k] for k in ("lines", "errors", "warnings")}


def test_a3_a_crash_whose_start_predates_the_window_is_reported(
    monkeypatch, tmp_path, capsys, recorded
):
    """A3: its lines are left out -- and the snapshot says so, everywhere it speaks.

    Derived: the restart window without the crashed process's `Start proc`.
    Nothing uid-carrying names that process, so its records stay out; they
    were dropped with no trace, the snapshot reporting its errors as the
    app's whole story. logd stamped them with the package's uid, so they are
    counted and named: in the logs block (``attribution``), on stderr, in
    summary.md, and in the text the command prints.
    """
    window, crashed = _without_the_crashed_start(recorded)
    left_out = len(_rendered(window, {crashed}))
    entry = _manifest_entry(recorded, RECORDS)
    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        _device(
            pidof=(entry["pids"][-1], 0),
            window=window,
            system_server=entry["system_server_pid"],
            recorded=recorded,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["app_state_capture.py", "--package", APP, "--output", str(tmp_path), "--log-lines", "0"],
    )
    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)
    captured = capsys.readouterr()

    assert excinfo.value.code == 0, captured.err
    summary = json.loads(next(tmp_path.rglob("snapshot-summary.json")).read_text("utf-8"))
    assert crashed not in summary["logs"]["pids"]
    assert summary["logs"].get("attribution") == {
        "complete": False,
        "unresolved_lines": left_out,
        "unresolved_pids": [crashed],
    }
    assert f"Incomplete: {left_out} line(s)" in captured.err
    assert "Log attribution INCOMPLETE" in captured.out
    assert "Attribution: INCOMPLETE" in next(tmp_path.rglob("summary.md")).read_text("utf-8")


# === N12: by default a snapshot goes under the skill's cache, not the cwd ===


def test_the_default_snapshot_dir_follows_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert app_state_capture.default_snapshot_dir() == (
        tmp_path / ".android-emulator-skill" / "cache" / "app-state-snapshots"
    )


def test_a_default_capture_leaves_the_cwd_alone(monkeypatch, tmp_path, capsys):
    """N12: `--output` defaulted to the relative `app-state-snapshots`."""
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)

    code = _snapshot_cli_without_output(monkeypatch, ["--no-logs", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    snapshot = Path(payload["snapshot_path"])
    assert snapshot.is_absolute(), "the path printed must not depend on the cwd"
    assert app_state_capture.default_snapshot_dir() in snapshot.parents
    assert (snapshot / "snapshot-summary.json").is_file()
    assert not list(cwd.iterdir())


def _snapshot_cli_without_output(monkeypatch, argv: list[str]):
    monkeypatch.setattr(
        app_state_capture,
        "capture_screenshot",
        lambda *_a, **_k: {"mode": "file", "file_path": "screenshot.png", "size_bytes": 10},
    )
    monkeypatch.setattr(app_state_capture, "get_ui_hierarchy", lambda *_a, **_k: {"tag": "root"})
    monkeypatch.setattr(
        app_state_capture.adb_exec.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["app_state_capture.py", "--package", APP, *argv])
    with pytest.raises(SystemExit) as excinfo:
        cli.run(app_state_capture.main)
    return excinfo.value.code
