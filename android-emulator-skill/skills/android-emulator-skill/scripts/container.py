#!/usr/bin/env python3
"""
App sandbox inspector for DEBUGGABLE Android apps.

The Android counterpart of the iOS container inspector. Where iOS uses
``xcrun simctl get_app_container`` to reach into a sandbox, Android has no such
affordance for arbitrary packages: the only sanctioned way to read another app's
private data dir (``/data/data/<package>``) on a non-rooted device is
``adb shell run-as <package> <cmd>``, which the platform permits **only for apps
flagged android:debuggable="true"** (i.e. debug builds).

This tool wraps ``run-as`` to provide semantic access to the data dir and clearly
reports when ``run-as`` is denied (release / non-debuggable app, or package not
installed) instead of failing cryptically.

Key features:
- List the data dir (--ls) via ``run-as ls -la``.
- Read a file (--cat) via ``run-as cat`` with a size cap + truncation note.
- Inspect shared_prefs/ XML (--shared-prefs), parsed into key/value JSON.
- Inspect databases/ (--databases), dumping the SQLite schema for a named DB.
- Export a snapshot (--export): shared_prefs/, a databases listing, and a file
  tree, written to a local directory.

Android Room is plain SQLite — there is no Core Data analogue here.

Out of scope: rooted-device ``su`` access and release builds. ``run-as`` is the
documented, safe mechanism and is what real debugging workflows rely on.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common.cache_utils import ProgressiveCache
from common.device_utils import build_adb_command, quote_for_device_shell, resolve_device_identifier
from common.env_config import env_int

# Cap on bytes pulled by --cat before truncation. Guards against dumping a
# multi-megabyte database/blob inline. Override via the ANDROID_EMU_ prefix.
CAT_MAX_BYTES = env_int("ANDROID_EMU_CONTAINER_CAT_MAX_BYTES", 65_536, min_value=1024)

# Threshold above which --cat output is cached for progressive disclosure
# instead of printed inline (kept below CAT_MAX_BYTES so caps still apply).
CAT_CACHE_BYTES = env_int("ANDROID_EMU_CONTAINER_CAT_CACHE_BYTES", 8_192, min_value=512)

# Timeout (seconds) for a single run-as adb invocation.
RUN_AS_TIMEOUT = env_int("ANDROID_EMU_CONTAINER_TIMEOUT", 30, min_value=5)

# Markers adb/run-as emit when run-as is refused. Matched case-insensitively as
# substrings; the platform wording has drifted across API levels.
_RUN_AS_DENIED_MARKERS = (
    "is not debuggable",
    "not debuggable",
    "is unknown",
    "unknown package",
    "run-as: package not found",
    "package not found",
    "is not an application",
    "user is not debuggable",
    "permission denied",
)


class RunAsDeniedError(RuntimeError):
    """Raised when ``run-as`` is refused for the target package.

    This is the expected outcome for release / non-debuggable builds and for
    packages that are not installed. Carries a human-readable explanation.
    """


class ContainerInspector:
    """Inspect a debuggable app's private data dir via ``adb run-as``."""

    def __init__(self, serial: str | None = None):
        """Initialize inspector.

        Args:
            serial: Device serial (uses default device if None).
        """
        self.serial = serial
        self._cache = ProgressiveCache()

    # === PUBLIC API ===

    def data_dir(self, package: str) -> str:
        """Return the conventional data dir for a package.

        Args:
            package: App package name (e.g. com.example.app).

        Returns:
            The absolute on-device path (/data/data/<package>).
        """
        return f"/data/data/{package}"

    def list_dir(self, package: str, subpath: str = "") -> tuple[bool, dict]:
        """List the app data dir (or a sub-path) via ``run-as ls -la``.

        Args:
            package: App package name.
            subpath: Path relative to the data dir (default: data dir root).

        Returns:
            (success, result_dict) with parsed 'entries' on success.
        """
        rel = _safe_relative(subpath)
        if rel is None:
            return False, {"error": f"Path escapes the data dir: {subpath!r}"}

        target = _join_remote(self.data_dir(package), rel)
        try:
            output = self._run_as(package, ["ls", "-la", target])
        except RunAsDeniedError as exc:
            return False, _denied_result(package, exc)
        except _RunAsError as exc:
            return False, {"error": str(exc)}

        entries = parse_ls_output(output)
        return True, {
            "package": package,
            "data_dir": self.data_dir(package),
            "listed_path": target,
            "entries": entries,
            "total_entries": len(entries),
        }

    def cat_file(self, package: str, relpath: str) -> tuple[bool, dict]:
        """Read a file under the data dir via ``run-as cat`` (size-capped).

        The file is read through ``adb exec-out run-as <pkg> cat`` so binary
        bytes survive transport. Output is capped at CAT_MAX_BYTES; large text
        is routed through ProgressiveCache.

        Args:
            package: App package name.
            relpath: Path relative to the data dir.

        Returns:
            (success, result_dict) with 'content' or 'cache_id'.
        """
        rel = _safe_relative(relpath)
        if not rel:
            return False, {"error": f"Invalid file path: {relpath!r}"}

        target = _join_remote(self.data_dir(package), rel)
        try:
            raw = self._exec_out_run_as(package, ["cat", target])
        except RunAsDeniedError as exc:
            return False, _denied_result(package, exc)
        except _RunAsError as exc:
            return False, {"error": str(exc)}

        size_bytes = len(raw)
        truncated = size_bytes > CAT_MAX_BYTES
        body = raw[:CAT_MAX_BYTES] if truncated else raw

        try:
            content = body.decode("utf-8")
            content_type = "text"
        except UnicodeDecodeError:
            return True, {
                "package": package,
                "path": rel,
                "size_bytes": size_bytes,
                "content_type": "binary",
                "truncated": truncated,
                "content": f"<binary file: {size_bytes} bytes — use --export to retrieve>",
            }

        base = {
            "package": package,
            "path": rel,
            "size_bytes": size_bytes,
            "content_type": content_type,
            "truncated": truncated,
        }
        if truncated:
            base["note"] = (
                f"Output capped at {CAT_MAX_BYTES:,} bytes "
                f"(file is at least {size_bytes:,}). Use --export for the full file."
            )

        if len(content) > CAT_CACHE_BYTES:
            cache_id = self._cache.save({**base, "content": content}, "container-cat")
            return True, {
                **base,
                "cache_id": cache_id,
                "note": (f"{base.get('note', '')} Full content cached as '{cache_id}'.".strip()),
            }

        return True, {**base, "content": content}

    def shared_prefs(self, package: str, name: str | None = None) -> tuple[bool, dict]:
        """List shared_prefs/*.xml, or dump a named prefs file as key/values.

        Args:
            package: App package name.
            name: Prefs file name (with or without .xml). If None, lists files.

        Returns:
            (success, result_dict).
        """
        prefs_dir = _join_remote(self.data_dir(package), "shared_prefs")

        if name is None:
            try:
                output = self._run_as(package, ["ls", "-la", prefs_dir])
            except RunAsDeniedError as exc:
                return False, _denied_result(package, exc)
            except _RunAsError as exc:
                return False, {"error": str(exc)}
            files = [e["name"] for e in parse_ls_output(output) if e["name"].endswith(".xml")]
            return True, {
                "package": package,
                "shared_prefs_dir": prefs_dir,
                "files": files,
                "total_files": len(files),
            }

        filename = name if name.endswith(".xml") else f"{name}.xml"
        target = _join_remote(prefs_dir, filename)
        try:
            raw = self._exec_out_run_as(package, ["cat", target])
        except RunAsDeniedError as exc:
            return False, _denied_result(package, exc)
        except _RunAsError as exc:
            return False, {"error": str(exc)}

        try:
            xml_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False, {"error": f"shared_prefs file is not UTF-8 text: {filename}"}

        try:
            values = parse_shared_prefs_xml(xml_text)
        except ValueError as exc:
            return False, {"error": f"Could not parse {filename}: {exc}"}

        return True, {
            "package": package,
            "file": filename,
            "path": target,
            "preferences": values,
            "total_keys": len(values),
        }

    def databases(self, package: str, name: str | None = None) -> tuple[bool, dict]:
        """List databases/ files, or dump the SQLite schema for a named DB.

        Schema strategy: try ``run-as <pkg> sqlite3 <db> .schema`` first (the
        on-device sqlite3 binary is not always present); on failure fall back to
        pulling the DB bytes via ``exec-out run-as cat`` to a local temp file and
        reading the schema with the host's sqlite3 module.

        Args:
            package: App package name.
            name: DB file name. If None, lists files.

        Returns:
            (success, result_dict).
        """
        db_dir = _join_remote(self.data_dir(package), "databases")

        if name is None:
            try:
                output = self._run_as(package, ["ls", "-la", db_dir])
            except RunAsDeniedError as exc:
                return False, _denied_result(package, exc)
            except _RunAsError as exc:
                return False, {"error": str(exc)}
            entries = parse_ls_output(output)
            files = [
                e for e in entries if e["kind"] == "file" and not _is_sqlite_sidecar(e["name"])
            ]
            sidecars = [e["name"] for e in entries if _is_sqlite_sidecar(e["name"])]
            return True, {
                "package": package,
                "databases_dir": db_dir,
                "databases": [{"name": e["name"], "size_bytes": e["size_bytes"]} for e in files],
                "sidecars": sidecars,
                "total_databases": len(files),
            }

        target = _join_remote(db_dir, name)
        schema_text, method = self._dump_schema(package, target, name)
        if schema_text is None:
            return False, {
                "error": method,  # method holds the error message on failure
                "package": package,
                "database": name,
            }

        tables = parse_sqlite_schema(schema_text)
        return True, {
            "package": package,
            "database": name,
            "path": target,
            "method": method,
            "tables": tables,
            "total_tables": len(tables),
            "schema": schema_text,
        }

    def export(self, package: str, dest_dir: str) -> tuple[bool, dict]:
        """Write a local snapshot of the app's inspectable data.

        Snapshot layout under ``<dest_dir>/<package>/``:
        - ``shared_prefs/<name>.xml`` for each prefs file
        - ``databases.json`` — a listing of databases/ (names + sizes)
        - ``file_tree.txt`` — the top-level data-dir listing

        Args:
            package: App package name.
            dest_dir: Local destination directory (created if absent).

        Returns:
            (success, result_dict).
        """
        # Fail fast (and clearly) if run-as is denied before creating anything.
        ok, listing = self.list_dir(package)
        if not ok:
            return False, listing

        target = Path(dest_dir) / package
        if target.exists():
            return False, {"error": f"Destination already exists: {target}"}
        target.mkdir(parents=True)

        written: list[str] = []

        # Top-level file tree.
        tree_path = target / "file_tree.txt"
        tree_lines = [
            f"{e['mode']}  {e['size_bytes']:>10}  {e['name']}" for e in listing["entries"]
        ]
        tree_path.write_text("\n".join(tree_lines) + "\n", encoding="utf-8")
        written.append(str(tree_path))

        # shared_prefs/*.xml — dump each verbatim.
        prefs_ok, prefs = self.shared_prefs(package)
        if prefs_ok and prefs["files"]:
            prefs_out = target / "shared_prefs"
            prefs_out.mkdir()
            for fname in prefs["files"]:
                src = _join_remote(self.data_dir(package), f"shared_prefs/{fname}")
                try:
                    raw = self._exec_out_run_as(package, ["cat", src])
                except (RunAsDeniedError, _RunAsError):
                    continue
                (prefs_out / fname).write_bytes(raw)
                written.append(str(prefs_out / fname))

        # databases listing (metadata only — not the binary DBs).
        db_ok, dbs = self.databases(package)
        db_json = target / "databases.json"
        db_payload = dbs if db_ok else {"error": dbs.get("error", "unavailable")}
        db_json.write_text(json.dumps(db_payload, indent=2), encoding="utf-8")
        written.append(str(db_json))

        return True, {
            "package": package,
            "destination": str(target),
            "files_written": written,
            "total_files": len(written),
        }

    # === COMMAND EXECUTION ===

    def _run_as(self, package: str, argv: list[str]) -> str:
        """Run ``adb shell run-as <package> <argv...>`` and return text stdout.

        Raises:
            RunAsDeniedError: run-as refused (non-debuggable / missing package).
            _RunAsError: any other command failure.
        """
        quoted = [quote_for_device_shell(a) for a in argv]
        cmd = build_adb_command(
            "shell", self.serial, "run-as", quote_for_device_shell(package), *quoted
        )
        result = self._invoke(cmd)
        combined = f"{result.stdout}\n{result.stderr}"
        _raise_if_denied(package, combined, result.returncode)
        if result.returncode != 0:
            raise _RunAsError(_describe_failure(argv, result))
        return result.stdout

    def _exec_out_run_as(self, package: str, argv: list[str]) -> bytes:
        """Run ``adb exec-out run-as <package> <argv...>`` and return raw bytes.

        ``exec-out`` avoids the line-ending mangling ``shell`` applies, so binary
        payloads (sqlite DBs, blobs) survive.

        Raises:
            RunAsDeniedError / _RunAsError as in :meth:`_run_as`.
        """
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

    def _invoke(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a text-mode adb command with consistent error mapping."""
        try:
            return subprocess.run(
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

    def _dump_schema(self, package: str, remote_db: str, name: str) -> tuple[str | None, str]:
        """Return (schema_text, method) or (None, error_message).

        Tries on-device sqlite3 first; falls back to pulling the DB locally.
        """
        # Strategy 1: on-device sqlite3.
        try:
            out = self._run_as(package, ["sqlite3", remote_db, ".schema"])
            if out.strip():
                return out, "device-sqlite3"
        except RunAsDeniedError as exc:
            return None, _denied_result(package, exc)["error"]
        except _RunAsError:
            pass  # sqlite3 binary may be absent — fall through to pull.

        # Strategy 2: pull the DB bytes and read its schema with the host sqlite3.
        try:
            raw = self._exec_out_run_as(package, ["cat", remote_db])
        except RunAsDeniedError as exc:
            return None, _denied_result(package, exc)["error"]
        except _RunAsError as exc:
            return None, f"Could not read database {name}: {exc}"

        if not raw:
            return None, f"Database {name} is empty or unreadable"

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


# === EXCEPTIONS / RESULT HELPERS ===


class _RunAsError(RuntimeError):
    """Generic run-as / adb command failure (not a denial)."""


def _raise_if_denied(package: str, text: str, returncode: int) -> None:
    """Raise RunAsDeniedError if output looks like a run-as denial.

    Args:
        package: Target package (for the message).
        text: Combined stdout+stderr to scan.
        returncode: Process exit code (denials are non-zero).
    """
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


def _describe_failure(argv: list[str], result: subprocess.CompletedProcess) -> str:
    """Compose an error message from a failed text-mode command."""
    detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    return f"Command failed ({' '.join(argv)}): {detail}"


# === PATH HELPERS ===


def _safe_relative(subpath: str) -> str | None:
    """Normalize a user-supplied relative path, rejecting escapes.

    Returns the cleaned relative path, or None if it escapes the data dir.
    An empty / "." path normalizes to "".
    """
    if not subpath or subpath in (".", "./"):
        return ""
    cleaned = subpath.strip().lstrip("/")
    parts: list[str] = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None  # refuse to climb out of the data dir
        parts.append(part)
    return "/".join(parts)


def _join_remote(base: str, rel: str) -> str:
    """Join a remote base path with a relative path (POSIX semantics)."""
    if not rel:
        return base
    return f"{base.rstrip('/')}/{rel}"


def _is_sqlite_sidecar(name: str) -> bool:
    """True for SQLite WAL/SHM/journal sidecar files."""
    lower = name.lower()
    return lower.endswith(("-wal", "-shm", "-journal"))


# === PURE PARSERS ===


def parse_ls_output(output: str) -> list[dict]:
    """Parse ``ls -la`` output into structured entries.

    Handles the toybox/coreutils layouts adb ships:

        drwx------ 4 u0_a123 u0_a123 4096 2024-01-02 03:04 shared_prefs
        -rw------- 1 u0_a123 u0_a123  220 2024-01-02 03:04 prefs.xml
        lrwxrwxrwx 1 root    root       11 2024-01-02 03:04 lib -> /data/app

    The leading ``total N`` line and the ``.`` / ``..`` entries are skipped.

    Args:
        output: Raw ``ls -la`` stdout.

    Returns:
        List of dicts: {name, mode, kind, size_bytes, owner, group, symlink_target}.
    """
    entries: list[dict] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith("total "):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        mode = parts[0]
        if not _looks_like_mode(mode):
            continue

        kind = _kind_from_mode(mode)
        # Size is the 5th column on a standard `ls -l` (after mode, links,
        # owner, group). Device files have major,minor instead — treat as 0.
        size_bytes = 0
        if len(parts) >= 5 and parts[4].isdigit():
            size_bytes = int(parts[4])

        owner = parts[2] if len(parts) > 2 else ""
        group = parts[3] if len(parts) > 3 else ""

        # The name is everything after the timestamp columns. Rather than guess
        # the date format, locate the name by stripping the known leading fields
        # and any date/time tokens; simplest robust approach: take the tail after
        # the size column index, then drop date-like tokens.
        name, symlink_target = _extract_name(parts, kind)
        if name in (".", "..", ""):
            continue

        entries.append(
            {
                "name": name,
                "mode": mode,
                "kind": kind,
                "size_bytes": size_bytes,
                "owner": owner,
                "group": group,
                "symlink_target": symlink_target,
            }
        )
    return entries


def _looks_like_mode(token: str) -> bool:
    """True if a token looks like a 10-char ls permission string."""
    if len(token) < 10:
        return False
    return token[0] in "-dlcbsp" and all(c in "rwxsStTl-" for c in token[1:10])


def _kind_from_mode(mode: str) -> str:
    """Map the leading mode char to a kind label."""
    head = mode[0]
    if head == "d":
        return "dir"
    if head == "l":
        return "symlink"
    return "file"


_MONTH_ABBR = {
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
}


def _extract_name(parts: list[str], kind: str) -> tuple[str, str | None]:
    """Extract the entry name (and symlink target) from ls columns.

    Two date-block layouts are handled, both starting after the fixed columns
    (mode, nlink, owner, group, size):

    - toybox / busybox (Android): ``YYYY-MM-DD HH:MM`` — two tokens.
    - GNU coreutils: ``Mon DD HH:MM`` or ``Mon DD YYYY`` — three tokens.

    Args:
        parts: Whitespace-split ls line.
        kind: Entry kind (symlinks split on " -> ").

    Returns:
        (name, symlink_target) — target is None for non-symlinks.
    """
    tail = parts[5:]
    if tail and tail[0][:3].lower() in _MONTH_ABBR:
        date_tokens = 3  # GNU: month, day, year-or-time
    else:
        # toybox: ISO date + time. Skip leading date/time-looking tokens.
        idx = 0
        while idx < len(tail) and _is_datetime_token(tail[idx]):
            idx += 1
        date_tokens = idx
    name = " ".join(tail[date_tokens:])

    symlink_target: str | None = None
    if kind == "symlink" and " -> " in name:
        name, _, symlink_target = name.partition(" -> ")
        name = name.rstrip()
        symlink_target = symlink_target.strip()
    return name, symlink_target


def _is_datetime_token(token: str) -> bool:
    """Heuristic: True if a token is a date / time / year column from ls."""
    if "-" in token and token.replace("-", "").isdigit():
        return True  # 2024-01-02
    if ":" in token and token.replace(":", "").isdigit():
        return True  # 03:04 or 03:04:05
    if token.isdigit() and len(token) == 4:
        return True  # bare year column (coreutils -l)
    return token in {
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    }


def parse_shared_prefs_xml(xml_text: str) -> dict:
    """Parse an Android shared_prefs XML document into a key/value dict.

    Recognized element types and their value semantics:

        <string name="k">v</string>            -> str
        <int name="k" value="1" />             -> int
        <long name="k" value="1" />            -> int
        <float name="k" value="1.5" />         -> float
        <boolean name="k" value="true" />      -> bool
        <set name="k"><string>a</string>...</set> -> list[str]

    Args:
        xml_text: Raw XML contents of a prefs file.

    Returns:
        Dict mapping preference names to typed values.

    Raises:
        ValueError: If the XML is malformed.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(str(exc)) from exc

    result: dict = {}
    for elem in root:
        key = elem.get("name")
        if key is None:
            continue
        tag = elem.tag
        if tag == "string":
            result[key] = elem.text if elem.text is not None else ""
        elif tag in ("int", "long"):
            result[key] = _to_int(elem.get("value"))
        elif tag == "float":
            result[key] = _to_float(elem.get("value"))
        elif tag == "boolean":
            result[key] = elem.get("value", "false").strip().lower() == "true"
        elif tag == "set":
            members: list[str] = []
            for child in elem:
                members.append(child.text if child.text is not None else "")
            result[key] = members
        else:
            # Unknown type — fall back to the raw value attr or text.
            result[key] = elem.get("value", elem.text)
    return result


def _to_int(value: str | None) -> int:
    """Best-effort int parse (0 on miss)."""
    if value is None:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _to_float(value: str | None) -> float:
    """Best-effort float parse (0.0 on miss)."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_sqlite_schema(schema_text: str) -> list[dict]:
    """Parse ``sqlite3 .schema`` output into per-table column metadata.

    Extracts ``CREATE TABLE`` statements (ignoring indexes/triggers/views) and
    pulls out column names + declared types. Robust to the multi-line formatting
    sqlite emits and to ``IF NOT EXISTS`` / quoting variations.

    Args:
        schema_text: Raw ``.schema`` (or sqlite_master ``sql``) output.

    Returns:
        List of {name, columns: [{name, type}]} dicts, in declaration order.
    """
    tables: list[dict] = []
    for statement in _split_sql_statements(schema_text):
        normalized = statement.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if not lowered.startswith("create table"):
            continue

        table_name = _extract_table_name(normalized)
        if table_name is None:
            continue

        columns = _extract_columns(normalized)
        tables.append({"name": table_name, "columns": columns})
    return tables


def _split_sql_statements(text: str) -> list[str]:
    """Split a schema dump into statements on top-level semicolons.

    Tracks parenthesis depth so semicolons inside column lists are ignored, and
    skips single/double-quoted string literals.
    """
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
    import re

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
        # Skip table-level constraints, not column definitions.
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
        # A type token that is itself a constraint keyword means "no type".
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


# === OUTPUT FORMATTING ===


def _format_size(num: int) -> str:
    """Format a byte count compactly (B/KB/MB/GB)."""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _emit(action: str, success: bool, result: dict, args: argparse.Namespace) -> None:
    """Print a result in the requested mode and dispatch text formatting."""
    if args.json:
        print(json.dumps({"action": action, "success": success, **result}, indent=2))
        return
    if not success:
        print(f"Error: {result.get('error', result)}", file=sys.stderr)
        if result.get("run_as_denied"):
            print(f"Hint: {result['hint']}", file=sys.stderr)
        return
    _format_text(action, result, args)


def _format_text(action: str, result: dict, args: argparse.Namespace) -> None:
    """Token-efficient text rendering per action (detail under --verbose)."""
    if action == "ls":
        print(f"{result['listed_path']}  ({result['total_entries']} entries)")
        for entry in result["entries"]:
            size = _format_size(entry["size_bytes"]) if entry["kind"] == "file" else ""
            suffix = f"  [{size}]" if size else ""
            link = f" -> {entry['symlink_target']}" if entry.get("symlink_target") else ""
            print(f"  {entry['name']}  ({entry['kind']}){suffix}{link}")
    elif action == "cat":
        if "cache_id" in result:
            print(f"[{result['path']}]  {_format_size(result['size_bytes'])}")
            print(result["note"])
        else:
            if result.get("truncated") and result.get("note"):
                print(f"# {result['note']}", file=sys.stderr)
            print(result["content"])
    elif action == "shared-prefs":
        if "preferences" in result:
            prefs = result["preferences"]
            print(f"{result['file']}  ({result['total_keys']} keys)")
            for key, value in sorted(prefs.items()):
                print(f"  {key} = {value!r}")
        else:
            files = result["files"]
            if not files:
                print(f"No shared_prefs found for {result['package']}")
            else:
                print(f"shared_prefs ({result['total_files']}):")
                for fname in files:
                    print(f"  {fname}")
    elif action == "databases":
        if "tables" in result:
            print(
                f"{result['database']}  ({result['total_tables']} tables, via {result['method']})"
            )
            for table in result["tables"]:
                cols = ", ".join(c["name"] for c in table["columns"])
                print(f"  {table['name']}: {cols}")
            if args.verbose:
                print("\n--- schema ---")
                print(result["schema"])
        else:
            dbs = result["databases"]
            if not dbs:
                print(f"No databases found for {result['package']}")
            else:
                print(f"databases ({result['total_databases']}):")
                for db in dbs:
                    print(f"  {db['name']}  [{_format_size(db['size_bytes'])}]")
    elif action == "export":
        print(f"Exported {result['package']} -> {result['destination']}")
        print(f"  {result['total_files']} files written")
        if args.verbose:
            for path in result["files_written"]:
                print(f"  - {path}")


# === CLI ===


def main() -> None:
    """Parse arguments and dispatch the requested inspection action."""
    parser = argparse.ArgumentParser(
        description="Inspect a DEBUGGABLE Android app's sandbox via 'adb run-as'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Mechanism:
  Uses 'adb shell run-as <package> <cmd>', which the Android platform permits
  ONLY for debuggable (debug-build) apps. Release builds and uninstalled
  packages are reported as 'run-as denied' rather than failing cryptically.

Examples:
  python container.py --package com.example.app --ls
  python container.py --package com.example.app --ls files
  python container.py --package com.example.app --cat shared_prefs/settings.xml
  python container.py --package com.example.app --shared-prefs
  python container.py --package com.example.app --shared-prefs settings --json
  python container.py --package com.example.app --databases
  python container.py --package com.example.app --databases app.db --verbose
  python container.py --package com.example.app --export ./snapshot/
        """,
    )

    parser.add_argument("--package", required=True, help="App package name (required)")
    parser.add_argument("--serial", help="Device serial (auto-detects if omitted)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Show extended detail")

    ops = parser.add_mutually_exclusive_group(required=True)
    ops.add_argument(
        "--ls",
        nargs="?",
        const="",
        metavar="SUBPATH",
        help="List the data dir (optionally a SUBPATH relative to it).",
    )
    ops.add_argument(
        "--cat",
        metavar="FILE",
        help="Print a file (path relative to the data dir; size-capped).",
    )
    ops.add_argument(
        "--shared-prefs",
        nargs="?",
        const=None,
        default=argparse.SUPPRESS,
        metavar="NAME",
        help="List shared_prefs/*.xml, or dump a named prefs file.",
    )
    ops.add_argument(
        "--databases",
        nargs="?",
        const=None,
        default=argparse.SUPPRESS,
        metavar="NAME",
        help="List databases/, or dump the SQLite schema for a named DB.",
    )
    ops.add_argument(
        "--export",
        metavar="DIR",
        help="Write a snapshot (shared_prefs, db listing, file tree) to DIR.",
    )

    args = parser.parse_args()

    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    inspector = ContainerInspector(serial=serial)
    pkg = args.package

    if args.ls is not None:
        success, result = inspector.list_dir(pkg, subpath=args.ls)
        _emit("ls", success, result, args)
    elif args.cat is not None:
        success, result = inspector.cat_file(pkg, args.cat)
        _emit("cat", success, result, args)
    elif hasattr(args, "shared_prefs"):
        success, result = inspector.shared_prefs(pkg, name=args.shared_prefs)
        _emit("shared-prefs", success, result, args)
    elif hasattr(args, "databases"):
        success, result = inspector.databases(pkg, name=args.databases)
        _emit("databases", success, result, args)
    elif args.export is not None:
        success, result = inspector.export(pkg, args.export)
        _emit("export", success, result, args)
    else:  # pragma: no cover - argparse guarantees one op
        parser.error("no operation selected")
        return

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
