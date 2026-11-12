# app_state_capture.py

Complete debugging snapshots

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Capture screenshot + UI hierarchy + logs + app info
- Create timestamped snapshots, by default under
  `~/.android-emulator-skill/cache/app-state-snapshots/` (never the working
  directory); the snapshot's path is printed, and is `snapshot_path` under `--json`
- Logs are the app's own, read from logd's records (`logcat -B`) by the
  same rule as `log_monitor --app`: the running pid (uid checked with
  `ps`) plus each pid system_server's own `Start proc` / `Killing` record
  names with the package's uid, inside that process's life -- a pid
  reused by another process is not the app's outside it -- and only
  records logd stamped with the package's uid. A crash before a restart
  is kept, by its `Start proc`. Not evidence: `has died` (no uid; it only
  ends a life), AndroidRuntime's `Process: <pkg>, PID:` (any app can write
  it), any line an app's tag or message could forge. Errors and warnings
  are counted from the priority column, lines split at `\n` only, and a
  message's line-ending and terminal controls are saved escaped (`\r` as
  `\\r`), so `app-logs.txt` reads back as the lines that were counted. A
  dump that ends inside a record, or does not finish in 60 s, asks `adb
  get-state` again, as `log_monitor` does: gone is exit 3 with what arrived;
  still there, the cut or the timeout fails the logs (exit 1). The cut is
  judged before adb's exit status, which a transport loss also sets (255).
  An app with no process in the window fails the logs rather than
  returning the whole device's
- Lines of the package's uid left out for want of evidence (a crash whose
  start is older than `--logs`) are counted and named: `logs.attribution`
  (`complete: false`, `unresolved_lines`, `unresolved_pids`) in the
  summary, `log_attribution` under `--json`, a line in `summary.md` and in
  the text output, and a stderr note
- A device that becomes unavailable during the capture is exit 3, as
  everywhere else, with what did arrive under `partial`
- All-in-one debugging artifact

## Example: debugging a failure

```bash
# 1. Capture complete state (prints where the snapshot went)
python3 "$SKILL_DIR/scripts/app_state_capture.py" --package com.myapp

# 2. Monitor logs in real-time
python3 "$SKILL_DIR/scripts/log_monitor.py" --app com.myapp --severity error,warning --follow

# 3. Check accessibility issues
python3 "$SKILL_DIR/scripts/accessibility_audit.py" --output audit-reports/ --verbose
```
