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

from .adb_exec import AdbError, run_adb
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

    failures = _failures(raw)
    if failures and check:
        raise EmuConsoleError(
            f"Emulator console rejected `emu {' '.join(args)}`: {failures[0]}\n"
            f"Note that `adb emu` exits 0 even when it fails, so the exit status "
            f"({result.returncode}) does not reflect this."
        )

    return EmuReply(payload=_strip_framing(raw), raw=result.stdout)


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
    "EmuConsoleError",
    "EmuReply",
    "console_available",
    "run_emu",
]
