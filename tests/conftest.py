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
RECORDED_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded"

# The profile carrying the complete fixture set. Other profiles hold whatever
# subset was safe or practical to capture on that device — a personal phone, for
# instance, should never have its logcat or screen contents committed.
PRIMARY_PROFILE = "emulator-api35"

RECORDED_DIR = RECORDED_ROOT / PRIMARY_PROFILE


def _available_profiles() -> list[str]:
    """Device profiles that have been recorded, primary first.

    ``recorded/`` also holds non-device profiles (Gradle output, keyed by Gradle
    version). Those are identified by their manifest — a device profile records
    a ``device`` block — so a tool profile is not parameterised into tests that
    only make sense for a device.
    """
    names = []
    for path in sorted(RECORDED_ROOT.iterdir()):
        manifest = path / "MANIFEST.json"
        if not path.is_dir() or not manifest.exists():
            continue
        if "device" in json.loads(manifest.read_text(encoding="utf-8")):
            names.append(path.name)
    return sorted(names, key=lambda n: (n != PRIMARY_PROFILE, n))


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

    def has(self, name: str) -> bool:
        """Whether this profile recorded that fixture.

        Profiles legitimately differ: a personal phone should not have its
        logcat or screen contents committed, so those exist only for the
        emulator profile.
        """
        stem = name.split(".", maxsplit=1)[0]
        return any((self._root / f"{stem}{ext}").exists() for ext in (".txt", ".xml"))

    @property
    def name(self) -> str:
        """Profile directory name, e.g. ``pixel4xl-api33``."""
        return self._root.name

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
    """Verbatim tool output from the primary (complete) device profile.

    Example:
        def test_parser_handles_real_logcat(recorded):
            parsed = [parse_logcat_line(ln) for ln in recorded.lines("logcat_threadtime")]
            assert any(p for p in parsed), "parser matched nothing against real output"
    """
    return RecordedFixtures(RECORDED_DIR)


@pytest.fixture(scope="session")
def recorded_anywhere():
    """Look a fixture up in whichever profile recorded it.

    For output produced by the *host* adb client rather than by a device --
    "error: device 'x' not found", for instance -- which is identical whatever
    is attached and so has no natural device profile. Still fails loudly when
    the fixture exists nowhere, rather than skipping.
    """

    def _find(name: str) -> str:
        for profile in _available_profiles():
            accessor = RecordedFixtures(RECORDED_ROOT / profile)
            if accessor.has(name):
                return accessor.text(name)
        pytest.fail(
            f"Recorded fixture '{name}' is missing from every profile.\n"
            f"Record it with: python tests/record_fixtures.py --only {name}"
        )

    return _find


@pytest.fixture(params=_available_profiles())
def any_profile(request) -> RecordedFixtures:
    """Each recorded device profile in turn.

    For invariants that must hold on *every* Android version we have evidence
    for — chiefly "this command does not exist". A subcommand absent on one API
    level but present on another is exactly the kind of drift that a
    single-device fixture set hides.

    Profiles hold different subsets, so use ``has()`` to skip cleanly rather
    than failing on a fixture that device never recorded.
    """
    return RecordedFixtures(RECORDED_ROOT / request.param)


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

    # Prefer an emulator, always. These tests tap, swipe, press keys, take
    # screenshots and send SMS, and `adb devices` lists a physical phone before
    # `emulator-5554`, so taking serials[0] pointed the whole lane at whatever
    # handset a developer happened to have plugged in. That has already
    # happened once in this project.
    #
    # A physical device is used only when it is the ONLY thing attached, which
    # is a deliberate choice by whoever attached it.
    emulators = [serial for serial in serials if serial.startswith("emulator-")]
    if emulators:
        return emulators[0]

    return serials[0]


@pytest.fixture(scope="session")
def emulator_only_device(live_device: str) -> str:
    """A booted EMULATOR, skipping rather than falling back to a phone.

    For tests whose side effects must never reach a real handset -- sending an
    SMS into someone's inbox, loading a snapshot, wiping state -- and for
    anything going through the emulator console, which a physical device does
    not have at all (`adb emu` there exits 1 and prints nothing).
    """
    if not live_device.startswith("emulator-"):
        pytest.skip(
            f"only a physical device ({live_device}) is attached. This test has "
            f"side effects that must not reach a real handset; boot an emulator."
        )
    return live_device
