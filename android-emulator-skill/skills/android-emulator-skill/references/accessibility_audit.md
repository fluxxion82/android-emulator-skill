# accessibility_audit.py

WCAG compliance checking

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Missing content descriptions: any **operable** control (by its uiautomator
  properties, not its class name) with no describing text in itself or its
  subtree is critical. The old class-name gate could not fire on a Compose
  screen at all, where controls are plain `android.view.View`.
- Touch target size verification
- EditText hint checking
- Image accessibility
- Categorize by severity (critical/warning/info)
- Save reports (JSON + Markdown)
