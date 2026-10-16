"""The throwaway-AVD lifecycle cannot reach the user's own AVDs.

`tests/record_fixtures.py` records avdmanager create / config.ini / delete by
making a real AVD and deleting it again. Every avdmanager call it makes runs
with ANDROID_AVD_HOME pointed at a directory created for that run, so a create
that fails half-way, a delete by name or a race with another creator all happen
somewhere nobody else uses. These tests hold it to that with a fake avdmanager
that does what the real one was measured doing under ANDROID_AVD_HOME: writes
`<name>.ini` and `<name>.avd/` into that directory, and lists only what is in it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import record_fixtures as rf

CONFIG = "abi.type=arm64-v8a\nhw.device.name=pixel_9\nimage.sysdir.1=x/\n"


class FakeAvdmanager:
    """Answers the lifecycle's calls the way avdmanager does under ANDROID_AVD_HOME."""

    def __init__(self, *, create_fails_half_way: bool = False) -> None:
        self.homes: list[str | None] = []
        self.create_fails_half_way = create_fails_half_way

    def run_host(self, argv, timeout=rf.DEFAULT_TIMEOUT, *, stdin=None, env=None):
        home_name = (env or {}).get("ANDROID_AVD_HOME")
        self.homes.append(home_name)
        if home_name is None:
            pytest.fail(f"avdmanager ran without a private ANDROID_AVD_HOME: {argv}")
        home = Path(home_name)
        verb, name = argv[1], argv[-1] if argv[1] == "delete" else argv[argv.index("--name") + 1]
        if verb == "create":
            (home / f"{name}.avd").mkdir()
            (home / f"{name}.ini").write_text(f"path={home}/{name}.avd\n")
            if self.create_fails_half_way:
                return rf.Capture("", "Error: half-way", 1)
            (home / f"{name}.avd" / "config.ini").write_text(CONFIG)
            # The real create prints no path (measured). This one prints the
            # private home in both spellings macOS has for it, because what is
            # under test is the scrub that would catch it if a release did.
            return rf.Capture(f"{home}\n{home.resolve()}\n", "", 0)
        if (home / f"{name}.avd").exists():
            (home / f"{name}.ini").unlink()
            for child in (home / f"{name}.avd").iterdir():
                child.unlink()
            (home / f"{name}.avd").rmdir()
            return rf.Capture(f"\nAVD '{name}' deleted.", "", 0)
        return rf.Capture("", f"Error: There is no Android Virtual Device named '{name}'.\n", 1)

    def list_avds(self, cmd, **kwargs):
        home_name = kwargs.get("env", {}).get("ANDROID_AVD_HOME")
        self.homes.append(home_name)
        names = sorted(p.stem for p in Path(home_name).glob("*.ini")) if home_name else []
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(names), stderr="")


@pytest.fixture
def user_home(monkeypatch, tmp_path) -> Path:
    """The user's AVD home, holding an AVD of theirs the lifecycle must never touch."""
    home = tmp_path / "user_avd"
    (home / "Pixel_9.avd").mkdir(parents=True)
    (home / "Pixel_9.ini").write_text("path=Pixel_9.avd\n")
    monkeypatch.setattr(rf, "_avd_home", lambda: home)
    monkeypatch.setattr(rf, "_sdk_tool", lambda name: f"/fake/{name}")
    return home


def _install(monkeypatch, fake: FakeAvdmanager) -> None:
    monkeypatch.setattr(rf, "_run_host", fake.run_host)
    monkeypatch.setattr(rf.subprocess, "run", fake.list_avds)


def test_every_avdmanager_call_runs_in_one_private_home_that_is_removed(monkeypatch, user_home):
    fake = FakeAvdmanager()
    _install(monkeypatch, fake)
    before = sorted(p.name for p in user_home.iterdir())

    captures = rf._ThrowawayAvd()._run_once()

    assert set(captures) == {"avdmanager_create", "avdmanager_delete", "avdmanager_delete_missing"}
    homes = set(fake.homes)
    assert len(homes) == 1, f"the calls used more than one AVD home: {homes}"
    private = Path(homes.pop())
    assert private != user_home and user_home not in private.parents
    assert not private.exists(), "the private AVD home was left behind"
    assert sorted(p.name for p in user_home.iterdir()) == before


def test_a_create_that_fails_half_way_is_cleaned_up_in_the_private_home(monkeypatch, user_home):
    """The window the review found: `finally` deletes whatever is there by name.

    In the private home only this run can have put anything there, so the
    delete removes the half-made AVD and nothing of anyone else's.
    """
    fake = FakeAvdmanager(create_fails_half_way=True)
    _install(monkeypatch, fake)
    before = sorted(p.name for p in user_home.iterdir())

    with pytest.raises(rf.CaptureError, match="create exited 1"):
        rf._ThrowawayAvd()._run_once()

    private = Path(fake.homes[0])
    assert len(fake.homes) == 2, "the half-made AVD was not deleted"
    assert not private.exists()
    assert sorted(p.name for p in user_home.iterdir()) == before


def test_the_users_own_avd_by_a_throwaway_name_stops_the_run_before_any_call(
    monkeypatch, user_home
):
    """Belt and braces: the private home makes it unreachable, and it is refused anyway."""
    fake = FakeAvdmanager()
    _install(monkeypatch, fake)
    (user_home / f"{rf.THROWAWAY_AVD}.ini").write_text("path=x\n")

    with pytest.raises(rf.CaptureError, match="already exists"):
        rf._ThrowawayAvd()._run_once()
    assert fake.homes == []


def test_the_private_home_never_reaches_a_recorded_byte(monkeypatch, user_home):
    """avdmanager's `.ini` names the private home; nothing recorded may."""
    fake = FakeAvdmanager()
    _install(monkeypatch, fake)

    avd = rf._ThrowawayAvd()
    captures = avd._run_once()
    recorded = "".join(c.stdout + c.stderr for c in captures.values())
    recorded += avd.config_ini(None, {}).decode()

    assert rf._PRIVATE_AVD_HOME_PREFIX not in recorded
