#!/usr/bin/env python3
"""Derive tests/fixtures/anr_legacy_sessions/: session directories an earlier anr_watcher saved.

Why this exists
---------------
The ANR session store is the skill's own on-disk format, and sessions outlive
the code that wrote them: ``--get-details``, ``--diff`` and ``--stop`` are
asked about a session recorded before an upgrade. A compatibility test needs
what that earlier code actually wrote -- not a meta.json written from memory
of its fields, which is the imagined-output bug class CLAUDE.md is about. So
this script runs an earlier version's ``anr_watcher.py`` (its scripts
directory, extracted from git) end to end and keeps the session directories
it leaves behind.

Its input is recorded, through ``tests/fake_toolchain.py``'s fake adb: the
attached emulator's recorded ``adb devices`` / ``adb get-state`` answers,
``pidof system_server`` as measured with the logcat recording
(``system_server_pid`` in MANIFEST.json), ``cmd package list packages -U``
(package_list_uid), the recorded device-not-found text, and logd's recorded
records (``*_records.bin``). Nothing here is tool output written by hand.

The three sessions, one directory each under ``<out>/<label>/``:

- ``stopped/<id>``: the worker read the recorded broadcast ANR's records
  (logcat_anr_broadcast_records) from a stream that stays open, and
  ``--stop`` stopped it: what that version's --stop left (status
  ``stopped``, its summary saved).
- ``crashed/<id>``: the worker's stream replayed the recorded crash dump
  (logcat_crash_buffer_records), whole, and adb then wrote the recorded
  device-not-found text and exited 255: the worker persisted the device loss
  and marked the session crashed, and ``--stop`` refused (exit 3) after
  saving the summary.
- ``running/<id>``: the session as its worker was leaving it mid-capture
  (the broadcast records read, the stream open), copied before any --stop;
  the worker was then killed. A running session whose worker is gone.

These are written by the skill, not by a device, so they live outside
``tests/fixtures/recorded/``; ``--label`` names the version that wrote them.

``--archive COMMIT`` also keeps that version's worker itself, to be run LIVE
by the tests: ``<out>/<label>/scripts.tar.gz`` is ``git archive
--format=tar.gz COMMIT`` of its ``anr_watcher.py`` and ``common/`` -- the
whole import closure of the worker, byte for byte as committed. Its gzip
header carries no time (mtime 0) and each tar entry the commit's own time,
so this git, archiving this commit, gives the same bytes again (checked
twice); another git or configuration may compress differently, which
changes the file, not what it holds. A
session store outlives an upgrade, and so can a worker still recording into
it: one that takes none of this version's locks (fourth review of PR #44).

Usage (the label names the commit whose scripts are run)::

    git archive fb5de9c android-emulator-skill/skills/android-emulator-skill/scripts \\
        | tar -x -C /tmp/main-fb5de9c
    python tests/derive_legacy_anr_sessions.py \\
        --scripts /tmp/main-fb5de9c/android-emulator-skill/skills/android-emulator-skill/scripts \\
        --label main-fb5de9c --archive fb5de9c
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from fake_toolchain import FakeToolchain, Rule  # noqa: E402

RECORDED = TESTS / "fixtures" / "recorded" / "emulator-api35"
OUT = TESTS / "fixtures" / "anr_legacy_sessions"
WAIT_SECONDS = 30


def _recorded(name: str) -> str:
    return (RECORDED / f"{name}.txt").read_text(encoding="utf-8")


def _system_server(name: str) -> str:
    manifest = json.loads((RECORDED / "MANIFEST.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["fixtures"] if e["name"] == name)
    return str(entry["system_server_pid"])


def _toolchain(root: Path, logcat: Rule) -> FakeToolchain:
    toolchain = FakeToolchain(root)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=_recorded("adb_devices_single")),
            Rule(("get-state",), stdout=_recorded("adb_get_state")),
            logcat,
            Rule(
                ("pidof", "system_server"),
                stdout=_system_server("logcat_anr_broadcast_records") + "\n",
            ),
            Rule(("cmd", "package"), stdout=_recorded("package_list_uid")),
        ],
    )
    return toolchain


def _session_dir(toolchain: FakeToolchain) -> Path:
    (session,) = (toolchain.home / "anr-sessions").iterdir()
    return session


def _meta(session: Path) -> dict:
    return json.loads((session / "meta.json").read_text(encoding="utf-8"))


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            raise SystemExit(f"never: {what}")
        time.sleep(0.05)


def _start(toolchain: FakeToolchain, script: Path) -> Path:
    started = toolchain.run(script, "--start", "--serial", "emulator-5554", "--json")
    if started.returncode != 0:
        raise SystemExit(f"--start exited {started.returncode}: {started.stdout}{started.stderr}")
    return _session_dir(toolchain)


def _events(session: Path) -> int:
    return sum(1 for line in (session / "events.jsonl").read_text().splitlines() if line.strip())


def _kill_worker(session: Path) -> None:
    """End the session's worker and its process group: its `adb logcat` stub holds on."""
    pid = _meta(session).get("pid")
    if pid:
        # --start setsid()s the worker, so its group is its own and the stub's.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def _keep(session: Path, out: Path) -> None:
    target = out / session.name
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(session, target, ignore=shutil.ignore_patterns("*.tmp", "lock"))
    print(f"kept {target.relative_to(TESTS.parent)}")


def derive(scripts: Path, out: Path) -> None:
    script = scripts / "anr_watcher.py"
    broadcast = (RECORDED / "logcat_anr_broadcast_records.bin").read_bytes()
    crash = (RECORDED / "logcat_crash_buffer_records.bin").read_bytes()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)

        toolchain = _toolchain(root / "stopped", Rule(("logcat",), stdout=broadcast, follows=True))
        session = _start(toolchain, script)
        try:
            _until(lambda: _events(session) > 0, "the worker wrote the broadcast ANR")
            stopped = toolchain.run(script, "--stop", session.name, "--json")
        finally:
            _kill_worker(session)
        print(f"--stop exited {stopped.returncode}")
        _keep(session, out / "stopped")

        toolchain = _toolchain(
            root / "crashed",
            Rule(
                ("logcat",),
                stdout=crash,
                stderr=_recorded("adb_device_not_found"),
                exit=255,
            ),
        )
        session = _start(toolchain, script)
        try:
            _until(lambda: _meta(session)["status"] == "crashed", "the worker marked it crashed")
            refused = toolchain.run(script, "--stop", session.name, "--json")
        finally:
            _kill_worker(session)
        print(f"--stop exited {refused.returncode}")
        _keep(session, out / "crashed")

        toolchain = _toolchain(root / "running", Rule(("logcat",), stdout=broadcast, follows=True))
        session = _start(toolchain, script)
        try:
            _until(lambda: _events(session) > 0, "the worker wrote the broadcast ANR")
            _keep(session, out / "running")
        finally:
            _kill_worker(session)


SCRIPTS = "android-emulator-skill/skills/android-emulator-skill/scripts"


def archive(commit: str, out: Path) -> None:
    """Keep ``commit``'s worker: its anr_watcher.py and common/, as ``git archive`` gives them."""
    out.mkdir(parents=True, exist_ok=True)
    target = out / "scripts.tar.gz"
    subprocess.run(
        [
            "git",
            "-C",
            str(TESTS.parent),
            "archive",
            "--format=tar.gz",
            "-o",
            str(target),
            commit,
            f"{SCRIPTS}/anr_watcher.py",
            f"{SCRIPTS}/common",
        ],
        check=True,
        timeout=60,
    )
    print(f"kept {target.relative_to(TESTS.parent)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--scripts", type=Path, help="the version's scripts/ dir: derive sessions")
    parser.add_argument("--label", required=True, help="names the version, e.g. main-fb5de9c")
    parser.add_argument("--archive", metavar="COMMIT", help="keep COMMIT's worker code too")
    args = parser.parse_args()
    if not (args.scripts or args.archive):
        parser.error("pass --scripts, --archive, or both")
    if args.archive:
        archive(args.archive, OUT / args.label)
    if args.scripts:
        derive(args.scripts.resolve(), OUT / args.label)


if __name__ == "__main__":
    main()
