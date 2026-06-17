#!/usr/bin/env python3
"""
Common utilities for Android emulator/device automation.

Shared modules:
- device_utils: ADB command building and device detection
- screenshot_utils: Screenshot capture and processing
- cache_utils: Progressive disclosure cache system
"""

from .cache_utils import ProgressiveCache, get_cache
from .device_utils import (
    build_adb_command,
    get_connected_devices,
    get_device_screen_size,
    list_devices,
    resolve_device_identifier,
)
from .screenshot_utils import (
    capture_screenshot,
    generate_screenshot_name,
    resize_screenshot,
)

__all__ = [
    # Cache utilities
    "ProgressiveCache",
    # Device utilities
    "build_adb_command",
    # Screenshot utilities
    "capture_screenshot",
    "generate_screenshot_name",
    "get_cache",
    "get_connected_devices",
    "get_device_screen_size",
    "list_devices",
    "resize_screenshot",
    "resolve_device_identifier",
]
