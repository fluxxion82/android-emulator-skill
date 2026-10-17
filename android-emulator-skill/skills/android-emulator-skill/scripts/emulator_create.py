#!/usr/bin/env python3
"""
Create Android Virtual Devices (AVDs) dynamically.

This script creates new AVDs with specified device type and Android version.
Useful for CI/CD pipelines that need on-demand test device provisioning.

Key features:
- Create by device type (Pixel 7, Pixel Tablet, etc.)
- Specify Android version (API 33, API 34, etc.)
- Custom device naming
- Return newly created AVD name
- List available device types and system images

Usage Examples:
    # Create Pixel 7 with Android 34
    python scripts/emulator_create.py --device "Pixel 7" --api 34 --name MyTestDevice

    # Create with everything inferred: latest installed API + auto name,
    # and fuzzy device matching ("pixel7" -> pixel_7).
    python scripts/emulator_create.py --device pixel7

    # List available devices
    python scripts/emulator_create.py --list-devices

    # List available system images
    python scripts/emulator_create.py --list-images

Tunables (env, ANDROID_EMU_ prefix):
    ANDROID_EMU_DEVICE_MATCH_CUTOFF   Fuzzy --device match cutoff 0-100 (default: 60)
    ANDROID_EMU_DEVICE_MATCH_SUGGEST  Max suggestions to show on no-match (default: 5)
"""

import argparse
import difflib
import json
import platform
import re
import subprocess
import sys

from common import cli
from common.env_config import env_int
from common.sdk_tools import (
    CMDLINE_TOOLS_REMEDY,
    CMDLINE_TOOLS_SUBDIRS,
    SdkToolError,
    missing_cmdline_tool_error,
    resolve_sdk_tool,
    run_sdk_tool,
    without_null_lines,
)

# Tunables (overridable via ANDROID_EMU_* env vars; see module docstring).
# Cutoff is expressed 0-100 for a friendly CLI/env surface and normalised to the
# 0.0-1.0 ratio that difflib expects.
DEVICE_MATCH_CUTOFF = env_int("ANDROID_EMU_DEVICE_MATCH_CUTOFF", 60, min_value=0)
DEVICE_MATCH_SUGGEST = env_int("ANDROID_EMU_DEVICE_MATCH_SUGGEST", 5, min_value=1)

# avdmanager and sdkmanager are Android SDK tools, not adb, so they do not go
# through common.adb_exec. They still need a ceiling: an unbounded call wedges
# the caller with no diagnosis. `sdkmanager --list` fetches the remote package
# index, so it gets the longer budget; every other call here is local.
SDK_TOOL_TIMEOUT = 120
SDK_LIST_TIMEOUT = 300

# What platform.machine() calls a 64-bit ARM host: macOS says `arm64`, Linux
# `aarch64`. The emulator on such a host boots only arm64-v8a images, so an
# x86_64 default asks for an image it cannot run (L11).
ARM64_MACHINES = frozenset({"arm64", "aarch64"})
ARM64_ABI = "arm64-v8a"
X86_64_ABI = "x86_64"

# The API level the "nothing installed" remedy suggests downloading. A
# suggestion only -- the ABI in it is this host's, which is what L11 was about.
SUGGESTED_API_LEVEL = 34


def host_abi() -> str:
    """The system-image ABI this host's emulator runs: arm64-v8a on ARM, else x86_64.

    Asked of ``platform.machine()`` on every call rather than fixed at import.
    The default was the constant ``x86_64``, so on an arm64 Mac -- where this
    skill is developed, and the first command of the documented CI flow --
    every create without ``--abi`` asked for an image the host cannot boot, and
    both remedies told the user to install one (L11). x86_64 stays the answer
    everywhere else, which is what CI's x86_64 runners need.
    """
    return ARM64_ABI if platform.machine().lower() in ARM64_MACHINES else X86_64_ABI


def _normalize_device_token(value: str) -> str:
    """Lowercase and strip non-alphanumerics so 'Pixel 7' ~= 'pixel_7' ~= 'pixel7'."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def fuzzy_match_device(query: str, devices: list, cutoff: int = DEVICE_MATCH_CUTOFF) -> str | None:
    """
    Resolve a user-supplied device string to a concrete device definition id.

    Matching is forgiving and ordered by confidence:
      1. Exact id match (case-insensitive).
      2. Exact normalized match against id or name ('Pixel 7' -> 'pixel_7').
      3. Substring match against id or name.
      4. difflib fuzzy ratio against id/name above ``cutoff`` (0-100 scale).

    Args:
        query: User-supplied --device value (id, name, or close approximation).
        devices: Device definitions as returned by list_device_definitions().
        cutoff: Minimum fuzzy score (0-100) to accept a fuzzy match.

    Returns:
        The matched device id, or None when nothing clears the cutoff.
    """
    if not query or not devices:
        return None

    q_norm = _normalize_device_token(query)
    q_lower = query.strip().lower()

    # 1. Exact id (case-insensitive).
    for device in devices:
        if device.get("id", "").lower() == q_lower:
            return device["id"]

    # 2. Exact normalized match against id or name.
    for device in devices:
        dev_id = device.get("id", "")
        name = device.get("name", "")
        if q_norm and q_norm in (_normalize_device_token(dev_id), _normalize_device_token(name)):
            return dev_id

    # 3. Substring match (normalized) against id or name.
    if q_norm:
        for device in devices:
            dev_id = device.get("id", "")
            name = device.get("name", "")
            if q_norm in _normalize_device_token(dev_id) or q_norm in _normalize_device_token(name):
                return dev_id

    # 4. difflib fuzzy ratio across both id and name; keep the best.
    best_id: str | None = None
    best_score = 0.0
    ratio_cutoff = cutoff / 100.0
    for device in devices:
        dev_id = device.get("id", "")
        candidates = [_normalize_device_token(dev_id)]
        name = device.get("name", "")
        if name:
            candidates.append(_normalize_device_token(name))
        for candidate in candidates:
            if not candidate:
                continue
            score = difflib.SequenceMatcher(None, q_norm, candidate).ratio()
            if score > best_score:
                best_score = score
                best_id = dev_id

    if best_id is not None and best_score >= ratio_cutoff:
        return best_id
    return None


def suggest_devices(query: str, devices: list, limit: int = DEVICE_MATCH_SUGGEST) -> list:
    """Return up to ``limit`` device ids closest to ``query`` for error messages."""
    if not devices:
        return []
    ids = [d.get("id", "") for d in devices if d.get("id")]
    q_norm = _normalize_device_token(query)
    scored = sorted(
        ids,
        key=lambda d: difflib.SequenceMatcher(None, q_norm, _normalize_device_token(d)).ratio(),
        reverse=True,
    )
    return scored[:limit]


# sdkmanager prints its listings in TWO formats, depending on which program
# answers to the name. Both are recorded, and this one parser reads both.
#
# The Java SdkManagerCli -- every cmdline-tools before 23, and the one the CI
# runner image runs -- prints a pipe table keyed by the SEMICOLON install id
# (sdkmanager_java_list, sdkmanager_java_list_installed):
#
#   Installed packages:
#     Path                                        | Version | Description | Location
#     system-images;android-34;default;arm64-v8a  | 4       | ARM 64 ...  | system-images/...
#   Available Packages:
#     system-images;android-36;google_apis;x86_64 | 1       | Google APIs ...
#   Available Updates:
#     ID       | Installed | Available
#     emulator | 37.1.11   | 37.2.12
#
# cmdline-tools 23's `bin/sdkmanager` is a wrapper that hands the command to
# the Android CLI, which prints slash PATHS in whitespace-padded columns, with
# no pipe and no semicolon (sdkmanager_list, sdkmanager_list_installed):
#
#   Installed packages:
#     system-images/android-34/google_apis/arm64-v8a   14.0.0   Google APIs ...
#   Available packages:
#     system-images/android-36/google_apis/x86_64      13.0.0   Google APIs ...
#
# Each format has in turn been taken for the only one. The first parser read
# only the pipe table, so with the wrapper both listings matched nothing
# (19325db). Its replacement read only the slash rows, under a comment saying
# the pipe table had never existed -- so on the runner, which had just booted
# from an installed image, `--list_installed` answered "none installed".
SYSTEM_IMAGE_PREFIX = "system-images/"
SYSTEM_IMAGE_ID_PREFIX = "system-images;"
_INSTALLED_HEADER = "installed packages"
_AVAILABLE_HEADER = "available packages"
# The Java tool's third section. Its rows are packages that are ALREADY
# installed and have a newer version, so none of them is an image to download.
_UPDATES_HEADER = "available updates"
_SECTION_HEADERS = (_INSTALLED_HEADER, _AVAILABLE_HEADER, _UPDATES_HEADER)


def _system_image_id(line: str) -> str | None:
    """The semicolon install id a listing row names, or None for any other row.

    A Java row is ``<id> | <version> | ...``; a wrapper row is ``<path>
    <version> ...`` with slashes, converted to the id that ``sdkmanager '<id>'``
    and ``avdmanager --package`` take.
    """
    stripped = line.strip()
    if stripped.startswith(SYSTEM_IMAGE_ID_PREFIX):
        return stripped.split("|", 1)[0].split()[0]
    if stripped.startswith(SYSTEM_IMAGE_PREFIX):
        return stripped.split()[0].replace("/", ";")
    return None


def parse_system_images(text: str) -> list:
    """
    Parse the system-image rows out of an sdkmanager listing, in either format.

    Args:
        text: stdout of ``sdkmanager --list`` or ``sdkmanager --list_installed``,
            from the Java tool or from cmdline-tools 23's wrapper.

    Returns:
        One dict per system-image row, in the order printed:

        - ``id``: the install id, semicolons not slashes -- what
          ``sdkmanager '<id>'`` and ``avdmanager --package`` want.
        - ``api``: the API token verbatim, minus the ``android-`` prefix.
          Not always an integer: ``34-ext12``, ``36.1``, ``37.2-beta1``,
          ``CANARY`` and ``UpsideDownCakePrivacySandbox`` are all real rows in
          the recordings.
        - ``api_level``: that token as an int when it is a bare integer, else
          None. Deliberately strict, because ``create()`` builds its package id
          as ``system-images;android-{api_level};...`` -- calling ``34-ext12``
          "API 34" would name an image that is not installed.
        - ``variant`` / ``abi``.
        - ``installed``: True while the rows are under ``Installed packages:``.
          ``--list_installed`` prints only that section; ``--list`` prints the
          installed one first and then everything downloadable. Rows under the
          Java tool's ``Available Updates:`` are skipped: they repeat packages
          already listed as installed.
    """
    images = []
    section = _INSTALLED_HEADER
    for line in text.splitlines():
        lowered = line.strip().lower()
        header = next((name for name in _SECTION_HEADERS if lowered.startswith(name)), None)
        if header is not None:
            section = header
            continue
        if section == _UPDATES_HEADER:
            continue
        image_id = _system_image_id(line)
        if image_id is None:
            continue
        parts = image_id.split(";")
        if len(parts) != 4:
            continue
        _, api_token, variant, abi = parts
        bare_integer = re.fullmatch(r"android-(\d+)", api_token)
        images.append(
            {
                "id": image_id,
                "api": api_token.removeprefix("android-"),
                "api_level": int(bare_integer.group(1)) if bare_integer else None,
                "variant": variant,
                "abi": abi,
                "installed": section == _INSTALLED_HEADER,
            }
        )
    return images


def latest_api_level(images: list) -> int | None:
    """
    Pick the highest API level from a list of (installed) system-image dicts.

    Args:
        images: System images as returned by list_system_images()/installed images,
            each with an integer ``api_level`` key.

    Returns:
        The highest api_level, or None when the list is empty.
    """
    levels = [img["api_level"] for img in images if isinstance(img.get("api_level"), int)]
    return max(levels) if levels else None


def generate_avd_name(device_id: str, api_level: int) -> str:
    """
    Build a sensible, filesystem-safe AVD name from a device id and API level.

    Examples:
        ("pixel_7", 34) -> "pixel_7_API_34"
        ("Pixel 7 Pro", 33) -> "Pixel_7_Pro_API_33"

    Args:
        device_id: Device definition id (or name).
        api_level: Android API level.

    Returns:
        A name containing only [A-Za-z0-9_], collapsing runs of separators.
    """
    base = re.sub(r"[^A-Za-z0-9]+", "_", device_id.strip()).strip("_")
    base = base or "AVD"
    return f"{base}_API_{api_level}"


class EmulatorCreator:
    """Create Android AVDs with specified configurations."""

    def __init__(self):
        """Initialize emulator creator."""
        # What the last listing could not include, said rather than dropped:
        # a degraded `--list-images` must not read as a complete one.
        self.warnings: list[str] = []

    def get_avdmanager_path(self) -> str | None:
        """
        Find avdmanager: PATH, then the SDK's command-line-tools directories.

        Returns:
            Path to an executable avdmanager, or None if not found.
        """
        return resolve_sdk_tool("avdmanager", CMDLINE_TOOLS_SUBDIRS)

    def get_sdkmanager_path(self) -> str | None:
        """
        Find sdkmanager: PATH, then the SDK's command-line-tools directories.

        Returns:
            Path to an executable sdkmanager, or None if not found.
        """
        return resolve_sdk_tool("sdkmanager", CMDLINE_TOOLS_SUBDIRS)

    def require_avdmanager(self) -> str:
        """avdmanager's path, or :class:`SdkToolError` naming where it was looked for."""
        path = self.get_avdmanager_path()
        if not path:
            raise missing_cmdline_tool_error("avdmanager")
        return path

    def require_sdkmanager(self) -> str:
        """sdkmanager's path, or :class:`SdkToolError` naming where it was looked for."""
        path = self.get_sdkmanager_path()
        if not path:
            raise missing_cmdline_tool_error("sdkmanager")
        return path

    def list_device_definitions(self) -> list:
        """
        List available device definitions.

        Every one of the listings here used to answer a missing or failing
        tool with ``[]`` at exit 0 -- ``--list-devices`` printed nothing, and
        a create reported its ``--device`` "not found" when the truth was that
        nothing had been asked (X3). An empty list now means avdmanager ran
        and defines no devices.

        Returns:
            List of device definition dicts.

        Raises:
            SdkToolError: avdmanager is absent, timed out, or exited non-zero.
        """
        avdmanager = self.require_avdmanager()
        stdout = run_sdk_tool(
            [avdmanager, "list", "device"], timeout=SDK_TOOL_TIMEOUT, remedy=CMDLINE_TOOLS_REMEDY
        )

        devices = []
        current_device = {}

        for line in stdout.split("\n"):
            line = line.strip()
            if line.startswith("id:"):
                if current_device:
                    devices.append(current_device)
                # `id: 53 or "pixel_9"` is TWO identifiers for one device,
                # not one. This used to keep the whole tail as the id and
                # pass it to `avdmanager --device`, which answered
                # `No device found matching --device 53 or "pixel_9"` --
                # echoing back the string it had been handed. Recorded as
                # avdmanager_list_device.
                raw = line.split(":", 1)[1].strip()
                numeric, _, quoted = raw.partition(" or ")
                current_device = {
                    "id": quoted.strip().strip('"') or numeric.strip(),
                    "index": numeric.strip(),
                    "raw_id": raw,
                }
            elif line.startswith("Name:"):
                current_device["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("OEM"):
                current_device["oem"] = line.split(":", 1)[1].strip()

        if current_device:
            devices.append(current_device)

        return devices

    def list_system_images(self) -> list:
        """
        List every system image sdkmanager knows about, flagging the local ones.

        ``sdkmanager --list`` is the only command that answers both halves of
        the question in one call: it prints the installed section first and then
        every downloadable package, so each entry carries ``installed`` and the
        caller can tell "I can create this now" from "this needs a download".

        The cost is that ``--list`` refreshes the remote package index: measured
        at ~4s warm here, and it is the reason this call gets SDK_LIST_TIMEOUT
        rather than SDK_TOOL_TIMEOUT. When it fails -- offline, or a repository
        that will not answer -- we fall back to the purely local
        ``--list_installed``, because a short true answer ("here is what you
        already have") beats none; the fallback is recorded in
        :attr:`warnings`, so the short answer does not pass for the whole one.

        Returns:
            List of system image dicts, as parse_system_images() shapes them.

        Raises:
            SdkToolError: sdkmanager is absent, or neither listing could be read.
        """
        sdkmanager = self.require_sdkmanager()
        try:
            stdout = run_sdk_tool(
                [sdkmanager, "--list"], timeout=SDK_LIST_TIMEOUT, remedy=CMDLINE_TOOLS_REMEDY
            )
        except SdkToolError as error:
            images = self.list_installed_system_images()
            self.warnings.append(
                f"only the installed system images are listed: `sdkmanager --list` failed "
                f"({error})"
            )
            return images

        return parse_system_images(stdout)

    def list_installed_system_images(self) -> list:
        """
        List only the system images that are already installed locally.

        Uses ``sdkmanager --list_installed`` so that ``--api`` inference defaults
        to a level the user can actually create an AVD with (no download needed).

        Returns:
            List of system image dicts (same shape as list_system_images()).
            Empty means sdkmanager ran and has no system image installed.

        Raises:
            SdkToolError: sdkmanager is absent, timed out, or exited non-zero.
                Not an empty list: ``create()`` checks its image against this,
                and an empty answer from a tool that never ran is how that
                check came to be skipped (N3).
        """
        sdkmanager = self.require_sdkmanager()
        stdout = run_sdk_tool(
            [sdkmanager, "--list_installed"], timeout=SDK_TOOL_TIMEOUT, remedy=CMDLINE_TOOLS_REMEDY
        )

        # Same rows, same parser as `--list`: `--list_installed` prints only the
        # `Installed packages:` section, so everything it yields is installed.
        return parse_system_images(stdout)

    def resolve_api_level(self, requested: int | None) -> int | None:
        """
        Resolve the API level to use, defaulting to the latest installed image.

        Args:
            requested: Explicit --api value, or None to infer.

        Returns:
            The requested level when provided, otherwise the highest installed
            API level, or None when nothing is installed/detectable.
        """
        if requested is not None:
            return requested
        return latest_api_level(self.list_installed_system_images())

    def resolve_device(self, requested: str) -> tuple:
        """
        Resolve a (possibly fuzzy) --device string to a concrete device id.

        Args:
            requested: User-supplied --device value.

        Returns:
            (device_id, suggestions) tuple. device_id is None when no match
            clears the fuzzy cutoff; suggestions lists close ids for the error.
        """
        devices = self.list_device_definitions()
        matched = fuzzy_match_device(requested, devices)
        if matched:
            return matched, []
        return None, suggest_devices(requested, devices)

    def create(
        self,
        device_id: str,
        api_level: int,
        name: str,
        abi: str | None = None,
        variant: str = "google_apis",
    ) -> tuple:
        """
        Create new AVD.

        Args:
            device_id: Device definition ID (e.g., "pixel_7")
            api_level: Android API level (e.g., 33, 34)
            name: AVD name
            abi: ABI type (x86_64, x86, arm64-v8a); None means this host's,
                as :func:`host_abi` reads it.
            variant: System image variant (google_apis, default, google_apis_playstore)

        Returns:
            (success, message, avd_name) tuple

        Raises:
            SdkToolError: avdmanager or sdkmanager is absent or failed. Without
                sdkmanager nobody can say the image is installed, so there is
                no create -- this used to skip the check and run avdmanager
                against an image nobody had verified (N3).
        """
        avdmanager = self.require_avdmanager()

        # Build system image path
        system_image = f"system-images;android-{api_level};{variant};{abi or host_abi()}"

        # Check the image is installed, against the LOCAL package list.
        #
        # This used to run `sdkmanager --list` and ask whether the semicolon
        # form appeared anywhere in its stdout. With cmdline-tools 23's wrapper
        # it never does: that --list prints paths with slashes (the Java tool's
        # does print it -- see parse_system_images). So every image looked
        # missing and no AVD could be created -- with an error telling the user to install
        # something they already had. `--list` also reaches the network, so the
        # check was slow and failed offline for the wrong reason.
        #
        # Unconditional: it was `if installed and ...`, so an sdkmanager that
        # answered nothing -- because it was missing -- skipped the check (N3).
        # A missing one raises now, and an empty listing is a real "none".
        installed = self.list_installed_system_images()
        if system_image not in {image["id"] for image in installed}:
            available = sorted(image["id"] for image in installed)
            return (
                False,
                f"System image not installed: {system_image}\n"
                f"Install with: sdkmanager '{system_image}'\n"
                f"Or use one you already have: {', '.join(available) or '(none installed)'}",
                None,
            )

        # Create AVD
        cmd = [
            avdmanager,
            "create",
            "avd",
            "--name",
            name,
            "--package",
            system_image,
            "--device",
            device_id,
        ]

        try:
            # Use 'no' to decline custom hardware profile
            subprocess.run(
                cmd,
                input="no\n",
                capture_output=True,
                text=True,
                timeout=SDK_TOOL_TIMEOUT,
                check=True,
            )

            return True, f"AVD created: {name}", name

        except subprocess.CalledProcessError as e:
            error_msg = without_null_lines(e.stderr or "") or str(e)
            return False, f"Failed to create AVD: {error_msg}", None

        except subprocess.TimeoutExpired:
            return (
                False,
                f"avdmanager did not finish creating {name} within {SDK_TOOL_TIMEOUT}s. "
                f"Check for a stale avdmanager process and retry.",
                None,
            )

    def delete(self, name: str) -> tuple:
        """
        Delete an AVD.

        Args:
            name: AVD name to delete

        Returns:
            (success, message) tuple
        """
        avdmanager = self.get_avdmanager_path()
        if not avdmanager:
            return (
                False,
                "avdmanager not found. Ensure Android SDK is installed and ANDROID_HOME is set.",
            )

        cmd = [avdmanager, "delete", "avd", "--name", name]

        try:
            subprocess.run(
                cmd, capture_output=True, text=True, timeout=SDK_TOOL_TIMEOUT, check=True
            )
            return True, f"AVD deleted: {name}"

        except subprocess.CalledProcessError as e:
            # avdmanager's reason, minus the bare `null` line it prints after
            # it (recorded: avdmanager_delete_missing) -- N13.
            error_msg = without_null_lines(e.stderr or "") or str(e)
            return False, f"Failed to delete AVD: {error_msg}"

        except subprocess.TimeoutExpired:
            return (
                False,
                f"avdmanager did not finish deleting {name} within {SDK_TOOL_TIMEOUT}s. "
                f"Check for a stale avdmanager process and retry.",
            )


# What to do about a create that did not happen: an image that is not
# installed, or avdmanager refusing the definition, name or image.
CREATE_REMEDY = (
    "Fix what the error names, then retry: `emulator_create.py --list-images` shows the "
    "installed images (pick one with --api, --abi or --variant) and `--list-devices` "
    "the device definitions."
)


def print_system_images(images: list) -> None:
    """
    Print a system-image listing that stays readable at 300+ entries.

    ``sdkmanager --list`` knows about hundreds of images and typically four of
    them are installed. One line each would bury the answer to the question
    actually being asked -- "what can I boot right now" -- so the installed ones
    are listed in full and the rest are folded to one line per API level. The
    whole list, unfolded, is what ``--json`` is for.

    Args:
        images: Dicts as parse_system_images() shapes them.
    """
    installed = [image for image in images if image["installed"]]
    available = [image for image in images if not image["installed"]]

    print(f"System Images: {len(installed)} installed, {len(images)} known to sdkmanager")

    if installed:
        print("Installed:")
        for image in installed:
            print(f"  API {image['api']}: {image['variant']} ({image['abi']}) - {image['id']}")

    if available:
        by_api: dict[str, dict[str, list]] = {}
        for image in available:
            by_api.setdefault(image["api"], {}).setdefault(image["variant"], []).append(
                image["abi"]
            )
        print(f"Available to install ({len(available)}; use --json for the full list):")
        for api, variants in by_api.items():
            detail = ", ".join(f"{v} ({', '.join(abis)})" for v, abis in variants.items())
            print(f"  API {api}: {detail}")


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Create Android Virtual Devices (AVDs)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create Pixel 7 with Android 34
  python scripts/emulator_create.py --device pixel_7 --api 34 --name MyTestDevice

  # Infer everything: fuzzy device, latest installed API, auto-generated name
  python scripts/emulator_create.py --device pixel7

  # Create with specific ABI
  python scripts/emulator_create.py --device pixel_7 --api 33 --abi x86_64 --name Test

  # List available devices
  python scripts/emulator_create.py --list-devices

  # List available system images
  python scripts/emulator_create.py --list-images
        """,
    )

    # List operations
    list_group = parser.add_argument_group("List Options")
    list_group.add_argument(
        "--list-devices", action="store_true", help="List available device definitions"
    )
    list_group.add_argument(
        "--list-images", action="store_true", help="List available system images"
    )

    # Create operations
    create_group = parser.add_argument_group("Create Options")
    create_group.add_argument(
        "--device", help="Device definition ID or name; fuzzy-matched (e.g., pixel_7, 'Pixel 7')"
    )
    create_group.add_argument(
        "--api",
        type=int,
        help="Android API level (e.g., 33, 34). Defaults to latest installed system image.",
    )
    create_group.add_argument(
        "--name", help="AVD name. Defaults to an auto-generated name from device + API."
    )
    # The default is resolved here, at run time, so `--help` states the ABI this
    # host will actually get rather than a constant that was wrong on ARM (L11).
    create_group.add_argument(
        "--abi",
        default=host_abi(),
        help=(
            f"System-image ABI (default: {host_abi()}, the one this "
            f"{platform.machine() or 'unknown'} host's emulator runs)"
        ),
    )
    create_group.add_argument(
        "--variant", default="google_apis", help="System image variant (default: google_apis)"
    )

    # Output options
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    creator = EmulatorCreator()

    # List operations
    if args.list_devices:
        devices = creator.list_device_definitions()
        if args.json:
            print(json.dumps({"devices": devices}, indent=2))
        else:
            print("Available Device Definitions:")
            for device in devices:
                print(f"  {device.get('id', 'unknown')}: {device.get('name', 'N/A')}")
        sys.exit(0)

    if args.list_images:
        images = creator.list_system_images()
        if args.json:
            print(json.dumps({"system_images": images, "warnings": creator.warnings}, indent=2))
        else:
            for warning in creator.warnings:
                print(f"Warning: {warning}", file=sys.stderr)
            print_system_images(images)
        sys.exit(0)

    # Create operation. Only --device is strictly required now: --api defaults to
    # the latest installed system image and --name is auto-generated from device+API.
    # Every refusal below goes through common.cli: they printed
    # `{"success": false, "message": ...}` -- no `error`, no remedy -- and the
    # missing --device printed the help text to stdout, the whole of what a
    # --json caller got.
    if not args.device:
        if not args.json:
            parser.print_help(file=sys.stderr)
        cli.fail(
            "--device is required (or use --list-devices / --list-images)",
            "Pass --device <definition>, e.g. --device pixel_7; `--list-devices` lists them.",
        )

    # Delta 3: fuzzy-resolve --device against avdmanager device definitions.
    device_id, suggestions = creator.resolve_device(args.device)
    if not device_id:
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        cli.fail(
            f"Device '{args.device}' not found.{hint}",
            "Pass a definition `emulator_create.py --list-devices` lists (its id or name).",
        )

    # Delta 1: default --api to the latest installed system-image API level.
    api_level = creator.resolve_api_level(args.api)
    if api_level is None:
        # The suggestion carries the ABI this create would use -- the host's
        # unless --abi said otherwise. It was a hard-coded x86_64 image, which
        # an arm64 host cannot boot (L11).
        suggested = f"system-images;android-{SUGGESTED_API_LEVEL};{args.variant};{args.abi}"
        cli.fail(
            "Could not determine an API level: sdkmanager lists no installed system "
            "image with one.",
            f"Pass --api explicitly or install one (sdkmanager '{suggested}').",
        )

    # Delta 2: auto-generate an AVD name from device + API when --name is omitted.
    name = args.name or generate_avd_name(device_id, api_level)

    success, message, avd_name = creator.create(
        device_id=device_id,
        api_level=api_level,
        name=name,
        abi=args.abi,
        variant=args.variant,
    )

    if not success:
        # The image check's refusal names the image and the installed set;
        # avdmanager's names its own reason. Either way the message is the
        # error, and what to do about it is the same.
        cli.fail(message, CREATE_REMEDY)

    if args.json:
        print(
            json.dumps(
                {
                    "success": True,
                    "message": message,
                    "avd_name": avd_name,
                    "device_id": device_id,
                    "api_level": api_level,
                },
                indent=2,
            )
        )
    else:
        print(message)

    sys.exit(0)


if __name__ == "__main__":
    cli.run(main)
