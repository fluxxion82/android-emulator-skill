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
the records whose pid is N; a rule opts in with ``filter_pid=True``. And a
following ``logcat -T N`` starts at the end of what the device had already
buffered -- its last N RECORDS -- before what arrives after; a rule that gives
that history as ``buffered=`` gets it whole without ``-T`` and only its tail
with it. logd tails records, not lines: a 13-line crash is one record, and
``-T 1`` prints all 13 lines (recorded: logcat_crash_tail, the tail of
logcat_crash_buffer). In a recorded threadtime transcript a record is a run of
lines sharing one header (``-v usec``: to the microsecond), and the buffer's
separator line before it is printed too; in recorded ``-B`` bytes
(``stdout``/``buffered`` given as bytes) a record is logd's own frame.
``-t <epoch>`` over recorded records answers only those stamped AFTER it --
logcat's own reading of an epoch ``-t`` (measured: logcat_stream_marks_records'
``since_is_strict``), which the live readers' catch-up asks for. Everything
else ``logcat`` can be asked (a ``MM-DD`` ``-t``, ``*:W``, ``-b``) is ignored,
which only ever makes the stub answer *more* than a device would.

Replayed text is byte-exact: a carriage return inside a recorded line stays
inside it, as it does on the device, whose logcat ends a line only at ``\\n``.
Replayed records are byte-exact too: bytes are written as they were recorded.

More behaviours are simulated: ``hang=True`` answers and then does not
exit until it is killed, as a streaming ``adb logcat`` aimed at a device that is
not attached waits (recorded: adb_waiting_for_device); ``follows=True`` does
the same for a following ``logcat`` only (no ``-d``), as a device's live read
never ends by itself, so its reader has to stop it; and ``once=True``
answers the first call it matches only, so a device can be there for one
question and gone for the next. And ``logged=<placeholder>`` stands for logd
storing what ``adb shell log -t TAG MSG`` wrote: a recorded frame whose message
is the placeholder is absent from the answer until such a call is in the log,
then carries that call's tag and message (the latest call's) -- the line the
script wrote is in the buffer only after it wrote it, where the test put it. A script that never stops such a stub is
stopped by :meth:`FakeToolchain.run`'s limit, which takes the stub with it.

Every invocation of every stub is appended to a log, so a spec can say both
"the script answered X" and "the script asked the device Y" -- and so a spec
that passes without the script ever reaching a tool can be caught.

The PATH handed to the script is ``<stub bin>:/usr/bin:/bin`` and
``ANDROID_HOME`` is an empty directory, so nothing real is reachable. This
matters on the machine these tests are written on: a phone is attached, and
every one of these scripts would otherwise drive it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "android-emulator-skill" / "skills" / "android-emulator-skill" / "scripts"

# The stubs exit immediately, so a run that takes longer than this is hung.
RUN_TIMEOUT_SECONDS = 60
# How long a killed run's pipes may take to close. Only a process outside its
# group (a detached worker, which writes to neither) could hold them open.
REAP_TIMEOUT_SECONDS = 10

# Directories that must stay reachable for the interpreter and coreutils, and
# that hold no Android tool on the machines this suite runs on (asserted by
# `FakeToolchain.assert_isolated`).
SYSTEM_PATH = ("/usr/bin", "/bin")

ANDROID_TOOLS = ("adb", "emulator", "avdmanager", "sdkmanager")

_STUB = """#!{python}
# Fake {name}: replays recorded output per subcommand. See tests/fake_toolchain.py.
import json
import os
import re
import shutil
import struct
import sys
import time

with open({spec!r}, encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps([spec["name"], *argv]) + "\\n")
# Its pid beside it, so a spec can ask whether a stub outlived the run.
with open(spec["log"] + ".pids", "a", encoding="utf-8") as pids:
    pids.write(json.dumps([os.getpid(), spec["name"], *argv]) + "\\n")

# A threadtime header: everything before the message. A run of lines sharing
# one is one record.
HEADER = re.compile(r"^\\S+ \\S+\\s+\\d+\\s+\\d+ \\S [^:]*:")
SEPARATOR = b"--------- beginning of"

def split_lines(data):
    # Split where logcat does -- at "\\n" only -- keeping the ends.
    parts = data.split(b"\\n")
    return [part + b"\\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

def text_records(data):
    # ("sep", [line]) for a buffer separator, ("rec", [lines]) per record.
    items, previous = [], None
    for line in split_lines(data):
        if line.startswith(SEPARATOR):
            items.append(("sep", [line]))
            previous = None
            continue
        match = HEADER.match(line.decode("utf-8", "replace"))
        key = match.group(0) if match else None
        if key is not None and key == previous and items and items[-1][0] == "rec":
            items[-1][1].append(line)
        else:
            items.append(("rec", [line]))
        previous = key
    return items

def binary_records(data):
    # logd's frames: u16 payload length, u16 header size, i32 pid, ...
    records, offset = [], 0
    while offset + 8 <= len(data):
        length, header = struct.unpack_from("<HH", data, offset)
        end = offset + header + length
        records.append((str(struct.unpack_from("<i", data, offset + 4)[0]), data[offset:end]))
        offset = end
    return records

def whole_records(data):
    # (the whole frames, the bytes after them): a dump cut inside a record --
    # however few of its bytes arrived -- stays cut when it is filtered.
    frames, offset = [], 0
    while offset + 20 <= len(data):
        length, header = struct.unpack_from("<HH", data, offset)
        end = offset + header + length
        if header < 20 or end > len(data):
            break
        frames.append(data[offset:end])
        offset = end
    return frames, data[offset:]

def logged_lines():
    # (tag, message) of every `shell log -t TAG MSG` asked of this toolchain, in order.
    out = []
    with open(spec["log"], encoding="utf-8") as handle:
        for line in handle:
            call = json.loads(line)[1:]
            at = next((i for i in range(len(call) - 1) if call[i : i + 2] == ["shell", "log"]), None)
            if at is not None and "-t" in call[at:]:
                tag = call.index("-t", at)
                out.append((call[tag + 1], " ".join(call[tag + 2 :])))
    return out

def with_logged(data, placeholder):
    # A frame whose message is the placeholder: gone until a `shell log` call is in
    # the log, then that call's line -- the frame's header, its tag and message.
    logged = logged_lines()
    out = b""
    for _, raw in binary_records(data):
        header = struct.unpack_from("<H", raw, 2)[0]
        payload = raw[header:]
        _tag, _, rest = payload[1:].partition(b"\\0")
        message, _, trailer = rest.partition(b"\\0")
        if message != placeholder.encode("utf-8"):
            out += raw
            continue
        if not logged:
            continue
        tag, text = logged[-1]
        body = payload[:1] + tag.encode("utf-8") + b"\\0" + text.encode("utf-8") + b"\\0" + trailer
        out += struct.pack("<H", len(body)) + raw[2:header] + body
    return out

def tail_text(data, count):
    items = text_records(data)
    recs = [i for i, (kind, _) in enumerate(items) if kind == "rec"]
    if count >= len(recs):
        return data
    first = recs[-count] if count else len(items)
    seps = [i for i in range(first) if items[i][0] == "sep"]
    chosen = ([items[seps[-1]]] if seps else []) + items[first:]
    return b"".join(line for _, lines in chosen for line in lines)

for index, rule in enumerate(spec["rules"]):
    if not all(token in argv for token in rule["match"]):
        continue
    if rule["once"]:
        # Answers the first matching call only; later calls fall through.
        used = spec["log"] + ".used-" + spec["name"] + "-" + str(index)
        if os.path.exists(used):
            continue
        open(used, "w").close()
    for destination, source in rule["writes"]:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(source, encoding="utf-8") as src, open(destination, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    if rule["pull_from"]:
        # `adb pull <device path> <host path>`: the host path is the last argument.
        shutil.copyfile(rule["pull_from"], argv[-1])

    def read(path):
        # Bytes: a carriage return is replayed, never turned into a newline.
        with open(path, "rb") as handle:
            return handle.read()

    binary = rule["binary"]
    tail = next((argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-T"), None)
    out = b""
    if rule["buffered"]:
        # logcat -T N: the last N RECORDS already buffered, then what follows.
        history = read(rule["buffered"])
        if rule["logged"]:
            history = with_logged(history, rule["logged"])
        if tail is not None:
            history = (
                b"".join(raw for _, raw in binary_records(history)[-int(tail):])
                if binary
                else tail_text(history, int(tail))
            )
        out = history
    if rule["stdout"]:
        out += (
            with_logged(read(rule["stdout"]), rule["logged"])
            if rule["logged"]
            else read(rule["stdout"])
        )
    pid = next((arg.split("=", 1)[1] for arg in argv if arg.startswith("--pid=")), None)
    if rule["filter_pid"] and pid is not None:
        # logcat --pid=N: only the records whose pid is N.
        if binary:
            out = b"".join(raw for owner, raw in binary_records(out) if owner == pid)
        else:
            out = b"".join(
                line for line in split_lines(out)
                if len(line.split()) > 2 and line.split()[2] == pid.encode()
            )
    since = next((argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-t"), None)
    if binary and since is not None and re.fullmatch(r"\\d+\\.\\d+", since):
        # logcat -t <epoch>: the records stamped after it, to the nanosecond.
        # A cut record after them is not filtered away: a cut dump stays cut.
        seconds, _, fraction = since.partition(".")
        limit = (int(seconds), int(fraction.ljust(9, "0")[:9]))
        whole, cut = whole_records(out)
        out = b"".join(raw for raw in whole if struct.unpack_from("<II", raw, 12) > limit) + cut
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    if rule["stderr"]:
        sys.stderr.buffer.write(read(rule["stderr"]))
        sys.stderr.flush()
    if rule["hang"] or (rule["follows"] and "-d" not in argv):
        # Waits like `adb logcat` for a device that is not there, or -- a
        # following read -- for records that are not coming, until killed.
        time.sleep(3600)
    sys.exit(rule["exit"])

sys.exit(spec["default_exit"])
"""


@dataclass(frozen=True)
class Rule:
    """One answer: when every token in ``match`` is in argv, replay this.

    ``stdout`` / ``stderr`` are the TEXT to replay. Pass recorded output, never
    a literal -- tests/test_fixture_policy.py enforces that for this keyword.
    ``stdout`` and ``buffered`` may instead be BYTES: a recorded ``logcat -B``
    (``recorded.data("<name>.bin")``), replayed byte for byte; both must then
    be bytes, and ``-T`` / ``--pid`` act on logd's record frames.
    """

    match: tuple[str, ...]
    stdout: str | bytes = ""
    stderr: str = ""
    exit: int = 0
    filter_pid: bool = False
    # What the device had already buffered when a following `logcat` began:
    # replayed whole before ``stdout``, or only its last N records under `-T N`.
    # Recorded, like ``stdout``.
    buffered: str | bytes = ""
    # Files the tool writes as a side effect, as (absolute path, text). Gradle
    # writes its JUnit XML during the run; a stub that did not would leave the
    # script reading whatever an earlier run left behind.
    writes: tuple[tuple[str, str], ...] = ()
    # A RECORDED binary file (its path) that `adb pull` delivers to the host
    # path it was given -- the recorded `screencap -p` PNG, for a capture that
    # has to succeed end to end. Copied byte for byte; nothing is generated.
    pull_from: str = ""
    # Answer, then wait until killed instead of exiting -- a streaming `adb
    # logcat` aimed at a device that is not attached (adb_waiting_for_device).
    hang: bool = False
    # A following `logcat` (no `-d`): answer, then wait until killed, as a
    # device's does -- a live read ends only when its reader stops it. A dump
    # (`-d`) answers and exits as before.
    follows: bool = False
    # Answer only the first call it matches; later ones go to the next rule --
    # a device that answers `get-state` once, then is gone.
    once: bool = False
    # A placeholder message: a recorded frame carrying it stands for the line an
    # `adb shell log -t TAG MSG` call wrote -- absent from the answer until such
    # a call is in the log, then that call's tag and message. Binary rules only.
    logged: str = ""

    @property
    def binary(self) -> bool:
        """Whether this rule replays recorded ``logcat -B`` records."""
        return isinstance(self.stdout, bytes) or isinstance(self.buffered, bytes)


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

    def _stage(self, text: str | bytes) -> str:
        """Write ``text`` (or recorded bytes) beside the stubs; its path, "" for none."""
        if not text:
            return ""
        self._staged += 1
        path = self.root / f"staged-{self._staged}.txt"
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            # newline="": what is staged is what is replayed, "\\r" and all.
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
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
                    "binary": rule.binary,
                    "writes": [[str(path), self._stage(text)] for path, text in rule.writes],
                    "pull_from": rule.pull_from,
                    "hang": rule.hang,
                    "follows": rule.follows,
                    "once": rule.once,
                    "logged": rule.logged,
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
        # The one thing passed through: the zone. A script prints logd's
        # records in local time (common.logcat.LogRecord.lines), and a spec
        # comparing its output with records it printed itself must share it.
        if "TZ" in os.environ:
            env["TZ"] = os.environ["TZ"]
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
        script: str | Path,
        *argv: str,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = RUN_TIMEOUT_SECONDS,
        interrupt_after: float | None = None,
        **extra_env: str,
    ) -> subprocess.CompletedProcess:
        """Run one skill script with this toolchain and nothing else on PATH.

        ``script`` is a name under scripts/, or the path of a copy elsewhere.
        ``interrupt_after`` sends the script SIGINT (Ctrl-C) once that many
        seconds have passed, for a mode that runs until it is interrupted
        (``log_monitor --follow`` over a device that ``follows``).

        The script runs in a process group of its own. Past ``timeout`` the
        whole group is killed -- the script and every stub it started -- and
        the script reaped, before ``TimeoutExpired`` is raised. ``subprocess.run``
        killed the script alone: a ``hang`` stub it had started slept on for an
        hour after every run that timed out (review of PR #36).
        """
        path = script if isinstance(script, Path) else SCRIPTS / script
        interpreter = ["/bin/bash"] if path.suffix == ".sh" else [sys.executable]
        command = [*interpreter, str(path), *argv]
        with subprocess.Popen(
            command,
            env=self.env(**extra_env),
            cwd=str(cwd or self.tmp),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                if interrupt_after is None:
                    stdout, stderr = process.communicate(
                        stdin if stdin is not None else "", timeout=timeout
                    )
                else:
                    try:
                        stdout, stderr = process.communicate(
                            stdin if stdin is not None else "", timeout=interrupt_after
                        )
                    except subprocess.TimeoutExpired:
                        process.send_signal(signal.SIGINT)
                        stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                # Reaped, and its pipes drained: they close once the group is gone.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.communicate(timeout=REAP_TIMEOUT_SECONDS)
                raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    # --- reading back -------------------------------------------------------

    def calls(self, tool: str | None = None) -> list[list[str]]:
        """argv of every stub invocation so far, optionally for one tool."""
        entries = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [entry[1:] for entry in entries if tool is None or entry[0] == tool]

    def stub_pids(self, tool: str | None = None) -> list[tuple[int, list[str]]]:
        """``(pid, argv)`` of every stub invocation so far, optionally for one tool."""
        path = Path(f"{self.log}.pids")
        if not path.exists():
            return []
        entries = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        return [(entry[0], entry[2:]) for entry in entries if tool is None or entry[1] == tool]

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


# The real Popen, kept before any spec replaces it.
_REAL_POPEN = subprocess.Popen


def answering_the_api_level(fake, level: str):
    """``fake`` as ``subprocess.Popen``, but for the API level a live read asks first.

    `adb shell getprop ro.build.version.sdk` is answered by a real child printing
    ``level`` -- recorded text (getprop_sdk) -- so ``subprocess.run``, which
    ``run_adb`` is built on, reads it as it reads adb; every other command goes
    to ``fake``. For a spec that replaces Popen to stand in for `adb logcat`.
    """

    def popen(cmd, *args, **kwargs):
        if list(cmd[-2:]) == ["getprop", "ro.build.version.sdk"]:
            script = "import sys; sys.stdout.write(sys.argv[1])"
            return _REAL_POPEN([sys.executable, "-c", script, level], *args, **kwargs)
        return fake(cmd, *args, **kwargs)

    return popen


def uid_rules(recorded, package: str, pid: str | None) -> list[Rule]:
    """The package's uid, and -- when ``pid`` is given -- that pid's, as the package's."""
    rules = [Rule(("cmd", "package"), stdout=recorded_package_listing(recorded, package))]
    if pid:
        rules.append(Rule(("shell", "ps"), stdout=recorded_ps_answer(recorded, pid)))
    return rules


# A file read that fails ON THE DEVICE, where the file is named like a device
# state. `adb shell` hands the device command's stderr back as its own, so
# these are the device's words in adb's stream -- the case that read as "the
# device is unauthorized" when the classifier matched keywords anywhere (Codex
# review of #29). Derived by substitution from run_as_cat_missing: only the
# path changes.
COLLIDING_FILE_NAMES = (
    "unauthorized.db",
    "device offline.db",
    "device not found.db",
    "more than one device.db",
    "no devices.db",
)


def recorded_missing_file(recorded, directory: str, name: str) -> str:
    """run_as_cat_missing's stderr (`cat: <path>: No such file or directory`) for ``directory/name``."""
    text = recorded.text("run_as_cat_missing")
    assert "shared_prefs/aes_missing.xml" in text, "the fixture moved; re-derive"
    return text.replace("shared_prefs/aes_missing.xml", f"{directory.rstrip('/')}/{name}")


# --- logd's records: deriving a window a record at a time -----------------------
#
# log_monitor and app_state_capture read `logcat -B` (common.logcat): logd's
# records whole. A spec that needs a window the device did not record -- a
# start deleted, a pid reused, a sentence forged -- derives it from a recorded
# one by substitution, at the record's own granularity: whole records deleted,
# moved or repeated, or one field of one record replaced. A replaced field is
# re-framed with its length recomputed, the way logd frames a record holding
# that text; every byte not substituted is recorded. Read with struct here,
# not with common.logcat, so the reader under test is not also the oracle.

_FRAME_HEADER = struct.Struct("<HHiIIIII")
_FRAME_PRIORITIES = {2: "V", 3: "D", 4: "I", 5: "W", 6: "E", 7: "F", 8: "S"}


@dataclass(frozen=True)
class Frame:
    """One recorded logd record, raw: ``struct logger_entry`` then its payload."""

    raw: bytes

    @property
    def _header_size(self) -> int:
        return struct.unpack_from("<H", self.raw, 2)[0]

    def _parts(self) -> tuple[bytes, bytes, bytes, bytes]:
        """``(priority byte, tag, message, what follows the message's NUL)``."""
        payload = self.raw[self._header_size :]
        tag, _, rest = payload[1:].partition(b"\0")
        message, _, trailer = rest.partition(b"\0")
        return payload[:1], tag, message, trailer

    @property
    def pid(self) -> str:
        return str(struct.unpack_from("<i", self.raw, 4)[0])

    @property
    def uid(self) -> int:
        return struct.unpack_from("<I", self.raw, 24)[0]

    @property
    def priority(self) -> str:
        return _FRAME_PRIORITIES.get(self._parts()[0][0], "?")

    @property
    def tag(self) -> str:
        return self._parts()[1].decode("utf-8")

    @property
    def message(self) -> str:
        return self._parts()[2].decode("utf-8")

    def replace(
        self,
        *,
        pid: str | None = None,
        tag: str | None = None,
        message: str | None = None,
        uid: int | None = None,
    ) -> Frame:
        """This record with its writer's pid or uid, its tag or its message substituted.

        ``uid`` is logd's, from the writing socket's credentials, so a record
        "from another uid" can only be derived this way: no app can write it.
        """
        priority, old_tag, old_message, trailer = self._parts()
        body = (
            priority
            + (old_tag if tag is None else tag.encode("utf-8"))
            + b"\0"
            + (old_message if message is None else message.encode("utf-8"))
            + b"\0"
            + trailer
        )
        header = bytearray(self.raw[: self._header_size])
        struct.pack_into("<H", header, 0, len(body))
        if pid is not None:
            struct.pack_into("<i", header, 4, int(pid))
        if uid is not None:
            struct.pack_into("<I", header, 24, uid)
        return Frame(bytes(header) + body)


def frames(data: bytes) -> list[Frame]:
    """The records of a recorded ``logcat -B`` dump, in order."""
    out: list[Frame] = []
    offset = 0
    while offset < len(data):
        length, header, *_ = _FRAME_HEADER.unpack_from(data, offset)
        out.append(Frame(data[offset : offset + header + length]))
        offset += header + length
    return out


def framed(records) -> bytes:
    """Records back into a ``logcat -B`` byte stream."""
    return b"".join(record.raw for record in records)


def recorded_zone(recorded, name: str) -> str:
    """The zone the device printed ``name``'s text half in (its MANIFEST ``timezone``)."""
    entry = next(e for e in recorded.manifest["fixtures"] if e["name"] == name)
    if "timezone" not in entry:
        pytest.fail(f"{name} records no timezone; re-record it")
    return entry["timezone"]


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
