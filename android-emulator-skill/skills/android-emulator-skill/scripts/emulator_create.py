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
                check=True,
            )

            devices = []
            current_device = {}

            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("id:"):
                    if current_device:
                        devices.append(current_device)
                    current_device = {"id": line.split(":", 1)[1].strip()}
                elif line.startswith("Name:"):
                    current_device["name"] = line.split(":", 1)[1].strip()
                elif line.startswith("OEM"):
                    current_device["oem"] = line.split(":", 1)[1].strip()

            if current_device:
                devices.append(current_device)

            return devices

        except subprocess.CalledProcessError:
            return []

    def list_system_images(self) -> list:
        """
        List available system images.

        Returns:
            List of system image dicts
        """
        sdkmanager = self.get_sdkmanager_path()
        if not sdkmanager:
            return []

        try:
            result = subprocess.run(
                [sdkmanager, "--list"],
                capture_output=True,
                text=True,
                check=True,
            )

            images = []
            in_system_images = False

            for line in result.stdout.split("\n"):
                if "system-images" in line and "|" in line:
                    in_system_images = True

                if in_system_images and line.strip().startswith("system-images;"):
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 1:
                        image_id = parts[0]
                        # Parse system-images;android-34;google_apis;x86_64
                        match = re.match(r"system-images;android-(\d+);([^;]+);([^;\s]+)", image_id)
                        if match:
                            api_level, variant, abi = match.groups()
                            images.append(
                                {
                                    "id": image_id,
                                    "api_level": int(api_level),
                                    "variant": variant,
                                    "abi": abi,
                                }
                            )

                # Stop at next section
                if in_system_images and "---" in line and len(images) > 0:
                    break

            return images

        except subprocess.CalledProcessError:
            return []

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
                check=True,
            )
        except subprocess.CalledProcessError:
            return []

        images = []
        for line in result.stdout.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("system-images;"):
                continue
            image_id = stripped.split("|", 1)[0].strip()
            match = re.match(r"system-images;android-(\d+);([^;]+);([^;\s]+)", image_id)
            if match:
                api_level, variant, abi = match.groups()
                images.append(
                    {
                        "id": image_id,
                        "api_level": int(api_level),
                        "variant": variant,
                        "abi": abi,
                    }
                )
        return images

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

        # Check if system image is installed
        sdkmanager = self.get_sdkmanager_path()
        if sdkmanager:
            try:
                result = subprocess.run(
                    [sdkmanager, "--list"],
                    capture_output=True,
                    text=True,
                    check=True,
                )

                if system_image not in result.stdout:
                    return (
                        False,
                        f"System image not installed: {system_image}\n"
                        f"Install with: sdkmanager '{system_image}'",
                        None,
                    )
            except subprocess.CalledProcessError:
                pass  # Continue anyway

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
            result = subprocess.run(
                cmd,
                input="no\n",
                capture_output=True,
                text=True,
                check=True,
            )

            return True, f"AVD created: {name}", name

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to create AVD: {error_msg}", None

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
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, f"AVD deleted: {name}"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return False, f"Failed to delete AVD: {error_msg}"


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
            print("Available System Images:")
            for image in images:
                print(
                    f"  API {image['api_level']}: {image['variant']} ({image['abi']}) - {image['id']}"
                )
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
