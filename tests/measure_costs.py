#!/usr/bin/env python3
"""Measure what an agent pays to see a screen with this skill, and without it.

Run it from a checkout of the repository (it is not part of the installed
plugin; Pillow is needed for the screenshot rows):

    uvx --python 3.12 -c dev-constraints.txt --with pillow python tests/measure_costs.py
    ... python tests/measure_costs.py --json     # the same numbers, one JSON object
    ... python tests/measure_costs.py --check    # compare with the committed table
    ... python tests/measure_costs.py --write    # rewrite the committed table

Why this exists
---------------
"Token-efficient" was a claim with no number behind it. This prints the numbers
and nothing in it is typed: every figure is the size of something, computed
when you run it.

What is measured
----------------
*Bytes.* For each screen in ``tests/fixtures/measurements/cases.json``:

- the raw dump: the recorded XML document ``uiautomator dump`` returned;
- what ``screen_mapper.py`` and ``navigator.py`` print for that same screen.
  Each script runs as it does from a shell -- its ``main()`` under ``cli.run``,
  the boundary that turns a failure into an exit status -- with the two things
  it asks the device replaced by recordings of that screen: the dump, and the
  focus lines of ``dumpsys window`` (``replaying``). Any other attempt to reach
  a device is an error even if the script shrugs it off, and so is any exit
  status but 0.

*Screenshots* report pixel dimensions and PNG bytes at each ``--size`` preset,
resized by the skill's own ``resize_screenshot``. The recorded screenshots have
their status bar painted out, so their bytes are a little under a live
capture's.

*The skill itself*: the bytes of SKILL.md, the file an agent loads first, and
of the reference files beside it when there are any.

What is estimated
-----------------
*Tokens* are bytes / 4, rounded. That is an estimate and is labelled one
everywhere it appears: no tokenizer is run, and no figure is given for what an
image costs a model, because that is not a function of its PNG size.

What this is not
----------------
A benchmark of an agent, or a like-for-like comparison of information. The six
"walk" screens are one small fixture app; the other screens are single
recordings replayed. It measures the size of one answer per screen. The raw
dump holds everything on the screen; the scripts' answers are summaries, or the
reply to one question. How many calls a task takes is not measured.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "android-emulator-skill" / "skills" / "android-emulator-skill"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPTS = SKILL_DIR / "scripts"
RECORDED = REPO / "tests" / "fixtures" / "recorded"
MEASUREMENTS = REPO / "tests" / "fixtures" / "measurements"
CASES = MEASUREMENTS / "cases.json"
EXPECTED = MEASUREMENTS / "expected.json"

ESTIMATE = "tokens are bytes / 4, rounded: an estimate, no tokenizer is run"
NOT_PRESENT = "not present"

# The serial the replayed scripts are told they are talking to. Nothing is.
REPLAY_SERIAL = "emulator-5554"


class MeasurementError(RuntimeError):
    """A case could not be measured. Never papered over with a number."""


def estimated_tokens(size: int) -> int:
    """Bytes / 4, rounded half up. An estimate; see the module docstring."""
    return (size + 2) // 4


def _size(text: str) -> dict:
    size = len(text.encode("utf-8"))
    return {"bytes": size, "tokens_est": estimated_tokens(size)}


def _scripts():
    """The skill's own modules, imported from this checkout."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import navigator
    import screen_mapper

    from common import adb_exec, cli, hierarchy, screenshot_utils

    return {
        "navigator": navigator,
        "screen_mapper": screen_mapper,
        "adb_exec": adb_exec,
        "cli": cli,
        "hierarchy": hierarchy,
        "screenshot_utils": screenshot_utils,
    }


# The one device command a measured script is allowed, and how it is spelled
# at the end of the adb argv: screen_mapper asks which activity has focus.
FOCUS_QUERY = ["shell", "dumpsys", "window"]


@contextlib.contextmanager
def replaying(module, xml: str, focus: str) -> Iterator[list[list[str]]]:
    """Answer ``module``'s two questions to the device from recordings, and no others.

    The script under measurement believes it is talking to
    :data:`REPLAY_SERIAL`. Its screen capture returns ``xml``; ``dumpsys
    window`` returns ``focus``, the focus lines recorded with that screen on
    top. Everything it does with them -- parsing, labelling, matching,
    formatting -- is its own code.

    Any other command is refused, and *remembered*: the list this yields holds
    every refused argv. Refusing is not enough by itself. ``screen_mapper``
    catches a failed focus lookup and prints "Unknown Screen", so a refusal it
    swallowed came back as a normal, shorter row -- which is how the first
    version of this table understated every mapper figure.
    """
    adb_exec = _scripts()["adb_exec"]
    root = ET.fromstring(xml)
    refused: list[list[str]] = []

    def device(cmd, **_keywords):
        argv = [str(part) for part in cmd]
        if argv[-len(FOCUS_QUERY) :] == FOCUS_QUERY:
            return adb_exec.subprocess.CompletedProcess(cmd, 0, focus, "")
        refused.append(argv)
        raise MeasurementError(f"the replayed script tried to reach a device: {' '.join(argv)}")

    saved = (
        module.capture_hierarchy,
        module.resolve_device_identifier,
        adb_exec.subprocess.run,
    )
    module.capture_hierarchy = lambda _serial=None, **_keywords: root
    module.resolve_device_identifier = lambda _identifier: REPLAY_SERIAL
    adb_exec.subprocess.run = device
    try:
        yield refused
    finally:
        (
            module.capture_hierarchy,
            module.resolve_device_identifier,
            adb_exec.subprocess.run,
        ) = saved


def tool_output(tool: str, argv: list[str], xml: str, focus: str) -> str:
    """What ``<tool>.py <argv>`` prints to stdout for one recorded screen.

    Run through ``cli.run``, the scripts' own boundary: called bare, ``main``
    raises ``cli.Failure`` for a "Not found" instead of exiting 1, and a
    harness that caught only ``SystemExit`` would never see the refusal.

    Args:
        tool: ``navigator`` or ``screen_mapper``.
        argv: Its arguments.
        xml: The screen's recorded dump.
        focus: The focus lines of ``dumpsys window`` recorded on that screen.

    Raises:
        MeasurementError: The script reached for a device beyond the two
            recorded answers, whether or not it carried on afterwards; or it
            did not exit 0. A "Not found" is short, and recording its size as
            the cost of finding something would make the table look better the
            more the tool failed.
    """
    module, cli = _scripts()[tool], _scripts()["cli"]
    stdout, stderr = io.StringIO(), io.StringIO()
    status: object = "did not exit"
    with (
        replaying(module, xml, focus) as refused,
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            cli.run(module.main, list(argv))
        except SystemExit as exit_:
            status = exit_.code
    if refused:
        raise MeasurementError(
            f"{tool}.py {' '.join(argv)} tried to reach a device "
            f"({len(refused)} command(s), first: adb ... {' '.join(refused[0][-4:])}); "
            f"its output is not a measurement of the recording"
        )
    if status not in (0, None):
        said = (stderr.getvalue() or stdout.getvalue()).strip()[:200]
        raise MeasurementError(f"{tool}.py {' '.join(argv)} exited {status}: {said!r}")
    return stdout.getvalue()


def load_cases() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def _recorded(cases: dict, name: str, ext: str) -> Path:
    path = RECORDED / cases["profile"] / f"{name}.{ext}"
    if not path.exists():
        raise MeasurementError(
            f"recorded fixture {path.relative_to(REPO)} is missing; record it with "
            f"tests/record_fixtures.py --only {name}"
        )
    return path


def measure_screens(cases: dict) -> list[dict]:
    """One entry per screen: the size of each output listed in the inventory."""
    screens = []
    for screen in cases["screens"]:
        xml = _recorded(cases, screen["fixture"], "xml").read_text(encoding="utf-8")
        focus = _recorded(cases, screen["focus"], "txt").read_text(encoding="utf-8")
        rows = {}
        for output in cases["outputs"]:
            if "tool" not in output:
                rows[output["id"]] = _size(xml)
                continue
            argv = [arg.format(find_text=screen["find_text"]) for arg in output["argv"]]
            rows[output["id"]] = _size(tool_output(output["tool"], argv, xml, focus))
        screens.append(
            {
                "id": screen["id"],
                "walk": screen.get("walk"),
                "fixture": screen["fixture"],
                "focus": screen["focus"],
                "find_text": screen["find_text"],
                "rows": rows,
            }
        )
    return screens


def measure_walk(screens: list[dict]) -> dict:
    """Totals over the screens that are part of the walk, in walk order."""
    steps = sorted((s for s in screens if s["walk"] is not None), key=lambda s: s["walk"])
    if [s["walk"] for s in steps] != list(range(1, len(steps) + 1)):
        raise MeasurementError(f"walk steps are not 1..n: {[s['walk'] for s in steps]}")
    totals = {}
    for output in steps[0]["rows"]:
        size = sum(step["rows"][output]["bytes"] for step in steps)
        totals[output] = {"bytes": size, "tokens_est": estimated_tokens(size)}
    raw = totals["raw_dump"]["bytes"]
    return {
        "screens": [step["id"] for step in steps],
        "totals": totals,
        "percent_of_raw_dump": {
            output: round(100 * total["bytes"] / raw, 1) for output, total in totals.items()
        },
    }


def measure_stream(cases: dict) -> dict:
    """How much of what ``exec-out`` delivers is the document, and how much is framing."""
    hierarchy = _scripts()["hierarchy"]
    stream = _recorded(cases, cases["stream"]["fixture"], "xml").read_text(encoding="utf-8")
    document = hierarchy._extract_xml(stream)
    if document is None:
        raise MeasurementError(f"{cases['stream']['fixture']} holds no XML document")
    stream_bytes, document_bytes = len(stream.encode("utf-8")), len(document.encode("utf-8"))
    return {
        "fixture": cases["stream"]["fixture"],
        "stream_bytes": stream_bytes,
        "document_bytes": document_bytes,
        "framing_bytes": stream_bytes - document_bytes,
    }


def measure_screenshots(cases: dict) -> list[dict]:
    """Pixel dimensions and PNG bytes of each walk screenshot at each size preset."""
    screenshot_utils = _scripts()["screenshot_utils"]
    if not screenshot_utils.HAS_PIL:
        raise MeasurementError(
            "the screenshot rows need Pillow. Run with it available, e.g.\n"
            "  uvx --python 3.12 -c dev-constraints.txt --with pillow "
            "python tests/measure_costs.py"
        )
    measured = []
    with tempfile.TemporaryDirectory(prefix="measure_costs_") as scratch:
        for screen in cases["screens"]:
            if "screenshot" not in screen:
                continue
            source = _recorded(cases, screen["screenshot"], "png")
            presets = {}
            for preset in cases["screenshot_presets"]:
                target = Path(scratch) / f"{screen['id']}_{preset}.png"
                _path, width, height = screenshot_utils.resize_screenshot(
                    str(source), str(target), preset
                )
                presets[preset] = {
                    "width": width,
                    "height": height,
                    "bytes": target.stat().st_size,
                }
            measured.append(
                {"id": screen["id"], "fixture": screen["screenshot"], "presets": presets}
            )
    return measured


def measure_skill() -> dict:
    """The bytes an agent loads to learn the skill: SKILL.md, and references if any."""
    content = SKILL_MD.read_bytes()
    references = sorted((SKILL_DIR / "references").glob("*.md"))
    skill = {
        "path": str(SKILL_MD.relative_to(REPO)),
        "bytes": len(content),
        "tokens_est": estimated_tokens(len(content)),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if not references:
        skill["references"] = NOT_PRESENT
        skill["skill_md_plus_largest_reference"] = NOT_PRESENT
        return skill
    sizes = {path.name: path.stat().st_size for path in references}
    largest = max(sizes.values())
    skill["references"] = {
        "files": len(sizes),
        "total_bytes": sum(sizes.values()),
        "smallest_bytes": min(sizes.values()),
        "largest_bytes": largest,
    }
    combined = len(content) + largest
    skill["skill_md_plus_largest_reference"] = {
        "bytes": combined,
        "tokens_est": estimated_tokens(combined),
    }
    return skill


def measure() -> dict:
    """Everything, as one JSON-serialisable object."""
    cases = load_cases()
    screens = measure_screens(cases)
    return {
        "estimate": ESTIMATE,
        "profile": cases["profile"],
        "skill": measure_skill(),
        "screens": screens,
        "walk": measure_walk(screens),
        "stream": measure_stream(cases),
        "screenshots": measure_screenshots(cases),
    }


def stable_view(result: dict) -> dict:
    """The part of a measurement that does not depend on the machine or on SKILL.md.

    "Does not depend on the machine" under the scripts' default configuration:
    an ``ANDROID_EMU_*`` override in the environment can change what a script
    prints, and this does not look for one.

    Two things are left out of the committed table, and said here rather than
    hidden:

    - ``skill``: SKILL.md's size is the thing this release changes. It is
      reported live and compared with ``skill_md_v0.9.0.json``, the size it had
      before.
    - the *bytes* of a resized screenshot: a PNG's size depends on the zlib the
      encoder was built with, which differs between platforms. Dimensions do
      not, and neither does the ``full`` preset, which is a copy of the file.
    """
    view = copy.deepcopy(result)
    del view["skill"]
    for screenshot in view["screenshots"]:
        for preset, measured in screenshot["presets"].items():
            if preset != "full":
                del measured["bytes"]
    return view


def differences(expected: object, actual: object, where: str = "") -> list[str]:
    """Every place two measurements disagree, as ``path: expected -> actual``."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        found = []
        for key in sorted(set(expected) | set(actual)):
            path = f"{where}.{key}" if where else str(key)
            if key not in expected:
                found.append(f"{path}: not in the committed table")
            elif key not in actual:
                found.append(f"{path}: no longer measured")
            else:
                found.extend(differences(expected[key], actual[key], path))
        return found
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        found = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            found.extend(differences(left, right, f"{where}[{index}]"))
        return found
    return [] if expected == actual else [f"{where}: {expected!r} -> {actual!r}"]


def _cell(measured: dict) -> str:
    return f"{measured['bytes']:,} (~{measured['tokens_est']:,})"


def render(result: dict) -> str:
    """The measurement as Markdown tables, for a person."""
    outputs = list(result["screens"][0]["rows"])
    titles = {
        "raw_dump": "raw dump",
        "screen_mapper": "`screen_mapper`",
        "screen_mapper_verbose": "`screen_mapper --verbose`",
        "navigator_list": "`navigator --list`",
        "navigator_find_text": "`navigator --find-text`",
    }
    lines = [
        "## What one screen costs to read",
        "",
        f"Bytes, with tokens in brackets. {ESTIMATE.capitalize()}.",
        "",
        "| Screen | " + " | ".join(titles.get(o, o) for o in outputs) + " |",
        "|---|" + "---:|" * len(outputs),
    ]
    for screen in result["screens"]:
        label = screen["id"].replace("_", " ")
        lines.append(f"| {label} | " + " | ".join(_cell(screen["rows"][o]) for o in outputs) + " |")
    walk = result["walk"]
    lines.append(
        f"| **walk, {len(walk['screens'])} screens** | "
        + " | ".join(f"**{_cell(walk['totals'][o])}**" for o in outputs)
        + " |"
    )
    lines.append(
        "| share of the raw dump | "
        + " | ".join(f"{walk['percent_of_raw_dump'][o]}%" for o in outputs)
        + " |"
    )
    stream = result["stream"]
    lines += [
        "",
        "What this compares, and what it does not:",
        "",
        f"- The walk is {len(walk['screens'])} screens of one small fixture app; the other "
        "rows are single recorded screens, replayed. It is the size of one answer per "
        "screen, not the cost of a task: taps, scrolling, retries and second looks are "
        "not counted.",
        "- The columns do not carry the same information. The raw dump is everything on "
        "screen. `screen_mapper` and `navigator --list` are summaries that leave things "
        "out and cut long lists short. `navigator --find-text` answers one question, for "
        "a text the agent already knew to ask for, and found it every time here.",
        "- The fixture app sets `testTagsAsResourceId`, so its controls are reported by "
        "short ids. An app that does not is reported by its visible text.",
        f"- Over `adb exec-out` a dump arrives with {stream['framing_bytes']} bytes of "
        f"status line after the document ({stream['stream_bytes']:,} bytes in all for a "
        f"{stream['document_bytes']:,}-byte document).",
        "",
        "## Screenshots",
        "",
        "Pixel dimensions and PNG bytes at each `--size` preset. No token figure: what an "
        "image costs a model is not a function of its file size. The recorded screens have "
        "their status bar painted out.",
        "",
    ]
    presets = list(result["screenshots"][0]["presets"])
    lines.append("| Screen | " + " | ".join(presets) + " |")
    lines.append("|---|" + "---:|" * len(presets))
    for screenshot in result["screenshots"]:
        cells = [
            f"{m['width']}x{m['height']}, {m['bytes']:,}" for m in screenshot["presets"].values()
        ]
        lines.append(f"| {screenshot['id'].replace('_', ' ')} | " + " | ".join(cells) + " |")
    skill = result["skill"]
    lines += [
        "",
        "## Loading the skill",
        "",
        "| What the agent loads | bytes | tokens (estimate) |",
        "|---|---:|---:|",
        f"| `SKILL.md` | {skill['bytes']:,} | ~{skill['tokens_est']:,} |",
    ]
    plus = skill["skill_md_plus_largest_reference"]
    if plus == NOT_PRESENT:
        lines.append(f"| `SKILL.md` plus one reference file | {NOT_PRESENT} | {NOT_PRESENT} |")
    else:
        lines.append(
            f"| `SKILL.md` plus its largest reference file | {plus['bytes']:,} | "
            f"~{plus['tokens_est']:,} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure what an agent pays to see a screen, from recorded fixtures.",
        epilog="Run from a checkout of the repository. See the module docstring for the method.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="Print one JSON object")
    mode.add_argument(
        "--check",
        action="store_true",
        help=f"Compare with {EXPECTED.relative_to(REPO)}; exit 1 where they differ",
    )
    mode.add_argument("--write", action="store_true", help=f"Rewrite {EXPECTED.relative_to(REPO)}")
    args = parser.parse_args(argv)

    try:
        result = measure()
    except MeasurementError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    elif args.write:
        EXPECTED.write_text(json.dumps(stable_view(result), indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {EXPECTED.relative_to(REPO)}")
    elif args.check:
        expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        changed = differences(expected, stable_view(result))
        if changed:
            print(f"{len(changed)} measurement(s) differ from the committed table:")
            for line in changed:
                print(f"  {line}")
            print("If the change is intended, run: python tests/measure_costs.py --write")
            return 1
        print(f"Measurements match {EXPECTED.relative_to(REPO)}")
    else:
        print(render(result), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
