#!/usr/bin/env python3
"""The one place a script's failure becomes an exit status and a message.

Why this exists
---------------
Every script used to own its own error boundary, and 32 copies of one idea
drifted into four shapes: ``Error: ...`` on stderr with an empty stdout under
``--json``, ``{"error": ...}``, ``{"success": false, "message": ...}``, and a
bare traceback where nobody caught anything. "Fixed in one file, alive in
another" was the dominant defect class in two consecutive retrospectives, and
this was its generator. :mod:`common.adb_exec` made an unbounded adb call
impossible by giving every call one place to go; this does the same for a
failure. ``tests/test_cli_boundary.py`` holds every script to it.

The contract
------------
Every script ends ``if __name__ == "__main__": cli.run(main)``. :func:`run`
calls ``main(argv)`` and exits with what it returns. When ``main`` raises one of
the typed family -- the failures this skill expects, each with a message that
names what went wrong -- or calls :func:`fail`, it is reported in one shape:

- ``--json`` requested: one line on stdout,
  ``{"success": false, "error": <str>, "remedy": <str>}``.
- otherwise, two lines on stderr: ``Error: <str>`` then ``Remedy: <str>``.

``error`` is the whole message, as every other caller sees it; ``remedy`` is
what to do. Many messages already end in their remedy, so the remedy can appear
in both -- accepted, because cutting it out of ``error`` is presentation logic
that can empty the field or disagree with the message.

A refusal that found something on the way -- the screens a search covered, the
entries a listing could read -- may carry it: ``fail(error, remedy,
details={...})`` adds those keys to the JSON object after the three above,
which they can never replace. The text form stays two lines; what a detail
says that matters belongs in ``error`` as well.

**What stdout holds under --json.** For a non-streaming mode (almost all of
them) the refusal object is the whole of stdout; a question for the user, such
as ``emulator_delete``'s confirmation prompt, goes to stderr. A streaming mode --
``log_monitor --follow``, ``anr_watcher --watch`` -- may already have printed
records when the failure arrives, and nothing can retract them; there the
contract is that the refusal object is the *last line* of stdout and parses on
its own. That is why it is one line rather than indented.

Exit status
-----------
``1`` failure. ``3`` the device is unavailable: not found, offline or
unauthorized, whether device resolution noticed or adb did mid-run. (Several
devices and no ``--serial`` is ``1``: nothing is unavailable, the caller has to
choose.) ``130`` interrupted. Everything a script exits with itself passes
through untouched -- argparse's ``2`` for a usage error, and the statuses
that mean a *finding*: ``crash_triage --fail-on-crash`` and
``localization_audit --strict`` exit ``2`` when they find something, and
``accessibility_audit`` exits ``1`` when a critical issue is present. So ``2``
is not always a usage error, and ``1`` is not always a failure.

Where a device error deliberately does NOT reach this boundary
----------------------------------------------------------------
A single-target operation lets ``adb_exec.DeviceError`` through; a batch (an
``--all`` loop) decides at the loop whether to go on. These six catch it on
purpose and are the only ones that may:

- ``emulator_boot.EmulatorBooter._is_boot_completed`` -- polls a booting
  emulator, for which "offline" and "not found yet" are the expected states.
- ``snapshot.SnapshotManager._await_listing`` -- the save/delete verify poll.
  An emulator busy writing or dropping a snapshot can drop off adb for a
  moment, so a device error means "ask again" until the deadline; still
  unavailable then, the last one is raised and this boundary says exit 3.
- ``common.device_utils.get_current_activity`` (unless ``strict=True``) -- an
  optional label; screen mapping and recording go on without it.
- ``common.emu_console.identify_emulator`` / ``survey_emulators`` /
  ``console_available`` -- survey predicates: an emulator that will not answer
  is a fact about the survey (an unknown probe, an unavailable listing), not a
  failure of it.
- ``test_recorder.TestRecorder._capture_screenshot`` / ``_capture_hierarchy``
  -- a step records what it could capture, and says what it could not.
- ``app_state_capture.AppStateCapture.capture`` -- X8's partial capture: what
  arrived before a component failed is kept and handed over.

Known limitation: ``--json`` is detected by scanning argv, not by parsing. It
has to be -- the failure may have happened *during* parsing, or before the
script got that far. The scan stops at ``--`` (after it, ``--json`` is a
positional value, e.g. a file name), and a script with an alias for ``--json``
names it (``cli.run(main, json_flags=("--json", "--details"))``). An argparse
abbreviation such as ``--js`` is not recognised and gets the text form.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from typing import NoReturn

from . import adb_exec
from .hierarchy import HierarchyError
from .sdk_tools import SdkToolError

EXIT_FAILURE = 1
EXIT_DEVICE_UNAVAILABLE = 3
EXIT_INTERRUPTED = 130

JSON_FLAGS: tuple[str, ...] = ("--json",)


class Failure(Exception):  # noqa: N818 -- reads as `cli.Failure` at the call site
    """A failure a script detected itself, raised by :func:`fail`.

    Deliberately not a ``RuntimeError``: a script's own ``except RuntimeError``
    around a library call must not swallow it on its way to :func:`run`.

    Attributes:
        remedy: What the caller should do about it.
        details: Extra keys for the ``--json`` object (see :func:`fail`).
    """

    def __init__(self, error: str, remedy: str, details: Mapping[str, object] | None = None):
        super().__init__(error)
        self.remedy = remedy
        self.details = dict(details or {})


def fail(error: str, remedy: str, *, details: Mapping[str, object] | None = None) -> NoReturn:
    """Raise a failure the script detected itself, for :func:`run` to report (exit 1).

    For the shape this skill keeps finding: a script notices the lookup failed,
    prints an empty result, and exits 0. Raise instead, and :func:`run` reports
    it -- JSON on stdout under ``--json``, two lines on stderr otherwise. This
    function neither prints nor exits; it raises :class:`Failure`.

    Args:
        error: What went wrong, as a sentence.
        remedy: What the caller should do next.
        details: What the failed operation found before it failed, for a JSON
            caller: added to the refusal object after ``success`` / ``error`` /
            ``remedy``, none of which a detail can replace. Not printed in the
            text form, so nothing an agent needs may live only here.

    Example:
        >>> if not packages and result.returncode != 0:
        ...     cli.fail(
        ...         f"`pm list packages` exited {result.returncode}",
        ...         "Check the device is booted (`adb shell getprop sys.boot_completed`).",
        ...     )
    """
    raise Failure(error, remedy, details)


# What the skill expects to fail with. Everything raised under RuntimeError
# counts -- AdbError, HierarchyError, SdkToolError and the scripts' own errors all
# subclass it deliberately -- as does an OSError from a tool that will not run.
TYPED_FAMILY: tuple[type[BaseException], ...] = (
    Failure,
    RuntimeError,
    OSError,
    subprocess.TimeoutExpired,
)

# RuntimeErrors that are never an expected failure: something in the skill is
# wrong, and saying so -- with the traceback -- is the honest report.
_DEFECTS: tuple[type[BaseException], ...] = (NotImplementedError, RecursionError)

# The device the command needed is not available. A separate exit status
# because the agent's next move differs: boot, reconnect or re-select a device,
# rather than change the command.
DEVICE_UNAVAILABLE: tuple[type[BaseException], ...] = (
    adb_exec.DeviceNotFoundError,
    adb_exec.DeviceOfflineError,
    adb_exec.DeviceUnauthorizedError,
)

DEFECT_REMEDY = "this is a defect in the skill; report the command line"

# Used only when an error carries no ``remedy`` of its own, looked up along the
# error's MRO so the most specific entry wins.
DEFAULT_REMEDIES: dict[type[BaseException], str] = {
    adb_exec.DeviceError: (
        "Run `adb devices` to see what is attached, then pass --serial with a device "
        "in state `device`."
    ),
    adb_exec.AdbCommandError: (
        "adb reached the device and the command failed there; the error quotes the "
        "device's answer. Fix what it names and retry."
    ),
    adb_exec.AdbTimeoutError: (
        "Retry; if it repeats, reset the adb connection with "
        "`adb kill-server && adb start-server`."
    ),
    adb_exec.AdbError: "Check that `adb devices` lists the target in state `device`, then retry.",
    HierarchyError: "Retry once the screen has settled; check an app is in the foreground.",
    SdkToolError: "Check the Android SDK installation ($ANDROID_HOME) and retry.",
    subprocess.TimeoutExpired: (
        "The tool did not answer in time. Check the device responds (`adb devices`) and retry."
    ),
    OSError: "A file or tool the command needs could not be used; check the path in the error.",
    RuntimeError: "Fix the cause the error names, then re-run the same command.",
}


def _one_line(text: str) -> str:
    """Fold a message onto one line, so the text form really is two lines."""
    return " ".join(text.split())


def wants_json(argv: Sequence[str], flags: Sequence[str] = JSON_FLAGS) -> bool:
    """Whether argv asks for JSON, scanning only up to ``--``.

    Args:
        argv: The argument list, without the program name.
        flags: The spellings that mean "answer in JSON" for this script.
    """
    for arg in argv:
        if arg == "--":
            return False
        if arg in flags:
            return True
    return False


def describe(error: BaseException) -> tuple[str, str]:
    """What went wrong, and what to do about it.

    Args:
        error: The exception ``main`` raised.

    Returns:
        ``(error, remedy)``, both single-line. ``error`` is the whole message;
        ``remedy`` is the error's own when it carries one, else the default for
        the nearest class in its MRO.
    """
    message = _one_line(str(error)) or type(error).__name__
    remedy = getattr(error, "remedy", None)
    if isinstance(remedy, str) and remedy.strip():
        remedy = _one_line(remedy)
        # A remedy that is the tail of its sentence ("...wedged; try `adb
        # kill-server` and retry.") starts lower-case; as a field it starts one.
        return message, remedy[:1].upper() + remedy[1:]
    for cls in type(error).__mro__:
        if cls in DEFAULT_REMEDIES:
            return message, DEFAULT_REMEDIES[cls]
    return message, DEFAULT_REMEDIES[RuntimeError]


def report(
    error: str,
    remedy: str,
    *,
    json_mode: bool,
    details: Mapping[str, object] | None = None,
) -> None:
    """Emit the one failure shape.

    Args:
        error: What went wrong, one line.
        remedy: What to do about it, one line.
        json_mode: Write the JSON object to stdout instead of two lines to stderr.
        details: Extra keys for the JSON object; never one of the three above.
    """
    try:
        if json_mode:
            payload: dict[str, object] = {"success": False, "error": error, "remedy": remedy}
            for key, value in (details or {}).items():
                payload.setdefault(key, value)
            # default=str: a detail that is not JSON must not turn the refusal
            # into a TypeError traceback with nothing on stdout.
            print(json.dumps(payload, default=str))
        else:
            print(f"Error: {error}", file=sys.stderr)
            print(f"Remedy: {remedy}", file=sys.stderr)
        sys.stdout.flush()
    except BrokenPipeError:
        _neutralize_stdout()
    except (OSError, ValueError):
        # The reader has gone: a broken pipe (OSError), or a stream that is
        # already closed (ValueError, "I/O operation on closed file"). There is
        # nobody left to tell, and raising here would replace the exit status
        # with a traceback; the caller still exits with the status.
        pass


def _neutralize_stdout() -> None:
    """Point stdout's file descriptor at the null device after a broken pipe.

    Catching ``BrokenPipeError`` is not enough: the buffered stream retries its
    failed flush during interpreter finalization and replaces the intended exit
    status with 120. Replacing the descriptor lets that final flush complete.
    """
    try:
        stdout_fd = sys.stdout.fileno()
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, stdout_fd)
        finally:
            os.close(null_fd)
    except (AttributeError, OSError, ValueError):
        pass


def exit_code_for(error: BaseException) -> int:
    """The exit status for a typed failure: 3 when the device is unavailable, else 1."""
    return EXIT_DEVICE_UNAVAILABLE if isinstance(error, DEVICE_UNAVAILABLE) else EXIT_FAILURE


def run(
    main: Callable[[list[str] | None], int | None],
    argv: list[str] | None = None,
    *,
    json_flags: Sequence[str] = JSON_FLAGS,
) -> NoReturn:
    """Run a script's ``main`` and turn its outcome into the process exit.

    ``SystemExit`` is not caught: argparse's exit 2 and every script's own
    ``sys.exit`` on its success and ordinary-failure paths pass straight through.

    An exception outside the typed family is a bug in the skill. It is still
    reported in the same shape -- an agent parsing stdout must not get a
    traceback where it expects JSON -- but the traceback is *also* written to
    stderr. It is kept rather than hidden because it is the only evidence of
    where the defect is: dropping it would turn a bug into an unexplained
    failure that nobody can fix, and stderr is the stream nothing parses.

    Args:
        main: The script's entry point. Takes the argument list (without the
            program name) and returns an exit status, or None for 0.
        argv: Arguments to hand to ``main``. Defaults to ``sys.argv[1:]``.
        json_flags: The spellings of "answer in JSON" for this script, when it
            has an alias for ``--json``.

    Raises:
        SystemExit: Always -- this function ends the process.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = wants_json(args, json_flags)
    try:
        sys.exit(main(args))
    except KeyboardInterrupt:
        with contextlib.suppress(OSError, ValueError):
            print("Interrupted.", file=sys.stderr)
        sys.exit(EXIT_INTERRUPTED)
    except _DEFECTS as error:
        _report_defect(error, json_mode=json_mode)
    except TYPED_FAMILY as error:
        try:
            what, remedy = describe(error)
        except Exception as broken:
            # str(error), or a `remedy` property, raised while being read. That
            # is a defect in the error class, and it must not escape as a bare
            # traceback with nothing on stdout: report it as one, keeping both
            # tracebacks (the original is this exception's __context__).
            _report_defect(broken, json_mode=json_mode)
        # Only a Failure carries details: another error's `details` attribute,
        # if it had one, would be its own business rather than this contract's.
        details = error.details if isinstance(error, Failure) else None
        report(what, remedy, json_mode=json_mode, details=details)
        sys.exit(exit_code_for(error))
    except Exception as error:
        _report_defect(error, json_mode=json_mode)


def _safe_str(error: BaseException) -> str:
    """``str(error)``, or a placeholder when even that raises."""
    try:
        return str(error)
    except Exception:
        return "<the message could not be read>"


def _report_defect(error: BaseException, *, json_mode: bool) -> NoReturn:
    """Report an exception the skill did not expect, keeping its traceback on stderr.

    This is the last stop, so nothing from the error itself may escape: an
    unreadable message becomes a placeholder, and a traceback that cannot be
    printed does not stop the report. If stdout or stderr is closed or its reader
    has gone, the report is dropped -- there is no one to report to -- and the
    process still exits 1. (A failure inside the interpreter's own machinery is
    beyond what any handler here can promise.)
    """
    # The traceback is evidence, not the report: if printing it fails, the
    # report still goes out.
    with contextlib.suppress(Exception):
        traceback.print_exception(error, file=sys.stderr)
    report(
        _one_line(f"{type(error).__name__}: {_safe_str(error)}"),
        DEFECT_REMEDY,
        json_mode=json_mode,
    )
    sys.exit(EXIT_FAILURE)
