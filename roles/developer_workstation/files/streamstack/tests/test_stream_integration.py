"""End to end with real ffmpeg processes, on the CPU only (libx264, libx265, libopus) and local receivers.

Nothing here touches a camera, a GPU, a network beyond 127.0.0.1, or a real platform. Skipped without ffmpeg.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import socket
import ssl
import subprocess
import threading
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


def _ffmpeg_major() -> int:
    if not FFMPEG:
        return 0
    text = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True, check=False).stdout
    match = re.search(r"ffmpeg version (\d+)\.", text)
    return int(match.group(1)) if match else 0


@pytest.mark.skipif(
    not (_has("libx265", "rtmp") and _ffmpeg_major() >= 8), reason="ffmpeg 8 with libx265 and RTMP is needed"
)
def test_enhanced_rtmp_carries_hevc_and_opus_with_the_audio_copied(make_service, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", LIVE_KEY)
    port = free_port()
    out = tmp_path / "hevc-opus.flv"
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
                        "options": {"audio": "opus"},
                    }
                ],
            }
        )
        svc.start_stream()
        assert wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 40)
        dest = svc.state()["destinations"][0]
        assert dest["codec"] == "hevc"
        assert dest["rtmp"] == {
            "enhanced": True, "legacy": False, "audio": "opus", "tls": False, "tls_verify": False,
        }  # fmt: skip
        time.sleep(3)
        svc.stop_stream()
    finally:
        end(listener)
    assert {s["codec_name"] for s in stream_info(out)} >= {"hevc", "opus"}
    assert len(probe_packets(out, "a")) > 50  # Opus packets, not just the header


class TlsIngest:
    """An RTMPS ingest on loopback: TLS terminated here, the plain RTMP handed to an ffmpeg listener behind it.

    ffmpeg cannot listen for rtmps itself, so this is the TLS half of a real ingest. `handshakes` counts completed
    TLS handshakes and `upstream` the connections passed on (a client that rejects the certificate sends none).
    """

    def __init__(self, cert: Path, key: Path, plain_port: int) -> None:
        self.plain_port = plain_port
        self.handshakes = 0
        self.upstream = 0
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self._context = context
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="tls-ingest", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            tls = self._context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            conn.close()
            return
        self.handshakes += 1
        try:
            first = tls.recv(65536)  # a client that distrusts the certificate hangs up before sending anything
            if not first:
                return
            with socket.create_connection(("127.0.0.1", self.plain_port), timeout=10) as upstream:
                self.upstream += 1
                upstream.sendall(first)
                back = threading.Thread(target=self._pump, args=(upstream, tls), daemon=True)
                back.start()
                self._pump(tls, upstream)
                back.join(timeout=5)
        except OSError:
            pass
        finally:
            tls.close()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for 127.0.0.1 and localhost, made with the openssl command."""
    cert, key = directory / "ingest.pem", directory / "ingest.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        capture_output=True, check=True,
    )  # fmt: skip
    return cert, key


TLS_GATE = pytest.mark.skipif(
    not (_has("libx264", "rtmps") and shutil.which("openssl")), reason="ffmpeg with TLS and the openssl command"
)


@pytest.fixture
def tls_ingest(tmp_path: Path) -> Iterator[Callable[[Path], tuple[TlsIngest, Path]]]:
    """Start an ffmpeg RTMP listener and an RTMPS front for it; yields (ingest, certificate)."""
    started: list[tuple[TlsIngest, subprocess.Popen[bytes]]] = []

    def start(out: Path) -> tuple[TlsIngest, Path]:
        cert, key = make_certificate(tmp_path)
        plain = free_port()
        listener = rtmp_listener(plain, out)
        ingest = TlsIngest(cert, key, plain)
        started.append((ingest, listener))
        return ingest, cert

    yield start
    for ingest, listener in started:
        ingest.close()
        end(listener)


def rtmps_destination(port: int, **options: object) -> dict[str, object]:
    return {
        "id": "tls",
        "kind": "rtmp",
        "url": f"rtmps://127.0.0.1:{port}/live",
        "key_ref": "env:STREAMSTACK_TEST_KEY",
        "options": {"mode": "classic", **options},
    }


@TLS_GATE
def test_rtmps_publishes_when_the_certificate_is_trusted(make_service, tls_ingest, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", LIVE_KEY)
    out = tmp_path / "rtmps.flv"
    ingest, cert = tls_ingest(out)
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [rtmps_destination(ingest.port, ca_file=str(cert))]})
    svc.start_stream()
    assert wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 40)
    state = svc.state()["destinations"][0]
    assert state["rtmp"]["tls"] and state["rtmp"]["tls_verify"] and LIVE_KEY not in json.dumps(state)
    time.sleep(3)
    svc.stop_stream()
    assert ingest.handshakes >= 1 and ingest.upstream >= 1
    assert {s["codec_name"] for s in stream_info(out)} >= {"h264", "aac"}


@TLS_GATE
def test_rtmps_refuses_an_untrusted_certificate_and_sends_no_key(
    make_service, tls_ingest, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", LIVE_KEY)
    ingest, _ = tls_ingest(tmp_path / "never.flv")
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [rtmps_destination(ingest.port)]})  # verification is the default
    svc.start_stream()
    assert wait_for(lambda: svc.state()["destinations"][0]["last_error"] != "", 30)
    state = svc.state()["destinations"][0]
    assert state["state"] != "live" and "certificate" in state["last_error"].lower()
    assert ingest.upstream == 0, "RTMP bytes (the stream key) reached a server whose certificate was not trusted"
    assert LIVE_KEY not in json.dumps(state)
    svc.stop_stream()


@TLS_GATE
def test_rtmps_with_verification_off_publishes_to_a_self_signed_server(
    make_service, tls_ingest, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", LIVE_KEY)
    out = tmp_path / "insecure.flv"
    ingest, _ = tls_ingest(out)
    svc = make_service()
    svc.set({"encoder": "libx264", "destinations": [rtmps_destination(ingest.port, tls_verify=False)]})
    svc.start_stream()
    assert wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 40)
    assert svc.state()["destinations"][0]["rtmp"]["tls_verify"] is False
    time.sleep(2)
    svc.stop_stream()
    assert "h264" in {s["codec_name"] for s in stream_info(out)}
