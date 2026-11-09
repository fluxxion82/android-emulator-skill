#!/usr/bin/env python3
"""
Gradle result parsing.

Pure functions over text and paths — no subprocess, no gradlew invocation. These
parse two kinds of output:

1. JUnit XML test reports into pass/fail/skip counts plus failed-test names
   and messages. Where a run writes them is its *results root*
   (:class:`ResultsRoot`): ``build/test-results/<task>/TEST-*.xml`` for unit
   tests, ``build/outputs/androidTest-results/connected/...`` for instrumented
   ones.
2. Gradle build stdout/stderr into structured errors and warnings (Kotlin ``e:``
   / ``w:`` lines, Java/Kotlin ``error:`` / ``warning:`` lines, and
   ``> Task ... FAILED`` markers).

Mirrors the iOS ``xcode/xcresult.py`` intent (parse native build/test results)
but uses Gradle's JUnit XML + console output instead of Apple's xcresult bundle.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from common import cli


def _unit_candidates(result_root: Path, task: str) -> list[Path]:
    """``<task>/`` directly below ``build/test-results``, the name compared without case."""
    wanted = task.casefold()
    try:
        return sorted(
            path
            for path in result_root.iterdir()
            if path.is_dir() and path.name.casefold() == wanted
        )
    except FileNotFoundError:
        return []


def _subdirectories(directory: Path) -> list[Path]:
    try:
        return sorted(path for path in directory.iterdir() if path.is_dir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def connected_variant(task: str) -> str:
    """The variant a ``connected<Variant>AndroidTest`` task runs, as the task spells it.

    ``connectedProDebugAndroidTest`` -> ``ProDebug``. Compared without case.
    """
    return task.removeprefix("connected").removesuffix("AndroidTest")


def _instrumented_candidates(result_root: Path, task: str) -> list[Path]:
    """The variant's directory below ``build/outputs/androidTest-results/connected``.

    Two layouts, both recorded with AGP 8.13.2 on one emulator; nothing else
    is assumed:

    * an unflavored variant writes ``connected/<variant>/`` -- ``debug/``
      (instrumented_results_tree);
    * a flavored one writes ``connected/<buildType>/flavors/<flavor>/`` --
      ``proDebug`` into ``debug/flavors/pro/``
      (instrumented_flavored_results_tree).

    The variant is matched against what is on disk rather than split into
    flavor and build type, which would need the project's build types:
    ``<flavor><BuildType>`` is AGP's variant name, so ``<buildType>/flavors/
    <flavor>/`` is the variant's directory when the two read back as it,
    without case. A variant AGP writes anywhere else is not found, so its run
    is refused as one with no results -- reports are never read from a place
    that was not cleaned first.
    """
    wanted = connected_variant(task).casefold()
    matches = []
    for build_type in _subdirectories(result_root):
        if build_type.name.casefold() == wanted:
            matches.append(build_type)
        capitalized = f"{build_type.name[:1].upper()}{build_type.name[1:]}"
        matches.extend(
            flavor
            for flavor in _subdirectories(build_type / "flavors")
            if f"{flavor.name}{capitalized}".casefold() == wanted
        )
    return sorted(matches)


def _flavor_directories(directory: Path) -> list[Path]:
    """``<variant dir>/flavors/<flavor>/``: where AGP writes a flavored variant below it.

    For ``connected/debug/`` these are ``debug/flavors/free/`` and
    ``debug/flavors/pro/`` (instrumented_flavored_results_tree's layout): a
    ``debug`` run in a flavored project leaves its reports there, none of its
    own. They are cleared before the run and checked like the variant
    directory, so whatever is in them afterwards is this run's -- but they are
    the flavored variants' reports, never read as this one's verdict.
    """
    return _subdirectories(directory / "flavors")


def flavored_report_variants(reports: list[Path], variant: str) -> list[str]:
    """The flavored variants a run's reports below its variant directory belong to.

    ``connected/debug/flavors/<flavor>/TEST-*.xml`` written by a ``debug`` run
    means the project has product flavors and ``debug`` is their build type,
    not a variant of its own. Returned as the variants to pass instead --
    ``<flavor><Variant>`` in the requested variant's capitalisation
    (``freeDebug``, ``proDebug``). These reports are this run's (cleared
    before it), but they word a refusal; they never pass one.
    """
    capitalized = f"{variant[:1].upper()}{variant[1:]}"
    return sorted({f"{report.parent.name}{capitalized}" for report in reports})


@dataclass(frozen=True)
class ResultsRoot:
    """Where one kind of test run writes its JUnit XML, below a module directory.

    Every report the builder deletes or reads is a ``TEST-*.xml`` directly in
    a task directory ``<module>/<base...>/<task directory...>/`` -- nothing
    deeper (AGP's per-device UTP directory sits beside its report) -- or
    directly in one of that directory's ``nested`` directories, which are
    cleared, checked and counted as written but not read as the verdict.
    Discovery,
    the lexical symlink checks, the resolved-path containment checks, the
    stale-report deletion and the no-results diagnostic all go through this
    one description, so the two kinds cannot drift apart in any one of them.

    Attributes:
        kind: ``"unit"`` or ``"instrumented"``.
        base: Path components from a module to the directory holding its task
            directories.
        candidates: The task's directories in ``<module>/<base...>``, given
            that directory and the unqualified task name. More than one is
            ambiguous, and refused.
        placeholder: Path components below ``<module>/<base...>`` naming where
            a module-scoped search looks before Gradle has created the task's
            directory. Kept for the containment checks and the no-results
            diagnostic; nothing in it is read.
        label: How a refusal names this root's task directory, from the module.
        noun: The same directory, named without its path.
        nested: Report directories below a found task directory that belong to
            other tasks or variants the run may write -- cleared before it and
            checked like the task directory, never read as its result.
    """

    kind: str
    base: tuple[str, ...]
    candidates: Callable[[Path, str], list[Path]]
    placeholder: Callable[[str], tuple[str, ...]]
    label: str
    noun: str
    nested: Callable[[Path], list[Path]] = lambda _directory: []

    def pattern(self, search_root: Path, task: str) -> Path:
        """Where a project-wide search looked, for the no-results diagnostic."""
        return search_root.joinpath("**", *self.base, *self.placeholder(task))


UNIT_RESULTS = ResultsRoot(
    kind="unit",
    base=("build", "test-results"),
    candidates=_unit_candidates,
    placeholder=lambda task: (task,),
    label="build/test-results task directory",
    noun="test result task directory",
)

INSTRUMENTED_RESULTS = ResultsRoot(
    kind="instrumented",
    base=("build", "outputs", "androidTest-results", "connected"),
    candidates=_instrumented_candidates,
    # Which of the two layouts a variant has is not known until AGP writes it,
    # so the diagnostic names the directory both live under.
    placeholder=lambda _task: (),
    label="build/outputs/androidTest-results/connected variant directory",
    noun="connected test result variant directory",
    nested=_flavor_directories,
)


@dataclass(frozen=True)
class ResultDirectory:
    """A task directory a search resolved, and the module its root hangs from.

    Attributes:
        module: The module directory (lexical).
        path: The task directory (lexical, unresolved).
        found: False for a module-scoped search's placeholder -- nothing
            matched, and nothing in it is read.
        read: False for a root's ``nested`` directory -- cleared and checked,
            its reports counted as written, not read as the task's result.
    """

    module: Path
    path: Path
    found: bool = True
    read: bool = True

    @property
    def components(self) -> tuple[Path, ...]:
        """Every lexical component from ``<module>/build`` down to the task directory."""
        parts = self.path.relative_to(self.module).parts
        return tuple(self.module.joinpath(*parts[: index + 1]) for index in range(len(parts)))


def find_result_directories(
    search_root: Path,
    *,
    task: str,
    recursive: bool = True,
    root: ResultsRoot = UNIT_RESULTS,
) -> list[ResultDirectory]:
    """Resolve the one case-insensitive task directory below each eligible module.

    Gradle accepts a miscased task selector and writes reports under the task's
    declared spelling. One case-insensitive match is therefore usable, while
    two are ambiguous: choosing either would make cleanup delete a directory
    the caller did not uniquely identify.
    """
    if recursive:
        result_roots = sorted(set(search_root.glob("/".join(("**", *root.base)))))
    else:
        result_roots = [search_root.joinpath(*root.base)]

    directories: list[ResultDirectory] = []
    for result_root in result_roots:
        module = result_root.parents[len(root.base) - 1]
        matches = root.candidates(result_root, task)
        if len(matches) > 1:
            listed = ", ".join(str(path) for path in matches)
            if root is UNIT_RESULTS:
                cli.fail(
                    f"ambiguous Gradle test result directories for {task}: {listed}",
                    "Remove or rename the case-colliding task result directories, then retry.",
                )
            variant = connected_variant(task)
            variant = f"{variant[:1].lower()}{variant[1:]}"  # AGP's spelling: proDebug
            cli.fail(
                f"ambiguous Gradle test result directories for {task}: {len(matches)} result "
                f"directories read back as variant {variant}: {listed}",
                "Remove the one that is not this variant's (AGP writes connected/<variant>/ "
                "for an unflavored variant, connected/<buildType>/flavors/<flavor>/ for a "
                "flavored one), then retry.",
            )
        if matches:
            for path in matches:
                directories.append(ResultDirectory(module, path))
                directories.extend(
                    ResultDirectory(module, nested, read=False) for nested in root.nested(path)
                )
        elif not recursive:
            # Keep the intended location available for containment checks and
            # for the no-results diagnostic even before Gradle creates it.
            placeholder = result_root.joinpath(*root.placeholder(task))
            directories.append(ResultDirectory(module, placeholder, found=False))
    return directories


def find_test_result_directories(
    search_root: Path,
    *,
    task: str,
    recursive: bool = True,
    root: ResultsRoot = UNIT_RESULTS,
) -> list[Path]:
    """The task directories :func:`find_result_directories` resolves, a placeholder included."""
    found = find_result_directories(search_root, task=task, recursive=recursive, root=root)
    return [directory.path for directory in found if directory.read]


def find_test_result_files(
    search_root: Path,
    *,
    task: str | None = None,
    recursive: bool = True,
    root: ResultsRoot = UNIT_RESULTS,
) -> list[Path]:
    """
    Find Gradle's JUnit XML reports under a project or module directory.

    Gradle writes one ``TEST-<class>.xml`` per test class into
    ``<module>/build/test-results/<task>/`` (the unit root, the default). AGP
    writes one ``TEST-<device>-<module>-<flavor>.xml`` per device into the
    variant's directory under ``<module>/build/outputs/androidTest-results/
    connected/`` (``root=INSTRUMENTED_RESULTS``), beside a per-device
    directory of UTP output that holds no report. Only files named that way,
    directly in the task's directory, are returned: a bare ``*.xml`` glob
    also returned every other XML file at the top of ``search_root`` (D9).

    This only finds reports; it cannot tell whose they are. The directory
    outlives the run -- measured on Gradle 8.13, an UP-TO-DATE test task exits
    0 and leaves the previous run's reports untouched -- so a build that reads
    these after running a task clears them first (``BuildRunner``, N6).

    Args:
        search_root: Directory to search under (a project or a module).
        task: The test task, unqualified (``testDebugUnitTest``,
            ``connectedDebugAndroidTest``). Only reports directly in that
            task's directory are returned, the name compared without case:
            Gradle resolves a miscased task name (recorded:
            gradle_miscased_variant_resolves) and writes the directory under
            the task's real spelling.
        recursive: Search nested modules. A module-scoped build passes false so
            cleanup cannot cross into one of that module's children.
        root: The results root the task writes to.

    Returns:
        Sorted list of report paths (empty if none match or the path is missing).
    """
    if not search_root.exists():
        return []

    candidates: set[Path] = set()
    if task is not None:
        for directory in find_result_directories(
            search_root, task=task, recursive=recursive, root=root
        ):
            if directory.found and directory.read:
                candidates.update(directory.path.glob("TEST-*.xml"))
    elif root is UNIT_RESULTS:
        candidates.update(search_root.glob("**/test-results/**/TEST-*.xml"))
        # search_root may itself be a task's results directory.
        candidates.update(search_root.glob("TEST-*.xml"))
    else:
        candidates.update(search_root.glob("/".join(("**", *root.base, "**", "TEST-*.xml"))))

    return sorted(path for path in candidates if path.is_file())


def parse_junit_xml(xml_text: str, *, strict: bool = False) -> dict:
    """
    Parse a single JUnit XML document's text into counts and failed tests.

    Handles both a ``<testsuites>`` root (wrapping multiple ``<testsuite>``) and a
    single ``<testsuite>`` root. Counts are derived from the actual ``<testcase>``
    elements (robust against missing/incorrect summary attributes), and failed
    tests carry their classname-qualified name plus the failure message.

    Args:
        xml_text: Raw JUnit XML content
        strict: Raise ``ElementTree.ParseError`` for empty or malformed XML.
            Aggregation enables this because dropping one report can reverse a
            verdict; the standalone parser preserves its historical empty
            result for callers inspecting arbitrary text.

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
        if strict:
            raise ElementTree.ParseError("empty JUnit XML")
        return result

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        if strict:
            raise
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
        except OSError as error:
            cli.fail(
                f"could not read JUnit report {path}: {error}",
                "Check the report is readable and rerun the Gradle test task; "
                "a partial test aggregate cannot determine the verdict.",
            )
        try:
            parsed = parse_junit_xml(xml_text, strict=True)
        except ElementTree.ParseError as error:
            cli.fail(
                f"could not parse JUnit report {path}: {error}",
                "Rerun the Gradle test task so it writes a complete report; "
                "a partial test aggregate cannot determine the verdict.",
            )
        for key in ("total", "passed", "failed", "skipped", "errors"):
            aggregate[key] += parsed[key]
        aggregate["failed_tests"].extend(parsed["failed_tests"])

    return aggregate


# --- Build output parsing ---------------------------------------------------

# Kotlin compiler diagnostics: "e: file:line:col message" / "w: ...".
_KOTLIN_ERROR_RE = re.compile(r"^e:\s*(?P<body>.+)$")
_KOTLIN_WARNING_RE = re.compile(r"^w:\s*(?P<body>.+)$")

# Java/Kotlin/clang style: "/path/File.java:12: error: message".
#
# The file group must start with a NON-space character. Gradle reprints the
# whole compiler output, indented by two spaces, inside its "* What went wrong:"
# section — visible in gradle_javac_compile_error — and `[^:]+` matched that
# indent quite happily, so every javac diagnostic was counted twice over. (The
# recording shows javac itself also reporting one missing type twice, once per
# caret position; that duplication is real output and is left alone.)
_JAVAC_ERROR_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*\.\w+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*error:\s*(?P<message>.+)$"
)
_JAVAC_WARNING_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*\.\w+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*warning:\s*(?P<message>.+)$"
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


# The one `* Where:` form recorded (gradle_where_block, Gradle 8.13): a build
# script's evaluation failed at a line. Gradle prints the absolute path.
#     Build file '/scaffold/build.gradle' line: 11
# Other forms -- a settings file, an applied script plugin, an init script --
# are not recorded and are left unlocated rather than parsed from memory.
_WHERE_BUILD_FILE_RE = re.compile(r"^Build file '(?P<file>.+)' line: (?P<line>\d+)$")


# The lines that open and close a failure report (recorded in every failing
# log: gradle_where_block, gradle_task_not_found, ...).
_FAILURE_BOUNDARY_RE = re.compile(r"^(?:FAILURE:|BUILD FAILED)")


def _no_location() -> dict:
    return {"file": None, "line": None, "column": None}


def _where_location(body: list[str]) -> dict:
    """The location a ``* Where:`` block names, or nulls for a form not recorded."""
    for line in body:
        where = _WHERE_BUILD_FILE_RE.match(line)
        if where:
            return {"file": where.group("file"), "line": int(where.group("line")), "column": None}
    return _no_location()


def extract_failures(text: str) -> list[dict]:
    """Each Gradle failure report as ``{"message", "location"}``.

    ``message`` is the body of a ``* What went wrong:`` section,
    whitespace-collapsed. ``location`` is ``{file, line, column}`` from the
    ``* Where:`` section Gradle prints just above it, when there is one, and
    all nulls otherwise. A ``Where:`` belongs only to the ``What went wrong``
    that directly follows it in the same failure: any other section (``Try:``,
    ``Exception is:``) and a ``FAILURE:`` or ``BUILD FAILED`` line drop a
    location still pending, so it is never carried to the next failure.

    Args:
        text: Combined Gradle stdout/stderr.

    Returns:
        One dict per failure section. Empty if the build did not fail.
    """
    failures: list[dict] = []
    heading: str | None = None
    body: list[str] = []
    location = _no_location()

    def close() -> None:
        nonlocal location
        if heading == "where":
            location = _where_location(body)
            return
        if heading == "what went wrong" and body:
            failures.append({"message": " ".join(body).strip(), "location": location})
        location = _no_location()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _FAILURE_BOUNDARY_RE.match(line):
            # Between failure reports: nothing pending survives it.
            close()
            heading, body, location = None, [], _no_location()
            continue
        section = _FAILURE_SECTION_RE.match(line.strip())

        if section:
            # A new section ends whatever we were collecting.
            close()
            heading = section.group("heading").strip().lower()
            body = []
            continue

        if heading not in ("where", "what went wrong"):
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith(_FAILURE_NOISE_PREFIXES):
            continue
        body.append(stripped)

    close()
    return failures


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
    return [failure["message"] for failure in extract_failures(text)]


def _split_kotlin_body(body: str) -> dict:
    """Split a Kotlin diagnostic body into location + message.

    Three shapes, because the Kotlin compiler changed its diagnostic format at
    K2 and this parser only knew the old one:

    * ``file:///abs/path.kt:12:5 Message.`` — Kotlin 2.x, recorded as
      ``gradle_kotlin_compile_error``. A ``file://`` URI, colon-separated
      line and column, no parentheses and no colon before the message.
    * ``file: /path/File.kt: (12, 5): message`` — Kotlin 1.x.
    * ``/path/File.kt:12:5: message`` — the plain colon form.

    The K2 branch is checked first: its body starts ``file://``, which the
    1.x pattern's optional ``file:`` prefix would otherwise eat, leaving
    ``//abs/path.kt:12:5 …`` to fall through to no location at all. That was
    the live behaviour — every Kotlin error since 2.0 was reported with file,
    line and column all None.
    """
    location = {"file": None, "line": None, "column": None}

    k2_uri = re.match(
        r"^file://(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\s+(?P<message>.+)$",
        body,
    )
    if k2_uri:
        location["file"] = k2_uri.group("file").strip()
        location["line"] = int(k2_uri.group("line"))
        location["column"] = int(k2_uri.group("col"))
        return {"message": k2_uri.group("message").strip(), "location": location}

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
    for failure in extract_failures(combined):
        if any(failure["message"] in existing["message"] for existing in errors):
            continue
        errors.append({**failure, "type": "gradle"})

    return {"errors": errors, "warnings": warnings, "failed_tasks": failed_tasks}
