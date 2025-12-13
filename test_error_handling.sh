#!/bin/bash
# Test error handling and edge cases

echo "🧪 Android Skill - Error Handling Tests"
echo ""

cd "$(dirname "$0")/skill/scripts"

PASS=0
FAIL=0

test_should_fail() {
    local name="$1"
    shift
    echo -n "Testing: $name ... "

    if "$@" > /dev/null 2>&1; then
        echo "❌ FAIL (should have failed but didn't)"
        ((FAIL++))
        return 1
    else
        echo "✓ PASS (failed as expected)"
        ((PASS++))
        return 0
    fi
}

test_should_succeed() {
    local name="$1"
    shift
    echo -n "Testing: $name ... "

    if "$@" > /dev/null 2>&1; then
        echo "✓ PASS"
        ((PASS++))
        return 0
    else
        echo "❌ FAIL"
        ((FAIL++))
        return 1
    fi
}

echo "Edge Case Tests:"
echo ""

# Test 1: Non-existent package
test_should_fail "Launch non-existent package" \
    python3 app_launcher.py --launch com.fake.nonexistent.package

# Test 2: Find non-existent element
test_should_fail "Find non-existent element" \
    python3 navigator.py --find-text "ThisElementDefinitelyDoesNotExist12345" --tap

# Test 3: Invalid swipe direction
test_should_fail "Invalid swipe direction" \
    python3 gesture.py --swipe invalid_direction

# Test 4: Invalid key name
test_should_fail "Invalid key name" \
    python3 keyboard.py --key invalid_key_name

# Test 5: Terminate non-running app
test_should_succeed "Terminate non-running app (should succeed)" \
    python3 app_launcher.py --terminate com.fake.app.that.does.not.exist

# Test 6: Valid operations that should work
echo ""
echo "Positive Tests:"
echo ""

test_should_succeed "Launch Settings" \
    python3 app_launcher.py --launch com.android.settings

test_should_succeed "Map screen" \
    python3 screen_mapper.py

test_should_succeed "List elements" \
    python3 navigator.py --list

test_should_succeed "Swipe up" \
    python3 gesture.py --swipe up

test_should_succeed "Press back" \
    python3 keyboard.py --button back

# Results
echo ""
echo "=== Results ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo ""

if [ "$FAIL" -eq 0 ]; then
    echo "✅ All error handling tests passed!"
    exit 0
else
    echo "❌ Some tests failed"
    exit 1
fi
