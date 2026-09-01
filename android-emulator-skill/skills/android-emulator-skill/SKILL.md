---
name: android-emulator-skill
version: 0.5.0
description: Production-ready scripts for Android app testing, building, and automation. Provides semantic UI navigation, build automation, accessibility testing, and emulator lifecycle management. Optimized for AI agents with minimal token output. Android equivalent of ios-simulator-skill.
---

# Android Emulator Skill

Build, test, and automate Android applications using accessibility-driven navigation and structured data instead of pixel coordinates.

## Invoking these scripts

Scripts live in `scripts/`, beside this file. **Use a path rooted at the skill
directory, not a bare relative path** — when the skill is installed as a plugin
and you are working in a project, `python scripts/foo.py` is a file-not-found.

```bash
SKILL_DIR=/path/to/skills/android-emulator-skill
python3 "$SKILL_DIR/scripts/screen_mapper.py" --json
```

The examples below are written with `$SKILL_DIR` set that way.

## Quick Start

```bash
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

## Scripts (v0.5.0)

### Implemented (31 scripts)

#### Core Utilities (10 modules in `common/`)
1. **common/device_utils.py** - ADB command building and device detection
2. **common/screenshot_utils.py** - Screenshot capture and processing
3. **common/cache_utils.py** - Progressive disclosure cache system

   Also in `common/`: **adb_exec.py** (the one bounded entry point for every adb
   call, with typed errors that name a remedy), **hierarchy.py** (the one way to
   capture the UI hierarchy — no temp files, so concurrent runs cannot read each
   other's screen), **emu_console.py** (`adb emu`, which exits 0 even when it
   fails), **logcat.py** (the one place that builds an `adb logcat` argv and
   parses a duration, shared by all four log readers), **env_config.py**,
   **anr_pipeline.py**, **anr_sessions.py**.

#### App Management (1 script)
4. **app_launcher.py** - App lifecycle management
   - Launch apps by package name
   - Terminate apps
   - Install/uninstall APKs
   - Deep link navigation
   - List installed packages
   - Check app state
   - Options: `--launch`, `--terminate`, `--install`, `--uninstall`, `--open-url`, `--list`, `--state`, `--json`

#### Device Lifecycle (5 scripts) ✓ COMPLETE
5. **emulator_boot.py** - Boot emulators with optional readiness verification
   - Boot by AVD name
   - Wait for device ready with timeout
   - Batch boot operations
   - Headless mode support
   - Options: `--avd`, `--wait-ready`, `--timeout`, `--headless`, `--list-avds`, `--json`

6. **emulator_shutdown.py** - Gracefully shutdown emulators
   - Shutdown by serial number
   - Optional verification of shutdown completion
   - Batch shutdown operations
   - Options: `--serial`, `--verify`, `--timeout`, `--all`, `--json`

7. **emulator_create.py** ⭐ NEW - Create AVDs dynamically
   - Create by device type and API level
   - List available device definitions
   - List available system images
   - Options: `--device`, `--api`, `--name`, `--abi`, `--variant`, `--list-devices`, `--list-images`, `--json`

8. **emulator_delete.py** ⭐ NEW - Delete AVDs permanently
   - Delete by AVD name
   - List available AVDs
   - Options: `--name`, `--list`, `--json`

9. **emulator_erase.py** ⭐ NEW - Factory reset AVDs
   - Wipe user data without deleting AVD
   - Preserve AVD configuration
   - Options: `--name`, `--force`, `--list`, `--json`

#### Build & Development (2 scripts) ✓ COMPLETE
10. **build_and_test.py** - Gradle build/test automation with progressive disclosure (backed by the `gradle/` subpackage)
    - One-line summary + a result ID by default; drill in on demand
    - Parses JUnit XML for pass/fail counts + failed test names; extracts Gradle/Kotlin/Java errors & warnings
    - Options: `--project`, `--module`/`-p`, `--variant`, `--clean`, `--test`, `--suite`, `--get-errors`/`--get-warnings`/`--get-log <ID>`, `--list-builds`, `--verbose`, `--json`

11. **log_monitor.py** ⭐ NEW - Real-time logcat monitoring
    - Filter by app package
    - Filter by severity (error/warning/info/debug)
    - Smart deduplication
    - Duration-based or follow mode
    - Save logs to file
    - Options: `--app`, `--serial`, `--severity`, `--follow`, `--duration`, `--output`, `--clear`, `--verbose`, `--json`

#### Navigation & Interaction (4 scripts)
12. **screen_mapper.py** - Analyze current screen and list interactive elements
    - Count elements by type
    - List buttons, EditTexts, etc.
    - Token-efficient summaries
    - Options: `--serial`, `--verbose`, `--json`, `--list`

13. **navigator.py** - Find and interact with elements semantically
    - Find by text, type, resource ID
    - Tap, enter text, get bounds
    - Fuzzy matching support
    - **`--scroll-to-find`** searches below the fold. Without it a lookup sees only the visible
      screen, and `Not found` is indistinguishable from "the item is two rows down". The default
      path now says which it was: `(searched 1 screen; this screen scrolls -- retry with
      --scroll-to-find)`. Scrolling is opt-in on purpose -- a swipe is not a read, so a lookup
      that scrolled silently would let `--find-text "Save is visible"` pass for a button three
      screens away.
    - Options: `--find-text`, `--find-type`, `--find-id`, `--tap`, `--enter-text`, `--list`,
      `--scroll-to-find`, `--scroll-direction {down,up}`, `--max-scrolls N`, `--serial`,
      `--json`, `--verbose`

14. **gesture.py** - Perform swipes, scrolls, long press
    - `--scroll {up,down}` names the direction the **content** moves; `--swipe` names what the
      **finger** does. They are opposites. (Conflating them was a real defect: `--scroll down`
      dragged lists back toward the top and reported success.)
    - Directional swipes
    - Custom swipe coordinates
    - Scroll and long press
    - Options: `--swipe`, `--from-edge`, `--duration`, `--long-press`, `--scroll`, `--serial`, `--json`

15. **keyboard.py** - Text input and hardware buttons
    - Type text
    - Press hardware keys (back, home, enter, etc.)
    - Clear text
    - Options: `--text`, `--key`, `--button`, `--clear`, `--serial`, `--json`

#### Testing & Analysis (5 scripts) ✓ COMPLETE
16. **accessibility_audit.py** ⭐ NEW - WCAG compliance checking
    - Missing content descriptions
    - Touch target size verification
    - EditText hint checking
    - Image accessibility
    - Categorize by severity (critical/warning/info)
    - Save reports (JSON + Markdown)
    - Options: `--serial`, `--output`, `--verbose`, `--json`

17. **visual_diff.py** - Compare screenshots for visual changes
    - Pixel-perfect comparison
    - Highlight differences
    - Generate diff images
    - Usage: `visual_diff.py BASELINE CURRENT [--output DIR] [--threshold N] [--details]`
    - Note: the two images are **positional**; there is no `--json` (use `--details`)

18. **test_recorder.py** ⭐ NEW - Automatically document test execution
    - Record test steps with screenshots
    - Capture UI hierarchy per step
    - Generate test reports (JSON + Markdown)
    - Inline mode for vision-based testing
    - Options: `--test-name`, `--output`, `--serial`, `--inline`, `--size`, `--app-name`

19. **app_state_capture.py** ⭐ NEW - Complete debugging snapshots
    - Capture screenshot + UI hierarchy + logs + app info
    - Create timestamped snapshots
    - All-in-one debugging artifact
    - Options: `--package`, `--output`, `--serial`, `--logs`, `--no-logs`, `--screenshot-size`, `--json`

#### Advanced Testing & Permissions (4 scripts) ✓ COMPLETE
20. **privacy_manager.py** ⭐ NEW - App permission management
    - Grant/revoke permissions
    - List app permissions
    - Support for 20+ permission types
    - Batch operations
    - Options: `--grant`, `--revoke`, `--list`, `--package`, `--serial`, `--list-permissions`, `--json`

22. **status_bar.py** ⭐ NEW - Status bar control
    - Set battery level and charging state
    - Set WiFi/mobile signal strength
    - Set time display (for consistent screenshots)
    - Demo mode support
    - Options: `--preset`, `--battery`, `--charging`, `--wifi`, `--mobile`, `--time`, `--reset`, `--serial`, `--json`
    - All of these drive SystemUI **demo mode**, which changes what the status
      bar *draws*. It does not change what the app reads from the system
      (`BatteryManager` still reports the real level).

23. **push_notification.py** ⭐ NEW - Push notification simulation
    - Send test notifications
    - List notification channels
    - Multiple delivery methods
    - Options: `--package`, `--title`, `--message`, `--id`, `--data`, `--method`, `--serial`, `--json`
    - ⚠️ Being reworked. It cannot deliver into an app's own FCM handler --
      no adb path reaches that -- and `--list-channels` never worked, because
      `cmd notification` has no `list channels` subcommand.

#### Discovery & Device State (6 scripts) ⭐ NEW

24. **android_health_check.sh** - Verify the environment (ANDROID_HOME, adb, emulator, avdmanager, sdkmanager, java, Python 3.12+, Pillow); lists connected devices and AVDs. Exits non-zero if adb is missing.

25. **device_list.py** - List connected devices (`adb devices -l`) and defined AVDs (`emulator -list-avds`) with progressive disclosure.
    - Options: `--get-details`, `--device-type`/`--name`, `--json`

26. **emulator_selector.py** - Suggest the best AVD (ranked by running → recently used → latest API → common models); list or boot one.
    - Options: `--suggest`, `--list`, `--boot NAME`, `--headless`, `--count`, `--json`, `--verbose`

27. **localization_audit.py** - Audit `res/values*/strings.xml` for missing keys per locale and placeholder mismatches; optional source cross-reference.
    - Options: `--res DIR`, `--source DIR`, `--locale CODE`, `--strict`, `--json`, `--verbose`

28. **appearance.py** - Control dark/light mode (`cmd uimode night`) and font scale (`settings put system font_scale`); best-effort locale.
    - Options: `--theme {light,dark}`, `--text-size {small,default,large,xl}`, `--font-scale`, `--locale`, `--reset`, `--serial`, `--json`

29. **location.py** - Simulate GPS on an emulator via `adb emu geo fix` (fixed coords, city presets, GPX route replay). Emulator-only.
    - Options: `--lat`/`--lng`, `--city`, `--gpx FILE`, `--interval`/`--speed`, `--clear`, `--list-cities`, `--serial`, `--json`

30. **container.py** ⭐ NEW - Inspect a **debuggable** app's sandbox via `adb shell run-as` (fails clearly on release apps).
    - List/read files, dump `shared_prefs` XML, list databases and dump SQLite schema (Room == SQLite), export a snapshot
    - Options: `--package`, `--ls [SUBPATH]`, `--cat FILE`, `--shared-prefs [NAME]`, `--databases [NAME]`, `--export DIR`, `--serial`, `--json`

31. **model_inspector.py** ⭐ NEW - Inspect Android persistence: Room annotations from source, Room exported-schema JSON, and live SQLite schema via `run-as` (Room == SQLite).
    - Options: `--source DIR`, `--schema PATH`, `--show-versions`, `--raw NAME`, `--package`, `--db NAME`, `--serial`, `--json`, `--verbose`

32. **anr_watcher.py** ⭐ NEW - Record & summarise Android ANRs/jank from logcat (Choreographer skipped-frames + ActivityManager ANRs) with session-based progressive disclosure.
    - Session mode: `--start [--package PKG]` → id; `--stop ID` → token-tight summary; `--get-details ID [--cluster N]`; `--list-sessions`; `--clear-sessions`; `--diff A B`
    - Legacy: `--watch [--duration N]`, `--since 5m`; plus `--budget-tokens`, `--terse`, `--json`

33. **sms.py** ⭐ NEW - Deliver an inbound SMS to an emulator and **prove it arrived**. Emulator-only.
    - `--send` reads the inbox back and reports `accepted` and `delivered` separately: the console's `OK` means the command was taken, not that a message exists. Delivery is asynchronous (~2s measured), so it polls.
    - `--otp` extracts a one-time code from the newest message and prints the message it came from, so the heuristic can be checked.
    - Options: `--send --to NUM --body TEXT [--no-verify]`, `--list [--limit N]`, `--otp`, `--serial`, `--json`, `--verbose`

34. **snapshot.py** ⭐ NEW - Save/load/delete emulator snapshots (`adb emu avd snapshot`). A ~2s state reset in place of a 60s reboot. Emulator-only.
    - A failed load is reported as a failure, which is the whole point: `adb emu` exits 0 even when it answers `KO`, so a restore that did not happen would otherwise be reported as success and the next test would run against unknown state.
    - Options: `--list`, `--save NAME`, `--load NAME`, `--delete NAME`, `--no-verify`, `--timeout`, `--serial`, `--json`, `--verbose`

35. **crash_triage.py** ⭐ NEW - Parse the dedicated crash buffer (`logcat -b crash`) into structured crashes, grouped by fault.
    - Groups repeats by package + exception + signature frame (a crash loop otherwise floods the output), and names the frame most useful for triage, **labelled with the basis for the choice** — including "no app frame; every frame is framework code" when that is the honest answer.
    - Exit status answers "did triage run", not "did anything crash" — branch on `crash_count` in `--json`, or pass `--fail-on-crash`.
    - Options: `--package PKG`, `--clear`, `--fail-on-crash`, `--serial`, `--json`, `--verbose`

36. **logs.py** ⭐ NEW - One entry point for reading logs; routes on the question being asked.
    - `logs.py tail` (main buffers) → `log_monitor.py`; `logs.py crashes` (`-b crash`) → `crash_triage.py`; `logs.py anr` (ANR/jank, incl. session mode) → `anr_watcher.py`.
    - Arguments are passed through verbatim, so each verb takes the full flag set of the script it delegates to, and those scripts remain callable unchanged.
    - Options: `<verb> [verb options]`, `--json` (routing table), `--help`

> The build system lives in the `gradle/` subpackage (`builder`, `results`, `cache`, `config`, `reporter`), used by `build_and_test.py`. The ANR watcher's clustering/session machinery lives in `common/anr_pipeline.py` and `common/anr_sessions.py`.

## Android vs iOS Mapping

| iOS Tool | Android Equivalent | Status |
|----------|-------------------|--------|
| xcrun simctl | adb / avdmanager / emulator | ✓ Complete |
| IDB | adb shell uiautomator / input | ✓ Complete |
| iOS Simulator | Android Emulator | ✓ Complete |
| xcodebuild | Gradle wrapper | ✓ Complete |
| Accessibility tree | UI hierarchy dump | ✓ Complete |
| simctl privacy | pm grant/revoke | ✓ Complete |
| xcresult | Gradle test reports / JUnit XML | ⚠ Basic (being improved) |

## Script Categories

### 🚀 Essential (Use Daily)
- **app_launcher.py** - Launch/terminate apps
- **screen_mapper.py** - Understand current screen
- **navigator.py** - Interact with UI elements
- **gesture.py** / **keyboard.py** - User input

### 🔧 Development
- **build_and_test.py** - Build projects and run tests
- **log_monitor.py** - Debug with filtered logs
- **emulator_boot.py** / **emulator_shutdown.py** - Device management

### 🧪 Testing
- **accessibility_audit.py** - Check accessibility compliance
- **visual_diff.py** - Visual regression testing
- **test_recorder.py** - Document test execution
- **app_state_capture.py** - Debug test failures

### ⚙️ Advanced
- **privacy_manager.py** - Test permission flows
- **push_notification.py** - Test notification handling
- **status_bar.py** - Fine-grained control
- **emulator_create/delete/erase.py** - CI/CD provisioning
- **sms.py** - Inbound SMS, and OTP login flows end to end (emulator-only)
- **snapshot.py** - ~2s state reset between tests (emulator-only)
- **crash_triage.py** - Structured crashes from the dedicated crash buffer

## Typical Workflows

### Manual Testing Flow
```bash
# 1. Launch app
python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app

# 2. See what's on screen
python3 "$SKILL_DIR/scripts/screen_mapper.py"

# 3. Interact
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --index 0 --enter-text "user@test.com"
python3 "$SKILL_DIR/scripts/keyboard.py" --button enter

# 4. Verify
python3 "$SKILL_DIR/scripts/screen_mapper.py"
```

### Automated Testing Flow
```bash
# 1. Start recording
recorder = TestRecorder("Login Flow")

# 2. Execute test steps
recorder.step("Launch app", screen_name="Splash")
# ... interactions ...
recorder.step("Verify logged in", screen_name="Home")

# 3. Finish
recorder.finish(passed=True)
```

### CI/CD Flow
```bash
# 1. Create fresh emulator
python3 "$SKILL_DIR/scripts/emulator_create.py" --device pixel_7 --api 34 --name test-device

# 2. Boot emulator
python3 "$SKILL_DIR/scripts/emulator_boot.py" --avd test-device --wait-ready

# 3. Build and test
python3 "$SKILL_DIR/scripts/build_and_test.py" --project . --test

# 4. Run UI tests
# ... your test scripts ...

# 5. Cleanup
python3 "$SKILL_DIR/scripts/emulator_shutdown.py" --serial emulator-5554
python3 "$SKILL_DIR/scripts/emulator_delete.py" --name test-device
```

### Debugging Flow
```bash
# 1. Capture complete state
python3 "$SKILL_DIR/scripts/app_state_capture.py" --package com.myapp --output debug-snapshots/

# 2. Monitor logs in real-time
python3 "$SKILL_DIR/scripts/log_monitor.py" --app com.myapp --severity error,warning --follow

# 3. Check accessibility issues
python3 "$SKILL_DIR/scripts/accessibility_audit.py" --output audit-reports/ --verbose
```

## Requirements

- macOS, Linux, or Windows
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
# Personal installation
git clone <repository-url> ~/.claude/skills/android-emulator-skill

# Project installation
git clone <repository-url> .claude/skills/android-emulator-skill
```

## Documentation

- **SKILL.md** (this file) - the script reference; the accurate inventory
- **README.md** - short orientation for humans
- **examples/** - complete automation workflows (ships with the package)

In the source repository only:

- **CLAUDE.md** - architecture, conventions, and the recorded-fixture policy
- **references/** - deep dives on adb, test patterns, accessibility
  (not included in the released package)

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

`clipboard.py` was removed in v0.5.0 for this reason: its surviving code path
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

## Key Design Principles

**Semantic Navigation**: Find elements by meaning (text, type, ID) not pixel coordinates. Survives UI changes.

**Token Efficiency**: Concise default output (3-5 lines), with `--verbose` for
human detail and `--json` for machine-readable output. The reduction versus
piping raw tool output is large but has not been measured; a budget table is
planned rather than an invented percentage.

**Accessibility-First**: Built on standard accessibility APIs for reliability and compatibility.

**Zero Configuration**: Works immediately with Android SDK installed. No complex setup required.

**Structured Data**: Scripts output JSON or formatted text, not raw logs. Easy to parse and integrate.

**Cross-Platform**: Works on macOS, Linux, and Windows.

**Real Devices**: Unlike iOS, works with both emulators and real devices.

## Android-Specific Features

**Real Device Support**: Works with both emulators and physical devices connected via USB/WiFi.

**Multiple Emulators**: Support for running multiple emulators simultaneously with batch operations.

**Flexible Architecture**: Works with both x86_64 and ARM emulator architectures.

**Gradle Integration**: Native integration with Android's Gradle build system.

**Advanced Logging**: Logcat filtering with regex, severity, and app-specific targeting.

**Permission Testing**: Programmatic grant/revoke of runtime permissions.

**Notification Testing**: Simulate push notifications for testing handling logic.

## Status & Roadmap

**Under active repair, not feature work.** An audit found that several scripts
were written against *assumed* tool output — and their tests were written
against the same assumption, so the suite stayed green while the script did
nothing. Some called Android commands that do not exist.

The correction is fixture-driven: `tests/fixtures/recorded/` holds verbatim
output captured from real devices, parser tests consume it, and known defects
are pinned with `xfail(strict=True)` so fixing one forces the marker's removal.
See `CLAUDE.md` in the source repository.

**Fixed so far:** Gradle failures now report a diagnostic instead of
"0 errors"; errored tests no longer count as a pass; `log_monitor` parses
device output at all and `--duration` terminates; `anr_watcher --all` no longer
returns 3 clusters and delete the rest from disk; the focused-activity lookup
works; cache ids can no longer address files outside the cache directory;
selector history no longer writes into the installed package; arguments
crossing into the device shell are quoted; `screen_mapper` sees Jetpack Compose
screens; touch targets are measured in dp against the device's real density;
display overrides (`wm size` / `wm density`) are honoured, so coordinates are
right while one is active; every script reads the screen through one
implementation that writes no temp files, so concurrent runs and multi-device
runs can no longer read each other's screen; `test_recorder` records again
(screenshots, UI dumps and a Markdown report per step); `push_notification` is
rescoped to what adb can actually do — post to the shade, and read back what a
package really posted — instead of broadcasting to a receiver class this skill
invented.

**Known-broken, being worked:**

- `accessibility_audit.py` — no contrast check, despite the script's own
  description mentioning one. Contrast needs pixel sampling from a screenshot,
  which is not implemented.
- AVD management needs `cmdline-tools`; the legacy `tools/bin` copies cannot run
  on Java 11+ (they use JAXB, removed in Java 11).

Removed rather than repaired: `clipboard.py` — see "Platform limitations"
above; writing the clipboard is not reachable from adb on any modern Android,
so the script could not be made to do what it advertised.

## Contributing

New scripts should:
- Use class-based design for > 50 lines of logic
- Support --serial and auto-detection
- Support --json output
- Provide --help documentation
- Follow Black and Ruff standards
- Update this SKILL.md
- Test with real emulators before submission

## Differences from iOS

### Architecture
- **iOS**: Uses IDB for UI interaction, xcrun simctl for device management
- **Android**: Uses adb for everything, uiautomator for UI interaction

### Element Types
- **iOS**: Button, TextField, SecureTextField, StaticText, etc.
- **Android**: Button, EditText, TextView, ImageView, etc.

### Device Management
- **iOS**: Simulators only (macOS required)
- **Android**: Emulators + real devices (cross-platform)

### Build System
- **iOS**: Xcode project files (.xcodeproj)
- **Android**: Gradle build files (build.gradle)

---

**Status**: Under active modernization toward parity with [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill). See the "Status & Roadmap" section above.

Use these scripts directly or let Claude Code invoke them automatically when your request matches the skill description.
