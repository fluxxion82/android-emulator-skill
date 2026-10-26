"""Device-free tests for privacy_manager.

Two of these cover defects that shipped inert behind a green suite:

S15  ``--list`` looked for a ``granted permissions:`` section in
     ``dumpsys package``. There is no such section, so every query answered
     ``{"granted": [], "requested": []}`` and exited 0. Nothing tested the
     parser at all, and the fix is only checkable against real dump text --
     hence the ``recorded`` fixtures rather than a hand-written sample.

S16  ``--grant`` reported success whenever ``pm grant`` exited 0. Granting a
     permission the app never requested exits 0 and prints nothing (recorded:
     ``pm_grant_not_requested``), so a no-op reported success. The old test
     asserted exactly that: it mocked ``returncode=0, stdout=""`` and required
     ``ok is True``. Grant and revoke now read the state back, so the mock has
     to answer the read-back too, which is why ``FakeAdb`` routes on the
     device-side command instead of returning one canned result.

All adb calls are faked, so no device is required. The fake sits where
``common.adb_exec.run_adb`` reaches the host, because that is where every call
goes now: this module used to call ``subprocess.run`` itself, four times, and
its fake asserted ``check=True`` reached it -- the bypass, pinned as the
contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import privacy_manager
import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from fake_toolchain import assert_refusal
from privacy_manager import PrivacyManager, build_test_trail, print_permissions

from common import adb_exec, cli
from common.device_utils import parse_package_permissions

DESKCLOCK = "com.google.android.deskclock"
COMPOSE_FIXTURE = "com.example.composefixture"


def _contains(cmd: list[str], tokens: tuple[str, ...]) -> bool:
    """Whether ``tokens`` appears as a contiguous run inside ``cmd``."""
    span = len(tokens)
    return any(cmd[i : i + span] == list(tokens) for i in range(len(cmd) - span + 1))


class FakeAdb:
    """Routes adb invocations to canned results and records every call.

    Keyed on the device-side command so one test can script the ``pm grant``
    and the ``dumpsys package`` read-back independently. The two disagreeing --
    a grant that exits 0 while the dump still shows the permission ungranted --
    is precisely the failure the old single-result mock could not express.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._routes: list[tuple[tuple[str, ...], SimpleNamespace]] = []
        self.default = SimpleNamespace(returncode=0, stdout="", stderr="")

    def when(self, *tokens: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Answer any command containing ``tokens`` with this result."""
        self._routes.append(
            (tokens, SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))
        )

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        # run_adb's contract with the host: bounded, never raising on status --
        # it classifies the result itself.
        assert kwargs.get("check") is False
        assert kwargs.get("timeout")
        for tokens, response in self._routes:
            if _contains(list(cmd), tokens):
                return response
        return self.default

    def commands_matching(self, *tokens: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if _contains(cmd, tokens)]


@pytest.fixture
def fake_adb(monkeypatch) -> FakeAdb:
    """Patch the host side of run_adb with a scriptable fake."""
    fake = FakeAdb()
    monkeypatch.setattr(privacy_manager.adb_exec.subprocess, "run", fake.run)
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])
    return fake


@pytest.fixture
def deskclock_dump(recorded) -> str:
    """A real dump: READ_CALENDAR denied, POST_NOTIFICATIONS granted."""
    return recorded.text("dumpsys_package_permissions")


# ---------------------------------------------------------------------------
# S15 — --list parses the sections dumpsys actually prints.
# ---------------------------------------------------------------------------


def test_the_header_the_old_parser_looked_for_does_not_exist(recorded):
    """The whole defect in one line, asserted against the device's own output."""
    for name in (
        "dumpsys_package_permissions",
        "dumpsys_package_shared_uid",
        "dumpsys_package_after_silent_grant",
    ):
        assert "granted permissions:" not in recorded.text(name).lower()


def test_list_reports_runtime_state_from_a_real_dump(fake_adb, deskclock_dump):
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    success, _, data = PrivacyManager(serial="emulator-5554").list_app_permissions(DESKCLOCK)

    assert success is True
    runtime = {entry["permission"]: entry["granted"] for entry in data["runtime"]}
    assert runtime == {
        "android.permission.READ_CALENDAR": False,
        "android.permission.POST_NOTIFICATIONS": True,
        "android.permission.READ_MEDIA_AUDIO": False,
    }
    assert data["denied"] == [
        "android.permission.READ_CALENDAR",
        "android.permission.READ_MEDIA_AUDIO",
    ]


def test_requested_is_not_reported_as_granted(fake_adb, deskclock_dump):
    """`requested permissions:` carries no state; reading it as held is the bug."""
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    _, _, data = PrivacyManager(serial="emulator-5554").list_app_permissions(DESKCLOCK)

    assert "android.permission.READ_CALENDAR" in data["requested"]
    assert "android.permission.READ_CALENDAR" not in data["granted"]


def test_install_permissions_are_kept_apart_from_runtime(fake_adb, deskclock_dump):
    """`pm grant` on an install permission raises; the split is what says so."""
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    _, _, data = PrivacyManager(serial="emulator-5554").list_app_permissions(DESKCLOCK)

    install = {entry["permission"] for entry in data["install"]}
    runtime = {entry["permission"] for entry in data["runtime"]}
    assert "android.permission.INTERNET" in install
    assert not install & runtime


def test_the_repeated_hidden_system_package_copy_is_not_merged_in(fake_adb, deskclock_dump):
    """An updated system app prints every section twice.

    Once under `Packages:` and again under `Hidden system packages:`. Taking
    every matching header reports each permission twice, so a caller counting
    them, or diffing two dumps, gets a wrong answer from a right-looking list.
    """
    assert deskclock_dump.count("install permissions:") == 2, "fixture lost the duplicate"
    assert deskclock_dump.count("runtime permissions:") == 2, "fixture lost the duplicate"
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    _, _, data = PrivacyManager(serial="emulator-5554").list_app_permissions(DESKCLOCK)

    names = [entry["permission"] for entry in data["install"] + data["runtime"]]
    assert len(names) == len(set(names)), f"duplicated entries: {names}"
    assert data["requested"].count("android.permission.READ_CALENDAR") == 1
    assert len(data["declared"]) == 2


def test_a_shared_uid_package_still_reports_its_runtime_permissions(fake_adb, recorded):
    """The trap in confining the parser to `Packages:`.

    A package in a shared uid has its runtime state tracked against the UID, so
    the dump prints it under `Shared users:` and nowhere else. Reading only
    `Packages:` answers that a system app holds no runtime permissions at all.
    """
    dump = recorded.text("dumpsys_package_shared_uid")
    packages_block = dump[dump.index("\nPackages:") : dump.index("\nQueries:")]
    assert "runtime permissions:" not in packages_block, "fixture lost the trap"
    fake_adb.when("dumpsys", "package", stdout=dump)

    _, _, data = PrivacyManager(serial="emulator-5554").list_app_permissions(
        "com.android.localtransport"
    )

    assert "android.permission.CAMERA" in data["granted"]
    assert len(data["runtime"]) > 0


def test_an_uninstalled_package_is_an_error_not_an_empty_list(fake_adb, recorded):
    """`dumpsys package <unknown>` prints one line and exits 0."""
    dump = recorded.text("dumpsys_package_unknown")
    assert "Unable to find package" in dump
    fake_adb.when("dumpsys", "package", stdout=dump)

    success, message, data = PrivacyManager(serial="emulator-5554").list_app_permissions(
        "com.example.not.installed"
    )

    assert success is False
    assert "not installed" in message
    assert data["found"] is False


def test_list_report_shows_runtime_state_and_hides_the_install_wall(capsys, deskclock_dump):
    """Concise by default: the three runtime lines, not the install-time wall."""
    data = {"package": DESKCLOCK, **parse_package_permissions(deskclock_dump)}

    print_permissions(DESKCLOCK, data, verbose=False)
    concise = capsys.readouterr().out
    assert "✗ android.permission.READ_CALENDAR" in concise
    assert "android.permission.INTERNET" not in concise

    print_permissions(DESKCLOCK, data, verbose=True)
    assert "android.permission.INTERNET" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# S16 — a grant that changed nothing is not a success.
# ---------------------------------------------------------------------------


def test_a_grant_that_did_nothing_is_not_a_success(fake_adb, recorded):
    """The exact shape the old test asserted was a success.

    `pm grant` of a permission the app never requested: exit 0, empty output
    (pm_grant_not_requested). The read-back dump, captured immediately after
    that same command, does not mention the permission at all.
    """
    silent = recorded.text("pm_grant_not_requested")
    assert silent.strip() == "exit=0", "the fixture no longer records a silent success"
    after = recorded.text("dumpsys_package_after_silent_grant")
    assert "android.permission.POST_NOTIFICATIONS" not in after

    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=after)

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(
        COMPOSE_FIXTURE, "notification"
    )

    assert ok is False
    assert "did not take effect" in message
    assert "does not request" in message


def test_a_grant_that_took_effect_is_a_success(fake_adb, deskclock_dump):
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(DESKCLOCK, "notification")

    assert ok is True
    assert "verified" in message
    assert fake_adb.commands_matching("pm", "grant"), f"no pm grant issued: {fake_adb.calls}"
    assert fake_adb.commands_matching("dumpsys", "package"), "grant never read the state back"


def test_a_grant_left_denied_is_not_a_success(fake_adb, deskclock_dump):
    """Exit 0, permission present in the dump, still granted=false."""
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(DESKCLOCK, "calendar")

    assert ok is False
    assert "still denied" in message


def test_a_refused_grant_reports_the_platforms_own_words(fake_adb, recorded):
    """Granting an install permission: SecurityException on stderr, exit 255."""
    refusal = recorded.text("pm_grant_not_changeable")
    fake_adb.when("pm", "grant", returncode=255, stderr=refusal)

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(
        COMPOSE_FIXTURE, "android.permission.INTERNET"
    )

    assert ok is False
    assert "SecurityException" in message
    assert not fake_adb.commands_matching("dumpsys"), "a refused grant should not be read back"


def test_a_revoke_that_left_it_granted_is_not_a_success(fake_adb, deskclock_dump):
    fake_adb.when("pm", "revoke", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    ok, message = PrivacyManager(serial="emulator-5554").revoke_permission(
        DESKCLOCK, "notification"
    )

    assert ok is False
    assert "still granted" in message


def test_a_revoke_that_took_effect_is_a_success(fake_adb, deskclock_dump):
    fake_adb.when("pm", "revoke", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    ok, message = PrivacyManager(serial="emulator-5554").revoke_permission(DESKCLOCK, "calendar")

    assert ok is True
    assert "verified" in message


def test_revoking_something_the_app_never_asked_for_says_so(fake_adb, recorded):
    """The end state is right, so it is not a failure -- but it is a typo signal."""
    fake_adb.when("pm", "revoke", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=recorded.text("dumpsys_package_after_silent_grant"))

    ok, message = PrivacyManager(serial="emulator-5554").revoke_permission(
        COMPOSE_FIXTURE, "notification"
    )

    assert ok is True
    assert "does not request it" in message


def test_an_unreadable_read_back_is_not_a_success(fake_adb):
    """No evidence is not the same as evidence of success.

    `dumpsys` ran and failed on the device -- a command failure, so the grant is
    reported as unverified and a batch moves on.
    """
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", returncode=1, stderr="")

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(DESKCLOCK, "calendar")

    assert ok is False
    assert "could not read" in message


def test_a_read_back_from_a_device_that_is_gone_raises(fake_adb, recorded_anywhere):
    """The other kind of failure: the device is not there. Not "unverified" --
    nothing can be verified -- so it raises, and the CLI boundary says exit 3."""
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when(
        "dumpsys", "package", returncode=1, stderr=recorded_anywhere("adb_shell_device_not_found")
    )

    with pytest.raises(adb_exec.DeviceNotFoundError):
        PrivacyManager(serial="emulator-5554").grant_permission(DESKCLOCK, "calendar")


# ---------------------------------------------------------------------------
# A batch: a refusal moves on to the next permission; a lost device stops it.
# ---------------------------------------------------------------------------


def _cli(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["privacy_manager.py", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.run(privacy_manager.main)
    captured = capsys.readouterr()
    return exc.value.code, captured.out, captured.err


def test_a_refused_permission_does_not_stop_the_batch(
    fake_adb, recorded, deskclock_dump, monkeypatch, capsys
):
    """`pm` refusing one permission (recorded: INTERNET is install-time, status 255)
    is that permission's failure; the next one in the list is still granted."""
    fake_adb.when(
        "android.permission.INTERNET",
        returncode=255,
        stderr=recorded.text("pm_grant_not_changeable"),
    )
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    code, out, _ = _cli(
        monkeypatch,
        capsys,
        "--grant",
        "android.permission.INTERNET,notification",
        "--package",
        DESKCLOCK,
        "--json",
    )

    assert code == 1, "one permission failed, so the batch did"
    report = json.loads(out)  # one object: no message line printed ahead of it
    assert [r["success"] for r in report["items"]] == [False, True]
    assert "SecurityException" in report["items"][0]["error"]


def test_a_device_lost_mid_batch_stops_it_with_the_boundary_shape(
    fake_adb, deskclock_dump, recorded_anywhere, monkeypatch, capsys
):
    """The first grant lands; the device is gone for the second. Going on would
    repeat the same error for every permission left, so the batch stops and
    common.batch reports it: exit 3, the refusal object with the permissions
    so far -- the one granted, and the one the device error stopped at."""
    fake_adb.when(
        "android.permission.READ_CALENDAR",
        returncode=1,
        stderr=recorded_anywhere("adb_shell_device_not_found"),
    )
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    code, out, _ = _cli(
        monkeypatch,
        capsys,
        "--grant",
        "notification,calendar",
        "--package",
        DESKCLOCK,
        "--json",
    )

    assert code == 3
    payload = json.loads(out)
    assert set(payload) == {"success", "error", "remedy", "action", "items"}
    assert payload["success"] is False
    assert [item["success"] for item in payload["items"]] == [True, False]
    assert len(fake_adb.commands_matching("pm", "grant")) == 2, "the batch did not stop there"


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_listing_a_package_that_is_not_installed_is_the_shared_refusal(
    fake_adb, recorded, monkeypatch, capsys, json_mode
):
    """`dumpsys package <unknown>` exits 0 (recorded: dumpsys_package_unknown).

    The refusal was `{"success": false, "message": ...}` under --json -- no
    `error`, no remedy -- and the bare message on stderr otherwise.
    """
    fake_adb.when("dumpsys", "package", stdout=recorded.text("dumpsys_package_unknown"))
    argv = ["--list", "--package", "com.example.app1", "--serial", "emulator-5554"]

    code, out, err = _cli(monkeypatch, capsys, *argv, *(["--json"] if json_mode else []))

    assert_refusal(
        subprocess.CompletedProcess(argv, code, out, err),
        json_mode=json_mode,
        naming=("not installed",),
        status=1,
    )


def test_no_package_is_reported_through_the_boundary(monkeypatch, capsys):
    """It printed the whole help text to stdout -- the answer, under --json."""
    code, out, err = _cli(monkeypatch, capsys, "--list", "--json")

    assert code == 1
    payload = json.loads(out)
    assert "--package" in payload["error"]
    assert err == ""


# ---------------------------------------------------------------------------
# --reset is an alias for revoke (Android has no prompt-state reset).
# ---------------------------------------------------------------------------


def test_reset_builds_the_revoke_command(fake_adb, deskclock_dump):
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    ok, message = PrivacyManager(serial="emulator-5554").reset_permission(DESKCLOCK, "calendar")

    assert ok is True
    # Records that reset went through revoke for transparency.
    assert "via revoke" in message
    revokes = fake_adb.commands_matching("pm", "revoke")
    assert len(revokes) == 1
    assert not fake_adb.commands_matching("pm", "grant")
    assert DESKCLOCK in revokes[0]
    assert "android.permission.READ_CALENDAR" in revokes[0]


def test_reset_matches_revoke_command_exactly(fake_adb, deskclock_dump):
    """reset_permission and revoke_permission must build identical adb argv."""
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    mgr = PrivacyManager(serial="emulator-5554")
    mgr.revoke_permission(DESKCLOCK, "calendar")
    mgr.reset_permission(DESKCLOCK, "calendar")

    revokes = fake_adb.commands_matching("pm", "revoke")
    assert len(revokes) == 2
    assert revokes[0] == revokes[1]
    assert "android.permission.READ_CALENDAR" in revokes[0]


def test_reset_unknown_permission_short_circuits(fake_adb):
    """Unknown permission fails before any adb call and is not reported as reset."""
    ok, message = PrivacyManager(serial="emulator-5554").reset_permission(
        DESKCLOCK, "not_a_real_perm"
    )

    assert ok is False
    assert "Unknown permission" in message
    assert fake_adb.calls == []


def test_reset_propagates_revoke_failure(fake_adb):
    """When revoke fails, reset returns the underlying failure unchanged."""
    fake_adb.when("pm", "revoke", returncode=1, stderr="boom")

    ok, message = PrivacyManager(serial="emulator-5554").reset_permission(DESKCLOCK, "camera")

    assert ok is False
    assert "via revoke" not in message
    assert "Failed to revoke permission" in message


# ---------------------------------------------------------------------------
# Test-trail metadata.
# ---------------------------------------------------------------------------


def test_build_test_trail_none_when_absent():
    assert build_test_trail(None, None) is None


def test_build_test_trail_scenario_only():
    trail = build_test_trail("onboarding", None)
    assert trail is not None
    assert trail["scenario"] == "onboarding"
    assert "step" not in trail
    assert "timestamp" in trail


def test_build_test_trail_step_only():
    trail = build_test_trail(None, 3)
    assert trail is not None
    assert trail["step"] == 3
    assert "scenario" not in trail
    assert "timestamp" in trail


def test_build_test_trail_step_zero_is_recorded():
    # step 0 is a real value, not "absent" -> must be recorded.
    trail = build_test_trail(None, 0)
    assert trail is not None
    assert trail["step"] == 0


def test_build_test_trail_both():
    trail = build_test_trail("login", 2)
    assert trail is not None
    assert trail["scenario"] == "login"
    assert trail["step"] == 2
    assert "timestamp" in trail


# ---------------------------------------------------------------------------
# --user: the mutation and the read-back name the same user.
#
# Ground truth is emulator-api33's two-user window: CAMERA granted for the
# secondary user only (dumpsys_package_two_users), READ_PHONE_NUMBERS likewise
# for a shared-uid package (dumpsys_package_shared_uid_two_users), and the
# refusals in pm_grant_user_not_requested's run.
# ---------------------------------------------------------------------------

API33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")
SHARED_UID_PACKAGE = "com.android.providers.contacts"


def _api33_user(name: str) -> int:
    for entry in API33.manifest["fixtures"]:
        if entry["name"] == name:
            return int(entry["secondary_user"])
    pytest.fail(f"emulator-api33/MANIFEST.json has no entry for {name}")


def test_the_grant_argv_names_the_user(fake_adb):
    user = _api33_user("dumpsys_package_two_users")
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_two_users"))

    PrivacyManager(serial="emulator-5554", user=user).grant_permission(COMPOSE_FIXTURE, "camera")

    (grant,) = fake_adb.commands_matching("pm", "grant")
    assert grant[grant.index("grant") + 1 :] == [
        "--user",
        str(user),
        COMPOSE_FIXTURE,
        "android.permission.CAMERA",
    ]


def test_the_revoke_argv_names_user_0_explicitly_by_default(fake_adb, deskclock_dump):
    fake_adb.when("dumpsys", "package", stdout=deskclock_dump)

    PrivacyManager(serial="emulator-5554").revoke_permission(DESKCLOCK, "calendar")

    (revoke,) = fake_adb.commands_matching("pm", "revoke")
    assert revoke[revoke.index("revoke") + 1 : revoke.index("revoke") + 3] == ["--user", "0"]


def test_a_secondary_user_grant_is_verified_against_that_user(fake_adb):
    """CAMERA is granted for the secondary user and denied for user 0 in one dump.

    Verifying against the first section (user 0's) would call this grant a failure;
    verifying user 0's grant against it must still say "still denied".
    """
    user = _api33_user("dumpsys_package_two_users")
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_two_users"))

    ok, message = PrivacyManager(serial="emulator-5554", user=user).grant_permission(
        COMPOSE_FIXTURE, "camera"
    )
    assert ok is True, message
    assert f"user {user}" in message

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(COMPOSE_FIXTURE, "camera")
    assert ok is False
    assert "still denied" in message


def test_a_shared_uid_revoke_is_verified_against_that_user(fake_adb):
    """READ_PHONE_NUMBERS: denied for user 0, granted for the secondary user, under
    `Shared users:`. A revoke for the secondary user that left it granted is a failure."""
    user = _api33_user("dumpsys_package_shared_uid_two_users")
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_shared_uid_two_users"))

    ok, message = PrivacyManager(serial="emulator-5554", user=user).revoke_permission(
        SHARED_UID_PACKAGE, "android.permission.READ_PHONE_NUMBERS"
    )
    assert ok is False
    assert "still granted" in message

    ok, message = PrivacyManager(serial="emulator-5554").revoke_permission(
        SHARED_UID_PACKAGE, "android.permission.READ_PHONE_NUMBERS"
    )
    assert ok is True, message


def test_a_read_back_with_no_section_for_the_user_fails_loudly(fake_adb):
    """A single-user dump asked about user 11: no `User 11:` section. Not "the app
    does not request it" (which a revoke would count as done) and not user 0's state."""
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_shared_uid"))

    for action in ("grant_permission", "revoke_permission"):
        ok, message = getattr(PrivacyManager(serial="emulator-5554", user=11), action)(
            "com.android.localtransport", "camera"
        )
        assert ok is False, (action, message)
        assert "User 11:" in message, message


def test_a_grant_for_a_user_without_the_app_is_not_verified(fake_adb):
    """`pm grant --user <id>` exits 0 for a user the app is not installed for, and the
    dump then reads CAMERA granted=true under `User <id>: ... installed=false`."""
    user = _api33_user("dumpsys_package_user_not_installed")
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_user_not_installed"))

    ok, message = PrivacyManager(serial="emulator-5554", user=user).grant_permission(
        COMPOSE_FIXTURE, "camera"
    )

    assert ok is False
    assert f"not installed for user {user}" in message
    assert "pm install-existing --user" in message


def test_a_revoked_permission_with_no_entry_left_is_denied_not_unrequested(fake_adb):
    """After a grant and revoke for a secondary user, CAMERA has no entry under that user's
    `runtime permissions:` at all (dumpsys_package_user_revoked) while the manifest still
    requests it. That is "denied", not "the app does not request it"."""
    user = _api33_user("dumpsys_package_user_revoked")
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_user_revoked"))
    manager = PrivacyManager(serial="emulator-5554", user=user)

    ok, message = manager.revoke_permission(COMPOSE_FIXTURE, "camera")
    assert ok is True, message
    assert "does not request" not in message

    ok, message = manager.grant_permission(COMPOSE_FIXTURE, "camera")
    assert ok is False
    assert "still denied" in message


def test_a_secondary_user_security_exception_is_reported_as_a_refusal(fake_adb):
    """API 33: granting an unrequested permission for a user throws (exit 255)."""
    user = _api33_user("pm_grant_user_not_requested")
    fake_adb.when("pm", "grant", returncode=255, stderr=API33.text("pm_grant_user_not_requested"))

    ok, message = PrivacyManager(serial="emulator-5554", user=user).grant_permission(
        COMPOSE_FIXTURE, "notification"
    )

    assert ok is False
    assert "SecurityException: Package com.example.composefixture has not requested" in message
    assert f"user {user}" in message
    assert "\tat " not in message and "PermissionManager" not in message, "stack trace leaked"
    assert not fake_adb.commands_matching("dumpsys"), "a refused grant should not be read back"


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
@pytest.mark.parametrize(
    "value",
    ["all", "current", "-1", "eleven", "²", "٣", "1_0", " 5", "1" * 4301, "2147483648"],
    ids=[
        "all",
        "current",
        "negative",
        "word",
        "superscript-2",
        "arabic-indic-3",
        "underscore",
        "space",
        "past-int-str-limit",
        "past-int32",
    ],
)
def test_a_user_that_is_not_one_id_is_refused(fake_adb, monkeypatch, capsys, json_mode, value):
    """Only ASCII digits: `str.isdigit()` accepts a superscript two, which `int()` then
    rejects with a traceback, and `int()` itself reads an Arabic-Indic three and `1_0`."""
    argv = ["--grant", "camera", "--package", COMPOSE_FIXTURE, "--user", value]

    code, out, err = _cli(monkeypatch, capsys, *argv, *(["--json"] if json_mode else []))

    assert_refusal(
        subprocess.CompletedProcess(argv, code, out, err),
        json_mode=json_mode,
        naming=("--user",),
        status=1,
    )
    assert "Traceback" not in out + err
    assert "defect" not in (out + err).lower()
    assert fake_adb.calls == [], "refused before touching the device"


def test_leading_zeros_name_the_same_user():
    assert privacy_manager.parse_user_arg("005") == 5
    assert privacy_manager.parse_user_arg("2147483647") == 2147483647
    assert privacy_manager.parse_user_arg(None) == 0


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_user_the_device_does_not_have_is_refused(fake_adb, monkeypatch, capsys, json_mode):
    """`pm grant --user <missing>` exits 0 silently (pm_grant_user_missing), so the id is
    checked against `pm list users` first."""
    fake_adb.when("pm", "list", "users", stdout=API33.text("pm_list_users"))
    argv = ["--grant", "camera", "--package", COMPOSE_FIXTURE, "--user", "11"]

    code, out, err = _cli(monkeypatch, capsys, *argv, *(["--json"] if json_mode else []))

    assert_refusal(
        subprocess.CompletedProcess(argv, code, out, err),
        json_mode=json_mode,
        naming=("user 11",),
        status=1,
    )
    assert not fake_adb.commands_matching("pm", "grant")


def test_cli_grant_for_a_listed_user_reaches_that_user(fake_adb, monkeypatch, capsys):
    """End to end on one recorded run: the listing names the user, so the grant goes
    out with --user <id>; that run's dump says the app is not installed for it, so the
    read-back refuses -- in the batch's existing output shape."""
    user = _api33_user("dumpsys_package_user_not_installed")
    assert user == _api33_user("pm_list_users_two_users"), "recorded in one run"
    fake_adb.when("pm", "list", "users", stdout=API33.text("pm_list_users_two_users"))
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_user_not_installed"))

    code, out, _ = _cli(
        monkeypatch,
        capsys,
        "--grant",
        "camera",
        "--package",
        COMPOSE_FIXTURE,
        "--user",
        str(user),
        "--json",
    )

    assert code == 1, out
    report = json.loads(out)
    assert set(report) == {"success", "error", "remedy", "action", "items"}, report
    assert report["items"][0]["success"] is False
    assert f"not installed for user {user}" in report["items"][0]["error"]
    (grant,) = fake_adb.commands_matching("pm", "grant")
    assert _contains(grant, ("--user", str(user)))


def test_cli_list_reads_the_requested_user(fake_adb, monkeypatch, capsys):
    """--user 0 names user 0's section; no listing is needed for the system user."""
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_two_users"))

    code, out, _ = _cli(
        monkeypatch, capsys, "--list", "--package", COMPOSE_FIXTURE, "--user", "0", "--json"
    )

    assert code == 0, out
    data = json.loads(out)
    assert data["user"] == 0
    assert [(e["permission"], e["granted"]) for e in data["runtime"]] == [
        ("android.permission.CAMERA", False)
    ]
    assert not fake_adb.commands_matching("pm", "list", "users")


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_cli_list_for_a_user_without_the_app_is_refused(fake_adb, monkeypatch, capsys, json_mode):
    user = _api33_user("dumpsys_package_user_not_installed")
    fake_adb.when("pm", "list", "users", stdout=API33.text("pm_list_users_two_users"))
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_user_not_installed"))
    argv = ["--list", "--package", COMPOSE_FIXTURE, "--user", str(user)]

    code, out, err = _cli(monkeypatch, capsys, *argv, *(["--json"] if json_mode else []))

    assert_refusal(
        subprocess.CompletedProcess(argv, code, out, err),
        json_mode=json_mode,
        naming=(f"not installed for user {user}",),
        status=1,
    )


def _list_revoked_user(fake_adb, monkeypatch, capsys, *extra: str) -> tuple[int, str, str]:
    user = _api33_user("dumpsys_package_user_revoked")
    assert user == _api33_user("pm_list_users_two_users"), "recorded in one run"
    fake_adb.when("pm", "list", "users", stdout=API33.text("pm_list_users_two_users"))
    fake_adb.when("dumpsys", "package", stdout=API33.text("dumpsys_package_user_revoked"))
    return _cli(
        monkeypatch, capsys, "--list", "--package", COMPOSE_FIXTURE, "--user", str(user), *extra
    )


def test_list_shows_a_revoked_permission_with_no_entry_as_denied(fake_adb, monkeypatch, capsys):
    """dumpsys_package_user_revoked: the user's `runtime permissions:` header has no
    entries after a revoke, yet the app requests CAMERA (and user 0 lists it as runtime).
    That is denied -- not "nothing here can be granted or revoked"."""
    code, out, _ = _list_revoked_user(fake_adb, monkeypatch, capsys)

    assert code == 0, out
    assert "✗ android.permission.CAMERA" in out
    assert "nothing here can be granted" not in out


def test_list_json_counts_a_revoked_permission_with_no_entry_as_denied(
    fake_adb, monkeypatch, capsys
):
    code, out, _ = _list_revoked_user(fake_adb, monkeypatch, capsys, "--json")

    assert code == 0, out
    data = json.loads(out)
    assert "android.permission.CAMERA" in data["denied"]
    assert [(e["permission"], e["granted"]) for e in data["runtime"]] == [
        ("android.permission.CAMERA", False)
    ]
