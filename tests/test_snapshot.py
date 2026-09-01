"""What snapshot.py may claim, and how it is allowed to prove it.

The load-bearing fact, measured on emulator-5554 (API 35):

    $ adb emu avd snapshot load no_such_snapshot_xyz
    KO: Device 'encrypt' does not have the requested snapshot 'no_such_snapshot_xyz'
    KO: Snapshot load failure: snapshot doesn't exist
    $ echo $?
    0

A restore that did not happen exits 0. Anything branching on the exit status of
a raw ``adb emu`` call reports a successful load of a snapshot that does not
exist, and every assertion after it runs against unknown emulator state while
reporting a pass -- the same trap as ``am broadcast`` always printing
``result=0`` (see ``test_push_notification.py``). The central test here feeds the
*recorded* KO reply in with returncode 0 and requires a failure, at the API and
at the exit code.

Every piece of console output used below comes from ``tests/fixtures/recorded/``
through the ``recorded`` fixture. Where a case needs a row the recording does not
contain -- a second snapshot, a broken row -- it is *derived* from the recorded
row and round-tripped through the parser, never hand-written, so the field
layout under test is still ground truth.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import snapshot

from common import adb_exec

SOURCE = Path(snapshot.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

LIST_FIXTURE = "emu_avd_snapshot_list"
KO_FIXTURE = "emu_avd_snapshot_load_missing"


@pytest.fixture(autouse=True)
def _fast_verify_poll(monkeypatch):
    """Collapse the read-back deadline so negative tests do not wait it out."""
    monkeypatch.setattr(snapshot, "VERIFY_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(snapshot, "VERIFY_POLL_SECONDS", 0)


# ---------------------------------------------------------------------------
# Test doubles. subprocess is patched inside common.adb_exec rather than
# snapshot.py, so run_emu's real framing and KO handling run in every test --
# mocking run_emu itself would test the mock's idea of failure, not the console's.
# ---------------------------------------------------------------------------


def _contains(cmd: list[str], tokens: tuple[str, ...]) -> bool:
    """Whether ``tokens`` appears as a contiguous run inside ``cmd``."""
    span = len(tokens)
    return any(cmd[i : i + span] == list(tokens) for i in range(len(cmd) - span + 1))


class FakeAdb:
    """Routes adb invocations to canned results and records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._routes: list[tuple[tuple[str, ...], SimpleNamespace]] = []
        self.default = SimpleNamespace(returncode=0, stdout="OK\r\n", stderr="")

    def when(self, *tokens: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Answer any command containing ``tokens`` with this result."""
        self._routes.append(
            (tokens, SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))
        )

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        for tokens, response in self._routes:
            if _contains(list(cmd), tokens):
                return response
        return self.default

    def commands_matching(self, *tokens: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if _contains(cmd, tokens)]


@pytest.fixture
def fake_adb(monkeypatch) -> FakeAdb:
    """Patch subprocess.run underneath common.adb_exec with a scriptable fake."""
    fake = FakeAdb()
    monkeypatch.setattr(adb_exec.subprocess, "run", fake.run)
    return fake


def _run_main(monkeypatch, argv: list[str], serial: str | None = None) -> int:
    """Run the CLI in-process and return its exit code.

    An agent branches on the exit code, so the exit code is what gets asserted.
    """
    monkeypatch.setattr(snapshot.sys, "argv", ["snapshot.py", *argv])
    monkeypatch.setattr(snapshot, "resolve_device_identifier", lambda _serial: serial)
    with pytest.raises(SystemExit) as exit_info:
        snapshot.main()
    return exit_info.value.code


def _row_named(recorded, name: str) -> str:
    """The recorded table row with its TAG swapped for ``name``.

    NOT a recording of a second snapshot -- only one existed when the fixture was
    captured, and inventing a plausible second row is the mistake this directory
    exists to prevent. The field layout, separator and column order all come from
    ground truth; only the TAG differs, and the round-trip assertion below proves
    the result is something the parser genuinely accepts.
    """
    template = snapshot.parse_snapshot_table(recorded.text(LIST_FIXTURE))[0]
    line = template.raw.replace(template.name, name, 1)
    parsed = snapshot.parse_snapshot_table(line)
    assert len(parsed) == 1, f"derived row is not in the recorded shape: {line!r}"
    assert parsed[0].name == name
    assert (parsed[0].vm_size, parsed[0].created) == (template.vm_size, template.created)
    return line


def _listing_including(recorded, name: str) -> str:
    """The recorded listing with one extra row named ``name``."""
    text = recorded.text(LIST_FIXTURE)
    template = snapshot.parse_snapshot_table(text)[0]
    return text.replace(template.raw, f"{template.raw}\n{_row_named(recorded, name)}", 1)


# ---------------------------------------------------------------------------
# The table parser, against the recorded listing.
# ---------------------------------------------------------------------------


def test_parses_the_recorded_snapshot_table(recorded):
    """The parser matches the format it was actually written for."""
    snapshots = snapshot.parse_snapshot_table(recorded.text(LIST_FIXTURE))
    assert len(snapshots) == 1, "parser matched nothing against real console output"
    entry = snapshots[0]
    assert entry.name == "default_boot"
    assert entry.vm_size == "138M"
    assert entry.created == "2026-08-22 14:25:48"
    assert entry.vm_clock == "04:33:18.874"


def test_the_boot_snapshot_id_is_a_double_dash_and_the_row_survives_it(recorded):
    """The ID column is literally `--`. A parser keying on ID must tolerate it."""
    entry = snapshot.parse_snapshot_table(recorded.text(LIST_FIXTURE))[0]
    assert entry.snapshot_id == "--"
    assert (
        not entry.snapshot_id.isdigit()
    ), "the recorded ID is not a number; do not parse it as one"


def test_the_recorded_id_really_is_not_numeric(recorded):
    """Guard the guard: if the fixture ever gained a numeric ID, the test above
    would keep passing while no longer proving anything."""
    id_column = recorded.lines(LIST_FIXTURE)[2].split()[0]
    assert id_column == "--"


def test_framing_lines_are_not_mistaken_for_snapshots(recorded):
    """Title, header and the console's OK are framing, not rows."""
    names = {item.name for item in snapshot.parse_snapshot_table(recorded.text(LIST_FIXTURE))}
    assert names == {"default_boot"}
    assert "TAG" not in names and "OK" not in names


def test_the_console_ok_is_tolerated_whether_or_not_run_emu_stripped_it(recorded):
    """The same text parses identically framed and unframed.

    run_emu removes the trailing OK; a fixture read straight off disk still has
    it. Both must give the same answer or the tests and the runtime disagree.
    """
    text = recorded.text(LIST_FIXTURE)
    unframed = "\n".join(line for line in text.splitlines() if line.strip() != "OK")
    assert snapshot.parse_snapshot_table(text) == snapshot.parse_snapshot_table(unframed)


def test_nothing_in_the_recorded_listing_is_left_unrecognised(recorded):
    """Every line is either known framing or a parsed row."""
    assert snapshot.unrecognised_lines(recorded.text(LIST_FIXTURE)) == []


def test_a_wider_tag_does_not_shift_the_size_out_from_under_the_parser(recorded):
    """Fields are read from the right, so the columns need not line up.

    In the recording the values do not sit under their headers -- VM SIZE is
    right-aligned and ends past the end of `VM SIZE` -- so header-offset slicing
    is already wrong for the one row that exists. This derives a row with a
    longer TAG to pin that the parser stays positional-from-the-right.
    """
    header = recorded.lines(LIST_FIXTURE)[1]
    row = recorded.lines(LIST_FIXTURE)[2]
    assert header.index("VM SIZE") != row.index("138M"), (
        "the recorded columns now line up with their headers; this test's premise "
        "needs rechecking against the new recording"
    )
    parsed = snapshot.parse_snapshot_table(_row_named(recorded, "a_considerably_longer_name"))[0]
    assert (parsed.vm_size, parsed.created) == ("138M", "2026-08-22 14:25:48")


def test_an_unparsable_row_is_surfaced_not_dropped(recorded):
    """A format we do not understand must not read as "no snapshots".

    The mangled line is deliberately not a recording: it stands in for a future
    format nobody has seen. The assertion is about behaviour on unknown input,
    not about what any device prints.
    """
    text = recorded.text(LIST_FIXTURE)
    template = snapshot.parse_snapshot_table(text)[0]
    mangled = text.replace(template.raw, template.raw.replace("2026-08-22", ""), 1)
    assert snapshot.parse_snapshot_table(mangled) == []
    assert snapshot.unrecognised_lines(mangled), "an unreadable row vanished silently"


def test_list_reports_the_reply_when_it_parsed_nothing(recorded, fake_adb):
    """`--list` must show what the console said rather than assert emptiness."""
    text = recorded.text(LIST_FIXTURE)
    template = snapshot.parse_snapshot_table(text)[0]
    mangled = text.replace(template.raw, template.raw.replace("2026-08-22", ""), 1)
    fake_adb.when("snapshot", "list", stdout=mangled)

    success, result = snapshot.SnapshotManager().list_snapshots()
    assert success and result["count"] == 0
    assert result["unrecognised"], "unreadable lines were not reported"
    assert "reply" in result, "the raw console reply was withheld"


# ---------------------------------------------------------------------------
# The whole point: `adb emu` exits 0 when it fails.
# ---------------------------------------------------------------------------


def test_the_recorded_failure_is_a_ko_with_no_ok(recorded):
    """Guard the guard: the fixture must really be a rejection."""
    text = recorded.text(KO_FIXTURE)
    assert text.startswith("KO:")
    assert "OK" not in text.replace("KO", ""), "the recorded rejection also acknowledges?"


def test_a_failed_load_is_not_reported_as_success(recorded, fake_adb):
    """The recorded KO, delivered with returncode 0, must be a failure.

    This is the test the script exists for. `adb emu` exited 0 on the real
    emulator when this was recorded, so the fake returns 0 too.
    """
    fake_adb.when("snapshot", "load", returncode=0, stdout=recorded.text(KO_FIXTURE))
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))

    success, result = snapshot.SnapshotManager().load("no_such_snapshot_xyz")

    assert success is False, "a KO reply was read as a successful load"
    assert result["verified"] is False
    assert "error" in result


def test_cli_exits_non_zero_on_the_recorded_failed_load(recorded, fake_adb, monkeypatch):
    """An agent branches on the exit code; adb's own is 0 here."""
    fake_adb.when("snapshot", "load", returncode=0, stdout=recorded.text(KO_FIXTURE))
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))

    assert _run_main(monkeypatch, ["--load", "no_such_snapshot_xyz"]) == 1


def test_the_failed_load_really_did_exit_zero(recorded, fake_adb, monkeypatch):
    """Pin the trap itself: had the script trusted returncode, it would pass."""
    fake_adb.when("snapshot", "load", returncode=0, stdout=recorded.text(KO_FIXTURE))
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    _run_main(monkeypatch, ["--load", "no_such_snapshot_xyz"])

    load_calls = fake_adb.commands_matching("snapshot", "load")
    assert load_calls, "no load was attempted"
    assert fake_adb.run(load_calls[0]).returncode == 0


def test_a_failed_load_names_the_snapshots_that_do_exist(recorded, fake_adb):
    """For an agent, the error message is the retry prompt."""
    fake_adb.when("snapshot", "load", returncode=0, stdout=recorded.text(KO_FIXTURE))
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))

    _success, result = snapshot.SnapshotManager().load("no_such_snapshot_xyz")
    assert "default_boot" in result["error"]


def test_a_successful_load_does_not_claim_verification(fake_adb):
    """`load` answers OK and describes nothing; the result must say so."""
    success, result = snapshot.SnapshotManager().load("default_boot")
    assert success is True
    assert result["verified"] is False
    assert "not been measured" in result["note"] or "restored" in result["note"]


def test_a_ko_on_any_snapshot_command_is_a_failure(recorded, fake_adb):
    """The recording is a *load* rejection; the framing is the console's, not
    the sub-command's, so a KO must fail a save too."""
    fake_adb.when("snapshot", "save", returncode=0, stdout=recorded.text(KO_FIXTURE))
    success, result = snapshot.SnapshotManager().save("before_upgrade")
    assert success is False
    assert "error" in result


# ---------------------------------------------------------------------------
# Save and delete are verified by read-back, not by the reply.
# ---------------------------------------------------------------------------


def test_save_succeeds_only_once_the_snapshot_is_actually_listed(recorded, fake_adb):
    fake_adb.when("snapshot", "list", stdout=_listing_including(recorded, "before_upgrade"))
    success, result = snapshot.SnapshotManager().save("before_upgrade")
    assert success is True
    assert result["verified"] is True


def test_save_fails_when_the_snapshot_never_appears(recorded, fake_adb):
    """The console said OK; the listing does not have it. That is not a success."""
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    success, result = snapshot.SnapshotManager().save("before_upgrade")
    assert success is False
    assert result["verified"] is False
    assert "did not happen" in result["error"] or "no snapshot named" in result["error"]


def test_no_verify_says_that_it_did_not_verify(recorded, fake_adb):
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    success, result = snapshot.SnapshotManager().save("before_upgrade", verify=False)
    assert success is True
    assert result["verified"] is False
    assert "not verified" in result["note"].lower()
    assert not fake_adb.commands_matching("snapshot", "list"), "read-back ran despite --no-verify"


def test_delete_is_verified_by_absence(recorded, fake_adb):
    """Whether `delete` KOs for a missing name is unmeasured, so absence is
    what gets checked."""
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    success, result = snapshot.SnapshotManager().delete("before_upgrade")
    assert success is True and result["verified"] is True


def test_delete_fails_when_the_snapshot_is_still_listed(recorded, fake_adb):
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    success, result = snapshot.SnapshotManager().delete("default_boot")
    assert success is False
    assert "still listed" in result["error"]


def test_cli_exit_codes_follow_the_read_back(recorded, fake_adb, monkeypatch):
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    assert _run_main(monkeypatch, ["--save", "before_upgrade"]) == 1
    assert _run_main(monkeypatch, ["--delete", "default_boot"]) == 1
    assert _run_main(monkeypatch, ["--list"]) == 0


# ---------------------------------------------------------------------------
# Names reach the console as arguments.
# ---------------------------------------------------------------------------


def test_the_emulators_own_boot_snapshot_passes_the_name_rule(recorded):
    """A rule that rejects `default_boot` would make the tool useless."""
    for item in snapshot.parse_snapshot_table(recorded.text(LIST_FIXTURE)):
        assert snapshot.validate_snapshot_name(item.name) is None, item.name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a b",
        "../escape",
        "dir/snap",
        "-x",
        ".hidden",
        "snap;rm -rf /",
        "snap$(id)",
        "snap\nload other",
        "x" * 65,
    ],
)
def test_unsafe_names_are_rejected(name):
    assert snapshot.validate_snapshot_name(name) is not None, f"{name!r} was accepted"


@pytest.mark.parametrize("name", ["default_boot", "before_upgrade", "clean-state.1", "A1"])
def test_ordinary_names_are_accepted(name):
    assert snapshot.validate_snapshot_name(name) is None


@pytest.mark.parametrize("action", ["--save", "--load", "--delete"])
def test_a_rejected_name_never_reaches_the_console(fake_adb, monkeypatch, action):
    assert _run_main(monkeypatch, [action, "../escape"]) == 1
    assert fake_adb.calls == [], "an unsafe name was sent to the emulator anyway"


# ---------------------------------------------------------------------------
# The commands issued, and the entry point they go through.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "tokens"),
    [
        (["--list"], ("avd", "snapshot", "list")),
        (
            ["--save", "before_upgrade", "--no-verify"],
            ("avd", "snapshot", "save", "before_upgrade"),
        ),
        (["--load", "default_boot"], ("avd", "snapshot", "load", "default_boot")),
        (["--delete", "gone", "--no-verify"], ("avd", "snapshot", "delete", "gone")),
    ],
)
def test_the_measured_console_commands_are_what_get_issued(fake_adb, monkeypatch, argv, tokens):
    """`adb emu avd snapshot {list,save,load,delete}` -- all four confirmed
    present in the console's own sub-command list on API 35."""
    _run_main(monkeypatch, argv)
    assert fake_adb.commands_matching(*tokens), f"{tokens} was never issued: {fake_adb.calls}"


def test_the_serial_is_passed_through_to_adb(fake_adb, monkeypatch):
    _run_main(monkeypatch, ["--list", "--serial", "emulator-5554"], serial="emulator-5554")
    assert fake_adb.calls[0][:5] == ["adb", "-s", "emulator-5554", "emu", "avd"]


def test_the_serial_is_resolved_through_the_shared_helper(fake_adb, monkeypatch):
    """CLAUDE.md: resolution goes through resolve_device_identifier."""
    seen: list[str | None] = []

    def _resolve(identifier):
        seen.append(identifier)
        return "emulator-5556"

    monkeypatch.setattr(snapshot, "resolve_device_identifier", _resolve)
    monkeypatch.setattr(snapshot.sys, "argv", ["snapshot.py", "--list", "--serial", "emulator"])
    with pytest.raises(SystemExit):
        snapshot.main()
    assert seen == ["emulator"]
    assert fake_adb.calls[0][:3] == ["adb", "-s", "emulator-5556"]


def test_every_console_call_goes_through_run_emu():
    """No hand-rolled `adb emu`: the KO check lives in one place on purpose."""
    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
    }
    assert "run_emu" in called, "the module does not use the console entry point at all"
    for forbidden in ("run_adb", "build_adb_command", "build_command", "Popen"):
        assert forbidden not in called, f"{forbidden} bypasses run_emu's KO check"


def test_the_module_does_not_import_subprocess():
    """Anything spawning its own process would sidestep the reply check."""
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(TREE)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "subprocess" not in imported


def test_no_console_call_disables_the_ko_check():
    """`run_emu(check=False)` would restore exactly the defect being fixed."""
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "run_emu":
            continue
        for keyword in node.keywords:
            assert keyword.arg != "check", f"run_emu(check=...) at line {node.lineno}"


def test_console_calls_are_bounded(fake_adb, monkeypatch):
    """An unbounded adb call wedges the connection for whatever runs next."""
    _run_main(monkeypatch, ["--list"])
    assert fake_adb.kwargs, "no adb call was made"
    for kwargs in fake_adb.kwargs:
        assert kwargs.get("timeout"), f"unbounded adb call: {kwargs}"
        assert kwargs.get("shell") is not True


def test_a_missing_adb_is_a_failure_not_a_pass(monkeypatch):
    """An exception on the way out must not look like a restored snapshot."""

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("adb")

    monkeypatch.setattr(adb_exec.subprocess, "run", _boom)
    success, result = snapshot.SnapshotManager().load("default_boot")
    assert success is False
    assert "adb" in result["error"].lower()


def test_a_hung_console_is_a_failure_not_a_pass(monkeypatch):
    def _hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="adb", timeout=1)

    monkeypatch.setattr(adb_exec.subprocess, "run", _hang)
    success, result = snapshot.SnapshotManager().load("default_boot")
    assert success is False
    assert result["error"]


# ---------------------------------------------------------------------------
# Output surfaces.
# ---------------------------------------------------------------------------


def test_json_output_is_machine_readable(recorded, fake_adb, monkeypatch, capsys):
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    assert _run_main(monkeypatch, ["--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert [item["name"] for item in payload["snapshots"]] == ["default_boot"]
    assert payload["snapshots"][0]["snapshot_id"] == "--"


def test_json_output_of_a_failed_load_says_it_failed(recorded, fake_adb, monkeypatch, capsys):
    fake_adb.when("snapshot", "load", returncode=0, stdout=recorded.text(KO_FIXTURE))
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    assert _run_main(monkeypatch, ["--load", "nope", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["action"] == "load"


def test_concise_output_names_the_snapshots(recorded, fake_adb, monkeypatch, capsys):
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    _run_main(monkeypatch, ["--list"])
    out = capsys.readouterr().out
    assert "default_boot" in out
    assert "138M" in out


def test_a_failure_is_reported_on_stderr(recorded, fake_adb, monkeypatch, capsys):
    fake_adb.when("snapshot", "load", returncode=0, stdout=recorded.text(KO_FIXTURE))
    fake_adb.when("snapshot", "list", stdout=recorded.text(LIST_FIXTURE))
    _run_main(monkeypatch, ["--load", "nope"])
    captured = capsys.readouterr()
    assert "Load failed" in captured.err
    assert captured.out == ""


def test_help_warns_that_adb_emu_exits_zero_on_failure(scripts_dir):
    """The trap has to be visible to whoever reads --help, not just the source."""
    result = subprocess.run(
        [sys.executable, str(scripts_dir / "snapshot.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    text = result.stdout.lower()
    assert "exits 0" in text
    assert "not verified" in text or "not verify" in text


# ---------------------------------------------------------------------------
# Against a real emulator. Read-only: nothing here writes or restores state.
# ---------------------------------------------------------------------------


@pytest.mark.emulator
def test_live_listing_is_usable(emulator_only_device):
    """Semantic floor: did the agent get an answer it can act on?"""
    success, result = snapshot.SnapshotManager(serial=emulator_only_device).list_snapshots()
    assert success, result.get("error")
    assert result.get("unrecognised", []) == [], "the live table has lines the parser cannot read"
    if not result["snapshots"]:
        # Not a failure -- an emulator may genuinely have none -- but the reply
        # must be shown rather than reported as a confident empty list.
        assert result.get("reply") is not None, "an empty answer came with no evidence"
    for item in result["snapshots"]:
        assert snapshot.validate_snapshot_name(item["name"]) is None
        assert item["vm_size"]


@pytest.mark.emulator
def test_live_load_of_a_missing_snapshot_fails(emulator_only_device):
    """The defect, against the real console. Loading a name that cannot exist
    changes nothing on the device."""
    success, result = snapshot.SnapshotManager(serial=emulator_only_device).load(
        "no_such_snapshot_xyz_from_tests"
    )
    assert success is False, "the real console's KO was read as a successful load"
    assert "error" in result


def test_an_emulator_with_no_snapshots_is_empty_not_unreadable(recorded):
    """A fresh emulator answers with a sentence, not a table.

    CI found this on the emulator lane's first successful run: a runner's AVD
    has no snapshots, while the dev machine's had `default_boot`, so the empty
    case never occurred locally. `There is no snapshot available.` was reported
    as a line the parser could not read -- which reads as "the format changed",
    the loudest possible way to say "there is nothing here".
    """
    text = recorded.text("emu_avd_snapshot_list_empty")

    assert snapshot.parse_snapshot_table(text) == []
    assert (
        snapshot.unrecognised_lines(text) == []
    ), "the no-snapshots sentence is being reported as unparsed output"
