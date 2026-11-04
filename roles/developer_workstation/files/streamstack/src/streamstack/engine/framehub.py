"""The engine's frame hub: the program picture, fanned out to the recording and streaming encoders (#184, #181).

Recording and streaming used to run `ffmpeg -f v4l2 -i /dev/video43`. v4l2loopback answers a call app's
VIDIOC_S_FMT with EBUSY while any other opener is streaming from the node, so a running recording or stream could keep
Chromium, Edge, Teams or Zoom from opening StreamStack Camera. Now the engine's own graph writes the finished program
picture (raw NV12, the same split the encode taps use) to a pipe, and this hub serves it on a Unix socket:

    engine ffmpeg --raw NV12 pipe--> FrameHub.pump --> every connected encoder (ffmpeg `-i unix:<socket>`)

* One writer, any number of readers, no device node involved. A reader joins at a frame boundary.
* A slow reader never slows the engine: each reader has a two-frame queue and loses its oldest frame when it is full.
* While the engine sends nothing (camera gone, slate on the node), the last frame is repeated, so a running stream or
  recording holds a frozen picture instead of stalling.
* `<socket>.json` says what the socket carries (size, rate, pixel format), so an encoder needs no configuration.
  A change of size closes every reader (they would misparse the new frames) and the encoder generation restarts.
"""

from __future__ import annotations

import collections
import contextlib
import fcntl
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

PIX_FMT = "nv12"
READ_BYTES = 1 << 20
PIPE_BYTES = 4 << 20
QUEUE_FRAMES = 2
SEND_TIMEOUT = 5.0
HOLD_AFTER = 0.5  # seconds without a frame before the last one is repeated


def frame_bytes(width: int, height: int) -> int:
    """Size of one NV12 frame (even sides)."""
    return width * height * 3 // 2


def info_path(socket_path: Path) -> Path:
    return socket_path.with_name(socket_path.name + ".json")


def read_info(socket_path: Path) -> dict | None:
    """What a running hub says it carries, or None when there is no hub (no socket, or no readable description)."""
    try:
        if not socket_path.exists():
            return None
        info = json.loads(info_path(socket_path).read_text())
        width, height, fps = int(info["width"]), int(info["height"]), float(info["fps"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if width <= 0 or height <= 0 or width % 2 or height % 2 or fps <= 0 or info.get("pix_fmt") != PIX_FMT:
        return None
    return {"width": width, "height": height, "fps": fps, "pix_fmt": PIX_FMT}


class _Reader:
    """One connected encoder: a short queue and a thread that writes it to the socket."""

    def __init__(self, conn: socket.socket, on_gone: Callable[[_Reader], None]) -> None:
        self.conn = conn
        self.on_gone = on_gone
        self.queue: collections.deque[bytes] = collections.deque(maxlen=QUEUE_FRAMES)
        self.cond = threading.Condition()
        self.dropped = 0
        self.closed = False
        conn.settimeout(SEND_TIMEOUT)
        threading.Thread(target=self._send, daemon=True, name="streamstack-framehub-reader").start()

    def offer(self, frame: bytes) -> None:
        with self.cond:
            if len(self.queue) == QUEUE_FRAMES:
                self.dropped += 1  # the deque drops its oldest frame
            self.queue.append(frame)
            self.cond.notify()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()
        with contextlib.suppress(OSError):
            self.conn.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.conn.close()

    def _send(self) -> None:
        try:
            while True:
                with self.cond:
                    while not self.queue and not self.closed:
                        self.cond.wait()
                    if self.closed:
                        return
                    frame = self.queue.popleft()
                self.conn.sendall(frame)
        except OSError:
            pass  # the encoder went away (or stopped reading for SEND_TIMEOUT): it is dropped, the engine is not
        finally:
            self.close()
            self.on_gone(self)


class FrameHub:
    """Serves the engine's raw program frames on a Unix socket."""

    def __init__(
        self, path: Path, width: int, height: int, fps: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._readers: list[_Reader] = []
        self._buffer = bytearray()  # always starts on a frame boundary
        self._last: bytes | None = None
        self._last_at = 0.0
        self._frames = 0
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.width, self.height, self.fps = width, height, fps

    # -- lifecycle -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Bind the socket (replacing a stale one of a crashed Console) and start accepting encoders."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(8)
        except OSError:
            server.close()
            raise
        server.settimeout(0.5)
        self._stop.clear()
        self._server = server
        self._write_info()
        for target, name in ((self._accept, "accept"), (self._hold, "hold")):
            thread = threading.Thread(target=target, daemon=True, name=f"streamstack-framehub-{name}")
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(OSError):
                server.close()
        self._drop_readers()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
        for path in (self.path, info_path(self.path)):
            with contextlib.suppress(OSError):
                path.unlink()

    def set_geometry(self, width: int, height: int, fps: float) -> None:
        """Another size or rate: readers are closed (they would misparse the frames) and the socket is re-described."""
        if (width, height, fps) == (self.width, self.height, self.fps):
            return
        with self._lock:
            self.width, self.height, self.fps = width, height, fps
            self._buffer.clear()
            self._last = None
        self._drop_readers()
        if self._server is not None:
            self._write_info()

    # -- the engine side --------------------------------------------------------------------------------------------

    def pump(self, fd: int) -> None:
        """Feed from the engine child's pipe until it closes. Takes ownership of `fd` and closes it."""
        try:
            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            log.debug("framehub: could not enlarge the frame pipe")  # the default pipe still works
        with self._lock:
            self._buffer.clear()  # a new child starts on a frame boundary
        try:
            while not self._stop.is_set():
                chunk = os.read(fd, READ_BYTES)
                if not chunk:
                    return
                self.feed(chunk)
        except OSError as exc:
            log.debug("framehub: frame pipe closed: %s", exc)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    def feed(self, chunk: bytes) -> None:
        """Take bytes of the raw stream; every complete frame goes to every reader."""
        deliver: list[bytes] = []
        with self._lock:
            size = frame_bytes(self.width, self.height)
            self._buffer += chunk
            while len(self._buffer) >= size:
                deliver.append(bytes(self._buffer[:size]))
                del self._buffer[:size]
            if not deliver:
                return
            self._frames += len(deliver)
            self._last, self._last_at = deliver[-1], self._clock()
            readers = list(self._readers)
        for frame in deliver:
            for reader in readers:
                reader.offer(frame)

    # -- the encoder side -------------------------------------------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            return {
                "socket": str(self.path),
                "readers": len(self._readers),
                "frames": self._frames,
                "dropped": sum(r.dropped for r in self._readers),
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
            }

    def _accept(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            reader = _Reader(conn, self._gone)
            with self._lock:
                self._readers.append(reader)
                last = self._last
            if last is not None:
                reader.offer(last)  # a picture at once, not after the next engine frame

    def _gone(self, reader: _Reader) -> None:
        with self._lock:
            if reader in self._readers:
                self._readers.remove(reader)

    def _drop_readers(self) -> None:
        with self._lock:
            readers, self._readers = self._readers, []
        for reader in readers:
            reader.close()

    def _hold(self) -> None:
        """Repeat the last frame while the engine is silent."""
        while not self._stop.wait(1.0 / max(self.fps, 1.0)):
            with self._lock:
                idle = self._clock() - self._last_at
                frame, readers = self._last, list(self._readers)
            if frame is not None and readers and idle > HOLD_AFTER:
                for reader in readers:
                    reader.offer(frame)

    def _write_info(self) -> None:
        body = {"width": self.width, "height": self.height, "fps": self.fps, "pix_fmt": PIX_FMT}
        target = info_path(self.path)
        staging = target.with_name(target.name + ".new")
        try:
            staging.write_text(json.dumps(body))
            os.replace(staging, target)
        except OSError as exc:
            log.warning("framehub: cannot describe the frame socket (%s); encoders fall back to the loopback", exc)
