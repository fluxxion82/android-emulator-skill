#!/usr/bin/env python3
"""
SMS on an emulator: deliver an inbound message, read the inbox back, and pull a
one-time code out of the newest message.

Two tool behaviours drive the design; both are the ``am broadcast result=0``
trap (defect S4, ``push_notification.py``) in a new place.

**``adb emu sms send`` answers a bare ``OK``, meaning the console accepted the
command -- not that a message was delivered.** With the body omitted it answers
``KO: missing argument`` and adb still **exits 0** (recorded: ``emu_sms_send``,
``emu_sms_send_missing_arg``), so failure lives in the reply text alone: every
console call goes through ``common.emu_console.run_emu``, which raises on a
``KO``, and none shells out to ``adb emu``. The sender is not validated either
-- ``sms send abc "hi"`` answers ``OK`` and delivers ``abc`` verbatim. So
``--send`` proves delivery by reading ``content://sms/inbox`` back for a message
the inbox did not already hold; *accepted* and *delivered* are reported
separately and never conflated.

**``adb shell content query`` also exits 0 when it fails**, writing
``Error while accessing provider:<authority>`` and a Java stack trace to
**stderr** and nothing to stdout (recorded: ``content_query_sms_error``); an
empty result set is instead ``No result found.`` on stdout
(``content_query_empty_result``). Checking only the exit status, or only stdout,
turns "the inbox could not be read" into "your message never arrived".

Row shape, and why it cannot be split naively::

    Row: 1 address=+15550002222, body=Order 42, shipped today, date=1788280004066

A body may contain the ``", "`` pair separator, as that recorded row does, so
:func:`parse_content_rows` bounds each value by the next *known key* (recorded:
``content_query_sms_inbox_multi``). ``date`` is epoch milliseconds; rows arrive
newest first, but this script sorts rather than trust that.

Limitations: ``--send`` is **emulator-only** (a physical device has no console;
``--list``/``--otp`` are plain content queries and work anywhere); there is **no
outgoing SMS and no MMS** -- ``adb emu help sms`` lists only ``send`` and
``pdu``, both simulating an *inbound* message (``emu_help_sms``); a message
cannot be aimed at **a particular app**, only at the platform; **nothing is
written to the provider** (no delete, no mark-as-read -- whether the shell uid
may is UNVERIFIED, and unverified capabilities are not shipped); and ``--otp``
is a **heuristic, not a parser** (see :func:`extract_otp`).

Usage Examples:
    python sms.py --send --to +15551234567 --body "Your code is 428193"
    python sms.py --list --limit 5
    python sms.py --otp --json
"""

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from common.adb_exec import AdbError, run_adb
from common.device_utils import resolve_device_identifier
from common.emu_console import EmuConsoleError, run_emu
from common.env_config import env_int

# Measured on API 35: readable from the shell uid with no permission grant.
INBOX_URI = "content://sms/inbox"

# Requested column order. The parser bounds each value by the *next* key here.
PROJECTION = ("address", "body", "date")

# `content query` says these, on the streams noted, and exits 0 either way.
EMPTY_RESULT = "No result found."  # stdout, when the cursor has no rows
PROVIDER_ERROR = "Error while accessing provider"  # stderr, when the query fails

# Every adb call is bounded; an unbounded one wedges the connection.
QUERY_TIMEOUT = env_int("ANDROID_EMU_SMS_TIMEOUT", 20, min_value=5)

# `adb emu sms send` returns before the message reaches the inbox (measured on
# API 35: absent immediately, present after ~2.1s), so the read-back polls.
VERIFY_TIMEOUT_SECONDS = env_int("ANDROID_EMU_SMS_VERIFY_TIMEOUT", 15, min_value=1)
VERIFY_POLL_SECONDS = 0.25

# A run of 4-8 digits with no digit or letter against either end, so a 10-digit
# phone number and the "1234" in "A1234B" are excluded. See extract_otp.
OTP_PATTERN = re.compile(r"(?<![0-9A-Za-z])(\d{4,8})(?![0-9A-Za-z])")

# Repeated wherever a send is reported: "OK" is not proof of delivery.
ACCEPTED_CAVEAT = (
    "`adb emu sms send` answered OK, which means the console accepted the "
    "command - not that a message was delivered."
)


@dataclass(frozen=True)
class SmsMessage:
    """One row of the SMS inbox.

    Attributes:
        address: Sender. Not guaranteed numeric: an alphanumeric sender is
            delivered verbatim.
        body: Message text, which may itself contain ``", "``.
        date: Receipt time in epoch **milliseconds**, as the provider stores it.
        row: The ``Row: <n>`` index the query printed, for traceability.
    """

    address: str
    body: str
    date: int
    row: int

    @property
    def received_at(self) -> str:
        """Receipt time as an ISO-8601 UTC string, for human-readable output."""
        return datetime.fromtimestamp(self.date / 1000, tz=UTC).isoformat(timespec="seconds")

    @property
    def identity(self) -> tuple[str, str, int]:
        """Identity used to tell an existing message from a new one; ``date`` is
        part of it because the inbox legitimately holds two messages with the
        same sender and body (the recorded fixture has that pair)."""
        return (self.address, self.body, self.date)


def parse_content_rows(output: str, projection: tuple[str, ...] = PROJECTION) -> list[dict]:
    """Parse ``adb shell content query`` rows into ``{key: value}`` dicts.

    The CLI prints ``Row: <n> k1=v1, k2=v2, ...`` unquoted, so a body containing
    ``", "`` looks exactly like a pair boundary; each value is therefore bounded
    by the *next key in the projection*. A body ending in ``", date=<digits>"``
    stays ambiguous and cannot be resolved from this output at all. Non-row
    lines (``No result found.``, a stack trace) are skipped here; the caller
    distinguishes those cases before calling.

    Args:
        output: Raw stdout of the ``content query``.
        projection: Column names in the order they were requested.

    Returns:
        One dict per parsed row, in the order printed. Each carries the
        projection keys plus ``row``, all as strings except ``row``.
    """
    pattern = _row_pattern(projection)
    rows = []
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match is None:
            continue
        fields = match.groupdict()
        rows.append({"row": int(fields.pop("row")), **fields})
    return rows


def _row_pattern(projection: tuple[str, ...]) -> re.Pattern[str]:
    """Build the row regex for one projection.

    Values are lazy (ending at the next key) except the second-to-last, which is
    greedy so it ends at the *last* occurrence of the final key.
    """
    parts = [r"^Row:\s*(?P<row>\d+)\s+"]
    for index, key in enumerate(projection):
        separator = "" if index == 0 else ", "
        if index == len(projection) - 1:
            value = f"(?P<{key}>.*)$"
        elif index == len(projection) - 2:
            value = f"(?P<{key}>.*)"
        else:
            value = f"(?P<{key}>.*?)"
        parts.append(f"{separator}{re.escape(key)}={value}")
    return re.compile("".join(parts))


def parse_inbox(output: str) -> list[SmsMessage]:
    """Parse inbox rows into messages, newest first.

    Rows whose ``date`` is not an integer are dropped rather than guessed at.

    Args:
        output: Raw stdout of the inbox query.

    Returns:
        Messages sorted by ``date`` descending -- the provider sorts that way
        too, but sorting here makes the order this function's guarantee.
    """
    messages = []
    for row in parse_content_rows(output):
        date = str(row.get("date", "")).strip()
        if not date.lstrip("-").isdigit():
            continue
        messages.append(
            SmsMessage(
                address=row.get("address", ""),
                body=row.get("body", ""),
                date=int(date),
                row=row["row"],
            )
        )
    return sorted(messages, key=lambda message: message.date, reverse=True)


def extract_otp(body: str) -> str | None:
    """Pull a likely one-time code out of a message body.

    **This is a heuristic and it does not know what a code is.** It returns the
    first run of 4 to 8 digits with neither a digit nor a letter against either
    end -- excluding a longer number and a digit run inside a word, nothing more
    -- so it cannot tell a code from an order number, an amount or a year.
    Callers must show the message it came from, which is what the CLI does.

    Args:
        body: Message text.

    Returns:
        The first matching digit run, or None when there is none.
    """
    match = OTP_PATTERN.search(body)
    return match.group(1) if match else None


class SmsTester:
    """Deliver inbound SMS to an emulator and read the inbox back."""

    def __init__(self, serial: str | None = None):
        """Initialize the tester (``serial`` None uses the default device)."""
        self.serial = serial

    # === PUBLIC API ===

    def list_inbox(self) -> tuple[bool, dict]:
        """Read ``content://sms/inbox``, newest first.

        Returns:
            (success, result_dict). Success means the query *ran*: an empty
            inbox is a successful read with ``count`` 0, a provider failure is
            not and carries the provider's own error text.
        """
        try:
            result = run_adb(
                "shell",
                self.serial,
                "content",
                "query",
                "--uri",
                INBOX_URI,
                "--projection",
                ":".join(PROJECTION),
                timeout=QUERY_TIMEOUT,
            )
        except AdbError as exc:
            return False, {"action": "list", "error": str(exc)}

        # `content query` exits 0 whatever happens: the answer is in which
        # stream said what, so the exit status is not consulted at all.
        if PROVIDER_ERROR in result.stderr:
            return False, {
                "action": "list",
                "uri": INBOX_URI,
                "error": (
                    f"could not read {INBOX_URI}: "
                    f"{result.stderr.strip().splitlines()[0]}. Note that "
                    f"`content query` exits 0 on this failure, so an empty "
                    f"inbox and an unreadable one look alike to a caller that "
                    f"checks the exit status."
                ),
                "stderr": result.stderr.strip(),
            }

        if EMPTY_RESULT in result.stdout:
            return True, {"action": "list", "uri": INBOX_URI, "count": 0, "messages": []}

        messages = parse_inbox(result.stdout)
        if not messages and result.stdout.strip():
            return False, {
                "action": "list",
                "uri": INBOX_URI,
                "error": (
                    "the inbox query printed output that is neither rows nor "
                    f"{EMPTY_RESULT!r}, so it was not parsed rather than being "
                    "reported as an empty inbox"
                ),
                "stdout": result.stdout.strip()[:500],
            }

        return True, {
            "action": "list",
            "uri": INBOX_URI,
            "count": len(messages),
            "messages": [_as_dict(message) for message in messages],
        }

    def send(self, to: str, body: str, verify: bool = True) -> tuple[bool, dict]:
        """Deliver an inbound SMS through the emulator console.

        Args:
            to: Sender to attribute the message to; unvalidated by the console.
            body: Message text.
            verify: Read the inbox back and require the message to appear before
                reporting success. With False, the only claim made is that the
                console accepted the command.

        Returns:
            (success, result_dict). ``accepted`` and ``delivered`` are separate
            keys and are never collapsed into one.
        """
        result = {
            "action": "send",
            "to": to,
            "body": body,
            "accepted": False,
            "delivered": False,
            "caveat": ACCEPTED_CAVEAT,
        }

        # Read the inbox BEFORE sending: two messages with the same sender and
        # body can coexist, so without a baseline an identical message from an
        # earlier run would be mistaken for this one. Pointless with
        # verify=False, which does no read-back to compare against.
        known: set[tuple] = set()
        if verify:
            baseline_read, baseline = self.list_inbox()
            if baseline_read:
                known = {tuple(message["identity"]) for message in baseline["messages"]}
            else:
                result["baseline_warning"] = (
                    "the inbox could not be read before sending "
                    f"({baseline.get('error', 'unknown error')}), so a pre-existing "
                    "identical message cannot be ruled out"
                )

        try:
            reply = run_emu("sms", "send", to, body, serial=self.serial)
        except EmuConsoleError as exc:
            result["error"] = str(exc)
            return False, result
        except AdbError as exc:
            result["error"] = str(exc)
            return False, result

        result["accepted"] = True
        result["console_reply"] = reply.payload or "OK"

        if not verify:
            result["note"] = (
                "Not verified (--no-verify). The console accepted the command; "
                "whether a message was delivered is unknown."
            )
            return True, result

        started = time.monotonic()
        deadline = started + VERIFY_TIMEOUT_SECONDS
        listed, listing = True, {}
        match = None
        while True:
            listed, listing = self.list_inbox()
            if listed:
                match = _find_new_message(listing["messages"], to, body, known)
                if match is not None:
                    break
            # The sleep is counted in, so a poll never *starts* after the
            # deadline. A poll already in flight finishes; each is separately
            # bounded by QUERY_TIMEOUT.
            if time.monotonic() + VERIFY_POLL_SECONDS >= deadline:
                break
            time.sleep(VERIFY_POLL_SECONDS)

        result["verify_seconds"] = round(time.monotonic() - started, 2)

        if not listed:
            result["error"] = (
                "the console accepted the command, but the inbox could not be "
                f"read back, so delivery is unknown: {listing.get('error', 'unknown error')}"
            )
            return False, result

        if match is None:
            result["inbox_count"] = listing["count"]
            result["error"] = (
                f"NOT delivered: the console answered OK, but no new message from "
                f"{to!r} with that body reached {INBOX_URI} within "
                f"{VERIFY_TIMEOUT_SECONDS}s (inbox holds {listing['count']} "
                f"message(s)). OK alone does not mean a message exists."
            )
            return False, result

        result["delivered"] = True
        result["message"] = match
        return True, result

    def newest_otp(self) -> tuple[bool, dict]:
        """Extract a one-time code from the **newest** inbox message.

        Only the newest message is examined; searching backwards would return a
        stale code. Extraction is the :func:`extract_otp` heuristic, so the
        message it came from is always reported with it.

        Returns:
            (found, result_dict).
        """
        listed, listing = self.list_inbox()
        if not listed:
            return False, {**listing, "action": "otp"}

        if not listing["messages"]:
            return False, {
                "action": "otp",
                "code": None,
                "error": f"the inbox is empty ({INBOX_URI} returned no rows)",
            }

        newest = listing["messages"][0]
        code = extract_otp(newest["body"])
        result = {
            "action": "otp",
            "code": code,
            "message": newest,
            "heuristic": (
                "first run of 4-8 digits not adjacent to another digit or "
                "letter, in the newest message only"
            ),
        }
        if code is None:
            result["error"] = (
                f"no 4-8 digit code found in the newest message (from "
                f"{newest['address']}): {newest['body']!r}"
            )
        return code is not None, result


def _as_dict(message: SmsMessage) -> dict:
    """Render a message for output, with the derived fields included."""
    return {**asdict(message), "received_at": message.received_at, "identity": message.identity}


def _addresses_match(sent: str, seen: str) -> bool:
    """Whether an inbox sender is the one a send was addressed to.

    Measured on API 35, the console stores the address exactly as given (even
    ``abc`` comes back verbatim), so the exact comparison is the one that
    matters; the digits-only fallback tolerates a platform that might normalise
    punctuation, rather than claiming one does.
    """
    if sent == seen:
        return True
    sent_digits = re.sub(r"\D", "", sent)
    seen_digits = re.sub(r"\D", "", seen)
    return bool(sent_digits) and sent_digits == seen_digits


def _find_new_message(messages: list[dict], to: str, body: str, known: set[tuple]) -> dict | None:
    """Find a message matching the send that the inbox did not already hold.

    Args:
        messages: Inbox messages, newest first.
        to, body: What was sent.
        known: Identities present before the send.

    Returns:
        The matching message, or None.
    """
    for message in messages:
        if tuple(message["identity"]) in known:
            continue
        if message["body"] == body and _addresses_match(to, message["address"]):
            return message
    return None


def _warn(message: str) -> None:
    """Write to stderr after flushing stdout, so the report reads in order."""
    sys.stdout.flush()
    print(message, file=sys.stderr)


def _print_send(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--send`` report."""
    if result.get("baseline_warning"):
        _warn(f"! {result['baseline_warning']}")

    if not result.get("accepted"):
        _warn(f"x Console rejected the send: {result.get('error', 'unknown error')}")
        return

    print(f"+ Console accepted `sms send` for {result['to']}")
    if not success:
        print(f"  {ACCEPTED_CAVEAT}")
        _warn(f"x {result.get('error', 'unknown error')}")
        return

    if result["delivered"]:
        message = result["message"]
        print(
            f"+ Delivered: {message['address']} -> {message['body']!r} ({result['verify_seconds']}s)"
        )
        if verbose:
            print(f"  Received at {message['received_at']} (date={message['date']})")
            print(f"  Read back from {INBOX_URI}")
    else:
        print(f"  {result.get('note', ACCEPTED_CAVEAT)}")


def _print_list(success: bool, result: dict, verbose: bool, limit: int | None = None) -> None:
    """Print the concise ``--list`` report."""
    if not success:
        _warn(f"x {result.get('error', 'unknown error')}")
        return

    messages = result["messages"]
    shown = messages if limit is None else messages[:limit]
    print(f"{result['count']} message(s) in {INBOX_URI}, newest first")
    for message in shown:
        body = message["body"] if verbose else _truncate(message["body"])
        print(f"  {message['received_at']}  {message['address']}  {body}")
    if len(shown) < len(messages):
        print(f"  ... {len(messages) - len(shown)} older (--limit to show more)")


def _print_otp(success: bool, result: dict, verbose: bool) -> None:
    """Print the concise ``--otp`` report."""
    if not success:
        _warn(f"x {result.get('error', 'unknown error')}")
        return
    message = result["message"]
    print(result["code"])
    print(f"  from {message['address']}: {message['body']!r}")
    if verbose:
        print(f"  received at {message['received_at']}")
    print(f"  Heuristic: {result['heuristic']} - check it against the message above.")


def _truncate(text: str, width: int = 60) -> str:
    """Shorten a body for the concise listing."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= width else f"{collapsed[: width - 1]}..."


def _emit(success: bool, result: dict, args: argparse.Namespace) -> None:
    """Render a result as JSON or as the concise human report."""
    if args.json:
        print(json.dumps({"success": success, **result}, indent=2))
        return
    action = result.get("action")
    if action == "send":
        _print_send(success, result, args.verbose)
    elif action == "otp":
        _print_otp(success, result, args.verbose)
    else:
        _print_list(success, result, args.verbose, args.limit)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Deliver an inbound SMS to an emulator, read the inbox back, and "
            "extract a one-time code from the newest message."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Why --send reads the inbox back:
  `adb emu sms send` answers a bare OK, and adb exits 0 even when the console
  answers KO. OK means the console accepted the command; it does not mean a
  message was delivered. The only proof is a message appearing in
  {INBOX_URI} that was not there before, which is what --send checks
  (skip it with --no-verify, and the report will say delivery is unknown).

  `adb emu` is emulator-only: a physical device has no console, so --send
  cannot work against one. --list and --otp are plain content queries.

Examples:
  python sms.py --send --to +15551234567 --body "Your code is 428193"
  python sms.py --send --to +15551234567 --body hi --no-verify --json
  python sms.py --list --limit 5
  python sms.py --list --json
  python sms.py --otp
  python sms.py --otp --json

Exit status:
  0  the action succeeded (for --send: the message was found in the inbox;
     for --otp: a code was extracted)
  1  it did not
        """,
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--send",
        action="store_true",
        help="Deliver an inbound SMS via the emulator console, then verify it landed",
    )
    action.add_argument(
        "--list",
        dest="list_inbox",
        action="store_true",
        help="List inbox messages, newest first",
    )
    action.add_argument(
        "--otp",
        action="store_true",
        help="Extract a one-time code from the newest message (exits 1 if there is none)",
    )

    parser.add_argument("--to", help="With --send: the sender to attribute the message to")
    parser.add_argument("--body", help="With --send: the message text")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="With --send: skip the inbox read-back and claim only that the console accepted it",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="With --list: how many messages to print (default: 10)",
    )
    parser.add_argument("--serial", help="Device serial (auto-detects if omitted)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show extended detail")
    return parser


def main() -> None:
    """Parse arguments, dispatch one action, and exit with its real status."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.send and not (args.to and args.body):
        parser.error("--send requires --to and --body")
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    tester = SmsTester(serial=serial)

    if args.send:
        success, result = tester.send(args.to, args.body, verify=not args.no_verify)
    elif args.otp:
        success, result = tester.newest_otp()
    else:
        success, result = tester.list_inbox()

    _emit(success, result, args)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
