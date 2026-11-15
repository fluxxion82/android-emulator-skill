# android-emulator-skill

Build, test, and drive Android apps through `adb`, `uiautomator`, the emulator
tooling, and the Gradle wrapper — with concise, structured output intended for
an AI agent rather than a terminal.

**`SKILL.md` is what an agent loads.** It holds the loop an agent follows, an
index of every script with its flags, and the failure contract, in 32 KiB or
less. Each script's detail — its modes, what it prints, how it fails — is in
`references/`, one file per script, opened when needed. This file exists only
to orient a human browsing the package; it deliberately does not duplicate
the script index, because the duplicate is what went stale last time.

## Running a script

Scripts live in `scripts/`, next to `SKILL.md`. Invoke them by path — relative
paths like `python scripts/foo.py` only work when the current directory happens
to be this one, which is not the case when the skill is installed as a plugin
and you are working in your own project:

```bash
python3 /path/to/skills/android-emulator-skill/scripts/screen_mapper.py --json
```

Every script supports `--help`. Most support `--json` and `--serial`; `SKILL.md`
notes the exceptions.

## Requirements

- Python 3.12+
- Android SDK platform-tools on `PATH` (`adb`), plus `emulator` for AVD work
- `cmdline-tools` on `PATH` for AVD management (`avdmanager`, `sdkmanager`).
  Android Studio does not install it by default, and the legacy
  `tools/bin/avdmanager` cannot stand in: it dies with
  `NoClassDefFoundError: javax/xml/bind/annotation/XmlSchema` on Java 11+
- Java 21, for Gradle builds
- Optional: Pillow, for screenshot resizing and image diffs
- A booted emulator or a connected device with USB debugging enabled

Put `$ANDROID_HOME/emulator` on `PATH`, **not** `$ANDROID_HOME`. The SDK root
contains a *directory* called `emulator`; the binary is
`$ANDROID_HOME/emulator/emulator`. With the root on `PATH` the launch fails with
`PermissionError: [Errno 13] Permission denied` rather than "not found", which
sends you looking in the wrong place.

`scripts/android_health_check.sh` verifies the environment and lists what it
finds.

## What it does not do

[references/known-gaps.md](references/known-gaps.md) lists what the skill does
not do yet, and the limits it states rather than closes.

Treat `SKILL.md`'s script index as the accurate inventory: tests hold every
flag it names to what the script accepts. If a flag documented there does not
behave as described, that is a bug worth reporting rather than a usage error.

## Links

- Repository: https://github.com/fluxxion82/android-emulator-skill
- Credit: this is the Android counterpart of
  [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill) by
  conorluddy, from which the architecture is adapted.
