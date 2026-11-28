"""``navigator --wait-for TEXT`` and ``--tap --expect TEXT``, against recorded screens.

One waiter, three answers, one deadline:

- **found** (exit 0): a capture that could be read held the text;
- **not observed** (exit 1): time ran out and the last capture that finished
  was a screen without it -- the answer says when that was and what it read;
- **could not read the screen** (exit 1): the last capture that finished was
  not a screen, or none finished. An earlier reading, if there was one, is
  given and called stale.

Nothing here is a hand-written dump. The screens are the delayed pair (one
launch, no input between the two dumps), the walk's checkout and confirmation
screens, and the three recorded ways a dump is not a screen: the View ticker
(``could not get idle state``, about eleven seconds), the sleeping display
(``null root node``, about two), and the dump the device stopped at its limit
(no bytes). The device below hands them out in the order a test scripts, each
taking the time the test says, on a clock only it and the waiter's pauses move.
A scripted dump that takes longer than the limit the waiter gave the device is
answered the way the recording says the device answers: nothing, at the limit.

What the clock here proves is how the budget is handed out: that no step is
given more than is left. It is not a measurement. On a device the stop itself
takes a moment (the recorded four-second limit answered after 4.0 and 4.1
seconds) and adb's round trip is not free; the wall clock is held, with slack,
by ``tests/test_navigator_wait_live.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET

import navigator
import pytest

from common import adb_exec, cli, hierarchy

BEFORE = "uiautomator_delayed_before"
AFTER = "uiautomator_delayed_after"
CHECKOUT = "uiautomator_walk_4_checkout"
CONFIRMATION = "uiautomator_walk_5_confirmation"
SETTINGS = "uiautomator_settings_top"
VIEW_TICKER = "uiautomator_never_idle_view_ticker"
ASLEEP = "uiautomator_dump_screen_asleep"
STOPPED = "uiautomator_dump_stopped_on_device"
LOST = "adb_device_not_found"

# How long each recorded answer took to arrive, from the manifest entries'
# own descriptions and clocks: a dump of a settled screen about two seconds,
# the idle failure about eleven, the sleeping display's about two.
DUMP_S = 2.2
IDLE_FAILURE_S = 11.0
ASLEEP_S = 2.0
TAP_S = 0.3
# Scripted in place of a screen: the next `input tap` does not return in the time it is given.
TAP_STALLS = "the tap command stalls"
# Scripted in place of a screen: the device is gone when the tap is sent.
TAP_LOST = "the device is lost at the tap"
# Scripted in place of a screen: adb itself does not answer, limit or no limit.
ADB_STALLS = "adb does not answer"

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
# The V10-A pin's pattern: the changing has to be said OF the content.
_SAID_OF_CONTENT = re.compile(r"\b(content|text|counter|timer)(\s+\w+){0,3}\s+(chang|updat)ing\b")


class Device:
    """Recorded answers, in a scripted order, on a clock that only moves when asked."""

    def __init__(self, recorded, recorded_anywhere, script, *, repeat_last=True):
        self._recorded = recorded
        self._recorded_anywhere = recorded_anywhere
        self._script = list(script)
        self._repeat_last = repeat_last
        self.clock = 0.0
        self.calls: list[list[str]] = []
        # (clock when asked, the limit given to the device, the host's own timeout)
        self.dumps: list[tuple[float, float, float]] = []
        # The host timeout each `input tap` was given.
        self.tap_timeouts: list[float] = []
        # The host timeout each `adb devices` was given.
        self.listing_timeouts: list[float] = []

    def _next(self) -> tuple[str, float]:
        if len(self._script) > 1 or not self._repeat_last:
            return self._script.pop(0)
        return self._script[0]

    def run(self, cmd, **kwargs):
        argv = list(cmd)
        self.calls.append(argv)
        if "uiautomator" in argv:
            limit = float(argv[argv.index("timeout") + 1])
            self.dumps.append((self.clock, limit, kwargs["timeout"]))
            name, took = self._next()
            if name == LOST:
                return subprocess.CompletedProcess(cmd, 1, "", self._recorded_anywhere(LOST))
            if name == ADB_STALLS:
                self.clock += kwargs["timeout"]
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            if took > limit:
                self.clock += limit
                return subprocess.CompletedProcess(cmd, 0, self._recorded.text(STOPPED), "")
            self.clock += took
            return subprocess.CompletedProcess(cmd, 0, self._recorded.text(name), "")
        if "input" in argv and "tap" in argv:
            self.tap_timeouts.append(kwargs["timeout"])
            if self._script and self._script[0][0] == TAP_LOST:
                return subprocess.CompletedProcess(cmd, 1, "", self._recorded_anywhere(LOST))
            if self._script and self._script[0][0] == TAP_STALLS:
                self.clock += kwargs["timeout"]
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            self.clock += TAP_S
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if argv[1:] == ["devices"]:
            # Not the waiter: adb_exec lists what is attached to word a lost device's error.
            self.listing_timeouts.append(kwargs["timeout"])
            listing = self._recorded_anywhere("adb_devices_single")
            return subprocess.CompletedProcess(cmd, 0, listing, "")
        raise AssertionError(f"the waiter ran something that is neither a dump nor a tap: {argv}")

    def pause(self, seconds: float) -> None:
        assert seconds >= 0
        self.clock += seconds

    @property
    def elapsed(self) -> float:
        """The clock, less the last bit a sum of tenths leaves behind."""
        return round(self.clock, 6)

    @property
    def taps(self) -> list[tuple[int, int]]:
        return [
            (int(argv[-2]), int(argv[-1])) for argv in self.calls if argv[-4:-2] == ["input", "tap"]
        ]

    @property
    def kinds(self) -> list[str]:
        return [
            "dump" if "uiautomator" in argv else "tap"
            for argv in self.calls
            if argv[1:] != ["devices"]
        ]


@pytest.fixture
def device(monkeypatch, recorded, recorded_anywhere):
    """``device(script)`` installs a scripted device and returns it."""

    def install(*script: tuple[str, float], repeat_last: bool = True) -> Device:
        fake = Device(recorded, recorded_anywhere, script, repeat_last=repeat_last)
        monkeypatch.setattr(adb_exec.subprocess, "run", fake.run)
        monkeypatch.setattr(navigator, "_now", lambda: fake.clock)
        monkeypatch.setattr(navigator, "_pause", fake.pause)
        monkeypatch.setattr(hierarchy.time, "sleep", fake.pause)
        return fake

    return install


@pytest.fixture
def run(capsys):
    """Run navigator as a process would, through the one boundary: (status, stdout, stderr)."""

    def _run(*argv: str) -> tuple[int, str, str]:
        with pytest.raises(SystemExit) as exit_info:
            cli.run(navigator.main, list(argv))
        captured = capsys.readouterr()
        code = exit_info.value.code
        return (0 if code is None else code), captured.out, captured.err

    return _run


def _rect(recorded, screen: str, resource_id: str) -> tuple[int, int, int, int]:
    """A control's rectangle, read from the recording and not from navigator."""
    root = ET.fromstring(recorded.text(screen))
    (node,) = [n for n in root.iter("node") if n.get("resource-id") == resource_id]
    return tuple(int(group) for group in _BOUNDS.fullmatch(node.get("bounds")).groups())


def _inside(rect, point) -> bool:
    left, top, right, bottom = rect
    return left <= point[0] < right and top <= point[1] < bottom


# ---------------------------------------------------------------------------
# --wait-for: found.
# ---------------------------------------------------------------------------


def test_text_that_arrives_late_is_found_from_the_capture_that_holds_it(device, run, recorded):
    late = [n for n in ET.fromstring(recorded.text(AFTER)).iter("node") if n.get("text")]
    assert "Order confirmed" not in recorded.text(BEFORE)
    assert {n.get("clickable") for n in late} == {"false"}, "the text waited for is a control"
    phone = device((BEFORE, DUMP_S), (BEFORE, DUMP_S), (AFTER, DUMP_S))

    status, out, err = run("--wait-for", "Order confirmed")

    assert (status, err) == (0, "")
    first, *rest = out.splitlines()
    assert first.startswith("Found: text='Order confirmed' after 7.2s"), first
    assert '"Order confirmed"' in first
    assert rest and rest[0].startswith("Screen: ")
    assert len(phone.dumps) == 3


def test_waiting_only_reads_the_screen(device, run):
    """No scroll, no tap, no setting changed: every adb call is the dump."""
    phone = device((BEFORE, DUMP_S), (AFTER, DUMP_S))

    run("--wait-for", "Order confirmed")

    assert phone.kinds == ["dump", "dump"]
    for argv in phone.calls:
        assert argv[1:] == ["exec-out", "timeout", argv[3], "uiautomator", "dump", "/dev/tty"]


def test_text_already_on_the_screen_is_found_by_the_first_capture(device, run):
    phone = device((AFTER, DUMP_S))

    status, out, _err = run("--wait-for", "Order #5107")

    assert status == 0
    assert "after 2.2s" in out
    assert len(phone.dumps) == 1


def test_found_as_json_is_one_object_with_the_match_and_the_screen(device, run):
    device((BEFORE, DUMP_S), (AFTER, DUMP_S))

    status, out, err = run("--wait-for", "Order confirmed", "--json")

    assert (status, err) == (0, "")
    answer = json.loads(out)
    assert answer["success"] is True
    assert answer["waited_for"] == "Order confirmed"
    assert answer["elapsed_seconds"] == 4.7
    assert answer["timeout_seconds"] == navigator.WAIT_TIMEOUT_SECONDS
    assert answer["captures"] == {"readable": 2, "failed": 0, "unfinished": 0}
    assert answer["element"]["label"] == "Order confirmed"
    assert answer["screen"]["interactive_elements"] == 0
    assert "tapped_at" not in answer


# ---------------------------------------------------------------------------
# What counts as the text.
# ---------------------------------------------------------------------------


def test_matching_ignores_case_and_finds_part_of_a_text_and_says_which(device, run):
    device((BEFORE, DUMP_S))

    status, out, _err = run("--wait-for", "order")

    assert status == 0
    assert '"Placing order"' in out.splitlines()[0], "the answer does not say what matched"


def test_exact_matches_the_whole_text_only(device, run):
    device((BEFORE, DUMP_S))

    assert run("--wait-for", "Placing", "--exact", "--timeout", "5")[0] == 1
    assert run("--wait-for", "Placing order", "--exact", "--timeout", "5")[0] == 0


def test_the_node_that_carries_the_text_is_the_answer_not_the_control_that_owns_it(device, run):
    """`--find-text` would answer with the search bar; a wait answers with what it saw.

    On the recorded Settings screen "Search settings" is a TextView inside a
    clickable ViewGroup. A tap wants the ViewGroup. A wait that reported it
    would be describing a node that does not carry the text.
    """
    device((SETTINGS, DUMP_S))

    status, out, _err = run("--wait-for", "Search settings", "--json")

    assert status == 0
    assert json.loads(out)["element"]["type"] == "TextView"


def test_the_text_of_a_disabled_node_is_text_on_the_screen(recorded):
    """A recorded node with ONE attribute flipped, as tests/test_hierarchy.py does.

    No recorded screen carries `enabled="false"`, so the case is made by
    changing that one attribute on the node the delayed screen's text sits on.
    `--find-text` skips such a node, because its answer is tapped; a wait must
    not, because the text is there to be read.
    """
    root = ET.fromstring(recorded.text(AFTER))
    (label,) = [node for node in root.iter("node") if node.get("text") == "Order confirmed"]
    label.set("enabled", "false")
    nav = navigator.Navigator()

    assert nav._find_in(root, text="Order confirmed") is None
    found = nav.find_visible(root, "Order confirmed")
    assert found is not None and found.enabled is False


def test_without_exact_an_id_is_still_matched_as_written(device, run):
    device((CONFIRMATION, DUMP_S))

    assert run("--wait-for", "ORDER PLACED", "--timeout", "5")[0] == 0
    assert run("--wait-for", "OPEN_SETTINGS_BUTTON", "--timeout", "5")[0] == 1
    assert run("--wait-for", "ORDER PLACED", "--exact", "--timeout", "5")[0] == 1


def test_a_name_the_screen_report_prints_is_text_to_wait_for(device, run):
    """A bare resource id, which is what screen_mapper prints for a tagged Compose control."""
    device((CONFIRMATION, DUMP_S))

    assert run("--wait-for", "open_settings_button")[0] == 0
    # An id is matched whole, as --find-text matches one: part of it is not a name.
    assert run("--wait-for", "settings_button", "--timeout", "5")[0] == 1


# ---------------------------------------------------------------------------
# --wait-for: not observed.
# ---------------------------------------------------------------------------


def test_text_that_never_arrives_is_not_observed_and_the_answer_says_what_was_read(device, run):
    phone = device((BEFORE, DUMP_S))

    status, out, err = run("--wait-for", "Order confirmed", "--timeout", "10")

    assert (status, out) == (1, "")
    error, remedy = err.splitlines()
    assert error.startswith("Error: Not observed: text='Order confirmed'")
    assert "10" in error
    assert '"Placing order"' in error, "what the screen said instead is not in the answer"
    assert "stale" not in error
    assert remedy.startswith("Remedy: ")
    assert phone.elapsed <= 10


def test_not_observed_as_json_carries_the_last_screen(device, run):
    device((BEFORE, DUMP_S))

    status, out, err = run("--wait-for", "Order confirmed", "--timeout", "10", "--json")

    assert (status, err) == (1, "")
    answer = json.loads(out)
    assert set(answer) >= {"success", "error", "remedy", "wait"}
    assert answer["success"] is False
    wait = answer["wait"]
    assert wait["outcome"] == "not_observed"
    assert wait["waited_for"] == "Order confirmed"
    assert wait["timeout_seconds"] == 10
    assert wait["elapsed_seconds"] <= 10
    assert wait["captures"]["readable"] >= 3
    assert wait["screen_stale"] is False
    assert "Placing order" in wait["texts"]
    assert wait["screen"]["total_elements"] > 0


def test_a_last_capture_that_did_not_finish_leaves_the_one_before_it_as_the_answer(device, run):
    """Read at 2.2s, then a dump still running when time is up: said, with both numbers."""
    device((BEFORE, DUMP_S), (BEFORE, 9.0))

    status, _out, err = run("--wait-for", "Order confirmed", "--timeout", "10")

    error = err.splitlines()[0]
    assert status == 1
    assert error.startswith("Error: Not observed")
    assert "2.2s" in error, "the answer does not say when the screen was last read"
    assert "did not finish" in error


# ---------------------------------------------------------------------------
# --wait-for: could not read the screen.
# ---------------------------------------------------------------------------


def test_a_screen_that_never_goes_idle_cannot_be_read_and_the_answer_says_why(device, run):
    phone = device((VIEW_TICKER, IDLE_FAILURE_S))

    status, out, err = run("--wait-for", "Synced", "--timeout", "30")

    assert (status, out) == (1, "")
    error, remedy = err.splitlines()
    assert error.startswith("Error: Could not read the screen")
    assert _SAID_OF_CONTENT.search(
        error.lower()
    ), "the answer names animation only; this recording is content that keeps changing"
    assert "Last screen" not in error and "stale" not in error
    assert remedy.startswith("Remedy: ")
    assert phone.elapsed <= 30


def test_an_earlier_reading_is_given_and_called_stale(device, run):
    device((BEFORE, DUMP_S), (VIEW_TICKER, IDLE_FAILURE_S))

    status, _out, err = run("--wait-for", "Order confirmed", "--timeout", "30")

    error = err.splitlines()[0]
    assert status == 1
    assert error.startswith("Error: Could not read the screen")
    assert "stale" in error
    assert '"Placing order"' in error


def test_a_dump_that_cannot_finish_inside_the_timeout_is_not_a_reading(device, run):
    """Eleven seconds of dump and five of timeout: stopped on the device at five."""
    phone = device((VIEW_TICKER, IDLE_FAILURE_S))

    status, _out, err = run("--wait-for", "Synced", "--timeout", "5")

    error = err.splitlines()[0]
    assert status == 1
    assert error.startswith("Error: Could not read the screen")
    assert "did not finish" in error
    assert [limit for _at, limit, _host in phone.dumps] == [5.0]
    assert phone.elapsed == 5.0


def test_a_sleeping_display_cannot_be_read_and_the_answer_says_so(device, run):
    device((ASLEEP, ASLEEP_S))

    status, _out, err = run("--wait-for", "Order placed", "--timeout", "10")

    assert status == 1
    assert err.startswith("Error: Could not read the screen")
    assert "no focused window" in err


def test_could_not_read_as_json(device, run):
    device((BEFORE, DUMP_S), (VIEW_TICKER, IDLE_FAILURE_S))

    status, out, _err = run("--wait-for", "Order confirmed", "--timeout", "30", "--json")

    wait = json.loads(out)["wait"]
    assert status == 1
    assert wait["outcome"] == "unreadable"
    assert wait["screen_stale"] is True
    assert wait["last_read_seconds"] == 2.2
    assert wait["captures"]["failed"] >= 2
    assert "idle" in wait["last_failure"]


def test_a_screen_read_after_a_failure_is_the_last_word(device, run):
    """Not a screen, then screens without the text: the failure is not what decides."""
    device((ASLEEP, ASLEEP_S), (BEFORE, DUMP_S))

    status, _out, err = run("--wait-for", "Order confirmed", "--timeout", "10")

    error = err.splitlines()[0]
    assert status == 1
    assert error.startswith("Error: Not observed"), error
    assert "1 capture answered without a screen" in error
    assert "stale" not in error


def test_a_readable_screen_after_unreadable_ones_is_still_read(device, run):
    """A transient failure is not the end of the wait: the next capture is asked for."""
    device((ASLEEP, ASLEEP_S), (ASLEEP, ASLEEP_S), (AFTER, DUMP_S))

    status, out, _err = run("--wait-for", "Order confirmed")

    assert status == 0
    assert "Found:" in out


# ---------------------------------------------------------------------------
# One deadline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [3, 5, 10, 30, 47.5])
@pytest.mark.parametrize(
    "script",
    [
        ((BEFORE, DUMP_S),),
        ((VIEW_TICKER, IDLE_FAILURE_S),),
        ((ASLEEP, ASLEEP_S),),
        ((BEFORE, DUMP_S), (VIEW_TICKER, IDLE_FAILURE_S), (BEFORE, 6.0)),
        ((BEFORE, 0.1),),
        ((BEFORE, 2.23), (ASLEEP, 1.87)),
    ],
    ids=["settled", "never idle", "asleep", "mixed", "fast answers", "odd durations"],
)
def test_the_wait_never_outlasts_its_timeout(device, run, timeout, script):
    phone = device(*script)

    status, _out, _err = run("--wait-for", "Order confirmed", "--timeout", str(timeout))

    assert status == 1
    assert phone.elapsed <= timeout, f"answered at {phone.clock}s"
    for asked_at, limit, host_timeout in phone.dumps:
        assert asked_at + limit <= timeout + 1e-6, "a dump was given more time than was left"
        assert (
            limit >= navigator.MIN_CAPTURE_SECONDS
        ), "a dump was started that uiautomator's own one-second idle wait cannot fit in"
        assert host_timeout > limit, "the host would have killed the adb client first"
    assert len(phone.dumps) < 200, "the wait spun"


def test_the_default_timeout_is_stated_and_used(device, run):
    phone = device((BEFORE, DUMP_S))

    _status, _out, err = run("--wait-for", "Order confirmed")

    assert f"{navigator.WAIT_TIMEOUT_SECONDS:g}" in err
    assert navigator.WAIT_TIMEOUT_SECONDS - 3 < phone.elapsed <= navigator.WAIT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# The device goes away.
# ---------------------------------------------------------------------------


def test_a_device_lost_while_waiting_is_exit_3(device, run):
    phone = device((BEFORE, DUMP_S), (LOST, 0.0))

    status, _out, err = run("--wait-for", "Order confirmed")

    assert status == 3
    assert err.startswith("Error: ")
    assert "adb devices" in err
    # The third stated way past the deadline: one listing, to word that error,
    # on a limit of its own that is not the wait's.
    assert phone.listing_timeouts == [10]


def test_an_adb_that_does_not_answer_is_adbs_error_a_few_seconds_past_the_limit(device, run):
    """The device stops a dump at its limit; only an adb that is not answering reaches this."""
    phone = device((ADB_STALLS, 0.0))

    status, out, err = run("--wait-for", "Order confirmed", "--timeout", "10")

    assert (status, out) == (1, "")
    assert "did not finish within" in err and "adb" in err
    assert len(phone.dumps) == 1, "a stalled adb was asked again"
    assert phone.elapsed == 15, "documented: 5 to 6 seconds past the dump's limit"


# ---------------------------------------------------------------------------
# --tap --expect.
# ---------------------------------------------------------------------------


def test_tap_then_expect_taps_once_and_reports_the_screen_that_followed(device, run, recorded):
    button = _rect(recorded, CHECKOUT, "place_order_button")
    phone = device((CHECKOUT, DUMP_S), (CONFIRMATION, DUMP_S))

    status, out, err = run("--find-text", "Place order", "--tap", "--expect", "Order placed")

    assert (status, err) == (0, "")
    assert phone.kinds == ["dump", "tap", "dump"]
    (tap,) = phone.taps
    assert _inside(button, tap), f"tapped {tap}, outside the button {button}"
    tapped, found, screen, *controls = out.splitlines()
    assert tapped.startswith("Tapped: ")
    assert found.startswith("Found: text='Order placed' after ")
    assert screen.startswith("Screen: ")
    assert any(
        '"open_settings_button"' in line for line in controls
    ), "the screen that followed is not summarised by its control names"


def test_tap_then_expect_as_json(device, run, recorded):
    button = _rect(recorded, CHECKOUT, "place_order_button")
    device((CHECKOUT, DUMP_S), (CONFIRMATION, DUMP_S))

    status, out, _err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--json"
    )

    answer = json.loads(out)
    assert status == 0
    assert _inside(button, answer["tapped_at"])
    assert answer["tapped"].startswith("Tapped: ")
    assert answer["waited_for"] == "Order placed"
    assert answer["element"]["label"] == "Order placed"
    assert "open_settings_button" in answer["screen"]["controls"]["Control"]


def test_the_time_spent_finding_and_tapping_counts_against_the_timeout(device, run):
    phone = device((CHECKOUT, DUMP_S))

    status, _out, _err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--timeout", "12"
    )

    assert status == 1
    assert phone.elapsed <= 12
    assert phone.kinds[:2] == ["dump", "tap"]
    for asked_at, limit, _host in phone.dumps:
        assert asked_at + limit <= 12 + 1e-6


def test_expected_text_that_does_not_follow_the_tap_says_the_tap_was_sent(device, run):
    phone = device((CHECKOUT, DUMP_S))

    status, out, err = run(
        "--find-text", "Gift wrap", "--tap", "--expect", "Order placed", "--timeout", "10"
    )

    assert (status, out) == (1, "")
    error, remedy = err.splitlines()
    assert "Tapped" in error and "Not observed: text='Order placed'" in error
    assert "tap was sent" in remedy
    assert len(phone.taps) == 1, "a wait that failed must not have tapped again"


def test_expected_text_not_observed_as_json_says_where_the_tap_went(device, run, recorded):
    box = _rect(recorded, CHECKOUT, "gift_wrap_checkbox")
    device((CHECKOUT, DUMP_S))

    _status, out, _err = run(
        "--find-text", "Gift wrap", "--tap", "--expect", "Order placed", "--timeout", "10", "--json"
    )

    answer = json.loads(out)
    assert answer["success"] is False
    assert _inside(box, answer["tapped_at"])
    assert answer["wait"]["outcome"] == "not_observed"


def test_a_target_that_is_not_on_the_screen_is_not_waited_for(device, run):
    phone = device((CHECKOUT, DUMP_S))

    status, _out, err = run("--find-text", "No such control", "--tap", "--expect", "Order placed")

    assert status == 1
    assert err.startswith("Error: Not found: text='No such control'")
    assert phone.kinds == ["dump"], "nothing was tapped, so there is nothing to wait for"


def test_a_screen_that_cannot_be_read_is_not_tapped(device, run):
    phone = device((VIEW_TICKER, IDLE_FAILURE_S))

    status, _out, err = run("--find-text", "Synced", "--tap", "--expect", "Done", "--timeout", "15")

    error = err.splitlines()[0]
    assert status == 1
    assert error.startswith("Error: Could not read the screen")
    assert "nothing was tapped" in error
    assert _SAID_OF_CONTENT.search(error.lower())
    assert phone.taps == []
    assert phone.elapsed <= 15


def test_finding_the_target_waits_out_a_screen_that_is_not_there_yet(device, run):
    """`null root node` is what a just-launched app answers; the budget covers a retry."""
    phone = device((ASLEEP, ASLEEP_S), (CHECKOUT, DUMP_S), (CONFIRMATION, DUMP_S))

    status, _out, _err = run("--find-text", "Place order", "--tap", "--expect", "Order placed")

    assert status == 0
    assert phone.kinds == ["dump", "dump", "tap", "dump"]


def test_verbose_reports_the_reads_made_to_find_the_target_too(device, run):
    device((ASLEEP, ASLEEP_S), (CHECKOUT, DUMP_S), (CONFIRMATION, DUMP_S))

    _status, _out, err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--verbose"
    )

    lines = [line for line in err.splitlines() if line.startswith("  capture ")]
    assert len(lines) == 3, lines
    assert "not a screen" in lines[0]
    assert lines[1].endswith(": a screen")
    assert lines[2].endswith(": match")


def test_a_tap_that_leaves_no_time_to_read_the_screen_says_so(device, run):
    phone = device((CHECKOUT, 9.5))

    status, _out, err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--timeout", "10"
    )

    error = err.splitlines()[0]
    assert status == 1
    assert len(phone.taps) == 1
    assert "Tapped" in error
    assert error.count("Could not read the screen") == 1
    assert "no time was left" in error
    assert phone.elapsed <= 10, "the pause after the tap was not paid for from the deadline"


def test_a_target_found_as_the_time_runs_out_is_not_tapped(device, run):
    phone = device((CHECKOUT, 10.0))

    status, _out, err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--timeout", "10"
    )

    assert status == 1
    assert "nothing was tapped" in err
    assert phone.taps == []


def test_the_tap_command_gets_what_is_left_and_never_less_than_its_floor(device, run):
    """A tap cut off half way is a tap nobody can say happened, so it is not cut short."""
    plenty = device((CHECKOUT, DUMP_S), (CONFIRMATION, DUMP_S))
    run("--find-text", "Place order", "--tap", "--expect", "Order placed", "--timeout", "30")
    assert plenty.tap_timeouts == [28]  # 30 less the 2.2s the first read took, rounded up

    little = device((CHECKOUT, 9.5))
    run("--find-text", "Place order", "--tap", "--expect", "Order placed", "--timeout", "10")
    assert little.tap_timeouts == [navigator.TAP_COMMAND_FLOOR_SECONDS]


def test_a_tap_command_that_stalls_is_reported_as_a_tap_nobody_can_vouch_for(device, run):
    phone = device((CHECKOUT, DUMP_S), (TAP_STALLS, 0.0), repeat_last=False)

    status, out, err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--timeout", "10"
    )

    assert (status, out) == (1, "")
    error, remedy = err.splitlines()
    assert "did not finish" in error and "not known" in error
    assert "before tapping again" in remedy, "the remedy for an unknown tap is to tap again"
    assert phone.kinds == ["dump", "tap"], "the screen was read as if the tap had been sent"


def test_a_device_lost_after_the_tap_is_exit_3_and_still_says_the_tap_was_sent(device, run):
    """ "Run `adb devices`" alone would be read as: nothing happened, run it again."""
    phone = device((CHECKOUT, DUMP_S), (LOST, 0.0), repeat_last=False)

    status, out, err = run("--find-text", "Place order", "--tap", "--expect", "Order placed")

    assert (status, out) == (3, "")
    assert phone.kinds == ["dump", "tap", "dump"]
    error, remedy = err.splitlines()
    assert error.startswith("Error: Tapped: ")
    assert "then the screen could not be read" in error
    assert "tap was sent" in remedy and "adb devices" in remedy


def test_a_device_lost_after_the_tap_as_json_says_where_the_tap_went(device, run, recorded):
    button = _rect(recorded, CHECKOUT, "place_order_button")
    device((CHECKOUT, DUMP_S), (LOST, 0.0), repeat_last=False)

    status, out, _err = run(
        "--find-text", "Place order", "--tap", "--expect", "Order placed", "--json"
    )

    answer = json.loads(out)
    assert status == 3
    assert answer["success"] is False
    assert _inside(button, answer["tapped_at"])
    assert answer["tapped"].startswith("Tapped: ")


def test_an_adb_that_stops_answering_after_a_tap_at_still_says_the_tap_was_sent(device, run):
    phone = device((ADB_STALLS, 0.0))

    status, _out, err = run("--tap-at", "540,1200", "--expect", "Done", "--timeout", "10")

    assert status == 1
    assert phone.taps == [(540, 1200)]
    error, remedy = err.splitlines()
    assert error.startswith("Error: Tapped at (540, 1200), then the screen could not be read")
    assert "tap was sent" in remedy


def test_an_index_past_either_end_is_not_found_and_nothing_is_tapped(device, run, recorded):
    """`--index -99` was an IndexError traceback; -1 is still the last match."""
    phone = device((CHECKOUT, DUMP_S))

    status, _out, err = run(
        "--find-text", "Place order", "--index", "-99", "--tap", "--expect", "Order placed"
    )

    assert status == 1
    assert err.startswith("Error: Not found")
    assert phone.taps == []
    root = ET.fromstring(recorded.text(CHECKOUT))
    nav = navigator.Navigator()
    fields = [nav._find_in(root, element_type="EditText", index=i) for i in (0, 1, 2, -1)]
    assert [bare.resource_id for bare in fields] == [
        "name_field",
        "street_field",
        "city_field",
        "city_field",
    ]
    assert nav._find_in(root, element_type="EditText", index=-99) is None
    assert nav._find_in(root, element_type="EditText", index=99) is None


def test_a_device_lost_at_the_tap_is_exit_3(device, run):
    phone = device((CHECKOUT, DUMP_S), (TAP_LOST, 0.0), repeat_last=False)

    status, _out, err = run("--find-text", "Place order", "--tap", "--expect", "Order placed")

    assert status == 3
    assert phone.kinds == ["dump", "tap"]
    assert err.startswith("Error: ")


def test_tap_at_then_expect_needs_no_reading_before_the_tap(device, run):
    phone = device((AFTER, DUMP_S))

    status, out, _err = run("--tap-at", "540,1200", "--expect", "Order confirmed")

    assert status == 0
    assert phone.kinds == ["tap", "dump"]
    assert phone.taps == [(540, 1200)]
    assert out.splitlines()[0] == "Tapped at (540, 1200)"
    assert out.splitlines()[1].startswith("Found: text='Order confirmed'")


def test_exact_covers_the_expected_text_too(device, run):
    device((CHECKOUT, DUMP_S), (CONFIRMATION, DUMP_S))

    status, _out, _err = run(
        "--find-text", "Place order", "--exact", "--tap", "--expect", "Order", "--timeout", "8"
    )

    assert status == 1, "`Order` is part of `Order placed`, not the whole of any text"


def test_verbose_reports_each_capture_as_it_is_made(device, run):
    device((BEFORE, DUMP_S), (ASLEEP, ASLEEP_S), (AFTER, DUMP_S))

    status, _out, err = run("--wait-for", "Order confirmed", "--verbose")

    assert status == 0
    lines = [line for line in err.splitlines() if line.startswith("  ")]
    assert len(lines) == 3
    assert "no match" in lines[0] and "not a screen" in lines[1] and "match" in lines[2]


# ---------------------------------------------------------------------------
# Usage: refused before anything reaches the device.
# ---------------------------------------------------------------------------

BAD_ARGUMENTS = [
    ("--wait-for", "x", "--tap"),
    ("--wait-for", "x", "--find-text", "y"),
    ("--wait-for", "x", "--find-text", ""),
    ("--wait-for", "x", "--find-exact", ""),
    ("--wait-for", "x", "--find-type", ""),
    ("--wait-for", "x", "--tap-at", ""),
    ("--wait-for", "x", "--find-id", ""),
    ("--find-text", "a", "--find-exact", "", "--tap", "--expect", "y"),
    ("--find-text", "a", "--find-exact", "", "--tap"),
    ("--find-id", " ", "--tap"),
    ("--find-text", "", "--find-id", "place_order_button"),
    ("--find-text", " ", "--find-type", "EditText", "--tap"),
    ("--find-type", "", "--find-text", "a"),
    ("--wait-for", "x", "--find-id", "y"),
    ("--wait-for", "x", "--list"),
    ("--wait-for", "x", "--scroll-to-find"),
    ("--wait-for", "x", "--tap-at", "1,2"),
    ("--wait-for", "x", "--enter-text", "y"),
    ("--wait-for", "x", "--expect", "y"),
    ("--wait-for", ""),
    ("--wait-for", "   "),
    ("--expect", "y"),
    ("--find-text", "a", "--expect", "y"),
    ("--find-text", "a", "--enter-text", "b", "--expect", "y"),
    ("--find-text", "a", "--tap", "--scroll-to-find", "--expect", "y"),
    ("--find-text", "a", "--tap", "--expect", ""),
    ("--list", "--expect", "y"),
    ("--timeout", "5"),
    ("--find-text", "a", "--tap", "--timeout", "5"),
    ("--wait-for", "x", "--timeout", "0"),
    ("--wait-for", "x", "--timeout", "0.5"),
    ("--wait-for", "x", "--timeout", "2.9"),
    ("--wait-for", "x", "--timeout", "601"),
    ("--wait-for", "x", "--timeout", "soon"),
    ("--wait-for", "x", "--timeout", "nan"),
    ("--wait-for", "x", "--timeout", "inf"),
]


@pytest.mark.parametrize("argv", BAD_ARGUMENTS, ids=[" ".join(a) or "-" for a in BAD_ARGUMENTS])
def test_arguments_that_do_not_go_together_are_a_usage_error(device, run, argv):
    phone = device((BEFORE, DUMP_S))

    status, out, err = run(*argv)

    assert status == 2
    assert out == ""
    assert "usage:" in err
    assert phone.calls == [], "a refused command reached the device"
    if argv[0] == "--wait-for" and argv[2:] and argv[2] != "--timeout":
        # Refused for what it is, not by the older check that an action needs a target.
        assert f"does not go with {argv[2]}" in err


@pytest.mark.parametrize("argv", BAD_ARGUMENTS[:3] + BAD_ARGUMENTS[-3:])
def test_a_usage_error_under_json_is_the_refusal_object(device, run, argv):
    device((BEFORE, DUMP_S))

    status, out, _err = run(*argv, "--json")

    answer = json.loads(out)
    assert status == 2
    assert answer["success"] is False
    assert answer["remedy"].startswith("usage:")
