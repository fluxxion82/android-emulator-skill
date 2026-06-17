# Android Emulator Skill — Development Guide

This is the **development repository** for the Android Emulator Skill, a Claude Code skill providing
scripts for Android emulator/device testing and automation. It is the Android counterpart of the
upstream `ios-simulator-skill` (see [README.md](README.md) for attribution).

## Repository layout

```
android-emulator-skill/                      # repo root
├── .claude-plugin/
│   └── marketplace.json                     # marketplace manifest (owner: fluxxion82)
├── android-emulator-skill/                  # distributable plugin package
│   ├── .claude-plugin/
│   │   └── plugin.json                      # plugin manifest
│   └── skills/
│       └── android-emulator-skill/
│           ├── SKILL.md                     # entry point / script reference
│           └── scripts/                     # production scripts + common/ utilities
├── .github/workflows/                       # lint, test, release, validate-version, pages
├── tests/                                   # pytest suite (adb/subprocess mocked, no device)
├── pyproject.toml                           # Black / Ruff / pytest config (Python 3.12+)
└── .pre-commit-config.yaml
```

## Prerequisites

- macOS, Linux, or Windows
- Android SDK with `platform-tools`, `emulator`, `avdmanager` on `PATH`; `ANDROID_HOME` set
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended for environment + tooling)

## Development environment (uv)

```bash
# From the repo root
uv venv --python 3.12
uv pip install black==26.5.1 ruff==0.15.14 pytest pillow pre-commit
source .venv/bin/activate
pre-commit install
```

## Linting and tests

```bash
SCRIPTS=android-emulator-skill/skills/android-emulator-skill/scripts

# Format + lint (must be clean)
black --check "$SCRIPTS"
ruff check "$SCRIPTS"

# Unit tests — fully mocked, no device/emulator required
pytest tests/
```

## CI workflows (`.github/workflows/`)

| Workflow | Trigger | Purpose |
|---|---|---|
| `lint.yml` | PR / push (scripts) | Black + Ruff |
| `test.yml` | PR / push (scripts, tests) | `pytest tests/` |
| `release.yml` | release published | Validate structure, zip the `android-emulator-skill/` package, attach to release |
| `validate-version.yml` | release published | `pyproject.toml` version == tag; SKILL.md mentions the version |
| `pages.yml` | push to `site/**` | Deploy the docs site to GitHub Pages |

## Release process

```bash
# 1. Bump version in pyproject.toml AND SKILL.md frontmatter (keep them in sync)
# 2. Commit, tag, push
git commit -am "chore: bump version to X.Y.Z"
git tag vX.Y.Z && git push origin main --tags
# 3. Create a GitHub release for the tag — release.yml packages the plugin zip
```

## Code style

- Python 3.12+ with modern type hints (`str | None`, `StrEnum`), enforced by Black (100 cols) and Ruff (strict).
- Class-based design for any script with > 50 lines of logic.
- Support `--serial` with auto-detection, `--json` output, and `--help` on every script.
- Never use `shell=True`; build adb commands via `common.device_utils.build_adb_command`.
- Token-efficient default output (3–5 lines); richer detail behind `--verbose` / `--json`.

## Testing strategy

- **Unit tests** mock `adb`/`subprocess` so they run in CI without a device — see `tests/conftest.py`
  and `tests/fixtures/` (e.g. `SAMPLE_UI_HIERARCHY`).
- **Manual smoke** (per change touching device behavior): boot an emulator, run
  `bash .../scripts/android_health_check.sh`, then exercise a launch → map → navigate flow.
