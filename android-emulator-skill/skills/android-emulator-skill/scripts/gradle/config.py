#!/usr/bin/env python3
"""
Project-local configuration for the Android Gradle build subpackage.

Stores learned build preferences (preferred module + variant) so repeat builds
don't have to re-specify them. Mirrors the iOS ``xcode/config.py`` architecture
but with Gradle-native fields.

Config file location: ``.claude/skills/<skill-directory-name>/config.json``

The skill directory name is auto-detected from this file's installation location,
so configs work regardless of what users name the skill directory. Auto-updates
``last_used_module`` / ``last_used_variant`` after successful builds.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Config:
    """
    Project-local configuration with auto-learning.

    Safe read/write: malformed JSON falls back to defaults, writes are atomic
    (temp file + rename) and never raise — a config problem must never break a
    build.
    """

    DEFAULT_CONFIG = {
        "build": {
            "preferred_module": None,
            "preferred_variant": None,
            "last_used_module": None,
            "last_used_variant": None,
            "last_used_at": None,
        }
    }

    def __init__(self, data: dict[str, Any], config_path: Path):
        """
        Initialize config.

        Args:
            data: Config data dict
            config_path: Path to config file
        """
        self.data = data
        self.config_path = config_path

    @staticmethod
    def load(project_dir: Path | None = None) -> "Config":
        """
        Load config from project directory.

        Args:
            project_dir: Project root (defaults to cwd)

        Returns:
            Config instance (creates default if not found)
        """
        if project_dir is None:
            project_dir = Path.cwd()

        # Auto-detect skill directory name from actual installation location.
        # This file lives at: skill/scripts/gradle/config.py
        # Navigate up to skill/ and use its name.
        skill_root = Path(__file__).parent.parent.parent  # gradle/ -> scripts/ -> skill/
        skill_name = skill_root.name

        config_path = project_dir / ".claude" / "skills" / skill_name / "config.json"

        if config_path.exists():
            try:
                with open(config_path) as f:
                    data = json.load(f)
                merged = Config._merge_with_defaults(data)
                return Config(merged, config_path)
            except json.JSONDecodeError as e:
                print(f"Warning: Invalid JSON in {config_path}: {e}", file=sys.stderr)
                print("Using default config", file=sys.stderr)
                return Config(Config._default_copy(), config_path)
            except Exception as e:
                print(f"Warning: Could not load config: {e}", file=sys.stderr)
                return Config(Config._default_copy(), config_path)

        # Return default config (created on first save).
        return Config(Config._default_copy(), config_path)

    @staticmethod
    def _default_copy() -> dict[str, Any]:
        """Return a deep-ish copy of the default config (nested dict copied)."""
        return {k: v.copy() if isinstance(v, dict) else v for k, v in Config.DEFAULT_CONFIG.items()}

    @staticmethod
    def _merge_with_defaults(data: dict[str, Any]) -> dict[str, Any]:
        """
        Merge user config with defaults.

        Args:
            data: User config data

        Returns:
            Merged config with all default fields present
        """
        merged = Config._default_copy()
        if isinstance(data.get("build"), dict):
            merged["build"].update(data["build"])
        return merged

    def save(self) -> None:
        """
        Save config to file atomically.

        Uses temp file + rename for atomic writes. Creates parent directories if
        needed. Never raises — a config write failure should not break a build.
        """
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.config_path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(self.data, f, indent=2)
                f.write("\n")
            temp_path.replace(self.config_path)
        except Exception as e:
            print(f"Warning: Could not save config: {e}", file=sys.stderr)

    def update_last_used(self, module: str | None, variant: str | None) -> None:
        """
        Record the module/variant used by a successful build.

        Args:
            module: Gradle module path (e.g., ":app") or None
            variant: Build variant (e.g., "debug") or None
        """
        if module is not None:
            self.data["build"]["last_used_module"] = module
        if variant is not None:
            self.data["build"]["last_used_variant"] = variant
        self.data["build"]["last_used_at"] = datetime.now(UTC).isoformat()

    def get_preferred_module(self) -> str | None:
        """
        Get preferred module.

        Priority: preferred_module (manual) -> last_used_module (learned) -> None.
        """
        build = self.data.get("build", {})
        return build.get("preferred_module") or build.get("last_used_module")

    def get_preferred_variant(self) -> str | None:
        """
        Get preferred variant.

        Priority: preferred_variant (manual) -> last_used_variant (learned) -> None.
        """
        build = self.data.get("build", {})
        return build.get("preferred_variant") or build.get("last_used_variant")
