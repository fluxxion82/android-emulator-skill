# emulator_boot.py

Boot emulators with optional readiness verification

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Boot by AVD name
- Wait for device ready with timeout
- Batch boot operations
- Headless mode support
- `--list-avds` reports an empty list (`No AVDs found`, or `{"avds": []}`)
  and exits **0** only when the emulator ran and this host defines no AVDs.
  A missing or failing `emulator` binary is an error naming where it was
  looked for, and exits 1 (`{"success": false, "error": ..., "remedy": ...}`
  under `--json`) — "no AVDs" and
  "could not look" are different answers. `--all` reports the same failure
  the same way
- `--all` is a batch (see **Failures** in SKILL.md): one object, `{"success", "action",
  "succeeded", "failed", "total", "items"}`, one item per AVD with `avd`,
  `message`, `success`, `error`, `remedy`; exit 1 with `error` / `remedy`
  on top when any AVD failed. **Breaking in v0.9.0:** `results` is now
  `items`
- Refuses to boot while any attached emulator cannot say which AVD it is
  (a non-`device` state, or a console that will not answer), or while the
  emulators cannot be listed at all: it may be this AVD, and a second
  instance of one AVD corrupts its userdata. The refusal names the serial,
  its state, and says to terminate the stale emulator *process* — a console
  that is not answering cannot be shut down through one
- Failures — a refused or failed boot, a missing `--avd` — are the shared
  refusal: `{"success": false, "error": ..., "remedy": ...}` under `--json`,
  `Error:` / `Remedy:` on stderr otherwise, exit 1. A successful boot under
  `--json` answers `{"success": true, ...}` (it reported a defect, exit 1,
  before v0.8.0)
- `--all` boots every defined AVD in the background, without the readiness wait
