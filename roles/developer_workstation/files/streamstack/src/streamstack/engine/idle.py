"""Idle the engine when no app reads StreamStack Camera, wake it within a second of the first reader (#128).

The native engine, its blur branch and the Canon live view cost 30 to 70 points of one core and keep the camera body
in live view for as long as they run, whether or not a call app is reading `/dev/video43`. This module decides *when*
that expensive part may rest. It does not touch a device; the Console's video controller acts on the verdicts.

**Why idle is not "stop writing".** With `exclusive_caps=1` the loopback node advertises capture capability only
while a producer is attached (v4l2loopback README), so an engine that simply closed its writer would make Edge, Teams
and Zoom drop "StreamStack Camera" from their device lists. Idle therefore means a cheap placeholder writer stays on
the node (the slate, `engine/slate.py`, about 1 percent of a core) while the engine child, the blur branch and the Canon
live view stop. Handing the node back is the slate's job (one writer at a time).

Two parts, both injectable for tests:

`IdleGovernor`  the state machine `running | idle | waking`. Pure: it is told "readers or not, demand or not, engine
                running or not" and answers `idle`, `wake` or nothing. Debounce (default 10 s without a reader before
                idle), a short confirmation before waking (an app that probes the device and closes it at once, as
                Edge and the Teams and Zoom join dialogs do, must not start the engine), and the keep-alive override.
`ReaderWatch`   the sensor: a thread that rescans `/proc/<pid>/fd` (`camerause.ProcScanner`, which never opens a video
                node) when inotify reports an open or close of the program node, and every few seconds as a safety
                net. Without inotify it polls. A scan that fails counts as "a reader is there": an unsure sensor must
                keep the camera awake, never put it to sleep under a call.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from streamstack import camerause

log = logging.getLogger(__name__)

MODES = ("auto", "off")
IDLE_AFTER = 10.0  # seconds without a reader before the engine rests
WAKE_CONFIRM = 0.25  # a reader must still be there this long after it first appeared (a probe is gone by then)
WAKE_TIMEOUT = 8.0  # a wake that has produced no frame by now is the engine supervisor's to report, not ours
SAFETY_SCAN = 3.0  # seconds between scans when inotify reports nothing (it can miss a reader that predates it)
POLL_SCAN = 1.0  # seconds between scans when inotify is not available
FRESH_FOR = 4.0  # a verdict is trusted for this long after the scan that produced it
TICK = 0.25  # the watch thread's own heartbeat: how stale a debounce or a confirmation can get between scans
EVENT_DEBOUNCE = 0.05  # open and close arrive in bursts; one scan answers the burst

RUNNING, IDLE, WAKING = "running", "idle", "waking"


def idle_mode(cfg: dict[str, str]) -> str:
    wanted = (cfg.get("STREAMSTACK_ENGINE_IDLE") or "auto").strip().lower()
    if wanted not in MODES:
        log.warning("STREAMSTACK_ENGINE_IDLE=%r is not auto or off; using auto", wanted)
        return "auto"
    return wanted


def idle_after(cfg: dict[str, str]) -> float:
    raw = cfg.get("STREAMSTACK_ENGINE_IDLE_AFTER") or IDLE_AFTER
    try:
        return max(1.0, float(raw))
    except ValueError:
        log.warning("STREAMSTACK_ENGINE_IDLE_AFTER=%r is not a number of seconds; using %g", raw, IDLE_AFTER)
        return IDLE_AFTER


def idle_ignore(cfg: dict[str, str]) -> set[str]:
    """Process names whose open handle on the program node does not count as a reader (a monitoring tool)."""
    return {part.strip() for part in (cfg.get("STREAMSTACK_ENGINE_IDLE_IGNORE") or "").split(",") if part.strip()}


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """What the governor wants done now: "idle" (rest the engine), "wake" (start it) or nothing."""

    action: str | None = None
    reason: str = ""


@dataclass
class IdleGovernor:
    """`running | idle | waking`. All times come from `clock`; every method is called from one thread (the monitor)."""

    clock: Callable[[], float] = time.monotonic
    enabled: bool = True
    after: float = IDLE_AFTER
    confirm: float = WAKE_CONFIRM
    wake_timeout: float = WAKE_TIMEOUT
    keep_alive: bool = False

    state: str = field(default=RUNNING, init=False)
    wakes: int = field(default=0, init=False)
    wake_ms: float | None = field(default=None, init=False)
    reason: str = field(default="", init=False)
    _since: float = field(default=0.0, init=False)
    _quiet_since: float | None = field(default=None, init=False)
    _seen_since: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._since = self._quiet_since = self.clock()

    # -- the one decision ----------------------------------------------------------------------------------------

    def step(self, *, readers: bool, demand: bool, engine_running: bool) -> Verdict:
        """Advance the machine. `readers`: an app holds the program node open. `demand`: something inside StreamStack
        needs the picture (a preview, a stream, a recording). `engine_running`: the engine child delivers frames."""
        now = self.clock()
        wanted = readers or demand or self.keep_alive
        if not self.enabled:
            if self.state == IDLE:
                return self._wake(now, "idling is switched off")
            return self._settle(now, engine_running)
        if self.state == RUNNING:
            return self._while_running(now, wanted, engine_running)
        if self.state == IDLE:
            return self._while_idle(now, readers, demand)
        return self._while_waking(now, wanted, engine_running)

    def _while_running(self, now: float, wanted: bool, engine_running: bool) -> Verdict:
        if wanted or not engine_running:
            self._quiet_since = None  # a faulting engine is never put to sleep: its state is the news
            return Verdict()
        if self._quiet_since is None:
            self._quiet_since = now
        quiet = now - self._quiet_since
        if quiet < self.after:
            return Verdict()
        self._go(IDLE, now)
        self._seen_since = None
        self.reason = f"no app has read the camera for {quiet:.0f} s"
        return Verdict("idle", self.reason)

    def _while_idle(self, now: float, readers: bool, demand: bool) -> Verdict:
        if self.keep_alive:
            return self._wake(now, "keep alive is on")
        if demand:
            return self._wake(now, "a preview or an output needs the picture")
        if not readers:
            self._seen_since = None
            return Verdict()
        if self._seen_since is None:
            self._seen_since = now
        if now - self._seen_since < self.confirm:
            return Verdict()
        return self._wake(now, "an app opened the camera")

    def _while_waking(self, now: float, wanted: bool, engine_running: bool) -> Verdict:
        if engine_running:
            self.wake_ms = round((now - self._since) * 1000.0, 1)
            self._go(RUNNING, now)
            self._quiet_since = None if wanted else now
            self.reason = ""
        elif now - self._since > self.wake_timeout:
            self._go(RUNNING, now)  # the supervisor reports why there is no picture; this machine stops waiting
            self._quiet_since = None
            self.reason = ""
        return Verdict()

    def _settle(self, now: float, engine_running: bool) -> Verdict:
        if self.state == WAKING:
            return self._while_waking(now, True, engine_running)
        return Verdict()

    def _wake(self, now: float, reason: str) -> Verdict:
        self._go(WAKING, now)
        self.wakes += 1
        self.reason = reason
        return Verdict("wake", reason)

    def _go(self, state: str, now: float) -> None:
        self.state, self._since = state, now

    # -- outside changes -----------------------------------------------------------------------------------------

    def force_running(self) -> bool:
        """The program camera was switched on or off by hand: leave idle at once. True if it was idle."""
        was = self.state == IDLE
        now = self.clock()
        if self.state != RUNNING:
            self._go(RUNNING, now)
        self._quiet_since = now
        self.reason = ""
        return was

    def snapshot(self) -> dict:
        now = self.clock()
        return {
            "mode": "auto" if self.enabled else "off",
            "state": self.state,
            "keep_alive": self.keep_alive,
            "after_s": self.after,
            "since_s": round(now - self._since, 1),
            "reason": self.reason,
            "wakes": self.wakes,
            "wake_ms": self.wake_ms,
        }


# ---------------------------------------------------------------------------
# inotify
# ---------------------------------------------------------------------------

IN_OPEN = 0x00000020
IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_DELETE_SELF = 0x00000400
IN_IGNORED = 0x00008000
IN_NONBLOCK = os.O_NONBLOCK
IN_CLOEXEC = os.O_CLOEXEC
_EVENT_HEAD = struct.Struct("iIII")  # wd, mask, cookie, len


class Inotify:
    """The few inotify calls this needs, through libc (the standard library has none). Raises OSError on failure.

    A watch on a device node reports every open and close of that node by any process, with no pid: it is a trigger
    for a scan, never an answer. It reads nothing from the device and cannot change it."""

    def __init__(self, libc: ctypes.CDLL | None = None) -> None:
        name = ctypes.util.find_library("c") or "libc.so.6"
        self._libc = libc or ctypes.CDLL(name, use_errno=True)
        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_init1: {os.strerror(err)}")
        self.fd = fd
        self._wds: dict[str, int] = {}

    def watch(self, path: str) -> bool:
        """Watch `path` for open and close. False when it does not exist (yet)."""
        if path in self._wds:
            return True
        wd = self._libc.inotify_add_watch(
            self.fd, os.fsencode(path), IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE | IN_DELETE_SELF
        )
        if wd < 0:
            err = ctypes.get_errno()
            if err in (errno.ENOENT, errno.EACCES, errno.ENOTDIR):
                return False
            raise OSError(err, f"inotify_add_watch {path}: {os.strerror(err)}")
        self._wds[path] = wd
        return True

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` seconds; True when the watched node was opened, closed or removed."""
        try:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return False
        if not ready:
            return False
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return False
        offset, seen = 0, False
        while offset + _EVENT_HEAD.size <= len(data):
            wd, mask, _cookie, length = _EVENT_HEAD.unpack_from(data, offset)
            offset += _EVENT_HEAD.size + length
            seen = True
            if mask & (IN_IGNORED | IN_DELETE_SELF):
                for path, known in list(self._wds.items()):
                    if known == wd:
                        del self._wds[path]  # the node was recreated: watch it again on the next round
        return seen

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class PollEvents:
    """The no-inotify stand-in: it never reports an event, so the watch falls back to scanning every POLL_SCAN."""

    fd = -1

    def watch(self, path: str) -> bool:
        return Path(path).exists()

    def wait(self, timeout: float) -> bool:
        time.sleep(max(0.0, min(timeout, 0.25)))
        return False

    def close(self) -> None:
        """Nothing is open."""


def open_events() -> Inotify | PollEvents:
    try:
        return Inotify()
    except (OSError, AttributeError) as exc:  # no libc symbol, a sandbox without inotify, descriptors used up
        log.warning("idle: inotify is not available (%s); polling the camera readers instead", exc)
        return PollEvents()


# ---------------------------------------------------------------------------
# The sensor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readers:
    """The last answer: who reads the program node, when it was taken, and whether the scan could be trusted."""

    at: float = 0.0
    ok: bool = False
    apps: tuple[camerause.Consumer, ...] = ()

    @property
    def present(self) -> bool:
        return bool(self.apps) or not self.ok  # an unsure sensor says "there is a reader"

    def public(self) -> list[dict[str, object]]:
        return [c.public() for c in self.apps]


class Scanner(Protocol):
    def scan(self, devices: set[str], ignore: Iterable[str] = ()) -> camerause.Scan: ...


class Events(Protocol):
    def watch(self, path: str) -> bool: ...
    def wait(self, timeout: float) -> bool: ...
    def close(self) -> None: ...


class ReaderWatch:
    """Keeps `latest` current: rescan when the node is opened or closed, and every `safety` seconds regardless.

    `on_tick` is called from the watch thread after every scan and every TICK seconds in between: the Console runs
    its idle governor from it, so a reader that appears is acted on within a fraction of a second and a debounce can
    expire without a new scan."""

    def __init__(
        self,
        device: Callable[[], str],
        *,
        scanner: Scanner | None = None,
        events: Events | None = None,
        ignore: set[str] | None = None,
        on_tick: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        safety: float = SAFETY_SCAN,
    ) -> None:
        self._device = device
        self._scanner = scanner or camerause.ProcScanner()
        self._events = events
        self._ignore = set(ignore or ())
        self._on_tick = on_tick
        self._clock = clock
        self._safety = safety
        self._latest = Readers()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scans = 0
        self._polling = False

    # -- lifecycle -----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self._events is None:
            self._events = open_events()
        self._polling = isinstance(self._events, PollEvents)
        self.scan()  # the first answer before the first tick, so a reader that predates the Console is seen
        self._thread = threading.Thread(target=self._run, daemon=True, name="streamstack-readers")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        if self._events is not None:
            self._events.close()
            self._events = None

    # -- the answer ----------------------------------------------------------------------------------------------

    @property
    def latest(self) -> Readers:
        with self._lock:
            return self._latest

    def fresh(self) -> bool:
        """True while the last scan is recent enough to put the camera to sleep on."""
        latest = self.latest
        return latest.ok and self._clock() - latest.at <= FRESH_FOR

    @property
    def scans(self) -> int:
        return self._scans

    def scan(self) -> Readers:
        """One look at /proc. A scan that raises counts as a reader (fail awake) and is logged once per change."""
        device = self._device()
        try:
            found = self._scanner.scan({device} if device else set(), self._ignore)
            new = Readers(self._clock(), True, tuple(found.consumers))
        except Exception as exc:  # the scanner reads other processes' state; whatever it throws, stay awake
            log.warning("idle: the camera reader scan failed (%s); keeping the camera awake", exc)
            new = Readers(self._clock(), False, ())
        self._scans += 1
        with self._lock:
            self._latest = new
        return new

    def _tick(self) -> None:
        if self._on_tick is None:
            return
        try:
            self._on_tick()
        except Exception:
            log.exception("idle: the tick hook failed")  # the sensor must outlive a bug in what it feeds

    # -- the thread ----------------------------------------------------------------------------------------------

    def _run(self) -> None:
        events = self._events
        if events is None:  # stop() ran before the thread did
            return
        next_scan = self._clock() + self._safety
        while not self._stop.is_set():
            device = self._device()
            watched = bool(device) and events.watch(device)
            period = POLL_SCAN if self._polling or not watched else self._safety  # no events: scan, do not wait for one
            try:
                moved = events.wait(max(0.05, min(next_scan - self._clock(), TICK)))
            except OSError as exc:
                log.warning("idle: reading device events failed (%s); polling instead", exc)
                events.close()
                self._events = events = PollEvents()
                self._polling = True
                moved = False
            if moved:
                self._stop.wait(EVENT_DEBOUNCE)  # the rest of the burst
                events.wait(0)
                self.scan()
                next_scan = self._clock() + period
            elif self._clock() >= next_scan:
                self.scan()
                next_scan = self._clock() + period
            self._tick()


class NullWatch:
    """A watch that never observes anything: tests and the demo rig. It is never `fresh`, so nothing idles."""

    latest = Readers()

    def start(self) -> None:
        """Nothing runs."""

    def stop(self) -> None:
        """Nothing runs."""

    def fresh(self) -> bool:
        return False

    def scan(self) -> Readers:
        return self.latest

    scans = 0
