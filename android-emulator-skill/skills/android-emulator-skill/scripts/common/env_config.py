#!/usr/bin/env python3
"""Env-var overrides for tunable defaults.

All overrides use the ``ANDROID_EMU_`` prefix. See SKILL.md → Configuration
for the canonical list of supported variables.
"""

import os
import sys


def env_int(name: str, default: int, min_value: int = 1) -> int:
    """Read ``name`` as an int, falling back to ``default`` on miss or parse error.

    A value below ``min_value`` is raised to it, with a warning on stderr. The
    floor defaults to 1, so a variable for which 0 means "none" or "off" has to
    pass ``min_value=0``: ``ANDROID_EMU_ANR_MAX_RESTARTS=0`` was read with the
    default floor and silently became one restart (N14). The warning is there so
    the next variable read that way says so instead of quietly disagreeing.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not an int; using default {default}", file=sys.stderr)
        return default
    if value < min_value:
        print(
            f"warning: {name}={value} is below the minimum {min_value}; using {min_value}",
            file=sys.stderr,
        )
        return min_value
    return value


def env_float(name: str, default: float, min_value: float = 0.0) -> float:
    """Read ``name`` as a float, falling back to ``default`` on miss or parse error."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not a float; using default {default}", file=sys.stderr)
        return default
    return max(value, min_value)
