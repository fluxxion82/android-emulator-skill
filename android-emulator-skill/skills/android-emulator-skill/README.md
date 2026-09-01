# android-emulator-skill

Build, test, and drive Android apps through `adb`, `uiautomator`, the emulator
tooling, and the Gradle wrapper — with concise, structured output intended for
an AI agent rather than a terminal.

**`SKILL.md` is the reference.** It lists every script, its flags, and the
environment variables. This file exists only to orient a human browsing the
package; it deliberately does not duplicate the script table, because the
duplicate is what went stale last time.

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
- Pillow, for screenshot resizing and image diffs
- A booted emulator or a connected device with USB debugging enabled

`scripts/android_health_check.sh` verifies the environment and lists what it
finds.

## Status

Under active repair. The upstream repository tracks known defects and the work
in progress; several scripts were built against assumed tool output and are
being corrected against recorded output from real devices.

Treat `SKILL.md`'s script list as the accurate inventory. If a flag documented
there does not behave as described, that is a bug worth reporting rather than a
usage error.

## Links

- Repository: https://github.com/fluxxion82/android-emulator-skill
- Credit: this is the Android counterpart of
  [`ios-simulator-skill`](https://github.com/conorluddy/ios-simulator-skill) by
  conorluddy, from which the architecture is adapted.
