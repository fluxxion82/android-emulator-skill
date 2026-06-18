"""
Gradle build automation module.

Provides structured, modular access to the project's gradlew wrapper, JUnit XML /
console parsing, progressive-disclosure caching, learned project config, and
output formatting. Android counterpart of the iOS ``xcode/`` subpackage.
"""

from .builder import BuildResult, BuildRunner
from .cache import BuildResultCache
from .config import Config
from .reporter import OutputFormatter
from .results import (
    aggregate_test_results,
    find_test_result_files,
    parse_build_output,
    parse_junit_xml,
)

__all__ = [
    "BuildResult",
    "BuildResultCache",
    "BuildRunner",
    "Config",
    "OutputFormatter",
    "aggregate_test_results",
    "find_test_result_files",
    "parse_build_output",
    "parse_junit_xml",
]
