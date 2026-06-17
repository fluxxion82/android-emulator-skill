"""Device-free tests for emulator_selector.

These exercise the pure logic — config.ini parsing, API-level extraction,
common-model matching, and the candidate ranking function — plus the
recent-use config read/write and adb-serial -> AVD-name resolution, all with
``subprocess``/filesystem mocked so no emulator or adb is needed.
"""

from __future__ import annotations

import importlib
import json

import emulator_selector
import pytest


# ---------------------------------------------------------------------------
# config.ini parsing + API-level extraction (pure)
# ---------------------------------------------------------------------------
def test_parse_config_ini_basic():
    text = "\n".join(
        [
            "# a comment",
            "AvdId=Pixel_9_Pro",
            "abi.type = arm64-v8a",
            "image.sysdir.1=system-images/android-36/google_apis_playstore/arm64-v8a/",
            "",
            "blank-without-equals",
        ]
    )
    config = emulator_selector.parse_config_ini(text)
    assert config["AvdId"] == "Pixel_9_Pro"
    assert config["abi.type"] == "arm64-v8a"
    assert "image.sysdir.1" in config
    assert "blank-without-equals" not in config


def test_parse_api_level_from_sysdir():
    config = {"image.sysdir.1": "system-images/android-34/google_apis/x86_64/"}
    assert emulator_selector.parse_api_level(config) == 34


def test_parse_api_level_falls_back_to_target():
    config = {"target": "android-30"}
    assert emulator_selector.parse_api_level(config) == 30


def test_parse_api_level_none_when_absent():
    assert emulator_selector.parse_api_level({"foo": "bar"}) is None


# ---------------------------------------------------------------------------
# common-model matching (pure)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "candidate,expected_rank",
    [
        ({"name": "Pixel_8_Pro"}, 0),
        ({"device": "pixel_8"}, 1),
        ({"display_name": "Pixel 7 Pro"}, 2),
        ({"name": "My_Pixel_AVD"}, 5),
        ({"name": "Samsung_S22_Plus"}, None),
    ],
)
def test_common_model_rank(candidate, expected_rank):
    assert emulator_selector.common_model_rank(candidate) == expected_rank


# ---------------------------------------------------------------------------
# ranking function (pure) — the primary unit-test target
# ---------------------------------------------------------------------------
def _candidate(name, api=None, running=False, recent_index=None, device=""):
    return {
        "name": name,
        "api_level": api,
        "device": device,
        "display_name": "",
        "running": running,
        "recent_index": recent_index,
    }


def test_rank_empty():
    assert emulator_selector.rank_candidates([]) == []


def test_rank_running_beats_everything():
    candidates = [
        _candidate("Old_Pixel", api=30, running=True),
        _candidate("Pixel_8_Pro", api=36, recent_index=0),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    assert ranked[0]["name"] == "Old_Pixel"
    assert "Recommended" in ranked[0]["reasons"]
    assert "Currently running" in ranked[0]["reasons"]


def test_rank_recent_beats_latest_api_and_model():
    candidates = [
        _candidate("Legacy_AVD", api=28, recent_index=0),
        _candidate("Pixel_8_Pro", api=36),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    assert ranked[0]["name"] == "Legacy_AVD"
    assert "Recently used" in ranked[0]["reasons"]


def test_rank_latest_api_reason_and_tiebreak():
    candidates = [
        _candidate("Pixel_7", api=33),
        _candidate("Pixel_8_Pro", api=34),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    # Latest API (34) + better common-model rank wins.
    assert ranked[0]["name"] == "Pixel_8_Pro"
    assert any("Latest API (34)" in r for r in ranked[0]["reasons"])


def test_rank_common_model_breaks_tie_when_api_equal():
    candidates = [
        _candidate("Generic_Device", api=34, device="generic"),
        _candidate("Pixel_8", api=34, device="pixel_8"),
    ]
    ranked = emulator_selector.rank_candidates(candidates)
    assert ranked[0]["name"] == "Pixel_8"


def test_rank_is_stable_and_nondestructive():
    original = [_candidate("B_AVD", api=34), _candidate("A_AVD", api=34)]
    ranked = emulator_selector.rank_candidates(original)
    # Equal score -> alphabetical name tie-break.
    assert [c["name"] for c in ranked] == ["A_AVD", "B_AVD"]
    # Inputs are not mutated (no score/reasons leak back).
    assert "score" not in original[0]
    assert "reasons" not in original[0]


# ---------------------------------------------------------------------------
# recent-use config read/write (filesystem via tmp_path, no mocking needed)
# ---------------------------------------------------------------------------
def test_record_and_load_recent_dedup_and_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(emulator_selector, "RECENT_HISTORY_MAX", 3)
    config_path = tmp_path / "config.json"
    selector = emulator_selector.EmulatorSelector(config_path=config_path)

    selector.record_recent("A")
    selector.record_recent("B")
    selector.record_recent("A")  # move A back to front, dedup
    selector.record_recent("C")
    selector.record_recent("D")  # exceeds cap of 3

    recent = selector.load_recent()
    assert recent == ["D", "C", "A"]
    # File is valid JSON with the documented shape.
    assert json.loads(config_path.read_text())["recent"] == ["D", "C", "A"]


def test_load_recent_missing_file_returns_empty(tmp_path):
    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "nope.json")
    assert selector.load_recent() == []


# ---------------------------------------------------------------------------
# adb serial -> AVD name resolution (subprocess mocked)
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


def test_running_avd_names_resolves_serials(monkeypatch, tmp_path):
    monkeypatch.setattr(
        emulator_selector,
        "get_connected_devices",
        lambda: [
            {"serial": "emulator-5554", "state": "device", "type": "emulator"},
            {"serial": "emulator-5556", "state": "offline", "type": "emulator"},
            {"serial": "ABC123", "state": "device", "type": "device"},
        ],
    )

    def fake_run(cmd, **_kwargs):
        # `adb -s emulator-5554 emu avd name` -> name then OK line.
        assert "emu" in cmd
        return _FakeResult("Pixel_9_Pro\nOK\n")

    monkeypatch.setattr(emulator_selector.subprocess, "run", fake_run)

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    running = selector.running_avd_names()
    # Only the ready emulator is resolved; offline + real device are skipped.
    assert running == {"Pixel_9_Pro"}


# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------
def test_default_tunables():
    assert emulator_selector.DEFAULT_SUGGEST_COUNT == 4
    assert emulator_selector.RECENT_HISTORY_MAX == 10
    assert emulator_selector.RUNNING_SCORE == 10000
    assert emulator_selector.RECENT_SCORE == 1000


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_SELECTOR_COUNT", "2")
    monkeypatch.setenv("ANDROID_EMU_SELECTOR_RUNNING_PTS", "999")
    reloaded = importlib.reload(emulator_selector)
    try:
        assert reloaded.DEFAULT_SUGGEST_COUNT == 2
        assert reloaded.RUNNING_SCORE == 999
    finally:
        monkeypatch.delenv("ANDROID_EMU_SELECTOR_COUNT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_SELECTOR_RUNNING_PTS", raising=False)
        importlib.reload(emulator_selector)
