"""The unified log entry point: routing, pass-through, and what it must not break.

``logs.py`` is a router, not a reimplementation. That choice is what makes the
consolidation non-breaking, and it is also what these tests have to hold it to:

- the verb selects a module and hands over the argument vector **verbatim**, so
  no second copy of any flag set exists here to drift out of step;
- the delegate's exit status reaches the shell unchanged, through three
  different ending conventions (``return int``, ``sys.exit(int)``,
  ``sys.exit(str)``);
- every script it routes to is still standalone-executable with the flags it
  always had.

Parser assertions read ``tests/fixtures/recorded/`` through the ``recorded``
fixture. Nothing here inlines invented tool output: the three defects this
consolidation must not undo (``-v threadtime``, a terminating ``--duration``,
``--all`` meaning *all*) are pinned against real logcat lines or against the
argument vector actually built, never against a plausible-looking string.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import logs
import pytest

from common import logcat

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"


def _content_lines(text: str) -> list[str]:
    """Recorded logcat body lines, minus the reader's own separators."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.startswith("--------- beginning of")
    ]


# ---------------------------------------------------------------------------
# Routing: one verb per question, and the argv passes through untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", logs.ROUTES, ids=lambda r: r.name)
def test_every_route_names_a_module_that_exists_and_exposes_main(route):
    """A route pointing at nothing would fail only at the moment of use."""
    module = __import__(route.module)
    assert callable(module.main), f"{route.module}.main is not callable"
    assert (SCRIPTS_DIR / route.script).exists(), f"{route.script} is missing"


def test_verb_dispatches_to_its_module(monkeypatch):
    seen: dict = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        return 0

    import crash_triage

    monkeypatch.setattr(crash_triage, "main", fake_main)
    assert logs.main(["crashes", "--package", "com.example.app", "--json"]) == 0
    assert seen["argv"][1:] == ["--package", "com.example.app", "--json"]


def test_argv_passes_through_verbatim_including_flags_the_router_also_defines(monkeypatch):
    """``--json`` belongs to the verb once a verb has been named.

    The router defines ``--json`` too. If it parsed the tail itself, ``logs.py
    tail --json`` would print a routing table instead of logs -- a silent wrong
    answer, which is this repo's characteristic failure. Dispatch therefore
    happens before the router's own parser ever runs.
    """
    seen: dict = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        return 0

    import log_monitor

    monkeypatch.setattr(log_monitor, "main", fake_main)
    logs.main(["tail", "--json", "--verbose", "--serial", "emulator-5554"])
    assert seen["argv"][1:] == ["--json", "--verbose", "--serial", "emulator-5554"]


def test_prog_name_reflects_how_the_agent_invoked_it(monkeypatch):
    """The delegate's own errors should name the command that was typed."""
    seen: dict = {}

    import anr_watcher

    monkeypatch.setattr(anr_watcher, "main", lambda: seen.setdefault("prog", sys.argv[0]))
    logs.main(["anr", "--since", "5m"])
    assert seen["prog"] == "logs.py anr"


def test_sys_argv_is_restored_after_dispatch(monkeypatch):
    import log_monitor

    monkeypatch.setattr(log_monitor, "main", lambda: 0)
    before = list(sys.argv)
    logs.main(["tail", "--duration", "3s"])
    assert sys.argv == before


# ---------------------------------------------------------------------------
# Exit status. The three delegates end three different ways.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        ("return-zero", 0),
        ("return-two", 2),
        ("return-none", 0),
        ("exit-zero", 0),
        ("exit-one", 1),
        ("exit-message", 1),
    ],
)
def test_exit_status_survives_the_router(monkeypatch, ending, expected, capsys):
    """A router that swallowed a non-zero status would report failure as success."""
    endings = {
        "return-zero": lambda: 0,
        "return-two": lambda: 2,
        "return-none": lambda: None,
        "exit-zero": lambda: sys.exit(0),
        "exit-one": lambda: sys.exit(1),
        "exit-message": lambda: sys.exit("device is offline"),
    }
    import crash_triage

    monkeypatch.setattr(crash_triage, "main", endings[ending])
    assert logs.main(["crashes"]) == expected
    if ending == "exit-message":
        assert "device is offline" in capsys.readouterr().err


def test_crash_triage_fail_on_crash_status_reaches_the_shell(monkeypatch):
    """`--fail-on-crash` exits 2; that contract is the whole point of the flag."""
    import crash_triage

    monkeypatch.setattr(crash_triage, "main", lambda: crash_triage.EXIT_CRASHES_FOUND)
    assert logs.main(["crashes", "--fail-on-crash"]) == 2


# ---------------------------------------------------------------------------
# The router's own surface.
# ---------------------------------------------------------------------------


def test_help_lists_every_verb_with_the_question_it_answers():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "logs.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    for route in logs.ROUTES:
        assert route.name in result.stdout
        assert route.question in result.stdout


def test_json_emits_the_routing_table():
    table = logs.routes_json()
    assert {entry["verb"] for entry in table["routes"]} == {r.name for r in logs.ROUTES}
    for entry in table["routes"]:
        assert entry["question"] and entry["source"] and entry["delegates_to"]


def test_bare_invocation_is_a_usage_error_that_still_explains_the_verbs(capsys):
    assert logs.main([]) == 2
    err = capsys.readouterr().err
    assert all(route.name in err for route in logs.ROUTES)


def test_unknown_verb_suggests_the_nearest_one(capsys):
    assert logs.main(["crash"]) == 2
    assert "logs.py crashes" in capsys.readouterr().err


def test_verb_help_reaches_the_delegate_not_the_router():
    """`logs.py tail --help` must document tail's flags, not the router's."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "logs.py"), "tail", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "logs.py tail" in result.stdout
    assert "--follow" in result.stdout and "--last" in result.stdout


# ---------------------------------------------------------------------------
# Nothing was taken away. The four scripts are a published surface.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    ["log_monitor.py", "anr_watcher.py", "crash_triage.py", "app_state_capture.py"],
)
def test_the_delegated_scripts_remain_standalone(script):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize(
    ("script", "flags"),
    [
        (
            "log_monitor.py",
            [
                "--app",
                "--serial",
                "--severity",
                "--follow",
                "--duration",
                "--last",
                "--output",
                "--verbose",
                "--json",
                "--clear",
            ],
        ),
        (
            "crash_triage.py",
            ["--package", "--serial", "--clear", "--fail-on-crash", "--verbose", "--json"],
        ),
        (
            "anr_watcher.py",
            [
                "--watch",
                "--since",
                "--start",
                "--stop",
                "--get-details",
                "--list-sessions",
                "--clear-sessions",
                "--diff",
                "--package",
                "--serial",
                "--duration",
                "--min-frames",
                "--top",
                "--all",
                "--budget-tokens",
                "--cluster",
                "--raw",
                "--older-than",
                "--terse",
                "--json",
            ],
        ),
        ("app_state_capture.py", ["--package", "--serial", "--logs", "--no-logs", "--log-lines"]),
    ],
)
def test_no_documented_flag_disappeared(script, flags):
    """The published flag set is the compatibility contract, so pin it by name."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    text = result.stdout + result.stderr
    missing = [flag for flag in flags if flag not in text]
    assert not missing, f"{script} no longer offers {missing}"


# ---------------------------------------------------------------------------
# The three defects this consolidation must not undo.
# ---------------------------------------------------------------------------


def test_shared_builder_requests_a_format_the_parser_can_read(recorded):
    """A1: `-v time` was requested while the parser understood only threadtime.

    Asserted against both recorded formats, so it fails whichever way the
    mismatch is introduced -- a changed default here, or a changed regex there.
    """
    import log_monitor

    monitor = log_monitor.LogMonitor()
    cmd = monitor.build_logcat_command()
    requested = cmd[cmd.index("-v") + 1]
    assert requested == logcat.DEFAULT_FORMAT

    parsed = [
        monitor.parse_logcat_line(line)
        for line in _content_lines(recorded.text(f"logcat_{requested}"))[:50]
    ]
    assert any(p is not None for p in parsed), (
        f"the shared builder requests '-v {requested}', which parse_logcat_line "
        f"matched in none of the recorded lines of that format"
    )

    other = "time" if requested == "threadtime" else "threadtime"
    wrong = [
        monitor.parse_logcat_line(line)
        for line in _content_lines(recorded.text(f"logcat_{other}"))[:50]
    ]
    assert not any(w is not None for w in wrong), (
        f"parse_logcat_line also matches '-v {other}', so this test can no "
        f"longer tell the two formats apart"
    )


def test_routed_duration_still_arrives_as_seconds(monkeypatch):
    """A2: `--duration` must still bound the capture after passing through logs.py.

    The watchdog that makes it *terminate* is exercised in
    tests/test_log_monitor_streaming.py against the module this routes to; what
    the router can break is the value, so that is what is pinned here.
    """
    seen: dict = {}

    import log_monitor

    def fake_stream(self, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(log_monitor.LogMonitor, "stream_logs", fake_stream)
    assert logs.main(["tail", "--duration", "90s", "--json"]) == 0
    assert seen["duration"] == 90
    assert seen["last_minutes"] is None


def test_routed_all_clusters_still_means_no_cap(monkeypatch):
    """`--all` returned three clusters and deleted the rest; 0 means uncapped."""
    seen: dict = {}

    import anr_watcher

    def fake_stop(self, session_id, **kwargs):
        seen.update(kwargs)
        seen["session_id"] = session_id
        return ""

    monkeypatch.setattr(anr_watcher.AnrBuster, "stop", fake_stop)
    assert logs.main(["anr", "--stop", "anr-abc123", "--all"]) == 0
    assert seen["session_id"] == "anr-abc123"
    assert seen["top_n"] == 0, "--all must disable the top-N cap, not set it to a number"


def test_routed_top_n_is_still_honoured(monkeypatch):
    """Guard the guard: `--all` mapping to 0 must not mean everything maps to 0."""
    seen: dict = {}

    import anr_watcher

    monkeypatch.setattr(
        anr_watcher.AnrBuster,
        "stop",
        lambda self, session_id, **kwargs: seen.update(kwargs) or "",
    )
    assert logs.main(["anr", "--stop", "anr-abc123", "--top", "7"]) == 0
    assert seen["top_n"] == 7
