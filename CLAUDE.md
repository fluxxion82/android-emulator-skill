# CLAUDE.md — Developer Guide

Guidance for Claude Code and developers working in this repository.

## Overview

Android Emulator Skill provides scripts for Android app building, testing, and automation, wrapping
`adb`, `uiautomator`, and the Gradle wrapper with semantic, token-efficient interfaces designed for
AI agents. It is the Android counterpart of `ios-simulator-skill` (credited in the README).

## Project structure

```
android-emulator-skill/                      # repo root
├── .claude-plugin/marketplace.json          # marketplace manifest (owner: fluxxion82)
├── android-emulator-skill/                   # distributable plugin package
│   ├── .claude-plugin/plugin.json
│   └── skills/android-emulator-skill/
│       ├── SKILL.md                         # entry point / script reference + roadmap
│       └── scripts/                         # production scripts + common/ utilities
├── .github/workflows/                        # lint, test, release, validate-version, pages
├── tests/                                    # pytest suite (adb/subprocess mocked)
└── pyproject.toml / .pre-commit-config.yaml
```

## Architecture patterns

- **Class-based scripts** with a thin `main()` that parses args and prints results.
- **Serial resolution**: scripts accept optional `--serial` and auto-detect via
  `common.device_utils.resolve_device_identifier`. (Note: `get_device_serial` does **not** exist —
  use `resolve_device_identifier`.)
- **Output modes**: concise default (3–5 lines) → `--verbose` → `--json`.
- **Batch operations** where sensible (`--all`).

## Shared utilities (`scripts/common/`)

- **device_utils.py** — `build_adb_command()` (never `shell=True`), device detection, and
  `get_ui_hierarchy()`.
  - ⚠️ **Hierarchy contract**: `get_ui_hierarchy()` returns nodes shaped as
    `{"tag": str, "attributes": {<raw XML string attrs>}, "children": [...]}`. All UI fields
    (`class`, `text`, `bounds`, `clickable`, `content-desc`, `resource-id`, …) live **under
    `node["attributes"]`** as **strings**. Consumers must read from `attributes` and parse types
    themselves (e.g. `bounds` is `"[l,t][r,b]"`; booleans are `"true"`/`"false"`). Reading fields
    directly off the node (`node.get("class")`) silently returns nothing — this was a real bug.
- **screenshot_utils.py** — capture/resize; **inline mode returns the image under the
  `base64_data` key** (not `base64`).
- **cache_utils.py** — progressive disclosure cache for large outputs.

## Testing: recorded fixtures are mandatory

**The defining bug class in this repo is code and tests both written against
*imagined* tool output.** When the imagination is wrong they are wrong in the
same direction, so the suite stays green while the script does nothing. Three
advertised capabilities shipped inert this way, past 470 passing tests.

Therefore:

1. **Parser tests read `tests/fixtures/recorded/`; they never inline tool output
   as a string literal.** Use the `recorded` pytest fixture. If ground truth is
   missing, record it — do not hand-write a plausible substitute.

   ```bash
   python tests/record_fixtures.py --list
   python tests/record_fixtures.py --only logcat_threadtime
   python tests/record_fixtures.py --profile pixel4xl-api33 --serial <SERIAL>
   ```

   Fixtures are namespaced per device under `tests/fixtures/recorded/<profile>/`,
   so the same command captured on a different API level is a new file rather
   than a silent overwrite — diffing two profiles is how you spot a format that
   shifted between releases. Use the `any_profile` pytest fixture for invariants
   that must hold everywhere ("this subcommand does not exist"), and `recorded`
   for the complete primary profile.

   **Never commit a fixture containing private data.** The recorder aliases
   non-AOSP package names to `com.example.appN`, but that does not catch
   free-text content. When recording from a personal device, capture only
   structural output (`wm size`, `cmd ... help`) — not logcat, notification
   lists, or screen dumps.

2. **Before writing any code that parses a tool's output, run the tool and look
   at it.** Several commands in this codebase do not exist at all
   (`cmd statusbar battery-level`, `dumpsys activity anr`,
   `cmd notification list channels`). Some fail silently and exit 0.

3. **The rule is enforced, not just written here.** `tests/test_fixture_policy.py`
   is a ratchet: it fails any *new* test that feeds a long string literal into a
   parser, either directly or through a mocked `subprocess`'s `stdout=`. The
   violations that exist today are frozen in `KNOWN_VIOLATIONS` as debt, and a
   stale entry fails too, so paying one off cannot silently re-open the door.

   The two worth paying off first are `test_container.py` and
   `test_model_inspector.py`, which test parsers against hand-written `run-as
   ls -la` and `sqlite3 .schema` output. Neither command has any recorded
   fixture, so there is currently nothing to compare against.

4. **Known defects are pinned with `@pytest.mark.xfail(strict=True)`** and a
   defect ID, in `tests/test_recorded_fixtures.py`. `strict=True` means fixing
   the defect turns the test red until the marker is removed — delete the marker
   in the same commit as the fix.

5. **A live test must never drive someone's phone.** `adb devices` lists a
   handset before `emulator-5554`, so taking the first serial pointed the whole
   lane at whatever was plugged in. `live_device` prefers an emulator; anything
   with side effects that must not reach a real device (SMS, snapshots, taps,
   key presses) takes `emulator_only_device`, which skips rather than falling
   back.

6. **Tests needing a device are marked `@pytest.mark.emulator`** and deselected
   by default. Run them with `pytest -m emulator`. They assert *semantic floors*
   ("did the agent get a usable answer"), not command shapes.

Run the suite with:

```bash
uvx --python 3.12 --with pytest --with pillow --with pyyaml pytest tests/
```

`pyyaml` is required, not optional: the guards in `tests/test_packaging_contract.py`
parse `.github/workflows/` to assert a release cannot be cut without running the
tests. They deliberately do not `importorskip`, because a guard that skips when a
dependency is missing protects nothing.

## Quality standards

1. Python 3.12+ with modern type hints (`str | None`, `StrEnum`).
2. Black (100 cols) + Ruff (strict, 0 errors) — see `pyproject.toml`.
3. `pytest tests/` green; unit tests mock adb/subprocess (no device needed).
4. Never `shell=True`; pass explicit `check=` to `subprocess.run`.
5. `--help` and `--json` on every script; update `SKILL.md` when adding scripts.

## Token efficiency

Default output is intentionally minimal (a few lines). Use `--verbose` for human detail and
`--json` for machine-readable output in CI. Screenshots are resized to control token cost.

## Design philosophy

Semantic (find by meaning, not pixels) · Progressive (minimal by default, detail on demand) ·
Accessible (built on the uiautomator hierarchy) · Structured (JSON / formatted text, not raw logs) ·
Reusable (shared patterns across scripts).
