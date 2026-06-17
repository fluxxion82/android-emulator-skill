"""Device-free tests for app_state_capture feature deltas.

Covers the pure helpers (UI element counting, logcat error/warning parsing)
and the arg->adb-command mapping for device-info probes and the log-lines cap,
with the module's ``subprocess.run`` monkeypatched so no device is needed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import app_state_capture
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

    monkeypatch.setattr(app_state_capture.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    info = capturer._get_device_info()

    assert info == {"model": "Pixel 7", "sdk": 34, "density": 420}

    # Verify the adb command shapes (serial targeting + getprop/wm density).
    joined = [" ".join(c) for c in calls]
    assert any(
        c == "adb -s emulator-5554 shell getprop ro.product.model" for c in joined
    ), joined
    assert any(
        c == "adb -s emulator-5554 shell getprop ro.build.version.sdk" for c in joined
    ), joined
    assert any(c == "adb -s emulator-5554 shell wm density" for c in joined), joined


def test_capture_logs_respects_log_lines_cap_and_counts(monkeypatch, tmp_path):
    """--log-lines caps retained lines (keeping the newest) and stats reflect the cap."""
    # 5 numbered lines; line 0 is an ERROR, line 1 a WARNING.
    log_text = "\n".join(
        [
            "06-17 10:00:00.000 1 1 E Tag: line0 error",
            "06-17 10:00:01.000 1 1 W Tag: line1 warning",
            "06-17 10:00:02.000 1 1 I Tag: line2",
            "06-17 10:00:03.000 1 1 I Tag: line3",
            "06-17 10:00:04.000 1 1 I Tag: line4",
        ]
    )

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd:
            return SimpleNamespace(stdout="4242\n", stderr="", returncode=0)
        if "logcat" in cmd:
            return SimpleNamespace(stdout=log_text, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.subprocess, "run", fake_run)

    capturer = AppStateCapture(package="com.example.app", serial="emulator-5554")
    out_path = tmp_path / "app-logs.txt"
    stats = capturer._capture_logs(out_path, "30s", log_lines=2)

    # Only the last 2 lines retained -> the early error/warning are dropped.
    written = out_path.read_text().split("\n")
    assert written == ["06-17 10:00:03.000 1 1 I Tag: line3", "06-17 10:00:04.000 1 1 I Tag: line4"]
    assert stats["captured"] is True
    assert stats["lines"] == 2
    assert stats["errors"] == 0
    assert stats["warnings"] == 0


def test_capture_logs_invalid_duration_reports_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(
        app_state_capture.subprocess,
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
