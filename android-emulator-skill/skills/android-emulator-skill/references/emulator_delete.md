# emulator_delete.py

Delete AVDs permanently

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Delete by AVD name, or in batch with `--all` / `--old N` (`--yes` skips the prompt)
- List available AVDs
- Every mode reports a missing or failing `avdmanager` the same way: exit 1
  with the `cmdline-tools` remedy, never "No AVDs deleted" at exit 0
- `--old N` keeps the newest N by config modification time -- the mtime of
  `<name>.avd/config.ini`, which the emulator rewrites when the AVD's
  configuration changes (it was the `.avd` directory's, which moves with
  any snapshot or lock file). That is a proxy for last use, not a record
  of it: an AVD booted without a configuration change keeps its old mtime.
  The prompt and the text summary say "by config modification time";
  `--json` carries `ranked_by: "config modification time"`
- An AVD whose `config.ini` cannot be found fails the whole ranking
  (exit 1) rather than sorting oldest and being deleted. The AVD home is
  `$ANDROID_AVD_HOME`, else `$ANDROID_SDK_HOME/.android/avd`, else
  `~/.android/avd`
- `N` must be at least 1: `--old 0` keeps nothing, so it is rejected as a
  usage error (exit 2) pointing at `--all --yes`
- Without `--yes` the question goes to stderr and the answer is read from
  stdin; anything but `yes` (including end of input) deletes nothing and is
  a refusal in every mode — `deletion cancelled`, exit 1 — so `--all` and
  `--old` no longer answer a "no" with an empty batch at exit 0
- Failures answer `{"success": false, "error": ..., "remedy": ...}` under
  `--json` — including a `--name` that is not listed, and a delete
  avdmanager refused or did not finish. `--all` / `--old` are batches (see
  **Failures** in SKILL.md): the summary (`action`, `succeeded`, `failed`, `total`,
  and for `--old` `keep_count` and `ranked_by`) and `items`, one per AVD
  with `name`, `message`, `success`, `error`, `remedy` -- always, not only with `--verbose`, where they were
  `results`. **Breaking in v0.9.0.** Exit 1 with `error` / `remedy` on top
  when any AVD failed
