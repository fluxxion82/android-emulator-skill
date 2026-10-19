#!/usr/bin/env python3
"""
Gradle build execution.

Invokes the project's ``./gradlew`` wrapper for assemble/build/test tasks. Never
uses ``shell=True``; commands are built as explicit argument lists and run with
an explicit ``check=False`` plus a timeout. Mirrors the iOS ``xcode/builder.py``
architecture but speaks Gradle (tasks, ``-p``/module, variants) instead of
xcodebuild.

Timeouts are tunable via the ANDROID_EMU_ prefix:
- ANDROID_EMU_BUILD_TIMEOUT (default 1800s; also bounds a --module test run's projectDir query)
- ANDROID_EMU_TEST_TIMEOUT  (default 2700s)
"""

import contextlib
import re
import shlex
import subprocess
from pathlib import Path

from common import cli
from common.env_config import env_int

from .config import Config
from .results import (
    aggregate_test_results,
    extract_failure_reasons,
    find_test_result_directories,
    find_test_result_files,
    parse_build_output,
)

# What a Gradle older than 7.6 says when handed a task's --rerun (recorded:
# gradle-7.5.1/gradle_rerun_unknown_option): "> Unknown command-line option
# '--rerun'." in the "* What went wrong:" block, exit 1.
RERUN_UNSUPPORTED = "Unknown command-line option '--rerun'"

BUILD_TIMEOUT = env_int("ANDROID_EMU_BUILD_TIMEOUT", 1800)
TEST_TIMEOUT = env_int("ANDROID_EMU_TEST_TIMEOUT", 2700)

# What ``<module>:properties --property projectDir`` prints for the module's
# directory (recorded: gradle-8.13 and gradle-7.5.1 gradle_properties_project_dir):
# a dashed ``Project ':app'`` banner even under -q, then exactly this one line
# holding the absolute path. A filter that matches no property still exits 0
# and prints ``<name>: null`` instead (gradle_properties_no_such_property).
_PROJECT_DIR_LINE = re.compile(r"^projectDir: (?P<path>.+?)\r?$", re.MULTILINE)


def _gradle_query(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run one read-only Gradle question; its own seam so a test can answer it."""
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


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
        results_dir: str | None = None,
        timeout_seconds: int | None = None,
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
        # Where a test run's JUnit XML was looked for; None for a build.
        self.results_dir = results_dir
        self.timeout_seconds = timeout_seconds

    @property
    def timed_out(self) -> bool:
        """Whether the Gradle wrapper exceeded the configured execution timeout."""
        return self.timeout_seconds is not None

    @property
    def rerun_unsupported(self) -> bool:
        """A test run this project's Gradle refused because it predates ``--rerun`` (7.6)."""
        return (
            self.results_dir is not None
            and self.returncode not in (None, 0)
            and RERUN_UNSUPPORTED in f"{self.stdout}\n{self.stderr}"
        )

    @property
    def no_test_results(self) -> bool:
        """A test run Gradle called successful that left no test result of its own.

        Not a pass: nothing says a test ran. ``build_and_test.py`` refuses it.
        A run Gradle failed is not this case -- its own failure is the verdict.
        """
        return self.results_dir is not None and self.returncode == 0 and not self.test_results


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
        return self._run([task], clean=clean, timeout=BUILD_TIMEOUT)

    def test(self, clean: bool = False, suite: str | None = None) -> BuildResult:
        """
        Run unit tests (``test<Variant>UnitTest``) and parse JUnit XML.

        The task is always run with ``--rerun`` (Gradle 7.6+). Without it a
        test task whose inputs have not changed is UP-TO-DATE: Gradle exits 0
        and writes no report, so the run has no result of its own to read.
        Measured on the compose scaffold, Gradle 8.13: a second identical run
        printed ``> Task :app:testDebugUnitTest UP-TO-DATE`` and left the
        report's mtime unchanged; with ``--rerun`` only the test task executed
        ("1 executed, 22 up-to-date") and the report was rewritten. Gradle
        7.5.1 rejects the option (recorded: gradle_rerun_unknown_option), and
        :attr:`BuildResult.rerun_unsupported` says so.

        Args:
            clean: Run a clean task first
            suite: Optional test filter passed to Gradle's ``--tests``

        Returns:
            BuildResult with parsed errors/warnings and aggregated test_results.
        """
        task = self._task_name("test") + "UnitTest"
        # Task options bind to the task named before them, so this stays next to it.
        extra = ["--rerun", *(["--tests", suite] if suite else [])]
        return self._run([task, *extra], clean=clean, timeout=TEST_TIMEOUT, test_task=task)

    def _module_path(self) -> str | None:
        """The selected module as a normalised ``:a:b`` Gradle path, or None."""
        if not self.module:
            return None
        parts = self.module.lstrip(":").split(":")
        if not parts or any(
            not part or part in {".", ".."} or "/" in part or "\\" in part for part in parts
        ):
            cli.fail(
                f"invalid Gradle module path: {self.module}",
                "Pass a colon-separated Gradle project path such as :app or :feature:login.",
            )
        return f":{':'.join(parts)}"

    def _results_root(self) -> Path:
        """The directory a test run's reports are searched under: the module's, if it has one.

        A module's directory is Gradle's own answer, never a reading of the
        settings script. Two static scanners (PR #37, #41) lost to Groovy and
        Kotlin syntax every review round -- slashy strings, unbraced ``if``
        bodies, ``project(":$name")``, ``setProjectDir``, ``includeFlat``,
        settings plugins -- and a wrong answer here points report cleanup at
        another directory. Only Gradle evaluates the settings script, so it is
        asked; it costs one configuration of the build per ``--module`` test run.
        """
        module = self._module_path()
        if module is None:
            return self.project_dir
        candidate = self._gradle_project_dir(module)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.project_dir.resolve()):
            cli.fail(
                f"Gradle module {module} resolves outside the project: {candidate}",
                "Pass --module a project whose projectDir is under the selected --project "
                "root; build_and_test only cleans and reads reports inside it.",
            )
        if not resolved.is_dir():
            cli.fail(
                f"Gradle module {module} directory does not exist: {candidate}",
                f"Point project('{module}').projectDir at an existing directory in "
                "settings.gradle(.kts), or pass --module for an existing Gradle project.",
            )
        return candidate

    def _gradle_project_dir(self, module: str) -> Path:
        """Ask the project's Gradle for ``module``'s projectDir; refuse anything but one answer."""
        command = [
            str(self.gradlew),
            "--console=plain",
            "-q",
            "-p",
            str(self.project_dir),
            f"{module}:properties",
            "--property",
            "projectDir",
        ]
        shown = shlex.join(command)
        try:
            completed = _gradle_query(command, self.project_dir, BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            cli.fail(
                f"Gradle did not report the directory of module {module} within "
                f"{BUILD_TIMEOUT}s: {shown}",
                "Raise ANDROID_EMU_BUILD_TIMEOUT (a first configuration may download plugins), "
                "or run the command yourself to see where Gradle stops.",
            )
        except OSError as error:
            cli.fail(
                f"could not start the Gradle wrapper to resolve module {module}: {error}",
                f"Make {self.gradlew} a runnable Gradle wrapper (./gradlew --version), then rerun.",
            )

        stdout = completed.stdout or ""
        output = f"{stdout}\n{completed.stderr or ''}".strip()
        if completed.returncode != 0:
            reasons = extract_failure_reasons(output)
            said = "; ".join(reasons) if reasons else (output[-500:] or "no output")
            cli.fail(
                f"Gradle could not resolve module {module} (exit {completed.returncode}): "
                f"{said}",
                f"Pass --module a project Gradle knows (./gradlew projects lists them), or fix "
                f"the error Gradle reports for: {shown}",
                details={"command": shown, "gradle_output": output[-2000:]},
            )

        answers = [match.group("path") for match in _PROJECT_DIR_LINE.finditer(stdout)]
        if len(answers) != 1:
            cli.fail(
                f"Gradle printed {len(answers)} projectDir lines for module {module}, "
                f"expected exactly one: {shown}",
                "Run the command yourself; an init script or plugin that prints its own "
                "projectDir line makes the module's directory ambiguous, so no report is "
                "cleaned or read.",
                details={"command": shown, "gradle_output": output[-2000:]},
            )
        # Gradle prints an absolute path; a relative one would be read against the
        # project rather than this process's cwd, then held to the same containment.
        return self.project_dir / answers[0]

    def _report_files(self, root: Path, task: str) -> tuple[list[Path], list[Path]]:
        """Find reports and prove every resolved path stays in its permitted directory."""
        recursive = self.module is None
        directories = find_test_result_directories(root, task=task, recursive=recursive)
        paths = find_test_result_files(root, task=task, recursive=recursive)
        project = self.project_dir.resolve()
        resolved_directories: list[Path] = []

        for directory in directories:
            # Check the lexical path before resolving it. Otherwise an in-project
            # build/test-results/task symlink can redefine the very boundary used
            # to decide which reports cleanup is permitted to unlink.
            module = directory.parents[2]
            components = (
                module / "build",
                module / "build" / "test-results",
                directory,
            )
            symlink = next((component for component in components if component.is_symlink()), None)
            if symlink is not None:
                affected = next((path for path in paths if path.parent == directory), None)
                report_detail = f"; refusing report {affected}" if affected is not None else ""
                cli.fail(
                    f"refusing symlinked JUnit report path component: {symlink}{report_detail}",
                    "Replace symlinks between the module and its test result task directory "
                    "with real directories, then rerun the Gradle test task.",
                )

            resolved_module = module.resolve()
            resolved = directory.resolve()
            expected = resolved_module / "build" / "test-results" / directory.name
            if resolved != expected:
                cli.fail(
                    f"refusing redirected JUnit report directory: {directory}",
                    "Restore the module's lexical build/test-results task directory, "
                    "then rerun the Gradle test task.",
                )
            resolved_directories.append(resolved)

        # Validate every target before unlinking any of them. In particular, a
        # build/ symlink may put both the directory and its reports elsewhere.
        for path in paths:
            resolved = path.resolve()
            permitted = any(
                resolved.is_relative_to(directory) and path.parent.resolve() == directory
                for directory in resolved_directories
            )
            if not resolved.is_relative_to(project) or not permitted:
                cli.fail(
                    f"refusing to use JUnit report outside its project task directory: {path}",
                    "Replace build/test-results symlinks with directories inside the project, "
                    "then rerun the Gradle test task.",
                )
        for directory in resolved_directories:
            if not directory.is_relative_to(project):
                cli.fail(
                    f"refusing JUnit report directory outside the project: {directory}",
                    "Replace build/test-results symlinks with directories inside the project, "
                    "then rerun the Gradle test task.",
                )
        return directories, paths

    def _clear_reports(self, root: Path, task: str) -> None:
        """Delete the task's ``TEST-*.xml`` reports under ``root`` before Gradle runs.

        After the run, everything in the task's results directory is then this
        run's -- exactly, with no clock and no comparison. Both earlier
        attempts were heuristics with a hole: an mtime cutoff refused fresh
        reports on a filesystem that rounds or lags and accepted an untouched
        one from just before the run; an (mtime, size) snapshot dropped a
        failing report rewritten byte for byte, and a run with one new passing
        report beside it passed (review of PR #23).

        These are build outputs, and Gradle regenerates them: the test task
        runs with ``--rerun``, and Gradle itself removes them when the task
        has no source (measured: NO-SOURCE left no ``test-results`` behind).
        Only ``TEST-*.xml`` directly in ``build/test-results/<task>/`` is
        touched -- not the task's ``binary/`` results, not another task's
        reports, not another module's when ``--module`` scopes ``root``.

        Raises:
            OSError: A report could not be deleted -- reading results then
                would be guessing again, so the run does not start.
        """
        _, paths = self._report_files(root, task)
        for path in paths:
            path.unlink(missing_ok=True)

    def _run(
        self,
        tasks: list[str],
        *,
        clean: bool,
        timeout: int,
        test_task: str | None = None,
    ) -> BuildResult:
        """
        Execute a gradlew command and assemble a BuildResult.

        For a test run the verdict is Gradle's exit status AND the tests'.
        ``test { ignoreFailures = true }`` makes Gradle print BUILD SUCCESSFUL
        and exit 0 over failing tests (recorded: gradle_junit_error_ignorefailures),
        so a run passes only if Gradle exited 0, this run wrote test results,
        and they hold no ``<failure>`` and no ``<error>`` (X7).

        Args:
            tasks: Gradle task names (and any extra args)
            clean: Run a clean task first
            timeout: subprocess timeout in seconds
            test_task: For a test run, the task whose JUnit XML this run wrote

        Returns:
            BuildResult.
        """
        import time

        cmd = self._build_command(tasks, clean)
        name = test_task.rsplit(":", 1)[-1] if test_task is not None else None
        self._module_path()
        # Only a test run reads reports, so only it pays for asking Gradle where they are.
        root = self._results_root() if name is not None else self.project_dir
        if name is not None:
            self._clear_reports(root, name)
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
                timeout_seconds=timeout,
            )

        duration = time.time() - start
        success = returncode == 0
        parsed = parse_build_output(stdout, stderr)

        test_results = None
        results_dir = None
        if name is not None:
            # The task's reports were cleared before the run, so whatever is
            # in its directory now, this run wrote (N6).
            directories, xml_files = self._report_files(root, name)
            results_dir = str(
                directories[0]
                if len(directories) == 1
                else root / "**" / "build" / "test-results" / name
            )
            aggregate = aggregate_test_results(xml_files)
            # A report with no test case in it (a truncated write parses to
            # zero) is no more a result than no report at all.
            test_results = aggregate if aggregate["total"] else None
            success = (
                success
                and test_results is not None
                and test_results["failed"] == 0
                and test_results["errors"] == 0
            )

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
            results_dir=results_dir,
        )
