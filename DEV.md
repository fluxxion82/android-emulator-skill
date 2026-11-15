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
│           ├── SKILL.md                     # what an agent loads: loop, script index, failures
│           ├── references/                  # one file per script, and known-gaps.md (ships)
│           └── scripts/                     # production scripts + common/ utilities
├── .github/workflows/                       # lint, test, emulator, release, validate-version, pages
├── tests/                                   # pytest suite (adb/subprocess mocked, no device)
├── pyproject.toml                           # Black / Ruff / pytest config (Python 3.12+)
├── dev-constraints.txt                      # exact pins for the dev toolchain; CI and local read it
└── .pre-commit-config.yaml
```

## Where documentation goes

An installed agent loads `SKILL.md` whole, on every task, so it is held to 32,768 bytes by a test.
Three surfaces, and what each one holds:

| Surface | Holds | Ships |
|---|---|---|
| `SKILL.md` | Rooted invocation and device selection, Quick Start, the loop, the script index, Failures, platform limitations, requirements and install | yes |
| `references/<name>.md`, one per script, and `references/known-gaps.md` | What each mode does, what it prints, how it fails, examples | yes |
| `CHANGELOG.md`, `docs/`, this file, `CLAUDE.md` | Release history, topic notes, how to work on the repository | no |

A new script gets both of these in the same change:

1. **An index entry in `SKILL.md`**, numbered under its `#### Category (N scripts)` heading (bump
   that count and `### Implemented (N scripts)`): the bold file name and one line saying when to
   reach for it; `- Run:` with one command that works as written; a note only for a side effect or
   a prerequisite; the complete `- Options:` bullet; `- Detail:` linking its reference file.
2. **`references/<name>.md`**, named after the script and titled with its file name
   (`# navigator.py`), holding everything else.

**Every flag goes in the `- Options:` bullet in `SKILL.md`.** An agent picks its flag from the file
it has loaded, not from `--help`, so a flag documented only in a reference file counts as
undocumented and fails `test_every_flag_a_script_accepts_is_documented_or_excused`.

Packaged Markdown is read by guards that refuse what they cannot place, code blocks included: a
link is `[text](relative/path)` or an http(s) URL and nothing else, `]:` appears nowhere (so no
`items[0]:` in an example), there are no tabs (show `adb devices` output with spaces), no fence
sits on a list marker's line or in a blockquote, and an example names its script as
`"$SKILL_DIR/scripts/navigator.py"`. The messages say which rule a line broke.

## Shared modules (`scripts/common/`)

`device_utils.py` (adb command building and device detection), `screenshot_utils.py` (capture and
resize) and `cache_utils.py` (the progressive-disclosure cache), and: **cli.py** (the error
boundary every script ends in, `cli.run(main)`; its contract, and what it does not cover yet, is
SKILL.md's **Failures** section), **batch.py** (the one shape of a
batch that reports per item -- `items`, each with `success` / `error` /
`remedy` -- and when a device error stops one), **adb_exec.py** (the one bounded
entry point for every adb call, with typed errors that name a remedy),
**hierarchy.py** (the one way to capture the UI hierarchy — no temp files,
so concurrent runs cannot read each other's screen), **emu_console.py** (`adb emu`, which exits 0 even when it
fails), **logcat.py** (the one place that builds an `adb logcat` argv and
parses a duration, shared by all four log readers), **dump_read.py** (the
live read made of dumps on API 33, whose logcat holds `-B` records back
while it streams: the level asked once, the opening, each poll's read after
the cursor, and every dump's one bounded ending, shared by `log_monitor`
and `anr_watcher`'s live readers), **sdk_tools.py** (resolves
SDK binaries; `emulator` by bare name hits the `<sdk>/emulator` *directory* on an
SDK-root PATH and raises `PermissionError`), **run_as.py** (reads a
debuggable app's files with `adb shell -T run-as`, never `exec-out`, which
answers a missing file with its error text as the bytes; checks the adb
client honours `-T` first; decides what a run-as refusal is, and lists a
directory as toybox find's NUL-terminated records, for both inspectors),
**env_config.py**, **anr_pipeline.py**,
**anr_sessions.py**, **log_lifecycle.py** (system_server's process
sentences — `Start proc` / `Killing` / `has died` — authenticated one way for
`log_monitor --app` and the ANR session worker alike).

SDK binary resolution lives in `common/sdk_tools.py` (`get_emulator_path()`). Never exec
`emulator` by bare name: the SDK root contains a *directory* named `emulator`, so a PATH
holding `$ANDROID_HOME` instead of `$ANDROID_HOME/emulator` makes execve raise
`PermissionError` — not the `FileNotFoundError` callers usually guard against.

## Prerequisites

- macOS or Linux (Windows is untested; see the skill's `references/known-gaps.md`)
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
- Test against a real emulator before submitting a change to device behaviour.
- Never use `shell=True`; build adb commands via `common.device_utils.build_adb_command`.
- Token-efficient default output (3–5 lines); richer detail behind `--verbose` / `--json`.

## Testing strategy

- **Unit tests** mock `adb`/`subprocess` so they run in CI without a device. Parsers read output
  recorded from real devices in `tests/fixtures/recorded/<profile>/` (`tests/record_fixtures.py`
  records it), never inline tool output; CLAUDE.md states the rule and `tests/test_fixture_policy.py`
  enforces it.
- **Emulator tests** (`@pytest.mark.emulator`, deselected by default) run with `pytest -m emulator`.
- **Measured costs.** `python tests/measure_costs.py` prints what one screen costs to read as a raw
  dump and through `screen_mapper` / `navigator`, from the recordings; nothing in it is typed. The
  screen rows are committed as `tests/fixtures/measurements/expected.json` and compared by
  `tests/test_measure_costs.py`, so a script whose output grows fails a test. When that is
  intended, `python tests/measure_costs.py --write` and commit the diff. SKILL.md's size is
  measured live; `skill_md_v0.9.0.json` is the frozen size it had before v0.10.0.
- **Manual smoke** (per change touching device behavior): boot an emulator, run
  `bash .../scripts/android_health_check.sh`, then exercise a launch → map → navigate flow.
