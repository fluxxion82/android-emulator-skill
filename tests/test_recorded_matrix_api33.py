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
from common.device_utils import (
    UserNotInstalledError,
    UserSectionMissingError,
    parse_package_permissions,
    parse_package_uids,
    parse_ps_uids,
    parse_user_ids,
    permission_state,
)
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
# API 33 logs no `Broadcasting: Intent` record (V9-A) and its `input` has no
# --async (V9-I), so these windows start at the recorder's own mark and the key
# event's adb call is bounded. What remains is API 35's shape, record for record.

ANR_FILES = {
    "logcat_anr_broadcast_records": "logcat_anr_broadcast_records.bin",
    "logcat_anr_input_records": "logcat_anr_input_records.bin",
    "logcat_anr_foreign_process_records": "logcat_anr_foreign_process_records.bin",
}


@pytest.mark.parametrize("name", sorted(ANR_FILES))
def test_an_anr_report_is_a_system_server_record(name):
    """`ANR in` from system_server: its pid (pidof in the same run) and uid 1000."""
    system_server = int(_entry(name)["system_server_pid"])
    reports = [r for r in parse_records(API33.data(ANR_FILES[name])) if "ANR in" in r.message]
    assert reports
    assert all(r.pid == system_server and r.uid == 1000 for r in reports)
    assert any(r.tag == "ActivityManager" and "\nPID: " in r.message for r in reports)


@pytest.mark.parametrize("name", sorted(ANR_FILES))
def test_api_33_has_all_of_api_35_s_anr_shape_but_the_broadcast_record(name):
    entry = _entry(name)
    assert entry["expected_absent"] == []
    assert "aes-recorder" in entry["window_anchor"]


def _events(file: str) -> list[tuple]:
    """``(record, event)`` for every record anr_watcher's classifier makes an event of."""
    pairs = []
    for record in parse_records(API33.data(file)):
        event = anr_pipeline.event_from_record(record)
        if event is not None:
            pairs.append((record, event))
    return pairs


def _activity_manager_anr(name: str) -> dict:
    """The one `anr` event ActivityManager's report classifies as, held to its writer."""
    system_server = int(_entry(name)["system_server_pid"])
    reports = [
        event
        for record, event in _events(ANR_FILES[name])
        if record.tag == "ActivityManager" and event["kind"] == "anr"
    ]
    assert len(reports) == 1, f"{name}: {len(reports)} ActivityManager ANR events"
    assert reports[0]["pid"] == system_server
    return reports[0]


@pytest.mark.parametrize(
    ("name", "package", "reason"),
    [
        (
            "logcat_anr_broadcast_records",
            PACKAGE,
            f"Broadcast of Intent {{ act={PACKAGE}.PROVOKE_ANR ",
        ),
        ("logcat_anr_input_records", PACKAGE, "Input dispatching timed out ("),
        (
            "logcat_anr_foreign_process_records",
            "com.example.victim",
            f"Broadcast of Intent {{ act={PACKAGE}.PROVOKE_FOREIGN_PROCESS_ANR ",
        ),
    ],
)
def test_the_anr_report_classifies_with_package_pid_and_reason(name, package, reason):
    """What anr_watcher's classifier (event_from_record) reads off the report record."""
    event = _activity_manager_anr(name)
    assert event["anr_package"] == package
    assert event["reason"].startswith(reason)
    assert event["app_pid"] not in (None, event["pid"])


def test_the_foreign_process_anr_names_the_stalled_pid():
    event = _activity_manager_anr("logcat_anr_foreign_process_records")
    assert event["app_pid"] == int(_entry("logcat_anr_foreign_process_records")["stalled_pid"])


def test_the_input_anr_s_activity_manager_report_names_the_component():
    event = _activity_manager_anr("logcat_anr_input_records")
    assert event["component"] == f"{PACKAGE}/.DefaultActivity"


@pytest.mark.parametrize("name", ["logcat_anr_input_records", "logcat_anr_broadcast_records"])
def test_window_manager_s_report_classifies_as_a_window_anr(name):
    """An `anr` of a window: its title kept as `window`, no package read from it."""
    windows = [event for record, event in _events(ANR_FILES[name]) if record.tag == "WindowManager"]
    assert len(windows) == 1
    assert windows[0]["kind"] == "anr"
    assert windows[0]["window"] == f"{PACKAGE}/{PACKAGE}.DefaultActivity"
    assert "anr_package" not in windows[0]


def test_the_input_anr_is_reported_by_window_manager_and_activity_manager():
    records = parse_records(API33.data(ANR_FILES["logcat_anr_input_records"]))
    assert any(r.tag == "WindowManager" and r.message.startswith("ANR in Window{") for r in records)
    assert any(
        r.tag == "ActivityManager"
        and r.message.startswith(f"ANR in {PACKAGE} (")
        and "\nReason: Input dispatching timed out" in r.message
        for r in records
    )


def test_the_input_anr_key_event_was_bounded_not_async():
    key = _entry("logcat_anr_input_records")["key_event"]
    assert key["input_lists_async"] is False
    assert "--async" not in key["argv"]


def test_the_foreign_process_anr_carries_its_start_proc_record():
    entry = _entry("logcat_anr_foreign_process_records")
    system_server, pid = int(entry["system_server_pid"]), entry["stalled_pid"]
    records = parse_records(API33.data(ANR_FILES["logcat_anr_foreign_process_records"]))
    starts = [r for r in records if r.message.startswith(f"Start proc {pid}:com.example.victim/")]
    assert starts and all(r.pid == system_server and r.uid == 1000 for r in starts)
    assert any(r.message.startswith(f"ANR in com.example.victim\nPID: {pid}\n") for r in records)


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


def test_each_strictmode_record_classifies_as_one_strictmode_event():
    """event_from_record: one `strictmode` event per violation, the app's pid, its own cost."""
    entry = _entry("logcat_strictmode_records")
    events = [event for _record, event in _events("logcat_strictmode_records.bin")]
    assert len(events) == len(_strictmode_records())
    for event in events:
        assert event["kind"] == "strictmode"
        assert str(event["pid"]) in entry["pids"]
        stated = re.search(r"~duration=(\d+) ms", event["message"])
        assert stated and event["duration_ms"] == float(stated.group(1))


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


CAMERA = "android.permission.CAMERA"


def _user_blocks(text: str) -> dict[str, str]:
    """``{user id: that user's block}`` from the package's ``    User N: `` blocks."""
    parts = re.split(r"^    User (\d+): ", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_camera_is_denied_for_user_0_and_granted_for_the_other_user():
    """`pm grant --user <id>` changed that user's runtime state and no other's."""
    user = _entry("dumpsys_package_two_users")["secondary_user"]
    blocks = _user_blocks(API33.text("dumpsys_package_two_users"))
    assert re.search(rf"^        {CAMERA}: granted=false\b", blocks["0"], flags=re.MULTILINE)
    assert re.search(rf"^        {CAMERA}: granted=true\b", blocks[user], flags=re.MULTILINE)


def test_the_two_user_dump_parses_to_user_0_s_permissions():
    """By default the parser reads `User 0:`'s runtime section, so CAMERA reads denied.

    The grant for the other user is invisible to it unless that user is asked
    for (test_the_secondary_user_s_runtime_state_is_read_from_its_own_section).
    """
    parsed = parse_package_permissions(API33.text("dumpsys_package_two_users"))
    assert parsed["found"]
    assert parsed["install"], "the app's install-time permission was not read"
    assert all(entry["granted"] for entry in parsed["install"])
    assert [(e["permission"], e["granted"]) for e in parsed["runtime"]] == [(CAMERA, False)]


def test_a_per_user_grant_prints_nothing_and_exits_zero():
    assert API33.text("pm_grant_user").strip() == "exit=0"
    assert _entry("pm_grant_user")["permission"] == CAMERA


READ_PHONE_NUMBERS = "android.permission.READ_PHONE_NUMBERS"


def _shared_users_blocks() -> dict[str, str]:
    """The `User N:` blocks of the `SharedUser [android.uid.shared]` section."""
    text = API33.text("dumpsys_package_shared_uid_two_users")
    shared = text.partition("\nShared users:")[2]
    assert "SharedUser [android.uid.shared]" in shared
    return _user_blocks(shared)


def test_a_shared_uid_grant_differs_between_users():
    """`pm grant --user <id>` on a shared uid: denied for user 0, granted for the other user.

    Under `Shared users:`, not under the package -- the shared-uid layout. Only
    the granted permission differs; the SYSTEM_FIXED ones read the same.
    """
    entry = _entry("dumpsys_package_shared_uid_two_users")
    user = entry["secondary_user"]
    assert entry["permission"] == READ_PHONE_NUMBERS
    assert entry["grant"]["output"].strip() == "exit=0"
    blocks = _shared_users_blocks()
    assert set(blocks) == {"0", user}
    states = {
        u: dict(re.findall(r"^        (\S+): granted=(\w+)", block, flags=re.MULTILINE))
        for u, block in blocks.items()
    }
    assert states["0"][READ_PHONE_NUMBERS] == "false"
    assert states[user][READ_PHONE_NUMBERS] == "true"
    differing = {p for p in states["0"] if states["0"][p] != states[user].get(p)}
    assert differing == {READ_PHONE_NUMBERS}


def test_the_shared_uid_dump_parses_to_user_0_s_runtime_state():
    """By default the parser reads `User 0:`: the grant for the other user is unseen."""
    parsed = parse_package_permissions(API33.text("dumpsys_package_shared_uid_two_users"))
    user0 = dict(
        re.findall(r"^        (\S+): granted=(\w+)", _shared_users_blocks()["0"], re.MULTILINE)
    )
    assert parsed["runtime"], "no runtime permissions read for a shared-uid package"
    assert {e["permission"]: str(e["granted"]).lower() for e in parsed["runtime"]} == user0
    assert user0[READ_PHONE_NUMBERS] == "false"


def test_the_shared_uid_dump_without_a_second_user_reads_runtime_permissions():
    parsed = parse_package_permissions(API33.text("dumpsys_package_shared_uid"))
    assert parsed["found"]
    assert parsed["runtime"]


# --- per-user selection (privacy_manager --user) -----------------------------


def _secondary(name: str) -> int:
    return int(_entry(name)["secondary_user"])


def test_the_secondary_user_s_runtime_state_is_read_from_its_own_section():
    """`--user <id>` reads that user's `User <id>:` block, where CAMERA is granted."""
    user = _secondary("dumpsys_package_two_users")
    parsed = parse_package_permissions(API33.text("dumpsys_package_two_users"), user=user)
    assert parsed["user"] == user
    assert parsed["user_found"] is True
    assert parsed["user_installed"] is True
    assert [(e["permission"], e["granted"]) for e in parsed["runtime"]] == [(CAMERA, True)]


def test_the_secondary_user_s_shared_uid_state_is_read_under_shared_users():
    """The shared-uid layout: the runtime section is the SharedUser block's `User <id>:`."""
    user = _secondary("dumpsys_package_shared_uid_two_users")
    parsed = parse_package_permissions(
        API33.text("dumpsys_package_shared_uid_two_users"), user=user
    )
    wanted = dict(
        re.findall(r"^        (\S+): granted=(\w+)", _shared_users_blocks()[str(user)], re.M)
    )
    assert parsed["user_found"] is True
    assert {e["permission"]: str(e["granted"]).lower() for e in parsed["runtime"]} == wanted
    assert wanted[READ_PHONE_NUMBERS] == "true"


def test_user_0_is_selected_by_its_label():
    """The default is `User 0:` by name, not "the first section".

    No recording shows a secondary user printed before user 0: in both two-user
    dumps the order is fixed, `User 0:` first. So this pins the label (the
    `user` key and the explicit call agreeing with the default) and the order
    the dump uses; the secondary-user tests above are what a first-section
    reader fails.
    """
    for name in ("dumpsys_package_two_users", "dumpsys_package_shared_uid_two_users"):
        text = API33.text(name)
        user = _secondary(name)
        assert text.index("    User 0: ") < text.index(f"    User {user}: "), name
        default = parse_package_permissions(text)
        assert default["user"] == 0
        assert default["user_found"] is True
        assert default == parse_package_permissions(text, user=0)


def test_a_user_with_no_section_reads_nothing_rather_than_user_0_s_state():
    """A single-user dump asked for user 11: not found, and no runtime state borrowed."""
    parsed = parse_package_permissions(API33.text("dumpsys_package_shared_uid"), user=11)
    assert parsed["found"] is True
    assert parsed["user_found"] is False
    assert parsed["runtime"] == []
    with pytest.raises(UserSectionMissingError):
        permission_state(API33.text("dumpsys_package_shared_uid"), CAMERA, user=11)
    assert permission_state(API33.text("dumpsys_package_shared_uid"), CAMERA) is True


def test_a_user_the_package_is_not_installed_for_reads_installed_false():
    """`User <id>: ... installed=false` -- yet CAMERA reads granted=true under it."""
    user = _secondary("dumpsys_package_user_not_installed")
    parsed = parse_package_permissions(API33.text("dumpsys_package_user_not_installed"), user=user)
    assert parsed["user_found"] is True
    assert parsed["user_installed"] is False
    assert [(e["permission"], e["granted"]) for e in parsed["runtime"]] == [(CAMERA, True)]
    assert parse_package_permissions(API33.text("dumpsys_package_user_not_installed"))[
        "user_installed"
    ]


def test_a_grant_under_an_installed_false_user_is_not_reported_as_held():
    """The shared read-back refuses that grant=true: the app does not exist for the user.

    One rule for every caller of ``permission_state``: privacy_manager refused this
    shape on its own, push_notification read it as a verified grant.
    """
    text = API33.text("dumpsys_package_user_not_installed")
    user = _secondary("dumpsys_package_user_not_installed")
    with pytest.raises(UserNotInstalledError) as refused:
        permission_state(text, CAMERA, user=user)
    assert refused.value.user == user
    assert f"pm install-existing --user {user} {PACKAGE}" in refused.value.remedy(PACKAGE)
    assert permission_state(text, CAMERA) is False


def test_a_revoked_permission_with_no_entry_left_parses_as_denied():
    """After grant+revoke for the secondary user its runtime section is empty; CAMERA is
    requested and listed as runtime under user 0, so for that user it is denied."""
    user = _secondary("dumpsys_package_user_revoked")
    text = API33.text("dumpsys_package_user_revoked")
    assert _user_blocks(text)[str(user)].count(CAMERA) == 0, "the recording changed"
    parsed = parse_package_permissions(text, user=user)
    assert [(e["permission"], e["granted"]) for e in parsed["runtime"]] == [(CAMERA, False)]
    assert CAMERA in parsed["denied"]
    assert permission_state(text, CAMERA, user=user) is False


def test_requested_permissions_no_user_lists_as_runtime_are_not_made_runtime():
    """deskclock (API 35) requests eight permissions the dump gives no state for at all;
    nothing marks them runtime, so they must not be reported as denied runtime ones."""
    text = RecordedFixtures(RECORDED_ROOT / "emulator-api35").text("dumpsys_package_permissions")
    parsed = parse_package_permissions(text)
    stated = {e["permission"] for e in parsed["install"] + parsed["runtime"]}
    unstated = [name for name in parsed["requested"] if name not in stated]
    assert "android.permission.QUERY_USERS" in unstated
    assert "android.permission.QUERY_USERS" not in parsed["denied"]


def _with_shared_user_renamed() -> str:
    """dumpsys_package_shared_uid_two_users, its `SharedUser [android.uid.shared]` header
    renamed to `SharedUser [android.uid.unrelated]` -- nothing else touched -- so the block
    no longer belongs to the package, whose `sharedUser=` still names android.uid.shared."""
    text = API33.text("dumpsys_package_shared_uid_two_users")
    old, new = "  SharedUser [android.uid.shared] (", "  SharedUser [android.uid.unrelated] ("
    assert text.count(old) == 1
    assert "sharedUser=SharedUserSetting{" in text and " android.uid.shared/" in text
    return text.replace(old, new)


def test_a_shared_user_block_the_package_is_not_in_supplies_no_state():
    derived = _with_shared_user_renamed()
    for user in (0, _secondary("dumpsys_package_shared_uid_two_users")):
        parsed = parse_package_permissions(derived, user=user)
        assert parsed["found"] and parsed["user_found"]
        assert parsed["runtime"] == [], f"user {user} read an unrelated shared user's state"
        assert permission_state(derived, READ_PHONE_NUMBERS, user=user) is None


def test_the_package_s_own_shared_user_block_still_supplies_state():
    parsed = parse_package_permissions(API33.text("dumpsys_package_shared_uid_two_users"))
    assert READ_PHONE_NUMBERS in {e["permission"] for e in parsed["runtime"]}
    assert parse_package_permissions(API33.text("dumpsys_package_shared_uid"))["runtime"]


def test_pm_list_users_parses_to_user_ids():
    assert parse_user_ids(API33.text("pm_list_users")) == frozenset({0})
    user = _secondary("pm_list_users_two_users")
    assert parse_user_ids(API33.text("pm_list_users_two_users")) == frozenset({0, user})


def test_a_grant_for_a_user_the_device_does_not_have_exits_zero_silently():
    """Why the user is checked against `pm list users` before the grant."""
    entry = _entry("pm_grant_user_missing")
    assert API33.text("pm_grant_user_missing").strip() == "exit=0"
    assert int(entry["missing_user"]) not in parse_user_ids(API33.text("pm_list_users_two_users"))


def test_an_unrequested_grant_for_a_secondary_user_throws():
    entry = _entry("pm_grant_user_not_requested")
    assert entry["exit_status"] == 255
    assert entry["stream"] == "stderr"
    assert "java.lang.SecurityException: Package" in API33.text("pm_grant_user_not_requested")


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


# --- host adb ------------------------------------------------------------------


def test_the_recorded_adb_client_is_one_the_skill_reads_files_with():
    """The banner parses, and to a client run_as accepts (`adb shell -T` is refused, not run)."""
    from common.run_as import MIN_ADB_FOR_READS, parse_adb_version

    version = parse_adb_version(API33.text("adb_version"))
    assert version is not None, "the recorded banner does not parse"
    assert version >= MIN_ADB_FOR_READS
