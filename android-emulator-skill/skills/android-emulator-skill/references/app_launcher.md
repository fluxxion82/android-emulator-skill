# app_launcher.py

App lifecycle management

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Launch apps by package name. `--launch` **waits for the activity to be
  displayed** (`am start -W`) and fails if the system reports anything but
  `Status: ok`, so the next command maps the app's screen rather than
  whatever was still in front. The message reports the activity that came up
  and how long it took.
- Terminate apps
- Install/uninstall APKs
- Deep link navigation. `--open-url` waits the same way `--launch` does and
  fails when the intent resolves to nothing, rather than reporting a URL it
  merely handed to the system.
- `--restart PKG` terminates then launches; `--activity NAME` picks the activity for
  `--launch`/`--restart`; `--args KEY=VALUE` (repeatable) passes an intent string extra
  (`am start --es KEY VALUE`)
- List installed packages (`--filter TEXT` narrows `--list`)
- Check app state
