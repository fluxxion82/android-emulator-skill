# push_notification.py

Post into the shade, and read back what is posted

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Rescoped in v0.6.0 to what adb can actually do. It no longer claims to
  send an app's notifications: `--post` posts via `cmd notification post`,
  and the result is owned by **com.android.shell** on channel `shell_cmd`,
  *not* by the app under test. The app's own channel, receiver and
  rendering are therefore not exercised. It is still the way to drive a
  NotificationListenerService, the shade UI, or an agent that reacts to a
  notification. `--tag` is the title and key, `--text` the body.
- `--list` reads back what is really posted, by any package — the check
  that a notification exists rather than that a command exited 0.
  `--expect-package PKG` turns that read-back into an exit status.
- A failed post, read-back or permission change is the shared refusal
  (`{"success": false, "error": ..., "remedy": ...}` under `--json`,
  carrying what it found), exit 1; a device adb cannot reach is exit 3
- `--grant-permission` / `--revoke-permission` toggle `POST_NOTIFICATIONS`
  (API 33+) on `--package`, for exercising the runtime-permission path.
  The change is read back from user 0's `User 0:` section of `dumpsys
  package`, by the same rule as `privacy_manager`: no section, or one
  reading `installed=false` (where a grant still shows `granted=true`),
  is a refusal with the `pm install-existing --user 0 <pkg>` remedy.
- ⚠️ It cannot deliver into an app's own FCM handler — no adb path reaches
  that, because the c2dm receiver is protected by a permission held by
  Play services, not by the shell user. Send through FCM instead. Channels
  cannot be listed either: `cmd notification` has no `list channels`
  subcommand, which is why the old `--list-channels` never worked.
