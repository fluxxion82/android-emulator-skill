# Android Emulator Skill

Build, test, and automate Android apps from Claude Code using accessibility-driven navigation and
structured, token-efficient output instead of pixel coordinates.

> **Attribution.** This project is the Android counterpart of, and was originally seeded from,
> [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill) by Conor Luddy. It is
> maintained independently under its own name and owner; that upstream project is gratefully
> acknowledged here.

## Status

**Under active repair.** Feature parity with the upstream iOS skill is done; the work now is
correcting scripts that were written against assumed `adb`/Gradle output rather than recorded
output. The repository uses the Claude Code plugin layout, Python 3.12+, strict Black/Ruff linting,
and a mocked pytest suite. See `android-emulator-skill/skills/android-emulator-skill/SKILL.md` for
the authoritative script list; if a flag documented there does not behave as described, that is a
bug worth reporting.

## Features

- **Semantic navigation** — find elements by text, type, or resource ID, not pixel coordinates.
- **Token-efficient** — concise 3–5 line default output; `--verbose` and `--json` for detail.
- **Emulators and real devices** — `--serial` with auto-detection.
- **Lifecycle management** — create / boot / shutdown / erase / delete AVDs.
- **Gradle build & test, logcat monitoring, accessibility audit, visual diff, test recording,
  state capture, permissions, status bar, notifications.**

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

## Prerequisites

- **`platform-tools` on `PATH`** — every script goes through `adb`.
- **`cmdline-tools` on `PATH`** for AVD management (`avdmanager`, `sdkmanager`). Not installed by
  default with Android Studio, and the legacy `tools/bin/avdmanager` is not a substitute: it dies
  with `NoClassDefFoundError: javax/xml/bind/annotation/XmlSchema` on Java 11+, so on a modern JDK
  it cannot run at all. Install "Android SDK Command-line Tools (latest)", or
  `sdkmanager 'cmdline-tools;latest'`.
- **Java 21** — what current Android Gradle builds expect.
- **Python 3.12+**.
- Optional: **Pillow**, for screenshot resizing and image diffs.

```bash
export ANDROID_HOME=$HOME/Library/Android/sdk        # macOS example
export PATH="$PATH:$ANDROID_HOME/platform-tools"
export PATH="$PATH:$ANDROID_HOME/emulator"           # the emulator DIRECTORY, not the SDK root
export PATH="$PATH:$ANDROID_HOME/cmdline-tools/latest/bin"
```

> **Put `$ANDROID_HOME/emulator` on `PATH`, not `$ANDROID_HOME`.** The SDK root contains a
> *directory* named `emulator`; the binary is `$ANDROID_HOME/emulator/emulator`. With the root on
> `PATH`, launching `emulator` hits the directory and fails with
> `PermissionError: [Errno 13] Permission denied` — not the "not found" you would go looking for.

## Installation (Claude Code plugin)

This skill is packaged as a Claude Code plugin (`.claude-plugin/marketplace.json` +
`android-emulator-skill/.claude-plugin/plugin.json`).

```bash
claude plugin marketplace add fluxxion82/android-emulator-skill
claude plugin install android-emulator-skill@fluxxion82
```

`@fluxxion82` is the `name` field in `marketplace.json`, not a GitHub username — they coincide here.

### Updating is two commands

```bash
claude plugin marketplace update fluxxion82      # git-pull the marketplace repo
claude plugin update android-emulator-skill      # apply it (restart Claude Code afterwards)
```

The second alone does nothing: it moves the installed plugin to whatever the first fetched. Running
only it looks exactly like "no update available".

### Without the plugin system

The skill Claude Code loads is the inner directory
`android-emulator-skill/skills/android-emulator-skill` (the one holding
`SKILL.md` and `scripts/`), **not** the repository root. Clone the repo
somewhere of your own, then link that directory into place:

```bash
git clone https://github.com/fluxxion82/android-emulator-skill ~/src/android-emulator-skill

# Personal: available in every project
ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill ~/.claude/skills/android-emulator-skill

# Project-local: commit it, or add it to .gitignore
ln -s ~/src/android-emulator-skill/android-emulator-skill/skills/android-emulator-skill .claude/skills/android-emulator-skill
```

Use `cp -R` in place of `ln -s` if you would rather not have a symlink; a
`git pull` then needs the copy repeating.

## Coming from ios-simulator-skill

| iOS Tool | Android Equivalent | Status |
|----------|-------------------|--------|
| xcrun simctl | adb / avdmanager / emulator | ✓ Complete |
| IDB | adb shell uiautomator / input | ✓ Complete |
| iOS Simulator | Android Emulator | ✓ Complete |
| xcodebuild | Gradle wrapper | ✓ Complete |
| Accessibility tree | UI hierarchy dump | ✓ Complete |
| simctl privacy | pm grant/revoke | ✓ Complete |
| xcresult | Gradle test reports / JUnit XML | ✓ Unit (`--test`) and instrumented (`--instrumented`, one device) verdicts from JUnit XML; no attachments, screenshots or coverage |

**Architecture**
- **iOS**: Uses IDB for UI interaction, xcrun simctl for device management
- **Android**: Uses adb for everything, uiautomator for UI interaction

**Element Types**
- **iOS**: Button, TextField, SecureTextField, StaticText, etc.
- **Android**: Button, EditText, TextView, ImageView, etc.

**Device Management**
- **iOS**: Simulators only (macOS required)
- **Android**: Emulators + real devices (cross-platform)

**Build System**
- **iOS**: Xcode project files (.xcodeproj)
- **Android**: Gradle build files (build.gradle)

## Documentation

- `android-emulator-skill/skills/android-emulator-skill/SKILL.md` — what an agent loads: the loop, the script index with every flag, and the failure contract.
- `android-emulator-skill/skills/android-emulator-skill/references/` — one file per script (modes, output, failures), and `known-gaps.md`, what the skill does not do yet.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.
- [`docs/`](docs/) — topic notes on adb, test patterns, accessibility and troubleshooting.
- [`DEV.md`](DEV.md) — development environment, linting, tests, CI, release process.
- [`CLAUDE.md`](CLAUDE.md) — architecture and conventions for contributors and Claude Code.

## License

MIT — see [`LICENSE.md`](LICENSE.md).
