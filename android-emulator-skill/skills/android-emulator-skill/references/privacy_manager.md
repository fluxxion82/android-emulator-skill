# privacy_manager.py

App permission management

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Grant/revoke permissions
- List app permissions
- Support for 20+ permission types
- Batch operations: `--grant` / `--revoke` / `--reset` take a
  comma-separated list and answer one object, `{"success", "action",
  "items"}` (plus `test_trail`), one item per permission with
  `permission`, `message`, `success`, `error`, `remedy`; exit 1 with
  `error` / `remedy` on top when any failed, 3 when the device went (see
  **Failures** in SKILL.md). **Breaking in v0.9.0:** it was `{"action", "results"}`
- `--user <id>` acts on one Android user (a secondary user or a work
  profile): the id goes into `pm grant/revoke --user`, and the read-back
  and `--list` read that user's `User <id>:` section of `dumpsys package`
  (under the package, or under `Shared users:` for a shared-uid app).
  Without it both name user 0. The id must be one `pm list users` names (a
  grant for a missing user exits 0 silently, so it is checked first);
  `--user all` / `current` are refused. A user the app is not installed
  for fails with the `pm install-existing --user <id>` remedy, and a
  read-back with no section for the user fails rather than reading
  another user's state.
