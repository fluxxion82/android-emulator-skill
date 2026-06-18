# Troubleshooting

Common failures when running the Android Emulator Skill scripts, in **problem → cause → fix**
form. All script paths are relative to `skills/android-emulator-skill/scripts/`. Every Python
script supports `--help` and `--json`; most accept `--serial` for device targeting.

When in doubt, run the [quick diagnostics](#quick-diagnostics) first — it tells you whether the
environment itself is the problem before you debug any individual script.

---

## No devices / `adb` unauthorized

**Problem:** A script exits with `No devices connected. Start an emulator or connect a device`,
or `adb devices` shows a device in the `unauthorized` (or `offline`) state.

**Cause:** No device/emulator is attached, or a physical device has not accepted the host's
USB debugging RSA key. `resolve_device_identifier()` only counts devices whose state is
`device`; `unauthorized` and `offline` devices are not selectable.

**Fix:**
```bash
adb devices -l                 # inspect state column: device / unauthorized / offline
```
- If empty: boot an emulator (`python emulator_boot.py --avd <AVD> --wait-ready`) or connect a
  device with USB debugging enabled.
- If `unauthorized`: unlock the device and tap **Allow** on the "Allow USB debugging?" prompt.
  If no prompt appears, reset the trust handshake:
  ```bash
  adb kill-server && adb start-server
  ```
  Then replug the device and accept the prompt. Revoking USB debugging authorizations under
  **Settings → Developer options** forces a fresh prompt.
- If `offline`: `adb reconnect` (or `adb reconnect offline`), or restart the emulator.

---

## Multiple devices connected

**Problem:** A script picks the wrong device, or `adb` errors with
`more than one device/emulator`.

**Cause:** When more than one device is attached and no target is given, scripts pass `None`
to `resolve_device_identifier()`, which defers to adb's default-device behavior — and adb
refuses to guess when several devices are present.

**Fix:** Pass `--serial` explicitly. Get the serial from `adb devices -l`:
```bash
adb devices -l
python screen_mapper.py --serial emulator-5554
python navigator.py --serial emulator-5554 --find-text "Login" --tap
```
`--serial` also accepts a partial serial or the type alias `emulator` / `device` (first match
of that type wins). Lifecycle scripts use `--serial` the same way:
```bash
python emulator_shutdown.py --serial emulator-5554
```
(`build_and_test.py` is host-side and has no `--serial`; it targets a project, not a device.)

---

## `uiautomator dump` fails / empty hierarchy

**Problem:** `screen_mapper.py`, `navigator.py`, or `accessibility_audit.py` fail with
`Failed to get UI hierarchy`, or report zero elements.

**Cause:** `get_ui_hierarchy()` runs `adb shell uiautomator dump /sdcard/window_dump.xml`,
pulls the XML, and parses it. It fails when the screen has no accessible window (lock screen,
secure/FLAG_SECURE surface, a pure SurfaceView/game, or a transient animation), when the app
is not in the foreground, or when `/sdcard` is not writable.

**Fix:**
- Make sure the target app is foregrounded first:
  ```bash
  python app_launcher.py --launch com.example.app
  ```
- Unlock the device and dismiss any system dialog, then re-map:
  ```bash
  python screen_mapper.py
  ```
- Wait for animations/splash to settle and retry; `uiautomator` cannot dump mid-transition.
- Secure windows and DRM/`FLAG_SECURE` screens never expose a hierarchy — this is expected;
  fall back to a screenshot-based check (e.g. `app_state_capture.py`).
- Verify the raw dump manually if it keeps failing:
  ```bash
  adb shell uiautomator dump /sdcard/window_dump.xml && adb shell cat /sdcard/window_dump.xml | head
  ```

---

## `run-as` denied (non-debuggable app)

**Problem:** `container.py` (or `model_inspector.py`'s live-DB mode) reports that `run-as` was
denied — e.g. `is not debuggable`, `run-as: package not found`, or `permission denied`.

**Cause:** `adb shell run-as <package>` is permitted by the platform **only** for apps built
with `android:debuggable="true"` (debug builds). Release/store builds, and packages not
installed for the current user, are refused by design. Rooted-device `su` access is out of
scope for these scripts.

**Fix:**
- Inspect a **debug build** of the app instead of the release variant:
  ```bash
  python build_and_test.py --project . --module :app --variant debug
  # install the resulting debug APK, then:
  python container.py --package com.example.app --ls
  ```
- Confirm the package is installed for the active user (`adb shell pm list packages | grep
  com.example`).
- If you only have a release build, the private data dir is not reachable via `run-as`; there
  is no supported workaround in this skill.

---

## `gradlew` not found / wrong module

**Problem:** `build_and_test.py` fails with `gradlew not found in <dir>` or
`Project directory not found`, or a build targets the wrong Gradle module.

**Cause:** The builder requires the project's `./gradlew` wrapper to exist under `--project`.
It does not fall back to a system-wide `gradle`. A wrong/missing `--module` scopes tasks to a
module path that does not exist (tasks become e.g. `:wrongmodule:assembleDebug`).

**Fix:**
- Point `--project` at the directory that actually contains `gradlew` (the repo root for most
  Android projects, not the `app/` subdirectory):
  ```bash
  python build_and_test.py --project /path/to/AndroidProject --test
  ```
- Scope to a module with `--module` (alias `-p`); use the Gradle module path with a leading
  colon. The leading colon is added for you if omitted:
  ```bash
  python build_and_test.py --project . --module :app --variant debug
  ```
- Module names come from `settings.gradle[.kts]`. If unsure, run
  `./gradlew projects` in the project directory to list valid module paths.
- The wrapper's executable bit is set automatically (cloned repos sometimes drop it), so a
  missing `+x` is not the cause here — a missing `gradlew` file is.

---

## Emulator boot timeout

**Problem:** `emulator_boot.py --wait-ready` exits with
`Timeout waiting for emulator readiness after <N>s`.

**Cause:** Readiness is polled via `adb shell getprop sys.boot_completed`. A cold boot on a
slow host (or a freshly created/erased AVD doing first-boot setup) can exceed the default
timeout of **300 seconds**.

**Fix:** Raise the timeout, either per-run or via the environment tunable:
```bash
# per-run flag (default 300)
python emulator_boot.py --avd Pixel_7_API_34 --wait-ready --timeout 600

# environment override (read by the script's defaults)
ANDROID_EMU_BOOT_TIMEOUT=600 python emulator_boot.py --avd Pixel_7_API_34 --wait-ready
```
Other related tunables (all use the `ANDROID_EMU_` prefix; invalid values warn and fall back
to the default):

| Variable | Default | Effect |
|----------|---------|--------|
| `ANDROID_EMU_BOOT_TIMEOUT` | `300` | Max seconds to wait for `sys.boot_completed`. |
| `ANDROID_EMU_POLL_INTERVAL` | `0.5` | Seconds between readiness polls (min `0.05`). |

If boots are consistently slow, prefer `--headless` in CI, and confirm hardware acceleration
(HAXM/KVM/Hypervisor) is available via the [health check](#quick-diagnostics).

---

## Pillow (PIL) missing

**Problem:** Screenshot resizing fails, or the health check warns
`Pillow (PIL) not importable — screenshot resizing won't work`.

**Cause:** Pillow is an **optional** dependency used only for resizing screenshots (token
control in `test_recorder.py`, `app_state_capture.py`, inline screenshot modes). It is not
required for core navigation or build/test.

**Fix:**
```bash
python3 -m pip install pillow
```
Capture still works without Pillow — images are just saved at full resolution rather than
resized.

---

## Locale change not taking effect

**Problem:** `appearance.py --locale fr-FR` runs but the UI language does not change.

**Cause:** This is **best-effort by design**. Android has no unprivileged public API to switch
the global system locale. The script writes `persist.sys.locale` via
`adb shell setprop`, which does not relocalize a running system on a non-rooted device, and
the script reports honestly whether the change applied rather than pretending it always works.

**Fix:**
- Theme and text size are reliable — prefer them when you only need appearance changes:
  ```bash
  python appearance.py --theme dark
  python appearance.py --text-size large      # or --font-scale 1.3
  ```
- For a genuine locale switch, set it in the device UI
  (**Settings → System → Languages**), or boot/create an AVD configured for the target locale.
- A rooted emulator may honor `setprop persist.sys.locale` after a reboot, but that is outside
  the supported, non-privileged path.
- Reset appearance to defaults with `python appearance.py --reset` (light theme, font scale
  1.0).

---

## Location simulation only works on emulators

**Problem:** `location.py` refuses to run against a physical device:
`Target '<serial>' is a physical device, not an emulator.`

**Cause:** `location.py` drives `adb emu geo fix <lon> <lat>`, which talks to the **emulator
console**. Physical devices have no emulator console, so the command cannot reach them. The
script refuses up front instead of silently doing nothing.

**Fix:**
- Run against an emulator (AVD) serial, which starts with `emulator-`:
  ```bash
  python location.py --serial emulator-5554 --city london
  python location.py --serial emulator-5554 --lat 37.7749 --lng -122.4194
  ```
  (Note: the underlying console call is `emu geo fix <LON> <LAT>` — longitude first — but the
  script's `--lat`/`--lng` flags handle the ordering for you.)
- To mock GPS on a **real device**, install a mock-location provider app and select it under
  **Settings → Developer options → Select mock location app**. This skill does not automate
  that path.

---

## Quick Diagnostics

Run the bundled health check first. It verifies `ANDROID_HOME`/`ANDROID_SDK_ROOT`, and that
`adb`, `emulator`, `avdmanager`, `sdkmanager`, and `java` are on `PATH` (with versions),
checks for Python 3.12+ and Pillow, and lists connected devices and defined AVDs:

```bash
bash skills/android-emulator-skill/scripts/android_health_check.sh
```

- Exit `0` = all hard requirements satisfied (warnings allowed).
- Exit `1` = a hard requirement is missing (`adb` not on `PATH`).
- It prints `PASS` / `WARN` / `FAIL` plus targeted hints for each failed or warned check.

Manual spot checks:

```bash
# Are any devices connected, and in what state?
adb devices -l

# What AVDs are defined / which should I boot?
python skills/android-emulator-skill/scripts/device_list.py
python skills/android-emulator-skill/scripts/emulator_selector.py --suggest

# Can I read the current screen? (foreground app + accessible window required)
python skills/android-emulator-skill/scripts/screen_mapper.py

# Does the emulator report boot-complete?
adb shell getprop sys.boot_completed     # "1" once ready
```
