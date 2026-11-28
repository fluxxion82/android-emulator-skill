# navigator.py

Find and interact with elements semantically

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

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
  message alone on stdout), exit 1 (3 when the device is unavailable) — and a lookup's refusal carries its
  `search` detail in the object. A usage error is the same object at exit
  2 (see **Failures** in SKILL.md).
- **`--scroll-to-find`** searches below the fold. Without it a lookup sees only the visible
  screen, and `Not found` is indistinguishable from "the item is two rows down". The default
  path now says which it was: `(searched 1 screen; this screen scrolls -- retry with
  --scroll-to-find)`. Scrolling is opt-in on purpose -- a swipe is not a read, so a lookup
  that scrolled silently would let `--find-text "Save is visible"` pass for a button three
  screens away.
- `--find-exact TEXT` (or `--find-text TEXT --exact`) matches the whole text, not fuzzily;
  `--index N` picks the Nth match (0-based); `--tap-at x,y` taps coordinates, the fallback
  when no element can be named

## Waiting: `--wait-for` and `--tap --expect`

`--wait-for TEXT` reads the screen until TEXT is on it. `--tap --expect TEXT` (or
`--tap-at x,y --expect TEXT`) sends one tap and then does the same. Both print the
screen the wait ended on -- its counts and its control names, the names
`screen_mapper` prints -- so the next command can be written from the answer.

```bash
python3 "$SKILL_DIR/scripts/navigator.py" --wait-for "Order confirmed"
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Place order" --tap --expect "Order placed"
```

- **What matches.** Any text or content description on the screen, a control's
  recovered caption, or a resource id given whole -- so any name the screen report
  prints, and any line of plain text. A passive label matches, and so does the
  text of a disabled control: a wait observes, it does not have to be able to tap
  what it finds.
  Without `--exact`, case is ignored and part of a text is enough; a resource id
  is always matched whole and as written. The answer names the node that
  matched. `--exact` wants the whole text, case included (and covers `--find-text` in the same
  command).
- **What it does to the device.** It reads. No scroll, no second tap, no setting
  changed. `--expect` does not go with `--scroll-to-find` or `--enter-text`.
- **One deadline.** `--timeout S` (default 30, from 3 to 600) is the whole
  command from the first thing it does to the device -- the read that finds the
  target, or the tap for `--tap-at`: finding the target, the tap, the settle
  pause, every dump and every pause between dumps. A dump of a settled screen took
  about 2 seconds on a warm emulator and up to 17 on one that had just booted, so
  a short timeout may not fit one. Each dump is given what
  is left and is stopped on the device when that is up, so nothing is left running
  there (the stop itself took up to a tenth of a second in the recordings). Three
  things can run past it. The tap command itself is never given less than 5
  seconds, because a tap cut off half way is a tap nobody can say happened; if adb
  stalls there, the answer says so. A device that does not answer adb at all is
  given up on by adb's own timeout, 5 to 6 seconds after the dump's limit, with
  adb's error. And when adb says the device is not there, or that there are
  several, it is asked once what is attached so the error can say, with 10 seconds
  of its own.
- **What it needs.** The device's own `timeout` command stops each dump (toybox;
  present on the API 33 and API 35 emulators this was measured on). What a device
  without one answers was not recorded.
- **Three answers.**
  - `Found:` -- exit 0. A capture that could be read held the text.
  - `Not observed:` -- exit 1. Time ran out and the last capture that finished was
    a screen without the text. The error says how many screens were read, when the
    last one was, and what it read: its controls and its first few texts.
  - `Could not read the screen:` -- exit 1. The last capture that finished was not
    a screen, or none finished. This is not an answer about the text. When a dump
    answered, the error says what the last such dump reported (in this skill's
    words for the answers it knows); a reading from earlier in the wait, if there
    was one, is given and called stale.
  After a tap, both failures start with the tap's own line (`Tapped: ...`, or
  `Tapped at (x, y)` for `--tap-at`) and the remedy says the tap was sent: look
  before tapping again. A target that is not on the screen is `Not found` as
  without `--expect`, with nothing tapped and no wait.
- **A screen that never goes idle cannot be read.** uiautomator waits for a second
  with no accessibility event before it dumps, and gives up after about eleven.
  The recorded case is an `android.widget.TextView` whose text changes every
  100 ms: it never gives uiautomator that second, and setting the animation
  scales to 0 does not help, because nothing is animating. A wait on such a screen
  ends `Could not read the screen` at any timeout. Content that keeps changing is
  not by itself such a screen, and neither is a spinner or an animation: the three
  Compose screens recorded (a text changing every 100 ms, an endless animation, an
  indeterminate spinner) dump normally.
- A device that goes away is exit 3, a bad combination of flags exit 2, as
  everywhere. Under `--json` a found text is one object (`waited_for`,
  `elapsed_seconds`, `captures`, `element`, `screen`, and `tapped` / `tapped_at`
  after a tap); a wait that ended without the text is the shared refusal with a
  `wait` object (`outcome` is `not_observed` or `unreadable`) and, after a tap,
  `tapped` / `tapped_at`. Before the tap: a target that is not there or is only
  a label fails as without `--expect`; a screen that could not be read to find it
  is `Could not read the screen to find the target, so nothing was tapped`, with
  the same `wait` object. The tap command failing is `Tap failed`, and one that
  stalled says it is not known whether the tap landed. After the tap, if adb fails
  while the screen is being read (the device gone: exit 3), the error still starts
  with the tap's line and carries `tapped` / `tapped_at`. `--verbose` prints one
  line on stderr per capture the wait finished: a match, no match, a screen (the
  read that finds the target), not a screen, or stopped at the limit.

## Example: launch, look, act, look again

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
