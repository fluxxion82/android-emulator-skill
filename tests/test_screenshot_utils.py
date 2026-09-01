"""Screenshots carried defect R4 after the UI dump was cured of it.

`common.hierarchy` removed the fixed-path collision for UI dumps: three
implementations had dumped to `/sdcard/window_dump.xml` and pulled to a fixed
`/tmp` path, so two concurrent runs -- or one run against two devices -- read
each other's screen. Screenshots kept exactly that shape:

    /sdcard/screenshot.png  ->  /tmp/android_screenshot.png

Both constants, both shared. An agent could act on another agent's screen, and
for a *screenshot* that is worse than for a dump: the image looks perfectly
valid, so nothing about the result suggests it came from the wrong device.

Two further defects were sitting in the same function:

- **The device copy was never deleted.** Every capture ever taken left a file
  on `/sdcard`, for the life of the device.
- **Dimensions were invented when Pillow was missing** -- literally
  `width, height = 1080, 1920`. A plausible lie is worse than an error: a
  caller comparing those against element bounds gets nonsense on any other
  device, and nothing marks the numbers as guessed. A PNG carries its real
  dimensions in its header, so the honest answer costs nothing.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import pytest

from common import adb_exec, screenshot_utils

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_header_only(width: int, height: int) -> bytes:
    """Just enough PNG to read dimensions from -- not a decodable image."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + ihdr


def _png_bytes(width: int, height: int) -> bytes:
    """A real, decodable PNG.

    Built with Pillow rather than hand-assembled: `capture_screenshot` opens
    the pulled file with Pillow, so a header-only stub fails with "cannot
    identify image file" and the test would be measuring the stub, not the code.
    """
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def device(monkeypatch):
    """Fake adb: record every call, and materialise the pulled file."""
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "pull" in cmd:
            # adb pull's destination is the final argument.
            Path(cmd[-1]).write_bytes(_png_bytes(1080, 2424))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)
    return calls


def _paths_of(calls: list[list[str]], operation: str) -> list[str]:
    return [c[-1] for c in calls if operation in c]


# ---------------------------------------------------------------------------
# R4, for screenshots.
# ---------------------------------------------------------------------------


def test_two_captures_do_not_share_a_device_path(device, tmp_path):
    """The whole defect: a fixed `/sdcard/screenshot.png` for every caller."""
    screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "a.png"))
    screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "b.png"))

    device_paths = [c[c.index("screencap") + 2] for c in device if "screencap" in c]
    assert len(device_paths) == 2
    assert device_paths[0] != device_paths[1], (
        f"both captures wrote {device_paths[0]} on the device; a concurrent run "
        f"overwrites the image before the other pulls it"
    )


def test_two_captures_do_not_share_a_host_path(device, tmp_path):
    screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "a.png"))
    screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "b.png"))

    pulled = _paths_of(device, "pull")
    assert len(pulled) == 2
    assert pulled[0] != pulled[1], f"both captures pulled to {pulled[0]}"


def test_no_fixed_screenshot_path_remains_in_the_source():
    """Structural, and parsed rather than grepped.

    A substring search over the file would match this module's own docstring
    explaining the old paths -- the mistake made repeatedly in this repo, where
    a guard fires on the documentation of its own fix. So: string *values* in
    code, with docstrings excluded.
    """
    import ast

    source = Path(screenshot_utils.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None and node.body:
                doc_lines.add(node.body[0].lineno)

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno not in doc_lines
        and node.value in ("/sdcard/screenshot.png", "/tmp/android_screenshot.png")
    ]
    assert not offenders, f"fixed screenshot paths still used as values: {offenders}"


# ---------------------------------------------------------------------------
# The device copy must not survive the call.
# ---------------------------------------------------------------------------


def test_the_capture_is_removed_from_the_device(device, tmp_path):
    """Every capture used to leak a file onto /sdcard permanently."""
    screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "a.png"))

    written = next(c[c.index("screencap") + 2] for c in device if "screencap" in c)
    removed = [c for c in device if "rm" in c and written in c]
    assert removed, f"{written} was left on the device"


def test_the_device_copy_is_removed_even_when_the_pull_fails(monkeypatch, tmp_path):
    """A screencap that worked and a pull that did not still leaves a file."""
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(list(cmd))
        failed = "pull" in cmd
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, "", "pull failed")

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    with pytest.raises(RuntimeError):
        screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "a.png"))

    assert any("rm" in c for c in calls), "the device copy leaked when the pull failed"


def test_cleanup_failure_does_not_replace_the_real_error(monkeypatch, tmp_path):
    """Cleanup runs with an exception in flight; it must not become the error."""

    def _run(cmd, **_kwargs):
        if "rm" in cmd:
            raise OSError("device went away during cleanup")
        if "pull" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "no space left on device")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    with pytest.raises(RuntimeError) as excinfo:
        screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "a.png"))

    assert "no space left" in str(
        excinfo.value
    ), f"the cleanup failure masked the real one: {excinfo.value}"


# ---------------------------------------------------------------------------
# Dimensions are read, not invented.
# ---------------------------------------------------------------------------


def test_png_dimensions_reads_the_header(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(_png_header_only(1440, 3120))
    assert screenshot_utils.png_dimensions(path) == (1440, 3120)


def test_png_dimensions_rejects_a_non_png(tmp_path):
    """Better a refusal than a number read out of arbitrary bytes."""
    path = tmp_path / "not.png"
    path.write_bytes(b"this is not a png file at all, no sir")
    with pytest.raises(ValueError):
        screenshot_utils.png_dimensions(path)


def test_dimensions_without_pillow_are_the_real_ones(device, monkeypatch, tmp_path):
    """The old fallback returned 1080x1920 for every device, invented."""
    monkeypatch.setattr(screenshot_utils, "HAS_PIL", False)

    result = screenshot_utils.capture_screenshot(size="full", output_path=str(tmp_path / "a.png"))

    assert (result["width"], result["height"]) == (
        1080,
        2424,
    ), "dimensions do not match the captured PNG; they are being guessed"


def test_no_fabricated_resolution_remains_in_the_source():
    """1080x1920 as a literal pair was the invented answer."""
    import ast

    tree = ast.parse(Path(screenshot_utils.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            values = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            assert values != [
                1080,
                1920,
            ], f"line {node.lineno} still hardcodes a fabricated 1080x1920 resolution"
