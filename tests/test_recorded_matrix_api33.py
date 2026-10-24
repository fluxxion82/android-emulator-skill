"""The parsers against a second API level: the emulator-api33 profile.

Every other recorded-fixture test reads emulator-api35 (``recorded``) or
whichever profiles happen to hold a fixture (``any_profile``, which SKIPS a
profile that lacks it). Neither proves a parser works on Android 13: a
fixture missing from this profile would pass silently. So this file names the
profile outright, and every fixture it needs is required -- a missing one
fails, through ``RecordedFixtures``.

Each assertion is on the parser's USEFUL output -- a record whose text says
``ANR in``, a uid set with something in it -- so a parser answering ``{}`` or
``[]`` cannot pass.

Recorded with ``python tests/record_fixtures.py --profile emulator-api33
--serial <api 33 emulator> --only <name>``.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from device_list import parse_adb_devices

from common import anr_pipeline
from common.device_utils import parse_package_permissions, parse_package_uids, parse_ps_uids
from common.logcat import parse_records

PROFILE = "emulator-api33"
API33 = RecordedFixtures(RECORDED_ROOT / PROFILE)
PACKAGE = "com.example.composefixture"


def _entry(name: str) -> dict:
    """``name``'s MANIFEST entry: the provenance measured in its own run."""
    for entry in API33.manifest["fixtures"]:
        if entry["name"] == name:
            return entry
    pytest.fail(f"{PROFILE}/MANIFEST.json has no entry for {name}")


def _app_uids() -> frozenset[int]:
    uids = parse_package_uids(API33.text("package_list_uid"), PACKAGE)
    assert uids, "package_list_uid names no uid for the fixture app"
    return uids


# --- the profile itself ------------------------------------------------------


def test_the_profile_is_an_api_33_device():
    device = API33.manifest["device"]
    assert device["api_level"] == "33"
    assert device["model"]


def test_manifest_matches_files_on_disk():
    listed = {entry["file"] for entry in API33.manifest["fixtures"]}
    root = RECORDED_ROOT / PROFILE
    on_disk = {p.name for p in root.iterdir() if p.name != "MANIFEST.json"}
    assert listed == on_disk


# --- ANR records -------------------------------------------------------------
#
# Not recorded on API 33. Both pinned defects are in the RECORDER, not a
# parser, and each test fails on the missing file until the recording exists.

_ANR_MARKER_ABSENT = (
    "V9-A: the ANR recorders start their window at ActivityManager's `Broadcasting: Intent "
    "{ act=...PROVOKE_ANR` record, which API 33 does not log (the ANR itself happens: "
    "'ANR in com.example.composefixture' is in the buffer), so they refuse with 'no "
    "provoking broadcast in the buffer'"
)
_INPUT_ASYNC_ABSENT = (
    "V9-I: logcat_anr_input_records refuses on API 33 with 'timed out after 30s' in a setup "
    "step. The likely one is `input keyevent --async`: API 33's `input` usage lists no "
    "--async (it still exits 0 on an idle device), so the key is presumably injected "
    "synchronously into the blocked window"
)


@pytest.mark.parametrize(
    "file",
    [
        pytest.param(
            "logcat_anr_broadcast_records.bin",
            marks=pytest.mark.xfail(strict=True, reason=_ANR_MARKER_ABSENT),
        ),
        pytest.param(
            "logcat_anr_input_records.bin",
            marks=pytest.mark.xfail(strict=True, reason=_INPUT_ASYNC_ABSENT),
        ),
        pytest.param(
            "logcat_anr_foreign_process_records.bin",
            marks=pytest.mark.xfail(strict=True, reason=_ANR_MARKER_ABSENT),
        ),
    ],
)
def test_anr_records_parse_to_an_anr_report(file):
    records = parse_records(API33.data(file))
    assert [r for r in records if "ANR in" in r.message]


# --- StrictMode --------------------------------------------------------------


def _strictmode_records():
    records = [
        r
        for r in parse_records(API33.data("logcat_strictmode_records.bin"))
        if r.tag == "StrictMode"
    ]
    assert records, "logcat_strictmode_records holds no StrictMode record"
    return records


def test_strictmode_records_parse():
    assert any("StrictMode policy violation" in r.message for r in _strictmode_records())


def test_a_strictmode_record_carries_the_app_s_uid_and_pid():
    """logd stamps the WRITER's credentials: the app's own pid and uid.

    So a records reader can attribute a violation to the package by uid, held
    against the uid listing measured in the same run.
    """
    entry = _entry("logcat_strictmode_records")
    listed = parse_package_uids(entry["package_uid_listing"]["output"], PACKAGE)
    assert listed, "the run's own uid listing names no uid"
    for record in _strictmode_records():
        assert record.uid in listed
        assert str(record.pid) in entry["pids"]


def test_a_strictmode_record_is_the_whole_violation():
    """One record per violation, its stack trace inside the message."""
    for record in _strictmode_records():
        assert record.message.startswith("StrictMode policy violation")
        assert "\n\tat " in record.message


def test_the_strictmode_text_is_one_event_per_violation():
    text = API33.text("logcat_strictmode_violation")
    headers = text.count("StrictMode policy violation")
    events = [anr_pipeline.parse_logcat_anr(line) for line in text.splitlines()]
    assert headers >= 1
    assert len([e for e in events if e]) == headers


# --- Choreographer, and ordinary traffic --------------------------------------


def test_the_choreographer_line_parses_as_jank():
    """On API 33 only the cold start's jank lands in the window.

    The 40 s AnrReceiver block that gives emulator-api35 its 'Skipped 2401
    frames' line leaves no Skipped line here; the grep also catches a Looper
    line that merely names Choreographer, which must not parse as jank.
    """
    events = [
        anr_pipeline.parse_logcat_anr(line)
        for line in API33.lines("logcat_choreographer_jank")
        if "Skipped" in line
    ]
    assert events and all(e and e["kind"] == "jank" and e["frames"] > 0 for e in events)
    others = [line for line in API33.lines("logcat_choreographer_jank") if "Skipped" not in line]
    assert [line for line in others if anr_pipeline.parse_logcat_anr(line)] == []


def test_ordinary_traffic_is_not_read_as_a_stall():
    lines = [
        line
        for line in API33.lines("logcat_threadtime")
        if not any(token in line for token in ("Skipped", "ANR in", "StrictMode"))
    ]
    assert len(lines) > 100
    assert [line for line in lines if anr_pipeline.parse_logcat_anr(line)] == []


# --- uids: ps and the package table -------------------------------------------


def test_ps_all_maps_system_server_to_uid_1000():
    uids = parse_ps_uids(API33.text("ps_all_pid_uid"))
    assert len(uids) > 50, "a whole process table parsed to almost nothing"
    assert uids[_entry("ps_all_pid_uid")["system_server_pid"]] == 1000


def test_ps_pid_uid_names_the_app_and_system_server():
    pids = _entry("ps_pid_uid")["pids"]
    uids = parse_ps_uids(API33.text("ps_pid_uid"))
    assert uids[pids["system_server"]] == 1000
    assert uids[pids["app"]] in _app_uids()


def test_the_whole_package_table_names_the_app_s_uid():
    whole = parse_package_uids(API33.text("package_list_uid_all"), PACKAGE)
    assert whole
    assert whole == _app_uids()


def test_the_whole_package_table_shows_a_shared_uid():
    """Several packages on one uid: what sharedUserId detection reads."""
    table = API33.text("package_list_uid_all")
    names = re.findall(r"^package:(\S+) uid:", table, flags=re.MULTILINE)
    on_system = [n for n in names if parse_package_uids(table, n) == frozenset({1000})]
    assert {"android", "com.android.localtransport"} <= set(on_system)


def test_two_users_list_one_uid_per_user():
    """ONE row, uids comma-separated, as on API 35: user N's uid is N * 100000 + app id."""
    uids = parse_package_uids(API33.text("package_list_uid_two_users"), PACKAGE)
    (app_uid,) = _app_uids()
    user = int(_entry("package_list_uid_two_users")["secondary_user"])
    assert uids == frozenset({app_uid, user * 100_000 + app_uid})


# --- dumpsys package with two users --------------------------------------------


def test_the_two_user_dump_has_a_block_per_user():
    user = _entry("dumpsys_package_two_users")["secondary_user"]
    text = API33.text("dumpsys_package_two_users")
    assert re.search(r"^    User 0: ", text, flags=re.MULTILINE)
    assert re.search(rf"^    User {user}: ", text, flags=re.MULTILINE)


def test_the_two_user_dump_parses_to_the_app_s_permissions():
    parsed = parse_package_permissions(API33.text("dumpsys_package_two_users"))
    assert parsed["found"]
    assert parsed["install"], "the app's install-time permission was not read"
    assert all(entry["granted"] for entry in parsed["install"])
    # The app requests no runtime permission, for either user.
    assert parsed["runtime"] == []


def test_the_shared_uid_dump_reads_user_0_of_two():
    """Runtime state of a shared-uid package, one block per user under `Shared users:`.

    The parser keeps the FIRST runtime section, documented as user 0's. The
    two users' entries differ in their flags here (user 0's carry
    USER_SENSITIVE_WHEN_GRANTED, the new user's do not), which is what shows
    the parser read user 0's block and not the other.
    """
    text = API33.text("dumpsys_package_shared_uid_two_users")
    user = _entry("dumpsys_package_shared_uid_two_users")["secondary_user"]
    shared = text[text.index("\nShared users:") :]
    first, second = shared.split(f"\n    User {user}: ", 1)
    parsed = parse_package_permissions(text)

    assert parsed["runtime"], "no runtime permissions read for a shared-uid package"
    user0_names = re.findall(r"^        (\S+): granted=", first, flags=re.MULTILINE)
    assert [e["permission"] for e in parsed["runtime"]] == user0_names
    sensitive = [e for e in parsed["runtime"] if "USER_SENSITIVE_WHEN_GRANTED" in e["flags"]]
    assert sensitive, "user 0's flags were not the ones read"
    assert "USER_SENSITIVE_WHEN_GRANTED" not in second.split("\n\n", 1)[0]


def test_the_shared_uid_dump_without_a_second_user_reads_runtime_permissions():
    parsed = parse_package_permissions(API33.text("dumpsys_package_shared_uid"))
    assert parsed["found"]
    assert parsed["runtime"]


def test_a_grant_for_another_user_of_an_unrequested_permission_throws():
    """API 33, user N: SecurityException and exit 255 -- not the silence of user 0 on API 35.

    emulator-api35's pm_grant_not_requested (user 0, the same permission) is
    no output and exit 0; here the same request for a secondary user fails
    loudly. Which of the two differences -- API level or user -- decides it is
    not separable from these two recordings.
    """
    text = API33.text("pm_grant_user")
    assert "SecurityException" in text
    assert "has not requested permission" in text
    assert text.rstrip().endswith("exit=255")


# --- adb devices -------------------------------------------------------------


def test_an_offline_device_parses_as_offline():
    devices = parse_adb_devices(API33.text("adb_devices_offline"))
    offline = [d for d in devices if d["state"] == "offline"]
    target = _entry("adb_devices_offline")["target_serial"]
    assert [d["serial"] for d in offline] == [target]
    assert offline[0]["online"] is False
    assert offline[0]["type"] == "emulator"


def test_the_offline_listing_publishes_no_device_serial():
    serials = [d["serial"] for d in parse_adb_devices(API33.text("adb_devices_offline"))]
    assert all(re.fullmatch(r"emulator-\d+|SERIALREDACTED", s) for s in serials), serials


def test_the_manifest_is_json_with_one_entry_per_name():
    data = json.loads((RECORDED_ROOT / PROFILE / "MANIFEST.json").read_text(encoding="utf-8"))
    names = [entry["name"] for entry in data["fixtures"]]
    assert len(names) == len(set(names))
