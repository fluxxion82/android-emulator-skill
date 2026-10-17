"""Device-free tests for container.py pure logic.

Three layers, all adb/subprocess-free unless explicitly monkeypatched:

1. ``parse_ls_output`` turns ``ls -la`` text into structured entries.
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
from container import (
    ContainerInspector,
    LsListing,
    RunAsDeniedError,
    parse_ls_output,
    parse_shared_prefs_xml,
    parse_sqlite_schema,
)
from fake_toolchain import (
    COLLIDING_FILE_NAMES,
    SYSTEM_PATH,
    assert_refusal,
    recorded_missing_file,
)

from common import cli

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


# === parse_ls_output ===


def test_parse_ls_basic_entries(recorded):
    """Kinds, sizes and owners off a real `run-as ls -la`.

    The listing this replaced was hand-written: single-spaced columns, owner
    equal to group, no `total` header. The recording differs in all three, and
    the cache dirs have a group (u0_a205_cache) that is not the owner.
    """
    listing = parse_ls_output(recorded.text("run_as_ls_data_dir"))
    assert listing.rejected == [], "a real recording had an entry refused"
    entries = {e["name"]: e for e in listing.entries}
    assert set(entries) == {"cache", "code_cache", "files"}
    assert entries["cache"]["kind"] == "dir"
    assert entries["cache"]["size_bytes"] == 4096
    assert entries["cache"]["owner"] == "u0_a205"
    assert entries["cache"]["group"] == "u0_a205_cache"

    files = {e["name"]: e for e in parse_ls_output(recorded.text("run_as_ls_databases")).entries}
    assert files["fixture.db"]["kind"] == "file"
    assert files["fixture.db"]["size_bytes"] == 32768
    assert files["fixture.db"]["owner"] == "u0_a205"


def test_parse_ls_skips_total_and_dot_entries(recorded):
    listing = recorded.text("run_as_ls_data_dir")
    assert listing.startswith("total "), "fixture no longer exercises the header case"

    names = [e["name"] for e in parse_ls_output(listing).entries]
    assert names == ["cache", "code_cache", "files"]


def test_parse_ls_refuses_lines_that_are_not_entries():
    """`ls -la` prints `total N` and entries; anything else is refused, never ignored."""
    output = "this is not an ls line\nrandom junk\n"
    assert parse_ls_output(output) == LsListing(
        entries=[], rejected=[repr("this is not an ls line"), repr("random junk")]
    )


# ---------------------------------------------------------------------------
# A symlink, and a filename with a space -- read off `run_as_ls_symlink_space`,
# a real `run-as ... ls -la` of a directory made in the fixture app's files/ to
# hold exactly those, and removed after. These were hand-written literals
# frozen in KNOWN_VIOLATIONS until the recording existed.
#
# The third literal frozen with them -- the GNU coreutils "Mon DD YYYY" date
# layout -- is gone and NOT replaced: no recording can back it. Every caller
# of parse_ls_output (container.py's list, prefs and databases paths) parses
# `run-as ... ls -la` on the device, which is toybox, and the recording shows
# toybox printing the ISO "YYYY-MM-DD HH:MM" layout. With no producer, the
# coreutils branch in `_extract_name` was removed too, rather than kept alive
# by a fixture for output this skill never reads; the old test's claim that
# the export path reads host listings is not borne out by container.py.
# ---------------------------------------------------------------------------


def _symlink_space_entries(recorded) -> dict[str, dict]:
    listing = parse_ls_output(recorded.text("run_as_ls_symlink_space"))
    assert listing.rejected == [], "a real recording had an entry refused"
    return {e["name"]: e for e in listing.entries}


def test_parse_ls_symlink_target(recorded):
    """`link -> a b.txt`: the name is `link`, the target keeps its space."""
    entry = _symlink_space_entries(recorded)["link"]
    assert entry["kind"] == "symlink"
    assert entry["mode"].startswith("l")
    assert entry["symlink_target"] == "a b.txt"
    # toybox reports a symlink's size as the length of its target string.
    assert entry["size_bytes"] == len("a b.txt")


def test_parse_ls_name_with_spaces(recorded):
    """toybox prints the spaced name bare -- unquoted, unescaped."""
    entries = _symlink_space_entries(recorded)
    assert set(entries) == {"a b.txt", "link"}, "dot entries or a split name leaked through"
    spaced = entries["a b.txt"]
    assert spaced["kind"] == "file"
    assert spaced["symlink_target"] is None
    assert spaced["size_bytes"] == 2, "`echo x` writes two bytes"


def test_the_recorded_date_layout_is_toyboxs_iso_form(recorded):
    """The layout the parser actually meets: two date tokens, ISO, not coreutils'.

    Pinned so that if a device ever answers in another layout, this fails before
    a name silently gains a date token.
    """
    for line in recorded.lines("run_as_ls_symlink_space")[1:]:
        day, clock = line.split()[5:7]
        assert len(day) == 10 and day[4] == "-" and day[7] == "-", line
        assert len(clock) == 5 and clock[2] == ":", line


def _derived_line(recorded, name: str, *, day: str | None = None) -> str:
    """The recorded `a b.txt` line of run_as_ls_symlink_space, with ONE column replaced.

    ``name`` replaces its name column -- what a file the app created would
    print as. ``day``, when given, also replaces its date column. Every other
    byte (mode, links, owner, group, size, the time column) is the device's.
    """
    line = next(
        line for line in recorded.lines("run_as_ls_symlink_space") if line.endswith(" a b.txt")
    )
    derived = line.removesuffix("a b.txt") + name
    if day is not None:
        # The date column precedes the name, so the first match is the column.
        recorded_day = line.split()[5]
        derived = derived.replace(f" {recorded_day} ", f" {day} ", 1)
    return derived


def _with_derived_entry(recorded, name: str, *, day: str | None = None) -> str:
    """The whole recorded listing, plus one derived line (see `_derived_line`)."""
    return recorded.text("run_as_ls_symlink_space") + _derived_line(recorded, name, day=day) + "\n"


@pytest.mark.parametrize("name", ["2026-10-16", "03:04", "May", "2024"])
def test_a_name_that_looks_like_a_timestamp_keeps_its_name(recorded, name):
    """The name is what follows the two timestamp columns, whatever it looks like.

    The parser used to skip every date- or time-looking token after the size
    column, and before that coreutils' month names and bare years too, so a
    file named any of these came back with no name and was dropped from the
    listing. Measured on emulator-5554: `run-as ... ls -la` of a directory
    holding exactly these four plus `a b.txt` printed them as-is after
    `2026-10-16 15:59`; the parser on main returned only `a b.txt`.

    The line is DERIVED (see `_derived_line`).
    """
    entries = {e["name"]: e for e in parse_ls_output(_with_derived_entry(recorded, name)).entries}

    assert set(entries) == {"a b.txt", "link", name}, entries
    assert entries[name]["kind"] == "file"
    assert entries[name]["size_bytes"] == entries["a b.txt"]["size_bytes"]


# ---------------------------------------------------------------------------
# A file name is chosen by the app, and export() makes host paths of the names
# it lists: they are untrusted input to the host. `/` and NUL cannot be in a
# real entry, so a listing that shows them is not what it claims; `\` is a
# separator on a Windows host and IS legal in a Linux file name; `.`/`..` walk
# the tree; a newline in a name forges a second `ls` line.
# ---------------------------------------------------------------------------

RECORDED_NAMES = {"a b.txt", "link"}


@pytest.mark.parametrize(
    "name",
    ["../escape", "..\\escape.xml", "evil\rname", "\rlead", "trail\r", "nul\x00name"],
    ids=["dotdot-slash", "dotdot-backslash", "inner-cr", "leading-cr", "trailing-cr", "nul"],
)
def test_a_name_that_is_not_a_plain_file_name_is_refused_and_reported(recorded, name):
    """Derived line (see `_derived_line`): the name is kept out of `entries` and counted.

    Refused, not dropped: `rejected` carries it escaped, so a listing whose
    only entry was refused cannot read as an empty directory.
    """
    listing = parse_ls_output(_with_derived_entry(recorded, name))

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES, listing.entries
    assert listing.rejected == [repr(name)], listing.rejected


def test_a_symlink_name_keeps_its_trailing_cr_and_is_refused(recorded):
    """The name before ` -> ` is taken as printed; stripping it would turn `link\\r` into `link`.

    Derived: the recorded `link -> a b.txt` line of run_as_ls_symlink_space,
    with its name `link` replaced by `link` + CR.
    """
    listing_text = recorded.text("run_as_ls_symlink_space")
    link_line = next(line for line in listing_text.split("\n") if " link -> " in line)
    derived = link_line.replace(" link -> ", " link\r -> ", 1)

    listing = parse_ls_output(listing_text + derived + "\n")

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES
    assert listing.rejected == [repr("link\r")]


@pytest.mark.parametrize("name", [".", ".."])
def test_the_directorys_own_dot_entries_are_skipped_not_refused(recorded, name):
    """`ls -la` prints `.` and `..` for every directory, and no file can take either name."""
    listing = parse_ls_output(_with_derived_entry(recorded, name))

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES
    assert listing.rejected == []


def test_a_newline_in_a_name_cannot_forge_a_hostile_entry(recorded):
    """A name ending in a newline plus a whole forged line: the forged entry is refused.

    The app names a file ``x`` + newline + (the recorded line, renamed
    ``../escape``). toybox prints it raw, so the listing gains a line that
    looks like an entry. Its name is not a plain file name, so it is refused and reported;
    the ``x`` before the newline is a real-looking entry and is kept -- the
    ambiguity `ls -1b` / `find -print0` would remove (a follow-up).
    """
    forged = _derived_line(recorded, "../escape")
    listing = parse_ls_output(_with_derived_entry(recorded, f"x\n{forged}"))

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES | {"x"}, listing.entries
    assert listing.rejected == [repr("../escape")]


def test_a_forged_line_with_no_timestamp_columns_is_refused(recorded):
    """The two timestamp columns are checked, not assumed: `yesterday` is no date.

    Derived: the recorded line with its DATE column replaced (see
    `_derived_line`), forged after a newline in a name as above.
    """
    forged = _derived_line(recorded, "forged.xml", day="yesterday")
    listing = parse_ls_output(_with_derived_entry(recorded, f"x\n{forged}"))

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES | {"x"}, listing.entries
    assert listing.rejected == [repr(forged)], "a line that is not the layout was not reported"


def test_only_a_newline_ends_a_line(recorded):
    """`str.splitlines` also breaks on U+2028 -- a name may hold one, and forge a line with it.

    ls separates entries with `\\n` alone, so the whole thing is ONE entry
    with an odd name, and no `forged.xml` appears.
    """
    forged = _derived_line(recorded, "forged.xml")
    listing = parse_ls_output(_with_derived_entry(recorded, f"x\u2028{forged}"))
    names = {e["name"] for e in listing.entries}

    assert "forged.xml" not in names, names
    assert names == RECORDED_NAMES | {f"x\u2028{forged}"}, names


# ---------------------------------------------------------------------------
# The same, through a real subprocess. `text=True` on the adb call applied
# universal newlines: a CR in a name became a line break BEFORE the parser saw
# it, so the checks above never ran on it. These run the real transport against
# an `adb` that writes exact bytes -- not tests/fake_toolchain.py's stub, which
# reads and writes text and would translate the CR itself.
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


@pytest.mark.parametrize(
    "name_of",
    [
        lambda forged: f"x\r{forged}",
        lambda forged: "\rlead",
        lambda forged: "trail\r",
    ],
    ids=["cr-forges-a-line", "leading-cr", "trailing-cr"],
)
def test_a_cr_in_a_name_survives_the_adb_transport(tmp_path, monkeypatch, recorded, name_of):
    """Derived listing (see `_derived_line`) carrying a CR, served as bytes by a fake adb.

    Before: `cr-forges-a-line` listed `x` and `forged.xml`, `trailing-cr`
    listed `trail` (renamed), and `leading-cr` vanished without a trace.
    """
    name = name_of(_derived_line(recorded, "forged.xml"))
    payload = _with_derived_entry(recorded, name).encode("utf-8")
    log = _bytes_adb(tmp_path, monkeypatch, payload)

    ok, result = ContainerInspector(serial="emulator-5554").list_dir("com.example.app")

    assert ok is True, result
    assert {e["name"] for e in result["entries"]} == RECORDED_NAMES, result["entries"]
    assert (result["rejected"], result["rejected_entries"]) == (1, [repr(name)])
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls == [
        [
            "-s",
            "emulator-5554",
            "shell",
            "run-as",
            "com.example.app",
            "ls",
            "-la",
            "/data/data/com.example.app",
        ]
    ], calls


def _run_main(monkeypatch, capsys, *argv: str) -> subprocess.CompletedProcess:
    """container.main through common.cli, with the --serial taken as given."""
    monkeypatch.setattr(container, "resolve_device_identifier", lambda serial: serial)
    with pytest.raises(SystemExit) as exc:
        cli.run(container.main, list(argv))
    out, err = capsys.readouterr()
    return subprocess.CompletedProcess(argv, exc.value.code, stdout=out, stderr=err)


def _prefs_listing(recorded, *names: str) -> bytes:
    """The recorded databases listing as a shared_prefs/ listing: its entries renamed.

    The `.`/`..`/`total` lines are the device's; each of the two file lines
    has its name column replaced (the first by ``names[0]``, and so on), and
    a file line left over is dropped.
    """
    lines = recorded.text("run_as_ls_databases").split("\n")
    out, files = [], iter(names)
    for line in lines:
        if line.startswith("-"):
            name = next(files, None)
            if name is None:
                continue
            line = line.rsplit(" ", 1)[0] + " " + name
        out.append(line)
    return "\n".join(out).encode("utf-8")


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


# ---------------------------------------------------------------------------
# Bytes that are not UTF-8, files listed but not read, modes ls could not
# read, and a CRLF transport: each is reported, none is "nothing there".
# ---------------------------------------------------------------------------


def _with_raw_name(line: str, raw_name: bytes) -> bytes:
    """``line`` (a derived line whose name column is NAME) with that name as raw bytes."""
    return line.encode("utf-8").replace(b"NAME", raw_name)


def test_a_name_that_is_not_utf8_is_refused_not_replaced(recorded):
    """`evil\\xff.xml`: decoded with replacement it became `evil\\ufffd.xml`, a name no file has.

    Derived: the recorded `a b.txt` line (see `_derived_line`) with its name
    column replaced by raw bytes that are not UTF-8.
    """
    raw_line = _with_raw_name(_derived_line(recorded, "NAME"), b"evil\xff.xml")
    listing = parse_ls_output(recorded.text("run_as_ls_symlink_space").encode() + raw_line + b"\n")

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES
    assert listing.rejected == [repr(raw_line)]


def test_a_line_whose_mode_ls_could_not_read_is_refused(recorded):
    """`??????????` is not a mode: refused and reported, not skipped as not-a-line.

    Derived: the recorded `a b.txt` line with its MODE column replaced.
    """
    line = _derived_line(recorded, "unknown.xml").replace("-rw-rw-rw-", "??????????", 1)
    listing = parse_ls_output(recorded.text("run_as_ls_symlink_space") + line + "\n")

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES
    assert listing.rejected == [repr(line)]


def test_a_symlink_target_holding_a_cr_is_refused(recorded):
    """Derived: the recorded `link -> a b.txt` line with a CR after its target."""
    text = recorded.text("run_as_ls_symlink_space")
    link_line = next(line for line in text.split("\n") if " link -> " in line)

    listing = parse_ls_output(text + link_line + "\r\n")

    assert {e["name"] for e in listing.entries} == RECORDED_NAMES
    assert listing.rejected == [repr("link -> a b.txt\r")]


def test_a_crlf_transport_refuses_every_entry_and_reports_each(recorded):
    """A shell without the v2 protocol ends lines CRLF; every entry is refused, none misread.

    Derived: the whole recorded listing with each LF made CRLF. Not handled
    as a transport (nothing here records a CRLF device); what matters is
    that it cannot pass as an empty directory or a listing of renamed files.
    """
    text = recorded.text("run_as_ls_symlink_space")
    listing = parse_ls_output(text.replace("\n", "\r\n"))

    assert listing.entries == []
    entry_lines = [line for line in text.split("\n") if line and not line.startswith("total ")]
    assert len(listing.rejected) == len(entry_lines) == 4, listing.rejected


PKG_DIR = "/data/data/com.example.app"


def _export_rules(recorded, prefs_listing: bytes, *cat_rules):
    """The fake adb's answers for an export: recorded listings and one recorded prefs file."""
    return [
        *cat_rules,
        (("cat",), recorded.text("shared_prefs_settings_xml").encode(), b"", 0),
        (("ls", f"{PKG_DIR}/shared_prefs"), prefs_listing, b"", 0),
        (("ls", f"{PKG_DIR}/databases"), recorded.text("run_as_ls_databases").encode(), b"", 0),
        (("ls", PKG_DIR), recorded.text("run_as_ls_data_dir_seeded").encode(), b"", 0),
    ]


def test_export_refuses_an_undecodable_name_instead_of_reading_another(
    tmp_path, monkeypatch, capsys, recorded
):
    """The regression: `evil\\xff.xml` exported as exit 0, success, rejected 0.

    Replaced, the name asked the device for `evil\\ufffd.xml`, which does not
    exist, and the failed read was skipped. Now the name is refused before
    any read, the run exits 1, and the recorded prefs file is still exported.
    """
    prefs = _prefs_listing(recorded, "fixture_settings.xml", "NAME").replace(
        b"NAME", b"evil\xff.xml"
    )
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
    raw_line = next(line for line in prefs.split(b"\n") if b"evil" in line)
    assert (payload["rejected"], payload["rejected_entries"]) == (1, [repr(raw_line)])
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


def test_list_dir_builds_run_as_ls(monkeypatch, recorded):
    """Parses a real `run-as ls -la`, not a tidy one-liner.

    The hand-written sample this replaced was
    `-rw------- 1 u0_a1 u0_a1 10 2024-01-02 03:04 f.txt` -- single-spaced, no
    header, owner equal to group. Real output has a `total 52` line, `.` and
    `..` entries, variable-width padding, and a group that differs from the
    owner on cache dirs (u0_a205 vs u0_a205_cache). Every one of those is a
    chance for the parser to be wrong in a way the tidy sample could not show.
    """
    captured: list[list[str]] = []
    listing = recorded.text("run_as_ls_data_dir")

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout=listing)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector(serial="emulator-5554").list_dir("com.example.app")
    assert ok is True

    # `.`, `..` and the `total` header must not be counted as entries.
    assert result["total_entries"] == 3, f"miscounted real ls output: {result}"
    names = {entry["name"] for entry in result["entries"]}
    assert names == {"cache", "code_cache", "files"}, names

    cmd = captured[0]
    # Targets the right device, uses run-as for the right package, lists the data dir.
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]
    assert "run-as" in cmd
    assert "com.example.app" in cmd
    assert "ls" in cmd and "-la" in cmd
    assert "/data/data/com.example.app" in cmd


def test_list_dir_subpath_joined_relative(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout="")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ContainerInspector().list_dir("com.example.app", subpath="files/cache")
    assert "/data/data/com.example.app/files/cache" in captured[0]


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
    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, returncode=1, stderr=recorded.text("run_as_unknown_package"))

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().list_dir("com.nope")
    assert ok is False
    assert result.get("run_as_denied") is True


def test_raise_if_denied_helper():
    import pytest

    with pytest.raises(RunAsDeniedError):
        container._raise_if_denied("com.x", "run-as: package not debuggable", 1)
    # Zero exit code never denies, even if text contains a marker.
    container._raise_if_denied("com.x", "is not debuggable", 0)


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


def test_databases_list_separates_sidecars(monkeypatch, recorded):
    """The journal beside the database is not a database.

    The listing this replaced invented `-wal` and `-shm` companions. What the
    fixture app actually leaves in databases/ is a `-journal` file at zero
    bytes, which is the case a caller most often hits.
    """
    listing = recorded.text("run_as_ls_databases")

    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, stdout=listing)

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
    listing holds them, so the root is the seeded data dir
    (run_as_ls_data_dir_seeded, which holds both); it and the databases
    listing are served verbatim -- no synthesised topology.

    The single derivation is the prefs directory, kept for the filter it
    exercises: the recorded databases listing has its `fixture.db-journal`
    entry renamed to `fixture_settings.xml`, the prefs file the recorder
    actually read. That leaves a directory holding one `.xml` and one file
    that is not one, which is what `shared_prefs()` has to filter (the real
    prefs listing, run_as_ls_shared_prefs, holds only the `.xml`). Column
    widths, the `total` header and the `.`/`..` entries are exactly as the
    device printed them.
    """
    ls_root = recorded.text("run_as_ls_data_dir_seeded")
    ls_dbs = recorded.text("run_as_ls_databases")
    ls_prefs = ls_dbs.replace("fixture.db-journal", "fixture_settings.xml")
    prefs_xml = recorded.text("shared_prefs_settings_xml").encode("utf-8")

    def fake_text_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "ls" in cmd:
            # Route the directory listing by the target path it ends in.
            if joined.endswith("/shared_prefs"):
                return _completed(cmd, stdout=ls_prefs)
            if joined.endswith("/databases"):
                return _completed(cmd, stdout=ls_dbs)
            return _completed(cmd, stdout=ls_root)
        # the cat answers in bytes (prefs files dumped during export)
        return subprocess.CompletedProcess(cmd, 0, stdout=prefs_xml, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_text_run)

    ok, _result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is True

    dest = tmp_path / "com.example.app"
    assert (dest / "file_tree.txt").exists()
    assert (dest / "databases.json").exists()
    assert (dest / "shared_prefs" / "fixture_settings.xml").exists()


def _serve_export(monkeypatch, recorded, ls_prefs: str) -> bytes:
    """Serve export() the recorded listings, with ``ls_prefs`` for shared_prefs/.

    Returns the recorded prefs bytes every `cat` answers with.
    """
    ls_root = recorded.text("run_as_ls_data_dir_seeded")
    ls_dbs = recorded.text("run_as_ls_databases")
    prefs_xml = recorded.text("shared_prefs_settings_xml").encode("utf-8")

    def fake_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if "ls" in cmd:
            if joined.endswith("/shared_prefs"):
                return _completed(cmd, stdout=ls_prefs)
            if joined.endswith("/databases"):
                return _completed(cmd, stdout=ls_dbs)
            return _completed(cmd, stdout=ls_root)
        return subprocess.CompletedProcess(cmd, 0, stdout=prefs_xml, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    return prefs_xml


def _files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_export_writes_only_plain_names_inside_the_snapshot(monkeypatch, tmp_path, recorded):
    """Hostile prefs names are not exported; the legitimate recorded one still is.

    The prefs listing is test_export_writes_snapshot's (the recorded databases
    listing with `fixture.db-journal` renamed `fixture_settings.xml`), with two
    derived entries added by substituting the name column of its own
    `fixture.db` line: `..\\escape.xml` -- a legal Linux file name, a path on
    a Windows host -- and `x` + newline + a forged line named
    `../../escape.xml`.
    """
    ls_dbs = recorded.text("run_as_ls_databases")
    db_line = next(line for line in ls_dbs.split("\n") if line.endswith(" fixture.db"))

    def named(name: str) -> str:
        return db_line.removesuffix("fixture.db") + name

    ls_prefs = (
        ls_dbs.replace("fixture.db-journal", "fixture_settings.xml")
        + named("..\\escape.xml")
        + "\n"
        + named("x\n" + named("../../escape.xml"))
        + "\n"
    )
    prefs_xml = _serve_export(monkeypatch, recorded, ls_prefs)
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

    parse_ls_output drops such names first, so this listing is handed to
    export() directly -- the case it guards is a listing that reached it
    another way. It refuses before creating anything.
    """
    _serve_export(monkeypatch, recorded, recorded.text("run_as_ls_databases"))
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
    listing = recorded.text("run_as_ls_data_dir")

    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, stdout=listing)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    (tmp_path / "com.example.app").mkdir()
    ok, result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is False
    assert "already exists" in result["error"]


# ---------------------------------------------------------------------------
# A sub-listing that fails (final review of v0.8.0, C2). export() took any
# failed `ls -la` of shared_prefs/ or databases/ for an empty directory: exit 0,
# `success: true`, `unread: 0`, the databases failure visible only inside the
# databases.json it wrote. Every listing below is the fixture app's own,
# recorded for it -- so these runs use its package, and the reason in each
# recorded failure names exactly the path export() lists.
# ---------------------------------------------------------------------------

FIXTURE_PKG = "com.example.composefixture"
FIXTURE_DIR = f"/data/data/{FIXTURE_PKG}"


def _recorded_answer(recorded, name: str) -> tuple[bytes, bytes, int]:
    """(stdout, stderr, exit) of a recorded command, each stream where the manifest says it was."""
    entry = {f["name"]: f for f in recorded.manifest["fixtures"]}[name]
    text = recorded.text(name).encode()
    if entry.get("stream") == "stderr":
        assert entry["stdout_bytes"] == 0, entry
        return b"", text, entry["exit_status"]
    return text, b"", entry.get("exit_status", 0)


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
            (("ls", f"{FIXTURE_DIR}/shared_prefs"), *_recorded_answer(recorded, prefs)),
            (("ls", f"{FIXTURE_DIR}/databases"), *_recorded_answer(recorded, dbs)),
            (("ls", FIXTURE_DIR), *root),
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

    run_as_ls_data_dir was recorded before the app first ran, so it names
    neither directory; the seeded listing names both.
    """
    for name, reason in [
        ("run_as_ls_shared_prefs_unreadable", "shared_prefs: Permission denied"),
        ("run_as_ls_databases_unreadable", "databases: Permission denied"),
        ("run_as_ls_shared_prefs_absent", "shared_prefs: No such file or directory"),
    ]:
        stdout, stderr, status = _recorded_answer(recorded, name)
        assert (stdout, status) == (b"", 1), name
        assert stderr.decode().strip() == f"ls: {FIXTURE_DIR}/{reason}", name
    seeded = {
        e["name"] for e in parse_ls_output(recorded.text("run_as_ls_data_dir_seeded")).entries
    }
    unseeded = {e["name"] for e in parse_ls_output(recorded.text("run_as_ls_data_dir")).entries}
    assert {"shared_prefs", "databases"} <= seeded
    assert not {"shared_prefs", "databases"} & unseeded


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
    listings = {"prefs": "run_as_ls_shared_prefs", "dbs": "run_as_ls_databases"}
    listings["prefs" if unreadable == "shared_prefs" else "dbs"] = (
        f"run_as_ls_{unreadable}_unreadable"
    )
    reason = recorded.text(f"run_as_ls_{unreadable}_unreadable").strip()

    result, snapshot, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_ls_data_dir_seeded",
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
        data_dir="run_as_ls_data_dir_seeded",
        prefs="run_as_ls_shared_prefs_unreadable",
        dbs="run_as_ls_databases_unreadable",
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
    reason = recorded.text("run_as_ls_shared_prefs_absent").strip()

    result, _, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_ls_data_dir_seeded",
        prefs="run_as_ls_shared_prefs_absent",
        dbs="run_as_ls_databases",
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

    run_as_ls_data_dir is that app -- recorded before the fixture app first
    ran. Neither directory is listed (the recorded "No such file" would be
    the answer if one were), and databases.json records the absence instead
    of an error.
    """
    result, snapshot, calls = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_ls_data_dir",
        prefs="run_as_ls_shared_prefs_absent",
        dbs="run_as_ls_shared_prefs_absent",
        text=text,
    )

    assert result.returncode == 0, result
    listed = [call[-1] for call in calls if "ls" in call]
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
    reason = recorded.text("run_as_ls_shared_prefs_unreadable").strip()

    result, snapshot, _ = _fixture_app_export(
        tmp_path,
        monkeypatch,
        capsys,
        recorded,
        data_dir="run_as_ls_data_dir_seeded",
        prefs="run_as_ls_shared_prefs_unreadable",
        dbs="run_as_ls_databases",
        text=True,
    )

    assert result.returncode == 1, result
    assert f"Exported {FIXTURE_PKG} -> {snapshot}" in result.stdout
    assert "1 directory not listed" in result.stdout, result.stdout
    assert "Error: could not list 1 directory" in result.stderr, result.stderr
    assert reason in result.stderr
    assert "Remedy:" in result.stderr


def _seeded_with_a_refused_entry(recorded, name: str) -> tuple[bytes, bytes, int]:
    """The seeded data-dir listing plus one entry parse_ls_output refuses, as adb answers it.

    Derived: the recorded `link -> a b.txt` line of run_as_ls_symlink_space,
    with its name replaced by ``name`` and a CR appended to its target --
    both chosen by the app. A target holding a CR is refused. With ``name``
    `shared_prefs`, the seeded listing's own shared_prefs line is dropped.
    """
    (link,) = [line for line in recorded.lines("run_as_ls_symlink_space") if " link -> " in line]
    derived = link.replace(" link -> a b.txt", f" {name} -> a b.txt\r")
    assert derived != link
    kept = [
        line
        for line in recorded.lines("run_as_ls_data_dir_seeded")
        if not line.endswith(f" {name}")
    ]
    _, stderr, status = _recorded_answer(recorded, "run_as_ls_data_dir_seeded")
    assert not stderr and status == 0
    return "".join(f"{line}\n" for line in [*kept, derived]).encode(), b"", status


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
        prefs="run_as_ls_shared_prefs",
        dbs="run_as_ls_databases",
        text=False,
    )

    claimed = json.loads(result.stdout).get("absent", [])
    assert "shared_prefs" not in claimed, f"absent claimed from an incomplete listing: {claimed}"
    payload = assert_refusal(result, json_mode=True, naming=("shared_prefs/",), status=1)
    assert "cannot tell whether" in payload["error"], payload["error"]
    assert payload["rejected"] == 1 and "shared_prefs -> a b.txt\\r" in payload["error"]
    assert payload["presence_unknown"] == ["shared_prefs"], payload
    assert "absent" not in payload, payload
    assert not snapshot.exists(), "the refusal left a snapshot behind"
    assert [call[-1] for call in calls if "ls" in call] == [FIXTURE_DIR], calls


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
        prefs="run_as_ls_shared_prefs",
        dbs="run_as_ls_databases",
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
# run_as_not_an_application_stderr, run_as_unknown_package_stderr).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "recording"),
    [
        (("--shared-prefs",), "run_as_ls_shared_prefs_unreadable"),
        (("--databases",), "run_as_ls_databases_unreadable"),
        (("--ls", "shared_prefs"), "run_as_ls_shared_prefs_unreadable"),
    ],
    ids=["shared-prefs", "databases", "ls-subdir"],
)
def test_an_unreadable_directory_is_a_filesystem_failure_not_a_run_as_denial(
    tmp_path, monkeypatch, capsys, recorded, action, recording
):
    reason = recorded.text(recording).strip()
    path = reason.removeprefix("ls: ").removesuffix(": Permission denied")
    log = _routed_adb(tmp_path, monkeypatch, [(("ls",), *_recorded_answer(recorded, recording))])

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
    assert call[-3:] == ["ls", "-la", path], "the recording is not of this command"


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
    """The classifier itself, on recorded stderr: run-as's refusal is one, ls's is not."""
    with pytest.raises(RunAsDeniedError):
        container._raise_if_denied(
            "com.android.settings", recorded.text("run_as_not_an_application_stderr"), 1
        )
    for name in ("run_as_ls_shared_prefs_unreadable", "run_as_ls_databases_unreadable"):
        container._raise_if_denied(FIXTURE_PKG, recorded.text(name), 1)


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
    listing = recorded.text("run_as_ls_databases")
    assert "fixture.db-journal" in listing, "fixture no longer exercises the journal case"

    names = {entry["name"] for entry in parse_ls_output(listing).entries}
    assert "fixture.db" in names
    assert "." not in names and ".." not in names
