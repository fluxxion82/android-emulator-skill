# Testing Guide for Android Emulator Skill

This guide provides practical ways to test the Android skill scripts.

## Quick Start Testing

### Option 1: Test with Android Settings App (Easiest)

The Settings app is pre-installed on all Android devices and has lots of interactive elements.

```bash
# 1. Start emulator (if not running)
emulator -avd Pixel_5_API_33 &

# Or use our boot script
python skill/scripts/emulator_boot.py --avd Pixel_5_API_33 --wait-ready

# 2. Launch Settings app
python skill/scripts/app_launcher.py --launch com.android.settings

# 3. Map the screen
python skill/scripts/screen_mapper.py

# Expected output:
# Screen: Settings (50+ elements, 15+ interactive)
# Buttons: "Network & internet", "Connected devices", "Apps"
# ...

# 4. Find and tap an element
python skill/scripts/navigator.py --find-text "Apps" --tap

# 5. Go back
python skill/scripts/keyboard.py --button back

# 6. Scroll down
python skill/scripts/gesture.py --swipe up --count 3

# 7. Take screenshot
adb shell screencap -p /sdcard/test.png
adb pull /sdcard/test.png
```

### Option 2: Test with Calculator App

```bash
# Launch calculator
python skill/scripts/app_launcher.py --launch com.android.calculator2

# Map screen
python skill/scripts/screen_mapper.py

# Tap buttons by text
python skill/scripts/navigator.py --find-text "5" --tap
python skill/scripts/navigator.py --find-text "+" --tap
python skill/scripts/navigator.py --find-text "3" --tap
python skill/scripts/navigator.py --find-text "=" --tap
```

### Option 3: Test with a Sample App

Use Google's sample apps:

```bash
# Clone sample app
git clone https://github.com/android/sunflower.git
cd sunflower

# Build APK
./gradlew assembleDebug

# Install
python ../android-skill/skill/scripts/app_launcher.py --install app/build/outputs/apk/debug/app-debug.apk

# Launch
python ../android-skill/skill/scripts/app_launcher.py --launch com.google.samples.apps.sunflower
```

## Systematic Testing Plan

### Phase 1: Environment & Device Detection

```bash
# Test 1: Check ADB connection
adb devices
# Expected: List of connected devices

# Test 2: Device detection
python skill/scripts/common/device_utils.py
# Should list devices without errors

# Test 3: Screen size detection
python -c "
from skill.scripts.common.device_utils import get_device_screen_size
size = get_device_screen_size()
print(f'Screen: {size[0]}x{size[1]}')
"
```

### Phase 2: App Lifecycle

```bash
# Test 4: List installed packages
python skill/scripts/app_launcher.py --list | grep "settings"

# Test 5: Launch app
python skill/scripts/app_launcher.py --launch com.android.settings

# Test 6: Check app state
python skill/scripts/app_launcher.py --state com.android.settings
# Expected: installed=True, running=True, foreground=True

# Test 7: Terminate app
python skill/scripts/app_launcher.py --terminate com.android.settings

# Test 8: Verify terminated
python skill/scripts/app_launcher.py --state com.android.settings
# Expected: running=False
```

### Phase 3: Screen Analysis

```bash
# Test 9: Launch settings
python skill/scripts/app_launcher.py --launch com.android.settings

# Test 10: Map screen (default)
python skill/scripts/screen_mapper.py

# Test 11: Map screen (verbose)
python skill/scripts/screen_mapper.py --verbose

# Test 12: Map screen (JSON)
python skill/scripts/screen_mapper.py --json > screen_map.json
cat screen_map.json | python -m json.tool

# Test 13: With hints
python skill/scripts/screen_mapper.py --hints
```

### Phase 4: Element Navigation

```bash
# Test 14: Find element by text
python skill/scripts/navigator.py --find-text "Apps"
# Expected: Found: Button "Apps" at (x, y)

# Test 15: Find and tap
python skill/scripts/navigator.py --find-text "Apps" --tap
# Expected: Tapped: Button "Apps" at (x, y)

# Test 16: Go back
python skill/scripts/keyboard.py --button back

# Test 17: Find by type
python skill/scripts/navigator.py --find-type TextView --index 0
# Expected: Found: TextView "..." at (x, y)

# Test 18: List all elements
python skill/scripts/navigator.py --list
# Expected: List of interactive elements

# Test 19: JSON output
python skill/scripts/navigator.py --find-text "Apps" --json
```

### Phase 5: Gestures

```bash
# Test 20: Swipe up (scroll down)
python skill/scripts/gesture.py --swipe up

# Test 21: Swipe down (scroll up)
python skill/scripts/gesture.py --swipe down

# Test 22: Swipe from edge (back gesture)
python skill/scripts/gesture.py --swipe right --from-edge

# Test 23: Multi-scroll
python skill/scripts/gesture.py --scroll up --count 3

# Test 24: Long press
python skill/scripts/gesture.py --long-press 540,960 --duration 1000

# Test 25: Custom swipe
python skill/scripts/gesture.py --swipe-path 100,500,900,500 --duration 500
```

### Phase 6: Keyboard Input

```bash
# Test 26: Launch app with search
python skill/scripts/app_launcher.py --launch com.android.settings

# Test 27: Press search key
python skill/scripts/keyboard.py --key search

# Test 28: Type text
python skill/scripts/keyboard.py --type "Display"

# Test 29: Press enter
python skill/scripts/keyboard.py --key enter

# Test 30: Go back
python skill/scripts/keyboard.py --button back
```

### Phase 7: Complete Workflow

```bash
# Test 31: Full interaction flow
python skill/scripts/app_launcher.py --launch com.android.settings
python skill/scripts/screen_mapper.py
python skill/scripts/navigator.py --find-text "Network" --tap
python skill/scripts/keyboard.py --button back
python skill/scripts/gesture.py --swipe up
python skill/scripts/app_launcher.py --terminate com.android.settings
```

## Automated Test Script

Create a test runner script:

```bash
cat > android-skill/test_basic.sh << 'EOF'
#!/bin/bash
set -e

echo "=== Android Skill Basic Tests ==="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASS=0
FAIL=0
SKIP=0

test_case() {
    local name="$1"
    shift
    echo -n "Testing: $name ... "

    if "$@" > /tmp/test_output.txt 2>&1; then
        echo -e "${GREEN}PASS${NC}"
        ((PASS++))
        return 0
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Output: $(cat /tmp/test_output.txt | head -1)"
        ((FAIL++))
        return 1
    fi
}

# Check prerequisites
echo "Checking prerequisites..."
if ! command -v adb &> /dev/null; then
    echo -e "${RED}Error: adb not found${NC}"
    exit 1
fi

DEVICES=$(adb devices | grep -v "List" | grep "device$" | wc -l)
if [ "$DEVICES" -eq 0 ]; then
    echo -e "${RED}Error: No devices connected${NC}"
    echo "Start an emulator first: emulator -avd <name>"
    exit 1
fi

echo -e "${GREEN}Prerequisites OK${NC}"
echo ""

# Navigate to skill directory
cd "$(dirname "$0")/skill/scripts"

# Test 1: Device detection
test_case "Device detection" python -c "from common.device_utils import get_connected_devices; assert len(get_connected_devices()) > 0"

# Test 2: Launch Settings
test_case "Launch Settings app" python app_launcher.py --launch com.android.settings

# Test 3: Check app state
test_case "Check app state" python app_launcher.py --state com.android.settings --json

# Test 4: Screen mapping
test_case "Screen mapping" python screen_mapper.py

# Test 5: Find element
test_case "Find element by text" python navigator.py --find-text "Apps"

# Test 6: List elements
test_case "List all elements" python navigator.py --list

# Test 7: Swipe gesture
test_case "Swipe gesture" python gesture.py --swipe up

# Test 8: Press back button
test_case "Press back button" python keyboard.py --button back

# Test 9: Terminate app
test_case "Terminate app" python app_launcher.py --terminate com.android.settings

# Results
echo ""
echo "=== Test Results ==="
echo -e "Passed: ${GREEN}${PASS}${NC}"
echo -e "Failed: ${RED}${FAIL}${NC}"
echo -e "Skipped: ${YELLOW}${SKIP}${NC}"
echo -e "Total: $((PASS + FAIL + SKIP))"
echo ""

if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed${NC}"
    exit 1
fi
EOF

chmod +x android-skill/test_basic.sh
```

Run the automated tests:
```bash
cd android-skill
./test_basic.sh
```

## Visual Testing

Test screenshot and visual diff:

```bash
# 1. Take baseline screenshot
python skill/scripts/app_launcher.py --launch com.android.settings
adb shell screencap -p /sdcard/baseline.png
adb pull /sdcard/baseline.png

# 2. Make a change (scroll)
python skill/scripts/gesture.py --swipe up

# 3. Take new screenshot
adb shell screencap -p /sdcard/after_scroll.png
adb pull /sdcard/after_scroll.png

# 4. Compare
python skill/scripts/visual_diff.py baseline.png after_scroll.png --threshold 0.05 --details

# Expected: Shows difference percentage and changed regions
```

## Testing with a Real App

### Example: Testing a Login Flow

1. **Create a test APK** or use an existing app
2. **Install it:**
   ```bash
   python skill/scripts/app_launcher.py --install /path/to/app.apk
   ```

3. **Test the login flow:**
   ```bash
   # Launch
   python skill/scripts/app_launcher.py --launch com.example.app

   # Map screen
   python skill/scripts/screen_mapper.py --hints

   # Enter username
   python skill/scripts/navigator.py --find-type EditText --index 0 --enter-text "testuser"

   # Enter password
   python skill/scripts/navigator.py --find-type EditText --index 1 --enter-text "password123"

   # Tap login
   python skill/scripts/navigator.py --find-text "Login" --tap

   # Wait and verify
   sleep 2
   python skill/scripts/screen_mapper.py
   ```

## Debugging Failed Tests

### Common Issues

1. **"No devices connected"**
   ```bash
   # Check ADB
   adb devices

   # Restart ADB server
   adb kill-server
   adb start-server

   # Start emulator
   emulator -list-avds
   emulator -avd <name>
   ```

2. **"Element not found"**
   ```bash
   # Check what's actually on screen
   python skill/scripts/screen_mapper.py --verbose

   # List all elements
   python skill/scripts/navigator.py --list

   # Try fuzzy matching (default) vs exact
   python skill/scripts/navigator.py --find-text "Apps" --tap  # Fuzzy
   python skill/scripts/navigator.py --find-text "Apps" --exact --tap  # Exact
   ```

3. **"UI dump failed"**
   ```bash
   # Try manually
   adb shell uiautomator dump
   adb pull /sdcard/window_dump.xml
   cat window_dump.xml

   # Check UIAutomator service
   adb shell "pm list packages | grep uiautomator"
   ```

4. **"Permission denied"**
   ```bash
   # Check device permissions
   adb shell ls -la /sdcard/

   # Some devices need this
   adb shell "pm grant com.android.shell android.permission.WRITE_EXTERNAL_STORAGE"
   ```

## Performance Testing

Measure token efficiency:

```bash
# Raw adb output
adb shell uiautomator dump
adb pull /sdcard/window_dump.xml
wc -l window_dump.xml
# Example: 847 lines

# Our screen mapper
python skill/scripts/screen_mapper.py | wc -l
# Example: 6 lines

# Token reduction: 99.3%!
```

## Integration Testing

Test with Claude Code:

```markdown
You: "Open Android settings and tap on Apps"

Claude: [Uses screen_mapper.py and navigator.py automatically]
Done! Tapped on Apps in Settings.

You: "Go back and scroll down"

Claude: [Uses keyboard.py and gesture.py]
Done! Went back and scrolled down.
```

## Continuous Testing

Add to CI/CD:

```yaml
# .github/workflows/test-android-skill.yml
name: Test Android Skill

on: [push, pull_request]

jobs:
  test:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v2

      - name: Set up Android SDK
        uses: android-actions/setup-android@v2

      - name: Create and start emulator
        run: |
          echo "no" | avdmanager create avd -n test -k "system-images;android-33;google_apis;x86_64"
          emulator -avd test -no-window -no-audio &
          adb wait-for-device

      - name: Run tests
        run: |
          cd android-skill
          ./test_basic.sh
```

## Success Criteria

Your Android skill is working if:

✅ **Device Detection**: Can list connected devices
✅ **App Launch**: Can launch Settings app
✅ **Screen Analysis**: Maps screen with 5-7 line summary
✅ **Element Finding**: Finds "Apps" button
✅ **Interaction**: Successfully taps elements
✅ **Gestures**: Swipes work without errors
✅ **Text Input**: Can type text
✅ **Token Efficiency**: Output is <10 lines by default

## Next Steps After Testing

1. **Fix any bugs** discovered during testing
2. **Optimize** element finding algorithms
3. **Add error handling** for edge cases
4. **Create more examples** in skill/examples/
5. **Document common patterns** in references/

---

**Quick Test**: Just want to see if it works?
```bash
emulator -avd Pixel_5_API_33 &
sleep 30
python skill/scripts/app_launcher.py --launch com.android.settings
python skill/scripts/screen_mapper.py
python skill/scripts/navigator.py --find-text "Apps" --tap
```

If those commands work, your skill is functional! 🎉
