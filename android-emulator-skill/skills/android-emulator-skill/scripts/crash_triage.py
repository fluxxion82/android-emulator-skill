#!/usr/bin/env python3
"""
Android crash triage from the dedicated crash log buffer.

``adb logcat -b crash -d`` reads Android's dedicated *crash* buffer, so a trace
arrives already isolated: triage does not mean grepping the main buffer for
``FATAL EXCEPTION`` and hoping the surrounding lines belong to the same crash.

Measured on emulator-5554 / API 35, not assumed:

- The buffer prints one ``--------- beginning of crash`` separator at the top of
  a dump and *only one*, however many crashes follow -- so blocks are delimited
  by ``FATAL EXCEPTION`` lines, never by the separator.
- A Java crash block is ``FATAL EXCEPTION: <thread>``, ``Process: <pkg>, PID:
  <n>``, the exception class (optionally ``: message``), then the indented
  ``at ...`` frames -- the only multi-line part.
- Repeated crashes append further blocks back to back with no blank line. A
  crash loop restarts the process, so each repeat carries a new PID and
  timestamp while exception, package and frames stay identical: PID and
  timestamp are therefore deliberately *not* part of the dedup key.
- On a device that has not crashed the dump is zero lines and exits 0. Empty is
  a healthy answer, reported as "no crashes" -- never as an error.
- ``adb logcat -b crash -c`` clears the buffer, prints nothing, exits 0.
- ``-v threadtime`` is passed explicitly. It was verified to produce
  byte-identical output to the device default here; passing it pins the format
  this parser expects rather than inheriting a device's log settings.

Scope: only ``AndroidRuntime`` lines are parsed into crashes, because that is
the shape there is recorded evidence for. The buffer also carries native crash
output (``DEBUG`` tombstones, ``libc`` fatal-signal lines); those are counted by
tag and reported rather than silently dropped.

Usage:
    python scripts/crash_triage.py
    python scripts/crash_triage.py --package com.myapp --verbose
    python scripts/crash_triage.py --json
    python scripts/crash_triage.py --clear
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

from common.adb_exec import AdbError, run_adb
from common.device_utils import resolve_device_identifier
from common.env_config import env_int

# The one tag whose lines this parser understands. See "Scope" above.
CRASH_TAG = "AndroidRuntime"

# Text-mode caps (env-configurable via the repo's ANDROID_EMU_ prefix).
MAX_GROUPS_SHOWN = env_int("ANDROID_EMU_CRASH_MAX_GROUPS", 5, min_value=1)
MAX_FRAMES_SHOWN = env_int("ANDROID_EMU_CRASH_MAX_FRAMES", 12, min_value=1)
# A crash dump is small; the default 30s adb budget is ample.
ADB_TIMEOUT = env_int("ANDROID_EMU_CRASH_TIMEOUT", 30, min_value=1)

# Exit statuses. See the --help epilog for why "crashes found" is opt-in.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CRASHES_FOUND = 2

# Framework / platform / stdlib prefixes. Only the *fallback* tier of the
# app-frame heuristic, and named in the output when it fires.
FRAMEWORK_PREFIXES = (
    "android.",
    "androidx.",
    "com.android.",
    "com.google.android.",
    "dalvik.",
    "java.",
    "javax.",
    "junit.",
    "kotlin.",
    "kotlinx.",
    "libcore.",
    "org.junit.",
    "sun.",
)

# logcat threadtime: "MM-DD HH:MM:SS.mmm  PID  TID  P TAG: message"
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})"
    r"\s+(?P<pid>\d+)"
    r"\s+(?P<tid>\d+)"
    r"\s+(?P<priority>[VDIWEF])"
    r"\s+(?P<tag>[^:]+):"
    r"\s?(?P<message>.*)$"
)
# "--------- beginning of crash" — a reader artifact, not a crash boundary.
_SEPARATOR = re.compile(r"^-{3,}\s*beginning of\b")
# "FATAL EXCEPTION: main". The system_server variant prints extra words before
# the colon ("FATAL EXCEPTION IN SYSTEM PROCESS"); that variant is not in the
# recorded fixture, so it is tolerated rather than modelled as its own field.
_FATAL = re.compile(r"^FATAL EXCEPTION(?P<scope>[^:]*):\s*(?P<thread>.+)$")
# "Process: com.example.app, PID: 5731"
_PROCESS = re.compile(r"^Process:\s*(?P<package>[^,\s]+)\s*,\s*PID:\s*(?P<pid>\d+)\s*$")
# "\tat android.app.ActivityThread.main(ActivityThread.java:8705)"
_FRAME = re.compile(r"^\s*at\s+(?P<symbol>\S+)\((?P<source>.*)\)\s*$")
# "android.app.RemoteServiceException$CrashedByAdbException: shell-induced crash"
_EXCEPTION = re.compile(r"^(?P<cls>[A-Za-z_$][\w.$]*)(?::\s*(?P<message>.*))?$")


# === TYPES ===


@dataclass(frozen=True)
class Frame:
    """One stack frame from a Java crash trace.

    Attributes:
        symbol: Fully-qualified method, e.g.
            ``android.app.ActivityThread.main``.
        source: Whatever was inside the parentheses — ``ActivityThread.java:8705``,
            ``Native Method`` or ``Unknown Source:0``. Kept verbatim; the forms
            differ and none of them are reliably a file:line pair.
    """

    symbol: str
    source: str

    def __str__(self) -> str:
        return f"{self.symbol}({self.source})"

    @property
    def is_framework(self) -> bool:
        """Whether the frame is platform/stdlib code by package prefix."""
        return self.symbol.startswith(FRAMEWORK_PREFIXES)


@dataclass(frozen=True)
class FrameChoice:
    """The frame picked for triage, plus *how* it was picked.

    ``basis`` is reported to the user so the heuristic is never mistaken for a
    fact: ``package`` (in the crashing package), ``vendor`` (shares the package's
    first two components — a sibling module), ``non-framework`` (nothing matched
    the app, so the topmost non-platform frame was taken) or ``none``.
    """

    frame: Frame | None
    basis: str


@dataclass
class Crash:
    """One parsed ``FATAL EXCEPTION`` block."""

    timestamp: str
    pid: int
    tid: int
    thread: str
    package: str | None = None
    exception_class: str | None = None
    message: str | None = None
    frames: list[Frame] = field(default_factory=list)
    # AndroidRuntime lines inside the block that are neither header nor frame —
    # "Caused by:", "... 3 more", suppressed-exception lines. Kept verbatim: none
    # appear in the recorded fixture, so there is no ground truth to parse them.
    extra_lines: list[str] = field(default_factory=list)

    @property
    def short_class(self) -> str:
        """Class name without its package, for token-tight output."""
        if not self.exception_class:
            return "unknown"
        return self.exception_class.rsplit(".", maxsplit=1)[-1]

    @property
    def top_frame(self) -> Frame | None:
        """Topmost frame, framework or not."""
        return self.frames[0] if self.frames else None

    def app_frame(self) -> FrameChoice:
        """The most useful frame for triage. See :func:`select_app_frame`."""
        return select_app_frame(self.frames, self.package)


@dataclass
class BufferScan:
    """Everything one crash-buffer dump contained, including what was skipped.

    Attributes:
        crashes: Parsed ``FATAL EXCEPTION`` blocks, in buffer order.
        total_lines: Non-blank lines in the dump.
        other_tags: Counts of log lines carrying a tag this parser does not
            understand — native crash output, chiefly. Surfaced rather than
            dropped, so "0 crashes" over a buffer of tombstone lines cannot read
            as "nothing happened".
        orphan_lines: AndroidRuntime lines seen before any ``FATAL EXCEPTION``,
            i.e. the tail of a trace whose header has rotated out of the buffer.
        unparsed_lines: Lines matching no known shape at all.
    """

    crashes: list[Crash] = field(default_factory=list)
    total_lines: int = 0
    other_tags: dict[str, int] = field(default_factory=dict)
    orphan_lines: int = 0
    unparsed_lines: int = 0


@dataclass
class CrashGroup:
    """Crashes sharing a signature, with the count that makes a loop readable."""

    package: str | None
    exception_class: str | None
    signature_symbol: str
    occurrences: list[Crash]

    @property
    def count(self) -> int:
        return len(self.occurrences)

    @property
    def sample(self) -> Crash:
        """First occurrence — the one whose frames are reported."""
        return self.occurrences[0]

    @property
    def messages(self) -> list[str]:
        """Distinct exception messages across occurrences, in first-seen order."""
        seen: list[str] = []
        for crash in self.occurrences:
            if crash.message and crash.message not in seen:
                seen.append(crash.message)
        return seen


# === PARSING ===


def scan_crash_buffer(text: str) -> BufferScan:
    """Parse a ``adb logcat -b crash -d`` dump into crashes plus accounting.

    Blocks are delimited by ``FATAL EXCEPTION`` lines, not by the separator (see
    the module docstring).

    Args:
        text: Raw dump. An empty string is normal (nothing has crashed).

    Returns:
        A :class:`BufferScan`. ``scan.crashes`` is empty for an empty buffer.
    """
    scan = BufferScan()
    other: Counter[str] = Counter()
    current: Crash | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        scan.total_lines += 1

        if _SEPARATOR.match(raw_line.strip()):
            continue

        match = _LOG_LINE.match(raw_line)
        if match is None:
            scan.unparsed_lines += 1
            continue

        if match["tag"].strip() != CRASH_TAG:
            other[match["tag"].strip()] += 1
            continue

        message = match["message"]
        body = message.strip()

        fatal = _FATAL.match(body)
        if fatal:
            if current is not None:
                scan.crashes.append(current)
            current = Crash(
                timestamp=match["ts"].strip(),
                pid=int(match["pid"]),
                tid=int(match["tid"]),
                thread=fatal["thread"].strip(),
            )
            continue

        if current is None:
            scan.orphan_lines += 1
            continue

        frame = _FRAME.match(message)
        if frame:
            current.frames.append(Frame(symbol=frame["symbol"], source=frame["source"]))
            continue

        process = _PROCESS.match(body)
        if process:
            # First one wins: a second Process line inside one block would
            # otherwise be misread as the exception header.
            if current.package is None:
                current.package = process["package"]
            continue

        if current.exception_class is None:
            exception = _EXCEPTION.match(body)
            if exception:
                current.exception_class = exception["cls"]
                text_message = (exception["message"] or "").strip()
                current.message = text_message or None
                continue

        current.extra_lines.append(body)

    if current is not None:
        scan.crashes.append(current)

    scan.other_tags = dict(other)
    return scan


def parse_crash_buffer(text: str) -> list[Crash]:
    """Parse a crash-buffer dump into crashes. Thin wrapper on :func:`scan_crash_buffer`."""
    return scan_crash_buffer(text).crashes


# === TRIAGE HEURISTIC ===


def select_app_frame(frames: list[Frame], package: str | None) -> FrameChoice:
    """Pick the frame most likely to name the app's own fault site.

    The heuristic, and what it cannot know:

    1. **Package match** — topmost frame whose symbol starts with the crashing
       process's package. The only tier close to evidence, and still not certain:
       the process name carries a ``:suffix`` for secondary processes (stripped
       here), and an ``applicationIdSuffix`` (``.debug``, ``.staging``) makes the
       runtime package *longer* than the source package, so app frames can fail a
       prefix test.
    2. **Vendor match** — topmost frame sharing the package's first two
       components (``com.example``). Catches sibling modules of the same app, and
       also an unrelated artifact under the same vendor prefix.
    3. **Non-framework** — no app frame found, so the topmost frame outside the
       platform/stdlib prefixes is returned. Often a third-party library: the
       best available lead, but *not* the app.

    No tier survives R8/ProGuard obfuscation, where minified frames may not
    contain the package at all, and none recognise inlined or synthetic frames
    (``-$$Nest$m…``, ``Unknown Source:0``). The chosen ``basis`` is returned so
    callers report which tier fired rather than presenting a guess as the answer.

    Args:
        frames: Frames of one crash, topmost first.
        package: Crashing process name from the ``Process:`` line, if any.

    Returns:
        A :class:`FrameChoice`; ``frame`` is None only when every frame is
        framework code (or there are no frames).
    """
    if not frames:
        return FrameChoice(frame=None, basis="none")

    base = (package or "").split(":", maxsplit=1)[0]
    if base:
        for frame in frames:
            if frame.symbol.startswith(f"{base}."):
                return FrameChoice(frame=frame, basis="package")

        parts = base.split(".")
        if len(parts) >= 2:
            vendor = ".".join(parts[:2])
            for frame in frames:
                if frame.symbol.startswith(f"{vendor}."):
                    return FrameChoice(frame=frame, basis="vendor")

    for frame in frames:
        if not frame.is_framework:
            return FrameChoice(frame=frame, basis="non-framework")

    return FrameChoice(frame=None, basis="none")


def crash_signature(crash: Crash) -> tuple[str, str, str]:
    """Dedup key: package, exception class, and the frame that identifies the site.

    The signature frame (the app frame when there is one, else the topmost) is
    included so two different faults raising the same exception class in the same
    app stay separate groups.

    Deliberately excluded: PID and timestamp, which a crash loop changes on every
    repeat (measured); and the exception *message*, which commonly carries
    per-occurrence values (an index, an id, a URL) that would split one fault
    into N groups of one.
    """
    choice = select_app_frame(crash.frames, crash.package)
    frame = choice.frame or crash.top_frame
    return (crash.package or "", crash.exception_class or "", frame.symbol if frame else "")


def group_crashes(crashes: list[Crash]) -> list[CrashGroup]:
    """Group crashes by :func:`crash_signature`, most frequent first.

    Ties keep buffer order, so a repeated crash never reorders unpredictably
    between runs.
    """
    grouped: dict[tuple[str, str, str], CrashGroup] = {}
    for crash in crashes:
        key = crash_signature(crash)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = CrashGroup(
                package=crash.package,
                exception_class=crash.exception_class,
                signature_symbol=key[2],
                occurrences=[crash],
            )
        else:
            existing.occurrences.append(crash)

    ordered = list(grouped.values())
    ordered.sort(key=lambda group: -group.count)
    return ordered


def matches_package(crash: Crash, package: str) -> bool:
    """Whether a crash belongs to ``package``.

    Matches the process name exactly, or a secondary process of it
    (``com.example.app:remote``). Applied post-parse: the crash buffer cannot be
    filtered by package at the adb level.
    """
    if not crash.package:
        return False
    return crash.package == package or crash.package.startswith(f"{package}:")


# === REPORT ===


def build_report(
    scan: BufferScan,
    *,
    device: str | None,
    package_filter: str | None,
) -> dict:
    """Assemble the JSON-ready report that both output modes render from."""
    selected = scan.crashes
    if package_filter:
        selected = [crash for crash in scan.crashes if matches_package(crash, package_filter)]

    groups = group_crashes(selected)
    return {
        "device": device,
        "package_filter": package_filter,
        "crash_count": len(selected),
        "unique_count": len(groups),
        "crashes_in_buffer": len(scan.crashes),
        "buffer_lines": scan.total_lines,
        "unparsed_tags": scan.other_tags,
        "orphan_lines": scan.orphan_lines,
        "unparsed_lines": scan.unparsed_lines,
        "groups": [_group_to_dict(group) for group in groups],
    }


def _group_to_dict(group: CrashGroup) -> dict:
    sample = group.sample
    choice = sample.app_frame()
    frames = [str(frame) for frame in sample.frames]
    return {
        "package": group.package,
        "exception_class": group.exception_class,
        "exception": sample.short_class,
        "message": sample.message,
        "messages": group.messages,
        "thread": sample.thread,
        "count": group.count,
        "pids": [crash.pid for crash in group.occurrences],
        "first_seen": group.occurrences[0].timestamp,
        "last_seen": group.occurrences[-1].timestamp,
        "app_frame": str(choice.frame) if choice.frame else None,
        "app_frame_basis": choice.basis,
        "top_frame": str(sample.top_frame) if sample.top_frame else None,
        "frames": frames[:MAX_FRAMES_SHOWN],
        "frames_truncated": len(frames) > MAX_FRAMES_SHOWN,
        "frame_count": len(frames),
        "extra_lines": sample.extra_lines,
    }


_BASIS_NOTE = {
    "package": "app frame",
    "vendor": "app frame (matched by vendor prefix, not the exact package)",
    "non-framework": "no app frame; topmost non-framework frame",
    "none": "no app frame; every frame is framework code",
}


def format_report(report: dict, *, verbose: bool = False) -> str:
    """Render the report as text: a few lines by default, detail with --verbose."""
    lines: list[str] = []
    device = report["device"] or "default device"
    package_filter = report["package_filter"]

    if report["crash_count"] == 0:
        lines.append(_format_empty(report, device))
        # Accounting is printed even here — especially here: "0 crashes" over a
        # buffer full of tombstone lines would otherwise read as "nothing
        # happened".
        lines.extend(_format_accounting(report))
        if verbose:
            lines.append("The crash buffer is not consumed by reading; use --clear to reset it.")
        return "\n".join(lines)

    scope = f" for {package_filter}" if package_filter else ""
    lines.append(
        f"{report['crash_count']} crash(es){scope}, {report['unique_count']} unique  ({device})"
    )

    groups = report["groups"]
    shown = groups if verbose else groups[:MAX_GROUPS_SHOWN]
    for group in shown:
        lines.extend(_format_group(group, verbose=verbose))

    hidden = len(groups) - len(shown)
    if hidden > 0:
        lines.append(f"  +{hidden} more group(s) — use --verbose or --json")

    lines.extend(_format_accounting(report))
    return "\n".join(lines)


def _format_empty(report: dict, device: str) -> str:
    """An empty buffer is the healthy answer, so say so plainly."""
    package_filter = report["package_filter"]
    if package_filter and report["crashes_in_buffer"]:
        others = report["crashes_in_buffer"]
        return (
            f"No crashes for {package_filter} ({others} crash(es) from other processes) ({device})"
        )
    return f"No crashes in the crash buffer ({device})"


def _format_group(group: dict, *, verbose: bool) -> list[str]:
    package = group["package"] or "unknown process"
    headline = f"[{group['count']}x] {package}  {group['exception']}"
    if group["message"]:
        headline += f": {group['message']}"
    lines = [headline]

    frame = group["app_frame"] or group["top_frame"]
    note = _BASIS_NOTE.get(group["app_frame_basis"], group["app_frame_basis"])
    if frame:
        lines.append(f"     at {frame}  [{note}]")

    if not verbose:
        return lines

    lines.append(f"     exception: {group['exception_class']}")
    lines.append(f"     thread: {group['thread']}  pids: {group['pids']}")
    lines.append(f"     first: {group['first_seen']}  last: {group['last_seen']}")
    if len(group["messages"]) > 1:
        for message in group["messages"][1:]:
            lines.append(f"     also: {message}")
    lines.append(f"     stack ({group['frame_count']} frames):")
    lines.extend(f"       at {entry}" for entry in group["frames"])
    if group["frames_truncated"]:
        lines.append(f"       … {group['frame_count'] - len(group['frames'])} more frame(s)")
    lines.extend(f"     | {entry}" for entry in group["extra_lines"])
    return lines


def _format_accounting(report: dict) -> list[str]:
    """Name what was in the buffer but not parsed, instead of dropping it."""
    lines: list[str] = []
    if report["unparsed_tags"]:
        tags = ", ".join(
            f"{tag} x{count}" for tag, count in sorted(report["unparsed_tags"].items())
        )
        lines.append(
            f"note: {sum(report['unparsed_tags'].values())} line(s) in the buffer from other "
            f"tags are not parsed here ({tags}); read them with `adb logcat -b crash -d`"
        )
    if report["orphan_lines"]:
        lines.append(
            f"note: {report['orphan_lines']} AndroidRuntime line(s) precede the first "
            f"FATAL EXCEPTION — an earlier trace was truncated out of the buffer"
        )
    return lines


# === CLI SURFACE ===


class CrashTriage:
    """Read, clear and triage a device's crash buffer."""

    def __init__(self, serial: str | None = None):
        """Resolve the target device.

        Raises:
            RuntimeError: If ``serial`` names no attached device.
        """
        self.serial = resolve_device_identifier(serial)

    def read_buffer(self) -> str:
        """Dump the crash buffer. Empty output means nothing has crashed."""
        result = run_adb(
            "logcat",
            self.serial,
            "-b",
            "crash",
            "-d",
            "-v",
            "threadtime",
            timeout=ADB_TIMEOUT,
            check=True,
        )
        return result.stdout

    def clear_buffer(self) -> None:
        """Clear the crash buffer (``adb logcat -b crash -c``)."""
        run_adb("logcat", self.serial, "-b", "crash", "-c", timeout=ADB_TIMEOUT, check=True)

    def triage(self, package: str | None = None) -> dict:
        """Read the buffer and return the structured report."""
        scan = scan_crash_buffer(self.read_buffer())
        return build_report(scan, device=self.serial, package_filter=package)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Triage Android crashes from the dedicated crash buffer "
            "(`adb logcat -b crash -d`), grouped and deduplicated."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/crash_triage.py
  python scripts/crash_triage.py --package com.myapp
  python scripts/crash_triage.py --verbose
  python scripts/crash_triage.py --json
  python scripts/crash_triage.py --clear          # reset before a repro run

Exit status (deliberate contract):
  0  the triage ran. An empty crash buffer is a healthy answer, not an error,
     and so is a run that found crashes.
  1  the triage could not run: adb failed, the device is unknown, or the
     arguments were invalid.
  2  crashes were found — but only when --fail-on-crash is passed.

  "Crashes found" is not non-zero by default because exit status answers "did
  the command work", and a triage that finds crashes worked; defaulting to
  non-zero breaks `set -e` pipelines and makes a real adb failure
  indistinguishable from a successful run over a crashy device. To branch on
  "did it crash", read `crash_count` from --json, or pass --fail-on-crash to
  get the status-code branch on demand.

Notes:
  Reading does not consume the buffer, so the same crashes are reported on
  every run until --clear. --clear resets the whole buffer; adb cannot clear
  one package's crashes, so --package is rejected with it rather than implying
  a per-package clear that would not happen.

Environment variables:
  ANDROID_EMU_CRASH_MAX_GROUPS  Groups shown in default text mode (default 5)
  ANDROID_EMU_CRASH_MAX_FRAMES  Frames per group in --verbose/--json (default 12)
  ANDROID_EMU_CRASH_TIMEOUT     Per-adb-call timeout in seconds (default 30)
        """,
    )
    parser.add_argument(
        "--package",
        help=(
            "Only report crashes from this process (post-parse filter; the crash "
            "buffer cannot be filtered by package at the adb level). Also matches "
            "secondary processes, e.g. com.myapp:remote."
        ),
    )
    parser.add_argument(
        "--serial", help="Device serial (auto-detects the default device if omitted)"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the crash buffer (`adb logcat -b crash -c`) and exit",
    )
    parser.add_argument(
        "--fail-on-crash",
        action="store_true",
        help="Exit 2 when crashes are found (default is 0; see Exit status above)",
    )
    parser.add_argument("--verbose", action="store_true", help="Full stacks and per-group detail")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def main() -> int:
    """Parse args, run one mode, print the result."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.clear and args.package:
        parser.error(
            "--clear clears the entire crash buffer; adb has no per-package clear, "
            "so --package cannot be combined with it."
        )

    try:
        triage = CrashTriage(serial=args.serial)
    except (RuntimeError, AdbError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_ERROR

    if args.clear:
        try:
            triage.clear_buffer()
        except AdbError as error:
            print(f"Error: {error}", file=sys.stderr)
            return EXIT_ERROR
        device = triage.serial or "default device"
        if args.json:
            print(json.dumps({"cleared": True, "device": triage.serial}, indent=2))
        else:
            print(f"Crash buffer cleared ({device})")
        return EXIT_OK

    try:
        report = triage.triage(package=args.package)
    except AdbError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report, verbose=args.verbose))

    if args.fail_on_crash and report["crash_count"]:
        return EXIT_CRASHES_FOUND
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
