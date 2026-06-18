---
name: android-emulator-skill
version: 0.4.0
description: Production-ready scripts for Android app testing, building, and automation. Provides semantic UI navigation, build automation, accessibility testing, and emulator lifecycle management. Optimized for AI agents with minimal token output. Android equivalent of ios-simulator-skill.
---

# Android Emulator Skill

Build, test, and automate Android applications using accessibility-driven navigation and structured data instead of pixel coordinates.

## Quick Start

```bash
# 1. Launch app
python scripts/app_launcher.py --launch com.example.app

# 2. Map screen to see elements
python scripts/screen_mapper.py

# 3. Tap button
python scripts/navigator.py --find-text "Login" --tap

# 4. Enter text
python scripts/navigator.py --find-type EditText --enter-text "user@example.com"

# 5. Run accessibility audit
python scripts/accessibility_audit.py
```

All scripts support `--help` for detailed options and `--json` for machine-readable output.

## Scripts (v0.4.0)

### Implemented (29 scripts)

#### Core Utilities (3 modules)
1. **common/device_utils.py** - ADB command building and device detection
2. **common/screenshot_utils.py** - Screenshot capture and processing
3. **common/cache_utils.py** - Progressive disclosure cache system

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
    - Options: `--find-text`, `--find-type`, `--find-id`, `--tap`, `--enter-text`, `--list`, `--serial`, `--json`

14. **gesture.py** - Perform swipes, scrolls, long press
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
    - Options: `--image1`, `--image2`, `--output`, `--threshold`, `--json`

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

21. **clipboard.py** ⭐ NEW - Clipboard management
    - Copy text to device clipboard
    - Test paste functionality
    - Options: `--copy`, `--paste`, `--serial`, `--json`

22. **status_bar.py** ⭐ NEW - Status bar control
    - Set battery level and charging state
    - Set WiFi/mobile signal strength
    - Set time display (for consistent screenshots)
    - Demo mode support
    - Options: `--battery`, `--charging`, `--wifi`, `--mobile`, `--time`, `--reset`, `--serial`, `--json`

23. **push_notification.py** ⭐ NEW - Push notification simulation
    - Send test notifications
    - List notification channels
    - Multiple delivery methods
    - Options: `--package`, `--title`, `--message`, `--id`, `--data`, `--list-channels`, `--method`, `--serial`, `--json`

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
- **clipboard.py** / **status_bar.py** - Fine-grained control
- **emulator_create/delete/erase.py** - CI/CD provisioning

## Typical Workflows

### Manual Testing Flow
```bash
# 1. Launch app
python scripts/app_launcher.py --launch com.example.app

# 2. See what's on screen
python scripts/screen_mapper.py

# 3. Interact
python scripts/navigator.py --find-text "Login" --tap
python scripts/navigator.py --find-type EditText --index 0 --enter-text "user@test.com"
python scripts/keyboard.py --button enter

# 4. Verify
python scripts/screen_mapper.py
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
python scripts/emulator_create.py --device pixel_7 --api 34 --name test-device

# 2. Boot emulator
python scripts/emulator_boot.py --avd test-device --wait-ready

# 3. Build and test
python scripts/build_and_test.py --project . --test

# 4. Run UI tests
# ... your test scripts ...

# 5. Cleanup
python scripts/emulator_shutdown.py --serial emulator-5554
python scripts/emulator_delete.py --name test-device
```

### Debugging Flow
```bash
# 1. Capture complete state
python scripts/app_state_capture.py --package com.myapp --output debug-snapshots/

# 2. Monitor logs in real-time
python scripts/log_monitor.py --app com.myapp --severity error,warning --follow

# 3. Check accessibility issues
python scripts/accessibility_audit.py --output audit-reports/ --verbose
```

## Requirements

- macOS, Linux, or Windows
- Android SDK with platform-tools and emulator
- Python 3.8+
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

- **SKILL.md** (this file) - Script reference and quick start
- **README.md** - Installation and examples
- **CLAUDE.md** - Architecture and implementation details
- **STATUS.md** - Project status and roadmap
- **TESTING.md** - Testing guide
- **references/** - Deep documentation on specific topics
- **examples/** - Complete automation workflows

## Key Design Principles

**Semantic Navigation**: Find elements by meaning (text, type, ID) not pixel coordinates. Survives UI changes.

**Token Efficiency**: Concise default output (3-5 lines) with optional verbose and JSON modes for detailed results. 96% reduction vs raw tools.

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

This skill is **actively being modernized** to track the upstream
[`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill), which has since grown well beyond
this mirror. Treat the script list above as the **currently implemented baseline**, not a finished product.

**Implemented today:** app lifecycle, emulator lifecycle (create/boot/shutdown/erase/delete), semantic
navigation, gestures, keyboard, Gradle build/test, logcat monitoring, accessibility audit, visual diff, test
recording, state capture, permissions, clipboard, status bar, push notifications, environment health check,
device/AVD discovery + selector, localization audit (`res/values` strings), appearance (dark mode / font
scale), location (`adb emu geo fix` + GPX route replay), a Gradle build subpackage with progressive disclosure,
a debuggable-app container inspector, a Room/SQLite persistence inspector, and an ANR/jank watcher.

**Parity status:** the targeted iOS v1.5 feature set has been ported to Android-native equivalents. iOS-only
mechanics (os_log hang predicates, HangBuster raw-capture/symbolication, xcresult specifics) are intentionally
not mirrored; some controls are rescoped to Android realities (appearance locale is best-effort, container
inspection is debuggable-apps-only, location is emulator-first).

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
