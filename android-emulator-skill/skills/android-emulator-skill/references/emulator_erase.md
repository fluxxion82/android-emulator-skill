# emulator_erase.py

Factory reset AVDs

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Wipe user data without deleting AVD
- Preserve AVD configuration
- Reads the AVD home directly (`$ANDROID_AVD_HOME`, else
  `$ANDROID_SDK_HOME/.android/avd`, else `~/.android/avd`); no SDK tool is
  consulted. An AVD home that does not exist is an error naming the path,
  exit 1 — not "No AVDs found"
- Batch erase with `--all`; `--verify` polls the AVD on disk until the wipe
  lands, bounded by `--timeout` (default 90s, `ANDROID_EMU_ERASE_TIMEOUT`)
- Refuses to erase unless every attached emulator has been identified and
  none of them is this AVD. A console query that failed, or an emulator not
  yet in state `device`, is "unknown" and refuses, naming the serial, its
  state and the remedy — `--force` erases anyway
- Every failure is the shared refusal: `{"success": false, "error": ...,
  "remedy": ...}` under `--json`, `Error:` / `Remedy:` on stderr otherwise,
  exit 1. `--all` is a batch (see **Failures** in SKILL.md): its summary and `items`,
  one per AVD with `avd`, `message`, `success`, `error`, `remedy`, and the
  refusal's `error` / `remedy` on top when any AVD failed (exit 1).
  **Breaking in v0.9.0:** `results` is now `items`
- **Snapshots are kept**: a successful erase prints "snapshots kept; use
  snapshot.py --delete `<name>` to remove them", because a later
  `snapshot.py --load` can otherwise undo the factory state
