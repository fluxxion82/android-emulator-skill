#!/usr/bin/env python3
"""
Visual Diff Tool for iOS Simulator Screenshots

Compares two screenshots pixel-by-pixel to detect visual changes.
Optimized for minimal token output.

Usage: python scripts/visual_diff.py baseline.png current.png [options]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from common import cli

try:
    from PIL import Image, ImageChops

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


class VisualDiffer:
    """Performs visual comparison between screenshots."""

    def __init__(self, threshold: float = 0.01):
        """
        Initialize differ with threshold.

        Args:
            threshold: Maximum acceptable difference ratio (0.01 = 1%)
        """
        self.threshold = threshold

    def compare(self, baseline_path: str, current_path: str) -> dict:
        """
        Compare two images and return difference metrics.

        Args:
            baseline_path: Path to baseline image
            current_path: Path to current image

        Returns:
            Dictionary with comparison results
        """
        # Load images. A missing file (FileNotFoundError) or one that is not an
        # image (PIL's UnidentifiedImageError, an OSError) raises, and
        # common.cli reports it -- these printed to stdout and exited 1 from
        # inside a library method, under --json too.
        baseline = Image.open(baseline_path)
        current = Image.open(current_path)

        # Verify dimensions match
        if baseline.size != current.size:
            return {
                "error": "Image dimensions do not match",
                "baseline_size": baseline.size,
                "current_size": current.size,
            }

        # Convert to RGB if needed
        if baseline.mode != "RGB":
            baseline = baseline.convert("RGB")
        if current.mode != "RGB":
            current = current.convert("RGB")

        # Calculate difference
        diff = ImageChops.difference(baseline, current)

        # Calculate metrics
        total_pixels = baseline.size[0] * baseline.size[1]
        diff_pixels = self._count_different_pixels(diff)
        diff_percentage = (diff_pixels / total_pixels) * 100

        # Determine pass/fail
        passed = diff_percentage <= (self.threshold * 100)

        return {
            "dimensions": baseline.size,
            "total_pixels": total_pixels,
            "different_pixels": diff_pixels,
            "difference_percentage": round(diff_percentage, 2),
            "threshold_percentage": self.threshold * 100,
            "passed": passed,
            "verdict": "PASS" if passed else "FAIL",
        }

    def _count_different_pixels(self, diff_image: Image.Image) -> int:
        """Count number of pixels that are different."""
        # Convert to grayscale for easier processing
        diff_gray = diff_image.convert("L")

        # Count non-zero pixels (different)
        pixels = diff_gray.getdata()
        return sum(1 for pixel in pixels if pixel > 10)  # Threshold for noise

    def generate_diff_image(self, baseline_path: str, current_path: str, output_path: str) -> None:
        """Generate highlighted difference image."""
        baseline = Image.open(baseline_path).convert("RGB")
        current = Image.open(current_path).convert("RGB")

        # Create difference image
        diff = ImageChops.difference(baseline, current)

        # Enhance differences with red overlay
        diff_enhanced = Image.new("RGB", baseline.size)
        for x in range(baseline.size[0]):
            for y in range(baseline.size[1]):
                diff_pixel = diff.getpixel((x, y))
                if sum(diff_pixel) > 30:  # Threshold for visibility
                    # Highlight in red
                    diff_enhanced.putpixel((x, y), (255, 0, 0))
                else:
                    # Keep original
                    diff_enhanced.putpixel((x, y), current.getpixel((x, y)))

        diff_enhanced.save(output_path)

    def generate_side_by_side(
        self, baseline_path: str, current_path: str, output_path: str
    ) -> None:
        """Generate side-by-side comparison image."""
        baseline = Image.open(baseline_path)
        current = Image.open(current_path)

        # Create combined image
        width = baseline.size[0] * 2 + 10  # 10px separator
        height = max(baseline.size[1], current.size[1])
        combined = Image.new("RGB", (width, height), color=(128, 128, 128))

        # Paste images
        combined.paste(baseline, (0, 0))
        combined.paste(current, (baseline.size[0] + 10, 0))

        combined.save(output_path)


def main(argv: list[str] | None = None):
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Compare screenshots for visual differences")
    parser.add_argument("baseline", help="Path to baseline screenshot")
    parser.add_argument("current", help="Path to current screenshot")
    parser.add_argument(
        "--output",
        default=".",
        help="Output directory for diff artifacts (default: current directory)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.01,
        help="Acceptable difference threshold (0.01 = 1%%, default: 0.01)",
    )
    # `--json` is the repo-wide contract for machine-readable output (CLAUDE.md:
    # "--help and --json on every script"), and this was the one script without
    # it: the same output was reachable only as `--details`, whose help text
    # reads like a --verbose. An agent following SKILL.md would pass --json here
    # and get an argparse error.
    #
    # `--details` is kept as an alias rather than renamed, because it is a
    # published flag -- the same reason logs.py and avd.py left every script
    # they route to callable unchanged.
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit the full report as JSON",
    )
    parser.add_argument(
        "--details",
        dest="json_output",
        action="store_true",
        help="Deprecated alias for --json",
    )

    args = parser.parse_args(argv)

    if not _PIL_AVAILABLE:
        cli.fail("Pillow is not installed", "Run: pip3 install pillow")

    # Create output directory if needed
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize differ
    differ = VisualDiffer(threshold=args.threshold)

    # Perform comparison
    result = differ.compare(args.baseline, args.current)

    # A dimension mismatch is a refusal, reported through common.cli: it was
    # three lines on stdout, under --json too.
    if "error" in result:
        cli.fail(
            f"{result['error']}: baseline {result['baseline_size']}, "
            f"current {result['current_size']}",
            "Compare screenshots taken at the same resolution and orientation "
            "(same device, same --size preset).",
        )

    # Generate artifacts
    diff_image_path = output_dir / "diff.png"
    comparison_image_path = output_dir / "side-by-side.png"

    try:
        differ.generate_diff_image(args.baseline, args.current, str(diff_image_path))
        differ.generate_side_by_side(args.baseline, args.current, str(comparison_image_path))
    except Exception as e:
        # The comparison stands without the pictures. To stderr under --json,
        # where stdout is the report.
        print(
            f"Warning: Could not generate images - {e}",
            file=sys.stderr if args.json_output else sys.stdout,
        )

    # Save JSON report -- BEFORE printing the result. Written after it, a
    # report file that could not be saved appended a refusal to a success
    # already on stdout: under --json, two objects.
    report_path = output_dir / "diff-report.json"
    with open(report_path, "w") as f:
        json.dump(
            {
                "baseline": os.path.basename(args.baseline),
                "current": os.path.basename(args.current),
                "results": result,
                "artifacts": {"diff": "diff.png", "comparison": "side-by-side.png"},
            },
            f,
            indent=2,
        )

    # Output results (token-optimized)
    if args.json_output:
        # Detailed output
        report = {
            "summary": {
                "baseline": args.baseline,
                "current": args.current,
                "threshold": args.threshold,
                "passed": result["passed"],
            },
            "results": result,
            "artifacts": {
                "diff_image": str(diff_image_path),
                "comparison_image": str(comparison_image_path),
            },
        }
        print(json.dumps(report, indent=2))
    else:
        # Minimal output (default)
        print(f"Difference: {result['difference_percentage']}% ({result['verdict']})")
        if result["different_pixels"] > 0:
            print(f"Changed pixels: {result['different_pixels']:,}")
        print(f"Artifacts saved to: {output_dir}/")

    # Exit with error if test failed
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    cli.run(main, json_flags=("--json", "--details"))
