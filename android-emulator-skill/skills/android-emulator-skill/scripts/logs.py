#!/usr/bin/env python3
"""One entry point for reading an Android device's logs.

Four scripts in this skill read logcat, and an agent scanning SKILL.md had to
choose between ``log_monitor``, ``anr_watcher``, ``crash_triage`` and
``app_state_capture --logs`` on the strength of their names. Those names are
near-synonyms; the questions behind them are not. This script puts the question
first:

    logs.py tail      What did it print?   (main buffers, live or historical)
    logs.py crashes   Did it crash?        (the dedicated ``-b crash`` buffer)
    logs.py anr       Did it hang?         (ANR / dropped frames, one-shot or
                                            as a recorded session)

Three verbs instead of four names, and the verb is the question, so picking the
wrong one is much harder than picking the wrong noun.


Why a router and not a merge
----------------------------

A single merged log reader was considered and rejected, because two of the four
are not variations on "read the log":

- ``crash_triage`` reads a **different buffer**. ``adb logcat -b crash -d``
  hands back traces already isolated by the platform; the main-buffer reader
  would have to grep for ``FATAL EXCEPTION`` and guess which surrounding lines
  belong to the same crash. It also has its own exit-status contract
  (``--fail-on-crash`` → 2) that a shared reader has no use for.
- ``anr_watcher`` has a **session lifecycle**. ``--start`` detaches a worker
  that keeps streaming while the agent drives the app, and ``--stop`` clusters
  what it saw and fits the summary to a token budget. Sessions live on disk,
  restart a dead stream, and can be diffed against each other. Collapsing that
  into a one-shot flag would lose the only mode that answers "did anything hang
  *while I was doing that*".

So the modules stay the implementation and this file routes to them. The
alternative shape -- move the code here and leave the four as shims -- would
have meant re-deriving every flag of three mature CLIs, which is exactly where a
"non-breaking" consolidation quietly breaks something. Here, the delegated
argument vector is passed through untouched, so each verb's behaviour, output
and exit status are the same objects that were already under test.

``app_state_capture`` gets no verb: it is not a log reader but a debugging
bundle (screenshot + UI hierarchy + dumpsys + logs), and a ``logs.py snapshot``
would misdescribe it. What it *was* -- a fourth private implementation of "ask
adb for a time window of logcat" -- is gone: all four now build their commands
through ``common.logcat``, so there is one place that knows the argv shape and
one duration grammar.


Compatibility
-------------

Nothing here replaces anything. ``log_monitor.py``, ``anr_watcher.py``,
``crash_triage.py`` and ``app_state_capture.py`` remain executable with exactly
the flags they always had -- this script *calls* them. An existing
``log_monitor.py --duration 3s --json`` keeps working, and so does the identical
``logs.py tail --duration 3s --json``.

Usage:
    python3 scripts/logs.py tail --app com.myapp --duration 30s
    python3 scripts/logs.py tail --serial emulator-5554 --severity error --last 5m
    python3 scripts/logs.py crashes --package com.myapp --json
    python3 scripts/logs.py anr --since 5m
    python3 scripts/logs.py anr --start --package com.myapp
    python3 scripts/logs.py <verb> --help      # the verb's full flag set
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Resolve imports whether run from the repo root or from scripts/.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)


@dataclass(frozen=True)
class Route:
    """One verb, and the module that already implements it.

    Attributes:
        name: The verb an agent types.
        question: The question this verb answers, in the user's words. This is
            the field that does the work: the routing table is a
            question-to-tool map, not a list of near-synonymous nouns.
        module: Importable module name, imported lazily on dispatch.
        script: The standalone script that still ships the same CLI.
        source: Which log source it reads, so "why not one command" is
            answerable from ``--help`` alone.
        example: A representative invocation.
    """

    name: str
    question: str
    module: str
    script: str
    source: str
    example: str


ROUTES: tuple[Route, ...] = (
    Route(
        name="tail",
        question="What did it print?",
        module="log_monitor",
        script="log_monitor.py",
        source="main logcat buffers (-v threadtime), live or a historical window",
        example="logs.py tail --app com.myapp --duration 30s",
    ),
    Route(
        name="crashes",
        question="Did it crash?",
        module="crash_triage",
        script="crash_triage.py",
        source="the dedicated crash buffer (logcat -b crash -d)",
        example="logs.py crashes --package com.myapp --verbose",
    ),
    Route(
        name="anr",
        question="Did it hang or drop frames?",
        module="anr_watcher",
        script="anr_watcher.py",
        source="ActivityManager ANRs + Choreographer jank, one-shot or as a session",
        example="logs.py anr --start --package com.myapp",
    ),
)

ROUTES_BY_NAME = {route.name: route for route in ROUTES}


def _normalise_exit(code: object) -> int:
    """Turn whatever a delegate's ``main()`` produced into a process exit status.

    The three delegates end differently -- ``crash_triage.main()`` returns an
    int, the other two call ``sys.exit()`` -- and ``SystemExit`` also carries a
    string when a CLI exits with a message. All three shapes have to become one
    integer, or the router would report success for a failed run.

    Args:
        code: A return value, or a ``SystemExit.code``.

    Returns:
        The exit status. A string code is printed and becomes 1, matching what
        the interpreter itself does with ``sys.exit("message")``.
    """
    if code is None or code is True:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def dispatch(route: Route, args: list[str]) -> int:
    """Run one verb by handing its arguments to the module that implements it.

    ``args`` is passed through verbatim -- not re-parsed, not filtered -- so the
    delegate sees exactly the command line it would have seen when invoked as a
    standalone script. That is what makes this consolidation non-breaking: there
    is no second copy of the flag set here to drift out of step.

    ``sys.argv[0]`` is set to ``"logs.py <verb>"`` so the delegate's own
    ``--help`` and error messages name the command the agent actually typed.

    Args:
        route: The selected route.
        args: Everything after the verb, including ``--help``.

    Returns:
        The delegate's exit status.
    """
    module = importlib.import_module(route.module)
    saved_argv = sys.argv
    sys.argv = [f"logs.py {route.name}", *args]
    try:
        return _normalise_exit(module.main())
    except SystemExit as exit_request:
        return _normalise_exit(exit_request.code)
    finally:
        sys.argv = saved_argv


def routes_json() -> dict:
    """The routing table, machine-readable.

    Returns:
        A dict naming each verb, the question it answers, its log source, and
        the standalone script that still ships the same CLI.
    """
    return {
        "entry_point": "logs.py",
        "routes": [
            {
                "verb": route.name,
                "question": route.question,
                "source": route.source,
                "delegates_to": route.script,
                "example": route.example,
            }
            for route in ROUTES
        ],
    }


def _guide() -> str:
    """The routing table as text, for ``--help`` and for an unknown verb."""
    lines = ["Verbs (the question comes first):"]
    width = max(len(route.name) for route in ROUTES)
    for route in ROUTES:
        lines.append(f"  {route.name:<{width}}  {route.question:<28} {route.source}")
    lines.append("")
    lines.append("Each verb takes the full flag set of the script it delegates to:")
    for route in ROUTES:
        invocation = f"logs.py {route.name} --help"
        lines.append(f"  {invocation:<{width + 21}} -> {route.script}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    """Parser for the router's own flags only; verbs are dispatched before this."""
    return argparse.ArgumentParser(
        prog="logs.py",
        description=(
            "Read an Android device's logs. One entry point over the skill's "
            "log readers, dispatching on the question you are asking."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="logs.py <verb> [options]   |   logs.py --help",
        epilog=f"""{_guide()}

Examples:
  python3 scripts/logs.py tail --app com.myapp --duration 30s
  python3 scripts/logs.py tail --serial emulator-5554 --severity error --last 5m
  python3 scripts/logs.py crashes --package com.myapp --json
  python3 scripts/logs.py anr --since 5m --json
  python3 scripts/logs.py anr --start --package com.myapp   # record a session

Nothing is replaced: the delegated scripts remain callable with the same flags.
`--serial`, `--json` and `--verbose` belong to the verbs, which accept them
exactly as they always did; this router adds only `--json` (the table above,
machine-readable).
        """,
    )


def main(argv: list[str] | None = None) -> int:
    """Route to a verb, or explain the verbs.

    Args:
        argv: Argument list without the program name. Defaults to ``sys.argv``.

    Returns:
        The delegate's exit status, or 2 for a usage error (argparse's
        convention for a missing or unknown argument).
    """
    args = list(sys.argv[1:] if argv is None else argv)

    # Dispatch before argparse: everything after the verb belongs to the
    # delegate, including flags this parser also defines (`--json`, `--help`).
    if args and args[0] in ROUTES_BY_NAME:
        return dispatch(ROUTES_BY_NAME[args[0]], args[1:])

    parser = _build_parser()
    parser.add_argument(
        "--json", action="store_true", help="Emit the routing table as JSON and exit"
    )
    parser.add_argument(
        "verb",
        nargs="?",
        choices=[route.name for route in ROUTES],
        help=argparse.SUPPRESS,
    )

    # An unknown verb: argparse would say "invalid choice", which does not help
    # an agent that guessed a synonym. Name the nearest verb instead.
    if args and not args[0].startswith("-"):
        suggestion = difflib.get_close_matches(args[0], ROUTES_BY_NAME, n=1, cutoff=0.4)
        hint = f"\nDid you mean: logs.py {suggestion[0]}" if suggestion else ""
        print(f"Unknown verb {args[0]!r}.\n\n{_guide()}{hint}", file=sys.stderr)
        return 2

    parsed = parser.parse_args(args)
    if parsed.json:
        print(json.dumps(routes_json(), indent=2))
        return 0

    # Bare `logs.py`: a missing required argument, so exit 2 like argparse does,
    # but print the table rather than a bare usage line.
    parser.print_usage(sys.stderr)
    print(f"\n{_guide()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
