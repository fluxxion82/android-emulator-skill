---
name: android-emulator-skill
version: 0.9.0
description: Use when a task needs an Android emulator or device driven from the shell, such as building an Android app with Gradle and reading its errors, launching it, seeing what is on screen, tapping and typing by label, reading logcat, crashes or ANRs, auditing accessibility, or creating, booting and resetting emulators. Scripts over adb, uiautomator and the Gradle wrapper that answer in a few lines instead of raw dumps.
---

# Android Emulator Skill

Build, test and drive Android apps by what is on the screen, not by pixel coordinates. Each
script answers in a few lines; `--json` gives the answer as JSON (its shapes: **Failures**).

## Invoking these scripts

Scripts live in `scripts/`, beside this file. **Use a path rooted at the skill
directory, not a bare relative path** — when the skill is installed as a plugin
and you are working in a project, `python scripts/foo.py` is a file-not-found.

```bash
SKILL_DIR=/path/to/skills/android-emulator-skill
python3 "$SKILL_DIR/scripts/screen_mapper.py" --json
```

Every example here and in `references/` is written with `$SKILL_DIR` set that way.

**Which device.** A script that talks to a device takes `--serial`. Without it the one
attached device is used; several attached and no `--serial` is a refusal (exit 1), and
none is exit 3. An entry says so where its script differs.

## Quick Start

```bash
# 0. Check the environment (exit 1 names what to fix: adb, a JDK below 17, ...)
bash "$SKILL_DIR/scripts/android_health_check.sh" --json

# 1. Launch app
python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app

# 2. Map screen to see elements
python3 "$SKILL_DIR/scripts/screen_mapper.py"

# 3. Tap button (add --scroll-to-find if it may be below the fold)
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap

# 4. Enter text
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --enter-text "user@example.com"

# 5. Run accessibility audit
python3 "$SKILL_DIR/scripts/accessibility_audit.py"
```

All scripts support `--help` for detailed options and `--json` for machine-readable output.

## The loop

Two handoffs carry most tasks. In each, one command prints the value the next one takes:
pass it back whole, as printed.

**See, act, verify.** `screen_mapper.py` names the controls on the screen. Hand one of
those names to `navigator.py`, then map again: the screen that comes back is the check
that the action did what was meant.

```bash
python3 "$SKILL_DIR/scripts/screen_mapper.py"
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Submit Order" --tap
python3 "$SKILL_DIR/scripts/screen_mapper.py"
```

`Not found` on a screen that scrolls: add `--scroll-to-find`. A control with no name:
`--find-type`, `--find-id`, and `--tap-at x,y` last.

**Build, then diagnose.** `build_and_test.py` prints a summary line ending in a build id
and, when the build failed, its first errors. Pass the id back for the rest (`--get-errors`,
`--get-warnings`, `--get-log`); do not run the build again to read them.

```bash
python3 "$SKILL_DIR/scripts/build_and_test.py" --project .
python3 "$SKILL_DIR/scripts/build_and_test.py" --get-errors build-20251028-143052-1a2b3c4d
```

## Scripts (v0.9.0)

Each entry: what the script is for, one command to start from, its flags, and a link to
its detail (modes, output, failures). Read the detail file before a mode you have not used.

### Implemented (33 scripts)

#### Core Utilities (16 modules in `common/`)

Shared by the scripts and not run directly: `adb_exec`, `anr_pipeline`, `anr_sessions`, `batch`, `cache_utils`, `cli`, `device_utils`, `dump_read`, `emu_console`, `env_config`, `hierarchy`, `log_lifecycle`, `logcat`, `run_as`, `screenshot_utils`, `sdk_tools`.

#### App Management (1 script)

1. **app_launcher.py** - Launch, restart, stop, install or deep-link an app; list packages; check an app's state.
    - Run: `python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app`
    - `--launch` and `--open-url` wait for the activity and fail unless the system reports `Status: ok`.
    - Options: `--launch`, `--restart`, `--activity`, `--args KEY=VALUE`, `--terminate`, `--install`, `--uninstall`, `--open-url`, `--list`, `--filter`, `--state`, `--serial`/`-s`, `--json`
    - Detail: [references/app_launcher.md](references/app_launcher.md)

#### Device Lifecycle (5 scripts)

2. **emulator_boot.py** - Boot an AVD, and wait until it is ready when asked.
    - Run: `python3 "$SKILL_DIR/scripts/emulator_boot.py" --avd Pixel_9 --wait-ready`
    - Refuses while an attached emulator cannot say which AVD it is: a second instance of one AVD corrupts its data.
    - Options: `--avd`, `--all`, `--wait-ready`, `--timeout`, `--headless`, `--list-avds`, `--json`
    - Detail: [references/emulator_boot.md](references/emulator_boot.md)

3. **emulator_shutdown.py** - Shut down a running emulator. Emulators only: any other serial is refused before adb runs.
    - Run: `python3 "$SKILL_DIR/scripts/emulator_shutdown.py" --serial emulator-5554`
    - Needs `--serial`, `--name` or `--all`: it never picks a device by itself.
    - Options: `--serial`, `--name`, `--verify`/`--no-verify`, `--timeout`, `--all`, `--json`
    - Detail: [references/emulator_shutdown.md](references/emulator_shutdown.md)

4. **emulator_create.py** - Create an AVD from a device definition and an API level.
    - Run: `python3 "$SKILL_DIR/scripts/emulator_create.py" --device pixel_7 --api 34 --name test-device`
    - The system image must be installed already; `--list-images` says which are. A Python running under Rosetta needs `--abi arm64-v8a`.
    - Options: `--device`, `--api`, `--name`, `--abi`, `--variant`, `--list-devices`, `--list-images`, `--json`
    - Detail: [references/emulator_create.md](references/emulator_create.md)

5. **emulator_delete.py** - Delete an AVD for good. Destructive.
    - Run: `python3 "$SKILL_DIR/scripts/emulator_delete.py" --name test-device --yes`
    - Without `--yes` it asks on stderr and reads the answer from stdin. `--old N` keeps the newest N by config modification time; N is not an age.
    - Options: `--name`, `--all`, `--old`, `--yes`, `--list`, `--json`, `--verbose`
    - Detail: [references/emulator_delete.md](references/emulator_delete.md)

6. **emulator_erase.py** - Factory-reset an AVD: wipe its data, keep the AVD. Destructive.
    - Run: `python3 "$SKILL_DIR/scripts/emulator_erase.py" --name test-device --verify`
    - Refuses while the AVD may be running (`--force` erases anyway). Snapshots are kept.
    - Options: `--name`, `--all`, `--force`, `--verify`, `--timeout`, `--list`, `--json`, `--verbose`
    - Detail: [references/emulator_erase.md](references/emulator_erase.md)

#### Build & Development (2 scripts)

7. **build_and_test.py** - Build with the project's Gradle wrapper, run unit or instrumented tests, and read a build's errors back by its id.
    - Run: `python3 "$SKILL_DIR/scripts/build_and_test.py" --project . --test`
    - Prints a summary line with a build id (see **The loop**). `--instrumented` runs on one device.
    - Options: `--project`, `--module`/`-p`, `--variant`, `--clean`, `--test`, `--instrumented`, `--suite`, `--serial`, `--get-errors`/`--get-warnings`/`--get-log <ID>`, `--list-builds`, `--verbose`, `--json`
    - Detail: [references/build_and_test.md](references/build_and_test.md)

8. **log_monitor.py** - Read logcat for one app or the whole device: a time window, a live read, or history.
    - Run: `python3 "$SKILL_DIR/scripts/log_monitor.py" --app com.example.app --duration 30s --severity error,warning`
    - `--duration` and `--last` need a unit (`30s`, `5m`). An app with no process in the window is refused.
    - Options: `--app`, `--serial`, `--severity`, `--follow`, `--duration`, `--last`, `--output`, `--clear`, `--verbose`, `--json`
    - Detail: [references/log_monitor.md](references/log_monitor.md)

#### Navigation & Interaction (4 scripts)

9. **screen_mapper.py** - See the current screen: names its interactive controls. Run it first, and again after acting.
    - Run: `python3 "$SKILL_DIR/scripts/screen_mapper.py"`
    - The summary shows up to 15 names per kind of control and the `--verbose` breakdown up to 10; `--json` holds the uncapped analysis.
    - Options: `--serial`/`-s`, `--verbose`/`-v`, `--hints`, `--json`
    - Detail: [references/screen_mapper.md](references/screen_mapper.md)

10. **navigator.py** - Find a control by a name `screen_mapper` printed, by type or by id, then tap it or type into it.
    - Run: `python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap`
    - Reads the visible screen only; add `--scroll-to-find` to search below the fold.
    - Options: `--find-text`, `--find-exact`, `--exact`, `--find-type`, `--find-id`, `--index`, `--tap`, `--enter-text`, `--tap-at x,y`, `--list`, `--scroll-to-find`, `--scroll-direction {down,up}`, `--max-scrolls N`, `--serial`, `--json`, `--verbose`
    - Detail: [references/navigator.md](references/navigator.md)

11. **gesture.py** - Swipe, scroll, drag, long-press, pull to refresh.
    - Run: `python3 "$SKILL_DIR/scripts/gesture.py" --scroll down`
    - `--scroll` names where the content moves, `--swipe` what the finger does: opposites.
    - Options: `--swipe`, `--from-edge`, `--swipe-path`, `--drag`, `--refresh`, `--duration`, `--long-press`, `--scroll`, `--count`, `--serial`, `--json`
    - Detail: [references/gesture.md](references/gesture.md)

12. **keyboard.py** - Type text, press keys and hardware buttons, put the keyboard away.
    - Run: `python3 "$SKILL_DIR/scripts/keyboard.py" --type "hello"`
    - `--button back` presses BACK; `--hide-keyboard` presses it only when a keyboard is shown.
    - Options: `--type`, `--delay`, `--key`, `--count`, `--keys`, `--button`, `--clear`, `--hide-keyboard`, `--dismiss`, `--serial`/`-s`, `--json`
    - Detail: [references/keyboard.md](references/keyboard.md)

#### Testing & Analysis (4 scripts)

13. **accessibility_audit.py** - Audit the current screen: unlabelled controls, small touch targets, missing hints.
    - Run: `python3 "$SKILL_DIR/scripts/accessibility_audit.py"`
    - Exits 1 when a critical issue is present. No contrast check.
    - Options: `--serial`, `--output`, `--verbose`, `--json`
    - Detail: [references/accessibility_audit.md](references/accessibility_audit.md)

14. **visual_diff.py** - Compare two screenshots and pass or fail against a pixel threshold.
    - Run: `python3 "$SKILL_DIR/scripts/visual_diff.py" baseline.png current.png --threshold 0.01`
    - The two images are positional. FAIL, or two sizes, is exit 1.
    - Options: `--output`, `--threshold`, `--json`, `--details`
    - Detail: [references/visual_diff.md](references/visual_diff.md)

15. **test_recorder.py** - Document a test run step by step: a screenshot and the hierarchy per step, a report at the end.
    - Run: `python3 "$SKILL_DIR/scripts/test_recorder.py" --start "Login flow"`
    - Then `--step TEXT` after each interaction and `--stop`; one command at a time per session.
    - Options: `--start`, `--step`, `--stop`, `--list`, `--get-details`, `--clear`, `--session`, `--serial`, `--app-name`, `--size`, `--screen`, `--state`, `--assert`, `--assert-failed`, `--failed`, `--older-than`, `--verbose`, `--json`
    - Detail: [references/test_recorder.md](references/test_recorder.md)

16. **app_state_capture.py** - Capture a debugging snapshot: screenshot, hierarchy, the app's own logs, app info.
    - Run: `python3 "$SKILL_DIR/scripts/app_state_capture.py" --package com.example.app`
    - Prints where the snapshot went; never the working directory unless `--output` says so.
    - Options: `--package`, `--output`, `--serial`, `--logs`, `--no-logs`, `--log-lines`, `--screenshot-size`, `--json`, `--verbose`
    - Detail: [references/app_state_capture.md](references/app_state_capture.md)

#### Advanced Testing & Permissions (3 scripts)

17. **privacy_manager.py** - Grant, revoke or list an app's runtime permissions.
    - Run: `python3 "$SKILL_DIR/scripts/privacy_manager.py" --package com.example.app --grant camera,location`
    - `--list-permissions` names the permissions it knows.
    - Options: `--grant`, `--revoke`, `--reset`, `--list`, `--package`, `--user`, `--serial`, `--list-permissions`, `--json`, `--verbose`
    - Detail: [references/privacy_manager.md](references/privacy_manager.md)

18. **status_bar.py** - Set what the status bar draws (battery, signal, clock) for stable screenshots.
    - Run: `python3 "$SKILL_DIR/scripts/status_bar.py" --preset clean`
    - SystemUI demo mode: it changes what is drawn, not what the app reads. `--reset` ends it.
    - Options: `--preset`, `--battery`, `--charging`, `--wifi`, `--wifi-level N`, `--mobile`, `--mobile-level N`, `--mobile-type TYPE`, `--time`, `--reset`, `--serial`, `--json`, `--verbose`
    - Detail: [references/status_bar.md](references/status_bar.md)

19. **push_notification.py** - Post a notification into the shade as the shell, and read back what any package has posted.
    - Run: `python3 "$SKILL_DIR/scripts/push_notification.py" --post --tag "Title" --text "Body"`
    - It cannot deliver into an app's own FCM handler; no adb path does.
    - Options: `--post`, `--list`, `--grant-permission`, `--revoke-permission`, `--tag`, `--text`, `--no-verify`, `--expect-package`, `--package`, `--serial`, `--json`, `--verbose`
    - Detail: [references/push_notification.md](references/push_notification.md)

#### Discovery & Device State (14 scripts)

20. **android_health_check.sh** - Check the environment before anything else: SDK tools, JDK 17 or newer, Python, devices, AVDs.
    - Run: `bash "$SKILL_DIR/scripts/android_health_check.sh" --json`
    - Exits 1 naming what to fix.
    - Options: `--json`, `--help`
    - Detail: [references/android_health_check.md](references/android_health_check.md)

21. **device_list.py** - List attached devices and defined AVDs.
    - Run: `python3 "$SKILL_DIR/scripts/device_list.py"`
    - Options: `--get-details`, `--device-type`/`--name`, `--json`
    - Detail: [references/device_list.md](references/device_list.md)

22. **emulator_selector.py** - Suggest which AVD to use, list them, or boot one.
    - Run: `python3 "$SKILL_DIR/scripts/emulator_selector.py" --suggest`
    - Options: `--suggest`, `--list`, `--boot NAME`, `--headless`, `--count`, `--json`, `--verbose`
    - Detail: [references/emulator_selector.md](references/emulator_selector.md)

23. **localization_audit.py** - Audit `strings.xml`: keys missing per locale, placeholder and plural mismatches.
    - Run: `python3 "$SKILL_DIR/scripts/localization_audit.py" --res app/src/main/res`
    - Reads files; no device. `--strict` exits 2 on findings.
    - Options: `--res DIR`, `--source DIR`, `--locale CODE`, `--strict`, `--json`, `--verbose`
    - Detail: [references/localization_audit.md](references/localization_audit.md)

24. **appearance.py** - Switch dark or light theme and the font scale.
    - Run: `python3 "$SKILL_DIR/scripts/appearance.py" --theme dark`
    - `--reset` restores the defaults.
    - Options: `--theme {light,dark}`, `--text-size {small,default,large,xl}`, `--font-scale`, `--locale`, `--reset`, `--serial`, `--json`, `--verbose`
    - Detail: [references/appearance.md](references/appearance.md)

25. **location.py** - Set an emulator's GPS position: coordinates, a city preset, or a GPX route. Emulator only.
    - Run: `python3 "$SKILL_DIR/scripts/location.py" --city london`
    - Options: `--lat`/`--lng`, `--city`, `--gpx FILE`, `--interval`/`--speed`, `--clear`, `--list-cities`, `--serial`, `--json`, `--verbose`
    - Detail: [references/location.md](references/location.md)

26. **container.py** - Inspect a debuggable app's files: list, read, shared preferences, databases, export.
    - Run: `python3 "$SKILL_DIR/scripts/container.py" --package com.example.app --ls`
    - Debuggable builds only (`run-as`); a release build is refused by name.
    - Options: `--package` (all but `--get-cached`), `--ls [SUBPATH]`, `--cat FILE`, `--shared-prefs [NAME]`, `--databases [NAME]`, `--export DIR`, `--get-cached ID`, `--serial`, `--json`, `--verbose`
    - Detail: [references/container.md](references/container.md)

27. **model_inspector.py** - Inspect persistence: Room entities in source, Room's exported schema, or a live app's SQLite schema.
    - Run: `python3 "$SKILL_DIR/scripts/model_inspector.py" --source app/src/main/java`
    - `--package` reads a live, debuggable app; the other two read files.
    - Options: `--source DIR`, `--schema PATH`, `--show-versions`, `--raw NAME`, `--package`, `--db NAME`, `--serial`, `--json`, `--verbose`
    - Detail: [references/model_inspector.md](references/model_inspector.md)

28. **anr_watcher.py** - Find ANRs and jank in logcat: a window of history, a live watch, or a session around a test.
    - Run: `python3 "$SKILL_DIR/scripts/anr_watcher.py" --since 5m`
    - Session: `--start` prints an id, `--stop ID` the summary. macOS and Linux only.
    - Options: `--watch`, `--since`, `--start`, `--stop`, `--get-details`, `--list-sessions`, `--clear-sessions`, `--diff`, `--package`, `--serial`, `--duration`, `--min-frames`, `--top`, `--all`, `--budget-tokens`, `--cluster`, `--raw`, `--older-than`, `--terse`, `--abandon`, `--json`
    - Detail: [references/anr_watcher.md](references/anr_watcher.md)

29. **sms.py** - Deliver an inbound SMS to an emulator and confirm it arrived; extract a one-time code from the inbox.
    - Run: `python3 "$SKILL_DIR/scripts/sms.py" --send --to 5551234567 --body "Your code is 123456"`
    - `--send` is emulator only.
    - Options: `--send --to NUM --body TEXT [--no-verify]`, `--list [--limit N]`, `--otp`, `--serial`, `--json`, `--verbose`
    - Detail: [references/sms.md](references/sms.md)

30. **snapshot.py** - Save, load or delete an emulator snapshot: a state reset in about 2 s. Emulator only.
    - Run: `python3 "$SKILL_DIR/scripts/snapshot.py" --save clean-state`
    - A load that did not happen is reported as a failure.
    - Options: `--list`, `--save NAME`, `--load NAME`, `--delete NAME`, `--no-verify`, `--timeout`, `--serial`, `--json`, `--verbose`
    - Detail: [references/snapshot.md](references/snapshot.md)

31. **crash_triage.py** - Read the crash buffer as structured crashes, grouped by fault.
    - Run: `python3 "$SKILL_DIR/scripts/crash_triage.py" --package com.example.app`
    - Exit 0 means triage ran, not that nothing crashed: read `crash_count`, or pass `--fail-on-crash`.
    - Options: `--package PKG`, `--clear`, `--fail-on-crash`, `--serial`, `--json`, `--verbose`
    - Detail: [references/crash_triage.md](references/crash_triage.md)

32. **logs.py** - One entry point for reading logs: `tail` (`log_monitor.py`), `crashes` (`crash_triage.py`), `anr` (`anr_watcher.py`).
    - Run: `python3 "$SKILL_DIR/scripts/logs.py" crashes`
    - Everything after the verb goes to that script unchanged.
    - Options: `<verb> [verb options]`, `--json` (routing table), `--help`
    - Detail: [references/logs.md](references/logs.md)

33. **avd.py** - One entry point for the emulator lifecycle: `list`, `pick`, `create`, `start`, `stop`, `reset`, `delete`.
    - Run: `python3 "$SKILL_DIR/scripts/avd.py" list`
    - Everything after the verb goes to that script unchanged, its confirmation flag included.
    - Options: `<verb> [verb options]`, `--json` (routing table), `--help`
    - Detail: [references/avd.md](references/avd.md)

## Requirements

- macOS or Linux. Windows is untested, and `build_and_test.py`, `android_health_check.sh`
  and `anr_watcher.py` are known not to run there (see **Known gaps** below)
- Android SDK with platform-tools and emulator
- Python 3.12+
- ADB (Android Debug Bridge)
- Optional: Gradle for building
- Optional: Pillow for screenshot resizing

## Installation

### Environment Setup

```bash
# 1. Install Android SDK (via Android Studio or command line tools)
# Download from: https://developer.android.com/studio

# 2. Set environment variables
export ANDROID_HOME=$HOME/Library/Android/sdk  # macOS
export PATH=$PATH:$ANDROID_HOME/platform-tools
export PATH=$PATH:$ANDROID_HOME/emulator

# 3. Verify installation
adb version
emulator -version
```

### As Claude Code Skill

```bash
# As a plugin (preferred; updating is then two commands -- see README.md)
claude plugin marketplace add fluxxion82/android-emulator-skill
claude plugin install android-emulator-skill@fluxxion82

# From a clone: the skill is the INNER directory (this file's own), not the
# repository root, so clone the repo and link that directory into place.
git clone https://github.com/fluxxion82/android-emulator-skill ~/src/android-emulator-skill

# Personal installation
ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill ~/.claude/skills/android-emulator-skill

# Project installation
ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill .claude/skills/android-emulator-skill
```

## Documentation

- **SKILL.md** (this file) - the loop, the script index with every flag, and the
  failure contract
- **README.md** - short orientation for humans
- **examples/** - complete automation workflows (ships with the package)
- **references/** - one file per script (its modes, output and failures), and
  `known-gaps.md`, what the skill does not do yet (ships with the package)

In the source repository only (not included in the released package):

- **CLAUDE.md** - architecture, conventions, and the recorded-fixture policy
- **CHANGELOG.md** - release history
- **docs/** - deep dives on adb, test patterns, accessibility

## Platform limitations worth knowing

**The clipboard is not reachable from adb.** There is no way to *set* clipboard
text from the shell on any modern Android:

- `cmd clipboard` reports "No shell command implementation" on API 33 and 35 —
  the clipboard service exposes no shell command interface at all.
- `service call clipboard <setPrimaryClip>` cannot work either: the call takes a
  `ClipData` Parcelable, whose text is marshalled with `writeString8()` plus
  version-dependent fields, and `service` can only write `i32`/`s16`/binders.
- The emulator console (`adb emu`) has no clipboard command.

Reading and clearing *are* reachable — `service call clipboard 3/4/9` with the
right signature is accepted for the shell uid, which holds
`READ_CLIPBOARD_IN_BACKGROUND` — but writing is not, so a paste-flow test needs
an app-side hook or a helper IME.

On an emulator only, the gRPC control API does expose `setClipboard` /
`getClipboard` (see `$ANDROID_HOME/emulator/lib/emulator_controller.proto`).
That needs a gRPC client and the emulator's auth token, and is not wired up here.

`clipboard.py` was removed in v0.6.0 for this reason: its surviving code path
called `service call clipboard 1` with the pre-Android-10 signature and always
failed.

**Jetpack Compose screens look different in the dump.** Everything below was
measured from recorded dumps of a real Compose app, not assumed — three
plausible-sounding assumptions about Compose turned out to be wrong.

- **The host class is `androidx.compose.ui.platform.ComposeView`.**
  `AndroidComposeView` does not appear in the dump at all, so a detector
  looking for it matches nothing.
- **There are no resource-ids.** A default Compose screen exposes only
  `android:id/content`, which belongs to the AOSP FrameLayout. `--find-id` has
  nothing to work with; use `--find-text` or `screen_mapper`.
- **Interactive nodes carry no label of their own.** Every clickable and
  checkable node has `text=""` and `content-desc=""`. uiautomator dumps the
  *unmerged* semantics tree, so a clickable `Card` keeps its Texts as separate
  children rather than merging them into one label — there is no concatenation
  anywhere. `screen_mapper` recovers labels from a control's descendants
  (Button, Card, list row) and from row-adjacent siblings (Checkbox, Switch,
  icon), which is why it can name a control that the dump leaves anonymous.

**To make your own Compose app addressable**, opt into test tags as resource-ids
on the root of the tree:

```kotlin
Modifier.semantics { testTagsAsResourceId = true }   // once, at the root
Modifier.testTag("submit_button")                    // on each control
```

The tag then surfaces as a **bare** `resource-id` — `submit_button`, *not*
`com.example.app:id/submit_button`. Anything that splits a resource-id on
`":id/"` to recover a name drops every Compose tag on the screen.

## Failures

Every script ends in `cli.run(main)` (`common/cli.py`), so a failure has one
shape whichever script met it. What is promised, and what is not yet:

- A *typed* failure — a missing `adb` or SDK tool, an adb call that failed, a
  screen that would not dump, a timeout, or a failure a script raises with
  `cli.fail` — answers `{"success": false, "error": ..., "remedy": ...}` on
  one line of stdout under `--json`, `Error:` / `Remedy:` on stderr
  otherwise, and exits **1**. A refusal that found something on the way may
  carry it as extra keys after those three (a lookup's `search`, a capture's
  `partial`, the entries a listing could read); the three are always there.
- A device that is **unavailable** (not found, offline, unauthorized) is the
  same object and exits **3** — whether `--serial` resolution noticed or adb
  did mid-run. Several devices and no `--serial` is 1.
- A **usage error** — an unknown flag, a bad value for a typed flag, a
  missing required argument, a flag combination the script rejects, an
  unknown `avd.py` / `logs.py` verb — exits **2** and is the same object
  under `--json`: one line on stdout, `error` argparse's message, `remedy`
  the usage line (for an unknown verb, the verbs). Inside a router verb it
  is the delegate's own (`avd.py list --bogus --json`). Without `--json` it
  is argparse's prose on stderr (the route guide for an unknown verb).
  `--json` after `--` is a positional value, not the flag.
- A script's own exit statuses pass through unchanged, including the ones
  that mean a *finding*: `crash_triage --fail-on-crash` and
  `localization_audit --strict` exit 2 when they find something, and
  `accessibility_audit` exits 1 when a critical issue is present.
- A streaming mode (`log_monitor --follow`, `anr_watcher --watch`) cannot
  take back the records it already printed: there the refusal object is the
  **last line** of stdout, and parses on its own. `anr_watcher --since
  --json` is JSON Lines as well, like `--watch --json`: one object per
  event, no enclosing document, so read it line by line (a single-document
  parse fails at the second event). `anr_watcher --get-details ID --raw`
  prints the session's stored event lines (`events.jsonl`, an abandoned
  session's up to its fence), with or without `--json`: JSON Lines too.
  Every other mode keeps stdout to one
  JSON document under `--json` (`emulator_delete`'s confirmation prompt
  goes to stderr). A refusal is always the object; a successful listing is
  an array instead in five modes: `build_and_test --get-errors`,
  `--get-warnings` and `--list-builds`, `test_recorder --list`, and
  `anr_watcher --list-sessions`.
- Some places catch a device error on purpose, so it does not reach the
  boundary. This list is not exhaustive; among them: boot-completion
  polling (`emulator_boot`), the snapshot
  save/delete verify poll (`snapshot`, which keeps asking until its deadline
  and then reports the device as unavailable), the optional
  foreground-activity lookup (`device_utils.get_current_activity`), the
  emulator-console survey (`emu_console`), and the partial-capture policy of
  `test_recorder` steps and `app_state_capture` (what arrived is kept and
  handed over -- by `app_state_capture` in a refusal that is still exit 3
  when the device was unavailable), and `anr_watcher`'s attribution lookups
  (system_server's pid, the package's uids, a live pid's uid and the whole
  uid table, each an unknown answer on failure, disclosed as unattributed or
  unknown attribution rather than ending a read or a session).
- Checked, not just promised: `tests/test_exit_code_sweep.py` runs every
  Quick Start command and the modes its table lists twice — with the Android
  tools present and failing, and with none on PATH at all — and
  asserts the refusal itself (one object, or `Error:` / `Remedy:`; an error
  naming the tool; the exact status), not just a non-zero exit. In all it
  asserts the payload of 40 documented modes, each of them twice. It runs a
  usage error through every script in both modes too, and fails if a
  script on disk is missing from that run (`android_health_check.sh`
  included; its object carries `ok` as well, like its other answers).
- A **batch** that reports per item — per operation in `status_bar` and
  `appearance`, per permission in `privacy_manager`'s grant/revoke/reset,
  per AVD or emulator in `emulator_boot` / `emulator_shutdown` /
  `emulator_erase` `--all` and `emulator_delete` `--all` / `--old`
  (`common/batch.py`) — is one object: `success` false when ANY item failed,
  then `error` and `remedy` as above, the batch's own keys, and `items`,
  each item its own fields plus `success`, `error`, `remedy` (null when it
  succeeded). Exit 0 only when every item succeeded, 1 when one failed; a
  batch whose every item failed is still that one object. **Breaking in
  v0.9.0:** each of these printed `results` (or, `emulator_shutdown`,
  counts only); it is `items` now. In a one-device batch (`status_bar`,
  `appearance`, `privacy_manager`) a device that goes away stops the batch
  at the item that met it: that item carries the device error, every item
  after it says `not attempted`, and the object exits 3 (1 for several
  devices and no `--serial`). In the `emulator_*` batches each item is its
  own emulator, so one that vanished is that item's failure and the rest
  still run. (`anr_watcher --all` keeps every ANR cluster; it is not a
  batch.)
- Device-error confirmation applies to app-file reads (`container` and
  `model_inspector` ask `run_argv` for it with `confirm_device_errors`): their
  stderr is a device command's, and a file name can print an `adb: …` line.
  logcat's readers do not ask: that stderr is adb's own, and a probe finding
  the device a moment later turned a real device error into exit 1. Of the
  `run_adb` callers whose arguments are agent-supplied, three print that
  text back on stderr where a forged `adb: device ... not found` line would
  read as adb's own, and ask `run_adb` for the same confirmation:
  `privacy_manager`'s `pm grant` / `pm revoke` (an unknown permission's
  name), `appearance --locale` (`setprop` names the value it refused) and
  `app_launcher --install` (adb's own `adb: failed to stat <path>`). These
  were recorded alongside them (`tests/record_fixtures.py`, the `*_echo`
  fixtures), and their text comes back on stdout, or not at all, so it is
  never classified: `privacy_manager`'s `dumpsys package`; `app_launcher`'s
  launch, `--open-url`, `--terminate`, `--uninstall` and launcher lookup;
  `device_utils`' `pm dump` and package-uid lookup; `pidof` (`log_monitor`'s
  stream path, `app_state_capture`, `app_launcher --state`) and
  `app_state_capture`'s version probe; `status_bar --mobile-type`;
  `keyboard` / `navigator` `input text`; and `push_notification --post`. The probe cannot tell such an echo from a real loss followed
  by a recovery before it, so a caller whose stderr is adb's alone does not
  ask.

## Known gaps and release history

- **Known gaps:** [references/known-gaps.md](references/known-gaps.md), which
  ships with the skill. Read it before relying on: Windows; a contrast check
  in `accessibility_audit`; `anr_watcher --package` attribution for isolated,
  app-zygote and SDK-sandbox processes, or for a package installed,
  reinstalled or given another user during the read; an ANR session without
  `--package` naming an app that was already running; a log capture as proof
  that a window is complete; `build_and_test --instrumented` beyond one
  device and one variant, or on a results layout other than the two recorded;
  writing the clipboard.
- **Release history:** `CHANGELOG.md` at the root of the source repository
  (https://github.com/fluxxion82/android-emulator-skill). It does not ship
  with the skill.
