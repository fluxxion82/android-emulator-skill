# screen_mapper.py

Analyze current screen and list interactive elements

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

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
