"""The recorder puts a device back, and says so only after reading it. No device needed.

The v0.10.0 recorders change things a later run would inherit: three animation
scales, ``sysui_demo_allowed``, whether the display is awake. Review of the
first version found each of them could be left changed:

- the animation scales were restored in a ``finally`` behind a force-stop, so a
  timeout in that one command skipped all three, and the readback ran only when
  the capture had succeeded;
- ``sysui_demo_allowed`` was written and never put back;
- a teardown command that timed out left the recording loop before
  ``teardown_verify`` ran, for every fixture that has one.

Also here: the delayed pair's clock. Its first version timed both dumps from the
moment ``am start -W`` *returned*, so a slow launch could put the first dump
after the text had changed while the manifest said three seconds.

A second review found three more: a settings read that FAILED was saved as the
original value and later written into the setting; the saved value was
forgotten before the restore was known to have worked; and nothing checked that
demo mode had actually been left. A third found the check written for that
could be satisfied by one changed pixel. Three holes in one mechanism is a
reason to remove the mechanism: the screenshots no longer use demo mode. The
status bar is painted out of the image on the host, and the device is not
touched (``_walk_screenshot``).

The fake device below is a dict of settings and a wakefulness flag. Where a test
needs what a real tool printed or drew, it reads the recording.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest
import record_fixtures as rf
from conftest import RECORDED_ROOT, RecordedFixtures

SERIAL = "emulator-5554"
SCALES = rf._ANIMATION_SCALES


class FakeDevice:
    """Answers the handful of adb commands the restore paths send."""

    # What adb says for a serial it cannot reach: recorded, not remembered.
    refusal = RecordedFixtures(RECORDED_ROOT / "emulator-api35").text("adb_device_not_found")

    def __init__(self, settings: dict[str, str]):
        self.settings = dict(settings)
        self.calls: list[list[str]] = []
        self.fail_on: list[list[str]] = []
        self.ignore_puts_to: set[str] = set()
        self.unreadable: set[str] = set()
        self.launch_report = ""
        self.clock = "02:46"

    def run(self, args, _serial, *_timeout, **_keywords):
        self.calls.append(list(args))
        if any(args[: len(prefix)] == prefix for prefix in self.fail_on):
            raise rf.CaptureError("timed out after 15s")
        if args[:4] == ["shell", "settings", "get", "global"]:
            if args[4] in self.unreadable:
                return rf.Capture("", self.refusal, 1)
            return rf.Capture(self.settings.get(args[4], "null") + "\n", "", 0)
        if args[:4] == ["shell", "am", "start", "-W"]:
            return rf.Capture(self.launch_report, "", 0)
        if args[:2] == ["shell", "date"]:
            return rf.Capture(self.clock + "\n", "", 0)
        if args[:4] == ["shell", "settings", "put", "global"]:
            if args[4] not in self.ignore_puts_to:
                self.settings[args[4]] = args[5]
            return rf.Capture("", "", 0)
        if args[:4] == ["shell", "settings", "delete", "global"]:
            self.settings.pop(args[4], None)
            return rf.Capture("Deleted 1 rows\n", "", 0)
        return rf.Capture("", "", 0)


@pytest.fixture
def device(monkeypatch):
    fake = FakeDevice({"window_animation_scale": "1.0", "transition_animation_scale": "0.5"})
    monkeypatch.setattr(rf, "_run", fake.run)
    monkeypatch.setattr(rf, "_SAVED_GLOBAL_SETTINGS", {})
    return fake


def _zero_the_scales(device: FakeDevice) -> dict[str, str]:
    original = dict(device.settings)
    for key in SCALES:
        rf._put_global_setting(SERIAL, key, "0")
    assert {device.settings[key] for key in SCALES} == {"0"}
    return original


# --- settings ---------------------------------------------------------------


def test_every_scale_is_put_back_and_an_unset_one_is_deleted(device):
    original = _zero_the_scales(device)

    rf._global_settings_restored(*SCALES)(SERIAL)

    assert device.settings == original, "an unset scale must end unset, not written as 'null'"
    assert "animator_duration_scale" not in device.settings


def test_one_restore_timing_out_does_not_skip_the_others(device):
    original = _zero_the_scales(device)
    device.fail_on = [["shell", "settings", "put", "global", "window_animation_scale"]]

    with pytest.raises(rf.CaptureError) as excinfo:
        rf._global_settings_restored(*SCALES)(SERIAL)

    assert "window_animation_scale" in str(excinfo.value)
    assert device.settings["transition_animation_scale"] == original["transition_animation_scale"]
    assert "animator_duration_scale" not in device.settings
    assert device.settings["window_animation_scale"] == "0", "the fake restored what it refused"


def test_a_restore_that_did_not_take_is_reported_from_the_readback(device):
    _zero_the_scales(device)
    device.ignore_puts_to = {"transition_animation_scale"}

    with pytest.raises(rf.CaptureError) as excinfo:
        rf._global_settings_restored(*SCALES)(SERIAL)

    assert "transition_animation_scale was '0.5' and is '0'" in str(excinfo.value)


def test_a_change_that_did_not_take_stops_the_recorder(device):
    device.ignore_puts_to = {"window_animation_scale"}

    with pytest.raises(rf.CaptureError, match=re.escape("read back as '1.0'")):
        rf._put_global_setting(SERIAL, "window_animation_scale", "0")


def test_a_run_that_changed_nothing_restores_nothing(device):
    rf._global_settings_restored(*SCALES)(SERIAL)

    assert device.calls == []


def test_a_second_change_does_not_overwrite_what_was_saved(device):
    rf._put_global_setting(SERIAL, "window_animation_scale", "0")
    rf._put_global_setting(SERIAL, "window_animation_scale", "2")

    rf._global_settings_restored("window_animation_scale")(SERIAL)

    assert device.settings["window_animation_scale"] == "1.0"


def test_a_read_that_failed_is_never_saved_as_the_original(device):
    """`error: device ...` must not become the value restored into the setting."""
    device.unreadable = {"window_animation_scale"}

    with pytest.raises(rf.CaptureError, match="could not read global setting"):
        rf._put_global_setting(SERIAL, "window_animation_scale", "0")

    assert rf._SAVED_GLOBAL_SETTINGS == {}
    assert not [call for call in device.calls if call[2] == "put"], "it wrote anyway"
    assert device.settings["window_animation_scale"] == "1.0"


def test_a_restore_that_failed_can_be_retried(device):
    original = _zero_the_scales(device)
    device.fail_on = [["shell", "settings", "put", "global", "window_animation_scale"]]
    with pytest.raises(rf.CaptureError):
        rf._global_settings_restored(*SCALES)(SERIAL)

    device.fail_on = []
    rf._global_settings_restored(*SCALES)(SERIAL)

    assert device.settings == original
    assert rf._SAVED_GLOBAL_SETTINGS == {}


def test_a_failed_restore_is_not_taken_for_the_baseline_by_the_next_change(device):
    _zero_the_scales(device)
    device.ignore_puts_to = {"transition_animation_scale"}
    with pytest.raises(rf.CaptureError):
        rf._global_settings_restored(*SCALES)(SERIAL)

    device.ignore_puts_to = set()
    rf._put_global_setting(SERIAL, "transition_animation_scale", "0")
    rf._global_settings_restored(*SCALES)(SERIAL)

    assert device.settings["transition_animation_scale"] == "0.5"


def test_a_setting_changed_between_two_recordings_is_restored_to_its_newer_value(device):
    rf._put_global_setting(SERIAL, "window_animation_scale", "0")
    rf._global_settings_restored("window_animation_scale")(SERIAL)
    device.settings["window_animation_scale"] = "0.25"  # somebody changed it since

    rf._put_global_setting(SERIAL, "window_animation_scale", "0")
    rf._global_settings_restored("window_animation_scale")(SERIAL)

    assert device.settings["window_animation_scale"] == "0.25", "a stale saved value came back"


# --- the fixtures' own hooks, run -------------------------------------------


def _fixture(name: str) -> rf.Fixture:
    return next(fixture for fixture in rf.FIXTURES if fixture.name == name)


def test_the_animations_off_fixture_zeroes_the_scales_and_puts_them_back(device, monkeypatch):
    monkeypatch.setattr(rf.time, "sleep", lambda _seconds: None)
    device.launch_report = RecordedFixtures(RECORDED_ROOT / "emulator-api35").text(
        "am_start_wait_settings"
    )
    original = dict(device.settings)
    fixture = _fixture("uiautomator_never_idle_animations_off")

    fixture.prepare(None, SERIAL)
    during = {key: device.settings[key] for key in SCALES}
    fixture.teardown_verify(SERIAL)

    assert set(during.values()) == {"0"}
    assert device.settings == original
    assert fixture.metadata(SERIAL) == {"animation_scales_during_capture": "0"}


# --- the walk screenshots: an edit on the host, nothing on the device ---------


def _a_screen_with_a_status_bar() -> tuple[bytes, bytes, str]:
    """A raw screenshot as the device would send it, the recorded one, and its dump.

    The corpus holds no unedited whole screen -- that is the point of the edit --
    so one is put back together from two recordings: the walk screenshot, and
    over its top rows `demo_mode_clock_941.png`, a real status bar with a clock.
    """
    import io

    from PIL import Image

    api35 = RecordedFixtures(RECORDED_ROOT / "emulator-api35")
    recorded = api35.data("screencap_walk_1_catalog.png")
    screen = Image.open(io.BytesIO(recorded)).convert("RGB")
    status_bar = Image.open(io.BytesIO(api35.data("demo_mode_clock_941.png"))).convert("RGB")
    assert len(status_bar.getcolors(maxcolors=1 << 20)) > 1, "that strip has nothing drawn in it"
    screen.paste(status_bar, (0, 0))
    raw = io.BytesIO()
    screen.save(raw, format="PNG")
    return raw.getvalue(), recorded, api35.text("uiautomator_walk_1_catalog")


@pytest.fixture
def screen(monkeypatch):
    """Answer the screenshot recorder's dump and screencap; collect what it sent."""
    answers: dict = {"sent": []}

    def fake_run(args, _serial, *_timeout, **_keywords):
        answers["sent"].append(list(args))
        return rf.Capture(answers["dump"], "", 0)

    monkeypatch.setattr(rf, "_run", fake_run)
    monkeypatch.setattr(rf, "_run_bytes", lambda *_args, **_keywords: answers["png"])
    return answers


def test_the_status_bar_is_painted_out_and_the_app_is_not(screen):
    import io

    from PIL import Image

    screen["png"], recorded, screen["dump"] = _a_screen_with_a_status_bar()
    extra: dict = {}

    payload = rf._walk_screenshot("Open cart")(SERIAL, extra)

    image = Image.open(io.BytesIO(payload)).convert("RGB")
    # Where the app starts, read here from the recorded dump and not from the
    # recorder: moving its paint and its bookkeeping together must not pass.
    title = next(
        node
        for node in ET.fromstring(screen["dump"]).iter("node")
        if node.get("resource-id") == "screen_title"
    )
    rows = int(re.match(r"\[\d+,(\d+)\]", title.get("bounds")).group(1))
    assert extra["status_bar_rows_painted_out"] == rows
    assert rows >= rf.STATUS_BAR_STRIP_PX, "fewer rows than the status bar is tall"
    assert len(image.crop((0, 0, image.width, rows)).getcolors(maxcolors=1 << 20)) == 1
    untouched = (0, rows, image.width, image.height)
    assert (
        image.crop(untouched).tobytes()
        == Image.open(io.BytesIO(recorded)).convert("RGB").crop(untouched).tobytes()
    ), "pixels of the app were changed"
    assert (extra["width"], extra["height"]) == image.size == (1080, 2424)


def test_taking_a_screenshot_sends_the_device_nothing_but_dumps(screen):
    screen["png"], _recorded, screen["dump"] = _a_screen_with_a_status_bar()

    rf._walk_screenshot("Open cart")(SERIAL, {})

    assert screen["sent"] == [rf._DUMP, rf._DUMP], "one dump before the screencap and one after"


def test_a_screencap_that_is_not_an_image_is_a_capture_error(screen):
    """Pillow's own exception would leave the recording loop; this one is reported."""
    png, _recorded, screen["dump"] = _a_screen_with_a_status_bar()
    screen["png"] = png[:200]

    with pytest.raises(rf.CaptureError, match="did not return an image"):
        rf._walk_screenshot("Open cart")(SERIAL, {})


def test_a_screen_with_no_title_to_measure_by_is_refused(screen):
    """The older Compose screen carries no `screen_title`: nothing says where its content starts."""
    screen["png"], _recorded, _dump = _a_screen_with_a_status_bar()
    screen["dump"] = RecordedFixtures(RECORDED_ROOT / "emulator-api35").text(
        "uiautomator_compose_testtags"
    )

    with pytest.raises(rf.CaptureError, match="no screen_title node"):
        rf._walk_screenshot("Submit Order")(SERIAL, {})


def test_the_screenshot_fixtures_change_nothing_on_the_device():
    screenshots = [f for f in rf.FIXTURES if f.name.startswith("screencap_walk_")]

    assert len(screenshots) == len(rf.WALK_SCREENS)
    for fixture in screenshots:
        commands = [*fixture.setup, *fixture.teardown]
        assert fixture.prepare is None and fixture.teardown_verify is None
        assert fixture.teardown == [["shell", "am", "force-stop", rf.COMPOSE_PACKAGE]]
        assert not [c for c in commands if "settings" in c or "broadcast" in c], fixture.name
    assert _fixture("uiautomator_dump_screen_asleep").teardown_verify is rf._device_awake


# --- the loop: a teardown command that raises ---------------------------------


def test_a_teardown_that_times_out_still_reaches_the_readback(monkeypatch, tmp_path, capsys):
    """record() on the asleep fixture, with its wake-up key event timing out once.

    Before the fix the CaptureError left the loop: no ``_device_awake``, no
    report, and the exception out of ``record`` itself.
    """
    api35 = RecordedFixtures(RECORDED_ROOT / "emulator-api35")
    device = api35.manifest["device"]
    props = {
        "ro.product.model": device["model"],
        "ro.build.version.sdk": device["api_level"],
        "ro.build.version.release": device["android_release"],
        "ro.product.cpu.abi": device["abi"],
        "ro.build.fingerprint": device["build_fingerprint"],
    }
    asleep_answer = api35.text("uiautomator_dump_screen_asleep")
    state = {"awake": True, "wakeups": 0}

    def fake_run(args, _serial, *_timeout, **_keywords):
        if args[:2] == ["shell", "getprop"]:
            return rf.Capture(props[args[2]] + "\n", "", 0)
        if args == ["shell", "input", "keyevent", "KEYCODE_SLEEP"]:
            state["awake"] = False
        elif args == ["shell", "input", "keyevent", "KEYCODE_WAKEUP"]:
            state["wakeups"] += 1
            if state["wakeups"] == 1:
                raise rf.CaptureError("timed out after 30s")
            state["awake"] = True
        elif args == rf._DUMP:
            return rf.Capture(asleep_answer, "", 0)
        elif args == ["shell", "dumpsys", "power"]:
            wakefulness = "Awake" if state["awake"] else "Asleep"
            return rf.Capture(f"  mWakefulness={wakefulness}\n", "", 0)
        return rf.Capture("", "", 0)

    monkeypatch.setattr(rf, "_run", fake_run)
    monkeypatch.setattr(rf, "RECORDED_ROOT", tmp_path)
    monkeypatch.setattr(rf.time, "sleep", lambda _seconds: None)

    status = rf.record(SERIAL, {"uiautomator_dump_screen_asleep"}, "p")

    assert state["awake"], "the device was left asleep"
    assert status == 1, "a teardown command that failed was not reported"
    assert "KEYCODE_WAKEUP" in capsys.readouterr().err
    assert (tmp_path / "p" / "uiautomator_dump_screen_asleep.txt").read_text(
        encoding="utf-8"
    ) == asleep_answer


# --- the delayed pair's clock -------------------------------------------------


class FakeClock:
    """``time.monotonic`` that only moves when a command or a sleep takes time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _delayed_launch(monkeypatch, *, launch_takes: float, dump_takes: float):
    """Run ``_delayed_window`` against recorded answers on a fake clock."""
    api35 = RecordedFixtures(RECORDED_ROOT / "emulator-api35")
    launch_report = api35.text("am_start_wait_settings")
    dumps = [api35.text("uiautomator_delayed_before"), api35.text("uiautomator_delayed_after")]
    clock = FakeClock()

    def fake_run(args, _serial, *_timeout, **_keywords):
        if args[:4] == ["shell", "am", "start", "-W"]:
            clock.now += launch_takes
            return rf.Capture(launch_report, "", 0)
        if args == rf._DUMP:
            clock.now += dump_takes
            return rf.Capture(dumps.pop(0), "", 0)
        return rf.Capture("", "", 0)

    monkeypatch.setattr(rf, "_run", fake_run)
    monkeypatch.setattr(rf.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rf.time, "sleep", clock.sleep)
    monkeypatch.setattr(rf, "_TEXT_TWINS", {})
    extra: dict = {}
    return rf._delayed_window(None, SERIAL, extra), extra


def test_a_slow_launch_cannot_pass_for_a_before_capture(monkeypatch):
    """Ten seconds of launch and three of dump is thirteen: past the twelve-second delay.

    The texts are right -- these are the real before and after documents -- and
    the capture is still refused, because the clock cannot show the first dump
    preceded the change.
    """
    with pytest.raises(rf.CaptureError, match=re.escape("13.0s after the launch was requested")):
        _delayed_launch(monkeypatch, launch_takes=10.0, dump_takes=3.0)


def test_the_clock_is_counted_from_the_launch_request(monkeypatch):
    before, extra = _delayed_launch(monkeypatch, launch_takes=2.0, dump_takes=3.0)

    assert "Placing order" in before.text
    assert extra["launch_command_took_s"] == 2.0
    assert extra["first_dump_finished_s_after_launch_requested"] == 5.0
    assert extra["second_dump_started_s_after_launch_returned"] == rf.DELAYED_MS / 1000 + 3
    assert "uiautomator_delayed_after" in rf._TEXT_TWINS
