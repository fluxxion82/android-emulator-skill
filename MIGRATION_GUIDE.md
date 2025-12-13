# iOS to Android Skill Migration Guide

This document outlines what has been completed and what can be reused/adapted from the iOS Simulator Skill when implementing remaining Android scripts.

## Status Overview

### ✅ Completed (v0.1.0)

1. **Core Utilities** (3/3)
   - `common/__init__.py` - Module exports
   - `common/device_utils.py` - Android/ADB version (500+ lines)
   - `common/screenshot_utils.py` - Android/ADB version (350+ lines)
   - `common/cache_utils.py` - Reused from iOS with path changes

2. **App Management** (1/1)
   - `app_launcher.py` - Complete with launch, terminate, install, uninstall, state checking

3. **Emulator Lifecycle** (2/5)
   - `emulator_boot.py` - Complete with AVD boot and readiness checking
   - `emulator_shutdown.py` - Complete with verification

4. **Documentation** (3/3)
   - `SKILL.md` - Complete reference
   - `README.md` - Installation and usage guide
   - `CLAUDE.md` - Developer guide

## What Can Be Reused

### 100% Reusable (Copy & Use)

These files can be copied directly from iOS with minimal changes:

1. **visual_diff.py**
   - Pure PIL/image comparison logic
   - No platform-specific code
   - Just update imports if needed

2. **Architectural Patterns**
   - Class-based design
   - Output formatting (default/verbose/json)
   - Error handling patterns
   - Argparse structure

### 80% Reusable (Adapt Commands)

These files need command changes but logic is reusable:

#### Navigation Scripts

**iOS: navigator.py → Android: navigator.py**
```python
# iOS uses IDB
cmd = ["idb", "ui", "describe-all", "--json"]
# Change to Android uiautomator
cmd = ["adb", "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"]
```

Key changes:
- Replace IDB commands with adb uiautomator
- Parse XML instead of JSON
- Element types: Button→Button, TextField→EditText
- Keep: Element finding logic, fuzzy matching, tap coordinate calculation

**iOS: gesture.py → Android: gesture.py**
```python
# iOS uses IDB
cmd = ["idb", "ui", "swipe", str(x1), str(y1), str(x2), str(y2)]
# Change to Android input
cmd = ["adb", "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
```

**iOS: keyboard.py → Android: keyboard.py**
```python
# iOS uses IDB
cmd = ["idb", "ui", "text", text]
# Change to Android input
cmd = ["adb", "shell", "input", "text", text.replace(" ", "%s")]
```

#### Testing Scripts

**iOS: test_recorder.py → Android: test_recorder.py**
- Reuse recording flow and markdown generation
- Change screenshot capture to use Android method
- Change UI hierarchy dump to use uiautomator

**iOS: app_state_capture.py → Android: app_state_capture.py**
- Reuse snapshot structure
- Change log capture: `log show` → `adb logcat -d`
- Change device info queries

### 50% Reusable (Major Changes)

**iOS: build_and_test.py → Android: gradle_build.py**

Needs significant changes:
- Xcode project detection → Gradle project detection
- xcodebuild commands → gradle/gradlew commands
- xcresult parsing → gradle test XML/HTML parsing
- Build log parsing different format

Keep:
- Progressive disclosure pattern
- Error extraction logic patterns
- Output formatting

**iOS: accessibility_audit.py → Android: accessibility_audit.py**

Similar accessibility concepts but different APIs:
- iOS accessibility traits → Android contentDescription/accessibility properties
- Similar audit rules (missing labels, small targets)
- Different element hierarchy structure

**iOS: log_monitor.py → Android: log_monitor.py**

Different log systems:
- `log show --predicate` → `adb logcat`
- iOS log levels → Android log levels (V/D/I/W/E/F)
- Different filtering syntax

Keep:
- Deduplication logic
- Severity filtering approach
- Output formatting

## Implementation Priority

### Phase 1: Complete Lifecycle (Remaining 3 scripts)

**emulator_create.py**
```bash
# Uses avdmanager
avdmanager create avd -n <name> -k "system-images;android-33;google_apis;x86_64"
```

**emulator_delete.py**
```bash
# Uses avdmanager
avdmanager delete avd -n <name>
```

**emulator_erase.py**
```bash
# Uses emulator with wipe flag
emulator -avd <name> -wipe-data
```

Reference: iOS `simctl_create/delete/erase.py` for:
- Argument parsing patterns
- List operations
- Batch operations
- Safety confirmations

### Phase 2: Navigation Scripts (Core Value)

Priority order:
1. **screen_mapper.py** - Foundation for other scripts
2. **navigator.py** - Core interaction tool
3. **gesture.py** - Touch interactions
4. **keyboard.py** - Text input

### Phase 3: Build & Testing

1. **gradle_build.py** - Essential for CI/CD
2. **log_monitor.py** - Debugging tool
3. **test_recorder.py** - Test documentation
4. **app_state_capture.py** - Debugging snapshots

### Phase 4: Advanced Features

1. **accessibility_audit.py**
2. **clipboard.py**
3. **status_bar.py**
4. **push_notification.py**
5. **privacy_manager.py**

## Key Differences to Remember

### Command Structure

| Operation | iOS | Android |
|-----------|-----|---------|
| Device list | `xcrun simctl list` | `adb devices` |
| Boot | `xcrun simctl boot <udid>` | `emulator -avd <name>` |
| Screenshot | `xcrun simctl io screenshot` | `adb shell screencap` |
| UI dump | `idb ui describe-all --json` | `adb shell uiautomator dump` |
| Tap | `idb ui tap x y` | `adb shell input tap x y` |
| Type text | `idb ui text "hello"` | `adb shell input text "hello"` |
| Install | `xcrun simctl install` | `adb install` |
| Launch | `xcrun simctl launch` | `adb shell am start` |
| Logs | `log show --predicate` | `adb logcat` |

### Element Types

| iOS | Android |
|-----|---------|
| Button | Button |
| TextField | EditText |
| SecureTextField | EditText (inputType=password) |
| StaticText | TextView |
| Image | ImageView |
| ScrollView | ScrollView |
| Table | RecyclerView/ListView |
| Cell | RecyclerView.ViewHolder |
| Switch | Switch |
| Slider | SeekBar |

### Accessibility Properties

| iOS | Android |
|-----|---------|
| accessibilityLabel | contentDescription |
| accessibilityValue | text |
| accessibilityIdentifier | android:id |
| isAccessibilityElement | importantForAccessibility |
| accessibilityTraits | (various properties) |

## File Reuse Strategy

### Direct Copy
```bash
# From ios-simulator-skill/skill/scripts/
cp visual_diff.py ../android-skill/skill/scripts/
```

### Copy and Adapt
```bash
# Copy then modify commands
cp screen_mapper.py ../android-skill/skill/scripts/
# Then update:
# - Import device_utils from common
# - Replace IDB commands with adb uiautomator
# - Update element type names
# - Update parsing logic for XML vs JSON
```

### Reference for Patterns
```bash
# Don't copy, but reference for:
# - Class structure
# - Argument parsing
# - Output formatting
# - Error handling
```

## Testing Checklist

When porting scripts, test with:

- [ ] Android emulator (x86_64)
- [ ] Real Android device (if applicable)
- [ ] Different API levels (21, 28, 33, 34)
- [ ] Different screen sizes (phone, tablet)
- [ ] Batch operations (if supported)
- [ ] JSON output mode
- [ ] Error conditions
- [ ] Missing dependencies

## Common Pitfalls

1. **Shell Escaping**: Android `adb shell` requires different escaping than direct commands
2. **Async Operations**: Emulator boot is async, always wait for boot completion
3. **Multiple Devices**: Android commonly has multiple devices connected (emulators + physical)
4. **Architecture**: x86 vs ARM emulators have different performance characteristics
5. **Permissions**: Real devices may require additional permissions vs emulators

## Next Steps

1. **Immediate**: Complete lifecycle scripts (create/delete/erase)
2. **High Priority**: Implement navigation scripts (screen_mapper, navigator)
3. **Medium Priority**: Build and testing scripts
4. **Lower Priority**: Advanced features

Focus on navigation scripts as they provide the most value - semantic element finding and interaction is the core differentiator from raw adb commands.

## Resources

- **Android Developer Docs**: https://developer.android.com/
- **ADB Documentation**: https://developer.android.com/tools/adb
- **UI Automator**: https://developer.android.com/training/testing/other-components/ui-automator
- **Gradle**: https://gradle.org/
- **iOS Skill Reference**: ../skill/scripts/

---

**Remember**: The goal is semantic, accessibility-driven automation with token-efficient output. Keep these principles when adapting from iOS to Android.
