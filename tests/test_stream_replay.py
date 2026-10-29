"""Instant replay (stream/replay.py): bounded memory, clips that start on a keyframe, and a real clip from ffmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
import tracemalloc
from pathlib import Path

import pytest
from stream_support import vblock, video_track

from streamstack.stream import mkv
from streamstack.stream.relay import Chain
from streamstack.stream.replay import MAX_SECONDS, MIN_SECONDS, ReplayError, ReplaySink

HEADER = mkv.stream_header(video_track(), None)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def cluster(ts: int, size: int, key: bool = False) -> bytes:
    return mkv.encode_cluster([vblock(ts, key, size)], {mkv.TRACK_VIDEO: 1, mkv.TRACK_AUDIO: 2})


def feed(sink: ReplaySink, clock: Clock, seconds: int, kbps: int, gop_s: int = 2, fps: int = 10) -> None:
    """Deliver `seconds` of a stream at `kbps` with a keyframe every `gop_s` seconds, one write per frame."""
    per_frame = kbps * 1000 // 8 // fps
    for i in range(seconds * fps):
        key = i % (gop_s * fps) == 0
        sink.write(cluster(i * 100, per_frame, key), 1, key)
        clock.now += 1 / fps


def armed(seconds: int = 20, max_mb: int = 64) -> tuple[ReplaySink, Clock]:
    clock = Clock()
    sink = ReplaySink("replay", seconds, max_mb * 1024 * 1024, clock=clock)
    sink.open(HEADER)
    return sink, clock


def test_the_buffer_holds_about_the_window_and_no_more() -> None:
    sink, clock = armed(seconds=20)
    feed(sink, clock, 120, kbps=4000)
    stats = sink.stats()
    assert 20 <= stats["depth_s"] <= 22 + 0.5  # the window plus at most one keyframe interval
    assert stats["bytes"] < 4000 * 1000 / 8 * 24  # about 23 s of 4 Mbit/s, not 120 s


def test_memory_is_bounded_by_the_cap_even_when_the_window_would_need_more() -> None:
    sink, clock = armed(seconds=60, max_mb=8)
    feed(sink, clock, 200, kbps=20000)  # 60 s at 20 Mbit/s is 150 MB: far over the 8 MB cap
    stats = sink.stats()
    assert stats["max_bytes"] == 8 * 1024 * 1024
    assert (
        stats["bytes"] <= stats["max_bytes"]
        and stats["peak_bytes"] <= stats["max_bytes"] + 20000 * 1000 // 8 // 10 * 21
    )
    assert stats["evicted_for_memory"] > 0 and stats["depth_s"] < 60  # depth was given up, not memory


def test_measured_allocation_matches_the_reported_bytes() -> None:
    sink, clock = armed(seconds=10, max_mb=64)
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        feed(sink, clock, 100, kbps=20000)  # ten windows of a 20 Mbit/s stream
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    reported = int(sink.stats()["bytes"])
    assert reported < 20000 * 1000 / 8 * 13  # about 12 s of video, not 100 s
    assert held < reported * 1.25 + 1_000_000  # what Python really holds is within a quarter of what we report


def test_a_clip_is_at_least_as_long_as_asked_and_starts_on_a_keyframe(tmp_path: Path) -> None:
    sink, clock = armed(seconds=30)
    feed(sink, clock, 60, kbps=2000, gop_s=2)
    clip = sink.save(tmp_path / "clip.mkv", seconds=10)
    assert 10 <= clip["seconds"] <= 12.5 and clip["short"] is False
    demux = mkv.Demuxer()
    data = (tmp_path / "clip.mkv").read_bytes()
    blocks = [e for e in demux.feed(data) if isinstance(e, mkv.Block)]
    assert blocks and blocks[0].keyframe  # a player can start at the first picture
    assert data.startswith(HEADER[:4])


def test_a_young_buffer_gives_a_shorter_clip_and_says_so(tmp_path: Path) -> None:
    sink, clock = armed(seconds=30)
    feed(sink, clock, 12, kbps=2000)
    clip = sink.save(tmp_path / "short.mkv", seconds=25)
    assert clip["short"] is True and clip["seconds"] < 25


def test_save_refuses_without_a_keyframe_and_for_bad_lengths(tmp_path: Path) -> None:
    sink, clock = armed(seconds=30)
    with pytest.raises(ReplayError, match="Nothing is buffered"):
        sink.save(tmp_path / "x.mkv")
    sink.write(cluster(0, 100, key=False), 1, False)  # a delta frame cannot start a clip
    with pytest.raises(ReplayError, match="Nothing is buffered"):
        sink.save(tmp_path / "x.mkv")
    feed(sink, clock, 10, kbps=1000)
    for bad in (MIN_SECONDS - 1, 31):
        with pytest.raises(ReplayError, match="A clip can be from") as err:
            sink.save(tmp_path / "x.mkv", seconds=bad)
        assert err.value.status == 400
    assert not list(tmp_path.glob("*.mkv"))


def test_a_new_header_starts_a_fresh_buffer() -> None:
    sink, clock = armed(seconds=30)
    feed(sink, clock, 10, kbps=1000)
    sink.open(mkv.stream_header(video_track(1920, 1080), None))  # the picture format changed
    assert sink.stats()["groups"] == 0 and sink.stats()["bytes"] == len(sink._header)


def test_shutdown_frees_the_memory() -> None:
    sink, clock = armed()
    feed(sink, clock, 10, kbps=4000)
    sink.shutdown()
    stats = sink.stats()
    assert stats["bytes"] == 0 and stats["groups"] == 0 and stats["active"] is False
    assert sink.write(b"x", 1, True) is False


def test_unwritable_target_is_reported_and_leaves_nothing(tmp_path: Path) -> None:
    sink, clock = armed()
    feed(sink, clock, 10, kbps=1000)
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    with pytest.raises(ReplayError, match="Cannot write"):
        sink.save(blocker / "clip.mkv", seconds=5)


@pytest.mark.parametrize(("seconds", "cap"), [(MIN_SECONDS - 1, 1 << 30), (MAX_SECONDS + 1, 1 << 30), (30, 1000)])
def test_arming_validates_length_and_memory(seconds: int, cap: int) -> None:
    with pytest.raises(ReplayError) as err:
        ReplaySink("replay", seconds, cap)
    assert err.value.status == 400


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg and ffprobe are needed")
def test_a_clip_cut_from_a_real_encode_has_the_requested_length(tmp_path: Path) -> None:
    """Real H.264 with a keyframe every 2 s, fed through the relay's own clustering; the clip is probed."""
    assert FFMPEG and FFPROBE
    source = tmp_path / "source.mkv"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10",
            "-t",
            "40",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "20",
            "-keyint_min",
            "20",
            "-sc_threshold",
            "0",
            str(source),
        ],
        check=True,
        timeout=120,
    )
    events = mkv.Demuxer().feed(source.read_bytes())
    header = next(e for e in events if isinstance(e, mkv.Header))
    blocks = [e for e in events if isinstance(e, mkv.Block)]
    assert sum(b.keyframe for b in blocks) == 20
    clock = Clock()
    sink = ReplaySink("replay", 30, 64 * 1024 * 1024, clock=clock)
    sink.open(mkv.stream_header(header.track("video"), None))
    group: list[mkv.Block] = []
    for block in [*blocks, None]:
        if block is None or (block.keyframe and group):
            sink.write(Chain._cluster(group), len(group), True)
            clock.now += len(group) / 10
            group = []
        if block is not None:
            group.append(block)
    out = tmp_path / "replay.mkv"
    clip = sink.save(out, seconds=10, ffmpeg=FFMPEG)
    probe = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames:format=duration",
            "-of",
            "json",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    info = json.loads(probe.stdout)
    duration = float(info["format"]["duration"])
    assert 10 <= duration <= 12.2, duration  # the length asked for, plus at most one keyframe interval
    assert int(info["streams"][0]["nb_read_frames"]) == pytest.approx(duration * 10, abs=2)
    assert clip["bytes"] == out.stat().st_size and not list(tmp_path.glob(".*.part"))


# -- the service and the Console ---------------------------------------------------------------------------------


def test_service_arms_feeds_saves_and_stops_the_buffer(tmp_path: Path) -> None:
    from test_stream_service import Env, start_with_audio

    from streamstack.stream.service import StreamError

    env = Env(tmp_path)
    assert "keeping the last 10 s" in start_with_audio(env, lambda: env.svc.start_replay(10))
    video = env.last("video-av1")
    env.feed(video, 0, 12000, key_at=set(range(0, 12000, 1000)))
    env.audio(0, 12000)
    state = env.svc.state()
    assert state["status"] == "replay" and state["replay"]["active"] and state["recording"]["active"] is False
    assert state["replay"]["bytes"] > 0 and state["replay"]["max_bytes"] == 512 * 1024 * 1024
    assert env.svc.start_replay(10) == "Instant replay is already armed."
    with pytest.raises(StreamError, match="not streaming"):  # replay is not a live destination: stop_stream skips it
        env.svc.stop_stream("replay")
    assert env.svc.stop_stream() == "Not streaming." and env.svc.state()["replay"]["active"]
    saved = env.svc.save_replay(5, "Goal")
    assert Path(str(saved["file"])).parent == tmp_path / "rec" and "Goal" in str(saved["file"])
    assert Path(str(saved["file"])).stat().st_size > 1000
    with pytest.raises(StreamError, match="A clip can be from") as err:
        env.svc.save_replay(11)
    assert err.value.status == 400
    assert "freed" in env.svc.stop_replay()
    after = env.svc.state()
    assert after["status"] == "idle" and after["replay"]["active"] is False and after["replay"]["bytes"] == 0
    with pytest.raises(StreamError, match="not armed"):
        env.svc.save_replay()


def test_service_refuses_a_length_that_cannot_fit_in_memory(tmp_path: Path) -> None:
    from test_stream_service import Env

    from streamstack.stream.service import StreamError

    env = Env(tmp_path, STREAMSTACK_REPLAY_MAX_MB="4")
    with pytest.raises(StreamError, match="STREAMSTACK_REPLAY_MAX_MB") as err:
        env.svc.start_replay(300)
    assert err.value.status == 400 and env.children == []  # refused before anything was started
    with pytest.raises(StreamError, match="whole number"):
        env.svc.start_replay("soon")  # type: ignore[arg-type]


def test_replay_runs_alongside_a_recording_and_leaves_it_running(tmp_path: Path) -> None:
    from test_stream_service import Env, start_with_audio

    env = Env(tmp_path)
    start_with_audio(env, lambda: env.svc.start_record("mkv", "show"))
    env.svc.start_replay(10)
    env.feed(env.last("video-av1"), 0, 3000, key_at={0, 1000, 2000})
    state = env.svc.state()
    assert state["status"] == "recording" and state["recording"]["active"] and state["replay"]["active"]
    env.svc.stop_replay()
    assert env.svc.state()["recording"]["active"]
    env.svc.stop_record()
    assert env.svc.state()["status"] == "idle"


def test_demo_replay_follows_the_same_surface_and_schema() -> None:
    from streamstack.console.errors import ActionError
    from streamstack.console.stream import DemoStream

    demo = DemoStream()
    idle = demo.state()["replay"]
    assert idle["active"] is False
    with pytest.raises(ActionError, match="not armed") as err:
        demo.act("replay.save", {})
    assert err.value.status == 409
    for bad in ({"seconds": 1}, {"seconds": "9"}, {"seconds": 5.5}):
        with pytest.raises(ActionError) as bad_err:
            demo.act("replay.start", bad)
        assert bad_err.value.status == 400
    assert "keeping the last 20 s" in str(demo.act("replay.start", {"seconds": 20}))
    armed_state = demo.state()
    assert armed_state["status"] == "replay" and set(armed_state["replay"]) == set(idle)
    assert "Replay-1.mkv" in str(demo.act("replay.save", {"seconds": 10}))
    with pytest.raises(ActionError, match="A clip can be from"):
        demo.act("replay.save", {"seconds": 40})
    demo.act("replay.stop", {})
    assert demo.state()["status"] == "idle"


def test_the_rig_routes_replay_actions() -> None:
    from streamstack.console.rig import Rig

    rig = Rig.demo()
    rig.start()
    try:
        assert "keeping" in str(rig.act("replay.start", {"seconds": 10}))
        assert rig.snapshot()["stream"]["replay"]["active"]
    finally:
        rig.stop()
