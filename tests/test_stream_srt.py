"""SRT path measurement (stream/srt.py) and the SRT input (stream/ingest.py)."""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from streamstack.stream import ffcmd, srt
from streamstack.stream.ingest import SrtIngest, ingest_argv, slug
from streamstack.stream.model import Destination
from streamstack.stream.proc import Child


def reply_to(request: bytes) -> bytes:
    """What an SRT listener answers to an induction request: the caller's socket id as destination."""
    socket_id = struct.unpack_from(">I", request, 16 + 24)[0]
    header = struct.pack(">IIII", srt.CONTROL_HANDSHAKE, 0, 0, socket_id)
    body = struct.pack(">IHHIIIIII", 5, 0, 0x4A17, 0, 1500, 8192, srt.INDUCTION, 7, 1234)
    return header + body + bytes(16)


@contextmanager
def responder(answer: bool = True, delay_s: float = 0.0) -> Iterator[int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.1)
    done = threading.Event()

    def serve() -> None:
        while not done.is_set():
            try:
                data, peer = sock.recvfrom(2048)
            except OSError:
                continue
            if answer:
                time.sleep(delay_s)
                sock.sendto(reply_to(data), peer)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        done.set()
        thread.join(2)
        sock.close()


def test_induction_request_is_a_64_byte_handshake_control_packet() -> None:
    packet = srt.induction_request(0xCAFE, 123456)
    assert len(packet) == srt.HANDSHAKE_SIZE
    assert struct.unpack_from(">I", packet, 0)[0] == srt.CONTROL_HANDSHAKE
    assert struct.unpack_from(">I", packet, 16 + 20)[0] == srt.INDUCTION
    assert struct.unpack_from(">I", packet, 16 + 24)[0] == 0xCAFE


def test_reply_is_matched_on_type_and_socket_id() -> None:
    request = srt.induction_request(42, 0)
    assert srt.is_induction_reply(reply_to(request), 42)
    assert not srt.is_induction_reply(reply_to(request), 43)
    assert not srt.is_induction_reply(b"\x00" * 10, 42)
    assert not srt.is_induction_reply(b"\x00" * 64, 42)


def test_round_trip_is_measured_against_a_real_udp_responder() -> None:
    with responder(delay_s=0.03) as port:
        rtt = srt.induction_rtt_ms("127.0.0.1", port, timeout=1.0)
    assert rtt is not None and 25 <= rtt < 500


def test_no_answer_is_none_and_counts_as_loss() -> None:
    with responder(answer=False) as port:
        assert srt.induction_rtt_ms("127.0.0.1", port, timeout=0.15) is None
        result = srt.probe("127.0.0.1", port, count=2, gap_s=0.0, timeout=0.1)
    assert result.answered == 0 and result.loss_percent == 100.0 and result.rtt_ms is None


def test_probe_uses_the_median_and_reports_loss() -> None:
    values = iter([10.0, None, 200.0, 12.0])
    result = srt.probe("h", 1, count=4, gap_s=0.0, rtt=lambda *_: next(values))
    assert result.rtts_ms == [10.0, 200.0, 12.0] and result.rtt_ms == 12.0 and result.loss_percent == 25.0


@pytest.mark.parametrize(
    ("rtt", "loss", "expected"),
    [(5, 0, 80), (50, 0, 200), (50, 5, 250), (50, 15, 300), (50, 40, 350), (5000, 0, 8000)],
)
def test_recommended_latency(rtt: float, loss: float, expected: int) -> None:
    assert srt.recommended_latency_ms(rtt, loss) == expected


def fixed(rtts: list[float], sent: int | None = None):
    return lambda _h, _p: srt.Probe(sent=sent or len(rtts), rtts_ms=list(rtts))


def test_fixed_latency_is_never_changed_by_measurements() -> None:
    link = srt.SrtLink("h", 1, "caller", 250, prober=fixed([100.0]))
    link.measure()
    assert link.resolve() == 250
    stats = link.stats()
    assert stats["latency_ms"] == 250 and stats["recommended_ms"] == 400 and stats["auto"] is False


def test_auto_latency_follows_the_measured_round_trip() -> None:
    link = srt.SrtLink("h", 1, "caller", "auto", prober=fixed([40.0, 42.0, 44.0]))
    link.measure()
    assert link.resolve() == 168
    stats = link.stats()
    assert stats["latency_ms"] == 168 and stats["rtt_ms"] == 42.0 and stats["rtt_min_ms"] == 40.0
    assert stats["probe_loss_percent"] == 0.0 and stats["configured_ms"] is None


def test_auto_latency_falls_back_to_default_without_an_answer() -> None:
    link = srt.SrtLink("h", 1, "caller", "auto", prober=fixed([], sent=3))
    link.measure()
    assert link.resolve() == srt.DEFAULT_LATENCY_MS
    stats = link.stats()
    assert stats["rtt_ms"] is None and stats["probe_loss_percent"] == 100.0 and "did not answer" in str(stats["hint"])


def test_listener_cannot_measure_and_says_so() -> None:
    link = srt.SrtLink("0.0.0.0", 9000, "listener", "auto")  # noqa: S104
    link.start()  # must not spawn a monitor or probe anything
    assert link.resolve() == srt.DEFAULT_LATENCY_MS
    stats = link.stats()
    assert stats["measurable"] is False and "Listening" in str(stats["hint"])
    link.stop()


def test_monitor_probes_in_the_background_and_resolve_waits_for_the_first_result() -> None:
    link = srt.SrtLink("h", 1, "caller", "auto", prober=fixed([30.0]))
    link.start()
    try:
        assert link.resolve(wait_s=2.0) == 120
    finally:
        link.stop()


def test_stale_measurement_is_taken_again() -> None:
    now = [1000.0]
    calls: list[int] = []

    def prober(_h: str, _p: int) -> srt.Probe:
        calls.append(1)
        return srt.Probe(sent=1, rtts_ms=[50.0])

    link = srt.SrtLink("h", 1, "caller", "auto", prober=prober, wall=lambda: now[0])
    link.measure()
    now[0] += srt.FRESH_S + 5
    assert link._fresh() is None


AUTO = {"id": "a", "kind": "srt", "url": "srt://h.test:9000", "options": {"latency_ms": "auto"}}


def test_model_accepts_auto_latency_and_rejects_nonsense() -> None:
    dest = Destination.from_dict(AUTO)
    assert dest.options["latency_ms"] == "auto"
    with pytest.raises(ValueError, match="atency"):
        Destination.from_dict({"id": "a", "kind": "srt", "url": "srt://h.test:9000", "options": {"latency_ms": "fast"}})


def test_srt_url_takes_a_resolved_latency_over_the_option() -> None:
    dest = Destination.from_dict(AUTO)
    assert "latency=168000" in ffcmd.srt_url(dest, latency_ms=168)


def test_slug_and_argv_for_the_ingest() -> None:
    assert slug("Backup / Camera 1!") == "Backup-Camera-1"
    assert slug("***") == "srt-backup"
    argv = ingest_argv("ffmpeg", 9100, Path("/x/y.mkv"), 300, "secret-phrase")
    url = argv[argv.index("-i") + 1]
    assert url.startswith("srt://0.0.0.0:9100?") and "mode=listener" in url and "latency=300000" in url
    assert "passphrase=secret-phrase" in url and argv[-1] == "/x/y.mkv" and "copy" in argv


# -- real ffmpeg ----------------------------------------------------------------------------------------------


def _ffmpeg_has(*needles: str) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    text = ""
    for flag in ("-encoders", "-protocols"):
        text += subprocess.run([ffmpeg, "-hide_banner", flag], capture_output=True, text=True, check=False).stdout
    return all(n in text for n in needles)


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_has("libx264", "srt"), reason="ffmpeg with libx264 and libsrt is needed")


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sender(port: int, seconds: int, passphrase: str = "") -> list[str]:
    query = f"mode=caller&latency=120000{f'&passphrase={passphrase}' if passphrase else ''}"
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "mpegts",
        f"srt://127.0.0.1:{port}?{query}",
    ]  # fmt: skip


def wait_for(check, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


def run_sender(port: int, seconds: int, passphrase: str = "") -> None:
    subprocess.run(sender(port, seconds, passphrase), check=False, timeout=60, capture_output=True)


@needs_ffmpeg
def test_ingest_records_a_real_sender_and_listens_again(tmp_path: Path) -> None:
    port = free_udp_port()
    ingest = SrtIngest("ffmpeg", port, tmp_path, "Backup", Child, latency_ms=120)
    ingest.start()
    try:
        assert ingest.status()["state"] == "waiting"
        time.sleep(1.0)
        run_sender(port, 3)
        assert wait_for(lambda: ingest.status()["bytes"] > 0), ingest.status()
        assert wait_for(lambda: ingest.tick() or ingest.status()["state"] == "waiting")
        first = Path(str(ingest.status()["file"]))
        assert first.exists() and first.stat().st_size > 1000
        time.sleep(1.2)
        ingest.tick()
        time.sleep(1.0)
        run_sender(port, 2)
        assert wait_for(lambda: (ingest.tick() or True) and len(ingest.status()["files"]) == 2), ingest.status()  # type: ignore[arg-type]
        assert ingest.status()["files"][1] != str(first)  # type: ignore[index]
    finally:
        ingest.stop()
    assert ingest.status()["state"] == "stopped"


@needs_ffmpeg
def test_ingest_with_passphrase_rejects_a_sender_with_the_wrong_one(tmp_path: Path) -> None:
    port = free_udp_port()
    ingest = SrtIngest("ffmpeg", port, tmp_path, "Locked", Child, latency_ms=120, passphrase="correct-horse-1")
    ingest.start()
    try:
        time.sleep(1.0)
        run_sender(port, 2, passphrase="wrong-passphrase")
        assert ingest.status()["bytes"] == 0
        assert ingest.status()["encrypted"] is True
    finally:
        ingest.stop()
