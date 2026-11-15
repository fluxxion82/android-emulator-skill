# status_bar.py

Status bar control

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- Set battery level and charging state
- Set WiFi/mobile signal strength, or hide either icon: `--wifi hide`,
  `--mobile hide` (bare `--wifi` / `--mobile` shows it). `--wifi-level N` and
  `--mobile-level N` are signal bars, 0-4 (anything else is refused); `--mobile-type TYPE`
  is the network label SystemUI shows, passed through as given (e.g. `lte`, `4g`, `5g`,
  `hspa`; it is not checked against a list)
- Set time display (for consistent screenshots): `--time` takes `H:MM` or
  `HH:MM` (`9:41` reaches SystemUI as `0941`); anything else is refused
- Demo mode support
- One item per operation asked for (see **Failures** in SKILL.md): `{"success",
  "items"}`, each with `operation`, `message`, `success`, `error`,
  `remedy`. **Breaking in v0.9.0:** it was `{"results": [...]}`
- All of these drive SystemUI **demo mode**, which changes what the status
  bar *draws*. It does not change what the app reads from the system
  (`BatteryManager` still reports the real level).
