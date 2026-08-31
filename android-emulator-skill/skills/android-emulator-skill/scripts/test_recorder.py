#!/usr/bin/env python3
"""
Test Recorder for Android Device/Emulator Testing

Records test execution with automatic screenshots and documentation.
Optimized for minimal token output during execution.

Usage:
    As a script: python scripts/test_recorder.py --test-name "Test Name" --output dir/
    As a module: from scripts.test_recorder import TestRecorder
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from common.device_utils import get_ui_hierarchy, resolve_device_identifier
from common.env_config import env_int
from common.screenshot_utils import capture_screenshot

# Tunable: max chars of a step description used when building artifact filenames.
STEP_NAME_MAXLEN = env_int("ANDROID_EMU_STEP_NAME_MAXLEN", 30)


class TestRecorder:
    """Records test execution with screenshots and UI snapshots."""

    __test__ = False  # production class, not a pytest test case

    def __init__(
        self,
        test_name: str,
        output_dir: str = "test-artifacts",
        serial: str | None = None,
        inline: bool = False,
        screenshot_size: str = "half",
        app_name: str | None = None,
    ):
        """
        Initialize test recorder.

        Args:
            test_name: Name of the test being recorded
            output_dir: Directory for test artifacts
            serial: Optional device serial (uses default if not specified)
            inline: If True, return screenshots as base64 (for vision-based automation)
            screenshot_size: 'full', 'half', 'quarter', 'thumb' (default: 'half')
            app_name: App name for semantic screenshot naming
        """
        self.test_name = test_name
        self.serial = serial
        self.inline = inline
        self.screenshot_size = screenshot_size
        self.app_name = app_name
        self.start_time = time.time()
        self.steps = []
        self.current_step = 0

        # Create timestamped output directory
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = test_name.lower().replace(" ", "-")
        self.output_dir = Path(output_dir) / f"{safe_name}-{timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories (only if not in inline mode)
        if not inline:
            self.screenshots_dir = self.output_dir / "screenshots"
            self.screenshots_dir.mkdir(exist_ok=True)
        else:
            self.screenshots_dir = None

        self.ui_dumps_dir = self.output_dir / "ui-dumps"
        self.ui_dumps_dir.mkdir(exist_ok=True)

        # Token-efficient output
        mode_str = "(inline mode)" if inline else ""
        print(f"Recording: {test_name} {mode_str}")
        print(f"Output: {self.output_dir}/")

    def step(
        self,
        description: str,
        screen_name: str | None = None,
        state: str | None = None,
        assertion: str | None = None,
        metadata: dict | None = None,
        assertion_passed: bool = True,
    ):
        """
        Record a test step with automatic screenshot.

        Args:
            description: Step description
            screen_name: Screen name for semantic naming
            state: State description for semantic naming
            assertion: Optional assertion to verify
            metadata: Optional metadata for the step
            assertion_passed: Result of the assertion when one is supplied
                (default True, preserving the historical auto-pass behavior).
                Ignored when ``assertion`` is None.
        """
        self.current_step += 1
        step_time = time.time() - self.start_time

        slug = self._slugify(description)

        # Capture screenshot
        screenshot_path = None
        if not self.inline:
            screenshot_name = f"{self.current_step:03d}-{slug}.png"
            screenshot_path = self.screenshots_dir / screenshot_name

        screenshot_result = capture_screenshot(
            self.serial,
            output_path=str(screenshot_path) if screenshot_path else None,
            size=self.screenshot_size,
            inline=self.inline,
        )

        # Capture UI hierarchy
        ui_dump_path = self.ui_dumps_dir / f"{self.current_step:03d}-{slug}.json"
        element_count = self._capture_ui_hierarchy(ui_dump_path)

        # Store step data
        step_data = {
            "number": self.current_step,
            "description": description,
            "timestamp": step_time,
            "element_count": element_count,
            "ui_dump": ui_dump_path.name,
            "screenshot_size": self.screenshot_size,
        }

        # Handle screenshot data based on mode
        if not self.inline and screenshot_path:
            step_data["screenshot"] = str(screenshot_path)
            step_data["screenshot_name"] = screenshot_path.name
        elif self.inline and screenshot_result.get("base64_data"):
            step_data["screenshot_base64"] = screenshot_result["base64_data"]
            if "width" in screenshot_result:
                step_data["screenshot_dimensions"] = (
                    screenshot_result["width"],
                    screenshot_result["height"],
                )

        if screen_name:
            step_data["screen_name"] = screen_name

        if state:
            step_data["state"] = state

        if assertion:
            step_data["assertion"] = assertion
            step_data["assertion_passed"] = bool(assertion_passed)

        if metadata:
            step_data["metadata"] = metadata

        self.steps.append(step_data)

        # Token-efficient output. When the step carries an assertion, prefix a
        # status symbol so pass/fail is visible at a glance; otherwise keep the
        # original element-count line unchanged.
        symbol = self._assertion_symbol(step_data)
        prefix = f"{symbol} " if symbol else ""
        print(f"  {prefix}[{self.current_step}] {description} ({element_count} elements)")

    @staticmethod
    def _slugify(description: str) -> str:
        """Build a filesystem-safe, length-capped slug from a step description."""
        return description.lower().replace(" ", "-")[:STEP_NAME_MAXLEN]

    @staticmethod
    def _assertion_symbol(step_data: dict) -> str:
        """Return a per-step assertion status symbol, or '' if no assertion.

        ✓ when the assertion passed, ✗ when it failed. Steps without an
        assertion return an empty string so non-assertion output is unchanged.
        """
        if "assertion" not in step_data:
            return ""
        return "✓" if step_data.get("assertion_passed") else "✗"

    def _capture_ui_hierarchy(self, output_path: Path) -> int:
        """
        Capture UI hierarchy to JSON file.

        Args:
            output_path: Path to save JSON

        Returns:
            Number of elements captured
        """
        try:
            hierarchy = get_ui_hierarchy(self.serial)

            # Save to file
            with open(output_path, "w") as f:
                json.dump(hierarchy, f, indent=2)

            # Count elements
            return self._count_elements(hierarchy)

        except Exception as e:
            print(f"  Warning: Failed to capture UI hierarchy: {e}")
            return 0

    def _count_elements(self, node: dict) -> int:
        """
        Recursively count elements in UI hierarchy.

        Args:
            node: UI hierarchy node

        Returns:
            Total element count
        """
        count = 1
        for child in node.get("children", []):
            count += self._count_elements(child)
        return count

    def generate_report(self, passed: bool = True) -> dict[str, str]:
        """
        Write the test report artifacts (JSON + markdown).

        Args:
            passed: Whether the test passed

        Returns:
            Dict of artifact paths with keys:
            - "report_path": path to the JSON report
            - "markdown_path": path to the markdown report
            - "output_dir": path to the artifact directory
        """
        total_time = time.time() - self.start_time

        # Generate test report
        report = {
            "test_name": self.test_name,
            "passed": passed,
            "duration_seconds": round(total_time, 2),
            "steps": self.steps,
            "app_name": self.app_name,
            "device_serial": self.serial,
            "timestamp": datetime.now().isoformat(),
        }

        # Save JSON report
        report_path = self.output_dir / "test-report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        # Generate markdown summary
        markdown_path = self.output_dir / "test-report.md"
        self._generate_markdown(markdown_path, report)

        return {
            "report_path": str(report_path),
            "markdown_path": str(markdown_path),
            "output_dir": str(self.output_dir),
        }

    def finish(self, passed: bool = True) -> str:
        """
        Finish recording and generate summary.

        Args:
            passed: Whether test passed

        Returns:
            Path to the JSON report (string)
        """
        artifacts = self.generate_report(passed=passed)

        total_time = time.time() - self.start_time

        # Token-efficient summary
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"\n{status} in {total_time:.1f}s ({len(self.steps)} steps)")
        print(f"Report: {artifacts['report_path']}")

        return artifacts["report_path"]

    def _generate_markdown(self, output_path: Path, report: dict):
        """
        Generate markdown test report.

        Args:
            output_path: Path to save markdown
            report: Test report data
        """
        lines = [
            f"# Test Report: {report['test_name']}",
            "",
            f"**Status:** {'✓ PASSED' if report['passed'] else '✗ FAILED'}",
            f"**Duration:** {report['duration_seconds']}s",
            f"**Steps:** {len(report['steps'])}",
            f"**Date:** {report['timestamp']}",
            "",
            "## Test Steps",
            "",
        ]

        for step in report["steps"]:
            lines.append(f"### Step {step['number']}: {step['description']}")
            lines.append(f"**Time:** {step['timestamp']:.2f}s")
            lines.append(f"**Elements:** {step['element_count']}")

            if "screenshot_name" in step:
                lines.append(
                    f"**Screenshot:** ![{step['description']}](screenshots/{step['screenshot_name']})"
                )

            if "assertion" in step:
                symbol = "✓" if step.get("assertion_passed") else "✗"
                lines.append(f"**Assertion:** {symbol} {step['assertion']}")

            lines.append("")

        with open(output_path, "w") as f:
            f.write("\n".join(lines))


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Record Android test execution with screenshots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Record test
  python scripts/test_recorder.py --test-name "Login Flow" --output test-results/

  # Use inline screenshots (for vision-based testing)
  python scripts/test_recorder.py --test-name "Checkout" --inline

  # Use as module:
  from scripts.test_recorder import TestRecorder

  recorder = TestRecorder("My Test")
  recorder.step("Open app", screen_name="Home")
  recorder.step("Tap login", screen_name="Login")
  recorder.finish(passed=True)
        """,
    )

    parser.add_argument("--test-name", required=True, help="Name of the test")
    parser.add_argument(
        "--output", default="test-artifacts", help="Output directory (default: test-artifacts)"
    )
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument(
        "--inline", action="store_true", help="Use inline base64 screenshots (for vision AI)"
    )
    parser.add_argument(
        "--size",
        default="half",
        choices=["full", "half", "quarter", "thumb"],
        help="Screenshot size (default: half)",
    )
    parser.add_argument("--app-name", help="App name for semantic naming")

    args = parser.parse_args()

    # Resolve device serial (auto-detects default device when --serial omitted)
    try:
        serial = resolve_device_identifier(args.device_serial)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Create recorder
    TestRecorder(
        test_name=args.test_name,
        output_dir=args.output,
        serial=serial,
        inline=args.inline,
        screenshot_size=args.size,
        app_name=args.app_name,
    )

    print("\nRecorder initialized. Use recorder.step() and recorder.finish() in your code.")
    print("Example usage shown in --help")


if __name__ == "__main__":
    main()
