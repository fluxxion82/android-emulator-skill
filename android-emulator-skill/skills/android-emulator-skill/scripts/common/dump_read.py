#!/usr/bin/env python3
"""A live logcat read made of dumps: the device side every reader on API 33 shares.

Android 13's logcat (API 33) writes ``-B`` records through a 4 KiB stdio
buffer and flushes only after a TEXT record, so a live ``logcat -B`` stopped by
its reader loses what it held back (``common.logcat``, ``read_after``; recorded
on the API 33 emulator: logcat_stream_*). A dump ends by itself and writes
everything it read, so on that level a live read is a series of dumps, each
continuing from a cursor. ``common.logcat`` is the pure half -- the cursor, the
count-based tails, the judgement of a dump's bytes. This module is the half
that touches the device, once for every reader of that level:
``log_monitor``'s live read, and ``anr_watcher``'s ``--watch`` and session
worker.

- :func:`holds_binary_back`: the level, asked once per read. An unknown level
  is refused -- guessed to stream, an API 33 device read quietly as zero lines.
- :class:`DumpRead`: one read's dumps -- the opening search (:meth:`DumpRead.open`,
  writing its own anchor when the buffer holds no system record), each poll's
  read after the cursor (:meth:`DumpRead.read`), and every dump's one ending
  (:meth:`DumpRead.dump`).
- :func:`poll`: the schedule, one read every ``POLL_SECONDS`` from the opening
  and once more at the end -- a deadline, an interrupt, a stop.

Failures are typed, never printed: :class:`DeviceGoneError` (the device went:
exit 3 at a CLI's boundary) and :class:`ReadRefusedError` -- :class:`PlaceLostError`
when the read cannot know where it is, :class:`NotRecordsError` when a dump is not
logd's records -- and :class:`ReadCancelledError`, no failure, for an opening
its caller stopped. Each reader turns them into its own answer, carrying what it
had read: ``log_monitor`` and ``--watch`` refuse with ``partial``; the session
worker ends the session with the reason.

What a read made of dumps is: what logd delivered. The cursor and the records
after it found in a dump prove logd still held the window's start when that
dump began; they do not prove the window complete -- logd may drop records it
prunes while any reader is behind, as with any logd reader on any level. A
cursor moves past what a read delivered, not past what its reader has
finished with: a reader that holds a record for later (anr_watcher's session
holds a WindowManager report for ActivityManager's) keeps it in memory, and
says how many it could not write if it ends itself holding them; killed, it
cannot, and their number is unknown.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from typing import NoReturn

from common import cli, logcat
from common.adb_exec import (
    DEFAULT_TIMEOUT,
    AdbError,
    AdbTimeoutError,
    DeviceError,
    DeviceOfflineError,
    classify_device_error,
    run_adb,
)
from common.device_utils import (
    UnknownSdkLevelError,
    build_adb_command,
    get_sdk_level,
    quote_for_device_shell,
)
from common.env_config import env_int

# Budget for `adb get-state`, asked after every dump and before any verdict on
# it, and for the level question and the anchor's write. It answers at once
# either way (measured: `device`, or `error: device '...' not found` exit 1);
# the bound is for a wedged server.
STATE_TIMEOUT = env_int("ANDROID_EMU_LOG_STATE_TIMEOUT", 15)
# The most any one dump may take: twice adb's per-call budget
# (ANDROID_EMU_ADB_TIMEOUT, 30 s), as log_monitor's `--last` dump. Measured on
# the API 35 emulator: its whole 12.9 MB buffer dumps as records in 0.2 s.
DUMP_TIMEOUT = 2 * DEFAULT_TIMEOUT
# A live read is a series of dumps, nominally one poll apart (:func:`poll`; a
# dump that takes longer than a poll is followed at once by a catch-up one).
# Its bounds, each separate:
# - each dump of the opening search for the newest system record:
#   NEWEST_TIMEOUT, and never more than the caller's own budget (a duration);
# - each dump's own deadline: DUMP_FLOOR seconds plus the seconds since the
#   last read (about one poll interval), never more than DUMP_TIMEOUT, the
#   same for every doubled tail within one read;
# - a dump still running when it ends -- its deadline, the byte cap, an output
#   closed early -- gets KILL_REAP_TIMEOUT more to be reaped: it runs in a
#   session of its own, so one left running would outlive the reader;
# - after each dump, whatever its outcome, and before any verdict on it:
#   `adb get-state`, bounded by STATE_TIMEOUT. A device error in adb's own
#   stderr is the answer before that, after the bounded `adb devices` its
#   message lists the attached devices from (common.adb_exec).
DUMP_FLOOR = 5
NEWEST_TIMEOUT = STATE_TIMEOUT
KILL_REAP_TIMEOUT = 2
# How often a dump-made read asks again, whatever its reader or mode: also how
# late an event logged just after a read can be seen (one poll, plus the dump).
POLL_SECONDS = env_int("ANDROID_EMU_LOG_POLL_SECONDS", 1)
# How often a running dump is checked against its deadline and the byte cap.
DUMP_WAIT_SECONDS = 0.05
# The output size past which a dump is stopped and refused, unread: the sum of a
# device's log buffers bounds a window, and 32 MiB is past the API 33
# emulator's four 2 MiB rings many times over (measured: `logcat -g`). A
# DETECTION threshold, not a maximum: it is checked every DUMP_WAIT_SECONDS, so
# the file can overshoot it by what adb writes in that time and before the kill
# lands. A read also stops doubling its tail before one of about twice the
# last dump's bytes would pass it (common.logcat.read_after). Raise it for a
# device whose rings are larger (`adb logcat -g`).
DUMP_CAP_BYTES = env_int("ANDROID_EMU_LOG_DUMP_CAP_BYTES", 32 * 1024 * 1024)
# The most of a dump's stderr that is read: its last 64 KiB, which holds adb's
# own final words, whatever the child wrote before them.
SAID_TAIL_BYTES = 64 * 1024

_GET_STATE_REMEDY = "Check adb and the device, then retry."


class ReadRefusedError(RuntimeError):
    """A dump-made read that cannot be reported as the window: its words and remedy.

    The plain class is a dump that failed with the device still there -- it
    did not finish, exited non-zero, passed the byte cap, could not be read or
    stopped, or `adb get-state` itself failed. A reader may try the read again
    from where it was (the session worker does). Its subclasses cannot be
    tried again from there: :class:`PlaceLostError`, :class:`NotRecordsError`.
    """

    def __init__(self, message: str, remedy: str) -> None:
        super().__init__(message)
        self.remedy = remedy


class PlaceLostError(ReadRefusedError):
    """The read cannot know where it is: its cursor turned over, is held twice, or is past the cap.

    Also the opening's: the marker it wrote is not found, or found twice.
    """


class NotRecordsError(ReadRefusedError):
    """A dump was not logd's records (or was cut inside one, the device still there)."""


class ReadCancelledError(RuntimeError):
    """A read's opening stopped before it opened: its caller asked it to (``stop``).

    Not a :class:`ReadRefusedError`: nothing failed, and nothing may be tried
    again. Only an opening given a ``stop`` raises it (:meth:`DumpRead.open`).
    """


class DeviceGoneError(RuntimeError):
    """The device went: adb's own words after a dump, or `adb get-state`, said so.

    Attributes:
        error: The device error, as adb_exec typed it (exit 3 at a CLI's boundary).
        note: The facts gathered about the dump that ended this way, for the
            reader's message.
        source: ``"logcat"`` when adb said it as the dump ended, ``"adb"``
            when adb said it when asked (`get-state`, or a write).
    """

    def __init__(self, error: DeviceError, note: str = "", *, source: str = "adb") -> None:
        super().__init__(f"{error}{note}")
        self.error = error
        self.note = note
        self.source = source


@functools.cache
def device_refusal_type(device_error: type[DeviceError]) -> type[cli.Failure]:
    """A refusal that is both a ``cli.Failure`` and ``device_error`` (exit 3, with ``details``).

    ``common.cli`` hands ``details`` over only for a ``cli.Failure`` and exits 3
    only for a device-unavailable error; a reader's refusal for a device gone
    mid-read is both, so what it had read reaches a JSON caller (``partial``).
    """
    return type(
        f"PartialCapture{device_error.__name__}",
        (cli.Failure, device_error),
        {"__doc__": f"A dump-made read refused because of a {device_error.__name__}."},
    )


@contextlib.contextmanager
def _sigint_held():
    """Hold SIGINT off while the block runs, where the platform can (POSIX ``pthread_sigmask``).

    A Ctrl-C meanwhile is delivered when the block ends; without
    ``pthread_sigmask`` (Windows) the block runs unshielded.
    """
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        yield
        return
    held = mask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        mask(signal.SIG_SETMASK, held)


def _tail(file, limit: int) -> str:
    """The last ``limit`` bytes of an open binary ``file``, decoded: never more, however large."""
    size = os.fstat(file.fileno()).st_size
    file.seek(max(0, size - limit))
    return file.read(limit).decode("utf-8", errors="replace")


def verify_device(serial: str | None, *, timeout: float = STATE_TIMEOUT) -> None:
    """Ask adb whether the device a read targets is there, before trusting a read.

    A streaming ``adb logcat`` aimed at a device that is not attached does not
    fail: it prints ``- waiting for device -`` and waits (recorded:
    adb_waiting_for_device), and a dump does the same. ``adb get-state``
    answers at once (bounded by ``timeout``: ``STATE_TIMEOUT``, or less of a
    read's own bound).

    Raises:
        DeviceNotFoundError, DeviceOfflineError, DeviceUnauthorizedError:
            Typed from adb's own words by ``run_adb`` -- exit 3 at a CLI's
            boundary (recorded: adb_device_not_found). A state other than
            ``device`` (recovery, bootloader) is offline for logcat's purposes.
        MultipleDevicesError: Several attached and no serial (exit 1).
        AdbCommandError: get-state failed in a way adb did not name.
    """
    result = run_adb("get-state", serial, check=True, timeout=timeout)
    state = result.stdout.strip()
    if state != "device":
        target = f"Device {serial!r}" if serial else "The device"
        raise DeviceOfflineError(
            f"{target} is in state {state or 'unknown'!r}, not `device`; logcat cannot "
            f"read it.",
            remedy="Wait for it to boot into Android (`adb wait-for-device`), then retry.",
        )


def require_device(serial: str | None, note: str = "", *, deadline: float | None = None) -> None:
    """``get-state`` for a dump-made read, every failure of it typed.

    Gone is :class:`DeviceGoneError`; any other failure of the question itself
    -- it timed out, adb failed -- is a :class:`ReadRefusedError`. The one place a
    dump-made read asks, so no caller can ask it unwrapped.

    With ``deadline`` (``time.monotonic()``: an opening's bound) the question
    gets no more than what is left of it, and none is asked once nothing is:
    that is a :class:`ReadRefusedError` too, since what was not asked cannot
    vouch for the read.
    """
    timeout: float = STATE_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise ReadRefusedError(
                f"`adb get-state` was not asked: the read's bound had passed, so whether the "
                f"device is still there is not known, and what was read is not reported.{note}",
                "Check the device answers adb promptly (`adb get-state`), then retry.",
            )
    try:
        verify_device(serial, timeout=timeout)
    except DeviceError as error:
        raise DeviceGoneError(error, note) from error
    except AdbError as error:
        raise ReadRefusedError(
            f"`adb get-state` failed after a dump ({error}), so the dump is not reported "
            f"as the window.{note}",
            getattr(error, "remedy", None) or _GET_STATE_REMEDY,
        ) from error


def holds_binary_back(
    serial: str | None,
    reader: str,
    *,
    timeout: float = STATE_TIMEOUT,
    deadline: float | None = None,
) -> bool:
    """Whether the device's logcat is Android 13's, which holds ``-B`` records back.

    ``getprop ro.build.version.sdk``, asked once per read (recorded:
    getprop_sdk). A device that does not say -- getprop failing, or answering
    an empty line as it does for a property it lacks (getprop_unset) -- is
    refused: guessed to stream, an API 33 device read quietly as zero lines,
    the very success a dump-made read exists to prevent.

    Args:
        serial: The device.
        reader: The script asking, for the refusal's words.
        timeout: The bound on getprop (``STATE_TIMEOUT``, or less of an
            opening's own budget).
        deadline: The opening's bound (``time.monotonic()``), if it has one:
            the `get-state` asked after a getprop that timed out gets only what
            is left of it (:func:`require_device`).

    Raises:
        ReadRefusedError: The level is unknown, or getprop did not answer in
            ``STATE_TIMEOUT`` and the device is still there.
        DeviceGoneError: getprop timed out and ``get-state`` then says the
            device went.
        adb_exec.DeviceError: As getprop raised it (exit 3 at a CLI's boundary).
    """
    try:
        level = get_sdk_level(serial, timeout=timeout)
    except AdbTimeoutError as error:
        require_device(serial, note=f" ({error})", deadline=deadline)
        raise ReadRefusedError(str(error), error.remedy) from error
    except UnknownSdkLevelError as error:
        raise ReadRefusedError(
            f"{error} {reader} reads Android 13's logcat differently from every other "
            "release's (it holds records back while it streams), so it does not read a "
            "device whose level it cannot tell.",
            error.remedy,
        ) from error
    return level == logcat.BUFFERED_BINARY_SDK


# The kernel's id for the boot the device is in: a random UUID made at boot
# (recorded: boot_id, on both levels). `ro.boot.bootreason` is no id: it names
# why the device last booted (`reboot,shell`), the same words every time.
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def parse_boot_id(text: str) -> str | None:
    """The boot id ``cat /proc/sys/kernel/random/boot_id`` answered; None for anything else."""
    value = text.strip()
    return value if _BOOT_ID.fullmatch(value) else None


def boot_id(serial: str | None, *, timeout: float = STATE_TIMEOUT) -> str:
    """The boot the device is in now (``BOOT_ID_PATH``), bounded by ``STATE_TIMEOUT``.

    A read's place is in one boot's log: a session ties its persisted cursor to
    this, so a read resumed after a reboot is refused rather than looked for
    in another boot's log.

    Raises:
        DeviceGoneError: The device went.
        ReadRefusedError: adb failed, or the answer is no boot id.
    """
    try:
        result = run_adb("shell", serial, "cat", BOOT_ID_PATH, check=True, timeout=timeout)
    except DeviceError as error:
        raise DeviceGoneError(error) from error
    except AdbError as error:
        raise ReadRefusedError(
            f"`adb shell cat {BOOT_ID_PATH}` failed ({error}), so the boot the read is in "
            "cannot be told.",
            getattr(error, "remedy", None) or _GET_STATE_REMEDY,
        ) from error
    value = parse_boot_id(result.stdout)
    if value is None:
        raise ReadRefusedError(
            f"`adb shell cat {BOOT_ID_PATH}` answered {result.stdout.strip()[:80]!r}, which is "
            "no boot id, so the boot the read is in cannot be told.",
            "Check the device has finished booting, then retry.",
        )
    return value


class DumpRead:
    """One live read made of dumps: where it opens, what each poll reads, how each dump ends.

    Attributes:
        serial: The device.
        tail: The tail the next read asks for first (``common.logcat.read_after``)
            -- twice the records the last read found, never under ``INITIAL_TAIL``.
        anchor_written: Whether the opening wrote its own marker line
            (:meth:`write_anchor`).
    """

    def __init__(self, serial: str | None) -> None:
        self.serial = serial
        self.tail = logcat.INITIAL_TAIL
        self.anchor_written = False

    def open(
        self,
        budget: float,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> logcat.Cursor:
        """The read's opening cursor (``common.logcat.open_cursor``), each dump within ``budget``.

        With ``deadline`` (a ``time.monotonic()`` value) the whole search is
        kept within it too: each dump, the anchor's write and every
        `get-state` after them gets no more than what is left, and none starts
        once nothing is. With ``stop``, a stop asked meanwhile ends the
        search: a dump under way is stopped and reaped, and
        :class:`ReadCancelledError` raised.

        ``-d -B -T 1``, doubled until it holds a record a system process wrote;
        what followed that record was logged before the read opened. A buffer
        with no such record (empty, as after ``--clear``, or only apps') gets
        one: :meth:`write_anchor`, then found by its nonce
        (``common.logcat.find_anchor``).

        Raises:
            PlaceLostError: The search would pass the byte cap, or the anchor is not
                found once.
            NotRecordsError: A dump is not records (asked of the device first when
                it was cut).
            ReadRefusedError, DeviceGoneError: A dump failed (:meth:`dump`).
            ReadCancelledError: ``stop`` was set.
        """

        def left() -> float:
            if deadline is None:
                return budget
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadRefusedError(
                    "the opening search for the newest record a system process wrote ran past "
                    "its bound, so where the read starts cannot be told.",
                    "Check the device answers `adb logcat -d -B -T 1` promptly, then retry.",
                )
            return min(budget, remaining)

        def go_on() -> None:
            if stop is not None and stop.is_set():
                raise ReadCancelledError("the read was asked to stop before it opened.")

        def dump(args: list[str]) -> bytes:
            go_on()
            return self.dump(args, left(), deadline=deadline, stop=stop)

        try:
            cursor = logcat.open_cursor(dump, max_bytes=DUMP_CAP_BYTES)
            if cursor is None:
                go_on()
                message = self.write_anchor(timeout=min(STATE_TIMEOUT, left()))
                cursor = logcat.find_anchor(dump, message, max_bytes=DUMP_CAP_BYTES)
            return cursor
        except (
            logcat.CursorBeyondCapError,
            logcat.AnchorNotFoundError,
            logcat.AmbiguousCursorError,
        ) as error:
            raise PlaceLostError(str(error), error.remedy) from error
        except logcat.LogFramingError as error:
            if error.truncated:
                require_device(self.serial, deadline=deadline)
            raise NotRecordsError(
                f"`adb logcat {' '.join(logcat.newest_args())}` (or a longer tail of it) did not "
                f"answer whole records ({error}).",
                logcat.LogFramingError.remedy,
            ) from error

    def write_anchor(self, *, timeout: float = STATE_TIMEOUT) -> str:
        """Write one marker line to the device's log, the read's anchor; return its message.

        ``adb shell log -t aes-log-monitor anchor-<128-bit hex>``: written as
        shell (uid 2000), a system writer no app can impersonate, with a nonce
        no one else has, so the frame is unique. Only when the buffer holds no
        system record to open on -- mostly after ``--clear``. Said on stderr;
        each reader says it in its own output too.

        Raises:
            ReadRefusedError: The write failed or did not answer within
                ``STATE_TIMEOUT``.
            DeviceGoneError: The write's device went.
        """
        message = f"anchor-{secrets.token_hex(16)}"
        try:
            run_adb(
                "shell",
                self.serial,
                "log",
                "-t",
                quote_for_device_shell(logcat.ANCHOR_TAG),
                quote_for_device_shell(message),
                check=True,
                timeout=timeout,
            )
        except DeviceError as error:
            raise DeviceGoneError(error) from error
        except AdbError as error:
            raise ReadRefusedError(
                f"The log held no record a system process wrote to start from, and `adb shell "
                f"log` could not write one ({error}).",
                getattr(error, "remedy", None) or _GET_STATE_REMEDY,
            ) from error
        self.anchor_written = True
        print(
            f"The log held no record a system process wrote to start from: wrote one marker "
            f"line ({logcat.ANCHOR_TAG}: {message}).",
            file=sys.stderr,
        )
        return message

    def read(
        self, cursor: logcat.Cursor, last_read: float
    ) -> tuple[list[logcat.LogRecord], logcat.Cursor, float]:
        """What logd delivered after ``cursor``; the next cursor; when this read began.

        ``common.logcat.read_after`` finds the cursor in a tail of logd's
        order: first the larger of ``INITIAL_TAIL`` and twice the records the
        previous read took, doubled while it does not reach the cursor. Each
        dump's budget is ``DUMP_FLOOR`` plus the seconds since ``last_read``,
        never more than ``DUMP_TIMEOUT``. A dump reaches ``read_after``'s
        judgement only after the device has answered (:meth:`dump`), so a
        vanished device is :class:`DeviceGoneError`, never a cursor "lost" to a
        buffer that turned over.

        Raises:
            PlaceLostError: The cursor's record is not in the whole buffer, is in a
                tail more than once, a record read after it is gone, or the
                tail that would hold it passes the byte cap.
            NotRecordsError: A dump was not whole records.
            ReadRefusedError, DeviceGoneError: A dump failed (:meth:`dump`).
        """
        now = time.monotonic()
        budget = min(DUMP_TIMEOUT, DUMP_FLOOR + (now - last_read))
        try:
            records, cursor = logcat.read_after(
                cursor,
                lambda args: self.dump(args, budget),
                tail=self.tail,
                max_bytes=DUMP_CAP_BYTES,
            )
        except (
            logcat.CursorLostError,
            logcat.AmbiguousCursorError,
            logcat.CursorBeyondCapError,
        ) as error:
            raise PlaceLostError(str(error), error.remedy) from error
        except logcat.LogFramingError as error:
            if error.truncated:
                require_device(self.serial)
            raise NotRecordsError(
                f"`adb logcat` did not answer whole records ({error}), so what it read is not "
                "the window.",
                logcat.LogFramingError.remedy,
            ) from error
        # The next tail must reach the cursor past its suffix, then about as
        # many new records as this read took, twice over.
        self.tail = max(logcat.INITIAL_TAIL, 1 + len(cursor.suffix) + 2 * len(records))
        return records, cursor, now

    def dump(
        self,
        args: list[str],
        timeout: float,
        *,
        deadline: float | None = None,
        stop: threading.Event | None = None,
    ) -> bytes:
        """Run ``adb logcat <args>`` -- a dump, which ends by itself -- and return what it wrote.

        Its output and stderr go to temporary files, and this thread waits
        for it to exit in ``DUMP_WAIT_SECONDS`` steps, the deadline checked
        first, then its exit, then the output's size against
        ``DUMP_CAP_BYTES``: no pipe to block on, no ``select`` (which takes no
        pipe on Windows), no timer. Its own session, so a Ctrl-C meant for the
        read does not cut the dump that ends it.

        However the wait ends, the same steps follow, in this order, none
        skipped:

        1. Stop: still running (its deadline, the cap, an output closed early,
           the wait itself failing) -> killed, then reaped within
           ``KILL_REAP_TIMEOUT``, with SIGINT held off meanwhile where the
           platform can (``_sigint_held``). Whatever raises between the start
           and here, the stop still runs: an ``OSError`` of the wait is this
           dump's outcome (the wait failed); anything else (Ctrl-C) is raised
           again once the child is stopped -- or, if it could not be, once
           that is said on stderr, still under the mask.
        2. Read bounded: stderr's last ``SAID_TAIL_BYTES``; the output only for
           a dump that exited 0 in time under the cap. A read that fails is a
           fact of the ending, not an exception out of it.
        3. adb's own words: a device error in that stderr, whatever the exit
           status -> :class:`DeviceGoneError`. Skipped, and said, when stderr
           could not be read.
        4. ``get-state`` (:func:`require_device`): gone ->
           :class:`DeviceGoneError`; any other failure of it -> :class:`ReadRefusedError`.
           Within ``deadline``, when the dump has one (an opening's): not
           asked once it has passed, which refuses the dump.
        5. Verdict: could not be stopped, the wait failed, stderr unreadable,
           cap exceeded, deadline passed (by the time its exit was seen), a
           non-zero exit, output unreadable -> :class:`ReadRefusedError`, with every
           fact gathered; otherwise its output is the dump's.

        A ``stop`` set while it runs ends the wait, checked before anything
        else: the dump is stopped and reaped (step 1), and
        :class:`ReadCancelledError` raised -- the device is not asked, and
        nothing is read.
        """
        command = build_adb_command("logcat", self.serial, *args)
        shown = " ".join(command[command.index("logcat") :])
        ends = time.monotonic() + timeout
        returncode: int | None = None
        exited_in_time = capped = cancelled = False
        wait_failed: OSError | None = None
        said_failed: OSError | None = None
        out_failed: OSError | None = None
        said = ""
        data = b""
        with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as said_file:
            process = subprocess.Popen(
                command,
                stdout=out_file,
                stderr=said_file,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                while True:
                    if stop is not None and stop.is_set():
                        cancelled = True
                        break
                    left = ends - time.monotonic()
                    if left <= 0:
                        break
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        returncode = process.wait(timeout=min(DUMP_WAIT_SECONDS, left))
                    exited_in_time = returncode is not None and time.monotonic() <= ends
                    if os.fstat(out_file.fileno()).st_size > DUMP_CAP_BYTES:
                        capped = True
                        break
                    if returncode is not None:
                        break
            except OSError as error:
                wait_failed = error
            except BaseException:
                # 1. Stop, on the way out of an interruption -- and say so,
                # still with SIGINT held, if it failed: a second Ctrl-C would
                # otherwise arrive first and the line would never be printed.
                with _sigint_held():
                    stopped, gone = self._stop(process)
                    if not gone:
                        print(
                            f"`adb {shown}` could not be stopped (pid {process.pid}: {stopped}).",
                            file=sys.stderr,
                        )
                raise
            # 1. Stop.
            with _sigint_held():
                stopped, gone = self._stop(process)
            if cancelled:
                raise ReadCancelledError(
                    f"`adb {shown}` was stopped as it ran ({stopped}): the read was asked to "
                    "stop before it opened."
                )
            # 2. Read bounded; the output only for a dump that can be accepted.
            try:
                said = _tail(said_file, SAID_TAIL_BYTES)
            except OSError as error:
                said_failed = error
            if exited_in_time and returncode == 0 and not capped and wait_failed is None:
                try:
                    if os.fstat(out_file.fileno()).st_size > DUMP_CAP_BYTES:
                        capped = True
                    else:
                        out_file.seek(0)
                        data = out_file.read()
                except OSError as error:
                    out_failed = error
        detail = " ".join(said.split())[-300:]
        facts = "" if gone else f" `adb {shown}` {stopped}."
        if wait_failed is not None:
            facts += f" Waiting for `adb {shown}` failed ({wait_failed})."
        if said_failed is not None:
            facts += (
                f" Its stderr could not be read ({said_failed}), so adb's own words were not "
                "checked."
            )
        if out_failed is not None:
            facts += f" It exited 0, but its output could not be read ({out_failed})."
        # 3. adb's own words, whatever the exit status -- when there are words.
        if said_failed is None:
            device_error = classify_device_error(said, self.serial)
            if device_error is not None:
                raise DeviceGoneError(device_error, facts, source="logcat")
        # 4. The device, whatever the outcome -- within the read's bound, when
        # it has one (an opening's): once that has passed it is not asked, and
        # the verdict says so; a dump the device was not asked after is never
        # the window.
        unasked = ""
        if deadline is None or time.monotonic() < deadline:
            require_device(self.serial, note=facts, deadline=deadline)
        else:
            unasked = " The device was not asked after it: the read's bound had passed."
        # 5. The verdict.
        retry = "Retry; if it repeats, restart adb (`adb kill-server && adb start-server`)."
        if facts:
            raise ReadRefusedError(
                facts.strip() + " What it read is not reported as the window." + unasked, retry
            )
        if capped:
            raise ReadRefusedError(
                f"`adb {shown}` wrote more than {DUMP_CAP_BYTES} bytes and {stopped}; it is not "
                "read, and not reported as the window." + unasked,
                "Capture for less time, or raise ANDROID_EMU_LOG_DUMP_CAP_BYTES.",
            )
        if not exited_in_time:
            raise ReadRefusedError(
                f"`adb {shown}` did not finish within {timeout:g}s and {stopped}, so what it "
                "read is not reported as the window."
                + (f" adb said: {detail}" if detail else "")
                + unasked,
                retry,
            )
        if returncode:
            raise ReadRefusedError(
                f"`adb {shown}` exited with status {returncode}."
                + (f" adb said: {detail}" if detail else "")
                + unasked,
                "Check the device is connected (`adb devices`) and retry.",
            )
        if unasked:
            raise ReadRefusedError(
                f"`adb {shown}` answered, but the read's bound passed before the device could be "
                "asked whether it is still there, so what it read is not reported as the window.",
                retry,
            )
        return data

    @staticmethod
    def _stop(process: subprocess.Popen) -> tuple[str, bool]:
        """Stop a dump that is still running; how it ended, and whether it is gone.

        Never raises an ``OSError``: a failed ``poll``, ``kill`` or reap is
        part of the answer.

        Returns:
            ``("exited (exit N)", True)`` by itself; ``("was killed (exit N)",
            True)``, the status the reap returned; or ``("could not be stopped
            ...", False)`` -- the kill failed, the reap failed, or it was still
            running after the kill.
        """
        with contextlib.suppress(OSError):
            status = process.poll()
            if status is not None:
                return f"exited (exit {status})", True
        try:
            process.kill()
        except OSError as error:
            return f"could not be stopped ({error})", False
        try:
            return f"was killed (exit {process.wait(timeout=KILL_REAP_TIMEOUT)})", True
        except subprocess.TimeoutExpired:
            return "could not be stopped: still running after it was killed", False
        except OSError as error:
            return f"could not be stopped: its reap failed ({error})", False


def poll(
    reader: DumpRead,
    cursor: logcat.Cursor,
    *,
    stop: threading.Event,
    deadline: float | None,
    on_read: Callable[[list[logcat.LogRecord], logcat.Cursor], None],
) -> logcat.Cursor:
    """Read every ``POLL_SECONDS`` until ``stop`` or ``deadline``, then once more; the last cursor.

    Polls on a fixed schedule from the call, so a slow dump does not push
    every later one back: a read that takes longer than a poll is followed at
    once by a catch-up one. The last read runs whatever ended the schedule --
    the deadline, an interrupt, a stop -- so what was logged since the read
    before it is read too: a reader stopped then loses nothing logd still
    held. ``on_read`` is given each read's records and the cursor after them,
    in order.

    Raises:
        Whatever :meth:`DumpRead.read` or ``on_read`` raises, at once: no read
        follows a failed one.
    """
    opened = last_read = time.monotonic()
    polls = 0
    while not stop.is_set():
        polls += 1
        due = opened + polls * POLL_SECONDS
        if deadline is not None and due >= deadline:
            due = deadline
        if stop.wait(max(0.0, due - time.monotonic())):
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        records, cursor, last_read = reader.read(cursor, last_read)
        on_read(records, cursor)
    records, cursor, last_read = reader.read(cursor, last_read)
    on_read(records, cursor)
    return cursor


def refuse(error: ReadRefusedError | DeviceGoneError, said: str, details: dict) -> NoReturn:
    """A reader's refusal for a dump-made read's failure: exit 3 for a device gone, else 1.

    Args:
        error: The failure.
        said: What the reader adds to its words: how much it had read.
        details: What it had read, for a JSON caller (``partial``).
    """
    if isinstance(error, DeviceGoneError):
        raise device_refusal_type(type(error.error))(
            f"{error}{said}",
            getattr(error.error, "remedy", None) or "Reconnect the device and retry.",
            details,
        ) from error.error
    cli.fail(f"{error}{said}", error.remedy, details=details)
