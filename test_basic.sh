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
    echo "Install Android SDK and add platform-tools to PATH"
    exit 1
fi

DEVICES=$(adb devices | grep -v "List" | grep "device$" | wc -l)
if [ "$DEVICES" -eq 0 ]; then
    echo -e "${RED}Error: No devices connected${NC}"
    echo "Start an emulator first:"
    echo "  emulator -avd <name>"
    echo "Or connect a physical device"
    exit 1
fi

echo -e "${GREEN}Prerequisites OK${NC}"
echo "Found $DEVICES device(s)"
echo ""

# Navigate to skill directory
cd "$(dirname "$0")/skill/scripts"

echo "Running tests..."
echo ""

# Test 1: Device detection
test_case "Device detection" python3 -c "from common.device_utils import get_connected_devices; devices = get_connected_devices(); assert len(devices) > 0, 'No devices found'; print(f'Found {len(devices)} device(s)')"

# Test 2: Launch Settings
test_case "Launch Settings app" python3 app_launcher.py --launch com.android.settings

# Small delay for app to start
sleep 1

# Test 3: Check app state
test_case "Check app state" python3 app_launcher.py --state com.android.settings --json

# Test 4: Screen mapping (default)
test_case "Screen mapping (default)" python3 screen_mapper.py

# Test 5: Screen mapping (JSON)
test_case "Screen mapping (JSON)" python3 screen_mapper.py --json

# Test 6: Find element by text
test_case "Find element by text" python3 navigator.py --find-text "Apps"

# Test 7: List elements
test_case "List all elements" python3 navigator.py --list

# Test 8: Find and tap (if Apps exists)
if python3 navigator.py --find-text "Apps" > /dev/null 2>&1; then
    test_case "Tap element" python3 navigator.py --find-text "Apps" --tap
    sleep 1
    test_case "Press back button" python3 keyboard.py --button back
else
    echo -e "Testing: Tap element ... ${YELLOW}SKIP${NC} (Apps button not found)"
    ((SKIP++))
fi

# Test 9: Swipe gesture
test_case "Swipe up gesture" python3 gesture.py --swipe up

# Test 10: Swipe down gesture
test_case "Swipe down gesture" python3 gesture.py --swipe down

# Test 11: Keyboard - type text (need focused field, so we'll test key press)
test_case "Press home button" python3 keyboard.py --button home

# Return to Settings
python3 app_launcher.py --launch com.android.settings > /dev/null 2>&1
sleep 1

# Test 12: Terminate app
test_case "Terminate app" python3 app_launcher.py --terminate com.android.settings

# Test 13: Verify terminated
test_case "Verify app terminated" python3 -c "
import json, sys
from subprocess import run
result = run(['python', 'app_launcher.py', '--state', 'com.android.settings', '--json'], capture_output=True, text=True)
state = json.loads(result.stdout)
assert state.get('running') == False or state.get('foreground') == False, f'App still running: {state}'
print('App correctly terminated')
"

# Results
echo ""
echo "=== Test Results ==="
echo -e "Passed: ${GREEN}${PASS}${NC}"
echo -e "Failed: ${RED}${FAIL}${NC}"
echo -e "Skipped: ${YELLOW}${SKIP}${NC}"
echo -e "Total: $((PASS + FAIL + SKIP))"
echo ""

if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
    echo ""
    echo "Your Android skill is working correctly!"
    echo "Try these next:"
    echo "  - Test with your own app"
    echo "  - Try the examples in TESTING.md"
    echo "  - Use with Claude Code for AI-driven automation"
    exit 0
else
    echo -e "${RED}✗ Some tests failed${NC}"
    echo ""
    echo "See TESTING.md for troubleshooting tips"
    exit 1
fi
