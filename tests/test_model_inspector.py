"""Device-free tests for model_inspector.py.

Three modes, all adb/subprocess-free unless explicitly monkeypatched:

1. SOURCE: a Kotlin @Entity/@Database fixture -> parsed tables/columns/version.
2. SCHEMA: the schema JSON Room's compiler exported (recorded_gradle) ->
   entities/fields/views/version, plus --show-versions and --raw behavior.
3. LIVE: a "sqlite .schema" fixture -> parsed tables; run-as command
   construction asserted with subprocess.run monkeypatched. No real device.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
    format_schema,
    parse_sqlite_schema,
    strip_comments,
)

from common.device_utils import quote_for_device_shell
from common.run_as import listing_command, parse_adb_version


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


# Every schema below is one Room's compiler EXPORTED (recorded_gradle:
# room_schema_v1 / room_schema_v2, built from tests/fixtures/scaffold/room), in
# the directory the Room Gradle plugin laid out (room_schema_tree). Until the
# recording these tests read a document written by hand, which gave every
# entity an `entityName`. Room writes no such key, so `--raw <ClassName>` and
# the `entity_name` field were tested and had never worked on a real export.


@pytest.fixture
def room_schemas(tmp_path, recorded_gradle) -> Path:
    """Room's schemaDirectory, rebuilt from the recording: the tree, and each file's bytes."""
    listing = recorded_gradle("room_schema_tree").splitlines()
    for line in listing:
        if line.endswith(".json"):
            path = tmp_path / line
            path.parent.mkdir(parents=True, exist_ok=True)
            exported = recorded_gradle(f"room_schema_v{path.stem}.json")
            path.write_text(exported, encoding="utf-8", newline="")
    return tmp_path / listing[0]


def _version_file(root: Path, version: int) -> Path:
    (path,) = root.glob(f"*/{version}.json")
    return path


def _exported(recorded_gradle, version: int) -> dict:
    return json.loads(recorded_gradle(f"room_schema_v{version}.json"))["database"]


def _by_table(result: dict) -> dict[str, dict]:
    return {entity["table"]: entity for entity in result["entities"]}


def test_schema_parses_entities_fields_and_version(room_schemas):
    ok, result = SchemaInspector(str(_version_file(room_schemas, 1))).execute()
    assert ok is True
    assert result["version"] == 1

    by_table = _by_table(result)
    assert set(by_table) == {"users", "notes"}

    users = by_table["users"]
    assert users["primary_key"] == ["id"]
    fields = {f["column_name"]: f for f in users["fields"]}
    assert fields["full_name"]["field_path"] == "name"
    assert fields["full_name"]["affinity"] == "TEXT"
    assert fields["id"]["not_null"] is True
    # Room writes no `notNull` key at all for a nullable column ...
    assert fields["email"]["not_null"] is False
    assert users["indices"] == [{"name": "index_users_email", "unique": True, "columns": ["email"]}]
    # ... and no `foreignKeys` key for an entity that has none.
    assert users["foreign_keys"] == []

    assert by_table["notes"]["foreign_keys"] == [
        {
            "table": "users",
            "columns": ["user_id"],
            "referenced_columns": ["id"],
            "on_delete": "CASCADE",
            "on_update": "NO ACTION",
        }
    ]


def test_schema_reads_embedded_fields_and_composite_keys(room_schemas):
    ok, result = SchemaInspector(str(room_schemas)).execute()
    assert ok is True
    by_table = _by_table(result)

    paths = {f["column_name"]: f["field_path"] for f in by_table["users"]["fields"]}
    assert paths["addr_city"] == "address.city"
    assert paths["addr_postal_code"] == "address.postalCode"

    tags = by_table["note_tags"]
    assert tags["primary_key"] == ["note_id", "tag"]
    assert tags["indices"] == [
        {"name": "idx_note_tags_by_tag", "unique": False, "columns": ["tag", "note_id"]}
    ]
    (fk,) = tags["foreign_keys"]
    assert (fk["table"], fk["on_delete"], fk["on_update"]) == ("notes", "CASCADE", "RESTRICT")


def test_room_exports_no_class_name_for_an_entity(recorded_gradle):
    """The premise of every test below that names a table and never a class."""
    for version in (1, 2):
        for entity in _exported(recorded_gradle, version)["entities"]:
            assert not {"entityName", "entity"} & entity.keys(), entity["tableName"]
            # Not as a value either: the package appears nowhere in the block.
            assert "roomfixture" not in json.dumps(entity), entity["tableName"]


def test_schema_reports_no_class_name_room_never_exported(room_schemas):
    """`entity_name` was "" for every entity of a real export, printed as `users (?)`."""
    ok, result = SchemaInspector(str(room_schemas)).execute()
    assert ok is True

    for entity in result["entities"]:
        assert "entity_name" not in entity, entity["table"]
    text = format_schema(result, verbose=True)
    assert "?" not in text
    assert "users, 6 fields, PK: id" in text.splitlines()


def test_schema_reports_a_view(room_schemas):
    """A @DatabaseView is under `views`, not `entities`: it was not reported at all."""
    ok, result = SchemaInspector(str(room_schemas)).execute()
    assert ok is True

    (view,) = result["views"]
    assert view["name"] == "note_summaries"
    # Room exports the statement with a placeholder for the name; filled in here.
    assert view["create_sql"].startswith(
        "CREATE VIEW `note_summaries` AS SELECT notes.noteId AS noteId, "
    )
    assert "${" not in view["create_sql"]

    brief = format_schema(result, verbose=False).splitlines()
    assert brief[0] == "Room schema v2: 4 entities, 1 view [2.json]"
    assert "  note_summaries: view" in brief
    assert f"  {view['create_sql']}" in format_schema(result, verbose=True).splitlines()


def test_a_schema_without_views_reports_none(room_schemas):
    """Version 1 has no `views` key at all."""
    ok, result = SchemaInspector(str(_version_file(room_schemas, 1))).execute()
    assert ok is True

    assert result["views"] == []
    assert format_schema(result, verbose=False).splitlines()[0] == (
        "Room schema v1: 2 entities, 0 views [1.json]"
    )


def test_schema_marks_an_fts_table(room_schemas):
    """An @Fts4 entity is a virtual table; it was reported as an ordinary one."""
    ok, result = SchemaInspector(str(room_schemas)).execute()
    assert ok is True
    by_table = _by_table(result)

    search = by_table["note_search"]
    assert search["fts_version"] == "FTS4"
    # Its primary key is SQLite's rowid, which is not a field of the entity.
    assert search["primary_key"] == ["rowid"]
    assert [f["column_name"] for f in search["fields"]] == ["body"]
    for table in ("users", "notes", "note_tags"):
        assert by_table[table]["fts_version"] is None, table

    assert "  note_search: 1 fields (FTS4)" in format_schema(result, verbose=False).splitlines()
    assert "note_search, 1 fields, PK: rowid, FTS4" in (
        format_schema(result, verbose=True).splitlines()
    )


def test_schema_show_versions_lists_files(room_schemas):
    path = _version_file(room_schemas, 2)
    ok, result = SchemaInspector(str(path)).execute(show_versions=True)
    assert ok is True
    assert result["versions"] == [
        {"version": "1", "file": "1.json", "is_current": False},
        {"version": "2", "file": "2.json", "is_current": True},
    ]


def test_schema_directory_resolves_to_the_highest_version(room_schemas):
    """Given the schemaDirectory itself, one level above the database's own."""
    ok, result = SchemaInspector(str(room_schemas)).execute()
    assert ok is True
    assert result["version"] == 2
    assert result["schema_file"] == str(_version_file(room_schemas, 2).resolve())


def test_schema_dir_orders_versions_as_numbers(room_schemas):
    """10 follows 9. The two NAMES are this test's -- Room exported 1 and 2 -- the bytes are Room's."""
    db_dir = _version_file(room_schemas, 2).parent
    (db_dir / "1.json").rename(db_dir / "9.json")
    (db_dir / "2.json").rename(db_dir / "10.json")

    ok, result = SchemaInspector(str(db_dir)).execute(show_versions=True)

    assert ok is True
    assert Path(result["schema_file"]).name == "10.json"
    assert [v["version"] for v in result["versions"]] == ["9", "10"]


def test_schema_raw_by_table_name(room_schemas, recorded_gradle):
    ok, raw = SchemaInspector(str(room_schemas)).get_raw_entity("users")
    assert ok is True
    (exported,) = [
        e for e in _exported(recorded_gradle, 2)["entities"] if e["tableName"] == "users"
    ]
    assert json.loads(raw) == exported


def test_schema_raw_by_view_name(room_schemas, recorded_gradle):
    ok, raw = SchemaInspector(str(room_schemas)).get_raw_entity("note_summaries")
    assert ok is True
    assert json.loads(raw) == _exported(recorded_gradle, 2)["views"][0]


def test_schema_raw_refusal_names_what_the_schema_holds(room_schemas):
    """A class name is the likely wrong guess: Room exported tables and views, not classes."""
    ok, msg = SchemaInspector(str(room_schemas)).get_raw_entity("User")
    assert ok is False
    assert "'User' not found in 2.json" in msg
    for name in ("users", "notes", "note_tags", "note_search", "note_summaries"):
        assert name in msg, name


@pytest.mark.parametrize("version", [1, 2])
def test_every_name_the_schema_listing_prints_is_one_raw_accepts(
    tmp_path, room_schemas, recorded_gradle, version
):
    """The loop an agent runs: list the schema, then ask for what it listed."""
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    schema = str(_version_file(room_schemas, version))
    exported = _exported(recorded_gradle, version)

    listing = chain.run("model_inspector.py", "--schema", schema)
    assert listing.returncode == 0, listing.stderr
    names = [line.split(":")[0].strip() for line in listing.stdout.splitlines()[1:]]
    assert names == [
        *(entity["tableName"] for entity in exported["entities"]),
        *(view["viewName"] for view in exported.get("views", [])),
    ]
    assert len(names) >= 2, names

    for name in names:
        raw = chain.run("model_inspector.py", "--schema", schema, "--raw", name)
        assert raw.returncode == 0, f"--raw {name}: {raw.stderr}"
        block = json.loads(raw.stdout)
        assert name in (block.get("tableName"), block.get("viewName"))


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_raw_given_a_class_name_is_refused_with_the_names_to_pass(
    tmp_path, room_schemas, json_mode
):
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    argv = ["--schema", str(room_schemas), "--raw", "User", *(["--json"] if json_mode else [])]

    result = chain.run("model_inspector.py", *argv)

    refusal = assert_refusal(result, json_mode=json_mode, naming=("'User' not found",), status=1)
    remedy = refusal["remedy"] if json_mode else result.stderr
    assert "table or view name" in remedy
    assert "class name" not in remedy


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


@pytest.mark.parametrize(
    "content", ["[" * 200_000, "9" * 5000], ids=["nested-too-deep", "number-too-long"]
)
def test_json_the_parser_gives_up_on_is_invalid_json(tmp_path, content):
    """json raises more than JSONDecodeError: RecursionError, and int()'s ValueError."""
    path = tmp_path / "1.json"
    path.write_text(content, encoding="utf-8")
    inspector = SchemaInspector(str(path))

    ok, result = inspector.execute()
    assert ok is False
    assert "Invalid schema JSON" in result["error"]

    ok, message = inspector.get_raw_entity("users")
    assert ok is False
    assert "Invalid schema JSON" in message


_ABSENT = object()
_ENTITIES = ("database", "entities")
_VIEWS = ("database", "views")


def _recorded_document(recorded_gradle, version: int = 2) -> dict:
    return json.loads(recorded_gradle(f"room_schema_v{version}.json"))


def _schema_file(tmp_path, document: object) -> Path:
    path = tmp_path / "written" / "1.json"
    path.parent.mkdir()
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _damage(document: dict, path: tuple, value: object) -> None:
    """Replace, or with ``_ABSENT`` remove, what stands at ``path`` in an exported document."""
    holder = document
    for step in path[:-1]:
        holder = holder[step]
    assert path[-1] in holder if isinstance(holder, dict) else path[-1] < len(holder), path
    if value is _ABSENT:
        del holder[path[-1]]
    else:
        holder[path[-1]] = value


# (id, path into the recorded v2 document, what is put there, how the refusal names the place)
SHAPE_DAMAGE = [
    ("version-absent", ("database", "version"), _ABSENT, "database.version"),
    ("version-text", ("database", "version"), "2", "database.version"),
    ("version-flag", ("database", "version"), True, "database.version"),
    ("entities-absent", _ENTITIES, _ABSENT, "database.entities"),
    ("entities-object", _ENTITIES, {}, "database.entities"),
    ("entity-text", (*_ENTITIES, 0), "users", "database.entities[0]"),
    ("table-name-absent", (*_ENTITIES, 0, "tableName"), _ABSENT, "entities[0].tableName"),
    ("table-name-number", (*_ENTITIES, 0, "tableName"), 7, "entities[0].tableName"),
    ("table-name-not-utf8", (*_ENTITIES, 0, "tableName"), "\ud83d", "entities[0].tableName"),
    ("fields-absent", (*_ENTITIES, 0, "fields"), _ABSENT, "entities[0].fields"),
    ("fields-null", (*_ENTITIES, 0, "fields"), None, "entities[0].fields"),
    ("field-list", (*_ENTITIES, 0, "fields", 1), [], "entities[0].fields[1]"),
    (
        "field-path-absent",
        (*_ENTITIES, 0, "fields", 0, "fieldPath"),
        _ABSENT,
        "fields[0].fieldPath",
    ),
    ("column-name-number", (*_ENTITIES, 0, "fields", 0, "columnName"), 5, "fields[0].columnName"),
    ("affinity-null", (*_ENTITIES, 0, "fields", 0, "affinity"), None, "fields[0].affinity"),
    ("not-null-text", (*_ENTITIES, 0, "fields", 0, "notNull"), "yes", "fields[0].notNull"),
    ("primary-key-list", (*_ENTITIES, 0, "primaryKey"), ["id"], "entities[0].primaryKey"),
    (
        "primary-key-columns-text",
        (*_ENTITIES, 0, "primaryKey", "columnNames"),
        "id",
        "entities[0].primaryKey.columnNames",
    ),
    ("indices-object", (*_ENTITIES, 0, "indices"), {}, "entities[0].indices"),
    ("index-name-absent", (*_ENTITIES, 0, "indices", 0, "name"), _ABSENT, "indices[0].name"),
    ("index-unique-text", (*_ENTITIES, 0, "indices", 0, "unique"), "true", "indices[0].unique"),
    (
        "index-columns-numbers",
        (*_ENTITIES, 0, "indices", 0, "columnNames"),
        [1],
        "indices[0].columnNames[0]",
    ),
    ("foreign-keys-text", (*_ENTITIES, 1, "foreignKeys"), "users", "entities[1].foreignKeys"),
    (
        "foreign-key-table-null",
        (*_ENTITIES, 1, "foreignKeys", 0, "table"),
        None,
        "foreignKeys[0].table",
    ),
    (
        "foreign-key-columns-text",
        (*_ENTITIES, 1, "foreignKeys", 0, "columns"),
        "user_id",
        "foreignKeys[0].columns",
    ),
    (
        "foreign-key-referenced-object",
        (*_ENTITIES, 1, "foreignKeys", 0, "referencedColumns"),
        {},
        "foreignKeys[0].referencedColumns",
    ),
    (
        "foreign-key-on-delete-number",
        (*_ENTITIES, 1, "foreignKeys", 0, "onDelete"),
        0,
        "foreignKeys[0].onDelete",
    ),
    ("fts-version-number", (*_ENTITIES, 3, "ftsVersion"), 4, "entities[3].ftsVersion"),
    ("views-object", _VIEWS, {}, "database.views"),
    ("view-number", (*_VIEWS, 0), 3, "database.views[0]"),
    ("view-name-absent", (*_VIEWS, 0, "viewName"), _ABSENT, "views[0].viewName"),
    ("view-sql-null", (*_VIEWS, 0, "createSql"), None, "views[0].createSql"),
]


@pytest.mark.parametrize(
    ("path", "value", "where"),
    [damage[1:] for damage in SHAPE_DAMAGE],
    ids=[damage[0] for damage in SHAPE_DAMAGE],
)
def test_a_document_not_shaped_as_room_exports_is_refused_naming_the_place(
    tmp_path, recorded_gradle, path, value, where
):
    """What the parser reads is held to the type Room exported it with (recorded: room_schema_v2).

    These were read with `.get`: a missing `tableName` was a table called
    None at exit 0, and a value of another type an AttributeError.
    """
    document = _recorded_document(recorded_gradle)
    _damage(document, path, value)
    inspector = SchemaInspector(str(_schema_file(tmp_path, document)))

    ok, result = inspector.execute()
    assert ok is False, result
    assert result["error"].startswith("Not a Room exported schema (1.json): "), result["error"]
    assert where in result["error"]
    result["error"].encode("utf-8")

    # --raw reads the same document, so it is refused for the same reason,
    # wherever in the document the damage is.
    ok, message = inspector.get_raw_entity("users")
    assert ok is False, message
    assert message == result["error"]


@pytest.mark.parametrize("version", [1, 2])
def test_what_room_exported_fits_the_shape_whole(room_schemas, version):
    """The other direction: neither recording is refused, extra keys and absent ones included."""
    inspector = SchemaInspector(str(_version_file(room_schemas, version)))
    ok, result = inspector.execute()
    assert ok is True, result
    assert result["version"] == version
    for entity in result["entities"]:
        ok, raw = inspector.get_raw_entity(entity["table"])
        assert ok is True, raw


@pytest.mark.parametrize(
    "document",
    [{}, {"hello": "world"}, [], {"database": None}],
    ids=["empty-object", "other-json", "a-list", "database-null"],
)
@pytest.mark.parametrize("raw", [False, True], ids=["listing", "raw"])
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_json_that_is_not_a_room_schema_is_refused_not_listed(tmp_path, document, raw, json_mode):
    """`{}` listed as "Room schema v?: 0 entities" at exit 0; `[]` was a traceback."""
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    argv = ["--schema", str(_schema_file(tmp_path, document))]
    argv += ["--raw", "users"] if raw else []
    argv += ["--json"] if json_mode else []

    result = chain.run("model_inspector.py", *argv)

    assert "Traceback" not in result.stderr, result.stderr
    refusal = assert_refusal(
        result, json_mode=json_mode, naming=("Not a Room exported schema (1.json)",), status=1
    )
    # Where to point --schema, with or without --raw; not advice about --raw's argument.
    remedy = refusal["remedy"] if json_mode else result.stderr
    assert "exported-schema JSON file" in remedy
    assert "Pass --raw" not in remedy


def test_a_view_query_that_holds_the_placeholder_itself_keeps_it(tmp_path, recorded_gradle):
    """Only the name is filled in. Every `${VIEW_NAME}` was, the query's own text included."""
    document = _recorded_document(recorded_gradle)
    (view,) = document["database"]["views"]
    head = "CREATE VIEW `${VIEW_NAME}` AS "
    assert view["createSql"].startswith(head)  # the recording's premise
    query = view["createSql"][len(head) :] + " WHERE notes.body != '${VIEW_NAME}'"
    view["createSql"] = head + query

    ok, result = SchemaInspector(str(_schema_file(tmp_path, document))).execute()
    assert ok is True, result

    assert result["views"][0]["create_sql"] == f"CREATE VIEW `{view['viewName']}` AS {query}"


def test_a_view_statement_that_does_not_open_as_room_writes_it_is_reported_as_written(
    tmp_path, recorded_gradle
):
    document = _recorded_document(recorded_gradle)
    (view,) = document["database"]["views"]
    view["createSql"] = "SELECT '${VIEW_NAME}' AS marker"

    ok, result = SchemaInspector(str(_schema_file(tmp_path, document))).execute()
    assert ok is True, result

    assert result["views"][0]["create_sql"] == "SELECT '${VIEW_NAME}' AS marker"


def _schema_text_file(tmp_path, text: str) -> Path:
    path = tmp_path / "written" / "1.json"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    return path


def _with_an_unread_key(recorded_gradle, value_text: str) -> str:
    """The recorded export with one key more on its first entity, holding JSON text as given.

    A key the shape does not name: the listing never reads it, --raw prints it.
    """
    text = json.dumps(_recorded_document(recorded_gradle))
    head = '"entities": [{'
    assert text.count(head) == 1
    return text.replace(head, head + f'"unread": {value_text}, ')


def _lists_deep(count: int) -> str:
    return "[" * count + "0" + "]" * count


# The unread value stands four levels down: document, database, entities, entity.
_UNREAD_LEVEL = 4


@pytest.mark.parametrize(
    "value_text",
    [_lists_deep(991), "NaN", "Infinity", "-Infinity", "1e999", '0, "unread": 1'],
    ids=["991-lists-deep", "nan", "infinity", "minus-infinity", "float-overflow", "key-twice"],
)
@pytest.mark.parametrize("raw", [False, True], ids=["listing", "raw"])
@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_json_that_could_not_be_printed_back_is_refused_where_it_is_read(
    tmp_path, recorded_gradle, value_text, raw, json_mode
):
    """What --raw prints is the whole entity, the keys the listing never reads included.

    991 lists deep in such a key listed at exit 0 and ended --raw in the JSON
    writer's RecursionError; NaN and Infinity were printed back as tokens that
    are not JSON; of a key named twice one value was silently dropped
    (verification of #69). Each is refused by the one reader, so the listing
    and --raw agree.
    """
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    path = _schema_text_file(tmp_path, _with_an_unread_key(recorded_gradle, value_text))
    argv = ["--schema", str(path)]
    argv += ["--raw", "users"] if raw else []
    argv += ["--json"] if json_mode else []

    result = chain.run("model_inspector.py", *argv)

    assert "Traceback" not in result.stderr, result.stderr[-400:]
    assert_refusal(result, json_mode=json_mode, naming=("Invalid schema JSON",), status=1)


def test_nesting_past_the_depth_named_is_refused_and_nesting_at_it_is_printed_back(
    tmp_path, recorded_gradle
):
    """The limit is one number, held on both sides, and it is the last depth that is read.

    "nested more than 64 deep" has to mean that: 64 lists and objects inside
    one another are printed back by --raw, and one more is refused.
    """
    limit = model_inspector.MAX_JSON_DEPTH
    assert limit < 100  # far under where the JSON writer's recursion ends

    held = _lists_deep(limit - _UNREAD_LEVEL)
    path = _schema_text_file(tmp_path, _with_an_unread_key(recorded_gradle, held))
    inspector = SchemaInspector(str(path))
    ok, result = inspector.execute()
    assert ok is True, result
    ok, raw = inspector.get_raw_entity(result["entities"][0]["table"])
    assert ok is True, raw
    assert json.loads(raw)["unread"] == json.loads(held)

    past = _lists_deep(limit - _UNREAD_LEVEL + 1)
    (path.parent / "2.json").write_text(
        _with_an_unread_key(recorded_gradle, past), encoding="utf-8"
    )
    inspector = SchemaInspector(str(path.parent / "2.json"))
    ok, result = inspector.execute()
    assert ok is False
    assert f"Invalid schema JSON: nested more than {limit} deep" in result["error"]
    ok, message = inspector.get_raw_entity("users")
    assert ok is False
    assert f"nested more than {limit} deep" in message


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_table_named_with_no_characters_is_still_a_name_raw_accepts(
    tmp_path, recorded_gradle, json_mode
):
    """`--raw ""` printed the whole listing at exit 0: the argument was tested for truth."""
    document = _recorded_document(recorded_gradle)
    document["database"]["entities"][0]["tableName"] = ""
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    argv = ["--schema", str(_schema_file(tmp_path, document)), "--raw", ""]

    result = chain.run("model_inspector.py", *argv, *(["--json"] if json_mode else []))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == document["database"]["entities"][0]


def test_a_view_name_is_written_between_backticks_as_room_writes_it(tmp_path, recorded_gradle):
    """Not escaped. Room's DatabaseView.createViewQuery is "CREATE VIEW `$viewName` AS ...",

    the name as the annotation spelled it, and the statement reported here is
    that one. Doubling a backtick would report a statement Room does not run.
    """
    document = _recorded_document(recorded_gradle)
    (view,) = document["database"]["views"]
    head = "CREATE VIEW `${VIEW_NAME}` AS "
    query = view["createSql"][len(head) :]
    view["viewName"] = "a``b"

    ok, result = SchemaInspector(str(_schema_file(tmp_path, document))).execute()
    assert ok is True, result

    assert result["views"][0]["create_sql"] == f"CREATE VIEW `a``b` AS {query}"


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
    """The fixture app's databases/ listing, recorded: the journal is not a database.

    This listing was hand-written as `app.db`, `-wal`, `-shm` and `other.db`;
    the device's is fixture.db beside a zero-byte fixture.db-journal, after the
    directory's own record (run_as_find_databases). It is read as container.py
    reads a listing (common.run_as.parse_listing).
    """
    listing = recorded.data("run_as_find_databases.txt")
    assert b"\0fixture.db\0" in listing and b"\0fixture.db-journal\0" in listing, "it changed"
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, result = LiveInspector().execute("com.example.composefixture")
    assert ok is True
    assert result["databases"] == ["fixture.db"]
    assert result["total_databases"] == 1
    assert (result["rejected"], result["rejected_entries"]) == (0, [])
    # The recording is of this listing: the listing command, aimed at databases/.
    assert _listed(captured[0]) == DATABASES_DIR


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
    """adb whose listing answers as ``recording`` did, each stream where the manifest says.

    A run-as refusal is all on stderr; find failing on an unreadable directory
    printed its own record on stdout first, its stderr recorded in the twin
    ``<recording>_stderr`` (same run). ``probe`` names the recording that
    answers `run-as <pkg> id`, the probe common.run_as asks before it calls a
    failure a run-as refusal.
    """
    status = {f["name"]: f for f in recorded.manifest["fixtures"]}
    entry = status[recording]
    if entry["stream"] == "stderr":
        assert entry["stdout_bytes"] == 0, entry
        stdout, stderr = "", recorded.text(recording)
    else:
        stdout, stderr = recorded.text(recording), recorded.text(f"{recording}_stderr")
    rules = [
        Rule(("devices",), stdout=recorded.text("adb_devices_single")),
        Rule(("find", "-printf"), stdout=stdout, stderr=stderr, exit=entry["exit_status"]),
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
    """run_as_find_databases_unreadable is this script's own listing, recorded with `chmod u-r`."""
    chain = _device_answering_the_listing(tmp_path, recorded, "run_as_find_databases_unreadable")
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
    (listing,) = [call for call in chain.calls("adb") if "find" in call]
    assert _listed(listing) == DATABASES_DIR, "the recording is not of this command"
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
    so the same refusal answers this script's listing, and the probe's
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
# The databases listing, read as container.py reads one.
#
# It was `ls -1` in text mode, split with `splitlines()` and each name
# `strip()`ped: the transport turned a CR in a name into a line break, so
# `x\ry.db` came back as two databases, `x` and `y.db`, at exit 0 (W4-D item
# 3). Then `ls -la` split on `\n`: `fixture.db` + LF + a whole `ls -la` line
# for ghost.db listed ghost.db, at exit 0 with nothing refused (final review
# of v0.8.0, REQUIRED 4). It is now common.run_as's find, NUL-terminated
# fields read as bytes by parse_listing -- the parser container.py lists with
# -- which refuses, and reports, a name no plain file name carries or that is
# not UTF-8, whole.
# ---------------------------------------------------------------------------


def _listed(argv: list[str]) -> str | None:
    """The directory an adb argv lists, when it ends in the WHOLE listing command; else None."""
    if "find" not in argv or argv.index("find") + 2 >= len(argv):
        return None
    at = argv.index("find")
    directory = argv[at + 2]
    command = [quote_for_device_shell(arg) for arg in listing_command(directory)]
    return directory if argv[at:] == command else None


def _with_name(recorded, name: bytes) -> dict[tuple[str, ...], bytes]:
    """Each recorded databases listing, ``fixture.db`` renamed ``name`` and nothing else.

    `ls -1` (run_as_ls_1_databases) and `ls -la` (run_as_ls_databases) are
    what the script ran before; find (run_as_find_databases) what it runs
    now, where the name is the last of six NUL-terminated fields. Each answers
    its own command, so the spec reads the same on any of them.
    """
    one = recorded.text("run_as_ls_1_databases").encode()
    long = recorded.text("run_as_ls_databases").encode()
    find = recorded.data("run_as_find_databases.txt")
    assert one.startswith(b"fixture.db\n") and b" fixture.db\n" in long, "the recordings moved"
    assert find.count(b"\0fixture.db\0") == 1, "the recording moved"
    return {
        ("ls", "-1"): name + one.removeprefix(b"fixture.db"),
        ("ls", "-la"): long.replace(b" fixture.db\n", b" " + name + b"\n", 1),
        ("find", "-printf"): find.replace(b"\0fixture.db\0", b"\0" + name + b"\0", 1),
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
    (listing,) = [call for call in chain.calls("adb") if "find" in call]
    assert _listed(listing) == DATABASES_DIR, listing


def test_a_newline_in_a_database_name_is_one_refused_name(tmp_path, recorded):
    """`x\\ny.db`: `ls -la` printed `x` as an entry and `y.db` as a line of its own.

    Now it is one name, refused as itself: exit 1, with the journal's sidecar
    status intact and nothing listed as a database.
    """
    chain = _device_listing(tmp_path, recorded, b"x\ny.db")
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, "--json")

    payload = assert_refusal(result, json_mode=True, naming=("refused 1 entry",), status=1)
    assert payload["rejected_entries"] == [repr("x\ny.db")], payload
    assert payload["databases"] == [], payload


# The final review's reproduction (REQUIRED 4): the recorded databases listing
# with ONLY fixture.db's name replaced by `fixture.db` + LF + the listing's own
# `ls -la` line for fixture.db renamed ghost.db -- one legal Unix file name.
# Through `ls -la` it listed ["fixture.db", "ghost.db"], total 2, rejected 0, at
# exit 0.


def _reviewers_forged_name(recorded) -> bytes:
    """`fixture.db` + LF + run_as_ls_databases's own fixture.db line, its name made ghost.db."""
    long = recorded.text("run_as_ls_databases")
    (line,) = [line for line in long.split("\n") if line.endswith(" fixture.db")]
    return ("fixture.db\n" + line.removesuffix("fixture.db") + "ghost.db").encode()


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "text"])
def test_a_name_forging_a_whole_ls_line_lists_no_ghost_database(tmp_path, recorded, json_mode):
    """REQUIRED 4: the forged line is inside one name, refused whole; ghost.db is not a database."""
    name = _reviewers_forged_name(recorded)
    chain = _device_listing(tmp_path, recorded, name)
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554"]

    result = chain.run("model_inspector.py", *argv, *(["--json"] if json_mode else ["--verbose"]))

    payload = assert_refusal(result, json_mode=json_mode, naming=("refused 1 entry",), status=1)
    if payload is not None:
        assert (payload["databases"], payload["total_databases"]) == ([], 0), payload
        assert payload["rejected_entries"] == [repr(name.decode())], payload
    else:
        assert result.stdout.startswith("databases (0, 1 refused):\n"), result.stdout
        assert f"  refused: {name.decode()!r}" in result.stdout, result.stdout
    assert "ghost.db\n" not in result.stdout


def test_the_recorded_forging_name_lists_no_ghost_database(tmp_path, recorded):
    """The same forgery recorded on the device: a names directory made in the fixture app's files/.

    Its find listing (run_as_find_names) and `ls -la` (run_as_ls_names) answer
    this script's listing of databases/ -- a substitution of which recording
    answers which directory; find's names do not carry the directory. Through
    `ls -la`, x.db and ghost.db were both databases.
    """
    chain = FakeToolchain(tmp_path / "toolchain")
    chain.assert_isolated()
    chain.install(
        "adb",
        [
            Rule(("devices",), stdout=recorded.text("adb_devices_single")),
            Rule(("find", "-printf"), stdout=recorded.text("run_as_find_names")),
            Rule(("ls", "-la"), stdout=recorded.text("run_as_ls_names")),
        ],
        default_exit=127,
    )
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554", "--json"]

    result = chain.run("model_inspector.py", *argv)

    payload = assert_refusal(result, json_mode=True, naming=("refused 3 entries",), status=1)
    assert payload["databases"] == ["e f.db"], payload
    forged = "x.db\n-rw-rw---- 1 u0_a205 u0_a205 32768 2026-10-18 22:28 ghost.db"
    assert repr(forged) in payload["rejected_entries"], payload["rejected_entries"]


@pytest.mark.parametrize(
    ("recording", "remedy"),
    [
        ("run_as_find_databases_unreadable", "ls -ld"),
        ("run_as_find_databases_unsearchable", "failed at the entry '.'"),
        ("run_as_find_unknown_primary", "toybox find"),
        ("run_as_find_unknown_escape", "toybox find"),
    ],
)
def test_a_failed_listing_names_the_path_or_the_command_by_finds_first_line(
    tmp_path, recorded, recording, remedy
):
    """`find: <dir>: ...` is the directory's failure; `find: bad ...` names no path, and is find's."""
    chain = _device_answering_the_listing(tmp_path, recorded, recording)
    argv = ["--package", "com.example.composefixture", "--serial", "emulator-5554", "--json"]

    result = chain.run("model_inspector.py", *argv)

    payload = assert_refusal(result, json_mode=True, naming=("find: ",), status=1)
    assert remedy in payload["remedy"], payload["remedy"]
    assert payload["path"] == DATABASES_DIR
