#!/usr/bin/env python3
"""Batches that report per item, in the one failure schema.

Why this exists
---------------
Four scripts run one operation per item -- per operation in ``status_bar`` and
``appearance``, per permission in ``privacy_manager``'s grant/revoke/reset, per
AVD or emulator in the ``emulator_*`` ``--all`` modes -- and each printed its
own summary (``{"results": [...]}``, ``{"action", "results"}``, counts and
nothing else) at exit 1, outside the shape every other failure has. An agent
had to know which script it ran to know where the failure was written.

The shape
---------
One object. ``success`` is false when ANY item failed; then ``error`` and
``remedy`` are there as on every refusal (common.cli), followed by the batch's
own keys and ``items``. Each item keeps its own fields (``operation``,
``permission``, ``avd``, ``message``, ...) and adds ``success``, ``error`` and
``remedy`` -- the last two null when it succeeded. Exit 0 only when every item
succeeded, 1 when one failed. A batch whose every item failed is still that
one object: an item's typed failure is caught here, recorded, and the batch
goes on.

A device error
--------------
In a batch against ONE device (``one_device=True``), an item that meets an
``adb_exec.DeviceError`` stops the batch: going on would only repeat it. That
item is reported with the device error, every item after it as not attempted,
and the batch exits with the device error's status -- 3 when the device is
unavailable (not found, offline, unauthorized), 1 for several devices and no
``--serial``. A batch over several emulators (the ``emulator_*`` ``--all``
modes) acts on a different device per item, so there a device error is that
item's failure and the batch goes on.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from . import adb_exec, cli

# What one item does: ``(success, message)``, the shape the scripts' operations
# already return.
Step = Callable[[], tuple[bool, str]]

NOT_ATTEMPTED = "not attempted: the device became unavailable at {noun} {label}"


def item(
    fields: Mapping[str, object],
    success: bool,
    message: str,
    *,
    remedy: str,
) -> dict:
    """One item: its own fields, then ``success``, ``message``, ``error``, ``remedy``.

    Args:
        fields: What names the item (``{"operation": "battery"}``) and any other
            field it already reported.
        success: Whether the item did what it was asked.
        message: What the item said, success or not.
        remedy: What to do when it failed; dropped (null) when it succeeded.
    """
    return {
        **fields,
        "success": success,
        "message": message,
        "error": None if success else message,
        "remedy": None if success else remedy,
    }


@dataclass
class Batch:
    """A batch's items and, when one stopped it, the device error that did.

    Attributes:
        noun: What an item is, for the top-level error ("operation", "AVD").
        key: The item field that names an item ("operation", "avd").
        items: The items, in order.
        stopped: The device error that ended a one-device batch, or None.
        stopped_at: The index of the item that met it, or None. Recorded when
            the error is caught: an earlier item may have failed on its own,
            so "the first failed item" is not where the batch stopped.
    """

    noun: str
    key: str
    items: list[dict] = field(default_factory=list)
    stopped: BaseException | None = None
    stopped_at: int | None = None

    @property
    def failed(self) -> int:
        """How many items failed."""
        return sum(1 for entry in self.items if not entry["success"])

    @property
    def succeeded(self) -> int:
        """How many items succeeded."""
        return len(self.items) - self.failed

    def run(
        self,
        steps: Iterable[tuple[Mapping[str, object], Step]],
        *,
        remedy: str,
        one_device: bool = True,
    ) -> Batch:
        """Run each step in order, recording one item per step.

        Args:
            steps: ``(fields, step)`` pairs; ``step()`` returns ``(success, message)``.
            remedy: The remedy for an item whose step returned ``(False, ...)``.
            one_device: Every step targets the same device, so a device error
                stops the batch (see the module docstring).

        Returns:
            This batch.
        """
        steps = list(steps)
        for index, (fields, step) in enumerate(steps):
            try:
                success, message = step()
            except adb_exec.DeviceError as error:
                message, own_remedy = cli.describe(error)
                self.items.append(item(fields, False, message, remedy=own_remedy))
                if one_device:
                    self.stopped = error
                    self.stopped_at = len(self.items) - 1
                    self._not_attempted(steps[index + 1 :], fields, own_remedy)
                    return self
                continue
            except cli.DEFECTS:
                raise
            except cli.TYPED_FAMILY as error:
                message, own_remedy = cli.describe(error)
                self.items.append(item(fields, False, message, remedy=own_remedy))
                continue
            self.items.append(item(fields, success, message, remedy=remedy))
        return self

    def _not_attempted(self, rest, at: Mapping[str, object], remedy: str) -> None:
        reason = NOT_ATTEMPTED.format(noun=self.noun, label=at.get(self.key))
        for fields, _step in rest:
            self.items.append(item(fields, False, reason, remedy=remedy))

    def finish(self, *, json_mode: bool, extra: Mapping[str, object] | None = None) -> int:
        """Report the batch: print it when every item succeeded, else raise.

        Text mode prints nothing here -- each script prints its items its own
        way first -- and a failed batch's ``Error:`` / ``Remedy:`` follow on
        stderr from common.cli.

        Args:
            json_mode: Print the JSON object on success.
            extra: The batch's own keys (``action``, counts, ...), placed after
                ``success`` (and ``error`` / ``remedy``) and before ``items``.

        Returns:
            0, when every item succeeded.

        Raises:
            cli.BatchFailure: An item failed, or a device error stopped the batch.
        """
        extra = dict(extra or {})
        if not self.failed:
            if json_mode:
                print(json.dumps({"success": True, **extra, "items": self.items}, indent=2))
            return 0
        error, remedy = self._verdict()
        status = cli.exit_code_for(self.stopped) if self.stopped is not None else cli.EXIT_FAILURE
        raise cli.BatchFailure(error, remedy, {**extra, "items": self.items}, exit_status=status)

    def _verdict(self) -> tuple[str, str]:
        """The batch's ``error`` and ``remedy``."""
        nouns = f"{self.noun}s"
        if self.stopped is not None and self.stopped_at is not None:
            message, remedy = cli.describe(self.stopped)
            at = self.items[self.stopped_at]
            return (
                f"the batch stopped at {self.noun} {at.get(self.key)} ({self.stopped_at} of "
                f"{len(self.items)} {nouns} ran before it): {message}",
                remedy,
            )
        failures = [entry for entry in self.items if not entry["success"]]
        first = failures[0]
        summary = f"{len(failures)} of {len(self.items)} {nouns} failed"
        if len(failures) == 1:
            return f"{summary}: {first.get(self.key)}: {first['error']}", first["remedy"]
        return (
            f"{summary}; the first, {first.get(self.key)}: {first['error']}",
            "Each failed item carries its own error and remedy (`items`); fix those and "
            "retry them.",
        )
