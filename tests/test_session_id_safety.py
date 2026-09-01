"""Session ids and package names crossing into paths and device commands.

Two shared-layer holes of the same class as ones already fixed elsewhere:

1. `SessionStore` joined a caller-supplied session id straight onto its storage
   root — `self.base_dir / session_id` — with no validation. `anr_watcher`
   feeds that id from argv (`--get-details SID`, `--stop SID`), so a traversing
   id reads `<root>/../../x/meta.json`, and the `--stop` path then *writes*
   meta.json and summary.json next to it. That is an arbitrary read and an
   arbitrary write, a step beyond the cache traversal fixed in commit 1e3972b
   which only read and deleted.

   Note `clear()` and `prune_to_aggregate_cap()` are NOT affected: they
   enumerate `base_dir.iterdir()` and use `entry.name`, never a caller string.

2. `device_utils.get_package_info` interpolated a package name into
   `pm dump <pkg>` without `quote_for_device_shell`, the same class as the
   run-as sites fixed in the same commit.
"""

from __future__ import annotations

import pytest

from common import anr_sessions, device_utils

TRAVERSING_IDS = [
    "../escape",
    "../../escape",
    "a/../../escape",
    "/etc/passwd",
    "sub/dir",
    "..",
    ".",
    "",
]


@pytest.fixture
def store(tmp_path) -> anr_sessions.SessionStore:
    return anr_sessions.SessionStore(base_dir=tmp_path / "anr-sessions")


@pytest.fixture
def outsider(tmp_path):
    """A file outside the session root that must never be reachable."""
    victim = tmp_path / "escape"
    victim.mkdir()
    (victim / "meta.json").write_text('{"owned": true}', encoding="utf-8")
    return victim


# ---------------------------------------------------------------------------
# Path construction must reject anything that is not a plain session id.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("session_id", TRAVERSING_IDS)
def test_session_dir_rejects_ids_that_leave_the_root(store, session_id):
    """The join is the primitive every other path helper is built on."""
    with pytest.raises(ValueError):
        store.session_dir(session_id)


@pytest.mark.parametrize("session_id", TRAVERSING_IDS)
def test_load_meta_rejects_traversing_ids(store, outsider, session_id):
    """`--get-details <id>` reaches this directly from argv."""
    with pytest.raises((ValueError, FileNotFoundError)):
        store.load_meta(session_id)


def test_traversing_id_cannot_read_a_file_outside_the_root(store, tmp_path, outsider):
    """The concrete exploit: read a meta.json the store does not own."""
    # ../escape/meta.json relative to <tmp>/anr-sessions is the outsider file.
    with pytest.raises((ValueError, FileNotFoundError)):
        store.load_meta("../escape")


def test_traversing_id_cannot_write_outside_the_root(store, outsider):
    """The `--stop` path writes meta.json and summary.json under the id."""
    from common.anr_pipeline import SummaryBuilder

    summary = SummaryBuilder(
        session_id="../escape", started_at="2026-06-17T14:30:00", duration_ms=1000
    ).build([])

    with pytest.raises((ValueError, FileNotFoundError)):
        store.stop("../escape", summary)

    assert (outsider / "meta.json").read_text(
        encoding="utf-8"
    ) == '{"owned": true}', "a traversing session id overwrote a file outside the session root"


# ---------------------------------------------------------------------------
# Real ids must keep working.
# ---------------------------------------------------------------------------


def test_generated_ids_are_accepted(store):
    """Whatever create() produces must survive validation."""
    meta = store.create({"package": "com.example.app"})
    assert store.session_dir(meta.session_id).is_dir()
    assert store.load_meta(meta.session_id).session_id == meta.session_id


def test_unknown_but_wellformed_id_is_a_normal_miss(store):
    """A plausible id that does not exist is not found, not rejected."""
    with pytest.raises(FileNotFoundError):
        store.load_meta("anr-20260101-000000-abcd")


def test_clear_still_removes_real_sessions(store):
    """clear() enumerates the root, so validation must not break it."""
    store.create({"package": "com.example.app"})
    store.create({"package": "com.example.other"})
    assert store.clear() == 2


# ---------------------------------------------------------------------------
# Package names crossing into the device shell.
# ---------------------------------------------------------------------------


def test_get_package_info_quotes_the_package(monkeypatch):
    """`pm dump <pkg>` is re-parsed by the device shell like any other command."""
    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(device_utils.subprocess, "run", _run)
    device_utils.get_package_info("com.example.app;id", serial="emulator-5554")

    joined = " ".join(captured["cmd"])
    assert (
        " com.example.app;id" not in joined
    ), f"unquoted package name reaches the device shell: {joined}"


def test_get_package_info_is_bounded(monkeypatch):
    """Every adb call must be bounded; an unbounded one wedges the connection."""
    captured = {}

    class _Result:
        stdout = ""
        stderr = ""
        returncode = 0

    def _run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(device_utils.subprocess, "run", _run)
    device_utils.get_package_info("com.example.app")

    assert captured["kwargs"].get("timeout"), "pm dump can hang without a timeout"


# ---------------------------------------------------------------------------
# The CLI must fail readably, not with a traceback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--get-details", "--stop"])
def test_cli_rejects_a_bad_session_id_without_a_traceback(tmp_path, monkeypatch, flag):
    """stderr is the agent's retry prompt; a stack trace is not actionable."""
    import subprocess as sp
    import sys
    from pathlib import Path

    scripts = (
        Path(__file__).resolve().parents[1]
        / "android-emulator-skill"
        / "skills"
        / "android-emulator-skill"
        / "scripts"
    )
    env = {
        **dict(__import__("os").environ),
        "ANDROID_EMU_ANR_HOME": str(tmp_path / "anr-sessions"),
    }
    result = sp.run(
        [sys.executable, str(scripts / "anr_watcher.py"), flag, "../../escape"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )

    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, f"unhandled exception reached the user:\n{combined}"
    assert "Invalid session id" in combined
    assert "anr-" in combined, "the error should show what a valid id looks like"
