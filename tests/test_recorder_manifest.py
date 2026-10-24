"""The recorder's MANIFEST bookkeeping, serial redaction and ANR window. No device needed.

Four things are held here:

- Every device profile's MANIFEST says, for each fixture, the description and
  command the recorder says today. Four entries had drifted (the recorder's
  text was edited after the recording and nobody re-recorded): a manifest is
  provenance, and provenance that disagrees with the tool that wrote it is
  wrong about one of them.
- ``--only`` leaves the profile-level ``recorded_at`` as it was and stamps the
  entries it records. It used to re-date the whole profile, so a one-fixture
  re-record claimed every fixture was captured that day.
- A serial that is not ``emulator-NNNN`` never reaches a committed
  ``adb devices`` fixture.
- The ANR recorders' window starts at ActivityManager's `Broadcasting:` record
  where there is one (API 35), and at the recorder's own mark where there is
  not (API 33, V9-A); the input ANR's key event cannot hang the run (V9-I).
"""

from __future__ import annotations

import json

import pytest
import record_fixtures as rf
from conftest import RECORDED_ROOT, RecordedFixtures
from device_list import parse_adb_devices

PHONE_SHAPED = "98ZZ1FFBA00000"


def _device_profiles() -> list[str]:
    names = []
    for path in sorted(RECORDED_ROOT.iterdir()):
        manifest = path / "MANIFEST.json"
        if manifest.exists() and "device" in json.loads(manifest.read_text(encoding="utf-8")):
            names.append(path.name)
    return names


# --- drift --------------------------------------------------------------------


@pytest.mark.parametrize("profile", _device_profiles())
def test_every_manifest_entry_says_what_the_recorder_says(profile):
    manifest = json.loads((RECORDED_ROOT / profile / "MANIFEST.json").read_text(encoding="utf-8"))
    drifted = []
    for entry in manifest["fixtures"]:
        try:
            command, description = rf.recorder_text(entry["name"])
        except KeyError:
            drifted.append(f"{entry['name']}: the recorder has no such fixture")
            continue
        if entry["description"] != description:
            drifted.append(f"{entry['name']}: description")
        if entry["command"] != command:
            drifted.append(f"{entry['name']}: command")
    assert not drifted, (
        f"{profile}/MANIFEST.json disagrees with tests/record_fixtures.py: {drifted}. "
        f"Re-record those fixtures, or bring the manifest text in line by hand."
    )


def test_the_device_profiles_include_both_emulators():
    """Guard the guard: a profile list that came back empty would check nothing."""
    assert {"emulator-api35", "emulator-api33"} <= set(_device_profiles())


# --- recorded_at --------------------------------------------------------------

OLD = "2000-01-01T00:00:00Z"
NEW = "2026-10-24T00:00:00Z"


def _manifest(stamp: str, names: list[str], device: dict, on: str = "host-a") -> dict:
    return {
        "profile": "p",
        "recorded_at": stamp,
        "recorded_on": on,
        "device": device,
        "fixtures": [{"name": name, "description": name} for name in names],
    }


def test_a_partial_run_keeps_the_profile_s_date_and_dates_its_own_entries():
    device = {"model": "m", "api_level": "33", "build_fingerprint": "f1"}
    previous = _manifest(OLD, ["kept", "redone"], device)
    previous["fixtures"][0]["recorded_at"] = OLD
    merged = rf._merge_manifest(_manifest(NEW, ["redone"], device), previous, partial=True)

    assert merged["recorded_at"] == OLD
    by_name = {entry["name"]: entry for entry in merged["fixtures"]}
    assert by_name["kept"] == previous["fixtures"][0]
    assert by_name["redone"]["recorded_at"] == NEW
    assert "device" not in by_name["redone"]
    assert "recorded_on" not in by_name["redone"]
    assert [entry["name"] for entry in merged["fixtures"]] == ["kept", "redone"]


def test_a_partial_run_on_a_changed_build_says_so_on_its_entries():
    before = {"model": "m", "api_level": "33", "build_fingerprint": "f1"}
    after = {**before, "build_fingerprint": "f2"}
    merged = rf._merge_manifest(
        _manifest(NEW, ["redone"], after, on="host-b"),
        _manifest(OLD, ["kept"], before),
        partial=True,
    )
    assert merged["device"] == before
    assert merged["recorded_on"] == "host-a"
    redone = next(entry for entry in merged["fixtures"] if entry["name"] == "redone")
    assert redone["device"] == after
    assert redone["recorded_on"] == "host-b"


def test_a_full_run_replaces_the_manifest():
    device = {"model": "m", "api_level": "33"}
    merged = rf._merge_manifest(
        _manifest(NEW, ["a"], device), _manifest(OLD, ["a", "gone"], device), partial=False
    )
    assert merged["recorded_at"] == NEW
    assert [entry["name"] for entry in merged["fixtures"]] == ["a"]
    assert merged["fixtures"][0]["recorded_at"] == NEW


def test_a_first_partial_run_dates_the_new_profile():
    merged = rf._merge_manifest(_manifest(NEW, ["a"], {}), None, partial=True)
    assert merged["recorded_at"] == NEW
    assert merged["fixtures"][0]["recorded_at"] == NEW


def test_an_only_run_end_to_end_leaves_the_profile_date(monkeypatch, tmp_path):
    """record() itself, its adb calls answered from the api33 recordings."""
    api33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")
    device = api33.manifest["device"]
    props = {
        "ro.product.model": device["model"],
        "ro.build.version.sdk": device["api_level"],
        "ro.build.version.release": device["android_release"],
        "ro.product.cpu.abi": device["abi"],
        "ro.build.fingerprint": device["build_fingerprint"],
    }
    help_text = api33.text("cmd_statusbar_help")
    fixture = next(f for f in rf.FIXTURES if f.name == "cmd_statusbar_help")

    def fake_run(args, _serial, *_timeout, **_keywords):
        if args[:2] == ["shell", "getprop"]:
            return rf.Capture(props[args[2]] + "\n", "", 0)
        assert args == fixture.args, args
        return rf.Capture(help_text, "", 1)

    monkeypatch.setattr(rf, "_run", fake_run)
    monkeypatch.setattr(rf, "RECORDED_ROOT", tmp_path)
    profile = tmp_path / "p"
    profile.mkdir()
    untouched = {"name": "other", "file": "other.txt", "description": "x", "command": "y"}
    (profile / "other.txt").write_text("x", encoding="utf-8")
    (profile / "MANIFEST.json").write_text(
        json.dumps({**_manifest(OLD, [], device), "fixtures": [untouched]}), encoding="utf-8"
    )

    assert rf.record("emulator-5556", {"cmd_statusbar_help"}, "p") == 0

    written = json.loads((profile / "MANIFEST.json").read_text(encoding="utf-8"))
    assert written["recorded_at"] == OLD
    by_name = {entry["name"]: entry for entry in written["fixtures"]}
    assert by_name["other"] == untouched
    assert by_name["cmd_statusbar_help"]["recorded_at"] != OLD
    assert (profile / "cmd_statusbar_help.txt").read_text(encoding="utf-8") == help_text


# --- serials ------------------------------------------------------------------


def _with_a_phone(listing: str) -> str:
    """A recorded listing with its first emulator's serial swapped for a phone-shaped one."""
    first = parse_adb_devices(listing)[0]["serial"]
    assert first.startswith("emulator-")
    return listing.replace(first, PHONE_SHAPED, 1)


def test_a_phone_s_serial_is_redacted_and_its_row_kept():
    listing = RecordedFixtures(RECORDED_ROOT / "emulator-api33").text("adb_devices_offline")
    redacted = rf._redact_serials(_with_a_phone(listing))

    assert PHONE_SHAPED not in redacted
    before = parse_adb_devices(_with_a_phone(listing))
    after = parse_adb_devices(redacted)
    assert [d["serial"] for d in after] == [
        rf.SERIAL_REDACTED if d["serial"] == PHONE_SHAPED else d["serial"] for d in before
    ]
    assert [d["state"] for d in after] == [d["state"] for d in before]
    assert "offline" in [d["state"] for d in after]


def test_emulator_serials_and_the_header_are_left_alone():
    listing = RecordedFixtures(RECORDED_ROOT / "emulator-api33").text("adb_devices_offline")
    assert rf._redact_serials(listing) == listing


@pytest.mark.parametrize("name", ["adb_devices_single", "adb_devices_multiple"])
def test_the_device_listings_redact_serials(name):
    fixture = next(f for f in rf.FIXTURES if f.name == name)
    listing = RecordedFixtures(RECORDED_ROOT / "emulator-api35").text("adb_devices_multiple")
    if name == "adb_devices_single":
        with pytest.raises(rf.CaptureError, match="exactly one"):
            fixture.post(_with_a_phone(listing))
        return
    assert PHONE_SHAPED not in fixture.post(_with_a_phone(listing))


def test_a_single_device_listing_must_be_one_device():
    api35 = RecordedFixtures(RECORDED_ROOT / "emulator-api35")
    single = api35.text("adb_devices_single")
    assert rf._exactly_one_device(single) == single
    with pytest.raises(rf.CaptureError, match="lists 2 devices"):
        rf._exactly_one_device(api35.text("adb_devices_multiple"))


# --- ANR window and key event ---------------------------------------------------

BROADCAST_MARK = "Broadcasting: Intent { act=com.example.composefixture.PROVOKE_ANR"


def test_the_window_starts_at_the_broadcast_record_where_one_is_logged():
    """API 35's recording begins with that record, so the window is all of it."""
    data = RecordedFixtures(RECORDED_ROOT / "emulator-api35").data(
        "logcat_anr_broadcast_records.bin"
    )
    records = rf._split_records(data)
    extra: dict = {}
    assert rf._anr_window(records, BROADCAST_MARK, extra) == records
    assert "Broadcasting" in extra["window_anchor"]


def test_without_a_broadcast_record_or_a_mark_the_window_is_refused():
    """API 33's recording holds neither: its mark was filtered out after it was used."""
    data = RecordedFixtures(RECORDED_ROOT / "emulator-api33").data(
        "logcat_anr_broadcast_records.bin"
    )
    with pytest.raises(rf.CaptureError, match="no window mark"):
        rf._anr_window(rf._split_records(data), BROADCAST_MARK, {})


def test_every_anr_provocation_logs_the_window_mark_first():
    for name in ("logcat_anr_broadcast_records", "logcat_anr_input_records"):
        fixture = next(f for f in rf.FIXTURES if f.name == name)
        provoke = next(i for i, step in enumerate(fixture.setup) if "broadcast" in step)
        assert fixture.setup[provoke - 1] == rf._LOG_WINDOW_MARK, name


class _Hanging:
    """A child process that never exits until killed, like `input` into a stalled window."""

    killed = False

    def __init__(self, argv, **_kwargs):
        self.argv = argv

    def wait(self, timeout=None):
        if not _Hanging.killed:
            raise rf.subprocess.TimeoutExpired(self.argv, timeout)
        return -9

    def kill(self):
        _Hanging.killed = True


def test_a_key_event_that_blocks_is_killed_not_waited_for(monkeypatch):
    # `input`'s usage is not recorded; an empty answer stands for one that lists no --async.
    monkeypatch.setattr(rf, "_run", lambda *_a, **_k: rf.Capture("", "", 0))
    monkeypatch.setattr(rf.subprocess, "Popen", _Hanging)
    _Hanging.killed = False
    extra: dict = {}
    rf._send_key_bounded("emulator-5556", extra)
    assert _Hanging.killed
    assert extra["key_event"]["killed"] is True
    assert "--async" not in extra["key_event"]["argv"]
