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
(:func:`refused`) and how a directory is listed (:func:`listing_command`,
:func:`parse_listing`).

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
a listing is records of NUL-terminated fields, the name last -- NUL is the one
byte no name can hold -- and a name no plain file name carries is refused and
reported, never split into two.
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
        "(a directory needs its owner's r bit to be listed and its x bit for what is in it "
        "to be reached; a file needs its r bit to be read)."
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
# Both inspectors list an app's directory with `run-as <pkg>` running
# listing_command(), read as bytes, and parse it here. It was `ls -la`, split
# on `\n`: a file name may hold a newline, and `fixture.db` + LF + a whole
# `ls -la` line for `ghost.db` listed a database the app does not have, at exit
# 0 with nothing refused (final review of v0.8.0, REQUIRED 4; recorded:
# run_as_ls_names shows the forged line, run_as_find_names the one name). Before
# that, model_inspector's `ls -1` in text mode made a CR a line break too.
#
# toybox find prints each field the format names and then the NUL after it, so
# a record is a FIXED number of NUL-terminated fields with the name last. NUL is
# the one byte no path component can hold, so splitting on it and taking six
# fields at a time is unambiguous whatever a name or a symlink's target holds --
# LF, CR, a tab, spaces, or the text of another record. Measured on
# emulator-5554, toybox 0.8.11-android (API 35); recorded: run_as_find_*.
#
#   %M  the type and mode as ls prints them, ten characters (`-rw-rw----`)
#   %u  the owner's name (`u0_a205`)
#   %g  the group's name -- not always the owner's (`u0_a205_cache` on cache/)
#   %s  the size in bytes; a symlink's is its target's length (6 for `e f.db`)
#   %l  a symlink's target, and empty for anything that is not a symlink
#   %P  the path minus the directory named: the entry's name, and EMPTY for the
#       directory itself, which find prints first
#
# `-maxdepth 1` stops find below the directory's own entries. There is no
# `-mindepth 1`: the directory's own record is how a listing shows it listed a
# DIRECTORY (find on a file prints the file's record alone, at exit 0 --
# run_as_find_not_a_directory) and that find printed at all, so nothing is ever
# read as an empty directory by default. `-H` follows the named path when it is a
# symlink, as `ls <dir>` does; without it find prints the link's record alone
# (measured: a link to a directory holding `inner` listed only the link).
#
# A toybox without `-printf`, or without one of these escapes, is not measured:
# this one answers an option it does not know with `find: bad arg '<format>'` and
# an escape with `find: bad -printf %Q`, exit 1 (run_as_find_unknown_primary,
# run_as_find_unknown_escape). Output that is not whole records is refused
# (ListingError), never read some other way and never handed to `ls`.
#
# Two limits, both the transport's rather than the parse's:
#
# - A listing is only as complete as its exit status says. The framing has no
#   end marker, so an answer cut after any COMPLETE record -- even right after
#   the directory's own -- parses as a directory with fewer entries. A cut that
#   adb or find reports (a non-zero exit, a device error) is refused before
#   anything is parsed; a cut at exit 0 cannot be seen here.
# - `-H` follows the named path's own symlink to any directory the app's uid
#   may search, inside its data dir or not (SELinux permitting): `--ls files/x`,
#   where the app planted `x` -> /some/dir, lists /some/dir. A listing reads
#   names and modes, never contents. container.py --export's reads of the files
#   a listing names follow those files' symlinks too; that predates this
#   transport.

LISTING_FORMAT = "%M\\0%u\\0%g\\0%s\\0%l\\0%P\\0"
LISTING_FIELDS = 6

# A name is chosen by the app on the device, so it is untrusted input to the
# host: container.py's export() turns it into a host path. None of these belongs
# in a single directory entry -- `/` and NUL cannot occur in one at all; `\` is a
# separator on a Windows host; a newline or a CR is printed raw to a terminal,
# and was how a crafted name forged a second `ls` line.
_NAME_FORBIDDEN = frozenset("/\\\0\n\r")
# A symlink's target is a device path, so `/` is expected there; a newline, CR
# or NUL is not, and the target is printed to the caller.
_TARGET_FORBIDDEN = frozenset("\0\n\r")


def listing_command(directory: str) -> list[str]:
    """The device argv that lists ``directory``: toybox find, one NUL-framed record per entry.

    Run it as ``run-as <package> <argv...>``, each argument quoted for the
    device shell, and hand its stdout's bytes to :func:`parse_listing`. The
    format string's ``\\0`` is find's own escape, so it reaches find intact
    inside single quotes.
    """
    return ["find", "-H", directory, "-maxdepth", "1", "-printf", LISTING_FORMAT]


class LsListing(NamedTuple):
    """What a directory listing held: the entries read, and the ones refused.

    ``rejected`` holds each refused name -- or the name and its target, for a
    symlink whose target was refused -- as ``repr()``, so a control character
    in it is shown escaped rather than reaching a terminal or a log raw. Every
    caller reports them: dropping them silently answered "nothing there" for
    "refused to read it". (The name is from when this held an ``ls -la``; the
    shape is the one both inspectors return.)
    """

    entries: list[dict]
    rejected: list[str]


class ListingError(ValueError):
    """A listing's stdout that is not what :func:`listing_command` prints, refused whole.

    Nothing in it is read: output that is not whole records of
    ``LISTING_FIELDS`` NUL-terminated fields, headed by the directory's own,
    cannot say which bytes are a name. ``not_a_directory`` marks the one case
    that is the path's rather than the tool's: find listed a file, or something
    else that is not a directory.
    """

    def __init__(self, message: str, *, not_a_directory: bool = False) -> None:
        super().__init__(message)
        self.not_a_directory = not_a_directory


def parse_listing(output: bytes) -> LsListing:
    r"""Parse :func:`listing_command`'s stdout into structured entries.

    Records of ``LISTING_FIELDS`` NUL-terminated fields -- mode, owner, group,
    size, symlink target, name (see the field notes above) -- the first the
    directory's own, with an empty name. Recorded: run_as_find_databases,
    run_as_find_data_dir, run_as_find_names.

    Refused whole (:class:`ListingError`), with nothing read from it: output
    that does not end in NUL (empty, or cut short), a field count that is not a
    multiple of six, a first record that is not the directory's own (an
    empty name and a mode), and -- ``not_a_directory`` -- a first record whose
    mode is not a directory's.

    Every other record is an entry or refused, and refused ones are returned in
    ``rejected`` (never dropped silently):

    - a name that is not UTF-8 -- decoded with replacement it would be a name
      no file has, and the read of it would fail;
    - a name that is not a plain file name (``_NAME_FORBIDDEN``: ``/``, ``\``,
      NUL, LF, CR; or ``.``/``..``, which find never prints for an entry);
    - a mode that is not ten ls characters, a size that is not digits, or a
      target on an entry that is not a symlink -- none of which find prints;
    - a symlink whose target is empty, not UTF-8, or holds a LF, CR or NUL.

    A newline in a name no longer forges anything: it is inside one field, and
    the name holding it is refused and reported as itself. What the framing
    cannot show is a cut after a complete record: such an answer reads as a
    shorter directory, so its completeness is the exit status's, which both
    callers check before they parse.

    Entries are sorted by name, as ``ls`` sorted them; find prints directory
    order.

    Args:
        output: Raw stdout of :func:`listing_command`, as bytes from the device.

    Returns:
        :class:`LsListing`: ``entries`` as dicts {name, mode, kind, size_bytes,
        owner, group, symlink_target}, and the ``rejected`` names.

    Raises:
        ListingError: the output is not a listing of a directory.
    """
    if not output.endswith(b"\0"):
        raise ListingError(
            f"find's answer is not NUL-terminated records: {_shown(output)}, "
            f"{len(output)} bytes"
        )
    fields = output[:-1].split(b"\0")
    if len(fields) % LISTING_FIELDS:
        raise ListingError(
            f"find's answer holds {len(fields)} fields, not whole records of {LISTING_FIELDS}"
        )
    records = [fields[at : at + LISTING_FIELDS] for at in range(0, len(fields), LISTING_FIELDS)]
    own, *children = records
    mode = own[0].decode("utf-8", errors="replace")
    if own[-1] != b"" or not _looks_like_mode(mode):
        shown = _shown(b"\0".join(own))
        raise ListingError(
            "find's answer does not start with the directory's own record (a mode and an "
            f"empty name): {shown}"
        )
    if mode[0] != "d":
        raise ListingError(
            f"it is not a directory: find listed it with the mode {mode}", not_a_directory=True
        )
    entries: list[dict] = []
    rejected: list[str] = []
    for record in sorted(children, key=lambda fields: fields[-1]):
        entry = _entry(record)
        if isinstance(entry, str):
            rejected.append(entry)
        else:
            entries.append(entry)
    return LsListing(entries, rejected)


def _entry(record: list[bytes]) -> dict | str:
    """One entry's record as an entry dict, or the ``repr`` it is refused as."""
    raw_name = record[-1]
    try:
        name = raw_name.decode("utf-8")
    except UnicodeDecodeError:
        return repr(raw_name)
    if not is_plain_file_name(name):
        return repr(name)
    try:
        target = record[4].decode("utf-8")
    except UnicodeDecodeError:
        return repr(raw_name + b" -> " + record[4])
    try:
        mode, owner, group, size = (field.decode("utf-8") for field in record[:4])
    except UnicodeDecodeError:
        return repr(name)
    if not _looks_like_mode(mode) or not (size.isascii() and size.isdigit()):
        return repr(name)
    kind = _kind_from_mode(mode)
    if kind == "symlink":
        if not target or _TARGET_FORBIDDEN.intersection(target):
            return repr(f"{name} -> {target}")
    elif target:
        return repr(name)
    return {
        "name": name,
        "mode": mode,
        "kind": kind,
        "size_bytes": int(size),
        "owner": owner,
        "group": group,
        "symlink_target": target if kind == "symlink" else None,
    }


def _shown(output: bytes) -> str:
    """The head of an answer that is not a listing, escaped, for an error message."""
    return repr(output[:80]) + ("..." if len(output) > 80 else "")


def listing_failure(package: str, directory: str, error: str, stderr: str) -> dict:
    """The result for a listing that find ran and that exited non-zero, run-as having let it run.

    Not a run-as denial (see :func:`refused`). Which remedy it gets is read off
    the FIRST line of stderr, find's own, which is one of three:

    - the directory itself: ``find: <dir>: Permission denied``, unquoted
      (run_as_find_databases_unreadable), or ``find: '<dir>': No such file or
      directory``, quoted (run_as_find_shared_prefs_absent) -- the path's
      failure, with :func:`path_failure_remedy`;
    - an entry in it: ``find: '<dir>/<entry>': Permission denied`` -- the
      directory could be read but not searched, so find could not stat what it
      holds (`chmod u-x`: run_as_find_databases_unsearchable, whose first line
      names ``.``). The listing failed AT that entry, and the remedy says so,
      naming it escaped (the app chose it) and the directory's r and x bits,
      rather than claiming the directory itself was refused;
    - neither: find refusing the command itself (run_as_find_unknown_primary,
      run_as_find_unknown_escape) -- this device's find cannot list this way,
      and the path is not the problem.

    An entry's name is printed after the directory's, so it cannot change which
    of the three a line is; a newline in it only ends the line early, and the
    entry named is the part before it.

    Args:
        package: The package run-as ran as.
        directory: The directory :func:`listing_command` named.
        error: The caller's message for the failure (it carries stderr).
        stderr: The listing's stderr, decoded.
    """
    first = stderr.split("\n", 1)[0]
    if first.startswith((f"find: {directory}: ", f"find: '{directory}': ")):
        remedy = path_failure_remedy(package, directory)
    elif first.startswith((f"find: {directory}/", f"find: '{directory}/")):
        remedy = _entry_failure_remedy(package, directory, _failed_entry(first, directory))
    else:
        remedy = _listing_tool_remedy(package, directory)
    return {"error": error, "path": directory, "remedy": remedy}


def _failed_entry(first: str, directory: str) -> str:
    """The entry find's first stderr line names below ``directory``, its reason dropped.

    find quotes an entry's path (`find: '<dir>/<entry>': <reason>`), so the
    reason follows the LAST `': ` -- a name may hold `': ` itself, the reasons
    find prints do not. Without that separator (an unquoted line, or one a
    newline in the name cut short) the rest of the line is the entry.
    """
    quoted = first.startswith(f"find: '{directory}/")
    rest = first.removeprefix(f"find: '{directory}/" if quoted else f"find: {directory}/")
    entry, separator, _reason = rest.rpartition("': " if quoted else ": ")
    return entry if separator else rest


def _entry_failure_remedy(package: str, directory: str, entry: str) -> str:
    """Listing ``directory`` failed at ``entry``: say so, and which bits a listing needs."""
    return (
        f"run-as works for this app; listing {directory} failed at the entry {entry!r}, "
        "which find could not read. A directory needs its owner's r bit to be listed and "
        "its x bit for its entries to be reached: check it with "
        f"`adb shell run-as {package} ls -ld {directory}`, and its entries with "
        f"`adb shell run-as {package} ls -lab {directory}`."
    )


def unreadable_listing(package: str, directory: str, exc: ListingError) -> dict:
    """The result for a listing whose stdout :func:`parse_listing` refused."""
    if exc.not_a_directory:
        remedy = (
            f"Name a directory to list. `adb shell run-as {package} ls -ld {directory}` "
            f"shows what {directory} is."
        )
    else:
        remedy = _listing_tool_remedy(package, directory)
    return {"error": f"cannot list {directory}: {exc}", "path": directory, "remedy": remedy}


def _listing_tool_remedy(package: str, directory: str) -> str:
    """Nothing was listed: this device's find does not print the records the listing reads."""
    return (
        "Nothing was listed. These scripts list a directory with toybox find's -H, "
        "-maxdepth and -printf (%M %u %g %s %l %P), measured on toybox 0.8.11 "
        "(Android 15); this device's find did not answer that way. "
        f"`adb shell run-as {package} ls -lab {directory}` shows the directory, "
        "with its names escaped."
    )


def rejections(listing: LsListing) -> dict:
    """The refused entries of a listing, as a result carries them (see LsListing)."""
    return {"rejected": len(listing.rejected), "rejected_entries": listing.rejected}


def is_plain_file_name(name: str) -> bool:
    """True if ``name`` is one directory entry that is safe to join to a host path."""
    return name not in ("", ".", "..") and not _NAME_FORBIDDEN.intersection(name)


def _looks_like_mode(token: str) -> bool:
    """True if a token is a 10-character ls permission string, as find's %M prints one."""
    if len(token) != 10:
        return False
    return token[0] in "-dlcbsp" and all(c in "rwxsStTl-" for c in token[1:])


def _kind_from_mode(mode: str) -> str:
    """Map the leading mode char to a kind label."""
    head = mode[0]
    if head == "d":
        return "dir"
    if head == "l":
        return "symlink"
    return "file"
