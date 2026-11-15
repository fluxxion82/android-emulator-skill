# test_recorder.py

Document a test run, step by step

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- **Session-based, one process per step** — there is no `--test-name`,
  `--output` or `--inline`. `--start NAME` opens a session and prints its
  id; each later `--step "description"` captures the current screen into
  it; `--stop` writes `report.md` + `manifest.json`. `--step` and `--stop`
  default to the newest session, or target one with `--session ID`.
- Run its commands on one session one at a time: overlapping ones are
  unsupported — `--step` reads the session's meta and writes it back
  after its capture, so a `--stop` landing in between can be lost.
- Each step records a screenshot and the UI hierarchy, plus optional
  `--screen`, `--state` and `--assert` (`--assert-failed` marks it failed).
  A step the recorder could not read back is refused (exit 1) and not
  recorded: before anything is captured when it is the step's own text
  (it does not encode as UTF-8) or a step past number 999,999, and
  where it would be written when it is something the capture brought
  back. `--start` refuses a name, `--app-name` or `--serial` that does
  not encode as UTF-8, and every command a storage root whose path does
  not.
- Sessions live under `~/.android-emulator-skill`, not in an output
  directory you pass: retrieve with `--list` / `--get-details ID`, remove
  with `--clear [--older-than 24h]`.
- A damaged `steps.jsonl` (a write cut short, a corrupt line, a file
  emptied) is disclosed, not refused and never skipped silently: a
  recording cannot be re-run, so the steps that still read are kept and
  the rest are counted. `--stop`, `--get-details` and `--list` carry
  `incomplete`, `unreadable_lines` (non-blank lines of the file that gave
  no step: not a record the recorder writes — a field it does not
  write, a value of the wrong type, text that is not UTF-8, a step
  number outside 1–999,999 — or a repeat of a step number already
  read) and `lost_steps`
  (steps recorded but not readable, which also counts lines that are
  gone; a repeat is not a lost step) under `--json` — `--stop` and
  `--get-details` name the file as `steps_file` — and say `INCOMPLETE` /
  `Incomplete:` with the same counts in text; `report.md` and
  `manifest.json` say it too. `--stop` on an incomplete recording still
  writes both and keeps the verdict it was given, but exits 1: exit 0
  means passed *and* every recorded step is in the report. `--step` keeps
  recording after a loss, warns on stderr, and never reuses a lost step's
  number. In `--list`, `steps` is the count readable from the file.

## Example: a recorded run

The recorder is driven from the shell, one process per step. The session is
stored on disk, so nothing has to be held open between commands.

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
