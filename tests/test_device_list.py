"""Device-free tests for device_list.

These exercise the PURE parsing/merging/filtering logic (no subprocess, no
device): ``adb devices -l`` parsing, ``emulator -list-avds`` parsing,
``avdmanager list avd`` metadata parsing, AVD merge, substring filtering, and
the summary-count aggregation. The one collect() test monkeypatches the
module's ``subprocess.run`` so no real adb/emulator is touched.
"""

from __future__ import annotations

import importlib

import device_list

# ---------------------------------------------------------------------------
# parse_adb_devices
# ---------------------------------------------------------------------------
ADB_OUTPUT = """List of devices attached
emulator-5554          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 device:emu64x transport_id:1
ABC123DEF456           device product:redfin model:Pixel_5 device:redfin transport_id:2
99887766               offline
77665544               unauthorized
"""


def test_parse_adb_devices_extracts_records():
    devices = device_list.parse_adb_devices(ADB_OUTPUT)
    assert len(devices) == 4

    emu = devices[0]
    assert emu["serial"] == "emulator-5554"
    assert emu["type"] == "emulator"
    assert emu["state"] == "device"
    assert emu["online"] is True
    assert emu["model"] == "sdk_gphone64_x86_64"

    phys = devices[1]
    assert phys["serial"] == "ABC123DEF456"
    assert phys["type"] == "device"
    assert phys["model"] == "Pixel_5"
    assert phys["online"] is True


def test_parse_adb_devices_offline_and_unauthorized_not_online():
    devices = device_list.parse_adb_devices(ADB_OUTPUT)
    by_serial = {d["serial"]: d for d in devices}
    assert by_serial["99887766"]["online"] is False
    assert by_serial["99887766"]["state"] == "offline"
    assert by_serial["77665544"]["online"] is False
    assert by_serial["77665544"]["state"] == "unauthorized"


def test_parse_adb_devices_skips_header_and_daemon_noise():
    output = (
        "* daemon not running; starting now at tcp:5037 *\n"
        "* daemon started successfully *\n"
        "List of devices attached\n"
        "emulator-5554   device model:Foo\n"
    )
    devices = device_list.parse_adb_devices(output)
    assert len(devices) == 1
    assert devices[0]["serial"] == "emulator-5554"


def test_parse_adb_devices_empty():
    assert device_list.parse_adb_devices("List of devices attached\n") == []
    assert device_list.parse_adb_devices("") == []


def test_parse_adb_devices_missing_model_yields_empty_model():
    devices = device_list.parse_adb_devices("List of devices attached\nXYZ device\n")
    assert devices[0]["model"] == ""


# ---------------------------------------------------------------------------
# parse_emulator_avds
# ---------------------------------------------------------------------------
def test_parse_emulator_avds_names():
    output = "Pixel_5_API_33\nPixel_7_API_34\n"
    avds = device_list.parse_emulator_avds(output)
    assert [a["name"] for a in avds] == ["Pixel_5_API_33", "Pixel_7_API_34"]
    assert all(a["kind"] == "avd" and a["online"] is False for a in avds)


def test_parse_emulator_avds_skips_banner_lines_with_spaces():
    output = "INFO | Storing crashdata in: /tmp/foo\nPixel_5_API_33\n"
    avds = device_list.parse_emulator_avds(output)
    assert [a["name"] for a in avds] == ["Pixel_5_API_33"]


def test_parse_emulator_avds_empty():
    assert device_list.parse_emulator_avds("") == []


# ---------------------------------------------------------------------------
# parse_avdmanager_avds
# ---------------------------------------------------------------------------
AVDMANAGER_OUTPUT = """Available Android Virtual Devices:
    Name: Pixel_5_API_33
  Device: pixel_5 (Google)
    Path: /Users/me/.android/avd/Pixel_5_API_33.avd
  Target: Google APIs (Google Inc.)
          Based on: Android 13 (Tiramisu) Tag/ABI: google_apis/x86_64
---------
    Name: Pixel_7_API_34
  Device: pixel_7 (Google)
    Path: /Users/me/.android/avd/Pixel_7_API_34.avd
  Target: Android 14
          Based on: Android 14 (UpsideDownCake) Tag/ABI: default/arm64-v8a
"""


def test_parse_avdmanager_avds_metadata():
    meta = device_list.parse_avdmanager_avds(AVDMANAGER_OUTPUT)
    assert set(meta.keys()) == {"Pixel_5_API_33", "Pixel_7_API_34"}

    p5 = meta["Pixel_5_API_33"]
    assert p5["device"] == "pixel_5 (Google)"
    assert p5["target"] == "Google APIs (Google Inc.)"
    assert p5["based_on"] == "Android 13 (Tiramisu)"
    assert p5["abi"] == "google_apis/x86_64"

    p7 = meta["Pixel_7_API_34"]
    assert p7["abi"] == "default/arm64-v8a"
    assert p7["based_on"] == "Android 14 (UpsideDownCake)"


def test_parse_avdmanager_avds_empty():
    assert device_list.parse_avdmanager_avds("") == {}
    assert device_list.parse_avdmanager_avds("Available Android Virtual Devices:\n") == {}


# ---------------------------------------------------------------------------
# merge_avds
# ---------------------------------------------------------------------------
def test_merge_avds_enriches_matching_names():
    names = device_list.parse_emulator_avds("Pixel_5_API_33\nGhost_AVD\n")
    meta = device_list.parse_avdmanager_avds(AVDMANAGER_OUTPUT)
    merged = device_list.merge_avds(names, meta)

    by_name = {a["name"]: a for a in merged}
    # Names from emulator are the source of truth -> both kept.
    assert set(by_name.keys()) == {"Pixel_5_API_33", "Ghost_AVD"}
    # Matching AVD enriched.
    assert by_name["Pixel_5_API_33"]["abi"] == "google_apis/x86_64"
    # Unknown-to-avdmanager AVD kept without metadata, never dropped.
    assert "abi" not in by_name["Ghost_AVD"]


# ---------------------------------------------------------------------------
# matches_filter
# ---------------------------------------------------------------------------
def test_matches_filter_device_fields_case_insensitive():
    dev = {"serial": "emulator-5554", "model": "Pixel_5", "type": "emulator"}
    assert device_list.matches_filter(dev, "pixel") is True
    assert device_list.matches_filter(dev, "EMULATOR") is True
    assert device_list.matches_filter(dev, "5554") is True
    assert device_list.matches_filter(dev, "nexus") is False


def test_matches_filter_avd_fields():
    avd = {"name": "Pixel_7_API_34", "abi": "default/arm64-v8a", "target": "Android 14"}
    assert device_list.matches_filter(avd, "api_34") is True
    assert device_list.matches_filter(avd, "arm64") is True
    assert device_list.matches_filter(avd, "android 14") is True
    assert device_list.matches_filter(avd, "x86") is False


def test_matches_filter_empty_needle_matches_all():
    assert device_list.matches_filter({"serial": "x"}, "") is True


# ---------------------------------------------------------------------------
# DeviceLister.collect (subprocess mocked)
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _fake_run_factory(adb_out: str, emulator_out: str, avdmanager_out: str):
    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["adb", "devices"]:
            return _FakeResult(adb_out)
        if cmd[:2] == ["emulator", "-list-avds"]:
            return _FakeResult(emulator_out)
        if cmd[:1] == ["avdmanager"]:
            return _FakeResult(avdmanager_out)
        return _FakeResult("", returncode=1)

    return fake_run


def test_collect_aggregates_counts(monkeypatch):
    monkeypatch.setattr(
        device_list.subprocess,
        "run",
        _fake_run_factory(ADB_OUTPUT, "Pixel_5_API_33\nPixel_7_API_34\n", AVDMANAGER_OUTPUT),
    )

    data = device_list.DeviceLister().collect()
    summary = data["summary"]

    assert summary["online"] == 2  # emulator-5554 + ABC123DEF456
    assert summary["offline"] == 2  # offline + unauthorized
    assert summary["avds"] == 2
    assert summary["total"] == 4 + 2
    # AVDs enriched from avdmanager.
    p5 = next(a for a in data["avds"] if a["name"] == "Pixel_5_API_33")
    assert p5["abi"] == "google_apis/x86_64"


def test_collect_filter_narrows_devices_and_avds(monkeypatch):
    monkeypatch.setattr(
        device_list.subprocess,
        "run",
        _fake_run_factory(ADB_OUTPUT, "Pixel_5_API_33\nPixel_7_API_34\n", AVDMANAGER_OUTPUT),
    )

    data = device_list.DeviceLister(name_filter="Pixel_5").collect()
    # Only the AVD named Pixel_5_API_33 matches; physical device model is "Pixel_5".
    assert [a["name"] for a in data["avds"]] == ["Pixel_5_API_33"]
    assert [d["serial"] for d in data["devices"]] == ["ABC123DEF456"]


def test_collect_handles_missing_tools(monkeypatch):
    def boom(_cmd, **_kwargs):
        raise FileNotFoundError("adb not found")

    monkeypatch.setattr(device_list.subprocess, "run", boom)

    data = device_list.DeviceLister().collect()
    assert data["devices"] == []
    assert data["avds"] == []
    assert data["summary"] == {"total": 0, "online": 0, "offline": 0, "avds": 0}


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
def test_default_tunables():
    assert device_list.SUMMARY_PREVIEW_COUNT == 3
    assert device_list.LIST_COMMAND_TIMEOUT == 15


def test_tunables_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_LIST_PREVIEW_COUNT", "1")
    monkeypatch.setenv("ANDROID_EMU_LIST_TIMEOUT", "42")
    reloaded = importlib.reload(device_list)
    try:
        assert reloaded.SUMMARY_PREVIEW_COUNT == 1
        assert reloaded.LIST_COMMAND_TIMEOUT == 42
    finally:
        monkeypatch.delenv("ANDROID_EMU_LIST_PREVIEW_COUNT", raising=False)
        monkeypatch.delenv("ANDROID_EMU_LIST_TIMEOUT", raising=False)
        importlib.reload(device_list)
