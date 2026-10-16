"""A fake Android toolchain on PATH, answering with RECORDED output only.

The red specifications for v0.8.0 Inc 1 assert what an agent sees -- exit
status, printed text, JSON -- so they run the real CLI as a subprocess, and the
CLI reaches for ``adb`` / ``emulator`` / ``avdmanager`` / ``sdkmanager`` /
``gradlew`` by name. This module puts a stub of each on a PATH that contains
nothing else from the SDK, the same arrangement as ``test_exit_code_sweep.py``,
with two differences that the specs need:

* **A stub answers per subcommand.** ``adb devices -l`` and ``adb logcat`` are
  different questions, and a spec about a log window needs the first to succeed
  so that the second is reached. Each stub is given ordered :class:`Rule`
  objects; the first whose tokens all appear in argv wins, and anything
  unmatched exits with the stub's default status and prints nothing.

* **What a stub prints is handed to it by the test, from the recorded corpus.**
  Nothing in this file is tool output. A rule's ``stdout`` is whatever the test
  read through the ``recorded`` / ``recorded_gradle`` fixtures (or derived from
  them, the way CLAUDE.md permits), written to a file beside the stub and
  replayed verbatim. A stub with no rule for a question answers with silence,
  never with a plausible sentence.

One behaviour is simulated rather than replayed, because ignoring it would make
the D4 spec pass against the very bug it pins: ``logcat --pid=N`` prints only
the lines whose PID column is N. A rule opts in with ``filter_pid=True``. That
is documented logcat behaviour, applied to a recorded threadtime transcript;
everything else ``logcat`` can be asked (``-t``, ``*:W``, ``-b``) is ignored,
which only ever makes the stub answer *more* than a device would.

Every invocation of every stub is appended to a log, so a spec can say both
"the script answered X" and "the script asked the device Y" -- and so a spec
that passes without the script ever reaching a tool can be caught.

The PATH handed to the script is ``<stub bin>:/usr/bin:/bin`` and
``ANDROID_HOME`` is an empty directory, so nothing real is reachable. This
matters on the machine these tests are written on: a phone is attached, and
every one of these scripts would otherwise drive it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"

# The stubs exit immediately, so a run that takes longer than this is hung.
RUN_TIMEOUT_SECONDS = 60

# Directories that must stay reachable for the interpreter and coreutils, and
# that hold no Android tool on the machines this suite runs on (asserted by
# `FakeToolchain.assert_isolated`).
SYSTEM_PATH = ("/usr/bin", "/bin")

ANDROID_TOOLS = ("adb", "emulator", "avdmanager", "sdkmanager")

_STUB = """#!{python}
# Fake {name}: replays recorded output per subcommand. See tests/fake_toolchain.py.
import json
import os
import sys
import time

with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps([spec["name"], *argv]) + "\\n")

for rule in spec["rules"]:
    if not all(token in argv for token in rule["match"]):
        continue
    if rule["once"]:
        if os.path.exists(rule["once"]):
            continue
        open(rule["once"], "w").close()
    for destination, source in rule["writes"]:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(source, encoding="utf-8") as src, open(destination, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    out = ""
    if rule["stdout"]:
        with open(rule["stdout"], encoding="utf-8") as handle:
            out = handle.read()
    pid = next((arg.split("=", 1)[1] for arg in argv if arg.startswith("--pid=")), None)
    if rule["filter_pid"] and pid is not None:
        # logcat --pid=N: only the lines whose threadtime PID column is N.
        out = "".join(
            line for line in out.splitlines(True)
            if len(line.split()) > 2 and line.split()[2] == pid
        )
    if rule["pace"]:
        for line in out.splitlines(True):
            sys.stdout.write(line)
            sys.stdout.flush()
            time.sleep(rule["pace"])
    else:
        sys.stdout.write(out)
        sys.stdout.flush()
    if rule["stderr"]:
        with open(rule["stderr"], encoding="utf-8") as handle:
            sys.stderr.write(handle.read())
        sys.stderr.flush()
    if rule["hold"]:
        time.sleep(rule["hold"])
    sys.exit(rule["exit"])

sys.exit(spec["default_exit"])
"""


@dataclass(frozen=True)
class Rule:
    """One answer: when every token in ``match`` is in argv, replay this.

    ``stdout`` / ``stderr`` are the TEXT to replay. Pass recorded output, never
    a literal -- tests/test_fixture_policy.py enforces that for this keyword.
    """

    match: tuple[str, ...]
    stdout: str = ""
    stderr: str = ""
    exit: int = 0
    filter_pid: bool = False
    # Seconds to keep stdout open after writing, the way a streaming
    # `adb logcat` does -- so a reader sees the lines arrive on a live stream
    # rather than a pipe that closed before it started reading.
    hold: float = 0.0
    # Files the tool writes as a side effect, as (absolute path, text). Gradle
    # writes its JUnit XML during the run; a stub that did not would leave the
    # script reading whatever an earlier run left behind.
    writes: tuple[tuple[str, str], ...] = ()
    # Seconds between lines, each written and flushed on its own, the way a
    # live `adb logcat` delivers them. Without it a whole recording lands in
    # one pipe write, and anr_watcher's worker -- which select()s on a
    # buffered text pipe (anr_watcher.py:659-670) -- loses the lines still in
    # Python's buffer if the stream ends during a select timeout. That is a
    # defect of its own; the D5/A5 specs are about classification, so the
    # stream they read is paced and the race is out of their way.
    pace: float = 0.0
    # Answer only the first matching call; later calls fall through to the
    # next rule. A logcat stream a worker re-spawns must not replay the same
    # recording, or one ANR is counted once per restart.
    once: bool = False


class FakeToolchain:
    """A scratch SDK: stub bin directory, empty ANDROID_HOME, private HOME."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.sdk = root / "sdk"
        self.home = root / "home"
        self.tmp = root / "tmp"
        self.log = root / "tools.log"
        for directory in (self.bin, self.sdk, self.home, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        self.log.touch()
        self._staged = 0

    # --- installing stubs ---------------------------------------------------

    def _stage(self, text: str) -> str:
        """Write ``text`` beside the stubs and return its path ("" for none)."""
        if not text:
            return ""
        self._staged += 1
        path = self.root / f"staged-{self._staged}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _once_marker(self) -> str:
        """A path whose existence records that a ``once`` rule has answered."""
        self._staged += 1
        return str(self.root / f"answered-{self._staged}")

    def install(
        self,
        name: str,
        rules: list[Rule] | tuple[Rule, ...] = (),
        *,
        default_exit: int = 1,
        directory: Path | None = None,
    ) -> Path:
        """Write a stub ``name`` into ``directory`` (the stub bin by default)."""
        target_dir = directory or self.bin
        target_dir.mkdir(parents=True, exist_ok=True)
        self._staged += 1
        spec_path = self.root / f"{name}-{self._staged}.spec.json"
        spec = {
            "name": name,
            "log": str(self.log),
            "default_exit": default_exit,
            "rules": [
                {
                    "match": list(rule.match),
                    "stdout": self._stage(rule.stdout),
                    "stderr": self._stage(rule.stderr),
                    "exit": rule.exit,
                    "filter_pid": rule.filter_pid,
                    "hold": rule.hold,
                    "pace": rule.pace,
                    "once": self._once_marker() if rule.once else "",
                    "writes": [[str(path), self._stage(text)] for path, text in rule.writes],
                }
                for rule in rules
            ],
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        stub = target_dir / name
        stub.write_text(_STUB.format(python=sys.executable, name=name, spec=str(spec_path)))
        stub.chmod(0o755)
        return stub

    def install_broken(self, name: str) -> Path:
        """A tool that is on PATH and answers every call with exit 127.

        The exit-code sweep's "SDK tool is not installed" double. It prints
        nothing: the status is the whole signal, and any message would be
        invented.
        """
        return self.install(name, (), default_exit=127)

    # --- running ------------------------------------------------------------

    def env(self, **extra: str) -> dict[str, str]:
        """A from-scratch environment: nothing inherited, nothing real reachable."""
        env = {
            "PATH": os.pathsep.join([str(self.bin), *SYSTEM_PATH]),
            "HOME": str(self.home),
            "ANDROID_HOME": str(self.sdk),
            "ANDROID_SDK_ROOT": str(self.sdk),
            "ANDROID_SDK_HOME": str(self.home),
            "ANDROID_EMU_ANR_HOME": str(self.home),
            "TMPDIR": str(self.tmp),
        }
        env.update(extra)
        return env

    def apply(self, monkeypatch, **extra: str) -> None:
        """The same environment, for a spec that has to run ``main()`` in-process."""
        for key in ("ANDROID_SERIAL", "ANDROID_AVD_HOME", "ANDROID_EMULATOR_HOME"):
            monkeypatch.delenv(key, raising=False)
        for key, value in self.env(**extra).items():
            monkeypatch.setenv(key, value)

    def run(
        self,
        script: str,
        *argv: str,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = RUN_TIMEOUT_SECONDS,
        **extra_env: str,
    ) -> subprocess.CompletedProcess:
        """Run one skill script with this toolchain and nothing else on PATH."""
        path = SCRIPTS / script
        interpreter = ["/bin/bash"] if script.endswith(".sh") else [sys.executable]
        return subprocess.run(
            [*interpreter, str(path), *argv],
            env=self.env(**extra_env),
            cwd=str(cwd or self.tmp),
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    # --- reading back -------------------------------------------------------

    def calls(self, tool: str | None = None) -> list[list[str]]:
        """argv of every stub invocation so far, optionally for one tool."""
        entries = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [entry[1:] for entry in entries if tool is None or entry[0] == tool]

    def assert_isolated(self) -> None:
        """No real Android tool is reachable from the PATH this harness hands out."""
        path = self.env()["PATH"]
        for tool in ANDROID_TOOLS:
            found = shutil.which(tool, path=path)
            assert found is None or Path(found).parent == self.bin, (
                f"{tool} resolves to {found}, outside the fake bin -- a real SDK "
                f"tool is reachable and these specs would drive a real device"
            )
