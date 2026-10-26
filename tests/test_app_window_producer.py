"""An app's log window cannot be widened by what an app writes (D4, security reviews).

``common.logcat.app_window`` decides which pids were an app's from three
sentences system_server writes. log_monitor ``--app`` and app_state_capture
both keep a window to exactly those pids. The tag on a record proves nothing:
any app may log under ``ActivityManager``, and one crafted ``Start proc
<N>:<package>/`` would pull process N's records -- another app's,
system_server's -- into the window. So a sentence counts only when
system_server wrote it. When system_server's pid cannot be learned, none
counts (fail closed): the window narrows to what ``pidof`` names. A crash's
own ``AndroidRuntime: Process: <package>, PID: N`` never counts on its own:
any app can write it, naming another package and its own pid, through the
public Log API (Codex review of #27, ruling A).

**Who wrote a record is logd's word, not the text's** (review of v0.8.0, A1).
In threadtime text a TAG an app chooses can hold a newline and a whole
system_server header, so one app record prints as two lines, the second
system_server's to any text parser (recorded: logcat_tag_injection). The
readers take logd's records (``logcat -B``), whose pid and uid logd took
from the writing socket's credentials; the tests below read them too.

The spoofed records are DERIVED from the recording (CLAUDE.md allows
derivation by substitution): a recorded record with its writer's pid, and
where stated its message, replaced (``fake_toolchain.Frame``).
system_server's pid is the one ``pidof system_server`` answered in the run
that recorded logcat_app_restart_records (its MANIFEST entry).
"""

from __future__ import annotations

import json
import re
import subprocess
from types import SimpleNamespace

import app_state_capture
import pytest
from fake_toolchain import (
    FakeToolchain,
    Frame,
    Rule,
    framed,
    frames,
    recorded_package_listing,
    recorded_ps_answer,
    uid_rules,
)

from common import logcat
from common.device_utils import parse_package_uids

PACKAGE = "com.example.composefixture"
CRASHED, STOPPED, ALIVE = "am crash", "am force-stop", "running when the window was read"
RECORDS = "logcat_app_restart_records"
INJECTION = "logcat_tag_injection_records"

# The threadtime layout, read for PID only.
_THREADTIME = re.compile(r"^\d{2}-\d{2}\s+\S+\s+(?P<pid>\d+)\s+\d+\s+[VDIWEF]\s")


def _entry(recorded, name: str = RECORDS) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == name)


def _pids(recorded) -> dict[str, str]:
    entry = _entry(recorded)
    return dict(zip(entry["pid_endings"], entry["pids"], strict=True))


def _system_server(recorded, name: str = RECORDS) -> str:
    pid = str(_entry(recorded, name).get("system_server_pid", ""))
    if not pid.isdigit():
        pytest.fail(f"{name} no longer records system_server's pid; re-record it")
    return pid


def _package_uid(recorded, name: str = RECORDS) -> frozenset[int]:
    """The package's uid, as `cmd package list packages -U` answered in the same run."""
    uid = parse_package_uids(_entry(recorded, name)["package_uid_listing"]["output"], PACKAGE)
    if uid is None:
        pytest.fail(f"{name} no longer records the package's uid; re-record it")
    return uid


def _oracles(recorded, name: str = RECORDS) -> dict:
    """Both oracles recorded with the window: system_server's pid, the package's uid."""
    return {
        "system_server_pid": _system_server(recorded, name),
        "package_uids": _package_uid(recorded, name),
    }


def _window(recorded) -> list[Frame]:
    return frames(recorded.data(f"{RECORDS}.bin"))


def _records(window: list[Frame]) -> list[logcat.LogRecord]:
    return logcat.parse_records(framed(window))


def _found(window: list[Frame], recorded, **oracles) -> set[str]:
    return logcat.parse_package_pids(_records(window), PACKAGE, **(oracles or _oracles(recorded)))


def _frame(recorded, opening: str, tag: str = "ActivityManager") -> Frame:
    """The first recorded record under ``tag`` whose message opens with ``opening``."""
    found = next(
        (f for f in _window(recorded) if f.tag == tag and f.message.startswith(opening)), None
    )
    if found is None:
        pytest.fail(f"{RECORDS} no longer holds a {tag} record opening {opening!r}")
    return found


def _spoofed_start(recorded, victim: str) -> Frame:
    """The live app writing system_server's sentence about a process it is not.

    The recorded ``Start proc <alive>`` record with TWO substitutions: its
    writer (system_server) becomes the alive app, and the pid the sentence
    names becomes ``victim``.
    """
    alive = _pids(recorded)[ALIVE]
    start = _frame(recorded, f"Start proc {alive}:")
    return start.replace(
        pid=alive, message=start.message.replace(f"Start proc {alive}:", f"Start proc {victim}:")
    )


def _victim(recorded) -> str:
    """A process in the window that is neither the app nor system_server."""
    others = (
        {f.pid for f in _window(recorded)}
        - set(_pids(recorded).values())
        - {_system_server(recorded)}
    )
    if not others:
        pytest.fail("the recording has no third process to be the victim")
    return min(others, key=int)


def _window_pids(lines) -> set[str]:
    return {m["pid"] for m in map(_THREADTIME.match, lines) if m}


# --- the parser ---------------------------------------------------------------


def test_system_servers_own_sentence_counts(recorded):
    stopped = _pids(recorded)[STOPPED]
    assert _found([_frame(recorded, f"Killing {stopped}:")], recorded) == {stopped}


@pytest.mark.parametrize("statement", ["Start proc", "Killing"])
def test_an_activitymanager_sentence_from_another_process_does_not_count(statement, recorded):
    """The recorded record, its writer swapped for the live app.

    (`has died` names no pid on its own -- it carries no uid -- so its spoof
    is tested by what it would END: the next test but one.)
    """
    pids = _pids(recorded)
    genuine = next(
        (
            f
            for f in _window(recorded)
            if f.tag == "ActivityManager"
            and f.message.startswith(statement)
            and PACKAGE in f.message
        ),
        None,
    )
    if genuine is None:
        pytest.fail(f"{RECORDS} no longer holds an ActivityManager {statement!r} record")
    assert _found([genuine], recorded)
    assert _found([genuine.replace(pid=pids[ALIVE])], recorded) == set()


@pytest.mark.parametrize("statement", ["Start proc", "Killing"])
def test_a_start_or_kill_with_another_uid_is_not_the_apps(statement, recorded):
    """A process named after the package but run under another uid is an impostor.

    Any app may name its own process after another package
    (`android:process`), and system_server then really writes `Start proc
    N:<package>/<the impostor's uid>`. Derived: the recorded record, written
    by system_server, with only the uid in its message substituted.
    """
    pids = _pids(recorded)
    pid = pids[ALIVE] if statement == "Start proc" else pids[STOPPED]
    genuine = _frame(recorded, f"{statement} {pid}:")
    assert _found([genuine], recorded) == {pid}
    impostor = genuine.message.replace(f"{pid}:{PACKAGE}/u0a205 ", f"{pid}:{PACKAGE}/u0a999 ")
    if impostor == genuine.message:
        pytest.fail("the recorded record no longer carries the package's uid as u0a205")
    assert _found([genuine.replace(message=impostor)], recorded) == set()


def test_only_the_opening_of_a_records_message_names_a_process(recorded):
    """A later line of a system_server message names nothing, whatever it says.

    logcat prints a multi-line message as several lines under one header, so
    a later line of a system_server message can start with any text an app
    put into it. Records end that question: the record says where its
    message begins. Derived: system_server's recorded `Force stopping` record
    with a second line appended to its message, holding a `Start proc`
    sentence for another process. As the second line it names nothing; the
    same sentence opening the record (the control) does.
    """
    victim = _victim(recorded)
    force = _frame(recorded, f"Force stopping {PACKAGE}")
    sentence = f"Start proc {victim}:{PACKAGE}/u0a205 for x"
    later = force.replace(message=f"{force.message}\n{sentence}")
    assert _found([later], recorded) == set()
    assert _found([force.replace(message=sentence)], recorded) == {victim}


@pytest.mark.parametrize(("writer", "ends"), [("system_server", True), ("app", False)])
def test_has_died_ends_a_confirmed_life_only_when_system_server_writes_it(writer, ends, recorded):
    """`has died` carries no uid: it never names a pid, it only ends a confirmed life.

    Derived: the recorded `has died` record for the crashed process, the pid
    it names replaced by the live one's, inserted right after the live
    process's `Start proc`. Written by system_server, it ends that life there,
    and the live process's records drop out; written by the live app itself
    (the writer substituted), it ends nothing.
    """
    pids = _pids(recorded)
    alive, crashed = pids[ALIVE], pids[CRASHED]
    died = _frame(recorded, f"Process {PACKAGE} (pid {crashed}) has died")
    died = died.replace(message=died.message.replace(f"(pid {crashed})", f"(pid {alive})"))
    if writer == "app":
        died = died.replace(pid=alive)
    window = _window(recorded)
    start = next(i for i, f in enumerate(window) if f.message.startswith(f"Start proc {alive}:"))
    derived = [*window[: start + 1], died, *window[start + 1 :]]
    kept = logcat.app_window(
        _records(derived), PACKAGE, **_oracles(recorded), running=[alive]
    ).records
    alive_records = [record for record in kept if str(record.pid) == alive]
    assert (alive_records == []) is ends, f"{len(alive_records)} live-process records kept"


def _victim_package(recorded) -> str:
    """Another app's package, as the recording names it (aliased by the recorder)."""
    other = next(
        (
            token
            for f in _window(recorded)
            for token in re.findall(r"com\.example\.(?:private|app)\d+", f.message)
        ),
        None,
    )
    if other is None:
        pytest.fail("the recording no longer names another app's package")
    return other


def _attacker_crash(recorded, victim_package: str) -> Frame:
    """The recorded crash record, with ONLY the package it names changed.

    Written by the crashed process about itself, so its writer still equals
    the pid it names: the shape any app can produce for any package through
    the public ``android.util.Log`` API (Codex review of #27).
    """
    crashed = _pids(recorded)[CRASHED]
    crash = next(
        (
            f
            for f in _window(recorded)
            if f.tag == "AndroidRuntime" and f"Process: {PACKAGE}, PID: {crashed}" in f.message
        ),
        None,
    )
    if crash is None:
        pytest.fail(f"{RECORDS} no longer holds the crashed process's AndroidRuntime record")
    return crash.replace(
        message=crash.message.replace(f"Process: {PACKAGE},", f"Process: {victim_package},")
    )


def test_only_the_opening_sentence_of_a_system_server_record_counts(recorded):
    """A sentence embedded later in a genuine system_server message names nothing.

    system_server's messages carry app-controlled text after their opening --
    here the component inside ``{...}`` -- and the writer check passes, since
    system_server really wrote the record (security review of #27). Derived
    from the recording, the writer kept:

    - the ``Start proc <crashed>`` record with a second ``Start proc
      <victim>:`` inside its component: only the leading pid counts;
    - the ``Force stopping`` record, which opens with no attributing
      sentence, with one embedded after ``user=0``: nothing counts.
    """
    pids = _pids(recorded)
    victim = _victim(recorded)
    start = _frame(recorded, f"Start proc {pids[CRASHED]}:")
    crafted = start.message.replace(
        "DefaultActivity}", f"DefaultActivity x Start proc {victim}:{PACKAGE}/u0a205 }}"
    )
    if crafted == start.message:
        pytest.fail("the recorded Start proc record no longer ends in its component")
    assert _found([start.replace(message=crafted)], recorded) == {pids[CRASHED]}

    force = _frame(recorded, f"Force stopping {PACKAGE}")
    crafted_force = force.message.replace(
        " user=0: ", f" user=0 x Start proc {victim}:{PACKAGE}/u0a205 for x: "
    )
    if crafted_force == force.message:
        pytest.fail("the recorded Force stopping record no longer reads `user=0: `")
    assert _found([force.replace(message=crafted_force)], recorded) == set()


def test_a_crash_record_attributes_nothing_on_its_own(recorded):
    """AndroidRuntime confirms, never sources (ruling A on Codex's P1).

    The attacker's record -- the recorded crash naming another package --
    claims nothing for that package. The genuine record claims nothing alone
    either; the genuine crash is attributed by system_server's `Start proc`
    for the same pid, which the whole recording carries.
    """
    pids = _pids(recorded)
    victim = _victim_package(recorded)
    attack = _attacker_crash(recorded, victim)
    assert logcat.parse_package_pids(_records([attack]), victim, **_oracles(recorded)) == set()
    genuine = _attacker_crash(recorded, PACKAGE)
    assert _found([genuine], recorded) == set()
    assert pids[CRASHED] in _found(_window(recorded), recorded)


@pytest.mark.parametrize(
    "unknown", [None, lambda: None], ids=["not-given", "lookup-answered-nothing"]
)
def test_with_no_system_server_known_activitymanager_attributes_nothing(unknown, recorded):
    """Fail closed: an unverifiable claim about another process is not evidence.

    With system_server's pid unknown, the whole recorded window -- genuine
    system_server records, the genuine crash and a spoofed claim alike --
    attributes nothing; only ``pidof`` can then name a process.
    """
    window = [*_window(recorded), _spoofed_start(recorded, _victim(recorded))]
    found = logcat.parse_package_pids(
        _records(window), PACKAGE, system_server_pid=unknown, package_uids=_package_uid(recorded)
    )
    assert found == set()


@pytest.mark.parametrize(
    "unknown", [None, lambda: None], ids=["not-given", "lookup-answered-nothing"]
)
def test_with_no_package_uid_known_nothing_is_attributed(unknown, recorded):
    """Fail closed on the uid too: no start or kill can be checked against it."""
    found = logcat.parse_package_pids(
        _records(_window(recorded)),
        PACKAGE,
        system_server_pid=_system_server(recorded),
        package_uids=unknown,
    )
    assert found == set()


def test_system_server_is_asked_only_when_the_answer_matters(recorded):
    """A crash-only window needs no lookup; a full one needs exactly one."""
    asked: list[int] = []

    def ask() -> str:
        asked.append(1)
        return _system_server(recorded)

    uid = _package_uid(recorded)
    crashes = logcat.parse_records(recorded.data("logcat_crash_buffer_records.bin"))
    logcat.parse_package_pids(crashes, PACKAGE, system_server_pid=ask, package_uids=uid)
    assert asked == []

    found = logcat.parse_package_pids(
        _records(_window(recorded)), PACKAGE, system_server_pid=ask, package_uids=uid
    )
    assert asked == [1]
    assert found == set(_pids(recorded).values())


# --- A1: a tag cannot make one record two lines --------------------------------


def _injection(recorded) -> dict:
    """The injection's oracles: the app's pid, system_server's, the forger and victim."""
    entry = _entry(recorded, INJECTION)
    injected = entry.get("injection") or {}
    if not (entry.get("pids") and injected.get("victim_pid") and injected.get("forger_pid")):
        pytest.fail(f"{INJECTION} no longer records the injection's pids; re-record it")
    return {
        "alive": entry["pids"][0],
        "victim": injected["victim_pid"],
        "forger": injected["forger_pid"],
        "system_server": _system_server(recorded, INJECTION),
    }


def test_the_recorded_text_shows_the_forged_system_server_line(recorded):
    """The premise, from the device's own text: the forgery is real in threadtime.

    One record of the forging shell prints as two lines, and the second is,
    to a threadtime parser, system_server's ActivityManager starting the
    victim as the app's process.
    """
    oracle = _injection(recorded)
    forged = [
        line
        for line in recorded.lines("logcat_tag_injection")
        if f"ActivityManager: Start proc {oracle['victim']}:" in line
    ]
    if len(forged) != 1:
        pytest.fail(f"logcat_tag_injection no longer shows one forged line: {forged}")
    assert logcat.parse_threadtime(forged[0]) == (oracle["system_server"], "I")


def test_a_tag_cannot_forge_system_servers_word(recorded):
    """Read as records, the forging record is the shell's, and names no one.

    The app's own `Start proc` still counts; the victim is not the app's.
    """
    oracle = _injection(recorded)
    records = logcat.parse_records(recorded.data(f"{INJECTION}.bin"))
    found = logcat.parse_package_pids(records, PACKAGE, **_oracles(recorded, INJECTION))
    assert oracle["victim"] not in found
    assert found == {oracle["alive"]}


def test_a_forging_tag_prints_as_one_line_with_its_newline_escaped(recorded):
    """The record prints as the device printed it -- except that it is one line.

    Every other line of the window is the device's own text, byte for byte
    (test_log_records.py holds the rest of that); the forging record's tag is
    printed with its newline escaped, so nothing after it can be read as a
    header.
    """
    oracle = _injection(recorded)
    records = logcat.parse_records(recorded.data(f"{INJECTION}.bin"))
    forging = [record for record in records if str(record.pid) == oracle["forger"]]
    assert len(forging) == 1
    (line,) = forging[0].lines()
    assert "\n" not in line and "x:\\n" in line
    assert logcat.parse_threadtime(line) == (oracle["forger"], "E")


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _injection_device(toolchain, recorded) -> None:
    """The emulator of the injection: its records under `-B`, its own text otherwise.

    Both halves of one recorded run. A reader asking for text gets what the
    device printed (the forged line in it); one asking for records gets them.
    """
    oracle = _injection(recorded)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
            Rule(("shell", "pidof", "system_server"), stdout=oracle["system_server"]),
            *uid_rules(recorded, PACKAGE, oracle["alive"]),
            Rule(("shell", "pidof"), stdout=oracle["alive"]),
            # A live read follows: it replays, then waits until --duration stops it.
            Rule(
                ("logcat", "-B"),
                stdout=recorded.data(f"{INJECTION}.bin"),
                filter_pid=True,
                follows=True,
            ),
            Rule(("logcat",), stdout=recorded.text("logcat_tag_injection"), filter_pid=True),
        ],
    )


@pytest.mark.parametrize("read", [("--last", "5m"), ("--duration", "2s")], ids=["last", "live"])
def test_a1_log_monitor_keeps_a_forging_tag_out_of_the_apps_window(read, toolchain, recorded):
    """A1, end to end: the recorded injection does not add its victim to the app.

    Over text (v0.8.0) the forged line put the victim's pid in ``app_pids``
    and its line in the window, through both reads.
    """
    oracle = _injection(recorded)
    _injection_device(toolchain, recorded)
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, *read, "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["app_pids"] == [oracle["alive"]]
    assert oracle["victim"] not in payload["attribution"]["pids_from_log"]
    assert _window_pids(payload["sample_logs"]) == {oracle["alive"]}


def test_a1_app_state_capture_keeps_a_forging_tag_out_of_the_apps_window(
    monkeypatch, toolchain, tmp_path, recorded
):
    """A1 through app_state_capture's log capture, on the real subprocess path."""
    oracle = _injection(recorded)
    _injection_device(toolchain, recorded)
    toolchain.apply(monkeypatch)
    capturer = app_state_capture.AppStateCapture(package=PACKAGE, serial="emulator-5554")
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    if not any("logcat" in call for call in toolchain.calls("adb")):
        pytest.fail("the capture never ran the stub adb's logcat")
    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert stats["pids"] == [oracle["alive"]]
    assert _window_pids(saved) == {oracle["alive"]}


# --- log_monitor --app --------------------------------------------------------


def _install(toolchain, recorded, window: list[Frame], *, system_server: str | None) -> None:
    alive = _pids(recorded)[ALIVE]
    rules = [
        Rule(("devices",), stdout=recorded.text("adb_devices_single")),
        Rule(("get-state",), stdout=recorded.text("adb_get_state")),
        Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
    ]
    if system_server is not None:
        rules.append(Rule(("shell", "pidof", "system_server"), stdout=system_server))
    rules += uid_rules(recorded, PACKAGE, alive)
    rules += [
        Rule(("shell", "pidof"), stdout=alive),
        # A live read follows: it replays, then waits until --duration stops it.
        Rule(("logcat",), stdout=framed(window), filter_pid=True, follows=True),
    ]
    toolchain.install("adb", rules)


def test_log_monitor_keeps_only_the_pidof_pid_with_the_packages_uid(toolchain, recorded):
    """`pidof` matches a process NAME; `ps` says whose process it is.

    `pidof` answers the live app's pid and system_server's (another app may
    name a process after the package). `ps` answers as recorded (ps_pid_uid:
    the package's uid for one, 1000 for system_server). Only the first is the
    app's: system_server's records stay out, and the JSON says which was left.
    """
    pids, system_server = _pids(recorded), _system_server(recorded)
    alive = pids[ALIVE]
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
            Rule(("shell", "pidof", "system_server"), stdout=system_server),
            *uid_rules(recorded, PACKAGE, alive),
            Rule(("shell", "pidof"), stdout=" ".join((alive, system_server))),
            Rule(("logcat",), stdout=recorded.data(f"{RECORDS}.bin"), filter_pid=True),
        ],
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert system_server not in payload["app_pids"]
    assert payload["attribution"]["pidof_rejected"] == [system_server]
    assert payload["attribution"]["pids_from_pidof"] == [alive]
    assert system_server not in _window_pids(payload["sample_logs"])


def test_log_monitor_does_not_take_an_apps_word_for_another_process(toolchain, recorded):
    victim = _victim(recorded)
    window = [*_window(recorded), _spoofed_start(recorded, victim)]
    _install(toolchain, recorded, window, system_server=_system_server(recorded))

    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert victim not in payload["app_pids"]
    assert _window_pids(payload["sample_logs"]) == set(_pids(recorded).values())


def test_log_monitor_without_system_server_uses_no_activitymanager_record(toolchain, recorded):
    """No usable ``pidof system_server`` answer: fail closed, and say so.

    The stub's ``pidof`` rule answers every name with the live app's pid, so
    the system_server question gets the app's own pid back -- which one
    process cannot be, so it is no answer. The window is then the live process
    ``pidof`` named, alone: not the crashed or force-stopped processes, which
    only ActivityManager names, and not the spoofed victim.
    """
    pids = _pids(recorded)
    victim = _victim(recorded)
    window = [*_window(recorded), _spoofed_start(recorded, victim)]
    _install(toolchain, recorded, window, system_server=None)

    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    assert "cannot be verified and are not used" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["app_pids"] == [pids[ALIVE]]
    assert _window_pids(payload["sample_logs"]) == {pids[ALIVE]}


def _victim_uid(recorded, victim_pid: str) -> int:
    """The uid logd stamped on the victim process's records."""
    uids = {f.uid for f in _window(recorded) if f.pid == victim_pid}
    if len(uids) != 1:
        pytest.fail(f"the victim {victim_pid}'s records carry uids {uids}, not one")
    return uids.pop()


def _victim_uid_answers(recorded, victim_package: str, victim_pid: str) -> tuple[str, str]:
    """``cmd package list packages -U`` and ``ps`` that make the victim one consistent app.

    The recorded answers (package_list_uid, ps_pid_uid) with the package, the
    pid, and the uid -- the one logd stamped on the victim's records --
    substituted: the victim package is the victim process's, by every witness.
    """
    recorded_uid = next(iter(_package_uid(recorded)))
    uid = _victim_uid(recorded, victim_pid)
    listing = recorded_package_listing(recorded, victim_package).replace(
        f" uid:{recorded_uid}", f" uid:{uid}"
    )
    ps = re.sub(
        rf"(?m)^(\s*{victim_pid}\s+){recorded_uid}$",
        lambda m: m[1] + str(uid),
        recorded_ps_answer(recorded, victim_pid),
    )
    if str(uid) not in listing or f"{uid}" not in ps:
        pytest.fail("the recorded uid answers no longer take the substitution")
    return listing, ps


def test_log_monitor_keeps_an_attackers_crash_record_out_of_its_victims_window(toolchain, recorded):
    """Codex's P1 attack, end to end: the crashed process claims another package.

    The victim is another app named in the recording, running as a process of
    the window (``pidof`` answers it, ``ps`` and the package listing give it
    the uid logd stamped on its records). The attacker -- the crashed process
    -- adds the recorded crash record with only the package changed. Before
    ruling A that admitted the attacker's pid and its records into the
    victim's window; the window is the victim's own records, and only them.
    """
    pids = _pids(recorded)
    victim_package, victim_pid = _victim_package(recorded), _victim(recorded)
    window = [*_window(recorded), _attacker_crash(recorded, victim_package)]
    listing, ps = _victim_uid_answers(recorded, victim_package, victim_pid)
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
            Rule(("shell", "pidof", "system_server"), stdout=_system_server(recorded)),
            Rule(("cmd", "package"), stdout=listing),
            Rule(("shell", "ps"), stdout=ps),
            Rule(("shell", "pidof"), stdout=victim_pid),
            Rule(("logcat",), stdout=framed(window), filter_pid=True),
        ],
    )

    # --severity verbose: every record the victim wrote in this window is V,
    # below the default floor.
    result = toolchain.run(
        "log_monitor.py",
        "--app",
        victim_package,
        "--severity",
        "verbose",
        "--last",
        "5m",
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert pids[CRASHED] not in payload["app_pids"]
    assert payload["app_pids"] == [victim_pid]
    assert _window_pids(payload["sample_logs"]) == {victim_pid}


def _carriage_return_forgery(recorded) -> tuple[list[Frame], str]:
    """The restart window where a third process forges a line after a carriage return.

    Derived: the first recorded record of a third process (``_victim``:
    neither the app nor system_server) gets "\\r" and the recorded
    ``Start proc <alive>`` line -- system_server's header and all, as the
    device printed it -- appended to its message, with the pid that line names
    replaced by the third process's own. One record of the third process; a
    reader that broke text at "\\r" read system_server starting the third
    process as the app (Codex review v3). Returns the window and the forger.
    """
    forger, alive = _victim(recorded), _pids(recorded)[ALIVE]
    start = next(
        (
            line
            for line in recorded.lines("logcat_app_restart_usec")
            if f"ActivityManager: Start proc {alive}:" in line
        ),
        None,
    )
    if start is None:
        pytest.fail("logcat_app_restart_usec no longer prints the live process's start")
    forged = start.replace(f"Start proc {alive}:", f"Start proc {forger}:")
    window = _window(recorded)
    first = next(i for i, f in enumerate(window) if f.pid == forger)
    window[first] = window[first].replace(message="\r".join((window[first].message, forged)))
    return window, forger


@pytest.mark.parametrize("read", [("--last", "5m"), ("--duration", "2s")], ids=["last", "live"])
def test_log_monitor_reads_a_carriage_return_as_part_of_its_record(read, toolchain, recorded):
    """A "\\r" inside an app's message does not start a line system_server wrote.

    The forgery reaches the script byte for byte, through the stub ``adb`` and
    the real pipe; the forger is not the app's, and the window is the app's
    three processes, in a historical read and live.
    """
    window, forger = _carriage_return_forgery(recorded)
    _install(toolchain, recorded, window, system_server=_system_server(recorded))
    replayed = subprocess.run(
        ["adb", "logcat", "-d", "-B"], env=toolchain.env(), capture_output=True, check=False
    ).stdout
    if replayed != framed(window):
        pytest.fail("the fake adb does not replay the forgery byte for byte")

    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, *read, "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert forger not in payload["app_pids"]
    assert forger not in payload["attribution"]["pids_from_log"]
    assert _window_pids(payload["sample_logs"]) == set(_pids(recorded).values())


def _second_user_uid(recorded) -> int:
    """The package's uid in the recording's second user (package_list_uid_two_users)."""
    both = parse_package_uids(recorded.text("package_list_uid_two_users"), PACKAGE)
    if both is None or len(both - _package_uid(recorded)) != 1:
        pytest.fail("package_list_uid_two_users no longer lists a second user's uid")
    (second,) = both - _package_uid(recorded)
    return second


def test_the_package_listing_is_read_as_a_set_of_uids(recorded):
    """One row per package, whatever the users: a second user adds a uid to the row.

    Both rows are recorded (package_list_uid, package_list_uid_two_users).
    Every uid is the same app in another user: one app id (``uid % 100000``).
    """
    one = parse_package_uids(recorded.text("package_list_uid"), PACKAGE)
    two = parse_package_uids(recorded.text("package_list_uid_two_users"), PACKAGE)
    assert one is not None and len(one) == 1
    assert two is not None and len(two) == 2 and one < two
    assert len({uid % 100_000 for uid in two}) == 1


def test_log_monitor_keeps_the_app_running_as_its_second_user(toolchain, recorded):
    """A process with the package's second-user uid is the app's: by ``ps``, and by the log.

    Derived: the live process started as the second user -- its ``Start
    proc`` names the uid as UserHandle.formatUid writes it (``u<user>a<app
    id - 10000>``), and ``ps`` answers that uid for it -- with the listing as
    recorded for two users. Its records still carry the first user's uid as
    logd recorded them; both uids are the package's. Keeping only the
    listing's first uid left this process out of its own app's window (Codex
    review v3).
    """
    pids, system_server = _pids(recorded), _system_server(recorded)
    alive = pids[ALIVE]
    (first,) = _package_uid(recorded)
    second = _second_user_uid(recorded)
    user, app_id = divmod(second, 100_000)
    started = f"Start proc {alive}:{PACKAGE}/"
    window = _window(recorded)
    at = next(i for i, f in enumerate(window) if f.message.startswith(started))
    message = window[at].message.replace(
        f"{started}u0a{first - 10_000} ", f"{started}u{user}a{app_id - 10_000} "
    )
    if message == window[at].message:
        pytest.fail("the live process's Start proc no longer names the first user's uid")
    window[at] = window[at].replace(message=message)
    ps_answer = re.sub(
        rf"(?m)^(\s*{alive}\s+){first}$",
        lambda m: m[1] + str(second),
        recorded_ps_answer(recorded, alive),
    )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
            Rule(("shell", "pidof", "system_server"), stdout=system_server),
            Rule(("cmd", "package"), stdout=recorded.text("package_list_uid_two_users")),
            Rule(("shell", "ps"), stdout=ps_answer),
            Rule(("shell", "pidof"), stdout=alive),
            Rule(("logcat",), stdout=framed(window), filter_pid=True),
        ],
    )
    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    attribution = payload["attribution"]
    assert attribution["package_uids"] == sorted({first, second})
    assert attribution["pids_from_pidof"] == [alive]
    assert attribution["pidof_rejected"] == []
    assert alive in attribution["pids_from_log"]
    assert _window_pids(payload["sample_logs"]) == set(pids.values())


# --- app_state_capture --------------------------------------------------------


def _uid_answer(recorded, cmd: list[str], pid: str):
    """The recorded uid answers, for an in-process fake: package listing and ``ps``."""
    if "package" in cmd and "-U" in cmd:
        listing = recorded_package_listing(recorded, cmd[-1])
        return SimpleNamespace(stdout=listing, stderr="", returncode=0)
    if "ps" in cmd:
        return SimpleNamespace(stdout=recorded_ps_answer(recorded, pid), stderr="", returncode=0)
    return None


def _capture_with(
    monkeypatch,
    recorded,
    window: bytes,
    *,
    package: str,
    pidof: str,
    uid_answers: tuple[str, str] | None = None,
):
    """``_capture_logs`` over an in-process fake whose logcat answers ``window`` (records).

    ``uid_answers``: the package listing and ``ps`` to answer instead of the
    recorded ones (``_victim_uid_answers``).
    """
    system_server = _system_server(recorded)

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd:
            answer = system_server if "system_server" in cmd else pidof
            return SimpleNamespace(stdout=answer, stderr="", returncode=0)
        if uid_answers is not None and "package" in cmd and "-U" in cmd:
            return SimpleNamespace(stdout=uid_answers[0], stderr="", returncode=0)
        if uid_answers is not None and "ps" in cmd:
            return SimpleNamespace(stdout=uid_answers[1], stderr="", returncode=0)
        if (uid_answer := _uid_answer(recorded, cmd, pidof.split(maxsplit=1)[0])) is not None:
            return uid_answer
        if "logcat" in cmd:
            return SimpleNamespace(stdout=window, stderr=b"", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)
    return app_state_capture.AppStateCapture(package=package, serial="emulator-5554")


def test_app_state_capture_keeps_only_the_pidof_pid_with_the_packages_uid(
    monkeypatch, tmp_path, recorded
):
    """The same `pidof` check through app_state_capture: system_server's pid is not the app's."""
    pids, system_server = _pids(recorded), _system_server(recorded)
    capturer = _capture_with(
        monkeypatch,
        recorded,
        recorded.data(f"{RECORDS}.bin"),
        package=PACKAGE,
        pidof=" ".join((pids[ALIVE], system_server)),
    )
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert system_server not in stats["pids"]
    assert system_server not in _window_pids(saved)


def test_app_state_capture_does_not_take_an_apps_word_for_another_process(
    monkeypatch, tmp_path, recorded
):
    """The same window rule, through app_state_capture's log capture."""
    victim = _victim(recorded)
    window = framed([*_window(recorded), _spoofed_start(recorded, victim)])
    capturer = _capture_with(
        monkeypatch, recorded, window, package=PACKAGE, pidof=_pids(recorded)[ALIVE]
    )
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert victim not in stats["pids"]
    assert _window_pids(saved) == set(_pids(recorded).values())


def test_app_state_capture_keeps_an_attackers_crash_record_out_of_its_victims_window(
    monkeypatch, tmp_path, recorded
):
    """Codex's P1 attack through app_state_capture: the same victim, the same record."""
    pids = _pids(recorded)
    victim_package, victim_pid = _victim_package(recorded), _victim(recorded)
    window = framed([*_window(recorded), _attacker_crash(recorded, victim_package)])
    capturer = _capture_with(
        monkeypatch,
        recorded,
        window,
        package=victim_package,
        pidof=victim_pid,
        uid_answers=_victim_uid_answers(recorded, victim_package, victim_pid),
    )
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert pids[CRASHED] not in stats["pids"]
    assert stats["pids"] == [victim_pid]
    assert _window_pids(saved) == {victim_pid}


def test_app_state_capture_reads_a_carriage_return_as_part_of_its_record(
    monkeypatch, toolchain, tmp_path, recorded
):
    """The same forgery through app_state_capture, on the real subprocess path.

    ``toolchain.apply`` puts the stub ``adb`` first on PATH, so the capture
    runs ``adb logcat`` as a real child and reads its real pipe.
    """
    window, forger = _carriage_return_forgery(recorded)
    _install(toolchain, recorded, window, system_server=_system_server(recorded))
    toolchain.apply(monkeypatch)
    capturer = app_state_capture.AppStateCapture(package=PACKAGE, serial="emulator-5554")
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    if not any("logcat" in call for call in toolchain.calls("adb")):
        pytest.fail("the capture never ran the stub adb's logcat")
    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert forger not in stats["pids"]
    assert _window_pids(saved) == set(_pids(recorded).values())
