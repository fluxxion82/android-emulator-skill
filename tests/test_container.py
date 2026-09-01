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


def test_parse_ls_basic_entries():
    output = (
        "total 24\n"
        "drwx------ 4 u0_a123 u0_a123 4096 2024-01-02 03:04 shared_prefs\n"
        "-rw------- 1 u0_a123 u0_a123  220 2024-01-02 03:04 prefs.xml\n"
    )
    entries = parse_ls_output(output)
    assert len(entries) == 2

    by_name = {e["name"]: e for e in entries}
    assert by_name["shared_prefs"]["kind"] == "dir"
    assert by_name["prefs.xml"]["kind"] == "file"
    assert by_name["prefs.xml"]["size_bytes"] == 220
    assert by_name["prefs.xml"]["owner"] == "u0_a123"


def test_parse_ls_skips_total_and_dot_entries():
    output = (
        "total 8\n"
        "drwx------ 2 u0_a1 u0_a1 4096 2024-01-02 03:04 .\n"
        "drwx------ 9 u0_a1 u0_a1 4096 2024-01-02 03:04 ..\n"
        "-rw------- 1 u0_a1 u0_a1   10 2024-01-02 03:04 file.txt\n"
    )
    entries = parse_ls_output(output)
    names = [e["name"] for e in entries]
    assert names == ["file.txt"]


def test_parse_ls_symlink_target():
    output = "lrwxrwxrwx 1 root root 11 2024-01-02 03:04 lib -> /data/app/lib\n"
    entries = parse_ls_output(output)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "symlink"
    assert entry["name"] == "lib"
    assert entry["symlink_target"] == "/data/app/lib"


def test_parse_ls_name_with_spaces():
    output = "-rw------- 1 u0_a1 u0_a1 5 2024-01-02 03:04 my file.txt\n"
    entries = parse_ls_output(output)
    assert entries[0]["name"] == "my file.txt"


def test_parse_ls_coreutils_layout_with_year():
    # GNU coreutils ls: month, day, year/time as separate tokens.
    output = "-rw-r--r-- 1 user group 1024 Jan  2  2024 data.bin\n"
    entries = parse_ls_output(output)
    assert entries[0]["name"] == "data.bin"
    assert entries[0]["size_bytes"] == 1024


def test_parse_ls_ignores_garbage_lines():
    output = "this is not an ls line\nrandom junk\n"
    assert parse_ls_output(output) == []


# === parse_shared_prefs_xml ===


PREFS_XML = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="username">alice</string>
    <int name="launch_count" value="7" />
    <long name="last_seen" value="1700000000000" />
    <float name="volume" value="0.8" />
    <boolean name="dark_mode" value="true" />
    <boolean name="notifications" value="false" />
    <set name="tags">
        <string>red</string>
        <string>green</string>
    </set>
    <string name="empty"></string>
</map>
"""


def test_parse_prefs_types():
    prefs = parse_shared_prefs_xml(PREFS_XML)
    assert prefs["username"] == "alice"
    assert prefs["launch_count"] == 7
    assert isinstance(prefs["launch_count"], int)
    assert prefs["last_seen"] == 1700000000000
    assert prefs["volume"] == 0.8
    assert prefs["dark_mode"] is True
    assert prefs["notifications"] is False
    assert prefs["tags"] == ["red", "green"]
    assert prefs["empty"] == ""


def test_parse_prefs_keys_complete():
    prefs = parse_shared_prefs_xml(PREFS_XML)
    assert set(prefs) == {
        "username",
        "launch_count",
        "last_seen",
        "volume",
        "dark_mode",
        "notifications",
        "tags",
        "empty",
    }


def test_parse_prefs_malformed_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_shared_prefs_xml("<map><string name='x'>unclosed")


def test_parse_prefs_bad_numeric_defaults_to_zero():
    xml = '<map><int name="n" value="notanumber" /></map>'
    assert parse_shared_prefs_xml(xml)["n"] == 0


# === parse_sqlite_schema ===


SCHEMA = """CREATE TABLE android_metadata (locale TEXT);
CREATE TABLE `users` (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    created_at INTEGER DEFAULT 0,
    FOREIGN KEY (team_id) REFERENCES teams(id)
);
CREATE INDEX idx_users_email ON users(email);
CREATE TABLE IF NOT EXISTS "notes" (
    note_id INTEGER PRIMARY KEY,
    body TEXT,
    user_id INTEGER
);
"""


def test_parse_schema_table_names():
    tables = parse_sqlite_schema(SCHEMA)
    names = [t["name"] for t in tables]
    assert names == ["android_metadata", "users", "notes"]


def test_parse_schema_columns_and_types():
    tables = {t["name"]: t for t in parse_sqlite_schema(SCHEMA)}
    user_cols = {c["name"]: c["type"] for c in tables["users"]["columns"]}
    assert user_cols["id"] == "INTEGER"
    assert user_cols["name"] == "TEXT"
    assert user_cols["email"] == "TEXT"
    assert user_cols["created_at"] == "INTEGER"
    # The FOREIGN KEY table-level constraint is not a column.
    assert "team_id" not in user_cols
    assert "FOREIGN" not in user_cols


def test_parse_schema_skips_indexes():
    tables = parse_sqlite_schema(SCHEMA)
    assert all(t["name"] != "idx_users_email" for t in tables)


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
    assert result["run_as_denied"] is True, (
        f"a real run-as denial was reported as a generic failure: {result}"
    )
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


def test_shared_prefs_dump_builds_exec_out_cat(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=PREFS_XML.encode("utf-8"), stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().shared_prefs("com.example.app", name="settings")
    assert ok is True
    assert result["file"] == "settings.xml"
    assert result["preferences"]["username"] == "alice"

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


def test_databases_list_separates_sidecars(monkeypatch):
    listing = (
        "total 48\n"
        "-rw------- 1 u0_a1 u0_a1 16384 2024-01-02 03:04 app.db\n"
        "-rw------- 1 u0_a1 u0_a1  8192 2024-01-02 03:04 app.db-wal\n"
        "-rw------- 1 u0_a1 u0_a1 32768 2024-01-02 03:04 app.db-shm\n"
    )

    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, stdout=listing)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().databases("com.example.app")
    assert ok is True
    assert result["total_databases"] == 1
    assert result["databases"][0]["name"] == "app.db"
    assert set(result["sidecars"]) == {"app.db-wal", "app.db-shm"}


def test_databases_schema_via_device_sqlite3(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout=SCHEMA)

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    ok, result = ContainerInspector().databases("com.example.app", name="app.db")
    assert ok is True
    assert result["method"] == "device-sqlite3"
    assert result["total_tables"] == 3
    # The on-device sqlite3 strategy must be attempted.
    assert any("sqlite3" in c and ".schema" in c for c in captured)


def test_export_writes_snapshot(monkeypatch, tmp_path):
    ls_root = (
        "total 12\n"
        "drwx------ 2 u0_a1 u0_a1 4096 2024-01-02 03:04 shared_prefs\n"
        "drwx------ 2 u0_a1 u0_a1 4096 2024-01-02 03:04 databases\n"
        "-rw------- 1 u0_a1 u0_a1  220 2024-01-02 03:04 a.txt\n"
    )
    ls_prefs = "-rw------- 1 u0_a1 u0_a1 100 2024-01-02 03:04 settings.xml\n"
    ls_dbs = "-rw------- 1 u0_a1 u0_a1 4096 2024-01-02 03:04 app.db\n"

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
        return subprocess.CompletedProcess(cmd, 0, stdout=PREFS_XML.encode("utf-8"), stderr=b"")

    monkeypatch.setattr(container.subprocess, "run", fake_text_run)

    ok, _result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is True

    dest = tmp_path / "com.example.app"
    assert (dest / "file_tree.txt").exists()
    assert (dest / "databases.json").exists()
    assert (dest / "shared_prefs" / "settings.xml").exists()


def test_export_refuses_existing_destination(monkeypatch, tmp_path):
    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, stdout="-rw------- 1 u0_a1 u0_a1 1 2024-01-02 03:04 a\n")

    monkeypatch.setattr(container.subprocess, "run", fake_run)

    (tmp_path / "com.example.app").mkdir()
    ok, result = ContainerInspector().export("com.example.app", str(tmp_path))
    assert ok is False
    assert "already exists" in result["error"]
