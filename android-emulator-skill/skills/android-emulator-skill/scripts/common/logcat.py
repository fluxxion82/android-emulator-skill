#!/usr/bin/env python3
"""The one place that knows how to ask ``adb`` for logs.

Four scripts in this skill read logcat, and before this module each of them
built its own ``adb logcat`` argv and parsed its own duration strings. That is
how ``app_state_capture`` ended up accepting ``30s`` and ``5m`` but rejecting
``1h``, while ``log_monitor`` and ``anr_watcher`` accepted all three -- the same
flag meaning different things depending on which script the agent reached for.
It is also how ``log_monitor`` once shipped ``-v time`` against a parser that
only understands ``-v threadtime``, matching zero lines while every test passed.

So the argv shape and the duration grammar live here, once, and the four callers
select options rather than re-deriving them:

    log_monitor        -> stream / historical window on the main buffers
    anr_watcher        -> stream / historical window, ANR+jank parse on top
    crash_triage       -> ``-b crash`` dump, a *different buffer*
    app_state_capture  -> historical window as one artifact of a snapshot

**Argument order is part of the contract.** Every caller emits

    adb [-s SERIAL] logcat [-b BUF] [-d] [-t TS] [-v FMT] [--pid=N] [*:PRIORITY]

which is the union of what the four produced before, in the order they produced
it, so the migration changed no command any of them sends.

This module is pure: it builds argv and parses strings, and never touches a
device. Execution stays with ``common.adb_exec.run_adb`` (bounded, typed errors)
or, for the two streaming readers, their own watchdog-terminated ``Popen``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from common.device_utils import build_adb_command

# `logcat -t` accepts a "MM-DD HH:MM:SS.mmm" timestamp and prints lines at or
# after it. Milliseconds are always zeroed: the window is a coarse "since
# roughly then", and a spurious sub-second precision would imply otherwise.
TIMESTAMP_FORMAT = "%m-%d %H:%M:%S.000"

# `threadtime` is the default because it is the only format the parsers in this
# skill can read: "MM-DD HH:MM:SS.mmm PID TID P Tag: msg". The `time` format
# omits the PID/TID columns and writes "P/Tag( PID):" instead. Passing it
# explicitly also pins the format rather than inheriting a device's log
# settings, which is why crash_triage passes it even though the emulator's
# default happened to match.
DEFAULT_FORMAT = "threadtime"

# Deliberately un-anchored at the end, matching the grammar log_monitor and
# anr_watcher have always accepted (so "30sec" still means 30 seconds). The
# `[smh]` set is the union: app_state_capture accepted only `[sm]`, which made
# `--logs 1h` a silent "invalid duration" there and nowhere else.
_DURATION = re.compile(r"(\d+)([smh])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# Ascending logcat priority. `*:<priority>` shows that level *and everything
# above it*, so a severity set resolves to its lowest member.
PRIORITY_ORDER = ("V", "D", "I", "W", "E", "F")

# Severity names used by this skill's CLIs -> the logcat priority letters they
# cover. "error" covers F as well: a fatal is an error the agent wants to see.
SEVERITY_PRIORITIES = {
    "verbose": ("V",),
    "debug": ("D",),
    "info": ("I",),
    "warning": ("W",),
    "error": ("E", "F"),
}


def parse_duration(text: str) -> float:
    """Parse a duration string like ``30s``, ``5m`` or ``1h`` into seconds.

    Args:
        text: Duration string. Case-insensitive.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: If the string carries no recognisable ``<int><s|m|h>``.
    """
    match = _DURATION.match(text.lower().strip()) if text else None
    if not match:
        raise ValueError(f"Invalid duration format: {text!r}. Use format like '30s', '5m', '1h'.")
    value, unit = match.groups()
    return int(value) * _UNIT_SECONDS[unit]


def window_start(seconds: float, now: datetime | None = None) -> str:
    """Return the ``logcat -t`` timestamp for a window ``seconds`` wide.

    Args:
        seconds: Width of the historical window, ending at ``now``.
        now: Reference time; defaults to the current time. Injectable so tests
            can assert an exact timestamp rather than a moving one.

    Returns:
        A ``"MM-DD HH:MM:SS.000"`` string.
    """
    reference = now or datetime.now()
    return (reference - timedelta(seconds=seconds)).strftime(TIMESTAMP_FORMAT)


def window_start_for(duration: str, now: datetime | None = None) -> str:
    """``parse_duration`` then ``window_start``, for the common one-step case.

    Raises:
        ValueError: If ``duration`` is unparseable.
    """
    return window_start(parse_duration(duration), now=now)


def min_priority_for(severities: list[str] | None) -> str | None:
    """Resolve a severity filter to the single lowest logcat priority letter.

    ``*:W`` shows warnings *and* errors, so a filter of several severities
    collapses to its lowest member rather than to several filter terms.

    Args:
        severities: Severity names (``error``, ``warning``, ``info``,
            ``debug``, ``verbose``). Unknown names are ignored.

    Returns:
        One priority letter, or None when the filter selects nothing.
    """
    if not severities:
        return None
    letters = [
        letter for severity in severities for letter in SEVERITY_PRIORITIES.get(severity, ())
    ]
    if not letters:
        return None
    return PRIORITY_ORDER[min(PRIORITY_ORDER.index(letter) for letter in letters)]


def logcat_args(
    *,
    buffer_name: str | None = None,
    dump: bool = False,
    clear: bool = False,
    since: str | None = None,
    fmt: str | None = DEFAULT_FORMAT,
    pid: str | int | None = None,
    min_priority: str | None = None,
) -> list[str]:
    """Build the argument tail that follows ``adb [-s S] logcat``.

    Returned separately from the full command because half the callers run
    through ``run_adb("logcat", serial, *logcat_args(...))`` (bounded, typed
    errors) and half need a bare argv for a streaming ``Popen``.

    Args:
        buffer_name: ``-b`` buffer, e.g. ``crash``. Default (None) leaves the
            device's default buffer set.
        dump: Add ``-d`` -- print what is buffered and exit instead of
            following. Implied by ``since``, which is meaningless while
            following.
        clear: Add ``-c`` -- clear the buffer instead of reading it.
        since: A ``"MM-DD HH:MM:SS.mmm"`` timestamp for ``-t``. Note that
            ``-t N`` with a bare integer means the last N *lines*, not a
            duration; that confusion silently turned "one minute of logs" into
            "sixty lines" here once, hence the timestamp-only signature.
        fmt: ``-v`` format. Defaults to ``threadtime``; pass None to inherit
            the device's format (only safe when nothing parses the output).
        pid: Restrict to one process via ``--pid=``. Falsy values are omitted,
            so an unresolved PID widens the capture rather than filtering to
            nothing.
        min_priority: A single letter for the trailing ``*:<priority>`` term.

    Returns:
        The argument list, in the fixed order documented at module level.
    """
    args: list[str] = []
    if buffer_name:
        args.extend(["-b", buffer_name])
    if clear:
        args.append("-c")
        # A clear neither reads nor formats anything; the remaining options
        # would be accepted by adb and mean nothing.
        return args
    if dump or since is not None:
        args.append("-d")
    if since is not None:
        args.extend(["-t", since])
    if fmt:
        args.extend(["-v", fmt])
    if pid:
        args.append(f"--pid={pid}")
    if min_priority:
        args.append(f"*:{min_priority}")
    return args


def build_logcat_command(serial: str | None = None, **kwargs) -> list[str]:
    """Full ``adb`` argv for a logcat read: ``build_adb_command`` + ``logcat_args``.

    Args:
        serial: Device serial; ``-s`` is omitted when None.
        **kwargs: Passed straight to :func:`logcat_args`.

    Returns:
        Complete argv, ready for subprocess. Never a shell string.
    """
    return build_adb_command("logcat", serial, *logcat_args(**kwargs))
