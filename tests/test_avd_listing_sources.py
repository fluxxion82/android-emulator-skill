"""The five AVD-listing sources, checked against one recorded host.

Five scripts enumerate "the AVDs" and they do not all ask the same question:

    device_list.py       `emulator -list-avds`
    emulator_boot.py     `emulator -list-avds`
    emulator_selector.py `emulator -list-avds`
    emulator_delete.py   `avdmanager list avd -c`
    emulator_erase.py    scans $ANDROID_AVD_HOME for `*.avd` directories

On the host these fixtures were recorded from, every source holds exactly one
AVD -- but three of the five returned nothing until the emulator binary was
resolved explicitly, because the bare name `emulator` hit the <sdk>/emulator
*directory* on a PATH containing the SDK root. Nothing in the corpus could have
shown that: `emulator -list-avds` had no fixture at all, which is why
recording it was part of the fix.

Expectations are derived from the recorded emulator output rather than
hard-coded, so re-recording on a machine with different AVDs still exercises
the invariant that matters: every source names the same set.
"""

from __future__ import annotations

import device_list
import emulator_boot
import emulator_delete
import emulator_erase
import emulator_selector
import pytest

from common import sdk_tools


class _Result:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.fixture
def avd_names(recorded) -> list[str]:
    """The AVD names the recorded host actually holds."""
    return [line.strip() for line in recorded.lines("emulator_list_avds") if line.strip()]


def _sdk_tools(monkeypatch, recorded):
    """Answer every SDK tool at the one boundary they now share.

    X3 moved the "ran and failed is not an empty list" policy into
    ``common.sdk_tools.run_sdk_tool``, so the subprocess call these three
    scripts make is no longer in the scripts. One fake, dispatching on the
    argv, replaces the per-module fakes -- which also removes a hazard the old
    shape had: the agreement test installs the emulator source and the
    avdmanager source together, and two fakes on one module would have meant
    the second silently replacing the first.
    """

    def fake_run(cmd, **_kwargs):
        if "-list-avds" in cmd:
            return _Result(recorded.text("emulator_list_avds"))
        if "avd" in cmd and "-c" in cmd:
            return _Result(recorded.text("avdmanager_list_avd_compact"))
        return _Result("", returncode=1)

    monkeypatch.setattr(sdk_tools.subprocess, "run", fake_run)


def _emulator_source(monkeypatch, module, recorded):
    """Point ``module`` at a resolved emulator that emits the recorded output."""
    monkeypatch.setattr(module, "get_emulator_path", lambda: "/sdk/emulator/emulator")
    _sdk_tools(monkeypatch, recorded)


def _avdmanager_source(monkeypatch, recorded):
    monkeypatch.setattr(
        emulator_delete.EmulatorDeleter,
        "get_avdmanager_path",
        lambda _self: "/sdk/cmdline-tools/latest/bin/avdmanager",
    )
    _sdk_tools(monkeypatch, recorded)


def _avd_home(monkeypatch, tmp_path, names):
    """Rebuild $ANDROID_AVD_HOME as the recorded host has it.

    Including the `gradle-managed` sibling, which sits next to the AVDs, is a
    directory, and carries no `.avd` suffix -- so it must not be listed.
    """
    avd_home = tmp_path / "avd"
    avd_home.mkdir()
    for name in names:
        (avd_home / f"{name}.avd").mkdir()
        (avd_home / f"{name}.ini").write_text("path=/somewhere\n")
    (avd_home / "gradle-managed").mkdir()
    monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))


# ---------------------------------------------------------------------------
# `emulator -list-avds` consumers
# ---------------------------------------------------------------------------
def test_device_list_reads_recorded_emulator_output(monkeypatch, recorded, avd_names):
    _emulator_source(monkeypatch, device_list, recorded)
    monkeypatch.setattr(device_list.DeviceLister, "get_devices", lambda _self: [])

    assert [a["name"] for a in device_list.DeviceLister().get_avds()] == avd_names


def test_emulator_boot_reads_recorded_emulator_output(monkeypatch, recorded, avd_names):
    _emulator_source(monkeypatch, emulator_boot, recorded)
    assert [a["name"] for a in emulator_boot.list_avds()] == avd_names


def test_emulator_selector_reads_recorded_emulator_output(
    monkeypatch, tmp_path, recorded, avd_names
):
    _emulator_source(monkeypatch, emulator_selector, recorded)
    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")
    assert selector._list_avd_names() == avd_names


# ---------------------------------------------------------------------------
# `avdmanager list avd -c` consumer
# ---------------------------------------------------------------------------
def test_emulator_delete_reads_recorded_avdmanager_output(monkeypatch, recorded, avd_names):
    _avdmanager_source(monkeypatch, recorded)
    assert emulator_delete.EmulatorDeleter().list_avds() == avd_names


# ---------------------------------------------------------------------------
# $ANDROID_AVD_HOME filesystem scan
# ---------------------------------------------------------------------------
def test_emulator_erase_reads_recorded_avd_home_layout(monkeypatch, tmp_path, avd_names):
    _avd_home(monkeypatch, tmp_path, avd_names)
    assert sorted(emulator_erase.EmulatorEraser().list_avds()) == sorted(avd_names)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def test_all_sources_agree_on_the_recorded_host(monkeypatch, tmp_path, recorded, avd_names):
    """One host: every source must name the same AVDs."""
    _emulator_source(monkeypatch, device_list, recorded)
    _emulator_source(monkeypatch, emulator_boot, recorded)
    _emulator_source(monkeypatch, emulator_selector, recorded)
    monkeypatch.setattr(device_list.DeviceLister, "get_devices", lambda _self: [])
    _avdmanager_source(monkeypatch, recorded)
    _avd_home(monkeypatch, tmp_path, avd_names)

    selector = emulator_selector.EmulatorSelector(config_path=tmp_path / "config.json")

    by_source = {
        "device_list (emulator -list-avds)": [
            a["name"] for a in device_list.DeviceLister().get_avds()
        ],
        "emulator_boot (emulator -list-avds)": [a["name"] for a in emulator_boot.list_avds()],
        "emulator_selector (emulator -list-avds)": selector._list_avd_names(),
        "emulator_delete (avdmanager list avd -c)": emulator_delete.EmulatorDeleter().list_avds(),
        "emulator_erase ($ANDROID_AVD_HOME scan)": sorted(
            emulator_erase.EmulatorEraser().list_avds()
        ),
    }

    assert avd_names, "the recorded host holds no AVDs; nothing is being compared"
    assert all(sorted(names) == sorted(avd_names) for names in by_source.values()), by_source
