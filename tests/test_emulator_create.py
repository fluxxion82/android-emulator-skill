"""Device-free tests for emulator_create.

These exercise the pure logic added for the three feature deltas without any
SDK / avdmanager / sdkmanager binary:

  1. ``--api`` defaults to the latest *installed* system-image API level.
  2. ``--name`` is auto-generated from device + API when omitted.
  3. ``--device`` is fuzzy-matched against avdmanager device definitions.

Where a subprocess boundary exists, the module's ``subprocess`` is monkeypatched
so the arg->command mapping (and parsing) is asserted without a real toolchain.
"""

from __future__ import annotations

import importlib
import subprocess

import emulator_create
import pytest

DEVICES = [
    {"id": "pixel_7", "name": "Pixel 7"},
    {"id": "pixel_7_pro", "name": "Pixel 7 Pro"},
    {"id": "pixel_tablet", "name": "Pixel Tablet"},
    {"id": "Nexus 6", "name": "Nexus 6"},
]


# --- Delta 3: fuzzy device matching ----------------------------------------


def test_fuzzy_exact_id():
    assert emulator_create.fuzzy_match_device("pixel_7", DEVICES) == "pixel_7"


def test_fuzzy_exact_id_case_insensitive():
    assert emulator_create.fuzzy_match_device("PIXEL_7", DEVICES) == "pixel_7"


def test_fuzzy_normalized_name_match():
    # "Pixel 7" (name) normalizes to the same token as id "pixel_7".
    assert emulator_create.fuzzy_match_device("Pixel 7", DEVICES) == "pixel_7"


def test_fuzzy_collapsed_spacing():
    # "pixel7" with no separator still resolves to pixel_7.
    assert emulator_create.fuzzy_match_device("pixel7", DEVICES) == "pixel_7"


def test_fuzzy_substring_prefers_specific():
    # "pixel 7 pro" should land on the pro variant, not the base.
    assert emulator_create.fuzzy_match_device("pixel 7 pro", DEVICES) == "pixel_7_pro"


def test_fuzzy_typo_within_cutoff():
    assert emulator_create.fuzzy_match_device("pixxel_tablet", DEVICES) == "pixel_tablet"


def test_fuzzy_no_match_returns_none():
    assert emulator_create.fuzzy_match_device("totally-unknown-device", DEVICES) is None


def test_fuzzy_empty_inputs():
    assert emulator_create.fuzzy_match_device("", DEVICES) is None
    assert emulator_create.fuzzy_match_device("pixel_7", []) is None


def test_suggest_devices_orders_by_closeness():
    suggestions = emulator_create.suggest_devices("pixel", DEVICES, limit=2)
    assert len(suggestions) == 2
    assert all(s in {d["id"] for d in DEVICES} for s in suggestions)
    assert suggestions[0].startswith("pixel")


# --- Delta 1: latest installed API default ---------------------------------


def test_latest_api_level_picks_max():
    images = [
        {"api_level": 31},
        {"api_level": 34},
        {"api_level": 33},
    ]
    assert emulator_create.latest_api_level(images) == 34


def test_latest_api_level_empty():
    assert emulator_create.latest_api_level([]) is None


def test_latest_api_level_ignores_non_int():
    assert emulator_create.latest_api_level([{"api_level": None}, {"foo": 1}]) is None


def test_resolve_api_level_honors_explicit(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    # Explicit value short-circuits; installed list never consulted.
    monkeypatch.setattr(
        creator, "list_installed_system_images", lambda: (_ for _ in ()).throw(AssertionError)
    )
    assert creator.resolve_api_level(33) == 33


def test_resolve_api_level_defaults_to_latest_installed(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(
        creator,
        "list_installed_system_images",
        lambda: [{"api_level": 30}, {"api_level": 34}],
    )
    assert creator.resolve_api_level(None) == 34


def test_resolve_api_level_none_when_nothing_installed(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "list_installed_system_images", lambda: [])
    assert creator.resolve_api_level(None) is None


def test_list_installed_system_images_parses_sdkmanager(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: "/fake/sdkmanager")

    installed_stdout = (
        "Installed packages:\n"
        "  Path                                        | Version | Description\n"
        "  -------                                     | ------- | -------\n"
        "  system-images;android-34;google_apis;x86_64 | 7       | Google APIs\n"
        "  system-images;android-30;default;arm64-v8a  | 5       | Default\n"
        "  platforms;android-34                         | 2       | Android SDK Platform 34\n"
    )

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["check"] = kwargs.get("check")
        return subprocess.CompletedProcess(cmd, 0, stdout=installed_stdout, stderr="")

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    images = creator.list_installed_system_images()

    assert captured["cmd"] == ["/fake/sdkmanager", "--list_installed"]
    assert captured["check"] is True
    assert {img["api_level"] for img in images} == {34, 30}
    # platforms;* lines (not system-images) are ignored.
    assert all(img["id"].startswith("system-images;") for img in images)
    assert emulator_create.latest_api_level(images) == 34


# --- Delta 2: auto-generated AVD name --------------------------------------


def test_generate_avd_name_basic():
    assert emulator_create.generate_avd_name("pixel_7", 34) == "pixel_7_API_34"


def test_generate_avd_name_sanitizes_spaces():
    assert emulator_create.generate_avd_name("Pixel 7 Pro", 33) == "Pixel_7_Pro_API_33"


def test_generate_avd_name_collapses_separators():
    assert emulator_create.generate_avd_name("  Pixel--7  ", 31) == "Pixel_7_API_31"


def test_generate_avd_name_empty_base_falls_back():
    assert emulator_create.generate_avd_name("@@@", 30) == "AVD_API_30"


# --- create() arg->command mapping (subprocess mocked) ---------------------


def test_create_builds_expected_avdmanager_command(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "get_avdmanager_path", lambda: "/fake/avdmanager")
    # No system-image pre-check (skips that subprocess call path).
    monkeypatch.setattr(creator, "get_sdkmanager_path", lambda: None)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["check"] = kwargs.get("check")
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(emulator_create.subprocess, "run", fake_run)

    success, _message, avd_name = creator.create(
        device_id="pixel_7", api_level=34, name="MyAVD", abi="x86_64", variant="google_apis"
    )

    assert success is True
    assert avd_name == "MyAVD"
    assert captured["check"] is True
    assert captured["input"] == "no\n"
    assert captured["cmd"] == [
        "/fake/avdmanager",
        "create",
        "avd",
        "--name",
        "MyAVD",
        "--package",
        "system-images;android-34;google_apis;x86_64",
        "--device",
        "pixel_7",
    ]


def test_resolve_device_returns_suggestions_on_miss(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "list_device_definitions", lambda: DEVICES)
    device_id, suggestions = creator.resolve_device("zzzzzzzz")
    assert device_id is None
    assert suggestions  # non-empty close-match list for the error message


def test_resolve_device_matches(monkeypatch):
    creator = emulator_create.EmulatorCreator()
    monkeypatch.setattr(creator, "list_device_definitions", lambda: DEVICES)
    device_id, suggestions = creator.resolve_device("Pixel 7")
    assert device_id == "pixel_7"
    assert suggestions == []


# --- tunable env override ---------------------------------------------------


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_DEVICE_MATCH_CUTOFF", "90")
    monkeypatch.setenv("ANDROID_EMU_DEVICE_MATCH_SUGGEST", "3")
    reloaded = importlib.reload(emulator_create)
    try:
        assert reloaded.DEVICE_MATCH_CUTOFF == 90
        assert reloaded.DEVICE_MATCH_SUGGEST == 3
    finally:
        monkeypatch.delenv("ANDROID_EMU_DEVICE_MATCH_CUTOFF", raising=False)
        monkeypatch.delenv("ANDROID_EMU_DEVICE_MATCH_SUGGEST", raising=False)
        importlib.reload(emulator_create)


def test_default_tunables():
    assert emulator_create.DEVICE_MATCH_CUTOFF == 60
    assert emulator_create.DEVICE_MATCH_SUGGEST == 5
