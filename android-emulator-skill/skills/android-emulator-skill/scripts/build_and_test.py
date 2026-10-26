#!/usr/bin/env python3
"""
Build and Test Automation for Android Gradle Projects.

Ultra token-efficient build automation with progressive disclosure. The default
output is one summary line plus a result ID; drill into details on demand with
``--get-errors`` / ``--get-warnings`` / ``--get-log <ID>``.

Architecture mirrors the iOS ``xcode/`` subpackage but uses Gradle-native
mechanics (gradlew tasks, JUnit XML, console parsing). See ``scripts/gradle/``.

Usage Examples:
    # Build (minimal output)
    python scripts/build_and_test.py --project /path/to/project
    # Output: Build: SUCCESS (0 errors, 3 warnings) [1:32] [build-20251028-143052]

    # Build a specific module + variant, clean first
    python scripts/build_and_test.py --project . --module :app --variant release --clean

    # Run unit tests (parses JUnit XML for pass/fail + failed names)
    python scripts/build_and_test.py --project . --test

    # Filter tests
    python scripts/build_and_test.py --project . --test --suite "com.example.MyTest"

    # Drill into a previous build's details
    python scripts/build_and_test.py --get-errors build-20251028-143052
    python scripts/build_and_test.py --get-warnings build-20251028-143052
    python scripts/build_and_test.py --get-log build-20251028-143052

    # List recent builds
    python scripts/build_and_test.py --list-builds
"""

import argparse
import json

from common import cli
from common.env_config import env_int
from gradle import BuildResultCache, BuildRunner, OutputFormatter

BUILD_JSON_CAP = env_int("ANDROID_EMU_BUILD_JSON_CAP", 50)


def _print_get_errors(payload: dict, as_json: bool) -> int:
    """Print cached errors for a build ID."""
    errors = payload.get("errors", [])
    if as_json:
        print(json.dumps(errors[:BUILD_JSON_CAP], indent=2))
    else:
        print(OutputFormatter.format_errors(errors))
    return 0


def _print_get_warnings(payload: dict, as_json: bool) -> int:
    """Print cached warnings for a build ID."""
    warnings = payload.get("warnings", [])
    if as_json:
        print(json.dumps(warnings[:BUILD_JSON_CAP], indent=2))
    else:
        print(OutputFormatter.format_warnings(warnings))
    return 0


def _print_get_log(cache: BuildResultCache, build_id: str, as_json: bool) -> int:
    """Print the cached combined log for a build ID."""
    log = cache.get_log(build_id)
    if log is None:
        # A sentence on stderr and exit 1, nothing under --json: the shared
        # refusal now.
        cli.fail(
            f"no build log is cached for build {build_id}",
            "Use --list-builds to see the builds that are cached, then pass one of their ids.",
        )
    if as_json:
        print(json.dumps({"build_id": build_id, "log": log}, indent=2))
    else:
        print(OutputFormatter.format_log(log))
    return 0


def _handle_disclosure(args: argparse.Namespace, cache: BuildResultCache) -> int | None:
    """
    Handle progressive-disclosure / list flags.

    Returns an exit code if a disclosure flag was handled, else None.
    """
    if args.list_builds:
        builds = cache.list()
        if args.json:
            print(json.dumps(builds, indent=2))
        elif not builds:
            print("No cached builds found")
        else:
            print(f"Recent builds ({len(builds)}):")
            for entry in builds:
                print(f"  {entry['id']}  ({entry['age_seconds']}s ago)")
        return 0

    build_id = args.get_errors or args.get_warnings or args.get_log
    if not build_id:
        return None

    if args.get_log:
        return _print_get_log(cache, build_id, args.json)

    payload = cache.get(build_id)
    if payload is None:
        # `Error:` and a hint on stderr, nothing under --json: the shared refusal now.
        cli.fail(
            f"build result not found: {build_id}",
            "Use --list-builds to see the builds that are cached, then pass one of their ids.",
        )

    if args.get_errors:
        return _print_get_errors(payload, args.json)
    return _print_get_warnings(payload, args.json)


def _emit_result(args: argparse.Namespace, result, build_id: str) -> None:
    """Format and print a build/test result per the selected output mode."""
    status = "SUCCESS" if result.success else "FAILED"
    error_count = len(result.errors)
    warning_count = len(result.warnings)

    test_info = result.test_results
    failed_tests = test_info.get("failed_tests") if test_info else None

    if args.verbose:
        print(
            OutputFormatter.format_verbose(
                status=status,
                error_count=error_count,
                warning_count=warning_count,
                build_id=build_id,
                duration=result.duration,
                errors=result.errors or None,
                warnings=result.warnings or None,
                test_info=test_info,
                failed_tests=failed_tests or None,
            )
        )
    elif args.json:
        data = {
            "success": result.success,
            "build_id": build_id,
            "duration": result.duration,
            "error_count": error_count,
            "warning_count": warning_count,
            "variant": result.variant,
            "module": result.module,
            "tasks": result.tasks,
        }
        if test_info:
            data["test_results"] = {
                "total": test_info.get("total", 0),
                "passed": test_info.get("passed", 0),
                "failed": test_info.get("failed", 0),
                # <error> testcases fail the verdict too; without the count a
                # `success: false` beside "0 failed" would be unexplained.
                "errors": test_info.get("errors", 0),
                "skipped": test_info.get("skipped", 0),
            }
        if not result.success:
            if result.errors:
                data["errors"] = result.errors[:BUILD_JSON_CAP]
            if failed_tests:
                data["failed_tests"] = failed_tests[:BUILD_JSON_CAP]
        print(json.dumps(data, indent=2))
    else:
        print(
            OutputFormatter.format_minimal(
                status=status,
                error_count=error_count,
                warning_count=warning_count,
                build_id=build_id,
                duration=result.duration,
                test_info=test_info,
                errors=result.errors if not result.success else None,
                failed_tests=failed_tests if failed_tests else None,
            )
        )


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = cli.parser(
        description="Build and test Android Gradle projects with progressive disclosure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build (minimal output)
  python scripts/build_and_test.py --project /path/to/project

  # Build a module + variant, clean first
  python scripts/build_and_test.py --project . --module :app --variant release --clean

  # Run tests (parses JUnit XML)
  python scripts/build_and_test.py --project . --test --suite com.example.MyTest

  # Drill into a previous build
  python scripts/build_and_test.py --get-errors build-20251028-143052
  python scripts/build_and_test.py --get-log build-20251028-143052

  # List recent builds
  python scripts/build_and_test.py --list-builds
        """,
    )

    build_group = parser.add_argument_group("Build/Test Options")
    build_group.add_argument("--project", help="Path to the Android project directory")
    build_group.add_argument(
        "--module", "-p", help="Gradle module path to scope tasks (e.g., :app)"
    )
    build_group.add_argument("--variant", default="debug", help="Build variant (default: debug)")
    build_group.add_argument("--clean", action="store_true", help="Clean before building")
    build_group.add_argument("--test", action="store_true", help="Run unit tests")
    build_group.add_argument("--suite", help="Test filter passed to Gradle --tests")

    disclosure_group = parser.add_argument_group("Progressive Disclosure Options")
    disclosure_group.add_argument(
        "--get-errors", metavar="BUILD_ID", help="Get error details from a cached build"
    )
    disclosure_group.add_argument(
        "--get-warnings", metavar="BUILD_ID", help="Get warning details from a cached build"
    )
    disclosure_group.add_argument(
        "--get-log", metavar="BUILD_ID", help="Get the full log from a cached build"
    )
    disclosure_group.add_argument(
        "--list-builds", action="store_true", help="List recent cached builds"
    )

    output_group = parser.add_argument_group("Output Options")
    output_group.add_argument("--verbose", action="store_true", help="Show detailed output")
    output_group.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    cache = BuildResultCache()

    disclosure_exit = _handle_disclosure(args, cache)
    if disclosure_exit is not None:
        return disclosure_exit

    if not args.project:
        parser.error("--project is required (or use --get-* / --list-builds)")

    try:
        runner = BuildRunner(args.project, module=args.module, variant=args.variant)
    except ValueError as e:
        # A project that is not one (no directory, no gradlew). Reported by
        # common.cli: this printed prose to stderr and returned 1, leaving a
        # --json caller an empty stdout.
        cli.fail(
            str(e),
            "Pass --project the Android project's root -- the directory that holds gradlew.",
        )

    if args.test:
        result = runner.test(clean=args.clean, suite=args.suite)
    else:
        result = runner.build(clean=args.clean)

    build_id = cache.save(
        success=result.success,
        stdout=result.stdout,
        stderr=result.stderr,
        errors=result.errors,
        warnings=result.warnings,
        failed_tasks=result.failed_tasks,
        test_results=result.test_results,
        variant=result.variant,
        module=result.module,
        tasks=result.tasks,
        duration=result.duration,
    )

    if result.timed_out:
        timeout_variable = "ANDROID_EMU_TEST_TIMEOUT" if args.test else "ANDROID_EMU_BUILD_TIMEOUT"
        cli.fail(
            f"Gradle timed out after {result.timeout_seconds}s; the partial log is cached as "
            f"{build_id}",
            f"Raise {timeout_variable} or check the build for a stuck Gradle task; "
            f"read the partial log with `build_and_test.py --get-log {build_id}`.",
        )

    if result.rerun_unsupported:
        # --rerun (Gradle 7.6+) is what makes "this run's results" true; an
        # older Gradle rejects it before any test runs. Said plainly rather
        # than as a bare "Build: FAILED".
        cli.fail(
            "Gradle 7.6+ is required to run tests (the --rerun option); "
            "this project's wrapper is older",
            "Upgrade the wrapper (`./gradlew wrapper --gradle-version <7.6 or later>`), "
            f"or run {result.tasks[0]} yourself with ./gradlew; the Gradle log: "
            f"`build_and_test.py --get-log {build_id}`.",
        )

    if result.no_test_results:
        # Gradle exited 0 and this run wrote no test result. Reporting it the
        # usual way read "0 tests, 0 failed" -- a pass no test produced (N6).
        # Cached first, so the remedy can point at the log that says why.
        cli.fail(
            f"the build produced no test results: {result.tasks[0]} exited 0 "
            f"and wrote no JUnit XML with a test case under {result.results_dir}",
            "Check the module has unit tests under src/test (the log says NO-SOURCE "
            "when it has none) and that --suite matches one; read the Gradle log with "
            f"`build_and_test.py --get-log {build_id}`.",
        )

    _emit_result(args, result, build_id)
    return 0 if result.success else 1


if __name__ == "__main__":
    cli.run(main)
