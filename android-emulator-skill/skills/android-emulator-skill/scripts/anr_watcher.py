#!/usr/bin/env python3
"""
Android ANR / jank watcher — featuring ANRBuster session mode.

The Android-native counterpart of the iOS HangBuster system. The clustering /
session / formatting machinery is ported from ios-simulator-skill; only the
capture + parse layer is rewritten for ``adb logcat``.

Two surfaces live in this file:

1. **AnrWatcher** (legacy, --watch / --since) — passive logcat ANR/jank stream.
2. **AnrBuster** (new, --start / --stop / --get-details / --list-sessions /
   --clear-sessions / --diff) — agent-native session recorder. Detaches a
   worker, normalises and thresholds events on the fly, clusters at stop time,
   emits a token-tight summary with progressive drill paths.

The shared filter pipeline lives in ``common/anr_pipeline.py``; session
storage in ``common/anr_sessions.py``.

Android signals recognised (see ``common.anr_pipeline.parse_logcat_anr``):

- Choreographer ``Skipped N frames!`` jank (duration ~= N * 16.7ms).
- ActivityManager ``ANR in <pkg> (<component>)`` hard ANRs.
- StrictMode / "Slow operation" hints (minor).

Environment variables (all ``ANDROID_EMU_ANR_`` prefixed):

- ``ANDROID_EMU_ANR_MIN_FRAMES``         Min skipped frames kept (default 30)
- ``ANDROID_EMU_ANR_SESSION_TTL_HOURS``  Session prune age (default 24)
- ``ANDROID_EMU_ANR_TOTAL_CAP_MB``       Aggregate disk cap in MB (default 100; 0 = none)
- ``ANDROID_EMU_ANR_MAX_RESTARTS``       Worker stream re-spawns on EOF (default 3; 0 = none)
- ``ANDROID_EMU_ANR_DEFAULT_TOP_N``      Default top-N for --stop (default 3; 0 = all)
- ``ANDROID_EMU_ANR_BUDGET_TOKENS``      Default token budget for --stop
- ``ANDROID_EMU_ANR_HOME``               Override storage root (tests)
"""

import argparse
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

# Resolve imports whether run from repo root or scripts/ directory.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from common import cli, logcat  # noqa: E402
from common.adb_exec import AdbError, classify_device_error, run_adb  # noqa: E402
from common.anr_pipeline import (  # noqa: E402
    AnrRecordAssembler,
    build_normalised_event,
    compress_to_budget,
    diff_sessions,
    event_to_jsonl,
    format_cluster_detail,
    format_diff,
    format_l0,
    format_l2,
    parse_process_age,
    parse_start_proc,
    summary_to_json,
)
from common.anr_sessions import SessionStore  # noqa: E402
from common.device_utils import quote_for_device_shell, resolve_device_identifier  # noqa: E402
from common.env_config import env_int  # noqa: E402

# === CONSTANTS ===

# Skipped-frame events below this frame count are dropped before clustering.
DEFAULT_MIN_FRAMES = 30
# How many times the worker re-spawns ``adb logcat`` after an EOF / subprocess
# death before giving up and marking the session crashed. 0 is a real setting:
# no restarts, so a dead stream marks the session crashed at once.
DEFAULT_MAX_STREAM_RESTARTS = 3
# Backoff between restart attempts. Short — logcat usually recovers fast.
RESTART_BACKOFF_SECONDS = 2.0
# How long the worker waits for a line before treating the stream as quiet:
# an ANR record still open is then complete, and the events file is synced.
STREAM_POLL_SECONDS = 0.25
# On a stop, how long the worker keeps reading what the stream has already
# delivered before it closes the session.
STOP_DRAIN_SECONDS = 0.5
# Bound on a `--since` dump. It is one `logcat -d` read, but of the whole
# buffer, which on a busy device is megabytes.
SINCE_TIMEOUT_SECONDS = 60
# Grace for a `--watch` stream that hit end-of-file to report its exit status.
STREAM_EXIT_TIMEOUT_SECONDS = 5
# Bound on the one device call the worker makes to name a janking pid. Short,
# because --stop waits for it: a 10 s lookup outlasted the stop's wait and the
# summary was saved before the worker had written (review of PR #26).
PS_TIMEOUT = 3
# A process's age (ELAPSED) is in whole seconds; this much slack, plus the
# lookup's own measured latency, keeps a just-started owner from being refused.
_AGE_SLACK_SECONDS = 1.0
# How long --stop waits for the worker to finish before saving the summary: a
# lookup already under way, the stop drain, and a margin to write and exit.
WORKER_EXIT_TIMEOUT_SECONDS = PS_TIMEOUT + STOP_DRAIN_SECONDS + 2.0

_LOGCAT_REMEDY = (
    "Check the device is connected (`adb devices`) and, with more than one attached, "
    "pass --serial."
)


def _compute_start_timestamp(duration_str: str) -> str:
    """Parse a duration string and return a ``logcat -t`` start timestamp.

    logcat's ``-t`` accepts ``MM-DD HH:MM:SS.mmm`` and prints lines at or after
    it, then exits (no live follow). Grammar and formatting are shared with
    every other logcat reader in the skill via ``common.logcat``.

    Raises:
        ValueError: If the format is unrecognised.
    """
    return logcat.window_start_for(duration_str)


def matches_package(event: dict, package: str) -> bool:
    """Check if a parsed event belongs to the target package (post-parse filter).

    Applied post-parse so ANR/jank events from system tags (ActivityManager,
    Choreographer) still flow through the pipeline; ``--package`` narrows the
    final output rather than the logcat filter. Matches against the ANR package,
    the component, the process name the session worker found for the line's pid
    (``app`` -- a jank line names no package, so without it ``--package``
    dropped every jank event), and the tag's app-name suffix.
    """
    needle = package.lower()
    short = package.rsplit(".", maxsplit=1)[-1].lower()
    haystacks = [
        str(event.get("anr_package", "")).lower(),
        str(event.get("app") or "").lower(),
        str(event.get("component", "")).lower(),
        str(event.get("message", "")).lower(),
        str(event.get("process", "")).lower(),
    ]
    return any(needle in h for h in haystacks) or any(short and short in h for h in haystacks)


def _raise_logcat_failure(returncode: int, stderr: str, serial: str | None) -> NoReturn:
    """Report a logcat read that adb ended with a failure status (X6).

    adb's own words decide the type, exactly as ``run_adb`` would have typed
    them: a device that is not there, offline or unauthorized is a device error
    (exit 3 at the CLI boundary); anything else is a failure naming the command.
    """
    detail = " ".join(stderr.split())
    device_error = classify_device_error(detail, serial)
    if device_error is not None:
        raise device_error
    said = f": {detail}" if detail else ", and said nothing on stderr."
    cli.fail(f"`adb logcat` exited {returncode}{said}", _LOGCAT_REMEDY)


# === STREAM READING ===


class _FdLines:
    """Complete lines from a child's stdout, read straight off its descriptor.

    N16: the worker select()ed the fd, then readline()d the file object.
    readline() reads ahead into Python's buffer, so a burst came back as one
    line with the rest parked in that buffer and the fd empty: select() then
    reported nothing to read, and the parked lines waited for the next burst --
    or, when --stop came first, were never read (four lines written, one kept).
    ``os.read`` takes exactly what the fd holds, so what select() calls
    readable is what is read, and nothing is ever parked anywhere select()
    cannot see except a partial line, which is not yet a line.

    Both live readers use it -- the session worker and ``--watch`` -- because
    "the stream has gone quiet" is what completes an ANR record, and a blocking
    readline() can never say so (review of PR #26).
    """

    _CHUNK = 65536

    def __init__(self, stream) -> None:
        self._fd = stream.fileno()
        self._partial = b""

    def read(self, timeout: float) -> list[str] | None:
        """The complete lines that arrive within ``timeout`` seconds.

        Returns:
            The lines, without their newlines; ``[]`` when no complete line
            arrived in time (the stream is quiet); ``None`` at end of stream,
            after a final unterminated line has been returned on its own.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            # Bounded in the source, so the stream guard can read it off: a
            # wait never longer than the poll interval.
            ready, _, _ = select.select([self._fd], [], [], min(remaining, STREAM_POLL_SECONDS))
            if not ready:
                return []
            chunk = os.read(self._fd, self._CHUNK)
            if not chunk:
                if self._partial:
                    tail, self._partial = self._partial, b""
                    return [_decode_line(tail)]
                return None
            *complete, self._partial = (self._partial + chunk).split(b"\n")
            if complete:
                return [_decode_line(line) for line in complete]


def _decode_line(raw: bytes) -> str:
    # Splitting on b"\n" before decoding is safe: in UTF-8 that byte is never
    # part of a multi-byte character.
    return raw.decode("utf-8", errors="replace").rstrip("\r")


class _ProcessNames:
    """pid -> process name, for the lines that name no process: jank above all.

    A Choreographer line says only its pid, and a pid does not outlive a
    restart, so jank keyed by pid read as "1 resolved, 1 new, regression"
    across one (review of PR #26). A pid is also RECYCLED, so a name counts
    only with evidence that its owner already existed when the line was READ.

    That evidence is a comparison of DURATIONS on one clock -- the host's
    ``time.monotonic()`` -- never of wall times. The reader is live (``-T 1``),
    so each line's ``read_at`` is taken as it is read; the device reports its
    owner's AGE (``ps -o PID,NAME,ELAPSED``), measured at ``lookup_at``. The
    owner existed at the read if ``age >= (lookup_at - read_at) - slack``, slack
    being 1 s (ELAPSED's whole seconds) plus the call's own measured latency.
    Wall-clock comparison got DST fall-back, clock steps and the year wrong
    (review of PR #26, round three); none of those can reach a monotonic
    duration, which is why no test here builds a wall clock. Two sources:

    1. ActivityManager's ``Start proc <pid>:<name>/`` lines in the stream itself
       (:meth:`observe`): the owner was born when that line was read.
    2. One bounded device call per pid still unknown, only when ``ask_device``
       (the session worker's resolver, which has the serial) and while
       ``may_ask()`` holds -- the worker stops asking once a stop is requested.

    :meth:`reset` forgets every owner: the worker calls it whenever a stream
    (re)starts, because a pid can change hands in the gap and ``-T 1`` does
    not replay the new owner's ``Start proc``. A line with no owner that
    predates it stays unnamed: keyed by pid, identity "pid".
    """

    def __init__(
        self,
        serial: str | None,
        *,
        ask_device: bool = True,
        may_ask: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._serial = serial
        self._ask_device = ask_device
        self._may_ask = may_ask
        self._clock = clock
        # pid -> (name, latest monotonic time it can have been born, slack),
        # or None when the device said nothing holds it.
        self._owners: dict[int, tuple[str, float, float] | None] = {}
        self._unnamed: set[int] = set()

    @property
    def unnamed(self) -> int:
        """How many pids had at least one line no owner could be found for."""
        return len(self._unnamed)

    def reset(self) -> None:
        """Forget every owner (a new stream: pids may have changed hands)."""
        self._owners.clear()

    def observe(self, line: str, read_at: float) -> None:
        """Learn a pid's owner from a ``Start proc`` line read at ``read_at``."""
        started = parse_start_proc(line)
        if started is not None:
            pid, name = started
            self._owners[pid] = (name, read_at, _AGE_SLACK_SECONDS)

    def name_of(self, pid: int, read_at: float) -> str | None:
        """The name of the process that held ``pid`` when a line was read.

        Args:
            pid: The logcat pid column.
            read_at: ``time.monotonic()`` when the line was read off the stream.
        """
        if pid not in self._owners:
            if not (self._ask_device and self._may_ask()):
                self._unnamed.add(pid)
                return None
            self._owners[pid] = self._ask(pid)
        owner = self._owners[pid]
        if owner is not None:
            name, born_by, slack = owner
            if born_by <= read_at + slack:
                return name
        self._unnamed.add(pid)
        return None

    def _ask(self, pid: int) -> tuple[str, float, float] | None:
        asked_at = self._clock()
        try:
            result = run_adb(
                "shell",
                self._serial,
                "ps",
                "-o",
                "PID,NAME,ELAPSED",
                "-p",
                quote_for_device_shell(str(pid)),
                timeout=PS_TIMEOUT,
            )
        except AdbError:
            # Deliberately not raised: this runs in the detached worker, which
            # has no caller to report to, and a session must not end because a
            # process could not be NAMED. The cost is visible downstream -- the
            # event is keyed by pid and marked identity "pid" -- and counted in
            # the session's `unnamed_pids`.
            return None
        lookup_at = self._clock()
        owner = parse_process_age(result.stdout, pid)
        if owner is None:
            return None
        born_by = lookup_at - owner.age.total_seconds()
        return owner.name, born_by, _AGE_SLACK_SECONDS + (lookup_at - asked_at)


# === LEGACY WATCHER ===


class AnrWatcher:
    """Watch for Android ANR/jank events via ``adb logcat``."""

    def __init__(self, serial: str | None = None):
        """Initialize ANR watcher.

        Args:
            serial: Device serial. Auto-detects default device if None.
        """
        self.serial = serial
        self.events: list[dict] = []
        self.interrupted = False
        self._process: subprocess.Popen | None = None
        # One per watcher: an ANR record can span lines, so whether a line is
        # an event depends on the lines before it (D5).
        self._assembler = AnrRecordAssembler()

    def build_logcat_command(self, since: str | None = None) -> list[str]:
        """Build the ``adb logcat -v threadtime`` command (pure arg→command map).

        A live stream starts at the END of the device's buffer: ``-T 1``.
        Without it `adb logcat` first replays the whole ring buffer, and every
        line of it reads as live. Measured on emulator-5554: a session started
        after a run of provoked ANRs reported every one of them, and every jank
        line still buffered, as happening in its first tenth of a second -- 33
        events for one ANR. ``-T 1`` is logcat's own "from now": it prints the
        single most recent line, then follows (measured). That one line
        predates the stream; it is read like any other.

        Args:
            since: If set, a ``MM-DD HH:MM:SS.mmm`` timestamp for a historical
                dump-and-exit window (``-d -t``) instead of a live stream.

        Returns:
            Complete adb command list ready for subprocess.
        """
        if since is not None:
            return logcat.build_logcat_command(self.serial, since=since)
        return [*logcat.build_logcat_command(self.serial), "-T", "1"]

    def watch(
        self,
        duration_seconds: int | None = None,
        package: str | None = None,
        json_mode: bool = False,
    ) -> bool:
        """Stream ANR/jank events live from the device.

        Raises:
            DeviceError: adb ended the stream because the device is not
                available (exit 3 at the CLI boundary).
            cli.Failure: adb ended the stream with any other failure status.
        """
        cmd = self.build_logcat_command()
        if not json_mode:
            print("Watching for ANR/jank events", file=sys.stderr)
            if package:
                print(f"Post-parse filter: {package}", file=sys.stderr)

        self._register_signal_handler()
        stopped_by_us = False
        # adb's stderr goes to a file rather than a pipe: nothing reads it until
        # the stream has ended, and a pipe nobody reads blocks adb once it
        # fills. It is the only account of WHY a stream ended (X6).
        with tempfile.TemporaryFile() as adb_stderr:
            try:
                # Bytes, unbuffered, read through _FdLines: the same reader as
                # the session worker. A blocking readline() could not notice the
                # stream going quiet, so a complete ANR record sat unprinted
                # until the next line or the end of the stream (review of PR
                # #26); and select() on a buffered pipe loses lines (N16).
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=adb_stderr,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                )
                stream = _FdLines(self._process.stdout)
                # Polling with a timeout also makes --duration exact on a quiet
                # device, which blocked in readline() before (A2).
                deadline = time.monotonic() + duration_seconds if duration_seconds else None
                last_line_at = time.monotonic()
                try:
                    while not self.interrupted:
                        if deadline is not None and time.monotonic() >= deadline:
                            stopped_by_us = True
                            break
                        lines = stream.read(timeout=STREAM_POLL_SECONDS)
                        if lines is None:
                            break
                        if not lines:
                            self._flush(package, json_mode, time.monotonic() - last_line_at)
                            continue
                        last_line_at = time.monotonic()
                        for line in lines:
                            self._handle_line(line, package, json_mode)
                finally:
                    # Whatever is still held when the stream ends -- or breaks
                    # -- was seen, and goes out before anything else is said.
                    self._close(package, json_mode)

                # A deadline and Ctrl-C end the stream themselves, so how adb
                # exited then says nothing.
                if not (stopped_by_us or self.interrupted):
                    self._check_stream_exit(adb_stderr)
                return True
            # No `except Exception` here any more: it printed and returned False,
            # so a missing adb or a vanished device exited 1 with no remedy and,
            # under --json, no refusal object after the records. Failures now
            # reach common.cli, whose refusal is the last line of stdout.
            finally:
                if self._process and self._process.poll() is None:
                    self._process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self._process.wait(timeout=2)
                if self._process and self._process.stdout:
                    self._process.stdout.close()

    def _check_stream_exit(self, adb_stderr) -> None:
        """Raise when a stream that ended on its own was ended by a failure (X6).

        This read stdout to the end and returned True without asking how adb
        exited, so a device that was not there read as a device that had no
        ANRs: exit 0, "No ANR/jank events detected."
        """
        process = self._process
        if process is None:
            return
        try:
            returncode = process.wait(timeout=STREAM_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Its stdout closed but it has not exited: there is no status to
            # judge, and the finally in watch() ends it.
            return
        if not returncode:
            return
        adb_stderr.seek(0)
        _raise_logcat_failure(
            returncode, adb_stderr.read().decode("utf-8", errors="replace"), self.serial
        )

    def show_since(
        self,
        since_duration: str,
        package: str | None = None,
        json_mode: bool = False,
    ) -> bool:
        """Show historical ANR/jank events using ``logcat -d -t``.

        Raises:
            DeviceError: adb could not reach the device (exit 3 at the CLI
                boundary).
            AdbCommandError: logcat exited with any other failure status.
            AdbTimeoutError: the dump took longer than ``SINCE_TIMEOUT_SECONDS``.
        """
        since = _compute_start_timestamp(since_duration)
        if not json_mode:
            print(f"Showing ANR/jank since {since}", file=sys.stderr)
        # Through run_adb, with check=True: a failed read is a typed failure,
        # where it was an empty dump read as "no ANRs" at exit 0 (X6).
        result = run_adb(
            "logcat",
            self.serial,
            *logcat.logcat_args(since=since),
            timeout=SINCE_TIMEOUT_SECONDS,
            check=True,
        )
        for raw_line in result.stdout.splitlines():
            self._handle_line(raw_line.rstrip(), package, json_mode)
        self._close(package, json_mode)
        return True

    def get_summary(self) -> str:
        """Return token-efficient summary of captured events."""
        total = len(self.events)
        if total == 0:
            return "No ANR/jank events detected."
        tags: dict[str, int] = {}
        for event in self.events:
            tag = event.get("process", "unknown")
            tags[tag] = tags.get(tag, 0) + 1
        top = sorted(tags.items(), key=lambda x: x[1], reverse=True)[:5]
        top_str = ", ".join(f"{p}({c})" for p, c in top)
        return f"ANR/jank events: {total} | Tags: {top_str}"

    def get_json_output(self) -> dict:
        """Return full results as a JSON-serialisable dict."""
        return {
            "events": self.events,
            "summary": {
                "total": len(self.events),
                "tags": list({e.get("process") for e in self.events}),
            },
        }

    # === PRIVATE ===

    def _handle_line(self, line: str, package: str | None, json_mode: bool) -> None:
        for event in self._assembler.feed(line):
            self._emit(event, package, json_mode)

    def _flush(self, package: str | None, json_mode: bool, quiet_seconds: float) -> None:
        """The stream is quiet: emit what quiet completes (an open ANR record)."""
        for event in self._assembler.flush(quiet_seconds):
            self._emit(event, package, json_mode)

    def _close(self, package: str | None, json_mode: bool) -> None:
        """The input has ended: emit everything the assembler still holds."""
        for event in self._assembler.close():
            self._emit(event, package, json_mode)

    def _emit(self, event: dict, package: str | None, json_mode: bool) -> None:
        if package and not matches_package(event, package):
            return
        self.events.append(event)
        if json_mode:
            print(json.dumps(event))
            sys.stdout.flush()
        else:
            print(self._format_event(event))

    def _format_event(self, event: dict) -> str:
        ms = event.get("duration_ms", 0)
        dur = f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
        frames = event.get("frames")
        frame_str = f" {frames}frames" if frames is not None else ""
        reason = event.get("reason")
        reason_str = f" | Reason: {reason[:120]}" if reason else ""
        return (
            f"{event['kind'].upper()} {event['timestamp']} | {event['process']} "
            f"(PID {event['pid']}) [{dur}{frame_str}] | {event['message'][:120]}{reason_str}"
        )

    def _register_signal_handler(self) -> None:
        """Stop the stream on Ctrl-C, when we are in a position to ask for it.

        ``signal.signal`` raises ValueError outside the main thread, so calling
        ``watch()`` from any worker thread previously died here before the
        stream even started. Ctrl-C handling is a convenience for interactive
        use, not a correctness requirement -- the duration watchdog stops the
        stream either way -- so a thread that cannot register simply goes
        without it.
        """

        def handle_sigint(_sig, _frame):
            self.interrupted = True
            if self._process:
                self._process.terminate()

        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, handle_sigint)


# === ANRBUSTER (session mode) ===


class SessionError(RuntimeError):
    """A stored ANR session could not be read the way it was asked for.

    Unknown session, no summary yet, a cluster index past the end. All three
    used to be *returned as ordinary strings*, which ``main`` printed on stdout
    before exiting 0 (X5) -- so `--get-details $SID --json` answered
    ``Unknown session: ...`` where the caller was parsing JSON, and the exit
    status said it had worked.
    """


class AnrBuster:
    """Session-mode façade.

    Methods route to ``SessionStore`` + filter pipeline. The worker subprocess
    re-enters this class via ``run_worker()``.
    """

    def __init__(self, store: SessionStore | None = None):
        self.store = store or SessionStore()

    # === PUBLIC API ===

    def start(self, args: dict, serial: str | None) -> str:
        """Create session, detach worker, return session_id once worker registers."""
        self.store.prune_expired()
        # 0 disables the cap -- and env_int's default floor of 1 turned that
        # into a 1 MB cap that pruned sessions (the N14 shape).
        aggregate_cap_mb = env_int("ANDROID_EMU_ANR_TOTAL_CAP_MB", 100, min_value=0)
        if aggregate_cap_mb > 0:
            self.store.prune_to_aggregate_cap(aggregate_cap_mb * 1024 * 1024)
        resolved_serial = self._resolve_serial(serial)
        meta = self.store.create({**args, "serial": resolved_serial})
        cmd = [
            sys.executable,
            __file__,
            "--worker-session-id",
            meta.session_id,
        ]
        # The detached worker survives parent exit. ``start_new_session=True``
        # calls setsid() so the process group is independent of the TTY.
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            self.store.wait_for_worker(meta.session_id, timeout_seconds=3.0)
        except TimeoutError:
            self.store.mark_crashed(meta.session_id)
            raise RuntimeError(f"Worker did not register within 3s for {meta.session_id}") from None
        return meta.session_id

    def stop(
        self,
        session_id: str,
        budget_tokens: int | None = None,
        top_n: int | None = None,
        terse: bool = False,
        json_mode: bool = False,
    ) -> str:
        """Signal worker, wait for it to finish, build summary, return formatted output.

        The summary is built only once the worker has exited, so it holds what
        the worker wrote on its way out -- the held ANR record, the line it was
        naming. If the worker outlives ``WORKER_EXIT_TIMEOUT_SECONDS`` the
        summary is still saved, marked ``incomplete``, and stderr says so: it
        used to be saved after a fixed 2 s with zero events while the worker
        was mid-lookup, and nothing said it was short (review of PR #26).
        """
        delivered = self.store.signal_worker(session_id, signal.SIGTERM)
        finished = True
        if delivered:
            finished = self._wait_for_worker_exit(
                session_id, timeout_seconds=WORKER_EXIT_TIMEOUT_SECONDS
            )
        meta = self.store.load_meta(session_id)
        line_counters = meta.extras.get("line_counters", {})

        summary = self.store.build_summary(
            session_id,
            matched_lines=line_counters.get("matched", 0),
            total_lines=line_counters.get("total", 0),
            dropped_below_threshold=line_counters.get("dropped", 0),
        )
        if not finished:
            summary = replace(summary, incomplete=True)
            print(
                f"warning: the worker for {session_id} was still running "
                f"{WORKER_EXIT_TIMEOUT_SECONDS:.1f}s after it was asked to stop; this "
                f"summary is marked incomplete and may miss what it wrote after that.",
                file=sys.stderr,
            )
        # Persist the complete summary BEFORE any capping. Top-N is a display
        # concern; truncating first destroyed every cluster past N on disk, so
        # `--get-details --cluster 4` could never resolve and `--diff` compared
        # two truncated sets, reporting clusters as new or resolved purely
        # because they fell outside the cap.
        self.store.stop(session_id, summary)

        # `top_n or default` treated both None (no flag) and 0 (--all, "no cap")
        # as "unset", so --all returned exactly the default it was meant to lift.
        # min_value=0: 0 is "no cap" here as it is for --all (the N14 shape).
        effective_top_n = (
            env_int("ANDROID_EMU_ANR_DEFAULT_TOP_N", 3, min_value=0) if top_n is None else top_n
        )

        view = summary
        if effective_top_n > 0:
            view = replace(summary, clusters=summary.clusters[:effective_top_n])

        if json_mode:
            return json.dumps(summary_to_json(view), indent=2)
        if terse:
            return format_l0(view)
        # min_value=0: 0 is "no budget", the default; floored to 1 it forced
        # the one-line L0 output (the N14 shape).
        budget = budget_tokens or env_int("ANDROID_EMU_ANR_BUDGET_TOKENS", 0, min_value=0) or None
        return compress_to_budget(
            view,
            max_tokens=budget,
            default_top_n=effective_top_n if effective_top_n > 0 else len(view.clusters),
        )

    def get_details(
        self,
        session_id: str,
        cluster: int | None = None,
        raw: bool = False,
        json_mode: bool = False,
    ) -> str:
        """Drill into a stored session. ``cluster`` is 1-indexed for human use.

        Raises:
            SessionError: The session is unknown, has no summary yet, or the
                requested cluster index does not exist. Each message names what
                to run instead.
            ValueError: ``session_id`` is not a well-formed session id. Raised
                by the store, which owns that grammar; the CLI rejects it as a
                usage error before reaching here.
        """
        try:
            self.store.load_meta(session_id)
        except FileNotFoundError as error:
            raise SessionError(
                f"Unknown session: {session_id}. Run --list-sessions to see the "
                f"sessions this host has stored."
            ) from error
        summary = self.store.load_summary(session_id)
        if summary is None:
            raise SessionError(
                f"No summary for {session_id}. Run --stop {session_id} first; a "
                f"session has no summary until it is stopped."
            )
        if raw:
            return self._dump_raw_events(session_id)
        if cluster is not None:
            index = cluster - 1
            if index < 0 or index >= len(summary.clusters):
                raise SessionError(
                    f"Cluster {cluster} out of range: this session has "
                    f"{len(summary.clusters)} cluster(s). Pass --cluster between "
                    f"1 and {len(summary.clusters)}, or omit it for the summary."
                )
            target = summary.clusters[index]
            events = [
                e for e in self.store.read_events(session_id) if e.fingerprint == target.fingerprint
            ]
            if json_mode:
                from common.anr_pipeline import cluster_to_json

                return json.dumps(cluster_to_json(target), indent=2)
            return format_cluster_detail(target, events)
        if json_mode:
            return json.dumps(summary_to_json(summary), indent=2)
        return format_l2(summary)

    def list_sessions(self, json_mode: bool = False) -> str:
        metas = self.store.list_sessions()
        if json_mode:
            return json.dumps([m.to_json() for m in metas], indent=2)
        if not metas:
            return "No sessions stored."
        lines = [f"Sessions: {len(metas)}"]
        for meta in metas[:20]:
            stopped = meta.stopped_at or "-"
            counters = meta.extras.get("line_counters", {})
            restarts = counters.get("stream_restarts", 0)
            duration_s = (
                (meta.stopped_at_ms - meta.started_at_ms) / 1000.0 if meta.stopped_at_ms else None
            )
            duration_str = f"  capture={duration_s:.1f}s" if duration_s is not None else ""
            restart_str = f"  restarts={restarts}" if restarts else ""
            lines.append(
                f"  {meta.session_id}  {meta.status:8s}  started={meta.started_at}  "
                f"stopped={stopped}{duration_str}{restart_str}"
            )
        if len(metas) > 20:
            lines.append(f"  ... {len(metas) - 20} more")
        return "\n".join(lines)

    def clear_sessions(self, older_than: str | None = None, json_mode: bool = False) -> str:
        deleted = self.store.clear(older_than=older_than)
        if json_mode:
            return json.dumps({"deleted": deleted, "older_than": older_than})
        suffix = f" older than {older_than}" if older_than else ""
        return f"Cleared {deleted} session(s){suffix}."

    def diff(self, session_a: str, session_b: str, json_mode: bool = False) -> str:
        """Compare two stopped sessions.

        Raises:
            SessionError: Either session has no summary -- it was never stopped,
                or the id does not exist. Returned as text at exit 0 before X5,
                which is indistinguishable from a diff that found no changes.
            cli.Failure: The sessions were captured under different fingerprint
                versions, so the same stall carries a different fingerprint in
                each, and every cluster would read as resolved in one and new in
                the other.
        """
        summary_a = self.store.load_summary(session_a)
        summary_b = self.store.load_summary(session_b)
        if summary_a is None or summary_b is None:
            missing = [s for s, x in [(session_a, summary_a), (session_b, summary_b)] if x is None]
            raise SessionError(
                f"No summary.json for: {', '.join(missing)}. Run --stop <id> for "
                f"each session before --diff, and --list-sessions to see which "
                f"ids exist."
            )
        if summary_a.fingerprint_version != summary_b.fingerprint_version:
            # Refused rather than reported as a "skipped" diff at exit 0, which
            # read as "no change" to anything checking the status.
            cli.fail(
                f"Cannot compare {session_a} (fingerprint v{summary_a.fingerprint_version}) "
                f"with {session_b} (fingerprint v{summary_b.fingerprint_version}): the "
                f"same stall is fingerprinted differently under each, so every cluster "
                f"would read as resolved in one and new in the other.",
                "Record both sessions with the same version of the skill -- re-run the "
                "older scenario with this one -- and diff those.",
            )
        result = diff_sessions(summary_a, summary_b)
        if json_mode:
            return json.dumps(result, indent=2)
        return format_diff(result)

    # === WORKER ===

    def run_worker(self, session_id: str) -> int:
        """Long-running worker entrypoint. Returns exit code.

        Layout: claim meta → build ``adb logcat -v threadtime`` command → open
        events.jsonl line-buffered → for each restart attempt, spawn logcat and
        read lines until EOF or subprocess death. SIGTERM flushes and exits
        cleanly. EOF / subprocess death triggers a bounded restart loop
        (``ANDROID_EMU_ANR_MAX_RESTARTS``); on exhaustion the session is marked
        ``crashed`` rather than left in stale ``running`` state.
        """
        meta = self.store.claim_worker(session_id, pid=os.getpid())
        args = meta.args
        min_frames = int(
            args.get("min_frames", env_int("ANDROID_EMU_ANR_MIN_FRAMES", DEFAULT_MIN_FRAMES))
        )
        package = args.get("package")
        serial = args.get("serial")
        # min_value=0: env_int floors at 1 by default, which turned an explicit
        # "no restarts" into one restart (N14).
        max_restarts = env_int(
            "ANDROID_EMU_ANR_MAX_RESTARTS", DEFAULT_MAX_STREAM_RESTARTS, min_value=0
        )

        events_path = self.store.events_path(session_id)
        counters = {
            "total": 0,
            "matched": 0,
            "dropped": 0,
            "stream_restarts": 0,
            "unnamed_pids": 0,
        }
        stop_flag = {"value": False}
        # One for the whole session: a name learned before a stream restart
        # still holds after it.
        process_names = _ProcessNames(serial, may_ask=lambda: not stop_flag["value"])

        def _on_sigterm(_signum, _frame):
            stop_flag["value"] = True

        signal.signal(signal.SIGTERM, _on_sigterm)
        signal.signal(signal.SIGINT, _on_sigterm)

        cmd = AnrWatcher(serial=serial).build_logcat_command()

        def _spawn_logcat() -> subprocess.Popen:
            # Bytes, unbuffered: the reader takes lines straight off the fd, so
            # there is no Python buffer for a line to wait in (N16).
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )

        proc: subprocess.Popen | None = None
        crashed = False
        try:
            with open(events_path, "a", buffering=1) as out_handle:
                for attempt in range(max_restarts + 1):
                    if stop_flag["value"]:
                        break
                    if attempt > 0:
                        counters["stream_restarts"] = attempt
                        out_handle.write(
                            json.dumps(
                                {
                                    "event": "stream_restart",
                                    "attempt": attempt,
                                    "at_ms": int(time.time() * 1000),
                                }
                            )
                            + "\n"
                        )
                        out_handle.flush()
                        time.sleep(RESTART_BACKOFF_SECONDS)
                        if stop_flag["value"]:
                            break
                    try:
                        proc = _spawn_logcat()
                    except FileNotFoundError:
                        crashed = True
                        break

                    exit_code = self._read_stream_into_events(
                        proc=proc,
                        out_handle=out_handle,
                        stop_flag=stop_flag,
                        counters=counters,
                        package=package,
                        min_frames=min_frames,
                        session_start_ms=meta.started_at_ms,
                        process_names=process_names,
                    )
                    # Read to its end or abandoned on a stop: either way this
                    # stream's pipe is done with, and a restart opens another.
                    if proc.stdout is not None:
                        proc.stdout.close()

                    if stop_flag["value"]:
                        out_handle.write(
                            json.dumps({"event": "stream_ended", "at_ms": int(time.time() * 1000)})
                            + "\n"
                        )
                        out_handle.flush()
                        break

                    out_handle.write(
                        json.dumps(
                            {
                                "event": "stream_died",
                                "exit_code": exit_code,
                                "attempt": attempt,
                                "at_ms": int(time.time() * 1000),
                            }
                        )
                        + "\n"
                    )
                    out_handle.flush()
                else:
                    # for/else: ran every restart without a clean break — exhausted.
                    crashed = True

                with contextlib.suppress(OSError):
                    os.fsync(out_handle.fileno())
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if crashed and not stop_flag["value"]:
                self.store.mark_crashed(session_id)
            self.store.persist_worker_counters(session_id, counters)

        return 2 if crashed else 0

    def _read_stream_into_events(
        self,
        *,
        proc: subprocess.Popen,
        out_handle,
        stop_flag: dict,
        counters: dict,
        package: str | None,
        min_frames: int,
        session_start_ms: int,
        process_names: _ProcessNames | None = None,
    ) -> int | None:
        """Read lines until EOF / subprocess death / stop request.

        Returns the subprocess exit code. Does not emit ``stream_died`` /
        ``stream_ended`` markers itself — the caller decides which to write.

        ``process_names`` names the process behind a jank or StrictMode line
        (the worker passes one that may ask the device). Without one, only the
        stream's own ``Start proc`` lines name anything -- the device is never
        asked.
        """
        if proc.stdout is None:
            return proc.poll()
        stream = _FdLines(proc.stdout)
        assembler = AnrRecordAssembler()
        names = process_names or _ProcessNames(None, ask_device=False)
        # A new stream: any pid may have changed hands while there was none,
        # and `-T 1` does not replay the new owner's Start proc line.
        names.reset()

        def record(raw_events: list[dict], read_at: float | None = None) -> None:
            for raw_event in raw_events:
                # Both are logged from inside the app, so the logcat pid is the
                # app's own -- and neither line says which app that is. Named
                # only for a line just read: its read time is the evidence.
                if raw_event["kind"] in ("jank", "strictmode"):
                    raw_event["app"] = (
                        names.name_of(raw_event["pid"], read_at) if read_at is not None else None
                    )
                if package and not matches_package(raw_event, package):
                    continue
                counters["matched"] += 1
                frames = raw_event.get("frames")
                # Only Choreographer jank is frame-thresholded; ANRs (frames=None) pass.
                if frames is not None and frames < min_frames:
                    counters["dropped"] += 1
                    continue
                normalised = build_normalised_event(
                    raw_event,
                    session_start_ms=session_start_ms,
                    current_ms=int(time.time() * 1000),
                )
                if normalised is not None:
                    out_handle.write(event_to_jsonl(normalised) + "\n")

        def take(lines: list[str]) -> None:
            read_at = time.monotonic()
            for line in lines:
                counters["total"] += 1
                names.observe(line, read_at)
                record(assembler.feed(line), read_at)

        try:
            last_fsync = time.time()
            last_line_at = time.monotonic()
            while not stop_flag["value"]:
                lines = stream.read(timeout=STREAM_POLL_SECONDS)
                if lines is None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=0.5)
                    return proc.poll()
                if lines:
                    last_line_at = time.monotonic()
                    take(lines)
                    continue
                # Quiet. An ActivityManager ANR record arrives as one burst, so
                # one still open now has ended: write it rather than hold it
                # until the next unrelated line happens along.
                record(assembler.flush(time.monotonic() - last_line_at))
                if time.time() - last_fsync > 1.0:
                    out_handle.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(out_handle.fileno())
                    last_fsync = time.time()
                exit_code = proc.poll()
                if exit_code is not None:
                    return exit_code

            # Stopped. Whatever the stream had already delivered is part of the
            # session: read it before closing, bounded, since logcat keeps writing.
            deadline = time.monotonic() + STOP_DRAIN_SECONDS
            while time.monotonic() < deadline:
                lines = stream.read(timeout=0)
                if not lines:
                    break
                take(lines)
            return proc.poll()
        finally:
            # However this stream ended, what the assembler still holds was
            # seen: an open record, and a WindowManager report nothing answered.
            record(assembler.close())
            counters["unnamed_pids"] = names.unnamed

    # === PRIVATE ===

    def _wait_for_worker_exit(self, session_id: str, timeout_seconds: float) -> bool:
        """Whether the worker exited within ``timeout_seconds``."""
        meta = self.store.load_meta(session_id)
        if not meta.pid:
            return True
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                os.kill(meta.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def _dump_raw_events(self, session_id: str) -> str:
        path = self.store.events_path(session_id)
        if not path.exists():
            return ""
        with open(path) as handle:
            return handle.read()

    def _resolve_serial(self, serial: str | None) -> str | None:
        # Not re-wrapped: a bare RuntimeError lost DeviceNotFoundError, so an
        # unavailable device exited 1 here and 3 everywhere else.
        return resolve_device_identifier(serial)


# === CLI ===


def _add_mode_args(parser: argparse.ArgumentParser) -> None:
    """Add mutually-exclusive mode flags (legacy + ANRBuster subcommands)."""
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--watch", action="store_true", help="Legacy live stream (until --duration / Ctrl-C)"
    )
    mode_group.add_argument(
        "--since",
        metavar="DURATION",
        help="Legacy historical query (e.g. 5m, 1h, 30s) via 'logcat -d -t'",
    )
    mode_group.add_argument(
        "--start",
        action="store_true",
        help="Start an ANRBuster session (detached worker, returns session ID)",
    )
    mode_group.add_argument(
        "--stop", metavar="SESSION_ID", help="Stop a session and emit the summary"
    )
    mode_group.add_argument(
        "--get-details",
        metavar="SESSION_ID",
        help="Drill into a stored session (combine with --cluster N or --raw)",
    )
    mode_group.add_argument(
        "--list-sessions", action="store_true", help="List stored ANRBuster sessions"
    )
    mode_group.add_argument("--clear-sessions", action="store_true", help="Delete stored sessions")
    mode_group.add_argument(
        "--diff", nargs=2, metavar=("SESSION_A", "SESSION_B"), help="Compare two sessions"
    )
    # Internal worker entry — hidden from --help.
    mode_group.add_argument("--worker-session-id", metavar="ID", help=argparse.SUPPRESS)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Watch for Android ANR/jank events via adb logcat. Use --watch/--since "
            "for the live stream, or --start/--stop for ANRBuster session mode."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Also reachable as `python scripts/logs.py anr ...` — the unified log entry
point, which routes here unchanged. Both invocations are supported.

Examples:
  # ANRBuster session mode (agent-friendly):
  SID=$(python scripts/anr_watcher.py --start --package com.myapp)
  # ... interact with the app ...
  python scripts/anr_watcher.py --stop $SID
  python scripts/anr_watcher.py --get-details $SID --cluster 1
  python scripts/anr_watcher.py --list-sessions
  python scripts/anr_watcher.py --diff $SID_A $SID_B
  python scripts/anr_watcher.py --clear-sessions --older-than 24h

  # Legacy:
  python scripts/anr_watcher.py --watch --duration 60
  python scripts/anr_watcher.py --since 5m --json

Environment variables:
  ANDROID_EMU_ANR_MIN_FRAMES         Min skipped frames kept (default 30)
  ANDROID_EMU_ANR_SESSION_TTL_HOURS  Session prune age (default 24)
  ANDROID_EMU_ANR_TOTAL_CAP_MB       Aggregate disk cap MB (default 100; 0 = none)
  ANDROID_EMU_ANR_MAX_RESTARTS       Worker stream re-spawns (default 3; 0 = none)
  ANDROID_EMU_ANR_DEFAULT_TOP_N      Default top-N for --stop (default 3; 0 = all)
  ANDROID_EMU_ANR_BUDGET_TOKENS      Default token budget for --stop
        """,
    )
    _add_mode_args(parser)

    # Filters / target
    parser.add_argument(
        "--package",
        help=(
            "Post-parse filter: keep only events whose ANR package / component / tag "
            "matches. Capture stays device-global (system tags are kept)."
        ),
    )
    parser.add_argument("--serial", help="Device serial (auto-detects default device if omitted)")

    # Legacy-only
    parser.add_argument(
        "--duration", type=int, metavar="SECONDS", help="Stop after N seconds (--watch only)"
    )

    # ANRBuster knobs
    parser.add_argument(
        "--min-frames",
        type=int,
        help=(
            "Drop skipped-frame events below this frame count "
            "(default 30 / env ANDROID_EMU_ANR_MIN_FRAMES)"
        ),
    )
    parser.add_argument("--top", type=int, dest="top_n", help="Top-N clusters to retain in summary")
    parser.add_argument(
        "--all", action="store_true", dest="all_clusters", help="Keep all clusters (no top-N cap)"
    )
    parser.add_argument(
        "--budget-tokens", type=int, help="Max tokens for --stop output (picks L0/L1/L2)"
    )
    parser.add_argument(
        "--cluster", type=int, help="Cluster index (1-based) for --get-details drill"
    )
    parser.add_argument("--raw", action="store_true", help="With --get-details: dump events.jsonl")
    parser.add_argument(
        "--older-than", help="With --clear-sessions: delete sessions older than e.g. 24h"
    )
    parser.add_argument("--terse", action="store_true", help="--stop: force L0 one-line output")

    # Output
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def _require_valid_session_ids(parser, store, session_ids) -> None:
    """Reject a malformed session id as a usage error (exit 2).

    The grammar belongs to ``SessionStore.session_dir``, which is where the
    path is built and where the traceback came from: `--diff a/b c` raised
    ValueError out of the store, through main, to the terminal (D7). Asking the
    store keeps one rule rather than a copy of it here; ``session_dir`` only
    validates and joins, it touches no filesystem.
    """
    for session_id in session_ids:
        try:
            store.session_dir(session_id)
        except ValueError as error:
            parser.error(str(error))


def main(argv: list[str] | None = None):
    """Main entry point — supports legacy + ANRBuster modes from one parser."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # === ANRBuster worker entry ===
    if args.worker_session_id:
        buster = AnrBuster()
        sys.exit(buster.run_worker(args.worker_session_id))

    # === ANRBuster session subcommands ===
    if args.start:
        buster = AnrBuster()
        start_args = {
            "min_frames": (
                args.min_frames
                if args.min_frames is not None
                else env_int("ANDROID_EMU_ANR_MIN_FRAMES", DEFAULT_MIN_FRAMES)
            ),
            "package": args.package,
        }
        session_id = buster.start(start_args, serial=args.serial)
        # The bare id is the text form, so `SID=$(anr_watcher.py --start)` keeps
        # working. Under --json it was the same bare id: a JSON document, by
        # luck of being a token, but not an object an agent could read a field
        # from -- and not the shape every other mode answers in.
        if args.json:
            print(json.dumps({"success": True, "session_id": session_id}))
        else:
            print(session_id)
        sys.exit(0)

    if args.stop:
        buster = AnrBuster()
        # A malformed id is a usage error here as it is for --get-details and
        # --diff; it reached the script-local `_fail` below as a ValueError.
        _require_valid_session_ids(parser, buster.store, [args.stop])
        # 0 means no cap, matching the repo's '0 = disabled' convention.
        top_n = 0 if args.all_clusters else args.top_n
        try:
            out = buster.stop(
                args.stop,
                budget_tokens=args.budget_tokens,
                top_n=top_n,
                terse=args.terse,
                json_mode=args.json,
            )
        except ValueError as error:
            # The id is well-formed, so this is the session's own files failing
            # to parse. Reported through the boundary in its one shape, where a
            # script-local `_fail` printed {"error": ...} and exited 1 itself.
            cli.fail(
                f"Session {args.stop} could not be read: {error}",
                f"Its files under {buster.store.session_dir(args.stop)} are damaged. "
                f"Delete that directory, or run --clear-sessions, and record a new session.",
            )
        except FileNotFoundError as error:
            # Re-raised typed, with the same words get_details uses, rather than
            # reported here: common.cli is the one place a failure is printed.
            raise SessionError(
                f"Unknown session: {args.stop}. Run --list-sessions to see the "
                f"sessions this host has stored."
            ) from error
        print(out)
        sys.exit(0)

    if args.get_details:
        buster = AnrBuster()
        _require_valid_session_ids(parser, buster.store, [args.get_details])
        out = buster.get_details(
            args.get_details,
            cluster=args.cluster,
            raw=args.raw,
            json_mode=args.json,
        )
        print(out)
        sys.exit(0)

    if args.list_sessions:
        buster = AnrBuster()
        print(buster.list_sessions(json_mode=args.json))
        sys.exit(0)

    if args.clear_sessions:
        buster = AnrBuster()
        try:
            out = buster.clear_sessions(older_than=args.older_than, json_mode=args.json)
        except ValueError as error:
            # `--older-than 24hours` reached the terminal as a ValueError
            # traceback (D7). The duration grammar is the store's, and it is
            # resolved before anything is deleted, so this is a usage error and
            # nothing has been removed by the time it is raised.
            parser.error(str(error))
        print(out)
        sys.exit(0)

    if args.diff:
        buster = AnrBuster()
        _require_valid_session_ids(parser, buster.store, args.diff)
        out = buster.diff(args.diff[0], args.diff[1], json_mode=args.json)
        print(out)
        sys.exit(0)

    # === Legacy modes ===
    if args.since:
        try:
            _compute_start_timestamp(args.since)
        except ValueError as error:
            parser.error(str(error))

    # Resolved before adb is asked anything: `adb -s <unknown> logcat` does not
    # fail, it prints "- waiting for device -" and blocks (measured), so a
    # mistyped --serial would read as a quiet device for as long as --watch
    # ran. An unknown serial is DeviceNotFoundError here, exit 3.
    watcher = AnrWatcher(serial=resolve_device_identifier(args.serial))
    if args.watch:
        success = watcher.watch(
            duration_seconds=args.duration,
            package=args.package,
            json_mode=args.json,
        )
    else:
        success = watcher.show_since(
            since_duration=args.since,
            package=args.package,
            json_mode=args.json,
        )
    if not success:
        sys.exit(1)
    if not args.json and not args.watch:
        print(f"\n{watcher.get_summary()}")
    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
