"""The WHEP preview: offer validation, a real WebRTC session against aiortc as the viewer, and the HTTP routes."""

from __future__ import annotations

import asyncio
import http.client
import re
import time
from collections.abc import Iterator

import pytest
from http_support import HTTP_TIMEOUT

from streamstack import config
from streamstack.console import whep
from streamstack.console.rig import Rig
from streamstack.console.server import ConsoleServer
from streamstack.console.whep import WhepError, WhepService, decode_jpeg, validate_offer

needs_aiortc = pytest.mark.skipif(bool(whep.check()), reason=f"WHEP preview unavailable: {whep.check()}")

OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\n"


def card(name: str) -> bytes:
    return (config.data_dir() / "console/demo" / name).read_bytes()


# -- offer validation (no aiortc needed) --------------------------------------------------------------------------


def test_validate_accepts_one_receive_only_video_section() -> None:
    validate_offer(OFFER)


@pytest.mark.parametrize(
    "sdp",
    [
        "hello",
        OFFER + "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
        OFFER + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n",
        "v=0\r\ns=-\r\nt=0 0\r\n",
        OFFER.replace("a=recvonly", "a=sendrecv"),
        OFFER.replace("a=recvonly", "a=sendonly"),
    ],
)
def test_validate_refuses_anything_else(sdp: str) -> None:
    with pytest.raises(WhepError) as info:
        validate_offer(sdp)
    assert info.value.status == 400


def test_offer_limits_and_unavailable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WhepService(lambda source, width: card("card-brb.jpg"))
    with pytest.raises(WhepError) as big:
        service.offer("v=0" + "x" * whep.MAX_SDP)
    assert big.value.status == 413
    monkeypatch.setattr(whep, "check", lambda: "aiortc is not installed (install the whep extra)")
    with pytest.raises(WhepError) as off:
        service.offer(OFFER)
    assert off.value.status == 501 and "whep extra" in off.value.message
    snapshot = service.snapshot()
    assert snapshot["available"] is False and "aiortc" in snapshot["reason"]
    service.close()


def test_negotiated_codec_reads_the_answer() -> None:
    sdp = "m=video 9 UDP/TLS/RTP/SAVPF 97 96\r\na=rtpmap:97 H264/90000\r\na=rtpmap:96 VP8/90000\r\n"
    assert whep._negotiated_codec(sdp) == "H264"
    assert whep._negotiated_codec("m=audio 9 RTP 0") == ""


@needs_aiortc
def test_decode_jpeg_gives_even_yuv420p_and_refuses_garbage() -> None:
    import av

    context = av.codec.CodecContext.create("mjpeg", "r")
    frame = decode_jpeg(context, card("card-canon.jpg"))
    assert frame is not None and frame.format.name == "yuv420p"
    assert frame.width % 2 == 0 and frame.height % 2 == 0
    assert decode_jpeg(context, b"\xff\xd8\xff not a picture") is None


# -- a real session ------------------------------------------------------------------------------------------------


async def view(service: WhepService, frames: int = 5, source: str = "program") -> tuple[list[tuple[int, int]], str]:
    """Be the browser: offer, apply the answer, receive `frames` frames. Returns their sizes and the session id."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    await pc.setLocalDescription(await pc.createOffer())
    loop = asyncio.get_running_loop()
    session_id, answer = await loop.run_in_executor(
        None, lambda: service.offer(pc.localDescription.sdp, source, 320, 15)
    )
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
    receiver = pc.getReceivers()[0]
    sizes: list[tuple[int, int]] = []
    try:
        for _ in range(frames):
            frame = await asyncio.wait_for(receiver.track.recv(), timeout=30)
            sizes.append((frame.width, frame.height))
    finally:
        await pc.close()
    return sizes, session_id


@pytest.fixture
def service() -> Iterator[WhepService]:
    cards = {"program": card("card-brb.jpg")}
    svc = WhepService(lambda source, width: cards.get(source))
    yield svc
    svc.close()


@needs_aiortc
def test_a_viewer_receives_the_program_picture(service: WhepService) -> None:
    sizes, session_id = asyncio.run(view(service))
    assert len(sizes) == 5 and all(w > 0 and h > 0 for w, h in sizes)
    deadline = time.monotonic() + 10
    while service.snapshot()["sessions"] and time.monotonic() < deadline:
        time.sleep(0.1)  # the viewer closed: the server notices and drops the session
    assert service.snapshot()["sessions"] == [] or service.delete(session_id)


@needs_aiortc
def test_stats_report_codec_and_frames(service: WhepService) -> None:
    async def run() -> dict:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        await pc.setLocalDescription(await pc.createOffer())
        loop = asyncio.get_running_loop()
        _, answer = await loop.run_in_executor(None, lambda: service.offer(pc.localDescription.sdp, "program", 320, 10))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        receiver = pc.getReceivers()[0]
        for _ in range(3):
            await asyncio.wait_for(receiver.track.recv(), timeout=30)
        stats = service.snapshot()["sessions"][0]
        await pc.close()
        return stats

    stats = asyncio.run(run())
    assert stats["codec"] in ("VP8", "H264") and stats["frames"] >= 3 and stats["source_frames"] == 1
    assert stats["source"] == "program" and stats["fps"] == 10


@needs_aiortc
def test_no_picture_is_refused_before_any_session(service: WhepService) -> None:
    with pytest.raises(WhepError) as info:
        service.offer(OFFER, "missing", 320, 10)
    assert info.value.status == 503
    assert service.snapshot()["sessions"] == []


@needs_aiortc
def test_a_garbled_offer_is_a_400_not_a_crash(service: WhepService) -> None:
    with pytest.raises(WhepError) as info:
        service.offer("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\nthis is not sdp\r\n", "program", 320, 10)
    assert info.value.status == 400
    assert service.snapshot()["sessions"] == []


@needs_aiortc
def test_the_oldest_session_makes_room() -> None:
    svc = WhepService(lambda source, width: card("card-brb.jpg"), max_sessions=1)
    try:

        async def two() -> None:
            from aiortc import RTCPeerConnection, RTCSessionDescription

            ids = []
            pcs = []
            for _ in range(2):
                pc = RTCPeerConnection()
                pcs.append(pc)
                pc.addTransceiver("video", direction="recvonly")
                await pc.setLocalDescription(await pc.createOffer())
                loop = asyncio.get_running_loop()
                sid, answer = await loop.run_in_executor(
                    None, lambda p=pc: svc.offer(p.localDescription.sdp, "program", 320, 10)
                )
                await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
                ids.append(sid)
            assert [s["id"] for s in svc.snapshot()["sessions"]] == [ids[1]]
            for pc in pcs:
                await pc.close()

        asyncio.run(two())
    finally:
        svc.close()


def test_delete_of_an_unknown_or_malformed_id_is_false(service: WhepService) -> None:
    assert service.delete("nope") is False
    assert service.delete("a" * 12) is False


# -- the HTTP routes ----------------------------------------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[ConsoleServer]:
    srv = ConsoleServer(Rig.demo(), port=0, token="s3cret-token", allow_hosts=set())
    srv.start()
    yield srv
    srv.close()


def http_request(
    server: ConsoleServer, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    send = {"X-StreamStack-Token": server.token, **(headers or {})}
    try:
        conn.request(method, path, body=body, headers=send)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_status_route_says_whether_whep_works(server: ConsoleServer) -> None:
    status, _, body = http_request(server, "GET", "/api/whep")
    assert status == 200
    assert b'"available"' in body and b'"sessions"' in body


def test_the_routes_need_the_token(server: ConsoleServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    conn.request("POST", "/api/whep", body=OFFER.encode(), headers={"Content-Type": "application/sdp"})
    assert conn.getresponse().status == 401
    conn.close()


def test_the_offer_must_be_sdp_with_a_length(server: ConsoleServer) -> None:
    assert http_request(server, "POST", "/api/whep", b"{}", {"Content-Type": "application/json"})[0] == 415
    assert (
        http_request(server, "POST", "/api/whep", b"x" * (whep.MAX_SDP + 1), {"Content-Type": "application/sdp"})[0]
        == 413
    )
    assert http_request(server, "POST", "/api/whep", b"hello", {"Content-Type": "application/sdp"})[0] == 400
    assert http_request(server, "DELETE", "/api/whep/unknownsession")[0] == 200  # already gone is ended
    assert http_request(server, "DELETE", "/api/whep/x")[0] == 404  # not an id this service hands out


@needs_aiortc
def test_whep_over_http_creates_and_deletes_a_session(server: ConsoleServer) -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    async def run() -> None:
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        await pc.setLocalDescription(await pc.createOffer())
        loop = asyncio.get_running_loop()
        status, headers, body = await loop.run_in_executor(
            None,
            lambda: http_request(
                server,
                "POST",
                "/api/whep?source=program&width=320&fps=10",
                pc.localDescription.sdp.encode(),
                {"Content-Type": "application/sdp"},
            ),
        )
        assert status == 201, body
        assert headers["content-type"] == "application/sdp"
        assert re.fullmatch(r"/api/whep/[A-Za-z0-9_-]{8,64}", headers["location"])
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body.decode(), type="answer"))
        frame = await asyncio.wait_for(pc.getReceivers()[0].track.recv(), timeout=30)
        assert frame.width > 0
        listed = await loop.run_in_executor(None, lambda: http_request(server, "GET", "/api/whep")[2])
        assert headers["location"].rsplit("/", 1)[1].encode() in listed
        assert (await loop.run_in_executor(None, lambda: http_request(server, "DELETE", headers["location"])))[0] == 200
        assert (await loop.run_in_executor(None, lambda: http_request(server, "DELETE", headers["location"])))[0] == 200
        await pc.close()

    asyncio.run(run())


@needs_aiortc
def test_the_rtp_clock_never_goes_back_when_the_frame_schedule_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """#158: a starved host restarts the frame schedule; the presentation time (the RTP timestamp) must keep rising,
    or the browser drops every frame as stale and the picture freezes until the old timestamps are overtaken."""
    import concurrent.futures
    import itertools

    monkeypatch.setattr(whep, "RESTART_SECONDS", 0.2)
    pictures = [card("card-brb.jpg"), card("card-canon.jpg")]
    calls = {"n": 0}

    def grab() -> bytes | None:
        calls["n"] += 1
        if calls["n"] == 6:
            time.sleep(0.5)  # the host is starved for longer than RESTART_SECONDS
        return pictures[calls["n"] % 2]

    async def run() -> list[int]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            track = whep.FrameTrack.create(grab, whep.SessionStats("program", 640, 30), pool, 30)
            return [(await track.recv()).pts for _ in range(12)]

    stamps = asyncio.run(run())
    assert all(b > a for a, b in itertools.pairwise(stamps)), stamps
    assert stamps[-1] >= 0.5 * 90000  # and it kept counting real time across the stall
