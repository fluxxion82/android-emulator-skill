#!/usr/bin/env python3
"""Reject commit-message trailers.

This repository's commit messages end at their last content line. No
``Co-Authored-By:``, no ``Signed-off-by:``, no generated-with footer.

Why this is a hook and not a line in a style guide: the instruction to add
``Co-Authored-By`` is a *default* in several tools and assistants, so it comes
back on its own. Something that reapplies itself needs a check that reapplies
itself too -- the same reasoning as ``tests/test_fixture_policy.py``, where a
rule that lived only in prose was already being skipped.

Install (once per clone; the commit-msg stage is not installed by default):

    pre-commit install --hook-type commit-msg

Usage: check-commit-trailers.py <path-to-commit-message-file>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Matched at the start of a line, case-insensitively. `Signed-off-by` is
# included even though nobody has added one yet: the rule is "no trailers", not
# "not that particular trailer".
TRAILER = re.compile(
    r"^\s*(Co-Authored-By|Co-authored-by|Signed-off-by|Reviewed-by|Helped-by)\s*:",
    re.IGNORECASE,
)

# The other half of the same habit: tool-generated attribution footers.
FOOTER_MARKERS = (
    "generated with [claude code]",
    "🤖 generated with",
)


def offending_lines(message: str) -> list[tuple[int, str]]:
    """Trailer and footer lines, as (1-indexed line number, text)."""
    offenders = []
    for number, line in enumerate(message.splitlines(), start=1):
        stripped = line.strip()
        # A comment line is git's own scaffolding, not part of the message.
        if stripped.startswith("#"):
            continue
        if TRAILER.match(line) or any(m in stripped.lower() for m in FOOTER_MARKERS):
            offenders.append((number, stripped))
    return offenders


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check-commit-trailers.py <commit-message-file>", file=sys.stderr)
        return 2

    message = Path(sys.argv[1]).read_text(encoding="utf-8")
    offenders = offending_lines(message)
    if not offenders:
        return 0

    print("Commit message contains trailers, which this repo does not use:", file=sys.stderr)
    for number, text in offenders:
        print(f"  line {number}: {text}", file=sys.stderr)
    print(
        "\nDelete those lines. The message should end at its last content line.\n"
        "To strip them from a commit that already exists:\n"
        "  git log -1 --pretty=%B | grep -vE '^(Co-Authored-By|Signed-off-by):' "
        "| git commit --amend -F -",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
