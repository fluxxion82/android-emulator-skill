"""Ground truth for sampling a hung app without root: `am_profile_hang.trace`.

`am profile start --sampling 1000 <pkg> /data/local/tmp/<file>` is the one way
to see what a debuggable app's main thread is doing while it hangs on a `user`
build -- no root, no simpleperf, no Studio. The recording is the fixture app
held in `Thread.sleep` for 40 s by `AnrReceiver`, sampled for 45 s, and it is
what the v0.9.0 hang sampler will be written against (vault note "Sampling a
Hung App Without Root").

**The production parser is v0.9.0's job, not this module's.** The small reader
below exists so the claims the plan makes about this file are checked against
its bytes now, before any code depends on them; it lives here, in the test
module, so nothing in `scripts/` grows a parser no feature calls yet.

The layout, as read from the file rather than assumed (ART method trace v3):

* A text header, UTF-8, one item per line: ``*version``, ``3``, then
  ``key=value`` lines (``data-file-overflow``, ``clock=dual``,
  ``elapsed-time-usec``, ``num-method-calls``, ``clock-call-overhead-nsec``,
  ``vm``, ``pid``); ``*threads`` with ``<tid>\\t<name>`` lines; ``*methods``
  with ``<id in hex>\\t<class>\\t<method>\\t<signature>\\t<source file>``
  lines; ``*end``.
* Directly after ``*end\\n``, a little-endian binary header: the magic ``SLOW``
  (u32 0x574f4c53), u16 version (3), u16 offset of the first record (32), u64
  start time, u16 record size (14), zero padding up to the offset.
* 14-byte records: u16 thread id; u32 method id whose low 2 bits are the action
  (0 enter, 1 exit, 2 unwind); then, because the header says ``clock=dual``,
  TWO u32 clocks -- the thread-CPU clock FIRST, the wall clock second, each
  relative to the start time.

The description in the brief matched, with one thing it left open made
explicit: which of the two clocks is which. Only the wall clock shows a hang.
Across the 40 s sleep the thread-CPU clock moves by well under a millisecond,
so a reader that took the first u32 as "the" clock would measure a hung main
thread as idle.

The header names no unit for the record clocks. The one unit it states is
``elapsed-time-usec``, and the wall clocks are consistent with it: the largest
is inside the elapsed time, and the sleep's dwell in those units is 40.0 s
against AnrReceiver's 40,000 ms. Microseconds is therefore what this module
divides by.

The records are TRANSITIONS between sampled stacks, not samples: a frame is
entered when it first appears in a sample and exited when it is gone. That is
why 45 s at one sample per millisecond is a few hundred records rather than
45,000, why the 40 s block is one enter and one exit with nothing between, and
why the stack main already had when profiling began appears as a burst of
enters at the first sample.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from itertools import pairwise

import pytest

TRACE = "am_profile_hang.trace"
END_MARKER = b"\n*end\n"
MAGIC = b"SLOW"
ENTER, EXIT, UNWIND = 0, 1, 2
US_PER_S = 1_000_000

ANR_RECEIVER = ("com.example.composefixture.AnrReceiver", "onReceive")
THREAD_SLEEP = ("java.lang.Thread", "sleep")


@dataclass(frozen=True)
class Record:
    thread: int
    method: int
    action: int
    thread_clock_us: int
    wall_clock_us: int


@dataclass(frozen=True)
class Trace:
    header_lines: list[str]
    header: dict[str, str]
    threads: dict[int, str]
    methods: dict[int, tuple[str, str, str, str]]
    version: int
    data_offset: int
    record_size: int
    body_bytes: int
    records: list[Record]


def _parse_trace(data: bytes) -> Trace:
    """Read an ART v3 dual-clock method trace. Test-only; see the module docstring."""
    end = data.index(END_MARKER)
    lines = data[:end].decode("utf-8").splitlines() + ["*end"]

    header: dict[str, str] = {}
    threads: dict[int, str] = {}
    methods: dict[int, tuple[str, str, str, str]] = {}
    section = ""
    for line in lines:
        if line.startswith("*"):
            section = line
        elif section == "*version" and "=" in line:
            key, _, value = line.partition("=")
            header[key] = value
        elif section == "*threads":
            tid, _, name = line.partition("\t")
            threads[int(tid)] = name
        elif section == "*methods":
            method_id, cls, name, signature, source = line.split("\t")
            methods[int(method_id, 16)] = (cls, name, signature, source)

    body = data[end + len(END_MARKER) :]
    magic, version, offset, _start, record_size = struct.unpack_from("<4sHHQH", body, 0)
    assert magic == MAGIC, f"no SLOW magic after *end: {magic!r}"
    assert header.get("clock") == "dual", "this reader only knows the dual-clock record"

    records = []
    for at in range(offset, len(body) - record_size + 1, record_size):
        thread, value, thread_clock, wall_clock = struct.unpack_from("<HIII", body, at)
        records.append(Record(thread, value & ~0b11, value & 0b11, thread_clock, wall_clock))

    return Trace(
        header_lines=lines,
        header=header,
        threads=threads,
        methods=methods,
        version=version,
        data_offset=offset,
        record_size=record_size,
        body_bytes=len(body),
        records=records,
    )


@dataclass(frozen=True)
class Frame:
    method: int
    enter: Record
    position: int  # index of ``enter`` in Trace.records; records can repeat by value


@dataclass(frozen=True)
class Gap:
    """The longest silence in one thread's record stream, and what was open."""

    stack: list[Frame]
    before: Record
    after: Record


def _longest_gap(trace: Trace, thread: int) -> Gap:
    """Replay ``thread``'s transitions and return its longest wall-clock gap.

    The stack open between two consecutive records is the stack the thread was
    sampled in for that whole stretch -- for a hang, the stack it is stuck in.
    """
    mine = [(at, record) for at, record in enumerate(trace.records) if record.thread == thread]
    stack: list[Frame] = []
    best: Gap | None = None
    for (at, current), (_, following) in pairwise(mine):
        if current.action == ENTER:
            stack.append(Frame(current.method, current, at))
        else:
            assert stack and stack[-1].method == current.method, "exit without its enter"
            stack.pop()
        span = following.wall_clock_us - current.wall_clock_us
        if best is None or span > best.after.wall_clock_us - best.before.wall_clock_us:
            best = Gap(list(stack), current, following)
    assert best is not None, f"thread {thread} has fewer than two records"
    return best


def _exit_of(trace: Trace, frame: Frame) -> Record:
    """The record that closes ``frame``: the first exit/unwind of it after its enter."""
    depth = 0
    for record in trace.records[frame.position :]:
        if record.thread != frame.enter.thread or record.method != frame.method:
            continue
        depth += 1 if record.action == ENTER else -1
        if depth == 0:
            return record
    pytest.fail(f"{trace.methods[frame.method]} is never exited")


def _name(trace: Trace, method: int) -> tuple[str, str]:
    cls, name, _signature, _source = trace.methods[method]
    return cls, name


@pytest.fixture(scope="module")
def trace(recorded) -> Trace:
    return _parse_trace(recorded.data(TRACE))


@pytest.fixture(scope="module")
def main_thread(trace) -> int:
    ids = [tid for tid, name in trace.threads.items() if name == "main"]
    assert len(ids) == 1, f"expected one thread named main, got {trace.threads}"
    return ids[0]


# --- the container -----------------------------------------------------------


def test_the_text_header_is_the_documented_sequence(trace):
    """`*version` / `3` / key=value / `*threads` / `*methods` / `*end`, in that order."""
    lines = trace.header_lines
    assert lines[:2] == ["*version", "3"], lines[:2]
    markers = [line for line in lines if line.startswith("*")]
    assert markers == ["*version", "*threads", "*methods", "*end"], markers

    keys = lines[2 : lines.index("*threads")]
    assert all("=" in line for line in keys), keys
    assert trace.header["clock"] == "dual"
    assert trace.header["vm"] == "art"
    assert (
        trace.header["data-file-overflow"] == "false"
    ), "the buffer overflowed, so records at the end of the hang are missing"


def test_the_binary_body_is_a_slow_header_and_fourteen_byte_records(trace):
    """The header after `*end` carries the record size; the body is whole records."""
    assert trace.version == 3
    assert trace.data_offset == 32
    assert trace.record_size == 14, "dual clock: u16 tid + u32 method + 2 x u32 clock"
    assert (trace.body_bytes - trace.data_offset) % trace.record_size == 0
    assert len(trace.records) == int(trace.header["num-method-calls"]), (
        "num-method-calls counts records, so a mismatch means the stream was "
        "cut or the record size was misread"
    )
    assert {record.action for record in trace.records} <= {ENTER, EXIT, UNWIND}
    assert all(record.method in trace.methods for record in trace.records), (
        "a record names a method id that *methods does not define -- the low "
        "two action bits were not masked off"
    )


def test_main_is_among_the_named_threads(trace):
    assert "main" in trace.threads.values(), trace.threads


def test_the_record_clocks_are_in_the_microseconds_the_header_declares(trace):
    """The only unit the header names is `elapsed-time-usec`; the wall clock fits it."""
    elapsed = int(trace.header["elapsed-time-usec"])
    latest = max(record.wall_clock_us for record in trace.records)
    assert 0 < latest <= elapsed, (latest, elapsed)
    assert elapsed >= 40 * US_PER_S, "the trace is shorter than the hang it recorded"


# --- the hang ----------------------------------------------------------------


def test_mains_deepest_open_frame_in_the_gap_is_thread_sleep_under_anr_receiver(trace, main_thread):
    """The question a hang sampler answers: what is main stuck in, and since when.

    The longest silence in main's record stream is the hang. The frame on top
    of the stack open during it is `Thread.sleep` -- three of them, nested, one
    per overload (`sleep(J)` -> `sleep(JI)` -> `sleep(Object, J, I)`) -- and the
    first frame under them is the app's own `AnrReceiver.onReceive`. That frame
    is what a report should name: the app code that called into the block.
    """
    gap = _longest_gap(trace, main_thread)
    names = [_name(trace, frame.method) for frame in gap.stack]

    assert names[-1] == THREAD_SLEEP, f"deepest open frame: {names[-1]}"
    caller = next(name for name in reversed(names) if name != THREAD_SLEEP)
    assert caller == ANR_RECEIVER, f"Thread.sleep was reached from {caller}"
    assert names.count(THREAD_SLEEP) == 3, names[-5:]


def test_the_sleep_dwell_is_the_forty_second_block(trace, main_thread):
    """Exit clock minus enter clock of the deepest frame, in microseconds, >= 39 s."""
    gap = _longest_gap(trace, main_thread)
    deepest = gap.stack[-1]
    closed = _exit_of(trace, deepest)

    dwell_us = closed.wall_clock_us - deepest.enter.wall_clock_us
    assert dwell_us >= 39 * US_PER_S, f"dwell {dwell_us / US_PER_S:.3f}s"
    assert dwell_us <= int(trace.header["elapsed-time-usec"])


def test_records_are_transitions_so_a_block_is_one_enter_and_one_exit(trace, main_thread):
    """Nothing is recorded for main while it is blocked.

    A sampler that expects one row per sample would look for ~40,000 rows of
    `Thread.sleep` here. There are none: the stack did not change, so there
    was no transition to record.
    """
    gap = _longest_gap(trace, main_thread)
    inside = [
        record
        for record in trace.records
        if record.thread == main_thread
        and gap.before.wall_clock_us < record.wall_clock_us < gap.after.wall_clock_us
    ]
    assert inside == []
    elapsed_ms = int(trace.header["elapsed-time-usec"]) // 1000
    assert len(trace.records) < elapsed_ms // 10, (
        f"{len(trace.records)} records over {elapsed_ms} ms of 1 ms sampling: "
        f"that is samples, not transitions"
    )


def test_a_blocked_main_thread_burns_no_cpu_while_it_waits(trace, main_thread):
    """Blocked, not busy -- and only the dual clock can tell the two apart.

    Under `clock=dual` the FIRST clock in a record is thread-CPU time. Across
    the 40 s sleep it advances by well under a second (measured: a fraction of
    a millisecond), while the wall clock advances by the whole 40 s. A hang
    with CPU time equal to its wall time would be a busy loop instead.
    """
    gap = _longest_gap(trace, main_thread)
    deepest = gap.stack[-1]
    closed = _exit_of(trace, deepest)

    cpu_us = closed.thread_clock_us - deepest.enter.thread_clock_us
    wall_us = closed.wall_clock_us - deepest.enter.wall_clock_us
    assert cpu_us < US_PER_S, f"{cpu_us} us of CPU across the sleep"
    assert wall_us > 39 * cpu_us


def test_the_stack_main_already_had_is_replayed_at_the_first_sample(trace, main_thread):
    """Profiling starts mid-life: main's existing frames arrive as enters at once.

    So the first records of a thread are not calls that happened during the
    trace, and a parser timing "calls" from the first enter would credit
    `ActivityThread.main` with the whole trace.
    """
    mine = [record for record in trace.records if record.thread == main_thread]
    opening = [record for record in mine if record.wall_clock_us == mine[0].wall_clock_us]
    assert len(opening) > 3
    assert all(record.action == ENTER for record in opening)
    assert _name(trace, opening[0].method) == ("com.android.internal.os.ZygoteInit", "main")


# --- the recorder's privacy check on this binary file ------------------------
#
# The package aliaser never sees this file, so `_check_hang_trace` is the only
# thing between a capture and a public repository. Each case below renames ONE
# name in the committed trace to something outside the allowlists and requires
# the check to refuse it, naming what it found. The first three are the
# bypasses an independent review demonstrated against the earlier, looser
# check: a `com.example.` class that is not the fixture app's, a private type
# that appears only in a method signature, and a private thread name.


def _header(data: bytes) -> tuple[list[list[str]], list[str]]:
    """(method lines split on tabs, thread names) from a trace's text header."""
    lines = data[: data.index(END_MARKER)].decode("utf-8").splitlines()
    methods = [line.split("\t") for line in lines[lines.index("*methods") + 1 :]]
    threads = [
        line.partition("\t")[2]
        for line in lines[lines.index("*threads") + 1 : lines.index("*methods")]
    ]
    return methods, threads


def _rename(data: bytes, case: str) -> tuple[bytes, str]:
    """The committed trace with one name made private, and that name."""
    methods, threads = _header(data)
    if case == "a com.example class that is not the fixture app's":
        cls = next(m[1] for m in methods if m[1].startswith("android."))
        name = f"com.example.secret.{cls.rsplit('.', 1)[1]}"
        old, new = f"\t{cls}\t", f"\t{name}\t"
    elif case == "a private type only in a signature":
        descriptor = next(m[3] for m in methods if "Landroid/" in m[3])
        typ = descriptor[
            descriptor.index("Landroid/") : descriptor.index(";", descriptor.index("Landroid/")) + 1
        ]
        name = "com.example.secret.Token"
        old, new = typ, "Lcom/example/secret/Token;"
    elif case == "a private thread name":
        thread = next(t for t in threads if t != "main")
        name = "SecretSyncWorker"
        old, new = f"\t{thread}\n", f"\t{name}\n"
    else:  # a class in nobody's public namespace
        cls = next(m[1] for m in methods if m[1].startswith("android."))
        name = f"io.private.app.{cls.rsplit('.', 1)[1]}"
        old, new = f"\t{cls}\t", f"\t{name}\t"
    assert old.encode() in data, f"{case}: nothing to rename"
    return data.replace(old.encode(), new.encode()), name


@pytest.mark.parametrize(
    "case",
    [
        "a com.example class that is not the fixture app's",
        "a private type only in a signature",
        "a private thread name",
        "a class in nobody's public namespace",
    ],
)
def test_the_recorder_refuses_a_trace_naming_anything_outside_its_allowlists(recorded, case):
    import record_fixtures

    data = recorded.data(TRACE)
    record_fixtures._check_hang_trace(data)  # the committed trace itself is acceptable

    tampered, name = _rename(data, case)
    with pytest.raises(record_fixtures.CaptureError, match=re.escape(name)):
        record_fixtures._check_hang_trace(tampered)
