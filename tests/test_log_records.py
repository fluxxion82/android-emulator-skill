"""common.logcat's record reader, against logd's records as the emulator wrote them.

log_monitor and app_state_capture read ``logcat -B`` (review of v0.8.0, A1): in
threadtime text a tag an app chooses can hold a newline and a whole header,
so one record prints as two lines and the second one is anybody's. Records
are length-framed and carry the pid and uid logd took from the writing
socket's credentials (the tid and time are the writer's own header's).

Each binary fixture was recorded in the same run as the device's own
threadtime text for the same window (its MANIFEST ``text_twin``), so the
reader's printing is held against what the device printed -- in the zone the
device printed it in (MANIFEST ``timezone``), whatever zone this test runs in.
The reader is also the module the ANR readers are to adopt, so its refusals
are pinned here: a stream that is not records is refused, never guessed at.
"""

from __future__ import annotations

import io
import os
import threading
import time
from zoneinfo import ZoneInfo

import pytest
from fake_toolchain import framed, frames, recorded_zone

from common import logcat
from common.device_utils import parse_package_uids

PACKAGE = "com.example.composefixture"
PAIRS = (
    ("logcat_app_restart_records", "logcat_app_restart_usec"),
    ("logcat_tag_injection_records", "logcat_tag_injection"),
    ("logcat_crash_buffer_records", "logcat_crash_buffer"),
    ("logcat_crash_tail_records", "logcat_crash_tail"),
)


def _entry(recorded, name: str) -> dict:
    return next(e for e in recorded.manifest["fixtures"] if e["name"] == name)


def _text_lines(recorded, name: str) -> list[str]:
    """The device's threadtime lines, split at "\\n" only, separators dropped."""
    return [
        line
        for line in recorded.text(name).split("\n")
        if line and not line.startswith("--------- beginning of")
    ]


def _forged(recorded) -> set[str]:
    """The two physical lines the injection's one record printed as (its text twin)."""
    injected = _entry(recorded, "logcat_tag_injection_records")["injection"]
    lines = _text_lines(recorded, "logcat_tag_injection")
    at = next(i for i, line in enumerate(lines) if line.split()[2:3] == [injected["forger_pid"]])
    return {lines[at], lines[at + 1]}


@pytest.mark.parametrize(("binary", "text"), PAIRS, ids=[pair[1] for pair in PAIRS])
def test_records_print_as_the_device_printed_them(binary, text, recorded):
    """Every record of the app's processes prints exactly as the device printed it.

    The text was dumped first and the records right after, so the records may
    run on past the text's end: the app's lines must be a prefix. And every
    line the device printed is among the lines the records print -- except
    the injection's forged pair, which by design is one escaped line.
    """
    records = logcat.parse_records(recorded.data(f"{binary}.bin"))
    zone = ZoneInfo(recorded_zone(recorded, binary))
    pids = set(_entry(recorded, binary)["pids"])
    printed = [line for record in records if str(record.pid) in pids for line in record.lines(zone)]
    device = [line for line in _text_lines(recorded, text) if line.split()[2] in pids]
    assert device, f"{text} holds no line of the app"
    assert printed[: len(device)] == device

    everything = {line for record in records for line in record.lines(zone)}
    expected_missing = _forged(recorded) if binary == "logcat_tag_injection_records" else set()
    missing = {line for line in _text_lines(recorded, text) if line not in everything}
    assert missing == expected_missing


def test_a_multi_line_message_is_one_record(recorded):
    """`-T 1` over a crash buffer returned one record whose message is the whole trace."""
    (record,) = logcat.parse_records(recorded.data("logcat_crash_tail_records.bin"))
    assert record.tag == "AndroidRuntime" and record.priority == "E"
    assert len(record.lines()) == len(_text_lines(recorded, "logcat_crash_tail")) > 1


def test_the_writer_is_logds_word(recorded):
    """pid and uid come from logd: the app's records carry the package's uid.

    And the injection's forging record carries the shell's -- not
    system_server's pid, whatever its tag says.
    """
    entry = _entry(recorded, "logcat_app_restart_records")
    uids = parse_package_uids(entry["package_uid_listing"]["output"], PACKAGE)
    records = logcat.parse_records(recorded.data("logcat_app_restart_records.bin"))
    app = [record for record in records if str(record.pid) in entry["pids"]]
    assert app and {record.uid for record in app} <= uids

    injection = _entry(recorded, "logcat_tag_injection_records")
    forging = [
        record
        for record in logcat.parse_records(recorded.data("logcat_tag_injection_records.bin"))
        if str(record.pid) == injection["injection"]["forger_pid"]
    ]
    assert len(forging) == 1
    assert str(forging[0].pid) != injection["system_server_pid"]
    assert forging[0].uid not in uids
    assert "\n" in forging[0].tag


def test_a_stream_reads_record_by_record_to_its_clean_end(recorded):
    """``read_record`` over a pipe-like stream: the same records, then None."""
    data = recorded.data("logcat_crash_buffer_records.bin")
    stream = io.BytesIO(data)
    read = []
    while (record := logcat.read_record(stream)) is not None:
        read.append(record)
    assert read == logcat.parse_records(data) == list(logcat.iter_records(io.BytesIO(data)))
    assert logcat.read_record(stream) is None


def test_a_stream_that_ends_inside_a_record_is_refused_as_truncated(recorded):
    """Derived: the recorded crash buffer cut short inside its second record.

    A live read stopped at its deadline ends this way and loses only that
    record (log_monitor checks ``truncated``); a dump never does, and refuses.
    """
    data = recorded.data("logcat_crash_buffer_records.bin")
    first = len(frames(data)[0].raw)
    for cut in (first + 2, first + 10, len(data) - 1):
        with pytest.raises(logcat.LogFramingError) as dump:
            logcat.parse_records(data[:cut])
        assert dump.value.truncated and dump.value.offset == first
        stream = io.BytesIO(data[:cut])
        assert logcat.read_record(stream) is not None
        with pytest.raises(logcat.LogFramingError) as live:
            logcat.read_record(stream)
        assert live.value.truncated


@pytest.mark.parametrize("header_size", [0, 20, 27, 65, 0xFFFF])
def test_a_header_outside_logds_bounds_is_refused_not_guessed(header_size, recorded):
    """Derived: the recorded record with its header-size field substituted.

    A length-framed stream cannot resynchronise; a header logcat itself would
    refuse (smaller than ``struct logger_entry``) or an absurd one ends the
    read with an error that names the offset, before anything is decoded.
    """
    raw = bytearray(frames(recorded.data("logcat_crash_tail_records.bin"))[0].raw)
    raw[2:4] = header_size.to_bytes(2, "little")
    with pytest.raises(logcat.LogFramingError) as refused:
        logcat.parse_records(bytes(raw))
    assert not refused.value.truncated and refused.value.offset == 0
    assert logcat.LogFramingError.remedy
    with pytest.raises(logcat.LogFramingError):
        logcat.read_record(io.BytesIO(bytes(raw)))


def test_a_payload_longer_than_logd_writes_is_refused(recorded):
    """Derived: the payload-length field substituted past LOGGER_ENTRY_MAX_LEN."""
    raw = bytearray(frames(recorded.data("logcat_crash_tail_records.bin"))[0].raw)
    raw[0:2] = (logcat.MAX_ENTRY_PAYLOAD + 1).to_bytes(2, "little")
    with pytest.raises(logcat.LogFramingError):
        logcat.parse_records(bytes(raw) + bytes(logcat.MAX_ENTRY_PAYLOAD))


def _piecewise(data: bytes, chunk: int):
    """A real pipe, unbuffered on both ends, that receives ``data`` ``chunk`` bytes at a time."""
    read_fd, write_fd = os.pipe()

    def write() -> None:
        with os.fdopen(write_fd, "wb", buffering=0) as pipe:
            for at in range(0, len(data), chunk):
                pipe.write(data[at : at + chunk])
                time.sleep(0.0002)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    return os.fdopen(read_fd, "rb", buffering=0), writer


@pytest.mark.parametrize("chunk", [1, 2, 7])
def test_a_record_that_arrives_in_pieces_is_read_whole(chunk, recorded):
    """A pipe delivers what has arrived, not what was asked for (review of PR #35).

    An unbuffered pipe (``bufsize=0``, as anr_watcher opens its stream)
    handed a header over in two reads, and one short read was taken for the
    stream's end: ``truncated``. The recorded crash buffer, written 1, 2 and
    7 bytes at a time, reads back as its records.
    """
    data = recorded.data("logcat_crash_buffer_records.bin")
    stream, writer = _piecewise(data, chunk)
    with stream:
        read = []
        while (record := logcat.read_record(stream)) is not None:
            read.append(record)
    writer.join(timeout=30)
    assert read == logcat.parse_records(data)


def test_a_record_cut_off_in_pieces_is_still_truncated(recorded):
    """...and a stream that really ends inside a record still says so."""
    data = recorded.data("logcat_crash_buffer_records.bin")
    stream, writer = _piecewise(data[:-1], 3)
    with stream:
        assert logcat.read_record(stream) is not None
        with pytest.raises(logcat.LogFramingError) as cut:
            logcat.read_record(stream)
    writer.join(timeout=30)
    assert cut.value.truncated


def test_a_message_cannot_print_a_line_of_its_own(recorded):
    """A message's line-ending and terminal controls print escaped (review of PR #35).

    LF-only splitting kept ``\\r<an E header>`` inside its line here, but a
    saved log read back by an ordinary reader -- ``str.splitlines``, a
    text-mode file -- was two lines, the second a forged error. Derived: the
    first recorded I record of the app, its message given a carriage return
    and a recorded E line (header and all), then VT, FF, a file separator,
    NEL, U+2028, U+2029 and an ESC sequence. TAB is not among them: crash
    frames are indented with it, and print as the device printed them (above).
    """
    entry = _entry(recorded, "logcat_app_restart_records")
    window = frames(recorded.data("logcat_app_restart_records.bin"))
    info = next(
        (f for f in window if f.pid in entry["pids"] and f.priority == "I" and f.message), None
    )
    error = next(
        (
            line
            for line in _text_lines(recorded, "logcat_app_restart_usec")
            if line.split()[4:5] == ["E"]
        ),
        None,
    )
    if info is None or error is None:
        pytest.fail("the recording no longer holds an I record of the app and an E line")
    hostile = "\r" + error + "\x0b\x0c\x1c\x85\u2028\u2029\x1b[2J"
    (record,) = logcat.parse_records(info.replace(message=info.message + hostile).raw)
    (line,) = record.lines()
    assert line.splitlines() == [line]
    assert "\x1b" not in line and "\\r" in line and error in line
    assert logcat.parse_threadtime(line) == (info.pid, "I")


def test_a_tag_is_printed_with_its_control_characters_escaped(recorded):
    """The forging tag's newline prints as ``\\n``: the record stays one line."""
    injection = _entry(recorded, "logcat_tag_injection_records")["injection"]
    (forging,) = [
        record
        for record in logcat.parse_records(recorded.data("logcat_tag_injection_records.bin"))
        if str(record.pid) == injection["forger_pid"]
    ]
    (line,) = forging.lines()
    assert forging.tag.replace("\n", "\\n") in line
    assert logcat.parse_threadtime(line) == (injection["forger_pid"], "E")


def test_records_text_is_the_printed_lines_one_newline_each(recorded):
    records = logcat.parse_records(recorded.data("logcat_crash_buffer_records.bin"))
    zone = ZoneInfo(recorded_zone(recorded, "logcat_crash_buffer_records"))
    assert logcat.records_text(records, zone) == "".join(
        f"{line}\n" for record in records for line in record.lines(zone)
    )
    assert framed(frames(recorded.data("logcat_crash_buffer_records.bin"))) == recorded.data(
        "logcat_crash_buffer_records.bin"
    )


def test_a_binary_read_asks_for_records_and_sends_no_filterspec():
    """``-B`` in place of the ``-v`` terms; logd's own options kept.

    Measured on API 35: ``-B`` ignores filterspecs (``*:E`` still returned I
    and D records), so none is sent; ``-d``, ``-t``, ``-T`` and ``--pid`` are
    logd's and apply.
    """
    args = logcat.logcat_args(
        since="10-17 10:00:00.000",
        binary=True,
        usec=True,
        min_priority="E",
        tag_priorities=(("ActivityManager", "I"),),
        pid=42,
    )
    assert args == ["-d", "-t", "10-17 10:00:00.000", logcat.BINARY_FLAG, "--pid=42"]
    assert logcat.logcat_args(binary=True, tail=1) == [logcat.BINARY_FLAG, "-T", "1"]
    # Text reads are unchanged.
    assert logcat.logcat_args(min_priority="W") == ["-v", "threadtime", "*:W"]
