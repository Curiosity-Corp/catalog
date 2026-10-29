"""Show a change first, save it afterwards: the newest value of one file, written one write at a time.

A Console action that changes a setting used to write the file (an fsync, so tens of seconds on a starved disk) while it
held the lock `state()` also takes, or before it set the value `state()` reads. The switch, the card or the toast the
person was waiting for then waited for the disk (issue #168, the same shape as #161 and #167). With a `LatestWriter`:

1. the action changes its in-memory state under its own lock, hands the new value to `publish` (still under that lock,
   so values are numbered in the order the state changed), and leaves the lock;
2. it then calls `flush`, which takes the writer's own lock (never the state lock), and saves the NEWEST value
   published so far. Two actions that overlap are saved by whichever gets the writer first, the other finds nothing
   left to do, and a write can never put an older value over a newer one;
3. a failed save raises the `OSError` for the caller to report to the person. The value is dropped, not retried behind
   their back: whoever changes the setting next saves the whole newest value again.

Durability is unchanged: `write` is whatever fsyncing writer the caller already used. A file-backed store also lets its
readers ask `pending()` for the document being saved, so a reader sees the change the moment it is accepted.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class Nothing:
    """`pending()` has nothing to show: read the file."""


NOTHING = Nothing()


class LatestWriter(Generic[T]):
    def __init__(self, write: Callable[[T], object]) -> None:
        self._write = write
        self._gate = threading.Lock()  # guards the fields below; never held across I/O
        self._save_lock = threading.Lock()  # one write at a time; never taken by a reader or by `publish`
        self._value: T | Nothing = NOTHING  # the newest value not yet known to be on the disk
        self._version = 0

    def publish(self, value: T) -> None:
        """Make `value` the newest. Never touches the disk: call it while holding the state lock."""
        with self._gate:
            self._value = value
            self._version += 1

    def pending(self) -> T | Nothing:
        """The newest value while it is being saved (else NOTHING: the file is the truth)."""
        with self._gate:
            return self._value

    @property
    def dirty(self) -> bool:
        with self._gate:
            return not isinstance(self._value, Nothing)

    def flush(self) -> None:
        """Save the newest published value, unless another flush already did. Raises what `write` raises."""
        with self._save_lock:
            with self._gate:
                value, version = self._value, self._version
            if isinstance(value, Nothing):
                return
            try:
                self._write(value)
            finally:
                with self._gate:
                    if self._version == version:  # nothing newer arrived while writing: saved (or given up on)
                        self._value = NOTHING
