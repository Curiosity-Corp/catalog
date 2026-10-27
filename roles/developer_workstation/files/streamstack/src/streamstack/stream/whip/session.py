"""One WHIP publishing session: negotiate, connect, send, watch, tear down. asyncio, one session per connection.

The pieces are aiortc's transports (ICE, DTLS-SRTP, RTP and RTCP with NACK retransmission and sender reports) and
nothing of its `RTCPeerConnection`: that class insists on encoding frames itself and on a codec list that has no
HEVC or AV1 and only baseline H.264. Here the encoders have already run on the GPU, so the senders only packetize
(`Passthrough`), and the SDP is ours (`sdp.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcdtlstransport import RTCCertificate, RTCDtlsFingerprint, RTCDtlsParameters, RTCDtlsTransport
from aiortc.rtcicetransport import RTCIceGatherer, RTCIceParameters, RTCIceServer, RTCIceTransport
from aiortc.rtcrtpparameters import (
    RTCRtcpParameters,
    RTCRtpCodecParameters,
    RTCRtpHeaderExtensionParameters,
    RTCRtpSendParameters,
)
from aiortc.rtcrtpsender import RTCEncodedFrame, RTCRtpSender
from aiortc.rtp import RtcpRtpfbPacket
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

from streamstack.stream.whip import protocol, sdp
from streamstack.stream.whip.media import Frame, VideoConfig
from streamstack.stream.whip.protocol import WhipClient, WhipError

log = logging.getLogger(__name__)

GATHER_TIMEOUT = 10.0
ICE_TIMEOUT = 15.0
DTLS_TIMEOUT = 10.0
CLOSE_TIMEOUT = 3.0
RTCP_SILENCE = 10.0  # seconds without any RTCP from a server that was sending it before the session is dead
STATS_INTERVAL = 1.0
VIDEO_QUEUE = 240
AUDIO_QUEUE = 100
WINDOW = 5.0
RTP_PLI = 1
RTP_FIR = 4
RTCP_NACK = 1


@dataclass
class SessionOptions:
    endpoint: str
    token: str = ""
    ice_servers: tuple[str, ...] = ()  # "stun:host:port"; none by default, so nothing leaves the machine but the POST
    timeout: float = 10.0  # for each HTTP exchange


class FrameTrack(MediaStreamTrack):
    """A track whose frames are already RTP payloads. Bounded: video that cannot be sent is dropped up to the next
    keyframe (a receiver cannot use the frames in between), audio drops its oldest."""

    def __init__(self, kind: str, limit: int) -> None:
        super().__init__()
        self.kind = kind
        self._limit = limit
        self._queue: deque[Frame] = deque()
        self._wake = asyncio.Event()
        self._need_key = kind == "video"
        self.dropped = 0

    def push(self, frame: Frame) -> None:
        if self.readyState == "ended":
            return
        if self.kind == "video":
            if self._need_key and not frame.keyframe:
                self.dropped += 1
                return
            if len(self._queue) >= self._limit:
                self.dropped += len(self._queue)
                self._queue.clear()
                if not frame.keyframe:
                    self.dropped += 1
                    self._need_key = True
                    return
            if frame.keyframe:
                self._need_key = False
        elif len(self._queue) >= self._limit:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(frame)
        self._wake.set()

    async def recv(self) -> Frame:  # type: ignore[override]
        while not self._queue:
            if self.readyState == "ended":
                raise MediaStreamError
            self._wake.clear()
            await self._wake.wait()
        return self._queue.popleft()

    def stop(self) -> None:
        super().stop()
        self._wake.set()

    @property
    def queued(self) -> int:
        return len(self._queue)


class Passthrough(RTCRtpSender):
    """An RTP sender for payloads that are already packetized. Everything else of aiortc's sender stays: sequence
    numbers, SSRC, sender reports, retransmission on NACK, and the receiver-report statistics (RTT, loss, jitter)."""

    def __init__(self, track: FrameTrack, transport: RTCDtlsTransport, ssrc: int) -> None:
        super().__init__(track, transport)
        self._ssrc = ssrc
        self.last_rtcp = 0.0
        self.nacks = 0
        self.plis = 0
        self.keyframe_requested: Callable[[], None] = lambda: None

    async def _next_encoded_frame(self, codec: RTCRtpCodecParameters) -> RTCEncodedFrame | None:
        frame = await self.track.recv()
        return RTCEncodedFrame(frame.payloads, frame.timestamp & 0xFFFFFFFF, None)

    def _send_keyframe(self) -> None:
        self.plis += 1
        self.keyframe_requested()

    async def _handle_rtcp_packet(self, packet) -> None:
        self.last_rtcp = time.monotonic()
        if isinstance(packet, RtcpRtpfbPacket) and packet.fmt == RTCP_NACK:
            self.nacks += len(packet.lost)
        await super()._handle_rtcp_packet(packet)


class _Rate:
    """Bytes per second over the last few seconds."""

    def __init__(self) -> None:
        self.points: deque[tuple[float, int]] = deque()

    def add(self, now: float, total: int) -> None:
        self.points.append((now, total))
        while len(self.points) > 2 and now - self.points[0][0] > WINDOW:
            self.points.popleft()

    def kbps(self) -> float:
        if len(self.points) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.points[0], self.points[-1]
        return round((b1 - b0) * 8 / 1000 / (t1 - t0), 1) if t1 > t0 else 0.0


def _servers(urls: tuple[str, ...]) -> list[RTCIceServer]:
    return [RTCIceServer(u) for u in urls]


def _loss_percent(fraction: int | float | None) -> float:
    return round((fraction or 0) * 100 / 256, 2)


@dataclass
class Stats:
    ice_state: str = "new"
    dtls_state: str = "new"
    rtt_ms: float | None = None
    loss_percent: float = 0.0
    jitter_ms: float = 0.0
    bitrate_kbps: float = 0.0
    packets_sent: int = 0
    bytes_sent: int = 0
    nacks: int = 0
    plis: int = 0
    dropped: int = 0
    queued: int = 0
    rtcp_age_s: float | None = None
    trickle: bool = False
    resource: str = ""
    codec: str = ""
    extra: dict[str, object] = field(default_factory=dict)


class WhipSession:
    """Negotiate and run one publish. `connect` raises `WhipError`; afterwards `dead()` resolves with the reason the
    session ended (the server dropped it, ICE or DTLS failed, the server went silent)."""

    def __init__(self, options: SessionOptions, video: VideoConfig, with_audio: bool) -> None:
        self.options = options
        self.video_config = video
        self.with_audio = with_audio
        self.stats = Stats(codec=video.label)
        self.video_track = FrameTrack("video", VIDEO_QUEUE)
        self.audio_track = FrameTrack("audio", AUDIO_QUEUE)
        self.keyframe_requested: Callable[[], None] = lambda: None
        self._client = WhipClient(options.endpoint, options.token, options.timeout)
        self._resource: protocol.Resource | None = None
        self._gatherer: RTCIceGatherer | None = None
        self._ice: RTCIceTransport | None = None
        self._dtls: RTCDtlsTransport | None = None
        self._senders: list[Passthrough] = []
        self._tasks: list[asyncio.Task] = []
        self._dead: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._closing = False
        self._close_task: asyncio.Future[None] | None = None
        self._rate = _Rate()
        self._started_at = 0.0

    # -- connect -------------------------------------------------------------------------------------------

    async def connect(self) -> None:
        try:
            await self._connect()
        except BaseException:
            await self.close()
            raise

    async def _connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._gatherer = RTCIceGatherer(iceServers=_servers(self.options.ice_servers))
        try:
            await asyncio.wait_for(self._gatherer.gather(), GATHER_TIMEOUT)
        except (TimeoutError, OSError) as exc:  # the same class as TimeoutError from Python 3.11
            raise WhipError(f"cannot gather network candidates ({exc or 'timed out'})") from None
        local = [candidate_to_sdp(c) for c in self._gatherer.getLocalCandidates() if c.component == 1]
        if not local:
            raise WhipError("this machine has no network address to publish from", fatal=False)
        params = self._gatherer.getLocalParameters()
        certificate = RTCCertificate.generateCertificate()
        fingerprints = [(f.algorithm, f.value) for f in certificate.getFingerprints() if f.algorithm == "sha-256"]
        cname = "ss-" + secrets.token_hex(5)
        ssrcs = {"video": secrets.randbits(32), "audio": secrets.randbits(32)}
        offered = [sdp.MediaOffer(self.video_config.spec, "0", ssrcs["video"])]
        if self.with_audio:
            offered.append(sdp.MediaOffer(sdp.OPUS, "1", ssrcs["audio"]))
        offer = sdp.build_offer(
            ufrag=params.usernameFragment or "",
            password=params.password or "",
            fingerprints=fingerprints,
            candidates=local,
            media=offered,
            cname=cname,
            session_id=int(time.time()),
        )
        resource = await loop.run_in_executor(None, self._client.publish, offer)
        self._resource = resource
        self.stats.trickle = resource.trickle
        self.stats.resource = protocol.plain(resource.url)
        try:
            answer = sdp.parse_answer(resource.answer)
            accepted = sdp.negotiate(answer, offered)
        except sdp.SdpError as exc:
            raise WhipError(str(exc), fatal=True) from None
        section = next(iter(accepted.values()))

        ice = RTCIceTransport(self._gatherer)
        ice._connection.ice_controlling = True  # we offered, so we nominate
        ice.on("statechange", lambda: self._state_changed("ice"))
        self._ice = ice
        for value in sdp.candidate_values(answer):
            try:
                candidate = candidate_from_sdp(value)
            except (AssertionError, ValueError):
                continue
            if candidate.component == 1:
                await ice.addRemoteCandidate(candidate)
        await ice.addRemoteCandidate(None)
        dtls = RTCDtlsTransport(ice, [certificate])
        dtls._set_role("server" if section.setup == "active" else "client")
        dtls.on("statechange", lambda: self._state_changed("dtls"))
        self._dtls = dtls
        self._started_at = time.monotonic()
        try:
            await asyncio.wait_for(
                ice.start(RTCIceParameters(section.ufrag, section.password, answer.ice_lite)), ICE_TIMEOUT
            )
        except TimeoutError:
            raise WhipError(
                f"ICE did not connect within {ICE_TIMEOUT:.0f}s: the server's media address is not reachable"
            ) from None
        if ice.state != "completed":
            raise WhipError("ICE failed: no network path to the server's media address (a firewall blocking UDP?)")
        remote = RTCDtlsParameters([RTCDtlsFingerprint(a, v) for a, v in section.fingerprints], "auto")
        try:
            await asyncio.wait_for(dtls.start(remote), DTLS_TIMEOUT)
        except TimeoutError:
            raise WhipError(f"the DTLS handshake took longer than {DTLS_TIMEOUT:.0f}s") from None
        except Exception as exc:  # pyOpenSSL raises its own error types for a bad certificate or alert
            raise WhipError(f"the DTLS handshake failed ({type(exc).__name__}: {exc})") from None
        if dtls.state != "connected":
            raise WhipError(
                "the DTLS handshake failed (the server's certificate did not match its fingerprint, or it refused)"
            )
        await self._start_senders(offered, ssrcs, cname, accepted)
        self._tasks = [asyncio.ensure_future(self._watch()), asyncio.ensure_future(self._poll())]

    async def _start_senders(
        self, offered: list[sdp.MediaOffer], ssrcs: dict[str, int], cname: str, accepted: dict[str, sdp.AnswerMedia]
    ) -> None:
        if self._dtls is None:
            raise WhipError("the media transport is not ready")
        for item in offered:
            kind = item.codec.kind
            track = self.video_track if kind == "video" else self.audio_track
            sender = Passthrough(track, self._dtls, ssrcs[kind])
            sender.keyframe_requested = self.keyframe_requested
            extensions = []
            for ident, uri in accepted[kind].extmap.items():
                if uri == sdp.MID_URI:
                    extensions.append(RTCRtpHeaderExtensionParameters(id=ident, uri=uri))
            codec = RTCRtpCodecParameters(
                mimeType=item.codec.mime,
                clockRate=item.codec.clock_rate,
                channels=item.codec.channels or None,
                payloadType=item.codec.payload_type,
            )
            await sender.send(
                RTCRtpSendParameters(
                    codecs=[codec], muxId=item.mid, rtcp=RTCRtcpParameters(cname=cname), headerExtensions=extensions
                )
            )
            self._senders.append(sender)

    # -- during ----------------------------------------------------------------------------------------------

    def push_video(self, frame: Frame) -> None:
        self.video_track.push(frame)

    def push_audio(self, frame: Frame) -> None:
        if self.with_audio:
            self.audio_track.push(frame)

    def _state_changed(self, which: str) -> None:
        ice, dtls = self._ice, self._dtls
        if which == "ice" and ice is not None and ice.state in ("failed", "closed"):
            self._die(f"ICE {ice.state}: the connection to the server was lost")
        if which == "dtls" and dtls is not None and dtls.state in ("failed", "closed"):
            self._die(f"DTLS {dtls.state}: the server ended the secure connection")

    def _die(self, reason: str) -> None:
        if not self._dead.done():
            self._dead.set_result(reason)

    async def dead(self) -> str:
        return await self._dead

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            seen = [s.last_rtcp for s in self._senders if s.last_rtcp]
            if seen and time.monotonic() - max(seen) > RTCP_SILENCE:
                self._die(f"the server stopped answering (no RTCP for {RTCP_SILENCE:.0f}s)")
                return

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            try:
                await self._collect()
            except (OSError, RuntimeError, AttributeError) as exc:  # a stats failure must never end a stream
                log.debug("whip stats failed: %s", exc)

    async def _collect(self) -> None:
        now = time.monotonic()
        stats = self.stats
        stats.ice_state = self._ice.state if self._ice else "new"
        stats.dtls_state = self._dtls.state if self._dtls else "new"
        packets = sent = 0
        rtt: float | None = None
        loss = jitter = 0.0
        for sender in self._senders:
            report = await sender.getStats()
            for item in report.values():
                if item.type == "outbound-rtp":
                    packets += item.packetsSent
                    sent += item.bytesSent
                elif item.type == "remote-inbound-rtp":
                    if item.roundTripTime is not None:
                        rtt = max(rtt or 0.0, item.roundTripTime * 1000)
                    loss = max(loss, _loss_percent(item.fractionLost))
                    clock = 90000 if item.kind == "video" else 48000
                    jitter = max(jitter, (item.jitter or 0) * 1000 / clock)
        seen = [s.last_rtcp for s in self._senders if s.last_rtcp]
        self._rate.add(now, sent)
        stats.packets_sent, stats.bytes_sent = packets, sent
        stats.rtt_ms = round(rtt, 1) if rtt is not None else stats.rtt_ms
        stats.loss_percent, stats.jitter_ms = loss, round(jitter, 1)
        stats.bitrate_kbps = self._rate.kbps()
        stats.nacks = sum(s.nacks for s in self._senders)
        stats.plis = sum(s.plis for s in self._senders)
        stats.dropped = self.video_track.dropped + self.audio_track.dropped
        stats.queued = self.video_track.queued + self.audio_track.queued
        stats.rtcp_age_s = round(now - max(seen), 1) if seen else None

    # -- end -----------------------------------------------------------------------------------------------

    async def close(self, delete: bool = True) -> None:
        """Tear down: end the senders, DELETE the resource, close DTLS and ICE. Never raises."""
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.ensure_future(self._teardown(delete))
        # a second caller (the attempt being cancelled while the sink closes it) waits for the same teardown
        await asyncio.shield(self._close_task)

    async def _teardown(self, delete: bool) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.video_track.stop()
        self.audio_track.stop()
        for sender in self._senders:
            await self._quietly(sender.stop(), "stop sender")
        if delete and self._resource is not None:
            # the server frees the session at once, before the transports go quiet and it has to time them out
            await self._quietly(asyncio.get_running_loop().run_in_executor(None, self._delete), "DELETE")
        if self._dtls is not None:
            await self._quietly(self._dtls.stop(), "close DTLS")
        if self._ice is not None:
            await self._quietly(self._ice.stop(), "close ICE")
        elif self._gatherer is not None:
            await self._quietly(self._gatherer._connection.close(), "close gatherer")
        self._die("closed")

    def _delete(self) -> None:
        if self._resource is None:
            return
        try:
            self._client.delete(self._resource)
        except WhipError as exc:
            log.info("WHIP DELETE: %s", exc.message)

    @staticmethod
    async def _quietly(awaitable, what: str) -> None:
        try:
            await asyncio.wait_for(awaitable, CLOSE_TIMEOUT)
        except (TimeoutError, OSError, RuntimeError, ConnectionError, MediaStreamError) as exc:
            log.debug("whip %s: %s", what, exc or type(exc).__name__)

    @property
    def host(self) -> str:
        return urlsplit(self.options.endpoint).hostname or ""
