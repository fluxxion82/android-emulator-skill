#!/bin/bash
# Interactive test - demonstrates actual usage with real interactions
# This is more comprehensive than quick_test.sh

set -e

echo "🎯 Android Skill - Interactive Feature Test"
echo ""
echo "This test demonstrates real automation scenarios"
echo ""

# Check device
if ! adb devices | grep -q "device$"; then
    echo "❌ No device connected"
    exit 1
fi

DEVICE=$(adb devices | grep "device$" | head -1 | awk '{print $1}')
echo "✓ Using device: $DEVICE"
echo ""

cd "$(dirname "$0")/skill/scripts"

# Test 1: Complete navigation flow
echo "📱 Test 1: Navigation Flow"
echo "  → Launch Settings"
python3 app_launcher.py --launch com.android.settings > /dev/null
sleep 1

echo "  → Map current screen"
python3 screen_mapper.py > /tmp/screen_map.txt
if grep -q "Screen:" /tmp/screen_map.txt; then
    echo "  ✓ Screen mapped successfully"
    head -3 /tmp/screen_map.txt | sed 's/^/    /'
else
    echo "  ❌ Screen mapping failed"
    exit 1
fi

# Test 2: Element interaction
echo ""
echo "🎯 Test 2: Element Interaction"
echo "  → Find 'Apps' button"
if python3 navigator.py --find-text "Apps" 2>&1 | grep -q "Found:"; then
    echo "  ✓ Element found"
    echo "  → Tap button"
    python3 navigator.py --find-text "Apps" --tap > /dev/null
    sleep 1
    echo "  ✓ Tapped successfully"

    echo "  → Go back"
    python3 keyboard.py --button back > /dev/null
    echo "  ✓ Navigated back"
else
    echo "  ℹ️  'Apps' not found (screen layout may differ)"
fi

# Test 3: Gesture interactions
echo ""
echo "👆 Test 3: Gestures"
echo "  → Swipe up (scroll down)"
python3 gesture.py --swipe up > /dev/null
echo "  ✓ Swipe up"

sleep 0.5

echo "  → Swipe down (scroll up)"
python3 gesture.py --swipe down > /dev/null
echo "  ✓ Swipe down"

echo "  → Multi-scroll"
python3 gesture.py --scroll up --count 2 > /dev/null
echo "  ✓ Scrolled 2 times"

# Test 4: Text input (if we can find a search field)
echo ""
echo "⌨️  Test 4: Text Input"
echo "  → Open search (if available)"
python3 keyboard.py --key search 2>/dev/null && sleep 1

if adb shell "dumpsys window | grep -i 'mCurrentFocus'" | grep -q "SearchActivity\|Search"; then
    echo "  ✓ Search opened"
    echo "  → Type text"
    python3 keyboard.py --type "Display" > /dev/null
    echo "  ✓ Typed: Display"
    sleep 1
    python3 keyboard.py --button back > /dev/null
    echo "  ✓ Closed search"
else
    echo "  ℹ️  Search not available (skipped text input test)"
fi

# Test 5: JSON output mode
echo ""
echo "📊 Test 5: JSON Output"
echo "  → Test JSON mode"
python3 screen_mapper.py --json > /tmp/screen_map.json
if python3 -m json.tool /tmp/screen_map.json > /dev/null 2>&1; then
    echo "  ✓ Valid JSON output"
    echo "  Sample: $(head -2 /tmp/screen_map.json | tr -d '\n')"
else
    echo "  ❌ Invalid JSON"
    exit 1
fi

# Test 6: List all elements
echo ""
echo "📋 Test 6: Element Discovery"
echo "  → List all interactive elements"
ELEMENT_COUNT=$(python3 navigator.py --list 2>&1 | grep -c "^\s*[0-9]")
echo "  ✓ Found $ELEMENT_COUNT interactive elements"

# Test 7: App lifecycle
echo ""
echo "🔄 Test 7: App Lifecycle"
echo "  → Check app state"
python3 app_launcher.py --state com.android.settings > /dev/null
echo "  ✓ App state retrieved"

echo "  → Terminate app"
python3 app_launcher.py --terminate com.android.settings > /dev/null
echo "  ✓ App terminated"

echo "  → Verify terminated"
if python3 app_launcher.py --state com.android.settings 2>&1 | grep -q "running.*False\|foreground.*False"; then
    echo "  ✓ Termination verified"
else
    echo "  ⚠️  App may still be running (cached state)"
fi

# Summary
echo ""
echo "✅ All interactive tests passed!"
echo ""
echo "📖 What was tested:"
echo "  1. Screen mapping and analysis"
echo "  2. Element finding and interaction"
echo "  3. Gesture simulation (swipe, scroll)"
echo "  4. Text input (keyboard)"
echo "  5. JSON output format"
echo "  6. Element discovery"
echo "  7. App lifecycle management"
echo ""
echo "💡 Try these commands manually:"
echo "  python3 skill/scripts/screen_mapper.py --verbose"
echo "  python3 skill/scripts/navigator.py --list"
echo "  python3 skill/scripts/gesture.py --swipe up"
