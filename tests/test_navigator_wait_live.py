"""``navigator --wait-for`` and ``--tap --expect`` on a live emulator.

One success and one refusal of each flag, on the fixture app's two screens that
exist for this: ``DelayedActivity``, whose text changes by itself after a delay,
and ``NeverIdleActivity`` in its ``view_ticker`` mode, which uiautomator cannot
dump. Then the loop the flags close, on the walk: tap, wait, and act on a name
the answer printed, with no screen map in between.

The unit tests script these answers from recordings on a clock of their own.
What only a device can show is here: that text which arrives late is found by a
real wait, that the timeout is kept on a real clock, and that a wait which ran
out of time on an unreadable screen leaves the device able to be read by the
next command. The last is why each dump is stopped on the device and not from
here (``tests/test_capture_within_limit.py``).

Emulators only: these launch, stop and tap. The device comes from
``test_agent_task_e2e.compose_device``, which asks emulators and nothing else.
"""

# ruff: noqa: F811 -- a test takes the fixtures imported below by naming them as arguments
from __future__ import annotations

import json
import re
import subprocess
import time

import pytest
from test_agent_task_e2e import APP, compose_device, run_skill  # noqa: F401 -- fixtures

pytestmark = pytest.mark.emulator

# The delayed screen changes this long after it opens. Long enough that a first
# dump on a slow hosted runner still lands before the change: a settled screen
# took 2s to dump on a warm emulator here and 17s on one that had just booted.
DELAY_MS = 30_000
# What the waits that must succeed are given. Under run_skill's own 120s limit.
PATIENT = "100"
# Somewhere on the screen that is not a control on either fixture screen.
EMPTY_SPOT = "540,1200"
# Wall-clock allowance on top of --timeout for starting Python and resolving the device.
STARTUP_SLACK_S = 10

_QUOTED = re.compile(r'"([^"]*)"')


@pytest.fixture
def open_screen(adb: str, compose_device: str):
    """Force-stop the fixture app and open one of its activities; stop it again afterwards."""

    def _adb(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [adb, "-s", compose_device, *args],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def _open(activity: str, *extras: str) -> None:
        assert _adb("shell", "am", "force-stop", APP).returncode == 0
        started = _adb("shell", "am", "start", "-W", "-n", f"{APP}/.{activity}", *extras)
        assert "Status: ok" in started.stdout, f"{activity} did not launch: {started.stdout!r}"

    yield _open
    _adb("shell", "am", "force-stop", APP)


def _timed(run_skill, *args: str) -> tuple[subprocess.CompletedProcess, float]:
    began = time.monotonic()
    result = run_skill(*args)
    return result, time.monotonic() - began


def test_wait_for_finds_text_that_arrives_by_itself(run_skill, open_screen):
    open_screen("DelayedActivity", "--ei", "delay_ms", str(DELAY_MS))

    result = run_skill(
        "navigator.py", "--wait-for", "Order confirmed", "--timeout", PATIENT, "--json"
    )

    assert result.returncode == 0, f"{result.stdout.strip()} {result.stderr.strip()}"
    answer = json.loads(result.stdout)
    assert answer["element"]["label"] == "Order confirmed"
    assert (
        answer["captures"]["readable"] >= 2
    ), f"the text was on the first screen read, so nothing was waited for: {answer}"


def test_wait_for_says_what_the_screen_read_when_the_text_never_comes(run_skill, open_screen):
    open_screen("DelayedActivity", "--ei", "delay_ms", "120000")

    result, took = _timed(
        run_skill, "navigator.py", "--wait-for", "No such text", "--timeout", "25", "--json"
    )

    assert result.returncode == 1, result.stdout
    wait = json.loads(result.stdout)["wait"]
    assert wait["outcome"] == "not_observed"
    assert "Placing order" in wait["texts"]
    assert took <= 25 + STARTUP_SLACK_S, f"a 25s wait took {took:.1f}s"


def test_wait_for_on_a_screen_that_never_settles_cannot_read_it_and_leaves_it_readable(
    run_skill, open_screen
):
    open_screen("NeverIdleActivity", "--es", "mode", "view_ticker")

    result, took = _timed(
        run_skill, "navigator.py", "--wait-for", "Synced", "--timeout", "15", "--json"
    )

    assert result.returncode == 1, result.stdout
    answer = json.loads(result.stdout)
    assert answer["wait"]["outcome"] == "unreadable"
    assert answer["wait"]["captures"]["readable"] == 0
    assert took <= 15 + STARTUP_SLACK_S, f"a 15s wait took {took:.1f}s"

    # The last dump of that wait was stopped at the deadline. The command an
    # agent would run next must work. (That the very next DUMP is a screen, with
    # nothing in between, is the recording uiautomator_dump_right_after_a_stop;
    # this opens another screen first and reads through a capture that retries.)
    open_screen("WalkActivity", "--ei", "screen", "5")
    mapped = run_skill("screen_mapper.py")
    assert mapped.returncode == 0, (
        f"the screen could not be read right after a wait ran out of time: "
        f"{mapped.stderr.strip()}"
    )
    assert "open_settings_button" in mapped.stdout


def test_tap_then_expect_finds_text_that_follows_on_the_delayed_screen(run_skill, open_screen):
    open_screen("DelayedActivity", "--ei", "delay_ms", str(DELAY_MS))

    result = run_skill(
        "navigator.py", "--tap-at", EMPTY_SPOT, "--expect", "Order confirmed", "--timeout", PATIENT
    )

    assert result.returncode == 0, f"{result.stdout.strip()} {result.stderr.strip()}"
    tapped, found, screen, *_ = result.stdout.splitlines()
    assert tapped.startswith("Tapped at (")
    assert found.startswith("Found: text='Order confirmed' after ")
    assert screen.startswith("Screen: ")


def test_tap_then_expect_on_a_screen_that_never_settles_says_the_tap_was_sent(
    run_skill, open_screen
):
    open_screen("NeverIdleActivity", "--es", "mode", "view_ticker")

    result, took = _timed(
        run_skill, "navigator.py", "--tap-at", EMPTY_SPOT, "--expect", "Done", "--timeout", "15"
    )

    assert result.returncode == 1, result.stdout
    error, remedy = result.stderr.strip().splitlines()
    assert error.startswith("Error: Tapped at (")
    assert "Could not read the screen" in error
    assert "tap was sent" in remedy
    assert took <= 15 + STARTUP_SLACK_S, f"a 15s wait took {took:.1f}s"


def test_the_screen_printed_after_a_tap_is_enough_to_act_again(run_skill, open_screen):
    """Checkout -> confirmation -> catalog, each step written from the one before's output."""
    open_screen("WalkActivity", "--ei", "screen", "4")

    first = run_skill(
        "navigator.py", "--find-text", "Place order", "--tap", "--expect", "Order placed"
    )

    assert first.returncode == 0, f"{first.stdout.strip()} {first.stderr.strip()}"
    _before, header, summary = first.stdout.partition("Screen: ")
    assert header, f"no screen was printed after the tap:\n{first.stdout}"
    names = [name for line in summary.splitlines()[1:] for name in _QUOTED.findall(line)]
    assert "back_to_catalog_button" in names, first.stdout

    second = run_skill(
        "navigator.py", "--find-text", "back_to_catalog_button", "--tap", "--expect", "Open cart"
    )

    assert second.returncode == 0, f"{second.stdout.strip()} {second.stderr.strip()}"
    assert '"open_cart_button"' in second.stdout
