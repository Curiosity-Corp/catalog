"""The relay sink that publishes to a WHIP endpoint.

It obeys the same contract as the ffmpeg pipe sink (`write` never blocks; a destination that cannot keep up costs
itself frames and nobody else anything; a lost connection is retried with backoff at the next keyframe) but there is
no child process: the Matroska the relay writes is read here, cut into RTP payloads (`media.py`) and handed to a
`WhipSession` that runs on its own thread and event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout  # builtin TimeoutError only from Python 3.11
from dataclasses import asdict

from streamstack.stream import mkv
from streamstack.stream.sinks import BACKOFF, STABLE_SECONDS
from streamstack.stream.whip import media
from streamstack.stream.whip.protocol import WhipError
from streamstack.stream.whip.session import SessionOptions, WhipSession

log = logging.getLogger(__name__)

GOP_FRAMES = 600  # what is kept while connecting, so the stream starts on the latest keyframe, not the next one
EVENT_GAP = 10.0  # at most one "the server asked for a keyframe" notice this often
STOP_TIMEOUT = 8.0


class WhipSink:
    name: str

    def __init__(
        self,
        name: str,
        endpoint: str,
        token_source: Callable[[], str],
        *,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, str], None] | None = None,
        ice_servers: tuple[str, ...] = (),
        timeout: float = 10.0,
        session_factory: Callable[[SessionOptions, media.VideoConfig, bool], WhipSession] = WhipSession,
    ) -> None:
        self.name = name
        self._endpoint = endpoint
        self._token_source = token_source
        self._clock = clock
        self._on_event = on_event or (lambda kind, text: None)
        self._ice_servers = ice_servers
        self._timeout = timeout
        self._factory = session_factory
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._attempt = 0
        self._task: Future[None] | None = None
        self._session: WhipSession | None = None
        self._demux = mkv.Demuxer()
        self._video: media.VideoPacketizer | None = None
        self._audio: media.AudioPacketizer | None = None
        self._with_audio = False
        self._gop: deque[tuple[str, media.Frame]] = deque()
        self._closing = False
        self._fatal = False
        self._not_before = 0.0
        self._failures = 0
        self._last_keyframe_event = 0.0
        self.state = "idle"
        self.restarts = 0
        self.dropped_frames = 0
        self.bytes_accepted = 0
        self.last_error = ""
        self.connected_at = 0.0
        self.ever_live = False

    # -- relay interface -----------------------------------------------------------------------------------

    def open(self, header: bytes) -> None:
        with self._lock:
            if self._closing:
                raise OSError("the sink is shut down")
            if self._fatal:
                raise OSError(self.last_error)
            now = self._clock()
            if now < self._not_before:
                raise OSError(f"{self.name} is waiting {self._not_before - now:.0f}s before it reconnects")
            self._stop_locked()
            self._demux = mkv.Demuxer()
            self._gop.clear()
            self._video = self._audio = None
            self.state = "connecting"
            self.connected_at = 0.0
            self._attempt += 1
            self._demux.feed(header)

    def write(self, data: bytes, video_blocks: int, keyframe: bool) -> bool:
        with self._lock:
            if self._closing or self.state not in ("connecting", "live"):
                return False
            try:
                events = self._demux.feed(data)
            except mkv.MkvError as exc:
                self._fail_locked(f"the stream is not valid Matroska ({exc})", fatal=True)
                return False
            for event in events:
                if isinstance(event, mkv.Header):
                    if not self._begin_locked(event):
                        return False
                elif isinstance(event, mkv.Block):
                    self._block_locked(event)
            self.bytes_accepted += len(data)
            return True

    def on_track(self, track: mkv.Track) -> None:
        """The relay switched to another encoder: its parameter sets apply from its next keyframe. A different
        codec cannot continue the session (the SDP named the old one), so that reconnects."""
        with self._lock:
            if track.kind != "video" or self._video is None:
                return
            try:
                config = media.video_config(track)
            except media.MediaError as exc:
                self._fail_locked(str(exc), fatal=True)
                return
            if config.kind != self._video.config.kind:
                self._on_event("info", f"{self.name} changed codec to {config.label}; reconnecting.")
                self._stop_locked()
                self.state = "idle"
                return  # write() now returns False and the relay reopens at the next keyframe
            self._video.reconfigure(config)

    def close(self) -> None:
        with self._lock:
            self._closing = False  # a close() from the relay is not a shutdown
            closing = self._stop_locked()
            if self.state not in ("failed", "backoff"):
                self.state = "idle"
        self._await(closing)

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            closing = self._stop_locked()
            self.state = "stopped"
        self._await(closing)
        self._stop_loop()

    # -- internals -----------------------------------------------------------------------------------------

    def _begin_locked(self, header: mkv.Header) -> bool:
        video = header.track("video")
        if video is None:
            self._fail_locked("the stream has no video track", fatal=True)
            return False
        try:
            config = media.video_config(video)
            audio = header.track("audio")
            if audio is not None:
                media.audio_config(audio)
        except media.MediaError as exc:
            self._fail_locked(str(exc), fatal=True)
            return False
        origin = media.Origin()
        self._video = media.VideoPacketizer(config, origin)
        self._audio = media.AudioPacketizer(origin) if audio is not None else None
        self._with_audio = audio is not None
        self._start_locked(config)
        return True

    def _start_locked(self, config: media.VideoConfig) -> None:
        loop = self._ensure_loop()
        attempt = self._attempt
        self._task = asyncio.run_coroutine_threadsafe(self._run(attempt, config), loop)

    def _block_locked(self, block: mkv.Block) -> None:
        if block.track == mkv.TRACK_VIDEO and self._video is not None:
            frame = self._video.packetize(block)
            kind = "video"
        elif block.track == mkv.TRACK_AUDIO and self._audio is not None:
            frame = self._audio.packetize(block)
            kind = "audio"
        else:
            return
        if frame is None:
            if kind == "video":
                self.dropped_frames += 1
            return
        if self.state == "connecting":
            if kind == "video" and frame.keyframe:
                self._gop.clear()
            if kind == "video" and not self._gop and not frame.keyframe:
                return  # nothing to anchor it to
            if len(self._gop) >= GOP_FRAMES:
                self._gop.clear()
                self.dropped_frames += 1
                return
            if kind == "audio" and not self._gop:
                return
            self._gop.append((kind, frame))
            return
        session, loop = self._session, self._loop
        if session is None or loop is None:
            return
        push = session.push_video if kind == "video" else session.push_audio
        loop.call_soon_threadsafe(push, frame)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=run, name=f"whip-{self.name}", daemon=True)
        thread.start()
        ready.wait(5)
        self._loop, self._thread = loop, thread
        return loop

    def _stop_loop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_TIMEOUT)

    def _stop_locked(self) -> Future[None] | None:
        """End the running attempt; the session's own teardown (DELETE included) runs on its loop. The returned
        future completes when that is done: wait for it with `_await`, never while holding the lock (the loop
        thread takes the lock too)."""
        self._attempt += 1  # whatever the old attempt reports from here on is stale
        task, session, loop = self._task, self._session, self._loop
        self._task = self._session = None
        self._gop.clear()
        if task is None or loop is None:
            return None
        task.cancel()
        if session is None:
            return None
        return asyncio.run_coroutine_threadsafe(session.close(), loop)

    def _await(self, done: Future[None] | None) -> None:
        if done is None:
            return
        try:
            done.result(timeout=STOP_TIMEOUT)
        except FutureTimeout:
            log.warning("WHIP %s did not close within %.0fs", self.name, STOP_TIMEOUT)
        except Exception as exc:  # session.close never raises; this is the future's own failure
            log.warning("WHIP %s close failed: %s", self.name, exc)

    async def _run(self, attempt: int, config: media.VideoConfig) -> None:
        token = ""
        try:
            token = self._token_source()
        except OSError as exc:
            self._finish(attempt, str(exc), fatal=True)
            return
        options = SessionOptions(self._endpoint, token, self._ice_servers, self._timeout)
        session = self._factory(options, config, self._with_audio)
        session.keyframe_requested = self._keyframe_requested
        with self._lock:
            if attempt != self._attempt:
                return
            self._session = session
        try:
            await session.connect()
        except WhipError as exc:
            self._finish(attempt, exc.message, fatal=exc.fatal, retry_after=exc.retry_after)
            return
        except asyncio.CancelledError:
            await session.close()
            raise
        with self._lock:
            if attempt != self._attempt:
                stale = True
            else:
                stale = False
                self.state = "live"
                self.ever_live = True
                self.connected_at = self._clock()
                backlog = list(self._gop)
                self._gop.clear()
        if stale:
            await session.close()
            return
        self._on_event("ok", f"{self.name} is connected")
        for kind, frame in backlog:
            (session.push_video if kind == "video" else session.push_audio)(frame)
        try:
            reason = await session.dead()
        except asyncio.CancelledError:
            raise
        if reason != "closed":
            await session.close()
            self._finish(attempt, reason, fatal=False, was_live=True)

    def _keyframe_requested(self) -> None:
        now = self._clock()
        if now - self._last_keyframe_event >= EVENT_GAP:
            self._last_keyframe_event = now
            self._on_event("info", f"{self.name}: the server asked for a new keyframe; one arrives within the interval")

    def _finish(self, attempt: int, reason: str, fatal: bool, retry_after: float = 0.0, was_live: bool = False) -> None:
        with self._lock:
            if attempt != self._attempt or self._closing:
                return
            if was_live and self.connected_at and self._clock() - self.connected_at >= STABLE_SECONDS:
                self._failures = 0
            self._session = None
            self._fail_locked(reason, fatal=fatal, retry_after=retry_after)

    def _fail_locked(self, reason: str, fatal: bool = False, retry_after: float = 0.0) -> None:
        self.last_error = reason
        self._attempt += 1
        self._gop.clear()
        if fatal:
            self._fatal = True
            self.state = "failed"
            self._on_event("error", f"{self.name} cannot publish: {reason}")
            return
        delay = max(BACKOFF[min(self._failures, len(BACKOFF) - 1)], retry_after)
        self._failures += 1
        self.restarts += 1
        self._not_before = self._clock() + delay
        self.state = "backoff"
        self._on_event("warn", f"{self.name} lost its connection: {reason}. Retrying in {delay:.0f}s.")

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            session = self._session
            stats = asdict(session.stats) if session is not None else {}
            stats.pop("extra", None)
            queued = stats.get("queued", 0)
            return {
                "state": self.state,
                "restarts": self.restarts,
                "dropped_frames": self.dropped_frames + stats.get("dropped", 0),
                "queued_bytes": 0 if not queued else int(queued * 1200),
                "net_bytes": stats.get("bytes_sent", 0),
                "last_error": self.last_error,
                "speed": 0.0,
                "retry_in_s": max(0.0, round(self._not_before - now, 1)) if self.state == "backoff" else 0.0,
                "connected_s": round(now - self.connected_at, 1) if self.state == "live" and self.connected_at else 0.0,
                "whip": stats,
            }
