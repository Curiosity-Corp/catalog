"""Sinks: where the relay's Matroska bytes go. A pipe into an ffmpeg that talks to the network, or a file.

Both obey the relay's one rule for sinks: `write` never blocks. A destination that cannot keep up (a congested
uplink, a stalled disk) fills a bounded queue, and when it is full the queue is dropped and the sink waits for the
next keyframe, so one slow destination costs itself some frames and costs the others nothing.
"""

from __future__ import annotations

import contextlib
import io
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from streamstack.stream.proc import Child, ProgressParser

QUEUE_BYTES = 6 * 1024 * 1024  # about four seconds of a 12 Mbit/s stream
BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
STABLE_SECONDS = 30.0  # a connection that lasted this long resets the backoff
FSYNC_SECONDS = 1.0

States = ("idle", "connecting", "live", "backoff", "failed", "stopped")


class _Queue:
    """A byte-bounded queue with one consumer thread. `put` never blocks."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.items: deque[bytes] = deque()
        self.size = 0
        self.cond = threading.Condition()
        self.closed = False

    def put(self, data: bytes) -> bool:
        """False (and nothing queued) when the queue is full."""
        with self.cond:
            if self.closed:
                return False
            if self.size + len(data) > self.limit:
                return False
            self.items.append(data)
            self.size += len(data)
            self.cond.notify()
            return True

    def clear(self) -> None:
        with self.cond:
            self.items.clear()
            self.size = 0

    def get(self, timeout: float) -> bytes | None:
        with self.cond:
            if not self.items and not self.closed:
                self.cond.wait(timeout)
            if self.items:
                data = self.items.popleft()
                self.size -= len(data)
                return data
            return None

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class PipeSink:
    """An ffmpeg child fed through its stdin, reconnected with backoff when it exits."""

    def __init__(
        self,
        name: str,
        spawn: Callable[[Callable[[int], None], Callable[[str], None]], Child],
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, str], None] | None = None,
        queue_bytes: int = QUEUE_BYTES,
    ) -> None:
        self.name = name
        self._spawn = spawn
        self._clock = clock
        self._on_event = on_event or (lambda kind, text: None)
        self._queue_limit = queue_bytes
        self._lock = threading.RLock()
        self._child: Child | None = None
        self._queue: _Queue | None = None
        self._writer: threading.Thread | None = None
        self._failures = 0
        self._not_before = 0.0
        self._resync = False
        self._closing = False
        self.state = "idle"
        self.restarts = 0
        self.dropped_frames = 0
        self.bytes_accepted = 0
        self.net_bytes = 0
        self.last_error = ""
        self.speed = 0.0
        self.last_progress = 0.0
        self.connected_at = 0.0
        self.ever_live = False
        self._progress = ProgressParser()

    # -- relay interface -----------------------------------------------------------------------------------

    def open(self, header: bytes) -> None:
        with self._lock:
            if self._closing:
                raise OSError("the sink is shut down")
            now = self._clock()
            if now < self._not_before:
                raise OSError(f"{self.name} is waiting {self._not_before - now:.0f}s before it reconnects")
            self._teardown_locked(wait=False)
            queue = _Queue(self._queue_limit)
            self._queue = queue
            self._resync = False
            try:
                child = self._spawn(self._exited, self._line)
                child.start()
            except OSError as exc:
                self._fail(f"cannot start ffmpeg: {exc.strerror or exc}")
                raise OSError(self.last_error) from None
            self._child = child
            self.state = "connecting"
            self.connected_at = 0.0
            queue.put(header)
            self._writer = threading.Thread(
                target=self._write_loop, args=(child, queue), name=f"sink-{self.name}", daemon=True
            )
            self._writer.start()

    def write(self, data: bytes, video_blocks: int, keyframe: bool) -> bool:
        with self._lock:
            queue = self._queue
            if queue is None or self._child is None or self._closing:
                return False
            if self._resync:
                if not keyframe:
                    self.dropped_frames += video_blocks
                    return True
                self._resync = False
            if queue.put(data):
                self.bytes_accepted += len(data)
                return True
            # a full queue: drop what is waiting and rejoin at the next keyframe
            queue.clear()
            self.dropped_frames += video_blocks
            self._resync = True
            self._on_event("warn", f"{self.name} cannot keep up; dropping to the next keyframe")
            return True

    def close(self) -> None:
        with self._lock:
            self._closing = False  # a close() from the relay (geometry change) is not a shutdown
            self._teardown_locked(wait=True)
            if self.state not in ("failed", "backoff"):
                self.state = "idle"

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            self._teardown_locked(wait=True)
            self.state = "stopped"

    # -- internals -----------------------------------------------------------------------------------------

    def _teardown_locked(self, wait: bool) -> None:
        child, queue = self._child, self._queue
        self._child = self._queue = None
        if queue is not None:
            queue.close()
        if child is not None:
            child.stop(grace=2.0 if wait else 0.5)

    def _write_loop(self, child: Child, queue: _Queue) -> None:
        while True:
            data = queue.get(0.5)
            if data is None:
                if queue.closed:
                    return
                continue
            try:
                child.write(data)
            except (OSError, ValueError):
                return  # the exit handler reports it

    def _line(self, line: str) -> None:
        report = self._progress.feed(line)
        if report is None:
            return
        try:
            total = int(report.get("total_size", "0") or 0)
        except ValueError:
            return
        now = self._clock()
        if total > 0:
            with self._lock:
                if self.state == "connecting":
                    self.state = "live"
                    self.ever_live = True
                    self.connected_at = now
                    self._on_event("ok", f"{self.name} is connected")
                self.net_bytes = total
                self.last_progress = now
                speed = report.get("speed", "").rstrip("x")
                self.speed = float(speed) if speed.replace(".", "", 1).isdigit() else self.speed

    def _exited(self, code: int) -> None:
        with self._lock:
            child = self._child
            if child is None or child.stopping:
                return  # we ended it ourselves
            lasted = self._clock() - self.connected_at if self.connected_at else 0.0
            reason = child.last_error() or f"ffmpeg exited with status {code}"
            self._child = None
            if self._queue is not None:
                self._queue.close()
                self._queue = None
            if lasted >= STABLE_SECONDS:
                self._failures = 0
            self._fail(reason)

    def _fail(self, reason: str) -> None:
        delay = BACKOFF[min(self._failures, len(BACKOFF) - 1)]
        self._failures += 1
        self.restarts += 1
        self.last_error = reason
        self._not_before = self._clock() + delay
        self.state = "backoff"
        self.speed = 0.0
        self._on_event("warn", f"{self.name} lost its connection: {reason}. Retrying in {delay:.0f}s.")

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            queued = self._queue.size if self._queue else 0
            stale = self.state == "live" and now - self.last_progress > 6
            return {
                "state": "connecting" if stale else self.state,
                "restarts": self.restarts,
                "dropped_frames": self.dropped_frames,
                "queued_bytes": queued,
                "net_bytes": self.net_bytes,
                "last_error": self.last_error,
                "speed": self.speed,
                "retry_in_s": max(0.0, round(self._not_before - now, 1)) if self.state == "backoff" else 0.0,
                "connected_s": round(now - self.connected_at, 1) if self.state == "live" and self.connected_at else 0.0,
            }


class FileSink:
    """Writes the Matroska stream to a file, flushed every cluster and fsynced about once a second.

    The segment has no size and there are no cues, so a crash or a power cut leaves everything but the last
    fraction of a second playable (and `ffmpeg -c copy` repairs the index). `segment` rolls to a new file
    when the picture format changes mid-recording.
    """

    def __init__(self, name: str, path: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.path = path
        self._clock = clock
        self._file: io.FileIO | None = None
        self._segment = 0
        self._last_sync = 0.0
        self.bytes_written = 0
        self.dropped_frames = 0
        self.state = "idle"
        self.last_error = ""
        self.paths: list[Path] = []
        self._lock = threading.Lock()

    def _segment_path(self) -> Path:
        if self._segment == 0:
            return self.path
        return self.path.with_name(f"{self.path.stem}-{self._segment + 1:03d}{self.path.suffix}")

    def open(self, header: bytes) -> None:
        with self._lock:
            self._close_locked()
            target = self._segment_path()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._file = io.FileIO(target, "wb")
                self._file.write(header)
            except OSError as exc:
                self._file = None
                self._fail(exc)
                raise OSError(self.last_error) from None
            self.paths.append(target)
            self._segment += 1
            self._last_sync = self._clock()
            self.state = "live"

    def write(self, data: bytes, video_blocks: int, keyframe: bool) -> bool:
        with self._lock:
            handle = self._file
            if handle is None:
                return False
            try:
                handle.write(data)
                self.bytes_written += len(data)
                now = self._clock()
                if now - self._last_sync >= FSYNC_SECONDS:
                    os.fsync(handle.fileno())
                    self._last_sync = now
            except OSError as exc:
                self.dropped_frames += video_blocks
                self._close_locked()
                self._fail(exc)
                return False
            return True

    def close(self) -> None:
        with self._lock:
            self._close_locked()
            if self.state != "failed":
                self.state = "idle"

    def shutdown(self) -> None:
        with self._lock:
            self._close_locked()
            if self.state != "failed":
                self.state = "stopped"

    def _close_locked(self) -> None:
        handle, self._file = self._file, None
        if handle is not None:
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                handle.close()

    def _fail(self, exc: OSError) -> None:
        self.state = "failed"
        self.last_error = f"cannot write {self.path.name}: {exc.strerror or exc}"

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "restarts": 0,
                "dropped_frames": self.dropped_frames,
                "queued_bytes": 0,
                "net_bytes": self.bytes_written,
                "last_error": self.last_error,
                "speed": 1.0 if self.state == "live" else 0.0,
                "retry_in_s": 0.0,
                "connected_s": 0.0,
                "file": str(self.paths[-1]) if self.paths else "",
                "files": [str(p) for p in self.paths],
            }
