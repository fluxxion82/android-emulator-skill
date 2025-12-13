#!/bin/bash
# Quick test to verify Android skill works
# Usage: ./quick_test.sh

set -e

echo "🤖 Android Emulator Skill - Quick Test"
echo ""

# Check ADB
if ! command -v adb &> /dev/null; then
    echo "❌ Error: adb not found"
    echo "Install Android SDK and add to PATH:"
    echo "  export PATH=\$PATH:\$ANDROID_HOME/platform-tools"
    exit 1
fi

# Check device
DEVICE_COUNT=$(adb devices | grep -v "List" | grep "device$" | wc -l)
if [ "$DEVICE_COUNT" -eq 0 ]; then
    echo "❌ No devices connected"
    echo ""
    echo "Start an emulator:"
    echo "  emulator -list-avds"
    echo "  emulator -avd <name> &"
    echo ""
    echo "Or connect a physical device"
    exit 1
fi

DEVICE=$(adb devices | grep "device$" | head -1 | awk '{print $1}')
echo "✓ Found device: $DEVICE"
echo ""

cd "$(dirname "$0")/skill/scripts"

echo "Testing 5 core functions..."
echo ""

# Test 1: Launch Settings
echo "1️⃣  Launching Settings app..."
if python3 app_launcher.py --launch com.android.settings 2>&1 | grep -q "Launched"; then
    echo "   ✓ App launched"
else
    echo "   ❌ Failed to launch app"
    exit 1
fi

sleep 2

# Test 2: Map screen
echo "2️⃣  Mapping screen..."
OUTPUT=$(python3 screen_mapper.py 2>&1)
if echo "$OUTPUT" | grep -q "Screen:"; then
    echo "   ✓ Screen mapped"
    echo "   Output: $(echo "$OUTPUT" | head -1)"
else
    echo "   ❌ Failed to map screen"
    echo "   $OUTPUT"
    exit 1
fi

# Test 3: Find element
echo "3️⃣  Finding element..."
if python3 navigator.py --find-text "Apps" 2>&1 | grep -q "Found:"; then
    echo "   ✓ Element found"
else
    echo "   ℹ️  'Apps' button not found (screen might be different)"
fi

# Test 4: Gesture
echo "4️⃣  Testing gesture..."
if python3 gesture.py --swipe up 2>&1 | grep -q "Swiped"; then
    echo "   ✓ Gesture worked"
else
    echo "   ❌ Gesture failed"
    exit 1
fi

# Test 5: Keyboard
echo "5️⃣  Testing keyboard..."
if python3 keyboard.py --button back 2>&1 | grep -q "Pressed"; then
    echo "   ✓ Button press worked"
else
    echo "   ❌ Button press failed"
    exit 1
fi

# Cleanup
python3 app_launcher.py --terminate com.android.settings > /dev/null 2>&1

echo ""
echo "🎉 Success! All core functions work!"
echo ""
echo "Next steps:"
echo "  • Run full test suite: ./test_basic.sh"
echo "  • Read testing guide: cat TESTING.md"
echo "  • Try with your app: python3 skill/scripts/app_launcher.py --launch com.your.app"
echo ""
echo "Example workflow:"
echo "  python3 skill/scripts/screen_mapper.py"
echo "  python3 skill/scripts/navigator.py --find-text 'Login' --tap"
echo "  python3 skill/scripts/navigator.py --find-type EditText --enter-text 'user@test.com'"
