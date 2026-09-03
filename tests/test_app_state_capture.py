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
from app_state_capture import AppStateCapture, analyze_logcat, count_ui_elements


def _node(tag: str, *children: dict) -> dict:
    return {"tag": tag, "attributes": {}, "children": list(children)}


def test_count_ui_elements_counts_all_nodes():
    tree = _node("root", _node("a", _node("a1")), _node("b"))
    # root + a + a1 + b == 4
    assert count_ui_elements(tree) == 4


def test_count_ui_elements_single_node():
    assert count_ui_elements(_node("only")) == 1


def test_count_ui_elements_handles_none_and_empty():
    assert count_ui_elements(None) == 0
    assert count_ui_elements({}) == 0


def test_analyze_logcat_counts_priority_columns():
    text = (
        "06-17 10:00:00.000  1000  1000 E ActivityManager: boom\n"
        "06-17 10:00:01.000  1000  1000 W WindowManager: heads up\n"
        "06-17 10:00:02.000  1000  1000 I MyApp: all good\n"
        "06-17 10:00:03.000  1000  1000 D MyApp: debug\n"
    )
    stats = analyze_logcat(text)
    assert stats["lines"] == 4
    assert stats["errors"] == 1
    assert stats["warnings"] == 1


def test_analyze_logcat_counts_free_text_and_brief_format():
    text = "E/Tag( 123): fatal exception\nW/Tag( 123): a warning here\n\n"
    stats = analyze_logcat(text)
    assert stats["lines"] == 2  # blank line ignored
    assert stats["errors"] == 1
    assert stats["warnings"] == 1


def test_analyze_logcat_empty():
    stats = analyze_logcat("")
    assert stats == {"lines": 0, "errors": 0, "warnings": 0}


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
    the recording's errors and warnings all sit outside its tail, so capping
    demonstrably drops them instead of a hand-placed `E Tag: line0 error`
    doing so by construction.
    """
    log_text = recorded.text("logcat_threadtime")
    tail = log_text.split("\n")[-3:]

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd:
            return SimpleNamespace(stdout="4242\n", stderr="", returncode=0)
        if "logcat" in cmd:
            return SimpleNamespace(stdout=log_text, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    uncapped = capturer._capture_logs(tmp_path / "all.txt", "30s", log_lines=0)
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=3)

    # The newest lines are the ones kept.
    assert (tmp_path / "app-logs.txt").read_text().split("\n") == tail
    assert stats["captured"] is True
    assert stats["lines"] < uncapped["lines"]

    # And the counts follow the cap rather than the whole window: every error
    # and warning in this recording is older than the retained tail.
    assert uncapped["errors"] > 0 and uncapped["warnings"] > 0
    assert stats["errors"] == 0
    assert stats["warnings"] == 0


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


def test_unknown_serial_exits_one_with_an_actionable_message(
    monkeypatch, capsys, tmp_path, recorded_anywhere
):
    """A wrong --serial must yield a remedy and exit 1, never a traceback."""
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
        app_state_capture.main()

    assert excinfo.value.code == 1
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


def _snapshot_cli(monkeypatch, tmp_path, argv: list[str]):
    """Run main() over a device that answers, with everything but logs stubbed."""
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
    monkeypatch.setattr(
        sys,
        "argv",
        ["app_state_capture.py", "--package", "com.example.app", "--output", str(tmp_path), *argv],
    )
    with pytest.raises(SystemExit) as excinfo:
        app_state_capture.main()
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
    """`--json` on failure is `{"error": ..., "partial": ...}` -- and the
    partial half is what makes keeping the artifacts worth anything."""
    code = _snapshot_cli(monkeypatch, tmp_path, ["--logs", "1x", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert set(payload) == {"error", "partial"}, payload
    assert "screenshot.png" in payload["partial"]["artifacts"]
    assert payload["partial"]["logs"]["captured"] is False


def test_a_screenshot_that_raises_fails_the_snapshot(monkeypatch, tmp_path, capsys):
    """`capture_screenshot` raises on failure, and the whole capture used to go
    with it -- including the artifacts collected after it would have been."""

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
        app_state_capture.main()
    payload = json.loads(capsys.readouterr().out)

    assert excinfo.value.code == 1
    assert set(payload) == {"error", "partial"}, payload
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
    all.
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
        app_state_capture.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert excinfo.value.code == 1
    assert "Traceback" not in captured.err
    assert set(payload) == {"error", "partial"}, payload
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
