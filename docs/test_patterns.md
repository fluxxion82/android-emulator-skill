# Android Test Patterns

Reusable test recipes built from this skill's **actual** scripts. All scripts live under
`scripts/` and support `--help`; most support `--json` for machine-readable output and
`--serial`/`-s` for targeting a specific device (auto-detected when omitted).

> Conventions used below: `PKG` = app package (e.g. `com.example.app`), `SERIAL` = device
> serial (e.g. `emulator-5554`). Replace navigation steps (`navigator.py`) with the elements
> your app actually exposes.

---

## Smoke Test

Boot, launch, sanity-check the first screen, run an accessibility pass, and grab a snapshot.

```bash
# 1. Boot an emulator and wait until it is ready
python scripts/emulator_boot.py --avd Pixel_7_API_34 --wait-ready

# 2. Launch the app under test
python scripts/app_launcher.py --launch com.example.app

# 3. Confirm something rendered (counts interactive elements)
python scripts/screen_mapper.py

# 4. Accessibility sanity pass
python scripts/accessibility_audit.py

# 5. Capture a debugging snapshot as the smoke artifact
python scripts/app_state_capture.py --package com.example.app --output smoke/
```

Exit codes are meaningful: each script exits non-zero on failure, so this sequence works as a
CI gate with `set -e`.

---

## Visual Regression (visual_diff.py)

`visual_diff.py` takes two **positional** image paths (`baseline` then `current`) and compares
them. Capture screenshots with `app_state_capture.py` (which saves a screenshot into its output
directory) or with raw `adb exec-out screencap`.

```bash
# 1. Capture the baseline once and keep it under version control
adb -s emulator-5554 exec-out screencap -p > baseline.png

# 2. After a change, capture the current state
adb -s emulator-5554 exec-out screencap -p > current.png

# 3. Compare — writes diff artifacts into ./visual-out/
python scripts/visual_diff.py baseline.png current.png \
  --output visual-out/ \
  --threshold 0.01

# Add --details for a per-region breakdown (costs more tokens), or --json for CI
python scripts/visual_diff.py baseline.png current.png --threshold 0.02 --json
```

`--threshold` is the fraction of changed pixels tolerated before the diff is flagged
(default `0.01`). For deterministic screenshots, freeze the status bar first:

```bash
python scripts/status_bar.py --time "12:00" --battery 100 --wifi
# ... capture screenshots ...
python scripts/status_bar.py --reset
```

---

## Full Accessibility Audit

Walk every screen, navigating with `navigator.py`, and save a report per screen.
`accessibility_audit.py` writes both JSON and Markdown into the `--output` directory.

```bash
mkdir -p a11y-reports

python scripts/app_launcher.py --launch com.example.app

# Home
python scripts/accessibility_audit.py --output a11y-reports/home --verbose

# Navigate to Login and audit
python scripts/navigator.py --find-text "Login" --tap
python scripts/accessibility_audit.py --output a11y-reports/login --verbose

# Navigate to Settings and audit
python scripts/navigator.py --find-text "Settings" --tap
python scripts/accessibility_audit.py --output a11y-reports/settings --verbose
```

The audit flags missing `content-desc`, undersized touch targets, and missing `EditText`
hints, categorized as critical / warning / info. Use `--json` instead of `--verbose` to feed
the results into a CI assertion step.

Re-run any screen under a large font scale to catch truncation/clipping:

```bash
python scripts/appearance.py --text-size xl
python scripts/accessibility_audit.py --output a11y-reports/login-xl --verbose
python scripts/appearance.py --reset
```

---

## Bug Capture (app_state_capture.py)

One command produces a timestamped, all-in-one debugging artifact: screenshot + UI hierarchy +
logcat tail + app info. `--package` is required.

```bash
# Default: includes the last 30s of logs
python scripts/app_state_capture.py \
  --package com.example.app \
  --output bug-reports/

# Tune the log window, or skip logs entirely
python scripts/app_state_capture.py --package com.example.app --output bug-reports/ --logs 1m
python scripts/app_state_capture.py --package com.example.app --output bug-reports/ --no-logs

# Shrink the screenshot for token-efficient sharing, target a specific device
python scripts/app_state_capture.py \
  --package com.example.app \
  --output bug-reports/ \
  --screenshot-size 600 \
  --serial emulator-5554
```

Pair it with live log streaming when you are reproducing intermittently:

```bash
python scripts/log_monitor.py --app com.example.app --severity error,warning --follow
```

---

## Localization Audit (localization_audit.py)

Audit `res/values*/strings.xml` for missing keys per locale and placeholder mismatches.
`--res` points at the resource root; `--source` optionally cross-references actual
`R.string` / `getString` / `stringResource` usage.

```bash
# Compare every values-<locale> set against the default strings.xml
python scripts/localization_audit.py --res app/src/main/res

# Restrict to one locale (e.g. Spanish, French, Simplified Chinese)
python scripts/localization_audit.py --res app/src/main/res --locale es
python scripts/localization_audit.py --res app/src/main/res --locale zh-rCN

# Cross-reference source usage and fail the build on any gap (CI)
python scripts/localization_audit.py \
  --res app/src/main/res \
  --source app/src/main/java \
  --strict \
  --json
```

`--strict` turns findings into a non-zero exit; combine with `--json` for parseable CI output.
A quick on-device spot check of a translated screen:

```bash
python scripts/appearance.py --locale es     # best-effort locale switch
python scripts/app_launcher.py --restart com.example.app
python scripts/screen_mapper.py
python scripts/appearance.py --reset
```

---

## ANR / Jank Capture (anr_watcher.py)

`anr_watcher.py` records ANRs and dropped frames (Choreographer skipped-frames +
ActivityManager ANRs) into a **session** you can drill into later. Start returns a session ID;
stop emits a token-tight summary.

```bash
# 1. Start a detached session scoped to the app; capture the printed session ID
SID=$(python scripts/anr_watcher.py --start --package com.example.app)

# 2. Exercise the app (manual steps, or scripted navigation)
python scripts/navigator.py --find-text "Sync" --tap
# ... reproduce the jank ...

# 3. Stop and print the summary
python scripts/anr_watcher.py --stop "$SID"

# 4. Drill into the worst cluster, or list/compare sessions
python scripts/anr_watcher.py --get-details "$SID" --cluster 1
python scripts/anr_watcher.py --list-sessions
python scripts/anr_watcher.py --diff "$SID_BEFORE" "$SID_AFTER"
```

Useful modifiers:

```bash
# Force a one-line summary, or fit the summary to a token budget
python scripts/anr_watcher.py --stop "$SID" --terse
python scripts/anr_watcher.py --stop "$SID" --budget-tokens 400

# Legacy live stream (no session) for a fixed duration
python scripts/anr_watcher.py --watch --duration 60 --package com.example.app

# Housekeeping
python scripts/anr_watcher.py --clear-sessions --older-than 24h
```

---

## Multi-Device Test

Provision, boot, exercise, and tear down across several emulators. AVD creation/deletion is
handled by `emulator_create.py` / `emulator_delete.py`; boot/shutdown by
`emulator_boot.py` / `emulator_shutdown.py`.

```bash
for spec in "pixel_7:34" "pixel_tablet:34"; do
  DEVICE="${spec%%:*}"
  API="${spec##*:}"
  NAME="test-${DEVICE}-${API}"

  # Create a fresh AVD (x86_64 by default)
  python scripts/emulator_create.py --device "$DEVICE" --api "$API" --name "$NAME"

  # Boot headless and wait for readiness
  python scripts/emulator_boot.py --avd "$NAME" --wait-ready --headless

  # Install + launch the build
  python scripts/app_launcher.py --install app/build/outputs/apk/debug/app-debug.apk
  python scripts/app_launcher.py --launch com.example.app

  # Capture a per-device artifact
  python scripts/app_state_capture.py --package com.example.app --output "runs/$NAME/"

  # Tear down: shut the emulator then delete the AVD
  python scripts/emulator_shutdown.py --name "$NAME" --verify
  python scripts/emulator_delete.py --name "$NAME"
done
```

Discover available device definitions and system images before scripting:

```bash
python scripts/emulator_create.py --list-devices
python scripts/emulator_create.py --list-images
```

---

## Login Flow

Semantic navigation end to end: find by text/type, enter text, submit, verify. `navigator.py`
finds elements by `--find-text` (fuzzy), `--find-exact`, `--find-type`, or `--find-id`, then
acts with `--tap` or `--enter-text`. Use `keyboard.py` for hardware keys.

```bash
python scripts/app_launcher.py --launch com.example.app

# Open the login screen
python scripts/navigator.py --find-text "Login" --tap

# Fill the email field (first EditText) and the password field (second)
python scripts/navigator.py --find-type EditText --index 0 --enter-text "user@example.com"
python scripts/navigator.py --find-type EditText --index 1 --enter-text "s3cret!"

# Submit
python scripts/keyboard.py --button enter
python scripts/navigator.py --find-text "Sign In" --tap

# Verify we landed on the authenticated screen
python scripts/navigator.py --find-text "Welcome" --list

# On failure, capture everything for triage
python scripts/app_state_capture.py --package com.example.app --output login-failure/
```

Prefer `--find-id` with a stable `resource-id` when your layout exposes one — it survives copy
changes that would break `--find-text`. To target a hidden password field reliably, you can also
match by `--find-id` (e.g. `com.example.app:id/password`) instead of `--index`.
