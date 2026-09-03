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

A tool that is missing, or that ran and failed, is an ERROR here -- never an
empty list. All three listing scripts used to answer a broken SDK with
``[]`` and exit 0, so an agent could not tell "this host defines no AVDs" from
"AVD discovery is broken", and had no second signal to consult (X3/L8). The
raise-with-a-remedy policy lives in :class:`SdkToolError` and
:func:`run_sdk_tool` so the three call sites cannot drift apart again, which is
how one finding came to exist in three files at once.

Used by:
- device_list.py - ``emulator -list-avds`` for defined AVDs
- emulator_boot.py - booting and listing AVDs
- emulator_selector.py - candidate AVD enumeration and CLI boot fallback
- emulator_delete.py - ``avdmanager list avd`` before a destructive delete
"""

import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

# Where the emulator binary lives relative to the SDK root: the modern
# ``emulator`` package, then the legacy pre-25.3 ``tools`` location.
EMULATOR_SDK_SUBDIRS = ("emulator", "tools")

# Actionable message for a missing emulator: names the directory to add rather
# than blaming "PATH", since the usual mistake is adding the SDK *root*. The
# remedy is kept separately from the "Error: " sentence because it is now also
# appended to raised errors, and a CLI boundary that prints "Error: {error}"
# would otherwise say it twice.
EMULATOR_NOT_FOUND_REMEDY = (
    "Install it with \"sdkmanager 'emulator'\" and put the emulator directory "
    'itself — not the SDK root — on PATH: export PATH="$PATH:$ANDROID_HOME/emulator"'
)

EMULATOR_NOT_FOUND_MESSAGE = (
    f"Error: Android SDK 'emulator' binary not found. {EMULATOR_NOT_FOUND_REMEDY}"
)

# avdmanager and sdkmanager ship in the command-line tools package, which is not
# installed by default with Android Studio -- the single most common reason AVD
# listing is unavailable on an otherwise working SDK.
CMDLINE_TOOLS_REMEDY = (
    "Install the SDK command-line tools (Android Studio -> SDK Manager -> SDK "
    "Tools -> 'Android SDK Command-line Tools (latest)', or "
    "\"sdkmanager 'cmdline-tools;latest'\") and put "
    "$ANDROID_HOME/cmdline-tools/latest/bin on PATH."
)

# AVD directories under the SDK root for the command-line tools, newest first.
CMDLINE_TOOLS_SUBDIRS = ("cmdline-tools/latest/bin", "tools/bin")


class SdkToolError(RuntimeError):
    """An Android SDK tool is absent, or it ran and failed.

    Deliberately NOT the same thing as a tool that ran and reported nothing:
    "this host has no AVDs" is an answer and exits 0, while "I could not look"
    is a failure and exits non-zero with a remedy. Conflating the two is X3/L8,
    and it was live in four scripts at once.
    """


def get_android_sdk_root() -> str | None:
    """Return ``$ANDROID_HOME`` (or the older ``$ANDROID_SDK_ROOT``), if set."""
    return os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")


# What to tell someone whose AVDs are not where :func:`resolve_avd_home` looked.
# Named separately so the two destructive scripts quote the same remedy.
AVD_HOME_REMEDY = (
    "set ANDROID_AVD_HOME to the directory that holds your .avd directories, "
    "or ANDROID_SDK_HOME to its .android parent"
)


def resolve_avd_home() -> Path:
    """Where this host keeps its ``<name>.avd`` directories.

    Resolved in ``avdmanager``'s own order, which is not the obvious one:

    1. ``$ANDROID_AVD_HOME`` -- the directory of ``.avd`` directories itself.
    2. ``$ANDROID_SDK_HOME`` -- the *parent of* ``.android``, so the AVDs are a
       further ``.android/avd`` down. Missing this one is L5: two scripts read
       only ``ANDROID_AVD_HOME`` and then fell through to the home directory,
       so on a host that relocates the AVD tree (CI images do) they looked
       somewhere the AVDs are not, found no directory to stat, and -- in
       ``emulator_delete --old`` -- ranked every AVD as equally ancient.
    3. ``~/.android/avd``.

    Note this is deliberately NOT under the SDK root: ``ANDROID_SDK_HOME`` is a
    user-data location despite the name, which is why it is resolved here and
    not through :func:`get_android_sdk_root`.

    Returns:
        The AVD home. It is not checked for existence -- "does this host even
        have one" is the caller's question, and the two callers answer it
        differently.
    """
    avd_home = os.environ.get("ANDROID_AVD_HOME")
    if avd_home:
        return Path(avd_home)

    sdk_home = os.environ.get("ANDROID_SDK_HOME")
    if sdk_home:
        return Path(sdk_home) / ".android" / "avd"

    return Path.home() / ".android" / "avd"


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


def searched_locations(tool: str, sdk_subdirs: Iterable[str]) -> str:
    """Where :func:`resolve_sdk_tool` looked, phrased for an error message.

    An error that says only "not found" leaves the reader guessing whether the
    problem is PATH, ANDROID_HOME, or a package that was never installed. This
    names the actual candidates that were tried, with the SDK root filled in.

    Args:
        tool: Tool name, e.g. "emulator".
        sdk_subdirs: The same subdirectories passed to :func:`resolve_sdk_tool`.

    Returns:
        A comma-separated list of the locations searched.
    """
    places = ["PATH"]
    sdk_root = get_android_sdk_root()
    if sdk_root:
        places += [str(Path(sdk_root) / subdir / tool) for subdir in sdk_subdirs]
    else:
        places.append("$ANDROID_HOME and $ANDROID_SDK_ROOT (neither is set)")
    return ", ".join(places)


def missing_emulator_error() -> SdkToolError:
    """The error for an unresolvable emulator, with the search path spelled out.

    Returned rather than raised so each caller keeps its own module-level
    ``get_emulator_path`` call -- three scripts and their tests substitute that
    name -- while the sentence an agent reads is written once.

    Returns:
        The :class:`SdkToolError` to raise.
    """
    return SdkToolError(
        f"Android SDK 'emulator' binary not found. Looked in: "
        f"{searched_locations('emulator', EMULATOR_SDK_SUBDIRS)}. "
        f"{EMULATOR_NOT_FOUND_REMEDY}"
    )


def run_sdk_tool(argv: list[str], *, timeout: float, remedy: str) -> str:
    """Run one SDK tool, bounded, and turn every failure into a remedy.

    Args:
        argv: Full command, with argv[0] an already-resolved executable path.
        timeout: Seconds to allow.
        remedy: What to tell the reader to do about a broken installation --
            :data:`EMULATOR_NOT_FOUND_REMEDY` or :data:`CMDLINE_TOOLS_REMEDY`.

    Returns:
        The tool's stdout. An empty string is a legitimate answer: it means the
        tool ran and had nothing to report.

    Raises:
        SdkToolError: The tool could not be run, timed out, or exited non-zero.
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SdkToolError(
            f"`{' '.join(argv)}` did not finish within {timeout}s. Check for a "
            f"stale {Path(argv[0]).name} process and retry."
        ) from exc
    except OSError as exc:
        # OSError, not FileNotFoundError: with the SDK *root* on PATH the bare
        # name `emulator` resolves to a directory and execve raises
        # PermissionError or IsADirectoryError instead.
        raise SdkToolError(f"`{argv[0]}` could not be run: {exc}. {remedy}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise SdkToolError(f"`{' '.join(argv)}` exited {result.returncode}: {detail}. {remedy}")
    return result.stdout
