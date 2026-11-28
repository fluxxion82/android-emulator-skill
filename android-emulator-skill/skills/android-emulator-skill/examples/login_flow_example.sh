#!/bin/bash
# Example: launch, wait, act, check -- the loop SKILL.md gives an agent.
#
# It runs against Android's own Settings app, which every emulator has, so
# there is nothing to install and nothing to log in to. It restarts Settings,
# waits until its first screen can be read (which prints what is on it), opens
# "Apps" and waits for that screen, then goes back and waits for the first one
# again.
#
# Nothing here sleeps for a guessed time and nothing greps a script's prose:
# each step waits for the text it needs (`--wait-for`, `--tap --expect`) and is
# judged by its exit status. The first step that fails ends the run with that
# step's status, after its own `Error:` / `Remedy:` lines.
#
# The names are stock Android's ("Connected devices", "Apps", "Default apps").
# A vendor's Settings may word them differently; the step that waits for one
# then says what the screen read instead.
#
# Usage: bash login_flow_example.sh [SERIAL]
#   SERIAL  the device to drive, as `adb devices` lists it. Needed when more
#           than one is attached.

set -u

# CDPATH is cleared for this one lookup: with it set, `cd` to a relative path
# prints the directory it chose, and that line would end up in SKILL_DIR too.
SKILL_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)" || exit 1

DEVICE=()
if [ "$#" -ge 1 ]; then
    DEVICE=(--serial "$1")
fi

# step WHY SCRIPT [ARG...]: say why, show the command, run it, stop if it fails.
step() {
    local why="$1" script="$2" word
    shift 2
    echo
    echo "# $why"
    printf '$ python3 "$SKILL_DIR/scripts/%s"' "$script"
    for word in "$@" ${DEVICE[@]+"${DEVICE[@]}"}; do
        case "$word" in
            *[!A-Za-z0-9_./:=,-]*) printf ' "%s"' "$word" ;;
            *) printf ' %s' "$word" ;;
        esac
    done
    echo
    python3 "$SKILL_DIR/scripts/$script" "$@" ${DEVICE[@]+"${DEVICE[@]}"}
    local status=$?
    if [ "$status" -ne 0 ]; then
        echo "Stopped: that command exited $status." >&2
        exit "$status"
    fi
}

step "Start Settings from its first screen" \
    app_launcher.py --restart com.android.settings

step "Wait until that screen can be read; the answer lists its controls" \
    navigator.py --wait-for "Connected devices"

step "Open Apps, and wait for the screen the tap should bring" \
    navigator.py --find-exact "Apps" --tap --expect "Default apps"

step "Go back" \
    keyboard.py --button back

step "Check the first screen is back" \
    navigator.py --wait-for "Connected devices"

cat <<'EOF'

Done. A login flow is the same loop with your app's names in it:

python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --index 0 --enter-text "user@example.com"
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --index 1 --enter-text "the password"
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap --expect "Welcome"
EOF
