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

# Ceiling for a literal sitting BESIDE a recording inside a transformation --
# a substitution pattern, a replacement or an index. The legitimate cases are
# small: a fully-qualified symbol name being substituted into a real crash
# ("com.example.composefixture.SyncService.onHandleWork", 51 characters) and
# an f-string replacement like f"Skipped {n} frames". A 200-character single
# line is a transcript wherever it sits, so the exemption stops here.
DERIVATION_ARG_MAX = 120

# `"-" * 500` is a literal too. Repetitions are folded so their real length is
# measured rather than the one-character operand's, and capped so a pathological
# count cannot allocate: any cap above both thresholds gives the same verdict.
STATIC_REPEAT_CAP = 10_000

# Keyword arguments that hand fabricated output to a test double.
OUTPUT_KEYWORDS = ("stdout", "stderr", "output")

# Fixtures that hand back verbatim recorded output. An expression reaching one
# of these is ground truth however much it is then reshaped -- CLAUDE.md's
# documented exception for "text built by transforming recorded lines", which
# several tests do deliberately (substituting a frame count into a real
# Choreographer line, renaming a stack frame in a real crash).
RECORDED_SOURCES = frozenset({"recorded", "recorded_anywhere", "recorded_gradle", "any_profile"})

# Substitution helpers whose SUBJECT -- the text being reshaped -- is the third
# positional argument rather than the receiver.
RE_SUBSTITUTIONS = frozenset({"sub", "subn"})

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
#     PARTLY: test_container.py and test_device_list.py each keep ONE literal,
#     for platform behaviour the parsers document and no recording covers --
#     see their entries below. Everything else in both files reads recordings.
#
#   test_screen_mapper.py, test_accessibility_audit.py, test_hierarchy.py,
#     test_compose_visibility.py — paid off when parser detection stopped
#     depending on the function's NAME (T2). `analyze_tree` and `_audit_node`
#     contain neither "parse" nor "scan_", so the two files testing "see the
#     screen" were outside the policy: four hand-written `<hierarchy>` blocks
#     in one, and a single fifteen-line imagined dump in the other, which was
#     shaped to make the checks it was written beside pass. Recording it found
#     L3 (see the xfail in test_accessibility_audit.py).
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
        # A symlink line, a filename containing a space, and the coreutils
        # month/day/year layout -- all documented by parse_ls_output, none in
        # any recording. Record `run-as ... ls -la` in a directory containing
        # a symlink and a spaced filename.
        "test_container.py::parse_ls_output",
        # `offline` and `unauthorized` device states, which parse_adb_devices
        # reports and no recording has. Record `adb devices -l` with a device
        # in each state (a second emulator killed mid-boot gives `offline`).
        "test_device_list.py::parse_adb_devices",
    }
)

# `_build_parser` is argparse, not a tool-output parser.
NOT_A_TOOL_PARSER = {"_build_parser"}

# Annotations that say "this takes a uiautomator hierarchy" -- but only when
# they came from xml.etree. `navigator.py` defines its own `Element` dataclass,
# and `tap(element: Element)` is a tap, not a parser, so the name alone is not
# enough: provenance is resolved per module from its imports.
HIERARCHY_ANNOTATIONS = frozenset({"Element", "ElementTree"})
XML_ETREE_MODULES = frozenset({"xml.etree.ElementTree", "xml.etree"})

# The dict shape `get_ui_hierarchy()` returns (CLAUDE.md's hierarchy contract):
# {"tag": str, "attributes": {...}, "children": [...]}. A function reading or
# building it consumes a dumped screen, whatever it is called. "tag" alone is
# too generic to key on; the other two are not.
HIERARCHY_KEYS = frozenset({"attributes", "children"})
HIERARCHY_SHAPE = frozenset({"tag", "attributes", "children"})


def _xml_element_names(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """(module aliases, bare names) through which this file reaches ET.Element."""
    aliases: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in XML_ETREE_MODULES:
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module not in XML_ETREE_MODULES:
                continue
            for alias in node.names:
                if alias.name in HIERARCHY_ANNOTATIONS:
                    bare.add(alias.asname or alias.name)
                if alias.name == "ElementTree":
                    aliases.add(alias.asname or alias.name)
    return frozenset(aliases), frozenset(bare)


def _takes_a_hierarchy(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: frozenset[str],
    bare: frozenset[str],
) -> bool:
    """Whether any parameter is annotated as an *xml.etree* element."""
    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if arg is None or arg.annotation is None:
            continue
        for sub in ast.walk(arg.annotation):
            if isinstance(sub, ast.Name) and sub.id in bare:
                return True
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr in HIERARCHY_ANNOTATIONS
                and isinstance(sub.value, ast.Name)
                and sub.value.id in aliases
            ):
                return True
    return False


def _handles_hierarchy_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the body reads or builds the documented hierarchy-node shape."""
    for sub in ast.walk(func):
        # node.get("attributes") / node.get("children")
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and sub.args[0].value in HIERARCHY_KEYS
        ):
            return True
        # node["attributes"] / node["children"]
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.slice, ast.Constant)
            and sub.slice.value in HIERARCHY_KEYS
        ):
            return True
        # {"tag": ..., "attributes": ..., "children": []} -- building one.
        if isinstance(sub, ast.Dict):
            keys = {k.value for k in sub.keys if isinstance(k, ast.Constant)}
            if len(keys & HIERARCHY_SHAPE) >= 2:
                return True
    return False


def _passes_a_parameter_to(func: ast.FunctionDef | ast.AsyncFunctionDef, known: set[str]) -> bool:
    """Whether ``func`` hands one of its own parameters to a known parser.

    `accessibility_audit.audit_tree(hierarchy: dict)` touches no key itself --
    it calls `_audit_node(hierarchy)`. A wrapper around a hierarchy parser is
    still a hierarchy consumer, and a test feeding it a literal is still
    inventing a screen.
    """
    args = func.args
    parameters = {
        arg.arg
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if arg is not None
    }
    for sub in ast.walk(func):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Attribute):
            callee = sub.func.attr
        else:
            callee = getattr(sub.func, "id", None)
        if callee not in known or callee == func.name:
            continue
        for argument in [*sub.args, *(k.value for k in sub.keywords)]:
            if isinstance(argument, ast.Name) and argument.id in parameters:
                return True
    return False


def _parser_names() -> set[str]:
    """Functions in the skill that turn tool output into data.

    Naming alone missed the two that matter most (T2). `screen_mapper`'s
    `analyze_tree` and `accessibility_audit`'s `_audit_node` are the whole of
    "see the screen", and neither contains "parse" nor starts with "scan_", so
    the files testing them were outside the policy and both fed the audit a
    hand-written 15-line dump nobody recorded. Detection is therefore
    structural as well: a function that takes an ``ET.Element``, or that reads
    the ``attributes`` key `get_ui_hierarchy()` nests UI fields under, is a
    parser regardless of what it is called.
    """
    names: set[str] = set()
    hierarchy: set[str] = set()
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    for path in SCRIPTS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases, bare = _xml_element_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in NOT_A_TOOL_PARSER:
                continue
            functions.append(node)
            if _takes_a_hierarchy(node, aliases, bare) or _handles_hierarchy_nodes(node):
                hierarchy.add(node.name)
            elif "parse" in node.name or node.name.startswith("scan_"):
                names.add(node.name)

    # Closure over the HIERARCHY set only: a function handing one of its own
    # parameters to a hierarchy parser is one too. Deliberately not applied to
    # the name-matched set -- `grant_permission(package, ...)` passes its
    # parameter to `parse_package_permissions`, and a package name is an
    # argument, not a screen.
    while True:
        grown = {
            node.name
            for node in functions
            if node.name not in hierarchy and _passes_a_parameter_to(node, hierarchy)
        }
        if not grown:
            return names | hierarchy
        hierarchy |= grown


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


def _recorded_subject(
    call: ast.Call,
    scope: dict[str, ast.AST],
    resolving: frozenset[str],
    helpers: frozenset[str],
) -> ast.AST | None:
    """The ground-truth argument of an allowlisted transformation, or None.

    Only two shapes qualify, and in both the recording must be the SUBJECT --
    the thing being reshaped -- not merely somewhere in the argument list:

      * a method call on a recorded receiver: ``recorded.text(...)``,
        ``<recorded>.replace(a, b)``, ``<recorded>.encode("utf-8")``;
      * ``re.sub`` / ``re.subn``, whose subject is the third positional
        argument (or ``string=``);
      * a derivation helper defined in the same test file, called with a
        recording among its arguments -- ``_repeat_of`` and ``_rename_frame``
        in test_crash_triage.py, the substitutions its module docstring
        describes.

    Anything else -- an arbitrary ``wrapper(<invented blob>, recorded.text(...))``
    -- is not a transformation of the recording, and its other arguments are
    scanned normally. Exempting a whole call because a recording appeared
    ANYWHERE inside it is how the first version of this rule let a 200-character
    invented transcript through untouched.
    """
    if isinstance(call.func, ast.Attribute):
        if _reads_a_recording(call.func.value, scope, resolving):
            return call.func.value
        if call.func.attr in RE_SUBSTITUTIONS:
            if len(call.args) >= 3:
                subject = call.args[2]
            else:
                subject = next((k.value for k in call.keywords if k.arg == "string"), None)
            if subject is not None and _reads_a_recording(subject, scope, resolving):
                return subject
        return None

    # A derivation helper defined in the same test file, over a recording:
    # `_rename_frame(recorded_crash, 0, "com.example.App.render")`. The file
    # has to define it -- an unknown `wrapper(...)` is not a transformation,
    # it is a call that happens to have a recording somewhere in it.
    if isinstance(call.func, ast.Name) and call.func.id in helpers:
        arguments = [*call.args, *(k.value for k in call.keywords)]
        return next((a for a in arguments if _reads_a_recording(a, scope, resolving)), None)
    return None


def _static_string(
    node: ast.AST, scope: dict[str, ast.AST], resolving: frozenset[str] = frozenset()
) -> str | None:
    """The string this expression evaluates to, when that is knowable statically.

    Constants, concatenations, repetitions and names bound to any of those.
    Folding them means the LENGTH being judged is the length the parser would
    receive: `"x" * 200` is a 200-character transcript, not the one-character
    operand the recursive rule would otherwise see on each side.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if node.id in resolving or node.id not in scope:
            return None
        return _static_string(scope[node.id], scope, resolving | {node.id})
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = _static_string(node.left, scope, resolving)
            right = _static_string(node.right, scope, resolving)
            return None if left is None or right is None else left + right
        if isinstance(node.op, ast.Mult):
            for text_node, count_node in ((node.left, node.right), (node.right, node.left)):
                text = _static_string(text_node, scope, resolving)
                if text is None or not isinstance(count_node, ast.Constant):
                    continue
                count = count_node.value
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    return text * min(count, STATIC_REPEAT_CAP)
    return None


def _judge_literal(text: str, beside_a_recording: bool) -> bool:
    """Whether a known string is a transcript somebody made up."""
    if len(text) < TOOL_OUTPUT_MIN_LENGTH:
        return False
    if not beside_a_recording:
        return True
    # Bounded exemption: beside a recording, a literal is a substitution
    # pattern or value only while it stays on one line AND stays short.
    return "\n" in text or len(text) >= DERIVATION_ARG_MAX


def _is_tool_output_literal(
    node: ast.AST,
    scope: dict[str, ast.AST] | None = None,
    _resolving: frozenset[str] = frozenset(),
    _helpers: frozenset[str] = frozenset(),
    _beside_a_recording: bool = False,
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

    ``_helpers`` names the functions the scanned file defines, so a derivation
    helper over a recording can be told from an arbitrary call.

    ``_beside_a_recording`` is True only for the non-subject arguments of an
    allowlisted transformation, where a literal is a substitution pattern or
    value rather than a transcript. There, only a MULTI-LINE literal counts:
    `re.sub(r"Skipped \\d+ frames", f"Skipped {n} frames", recorded_line)` and
    `_rename_frame(recorded_crash, 0, "com.example.App.render")` are the
    documented derivations, and both would otherwise be flagged for a pattern
    or a symbol name.
    """
    scope = scope or {}

    def recurse(
        child: ast.AST,
        resolving: frozenset[str] = _resolving,
        beside_a_recording: bool = _beside_a_recording,
    ) -> bool:
        return _is_tool_output_literal(child, scope, resolving, _helpers, beside_a_recording)

    folded = _static_string(node, scope, _resolving)
    if folded is not None:
        return _judge_literal(folded, _beside_a_recording)
    if isinstance(node, ast.JoinedStr):
        # An f-string assembling output is still assembled output.
        return any(recurse(value) for value in node.values) if _beside_a_recording else True
    if isinstance(node, ast.FormattedValue):
        return recurse(node.value)
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
        subject = _recorded_subject(node, scope, _resolving, _helpers)

        parts: list[ast.AST] = []
        if isinstance(node.func, ast.Attribute):
            if node.func.value is not subject:
                parts.append(node.func.value)
        else:
            parts.append(node.func)
        parts += [a for a in node.args if a is not subject]
        parts += [k.value for k in node.keywords if k.value is not subject]

        # Inside a transformation the recording is ground truth and the pattern
        # and replacement are not transcripts -- but a long literal sitting
        # beside them still is.
        return any(recurse(part, beside_a_recording=subject is not None) for part in parts)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(recurse(element) for element in node.elts)
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
    tree = ast.parse(source)
    helpers = frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    found: set[str] = set()
    for node, scope in _calls_with_scope(tree, {}):
        name = _called_name(node)

        # Shape 1: straight into a parser.
        if name in parsers and any(
            _is_tool_output_literal(a, scope, frozenset(), helpers) for a in node.args
        ):
            found.add(name)

        # Shape 2: through a double's stdout.
        for keyword in node.keywords:
            if keyword.arg in OUTPUT_KEYWORDS and _is_tool_output_literal(
                keyword.value, scope, frozenset(), helpers
            ):
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


# Every function in the skill that consumes a dumped screen. Named rather than
# implied, so the coverage cannot regress silently when the detection rules are
# next edited. The first two are T2 -- `analyze_tree` and `_audit_node` are the
# whole of "see the screen" and matched neither name rule; the rest were found
# afterwards, when only `attributes` was keyed on and a wrapper that delegates
# was invisible.
HIERARCHY_CONSUMERS = (
    "analyze_tree",  # screen_mapper
    "_audit_node",  # accessibility_audit
    "_descendants",  # accessibility_audit, walks "children"
    "audit_tree",  # accessibility_audit, delegates to _audit_node
    "count_ui_elements",  # app_state_capture, walks "children"
    "_xml_to_dict",  # common/device_utils, builds the node shape
)

# Not parsers, however they are annotated: navigator defines its own `Element`
# dataclass, and tapping one is not reading a screen.
NOT_HIERARCHY_CONSUMERS = ("tap", "enter_text")


def test_every_hierarchy_consumer_is_in_policy():
    """T2 and its follow-up, named rather than implied."""
    parsers = _parser_names()
    missing = [name for name in HIERARCHY_CONSUMERS if name not in parsers]
    assert not missing, (
        f"{missing} consume a UI hierarchy but are not detected as parsers, so "
        f"tests feeding them hand-written screens are outside the fixture policy"
    )


def test_navigators_own_element_type_is_not_mistaken_for_xml():
    """Provenance, not the bare name.

    `navigator.py` has `@dataclass class Element` and `def tap(element: Element)`.
    Matching on the annotation's name alone made every tap a parser, which is
    both wrong and the kind of slack that makes an explicit-membership test
    look green for the wrong reason.
    """
    parsers = _parser_names()
    wrong = [name for name in NOT_HIERARCHY_CONSUMERS if name in parsers]
    assert not wrong, f"{wrong} are typed with navigator's own Element, not xml.etree's"


def test_only_xml_etree_element_annotations_count():
    tree = ast.parse(
        "import xml.etree.ElementTree as ET\n" "def reads(root: ET.Element):\n" "    return root\n"
    )
    aliases, bare = _xml_element_names(tree)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert [f.name for f in functions if _takes_a_hierarchy(f, aliases, bare)] == ["reads"]

    local = ast.parse(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Element:\n"
        "    text: str\n"
        "def tap(element: Element):\n"
        "    return element\n"
    )
    aliases, bare = _xml_element_names(local)
    functions = [n for n in ast.walk(local) if isinstance(n, ast.FunctionDef)]
    assert [f.name for f in functions if _takes_a_hierarchy(f, aliases, bare)] == []


def test_a_wrapper_that_delegates_to_a_hierarchy_parser_is_one_too():
    """`audit_tree(hierarchy)` touches no key itself; it calls `_audit_node`."""
    tree = ast.parse(
        "def _audit_node(node):\n"
        '    return node.get("attributes", {})\n'
        "def audit_tree(hierarchy):\n"
        "    return _audit_node(hierarchy)\n"
        "def unrelated(value):\n"
        "    return _audit_node({})\n"
    )
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    seeded = {f.name for f in functions if _handles_hierarchy_nodes(f)}
    assert seeded == {"_audit_node"}

    grown = {f.name for f in functions if _passes_a_parameter_to(f, seeded)}
    assert grown == {"audit_tree"}, "a delegating wrapper is still a hierarchy consumer"


def test_a_function_taking_an_element_is_a_parser():
    """Structural rule 1: the annotation says it consumes a dumped screen."""
    tree = ast.parse(
        "import xml.etree.ElementTree as ET\n"
        "from xml.etree.ElementTree import Element\n"
        "def render(root: ET.Element) -> dict:\n"
        "    return {}\n"
        "def render_maybe(node: Element | None):\n"
        "    return None\n"
    )
    aliases, bare = _xml_element_names(tree)
    found = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _takes_a_hierarchy(node, aliases, bare)
    }
    assert found == {"render", "render_maybe"}


def test_a_function_handling_the_node_shape_is_a_parser():
    """Structural rule 2: `_audit_node` is annotated `dict`, not `ET.Element`.

    What identifies it is the hierarchy contract it touches. Keying on
    `attributes` alone missed `_descendants` and `count_ui_elements` (which
    walk `children`) and `_xml_to_dict` (which BUILDS the shape rather than
    reading it), so all three are covered.
    """
    tree = ast.parse(
        "def audit(node: dict):\n"
        '    attrs = node.get("attributes", {})\n'
        "    return attrs\n"
        "def subscripted(node):\n"
        '    return node["attributes"]\n'
        "def walker(node):\n"
        '    return node.get("children", [])\n'
        "def builder(element):\n"
        '    return {"tag": element.tag, "attributes": {}, "children": []}\n'
        "def unrelated(node):\n"
        '    return node.get("serial", "")\n'
    )
    found = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _handles_hierarchy_nodes(node)
    }
    assert found == {"audit", "subscripted", "walker", "builder"}


def test_a_hand_written_hierarchy_fed_to_a_hierarchy_parser_is_caught():
    """The T2 violation itself, end to end."""
    found = _scan_source(
        'HIERARCHY = "<hierarchy rotation=\\"0\\"><node index=\\"0\\" '
        'class=\\"android.widget.EditText\\" password=\\"true\\" /></hierarchy>"\n'
        "analyze_tree(ET.fromstring(HIERARCHY))\n",
        _parser_names(),
    )
    assert found == {"analyze_tree"}


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


# A transcript nobody recorded, in the shape a fabricated `adb devices -l`
# actually has: a header and repeated device lines.
INVENTED_TRANSCRIPT = "List of devices attached\n" + (
    "emulator-5554          device product:sdk_gphone16k_arm64 model:sdk\n" * 3
)


def test_a_recording_elsewhere_in_the_call_does_not_launder_the_literal():
    """F1: the first version of the exemption short-circuited the whole call.

    `_reads_a_recording` was asked about the call node, so ANY argument being
    a recording excused every other argument -- and Codex demonstrated it:
    `parse(wrapper(<200 invented chars>, recorded.text("adb_devices_single")))`
    scanned clean. `wrapper` is not a transformation of that recording; the
    recording merely sits beside the invention.
    """
    found = _synthetic(f"PARSER(wrapper({INVENTED_TRANSCRIPT!r}, recorded.text('x')))")
    assert found == {_a_parser()}, "a recorded sibling still launders an invented literal"


def test_a_single_line_invented_blob_is_caught_too():
    """The same probe without newlines: length alone is the rule outside a
    transformation, so nothing here turns on the blob being multi-line."""
    found = _synthetic(f"PARSER(wrapper({'x y ' * 50!r}, recorded.text('x')))")
    assert found == {_a_parser()}


def test_a_local_derivation_helper_over_a_recording_is_not_flagged():
    """test_crash_triage's documented substitutions must stay clean.

    `_rename_frame(recorded_crash, 0, "com.example.App.render")` reshapes a real
    crash; the symbol is a value being substituted IN, not a transcript. The
    file has to define the helper -- that is what separates it from `wrapper`.
    """
    found = _synthetic(
        "def _rename_frame(text, index, symbol):\n"
        "    return text\n"
        "def test_x(recorded):\n"
        "    crash = recorded.text('logcat_crash_java')\n"
        "    PARSER(_rename_frame(crash, 0, 'com.example.composefixture.HomeScreen.render'))\n"
    )
    assert found == set()


def test_a_derivation_helper_does_not_launder_a_transcript_beside_it():
    """The exemption covers substitution values, not a second invented dump."""
    found = _synthetic(
        "def _rename_frame(text, index, symbol):\n"
        "    return text\n"
        "def test_x(recorded):\n"
        "    crash = recorded.text('logcat_crash_java')\n"
        f"    PARSER(_rename_frame(crash, 0, {INVENTED_TRANSCRIPT!r}))\n"
    )
    assert found == {_a_parser()}


# The transformation exemption is bounded at both ends, and both ends are
# pinned. Newlines are not the test -- a 200-character single line is a
# transcript wherever it sits -- and length is not the whole test either, or
# the substitutions the corpus documents would become debt.
A_SYMBOL_NAME = "com.example.composefixture.SyncService.onHandleWork"
A_LONG_SINGLE_LINE = "x" * 200


def test_a_long_single_line_beside_a_recording_is_still_a_transcript():
    """The residual hole after the first narrowing: length was not bounded.

    Exempting every single-line argument of a transformation let a
    200-character blob through, in both shapes the allowlist admits.
    """
    via_helper = _synthetic(
        "def _derive(blob, text):\n"
        "    return text\n"
        "def test_x(recorded):\n"
        f"    PARSER(_derive({A_LONG_SINGLE_LINE!r}, recorded.text('adb_devices_single')))\n"
    )
    assert via_helper == {_a_parser()}, "a helper laundered a 200-character single line"

    via_re_sub = _synthetic(
        "def test_x(recorded):\n"
        f"    PARSER(re.sub('a', {A_LONG_SINGLE_LINE!r}, recorded.text('adb_devices_single')))\n"
    )
    assert via_re_sub == {_a_parser()}, "re.sub's replacement laundered a 200-character line"


def test_a_short_single_line_replacement_beside_a_recording_is_not():
    """The other end: the substitutions the corpus documents stay clean.

    A fully-qualified symbol name is 51 characters -- over the transcript
    threshold, under the derivation ceiling -- and is a value being substituted
    INTO recorded output, not output.
    """
    assert TOOL_OUTPUT_MIN_LENGTH < len(A_SYMBOL_NAME) < DERIVATION_ARG_MAX

    via_re_sub = _synthetic(
        "def test_x(recorded):\n"
        f"    PARSER(re.sub('a', {A_SYMBOL_NAME!r}, recorded.text('logcat_crash_java')))\n"
    )
    assert via_re_sub == set()

    via_helper = _synthetic(
        "def _rename_frame(text, index, symbol):\n"
        "    return text\n"
        "def test_x(recorded):\n"
        "    crash = recorded.text('logcat_crash_java')\n"
        f"    PARSER(_rename_frame(crash, 0, {A_SYMBOL_NAME!r}))\n"
    )
    assert via_helper == set()


def test_a_repetition_is_measured_at_its_real_length():
    """`"x" * 200` is a 200-character literal, not a one-character one.

    The recursive rule judged each operand of a BinOp separately, so a
    repetition read as a single character. Statically-known strings are folded
    before the length rule runs.
    """
    assert _synthetic(f"PARSER({A_LONG_SINGLE_LINE!r})") == {_a_parser()}
    assert _synthetic("PARSER('x' * 200)") == {_a_parser()}
    assert _synthetic("BLOB = 'x' * 200\nPARSER(BLOB)\n") == {_a_parser()}


def test_the_documented_derivations_are_not_debt():
    """test_crash_triage and test_anr_watcher reshape recorded lines by design.

    Named here because the bound above is what keeps them out: their
    substitution arguments are symbol names and frame counts, and a rule
    tightened without a ceiling would have turned the corpus's own documented
    practice into two more KNOWN_VIOLATIONS entries.
    """
    offenders = {entry.split("::")[0] for entry in _violations()}
    assert "test_crash_triage.py" not in offenders
    assert "test_anr_watcher.py" not in offenders


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
