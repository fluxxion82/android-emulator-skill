"""The live instrumented test's teardown never errors the module (no device needed).

``tests/test_instrumented_live.py`` puts back the fixture app AGP uninstalls
after a connected run. Raised in teardown, any failure there would be an ERROR
against the last test, failing the lane on housekeeping instead of on an
instrumented verdict. Each path is driven here with adb doubled: the query
that fails, the reinstall that fails (adb's recorded refusal of a missing APK)
or times out, the reinstall that works, and a report file that cannot be
written.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest
import test_instrumented_live as live

ADB = "adb"
DEVICE = "emulator-5554"


@pytest.fixture
def observed(monkeypatch, tmp_path) -> Path:
    report = tmp_path / "instrumented.md"
    monkeypatch.setattr(live, "REPORT", str(report))
    return report


def _never_installs(monkeypatch) -> list:
    calls: list = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        raise AssertionError(f"the teardown ran {cmd}")

    monkeypatch.setattr(live.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(["adb"], 30), OSError("adb: not found")],
    ids=["timeout", "oserror"],
)
def test_a_failed_installed_query_is_observed_and_skips_the_reinstall(
    error, monkeypatch, tmp_path, observed
):
    def query(_adb, _device):
        raise error

    monkeypatch.setattr(live, "_installed", query)
    calls = _never_installs(monkeypatch)

    live._restore_fixture_app(ADB, DEVICE, tmp_path, was_installed=True)

    assert calls == []
    assert "fixture app: could not check whether it is installed: " in observed.read_text()


def test_a_failed_reinstall_is_observed_with_adbs_last_line(
    monkeypatch, tmp_path, observed, recorded_anywhere
):
    refusal = recorded_anywhere("adb_install_missing_apk_echo_stderr")
    monkeypatch.setattr(live, "_installed", lambda _adb, _device: False)
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=refusal),
    )

    live._restore_fixture_app(ADB, DEVICE, tmp_path, was_installed=True)

    said = observed.read_text()
    assert "reinstall FAILED: " in said, said
    assert refusal.strip().splitlines()[-1] in said, said


def test_a_reinstall_that_times_out_is_observed(monkeypatch, tmp_path, observed):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(live, "_installed", lambda _adb, _device: False)
    monkeypatch.setattr(live.subprocess, "run", run)

    live._restore_fixture_app(ADB, DEVICE, tmp_path, was_installed=True)

    assert "reinstall FAILED: " in observed.read_text()


def test_a_reinstall_that_works_is_observed(monkeypatch, tmp_path, observed):
    installs: list = []

    def run(cmd, **_kwargs):
        installs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(live, "_installed", lambda _adb, _device: False)
    monkeypatch.setattr(live.subprocess, "run", run)

    live._restore_fixture_app(ADB, DEVICE, tmp_path, was_installed=True)

    ((cmd),) = installs
    assert cmd[:4] == [ADB, "-s", DEVICE, "install"]
    assert "reinstalled" in observed.read_text()


@pytest.mark.parametrize("still_installed", [True, None], ids=["still-there", "never-was"])
def test_nothing_to_restore_does_nothing(still_installed, monkeypatch, tmp_path, observed):
    monkeypatch.setattr(live, "_installed", lambda _adb, _device: bool(still_installed))
    calls = _never_installs(monkeypatch)

    live._restore_fixture_app(ADB, DEVICE, tmp_path, was_installed=still_installed is not None)

    assert calls == [] and not observed.exists()


def test_a_report_that_cannot_be_written_falls_back_to_the_print(monkeypatch, tmp_path, capsys):
    # A directory where the report file should be: open() raises IsADirectoryError.
    monkeypatch.setattr(live, "REPORT", str(tmp_path))

    live._observe("passing run: observed")

    out = capsys.readouterr().out
    assert "passing run: observed" in out
    assert "not written to" in out


def _stdout_gone(monkeypatch, how: str) -> None:
    """Take stdout away the two ways a pytest or CI runner can: a broken pipe, or closed."""
    if how == "broken_pipe":
        import builtins

        def broken_print(*_args, **_kwargs):
            raise BrokenPipeError(32, "Broken pipe")

        monkeypatch.setattr(builtins, "print", broken_print)
    else:
        # print() on a closed file raises ValueError, not OSError.
        closed = io.StringIO()
        closed.close()
        monkeypatch.setattr(sys, "stdout", closed)


@pytest.mark.parametrize("how", ["broken_pipe", "closed"])
def test_a_reinstall_with_stdout_gone_does_not_raise(how, monkeypatch, tmp_path, observed):
    """Stdout gone while reporting a successful reinstall: nothing escapes."""
    monkeypatch.setattr(live, "_installed", lambda _adb, _device: False)
    monkeypatch.setattr(
        live.subprocess, "run", lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    _stdout_gone(monkeypatch, how)

    live._restore_fixture_app(ADB, DEVICE, tmp_path, was_installed=True)

    assert "reinstalled" in observed.read_text()


@pytest.mark.parametrize("how", ["broken_pipe", "closed"])
def test_an_unwritable_report_with_stdout_gone_does_not_raise(how, monkeypatch, tmp_path):
    monkeypatch.setattr(live, "REPORT", str(tmp_path))
    _stdout_gone(monkeypatch, how)

    live._observe("passing run: observed")
