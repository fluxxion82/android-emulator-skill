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
# Usage: bash scripts/android_health_check.sh [--help] [--json]
#
# Exit codes:
#   0 - All hard requirements satisfied (warnings allowed)
#   1 - A hard requirement is missing (adb not on PATH), or --json could not
#       emit its report (one {"success": false, "error", "remedy"} line instead)

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
JSON_MODE=false

for arg in "$@"; do
    case "$arg" in
        --help | -h)
            SHOW_HELP=true
            ;;
        --json)
            JSON_MODE=true
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
  --json        Emit one JSON object on stdout (progress goes to stderr)

This script checks for:
  - ANDROID_HOME / ANDROID_SDK_ROOT environment variable
  - adb, emulator, avdmanager, sdkmanager, and java on PATH (with versions)
  - Python 3.12+ (for the skill's Python scripts) and Pillow importability
  - Connected devices/emulators (adb devices -l)
  - Defined AVDs (emulator -list-avds)

Exit codes:
  0 - All hard requirements satisfied (warnings allowed)
  1 - A hard requirement is missing (adb not on PATH), or --json could not
      emit its report (one {"success": false, "error", "remedy"} line instead)
EOF
    exit 0
fi

# ---------------------------------------------------------------------------
# Counters and helpers
# ---------------------------------------------------------------------------
CHECKS_PASSED=0
CHECKS_WARNED=0
CHECKS_FAILED=0
CURRENT_SECTION=""
CURRENT_TITLE=""
FIRST_FAILURE=""
CHECKS_RECORDED=0
JSON_RECORD_DIR=""
# Why a check's record could not be written, if one could not. A report
# without that check would claim a completeness it does not have, so --json
# then ends in a refusal instead (see emit_json).
JSON_RECORD_ERROR=""

record_check() {
    local record_prefix
    local write_error

    if [ "$JSON_MODE" != true ]; then
        return
    fi

    record_prefix="$JSON_RECORD_DIR/check-$CHECKS_RECORDED"
    # In a subshell so a redirection's failure is captured as text; the files
    # it writes are on disk either way.
    if ! write_error=$(
        {
            printf "%s" "$CURRENT_SECTION" >"$record_prefix.section" &&
                printf "%s" "$CURRENT_TITLE" >"$record_prefix.name" &&
                printf "%s" "$1" >"$record_prefix.status" &&
                printf "%s" "$2" >"$record_prefix.message"
        } 2>&1
    ); then
        if [ -z "$JSON_RECORD_ERROR" ]; then
            JSON_RECORD_ERROR="could not write the record of check $((CHECKS_RECORDED + 1)) ($2): ${write_error:-no error output}"
        fi
    fi
    CHECKS_RECORDED=$((CHECKS_RECORDED + 1))
}

check_passed() {
    printf "%b✓%b %s\n" "$GREEN" "$NC" "$1"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
    record_check "passed" "$1"
}

check_warning() {
    printf "%b⚠%b %s\n" "$YELLOW" "$NC" "$1"
    CHECKS_WARNED=$((CHECKS_WARNED + 1))
    record_check "warning" "$1"
}

check_failed() {
    printf "%b✗%b %s\n" "$RED" "$NC" "$1"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
    if [ -z "$FIRST_FAILURE" ]; then
        FIRST_FAILURE="$1"
    fi
    record_check "failed" "$1"
}

hint() {
    printf "       %s\n" "$1"
}

section() {
    CURRENT_SECTION="$1"
    CURRENT_TITLE="$2"
    printf "%b[%s]%b %s\n" "$BLUE" "$1" "$NC" "$2"
}

fallback_json_escape() {
    local value="$1"
    local escaped=""
    local char
    local char_code
    local unicode_escape
    local index
    (
        # Bash 3.2 reports bytes >= 0x80 as negative numbers. Work byte-by-byte
        # and normalize them before emitting \u00XX. This fallback is lossy for
        # UTF-8, but it always produces valid JSON when python3 is unavailable.
        LC_ALL=C
        export LC_ALL
        for ((index = 0; index < ${#value}; index++)); do
            char=${value:index:1}
            case "$char" in
                '"') escaped="${escaped}\\\"" ;;
                \\) escaped="${escaped}\\\\" ;;
                $'\b') escaped="${escaped}\\b" ;;
                $'\f') escaped="${escaped}\\f" ;;
                $'\n') escaped="${escaped}\\n" ;;
                $'\r') escaped="${escaped}\\r" ;;
                $'\t') escaped="${escaped}\\t" ;;
                *)
                    printf -v char_code '%d' "'$char"
                    if ((char_code < 0)); then
                        char_code=$((char_code + 256))
                    fi
                    if ((char_code < 32)); then
                        printf -v unicode_escape '\\u%04X' "$char_code"
                        escaped="${escaped}${unicode_escape}"
                    elif ((char_code >= 128)); then
                        printf -v unicode_escape '\\u00%02X' "$char_code"
                        escaped="${escaped}${unicode_escape}"
                    else
                        escaped="${escaped}${char}"
                    fi
                    ;;
            esac
        done
        printf "%s" "$escaped"
    )
}

emit_json_fallback() {
    local ok=true
    local index
    local section_value
    local name_value
    local status_value
    local message_value
    local record_prefix
    local separator=""
    if [ "$CHECKS_FAILED" -gt 0 ]; then
        ok=false
    fi

    printf '{"ok":%s,"success":%s,"checks":[' "$ok" "$ok"
    for ((index = 0; index < CHECKS_RECORDED; index++)); do
        record_prefix="$JSON_RECORD_DIR/check-$index"
        # A record that cannot be read fails the whole document; the caller
        # captures it and discards a partial one.
        section_value=$(<"$record_prefix.section") || return 1
        name_value=$(<"$record_prefix.name") || return 1
        status_value=$(<"$record_prefix.status") || return 1
        message_value=$(<"$record_prefix.message") || return 1
        printf '%s{"section":"%s","name":"%s","status":"%s","message":"%s"}' \
            "$separator" \
            "$(fallback_json_escape "$section_value")" \
            "$(fallback_json_escape "$name_value")" \
            "$status_value" \
            "$(fallback_json_escape "$message_value")"
        separator=","
    done
    printf '],"summary":{"passed":%s,"warnings":%s,"failed":%s}' \
        "$CHECKS_PASSED" "$CHECKS_WARNED" "$CHECKS_FAILED"
    if [ "$ok" = false ]; then
        printf ',"error":"%s","remedy":"%s"' \
            "$(fallback_json_escape "$FIRST_FAILURE")" \
            "Fix the failed checks above, then run the health check again."
    fi
    printf '}\n'
}

emit_json_unavailable() {
    # One line built from nothing but its two arguments: for when the checks'
    # results cannot be emitted at all. The checks' own lines are on stderr.
    printf '{"ok":false,"success":false,"error":"%s","remedy":"%s"}\n' \
        "$(fallback_json_escape "$1")" "$(fallback_json_escape "$2")"
}

cleanup_json_records() {
    local index
    local record_prefix

    for ((index = 0; index < CHECKS_RECORDED; index++)); do
        record_prefix="$JSON_RECORD_DIR/check-$index"
        rm -f \
            "$record_prefix.section" \
            "$record_prefix.name" \
            "$record_prefix.status" \
            "$record_prefix.message"
    done
    if [ -d "$JSON_RECORD_DIR" ]; then
        rmdir "$JSON_RECORD_DIR"
    fi
}

# One line that opens and closes an object -- what both encoders print. Braces
# alone let a line printed ahead of the report (a startup diagnostic) through
# as two documents at exit 0 (review of #33).
is_json_object() {
    case "$1" in
        *$'\n'*) return 1 ;;
        "{"*"}") return 0 ;;
        *) return 1 ;;
    esac
}

# Prints the report on stdout, and returns non-zero when it could not: the
# caller's exit status must say so. The encoder's output is captured and
# checked before any of it is printed -- python3 used to be run unchecked, so
# one that resolved on PATH and could not run left stdout empty at exit 0.
emit_json() {
    local document=""
    local status=0
    local remedy="The checks ran and their results are on stderr. Fix what the error names, then run the health check again."

    if [ -n "$JSON_RECORD_ERROR" ]; then
        emit_json_unavailable "could not emit the diagnostics: $JSON_RECORD_ERROR" "$remedy"
        status=1
    elif command -v python3 >/dev/null 2>&1 && document=$(emit_json_python) &&
        is_json_object "$document"; then
        printf '%s\n' "$document"
    elif document=$(emit_json_fallback) && is_json_object "$document"; then
        if command -v python3 >/dev/null 2>&1; then
            printf "python3 could not encode the report; the Bash encoder did.\n" >&2
        fi
        printf '%s\n' "$document"
    else
        emit_json_unavailable \
            "could not emit the diagnostics: neither python3 nor the Bash encoder could read the check records in $JSON_RECORD_DIR" \
            "$remedy"
        status=1
    fi
    cleanup_json_records
    return "$status"
}

emit_json_python() {
    # Read each field as UTF-8 once, replacing every invalid byte with U+FFFD.
    # Python owns the real JSON encoding so Unicode and control characters
    # are never interpreted by Bash's locale-sensitive character handling.
    run_bounded 10 python3 -c '
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
count, passed, warnings, failed = map(int, sys.argv[2:])


def read_field(index, field):
    return (root / f"check-{index}.{field}").read_text(
        encoding="utf-8", errors="replace"
    )


checks = [
    {
        "section": read_field(index, "section"),
        "name": read_field(index, "name"),
        "status": read_field(index, "status"),
        "message": read_field(index, "message"),
    }
    for index in range(count)
]
ok = failed == 0
payload = {
    "ok": ok,
    "success": ok,
    "checks": checks,
    "summary": {"passed": passed, "warnings": warnings, "failed": failed},
}
if not ok:
    payload["error"] = next(check["message"] for check in checks if check["status"] == "failed")
    payload["remedy"] = "Fix the failed checks above, then run the health check again."
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
' "$JSON_RECORD_DIR" "$CHECKS_RECORDED" "$CHECKS_PASSED" "$CHECKS_WARNED" "$CHECKS_FAILED"
}

run_bounded() {
    local timeout_seconds="$1"
    shift

    if command -v timeout >/dev/null 2>&1; then
        timeout "$timeout_seconds" "$@"
    else
        # macOS does not ship `timeout`; alarm survives exec and bounds the tool.
        perl -e 'alarm shift; exec @ARGV or die "$ARGV[0]: $!\n"' "$timeout_seconds" "$@"
    fi
}

CAPTURED_STDOUT=""
CAPTURED_STDERR=""
CAPTURED_STATUS=0

# mktemp into the variable named $1. On failure the probe has not run, and
# says why in CAPTURED_STDERR and CAPTURED_COMBINED. Unchecked, the redirect
# to an empty path failed, the tool never ran, and the probe read "exited 1
# with no error output" -- a failure of the tool's that never happened.
make_temp_file() {
    local made
    if ! made=$(mktemp "${TMPDIR:-/tmp}/android-health-check-$2.XXXXXX" 2>&1); then
        CAPTURED_STATUS=1
        CAPTURED_STDERR="did not run, no temporary file for its output: ${made:-mktemp failed}"
        CAPTURED_COMBINED=$CAPTURED_STDERR
        return 1
    fi
    printf -v "$1" "%s" "$made"
}

run_captured() {
    local stdout_file
    local stderr_file

    make_temp_file stdout_file stdout || return 1
    if ! make_temp_file stderr_file stderr; then
        rm -f "$stdout_file"
        return 1
    fi
    if run_bounded 10 "$@" >"$stdout_file" 2>"$stderr_file"; then
        CAPTURED_STATUS=0
    else
        CAPTURED_STATUS=$?
    fi

    CAPTURED_STDOUT=$(<"$stdout_file")
    CAPTURED_STDERR=""
    IFS= read -r CAPTURED_STDERR <"$stderr_file" || true
    if [ "$CAPTURED_STATUS" -ne 0 ] && [ -z "$CAPTURED_STDERR" ]; then
        CAPTURED_STDERR="exited $CAPTURED_STATUS with no error output"
    fi
    rm -f "$stdout_file" "$stderr_file"
    [ "$CAPTURED_STATUS" -eq 0 ]
}

CAPTURED_COMBINED=""

run_combined_captured() {
    local combined_file

    make_temp_file combined_file combined || return 1
    if run_bounded 10 "$@" >"$combined_file" 2>&1; then
        CAPTURED_STATUS=0
    else
        CAPTURED_STATUS=$?
    fi

    CAPTURED_COMBINED=""
    IFS= read -r CAPTURED_COMBINED <"$combined_file" || true
    if [ "$CAPTURED_STATUS" -ne 0 ] && [ -z "$CAPTURED_COMBINED" ]; then
        CAPTURED_COMBINED="exited $CAPTURED_STATUS with no error output"
    fi
    rm -f "$combined_file"
    [ "$CAPTURED_STATUS" -eq 0 ]
}

if [ "$JSON_MODE" = true ]; then
    exec 3>&1
    exec 1>&2
    # Without somewhere to keep the records there is no report to emit: refuse
    # now, in the one line --json promises, rather than run the checks for none.
    if ! JSON_RECORD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/android-health-check-json.XXXXXX" 2>&1); then
        emit_json_unavailable \
            "could not create a directory for the check records: ${JSON_RECORD_DIR:-mktemp -d failed with no error output}" \
            "Point TMPDIR at a writable directory (it is ${TMPDIR:-unset, so /tmp}), then run the health check again." >&3
        exit 1
    fi
fi

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
    if run_captured adb --version; then
        ADB_VERSION=${CAPTURED_STDOUT%%$'\n'*}
        check_passed "adb installed ($ADB_VERSION)"
    else
        check_failed "adb --version failed: $CAPTURED_STDERR"
    fi
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
    if run_captured emulator -version; then
        EMU_VERSION=${CAPTURED_STDOUT%%$'\n'*}
        check_passed "emulator installed ($EMU_VERSION)"
    else
        check_failed "emulator -version failed: $CAPTURED_STDERR"
    fi
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
    if run_captured sdkmanager --version; then
        SDKMGR_VERSION=${CAPTURED_STDOUT%%$'\n'*}
        check_passed "sdkmanager installed (version $SDKMGR_VERSION)"
    else
        check_failed "sdkmanager --version failed: $CAPTURED_STDERR"
    fi
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
    # Java normally uses stderr, but preserve the original first-combined-line behavior.
    if run_combined_captured java -version; then
        JAVA_VERSION=$CAPTURED_COMBINED
        check_passed "java installed ($JAVA_VERSION)"
    else
        check_failed "java -version failed: $CAPTURED_COMBINED"
    fi
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
PYTHON_RUNS=false
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

# One part of the interpreter's version into CAPTURED_STDOUT, bounded like
# every other probe. Fails, with the reason in CAPTURED_STDERR, unless the
# interpreter ran and printed a number: a failed probe used to read as
# "Python 0.0 found".
python_version_part() {
    run_captured "$PYTHON_BIN" -c "import sys; print(sys.version_info.$1)" || return 1
    case "$CAPTURED_STDOUT" in
        "" | *[!0-9]*)
            CAPTURED_STDERR="printed ${CAPTURED_STDOUT:-nothing} as its version"
            return 1
            ;;
    esac
}

if [ -n "$PYTHON_BIN" ]; then
    if python_version_part major && PY_MAJOR=$CAPTURED_STDOUT &&
        python_version_part minor && PY_MINOR=$CAPTURED_STDOUT; then
        PYTHON_RUNS=true
        # Pure version-gate logic: pass only when version >= 3.12.
        if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 12 ]; }; then
            check_passed "Python $PY_MAJOR.$PY_MINOR (>= 3.12 required)"
        else
            check_warning "Python $PY_MAJOR.$PY_MINOR found — Python 3.12+ required for skill scripts"
            hint "Install Python 3.12+: brew install python@3.12"
        fi
    else
        check_warning "$PYTHON_BIN is on PATH but could not run: $CAPTURED_STDERR"
        hint "Path: $(command -v "$PYTHON_BIN")"
        hint "Python 3.12+ is required to run this skill's Python scripts"
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
if [ "$PYTHON_RUNS" = true ]; then
    if run_captured "$PYTHON_BIN" -c "import PIL"; then
        check_passed "Pillow (PIL) importable — screenshot resizing available"
    else
        check_warning "Pillow (PIL) not importable — screenshot resizing won't work"
        hint "Install: $PYTHON_BIN -m pip install pillow"
    fi
elif [ -n "$PYTHON_BIN" ]; then
    check_warning "Cannot check Pillow ($PYTHON_BIN could not run)"
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
    ADB_ERROR_FILE=""
    if ! make_temp_file ADB_ERROR_FILE adb; then
        check_failed "could not list devices (adb devices -l): $CAPTURED_STDERR"
    elif ADB_DEVICES_OUTPUT=$(run_bounded 10 adb devices -l 2>"$ADB_ERROR_FILE"); then
        DEVICE_LINES=$(printf "%s\n" "$ADB_DEVICES_OUTPUT" | sed '1d' | grep -v '^$' || true)
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
        ADB_STATUS=$?
        ADB_ERROR=""
        IFS= read -r ADB_ERROR <"$ADB_ERROR_FILE" || true
        if [ -z "$ADB_ERROR" ]; then
            ADB_ERROR="adb devices -l exited $ADB_STATUS with no error output"
        fi
        check_failed "could not list devices (adb devices -l): $ADB_ERROR"
        hint "Check that adb can start and retry: adb devices -l"
    fi
    if [ -n "$ADB_ERROR_FILE" ]; then
        rm -f "$ADB_ERROR_FILE"
    fi
else
    check_warning "Cannot list devices (adb not available)"
fi

if command -v emulator >/dev/null 2>&1; then
    if run_captured emulator -list-avds; then
        AVD_LINES=$(printf "%s\n" "$CAPTURED_STDOUT" | grep -v '^$' || true)
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
        check_failed "emulator -list-avds failed: $CAPTURED_STDERR"
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
    if [ "$JSON_MODE" = true ]; then
        emit_json >&3
    fi
    exit 1
elif [ "$CHECKS_WARNED" -gt 0 ]; then
    printf "%bWARN%b — environment is usable, but address the warnings above for full functionality.\n" "$YELLOW" "$NC"
    if [ "$JSON_MODE" = true ] && ! emit_json >&3; then
        exit 1
    fi
    exit 0
else
    printf "%bPASS%b — environment is ready for Android emulator testing.\n" "$GREEN" "$NC"
    if [ "$JSON_MODE" = true ] && ! emit_json >&3; then
        exit 1
    fi
    exit 0
fi
