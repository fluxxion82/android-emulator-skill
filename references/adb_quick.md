# adb Quick Reference

The Android command cheat-sheet that sits behind this skill. Every script in
`scripts/` ultimately shells out to the commands below via
`common.device_utils.build_adb_command()` (never `shell=True`). Use this page when
you need the raw command, or to understand what a script is doing under the hood.

Conventions:

- `<serial>` = a device serial from `adb devices -l` (e.g. `emulator-5554`). Omit
  `-s <serial>` to target the single connected device.
- `<pkg>` = an Android package name (e.g. `com.example.app`).
- Coordinates are screen pixels; bounds come from the UI hierarchy (see below).

## Devices & targeting

```bash
adb devices -l                       # list devices/emulators with model + transport
adb -s <serial> shell <cmd...>       # run a shell command on a specific device
adb start-server                     # start the adb daemon
adb kill-server                      # restart adb if devices misbehave
adb -s <serial> wait-for-device      # block until the device is connected
adb -s <serial> get-state            # device | offline | bootloader
adb -s <serial> shell getprop sys.boot_completed   # "1" once fully booted
```

`adb devices -l` is the source of truth for serials. Output line shape:
`<serial>\t<state>` plus key=value pairs (`model:`, `device:`, `transport_id:`).
State `device` = ready; `offline`/`unauthorized` = not usable yet.

## Install / uninstall

```bash
adb -s <serial> install -r app.apk   # -r = replace/reinstall, keep data
adb -s <serial> install app.apk      # fresh install (fails if already present)
adb -s <serial> uninstall <pkg>      # remove app and its data
```

## Capture binary output (exec-out)

`exec-out` streams raw bytes without the line-ending mangling `shell` applies — use
it for screenshots, file pulls, and SQLite dumps.

```bash
adb -s <serial> exec-out screencap -p > screen.png      # PNG to stdout
adb -s <serial> shell screencap -p /sdcard/screen.png   # on-device file
adb -s <serial> exec-out run-as <pkg> cat databases/app.db > app.db
```

## UI hierarchy: uiautomator dump

The accessibility-driven navigation in `screen_mapper.py` / `navigator.py` is built
on this. Dump the current window, then pull and parse the XML.

```bash
adb -s <serial> shell uiautomator dump /sdcard/window_dump.xml
adb -s <serial> shell cat /sdcard/window_dump.xml        # read the XML back
```

Parsing notes:

- Every UI node is a `<node>` element. Attributes are **strings**:
  `class`, `text`, `content-desc`, `resource-id`, `clickable`, `bounds`, etc.
- `bounds` is `"[left,top][right,bottom]"`. Tap center =
  `((left+right)/2, (top+bottom)/2)`.
- Booleans are the strings `"true"` / `"false"` — coerce them yourself.

## input: tap / text / keyevent / swipe

```bash
adb -s <serial> shell input tap <x> <y>
adb -s <serial> shell input text "hello%sworld"   # %s = space; escape special chars
adb -s <serial> shell input keyevent KEYCODE_ENTER
adb -s <serial> shell input swipe <x1> <y1> <x2> <y2> [duration_ms]
```

Common keyevents used by `keyboard.py`:

| Name      | Keycode               | Name        | Keycode                |
|-----------|-----------------------|-------------|------------------------|
| enter     | `KEYCODE_ENTER`       | back        | `KEYCODE_BACK`         |
| delete    | `KEYCODE_DEL`         | home        | `KEYCODE_HOME`         |
| tab       | `KEYCODE_TAB`         | menu        | `KEYCODE_MENU`         |
| space     | `KEYCODE_SPACE`       | search      | `KEYCODE_SEARCH`       |
| escape    | `KEYCODE_ESCAPE`      | volume_up   | `KEYCODE_VOLUME_UP`    |
| up/down   | `KEYCODE_DPAD_UP/DOWN`| volume_down | `KEYCODE_VOLUME_DOWN`  |
| left/right| `KEYCODE_DPAD_LEFT/RIGHT` | power   | `KEYCODE_POWER`        |

`input text` does not type spaces literally — send `%s` for each space. A long swipe
duration turns a swipe into a scroll/fling; `gesture.py` builds directional swipes
from screen size + a swipe percentage.

## am: start / force-stop / broadcast

```bash
# Launch an activity explicitly
adb -s <serial> shell am start -n <pkg>/.MainActivity

# Open a deep link (VIEW intent)
adb -s <serial> shell am start -a android.intent.action.VIEW -d "<url>"

# Pass string extras
adb -s <serial> shell am start -n <pkg>/.MainActivity --es KEY VALUE

# Force-stop an app
adb -s <serial> shell am force-stop <pkg>

# Broadcast an intent (used to simulate notifications)
adb -s <serial> shell am broadcast -a <ACTION> --es title "Hi" --es message "Body"
```

## pm: grant / revoke / list

```bash
adb -s <serial> shell pm grant  <pkg> <permission>   # e.g. android.permission.CAMERA
adb -s <serial> shell pm revoke <pkg> <permission>
adb -s <serial> shell pm list packages               # all installed packages
adb -s <serial> shell pm list packages -3            # third-party only
adb -s <serial> shell dumpsys package <pkg>          # inspect granted permissions
```

Runtime permissions can only be granted/revoked if they are declared in the app's
manifest. `dumpsys package <pkg>` is how `privacy_manager.py` reads current grants.

## logcat: filters and history

```bash
adb -s <serial> logcat                       # stream live
adb -s <serial> logcat -c                     # clear the buffer
adb -s <serial> logcat *:E                    # errors and above
adb -s <serial> logcat *:W                    # warnings and above
adb -s <serial> logcat --pid=<pid>            # only one process
adb -s <serial> logcat -d -t "MM-DD HH:MM:SS.mmm"   # dump-and-exit since a timestamp
```

Priority filter `*:<P>` shows level `P` and everything above (`V < D < I < W < E < F`).
To scope to one app, resolve its pid first:

```bash
PID=$(adb -s <serial> shell pidof <pkg>)
adb -s <serial> logcat --pid=$PID
```

`log_monitor.py` uses `-d -t <timestamp>` to fetch a bounded historical window
(e.g. "the last 5 minutes") instead of streaming.

## Emulator lifecycle: emulator / avdmanager / sdkmanager

These live in `$ANDROID_HOME` (`emulator/`, `cmdline-tools/latest/bin/`), not in
`platform-tools`. Make sure they are on `PATH`.

```bash
# List defined AVDs
emulator -list-avds

# Boot an AVD (foreground); -no-window for headless/CI
emulator -avd <avd_name>
emulator -avd <avd_name> -no-window

# Shut down a running emulator (graceful, emulator-native)
adb -s <serial> emu kill

# Inspect a running emulator over its console
adb -s <serial> emu avd name        # which AVD this serial is running

# Create / list AVDs and system images
avdmanager list device
avdmanager create avd -n <name> -k "system-images;android-34;google_apis;x86_64" -d pixel_7
sdkmanager --list
sdkmanager "system-images;android-34;google_apis;x86_64"
```

Boot readiness: a serial appearing in `adb devices -l` is not enough — poll
`getprop sys.boot_completed` until it returns `1` (what `emulator_boot.py --wait-ready`
does).

## run-as: debuggable app data

`run-as` gives shell access to a **debuggable** app's private data dir
(`/data/data/<pkg>/`). It fails on release/non-debuggable builds and on unknown
packages. This backs `container.py` and `model_inspector.py`.

```bash
adb -s <serial> shell run-as <pkg> ls -la                  # list the data dir
adb -s <serial> shell run-as <pkg> cat shared_prefs/<name>.xml
adb -s <serial> shell run-as <pkg> ls -la databases
adb -s <serial> exec-out run-as <pkg> cat databases/app.db > app.db   # binary-safe pull
```

Use `exec-out run-as ... cat` (not `shell`) when pulling SQLite/Room databases so the
bytes are not corrupted by newline translation.

## Location: adb emu geo fix (emulator only)

`emu geo fix` drives the emulator console, so it only works on AVDs, not physical
devices. **Longitude comes first**, then latitude:

```bash
adb -s <serial> emu geo fix <LON> <LAT>
adb -s emulator-5554 emu geo fix -122.4194 37.7749   # San Francisco
```

`location.py` adds city presets and sequential GPX route replay on top of this. For a
real device you need a mock-location app instead.

## Common patterns

```bash
# Quick smoke test on a fresh emulator
emulator -avd test-device -no-window &
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = "1" ]; do :; done
adb install -r app.apk
adb shell am start -n com.example.app/.MainActivity

# Inspect what's on screen
adb shell uiautomator dump /sdcard/window_dump.xml
adb shell cat /sdcard/window_dump.xml

# Tap + type into the focused field
adb shell input tap 540 1200
adb shell input text "user@example.com"
adb shell input keyevent KEYCODE_ENTER
```

## See also

- `SKILL.md` — the script reference; each script wraps the commands above.
- `references/` — deeper topic docs.
