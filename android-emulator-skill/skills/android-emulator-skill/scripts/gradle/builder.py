#!/usr/bin/env python3
"""
Gradle build execution.

Invokes the project's ``./gradlew`` wrapper for assemble/build/test tasks. Never
uses ``shell=True``; commands are built as explicit argument lists and run with
an explicit ``check=False`` plus a timeout. Mirrors the iOS ``xcode/builder.py``
architecture but speaks Gradle (tasks, ``-p``/module, variants) instead of
xcodebuild.

Timeouts are tunable via the ANDROID_EMU_ prefix:
- ANDROID_EMU_BUILD_TIMEOUT (default 1800s)
- ANDROID_EMU_TEST_TIMEOUT  (default 2700s)
"""

import contextlib
import subprocess
from pathlib import Path

from common.env_config import env_int

from .config import Config
from .results import (
    aggregate_test_results,
    find_test_result_files,
    parse_build_output,
)

BUILD_TIMEOUT = env_int("ANDROID_EMU_BUILD_TIMEOUT", 1800)
TEST_TIMEOUT = env_int("ANDROID_EMU_TEST_TIMEOUT", 2700)


class BuildResult:
    """Structured outcome of a build/test run."""

    def __init__(
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
        returncode: int | None,
    ):
        self.success = success
        self.stdout = stdout
        self.stderr = stderr
        self.errors = errors
        self.warnings = warnings
        self.failed_tasks = failed_tasks
        self.test_results = test_results
        self.variant = variant
        self.module = module
        self.tasks = tasks
        self.duration = duration
        self.returncode = returncode


class BuildRunner:
    """
    Execute Gradle build/test tasks via the project's gradlew wrapper.

    Handles task construction (assemble/build/test), module targeting
    (``-p``/module path), variant capitalization, clean, and JUnit XML
    discovery for test runs.
    """

    def __init__(
        self,
        project_dir: str,
        module: str | None = None,
        variant: str = "debug",
    ):
        """
        Initialize the build runner.

        Args:
            project_dir: Path to the Android project directory (must contain gradlew)
            module: Optional Gradle module path (e.g., ":app") to scope tasks
            variant: Build variant (e.g., "debug", "release")

        Raises:
            ValueError: If the project directory or gradlew wrapper is missing.
        """
        self.project_dir = Path(project_dir)
        if not self.project_dir.exists():
            raise ValueError(f"Project directory not found: {project_dir}")

        self.gradlew = self.project_dir / "gradlew"
        if not self.gradlew.exists():
            raise ValueError(f"gradlew not found in {project_dir}")

        # Ensure the wrapper is executable (cloned repos sometimes drop the bit).
        with contextlib.suppress(OSError):
            self.gradlew.chmod(0o755)

        self.module = module
        self.variant = variant

    def _task_name(self, base: str) -> str:
        """
        Build a Gradle task name, scoped to a module if one is set.

        e.g. base="assembleDebug", module=":app" -> ":app:assembleDebug".
        """
        # Only the first letter changes case. str.capitalize() lowercases the
        # remainder, turning AGP's "proStagingDebug" into "Prostagingdebug".
        # Gradle's name-abbreviation matching is case-insensitive and resolves
        # that anyway, so it does not break the build -- but the task name is
        # wrong in logs, and the fallback stops working as soon as the
        # abbreviation is ambiguous.
        capitalized = f"{base}{self.variant[:1].upper()}{self.variant[1:]}"
        if self.module:
            module = self.module if self.module.startswith(":") else f":{self.module}"
            return f"{module}:{capitalized}"
        return capitalized

    def _build_command(self, tasks: list[str], clean: bool) -> list[str]:
        """
        Construct the gradlew argument list (never shell=True).

        Args:
            tasks: Gradle task names to run
            clean: Prepend a clean task

        Returns:
            Full command list, e.g. ["/path/gradlew", "clean", "assembleDebug"].
        """
        cmd = [str(self.gradlew)]
        # --console=plain keeps output parseable; -p targets the project dir.
        cmd.extend(["--console=plain", "-p", str(self.project_dir)])
        if clean:
            cmd.append("clean")
        cmd.extend(tasks)
        return cmd

    def build(self, clean: bool = False) -> BuildResult:
        """
        Assemble the project (``assemble<Variant>``).

        Args:
            clean: Run a clean task first

        Returns:
            BuildResult with parsed errors/warnings.
        """
        task = self._task_name("assemble")
        return self._run([task], clean=clean, timeout=BUILD_TIMEOUT, is_test=False)

    def test(self, clean: bool = False, suite: str | None = None) -> BuildResult:
        """
        Run unit tests (``test<Variant>UnitTest``) and parse JUnit XML.

        Args:
            clean: Run a clean task first
            suite: Optional test filter passed to Gradle's ``--tests``

        Returns:
            BuildResult with parsed errors/warnings and aggregated test_results.
        """
        task = self._task_name("test") + "UnitTest"
        extra = ["--tests", suite] if suite else []
        return self._run([task, *extra], clean=clean, timeout=TEST_TIMEOUT, is_test=True)

    def _run(self, tasks: list[str], *, clean: bool, timeout: int, is_test: bool) -> BuildResult:
        """
        Execute a gradlew command and assemble a BuildResult.

        Args:
            tasks: Gradle task names (and any extra args)
            clean: Run a clean task first
            timeout: subprocess timeout in seconds
            is_test: Whether to discover + parse JUnit XML afterwards

        Returns:
            BuildResult.
        """
        import time

        cmd = self._build_command(tasks, clean)
        start = time.time()

        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self.project_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            returncode = completed.returncode
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (
                e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            ) + f"\nerror: Gradle timed out after {timeout}s"
            parsed = parse_build_output(stdout, stderr)
            return BuildResult(
                success=False,
                stdout=stdout,
                stderr=stderr,
                errors=parsed["errors"],
                warnings=parsed["warnings"],
                failed_tasks=parsed["failed_tasks"],
                test_results=None,
                variant=self.variant,
                module=self.module,
                tasks=tasks,
                duration=round(duration, 2),
                returncode=None,
            )

        duration = time.time() - start
        success = returncode == 0
        parsed = parse_build_output(stdout, stderr)

        test_results = None
        if is_test:
            search_root = self.project_dir
            if self.module:
                module_dir = self.module.lstrip(":").replace(":", "/")
                candidate = self.project_dir / module_dir
                if candidate.exists():
                    search_root = candidate
            xml_files = find_test_result_files(search_root)
            test_results = aggregate_test_results(xml_files)

        # Learn the module/variant on success (never let config break the build).
        if success:
            try:
                config = Config.load(project_dir=self.project_dir)
                config.update_last_used(self.module, self.variant)
                config.save()
            except Exception:
                pass

        return BuildResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            errors=parsed["errors"],
            warnings=parsed["warnings"],
            failed_tasks=parsed["failed_tasks"],
            test_results=test_results,
            variant=self.variant,
            module=self.module,
            tasks=tasks,
            duration=round(duration, 2),
            returncode=returncode,
        )
