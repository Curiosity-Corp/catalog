"""Instant replay: the last N seconds of the encoded program held in RAM, written to a clip on demand.

`ReplaySink` is an ordinary relay sink: it receives the same Matroska clusters a recording would, so the video is
whatever the encoder chain already produces (hardware AV1, HEVC or H.264) and nothing is re-encoded to keep it or to
save it. It keeps whole groups of pictures (a keyframe and what follows), because a clip must start on a keyframe.

Memory is bounded twice. Groups older than the window (plus the one that straddles its edge) are dropped, and a byte
cap drops the oldest group whenever the buffer would exceed it, whatever the window says: a bitrate spike costs replay
depth, never memory. `stats()` reports the bytes held, the peak, the cap and the depth actually available, so the cap
is measured rather than assumed (tests/test_stream_replay.py feeds ten windows of a 20 Mbit/s stream and checks both
the reported figure and the interpreter's own allocation).

A clip is `header + the clusters from the chosen keyframe to now`, so its length is at least the length asked for and
at most that plus one keyframe interval (a copy cannot start between keyframes). It is written to a temporary name and
renamed, then remuxed with `ffmpeg -c copy` when ffmpeg is known so it has an index and a duration.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPLAY_ID = "replay"  # the pseudo-destination the service files the buffer under
DEFAULT_SECONDS = 30
MIN_SECONDS = 5
MAX_SECONDS = 600
DEFAULT_MAX_MB = 512
REMUX_TIMEOUT_S = 60.0


class ReplayError(Exception):
    """The buffer could not be armed or a clip could not be written; the message says why."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Group:
    """One keyframe and everything delivered until the next one."""

    at: float  # the sink clock when its first bytes arrived
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    frames: int = 0


class ReplaySink:
    """Holds the last `seconds` of the stream in memory (never more than `max_bytes`) and writes clips from it."""

    def __init__(
        self,
        name: str,
        seconds: int,
        max_bytes: int,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        if not MIN_SECONDS <= seconds <= MAX_SECONDS:
            raise ReplayError(f"The replay length must be from {MIN_SECONDS} to {MAX_SECONDS} seconds.", 400)
        if max_bytes < 1024 * 1024:
            raise ReplayError("The replay memory limit must be at least 1 MB.", 400)
        self.name = name
        self.seconds = seconds
        self.max_bytes = max_bytes
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._header = b""
        self._groups: deque[Group] = deque()
        self._bytes = 0
        self.peak_bytes = 0
        self.evicted_for_memory = 0  # groups dropped by the byte cap while still inside the window
        self.dropped_frames = 0
        self.state = "idle"
        self.last_error = ""
        self.saved: list[str] = []

    # -- relay interface ---------------------------------------------------------------------------------------

    def open(self, header: bytes) -> None:
        """A (new) stream begins: the picture format may have changed, so what was kept cannot join the new one."""
        with self._lock:
            self._groups.clear()
            self._header = header
            self._bytes = len(header)
            self.state = "live"
            self.last_error = ""

    def write(self, data: bytes, video_blocks: int, keyframe: bool) -> bool:
        with self._lock:
            if self.state != "live":
                return False
            now = self._clock()
            if keyframe or not self._groups:
                if not keyframe:
                    return True  # nothing can start on a delta frame: wait for the next keyframe
                self._groups.append(Group(now))
            group = self._groups[-1]
            group.chunks.append(data)
            group.size += len(data)
            group.frames += video_blocks
            self._bytes += len(data)
            self._trim(now)
            self.peak_bytes = max(self.peak_bytes, self._bytes)
            return True

    def close(self) -> None:
        with self._lock:
            if self.state != "failed":
                self.state = "idle"

    def shutdown(self) -> None:
        with self._lock:
            self._groups.clear()
            self._bytes = 0
            self.state = "stopped"

    # -- bounding --------------------------------------------------------------------------------------------

    def _trim(self, now: float) -> None:
        """Drop the oldest groups that the window no longer needs, then any beyond the byte cap."""
        while len(self._groups) > 1 and now - self._groups[1].at >= self.seconds:
            self._drop()  # the next group alone already reaches back a full window
        while len(self._groups) > 1 and self._bytes > self.max_bytes:
            self._drop()
            self.evicted_for_memory += 1
        if len(self._groups) == 1 and self._bytes > self.max_bytes:
            # a single group larger than the cap: keep nothing rather than exceed it
            self._drop()
            self.evicted_for_memory += 1

    def _drop(self) -> None:
        group = self._groups.popleft()
        self._bytes -= group.size

    # -- clips -----------------------------------------------------------------------------------------------

    def depth_seconds(self) -> float:
        with self._lock:
            return round(self._clock() - self._groups[0].at, 1) if self._groups else 0.0

    def save(self, path: Path, seconds: int | None = None, ffmpeg: str = "") -> dict[str, object]:
        """Write the last `seconds` (default: the buffer's length) to `path`; returns what was written."""
        wanted = self.seconds if seconds is None else seconds
        if not MIN_SECONDS <= wanted <= self.seconds:
            raise ReplayError(f"A clip can be from {MIN_SECONDS} to {self.seconds} seconds (the buffer's length).", 400)
        with self._lock:
            if self.state != "live" or not self._groups:
                raise ReplayError("Nothing is buffered yet: wait for the stream to produce a keyframe.", 409)
            now = self._clock()
            groups = list(self._groups)
            header = self._header
        # the latest keyframe at least `wanted` seconds back; when the buffer is shallower, the whole of it
        start = next((i for i in range(len(groups) - 1, -1, -1) if now - groups[i].at >= wanted), 0)
        chosen = groups[start:]
        length = now - chosen[0].at
        payload = b"".join([header] + [chunk for g in chosen for chunk in g.chunks])
        written = _write(path, payload, ffmpeg)
        self.saved.append(str(path))
        return {
            "file": str(path),
            "seconds": round(length, 1),
            "asked": wanted,
            "short": length + 0.001 < wanted,  # the buffer had not reached back that far yet
            "bytes": written,
            "frames": sum(g.frames for g in chosen),
        }

    # -- state -----------------------------------------------------------------------------------------------

    def status(self) -> dict:
        """The relay sink status the service reads for every destination."""
        with self._lock:
            return {
                "state": self.state,
                "restarts": 0,
                "dropped_frames": self.dropped_frames,
                "queued_bytes": 0,
                "net_bytes": self._bytes,
                "last_error": self.last_error,
                "speed": 1.0 if self.state == "live" else 0.0,
                "retry_in_s": 0.0,
                "connected_s": 0.0,
            }

    def stats(self) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            depth = round(now - self._groups[0].at, 1) if self._groups else 0.0
            return {
                "active": self.state == "live",
                "state": self.state,
                "seconds": self.seconds,
                "depth_s": min(depth, float(self.seconds) + 30.0),
                "bytes": self._bytes,
                "peak_bytes": self.peak_bytes,
                "max_bytes": self.max_bytes,
                "groups": len(self._groups),
                "evicted_for_memory": self.evicted_for_memory,
                "clips": list(self.saved[-5:]),
            }


def idle_stats(max_bytes: int, seconds: int = 0) -> dict[str, object]:
    """What `stats()` reports when nothing is armed, with the same keys so the state schema never changes shape."""
    return {
        "active": False,
        "state": "idle",
        "seconds": seconds,
        "depth_s": 0.0,
        "bytes": 0,
        "peak_bytes": 0,
        "max_bytes": max_bytes,
        "groups": 0,
        "evicted_for_memory": 0,
        "clips": [],
    }


def _write(path: Path, payload: bytes, ffmpeg: str) -> int:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    except OSError as exc:
        raise ReplayError(f"Cannot write in {path.parent}: {exc.strerror or exc}.") from None
    raw = Path(name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        final = raw
        if ffmpeg:
            final = _remux(ffmpeg, raw, path.parent) or raw
        os.replace(final, path)
        if final is not raw:
            raw.unlink(missing_ok=True)
        return path.stat().st_size
    except OSError as exc:
        raise ReplayError(f"Cannot write {path.name}: {exc.strerror or exc}.") from None
    finally:
        with contextlib.suppress(OSError):
            raw.unlink(missing_ok=True)


def _remux(ffmpeg: str, source: Path, directory: Path) -> Path | None:
    """Copy the streams into a Matroska file with an index and a duration; None when that did not work."""
    handle, name = tempfile.mkstemp(prefix=".remux.", suffix=".mkv.part", dir=directory)
    os.close(handle)
    target = Path(name)
    try:
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
        argv += ["-map", "0", "-c", "copy", "-f", "matroska", str(target)]
        run = subprocess.run(
            argv,
            capture_output=True,
            timeout=REMUX_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        target.unlink(missing_ok=True)
        return None
    if run.returncode or not target.stat().st_size:
        target.unlink(missing_ok=True)
        return None
    return target
