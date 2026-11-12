"""Ground truth for reading a screen that is changing, late, or not there.

Recorded for v0.10.0 from three activities added to the Compose scaffold
(``tests/fixtures/scaffold/compose``, ``WalkActivity.kt``), before anything was
written that waits on a screen. What the recordings say, and what was expected
instead:

1. **A dump that cannot read the screen exits 0 and says so on stdout.** Both
   failures -- ``could not get idle state`` and ``null root node`` -- arrive as a
   single line on stdout with an empty stderr. An exit status or a stderr check
   sees a success.
2. **An animating Compose screen did not stop a dump.** Three never-ending
   Compose screens were built to obtain a capture failure: a Text changing every
   100 ms, an endless alpha animation, an indeterminate spinner. All three
   dumped normally. The failure came from an ``android.widget.TextView`` whose
   text keeps changing.
3. **Disabling animations did not fix it.** With all three animation scales at
   0 that TextView screen fails the same way.
4. **Text can arrive with no input.** One launch, two dumps, nothing sent
   between them: the first document does not hold the text the second does.
5. **A walk is six screens.** The flow's six dumps are six different documents,
   each with its own screenshot (the status bar painted out of each).

How far this reaches: one fixture app, API 35 and API 33, where findings 1 to 4
came out the same (their tests run against both profiles; the walk is API 35
only). Findings 2 and 3 are counterexamples, not rules. They show that "the
screen is animating" does not by itself predict a failed dump and that
"disable animations" does not by itself cure one; they do not show that no
Compose screen can block a dump, or that animations never do.

Nothing here feeds a hand-written dump to anything: every payload is read from
``tests/fixtures/recorded/``.
"""

from __future__ import annotations

import io
import subprocess
import xml.etree.ElementTree as ET

import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from navigator import Navigator
from screen_mapper import ScreenMapper

from common import adb_exec, hierarchy

VIEW_TICKER = "uiautomator_never_idle_view_ticker"
ANIMATIONS_OFF = "uiautomator_never_idle_animations_off"
ASLEEP = "uiautomator_dump_screen_asleep"
COMPOSE_NEVER_IDLE = {
    "uiautomator_never_idle_compose_ticker": "sync_counter",
    "uiautomator_never_idle_compose_animation": "sync_pulse",
    "uiautomator_never_idle_compose_progress": "sync_spinner",
}
BEFORE = "uiautomator_delayed_before"
AFTER = "uiautomator_delayed_after"

# (name suffix, a text only that screen carries) -- as the recorder states it.
WALK = (
    ("1_catalog", "Open cart"),
    ("2_product", "Add to cart"),
    ("3_cart", "Keep shopping"),
    ("4_checkout", "Place order"),
    ("5_confirmation", "Arrives Thursday"),
    ("6_settings", "Use cellular data"),
)


def _manifest_entry(recorded, name: str) -> dict:
    entries = [entry for entry in recorded.manifest["fixtures"] if entry["name"] == name]
    assert len(entries) == 1, f"{name}: {len(entries)} manifest entries"
    return entries[0]


def _texts(root: ET.Element) -> set[str]:
    return {node.get("text") for node in root.iter("node") if node.get("text")}


@pytest.fixture(params=["emulator-api35", "emulator-api33"])
def emulator(request) -> RecordedFixtures:
    """Each emulator profile that recorded the capture-behaviour fixtures."""
    return RecordedFixtures(RECORDED_ROOT / request.param)


@pytest.fixture
def device_answers(monkeypatch):
    """Serve recorded dump output in place of a device, at the recorded exit status."""
    calls: list[list[str]] = []

    def install(payload: str, returncode: int):
        def run(cmd, **_kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, returncode, payload, "")

        monkeypatch.setattr(adb_exec.subprocess, "run", run)
        monkeypatch.setattr(hierarchy.time, "sleep", lambda _seconds: None)
        return calls

    return install


# ---------------------------------------------------------------------------
# 1. A failed dump exits 0 and reports on stdout.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "says"),
    [
        (VIEW_TICKER, "ERROR: could not get idle state."),
        (ANIMATIONS_OFF, "ERROR: could not get idle state."),
        (ASLEEP, "ERROR: null root node returned by UiTestAutomationBridge."),
    ],
)
def test_a_dump_that_cannot_read_the_screen_exits_0_and_says_so_on_stdout(emulator, name, says):
    entry = _manifest_entry(emulator, name)

    assert emulator.text(name).strip() == says
    assert entry["exit_status"] == 0, "the tool's exit status is not where this failure shows"
    assert entry["stream"] == "stdout"
    assert entry["stderr_bytes"] == 0, "stderr was not empty; a reader could have used it"


@pytest.mark.parametrize(
    ("name", "expected"),
    [(VIEW_TICKER, "idle"), (ASLEEP, "no focused window")],
)
def test_the_capture_layer_turns_the_recorded_failure_into_an_error(
    emulator, device_answers, name, expected
):
    """Exit 0 plus one line of text must not come back as a screen."""
    entry = _manifest_entry(emulator, name)
    calls = device_answers(emulator.text(name), entry["exit_status"])

    with pytest.raises(hierarchy.HierarchyError) as excinfo:
        hierarchy.capture_hierarchy("emulator-5554", retries=3)

    assert expected in str(excinfo.value)
    assert len(calls) == 3, "a transient failure was not retried up to the attempt limit"


# ---------------------------------------------------------------------------
# 2. Compose content that never stops does not block the dump.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "tag"), sorted(COMPOSE_NEVER_IDLE.items()))
def test_a_compose_screen_that_never_stops_still_dumps(emulator, device_answers, name, tag):
    entry = _manifest_entry(emulator, name)
    calls = device_answers(emulator.text(name), entry["exit_status"])

    root = hierarchy.capture_hierarchy("emulator-5554")

    assert len(calls) == 1, "the dump of an animating Compose screen needed a retry"
    assert "Syncing" in _texts(root)
    assert tag in {node.get("resource-id") for node in root.iter("node")}


def test_the_compose_ticker_was_caught_mid_count(emulator):
    """The control is only a control if its content really was changing."""
    root = ET.fromstring(emulator.text("uiautomator_never_idle_compose_ticker"))
    counters = [text for text in _texts(root) if text.startswith("Synced ")]

    assert len(counters) == 1
    assert int(counters[0].split()[1]) > 0, "the ticker had not ticked when it was dumped"


# ---------------------------------------------------------------------------
# 3. Disabling animations does not help.
# ---------------------------------------------------------------------------


def test_the_same_screen_fails_the_same_way_with_animations_disabled(emulator):
    entry = _manifest_entry(emulator, ANIMATIONS_OFF)

    assert entry["animation_scales_during_capture"] == "0"
    assert "could not get idle state" in emulator.text(ANIMATIONS_OFF)
    assert emulator.text(ANIMATIONS_OFF) == emulator.text(VIEW_TICKER)


# ---------------------------------------------------------------------------
# 4. Text that arrives with no input.
# ---------------------------------------------------------------------------


def test_the_delayed_pair_s_clock_puts_one_dump_before_the_change_and_one_after(emulator):
    """Bounds that hold wherever inside the launch the activity started its timer.

    The text changes no earlier than the launch request plus the delay, and no
    later than the launch command's return plus the delay. That the two dumps
    are of one launch, with nothing sent between, is what the recorder does
    (``_delayed_window``); a manifest cannot prove it, only state it.
    """
    before, after = _manifest_entry(emulator, BEFORE), _manifest_entry(emulator, AFTER)
    delay_s = before["delay_ms"] / 1000

    assert before["input_between_dumps"] == after["input_between_dumps"] == "none"
    assert before["first_dump_finished_s_after_launch_requested"] < delay_s
    assert after["second_dump_started_s_after_launch_returned"] > delay_s
    for key in ("delay_ms", "launch_command_took_s"):
        assert before[key] == after[key], f"{key}: the two entries describe different launches"


def test_the_late_text_is_in_the_second_document_only(emulator):
    before = ET.fromstring(emulator.text(BEFORE))
    after = ET.fromstring(emulator.text(AFTER))

    assert "Placing order" in _texts(before)
    assert "Order confirmed" not in _texts(before)
    assert {"Order confirmed", "Order #5107"} <= _texts(after)
    assert "Placing order" not in _texts(after)


def test_a_text_search_misses_then_finds_the_late_text(emulator):
    """The two answers a poll for this text would get, from the real matcher.

    The text is a passive label, not a control. That it is found at all is the
    fact a wait depends on.
    """
    navigator = Navigator("emulator-5554")
    before = ET.fromstring(emulator.text(BEFORE))
    after = ET.fromstring(emulator.text(AFTER))

    # The same screen both times: a miss on some unrelated screen proves nothing.
    assert navigator._find_in(before, text="Placing order") is not None
    assert navigator._find_in(before, text="Order confirmed") is None
    assert navigator._find_in(after, text="Order confirmed") is not None
    assert navigator._find_in(after, text="Placing order") is None


# ---------------------------------------------------------------------------
# 5. Six screens, six documents, six screenshots.
# ---------------------------------------------------------------------------


def test_each_walk_marker_is_on_exactly_one_screen(recorded):
    screens = {
        suffix: _texts(ET.fromstring(recorded.text(f"uiautomator_walk_{suffix}")))
        for suffix, _marker in WALK
    }

    for suffix, marker in WALK:
        holders = sorted(name for name, texts in screens.items() if marker in texts)
        assert holders == [suffix], f"{marker!r} is on {holders}, not only on {suffix}"


def test_the_walk_is_six_different_documents(recorded):
    documents = {recorded.text(f"uiautomator_walk_{suffix}") for suffix, _marker in WALK}

    assert len(documents) == len(WALK)


@pytest.mark.parametrize("suffix", [suffix for suffix, _marker in WALK])
def test_every_walk_screen_has_something_to_act_on(recorded, suffix):
    root = ET.fromstring(recorded.text(f"uiautomator_walk_{suffix}"))

    analysis = ScreenMapper().analyze_tree(root)

    assert analysis["interactive_elements"] >= 1, f"{suffix}: the mapper found nothing to act on"


@pytest.mark.parametrize("suffix", [suffix for suffix, _marker in WALK])
def test_every_walk_screen_has_a_screenshot_of_the_recorded_size(recorded, suffix):
    # Not importorskip: a screenshot check that skips without Pillow checks nothing.
    from PIL import Image

    name = f"screencap_walk_{suffix}"
    entry = _manifest_entry(recorded, name)

    image = Image.open(io.BytesIO(recorded.data(f"{name}.png")))

    assert image.format == "PNG"
    assert image.size == (entry["width"], entry["height"])
    # The status bar is the recording machine's; the committed image has none of it.
    rows = entry["status_bar_rows_painted_out"]
    assert 0 < rows < image.height // 4
    painted = image.convert("RGB").crop((0, 0, image.width, rows))
    assert len(painted.getcolors(maxcolors=1 << 20)) == 1, f"{name}: something is drawn up there"
    assert entry["bytes"] == len(recorded.data(f"{name}.png"))
