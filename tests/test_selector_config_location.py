"""Where emulator_selector stores its recent-AVD history.

A10, two defects:

1. `_default_config_path()` returned ``Path(__file__).parent / "config.json"``
   whenever that directory was writable -- which it normally is. The skill ships
   as a plugin, so this writes user state **into the installed package**: shared
   across every project and checkout, lost on reinstall, and dirtying the
   distribution. `.gitignore` already carries an entry for that file, which is
   evidence it has leaked into the repo before.

2. `record_recent()` wrote the whole file as ``{"recent": [...]}``, discarding
   any other keys a config might hold rather than merging.

User state belongs under the user's home directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import emulator_selector
import pytest

SCRIPTS_DIR = Path(emulator_selector.__file__).resolve().parent


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Redirect the fallback config dir at module level."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(
        emulator_selector, "FALLBACK_CONFIG_DIR", fake_home / ".android-emulator-skill"
    )
    # A legacy file left behind on the developer's machine must not change the
    # answer these tests are asking about.
    monkeypatch.setattr(
        emulator_selector, "LEGACY_CONFIG_PATH", tmp_path / "absent" / "config.json"
    )
    return fake_home


# ---------------------------------------------------------------------------
# The package directory is not a state store.
# ---------------------------------------------------------------------------


def test_default_config_is_not_inside_the_installed_package(home):
    """The resolved default must sit outside the shipped scripts directory."""
    path = emulator_selector.EmulatorSelector._default_config_path()
    assert (
        SCRIPTS_DIR not in path.parents
    ), f"default config path {path} is inside the installed plugin package"


def test_default_config_is_under_the_user_config_dir(home):
    path = emulator_selector.EmulatorSelector._default_config_path()
    assert path == home / ".android-emulator-skill" / "config.json"


def test_recording_never_creates_a_file_in_the_package(home):
    """The end-to-end guarantee, independent of how the path is resolved."""
    before = set(SCRIPTS_DIR.glob("config.json"))

    selector = emulator_selector.EmulatorSelector()
    selector.record_recent("Pixel_9")

    assert (
        set(SCRIPTS_DIR.glob("config.json")) == before
    ), "record_recent created config.json inside the installed package"
    assert (home / ".android-emulator-skill" / "config.json").exists()


# ---------------------------------------------------------------------------
# Writing history must not destroy the rest of the config.
# ---------------------------------------------------------------------------


def test_record_recent_preserves_unrelated_keys(tmp_path):
    """A whole-file rewrite silently discarded any other settings."""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"preferred_module": ":app", "recent": ["Old_AVD"]}), encoding="utf-8"
    )

    selector = emulator_selector.EmulatorSelector(config_path=config)
    selector.record_recent("Pixel_9")

    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["recent"][0] == "Pixel_9"
    assert (
        payload.get("preferred_module") == ":app"
    ), "record_recent discarded an unrelated key instead of merging"


def test_record_recent_survives_a_corrupt_config(tmp_path):
    """A merge must not turn an unreadable file into a hard failure."""
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")

    selector = emulator_selector.EmulatorSelector(config_path=config)
    selector.record_recent("Pixel_9")

    assert json.loads(config.read_text(encoding="utf-8"))["recent"] == ["Pixel_9"]


def test_record_recent_dedupes_and_orders_most_recent_first(tmp_path):
    """Guard the existing behaviour while changing how the file is written."""
    config = tmp_path / "config.json"
    selector = emulator_selector.EmulatorSelector(config_path=config)

    selector.record_recent("A")
    selector.record_recent("B")
    selector.record_recent("A")

    assert json.loads(config.read_text(encoding="utf-8"))["recent"] == ["A", "B"]


# ---------------------------------------------------------------------------
# Back-compat: an existing script-local config must still be readable.
# ---------------------------------------------------------------------------


def test_existing_script_local_config_is_still_honoured(home, monkeypatch, tmp_path):
    """Anyone who already has one should not silently lose their history."""
    package_config = tmp_path / "package" / "config.json"
    package_config.parent.mkdir(parents=True)
    package_config.write_text(json.dumps({"recent": ["Legacy_AVD"]}), encoding="utf-8")

    monkeypatch.setattr(emulator_selector, "LEGACY_CONFIG_PATH", package_config)

    path = emulator_selector.EmulatorSelector._default_config_path()
    assert path == package_config, "an existing legacy config should still be used"
