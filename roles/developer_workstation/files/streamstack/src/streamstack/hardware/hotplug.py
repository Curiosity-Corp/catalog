"""Hot-plug: when hardware comes or goes, probe again and say what changed (ADR 0003, section 2).

Nothing in the codebase listened for udev before this, so the monitor uses the one dependency-free way to: it runs
`udevadm monitor --udev` for the subsystems the probe reads (usb, drm, sound, video4linux) and treats every event line
as "something may have changed". Events arrive in bursts (a USB audio interface announces a device, its interfaces,
its sound card and its PipeWire nodes within a second), so they are debounced: one probe runs after a quiet second,
the new `Snapshot` is compared with the last one, and only a real difference is emitted, with the plan's changed
sections. A machine without `udevadm` (or where it cannot run) falls back to probing on a slow interval, which has
the same result with more delay.

`Monitor` takes its probe, its resolver and its event source as parameters so tests drive it with a fake sysfs and a
scripted event stream.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import IO, Protocol

from .model import Snapshot
from .plan import Plan

log = logging.getLogger("streamstack.hardware.hotplug")

SUBSYSTEMS = ("usb", "drm", "sound", "video4linux")
DEBOUNCE_SECONDS = 1.0
POLL_SECONDS = 10.0
RESTART_BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)


@dataclass(frozen=True)
class Change:
    """A hot-plug event: the hardware picture changed. `sources` are the probe sources whose data differ and
    `sections` the plan sections that differ (empty when only data the plan does not use changed)."""

    old: Snapshot
    new: Snapshot
    old_plan: Plan
    new_plan: Plan
    sources: tuple[str, ...]
    sections: tuple[str, ...]


def changed_sources(old: Snapshot, new: Snapshot) -> tuple[str, ...]:
    """Which probe sources report something different. A source whose status flipped (ok to denied) counts."""
    pairs = (
        ("usb", old.usb, new.usb),
        ("pci", old.pci, new.pci),
        ("drm", old.render_nodes, new.render_nodes),
        ("vulkan", old.vulkan, new.vulkan),
        ("alsa", old.alsa, new.alsa),
        ("pipewire", old.pipewire, new.pipewire),
        ("v4l2", old.v4l2, new.v4l2),
        ("loopback", old.loopback, new.loopback),
        ("ffmpeg", old.ffmpeg, new.ffmpeg),
        ("machine", old.machine, new.machine),
    )
    found = [name for name, before, after in pairs if before != after]
    for status in new.sources:
        before = old.status(status.source)
        differs = before is None or (before.state, before.error) != (status.state, status.error)
        if differs and status.source not in found:
            found.append(status.source)
    return tuple(found)


def parse_udev_line(line: str) -> bool:
    """Whether a `udevadm monitor` output line is an event. Header lines and blanks are not."""
    parts = line.split()
    # "UDEV  [1234.567] add      /devices/pci0000:00/... (usb)"
    return len(parts) >= 4 and parts[0] == "UDEV" and parts[2] in ("add", "remove", "change", "bind", "unbind")


class EventProcess(Protocol):
    stdout: IO[str] | None

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[[list[str]], EventProcess]


def _spawn(argv: list[str]) -> EventProcess:
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)


class Monitor:
    """Watches udev and emits `Change`s. `start()` returns at once; callbacks run on the monitor's own thread."""

    def __init__(
        self,
        probe: Callable[[], Snapshot],
        resolve: Callable[[Snapshot], Plan],
        on_change: Callable[[Change], None],
        *,
        debounce: float = DEBOUNCE_SECONDS,
        poll: float = POLL_SECONDS,
        spawn: Spawn = _spawn,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        backoff: Iterable[float] = RESTART_BACKOFF,
    ) -> None:
        self._probe, self._resolve, self._on_change = probe, resolve, on_change
        self._debounce, self._poll = debounce, poll
        self._spawn, self._which, self._clock = spawn, which, clock
        self._backoff = tuple(backoff) or (1.0,)
        self._cond = threading.Condition()
        self._dirty_at: float | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._process: EventProcess | None = None
        self._last: Snapshot | None = None
        self._last_plan: Plan | None = None
        self._probe_lock = threading.Lock()
        self.mode = "stopped"  # "udev" | "polling" | "stopped"
        self.error: str | None = None

    # -- lifecycle --------------------------------------------------------------------------------------------

    def start(self, initial: Snapshot | None = None) -> None:
        """Begin watching. `initial` is the snapshot the caller already has; without it the first probe runs here."""
        if self._threads:
            return
        self._stop.clear()
        snapshot = initial if initial is not None else self._probe()
        self._last, self._last_plan = snapshot, self._resolve(snapshot)
        use_udev = self._which("udevadm") is not None
        self.mode = "udev" if use_udev else "polling"
        if not use_udev:
            self.error = f"udevadm is not installed; probing every {self._poll:g} s instead"
        loops = [self._debounce_loop, self._udev_loop if use_udev else self._poll_loop]
        for loop in loops:
            thread = threading.Thread(target=loop, name=f"hotplug-{loop.__name__}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None:
            try:
                process.terminate()
            except OSError as exc:
                log.debug("udevadm already gone: %s", exc)
        with self._cond:
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
        self.mode = "stopped"

    # -- inputs -------------------------------------------------------------------------------------------------

    def poke(self) -> None:
        """Something may have changed: probe after the debounce interval, unless more events keep arriving."""
        with self._cond:
            self._dirty_at = self._clock()
            self._cond.notify_all()

    def refresh(self) -> Change | None:
        """Probe now (no debounce) and return the change, if any. Used by an explicit refresh action."""
        return self._reprobe()

    # -- threads ------------------------------------------------------------------------------------------------

    def _argv(self) -> list[str]:
        argv = ["udevadm", "monitor", "--udev"]
        for subsystem in SUBSYSTEMS:
            argv.append(f"--subsystem-match={subsystem}")
        return argv

    def _udev_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            started = self._clock()
            try:
                process = self._spawn(self._argv())
            except OSError as exc:
                self.error = f"udevadm monitor could not start: {exc}"
                log.warning(self.error)
                process = None
            if process is not None:
                self._process = process
                self.error = None
                self._read_events(process)
                self._process = None
            if self._stop.is_set():
                return
            if self._clock() - started > 60:
                attempt = 0
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            self.error = self.error or "udevadm monitor ended; restarting"
            # Events may have been missed while it was down.
            self.poke()
            if self._stop.wait(delay):
                return

    def _read_events(self, process: EventProcess) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                if self._stop.is_set():
                    return
                if parse_udev_line(line):
                    self.poke()
        except (OSError, ValueError) as exc:  # the pipe closed under us
            log.debug("udev stream closed: %s", exc)
        finally:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.debug("udevadm did not stop cleanly: %s", exc)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll):
            self.poke()

    def _debounce_loop(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while self._dirty_at is None and not self._stop.is_set():
                    self._cond.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                # wait until no event has arrived for the debounce interval
                while not self._stop.is_set():
                    due = (self._dirty_at or 0.0) + self._debounce
                    remaining = due - self._clock()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
                self._dirty_at = None
            if self._stop.is_set():
                return
            try:
                self._reprobe()
            except Exception:
                # A probe or a subscriber failing must not end hot-plug handling; the next event tries again.
                log.exception("hot-plug probe failed")
                self.error = "the last hot-plug probe failed; see the log"

    def _reprobe(self) -> Change | None:
        with self._probe_lock:
            return self._reprobe_locked()

    def _reprobe_locked(self) -> Change | None:
        snapshot = self._probe()
        previous, previous_plan = self._last, self._last_plan
        if previous is None or previous_plan is None:
            self._last, self._last_plan = snapshot, self._resolve(snapshot)
            return None
        if snapshot == previous:
            return None
        plan = self._resolve(snapshot)
        self._last, self._last_plan = snapshot, plan
        change = Change(
            previous, snapshot, previous_plan, plan, changed_sources(previous, snapshot), previous_plan.diff(plan)
        )
        self._on_change(change)
        return change
