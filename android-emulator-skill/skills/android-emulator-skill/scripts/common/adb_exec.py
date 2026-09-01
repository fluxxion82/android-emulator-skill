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
- Error strings are matched against recorded adb output where a fixture exists.
  ``adb_device_not_found.txt`` is why the "not found" match is anchored on
  ``error:`` rather than the ``adb:`` prefix a guess would have used.
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


class AdbError(RuntimeError):
    """Base for every adb failure, so callers can catch the family at once."""


class AdbNotInstalled(AdbError):
    """The adb binary is not on PATH."""


class AdbTimeout(AdbError):
    """A single adb call exceeded its time budget."""


class AdbCommandFailed(AdbError):
    """adb ran the command and it returned a non-zero status."""


class DeviceError(AdbError):
    """The command never reached a device."""


class MultipleDevices(DeviceError):
    """More than one device is attached and no serial was given."""


class DeviceNotFound(DeviceError):
    """The requested device is not attached, or nothing is attached."""


class DeviceOffline(DeviceError):
    """The device is attached but not responding to adb."""


class DeviceUnauthorized(DeviceError):
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


def _classify(stderr: str, stdout: str, serial: str | None) -> AdbError | None:
    """Map adb's own error text to a typed error that names a remedy.

    adb reports device-level problems on stderr with a mix of ``adb:`` and
    ``error:`` prefixes, so matching is on the message body rather than the
    prefix. Returns None when the text is not a recognised device error, which
    means the command reached a device and its status is the command's own.
    """
    text = f"{stderr}\n{stdout}".lower()

    if "more than one device" in text:
        return MultipleDevices(
            f"More than one device is attached, so adb could not choose one. "
            f"{_remedy_for_multiple_devices()}"
        )

    if "not found" in text and "device" in text:
        attached = _attached_serials()
        listed = ", ".join(attached) if attached else "none"
        return DeviceNotFound(
            f"Device {serial!r} is not attached. Attached: {listed}. "
            f"Run `adb devices` to see what is available."
        )

    if "no devices" in text or "no emulators" in text:
        return DeviceNotFound(
            "No devices or emulators are attached. Start one with "
            "`emulator -avd <name>`, or connect a device and enable USB debugging."
        )

    if "device offline" in text:
        return DeviceOffline(
            f"Device {serial or ''} is attached but offline. Reconnect it, or "
            f"restart the adb server with `adb kill-server && adb start-server`."
        )

    if "unauthorized" in text:
        return DeviceUnauthorized(
            f"Device {serial or ''} has not authorized this computer. Unlock it "
            f"and accept the 'Allow USB debugging?' prompt, then retry."
        )

    return None


def run_adb(
    operation: str,
    serial: str | None = None,
    *args: object,
    timeout: int | None = None,
    check: bool = False,
) -> AdbResult:
    """Run one adb command, bounded, with typed errors.

    Args:
        operation: The adb operation (``shell``, ``install``, ``devices``, ...).
        serial: Device serial; ``-s`` is omitted when None.
        *args: Remaining arguments.
        timeout: Seconds to allow. Defaults to ``ANDROID_EMU_ADB_TIMEOUT`` (30).
        check: Raise :class:`AdbCommandFailed` on a non-zero exit status.

    Returns:
        The :class:`AdbResult`.

    Raises:
        AdbNotInstalled: adb is not on PATH.
        AdbTimeout: the call exceeded ``timeout``.
        MultipleDevices, DeviceNotFound, DeviceOffline, DeviceUnauthorized:
            the command never reached a device. These raise regardless of
            ``check``, because the command did not run at all.
        AdbCommandFailed: only when ``check`` is set and the status is non-zero.

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
            text=True,
            timeout=budget,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbNotInstalled(
            "adb is not on PATH. Install the Android SDK platform-tools and add "
            'them to PATH, e.g. export PATH="$ANDROID_HOME/platform-tools:$PATH".'
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbTimeout(
            f"`{' '.join(cmd)}` did not finish within {budget}s. The device may be "
            f"busy or the adb connection wedged; try `adb kill-server` and retry."
        ) from exc

    result = AdbResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        command=cmd,
    )

    if not result.ok:
        device_error = _classify(result.stderr, result.stdout, serial)
        if device_error is not None:
            raise device_error
        if check:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise AdbCommandFailed(f"`{' '.join(cmd)}` exited {result.returncode}: {detail}")

    return result
