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

Either shape counts **however the literal reaches the call**. That took a
second pass to get right: the first version compared only the expression
written at the call site, so ::

    JUNIT_XML = \"\"\"<testsuite ...>\"\"\"
    parse_junit_xml(JUNIT_XML)

was invisible -- the argument is an ``ast.Name``. Thirty-one call sites in five
files were sitting behind that one-line refactor, including both files this
module's comments called paid off. Names are now resolved against the
assignments in the same file (module level, and the enclosing test), and
wrapping calls, attribute access and containers are seen through, because what
the parser receives is the literal whatever it was passed through on the way.

What is deliberately NOT flagged: short strings (under 40 characters), which are
values rather than tool output; anything read from a fixture; and text built by
transforming recorded lines, which several tests do legitimately and document --
an expression reaching one of ``RECORDED_SOURCES`` is ground truth however much
it is then reshaped.
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

# Fixtures that hand back verbatim recorded output. An expression reaching one
# of these is ground truth however much it is then reshaped -- CLAUDE.md's
# documented exception for "text built by transforming recorded lines", which
# several tests do deliberately (substituting a frame count into a real
# Choreographer line, renaming a stack frame in a real crash).
RECORDED_SOURCES = frozenset({"recorded", "recorded_anywhere", "recorded_gradle", "any_profile"})

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
#   test_container.py, test_device_list.py, test_model_inspector.py,
#     test_app_state_capture.py — paid off when the detector stopped being
#     fooled by a hoisted constant (T1). Until then all four looked clean:
#     31 call sites bound the transcript to a name first, and an `ast.Name`
#     was invisible to the rule. What the recordings then said: an app's
#     databases/ holds a zero-byte `-journal`, not the `-wal`/`-shm` pair the
#     literal invented; `avdmanager list avd` indents its keys inconsistently
#     and hangs Tag/ABI off the `Based on:` continuation; and every Android
#     database carries android_metadata and sqlite_sequence, so a table count
#     taken from a hand-written schema is short by two.
#
# STILL OUTSTANDING — one line each, and what would retire it:
KNOWN_VIOLATIONS = frozenset(
    {
        # avdmanager create/delete output is not recorded on any profile yet.
        "test_emulator_create.py::CompletedProcess",
        # Partly paid by Inc -1: the shutdown-path tests read `adb devices -l`
        # from the corpus now. What still trips the detector is the f-string
        # feeding `emu avd name` output in the AVD-resolution test, which has
        # no recording yet.
        "test_emulator_shutdown.py::_FakeResult",
        # EXCEPTION, not debt: GPX is a document the USER hands to
        # `location.py --gpx` (a GPS device or mapping service writes it), so
        # there is no tool for a recorder to run, nothing in the Android SDK
        # emits it, and a real track is somebody's movements. The flagged line
        # is deliberately MALFORMED (`lat="north"`), which by definition
        # nothing produces. See the module docstring of test_location.py.
        "test_location.py::parse_gpx",
        # No recorded JUnit XML exists. Inc 0's recording PR captures one from
        # the scaffold whose @Before throws (with ignoreFailures = true) and
        # MUST delete this entry when it lands.
        "test_gradle.py::parse_junit_xml",
        # No AVD `config.ini` is recorded. Same PR should capture one; until
        # then the hand-written key/value block stands.
        "test_emulator_selector.py::parse_config_ini",
        # Hand-written `<hierarchy>` XML fed to screen_mapper through a mocked
        # dump. T2 repoints this file at the recorded uiautomator dumps and
        # deletes this entry.
        "test_screen_mapper.py::_fake_result",
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


def _reads_a_recording(
    node: ast.AST,
    scope: dict[str, ast.AST],
    _resolving: frozenset[str] = frozenset(),
) -> bool:
    """Whether this expression reaches a recorded fixture, however indirectly."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Name):
            continue
        if sub.id in RECORDED_SOURCES:
            return True
        if sub.id not in _resolving and sub.id in scope:
            if _reads_a_recording(scope[sub.id], scope, _resolving | {sub.id}):
                return True
    return False


def _is_tool_output_literal(
    node: ast.AST,
    scope: dict[str, ast.AST] | None = None,
    _resolving: frozenset[str] = frozenset(),
) -> bool:
    """Whether this expression *is* a hand-written transcript.

    ``scope`` maps names to the expression each was last bound to in the file
    being scanned, so the literal is still found after it has been hoisted:

        JUNIT_XML = \"\"\"<testsuite .../>\"\"\"   # module level, or a local
        parse_junit_xml(JUNIT_XML)             # <- the argument is a Name

    Without that lookup the ratchet saw an ``ast.Name`` and returned False, and
    a one-line refactor turned the whole rule off. Wrapping calls, attribute
    access and containers are seen through for the same reason: what a parser
    receives is the literal, whatever it was passed through on the way.
    """
    scope = scope or {}

    def recurse(child: ast.AST, resolving: frozenset[str] = _resolving) -> bool:
        return _is_tool_output_literal(child, scope, resolving)

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value) >= TOOL_OUTPUT_MIN_LENGTH
    if isinstance(node, ast.JoinedStr):
        # An f-string assembling output is still assembled output.
        return True
    if isinstance(node, ast.BinOp):
        # Concatenation, `%` formatting, `*` repetition: all still the literal.
        return recurse(node.left) or recurse(node.right)
    if isinstance(node, ast.Name):
        # The hoist. `_resolving` stops `x = x + "..."` recursing forever.
        if node.id in _resolving or node.id not in scope:
            return False
        return recurse(scope[node.id], _resolving | {node.id})
    if isinstance(node, ast.Attribute):
        # `PREFS_XML.strip()` -- the receiver is what matters.
        return recurse(node.value)
    if isinstance(node, ast.Call):
        # `ET.fromstring(HIERARCHY)`, `dedent(OUTPUT)`, `X.encode("utf-8")`:
        # a conversion in front of the parser is not a recording.
        if _reads_a_recording(node, scope, _resolving):
            # ...but `re.sub(pattern, replacement, recorded_line)` is. The
            # pattern is a literal and the subject is ground truth; flagging it
            # would forbid the one reshaping CLAUDE.md explicitly permits.
            return False
        return (
            recurse(node.func)
            or any(recurse(a) for a in node.args)
            or any(recurse(k.value) for k in node.keywords)
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(recurse(e) for e in node.elts)
    return False


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", None)


def _bindings(nodes: list[ast.AST]) -> dict[str, ast.AST]:
    """Name -> bound expression, for the assignments among ``nodes``."""
    bound: dict[str, ast.AST] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.value is not None:
                bound[node.target.id] = node.value
    return bound


def _walk_scope(node: ast.AST) -> list[ast.AST]:
    """Descendants of ``node``, not descending into nested def/class bodies.

    Nested functions get their own pass (with the enclosing scope handed down),
    so that a ``fake_run`` closure reading its test's local ``listing`` is
    resolved against that test, not against some other file-level name.
    """
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        out.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(current))
    return out


def _calls_with_scope(node: ast.AST, inherited: dict[str, ast.AST]):
    """Yield ``(call, scope)`` for every call in ``node``, scopes chained."""
    local = _walk_scope(node)
    scope = {**inherited, **_bindings(local)}
    for child in local:
        if isinstance(child, ast.Call):
            yield child, scope
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _calls_with_scope(child, scope)


def _scan_source(source: str, parsers: set[str]) -> set[str]:
    """Names of the calls in one module that are fed invented tool output."""
    found: set[str] = set()
    for node, scope in _calls_with_scope(ast.parse(source), {}):
        name = _called_name(node)

        # Shape 1: straight into a parser.
        if name in parsers and any(_is_tool_output_literal(a, scope) for a in node.args):
            found.add(name)

        # Shape 2: through a double's stdout.
        for keyword in node.keywords:
            if keyword.arg in OUTPUT_KEYWORDS and _is_tool_output_literal(keyword.value, scope):
                found.add(name or "?")

    return found


def _violations() -> set[str]:
    parsers = _parser_names()
    return {
        f"{path.name}::{name}"
        for path in sorted(TESTS.rglob("test_*.py"))
        for name in _scan_source(path.read_text(encoding="utf-8"), parsers)
    }


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


# --- Anti-vacuity: synthetic violations the detector must still catch -------
#
# Each case is a module the detector is pointed at directly, so "would this be
# caught?" is answered by running it rather than by reading the code. The
# parser name is taken from the real skill, so renaming that parser out of
# existence turns these red rather than leaving them quietly synthetic.


def _synthetic(body: str) -> set[str]:
    """Run the detector over a module written for this test."""
    parsers = _parser_names()
    parser = sorted(parsers)[0]
    return _scan_source(body.replace("PARSER", parser), parsers)


def _a_parser() -> str:
    return sorted(_parser_names())[0]


def test_a_literal_passed_straight_into_a_parser_is_caught():
    """The shape the ratchet was written for, still caught."""
    found = _synthetic(
        'PARSER("total 52\\n' 'drwx------ 5 u0_a205 u0_a205 4096 2026-08-31 21:44 .\\n")'
    )
    assert found == {_a_parser()}


def test_a_module_level_constant_does_not_hide_the_literal():
    """T1: the one-line refactor that used to switch the whole rule off.

    Binding the transcript to a name first made the argument an ``ast.Name``,
    which the detector did not resolve, so 31 call sites across five files
    asserted parser behaviour against invented output while the ratchet
    reported them as clean.
    """
    found = _synthetic(
        'SAMPLE = "total 52\\n'
        'drwx------ 5 u0_a205 u0_a205 4096 2026-08-31 21:44 .\\n"\n'
        "PARSER(SAMPLE)\n"
    )
    assert found == {_a_parser()}, "a hoisted constant still defeats the ratchet"


def test_a_function_local_binding_does_not_hide_the_literal():
    found = _synthetic(
        "def test_x():\n"
        '    sample = "total 52\\n'
        'drwx------ 5 u0_a205 u0_a205 4096 2026-08-31 21:44 .\\n"\n'
        "    PARSER(sample)\n"
    )
    assert found == {_a_parser()}


def test_a_constant_hidden_behind_a_conversion_is_caught():
    """`ET.fromstring(HIERARCHY)` is still the hand-written hierarchy."""
    found = _synthetic(
        'HIERARCHY = "<hierarchy rotation=\\"0\\"><node index=\\"0\\" '
        'class=\\"android.widget.FrameLayout\\" /></hierarchy>"\n'
        "PARSER(ET.fromstring(HIERARCHY))\n"
    )
    assert found == {_a_parser()}


def test_a_closure_reading_its_test_s_local_is_caught():
    """Shape 2 through a hoist: the mock is defined in a nested function."""
    found = _synthetic(
        "def test_x(monkeypatch):\n"
        '    listing = "total 52\\n'
        'drwx------ 5 u0_a205 u0_a205 4096 2026-08-31 21:44 .\\n"\n'
        "    def fake_run(cmd, **kwargs):\n"
        "        return _completed(cmd, stdout=listing)\n"
    )
    assert found == {"_completed"}


def test_a_recorded_fixture_is_not_flagged():
    """The negative control. Over-flagging would force fixtures into the debt list."""
    assert _synthetic('PARSER(recorded.text("run_as_ls_data_dir"))') == set()
    assert _synthetic("def test_x(recorded):\n    PARSER(recorded.text('x'))\n") == set()


def test_a_recorded_line_reshaped_in_the_test_is_not_flagged():
    """CLAUDE.md's documented exception, exercised rather than trusted.

    Substituting a frame count into a real Choreographer line keeps the line
    itself ground truth; the substitution pattern is a literal, and flagging on
    that would ban the one reshaping the policy allows.
    """
    found = _synthetic(
        "def test_x(recorded):\n"
        '    template = next(ln for ln in recorded.lines("logcat_choreographer_jank") if ln)\n'
        '    PARSER(re.sub(r"Skipped \\d+ frames", f"Skipped {n} frames", template))\n'
    )
    assert found == set()


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
