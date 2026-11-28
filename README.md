# Android Emulator Skill

Scripts that let a coding agent build, test and drive Android apps. They put `adb`,
`uiautomator`, the emulator tools and the Gradle wrapper behind commands that find things
on the screen by name and answer in a few lines. It installs as a plugin in Claude Code and
in Codex.

> **Attribution.** This project is the Android counterpart of, and was originally seeded from,
> [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill) by Conor Luddy. It is
> maintained independently under its own name and owner; that upstream project is gratefully
> acknowledged here.

## Status

Before 1.0. What can be said for it is what is checked:

- The suite's parser tests read recorded output: from real emulators (API 35 and API 33)
  and from the host tools (Gradle, the agents' CLIs). A ratchet,
  `tests/test_fixture_policy.py`, fails a new test that hands a parser a long sample
  written by hand, in the forms it recognises: a direct parser input, or one of the mocked
  output fields it knows. It is a ratchet and not a proof: a few tests still build small
  inputs themselves.
- Every pull request to `main` runs that suite, a lane of tests on a real API 35 emulator,
  and an install of the plugin with each supported agent's CLI. A release is not packaged
  unless all three passed.
- What the skill does not do yet is written down in
  [`known-gaps.md`](android-emulator-skill/skills/android-emulator-skill/references/known-gaps.md).

`SKILL.md` is the file an agent loads, and tests hold every flag it names to what the
script accepts. If a flag documented there does not behave as described, that is a bug
worth reporting.

## Supported agents

One row per agent whose own CLI installs this plugin into an empty home and lists the skill
afterwards, in CI, on every pull request to `main`. The check is one command from a checkout.

| Agent | Checked with | The check |
|---|---|---|
| Claude Code | `@anthropic-ai/claude-code@2.1.322` | `python tests/verify_install.py --agent claude` |
| Codex | `@openai/codex@0.170.0` | `python tests/verify_install.py --agent codex` |

The check installs from a local checkout and refuses a CLI at any other version. Other
agents may well be able to load the skill directory; none is listed until it has a check.

## Install

Both agents take the same two steps: add this repository as a marketplace, then install
the plugin from it.

### Claude Code

```bash
claude plugin marketplace add fluxxion82/android-emulator-skill
claude plugin install android-emulator-skill@fluxxion82
```

### Codex

```bash
codex plugin marketplace add fluxxion82/android-emulator-skill
codex plugin add android-emulator-skill@fluxxion82
```

`fluxxion82` after the `@` is the marketplace's name, the `name` field of its manifest, not a
GitHub username; they coincide here. The install check runs these commands with a local path
where they name the repository; installing from GitHub is not part of it. Codex's help names
`owner/repo` as a source, and Claude Code's says "a URL, path, or GitHub repo".

### Updating Claude Code's copy is two commands

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

## What an agent does with it

`SKILL.md` gives the agent a loop: look at the screen, act on a name it printed, check what
followed. `$SKILL_DIR` is the installed skill's directory, the one holding `SKILL.md`; from
a checkout it is `android-emulator-skill/skills/android-emulator-skill`.

```bash
python3 "$SKILL_DIR/scripts/app_launcher.py" --launch com.example.app
python3 "$SKILL_DIR/scripts/screen_mapper.py"
python3 "$SKILL_DIR/scripts/navigator.py" --find-text "Login" --tap --expect "Welcome"
```

The last command taps, waits for the text the tap should bring, and prints the screen that
followed. Every script has `--help`; `SKILL.md` lists each one with its flags.

## What it costs

The numbers below are printed by a script, and a test fails if this file and the script
disagree. The screen figures are measured from recorded screens: the size of the recorded
`uiautomator` dump, and the size of what each command prints for that same screen. The
skill's sizes are those of the files in the checkout; the v0.9.0 one was measured once,
from that release's commit, and is kept.
Tokens are bytes divided by four, an estimate; no tokenizer is run.

### What one screen costs to read

Bytes, with tokens in brackets. Tokens are bytes / 4, rounded: an estimate, no tokenizer is run.

| Screen | raw dump | `screen_mapper` | `screen_mapper --verbose` | `navigator --list` | `navigator --find-text` |
|---|---:|---:|---:|---:|---:|
| walk 1 catalog | 8,586 (~2,147) | 250 (~63) | 604 (~151) | 282 (~71) | 46 (~12) |
| walk 2 product | 5,583 (~1,396) | 182 (~46) | 445 (~111) | 120 (~30) | 47 (~12) |
| walk 3 cart | 7,915 (~1,979) | 216 (~54) | 513 (~128) | 200 (~50) | 44 (~11) |
| walk 4 checkout | 7,214 (~1,804) | 264 (~66) | 548 (~137) | 244 (~61) | 47 (~12) |
| walk 5 confirmation | 5,583 (~1,396) | 184 (~46) | 448 (~112) | 122 (~31) | 49 (~12) |
| walk 6 settings | 7,878 (~1,970) | 220 (~55) | 489 (~122) | 204 (~51) | 51 (~13) |
| compose single screen | 10,895 (~2,724) | 334 (~84) | 791 (~198) | 350 (~88) | 41 (~10) |
| settings home | 23,466 (~5,867) | 577 (~144) | 1,327 (~332) | 799 (~200) | 87 (~22) |
| dialer keypad | 41,034 (~10,259) | 430 (~108) | 905 (~226) | 907 (~227) | 36 (~9) |
| **walk, 6 screens** | **42,759 (~10,690)** | **1,316 (~329)** | **3,047 (~762)** | **1,172 (~293)** | **284 (~71)** |
| share of the raw dump | 100.0% | 3.1% | 7.1% | 2.7% | 0.7% |

What this compares, and what it does not:

- The walk is 6 screens of one small fixture app; the other rows are single recorded screens, replayed. It is the size of one answer per screen, not the cost of a task: taps, scrolling, retries and second looks are not counted.
- The columns do not carry the same information. The raw dump is the whole hierarchy `uiautomator` exposes. `screen_mapper` and `navigator --list` are summaries that leave things out and cut long lists short. `navigator --find-text` answers one question, for a text the agent already knew to ask for, and found it every time here.
- The fixture app sets `testTagsAsResourceId`, so its Compose controls are reported by short ids. The `compose single screen` row is a screen without it: its controls have no id and no text of their own, and are reported by a caption recovered from the texts around them.
- Over `adb exec-out` a dump arrives with 33 bytes of status line after the document (10,927 bytes in all for a 10,894-byte document).

### Loading the skill

| What the agent loads | bytes | tokens (estimate) |
|---|---:|---:|
| `SKILL.md` | 32,593 | ~8,148 |
| `SKILL.md` plus its largest reference file | 77,726 | ~19,432 |
| `SKILL.md` at v0.9.0, with no reference files beside it | 156,541 | ~39,135 |

To reproduce it from a checkout:

```bash
uvx --python 3.12 -c dev-constraints.txt --with pillow python tests/measure_costs.py
```

That prints these tables and one more, of screenshot sizes at each `--size` preset.

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
- [`AGENTS.md`](AGENTS.md) — architecture and conventions for contributors and coding agents (`CLAUDE.md` imports it).

## License

MIT — see [`LICENSE.md`](LICENSE.md).
