#!/usr/bin/env bash
#
# Android Emulator Testing Environment Health Check
#
# Verifies that the required Android SDK tools and dependencies are properly
# installed and configured for emulator/device testing with this skill.
#
# This is the Android counterpart of the iOS sim_health_check.sh: same intent
# (one quick command that tells you whether your environment is ready), but
# Android mechanics — ANDROID_HOME/ANDROID_SDK_ROOT, adb, emulator, avdmanager,
# sdkmanager, and java rather than Xcode/simctl/idb.
#
# Usage: bash scripts/android_health_check.sh [--help]
#
# Exit codes:
#   0 - All hard requirements satisfied (warnings allowed)
#   1 - A hard requirement is missing (adb not on PATH)

set -u

# ---------------------------------------------------------------------------
# Output styling (disabled automatically when stdout is not a TTY)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

SHOW_HELP=false

for arg in "$@"; do
    case "$arg" in
        --help | -h)
            SHOW_HELP=true
            ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    cat <<EOF
Android Emulator Testing - Environment Health Check

Verifies that your environment is properly configured for Android emulator and
device testing with this skill.

Usage: bash scripts/android_health_check.sh [options]

Options:
  --help, -h    Show this help message

This script checks for:
  - ANDROID_HOME / ANDROID_SDK_ROOT environment variable
  - adb, emulator, avdmanager, sdkmanager, and java on PATH (with versions)
  - Python 3.12+ (for the skill's Python scripts) and Pillow importability
  - Connected devices/emulators (adb devices -l)
  - Defined AVDs (emulator -list-avds)

Exit codes:
  0 - All hard requirements satisfied (warnings allowed)
  1 - A hard requirement is missing (adb not on PATH)
EOF
    exit 0
fi

# ---------------------------------------------------------------------------
# Counters and helpers
# ---------------------------------------------------------------------------
CHECKS_PASSED=0
CHECKS_WARNED=0
CHECKS_FAILED=0

check_passed() {
    printf "%b✓%b %s\n" "$GREEN" "$NC" "$1"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
}

check_warning() {
    printf "%b⚠%b %s\n" "$YELLOW" "$NC" "$1"
    CHECKS_WARNED=$((CHECKS_WARNED + 1))
}

check_failed() {
    printf "%b✗%b %s\n" "$RED" "$NC" "$1"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
}

hint() {
    printf "       %s\n" "$1"
}

section() {
    printf "%b[%s]%b %s\n" "$BLUE" "$1" "$NC" "$2"
}

printf "%b━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%b\n" "$BLUE" "$NC"
printf "%b  Android Emulator Testing - Environment Health Check%b\n" "$BLUE" "$NC"
printf "%b━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%b\n" "$BLUE" "$NC"
echo ""

# ---------------------------------------------------------------------------
# Check 1: ANDROID_HOME / ANDROID_SDK_ROOT
# ---------------------------------------------------------------------------
section "1/8" "Checking Android SDK environment..."
SDK_ROOT=""
if [ -n "${ANDROID_HOME:-}" ]; then
    SDK_ROOT="$ANDROID_HOME"
    check_passed "ANDROID_HOME is set ($ANDROID_HOME)"
elif [ -n "${ANDROID_SDK_ROOT:-}" ]; then
    SDK_ROOT="$ANDROID_SDK_ROOT"
    check_passed "ANDROID_SDK_ROOT is set ($ANDROID_SDK_ROOT)"
else
    check_warning "Neither ANDROID_HOME nor ANDROID_SDK_ROOT is set"
    hint "Export it so the SDK tools can be found, e.g.:"
    hint "  export ANDROID_HOME=\$HOME/Library/Android/sdk"
    hint "  export PATH=\$PATH:\$ANDROID_HOME/platform-tools:\$ANDROID_HOME/emulator"
fi
if [ -n "$SDK_ROOT" ] && [ ! -d "$SDK_ROOT" ]; then
    check_warning "SDK directory does not exist: $SDK_ROOT"
    hint "Install the Android SDK or fix the path"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 2: adb (HARD REQUIREMENT)
# ---------------------------------------------------------------------------
section "2/8" "Checking adb (Android Debug Bridge)..."
ADB_MISSING=false
if command -v adb >/dev/null 2>&1; then
    ADB_VERSION=$(adb --version 2>/dev/null | head -n 1 || echo "Unknown")
    check_passed "adb installed ($ADB_VERSION)"
    hint "Path: $(command -v adb)"
else
    check_failed "adb not found on PATH (required)"
    ADB_MISSING=true
    hint "Install platform-tools and add them to PATH:"
    hint "  sdkmanager 'platform-tools'"
    hint "  export PATH=\$PATH:\$ANDROID_HOME/platform-tools"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 3: emulator
# ---------------------------------------------------------------------------
section "3/8" "Checking emulator..."
if command -v emulator >/dev/null 2>&1; then
    EMU_VERSION=$(emulator -version 2>/dev/null | head -n 1 || echo "Unknown")
    check_passed "emulator installed ($EMU_VERSION)"
    hint "Path: $(command -v emulator)"
else
    check_warning "emulator not found on PATH"
    hint "Install the emulator and add it to PATH:"
    hint "  sdkmanager 'emulator'"
    hint "  export PATH=\$PATH:\$ANDROID_HOME/emulator"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 4: avdmanager & sdkmanager
# ---------------------------------------------------------------------------
section "4/8" "Checking avdmanager and sdkmanager..."
if command -v avdmanager >/dev/null 2>&1; then
    check_passed "avdmanager installed"
    hint "Path: $(command -v avdmanager)"
else
    check_warning "avdmanager not found on PATH"
    hint "Install command-line tools and add cmdline-tools/latest/bin to PATH:"
    hint "  sdkmanager 'cmdline-tools;latest'"
    hint "  export PATH=\$PATH:\$ANDROID_HOME/cmdline-tools/latest/bin"
fi
if command -v sdkmanager >/dev/null 2>&1; then
    SDKMGR_VERSION=$(sdkmanager --version 2>/dev/null | head -n 1 || echo "Unknown")
    check_passed "sdkmanager installed (version $SDKMGR_VERSION)"
    hint "Path: $(command -v sdkmanager)"
else
    check_warning "sdkmanager not found on PATH"
    hint "Install command-line tools and add cmdline-tools/latest/bin to PATH:"
    hint "  export PATH=\$PATH:\$ANDROID_HOME/cmdline-tools/latest/bin"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 5: java
# ---------------------------------------------------------------------------
section "5/8" "Checking java..."
if command -v java >/dev/null 2>&1; then
    # java prints its version banner to stderr.
    JAVA_VERSION=$(java -version 2>&1 | head -n 1 || echo "Unknown")
    check_passed "java installed ($JAVA_VERSION)"
    hint "Path: $(command -v java)"
else
    check_warning "java not found on PATH"
    hint "Android SDK tools (sdkmanager/avdmanager) and Gradle require a JDK"
    hint "Install a JDK 17+ (e.g. Temurin) and ensure java is on PATH"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 6: Python 3.12+ (for the skill's Python scripts)
# ---------------------------------------------------------------------------
section "6/8" "Checking Python 3.12+..."
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

if [ -n "$PYTHON_BIN" ]; then
    PY_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
    PY_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
    # Pure version-gate logic: pass only when version >= 3.12.
    if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 12 ]; }; then
        check_passed "Python $PY_MAJOR.$PY_MINOR (>= 3.12 required)"
    else
        check_warning "Python $PY_MAJOR.$PY_MINOR found — Python 3.12+ required for skill scripts"
        hint "Install Python 3.12+: brew install python@3.12"
    fi
else
    check_warning "Python 3 not found on PATH"
    hint "Python 3.12+ is required to run this skill's Python scripts"
    hint "Install: brew install python@3.12"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 7: Pillow (optional Python dependency)
# ---------------------------------------------------------------------------
section "7/8" "Checking Python Pillow (PIL)..."
if [ -n "$PYTHON_BIN" ]; then
    if "$PYTHON_BIN" -c "import PIL" >/dev/null 2>&1; then
        check_passed "Pillow (PIL) importable — screenshot resizing available"
    else
        check_warning "Pillow (PIL) not importable — screenshot resizing won't work"
        hint "Install: $PYTHON_BIN -m pip install pillow"
    fi
else
    check_warning "Cannot check Pillow (Python 3 not available)"
fi
echo ""

# ---------------------------------------------------------------------------
# Check 8: Connected devices and defined AVDs
# ---------------------------------------------------------------------------
section "8/8" "Checking devices and AVDs..."
if [ "$ADB_MISSING" = false ]; then
    # adb devices -l prints a header line, then one line per device.
    DEVICE_LINES=$(adb devices -l 2>/dev/null | sed '1d' | grep -v '^$' || true)
    if [ -n "$DEVICE_LINES" ]; then
        DEVICE_COUNT=$(printf "%s\n" "$DEVICE_LINES" | wc -l | tr -d ' ')
        check_passed "$DEVICE_COUNT device(s)/emulator(s) connected"
        printf "%s\n" "$DEVICE_LINES" | while IFS= read -r line; do
            hint "- $line"
        done
    else
        check_warning "No devices or emulators connected"
        hint "Start an emulator or connect a device:"
        hint "  emulator -avd <avd-name>"
        hint "  adb devices"
    fi
else
    check_warning "Cannot list devices (adb not available)"
fi

if command -v emulator >/dev/null 2>&1; then
    AVD_LINES=$(emulator -list-avds 2>/dev/null | grep -v '^$' || true)
    if [ -n "$AVD_LINES" ]; then
        AVD_COUNT=$(printf "%s\n" "$AVD_LINES" | wc -l | tr -d ' ')
        check_passed "$AVD_COUNT AVD(s) defined"
        printf "%s\n" "$AVD_LINES" | while IFS= read -r line; do
            hint "- $line"
        done
    else
        check_warning "No AVDs defined"
        hint "Create one with avdmanager, e.g.:"
        hint "  avdmanager create avd -n Pixel_7_API_34 -k 'system-images;android-34;google_apis;arm64-v8a'"
    fi
else
    check_warning "Cannot list AVDs (emulator not available)"
fi
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf "%b━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%b\n" "$BLUE" "$NC"
printf "%b  Summary%b\n" "$BLUE" "$NC"
printf "%b━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%b\n" "$BLUE" "$NC"
echo ""
printf "Passed:   %b%s%b\n" "$GREEN" "$CHECKS_PASSED" "$NC"
printf "Warnings: %b%s%b\n" "$YELLOW" "$CHECKS_WARNED" "$NC"
printf "Failed:   %b%s%b\n" "$RED" "$CHECKS_FAILED" "$NC"
echo ""

if [ "$CHECKS_FAILED" -gt 0 ]; then
    printf "%bFAIL%b — a hard requirement is missing. Fix the failed checks above before testing.\n" "$RED" "$NC"
    exit 1
elif [ "$CHECKS_WARNED" -gt 0 ]; then
    printf "%bWARN%b — environment is usable, but address the warnings above for full functionality.\n" "$YELLOW" "$NC"
    exit 0
else
    printf "%bPASS%b — environment is ready for Android emulator testing.\n" "$GREEN" "$NC"
    exit 0
fi
