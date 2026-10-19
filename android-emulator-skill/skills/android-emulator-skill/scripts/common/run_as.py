"""Reading a debuggable app's files with ``adb shell -T run-as`` -- and nothing else.

Why this exists
---------------
``adb exec-out run-as <pkg> cat <file>`` exits 0 whatever the device command
did, and delivers the device's stderr on STDOUT. A missing file comes back as
the bytes ``cat: <file>: No such file or directory``, and a run-as refusal as
its own message -- each indistinguishable, to a caller deciding by exit status,
from a file that holds those words (recorded: ``run_as_cat_missing_exec_out``,
exit 0, stderr empty). ``container.py`` read files that way until PR #22, and
``model_inspector.py`` pulled databases that way after it: a missing database
was written to a temp file and reported as "not a valid SQLite database".

``adb shell -T`` (no pty) returns the device command's exit status and its
stderr apart, and keeps stdout's bytes as exec-out delivers them -- fixture.db,
32768 bytes, cmp-identical over both (recorded: ``run_as_cat_missing``, exit 1,
stdout empty, the reason on stderr). Two scripts read app files now, so the
transport and its guard live here once -- and so do what a run-as refusal is
(:func:`refused`) and how a directory listing is read (:func:`parse_ls_output`).

The client guard
----------------
adb has understood ``shell -T`` since 1.0.36 (platform-tools 24). An older
CLIENT does not refuse it: it forwards ``-T`` to the device shell as command
text and exits 0 after printing the shell's error, and a read deciding by exit
status would take that error for the file. So the client's version is checked
once per process, before the first read (recorded: ``adb_version``).

Text an app chose
-----------------
A device command's stderr names what it failed on, and the app chose those
names: ``cat`` prints the file's, ``ls`` the directory entry's. A name may hold
a newline, a carriage return, or a line of its own -- ``run-as: package not
debuggable``, ``adb: device offline``. Nothing here decides anything from a
line a name could have printed: a refusal is the FIRST line of stderr (a
command that ran names itself first), confirmed by a probe that names no file;
a listing is split on ``\\n`` only, and a name no plain file name carries is
refused and reported, never split into two.
"""

from __future__ import annotations

import functools
import re
from typing import NamedTuple

from . import adb_exec, cli
from .device_utils import build_adb_command, quote_for_device_shell

MIN_ADB_FOR_READS = (1, 0, 36)
_ADB_VERSION_LINE = re.compile(r"^Android Debug Bridge version (\d+)\.(\d+)\.(\d+)\s*$", re.M)
ADB_UPDATE_REMEDY = (
    'Update Android SDK platform-tools (sdkmanager "platform-tools") so `adb version` '
    "reports 1.0.36 or later, and make sure that adb is the one on PATH."
)


def parse_adb_version(banner: str) -> tuple[int, int, int] | None:
    """The X.Y.Z of an ``adb version`` banner's first line, or None when it has none.

    Recorded: adb_version -- ``Android Debug Bridge version 1.0.41``.
    """
    match = _ADB_VERSION_LINE.search(banner)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


@functools.cache
def adb_client_version() -> tuple[int, int, int]:
    """The host adb client's version, asked once per process (``adb version``, bounded).

    Raises:
        cli.Failure: the banner names no version. Not assumed fine: a client
            whose version cannot be read cannot be shown to honour ``-T``.
        AdbError: adb is missing, timed out, or ``adb version`` failed.
    """
    banner = adb_exec.run_adb("version", None, check=True).stdout
    version = parse_adb_version(banner)
    if version is None:
        first = banner.strip().splitlines()[0] if banner.strip() else "(nothing)"
        cli.fail(
            f"cannot tell whether this adb can read app files safely: `adb version` "
            f"answered {first!r}, with no 'Android Debug Bridge version X.Y.Z' line",
            ADB_UPDATE_REMEDY,
        )
    return version


def require_adb_for_reads(version: tuple[int, int, int]) -> None:
    """Refuse, before any file is read, an adb client too old to honour ``shell -T``.

    The version is handed in rather than asked for here, so each script asks
    through its own ``adb_client_version`` name -- the one its tests replace.

    Raises:
        cli.Failure: ``version`` is older than ``MIN_ADB_FOR_READS``.
    """
    if version < MIN_ADB_FOR_READS:
        shown = ".".join(map(str, version))
        cli.fail(
            f"adb {shown} is older than 1.0.36 and cannot read app files safely",
            ADB_UPDATE_REMEDY,
        )


# === WHAT A RUN-AS REFUSAL IS ================================================

# The whole of each refusal the device printed, before the package it names:
# `run-as: <reason>: <package>` (recorded: run_as_id_not_debuggable,
# run_as_id_not_an_application, run_as_id_unknown_package, and the `ls` pair
# run_as_not_an_application_stderr, run_as_unknown_package_stderr). Both
# inspectors matched a list of substrings instead, written from memory: it held
# "is not an application", with an "is" the platform never prints, so a system
# package's refusal fell through to a generic failure; and "permission denied",
# which `ls: <dir>: Permission denied` matched (review of #33). A wording that
# is not one of these -- an older release's -- reads as the command's failure.
RUN_AS_REFUSALS = (
    "package not debuggable",
    "package not an application",
    "unknown package",
)

# What `id` prints first once run-as has switched to the app (recorded:
# run_as_id, `uid=10205(u0_a205) gid=...`).
_ID_UID = re.compile(r"uid=(\d+)\(")
# The first app uid (Android's AID_APP_START). run-as refuses a package below
# it ("not an application"), so an `id` run as anything lower ran as something
# other than the app.
_FIRST_APP_UID = 10_000


def run_as_refused(stderr: str, returncode: int, package: str) -> bool:
    """True when stderr's first line is run-as refusing ``package``, at a non-zero exit.

    run-as refuses before it runs any command, so its refusal is all of stderr
    -- one line, stdout empty, exit 1 (run_as_*_stderr, run_as_id_*). A command
    that DID run names itself first (``cat: ``, ``ls: ``), so a later line is
    the command's however it reads: a file name holding ``\\n`` and
    ``run-as: package not debuggable`` printed exactly such a line after cat's
    ``No such file`` -- and, with ``str.splitlines``, a ``\\r`` did too -- and
    both inspectors answered a debuggable app's missing file with "install a
    debug build" (final review of v0.8.0). Lines are split on ``\\n`` only.

    Before that (review of #33) both scripts matched the markers anywhere in
    stdout and stderr, so ``ls: <dir>: Permission denied`` -- the filesystem
    refusing a directory of an app run-as had entered (recorded:
    run_as_ls_shared_prefs_unreadable) -- was a run-as denial too.

    Args:
        stderr: The command's stderr, decoded. Never stdout, which holds what
            the app wrote.
        returncode: Its exit status; a refusal is non-zero.
        package: The package the command named. The first line must be the
            WHOLE recorded refusal for it, ``run-as: <reason>: <package>``
            (``RUN_AS_REFUSALS``) -- one package's refusal is not another's.
    """
    if returncode == 0:
        return False
    first = stderr.split("\n", 1)[0]
    return first in {f"run-as: {reason}: {package}" for reason in RUN_AS_REFUSALS}


def run_as_works(serial: str | None, package: str, *, timeout: float) -> bool:
    """Whether ``run-as <package> id`` runs as the app: exit 0, ``uid=<app uid>(`` first.

    The independent half of :func:`refused`. ``id`` takes no argument, so
    nothing in its answer is a name an app chose, and run-as answers it the way
    it answers any command it refuses -- or runs it as the app (recorded:
    run_as_id for the fixture app; run_as_id_not_an_application,
    run_as_id_not_debuggable, run_as_id_unknown_package). One bounded call.

    Its device errors are not put to ``adb get-state``: that confirmation is
    for stderr that can carry a name an app chose, and this carries none. With
    it, a device lost during the probe and back for get-state dropped the
    error, and the refusal it was confirming became "install a debug build" at
    exit 1 (Codex review of #38).

    Raises:
        AdbError: adb itself failed -- the device went, or the call timed out.
            Not a verdict about the app either way, so not taken for one.
    """
    completed = adb_exec.run_argv(
        read_command(serial, package, ["id"]),
        serial=serial,
        timeout=timeout,
        confirm_device_errors=False,
    )
    if completed.returncode != 0:
        return False
    match = _ID_UID.match(completed.stdout.decode("utf-8", errors="replace"))
    return match is not None and int(match.group(1)) >= _FIRST_APP_UID


def refused(
    serial: str | None,
    package: str,
    stderr: str,
    returncode: int,
    *,
    timeout: float,
) -> bool:
    """True when run-as refused ``package``: its refusal heads stderr, and run-as does not work.

    The one rule both inspectors give the debug-build remedy by: the refusal
    read off the failed command (:func:`run_as_refused`), then one bounded
    probe (:func:`run_as_works`). The probe can only take the verdict back,
    never make one: a first line that reads as a refusal while run-as runs
    ``id`` as the app is the command's own failure. Nothing is probed when
    stderr holds no refusal.

    Args:
        serial: The device the command ran on.
        package: The package it named.
        stderr: Its stderr, decoded.
        returncode: Its exit status.
        timeout: Bound on the probe.
    """
    if not run_as_refused(stderr, returncode, package):
        return False
    return not run_as_works(serial, package, timeout=timeout)


def path_failure_remedy(package: str, path: str) -> str:
    """The remedy for a device command that failed on ``path`` once run-as had let it run.

    Not a run-as denial (see :func:`refused`): the app is installed and
    debuggable, and the device refused the path itself -- it is missing, or its
    mode does not let the app read it.
    """
    return (
        f"run-as works for this app; the device refused {path} itself. Check that it "
        f"exists and that the app may read it: `adb shell run-as {package} ls -ld {path}` "
        "(a directory needs its owner's r bit to be listed, a file its r bit to be read)."
    )


def read_command(serial: str | None, package: str, argv: list[str]) -> list[str]:
    """The argv of ``adb shell -T run-as <package> <argv...>``, quoted for the device shell.

    Run it with ``adb_exec.run_argv`` (bytes, ``confirm_device_errors=True``):
    its status and stderr are the read's verdict, and adb's own failures arrive
    typed.
    """
    quoted = [quote_for_device_shell(arg) for arg in argv]
    return build_adb_command(
        "shell", serial, "-T", "run-as", quote_for_device_shell(package), *quoted
    )


# === READING A LISTING =======================================================
#
# Both inspectors list an app's directory with `run-as <pkg> ls -la`, read as
# bytes, and parse it here. model_inspector listed databases/ with `ls -1` in
# text mode and `splitlines()`: the transport turned a CR in a name into a line
# break, and `x\ry.db` became two databases, `x` and `y.db` (final review of
# v0.8.0). container.py's parser had already been made to refuse such a name;
# it moved here so both read a listing one way.

# toybox `ls -l` -- what every caller's `run-as <pkg> ls -la` runs -- prints
# mode, links, owner, group and size separated by runs of spaces, the timestamp
# as two columns, `%F %H:%M` (recorded: run_as_ls_symlink_space), then ONE space
# and the name exactly as printed: it may hold spaces, start or end with one, or
# look like a date.
_LS_ENTRY = re.compile(
    r"(?P<mode>\S+) +(?P<links>\S+) +(?P<owner>\S+) +(?P<group>\S+) +(?P<size>\S+) +"
    r"(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2}) (?P<clock>[0-9]{2}:[0-9]{2}) (?P<name>.+)"
)

# A name is chosen by the app on the device, so it is untrusted input to the
# host: container.py's export() turns it into a host path. None of these belongs
# in a single directory entry -- `/` and NUL cannot occur in one at all, so
# seeing them means the line is not what it claims; `\` is a separator on a
# Windows host; a newline is how a crafted name forges a second `ls` line.
_NAME_FORBIDDEN = frozenset("/\\\0\n\r")
# A symlink's target is a device path, so `/` is expected there; a CR or NUL is
# not, and the target is printed to the caller.
_TARGET_FORBIDDEN = frozenset("\0\n\r")


class LsListing(NamedTuple):
    """What an ``ls -la`` listing held: the entries read, and the ones refused.

    ``rejected`` holds each refused name -- or the whole line, when its columns
    could not be placed -- as ``repr()``, so a control character in it is
    shown escaped rather than reaching a terminal or a log raw. Every caller
    reports them: dropping them silently answered "nothing there" for
    "refused to read it".
    """

    entries: list[dict]
    rejected: list[str]


def parse_ls_output(output: bytes | str) -> LsListing:
    r"""Parse ``ls -la`` output into structured entries.

        Reads toybox's layout by position -- every caller runs ``run-as <pkg> ls
        -la`` on the device, and the device's ``ls`` is toybox (recorded:
        ``run_as_ls_symlink_space``):

            drwx------ 4 u0_a123 u0_a123 4096 2024-01-02 03:04 shared_prefs
            -rw------- 1 u0_a123 u0_a123  220 2024-01-02 03:04 prefs.xml
            lrwxrwxrwx 1 root    root       11 2024-01-02 03:04 lib -> /data/app

        The name is everything after the two timestamp columns, so a file named
        ``2026-10-16``, ``03:04`` or ``May`` keeps its name. (Skipping every
        date-looking token instead -- and, before that, coreutils' month names and
        bare years -- made such names vanish from the listing.)

        Every line is an entry, refused, or one of three skips: a blank line, the
        ``total N`` line, and the directory's own ``.`` / ``..``. Refused, and
        returned in ``rejected`` (never dropped silently):

        - a line that is not UTF-8 -- decoding it with replacement would hand on
          a name that is not the file's, and the read of it would fail;
        - a line that is not that layout: an unknown mode (``??????????``), a
          device node, whose size is ``major, minor`` (measured: ``1,   3``) and
          takes two columns, or any other text;
        - a name that is not a plain file name (see ``_NAME_FORBIDDEN``), or a
          symlink target holding a CR or NUL.

        The names come from the app, and container.py's export makes host paths
        of them.

        Lines are split on ``\n`` only -- what ``ls`` separates entries with --
        not ``str.splitlines``, which also breaks on ``\r``, ``\x1c``-``\x1e``,
        ``\x85`` and ``
    ``/``
    ``, any of which a name may hold and
        would then forge a line without a newline. (The transport keeps a CR a CR
        for the same reason: both callers read the listing as bytes.) A name
        holding a real newline still can: ``ls -1b`` or ``find -print0`` would
        make names unambiguous, and is not done here.

        Args:
            output: Raw ``ls -la`` stdout: bytes from the device, or text already
                decoded (the recorded fixtures).

        Returns:
            :class:`LsListing`: ``entries`` as dicts {name, mode, kind, size_bytes,
            owner, group, symlink_target}, and the ``rejected`` names.
    """
    raw = output.encode("utf-8") if isinstance(output, str) else output
    entries: list[dict] = []
    rejected: list[str] = []
    for raw_line in raw.split(b"\n"):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            rejected.append(repr(raw_line))
            continue
        if not line.strip() or line.startswith("total "):
            continue
        match = _LS_ENTRY.fullmatch(line)
        if match is None or not _looks_like_mode(match["mode"]):
            rejected.append(repr(line))
            continue
        kind = _kind_from_mode(match["mode"])
        name, symlink_target = _extract_name(match["name"], kind)
        if name in (".", ".."):
            continue
        if not is_plain_file_name(name):
            rejected.append(repr(name))
            continue
        if symlink_target is not None and _TARGET_FORBIDDEN.intersection(symlink_target):
            rejected.append(repr(match["name"]))
            continue

        entries.append(
            {
                "name": name,
                "mode": match["mode"],
                "kind": kind,
                "size_bytes": int(match["size"]) if match["size"].isdigit() else 0,
                "owner": match["owner"],
                "group": match["group"],
                "symlink_target": symlink_target,
            }
        )
    return LsListing(entries, rejected)


def rejections(listing: LsListing) -> dict:
    """The refused entries of a listing, as a result carries them (see LsListing)."""
    return {"rejected": len(listing.rejected), "rejected_entries": listing.rejected}


def is_plain_file_name(name: str) -> bool:
    """True if ``name`` is one directory entry that is safe to join to a host path."""
    return name not in ("", ".", "..") and not _NAME_FORBIDDEN.intersection(name)


def _looks_like_mode(token: str) -> bool:
    """True if a token looks like a 10-char ls permission string."""
    if len(token) < 10:
        return False
    return token[0] in "-dlcbsp" and all(c in "rwxsStTl-" for c in token[1:10])


def _kind_from_mode(mode: str) -> str:
    """Map the leading mode char to a kind label."""
    head = mode[0]
    if head == "d":
        return "dir"
    if head == "l":
        return "symlink"
    return "file"


def _extract_name(rest: str, kind: str) -> tuple[str, str | None]:
    """Split the name column into the name and, for a symlink, its target.

    Args:
        rest: Everything after the time column. toybox prints a name bare
            (unquoted, unescaped) and a symlink as ``name -> target``.
        kind: Entry kind (symlinks split on " -> ").

    Returns:
        (name, symlink_target) — target is None for non-symlinks.
    """
    if kind == "symlink" and " -> " in rest:
        # Not stripped: a CR or space at either end belongs to the name.
        name, _, symlink_target = rest.partition(" -> ")
        return name, symlink_target
    return rest, None
