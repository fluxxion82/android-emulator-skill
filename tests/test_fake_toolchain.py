"""The harness the red specs stand on, checked directly.

``tests/fake_toolchain.py`` holds two things every Inc 1 spec depends on: the
stub tools, and ``assert_refusal``, the refusal contract of the shared CLI
boundary (PR #18). A contract checker that accepted a traceback, a bare
``null`` or a prompt glued to the JSON would make every refusal spec vacuous
in the same direction at once -- the review of PR #20 found the first version
of these specs did exactly that by checking "non-zero, and the tool's name
somewhere". So the checker is exercised here against each shape it must
accept and each it must reject.

The documents are built with ``json.dumps`` and ``"\\n".join`` from short
values: they are this skill's OWN output shape, not a tool's, and nothing here
is handed to a parser.

And ``FakeToolchain.run``'s limit: a script killed there must take the stubs it
started with it.
"""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import time

import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from fake_toolchain import FakeToolchain, Rule, assert_refusal, framed, frames

API33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")

REFUSAL = {"success": False, "error": "`sdkmanager` was not found", "remedy": "Install it."}


def _run(returncode: int = 1, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


def _text_refusal() -> str:
    return "\n".join(["Error: `sdkmanager` was not found", "Remedy: Install it."]) + "\n"


def test_the_contract_shape_is_accepted_in_both_modes():
    payload = assert_refusal(
        _run(stdout=json.dumps(REFUSAL) + "\n"),
        json_mode=True,
        naming=("sdkmanager",),
        status=1,
    )
    assert payload == REFUSAL
    assert (
        assert_refusal(
            _run(stderr=_text_refusal()), json_mode=False, naming=("sdkmanager",), status=1
        )
        is None
    )


def test_a_streaming_refusal_is_the_last_line():
    record = json.dumps({"kind": "anr"})
    stdout = "\n".join([record, json.dumps(REFUSAL)]) + "\n"
    assert_refusal(
        _run(stdout=stdout), json_mode=True, naming=("sdkmanager",), status=1, streaming=True
    )
    with pytest.raises(AssertionError):
        assert_refusal(_run(stdout=stdout), json_mode=True, naming=("sdkmanager",), status=1)


REJECTED_JSON = {
    "a traceback": _run(stdout=json.dumps(REFUSAL), stderr="Traceback (most recent call last):"),
    "the wrong status": _run(returncode=3, stdout=json.dumps(REFUSAL)),
    "a bare null": _run(stdout="null"),
    "a prompt before the object": _run(stdout="Delete? " + json.dumps(REFUSAL)),
    "success true": _run(stdout=json.dumps({**REFUSAL, "success": True})),
    "no remedy": _run(stdout=json.dumps({"success": False, "error": "sdkmanager"})),
    "an error naming nothing": _run(stdout=json.dumps({**REFUSAL, "error": "it failed"})),
    "the legacy error-only shape": _run(stdout=json.dumps({"error": "sdkmanager missing"})),
    "an empty stdout": _run(stderr=_text_refusal()),
}


@pytest.mark.parametrize("case", list(REJECTED_JSON))
def test_json_mode_rejects(case):
    with pytest.raises(AssertionError):
        assert_refusal(REJECTED_JSON[case], json_mode=True, naming=("sdkmanager",), status=1)


TRACEBACK_HEAD = "Traceback (most recent call last):"


def test_a_traceback_on_stdout_is_rejected_in_streaming_mode():
    """A traceback ahead of a well-formed last line is still a crash.

    The first checker looked for tracebacks on stderr only, so this passed
    (verification of PR #20).
    """
    stdout = "\n".join([TRACEBACK_HEAD, json.dumps(REFUSAL)]) + "\n"
    with pytest.raises(AssertionError):
        assert_refusal(
            _run(stdout=stdout), json_mode=True, naming=("sdkmanager",), status=1, streaming=True
        )


REJECTED_TEXT = {
    "no Error line": _run(stderr="sdkmanager is missing\n"),
    "no Remedy line": _run(stderr="Error: sdkmanager was not found\n"),
    "JSON on stdout": _run(stdout=json.dumps(REFUSAL) + "\n", stderr=_text_refusal()),
    "an Error line naming nothing": _run(stderr="\n".join(["Error: it failed", "Remedy: retry"])),
    # Each of these passed the first checker, which looked at stdout one line
    # at a time and only for objects (verification of PR #20).
    "a traceback on stdout": _run(stdout=TRACEBACK_HEAD + "\n", stderr=_text_refusal()),
    "indented JSON on stdout": _run(stdout=json.dumps(REFUSAL, indent=2), stderr=_text_refusal()),
    "null on stdout": _run(stdout="null\n", stderr=_text_refusal()),
    "an array on stdout": _run(stdout=json.dumps(["sdkmanager"]), stderr=_text_refusal()),
}


@pytest.mark.parametrize("case", list(REJECTED_TEXT))
def test_text_mode_rejects(case):
    with pytest.raises(AssertionError):
        assert_refusal(REJECTED_TEXT[case], json_mode=False, naming=("sdkmanager",), status=1)


def test_a_malformed_object_is_loud_not_excused():
    """Looks like an object, is not one: a broken fix, so not an AssertionError."""
    with pytest.raises(json.JSONDecodeError):
        assert_refusal(_run(stdout='{"success": false,}'), json_mode=True, naming=("x",), status=1)


# --- a run past its limit takes what it started with it (review of PR #36) ------

# How long a killed stub may take to be reaped once its parent is gone too.
REAP_LIMIT = 5


def _gone_within(pid: int, seconds: float) -> bool:
    """Whether ``pid`` stops existing within ``seconds`` (killed, then reaped)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_run_past_its_limit_leaves_no_stub_running(tmp_path, recorded):
    """The script is killed at the limit, and so is the hanging stub it started.

    ``log_monitor --follow`` over a ``logcat`` that waits never ends: the limit
    is its end here. ``subprocess.run`` killed the script alone, and the stub
    (``hang=True``, an hour's sleep) outlived it, one per timed-out run. Every
    answer is recorded: adb_get_state, getprop_sdk (a level that streams), then
    adb_waiting_for_device's banner.
    """
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "adb",
        [
            Rule(("get-state",), stdout=recorded.text("adb_get_state")),
            Rule(("shell", "getprop", "ro.build.version.sdk"), stdout=recorded.text("getprop_sdk")),
            Rule(("logcat",), stderr=recorded.text("adb_waiting_for_device"), hang=True),
        ],
    )
    with pytest.raises(subprocess.TimeoutExpired):
        chain.run("log_monitor.py", "--follow", timeout=4)

    stubs = [pid for pid, argv in chain.stub_pids("adb") if "logcat" in argv]
    assert len(stubs) == 1, f"the script never started its logcat: {chain.calls('adb')}"
    gone = _gone_within(stubs[0], REAP_LIMIT)
    if not gone:
        # Red or not, nothing is left behind.
        os.kill(stubs[0], signal.SIGKILL)
    assert gone, f"the hanging logcat stub (pid {stubs[0]}) outlived the run that started it"


# --- the stub's `-t <epoch>` over recorded records -------------------------------


def _ask_adb(chain: FakeToolchain, *argv: str) -> bytes:
    return subprocess.run(
        [str(chain.bin / "adb"), *argv],
        env=chain.env(),
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout


def _epoch(frame, delta_ns: int = 0) -> str:
    sec, nsec = struct.unpack_from("<II", frame.raw, 12)
    total = sec * 1_000_000_000 + nsec + delta_ns
    return f"{total // 1_000_000_000}.{total % 1_000_000_000:09d}"


def test_the_fakes_epoch_reads_the_records_stamped_after_it(tmp_path):
    """As logcat does (measured: logcat_stream_marks_records, ``since_is_strict``)."""
    window = frames(API33.data("logcat_stream_marks_window_records.bin"))
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.install("adb", [Rule(("logcat",), stdout=framed(window))])

    assert _ask_adb(chain, "logcat", "-d", "-B", "-t", _epoch(window[2])) == framed(window[3:])
    assert _ask_adb(chain, "logcat", "-d", "-B", "-t", _epoch(window[2], -1)) == framed(window[2:])


@pytest.mark.parametrize("kept", [5, 25], ids=["inside-the-header", "inside-the-payload"])
def test_the_fakes_epoch_keeps_a_cut_dump_cut(kept, tmp_path):
    """A dump cut inside a record stays cut when ``-t`` filters it.

    The filter dropped a cut shorter than eight bytes, and filtered a longer
    one by the stamp it may not even hold -- turning a cut dump into a clean
    one, which a reader then took for the whole window (review of closure B).
    Derived: a recorded window and the first ``kept`` bytes of its last record.
    """
    window = frames(API33.data("logcat_stream_marks_window_records.bin"))
    cut = window[-1].raw[:kept]
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.install("adb", [Rule(("logcat",), stdout=framed(window) + cut)])

    answer = _ask_adb(chain, "logcat", "-d", "-B", "-t", _epoch(window[2]))

    assert answer == framed(window[3:]) + cut


# --- the stub's `logged=`: the line a `shell log` call wrote, in the buffer after it ------


def test_the_fakes_placeholder_frame_is_the_line_shell_log_wrote_once_it_was_written(tmp_path):
    """Absent before the call; after it, the recorded frame carrying the call's tag and message.

    Derived: the marks window, a shell record (uid 2000) of it carrying a
    placeholder. ``-T`` counts the frame only once it is there.
    """
    window = frames(API33.data("logcat_stream_marks_window_records.bin"))
    shell = next(frame for frame in window if frame.uid == 2000)
    placeholder = shell.replace(message="placeholder")
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.install(
        "adb",
        [
            Rule(("shell", "log")),
            Rule(("logcat",), buffered=framed([*window, placeholder]), logged="placeholder"),
        ],
    )

    assert _ask_adb(chain, "logcat", "-d", "-B", "-T", "1") == window[-1].raw
    _ask_adb(chain, "shell", "log", "-t", "aes-log-monitor", "anchor-1")
    written = shell.replace(tag="aes-log-monitor", message="anchor-1")

    assert _ask_adb(chain, "logcat", "-d", "-B", "-T", "1") == written.raw
    assert _ask_adb(chain, "logcat", "-d", "-B") == framed([*window, written])
