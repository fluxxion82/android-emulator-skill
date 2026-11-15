# emulator_shutdown.py

Gracefully shutdown emulators

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Shutdown by serial number. Emulators only: a serial that is not an
  `emulator-NNNN` is refused before any adb command runs, so this never powers off an
  attached phone or tablet
- `--name AVD` resolves the AVD to a serial. If some emulator could not be
  queried — or the emulators could not be listed at all — that is reported
  as itself and exits non-zero, never as "No running emulator found", which
  is a confident negative over a device nobody managed to ask
- Failures are the shared refusal: `{"success": false, "error": ...,
  "remedy": ...}` under `--json`, `Error:` / `Remedy:` on stderr otherwise,
  exit 1 (3 when adb says the device is not there)
- Optional verification of shutdown completion
- Batch shutdown operations (running emulators only). `--all` is a batch
  (see **Failures** in SKILL.md): `{"success", "action", "success_count", "fail_count",
  "items"}`, one item per emulator with `serial`, `message`, `success`,
  `error`, `remedy` (it reported counts only); one that vanished before its
  kill is that item's failure and the rest still stop. Exit 1 when any
  failed
