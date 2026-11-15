# visual_diff.py

Compare two Android screenshots (emulator or device) for visual changes

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- `diff.png` is the current screenshot with every pixel whose R+G+B
  difference sums past 30 painted red: a thresholded per-pixel overlay,
  not a bounding box. `side-by-side.png` puts the two next to each other;
  `diff-report.json` holds the metrics and the artifacts' paths
- The verdict counts a pixel as different when the greyscale of its
  difference exceeds 10, and passes (exit 0) when that count is at most
  `--threshold` of all pixels (0.01 = 1%); FAIL is exit 1. Different
  sizes are refused (exit 1)
- Without `--output` the artifacts go to a new
  `~/.android-emulator-skill/cache/visual-diff/<YYYYmmdd-HHMMSS>/`, never
  the working directory (the default was `.`). `--output DIR` is used as
  given, created if missing
- Every path printed is absolute, including the inputs (`--json`'s
  `summary.baseline` / `summary.current`, and `diff-report.json`'s
  `baseline` / `current`, which were bare file names). Artifact paths
  name files that were written -- the `Diff overlay:` line, `--json`'s
  `artifacts` (`diff_image`, `comparison_image`, `report`; null for an
  image that could not be written) and `diff-report.json`'s `artifacts`
  -- and `Artifacts saved to:` names their directory
- Usage: `visual_diff.py BASELINE CURRENT [--output DIR] [--threshold N] [--json]`
- Note: the two images are **positional**. `--json` now emits the full
  report, so this script keeps the same contract as every other one;
  `--details` survives as a deprecated alias for callers written before it.
