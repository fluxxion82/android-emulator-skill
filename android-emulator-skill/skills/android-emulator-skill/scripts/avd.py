#!/usr/bin/env python3
"""One entry point for the emulator/AVD lifecycle.

Seven scripts in this skill make, start, stop, wipe, remove, list and rank
Android virtual devices, and an agent scanning SKILL.md had to choose among
``emulator_create``, ``emulator_boot``, ``emulator_shutdown``,
``emulator_erase``, ``emulator_delete``, ``emulator_selector`` and
``device_list`` on the strength of their names. Two of those names --
``erase`` and ``delete`` -- read as synonyms in English for operations that are
not synonyms at all: one throws away the AVD's *data* and keeps the AVD, the
other throws away *the AVD*. Guessing wrong there is unrecoverable.

So this script puts the question first, and says what each answer costs:

    avd.py list     What is attached, and what is defined?
    avd.py pick     Which one should I use?
    avd.py create   Make one that does not exist yet.
    avd.py start    Start one.
    avd.py stop     Stop one.
    avd.py reset    Throw away its data, keep the AVD.       (destructive)
    avd.py delete   Throw away the AVD.                      (destructive)

The verbs are chosen so that the pair that can destroy something is the pair
that reads most differently: ``reset`` restores a device you keep, ``delete``
removes the device. ``erase``, which is what the underlying script is called,
is deliberately *not* a verb here -- it is the word that sits exactly between
the two meanings, and it was the name doing the damage.


Why a router and not a merge
----------------------------

A single ``avd.py`` owning the whole lifecycle was considered and rejected. The
seven do not share an implementation to unify; they share a *noun*, and merging
on a shared noun is how a "non-breaking" consolidation quietly breaks something:

- Four of them already disagree about what "the AVDs" even means, and the
  disagreement is real rather than theoretical. ``emulator_boot`` and
  ``emulator_selector`` (and ``device_list``) ask ``emulator -list-avds``;
  ``emulator_delete`` asks ``avdmanager list avd -c``; ``emulator_erase`` scans
  ``$ANDROID_AVD_HOME`` for ``*.avd`` directories. On a host where the SDK's
  ``emulator`` directory is on ``PATH`` but its ``emulator`` binary is not,
  three of those return nothing and one returns the truth. Picking a winner is
  a defect fix with its own evidence to gather, not a side effect of adding an
  entry point -- so this file routes, and the divergence stays visible rather
  than being averaged away.
- ``emulator_create`` talks to ``avdmanager``/``sdkmanager``, not to adb at
  all, and its three parsers were inert until an hour ago (19325db). Re-deriving
  them here would be a second copy of the code that has already been wrong once.

So the modules stay the implementation and this file routes to them. The
delegated argument vector is passed through untouched, so each verb's
behaviour, output and exit status are the same objects that were already under
test.


What is deliberately NOT merged
-------------------------------

- **``reset`` and ``delete`` stay two verbs.** They are both destructive, so the
  tempting shape is one verb with a ``--level``. That would put the
  unrecoverable case one typo away from the recoverable one. Note also that the
  two disagree about their confirmation flag -- ``delete`` takes ``--yes``,
  ``reset`` takes ``--force`` -- and this router does not paper over that,
  because normalising it would mean owning a second copy of both flag sets.
- **``list`` and ``pick`` stay two verbs.** ``device_list`` answers "what is
  attached and what is defined", from ``adb devices -l`` plus the AVD listing;
  ``emulator_selector`` answers "which of them should I use", by reading every
  AVD's ``config.ini`` and a recency history. The second is strictly more work,
  and the first is the common case.
- **``emulator_selector --boot`` is left in place** even though ``avd.py start``
  exists. It is not a duplicate implementation: the selector already delegates
  to ``EmulatorBooter``. It is the "pick one and go" path, and removing it would
  break a published flag for no gain.
- **``snapshot.py`` gets no verb.** It is emulator state management, but it is a
  different question -- "save and restore where this *running* device is" --
  addressed to the emulator console, not to an AVD on disk. An
  ``avd.py snapshot`` would file it under lifecycle, which is where an agent
  would then fail to find it when it wanted to bookmark a login.
- **No ``common/avd.py`` is extracted.** ``logs.py`` could pull four private
  copies of "ask adb for a window of logcat" into one because all four wanted
  the same bytes. Here the four AVD listings want different bytes from different
  tools; unifying them requires deciding which is right, which requires evidence
  this repo does not yet have recorded.


Compatibility
-------------

Nothing here replaces anything. ``device_list.py``, ``emulator_selector.py``,
``emulator_create.py``, ``emulator_boot.py``, ``emulator_shutdown.py``,
``emulator_erase.py`` and ``emulator_delete.py`` remain executable with exactly
the flags they always had -- this script *calls* them. An existing
``emulator_boot.py --avd Pixel_9 --wait-ready`` keeps working, and so does the
identical ``avd.py start --avd Pixel_9 --wait-ready``.

Usage:
    python3 scripts/avd.py list --json
    python3 scripts/avd.py pick --suggest
    python3 scripts/avd.py create --device pixel_7 --api 34 --name test-device
    python3 scripts/avd.py start --avd test-device --wait-ready
    python3 scripts/avd.py stop --serial emulator-5554
    python3 scripts/avd.py reset --name test-device --verify
    python3 scripts/avd.py delete --name test-device --yes
    python3 scripts/avd.py <verb> --help      # the verb's full flag set
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
        effect: What the verb changes, so the difference between the two
            destructive verbs is answerable from ``--help`` alone.
        destructive: Whether running it can lose work. ``reset`` and ``delete``
            both can, and they lose different things; every other verb is
            either read-only or additive.
        example: A representative invocation.
    """

    name: str
    question: str
    module: str
    script: str
    effect: str
    destructive: bool
    example: str


ROUTES: tuple[Route, ...] = (
    Route(
        name="list",
        question="What is attached, and what is defined?",
        module="device_list",
        script="device_list.py",
        effect="reads adb devices -l and the AVD listing; changes nothing",
        destructive=False,
        example="avd.py list --get-details",
    ),
    Route(
        name="pick",
        question="Which one should I use?",
        module="emulator_selector",
        script="emulator_selector.py",
        effect="ranks AVDs (running > recent > latest API > common model)",
        destructive=False,
        example="avd.py pick --suggest --json",
    ),
    Route(
        name="create",
        question="Make one that does not exist yet.",
        module="emulator_create",
        script="emulator_create.py",
        effect="adds an AVD via avdmanager; touches nothing that exists",
        destructive=False,
        example="avd.py create --device pixel_7 --api 34 --name test-device",
    ),
    Route(
        name="start",
        question="Start one.",
        module="emulator_boot",
        script="emulator_boot.py",
        effect="boots an AVD, optionally waiting for readiness",
        destructive=False,
        example="avd.py start --avd Pixel_9 --wait-ready",
    ),
    Route(
        name="stop",
        question="Stop one.",
        module="emulator_shutdown",
        script="emulator_shutdown.py",
        effect="shuts a running emulator down; the AVD and its data survive",
        destructive=False,
        example="avd.py stop --serial emulator-5554",
    ),
    Route(
        name="reset",
        question="Throw away its data, keep the AVD.",
        module="emulator_erase",
        script="emulator_erase.py",
        effect="DESTRUCTIVE: wipes userdata/cache/sdcard; the AVD remains",
        destructive=True,
        example="avd.py reset --name test-device --verify",
    ),
    Route(
        name="delete",
        question="Throw away the AVD.",
        module="emulator_delete",
        script="emulator_delete.py",
        effect="DESTRUCTIVE: removes the AVD itself (prompts unless --yes)",
        destructive=True,
        example="avd.py delete --name test-device --yes",
    ),
)

ROUTES_BY_NAME = {route.name: route for route in ROUTES}

# Words an agent already knows -- the old script names, and the ordinary
# English for each question -- mapped to the verb that answers them. Explicit
# rather than fuzzy, because difflib is confidently wrong on exactly the words
# that matter here: it scores "devices" (which means `list`) closest to
# `delete`, and has no idea that "boot" means `start`.
SYNONYMS: dict[str, str] = {
    "device_list": "list",
    "devices": "list",
    "avds": "list",
    "ls": "list",
    "emulator_selector": "pick",
    "selector": "pick",
    "select": "pick",
    "suggest": "pick",
    "choose": "pick",
    "best": "pick",
    "emulator_create": "create",
    "new": "create",
    "make": "create",
    "add": "create",
    "emulator_boot": "start",
    "boot": "start",
    "launch": "start",
    "emulator_shutdown": "stop",
    "shutdown": "stop",
    "kill": "stop",
    "halt": "stop",
    "emulator_erase": "reset",
    "factory-reset": "reset",
    "emulator_delete": "delete",
    "remove": "delete",
    "rm": "delete",
    "destroy": "delete",
}

# Words that say "destroy something" without saying which thing. ``erase`` is
# the worst of them: it is the name of the script behind ``reset`` *and* an
# ordinary way to say ``delete``. These are never resolved on the caller's
# behalf -- a wrong guess here costs an AVD, and asking costs one line.
AMBIGUOUS_DESTRUCTIVE = frozenset({"erase", "wipe", "clean", "clear"})

_DISAMBIGUATION = (
    "\nDid you mean: avd.py reset (keep the AVD, throw away its data) "
    "or avd.py delete (throw away the AVD)?"
)


def suggest_verb(typed: str) -> str:
    """The 'did you mean' line for a word that is not a verb.

    Args:
        typed: The unrecognised first argument.

    Returns:
        A hint beginning with a newline, or ``""`` when nothing is close
        enough. A hint never names a single destructive verb unless the caller
        asked for it by an unambiguous name: ``reset`` and ``delete`` destroy
        different things, so a near-miss between them is reported as a
        question, not resolved into an answer.
    """
    if typed in AMBIGUOUS_DESTRUCTIVE:
        return _DISAMBIGUATION
    if typed in SYNONYMS:
        return f"\nDid you mean: avd.py {SYNONYMS[typed]}"

    match = difflib.get_close_matches(typed, ROUTES_BY_NAME, n=1, cutoff=0.4)
    if not match:
        return ""
    if ROUTES_BY_NAME[match[0]].destructive:
        return _DISAMBIGUATION
    return f"\nDid you mean: avd.py {match[0]}"


def _normalise_exit(code: object) -> int:
    """Turn whatever a delegate's ``main()`` produced into a process exit status.

    The seven delegates do not end alike -- most call ``sys.exit()``, and a CLI
    that exits with a message carries a string in ``SystemExit.code``. Every
    shape has to become one integer, or the router would report a failed wipe or
    a failed boot as success.

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
    is no second copy of the flag set here to drift out of step, and in
    particular no second opinion about which flag means "yes, really delete it".

    ``sys.argv[0]`` is set to ``"avd.py <verb>"`` so the delegate's own
    ``--help`` and error messages name the command the agent actually typed.

    Args:
        route: The selected route.
        args: Everything after the verb, including ``--help``.

    Returns:
        The delegate's exit status.
    """
    module = importlib.import_module(route.module)
    saved_argv = sys.argv
    sys.argv = [f"avd.py {route.name}", *args]
    try:
        return _normalise_exit(module.main())
    except SystemExit as exit_request:
        return _normalise_exit(exit_request.code)
    finally:
        sys.argv = saved_argv


def routes_json() -> dict:
    """The routing table, machine-readable.

    Returns:
        A dict naming each verb, the question it answers, what it changes,
        whether it can lose work, and the standalone script that still ships
        the same CLI.
    """
    return {
        "entry_point": "avd.py",
        "routes": [
            {
                "verb": route.name,
                "question": route.question,
                "effect": route.effect,
                "destructive": route.destructive,
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
    question_width = max(len(route.question) for route in ROUTES)
    for route in ROUTES:
        lines.append(f"  {route.name:<{width}}  {route.question:<{question_width}}  {route.effect}")
    lines.append("")
    lines.append(
        "reset and delete are not synonyms: reset keeps the AVD and throws away "
        "its data,\ndelete throws away the AVD. Neither is undoable."
    )
    lines.append("")
    lines.append("Each verb takes the full flag set of the script it delegates to:")
    for route in ROUTES:
        invocation = f"avd.py {route.name} --help"
        lines.append(f"  {invocation:<{width + 20}} -> {route.script}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    """Parser for the router's own flags only; verbs are dispatched before this."""
    return argparse.ArgumentParser(
        prog="avd.py",
        description=(
            "Manage Android virtual devices. One entry point over the skill's "
            "emulator lifecycle scripts, dispatching on the question you are asking."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="avd.py <verb> [options]   |   avd.py --help",
        epilog=f"""{_guide()}

Examples:
  python3 scripts/avd.py list --json
  python3 scripts/avd.py pick --suggest
  python3 scripts/avd.py create --device pixel_7 --api 34 --name test-device
  python3 scripts/avd.py start --avd test-device --wait-ready
  python3 scripts/avd.py stop --serial emulator-5554
  python3 scripts/avd.py reset --name test-device --verify   # keeps the AVD
  python3 scripts/avd.py delete --name test-device --yes     # removes the AVD

Nothing is replaced: the delegated scripts remain callable with the same flags.
`--serial`, `--name`, `--json` and `--verbose` belong to the verbs, which accept
them exactly as they always did; this router adds only `--json` (the table
above, machine-readable).
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
    # an agent that guessed a synonym. Name the nearest verb instead -- except
    # where the nearest verb is one of the two that destroy things, which is
    # asked rather than assumed. See suggest_verb.
    if args and not args[0].startswith("-"):
        print(f"Unknown verb {args[0]!r}.\n\n{_guide()}{suggest_verb(args[0])}", file=sys.stderr)
        return 2

    parsed = parser.parse_args(args)
    if parsed.json:
        print(json.dumps(routes_json(), indent=2))
        return 0

    # Bare `avd.py`: a missing required argument, so exit 2 like argparse does,
    # but print the table rather than a bare usage line.
    parser.print_usage(sys.stderr)
    print(f"\n{_guide()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
