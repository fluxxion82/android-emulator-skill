# Android Emulator Skill

Build, test, and automate Android apps from Claude Code using accessibility-driven navigation and
structured, token-efficient output instead of pixel coordinates.

> **Attribution.** This project is the Android counterpart of, and was originally seeded from,
> [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill) by Conor Luddy. It is
> maintained independently under its own name and owner; that upstream project is gratefully
> acknowledged here.

## Status

**Actively modernizing toward parity with the upstream iOS skill.** The repository now uses the
Claude Code plugin layout, Python 3.12+, strict Black/Ruff linting, and a mocked pytest suite. See
`android-emulator-skill/skills/android-emulator-skill/SKILL.md` for the authoritative, up-to-date
script list and the roadmap of features still being ported.

## Features

- **Semantic navigation** — find elements by text, type, or resource ID, not pixel coordinates.
- **Token-efficient** — concise 3–5 line default output; `--verbose` and `--json` for detail.
- **Emulators and real devices** — `--serial` with auto-detection.
- **Lifecycle management** — create / boot / shutdown / erase / delete AVDs.
- **Gradle build & test, logcat monitoring, accessibility audit, visual diff, test recording,
  state capture, permissions, clipboard, status bar, push notifications.**

## Quick start

```bash
SCRIPTS=android-emulator-skill/skills/android-emulator-skill/scripts

# Launch an app, inspect the screen, interact semantically
python "$SCRIPTS/app_launcher.py" --launch com.example.app
python "$SCRIPTS/screen_mapper.py"
python "$SCRIPTS/navigator.py" --find-text "Login" --tap
python "$SCRIPTS/navigator.py" --find-type EditText --enter-text "user@example.com"
```

Every script supports `--help` and `--json`.

## Requirements

- Android SDK with `platform-tools`, `emulator`, and `avdmanager` on `PATH`; `ANDROID_HOME` set.
- Python 3.12+.
- Optional: Pillow (for screenshot resizing / visual diff).

```bash
export ANDROID_HOME=$HOME/Library/Android/sdk        # macOS example
export PATH="$PATH:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator"
```

## Installation (Claude Code plugin)

This skill is packaged as a Claude Code plugin (`.claude-plugin/marketplace.json` +
`android-emulator-skill/.claude-plugin/plugin.json`). Plugin install instructions will be finalized
once feature parity work lands:

```bash
claude plugin marketplace add fluxxion82/android-emulator-skill
claude plugin install android-emulator-skill@fluxxion82
```

## Documentation

- `android-emulator-skill/skills/android-emulator-skill/SKILL.md` — script reference + roadmap.
- [`DEV.md`](DEV.md) — development environment, linting, tests, CI, release process.
- [`CLAUDE.md`](CLAUDE.md) — architecture and conventions for contributors and Claude Code.

## License

MIT — see [`LICENSE.md`](LICENSE.md).
