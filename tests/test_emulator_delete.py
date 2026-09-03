"""Device-free tests for emulator_delete.

These mock the module's ``subprocess`` and the avdmanager/AVD-home lookups so no
emulator, adb, or Android SDK is required. They assert the pure arg->command
mapping for single delete, ``--all`` (batch delete), and the ``--old`` newest-N
recency ordering, plus the ANDROID_EMU_DELETE_KEEP tunable.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import emulator_delete
import pytest

from common.sdk_tools import SdkToolError


@pytest.fixture
def deleter(monkeypatch):
    """An EmulatorDeleter whose avdmanager binary always resolves."""
    d = emulator_delete.EmulatorDeleter()
    monkeypatch.setattr(d, "get_avdmanager_path", lambda: "/sdk/avdmanager")
    return d


def _capture_run(monkeypatch):
    """Patch subprocess.run to record commands and report success."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(emulator_delete.subprocess, "run", fake_run)
    return calls


def test_single_delete_command_mapping(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: ["MyTestDevice", "Other"])
    calls = _capture_run(monkeypatch)

    success, message = deleter.delete("MyTestDevice", confirm=True)

    assert success is True
    assert message == "AVD deleted: MyTestDevice"
    assert calls == [["/sdk/avdmanager", "delete", "avd", "--name", "MyTestDevice"]]


def test_single_delete_missing_avd(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: ["Other"])
    calls = _capture_run(monkeypatch)

    success, message = deleter.delete("MyTestDevice", confirm=True)

    assert success is False
    assert message == "AVD not found: MyTestDevice"
    assert calls == []  # never invokes avdmanager when the AVD is absent


def test_delete_prompts_by_default_and_can_decline(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: ["MyTestDevice"])
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    success, message = deleter.delete("MyTestDevice", confirm=False)

    assert success is False
    assert message == "Deletion cancelled by user"
    assert calls == []  # declined -> no delete command issued


def test_delete_prompt_accepts_yes(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: ["MyTestDevice"])
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "YES")

    success, _message = deleter.delete("MyTestDevice", confirm=False)

    assert success is True
    assert calls == [["/sdk/avdmanager", "delete", "avd", "--name", "MyTestDevice"]]


def test_delete_yes_skips_prompt(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: ["MyTestDevice"])
    calls = _capture_run(monkeypatch)

    def _boom(_prompt):
        raise AssertionError("input() must not be called when confirm=True")

    monkeypatch.setattr("builtins.input", _boom)

    success, _message = deleter.delete("MyTestDevice", confirm=True)

    assert success is True
    assert len(calls) == 1


def test_delete_all_deletes_every_avd(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: ["A", "B", "C"])
    calls = _capture_run(monkeypatch)

    succeeded, failed, results = deleter.delete_all(confirm=True)

    assert (succeeded, failed) == (3, 0)
    assert [r["name"] for r in results] == ["A", "B", "C"]
    assert calls == [
        ["/sdk/avdmanager", "delete", "avd", "--name", "A"],
        ["/sdk/avdmanager", "delete", "avd", "--name", "B"],
        ["/sdk/avdmanager", "delete", "avd", "--name", "C"],
    ]


def test_delete_all_empty(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds", lambda: [])
    calls = _capture_run(monkeypatch)

    assert deleter.delete_all(confirm=True) == (0, 0, [])
    assert calls == []


def test_list_avds_by_recency_orders_newest_first(monkeypatch, tmp_path, deleter):
    # Build three AVD config dirs with distinct mtimes.
    monkeypatch.setattr(deleter, "list_avds", lambda: ["old", "mid", "new"])
    monkeypatch.setattr(deleter, "get_avd_home", lambda: tmp_path)
    for name, mtime in [("old", 100), ("mid", 200), ("new", 300)]:
        avd_dir = tmp_path / f"{name}.avd"
        avd_dir.mkdir()
        import os

        os.utime(avd_dir, (mtime, mtime))

    assert deleter.list_avds_by_recency() == ["new", "mid", "old"]


def test_delete_old_keeps_newest_n(monkeypatch, deleter):
    # Recency order is newest-first; --old 1 keeps "new", deletes the rest.
    monkeypatch.setattr(deleter, "list_avds_by_recency", lambda: ["new", "mid", "old"])
    calls = _capture_run(monkeypatch)

    succeeded, failed, results = deleter.delete_old(keep_count=1, confirm=True)

    assert (succeeded, failed) == (2, 0)
    assert [r["name"] for r in results] == ["mid", "old"]
    assert calls == [
        ["/sdk/avdmanager", "delete", "avd", "--name", "mid"],
        ["/sdk/avdmanager", "delete", "avd", "--name", "old"],
    ]


def test_delete_old_keep_all_when_count_exceeds(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds_by_recency", lambda: ["a", "b"])
    calls = _capture_run(monkeypatch)

    assert deleter.delete_old(keep_count=5, confirm=True) == (0, 0, [])
    assert calls == []


def test_delete_old_prompt_can_decline(monkeypatch, deleter):
    monkeypatch.setattr(deleter, "list_avds_by_recency", lambda: ["new", "old"])
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert deleter.delete_old(keep_count=1, confirm=False) == (0, 0, [])
    assert calls == []


def test_default_keep_count_tunable():
    assert emulator_delete.DEFAULT_KEEP_COUNT == 3


def test_keep_count_env_override(monkeypatch):
    monkeypatch.setenv("ANDROID_EMU_DELETE_KEEP", "7")
    reloaded = importlib.reload(emulator_delete)
    try:
        assert reloaded.DEFAULT_KEEP_COUNT == 7
    finally:
        monkeypatch.delenv("ANDROID_EMU_DELETE_KEEP", raising=False)
        importlib.reload(emulator_delete)


# --- every avdmanager call is bounded --------------------------------------
# avdmanager is an Android SDK tool, not adb, so it does not route through
# common.adb_exec -- but an unbounded call here still wedges the caller.


def test_every_avdmanager_call_is_bounded(monkeypatch, deleter):
    """Both of the AST sweep's unbounded calls in this file, pinned."""
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout="MyTestDevice\n", stderr="")

    monkeypatch.setattr(emulator_delete.subprocess, "run", fake_run)

    deleter.list_avds()
    deleter.delete("MyTestDevice", confirm=True)

    assert len(calls) == 3  # list, then delete()'s existence check + the delete
    unbounded = [c["cmd"] for c in calls if not c["kwargs"].get("timeout")]
    assert not unbounded, f"unbounded avdmanager calls: {unbounded}"


def test_listing_timeout_is_reported_not_degraded_to_an_empty_list(monkeypatch, deleter):
    """L8: an empty list is "no AVDs", which is not what a timeout means.

    `--all` and `--old` read that empty list, deleted nothing, printed "No AVDs
    deleted" and exited 0 -- indistinguishable from a host that had none.
    """

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=emulator_delete.SDK_TOOL_TIMEOUT)

    monkeypatch.setattr(emulator_delete.subprocess, "run", fake_run)
    with pytest.raises(SdkToolError) as excinfo:
        deleter.list_avds()
    assert "retry" in str(excinfo.value), "the timeout names no remedy"


def test_every_mode_reports_a_missing_avdmanager_the_same_way(monkeypatch):
    """L8: `--name` failed loudly while `--all` and `--old` exited 0.

    The single-name path returned "avdmanager not found" and exit 1; the two
    batch paths turned the identical condition into (0, 0, []), which
    `_report_batch` printed as "No AVDs deleted". One error, one exit status,
    whichever mode asked.
    """
    deleter = emulator_delete.EmulatorDeleter()
    monkeypatch.setattr(deleter, "get_avdmanager_path", lambda: None)

    messages = []
    for call in (
        lambda: deleter.delete("MyTestDevice", confirm=True),
        lambda: deleter.delete_all(confirm=True),
        lambda: deleter.delete_old(keep_count=1, confirm=True),
        deleter.list_avds,
    ):
        with pytest.raises(SdkToolError) as excinfo:
            call()
        messages.append(str(excinfo.value))

    assert len(set(messages)) == 1, f"the modes disagree about the same failure: {messages}"
    assert "cmdline-tools" in messages[0], "the failure names no remedy"


@pytest.mark.parametrize(
    "argv",
    [
        ["--all", "--yes", "--json"],
        ["--old", "1", "--yes", "--json"],
        ["--name", "MyTestDevice", "--yes", "--json"],
        ["--list", "--json"],
    ],
    ids=["all", "old", "name", "list"],
)
def test_the_cli_exits_non_zero_without_avdmanager(monkeypatch, capsys, argv):
    """The agent-visible half: exit status, and an error in the JSON body."""
    monkeypatch.setattr(emulator_delete.EmulatorDeleter, "get_avdmanager_path", lambda _self: None)
    monkeypatch.setattr(emulator_delete.sys, "argv", ["emulator_delete.py", *argv])

    with pytest.raises(SystemExit) as exc:
        emulator_delete.main()

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload, f"--json reported no error: {payload}"
    assert payload.get("succeeded") is None, "a batch summary was printed alongside the error"


# --- L5: ranking by age needs real mtimes ----------------------------------
# `_mtime` answered OSError with 0.0. In a newest-first sort that puts every
# AVD the script could not find at the END -- which is the end `--old` deletes.
# So on a host whose AVD home is not where this script looked, `--old 3`
# proposed deleting everything, and the only symptom was that it worked.


def test_an_unresolvable_avd_home_fails_instead_of_ranking_everything_oldest(
    monkeypatch, tmp_path, deleter
):
    """L5: no `.avd` directory means no ranking, not a ranking of zeroes."""
    monkeypatch.setattr(deleter, "list_avds", lambda: ["alpha", "beta", "gamma"])
    monkeypatch.setattr(deleter, "get_avd_home", lambda: tmp_path / "nowhere")
    calls = _capture_run(monkeypatch)

    with pytest.raises(emulator_delete.AvdHomeError) as excinfo:
        deleter.delete_old(keep_count=1, confirm=True)

    assert calls == [], "AVDs were deleted on a ranking that could not be computed"
    message = str(excinfo.value)
    assert "cannot rank AVDs by age" in message
    assert "ANDROID_AVD_HOME" in message and "ANDROID_SDK_HOME" in message, message


def test_the_cli_exits_non_zero_when_the_avds_cannot_be_ranked(monkeypatch, tmp_path, capsys):
    """The agent-visible half of L5: `--old 3 --yes` deletes nothing and says why."""
    monkeypatch.setattr(
        emulator_delete.EmulatorDeleter, "get_avdmanager_path", lambda _self: "/sdk/avdmanager"
    )
    monkeypatch.setattr(
        emulator_delete.EmulatorDeleter, "list_avds", lambda _self: ["alpha", "beta", "gamma"]
    )
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "nowhere"))
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(
        emulator_delete.sys, "argv", ["emulator_delete.py", "--old", "3", "--yes", "--json"]
    )

    with pytest.raises(SystemExit) as exc:
        emulator_delete.main()

    assert exc.value.code == 1
    assert calls == [], "a delete was issued despite the failure"
    payload = json.loads(capsys.readouterr().out)
    assert "cannot rank AVDs by age" in payload.get("error", ""), payload


def test_a_resolvable_home_still_keeps_the_newest_n(monkeypatch, tmp_path, deleter):
    """The other side of the same change: real mtimes still rank normally."""
    monkeypatch.setattr(deleter, "list_avds", lambda: ["old", "mid", "new"])
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    for name, mtime in [("old", 100), ("mid", 200), ("new", 300)]:
        avd_dir = tmp_path / f"{name}.avd"
        avd_dir.mkdir()
        os.utime(avd_dir, (mtime, mtime))
    calls = _capture_run(monkeypatch)

    succeeded, failed, _results = deleter.delete_old(keep_count=2, confirm=True)

    assert (succeeded, failed) == (1, 0)
    assert calls == [["/sdk/avdmanager", "delete", "avd", "--name", "old"]]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"ANDROID_AVD_HOME": "/avds"}, "/avds"),
        ({"ANDROID_SDK_HOME": "/sdkhome"}, "/sdkhome/.android/avd"),
        ({"ANDROID_AVD_HOME": "/avds", "ANDROID_SDK_HOME": "/sdkhome"}, "/avds"),
    ],
    ids=["avd_home", "sdk_home", "avd_home_wins"],
)
def test_the_avd_home_is_resolved_the_way_avdmanager_resolves_it(monkeypatch, env, expected):
    """L5: `ANDROID_SDK_HOME` was ignored, so relocated AVD trees were invisible.

    Note the asymmetry that makes this easy to get wrong: ANDROID_AVD_HOME is
    the directory of `.avd` directories, while ANDROID_SDK_HOME is the parent
    of `.android`.
    """
    for name in ("ANDROID_AVD_HOME", "ANDROID_SDK_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert emulator_delete.EmulatorDeleter().get_avd_home() == Path(expected)


def test_the_avd_home_falls_back_to_the_home_directory(monkeypatch, tmp_path):
    """With neither variable set, `~/.android/avd` -- the SDK's own default."""
    for name in ("ANDROID_AVD_HOME", "ANDROID_SDK_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(emulator_delete.Path, "home", staticmethod(lambda: tmp_path))

    assert emulator_delete.EmulatorDeleter().get_avd_home() == tmp_path / ".android" / "avd"


def test_erase_resolves_the_avd_home_the_same_way(monkeypatch):
    """The same two-branch resolver was in emulator_erase; one function now."""
    import emulator_erase

    monkeypatch.delenv("ANDROID_AVD_HOME", raising=False)
    monkeypatch.setenv("ANDROID_SDK_HOME", "/sdkhome")

    assert (
        emulator_erase.EmulatorEraser().get_avd_home()
        == emulator_delete.EmulatorDeleter().get_avd_home()
    )


@pytest.mark.parametrize("keep", ["0", "-1"], ids=["zero", "negative"])
def test_old_with_no_survivors_is_a_usage_error_not_a_silent_delete_all(
    monkeypatch, tmp_path, capsys, keep
):
    """`--old 0` keeps nothing, so it quietly means `--all` (part of L5).

    Reached by a typo, by ANDROID_EMU_DELETE_KEEP=0, or by reading `--old 0` as
    "zero days old". Two spellings for the same irreversible act, one of them
    by accident, so this one is rejected and points at the other.
    """
    # The guard fires before any AVD home is consulted, but point it at a tmp
    # dir anyway: a test that reaches the developer's real ~/.android/avd when
    # the guard is removed is reading a machine, not a fixture.
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path))
    monkeypatch.setattr(
        emulator_delete.EmulatorDeleter, "get_avdmanager_path", lambda _self: "/sdk/avdmanager"
    )
    monkeypatch.setattr(emulator_delete.EmulatorDeleter, "list_avds", lambda _self: ["a", "b"])
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(
        emulator_delete.sys, "argv", ["emulator_delete.py", "--old", keep, "--yes", "--json"]
    )

    with pytest.raises(SystemExit) as exc:
        emulator_delete.main()

    assert exc.value.code == 2, "a usage error must not share an exit status with a real failure"
    assert calls == [], "AVDs were deleted by a rejected invocation"
    assert "--all" in json.loads(capsys.readouterr().out).get("error", "")


def test_delete_reports_its_own_timeout(monkeypatch, deleter):
    """A bounded call raises TimeoutExpired; the user gets a message, not a trace."""
    monkeypatch.setattr(deleter, "list_avds", lambda: ["MyTestDevice"])

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=emulator_delete.SDK_TOOL_TIMEOUT)

    monkeypatch.setattr(emulator_delete.subprocess, "run", fake_run)

    success, message = deleter.delete("MyTestDevice", confirm=True)

    assert success is False
    assert str(emulator_delete.SDK_TOOL_TIMEOUT) in message
    assert "MyTestDevice" in message
