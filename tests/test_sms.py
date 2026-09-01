"""What sms.py may claim about a message, and how it is allowed to prove it.

``adb emu sms send`` answers a bare ``OK``. That is the console acknowledging a
command, not a message being delivered -- the same shape of trap as
``am broadcast`` always printing ``result=0``, which is how this skill once
shipped a push-notification path whose failure branch was unreachable (S4). The
read-back has the mirror-image trap: ``adb shell content query`` also exits 0
when it fails, writing ``Error while accessing provider`` to stderr and nothing
to stdout, so a caller reading only the exit status or only stdout mistakes an
unreadable inbox for an empty one.

So the central tests here script the console and the inbox *independently* and
assert on their disagreement:

* a bare ``OK`` plus an empty inbox is reported as NOT delivered;
* an ``OK`` whose matching message was already in the inbox before the send is
  reported as NOT delivered;
* a ``KO`` reply, which adb delivers with exit status 0, is a failure;
* a provider error is reported as a failed read, never as an empty inbox.

Every piece of tool output comes from ``tests/fixtures/recorded/`` through the
``recorded`` fixture. Where a test needs a subset of an inbox, it *selects
verbatim recorded lines* (``_select_rows``) rather than composing output, so no
string in this file stands in for something a tool said.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import sms

from common import adb_exec

SOURCE = Path(sms.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# Fixture names, so a rename is a single edit and a typo fails loudly in the
# `recorded` fixture rather than silently skipping.
INBOX_ONE = "content_query_sms_inbox"
INBOX_MANY = "content_query_sms_inbox_multi"
INBOX_EMPTY = "content_query_empty_result"
QUERY_ERROR = "content_query_sms_error"
SEND_OK = "emu_sms_send"
SEND_KO = "emu_sms_send_missing_arg"
CONSOLE_SMS_HELP = "emu_help_sms"


@pytest.fixture(autouse=True)
def _no_verify_polling(monkeypatch):
    """Collapse the read-back deadline so negative tests do not wait it out.

    ``send()`` polls because the message is not in the inbox when the console
    returns (measured on API 35: absent immediately, present after ~2.1s). The
    production default of 15s is right for a loaded emulator and wrong for a
    test that is *meant* to find nothing.
    """
    monkeypatch.setattr(sms, "VERIFY_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(sms, "VERIFY_POLL_SECONDS", 0)


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class FakeAdb:
    """Scripts the emulator console and the inbox query independently.

    Both go through ``common.adb_exec.run_adb`` -- ``run_emu`` is a wrapper over
    it -- so a single seam intercepts everything, and patching it here (rather
    than ``sms.subprocess``, which does not exist) is what stops a test reaching
    a real device.

    Successive inbox reads can differ, which is the whole point: ``send()``
    reads the inbox before and after, and the two disagreeing is the only
    evidence a message actually arrived.
    """

    def __init__(self, console: str = "", inbox: list[SimpleNamespace] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.console = SimpleNamespace(returncode=0, stdout=console, stderr="")
        self.inbox = inbox or [SimpleNamespace(returncode=0, stdout="", stderr="")]

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        if "emu" in cmd:
            return self.console
        if "content" in cmd:
            # The last scripted response repeats, so a test only has to script
            # the reads whose difference it cares about.
            return self.inbox.pop(0) if len(self.inbox) > 1 else self.inbox[0]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def console_calls(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if "emu" in cmd]

    @property
    def query_calls(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if "content" in cmd]


def _ok(stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """A completed adb call. Exit status 0 -- which is what both tools give on
    failure too, so no test here may lean on it."""
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_adb(monkeypatch) -> Callable[..., FakeAdb]:
    """Install a scripted adb and return the handle, so a test can assert calls."""

    def _install(console: str = "", inbox: list[SimpleNamespace] | None = None) -> FakeAdb:
        fake = FakeAdb(console=console, inbox=inbox)
        monkeypatch.setattr(adb_exec.subprocess, "run", fake.run)
        return fake

    return _install


def _run_main(monkeypatch, argv: list[str]) -> int:
    """Run the CLI in-process and return its exit code.

    An agent branches on the exit code, so the exit code is what is asserted.
    """
    monkeypatch.setattr(sms.sys, "argv", ["sms.py", *argv])
    monkeypatch.setattr(sms, "resolve_device_identifier", lambda _serial: None)
    with pytest.raises(SystemExit) as exit_info:
        sms.main()
    return exit_info.value.code


def _select_rows(recorded, name: str, keep: Callable[[sms.SmsMessage], bool]) -> str:
    """A recorded inbox narrowed to the rows whose parsed message matches ``keep``.

    Every line returned is byte-identical to one the device printed; only the
    *selection* belongs to the test. That keeps a case the fixture happens not
    to isolate (an inbox whose newest message carries no code) reachable without
    anyone hand-writing a row -- which is the failure this directory exists to
    prevent.
    """
    lines = []
    for line in recorded.lines(name):
        parsed = sms.parse_inbox(line)
        if len(parsed) == 1 and keep(parsed[0]):
            lines.append(line)
    assert lines, f"no row of {name} matched; the fixture no longer covers this case"
    return "\n".join(lines) + "\n"


def _longest_digit_run(text: str) -> int:
    """Length of the longest digit run, used to pick fixture rows by property."""
    runs = re.findall(r"\d+", text)
    return max((len(run) for run in runs), default=0)


# ---------------------------------------------------------------------------
# The row format: a body may contain the pair separator.
# ---------------------------------------------------------------------------


def test_every_parsed_field_reassembles_into_the_recorded_line(recorded):
    """Round-trip against ground truth: no field may be truncated or invented.

    `address=<a>, body=<b>, date=<d>` must appear verbatim in the fixture for
    every message parsed. A body cut short at an embedded ", " fails here, and
    so does a value that picked up the next key.
    """
    raw = recorded.text(INBOX_MANY)
    messages = sms.parse_inbox(raw)
    assert messages, "the recorded inbox parsed to nothing"
    for message in messages:
        segment = f"address={message.address}, body={message.body}, date={message.date}"
        assert (
            segment in raw
        ), f"parsed fields do not reassemble into the recorded line: {segment!r}"


DATE_IN_BODY = "content_query_sms_date_in_body"


def test_a_body_containing_date_equals_does_not_eat_the_real_date(recorded):
    """The worst case the format allows, recorded rather than imagined.

    `sms.py` claimed to survive a body containing a literal `date=`, but no
    fixture held one -- the claim rested on constructed stress input, which is
    the shape of assumption this corpus exists to replace. So a message was
    actually sent through the emulator console and the inbox re-read:

        Row: 0 address=+15550007777, body=Meeting moved, date=2026-09-15,
        code 5521, date=1788282145694

    A parser that stops the body at the first `date=` truncates it and then
    reads `2026-09-15` as the timestamp; one that runs greedily to the last
    `, ` swallows the real date into the body.
    """
    messages = sms.parse_inbox(recorded.text(DATE_IN_BODY))
    assert messages, "the recorded inbox parsed to nothing"

    embedded = [m for m in messages if "date=" in m.body]
    assert embedded, (
        f"{DATE_IN_BODY} no longer contains a body with 'date=' in it, so this "
        f"test proves nothing. Re-record it: "
        f"python tests/record_fixtures.py --only {DATE_IN_BODY}"
    )

    message = embedded[0]
    assert message.body == "Meeting moved, date=2026-09-15, code 5521"
    assert message.date == 1788282145694, f"the real trailing date= was lost; got {message.date}"
    assert message.address == "+15550007777"


def test_body_containing_the_pair_separator_survives_parsing(recorded):
    """The comma case, from the recorded inbox rather than a constructed one."""
    messages = sms.parse_inbox(recorded.text(INBOX_MANY))
    commas = [message for message in messages if ", " in message.body]
    assert commas, (
        f"{INBOX_MANY} no longer contains a body with ', ' in it, so this test "
        f"proves nothing. Re-record it: python tests/record_fixtures.py --only {INBOX_MANY}"
    )
    for message in commas:
        assert "date=" not in message.body, "the body swallowed the next key"
        assert not message.body.endswith(","), "the body was cut at a separator"


def test_naive_split_on_the_separator_would_be_wrong(recorded):
    """The trap itself, demonstrated on the recorded line.

    Splitting the row on ", " yields more parts than there are columns, which is
    exactly why the parser bounds values by the next known key instead.
    """
    offenders = [
        line
        for line in recorded.lines(INBOX_MANY)
        if line.startswith("Row:") and len(line.split(", ")) > len(sms.PROJECTION)
    ]
    assert offenders, "no recorded row exercises the ambiguous separator"
    naive = offenders[0].split(", ")
    orphans = [part for part in naive if "=" not in part]
    assert orphans, (
        "expected the naive split to strand part of a body as a keyless "
        f"fragment, which is the whole hazard: {naive}"
    )


def test_single_row_fixture_parses_to_one_message(recorded):
    """The original recorded inbox, unchanged in shape."""
    messages = sms.parse_inbox(recorded.text(INBOX_ONE))
    assert len(messages) == 1
    only = messages[0]
    assert only.address and only.body
    assert only.date > 0, "date is epoch milliseconds and must be positive"
    assert only.received_at.startswith("20"), f"unreadable timestamp: {only.received_at}"


def test_inbox_is_returned_newest_first(recorded):
    """Order is a property of the parser, not an assumption about the provider."""
    dates = [message.date for message in sms.parse_inbox(recorded.text(INBOX_MANY))]
    assert dates == sorted(dates, reverse=True)


def test_identical_bodies_remain_distinct_messages(recorded):
    """Two messages may share sender and body; only the date separates them.

    This is why verifying a send means finding a message the inbox did not
    already hold, rather than merely finding a matching one.
    """
    messages = sms.parse_inbox(recorded.text(INBOX_MANY))
    pairs = [(message.address, message.body) for message in messages]
    duplicated = {pair for pair in pairs if pairs.count(pair) > 1}
    assert duplicated, (
        f"{INBOX_MANY} no longer holds two messages with the same sender and "
        "body, so the duplicate case is untested"
    )
    identities = [message.identity for message in messages]
    assert len(set(identities)) == len(identities), "duplicates collapsed into one identity"


def test_non_row_output_is_never_parsed_as_a_message(recorded):
    """Neither the empty-result line nor a stack trace is a row."""
    assert sms.parse_inbox(recorded.text(INBOX_EMPTY)) == []
    assert sms.parse_inbox(recorded.text(QUERY_ERROR)) == []


# ---------------------------------------------------------------------------
# `content query` exits 0 whether it succeeds, finds nothing, or fails.
# ---------------------------------------------------------------------------


def test_empty_inbox_is_a_successful_read_with_no_messages(recorded, fake_adb):
    fake_adb(inbox=[_ok(stdout=recorded.text(INBOX_EMPTY))])
    success, result = sms.SmsTester().list_inbox()
    assert success is True
    assert result["count"] == 0
    assert result["messages"] == []


def test_provider_error_is_a_failed_read_not_an_empty_inbox(recorded, fake_adb):
    """The mirror-image trap: exit 0, nothing on stdout, the error on stderr.

    Reporting this as an empty inbox would tell an agent its message never
    arrived when the truth is that the inbox could not be read.
    """
    fake_adb(inbox=[_ok(stdout="", stderr=recorded.text(QUERY_ERROR))])
    success, result = sms.SmsTester().list_inbox()
    assert success is False
    assert "count" not in result, "a failed read must not report a message count"
    assert sms.PROVIDER_ERROR.lower() in result["error"].lower()


def test_list_exit_code_reports_a_provider_error(recorded, fake_adb, monkeypatch, capsys):
    fake_adb(inbox=[_ok(stdout="", stderr=recorded.text(QUERY_ERROR))])
    assert _run_main(monkeypatch, ["--list"]) == 1
    assert "0 message" not in capsys.readouterr().out


def test_list_json_reports_the_recorded_inbox(recorded, fake_adb, monkeypatch, capsys):
    fake_adb(inbox=[_ok(stdout=recorded.text(INBOX_MANY))])
    assert _run_main(monkeypatch, ["--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["count"] == len(sms.parse_inbox(recorded.text(INBOX_MANY)))
    assert payload["messages"][0]["date"] >= payload["messages"][-1]["date"]


# ---------------------------------------------------------------------------
# `OK` from the console is not delivery. This is the point of the script.
# ---------------------------------------------------------------------------


def test_ok_with_an_empty_inbox_is_reported_as_not_delivered(recorded, fake_adb):
    """The trap, stated as directly as it can be.

    The console gives the recorded ``OK``; the inbox stays empty. Anything that
    treats the reply as the answer reports success here.
    """
    fake = fake_adb(
        console=recorded.text(SEND_OK),
        inbox=[_ok(stdout=recorded.text(INBOX_EMPTY))],
    )
    success, result = sms.SmsTester().send("+15551234567", "Your code is 428193")

    assert success is False, "a bare OK was accepted as proof of delivery"
    assert result["accepted"] is True, "the console did accept the command"
    assert result["delivered"] is False
    assert "not delivered" in result["error"].lower()
    assert fake.console_calls, "no console command was issued"
    assert fake.query_calls, "delivery was judged without reading the inbox"


def test_not_delivered_exits_non_zero(recorded, fake_adb, monkeypatch):
    fake_adb(console=recorded.text(SEND_OK), inbox=[_ok(stdout=recorded.text(INBOX_EMPTY))])
    code = _run_main(monkeypatch, ["--send", "--to", "+15551234567", "--body", "Your code is 1"])
    assert code == 1


def test_a_message_already_in_the_inbox_does_not_count_as_delivery(recorded, fake_adb):
    """A pre-existing identical message must not be mistaken for the new one.

    Both reads return the same recorded inbox, so nothing arrived. The address
    and body are read out of that fixture, so the send targets a message the
    inbox demonstrably already holds.
    """
    existing = sms.parse_inbox(recorded.text(INBOX_MANY))[0]
    fake_adb(console=recorded.text(SEND_OK), inbox=[_ok(stdout=recorded.text(INBOX_MANY))])

    success, result = sms.SmsTester().send(existing.address, existing.body)

    assert success is False
    assert result["accepted"] is True
    assert result["delivered"] is False


def test_a_message_that_arrives_is_reported_as_delivered(recorded, fake_adb):
    """The positive case: the inbox after the send holds a row it did not before.

    The "before" read is the single-row fixture and the "after" read is the
    multi-row one, so the arriving message is genuinely absent from the first.
    """
    before = recorded.text(INBOX_ONE)
    after = recorded.text(INBOX_MANY)
    arrived = next(
        message
        for message in sms.parse_inbox(after)
        if message.identity not in {m.identity for m in sms.parse_inbox(before)}
    )
    fake_adb(console=recorded.text(SEND_OK), inbox=[_ok(stdout=before), _ok(stdout=after)])

    success, result = sms.SmsTester().send(arrived.address, arrived.body)

    assert success is True
    assert result["delivered"] is True
    assert result["message"]["body"] == arrived.body
    assert result["message"]["date"] == arrived.date


def test_console_ko_is_a_failure_despite_exit_status_zero(recorded, fake_adb):
    """`adb emu` exits 0 and puts the refusal in the reply text.

    The recorded KO is what the console says when the body is missing; the
    script must never read it as a send.
    """
    fake = fake_adb(console=recorded.text(SEND_KO), inbox=[_ok(stdout=recorded.text(INBOX_EMPTY))])
    success, result = sms.SmsTester().send("+15551234567", "anything")

    assert success is False
    assert result["accepted"] is False, "a KO reply was read as an accepted command"
    assert result["delivered"] is False
    assert "KO" in result["error"], f"the console's refusal is not in the error: {result['error']}"
    assert fake.console_calls, "the console was never asked"


def test_no_verify_claims_acceptance_only(recorded, fake_adb):
    """With the read-back skipped, the tool may not claim delivery."""
    fake = fake_adb(console=recorded.text(SEND_OK))
    success, result = sms.SmsTester().send("+15551234567", "hi", verify=False)

    assert success is True
    assert result["accepted"] is True
    assert result["delivered"] is False, "--no-verify claimed a delivery it did not check"
    assert not fake.query_calls, "--no-verify still queried the inbox"


def test_send_report_never_calls_acceptance_delivery(recorded, fake_adb, monkeypatch, capsys):
    """The human-readable report must keep the two words apart."""
    fake_adb(console=recorded.text(SEND_OK), inbox=[_ok(stdout=recorded.text(INBOX_EMPTY))])
    _run_main(monkeypatch, ["--send", "--to", "+1555", "--body", "x", "--no-verify"])
    out = capsys.readouterr().out.lower()
    assert "accepted" in out, "the report does not say the console accepted the command"
    assert "+ delivered" not in out, "an unverified send was reported as a delivery"


# ---------------------------------------------------------------------------
# OTP extraction: a heuristic, held to what it claims.
# ---------------------------------------------------------------------------


def test_otp_is_extracted_from_the_recorded_message(recorded):
    """The recorded body ends in its code, so the code is its last word."""
    body = sms.parse_inbox(recorded.text(INBOX_ONE))[0].body
    code = sms.extract_otp(body)
    assert code == body.split()[-1]
    assert 4 <= len(code) <= 8


def test_bodies_with_no_standalone_code_yield_nothing(recorded):
    """Selected by property from ground truth, not by quoting a body."""
    messages = sms.parse_inbox(recorded.text(INBOX_MANY))
    short = [message for message in messages if _longest_digit_run(message.body) < 4]
    assert short, f"{INBOX_MANY} has no body without a 4-8 digit run"
    for message in short:
        assert sms.extract_otp(message.body) is None


def test_a_phone_number_is_not_mistaken_for_a_code(recorded):
    """A digit run longer than 8 must not yield a code from inside it."""
    addresses = [
        message.address
        for message in sms.parse_inbox(recorded.text(INBOX_MANY))
        if _longest_digit_run(message.address) > 8
    ]
    assert addresses, f"{INBOX_MANY} has no address long enough to test the upper bound"
    for address in addresses:
        assert sms.extract_otp(address) is None


def test_otp_reads_only_the_newest_message(recorded, fake_adb):
    """A code from an older message is not the one just requested."""
    fake_adb(inbox=[_ok(stdout=recorded.text(INBOX_MANY))])
    newest = sms.parse_inbox(recorded.text(INBOX_MANY))[0]
    _found, result = sms.SmsTester().newest_otp()
    assert result["message"]["date"] == newest.date
    assert result["code"] == sms.extract_otp(newest.body)


def test_otp_exits_non_zero_when_the_newest_message_has_no_code(recorded, fake_adb, monkeypatch):
    """An inbox of verbatim recorded rows whose newest body carries no code."""
    inbox = _select_rows(recorded, INBOX_MANY, lambda message: _longest_digit_run(message.body) < 4)
    assert sms.extract_otp(sms.parse_inbox(inbox)[0].body) is None, "premise broken"
    fake_adb(inbox=[_ok(stdout=inbox)])
    assert _run_main(monkeypatch, ["--otp"]) == 1


def test_otp_exits_non_zero_on_an_empty_inbox(recorded, fake_adb, monkeypatch):
    fake_adb(inbox=[_ok(stdout=recorded.text(INBOX_EMPTY))])
    assert _run_main(monkeypatch, ["--otp"]) == 1


def test_otp_json_reports_the_message_the_code_came_from(recorded, fake_adb, monkeypatch, capsys):
    """The heuristic is only honest if the evidence travels with the answer."""
    fake_adb(inbox=[_ok(stdout=recorded.text(INBOX_ONE))])
    assert _run_main(monkeypatch, ["--otp", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] in payload["message"]["body"]
    assert payload["heuristic"]


# ---------------------------------------------------------------------------
# Structure: the console is reached one way, and only through commands that exist.
# ---------------------------------------------------------------------------


def _string_constants(node: ast.AST) -> list[str]:
    """Constant string arguments of one call."""
    return [
        arg.value
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _calls_to(*names: str) -> list[ast.Call]:
    """Every call to one of ``names`` in the module."""
    return [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) in names
    ]


def test_the_console_is_reached_only_through_run_emu():
    """`adb emu` exits 0 on failure; run_emu is where that is caught.

    A direct call would reintroduce the defect by simply forgetting, which is
    the reason common/emu_console exists at all.
    """
    for call in _calls_to("run_adb", "build_adb_command", "build_command"):
        assert "emu" not in _string_constants(call), f"bypasses run_emu: {ast.unparse(call)}"


def test_only_console_subcommands_that_exist_are_issued(recorded):
    """Checked against the console's own help, not against belief.

    Several scripts in this skill have invented sub-commands outright, so the
    authoritative list is recorded and compared to.
    """
    help_text = recorded.text(CONSOLE_SMS_HELP)
    available = {
        line.split()[1] for line in help_text.splitlines() if line.strip().startswith("sms ")
    }
    assert available, f"{CONSOLE_SMS_HELP} no longer lists any sub-command"
    for call in _calls_to("run_emu"):
        constants = _string_constants(call)
        if constants and constants[0] == "sms":
            assert constants[1] in available, f"invented sub-command: sms {constants[1]}"


def test_no_membership_test_stands_in_for_a_result():
    """S4's spelling: ``if "result=" in stdout``.

    The markers this script's tools print on success and failure alike may not
    be used as evidence of an outcome. ``EMPTY_RESULT`` and ``PROVIDER_ERROR``
    are named constants tested against a specific stream, which is a different
    thing and is why the check is on literals.
    """
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            continue
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            assert node.left.value.strip().upper() not in {
                "OK",
                "KO",
                "RESULT=",
            }, f"an outcome is being inferred from a substring: {ast.unparse(node)}"


def test_no_shell_true_and_no_direct_subprocess():
    """Every adb call goes through the bounded, typed helpers."""
    assert "shell=True" not in SOURCE
    assert not _calls_to("Popen"), "sms.py must not spawn processes directly"


# ---------------------------------------------------------------------------
# Live device. Semantic floor only: did the agent get a usable answer?
# ---------------------------------------------------------------------------


@pytest.mark.emulator
def test_send_is_proven_by_the_inbox_on_a_real_emulator(emulator_only_device, monkeypatch):
    """Send a message and require the tool to find it, end to end."""
    monkeypatch.setattr(sms, "VERIFY_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(sms, "VERIFY_POLL_SECONDS", 0.25)
    tester = sms.SmsTester(serial=emulator_only_device)
    body = f"Recorded test, code 246813 at {id(tester)}"

    success, result = tester.send("+15551230000", body)

    assert success, result.get("error")
    assert result["delivered"] is True
    assert result["message"]["body"] == body, "the body was altered in transit"

    found, otp = tester.newest_otp()
    assert found and otp["code"] == "246813", otp
