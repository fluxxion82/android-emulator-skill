"""The emulator console lies about failure, and this is where that is pinned.

`adb emu` exits 0 whether the command succeeded or not. Measured on API 35::

    $ adb emu avd snapshot load no_such_snapshot_xyz
    KO: Device 'encrypt' does not have the requested snapshot 'no_such_snapshot_xyz'
    KO: Snapshot load failure: snapshot doesn't exist
    $ echo $?
    0

So a caller checking `returncode` reports a successful load of a snapshot that
does not exist, and whatever test runs next runs against unknown emulator state
while reporting a pass. This is the same shape as `am broadcast` always printing
`result=0`, which is how this skill once shipped a push-notification path whose
failure branch was unreachable.

A note on fixtures
------------------
Everything here reads recorded output except the CRLF cases, which cannot be
recorded: `record_fixtures._strip_cr` normalises CRLF to LF on the way in --
deliberately, so committed diffs stay readable -- and the console's CRLF is
precisely what caused defect S5. The real bytes were measured directly::

    $ adb emu avd name | xxd
    00000000: 5069 7865 6c5f 390d 0a4f 4b0d 0a    Pixel_9..OK..

The two CRLF tests below construct that byte sequence, with the measurement
above as their provenance. They are the deliberate exception to the repo's
no-inline-tool-output rule, because the recorder cannot preserve the one
property they exist to check.
"""

from __future__ import annotations

import subprocess

import pytest

from common import adb_exec
from common.adb_exec import AdbError
from common.emu_console import EmuConsoleError, console_available, run_emu


@pytest.fixture
def console(monkeypatch):
    """Answer `adb emu` with a chosen reply, capturing the argv."""
    calls: list[list[str]] = []

    def _install(reply: str, returncode: int = 0):
        def _run(cmd, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, reply, "")

        monkeypatch.setattr(adb_exec.subprocess, "run", _run)
        return calls

    _install.calls = calls
    return _install


# ---------------------------------------------------------------------------
# The exit status cannot be trusted.
# ---------------------------------------------------------------------------


def test_a_ko_reply_raises_even_though_adb_exited_zero(console, recorded):
    """The defect this module exists to make impossible."""
    console(recorded.text("emu_avd_snapshot_load_missing"), returncode=0)

    with pytest.raises(EmuConsoleError) as excinfo:
        run_emu("avd", "snapshot", "load", "no_such_snapshot_xyz")

    assert "no_such_snapshot_xyz" in str(excinfo.value)


def test_the_ko_error_says_the_exit_status_is_meaningless(console, recorded):
    """Whoever hits this next needs to know why returncode did not catch it."""
    console(recorded.text("emu_avd_snapshot_load_missing"), returncode=0)

    with pytest.raises(EmuConsoleError) as excinfo:
        run_emu("avd", "snapshot", "load", "no_such_snapshot_xyz")

    assert "exits 0" in str(excinfo.value)


def test_check_false_returns_the_ko_instead_of_raising(console, recorded):
    """A caller that wants to inspect the rejection can still have it."""
    console(recorded.text("emu_avd_snapshot_load_missing"), returncode=0)

    reply = run_emu("avd", "snapshot", "load", "nope", check=False)
    assert "KO:" in reply.raw


def test_a_console_error_is_an_adb_error(console, recorded):
    """CLI boundaries already catch AdbError/RuntimeError; this must reach them."""
    console(recorded.text("emu_avd_snapshot_load_missing"), returncode=0)

    with pytest.raises(AdbError):
        run_emu("avd", "snapshot", "load", "nope")


def test_a_snapshot_named_like_the_failure_token_is_not_a_failure(console):
    """`KO` is matched as a line's first token, not as a substring.

    A snapshot named KOALA appears in the list output; reading that as a
    rejection would fail a load that worked.
    """
    console("KOALA\nOK\n")
    assert run_emu("avd", "snapshot", "list").payload == "KOALA"


# ---------------------------------------------------------------------------
# Framing. Defect S5 lived here.
# ---------------------------------------------------------------------------


def test_the_trailing_ok_is_not_part_of_the_payload(console, recorded):
    """S5: `.strip()` of the reply yields 'Pixel_9\\nOK', which matches no AVD name.

    emulator_boot compared that against the AVD it wanted, never matched, and so
    spawned a second emulator for an AVD that was already running.
    """
    console(recorded.text("emu_avd_name"))
    assert run_emu("avd", "name").payload == "Pixel_9"


def test_crlf_framing_is_stripped():
    """The real transport is CRLF; see this module's docstring for the bytes.

    Recorded fixtures are normalised to LF, so only a constructed input can
    prove the CR is handled.
    """
    import common.emu_console as module

    assert module._strip_framing(module._normalise("Pixel_9\r\nOK\r\n")) == "Pixel_9"


def test_crlf_does_not_hide_a_failure():
    """A KO arriving with CRLF must still be detected as a KO."""
    import common.emu_console as module

    assert module._failures(module._normalise("KO: nope\r\n")) == ["KO: nope"]


def test_a_bare_acknowledgement_has_an_empty_payload(console, recorded):
    """`sms send` answers only 'OK'; there is no payload to hand back."""
    console(recorded.text("emu_sms_send"))
    assert run_emu("sms", "send", "+15551234567", "hi").payload == ""


def test_a_table_reply_keeps_every_row(console, recorded):
    """Stripping the framing must not eat the last row of real output."""
    console(recorded.text("emu_avd_snapshot_list"))
    lines = run_emu("avd", "snapshot", "list").lines

    assert lines[0].startswith("List of snapshots")
    assert any("default_boot" in line for line in lines), f"lost the snapshot rows: {lines}"
    assert not any(line.strip() == "OK" for line in lines)


# ---------------------------------------------------------------------------
# The console does not exist on a phone.
# ---------------------------------------------------------------------------


def test_a_physical_device_gets_an_answer_that_names_the_reason(console):
    """adb says NOTHING on a phone, so the wording cannot be matched.

    Measured on a Pixel 4 XL (API 35) after this branch first shipped guessing
    at two plausible error strings, both wrong -- the remedy was dead code:

        $ adb -s <phone> emu avd name  ->  exit 1, stdout empty, stderr empty

    So the discriminator is the shape of the reply. This is deliberately not a
    recorded fixture: the entire signal is an exit code plus two empty streams,
    and a fixture stores only output.
    """
    console("", returncode=1)

    with pytest.raises(EmuConsoleError) as excinfo:
        run_emu("avd", "name", serial="1A2B3C4D")

    message = str(excinfo.value)
    assert "physical device" in message
    assert "1A2B3C4D" in message


def test_an_emulator_answering_zero_with_output_is_never_mistaken_for_a_phone(console):
    """The other half of the discriminator, measured on emulator-5554.

    An emulator answers 0 with output for EVERY command, including an unknown
    subcommand. If it did not, a `KO` would be misreported as "this is a
    physical device" and the user would be sent looking for the wrong problem.
    """
    console("KO: unknown command\n", returncode=0)

    with pytest.raises(EmuConsoleError) as excinfo:
        run_emu("bogus-subcommand", serial="emulator-5554")

    assert "physical device" not in str(excinfo.value)
    assert "KO: unknown command" in str(excinfo.value)


def test_console_available_is_false_rather_than_raising(console):
    console("", returncode=1)
    assert console_available(serial="1A2B3C4D") is False


def test_console_available_is_true_for_an_emulator(console, recorded):
    console(recorded.text("emu_avd_name"))
    assert console_available() is True


# ---------------------------------------------------------------------------
# Command construction.
# ---------------------------------------------------------------------------


def test_the_command_is_emu_and_is_bounded(console, recorded):
    """An unbounded console call wedges adb for whatever runs next."""
    calls = console(recorded.text("emu_avd_name"))
    run_emu("avd", "name", serial="emulator-5554")

    assert calls[-1][:4] == ["adb", "-s", "emulator-5554", "emu"]
    assert calls[-1][4:] == ["avd", "name"]


def test_the_console_budget_is_larger_than_an_ordinary_adb_call():
    """A snapshot save moves hundreds of MB; 30s is not enough for it."""
    from common.adb_exec import DEFAULT_TIMEOUT
    from common.emu_console import CONSOLE_TIMEOUT

    assert CONSOLE_TIMEOUT > DEFAULT_TIMEOUT
