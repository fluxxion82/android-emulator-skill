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
- ``ANDROID_EMU_ANR_TOTAL_CAP_MB``       Aggregate disk cap in MB (default 100)
- ``ANDROID_EMU_ANR_MAX_RESTARTS``       Worker stream re-spawns on EOF (default 3)
- ``ANDROID_EMU_ANR_DEFAULT_TOP_N``      Default top-N for --stop (default 3)
- ``ANDROID_EMU_ANR_BUDGET_TOKENS``      Default token budget for --stop
- ``ANDROID_EMU_ANR_HOME``               Override storage root (tests)
"""

import argparse
import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

# Resolve imports whether run from repo root or scripts/ directory.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from common.anr_pipeline import (  # noqa: E402
    build_normalised_event,
    compress_to_budget,
    diff_sessions,
    event_to_jsonl,
    format_cluster_detail,
    format_diff,
    format_l0,
    format_l2,
    parse_logcat_anr,
    summary_to_json,
)
from common.anr_sessions import SessionStore  # noqa: E402
from common.device_utils import build_adb_command, resolve_device_identifier  # noqa: E402
from common.env_config import env_int  # noqa: E402

# === CONSTANTS ===

# Skipped-frame events below this frame count are dropped before clustering.
DEFAULT_MIN_FRAMES = 30
# How many times the worker re-spawns ``adb logcat`` after an EOF / subprocess
# death before giving up and marking the session crashed.
DEFAULT_MAX_STREAM_RESTARTS = 3
# Backoff between restart attempts. Short — logcat usually recovers fast.
RESTART_BACKOFF_SECONDS = 2.0
# Best-effort dumpsys pull on --start, as an extra (historical) ANR source.
DUMPSYS_TIMEOUT_SECONDS = 15


def _compute_start_timestamp(duration_str: str) -> str:
    """Parse a duration string and return a ``logcat -t`` start timestamp.

    logcat's ``-t`` accepts ``MM-DD HH:MM:SS.mmm`` and prints lines at or after
    it, then exits (no live follow).

    Raises:
        ValueError: If the format is unrecognised.
    """
    match = re.match(r"(\d+)([smh])", duration_str.lower())
    if not match:
        raise ValueError(
            f"Invalid duration format: {duration_str!r}. Use format like '30s', '5m', '1h'."
        )
    value, unit = match.groups()
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}[unit]
    start = datetime.now() - timedelta(seconds=seconds)
    return start.strftime("%m-%d %H:%M:%S.000")


def matches_package(event: dict, package: str) -> bool:
    """Check if a parsed event belongs to the target package (post-parse filter).

    Applied post-parse so ANR/jank events from system tags (ActivityManager,
    Choreographer) still flow through the pipeline; ``--package`` narrows the
    final output rather than the logcat filter. Matches against the ANR package,
    the component, and the tag's app-name suffix.
    """
    needle = package.lower()
    short = package.rsplit(".", maxsplit=1)[-1].lower()
    haystacks = [
        str(event.get("anr_package", "")).lower(),
        str(event.get("component", "")).lower(),
        str(event.get("message", "")).lower(),
        str(event.get("process", "")).lower(),
    ]
    return any(needle in h for h in haystacks) or any(short and short in h for h in haystacks)


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

    def build_logcat_command(self, since: str | None = None) -> list[str]:
        """Build the ``adb logcat -v threadtime`` command (pure arg→command map).

        Args:
            since: If set, a ``MM-DD HH:MM:SS.mmm`` timestamp for a historical
                dump-and-exit window (``-d -t``) instead of a live stream.

        Returns:
            Complete adb command list ready for subprocess.
        """
        cmd = build_adb_command("logcat", self.serial)
        if since is not None:
            cmd.extend(["-d", "-t", since])
        cmd.extend(["-v", "threadtime"])
        return cmd

    def watch(
        self,
        duration_seconds: int | None = None,
        package: str | None = None,
        json_mode: bool = False,
    ) -> bool:
        """Stream ANR/jank events live from the device."""
        cmd = self.build_logcat_command()
        if not json_mode:
            print("Watching for ANR/jank events", file=sys.stderr)
            if package:
                print(f"Post-parse filter: {package}", file=sys.stderr)

        self._register_signal_handler()
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            start_time = datetime.now()
            for raw_line in iter(self._process.stdout.readline, ""):
                if not raw_line:
                    break
                self._handle_line(raw_line.rstrip(), package, json_mode)
                if (
                    duration_seconds
                    and (datetime.now() - start_time).total_seconds() >= duration_seconds
                ):
                    break
                if self.interrupted:
                    break
            if self._process and self._process.poll() is None:
                self._process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=2)
            return True
        except Exception as error:
            print(f"Error streaming ANR events: {error}", file=sys.stderr)
            return False
        finally:
            if self._process and self._process.poll() is None:
                self._process.terminate()

    def show_since(
        self,
        since_duration: str,
        package: str | None = None,
        json_mode: bool = False,
    ) -> bool:
        """Show historical ANR/jank events using ``logcat -d -t``."""
        since = _compute_start_timestamp(since_duration)
        cmd = self.build_logcat_command(since=since)
        if not json_mode:
            print(f"Showing ANR/jank since {since}", file=sys.stderr)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            for raw_line in result.stdout.splitlines():
                self._handle_line(raw_line.rstrip(), package, json_mode)
            return True
        except subprocess.TimeoutExpired:
            print("Error: logcat timed out after 60s", file=sys.stderr)
            return False
        except Exception as error:
            print(f"Error fetching historical ANR events: {error}", file=sys.stderr)
            return False

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
        event = parse_logcat_anr(line)
        if event is None:
            return
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
        return (
            f"{event['kind'].upper()} {event['timestamp']} | {event['process']} "
            f"(PID {event['pid']}) [{dur}{frame_str}] | {event['message'][:120]}"
        )

    def _register_signal_handler(self) -> None:
        def handle_sigint(_sig, _frame):
            self.interrupted = True
            if self._process:
                self._process.terminate()

        signal.signal(signal.SIGINT, handle_sigint)


# === ANRBUSTER (session mode) ===


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
        aggregate_cap_mb = env_int("ANDROID_EMU_ANR_TOTAL_CAP_MB", 100)
        if aggregate_cap_mb > 0:
            self.store.prune_to_aggregate_cap(aggregate_cap_mb * 1024 * 1024)
        resolved_serial = self._resolve_serial(serial)
        meta = self.store.create({**args, "serial": resolved_serial})
        # Best-effort historical pull via dumpsys, recorded as an extra source.
        self._pull_dumpsys_anr(meta.session_id, resolved_serial)
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
        """Signal worker, drain, build summary, return formatted output."""
        delivered = self.store.signal_worker(session_id, signal.SIGTERM)
        if delivered:
            self._wait_for_worker_exit(session_id, timeout_seconds=2.0)
        meta = self.store.load_meta(session_id)
        line_counters = meta.extras.get("line_counters", {})

        summary = self.store.build_summary(
            session_id,
            matched_lines=line_counters.get("matched", 0),
            total_lines=line_counters.get("total", 0),
            dropped_below_threshold=line_counters.get("dropped", 0),
        )
        # Persist the complete summary BEFORE any capping. Top-N is a display
        # concern; truncating first destroyed every cluster past N on disk, so
        # `--get-details --cluster 4` could never resolve and `--diff` compared
        # two truncated sets, reporting clusters as new or resolved purely
        # because they fell outside the cap.
        self.store.stop(session_id, summary)

        # `top_n or default` treated both None (no flag) and 0 (--all, "no cap")
        # as "unset", so --all returned exactly the default it was meant to lift.
        effective_top_n = env_int("ANDROID_EMU_ANR_DEFAULT_TOP_N", 3) if top_n is None else top_n

        view = summary
        if effective_top_n > 0:
            view = replace(summary, clusters=summary.clusters[:effective_top_n])

        if json_mode:
            return json.dumps(summary_to_json(view), indent=2)
        if terse:
            return format_l0(view)
        budget = budget_tokens or env_int("ANDROID_EMU_ANR_BUDGET_TOKENS", 0) or None
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
        """Drill into a stored session. ``cluster`` is 1-indexed for human use."""
        try:
            self.store.load_meta(session_id)
        except ValueError as error:
            return f"{error}"
        except FileNotFoundError:
            return f"Unknown session: {session_id}"
        summary = self.store.load_summary(session_id)
        if summary is None:
            return f"No summary for {session_id}. Run --stop first."
        if raw:
            return self._dump_raw_events(session_id)
        if cluster is not None:
            index = cluster - 1
            if index < 0 or index >= len(summary.clusters):
                return f"Cluster {cluster} out of range (1..{len(summary.clusters)})"
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
        summary_a = self.store.load_summary(session_a)
        summary_b = self.store.load_summary(session_b)
        if summary_a is None or summary_b is None:
            missing = [s for s, x in [(session_a, summary_a), (session_b, summary_b)] if x is None]
            return f"Missing summary.json for: {', '.join(missing)}"
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
        max_restarts = env_int("ANDROID_EMU_ANR_MAX_RESTARTS", DEFAULT_MAX_STREAM_RESTARTS)

        events_path = self.store.events_path(session_id)
        counters = {"total": 0, "matched": 0, "dropped": 0, "stream_restarts": 0}
        stop_flag = {"value": False}

        def _on_sigterm(_signum, _frame):
            stop_flag["value"] = True

        signal.signal(signal.SIGTERM, _on_sigterm)
        signal.signal(signal.SIGINT, _on_sigterm)

        cmd = AnrWatcher(serial=serial).build_logcat_command()

        def _spawn_logcat() -> subprocess.Popen:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
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
                    )

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
    ) -> int | None:
        """Read lines until EOF / subprocess death / stop request.

        Returns the subprocess exit code. Does not emit ``stream_died`` /
        ``stream_ended`` markers itself — the caller decides which to write.
        """
        last_fsync = time.time()
        while not stop_flag["value"]:
            if proc.stdout is None:
                return proc.poll()
            ready, _, _ = select.select([proc.stdout], [], [], 0.25)
            if not ready:
                if time.time() - last_fsync > 1.0:
                    out_handle.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(out_handle.fileno())
                    last_fsync = time.time()
                exit_code = proc.poll()
                if exit_code is not None:
                    return exit_code
                continue
            line = proc.stdout.readline()
            if not line:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=0.5)
                return proc.poll()
            counters["total"] += 1
            raw_event = parse_logcat_anr(line.rstrip())
            if raw_event is None:
                continue
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
            if normalised is None:
                continue
            out_handle.write(event_to_jsonl(normalised) + "\n")
        return proc.poll()

    # === PRIVATE ===

    def _pull_dumpsys_anr(self, session_id: str, serial: str | None) -> None:
        """Best-effort one-shot ``adb shell dumpsys activity anr`` pull.

        Captures any ANR state already on the device at session start as an extra
        source. Parsed lines that describe ANR/jank are normalised into
        events.jsonl alongside the live stream. Failures are silent — this is a
        bonus source, never a hard dependency.
        """
        cmd = build_adb_command("shell", serial, "dumpsys", "activity", "anr")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DUMPSYS_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return
        if result.returncode != 0 or not result.stdout.strip():
            return
        meta = self.store.load_meta(session_id)
        events_path = self.store.events_path(session_id)
        now_ms = int(time.time() * 1000)
        with open(events_path, "a", buffering=1) as handle:
            for raw_line in result.stdout.splitlines():
                event = parse_logcat_anr(raw_line.rstrip())
                if event is None:
                    continue
                normalised = build_normalised_event(
                    event, session_start_ms=meta.started_at_ms, current_ms=now_ms
                )
                if normalised is not None:
                    handle.write(event_to_jsonl(normalised) + "\n")

    def _wait_for_worker_exit(self, session_id: str, timeout_seconds: float) -> None:
        meta = self.store.load_meta(session_id)
        if not meta.pid:
            return
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                os.kill(meta.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)

    def _dump_raw_events(self, session_id: str) -> str:
        path = self.store.events_path(session_id)
        if not path.exists():
            return ""
        with open(path) as handle:
            return handle.read()

    def _resolve_serial(self, serial: str | None) -> str | None:
        try:
            return resolve_device_identifier(serial)
        except RuntimeError as error:
            raise RuntimeError(str(error)) from error


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
  ANDROID_EMU_ANR_TOTAL_CAP_MB       Aggregate disk cap MB (default 100)
  ANDROID_EMU_ANR_MAX_RESTARTS       Worker stream re-spawns (default 3)
  ANDROID_EMU_ANR_DEFAULT_TOP_N      Default top-N for --stop (default 3)
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


def main():
    """Main entry point — supports legacy + ANRBuster modes from one parser."""
    parser = _build_parser()
    args = parser.parse_args()

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
        try:
            session_id = buster.start(start_args, serial=args.serial)
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
        print(session_id)
        sys.exit(0)

    if args.stop:
        buster = AnrBuster()
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
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
        except FileNotFoundError:
            print(f"Error: unknown session {args.stop}", file=sys.stderr)
            sys.exit(1)
        print(out)
        sys.exit(0)

    if args.get_details:
        buster = AnrBuster()
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
        print(buster.clear_sessions(older_than=args.older_than, json_mode=args.json))
        sys.exit(0)

    if args.diff:
        buster = AnrBuster()
        print(buster.diff(args.diff[0], args.diff[1], json_mode=args.json))
        sys.exit(0)

    # === Legacy modes ===
    if args.since:
        try:
            _compute_start_timestamp(args.since)
        except ValueError as error:
            parser.error(str(error))

    watcher = AnrWatcher(serial=args.serial)
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
    main()
