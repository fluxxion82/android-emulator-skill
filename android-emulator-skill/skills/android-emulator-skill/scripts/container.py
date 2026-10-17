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
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from common import adb_exec, cli, run_as
from common.cache_utils import ProgressiveCache
from common.device_utils import build_adb_command, quote_for_device_shell, resolve_device_identifier
from common.env_config import env_int

# The shell -T transport and its client guard moved to common.run_as when
# model_inspector became the second script to read app files. Both names are
# bound here: `adb_client_version` is what require_adb_for_reads asks (and what
# a test replaces), `parse_adb_version` is where the tests found it.
from common.run_as import adb_client_version, parse_adb_version  # noqa: F401

# Cap on bytes pulled by --cat before truncation. Guards against dumping a
# multi-megabyte database/blob inline. Override via the ANDROID_EMU_ prefix.
CAT_MAX_BYTES = env_int("ANDROID_EMU_CONTAINER_CAT_MAX_BYTES", 65_536, min_value=1024)

# Threshold above which --cat output is cached for progressive disclosure
# instead of printed inline (kept below CAT_MAX_BYTES so caps still apply).
CAT_CACHE_BYTES = env_int("ANDROID_EMU_CONTAINER_CAT_CACHE_BYTES", 8_192, min_value=512)

# Timeout (seconds) for a single run-as adb invocation.
RUN_AS_TIMEOUT = env_int("ANDROID_EMU_CONTAINER_TIMEOUT", 30, min_value=5)

# The data-dir subdirectories --export lists, in the order it lists them.
_EXPORTED_DIRS = ("shared_prefs", "databases")

# Files are read with `adb shell -T` (common.run_as), and the adb client is
# checked once, before the first read, for a version that honours `-T`.

# Markers adb/run-as emit when run-as is refused. Matched case-insensitively as
# substrings; the platform wording has drifted across API levels.
_RUN_AS_DENIED_MARKERS = (
    "is not debuggable",
    "not debuggable",
    "is unknown",
    "unknown package",
    "run-as: package not found",
    "package not found",
    # Measured, not guessed: the device prints "run-as: package not an
    # application: <pkg>". This entry used to read "is not an application",
    # with an "is" the platform never emits, so a system or release package
    # fell through to a generic "Command failed" instead of the hint below.
    # Recorded as run_as_not_an_application.
    "not an application",
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
            output = self._run_as_bytes(package, ["ls", "-la", target])
        except RunAsDeniedError as exc:
            return False, _denied_result(package, exc)
        except _RunAsError as exc:
            return False, {"error": str(exc)}

        listing = parse_ls_output(output)
        return True, {
            "package": package,
            "data_dir": self.data_dir(package),
            "listed_path": target,
            "entries": listing.entries,
            "total_entries": len(listing.entries),
            **_rejections(listing),
        }

    def cat_file(self, package: str, relpath: str) -> tuple[bool, dict]:
        """Read a file under the data dir via ``run-as cat`` (size-capped).

        The file is read through :meth:`_read_run_as` (``adb shell -T``), so
        binary bytes survive transport and a failed read is a failure. Output is
        capped at CAT_MAX_BYTES; large text is routed through ProgressiveCache.

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
            raw = self._read_run_as(package, ["cat", target])
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
                output = self._run_as_bytes(package, ["ls", "-la", prefs_dir])
            except RunAsDeniedError as exc:
                return False, _denied_result(package, exc)
            except _RunAsError as exc:
                return False, {"error": str(exc)}
            listing = parse_ls_output(output)
            files = [e["name"] for e in listing.entries if e["name"].endswith(".xml")]
            return True, {
                "package": package,
                "shared_prefs_dir": prefs_dir,
                "files": files,
                "total_files": len(files),
                **_rejections(listing),
            }

        filename = name if name.endswith(".xml") else f"{name}.xml"
        target = _join_remote(prefs_dir, filename)
        try:
            raw = self._read_run_as(package, ["cat", target])
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
        pulling the DB bytes via ``run-as cat`` (:meth:`_read_run_as`) to a local temp file and
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
                output = self._run_as_bytes(package, ["ls", "-la", db_dir])
            except RunAsDeniedError as exc:
                return False, _denied_result(package, exc)
            except _RunAsError as exc:
                return False, {"error": str(exc)}
            listing = parse_ls_output(output)
            files = [
                e
                for e in listing.entries
                if e["kind"] == "file" and not _is_sqlite_sidecar(e["name"])
            ]
            sidecars = [e["name"] for e in listing.entries if _is_sqlite_sidecar(e["name"])]
            return True, {
                "package": package,
                "databases_dir": db_dir,
                "databases": [{"name": e["name"], "size_bytes": e["size_bytes"]} for e in files],
                "sidecars": sidecars,
                "total_databases": len(files),
                **_rejections(listing),
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

        shared_prefs/ and databases/ are listed only when the data-dir listing
        holds them. One it does not hold is ``absent``: the app has none, a
        complete answer, and databases.json says so. One it holds whose own
        listing then fails is in ``unlisted_dirs`` with the device's reason,
        and nothing is written for it; the snapshot is incomplete, and main()
        refuses it with what was written. A failed listing used to be taken
        for an empty directory -- exit 0, ``success: true``, ``unread: 0`` --
        and a databases failure was written into databases.json as content.

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

        held = {entry["name"] for entry in listing["entries"]}
        absent = [name for name in _EXPORTED_DIRS if name not in held]
        unlisted: list[dict] = []

        # The prefs file names come from the app. Every destination is checked
        # before anything is created, so a refusal leaves nothing behind.
        prefs: dict = {}
        if "shared_prefs" not in absent:
            prefs_ok, prefs = self.shared_prefs(package)
            if not prefs_ok:
                unlisted.append(self._unlisted(package, "shared_prefs", prefs))
                prefs = {}
        prefs_out = target / "shared_prefs"
        prefs_files = [
            (fname, _host_path_inside(prefs_out, fname)) for fname in prefs.get("files", [])
        ]

        # Before anything is created: a client that cannot read files safely
        # must not leave a half-written snapshot behind its refusal.
        require_adb_for_reads()
        target.mkdir(parents=True)
        written: list[str] = []

        # Top-level file tree.
        tree_path = target / "file_tree.txt"
        tree_lines = [
            f"{e['mode']}  {e['size_bytes']:>10}  {e['name']}" for e in listing["entries"]
        ]
        tree_path.write_text("\n".join(tree_lines) + "\n", encoding="utf-8")
        written.append(str(tree_path))

        # shared_prefs/*.xml — dump each verbatim. A file the listing showed
        # and the read could not deliver is reported, not skipped: the
        # snapshot is incomplete, and saying nothing made it look whole.
        unread: list[dict] = []
        if prefs_files:
            prefs_out.mkdir()
            for fname, dest in prefs_files:
                src = _join_remote(self.data_dir(package), f"shared_prefs/{fname}")
                try:
                    raw = self._read_run_as(package, ["cat", src])
                except (RunAsDeniedError, _RunAsError) as exc:
                    unread.append({"name": fname, "error": str(exc)})
                    continue
                dest.write_bytes(raw)
                written.append(str(dest))

        # databases listing (metadata only — not the binary DBs). A listing that
        # failed writes no file: databases.json holds a listing, never an error.
        dbs: dict = {}
        if "databases" in absent:
            dbs = {
                "package": package,
                "databases_dir": _join_remote(self.data_dir(package), "databases"),
                "absent": True,
                "databases": [],
                "sidecars": [],
                "total_databases": 0,
            }
        else:
            db_ok, dbs = self.databases(package)
            if not db_ok:
                unlisted.append(self._unlisted(package, "databases", dbs))
                dbs = {}
        if dbs:
            db_json = target / "databases.json"
            db_json.write_text(json.dumps(dbs, indent=2), encoding="utf-8")
            written.append(str(db_json))

        # Every listing export read: an entry refused anywhere leaves the
        # snapshot incomplete, and the result says so.
        refused = [
            name for part in (listing, prefs, dbs) for name in part.get("rejected_entries", [])
        ]
        return True, {
            "package": package,
            "destination": str(target),
            "files_written": written,
            "total_files": len(written),
            "rejected": len(refused),
            "rejected_entries": refused,
            "unread": len(unread),
            "unread_entries": unread,
            "absent": absent,
            "unlisted": len(unlisted),
            "unlisted_dirs": unlisted,
        }

    def _unlisted(self, package: str, name: str, failure: dict) -> dict:
        """A directory the data dir holds whose own listing failed, with the device's reason.

        Its listing goes through run-as, which the data-dir listing just used,
        so a "denial" here is not run-as refusing the app: `ls: <dir>:
        Permission denied` matches the denial markers (recorded:
        run_as_ls_shared_prefs_unreadable). The reason is the device's text,
        without the "run-as denied" framing or the debuggable-app hint.
        """
        error = str(failure.get("error") or "the listing failed")
        if failure.get("run_as_denied"):
            error = error.removeprefix(_denial_prefix(package))
        return {"name": name, "path": _join_remote(self.data_dir(package), name), "error": error}

    # === COMMAND EXECUTION ===

    def _run_as(self, package: str, argv: list[str]) -> str:
        """Run ``adb shell run-as <package> <argv...>`` and return stdout as text.

        For output nothing makes a path of (a schema); a listing is parsed from
        :meth:`_run_as_bytes`, where an undecodable name can be refused rather
        than replaced.

        Raises:
            RunAsDeniedError: run-as refused (non-debuggable / missing package).
            _RunAsError: any other command failure.
        """
        return self._run_as_bytes(package, argv).decode("utf-8", errors="replace")

    def _run_as_bytes(self, package: str, argv: list[str]) -> bytes:
        """Run ``adb shell run-as <package> <argv...>`` and return stdout's bytes.

        Bytes, with no universal-newlines translation: ``text=True`` turned a CR
        inside a file name into a line break before parse_ls_output saw it, and
        a crafted name then forged a second ``ls`` line.

        Raises:
            RunAsDeniedError: run-as refused (non-debuggable / missing package).
            _RunAsError: any other command failure.
        """
        quoted = [quote_for_device_shell(a) for a in argv]
        cmd = build_adb_command(
            "shell", self.serial, "run-as", quote_for_device_shell(package), *quoted
        )
        result = self._invoke(cmd)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        _raise_if_denied(package, f"{stdout}\n{stderr}", result.returncode)
        if result.returncode != 0:
            raise _RunAsError(_describe_failure(argv, result.returncode, stdout, stderr))
        return result.stdout

    def _read_run_as(self, package: str, argv: list[str]) -> bytes:
        """Run ``adb shell -T run-as <package> <argv...>`` and return stdout's raw bytes.

        Not ``adb exec-out``, which this used: it exits 0 whatever the device
        command did and delivers the device's stderr on stdout, so a ``cat`` of
        a missing file came back as content -- ``cat: …: No such file or
        directory`` -- and a non-debuggable app's ``run-as`` refusal did too
        (measured on emulator-5554). ``shell -T`` (no pty) keeps stdout's bytes
        as exec-out delivers them -- fixture.db, 32768 bytes, cmp-identical --
        and returns the exit status and stderr apart (recorded:
        run_as_cat_missing).

        Raises:
            RunAsDeniedError / _RunAsError as in :meth:`_run_as`.
            cli.Failure: the adb client is older than ``run_as.MIN_ADB_FOR_READS``,
                or its version cannot be read (see :func:`require_adb_for_reads`).
        """
        require_adb_for_reads()
        result = self._invoke(run_as.read_command(self.serial, package, argv))
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        _raise_if_denied(package, stderr_text, result.returncode)
        if result.returncode != 0:
            raise _RunAsError(stderr_text.strip() or f"exit code {result.returncode}")
        return result.stdout

    def _invoke(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run an adb command for raw bytes; adb's own failures raise typed.

        A missing adb, a hang and a device that is not there used to become
        ``_RunAsError`` -- "Command failed (ls -la ...): adb: device ... not
        found" at exit 1, with no remedy -- and so read as a refusal by the app.
        They are not the app's: they reach common.cli as adb_exec's errors
        (exit 3 for the device), the way every other script reports them.
        """
        return adb_exec.run_argv(cmd, serial=self.serial, timeout=RUN_AS_TIMEOUT)

    def _dump_schema(self, package: str, remote_db: str, name: str) -> tuple[str | None, str]:
        """Return (schema_text, method) or (None, error_message).

        Tries on-device sqlite3 first; falls back to pulling the DB locally.
        The adb client is checked before either, so no read of the database
        happens ahead of it (the sqlite3 query did; Codex review of #29).
        """
        require_adb_for_reads()
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
            raw = self._read_run_as(package, ["cat", remote_db])
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


# === ADB CLIENT ===


def require_adb_for_reads() -> None:
    """Refuse, before any file is read, an adb client too old to honour ``shell -T``.

    Asks through this module's ``adb_client_version`` name (common.run_as's,
    re-exported), so a test that replaces it here is the one consulted.

    Raises:
        cli.Failure: older than ``run_as.MIN_ADB_FOR_READS``, or unreadable.
    """
    run_as.require_adb_for_reads(adb_client_version())


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
            raise RunAsDeniedError(f"{_denial_prefix(package)}{text.strip() or marker}")


def _denial_prefix(package: str) -> str:
    """How a RunAsDeniedError's message starts; the rest is the device's own text."""
    return f"run-as denied for '{package}': "


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


def _describe_failure(argv: list[str], returncode: int, stdout: str, stderr: str) -> str:
    """Compose an error message from a failed command's decoded output."""
    detail = stderr.strip() or stdout.strip() or f"exit code {returncode}"
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


def _host_path_inside(directory: Path, name: str) -> Path:
    """``directory / name`` resolved -- refused unless it lands directly in ``directory``.

    ``name`` was chosen by the app on the device. parse_ls_output already drops
    names that are not plain file names; this is the check at the point the
    name becomes a host path, so a listing that reached export() another way
    cannot write outside the snapshot either.

    Raises:
        cli.Failure: The name resolves anywhere but ``directory`` itself.
    """
    dest = (directory / name).resolve()
    if dest.parent != directory.resolve():
        cli.fail(
            f"refusing to export the device entry {name!r}: as a host path it resolves "
            f"to {dest}, outside {directory}",
            "Nothing was written. The app holds a file whose name is not a plain file "
            "name; inspect it with --ls, and rename or remove it on the device first.",
        )
    return dest


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

# toybox `ls -l` -- what every caller's `run-as <pkg> ls -la` runs -- prints
# mode, links, owner, group and size separated by runs of spaces, the timestamp
# as two columns, `%F %H:%M` (recorded: run_as_ls_symlink_space), then ONE space
# and the name exactly as printed: it may hold spaces, start or end with one, or
# look like a date.
_LS_ENTRY = re.compile(
    r"(?P<mode>\S+) +(?P<links>\S+) +(?P<owner>\S+) +(?P<group>\S+) +(?P<size>\S+) +"
    r"(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2}) (?P<clock>[0-9]{2}:[0-9]{2}) (?P<name>.+)"
)

# A name is chosen by the app on the device, so it is untrusted input to the
# host: export() turns it into a host path. None of these belongs in a single
# directory entry -- `/` and NUL cannot occur in one at all, so seeing them means
# the line is not what it claims; `\` is a separator on a Windows host; a
# newline is how a crafted name forges a second `ls` line.
_NAME_FORBIDDEN = frozenset("/\\\0\n\r")
# A symlink's target is a device path, so `/` is expected there; a CR or NUL is
# not, and the target is printed to the caller.
_TARGET_FORBIDDEN = frozenset("\0\n\r")


class LsListing(NamedTuple):
    """What an ``ls -la`` listing held: the entries read, and the ones refused.

    ``rejected`` holds each refused name -- or the whole line, when its columns
    could not be placed -- as ``repr()``, so a control character in it is
    shown escaped rather than reaching a terminal or a log raw. Every caller
    reports them: dropping them silently answered "nothing there" for
    "refused to read it".
    """

    entries: list[dict]
    rejected: list[str]


def parse_ls_output(output: bytes | str) -> LsListing:
    r"""Parse ``ls -la`` output into structured entries.

    Reads toybox's layout by position -- every caller runs ``run-as <pkg> ls
    -la`` on the device, and the device's ``ls`` is toybox (recorded:
    ``run_as_ls_symlink_space``):

        drwx------ 4 u0_a123 u0_a123 4096 2024-01-02 03:04 shared_prefs
        -rw------- 1 u0_a123 u0_a123  220 2024-01-02 03:04 prefs.xml
        lrwxrwxrwx 1 root    root       11 2024-01-02 03:04 lib -> /data/app

    The name is everything after the two timestamp columns, so a file named
    ``2026-10-16``, ``03:04`` or ``May`` keeps its name. (Skipping every
    date-looking token instead -- and, before that, coreutils' month names and
    bare years -- made such names vanish from the listing.)

    Every line is an entry, refused, or one of three skips: a blank line, the
    ``total N`` line, and the directory's own ``.`` / ``..``. Refused, and
    returned in ``rejected`` (never dropped silently):

    - a line that is not UTF-8 -- decoding it with replacement would hand on
      a name that is not the file's, and the read of it would fail;
    - a line that is not that layout: an unknown mode (``??????????``), a
      device node, whose size is ``major, minor`` (measured: ``1,   3``) and
      takes two columns, or any other text;
    - a name that is not a plain file name (see ``_NAME_FORBIDDEN``), or a
      symlink target holding a CR or NUL.

    The names come from the app, and :meth:`ContainerInspector.export` makes
    host paths of them.

    Lines are split on ``\n`` only -- what ``ls`` separates entries with --
    not ``str.splitlines``, which also breaks on ``\r``, ``\x1c``-``\x1e``,
    ``\x85`` and ``\u2028``/``\u2029``, any of which a name may hold and
    would then forge a line without a newline. (The transport keeps a CR a CR
    for the same reason; see ``_run_as``.) A name holding a real newline still
    can: ``ls -1b`` or ``find -print0`` would make names unambiguous, and is
    not done here.

    Args:
        output: Raw ``ls -la`` stdout: bytes from the device, or text already
            decoded (the recorded fixtures).

    Returns:
        :class:`LsListing`: ``entries`` as dicts {name, mode, kind, size_bytes,
        owner, group, symlink_target}, and the ``rejected`` names.
    """
    raw = output.encode("utf-8") if isinstance(output, str) else output
    entries: list[dict] = []
    rejected: list[str] = []
    for raw_line in raw.split(b"\n"):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            rejected.append(repr(raw_line))
            continue
        if not line.strip() or line.startswith("total "):
            continue
        match = _LS_ENTRY.fullmatch(line)
        if match is None or not _looks_like_mode(match["mode"]):
            rejected.append(repr(line))
            continue
        kind = _kind_from_mode(match["mode"])
        name, symlink_target = _extract_name(match["name"], kind)
        if name in (".", ".."):
            continue
        if not _is_plain_file_name(name):
            rejected.append(repr(name))
            continue
        if symlink_target is not None and _TARGET_FORBIDDEN.intersection(symlink_target):
            rejected.append(repr(match["name"]))
            continue

        entries.append(
            {
                "name": name,
                "mode": match["mode"],
                "kind": kind,
                "size_bytes": int(match["size"]) if match["size"].isdigit() else 0,
                "owner": match["owner"],
                "group": match["group"],
                "symlink_target": symlink_target,
            }
        )
    return LsListing(entries, rejected)


def _rejections(listing: LsListing) -> dict:
    """The refused entries of a listing, as a result carries them (see LsListing)."""
    return {"rejected": len(listing.rejected), "rejected_entries": listing.rejected}


def _is_plain_file_name(name: str) -> bool:
    """True if ``name`` is one directory entry that is safe to join to a host path."""
    return name not in ("", ".", "..") and not _NAME_FORBIDDEN.intersection(name)


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


def _extract_name(rest: str, kind: str) -> tuple[str, str | None]:
    """Split the name column into the name and, for a symlink, its target.

    Args:
        rest: Everything after the time column. toybox prints a name bare
            (unquoted, unescaped) and a symlink as ``name -> target``.
        kind: Entry kind (symlinks split on " -> ").

    Returns:
        (name, symlink_target) — target is None for non-symlinks.
    """
    if kind == "symlink" and " -> " in rest:
        # Not stripped: a CR or space at either end belongs to the name.
        name, _, symlink_target = rest.partition(" -> ")
        return name, symlink_target
    return rest, None


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


# What to do about a failed inspection whose result names no remedy of its own
# (a run-as refusal carries its hint; an incomplete listing, its own remedy).
CONTAINER_REMEDY = (
    "Fix what the error names, then retry; `container.py --package <pkg> --ls` shows "
    "what the app's data directory holds."
)


def _emit(action: str, result: dict, args: argparse.Namespace) -> None:
    """Print a successful result in the requested mode."""
    if args.json:
        print(json.dumps({"action": action, "success": True, **result}, indent=2))
        return
    _format_text(action, result, args)


def _refuse(action: str, result: dict, args: argparse.Namespace) -> None:
    """Report a failed or incomplete inspection through common.cli (exit 1).

    It was printed here -- ``{"action", "success": false, "error"}`` with no
    remedy under --json, a ``Hint:`` line on stderr otherwise -- and the exit
    decided by ``main``. Now it is the shared refusal; what the inspection did
    read (an incomplete listing's entries, a denial's package) rides along in
    the object, and in text mode the partial answer is still printed first.

    Raises:
        cli.Failure: always.
    """
    if not args.json and (result.get("rejected") or result.get("unread") or result.get("unlisted")):
        # A partial answer: what was read, then why it is incomplete.
        _format_text(action, result, args)
    remedy = result.get("remedy") or result.get("hint") or CONTAINER_REMEDY
    details = {key: value for key, value in result.items() if key not in ("error", "remedy")}
    cli.fail(
        str(result.get("error") or f"{action} failed"),
        remedy,
        details={"action": action, **details},
    )


def _report_rejections(success: bool, result: dict) -> tuple[bool, dict]:
    """Make an incomplete answer a failure that still carries the answer.

    Incomplete means an entry was refused (see LsListing), a listed file
    could not be read (export's ``unread``), or a directory the data dir
    holds could not be listed (export's ``unlisted``). Exit 1 even when only
    some were: the listing or snapshot is incomplete, and the exit status is
    the one signal a caller cannot skip. What was read stays in the result,
    as a failing batch keeps its summary. Everything refused and nothing read
    is the same failure -- never "No shared_prefs found".
    """
    refused, unread = result.get("rejected", 0), result.get("unread", 0)
    unlisted = result.get("unlisted", 0)
    if not success or not (refused or unread or unlisted):
        return success, result
    reasons = []
    if refused:
        shown = ", ".join(result["rejected_entries"][:3]) + (", ..." if refused > 3 else "")
        reasons.append(
            f"refused {refused} entr{'y' if refused == 1 else 'ies'} the app named with "
            f"bytes that are not UTF-8 or a character no plain file name carries (/, \\, "
            f"NUL, CR, LF), or in a layout ls does not print: {shown}"
        )
    if unread:
        shown = ", ".join(
            f"{entry['name']!r} ({entry['error']})" for entry in result["unread_entries"][:3]
        ) + (", ..." if unread > 3 else "")
        reasons.append(f"could not read {unread} listed file(s): {shown}")
    if unlisted:
        shown = ", ".join(
            f"{entry['name']}/ ({entry['error']})" for entry in result["unlisted_dirs"]
        )
        reasons.append(
            f"could not list {unlisted} director{'y' if unlisted == 1 else 'ies'} the data dir "
            f"holds: {shown}"
        )
    remedy = ["Whatever could be read is shown."]
    if refused or unread:
        remedy.append(
            "Inspect the named entries on the device (`adb shell run-as <package> ls -lab`); "
            "rename or remove a refused one, and retry an unread one, to read them here."
        )
    if unlisted:
        remedy.append(
            "Find why the named directory cannot be listed (`adb shell run-as <package> "
            "ls -la <directory>`) and fix it."
        )
    if "destination" in result:
        remedy.append(
            f"What was exported is in {result['destination']}; export again to a destination "
            "that does not exist yet."
        )
    return False, {**result, "error": "; ".join(reasons), "remedy": " ".join(remedy)}


def _print_refused(result: dict, args: argparse.Namespace) -> None:
    """The refused names (escaped) and the unread files, under --verbose."""
    if args.verbose:
        for name in result.get("rejected_entries", []):
            print(f"  refused: {name}")
        for entry in result.get("unread_entries", []):
            print(f"  unread: {entry['name']!r} ({entry['error']})")
        for entry in result.get("unlisted_dirs", []):
            print(f"  not listed: {entry['name']}/ ({entry['error']})")


def _format_text(action: str, result: dict, args: argparse.Namespace) -> None:
    """Token-efficient text rendering per action (detail under --verbose)."""
    refused = f", {result['rejected']} refused" if result.get("rejected") else ""
    if action == "ls":
        print(f"{result['listed_path']}  ({result['total_entries']} entries{refused})")
        for entry in result["entries"]:
            size = _format_size(entry["size_bytes"]) if entry["kind"] == "file" else ""
            suffix = f"  [{size}]" if size else ""
            link = f" -> {entry['symlink_target']}" if entry.get("symlink_target") else ""
            print(f"  {entry['name']}  ({entry['kind']}){suffix}{link}")
        _print_refused(result, args)
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
            if not files and not refused:
                print(f"No shared_prefs found for {result['package']}")
            else:
                print(f"shared_prefs ({result['total_files']}{refused}):")
                for fname in files:
                    print(f"  {fname}")
            _print_refused(result, args)
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
            if not dbs and not refused:
                print(f"No databases found for {result['package']}")
            else:
                print(f"databases ({result['total_databases']}{refused}):")
                for db in dbs:
                    print(f"  {db['name']}  [{_format_size(db['size_bytes'])}]")
            _print_refused(result, args)
    elif action == "export":
        print(f"Exported {result['package']} -> {result['destination']}")
        unread = f", {result['unread']} unread" if result.get("unread") else ""
        count = result.get("unlisted", 0)
        unlisted = f", {count} director{'y' if count == 1 else 'ies'} not listed" if count else ""
        print(f"  {result['total_files']} files written{refused}{unread}{unlisted}")
        for name in result.get("absent", []):
            print(f"  {name}/: absent (the data dir holds none)")
        if args.verbose:
            for path in result["files_written"]:
                print(f"  - {path}")
        _print_refused(result, args)


# === CLI ===


def main(argv: list[str] | None = None) -> None:
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

    args = parser.parse_args(argv)

    serial = resolve_device_identifier(args.serial)

    inspector = ContainerInspector(serial=serial)
    pkg = args.package

    if args.ls is not None:
        action, (success, result) = "ls", inspector.list_dir(pkg, subpath=args.ls)
    elif args.cat is not None:
        action, (success, result) = "cat", inspector.cat_file(pkg, args.cat)
    elif hasattr(args, "shared_prefs"):
        action = "shared-prefs"
        success, result = inspector.shared_prefs(pkg, name=args.shared_prefs)
    elif hasattr(args, "databases"):
        action = "databases"
        success, result = inspector.databases(pkg, name=args.databases)
    elif args.export is not None:
        action, (success, result) = "export", inspector.export(pkg, args.export)
    else:  # pragma: no cover - argparse guarantees one op
        parser.error("no operation selected")
        return

    success, result = _report_rejections(success, result)
    if not success:
        _refuse(action, result, args)
    _emit(action, result, args)
    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
