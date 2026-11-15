# snapshot.py

Save/load/delete emulator snapshots (`adb emu avd snapshot`). A ~2s state reset in place of a 60s reboot. Emulator-only.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- A failed load is reported as a failure, which is the whole point: `adb emu` exits 0 even when it answers `KO`, so a restore that did not happen would otherwise be reported as success and the next test would run against unknown state.
