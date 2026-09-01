"""Findings from the post-migration review, by Codex and Fable.

Four defects the adb_exec migration introduced or failed to close:

  P1  `screenshot_utils` caught `AdbCommandError` and read `e.stderr`, an
      attribute it did not have. Every failed screencap raised AttributeError
      from inside the handler, replacing the real adb failure — and the sibling
      `except Exception` could not catch it, being in the same `try`.

  P2  `device_utils.get_device_screen_size` returns a fabricated (1080, 1920)
      on *any* exception, now including AdbTimeoutError. `gesture` computes
      swipe coordinates from it, so a transient timeout on a tablet produces a
      confident, wrong gesture instead of an error. Exactly the silently-wrong
      class this repo is being repaired for.

  P2  `AnrWatcher.watch(duration_seconds=...)` checks the clock only after
      `readline()` returns, so on a quiet device it blocks forever — the same
      A2 defect already fixed in log_monitor.

  P2  The streaming exemption in test_no_unbounded_subprocess.py was vacuous:
      it substring-matched `terminate()`/`kill()`, which appear in unrelated
      cleanup code, so it blessed the hang above. A guard against vacuous
      guards that was itself vacuous.
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from common import adb_exec, device_utils


@pytest.fixture
def fake_adb(monkeypatch):
    """Fake adb at the one place every call now goes."""

    def _install(stdout="", stderr="", returncode=0, raises=None):
        def _run(cmd, **_kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(adb_exec.subprocess, "run", _run)

    return _install


# ---------------------------------------------------------------------------
# P1 — exceptions must expose what the caller reads off them.
# ---------------------------------------------------------------------------


def test_screenshot_failure_reports_adb_not_an_attribute_error(fake_adb, tmp_path):
    """A failed capture must surface adb's reason, not AttributeError."""
    from common import screenshot_utils

    fake_adb(returncode=1, stderr="screencap: permission denied")

    with pytest.raises(RuntimeError) as excinfo:
        screenshot_utils.capture_screenshot(None, output_path=str(tmp_path / "s.png"))

    message = str(excinfo.value)
    assert "AttributeError" not in message
    assert "permission denied" in message, f"adb's reason was lost: {message}"


# ---------------------------------------------------------------------------
# P2 — a wrong screen size is worse than no screen size.
# ---------------------------------------------------------------------------


def test_screen_size_does_not_fabricate_a_default_on_failure(fake_adb):
    """gesture derives swipe coordinates from this; a guess aims them wrongly."""
    fake_adb(raises=subprocess.TimeoutExpired(cmd="adb", timeout=5))

    with pytest.raises(adb_exec.AdbError):
        device_utils.get_device_screen_size("emulator-5554")


def test_screen_size_does_not_fabricate_when_output_is_unparseable(fake_adb):
    """A successful call whose output we cannot read is still not 1080x1920."""
    fake_adb(returncode=0, stdout="something unexpected\n")

    with pytest.raises(RuntimeError):
        device_utils.get_device_screen_size("emulator-5554")


def test_screen_size_still_parses_a_normal_response(fake_adb):
    """Guard against over-correcting into always raising."""
    fake_adb(returncode=0, stdout="Physical size: 1440x3040\n")
    assert device_utils.get_device_screen_size("emulator-5554") == (1440, 3040)


# ---------------------------------------------------------------------------
# P2 — the legacy ANR watcher must honour its own duration.
# ---------------------------------------------------------------------------


class _SilentProc:
    """A logcat that is alive but emits nothing, so readline() blocks.

    This is the case an emitting fake cannot reproduce: the in-loop clock check
    only runs *after* readline() returns, so a device that logs nothing never
    reaches it. Only a watchdog that terminates the child from outside the loop
    can end that wait, which is what log_monitor already does.
    """

    def __init__(self):
        self._released = threading.Event()
        self.terminated = False
        self.stdout = self
        self.stderr = None
        self.returncode = None

    def readline(self) -> str:
        # Blocks until something terminates us, exactly like a quiet logcat.
        self._released.wait(timeout=30)
        return ""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self._released.set()

    def kill(self):
        self.terminate()


def test_legacy_watch_stops_even_when_the_device_is_silent(monkeypatch):
    """The A2 defect, still present here after log_monitor's was fixed.

    `watch()` checks its duration only after `readline()` returns, so a device
    that logs nothing blocks forever. A real (short) duration is used rather
    than a fake clock, because the whole point is that no code in the loop runs
    to observe a fake clock.
    """
    import anr_watcher

    proc = _SilentProc()
    monkeypatch.setattr(anr_watcher.subprocess, "Popen", lambda *_a, **_k: proc)

    finished = threading.Event()

    def _run():
        anr_watcher.AnrWatcher(serial="emulator-5554").watch(duration_seconds=1, json_mode=True)
        finished.set()

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    assert finished.wait(timeout=15), (
        "watch(duration_seconds=1) did not return on a silent device; "
        "--duration hangs until the stream produces a line"
    )
    assert proc.terminated
