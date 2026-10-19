"""common.log_lifecycle: system_server's process sentences, read one way by both readers.

``log_monitor --app`` / ``app_state_capture`` (through ``common.logcat``) and
the ANR session worker (``anr_watcher._ProcessNames``) used to read these
sentences two ways, the worker's the looser (review of PR #34). Both now read
logd's records (`logcat -B`, W3-L), and every record here is a recorded one --
logcat_app_restart_records, with its own oracles: system_server's pid and the
package's uid listing, measured by the recorder in the same run; and the
forging record of logcat_tag_injection_records -- or a recorded record with
only the field an app controls changed, and the change said.
"""

from __future__ import annotations

from dataclasses import replace

import anr_watcher
import pytest

from common import log_lifecycle, logcat
from common.device_utils import parse_package_uids
from common.logcat import LogRecord

PACKAGE = "com.example.composefixture"
FIXTURE = "logcat_app_restart_records"
INJECTION = "logcat_tag_injection_records"
# A pid no process in the recording holds.
VICTIM = "4242"


def _entry(recorded, name: str = FIXTURE) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == name)


def _records(recorded, name: str = FIXTURE) -> list[LogRecord]:
    return logcat.parse_records(recorded.data(f"{name}.bin"))


def _system_server(recorded) -> str:
    pid = str(_entry(recorded).get("system_server_pid", ""))
    if not pid.isdigit():
        pytest.fail(f"{FIXTURE} no longer records system_server's pid; re-record it")
    return pid


def _uids(recorded, package: str) -> frozenset[int] | None:
    """The package's uids, from the listing `cmd package list packages -U` gave in that run."""
    return parse_package_uids(_entry(recorded)["package_uid_listing"]["output"], package)


def _reader(recorded) -> log_lifecycle.LifecycleReader:
    return log_lifecycle.LifecycleReader(
        _system_server(recorded), lambda package: _uids(recorded, package)
    )


def _record(recorded, opening: str, tag: str = "ActivityManager") -> LogRecord:
    """The first recorded record under ``tag`` whose message opens with ``opening``."""
    found = next(
        (r for r in _records(recorded) if r.tag == tag and r.message.startswith(opening)), None
    )
    if found is None:
        pytest.fail(f"{FIXTURE} no longer holds a {tag} record opening {opening!r}")
    return found


def _later(record: LogRecord, seconds: float) -> LogRecord:
    """The recorded record with only its time (logd's sec/nsec) moved ``seconds`` later."""
    total = record.sec * 1_000_000_000 + record.nsec + round(seconds * 1_000_000_000)
    return replace(record, sec=total // 1_000_000_000, nsec=total % 1_000_000_000)


def _forging(recorded) -> LogRecord:
    """logcat_tag_injection_records' forging record, as recorded.

    The shell's record (pid and uid the shell's), its TAG ``x:``, a newline and
    a threadtime header claiming system_server's pid and the tag
    ActivityManager; its message system_server's ``Start proc
    <victim>:<package>/<package uid>``. Printed as text (logcat_tag_injection)
    it is a line of system_server's naming the victim pid.
    """
    forger = int(_entry(recorded, INJECTION)["injection"]["forger_pid"])
    forging = next(r for r in _records(recorded, INJECTION) if r.pid == forger)
    if "\n" not in forging.tag or not forging.message.startswith("Start proc "):
        pytest.fail("the recorded forging record no longer has its shape")
    return forging


# --- the reading itself -----------------------------------------------------------


def test_each_recorded_sentence_is_read_with_its_pid_name_and_uid(recorded):
    """Against the run's own `pidof` answers: three starts, a crash's death, a force-stop's kill."""
    entry = _entry(recorded)
    endings = dict(zip(entry["pid_endings"], entry["pids"], strict=True))
    reader = _reader(recorded)

    sentences = [s for record in _records(recorded) if (s := reader.read(record)[1])]

    assert [s.pid for s in sentences if s.what == log_lifecycle.STARTED] == entry["pids"]
    assert [(s.what, s.pid) for s in sentences if s.what != log_lifecycle.STARTED] == [
        (log_lifecycle.DIED, endings["am crash"]),
        (log_lifecycle.KILLED, endings["am force-stop"]),
    ]
    assert {(s.name, s.uid) for s in sentences if s.what != log_lifecycle.DIED} == {
        (PACKAGE, 10205)
    }
    assert all(reader.is_package(s) for s in sentences if s.what != log_lifecycle.DIED)


def test_the_writer_is_the_records_pid(recorded):
    """logd's pid, from the writing socket -- never text a tag or message can carry."""
    start = _record(recorded, "Start proc ")

    writer, sentence = _reader(recorded).read(start)

    assert writer == str(start.pid) == _system_server(recorded)
    assert sentence is not None and sentence.what == log_lifecycle.STARTED


@pytest.mark.parametrize("where", ["after the caller's text", "on a later line"])
def test_a_sentence_counts_only_as_the_exact_opening(where, recorded):
    """system_server's own messages carry an app's text further on.

    Derived: system_server's recorded ``Force stopping`` record with the
    recorded ``Start proc`` sentence appended -- after the package, which the
    force-stop's caller chose, or after a newline: a later line of the same
    message, which in text was a line of its own under system_server's header.
    """
    force = _record(recorded, "Force stopping ")
    sentence = _record(recorded, "Start proc ").message
    head = force.message.split(PACKAGE, 1)[0] + PACKAGE
    forged = replace(
        force,
        message=(
            f"{head} x {sentence}"
            if where == "after the caller's text"
            else f"{force.message}\n{sentence}"
        ),
    )

    assert _reader(recorded).read(forged)[1] is None


def test_a_sentence_counts_only_under_activitymanagers_tag(recorded):
    """system_server writes under many tags, and some carry an app's text.

    Derived: system_server's recorded ``Start proc`` record with only its tag
    changed to WindowManager's -- system_server's writer, its sentence, not
    ActivityManager's record.
    """
    start = _record(recorded, "Start proc ")

    assert _reader(recorded).read(replace(start, tag="WindowManager"))[1] is None


def test_a_start_proc_forged_in_a_tag_is_not_read(recorded):
    """The tag-injection regression for the lifecycle reader (A1, adopted from W3-L).

    The recorded forging record, unchanged: its tag is not ActivityManager and
    its writer is the shell, so it names nothing -- whatever its text claims.
    """
    forging = _forging(recorded)

    writer, sentence = _reader(recorded).read(forging)

    assert (writer, sentence) == (str(forging.pid), None)


def test_a_private_process_is_not_its_package(recorded):
    """The process must be exactly a package: ``<package>:<name>`` is not named.

    Derived: the recorded ``Start proc`` with only the process name given a
    private suffix (the uid stays the package's).
    """
    start = _record(recorded, "Start proc ")
    private = replace(
        start, message=start.message.replace(f":{PACKAGE}/", f":{PACKAGE}:remote/", 1)
    )
    reader = _reader(recorded)

    sentence = reader.read(private)[1]

    assert sentence is not None and sentence.name == f"{PACKAGE}:remote"
    assert reader.is_package(sentence) is False


def test_system_server_and_uids_are_asked_only_when_needed(recorded):
    asked: list[str] = []
    reader = log_lifecycle.LifecycleReader(
        lambda: asked.append("system_server") or _system_server(recorded),
        lambda package: asked.append(package) or _uids(recorded, package),
    )
    jank = _record(recorded, "Skipped", tag="Choreographer")

    reader.read(jank)
    assert asked == []
    _writer, sentence = reader.read(_record(recorded, "Start proc "))
    assert asked == ["system_server"]
    reader.is_package(sentence)
    reader.is_package(sentence)
    assert asked == ["system_server", PACKAGE]


# --- one reading, two readers: the same pids ---------------------------------------


def _pid_of(recorded, ending: str) -> str:
    return dict(zip(_entry(recorded)["pid_endings"], _entry(recorded)["pids"], strict=True))[ending]


def _start_of(recorded, ending: str) -> LogRecord:
    return _record(recorded, f"Start proc {_pid_of(recorded, ending)}:")


def _variants(recorded) -> dict[str, list[LogRecord]]:
    """The recording, and the recording with one sentence forged a field at a time."""
    records = _records(recorded)
    alive = _start_of(recorded, "running when the window was read")
    alive_pid = _pid_of(recorded, "running when the window was read")
    force = _record(recorded, "Force stopping ")

    def swapped(old: LogRecord, new: LogRecord) -> list[LogRecord]:
        return [new if record == old else record for record in records]

    def inserted(after: LogRecord, new: LogRecord) -> list[LogRecord]:
        at = records.index(after)
        return [*records[: at + 1], new, *records[at + 1 :]]

    return {
        "as recorded": records,
        # The live app writes system_server's sentence about itself.
        "start written by the app": swapped(alive, replace(alive, pid=int(alive_pid))),
        # system_server's own record for a process run under another uid.
        "start under another uid": swapped(
            alive, replace(alive, message=alive.message.replace("/u0a205 ", "/u0a999 "))
        ),
        # A later line of system_server's message, starting with an app's text:
        # the alive process's sentence, naming a pid nothing in the window holds.
        "start on a later line": swapped(
            force,
            replace(
                force, message=f"{force.message}\n{alive.message.replace(alive_pid, VICTIM, 1)}"
            ),
        ),
        # The recorded tag injection, as recorded: a header forged in a tag.
        "start forged in a tag": inserted(force, _forging(recorded)),
    }


def _named_by_the_window(recorded, records: list[LogRecord]) -> set[str]:
    """log_monitor's reader: the pids the log itself showed were the package's."""
    return logcat.app_window(
        records,
        PACKAGE,
        system_server_pid=_system_server(recorded),
        package_uids=lambda: _uids(recorded, PACKAGE),
    ).pids_from_log


def _named_by_the_session(recorded, records: list[LogRecord]) -> set[str]:
    """The ANR worker's reader: the pids it named the package at any point."""
    names = anr_watcher._ProcessNames(
        system_server_pid=lambda: _system_server(recorded),
        package_uids=lambda package: _uids(recorded, package),
    )
    victim = _entry(recorded, INJECTION)["injection"]["victim_pid"]
    candidates = {str(record.pid) for record in records} | {VICTIM, victim}
    named: set[str] = set()
    for record in records:
        names.observe(record)
        named |= {pid for pid in candidates if names.name_of(int(pid)) == PACKAGE}
    return named


@pytest.mark.parametrize(
    "variant",
    [
        "as recorded",
        "start written by the app",
        "start under another uid",
        "start on a later line",
        "start forged in a tag",
    ],
)
def test_both_readers_name_the_same_pids(variant, recorded):
    """Parity: the same recorded records through both consumers name the same pids."""
    records = _variants(recorded)[variant]

    by_window = _named_by_the_window(recorded, records)
    by_session = _named_by_the_session(recorded, records)

    assert by_window == by_session, (variant, by_window, by_session)
    expected = set(_entry(recorded)["pids"])
    if variant in ("start written by the app", "start under another uid"):
        expected -= {_pid_of(recorded, "running when the window was read")}
    assert by_window == expected, by_window


def test_a_killed_pid_is_not_named_for_what_comes_after(recorded):
    """Review of PR #34: a kill missed as a 'continuation' left the pid named.

    In text, a `Force stopping` and its `Killing` could share a header at
    millisecond resolution, and the second then read as the first's
    continuation. As records they are two records whatever their times.
    Derived: the crashed process's recorded jank record, with only its pid set
    to the killed process's and its time moved 20 s past the kill.
    """
    pid = _pid_of(recorded, "am force-stop")
    killed = _start_of(recorded, "am force-stop")
    force = _record(recorded, "Force stopping ")
    killing = _record(recorded, f"Killing {pid}:")
    jank = _record(recorded, "Skipped", tag="Choreographer")
    later = replace(_later(killing, 20), pid=int(pid), tag=jank.tag, message=jank.message)
    names = anr_watcher._ProcessNames(
        system_server_pid=lambda: _system_server(recorded),
        package_uids=lambda package: _uids(recorded, package),
    )

    for record in (killed, force):
        names.observe(record)
    assert names.name_of(int(pid)) == PACKAGE
    names.observe(killing)
    names.observe(later)

    assert names.name_of(int(pid)) is None


def test_the_live_and_since_reads_ask_for_records():
    """logd's records, not text: the writer is the record's pid, which no tag can forge."""
    watcher = anr_watcher.AnrWatcher(serial="emulator-5554")

    for cmd in (watcher.build_logcat_command(), watcher.build_logcat_command(since="x")):
        assert logcat.BINARY_FLAG in cmd and "-v" not in cmd, cmd
