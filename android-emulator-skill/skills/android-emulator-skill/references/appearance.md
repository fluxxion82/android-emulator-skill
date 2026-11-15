# appearance.py

Control dark/light mode (`cmd uimode night`) and font scale (`settings put system font_scale`); best-effort locale.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- One item per change asked for (see **Failures** in SKILL.md): `{"success",
  "items"}`, each with `operation`, `message`, `success`, `error`,
  `remedy`. **Breaking in v0.9.0:** it was `{"results": [...]}`
