#!/usr/bin/env python3
"""One entry point for the emulator console (``adb emu``).

**`adb emu` exits 0 when the command fails.** Measured on API 35::

    $ adb emu avd snapshot load no_such_snapshot_xyz
    KO: Device 'encrypt' does not have the requested snapshot 'no_such_snapshot_xyz'
    KO: Snapshot load failure: snapshot doesn't exist
    $ echo $?
    0

Failure is visible only in the reply text, so anything checking the exit status
reports a successful load of a snapshot that does not exist and runs on against
unknown emulator state while reporting a pass. (Same trap as ``am broadcast``
always printing ``result=0``; see ``push_notification.py``.) Centralising the
check here means a new ``adb emu`` caller cannot reintroduce it by forgetting.

**The reply is framed, not bare.** Every reply ends with an ``OK`` line and the
transport is CRLF, so ``adb emu avd name`` returns ``b"Pixel_9\\r\\nOK\\r\\n"``.
``.strip()`` of that yields ``"Pixel_9\\nOK"``, which matches no AVD name -- an
already-booted emulator therefore went unrecognised and a second was spawned for
the same AVD (defect S5). :func:`run_emu` returns the payload with the framing
removed, so callers never see the ``OK``.

**The console is emulator-only.** A physical device has no console, and the
error adb gives for one does not say so in as many words, so it is mapped here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .adb_exec import AdbCommandError, AdbError, run_adb
from .env_config import env_int

# Snapshot save/load moves hundreds of MB of guest RAM: measured at ~3s for a
# save on an idle API 35 emulator, and the ceiling allows for a loaded machine.
CONSOLE_TIMEOUT = env_int("ANDROID_EMU_CONSOLE_TIMEOUT", 120, min_value=5)

# The console's own framing.
_OK = "OK"
_FAILURE_PREFIX = "KO"


class EmuConsoleError(AdbError):
    """An `adb emu` command was rejected by the emulator console.

    Subclasses :class:`common.adb_exec.AdbError` so callers that catch the adb
    family -- and the CLI boundaries that catch ``RuntimeError`` -- handle a
    console rejection without a separate branch.
    """


@dataclass
class EmuReply:
    """A console reply with its framing removed.

    Attributes:
        payload: The reply body: the ``OK``/``KO`` framing lines removed and
            CRLF normalised. Empty for a command that only acknowledges.
        raw: The reply exactly as the console sent it, kept for diagnostics.
    """

    payload: str
    raw: str

    @property
    def lines(self) -> list[str]:
        """Non-empty payload lines, stripped of trailing whitespace."""
        return [line.rstrip() for line in self.payload.splitlines() if line.strip()]


def _normalise(raw: str) -> str:
    """CRLF to LF. The console transport is CRLF; the rest of this skill is not."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def _failures(text: str) -> list[str]:
    """Console failure lines.

    Matches ``KO`` as a line's first token, so a payload that merely contains
    the letters (a snapshot named ``KOALA``) is not read as a failure.
    """
    failures = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _FAILURE_PREFIX or stripped.startswith(f"{_FAILURE_PREFIX}:"):
            failures.append(stripped)
    return failures


def _strip_framing(text: str) -> str:
    """Drop the trailing ``OK`` acknowledgement the console appends to replies."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == _OK:
        lines.pop()
    return "\n".join(lines)


def run_emu(
    *args: str,
    serial: str | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> EmuReply:
    """Run one emulator-console command and return its unframed reply.

    Args:
        *args: Console command and arguments, e.g. ``"avd", "snapshot", "list"``.
        serial: Emulator serial; the default device is used when None.
        timeout: Seconds to allow. Defaults to ``ANDROID_EMU_CONSOLE_TIMEOUT``
            (120), because a snapshot save is far slower than an adb call.
        check: Raise :class:`EmuConsoleError` when the console answers ``KO``.
            Defaults to True -- the opposite of :func:`common.adb_exec.run_adb`,
            deliberately: a non-zero adb exit is sometimes just an answer, but a
            ``KO`` is always a rejection, and the exit status will not show it.

    Returns:
        The :class:`EmuReply`, with ``OK``/``KO`` framing removed.

    Raises:
        EmuConsoleError: The console answered ``KO`` (and ``check`` is True), or
            the target has no console because it is a physical device.
        AdbCommandError: adb ran and exited non-zero. Raised regardless of
            ``check``, which governs the console protocol (``KO`` at exit 0),
            not adb's own status: a caller reading the reply of a command adb
            says failed is reading nothing. ``emulator_shutdown`` and
            ``location`` both hand-rolled this check before they were routed
            here, and it has to survive the routing.
        AdbError: adb itself failed -- no device, timeout, adb missing.

    Example:
        >>> run_emu("avd", "name").payload
        'Pixel_9'
    """
    result = run_adb("emu", serial, *args, timeout=timeout or CONSOLE_TIMEOUT)
    raw = _normalise(result.stdout)

    # A physical device has no console. Measured on a Pixel 4 XL against API 35,
    # after this branch shipped guessing at two plausible error strings -- both
    # of which were wrong, so the remedy was dead code until it was measured:
    #
    #     $ adb -s <phone> emu avd name   ->  exit 1, stdout empty, stderr empty
    #     $ adb -s <emulator> emu <anything>  ->  exit 0, stdout non-empty
    #
    # adb says nothing at all. The discriminator is therefore the *shape* of the
    # reply, not its wording: an emulator answers 0 with output for every
    # command including an unknown subcommand ("KO: unknown command"), while a
    # phone answers non-zero with both streams empty.
    #
    # This cannot be a recorded fixture: the whole signal is an exit code and
    # two empty streams, and a fixture stores only output. It is pinned
    # behaviourally in tests/test_emu_console.py instead.
    if result.returncode != 0 and not raw.strip() and not result.stderr.strip():
        raise EmuConsoleError(
            f"{serial or 'the target device'} has no emulator console, which "
            f"means it is a physical device (adb exits {result.returncode} here "
            f"and prints nothing at all). `adb emu` commands -- sms, snapshot, "
            f"gsm, geo -- work only on emulators; use a booted emulator instead."
        )

    # adb ran and failed. Not the console's ``KO`` -- that arrives at exit 0 --
    # but the transport underneath it, e.g. a console kill the emulator
    # refused. The two sites routed here in L7 each tested ``returncode`` by
    # hand and reported the stderr; keeping that here is what let them stop.
    if result.returncode != 0:
        detail = result.stderr.strip() or raw.strip() or "no output"
        raise AdbCommandError(
            f"`{' '.join(result.command)}` exited {result.returncode}: {detail}",
            command=result.command,
            result=result,
        )

    failures = _failures(raw)
    if failures and check:
        raise EmuConsoleError(
            f"Emulator console rejected `emu {' '.join(args)}`: {failures[0]}\n"
            f"Note that `adb emu` exits 0 even when it fails, so the exit status "
            f"({result.returncode}) does not reflect this."
        )

    return EmuReply(payload=_strip_framing(raw), raw=result.stdout)


# How long to wait for a console to say which AVD it is. Short on purpose: the
# probe runs once per attached emulator before a boot, an erase or a shutdown,
# and an emulator that is going to answer answers at once.
IDENTIFY_TIMEOUT = env_int("ANDROID_EMU_IDENTIFY_TIMEOUT", 5, min_value=1)

# Remedies for an emulator that will not say which AVD it is. Kept here so the
# four scripts that ask the question quote the same fix.
UNKNOWN_EMULATOR_REMEDY = (
    "restart the emulator, or reset the adb connection with "
    "`adb kill-server && adb start-server`"
)


class EmulatorProbeError(AdbError):
    """The set of attached emulators could not be determined at all.

    Distinct from an emulator that would not identify itself: that one is a
    :class:`EmulatorProbe` with no name, which the caller decides what to do
    about. This means the question could not even be asked.
    """


class RunningVerdict(StrEnum):
    """Whether a named AVD is running -- including "nobody knows".

    The third case is the point. Every script here used to collapse it into
    ``False``/``None``, and each collapse authorised something irreversible: an
    erase wiped a live AVD (L4), a boot launched a second instance of an AVD
    that was already up (S5), a shutdown reported nothing to kill.
    """

    RUNNING = "running"
    NOT_RUNNING = "not-running"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EmulatorProbe:
    """One attached emulator, and whether we know which AVD it is.

    Attributes:
        serial: The emulator serial, e.g. ``emulator-5554``.
        state: adb's own state column -- ``device``, ``offline``,
            ``unauthorized``. Anything but ``device`` means the console cannot
            be asked at all.
        avd_name: The AVD it is running, or None when that could not be read.
        reason: Why the name is missing, phrased for a reader. None when the
            emulator identified itself.
    """

    serial: str
    state: str
    avd_name: str | None
    reason: str | None

    @property
    def identified(self) -> bool:
        """Whether this emulator said which AVD it is."""
        return self.avd_name is not None

    def describe(self) -> str:
        """One line naming the serial, its state and why it is unidentified."""
        if self.identified:
            return f"{self.serial} ({self.state}) is running {self.avd_name}"
        return f"{self.serial} (state {self.state}) could not be identified: {self.reason}"


@dataclass(frozen=True)
class RunningAnswer:
    """The tri-state answer to "is AVD <name> running?".

    Attributes:
        verdict: See :class:`RunningVerdict`.
        serial: The emulator running the AVD, when the verdict is RUNNING.
        probes: Every attached emulator, identified or not, so a caller can
            report what it saw rather than only what it concluded.
    """

    verdict: RunningVerdict
    serial: str | None
    probes: tuple[EmulatorProbe, ...]

    @property
    def unidentified(self) -> tuple[EmulatorProbe, ...]:
        """The emulators that would not say which AVD they are."""
        return tuple(probe for probe in self.probes if not probe.identified)

    def describe_unknown(self) -> str:
        """Why the verdict is UNKNOWN, naming each emulator and its remedy."""
        return (
            "; ".join(probe.describe() for probe in self.unidentified)
            + f". {UNKNOWN_EMULATOR_REMEDY}"
        )


def identify_emulator(
    serial: str, state: str = "device", *, timeout: int | None = None
) -> EmulatorProbe:
    """Ask one emulator which AVD it is running.

    The single place in this skill that turns a serial into an AVD name. It was
    four places -- ``emulator_boot``, ``emulator_erase``, ``emulator_selector``
    and ``emulator_shutdown`` each had their own copy -- and the four disagreed
    about what a failure means: one caught ``AdbError``, one only
    ``EmuConsoleError``, one bare ``Exception``, and every one of them returned
    None or False, so "I could not ask" and "it is not that AVD" were the same
    answer. They are not the same answer, and the difference is what the three
    destructive paths were getting wrong.

    Never raises for a console or command failure: that outcome is the probe's
    answer, carried as ``reason``. Deciding what an unknown emulator means is
    the caller's job, and the four callers decide differently.

    Args:
        serial: Emulator serial to ask.
        state: adb's state column for that serial. Anything but ``device``
            short-circuits -- an emulator mid-boot is listed before its console
            is up, so asking wastes the timeout and answers nothing.
        timeout: Seconds to allow; defaults to :data:`IDENTIFY_TIMEOUT`.

    Returns:
        An :class:`EmulatorProbe`, identified or not.
    """
    if state != "device":
        return EmulatorProbe(
            serial=serial,
            state=state,
            avd_name=None,
            reason=f"adb reports it in state {state!r}, so its console is not up yet",
        )

    try:
        reply = run_emu("avd", "name", serial=serial, timeout=timeout or IDENTIFY_TIMEOUT)
    except AdbError as error:
        # The whole family, deliberately. A device-level error here is not
        # evidence about the AVD either -- `emulator_boot` relies on that,
        # since an emulator restarts adbd mid-boot and answers "device offline"
        # for a window, which is exactly the state --wait-ready waits out.
        return EmulatorProbe(serial=serial, state=state, avd_name=None, reason=str(error))

    lines = reply.lines
    if not lines:
        return EmulatorProbe(
            serial=serial,
            state=state,
            avd_name=None,
            reason="its console answered with no AVD name",
        )
    return EmulatorProbe(serial=serial, state=state, avd_name=lines[0], reason=None)


def avd_name_for_serial(serial: str, *, timeout: int | None = None) -> str | None:
    """The AVD an emulator is running, or None when it would not say.

    The lossy convenience form of :func:`identify_emulator`, for callers that
    genuinely only want the name. Anything deciding whether to boot, erase or
    kill should use the probe and read its ``reason``.
    """
    return identify_emulator(serial, timeout=timeout).avd_name


def probe_emulators(
    devices: list[dict] | None = None, *, timeout: int | None = None
) -> list[EmulatorProbe]:
    """Every attached emulator, each with its AVD name or the reason there is none.

    Args:
        devices: A listing from
            :func:`common.device_utils.get_connected_devices`, when the caller
            already has one. Omit it and one is fetched. Passing it in keeps
            the listing on the caller's own seam, which is where its tests
            already stub it, and means one boot does not query adb twice.
        timeout: Per-emulator console timeout; defaults to :data:`IDENTIFY_TIMEOUT`.

    Returns:
        One :class:`EmulatorProbe` per emulator serial. Physical devices are
        not included: they have no console and are not AVDs.

    Raises:
        EmulatorProbeError: The device listing failed, so not even the set of
            emulators is known. Only when this function did the listing --
            a caller supplying ``devices`` has already handled that.
    """
    if devices is None:
        # Imported here rather than at module scope: device_utils imports the
        # hierarchy parser, and every `adb emu` caller would otherwise pay for
        # a module it does not use.
        from .device_utils import get_connected_devices

        try:
            devices = get_connected_devices()
        except AdbError:
            raise
        except RuntimeError as error:
            # get_connected_devices re-wraps a failed listing as a plain
            # RuntimeError, which the CLI boundaries do not recognise as an adb
            # failure and would print as a traceback.
            raise EmulatorProbeError(
                f"the attached emulators could not be listed: {error}"
            ) from error

    return [
        identify_emulator(device["serial"], device["state"], timeout=timeout)
        for device in devices
        if device["type"] == "emulator"
    ]


def avd_running(
    name: str, devices: list[dict] | None = None, *, timeout: int | None = None
) -> RunningAnswer:
    """Whether ``name`` is running: yes, no, or nobody knows.

    UNKNOWN wins over NOT_RUNNING and loses to RUNNING. That ordering is the
    whole safety property: an emulator that would not identify itself might be
    this AVD, so "no emulator said it was this one" is only NOT_RUNNING when
    every emulator answered.

    Args:
        name: AVD name, compared for equality against each console's reply.
        devices: A listing from
            :func:`common.device_utils.get_connected_devices`, when the caller
            already has one. See :func:`probe_emulators`.
        timeout: Per-emulator console timeout.

    Returns:
        A :class:`RunningAnswer`.

    Raises:
        EmulatorProbeError: The device listing itself failed, when this
            function did the listing.
    """
    probes = tuple(probe_emulators(devices, timeout=timeout))
    for probe in probes:
        if probe.avd_name == name:
            return RunningAnswer(RunningVerdict.RUNNING, probe.serial, probes)
    if any(not probe.identified for probe in probes):
        return RunningAnswer(RunningVerdict.UNKNOWN, None, probes)
    return RunningAnswer(RunningVerdict.NOT_RUNNING, None, probes)


def console_available(serial: str | None = None) -> bool:
    """Whether the target has a reachable emulator console.

    Args:
        serial: Device serial; the default device is used when None.

    Returns:
        True if the console answers, False for a physical device or an
        emulator whose console is not reachable.
    """
    try:
        run_emu("avd", "name", serial=serial, timeout=10)
    except AdbError:
        return False
    return True


__all__ = [
    "CONSOLE_TIMEOUT",
    "IDENTIFY_TIMEOUT",
    "UNKNOWN_EMULATOR_REMEDY",
    "EmuConsoleError",
    "EmuReply",
    "EmulatorProbe",
    "EmulatorProbeError",
    "RunningAnswer",
    "RunningVerdict",
    "avd_name_for_serial",
    "avd_running",
    "console_available",
    "identify_emulator",
    "probe_emulators",
    "run_emu",
]
