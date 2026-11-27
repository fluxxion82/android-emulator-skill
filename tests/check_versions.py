#!/usr/bin/env python3
"""Whether the five versioned files all state one version, each read the way its format says.

    python3 tests/check_versions.py 0.10.0

Run from the repository's root by the `version` job of release.yml and by
validate-version.yml, on a runner with nothing installed: the standard library
only. Exit 0 when all five state the version given, 1 when one states another
or cannot be read, 2 when the argument is not `X.Y.Z`.

Each file is parsed, and the version is the one at the place the format gives
it. The gates used to look for the first line that resembled a version, which
is another table's `version` in pyproject.toml, a nested `"version"` in a
manifest, a line of SKILL.md's body, or the first half of `version: 0.9.0
extra`; and they found none at all when a value stood on the line after its key.

SKILL.md's frontmatter is YAML and there is no YAML reader here, so it is not
parsed. The block between the first two `---` lines is held to a grammar in
which YAML reads the same version this does (the other two fields it may still
read another way: `description: off` is a boolean to YAML, and the suite's
business). It is three lines, one each for `name`,
`description` and `version`, in any order: the three keys a skill's
frontmatter has, each written once. Every line is `key: value`; the value
starts with a letter, is printable ASCII and nothing else, holds no `: ` and
no ` #`, and does not end in `:`. That leaves no character YAML could take
for a line break, a second mapping, a comment or the start of a quoted or
folded value, no first character from which it could read a date or a number,
and no key but those three, so no line can be the inside of another field and
no key can be written a second way. The version line alone starts with a
digit: it is `version: X.Y.Z` and nothing else.

The price is that frontmatter YAML reads perfectly well (a quoted value, an
accented letter, a comment, a fourth key) is refused here, not guessed at.
tests/test_packaging_contract.py reads the same frontmatter with a YAML parser
and holds the file to what this reads, so the refusal is met in a pull request
and not on the day of a release.

The limit, stated: this grammar is an argument that YAML reads the block the
same way, made by a reader that is not a YAML parser. The proof is that test,
which parses the block and compares; it runs in the suite, and so in the
`verify` job a release's packaging waits for. validate-version.yml runs this
script alone.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

PLUGIN = "android-emulator-skill"
EXPECTED = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# One field of the frontmatter: one of its three keys, then a value that is
# printable ASCII from its first character to its last. `[ -~]` is the space
# to the tilde: no control character, no line break of any script. A name or
# a description starts with a letter, since YAML reads `2026-02-30` as a date
# (and refuses it) and `12` as a number; the version is `X.Y.Z`, which it
# reads as text.
FRONTMATTER_KEYS = ("description", "name", "version")
FRONTMATTER_FIELD = re.compile(
    r"(?P<key>description|name): (?P<value>[A-Za-z][ -~]*)"
    r"|(?P<version_key>version): (?P<version>[0-9][ -~]*)"
)
# What would make YAML read such a value as more than text.
FRONTMATTER_STRUCTURE = (": ", " #")


class UnreadableError(Exception):
    """A file that does not state a version where its format puts one."""


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise UnreadableError(f"{where} is {value!r}, not a version written as text")
    return value


def _json(text: str) -> dict:
    def refusing_a_key_written_twice(pairs: list[tuple[str, object]]) -> dict:
        keys = [key for key, _ in pairs]
        twice = sorted({key for key in keys if keys.count(key) > 1})
        if twice:
            raise UnreadableError(f"an object writes {twice} more than once")
        return dict(pairs)

    def refusing_what_is_not_json(constant: str) -> object:
        raise UnreadableError(f"it holds `{constant}`, which Python reads and JSON does not have")

    try:
        document = json.loads(
            text,
            object_pairs_hook=refusing_a_key_written_twice,
            parse_constant=refusing_what_is_not_json,
        )
    except ValueError as error:
        raise UnreadableError(f"it is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise UnreadableError("it is not a JSON object")
    return document


def pyproject(text: str) -> str:
    """`[project].version` of a pyproject.toml."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise UnreadableError(f"it is not TOML: {error}") from error
    project = document.get("project")
    if not isinstance(project, dict):
        raise UnreadableError("it has no [project] table")
    return _text(project.get("version"), "[project].version")


def manifest(text: str) -> str:
    """The top-level `"version"` of a plugin manifest."""
    return _text(_json(text).get("version"), 'the top-level "version"')


def marketplace(text: str) -> str:
    """The `"version"` of this plugin's entry in a marketplace file, found by its name."""
    plugins = _json(text).get("plugins")
    entries = [
        entry
        for entry in (plugins if isinstance(plugins, list) else [])
        if isinstance(entry, dict) and entry.get("name") == PLUGIN
    ]
    if len(entries) != 1:
        raise UnreadableError(f"it lists {len(entries)} plugins named {PLUGIN}, not one")
    return _text(entries[0].get("version"), f'the "version" of the {PLUGIN} entry')


def skill_md(text: str) -> str:
    """The `version:` line of a SKILL.md's frontmatter, in its one spelling."""
    lines = text.split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        raise UnreadableError("it does not open with a frontmatter block between two `---` lines")
    block = lines[1 : lines.index("---", 1)]
    fields: dict[str, str] = {}
    for line in block:
        field = FRONTMATTER_FIELD.fullmatch(line)
        key = (field["key"] or field["version_key"]) if field else ""
        value = (field["value"] or field["version"]) if field else ""
        # With a space after it, so that a `:` at the very end is the `: ` it would be to YAML.
        if field is None or any(mark in f"{value} " for mark in FRONTMATTER_STRUCTURE):
            raise UnreadableError(
                f"its frontmatter line {line[:60]!r} is not one plain field of "
                f"{', '.join(FRONTMATTER_KEYS)}: `key: value` in printable ASCII, a name or a "
                "description starting with a letter, with no `: `, no ` #` and no `:` at its end"
            )
        if key in fields:
            raise UnreadableError(f"its frontmatter writes `{key}:` more than once")
        fields[key] = value
    if sorted(fields) != list(FRONTMATTER_KEYS):
        missing = sorted(set(FRONTMATTER_KEYS) - set(fields))
        raise UnreadableError(f"its frontmatter has no {' or '.join(missing)}")
    if not EXPECTED.fullmatch(fields["version"]):
        raise UnreadableError(f"its frontmatter version {fields['version']!r} is not X.Y.Z")
    return fields["version"]


READERS: dict[str, Callable[[str], str]] = {
    "pyproject.toml": pyproject,
    f"{PLUGIN}/skills/{PLUGIN}/SKILL.md": skill_md,
    f"{PLUGIN}/.claude-plugin/plugin.json": manifest,
    f"{PLUGIN}/.codex-plugin/plugin.json": manifest,
    ".claude-plugin/marketplace.json": marketplace,
}


def read(root: Path) -> dict[str, str | UnreadableError]:
    """What each versioned file under a repository root states: a version, or why it has none."""
    found: dict[str, str | UnreadableError] = {}
    for relative, reader in READERS.items():
        try:
            found[relative] = reader((root / relative).read_text(encoding="utf-8"))
        except UnreadableError as unreadable:
            found[relative] = unreadable
        except (OSError, UnicodeDecodeError) as error:
            found[relative] = UnreadableError(f"it could not be read: {error}")
    return found


def problems(root: Path, expected: str) -> list[str]:
    """One line for each versioned file that does not state the expected version."""
    wrong = []
    for relative, stated in read(root).items():
        if isinstance(stated, UnreadableError):
            wrong.append(f"::error file={relative}::expected {expected}, and {stated}")
        elif stated != expected:
            wrong.append(f"::error file={relative}::expected {expected}, found '{stated}'")
    return wrong


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or not EXPECTED.fullmatch(arguments[0]):
        print("usage: python3 tests/check_versions.py X.Y.Z   (from the repository's root)")
        return 2
    expected = arguments[0]
    root = Path.cwd()
    wrong = problems(root, expected)
    for line in wrong:
        print(line)
    if not wrong:
        for relative in READERS:
            print(f"ok  {relative} -> {expected}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
