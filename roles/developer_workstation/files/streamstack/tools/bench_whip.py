#!/usr/bin/env python3
"""Measure what publishing over WHIP costs here: the hardware encode, StreamStack's WHIP path, and the delay.

    python3 tools/bench_whip.py                                    # every working encoder, 720p30 and 1080p30
    python3 tools/bench_whip.py --encoders h264_vaapi,hevc_vaapi,av1_vaapi --seconds 20 --json out.json

It runs the same ffmpeg command the stream service runs (`ffcmd.video_generation_argv`) on a wall-clock paced test
picture, feeds its Matroska into a real `WhipSink`, and publishes over real ICE and DTLS-SRTP on 127.0.0.1 to a
receiver that mirrors whatever codec was offered and only timestamps the RTP packets as they arrive (no decode, so
HEVC and AV1 can be measured too). Per case it reports:

- encode_ms: capture to the relay (the encoder's own latency; what `bench_stream.py` reports).
- handoff_ms: capture to the moment the WHIP session takes the frame off its queue (encode plus packetize plus queue).
- arrival_ms: capture to the last RTP packet of the frame reaching the receiver's socket (adds SRTP and the loopback
  hop). This is the delay StreamStack adds up to the wire on this machine; the network, the server and the viewer's
  jitter buffer come on top and are not measured here.
- cpu: the encoder process, and StreamStack's WHIP path (the thread that reads, demuxes and packetizes plus the
  thread that encrypts and sends), each as a percent of one core; gpu: DRM engine busy percent of the encoder.
- kbps, fps and packet loss as the receiver counted them.

Nothing is sent beyond 127.0.0.1 and no capture device is opened. Needs the `whip` extra.
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiortc import RTCCertificate, RTCIceGatherer, RTCIceTransport
from aiortc.rtcdtlstransport import RTCDtlsFingerprint, RTCDtlsParameters, RTCDtlsTransport
from aiortc.rtcicetransport import RTCIceParameters
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from bench_stream import SIZES, TICKS, WARMUP_S, baseline_cpu, cpu_seconds, gpu_engines, percentile

from streamstack.stream import ffcmd, mkv
from streamstack.stream.ffcmd import Source
from streamstack.stream.model import EncoderSettings
from streamstack.stream.probe import probe_host
from streamstack.stream.whip import session as whip_session
from streamstack.stream.whip import support
from streamstack.stream.whip.sink import WhipSink


def thread_cpu(tid: int) -> float:
    """CPU seconds of one thread of this process."""
    try:
        fields = Path(f"/proc/self/task/{tid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return 0.0
    return (int(fields[11]) + int(fields[12])) / TICKS


@dataclass
class Arrivals:
    packets: int = 0
    lost: int = 0
    bytes: int = 0
    frames: list[tuple[float, int]] = field(default_factory=list)  # (arrival wall time, RTP timestamp) per frame end
    _last_seq: int | None = None


class CountingDtls(RTCDtlsTransport):
    """A DTLS transport that timestamps every RTP packet the instant it is decrypted."""

    arrivals: Arrivals

    async def _handle_rtp_data(self, data: bytes, arrival_time_ms: int) -> None:
        now = time.time()
        if len(data) >= 12 and (data[0] >> 6) == 2:
            seq = int.from_bytes(data[2:4], "big")
            got = self.arrivals
            got.packets += 1
            got.bytes += len(data)
            if got._last_seq is not None:
                gap = (seq - got._last_seq) & 0xFFFF
                if 1 < gap < 0x8000:
                    got.lost += gap - 1
            got._last_seq = seq
            if data[1] & 0x80:  # the marker bit ends a frame
                got.frames.append((now, int.from_bytes(data[4:8], "big")))


class Receiver:
    """A WHIP endpoint that answers any offer by mirroring it and counts what arrives."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.arrivals = Arrivals()
        self.tid = 0
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.tid = threading.get_native_id()
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True, name="bench-receiver").start()
        ready.wait(5)
        self._dtls: CountingDtls | None = None
        self._ice: RTCIceTransport | None = None
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return

            def reply(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                offer = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                answer = asyncio.run_coroutine_threadsafe(receiver.accept(offer), receiver.loop).result(20)
                self.reply(201, {"Content-Type": "application/sdp", "Location": "/whip/1"}, answer.encode())

            def do_DELETE(self) -> None:
                asyncio.run_coroutine_threadsafe(receiver.close(), receiver.loop).result(10)
                self.reply(200, {})

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/whip"

    async def accept(self, offer: str) -> str:
        gatherer = RTCIceGatherer()
        await gatherer.gather()
        params = gatherer.getLocalParameters()
        ice = RTCIceTransport(gatherer)
        certificate = RTCCertificate.generateCertificate()
        dtls = CountingDtls(ice, [certificate])
        dtls.arrivals = self.arrivals
        dtls._set_role("server")
        self._ice, self._dtls = ice, dtls
        lines = offer.splitlines()
        ufrag = next(line.split(":", 1)[1] for line in lines if line.startswith("a=ice-ufrag:"))
        password = next(line.split(":", 1)[1] for line in lines if line.startswith("a=ice-pwd:"))
        fingerprints = [
            RTCDtlsFingerprint(*line.split(":", 1)[1].split(" ", 1))
            for line in lines
            if line.startswith("a=fingerprint:")
        ]
        for line in lines:
            if line.startswith("a=candidate:"):
                candidate = candidate_from_sdp(line.removeprefix("a=candidate:"))
                if candidate.component == 1:
                    await ice.addRemoteCandidate(candidate)
        await ice.addRemoteCandidate(None)
        mine = [f"a=candidate:{candidate_to_sdp(c)}" for c in gatherer.getLocalCandidates() if c.component == 1]
        sha256 = next(f for f in certificate.getFingerprints() if f.algorithm == "sha-256")
        skip = ("a=ice-ufrag", "a=ice-pwd", "a=fingerprint", "a=candidate", "a=end-of-candidates", "a=msid:", "a=ssrc:")
        out: list[str] = []
        for line in lines:
            if line.startswith(skip):
                continue
            if line == "a=sendonly":
                line = "a=recvonly"
            elif line.startswith("a=setup:"):
                line = "a=setup:passive"
            out.append(line)
            if line.startswith("a=mid:"):
                out += [
                    f"a=ice-ufrag:{params.usernameFragment}",
                    f"a=ice-pwd:{params.password}",
                    f"a=fingerprint:sha-256 {sha256.value}",
                    *mine,
                    "a=end-of-candidates",
                ]

        async def connect() -> None:
            await ice.start(RTCIceParameters(ufrag, password))
            await dtls.start(RTCDtlsParameters(fingerprints, "auto"))

        self.loop.create_task(connect())
        return "\r\n".join(out) + "\r\n"

    async def close(self) -> None:
        if self._dtls is not None:
            await self._dtls.stop()
        if self._ice is not None:
            await self._ice.stop()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        try:
            asyncio.run_coroutine_threadsafe(self.close(), self.loop).result(10)
        except Exception as exc:
            print(f"receiver close: {exc}", file=sys.stderr)
        self.loop.call_soon_threadsafe(self.loop.stop)


def run_case(argv: list[str], seconds: float, epoch: float) -> dict[str, Any]:
    receiver = Receiver()
    sink = WhipSink("bench", receiver.url, lambda: "")
    handoff: list[tuple[float, int]] = []
    original = whip_session.Passthrough._next_encoded_frame

    async def tapped(self, codec):
        frame = await original(self, codec)
        if frame is not None and self.track.kind == "video":
            handoff.append((time.time(), frame.timestamp))
        return frame

    whip_session.Passthrough._next_encoded_frame = tapped  # type: ignore[method-assign]
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
    )
    demux = mkv.Demuxer()
    origin_ms: list[int] = []
    encode_lat: list[float] = []
    measuring = threading.Event()
    stop = threading.Event()
    reader_tid: list[int] = []
    result: dict[str, Any] = {"ok": False}

    def reader() -> None:
        reader_tid.append(threading.get_native_id())
        fd = proc.stdout.fileno() if proc.stdout else -1
        sink.open(b"")
        while not stop.is_set():
            data = os.read(fd, 65536)
            if not data:
                return
            arrived = time.time()
            if not origin_ms or measuring.is_set():
                for event in demux.feed(data):
                    if isinstance(event, mkv.Block):
                        if not origin_ms:
                            origin_ms.append(event.ts_ms)
                        if measuring.is_set():
                            encode_lat.append((arrived - epoch) * 1000 - event.ts_ms)
            sink.write(data, 1, False)

    thread = threading.Thread(target=reader, daemon=True, name="bench-reader")
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while sink.state != "live" and time.monotonic() < deadline:
            time.sleep(0.1)
        if sink.state != "live":
            return {"ok": False, "error": f"never connected ({sink.state}: {sink.last_error})"}
        time.sleep(WARMUP_S)
        sink_tid = sink._thread.native_id if sink._thread else 0
        pid = proc.pid
        enc0, gpu0, t0 = cpu_seconds(pid), gpu_engines(pid), time.monotonic()
        rd0, sk0, rc0 = thread_cpu(reader_tid[0]), thread_cpu(sink_tid or 0), thread_cpu(receiver.tid)
        b0, p0, l0 = receiver.arrivals.bytes, receiver.arrivals.packets, receiver.arrivals.lost
        mark_frames = len(receiver.arrivals.frames)
        mark_handoff = len(handoff)
        measuring.set()
        time.sleep(seconds)
        measuring.clear()
        enc1, gpu1, t1 = cpu_seconds(pid), gpu_engines(pid), time.monotonic()
        wall = t1 - t0
        rd1, sk1, rc1 = thread_cpu(reader_tid[0]), thread_cpu(sink_tid or 0), thread_cpu(receiver.tid)
        got = receiver.arrivals
        frames = got.frames[mark_frames:]
        handoffs = handoff[mark_handoff:]
        # RTP timestamps carry a random offset (aiortc's); the sender-side tap pairs them with the block times
        offset = (got.frames[0][1] - handoff[0][1]) & 0xFFFFFFFF if got.frames and handoff else 0
        base = origin_ms[0] if origin_ms else 0

        def captured(frame_ts: int) -> float:
            return epoch + (base + frame_ts / 90.0) / 1000.0  # the block time the frame was stamped with

        arrival = [(t - captured((ts - offset) & 0xFFFFFFFF)) * 1000 for t, ts in frames]
        taken = [(t - captured(ts)) * 1000 for t, ts in handoffs]
        gpu: dict[str, float] = {}
        for engine, (busy1, total1) in gpu1.items():
            busy0, total0 = gpu0.get(engine, (0.0, 0.0))
            if engine.endswith("(cycles)"):
                span = total1 - total0
                gpu[engine.removesuffix("(cycles)")] = round(100 * (busy1 - busy0) / span, 1) if span > 0 else 0.0
            else:
                gpu[engine] = round(100 * (busy1 - busy0) / (wall * 1e9), 1)

        def spread(values: list[float]) -> dict[str, float]:
            if not values:
                return {"p50": 0.0, "p95": 0.0, "max": 0.0}
            return {
                "p50": round(statistics.median(values), 1),
                "p95": round(percentile(values, 95), 1),
                "max": round(max(values), 1),
            }

        lost = got.lost - l0
        total = got.packets - p0 + lost
        result = {
            "ok": len(frames) > 0,
            "encode_ms": spread(encode_lat),
            "handoff_ms": spread(taken),
            "arrival_ms": spread(arrival),
            "cpu_encoder_pct": round(100 * (enc1 - enc0) / wall, 1),
            "cpu_whip_read_packetize_pct": round(100 * (rd1 - rd0) / wall, 1),
            "cpu_whip_send_pct": round(100 * (sk1 - sk0) / wall, 1),
            "cpu_receiver_pct": round(100 * (rc1 - rc0) / wall, 1),
            "gpu_pct": gpu,
            "kbps": round((got.bytes - b0) * 8 / wall / 1000),
            "fps": round(len(frames) / wall, 1),
            "packets": got.packets - p0,
            "loss_pct": round(100 * lost / total, 3) if total else 0.0,
            "whip_state": sink.status()["whip"].get("ice_state"),
        }
    finally:
        stop.set()
        whip_session.Passthrough._next_encoded_frame = original  # type: ignore[method-assign]
        try:
            os.killpg(proc.pid, 15)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.wait()
        thread.join(timeout=3)
        sink.shutdown()
        receiver.stop()
    return result


def bench(encoder_names: list[str] | None, seconds: float, sizes: list[str]) -> dict[str, Any]:
    host = probe_host()
    if not host.ffmpeg:
        raise SystemExit("ffmpeg is not installed")
    working = {e.name: e for e in host.working()}
    names = encoder_names or list(working)
    report: dict[str, Any] = {
        "ffmpeg": host.version,
        "aiortc": support.version(),
        "cores": host.cores,
        "seconds": seconds,
        "results": [],
    }
    baselines = {label: baseline_cpu(host.ffmpeg, *SIZES[label], seconds=min(seconds, 8)) for label in sizes}
    report["source_cpu_pct"] = {k: round(v, 1) for k, v in baselines.items()}
    for name in names:
        enc = working.get(name)
        if enc is None:
            report["results"].append({"encoder": name, "ok": False, "error": "not a working encoder on this host"})
            continue
        for label in sizes:
            width, height, fps = SIZES[label]
            source = Source("lavfi", width=width, height=height, fps=fps)
            settings = EncoderSettings(encoder=name, rate_control="cbr")
            epoch = time.time()
            argv = ffcmd.video_generation_argv(host.ffmpeg, source, enc.choice(), settings, epoch)
            row = run_case(argv, seconds, epoch)
            row.update(encoder=name, codec=enc.codec, backend=enc.backend, size=label)
            if row.get("ok"):
                row["cpu_encoder_net_pct"] = round(max(0.0, row["cpu_encoder_pct"] - baselines[label]), 1)
            report["results"].append(row)
            print(json.dumps(row), file=sys.stderr, flush=True)
    return report


def table(report: dict[str, Any]) -> str:
    lines = [
        "| Encoder | Size | Encode p50 (ms) | To WHIP p50 (ms) | At receiver p50 / p95 / max (ms) "
        "| Encoder CPU net | WHIP CPU read+packetize / send | GPU (%) | kbps | Loss |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["results"]:
        if not row.get("ok"):
            lines.append(f"| {row['encoder']} | {row.get('size', '')} | failed: {row.get('error', '')} | | | | | | | |")
            continue
        gpu = ", ".join(f"{k} {v}" for k, v in row["gpu_pct"].items() if v >= 0.5) or "n/a"
        a = row["arrival_ms"]
        lines.append(
            f"| {row['encoder']} | {row['size']} | {row['encode_ms']['p50']} | {row['handoff_ms']['p50']} "
            f"| {a['p50']} / {a['p95']} / {a['max']} | {row['cpu_encoder_net_pct']} "
            f"| {row['cpu_whip_read_packetize_pct']} / {row['cpu_whip_send_pct']} | {gpu} | {row['kbps']} "
            f"| {row['loss_pct']} % |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--encoders", default="", help="comma list (default: every working encoder)")
    parser.add_argument("--sizes", default="720p30,1080p30")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()
    if not support.available():
        raise SystemExit(f"WHIP is unavailable: {support.check()}")
    names = [n for n in args.encoders.split(",") if n] or None
    sizes = [s for s in args.sizes.split(",") if s in SIZES]
    report = bench(names, args.seconds, sizes)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    print(table(report))
    print(f"\nsynthetic source alone: {report['source_cpu_pct']} % of one core (subtracted from the encoder column)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
