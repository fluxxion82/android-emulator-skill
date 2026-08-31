"""Device-free tests for test_recorder feature deltas.

Covers the pure / device-mockable logic added in this change:
- ``_assertion_symbol``: per-step status-symbol mapping (✓/✗/none).
- ``generate_report``: returns a dict of artifact paths and writes the
  JSON + markdown reports (the markdown surfaces the assertion symbol).
- ``step``: records ``assertion_passed`` and emits the status symbol, with
  the device-touching helpers (screenshot + UI hierarchy) monkeypatched.

No emulator or adb is required: ``capture_screenshot`` and
``get_ui_hierarchy`` are monkeypatched on the test_recorder module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_recorder
from test_recorder import TestRecorder


def _make_recorder(tmp_path: Path) -> TestRecorder:
    return TestRecorder("Login Flow", output_dir=str(tmp_path), serial="emulator-5554")


def test_assertion_symbol_pass():
    assert TestRecorder._assertion_symbol({"assertion": "x", "assertion_passed": True}) == "✓"


def test_assertion_symbol_fail():
    assert TestRecorder._assertion_symbol({"assertion": "x", "assertion_passed": False}) == "✗"


def test_assertion_symbol_none_when_no_assertion():
    # Steps without an assertion produce no symbol (output stays unchanged).
    assert TestRecorder._assertion_symbol({"description": "tap"}) == ""


def test_slugify_caps_length_and_lowercases():
    slug = TestRecorder._slugify("Open The Main Settings Screen Now Please")
    assert slug == slug.lower()
    assert " " not in slug
    assert len(slug) <= test_recorder.STEP_NAME_MAXLEN


def _patch_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the two device-touching calls used by ``step``."""
    monkeypatch.setattr(
        test_recorder,
        "capture_screenshot",
        lambda *a, **k: {"mode": "file", "file_path": "shot.png"},
    )
    monkeypatch.setattr(test_recorder, "get_ui_hierarchy", lambda serial=None: {"tag": "root"})


def test_step_records_assertion_result_and_symbol(monkeypatch, tmp_path, capsys):
    _patch_device(monkeypatch)
    recorder = _make_recorder(tmp_path)

    recorder.step("Verify home", assertion="Home visible", assertion_passed=True)
    recorder.step("Verify badge", assertion="Badge shown", assertion_passed=False)
    recorder.step("Plain step")  # no assertion -> no symbol

    out = capsys.readouterr().out
    assert "✓ [1] Verify home" in out
    assert "✗ [2] Verify badge" in out
    # The plain step keeps the original (symbol-free) element-count line.
    assert "  [3] Plain step" in out

    assert recorder.steps[0]["assertion_passed"] is True
    assert recorder.steps[1]["assertion_passed"] is False
    assert "assertion" not in recorder.steps[2]


def test_generate_report_returns_artifact_paths(monkeypatch, tmp_path):
    _patch_device(monkeypatch)
    recorder = _make_recorder(tmp_path)
    recorder.step("Verify home", assertion="Home visible", assertion_passed=False)

    artifacts = recorder.generate_report(passed=False)

    # Dict of artifact paths.
    assert set(artifacts) == {"report_path", "markdown_path", "output_dir"}
    report_path = Path(artifacts["report_path"])
    markdown_path = Path(artifacts["markdown_path"])
    assert report_path.exists()
    assert markdown_path.exists()
    assert report_path.parent == Path(artifacts["output_dir"])

    # JSON report carries the recorded assertion result.
    report = json.loads(report_path.read_text())
    assert report["passed"] is False
    assert report["steps"][0]["assertion_passed"] is False

    # Markdown surfaces the failing assertion symbol.
    assert "✗ Home visible" in markdown_path.read_text()


def test_finish_delegates_to_generate_report(monkeypatch, tmp_path):
    _patch_device(monkeypatch)
    recorder = _make_recorder(tmp_path)
    recorder.step("Verify home", assertion="Home visible", assertion_passed=True)

    result = recorder.finish(passed=True)

    # finish() preserves its contract: returns the JSON report path string.
    assert result.endswith("test-report.json")
    assert Path(result).exists()
