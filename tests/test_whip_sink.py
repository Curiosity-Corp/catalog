"""WhipSink with a scripted session: connect and backoff policy, fatal errors, GOP join, codec change, status."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import ClassVar

import pytest
from stream_support import ablock
from test_whip_media import block, sample, track_with
from test_whip_payload import avcc, nal

from streamstack.stream import mkv
from streamstack.stream.whip import media
from streamstack.stream.whip.protocol import WhipError
from streamstack.stream.whip.session import SessionOptions, Stats
from streamstack.stream.whip.sink import WhipSink

H264 = "V_MPEG4/ISO/AVC"


class FakeSession:
    """Stands in for WhipSession: connect succeeds or raises what the script says; frames are recorded."""

    made: ClassVar[list[FakeSession]] = []
    script: ClassVar[list[BaseException | None]] = []

    def __init__(self, options: SessionOptions, video: media.VideoConfig, with_audio: bool) -> None:
        self.options, self.video_config, self.with_audio = options, video, with_audio
        self.stats = Stats(codec=video.label, rtt_ms=1.5, ice_state="completed", dtls_state="connected")
        self.keyframe_requested: Callable[[], None] = lambda: None
        self.video: list[media.Frame] = []
        self.audio: list[media.Frame] = []
        self.closed = 0
        self._dead: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        FakeSession.made.append(self)

    async def connect(self) -> None:
        step = FakeSession.script.pop(0) if FakeSession.script else None
        if step is not None:
            raise step

    def push_video(self, frame: media.Frame) -> None:
        self.video.append(frame)

    def push_audio(self, frame: media.Frame) -> None:
        self.audio.append(frame)

    async def dead(self) -> str:
        return await self._dead

    def die(self, reason: str) -> None:
        self._dead.get_loop().call_soon_threadsafe(lambda: self._dead.done() or self._dead.set_result(reason))

    async def close(self) -> None:
        self.closed += 1
        if not self._dead.done():
            self._dead.set_result("closed")


@pytest.fixture(autouse=True)
def reset() -> None:
    FakeSession.made = []
    FakeSession.script = []


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def header(codec: str = H264, audio: bool = True) -> bytes:
    video = track_with(codec, avcc() if codec == H264 else b"\x81\x00\x0c\x00")
    opus = track_with("A_OPUS", b"OpusHead" + bytes(11), kind="audio") if audio else None
    return mkv.stream_header(video, opus)


def keyframe_cluster(ts: int) -> tuple[bytes, int, bool]:
    data = sample(nal(5, 40))
    return mkv.encode_cluster([block(data, ts, True)], {1: 1, 2: 2}), 1, True


def delta_cluster(ts: int) -> tuple[bytes, int, bool]:
    data = sample(nal(1, 40))
    return mkv.encode_cluster([block(data, ts, False)], {1: 1, 2: 2}), 1, False


def audio_cluster(ts: int) -> tuple[bytes, int, bool]:
    return mkv.encode_cluster([ablock(ts)], {1: 1, 2: 2}), 0, False


@pytest.fixture
def sink() -> Callable[..., WhipSink]:
    made: list[WhipSink] = []

    def make(token: Callable[[], str] = lambda: "tok", clock: Callable[[], float] | None = None) -> WhipSink:
        events: list[tuple[str, str]] = []
        s = WhipSink(
            "w",
            "http://127.0.0.1:1/whip",
            token,
            clock=clock or time.monotonic,
            on_event=lambda kind, text: events.append((kind, text)),
            session_factory=FakeSession,
        )
        s.events = events  # type: ignore[attr-defined]
        made.append(s)
        return s

    yield make
    for s in made:
        s.shutdown()


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def feed(s: WhipSink, *parts: tuple[bytes, int, bool]) -> list[bool]:
    return [s.write(*part) for part in parts]


def test_connects_and_forwards_frames_in_order(sink) -> None:
    s = sink()
    s.open(header())
    assert s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "live")
    feed(s, delta_cluster(33), audio_cluster(20), delta_cluster(66))
    assert wait_for(lambda: len(FakeSession.made[0].video) >= 3 and FakeSession.made[0].audio)
    session = FakeSession.made[0]
    assert session.options.token == "tok" and session.with_audio
    assert [f.keyframe for f in session.video][:2] == [True, False]
    assert s.status()["whip"]["rtt_ms"] == 1.5 and s.status()["state"] == "live"


def test_frames_wait_for_the_connection_and_start_at_a_keyframe(sink) -> None:
    class Slow(FakeSession):
        async def connect(self) -> None:
            await asyncio.sleep(0.3)

    s = sink()
    s._factory = Slow  # type: ignore[assignment]
    s.open(header())
    assert s.write(*delta_cluster(0)), "a frame with nothing to anchor it is dropped quietly, not an error"
    s.write(*keyframe_cluster(33))
    s.write(*delta_cluster(66))
    assert wait_for(lambda: s.state == "live")
    assert wait_for(lambda: len(FakeSession.made[0].video) == 2)
    assert [f.keyframe for f in FakeSession.made[0].video] == [True, False]


def test_retryable_error_backs_off_then_reopens(sink) -> None:
    clock = Clock()
    FakeSession.script = [WhipError("the server is busy (503)", retry_after=0.0), None]
    s = sink(clock=clock)
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "backoff")
    assert s.restarts == 1 and "503" in s.last_error
    assert s.write(*delta_cluster(33)) is False, "the relay is told to reopen at the next keyframe"
    with pytest.raises(OSError, match="waiting"):
        s.open(header())
    clock.now += 60
    s.open(header())
    s.write(*keyframe_cluster(100))
    assert wait_for(lambda: s.state == "live")


def test_retry_after_longer_than_the_backoff_wins(sink) -> None:
    FakeSession.script = [WhipError("slow down (429)", retry_after=20.0)]
    s = sink(clock=Clock())
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "backoff")
    assert s.status()["retry_in_s"] == 20.0


@pytest.mark.parametrize("status", [401, 404, 405])
def test_fatal_errors_stop_for_good(sink, status: int) -> None:
    FakeSession.script = [WhipError(f"the server answered {status}", fatal=True)]
    s = sink()
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "failed")
    assert str(status) in s.last_error and s.restarts == 0
    with pytest.raises(OSError, match=str(status)):
        s.open(header())
    assert any(kind == "error" for kind, _ in s.events)  # type: ignore[attr-defined]
    assert len(FakeSession.made) == 1


def test_session_death_after_live_is_retried(sink) -> None:
    clock = Clock()
    s = sink(clock=clock)
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "live")
    FakeSession.made[0].die("ICE failed")
    assert wait_for(lambda: s.state == "backoff")
    assert s.last_error == "ICE failed" and s.restarts == 1
    assert FakeSession.made[0].closed >= 1


def test_a_token_source_failure_is_fatal_and_never_connects(sink) -> None:
    def broken() -> str:
        raise OSError("the secret env:X is not set")

    s = sink(token=broken)
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "failed")
    assert "env:X" in s.last_error and not FakeSession.made


def test_close_ends_the_session_and_reopen_starts_a_new_one(sink) -> None:
    s = sink()
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "live")
    s.close()
    assert FakeSession.made[0].closed >= 1 and s.state == "idle"
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: len(FakeSession.made) == 2 and s.state == "live")


def test_invalid_matroska_is_fatal(sink) -> None:
    s = sink()
    s.open(header())
    assert s.write(b"\x00\x01\x02garbage-not-ebml" * 8, 0, False) is False
    assert s.state == "failed" and "Matroska" in s.last_error


def test_a_stream_without_video_fails(sink) -> None:
    s = sink()
    s.open(mkv.stream_header(None, track_with("A_OPUS", b"", kind="audio")))
    assert s.write(*audio_cluster(0)) is False
    assert s.state == "failed" and "video" in s.last_error


def test_unsupported_codec_fails(sink) -> None:
    s = sink()
    s.open(mkv.stream_header(track_with("V_VP9", b""), None))
    s.write(*keyframe_cluster(0))
    assert s.state == "failed"


def test_codec_change_reconnects_and_same_codec_does_not(sink) -> None:
    s = sink()
    s.open(header())
    s.write(*keyframe_cluster(0))
    assert wait_for(lambda: s.state == "live")
    s.on_track(track_with(H264, avcc()))
    assert s.state == "live" and len(FakeSession.made) == 1
    s.on_track(track_with("V_AV1", b"\x81\x00\x0c\x00"))
    assert s.state == "idle"
    assert s.write(*delta_cluster(66)) is False
    assert wait_for(lambda: FakeSession.made[0].closed >= 1)


def test_status_has_the_shape_the_service_reads(sink) -> None:
    s = sink()
    status = s.status()
    for key in ("state", "restarts", "dropped_frames", "queued_bytes", "net_bytes", "last_error", "retry_in_s", "whip"):
        assert key in status
    assert status["state"] == "idle" and status["whip"] == {}
