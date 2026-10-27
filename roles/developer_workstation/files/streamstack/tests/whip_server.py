"""A real WHIP server for the tests: HTTP signalling on a thread, an aiortc peer connection per session.

It accepts the publish like a production ingest does (POST an offer, 201 with Location and an SDP answer, DELETE to
end it), receives the SRTP media over real ICE and DTLS on 127.0.0.1, and decodes it with libavcodec (through PyAV),
so a test can assert that picture and sound actually arrived and read the RTP statistics. It also injects the
failures a client has to survive: an HTTP error for the next requests, a session the server ends, and a server that
goes silent.

aiortc's answerer only knows constrained-baseline H.264, so the profile in the offer is relabelled for aiortc's
SDP negotiation; the decoder behind it is libavcodec, which decodes High profile as it comes.
"""

from __future__ import annotations

import asyncio
import http.server
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError


@dataclass
class Received:
    """What one session has received so far."""

    video_frames: int = 0
    audio_frames: int = 0
    width: int = 0
    height: int = 0
    sizes: list[tuple[int, int]] = field(default_factory=list)
    luma_levels: int = 0  # distinct luma values in the latest picture: a real picture has many, a black one few
    audio_peak: int = 0
    first_video_at: float = 0.0


@dataclass
class Session:
    id: str
    pc: RTCPeerConnection
    offer: str
    answer: str = ""
    received: Received = field(default_factory=Received)
    deleted: bool = False
    patches: int = 0
    tasks: list[asyncio.Task] = field(default_factory=list)
    silent: bool = False


class WhipServer:
    def __init__(self, token: str = "", path: str = "/whip", trickle: bool = False) -> None:
        self.token = token
        self.path = path
        self.trickle = trickle
        self.sessions: list[Session] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.fail_with: list[int] = []  # statuses for the next POSTs (consumed in order)
        self.break_ice = False  # answer with an ICE password that does not match: the client's checks are refused
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="whip-test-server", daemon=True)
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self.url = ""

    # -- lifecycle -----------------------------------------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self, port: int = 0) -> WhipServer:
        self._thread.start()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return

            def reply(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                server.requests.append((self.command, self.path, headers))
                if server.token and headers.get("authorization") != f"Bearer {server.token}":
                    return self.reply(401, {}, b"unauthorized")
                if self.command == "POST" and self.path == server.path:
                    if server.fail_with:
                        return self.reply(server.fail_with.pop(0), {"Retry-After": "1"}, b"nope")
                    try:
                        session = asyncio.run_coroutine_threadsafe(server._accept(body.decode()), server.loop).result(
                            20
                        )
                    except Exception as exc:  # the test then sees the 500 and the reason
                        return self.reply(500, {}, f"{type(exc).__name__}: {exc}".encode())
                    extra = {"Accept-Patch": "application/trickle-ice-sdpfrag"} if server.trickle else {}
                    return self.reply(
                        201,
                        {
                            "Content-Type": "application/sdp",
                            "Location": f"{server.path}/{session.id}",
                            "ETag": '"1"',
                            **extra,
                        },
                        session.answer.encode(),
                    )
                session = server._find(self.path)
                if session is None:
                    return self.reply(404)
                if self.command == "DELETE":
                    session.deleted = True
                    asyncio.run_coroutine_threadsafe(server._close(session), server.loop).result(10)
                    return self.reply(200)
                if self.command == "PATCH":
                    session.patches += 1
                    return self.reply(204 if server.trickle else 405, {"ETag": '"2"'} if server.trickle else {})
                return self.reply(405)

            do_POST = do_DELETE = do_PATCH = do_GET = handle_any

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}{self.path}"
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        for session in list(self.sessions):
            try:
                asyncio.run_coroutine_threadsafe(self._close(session), self.loop).result(10)
            except Exception:  # noqa: S110 - teardown of a test fixture: nothing to report if a peer is already gone
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)

    # -- sessions ------------------------------------------------------------------------------------------

    def _find(self, path: str) -> Session | None:
        ident = path.rsplit("/", 1)[-1].split("?")[0]
        return next((s for s in self.sessions if s.id == ident), None)

    async def _accept(self, offer: str) -> Session:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        session = Session(uuid.uuid4().hex[:12], pc, offer)
        self.sessions.append(session)

        @pc.on("track")
        def on_track(track) -> None:
            session.tasks.append(asyncio.ensure_future(self._consume(session, track)))

        relabelled = re.sub(r"profile-level-id=[0-9a-fA-F]{6}", "profile-level-id=42e01f", offer)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=relabelled, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        session.answer = pc.localDescription.sdp
        if self.break_ice:
            session.answer = re.sub(r"a=ice-pwd:\S+", "a=ice-pwd:" + "x" * 24, session.answer)
        return session

    async def _consume(self, session: Session, track) -> None:
        got = session.received
        try:
            while True:
                frame = await track.recv()
                if track.kind == "video":
                    got.video_frames += 1
                    got.first_video_at = got.first_video_at or time.monotonic()
                    got.width, got.height = frame.width, frame.height
                    if not got.sizes or got.sizes[-1] != (frame.width, frame.height):
                        got.sizes.append((frame.width, frame.height))
                    luma = bytes(frame.planes[0])
                    got.luma_levels = len(set(luma[:: max(1, len(luma) // 4096)]))
                else:
                    got.audio_frames += 1
                    pcm = bytes(frame.planes[0])
                    got.audio_peak = max(
                        got.audio_peak,
                        max(
                            (
                                abs(int.from_bytes(pcm[i : i + 2], "little", signed=True))
                                for i in range(0, min(len(pcm), 2000), 2)
                            ),
                            default=0,
                        ),
                    )
        except MediaStreamError:
            return

    async def _close(self, session: Session) -> None:
        for task in session.tasks:
            task.cancel()
        await session.pc.close()

    # -- test controls -------------------------------------------------------------------------------------

    def drop(self, session: Session) -> None:
        """The server ends the session (as a restart or an admin kick would): DTLS closes, nothing more is answered."""
        asyncio.run_coroutine_threadsafe(self._close(session), self.loop).result(10)

    def go_silent(self, session: Session) -> None:
        """Keep the connection but stop sending anything back (RTCP included): a one-way failure."""

        async def mute() -> None:
            async def swallow(data: bytes) -> None:
                return None

            for transceiver in session.pc.getTransceivers():
                transceiver.receiver.transport._send_rtp = swallow  # type: ignore[method-assign]
            session.silent = True

        asyncio.run_coroutine_threadsafe(mute(), self.loop).result(10)

    def stats(self, session: Session) -> dict[str, int]:
        async def collect() -> dict[str, int]:
            report = await session.pc.getStats()
            out = {"packets": 0, "lost": 0}
            for item in report.values():
                if item.type == "inbound-rtp":
                    out["packets"] += item.packetsReceived
                    out["lost"] += item.packetsLost
            return out

        return asyncio.run_coroutine_threadsafe(collect(), self.loop).result(10)

    def live(self) -> list[Session]:
        return [s for s in self.sessions if not s.deleted and s.pc.connectionState == "connected"]
