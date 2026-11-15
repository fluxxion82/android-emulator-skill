# emulator_selector.py

Suggest the best AVD (ranked by running → recently used → latest API → common models); list or boot one.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- A host with no AVDs ranks nothing and exits 0; a missing or failing
  `emulator` binary exits 1 with the remedy instead of an empty ranking
  (`{"success": false, "error": ..., "remedy": ...}` under `--json`), and
  so does a `--boot` that is refused
- An emulator that will not say which AVD it is gets no "currently running"
  bonus, but is listed with its state (`unidentified_emulators` under
  `--json`, a warning on stderr otherwise) so the ranking never claims a
  completeness it does not have. If the emulators cannot be listed at all,
  the ranking still runs — the AVDs on disk are real — and says so
  (`warnings` under `--json`). The same goes for an AVD whose `config.ini`
  cannot be read: it is ranked without its API level and device, and named
  in `warnings` (and on stderr) with the path that was tried
