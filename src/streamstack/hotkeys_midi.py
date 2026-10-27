"""MIDI input for the Console: reads the raw MIDI ports of ALSA (`/dev/snd/midiC*D*`) and reports note, control and
program messages to a callback, with hot-plug.

Raw ports need no library: each node is a byte stream, which `hotkeys.MidiParser` turns into events. A scan every
second opens new ports and forgets unplugged ones, so a controller plugged in during a call works at once. Ports are
opened for reading only; a port another program holds exclusively is listed as "busy" and retried.

Where a port's name comes from: `/proc/asound/card<N>/id` for the card ("nanoKONTROL2"), plus `.D<device>` when a card
has more than one port. Tests point `dev_dir` at a directory of FIFOs and `proc_dir` at a fake tree.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import select
import threading
import time
from collections.abc import Callable
from pathlib import Path

from streamstack import hotkeys

NODE = re.compile(r"midiC(\d+)D(\d+)")
SCAN_SECONDS = 1.0
Event = Callable[[str, hotkeys.MidiEvent], None]
Change = Callable[[list[dict]], None]


def available(dev_dir: Path = Path("/dev/snd")) -> str | None:
    """None when MIDI ports can be read here, else why not."""
    if not dev_dir.is_dir():
        return "This machine has no sound devices (/dev/snd)."
    return None


class MidiSource:
    """Watches the MIDI ports and calls `on_event(port id, event)`; `devices()` lists them with their status."""

    def __init__(
        self,
        on_event: Event,
        on_change: Change | None = None,
        dev_dir: Path = Path("/dev/snd"),
        proc_dir: Path = Path("/proc/asound"),
        scan_seconds: float = SCAN_SECONDS,
    ) -> None:
        self._on_event = on_event
        self._on_change = on_change
        self._dev_dir = dev_dir
        self._proc_dir = proc_dir
        self._scan = scan_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake_r = self._wake_w = -1
        self._open: dict[str, tuple[int, hotkeys.MidiParser]] = {}  # port id -> (fd, parser)
        self._info: dict[str, dict] = {}  # port id -> {"id", "name", "status"}
        self._retry: dict[str, float] = {}  # port id -> not before (monotonic): a port that just ended
        self.reason: str | None = None

    # -- control ----------------------------------------------------------------

    def start(self) -> str | None:
        """Start watching. None on success, else why MIDI is unavailable."""
        self.reason = available(self._dev_dir)
        if self.reason:
            return self.reason
        self._stop.clear()
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="hotkeys-midi", daemon=True)
        self._thread.start()
        return None

    def stop(self) -> None:
        self._stop.set()
        if self._wake_w >= 0:
            with contextlib.suppress(OSError):
                os.write(self._wake_w, b"x")
        thread = self._thread
        if thread is not None:
            thread.join(3.0)
        self._thread = None
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._wake_r = self._wake_w = -1

    def devices(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in sorted(self._info.values(), key=lambda d: d["id"])]

    # -- the watcher ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._rescan()
                fds = {fd: port for port, (fd, _) in self._open.items()}
                try:
                    ready, _, _ = select.select([self._wake_r, *fds], [], [], self._scan)
                except (OSError, ValueError):
                    continue
                for fd in ready:
                    if fd == self._wake_r:
                        with contextlib.suppress(OSError):
                            os.read(self._wake_r, 4096)
                    elif fd in fds:
                        self._read(fds[fd], fd)
        finally:
            for port in list(self._open):
                self._close(port)

    def _name(self, card: str, device: str) -> str:
        try:
            base = (self._proc_dir / f"card{card}" / "id").read_text().strip() or f"card {card}"
        except OSError:
            base = f"MIDI card {card}"
        return base if device == "0" else f"{base}.{device}"

    def _rescan(self) -> None:
        try:
            nodes = sorted(p.name for p in self._dev_dir.iterdir() if NODE.fullmatch(p.name))
        except OSError:
            nodes = []
        changed = False
        for node in list(self._open):
            if node not in nodes:
                self._close(node)
                changed = True
        for node in nodes:
            if node in self._open:
                continue
            if self._retry.get(node, 0.0) > time.monotonic():
                continue
            found = NODE.fullmatch(node)
            if found is None:
                continue
            card, device = found.groups()
            info = {"id": node, "name": self._name(card, device), "status": "ok"}
            try:
                fd = os.open(self._dev_dir / node, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                info["status"] = "busy" if exc.errno in (errno.EBUSY, errno.EACCES) else "error"
                if exc.errno == errno.EACCES:
                    info["status"] = "denied"
                changed |= self._remember(info)
                continue
            self._open[node] = (fd, hotkeys.MidiParser())
            changed |= self._remember(info)
        with self._lock:
            for gone in [k for k in self._info if k not in nodes]:
                del self._info[gone]
                changed = True
        if changed and self._on_change:
            self._on_change(self.devices())

    def _remember(self, info: dict) -> bool:
        with self._lock:
            previous = self._info.get(info["id"])
            self._info[info["id"]] = info
        return previous != info

    def _read(self, port: str, fd: int) -> None:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError:  # the device went away
            self._close(port)
            return
        if not data:  # end of stream (a FIFO whose writer left): reopen a little later
            self._close(port)
            self._retry[port] = time.monotonic() + self._scan
            return
        parser = self._open[port][1]
        for event in parser.feed(data):
            try:
                self._on_event(port, event)
            except Exception as exc:
                self.reason = f"a MIDI handler failed: {exc}"

    def _close(self, port: str) -> None:
        entry = self._open.pop(port, None)
        if entry:
            with contextlib.suppress(OSError):
                os.close(entry[0])
