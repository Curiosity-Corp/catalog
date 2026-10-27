"""WHIP against a production-grade ingest: MediaMTX (pinned, checksummed), decoded back out over RTSP.

This is the interoperability check for the codecs aiortc's own answerer cannot take (HEVC, AV1) and for a server
that is ICE-lite and bundles the way real ingests do. The release is downloaded once into the temp directory and
verified against the SHA-256 below; the tests skip, with the reason, when it cannot be fetched (offline).
"""

from __future__ import annotations

import hashlib
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from streamstack.stream.whip import support
from streamstack.stream.whip.protocol import TrickleUnsupported, build_sdpfrag
from streamstack.stream.whip.sink import WhipSink

VERSION = "v1.21.2"
URL = f"https://github.com/bluenviron/mediamtx/releases/download/{VERSION}/mediamtx_{VERSION}_linux_amd64.tar.gz"
SHA256 = "121be6e00397e473a95d83c75f4fcd3819a6af3b0e1a50889cba43ab4fab6a27"

FFMPEG = shutil.which("ffmpeg")


def _encoders() -> str:
    if not FFMPEG:
        return ""
    return subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False).stdout


ENCODERS = _encoders()

pytestmark = [
    pytest.mark.skipif(not FFMPEG or "libopus" not in ENCODERS, reason="ffmpeg with libopus is needed"),
    pytest.mark.skipif(not support.available(), reason="the whip extra (aiortc) is not installed"),
    pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is needed"),
]

ENCODE = {
    "h264": ("libx264", ["-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-bf", "0", "-pix_fmt", "yuv420p"]),
    "hevc": (
        "libx265",
        ["-c:v", "libx265", "-preset", "veryfast", "-x265-params", "keyint=30:bframes=0", "-pix_fmt", "yuv420p"],
    ),
    "av1": ("libsvtav1", ["-c:v", "libsvtav1", "-preset", "10", "-g", "30"]),
}


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch_mediamtx() -> Path:
    """The verified binary, downloaded once per machine; skips the test when it cannot be had."""
    root = Path(tempfile.gettempdir()) / f"streamstack-mediamtx-{SHA256[:12]}"
    binary = root / "mediamtx"
    if binary.is_file():
        return binary
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "release.tar.gz"
    try:
        with urllib.request.urlopen(URL, timeout=60) as reply:
            data = reply.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"cannot download MediaMTX {VERSION} ({exc}); this interop test needs the network once")
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        pytest.fail(f"MediaMTX {VERSION} download has SHA-256 {digest}, expected {SHA256}")
    archive.write_bytes(data)
    with tarfile.open(archive) as tar:
        member = tar.extractfile("mediamtx")  # one named member, written to a fixed path: nothing else is unpacked
        if member is None:
            pytest.fail(f"MediaMTX {VERSION} archive has no mediamtx binary")
        binary.write_bytes(member.read())
    binary.chmod(0o755)
    return binary


class Ingest:
    def __init__(self, binary: Path, tmp: Path) -> None:
        self.http = free_port()
        self.rtsp = free_port()
        self.udp = free_port(socket.SOCK_DGRAM)
        self.api = free_port()
        config = tmp / "mediamtx.yml"
        config.write_text(
            "\n".join(
                [
                    "logLevel: warn",
                    "api: yes",
                    f"apiAddress: 127.0.0.1:{self.api}",
                    "rtsp: yes",
                    f"rtspAddress: 127.0.0.1:{self.rtsp}",
                    "rtmp: no",
                    "hls: no",
                    "srt: no",
                    "webrtc: yes",
                    f"webrtcAddress: 127.0.0.1:{self.http}",
                    f"webrtcLocalUDPAddress: 127.0.0.1:{self.udp}",
                    "webrtcAdditionalHosts: [127.0.0.1]",
                    "paths:",
                    "  all_others:",
                    "",
                ]
            )
        )
        self.log = (tmp / "mediamtx.log").open("wb")
        self.proc = subprocess.Popen(
            [str(binary), str(config)], stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.http), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        pytest.fail("MediaMTX did not start")

    def whip_url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.http}/{name}/whip"

    def rtsp_url(self, name: str) -> str:
        return f"rtsp://127.0.0.1:{self.rtsp}/{name}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log.close()


@pytest.fixture(scope="module")
def ingest(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Ingest]:
    binary = fetch_mediamtx()
    server = Ingest(binary, tmp_path_factory.mktemp("mediamtx"))
    yield server
    server.stop()


def publish(sink: WhipSink, codec: str, seconds: float, until: Callable[[], bool] = lambda: False) -> None:
    """Feed a real ffmpeg encode into the sink as the relay would, for `seconds` or until `until()`."""
    assert FFMPEG
    name, args = ENCODE[codec]
    if name not in ENCODERS:
        pytest.skip(f"ffmpeg has no {name}")
    child = subprocess.Popen(
        [FFMPEG, "-v", "error", "-nostdin", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", *args,
         "-c:a", "libopus", "-b:a", "96k", "-ar", "48000", "-ac", "2",
         "-flags:v", "+global_header", "-f", "matroska", "-live", "1", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )  # fmt: skip
    assert child.stdout
    try:
        sink.open(b"")
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            data = child.stdout.read1(65536)
            if not data:
                break
            sink.write(data, 1, False)
    finally:
        child.kill()
        child.wait()


def read_back(url: str, frames: int = 30) -> tuple[str, int, int]:
    """Decode `frames` pictures from the RTSP side; returns codec name, width, height."""
    assert FFMPEG
    probe = subprocess.run(
        [shutil.which("ffprobe") or "", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v",
         "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", url],
        capture_output=True, text=True, timeout=40, check=False,
    )  # fmt: skip
    # a reader that joins mid-GOP sees reference errors until the next keyframe (HEVC especially); what matters is
    # that whole pictures decode afterwards, so count the frames that came out rather than read the log
    decode = subprocess.run(
        [FFMPEG, "-v", "quiet", "-rtsp_transport", "tcp", "-i", url, "-map", "0:v:0", "-frames:v", str(frames),
         "-f", "framecrc", "-"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    decoded = [line for line in decode.stdout.splitlines() if line and not line.startswith("#")]
    assert len(decoded) >= frames - 5, f"only {len(decoded)} pictures decoded (ffmpeg exit {decode.returncode})"
    codec, width, height = probe.stdout.strip().splitlines()[0].split(",")
    return codec, int(width), int(height)


@pytest.mark.parametrize(("codec", "expect"), [("h264", "h264"), ("hevc", "hevc"), ("av1", "av1")])
def test_each_codec_is_ingested_and_decodes(ingest: Ingest, codec: str, expect: str) -> None:
    name = f"cam-{codec}"
    sink = WhipSink(name, ingest.whip_url(name), lambda: "")
    found: list[tuple[str, int, int]] = []
    failure: list[BaseException] = []

    def reader() -> None:
        try:
            found.append(read_back(ingest.rtsp_url(name)))
        except (AssertionError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            failure.append(exc)

    thread = threading.Thread(target=reader, daemon=True)

    def live() -> bool:
        if sink.state == "live" and not thread.is_alive() and not found and not failure and not thread.ident:
            thread.start()  # read the picture back while the publisher keeps feeding
        return bool(found or failure)

    try:
        publish(sink, codec, 60, live)
        status = sink.status()
        assert not failure, failure
        assert found, (sink.state, sink.last_error, status["whip"])
        assert found[0] == (expect, 640, 360)
        whip = status["whip"]
        assert whip["ice_state"] == "completed" and whip["dtls_state"] == "connected"
        assert whip["packets_sent"] > 100 and whip["bitrate_kbps"] > 50
    finally:
        sink.shutdown()
        if thread.ident:
            thread.join(timeout=10)
    assert sink.state == "stopped"


def test_trickle_patch_is_accepted_or_reported_as_unsupported(ingest: Ingest) -> None:
    """The client's PATCH against a real server: MediaMTX either takes the end-of-candidates fragment or says no, and
    the client says which."""
    sink = WhipSink("cam-patch", ingest.whip_url("cam-patch"), lambda: "")
    try:
        publish(sink, "h264", 30, lambda: sink.state == "live")
        assert sink.state == "live", (sink.state, sink.last_error)
        session = sink._session
        assert session is not None and session._resource is not None and session._gatherer is not None
        params = session._gatherer.getLocalParameters()
        fragment = build_sdpfrag(params.usernameFragment, params.password, "0", [], end=True)
        try:
            session._client.patch(session._resource, fragment)
        except TrickleUnsupported as exc:
            assert not session.stats.trickle and exc.status in (405, 501)
        else:
            assert session.stats.trickle
        assert sink.state == "live", "a PATCH must not disturb the running session"
    finally:
        sink.shutdown()


def test_stop_removes_the_path_and_a_second_publish_works(ingest: Ingest) -> None:
    name = "cam-again"
    for _ in range(2):
        sink = WhipSink(name, ingest.whip_url(name), lambda: "")
        try:
            publish(sink, "h264", 30, lambda s=sink: s.state == "live" and s.status()["whip"]["packets_sent"] > 30)
            assert sink.state == "live", (sink.state, sink.last_error)
        finally:
            sink.shutdown()
        assert sink.state == "stopped"
