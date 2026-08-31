#!/usr/bin/env python3
"""
Gradle result parsing.

Pure functions over text and paths — no subprocess, no gradlew invocation. These
parse two kinds of output:

1. JUnit XML test reports (``build/test-results/**/*.xml``) into pass/fail/skip
   counts plus failed-test names and messages.
2. Gradle build stdout/stderr into structured errors and warnings (Kotlin ``e:``
   / ``w:`` lines, Java/Kotlin ``error:`` / ``warning:`` lines, and
   ``> Task ... FAILED`` markers).

Mirrors the iOS ``xcode/xcresult.py`` intent (parse native build/test results)
but uses Gradle's JUnit XML + console output instead of Apple's xcresult bundle.
"""

import re
from pathlib import Path
from xml.etree import ElementTree


def find_test_result_files(search_root: Path) -> list[Path]:
    """
    Find JUnit XML test result files under a project or module directory.

    Globs the standard Gradle layout ``**/build/test-results/**/*.xml`` so it
    works whether ``search_root`` is the project root, a single module, or a
    test-results directory itself.

    Args:
        search_root: Directory to search under

    Returns:
        Sorted list of XML file paths (empty if none found or path missing)
    """
    if not search_root.exists():
        return []

    candidates: set[Path] = set()

    # Standard Gradle test report locations.
    candidates.update(search_root.glob("**/build/test-results/**/*.xml"))
    # Allow pointing directly at a test-results directory.
    candidates.update(search_root.glob("**/test-results/**/*.xml"))
    # Allow pointing directly at a directory full of TEST-*.xml files.
    candidates.update(search_root.glob("TEST-*.xml"))
    candidates.update(search_root.glob("*.xml"))

    # Keep only JUnit-style report files (TEST-*.xml or anything with testsuite).
    return sorted(p for p in candidates if p.is_file())


def parse_junit_xml(xml_text: str) -> dict:
    """
    Parse a single JUnit XML document's text into counts and failed tests.

    Handles both a ``<testsuites>`` root (wrapping multiple ``<testsuite>``) and a
    single ``<testsuite>`` root. Counts are derived from the actual ``<testcase>``
    elements (robust against missing/incorrect summary attributes), and failed
    tests carry their classname-qualified name plus the failure message.

    Args:
        xml_text: Raw JUnit XML content

    Returns:
        Dict with keys: total, passed, failed, skipped, errors,
        failed_tests (list of {test_name, failure_message}).
    """
    result = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "failed_tests": [],
    }

    if not xml_text or not xml_text.strip():
        return result

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return result

    # Collect all testsuite elements regardless of root shape.
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        suites = root.findall(".//testsuite")

    for suite in suites:
        for case in suite.findall("testcase"):
            result["total"] += 1

            name = case.get("name", "unknown")
            classname = case.get("classname", "")
            qualified = f"{classname}.{name}" if classname else name

            failure_el = case.find("failure")
            error_el = case.find("error")
            skipped_el = case.find("skipped")

            if failure_el is not None or error_el is not None:
                bad_el = failure_el if failure_el is not None else error_el
                if error_el is not None and failure_el is None:
                    result["errors"] += 1
                else:
                    result["failed"] += 1
                result["failed_tests"].append(
                    {
                        "test_name": qualified,
                        "failure_message": _failure_message(bad_el),
                    }
                )
            elif skipped_el is not None:
                result["skipped"] += 1
            else:
                result["passed"] += 1

    return result


def _failure_message(element: ElementTree.Element) -> str:
    """Extract a concise failure message from a <failure>/<error> element."""
    message = element.get("message")
    if message:
        return message.strip()
    text = (element.text or "").strip()
    if text:
        # First non-empty line is usually the assertion message.
        return text.splitlines()[0].strip()
    return ""


def aggregate_test_results(xml_files: list[Path]) -> dict:
    """
    Parse and merge multiple JUnit XML files into aggregate test results.

    Args:
        xml_files: List of JUnit XML file paths

    Returns:
        Merged result dict (same shape as parse_junit_xml).
    """
    aggregate = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "failed_tests": [],
    }

    for path in xml_files:
        try:
            xml_text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_junit_xml(xml_text)
        for key in ("total", "passed", "failed", "skipped", "errors"):
            aggregate[key] += parsed[key]
        aggregate["failed_tests"].extend(parsed["failed_tests"])

    return aggregate


# --- Build output parsing ---------------------------------------------------

# Kotlin compiler diagnostics: "e: file:line:col message" / "w: ...".
_KOTLIN_ERROR_RE = re.compile(r"^e:\s*(?P<body>.+)$")
_KOTLIN_WARNING_RE = re.compile(r"^w:\s*(?P<body>.+)$")

# Java/Kotlin/clang style: "/path/File.java:12: error: message".
_JAVAC_ERROR_RE = re.compile(
    r"^(?P<file>[^:]+\.\w+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*error:\s*(?P<message>.+)$"
)
_JAVAC_WARNING_RE = re.compile(
    r"^(?P<file>[^:]+\.\w+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*warning:\s*(?P<message>.+)$"
)

# Bare "error:" / "warning:" diagnostics without a leading file path.
_BARE_ERROR_RE = re.compile(r"^\s*error:\s*(?P<message>.+)$", re.IGNORECASE)
_BARE_WARNING_RE = re.compile(r"^\s*warning:\s*(?P<message>.+)$", re.IGNORECASE)

# Gradle task failure markers: "> Task :app:compileDebugKotlin FAILED".
_TASK_FAILED_RE = re.compile(r"^>\s*Task\s+(?P<task>\S+)\s+FAILED\s*$")

# Gradle "What went wrong" / "FAILURE: Build failed" sections.
_BUILD_FAILED_RE = re.compile(r"^(?:FAILURE:\s*)?Build failed", re.IGNORECASE)

# Section headers inside a Gradle failure report: "* What went wrong:", "* Try:".
_FAILURE_SECTION_RE = re.compile(r"^\*\s*(?P<heading>.+?):\s*$")

# Boilerplate that follows every failure and carries no diagnostic value.
_FAILURE_NOISE_PREFIXES = ("> Run ", "> Get more help", "> For more on ", "> Run with ")


def extract_failure_reasons(text: str) -> list[str]:
    """Pull the body of each ``* What went wrong:`` section from Gradle output.

    Gradle reports configuration failures, dependency-resolution failures and
    task-lookup failures through this block **only** — such builds emit no
    ``> Task ... FAILED`` marker and no compiler ``e:`` lines. Without this,
    those failures parse to zero errors and the CLI prints
    ``Build: FAILED (0 errors, 0 warnings)`` with nothing actionable.

    Args:
        text: Combined Gradle stdout/stderr.

    Returns:
        One string per failure section, whitespace-collapsed. Empty if the
        build did not fail.

    Example:
        >>> extract_failure_reasons("FAILURE: Build failed\\n\\n* What went wrong:\\nBoom\\n")
        ['Boom']
    """
    reasons: list[str] = []
    collecting = False
    body: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        section = _FAILURE_SECTION_RE.match(line.strip())

        if section:
            # A new section ends whatever we were collecting.
            if collecting and body:
                reasons.append(" ".join(body).strip())
            body = []
            collecting = section.group("heading").strip().lower() == "what went wrong"
            continue

        if not collecting:
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith(_FAILURE_NOISE_PREFIXES):
            continue
        body.append(stripped)

    if collecting and body:
        reasons.append(" ".join(body).strip())

    return reasons


def _split_kotlin_body(body: str) -> dict:
    """Split a Kotlin diagnostic body into location + message."""
    location = {"file": None, "line": None, "column": None}
    # Format: "file: /path/File.kt: (12, 5): message" OR "/path:12:5: message".
    file_paren = re.match(
        r"^(?:file:\s*)?(?P<file>.+?):\s*\((?P<line>\d+),\s*(?P<col>\d+)\):\s*(?P<message>.+)$",
        body,
    )
    if file_paren:
        location["file"] = file_paren.group("file").strip()
        location["line"] = int(file_paren.group("line"))
        location["column"] = int(file_paren.group("col"))
        return {"message": file_paren.group("message").strip(), "location": location}

    colon_form = re.match(
        r"^(?P<file>[^:]+\.\w+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<message>.+)$", body
    )
    if colon_form:
        location["file"] = colon_form.group("file").strip()
        location["line"] = int(colon_form.group("line"))
        if colon_form.group("col"):
            location["column"] = int(colon_form.group("col"))
        return {"message": colon_form.group("message").strip(), "location": location}

    return {"message": body.strip(), "location": location}


def parse_build_output(stdout: str, stderr: str) -> dict:
    """
    Parse Gradle build stdout/stderr into structured errors and warnings.

    Recognises Kotlin (``e:``/``w:``), Java/Kotlin file-prefixed
    ``error:``/``warning:`` lines, bare ``error:``/``warning:`` diagnostics, and
    Gradle ``> Task ... FAILED`` markers (recorded as ``type: "task"`` errors).

    Args:
        stdout: Gradle standard output
        stderr: Gradle standard error

    Returns:
        Dict with keys: errors (list), warnings (list), failed_tasks (list of str).
        Each error/warning dict has message, type, and location {file, line, column}.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    failed_tasks: list[str] = []

    combined = "\n".join(part for part in (stdout, stderr) if part)

    for raw_line in combined.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        task_match = _TASK_FAILED_RE.match(line.strip())
        if task_match:
            task = task_match.group("task")
            failed_tasks.append(task)
            errors.append(
                {
                    "message": f"Task {task} FAILED",
                    "type": "task",
                    "location": {"file": None, "line": None, "column": None},
                }
            )
            continue

        kotlin_err = _KOTLIN_ERROR_RE.match(line)
        if kotlin_err:
            parsed = _split_kotlin_body(kotlin_err.group("body"))
            errors.append({**parsed, "type": "kotlin"})
            continue

        kotlin_warn = _KOTLIN_WARNING_RE.match(line)
        if kotlin_warn:
            parsed = _split_kotlin_body(kotlin_warn.group("body"))
            warnings.append({**parsed, "type": "kotlin"})
            continue

        javac_err = _JAVAC_ERROR_RE.match(line)
        if javac_err:
            errors.append(
                {
                    "message": javac_err.group("message").strip(),
                    "type": "javac",
                    "location": {
                        "file": javac_err.group("file").strip(),
                        "line": int(javac_err.group("line")),
                        "column": int(javac_err.group("col")) if javac_err.group("col") else None,
                    },
                }
            )
            continue

        javac_warn = _JAVAC_WARNING_RE.match(line)
        if javac_warn:
            warnings.append(
                {
                    "message": javac_warn.group("message").strip(),
                    "type": "javac",
                    "location": {
                        "file": javac_warn.group("file").strip(),
                        "line": int(javac_warn.group("line")),
                        "column": (
                            int(javac_warn.group("col")) if javac_warn.group("col") else None
                        ),
                    },
                }
            )
            continue

        bare_err = _BARE_ERROR_RE.match(line)
        if bare_err:
            errors.append(
                {
                    "message": bare_err.group("message").strip(),
                    "type": "build",
                    "location": {"file": None, "line": None, "column": None},
                }
            )
            continue

        bare_warn = _BARE_WARNING_RE.match(line)
        if bare_warn:
            warnings.append(
                {
                    "message": bare_warn.group("message").strip(),
                    "type": "build",
                    "location": {"file": None, "line": None, "column": None},
                }
            )
            continue

    # Gradle's own failure report. Kept last so a compile error that already
    # explains the failure stays first in the list, and deduplicated against it
    # so a task-level failure is not reported twice.
    for reason in extract_failure_reasons(combined):
        if any(reason in existing["message"] for existing in errors):
            continue
        errors.append(
            {
                "message": reason,
                "type": "gradle",
                "location": {"file": None, "line": None, "column": None},
            }
        )

    return {"errors": errors, "warnings": warnings, "failed_tasks": failed_tasks}
