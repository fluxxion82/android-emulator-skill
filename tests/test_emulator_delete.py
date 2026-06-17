"""Device-free tests for emulator_delete.

These mock the module's ``subprocess`` and the avdmanager/AVD-home lookups so no
emulator, adb, or Android SDK is required. They assert the pure arg->command
mapping for single delete, ``--all`` (batch delete), and the ``--old`` newest-N
recency ordering, plus the ANDROID_EMU_DELETE_KEEP tunable.
"""

from __future__ import annotations

import importlib

import emulator_delete
import pytest


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
