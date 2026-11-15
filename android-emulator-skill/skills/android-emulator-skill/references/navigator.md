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
  message alone on stdout), exit 1 — and a lookup's refusal carries its
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
