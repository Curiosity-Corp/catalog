from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from stream_support import ablock, audio_track, vblock, video_track

from streamstack.stream import probe, service
from streamstack.stream.ffcmd import AudioSource, Source
from streamstack.stream.service import StreamError, StreamService


class FakeChild:
    """Stands in for `proc.Child`: records what was asked and lets the test play the process's output."""

    def __init__(self, name: str, argv: list[str], **kwargs: Any) -> None:
        self.name, self.argv, self.kwargs = name, argv, kwargs
        self.started = False
        self.stopped = False
        self.stopping = False
        self.error = ""
        self.written = b""
        self.sent_header = False
        self.pid: int | None = None

    def start(self) -> None:
        self.started = True

    def stop(self, grace: float = 1.0) -> int:
        self.stopped = self.stopping = True
        return 0

    def write(self, data: bytes) -> None:
        self.written += data

    @property
    def alive(self) -> bool:
        return self.started and not self.stopped

    def last_error(self) -> str:
        return self.error

    def header(self) -> bytes:
        """The Matroska header this process would write first (track layout follows its command line)."""
        from streamstack.stream import mkv

        if self.name.startswith("audio"):
            return mkv.stream_header(None, audio_track())
        codec = {"av1": "V_AV1", "hevc": "V_MPEGH/ISO/HEVC", "h264": "V_MPEG4/ISO/AVC"}[self.name.split("-")[1]]
        width, height = 1280, 720
        match = re.search(r"w=(\d+):h=(\d+)", " ".join(self.argv))
        if match:
            width, height = int(match.group(1)), int(match.group(2))
        return mkv.stream_header(video_track(width, height, codec), None)

    def emit(self, data: bytes) -> None:
        if not self.sent_header:
            self.sent_header = True
            self.kwargs["on_stdout"](self.header())
        if data:
            self.kwargs["on_stdout"](data)

    def exit(self, code: int = 1, message: str = "") -> None:
        self.error = message
        self.kwargs["on_exit"](code)


class Wall:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


MUXERS = " E mp4 MP4\n E flv FLV\n E matroska Matroska\n"
ENCODERS = " V....D av1_vaapi x\n V....D hevc_vaapi x\n V....D h264_vaapi x\n V....D libx264 x\n V....D libx265 x\n"


def runner(argv: list[str], timeout: float) -> tuple[int, str]:
    if "-version" in argv:
        return 0, "ffmpeg version 8.0.1"
    if "-encoders" in argv:
        return 0, ENCODERS
    if "-protocols" in argv:
        return 0, "Output:\n file\n rtmp\n srt\n tls\n"
    if "-muxers" in argv:
        return 0, MUXERS
    if "protocol=rtmp" in argv:
        return 0, "-rtmp_enhanced_codecs"
    return 0, ""


class Env:
    def __init__(self, tmp_path: Path, source: Source | None = None, **cfg: str) -> None:
        self.wall = Wall()
        self.children: list[FakeChild] = []
        self.events: list[tuple[str, str]] = []
        self.whip_reason = ""  # what the WHIP probe says: empty means this host can publish over WHIP
        self.svc = StreamService(
            {"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec"), **cfg},
            probe=probe.ProbeCache(
                lambda: probe.probe_host(
                    "ffmpeg", runner, nodes=["/dev/null"], cores=16, whip_check=lambda: self.whip_reason
                )
            ),
            notify=lambda level, text: self.events.append((level, text)),
            spawn=self.spawn,
            wall=self.wall,
            state_path=tmp_path / "stream.json",
            source_override=source or Source("lavfi", width=1280, height=720, fps=30),
            audio_override=AudioSource("lavfi"),
        )

    def spawn(self, name: str, argv: list[str], **kwargs: Any) -> FakeChild:
        child = FakeChild(name, argv, **kwargs)
        self.children.append(child)
        return child

    def named(self, prefix: str) -> list[FakeChild]:
        return [c for c in self.children if c.name.startswith(prefix)]

    def last(self, prefix: str) -> FakeChild:
        return self.named(prefix)[-1]

    def audio_up(self, wait: bool = False) -> None:
        """Give the audio encoder's header (the service waits for it before starting video)."""
        self.last("audio").emit(b"")

    def feed(
        self, child: FakeChild, start_ms: int, end_ms: int, key_at: set[int] | None = None, step: int = 33
    ) -> None:
        for ts in range(start_ms, end_ms, step):
            self.wall.now = 1_000_000.0 + ts / 1000.0 + 0.02
            child.emit(source_stream_blocks([vblock(ts, key=ts in (key_at or set()) or ts == start_ms)]))

    def audio(self, start_ms: int, end_ms: int) -> None:
        for ts in range(start_ms, end_ms, 20):
            self.last("audio").emit(source_stream_blocks([ablock(ts)]))


def source_stream_blocks(blocks: list) -> bytes:
    from streamstack.stream import mkv

    return b"".join(mkv.encode_cluster([b], {1: 1, 2: 1}) for b in blocks)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(service, "AUDIO_HEADER_WAIT_S", 0.05)
    return Env(tmp_path)


def start_with_audio(env: Env, action: Callable[[], str]) -> str:
    """Run a start action; the audio header arrives while it waits."""
    import threading

    result: list[str] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(action())
        except BaseException as exc:  # relayed to the test thread
            error.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 3
    while not env.named("audio") and time.monotonic() < deadline and thread.is_alive():
        time.sleep(0.005)
    if env.named("audio"):
        env.audio_up()
    thread.join(5)
    if error:
        raise error[0]
    return result[0]


# -- starting and stopping --------------------------------------------------------------------------------------


def test_nothing_to_stream_without_a_destination(env: Env) -> None:
    with pytest.raises(StreamError, match="Add a destination") as err:
        env.svc.start_stream()
    assert err.value.status == 400
    assert env.children == []


def test_record_starts_audio_then_video_and_writes_a_file(env: Env, tmp_path: Path) -> None:
    message = start_with_audio(env, lambda: env.svc.start_record("mkv", "My Show!"))
    assert "Recording to" in message and "My-Show" in message
    video = env.last("video-av1")  # the best codec on this probe, on the GPU
    assert "av1_vaapi" in video.argv
    env.feed(video, 0, 700)
    env.audio(0, 700)
    state = env.svc.state()
    assert state["status"] == "recording" and state["recording"]["active"] and state["encoder"] == "av1_vaapi"
    path = Path(state["recording"]["file"])
    assert path.parent == tmp_path / "rec" and path.suffix == ".mkv" and path.stat().st_size > 1000
    env.svc.stop_record()
    assert video.stopped and env.last("audio").stopped
    assert env.svc.state()["status"] == "idle" and env.svc.state()["encoders"] == []


def test_mp4_record_goes_through_an_ffmpeg_sink(env: Env) -> None:
    start_with_audio(env, lambda: env.svc.start_record("mp4", "clip"))
    env.feed(env.last("video-av1"), 0, 300)
    env.audio(0, 300)
    sink = env.last("sink-record")
    assert "mp4" in sink.argv and sink.argv[-1].endswith(".mp4") and sink.written.startswith(b"\x1aE\xdf\xa3")


def test_unknown_record_format_is_rejected(env: Env) -> None:
    with pytest.raises(StreamError):
        env.svc.start_record("avi")


def test_a_transport_this_host_lacks_refuses_to_start_with_the_reason(env: Env) -> None:
    env.whip_reason = "aiortc is not installed (install the whip extra: pip install 'streamstack[whip]')"
    env.svc.set({"destinations": [{"id": "w", "kind": "whip", "url": "https://w.test/x"}]})
    with pytest.raises(StreamError, match="aiortc is not installed"):
        env.svc.start_stream()
    assert env.children == []


def test_start_stream_assigns_codecs_per_destination(env: Env) -> None:
    env.svc.set(
        {
            "destinations": [
                {"id": "a", "kind": "srt", "url": "srt://h.test:9000"},
                {"id": "b", "kind": "rtmp", "url": "rtmp://h.test/app", "key_ref": "env:K"},
            ]
        }
    )
    start_with_audio(env, env.svc.start_stream)
    states = {d["id"]: d for d in env.svc.state()["destinations"]}
    assert states["a"]["codec"] == "hevc" and states["b"]["codec"] == "av1"
    assert {e["codec"] for e in env.svc.state()["encoders"]} == {"hevc", "av1"}
    env.svc.stop_stream("a")
    assert [e["codec"] for e in env.svc.state()["encoders"]] == ["av1"]  # the HEVC encode ended with its destination
    env.svc.stop_stream()
    assert env.svc.state()["status"] == "idle"


def test_stop_of_something_not_running_is_an_error(env: Env) -> None:
    assert env.svc.stop_stream() == "Not streaming."
    with pytest.raises(StreamError):
        env.svc.stop_stream("nope")
    assert env.svc.stop_record() == "Not recording."


def test_a_running_destination_cannot_be_edited_or_removed(env: Env) -> None:
    dest = {"id": "a", "kind": "srt", "url": "srt://h.test:9000"}
    env.svc.set({"destinations": [dest]})
    start_with_audio(env, env.svc.start_stream)
    with pytest.raises(StreamError, match="stop it before removing"):
        env.svc.set({"destinations": []})
    with pytest.raises(StreamError, match="stop it before changing"):
        env.svc.set({"destinations": [{**dest, "url": "srt://other.test:9000"}]})
    env.svc.set({"destinations": [dest, {"id": "b", "kind": "record"}]})  # adding is fine
    env.svc.shutdown()


def test_key_values_never_reach_the_state_or_the_argv_of_other_children(env: Env, monkeypatch) -> None:
    monkeypatch.setenv("SS_KEY", "hunter2-secret")
    env.svc.set({"destinations": [{"id": "b", "kind": "rtmp", "url": "rtmp://h.test/app", "key_ref": "env:SS_KEY"}]})
    start_with_audio(env, env.svc.start_stream)
    env.feed(env.last("video-av1"), 0, 400)
    env.audio(0, 400)
    assert "hunter2-secret" not in repr(env.svc.state())
    sink = env.last("sink-b")
    assert sink.argv[-1].endswith("/hunter2-secret")  # the one place it must be: the sink's own argv
    assert sink.kwargs["redact"]("push hunter2-secret ok") == "push *** ok"
    assert not any("hunter2" in " ".join(c.argv) for c in env.children if c is not sink)
    env.svc.shutdown()


def test_an_unresolvable_key_is_reported_not_leaked(env: Env) -> None:
    env.svc.set({"destinations": [{"id": "b", "kind": "rtmp", "url": "rtmp://h.test/app", "key_ref": "env:NOPE_X"}]})
    start_with_audio(env, env.svc.start_stream)
    env.feed(env.last("video-av1"), 0, 400)
    env.audio(0, 400)
    dest = env.svc.state()["destinations"][0]
    assert dest["state"] == "backoff" and "NOPE_X" in dest["last_error"]
    env.svc.shutdown()


def test_no_audio_means_video_only_with_a_warning(env: Env) -> None:
    env.svc.set({"destinations": [{"id": "a", "kind": "record"}]})
    env.svc.start_record("mkv", "silent")  # nobody gives the audio encoder a header
    assert not env.svc.state()["audio"]["ok"]
    assert any("without audio" in text for _, text in env.events)
    env.svc.shutdown()


# -- settings ----------------------------------------------------------------------------------------------------


def test_settings_persist_and_a_damaged_file_starts_clean(env: Env, tmp_path: Path) -> None:
    env.svc.set({"bitrate_kbps": 4321, "destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000"}]})
    again = StreamService({}, state_path=tmp_path / "stream.json")
    assert again.settings.bitrate_kbps == 4321 and [d.id for d in again.destinations] == ["a"]
    (tmp_path / "stream.json").write_text("{not json")
    broken = StreamService({}, state_path=tmp_path / "stream.json")
    assert broken.settings.bitrate_kbps == 0 and broken.destinations == []


def test_bad_settings_are_400s_and_change_nothing(env: Env) -> None:
    with pytest.raises(StreamError) as err:
        env.svc.set({"bitrate_kbps": 5})
    assert err.value.status == 400
    assert env.svc.settings.bitrate_kbps == 0
    with pytest.raises(StreamError, match="cannot be used"):
        env.svc.set({"encoder": "av1_nvenc"})


def test_set_while_idle_only_saves(env: Env) -> None:
    assert "applies when the stream starts" in env.svc.set({"tier": "720p"})
    assert env.children == []


# -- live changes ------------------------------------------------------------------------------------------------


def running_record(env: Env, codec: str = "av1") -> FakeChild:
    start_with_audio(env, lambda: env.svc.start_record("mkv", "live"))
    video = env.last(f"video-{codec}")
    env.feed(video, 0, 1000, key_at={0, 600})
    env.audio(0, 1000)
    return video


def test_a_bitrate_change_starts_a_second_encoder_and_cuts_over_at_a_keyframe(env: Env) -> None:
    old = running_record(env)
    message = env.svc.set({"bitrate_kbps": 2000})
    assert "stay connected" in message
    new = env.last("video-av1")
    assert new is not old and "-force_key_frames" in new.argv and "2000k" in new.argv
    cut_ms = float(new.argv[new.argv.index("-force_key_frames") + 1].split("t,")[1].split("+")[0]) * 1000
    assert cut_ms > 1000  # planned ahead of "now"
    assert env.svc.state()["encoders"][0]["swapping"]
    # both encoders keep delivering on the shared clock; the new one keys at the cut
    key = round(cut_ms)
    env.feed(new, key - 330, key + 400, key_at={key})
    env.feed(old, 1000, key + 400)
    env.audio(1000, key + 400)
    env.svc.tick()
    state = env.svc.state()
    assert state["last_swap"]["ok"] is True and not state["encoders"][0]["swapping"]
    assert old.stopped and not new.stopped
    assert env.svc.state()["recording"]["active"]
    assert env.svc.settings.bitrate_kbps == 2000


def test_a_late_swap_is_retried_once_then_abandoned_and_the_old_encoder_keeps_going(env: Env) -> None:
    old = running_record(env)
    env.svc.set({"bitrate_kbps": 2000})
    first = env.last("video-av1")
    env.wall.now += 5.0  # past the relay's swap timeout; the new encoder never produced anything
    env.svc.tick()
    second = env.last("video-av1")
    assert second is not first and any("retrying with more headroom" in text for _, text in env.events)
    cut = lambda child: float(child.argv[child.argv.index("-force_key_frames") + 1].split("t,")[1].split("+")[0])  # noqa: E731
    assert cut(second) - (env.wall.now - 1_000_000.0) > 1.4  # a longer lead than the first attempt had
    env.wall.now += 5.0
    env.svc.tick()
    state = env.svc.state()
    assert state["last_swap"]["ok"] is False and not state["encoders"][0]["swapping"]
    assert first.stopped or second.stopped
    env.feed(old, 1000, 1300)
    assert not old.stopped
    assert any("could not be applied" in text for _, text in env.events)


def test_an_exclusive_capture_device_swaps_by_stopping_the_old_encoder_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "AUDIO_HEADER_WAIT_S", 0.05)
    monkeypatch.setattr(service, "source_info", lambda device: {"multi_reader": False})
    env = Env(tmp_path, source=Source("v4l2", "/dev/video9", 1280, 720, 30))
    old = running_record(env)
    env.svc.set({"rate_control": "vbr"})
    new = env.last("video-av1")
    assert old.stopped and new is not old and "-force_key_frames" not in new.argv
    env.feed(new, 1200, 1600)
    env.svc.tick()
    assert env.svc.state()["last_swap"]["ok"] is True


def test_an_audio_bitrate_change_swaps_the_audio_encoder_without_touching_video(env: Env) -> None:
    video = running_record(env)
    old_audio = env.last("audio")
    message = env.svc.set({"audio_bitrate_kbps": 96})
    new_audio = env.last("audio")
    assert "Audio bitrate changed live" in message and new_audio is not old_audio and "96k" in new_audio.argv
    new_audio.emit(source_stream_blocks([ablock(500)]))  # overlaps what the old one delivered: ignored
    assert not old_audio.stopped
    new_audio.emit(source_stream_blocks([ablock(1010)]))
    assert env.svc.state()["audio"]["bitrate_kbps"] == 96
    assert old_audio.stopped and not video.stopped


def test_a_codec_change_moves_destinations_to_the_new_chain(env: Env) -> None:
    env.svc.set({"destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000"}]})
    start_with_audio(env, env.svc.start_stream)
    assert env.svc.state()["destinations"][0]["codec"] == "hevc"
    first = env.last("video-hevc")
    env.svc.set({"codec": "h264"})
    assert env.svc.state()["destinations"][0]["codec"] == "h264"
    assert first.stopped and env.last("video-h264").started
    assert [e["codec"] for e in env.svc.state()["encoders"]] == ["h264"]
    env.svc.shutdown()


def test_pinning_an_encoder_chooses_its_codec(env: Env) -> None:
    env.svc.set({"encoder": "libx264"})
    start_with_audio(env, lambda: env.svc.start_record("mkv", "pin"))
    assert "libx264" in env.last("video-h264").argv
    env.svc.shutdown()


# -- supervision -------------------------------------------------------------------------------------------------


def crash(env: Env, message: str = "boom") -> None:
    env.last("video").exit(1, message)
    env.wall.now += 10
    env.svc.tick()  # notices, schedules the restart
    env.wall.now += 10
    env.svc.tick()  # restarts


def test_a_crashed_encoder_is_restarted_with_backoff_then_replaced(env: Env) -> None:
    env.svc.set({"encoder": "h264_vaapi"})
    first = running_record(env, "h264")
    spawned = len(env.named("video"))
    first.exit(1, "Device or resource busy")
    env.svc.tick()
    assert len(env.named("video")) == spawned  # waiting out the backoff
    env.wall.now += 1.1
    env.svc.tick()
    assert len(env.named("video")) == spawned + 1
    names = []
    for _ in range(5):  # it keeps dying right away: after three fast failures the next encoder is tried
        crash(env)
        names.append(env.svc.state()["encoders"][0]["encoder"])
    assert names[0] == "h264_vaapi" and names[-1] == "libx264"
    assert any("keeps failing" in text for _, text in env.events)
    env.svc.shutdown()


def test_an_encoder_with_no_alternative_ends_in_a_clear_error(env: Env) -> None:
    running_record(env)  # AV1 has one encoder on this probe
    for _ in range(5):
        crash(env, "no device")
    state = env.svc.state()
    assert state["status"] == "error" and "No AV1 encoder can run here" in state["encoders"][0]["failed"]
    assert any(level == "error" for level, _ in env.events)
    env.svc.shutdown()


def test_enhanced_rtmp_that_never_connects_falls_back_to_classic_h264(env: Env) -> None:
    env.svc.set({"destinations": [{"id": "r", "kind": "rtmp", "url": "rtmp://h.test/app"}]})
    start_with_audio(env, env.svc.start_stream)
    assert env.svc.state()["destinations"][0]["codec"] == "av1"
    active = env.svc._active["r"]
    active.sink.restarts = 2  # two connection attempts failed and it never went live
    env.svc.tick()
    state = env.svc.state()
    assert state["destinations"][0]["codec"] == "h264" and state["destinations"][0]["moved"]
    assert [e["codec"] for e in state["encoders"]] == ["h264"]
    assert any("falling back to classic H.264" in text for _, text in env.events)
    env.svc.shutdown()


def test_forced_enhanced_rtmp_does_not_fall_back(env: Env) -> None:
    env.svc.set(
        {"destinations": [{"id": "r", "kind": "rtmp", "url": "rtmp://h.test/app", "options": {"mode": "enhanced"}}]}
    )
    start_with_audio(env, env.svc.start_stream)
    env.svc._active["r"].sink.restarts = 5
    env.svc.tick()
    assert env.svc.state()["destinations"][0]["codec"] == "av1"
    env.svc.shutdown()


def test_state_reports_health_latency_and_totals(env: Env) -> None:
    env.svc.set(
        {"destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000", "options": {"latency_ms": 200}}]}
    )
    start_with_audio(env, env.svc.start_stream)
    video = env.last("video-hevc")
    env.feed(video, 0, 1500, key_at={0, 600, 1200})
    env.audio(0, 1500)
    sink = env.svc._active["a"].sink
    sink._line("total_size=5000")
    sink._line("speed=1.00x")
    sink._line("progress=continue")
    state = env.svc.state()
    dest = state["destinations"][0]
    assert state["status"] == "live" and dest["health"] == "ok" and dest["state"] == "live"
    assert dest["latency_ms"] >= 200 and dest["bitrate_kbps"] > 0 and state["bitrate_kbps"] > 0
    assert state["latency_ms"] == dest["latency_ms"]
    assert state["uptime_s"] >= 0 and state["output"] == {"width": 1280, "height": 720, "fps": 30.0}
    video.kwargs["on_line"]("fps=29.5")
    video.kwargs["on_line"]("drop_frames=3")
    video.kwargs["on_line"]("progress=continue")
    state = env.svc.state()
    assert state["encoders"][0]["encoder_dropped"] == 3 and state["dropped_frames"] >= 3
    env.svc.shutdown()


def test_shutdown_stops_every_child(env: Env) -> None:
    env.svc.set({"destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000"}]})
    start_with_audio(env, env.svc.start_stream)
    env.svc.shutdown()
    assert all(c.stopped for c in env.children if c.started)
    assert env.svc.state()["status"] == "idle"


# -- keys typed into the Console ---------------------------------------------------------------------------------


def test_a_typed_key_is_stored_privately_and_never_comes_back(env: Env, tmp_path: Path) -> None:
    env.svc.set({"destinations": [{"id": "tw", "kind": "rtmp", "url": "rtmp://h.test/app", "key": "live_abc_123"}]})
    stored = tmp_path / "keys" / "tw"
    assert stored.read_text() == "live_abc_123" and stored.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "keys").stat().st_mode & 0o777 == 0o700
    assert env.svc.state()["destinations"][0]["key"] == "set"
    assert "live_abc_123" not in repr(env.svc.state()) + (tmp_path / "stream.json").read_text()
    assert env.svc.destinations[0].key_ref == f"file:{stored}"
    assert not any("live_abc_123" in text for _, text in env.events)


def test_an_edit_without_a_key_keeps_the_stored_one(env: Env, tmp_path: Path) -> None:
    env.svc.set({"destinations": [{"id": "tw", "kind": "rtmp", "url": "rtmp://h.test/app", "key": "live_abc_123"}]})
    env.svc.set({"destinations": [{"id": "tw", "kind": "rtmp", "url": "rtmp://h2.test/app", "name": "Twitch"}]})
    assert env.svc.state()["destinations"][0]["key"] == "set"
    assert (tmp_path / "keys" / "tw").read_text() == "live_abc_123"
    assert env.svc.destinations[0].url == "rtmp://h2.test/app"


def test_an_empty_key_or_a_removed_destination_deletes_the_file(env: Env, tmp_path: Path) -> None:
    dests = [
        {"id": "a", "kind": "rtmp", "url": "rtmp://h.test/app", "key": "k-a"},
        {"id": "b", "kind": "rtmp", "url": "rtmp://h.test/app", "key": "k-b"},
    ]
    env.svc.set({"destinations": dests})
    assert sorted(p.name for p in (tmp_path / "keys").iterdir()) == ["a", "b"]
    env.svc.set({"destinations": [{**dests[0], "key": ""}]})
    assert list((tmp_path / "keys").iterdir()) == []
    assert env.svc.state()["destinations"][0]["key"] == "none"


def test_a_reference_replaces_a_typed_key(env: Env, tmp_path: Path) -> None:
    dest = {"id": "a", "kind": "rtmp", "url": "rtmp://h.test/app"}
    env.svc.set({"destinations": [{**dest, "key": "k-a"}]})
    env.svc.set({"destinations": [{**dest, "key_ref": "env:SS_KEY"}]})
    assert env.svc.destinations[0].key_ref == "env:SS_KEY"
    assert list((tmp_path / "keys").iterdir()) == []


def test_a_bad_edit_stores_nothing(env: Env, tmp_path: Path) -> None:
    good = {"id": "a", "kind": "rtmp", "url": "rtmp://h.test/app", "key": "k-a"}
    for bad in (
        [good, {"id": "b", "kind": "rtmp", "url": "not a url", "key": "k-b"}],
        [{**good, "key_ref": "env:X"}],
        [{**good, "key": "two words"}],
        [{**good, "key": 5}],
        [{**good, "key": "x" * 2000}],
    ):
        with pytest.raises(StreamError) as err:
            env.svc.set({"destinations": bad})
        assert err.value.status == 400 and "k-a" not in err.value.message
    assert not (tmp_path / "keys").exists()


def test_a_running_destination_cannot_be_given_a_new_key(env: Env) -> None:
    dest = {"id": "a", "kind": "srt", "url": "srt://h.test:9000"}
    env.svc.set({"destinations": [dest]})
    start_with_audio(env, env.svc.start_stream)
    with pytest.raises(StreamError, match="stop it before changing its key"):
        env.svc.set({"destinations": [{**dest, "key": "new-pass"}]})
    env.svc.shutdown()


def test_the_state_reports_cpu_and_gpu_of_each_encoder(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamstack.stream.usage import Usage

    env.svc.set({"destinations": [{"id": "a", "kind": "srt", "url": "srt://h.test:9000"}]})
    start_with_audio(env, env.svc.start_stream)
    env.feed(env.last("video-hevc"), 0, 200)
    monkeypatch.setattr(env.svc._usage, "sample", lambda pid: Usage(37.5, 12.0))
    state = env.svc.state()
    assert state["encoders"][0]["cpu_percent"] == 37.5 and state["encoders"][0]["gpu_percent"] == 12.0
    assert state["cpu_percent"] == 37.5 and state["gpu_percent"] == 12.0
    monkeypatch.setattr(env.svc._usage, "sample", lambda pid: Usage(None, None))
    idle = env.svc.state()
    assert idle["cpu_percent"] is None and idle["gpu_percent"] is None
    env.svc.shutdown()


def test_the_program_audio_is_the_streamstack_mic_unless_configured(tmp_path: Path) -> None:
    """Easy Effects is gone: the voice chain's own source is what the stream and recordings carry."""
    bare = StreamService({}, state_path=tmp_path / "stream.json")
    assert bare.audio_source().name == "streamstack_mic"
    chosen = StreamService({"STREAMSTACK_STREAM_AUDIO": "other_source"}, state_path=tmp_path / "stream.json")
    assert chosen.audio_source().name == "other_source"
