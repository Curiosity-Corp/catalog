#!/usr/bin/env python3
"""Measure what an encoder costs here: latency from capture to the relay, CPU, and GPU engine time.

    python3 tools/bench_stream.py                       # every working encoder, 720p30 and 1080p30, 12 s each
    python3 tools/bench_stream.py --encoders av1_vaapi,hevc_vaapi,h264_vaapi --seconds 20 --json out.json

It runs the same ffmpeg command the stream service runs (`ffcmd.video_generation_argv`) on a synthetic, wall-clock
paced test picture (lavfi `testsrc2`), reads its Matroska output the way the relay does, and reports:

- latency: for each coded frame, the time it reached the relay minus the time the frame was stamped as captured.
  That is capture-to-relay: filter chain, upload, encode, mux and pipe. It excludes the camera and the network.
- cpu: the encoder process's own CPU time as a percentage of one core, minus the same chain with no encoder
  (the synthetic picture generator), so the figure is the encoder's, not the test source's.
- gpu: DRM engine busy percentage read from the process's `/proc/<pid>/fdinfo` (no root or CAP_PERFMON needed),
  per engine (render, video, video-enhance, copy).
- kbps and fps actually produced.

The test only ever encodes to a pipe it reads itself: nothing is sent anywhere and no capture device is opened.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from streamstack.stream import ffcmd, mkv
from streamstack.stream.ffcmd import Source
from streamstack.stream.model import EncoderSettings
from streamstack.stream.probe import probe_host

TICKS = os.sysconf("SC_CLK_TCK")
SIZES = {"720p30": (1280, 720, 30.0), "1080p30": (1920, 1080, 30.0)}
WARMUP_S = 3.0


def cpu_seconds(pid: int) -> float:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return 0.0
    return (int(fields[11]) + int(fields[12])) / TICKS  # utime + stime


def gpu_engines(pid: int) -> dict[str, tuple[float, float]]:
    """{engine: (busy, total)} from the DRM fdinfo of every client the process has open (i915 and xe formats)."""
    seen: set[str] = set()
    out: dict[str, tuple[float, float]] = {}
    fdinfo = Path(f"/proc/{pid}/fdinfo")
    try:
        entries = list(fdinfo.iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            text = entry.read_text()
        except OSError:
            continue
        if "drm-driver" not in text:
            continue
        client = re.search(r"drm-client-id:\s*(\d+)", text)
        pdev = re.search(r"drm-pdev:\s*(\S+)", text)
        key = f"{pdev.group(1) if pdev else ''}-{client.group(1) if client else entry.name}"
        if key in seen:
            continue
        seen.add(key)
        for engine, value in re.findall(r"drm-engine-([a-z-]+):\s*(\d+) ns", text):
            busy, total = out.get(engine, (0.0, 0.0))
            out[engine] = (busy + float(value), total)
        for engine, value in re.findall(r"drm-cycles-([a-z0-9-]+):\s*(\d+)", text):
            total_match = re.search(rf"drm-total-cycles-{re.escape(engine)}:\s*(\d+)", text)
            if total_match:
                busy, total = out.get(f"{engine}(cycles)", (0.0, 0.0))
                out[f"{engine}(cycles)"] = (busy + float(value), total + float(total_match.group(1)))
    return out


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))] if ordered else 0.0


def run_once(argv: list[str], seconds: float, epoch: float) -> dict[str, Any]:
    """Run one ffmpeg for WARMUP_S + `seconds`, sampling it after the warm-up."""
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
    )
    demux = mkv.Demuxer()
    latencies: list[float] = []
    bytes_out = 0
    frames = 0
    measuring = threading.Event()
    done = threading.Event()

    def reader() -> None:
        nonlocal bytes_out, frames
        fd = proc.stdout.fileno() if proc.stdout else -1
        while not done.is_set() and fd >= 0:
            data = os.read(fd, 65536)
            if not data:
                return
            arrived = time.time()
            for event in demux.feed(data):
                if isinstance(event, mkv.Block) and measuring.is_set():
                    latencies.append((arrived - epoch) * 1000 - event.ts_ms)
                    bytes_out += event.size
                    frames += 1

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    result: dict[str, Any] = {"ok": False}
    try:
        time.sleep(WARMUP_S)
        if proc.poll() is not None:
            return {"ok": False, "error": f"ffmpeg exited with status {proc.returncode} during warm-up"}
        pid = proc.pid
        cpu0, gpu0, t0 = cpu_seconds(pid), gpu_engines(pid), time.monotonic()
        measuring.set()
        time.sleep(seconds)
        measuring.clear()
        cpu1, gpu1, t1 = cpu_seconds(pid), gpu_engines(pid), time.monotonic()
        wall = t1 - t0
        gpu: dict[str, float] = {}
        for engine, (busy1, total1) in gpu1.items():
            busy0, total0 = gpu0.get(engine, (0.0, 0.0))
            if engine.endswith("(cycles)"):
                span = total1 - total0
                gpu[engine.removesuffix("(cycles)")] = round(100 * (busy1 - busy0) / span, 1) if span > 0 else 0.0
            else:
                gpu[engine] = round(100 * (busy1 - busy0) / (wall * 1e9), 1)
        result = {
            "ok": frames > 0,
            "cpu_pct": round(100 * (cpu1 - cpu0) / wall, 1),
            "gpu_pct": gpu,
            "fps": round(frames / wall, 1),
            "kbps": round(bytes_out * 8 / wall / 1000),
            "latency_ms": {
                "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
                "p95": round(percentile(latencies, 95), 1),
                "max": round(max(latencies), 1) if latencies else 0.0,
                "min": round(min(latencies), 1) if latencies else 0.0,
            },
        }
        if not frames:
            result["error"] = "no frames arrived"
    finally:
        done.set()
        try:
            os.killpg(proc.pid, 15)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.wait()
        thread.join(timeout=2)
    return result


def baseline_cpu(ffmpeg: str, width: int, height: int, fps: float, seconds: float) -> float:
    """CPU percent of the synthetic source alone (the same chain without an encoder), to subtract."""
    argv = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps:g}",
        "-vf", "realtime,format=nv12", "-f", "null", "-",
    ]  # fmt: skip
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(WARMUP_S)
        cpu0, t0 = cpu_seconds(proc.pid), time.monotonic()
        time.sleep(seconds)
        cpu1, t1 = cpu_seconds(proc.pid), time.monotonic()
    finally:
        os.killpg(proc.pid, 15)
        proc.wait(timeout=5)
    return 100 * (cpu1 - cpu0) / (t1 - t0)


def bench(encoder_names: list[str] | None, seconds: float, sizes: list[str]) -> dict[str, Any]:
    host = probe_host()
    if not host.ffmpeg:
        raise SystemExit("ffmpeg is not installed")
    working = {e.name: e for e in host.working()}
    names = encoder_names or [n for n in working]
    report: dict[str, Any] = {"ffmpeg": host.version, "cores": host.cores, "seconds": seconds, "results": []}
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
            row = run_once(argv, seconds, epoch)
            row.update(encoder=name, codec=enc.codec, backend=enc.backend, size=label)
            if row.get("ok"):
                row["cpu_net_pct"] = round(max(0.0, row["cpu_pct"] - baselines[label]), 1)
            report["results"].append(row)
            print(json.dumps(row), file=sys.stderr, flush=True)
    return report


def table(report: dict[str, Any]) -> str:
    lines = [
        "| Encoder | Size | Latency p50 / p95 / max (ms) | CPU net (% of one core) | GPU (%) | kbps | fps |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["results"]:
        if not row.get("ok"):
            lines.append(f"| {row['encoder']} | {row.get('size', '')} | failed: {row.get('error', '')} | | | | |")
            continue
        gpu = ", ".join(f"{k} {v}" for k, v in row["gpu_pct"].items() if v >= 0.5) or "n/a"
        lat = row["latency_ms"]
        lines.append(
            f"| {row['encoder']} | {row['size']} | {lat['p50']} / {lat['p95']} / {lat['max']} | {row['cpu_net_pct']} "
            f"| {gpu} | {row['kbps']} | {row['fps']} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--encoders", default="", help="comma list (default: every working encoder)")
    parser.add_argument("--sizes", default="720p30,1080p30")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()
    names = [n for n in args.encoders.split(",") if n] or None
    sizes = [s for s in args.sizes.split(",") if s in SIZES]
    report = bench(names, args.seconds, sizes)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    print(table(report))
    print(f"\nsynthetic source alone: {report['source_cpu_pct']} % of one core (subtracted above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
