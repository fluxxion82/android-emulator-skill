---
name: android-emulator-skill
version: 0.8.0
description: Production-ready scripts for Android app testing, building, and automation. Provides semantic UI navigation, build automation, accessibility testing, and emulator lifecycle management. Optimized for AI agents with minimal token output. Android equivalent of ios-simulator-skill.
---

# Android Emulator Skill

Build, test, and automate Android applications using accessibility-driven navigation and structured data instead of pixel coordinates.

## Invoking these scripts

Scripts live in `scripts/`, beside this file. **Use a path rooted at the skill
directory, not a bare relative path** — when the skill is installed as a plugin
and you are working in a project, `python scripts/foo.py` is a file-not-found.

```bash
SKILL_DIR=/path/to/skills/android-emulator-skill
python3 "$SKILL_DIR/scripts/screen_mapper.py" --json
```

The examples below are written with `$SKILL_DIR` set that way.

## Quick Start

```bash
# 1. Launch app
python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app

# 2. Map screen to see elements
python3 "$SKILL_DIR/scripts/screen_mapper.py"

# 3. Tap button (add --scroll-to-find if it may be below the fold)
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap

# 4. Enter text
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --enter-text "user@example.com"

# 5. Run accessibility audit
python3 "$SKILL_DIR/scripts/accessibility_audit.py"
```

All scripts support `--help` for detailed options and `--json` for machine-readable output.

## Scripts (v0.8.0)

### Implemented (32 scripts)

#### Core Utilities (14 modules in `common/`)
1. **common/device_utils.py** - ADB command building and device detection
2. **common/screenshot_utils.py** - Screenshot capture and processing
3. **common/cache_utils.py** - Progressive disclosure cache system

   Also in `common/`: **cli.py** (the error boundary every script ends in,
   `cli.run(main)`; its contract, and what it does not cover yet, is the
   **Failures** entry under Known gaps below), **adb_exec.py** (the one bounded
   entry point for every adb call, with typed errors that name a remedy),
   **hierarchy.py** (the one way to capture the UI hierarchy — no temp files,
   so concurrent runs cannot read each other's screen), **emu_console.py** (`adb emu`, which exits 0 even when it
   fails), **logcat.py** (the one place that builds an `adb logcat` argv and
   parses a duration, shared by all four log readers), **sdk_tools.py** (resolves
   SDK binaries; `emulator` by bare name hits the `<sdk>/emulator` *directory* on an
   SDK-root PATH and raises `PermissionError`), **run_as.py** (reads a
   debuggable app's files with `adb shell -T run-as`, never `exec-out`, which
   answers a missing file with its error text as the bytes; checks the adb
   client honours `-T` first; decides what a run-as refusal is, and lists a
   directory as toybox find's NUL-terminated records, for both inspectors),
   **env_config.py**, **anr_pipeline.py**,
   **anr_sessions.py**, **log_lifecycle.py** (system_server's process
   sentences — `Start proc` / `Killing` / `has died` — authenticated one way for
   `log_monitor --app` and the ANR session worker alike).

#### App Management (1 script)
4. **app_launcher.py** - App lifecycle management
   - Launch apps by package name. `--launch` **waits for the activity to be
     displayed** (`am start -W`) and fails if the system reports anything but
     `Status: ok`, so the next command maps the app's screen rather than
     whatever was still in front. The message reports the activity that came up
     and how long it took.
   - Terminate apps
   - Install/uninstall APKs
   - Deep link navigation. `--open-url` waits the same way `--launch` does and
     fails when the intent resolves to nothing, rather than reporting a URL it
     merely handed to the system.
   - List installed packages
   - Check app state
   - Options: `--launch`, `--terminate`, `--install`, `--uninstall`, `--open-url`, `--list`, `--state`, `--json`

#### Device Lifecycle (5 scripts) ✓ COMPLETE
5. **emulator_boot.py** - Boot emulators with optional readiness verification
   - Boot by AVD name
   - Wait for device ready with timeout
   - Batch boot operations
   - Headless mode support
   - `--list-avds` reports an empty list (`No AVDs found`, or `{"avds": []}`)
     and exits **0** only when the emulator ran and this host defines no AVDs.
     A missing or failing `emulator` binary is an error naming where it was
     looked for, and exits 1 (`{"success": false, "error": ..., "remedy": ...}`
     under `--json`) — "no AVDs" and
     "could not look" are different answers. `--all` reports the same failure
     the same way
   - Refuses to boot while any attached emulator cannot say which AVD it is
     (a non-`device` state, or a console that will not answer), or while the
     emulators cannot be listed at all: it may be this AVD, and a second
     instance of one AVD corrupts its userdata. The refusal names the serial,
     its state, and says to terminate the stale emulator *process* — a console
     that is not answering cannot be shut down through one
   - Failures — a refused or failed boot, a missing `--avd` — are the shared
     refusal: `{"success": false, "error": ..., "remedy": ...}` under `--json`,
     `Error:` / `Remedy:` on stderr otherwise, exit 1. A successful boot under
     `--json` answers `{"success": true, ...}` (it reported a defect, exit 1,
     before v0.8.0)
   - Options: `--avd`, `--wait-ready`, `--timeout`, `--headless`, `--list-avds`, `--json`

6. **emulator_shutdown.py** - Gracefully shutdown emulators
   - Shutdown by serial number. Emulators only: a serial that is not an
     `emulator-NNNN` is refused before any adb command runs, so this never powers off an
     attached phone or tablet
   - `--name AVD` resolves the AVD to a serial. If some emulator could not be
     queried — or the emulators could not be listed at all — that is reported
     as itself and exits non-zero, never as "No running emulator found", which
     is a confident negative over a device nobody managed to ask
   - Failures are the shared refusal: `{"success": false, "error": ...,
     "remedy": ...}` under `--json`, `Error:` / `Remedy:` on stderr otherwise,
     exit 1 (3 when adb says the device is not there)
   - Optional verification of shutdown completion
   - Batch shutdown operations (running emulators only)
   - Options: `--serial`, `--name`, `--verify`, `--timeout`, `--all`, `--json`

7. **emulator_create.py** ⭐ NEW - Create AVDs dynamically
   - Create by device type and API level
   - List available device definitions
   - List available system images
   - `--abi` defaults to this host's: `arm64-v8a` on an arm64/aarch64 machine
     (an Apple-silicon Mac's emulator boots nothing else), `x86_64` otherwise;
     `--help` states the value it will use. A Python running under Rosetta
     reports `x86_64`, so the default is wrong there: pass `--abi arm64-v8a`
     explicitly
   - Checks the image is installed before creating. No `sdkmanager` (or no
     `avdmanager`) is an error naming where it was looked for, exit 1 — never
     an empty listing, and never a create of an image nobody could check
   - Every refusal — a device definition not found, no API level, an image
     not installed (the error names the installed ones), a create avdmanager
     refused, a missing `--device` — is the shared `{"success": false,
     "error": ..., "remedy": ...}` under `--json` (it was `{"success": false,
     "message": ...}`), `Error:` / `Remedy:` on stderr otherwise, exit 1
   - Reads both `sdkmanager` listing formats: the Java tool's pipe table
     (every cmdline-tools before 23, and CI images) and the slash-path columns
     of cmdline-tools 23's Android-CLI wrapper. It read only the second, so
     with the Java tool an installed image was reported as not installed
   - Options: `--device`, `--api`, `--name`, `--abi`, `--variant`, `--list-devices`, `--list-images`, `--json`

8. **emulator_delete.py** ⭐ NEW - Delete AVDs permanently
   - Delete by AVD name, or in batch with `--all` / `--old N` (`--yes` skips the prompt)
   - List available AVDs
   - Every mode reports a missing or failing `avdmanager` the same way: exit 1
     with the `cmdline-tools` remedy, never "No AVDs deleted" at exit 0
   - `--old N` ranks by the mtime of `<name>.avd`; an AVD whose directory
     cannot be found fails the whole ranking (exit 1) rather than sorting
     oldest and being deleted. The AVD home is `$ANDROID_AVD_HOME`, else
     `$ANDROID_SDK_HOME/.android/avd`, else `~/.android/avd`
   - `N` must be at least 1: `--old 0` keeps nothing, so it is rejected as a
     usage error (exit 2) pointing at `--all --yes`
   - Without `--yes` the question goes to stderr and the answer is read from
     stdin; anything but `yes` (including end of input) deletes nothing and is
     a refusal in every mode — `deletion cancelled`, exit 1 — so `--all` and
     `--old` no longer answer a "no" with an empty batch at exit 0
   - Failures answer `{"success": false, "error": ..., "remedy": ...}` under
     `--json` — including a `--name` that is not listed, and a delete
     avdmanager refused or did not finish; a batch keeps its summary and
     carries `error` alongside it when any AVD failed (exit 1)
   - Options: `--name`, `--all`, `--old`, `--yes`, `--list`, `--json`, `--verbose`

9. **emulator_erase.py** ⭐ NEW - Factory reset AVDs
   - Wipe user data without deleting AVD
   - Preserve AVD configuration
   - Reads the AVD home directly (`$ANDROID_AVD_HOME`, else
     `$ANDROID_SDK_HOME/.android/avd`, else `~/.android/avd`); no SDK tool is
     consulted. An AVD home that does not exist is an error naming the path,
     exit 1 — not "No AVDs found"
   - Batch erase with `--all`; `--verify` polls the AVD on disk until the wipe
     lands, bounded by `--timeout` (default 90s, `ANDROID_EMU_ERASE_TIMEOUT`)
   - Refuses to erase unless every attached emulator has been identified and
     none of them is this AVD. A console query that failed, or an emulator not
     yet in state `device`, is "unknown" and refuses, naming the serial, its
     state and the remedy — `--force` erases anyway
   - Every failure is the shared refusal: `{"success": false, "error": ...,
     "remedy": ...}` under `--json`, `Error:` / `Remedy:` on stderr otherwise,
     exit 1. `--all` keeps its per-AVD summary and carries `error` alongside it
     when any AVD failed
   - **Snapshots are kept**: a successful erase prints "snapshots kept; use
     snapshot.py --delete `<name>` to remove them", because a later
     `snapshot.py --load` can otherwise undo the factory state
   - Options: `--name`, `--all`, `--force`, `--verify`, `--timeout`, `--list`,
     `--json`, `--verbose`

#### Build & Development (2 scripts) ✓ COMPLETE
10. **build_and_test.py** - Gradle build/test automation with progressive disclosure (backed by the `gradle/` subpackage)
    - One-line summary + a result ID by default; drill in on demand
    - Parses JUnit XML for pass/fail counts + failed test names; extracts Gradle/Kotlin/Java errors & warnings
    - For `--module ... --test`, the module's report directory is Gradle's own answer: it runs
      `./gradlew -q <module>:properties --property projectDir` first (one extra configuration of the
      build per such run; a build without `--test` does not ask) and never reads the settings script
      itself, so any `projectDir`/`setProjectDir`/`includeFlat`/settings-plugin mapping is honoured.
      A module Gradle does not know, a Gradle failure or timeout (`ANDROID_EMU_BUILD_TIMEOUT`), an
      answer without exactly one `projectDir:` line, and a directory that is missing or outside
      `--project` are refused by name before any report is deleted or the test task starts
    - `--test` reads only the reports *this* run wrote: it deletes the test task's old `TEST-*.xml` reports
      first, after proving their resolved paths stay inside the selected module and exact task directory,
      and reruns the task (`--rerun`, Gradle 7.6+; an older wrapper is refused by name). It passes only
      if Gradle exits 0 **and** they hold no failure or error — `ignoreFailures = true` cannot turn failing
      tests into a pass. An unreadable or malformed report refuses the partial verdict. A well-formed empty
      suite contributes no tests alongside nonempty reports; if every collected report has zero testcases,
      the existing no-results rule refuses the run. A run that wrote no test results is a failure, not "0 tests
      passed". If Gradle times out, its partial log is cached and the command refuses with a remedy to raise
      `ANDROID_EMU_BUILD_TIMEOUT` or `ANDROID_EMU_TEST_TIMEOUT` for the active mode, or inspect the stuck task
    - Options: `--project`, `--module`/`-p`, `--variant`, `--clean`, `--test`, `--suite`, `--get-errors`/`--get-warnings`/`--get-log <ID>`, `--list-builds`, `--verbose`, `--json`

11. **log_monitor.py** ⭐ NEW - Real-time logcat monitoring
    - Reads logd's records (`logcat -B`), never text: each record whole, with
      the pid and uid logd took from the writing socket's credentials -- the
      only metadata an app cannot write (the tid and time are the writer's
      own). In text, a tag an app chooses can hold a newline and a whole
      header, so one app record printed a second line "from" system_server.
      Each record is printed back as `-v threadtime -v usec` would print it,
      in this host's local time, with the control characters of its tag and
      the line-ending and terminal controls of its message escaped (`\r` as
      `\\r`, ESC as `\\x1b`; TAB is kept) -- so what it prints or saves reads
      back as the same lines in any reader
    - A stream that ends inside a record is never a complete capture: on a
      `--last` dump, or a live read that ended by itself, the device is asked
      again (gone: exit 3) and otherwise the capture is refused (exit 1). Only
      when the read was stopped by its own `--duration` or an interrupt is
      the record that was arriving dropped -- and counted
      (`statistics.cut_records`, `attribution.complete: false`, a stderr
      note, an `INCOMPLETE` line in the text summary)
    - Filter by app package: the records of each process the device vouches
      was the app's, inside that process's life, that logd stamped with one of
      the package's uids. A pid counts when its uid is the package's: the one
      `pidof` names (its uid checked with `ps`), or one system_server's
      `Start proc` / `Killing` record names with the package's uid (`has
      died`, which has no uid, only ends a life). Only system_server's own
      records count (logd's pid, checked against `pidof system_server`), and
      only a sentence that opens the record's message; unknown system_server
      or uid, nothing counts. A crash's own `Process: <pkg>, PID: N` record is
      not evidence: any app can write one. So a crash before a restart stays
      in the window (`app_pids`, and `attribution` -- what decided it -- in the
      JSON). A live read holds back the package uid's records of a pid not
      yet named, keeps them if it is (stored in input order; marked `[held
      back]` under `--follow`), up to `ANDROID_EMU_LOG_PENDING_CAP` (2000)
      lines, and counts what it dropped. An app with no process in the window
      is refused, not widened to the whole device
    - Says when the window is incomplete: records logd stamped with the
      package's uid whose process nothing in the window ties to the package
      -- a process that started before a `--last` window and is gone by its
      end, such as a crash read too late (`has died` names no uid) -- are
      left out, counted and named (`attribution.unresolved_lines` /
      `unresolved_pids`, `attribution.complete: false`, a stderr note, and an
      `Attribution: INCOMPLETE` line in the text summary). Counts are of what
      is shown; widen `--last` to include the start
    - A live read (`--follow`, `--duration`) starts at the end of the device's
      buffer (`logcat -T 1`, which first prints the one most recent record,
      every line of it) and reads what happens from then on; it used to replay
      the whole buffer first. Use `--last` for history. A process already
      running when a live read begins counts from the first record the read
      sees, so a replayed buffer could hand it the older records of another
      process that had the same pid
    - A device that is not there is never a quiet capture: every mode asks
      `adb get-state` first, and a live read that ends by itself asks again.
      (A streaming `adb logcat` aimed at a device that is not attached waits
      instead of failing, so `--duration` used to report it as zero lines.)
      Unavailable is exit 3, as everywhere else
    - A live read (`--follow`, `--duration`) ends only when it is stopped: by
      its `--duration`, or an interrupt, and either is the capture. One that
      ends by itself -- whatever adb's exit status, cut or not -- cannot show
      it observed what was asked: exit 3 when the device went, otherwise a
      refusal (exit 1) that keeps what it read, its line count in the error
      and its JSON summary under `partial` (`--json`). It used to be the
      capture, with a note on stderr
    - A `--last` dump has a deadline of its own: twice `ANDROID_EMU_ADB_TIMEOUT`
      (60 s by default). A dump that has not ended by then -- a device lost after
      the first question (`logcat -d` waits for it too), a stream that stalls
      mid-record -- is stopped, the device is asked again (gone: exit 3), and
      otherwise the dump is refused (exit 1), never reported as the window. It
      used to wait for as long as the stall lasted. `--follow` (and no mode
      flag) reads until interrupted
    - Identity is the uid (every user's uid for the package). A process
      another package runs under the same shared uid, named after this
      package, counts as this app's: the device does not tell them apart
    - Filter by severity: `error`, `warning`, `info`, `debug`, `verbose`; any
      other name (`warn`) is a usage error (exit 2). The floor is applied on
      the host (`-B` ignores filterspecs), after attribution, so
      `--severity error` still sees the records that name the app's processes
    - Smart deduplication. In the JSON, `statistics.errors` /
      `statistics.warnings` count LINES (E/F, W); `statistics.unique_errors` /
      `unique_warnings` count distinct messages; the top-level `errors` /
      `warnings` are those distinct messages as a list, capped at
      `ANDROID_EMU_LOG_JSON_CAP` (20)
    - Duration-based or follow mode. `--duration` / `--last` need a unit
      (`30s`, `5m`, `1h`); a bare number is rejected as a usage error (exit 2)
    - Save logs to file
    - Options: `--app`, `--serial`, `--severity`, `--follow`, `--duration`, `--output`, `--clear`, `--verbose`, `--json`

#### Navigation & Interaction (4 scripts)
12. **screen_mapper.py** - Analyze current screen and list interactive elements
    - Listing is the default, not a mode: **there is no `--list`.** The bare
      command prints the summary; `--verbose` expands it to the per-element
      breakdown, `--hints` adds navigation suggestions.
    - The default output **names every interactive control**, one line per kind
      (`Button:`, `Control:`, `CheckBox:`, `EditText:` …), capped per kind. Those
      names are exactly what `navigator.py --find-text` accepts, so step 2 of
      Quick Start feeds step 3 directly. It used to print counts only, and on a
      Compose screen the names existed solely under `--verbose`/`--json`.
    - A control with no text of its own is named by the caption recovered from
      its subtree or its row; one with a resource id is named by the **bare** id
      (`com.android.settings:id/search_action_bar` prints as
      `search_action_bar`), which is what `navigator` prints and what
      `--find-id` takes. Compose test tags are already bare, so both toolkits
      name things the same way.
    - `--hints` prints the next command to run, already filled in.
    - Options: `--serial`/`-s`, `--verbose`/`-v`, `--hints`, `--json`

13. **navigator.py** - Find and interact with elements semantically
    - Find by text, type, resource ID
    - Tap, enter text, get bounds
    - Fuzzy matching support
    - `--find-text` matches **any name the screen report printed**: an element's
      own text or content-desc, the caption recovered for an unlabelled control,
      or a resource id (bare or fully qualified, matched whole). A match on a
      caption resolves to the control that owns it -- the control it sits
      inside, or the one beside it in the same row -- so the tap lands on the
      checkbox rather than 143px to its right. The owner must be **tappable**
      (clickable / long-clickable / checkable): a scrolling container is
      interactive, but it is not what a caption inside it names, and resolving
      to it would tap the middle of the screen. A name that matches only a
      passive label with no such control is refused, not tapped.
    - `--tap` and `--enter-text` **require a target**: one of `--find-text`,
      `--find-exact`, `--find-type`, `--find-id`, or explicit `--tap-at x,y`.
      Without one it is a usage error (exit 2) and nothing is sent to the device.
    - `--max-scrolls` is capped at 50 (so is `ANDROID_EMU_MAX_SCROLLS`), and a
      `--scroll-to-find` search is also bounded in wall-clock time
      (`ANDROID_EMU_SCROLL_SEARCH_DEADLINE`, default 120s -- see `--help`),
      which bounds the screen dumps too. Each scroll prints a progress line to
      stderr as it happens.
    - Under `--json`, a successful `--tap`/`--enter-text` reports
      **`tapped_at: [x, y]`** -- the coordinates that reached the device, so a
      caller can check where the tap went without parsing prose. Every failure
      is the shared refusal — `{"success": false, "error": ..., "remedy": ...}`
      under `--json`, `Error:` / `Remedy:` on stderr otherwise (it was the
      message alone on stdout), exit 1 — and a lookup's refusal carries its
      `search` detail in the object. A usage error still prints `{"error": ...}`
      at exit 2 (see Failures under Known gaps).
    - **`--scroll-to-find`** searches below the fold. Without it a lookup sees only the visible
      screen, and `Not found` is indistinguishable from "the item is two rows down". The default
      path now says which it was: `(searched 1 screen; this screen scrolls -- retry with
      --scroll-to-find)`. Scrolling is opt-in on purpose -- a swipe is not a read, so a lookup
      that scrolled silently would let `--find-text "Save is visible"` pass for a button three
      screens away.
    - Options: `--find-text`, `--find-type`, `--find-id`, `--tap`, `--enter-text`, `--list`,
      `--scroll-to-find`, `--scroll-direction {down,up}`, `--max-scrolls N`, `--serial`,
      `--json`, `--verbose`

14. **gesture.py** - Perform swipes, scrolls, long press
    - `--scroll {up,down}` names the direction the **content** moves; `--swipe` names what the
      **finger** does. They are opposites. (Conflating them was a real defect: `--scroll down`
      dragged lists back toward the top and reported success.)
    - Directional swipes
    - Custom swipe coordinates
    - Scroll and long press
    - Options: `--swipe`, `--from-edge`, `--duration`, `--long-press`, `--scroll`, `--serial`, `--json`

15. **keyboard.py** - Text input and hardware buttons
    - Type text with **`--type`**. There is no `--text` — that flag belongs to
      `push_notification.py`, and guessing it here costs a turn. `--delay N`
      types one character at a time for fields that debounce.
    - Press special keys (`--key enter`, repeated with `--count N`) and hardware
      buttons (`--button back`); `--keys a,b` presses a sequence. `--key` and
      `--button` share one name table, so either accepts either name.
    - `--clear N` deletes N characters; `--hide-keyboard` / `--dismiss` put the
      soft keyboard away. Both read `dumpsys input_method` first and press BACK
      only when an IME is actually shown — BACK is not a "hide keyboard" key,
      and with no keyboard up it leaves the current screen. With none shown they
      report "No keyboard shown", send no key event and exit 0; if the IME state
      cannot be read they exit 1 rather than guess. To press BACK regardless,
      ask for it: `--button back`. (The state is read from `mInputShown`, with
      `mIsInputViewShown` as a backup — both measured on API 33 and API 35.)
    - Options: `--type`, `--delay`, `--key`, `--count`, `--keys`, `--button`,
      `--clear`, `--hide-keyboard`, `--dismiss`, `--serial`/`-s`, `--json`

#### Testing & Analysis (4 scripts) ✓ COMPLETE
16. **accessibility_audit.py** ⭐ NEW - WCAG compliance checking
    - Missing content descriptions: any **operable** control (by its uiautomator
      properties, not its class name) with no describing text in itself or its
      subtree is critical. The old class-name gate could not fire on a Compose
      screen at all, where controls are plain `android.view.View`.
    - Touch target size verification
    - EditText hint checking
    - Image accessibility
    - Categorize by severity (critical/warning/info)
    - Save reports (JSON + Markdown)
    - Options: `--serial`, `--output`, `--verbose`, `--json`

17. **visual_diff.py** - Compare screenshots for visual changes
    - Pixel-perfect comparison
    - Highlight differences
    - Generate diff images
    - Usage: `visual_diff.py BASELINE CURRENT [--output DIR] [--threshold N] [--json]`
    - Note: the two images are **positional**. `--json` now emits the full
      report, so this script keeps the same contract as every other one;
      `--details` survives as a deprecated alias for callers written before it.
    - Options: `--output`, `--threshold`, `--json`, `--details`

18. **test_recorder.py** - Document a test run, step by step
    - **Session-based, one process per step** — there is no `--test-name`,
      `--output` or `--inline`. `--start NAME` opens a session and prints its
      id; each later `--step "description"` captures the current screen into
      it; `--stop` writes `report.md` + `manifest.json`. `--step` and `--stop`
      default to the newest session, or target one with `--session ID`.
    - Each step records a screenshot and the UI hierarchy, plus optional
      `--screen`, `--state` and `--assert` (`--assert-failed` marks it failed).
    - Sessions live under `~/.android-emulator-skill`, not in an output
      directory you pass: retrieve with `--list` / `--get-details ID`, remove
      with `--clear [--older-than 24h]`.
    - Options: `--start`, `--step`, `--stop`, `--list`, `--get-details`,
      `--clear`, `--session`, `--serial`, `--app-name`, `--size`, `--screen`,
      `--state`, `--assert`, `--assert-failed`, `--failed`, `--older-than`,
      `--verbose`, `--json`

19. **app_state_capture.py** ⭐ NEW - Complete debugging snapshots
    - Capture screenshot + UI hierarchy + logs + app info
    - Create timestamped snapshots, by default under
      `~/.android-emulator-skill/cache/app-state-snapshots/` (never the working
      directory); the snapshot's path is printed, and is `snapshot_path` under `--json`
    - Logs are the app's own, read from logd's records (`logcat -B`) by the
      same rule as `log_monitor --app`: the running pid (uid checked with
      `ps`) plus each pid system_server's own `Start proc` / `Killing` record
      names with the package's uid, inside that process's life -- a pid
      reused by another process is not the app's outside it -- and only
      records logd stamped with the package's uid. A crash before a restart
      is kept, by its `Start proc`. Not evidence: `has died` (no uid; it only
      ends a life), AndroidRuntime's `Process: <pkg>, PID:` (any app can write
      it), any line an app's tag or message could forge. Errors and warnings
      are counted from the priority column, lines split at `\n` only, and a
      message's line-ending and terminal controls are saved escaped (`\r` as
      `\\r`), so `app-logs.txt` reads back as the lines that were counted. A
      dump that ends inside a record, or does not finish in 60 s, asks `adb
      get-state` again, as `log_monitor` does: gone is exit 3 with what arrived;
      still there, the cut or the timeout fails the logs (exit 1). The cut is
      judged before adb's exit status, which a transport loss also sets (255).
      An app with no process in the window fails the logs rather than
      returning the whole device's
    - Lines of the package's uid left out for want of evidence (a crash whose
      start is older than `--logs`) are counted and named: `logs.attribution`
      (`complete: false`, `unresolved_lines`, `unresolved_pids`) in the
      summary, `log_attribution` under `--json`, a line in `summary.md` and in
      the text output, and a stderr note
    - A device that becomes unavailable during the capture is exit 3, as
      everywhere else, with what did arrive under `partial`
    - All-in-one debugging artifact
    - Options: `--package`, `--output`, `--serial`, `--logs`, `--no-logs`, `--log-lines`, `--screenshot-size`, `--json`

#### Advanced Testing & Permissions (3 scripts) ✓ COMPLETE
20. **privacy_manager.py** ⭐ NEW - App permission management
    - Grant/revoke permissions
    - List app permissions
    - Support for 20+ permission types
    - Batch operations
    - Options: `--grant`, `--revoke`, `--list`, `--package`, `--serial`, `--list-permissions`, `--json`

21. **status_bar.py** ⭐ NEW - Status bar control
    - Set battery level and charging state
    - Set WiFi/mobile signal strength
    - Set time display (for consistent screenshots)
    - Demo mode support
    - Options: `--preset`, `--battery`, `--charging`, `--wifi`, `--mobile`, `--time`, `--reset`, `--serial`, `--json`
    - All of these drive SystemUI **demo mode**, which changes what the status
      bar *draws*. It does not change what the app reads from the system
      (`BatteryManager` still reports the real level).

22. **push_notification.py** - Post into the shade, and read back what is posted
    - Rescoped in v0.6.0 to what adb can actually do. It no longer claims to
      send an app's notifications: `--post` posts via `cmd notification post`,
      and the result is owned by **com.android.shell** on channel `shell_cmd`,
      *not* by the app under test. The app's own channel, receiver and
      rendering are therefore not exercised. It is still the way to drive a
      NotificationListenerService, the shade UI, or an agent that reacts to a
      notification. `--tag` is the title and key, `--text` the body.
    - `--list` reads back what is really posted, by any package — the check
      that a notification exists rather than that a command exited 0.
      `--expect-package PKG` turns that read-back into an exit status.
    - A failed post, read-back or permission change is the shared refusal
      (`{"success": false, "error": ..., "remedy": ...}` under `--json`,
      carrying what it found), exit 1; a device adb cannot reach is exit 3
    - `--grant-permission` / `--revoke-permission` toggle `POST_NOTIFICATIONS`
      (API 33+) on `--package`, for exercising the runtime-permission path.
    - Options: `--post`, `--list`, `--grant-permission`, `--revoke-permission`,
      `--tag`, `--text`, `--no-verify`, `--expect-package`, `--package`,
      `--serial`, `--json`, `--verbose`
    - ⚠️ It cannot deliver into an app's own FCM handler — no adb path reaches
      that, because the c2dm receiver is protected by a permission held by
      Play services, not by the shell user. Send through FCM instead. Channels
      cannot be listed either: `cmd notification` has no `list channels`
      subcommand, which is why the old `--list-channels` never worked.

#### Discovery & Device State (14 scripts) ⭐ NEW

23. **android_health_check.sh** - Verify the environment (ANDROID_HOME, adb, emulator, avdmanager, sdkmanager, java, Python 3.12+, Pillow); lists connected devices and AVDs. Exits non-zero if adb is missing.
    - Every probe is bounded (10 s), the Python version and Pillow probes
      included. A `python3` on PATH that cannot run is a warning saying so,
      not "Python 0.0 found"
    - `--json` prints one object on stdout (progress goes to stderr). When
      `python3` cannot encode it, a Bash encoder does; when the report cannot
      be emitted at all (no temporary directory, or a check record that could
      not be written or read back), it exits 1 with one `{"success": false,
      "error": ..., "remedy": ...}` line — never an empty stdout
    - When Python runs, encoder stdout is parsed as one complete report with the expected keys;
      without Python, the Bash guard enforces one brace-balanced top-level object. Concatenated
      startup-hook output is discarded before the Bash fallback is used
    - Options: `--json`, `--help`

24. **device_list.py** - List connected devices (`adb devices -l`) and defined AVDs (`emulator -list-avds`) with progressive disclosure.
    - An empty inventory at exit 0 means the tools ran and found nothing. A
      missing or failing `adb` or `emulator` exits 1 with the remedy
      (`{"success": false, "error": ..., "remedy": ...}` under `--json`).
      `avdmanager` is the exception: it only adds target/ABI detail, so its
      absence is a warning on stderr (and in
      `warnings` in the JSON) rather than a failure
    - Options: `--get-details`, `--device-type`/`--name`, `--json`

25. **emulator_selector.py** - Suggest the best AVD (ranked by running → recently used → latest API → common models); list or boot one.
    - A host with no AVDs ranks nothing and exits 0; a missing or failing
      `emulator` binary exits 1 with the remedy instead of an empty ranking
      (`{"success": false, "error": ..., "remedy": ...}` under `--json`), and
      so does a `--boot` that is refused
    - An emulator that will not say which AVD it is gets no "currently running"
      bonus, but is listed with its state (`unidentified_emulators` under
      `--json`, a warning on stderr otherwise) so the ranking never claims a
      completeness it does not have. If the emulators cannot be listed at all,
      the ranking still runs — the AVDs on disk are real — and says so
      (`warnings` under `--json`). The same goes for an AVD whose `config.ini`
      cannot be read: it is ranked without its API level and device, and named
      in `warnings` (and on stderr) with the path that was tried
    - Options: `--suggest`, `--list`, `--boot NAME`, `--headless`, `--count`, `--json`, `--verbose`

26. **localization_audit.py** - Audit `res/values*/strings.xml` for missing keys per locale and placeholder mismatches; optional source cross-reference.
    - An audit of nothing is a refusal (exit 1), never "No issues found": a
      `--res` with no `values/strings.xml` (often a path one level off, e.g.
      `app/src/main` for `app/src/main/res`), or a `--locale` with no
      `values-<code>/strings.xml` there. `--strict`'s exit 2 stays for findings
    - Options: `--res DIR`, `--source DIR`, `--locale CODE`, `--strict`, `--json`, `--verbose`

27. **appearance.py** - Control dark/light mode (`cmd uimode night`) and font scale (`settings put system font_scale`); best-effort locale.
    - Options: `--theme {light,dark}`, `--text-size {small,default,large,xl}`, `--font-scale`, `--locale`, `--reset`, `--serial`, `--json`

28. **location.py** - Simulate GPS on an emulator via `adb emu geo fix` (fixed coords, city presets, GPX route replay). Emulator-only.
    - Options: `--lat`/`--lng`, `--city`, `--gpx FILE`, `--interval`/`--speed`, `--clear`, `--list-cities`, `--serial`, `--json`

29. **container.py** ⭐ NEW - Inspect a **debuggable** app's sandbox via `adb shell run-as` (fails clearly on release apps).
    - List/read files, dump `shared_prefs` XML, list databases and dump SQLite schema (Room == SQLite), export a snapshot
    - File names come from the app, so they are untrusted. A directory is
      listed with `find -H <dir> -maxdepth 1 -printf` (toybox; measured on
      0.8.11, Android 15): six NUL-terminated fields per entry — mode, owner,
      group, size, symlink target, name — read as bytes. NUL is the one byte a
      name cannot hold, so a name holding a newline, a CR or a whole forged
      line is one name: it can no longer add or hide an entry, as it could
      through `ls -la`. The directory's own record comes first, so a path that
      is a file is refused as not a directory, and an answer that is not whole
      records (empty, cut short, or another tool's) is refused whole — never
      read as an empty directory, never retried with `ls`. Refused per entry: a
      name that is not UTF-8 or not a plain file name (`/`, `\`, NUL, CR, LF,
      `.`/`..`), a field find does not print, and a symlink target that is
      empty, not UTF-8, or holds a LF, CR or NUL. A refused entry is counted
      in `rejected` and named, escaped, in `rejected_entries` (`--verbose`
      prints them), and the answer exits 1 with what could be read still shown
      — all refused is never "No shared_prefs found"
    - Two limits of that listing. It is only as complete as adb's exit status
      says: the records carry no end marker, so an answer cut after a complete
      record at exit 0 reads as a directory with fewer entries (a cut adb or
      find reports, a non-zero exit, is refused). And `-H` follows the named
      path's own symlink to any directory the app's uid may search, inside its
      data dir or not — `--ls files/x`, where the app linked `x` elsewhere,
      lists that directory's names and modes. A listing reads no file contents;
      `--export`'s reads of the files a listing names follow their symlinks
      too, and do not skip FIFOs or other special files (as before this
      listing; v0.9.0)
    - Requires adb 1.0.36 or later (platform-tools 24+), checked once with
      `adb version` before the first file read: an older client does not
      refuse `shell -T` but forwards it to the device and exits 0 after the
      error, so reads would take the error for the file. Older, or a banner
      with no version, is refused (exit 1) and nothing is read or written
    - Files are read with `adb shell -T`, so a failed read is a failure: `--cat`
      of a missing file exits 1 (over `exec-out` it answered with the error text
      as the content). `--export` counts a listed file it could not read in
      `unread` / `unread_entries` and exits 1, and refuses (exit 1, nothing
      written) a name that would land outside the snapshot
    - `--export` lists `shared_prefs/` and `databases/` only when the data dir
      holds them. One it does not hold is named in `absent` — the app has none,
      a complete export at exit 0, and `databases.json` says `"absent": true` —
      but only when that listing refused no entry: a refused entry may be the
      directory, so the export is refused first (`presence_unknown`, exit 1,
      nothing written).
      One it holds whose own listing fails (unreadable, or gone since) is in
      `unlisted` / `unlisted_dirs` with the device's reason; nothing is written
      for it, and the export exits 1 naming what was written and where (a
      failed listing used to read as an empty directory, at exit 0)
    - A failed or incomplete inspection is the shared refusal (`{"success":
      false, "error": ..., "remedy": ...}` under `--json`, carrying what was
      read; a run-as denial's hint is its remedy). No adb, a hang, or a device
      that is not there is adb's own error (exit 3 for the device), not "the
      command failed" at exit 1
    - A run-as denial (debug-build remedy) is only a refusal run-as itself
      printed: the FIRST line of stderr, at a non-zero exit, is the whole
      refusal the device prints for THIS package (`run-as: package not
      debuggable: <pkg>`, `package not an application`, `unknown package`; all
      recorded), and one bounded `run-as <pkg> id` (which names no file) does
      not run as the app. A device error during that probe is the device's
      (exit 3). A path the device refuses once run-as has run — `find: <dir>:
      Permission denied`, a missing file, a file whose name holds a newline
      and a `run-as: …` line of its own — is that path's failure, with an
      `adb shell run-as <pkg> ls -ld <path>` check as its remedy. A listing
      that fails at an entry — `find: '<dir>/<entry>': Permission denied`,
      when the directory has its r bit but not its x bit — says it failed at
      that entry (named escaped) and that a directory needs both bits, not
      that the directory itself was refused. A listing
      find refuses without naming a path (`find: bad arg …`, `find: bad
      -printf …`, as this toybox answers an option or escape it lacks) is
      find's failure, not the directory's: its remedy says the device's find
      cannot list this way. An older toybox is not measured. An older
      release's refusal wording is not recorded, and reads as the failure
    - Options: `--package`, `--ls [SUBPATH]`, `--cat FILE`, `--shared-prefs [NAME]`, `--databases [NAME]`, `--export DIR`, `--serial`, `--json`

30. **model_inspector.py** ⭐ NEW - Inspect Android persistence: Room annotations from source, Room exported-schema JSON, and live SQLite schema via `run-as` (Room == SQLite).
    - LIVE reads the database with `adb shell -T` (common/run_as.py), after the
      same adb 1.0.36 check as container.py. It used `adb exec-out`, which
      exits 0 on a missing file and hands back the error text as the bytes, so
      a database that was not there was reported as "not a valid SQLite
      database"; now the device's reason is the error, exit 1
    - Failures in every mode are the shared refusal (`{"success": false,
      "error": ..., "remedy": ...}` under `--json`; a run-as denial's hint is
      its remedy)
    - A run-as denial is decided in common/run_as.py by the one rule
      container.py uses: the first line of stderr must be the whole refusal
      the device prints for THIS package (`run-as: package not debuggable:
      <pkg>`, `package not an application`, `unknown package`; all recorded),
      and one bounded `run-as <pkg> id` must not run as the app. An unreadable
      `databases/` (`find: <dir>: Permission denied`), or a database whose name
      holds a newline or CR and a `run-as: …` line, is that path's failure,
      with an `ls -ld` check as its remedy, not "install a debug build"
    - LIVE lists `databases/` as container.py lists a directory
      (common/run_as.py: toybox find, NUL-terminated fields, read as bytes).
      It was `ls -1` in text mode, where `x\ry.db` was two databases, `x` and
      `y.db`, at exit 0; then `ls -la` split on newlines, where `fixture.db` +
      LF + a whole `ls -la` line listed `ghost.db`, a database the app does
      not have, at exit 0. Either name is now one name, refused and named in
      `rejected_entries`, and the listing exits 1 with what it read
      (`databases (0, 1 refused)`, never "No databases found")
    - Options: `--source DIR`, `--schema PATH`, `--show-versions`, `--raw NAME`, `--package`, `--db NAME`, `--serial`, `--json`, `--verbose`

31. **anr_watcher.py** ⭐ NEW - Record & summarise Android ANRs/jank from logcat (Choreographer skipped-frames + ActivityManager ANRs) with session-based progressive disclosure.
    - Session mode: `--start [--package PKG]` → id (the bare id as text, for
      `SID=$(...)`; `{"success": true, "session_id": ...}` under `--json`); `--stop ID [--abandon]` → token-tight summary; `--get-details ID [--cluster N]`; `--list-sessions`; `--clear-sessions`; `--diff A B`
    - **A session's lifecycle is serialised** (`common/anr_sessions.py`):
      `pending` → (the worker claims it) `running` ⇄ `deciding` → `stopped` |
      `crashed`, or `pending` → `cancelled` (a `--stop` or a `--start` that
      gave up on its worker), or `running`/`deciding` → `abandoned` (`--stop
      --abandon`). Every read-modify-write of a session's `meta.json` and
      `summary.json` is made under its lock (`flock` on `<session>/lock`; the
      wait for it is bounded, not the hold, and a wait past the bound is a
      refusal, exit 1, "run the same command again"), by one owner per
      transition, and only from the state it expects — otherwise it is
      refused, never applied — and goes to a temporary file of its own,
      renamed into place. The worker owns its claim, `deciding`, its
      counters and its end, which it publishes before it exits (a stop, a
      device lost, restarts exhausted, a stream that broke, an error nothing
      foresaw; a SIGTERM meanwhile only asks for a stop): `stopped`, or
      `crashed` with its failure, in one write — unless it cannot have the
      lock within 5 minutes or the session was deleted, when `--stop` finds
      it gone without having said, and `--abandon` is the way out. The CLI
      owns `cancelled`, `abandoned` and `summary.json`. `events.jsonl` is not
      locked: the worker appends to it as it reads. A worker whose claim is
      refused — its session cancelled, already claimed, or not its token —
      exits writing nothing; one whose session is abandoned or deleted stops
      at its next meta write (a counter write while its stream is quiet, at
      most every 5 s and skipped when the lock is busy, or its next
      transition), and may append events until then, which the session's
      answers ignore (below). The lock needs a filesystem that implements
      `flock` — a local one; a home on a network filesystem serialises only as
      far as its server and mount options honour it — and telling the worker
      from another process needs `ps`. A worker of an earlier version (a
      session with no `lifecycle` in its meta) takes none of these locks:
      while its recorded pid may be it, `--stop` trusts nothing the session
      says — it signals it and waits until it is gone, and refuses (exit 1,
      `--abandon` included, which cannot fence it) while it lives or cannot
      be identified; `--get-details` and `--diff` refuse such a session too
      (exit 1, naming the pid: they answered 0 from a snapshot the old worker
      could still change), and `--list-sessions` marks it `provisional`
      (`"provisional": true` and `provisional_reason` in JSON, a
      `provisional:` note in text). Refused rather than answered as
      provisional, because an exit 0 is what an agent acts on. Four
      reviews had closed one timing window at a time: a stop
      during the reader's end-of-stream grace skipped the decision (exit 0
      over a device loss); `--stop` answered at exit 0 while the worker was
      still to publish a failure, wrote back a meta it had read after the
      worker had persisted one, and took an unrecorded start time for
      another process; a worker claimed a session `--stop` had already
      answered for; `--start` took a failure published under `running` for
      a started session; a fifth found a `stopped` meta trusted while that
      version's worker ran on (exit 0, then the device loss it persisted),
      and an abandoned session's answer growing with every event appended
      after the abandonment
    - An unknown session, a session with no summary yet, or a `--cluster` past
      the end exits 1 and says what to run instead (the refusal object under
      `--json`) — it does not print the sentence and exit 0
    - `--older-than` takes `30s`/`5m`/`24h`/`7d` and a session id is
      `anr-YYYYMMDD-HHMMSS-XXXX`; either malformed is a usage error (exit 2)
      for every mode that takes one, `--stop` included, and `--clear-sessions`
      deletes nothing when it rejects one
    - Every reader — `--watch`, `--since` and a session's worker — reads
      logd's records (`logcat -B`, `common/logcat.py`), never text: each
      record carries the pid logd took from the writing socket, and an ANR
      report arrives whole, as one record
    - An ANR is counted once per incident as far as the log can show it.
      ActivityManager logs an ANR as ONE record of many lines; its `PID:` (the
      stalled app's own pid — the record's pid is system_server's) and
      `Reason:` (what timed out) arrive as the event's `app_pid` and `reason`,
      not as further ANRs, and the event is out as soon as the record is read.
      WindowManager's `ANR in Window{<hash> u0 <title>}` record is a report
      *to* ActivityManager: it is dropped when ActivityManager names the same
      WINDOW, by its hash, within 10 s (by log time, either side) — the
      Reason: of its report of an input ANR (recorded: 0.46 s after
      WindowManager's), or its `Skipping duplicate ANR` verdict (recorded:
      after a broadcast ANR, whose own report names no window). Otherwise it
      is an event of its own — a stream cut between the two, or one of
      ActivityManager's early returns — emitted once 10 s of system_server's
      log, or of quiet, have passed or the stream ends, so it can print after
      later events. One that ActivityManager's report of a window-less
      (broadcast) ANR within 10 s may answer is kept and marked
      `possible_duplicate` (a session counts them, `possible_duplicates`, and
      says so in a `Possible duplicate:` line): only the window's title could
      tie them, and the title used to settle it — so any title made a separate
      ANR vanish. Only system_server's records move that clock: a record's
      time is its writer's, and an app's record dated a minute ahead released
      a held report early, so one ANR counted twice. An ActivityManager record
      arriving more than 10 s after WindowManager's report would still be
      counted twice
    - **A window names no package.** What follows the hash in `Window{…}` is
      the window's TITLE, which the app sets (an activity's defaults to its
      component, which is what the recordings show) — a title reading
      `com.example.victim/…` made an ANR of that app, and one reading
      `Skipped 80 frames!` made the report jank. The title is display text:
      nothing in it is classified or settles anything, and an ANR WindowManager
      alone reported is an ANR of a window — `anr_package: null`, `identity:
      "window"`, the title kept as `window`, no symbol taken from it
    - **ActivityManager names a process, not a package.** `ANR in <name>` is
      the stalled process's name, and an app may name its process after any
      package (`android:process`; recorded: the fixture app's own stall,
      reported as `ANR in com.example.victim`). So an ANR is filed under the
      package (`"attribution": "verified"`) only when its `PID:` is shown to
      run as one of that package's uids: by system_server's `Start proc
      <pid>:<name>/<uid>` earlier in what was read, or — without one — by one
      bounded `ps -o PID,UID -p <pid>` now, which cannot prove who held the
      pid when the ANR was written if it has been reused since. Otherwise it
      is an ANR of a process: `anr_package: null`, `identity: "process"`, the
      name kept as `process_name`, `attribution` `unverified` or
      `contradicted`. An ANR whose process was already gone and not started
      while it was read stays unattributed
    - `--diff` compares neither kind (both are listed under `not_comparable`,
      as is an app's ANR whose only possible counterpart is one), and
      `--package` drops both — counted in a session's `unattributed_anrs` (an
      `Unattributed:` line in text), and said on stderr by `--watch` /
      `--since`. In text, each such line says it too (`[unverified: process
      <name> names no verified package]`, `[an ANR of a window: its title
      names no package]`, `[possible duplicate of the ANR at <time>]`), as do
      `--get-details --cluster N` (the stored `attribution`) and the
      `--since` summary ("ANR/jank events: 2, including 1 possible
      duplicate; 1 unverified")
    - An ANR report counts only as system_server wrote it: an `ANR in` record,
      and ActivityManager's `Skipping duplicate ANR` verdict, must carry
      exactly the right tag, open the record's message, and have been written
      by system_server — the record's pid, checked against `pidof
      system_server` (asked once, bounded at 3 s, in every mode). Any app can
      log under those tags, and one could forge an ANR for another package or
      make a real one vanish; in text, an app's TAG with a newline in it
      printed a whole line headed with system_server's pid (recorded:
      logcat_tag_injection), which as a record is the app's. When
      system_server's pid is unknown nothing is counted, and it is said: the
      session summary carries `"verification": "system_server pid unknown —
      ANR records could not be authenticated"` and `unverified_anr_lines`, its
      text forms an `Unverified:` line ("no *verified* ANR/jank"), and
      `--watch` / `--since` a `warning:` on stderr (and, in text, "No verified
      ANR/jank events")
    - Jank clusters by process, not by frame count: one screen dropping 31 and
      120 frames is one cluster, two apps janking alike are two. A session
      names the process behind a jank line and keys it by that name
      (`identity: "package"`), so an app restarted mid-way is still one site.
      The name comes ONLY from a `Start proc <pid>:<name>/<uid>` record earlier
      in the same stream, read by `common/log_lifecycle.py` — the same reading
      `log_monitor --app` uses: written by system_server (the record's pid,
      against `pidof system_server`, asked of the device once), under
      ActivityManager's tag exactly, the exact opening of the record's
      message, and a process that is exactly a package
      running under one of its uids (`cmd package list packages -U`, once per
      package) — any app can log under ActivityManager's tag, and one can name
      its own process after another package. A private process
      (`<package>:<name>`) is not named. Each lookup is bounded at 3 s; an
      unknown answer names nothing. A name ends at system_server's `Killing` /
      `has died` for the pid, and every stream (re)start forgets them all. The
      device is never asked who holds a pid now: `ps … ELAPSED` was judged
      against when a line was *read*, and a line read late was given a
      recycled pid's new owner. **So only a process started while the session
      runs is named**: an app already running when `--start` was called (warm
      in the background) is keyed by pid. Jank whose process could not be
      named is keyed by pid (`identity: "pid"`), and the summary lists those
      pids — `"unnamed_pids": {"count": N, "pids": [...]}` and one `Unnamed:`
      sentence in text; `--diff` lists it under `not_comparable` instead of
      calling it resolved or new, and so does a named cluster whose only
      possible counterpart in the other session is such a pid-keyed cluster of
      the same kind and text
    - `--stop` signals the session's worker only once the process at its
      recorded pid is shown to still be it: the random token `--start` put on
      its command line, and the start time `ps` gave when it claimed the pid.
      Anything else holding that pid is never signalled. A live pid that can
      be shown neither to be the worker nor not to be — `ps` could not say,
      the signal was refused, or the claim's start time was not recorded
      (`ps` did not answer then; that is no proof of a later process) — is
      not taken for a worker that ended: `--stop` refuses (exit 1, naming the
      pid and why), saves nothing and keeps the pid, for a later `--stop`
    - `--stop` answers with a summary only once the session has ended: its
      worker published `stopped` or `crashed`, or the session was cancelled
      or abandoned. It waits for the signalled worker to publish its end (up
      to 5.5 s: a lookup under way, the drain, a margin; and, for a worker
      deciding how its stream ended, until that decision's own bound, about
      31 s from when it began). How reading ended is fixed when it ends, so
      a stop that arrives during the reader's end-of-stream grace no longer
      skips deciding how the stream ended. A worker still at work at the end
      of the wait is refused (exit 1: "had not finished", run `--stop`
      again), and so is a worker gone without having said how its capture
      ended — killed, or dead before it could, so whether its stream failed
      is unknown (exit 1) — and nothing is saved either way: a summary saved
      then answered at exit 0 while the worker went on to persist a device
      loss. `--stop --abandon` is the way out when this version's worker
      never will say (gone, stuck, or a pid that cannot be identified) — not
      for an earlier version's worker while it may be alive (below): the
      session is
      marked `abandoned` — no meta write of the worker's is applied after
      that — and what was read is saved with `"incomplete": true`, the
      reason beginning "abandoned with --abandon", at exit 0. The worker may
      still append raw event lines after the fence (that file is not
      locked); the abandonment records how much of `events.jsonl` was
      there, to its last whole line, and every answer of the session —
      `--stop`, `--get-details`, `--cluster`, `--raw` — reads that much
      only, so a later `--stop` gives the same answer (it rebuilt the
      summary from the grown file: event count 1, then 2). It never claims
      completeness. A pending
      session (no worker has claimed it) is cancelled: `--stop` saves an
      empty summary marked incomplete and refuses (exit 1), and a worker
      that claims it later is refused. Two `--stop`s of a session that has
      ended give the same answer, and a repeated `--stop` keeps what the
      first said was missing. The summary is saved with `"incomplete": true`,
      and `incomplete_reason` saying why, whenever the capture may be
      missing events: the stream broke or ended by itself unrestarted, a
      record was cut by the stop (`cut_records`), the stream died and was
      restarted (`stream_restarts`: `-T 1` replays only the newest record
      logged while it was down), ANR reports could not be authenticated, or
      the session was abandoned or cancelled — so `--diff` concludes nothing
      from what such a capture lacks. A session whose stream failed is also
      refused (below): the summary is saved, and `--stop` exits 3 or 1.
      Every text form of an
      incomplete summary says so too (`--stop`, `--get-details`,
      `--get-details --cluster N`, whose JSON carries `incomplete`), and "no
      ANR/jank" there reads "… in the part captured". A token budget or
      `--terse` cuts clusters, never a disclosure: the one-line form keeps
      them as a bracketed clause (`[stream broken; 1 record cut by the stop;
      1 stream restart losing a gap; 2 ANR records unverified; 1 pid unnamed;
      1 jank/StrictMode record dropped unnamed; 1 record may predate the
      session, not counted]`)
    - `--diff` never says "no change" about what it could not see: with
      clusters set aside as not comparable, or a session saved incomplete, and
      nothing else changed, the verdict is `inconclusive: …` naming why; a
      verdict that did find a change carries the same caveats after it. A
      cluster one session has and the other, saved incomplete, lacks is never
      called resolved or new — the missing events may simply be unwritten — but
      listed under `unconfirmed` with the session it is in and the one it is
      `absent_from`; what a complete session lacks is still resolved or new.
      So too a session holding evidence it did not count — an `ambiguous_tail`,
      or `unattributed_anrs` — whose missing cluster may be right there: its
      absences are `unconfirmed`, and the verdict names what it did not count
      (a counted ANR against the same ANR held only as either read
      "improvement: 1 resolved"). And a `--package` session that dropped
      jank or StrictMode for want of a name (`unnamed_dropped`, below): a
      jank or StrictMode cluster absent from it is `unconfirmed` (the same
      jank, its process started before the capture, read as "improvement: 1
      resolved"), and the verdict says what it dropped, even when nothing is
      missing — the dropped record may be a cluster the other session lacks.
      Its ANR clusters still compare: a dropped jank record is no ANR. A
      `--package` summary saved before drops were counted has no
      `unnamed_dropped` (`--stop` of a session an earlier version's worker
      ran gives `"unnamed_dropped": null`, and a `Dropped: not recorded`
      line): what it dropped is unknown, so it is read the same way, the
      verdict saying "`<id>` does not record what --package dropped". A
      session that did not filter by package dropped nothing, recorded or
      not.
      A cluster both hold does not drift either when one side is incomplete —
      its maximum there is only a lower bound — but is listed under
      `not_compared` with `"reason": "not comparable: <id> is incomplete"`;
      so is a jank or StrictMode cluster against a side that dropped such
      records unnamed (`"<id> dropped N jank/StrictMode records of M pids it
      never named, which --package could not attribute"`)
    - A session keeps the fingerprint version it was captured with — `--stop`
      on an older session does not restamp it — and `--diff` refuses (exit 1,
      naming both versions) two sessions captured under different versions.
      This release's fingerprint is version 3 (an ANR of a window or of a
      process is filed under no package); version 2 filed a WindowManager-only
      ANR under its title's package, so a v2 session is not compared with a
      v3 one. Sessions recorded before versions were kept are version 1
    - `--diff --json`: each `drift` entry carries `from_zero`; when it is true
      (the cluster's max duration was 0 in the first session) `delta_pct` is
      `null` — never the non-JSON `Infinity` — and the entry counts as worsened
    - A live stream (`--start`, `--watch`) starts at the end of the device's
      buffer (`logcat -B -T 1`, which first prints the one most recent record);
      it used to replay the whole buffer as if it were happening now. **That
      one record may predate the read, so it is shown and never counted**:
      logcat refuses `-T 0` ("-T 0 invalid, setting to 1", recorded), and
      nothing in a record shows it was logged after `--start` — after `logcat
      -c` it is a live one. A session keeps what that record showed as
      `ambiguous_tail` (an `Ambiguous:` line in text), out of `event_count`,
      the clusters and `--diff`: counted, a stall from before the session read
      as its incident, and "resolved" in the next. So start the session before
      the scenario: a record logged just as `--start` returns may be that one,
      shown and not counted. `--watch` prints it with `"ambiguous_tail":
      true`. A session's
      restarted stream skips the records it replays that the session already
      read, whole, and reads one it never did (logged while the stream was
      down); nothing is compared by timestamp, so no clock change moves that
      boundary. `--since` reads its own window
    - A stream that is not logd's records to its end is never read as if it
      were. A record still arriving when our own stop — `--duration`, Ctrl-C,
      `--stop` — ends the read, on a stream still open or one that ended, is
      lost and said: a `warning:` on stderr for `--watch`, and in a session
      `cut_records` (a `Cut:` line in text). One that ends inside a record by
      itself is typed from adb's own stderr first — a device error adb wrote
      as it ended is exit 3, even when the device is back by the time it is
      asked (it was a framing refusal, exit 1, where `log_monitor` said 3) —
      then makes `--watch` and `--since` ask `adb get-state` again — before
      adb's other exit statuses are judged — and exit 3 when the device went,
      or refuse (exit 1) naming the framing error and how adb exited. Bytes
      that are not records at all are refused (exit 1) with no device
      question — after what adb has said, read once adb has had 0.5 s to
      finish (the session worker's grace too), which may name a device error
      (exit 3). adb's stderr is read from its start and its end (64 KiB
      each): an error adb wrote first and then chattered past was lost with a
      tail alone. An error only in the middle of more than 128 KiB of stderr
      is not read, so it is not typed: exit 1 where it is 3. A message shows
      adb's stderr shortened.
      A session's worker does not restart either kind of
      stream: it keeps each stream's stderr (it went to `/dev/null`) and reads
      what adb has said whether or not adb has exited, takes a device error
      adb wrote as the answer, and asks the device only without one; the
      session is marked crashed, and `--stop` saves what was read
      (`stream_failure` in the summary, a `Broken:` line in text, readable
      with `--get-details`, saying how adb ended it) and then refuses with
      that answer, raised again as the error it was — exit 3 when the device
      went, 1 for several devices and no `--serial` (naming the serials to
      pass) or anything else. A session's stream that ends by itself, whole,
      is asked about the same way: a device error — adb's words, or `adb
      get-state`'s — ends the session as that error (exit 3 at `--stop`);
      otherwise — the device there, or `adb get-state` unanswered — the
      stream is restarted (`ANDROID_EMU_ANR_MAX_RESTARTS`, default 3) until
      no restart is left, when the session ends as a stream failure `--stop`
      refuses (exit 1, saying how adb ended it). Either used to be restarted,
      then saved as an incomplete summary at exit 0. A stop that comes before
      a restart that was due saves the summary incomplete (what was logged
      after the stream ended is missing). Each stream's process is ended
      before the next starts
    - `--package PKG` keeps an event only when its package is exactly PKG, by
      the identity the reader established: the package ActivityManager's ANR
      report names (or its component's), or the name a session verified for
      a jank record's process. It used to match substrings of the message
      and tag, and the package's last segment alone, so the recorded
      Choreographer text "The application may…" passed `com.example.app` and
      `com.example.application` alike. A session keeps its own jank when it
      named the janking process — that is, when the process started while the
      session ran (above); a process already running is keyed by pid, and
      `--package` drops its jank (its pid is listed under `unnamed_pids`).
      What it drops so is counted, never dropped unsaid — it may be the
      package's: `"unnamed_dropped": {"records": N, "pids": [...]}` in the
      summary (records at or above the frame threshold), a `Dropped:` line in
      text, and `--diff` concludes nothing about jank from such a session
      (above). **The cost:** under `--package`, one such jank or StrictMode
      record from ANY process the session did not see start — an unrelated
      app already running, SystemUI, the launcher, the app itself warm —
      makes that session's jank/StrictMode conclusions in `--diff`
      inconclusive; its ANR conclusions stand. Start the session before the
      app, on a device with little else running, to keep them.
      **`--watch` and `--since` do not name processes**: a
      Choreographer or StrictMode record names no package, so under `--package`
      they drop all jank and StrictMode events, and say how many on stderr
      (`warning: --package dropped N jank/StrictMode records`; the `--since`
      text summary says so too)
    - Legacy: `--watch [--duration N]`, `--since 5m`. A logcat read that adb
      ends with a failure is a refusal, not "no ANRs": exit 3 when the device
      is unavailable (an unknown `--serial` is caught before streaming), 1
      otherwise, typed from adb's stderr as adb wrote it. A live stream ends
      only when it is stopped, so one that ends by itself — whatever adb's
      exit status, or none — makes `--watch` ask `adb get-state` again: exit 3
      when the device went, otherwise a refusal (exit 1), never success with
      nothing printed. A `--since` dump that hits its 60 s bound asks the same
      question first: adb waits rather than fail for a device that went
      ("- waiting for device -"), so the bound is how such a dump ends
    - `adb logcat` does not fail when its device is not there — it waits — so
      `--watch`, `--since` and `--start` ask `adb get-state` first (bounded,
      with or without `--serial`) and refuse at once, exit 3, when adb names
      no device in state `device` (several and no `--serial` is exit 1);
      `--start` creates no session then. `--start` succeeds only on a
      worker that has claimed the session and is `running` — which holds no
      failure, since a failure is published only with the session's end. A
      worker deciding how its stream ended is waited for; one that registers
      and ends before `--start` answers — its stream broke at once — is not
      "did not register" (exit 1, the race CI met): `--start` stops the
      session, so its summary is saved, and refuses with what ended it, as
      `--stop` would (exit 3 for a device that went). A worker that has not
      claimed the session within 3 s is given up on: the session is
      cancelled (exit 1), so a worker that starts late records nothing into
      it. A worker that meets an error nothing foresaw ends the session
      `crashed` with it as the stream failure — beside the stream's own
      failure when there was one, not in its place — where it left it
      `running` with nothing said. Sessions written by v0.8.0 main load
      unchanged (the same files and keys; this version only adds keys): a
      stopped one is answered as saved, a crashed one is refused as before,
      and a `running` one whose worker is gone is refused with `--abandon`
      named, since that version's worker never said how a session ended
    - **Compatibility boundary.** Upgrade quiescently: stop every ANR session
      with the old version before installing this one. A worker of v0.8.0
      main still recording is fenced only by waiting for it (above), and a
      `--start` of that version still in flight can claim a session this
      version cancelled — that version's claim checks nothing. Sessions this
      version writes are not for older binaries (no forward compatibility),
      and two versions writing one session is unsupported. While an earlier
      version's worker may be alive or cannot be identified, `--stop` and
      `--stop --abandon` both refuse; with `ps` unavailable and the pid
      present that lasts until `ps` works again or the process exits —
      otherwise the only recovery is outside the skill: delete the session's
      directory (or `--clear-sessions`) once you have established yourself
      that the worker has ended
    - `ANDROID_EMU_ANR_MAX_RESTARTS`, `_TOTAL_CAP_MB`, `_DEFAULT_TOP_N` and
      `_BUDGET_TOKENS` take `0` (no restarts, no cap, no top-N cap, no budget);
      it used to be raised to 1. A restart happens unless adb or `adb
      get-state` names a device error; with `0`, a stream that ends by itself
      ends the session at once
    - Options: `--watch`, `--since`, `--start`, `--stop`, `--get-details`,
      `--list-sessions`, `--clear-sessions`, `--diff`, `--package`, `--serial`,
      `--duration`, `--min-frames`, `--top`, `--all`, `--budget-tokens`,
      `--cluster`, `--raw`, `--older-than`, `--terse`, `--abandon`, `--json`

32. **sms.py** ⭐ NEW - Deliver an inbound SMS to an emulator and **prove it arrived**. Emulator-only.
    - `--send` reads the inbox back and reports `accepted` and `delivered` separately: the console's `OK` means the command was taken, not that a message exists. Delivery is asynchronous (~2s measured), so it polls.
    - `--otp` extracts a one-time code from the newest message and prints the message it came from, so the heuristic can be checked.
    - Options: `--send --to NUM --body TEXT [--no-verify]`, `--list [--limit N]`, `--otp`, `--serial`, `--json`, `--verbose`

33. **snapshot.py** ⭐ NEW - Save/load/delete emulator snapshots (`adb emu avd snapshot`). A ~2s state reset in place of a 60s reboot. Emulator-only.
    - A failed load is reported as a failure, which is the whole point: `adb emu` exits 0 even when it answers `KO`, so a restore that did not happen would otherwise be reported as success and the next test would run against unknown state.
    - Options: `--list`, `--save NAME`, `--load NAME`, `--delete NAME`, `--no-verify`, `--timeout`, `--serial`, `--json`, `--verbose`

34. **crash_triage.py** ⭐ NEW - Parse the dedicated crash buffer (`logcat -b crash`) into structured crashes, grouped by fault.
    - Groups repeats by package + exception + signature frame (a crash loop otherwise floods the output), and names the frame most useful for triage, **labelled with the basis for the choice** — including "no app frame; every frame is framework code" when that is the honest answer.
    - Exit status answers "did triage run", not "did anything crash" — branch on `crash_count` in `--json`, or pass `--fail-on-crash`.
    - Options: `--package PKG`, `--clear`, `--fail-on-crash`, `--serial`, `--json`, `--verbose`

35. **logs.py** ⭐ NEW - One entry point for reading logs; routes on the question being asked.
    - `logs.py tail` (main buffers) → `log_monitor.py`; `logs.py crashes` (`-b crash`) → `crash_triage.py`; `logs.py anr` (ANR/jank, incl. session mode) → `anr_watcher.py`.
    - Arguments are passed through verbatim, so each verb takes the full flag set of the script it delegates to, and those scripts remain callable unchanged.
    - Options: `<verb> [verb options]`, `--json` (routing table), `--help`

36. **avd.py** ⭐ NEW - One entry point for the emulator/AVD lifecycle; routes on the question being asked.
    - `avd.py list` → `device_list.py`; `pick` → `emulator_selector.py`; `create` → `emulator_create.py`; `start` → `emulator_boot.py`; `stop` → `emulator_shutdown.py`; `reset` (wipe data, keep the AVD) → `emulator_erase.py`; `delete` (remove the AVD) → `emulator_delete.py`.
    - `reset` and `delete` are separate verbs because they destroy different things, and a near-miss between them is asked about rather than guessed.
    - Arguments are passed through verbatim, so each verb takes the full flag set of the script it delegates to — including its own confirmation flag (`delete --yes`, `reset --force`) — and those scripts remain callable unchanged.
    - Options: `<verb> [verb options]`, `--json` (routing table), `--help`

> The build system lives in the `gradle/` subpackage (`builder`, `results`, `cache`, `config`, `reporter`), used by `build_and_test.py`. The ANR watcher's clustering/session machinery lives in `common/anr_pipeline.py` and `common/anr_sessions.py`.
>
> SDK binary resolution lives in `common/sdk_tools.py` (`get_emulator_path()`). Never exec
> `emulator` by bare name: the SDK root contains a *directory* named `emulator`, so a PATH
> holding `$ANDROID_HOME` instead of `$ANDROID_HOME/emulator` makes execve raise
> `PermissionError` — not the `FileNotFoundError` callers usually guard against.

## Android vs iOS Mapping

| iOS Tool | Android Equivalent | Status |
|----------|-------------------|--------|
| xcrun simctl | adb / avdmanager / emulator | ✓ Complete |
| IDB | adb shell uiautomator / input | ✓ Complete |
| iOS Simulator | Android Emulator | ✓ Complete |
| xcodebuild | Gradle wrapper | ✓ Complete |
| Accessibility tree | UI hierarchy dump | ✓ Complete |
| simctl privacy | pm grant/revoke | ✓ Complete |
| xcresult | Gradle test reports / JUnit XML | ⚠ Basic (being improved) |

## Script Categories

### 🚀 Essential (Use Daily)
- **app_launcher.py** - Launch/terminate apps
- **screen_mapper.py** - Understand current screen
- **navigator.py** - Interact with UI elements
- **gesture.py** / **keyboard.py** - User input

### 🔧 Development
- **build_and_test.py** - Build projects and run tests
- **log_monitor.py** - Debug with filtered logs
- **emulator_boot.py** / **emulator_shutdown.py** - Device management

### 🧪 Testing
- **accessibility_audit.py** - Check accessibility compliance
- **visual_diff.py** - Visual regression testing
- **test_recorder.py** - Document test execution
- **app_state_capture.py** - Debug test failures

### ⚙️ Advanced
- **privacy_manager.py** - Test permission flows
- **push_notification.py** - Post to the shade as the shell; verify what an app posted
- **status_bar.py** - Fine-grained control
- **emulator_create/delete/erase.py** - CI/CD provisioning
- **sms.py** - Inbound SMS, and OTP login flows end to end (emulator-only)
- **snapshot.py** - ~2s state reset between tests (emulator-only)
- **crash_triage.py** - Structured crashes from the dedicated crash buffer

## Typical Workflows

### Manual Testing Flow
```bash
# 1. Launch app
python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app

# 2. See what's on screen
python3 "$SKILL_DIR/scripts/screen_mapper.py"

# 3. Interact
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap
python3 "$SKILL_DIR/scripts/navigator.py" --find-type EditText --index 0 --enter-text "user@test.com"
python3 "$SKILL_DIR/scripts/keyboard.py" --button enter

# 4. Verify
python3 "$SKILL_DIR/scripts/screen_mapper.py"
```

### Automated Testing Flow

`test_recorder.py` is driven from the shell, one process per step — the session
is stored on disk, so nothing has to be held open between commands.

```bash
# 1. Start recording; the session id is printed, and later steps default to it
python3 "$SKILL_DIR/scripts/test_recorder.py" --start "Login Flow" --app-name MyApp

# 2. Execute test steps, capturing the screen after each interaction
python3 "$SKILL_DIR/scripts/test_recorder.py" --step "Launch app" --screen Splash
# ... interactions ...
python3 "$SKILL_DIR/scripts/test_recorder.py" --step "Verify logged in" \
    --screen Home --assert "Home screen shown"

# 3. Finish: writes report.md + manifest.json (add --failed to mark it failed)
python3 "$SKILL_DIR/scripts/test_recorder.py" --stop
```

### CI/CD Flow
```bash
# 1. Create fresh emulator
python3 "$SKILL_DIR/scripts/emulator_create.py" --device pixel_7 --api 34 --name test-device

# 2. Boot emulator
python3 "$SKILL_DIR/scripts/emulator_boot.py" --avd test-device --wait-ready

# 3. Build and test
python3 "$SKILL_DIR/scripts/build_and_test.py" --project . --test

# 4. Run UI tests
# ... your test scripts ...

# 5. Cleanup
python3 "$SKILL_DIR/scripts/emulator_shutdown.py" --serial emulator-5554
python3 "$SKILL_DIR/scripts/emulator_delete.py" --name test-device
```

### Debugging Flow
```bash
# 1. Capture complete state (prints where the snapshot went)
python3 "$SKILL_DIR/scripts/app_state_capture.py" --package com.myapp

# 2. Monitor logs in real-time
python3 "$SKILL_DIR/scripts/log_monitor.py" --app com.myapp --severity error,warning --follow

# 3. Check accessibility issues
python3 "$SKILL_DIR/scripts/accessibility_audit.py" --output audit-reports/ --verbose
```

## Requirements

- macOS, Linux, or Windows -- except `anr_watcher.py`'s live reads (`--watch`, and the session worker `--start` runs), which wait on adb's pipe with `select()`, and Python's `select()` takes only sockets on Windows: those two are Unix-only (macOS, Linux)
- Android SDK with platform-tools and emulator
- Python 3.12+
- ADB (Android Debug Bridge)
- Optional: Gradle for building
- Optional: Pillow for screenshot resizing

## Installation

### Environment Setup

```bash
# 1. Install Android SDK (via Android Studio or command line tools)
# Download from: https://developer.android.com/studio

# 2. Set environment variables
export ANDROID_HOME=$HOME/Library/Android/sdk  # macOS
export PATH=$PATH:$ANDROID_HOME/platform-tools
export PATH=$PATH:$ANDROID_HOME/emulator

# 3. Verify installation
adb version
emulator -version
```

### As Claude Code Skill

```bash
# As a plugin (preferred; updating is then two commands -- see README.md)
claude plugin marketplace add fluxxion82/android-emulator-skill
claude plugin install android-emulator-skill@fluxxion82

# From a clone: the skill is the INNER directory (this file's own), not the
# repository root, so clone the repo and link that directory into place.
git clone https://github.com/fluxxion82/android-emulator-skill ~/src/android-emulator-skill

# Personal installation
ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill ~/.claude/skills/android-emulator-skill

# Project installation
ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill .claude/skills/android-emulator-skill
```

## Documentation

- **SKILL.md** (this file) - the script reference; the accurate inventory
- **README.md** - short orientation for humans
- **examples/** - complete automation workflows (ships with the package)

In the source repository only:

- **CLAUDE.md** - architecture, conventions, and the recorded-fixture policy
- **references/** - deep dives on adb, test patterns, accessibility
  (not included in the released package)

## Platform limitations worth knowing

**The clipboard is not reachable from adb.** There is no way to *set* clipboard
text from the shell on any modern Android:

- `cmd clipboard` reports "No shell command implementation" on API 33 and 35 —
  the clipboard service exposes no shell command interface at all.
- `service call clipboard <setPrimaryClip>` cannot work either: the call takes a
  `ClipData` Parcelable, whose text is marshalled with `writeString8()` plus
  version-dependent fields, and `service` can only write `i32`/`s16`/binders.
- The emulator console (`adb emu`) has no clipboard command.

Reading and clearing *are* reachable — `service call clipboard 3/4/9` with the
right signature is accepted for the shell uid, which holds
`READ_CLIPBOARD_IN_BACKGROUND` — but writing is not, so a paste-flow test needs
an app-side hook or a helper IME.

On an emulator only, the gRPC control API does expose `setClipboard` /
`getClipboard` (see `$ANDROID_HOME/emulator/lib/emulator_controller.proto`).
That needs a gRPC client and the emulator's auth token, and is not wired up here.

`clipboard.py` was removed in v0.6.0 for this reason: its surviving code path
called `service call clipboard 1` with the pre-Android-10 signature and always
failed.

**Jetpack Compose screens look different in the dump.** Everything below was
measured from recorded dumps of a real Compose app, not assumed — three
plausible-sounding assumptions about Compose turned out to be wrong.

- **The host class is `androidx.compose.ui.platform.ComposeView`.**
  `AndroidComposeView` does not appear in the dump at all, so a detector
  looking for it matches nothing.
- **There are no resource-ids.** A default Compose screen exposes only
  `android:id/content`, which belongs to the AOSP FrameLayout. `--find-id` has
  nothing to work with; use `--find-text` or `screen_mapper`.
- **Interactive nodes carry no label of their own.** Every clickable and
  checkable node has `text=""` and `content-desc=""`. uiautomator dumps the
  *unmerged* semantics tree, so a clickable `Card` keeps its Texts as separate
  children rather than merging them into one label — there is no concatenation
  anywhere. `screen_mapper` recovers labels from a control's descendants
  (Button, Card, list row) and from row-adjacent siblings (Checkbox, Switch,
  icon), which is why it can name a control that the dump leaves anonymous.

**To make your own Compose app addressable**, opt into test tags as resource-ids
on the root of the tree:

```kotlin
Modifier.semantics { testTagsAsResourceId = true }   // once, at the root
Modifier.testTag("submit_button")                    // on each control
```

The tag then surfaces as a **bare** `resource-id` — `submit_button`, *not*
`com.example.app:id/submit_button`. Anything that splits a resource-id on
`":id/"` to recover a name drops every Compose tag on the screen.

## Key Design Principles

**Semantic Navigation**: Find elements by meaning (text, type, ID) not pixel coordinates. Survives UI changes.

**Token Efficiency**: Concise default output (3-5 lines), with `--verbose` for
human detail and `--json` for machine-readable output. The reduction versus
piping raw tool output is large but has not been measured; a budget table is
planned rather than an invented percentage.

**Accessibility-First**: Built on standard accessibility APIs for reliability and compatibility.

**Zero Configuration**: Works immediately with Android SDK installed. No complex setup required.

**Structured Data**: Scripts output JSON or formatted text, not raw logs. Easy to parse and integrate.

**Cross-Platform**: Works on macOS, Linux, and Windows (`anr_watcher.py`'s live reads are Unix-only: see Requirements).

**Real Devices**: Unlike iOS, works with both emulators and real devices.

## Android-Specific Features

**Real Device Support**: Works with both emulators and physical devices connected via USB/WiFi.

**Multiple Emulators**: Support for running multiple emulators simultaneously with batch operations.

**Flexible Architecture**: Works with both x86_64 and ARM emulator architectures.

**Gradle Integration**: Native integration with Android's Gradle build system.

**Advanced Logging**: Logcat filtering with regex, severity, and app-specific targeting.

**Permission Testing**: Programmatic grant/revoke of runtime permissions.

**Notification Testing**: Post a notification into the shade as the shell, and
read back what a package has actually posted. Delivering into an app's own FCM
handler is not reachable from adb — see `push_notification.py` above.

## Status & Roadmap

**v0.8.0 — the diagnostics stop lying.** v0.7.0 proved the agent loop closes
from the skill's own output. This release goes after the numbers an agent
reads: a verdict, a count or "nothing found" that the device did not actually
produce. Each fix below started as a strict-xfail spec written against a
recorded fixture, and was red before the fix.

- **One error boundary for every script** (`common/cli.py`). A refusal is one
  `{"success": false, "error", "remedy"}` object under `--json` (or
  `Error:`/`Remedy:` on stderr), and exits 1, or 3 when the device is
  unavailable. An AST guard enumerates all 32 scripts, and the exit-code sweep
  asserts the payload of 39 documented modes twice: once with every tool
  failing and once with none installed.
- **"Could not look" is no longer "nothing there."** `emulator_create`,
  `emulator_erase`, `device_list` and `android_health_check.sh` refuse a
  missing or failing SDK tool or AVD home instead of returning an empty list.
  `emulator_create` defaults to the host's ABI, never skips its image check,
  and reads both `sdkmanager` formats. The Java tool's pipe table had parsed to
  nothing, which kept the lane's AVD lifecycle test skipped on every CI run.
- **Build verdicts come from this run's tests.** `build_and_test` deletes the
  test task's old JUnit reports, reruns the task, and passes only on Gradle
  exit 0 with no `<failure>`/`<error>`. A run that wrote no results is refused,
  never reported as "0 passed". A Gradle older than 7.6 is refused by name.
- **Log windows belong to the app.** `log_monitor --app` and
  `app_state_capture` keep every process of the app and no other, including a
  crashed one that has since restarted. A pid counts only on evidence that
  system_server wrote and whose uid matches the package; both read logd's
  records (`-B`), so no tag or message an app writes can forge attribution,
  and a window that had to leave the app's lines out says so
  (`complete: false`). `--severity` validates its names, and counts are line
  counts. Snapshots no longer land in the caller's working directory.
- **One ANR is one event.** `anr_watcher` reads logd's records too, so the
  `Reason:`/`PID:` lines arrive inside their report, and a WindowManager-only
  ANR still counts, but only as system_server wrote it — the record's pid,
  which no tag can forge — and as an ANR of a window: the window's title is
  the app's text, so it names, classifies and settles nothing. An ANR is
  filed under a package only when its process is shown to run as that
  package's uid (ActivityManager names the process, which an app may name
  after another package). Jank is keyed by process, and a pid is named only
  from system_server's own `Start proc` record for it, seen in the same
  stream; `--package` matches that identity whole. A session's capture
  losses — a record cut by the stop, a stream restart's gap — mark it
  incomplete, and the one record `-T 1` replays when it starts is shown,
  never counted. `--diff` calls nothing resolved, new or stable on the
  strength of a side that may lack it — saved incomplete, or holding
  evidence it did not count (for jank and StrictMode, what `--package`
  dropped for want of a name, or did not record) — and says so in its
  verdict: "inconclusive" when nothing else changed, the change with its
  caveats otherwise. It never emits `Infinity`. The live readers no longer
  swallow adb's exit status: losses are disclosed; a `--watch` stream that
  ends by itself is a refusal, and a session's is restarted, while a restart
  is left, unless adb or `adb get-state` names a device error, then refused
  at `--stop` — adb's own words typed first (a device error is exit 3), in
  `--watch`, `--since` and the session worker alike. A session's lifecycle
  is serialised under a per-session lock (meta and summary; events are
  appended unlocked), each transition owned by the worker or the CLI and
  applied only from the state it expects: the worker publishes how it ended
  before it exits (unless it cannot have the lock in 5 minutes),
  `--stop` answers with a summary only once the session has ended
  (otherwise it refuses, exit 1, and `--stop --abandon` saves what was
  read, marked incomplete, its events frozen at the abandonment), and
  `--start` succeeds only on a worker that is recording. A worker of the
  previous version, which takes no lock, is waited for until it is gone
  before its session is answered.
- **App files are read honestly.** `adb exec-out run-as … cat` exits 0 with
  the error text as content (measured), so `container` and `model_inspector`
  now read through `adb shell -T` and refuse an adb client too old to honour
  it. Device file names are treated as untrusted input: directories are
  listed as NUL-terminated records (toybox find), so a name holding a newline
  cannot forge or hide an entry; unsafe, undecodable or CR/LF-carrying names
  are refused and reported, and exports stay inside their directory.
- **The required emulator lane can no longer pass without testing.** It fails
  on any skip, exits 3 with diagnostics when the device is lost, retries only
  that case once, and counts the retry in the step summary.

Tests: 1,653 → 2,619, and 0 xfailed. Fixture-policy debt is down to two
entries, and the boundary guard's debt list is empty.

The correction remains fixture-driven: `tests/fixtures/recorded/` holds verbatim
output captured from real devices on two API levels, parser tests consume it,
and a known defect, when one exists, is pinned with `xfail(strict=True)` so
fixing it forces the marker's removal. None is pinned in v0.8.0. See
`CLAUDE.md` in the source repository.

**Known gaps, for the next release:**

- `status_bar --time` must be given zero-padded — `09:41`, not `9:41`. The value
  reaches SystemUI's demo mode as `hhmm`; an unpadded one is ignored while the
  script still reports success.
- `accessibility_audit` has no contrast check. Contrast needs pixel sampling
  from a screenshot, which is not implemented.
- `anr_watcher --watch` / `--since` with `--package` drop every jank and
  StrictMode event: those lines name no package, and only session mode names
  the process behind a pid. Use `--start`/`--stop` to keep an app's jank.
- An ANR session names only a process started while it runs (system_server's
  `Start proc` is the only evidence it takes); an app already running when
  `--start` is called is keyed by pid, listed under `unnamed_pids`, and its
  jank is dropped under `--package`. Start the session before the app.
- `privacy_manager` acts on the default user only. It passes no `--user`, so a
  work profile or a secondary user is out of reach.
- **Failures** (`common/cli.py`; every script ends `cli.run(main)`). What is
  promised, and what is not yet:
  - A *typed* failure — a missing `adb` or SDK tool, an adb call that failed, a
    screen that would not dump, a timeout, or a failure a script raises with
    `cli.fail` — answers `{"success": false, "error": ..., "remedy": ...}` on
    one line of stdout under `--json`, `Error:` / `Remedy:` on stderr
    otherwise, and exits **1**. A refusal that found something on the way may
    carry it as extra keys after those three (a lookup's `search`, a capture's
    `partial`, the entries a listing could read); the three are always there.
  - A device that is **unavailable** (not found, offline, unauthorized) is the
    same object and exits **3** — whether `--serial` resolution noticed or adb
    did mid-run. Several devices and no `--serial` is 1.
  - A script's own exit statuses pass through unchanged, including the ones
    that mean a *finding*: `crash_triage --fail-on-crash` and
    `localization_audit --strict` exit 2 when they find something, and
    `accessibility_audit` exits 1 when a critical issue is present.
  - A streaming mode (`log_monitor --follow`, `anr_watcher --watch`) cannot
    take back the records it already printed: there the refusal object is the
    **last line** of stdout, and parses on its own. Every other mode keeps
    stdout to the one object under `--json` (`emulator_delete`'s confirmation
    prompt goes to stderr).
  - Six places catch a device error on purpose, so it does not reach the
    boundary: boot-completion polling (`emulator_boot`), the snapshot
    save/delete verify poll (`snapshot`, which keeps asking until its deadline
    and then reports the device as unavailable), the optional
    foreground-activity lookup (`device_utils.get_current_activity`), the
    emulator-console survey (`emu_console`), and the partial-capture policy of
    `test_recorder` steps and `app_state_capture` (what arrived is kept and
    handed over -- by `app_state_capture` in a refusal that is still exit 3
    when the device was unavailable).
  - Checked, not just promised: `tests/test_exit_code_sweep.py` runs every
    Quick Start command and the modes its table lists twice — with the Android
    tools present and failing, and with none on PATH at all — and
    asserts the refusal itself (one object, or `Error:` / `Remedy:`; an error
    naming the tool; the exact status), not just a non-zero exit.
  - Not in the schema yet: a usage error (exit 2 — argparse's prose on stderr,
    or a script's own `{"error": ...}` where it answers usage errors in JSON,
    as `navigator` and `emulator_delete` do), and batches that report per item
    — per operation in `status_bar` and `appearance`, per permission in
    `privacy_manager`'s grant/revoke, per AVD in the `emulator_*` `--all`
    modes — which exit 1 with their own summary when one item failed.
  - Device-error confirmation applies to app-file reads (`container` and
    `model_inspector` ask `run_argv` for it with `confirm_device_errors`): their
    stderr is a device command's, and a file name can print an `adb: …` line.
    logcat's readers do not ask: that stderr is adb's own, and a probe finding
    the device a moment later turned a real device error into exit 1. Other
    shell commands use `run_adb`; their arguments are agent-supplied and
    matching stderr is not yet confirmed (follow-up).
- Instrumented tests are not a feature. Nothing here drives
  `connectedAndroidTest`; the build and test scripts run unit tests.

Removed rather than repaired: `clipboard.py` — see "Platform limitations"
above; writing the clipboard is not reachable from adb on any modern Android,
so the script could not be made to do what it advertised.

## Contributing

New scripts should:
- Use class-based design for > 50 lines of logic
- Support --serial and auto-detection
- Support --json output
- Provide --help documentation
- Follow Black and Ruff standards
- Update this SKILL.md
- Test with real emulators before submission

## Differences from iOS

### Architecture
- **iOS**: Uses IDB for UI interaction, xcrun simctl for device management
- **Android**: Uses adb for everything, uiautomator for UI interaction

### Element Types
- **iOS**: Button, TextField, SecureTextField, StaticText, etc.
- **Android**: Button, EditText, TextView, ImageView, etc.

### Device Management
- **iOS**: Simulators only (macOS required)
- **Android**: Emulators + real devices (cross-platform)

### Build System
- **iOS**: Xcode project files (.xcodeproj)
- **Android**: Gradle build files (build.gradle)

---

**Status**: Under active modernization toward parity with [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill). See the "Status & Roadmap" section above.

Use these scripts directly or let Claude Code invoke them automatically when your request matches the skill description.
