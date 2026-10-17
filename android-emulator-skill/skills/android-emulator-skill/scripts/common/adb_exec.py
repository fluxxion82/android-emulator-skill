#!/usr/bin/env python3
"""One bounded entry point for every adb invocation.

Why this exists
---------------
Two problems, both measured rather than assumed.

**Unbounded calls.** An AST sweep found 69 ``subprocess.run``/``Popen`` calls
across 17 scripts with no ``timeout=``. An unbounded adb call does not just hang
its caller: it wedges the adb connection for whatever runs next, which presents
as a hang with no diagnosis. Every call made through :func:`run_adb` is bounded.

**Unusable errors.** For an agent driving this skill, stderr is the retry
prompt. ``adb: more than one device/emulator`` states a fact and offers no
action; the agent needs to be told to pass ``--serial``, and ideally which
serials exist. Raw adb text was reaching the agent unmapped. Each failure here
raises a typed error whose message names a remedy, and the tests assert that
rather than trusting it.

Design notes
------------
- **Device errors raise even without ``check=True``.** "more than one device"
  means the command never ran at all, which is categorically different from a
  command that ran and returned non-zero. A caller should not have to opt in to
  noticing that its command did not execute.
- **A non-zero exit is not, by itself, an error.** ``adb shell exit 7``
  propagates 7 (verified on a device); plenty of commands answer that way. Use
  ``check=True`` when a non-zero status is genuinely a failure.
- Error strings are matched against recorded adb output where a fixture exists,
  and the two profiles disagree about the prefix: ``adb -s <unknown> shell``
  prints ``adb: device '...' not found`` on emulator-api35, while
  ``adb_device_not_found.txt`` on pixel4xl-api33 prints ``error: ...``. So the
  match is deliberately prefix-AGNOSTIC ("not found" and "device"); anchoring on
  either prefix would break the other profile. This docstring previously claimed
  the match was anchored on ``error:``, which the code never did -- a doc
  asserting a precision the code does not have.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .env_config import env_int

# Default ceiling for a single adb call. Generous enough for a slow `dumpsys` on
# a loaded emulator, short enough that a wedged call surfaces as an error rather
# than an apparent stall. Long-running work (logcat streaming, gradle) passes its
# own timeout rather than raising this.
DEFAULT_TIMEOUT = env_int("ANDROID_EMU_ADB_TIMEOUT", 30, min_value=1)

# Grace period for a terminated child to exit before it is killed.
SHUTDOWN_TIMEOUT = env_int("ANDROID_EMU_ADB_SHUTDOWN_TIMEOUT", 5, min_value=1)

_ADB_NOT_INSTALLED_REMEDY = (
    "Install the Android SDK platform-tools and add them to PATH, e.g. "
    'export PATH="$ANDROID_HOME/platform-tools:$PATH" -- note that is the '
    "platform-tools directory, not the SDK root."
)


class AdbError(RuntimeError):
    """Base for every adb failure, so callers can catch the family at once.

    Subclassing ``RuntimeError`` is deliberate API, not an accident. Several CLI
    boundaries pre-date this module and already catch ``RuntimeError`` from
    ``resolve_device_identifier``; inheriting from it means an adb failure
    reaches those handlers already carrying its remedy, and ``screen_mapper``
    relies on exactly that to route the remedy into its normal output. Pinned by
    ``test_adb_error_is_a_runtime_error``.

    Attributes:
        command: The argv that failed, so a caller can say what was run.
        result: The :class:`AdbResult` when adb produced one, else None.
        remedy: The sentence of the message that says what to do, when the
            message has one. ``str(error)`` still carries it -- every caller that
            prints the error keeps printing the remedy -- and ``common.cli``
            uses this to report the two halves as separate fields without
            saying the remedy twice.
    """

    def __init__(
        self,
        message: str,
        command: list[str] | None = None,
        result=None,
        *,
        remedy: str | None = None,
    ):
        super().__init__(message)
        self.command = command or []
        self.result = result
        self.remedy = remedy

    @property
    def stdout(self) -> str:
        """Captured stdout, or empty when adb produced no result."""
        return self.result.stdout if self.result else ""

    @property
    def stderr(self) -> str:
        """Captured stderr, or empty when adb produced no result."""
        return self.result.stderr if self.result else ""

    @property
    def returncode(self) -> int | None:
        """Exit status, or None when the command never produced one."""
        return self.result.returncode if self.result else None


class AdbNotInstalledError(AdbError):
    """The adb binary is not on PATH."""


class AdbTimeoutError(AdbError):
    """A single adb call exceeded its time budget."""


class AdbCommandError(AdbError):
    """adb ran the command and it returned a non-zero status."""


class DeviceError(AdbError):
    """The command never reached a device."""


class MultipleDevicesError(DeviceError):
    """More than one device is attached and no serial was given."""


class DeviceNotFoundError(DeviceError):
    """The requested device is not attached, or nothing is attached."""


class DeviceOfflineError(DeviceError):
    """The device is attached but not responding to adb."""


class DeviceUnauthorizedError(DeviceError):
    """The device has not accepted this host's USB debugging key."""


@dataclass(frozen=True)
class AdbResult:
    """Outcome of one adb call.

    Attributes:
        returncode: Process exit status. For ``adb shell``, this is the exit
            status of the command that ran *on the device*.
        stdout: Captured standard output.
        stderr: Captured standard error.
        command: The full argv, kept so a failure can show what was run.
    """

    returncode: int
    stdout: str
    stderr: str
    command: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the command reported success."""
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout, falling back to stderr when a tool writes its answer there."""
        return self.stdout if self.stdout.strip() else self.stderr


def build_command(operation: str, serial: str | None = None, *args: object) -> list[str]:
    """Assemble an adb argv, targeting a serial when one is given.

    Args:
        operation: The adb operation (``shell``, ``install``, ``devices``, ...).
        serial: Device serial; the ``-s`` flag is omitted when None.
        *args: Remaining arguments, stringified.

    Returns:
        Complete argv, ready for subprocess. Never a shell string.
    """
    if serial and serial.startswith("-"):
        raise ValueError(
            f"serial={serial!r} looks like a flag. `serial` is the second "
            f"positional argument, so run_adb('devices', '-l') would become "
            f"`adb -s -l devices`. Pass None for the serial, or use serial=."
        )

    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.append(operation)
    cmd.extend(str(arg) for arg in args)
    return cmd


def _attached_serials() -> list[str]:
    """Serials currently in state ``device``, for use in error messages.

    Best effort and deliberately quiet: this runs while building a failure
    message, so it must never raise or add meaningful delay.
    """
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        line.split()[0]
        for line in result.stdout.splitlines()[1:]
        if line.strip() and line.split()[-1] == "device"
    ]


def _remedy_for_multiple_devices() -> str:
    """Tell the caller which serial to pass, not merely that one is needed."""
    serials = _attached_serials()
    if serials:
        listed = ", ".join(serials)
        return f"Pass --serial with one of: {listed}."
    return "Pass --serial to choose a device (see `adb devices`)."


# How adb itself begins an error line (case-sensitive: a device tool's
# "Error: ..." is not adb's). Both are recorded, one per profile.
_ADB_OWN_PREFIXES = ("adb: ", "error: ")


def _classify(stderr: str, serial: str | None) -> AdbError | None:
    """Map adb's own error text to a typed error that names a remedy.

    Only adb's own stderr is examined. A command running *on the device* can
    legitimately print "device not found" -- a log line, a grep hit, an app's own
    message -- and classifying stdout let device-generated text forge a
    host-level failure. For an agent that treats these messages as retry
    prompts, that is a false remedy.

    adb reports device problems with a mix of ``adb:`` and ``error:`` prefixes
    (both recorded: adb_shell_device_not_found, adb_device_not_found), so either
    prefix is accepted -- but only at the START of a line. A command run on the
    device writes ITS stderr through adb's (``adb shell``), and matching the
    body anywhere read ``cat: databases/unauthorized.db: No such file or
    directory`` as an unauthorized device: exit 3, the file's reason lost
    (Codex review of #29, once file reads went through run_argv). Returns None
    when the text is not a recognised device error, meaning the command reached
    a device and its status is the command's own.
    """
    text = "\n".join(
        line for line in stderr.split("\n") if line.startswith(_ADB_OWN_PREFIXES)
    ).lower()
    if not text:
        return None

    if "more than one device" in text:
        remedy = _remedy_for_multiple_devices()
        return MultipleDevicesError(
            f"More than one device is attached, so adb could not choose one. {remedy}",
            remedy=remedy,
        )

    if "not found" in text and "device" in text:
        attached = _attached_serials()
        listed = ", ".join(attached) if attached else "none"
        remedy = "Run `adb devices` to see what is available."
        return DeviceNotFoundError(
            f"Device {serial!r} is not attached. Attached: {listed}. {remedy}",
            remedy=remedy,
        )

    if "no devices" in text or "no emulators" in text:
        remedy = (
            "Start one with `emulator -avd <name>`, or connect a device and enable USB debugging."
        )
        return DeviceNotFoundError(
            f"No devices or emulators are attached. {remedy}",
            remedy=remedy,
        )

    if "device offline" in text:
        remedy = (
            "Reconnect it, or restart the adb server with `adb kill-server && adb start-server`."
        )
        return DeviceOfflineError(
            f"Device {serial or ''} is attached but offline. {remedy}",
            remedy=remedy,
        )

    if "unauthorized" in text:
        remedy = "Unlock it and accept the 'Allow USB debugging?' prompt, then retry."
        return DeviceUnauthorizedError(
            f"Device {serial or ''} has not authorized this computer. {remedy}",
            remedy=remedy,
        )

    return None


def classify_device_error(stderr: str, serial: str | None = None) -> DeviceError | None:
    """The typed device error adb's own stderr names, or None.

    For the one kind of caller that cannot go through :func:`run_adb`: a
    streaming ``adb logcat`` read line by line (``log_monitor``). Its stderr
    gets the same classification, so a device that is not there is the same
    error -- and the same exit status at the CLI boundary -- however adb was run.

    Args:
        stderr: What adb wrote to stderr.
        serial: The serial the command targeted, for the message.
    """
    return _classify(stderr, serial)


def _as_text(output: bytes | str | None) -> str:
    """Output as text; bytes decoded as UTF-8 with no newline translation."""
    if output is None:
        return ""
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output


def _not_installed_error() -> AdbNotInstalledError:
    """The error for an adb that could not be run at all, with its remedy."""
    return AdbNotInstalledError(
        f"adb could not be run: it is not on PATH, or the file found is not "
        f"executable. {_ADB_NOT_INSTALLED_REMEDY}",
        remedy=_ADB_NOT_INSTALLED_REMEDY,
    )


def _timeout_error(cmd: list[str], budget: float) -> AdbTimeoutError:
    """The error for an adb call that did not finish within ``budget`` seconds."""
    remedy = "try `adb kill-server` and retry."
    return AdbTimeoutError(
        f"`{' '.join(cmd)}` did not finish within {budget}s. The device may be "
        f"busy or the adb connection wedged; {remedy}",
        command=cmd,
        remedy=remedy,
    )


def _confirm_device_error(error: AdbError, serial: str | None) -> AdbError | None:
    """Keep a suspected device verdict only when one bounded reachability probe fails.

    ``adb shell`` puts a remote command's stderr on adb's stderr, so even an
    anchored ``adb:`` line can come from a filename or other device-controlled
    field. A successful ``get-state`` is enough to show that the original
    failure belongs to the remote command; its output is deliberately not
    parsed. If the probe itself cannot reach adb or the target, retain the
    original, more specific verdict and remedy.
    """
    try:
        run_adb("get-state", serial, timeout=DEFAULT_TIMEOUT, check=True)
    except AdbError:
        return error
    return None


def run_adb(
    operation: str,
    serial: str | None = None,
    *args: object,
    timeout: int | None = None,
    check: bool = False,
    raw_text: bool = False,
) -> AdbResult:
    """Run one adb command, bounded, with typed errors.

    Args:
        operation: The adb operation (``shell``, ``install``, ``devices``, ...).
        serial: Device serial; ``-s`` is omitted when None.
        *args: Remaining arguments.
        timeout: Seconds to allow. Defaults to ``ANDROID_EMU_ADB_TIMEOUT`` (30).
        check: Raise :class:`AdbCommandError` on a non-zero exit status.
        raw_text: Read the output as bytes and decode it WITHOUT newline
            translation. Text mode turns a ``\\r`` into a line break; logcat
            frames lines with ``\\n`` only and leaves an app's ``\\r`` raw, so
            in text mode an app could start a forged "line" mid-message
            (security review of #27). Split such output on ``\\n`` only.

    Returns:
        The :class:`AdbResult`.

    Raises:
        AdbNotInstalledError: adb is not on PATH.
        AdbTimeoutError: the call exceeded ``timeout``.
        MultipleDevicesError, DeviceNotFoundError, DeviceOfflineError,
        DeviceUnauthorizedError:
            the command never reached a device. These raise regardless of
            ``check``, because the command did not run at all.
        AdbCommandError: only when ``check`` is set and the status is non-zero.

    Example:
        >>> run_adb("shell", "emulator-5554", "getprop", "ro.build.version.sdk").stdout
        '35\\n'
    """
    budget = DEFAULT_TIMEOUT if timeout is None else timeout
    cmd = build_command(operation, serial, *args)

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=not raw_text,
            timeout=budget,
            check=False,
        )
    except OSError as exc:
        # OSError, not just FileNotFoundError. Exec can fail several ways that
        # are all "adb could not be run", and only one of them is "missing":
        #
        #   PermissionError    a non-executable file named adb is found first
        #   IsADirectoryError  `adb` resolves to a directory -- which is exactly
        #                      what happens when the SDK ROOT is on PATH, the
        #                      same shape as the `emulator` directory bug
        #
        # Both were reproduced: with only such a stub on PATH, the raw
        # PermissionError escaped this function and reached the user as a
        # traceback instead of a remedy.
        raise _not_installed_error() from exc
    except subprocess.TimeoutExpired as exc:
        raise _timeout_error(cmd, budget) from exc

    result = AdbResult(
        returncode=completed.returncode,
        stdout=_as_text(completed.stdout),
        stderr=_as_text(completed.stderr),
        command=cmd,
    )

    if not result.ok:
        device_error = _classify(result.stderr, serial)
        if device_error is not None:
            device_error.command = cmd
            device_error.result = result
            raise device_error
        if check:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise AdbCommandError(
                f"`{' '.join(cmd)}` exited {result.returncode}: {detail}",
                command=cmd,
                result=result,
            )

    return result


def run_argv(
    cmd: list[str],
    *,
    serial: str | None,
    timeout: float | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess:
    """Run an adb argv the caller built, for raw bytes, failing as :func:`run_adb` does.

    For the reads :func:`run_adb` cannot serve: an app's file or database
    (``common.run_as``), and a listing whose names are bytes until something
    decides otherwise. ``raw_text`` decodes, which would replace what is not
    UTF-8. The argv comes from ``build_command`` or
    ``device_utils.build_adb_command``, so it starts with ``adb`` and carries
    ``-s`` when ``serial`` is set.

    adb's own failures are typed exactly as in :func:`run_adb`, so they reach
    ``common.cli`` with their remedy and exit status instead of being folded
    into "the command failed": they were, in container.py and
    model_inspector.py, which answered a device that was not there with
    "Command failed (ls -la ...): adb: device ... not found" at exit 1.

    Args:
        cmd: The full argv.
        serial: The serial ``cmd`` targets, for a device error's message.
        timeout: Seconds to allow. Defaults to ``ANDROID_EMU_ADB_TIMEOUT`` (30).
        text: Decode as text instead (for output nothing makes a path of).

    Returns:
        The completed process; its status and streams are the caller's to judge.

    Raises:
        AdbNotInstalledError, AdbTimeoutError, and the device errors, as
        :func:`run_adb` raises them. Never :class:`AdbCommandError`.
    """
    budget = DEFAULT_TIMEOUT if timeout is None else timeout
    try:
        completed = subprocess.run(cmd, capture_output=True, text=text, timeout=budget, check=False)
    except OSError as exc:
        raise _not_installed_error() from exc
    except subprocess.TimeoutExpired as exc:
        raise _timeout_error(cmd, budget) from exc
    if completed.returncode != 0:
        device_error = _classify(_as_text(completed.stderr), serial)
        if device_error is not None:
            device_error = _confirm_device_error(device_error, serial)
        if device_error is not None:
            device_error.command = cmd
            raise device_error
    return completed
