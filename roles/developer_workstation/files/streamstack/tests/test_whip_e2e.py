"""WHIP end to end: real ffmpeg (libx264, libopus) -> StreamService -> WhipSink -> real ICE/DTLS/SRTP on 127.0.0.1
-> a real WHIP server (aiortc) that decodes what arrives.

CPU only; nothing leaves 127.0.0.1. Skipped without ffmpeg, libx264, libopus or the `whip` extra.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from whip_server import WhipServer

from streamstack.stream import proc
from streamstack.stream.ffcmd import AudioSource, Source
from streamstack.stream.service import StreamService
from streamstack.stream.whip import session as whip_session
from streamstack.stream.whip import sink as whip_sink
from streamstack.stream.whip import support

FFMPEG = shutil.which("ffmpeg")
TOKEN = "t0ken-for-the-whip-test"


def _has_encoders(*needles: str) -> bool:
    if not FFMPEG:
        return False
    text = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False).stdout
    return all(n in text for n in needles)


pytestmark = [
    pytest.mark.skipif(not _has_encoders("libx264", "libopus"), reason="ffmpeg needs libx264 and libopus"),
    pytest.mark.skipif(not support.available(), reason="the whip extra (aiortc) is not installed"),
]


def wait_for(predicate: Callable[[], bool], timeout: float = 30.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


@pytest.fixture
def server() -> Iterator[WhipServer]:
    srv = WhipServer(token=TOKEN).start()
    yield srv
    srv.stop()


@pytest.fixture
def make_service(tmp_path: Path, monkeypatch) -> Iterator[Callable[..., StreamService]]:
    monkeypatch.setenv("STREAMSTACK_WHIP_TEST_TOKEN", TOKEN)
    monkeypatch.setattr(whip_sink, "BACKOFF", (0.3, 0.3, 0.3))
    made: list[StreamService] = []

    def make() -> StreamService:
        svc = StreamService(
            {"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec")},
            source_override=Source("lavfi", width=640, height=360, fps=30),
            audio_override=AudioSource("lavfi"),
        )
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()
    assert proc.kill_all() == 0, "a whip test left a process running"


def destination(url: str, **extra: object) -> dict[str, object]:
    return {"id": "whip-local", "kind": "whip", "url": url, "key_ref": "env:STREAMSTACK_WHIP_TEST_TOKEN", **extra}


def dest_state(svc: StreamService) -> dict:
    return svc.state()["destinations"][0]


def test_media_arrives_decoded_and_stop_deletes_the_session(make_service, server: WhipServer) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264", "bitrate_kbps": 1500, "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "live"), svc.state()["events"]
    assert wait_for(lambda: bool(server.sessions) and server.sessions[0].received.video_frames > 45)
    assert wait_for(lambda: server.sessions[0].received.audio_frames > 50)
    got = server.sessions[0].received
    assert (got.width, got.height) == (640, 360)
    assert got.luma_levels > 20, "the decoded picture is flat: no test pattern came through"
    assert got.audio_peak > 100, "the decoded audio is silent"

    state = dest_state(svc)
    assert state["codec"] == "h264"
    whip = state["whip"]
    assert whip["ice_state"] == "completed" and whip["dtls_state"] == "connected"
    assert wait_for(
        lambda: (dest_state(svc)["whip"]["rtt_ms"] is not None) and dest_state(svc)["whip"]["bitrate_kbps"] > 100
    )
    assert TOKEN not in json.dumps(svc.state()), "the bearer token leaked into the state"
    stats = server.stats(server.sessions[0])
    assert stats["packets"] > 100 and stats["lost"] <= 2  # loopback: nothing should be lost

    method, _, headers = server.requests[0]
    assert method == "POST" and headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["content-type"] == "application/sdp"

    svc.stop_stream()
    assert wait_for(lambda: server.sessions[0].deleted), "stop did not DELETE the WHIP resource"
    assert [m for m, _, _ in server.requests].count("DELETE") == 1
    assert svc.state()["status"] == "idle"


def test_hot_swapping_the_bitrate_keeps_the_session(make_service, server: WhipServer) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264", "bitrate_kbps": 1500, "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "live")
    assert wait_for(lambda: server.sessions[0].received.video_frames > 30)
    before = server.sessions[0].received.video_frames
    svc.set({"bitrate_kbps": 700, "keyint_s": 1})
    assert wait_for(lambda: (svc.state()["last_swap"] or {}).get("ok") is True)
    assert wait_for(lambda: server.sessions[0].received.video_frames > before + 45)
    assert len(server.sessions) == 1 and dest_state(svc)["restarts"] == 0, "a bitrate change must not renegotiate"
    svc.stop_stream()


def test_a_tier_change_keeps_publishing_at_the_new_size(make_service, server: WhipServer) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "live")
    assert wait_for(lambda: server.sessions[0].received.video_frames > 20)
    svc.set({"tier": "480p"})
    assert wait_for(lambda: (854, 480) in [s for sess in server.sessions for s in sess.received.sizes], 40), (
        svc.state()["events"]
    )
    assert wait_for(lambda: dest_state(svc)["state"] == "live")
    svc.stop_stream()


def test_reconnects_after_the_server_ends_the_session(make_service, server: WhipServer) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264", "keyint_s": 1, "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "live")
    assert wait_for(lambda: server.sessions[0].received.video_frames > 20)
    first = server.sessions[0]
    server.drop(first)
    assert wait_for(lambda: len(server.sessions) == 2, 40), svc.state()["events"]
    second = server.sessions[1]
    assert wait_for(lambda: second.received.video_frames > 30 and dest_state(svc)["state"] == "live", 40)
    assert dest_state(svc)["restarts"] >= 1
    assert second.received.luma_levels > 20
    svc.stop_stream()
    assert wait_for(lambda: second.deleted)


def test_reconnects_after_a_server_error_with_backoff(make_service, server: WhipServer) -> None:
    server.fail_with = [503, 429]
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "live", 60), svc.state()["events"]
    posts = [m for m, _, _ in server.requests if m == "POST"]
    assert len(posts) == 3
    svc.stop_stream()


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        ("401", "401"),
        ("404", "404"),
        ("405", "405"),
    ],
)
def test_client_errors_fail_without_retrying(make_service, server: WhipServer, setup: str, needle: str) -> None:
    if setup == "401":
        server.token = "a-different-token"
    elif setup == "404":
        server.path = "/elsewhere"
    else:
        server.fail_with = [405] * 20
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [destination(server.url.replace("/elsewhere", "/whip"))]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "failed", 30), (dest_state(svc), svc.state()["events"])
    assert needle in dest_state(svc)["last_error"]
    assert TOKEN not in json.dumps(svc.state())
    time.sleep(2.5)
    assert len([m for m, _, _ in server.requests if m == "POST"]) == 1, "a client error must not be retried"
    svc.stop_stream()


def test_ice_failure_is_retried_and_reported(make_service, server: WhipServer, monkeypatch) -> None:
    """The answer carries an ICE password that does not match, so the connectivity checks are refused."""
    server.break_ice = True
    monkeypatch.setattr(whip_session, "ICE_TIMEOUT", 3.0)
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["restarts"] >= 1, 60), (dest_state(svc), svc.state()["events"])
    assert "ICE" in dest_state(svc)["last_error"]
    assert dest_state(svc)["state"] != "failed", "an ICE failure is transient and must be retried"
    assert wait_for(lambda: len(server.sessions) >= 2), "no second attempt"
    svc.stop_stream()


def test_a_server_that_goes_silent_is_noticed_and_replaced(make_service, server: WhipServer, monkeypatch) -> None:
    monkeypatch.setattr(whip_session, "RTCP_SILENCE", 3.0)
    svc = make_service()
    svc.set({"encoder": "libx264", "keyint_s": 1, "destinations": [destination(server.url)]})
    svc.start_stream()
    assert wait_for(lambda: dest_state(svc)["state"] == "live")
    assert wait_for(
        lambda: (dest_state(svc)["whip"]["rtcp_age_s"] or 99) < 3 and server.sessions[0].received.video_frames > 30
    )
    server.go_silent(server.sessions[0])
    assert wait_for(lambda: len(server.sessions) == 2, 60), (dest_state(svc), svc.state()["events"])
    assert wait_for(lambda: server.sessions[1].received.video_frames > 20 and dest_state(svc)["state"] == "live", 40)
    svc.stop_stream()
