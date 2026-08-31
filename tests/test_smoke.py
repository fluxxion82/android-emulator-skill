"""Device-free smoke tests: every CLI script must respond to --help cleanly.

This catches syntax errors, broken imports, and missing argparse wiring without
needing an emulator or device. Deeper behavioral tests live alongside each
feature (with adb/subprocess mocked).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"

SCRIPTS = sorted(p for p in SCRIPTS_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_help_runs(script: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    combined = (result.stdout + result.stderr).lower()
    assert (
        result.returncode == 0
    ), f"{script.name} --help exited {result.returncode}: {result.stderr}"
    assert "usage" in combined, f"{script.name} --help produced no usage text"
