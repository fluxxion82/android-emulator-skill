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
├── .github/workflows/                       # lint, test, emulator, release, validate-version, pages
├── tests/                                   # pytest suite (adb/subprocess mocked, no device)
├── pyproject.toml                           # Black / Ruff / pytest config (Python 3.12+)
├── dev-constraints.txt                      # exact pins for the dev toolchain; CI and local read it
└── .pre-commit-config.yaml
```

## Prerequisites

- macOS or Linux (Windows is untested; see SKILL.md, Known gaps)
- Android SDK with `platform-tools`, `emulator`, `avdmanager` on `PATH`; `ANDROID_HOME` set
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended for environment + tooling)

## Development environment (uv)

```bash
# From the repo root
uv venv --python 3.12
uv pip install -c dev-constraints.txt black ruff pytest pillow pyyaml pre-commit
source .venv/bin/activate
pre-commit install && pre-commit install --hook-type commit-msg
```

Versions are stated once, in [dev-constraints.txt](dev-constraints.txt) (exact pins: pip, pytest,
Pillow, PyYAML, Black, Ruff, pre-commit). Every install step in `.github/workflows/` is written
one way, `pip install -c dev-constraints.txt <pinned names>`, so CI and the commands here get the
same versions of those packages (a dependency the file does not name still floats);
`tests/test_packaging_contract.py` fails a workflow that names an installer any other way (it
checks how the workflows are written, not what a job does at run time). To move a
version, change it there (and the matching `rev:` in `.pre-commit-config.yaml` for Black or Ruff),
run the suite, and commit both. Without a venv:

```bash
uvx --python 3.12 -c dev-constraints.txt --with pillow --with pyyaml pytest tests/
```

## Linting and tests

```bash
SCRIPTS=android-emulator-skill/skills/android-emulator-skill/scripts

# Format + lint (must be clean)
black --check "$SCRIPTS" tests/
ruff check "$SCRIPTS" tests/

# Unit tests — fully mocked, no device/emulator required
pytest tests/
```

## CI workflows (`.github/workflows/`)

| Workflow | Trigger | Purpose |
|---|---|---|
| `lint.yml` | PR / push to `main` | Black + Ruff over `scripts/` and `tests/` |
| `test.yml` | PR / push to `main` | `pytest tests/` |
| `emulator.yml` | PR, nightly, manual, called by `release.yml` | `pytest -m emulator` against a booted emulator |
| `release.yml` | release published | Lint + unit tests, the emulator lane, and the four manifests == tag; only then zip the `android-emulator-skill/` package and attach it |
| `validate-version.yml` | release published, manual | `pyproject.toml`, SKILL.md frontmatter, `plugin.json` and `marketplace.json` versions == tag |
| `pages.yml` | push to `main` touching `site/**` or the workflow itself, manual | Deploy the docs site to GitHub Pages |

## Release process

```bash
# 1. Bump the version in pyproject.toml, SKILL.md (frontmatter and `## Scripts (vX.Y.Z)`),
#    android-emulator-skill/.claude-plugin/plugin.json, .claude-plugin/marketplace.json
#    and the hero tag in site/index.html; tests/test_packaging_contract.py fails until all agree
# 2. Commit, tag, push
git commit -am "chore: bump version to X.Y.Z"
git tag vX.Y.Z && git push origin main --tags
# 3. Create the GitHub release on tag vX.Y.Z — release.yml and validate-version.yml strip only
#    a leading `v`, so a release on the plugin tag fails its version gate. The plugin tag
#    (`cd android-emulator-skill && claude plugin tag`, giving android-emulator-skill--vX.Y.Z)
#    goes on the same commit, as v0.8.0's did; release.yml packages the plugin zip
```

## Code style

- Python 3.12+ with modern type hints (`str | None`, `StrEnum`), enforced by Black (100 cols) and Ruff (strict).
- Class-based design for any script with > 50 lines of logic.
- Support `--serial` with auto-detection, `--json` output, and `--help` on every script.
- Never use `shell=True`; build adb commands via `common.device_utils.build_adb_command`.
- Token-efficient default output (3–5 lines); richer detail behind `--verbose` / `--json`.

## Testing strategy

- **Unit tests** mock `adb`/`subprocess` so they run in CI without a device. Parsers read output
  recorded from real devices in `tests/fixtures/recorded/<profile>/` (`tests/record_fixtures.py`
  records it), never inline tool output; CLAUDE.md states the rule and `tests/test_fixture_policy.py`
  enforces it.
- **Emulator tests** (`@pytest.mark.emulator`, deselected by default) run with `pytest -m emulator`.
- **Manual smoke** (per change touching device behavior): boot an emulator, run
  `bash .../scripts/android_health_check.sh`, then exercise a launch → map → navigate flow.
