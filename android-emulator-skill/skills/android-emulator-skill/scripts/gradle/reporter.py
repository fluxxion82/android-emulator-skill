#!/usr/bin/env python3
"""
Build/test output formatting.

Provides ultra-minimal default output (one summary line + a result ID), a verbose
mode, and JSON. Mirrors the iOS ``xcode/reporter.py`` role. Caps are tunable via
the ANDROID_EMU_ prefix.
"""

import json

from common.env_config import env_int

BUILD_SUMMARY_CAP = env_int("ANDROID_EMU_BUILD_SUMMARY_CAP", 5)
BUILD_VERBOSE_CAP = env_int("ANDROID_EMU_BUILD_VERBOSE_CAP", 50)
BUILD_LOG_TAIL = env_int("ANDROID_EMU_BUILD_LOG_TAIL", 200)


class OutputFormatter:
    """Format Gradle build/test results across minimal/verbose/JSON modes."""

    @staticmethod
    def _duration(seconds: float) -> str:
        """Format a duration as M:SS."""
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def format_minimal(
        *,
        status: str,
        error_count: int,
        warning_count: int,
        build_id: str,
        duration: float,
        test_info: dict | None = None,
        errors: list[dict] | None = None,
        failed_tests: list[dict] | None = None,
    ) -> str:
        """
        Format the ultra-minimal default output: one summary line + a result ID.

        On failure, surfaces the top errors / failed tests inline so an agent
        doesn't need a second round-trip with ``--get-errors``.

        Args:
            status: "SUCCESS" or "FAILED"
            error_count: Number of errors
            warning_count: Number of warnings
            build_id: Cached result ID for progressive disclosure
            duration: Build duration in seconds
            test_info: Optional aggregated test result dict
            errors: Optional error list to surface on failure
            failed_tests: Optional failed-test list to surface on failure

        Returns:
            Minimal formatted string.
        """
        lines: list[str] = []
        dur = OutputFormatter._duration(duration)

        if test_info:
            total = test_info.get("total", 0)
            passed = test_info.get("passed", 0)
            failed = test_info.get("failed", 0)
            skipped = test_info.get("skipped", 0)
            # `errors` counts <error> testcases -- anything that threw, including
            # a crash in @Before/setUp. Omitting them from the verdict reported a
            # suite of 10 with 5 errors as "PASS (5/10 passed, 0 failed,
            # 0 skipped)": a pass claim, with numbers that do not sum.
            errored = test_info.get("errors", 0)
            not_passing = failed + errored
            test_status = "PASS" if not_passing == 0 and status != "FAILED" else "FAIL"
            counts = f"{passed}/{total} passed, {failed} failed"
            if errored:
                counts += f", {errored} errors"
            lines.append(f"Tests: {test_status} ({counts}, {skipped} skipped) [{build_id}]")
        else:
            lines.append(
                f"Build: {status} ({error_count} errors, {warning_count} warnings) "
                f"[{dur}] [{build_id}]"
            )

        if status == "FAILED" and errors:
            lines.append("")
            lines.append(OutputFormatter.format_errors(errors, limit=BUILD_SUMMARY_CAP))

        if failed_tests:
            lines.append("")
            lines.append(
                OutputFormatter.format_test_failures(failed_tests, limit=BUILD_SUMMARY_CAP)
            )

        return "\n".join(lines)

    @staticmethod
    def format_errors(errors: list[dict], limit: int = BUILD_VERBOSE_CAP) -> str:
        """Format a list of error dicts."""
        if not errors:
            return "No errors found."

        lines = [f"Errors ({len(errors)}):", ""]
        for i, error in enumerate(errors[:limit], 1):
            lines.append(f"{i}. {error.get('message', 'Unknown error')}")
            location = OutputFormatter._format_location(error.get("location"))
            if location:
                lines.append(f"   Location: {location}")
            lines.append("")

        if len(errors) > limit:
            lines.append(f"... and {len(errors) - limit} more errors")

        return "\n".join(lines).rstrip()

    @staticmethod
    def format_warnings(warnings: list[dict], limit: int = BUILD_VERBOSE_CAP) -> str:
        """Format a list of warning dicts."""
        if not warnings:
            return "No warnings found."

        lines = [f"Warnings ({len(warnings)}):", ""]
        for i, warning in enumerate(warnings[:limit], 1):
            lines.append(f"{i}. {warning.get('message', 'Unknown warning')}")
            location = OutputFormatter._format_location(warning.get("location"))
            if location:
                lines.append(f"   Location: {location}")
            lines.append("")

        if len(warnings) > limit:
            lines.append(f"... and {len(warnings) - limit} more warnings")

        return "\n".join(lines).rstrip()

    @staticmethod
    def _format_location(location: dict | None) -> str:
        """Render a {file, line, column} location dict as 'file:line:col'."""
        if not location:
            return ""
        parts = []
        if location.get("file"):
            parts.append(str(location["file"]))
        if location.get("line"):
            parts.append(str(location["line"]))
        if location.get("column"):
            parts.append(str(location["column"]))
        return ":".join(parts)

    @staticmethod
    def format_test_failures(failed_tests: list[dict], limit: int = BUILD_SUMMARY_CAP) -> str:
        """Format failed-test names + messages."""
        if not failed_tests:
            return "No test failures found."

        lines = [f"Failed tests ({len(failed_tests)}):", ""]
        for i, test in enumerate(failed_tests[:limit], 1):
            lines.append(f"{i}. {test.get('test_name', 'Unknown')}")
            message = test.get("failure_message", "")
            if message:
                lines.append(f"   {message}")
            lines.append("")

        if len(failed_tests) > limit:
            lines.append(f"... and {len(failed_tests) - limit} more failures")

        return "\n".join(lines).rstrip()

    @staticmethod
    def format_log(log: str, lines: int = BUILD_LOG_TAIL) -> str:
        """Format a build log, showing the last N lines."""
        if not log:
            return "No build log available."

        log_lines = log.strip().split("\n")
        if len(log_lines) <= lines:
            return log

        excerpt = log_lines[-lines:]
        return f"... (showing last {lines} lines of {len(log_lines)})\n\n" + "\n".join(excerpt)

    @staticmethod
    def format_verbose(
        *,
        status: str,
        error_count: int,
        warning_count: int,
        build_id: str,
        duration: float,
        errors: list[dict] | None = None,
        warnings: list[dict] | None = None,
        test_info: dict | None = None,
        failed_tests: list[dict] | None = None,
    ) -> str:
        """Format verbose output with full error/warning/test detail."""
        lines: list[str] = []

        if test_info:
            total = test_info.get("total", 0)
            passed = test_info.get("passed", 0)
            failed = test_info.get("failed", 0)
            skipped = test_info.get("skipped", 0)
            test_status = "PASS" if failed == 0 and status != "FAILED" else "FAIL"
            lines.append(f"Tests: {test_status}")
            lines.append(f"  Total:   {total}")
            lines.append(f"  Passed:  {passed}")
            lines.append(f"  Failed:  {failed}")
            lines.append(f"  Skipped: {skipped}")
        else:
            lines.append(f"Build: {status}")

        lines.append(f"Duration: {OutputFormatter._duration(duration)}")
        lines.append(f"Result ID: {build_id}")
        lines.append("")

        if errors:
            lines.append(OutputFormatter.format_errors(errors, limit=BUILD_VERBOSE_CAP))
            lines.append("")
        if warnings:
            lines.append(OutputFormatter.format_warnings(warnings, limit=BUILD_VERBOSE_CAP))
            lines.append("")
        if failed_tests:
            lines.append(
                OutputFormatter.format_test_failures(failed_tests, limit=BUILD_VERBOSE_CAP)
            )
            lines.append("")

        lines.append(f"Summary: {error_count} errors, {warning_count} warnings")
        return "\n".join(lines)

    @staticmethod
    def format_json(data: dict) -> str:
        """Pretty-print a dict as JSON."""
        return json.dumps(data, indent=2)
