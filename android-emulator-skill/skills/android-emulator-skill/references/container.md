# container.py

Inspect a **debuggable** app's sandbox via `adb shell run-as` (fails clearly on release apps).

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- List/read files, dump `shared_prefs` XML, list databases and dump SQLite schema (Room == SQLite), export a snapshot
- File names come from the app, so they are untrusted. A directory is
  listed with `find -H <dir> -maxdepth 1 -printf` (toybox; measured on
  0.8.11, Android 15): six NUL-terminated fields per entry — mode, owner,
  group, size, symlink target, name — read as bytes. NUL is the one byte a
  name cannot hold, so a name holding a newline, a CR or a whole forged
  line is one name: it can no longer add or hide an entry, as it could
  through `ls -la`. The directory's own record comes first, so a path that
  is a file is refused as not a directory, and an answer that is not whole
  records (empty, cut short, or another tool's) is refused whole — never
  read as an empty directory, never retried with `ls`. Refused per entry: a
  name that is not UTF-8 or not a plain file name (`/`, `\`, NUL, CR, LF,
  `.`/`..`), a field find does not print, and a symlink target that is
  empty, not UTF-8, or holds a LF, CR or NUL. A refused entry is counted
  in `rejected` and named, escaped, in `rejected_entries` (`--verbose`
  prints them), and the answer exits 1 with what could be read still shown
  — all refused is never "No shared_prefs found"
- Two limits of that listing. It is only as complete as adb's exit status
  says: the records carry no end marker, so an answer cut after a complete
  record at exit 0 reads as a directory with fewer entries (a cut adb or
  find reports, a non-zero exit, is refused). And `-H` follows the named
  path's own symlink to any directory the app's uid may search, inside its
  data dir or not — `--ls files/x`, where the app linked `x` elsewhere,
  lists that directory's names and modes. A listing reads no file contents;
  `--export`'s reads of the files a listing names follow their symlinks
  too, and do not skip FIFOs or other special files (as before this
  listing; v0.9.0)
- Requires adb 1.0.36 or later (platform-tools 24+), checked once with
  `adb version` before the first file read: an older client does not
  refuse `shell -T` but forwards it to the device and exits 0 after the
  error, so reads would take the error for the file. Older, or a banner
  with no version, is refused (exit 1) and nothing is read or written
- Files are read with `adb shell -T`, so a failed read is a failure: `--cat`
  of a missing file exits 1 (over `exec-out` it answered with the error text
  as the content). `--cat` reads the whole file and caps its output at
  `ANDROID_EMU_CONTAINER_CAT_MAX_BYTES` (64 KiB), cut on a character: a
  UTF-8 file whose output cap falls inside a character is `text`, ending
  at its last whole character, with `truncated: true`; any invalid byte
  before the cut, or a whole file that ends mid-character, is `binary`. `--export`
  counts a listed file it could not read in
  `unread` / `unread_entries` and exits 1, and refuses (exit 1, nothing
  written) a name that would land outside the snapshot
- `--export` lists `shared_prefs/` and `databases/` only when the data dir
  holds them. One it does not hold is named in `absent` — the app has none,
  a complete export at exit 0, and `databases.json` says `"absent": true` —
  but only when that listing refused no entry: a refused entry may be the
  directory, so the export is refused first (`presence_unknown`, exit 1,
  nothing written).
  One it holds whose own listing fails (unreadable, or gone since) is in
  `unlisted` / `unlisted_dirs` with the device's reason; nothing is written
  for it, and the export exits 1 naming what was written and where (a
  failed listing used to read as an empty directory, at exit 0)
- A failed or incomplete inspection is the shared refusal (`{"success":
  false, "error": ..., "remedy": ...}` under `--json`, carrying what was
  read; a run-as denial's hint is its remedy). No adb, a hang, or a device
  that is not there is adb's own error (exit 3 for the device), not "the
  command failed" at exit 1
- A run-as denial (debug-build remedy) is only a refusal run-as itself
  printed: the FIRST line of stderr, at a non-zero exit, is the whole
  refusal the device prints for THIS package (`run-as: package not
  debuggable: <pkg>`, `package not an application`, `unknown package`; all
  recorded), and one bounded `run-as <pkg> id` (which names no file) does
  not run as the app. A device error during that probe is the device's
  (exit 3). A path the device refuses once run-as has run — `find: <dir>:
  Permission denied`, a missing file, a file whose name holds a newline
  and a `run-as: …` line of its own — is that path's failure, with an
  `adb shell run-as <pkg> ls -ld <path>` check as its remedy. A listing
  that fails at an entry — `find: '<dir>/<entry>': Permission denied`,
  when the directory has its r bit but not its x bit — says it failed at
  that entry (named escaped) and that a directory needs both bits, not
  that the directory itself was refused. A listing
  find refuses without naming a path (`find: bad arg …`, `find: bad
  -printf …`, as this toybox answers an option or escape it lacks) is
  find's failure, not the directory's: its remedy says the device's find
  cannot list this way. An older toybox is not measured. An older
  release's refusal wording is not recorded, and reads as the failure
- A `--cat` whose text is more than `ANDROID_EMU_CONTAINER_CAT_CACHE_BYTES`
  (8,192 bytes of UTF-8; a byte count, not a character count) is not
  printed: it is cached for an hour and the answer names the id
  (`Full content cached as '<id>'`, `cache_id` under `--json`).
  `--get-cached ID` prints that content whole — no further cap, no
  device, no `--package` — exactly as cached in text mode, and with the
  `--cat` fields (`path`, `size_bytes`, `truncated`, `content`) under
  `--json`. An unknown or expired id, or one another script cached (a
  build id), is refused (exit 1); a corrupt
  entry is refused as corrupt, naming the file, which is kept
