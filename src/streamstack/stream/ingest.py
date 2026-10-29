"""SRT input: listen for a stream from another machine and record it as it arrives.

This is the receiving end of a contribution link and the "remote backup recording": a second StreamStack (or any SRT
sender) calls this one, and what arrives is written to a Matroska file with no re-encode. Run it on the machine that
should hold the backup, so the recording survives the sender's disk, crash or lost network.

One ffmpeg listens (`mode=listener`) and copies packets to disk. It serves one caller at a time; when that caller
goes away (the sender stopped, or the link dropped and it will reconnect) the file is closed and a new listener, with a
new numbered file, takes its place. The file is flushed packet by packet, so a crash costs at most the last moment.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from streamstack.stream.proc import Child, ProgressParser

Spawn = Callable[..., Child]
Notify = Callable[[str, str], None]

RELISTEN_S = 1.0
QUICK_FAIL_S = 3.0  # a listener that ends this soon never got to listen (the port is taken, the passphrase is bad)
MAX_QUICK_FAILS = 3


def ingest_argv(
    ffmpeg: str,
    port: int,
    path: Path,
    latency_ms: int = 200,
    passphrase: str = "",
    bind: str = "0.0.0.0",  # noqa: S104 - a receiver listens for remote senders
) -> list[str]:
    query = {"mode": "listener", "latency": str(latency_ms * 1000), "pkt_size": "1316"}
    if passphrase:
        query["passphrase"] = passphrase
    url = f"srt://{bind}:{port}?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats", "-progress", "pipe:2",
        "-stats_period", "1", "-y", "-i", url, "-map", "0", "-c", "copy", "-f", "matroska", "-flush_packets", "1",
        str(path),
    ]  # fmt: skip


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "srt-backup"


@dataclass
class Segment:
    path: Path
    started_at: float
    bytes: int = 0


class SrtIngest:
    """One SRT listener that records what a remote sender delivers."""

    def __init__(
        self,
        ffmpeg: str,
        port: int,
        directory: Path,
        name: str,
        spawn: Spawn,
        latency_ms: int = 200,
        passphrase: str = "",
        wall: Callable[[], float] = time.time,
        notify: Notify | None = None,
    ) -> None:
        self.port = port
        self.latency_ms = latency_ms
        self._ffmpeg = ffmpeg
        self._directory = directory
        self._stem = slug(name)
        self._spawn = spawn
        self._passphrase = passphrase
        self._wall = wall
        self._notify = notify or (lambda level, text: None)
        self._stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(wall()))
        self._lock = threading.RLock()
        self._child: Child | None = None
        self._progress = ProgressParser()
        self._segments: list[Segment] = []
        self._quick_fails = 0
        self._next_listen = 0.0
        self._stopped = False
        self.state = "idle"  # idle | waiting | receiving | failed | stopped
        self.last_error = ""
        self.callers = 0

    # -- control --------------------------------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._listen()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            child, self._child = self._child, None
            self.state = "stopped"
        if child is not None:
            child.stop(grace=2.0)

    def tick(self) -> None:
        """Called regularly: when the caller has gone, listen again for the next one."""
        with self._lock:
            if self._stopped or self.state == "failed" or self._child is not None:
                return
            if self._wall() >= self._next_listen:
                self._listen()

    def _listen(self) -> None:
        index = len(self._segments) + 1
        suffix = "" if index == 1 else f"-{index:03d}"
        path = self._directory / f"{self._stem}-{self._stamp}{suffix}.mkv"
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"cannot create {self._directory}: {exc.strerror or exc}")
            return
        segment = Segment(path, self._wall())
        argv = ingest_argv(self._ffmpeg, self.port, path, self.latency_ms, self._passphrase)
        redact = (lambda text: text.replace(self._passphrase, "***")) if self._passphrase else None
        self._progress = ProgressParser()
        child = self._spawn(
            f"ingest-{self.port}",
            argv,
            on_line=lambda line: self._line(segment, line),
            on_exit=lambda code: self._exited(segment, code),
            redact=redact,
        )
        try:
            child.start()
        except OSError as exc:
            self._fail(f"cannot start ffmpeg: {exc.strerror or exc}")
            return
        self._segments.append(segment)
        self._child = child
        self.state = "waiting"

    # -- events ---------------------------------------------------------------------------------------------

    def _line(self, segment: Segment, line: str) -> None:
        report = self._progress.feed(line)
        if report is None:
            return
        try:
            total = int(report.get("total_size", "0") or 0)
        except ValueError:
            return
        with self._lock:
            segment.bytes = max(segment.bytes, total)
            if total > 0 and self.state == "waiting" and not self._stopped:
                self.state = "receiving"
                self.callers += 1
                self._quick_fails = 0
                self._notify(
                    "ok", f"SRT input on port {self.port}: a sender connected, recording to {segment.path.name}."
                )

    def _exited(self, segment: Segment, code: int) -> None:
        with self._lock:
            child = self._child
            if self._stopped or child is None or child.stopping:
                return
            lasted = self._wall() - segment.started_at
            reason = child.last_error()
            self._child = None
            if segment.bytes > 0:
                self._notify(
                    "info", f"SRT input on port {self.port}: the sender went away; {segment.path.name} is saved."
                )
                self.state, self._next_listen = "waiting", self._wall() + RELISTEN_S
                return
            if lasted < QUICK_FAIL_S:
                self._quick_fails += 1
                if self._quick_fails >= MAX_QUICK_FAILS:
                    self._fail(reason or f"ffmpeg exited with status {code} (is port {self.port} already in use?)")
                    return
            self.state, self._next_listen = "waiting", self._wall() + RELISTEN_S
            self._remove_empty(segment)

    def _remove_empty(self, segment: Segment) -> None:
        if segment in self._segments:
            self._segments.remove(segment)
        try:
            if segment.path.exists() and segment.path.stat().st_size == 0:
                segment.path.unlink()
        except OSError as exc:
            self._notify("warn", f"Could not remove the empty {segment.path.name}: {exc.strerror or exc}.")

    def _fail(self, reason: str) -> None:
        self.state, self.last_error = "failed", reason
        self._notify("error", f"SRT input on port {self.port} stopped: {reason}")

    # -- state ----------------------------------------------------------------------------------------------

    def status(self) -> dict[str, object]:
        with self._lock:
            files = [str(s.path) for s in self._segments if s.bytes > 0]
            return {
                "active": not self._stopped and self.state != "failed",
                "state": self.state,
                "port": self.port,
                "latency_ms": self.latency_ms,
                "encrypted": bool(self._passphrase),
                "file": files[-1] if files else "",
                "files": files,
                "bytes": sum(s.bytes for s in self._segments),
                "callers": self.callers,
                "last_error": self.last_error,
            }
