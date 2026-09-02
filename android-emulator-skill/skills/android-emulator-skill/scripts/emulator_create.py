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
import re
import subprocess
import sys
from pathlib import Path

from common.env_config import env_int

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


# sdkmanager listings -- both `--list` and `--list_installed` -- print package
# PATHS with SLASHES in whitespace-padded columns, under section headers:
#
#   Installed packages:
#     system-images/android-34/google_apis/arm64-v8a   14.0.0   Google APIs ...
#   Available packages:
#     system-images/android-36/google_apis/x86_64      13.0.0   Google APIs ...
#
# There is no pipe anywhere and no semicolon anywhere. Both listings were once
# parsed as `system-images;<id> | <rev> | <desc>`, a format neither command has
# ever printed, so both matched nothing: creation was impossible (fixed in
# 19325db) and `--list-images` printed an empty list (this). One parser now, so
# the two cannot drift apart again. Recorded as sdkmanager_list and
# sdkmanager_list_installed.
SYSTEM_IMAGE_PREFIX = "system-images/"
_INSTALLED_HEADER = "installed packages"
_AVAILABLE_HEADER = "available packages"


def parse_system_images(text: str) -> list:
    """
    Parse the system-image rows out of an sdkmanager listing.

    Args:
        text: stdout of ``sdkmanager --list`` or ``sdkmanager --list_installed``.

    Returns:
        One dict per ``system-images/...`` row, in the order printed:

        - ``id``: the install id, semicolons not slashes -- what
          ``sdkmanager '<id>'`` and ``avdmanager --package`` want.
        - ``api``: the API token verbatim, minus the ``android-`` prefix.
          Not always an integer: ``34-ext12``, ``36.1``, ``37.2-beta1`` and
          ``CANARY`` are all real rows in the recording.
        - ``api_level``: that token as an int when it is a bare integer, else
          None. Deliberately strict, because ``create()`` builds its package id
          as ``system-images;android-{api_level};...`` -- calling ``34-ext12``
          "API 34" would name an image that is not installed.
        - ``variant`` / ``abi``.
        - ``installed``: True while the rows are under ``Installed packages:``.
          ``--list_installed`` prints only that section; ``--list`` prints the
          installed one first and then everything downloadable.
    """
    images = []
    installed = True
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(_AVAILABLE_HEADER):
            installed = False
            continue
        if lowered.startswith(_INSTALLED_HEADER):
            installed = True
            continue
        if not stripped.startswith(SYSTEM_IMAGE_PREFIX):
            continue
        path = stripped.split()[0]
        parts = path.split("/")
        if len(parts) != 4:
            continue
        _, api_token, variant, abi = parts
        bare_integer = re.fullmatch(r"android-(\d+)", api_token)
        images.append(
            {
                "id": path.replace("/", ";"),
                "api": api_token.removeprefix("android-"),
                "api_level": int(bare_integer.group(1)) if bare_integer else None,
                "variant": variant,
                "abi": abi,
                "installed": installed,
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
        pass

    def get_avdmanager_path(self) -> str | None:
        """
        Find avdmanager command.

        Returns:
            Path to avdmanager or None if not found
        """
        # Try common locations
        import shutil

        avdmanager = shutil.which("avdmanager")
        if avdmanager:
            return avdmanager

        # Try ANDROID_HOME
        import os

        android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if android_home:
            possible_paths = [
                Path(android_home) / "cmdline-tools" / "latest" / "bin" / "avdmanager",
                Path(android_home) / "tools" / "bin" / "avdmanager",
            ]
            for path in possible_paths:
                if path.exists():
                    return str(path)

        return None

    def get_sdkmanager_path(self) -> str | None:
        """
        Find sdkmanager command.

        Returns:
            Path to sdkmanager or None if not found
        """
        import shutil

        sdkmanager = shutil.which("sdkmanager")
        if sdkmanager:
            return sdkmanager

        # Try ANDROID_HOME
        import os

        android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if android_home:
            possible_paths = [
                Path(android_home) / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
                Path(android_home) / "tools" / "bin" / "sdkmanager",
            ]
            for path in possible_paths:
                if path.exists():
                    return str(path)

        return None

    def list_device_definitions(self) -> list:
        """
        List available device definitions.

        Returns:
            List of device definition dicts
        """
        avdmanager = self.get_avdmanager_path()
        if not avdmanager:
            return []

        try:
            result = subprocess.run(
                [avdmanager, "list", "device"],
                capture_output=True,
                text=True,
                timeout=SDK_TOOL_TIMEOUT,
                check=True,
            )

            devices = []
            current_device = {}

            for line in result.stdout.split("\n"):
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

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []

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
        ``--list_installed`` rather than returning nothing, because a short true
        answer ("here is what you already have") beats an empty one that reads
        as "no system images exist".

        Returns:
            List of system image dicts, as parse_system_images() shapes them.
        """
        sdkmanager = self.get_sdkmanager_path()
        if not sdkmanager:
            return []

        try:
            result = subprocess.run(
                [sdkmanager, "--list"],
                capture_output=True,
                text=True,
                timeout=SDK_LIST_TIMEOUT,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return self.list_installed_system_images()

        return parse_system_images(result.stdout)

    def list_installed_system_images(self) -> list:
        """
        List only the system images that are already installed locally.

        Uses ``sdkmanager --list_installed`` so that ``--api`` inference defaults
        to a level the user can actually create an AVD with (no download needed).
        Falls back to an empty list when sdkmanager is unavailable.

        Returns:
            List of system image dicts (same shape as list_system_images()).
        """
        sdkmanager = self.get_sdkmanager_path()
        if not sdkmanager:
            return []

        try:
            result = subprocess.run(
                [sdkmanager, "--list_installed"],
                capture_output=True,
                text=True,
                timeout=SDK_TOOL_TIMEOUT,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []

        # Same rows, same parser as `--list`: `--list_installed` prints only the
        # `Installed packages:` section, so everything it yields is installed.
        return parse_system_images(result.stdout)

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
        abi: str = "x86_64",
        variant: str = "google_apis",
    ) -> tuple:
        """
        Create new AVD.

        Args:
            device_id: Device definition ID (e.g., "pixel_7")
            api_level: Android API level (e.g., 33, 34)
            name: AVD name
            abi: ABI type (x86_64, x86, arm64-v8a)
            variant: System image variant (google_apis, default, google_apis_playstore)

        Returns:
            (success, message, avd_name) tuple
        """
        avdmanager = self.get_avdmanager_path()
        if not avdmanager:
            return (
                False,
                "avdmanager not found. Ensure Android SDK is installed and ANDROID_HOME is set.",
                None,
            )

        # Build system image path
        system_image = f"system-images;android-{api_level};{variant};{abi}"

        # Check the image is installed, against the LOCAL package list.
        #
        # This used to run `sdkmanager --list` and ask whether the semicolon
        # form appeared anywhere in its stdout. It never does: --list prints
        # paths with slashes. So every image looked missing and no AVD could
        # ever be created -- with an error telling the user to install
        # something they already had. `--list` also reaches the network, so the
        # check was slow and failed offline for the wrong reason.
        installed = self.list_installed_system_images()
        if installed and system_image not in {image["id"] for image in installed}:
            available = sorted(image["id"] for image in installed)
            return (
                False,
                f"System image not installed: {system_image}\n"
                f"Install with: sdkmanager '{system_image}'\n"
                f"Or use one you already have: {', '.join(available)}",
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
            error_msg = e.stderr if e.stderr else str(e)
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
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to delete AVD: {error_msg}"

        except subprocess.TimeoutExpired:
            return (
                False,
                f"avdmanager did not finish deleting {name} within {SDK_TOOL_TIMEOUT}s. "
                f"Check for a stale avdmanager process and retry.",
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


def main():
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
    create_group.add_argument("--abi", default="x86_64", help="ABI type (default: x86_64)")
    create_group.add_argument(
        "--variant", default="google_apis", help="System image variant (default: google_apis)"
    )

    # Output options
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

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
            print(json.dumps({"system_images": images}, indent=2))
        else:
            print_system_images(images)
        sys.exit(0)

    # Create operation. Only --device is strictly required now: --api defaults to
    # the latest installed system image and --name is auto-generated from device+API.
    if not args.device:
        print(
            "Error: --device is required (or use --list-devices / --list-images)", file=sys.stderr
        )
        parser.print_help()
        sys.exit(1)

    def _fail(message: str) -> None:
        if args.json:
            print(json.dumps({"success": False, "message": message, "avd_name": None}, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)

    # Delta 3: fuzzy-resolve --device against avdmanager device definitions.
    device_id, suggestions = creator.resolve_device(args.device)
    if not device_id:
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        _fail(
            f"Device '{args.device}' not found.{hint} "
            f"Use --list-devices for available definitions."
        )

    # Delta 1: default --api to the latest installed system-image API level.
    api_level = creator.resolve_api_level(args.api)
    if api_level is None:
        _fail(
            "Could not determine an API level: pass --api explicitly or install a "
            "system image (sdkmanager 'system-images;android-34;google_apis;x86_64')."
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

    if args.json:
        print(
            json.dumps(
                {
                    "success": success,
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

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
