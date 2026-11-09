#!/usr/bin/env python3
"""
Visual diff for Android screenshots (emulator or device).

Compares two screenshots of the same size pixel by pixel and writes three
artifacts:

* ``diff.png`` -- the current screenshot with every changed pixel painted
  red. "Changed" here means the per-channel difference summed over R, G and B
  exceeds 30 (so ``(100,100,100)`` against ``(110,110,110)`` is not painted,
  ``(110,110,111)`` is). It is a thresholded per-pixel overlay, not a
  bounding box.
* ``side-by-side.png`` -- baseline and current next to each other.
* ``diff-report.json`` -- the metrics below and the artifacts' paths.

The verdict counts a pixel as different when the greyscale of its difference
exceeds 10 (a noise floor), and passes when that count is at most
``--threshold`` of all pixels (0.01 = 1%). The two cut-offs are separate:
the overlay can paint fewer pixels than the count reports.

Without ``--output`` the artifacts go to a new
``~/.android-emulator-skill/cache/visual-diff/<YYYYmmdd-HHMMSS>/`` directory,
never into the caller's working directory. Every path printed is absolute.

Usage: python scripts/visual_diff.py baseline.png current.png [options]
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from common import cli
from common.cache_utils import default_cache_dir

try:
    from PIL import Image, ImageChops

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# A pixel is painted red in diff.png when its R, G and B differences sum past this.
OVERLAY_THRESHOLD = 30

# Where a run's artifacts go when --output is not given, under the skill's cache
# root. The default was ".", so an agent standing in a repository root left
# diff.png, side-by-side.png and diff-report.json in it (L12) -- the same
# litter app_state_capture's snapshots used to leave (N12).
ARTIFACTS_DIRNAME = "visual-diff"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def default_output_dir() -> Path:
    """A new ``~/.android-emulator-skill/cache/visual-diff/<YYYYmmdd-HHMMSS>/``, created.

    Two runs inside the same second get ``-1``, ``-2``... rather than one
    overwriting the other's artifacts.
    """
    base = default_cache_dir() / ARTIFACTS_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    candidate = base / stamp
    suffix = 0
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            suffix += 1
            candidate = base / f"{stamp}-{suffix}"


class VisualDiffer:
    """Compares two screenshots and draws the thresholded overlay of what changed."""

    def __init__(self, threshold: float = 0.01):
        """
        Initialize differ with threshold.

        Args:
            threshold: Largest fraction of differing pixels that still passes
                (0.01 = 1%); a pixel differs when the greyscale of its
                difference exceeds 10
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
        """Write ``current`` with every pixel whose R+G+B difference exceeds 30 painted red.

        PIL operations over whole images, not a ``getpixel``/``putpixel`` loop
        (2.6 M Python calls for a 1080x2400 screenshot), and the same picture
        pixel for pixel. ``ImageChops.add`` clips at 255, which cannot change a
        ``> 30`` decision.

        Raises:
            ValueError: The images differ in size. The loop this replaced
                crashed when the baseline was the larger and drew a top-left
                crop when it was the smaller; neither is a diff.
        """
        baseline = Image.open(baseline_path).convert("RGB")
        current = Image.open(current_path).convert("RGB")
        if baseline.size != current.size:
            raise ValueError(
                f"Image dimensions do not match: baseline {baseline.size}, "
                f"current {current.size}"
            )

        red, green, blue = ImageChops.difference(baseline, current).split()
        channel_sum = ImageChops.add(ImageChops.add(red, green), blue)
        mask = channel_sum.point(lambda level: 255 if level > OVERLAY_THRESHOLD else 0)
        painted = Image.new("RGB", current.size, (255, 0, 0))
        Image.composite(painted, current, mask).save(output_path)

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
    parser = cli.parser(
        description=(
            "Compare two Android screenshots (emulator or device) of the same size. "
            "diff.png is the current screenshot with every pixel whose R+G+B "
            "difference exceeds 30 painted red."
        ),
        json_flags=("--json", "--details"),
    )
    parser.add_argument("baseline", help="Path to baseline screenshot")
    parser.add_argument("current", help="Path to current screenshot")
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output directory for diff artifacts (default: a new "
            f"~/.android-emulator-skill/cache/{ARTIFACTS_DIRNAME}/<YYYYmmdd-HHMMSS>/)"
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.01,
        help=(
            "Largest fraction of pixels that may differ and still PASS "
            "(0.01 = 1%%, default: 0.01). A pixel differs when the greyscale of "
            "its difference exceeds 10"
        ),
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

    # Only now, so a refused comparison leaves no empty run directory behind.
    # Absolute, because every path printed below is one an agent should be able
    # to open from wherever it stands; a bare `diff.png` was relative to a cwd
    # the reader may not share.
    if args.output is None:
        output_dir = default_output_dir()
    else:
        output_dir = Path(args.output).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    # Generate artifacts
    diff_image_path = output_dir / "diff.png"
    comparison_image_path = output_dir / "side-by-side.png"
    written: set[Path] = set()

    try:
        differ.generate_diff_image(args.baseline, args.current, str(diff_image_path))
        written.add(diff_image_path)
        differ.generate_side_by_side(args.baseline, args.current, str(comparison_image_path))
        written.add(comparison_image_path)
    except Exception as e:
        # The comparison stands without the pictures. To stderr under --json,
        # where stdout is the report.
        print(
            f"Warning: Could not generate images - {e}",
            file=sys.stderr if args.json_output else sys.stdout,
        )

    def _written(path: Path) -> str | None:
        """The absolute path, or None for an artifact that was not written."""
        return str(path) if path in written else None

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
                "artifacts": {
                    "diff": _written(diff_image_path),
                    "comparison": _written(comparison_image_path),
                },
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
                "diff_image": _written(diff_image_path),
                "comparison_image": _written(comparison_image_path),
                "report": str(report_path),
            },
        }
        print(json.dumps(report, indent=2))
    else:
        # Minimal output (default)
        print(f"Difference: {result['difference_percentage']}% ({result['verdict']})")
        if result["different_pixels"] > 0:
            print(f"Changed pixels: {result['different_pixels']:,}")
        if diff_image_path in written:
            print(f"Diff overlay: {diff_image_path}")
        print(f"Artifacts saved to: {output_dir}/")

    # Exit with error if test failed
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    cli.run(main, json_flags=("--json", "--details"))
