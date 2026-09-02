#!/usr/bin/env python3
"""
Resolution of Android SDK command-line tools.

The bare tool name is not a safe ``argv[0]`` for the emulator. The SDK root
contains a *directory* named ``emulator`` (the binary lives at
``$ANDROID_HOME/emulator/emulator``), so a PATH that includes the SDK root
instead of the emulator directory makes ``execve("emulator")`` hit that
directory and raise ``PermissionError: [Errno 13] Permission denied`` — not the
``FileNotFoundError`` callers usually guard against.

``shutil.which`` rejects directories, so it is the honest probe: it keeps
scanning PATH past ``<sdk>/emulator`` and returns only a real executable file.
When PATH has nothing, fall back to the SDK layout the way
``emulator_create.get_avdmanager_path`` / ``get_sdkmanager_path`` already do.

Used by:
- device_list.py - ``emulator -list-avds`` for defined AVDs
- emulator_boot.py - booting and listing AVDs
- emulator_selector.py - candidate AVD enumeration and CLI boot fallback
"""

import os
import shutil
from collections.abc import Iterable
from pathlib import Path

# Where the emulator binary lives relative to the SDK root: the modern
# ``emulator`` package, then the legacy pre-25.3 ``tools`` location.
EMULATOR_SDK_SUBDIRS = ("emulator", "tools")

# Actionable message for a missing emulator: names the directory to add rather
# than blaming "PATH", since the usual mistake is adding the SDK *root*.
EMULATOR_NOT_FOUND_MESSAGE = (
    "Error: Android SDK 'emulator' binary not found. Install it with "
    "\"sdkmanager 'emulator'\" and put the emulator directory itself — not the "
    'SDK root — on PATH: export PATH="$PATH:$ANDROID_HOME/emulator"'
)


def get_android_sdk_root() -> str | None:
    """Return ``$ANDROID_HOME`` (or the older ``$ANDROID_SDK_ROOT``), if set."""
    return os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")


def resolve_sdk_tool(tool: str, sdk_subdirs: Iterable[str]) -> str | None:
    """
    Resolve an SDK tool to an executable file path.

    Args:
        tool: Tool name, e.g. "emulator"
        sdk_subdirs: Directories under the SDK root to try when PATH misses,
            in preference order, e.g. ("emulator", "tools")

    Returns:
        Absolute path to an executable file, or None when nothing usable exists.
        Never returns a bare name, and never returns a directory.
    """
    on_path = shutil.which(tool)
    if on_path:
        return on_path

    sdk_root = get_android_sdk_root()
    if not sdk_root:
        return None

    for subdir in sdk_subdirs:
        candidate = Path(sdk_root) / subdir / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def get_emulator_path() -> str | None:
    """
    Find the ``emulator`` binary.

    Returns:
        Path to the emulator executable, or None if not found. Callers should
        report :data:`EMULATOR_NOT_FOUND_MESSAGE` rather than execing the bare
        name, which crashes with PermissionError on an SDK-root PATH.
    """
    return resolve_sdk_tool("emulator", EMULATOR_SDK_SUBDIRS)
