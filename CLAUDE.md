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
