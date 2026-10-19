"""Device-free tests for model_inspector.py.

Three modes, all adb/subprocess-free unless explicitly monkeypatched:

1. SOURCE: a Kotlin @Entity/@Database fixture -> parsed tables/columns/version.
2. SCHEMA: a Room exported-schema JSON fixture (tmp_path) -> entities/fields/
   version, plus --show-versions and --raw behavior.
3. LIVE: a "sqlite .schema" fixture -> parsed tables; run-as command
   construction asserted with subprocess.run monkeypatched. No real device.
"""

from __future__ import annotations

import json
import subprocess

import model_inspector
import pytest
from fake_toolchain import (
    COLLIDING_FILE_NAMES,
    FakeToolchain,
    Rule,
    assert_refusal,
    recorded_missing_file,
)
from model_inspector import (
    LiveInspector,
    SchemaInspector,
    SourceInspector,
    parse_sqlite_schema,
    strip_comments,
)

from common.run_as import parse_adb_version


@pytest.fixture(autouse=True)
def _the_recorded_adb_client(monkeypatch, recorded):
    """A database read first asks `adb version`; in-process, answer with the recorded banner.

    The CLI tests at the bottom run the script as a subprocess, where this does
    not reach: there the fake adb answers `adb version` itself.
    """
    version = parse_adb_version(recorded.text("adb_version"))
    assert version is not None, "the recorded banner no longer parses"
    monkeypatch.setattr(model_inspector, "adb_client_version", lambda: version)


# === SOURCE MODE ===========================================================


KOTLIN_SOURCE = """
package com.example.app.data

import androidx.room.*

// A user row in the app database.
@Entity(tableName = "users")
data class User(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    @ColumnInfo(name = "full_name")
    val name: String,
    val email: String?,
    @Ignore
    val transientCache: String? = null
)

/* The notes table references users via a foreign key. */
@Entity(
    foreignKeys = [ForeignKey(entity = User::class, parentColumns = ["id"], childColumns = ["user_id"])]
)
data class Note(
    @PrimaryKey val noteId: Long,
    @ColumnInfo(name = "user_id")
    @ForeignKey
    val userId: Long,
    val body: String
)

@Dao
interface UserDao {
    @Query("SELECT * FROM users")
    fun all(): List<User>
}

@Database(entities = [User::class, Note::class], version = 4)
abstract class AppDatabase : RoomDatabase() {
    abstract fun userDao(): UserDao
}
"""


def _write_source(tmp_path, content: str, name: str = "Model.kt"):
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / name).write_text(content, encoding="utf-8")
    return src_dir


def test_source_parses_entities_tables_and_columns(tmp_path):
    src_dir = _write_source(tmp_path, KOTLIN_SOURCE)
    ok, result = SourceInspector(str(src_dir)).execute()
    assert ok is True

    by_class = {e["class_name"]: e for e in result["entities"]}
    assert set(by_class) == {"User", "Note"}

    # tableName override is honored; Note falls back to its class name.
    assert by_class["User"]["table"] == "users"
    assert by_class["Note"]["table"] == "Note"

    user_cols = {c["name"]: c for c in by_class["User"]["columns"]}
    # @Ignore field is excluded.
    assert "transientCache" not in user_cols
    # @ColumnInfo(name=...) renames the column.
    assert "full_name" in user_cols
    assert user_cols["full_name"]["field"] == "name"
    # @PrimaryKey flag detected.
    assert user_cols["id"]["primary_key"] is True
    assert user_cols["email"]["primary_key"] is False


def test_source_parses_foreign_key_flags(tmp_path):
    src_dir = _write_source(tmp_path, KOTLIN_SOURCE)
    ok, result = SourceInspector(str(src_dir)).execute()
    assert ok is True

    note = next(e for e in result["entities"] if e["class_name"] == "Note")
    cols = {c["name"]: c for c in note["columns"]}
    assert cols["user_id"]["foreign_key"] is True
    # Entity-level @ForeignKey referencing User is captured.
    assert any(fk["entity"] == "User" for fk in note["foreign_keys"])


def test_source_parses_database_version_and_entities(tmp_path):
    src_dir = _write_source(tmp_path, KOTLIN_SOURCE)
    ok, result = SourceInspector(str(src_dir)).execute()
    assert ok is True

    assert len(result["databases"]) == 1
    db = result["databases"][0]
    assert db["class_name"] == "AppDatabase"
    assert db["version"] == 4
    assert db["entities"] == ["User", "Note"]
    assert "UserDao" in result["daos"]


def test_source_missing_dir_fails(tmp_path):
    ok, result = SourceInspector(str(tmp_path / "nope")).execute()
    assert ok is False
    assert "not found" in result["error"]


def test_source_dedupes_across_rescan(tmp_path):
    # Two files declaring the same entity should not double-count.
    src_dir = _write_source(tmp_path, KOTLIN_SOURCE, name="A.kt")
    (src_dir / "B.kt").write_text(KOTLIN_SOURCE, encoding="utf-8")
    ok, result = SourceInspector(str(src_dir)).execute()
    assert ok is True
    classes = [e["class_name"] for e in result["entities"]]
    assert classes.count("User") == 1


def test_strip_comments_removes_line_and_block():
    text = 'val x = "a // not a comment" // real\n/* block */ val y = 1'
    stripped = strip_comments(text)
    assert "// real" not in stripped
    assert "block" not in stripped
    # String literal content is preserved.
    assert "a // not a comment" in stripped


# === SCHEMA MODE ===========================================================


SCHEMA_DOC = {
    "formatVersion": 1,
    "database": {
        "version": 3,
        "entities": [
            {
                "tableName": "users",
                "entityName": "com.example.app.data.User",
                "createSql": "CREATE TABLE ...",
                "fields": [
                    {"fieldPath": "id", "columnName": "id", "affinity": "INTEGER", "notNull": True},
                    {
                        "fieldPath": "name",
                        "columnName": "full_name",
                        "affinity": "TEXT",
                        "notNull": True,
                    },
                    {
                        "fieldPath": "email",
                        "columnName": "email",
                        "affinity": "TEXT",
                        "notNull": False,
                    },
                ],
                "primaryKey": {"columnNames": ["id"], "autoGenerate": True},
                "indices": [
                    {"name": "index_users_email", "unique": True, "columnNames": ["email"]}
                ],
                "foreignKeys": [],
            },
            {
                "tableName": "notes",
                "entityName": "com.example.app.data.Note",
                "fields": [
                    {
                        "fieldPath": "noteId",
                        "columnName": "noteId",
                        "affinity": "INTEGER",
                        "notNull": True,
                    },
                    {
                        "fieldPath": "userId",
                        "columnName": "user_id",
                        "affinity": "INTEGER",
                        "notNull": True,
                    },
                ],
                "primaryKey": {"columnNames": ["noteId"]},
                "indices": [],
                "foreignKeys": [
                    {
                        "table": "users",
                        "onDelete": "CASCADE",
                        "onUpdate": "NO ACTION",
                        "columns": ["user_id"],
                        "referencedColumns": ["id"],
                    }
                ],
            },
        ],
    },
}


def _write_schema(tmp_path, version: int, doc: dict):
    db_dir = tmp_path / "schemas" / "com.example.app.data.AppDatabase"
    db_dir.mkdir(parents=True, exist_ok=True)
    path = db_dir / f"{version}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_schema_parses_entities_fields_and_version(tmp_path):
    path = _write_schema(tmp_path, 3, SCHEMA_DOC)
    ok, result = SchemaInspector(str(path)).execute()
    assert ok is True
    assert result["version"] == 3

    by_table = {e["table"]: e for e in result["entities"]}
    assert set(by_table) == {"users", "notes"}

    users = by_table["users"]
    assert users["entity_name"] == "User"
    assert users["primary_key"] == ["id"]
    fields = {f["column_name"]: f for f in users["fields"]}
    assert fields["full_name"]["field_path"] == "name"
    assert fields["full_name"]["affinity"] == "TEXT"
    assert fields["id"]["not_null"] is True
    assert fields["email"]["not_null"] is False
    assert users["indices"][0]["name"] == "index_users_email"
    assert users["indices"][0]["unique"] is True

    notes = by_table["notes"]
    fk = notes["foreign_keys"][0]
    assert fk["table"] == "users"
    assert fk["columns"] == ["user_id"]
    assert fk["referenced_columns"] == ["id"]
    assert fk["on_delete"] == "CASCADE"


def test_schema_show_versions_lists_files(tmp_path):
    _write_schema(tmp_path, 1, SCHEMA_DOC)
    _write_schema(tmp_path, 2, SCHEMA_DOC)
    path = _write_schema(tmp_path, 3, SCHEMA_DOC)
    ok, result = SchemaInspector(str(path)).execute(show_versions=True)
    assert ok is True
    versions = {v["version"]: v for v in result["versions"]}
    assert set(versions) == {"1", "2", "3"}
    assert versions["3"]["is_current"] is True
    assert versions["1"]["is_current"] is False


def test_schema_dir_resolves_to_highest_version(tmp_path):
    _write_schema(tmp_path, 1, {"database": {"version": 1, "entities": []}})
    _write_schema(tmp_path, 10, SCHEMA_DOC)
    db_dir = tmp_path / "schemas" / "com.example.app.data.AppDatabase"
    ok, result = SchemaInspector(str(db_dir)).execute()
    assert ok is True
    # Highest numeric version wins (10 > 1, not lexical where "1" precedes).
    assert result["version"] == 3


def test_schema_raw_by_table_name(tmp_path):
    path = _write_schema(tmp_path, 3, SCHEMA_DOC)
    ok, raw = SchemaInspector(str(path)).get_raw_entity("users")
    assert ok is True
    parsed = json.loads(raw)
    assert parsed["tableName"] == "users"


def test_schema_raw_by_class_name(tmp_path):
    path = _write_schema(tmp_path, 3, SCHEMA_DOC)
    ok, raw = SchemaInspector(str(path)).get_raw_entity("Note")
    assert ok is True
    assert json.loads(raw)["tableName"] == "notes"


def test_schema_raw_missing_entity(tmp_path):
    path = _write_schema(tmp_path, 3, SCHEMA_DOC)
    ok, msg = SchemaInspector(str(path)).get_raw_entity("ghosts")
    assert ok is False
    assert "not found" in msg


def test_schema_missing_path_fails(tmp_path):
    ok, result = SchemaInspector(str(tmp_path / "nope.json")).execute()
    assert ok is False
    assert "not found" in result["error"]


def test_schema_invalid_json_fails(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    ok, result = SchemaInspector(str(path)).execute()
    assert ok is False
    assert "Invalid schema JSON" in result["error"]


# === LIVE MODE (sqlite parsing + run-as command construction) ==============


def test_parse_sqlite_schema_skips_indexes(recorded):
    """A CREATE INDEX is not a table, including the UNIQUE form."""
    schema = recorded.text("sqlite_schema_host")
    assert "CREATE INDEX index_order_items_order_id" in schema
    assert "CREATE UNIQUE INDEX index_orders_reference" in schema

    names = {t["name"] for t in parse_sqlite_schema(schema)}
    assert "index_order_items_order_id" not in names
    assert "index_orders_reference" not in names


def _completed(cmd, returncode=0, stdout="", stderr=""):
    """An adb result as run_argv hands it back: bytes (no call asks for text any more)."""
    return subprocess.CompletedProcess(
        cmd, returncode, stdout=stdout.encode("utf-8"), stderr=stderr.encode("utf-8")
    )


def test_live_dump_schema_via_device_sqlite3(monkeypatch, recorded):
    captured: list[list[str]] = []
    schema = recorded.text("sqlite_schema_host")

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout=schema)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, result = LiveInspector(serial="emulator-5554").execute("com.example.app", db_name="app.db")
    assert ok is True
    assert result["method"] == "device-sqlite3"
    # Four, not two: android_metadata and sqlite_sequence are in every Android
    # database, and only a hand-written schema leaves them out.
    assert result["total_tables"] == 4

    cmd = captured[0]
    # Right device, run-as for the right package, sqlite3 .schema on the DB path.
    assert cmd[:3] == ["adb", "-s", "emulator-5554"]
    assert "run-as" in cmd
    assert "com.example.app" in cmd
    assert "sqlite3" in cmd and ".schema" in cmd
    assert "/data/data/com.example.app/databases/app.db" in cmd


def test_live_list_databases_skips_sidecars(monkeypatch, recorded):
    """The fixture app's `ls -la databases/`, recorded: the journal is not a database.

    This listing was hand-written as `app.db`, `-wal`, `-shm` and `other.db`;
    the device's is fixture.db beside a zero-byte fixture.db-journal, with
    `total`, `.` and `..` (run_as_ls_databases). It is read as container.py
    reads a listing (common.run_as.parse_ls_output), where it was `ls -1`.
    """
    listing = recorded.text("run_as_ls_databases")
    assert " fixture.db\n" in listing and " fixture.db-journal\n" in listing, "it changed"
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _completed(cmd, stdout=listing)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, result = LiveInspector().execute("com.example.composefixture")
    assert ok is True
    assert result["databases"] == ["fixture.db"]
    assert result["total_databases"] == 1
    assert (result["rejected"], result["rejected_entries"]) == (0, [])
    # The recording is of this listing: `ls -la` of the databases directory.
    assert captured[0][-3:] == ["ls", "-la", "/data/data/com.example.composefixture/databases"]


def test_live_falls_back_to_host_sqlite3(monkeypatch, tmp_path):
    # Build a real little SQLite DB on disk so the host-side path can read it.
    import sqlite3

    db_file = tmp_path / "real.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
    conn.commit()
    conn.close()
    db_bytes = db_file.read_bytes()

    def fake_run(cmd, *args, **kwargs):
        # First strategy (run-as sqlite3 on the device) fails -> binary absent.
        if "sqlite3" in cmd:
            return _completed(cmd, returncode=1, stderr="sqlite3: not found")
        # The `shell -T` cat returns raw DB bytes.
        return subprocess.CompletedProcess(cmd, 0, stdout=db_bytes, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, result = LiveInspector().execute("com.example.app", db_name="real.db")
    assert ok is True
    assert result["method"] == "host-sqlite3"
    names = [t["name"] for t in result["tables"]]
    assert "widgets" in names


def test_live_run_as_denied_release_build(monkeypatch, recorded):
    """`run-as: package not debuggable: <pkg>` is a string Android never prints.

    The real refusal for a non-debuggable package is
    `run-as: package not an application: <pkg>` (recorded). Asserting against
    the invented one meant this test could not tell whether the detection
    worked on a real denial -- and in container.py, the matching marker was
    invented in the same direction, so it did not.
    """
    denial = recorded.text("run_as_not_an_application")

    def fake_run(cmd, *args, **kwargs):
        # Every call -- the sqlite3 attempt and the `run-as <pkg> id` probe --
        # as bytes, refused as run-as refuses the package the recording names.
        assert not kwargs.get("text"), "a run-as call asked for text"
        return _completed(cmd, returncode=1, stderr=denial)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, result = LiveInspector().execute("com.android.settings", db_name="app.db")
    assert ok is False
    assert result["run_as_denied"] is True
    assert "debuggable" in result["hint"].lower()


def test_live_run_as_denied_listing(monkeypatch, recorded):
    def fake_run(cmd, *args, **kwargs):
        return _completed(cmd, returncode=1, stderr=recorded.text("run_as_unknown_package_stderr"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, result = LiveInspector().execute("com.example.app1")
    assert ok is False
    assert result.get("run_as_denied") is True


# ---------------------------------------------------------------------------
# A real Android database, via the path that actually runs.
# ---------------------------------------------------------------------------


def test_sqlite_schema_parses_a_real_android_database(recorded):
    """Schema from a real app database, recorded via host sqlite3.

    Host, not device: `sqlite3` is absent from Android user builds (verified
    missing on API 35), so `LiveInspector`'s working path is pull-then-host,
    and a device-sqlite3 capture would have recorded a failure.

    What the recording contains that a hand-written schema omits: the
    `android_metadata` and `sqlite_sequence` tables SQLite and Android create
    themselves, and `CREATE TABLE sqlite_sequence(name,seq)` with no space
    before the parenthesis and no newlines inside it.
    """
    tables = parse_sqlite_schema(recorded.text("sqlite_schema_host"))
    by_name = {table["name"]: table for table in tables}

    assert {"orders", "order_items"} <= set(by_name), f"missed real tables: {list(by_name)}"

    assert [c["name"] for c in by_name["orders"]["columns"]] == [
        "id",
        "reference",
        "total_cents",
        "placed_at",
    ]

    # The platform's own tables are present in every Android database; a
    # consumer that cannot see them will be surprised by them.
    assert "android_metadata" in by_name
    assert [c["name"] for c in by_name["sqlite_sequence"]["columns"]] == ["name", "seq"]


def test_a_foreign_key_clause_is_not_mistaken_for_a_column(recorded):
    """`FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE`.

    It sits in the column list and is not a column. A parser splitting on
    commas invents one called "FOREIGN KEY(order_id) REFERENCES orders(id)".
    """
    tables = {t["name"]: t for t in parse_sqlite_schema(recorded.text("sqlite_schema_host"))}
    columns = [c["name"] for c in tables["order_items"]["columns"]]

    assert columns == ["id", "order_id", "label", "quantity"], columns
    assert not any("FOREIGN" in name.upper() for name in columns)


# ---------------------------------------------------------------------------
# LIVE's database read: `adb shell -T`, never `adb exec-out`.
#
# exec-out exits 0 whatever the device command did and puts the device's
# stderr on STDOUT, so a database that is not there came back as the bytes
# "cat: ...: No such file or directory", was written to a temp file, and was
# reported as "not a valid SQLite database" -- the reason lost, the read
# reported as a corrupt file. container.py was fixed for the same lie in #22
# (recorded: run_as_cat_missing_exec_out vs run_as_cat_missing). Both
# transports are answered here, each with its own recording, so the test says
# which one the script used by what it reports.
# ---------------------------------------------------------------------------


def _device_without_the_database(
    tmp_path, recorded, cat_stderr: str | None = None, *more: Rule
) -> FakeToolchain:
    """adb answering with recorded output only; a missing DB over either transport.

    The on-device `sqlite3 .schema` attempt is answered with silence and exit
    127 (no rule): sqlite3 is absent from user builds (see
    test_sqlite_schema_parses_a_real_android_database), and nothing here
    claims to know what the device prints when it is. ``cat_stderr`` replaces
    what the `shell -T` read answers on stderr (default: the recording);
    ``more`` answers anything else the spec needs.
    """
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    if cat_stderr is None:
        cat_stderr = recorded.text("run_as_cat_missing")
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("version",), stdout=recorded.text("adb_version"), exit=status["adb_version"]),
            Rule(
                ("exec-out", "cat"),
                stdout=recorded.text("run_as_cat_missing_exec_out"),
                exit=status["run_as_cat_missing_exec_out"],
            ),
            Rule(("-T", "cat"), stderr=cat_stderr, exit=status["run_as_cat_missing"]),
            *more,
        ],
        default_exit=127,
    )
    return chain


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_database_that_is_not_there_is_refused_with_the_devices_reason(
    tmp_path, recorded, json_mode
):
    chain = _device_without_the_database(tmp_path, recorded)
    argv = ["--package", "com.example.composefixture", "--db", "absent.db"]
    result = chain.run(
        "model_inspector.py", *argv, "--serial", "emulator-5554", *(["--json"] if json_mode else [])
    )

    assert_refusal(result, json_mode=json_mode, naming=("no such file or directory",), status=1)
    assert "not a valid sqlite" not in (result.stdout + result.stderr).lower()
    reads = [call for call in chain.calls("adb") if "cat" in call]
    assert reads and all("exec-out" not in call for call in reads), reads


@pytest.mark.parametrize("name", COLLIDING_FILE_NAMES)
def test_a_missing_database_named_like_a_device_state_is_still_a_missing_file(
    tmp_path, recorded, name
):
    """Codex review of #29: `--db unauthorized.db` exited 3, "unauthorized device".

    The device's `cat: ...: No such file or directory` came back through adb's
    stderr, and the classifier found "unauthorized" in it. The read is the
    app's file, missing: exit 1, with the device's reason.
    """
    databases = "/data/data/com.example.composefixture/databases"
    chain = _device_without_the_database(
        tmp_path, recorded, recorded_missing_file(recorded, databases, name)
    )
    argv = ["--package", "com.example.composefixture", "--db", name, "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, "--json")

    payload = assert_refusal(
        result, json_mode=True, naming=("no such file or directory",), status=1
    )
    assert name in payload["error"], payload


def test_a_device_that_is_gone_during_the_read_is_still_exit_3(tmp_path, recorded):
    """The control: adb's own `adb: device '...' not found` on that read is the device's."""
    chain = _device_without_the_database(
        tmp_path, recorded, recorded.text("adb_shell_device_not_found")
    )
    argv = ["--package", "com.example.composefixture", "--db", "fixture.db"]

    result = chain.run("model_inspector.py", *argv, "--serial", "emulator-5554", "--json")

    assert_refusal(result, json_mode=True, naming=("not attached",), status=3)


def test_an_adb_client_too_old_for_t_is_refused_before_the_database_is_read(tmp_path, recorded):
    """The recorded banner with ONLY its X.Y.Z replaced (1.0.32): `-T` predates it.

    Before ANY read of the app's data -- the on-device `sqlite3 .schema` query
    included, which ran ahead of the check (Codex review of #29).
    """
    status = {f["name"]: f.get("exit_status") for f in recorded.manifest["fixtures"]}
    banner = recorded.text("adb_version")
    old = banner.replace(".".join(map(str, parse_adb_version(banner))), "1.0.32", 1)
    assert old != banner
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("version",), stdout=old, exit=status["adb_version"]),
        ],
        default_exit=127,
    )

    result = chain.run(
        "model_inspector.py",
        *("--package", "com.example.composefixture", "--db", "fixture.db"),
        *("--serial", "emulator-5554", "--json"),
    )

    payload = assert_refusal(result, json_mode=True, naming=("older than 1.0.36",), status=1)
    assert "platform-tools" in payload["remedy"]
    reads = [call for call in chain.calls("adb") if "run-as" in call]
    assert not reads, f"the app's data was read before the client was checked: {reads}"


# ---------------------------------------------------------------------------
# What a run-as denial is (review of #33), decided in common.run_as for this
# script and container.py alike. This matched the markers anywhere in stdout
# and stderr, so `ls: <dir>: Permission denied` -- the filesystem refusing the
# databases directory of an app run-as had entered -- came back as a run-as
# denial with "install a debug build". Only a line run-as printed counts: on
# stderr, starting `run-as: `, at a non-zero exit.
# ---------------------------------------------------------------------------

DATABASES_DIR = "/data/data/com.example.composefixture/databases"


def _device_answering_the_listing(
    tmp_path, recorded, recording: str, probe: str | None = None
) -> FakeToolchain:
    """adb whose `ls -la` answers as ``recording`` did: stdout empty, the reason on stderr.

    ``probe`` names the recording that answers `run-as <pkg> id`, the probe
    common.run_as asks before it calls a failure a run-as refusal.
    """
    status = {f["name"]: f for f in recorded.manifest["fixtures"]}
    entry = status[recording]
    assert (entry["stream"], entry["stdout_bytes"]) == ("stderr", 0), entry
    rules = [
        Rule(("devices",), stdout=recorded.text("adb_devices_single")),
        Rule(("ls", "-la"), stderr=recorded.text(recording), exit=entry["exit_status"]),
    ]
    if probe is not None:
        rules.append(
            Rule(("run-as", "id"), stderr=recorded.text(probe), exit=status[probe]["exit_status"])
        )
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install("adb", rules, default_exit=127)
    return chain


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_an_unreadable_databases_dir_is_a_filesystem_failure_not_a_run_as_denial(
    tmp_path, recorded, json_mode
):
    """run_as_ls_databases_unreadable is this script's own `ls -la`, recorded with `chmod u-r`."""
    chain = _device_answering_the_listing(tmp_path, recorded, "run_as_ls_databases_unreadable")
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, *(["--json"] if json_mode else []))

    remedy = f"adb shell run-as com.example.composefixture ls -ld {DATABASES_DIR}"
    payload = assert_refusal(result, json_mode=json_mode, naming=("permission denied",), status=1)
    output = result.stdout + result.stderr
    assert "Install a debug build" not in output and "run-as denied" not in output, output
    assert remedy in output, output
    if payload is not None:
        assert "run_as_denied" not in payload and "hint" not in payload, payload
        assert remedy in payload["remedy"]
    (listing,) = [call for call in chain.calls("adb") if "ls" in call]
    assert listing[-3:] == ["ls", "-la", DATABASES_DIR], "the recording is not of this command"
    assert not [call for call in chain.calls("adb") if call[-1] == "id"], "probed for ls's refusal"


@pytest.mark.parametrize(
    ("recording", "package", "probe"),
    [
        (
            "run_as_not_an_application_stderr",
            "com.android.settings",
            "run_as_id_not_an_application",
        ),
        ("run_as_unknown_package_stderr", "com.example.app1", "run_as_id_unknown_package"),
    ],
)
def test_a_refusal_run_as_printed_is_still_a_run_as_denial(
    tmp_path, recorded, recording, package, probe
):
    """The two recorded refusals keep the debug-build remedy, the probe agreeing.

    Recorded for `run-as <pkg> ls`; run-as refuses before it runs any command,
    so the same refusal answers this script's `ls -la`, and the probe's
    `run-as <pkg> id` is answered with its own recording.
    """
    chain = _device_answering_the_listing(tmp_path, recorded, recording, probe)

    result = chain.run(
        "model_inspector.py", "--package", package, "--serial", "emulator-5554", "--json"
    )

    payload = assert_refusal(result, json_mode=True, naming=("run-as denied",), status=1)
    assert payload["run_as_denied"] is True, payload
    assert "Install a debug build" in payload["remedy"]
    assert recorded.text(recording).strip() in payload["error"]
    probes = [call for call in chain.calls("adb") if call[-1] == "id"]
    assert probes == [["-s", "emulator-5554", "shell", "-T", "run-as", package, "id"]], probes


@pytest.mark.parametrize(("separator", "probes"), [("\n", 1), ("\r", 0)], ids=["lf", "cr"])
def test_a_database_name_cannot_forge_an_offline_verdict(tmp_path, recorded, separator, probes):
    """A6 for this script: only the file name in ``run_as_cat_missing`` is substituted.

    The read's stderr is the device command's, so an `adb: ` line after a
    newline in the name is put to one `adb get-state` (which answers `device`,
    recorded) and is the missing file it is: exit 1. A CR is not a line break
    to adb's classifier, so it is not even asked. The run-as readers ask for
    that confirmation (``confirm_device_errors``); logcat's readers do not.
    """
    name = f"x{separator}adb: device offline"
    chain = _device_without_the_database(
        tmp_path,
        recorded,
        recorded_missing_file(recorded, DATABASES_DIR, name),
        Rule(("get-state",), stdout=recorded.text("adb_get_state")),
    )
    argv = ["--package", "com.example.composefixture", "--db", name, "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, "--json")

    assert_refusal(result, json_mode=True, naming=("no such file or directory",), status=1)
    assert sum(call[-1] == "get-state" for call in chain.calls("adb")) == probes


# ---------------------------------------------------------------------------
# The databases listing, read as container.py reads one (W4-D item 3).
#
# It was `ls -1` in text mode, split with `splitlines()` and each name
# `strip()`ped: the transport turned a CR in a name into a line break, so
# `x\ry.db` came back as two databases, `x` and `y.db`, at exit 0. It is now
# `ls -la` read as bytes and parsed by common.run_as.parse_ls_output -- the
# parser container.py lists with -- which splits on `\n` only and refuses, and
# reports, a name no plain file name carries or that is not UTF-8.
# ---------------------------------------------------------------------------


def _with_name(recorded, name: bytes) -> dict[tuple[str, ...], bytes]:
    """Both recorded databases listings, ``fixture.db`` renamed ``name`` and nothing else.

    `ls -1` (run_as_ls_1_databases) is what the script ran before; `ls -la`
    (run_as_ls_databases) what it runs now. Each answers its own command, so
    the spec reads the same on either.
    """
    one = recorded.text("run_as_ls_1_databases").encode()
    long = recorded.text("run_as_ls_databases").encode()
    assert one.startswith(b"fixture.db\n") and b" fixture.db\n" in long, "the recordings moved"
    return {
        ("ls", "-1"): name + one.removeprefix(b"fixture.db"),
        ("ls", "-la"): long.replace(b" fixture.db\n", b" " + name + b"\n", 1),
    }


def _device_listing(tmp_path, recorded, name: bytes) -> FakeToolchain:
    """adb answering both databases listings with ``fixture.db`` renamed ``name``."""
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    listings = _with_name(recorded, name)
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            *(Rule(tokens, stdout=listing) for tokens, listing in listings.items()),
        ],
        default_exit=127,
    )
    return chain


@pytest.mark.parametrize(
    ("name", "refused"),
    [(b"x\ry.db", repr("x\ry.db")), (b"x\xff.db", None)],
    ids=["cr", "not-utf8"],
)
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_database_name_that_is_not_a_plain_file_name_is_refused_not_split(
    tmp_path, recorded, name, refused, json_mode
):
    """The reviewer's reproduction: `x\\ry.db` was two databases, `x` and `y.db`, at exit 0."""
    chain = _device_listing(tmp_path, recorded, name)
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, *(["--json"] if json_mode else []))

    payload = assert_refusal(result, json_mode=json_mode, naming=("refused 1 entry",), status=1)
    if payload is not None:
        assert (payload["databases"], payload["total_databases"]) == ([], 0), payload
        assert payload["rejected"] == 1, payload
        if refused is not None:
            assert payload["rejected_entries"] == [refused], payload
    else:
        # What was read, then the refusal: never "No databases found".
        assert result.stdout == "databases (0, 1 refused):\n", result.stdout
    (listing,) = [call for call in chain.calls("adb") if "ls" in call]
    assert listing[-2:] == ["-la", DATABASES_DIR], listing


def test_a_newline_in_a_database_name_makes_the_listing_a_failure(tmp_path, recorded):
    """`x\\ny.db`: `ls -la` prints `x` as an entry and `y.db` as a line of its own.

    The second line is not ls's layout, so it is refused -- and a listing that
    refused a line is incomplete: exit 1, with what was read and the refused
    line named. (A name can still forge a WHOLE `ls -la` line after its
    newline; parse_ls_output's docstring says so, and container.py shares it.)
    """
    chain = _device_listing(tmp_path, recorded, b"x\ny.db")
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("refused 1 entry",), status=1)
    assert payload["rejected_entries"] == [repr("y.db")], payload
