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

All adb calls are faked, so no device is required.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import privacy_manager
import pytest
from privacy_manager import PrivacyManager, build_test_trail, print_permissions

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
        # The script passes check=True and an explicit timeout; assert contract.
        assert kwargs.get("check") is True
        assert "timeout" in kwargs
        for tokens, response in self._routes:
            if _contains(list(cmd), tokens):
                if response.returncode != 0:
                    raise subprocess.CalledProcessError(
                        response.returncode, cmd, response.stdout, response.stderr
                    )
                return response
        return self.default

    def commands_matching(self, *tokens: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if _contains(cmd, tokens)]


@pytest.fixture
def fake_adb(monkeypatch) -> FakeAdb:
    """Patch the module's subprocess.run with a scriptable fake."""
    fake = FakeAdb()
    monkeypatch.setattr(privacy_manager.subprocess, "run", fake.run)
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
    """No evidence is not the same as evidence of success."""
    fake_adb.when("pm", "grant", returncode=0, stdout="")
    fake_adb.when("dumpsys", "package", returncode=1, stderr="device offline")

    ok, message = PrivacyManager(serial="emulator-5554").grant_permission(DESKCLOCK, "calendar")

    assert ok is False
    assert "could not read" in message


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
