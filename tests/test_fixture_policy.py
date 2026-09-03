"""The fixture rule, enforced instead of merely written down.

CLAUDE.md says: *parser tests read `tests/fixtures/recorded/`; they never inline
tool output as a string literal.* That rule exists because this repo's defining
bug class is code and tests both written against imagined `adb` output -- when
the imagination is wrong they are wrong in the same direction, so the suite
stays green while the script does nothing. Three advertised capabilities
shipped inert that way behind 470 passing tests.

Until now the rule lived only in prose, and it was already being skipped: two
scripts written *after* the register named the problem test their parsers
against hand-written `ls -la` and `sqlite3 .schema` output, with no recorded
`run-as`, `sqlite3` or `shared_prefs` fixture anywhere in the corpus. A rule
that lives only in a document holds until the first tired evening.

So this is a **ratchet**, modelled on `test_no_unbounded_subprocess.py`: the
violations that exist today are listed by name, and no new one may appear. The
list is meant to shrink. Each entry is debt, not permission.

Two shapes are detected, because the second is how the rule actually gets
sidestepped:

1. A long string literal passed straight into a parser function.
2. A long string literal handed to a test double as ``stdout=`` -- the parser
   is reached through a mocked ``subprocess``, so the literal never appears as
   a parser argument, but it is still invented tool output.

What is deliberately NOT flagged: short strings (under 40 characters), which are
values rather than tool output; anything read from a fixture; and text built by
transforming recorded lines, which several tests do legitimately and document.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"
TESTS = Path(__file__).resolve().parent

# A literal shorter than this is a value ("emulator-5554", "true"), not a
# transcript of what a tool printed.
TOOL_OUTPUT_MIN_LENGTH = 40

# Keyword arguments that hand fabricated output to a test double.
OUTPUT_KEYWORDS = ("stdout", "stderr", "output")

# --- The debt ---------------------------------------------------------------
#
# Every entry is a test asserting a parser's behaviour against output nobody
# recorded. They are frozen here so no NEW one can appear; the fix for any of
# them is to record the real thing with `python tests/record_fixtures.py` and
# read it through the `recorded` fixture.
#
# PAID OFF: test_container.py and test_model_inspector.py were the two that
# mattered most -- the original sin recurring in scripts written after the
# register named it. Both now read recorded `run-as` output, and recording it
# immediately found two real defects: a denial marker written as "is not an
# application" where the device says "package not an application", in BOTH
# files, so a non-debuggable app got a generic failure from one and
# "app.db is not a valid SQLite database" from the other.
#
# PAID OFF SINCE, and every one of them cost the production code a fix:
#
#   test_anr_pipeline.py, test_anr_watcher.py — a real ANR now exists to read,
#     provoked by tests/fixtures/scaffold/compose's AnrReceiver. Android writes
#     "ANR in com.example.app" with NO parenthesised component, reports the same
#     stall a SECOND time as "ANR in Window{...}" (whose package the parser read
#     as the literal word "Window"), and logs a StrictMode violation as a header
#     plus a stack trace all under one tag (one violation had been arriving as
#     thirty events, at a constant duration, while the real one was stated in
#     the line).
#   test_gradle.py — Kotlin K2 prints `e: file:///abs/path.kt:5:13 Message.`,
#     not Kotlin 1.x's `e: file: /path.kt: (5, 13): message`, so every Kotlin
#     diagnostic since Kotlin 2.0 had parsed to no file, line or column at all;
#     and Gradle's indented reprint of the compiler output inside
#     "* What went wrong:" was being counted as more javac errors.
#   test_push_notification.py — `cmd notification post` was driven into every
#     failure it has and exited 0 every time, so the non-zero-exit case the test
#     asserted does not exist; the real rejection is text at a successful exit,
#     and only the usage wording was being checked for.
#   test_status_bar.py — `adb -s <unknown> shell` prefixes its message `adb:`,
#     not the `error:` the literal claimed (that is `adb get-state`'s prefix).
#
# STILL OUTSTANDING:
#
#   test_emulator_create.py — not yet recorded.
#
#   test_emulator_shutdown.py — partly paid: the shutdown-path tests now read
#     `adb devices -l` from the corpus. What still trips the detector is the
#     f-string feeding `emu avd name` output in the AVD-resolution test, which
#     has no recording yet.
#
#   test_location.py::parse_gpx — a documented EXCEPTION, in the same class as
#     test_emu_console.py, and not expected to be paid off. GPX is not tool
#     output: it is a document the USER hands to `location.py --gpx`, produced
#     by a GPS device or a mapping service, so there is no tool for a recorder
#     to run and look at. Nothing in the Android SDK emits GPX, and a real track
#     is somebody's movements, which must not be committed to a public
#     repository. The specific flagged line is a deliberately MALFORMED document
#     (`lat="north"`), which by definition nothing produces. See the module
#     docstring of test_location.py.
KNOWN_VIOLATIONS = frozenset(
    {
        "test_emulator_create.py::CompletedProcess",
        "test_emulator_shutdown.py::_FakeResult",
        "test_location.py::parse_gpx",
    }
)

# `_build_parser` is argparse, not a tool-output parser.
NOT_A_TOOL_PARSER = {"_build_parser"}


def _parser_names() -> set[str]:
    """Functions in the skill that turn tool output into data."""
    names: set[str] = set()
    for path in SCRIPTS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in NOT_A_TOOL_PARSER:
                continue
            if "parse" in node.name or node.name.startswith("scan_"):
                names.add(node.name)
    return names


def _is_tool_output_literal(node: ast.AST) -> bool:
    """A string literal long enough to be a transcript rather than a value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value) >= TOOL_OUTPUT_MIN_LENGTH
    if isinstance(node, ast.JoinedStr):
        # An f-string assembling output is still assembled output.
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_tool_output_literal(node.left) or _is_tool_output_literal(node.right)
    return False


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", None)


def _violations() -> set[str]:
    parsers = _parser_names()
    found: set[str] = set()

    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)

            # Shape 1: straight into a parser.
            if name in parsers and any(_is_tool_output_literal(a) for a in node.args):
                found.add(f"{path.name}::{name}")

            # Shape 2: through a double's stdout.
            for keyword in node.keywords:
                if keyword.arg in OUTPUT_KEYWORDS and _is_tool_output_literal(keyword.value):
                    found.add(f"{path.name}::{name or '?'}")

    return found


def test_no_new_test_invents_tool_output():
    """The ratchet. Record the real output instead of writing what it might say."""
    new = _violations() - KNOWN_VIOLATIONS
    assert not new, (
        f"These tests assert a parser's behaviour against invented tool output: "
        f"{sorted(new)}.\n"
        f"That is the bug class this repo exists to correct -- when the guess is "
        f"wrong, the code and the test are wrong together and the suite stays "
        f"green.\n"
        f"Record the real thing:\n"
        f"  python tests/record_fixtures.py --list\n"
        f"  python tests/record_fixtures.py --only <name>\n"
        f"then read it with the `recorded` fixture. If the input genuinely cannot "
        f"be recorded (see tests/test_emu_console.py, where the recorder "
        f"normalises away the CRLF the test needs), document why in the test and "
        f"add it to KNOWN_VIOLATIONS."
    )


def test_the_debt_list_does_not_rot():
    """A stale exemption hides the fact that the debt was already paid.

    If an entry no longer corresponds to a real violation, someone fixed it and
    left the exemption behind -- which would silently re-permit that file.
    """
    stale = KNOWN_VIOLATIONS - _violations()
    assert not stale, (
        f"{sorted(stale)} are listed as known violations but no longer violate "
        f"anything. Delete them from KNOWN_VIOLATIONS -- leaving them there "
        f"re-opens the door they were holding."
    )


def test_the_detector_actually_detects():
    """Guard against the guard being vacuous.

    A detector that matches nothing would make both tests above pass forever.
    """
    assert _parser_names(), "no parser functions found; the detector matches nothing"
    assert _violations(), (
        "the detector found zero violations, which contradicts KNOWN_VIOLATIONS; "
        "it has probably stopped working"
    )


def test_recorded_fixtures_are_actually_consumed():
    """A fixture nothing reads is not evidence, it is a file.

    Recording output and then not asserting against it gives the appearance of
    the discipline without its effect.
    """
    corpus = REPO_ROOT / "tests" / "fixtures" / "recorded"
    names = {
        path.stem
        for profile in corpus.iterdir()
        if profile.is_dir()
        for path in profile.iterdir()
        if path.name != "MANIFEST.json"
    }
    assert names, "no recorded fixtures found"

    test_source = "\n".join(path.read_text(encoding="utf-8") for path in TESTS.rglob("test_*.py"))
    unused = sorted(name for name in names if name not in test_source)

    # Not an error: some fixtures are recorded as evidence for a documented
    # claim rather than as parser input. Reported so the ratio stays visible.
    assert len(unused) <= len(names) // 2, (
        f"{len(unused)} of {len(names)} recorded fixtures are referenced by no "
        f"test: {unused}. The corpus is drifting toward decoration."
    )
