#!/usr/bin/env python3
"""system_server's sentences about app processes, read one way by every reader.

Two readers need to know which process a pid is: ``log_monitor --app`` and
``app_state_capture`` (one app's log window, through ``common.logcat``) and
the ANR session worker (which app a jank record's pid belongs to). They used
to read the evidence two ways, and the ANR worker's way was the looser one
(final review of v0.8.0, B1, and PR #34's review). This module is the one
reading both call now, and it reads logd's RECORDS (``common.logcat
.LogRecord``, from ``logcat -B``), never text: a tag is the app's to choose
and logcat prints it verbatim, so in text one record could print a line
headed with system_server's pid (recorded: logcat_tag_injection).

The evidence, all recorded in logcat_app_restart_records (an app crashed, was
relaunched, force-stopped and relaunched)::

    ActivityManager: Start proc 21149:<package>/u0a205 for next-top-activity {...}
    ActivityManager: Process <package> (pid 21149) has died: fg  TOP
    ActivityManager: Killing 21205:<package>/u0a205 (adj 0): stop <package> ...

What does NOT count, and why (security reviews of #27 and v0.8.0):

- the tag alone: any app may log under ActivityManager -- a sentence counts
  only when system_server WROTE the record (its pid, which logd takes from the
  socket's credentials, is system_server's pid, asked of the device; unknown,
  nothing counts);
- a sentence anywhere but the very start of the record's message:
  system_server's messages carry app-controlled text further on (a
  component, an intent), including whole further lines;
- the process name: an app may name its own process after another package
  (``android:process``), and system_server then really logs ``Start proc
  N:<that package>/<the impostor's uid>``. Start proc and Killing carry the
  uid; the process must be exactly a package, running under one of its uids
  (asked of the device; unknown, it is not the package's). ``has died``
  carries none, so it can only end a life.

This module reads records; asking the device is the caller's (the lookups are
passed in, and asked at most once, only when a record needs them).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.logcat import LogRecord

# The tag whose records name an app's processes.
PROCESS_TAG = "ActivityManager"

# AOSP's UserHandle.formatUid, as system_server prints the uid after a process
# name: below FIRST_APPLICATION_UID the bare number, else u<user> followed by
# a<n> (an app; recorded u0a205), i<n> (isolated), ai<n> (app-zygote
# isolated) or s<appid> (a shared system id).
_PER_USER_RANGE = 100_000
_UID_BASES = {"a": 10_000, "ai": 90_000, "i": 99_000, "s": 0}
_FORMATTED_UID = re.compile(r"u(?P<user>\d+)(?P<kind>ai|a|i|s)(?P<n>\d+)")

# What a sentence says about the pid it names.
STARTED = "started"
KILLED = "killed"
DIED = "died"

# The three sentences, as the whole opening of an ActivityManager record's
# message, for ANY process name; whose process it is comes after.
_SENTENCES = (
    (STARTED, re.compile(r"Start proc (?P<pid>\d+):(?P<name>[^/\s]+)/(?P<uid>\S+) ")),
    (KILLED, re.compile(r"Killing (?P<pid>\d+):(?P<name>[^/\s]+)/(?P<uid>\S+) \(adj -?\d+\): ")),
    (DIED, re.compile(r"Process (?P<name>\S+) \(pid (?P<pid>\d+)\) has died\b")),
)


def uid_from_formatted(text: str) -> int | None:
    """The numeric uid ``UserHandle.formatUid`` printed as ``text``.

    ``u0a205`` -> 10205, ``u10a205`` -> 1010205, ``1000`` -> 1000; None for
    anything else.
    """
    if text.isdigit():
        return int(text)
    match = _FORMATTED_UID.fullmatch(text)
    if match is None:
        return None
    return int(match["user"]) * _PER_USER_RANGE + _UID_BASES[match["kind"]] + int(match["n"])


def uid_set(uids: int | Iterable[int] | None) -> frozenset[int]:
    """One uid, several, or none, as a set."""
    if uids is None:
        return frozenset()
    return frozenset([uids]) if isinstance(uids, int) else frozenset(uids)


class Lazy:
    """A value, or a function giving it, asked for at most once."""

    def __init__(self, value) -> None:
        self._value = value
        self._asked = not callable(value)

    def __call__(self):
        if not self._asked:
            self._asked = True
            self._value = self._value()
        return self._value


@dataclass(frozen=True)
class Sentence:
    """One authenticated sentence: system_server says process ``pid`` started or ended.

    ``uid`` is the uid it names (Start proc, Killing), or None (has died, or a
    uid in a form not read).
    """

    what: str
    pid: str
    name: str
    uid: int | None


class LifecycleReader:
    """Reads records: which pid wrote each, and system_server's sentence in it.

    Args:
        system_server_pid: system_server's pid, or a function returning it
            (None when the device cannot say); asked once, and only when a
            record opens with a sentence.
        package_uids: ``package -> its uids`` (an int, several, or None when
            the device cannot say); asked once per package, only by
            :meth:`is_package`.
    """

    def __init__(
        self,
        system_server_pid: str | Callable[[], str | None] | None,
        package_uids: Callable[[str], int | Iterable[int] | None],
    ) -> None:
        self.system_server = Lazy(system_server_pid)
        self._package_uids = package_uids
        self._uids: dict[str, frozenset[int]] = {}

    def read(self, record: LogRecord) -> tuple[str, Sentence | None]:
        """``(pid that wrote the record, the sentence it holds or None)``."""
        writer = str(record.pid)
        if record.tag != PROCESS_TAG:
            return writer, None
        return writer, self._sentence(writer, record.message)

    def is_package(self, sentence: Sentence) -> bool:
        """Whether the sentence's process is exactly a package, under one of its uids."""
        if sentence.uid is None:
            return False
        if sentence.name not in self._uids:
            self._uids[sentence.name] = uid_set(self._package_uids(sentence.name))
        return sentence.uid in self._uids[sentence.name]

    def _sentence(self, writer: str, message: str) -> Sentence | None:
        for what, pattern in _SENTENCES:
            found = pattern.match(message)
            if found is None:
                continue
            if writer != self.system_server():
                return None
            uid = found.groupdict().get("uid")
            return Sentence(
                what=what,
                pid=found["pid"],
                name=found["name"],
                uid=uid_from_formatted(uid) if uid else None,
            )
        return None
