# crash_triage.py

Parse the dedicated crash buffer (`logcat -b crash`) into structured crashes, grouped by fault.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Groups repeats by package + exception + signature frame (a crash loop otherwise floods the output), and names the frame most useful for triage, **labelled with the basis for the choice** — including "no app frame; every frame is framework code" when that is the honest answer.
- Exit status answers "did triage run", not "did anything crash" — branch on `crash_count` in `--json`, or pass `--fail-on-crash`.
