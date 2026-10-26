"""anr_watcher's live readers on API 33: dumps from a cursor, never a stream it stops.

Android 13's logcat writes ``-B`` records through a 4 KiB stdio buffer and
flushes only after a TEXT record, so a live ``logcat -B -T 1`` stopped by its
reader loses what it held back (logcat_stream_*). ``anr_watcher --watch`` and
the session worker both streamed and stopped that way. Measured on the API 33
emulator: a Choreographer jank logged 2 s into a 4.5 s session ended
``event_count: 0``, ``total_lines: 0``, ``incomplete: false``, and ``--watch
--duration 6`` showed nothing at exit 0. Recorded here with a real jank
(``logcat_stream_jank_records``): the fixture app cold-started inside a live
stream until Choreographer skipped 30 frames, the stream stopped just after --
it withheld the jank (manifest ``jank.delivered_by_the_stream: false``), and
the window dumped back holds it.

On that level both readers now read as log_monitor does (``common.dump_read``):
the newest record a system process wrote is the cursor, each poll dumps a
count-based tail of logd's order until it reaches it, and the read ends with
one more poll -- at ``--duration``, at Ctrl-C, at ``--stop``. The session keeps
its cursor in its meta, tied to the device's serial and boot id, and a
restarted read resumes from it.

The fake device answers as the API 33 emulator did in the recorded run: the
open record for ``-d -B -T 1``, the window's last N records for ``-T N`` (as
logd answers, logcat_tail_records), and, for a stream, the bytes the recorded
stream delivered before it was stopped.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import anr_watcher
import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from fake_toolchain import SCRIPTS, FakeToolchain, Frame, Rule, framed, frames, uid_rules
from test_anr_watcher import _Answer, _LiveDevice
from test_log_monitor_dump_read import (
    PLACEHOLDER,
    Scenario,
    _anchor_calls,
    _marker,
    _opening,
    _scenario,
)

from common import dump_read, logcat
from common.anr_sessions import Identity, SessionStore, worker_identity

API33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")
API35 = RecordedFixtures(RECORDED_ROOT / "emulator-api35")
JANK = "logcat_stream_jank"
PACKAGE = "com.example.composefixture"
# The serial the recorded `adb devices` lists; the fake answers for it as the
# API 33 emulator did.
SERIAL = "emulator-5554"
# A poll interval no run reaches: the only reads are the opening and the last.
NO_POLLS = {"ANDROID_EMU_LOG_POLL_SECONDS": "600"}


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    yield chain
    _kill_workers(chain)


def _kill_workers(toolchain: FakeToolchain) -> None:
    """End a worker a spec left running -- only a pid still shown to be that session's worker."""
    store = SessionStore(base_dir=toolchain.home / "anr-sessions")
    for meta in store.list_sessions():
        if meta.pid and worker_identity(meta)[0] is Identity.WORKER:
            with contextlib.suppress(ProcessLookupError):
                os.kill(meta.pid, signal.SIGKILL)


def _jank() -> Scenario:
    return _scenario(API33, JANK)


def _jank_frame(scenario: Scenario) -> Frame:
    """The recorded jank: the app's own Choreographer skip, by its uid."""
    entry = scenario.entry["jank"]
    (jank,) = [
        f
        for f in frames(scenario.window)
        if f.tag == "Choreographer" and f.uid == entry["uid"] and f.pid == str(entry["pid"])
    ]
    return jank


def _device(
    toolchain: FakeToolchain,
    recorded,
    scenario: Scenario,
    *,
    window: bytes | None = None,
    dumps: tuple[Rule, ...] = (),
    opening: tuple[Rule, ...] | None = None,
    level: str | None = None,
    early: tuple[Rule, ...] = (),
    boot: tuple[Rule, ...] = (),
    state: tuple[Rule, ...] | None = None,
    system_server: str | None = None,
    listing: str | None = None,
    extra: tuple[Rule, ...] = (),
) -> None:
    """The API 33 emulator as it answered in ``scenario``'s recorded run.

    Its level (getprop_sdk), its boot id (boot_id), system_server's pid and the
    fixture app's uid listing as measured in the run; ``-d -B -T 1`` with the
    open record (or the ``opening`` rules); ``-d -B -T N`` with the last N
    records of the window (``dumps`` rules first); and a live stream replaying
    what the recorded stream delivered before it was stopped, then waiting.
    ``level``, ``early`` (rules before every lookup's), ``boot`` (rules first),
    ``state`` (``get-state``'s rules),
    ``system_server`` and ``listing`` replace those answers.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *(state or (Rule(("get-state",), stdout=recorded.text("adb_get_state")),)),
            *early,
            Rule(
                ("shell", "getprop", "ro.build.version.sdk"),
                stdout=API33.text("getprop_sdk") if level is None else level,
            ),
            *boot,
            Rule(("shell", "cat", dump_read.BOOT_ID_PATH), stdout=API33.text("boot_id")),
            Rule(
                ("pidof", "system_server"),
                stdout=(system_server or scenario.entry["system_server_pid"]) + "\n",
            ),
            Rule(
                ("cmd", "package"),
                stdout=listing or scenario.entry["app"]["package_uid_listing"]["output"],
            ),
            *extra,
            *(
                (Rule(("logcat", "-d", "-T", "1"), stdout=scenario.opened),)
                if opening is None
                else opening
            ),
            *dumps,
            Rule(
                ("logcat", "-d", "-T"),
                buffered=scenario.window if window is None else window,
            ),
            Rule(("logcat",), stdout=scenario.stream, follows=True),
        ],
    )


def _watch(toolchain: FakeToolchain, *argv: str, **env: str) -> subprocess.CompletedProcess:
    return toolchain.run(
        "anr_watcher.py", "--watch", "--serial", SERIAL, "--json", *argv, timeout=60, **env
    )


def _events(result: subprocess.CompletedProcess) -> list[dict]:
    """The events a --watch printed, one JSON object a line (a refusal is not one)."""
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return [line for line in lines if "kind" in line]


def _start(toolchain: FakeToolchain, *argv: str, **env: str) -> subprocess.CompletedProcess:
    return toolchain.run("anr_watcher.py", "--start", "--serial", SERIAL, "--json", *argv, **env)


def _session_id(started: subprocess.CompletedProcess) -> str:
    assert started.returncode == 0, started.stdout + started.stderr
    return json.loads(started.stdout)["session_id"]


def _stop(toolchain: FakeToolchain, session_id: str) -> subprocess.CompletedProcess:
    return toolchain.run("anr_watcher.py", "--stop", session_id, "--json", "--all")


def _store(toolchain: FakeToolchain) -> SessionStore:
    return SessionStore(base_dir=toolchain.home / "anr-sessions")


def _wait_for(check, seconds: float = 20) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def _janks(summary: dict) -> list[tuple]:
    """``(frames, uid, app, count)`` of each jank cluster of a summary."""
    return [
        (
            cluster["sample_event"]["frames"],
            cluster["sample_event"]["uid"],
            cluster["sample_event"]["app"],
            cluster["count"],
        )
        for cluster in summary["clusters"]
        if cluster["sample_event"]["kind"] == "jank"
    ]


def _expected_jank(scenario: Scenario) -> tuple:
    entry = scenario.entry["jank"]
    return (entry["frames"], entry["uid"], PACKAGE, 1)


def _logcat_calls(toolchain: FakeToolchain) -> list[list[str]]:
    return [call for call in toolchain.calls("adb") if "logcat" in call]


def _streams(toolchain: FakeToolchain) -> list[list[str]]:
    """Every logcat the script left following: no ``-d``."""
    return [call for call in _logcat_calls(toolchain) if "-d" not in call]


# --- the recording: what a stream stopped then delivered, and what logd held ----------


def test_the_recorded_stream_withheld_the_jank_and_the_window_holds_it():
    """The ground truth every spec here stands on, checked off the bytes.

    The stream delivered whole 4 KiB blocks only (manifest
    ``chunks_at_s_and_bytes``), and the jank was not in them; the window
    dumped back holds it after the open record, and holds the `Start proc`
    that names its pid as the fixture app.
    """
    scenario = _jank()
    entry = scenario.entry
    assert {size for _at, size in entry["stream"]["chunks_at_s_and_bytes"]} == {4096}
    assert entry["jank"]["delivered_by_the_stream"] is False
    assert entry["jank"]["frames"] >= anr_watcher.DEFAULT_MIN_FRAMES
    jank = _jank_frame(scenario)
    assert jank.raw not in {f.raw for f in frames(scenario.stream)}
    assert jank.raw in {f.raw for f in scenario.after}
    pid = entry["jank"]["pid"]
    assert any(f.message.startswith(f"Start proc {pid}:{PACKAGE}/") for f in scenario.after)


# --- red: the measured case, and a reader stopped just after the event -----------------


def test_a_jank_inside_a_short_session_on_api_33_is_in_its_summary(toolchain, recorded):
    """The measured case: a session on API 33 with the app's jank in it.

    On main the worker streamed: the recorded stream's bytes never held the
    jank, so --stop's summary said ``event_count: 0``, ``incomplete: false``
    -- a complete-looking empty summary. Now the worker's polls read it while
    the session runs (it is in the events file before --stop is asked), named
    by the window's own `Start proc`, and the summary holds it once.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    session_id = _session_id(_start(toolchain))
    _wait_for(lambda: _store(toolchain).read_events(session_id))
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert _janks(summary) == [_expected_jank(scenario)], summary
    assert summary["event_count"] == 1, summary
    assert summary["incomplete"] is False, summary
    assert summary["total_lines"] == len(scenario.after), summary
    # Read by dumps, it says whether it wrote a marker line: it did not.
    assert summary["anchor_written"] is False, summary
    assert _streams(toolchain) == [], "a session on API 33 started a stream"


def test_a_watch_on_api_33_reads_its_tail_at_the_deadline(toolchain, recorded):
    """--watch --duration on API 33, polled only at its end: the jank is printed.

    No poll falls inside the watch, so the opening and the read at the
    deadline are the only reads: what was logged after the last read before
    the deadline -- here, everything -- is read then. On main the watch
    streamed, and the recorded stream never delivered the jank: zero jank
    events at exit 0.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    result = _watch(toolchain, "--duration", "2", **NO_POLLS)

    assert result.returncode == 0, result.stdout + result.stderr
    jank = [(e["frames"], e["uid"]) for e in _events(result) if e["kind"] == "jank"]
    entry = scenario.entry["jank"]
    assert jank == [(entry["frames"], entry["uid"])], result.stdout
    assert not any(e.get("ambiguous_tail") for e in _events(result)), result.stdout
    assert _streams(toolchain) == [], "a watch on API 33 started a stream"


def test_a_watch_on_api_33_prints_each_polls_records(toolchain, recorded):
    """Polled every second, the jank is printed by a poll, through the same pipeline as a stream's."""
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    result = _watch(toolchain, "--duration", "3")

    assert result.returncode == 0, result.stdout + result.stderr
    entry = scenario.entry["jank"]
    jank = [(e["frames"], e["uid"]) for e in _events(result) if e["kind"] == "jank"]
    assert jank == [(entry["frames"], entry["uid"])], result.stdout
    dumps = [call for call in _logcat_calls(toolchain) if "-d" in call]
    assert len(dumps) >= 3, f"one opening and a read a second: {dumps}"


def test_the_session_reads_its_tail_at_stop(toolchain, recorded):
    """--stop's SIGTERM: one more read before the worker publishes ``stopped``.

    No poll falls inside the session, so the read at --stop is the only one
    after the opening: the jank logged before --stop is in the summary.
    Without that last read the worker would end on what its last poll read.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    session_id = _session_id(_start(toolchain, **NO_POLLS))
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert _janks(summary) == [_expected_jank(scenario)], summary
    assert summary["incomplete"] is False, summary


# --- the cursor: lost, kept, resumed -----------------------------------------------------


def test_a_session_whose_cursor_is_lost_ends_incomplete_with_the_reason(toolchain, recorded):
    """The buffer turned over past the session's cursor: the session ends, incomplete, and says why.

    Derived: the window without its open record -- logd's ring has moved past
    it. The worker refuses to continue from a later point, ends the session
    ``crashed`` with the reason, and --stop saves the summary incomplete
    (``--diff`` concludes nothing from it) and refuses, exit 1.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario, window=framed(scenario.after))

    session_id = _session_id(_start(toolchain))
    assert _wait_for(lambda: _store(toolchain).load_meta(session_id).status == "crashed")
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 1, stopped.stdout + stopped.stderr
    refusal = json.loads(stopped.stdout.splitlines()[-1])
    assert "lost its place in logd's buffer" in refusal["error"], refusal
    assert "adb logcat -G" in refusal["remedy"], refusal
    summary = _store(toolchain).load_summary(session_id)
    assert summary.incomplete is True
    assert "lost its place in logd's buffer" in summary.incomplete_reason


def test_the_session_keeps_its_cursor_in_its_meta_tied_to_serial_and_boot(toolchain, recorded):
    """The checkpoint in meta: the cursor's frame and suffix, base64, with the serial and boot id."""
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    session_id = _session_id(_start(toolchain))
    _wait_for(lambda: _store(toolchain).read_events(session_id))
    stopped = _stop(toolchain, session_id)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr

    checkpoint = _store(toolchain).load_meta(session_id).counters["cursor"]
    assert checkpoint["serial"] == SERIAL
    assert checkpoint["boot_id"] == API33.text("boot_id").strip()
    frame = base64.b64decode(checkpoint["frame"])
    suffix = [base64.b64decode(item) for item in checkpoint["suffix"]]
    held = [f.raw for f in frames(scenario.window)]
    # The newest system record of the window, and the frames after it.
    newest = max(i for i, f in enumerate(frames(scenario.window)) if f.uid % 100_000 < 10_000)
    assert frame == held[newest]
    assert suffix == held[newest + 1 :]


def _dump_fails_once() -> Rule:
    """A tail that exits 1 once, saying nothing, the device still there."""
    return Rule(("logcat", "-d", "-T"), exit=1, once=True)


def test_a_restarted_read_resumes_from_its_checkpoint_without_duplicates(toolchain, recorded):
    """A dump fails with the device there; the read restarts from the checkpoint its meta holds.

    Derived: the first poll's tail holds the window's first records only
    (logd's buffer then), the second poll's dump exits 1, and every later
    tail is the whole window. The restarted read resumes from the cursor the
    meta holds -- written as the worker decided to restart -- so it reads on
    from the records already read: every record of the window once
    (``total_lines``), the jank once, and the summary complete. A fresh
    opening would have placed the read after the jank (the newest record by
    then is the window's last): the second ``-T 1`` answers that.
    """
    scenario = _jank()
    early = scenario.after[:20]
    assert _jank_frame(scenario) not in early
    _device(
        toolchain,
        recorded,
        scenario,
        opening=(
            Rule(("logcat", "-d", "-T", "1"), stdout=scenario.opened, once=True),
            # Asked again, the newest record is the whole window's last.
            Rule(("logcat", "-d", "-T", "1"), stdout=frames(scenario.window)[-1].raw),
        ),
        dumps=(
            Rule(("logcat", "-d", "-T"), buffered=framed([scenario.anchor, *early]), once=True),
            _dump_fails_once(),
        ),
    )

    session_id = _session_id(_start(toolchain))
    store = _store(toolchain)
    assert _wait_for(lambda: store.read_events(session_id)), "the restarted read read no jank"
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert summary["total_lines"] == len(scenario.after), summary
    assert _janks(summary) == [_expected_jank(scenario)], summary
    assert summary["incomplete"] is False, summary
    notes = [
        json.loads(line)
        for line in store.read_raw_events(session_id).splitlines()
        if '"event"' in line
    ]
    assert [n["event"] for n in notes] == ["dump_restart", "stream_ended"], notes
    assert store.load_meta(session_id).counters["dump_restarts"] == 1


def test_a_restarted_read_on_another_boot_ends_the_session_incomplete(toolchain, recorded):
    """The device rebooted while the read was down: its checkpoint is another boot's, refused.

    Derived: a dump fails once, and the boot id asked again is another boot's
    -- the API 35 emulator's recorded one. The session ends ``crashed``,
    incomplete, saying the device rebooted, rather than looking for its place
    in another boot's log.
    """
    scenario = _jank()
    _device(
        toolchain,
        recorded,
        scenario,
        dumps=(_dump_fails_once(),),
        boot=(
            Rule(("shell", "cat", dump_read.BOOT_ID_PATH), stdout=API33.text("boot_id"), once=True),
            Rule(("shell", "cat", dump_read.BOOT_ID_PATH), stdout=API35.text("boot_id")),
        ),
    )

    session_id = _session_id(_start(toolchain))
    assert _wait_for(lambda: _store(toolchain).load_meta(session_id).status == "crashed")
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 1, stopped.stdout + stopped.stderr
    refusal = json.loads(stopped.stdout.splitlines()[-1])
    assert "could not resume" in refusal["error"], refusal
    assert "rebooted" in refusal["error"], refusal
    assert _store(toolchain).load_summary(session_id).incomplete is True


def test_a_session_whose_device_goes_mid_read_is_exit_three(toolchain, recorded):
    """`get-state` after a poll's dump says the device went: --stop raises it, exit 3.

    Attached for --start's check and the opening's dump, then recorded as not
    found (adb_device_not_found).
    """
    scenario = _jank()
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "adb_device_not_found")
    _device(
        toolchain,
        recorded,
        scenario,
        state=(
            Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
            Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
            Rule(
                ("get-state",),
                stderr=recorded.text("adb_device_not_found"),
                exit=entry["exit_status"],
            ),
        ),
    )

    session_id = _session_id(_start(toolchain))
    assert _wait_for(lambda: _store(toolchain).load_meta(session_id).status == "crashed")
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 3, stopped.stdout + stopped.stderr
    refusal = json.loads(stopped.stdout.splitlines()[-1])
    assert "lost its device" in refusal["error"], refusal


def test_a_watch_that_loses_its_place_is_refused_with_what_it_showed(toolchain, recorded):
    """--watch on API 33 whose cursor turned over: refused, exit 1, with ``partial`` -- never exit 0."""
    scenario = _jank()
    _device(toolchain, recorded, scenario, window=framed(scenario.after))

    result = _watch(toolchain, "--duration", "2")

    assert result.returncode == 1, result.stdout + result.stderr
    refusal = json.loads(result.stdout.splitlines()[-1])
    assert "turned over" in refusal["error"], refusal
    assert refusal["partial"]["summary"]["total"] == 0, refusal


@pytest.mark.parametrize("mode", ["watch", "session"])
def test_an_unknown_level_is_refused_and_never_streams(mode, toolchain, recorded):
    """A device that does not say its level (getprop_unset: an empty line): no stream, no dump.

    --watch is refused (exit 1, ``partial``); a session ends with the reason
    -- --start refuses it, and its saved summary is incomplete.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario, level=API33.text("getprop_unset"))

    if mode == "watch":
        result = _watch(toolchain, "--duration", "2")
        refusal = json.loads(result.stdout.splitlines()[-1])
        assert result.returncode == 1, result.stdout + result.stderr
        assert "partial" in refusal, refusal
    else:
        result = _start(toolchain)
        assert result.returncode == 1, result.stdout + result.stderr
        refusal = json.loads(result.stdout.splitlines()[-1])
        (meta,) = _store(toolchain).list_sessions()
        summary = _store(toolchain).load_summary(meta.session_id)
        assert summary.incomplete is True
        assert "could not start" in summary.incomplete_reason
    assert "no API level" in refusal["error"], refusal
    assert _logcat_calls(toolchain) == []


# --- controls: what main counted, still counted the same way -----------------------------


def _anr_window(scenario: Scenario) -> list[Frame]:
    """API 33's recorded broadcast ANR after a system record to open on: the jank run's open mark.

    Derived: two recordings of the same emulator joined, the open record first.
    """
    return [scenario.anchor, *frames(API33.data("logcat_anr_broadcast_records.bin"))]


def _anr_system_server() -> str:
    entry = next(
        e for e in API33.manifest["fixtures"] if e["name"] == "logcat_anr_broadcast_records"
    )
    return entry["system_server_pid"]


def _stated_pid() -> str:
    report = next(
        f
        for f in frames(API33.data("logcat_anr_broadcast_records.bin"))
        if f.tag == "ActivityManager" and f.message.startswith("ANR in ")
    )
    return report.message.split("\nPID: ", 1)[1].split("\n", 1)[0]


def test_a_verified_anr_is_counted_as_main_counted_it(toolchain, recorded):
    """PR #53's control on API 33: the recorded ANR read by dumps, under --package, is one verified event.

    Its process shown to be the package's as the device shows it (the
    recorded uid listing, and ps_pid_uid for the report's own PID:,
    substituted), as main's stream counted it.
    """
    scenario = _jank()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed(_anr_window(scenario)),
        system_server=_anr_system_server(),
        extra=tuple(uid_rules(API33, PACKAGE, _stated_pid())[1:]),
    )

    result = _watch(toolchain, "--duration", "2", "--package", PACKAGE)

    assert result.returncode == 0, result.stdout + result.stderr
    events = _events(result)
    assert [(e["kind"], e["anr_package"], e["attribution"]) for e in events] == [
        ("anr", PACKAGE, "verified")
    ], events


def test_a_verified_anr_in_a_session_is_one_cluster(toolchain, recorded):
    """The same window through the session worker's dumps: one ANR cluster, the package's."""
    scenario = _jank()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed(_anr_window(scenario)),
        system_server=_anr_system_server(),
        extra=tuple(uid_rules(API33, PACKAGE, _stated_pid())[1:]),
    )

    session_id = _session_id(_start(toolchain, "--package", PACKAGE))
    _wait_for(lambda: _store(toolchain).read_events(session_id))
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    anrs = [c for c in summary["clusters"] if c["sample_event"]["kind"] == "anr"]
    assert [
        (c["sample_event"]["app"], c["sample_event"]["attribution"], c["count"]) for c in anrs
    ] == [(PACKAGE, "verified", 1)], summary


@pytest.mark.parametrize("mode", ["watch", "session"])
def test_a_jank_is_the_packages_by_its_uid_when_no_start_proc_names_it(mode, toolchain, recorded):
    """PR #53's control on API 33: the app already running, its jank is the package's by its uid.

    Derived: the jank window with its `Start proc` removed -- the app started
    before the read -- under --package. The jank's uid is the one the run's
    own listing names: the package's, as main's stream attributed it.
    """
    scenario = _jank()
    window = [f for f in frames(scenario.window) if not f.message.startswith("Start proc ")]
    assert len(window) == len(frames(scenario.window)) - 1
    _device(toolchain, recorded, scenario, window=framed(window))
    entry = scenario.entry["jank"]

    if mode == "watch":
        result = _watch(toolchain, "--duration", "2", "--package", PACKAGE)
        assert result.returncode == 0, result.stdout + result.stderr
        jank = [(e["app"], e["uid"]) for e in _events(result) if e["kind"] == "jank"]
        assert jank == [(PACKAGE, entry["uid"])], result.stdout
        return
    session_id = _session_id(_start(toolchain, "--package", PACKAGE))
    _wait_for(lambda: _store(toolchain).read_events(session_id))
    stopped = _stop(toolchain, session_id)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert _janks(summary) == [_expected_jank(scenario)], summary
    assert summary["uid_attribution"] == "uid", summary


def _restart_entry(recorded) -> dict:
    return next(
        e for e in recorded.manifest["fixtures"] if e["name"] == "logcat_app_restart_records"
    )


@pytest.mark.parametrize("mode", ["watch", "session"])
def test_api_35_streams_as_on_main(mode, toolchain, recorded):
    """Every level but 33 streams, as main did: `logcat -B -T 1`, no dump, no boot id asked.

    The recorded API 35 restart window (three jank records of the fixture app)
    replayed as a stream after one buffered record: the three jank events, as
    main printed and counted them.
    """
    records = frames(recorded.data("logcat_app_restart_records.bin"))
    entry = _restart_entry(recorded)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
            Rule(("pidof", "system_server"), stdout=str(entry["system_server_pid"]) + "\n"),
            Rule(("cmd", "package"), stdout=entry["package_uid_listing"]["output"]),
            Rule(
                ("logcat",),
                buffered=framed(records[:1]),
                stdout=framed(records[1:]),
                follows=True,
            ),
        ],
    )

    if mode == "watch":
        result = _watch(toolchain, "--duration", "2")
        assert result.returncode == 0, result.stdout + result.stderr
        assert len([e for e in _events(result) if e["kind"] == "jank"]) == 3, result.stdout
    else:
        session_id = _session_id(_start(toolchain))
        _wait_for(lambda: len(_store(toolchain).read_events(session_id)) >= 3)
        stopped = _stop(toolchain, session_id)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert json.loads(stopped.stdout)["event_count"] == 3, stopped.stdout
        assert "cursor" not in _store(toolchain).load_meta(session_id).counters
        # A stream writes no marker line, and its summary has nothing to say of one.
        assert "anchor_written" not in json.loads(stopped.stdout), stopped.stdout
    calls = _logcat_calls(toolchain)
    assert calls and all(call[-3:] == ["-B", "-T", "1"] for call in calls), calls
    assert not any(dump_read.BOOT_ID_PATH in call for call in toolchain.calls("adb"))


# --- the pieces, on recorded bytes ---------------------------------------------------------


@pytest.mark.parametrize("profile", [API33, API35], ids=["api33", "api35"])
def test_the_boot_id_reads_as_the_device_wrote_it(profile):
    """boot_id on both levels is a UUID, and two boots' differ; an empty answer is no id."""
    value = dump_read.parse_boot_id(profile.text("boot_id"))
    assert value is not None and len(value) == 36
    assert dump_read.parse_boot_id(API33.text("getprop_unset")) is None
    assert dump_read.parse_boot_id(API33.text("boot_id")) != dump_read.parse_boot_id(
        API35.text("boot_id")
    )


def test_a_checkpoint_round_trips_and_refuses_an_app_frame():
    """``_checkpoint`` / ``_cursor_of``: the frame and suffix byte for byte; an app's frame refused."""
    scenario = _jank()
    cursor = logcat.cursor_from(scenario.window)
    boot = API33.text("boot_id").strip()
    checkpoint = anr_watcher._checkpoint(cursor, SERIAL, boot)

    back, serial, boot_back = anr_watcher._cursor_of(json.loads(json.dumps(checkpoint)))

    assert (back.raw, back.suffix, serial, boot_back) == (cursor.raw, cursor.suffix, SERIAL, boot)
    forged = {**checkpoint, "frame": base64.b64encode(_jank_frame(scenario).raw).decode("ascii")}
    with pytest.raises(ValueError):
        anr_watcher._cursor_of(forged)


def test_a_session_whose_buffer_holds_no_system_record_writes_one_anchor(toolchain, recorded):
    """The buffer at the open holds nothing to open on (as after `logcat -c`): one marker line.

    As log_monitor's read (test_4b): the worker writes ``aes-log-monitor
    anchor-<128-bit hex>`` as shell, finds it by its nonce, and reads what
    follows it -- the recorded jank window. The session's meta keeps that it
    wrote one (``anchor_written``). Derived: a recorded shell record carrying
    the line the read writes (``Rule.logged``).
    """
    scenario = _jank()
    marker = _marker(scenario)
    first, doublings = _opening([marker], logged=PLACEHOLDER)
    _device(
        toolchain,
        recorded,
        scenario,
        opening=(first, *doublings),
        extra=(Rule(("shell", "log")),),
        dumps=(
            Rule(
                ("logcat", "-d", "-T"),
                buffered=framed([marker, *scenario.after]),
                logged=PLACEHOLDER,
            ),
        ),
    )

    session_id = _session_id(_start(toolchain))
    _wait_for(lambda: _store(toolchain).read_events(session_id))
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert _janks(summary) == [_expected_jank(scenario)], summary
    assert _store(toolchain).load_meta(session_id).counters["anchor_written"] is True
    (written,) = _anchor_calls(toolchain)
    assert written[2:6] == ["shell", "log", "-t", "aes-log-monitor"], written


# --- review of 7d14210: a restarted read is the same read --------------------------------


def _input_anr() -> list[Frame]:
    """API 33's recorded input ANR: WindowManager's report, then ActivityManager's, 836 ms apart."""
    return frames(API33.data("logcat_anr_input_records.bin"))


def _input_anr_system_server() -> str:
    entry = next(e for e in API33.manifest["fixtures"] if e["name"] == "logcat_anr_input_records")
    return entry["system_server_pid"]


def test_an_anr_split_across_a_failed_dump_is_one_event(toolchain, recorded):
    """WindowManager's report read, a dump fails, ActivityManager's report read after the restart.

    Codex's split, derived from the recorded input ANR (its two reports 836 ms
    apart: one poll boundary and one transient failure are enough). The
    retry is the same logical read -- only one dump call failed -- so the
    held WindowManager report is still held when ActivityManager's report of
    the same window arrives, and settles: one ANR, as the uninterrupted read
    counts it. A fresh assembler for the retry emitted the held report as an
    ANR of a window and then counted ActivityManager's: two events, the
    summary complete.
    """
    scenario = _jank()
    report, *rest = _input_anr()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, report, *rest]),
        system_server=_input_anr_system_server(),
        dumps=(
            Rule(("logcat", "-d", "-T"), buffered=framed([scenario.anchor, report]), once=True),
            _dump_fails_once(),
        ),
    )

    session_id = _session_id(_start(toolchain))
    store = _store(toolchain)
    assert _wait_for(lambda: store.read_events(session_id)), "no ANR was read"
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert summary["event_count"] == 1, summary
    assert [c["sample_event"]["kind"] for c in summary["clusters"]] == ["anr"], summary
    assert store.load_meta(session_id).counters["dump_restarts"] == 1


def _live(tmp_path, answers) -> _LiveDevice:
    return _LiveDevice(tmp_path, answers)


def _opening_answers(recorded, scenario: Scenario, opening: _Answer) -> list[_Answer]:
    """The API 33 emulator as recorded, its opening dump answered by ``opening``."""
    entry = scenario.entry
    return [
        _Answer(("devices",), stdout=recorded.text("adb_devices_single")),
        _Answer(("get-state",), stdout=recorded.text("adb_get_state")),
        _Answer(("getprop", "ro.build.version.sdk"), stdout=API33.text("getprop_sdk")),
        _Answer(("cat", dump_read.BOOT_ID_PATH), stdout=API33.text("boot_id")),
        _Answer(("pidof", "system_server"), stdout=entry["system_server_pid"] + "\n"),
        _Answer(("cmd", "package"), stdout=entry["app"]["package_uid_listing"]["output"]),
        opening,
        _Answer(("logcat", "-d", "-T"), stdout=scenario.window),
    ]


def _kill_live_workers(device: _LiveDevice) -> None:
    for meta in device.store.list_sessions():
        if meta.pid and worker_identity(meta)[0] is Identity.WORKER:
            with contextlib.suppress(ProcessLookupError):
                os.kill(meta.pid, signal.SIGKILL)


def test_start_waits_for_an_opening_still_within_its_bounds(recorded, tmp_path):
    """The opening's first dump answers at 3.4 s, inside its own bound: --start waits for it.

    --start gave its worker 3 s to claim the session, and the worker read the
    device before it claimed: a valid opening finishing at 3.4 s was
    cancelled at 3.0 s, and the refusal blamed the host's Python startup. The
    worker now claims first and says it is opening, with the bound it keeps;
    --start waits for that.
    """
    scenario = _jank()
    device = _live(
        tmp_path,
        _opening_answers(
            recorded,
            scenario,
            _Answer(("logcat", "-d", "-T", "1"), stdout=scenario.opened, delay=3.4),
        ),
    )
    try:
        started = device.run("--start", "--serial", SERIAL, "--json")
        assert started.returncode == 0, started.stdout + started.stderr
        session_id = json.loads(started.stdout)["session_id"]
        stopped = device.run("--stop", session_id, "--json", "--all")
    finally:
        _kill_live_workers(device)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert _janks(json.loads(stopped.stdout)) == [_expected_jank(scenario)], stopped.stdout


def test_an_opening_past_its_own_bound_is_refused_naming_the_device(recorded, tmp_path):
    """The opening's dump never ends: --start refuses, saying the device's log read ran long.

    The bound shortened to 4 s (``ANDROID_EMU_LOG_STATE_TIMEOUT``), past --start's
    3 s registration: the refusal names the dump that did not finish, not the
    host's Python startup.
    """
    scenario = _jank()
    device = _live(
        tmp_path,
        _opening_answers(
            recorded,
            scenario,
            _Answer(("logcat", "-d", "-T", "1"), stdout=scenario.opened, hold=True),
        ),
    )
    try:
        started = device.toolchain.run(
            "anr_watcher.py",
            "--start",
            "--serial",
            SERIAL,
            "--json",
            ANDROID_EMU_LOG_STATE_TIMEOUT="4",
            timeout=90,
        )
    finally:
        _kill_live_workers(device)

    assert started.returncode == 1, started.stdout + started.stderr
    refusal = json.loads(started.stdout.splitlines()[-1])
    assert "did not register" not in refusal["error"], refusal
    assert "logcat -d -B -T 1" in refusal["error"], refusal
    assert "did not finish" in refusal["error"], refusal


def test_an_anchor_an_opening_wrote_is_said_even_when_the_opening_fails(toolchain, recorded):
    """The buffer held nothing, the marker the worker wrote is never found: the session says it wrote one.

    The worker's stderr goes nowhere, so the meta is where it is said:
    ``anchor_written`` in its counters, kept under the session's lock, and in
    the summary --start's refusal saves (and --get-details reads). Derived:
    every tail answers nothing (an empty buffer, as after `logcat -c`).
    """
    scenario = _jank()
    _device(
        toolchain,
        recorded,
        scenario,
        opening=(Rule(("logcat", "-d", "-T"), buffered=b""),),
        extra=(Rule(("shell", "log")),),
    )

    started = _start(toolchain)

    assert started.returncode == 1, started.stdout + started.stderr
    (meta,) = _store(toolchain).list_sessions()
    assert meta.counters.get("anchor_written") is True, meta.counters
    assert _store(toolchain).load_summary(meta.session_id).anchor_written is True
    details = toolchain.run("anr_watcher.py", "--get-details", meta.session_id, "--json")
    assert json.loads(details.stdout)["anchor_written"] is True, details.stdout
    text = toolchain.run("anr_watcher.py", "--get-details", meta.session_id)
    assert "Anchor written:" in text.stdout, text.stdout


def test_a_datagram_resent_right_after_the_checkpoint_is_read_after_a_restart(toolchain, recorded):
    """An app's byte-identical resend right after the checkpoint is a new record, and is read.

    The cursor already excludes what was consumed -- its frame and the frames
    read after it are found, then what follows is new -- so the stream's replay
    suppression (skip leading records the session consumed) must not be
    applied on top: it dropped the resend as "replayed". Derived: the window
    with the recorded jank's frame logged twice in a row, the first copy the
    last record read before a dump fails.
    """
    scenario = _jank()
    jank = _jank_frame(scenario)
    after = scenario.after
    at = next(i for i, frame in enumerate(after) if frame.raw == jank.raw)
    # The forty records before the jank, so the first poll's one dump holds the
    # open record: the window, its earlier records left out.
    before, rest = after[at - 39 : at + 1], after[at + 1 :]
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, *before, jank, *rest]),
        dumps=(
            Rule(("logcat", "-d", "-T"), buffered=framed([scenario.anchor, *before]), once=True),
            _dump_fails_once(),
        ),
    )

    session_id = _session_id(_start(toolchain))
    store = _store(toolchain)
    assert _wait_for(lambda: len(store.read_events(session_id)) >= 2), store.read_events(session_id)
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert summary["total_lines"] == len(before) + 1 + len(rest), summary
    (cluster,) = [c for c in summary["clusters"] if c["sample_event"]["kind"] == "jank"]
    assert cluster["count"] == 2, summary


def test_a_failed_system_server_lookup_is_asked_again_after_a_restart(toolchain, recorded):
    """system_server's pid unknown at first; after a restart it is asked again, and answers.

    Derived: the recorded broadcast ANR read in two parts across a failed dump;
    `pidof system_server` fails the first time it is asked (exit 1, nothing
    said). ActivityManager's report read before the failure stays unverified;
    the reports read after the restart are authenticated: one record set
    aside, not three.
    """
    scenario = _jank()
    records = frames(API33.data("logcat_anr_broadcast_records.bin"))
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, *records]),
        system_server=_anr_system_server(),
        early=(Rule(("pidof", "system_server"), exit=1, once=True),),
        dumps=(
            Rule(
                ("logcat", "-d", "-T"), buffered=framed([scenario.anchor, *records[:2]]), once=True
            ),
            _dump_fails_once(),
        ),
    )

    session_id = _session_id(_start(toolchain))
    # The opening's dump, the first poll's, the failed one, the restarted read's.
    assert _wait_for(lambda: len([c for c in _logcat_calls(toolchain) if "-d" in c]) >= 4)
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    summary = json.loads(stopped.stdout)
    assert summary["unverified_anr_lines"] == 1, summary
    asked = [call for call in toolchain.calls("adb") if "system_server" in call]
    assert len(asked) == 2, asked


_EVENTS_FSYNC_FAILS = """
import os

_fsync = os.fsync


def _path(fd):
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):
            return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\\0", 1)[0].decode()
    except OSError:
        pass
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return ""


def fsync(fd):
    if _path(fd).endswith("events.jsonl"):
        raise OSError(5, "Input/output error")
    return _fsync(fd)


os.fsync = fsync
"""


def test_a_checkpoint_never_runs_ahead_of_events_that_could_not_be_synced(
    toolchain, recorded, tmp_path
):
    """fsync of the events file fails: the cursor is not published past them, and the session says so.

    A stand-in for a full or failing disk, not tool output: ``os.fsync`` of
    ``events.jsonl`` raises EIO in the worker (a ``sitecustomize`` on its
    path). The jank was read and written, but never made durable: the
    session ends incomplete with the reason, --stop refuses, and the cursor
    in its meta is the last one whose events were synced -- the opening's.
    """
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_EVENTS_FSYNC_FAILS, encoding="utf-8")
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    session_id = _session_id(_start(toolchain, PYTHONPATH=str(hook)))
    store = _store(toolchain)
    assert _wait_for(lambda: store.read_events(session_id))
    stopped = _stop(toolchain, session_id)

    assert stopped.returncode == 1, stopped.stdout + stopped.stderr
    refusal = json.loads(stopped.stdout.splitlines()[-1])
    assert "Input/output error" in refusal["error"], refusal
    assert store.load_summary(session_id).incomplete is True
    checkpoint = store.load_meta(session_id).counters["cursor"]
    assert base64.b64decode(checkpoint["frame"]) == scenario.opened


# --- the opening is a lifecycle status (second review of the opening) -------------------
#
# A worker claims its session into OPENING and leaves it by one write of its
# own: RUNNING once its read is open (the checkpoint published with it),
# CRASHED when the opening failed, CANCELLED when a stop came first. --start
# answers success only on RUNNING. Each cell below is a status, whether its
# worker is alive, and what a command answers then.


def _ps_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _running_stubs(toolchain: FakeToolchain) -> list[int]:
    """The fake adb processes still running: pids whose command is still this toolchain's stub."""
    return [
        pid for pid, _argv in toolchain.stub_pids("adb") if str(toolchain.bin) in _ps_command(pid)
    ]


def _end_stubs(toolchain: FakeToolchain) -> None:
    """End a fake adb a spec left waiting -- only a pid still running this toolchain's stub."""
    for pid in _running_stubs(toolchain):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _spawn_worker(toolchain: FakeToolchain, session_id: str, token: str, **env: str):
    """The worker --start detaches, started by hand: same command, same environment."""
    return subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "anr_watcher.py"),
            "--worker-session-id",
            session_id,
            anr_watcher.WORKER_TOKEN_FLAG,
            token,
        ],
        env=toolchain.env(**env),
        cwd=str(toolchain.tmp),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _opening_device(toolchain: FakeToolchain, recorded) -> None:
    """The API 33 emulator as recorded, its opening's first dump never ending (`hang`)."""
    scenario = _jank()
    _device(
        toolchain,
        recorded,
        scenario,
        opening=(Rule(("logcat", "-d", "-T", "1"), stdout=scenario.opened, hang=True),),
    )


def _opening_dump_started(toolchain: FakeToolchain) -> bool:
    return any("-T" in call and call[-1] == "1" for call in _logcat_calls(toolchain))


@pytest.fixture
def opening_session(toolchain, recorded):
    """A session whose worker is opening its read, inside its 8 s bound."""
    _opening_device(toolchain, recorded)
    store = _store(toolchain)
    meta = store.create({"min_frames": 30, "package": None, "serial": SERIAL})
    worker = _spawn_worker(
        toolchain, meta.session_id, meta.worker_token, ANDROID_EMU_LOG_STATE_TIMEOUT="8"
    )
    try:
        assert _wait_for(
            lambda: store.load_meta(meta.session_id).status == "opening"
            and _opening_dump_started(toolchain)
        ), store.load_meta(meta.session_id)
        yield toolchain, store, meta.session_id, worker
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)
        _end_stubs(toolchain)


def _cli(toolchain: FakeToolchain, *argv: str) -> tuple[int, dict | list]:
    result = toolchain.run("anr_watcher.py", *argv, "--json")
    return result.returncode, json.loads(result.stdout)


# (worker, command) -> (exit status, what the answer says, the status after it)
OPENING_CELLS = {
    ("alive", "--list-sessions"): (0, "opening, unpublished_end false", "opening"),
    ("alive", "--get-details"): (1, "No summary for", "opening"),
    ("alive", "--diff"): (1, "No summary.json", "opening"),
    ("alive", "--stop"): (1, "still opening its read", "cancelled"),
    # A worker that answers its stop is answered for: --abandon has nothing to give up on.
    ("alive", "--stop --abandon"): (1, "still opening its read", "cancelled"),
    ("gone", "--list-sessions"): (0, "opening, unpublished_end true", "opening"),
    ("gone", "--get-details"): (1, "worker is gone", "opening"),
    ("gone", "--diff"): (1, "worker is gone", "opening"),
    ("gone", "--stop"): (1, "worker is gone", "opening"),
    ("gone", "--stop --abandon"): (0, "incomplete, abandoned", "abandoned"),
}


@pytest.mark.parametrize(("worker", "command"), sorted(OPENING_CELLS))
def test_an_opening_session_is_answered_by_every_command(worker, command, opening_session):
    toolchain, store, session_id, process = opening_session
    status, says, after = OPENING_CELLS[(worker, command)]
    if worker == "gone":
        process.kill()
        process.wait(timeout=10)
    argv = {
        "--list-sessions": ("--list-sessions",),
        "--get-details": ("--get-details", session_id),
        "--diff": ("--diff", session_id, session_id),
        "--stop": ("--stop", session_id),
        "--stop --abandon": ("--stop", session_id, "--abandon"),
    }[command]

    began = time.monotonic()
    exit_status, answer = _cli(toolchain, *argv)
    took = time.monotonic() - began

    assert exit_status == status, answer
    if command == "--list-sessions":
        (listed,) = [entry for entry in answer if entry["session_id"] == session_id]
        assert listed["status"] == "opening", listed
        assert listed["unpublished_end"] is (worker == "gone"), listed
    elif exit_status == 0:
        assert answer["incomplete"] is True, answer
        assert "abandoned" in answer["incomplete_reason"], answer
    else:
        assert says in answer["error"], answer
    if worker == "alive" and command.startswith("--stop"):
        assert _wait_for(lambda: process.poll() is not None, seconds=10), "the worker ran on"
        assert store.load_meta(session_id).counters.get("cancelled"), store.load_meta(session_id)
        # It cancelled its opening with the dump it was waiting on stopped and
        # reaped -- at once, not when that dump's share of the 8 s ran out.
        assert _running_stubs(toolchain) == [], "the opening's dump outlived its worker"
        assert took < 4, f"--stop answered {took:.1f}s later: the dump was waited out"
    assert store.load_meta(session_id).status == after


def test_start_refuses_and_stops_an_opening_past_its_wait(toolchain, recorded):
    """--start's wait runs out while its worker is still opening: refused, and the session stopped.

    The worker is held (SIGSTOP) once it is opening, as a host too loaded to
    run it would hold it. --start used to answer success at 34 s with no cursor
    (Codex, second review of the opening). It now answers only RUNNING: past
    its wait it asks the worker to stop and refuses, naming the opening and
    how long it ran; once the worker can run again it cancels, and the
    session ends ``cancelled``, never left opening.
    """
    _opening_device(toolchain, recorded)
    env = {"ANDROID_EMU_LOG_STATE_TIMEOUT": "4"}
    store = _store(toolchain)
    start = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "anr_watcher.py"), "--start", "--serial", SERIAL, "--json"],
        env=toolchain.env(**env),
        cwd=str(toolchain.tmp),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker_pid = None
    try:
        assert _wait_for(
            lambda: any(m.status == "opening" and m.pid for m in store.list_sessions())
        ), "no worker opened"
        (meta,) = store.list_sessions()
        worker_pid = meta.pid
        os.kill(worker_pid, signal.SIGSTOP)
        stdout, _stderr = start.communicate(timeout=90)
    finally:
        if start.poll() is None:
            start.kill()
        if worker_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker_pid, signal.SIGCONT)

    assert start.returncode == 1, stdout
    refusal = json.loads(stdout.splitlines()[-1])
    assert "still opening its read" in refusal["error"], refusal
    assert "s after it began" in refusal["error"], refusal
    assert _wait_for(lambda: store.load_meta(meta.session_id).status == "cancelled", seconds=20)
    _end_stubs(toolchain)


def test_start_answers_a_worker_gone_while_opening_as_gone(toolchain, recorded):
    """The worker is killed while it opens: --start says it is gone, as --stop would."""
    _opening_device(toolchain, recorded)
    store = _store(toolchain)
    start = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "anr_watcher.py"), "--start", "--serial", SERIAL, "--json"],
        env=toolchain.env(ANDROID_EMU_LOG_STATE_TIMEOUT="8"),
        cwd=str(toolchain.tmp),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for(
            lambda: any(m.status == "opening" and m.pid for m in store.list_sessions())
        ), "no worker opened"
        (meta,) = store.list_sessions()
        os.kill(meta.pid, signal.SIGKILL)
        stdout, _stderr = start.communicate(timeout=60)
    finally:
        if start.poll() is None:
            start.kill()
        _end_stubs(toolchain)

    assert start.returncode == 1, stdout
    refusal = json.loads(stdout.splitlines()[-1])
    assert "worker is gone" in refusal["error"], refusal


def test_a_wedged_adb_server_ends_the_opening_within_its_stated_bound(toolchain, recorded):
    """Every probe of the opening times out: the session is published crashed within the bound.

    adb answers --start's own `get-state`, then nothing: every later question
    hangs (`hang`). The opening's bound shortened to 2 s; every probe inside it
    -- the level, the device question after it, the failure's own -- gets only
    what is left, and the publication follows. A failure conversion asking
    `get-state` with a fresh 10 s of its own ran past it (Codex: 15 + 15 + 10 s).
    """
    scenario = _jank()
    _device(
        toolchain,
        recorded,
        scenario,
        state=(
            Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
            Rule(("get-state",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
        ),
        early=(
            Rule(
                ("shell", "getprop", "ro.build.version.sdk"),
                stderr=recorded.text("adb_waiting_for_device"),
                hang=True,
            ),
        ),
    )
    started_at = time.monotonic()
    try:
        started = _start(toolchain, ANDROID_EMU_LOG_STATE_TIMEOUT="2")
    finally:
        _end_stubs(toolchain)
    took = time.monotonic() - started_at

    assert started.returncode == 1, started.stdout + started.stderr
    (meta,) = _store(toolchain).list_sessions()
    assert meta.status == "crashed", meta
    published = (meta.stopped_at_ms - meta.claimed_at_ms) / 1000
    # The opening (2 s), a stopped dump's reap, the publication's write: no
    # refusal named several devices, so adb's device listing never ran.
    publication = 2.0
    bound = 2 + dump_read.KILL_REAP_TIMEOUT + publication
    assert published <= bound, f"published {published:.1f}s after the claim, bound {bound}s"
    assert (
        took <= anr_watcher.REGISTRATION_TIMEOUT_SECONDS + 2 + anr_watcher.OPENING_OVERRUN_SECONDS
    )
    assert publication == anr_watcher.PUBLICATION_SECONDS


_FILES_RUN_OUT = """
import os
import tempfile

_fsync = os.fsync
_temporary = tempfile.TemporaryFile
_made = [0]


def _path(fd):
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):
            return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\\0", 1)[0].decode()
    except OSError:
        pass
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return ""


def fsync(fd):
    if _path(fd).endswith("events.jsonl"):
        raise OSError(5, "Input/output error")
    return _fsync(fd)


def temporary_file(*args, **kwargs):
    _made[0] += 1
    if _made[0] > int(os.environ.get("AES_TEMPORARY_FILES", "1000000")):
        raise OSError(24, "Too many open files")
    return _temporary(*args, **kwargs)


os.fsync = fsync
tempfile.TemporaryFile = temporary_file
"""


def test_an_unforeseen_error_after_a_good_poll_publishes_only_a_synced_checkpoint(
    toolchain, recorded, tmp_path
):
    """EMFILE creating the next dump, after one good poll: the end publishes the last synced cursor.

    A stand-in for a process out of descriptors and a failing disk, not tool
    output (a ``sitecustomize`` on the worker's path): fsync of the events
    file fails, and the ninth temporary file -- the second poll's first dump
    -- cannot be made. The sync in the read's ``finally`` runs on that path
    too, so the cursor the worker publishes as it ends is the last one whose
    events were synced: the opening's. It published the first poll's.
    """
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_FILES_RUN_OUT, encoding="utf-8")
    scenario = _jank()
    _device(toolchain, recorded, scenario)

    session_id = _session_id(_start(toolchain, PYTHONPATH=str(hook), AES_TEMPORARY_FILES="8"))
    store = _store(toolchain)
    assert _wait_for(lambda: store.load_meta(session_id).status == "crashed"), store.load_meta(
        session_id
    )

    meta = store.load_meta(session_id)
    assert "Too many open files" in meta.counters["stream_failure"]["error"], meta.counters
    assert base64.b64decode(meta.counters["cursor"]["frame"]) == scenario.opened


# --- third review of the opening: a stop and the open's publication, one lock ----------
#
# A stop is recorded in the session's meta, under its lock, before the worker
# is signalled; the worker's publication that its read is open reads it under
# the same lock. Whichever takes the lock first decides: the stop -- the
# session ends ``cancelled``, nothing recorded -- or the publication -- the
# session is ``running``, and the stop that follows is an ordinary stop. The
# barrier below (a ``sitecustomize`` on the worker's path, not tool output)
# holds the worker just before, or just after, that publication.

_BARRIER = """
import importlib.abc
import importlib.util
import os
import sys
import time

_DIR = os.environ.get("AES_BARRIER")
_AT = os.environ.get("AES_BARRIER_AT", "before")


def _hold():
    open(os.path.join(_DIR, "at"), "w").close()
    while not os.path.exists(os.path.join(_DIR, "go")):
        time.sleep(0.01)


def _patch(module):
    publish = module.SessionStore.worker_opened

    def worker_opened(self, *args, **kwargs):
        if _AT == "before":
            _hold()
        meta = publish(self, *args, **kwargs)
        if _AT == "after":
            _hold()
        return meta

    module.SessionStore.worker_opened = worker_opened


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "common.anr_sessions":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            _patch(module)

        spec.loader.exec_module = exec_module
        return spec


if _DIR:
    sys.meta_path.insert(0, _Finder())
"""

# Where a stop asked of a session is recorded (common.anr_sessions.STOP_REQUESTED).
STOP_REQUESTED = "stop_requested_ms"


def _barrier(tmp_path, at: str) -> tuple[Path, dict[str, str]]:
    """The barrier's directory, and the worker environment that holds it ``at`` its publication."""
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_BARRIER, encoding="utf-8")
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    return barrier, {"PYTHONPATH": str(hook), "AES_BARRIER": str(barrier), "AES_BARRIER_AT": at}


def _cli_in_background(toolchain: FakeToolchain, *argv: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SCRIPTS / "anr_watcher.py"), *argv, "--json"],
        env=toolchain.env(),
        cwd=str(toolchain.tmp),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_recorded(store: SessionStore, session_id: str) -> bool:
    return STOP_REQUESTED in store.load_meta(session_id).extras


def test_a_stop_that_takes_the_lock_before_the_open_is_published_cancels_it(
    toolchain, recorded, tmp_path
):
    """The read opened, the stop came first: ``cancelled``, nothing recorded -- never running.

    Codex's interleaving (third review of the opening): the opening returned,
    the CLI signalled the still-``opening`` worker, and the worker then
    published ``running`` without looking: ``opening -> running -> stopped``,
    a final dump read, where the answer was that nothing was recorded.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario)
    barrier, env = _barrier(tmp_path, "before")
    store = _store(toolchain)
    meta = store.create({"min_frames": 30, "package": None, "serial": SERIAL})
    worker = _spawn_worker(toolchain, meta.session_id, meta.worker_token, **env)
    try:
        assert _wait_for(lambda: (barrier / "at").exists()), "the worker never opened its read"
        dumps = len(_logcat_calls(toolchain))
        stop = _cli_in_background(toolchain, "--stop", meta.session_id)
        # The barrier: the stop has taken the lock, recorded itself and
        # signalled before the worker's publication takes it.
        _wait_for(lambda: _stop_recorded(store, meta.session_id), seconds=5)
        (barrier / "go").touch()
        stdout, _stderr = stop.communicate(timeout=60)
        worker.wait(timeout=30)
    finally:
        if worker.poll() is None:
            worker.kill()
        _end_stubs(toolchain)

    ended = store.load_meta(meta.session_id)
    assert ended.status == "cancelled", ended
    assert stop.returncode == 1, stdout
    assert "still opening its read" in json.loads(stdout)["error"], stdout
    assert len(_logcat_calls(toolchain)) == dumps, "a dump was read after the stop"
    assert store.read_events(meta.session_id) == []
    assert ended.counters.get("cancelled"), ended.counters
    assert _stop_recorded(store, meta.session_id)


def test_a_publication_that_takes_the_lock_before_the_stop_runs_then_stops(
    toolchain, recorded, tmp_path
):
    """The open was published first: ``running``, and the stop is an ordinary one, its last read made."""
    scenario = _jank()
    _device(toolchain, recorded, scenario)
    barrier, env = _barrier(tmp_path, "after")
    store = _store(toolchain)
    meta = store.create({"min_frames": 30, "package": None, "serial": SERIAL})
    worker = _spawn_worker(toolchain, meta.session_id, meta.worker_token, **env, **NO_POLLS)
    try:
        assert _wait_for(lambda: (barrier / "at").exists()), "the worker never published"
        assert store.load_meta(meta.session_id).status == "running"
        dumps = len(_logcat_calls(toolchain))
        stop = _cli_in_background(toolchain, "--stop", meta.session_id)
        assert _wait_for(lambda: _stop_recorded(store, meta.session_id), seconds=10)
        (barrier / "go").touch()
        stdout, _stderr = stop.communicate(timeout=60)
        worker.wait(timeout=30)
    finally:
        if worker.poll() is None:
            worker.kill()
        _end_stubs(toolchain)

    assert stop.returncode == 0, stdout
    assert store.load_meta(meta.session_id).status == "stopped"
    assert len(_logcat_calls(toolchain)) > dumps, "the stop's last read was not made"
    assert _janks(json.loads(stdout)) == [_expected_jank(scenario)], stdout


def test_start_past_its_wait_with_the_read_opened_but_unpublished_ends_cancelled(
    toolchain, recorded, tmp_path
):
    """--start's wait runs out between the open and its publication: refused, and ``cancelled``.

    --start said the session ends cancelled with nothing recorded into it; the
    worker published ``running`` once it could run and read on to a stop.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario)
    barrier, env = _barrier(tmp_path, "before")
    store = _store(toolchain)
    try:
        started = _start(toolchain, ANDROID_EMU_LOG_STATE_TIMEOUT="2", **env)
        (meta,) = store.list_sessions()
        dumps = len(_logcat_calls(toolchain))
        (barrier / "go").touch()
        assert _wait_for(lambda: store.load_meta(meta.session_id).status != "opening")
        assert _wait_for(lambda: store.load_meta(meta.session_id).pid is None)
    finally:
        _end_stubs(toolchain)

    assert started.returncode == 1, started.stdout
    assert "still opening its read" in json.loads(started.stdout)["error"], started.stdout
    ended = store.load_meta(meta.session_id)
    assert ended.status == "cancelled", ended
    assert len(_logcat_calls(toolchain)) == dumps, "a dump was read after the stop"
    assert store.read_events(meta.session_id) == []
    assert _stop_recorded(store, meta.session_id)


def test_a_late_stop_waits_no_longer_than_the_opening_published(toolchain, recorded, tmp_path):
    """A stop asked 3 s before the opening's published end answers by that end, not 5.5 s later.

    The worker is held before its publication and never answers. --stop
    kept a fresh stop allowance on top of the opening's end (Codex: asked at
    28 s, answered at 33.55 s against 29). Then: asked again, a stop already
    recorded answers at once; --abandon gives the session up; the worker,
    released, writes nothing.
    """
    scenario = _jank()
    _device(toolchain, recorded, scenario)
    barrier, env = _barrier(tmp_path, "before")
    store = _store(toolchain)
    meta = store.create({"min_frames": 30, "package": None, "serial": SERIAL})
    worker = _spawn_worker(
        toolchain, meta.session_id, meta.worker_token, ANDROID_EMU_LOG_STATE_TIMEOUT="2", **env
    )
    try:
        assert _wait_for(lambda: (barrier / "at").exists()), "the worker never opened its read"
        bound = store.load_meta(meta.session_id).extras["opening"]
        ends = bound["since_ms"] / 1000 + bound["within_s"] + anr_watcher.OPENING_OVERRUN_SECONDS
        time.sleep(max(0.0, ends - 3 - time.time()))

        first_status, first = _cli(toolchain, "--stop", meta.session_id)
        answered = time.time()
        again_began = time.monotonic()
        again_status, again = _cli(toolchain, "--stop", meta.session_id)
        again_took = time.monotonic() - again_began
        abandoned_status, abandoned = _cli(toolchain, "--stop", meta.session_id, "--abandon")
        (barrier / "go").touch()
        worker.wait(timeout=30)
    finally:
        if worker.poll() is None:
            worker.kill()
        _end_stubs(toolchain)

    assert first_status == 1, first
    assert "still opening its read" in first["error"], first
    assert answered <= ends + 1.0, f"--stop answered {answered - ends:+.1f}s past the opening's end"
    assert again_status == 1, again
    assert "still opening its read" in again["error"], again
    assert again_took < 3, f"a stop already recorded, past the end, waited {again_took:.1f}s"
    assert abandoned_status == 0, abandoned
    assert abandoned["incomplete"] is True and "abandoned" in abandoned["incomplete_reason"]
    assert store.load_meta(meta.session_id).status == "abandoned"


_DISK_FILLS = """
import builtins
import errno
import os

_open = builtins.open


class _Events:
    def __init__(self, handle):
        self._handle = handle

    def write(self, text):
        if os.path.exists(os.environ["AES_DISK_FULL"]):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return self._handle.write(text)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


def open(file, mode="r", *args, **kwargs):
    handle = _open(file, mode, *args, **kwargs)
    if str(file).endswith("events.jsonl") and "a" in mode:
        return _Events(handle)
    return handle


builtins.open = open
"""


def test_a_held_report_that_cannot_be_written_at_the_end_publishes_the_synced_cursor(
    toolchain, recorded, tmp_path
):
    """The disk fills with a WindowManager report held: the end keeps the last synced checkpoint.

    A stand-in for a full disk, not tool output (a ``sitecustomize`` on the
    worker's path): once the marker file exists, every write to the events
    file fails ENOSPC. A poll reads the recorded input ANR's WindowManager
    report -- held for ActivityManager's, which never comes -- and the
    cursor moves past it, nothing synced yet; the disk fills; at --stop the
    read's close writes the held report and fails. That raised before the
    sync in the same ``finally``, and ``crashed`` was published with the
    advanced cursor (Codex, third review of the opening). The cursor
    published is the opening's, the last one synced, and the session ends
    crashed with the error.
    """
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_DISK_FILLS, encoding="utf-8")
    full = tmp_path / "disk-full"
    scenario = _jank()
    report, *_rest = _input_anr()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, report]),
        system_server=_input_anr_system_server(),
    )

    session_id = _session_id(_start(toolchain, PYTHONPATH=str(hook), AES_DISK_FULL=str(full)))
    store = _store(toolchain)
    # Two polls after the opening: the first read the report, and was over.
    assert _wait_for(lambda: len(_logcat_calls(toolchain)) >= 3), _logcat_calls(toolchain)
    full.touch()
    stopped = _stop(toolchain, session_id)

    meta = store.load_meta(session_id)
    assert meta.status == "crashed", meta
    assert "No space left on device" in meta.counters["stream_failure"]["error"], meta.counters
    assert base64.b64decode(meta.counters["cursor"]["frame"]) == scenario.opened
    assert stopped.returncode == 1, stopped.stdout
    assert store.load_summary(session_id).incomplete is True


def test_a_report_held_past_a_periodic_checkpoint_and_never_written_is_counted(
    toolchain, recorded, tmp_path
):
    """Codex's sequence: the report read at 1 s, the periodic checkpoint at 6 s, a full disk at stop.

    The checkpoint covers the records read and processed. A WindowManager
    report held for ActivityManager's (ANR_CORRELATION_MS) lives in memory
    until its event is written, so the periodic checkpoint passes it; only
    the same worker resumes from a checkpoint, with its held reports. This
    worker cannot write the report as it ends (ENOSPC, the stand-in above):
    the session ends crashed and incomplete, and says so --
    ``held_unwritten: 1`` in its counters and its summary, the reason naming
    it. It ended with zero events and nothing said of the report (Codex,
    merge gate of e03e8e7).
    """
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_DISK_FILLS, encoding="utf-8")
    full = tmp_path / "disk-full"
    scenario = _jank()
    report, *_rest = _input_anr()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, report]),
        system_server=_input_anr_system_server(),
    )

    session_id = _session_id(_start(toolchain, PYTHONPATH=str(hook), AES_DISK_FULL=str(full)))
    store = _store(toolchain)

    def checkpointed_past_the_report() -> bool:
        frame = store.load_meta(session_id).counters["cursor"]["frame"]
        return base64.b64decode(frame) != scenario.opened

    # The periodic checkpoint (every COUNTERS_PERSIST_SECONDS), the report still held.
    assert _wait_for(checkpointed_past_the_report, seconds=20), "no periodic checkpoint"
    full.touch()
    stopped = _stop(toolchain, session_id)

    meta = store.load_meta(session_id)
    assert meta.status == "crashed", meta
    assert meta.counters.get("held_unwritten") == 1, meta.counters
    assert "1 held report was not written" in meta.counters["stream_failure"]["error"]
    assert store.read_events(session_id) == []
    assert stopped.returncode == 1, stopped.stdout
    assert "1 held report was not written" in json.loads(stopped.stdout)["error"], stopped.stdout
    summary = store.load_summary(session_id)
    assert summary.held_unwritten == 1, summary
    assert summary.incomplete is True
    assert "1 held report was not written" in summary.incomplete_reason, summary
    details = toolchain.run("anr_watcher.py", "--get-details", session_id, "--json")
    assert json.loads(details.stdout)["held_unwritten"] == 1, details.stdout


# --- final gate of c832cdf: every event write counts what it could not write --------------

# What a killed worker's session, and an abandoned one, say of the reports
# its worker held for correlation: none of their number is known.
HELD_UNKNOWN = (
    "reports the worker held for correlation, if any, were not written; their number is unknown"
)


def _released_by_a_later_system_record(scenario: Scenario) -> Frame:
    """A record system_server wrote more than ANR_CORRELATION_MS after the recorded report.

    Derived: the jank recording's first record a system process wrote (uid
    1000, logged two days after the input ANR was recorded), its writer's
    pid set to that run's system_server (``Frame.replace``), so the assembler
    takes it as system_server's and releases the report it holds.
    """
    later = next(
        f
        for f in frames(scenario.window)
        if f.uid == 1000 and f.tag not in ("Choreographer", "ActivityManager", "StrictMode")
    )
    return later.replace(pid=_input_anr_system_server())


def _disk_full_from_the_start(tmp_path) -> dict[str, str]:
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_DISK_FILLS, encoding="utf-8")
    full = tmp_path / "disk-full"
    full.touch()
    return {"PYTHONPATH": str(hook), "AES_DISK_FULL": str(full)}


def _held_unwritten_session(toolchain: FakeToolchain, store: SessionStore, session_id: str):
    assert _wait_for(
        lambda: store.load_meta(session_id).status == "crashed", seconds=40
    ), store.load_meta(session_id)
    meta = store.load_meta(session_id)
    assert meta.counters.get("held_unwritten") == 1, meta.counters
    assert "1 held report was not written" in meta.counters["stream_failure"]["error"], meta
    assert store.read_events(session_id) == []
    stopped = _stop(toolchain, session_id)
    assert stopped.returncode == 1, stopped.stdout
    summary = store.load_summary(session_id)
    assert summary.held_unwritten == 1, summary
    assert "1 held report was not written" in summary.incomplete_reason, summary
    return meta


def test_a_held_report_released_by_quiet_that_cannot_be_written_is_counted(
    toolchain, recorded, tmp_path
):
    """Codex's sequence: the report read at 1 s, the checkpoint at 6 s, its quiet release unwritable.

    The read goes quiet for ANR_CORRELATION_MS after the recorded
    WindowManager report, so the assembler releases it (``flush``) -- out of
    its hold before its event is written -- and the write fails (ENOSPC, the
    stand-in above, from the start). ``close`` then held nothing, and the
    session ended with zero events, no count, a generic failure (Codex,
    final gate of c832cdf). Every event write now goes through one function,
    which counts what it had not written.
    """
    scenario = _jank()
    report, *_rest = _input_anr()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, report]),
        system_server=_input_anr_system_server(),
    )

    session_id = _session_id(_start(toolchain, **_disk_full_from_the_start(tmp_path)))
    store = _store(toolchain)

    meta = _held_unwritten_session(toolchain, store, session_id)
    # The periodic checkpoint was written past the report before the quiet ran out.
    assert base64.b64decode(meta.counters["cursor"]["frame"]) != scenario.opened


def test_a_held_report_released_by_a_later_record_that_cannot_be_written_is_counted(
    toolchain, recorded, tmp_path
):
    """The report released by system_server's next record, 10 s on, as it is read: unwritable, counted."""
    scenario = _jank()
    report, *_rest = _input_anr()
    later = _released_by_a_later_system_record(scenario)
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, report, later]),
        system_server=_input_anr_system_server(),
        dumps=(
            Rule(("logcat", "-d", "-T"), buffered=framed([scenario.anchor, report]), once=True),
        ),
    )

    session_id = _session_id(_start(toolchain, **_disk_full_from_the_start(tmp_path)))

    _held_unwritten_session(toolchain, _store(toolchain), session_id)


def test_a_killed_or_abandoned_worker_says_the_number_of_its_held_reports_is_unknown(
    toolchain, recorded
):
    """Killed with a report held: the gone answer, the --abandon remedy and the summary say so.

    The count is exact only when the worker itself ends. Killed (SIGKILL), it
    publishes nothing, and what it held in memory is lost; --stop --abandon
    saves what the events file holds. The gone-worker answer said "saves
    what was read", and the abandoned reason spoke only of reads after the
    abandonment (Codex, final gate of c832cdf).
    """
    scenario = _jank()
    report, *_rest = _input_anr()
    _device(
        toolchain,
        recorded,
        scenario,
        window=framed([scenario.anchor, report]),
        system_server=_input_anr_system_server(),
    )
    session_id = _session_id(_start(toolchain))
    store = _store(toolchain)
    # Two polls after the opening: the first read the report, which is held.
    assert _wait_for(lambda: len(_logcat_calls(toolchain)) >= 3), _logcat_calls(toolchain)
    os.kill(store.load_meta(session_id).pid, signal.SIGKILL)
    assert _wait_for(lambda: worker_identity(store.load_meta(session_id))[0] is Identity.GONE)

    gone_status, gone = _cli(toolchain, "--stop", session_id)
    details_status, details = _cli(toolchain, "--get-details", session_id)
    abandoned_status, abandoned = _cli(toolchain, "--stop", session_id, "--abandon")

    assert gone_status == 1, gone
    assert "worker is gone" in gone["error"], gone
    assert HELD_UNKNOWN in gone["error"], gone
    assert HELD_UNKNOWN in gone["remedy"], gone
    assert details_status == 1, details
    assert HELD_UNKNOWN in details["error"], details
    assert HELD_UNKNOWN in details["remedy"], details
    assert abandoned_status == 0, abandoned
    assert abandoned["incomplete"] is True, abandoned
    assert HELD_UNKNOWN in abandoned["incomplete_reason"], abandoned


def test_every_event_write_goes_through_the_one_writer():
    """The events file is written by ``_SessionRecords._record_one``, called only by ``record``.

    Parsed, not grepped: ``event_to_jsonl`` (an event's line) appears in one
    function, and that function is called from one -- the choke point that
    counts what it could not write. A release path that wrote around it
    dropped a held report with nothing said.
    """
    tree = ast.parse((SCRIPTS / "anr_watcher.py").read_text(encoding="utf-8"))
    writers, callers = [], []
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef):
            continue
        for node in ast.walk(scope):
            if isinstance(node, ast.Name) and node.id == "event_to_jsonl":
                writers.append(scope.name)
            if isinstance(node, ast.Attribute) and node.attr == "_record_one":
                callers.append(scope.name)
    assert writers == ["_record_one"], writers
    assert callers == ["record"], callers
