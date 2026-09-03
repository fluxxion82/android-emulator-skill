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


def test_parse_adb_devices_extracts_records(recorded):
    """Two attached emulators, as `adb devices -l` actually prints them."""
    devices = device_list.parse_adb_devices(recorded.text("adb_devices_multiple"))
    assert len(devices) == 2

    emu = devices[0]
    assert emu["serial"] == "emulator-5554"
    assert emu["type"] == "emulator"
    assert emu["state"] == "device"
    assert emu["online"] is True
    assert emu["model"] == "sdk_gphone16k_arm64"

    assert [d["serial"] for d in devices] == ["emulator-5554", "emulator-5556"]


def test_a_hardware_serial_is_typed_as_a_device_not_an_emulator(recorded):
    """`type` is read off the serial alone, so only the serial is substituted.

    No `adb devices -l` listing with a handset attached is recorded (and a real
    one carries somebody's device serial), but the classification rule reads
    nothing else: the recorded line is used verbatim apart from the field under
    test. Emulator-vs-device is what the shutdown path keys off, so it needs
    coverage rather than deletion.
    """
    listing = recorded.text("adb_devices_single").replace("emulator-5554", "9B021FFAZ0057H")
    devices = device_list.parse_adb_devices(listing)

    assert len(devices) == 1
    assert devices[0]["serial"] == "9B021FFAZ0057H"
    assert devices[0]["type"] == "device"


def test_parse_adb_devices_skips_the_header(recorded):
    listing = recorded.text("adb_devices_single")
    assert listing.startswith("List of devices attached")
    assert len(device_list.parse_adb_devices(listing)) == 1


def test_parse_adb_devices_skips_daemon_noise(recorded):
    """adb prefixes its own chatter with `*`; those lines are not devices.

    The banner is short enough not to be a transcript by the fixture policy's
    own measure, and it is prepended to a real listing rather than replacing
    one, so the device lines being parsed are still the device's.
    """
    noisy = "* daemon started successfully *\n" + recorded.text("adb_devices_single")
    devices = device_list.parse_adb_devices(noisy)
    assert len(devices) == 1
    assert devices[0]["serial"] == "emulator-5554"


# ---------------------------------------------------------------------------
# FROZEN DEBT, not an exception. `offline` and `unauthorized` are real adb
# states that `parse_adb_devices` documents and reports (`online` is False for
# both), and no recording has either: capturing `unauthorized` needs a handset
# with the RSA prompt pending, and this lane must never drive one.
#
# `test_device_list.py::parse_adb_devices` is frozen in KNOWN_VIOLATIONS for
# this literal. To pay it off, record `adb devices -l` with a device in each
# state -- a second emulator killed mid-boot gives `offline` -- as
# `adb_devices_offline` / `adb_devices_unauthorized`.
# ---------------------------------------------------------------------------

ADB_OUTPUT_WITH_BAD_STATES = """List of devices attached
emulator-5554          device product:sdk_gphone16k_arm64 model:sdk_gphone16k_arm64 device:emu64a16k
99887766               offline
77665544               unauthorized
"""


def test_parse_adb_devices_offline_and_unauthorized_not_online():
    devices = device_list.parse_adb_devices(ADB_OUTPUT_WITH_BAD_STATES)
    by_serial = {d["serial"]: d for d in devices}

    assert by_serial["emulator-5554"]["online"] is True
    assert by_serial["99887766"]["online"] is False
    assert by_serial["99887766"]["state"] == "offline"
    assert by_serial["77665544"]["online"] is False
    assert by_serial["77665544"]["state"] == "unauthorized"


def test_parse_adb_devices_empty():
    assert device_list.parse_adb_devices("List of devices attached\n") == []
    assert device_list.parse_adb_devices("") == []


def test_parse_adb_devices_missing_model_yields_empty_model():
    devices = device_list.parse_adb_devices("List of devices attached\nXYZ device\n")
    assert devices[0]["model"] == ""


# ---------------------------------------------------------------------------
# parse_emulator_avds
# ---------------------------------------------------------------------------
def test_parse_emulator_avds_names(recorded):
    """`emulator -list-avds`: one bare AVD name per line, no header."""
    avds = device_list.parse_emulator_avds(recorded.text("emulator_list_avds"))
    assert [a["name"] for a in avds] == ["Pixel_9"]
    assert all(a["kind"] == "avd" and a["online"] is False for a in avds)


def test_parse_emulator_avds_skips_banner_lines_with_spaces(recorded):
    """`emulator` writes INFO chatter to the same stream; an AVD name has no space.

    Prepended to the recorded listing rather than replacing it, and short
    enough not to be a transcript by the policy's own measure.
    """
    noisy = "INFO | Storing crashdata in: /tmp/x\n" + recorded.text("emulator_list_avds")
    assert [a["name"] for a in device_list.parse_emulator_avds(noisy)] == ["Pixel_9"]


def test_parse_emulator_avds_empty():
    assert device_list.parse_emulator_avds("") == []


# ---------------------------------------------------------------------------
# parse_avdmanager_avds
# ---------------------------------------------------------------------------


def test_parse_avdmanager_avds_metadata(recorded):
    """Real `avdmanager list avd`, whose shape is nothing like a tidy sample.

    Inconsistent leading whitespace (`    Name:` four spaces, `  Device:`
    two), Tag/ABI riding on the `Based on:` continuation line rather than
    having a key of its own, and a `Sdcard:` key the parser must ignore.
    """
    meta = device_list.parse_avdmanager_avds(recorded.text("avdmanager_list_avd"))
    assert set(meta.keys()) == {"Pixel_9"}

    entry = meta["Pixel_9"]
    assert entry["device"] == "pixel_9 (Google)"
    assert entry["target"] == "16 KB Page Size (Google Inc.)"
    assert entry["based_on"] == 'Android 15.0 ("VanillaIceCream")'
    assert entry["abi"] == "page_size_16kb/arm64-v8a"


def test_parse_avdmanager_avds_empty():
    assert device_list.parse_avdmanager_avds("") == {}
    assert device_list.parse_avdmanager_avds("Available Android Virtual Devices:\n") == {}


# ---------------------------------------------------------------------------
# merge_avds
# ---------------------------------------------------------------------------
def test_merge_avds_enriches_matching_names(recorded):
    # A second name appended to the recorded listing: avdmanager knows nothing
    # about it, which is the case under test (it must survive the merge).
    names = device_list.parse_emulator_avds(recorded.text("emulator_list_avds") + "Ghost_AVD\n")
    meta = device_list.parse_avdmanager_avds(recorded.text("avdmanager_list_avd"))
    merged = device_list.merge_avds(names, meta)

    by_name = {a["name"]: a for a in merged}
    # Names from emulator are the source of truth -> both kept.
    assert set(by_name.keys()) == {"Pixel_9", "Ghost_AVD"}
    # Matching AVD enriched.
    assert by_name["Pixel_9"]["abi"] == "page_size_16kb/arm64-v8a"
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


def _fake_emulator_on_path(monkeypatch):
    """Pin emulator resolution to the bare name so argv assertions stay stable."""
    monkeypatch.setattr(device_list, "get_emulator_path", lambda: "emulator")


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


def _recorded_run(recorded, monkeypatch):
    """Serve all three tools their own recorded output."""
    _fake_emulator_on_path(monkeypatch)
    monkeypatch.setattr(
        device_list.subprocess,
        "run",
        _fake_run_factory(
            recorded.text("adb_devices_multiple"),
            recorded.text("emulator_list_avds"),
            recorded.text("avdmanager_list_avd"),
        ),
    )


def test_collect_aggregates_counts(monkeypatch, recorded):
    _recorded_run(recorded, monkeypatch)

    data = device_list.DeviceLister().collect()
    summary = data["summary"]

    assert summary["online"] == 2  # emulator-5554 + emulator-5556
    assert summary["offline"] == 0
    assert summary["avds"] == 1
    assert summary["total"] == 2 + 1
    # AVDs enriched from avdmanager.
    avd = next(a for a in data["avds"] if a["name"] == "Pixel_9")
    assert avd["abi"] == "page_size_16kb/arm64-v8a"


def test_collect_filter_narrows_avds(monkeypatch, recorded):
    _recorded_run(recorded, monkeypatch)

    data = device_list.DeviceLister(name_filter="Pixel_9").collect()
    assert [a["name"] for a in data["avds"]] == ["Pixel_9"]
    assert data["devices"] == []


def test_collect_filter_narrows_devices(monkeypatch, recorded):
    _recorded_run(recorded, monkeypatch)

    data = device_list.DeviceLister(name_filter="5556").collect()
    assert [d["serial"] for d in data["devices"]] == ["emulator-5556"]
    assert data["avds"] == []


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


# ---------------------------------------------------------------------------
# Emulator resolution (SDK-root-on-PATH regression)
# ---------------------------------------------------------------------------
def test_collect_survives_permission_error_from_a_directory_argv0(monkeypatch):
    """An unresolvable `emulator` must degrade to "no AVDs", not traceback.

    With the SDK root on PATH the bare name `emulator` resolves to the
    <sdk>/emulator *directory* and execve raises PermissionError, which is an
    OSError but not a FileNotFoundError.
    """

    def boom(_cmd, **_kwargs):
        raise PermissionError(13, "Permission denied", "emulator")

    monkeypatch.setattr(device_list.subprocess, "run", boom)

    data = device_list.DeviceLister().collect()
    assert data["devices"] == []
    assert data["avds"] == []


def test_get_avds_skips_emulator_when_binary_is_unresolvable(monkeypatch):
    """No resolved emulator -> no exec attempt at all."""
    monkeypatch.setattr(device_list, "get_emulator_path", lambda: None)

    attempted: list[list[str]] = []

    def record(cmd, **_kwargs):
        attempted.append(cmd)
        return _FakeResult("", returncode=1)

    monkeypatch.setattr(device_list.subprocess, "run", record)

    assert device_list.DeviceLister().get_avds() == []
    assert all(cmd[0] != "emulator" for cmd in attempted)


def test_get_avds_uses_the_resolved_emulator_path(monkeypatch, recorded):
    """The resolved absolute path is what gets exec'd, not the bare name."""
    monkeypatch.setattr(device_list, "get_emulator_path", lambda: "/opt/sdk/emulator/emulator")

    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        if cmd[0] == "/opt/sdk/emulator/emulator":
            return _FakeResult(recorded.text("emulator_list_avds"))
        return _FakeResult("", returncode=1)

    monkeypatch.setattr(device_list.subprocess, "run", fake_run)

    avds = device_list.DeviceLister().get_avds()

    expected = [ln.strip() for ln in recorded.lines("emulator_list_avds") if ln.strip()]
    assert [a["name"] for a in avds] == expected
    assert ["/opt/sdk/emulator/emulator", "-list-avds"] in seen
