#!/usr/bin/env python3
"""
Intelligent Emulator Selector

Suggests the best available AVD/device for a task, mirroring the iOS
``simulator_selector.py`` intent with Android-native mechanics.

Ranking factors (highest first):
1. Already running (visible in ``adb devices``)
2. Recently used (persisted to a small config.json under the script dir, or
   ``~/.android-emulator-skill/`` when the script dir is not writable)
3. Latest API level (parsed from each AVD's ``config.ini``)
4. Common test device models (Pixel / mainstream phones)

The ranking function is intentionally pure: :func:`rank_candidates` takes a list
of candidate dicts and returns a scored, sorted list, so it can be unit-tested
without a real emulator, adb, or filesystem.

Usage Examples:
    # Ranked best AVDs for the current host
    python scripts/emulator_selector.py --suggest

    # List every candidate AVD (unranked detail)
    python scripts/emulator_selector.py --list

    # Boot a chosen AVD (delegates to emulator_boot.py / `emulator -avd`)
    python scripts/emulator_selector.py --boot Pixel_9_Pro

    # Machine-readable suggestions
    python scripts/emulator_selector.py --suggest --json

Tunables (env, ANDROID_EMU_ prefix):
    ANDROID_EMU_SELECTOR_COUNT        Default number of suggestions (default: 4)
    ANDROID_EMU_SELECTOR_RECENT_MAX   Recent-AVD history length kept (default: 10)
    ANDROID_EMU_SELECTOR_RUNNING_PTS  Score for an already-running AVD (default: 100)
    ANDROID_EMU_SELECTOR_RECENT_PTS   Score for the most-recently-used AVD (default: 60)
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from common import adb_exec
from common.device_utils import get_connected_devices
from common.env_config import env_int

# Tunable defaults (override via the ANDROID_EMU_ prefix).
DEFAULT_SUGGEST_COUNT = env_int("ANDROID_EMU_SELECTOR_COUNT", 4, min_value=1)
RECENT_HISTORY_MAX = env_int("ANDROID_EMU_SELECTOR_RECENT_MAX", 10, min_value=1)
# Tier bands. The spec mandates a strict priority order (running > recent >
# latest API > common model), so each tier sits in its own band, separated by a
# gap larger than anything a lower tier can contribute. That keeps the ranking a
# single, transparent numeric score while guaranteeing a higher tier never loses
# to a stack of lower-tier bonuses.
RUNNING_SCORE = env_int("ANDROID_EMU_SELECTOR_RUNNING_PTS", 10000, min_value=0)
RECENT_SCORE = env_int("ANDROID_EMU_SELECTOR_RECENT_PTS", 1000, min_value=0)

# Sub-tier weights (kept well below the band gaps above). These only ever break
# ties among candidates that share the same running/recent standing.
LATEST_API_SCORE = 100
COMMON_MODEL_BASE_SCORE = 30
COMMON_MODEL_STEP = 2

# Common test device models ranked by testing priority. Matched (normalized,
# substring) against an AVD's device name / display name / AVD name.
COMMON_MODELS = [
    "pixel 8 pro",
    "pixel 8",
    "pixel 7 pro",
    "pixel 7",
    "pixel 6",
    "pixel",
]

# Ceiling for the per-serial AVD-name query. Short: this runs once per attached
# emulator while building a ranking, so a stalled console must not hold up the
# whole suggestion.
PROBE_TIMEOUT_SECONDS = 5

# `emulator` is an Android SDK tool, not adb, so it does not go through
# common.adb_exec -- but it still needs a ceiling.
EMULATOR_TOOL_TIMEOUT = 30

CONFIG_FILENAME = "config.json"
FALLBACK_CONFIG_DIR = Path.home() / ".android-emulator-skill"
# Pre-0.5 installs kept history next to the script, inside the distributed
# package. Still read if present so nobody loses their history; never created.
LEGACY_CONFIG_PATH = Path(__file__).resolve().parent / CONFIG_FILENAME


def _normalize(value: str) -> str:
    """Lowercase and collapse separators so 'Pixel_8_Pro' ~= 'pixel 8 pro'."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def common_model_rank(candidate: dict) -> int | None:
    """
    Return the 0-based COMMON_MODELS rank a candidate matches, or None.

    Matching is normalized substring across the device name, display name, and
    AVD name so 'Pixel_8_Pro' / 'pixel_8_pro' / 'Pixel 8 Pro' all resolve.

    Args:
        candidate: Candidate dict (see :func:`rank_candidates`).

    Returns:
        Index into COMMON_MODELS for the best (earliest) match, or None.
    """
    haystacks = [
        _normalize(str(candidate.get(key, "")))
        for key in ("device", "display_name", "name")
        if candidate.get(key)
    ]
    for rank, model in enumerate(COMMON_MODELS):
        token = _normalize(model)
        if any(token and token in hay for hay in haystacks):
            return rank
    return None


def score_candidate(candidate: dict, latest_api: int | None) -> tuple[float, list[str]]:
    """
    Score a single candidate and collect human-readable reasons.

    Pure function: depends only on its arguments. ``candidate`` carries the
    pre-resolved ``running``/``recent_index`` flags so this stays device-free.

    Args:
        candidate: Candidate dict with keys:
            - "name": AVD name (required)
            - "api_level": int | None
            - "device" / "display_name": model strings (optional)
            - "running": bool — currently visible in adb devices
            - "recent_index": int | None — position in recent history (0 = most
              recent), or None if never used
        latest_api: Highest API level across all candidates (for the bonus).

    Returns:
        (score, reasons) tuple. Higher score = better recommendation.
    """
    score = 0.0
    reasons: list[str] = []

    if candidate.get("running"):
        score += RUNNING_SCORE
        reasons.append("Currently running")

    recent_index = candidate.get("recent_index")
    if recent_index is not None:
        # Whole recent band, plus a tiny within-band nudge so a more-recent AVD
        # edges out an older one. The nudge stays < 1 so it can never push a
        # recent candidate down into (or a non-recent one up into) another tier.
        score += RECENT_SCORE
        score += (RECENT_HISTORY_MAX - min(recent_index, RECENT_HISTORY_MAX)) / (
            RECENT_HISTORY_MAX + 1
        )
        reasons.append("Recently used" if recent_index == 0 else "Used recently")

    api_level = candidate.get("api_level")
    if api_level is not None:
        # Minor tie-breaker so higher APIs edge out lower ones.
        score += api_level * 0.5
        if latest_api is not None and api_level == latest_api:
            score += LATEST_API_SCORE
            reasons.append(f"Latest API ({api_level})")

    rank = common_model_rank(candidate)
    if rank is not None:
        score += COMMON_MODEL_BASE_SCORE - (rank * COMMON_MODEL_STEP)
        reasons.append(f"#{rank + 1} common model")

    return score, reasons


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """
    Rank candidate AVDs best-first. Pure logic — the unit-test entry point.

    Each input candidate dict is copied and annotated with ``score`` and
    ``reasons``; the top result also gains a "Recommended" reason. The list is
    sorted by score descending, then by name for a stable tie-break.

    Args:
        candidates: List of candidate dicts (see :func:`score_candidate`).

    Returns:
        New list of annotated candidate dicts, sorted best-first.
    """
    if not candidates:
        return []

    api_levels = [c["api_level"] for c in candidates if isinstance(c.get("api_level"), int)]
    latest_api = max(api_levels) if api_levels else None

    ranked: list[dict] = []
    for candidate in candidates:
        enriched = dict(candidate)
        score, reasons = score_candidate(enriched, latest_api)
        enriched["score"] = score
        enriched["reasons"] = reasons
        ranked.append(enriched)

    ranked.sort(key=lambda c: (-c["score"], c.get("name", "")))

    if ranked:
        # Lead the recommendation list with an explicit marker.
        ranked[0]["reasons"] = ["Recommended", *ranked[0]["reasons"]]

    return ranked


class EmulatorSelector:
    """Suggest, list, and boot the best AVD for a task."""

    def __init__(self, config_path: Path | None = None):
        """
        Initialize selector.

        Args:
            config_path: Override for the recent-use config file (tests inject
                a temp path). Defaults to ``config.json`` next to this script,
                falling back to ``~/.android-emulator-skill/config.json``.
        """
        self.config_path = config_path or self._default_config_path()

    @staticmethod
    def _default_config_path() -> Path:
        """Resolve where recent-AVD history lives.

        User state goes under the user's config directory, never into the
        installed package. This previously preferred a file next to the script
        whenever that directory was writable -- which it normally is -- so the
        history was written into the distributed plugin: shared across every
        project and checkout, and lost on reinstall.

        An existing legacy file is still read so nobody silently loses history,
        but a new one is never created there.
        """
        if LEGACY_CONFIG_PATH.exists():
            return LEGACY_CONFIG_PATH
        return FALLBACK_CONFIG_DIR / CONFIG_FILENAME

    def list_avds(self) -> list[dict]:
        """
        List every defined AVD with parsed metadata.

        Returns:
            List of candidate dicts with name, api_level, device, display_name.
        """
        names = self._list_avd_names()
        return [self._build_candidate(name) for name in names]

    def _build_candidate(self, name: str) -> dict:
        """Assemble a candidate dict for ``name`` from its config.ini."""
        config = read_avd_config(name)
        return {
            "name": name,
            "api_level": parse_api_level(config),
            "device": config.get("hw.device.name", ""),
            "display_name": config.get("avd.ini.displayname", ""),
        }

    def _list_avd_names(self) -> list[str]:
        """Return AVD names via ``emulator -list-avds`` (empty on any failure)."""
        try:
            result = subprocess.run(
                ["emulator", "-list-avds"],
                capture_output=True,
                text=True,
                timeout=EMULATOR_TOOL_TIMEOUT,
                check=True,
            )
        except FileNotFoundError:
            print(
                "Error: 'emulator' command not found. "
                "Ensure the Android SDK emulator is on PATH.",
                file=sys.stderr,
            )
            return []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def running_avd_names(self) -> set[str]:
        """
        Return the set of AVD names currently visible via ``adb devices``.

        Each ready emulator serial is resolved to its AVD name via
        ``adb -s <serial> emu avd name``.
        """
        running: set[str] = set()
        try:
            devices = get_connected_devices()
        except RuntimeError:
            # Deliberate: with nothing attached (or no adb at all) there is
            # nothing running, and --suggest must still rank the AVDs on disk.
            # Note this also absorbs an adb_exec.AdbError out of the listing,
            # which subclasses RuntimeError. The per-serial resolution below is
            # outside this guard on purpose -- see _avd_name_for_serial.
            return running

        for dev in devices:
            if dev["type"] != "emulator" or dev["state"] != "device":
                continue
            avd_name = self._avd_name_for_serial(dev["serial"])
            if avd_name:
                running.add(avd_name)
        return running

    def _avd_name_for_serial(self, serial: str) -> str | None:
        """Resolve an emulator serial to its AVD name (None when it answers nothing).

        A console that declines to answer yields None, which simply costs that
        AVD its "currently running" bonus. Device-level adb errors are *not*
        swallowed: the serial came straight from ``adb devices``, so one that can
        no longer be reached means the running check went unanswered, and ranking
        a live AVD as idle is how a second copy of it gets booted.
        """
        result = adb_exec.run_adb("emu", serial, "avd", "name", timeout=PROBE_TIMEOUT_SECONDS)
        # `adb emu avd name` prints the name then an "OK" line.
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and line != "OK":
                return line
        return None

    def load_recent(self) -> list[str]:
        """Load the recent-use AVD history (most-recent first); [] on miss."""
        try:
            data = json.loads(self.config_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        recent = data.get("recent", [])
        if isinstance(recent, list):
            return [str(name) for name in recent]
        return []

    def record_recent(self, name: str) -> None:
        """
        Record ``name`` as most-recently used, de-duplicated and capped.

        Best-effort: write failures are swallowed so booting never fails just
        because the history file is unwritable.
        """
        recent = [name, *(n for n in self.load_recent() if n != name)]
        recent = recent[:RECENT_HISTORY_MAX]

        # Merge rather than replace: this file is shared config, and a
        # whole-file rewrite silently discarded every key except "recent".
        payload: dict = {}
        try:
            payload = json.loads(self.config_path.read_text())
            if not isinstance(payload, dict):
                payload = {}
        except (OSError, json.JSONDecodeError):
            payload = {}

        payload["recent"] = recent
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass

    def suggest(self, count: int = DEFAULT_SUGGEST_COUNT) -> list[dict]:
        """
        Return the top ``count`` ranked candidates for the current host.

        Resolves the live running set and recent history, annotates each
        candidate, then defers ordering to the pure :func:`rank_candidates`.
        """
        candidates = self.list_avds()
        running = self.running_avd_names()
        recent = self.load_recent()
        recent_pos = {name: i for i, name in enumerate(recent)}

        for candidate in candidates:
            candidate["running"] = candidate["name"] in running
            candidate["recent_index"] = recent_pos.get(candidate["name"])

        return rank_candidates(candidates)[:count]

    def boot(self, name: str, headless: bool = False) -> tuple[bool, str]:
        """
        Boot ``name``, delegating to emulator_boot.py when importable.

        Records the AVD in recent history first so the next ``--suggest`` ranks
        it higher even if the boot delegation path varies.

        Args:
            name: AVD name to boot.
            headless: Boot without a GUI window.

        Returns:
            (success, message) tuple.
        """
        self.record_recent(name)

        try:
            from emulator_boot import EmulatorBooter
        except ImportError:
            return self._boot_via_cli(name, headless)

        booter = EmulatorBooter(name)
        return booter.boot(wait_ready=False, headless=headless)

    @staticmethod
    def _boot_via_cli(name: str, headless: bool) -> tuple[bool, str]:
        """Fallback boot using ``emulator -avd <name>`` directly."""
        cmd = ["emulator", "-avd", name]
        if headless:
            cmd.append("-no-window")
        try:
            # Exempt from run_adb/timeout: this is not an adb call, and the
            # emulator process is meant to outlive this script -- bounding it
            # would kill the emulator we just launched.
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return False, "Error: 'emulator' command not found. Ensure the Android SDK is on PATH."
        except Exception as e:
            return False, f"Boot error: {e}"
        return True, f"Emulator booting: {name} (use emulator_boot.py --wait-ready to wait)"


def _is_writable_dir(path: Path) -> bool:
    """Return True if ``path`` is an existing, writable directory."""
    import os

    return path.is_dir() and os.access(path, os.W_OK)


def read_avd_config(name: str) -> dict[str, str]:
    """
    Read an AVD's ``config.ini`` into a flat key->value dict.

    Looks under ``~/.android/avd/<name>.avd/config.ini``. Returns an empty dict
    when the file is missing or unreadable so callers can degrade gracefully.

    Args:
        name: AVD name (without the ``.avd`` suffix).

    Returns:
        Dict of config.ini keys to string values.
    """
    config_path = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    try:
        text = config_path.read_text()
    except (FileNotFoundError, OSError):
        return {}
    return parse_config_ini(text)


def parse_config_ini(text: str) -> dict[str, str]:
    """
    Parse simple ``key=value`` AVD config.ini text into a dict.

    Pure helper (no I/O) so it is directly unit-testable.

    Args:
        text: Raw config.ini contents.

    Returns:
        Dict of stripped keys to stripped values; blank/comment lines skipped.
    """
    config: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config


def parse_api_level(config: dict[str, str]) -> int | None:
    """
    Extract the Android API level from a parsed config.ini dict.

    Tries ``image.sysdir.1`` (e.g. ``system-images/android-36/...``) first, then
    falls back to ``target`` (e.g. ``android-36``). Pure logic.

    Args:
        config: Parsed config.ini dict (see :func:`parse_config_ini`).

    Returns:
        API level as int, or None when it cannot be determined.
    """
    for key in ("image.sysdir.1", "target"):
        value = config.get(key, "")
        match = re.search(r"android-(\d+)", value)
        if match:
            return int(match.group(1))
    return None


def format_candidates(candidates: list[dict], json_format: bool = False) -> str:
    """
    Format candidates for output (token-efficient by default).

    Args:
        candidates: Ranked/annotated candidate dicts.
        json_format: If True, emit pretty JSON.

    Returns:
        Formatted output string.
    """
    if json_format:
        return json.dumps({"suggestions": candidates}, indent=2)

    if not candidates:
        return "No AVDs found"

    lines = []
    for i, candidate in enumerate(candidates, 1):
        api = candidate.get("api_level")
        api_str = f"API {api}" if api is not None else "API ?"
        line = f"{i}. {candidate['name']} ({api_str})"
        reasons = candidate.get("reasons")
        if reasons:
            line += f" - {', '.join(reasons)}"
        lines.append(line)
    return "\n".join(lines)


def main():
    """Main entry point: run the CLI, reporting adb failures without a traceback."""
    try:
        _run()
    except adb_exec.AdbError as error:
        # run_adb raises errors whose message already names a remedy. Print it
        # rather than a traceback -- for an agent, stderr is the retry prompt.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


def _run():
    parser = argparse.ArgumentParser(
        description="Suggest, list, and boot the best Android AVD for a task",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ranked best AVDs
  python scripts/emulator_selector.py --suggest

  # List every candidate AVD
  python scripts/emulator_selector.py --list

  # Boot a chosen AVD (delegates to emulator_boot.py)
  python scripts/emulator_selector.py --boot Pixel_9_Pro

  # Suggestions as JSON
  python scripts/emulator_selector.py --suggest --json
        """,
    )

    parser.add_argument("--suggest", action="store_true", help="Show ranked best AVDs (default)")
    parser.add_argument("--list", action="store_true", help="List all candidate AVDs")
    parser.add_argument("--boot", metavar="NAME", help="Boot the named AVD")
    parser.add_argument("--headless", action="store_true", help="Boot in headless mode (no GUI)")
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_SUGGEST_COUNT,
        help=(
            f"Number of suggestions (default: {DEFAULT_SUGGEST_COUNT}, "
            "override via ANDROID_EMU_SELECTOR_COUNT)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()
    selector = EmulatorSelector()

    # Boot mode
    if args.boot:
        success, message = selector.boot(args.boot, headless=args.headless)
        if args.json:
            print(
                json.dumps(
                    {"action": "boot", "avd": args.boot, "success": success, "message": message},
                    indent=2,
                )
            )
        else:
            print(message)
        sys.exit(0 if success else 1)

    # List mode (every candidate, ranked for context)
    if args.list:
        candidates = selector.suggest(count=len(selector.list_avds()) or 1)
        print(format_candidates(candidates, args.json))
        sys.exit(0)

    # Default + --suggest: ranked top N
    suggestions = selector.suggest(args.count)
    if args.json or args.verbose:
        print(format_candidates(suggestions, args.json))
    elif suggestions:
        # Token-efficient default: the single best pick plus its reasons.
        best = suggestions[0]
        api = best.get("api_level")
        api_str = f"API {api}" if api is not None else "API ?"
        reasons = best.get("reasons") or []
        suffix = f" - {', '.join(reasons)}" if reasons else ""
        print(f"Recommended: {best['name']} ({api_str}){suffix}")
    else:
        print("No AVDs found")
    sys.exit(0)


if __name__ == "__main__":
    main()
