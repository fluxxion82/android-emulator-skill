#!/usr/bin/env python3
"""
Android persistence / Room model inspector.

The Android counterpart of the iOS Core Data / SwiftData inspector — but Android
persistence is a different world, so this tool is rescoped to Android-native
mechanisms rather than mirroring Core Data semantics.

Room is backed by SQLite. This inspector covers Room (annotations + exported
schema) and raw SQLite. Other Android persistence (SQLDelight, Realm, ObjectBox,
Jetpack DataStore) is not specifically parsed.

Three modes (exactly one per run):

1. SOURCE (--source DIR): best-effort regex parse of Kotlin/Java source for Room
   annotations — @Entity (optional tableName), @Database(entities=[...],
   version=N), @Dao, and per-field @ColumnInfo / @PrimaryKey / @ForeignKey /
   @Embedded / @Ignore. Reports each entity's table name + columns and the
   @Database version + entity list. Like the iOS SwiftData @Model scan this is
   best-effort: comments are stripped, results deduped, formatting tolerated.

2. SCHEMA (--schema PATH): parse Room's EXPORTED schema JSON. When
   ``room.schemaLocation`` is set, Room writes ``schemas/<db-class>/<version>.json``.
   Parses ``database.entities[]`` -> tableName, fields (fieldPath, columnName,
   affinity, notNull), primaryKey, indices, foreignKeys. ``--show-versions``
   lists all exported version files; ``--raw NAME`` dumps one entity's raw JSON.

3. LIVE (--package PKG [--db NAME]): dump the SQLite schema of a DEBUGGABLE app's
   database via ``adb shell run-as PKG``. Tries on-device ``sqlite3 <db> .schema``
   first, else pulls the DB bytes via ``adb exec-out run-as PKG cat`` into a temp
   file and reads the schema with the host's sqlite3 module. Fails clearly when
   run-as is denied (release / non-debuggable app, or uninstalled package).
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from common.device_utils import build_adb_command, quote_for_device_shell, resolve_device_identifier
from common.env_config import env_int

# Cap on bytes pulled from a device DB before we give up reading its schema
# locally. Guards against dumping a multi-megabyte database inline. Override via
# the ANDROID_EMU_ prefix.
DB_MAX_BYTES = env_int("ANDROID_EMU_MODEL_DB_MAX_BYTES", 50_000_000, min_value=4096)

# Timeout (seconds) for a single run-as adb invocation in LIVE mode.
RUN_AS_TIMEOUT = env_int("ANDROID_EMU_MODEL_TIMEOUT", 30, min_value=5)

# Markers adb/run-as emit when run-as is refused. Matched case-insensitively as
# substrings; the platform wording has drifted across API levels.
_RUN_AS_DENIED_MARKERS = (
    "is not debuggable",
    "not debuggable",
    "is unknown",
    "unknown package",
    "run-as: package not found",
    "package not found",
    # Measured: the device prints "run-as: package not an application: <pkg>".
    # This read "is not an application", with an "is" Android never emits, so a
    # real denial fell through and the failure was reported as
    # "app.db is not a valid SQLite database" -- blaming the database for a
    # permissions problem. Recorded as run_as_not_an_application.
    "not an application",
    "user is not debuggable",
    "permission denied",
)

SCOPING_NOTE = (
    "Room is backed by SQLite. This inspector covers Room (annotations + "
    "exported schema) and raw SQLite. Other Android persistence (SQLDelight, "
    "Realm, ObjectBox, Jetpack DataStore) is not specifically parsed."
)


class RunAsDeniedError(RuntimeError):
    """Raised when ``run-as`` is refused for the target package.

    Expected for release / non-debuggable builds and uninstalled packages.
    """


# === SOURCE MODE ===========================================================


class SourceInspector:
    """Best-effort Room annotation scanner over Kotlin/Java source."""

    # Directories that never contain hand-written source worth scanning.
    _SKIP_DIRS = {"build", ".gradle", ".idea", "node_modules"}

    def __init__(self, source_dir: str):
        """Initialize with the source root to scan recursively."""
        self.source_dir = Path(source_dir).resolve()

    def execute(self) -> tuple[bool, dict]:
        """Scan source files and return (success, result_dict)."""
        if not self.source_dir.is_dir():
            return False, {"error": f"Directory not found: {self.source_dir}"}

        entities: list[dict] = []
        databases: list[dict] = []
        daos: list[str] = []

        for path in self._iter_source_files():
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            content = strip_comments(content)
            rel = str(path.relative_to(self.source_dir))

            entities.extend(parse_entities(content, rel))
            databases.extend(parse_databases(content, rel))
            daos.extend(parse_daos(content))

        # Dedupe by (class_name, table) so a re-scanned file can't double-count.
        entities = _dedupe(entities, key=lambda e: (e["class_name"], e["table"]))
        databases = _dedupe(databases, key=lambda d: (d["class_name"], d["version"]))
        daos = sorted(set(daos))

        result = {
            "source_dir": str(self.source_dir),
            "entities": entities,
            "databases": databases,
            "daos": daos,
        }
        has_results = bool(entities or databases or daos)
        return has_results, result

    def _iter_source_files(self):
        """Yield .kt / .java files, skipping build / dotdir noise."""
        for path in sorted(self.source_dir.rglob("*")):
            if path.suffix not in (".kt", ".java"):
                continue
            parts = path.relative_to(self.source_dir).parts
            if any(p in self._SKIP_DIRS or p.startswith(".") for p in parts):
                continue
            yield path


def strip_comments(content: str) -> str:
    """Remove // line comments and /* */ block comments (string-literal aware)."""
    out: list[str] = []
    i = 0
    n = len(content)
    quote: str | None = None
    while i < n:
        ch = content[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(content[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        # Not currently inside a string.
        two = content[i : i + 2]
        if two == "//":
            while i < n and content[i] != "\n":
                i += 1
            continue
        if two == "/*":
            i += 2
            while i < n and content[i : i + 2] != "*/":
                i += 1
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def parse_entities(content: str, source_file: str) -> list[dict]:
    """Extract @Entity-annotated classes with their columns.

    Best-effort: handles ``@Entity`` with an optional ``tableName = "..."`` arg
    and either a class body ``{ ... }`` or a primary-constructor parameter list.
    """
    entities: list[dict] = []
    for match in re.finditer(r"@Entity\b", content):
        anno_args = _read_annotation_args(content, match.end())
        decl = _read_class_decl(content, match.end())
        if decl is None:
            continue
        class_name, decl_end = decl

        table_match = re.search(r'tableName\s*=\s*"([^"]+)"', anno_args or "")
        table = table_match.group(1) if table_match else class_name

        # @ForeignKey declarations live in the @Entity annotation args.
        entity_fks = _parse_entity_foreign_keys(anno_args or "")

        body = _read_member_region(content, decl_end)
        columns = parse_columns(body) if body else []

        entities.append(
            {
                "class_name": class_name,
                "table": table,
                "columns": columns,
                "foreign_keys": entity_fks,
                "source_file": source_file,
            }
        )
    return entities


def parse_columns(region: str) -> list[dict]:
    """Parse field/property declarations within an entity region into columns.

    Recognizes Kotlin ``val``/``var`` and Java field declarations. Each field's
    annotations (@PrimaryKey, @ColumnInfo, @ForeignKey, @Embedded, @Ignore) are
    attributed by scanning only the text *between* the previous declaration and
    this one, so a renamed column can't leak its @ColumnInfo onto its neighbor.
    @Ignore'd and @Embedded fields are skipped (Embedded flattens elsewhere).
    """
    columns: list[dict] = []
    seen: set[str] = set()

    # Kotlin val/var declarations and Java-style field declarations, in order.
    kotlin_pattern = re.compile(r"\b(?:val|var)\s+(\w+)\s*:\s*([^\n,=){]+)")
    java_pattern = re.compile(
        r"(?:public|private|protected|final|\s)*" r"([A-Za-z_][\w<>\[\].]*)\s+(\w+)\s*;"
    )

    prev_end = 0
    matches = sorted(
        list(kotlin_pattern.finditer(region)) + list(java_pattern.finditer(region)),
        key=lambda m: m.start(),
    )
    for m in matches:
        # Annotations are whatever sits between the previous field and this one.
        gap = region[prev_end : m.start()]
        prev_end = m.end()
        annotations = _annotations_in(gap)

        if m.re is kotlin_pattern:
            name = m.group(1)
            decl_type = m.group(2).strip().rstrip(",").strip()
        else:
            decl_type = m.group(1)
            name = m.group(2)
            # Skip obvious non-field matches (method-ish / control keywords).
            if decl_type in ("return", "new", "class", "void", "val", "var"):
                continue

        if "Ignore" in annotations or "Embedded" in annotations:
            continue

        column = _build_column(name, decl_type, annotations)
        if column["name"] in seen:
            continue
        seen.add(column["name"])
        columns.append(column)

    return columns


def _build_column(name: str, decl_type: str, annotations: list[str]) -> dict:
    """Assemble a column dict from a field name, type, and its annotations."""
    column_name = name
    col_info = _annotation_args(annotations, "ColumnInfo")
    if col_info:
        name_match = re.search(r'name\s*=\s*"([^"]+)"', col_info)
        if name_match:
            column_name = name_match.group(1)
    return {
        "name": column_name,
        "field": name,
        "type": decl_type,
        "primary_key": any(a.startswith("PrimaryKey") for a in annotations),
        "foreign_key": any(a.startswith("ForeignKey") for a in annotations),
    }


def parse_databases(content: str, source_file: str) -> list[dict]:
    """Extract @Database(entities=[...], version=N) declarations."""
    databases: list[dict] = []
    for match in re.finditer(r"@Database\b", content):
        args = _read_annotation_args(content, match.end())
        if args is None:
            continue
        decl = _read_class_decl(content, match.end())
        class_name = decl[0] if decl else "<unknown>"

        version_match = re.search(r"version\s*=\s*(\d+)", args)
        version = int(version_match.group(1)) if version_match else None

        entities = _parse_entities_list(args)

        databases.append(
            {
                "class_name": class_name,
                "version": version,
                "entities": entities,
                "source_file": source_file,
            }
        )
    return databases


def parse_daos(content: str) -> list[str]:
    """Extract names of @Dao-annotated interfaces/classes."""
    daos: list[str] = []
    for match in re.finditer(r"@Dao\b", content):
        decl = _read_class_decl(content, match.end())
        if decl:
            daos.append(decl[0])
    return daos


def _parse_entities_list(args: str) -> list[str]:
    """Pull ``entities = [Foo::class, Bar.class]`` names out of @Database args."""
    entities_match = re.search(r"entities\s*=\s*[\[{]([^\]}]*)[\]}]", args, re.DOTALL)
    if not entities_match:
        return []
    names: list[str] = []
    for ref in re.finditer(r"(\w+)\s*(?:::class|\.class)", entities_match.group(1)):
        names.append(ref.group(1))
    return names


def _parse_entity_foreign_keys(anno_args: str) -> list[dict]:
    """Extract ForeignKey(entity=..., ...) refs from @Entity annotation args.

    Inside the ``foreignKeys = [...]`` array the nested usages are written
    ``ForeignKey(...)`` (no ``@``), so the leading ``@`` is optional here.
    """
    fks: list[dict] = []
    for fk in re.finditer(r"@?ForeignKey\s*\(([^)]*)\)", anno_args):
        body = fk.group(1)
        entity_match = re.search(r"entity\s*=\s*(\w+)\s*(?:::class|\.class)", body)
        fks.append({"entity": entity_match.group(1) if entity_match else None})
    return fks


def _read_annotation_args(content: str, start: int) -> str | None:
    """Return the parenthesized argument text immediately after an annotation.

    Returns "" for an annotation used without parentheses, None if the next
    non-space char is not "(" and not a declaration boundary.
    """
    i = start
    n = len(content)
    while i < n and content[i] in " \t":
        i += 1
    if i >= n or content[i] != "(":
        return ""
    depth = 0
    out: list[str] = []
    while i < n:
        ch = content[i]
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return "".join(out)
        out.append(ch)
        i += 1
    return "".join(out)


def _read_class_decl(content: str, start: int) -> tuple[str, int] | None:
    """Find the next class/interface/object/data-class name after ``start``.

    Returns (class_name, position_after_name) or None if none found nearby.
    Skips over any further annotations and modifiers between the annotation and
    the declaration keyword.
    """
    decl = re.compile(
        r"\b(?:data\s+|abstract\s+|open\s+|final\s+|sealed\s+|public\s+|"
        r"internal\s+|private\s+|static\s+)*"
        r"(?:class|interface|object|enum\s+class)\s+(\w+)"
    )
    match = decl.search(content, start)
    if not match:
        return None
    return match.group(1), match.end()


def _read_member_region(content: str, start: int) -> str | None:
    """Return the text holding an entity's fields: class body and/or ctor params.

    Handles three shapes:
      - Kotlin primary constructor params: ``class X(...) { ... }`` -> both.
      - Kotlin / Java class body only: ``class X { ... }``.
      - Param list only (rare): ``class X(...)``.
    """
    i = start
    n = len(content)
    ctor = ""
    # Capture a primary-constructor parameter list if one precedes the body.
    while i < n and content[i] in " \t\n":
        i += 1
    if i < n and content[i] == "(":
        ctor, i = _read_balanced(content, i, "(", ")")
    # Capture a class body if one follows.
    while i < n and content[i] not in ("{", "\n"):
        # Skip ": Super(...)" supertype lists until the body brace.
        if content[i] == "{":
            break
        i += 1
    body = ""
    brace = content.find("{", start)
    if brace != -1:
        body, _ = _read_balanced(content, brace, "{", "}")
    region = f"{ctor}\n{body}"
    return region if region.strip() else None


def _read_balanced(content: str, start: int, open_ch: str, close_ch: str) -> tuple[str, int]:
    """Return (inner_text, index_after_close) for a balanced delimiter run."""
    depth = 0
    out: list[str] = []
    i = start
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == open_ch:
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), i


def _annotations_in(text: str) -> list[str]:
    """Extract annotation tokens (with optional arg bodies) from a text blob."""
    annotations: list[str] = []
    for m in re.finditer(r"@(\w+)(\s*\([^)]*\))?", text):
        name = m.group(1)
        args = (m.group(2) or "").strip()
        annotations.append(f"{name}{args}" if args else name)
    return annotations


def _annotation_args(annotations: list[str], name: str) -> str | None:
    """Return the raw arg text for a named annotation, if present."""
    for anno in annotations:
        if anno.startswith(name):
            inner = anno[len(name) :].strip()
            if inner.startswith("(") and inner.endswith(")"):
                return inner[1:-1]
    return None


def _dedupe(items: list[dict], *, key) -> list[dict]:
    """Stable de-duplication of dicts by a key function."""
    seen: set = set()
    out: list[dict] = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


# === SCHEMA MODE ===========================================================


class SchemaInspector:
    """Parse Room's exported schema JSON files."""

    def __init__(self, schema_path: str):
        """Initialize with a path to a schema JSON file or a schemas/ dir."""
        self.schema_path = Path(schema_path).resolve()

    def execute(self, *, show_versions: bool = False) -> tuple[bool, dict]:
        """Parse the schema JSON; optionally list sibling version files."""
        target = self._resolve_target()
        if target is None:
            return False, {"error": f"Schema path not found: {self.schema_path}"}

        try:
            raw = target.read_text(encoding="utf-8")
            doc = json.loads(raw)
        except (OSError, UnicodeDecodeError) as exc:
            return False, {"error": f"Could not read schema: {exc}"}
        except json.JSONDecodeError as exc:
            return False, {"error": f"Invalid schema JSON: {exc}"}

        result = parse_schema_doc(doc)
        result["schema_file"] = str(target)
        if show_versions:
            result["versions"] = self._list_versions(target)
        return True, result

    def get_raw_entity(self, name: str) -> tuple[bool, str]:
        """Dump one entity's raw JSON block by tableName or entity class name."""
        target = self._resolve_target()
        if target is None:
            return False, f"Schema path not found: {self.schema_path}"
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return False, f"Could not read schema: {exc}"

        for entity in doc.get("database", {}).get("entities", []):
            table = entity.get("tableName")
            class_path = entity.get("entityName") or entity.get("entity")
            class_name = class_path.split(".")[-1] if class_path else None
            if name in (table, class_name):
                return True, json.dumps(entity, indent=2)
        return False, f"Entity '{name}' not found in {target.name}"

    def _resolve_target(self) -> Path | None:
        """Resolve to a concrete JSON file.

        If given a directory, pick the highest-numbered ``<N>.json`` file under
        it (recursively, to allow pointing at the ``schemas/`` root).
        """
        if self.schema_path.is_file():
            return self.schema_path
        if self.schema_path.is_dir():
            candidates = sorted(
                self.schema_path.rglob("*.json"),
                key=lambda p: (_version_sort_key(p.stem), p.name),
            )
            return candidates[-1] if candidates else None
        return None

    def _list_versions(self, target: Path) -> list[dict]:
        """List sibling exported version files (``<N>.json``) for the same DB."""
        versions: list[dict] = []
        for path in sorted(target.parent.glob("*.json"), key=lambda p: _version_sort_key(p.stem)):
            versions.append(
                {
                    "version": path.stem,
                    "file": path.name,
                    "is_current": path == target,
                }
            )
        return versions


def parse_schema_doc(doc: dict) -> dict:
    """Parse a Room exported-schema document into structured data.

    Pulls ``database.version`` and each entity's tableName, fields
    (fieldPath, columnName, affinity, notNull), primaryKey, indices, and
    foreignKeys.
    """
    database = doc.get("database", {})
    version = database.get("version")
    entities_out: list[dict] = []

    for entity in database.get("entities", []):
        fields = [
            {
                "field_path": f.get("fieldPath"),
                "column_name": f.get("columnName"),
                "affinity": f.get("affinity"),
                "not_null": bool(f.get("notNull", False)),
            }
            for f in entity.get("fields", [])
        ]
        primary_key = entity.get("primaryKey", {})
        pk_columns = primary_key.get("columnNames", []) if isinstance(primary_key, dict) else []

        indices = [
            {
                "name": idx.get("name"),
                "unique": bool(idx.get("unique", False)),
                "columns": idx.get("columnNames", []),
            }
            for idx in entity.get("indices", [])
        ]

        foreign_keys = [
            {
                "table": fk.get("table"),
                "columns": fk.get("columns", []),
                "referenced_columns": fk.get("referencedColumns", []),
                "on_delete": fk.get("onDelete"),
                "on_update": fk.get("onUpdate"),
            }
            for fk in entity.get("foreignKeys", [])
        ]

        entities_out.append(
            {
                "table": entity.get("tableName"),
                "entity_name": (entity.get("entityName") or entity.get("entity") or "").split(".")[
                    -1
                ],
                "fields": fields,
                "primary_key": pk_columns,
                "indices": indices,
                "foreign_keys": foreign_keys,
            }
        )

    return {
        "version": version,
        "entities": entities_out,
    }


def _version_sort_key(stem: str) -> tuple[int, int | str]:
    """Sort schema file stems numerically when they are integers."""
    if stem.isdigit():
        return (0, int(stem))
    return (1, stem)


# === LIVE MODE =============================================================


class LiveInspector:
    """Dump a debuggable app's SQLite schema via ``adb run-as``."""

    def __init__(self, serial: str | None = None):
        """Initialize with an optional resolved device serial."""
        self.serial = serial

    def execute(self, package: str, db_name: str | None = None) -> tuple[bool, dict]:
        """List databases for a package, or dump a named DB's SQLite schema."""
        db_dir = f"/data/data/{package}/databases"

        if db_name is None:
            try:
                output = self._run_as(package, ["ls", "-1", db_dir])
            except RunAsDeniedError as exc:
                return False, _denied_result(package, exc)
            except _RunAsError as exc:
                return False, {"error": str(exc)}
            names = [
                line.strip()
                for line in output.splitlines()
                if line.strip() and not _is_sqlite_sidecar(line.strip())
            ]
            return True, {
                "package": package,
                "databases_dir": db_dir,
                "databases": names,
                "total_databases": len(names),
            }

        remote_db = f"{db_dir}/{db_name}"
        try:
            schema_text, method = self._dump_schema(package, remote_db, db_name)
        except RunAsDeniedError as exc:
            return False, _denied_result(package, exc)
        if schema_text is None:
            return False, {"error": method, "package": package, "database": db_name}

        tables = parse_sqlite_schema(schema_text)
        return True, {
            "package": package,
            "database": db_name,
            "path": remote_db,
            "method": method,
            "tables": tables,
            "total_tables": len(tables),
            "schema": schema_text,
        }

    # --- command execution -------------------------------------------------

    def _run_as(self, package: str, argv: list[str]) -> str:
        """Run ``adb shell run-as <pkg> <argv>`` returning text stdout."""
        quoted = [quote_for_device_shell(a) for a in argv]
        cmd = build_adb_command(
            "shell", self.serial, "run-as", quote_for_device_shell(package), *quoted
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=RUN_AS_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise _RunAsError("adb not found on PATH — install Android platform-tools") from exc
        except subprocess.TimeoutExpired as exc:
            raise _RunAsError(f"adb timed out after {RUN_AS_TIMEOUT}s") from exc

        combined = f"{result.stdout}\n{result.stderr}"
        _raise_if_denied(package, combined, result.returncode)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise _RunAsError(f"Command failed ({' '.join(argv)}): {detail}")
        return result.stdout

    def _exec_out_run_as(self, package: str, argv: list[str]) -> bytes:
        """Run ``adb exec-out run-as <pkg> <argv>`` returning raw bytes."""
        quoted = [quote_for_device_shell(a) for a in argv]
        cmd = build_adb_command(
            "exec-out", self.serial, "run-as", quote_for_device_shell(package), *quoted
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=RUN_AS_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise _RunAsError("adb not found on PATH — install Android platform-tools") from exc
        except subprocess.TimeoutExpired as exc:
            raise _RunAsError(f"adb timed out after {RUN_AS_TIMEOUT}s") from exc

        stderr_text = result.stderr.decode("utf-8", errors="replace")
        _raise_if_denied(package, stderr_text, result.returncode)
        if result.returncode != 0:
            raise _RunAsError(stderr_text.strip() or f"exit code {result.returncode}")
        return result.stdout

    def _dump_schema(self, package: str, remote_db: str, name: str) -> tuple[str | None, str]:
        """Return (schema_text, method) or (None, error_message).

        Tries on-device sqlite3 first; falls back to pulling the DB locally and
        reading it with the host's sqlite3 module. A run-as denial propagates as
        ``RunAsDeniedError`` so the caller can build a structured denial result.
        """
        # Strategy 1: on-device sqlite3 binary (not always present).
        try:
            out = self._run_as(package, ["sqlite3", remote_db, ".schema"])
            if out.strip():
                return out, "device-sqlite3"
        except _RunAsError:
            pass  # fall through to host-side read (RunAsDeniedError propagates)

        # Strategy 2: pull the DB bytes and read its schema with host sqlite3.
        try:
            raw = self._exec_out_run_as(package, ["cat", remote_db])
        except _RunAsError as exc:
            return None, f"Could not read database {name}: {exc}"

        if not raw:
            return None, f"Database {name} is empty or unreadable"
        if len(raw) > DB_MAX_BYTES:
            return None, (
                f"Database {name} is {len(raw):,} bytes, over the "
                f"{DB_MAX_BYTES:,}-byte cap (ANDROID_EMU_MODEL_DB_MAX_BYTES)"
            )

        return _read_schema_from_bytes(raw, name)


def _read_schema_from_bytes(raw: bytes, name: str) -> tuple[str | None, str]:
    """Write DB bytes to a temp file and read its schema via host sqlite3."""
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            conn = sqlite3.connect(tmp.name)
            try:
                rows = conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL ORDER BY type DESC, name"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            return None, f"{name} is not a valid SQLite database: {exc}"

    schema_text = "\n".join(f"{row[0]};" for row in rows)
    return schema_text, "host-sqlite3"


# === RUN-AS HELPERS ========================================================


class _RunAsError(RuntimeError):
    """Generic run-as / adb command failure (not a denial)."""


def _raise_if_denied(package: str, text: str, returncode: int) -> None:
    """Raise RunAsDeniedError if output looks like a run-as denial."""
    if returncode == 0:
        return
    lowered = text.lower()
    for marker in _RUN_AS_DENIED_MARKERS:
        if marker in lowered:
            raise RunAsDeniedError(f"run-as denied for '{package}': {text.strip() or marker}")


def _denied_result(package: str, exc: RunAsDeniedError) -> dict:
    """Build a structured, actionable error dict for a run-as denial."""
    return {
        "error": str(exc),
        "package": package,
        "run_as_denied": True,
        "hint": (
            "run-as only works for DEBUGGABLE apps (debug builds with "
            'android:debuggable="true"). Release/AAB-signed builds and '
            "uninstalled packages are not inspectable this way. Install a debug "
            "build, or use a rooted device for release builds."
        ),
    }


def _is_sqlite_sidecar(name: str) -> bool:
    """True for SQLite WAL/SHM/journal sidecar files."""
    lower = name.lower()
    return lower.endswith(("-wal", "-shm", "-journal"))


# === SQLITE SCHEMA PARSER ==================================================


def parse_sqlite_schema(schema_text: str) -> list[dict]:
    """Parse ``sqlite3 .schema`` output into per-table column metadata.

    Extracts ``CREATE TABLE`` statements (ignoring indexes/triggers/views) and
    pulls out column names + declared types. Robust to multi-line formatting and
    ``IF NOT EXISTS`` / quoting variations.
    """
    tables: list[dict] = []
    for statement in _split_sql_statements(schema_text):
        normalized = statement.strip()
        if not normalized or not normalized.lower().startswith("create table"):
            continue
        table_name = _extract_table_name(normalized)
        if table_name is None:
            continue
        columns = _extract_columns(normalized)
        tables.append({"name": table_name, "columns": columns})
    return tables


def _split_sql_statements(text: str) -> list[str]:
    """Split a schema dump into statements on top-level semicolons."""
    statements: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            statements.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        statements.append("".join(buf))
    return statements


def _extract_table_name(statement: str) -> str | None:
    """Pull the table name out of a CREATE TABLE statement."""
    match = re.search(
        r'create\s+table\s+(?:if\s+not\s+exists\s+)?[`"\[]?([A-Za-z0-9_]+)[`"\]]?',
        statement,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _extract_columns(statement: str) -> list[dict]:
    """Extract column {name, type} dicts from a CREATE TABLE statement."""
    start = statement.find("(")
    end = statement.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return []
    body = statement[start + 1 : end]

    columns: list[dict] = []
    for raw_def in _split_column_defs(body):
        definition = raw_def.strip()
        if not definition:
            continue
        lowered = definition.lower()
        if lowered.startswith(("primary key", "foreign key", "unique", "check", "constraint")):
            continue
        tokens = (
            definition.replace("`", " ")
            .replace('"', " ")
            .replace("[", " ")
            .replace("]", " ")
            .split()
        )
        if not tokens:
            continue
        col_name = tokens[0]
        col_type = tokens[1] if len(tokens) > 1 else ""
        if col_type.lower() in ("primary", "not", "default", "unique", "references", "check"):
            col_type = ""
        columns.append({"name": col_name, "type": col_type.upper()})
    return columns


def _split_column_defs(body: str) -> list[str]:
    """Split a CREATE TABLE column body on top-level commas."""
    defs: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            defs.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        defs.append("".join(buf))
    return defs


# === OUTPUT FORMATTING =====================================================


def format_source(result: dict, *, verbose: bool) -> str:
    """Render SOURCE-mode results (concise by default, detailed under verbose)."""
    lines: list[str] = []
    entities = result["entities"]
    databases = result["databases"]
    daos = result["daos"]

    for db in databases:
        version = db["version"] if db["version"] is not None else "?"
        lines.append(
            f"@Database {db['class_name']} (version {version}, "
            f"{len(db['entities'])} entities): {', '.join(db['entities']) or '—'}"
        )

    col_total = sum(len(e["columns"]) for e in entities)
    lines.append(f"Room: {len(entities)} @Entity, {col_total} columns, {len(daos)} @Dao")

    if verbose:
        for entity in entities:
            lines.append("")
            lines.append(
                f"@Entity {entity['class_name']} -> table '{entity['table']}' "
                f"({len(entity['columns'])} columns)  [{entity['source_file']}]"
            )
            for col in entity["columns"]:
                flags = []
                if col["primary_key"]:
                    flags.append("PK")
                if col["foreign_key"]:
                    flags.append("FK")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                rename = f" (field {col['field']})" if col["name"] != col["field"] else ""
                lines.append(f"  - {col['name']}: {col['type']}{flag_str}{rename}")
            for fk in entity["foreign_keys"]:
                lines.append(f"  FK -> {fk['entity']}")
        if daos:
            lines.append("")
            lines.append(f"DAOs: {', '.join(daos)}")

    return "\n".join(lines)


def format_schema(result: dict, *, verbose: bool) -> str:
    """Render SCHEMA-mode results."""
    lines: list[str] = []
    version = result.get("version")
    entities = result["entities"]
    lines.append(
        f"Room schema v{version}: {len(entities)} entities " f"[{Path(result['schema_file']).name}]"
    )

    if result.get("versions"):
        names = []
        for v in result["versions"]:
            marker = "*" if v["is_current"] else ""
            names.append(f"{v['version']}{marker}")
        lines.append(f"Exported versions: {', '.join(names)} (* = parsed)")

    if verbose:
        for entity in entities:
            lines.append("")
            pk = ", ".join(entity["primary_key"]) or "—"
            lines.append(
                f"{entity['table']} ({entity['entity_name'] or '?'}), "
                f"{len(entity['fields'])} fields, PK: {pk}"
            )
            for field in entity["fields"]:
                nn = " NOT NULL" if field["not_null"] else ""
                lines.append(
                    f"  - {field['column_name']}: {field['affinity']}{nn} "
                    f"(field {field['field_path']})"
                )
            for idx in entity["indices"]:
                unique = "UNIQUE " if idx["unique"] else ""
                lines.append(f"  index {unique}{idx['name']}: {', '.join(idx['columns'])}")
            for fk in entity["foreign_keys"]:
                lines.append(
                    f"  FK {', '.join(fk['columns'])} -> "
                    f"{fk['table']}({', '.join(fk['referenced_columns'])})"
                )
    else:
        for entity in entities:
            lines.append(f"  {entity['table']}: {len(entity['fields'])} fields")

    return "\n".join(lines)


def format_live(result: dict, *, verbose: bool) -> str:
    """Render LIVE-mode results."""
    if "tables" in result:
        lines = [
            f"{result['database']}: {result['total_tables']} tables " f"(via {result['method']})"
        ]
        for table in result["tables"]:
            cols = ", ".join(c["name"] for c in table["columns"])
            lines.append(f"  {table['name']}: {cols}")
        if verbose:
            lines.append("")
            lines.append("--- schema ---")
            lines.append(result["schema"])
        return "\n".join(lines)

    dbs = result["databases"]
    if not dbs:
        return f"No databases found for {result['package']}"
    lines = [f"databases ({result['total_databases']}):"]
    for db in dbs:
        lines.append(f"  {db}")
    return "\n".join(lines)


# === CLI ===================================================================


def main() -> None:
    """Parse arguments and dispatch the selected inspection mode."""
    parser = argparse.ArgumentParser(
        description=(
            "Inspect Android persistence models: Room annotations (--source), "
            "Room exported schema JSON (--schema), or a live debuggable app's "
            "SQLite schema (--package). " + SCOPING_NOTE
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scoping:
  Room is backed by SQLite. This inspector covers Room (annotations + exported
  schema) and raw SQLite. Other Android persistence (SQLDelight, Realm,
  ObjectBox, Jetpack DataStore) is not specifically parsed.

Examples:
  # SOURCE: scan Kotlin/Java source for Room annotations
  python model_inspector.py --source app/src/main/java
  python model_inspector.py --source app/src/main/java --verbose

  # SCHEMA: parse Room's exported schema JSON (room.schemaLocation)
  python model_inspector.py --schema app/schemas/com.app.AppDb/3.json
  python model_inspector.py --schema app/schemas --show-versions
  python model_inspector.py --schema app/schemas/com.app.AppDb/3.json --raw users

  # LIVE: dump a debuggable app's SQLite schema via run-as
  python model_inspector.py --package com.example.app
  python model_inspector.py --package com.example.app --db app.db --verbose
        """,
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", metavar="DIR", help="Scan a source dir for Room annotations")
    mode.add_argument("--schema", metavar="PATH", help="Parse a Room exported-schema JSON file/dir")
    mode.add_argument(
        "--package", metavar="PKG", help="Live: dump a debuggable app's SQLite schema"
    )

    parser.add_argument(
        "--show-versions",
        action="store_true",
        help="SCHEMA: list all exported version files alongside the parsed one",
    )
    parser.add_argument(
        "--raw",
        metavar="NAME",
        help="SCHEMA: dump one entity's raw JSON (by tableName or class name)",
    )
    parser.add_argument(
        "--db",
        metavar="NAME",
        help="LIVE: database file name to dump (lists databases/ if omitted)",
    )
    parser.add_argument("--serial", help="LIVE: device serial (auto-detects if omitted)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show full detail")

    args = parser.parse_args()

    if args.source is not None:
        _run_source(args)
    elif args.schema is not None:
        _run_schema(args)
    else:
        _run_live(args)


def _run_source(args: argparse.Namespace) -> None:
    """Dispatch SOURCE mode."""
    inspector = SourceInspector(args.source)
    success, result = inspector.execute()
    if not success:
        _fail(result, args)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_source(result, verbose=args.verbose))
    sys.exit(0)


def _run_schema(args: argparse.Namespace) -> None:
    """Dispatch SCHEMA mode."""
    inspector = SchemaInspector(args.schema)
    if args.raw:
        ok, raw = inspector.get_raw_entity(args.raw)
        if not ok:
            print(f"Error: {raw}", file=sys.stderr)
            sys.exit(1)
        print(raw)
        sys.exit(0)

    success, result = inspector.execute(show_versions=args.show_versions)
    if not success:
        _fail(result, args)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_schema(result, verbose=args.verbose or args.show_versions))
    sys.exit(0)


def _run_live(args: argparse.Namespace) -> None:
    """Dispatch LIVE mode."""
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    inspector = LiveInspector(serial=serial)
    success, result = inspector.execute(args.package, db_name=args.db)

    if args.json:
        print(json.dumps({"success": success, **result}, indent=2))
        sys.exit(0 if success else 1)

    if not success:
        print(f"Error: {result.get('error', result)}", file=sys.stderr)
        if result.get("run_as_denied"):
            print(f"Hint: {result['hint']}", file=sys.stderr)
        sys.exit(1)

    print(format_live(result, verbose=args.verbose))
    sys.exit(0)


def _fail(result: dict, args: argparse.Namespace) -> None:
    """Emit a failure result and exit non-zero."""
    if args.json:
        print(json.dumps({"success": False, **result}, indent=2))
    else:
        print(f"Error: {result.get('error', 'no results found')}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
