"""A dump with a time limit: where it is stopped, and what each answer means.

Recorded for v0.10.0 before ``navigator --wait-for`` was written, because a
reader with a deadline has to end a dump that is still running, and the first
way that comes to hand does damage:

1. **Ending a dump from the host leaves the device refusing the next one.**
   Killing the adb client (what a ``subprocess`` timeout does) leaves the dump
   registered on the device for a moment, and the dump asked for next answers
   the one word ``Killed`` at exit 0 (``uiautomator_dump_after_host_kill``).
2. **So does a second dump while one is running**, for the same reason
   (``uiautomator_dump_while_another_runs``): only one UI automation session
   can be registered at a time.
3. **A dump stopped ON the device leaves nothing behind.** Under toybox
   ``timeout`` the answer is no bytes at all at exit 0
   (``uiautomator_dump_stopped_on_device``, ``..._stopped_mid_read``), and the
   dump asked for at once afterwards is the screen
   (``uiautomator_dump_right_after_a_stop``).
4. **The wrapper changes nothing about a dump that answers in time**
   (``uiautomator_dump_within_device_limit``).

Every payload is read from ``tests/fixtures/recorded/``; API 35 and API 33.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import record_fixtures as rf
from conftest import RECORDED_ROOT, RecordedFixtures

from common import adb_exec, hierarchy

STOPPED = "uiautomator_dump_stopped_on_device"
STOPPED_MID_READ = "uiautomator_dump_stopped_mid_read"
RIGHT_AFTER = "uiautomator_dump_right_after_a_stop"
WITHIN = "uiautomator_dump_within_device_limit"
OVERLAP = "uiautomator_dump_while_another_runs"
AFTER_HOST_KILL = "uiautomator_dump_after_host_kill"
VIEW_TICKER = "uiautomator_never_idle_view_ticker"
ASLEEP = "uiautomator_dump_screen_asleep"


def _entry(recorded: RecordedFixtures, name: str) -> dict:
    entries = [entry for entry in recorded.manifest["fixtures"] if entry["name"] == name]
    assert len(entries) == 1, f"{name}: {len(entries)} manifest entries"
    return entries[0]


@pytest.fixture(params=["emulator-api35", "emulator-api33"])
def emulator(request) -> RecordedFixtures:
    """Each emulator profile that recorded the stopped-dump fixtures."""
    return RecordedFixtures(RECORDED_ROOT / request.param)


@pytest.fixture
def device(monkeypatch, emulator):
    """Answer each adb call with the next recorded fixture named, at its recorded status."""
    calls: list[tuple[list[str], dict]] = []

    def answers(*names: str):
        queue = list(names)

        def run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            name = queue.pop(0)
            status = _entry(emulator, name)["exit_status"]
            return subprocess.CompletedProcess(cmd, status, emulator.text(name), "")

        monkeypatch.setattr(adb_exec.subprocess, "run", run)
        monkeypatch.setattr(hierarchy.time, "sleep", lambda _seconds: None)
        return calls

    return answers


# ---------------------------------------------------------------------------
# What the recordings say.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [STOPPED, STOPPED_MID_READ])
def test_a_dump_the_device_stopped_answers_nothing_at_exit_0(emulator, name):
    entry = _entry(emulator, name)

    assert emulator.text(name) == ""
    assert entry["exit_status"] == 0
    assert (entry["stdout_bytes"], entry["stderr_bytes"]) == (0, 0)
    overrun = entry["answered_after_s"] - entry["device_side_limit_s"]
    assert 0 <= overrun <= 1, "the dump was not stopped at its limit"


def test_the_dump_asked_for_right_after_a_stop_on_the_device_is_the_screen(emulator):
    entry = _entry(emulator, RIGHT_AFTER)

    assert entry["asked_s_after_the_stopped_dump_returned"] < 0.5
    assert "Order placed" in emulator.text(RIGHT_AFTER)
    same = emulator.text(WITHIN) == emulator.text(RIGHT_AFTER)
    assert same, "a dump under `timeout` that answered in time is not the plain dump"
    assert _entry(emulator, WITHIN)["same_document_as_a_plain_dump_taken_next"] is True


@pytest.mark.parametrize("name", [OVERLAP, AFTER_HOST_KILL])
def test_a_dump_beside_another_answers_killed_at_exit_0(emulator, name):
    entry = _entry(emulator, name)

    assert emulator.text(name).strip() == "Killed"
    assert entry["exit_status"] == 0
    assert entry["stderr_bytes"] == 0
    assert entry["answered_in_s"] < 1
    logged = entry["android_runtime_logged"]
    assert logged.startswith("java.lang.IllegalStateException: UiAutomationService ")
    assert logged.endswith("already registered!")


def test_ending_a_dump_from_the_host_is_what_leaves_that_behind(emulator):
    entry = _entry(emulator, AFTER_HOST_KILL)

    assert entry["asked_s_after_the_kill"] < 0.5
    assert entry["a_later_dump_was"] == "a hierarchy"
    assert 3 <= entry["the_later_dump_was_asked_s_after_the_kill"] <= 4


# ---------------------------------------------------------------------------
# The bounded capture.
# ---------------------------------------------------------------------------


def test_the_limit_is_given_to_the_device_and_the_host_waits_longer(device):
    calls = device(WITHIN)

    hierarchy.capture_hierarchy_within("emulator-5554", 7.5)

    (cmd, kwargs), *rest = calls
    assert not rest, "a bounded capture is one dump, not a retry loop"
    assert cmd[-6:] == ["exec-out", "timeout", "7.5", "uiautomator", "dump", "/dev/tty"]
    assert kwargs["timeout"] == 13, "documented: 5 to 6 seconds past the device's limit"
    assert kwargs["timeout"] > 7.5, (
        "the host's own timeout is not above the device's: it would kill the adb client "
        "first, and uiautomator_dump_after_host_kill is what that leaves behind"
    )


def test_a_dump_that_answers_in_time_is_the_screen(device):
    device(WITHIN)

    root = hierarchy.capture_hierarchy_within("emulator-5554", 30)

    assert root.tag == "hierarchy"
    assert "Order placed" in {node.get("text") for node in root.iter("node")}


@pytest.mark.parametrize("name", [STOPPED, STOPPED_MID_READ])
def test_a_dump_the_device_stopped_is_reported_as_cut_off(device, name):
    device(name)

    with pytest.raises(hierarchy.CaptureCutOff) as excinfo:
        hierarchy.capture_hierarchy_within("emulator-5554", 4)

    assert "4.0s" in str(excinfo.value)
    assert isinstance(excinfo.value, hierarchy.HierarchyError)


@pytest.mark.parametrize(
    ("name", "says"),
    [(VIEW_TICKER, "idle"), (ASLEEP, "no focused window"), (OVERLAP, "already registered")],
)
def test_a_dump_that_answered_without_a_screen_is_an_error_and_not_a_cut(device, name, says):
    device(name)

    with pytest.raises(hierarchy.HierarchyError) as excinfo:
        hierarchy.capture_hierarchy_within("emulator-5554", 30)

    assert not isinstance(excinfo.value, hierarchy.CaptureCutOff)
    assert says in str(excinfo.value)


# ---------------------------------------------------------------------------
# "Killed" in the ordinary capture: another session, and worth one more try.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [OVERLAP, AFTER_HOST_KILL])
def test_killed_is_retried_and_the_screen_that_follows_is_returned(device, name):
    """After a host kill the state passes in under a second: the retry is the fix."""
    calls = device(name, RIGHT_AFTER)

    root = hierarchy.capture_hierarchy("emulator-5554", retries=3)

    assert len(calls) == 2
    assert "Order placed" in {node.get("text") for node in root.iter("node")}


def test_killed_every_time_says_another_session_holds_the_device(device):
    device(OVERLAP, OVERLAP, OVERLAP)

    with pytest.raises(hierarchy.HierarchyError) as excinfo:
        hierarchy.capture_hierarchy("emulator-5554", retries=3)

    message = str(excinfo.value)
    assert "already registered" in message
    assert "another" in message.lower()
    assert (
        "No UI hierarchy in uiautomator output" not in message
    ), "the one word the device printed is all the reader was given"


# ---------------------------------------------------------------------------
# The recorder refuses a run that does not show what the fixture says.
# ---------------------------------------------------------------------------
#
# The assertions above read what was committed. These hold the recorder to
# writing nothing weaker next time: `_dump_after_host_kill` is run against the
# recorded answers, one of them spoiled in turn. No device, and no clock but
# the one here.


class _RecorderClock:
    """``time`` for the recorder: sleeping, and a slow logcat read, move it."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class _KilledChild:
    """The first dump's adb client, which the capture kills."""

    def kill(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        return -9


@pytest.fixture
def host_kill(monkeypatch, emulator):
    """Run ``_dump_after_host_kill`` on recorded answers, with one of them replaceable."""
    logged = _entry(emulator, AFTER_HOST_KILL)["android_runtime_logged"]
    # The manifest keeps the message with the proxy's hash taken out; a line has one.
    real_line = "E/AndroidRuntime( 5737): " + logged.replace("@<hash>", "@2938e26")

    def run(
        *,
        later: str | None = None,
        log: str | None = None,
        log_read_takes: float = 0.0,
        rename_service: bool = False,
    ):
        if rename_service:
            # A wrong log is the recorded line with one word changed, never a
            # line written here.
            assert log is None and "UiAutomationService" in real_line
            log = real_line.replace("UiAutomationService", "SomeOtherService")
        clock = _RecorderClock()
        screen = emulator.text(RIGHT_AFTER)
        dumps = [screen, emulator.text(AFTER_HOST_KILL), screen if later is None else later]

        def fake_run(args, serial, timeout=30):
            if args == rf._DUMP:
                return rf.Capture(dumps.pop(0), "", 0)
            if args == ["logcat", "-c"]:
                return rf.Capture("", "", 0)
            assert args[:2] == ["logcat", "-d"], args
            clock.now += log_read_takes
            return rf.Capture(real_line if log is None else log, "", 0)

        monkeypatch.setattr(rf, "_run", fake_run)
        monkeypatch.setattr(rf, "_dump_started", lambda serial: _KilledChild())
        monkeypatch.setattr(rf, "time", clock)
        extra: dict = {}
        return rf._dump_after_host_kill(Path(), "emulator-5554", extra), extra

    return run


def test_the_recorder_accepts_the_run_the_fixture_describes(host_kill):
    capture, extra = host_kill()

    assert capture.stdout.strip() == "Killed"
    assert extra["a_later_dump_was"] == "a hierarchy"
    assert extra["the_later_dump_was_asked_s_after_the_kill"] == 3.0
    assert extra["android_runtime_logged"].endswith("@<hash>already registered!")


def test_the_recorder_refuses_a_later_dump_that_does_not_parse(host_kill, emulator):
    screen = emulator.text(RIGHT_AFTER)
    head, closing, tail = screen.rpartition("</node>")
    assert closing and "<?xml" in head and "</hierarchy>" in tail
    broken = head + "</nodx>" + tail

    with pytest.raises(rf.CaptureError, match="not a hierarchy that parses"):
        host_kill(later=broken)


def test_the_recorder_refuses_a_later_dump_that_is_still_killed(host_kill, emulator):
    with pytest.raises(rf.CaptureError, match="not a hierarchy that parses"):
        host_kill(later=emulator.text(AFTER_HOST_KILL))


def test_the_recorder_refuses_a_later_dump_asked_for_too_late(host_kill):
    """Half a second past the four the fixture's description allows; just inside is taken."""
    with pytest.raises(rf.CaptureError, match="not within 4"):
        host_kill(log_read_takes=4.5)

    _capture, extra = host_kill(log_read_takes=3.9)
    assert extra["the_later_dump_was_asked_s_after_the_kill"] == 3.9


def test_the_recorder_refuses_a_run_that_logged_nothing(host_kill):
    with pytest.raises(rf.CaptureError, match="UiAutomationService"):
        host_kill(log="")


def test_the_recorder_refuses_a_log_line_about_another_service(host_kill):
    """The recorded line with its service renamed still says 'already registered'."""
    with pytest.raises(rf.CaptureError, match="UiAutomationService"):
        host_kill(rename_service=True)
