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

Two behaviours are simulated rather than replayed, because ignoring them would
make a spec pass against the very bug it pins. ``logcat --pid=N`` prints only
the lines whose PID column is N; a rule opts in with ``filter_pid=True``. And a
following ``logcat -T N`` starts at the end of what the device had already
buffered -- its last N lines -- before what arrives after; a rule that gives
that history as ``buffered=`` gets it whole without ``-T`` and only its tail
with it. Both are documented logcat behaviour, applied to a recorded threadtime
transcript; everything else ``logcat`` can be asked (``-t``, ``*:W``, ``-b``)
is ignored, which only ever makes the stub answer *more* than a device would.

Replayed text is byte-exact: a carriage return inside a recorded line stays
inside it, as it does on the device, whose logcat ends a line only at ``\\n``.

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

import pytest

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
import shutil
import sys

with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps([spec["name"], *argv]) + "\\n")

for rule in spec["rules"]:
    if not all(token in argv for token in rule["match"]):
        continue
    for destination, source in rule["writes"]:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(source, encoding="utf-8") as src, open(destination, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    if rule["pull_from"]:
        # `adb pull <device path> <host path>`: the host path is the last argument.
        shutil.copyfile(rule["pull_from"], argv[-1])
    def read(path):
        # newline="": a carriage return is replayed, not turned into a newline.
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()

    def lines(text):
        # Split where logcat does -- at "\\n" only -- keeping the ends.
        parts = text.split("\\n")
        return [part + "\\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

    out = ""
    if rule["buffered"]:
        # logcat -T N: the last N lines already buffered, then what follows.
        history = lines(read(rule["buffered"]))
        tail = next((argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-T"), None)
        if tail is not None:
            history = history[-int(tail):]
        out = "".join(history)
    if rule["stdout"]:
        out += read(rule["stdout"])
    pid = next((arg.split("=", 1)[1] for arg in argv if arg.startswith("--pid=")), None)
    if rule["filter_pid"] and pid is not None:
        # logcat --pid=N: only the lines whose threadtime PID column is N.
        out = "".join(
            line for line in lines(out)
            if len(line.split()) > 2 and line.split()[2] == pid
        )
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.flush()
    if rule["stderr"]:
        with open(rule["stderr"], encoding="utf-8") as handle:
            sys.stderr.write(handle.read())
        sys.stderr.flush()
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
    # What the device had already buffered when a following `logcat` began:
    # replayed whole before ``stdout``, or only its last N lines under `-T N`.
    # Recorded text, like ``stdout``.
    buffered: str = ""
    # Files the tool writes as a side effect, as (absolute path, text). Gradle
    # writes its JUnit XML during the run; a stub that did not would leave the
    # script reading whatever an earlier run left behind.
    writes: tuple[tuple[str, str], ...] = ()
    # A RECORDED binary file (its path) that `adb pull` delivers to the host
    # path it was given -- the recorded `screencap -p` PNG, for a capture that
    # has to succeed end to end. Copied byte for byte; nothing is generated.
    pull_from: str = ""


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
                    "buffered": self._stage(rule.buffered),
                    "writes": [[str(path), self._stage(text)] for path, text in rule.writes],
                    "pull_from": rule.pull_from,
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
            if found is not None and Path(found).parent != self.bin:
                # pytest.fail, not assert: an unisolated harness must never be
                # excused as the defect a spec pins (raises=AssertionError).
                pytest.fail(
                    f"{tool} resolves to {found}, outside the fake bin -- a real SDK "
                    f"tool is reachable and these specs would drive a real device"
                )


# --- uid answers: whose process a pid is -----------------------------------------
#
# log_monitor --app and app_state_capture confirm a pid as the app's by its uid
# (common.logcat.app_window): `cmd package list packages -U <package>` for the
# package's uid, `ps -o PID,UID -p <pids>` for a process's. A fake device that
# says "pidof <package> -> P" must also say "P's uid is the package's", or the
# fake contradicts itself. Both answers are the RECORDED ones (package_list_uid,
# ps_pid_uid), with only the package name or the pid substituted -- derivation
# by substitution, as CLAUDE.md allows.


def recorded_package_listing(recorded, package: str) -> str:
    """package_list_uid as if ``package`` were the recorded package (its uid kept)."""
    text = recorded.text("package_list_uid")
    recorded_package = text.split()[0].removeprefix("package:")
    return text.replace(f"package:{recorded_package} ", f"package:{package} ")


def recorded_ps_answer(recorded, pid: str) -> str:
    """ps_pid_uid with its app row's pid replaced by ``pid`` (the uid kept).

    The recorded system_server row stays: a ``ps`` answer naming other pids is
    what the device prints when asked for several.
    """
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == "ps_pid_uid")
    app = entry["pids"]["app"]
    lines = recorded.text("ps_pid_uid").splitlines(keepends=True)
    return "".join(
        line.replace(app, pid, 1) if line.split()[:1] == [app] else line for line in lines
    )


def uid_rules(recorded, package: str, pid: str | None) -> list[Rule]:
    """The package's uid, and -- when ``pid`` is given -- that pid's, as the package's."""
    rules = [Rule(("cmd", "package"), stdout=recorded_package_listing(recorded, package))]
    if pid:
        rules.append(Rule(("shell", "ps"), stdout=recorded_ps_answer(recorded, pid)))
    return rules


# --- the refusal contract ------------------------------------------------------
#
# What a script must print when it refuses, as the shared CLI boundary (PR #18,
# `common/cli.py`) defines it -- the contract the Inc 1 fixes are written
# against. The red specs assert it rather than "non-zero, and the tool's name
# somewhere", which a traceback also satisfies (review of PR #20):
#
#   --json, non-streaming   stdout is exactly one object
#                           {"success": false, "error": <str>, "remedy": <str>}
#   --json, streaming       that object is the LAST line of stdout
#   text                    stderr carries `Error: <str>` then `Remedy: <str>`,
#                           and stdout carries no JSON
#   exit status             1 failure, 2 usage, 3 device unavailable
#
# `error` must NAME what failed -- the tool or the operation -- checked as a
# case-insensitive substring from a set the spec chooses, never as a sentence.


def _parses_as_json(text: str) -> bool:
    """Whether ``text`` as a whole is a JSON document (any value, any layout)."""
    if not text.strip():
        return False
    try:
        json.loads(text.strip())
    except json.JSONDecodeError:
        return False
    return True


def _json_lines(text: str) -> list[str]:
    """Lines of ``text`` that parse, on their own, as a JSON object."""
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            if isinstance(json.loads(stripped), dict):
                found.append(stripped)
        except json.JSONDecodeError:
            continue
    return found


def assert_refusal(
    result: subprocess.CompletedProcess,
    *,
    json_mode: bool,
    naming: tuple[str, ...],
    status: int,
    streaming: bool = False,
) -> dict | None:
    """Assert ``result`` is a refusal in the shared boundary's shape.

    Args:
        result: The finished run.
        json_mode: Whether ``--json`` was asked for.
        naming: Any one of these (case-insensitive) must appear in the error --
            the tool or operation that failed.
        status: The exit status the refusal must carry (1, 2 or 3).
        streaming: A streaming mode, where the refusal is the last stdout line.

    Returns:
        The parsed refusal object under ``--json``, else None.

    Raises:
        AssertionError: on any departure from the contract. A malformed
            document that LOOKS like an object (starts and ends with a brace)
            raises JSONDecodeError instead: that is a broken fix, not the
            pinned defect, and ``raises=AssertionError`` must not excuse it.
    """
    names = tuple(name.lower() for name in naming)
    shown = f"\nstdout: {result.stdout[-600:]!r}\nstderr: {result.stderr[-600:]!r}"

    # Either stream: a traceback printed to stdout ahead of a well-formed last
    # line would otherwise pass a streaming refusal (verification of PR #20).
    assert "Traceback" not in result.stderr + result.stdout, f"refused by crashing:{shown}"
    assert result.returncode == status, f"exit {result.returncode}, not {status}{shown}"

    if json_mode:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert lines, f"--json printed nothing on stdout{shown}"
        document = lines[-1] if streaming else result.stdout.strip()
        assert document.startswith("{") and document.endswith(
            "}"
        ), f"stdout is not {'a last line holding ' if streaming else ''}one JSON object{shown}"
        objects = len(_json_lines(result.stdout))
        assert streaming or objects <= 1, f"stdout holds {objects} JSON objects, not one{shown}"
        payload = json.loads(document)
        assert isinstance(payload, dict), f"not an object: {payload!r}"
        assert payload.get("success") is False, f"success is not false: {payload!r}"
        error, remedy = payload.get("error"), payload.get("remedy")
        assert isinstance(error, str) and error.strip(), f"no error string: {payload!r}"
        assert isinstance(remedy, str) and remedy.strip(), f"no remedy string: {payload!r}"
        assert any(
            name in error.lower() for name in names
        ), f"the error does not name any of {naming}: {error!r}"
        return payload

    stderr_lines = result.stderr.splitlines()
    errors = [i for i, line in enumerate(stderr_lines) if line.startswith("Error:")]
    remedies = [i for i, line in enumerate(stderr_lines) if line.startswith("Remedy:")]
    assert errors, f"no `Error:` line on stderr{shown}"
    assert remedies and remedies[-1] > errors[0], f"no `Remedy:` line after the error{shown}"
    assert any(
        name in stderr_lines[i].lower() for i in errors for name in names
    ), f"no `Error:` line names any of {naming}{shown}"
    assert not _json_lines(result.stdout), f"text mode printed JSON on stdout{shown}"
    # Per line catches one-line objects; the whole of stdout catches what a
    # line-by-line look cannot -- an indented object, `null`, an array.
    assert not _parses_as_json(result.stdout), f"text mode printed JSON on stdout{shown}"
    return None
