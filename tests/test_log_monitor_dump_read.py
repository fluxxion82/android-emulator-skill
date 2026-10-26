"""log_monitor on API 33: a live read made of dumps, so logcat's stdio buffering loses nothing.

Android 13's logcat writes ``-B`` records through stdio and flushes only after
a TEXT record (AOSP system/logging logcat.cpp, android13-release), so a live
``logcat -B -T 1`` stopped by its reader loses what it held back. Recorded on
the API 33 emulator (``logcat_stream_*``, profile emulator-api33): a 5 s stream
delivered no byte while its window held records, none of 5 marks logged into
it, and -- marks padded past 4 KiB -- one 4096-byte block cut inside a record.
``log_monitor --duration 3s --json`` reported ``total_lines: 0`` and success.

On that level (and only there: ``getprop_sdk``) a live read no longer streams.
It opens on the newest record a system process wrote (``-d -B -T 1``, doubled
until it holds one) and, every poll interval in every mode and once more at the
end, takes what logd delivers after that record and the records already read
after it, found in a count-based tail of logd's order, ``-d -B -T N``
(``common.logcat.read_after``) -- never by a timestamp, never by an app's
record (an app can resend one byte for byte). Each tail must hold that record's
frame byte for byte, exactly once (doubled while it is too short to reach it),
with the records read after it next, or the read is refused; and a dump is
accepted only once the device answers ``get-state``. That proves logd still
held the records when the dump began -- not that the window is complete: a
capture is what logd
delivered, and logd may drop records it prunes while a reader is behind
(pinned below, so nobody turns it into a certificate).

The fake device answers as the device did in the recorded run: the opening
record (``_open_records``) and the window read back (``_window_records``,
which begins with it: manifest ``window.open_record_at`` 0), of which the fake
answers ``-T N`` with the last N records, as logd does (logcat_tail_records). Codex's review of the
previous design (stream, then align a dump with it) named seven ways that
alignment failed; each has a spec below, marked ``(n)``, saying whether it
passes under this design or cannot arise in it.
"""

from __future__ import annotations

import errno
import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

import log_monitor
import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from fake_toolchain import (
    FakeToolchain,
    Frame,
    Rule,
    assert_refusal,
    framed,
    frames,
    uid_rules,
)
from test_log_monitor import (
    PACKAGE,
    RECORDS,
    _lines,
    _not_running,
    _restart_pids,
    _system_server,
)

from common import adb_exec, cli, logcat

API33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")
API35 = RecordedFixtures(RECORDED_ROOT / "emulator-api35")
SERIAL = "emulator-5556"
LIVE = ("--serial", SERIAL, "--duration", "1s")
MARK_TAG = "aes-recorder"


@dataclass(frozen=True)
class Scenario:
    """One recorded run of ``_stream_scenario`` (tests/record_fixtures.py)."""

    stream: bytes
    opened: bytes
    window: bytes
    entry: dict

    @property
    def anchor(self) -> Frame:
        (frame,) = frames(self.opened)
        return frame

    @property
    def after(self) -> list[Frame]:
        """The window's records after the opening record, by its bytes (struct, not the reader)."""
        held = frames(self.window)
        at = [index for index, frame in enumerate(held) if frame.raw == self.anchor.raw]
        assert at, "the recorded window does not hold the opening record"
        return held[at[-1] + 1 :]


def _scenario(profile: RecordedFixtures, name: str) -> Scenario:
    entry = next(e for e in profile.manifest["fixtures"] if e["name"] == f"{name}_records")
    return Scenario(
        stream=profile.data(f"{name}_records.bin"),
        opened=profile.data(f"{name}_open_records.bin"),
        window=profile.data(f"{name}_window_records.bin"),
        entry=entry,
    )


def _shown(records: list[Frame] | bytes) -> list[str]:
    """The lines log_monitor's default floor (D) keeps of ``records``, as it prints them."""
    data = records if isinstance(records, bytes) else framed(records)
    order = logcat.PRIORITY_ORDER
    return [
        line
        for record in logcat.parse_records(data)
        if record.priority in order and order.index(record.priority) >= order.index("D")
        for line in record.lines()
    ]


def _marks(frames_: list[Frame]) -> list[str]:
    return [f.message for f in frames_ if f.tag == MARK_TAG and f.message.startswith("stream-mark")]


def _stamp(frame: Frame) -> tuple[int, int]:
    return struct.unpack_from("<II", frame.raw, 12)


def _restamped(frame: Frame, sec: int, nsec: int) -> Frame:
    """``frame`` with its writer's time substituted -- a writer clock elsewhere."""
    raw = bytearray(frame.raw)
    struct.pack_into("<II", raw, 12, sec, nsec)
    return Frame(bytes(raw))


def _last_byte(frame: Frame, value: int) -> Frame:
    """``frame`` with its message's last byte replaced: same length, same header."""
    raw = bytearray(frame.raw)
    raw[-2] = value  # the byte before the message's closing NUL
    return Frame(bytes(raw))


@pytest.fixture
def toolchain(tmp_path) -> FakeToolchain:
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    return chain


def _device(
    toolchain: FakeToolchain,
    recorded,
    scenario: Scenario,
    *,
    window: bytes | None = None,
    newest: bytes | None = None,
    dump: Rule | None = None,
    whole: bytes | None = None,
    level: RecordedFixtures = API33,
    sdk: Rule | None = None,
    state: tuple[Rule, ...] | None = None,
    newest_rule: Rule | None = None,
    extra: tuple[Rule, ...] = (),
) -> None:
    """A device answering as it did in ``scenario``'s recorded run.

    ``getprop ro.build.version.sdk`` as ``level`` recorded it; ``-d -B -T 1``
    with the opening record (or ``newest``; ``newest_rule`` replaces the
    rule); a ``-T N`` tail of the recorded window (or ``window``), its last N
    records as logd answers it (``dump`` replaces the rule); a whole-buffer
    dump with ``whole``; and a live read replaying the stream's recorded
    bytes, then waiting -- as the device's did. ``sdk`` replaces the getprop
    answer, ``state`` the ``get-state`` rules. ``extra`` rules go after the
    opening question's, so a ``-T`` rule there answers the polls only.
    """
    toolchain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *(state or (Rule(("get-state",), stdout=recorded.text("adb_get_state")),)),
            sdk
            or Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=level.text("getprop_sdk")),
            newest_rule
            or Rule(
                ("logcat", "-d", "-T", "1"), stdout=scenario.opened if newest is None else newest
            ),
            *extra,
            dump
            or Rule(("logcat", "-d", "-T"), buffered=scenario.window if window is None else window),
            Rule(("logcat", "-d"), stdout=whole if whole is not None else scenario.window),
            Rule(("logcat",), stdout=scenario.stream, follows=True),
        ],
    )


def _device_answer(cmd: list[str], recorded) -> subprocess.CompletedProcess:
    """An in-process adb answering ``get-state`` as recorded (adb_get_state)."""
    return subprocess.CompletedProcess(cmd, 0, recorded.text("adb_get_state"), "")


def _capture(toolchain: FakeToolchain, *args: str, **env: str) -> dict:
    result = toolchain.run("log_monitor.py", *LIVE, "--json", *args, **env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _logcat_calls(toolchain: FakeToolchain) -> list[list[str]]:
    return [call for call in toolchain.calls("adb") if "logcat" in call]


# --- the recorded ground truth --------------------------------------------------


def test_api_33_streams_withhold_records_and_the_window_dump_begins_with_the_open_record():
    """What the recordings say, before any code reads them."""
    quiet, marks, block = (
        _scenario(API33, f"logcat_stream_{name}") for name in ("quiet", "marks", "block")
    )
    for scenario in (quiet, marks):
        assert scenario.entry["stream"]["bytes_delivered"] == 0 and scenario.stream == b""
        assert scenario.entry["window"]["records_through_close"] > 0
    assert marks.entry["marks"]["in_window"] == 5
    assert marks.entry["marks"]["delivered_by_the_stream"] == 0
    assert [n for _at, n in block.entry["stream"]["chunks_at_s_and_bytes"]] == [4096]
    assert block.entry["stream"]["cut_record_bytes"] > 0
    assert block.entry["marks"]["delivered_by_the_stream"] < block.entry["marks"]["in_window"]
    for scenario in (quiet, marks, block):
        assert scenario.entry["window"]["open_record_at"] == 0
        assert frames(scenario.window)[0].raw == scenario.anchor.raw
        strict = scenario.entry.get("since_is_strict")
        if strict:
            assert strict["at_its_epoch"]["includes_the_record"] is False
            assert strict["one_ns_earlier"]["includes_the_record"] is True
    assert API33.text("getprop_sdk").strip() == "33"

    prompt = _scenario(API35, "logcat_stream_quiet")
    window = prompt.entry["window"]
    assert window["records_through_close"] > 0
    assert window["of_them_delivered_by_the_stream"] == window["records_through_close"]
    assert API35.text("getprop_sdk").strip() == "35"
    # A count-based tail is exactly the newest N whole frames, on both levels.
    for profile in (API33, API35):
        entry = next(e for e in profile.manifest["fixtures"] if e["name"] == "logcat_tail_records")
        for count, answer in entry["measured"].items():
            assert answer["frames"] == int(count) and answer["cut_bytes"] == 0, (count, answer)


# --- red: the window is the capture ---------------------------------------------


@pytest.mark.parametrize("name", ["quiet", "marks", "block"])
def test_an_api_33_capture_is_every_record_after_the_open_one(name, toolchain, recorded):
    """The window read back, after its opening record, is the capture -- not the stream.

    It was the stream: ``total_lines: 0`` for quiet and marks, the first 4 KiB
    block for block.
    """
    scenario = _scenario(API33, f"logcat_stream_{name}")
    assert scenario.after
    _device(toolchain, recorded, scenario)

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(scenario.after)
    assert capture["statistics"]["total_lines"] == len(_shown(scenario.after))
    assert capture["statistics"]["cut_records"] == 0
    shown = "\n".join(capture["sample_logs"])
    assert all(shown.count(mark) == 1 for mark in _marks(scenario.after)), shown


def test_follow_on_api_33_prints_the_window_once_and_reads_once_more_at_ctrl_c(toolchain, recorded):
    """--follow polls every second; each record is printed once; Ctrl-C reads a last time."""
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(toolchain, recorded, scenario)

    result = toolchain.run("log_monitor.py", "--serial", SERIAL, "--follow", interrupt_after=3)

    assert result.returncode == 0, result.stderr
    for line in _shown(scenario.after):
        assert result.stdout.count(line) == 1, line
    polls = [call for call in _logcat_calls(toolchain) if call[-1] != "1"]
    assert len(polls) >= 2, polls


def test_the_app_filter_reads_the_dumped_window_as_it_reads_a_stream(toolchain, recorded):
    """``--app`` over a dump-made read: the same reader, the same window.

    Derived from the restart window (from its first system record, ``_restart``):
    the device's newest record when the read opens is the window's first, and
    the dump holds the window; the app's lines are those of every record after
    it.
    """
    window = _restart(recorded)
    pids = set(_restart_pids(recorded).values())
    scenario = Scenario(stream=b"", opened=framed(window[:1]), window=framed(window), entry={})
    _device(
        toolchain,
        recorded,
        scenario,
        extra=(
            Rule(("shell", "pidof", "system_server"), stdout=_system_server(recorded)),
            *uid_rules(recorded, PACKAGE, None),
            _not_running(recorded),
        ),
    )

    capture = _capture(toolchain, "--app", PACKAGE, ANDROID_EMU_LOG_TAIL="1000")

    assert capture["sample_logs"] == _lines(framed(window[1:]), pids)
    assert capture["attribution"]["complete"] is True


# --- the was-caught control: every other level streams, as on main --------------


def test_api_35_streams_as_it_always_did(toolchain, recorded):
    """A device that says 35 is read by a stream, exactly as before: no dump is asked.

    The recorded API 35 stream delivered every record of its window before
    the stop; the capture is those records, `-T 1`'s replay of the opening one
    included. (Holds on main too, where nothing asked the level.)
    """
    scenario = _scenario(API35, "logcat_stream_quiet")
    _device(toolchain, recorded, scenario, level=API35)

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(scenario.stream)
    assert [call for call in _logcat_calls(toolchain) if "-d" in call] == []


# --- (1)..(7): Codex's constructions against the previous design ----------------


def test_1_records_logged_before_any_subscription_are_captured(toolchain, recorded):
    """(1) Passes. Open O, then A and B before the stream subscribed: B, C at exit 0.

    There is no subscription now: the window is everything logd holds after
    O. In the block recording the stream's own first record was not O but a
    later one (`-T 1` replays the newest at ITS opening); every record between
    is in the capture, first of all the one right after O.
    """
    scenario = _scenario(API33, "logcat_stream_block")
    _device(toolchain, recorded, scenario)

    capture = _capture(toolchain)

    first_after = _shown(scenario.after[:1])
    assert first_after and capture["sample_logs"][: len(first_after)] == first_after


def test_2a_two_records_that_decode_alike_are_told_apart_by_their_bytes(toolchain, recorded):
    """(2) Passes. Payloads that are not UTF-8 decode alike; their frames do not.

    Derived: the opening record, and a twin of it whose message's last byte
    is another invalid one (0xff, 0xfe), logged after it in the window. Both
    decode to the same replacement character; matching by decoded fields took
    the twin for the opening record and dropped it.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    anchor = _last_byte(scenario.anchor, 0xFF)
    twin = _last_byte(scenario.anchor, 0xFE)
    assert logcat.parse_records(anchor.raw) == logcat.parse_records(twin.raw)
    assert anchor.raw != twin.raw
    window = framed([anchor, twin, *scenario.after])
    _device(toolchain, recorded, scenario, newest=anchor.raw, window=window)

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown([twin, *scenario.after])


@pytest.mark.parametrize("shape", ["O,O,rest", "O,A,O,B"])
def test_2b_an_open_record_held_more_than_once_is_refused_as_ambiguous(shape, toolchain, recorded):
    """(2) Refused. A byte-identical copy of the opening record makes the seam a guess.

    A frame's tid and time are the writer's to supply (AOSP logd LogListener),
    so a native logger can send one header and payload twice. Codex's
    construction ``O, A, O, B`` was read as ``B`` alone; ``O, O, rest`` as
    ``rest``. Neither copy is picked now: exit 1, with ``partial``. Derived:
    the recorded opening record repeated whole.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    first, *rest = scenario.after
    held = (
        [scenario.anchor, scenario.anchor, *scenario.after]
        if shape == "O,O,rest"
        else [scenario.anchor, first, scenario.anchor, *rest]
    )
    _device(toolchain, recorded, scenario, window=framed(held))

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("2 times",), status=1)
    assert payload["partial"]["statistics"]["total_lines"] == 0


def test_a_record_logd_pruned_before_delivering_it_is_not_claimed(toolchain, recorded):
    """The contract, pinned: a capture is what logd delivered, never a certificate.

    logd can send the opening record, prune unread records of the same buffer
    while the dump is flushing to a slow reader, and finish normally (Android
    13's SerializedLogBuffer prunes while flushing; slow readers lose entries
    without being disconnected) -- a property of every logd reader, streams
    included, on every level. Codex's construction: the dump ``O, B`` with
    ``A`` pruned. It is accepted and reports exactly ``B``: no reader can tell
    a pruned record from one never logged, so none is claimed either way.
    Derived: the recorded window with its first record after O deleted.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    delivered = scenario.after[1:]
    _device(toolchain, recorded, scenario, window=framed([scenario.anchor, *delivered]))

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(delivered)


def test_3a_a_record_stamped_in_the_open_records_nanosecond_is_captured(toolchain, recorded):
    """(3) Passes. The dump asks from one nanosecond before the opening record's time.

    Derived: the first record after the opening one, restamped to its exact
    nanosecond. It used to be left out by a strictly-after filter.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    same = _restamped(scenario.after[0], *_stamp(scenario.anchor))
    window = framed([scenario.anchor, same, *scenario.after[1:]])
    _device(toolchain, recorded, scenario, window=window)

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown([same, *scenario.after[1:]])


@pytest.mark.parametrize("behind_ns", [1, 86_400 * 1_000_000_000], ids=["1ns", "a-day"])
def test_3b_a_record_its_writer_stamped_before_the_open_record_is_read(
    behind_ns, toolchain, recorded
):
    """(3) Passes now: the read follows logd's order, so a writer clock behind is no gap.

    Derived: the first record after the opening one, restamped ``behind_ns``
    before it. When the read was positioned by time, ``-t`` left such a
    record out (the documented gap 3b); a tail of logd's order holds it after
    the opening record, where logd took it.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    at = _ns(scenario.anchor) - behind_ns
    behind = _restamped(scenario.after[0], at // 1_000_000_000, at % 1_000_000_000)
    window = framed([scenario.anchor, behind, *scenario.after[1:]])
    _device(toolchain, recorded, scenario, window=window)

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown([behind, *scenario.after[1:]])


def test_3c_zero_lines_means_nothing_after_the_open_record_in_the_devices_dump(toolchain, recorded):
    """(3) Passes. No close record, so nothing can make the window end where it began.

    Zero lines now says what was read: logd delivered no record after the
    opening one. Derived: the window cut to its opening record.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(toolchain, recorded, scenario, window=scenario.anchor.raw)

    capture = _capture(toolchain)

    assert capture["statistics"]["total_lines"] == 0


def test_4_a_dump_without_the_open_record_is_refused(toolchain, recorded):
    """(4) Passes. The buffer turned over past the window's beginning: refused, not short.

    Derived: the recorded window without its opening record. With no
    stream, nothing else stands in for it: the frame is there byte for byte,
    or the read is refused (exit 1), with what was read under ``partial``.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(toolchain, recorded, scenario, window=framed(scenario.after))

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("no longer holds",), status=1)
    assert payload["partial"]["statistics"]["total_lines"] == 0
    assert "logcat -G" in payload["remedy"]


def test_4b_an_empty_buffer_at_the_open_gets_an_anchor_and_the_window_follows_it(
    toolchain, recorded
):
    """(4) The device held nothing when the read opened (as after ``--clear``).

    There is no record to open on, and apps' records alone must never place a
    read (``test_copies_of_one_app_datagram_cannot_pass_for_records_already_read``),
    so the read writes one marker line as shell -- ``adb shell log -t
    aes-log-monitor anchor-<128-bit hex>`` -- finds it by its nonce, and reads
    what follows it. Derived: the marks window after its opening record, behind
    the marker (a recorded shell record carrying the line the read wrote).
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    marker = _marker(scenario)
    newest_rule, opening = _opening([marker], logged=PLACEHOLDER)
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=(*opening, Rule(("shell", "log"))),
        dump=Rule(
            ("logcat", "-d", "-T"), buffered=framed([marker, *scenario.after]), logged=PLACEHOLDER
        ),
    )

    result = toolchain.run("log_monitor.py", "--serial", SERIAL, "--duration", "3s", "--json")

    assert result.returncode == 0, result.stderr
    capture = json.loads(result.stdout)
    assert capture["sample_logs"] == _shown(scenario.after)
    assert capture["anchor_written"] is True
    (written,) = _anchor_calls(toolchain)
    assert written[:6] == ["-s", SERIAL, "shell", "log", "-t", "aes-log-monitor"]
    assert len(written) == 7 and re.fullmatch(r"anchor-[0-9a-f]{32}", written[6]), written
    assert "wrote one marker line" in result.stderr


def test_5a_a_dump_that_overruns_its_budget_is_refused_within_it(toolchain, recorded):
    """(5) Passes. The one timer is the dump's watchdog, joined before its result is read.

    The device answers the opening question and then never finishes the
    window's dump. Its budget is 5 s plus the 1 s it covers: refused at exit
    1, with ``partial``, well inside the run's limit.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    hung = Rule(
        ("logcat", "-d", "-T"),
        stderr=recorded.text("adb_waiting_for_device"),
        hang=True,
    )
    _device(toolchain, recorded, scenario, dump=hung)

    result = toolchain.run("log_monitor.py", *LIVE, "--json", timeout=20)

    payload = assert_refusal(result, json_mode=True, naming=("did not finish within",), status=1)
    assert "partial" in payload


def test_5b_a_device_gone_by_the_window_dump_is_exit_three(toolchain, recorded):
    """(5) Passes. adb's recorded words for a device not there, on the dump: exit 3."""
    scenario = _scenario(API33, "logcat_stream_marks")
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "adb_device_not_found")
    gone = Rule(
        ("logcat", "-d", "-T"),
        stderr=recorded.text("adb_device_not_found"),
        exit=entry["exit_status"],
    )
    _device(toolchain, recorded, scenario, dump=gone)

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    assert_refusal(result, json_mode=True, naming=("not",), status=3)


def test_6_nothing_can_arrive_beside_the_window_read(toolchain, recorded):
    """(6) Impossible by construction: there is no stream to keep delivering.

    The calls an API 33 --duration makes: the level, the opening record, and
    one tail continuing from it -- never the stream ``logcat -B -T 1``, and no
    timestamp. The capture is that tail's records after the opening one, and
    nothing else.
    """
    scenario = _scenario(API33, "logcat_stream_block")
    _device(toolchain, recorded, scenario)
    _capture(toolchain)

    assert _logcat_calls(toolchain) == [
        ["-s", SERIAL, "logcat", "-d", "-B", "-T", "1"],
        ["-s", SERIAL, "logcat", "-d", "-B", "-T", "64"],
    ]
    assert not any("date" in call for call in toolchain.calls("adb"))


def test_7a_a_window_dump_that_is_not_records_is_refused_with_partial(toolchain, recorded):
    """(7) Passes. Bad framing is a refusal carrying what was read, never a traceback.

    Derived: under --follow the first poll reads the recorded window, the
    second answers it with its first record's header size zeroed. The refusal
    is the last line of stdout, and its ``partial`` holds the first poll's
    lines.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    broken = bytearray(scenario.window)
    struct.pack_into("<H", broken, 2, 0)
    _device(
        toolchain,
        recorded,
        scenario,
        extra=(Rule(("logcat", "-d", "-T"), stdout=scenario.window, once=True),),
        dump=Rule(("logcat", "-d", "-T"), stdout=bytes(broken)),
    )

    result = toolchain.run("log_monitor.py", "--serial", SERIAL, "--follow", "--json", timeout=30)

    payload = assert_refusal(
        result, json_mode=True, naming=("whole records",), status=1, streaming=True
    )
    assert payload["partial"]["sample_logs"] == _shown(scenario.after)


def test_7b_a_cut_window_dump_is_refused_after_the_device_is_asked(toolchain, recorded):
    """(7) Passes. A dump that ends inside a record: the device is asked, then refused.

    Derived: the recorded window and the first 25 bytes of its last record
    again (the fake keeps a cut tail cut when it applies ``-t``).
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    cut = scenario.window + frames(scenario.window)[-1].raw[:25]
    _device(toolchain, recorded, scenario, window=cut)

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("whole records",), status=1)
    assert "partial" in payload
    assert sum(1 for call in toolchain.calls("adb") if "get-state" in call) >= 2


# --- bounds ---------------------------------------------------------------------


def test_a_window_dump_past_the_byte_cap_is_refused(toolchain, recorded):
    """The dump is read up to ``ANDROID_EMU_LOG_DUMP_CAP_BYTES`` and refused past it."""
    scenario = _scenario(API33, "logcat_stream_block")
    assert len(scenario.window) > 1000
    _device(toolchain, recorded, scenario)

    result = toolchain.run("log_monitor.py", *LIVE, "--json", ANDROID_EMU_LOG_DUMP_CAP_BYTES="1000")

    assert_refusal(result, json_mode=True, naming=("more than 1000 bytes",), status=1)


def test_a_device_whose_logcat_never_answers_is_refused_within_the_duration(toolchain, recorded):
    """No dump at all: not an empty capture, and not a read that outlives its bound.

    Every ``logcat`` prints adb_waiting_for_device's banner and never ends.
    The opening question gets no longer than the --duration: refused at exit 1
    inside it.
    """
    toolchain.install(
        "adb",
        [
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=API33.text("getprop_sdk")),
            Rule(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
        ],
    )

    result = toolchain.run(
        "log_monitor.py", "--serial", SERIAL, "--duration", "2s", "--json", timeout=6
    )

    assert_refusal(result, json_mode=True, naming=("did not finish within 2s",), status=1)


# --- the cursor read itself, as the ANR readers will call it -------------------


def test_read_after_continues_from_its_cursor_and_hands_back_the_next():
    """``common.logcat.read_after`` over a recorded window: the records after, the last as cursor."""
    scenario = _scenario(API33, "logcat_stream_marks")
    asked: list[list[str]] = []

    def dump(args: list[str]) -> bytes:
        asked.append(args)
        return scenario.window

    cursor = logcat.cursor_from(scenario.opened)
    records, after = logcat.read_after(cursor, dump)

    assert records == logcat.parse_records(framed(scenario.after))
    assert after is not None and after.raw == scenario.after[-1].raw
    assert asked == [["-d", "-B", "-T", "64"]]


def test_read_after_refuses_a_dump_without_its_cursor():
    scenario = _scenario(API33, "logcat_stream_marks")
    cursor = logcat.cursor_from(scenario.opened)

    with pytest.raises(logcat.CursorLostError):
        logcat.read_after(cursor, lambda _args: framed(scenario.after))


# --- an unknown level is refused, never guessed to stream -----------------------


@pytest.mark.parametrize("answer", ["unset", "failed"])
def test_a_device_that_does_not_say_its_level_is_refused(answer, toolchain, recorded):
    """No API level, no guess: refused at exit 1, with ``partial``.

    Guessed to stream, the API 33 device here -- its stream recorded giving no
    byte -- was read as the quiet zero-line success again (Codex). ``unset``:
    getprop's recorded answer for a property a device lacks, an empty line at
    exit 0 (getprop_unset). ``failed``: no answer at all, exit 1 (the fake's
    silence for a question it has no rule for).
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    sdk = Rule(
        ("shell", "getprop", "ro.build.version.sdk"),
        stdout=API33.text("getprop_unset") if answer == "unset" else "",
        exit=0 if answer == "unset" else 1,
    )
    _device(toolchain, recorded, scenario, sdk=sdk)

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("getprop",), status=1)
    assert "partial" in payload
    assert "booting" in payload["remedy"]
    assert _logcat_calls(toolchain) == []


def test_the_opening_question_s_refusal_carries_partial_too(toolchain, recorded):
    """Every refusal of the dump-made read carries ``partial`` -- empty counts before any read."""
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=Rule(("logcat", "-d", "-T", "1"), exit=1),
    )

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("exited with status 1",), status=1)
    assert payload["partial"]["statistics"]["total_lines"] == 0


def test_a_dump_that_closes_its_output_but_lingers_is_killed_before_the_refusal(
    monkeypatch, recorded
):
    """A real child that closes stdout and keeps running is stopped, reaped, then refused.

    It ran in a session of its own and outlived the CLI that refused it: the
    wait for its exit timed out and the refusal went out with the child still
    sleeping (Codex: ``os.close(1); time.sleep(30)``). Now a dump is done when
    it EXITS, not when its output closes: at its deadline it is killed and
    reaped, bounded, before the refusal, which says so with the reaped status;
    the spec checks the process is gone.
    """
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    lingering = [sys.executable, "-c", "import os, time; os.close(1); time.sleep(30)", "logcat"]
    monkeypatch.setattr(log_monitor, "build_adb_command", lambda *_a: lingering)
    monkeypatch.setattr(log_monitor.subprocess, "Popen", popen)
    # The device is asked once the dump is stopped: answered as recorded, attached.
    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: _device_answer(cmd, recorded),
    )

    with pytest.raises(cli.Failure) as refused:
        log_monitor.LogMonitor(device_serial=SERIAL)._dump(["-d", "-B"], 1)

    (child,) = started
    try:
        assert "did not finish within 1s and was killed (exit" in str(refused.value)
        assert refused.value.details["partial"]["statistics"]["total_lines"] == 0
        assert child.poll() is not None, "the lingering dump outlived its refusal"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_a_getprop_that_times_out_is_refused_with_partial(toolchain, recorded):
    """The level question overrunning its bound: a refusal like every other, with ``partial``.

    It exited 1 without the refusal's ``partial`` (or the text form's
    disclosure). getprop answers nothing and never ends; the bound is
    shortened to 1 s (``ANDROID_EMU_LOG_STATE_TIMEOUT``); the device is asked
    again (``get-state``: still there) and the read is refused.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    sdk = Rule(
        ("shell", "getprop", "ro.build.version.sdk"),
        stderr=recorded.text("adb_waiting_for_device"),
        hang=True,
    )
    _device(toolchain, recorded, scenario, sdk=sdk)

    result = toolchain.run(
        "log_monitor.py", *LIVE, "--json", ANDROID_EMU_LOG_STATE_TIMEOUT="1", timeout=20
    )

    payload = assert_refusal(result, json_mode=True, naming=("did not finish within",), status=1)
    assert payload["partial"]["statistics"]["total_lines"] == 0
    assert _logcat_calls(toolchain) == []


# --- a dump is accepted only once the device answers ----------------------------


def _gone_after_the_first_ask(recorded) -> tuple[Rule, ...]:
    """``get-state``: attached once (the read's opening check), then recorded as not found."""
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "adb_device_not_found")
    return (
        Rule(("get-state",), stdout=recorded.text("adb_get_state"), once=True),
        Rule(
            ("get-state",),
            stderr=recorded.text("adb_device_not_found"),
            exit=entry["exit_status"],
        ),
    )


def _exactly_4096_bytes(anchor: Frame, record: Frame) -> bytes:
    """The anchor and one more record, its message padded so the two make 4096 bytes.

    Codex's shape: older adb exits 0 when the transport goes, with what had
    arrived -- here one whole 4 KiB block, as logcat writes them on API 33.
    Derived: the record's message replaced by padding (re-framed, as logd frames
    one holding that text).
    """
    padded = record.replace(message="x")
    padded = record.replace(message="x" * (1 + 4096 - len(anchor.raw) - len(padded.raw)))
    data = anchor.raw + padded.raw
    assert len(data) == 4096
    return data


@pytest.mark.parametrize("answer", ["a-4096-byte-block", "nothing"])
def test_a_dump_whose_device_went_is_exit_three_not_a_capture(answer, toolchain, recorded):
    """An exit-0 dump is not accepted until ``get-state`` answers: gone is exit 3, with ``partial``.

    The dump exits 0 with one 4 KiB block (the anchor and one record) or with
    nothing, and the device is then gone. That was a capture of one record,
    or of zero lines, at exit 0 -- or, with nothing, a "turned over" refusal.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    window = (
        _exactly_4096_bytes(scenario.anchor, scenario.after[0])
        if answer == "a-4096-byte-block"
        else b""
    )
    _device(
        toolchain,
        recorded,
        scenario,
        window=window,
        state=_gone_after_the_first_ask(recorded),
    )

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("not attached",), status=3)
    assert payload["partial"]["statistics"]["total_lines"] == 0


# --- portable: no select ------------------------------------------------------------


def test_a_dump_needs_no_select(monkeypatch, recorded):
    """``select`` takes no pipe on Windows; the dump does not use it at all.

    A real child writes the recorded window and exits 0; with ``select.select``
    made to raise, ``_dump`` still returns its bytes, once the device answers.
    """
    import select

    def no_select(*_args, **_kwargs):
        raise OSError("select is not available for pipes here")

    monkeypatch.setattr(select, "select", no_select)
    window = API33.data("logcat_stream_marks_window_records.bin")
    writer = ("import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())",)
    path = RECORDED_ROOT / "emulator-api33" / "logcat_stream_marks_window_records.bin"
    command = [sys.executable, "-c", *writer, str(path), "logcat"]
    monkeypatch.setattr(log_monitor, "build_adb_command", lambda *_a: command)
    monkeypatch.setattr(
        adb_exec.subprocess,
        "run",
        lambda cmd, **_k: _device_answer(cmd, recorded),
    )

    assert log_monitor.LogMonitor(device_serial=SERIAL)._dump(["-d", "-B"], 10) == window


# --- the level before --clear -----------------------------------------------------------


def test_an_unknown_level_is_refused_before_clear_erases_the_log(toolchain, recorded):
    """``--clear`` with a level the device does not say: refused before ``logcat -c``.

    The log was erased (``get-state``, ``logcat -c``) and then the read
    refused. Now the level is asked before anything touches the log.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    sdk = Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=API33.text("getprop_unset"))
    _device(toolchain, recorded, scenario, sdk=sdk)

    result = toolchain.run("log_monitor.py", *LIVE, "--clear", "--json")

    assert_refusal(result, json_mode=True, naming=("getprop",), status=1)
    assert not any("-c" in call for call in _logcat_calls(toolchain)), _logcat_calls(toolchain)


# --- every mode polls -----------------------------------------------------------------


def test_a_duration_polls_so_the_cursor_moves_before_the_anchor_turns_over(toolchain, recorded):
    """A window whose start is gone by its end is still read whole: the cursor moved on.

    Derived from the marks window: the first poll's dump begins at the
    anchor; the second's at the first poll's last record, the anchor already
    turned over; the final one's at the second's last record. One dump at the
    end -- what --duration took -- held no anchor, and was refused as lost.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    after = scenario.after
    assert len(after) >= 6
    first, second, final = after[:2], after[2:4], after[4:]
    _device(
        toolchain,
        recorded,
        scenario,
        extra=(
            Rule(("logcat", "-d", "-T"), stdout=framed([scenario.anchor, *first]), once=True),
            Rule(("logcat", "-d", "-T"), stdout=framed([first[-1], *second]), once=True),
        ),
        window=framed([second[-1], *final]),
    )

    result = toolchain.run("log_monitor.py", "--serial", SERIAL, "--duration", "3s", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _shown(after)


def test_a_three_second_duration_makes_three_dumps_one_poll_apart(toolchain, recorded):
    """``--duration 3s`` with the 1 s poll: dumps at 1 s and 2 s, and the final one at 3 s."""
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(toolchain, recorded, scenario)

    result = toolchain.run("log_monitor.py", "--serial", SERIAL, "--duration", "3s", "--json")

    assert result.returncode == 0, result.stderr
    dumps = [call for call in _logcat_calls(toolchain) if call[-1] != "1"]
    assert len(dumps) == 3, dumps


def test_follow_s_last_read_at_ctrl_c_prints_what_only_it_could(toolchain, recorded):
    """The read at Ctrl-C is a read: with no poll due before it, it prints the window.

    The poll interval is 30 s and the interrupt comes at 3 s, so the only
    window dump is the one Ctrl-C makes. Deleting it passed before (the
    earlier polls printed everything).
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(toolchain, recorded, scenario)

    result = toolchain.run(
        "log_monitor.py",
        "--serial",
        SERIAL,
        "--follow",
        interrupt_after=3,
        ANDROID_EMU_LOG_POLL_SECONDS="30",
    )

    assert result.returncode == 0, result.stderr
    assert [call for call in _logcat_calls(toolchain) if call[-1] != "1"], "no read at Ctrl-C"
    for line in _shown(scenario.after):
        assert result.stdout.count(line) == 1, line


# --- the dump's ending: one ordered sequence, every outcome -----------------------------
#
# Stop -> read bounded -> adb's own words -> get-state -> verdict, with no branch
# skipping a step and no exception leaving it (four rounds of review found a new
# interleaving in a tail made of branches). One matrix over how the dump ended,
# how the device answers afterwards, what adb said, and which file read fails.
# Each cell asserts the exit status and ``partial``; the exact steps that ran,
# in order (the kill, each read with its byte count, the classification, one
# ``get-state``); that the child is gone or the refusal says it could not be
# stopped; and that both files are closed. The child is a stand-in for Popen,
# scripted per outcome; the real-child specs above cover a real process.

_CAP = 4096
_BUDGET = 0.2
_SAID_TAIL = 64 * 1024
_OUTCOMES = (
    "exit0",  # exits 0 at once
    "exit0_late",  # would exit 0 after its deadline: killed first
    "exit0_seen_late",  # exits 0, but the wait reports it only after the deadline
    "nonzero",  # exits 1 at once
    "overrun",  # writes nothing, never exits
    "capped",  # floods past the cap, never exits
    "lingers",  # writes its window, never exits
    "unkillable",  # never exits; kill raises
    "reap_hangs",  # never exits; still running after the kill
    "wait_raises",  # the first wait raises OSError
    "interrupted",  # the first wait raises KeyboardInterrupt (Ctrl-C)
    "interrupted_unkillable",  # Ctrl-C, then the kill raises
)
_RECHECKS = ("answers", "gone", "times_out", "command_error")
_STDERRS = ("clean", "device_not_found")
_READS = ("ok", "stderr_raises", "stdout_raises")
_REAL_TEMPORARY_FILE = log_monitor.tempfile.TemporaryFile
_WINDOW = API33.data("logcat_stream_marks_window_records.bin")


class _CountedFile:
    """A real temporary file that logs each read (bytes, or that it failed) and its closing."""

    def __init__(self, name: str, events: list[str], fails: bool) -> None:
        self._file = _REAL_TEMPORARY_FILE()
        self.name, self.events, self.fails = name, events, fails
        self.closed = False

    def fileno(self) -> int:
        return self._file.fileno()

    def seek(self, *args) -> int:
        return self._file.seek(*args)

    def write(self, data: bytes) -> int:
        written = self._file.write(data)
        self._file.flush()
        return written

    def read(self, size: int = -1) -> bytes:
        if self.fails:
            self.events.append(f"read {self.name} raised")
            raise OSError(errno.EIO, "Input/output error")
        data = self._file.read(size)
        self.events.append(f"read {self.name} {len(data)}")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self._file.close()
        self.closed = True


class _ScriptedDump:
    """A dump child that ends as ``outcome`` says."""

    def __init__(self, outcome: str, out, said, said_bytes: bytes, events: list[str]):
        self.outcome, self.events = outcome, events
        now = time.monotonic()
        self.exit_at = {
            "exit0": now,
            "nonzero": now,
            "exit0_late": now + _BUDGET + 0.05,
        }.get(outcome)
        self.seen_at = now + _BUDGET + 0.02  # exit0_seen_late's report
        self.code = 1 if outcome == "nonzero" else 0
        self.killed = self.reported = False
        self.waits = 0
        self.pid = 4242
        if outcome == "capped":
            out.write(_WINDOW * (_CAP // len(_WINDOW) + 2))
        elif outcome != "overrun":
            out.write(_WINDOW)
        said.write(said_bytes)

    @property
    def alive(self) -> bool:
        if self.killed:
            return self.outcome == "reap_hangs"
        if self.outcome == "exit0_seen_late":
            return not self.reported
        return self.exit_at is None or time.monotonic() < self.exit_at

    def _status(self) -> int | None:
        if self.killed:
            return None if self.outcome == "reap_hangs" else -9
        return None if self.alive else self.code

    def poll(self) -> int | None:
        self.events.append("poll")
        return self._status()

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1 and self.outcome == "wait_raises":
            raise OSError(errno.ECHILD, "No child processes")
        if self.waits == 1 and self.outcome.startswith("interrupted"):
            raise KeyboardInterrupt
        if self.outcome == "exit0_seen_late" and not self.killed:
            time.sleep(max(0.0, self.seen_at - time.monotonic()))
            self.reported = True
            return 0
        status = self._status()
        if status is not None:
            return status
        if self.exit_at is not None and timeout is not None:
            left = self.exit_at - time.monotonic()
            if left <= timeout:
                time.sleep(max(0.0, left))
                return self.code
        time.sleep(timeout or 0)
        raise subprocess.TimeoutExpired("adb logcat", timeout)

    def kill(self) -> None:
        self.events.append("kill")
        if self.outcome in ("unkillable", "interrupted_unkillable"):
            raise OSError(errno.EPERM, "Operation not permitted")
        self.killed = True


def _recheck(recorded, answer: str, events: list[str]):
    """``run_adb`` as ``get-state`` answers after the dump."""

    def run(*_args, **_kwargs):
        events.append("recheck")
        if answer == "gone":
            raise adb_exec.DeviceNotFoundError(
                f"Device {SERIAL!r} is not attached.", remedy="Run `adb devices`."
            )
        if answer == "times_out":
            raise adb_exec.AdbTimeoutError("`adb get-state` did not finish within 15s.")
        if answer == "command_error":
            raise adb_exec.AdbCommandError("`adb get-state` exited 1: no output")
        return adb_exec.AdbResult(0, recorded.text("adb_get_state"), "", [])

    return run


def _said(recorded, variant: str) -> bytes:
    """Over 64 KiB of stderr: adb's recorded banner repeated, ending in its device error or not."""
    banner = recorded.text("adb_waiting_for_device").encode()
    filler = banner * (2 * _SAID_TAIL // len(banner) + 1)
    if variant == "device_not_found":
        return filler + recorded.text("adb_device_not_found").encode()
    return filler


_ALIVE_AT_THE_WAIT_S_END = {
    "exit0_late",
    "overrun",
    "capped",
    "lingers",
    "unkillable",
    "reap_hangs",
    "wait_raises",
    "interrupted",
    "interrupted_unkillable",
}


def _expected(outcome: str, recheck: str, said: str, reads: str) -> tuple[int | None, list[str]]:
    """The exit status (0: accepted; None: Ctrl-C raised again) and the steps, in order."""
    steps = ["stop", "poll", *(["kill"] if outcome in _ALIVE_AT_THE_WAIT_S_END else [])]
    if outcome.startswith("interrupted"):
        return None, steps
    words = reads != "stderr_raises"
    steps.append(f"read said {_SAID_TAIL}" if words else "read said raised")
    if outcome == "exit0":
        steps.append(f"read out {len(_WINDOW)}" if reads != "stdout_raises" else "read out raised")
    if words:
        steps.append("classify")
        if said == "device_not_found":
            return 3, steps
    steps.append("recheck")
    if recheck == "gone":
        return 3, steps
    if recheck != "answers":
        return 1, steps
    return (0 if outcome == "exit0" and reads == "ok" else 1), steps


@pytest.mark.parametrize("reads", _READS)
@pytest.mark.parametrize("said", _STDERRS)
@pytest.mark.parametrize("recheck", _RECHECKS)
@pytest.mark.parametrize("outcome", _OUTCOMES)
def test_every_dump_ending_passes_the_same_steps(
    outcome, recheck, said, reads, monkeypatch, recorded, capsys
):
    events: list[str] = []
    files: list[_CountedFile] = []
    children: list[_ScriptedDump] = []

    def temporary_file():
        name = "out" if not files else "said"
        fails = (name, reads) in (("out", "stdout_raises"), ("said", "stderr_raises"))
        files.append(_CountedFile(name, events, fails))
        return files[-1]

    def popen(_command, **kwargs):
        children.append(
            _ScriptedDump(
                outcome, kwargs["stdout"], kwargs["stderr"], _said(recorded, said), events
            )
        )
        return children[-1]

    def classify(*args, **kwargs):
        events.append("classify")
        return real_classify(*args, **kwargs)

    def stop(process):
        events.append("stop")
        return real_stop(process)

    real_classify = log_monitor.classify_device_error
    real_stop = log_monitor.LogMonitor._stop
    monkeypatch.setattr(log_monitor.LogMonitor, "_stop", staticmethod(stop))
    monkeypatch.setattr(log_monitor.tempfile, "TemporaryFile", temporary_file)
    monkeypatch.setattr(log_monitor.subprocess, "Popen", popen)
    monkeypatch.setattr(log_monitor, "classify_device_error", classify)
    monkeypatch.setattr(log_monitor, "DUMP_CAP_BYTES", _CAP)
    monkeypatch.setattr(log_monitor, "KILL_REAP_TIMEOUT", 0.1)
    monkeypatch.setattr(log_monitor, "run_adb", _recheck(recorded, recheck, events))
    monkeypatch.setattr(adb_exec, "_attached_serials", lambda: ["emulator-5554"])
    expected, steps = _expected(outcome, recheck, said, reads)

    answer = refused = interrupted = None
    try:
        answer = log_monitor.LogMonitor(device_serial=SERIAL)._dump(["-d", "-B"], _BUDGET)
    except cli.Failure as error:
        refused = error
    except KeyboardInterrupt:
        interrupted = True

    (child,) = children
    assert events == steps, f"steps {events}, not {steps}"
    assert all(file.closed for file in files), "a temporary file was left open"
    if expected is None:
        assert interrupted, "Ctrl-C was not raised again"
        if outcome == "interrupted_unkillable":
            said_on_stderr = capsys.readouterr().err
            assert f"could not be stopped (pid {child.pid}:" in said_on_stderr, said_on_stderr
    elif expected == 0:
        assert refused is None, f"refused: {refused}"
        assert answer == _WINDOW
    else:
        assert refused is not None, "accepted"
        status = cli.exit_code_for(refused)
        assert status == expected, f"exit {status}, not {expected}: {refused}"
        assert "partial" in refused.details, f"no partial: {refused}"
    if outcome == "exit0" and reads == "stdout_raises":
        assert "output could not be read" in str(refused), f"stdout EIO not said: {refused}"
    if outcome in ("unkillable", "reap_hangs"):
        assert refused is not None and "could not be stopped" in str(
            refused
        ), f"unstoppable not said: {refused}"
    elif outcome != "interrupted_unkillable":
        assert not child.alive, "the dump outlived its ending"


# --- logd's order positions the read; timestamps do not ---------------------------------
#
# A record's time is its writer's (a native app can send any `realtime` to
# /dev/socket/logdw), on a clock that can step. The read used to ask
# `-t <time>` -- logd filtering by those stamps -- and four rounds of review
# found a stamp or a clock step that moved it past real records each time. It
# now finds the cursor in a count-based tail of logd's order, so no stamp
# positions anything. Derived from the marks window and, for a burst, the
# recorded restart window (168 real records): records restamped (``_restamped``),
# or the windows cut whole.

_DAY_NS = 86_400 * 1_000_000_000


def _ns(frame: Frame) -> int:
    sec, nsec = _stamp(frame)
    return sec * 1_000_000_000 + nsec


def _shifted(frame: Frame, by_ns: int) -> Frame:
    """``frame`` as a writer whose clock was ``by_ns`` off would have stamped it."""
    ns = _ns(frame) + by_ns
    return _restamped(frame, ns // 1_000_000_000, ns % 1_000_000_000)


def test_a_future_stamped_record_cannot_move_the_read_past_real_ones(toolchain, recorded):
    """A poll whose last record is stamped a day ahead; the next poll still reads what follows.

    Poll 1 ends on the forged record. Asked from that record's stamp, every
    real record after it was filtered out -- a zero-line success for the rest
    of the read. Found in a tail of logd's order, it is just a record.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    a = scenario.after
    forged = _shifted(a[2], _DAY_NS)
    held = [scenario.anchor, a[0], a[1], forged, *a[3:]]
    _device(
        toolchain,
        recorded,
        scenario,
        extra=(Rule(("logcat", "-d", "-T"), stdout=framed(held[:4]), once=True),),
        window=framed(held),
    )

    result = toolchain.run("log_monitor.py", "--serial", SERIAL, "--duration", "2s", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sample_logs"] == _shown([a[0], a[1], forged, *a[3:]])


def test_a_future_stamped_opening_anchor_does_not_hide_the_window(toolchain, recorded):
    """The newest record when the read opened was stamped a day ahead: the window is still read."""
    scenario = _scenario(API33, "logcat_stream_marks")
    anchor = _shifted(scenario.anchor, _DAY_NS)
    _device(
        toolchain,
        recorded,
        scenario,
        newest=anchor.raw,
        window=framed([anchor, *scenario.after]),
    )

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(scenario.after)


@pytest.mark.parametrize(
    "case",
    ["step-back-during-the-opening-call", "correction-masked-by-a-late-recheck", "host-suspend"],
)
def test_a_device_clock_step_cannot_hide_records(case, toolchain, recorded):
    """Codex's three clock-step sequences, which hid records from the time-positioned read.

    - A backward step during the opening call: every record after the anchor
      is stamped 10 s before it.
    - A correction masked by a late recheck: the clock steps back 3 s; one
      record lands at T - 2.7 s among records stamped normally.
    - The host sleeps while the phone corrects its clock by an hour: the
      records after the anchor are stamped an hour earlier.

    The read now follows logd's order: whatever the stamps, the records after
    the anchor are the window, and the device's clock is never asked.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    a = scenario.after
    if case == "step-back-during-the-opening-call":
        after = [_shifted(f, -10_000_000_000) for f in a]
    elif case == "correction-masked-by-a-late-recheck":
        t = _ns(scenario.anchor)
        masked = _restamped(
            a[3], (t - 2_700_000_000) // 1_000_000_000, (t - 2_700_000_000) % 1_000_000_000
        )
        after = [*a[:3], masked, *a[4:]]
    else:
        after = [_shifted(f, -3_600_000_000_000) for f in a]
    _device(toolchain, recorded, scenario, window=framed([scenario.anchor, *after]))

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(after)
    assert not any("date" in call for call in toolchain.calls("adb"))


# --- the tail search: doubled until it reaches the cursor ------------------------------


def _restart(recorded) -> list[Frame]:
    """The recorded restart window from its second record, a system one (the first is an app's).

    A read opens on the newest system record, so the window's first record is
    the cursor here only when a system process wrote it.
    """
    held = frames(recorded.data(f"{RECORDS}.bin"))
    assert held[0].uid % 100_000 >= 10_000 > held[1].uid % 100_000
    return held[1:]


def _tails(toolchain: FakeToolchain) -> list[str]:
    """The ``-T N`` of each poll's dump (the opening question's ``-T 1`` left out)."""
    return [call[-1] for call in _logcat_calls(toolchain) if call[-1] != "1"]


def test_a_cursor_beyond_the_first_tail_is_found_in_twice_as_many(toolchain, recorded):
    """100 records after the anchor: the tail of 64 misses it; the tail of 128 holds it, short."""
    window = _restart(recorded)[:101]
    scenario = Scenario(stream=b"", opened=window[0].raw, window=framed(window), entry={})
    _device(toolchain, recorded, scenario)

    capture = _capture(toolchain, ANDROID_EMU_LOG_TAIL="1000")

    assert capture["sample_logs"] == _shown(window[1:])
    assert _tails(toolchain) == ["64", "128"]


def test_a_burst_of_more_than_a_tail_between_polls_is_read_whole(toolchain, recorded):
    """166 records after the anchor -- a burst past 64 and 128 -- read whole at 256."""
    window = _restart(recorded)
    assert len(window) > 128 + 1
    scenario = Scenario(stream=b"", opened=window[0].raw, window=framed(window), entry={})
    _device(toolchain, recorded, scenario)

    capture = _capture(toolchain, ANDROID_EMU_LOG_TAIL="1000")

    assert capture["sample_logs"] == _shown(window[1:])
    assert _tails(toolchain) == ["64", "128", "256"]


def test_a_cursor_absent_from_the_whole_buffer_is_lost(toolchain, recorded):
    """A tail that comes back short is the whole buffer: the cursor not in it, the window turned over.

    Derived: the restart window without its first record, the anchor; asked for
    64, it answers 64 -- doubled -- then 128 and 166: short, and no anchor.
    """
    window = _restart(recorded)
    scenario = Scenario(stream=b"", opened=window[0].raw, window=framed(window[1:]), entry={})
    _device(toolchain, recorded, scenario)

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    assert_refusal(result, json_mode=True, naming=("no longer holds",), status=1)
    assert _tails(toolchain) == ["64", "128", "256"]


def test_a_tail_that_would_pass_the_byte_cap_is_refused_before_it_is_asked(toolchain, recorded):
    """The cursor is past the tail of 64, and 128 would pass the cap: refused, and 128 never asked."""
    window = _restart(recorded)
    scenario = Scenario(stream=b"", opened=window[0].raw, window=framed(window), entry={})
    cap = len(framed(window[-64:])) + 1
    _device(toolchain, recorded, scenario)

    result = toolchain.run(
        "log_monitor.py", *LIVE, "--json", ANDROID_EMU_LOG_DUMP_CAP_BYTES=str(cap)
    )

    payload = assert_refusal(result, json_mode=True, naming=("would pass",), status=1)
    assert "partial" in payload
    assert _tails(toolchain) == ["64"]


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"), reason="no signal masks on this platform"
)
def test_a_second_ctrl_c_during_the_cleanup_does_not_swallow_the_diagnostic(
    monkeypatch, recorded, capsys
):
    """Ctrl-C, the kill fails, and a second Ctrl-C is pending: the diagnostic is still printed.

    The second SIGINT is held while the child is stopped; it was delivered
    when the mask was restored -- before the "could not be stopped" line was
    printed, which then never was. The line now goes out first.
    """
    events: list[str] = []

    def popen(_command, **kwargs):
        child = _ScriptedDump(
            "interrupted_unkillable", kwargs["stdout"], kwargs["stderr"], b"", events
        )
        real_kill = child.kill

        def kill() -> None:
            os.kill(os.getpid(), signal.SIGINT)  # the second Ctrl-C, held by the mask
            real_kill()

        child.kill = kill
        return child

    monkeypatch.setattr(log_monitor.subprocess, "Popen", popen)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            log_monitor.LogMonitor(device_serial=SERIAL)._dump(["-d", "-B"], _BUDGET)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert "could not be stopped (pid 4242:" in capsys.readouterr().err


# --- the cursor is a record a system process wrote: an identity no app can forge ------
#
# A frame's time, tid and payload are its writer's (AOSP logd LogListener), so
# an app can send the same datagram twice and logd stores two byte-identical
# frames. With the newest frame of any writer as the cursor, a resend of it
# after 100 records was the only copy a tail of 64 held: found once, read from
# there, and the 100 records between were never read (Codex, round 11). logd
# sets a record's uid from the writing socket's credentials, so no app can
# write under a system uid: the cursor is now the newest record whose uid is
# below AID_APP_START in its user, with the frames that followed it (its
# suffix, already read) checked byte for byte after it. App frames are
# recorded ones (logcat_strictmode_records on API 33, uid 10174); the system
# anchor is the recorded marks window's opening record.


def _app_frames() -> list[Frame]:
    """API 33's recorded StrictMode records an app wrote (uid 10174), in order."""
    held = frames(API33.data("logcat_strictmode_records.bin"))
    return [frame for frame in held if frame.uid % 100_000 >= 10_000]


def _with_uid(frame: Frame, uid: int) -> Frame:
    """``frame`` as logd would have stored it from a writer whose socket said ``uid``."""
    raw = bytearray(frame.raw)
    struct.pack_into("<I", raw, 24, uid)
    return Frame(bytes(raw))


def _opening(
    open_buffer: list[Frame], *counts: int, logged: str = ""
) -> tuple[Rule, tuple[Rule, ...]]:
    """The opening's ``-T 1`` rule and its doublings, each the last N of ``open_buffer``.

    ``logged``: the placeholder of a marker frame in it (``Rule.logged``).
    """
    data = framed(open_buffer)
    first, *rest = (
        Rule(("logcat", "-d", "-T", str(n)), buffered=data, logged=logged) for n in (1, *counts)
    )
    return first, tuple(rest)


def _all_tails(toolchain: FakeToolchain) -> list[str]:
    """The ``-T N`` of every dump the read asked, the opening's included."""
    return [call[call.index("-T") + 1] for call in _logcat_calls(toolchain) if "-T" in call]


@pytest.mark.parametrize(
    ("uid", "system"),
    [
        (0, True),  # root
        (1000, True),  # system_server
        (2000, True),  # shell: `adb shell log`, as the recorded marks were written
        (9999, True),
        (10_000, False),  # AID_APP_START
        (10_174, False),  # the recorded app
        (99_999, False),  # isolated
        (101_000, True),  # system, user 1
        (1_010_174, False),  # the recorded app, user 10
    ],
)
def test_a_system_writer_is_a_uid_below_the_first_app_uid_of_its_user(uid, system):
    """``uid % 100000 < 10000``: logd's uid, from the socket's credentials, never the writer's say."""
    frame = _with_uid(_app_frames()[0], uid)

    (record,) = logcat.parse_records(frame.raw)

    assert logcat.system_written(record) is system


def test_a_resent_app_record_cannot_hide_what_followed_it(toolchain, recorded):
    """Codex's replay: O, 100 records, a byte-identical resend of O -- all 100 are read.

    The device's newest record at the open is O, an app's. A tail of 64 holds
    only the resend; taken as the cursor, it was "found" once and the 100
    records before it were never read. The cursor is now S, the system record
    before O, with O as what followed it: S is in the tail of 128, O right
    after it, and everything after O is the read's -- the resend included,
    which logd did take.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    anchor, o = scenario.anchor, _app_frames()[0]
    between = _restart(recorded)[:100]
    newest_rule, opening = _opening([anchor, o], 2)
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=opening,
        window=framed([anchor, o, *between, o]),
    )

    capture = _capture(toolchain, ANDROID_EMU_LOG_TAIL="1000")

    assert capture["sample_logs"] == _shown([*between, o])
    assert _all_tails(toolchain) == ["1", "2", "64", "128"]


def test_an_app_that_resends_everything_after_the_cursor_is_still_read(toolchain, recorded):
    """The app replays every record after the system cursor: the copies are new records, read.

    Before, the cursor was the app's last record, and the dump held it twice:
    a refusal. The cursor is the system record before them, and the records
    after it are checked against what was read, then the rest taken.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    anchor, apps = scenario.anchor, _app_frames()[:3]
    newest_rule, opening = _opening([anchor, *apps], 2, 4)
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=opening,
        window=framed([anchor, *apps, *apps]),
    )

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(apps)
    assert _all_tails(toolchain) == ["1", "2", "4", "64"]


def test_records_only_apps_wrote_between_two_system_records_are_read_once(toolchain, recorded):
    """Polls that see app records only keep the system cursor; each record is read once.

    Poll 1 and 2 add app records and no system one: the cursor stays the
    opening record, what followed it grows. The last read brings a system
    record, which becomes the cursor.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    anchor, apps, later = scenario.anchor, _app_frames(), scenario.after
    first, second, third = apps[:8], apps[8:16], apps[16:]
    final = [anchor, *first, *second, *third, *later]
    _device(
        toolchain,
        recorded,
        scenario,
        extra=(
            Rule(("logcat", "-d", "-T"), buffered=framed([anchor, *first]), once=True),
            Rule(("logcat", "-d", "-T"), buffered=framed([anchor, *first, *second]), once=True),
        ),
        window=framed(final),
    )

    result = toolchain.run(
        "log_monitor.py",
        "--serial",
        SERIAL,
        "--duration",
        "3s",
        "--json",
        ANDROID_EMU_LOG_TAIL="1000",
    )

    assert result.returncode == 0, result.stderr
    shown = json.loads(result.stdout)["sample_logs"]
    assert shown == _shown(final[1:])


@pytest.mark.parametrize("change", ["pruned", "replaced"])
def test_a_record_read_after_the_cursor_that_is_not_there_any_more_is_lost(
    change, toolchain, recorded
):
    """The cursor is held, but a record read after it is gone (or different): refused, not resumed.

    Opening: S then the app's A1 -- the cursor S, A1 already read. The next
    dump holds S, then not A1: logd pruned it (main turns over apart from
    system), or something else sits there. Records logged after A1 may have
    gone with it, so the read is refused rather than reported from a guess.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    anchor, apps = scenario.anchor, _app_frames()
    newest_rule, opening = _opening([anchor, apps[0]], 2)
    held = (
        [anchor, *apps[1:4]]
        if change == "pruned"
        else [anchor, _last_byte(apps[0], ord("~")), *apps[1:4]]
    )
    _device(
        toolchain, recorded, scenario, newest_rule=newest_rule, extra=opening, window=framed(held)
    )

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("no longer holds",), status=1)
    assert payload["partial"]["statistics"]["total_lines"] == 0


def test_a_buffer_only_apps_wrote_at_the_open_gets_an_anchor_and_is_not_the_window(
    toolchain, recorded
):
    """No system record at the open, the whole buffer an app's: an anchor, and only what follows.

    The opening doubles until the tail comes back short, with no system
    record in it; the read writes its marker, finds it (a tail of 1), and the
    app's records before it are not the window.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    apps, later, marker = _app_frames()[:3], scenario.after, _marker(scenario)
    newest_rule, opening = _opening([*apps, marker], 2, 4, logged=PLACEHOLDER)
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=(*opening, Rule(("shell", "log"))),
        dump=Rule(
            ("logcat", "-d", "-T"), buffered=framed([*apps, marker, *later]), logged=PLACEHOLDER
        ),
    )

    capture = _capture(toolchain)

    assert capture["sample_logs"] == _shown(later)
    assert _all_tails(toolchain) == ["1", "2", "4", "1", "64"]
    assert len(_anchor_calls(toolchain)) == 1


# --- no system record to open on: the read writes one ------------------------------------
#
# Round 11 let a read with no system record continue from apps' bytes alone:
# the whole buffer at the open as a prefix, checked at the start of each later
# whole-buffer dump. After --clear, an app logging copies of one native
# datagram fills the buffer with identical frames; once they turn over, new
# copies reproduce that prefix exactly and were taken as already read -- zero
# records, exit 0, under the cap, with no system uid involved (Codex, round
# 12). Now such a read writes its own anchor as shell (uid 2000) with a 128-bit
# nonce, and no read continues from app-written bytes alone. The marker frame
# is a recorded shell record (the marks window's) carrying a placeholder, which
# the fake turns into the line the read wrote once it has written it
# (``Rule.logged``); the copies are one recorded app frame, repeated.

PLACEHOLDER = "the line log_monitor writes"


def _marker(scenario: Scenario) -> Frame:
    """A recorded shell record (uid 2000) standing for the marker the read will write."""
    shell = next(frame for frame in scenario.after if frame.uid == 2000)
    return shell.replace(tag="aes-log-monitor", message=PLACEHOLDER)


def _anchor_calls(toolchain: FakeToolchain) -> list[list[str]]:
    """Every `adb shell log` the read made, its argv after ``adb``."""
    return [call for call in toolchain.calls("adb") if "shell" in call and "log" in call]


@pytest.mark.parametrize("marker_kept", [True, False], ids=["marker-kept", "marker-pruned-too"])
def test_copies_of_one_app_datagram_cannot_pass_for_records_already_read(
    marker_kept, toolchain, recorded
):
    """Codex's construction: after --clear, four copies of one app datagram; they turn over;
    four new copies reproduce the buffer the read opened on.

    Round 11 took the four new copies for the four already seen: zero lines, exit
    0. Now the read wrote its anchor after the first four. Kept, the new copies
    follow it and are read; pruned with them, the anchor is gone and the read is
    refused. Never zero lines at exit 0, and the anchor written exactly once.
    """
    scenario = _scenario(API33, "logcat_stream_marks")
    copy, marker = _app_frames()[0], _marker(scenario)
    newest_rule, opening = _opening([copy] * 4 + [marker], 2, 4, 8, logged=PLACEHOLDER)
    after_turnover = ([marker] if marker_kept else []) + [copy] * 4
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=(*opening, Rule(("logcat", "-c")), Rule(("shell", "log"))),
        dump=Rule(("logcat", "-d", "-T"), buffered=framed(after_turnover), logged=PLACEHOLDER),
    )

    result = toolchain.run("log_monitor.py", *LIVE, "--clear", "--json")

    if marker_kept:
        assert result.returncode == 0, result.stderr
        capture = json.loads(result.stdout)
        assert capture["sample_logs"] == _shown([copy] * 4)
        assert capture["anchor_written"] is True
    else:
        payload = assert_refusal(result, json_mode=True, naming=("no longer holds",), status=1)
        assert payload["partial"]["anchor_written"] is True
    assert len(_anchor_calls(toolchain)) == 1
    assert _all_tails(toolchain)[:5] == ["1", "2", "4", "8", "1"]


def test_an_anchor_that_never_appears_is_refused(toolchain, recorded):
    """The marker was written but the whole buffer does not hold it: refused, not guessed."""
    scenario = _scenario(API33, "logcat_stream_marks")
    copies = [_app_frames()[0]] * 4
    newest_rule, opening = _opening(copies, 2, 4, 8)
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=(*opening, Rule(("shell", "log"))),
        window=framed(copies),
    )

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("marker",), status=1)
    assert payload["partial"]["statistics"]["total_lines"] == 0
    assert len(_anchor_calls(toolchain)) == 1
    assert _all_tails(toolchain) == ["1", "2", "4", "8", "1", "2", "4", "8"]


def test_an_anchor_that_cannot_be_written_is_refused(toolchain, recorded):
    """`adb shell log` failing: refused at exit 1, with ``partial`` -- the read never starts."""
    scenario = _scenario(API33, "logcat_stream_marks")
    _device(toolchain, recorded, scenario, newest=b"", extra=(Rule(("shell", "log"), exit=1),))

    result = toolchain.run("log_monitor.py", *LIVE, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("could not write",), status=1)
    assert "partial" in payload
    assert [call for call in _logcat_calls(toolchain) if "64" in call] == []


def test_the_anchor_is_said_in_the_text_summary_and_only_api_33_has_the_key(toolchain, recorded):
    """Written: a summary line. Not written: ``anchor_written: false``; API 35: no such key."""
    scenario = _scenario(API33, "logcat_stream_marks")
    marker = _marker(scenario)
    newest_rule, opening = _opening([marker], logged=PLACEHOLDER)
    _device(
        toolchain,
        recorded,
        scenario,
        newest_rule=newest_rule,
        extra=(*opening, Rule(("shell", "log"))),
        dump=Rule(
            ("logcat", "-d", "-T"), buffered=framed([marker, *scenario.after]), logged=PLACEHOLDER
        ),
    )
    text = toolchain.run("log_monitor.py", *LIVE)
    assert text.returncode == 0, text.stderr
    assert "Anchor written:" in text.stdout

    _device(toolchain, recorded, scenario)
    assert _capture(toolchain)["anchor_written"] is False

    prompt = _scenario(API35, "logcat_stream_quiet")
    _device(toolchain, recorded, prompt, level=API35)
    assert "anchor_written" not in _capture(toolchain)
