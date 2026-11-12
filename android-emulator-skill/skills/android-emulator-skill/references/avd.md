# avd.py

One entry point for the emulator/AVD lifecycle; routes on the question being asked.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- `avd.py list` → `device_list.py`; `pick` → `emulator_selector.py`; `create` → `emulator_create.py`; `start` → `emulator_boot.py`; `stop` → `emulator_shutdown.py`; `reset` (wipe data, keep the AVD) → `emulator_erase.py`; `delete` (remove the AVD) → `emulator_delete.py`.
- `reset` and `delete` are separate verbs because they destroy different things, and a near-miss between them is asked about rather than guessed.
- Arguments are passed through verbatim, so each verb takes the full flag set of the script it delegates to — including its own confirmation flag (`delete --yes`, `reset --force`) — and those scripts remain callable unchanged.
