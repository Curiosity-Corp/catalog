"""A WHEP preview of the program picture: low-latency WebRTC video for the Console (issue #103, docs/whep-preview.md).

WHEP (WebRTC-HTTP egress protocol) is the player-side twin of WHIP (ADR 0002): the browser POSTs an SDP offer that
asks to receive one video track, the server answers with its own SDP and a resource URL, and DELETE on that URL ends
the session. There is no signalling channel beyond those requests, so the Console's existing HTTP server is all it
takes.

The picture is the same one the MJPEG preview shows: `Rig.frame(source, width)` returns a JPEG of the program output
(what /dev/video43 shows). A `FrameTrack` pulls it at the preview frame rate, decodes it and hands the frames to
aiortc, which encodes (VP8 or H.264, whichever the browser prefers) and sends them over ICE and DTLS-SRTP. Nothing
here touches the camera or the encoder generations of the stream relay.

aiortc is the optional `streamstack[whep]` extra (the same library as WHIP publishing). Without it `check()` says why
and the Console keeps the MJPEG preview, which is also the fallback whenever a WHEP session cannot start or breaks.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import importlib.metadata
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EXTRA_HINT = "install the whep extra: pip install 'streamstack[whep]'"
MAX_SDP = 64 * 1024
MAX_SESSIONS = 4  # a few windows at most; the oldest is closed to admit a new one (a reload leaves a stale session)
CONNECT_SECONDS = 15.0  # a session whose ICE and DTLS never complete is dropped
RESTART_SECONDS = 1.0  # the frame clock this far behind (the host was starved) restarts instead of bursting
STALL_SECONDS = 5.0  # no fresh picture from the source for this long ends the track, so the page falls back
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
MIN_FPS, MAX_FPS = 1, 30


class WhepError(Exception):
    """A refusal with the HTTP status the Console answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def check() -> str:
    """Empty when WHEP previews work here, otherwise the reason they do not."""
    try:
        importlib.import_module("aiortc")
    except ImportError as exc:
        name = getattr(exc, "name", "") or "aiortc"
        if name == "aiortc":
            return f"aiortc is not installed ({EXTRA_HINT})"
        return f"aiortc cannot load because {name} is missing or broken ({EXTRA_HINT})"
    for module_name, names in (
        ("aiortc", ("RTCPeerConnection", "RTCSessionDescription", "RTCConfiguration")),
        ("aiortc.mediastreams", ("MediaStreamTrack", "MediaStreamError", "VIDEO_CLOCK_RATE", "VIDEO_TIME_BASE")),
    ):
        module = importlib.import_module(module_name)
        missing = [n for n in names if not hasattr(module, n)]
        if missing:
            return f"aiortc has no {module_name}.{missing[0]}; the preview needs aiortc 1.15"
    try:
        from aiortc.rtcdtlstransport import RTCCertificate

        RTCCertificate.generateCertificate()
        import av

        av.codec.Codec("mjpeg", "r")
    except Exception as exc:  # a broken OpenSSL or libav must read as "unavailable", whatever it raises
        return f"WebRTC does not work here ({type(exc).__name__}: {exc})"
    return ""


def validate_offer(sdp: str) -> None:
    """A WHEP offer asks to receive exactly one video track; anything else is refused before aiortc sees it."""
    if not sdp.startswith("v=0"):
        raise WhepError("The body is not an SDP offer.", 400)
    sections = re.findall(r"^m=(\w+) ", sdp, flags=re.MULTILINE)
    if sections != ["video"]:
        raise WhepError("Offer exactly one video m-line (a receive-only video track).", 400)
    if re.search(r"^a=(sendrecv|sendonly)\s*$", sdp, flags=re.MULTILINE):
        raise WhepError("A WHEP offer is receive-only (a=recvonly).", 400)


@dataclass
class SessionStats:
    """What a session reports: enough to see whether a preview is healthy without a WebRTC stats dialog."""

    source: str
    width: int
    fps: int
    started: float = field(default_factory=time.time)
    state: str = "new"
    codec: str = ""
    frames: int = 0
    source_frames: int = 0  # distinct pictures decoded (a still program shows 1)
    grab_ms: float = 0.0  # the last fetch plus decode, the server's share of the latency

    def as_json(self, session_id: str) -> dict[str, Any]:
        return {
            "id": session_id,
            "source": self.source,
            "width": self.width,
            "fps": self.fps,
            "state": self.state,
            "codec": self.codec,
            "age_s": round(time.time() - self.started, 1),
            "frames": self.frames,
            "source_frames": self.source_frames,
            "grab_ms": round(self.grab_ms, 1),
        }


def decode_jpeg(codec_context: Any, jpeg: bytes) -> Any | None:
    """One JPEG to an even-sized yuv420p `av.VideoFrame`, or None when it is not a decodable picture."""
    import av

    try:
        frames = codec_context.decode(av.Packet(jpeg))
    except av.error.FFmpegError as exc:
        log.debug("whep: undecodable JPEG: %s", exc)
        return None
    if not frames:
        return None
    frame = frames[0]
    width, height = frame.width // 2 * 2, frame.height // 2 * 2
    if width < 2 or height < 2:
        return None
    return frame.reformat(width=width, height=height, format="yuv420p")


class FrameTrack:
    """Factory for the aiortc video track; the class is built lazily so importing this module needs no aiortc."""

    @staticmethod
    def create(
        grab: Callable[[], bytes | None],
        stats: SessionStats,
        pool: concurrent.futures.Executor,
        fps: int,
    ) -> Any:
        import av
        from aiortc.mediastreams import VIDEO_CLOCK_RATE, VIDEO_TIME_BASE, MediaStreamError, MediaStreamTrack

        class _Track(MediaStreamTrack):
            kind = "video"

            def __init__(self) -> None:
                super().__init__()
                self._period = 1.0 / fps
                self._began: float | None = None
                self._origin: float | None = None  # when the first frame went out: the RTP clock counts from here
                self._pts = -1
                self._count = 0
                self._last_jpeg: bytes | None = None
                self._frame: Any | None = None
                self._fresh_at = time.monotonic()
                self._codec = av.codec.CodecContext.create("mjpeg", "r")

            def _fetch(self) -> Any | None:
                """Runs in the pool: the newest program JPEG, decoded only when it changed."""
                began = time.monotonic()
                jpeg = grab()
                if jpeg is not None and jpeg is not self._last_jpeg and jpeg != self._last_jpeg:
                    frame = decode_jpeg(self._codec, jpeg)
                    if frame is not None:
                        self._last_jpeg, self._frame = jpeg, frame
                        stats.source_frames += 1
                if jpeg is not None:
                    self._fresh_at = time.monotonic()
                stats.grab_ms = (time.monotonic() - began) * 1000
                return self._frame

            async def recv(self) -> Any:
                if self.readyState != "live":
                    raise MediaStreamError
                loop = asyncio.get_running_loop()
                if self._began is None:
                    self._began = loop.time()
                else:
                    self._count += 1
                    due = self._began + self._count * self._period
                    wait = due - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    elif wait < -RESTART_SECONDS:  # starved: restart the schedule instead of bursting
                        self._began, self._count = loop.time(), 0
                frame = await loop.run_in_executor(pool, self._fetch)
                if frame is None or time.monotonic() - self._fresh_at > STALL_SECONDS:
                    self.stop()  # no picture (any more): the page sees the track end and falls back to MJPEG
                    raise MediaStreamError
                # The presentation time is the wall clock since the first frame, never the schedule's frame count:
                # aiortc sends it as the RTP timestamp, and the schedule restarts above. A count that went back to 0
                # sent timestamps from the past, which the browser's jitter buffer drops as stale, so the picture
                # froze for as long as the stream had already run (#158).
                if self._origin is None:
                    self._origin = loop.time()
                self._pts = max(self._pts + 1, int((loop.time() - self._origin) * VIDEO_CLOCK_RATE))
                frame.pts = self._pts
                frame.time_base = VIDEO_TIME_BASE
                stats.frames += 1
                return frame

        return _Track()


def _negotiated_codec(answer_sdp: str) -> str:
    """The first payload type of the answer's video m-line as a codec name ("VP8", "H264"), or ''."""
    match = re.search(r"^m=video \d+ \S+ (\d+)", answer_sdp, flags=re.MULTILINE)
    if not match:
        return ""
    rtpmap = re.search(rf"^a=rtpmap:{match.group(1)} ([A-Za-z0-9]+)/", answer_sdp, flags=re.MULTILINE)
    return rtpmap.group(1) if rtpmap else ""


@dataclass
class _Session:
    pc: Any
    stats: SessionStats
    opened: float = field(default_factory=time.monotonic)


class WhepService:
    """The WHEP sessions of one Console. Thread-safe: HTTP handler threads call it, an asyncio thread runs it."""

    def __init__(self, grab: Callable[[str, int], bytes | None], max_sessions: int = MAX_SESSIONS) -> None:
        self.grab = grab
        self.max_sessions = max_sessions
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._sessions: dict[str, _Session] = {}
        self._reason: str | None = None
        self._tasks: set[asyncio.Future[bool]] = set()

    # -- availability -----------------------------------------------------------

    def reason(self) -> str:
        """Why previews are unavailable (cached: importing aiortc and its crypto is not free), or ''."""
        with self._lock:
            if self._reason is None:
                self._reason = check()
            return self._reason

    def snapshot(self) -> dict[str, Any]:
        reason = self.reason()
        with self._lock:
            sessions = [s.stats.as_json(sid) for sid, s in self._sessions.items()]
        return {"available": not reason, "reason": reason, "max_sessions": self.max_sessions, "sessions": sessions}

    # -- the event loop ---------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_sessions, thread_name_prefix="whep")
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            self._thread = threading.Thread(target=run, name="streamstack-whep", daemon=True)
            self._thread.start()
            ready.wait(5)
            self._loop = loop
            return loop

    def _call(self, coro: Any, timeout: float) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise WhepError("The preview did not start in time.", 504) from None

    # -- sessions ---------------------------------------------------------------

    def offer(self, sdp: str, source: str = "program", width: int = 640, fps: int = 15) -> tuple[str, str]:
        """Answer a WHEP offer. Returns (session id, answer SDP). Raises WhepError."""
        if len(sdp) > MAX_SDP:
            raise WhepError("The offer is too large.", 413)
        validate_offer(sdp)
        reason = self.reason()
        if reason:
            raise WhepError(f"The WebRTC preview is unavailable: {reason}.", 501)
        fps = max(MIN_FPS, min(MAX_FPS, int(fps)))
        if not self.grab(source, width):
            raise WhepError("No picture from this source right now.", 503)
        return self._call(self._offer(sdp, source, width, fps), CONNECT_SECONDS)

    async def _offer(self, sdp: str, source: str, width: int, fps: int) -> tuple[str, str]:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

        await self._evict_for_room()
        stats = SessionStats(source=source, width=width, fps=fps)
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))  # host candidates only: the Console is local
        session_id = secrets.token_urlsafe(12)
        pool = self._pool
        if pool is None:
            await pc.close()
            raise WhepError("The Console is shutting down.", 503)
        track = FrameTrack.create(lambda: self.grab(source, width), stats, pool, fps)

        @pc.on("connectionstatechange")
        async def on_state() -> None:
            stats.state = pc.connectionState
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self._drop(session_id)

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            transceivers = [t for t in pc.getTransceivers() if t.kind == "video"]
            if len(transceivers) != 1:
                raise WhepError("Offer exactly one video m-line.", 400)
            pc.addTrack(track)
            await pc.setLocalDescription(await pc.createAnswer())
        except WhepError:
            await pc.close()
            raise
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            await pc.close()
            raise WhepError(f"The offer could not be applied: {exc}", 400) from exc
        except Exception as exc:  # aiortc raises a mix of types on a malformed SDP; the session is not worth a crash
            await pc.close()
            log.warning("whep: offer failed: %s: %s", type(exc).__name__, exc)
            raise WhepError(f"The offer could not be applied: {type(exc).__name__}", 400) from exc
        stats.codec = _negotiated_codec(pc.localDescription.sdp)
        session = _Session(pc, stats)
        with self._lock:
            self._sessions[session_id] = session
        asyncio.get_running_loop().call_later(CONNECT_SECONDS, self._connect_deadline, session_id)
        return session_id, pc.localDescription.sdp

    def _connect_deadline(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None and session.pc.connectionState not in ("connected", "closed"):
            log.info("whep: session %s never connected; dropping it", session_id)
            task = asyncio.ensure_future(self._drop(session_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _evict_for_room(self) -> None:
        while True:
            with self._lock:
                if len(self._sessions) < self.max_sessions:
                    return
                oldest = min(self._sessions, key=lambda sid: self._sessions[sid].opened)
            await self._drop(oldest)

    async def _drop(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            await session.pc.close()
        except Exception as exc:  # closing a half-open peer connection can raise from the transports
            log.debug("whep: closing %s: %s", session_id, exc)
        return True

    @staticmethod
    def valid_id(session_id: str) -> bool:
        """Whether this is the shape of an id this service hands out (one it no longer holds is not an error)."""
        return bool(SESSION_ID.fullmatch(session_id))

    def delete(self, session_id: str) -> bool:
        """End a session (the WHEP DELETE). False when there is no such session."""
        if not SESSION_ID.fullmatch(session_id):
            return False
        with self._lock:
            known = session_id in self._sessions
        if not known or self._loop is None:
            return False
        return bool(self._call(self._drop(session_id), 5.0))

    def close(self) -> None:
        """End every session and stop the loop (the Console is shutting down)."""
        with self._lock:
            loop, self._loop = self._loop, None
            ids = list(self._sessions)
            pool, self._pool = self._pool, None
        if loop is None:
            return

        async def shutdown() -> None:
            for sid in ids:
                await self._drop(sid)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            log.warning("whep: sessions did not close in time")
        loop.call_soon_threadsafe(loop.stop)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
