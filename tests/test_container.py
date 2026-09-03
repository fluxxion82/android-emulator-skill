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

import subprocess

import container
from container import (
    ContainerInspector,
    RunAsDeniedError,
    parse_ls_output,
    parse_shared_prefs_xml,
    parse_sqlite_schema,
)

# === parse_ls_output ===


def test_parse_ls_basic_entries(recorded):
    """Kinds, sizes and owners off a real `run-as ls -la`.

    The listing this replaced was hand-written: single-spaced columns, owner
    equal to group, no `total` header. The recording differs in all three, and
    the cache dirs have a group (u0_a205_cache) that is not the owner.
    """
    entries = {e["name"]: e for e in parse_ls_output(recorded.text("run_as_ls_data_dir"))}
    assert set(entries) == {"cache", "code_cache", "files"}
    assert entries["cache"]["kind"] == "dir"
    assert entries["cache"]["size_bytes"] == 4096
    assert entries["cache"]["owner"] == "u0_a205"
    assert entries["cache"]["group"] == "u0_a205_cache"

    files = {e["name"]: e for e in parse_ls_output(recorded.text("run_as_ls_databases"))}
    assert files["fixture.db"]["kind"] == "file"
    assert files["fixture.db"]["size_bytes"] == 32768
    assert files["fixture.db"]["owner"] == "u0_a205"


def test_parse_ls_skips_total_and_dot_entries(recorded):
    listing = recorded.text("run_as_ls_data_dir")
    assert listing.startswith("total "), "fixture no longer exercises the header case"

    names = [e["name"] for e in parse_ls_output(listing)]
    assert names == ["cache", "code_cache", "files"]


def test_parse_ls_ignores_garbage_lines():
    output = "this is not an ls line\nrandom junk\n"
    assert parse_ls_output(output) == []


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
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


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


def test_shared_prefs_dump_builds_exec_out_cat(monkeypatch, recorded):
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
    assert "exec-out" in cmd
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
    """Every listing served here is recorded output.

    Two substitutions on recorded lines, because no `ls -la` of a shared_prefs
    directory was captured: the data-dir listing gains the two subdirectories
    export descends into (by renaming recorded entries), and the prefs
    directory is the recorded databases listing with the file renamed to the
    prefs file the recorder actually read. Column widths, the `total` header
    and the `.`/`..` entries stay exactly as the device printed them.
    """
    ls_root = recorded.text("run_as_ls_data_dir").replace("code_cache", "shared_prefs")
    ls_root = ls_root.replace(" cache\n", " databases\n")
    ls_dbs = recorded.text("run_as_ls_databases")
    ls_prefs = ls_dbs.replace("fixture.db-journal", "notes.txt").replace(
        "fixture.db", "fixture_settings.xml"
    )
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
        # exec-out cat returns bytes (prefs files dumped during export)
        return subprocess.CompletedProcess(cmd, 0, stdout=prefs_xml, stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_text_run)

    ok, _result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is True

    dest = tmp_path / "com.example.app"
    assert (dest / "file_tree.txt").exists()
    assert (dest / "databases.json").exists()
    assert (dest / "shared_prefs" / "fixture_settings.xml").exists()


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

    entries = parse_ls_output(listing)
    names = {entry["name"] for entry in entries}
    assert "fixture.db" in names
    assert "." not in names and ".." not in names
