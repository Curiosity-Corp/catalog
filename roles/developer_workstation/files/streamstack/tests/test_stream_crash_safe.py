"""Crash-safe local recording (#81): the raw microphone track, the disk guard, orphan recovery after kill -9, and the
clean finalise when the Console is told to stop (SIGTERM).

The recorder tests run real ffmpeg on the CPU against lavfi sources (no camera, no microphone, no GPU) and skip
without ffmpeg, libx264 and libopus.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from stream_support import RecordingSink, ablock, audio_track, vblock, video_track

from streamstack.stream import mkv, proc, recover
from streamstack.stream.ffcmd import AudioSource, Source
from streamstack.stream.relay import Chain
from streamstack.stream.service import StreamError, StreamService

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
ROOT = Path(__file__).resolve().parent.parent


def _has_encoders() -> bool:
    if not (FFMPEG and FFPROBE):
        return False
    text = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False).stdout
    return "libx264" in text and "libopus" in text


needs_ffmpeg = pytest.mark.skipif(not _has_encoders(), reason="ffmpeg with libx264 and libopus is needed")


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def streams(path: Path) -> list[dict[str, object]]:
    assert FFPROBE
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name:stream_tags=title", "-of", "json",
         str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout  # fmt: skip
    return json.loads(out)["streams"]


def duration(path: Path) -> float:
    assert FFPROBE
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def mean_volume_hz_split(path: Path, stream_index: int) -> float:
    """The dominant tone of one audio stream, by counting zero crossings of its first second."""
    assert FFMPEG
    pcm = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-map", f"0:{stream_index}", "-t", "1", "-ac", "1", "-ar", "48000",
         "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    samples = [int.from_bytes(pcm[i : i + 2], "little", signed=True) for i in range(0, len(pcm) - 1, 2)]
    crossings = sum(1 for a, b in itertools.pairwise(samples) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / 48000)


def declared_tracks(header: bytes) -> list[int]:
    """The track numbers a stream header declares (the demuxer reports them with the first cluster)."""
    cluster = mkv.encode_cluster([vblock(0, key=True)], {mkv.TRACK_VIDEO: 1})
    first = next(e for e in mkv.Demuxer().feed(header + cluster) if isinstance(e, mkv.Header))
    return [t.number for t in first.tracks]


# -- the track, in the container ---------------------------------------------------------------------------


def test_a_header_can_carry_a_named_raw_microphone_track() -> None:
    header = mkv.stream_header(
        video_track(),
        audio_track(),
        raw=audio_track(),
        names={mkv.TRACK_AUDIO: "Processed", mkv.TRACK_RAW: "Raw"},
    )
    assert declared_tracks(header) == [1, 2, 3]
    assert b"Processed" in header and b"Raw" in header


def test_a_header_without_a_raw_track_is_unchanged() -> None:
    plain = mkv.stream_header(video_track(), audio_track())
    assert plain == mkv.stream_header(video_track(), audio_track(), raw=None, names=None)
    assert declared_tracks(plain) == [1, 2]


# -- the relay hands the raw track to recordings only -----------------------------------------------------


def feed(chain: Chain, now: list[float], seconds: int = 3) -> None:
    for ms in range(0, seconds * 1000, 20):
        now[0] = float(ms)
        if ms % 1000 == 0:
            chain.video_block(1, vblock(ms, key=True))
        elif ms % 40 == 0:
            chain.video_block(1, vblock(ms))
        chain.audio_block(ablock(ms))
        chain.raw_block(mkv.Block(mkv.TRACK_RAW, ms, True, b"\x55" * 20))


def test_only_a_recording_receives_the_raw_microphone() -> None:
    now = [0.0]
    chain = Chain("h264", lambda: now[0], 40.0)
    chain.set_audio(audio_track())
    chain.set_raw(audio_track())
    chain.generation_header(1, video_track())
    chain.begin_swap(1, 0)
    live, rec = RecordingSink("live"), RecordingSink("rec")
    chain.attach(live)
    chain.attach(rec, wants_raw=True)
    feed(chain, now)
    live_tracks = {b.track for b in live.blocks()}
    rec_tracks = {b.track for b in rec.blocks()}
    assert mkv.TRACK_RAW not in live_tracks and live_tracks == {mkv.TRACK_VIDEO, mkv.TRACK_AUDIO}
    assert rec_tracks == {mkv.TRACK_VIDEO, mkv.TRACK_AUDIO, mkv.TRACK_RAW}
    assert declared_tracks(rec.headers[-1]) == [1, 2, 3]
    assert declared_tracks(live.headers[-1]) == [1, 2]


def test_a_stalled_raw_capture_never_delays_the_live_path() -> None:
    now = [0.0]
    chain = Chain("h264", lambda: now[0], 40.0)
    chain.set_audio(audio_track())
    chain.set_raw(audio_track())
    chain.generation_header(1, video_track())
    chain.begin_swap(1, 0)
    live = RecordingSink("live")
    chain.attach(live)
    chain.attach(RecordingSink("rec"), wants_raw=True)
    for ms in range(0, 2000, 20):  # no raw block ever arrives
        now[0] = float(ms)
        if ms % 1000 == 0:
            chain.video_block(1, vblock(ms, key=True))
        elif ms % 40 == 0:
            chain.video_block(1, vblock(ms))
        chain.audio_block(ablock(ms))
    assert max(b.ts_ms for b in live.blocks()) >= 1700


def test_no_raw_block_is_kept_when_nobody_asked_for_it() -> None:
    now = [0.0]
    chain = Chain("h264", lambda: now[0], 40.0)
    chain.set_audio(audio_track())
    chain.set_raw(audio_track())
    chain.generation_header(1, video_track())
    chain.begin_swap(1, 0)
    chain.attach(RecordingSink("live"))
    chain.raw_block(mkv.Block(mkv.TRACK_RAW, 0, True, b"x"))
    assert chain.merger.pending() == 0


# -- markers and orphans ----------------------------------------------------------------------------------


def test_a_marker_names_its_writer_and_goes_away_on_a_clean_stop(tmp_path: Path) -> None:
    rec = tmp_path / "a.mkv"
    rec.write_bytes(b"x")
    marker = recover.write_marker(rec, "mkv", 3)
    assert marker.name == "a.mkv.recording"
    assert recover.orphans(tmp_path) == []  # this process is alive
    recover.clear_marker(rec)
    assert not marker.exists()
    recover.clear_marker(rec)  # clearing twice is harmless


def test_a_marker_of_a_dead_process_is_an_orphan_and_a_reused_pid_is_not_mistaken_for_the_writer(
    tmp_path: Path,
) -> None:
    rec = tmp_path / "b.mkv"
    rec.write_bytes(b"x")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        recover.write_marker(rec, "mkv", 2, pid=child.pid)
        assert recover.orphans(tmp_path) == []
        child.kill()
        child.wait()
        found = recover.orphans(tmp_path)
        assert [m.recording for m in found] == [rec]
    finally:
        child.kill()
    # the same pid but another start time: the writer is gone even though the pid answers
    body = json.loads(recover.marker_for(rec).read_text())
    body.update(pid=os.getpid(), start_ticks=body["start_ticks"] + 12345)
    recover.marker_for(rec).write_text(json.dumps(body))
    assert [m.recording for m in recover.orphans(tmp_path)] == [rec]


def test_a_damaged_marker_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "c.mkv.recording").write_text("{not json")
    assert recover.orphans(tmp_path) == []


def test_a_recording_that_never_got_a_frame_leaves_nothing_behind(tmp_path: Path) -> None:
    rec = tmp_path / "d.mkv"
    marker = recover.write_marker(rec, "mkv", 2, pid=2**22 + 7)
    outcomes = recover.recover(recover._read_marker(marker), "ffmpeg")  # type: ignore[arg-type]
    assert [o.status for o in outcomes] == ["missing"]
    assert not marker.exists()


# -- the real recorder ------------------------------------------------------------------------------------


@pytest.fixture
def make_service(tmp_path: Path) -> Iterator[Callable[..., StreamService]]:
    made: list[StreamService] = []

    def make(raw: AudioSource | None = None, **kwargs: object) -> StreamService:
        cfg = {"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec"), **kwargs.pop("cfg", {})}  # type: ignore[arg-type]
        svc = StreamService(
            cfg,
            source_override=Source("lavfi", width=640, height=360, fps=30),
            audio_override=AudioSource("lavfi", tone_hz=440),
            raw_override=raw,
            **kwargs,  # type: ignore[arg-type]
        )
        svc.set({"encoder": "libx264", "bitrate_kbps": 1500})
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()
    assert proc.kill_all() == 0, "a test left a process running"


@needs_ffmpeg
def test_a_recording_keeps_the_raw_microphone_as_its_own_track(make_service, tmp_path: Path) -> None:
    svc = make_service(raw=AudioSource("lavfi", tone_hz=880))
    svc.start_record("mkv", "raw")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 80_000)
    assert svc.state()["recording"]["raw_mic"] is True
    time.sleep(1.5)
    svc.stop_record()
    (path,) = list((tmp_path / "rec").glob("raw-*.mkv"))
    info = streams(path)
    assert [s["codec_type"] for s in info] == ["video", "audio", "audio"]
    titles = [(s.get("tags") or {}).get("title") for s in info]  # type: ignore[union-attr]
    assert titles[1:] == ["Microphone (processed)", "Microphone (raw)"]
    assert 400 < mean_volume_hz_split(path, 1) < 480  # the processed track is the 440 Hz tone
    assert 820 < mean_volume_hz_split(path, 2) < 940  # the raw track is the 880 Hz tone
    assert not recover.marker_for(path).exists()


@needs_ffmpeg
def test_an_mp4_recording_carries_the_raw_microphone_too(make_service, tmp_path: Path) -> None:
    svc = make_service(raw=AudioSource("lavfi", tone_hz=880))
    svc.start_record("mp4", "raw")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 80_000)
    time.sleep(1.5)
    svc.stop_record()
    (path,) = list((tmp_path / "rec").glob("raw-*.mp4"))
    assert [s["codec_type"] for s in streams(path)] == ["video", "audio", "audio"]


@needs_ffmpeg
def test_the_raw_track_can_be_turned_off(make_service, tmp_path: Path) -> None:
    svc = make_service(raw=AudioSource("lavfi", tone_hz=880), cfg={"STREAMSTACK_RECORD_RAW_MIC": "false"})
    svc.start_record("mkv", "plain")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 80_000)
    time.sleep(1.0)
    svc.stop_record()
    (path,) = list((tmp_path / "rec").glob("plain-*.mkv"))
    assert [s["codec_type"] for s in streams(path)] == ["video", "audio"]


def failing_raw_capture(name: str, argv: list[str], **kwargs: object) -> proc.Child:
    """The raw capture dies at once (a missing device); every other process is the real thing."""
    if name.startswith("raw-"):
        argv = [sys.executable, "-c", "import sys; sys.stderr.write('no such device\\n'); sys.exit(1)"]
    return proc.Child(name, argv, **kwargs)  # type: ignore[arg-type]


@needs_ffmpeg
def test_a_recording_goes_on_when_the_raw_capture_cannot_start(make_service, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("streamstack.stream.service.AUDIO_HEADER_WAIT_S", 1.0)
    svc = make_service(raw=AudioSource("lavfi", tone_hz=880), spawn=failing_raw_capture)
    svc.start_record("mkv", "noraw")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 80_000)
    state = svc.state()["recording"]
    assert state["raw_mic"] is False and "raw microphone" in state["raw_error"].lower()
    svc.stop_record()
    (path,) = list((tmp_path / "rec").glob("noraw-*.mkv"))
    assert [s["codec_type"] for s in streams(path)] == ["video", "audio"]


# -- the disk guard ---------------------------------------------------------------------------------------


MB = 1024 * 1024


@needs_ffmpeg
def test_a_recording_is_refused_when_the_disk_is_nearly_full(make_service) -> None:
    svc = make_service(disk_free=lambda _p: 1500 * MB)  # the minimum is 1024 MB: two of it are needed to start
    with pytest.raises(StreamError) as err:
        svc.start_record("mkv", "full")
    assert err.value.status == 507 and "free disk space" in err.value.message
    assert "STREAMSTACK_RECORD_MIN_FREE_MB" in err.value.message
    assert not svc.running


@needs_ffmpeg
def test_the_minimum_free_space_is_configurable(make_service) -> None:
    svc = make_service(disk_free=lambda _p: 1500 * MB, cfg={"STREAMSTACK_RECORD_MIN_FREE_MB": "100"})
    svc.start_record("mkv", "ok")
    assert svc.running


@needs_ffmpeg
def test_a_recording_warns_once_then_stops_cleanly_before_the_disk_is_full(make_service, tmp_path: Path) -> None:
    free = [20_000 * MB]
    events: list[tuple[str, str]] = []
    svc = make_service(disk_free=lambda _p: free[0])
    svc.notify = lambda level, text: events.append((level, text))
    svc.start_record("mkv", "guard")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 80_000)
    shown = svc.state()["recording"]
    assert shown["free_mb"] == 20_000 and shown["minutes_left"] > 0
    free[0] = 2500 * MB  # under three times the minimum
    assert wait_for(lambda: any("getting low" in t for _, t in events), 5)
    time.sleep(1.0)
    assert sum("getting low" in t for _, t in events) == 1
    free[0] = 900 * MB  # under the minimum
    assert wait_for(lambda: any(lvl == "error" for lvl, _ in events), 5)
    assert not svc.running
    stopped = [t for lvl, t in events if lvl == "error"]
    assert stopped and "Everything recorded so far is saved" in stopped[0]
    assert svc.state()["recording"].get("stopped")
    (path,) = list((tmp_path / "rec").glob("guard-*.mkv"))
    assert [x["codec_type"] for x in streams(path)] == ["video", "audio"]  # closed and playable
    assert not recover.marker_for(path).exists()


# -- kill -9 and recovery ---------------------------------------------------------------------------------

RECORDER = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    from streamstack.stream.ffcmd import AudioSource, Source
    from streamstack.stream.service import StreamService

    svc = StreamService(
        {"STREAMSTACK_RECORD_DIR": sys.argv[1]},
        source_override=Source("lavfi", width=640, height=360, fps=30),
        audio_override=AudioSource("lavfi", tone_hz=440),
        raw_override=AudioSource("lavfi", tone_hz=880),
    )
    svc.set({"encoder": "libx264", "bitrate_kbps": 1500})
    svc.start_record(sys.argv[2], "crash")
    while svc.state()["recording"]["bytes"] < 300_000:
        time.sleep(0.1)
    time.sleep(2.0)
    kids = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout.split()
    print(json.dumps({"pid": os.getpid(), "children": [int(k) for k in kids]}), flush=True)
    time.sleep(120)
    """
)


def run_recorder(tmp_path: Path, fmt: str) -> tuple[subprocess.Popen[str], dict[str, list[int] | int]]:
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER)
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}"}
    child = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "rec"), fmt],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line, "the recorder did not report"
    return child, json.loads(line)


def reap(info: dict[str, list[int] | int]) -> None:
    """A SIGKILLed recorder cannot stop its ffmpeg children; end them so no test leaves one behind."""
    for pid in info["children"]:  # type: ignore[union-attr]
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@needs_ffmpeg
def test_a_recording_survives_kill_9_and_is_repaired_at_the_next_start(tmp_path: Path) -> None:
    child, info = run_recorder(tmp_path, "mkv")
    try:
        child.kill()  # no chance to close anything
        child.wait(timeout=10)
    finally:
        reap(info)
    (path,) = list((tmp_path / "rec").glob("crash-*.mkv"))
    marker = recover.marker_for(path)
    assert marker.exists(), "the recording was not marked as in progress"
    before = path.stat().st_size
    assert before > 100_000
    # the killed file already plays, but it has no index, so no duration
    assert streams(path)
    found = recover.orphans(tmp_path / "rec")
    assert [m.recording for m in found] == [path]

    events: list[tuple[str, str]] = []
    svc = StreamService({"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec")})
    svc.notify = lambda level, text: events.append((level, text))
    svc._probe.get = lambda refresh=False: type("H", (), {"ffmpeg": FFMPEG})()  # type: ignore[method-assign]
    svc.start()
    assert wait_for(lambda: not marker.exists(), 60)
    assert wait_for(lambda: any("repaired" in t for _, t in events), 5)
    assert [s["codec_type"] for s in streams(path)] == ["video", "audio", "audio"]
    assert duration(path) > 1.0
    assert path.stat().st_size >= 0.97 * before
    assert not list((tmp_path / "rec").glob(".*repair*"))
    assert recover.orphans(tmp_path / "rec") == []


@needs_ffmpeg
def test_a_killed_fragmented_mp4_is_checked_and_left_as_it_is(tmp_path: Path) -> None:
    child, info = run_recorder(tmp_path, "mp4")
    try:
        child.kill()
        child.wait(timeout=10)
    finally:
        reap(info)
    (path,) = list((tmp_path / "rec").glob("crash-*.mp4"))
    (marker,) = recover.orphans(tmp_path / "rec")
    assert streams(path), "a fragmented MP4 plays after a kill"
    size = path.stat().st_size
    outcomes = recover.recover(marker, FFMPEG or "ffmpeg")
    assert [o.status for o in outcomes] == ["intact"]
    assert path.stat().st_size == size
    assert not recover.marker_for(path).exists()


@needs_ffmpeg
def test_a_repair_that_cannot_be_verified_keeps_the_original(tmp_path: Path) -> None:
    rec = tmp_path / "bad.mkv"
    rec.write_bytes(b"this is not a recording at all" * 100)
    marker = recover._read_marker(recover.write_marker(rec, "mkv", 2, pid=2**22 + 9))
    assert marker is not None
    (outcome,) = recover.recover(marker, FFMPEG or "ffmpeg")
    assert outcome.status == "failed"
    assert rec.read_bytes().startswith(b"this is not")
    assert recover.marker_for(rec).exists()  # still there: it can be looked at again


@needs_ffmpeg
def test_recovery_does_not_touch_a_recording_this_service_is_writing(make_service, tmp_path: Path) -> None:
    svc = make_service()
    svc.start_record("mkv", "live")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 50_000)
    (path,) = list((tmp_path / "rec").glob("live-*.mkv"))
    assert recover.orphans(tmp_path / "rec") == []  # its writer (this process) is alive
    assert recover.marker_for(path).exists()
    svc.stop_record()
    assert not recover.marker_for(path).exists()


# -- SIGTERM on the Console -------------------------------------------------------------------------------

CONSOLE = textwrap.dedent(
    """
    import json, os, sys, threading, time
    from streamstack.console import launcher, rig as rigmod
    from streamstack.console.stream import StreamController
    from streamstack.stream.ffcmd import AudioSource, Source
    from streamstack.stream.service import StreamService

    svc = StreamService(
        {"STREAMSTACK_RECORD_DIR": sys.argv[1]},
        source_override=Source("lavfi", width=640, height=360, fps=30),
        audio_override=AudioSource("lavfi", tone_hz=440),
        raw_override=AudioSource("lavfi", tone_hz=880),
    )
    svc.set({"encoder": "libx264", "bitrate_kbps": 1500})
    factory = rigmod.Rig.demo

    def demo_with_a_real_recorder():
        rig = factory()
        rig.stream = StreamController({}, service=svc)
        return rig

    rigmod.Rig.live = staticmethod(demo_with_a_real_recorder)

    def record():
        svc.start_record("mkv", "term")
        while svc.state()["recording"]["bytes"] < 300_000:
            time.sleep(0.1)
        time.sleep(1.5)
        print(json.dumps({"ready": True}), flush=True)

    threading.Thread(target=record, daemon=True).start()
    sys.exit(launcher.run_console(window=False, idle_exit=0, poll=0.1))
    """
)


@needs_ffmpeg
def test_sigterm_on_the_console_finishes_the_recording(tmp_path: Path) -> None:
    script = tmp_path / "console.py"
    script.write_text(CONSOLE)
    home = tmp_path / "home"
    for sub in ("run", "state", "config", "cache"):
        (home / sub).mkdir(parents=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }
    child = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "rec")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert child.stdout is not None
    try:
        ready = ""
        end = time.monotonic() + 60
        while time.monotonic() < end and '"ready"' not in ready:
            ready = child.stdout.readline()
            if not ready and child.poll() is not None:
                break
        assert '"ready"' in ready, child.stderr.read() if child.stderr else "the Console did not start recording"
        (path,) = list((tmp_path / "rec").glob("term-*.mkv"))
        assert recover.marker_for(path).exists()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
    assert not recover.marker_for(path).exists(), "the recording was left marked as unfinished"
    assert [s["codec_type"] for s in streams(path)] == ["video", "audio", "audio"]
    assert path.stat().st_size > 100_000
    leftovers = subprocess.run(["pgrep", "-f", f"{tmp_path}"], capture_output=True, text=True).stdout.split()
    assert [p for p in leftovers if int(p) != os.getpid()] == [], "an ffmpeg outlived the Console"
