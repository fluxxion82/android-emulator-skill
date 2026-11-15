# keyboard.py

Text input and hardware buttons

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

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
