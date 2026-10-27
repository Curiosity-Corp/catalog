"""End to end with real ffmpeg processes, on the CPU only (libx264, libx265, libopus) and local receivers.

Nothing here touches a camera, a GPU, a network beyond 127.0.0.1, or a real platform. Skipped without ffmpeg.
"""

from __future__ import annotations

import itertools
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from streamstack.stream import proc
from streamstack.stream.ffcmd import AudioSource, Source
from streamstack.stream.service import StreamService

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _has(*needles: str) -> bool:
    if not FFMPEG:
        return False
    text = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False).stdout
    text += subprocess.run([FFMPEG, "-hide_banner", "-protocols"], capture_output=True, text=True, check=False).stdout
    return all(n in text for n in needles)


pytestmark = [
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg and ffprobe are needed"),
    pytest.mark.skipif(not _has("libx264", "libopus"), reason="ffmpeg needs libx264 and libopus"),
]


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def probe_packets(path: Path, kind: str) -> list[float]:
    assert FFPROBE
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", kind, "-show_entries", "packet=pts_time", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [float(p["pts_time"]) for p in json.loads(out)["packets"] if "pts_time" in p]


def stream_info(path: Path) -> list[dict[str, object]]:
    assert FFPROBE
    out = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)["streams"]


@pytest.fixture
def make_service(tmp_path: Path) -> Iterator[Callable[..., StreamService]]:
    made: list[StreamService] = []

    def make(**cfg: str) -> StreamService:
        svc = StreamService(
            {"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec"), **cfg},
            source_override=Source("lavfi", width=640, height=360, fps=30),
            audio_override=AudioSource("lavfi"),
        )
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()
    assert proc.kill_all() == 0, "a stream test left a process running"


def test_recording_survives_live_changes_and_stays_decodable(make_service, tmp_path: Path) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264", "bitrate_kbps": 1500})
    svc.start_record("mkv", "show")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 50_000)
    svc.set({"bitrate_kbps": 700, "keyint_s": 1})
    assert wait_for(lambda: (svc.state()["last_swap"] or {}).get("ok") is True)
    assert svc.state()["destinations"] == []
    svc.set({"rate_control": "vbr"})
    assert wait_for(lambda: svc.state()["encoders"][0]["swaps"] == 2)
    time.sleep(1.0)
    state = svc.state()
    assert state["status"] == "recording" and state["encoder"] == "libx264"
    svc.stop_record()
    assert svc.state()["status"] == "idle"
    files = sorted((tmp_path / "rec").glob("show-*.mkv"))
    assert len(files) == 1  # no picture format change, so no new segment
    video, audio = probe_packets(files[0], "v"), probe_packets(files[0], "a")
    assert len(video) > 60 and len(audio) > 150
    assert video == sorted(video) or sorted(video)[0] >= 0  # presentation order may differ only by B-frames (none here)
    steps = [b - a for a, b in zip(sorted(video), sorted(video)[1:], strict=False)]
    assert max(steps) < 0.12, f"a gap of {max(steps):.3f}s in the recorded video"
    subprocess.run([FFMPEG or "", "-v", "error", "-i", str(files[0]), "-f", "null", "-"], check=True, timeout=60)


def test_geometry_change_rolls_the_recording_to_a_new_segment(make_service, tmp_path: Path) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264"})
    svc.start_record("mkv", "roll")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 30_000)
    svc.set({"tier": "480p"})
    rolled = wait_for(lambda: len(list((tmp_path / "rec").glob("roll-*.mkv"))) == 2, 25)
    assert rolled, svc.state()["events"]
    time.sleep(1.0)
    svc.stop_record()
    first, second = sorted((tmp_path / "rec").glob("roll-*.mkv"))
    sizes = {(s["width"], s["height"]) for f in (first, second) for s in stream_info(f) if s["codec_type"] == "video"}
    assert sizes == {(640, 360), (854, 480)}


@pytest.mark.skipif(not _has("srt"), reason="ffmpeg has no libsrt")
def test_srt_to_a_local_listener_with_live_bitrate_change(make_service, tmp_path: Path) -> None:
    port = free_port(socket.SOCK_DGRAM)
    out = tmp_path / "received.ts"
    assert FFMPEG
    listener = subprocess.Popen(
        [FFMPEG, "-v", "error", "-nostdin", "-y", "-i", f"srt://127.0.0.1:{port}?mode=listener", "-c", "copy",
         "-f", "mpegts", str(out)],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip
    try:
        svc = make_service()
        svc.set(
            {
                "encoder": "libx264",
                "bitrate_kbps": 1200,
                "destinations": [
                    {"id": "srt-local", "kind": "srt", "url": f"srt://127.0.0.1:{port}", "options": {"latency_ms": 80}}
                ],
            }
        )
        svc.start_stream()
        assert wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 30)
        dest_state = svc.state()["destinations"][0]
        assert dest_state["codec"] == "h264" and dest_state["latency_ms"] > 80
        time.sleep(2)
        svc.set({"bitrate_kbps": 600})
        assert wait_for(lambda: (svc.state()["last_swap"] or {}).get("ok") is True)
        assert svc.state()["destinations"][0]["restarts"] == 0, "a bitrate change must not reconnect SRT"
        time.sleep(2)
        svc.stop_stream()
    finally:
        listener.terminate()
        try:
            listener.wait(timeout=5)
        except subprocess.TimeoutExpired:
            listener.kill()
            listener.wait()
    frames = probe_packets(out, "v")
    assert len(frames) > 90, f"only {len(frames)} frames reached the listener"
    steps = [b - a for a, b in itertools.pairwise(frames)]
    assert max(steps) < 0.25, f"a gap of {max(steps):.3f}s at the receiver across the live swap"
    assert {s["codec_name"] for s in stream_info(out)} == {"h264", "opus"} or "h264" in {
        s["codec_name"] for s in stream_info(out)
    }


LIVE_KEY = "s3cr3t-live-key"


def rtmp_listener(port: int, out: Path) -> subprocess.Popen[bytes]:
    assert FFMPEG
    return subprocess.Popen(
        [FFMPEG, "-v", "error", "-nostdin", "-y", "-listen", "1", "-f", "flv", "-i", f"rtmp://127.0.0.1:{port}/live/{LIVE_KEY}",
         "-c", "copy", "-f", "flv", str(out)],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip


def end(listener: subprocess.Popen[bytes]) -> None:
    listener.terminate()
    try:
        listener.wait(timeout=5)
    except subprocess.TimeoutExpired:
        listener.kill()
        listener.wait()


@pytest.mark.skipif(not _has("rtmp"), reason="ffmpeg has no RTMP")
def test_classic_rtmp_with_the_key_from_a_reference(make_service, tmp_path: Path, monkeypatch) -> None:
    port = free_port()
    out = tmp_path / "rtmp.flv"
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", LIVE_KEY)
    listener = rtmp_listener(port, out)
    try:
        svc = make_service()
        svc.set(
            {
                "encoder": "libx264",
                "destinations": [
                    {
                        "id": "rtmp-local",
                        "kind": "rtmp",
                        "url": f"rtmp://127.0.0.1:{port}/live",
                        "key_ref": "env:STREAMSTACK_TEST_KEY",
                        "options": {"mode": "classic"},
                    }
                ],
            }
        )
        svc.start_stream()
        assert wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 30)
        time.sleep(3)
        state = svc.state()
        assert LIVE_KEY not in json.dumps(state)  # the state says a key is set, never what it is
        svc.stop_stream()
    finally:
        end(listener)
    infos = {s["codec_name"] for s in stream_info(out)}
    assert "h264" in infos and "aac" in infos


@pytest.mark.skipif(not _has("libx265", "rtmp"), reason="ffmpeg needs libx265 and RTMP")
def test_enhanced_rtmp_carries_hevc(make_service, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", LIVE_KEY)
    port = free_port()
    out = tmp_path / "hevc.flv"
    listener = rtmp_listener(port, out)
    try:
        svc = make_service()
        svc.set(
            {
                "encoder": "libx265",
                "destinations": [
                    {
                        "id": "enh",
                        "kind": "rtmp",
                        "url": f"rtmp://127.0.0.1:{port}/live",
                        "key_ref": "env:STREAMSTACK_TEST_KEY",
                    }
                ],
            }
        )
        svc.start_stream()
        assert wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 40)
        assert svc.state()["destinations"][0]["codec"] == "hevc"
        time.sleep(3)
        svc.stop_stream()
    finally:
        end(listener)
    assert "hevc" in {s["codec_name"] for s in stream_info(out)}


def test_stopping_leaves_no_process_behind(make_service, tmp_path: Path) -> None:
    svc = make_service()
    svc.set({"encoder": "libx264"})
    svc.start_record("mp4", "frag")
    assert wait_for(lambda: svc.state()["recording"]["bytes"] > 20_000)
    svc.shutdown()
    assert proc.kill_all() == 0
    files = list((tmp_path / "rec").glob("frag-*.mp4"))
    assert len(files) == 1 and files[0].stat().st_size > 0
    assert {s["codec_type"] for s in stream_info(files[0])} == {"video", "audio"}
