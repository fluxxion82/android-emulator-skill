"""An app's log window cannot be widened by what an app writes (D4, security review).

``common.logcat.parse_package_pids`` decides which pids were an app's from four
sentences in the log. log_monitor ``--app`` and app_state_capture both keep a
window to exactly those pids. The tag on a line proves nothing: any app may log
under ``ActivityManager``, and one crafted ``Start proc <N>:<package>/`` would
pull process N's lines -- another app's, system_server's -- into the window.
So a sentence counts only when system_server wrote it. When system_server's
pid cannot be learned, none counts (fail closed): the window narrows to what
``pidof`` names. A crash's own ``AndroidRuntime: Process: <package>, PID: N``
never counts on its own: any app can write it, naming another package and
its own pid, through the public Log API (Codex review of #27, ruling A).

The spoofed lines are DERIVED from the recording (CLAUDE.md allows derivation
by substitution): a recorded line with its PID column, and where stated the pid
it names, replaced. system_server's pid is the one ``pidof system_server``
answered in the run that recorded logcat_app_restart_usec (its MANIFEST entry).
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
    Rule,
    recorded_package_listing,
    recorded_ps_answer,
    uid_rules,
)

from common import logcat
from common.device_utils import parse_package_uids

PACKAGE = "com.example.composefixture"
CRASHED, STOPPED, ALIVE = "am crash", "am force-stop", "running when the window was read"

# The header's PID column, and the threadtime layout read for PID only.
_PID_COLUMN = re.compile(r"^(\S+\s+\S+\s+)(\d+)")
_THREADTIME = re.compile(r"^\d{2}-\d{2}\s+\S+\s+(?P<pid>\d+)\s+\d+\s+[VDIWEF]\s")


def _entry(recorded) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == "logcat_app_restart_usec")


def _pids(recorded) -> dict[str, str]:
    entry = _entry(recorded)
    return dict(zip(entry["pid_endings"], entry["pids"], strict=True))


def _system_server(recorded) -> str:
    pid = str(_entry(recorded).get("system_server_pid", ""))
    if not pid.isdigit():
        pytest.fail("logcat_app_restart_usec no longer records system_server's pid; re-record it")
    return pid


def _package_uid(recorded) -> frozenset[int]:
    """The package's uid, as `cmd package list packages -U` answered in the same run."""
    uid = parse_package_uids(_entry(recorded)["package_uid_listing"]["output"], PACKAGE)
    if uid is None:
        pytest.fail("logcat_app_restart_usec no longer records the package's uid; re-record it")
    return uid


def _oracles(recorded) -> dict:
    """Both oracles recorded with the window: system_server's pid, the package's uid."""
    return {"system_server_pid": _system_server(recorded), "package_uids": _package_uid(recorded)}


def _written_by(line: str, pid: str) -> str:
    """``line`` as if process ``pid`` had logged it: only the PID column changes."""
    return _PID_COLUMN.sub(lambda m: m[1] + pid, line, count=1)


def _recorded_line(recorded, needle: str) -> str:
    line = next(
        (line for line in recorded.lines("logcat_app_restart_usec") if needle in line), None
    )
    if line is None:
        pytest.fail(f"logcat_app_restart_usec no longer holds {needle!r}")
    return line


def _spoofed_start(recorded, victim: str) -> str:
    """The live app writing system_server's sentence about a process it is not.

    The recorded ``Start proc <alive>`` line with TWO substitutions: the PID
    column (system_server's) becomes the alive app's, and the pid the sentence
    names becomes ``victim``. (``_written_by`` alone changes only the PID column.)
    """
    alive = _pids(recorded)[ALIVE]
    start = _recorded_line(recorded, f"ActivityManager: Start proc {alive}:")
    return _written_by(start, alive).replace(f"Start proc {alive}:", f"Start proc {victim}:")


def _victim(recorded) -> str:
    """A process in the window that is neither the app nor system_server."""
    text = recorded.text("logcat_app_restart_usec")
    others = (
        {m["pid"] for m in map(_THREADTIME.match, text.splitlines()) if m}
        - set(_pids(recorded).values())
        - {_system_server(recorded)}
    )
    if not others:
        pytest.fail("the recording has no third process to be the victim")
    return min(others, key=int)


# --- the parser ---------------------------------------------------------------


def test_system_servers_own_sentence_counts(recorded):
    stopped = _pids(recorded)[STOPPED]
    killing = _recorded_line(recorded, f"ActivityManager: Killing {stopped}:")
    found = logcat.parse_package_pids(killing, PACKAGE, **_oracles(recorded))
    assert found == {stopped}


@pytest.mark.parametrize("statement", ["Start proc", "Killing"])
def test_an_activitymanager_sentence_from_another_process_does_not_count(statement, recorded):
    """The recorded line, its PID column swapped for the live app's.

    (`has died` names no pid on its own -- it carries no uid -- so its spoof
    is tested by what it would END: the next test.)
    """
    pids = _pids(recorded)
    line = next(
        (
            line
            for line in recorded.lines("logcat_app_restart_usec")
            if " ActivityManager: " in line and statement in line and PACKAGE in line
        ),
        None,
    )
    if line is None:
        pytest.fail(
            f"logcat_app_restart_usec no longer holds an ActivityManager {statement!r} line"
        )
    assert logcat.parse_package_pids(line, PACKAGE, **_oracles(recorded))
    spoofed = _written_by(line, pids[ALIVE])
    assert logcat.parse_package_pids(spoofed, PACKAGE, **_oracles(recorded)) == set()


@pytest.mark.parametrize("statement", ["Start proc", "Killing"])
def test_a_start_or_kill_with_another_uid_is_not_the_apps(statement, recorded):
    """A process named after the package but run under another uid is an impostor.

    Any app may name its own process after another package
    (`android:process`), and system_server then really writes `Start proc
    N:<package>/<the impostor's uid>`. Derived: the recorded line, written by
    system_server, with only its uid substituted (u0a205 -> u0a999).
    """
    pids = _pids(recorded)
    pid = pids[ALIVE] if statement == "Start proc" else pids[STOPPED]
    line = _recorded_line(recorded, f"ActivityManager: {statement} {pid}:")
    assert logcat.parse_package_pids(line, PACKAGE, **_oracles(recorded)) == {pid}
    impostor = line.replace(f"{pid}:{PACKAGE}/u0a205 ", f"{pid}:{PACKAGE}/u0a999 ")
    if impostor == line:
        pytest.fail("the recorded line no longer carries the package's uid as u0a205")
    assert logcat.parse_package_pids(impostor, PACKAGE, **_oracles(recorded)) == set()


def test_a_continuation_line_names_nothing(recorded):
    """Only the first line of a message may hold a sentence.

    logcat prints a multi-line message as several lines with one header --
    the same to the microsecond (recorded crash traces) -- so a later line of
    a system_server message can start with any text an app put into it.
    Derived: the recorded `Force stopping` line (system_server's), followed by
    a line with its exact header whose text is a `Start proc` sentence. As a
    continuation it names nothing; the same line after a different header
    (the control) would.
    """
    victim = _victim(recorded)
    force = _recorded_line(recorded, f"ActivityManager: Force stopping {PACKAGE}")
    continuation = re.sub(
        r"ActivityManager: .*$",
        f"ActivityManager: Start proc {victim}:{PACKAGE}/u0a205 for x",
        force,
    )
    if continuation == force:
        pytest.fail("the recorded Force stopping line no longer has the expected tag")
    oracles = _oracles(recorded)
    message = "\n".join((force, continuation))
    assert logcat.parse_package_pids(message, PACKAGE, **oracles) == set()
    assert logcat.parse_package_pids(continuation, PACKAGE, **oracles) == {victim}


@pytest.mark.parametrize(("writer", "ends"), [("system_server", True), ("app", False)])
def test_has_died_ends_a_confirmed_life_only_when_system_server_writes_it(writer, ends, recorded):
    """`has died` carries no uid: it never names a pid, it only ends a confirmed life.

    Derived: the recorded `has died` line for the crashed process, its pid
    replaced by the live one's and inserted right after the live process's
    `Start proc`. Written by system_server, it ends that life there, and the
    live process's lines drop out; written by the live app itself (the PID
    column substituted), it ends nothing.
    """
    pids = _pids(recorded)
    alive, crashed = pids[ALIVE], pids[CRASHED]
    died = _recorded_line(recorded, f"(pid {crashed}) has died").replace(
        f"(pid {crashed})", f"(pid {alive})"
    )
    if writer == "app":
        died = _written_by(died, alive)
    lines = recorded.lines("logcat_app_restart_usec")
    start = next(i for i, line in enumerate(lines) if f"Start proc {alive}:" in line)
    window = "\n".join([*lines[: start + 1], died, *lines[start + 1 :]])
    kept = logcat.app_window(window, PACKAGE, **_oracles(recorded), running=[alive]).lines
    alive_lines = [line for line in kept if line.split()[2:3] == [alive]]
    assert (alive_lines == []) is ends, f"{len(alive_lines)} live-process lines kept"


def _victim_package(recorded) -> str:
    """Another app's package, as the recording names it (aliased by the recorder)."""
    other = next(
        (
            token
            for line in recorded.lines("logcat_app_restart_usec")
            for token in re.findall(r"com\.example\.app\d+", line)
        ),
        None,
    )
    if other is None:
        pytest.fail("the recording no longer names another app's package")
    return other


def _attacker_crash_line(recorded, victim_package: str) -> str:
    """The recorded crash line, with ONLY the package it names changed.

    Written by the crashed process about itself, so its writer still equals
    the pid it names: the shape any app can produce for any package through
    the public ``android.util.Log`` API (Codex review of #27).
    """
    crashed = _pids(recorded)[CRASHED]
    line = _recorded_line(recorded, f"Process: {PACKAGE}, PID: {crashed}")
    return line.replace(f"Process: {PACKAGE},", f"Process: {victim_package},")


def test_only_the_opening_sentence_of_a_system_server_line_counts(recorded):
    """A sentence embedded later in a genuine system_server line names nothing.

    system_server's messages carry app-controlled text after their opening --
    here the component inside ``{...}`` -- and the writer check passes, since
    system_server really wrote the line (security review of #27). Derived from
    the recording, writer 598 kept:

    - the ``Start proc <crashed>`` line with a second ``Start proc <victim>:``
      inside its component: only the leading pid counts;
    - the ``Force stopping`` line, which opens with no attributing sentence,
      with one embedded after ``user=0``: nothing counts.
    """
    pids = _pids(recorded)
    victim = _victim(recorded)
    start = _recorded_line(recorded, f"ActivityManager: Start proc {pids[CRASHED]}:")
    crafted = start.replace(
        "DefaultActivity}", f"DefaultActivity x Start proc {victim}:{PACKAGE}/u0a205 }}"
    )
    if crafted == start:
        pytest.fail("the recorded Start proc line no longer ends in its component")
    assert logcat.parse_package_pids(crafted, PACKAGE, **_oracles(recorded)) == {pids[CRASHED]}

    force = _recorded_line(recorded, f"ActivityManager: Force stopping {PACKAGE}")
    crafted_force = force.replace(
        " user=0: ", f" user=0 x Start proc {victim}:{PACKAGE}/u0a205 for x: "
    )
    if crafted_force == force:
        pytest.fail("the recorded Force stopping line no longer reads `user=0: `")
    assert logcat.parse_package_pids(crafted_force, PACKAGE, **_oracles(recorded)) == (set())


def test_a_crash_line_attributes_nothing_on_its_own(recorded):
    """AndroidRuntime confirms, never sources (ruling A on Codex's P1).

    The attacker's line -- the recorded crash line naming another package --
    claims nothing for that package. The genuine line claims nothing alone
    either; the genuine crash is attributed by system_server's `Start proc`
    and `has died` for the same pid, which the whole recording carries.
    """
    pids = _pids(recorded)
    victim = _victim_package(recorded)
    attack = _attacker_crash_line(recorded, victim)
    assert logcat.parse_package_pids(attack, victim, **_oracles(recorded)) == set()

    genuine = _recorded_line(recorded, f"Process: {PACKAGE}, PID: {pids[CRASHED]}")
    assert logcat.parse_package_pids(genuine, PACKAGE, **_oracles(recorded)) == set()
    whole = recorded.text("logcat_app_restart_usec")
    assert pids[CRASHED] in logcat.parse_package_pids(whole, PACKAGE, **_oracles(recorded))


@pytest.mark.parametrize(
    "unknown", [None, lambda: None], ids=["not-given", "lookup-answered-nothing"]
)
def test_with_no_system_server_known_activitymanager_attributes_nothing(unknown, recorded):
    """Fail closed: an unverifiable claim about another process is not evidence.

    With system_server's pid unknown, the whole recorded window -- genuine
    system_server lines, the genuine crash line and a spoofed claim alike --
    attributes nothing; only ``pidof`` can then name a process.
    """
    window = recorded.text("logcat_app_restart_usec") + _spoofed_start(recorded, _victim(recorded))
    found = logcat.parse_package_pids(
        window, PACKAGE, system_server_pid=unknown, package_uids=_package_uid(recorded)
    )
    assert found == set()


@pytest.mark.parametrize(
    "unknown", [None, lambda: None], ids=["not-given", "lookup-answered-nothing"]
)
def test_with_no_package_uid_known_nothing_is_attributed(unknown, recorded):
    """Fail closed on the uid too: no start or kill can be checked against it."""
    found = logcat.parse_package_pids(
        recorded.text("logcat_app_restart_usec"),
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
    logcat.parse_package_pids(
        recorded.text("logcat_crash_loop"), PACKAGE, system_server_pid=ask, package_uids=uid
    )
    assert asked == []

    found = logcat.parse_package_pids(
        recorded.text("logcat_app_restart_usec"), PACKAGE, system_server_pid=ask, package_uids=uid
    )
    assert asked == [1]
    assert found == set(_pids(recorded).values())


# --- log_monitor --app --------------------------------------------------------


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _install(toolchain, recorded, window: str, *, system_server: str | None) -> None:
    alive = _pids(recorded)[ALIVE]
    rules = [Rule(("devices",), stdout=recorded.text("adb_devices_single"))]
    if system_server is not None:
        rules.append(Rule(("shell", "pidof", "system_server"), stdout=system_server))
    rules += uid_rules(recorded, PACKAGE, alive)
    rules += [
        Rule(("shell", "pidof"), stdout=alive),
        Rule(("logcat",), stdout=window, filter_pid=True),
    ]
    toolchain.install("adb", rules)


def _window_pids(lines) -> set[str]:
    return {m["pid"] for m in map(_THREADTIME.match, lines) if m}


def test_log_monitor_keeps_only_the_pidof_pid_with_the_packages_uid(toolchain, recorded):
    """`pidof` matches a process NAME; `ps` says whose process it is.

    `pidof` answers the live app's pid and system_server's (another app may
    name a process after the package). `ps` answers as recorded (ps_pid_uid:
    the package's uid for one, 1000 for system_server). Only the first is the
    app's: system_server's lines stay out, and the JSON says which was left.
    """
    pids, system_server = _pids(recorded), _system_server(recorded)
    alive = pids[ALIVE]
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("shell", "pidof", "system_server"), stdout=system_server),
            *uid_rules(recorded, PACKAGE, alive),
            Rule(("shell", "pidof"), stdout=" ".join((alive, system_server))),
            Rule(("logcat",), stdout=recorded.text("logcat_app_restart_usec"), filter_pid=True),
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
    window = recorded.text("logcat_app_restart_usec") + _spoofed_start(recorded, victim) + "\n"
    _install(toolchain, recorded, window, system_server=_system_server(recorded))

    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert victim not in payload["app_pids"]
    assert _window_pids(payload["sample_logs"]) == set(_pids(recorded).values())


def test_log_monitor_without_system_server_uses_no_activitymanager_line(toolchain, recorded):
    """No usable ``pidof system_server`` answer: fail closed, and say so.

    The stub's ``pidof`` rule answers every name with the live app's pid, so
    the system_server question gets the app's own pid back -- which one
    process cannot be, so it is no answer. The window is then the live process
    ``pidof`` named, alone: not the crashed or force-stopped processes, which
    only ActivityManager names, and not the spoofed victim.
    """
    pids = _pids(recorded)
    victim = _victim(recorded)
    window = recorded.text("logcat_app_restart_usec") + _spoofed_start(recorded, victim) + "\n"
    _install(toolchain, recorded, window, system_server=None)

    result = toolchain.run(
        "log_monitor.py", "--app", PACKAGE, "--last", "5m", "--json", ANDROID_EMU_LOG_TAIL="1000"
    )
    assert result.returncode == 0, result.stderr
    assert "cannot be verified and are not used" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["app_pids"] == [pids[ALIVE]]
    assert _window_pids(payload["sample_logs"]) == {pids[ALIVE]}


def test_log_monitor_keeps_an_attackers_crash_line_out_of_its_victims_window(toolchain, recorded):
    """Codex's P1 attack, end to end: the crashed process claims another package.

    The victim is another app named in the recording, running as a process of
    the window (``pidof`` answers it). The attacker -- the crashed process --
    adds the recorded crash line with only the package changed. Before ruling
    A that admitted the attacker's pid and its lines into the victim's window.
    """
    pids = _pids(recorded)
    victim_package, victim_pid = _victim_package(recorded), _victim(recorded)
    window = (
        recorded.text("logcat_app_restart_usec")
        + _attacker_crash_line(recorded, victim_package)
        + "\n"
    )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("shell", "pidof", "system_server"), stdout=_system_server(recorded)),
            *uid_rules(recorded, victim_package, victim_pid),
            Rule(("shell", "pidof"), stdout=victim_pid),
            Rule(("logcat",), stdout=window, filter_pid=True),
        ],
    )

    result = toolchain.run(
        "log_monitor.py",
        "--app",
        victim_package,
        "--last",
        "5m",
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert pids[CRASHED] not in payload["app_pids"]
    assert _window_pids(payload["sample_logs"]) == {victim_pid}


def _carriage_return_forgery(recorded) -> tuple[str, str]:
    """logcat_app_restart_usec where a third process forges a line after a carriage return.

    Derived: the first recorded line of a third process (``_victim``: neither
    the app nor system_server) gets "\\r" and the recorded ``Start proc
    <alive>`` line -- system_server's header and all -- appended, with the pid
    that line names replaced by the third process's own. logcat ends a line
    only at "\\n", so on the device the whole is ONE line of the third
    process. A reader that also breaks at "\\r" (a text-mode pipe,
    ``str.splitlines``) reads system_server starting the third process as the
    app (Codex review v3). Returns the window and the forger's pid.
    """
    forger, alive = _victim(recorded), _pids(recorded)[ALIVE]
    start = _recorded_line(recorded, f"ActivityManager: Start proc {alive}:")
    forged = start.replace(f"Start proc {alive}:", f"Start proc {forger}:")
    lines = recorded.text("logcat_app_restart_usec").split("\n")
    first = next(i for i, line in enumerate(lines) if _window_pids([line]) == {forger})
    lines[first] = "\r".join((lines[first], forged))
    return "\n".join(lines), forger


@pytest.mark.parametrize("read", [("--last", "5m"), ("--duration", "30s")], ids=["last", "live"])
def test_log_monitor_reads_a_carriage_return_as_part_of_its_line(read, toolchain, recorded):
    """A "\\r" inside an app's message does not start a line system_server wrote.

    The forgery reaches the script byte for byte, through the stub ``adb`` and
    the real pipe; the forger is not the app's, and the window is the app's
    three processes, in a historical read and live.
    """
    window, forger = _carriage_return_forgery(recorded)
    _install(toolchain, recorded, window, system_server=_system_server(recorded))
    replayed = subprocess.run(
        ["adb", "logcat"], env=toolchain.env(), capture_output=True, check=False
    ).stdout
    if replayed != window.encode("utf-8"):
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
    recorded for two users. Keeping only the listing's first uid left this
    process out of its own app's window (Codex review v3).
    """
    pids, system_server = _pids(recorded), _system_server(recorded)
    alive = pids[ALIVE]
    (first,) = _package_uid(recorded)
    second = _second_user_uid(recorded)
    user, app_id = divmod(second, 100_000)
    started = f"Start proc {alive}:{PACKAGE}/"
    window = recorded.text("logcat_app_restart_usec").replace(
        f"{started}u0a{first - 10_000} ", f"{started}u{user}a{app_id - 10_000} "
    )
    if window == recorded.text("logcat_app_restart_usec"):
        pytest.fail("the live process's Start proc no longer names the first user's uid")
    ps_answer = re.sub(
        rf"(?m)^(\s*{alive}\s+){first}$",
        lambda m: m[1] + str(second),
        recorded_ps_answer(recorded, alive),
    )
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("shell", "pidof", "system_server"), stdout=system_server),
            Rule(("cmd", "package"), stdout=recorded.text("package_list_uid_two_users")),
            Rule(("shell", "ps"), stdout=ps_answer),
            Rule(("shell", "pidof"), stdout=alive),
            Rule(("logcat",), stdout=window, filter_pid=True),
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


def test_app_state_capture_keeps_only_the_pidof_pid_with_the_packages_uid(
    monkeypatch, tmp_path, recorded
):
    """The same `pidof` check through app_state_capture: system_server's pid is not the app's."""
    pids, system_server = _pids(recorded), _system_server(recorded)
    alive = pids[ALIVE]
    window = recorded.text("logcat_app_restart_usec")

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd:
            answer = system_server if "system_server" in cmd else " ".join((alive, system_server))
            return SimpleNamespace(stdout=answer, stderr="", returncode=0)
        if (uid_answer := _uid_answer(recorded, cmd, alive)) is not None:
            return uid_answer
        if "logcat" in cmd:
            return SimpleNamespace(stdout=window, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)
    capturer = app_state_capture.AppStateCapture(package=PACKAGE, serial="emulator-5554")
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert system_server not in stats["pids"]
    assert system_server not in _window_pids(saved)


def _uid_answer(recorded, cmd: list[str], pid: str):
    """The recorded uid answers, for an in-process fake: package listing and ``ps``."""
    if "package" in cmd and "-U" in cmd:
        listing = recorded_package_listing(recorded, cmd[-1])
        return SimpleNamespace(stdout=listing, stderr="", returncode=0)
    if "ps" in cmd:
        return SimpleNamespace(stdout=recorded_ps_answer(recorded, pid), stderr="", returncode=0)
    return None


def test_app_state_capture_does_not_take_an_apps_word_for_another_process(
    monkeypatch, tmp_path, recorded
):
    """The same window rule, through app_state_capture's log capture."""
    victim, system_server = _victim(recorded), _system_server(recorded)
    alive = _pids(recorded)[ALIVE]
    window = recorded.text("logcat_app_restart_usec") + _spoofed_start(recorded, victim) + "\n"

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd:
            answer = system_server if "system_server" in cmd else alive
            return SimpleNamespace(stdout=answer, stderr="", returncode=0)
        if (uid_answer := _uid_answer(recorded, cmd, alive)) is not None:
            return uid_answer
        if "logcat" in cmd:
            return SimpleNamespace(stdout=window, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)
    capturer = app_state_capture.AppStateCapture(package=PACKAGE, serial="emulator-5554")
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert victim not in stats["pids"]
    assert _window_pids(saved) == set(_pids(recorded).values())


def test_app_state_capture_keeps_an_attackers_crash_line_out_of_its_victims_window(
    monkeypatch, tmp_path, recorded
):
    """Codex's P1 attack through app_state_capture: the same victim, the same line."""
    pids, system_server = _pids(recorded), _system_server(recorded)
    victim_package, victim_pid = _victim_package(recorded), _victim(recorded)
    window = (
        recorded.text("logcat_app_restart_usec")
        + _attacker_crash_line(recorded, victim_package)
        + "\n"
    )

    def fake_run(cmd, *args, **kwargs):
        if "pidof" in cmd:
            answer = system_server if "system_server" in cmd else victim_pid
            return SimpleNamespace(stdout=answer, stderr="", returncode=0)
        if (uid_answer := _uid_answer(recorded, cmd, victim_pid)) is not None:
            return uid_answer
        if "logcat" in cmd:
            return SimpleNamespace(stdout=window, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_state_capture.adb_exec.subprocess, "run", fake_run)
    capturer = app_state_capture.AppStateCapture(package=victim_package, serial="emulator-5554")
    stats = capturer._capture_logs(tmp_path / "app-logs.txt", "30s", log_lines=0)

    saved = (tmp_path / "app-logs.txt").read_text(encoding="utf-8").splitlines()
    assert pids[CRASHED] not in stats["pids"]
    assert _window_pids(saved) == {victim_pid}


def test_app_state_capture_reads_a_carriage_return_as_part_of_its_line(
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
