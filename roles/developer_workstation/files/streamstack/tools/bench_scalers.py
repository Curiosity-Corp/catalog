#!/usr/bin/env python3
"""Benchmark the libplacebo scalers the engine may select (STREAMSTACK_ENGINE_SCALER): quality and GPU cost.

For each scaler a still photo is scaled to the program size (R) and a copy of it at `--ratio` of the side (S, made
from R with ffmpeg's lanczos; both limited-range BT.709 yuv420p, so nothing but the scaler differs) goes through the
real engine path (`hwupload, libplacebo=upscaler=...:downscaler=..., hwdownload`) back to R's size for `--frames`
frames. A ratio under 1 is the zoom case of the framing crop (a window scaled up to the program size); above 1 it is a
round trip through a larger picture (the downscale case, a 4K camera into a 1080p program). PSNR and SSIM (ffmpeg's
filters, over all planes) say how close the output is to R, and the GPU engine time and CPU time of the ffmpeg process
(kernel DRM fdinfo, /proc) say what it costs per frame. Nothing here touches a camera or a loopback device.

    PYTHONPATH=src python3 tools/bench_scalers.py [--size 1920x1080] [--ratio 0.8] [--frames 240]

Prints one JSON line per scaler, then a table. Run it `nice`d.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from streamstack.engine.graph import SCALERS

PHOTO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "canon_liveview_1056x704.jpg"


def drm_ns(pid: int) -> int:
    total = 0
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            text = Path(f"/proc/{pid}/fdinfo/{fd}").read_text()
        except OSError:
            continue
        if "drm-driver" not in text:
            continue
        for line in text.splitlines():
            if line.startswith("drm-engine-") and not line.startswith("drm-engine-capacity"):
                total += int(line.split(":")[1].split()[0])
    return total


def cpu_s(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


TAGS = "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"


def prepare(size: tuple[int, int], ratio: float, directory: Path) -> tuple[Path, Path, tuple[int, int]]:
    """The reference picture R and the smaller source S (made from R), as raw yuv420p files in limited range."""
    reference, small = directory / "ref.yuv", directory / "small.yuv"
    width, height = size
    sw, sh = round(width * ratio / 2) * 2, round(height * ratio / 2) * 2
    to_yuv = f"scale=out_color_matrix=bt709:out_range=tv:flags=lanczos,format=yuv420p,{TAGS}"
    subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-y", "-i", str(PHOTO)),
            *("-vf", f"scale={width}:{height}:flags=lanczos,{to_yuv}", "-f", "rawvideo", str(reference)),
        ],
        check=True,
    )
    subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p"),
            *("-video_size", f"{width}x{height}", "-i", str(reference)),
            *("-vf", f"scale={sw}:{sh}:flags=lanczos", "-f", "rawvideo", str(small)),
        ],
        check=True,
    )
    return reference, small, (sw, sh)


def run_one(scaler: str, size: tuple[int, int], source: tuple[int, int], files: tuple[Path, Path], frames: int) -> dict:
    reference, small = files
    width, height = size
    chain = f"{TAGS},hwupload,libplacebo=w={width}:h={height}:format=yuv420p:upscaler={scaler}:downscaler={scaler}"
    chain += ",hwdownload,format=yuv420p"
    argv = ["ffmpeg", "-v", "error", "-init_hw_device", "vulkan", "-stream_loop", "-1", "-f", "rawvideo"]
    argv += ["-pix_fmt", "yuv420p", "-video_size", f"{source[0]}x{source[1]}", "-framerate", "60", "-i", str(small)]
    argv += ["-vf", chain, "-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stdout is None:
        raise SystemExit("ffmpeg gave no output pipe")
    frame_bytes = width * height * 3 // 2
    first = proc.stdout.read(frame_bytes)  # frame 1 is kept for the quality comparison
    started, gpu0, cpu0 = time.monotonic(), drm_ns(proc.pid), cpu_s(proc.pid)
    got = 1
    stop_at = frames - 10  # sample before the last frames: the process is gone (and its counters with it) after them
    gpu1, cpu1, ended = gpu0, cpu0, started
    while proc.stdout.read(frame_bytes):
        got += 1
        if got == stop_at:
            gpu1, cpu1, ended = drm_ns(proc.pid), cpu_s(proc.pid), time.monotonic()
    proc.wait()
    measured = stop_at - 1
    wall = max(ended - started, 1e-6)
    if len(first) != frame_bytes or got < frames or frames < 30:
        raise SystemExit(f"{scaler}: the engine path gave {got} frames: {proc.stderr.read().decode()[-300:]}")
    return {
        "scaler": scaler,
        "frames": got,
        "fps": round(measured / wall, 1),
        "gpu_ms_per_frame": round((gpu1 - gpu0) / 1e6 / measured, 2),
        "cpu_ms_per_frame": round((cpu1 - cpu0) * 1000 / measured, 2),
        **score(reference, first, size),
    }


def score(reference: Path, raw_frame: bytes, size: tuple[int, int]) -> dict:
    """PSNR and SSIM of one raw yuv420p frame against the reference frame (ffmpeg's filters)."""
    width, height = size
    argv = ["ffmpeg", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", f"{width}x{height}"]
    argv += ["-i", "-", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", f"{width}x{height}"]
    argv += ["-i", str(reference), "-filter_complex"]
    psnr = subprocess.run(
        [*argv, "[0:v][1:v]psnr", "-f", "null", "-"], input=raw_frame, capture_output=True, check=True
    )
    ssim = subprocess.run(
        [*argv, "[0:v][1:v]ssim", "-f", "null", "-"], input=raw_frame, capture_output=True, check=True
    )
    p = re.search(r"PSNR .*average:([0-9.]+|inf)", psnr.stderr.decode())
    s = re.search(r"SSIM .* All:([0-9.]+)", ssim.stderr.decode())
    value = float(p.group(1)) if p and p.group(1) != "inf" else None
    return {"psnr_db": value, "ssim": float(s.group(1)) if s else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", default="1920x1080")
    parser.add_argument("--ratio", type=float, default=0.8, help="the source's side as a share of the output's")
    parser.add_argument("--frames", type=int, default=240)
    parser.add_argument("--scalers", default=",".join(SCALERS), help="comma separated; default: every allowed one")
    parser.add_argument("--workdir", default=".", help="where the two scratch pictures go")
    opts = parser.parse_args()
    width, height = (int(v) for v in opts.size.split("x"))
    directory = Path(opts.workdir)
    reference, small, source = prepare((width, height), opts.ratio, directory)
    rows = []
    for name in opts.scalers.split(","):
        row = run_one(name, (width, height), source, (reference, small), opts.frames)
        rows.append(row)
        print(json.dumps(row), flush=True)
    print(f"\n{'scaler':<18}{'PSNR dB':>10}{'SSIM':>9}{'GPU ms/frame':>14}{'CPU ms/frame':>14}")
    for row in rows:
        print(
            f"{row['scaler']:<18}{row['psnr_db']!s:>10}{row['ssim']!s:>9}"
            f"{row['gpu_ms_per_frame']:>14}{row['cpu_ms_per_frame']:>14}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
