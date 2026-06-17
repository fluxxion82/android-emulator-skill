"""Pytest bootstrap.

`pythonpath` is configured in pyproject.toml so tests can `import common.*`
without sys.path mangling. Fixtures here mock adb/subprocess so unit tests run
without a connected device or emulator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"


@pytest.fixture
def scripts_dir() -> Path:
    """Absolute path to the skill's scripts directory."""
    return SCRIPTS_DIR
