#!/usr/bin/env python3
"""
Android Gesture Simulator

Performs swipes, scrolls, pinches, and complex touch gestures on Android devices/emulators.

Key Features:
- Directional swipes (up/down/left/right)
- Multi-swipe scrolling
- Pull-to-refresh
- Custom swipe paths
- Long press
- Multi-touch gestures
- Configurable duration and speed

Usage Examples:
    # Swipe up (scroll down content)
    python scripts/gesture.py --swipe up

    # Swipe right from left edge (back gesture)
    python scripts/gesture.py --swipe right --from-edge

    # Scroll up 3 times
    python scripts/gesture.py --scroll up --count 3

    # Long press at center
    python scripts/gesture.py --long-press 540,960 --duration 2000

    # Custom swipe path
    python scripts/gesture.py --swipe-path 100,500,900,500 --duration 300

    # Pull to refresh
    python scripts/gesture.py --refresh

Output Format:
    Swiped: up (540,1600) → (540,400) [300ms]
    Scrolled: down (3 swipes)
    Long pressed: (540, 960) for 2000ms
    Pulled to refresh: (540,720) → (540,1920) [600ms]
"""

import argparse
import sys
import time

from common import adb_exec
from common.device_utils import get_device_screen_size, resolve_device_identifier
from common.env_config import env_float, env_int

# Tunable pull-to-refresh defaults (overridable via ANDROID_EMU_* env vars).
REFRESH_START_PCT = env_float("ANDROID_EMU_REFRESH_START_PCT", 0.30)
REFRESH_END_PCT = env_float("ANDROID_EMU_REFRESH_END_PCT", 0.80)
REFRESH_DURATION_MS = env_int("ANDROID_EMU_REFRESH_DURATION_MS", 600)


def _gesture_timeout(duration_ms: int) -> int:
    """Time budget for an ``input swipe`` that itself lasts ``duration_ms``.

    ``input swipe`` blocks for the whole gesture, so a long drag would trip the
    default adb budget while working perfectly. Allow the gesture's own runtime
    on top of it.
    """
    return adb_exec.DEFAULT_TIMEOUT + max(duration_ms, 0) // 1000


class GestureSimulator:
    """Simulates touch gestures on Android devices."""

    # Predefined swipe parameters
    SWIPE_PERCENTAGE = 0.8  # How much of screen to swipe
    EDGE_START_PERCENTAGE = 0.05  # For edge swipes

    def __init__(self, serial: str | None = None):
        """Initialize gesture simulator."""
        self.serial = serial
        self._screen_size = None

    def get_screen_size(self) -> tuple:
        """Get or cache screen size."""
        if self._screen_size is None:
            self._screen_size = get_device_screen_size(self.serial)
        return self._screen_size

    def swipe(
        self,
        direction: str,
        from_edge: bool = False,
        duration_ms: int = 300,
    ) -> tuple:
        """
        Perform directional swipe.

        Args:
            direction: 'up', 'down', 'left', or 'right'
            from_edge: Start swipe from screen edge
            duration_ms: Swipe duration in milliseconds

        Returns:
            (success, message) tuple
        """
        width, height = self.get_screen_size()
        center_x = width // 2
        center_y = height // 2

        # Calculate swipe start and end points
        if direction == "up":
            # Swipe up = scroll down
            start_x = center_x
            start_y = int(height * 0.8) if not from_edge else int(height * 0.95)
            end_x = center_x
            end_y = int(height * 0.2)
        elif direction == "down":
            # Swipe down = scroll up
            start_x = center_x
            start_y = int(height * 0.2) if not from_edge else int(height * 0.05)
            end_x = center_x
            end_y = int(height * 0.8)
        elif direction == "left":
            # Swipe left = go forward/next
            start_x = int(width * 0.8) if not from_edge else int(width * 0.95)
            start_y = center_y
            end_x = int(width * 0.2)
            end_y = center_y
        elif direction == "right":
            # Swipe right = go back/previous
            start_x = int(width * 0.2) if not from_edge else int(width * 0.05)
            start_y = center_y
            end_x = int(width * 0.8)
            end_y = center_y
        else:
            return False, f"Invalid direction: {direction}. Use: up, down, left, right"

        return self.swipe_path(start_x, start_y, end_x, end_y, duration_ms)

    def swipe_path(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> tuple:
        """
        Perform swipe along custom path.

        Args:
            x1, y1: Start coordinates
            x2, y2: End coordinates
            duration_ms: Swipe duration in milliseconds

        Returns:
            (success, message) tuple
        """
        try:
            adb_exec.run_adb(
                "shell",
                self.serial,
                "input",
                "swipe",
                str(x1),
                str(y1),
                str(x2),
                str(y2),
                str(duration_ms),
                timeout=_gesture_timeout(duration_ms),
                check=True,
            )
            return True, f"Swiped: ({x1},{y1}) → ({x2},{y2}) [{duration_ms}ms]"
        except adb_exec.AdbCommandError as e:
            return False, f"Swipe failed: {e}"

    def scroll(
        self,
        direction: str,
        count: int = 1,
        duration_ms: int = 300,
    ) -> tuple:
        """
        Perform multiple swipes for scrolling.

        Args:
            direction: 'up' or 'down'
            count: Number of swipes
            duration_ms: Duration per swipe

        Returns:
            (success, message) tuple
        """
        if direction not in ["up", "down"]:
            return False, "Scroll direction must be 'up' or 'down'"

        for i in range(count):
            success, message = self.swipe(direction, duration_ms=duration_ms)
            if not success:
                return False, f"Scroll failed on swipe {i+1}: {message}"
            # Small delay between swipes
            if i < count - 1:
                time.sleep(0.1)

        return True, f"Scrolled: {direction} ({count} swipes)"

    def long_press(
        self,
        x: int,
        y: int,
        duration_ms: int = 1000,
    ) -> tuple:
        """
        Perform long press at coordinates.

        Args:
            x, y: Coordinates to press
            duration_ms: Press duration in milliseconds

        Returns:
            (success, message) tuple
        """
        # Long press is a swipe from point to same point with duration
        try:
            adb_exec.run_adb(
                "shell",
                self.serial,
                "input",
                "swipe",
                str(x),
                str(y),
                str(x),
                str(y),
                str(duration_ms),
                timeout=_gesture_timeout(duration_ms),
                check=True,
            )
            return True, f"Long pressed: ({x}, {y}) for {duration_ms}ms"
        except adb_exec.AdbCommandError as e:
            return False, f"Long press failed: {e}"

    def refresh(
        self,
        duration_ms: int = REFRESH_DURATION_MS,
    ) -> tuple:
        """
        Perform a pull-to-refresh gesture.

        Swipes downward from near the top of the screen to lower down,
        mimicking the standard Android "pull down to refresh" interaction.
        The start/end vertical positions and duration are tunable via the
        ANDROID_EMU_REFRESH_* environment variables.

        Args:
            duration_ms: Swipe duration in milliseconds (slow enough to
                register as a refresh, not a fling)

        Returns:
            (success, message) tuple
        """
        width, height = self.get_screen_size()
        center_x = width // 2
        start_y = int(height * REFRESH_START_PCT)
        end_y = int(height * REFRESH_END_PCT)

        success, _ = self.swipe_path(center_x, start_y, center_x, end_y, duration_ms)
        if not success:
            return False, "Pull-to-refresh failed"
        return (
            True,
            f"Pulled to refresh: ({center_x},{start_y}) → ({center_x},{end_y}) [{duration_ms}ms]",
        )

    def pinch(
        self,
        direction: str,
        x: int | None = None,
        y: int | None = None,
    ) -> tuple:
        """
        Perform pinch gesture (zoom in/out).

        Note: Android input command doesn't have native pinch support.
        This is a placeholder for when using UI Automator 2.0 or other methods.

        Args:
            direction: 'in' (zoom out) or 'out' (zoom in)
            x, y: Center point (uses screen center if None)

        Returns:
            (success, message) tuple
        """
        if x is None or y is None:
            width, height = self.get_screen_size()
            x = width // 2 if x is None else x
            y = height // 2 if y is None else y
        return (
            False,
            f"Pinch {direction} at ({x}, {y}) requires UI Automator 2.0 or Appium "
            "(not yet implemented)",
        )

    def drag_and_drop(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 1000,
    ) -> tuple:
        """
        Perform drag and drop gesture.

        Args:
            start_x, start_y: Start coordinates
            end_x, end_y: End coordinates
            duration_ms: Drag duration

        Returns:
            (success, message) tuple
        """
        # Drag is just a long swipe
        return self.swipe_path(start_x, start_y, end_x, end_y, duration_ms)


def main():
    parser = argparse.ArgumentParser(
        description="Android touch gesture simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Swipe up (scroll down content)
  python gesture.py --swipe up

  # Swipe from edge (back gesture)
  python gesture.py --swipe right --from-edge

  # Scroll up 3 times
  python gesture.py --scroll up --count 3

  # Long press at coordinates
  python gesture.py --long-press 540,960 --duration 2000

  # Custom swipe path
  python gesture.py --swipe-path 100,500,900,500

  # Drag and drop
  python gesture.py --drag 100,500,900,500 --duration 1000

  # Pull to refresh
  python gesture.py --refresh
        """,
    )

    parser.add_argument("--serial", "-s", help="Device serial number (auto-detects if omitted)")
    parser.add_argument(
        "--swipe", choices=["up", "down", "left", "right"], help="Directional swipe"
    )
    parser.add_argument("--from-edge", action="store_true", help="Start swipe from screen edge")
    parser.add_argument("--scroll", choices=["up", "down"], help="Scroll direction")
    parser.add_argument("--count", type=int, default=1, help="Number of scrolls (default: 1)")
    parser.add_argument("--long-press", help="Long press at coordinates (format: x,y)")
    parser.add_argument("--swipe-path", help="Custom swipe path (format: x1,y1,x2,y2)")
    parser.add_argument("--drag", help="Drag and drop (format: x1,y1,x2,y2)")
    parser.add_argument(
        "--refresh", action="store_true", help="Pull-to-refresh gesture (swipe down from top)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help=f"Gesture duration in ms (default: 300; --refresh defaults to {REFRESH_DURATION_MS})",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    args = parser.parse_args()

    # Resolve device
    try:
        serial = resolve_device_identifier(args.serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    simulator = GestureSimulator(serial)

    # Resolve durations: existing gestures keep their 300ms contract; --refresh
    # falls back to its own (tunable) default when --duration is omitted.
    duration = 300 if args.duration is None else args.duration
    refresh_duration = REFRESH_DURATION_MS if args.duration is None else args.duration

    # Execute gesture
    success = False
    message = ""

    try:
        if args.swipe:
            success, message = simulator.swipe(args.swipe, args.from_edge, duration)
        elif args.scroll:
            success, message = simulator.scroll(args.scroll, args.count, duration)
        elif args.long_press:
            try:
                x, y = map(int, args.long_press.split(","))
                success, message = simulator.long_press(x, y, duration)
            except ValueError:
                message = "Error: --long-press requires format 'x,y'"
        elif args.swipe_path:
            try:
                x1, y1, x2, y2 = map(int, args.swipe_path.split(","))
                success, message = simulator.swipe_path(x1, y1, x2, y2, duration)
            except ValueError:
                message = "Error: --swipe-path requires format 'x1,y1,x2,y2'"
        elif args.drag:
            try:
                x1, y1, x2, y2 = map(int, args.drag.split(","))
                success, message = simulator.drag_and_drop(x1, y1, x2, y2, duration)
            except ValueError:
                message = "Error: --drag requires format 'x1,y1,x2,y2'"
        elif args.refresh:
            success, message = simulator.refresh(refresh_duration)
        else:
            parser.print_help()
            sys.exit(1)
    except adb_exec.AdbError as error:
        # The gesture never reached a device. The error names the remedy, so
        # print it rather than letting a traceback bury it.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        import json

        print(json.dumps({"success": success, "message": message}, indent=2))
    else:
        print(message)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
