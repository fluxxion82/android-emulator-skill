"""Tests for common.sdk_tools — SDK tool resolution.

The scenario these pin down is a real host misconfiguration: PATH contains the
Android SDK *root* rather than ``$ANDROID_HOME/emulator``. The SDK root holds a
directory named ``emulator``, so exec'ing the bare name ``emulator`` hits that
directory and raises ``PermissionError: [Errno 13] Permission denied`` — which
no caller was catching. These tests build that exact layout on disk.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from common import sdk_tools


def _make_sdk(tmp_path):
    """Create a minimal SDK layout: <sdk>/emulator/emulator (executable file)."""
    sdk_root = tmp_path / "sdk"
    emulator_dir = sdk_root / "emulator"
    emulator_dir.mkdir(parents=True)
    binary = emulator_dir / "emulator"
    binary.write_text("#!/bin/sh\necho Pixel_9\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sdk_root, binary


def _clear_sdk_env(monkeypatch):
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)


# ---------------------------------------------------------------------------
# The directory-on-PATH trap
# ---------------------------------------------------------------------------
def test_sdk_root_on_path_is_not_a_file_not_found_error(tmp_path, monkeypatch):
    """Baseline: the bare name with the SDK root on PATH does not raise ENOENT.

    execve on a directory is EACCES (``PermissionError: [Errno 13]`` is what a
    real macOS host produced), so the ``FileNotFoundError`` guard every caller
    had was the wrong exception. The assertion is kept at the OSError level
    because the exact errno is a platform detail; the load-bearing claim is
    that FileNotFoundError does not cover it.
    """
    sdk_root, _binary = _make_sdk(tmp_path)
    monkeypatch.setenv("PATH", str(sdk_root))

    with pytest.raises(OSError) as excinfo:
        subprocess.run(["emulator", "-list-avds"], capture_output=True, check=False)

    assert not isinstance(excinfo.value, FileNotFoundError), excinfo.value


def test_which_rejects_the_emulator_directory(tmp_path, monkeypatch):
    """``shutil.which`` skips the directory instead of returning it."""
    sdk_root, _binary = _make_sdk(tmp_path)
    monkeypatch.setenv("PATH", str(sdk_root))

    assert shutil.which("emulator") is None


def test_resolves_via_android_home_when_path_holds_only_the_sdk_root(tmp_path, monkeypatch):
    """The documented broken host: SDK root on PATH, ANDROID_HOME set."""
    sdk_root, binary = _make_sdk(tmp_path)
    monkeypatch.setenv("PATH", str(sdk_root))
    monkeypatch.setenv("ANDROID_HOME", str(sdk_root))
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)

    resolved = sdk_tools.get_emulator_path()

    assert resolved == str(binary)
    # Crucially a file, not the <sdk>/emulator directory that broke execve.
    assert Path(resolved).is_file()
    assert not Path(resolved).is_dir()


def test_resolved_path_actually_execs(tmp_path, monkeypatch):
    """The resolved path runs; the bare name in the same env does not."""
    sdk_root, _binary = _make_sdk(tmp_path)
    monkeypatch.setenv("PATH", str(sdk_root))
    monkeypatch.setenv("ANDROID_HOME", str(sdk_root))

    resolved = sdk_tools.get_emulator_path()
    result = subprocess.run([resolved, "-list-avds"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert result.stdout == "Pixel_9\n"


# ---------------------------------------------------------------------------
# Ordinary resolution
# ---------------------------------------------------------------------------
def test_path_wins_over_android_home(tmp_path, monkeypatch):
    sdk_root, _binary = _make_sdk(tmp_path)
    other_dir = tmp_path / "bin"
    other_dir.mkdir()
    on_path = other_dir / "emulator"
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(on_path.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv("PATH", str(other_dir))
    monkeypatch.setenv("ANDROID_HOME", str(sdk_root))

    assert sdk_tools.get_emulator_path() == str(on_path)


def test_android_sdk_root_is_accepted_as_a_fallback_env(tmp_path, monkeypatch):
    sdk_root, binary = _make_sdk(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk_root))

    assert sdk_tools.get_emulator_path() == str(binary)


def test_returns_none_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    _clear_sdk_env(monkeypatch)

    assert sdk_tools.get_emulator_path() is None


def test_returns_none_when_sdk_holds_a_non_executable_file(tmp_path, monkeypatch):
    sdk_root = tmp_path / "sdk"
    (sdk_root / "emulator").mkdir(parents=True)
    stub = sdk_root / "emulator" / "emulator"
    stub.write_text("not executable")
    stub.chmod(0o644)

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("ANDROID_HOME", str(sdk_root))

    assert sdk_tools.get_emulator_path() is None


def test_legacy_tools_subdir_is_searched(tmp_path, monkeypatch):
    sdk_root = tmp_path / "sdk"
    (sdk_root / "tools").mkdir(parents=True)
    binary = sdk_root / "tools" / "emulator"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("ANDROID_HOME", str(sdk_root))

    assert sdk_tools.get_emulator_path() == str(binary)


def test_not_found_message_points_at_the_emulator_directory():
    """The hint must name the directory to add, not just say 'PATH'."""
    message = sdk_tools.EMULATOR_NOT_FOUND_MESSAGE
    assert "$ANDROID_HOME/emulator" in message
    assert "sdkmanager" in message
