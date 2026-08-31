"""Pytest bootstrap.

`pythonpath` is configured in pyproject.toml so tests can `import common.*`
without sys.path mangling. Fixtures here mock adb/subprocess so unit tests run
without a connected device or emulator.

Two conventions this file enforces:

1. **Parser tests read recorded tool output, never inline literals.** Use the
   ``recorded`` fixture. See ``tests/record_fixtures.py`` for why: the repo's
   defining bug class is code and tests both written against *imagined* adb and
   gradle output, which keeps the suite green while the script does nothing.

2. **Tests needing a real device are marked ``@pytest.mark.emulator``** and are
   deselected by default (see ``addopts`` in pyproject.toml). Run them with
   ``pytest -m emulator``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"
RECORDED_DIR = Path(__file__).resolve().parent / "fixtures" / "recorded"


@pytest.fixture
def scripts_dir() -> Path:
    """Absolute path to the skill's scripts directory."""
    return SCRIPTS_DIR


class RecordedFixtures:
    """Accessor for verbatim tool output under ``tests/fixtures/recorded/``.

    Deliberately fails loudly on a missing fixture, naming the exact command
    needed to record it. A test that silently skips because ground truth is
    absent is the same failure mode as a test that asserts imagined output.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def text(self, name: str) -> str:
        """Return one recorded fixture verbatim.

        Args:
            name: Fixture name with or without extension, e.g. ``logcat_threadtime``.

        Raises:
            pytest.fail: if the fixture has not been recorded.
        """
        candidates = (
            [self._root / name]
            if "." in name
            else [self._root / f"{name}.txt", self._root / f"{name}.xml"]
        )
        for path in candidates:
            if path.exists():
                return path.read_text(encoding="utf-8")
        pytest.fail(
            f"Recorded fixture '{name}' is missing.\n"
            f"Boot an emulator and run:\n"
            f"    python tests/record_fixtures.py --only {name.split('.', maxsplit=1)[0]}\n"
            f"Do not substitute a hand-written literal — that is the bug class "
            f"this directory exists to prevent."
        )

    def lines(self, name: str) -> list[str]:
        """Return a recorded fixture split into lines, trailing blank removed."""
        return self.text(name).splitlines()

    @property
    def manifest(self) -> dict:
        """Provenance for the recorded set: device, API level, per-fixture command."""
        path = self._root / "MANIFEST.json"
        if not path.exists():
            pytest.fail(
                "tests/fixtures/recorded/MANIFEST.json is missing. "
                "Run `python tests/record_fixtures.py` against a booted device."
            )
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def recorded() -> RecordedFixtures:
    """Verbatim tool output recorded from a real device.

    Example:
        def test_parser_handles_real_logcat(recorded):
            parsed = [parse_logcat_line(ln) for ln in recorded.lines("logcat_threadtime")]
            assert any(p for p in parsed), "parser matched nothing against real output"
    """
    return RecordedFixtures(RECORDED_DIR)


@pytest.fixture(scope="session")
def adb() -> str:
    """Path to a working `adb`, skipping the test if none is on PATH."""
    path = shutil.which("adb")
    if not path:
        pytest.skip("adb not on PATH")
    return path


@pytest.fixture(scope="session")
def live_device(adb: str) -> str:
    """Serial of a booted device, or skip.

    Only for tests marked ``@pytest.mark.emulator``. Every call here is bounded:
    an unbounded adb call wedges the connection for every later test, which is
    the same defect class the skill itself ships.
    """
    try:
        result = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=20, check=False
        )
    except subprocess.TimeoutExpired:
        pytest.skip("adb devices timed out")

    serials = [
        line.split()[0]
        for line in result.stdout.splitlines()[1:]
        if line.strip() and line.split()[-1] == "device"
    ]
    if not serials:
        pytest.skip("no booted device; start one and re-run with -m emulator")
    return serials[0]
