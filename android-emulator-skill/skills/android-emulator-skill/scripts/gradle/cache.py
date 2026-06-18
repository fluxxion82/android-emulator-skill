#!/usr/bin/env python3
"""
Build/test result cache for progressive disclosure.

Persists the full Gradle stdout/stderr plus parsed errors, warnings and test
results keyed by a short build ID. Agents see a one-line summary + the ID by
default, then drill into details via ``--get-errors`` / ``--get-warnings`` /
``--get-log <ID>`` without re-running the build.

Reuses ``common.cache_utils.ProgressiveCache`` for storage (under
``~/.android-emulator-skill/cache``), mirroring the iOS ``xcode/cache.py`` role.
"""

from common.cache_utils import ProgressiveCache
from common.env_config import env_int

# Cached build results live longer than the default 1h so a drill-down later in a
# session still resolves. Override via ANDROID_EMU_BUILD_CACHE_TTL_HOURS.
CACHE_TTL_HOURS = env_int("ANDROID_EMU_BUILD_CACHE_TTL_HOURS", 24)


class BuildResultCache:
    """
    Persist and retrieve full build/test output keyed by short build IDs.

    The stored payload contains everything needed for progressive disclosure:
    raw stdout/stderr, parsed errors/warnings, failed tasks, and test results.
    """

    CACHE_TYPE = "build-result"

    def __init__(self, cache_dir: str | None = None):
        """
        Initialize the cache.

        Args:
            cache_dir: Custom cache directory (defaults to ProgressiveCache's
                ``~/.android-emulator-skill/cache``).
        """
        self._cache = ProgressiveCache(cache_dir=cache_dir, max_age_hours=CACHE_TTL_HOURS)

    def save(
        self,
        *,
        success: bool,
        stdout: str,
        stderr: str,
        errors: list[dict],
        warnings: list[dict],
        failed_tasks: list[str],
        test_results: dict | None,
        variant: str | None,
        module: str | None,
        tasks: list[str],
        duration: float,
    ) -> str:
        """
        Save a build/test result and return its short build ID.

        Returns:
            Build ID like ``build-20251028-143052``.
        """
        payload = {
            "success": success,
            "stdout": stdout,
            "stderr": stderr,
            "errors": errors,
            "warnings": warnings,
            "failed_tasks": failed_tasks,
            "test_results": test_results,
            "variant": variant,
            "module": module,
            "tasks": tasks,
            "duration": duration,
        }
        return self._cache.save(payload, self.CACHE_TYPE)

    def get(self, build_id: str) -> dict | None:
        """
        Retrieve a cached build/test result by ID.

        Args:
            build_id: Build ID from :meth:`save`

        Returns:
            Stored payload dict, or None if missing/expired.
        """
        return self._cache.get(build_id)

    def list(self) -> list[dict]:
        """List recent cached build results (most recent first)."""
        return self._cache.list_entries(self.CACHE_TYPE)

    def get_log(self, build_id: str) -> str | None:
        """
        Retrieve the combined stdout+stderr log for a build.

        Args:
            build_id: Build ID

        Returns:
            Combined log string, or None if the build is not cached.
        """
        payload = self.get(build_id)
        if payload is None:
            return None
        parts = [part for part in (payload.get("stdout"), payload.get("stderr")) if part]
        return "\n".join(parts)
