"""Device-free tests for container.py pure logic.

Three layers, all adb/subprocess-free unless explicitly monkeypatched:

1. ``parse_listing`` turns the listing command's NUL-terminated records into
   structured entries (common.run_as; container.py re-exports it).
2. ``parse_shared_prefs_xml`` turns a shared_prefs XML doc into typed key/values.
3. ``parse_sqlite_schema`` turns ``.schema`` text into per-table column metadata.

Plus command-construction assertions for ``run-as`` (debuggable mechanism) and
denial detection for release / non-debuggable apps, with ``subprocess.run``
mocked. No real device is ever contacted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import container
import pytest
from conftest import RECORDED_ROOT, RecordedFixtures
from container import (
    ContainerInspector,
    RunAsDeniedError,
    parse_listing,
    parse_shared_prefs_xml,
    parse_sqlite_schema,
)
from fake_toolchain import (
    COLLIDING_FILE_NAMES,
    SYSTEM_PATH,
    assert_refusal,
    recorded_missing_file,
)

from common import cli, run_as
from common.device_utils import quote_for_device_shell
from common.run_as import ListingError

# The real client check, kept before the autouse fixture below replaces it.
_REAL_ADB_CLIENT_VERSION = container.adb_client_version


@pytest.fixture(autouse=True)
def _the_recorded_adb_client(monkeypatch, recorded):
    """Every read first asks `adb version`; answer it with the recorded banner.

    The check itself is exercised against the real function (``_real_client_check``)
    by the tests that pin it; everywhere else it is the recorded client's version.
    """
    version = container.parse_adb_version(recorded.text("adb_version"))
    assert version is not None, "the recorded banner no longer parses"
    monkeypatch.setattr(container, "adb_client_version", lambda: version)


# === parse_listing ===
#
# Every listing below is a recording of the command the scripts run
# (common.run_as.listing_command): toybox find, six NUL-terminated fields per
# record -- mode, owner, group, size, symlink target, name -- the directory's
# own record first, with an empty name. A derived listing changes ONE field of
# one recorded record (see `_derived`); NUL framing makes a field a field, so
# the substitution touches nothing else.

FIXTURE_PKG = "com.example.composefixture"
FIXTURE_DIR = f"/data/data/{FIXTURE_PKG}"

# The recordings of the listing command, and the directory each one listed.
LISTED = {
    "run_as_find_data_dir": FIXTURE_DIR,
    "run_as_find_data_dir_bare": FIXTURE_DIR,
    "run_as_find_databases": f"{FIXTURE_DIR}/databases",
    "run_as_find_shared_prefs": f"{FIXTURE_DIR}/shared_prefs",
    "run_as_find_names": f"{FIXTURE_DIR}/files/aes_names",
    "run_as_find_names_dirlink": f"{FIXTURE_DIR}/files/aes_names/dirlink",
    "run_as_find_not_a_directory": f"{FIXTURE_DIR}/shared_prefs/fixture_settings.xml",
}

# The name the recorded scratch directory's forging file has: `x.db`, a newline,
# and a complete `ls -la` line for ghost.db (run_as_find_names, run_as_ls_names).
FORGED = "x.db\n-rw-rw---- 1 u0_a205 u0_a205 32768 2026-10-18 22:28 ghost.db"


def _listing(recorded, name: str) -> bytes:
    """A recorded listing's stdout, byte for byte."""
    return recorded.data(f"{name}.txt")


def _records(listing: bytes) -> list[list[bytes]]:
    """A recorded listing's records: its NUL-terminated fields, six at a time."""
    fields = listing.split(b"\0")
    assert fields[-1] == b"" and (len(fields) - 1) % 6 == 0, "not whole six-field records"
    return [fields[at : at + 6] for at in range(0, len(fields) - 1, 6)]


def _joined(records: list[list[bytes]]) -> bytes:
    """Records back into the listing's bytes: every field NUL-terminated."""
    return b"".join(field + b"\0" for record in records for field in record)


def _derived(
    recorded,
    value: bytes,
    *,
    field: int = 5,
    of: bytes = b"fixture.db-journal",
    name: str = "run_as_find_databases",
) -> bytes:
    """Recording ``name`` with ONE field of the record named ``of`` replaced by ``value``.

    Field 5 is the name, 4 the symlink target, 3 the size, 0 the mode; every
    other byte is the device's.
    """
    records = _records(_listing(recorded, name))
    (record,) = [record for record in records if record[5] == of]
    record[field] = value
    return _joined(records)


def test_every_recording_is_of_the_command_the_scripts_run(recorded):
    """Anti-vacuity: each listing recording is of listing_command, quoted as the scripts quote it.

    And each is what the parser takes it for: whole records of six fields, the
    directory's own first with an empty name, no other record with one.
    """
    commands = {f["name"]: f["command"] for f in recorded.manifest["fixtures"]}
    for name, directory in LISTED.items():
        argv = [quote_for_device_shell(arg) for arg in run_as.listing_command(directory)]
        assert commands[name] == f"adb shell run-as {FIXTURE_PKG} " + " ".join(argv), name
        own, *children = _records(_listing(recorded, name))
        assert own[5] == b"" and all(record[5] for record in children), name


def test_parse_listing_basic_entries(recorded):
    """Kinds, sizes, owners and groups off a real listing of the data dir.

    The cache dirs have a group (u0_a205_cache) that is not the owner, and
    find prints the size of a directory as ls did.
    """
    listing = parse_listing(_listing(recorded, "run_as_find_data_dir"))
    assert listing.rejected == [], "a real recording had an entry refused"
    entries = {e["name"]: e for e in listing.entries}
    assert set(entries) == {"cache", "code_cache", "databases", "files", "shared_prefs"}
    assert entries["cache"]["kind"] == "dir"
    assert entries["cache"]["size_bytes"] == 4096
    assert entries["cache"]["owner"] == "u0_a205"
    assert entries["cache"]["group"] == "u0_a205_cache"

    files = {
        e["name"]: e for e in parse_listing(_listing(recorded, "run_as_find_databases")).entries
    }
    assert files["fixture.db"]["kind"] == "file"
    assert files["fixture.db"]["size_bytes"] == 32768
    assert files["fixture.db"]["owner"] == "u0_a205"
    assert files["fixture.db"]["symlink_target"] is None


def test_the_directorys_own_record_is_not_an_entry_and_entries_are_sorted(recorded):
    """find prints the directory first (empty name), then its entries in directory order."""
    raw = _listing(recorded, "run_as_find_data_dir")
    printed = [record[5].decode() for record in _records(raw)[1:]]
    assert printed != sorted(printed), "the recording no longer shows find's own order"

    names = [e["name"] for e in parse_listing(raw).entries]
    assert names == sorted(printed)
    assert "" not in names


# ---------------------------------------------------------------------------
# The final review's forgery, recorded (REQUIRED 4). A scratch directory in the
# fixture app's files/ held `x.db` + LF + a whole `ls -la` line for ghost.db,
# `c\d.db`, `e f.db`, `link -> e f.db`, `lflink` whose target holds a LF,
# `sub/` (holding `inner`) and `dirlink -> sub`. Through `ls -la`
# (run_as_ls_names) the forged line is a well-formed
# entry for a file that does not exist; through find (run_as_find_names) it is
# inside one name, which is refused as itself.
# ---------------------------------------------------------------------------


def test_the_ls_recording_of_the_same_directory_holds_the_forgery(recorded):
    """Anti-vacuity: `ls -la` printed ghost.db as an entry line of its own."""
    lines = recorded.text("run_as_ls_names").split("\n")
    (ghost,) = [line for line in lines if line.endswith(" ghost.db")]
    assert ghost.startswith("-rw-rw---- 1 "), ghost
    assert FORGED.split("\n", 1)[0] in [line.rsplit(" ", 1)[-1] for line in lines]


def test_a_newline_in_a_name_is_one_refused_name_not_a_forged_entry(recorded):
    """The forged line stays inside x.db's name; no entry is ghost.db, and x.db is not split."""
    listing = parse_listing(_listing(recorded, "run_as_find_names"))
    names = {e["name"] for e in listing.entries}

    assert "ghost.db" not in names and "x.db" not in names, names
    assert repr(FORGED) in listing.rejected, listing.rejected


def test_names_and_targets_are_kept_whole(recorded):
    """Spaced names, symlinks and their targets, a subdirectory -- and what is refused.

    `c\\d.db` (a separator on a Windows host) and `lflink`, whose TARGET holds
    a newline, are refused and reported, each as itself.
    """
    listing = parse_listing(_listing(recorded, "run_as_find_names"))
    entries = {e["name"]: e for e in listing.entries}

    assert set(entries) == {"e f.db", "link", "sub", "dirlink"}, entries
    assert (entries["dirlink"]["kind"], entries["dirlink"]["symlink_target"]) == ("symlink", "sub")
    link = entries["link"]
    assert (link["kind"], link["symlink_target"]) == ("symlink", "e f.db")
    assert link["mode"].startswith("l")
    # A symlink's size is the length of its target, as ls printed it.
    assert link["size_bytes"] == len("e f.db")
    spaced = entries["e f.db"]
    assert (spaced["kind"], spaced["symlink_target"], spaced["size_bytes"]) == ("file", None, 0)
    assert entries["sub"]["kind"] == "dir"
    assert listing.rejected == [repr("c\\d.db"), repr("lflink -> dangling\ntarget"), repr(FORGED)]


def test_a_symlinked_directory_is_listed_as_its_target(recorded):
    """`find -H <dir>/dirlink`: the named link is followed, as `ls sub` would list.

    The directory's own record is a directory's (mode d) and still carries the
    link's target; `inner`, the one file in sub/, is the listing. Without -H
    find printed the link's own record alone -- not a directory.
    """
    raw = _listing(recorded, "run_as_find_names_dirlink")
    own = _records(raw)[0]
    assert own[0].startswith(b"d") and (own[4], own[5]) == (b"sub", b""), own

    listing = parse_listing(raw)

    assert [e["name"] for e in listing.entries] == ["inner"]
    assert listing.rejected == []


# ---------------------------------------------------------------------------
# What is refused whole: anything that is not the listing command's records.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "run_as_ls_names",
        "run_as_ls_databases",
        "run_as_ls_data_dir_seeded",
        "run_as_ls_shared_prefs",
        "run_as_ls_symlink_space",
        "run_as_ls_1_databases",
    ],
)
def test_ls_output_is_refused_whole_not_misread(recorded, name):
    """The old transport's answer, recorded: its names are not delimited, so nothing in it is read."""
    with pytest.raises(ListingError) as excinfo:
        parse_listing(recorded.text(name).encode("utf-8"))
    assert not excinfo.value.not_a_directory


def test_a_listing_cut_short_is_refused_whole(recorded):
    """Derived: the recording with its final NUL, or its whole last field, cut off -- or nothing."""
    raw = _listing(recorded, "run_as_find_databases")
    last = _records(raw)[-1][5] + b"\0"
    for cut in (raw[:-1], raw[: -len(last)], b""):
        with pytest.raises(ListingError):
            parse_listing(cut)


def test_a_nul_in_a_name_cannot_shift_the_fields(recorded):
    """A NUL cannot be in a name; one that appears where a name is adds a field, and the count refuses it.

    Derived: run_as_find_databases with the journal's name replaced by `a` NUL `b`.
    """
    with pytest.raises(ListingError) as excinfo:
        parse_listing(_derived(recorded, b"a\0b"))
    assert "fields" in str(excinfo.value)


def test_a_find_that_stopped_at_an_escape_it_lacks_is_refused(recorded):
    """run_as_find_unknown_escape: five fields of the directory's record, then exit 1."""
    with pytest.raises(ListingError):
        parse_listing(_listing(recorded, "run_as_find_unknown_escape"))


def test_a_listing_of_a_file_is_not_an_empty_directory(recorded):
    """run_as_find_not_a_directory: one record, the file's own. Not "0 entries"."""
    raw = _listing(recorded, "run_as_find_not_a_directory")
    ((mode, *_rest),) = _records(raw)
    assert mode.startswith(b"-"), "the recording is not of a file"

    with pytest.raises(ListingError) as excinfo:
        parse_listing(raw)
    assert excinfo.value.not_a_directory


# ---------------------------------------------------------------------------
# A file name is chosen by the app, and export() makes host paths of the names
# it lists: they are untrusted input to the host. `/` and NUL cannot be in a
# real entry; `\` is a separator on a Windows host and IS legal in a Linux file
# name; `.`/`..` walk the tree (find never prints either for an entry); a CR or
# LF reaches a terminal raw.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape", "..\\escape.xml", "evil\rname", "\rlead", "trail\r", "x\ny.db", ".", ".."],
    ids=[
        "dotdot-slash",
        "dotdot-backslash",
        "inner-cr",
        "leading-cr",
        "trailing-cr",
        "lf",
        "dot",
        "dotdot",
    ],
)
def test_a_name_that_is_not_a_plain_file_name_is_refused_and_reported(recorded, name):
    """Derived (see `_derived`): the journal renamed. Refused as itself, never dropped or split."""
    listing = parse_listing(_derived(recorded, name.encode()))

    assert [e["name"] for e in listing.entries] == ["fixture.db"], listing.entries
    assert listing.rejected == [repr(name)], listing.rejected


def test_a_name_that_is_not_utf8_is_refused_not_replaced(recorded):
    """`evil\\xff.db`: decoded with replacement it became `evil\\ufffd.db`, a name no file has."""
    listing = parse_listing(_derived(recorded, b"evil\xff.db"))

    assert [e["name"] for e in listing.entries] == ["fixture.db"]
    assert listing.rejected == [repr(b"evil\xff.db")]


@pytest.mark.parametrize(
    ("field", "value"),
    [(0, b"??????????"), (3, b"1,   3"), (4, b"elsewhere")],
    ids=["mode", "size", "target-on-a-file"],
)
def test_a_record_find_does_not_print_is_refused(recorded, field, value):
    """Derived: one field of the journal's record replaced -- a mode that is not ls's, a
    device node's `major, minor` size, a target on a file. find prints none of these."""
    listing = parse_listing(_derived(recorded, value, field=field))

    assert [e["name"] for e in listing.entries] == ["fixture.db"]
    assert listing.rejected == [repr("fixture.db-journal")]


@pytest.mark.parametrize(
    ("field", "value", "refused"),
    [
        (4, b"e f.db\r", repr("link -> e f.db\r")),
        (5, b"link\r", repr("link\r")),
        (4, b"\xff", repr(b"link -> \xff")),
    ],
    ids=["target-cr", "name-cr", "target-not-utf8"],
)
def test_a_symlink_is_refused_for_its_target_or_its_name(recorded, field, value, refused):
    """Derived: run_as_find_names's `link -> e f.db` record with its target or name replaced."""
    raw = _derived(recorded, value, field=field, of=b"link", name="run_as_find_names")
    listing = parse_listing(raw)

    assert {e["name"] for e in listing.entries} == {"e f.db", "sub", "dirlink"}
    # The recording's own three refusals, and the derived one in its name's
    # place: refusals are in the order of the names' bytes, as entries are.
    expected = [repr("c\\d.db"), repr("lflink -> dangling\ntarget"), refused, repr(FORGED)]
    assert listing.rejected == expected, listing.rejected


def test_only_a_nul_ends_a_name(recorded):
    """`str.splitlines` breaks on U+2028 too; a name holding one is one entry with that name."""
    name = "x forged.xml"
    listing = parse_listing(_derived(recorded, name.encode()))

    assert sorted(e["name"] for e in listing.entries) == ["fixture.db", name]
    assert listing.rejected == []


# ---------------------------------------------------------------------------
# The same, through a real subprocess. `text=True` on the adb call applied
# universal newlines: a CR in a name became a line break BEFORE the parser saw
# it. These run the real transport against an `adb` that writes exact bytes --
# not tests/fake_toolchain.py's stub, which reads and writes text and would
# translate the CR itself.
# ---------------------------------------------------------------------------

_BYTES_ADB = """#!{python}
# Fake adb for test_container.py. Logs argv; the first rule whose tokens are
# all in argv answers: stdout and stderr byte for byte, then its exit status.
import json
import sys

with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
for rule in spec["rules"]:
    if all(token in argv for token in rule["match"]):
        for stream, key in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
            with open(rule[key], "rb") as handle:
                stream.buffer.write(handle.read())
        sys.exit(rule["exit"])
sys.exit(1)
"""


def _routed_adb(tmp_path: Path, monkeypatch, rules) -> Path:
    """Put a byte-exact fake `adb` first on PATH; return its argv log.

    ``rules``: ``(tokens, stdout, stderr, exit)`` tuples, first match wins.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "adb.log"
    staged = []
    for index, (tokens, out, err, status) in enumerate(rules):
        out_path, err_path = tmp_path / f"rule{index}.out", tmp_path / f"rule{index}.err"
        out_path.write_bytes(out)
        err_path.write_bytes(err)
        staged.append(
            {
                "match": list(tokens),
                "stdout": str(out_path),
                "stderr": str(err_path),
                "exit": status,
            }
        )
    spec = tmp_path / "adb.spec.json"
    spec.write_text(json.dumps({"log": str(log), "rules": staged}), encoding="utf-8")
    stub = bin_dir / "adb"
    stub.write_text(_BYTES_ADB.format(python=sys.executable, spec=str(spec)))
    stub.chmod(0o755)
    path = os.pathsep.join([str(bin_dir), *SYSTEM_PATH])
    assert shutil.which("adb", path=path) == str(stub), "a real adb is reachable"
    monkeypatch.setenv("PATH", path)
    log.touch()
    return log


def _bytes_adb(tmp_path: Path, monkeypatch, payload: bytes) -> Path:
    """A byte-exact fake `adb` that answers every call with ``payload``, exit 0."""
    return _routed_adb(tmp_path, monkeypatch, [((), payload, b"", 0)])


def _listed(argv: list[str]) -> str | None:
    """The directory an adb argv lists, when it ends in the WHOLE listing command; else None.

    The whole command, quoted as the scripts quote it -- not just `find` and a
    path -- so every test that finds a call this way also pins its argv.
    """
    if "find" not in argv or argv.index("find") + 2 >= len(argv):
        return None
    at = argv.index("find")
    directory = argv[at + 2]
    command = [quote_for_device_shell(arg) for arg in run_as.listing_command(directory)]
    return directory if argv[at:] == command else None


@pytest.mark.parametrize(
    "name", ["x\rforged.xml", "\rlead", "trail\r"], ids=["inner-cr", "leading-cr", "trailing-cr"]
)
def test_a_cr_in_a_name_survives_the_adb_transport(tmp_path, monkeypatch, recorded, name):
    """Derived listing (see `_derived`) carrying a CR, served as bytes by a fake adb.

    Before (`ls -la`, then text mode): `x\\rforged.xml` listed `x` and
    `forged.xml`, `trail\\r` listed `trail` (renamed), and `\\rlead` vanished.
    """
    log = _bytes_adb(tmp_path, monkeypatch, _derived(recorded, name.encode()))

    ok, result = ContainerInspector(serial="emulator-5554").list_dir("com.example.app")

    assert ok is True, result
    assert [e["name"] for e in result["entries"]] == ["fixture.db"], result["entries"]
    assert (result["rejected"], result["rejected_entries"]) == (1, [repr(name)])
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls == [
        [
            "-s",
            "emulator-5554",
            "shell",
            "run-as",
            "com.example.app",
            "find",
            "-H",
            "/data/data/com.example.app",
            "-maxdepth",
            "1",
            "-printf",
            "'%M\\0%u\\0%g\\0%s\\0%l\\0%P\\0'",
        ]
    ], calls


def _run_main(monkeypatch, capsys, *argv: str) -> subprocess.CompletedProcess:
    """container.main through common.cli, with the --serial taken as given."""
    monkeypatch.setattr(container, "resolve_device_identifier", lambda serial: serial)
    with pytest.raises(SystemExit) as exc:
        cli.run(container.main, list(argv))
    out, err = capsys.readouterr()
    return subprocess.CompletedProcess(argv, exc.value.code, stdout=out, stderr=err)


def _prefs_listing(recorded, *names: str | bytes) -> bytes:
    """The recorded databases listing as a shared_prefs/ listing: its file records renamed.

    The directory's own record and every field but the name are the device's;
    the first file record's name becomes ``names[0]``, and so on, and a file
    record left over is dropped.
    """
    own, *files = _records(_listing(recorded, "run_as_find_databases"))
    renamed = [
        [*record[:5], name.encode() if isinstance(name, str) else name]
        for record, name in zip(files, names, strict=False)
    ]
    return _joined([own, *renamed])


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_shared_prefs_that_were_all_refused_are_not_reported_as_none(
    tmp_path, monkeypatch, capsys, recorded, json_mode
):
    """A sole `..\\escape.xml` answered `No shared_prefs found`, exit 0.

    That is "nothing there" for "refused to look at it". Now: exit 1, the
    refusal named and counted, and never the "none found" sentence.
    """
    _bytes_adb(tmp_path, monkeypatch, _prefs_listing(recorded, "..\\escape.xml"))
    argv = ["--package", "com.example.app", "--serial", "emulator-5554", "--shared-prefs"]

    result = _run_main(monkeypatch, capsys, *argv, *(["--json"] if json_mode else []))

    assert result.returncode == 1, result
    assert "No shared_prefs found" not in result.stdout + result.stderr
    if json_mode:
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert (payload["files"], payload["rejected"]) == ([], 1)
        assert payload["rejected_entries"] == [repr("..\\escape.xml")]
        assert "refused 1 entry" in payload["error"] and payload["remedy"]
    else:
        assert "shared_prefs (0, 1 refused):" in result.stdout
        assert "Error: refused 1 entry" in result.stderr
        assert "Remedy:" in result.stderr


def test_a_partly_refused_listing_keeps_what_was_read_and_exits_one(
    tmp_path, monkeypatch, capsys, recorded
):
    """Exit 1 when SOME entries were refused too: the answer is incomplete.

    The entries that were read are still handed over, as a failing batch keeps
    its summary; --verbose names the refused one, escaped.
    """
    _bytes_adb(
        tmp_path, monkeypatch, _prefs_listing(recorded, "fixture_settings.xml", "evil\rname.xml")
    )
    argv = ["--package", "com.example.app", "--serial", "emulator-5554", "--shared-prefs"]

    result = _run_main(monkeypatch, capsys, *argv, "--verbose")

    assert result.returncode == 1, result
    assert "  fixture_settings.xml" in result.stdout
    assert f"  refused: {'evil' + chr(13) + 'name.xml'!r}" in result.stdout
    assert "\r" not in result.stdout, "a raw CR reached the terminal"
    assert "Error: refused 1 entry" in result.stderr


def test_a_listing_with_nothing_refused_still_exits_zero(tmp_path, monkeypatch, capsys, recorded):
    """The control: the recorded names alone answer as before, exit 0."""
    _bytes_adb(tmp_path, monkeypatch, _prefs_listing(recorded, "fixture_settings.xml"))
    argv = ["--package", "com.example.app", "--serial", "emulator-5554", "--shared-prefs"]

    result = _run_main(monkeypatch, capsys, *argv, "--json")

    assert result.returncode == 0, result
    payload = json.loads(result.stdout)
    assert (payload["files"], payload["rejected"]) == (["fixture_settings.xml"], 0)


PKG_DIR = "/data/data/com.example.app"


def _export_rules(recorded, prefs_listing: bytes, *cat_rules):
    """The fake adb's answers for an export: recorded listings and one recorded prefs file."""
    return [
        *cat_rules,
        (("cat",), recorded.text("shared_prefs_settings_xml").encode(), b"", 0),
        (("find", f"{PKG_DIR}/shared_prefs"), prefs_listing, b"", 0),
        (("find", f"{PKG_DIR}/databases"), _listing(recorded, "run_as_find_databases"), b"", 0),
        (("find", PKG_DIR), _listing(recorded, "run_as_find_data_dir"), b"", 0),
    ]


def test_export_refuses_an_undecodable_name_instead_of_reading_another(
    tmp_path, monkeypatch, capsys, recorded
):
    """The regression: `evil\\xff.xml` exported as exit 0, success, rejected 0.

    Replaced, the name asked the device for `evil\\ufffd.xml`, which does not
    exist, and the failed read was skipped. Now the name is refused before
    any read, the run exits 1, and the recorded prefs file is still exported.
    """
    prefs = _prefs_listing(recorded, "fixture_settings.xml", b"evil\xff.xml")
    log = _routed_adb(tmp_path, monkeypatch, _export_rules(recorded, prefs))
    out = tmp_path / "out"
    out.mkdir()

    result = _run_main(
        monkeypatch,
        capsys,
        "--package",
        "com.example.app",
        "--serial",
        "emulator-5554",
        "--export",
        str(out),
        "--json",
    )

    assert result.returncode == 1, result
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert (payload["rejected"], payload["rejected_entries"]) == (1, [repr(b"evil\xff.xml")])
    assert payload["unread"] == 0
    assert (out / "com.example.app/shared_prefs/fixture_settings.xml").exists()
    reads = [argv for argv in map(json.loads, log.read_text().splitlines()) if "cat" in argv]
    assert all("evil" not in " ".join(argv) for argv in reads), reads


def _missing_read(recorded, path: str) -> list:
    """The read of a missing ``path``, answered as each transport recorded it.

    ``shell -T``: run_as_cat_missing -- stdout empty, the reason on stderr,
    exit 1. ``exec-out``: run_as_cat_missing_exec_out -- the same reason on
    STDOUT, stderr empty, exit 0. Both recorded statuses come from the manifest.
    """
    entry = {f["name"]: f for f in recorded.manifest["fixtures"]}
    shell_t, exec_out = entry["run_as_cat_missing"], entry["run_as_cat_missing_exec_out"]
    # Which stream carried the reason, and that the other was empty, are
    # recorded facts -- read here, not assumed by where the bytes are placed.
    assert (shell_t["stream"], shell_t["stdout_bytes"]) == ("stderr", 0), shell_t
    assert (exec_out["stream"], exec_out["stderr_bytes"]) == ("stdout", 0), exec_out
    return [
        (
            ("-T", "cat", path),
            b"",
            recorded.text("run_as_cat_missing").encode(),
            shell_t["exit_status"],
        ),
        (
            ("exec-out", "cat", path),
            recorded.text("run_as_cat_missing_exec_out").encode(),
            b"",
            exec_out["exit_status"],
        ),
    ]


def test_export_reports_a_listed_file_it_could_not_read(tmp_path, monkeypatch, capsys, recorded):
    """A file the listing showed and the read could not deliver: reported, exit 1.

    Over exec-out the read "succeeded" with the error text as its bytes, and
    export wrote that into the snapshot as a prefs file; a read that raised
    was skipped. Now it is `unread`, and the run exits 1. The file is
    `aes_missing.xml` in a derived prefs listing, so the recorded reads of
    that name answer for it (see `_missing_read`).
    """
    reason = recorded.text("run_as_cat_missing")
    prefs = _prefs_listing(recorded, "fixture_settings.xml", "aes_missing.xml")
    rules = _export_rules(
        recorded, prefs, *_missing_read(recorded, f"{PKG_DIR}/shared_prefs/aes_missing.xml")
    )
    _routed_adb(tmp_path, monkeypatch, rules)
    out = tmp_path / "out"
    out.mkdir()

    result = _run_main(
        monkeypatch,
        capsys,
        "--package",
        "com.example.app",
        "--serial",
        "emulator-5554",
        "--export",
        str(out),
        "--json",
    )

    assert result.returncode == 1, result
    payload = json.loads(result.stdout)
    assert payload["unread"] == 1
    assert payload["unread_entries"] == [{"name": "aes_missing.xml", "error": reason.strip()}]
    assert "could not read 1 listed file" in payload["error"]
    assert (out / "com.example.app/shared_prefs/fixture_settings.xml").exists()
    assert not (out / "com.example.app/shared_prefs/aes_missing.xml").exists()


def test_cat_of_a_missing_file_is_a_failure_not_content(tmp_path, monkeypatch, capsys, recorded):
    """Over exec-out, `--cat` of a missing file answered success with the error as content.

    Measured on emulator-5554 before the fix: exit 0, `content: "cat: ...: No
    such file or directory"`. Each transport answers as recorded (see
    `_missing_read`); `shell -T` carries the failure.
    """
    reason = recorded.text("run_as_cat_missing")
    _routed_adb(
        tmp_path, monkeypatch, _missing_read(recorded, f"{PKG_DIR}/shared_prefs/aes_missing.xml")
    )

    result = _run_main(
        monkeypatch,
        capsys,
        "--package",
        "com.example.app",
        "--serial",
        "emulator-5554",
        "--cat",
        "shared_prefs/aes_missing.xml",
        "--json",
    )

    assert result.returncode == 1, result
    payload = json.loads(result.stdout)
    assert payload["success"] is False and "content" not in payload
    assert reason.strip() in payload["error"]


@pytest.mark.parametrize("name", COLLIDING_FILE_NAMES)
def test_a_missing_file_named_like_a_device_state_is_still_a_missing_file(
    tmp_path, monkeypatch, capsys, recorded, name
):
    """Codex review of #29: the device's `cat` error for `unauthorized.db` exited 3.

    `shell -T` hands back the device command's stderr as adb's, and the
    classifier found "unauthorized" in it. It is the app's file, missing: exit
    1, with the device's reason (derived: run_as_cat_missing, path replaced).
    """
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    reason = recorded_missing_file(recorded, PKG_DIR + "/shared_prefs", name)
    _routed_adb(
        tmp_path,
        monkeypatch,
        [(("-T", "cat"), b"", reason.encode(), status["run_as_cat_missing"])],
    )

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", "com.example.app", "--serial", "emulator-5554"),
        *("--cat", f"shared_prefs/{name}", "--json"),
    )

    payload = assert_refusal(
        result, json_mode=True, naming=("no such file or directory",), status=1
    )
    assert reason.strip() in payload["error"]


@pytest.mark.parametrize(("separator", "probes"), [("\n", 1), ("\r", 0)], ids=["lf", "cr"])
def test_a_device_filename_cannot_forge_an_offline_verdict(
    tmp_path, monkeypatch, capsys, recorded, separator, probes
):
    """A6: only the filename field is substituted in ``run_as_cat_missing``.

    The derived device-side error names
    ``shared_prefs/x<LF-or-CR>adb: device offline``. LF resembles a second adb
    error line and therefore requires one reachability probe; CR is data, not an
    adb line boundary. Both remain the recorded ``cat`` failure at exit 1.
    """
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    name = f"x{separator}adb: device offline"
    reason = recorded_missing_file(recorded, PKG_DIR + "/shared_prefs", name)
    log = _routed_adb(
        tmp_path,
        monkeypatch,
        [
            (("-T", "cat"), b"", reason.encode(), status["run_as_cat_missing"]),
            (("get-state",), b"device\n", b"", 0),
        ],
    )

    result = _run_main(
        monkeypatch,
        capsys,
        *(
            "--package",
            "com.example.app",
            "--serial",
            "emulator-5554",
            "--cat",
            f"shared_prefs/{name}",
            "--json",
        ),
    )

    payload = assert_refusal(
        result, json_mode=True, naming=("no such file or directory",), status=1
    )
    assert " ".join(reason.split()) in payload["error"]
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert sum(call[-1] == "get-state" for call in calls) == probes


def test_a_device_that_is_gone_during_a_read_is_still_exit_3(
    tmp_path, monkeypatch, capsys, recorded, recorded_anywhere
):
    """The control: adb's own `adb: device '...' not found` on that read is the device's."""
    log = _routed_adb(
        tmp_path,
        monkeypatch,
        [
            (("-T", "cat"), b"", recorded.text("adb_shell_device_not_found").encode(), 1),
            (("get-state",), b"", recorded_anywhere("adb_device_not_found").encode(), 1),
        ],
    )

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", "com.example.app", "--serial", "emulator-5554"),
        *("--cat", "shared_prefs/fixture_settings.xml", "--json"),
    )

    assert_refusal(result, json_mode=True, naming=("not attached",), status=3)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert sum(call[-1] == "get-state" for call in calls) == 1


# ---------------------------------------------------------------------------
# The adb client check. `shell -T` needs adb 1.0.36; an older client forwards
# `-T` to the device shell and exits 0 after its error, which a read deciding
# by exit status takes for the file. The banner is recorded (adb_version).
# ---------------------------------------------------------------------------


@pytest.fixture
def _real_client_check(monkeypatch):
    """The real, once-per-process `adb version` check, with its cache emptied either side."""
    _REAL_ADB_CLIENT_VERSION.cache_clear()
    monkeypatch.setattr(container, "adb_client_version", _REAL_ADB_CLIENT_VERSION)
    yield
    _REAL_ADB_CLIENT_VERSION.cache_clear()


def _read_with_banner(tmp_path, monkeypatch, capsys, recorded, banner: str):
    """`--cat` the recorded prefs file, the fake adb answering `adb version` with ``banner``."""
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    log = _routed_adb(
        tmp_path,
        monkeypatch,
        [
            (("version",), banner.encode(), b"", status["adb_version"]),
            (("cat",), recorded.text("shared_prefs_settings_xml").encode(), b"", 0),
        ],
    )
    argv = ["--package", "com.example.app", "--serial", "emulator-5554", "--json"]
    result = _run_main(monkeypatch, capsys, *argv, "--cat", "shared_prefs/fixture_settings.xml")
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, calls


def test_the_recorded_adb_client_may_read_and_is_asked_once(
    _real_client_check, tmp_path, monkeypatch, capsys, recorded
):
    """The recorded banner (1.0.41) passes; a second read does not ask again."""
    banner = recorded.text("adb_version")
    first, _ = _read_with_banner(tmp_path, monkeypatch, capsys, recorded, banner)
    second = _run_main(
        monkeypatch,
        capsys,
        "--package",
        "com.example.app",
        "--serial",
        "emulator-5554",
        "--json",
        "--cat",
        "shared_prefs/fixture_settings.xml",
    )

    for result in (first, second):
        assert result.returncode == 0, result
        assert json.loads(result.stdout)["content"] == recorded.text("shared_prefs_settings_xml")
    log = tmp_path / "adb.log"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call for call in calls if call == ["version"]] == [["version"]], calls


@pytest.mark.parametrize(
    ("version", "error"),
    [
        ("1.0.32", "adb 1.0.32 is older than 1.0.36 and cannot read app files safely"),
        ("unknown", "cannot tell whether this adb can read app files safely"),
    ],
    ids=["older-than-1.0.36", "no-version"],
)
def test_an_adb_client_that_cannot_honour_t_is_refused_before_any_read(
    _real_client_check, tmp_path, monkeypatch, capsys, recorded, version, error
):
    """Derived banner: the recorded `adb version` with ONLY its X.Y.Z replaced.

    A client older than 1.0.36 would send `-T` to the device as command text
    and exit 0 after the shell's error; a banner with no version cannot be
    shown to be newer. Either way: refused, and no read is attempted.
    """
    banner = recorded.text("adb_version")
    recorded_version = ".".join(map(str, container.parse_adb_version(banner)))
    derived = banner.replace(recorded_version, version, 1)
    assert derived != banner

    result, calls = _read_with_banner(tmp_path, monkeypatch, capsys, recorded, derived)

    assert result.returncode == 1, result
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert error in payload["error"], payload
    assert "platform-tools" in payload["remedy"]
    assert calls == [["version"]], f"a read was attempted: {calls}"


def test_a_schema_dump_checks_the_client_before_the_on_device_query(
    _real_client_check, tmp_path, monkeypatch, capsys, recorded
):
    """`--databases NAME` ran `run-as ... sqlite3 <db> .schema` BEFORE the check.

    Codex review of #29: "refused before any read" was not true of the schema
    path. Derived banner as above (recorded `adb version`, X.Y.Z -> 1.0.32).
    """
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    banner = recorded.text("adb_version")
    recorded_version = ".".join(map(str, container.parse_adb_version(banner)))
    old_client = banner.replace(recorded_version, "1.0.32", 1)
    log = _routed_adb(
        tmp_path, monkeypatch, [(("version",), old_client.encode(), b"", status["adb_version"])]
    )

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", "com.example.app", "--serial", "emulator-5554"),
        *("--databases", "fixture.db", "--json"),
    )

    assert_refusal(result, json_mode=True, naming=("older than 1.0.36",), status=1)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not [call for call in calls if "run-as" in call], calls


def test_export_with_a_refused_adb_client_writes_nothing(
    _real_client_check, tmp_path, monkeypatch, capsys, recorded
):
    """The client is checked before the snapshot directory is created.

    Derived banner as above (recorded `adb version`, X.Y.Z replaced by 1.0.32);
    the listings are the recorded ones (see `_export_rules`).
    """
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    banner = recorded.text("adb_version")
    recorded_version = ".".join(map(str, container.parse_adb_version(banner)))
    old_client = banner.replace(recorded_version, "1.0.32", 1)
    prefs = _prefs_listing(recorded, "fixture_settings.xml")
    rules = [(("version",), old_client.encode(), b"", status["adb_version"])]
    log = _routed_adb(tmp_path, monkeypatch, rules + _export_rules(recorded, prefs))
    out = tmp_path / "out"
    out.mkdir()

    result = _run_main(
        monkeypatch,
        capsys,
        "--package",
        "com.example.app",
        "--serial",
        "emulator-5554",
        "--export",
        str(out),
        "--json",
    )

    assert result.returncode == 1, result
    assert "older than 1.0.36" in json.loads(result.stdout)["error"]
    assert list(out.iterdir()) == [], "the refusal left a snapshot behind"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not [call for call in calls if "cat" in call], calls


# === parse_shared_prefs_xml ===
#
# The typed round-trip lives at the bottom of this file, against
# `shared_prefs_settings_xml` — a real file the fixture app writes, carrying
# one value of every type Android encodes differently.


def test_parse_prefs_keys_complete(recorded):
    prefs = parse_shared_prefs_xml(recorded.text("shared_prefs_settings_xml"))
    assert set(prefs) == {
        "display_name",
        "launch_count",
        "last_sync_epoch_ms",
        "playback_speed",
        "dark_theme",
        "enabled_flags",
    }


def test_parse_prefs_malformed_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_shared_prefs_xml("<map><string name='x'>unclosed")


def test_parse_prefs_bad_numeric_defaults_to_zero(recorded):
    """Coercion must not raise on a value that will not convert.

    The document is the recorded prefs file with one attribute changed, so the
    surrounding shape is still what Android writes -- only the thing under test
    is substituted.
    """
    xml = recorded.text("shared_prefs_settings_xml").replace('value="7"', 'value="notanumber"')
    assert parse_shared_prefs_xml(xml)["launch_count"] == 0


# === parse_sqlite_schema ===


def test_parse_schema_table_names(recorded):
    """Including the two tables SQLite adds that a hand-written schema omits."""
    names = [t["name"] for t in parse_sqlite_schema(recorded.text("sqlite_schema_host"))]
    assert names == ["android_metadata", "orders", "sqlite_sequence", "order_items"]


def test_parse_schema_columns_and_types(recorded):
    tables = {t["name"]: t for t in parse_sqlite_schema(recorded.text("sqlite_schema_host"))}

    order_cols = {c["name"]: c["type"] for c in tables["orders"]["columns"]}
    assert order_cols["id"] == "INTEGER"
    assert order_cols["reference"] == "TEXT"
    assert order_cols["total_cents"] == "INTEGER"
    assert order_cols["placed_at"] == "INTEGER"

    item_cols = {c["name"]: c["type"] for c in tables["order_items"]["columns"]}
    assert item_cols["quantity"] == "INTEGER"
    # The FOREIGN KEY table-level constraint is not a column.
    assert "FOREIGN" not in item_cols


def test_parse_schema_skips_indexes(recorded):
    schema = recorded.text("sqlite_schema_host")
    assert "CREATE INDEX index_order_items_order_id" in schema
    assert "CREATE UNIQUE INDEX index_orders_reference" in schema

    names = {t["name"] for t in parse_sqlite_schema(schema)}
    assert "index_order_items_order_id" not in names
    assert "index_orders_reference" not in names


def test_parse_schema_empty():
    assert parse_sqlite_schema("") == []


# === command construction (run-as mechanism) ===


def _completed(cmd, returncode=0, stdout="", stderr=""):
    """An adb result as `_invoke` gets it: bytes (it no longer asks for text)."""
    return subprocess.CompletedProcess(
        cmd, returncode, stdout=stdout.encode("utf-8"), stderr=stderr.encode("utf-8")
    )


def test_list_dir_builds_the_run_as_listing(monkeypatch, recorded):
    """Parses a real listing of the data dir, by the command it was recorded with.

    The directory's own record is not an entry, and the five subdirectories
    the fixture app has once it has run are.
    """
    captured: list[list[str]] = []
    listing = _listing(recorded, "run_as_find_data_dir")

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector(serial="emulator-5554").list_dir("com.example.app")
    assert ok is True

    assert result["total_entries"] == 5, f"miscounted a real listing: {result}"
    names = {entry["name"] for entry in result["entries"]}
    assert names == {"cache", "code_cache", "databases", "files", "shared_prefs"}, names

    cmd = captured[0]
    # Targets the right device, uses run-as for the right package, lists the data dir.
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]
    assert cmd[cmd.index("run-as") + 1] == "com.example.app"
    assert _listed(cmd) == "/data/data/com.example.app"


def test_list_dir_subpath_joined_relative(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout="")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().list_dir("com.example.app", subpath="files/cache")
    assert _listed(captured[0]) == "/data/data/com.example.app/files/cache"
    # Nothing printed is not an empty directory: find prints the directory's own record.
    assert ok is False and "not NUL-terminated records" in result["error"], result


def test_list_dir_rejects_path_escape():
    ok, result = ContainerInspector().list_dir("com.example.app", subpath="../../etc")
    assert ok is False
    assert "escapes" in result["error"]


def test_run_as_denied_for_a_non_debuggable_package(monkeypatch, recorded):
    """The denial that was NOT being detected, in the platform's own words.

    This test used to assert against `run-as: package not debuggable: <pkg>`,
    a string the platform never prints. The real one is `run-as: package not
    an application: <pkg>`, and container.py's marker list read "is not an
    application" -- with an "is" that is not there -- so this denial fell
    through to a generic "Command failed" and the user never saw the hint
    telling them the app has to be debuggable.

    Both the invented assertion and the invented marker were wrong in the same
    direction, which is exactly why the suite stayed green.
    """
    denial = recorded.text("run_as_not_an_application")

    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, returncode=1, stderr=denial)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().list_dir("com.android.settings")
    assert ok is False
    assert (
        result["run_as_denied"] is True
    ), f"a real run-as denial was reported as a generic failure: {result}"
    assert "debuggable" in result["hint"].lower()


def test_run_as_denied_unknown_package(monkeypatch, recorded):
    """The recorded refusal for the package it names (aliased com.example.app1), and only that one.

    This listed `com.nope` and answered with the refusal for com.example.app1;
    a refusal is now the whole recorded form for the package asked about.
    """

    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, returncode=1, stderr=recorded.text("run_as_unknown_package"))

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().list_dir("com.example.app1")
    assert ok is False
    assert result.get("run_as_denied") is True

    ok, result = ContainerInspector().list_dir("com.example.composefixture")
    assert ok is False
    assert "run_as_denied" not in result, "another package's refusal was this one's"


def test_raise_if_denied_helper(recorded):
    """The classifier on the recorded release-build refusal (run_as_id_not_debuggable).

    This asserted against `run-as: package not debuggable`, with no package,
    for `com.x`: a string no device printed, short enough to slip past the
    fixture ratchet.
    """
    refusal = recorded.text("run_as_id_not_debuggable")
    with pytest.raises(RunAsDeniedError):
        container._raise_if_denied("com.android.chrome", refusal, 1)
    # Another package's refusal is not this one's, and a zero exit never denies.
    container._raise_if_denied("com.example.composefixture", refusal, 1)
    container._raise_if_denied("com.android.chrome", refusal, 0)


def test_shared_prefs_dump_reads_through_shell_t_cat(monkeypatch, recorded):
    """`adb shell -T`, not `exec-out`: exec-out exits 0 and puts the device's stderr on stdout."""
    captured: list[list[str]] = []
    prefs_xml = recorded.text("shared_prefs_settings_xml").encode("utf-8")

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=prefs_xml, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().shared_prefs("com.example.app", name="settings")
    assert ok is True
    assert result["file"] == "settings.xml"
    assert result["preferences"]["display_name"] == "Fixture User"

    cmd = captured[0]
    assert "exec-out" not in cmd
    assert cmd[cmd.index("shell") + 1] == "-T", cmd
    assert "run-as" in cmd
    assert "cat" in cmd
    assert "/data/data/com.example.app/shared_prefs/settings.xml" in cmd


def test_cat_truncates_large_file(monkeypatch):
    big = b"A" * (container.CAT_MAX_BYTES + 5000)

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=big, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().cat_file("com.example.app", "files/big.txt")
    assert ok is True
    assert result["truncated"] is True
    assert result["size_bytes"] == len(big)
    # Cached because it exceeds the cache threshold; note mentions the cap.
    assert "cache_id" in result
    assert "capped" in result["note"]


# --- D10: the cap cuts a character, not a text file ---------------------------
#
# Both reads are recordings of container.py's own argv on the API 33 emulator
# (record_fixtures.py: run_as_cat_utf8_cap, run_as_cat_fixture_db). The UTF-8
# file is longer than the cap, and its manifest entry says where the cap falls:
# inside a three-byte character, after its second byte.

API33 = RecordedFixtures(RECORDED_ROOT / "emulator-api33")


def _manifest_entry(name: str) -> dict:
    return next(e for e in API33.manifest["fixtures"] if e["name"] == name)


def _cat_recorded(monkeypatch, payload: bytes, *, cap: int) -> dict:
    """--cat of a file whose read is ``payload``, under a cap of ``cap`` bytes, inline."""
    monkeypatch.setattr(container, "CAT_MAX_BYTES", cap)
    monkeypatch.setattr(container, "CAT_CACHE_BYTES", 10 * len(payload))

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    ok, result = ContainerInspector().cat_file(FIXTURE_PKG, "files/aes_utf8_cap.txt")
    assert ok is True, result
    return result


def test_cat_keeps_a_utf8_file_text_when_the_cap_cuts_a_character(monkeypatch):
    """D10: a UTF-8 text file whose cap lands mid-character is text, cut on the character."""
    entry = _manifest_entry("run_as_cat_utf8_cap")
    payload = API33.data(entry["file"])
    cap, split = entry["cap_bytes"], entry["split_character_starts_at"]
    assert len(payload) > cap > split, "the recording no longer puts the cap inside a character"

    result = _cat_recorded(monkeypatch, payload, cap=cap)
    assert result["content_type"] == "text", result.get("content")
    assert result["truncated"] is True
    assert result["size_bytes"] == len(payload)
    # Everything up to the split character, and nothing of it: the content is
    # the file's text, ending at its last whole character before the cap.
    assert result["content"] == payload[:split].decode("utf-8")
    assert payload.decode("utf-8").startswith(result["content"])


def test_a_file_ending_mid_character_without_a_cut_is_still_binary(monkeypatch):
    """Only the cut is forgiven: the same bytes, whole and uncapped, are not UTF-8."""
    entry = _manifest_entry("run_as_cat_utf8_cap")
    ends_mid_character = API33.data(entry["file"])[: entry["cap_bytes"]]

    result = _cat_recorded(monkeypatch, ends_mid_character, cap=len(ends_mid_character) + 1)
    assert result["truncated"] is False
    assert result["content_type"] == "binary", result


def test_a_file_exactly_at_the_cap_that_ends_mid_character_is_binary(monkeypatch):
    """``len(raw) == cap`` is the whole file, uncut: its partial character is not forgiven."""
    entry = _manifest_entry("run_as_cat_utf8_cap")
    ends_mid_character = API33.data(entry["file"])[: entry["cap_bytes"]]

    result = _cat_recorded(monkeypatch, ends_mid_character, cap=len(ends_mid_character))
    assert result["truncated"] is False
    assert result["content_type"] == "binary", result


# The byte(s) at a cut, in front of the recorded file's first line (which is
# ASCII, so it ends on a character boundary) and behind it the rest of the
# recording, so the file runs past the cap. The cap falls right after the
# probe. The 2- and 3-byte leads are the recording's own (from "ü" and "日");
# the recording holds no 4-byte character, so that lead is F0 9F 98, the first
# three bytes of U+1F600. Every malformed one is invalid where it stands, so
# no amount of following input could complete it.
MALFORMED_AT_THE_CUT = {
    "FF": b"\xff",
    "C0": b"\xc0",
    "lone 80": b"\x80",
    "F4 90": b"\xf4\x90",
    "F5": b"\xf5",
    "E0 80": b"\xe0\x80",
}


def _probe_file(probe: bytes) -> tuple[bytes, bytes, int]:
    payload = API33.data(_manifest_entry("run_as_cat_utf8_cap")["file"])
    head = payload[: payload.index(b"\n") + 1]
    return head, head + probe + payload[len(head) :], len(head) + len(probe)


@pytest.mark.parametrize("probe", sorted(MALFORMED_AT_THE_CUT))
def test_a_malformed_sequence_at_the_cut_is_binary(monkeypatch, probe):
    _head, raw, cap = _probe_file(MALFORMED_AT_THE_CUT[probe])
    result = _cat_recorded(monkeypatch, raw, cap=cap)
    assert result["truncated"] is True
    assert result["content_type"] == "binary", result


def _recorded_lead(character: str, length: int) -> bytes:
    payload = API33.data(_manifest_entry("run_as_cat_utf8_cap")["file"])
    encoded = character.encode("utf-8")
    assert encoded in payload, f"the recording no longer holds {character!r}"
    return encoded[:length]


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param(lambda: _recorded_lead("ü", 1), id="2-byte, 1 of 2"),
        pytest.param(lambda: _recorded_lead("日", 2), id="3-byte, 2 of 3"),
        pytest.param(lambda: _recorded_lead("日", 1), id="3-byte, 1 of 3"),
        pytest.param(lambda: b"\xf0\x9f\x98", id="4-byte, 3 of 4"),
    ],
)
def test_a_valid_partial_character_at_the_cut_is_withheld(monkeypatch, lead):
    head, raw, cap = _probe_file(lead())
    result = _cat_recorded(monkeypatch, raw, cap=cap)
    assert result["truncated"] is True
    assert result["content_type"] == "text", result
    assert result["content"] == head.decode("utf-8")


@pytest.mark.parametrize("cap", [1024, 65_536])
def test_a_recorded_binary_file_stays_binary_capped_or_whole(monkeypatch, cap):
    """fixture.db: invalid bytes before the cut make it binary, cut or not."""
    payload = API33.data(_manifest_entry("run_as_cat_fixture_db")["file"])
    with pytest.raises(UnicodeDecodeError) as raised:
        payload.decode("utf-8")
    assert raised.value.start < 1024, "the database's first invalid byte moved past the cap"

    result = _cat_recorded(monkeypatch, payload, cap=cap)
    assert result["truncated"] is (len(payload) > cap)
    assert result["content_type"] == "binary", result


def test_databases_list_separates_sidecars(monkeypatch, recorded):
    """The journal beside the database is not a database.

    The listing this replaced invented `-wal` and `-shm` companions. What the
    fixture app actually leaves in databases/ is a `-journal` file at zero
    bytes, which is the case a caller most often hits.
    """
    listing = _listing(recorded, "run_as_find_databases")

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().databases("com.example.app")
    assert ok is True
    assert result["total_databases"] == 1
    assert result["databases"][0]["name"] == "fixture.db"
    assert result["databases"][0]["size_bytes"] == 32768
    assert set(result["sidecars"]) == {"fixture.db-journal"}


def test_databases_schema_via_device_sqlite3(monkeypatch, recorded):
    captured: list[list[str]] = []
    schema = recorded.text("sqlite_schema_host")

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout=schema)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().databases("com.example.app", name="fixture.db")
    assert ok is True
    assert result["method"] == "device-sqlite3"
    # android_metadata and sqlite_sequence are tables too -- a hand-written
    # schema leaves them out, and the count then looks like the app's own.
    assert result["total_tables"] == 4
    # The on-device sqlite3 strategy must be attempted.
    assert any("sqlite3" in c and ".schema" in c for c in captured)


def test_export_writes_snapshot(monkeypatch, tmp_path, recorded):
    """Every listing served here is recorded output, with ONE substitution.

    `export` lists shared_prefs/ and databases/ only when the data-dir
    listing holds them, so the root is the data dir after the app has run
    (run_as_find_data_dir, which holds both); it and the databases listing
    are served verbatim -- no synthesised topology.

    The single derivation is the prefs directory, kept for the filter it
    exercises: the recorded databases listing has its `fixture.db-journal`
    record renamed `fixture_settings.xml`, the prefs file the recorder
    actually read (see `_prefs_listing`). That leaves a directory holding one
    `.xml` and one file that is not one, which is what `shared_prefs()` has to
    filter (the real prefs listing, run_as_find_shared_prefs, holds only the
    `.xml`).
    """
    _serve_export(
        monkeypatch, recorded, _prefs_listing(recorded, "fixture.db", "fixture_settings.xml")
    )

    ok, _result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is True

    dest = tmp_path / "com.example.app"
    assert (dest / "file_tree.txt").exists()
    assert (dest / "databases.json").exists()
    assert (dest / "shared_prefs" / "fixture_settings.xml").exists()


def _serve_export(monkeypatch, recorded, prefs: bytes) -> bytes:
    """Serve export() the recorded listings, with ``prefs`` for shared_prefs/.

    Each listing is routed by the directory it names. Returns the recorded
    prefs bytes every `cat` answers with.
    """
    listings = {
        "shared_prefs": prefs,
        "databases": _listing(recorded, "run_as_find_databases"),
        "": _listing(recorded, "run_as_find_data_dir"),
    }
    prefs_xml = recorded.text("shared_prefs_settings_xml").encode("utf-8")

    def fake_run(cmd, *args, **kwargs):
        directory = _listed(cmd)
        if directory is not None:
            below = directory.removeprefix(PKG_DIR).lstrip("/")
            return subprocess.CompletedProcess(cmd, 0, stdout=listings[below], stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=prefs_xml, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    return prefs_xml


def _files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_export_writes_only_plain_names_inside_the_snapshot(monkeypatch, tmp_path, recorded):
    """Hostile prefs names are not exported; the legitimate recorded one still is.

    The prefs listing is test_export_writes_snapshot's (the recorded databases
    listing with `fixture.db-journal` renamed `fixture_settings.xml`), with two
    derived records added, copies of its own `fixture.db` record with the name
    replaced: `..\\escape.xml` -- a legal Linux file name, a path on a Windows
    host -- and `x` + newline + `../../escape.xml`.
    """
    own, db, _journal = _records(_listing(recorded, "run_as_find_databases"))
    prefs = _joined(
        [
            own,
            [*db[:5], b"fixture_settings.xml"],
            [*db[:5], b"..\\escape.xml"],
            [*db[:5], b"x\n../../escape.xml"],
        ]
    )
    prefs_xml = _serve_export(monkeypatch, recorded, prefs)
    out = tmp_path / "out"
    out.mkdir()

    ok, result = ContainerInspector().export("com.example.app", str(out))

    assert ok is True, result
    assert result["rejected"] == 2, result["rejected_entries"]
    assert _files_under(tmp_path) == {
        "out/com.example.app/file_tree.txt",
        "out/com.example.app/databases.json",
        "out/com.example.app/shared_prefs/fixture_settings.xml",
    }
    assert (out / "com.example.app/shared_prefs/fixture_settings.xml").read_bytes() == prefs_xml


@pytest.mark.parametrize("name", ["../escape.xml", "../../escape.xml", "sub/escape.xml"])
def test_export_refuses_a_name_that_resolves_outside_its_directory(
    monkeypatch, tmp_path, recorded, name
):
    """The check where a device name becomes a host path, whatever the listing let through.

    parse_listing refuses such names first, so this listing is handed to
    export() directly -- the case it guards is a listing that reached it
    another way. It refuses before creating anything.
    """
    _serve_export(monkeypatch, recorded, _listing(recorded, "run_as_find_databases"))
    monkeypatch.setattr(
        ContainerInspector,
        "shared_prefs",
        lambda _self, _package, name_=None: (True, {"files": ["fixture_settings.xml", name]}),
    )
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(cli.Failure) as excinfo:
        ContainerInspector().export("com.example.app", str(out))

    assert repr(name) in str(excinfo.value), str(excinfo.value)
    assert "Nothing was written" in excinfo.value.remedy
    assert _files_under(tmp_path) == set(), "the refusal left files behind"
    assert not (out / "com.example.app").exists()


def test_export_refuses_existing_destination(monkeypatch, tmp_path, recorded):
    listing = _listing(recorded, "run_as_find_data_dir")

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    (tmp_path / "com.example.app").mkdir()
    ok, result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is False
    assert "already exists" in result["error"]


# ---------------------------------------------------------------------------
# A sub-listing that fails (final review of v0.8.0, C2). export() took any
# failed listing of shared_prefs/ or databases/ for an empty directory: exit 0,
# `success: true`, `unread: 0`, the databases failure visible only inside the
# databases.json it wrote. Every listing below is the fixture app's own,
# recorded for it -- so these runs use its package, and the reason in each
# recorded failure names exactly the path export() lists.
# ---------------------------------------------------------------------------


def _recorded_answer(recorded, name: str) -> tuple[bytes, bytes, int]:
    """(stdout, stderr, exit) of a recorded command, each stream where the manifest says it was.

    A command that printed on both streams was recorded once, its stderr in the
    twin ``<name>_stderr`` (run_as_find_*_unreadable); the manifest's byte
    counts say which streams there were.
    """
    entry = {f["name"]: f for f in recorded.manifest["fixtures"]}[name]
    data = recorded.data(f"{name}.txt")
    if entry.get("stream") == "stderr":
        assert entry["stdout_bytes"] == 0, entry
        return b"", data, entry["exit_status"]
    stderr = recorded.data(f"{name}_stderr.txt") if entry.get("stderr_bytes") else b""
    assert len(stderr) == entry.get("stderr_bytes", 0), entry
    return data, stderr, entry.get("exit_status", 0)


def _fixture_app_export(tmp_path, monkeypatch, capsys, recorded, *, data_dir, prefs, dbs, text):
    """Export the fixture app with each listing answered by the named recording.

    ``data_dir`` may instead be a derived (stdout, stderr, exit) answer.
    Returns (the run, the export directory, the argv of every adb call).
    """
    root = data_dir if isinstance(data_dir, tuple) else _recorded_answer(recorded, data_dir)
    log = _routed_adb(
        tmp_path,
        monkeypatch,
        [
            (("cat",), recorded.text("shared_prefs_settings_xml").encode(), b"", 0),
            (("find", f"{FIXTURE_DIR}/shared_prefs"), *_recorded_answer(recorded, prefs)),
            (("find", f"{FIXTURE_DIR}/databases"), *_recorded_answer(recorded, dbs)),
            (("find", FIXTURE_DIR), *root),
        ],
    )
    out = tmp_path / "out"
    out.mkdir()
    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554", "--export", str(out)),
        *([] if text else ["--json"]),
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, out / FIXTURE_PKG, calls


def test_the_recorded_export_listings_are_what_the_device_printed(recorded):
    """Anti-vacuity: each failure is exit 1 with its reason on stderr, the listings exit 0.

    An unreadable directory could still be stat'ed, so find printed its own
    record on stdout before the reason; a missing one printed nothing, and
    find quotes its path. run_as_find_data_dir_bare was recorded with both
    directories moved aside, so it names neither; run_as_find_data_dir names both.
    """
    for name, stdout_is_own_record, reason in [
        (
            "run_as_find_shared_prefs_unreadable",
            True,
            f"{FIXTURE_DIR}/shared_prefs: Permission denied",
        ),
        ("run_as_find_databases_unreadable", True, f"{FIXTURE_DIR}/databases: Permission denied"),
        (
            "run_as_find_shared_prefs_absent",
            False,
            f"'{FIXTURE_DIR}/shared_prefs': No such file or directory",
        ),
    ]:
        stdout, stderr, status = _recorded_answer(recorded, name)
        assert status == 1, name
        if stdout_is_own_record:
            ((mode, *_fields, own_name),) = _records(stdout)
            assert mode.startswith(b"d") and own_name == b"", name
        else:
            assert stdout == b"", name
        assert stderr.decode() == f"find: {reason}\n", name
    for name, holds in [("run_as_find_data_dir", True), ("run_as_find_data_dir_bare", False)]:
        stdout, stderr, status = _recorded_answer(recorded, name)
        assert (stderr, status) == (b"", 0), name
        names = {e["name"] for e in parse_listing(stdout).entries}
        assert ({"shared_prefs", "databases"} <= names) is holds, (name, names)
        assert holds or not {"shared_prefs", "databases"} & names, (name, names)


@pytest.mark.parametrize(
    ("unreadable", "still_written"),
    [
        ("shared_prefs", "databases.json"),
        ("databases", "shared_prefs/fixture_settings.xml"),
    ],
)
def test_export_refuses_when_a_directory_it_holds_cannot_be_listed(
    tmp_path, monkeypatch, capsys, recorded, unreadable, still_written
):
    """C2: the data dir names the directory, and its listing fails -> exit 1, reason kept.

    What was exported stays exported and is named in the refusal; nothing is
    written for the directory that could not be listed (no databases.json
    holding an error).
    """
    listings = {"prefs": "run_as_find_shared_prefs", "dbs": "run_as_find_databases"}
    listings["prefs" if unreadable == "shared_prefs" else "dbs"] = (
        f"run_as_find_{unreadable}_unreadable"
    )
    reason = recorded.text(f"run_as_find_{unreadable}_unreadable_stderr").strip()

    result, snapshot, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_find_data_dir",
        text=False,
        **listings,
    )

    payload = assert_refusal(result, json_mode=True, naming=(f"{unreadable}/",), status=1)
    assert reason in payload["error"], payload["error"]
    assert payload["unlisted"] == 1, payload
    (unlisted,) = payload["unlisted_dirs"]
    assert (unlisted["name"], unlisted["path"]) == (unreadable, f"{FIXTURE_DIR}/{unreadable}")
    # The device's words. `ls: ...: Permission denied` is the filesystem's, not
    # run-as's (which just listed the data dir): no "run-as denied", no debug hint.
    assert reason in unlisted["error"], unlisted
    assert "run-as denied" not in payload["error"] and "debuggable" not in payload["remedy"]
    written = {str(Path(p).relative_to(snapshot)) for p in payload["files_written"]}
    assert written == {"file_tree.txt", still_written}, written
    assert written == _files_under(snapshot)
    assert payload["destination"] in payload["remedy"]


def test_export_refuses_when_neither_directory_can_be_listed(
    tmp_path, monkeypatch, capsys, recorded
):
    """The reviewer's reproduction: both sub-listings fail. Was exit 0, success, unread 0."""
    result, snapshot, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_find_data_dir",
        prefs="run_as_find_shared_prefs_unreadable",
        dbs="run_as_find_databases_unreadable",
        text=False,
    )

    payload = assert_refusal(result, json_mode=True, naming=("could not list 2",), status=1)
    assert "shared_prefs/" in payload["error"] and "databases/" in payload["error"]
    assert [d["name"] for d in payload["unlisted_dirs"]] == ["shared_prefs", "databases"]
    assert _files_under(snapshot) == {"file_tree.txt"}


def test_a_directory_listed_and_then_not_found_is_a_failed_listing(
    tmp_path, monkeypatch, capsys, recorded
):
    """Absent is decided by the data-dir listing, not by a failure's wording.

    The data dir names shared_prefs/, and its listing answers "No such file or
    directory" (recorded with the directory moved aside): gone between two
    listings, or never readable -- either way the export did not read it.
    """
    reason = recorded.text("run_as_find_shared_prefs_absent").strip()

    result, _, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_find_data_dir",
        prefs="run_as_find_shared_prefs_absent",
        dbs="run_as_find_databases",
        text=False,
    )

    payload = assert_refusal(result, json_mode=True, naming=("shared_prefs/",), status=1)
    assert reason in payload["error"]
    assert payload["absent"] == []


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_an_app_with_no_prefs_and_no_databases_exports_and_says_so(
    tmp_path, monkeypatch, capsys, recorded, text
):
    """The data-dir listing names neither directory: a complete export, exit 0, saying so.

    run_as_find_data_dir_bare is that app -- recorded with both directories
    moved aside. Neither directory is listed (the recorded "No such file"
    would be the answer if one were), and databases.json records the absence
    instead of an error.
    """
    result, snapshot, calls = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_find_data_dir_bare",
        prefs="run_as_find_shared_prefs_absent",
        dbs="run_as_find_shared_prefs_absent",
        text=text,
    )

    assert result.returncode == 0, result
    listed = [_listed(call) for call in calls if _listed(call)]
    assert listed == [FIXTURE_DIR], f"a directory the data dir does not hold was listed: {listed}"
    assert _files_under(snapshot) == {"file_tree.txt", "databases.json"}
    databases_json = json.loads((snapshot / "databases.json").read_text(encoding="utf-8"))
    assert (databases_json["absent"], databases_json["databases"]) == (True, [])
    if text:
        assert "shared_prefs/: absent" in result.stdout, result.stdout
        assert "databases/: absent" in result.stdout, result.stdout
    else:
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["absent"] == ["shared_prefs", "databases"]
        assert (payload["unlisted"], payload["unlisted_dirs"]) == (0, [])


def test_a_failed_listing_in_text_mode_shows_the_partial_export_then_the_refusal(
    tmp_path, monkeypatch, capsys, recorded
):
    """Text mode: what was written on stdout, the reason and remedy on stderr, exit 1."""
    reason = recorded.text("run_as_find_shared_prefs_unreadable_stderr").strip()

    result, snapshot, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_find_data_dir",
        prefs="run_as_find_shared_prefs_unreadable",
        dbs="run_as_find_databases",
        text=True,
    )

    assert result.returncode == 1, result
    assert f"Exported {FIXTURE_PKG} -> {snapshot}" in result.stdout
    assert "1 directory not listed" in result.stdout, result.stdout
    assert "Error: could not list 1 directory" in result.stderr, result.stderr
    assert reason in result.stderr
    assert "Remedy:" in result.stderr


def _seeded_with_a_refused_entry(recorded, name: str) -> tuple[bytes, bytes, int]:
    """The data-dir listing plus one entry parse_listing refuses, as adb answers it.

    Derived: the recorded `link -> e f.db` record of run_as_find_names, with
    its name replaced by ``name`` and a CR appended to its target -- both
    chosen by the app. A target holding a CR is refused. With ``name``
    `shared_prefs`, the data-dir listing's own shared_prefs record is dropped.
    """
    (link,) = [r for r in _records(_listing(recorded, "run_as_find_names")) if r[5] == b"link"]
    derived = [*link[:4], link[4] + b"\r", name.encode()]
    stdout, stderr, status = _recorded_answer(recorded, "run_as_find_data_dir")
    assert not stderr and status == 0
    kept = [record for record in _records(stdout) if record[5] != name.encode()]
    return _joined([*kept, derived]), b"", status


def test_export_claims_no_absence_from_a_listing_that_refused_an_entry(
    tmp_path, monkeypatch, capsys, recorded
):
    """Absence is concluded only from a complete data-dir listing (review of #33).

    `shared_prefs` here is an entry the parser refused (a symlink whose target
    holds a CR). It was then reported `absent` and its listing skipped -- a
    claim made from a listing with a hole in it. Now: presence unknown, a
    refusal naming the refused entry, and nothing written.
    """
    result, snapshot, calls = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir=_seeded_with_a_refused_entry(recorded, "shared_prefs"),
        prefs="run_as_find_shared_prefs",
        dbs="run_as_find_databases",
        text=False,
    )

    claimed = json.loads(result.stdout).get("absent", [])
    assert "shared_prefs" not in claimed, f"absent claimed from an incomplete listing: {claimed}"
    payload = assert_refusal(result, json_mode=True, naming=("shared_prefs/",), status=1)
    assert "cannot tell whether" in payload["error"], payload["error"]
    assert payload["rejected"] == 1 and "shared_prefs -> e f.db\\r" in payload["error"]
    assert payload["rejected_entries"] == [repr("shared_prefs -> e f.db\r")], payload
    assert payload["presence_unknown"] == ["shared_prefs"], payload
    assert "absent" not in payload, payload
    assert not snapshot.exists(), "the refusal left a snapshot behind"
    assert [_listed(call) for call in calls if _listed(call)] == [FIXTURE_DIR], calls


def test_a_refused_entry_beside_both_directories_still_exports_and_refuses(
    tmp_path, monkeypatch, capsys, recorded
):
    """The control: the refused entry is another name, so presence is known.

    Both directories are listed and exported; the refusal is the existing
    one for a refused entry, with nothing called absent.
    """
    result, snapshot, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir=_seeded_with_a_refused_entry(recorded, "link"),
        prefs="run_as_find_shared_prefs",
        dbs="run_as_find_databases",
        text=False,
    )

    payload = assert_refusal(result, json_mode=True, naming=("refused 1 entry",), status=1)
    assert (payload["absent"], payload["unlisted"]) == ([], 0), payload
    assert _files_under(snapshot) == {
        "file_tree.txt",
        "databases.json",
        "shared_prefs/fixture_settings.xml",
    }


# ---------------------------------------------------------------------------
# What a run-as denial is (review of #33). The markers were matched anywhere in
# stdout and stderr, so `ls: <dir>: Permission denied` -- the filesystem
# refusing one directory of an app run-as had entered -- came back as a run-as
# denial with "install a debug build". Only a line run-as printed counts: on
# stderr, starting `run-as: `, at a non-zero exit (recorded without `2>&1`:
# run_as_not_an_application_stderr, run_as_unknown_package_stderr). The listing
# is find's now, and its `find: <dir>: Permission denied` is the same kind of
# failure (run_as_find_*_unreadable).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "recording"),
    [
        (("--shared-prefs",), "run_as_find_shared_prefs_unreadable"),
        (("--databases",), "run_as_find_databases_unreadable"),
        (("--ls", "shared_prefs"), "run_as_find_shared_prefs_unreadable"),
    ],
    ids=["shared-prefs", "databases", "ls-subdir"],
)
def test_an_unreadable_directory_is_a_filesystem_failure_not_a_run_as_denial(
    tmp_path, monkeypatch, capsys, recorded, action, recording
):
    reason = recorded.text(f"{recording}_stderr").strip()
    path = reason.removeprefix("find: ").removesuffix(": Permission denied")
    answer = _recorded_answer(recorded, recording)
    log = _routed_adb(tmp_path, monkeypatch, [(("find",), *answer)])

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554", *action, "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("permission denied",), status=1)
    assert reason in payload["error"], payload
    assert "run_as_denied" not in payload and "hint" not in payload, payload
    assert "run-as denied" not in payload["error"]
    assert "Install a debug build" not in payload["remedy"], payload["remedy"]
    assert f"adb shell run-as {FIXTURE_PKG} ls -ld {path}" in payload["remedy"]
    (call,) = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert _listed(call) == path, "the recording is not of this command"


@pytest.mark.parametrize(
    ("recording", "package"),
    [
        ("run_as_not_an_application_stderr", "com.android.settings"),
        ("run_as_unknown_package_stderr", "com.example.app1"),
    ],
)
def test_a_refusal_run_as_printed_is_still_a_run_as_denial(
    tmp_path, monkeypatch, capsys, recorded, recording, package
):
    """The two recorded refusals keep the debug-build remedy, from stderr where run-as put them."""
    _routed_adb(tmp_path, monkeypatch, [((), *_recorded_answer(recorded, recording))])

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", package, "--serial", "emulator-5554", "--shared-prefs", "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("run-as denied",), status=1)
    assert payload["run_as_denied"] is True, payload
    assert "Install a debug build" in payload["remedy"]
    assert recorded.text(recording).strip() in payload["error"]


def test_only_a_line_run_as_printed_is_a_denial(recorded):
    """The classifier itself, on recorded stderr: run-as's refusal is one, ls's and find's are not."""
    with pytest.raises(RunAsDeniedError):
        container._raise_if_denied(
            "com.android.settings", recorded.text("run_as_not_an_application_stderr"), 1
        )
    for name in (
        "run_as_ls_shared_prefs_unreadable",
        "run_as_ls_databases_unreadable",
        "run_as_find_shared_prefs_unreadable_stderr",
        "run_as_find_databases_unreadable_stderr",
        "run_as_find_shared_prefs_absent",
    ):
        container._raise_if_denied(FIXTURE_PKG, recorded.text(name), 1)


# ---------------------------------------------------------------------------
# REQUIRED 4 end to end: the recorded names directory through --ls and --export.
# Each transport is answered by its own recording of the same directory --
# find by run_as_find_names, `ls -la` by run_as_ls_names -- so the spec reads
# the same on the listing before the fix (where ghost.db was an entry) and on
# the one now.
# ---------------------------------------------------------------------------

NAMES_DIR = f"{FIXTURE_DIR}/files/aes_names"
REFUSED_NAMES = [repr("c\\d.db"), repr("lflink -> dangling\ntarget"), repr(FORGED)]


def _names_answers(recorded, directory: str) -> list:
    """Both recordings of the names directory, answering a listing of ``directory``."""
    return [
        (("find", directory), _listing(recorded, "run_as_find_names"), b"", 0),
        (("ls", "-la", directory), recorded.text("run_as_ls_names").encode(), b"", 0),
    ]


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_ls_lists_no_ghost_and_refuses_the_forging_name_as_itself(
    tmp_path, monkeypatch, capsys, recorded, json_mode
):
    """`--ls files/aes_names`: x.db's name is refused whole, and ghost.db is no entry.

    Through `ls -la` the forged line was ghost.db, a file the app does not
    have, and x.db was listed as a file of its own.
    """
    log = _routed_adb(tmp_path, monkeypatch, _names_answers(recorded, NAMES_DIR))

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554", "--ls", "files/aes_names"),
        *(["--json"] if json_mode else ["--verbose"]),
    )

    payload = assert_refusal(result, json_mode=json_mode, naming=("refused 3 entries",), status=1)
    if payload is not None:
        assert {e["name"] for e in payload["entries"]} == {"e f.db", "link", "sub", "dirlink"}
        assert payload["rejected_entries"] == REFUSED_NAMES, payload["rejected_entries"]
    else:
        assert f"{NAMES_DIR}  (4 entries, 3 refused)" in result.stdout, result.stdout
        assert f"  refused: {FORGED!r}" in result.stdout, result.stdout
        assert "\n-rw" not in result.stdout, "a forged line reached the terminal raw"
    assert "ghost.db  (file)" not in result.stdout
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [_listed(call) for call in calls] == [NAMES_DIR], calls


def test_export_writes_no_ghost_database_and_refuses_the_forging_name(
    tmp_path, monkeypatch, capsys, recorded
):
    """--export with databases/ answered by the names directory's recordings.

    A substitution of WHICH recorded listing answers databases/: the names
    directory was recorded under files/, and the listing's names do not carry
    the directory (find's %P). databases.json lists `e f.db` only -- never
    ghost.db -- and the export exits 1 naming the refused names.
    """
    find_root, ls_root = "run_as_find_data_dir", "run_as_ls_data_dir_seeded"
    rules = [
        (("cat",), recorded.text("shared_prefs_settings_xml").encode(), b"", 0),
        *_names_answers(recorded, f"{FIXTURE_DIR}/databases"),
        (
            ("find", f"{FIXTURE_DIR}/shared_prefs"),
            _listing(recorded, "run_as_find_shared_prefs"),
            b"",
            0,
        ),
        (
            ("ls", "-la", f"{FIXTURE_DIR}/shared_prefs"),
            recorded.text("run_as_ls_shared_prefs").encode(),
            b"",
            0,
        ),
        (("find", FIXTURE_DIR), _listing(recorded, find_root), b"", 0),
        (("ls", "-la", FIXTURE_DIR), recorded.text(ls_root).encode(), b"", 0),
    ]
    _routed_adb(tmp_path, monkeypatch, rules)
    out = tmp_path / "out"
    out.mkdir()

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554", "--export", str(out), "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("refused 3 entries",), status=1)
    assert payload["rejected_entries"] == REFUSED_NAMES, payload["rejected_entries"]
    databases = json.loads((out / FIXTURE_PKG / "databases.json").read_text(encoding="utf-8"))
    assert [db["name"] for db in databases["databases"]] == ["e f.db"], databases
    assert databases["rejected_entries"] == REFUSED_NAMES


@pytest.mark.parametrize("recording", ["run_as_find_unknown_primary", "run_as_find_unknown_escape"])
def test_a_find_that_cannot_list_this_way_is_not_blamed_on_the_path(
    tmp_path, monkeypatch, capsys, recorded, recording
):
    """find refusing the command names no path: the remedy is about find, not the directory.

    The recordings are this toybox's answers to a primary and an escape it
    does not know -- the stand-ins for a toybox without -printf or one of its
    escapes. Before, any failed listing was "the device refused <dir> itself".
    """
    _routed_adb(tmp_path, monkeypatch, [(("find",), *_recorded_answer(recorded, recording))])

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554", "--databases", "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("find: bad",), status=1)
    assert "toybox find" in payload["remedy"] and "Nothing was listed" in payload["remedy"]
    assert "refused" not in payload["remedy"], payload["remedy"]
    assert payload["path"] == f"{FIXTURE_DIR}/databases"


def test_a_listing_that_failed_at_an_entry_names_the_entry_not_the_directory(
    tmp_path, monkeypatch, capsys, recorded
):
    """run_as_find_databases_unsearchable (`chmod u-x`): find names `<dir>/.`, not `<dir>`.

    The remedy said the device refused the directory itself; it was the
    directory's x bit, and the listing failed at an entry. It says that now,
    naming the entry escaped and both bits, and still gives the `ls -ld` check.
    """
    directory = f"{FIXTURE_DIR}/databases"
    answer = _recorded_answer(recorded, "run_as_find_databases_unsearchable")
    assert answer[1].startswith(f"find: '{directory}/.': ".encode()), answer
    _routed_adb(tmp_path, monkeypatch, [(("find",), *answer)])

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554", "--databases", "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("permission denied",), status=1)
    remedy = payload["remedy"]
    assert f"listing {directory} failed at the entry {'.'!r}" in remedy, remedy
    assert "x bit" in remedy and "r bit" in remedy, remedy
    assert f"refused {directory} itself" not in remedy, remedy
    assert f"adb shell run-as {FIXTURE_PKG} ls -ld {directory}" in remedy, remedy


@pytest.mark.parametrize(
    "entry",
    ["fixture.db", "evil\rname': x", "a\nfind: /elsewhere: Permission denied"],
    ids=["plain", "cr-and-separator", "lf"],
)
def test_an_entry_failure_names_the_entry_escaped(recorded, entry):
    """Derived: run_as_find_databases_unreadable_stderr with ONLY its path, `<dir>`,
    replaced by `<dir>/<entry>` -- the position toybox prints an entry's path in
    (unquoted here, as that recording prints its path; the quoted form is
    run_as_find_databases_unsearchable's).

    A CR or a `': ` in the name stays in the name shown, escaped; a LF ends
    find's line, so the entry named is the part before it -- and the line is
    still the entry's, not another path's or the tool's.
    """
    directory = f"{FIXTURE_DIR}/databases"
    recorded_line = recorded.text("run_as_find_databases_unreadable_stderr")
    assert recorded_line.startswith(f"find: {directory}: "), recorded_line
    stderr = recorded_line.replace(directory, f"{directory}/{entry}", 1)

    remedy = run_as.listing_failure(FIXTURE_PKG, directory, "error", stderr)["remedy"]

    shown = entry.split("\n", 1)[0] if "\n" in entry else entry
    assert f"failed at the entry {shown!r}" in remedy, remedy
    assert "\r" not in remedy and "\n" not in remedy, remedy


def test_find_s_first_line_decides_whose_failure_it_is(recorded):
    """The three recorded kinds, each to its own remedy: the directory, an entry, the command."""
    directory = f"{FIXTURE_DIR}/databases"
    remedies = {
        name: run_as.listing_failure(FIXTURE_PKG, directory, "error", text)["remedy"]
        for name, text in [
            ("dir", recorded.text("run_as_find_databases_unreadable_stderr")),
            ("entry", recorded.text("run_as_find_databases_unsearchable_stderr")),
            ("tool", recorded.text("run_as_find_unknown_primary")),
        ]
    }
    assert remedies["dir"] == run_as.path_failure_remedy(FIXTURE_PKG, directory)
    assert "failed at the entry" in remedies["entry"]
    assert "toybox find" in remedies["tool"] and "refused" not in remedies["tool"]


def test_a_listing_of_a_file_is_refused_not_an_empty_directory(
    tmp_path, monkeypatch, capsys, recorded
):
    """`--ls shared_prefs/fixture_settings.xml`: find printed the file's own record (exit 0)."""
    _routed_adb(
        tmp_path,
        monkeypatch,
        [(("find",), *_recorded_answer(recorded, "run_as_find_not_a_directory"))],
    )

    result = _run_main(
        monkeypatch,
        capsys,
        *("--package", FIXTURE_PKG, "--serial", "emulator-5554"),
        *("--ls", "shared_prefs/fixture_settings.xml", "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("not a directory",), status=1)
    assert "entries" not in payload, payload
    assert "Name a directory" in payload["remedy"], payload["remedy"]


# ---------------------------------------------------------------------------
# Real SharedPreferences, from the fixture app's own data dir.
# ---------------------------------------------------------------------------


def test_shared_prefs_parses_every_type_android_writes(recorded):
    """The six types Android encodes differently, from a real prefs file.

    A parser that only ever saw ``<string>`` has not been tested, and the
    hand-written sample this complements was written by someone imagining the
    format. The fixture app now writes one of each so the corpus contains
    ground truth: note that ``<set>`` is nested ``<string>`` children rather
    than an attribute, and that the entries are not in insertion order.
    """
    parsed = parse_shared_prefs_xml(recorded.text("shared_prefs_settings_xml"))

    assert parsed["display_name"] == "Fixture User"
    assert parsed["launch_count"] == 7
    assert parsed["last_sync_epoch_ms"] == 1788280000000
    assert parsed["playback_speed"] == 1.25
    assert parsed["dark_theme"] is True
    assert sorted(parsed["enabled_flags"]) == ["compose", "telemetry"]


def test_shared_prefs_types_are_converted_not_left_as_strings(recorded):
    """`value="7"` must become 7, or every caller has to re-parse it."""
    parsed = parse_shared_prefs_xml(recorded.text("shared_prefs_settings_xml"))

    assert isinstance(parsed["launch_count"], int)
    assert isinstance(parsed["playback_speed"], float)
    assert isinstance(parsed["dark_theme"], bool)
    assert isinstance(parsed["enabled_flags"], list)
    assert not isinstance(parsed["dark_theme"], str), "boolean left as the literal 'true'"


def test_databases_listing_does_not_offer_the_journal_as_a_database(recorded):
    """`fixture.db-journal` sits beside `fixture.db` and is not a database."""
    listing = _listing(recorded, "run_as_find_databases")
    assert b"\0fixture.db-journal\0" in listing, "fixture no longer exercises the journal case"

    names = {entry["name"] for entry in parse_listing(listing).entries}
    assert names == {"fixture.db", "fixture.db-journal"}
    assert [db for db in sorted(names) if not container._is_sqlite_sidecar(db)] == ["fixture.db"]
