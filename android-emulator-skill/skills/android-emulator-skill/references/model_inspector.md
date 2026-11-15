# model_inspector.py

Inspect Android persistence: Room annotations from source, Room exported-schema JSON, and live SQLite schema via `run-as` (Room == SQLite).

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- SCHEMA reads the JSON Room's compiler exports (`room.schemaLocation`, or
  the Room Gradle plugin's `schemaDirectory`; a file, the database's
  directory, or the schema directory itself, where the highest version is
  read): each table's fields, primary key, indices and foreign keys,
  `fts_version` for an `@Fts4` table, and the database's `views`. The
  export names tables and views, never the Kotlin classes behind them, so
  `--raw NAME` takes a table or view name — every name the listing prints
  — and a refusal lists the names the file holds. A column's default
  value, `autoGenerate`, an index's order and an FTS table's `ftsOptions`
  and `contentSyncTriggers` are in the `--raw` block only. JSON that is
  not shaped as Room exports it (`{}`, a list, a `tableName` that is
  missing or not text) is refused, exit 1, naming the place
  (`database.entities[0].tableName is missing`), with or without `--raw`.
  So is JSON that `--raw` could not print back as JSON: `NaN` or
  `Infinity`, a key named twice in one object, nesting far deeper than
  Room writes.
  (Tested on a schema Room 2.8.5 exported, recorded with the Gradle
  fixtures. Before that it read a class name Room never wrote: every table
  printed as `users (?)`, `--raw <ClassName>` found nothing, and a
  `@DatabaseView` was not reported at all.)
- LIVE reads the database with `adb shell -T` (common/run_as.py), after the
  same adb 1.0.36 check as container.py. It used `adb exec-out`, which
  exits 0 on a missing file and hands back the error text as the bytes, so
  a database that was not there was reported as "not a valid SQLite
  database"; now the device's reason is the error, exit 1
- Failures in every mode are the shared refusal (`{"success": false,
  "error": ..., "remedy": ...}` under `--json`; a run-as denial's hint is
  its remedy)
- A run-as denial is decided in common/run_as.py by the one rule
  container.py uses: the first line of stderr must be the whole refusal
  the device prints for THIS package (`run-as: package not debuggable:
  <pkg>`, `package not an application`, `unknown package`; all recorded),
  and one bounded `run-as <pkg> id` must not run as the app. An unreadable
  `databases/` (`find: <dir>: Permission denied`), or a database whose name
  holds a newline or CR and a `run-as: …` line, is that path's failure,
  with an `ls -ld` check as its remedy, not "install a debug build"
- LIVE lists `databases/` as container.py lists a directory
  (common/run_as.py: toybox find, NUL-terminated fields, read as bytes).
  It was `ls -1` in text mode, where `x\ry.db` was two databases, `x` and
  `y.db`, at exit 0; then `ls -la` split on newlines, where `fixture.db` +
  LF + a whole `ls -la` line listed `ghost.db`, a database the app does
  not have, at exit 0. Either name is now one name, refused and named in
  `rejected_entries`, and the listing exits 1 with what it read
  (`databases (0, 1 refused)`, never "No databases found")
