"""visual_diff: the overlay picture, and where the artifacts go (L12).

Every image here is constructed under ``tmp_path`` -- test state, not tool
output. Two contracts:

* ``generate_diff_image`` paints every pixel whose per-channel difference sum
  exceeds 30 red over the current image. It did so with a nested
  ``getpixel``/``putpixel`` loop (2.6 M Python calls for a 1080x2400
  screenshot). The vectorised version must give the same picture pixel for
  pixel: ``_reference_overlay`` below is the original loop, kept verbatim as
  the reference, and the guard test forbids the per-pixel calls outright.
* Without ``--output``, nothing is written into the caller's working
  directory, and every path the script reports is absolute and exists.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import visual_diff

Image = pytest.importorskip("PIL.Image")
ImageChops = pytest.importorskip("PIL.ImageChops")


def _reference_overlay(baseline_path: str, current_path: str, output_path: str) -> None:
    """The original ``generate_diff_image`` body (main @ 3d68bab), verbatim but for ``self``."""
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


# (baseline pixel, current pixel, highlighted?) -- one per branch of the loop.
EDGE_PIXELS = [
    ((10, 20, 30), (10, 20, 30), False),  # identical
    ((100, 100, 100), (110, 110, 110), False),  # channel sum exactly 30: not highlighted
    ((0, 0, 0), (0, 0, 30), False),  # 30 in one channel alone: not highlighted
    ((100, 100, 100), (110, 110, 111), True),  # sum 31: highlighted
    ((0, 0, 0), (11, 10, 10), True),  # 31 only across channels: highlighted
    ((0, 0, 0), (255, 0, 0), True),  # one channel's difference at its 255 ceiling
    ((0, 0, 0), (255, 255, 255), True),  # sum 765, past what ImageChops.add holds
    ((255, 0, 0), (255, 0, 0), False),  # already red and unchanged: kept, not painted
    ((200, 50, 50), (170, 50, 50), False),  # a decrease of exactly 30
    ((200, 50, 50), (169, 50, 50), True),  # a decrease of 31
]


def _edge_pair(tmp_path: Path) -> tuple[str, str]:
    """A 2-row image pair holding every EDGE_PIXELS case, in each row order."""
    width = len(EDGE_PIXELS)
    baseline = Image.new("RGB", (width, 2))
    current = Image.new("RGB", (width, 2))
    for x, (before, after, _) in enumerate(EDGE_PIXELS):
        baseline.putpixel((x, 0), before)
        current.putpixel((x, 0), after)
        # Swapped on the second row, so the difference is taken both ways.
        baseline.putpixel((x, 1), after)
        current.putpixel((x, 1), before)
    paths = (str(tmp_path / "edge-base.png"), str(tmp_path / "edge-cur.png"))
    baseline.save(paths[0])
    current.save(paths[1])
    return paths


def _noise_pair(tmp_path: Path, mode: str) -> tuple[str, str]:
    """Seeded noise, with a third of the pixels nudged by a few levels only."""
    rng = random.Random(1234)
    size = (37, 23)
    channels = len(mode)
    count = size[0] * size[1] * channels
    base = bytes(rng.randrange(256) for _ in range(count))
    cur = bytes(
        min(255, max(0, b + rng.choice((-12, -11, -10, 0, 0, 10, 11, 12, 60)))) for b in base
    )
    paths = (str(tmp_path / f"noise-base-{mode}.png"), str(tmp_path / f"noise-cur-{mode}.png"))
    Image.frombytes(mode, size, base).save(paths[0])
    Image.frombytes(mode, size, cur).save(paths[1])
    return paths


def _same_picture(first: str, second: str) -> bool:
    a = Image.open(first).convert("RGB")
    b = Image.open(second).convert("RGB")
    return a.size == b.size and ImageChops.difference(a, b).getbbox() is None


def test_the_edge_pixels_decide_as_the_reference_says(tmp_path):
    """The table's own claims, checked against the reference before anything else."""
    baseline, current = _edge_pair(tmp_path)
    out = str(tmp_path / "ref.png")
    _reference_overlay(baseline, current, out)
    picture = Image.open(out).convert("RGB")
    for x, (_, after, highlighted) in enumerate(EDGE_PIXELS):
        expected = (255, 0, 0) if highlighted else after
        assert picture.getpixel((x, 0)) == expected, EDGE_PIXELS[x]


@pytest.mark.parametrize(
    "pair",
    ["edge", "noise-RGB", "noise-RGBA", "noise-L"],
)
def test_the_overlay_is_the_reference_picture_pixel_for_pixel(tmp_path, pair):
    """Green before and after the vectorisation: the guard that the picture did not change."""
    if pair == "edge":
        baseline, current = _edge_pair(tmp_path)
    else:
        baseline, current = _noise_pair(tmp_path, pair.split("-")[1])
    reference = str(tmp_path / "reference.png")
    produced = str(tmp_path / "produced.png")

    _reference_overlay(baseline, current, reference)
    visual_diff.VisualDiffer().generate_diff_image(baseline, current, produced)

    new = Image.open(produced).convert("RGB")
    ref = Image.open(reference).convert("RGB")
    assert new.size == ref.size
    assert ImageChops.difference(new, ref).getbbox() is None


def test_the_overlay_makes_no_per_pixel_calls(tmp_path, monkeypatch):
    """Timing-independent: the overlay is PIL operations, not a getpixel/putpixel loop."""
    baseline, current = _edge_pair(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("generate_diff_image made a per-pixel call")

    monkeypatch.setattr(Image.Image, "getpixel", forbidden)
    monkeypatch.setattr(Image.Image, "putpixel", forbidden)

    visual_diff.VisualDiffer().generate_diff_image(baseline, current, str(tmp_path / "out.png"))
    monkeypatch.undo()

    assert _same_picture(str(tmp_path / "out.png"), _reference_of(tmp_path, baseline, current))


def _reference_of(tmp_path: Path, baseline: str, current: str) -> str:
    out = str(tmp_path / "reference.png")
    _reference_overlay(baseline, current, out)
    return out


@pytest.mark.parametrize(
    ("base_size", "cur_size"),
    [((4, 3), (3, 2)), ((3, 2), (4, 3)), ((4, 3), (4, 2))],
    ids=["baseline-larger", "baseline-smaller", "baseline-taller"],
)
def test_images_of_different_sizes_are_refused(tmp_path, base_size, cur_size):
    """The loop crashed (IndexError) when the baseline was larger in either dimension,
    and silently drew a top-left crop when it was smaller. Refused either way now;
    main() refuses a mismatch before it gets here, so this is the library contract."""
    baseline = str(tmp_path / "b.png")
    current = str(tmp_path / "c.png")
    Image.new("RGB", base_size).save(baseline)
    Image.new("RGB", cur_size, (50, 0, 0)).save(current)

    with pytest.raises(ValueError, match="dimensions"):
        visual_diff.VisualDiffer().generate_diff_image(baseline, current, str(tmp_path / "o.png"))
    assert not (tmp_path / "o.png").exists()


# --- where the artifacts go ---------------------------------------------------


@pytest.fixture
def screenshots(tmp_path) -> tuple[str, str]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    Image.new("RGB", (8, 8), (0, 0, 0)).save(inputs / "base.png")
    changed = Image.new("RGB", (8, 8), (0, 0, 0))
    changed.putpixel((3, 3), (200, 200, 200))
    changed.save(inputs / "cur.png")
    return str(inputs / "base.png"), str(inputs / "cur.png")


@pytest.fixture
def isolated(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """A cwd and a HOME of their own, both empty."""
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    return cwd, home


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        visual_diff.main(argv)
    captured = capsys.readouterr()
    return exc.value.code, captured.out, captured.err


@pytest.mark.parametrize("flag", ["--json", "--details"])
def test_without_output_nothing_lands_in_the_cwd(screenshots, isolated, capsys, flag):
    cwd, home = isolated

    status, out, _err = _run([*screenshots, flag], capsys)

    assert status == 1  # one changed pixel of 64 is over the 1% default
    assert sorted(cwd.iterdir()) == [], "visual_diff wrote into the caller's cwd"
    report = json.loads(out)
    artifacts = report["artifacts"]
    assert set(artifacts) >= {"diff_image", "comparison_image", "report"}
    base = home / ".android-emulator-skill" / "cache" / "visual-diff"
    for key, value in artifacts.items():
        path = Path(value)
        assert path.is_absolute(), f"{key} is not absolute: {value}"
        assert path.is_file(), f"{key} names a file that was not written: {value}"
        assert base in path.parents, f"{key} is not under {base}: {value}"
    run_dir = Path(artifacts["report"]).parent
    assert run_dir.parent == base
    assert len(run_dir.name) == len("YYYYmmdd-HHMMSS"), run_dir.name

    saved = json.loads(Path(artifacts["report"]).read_text())
    for key, value in saved["artifacts"].items():
        assert Path(value).is_absolute() and Path(value).is_file(), (key, value)


def test_the_text_mode_names_absolute_paths(screenshots, isolated, capsys):
    cwd, _home = isolated

    status, out, _err = _run(list(screenshots), capsys)

    assert status == 1
    assert sorted(cwd.iterdir()) == []
    paths = [line.split(": ", 1)[1] for line in out.splitlines() if "/" in line]
    assert paths, out
    for value in paths:
        assert Path(value).is_absolute() and Path(value).exists(), out


def test_a_given_relative_output_is_honoured_and_reported_absolute(screenshots, isolated, capsys):
    cwd, home = isolated

    _status, out, _err = _run([*screenshots, "--output", "artifacts", "--json"], capsys)

    report = json.loads(out)
    for value in report["artifacts"].values():
        assert Path(value).parent == cwd / "artifacts", value
        assert Path(value).is_file(), value
    assert not (home / ".android-emulator-skill").exists()


def test_two_runs_in_one_second_do_not_share_a_directory(
    screenshots, isolated, capsys, monkeypatch
):
    monkeypatch.setattr(visual_diff, "_timestamp", lambda: "20261109-120000")

    _s, first, _e = _run([*screenshots, "--json"], capsys)
    _s, second, _e = _run([*screenshots, "--json"], capsys)

    first_dir = Path(json.loads(first)["artifacts"]["report"]).parent
    second_dir = Path(json.loads(second)["artifacts"]["report"]).parent
    assert first_dir != second_dir
