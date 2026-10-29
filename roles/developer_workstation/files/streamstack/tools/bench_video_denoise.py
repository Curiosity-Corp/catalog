#!/usr/bin/env python3
"""Benchmark video denoisers for the noisy Canon feed (#92): quality and cost per filter.

A real photo (the Canon live-view fixture) pans slowly, so the temporal filters face motion and not a frozen scene.
That is the clean reference C. Noise is added to it at each `--noise` level (ffmpeg's `noise` filter, temporal and
uniform, a stand-in for sensor noise at high ISO). Every candidate runs on the noisy clip and is scored against C:
PSNR and SSIM (ffmpeg's filters, over all planes) with a border cut off, because `nlmeans_vulkan` writes garbage into
its outermost rows and columns (a filter bug seen on ffmpeg 8.0.1 with Mesa 26.0). Cost comes from a second run that
discards the output: the ffmpeg process's CPU time and GPU engine time (kernel DRM fdinfo, no privilege) per frame.
Nothing here touches a camera, a loopback device or the engine; run it `nice`d (it uses the render node).

    python3 tools/bench_video_denoise.py [--size 1280x720] [--noise 12,25] [--frames 120]

Prints one JSON line per candidate and noise level, then a table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PHOTO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "canon_liveview_1056x704.jpg"
BORDER = 8  # pixels cut from every side before scoring (the nlmeans_vulkan edge bug)
VAAPI_DEVICE = ("-vaapi_device", "/dev/dri/renderD128")
VAAPI = "format=nv12,hwupload,{f},hwdownload,format=nv12,format=yuv420p"
VULKAN = "hwupload,{f},hwdownload,format=yuv420p"

# name -> (extra input args, filter chain)
CANDIDATES: dict[str, tuple[tuple[str, ...], str]] = {
    "none": ((), "null"),
    "hqdn3d": ((), "hqdn3d=4:3:6:4.5"),
    "atadenoise": ((), "atadenoise=0a=0.02:0b=0.04:1a=0.02:1b=0.04:2a=0.02:2b=0.04"),
    "denoise_vaapi_16": (VAAPI_DEVICE, VAAPI.format(f="denoise_vaapi=denoise=16")),
    "denoise_vaapi_32": (VAAPI_DEVICE, VAAPI.format(f="denoise_vaapi=denoise=32")),
    "nlmeans_vulkan_s3_p3_r5": (("-init_hw_device", "vulkan"), VULKAN.format(f="nlmeans_vulkan=s=3:p=3:r=5")),
    "nlmeans_vulkan_s5_p3_r7": (("-init_hw_device", "vulkan"), VULKAN.format(f="nlmeans_vulkan=s=5:p=3:r=7")),
}


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


def ffmpeg(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "info" if capture else "error", *args]
    return subprocess.run(argv, check=not capture, capture_output=capture, text=True)


def raw_input(size: tuple[int, int], path: Path) -> list[str]:
    width, height = size
    return [
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        "30",
        "-i",
        str(path),
    ]


def prepare(size: tuple[int, int], frames: int, levels: list[int], directory: Path) -> tuple[Path, dict[int, Path]]:
    """The clean panning clip and one noisy copy per level, as raw yuv420p."""
    width, height = size
    clean = directory / "clean.yuv"
    crop = f"crop={width}:{height}:x='t*{width // 40}':y='t*2'"
    pan = f"scale={width * 5 // 4}:{height * 5 // 4}:flags=lanczos,{crop}"
    ffmpeg(
        *("-y", "-loop", "1", "-framerate", "30", "-i", str(PHOTO)),
        *("-vf", f"{pan},format=yuv420p", "-frames:v", str(frames), "-f", "rawvideo", str(clean)),
    )
    noisy = {}
    for level in levels:
        path = directory / f"noisy{level}.yuv"
        ffmpeg(
            "-y",
            *raw_input(size, clean),
            *("-vf", f"noise=alls={level}:allf=t+u:all_seed=7", "-f", "rawvideo", str(path)),
        )
        noisy[level] = path
    return clean, noisy


def quality(size: tuple[int, int], clean: Path, candidate: Path) -> dict:
    width, height = size
    cut = f"crop={width - 2 * BORDER}:{height - 2 * BORDER}:{BORDER}:{BORDER}"
    scores: dict = {}
    for name, pattern in (("psnr", r"PSNR .*average:([0-9.]+|inf)"), ("ssim", r"SSIM .* All:([0-9.]+)")):
        graph = f"[0:v]{cut}[a];[1:v]{cut}[b];[a][b]{name}"
        done = ffmpeg(
            *raw_input(size, candidate),
            *raw_input(size, clean),
            *("-filter_complex", graph, "-f", "null", "-"),
            capture=True,
        )
        found = re.search(pattern, done.stderr)
        scores[name] = None if not found or found.group(1) == "inf" else float(found.group(1))
    return scores


def cost(name: str, size: tuple[int, int], noisy: Path, frames: int) -> dict:
    """CPU and GPU time per frame of one candidate: output discarded, sampled from frame 10 to ten before the end."""
    hw, chain = CANDIDATES[name]
    total = frames * 2
    argv = ["ffmpeg", "-v", "error", "-nostdin", *hw, "-stream_loop", "-1", *raw_input(size, noisy)]
    argv += ["-vf", chain, "-frames:v", str(total), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stdout is None:
        raise SystemExit("ffmpeg gave no output pipe")
    frame_bytes = size[0] * size[1] * 3 // 2
    got, first, last = 0, None, None
    stop = total - 10
    while proc.stdout.read(frame_bytes):
        got += 1
        if got == 10:
            first = (time.monotonic(), drm_ns(proc.pid), cpu_s(proc.pid))
        if got == stop:
            last = (time.monotonic(), drm_ns(proc.pid), cpu_s(proc.pid))
    proc.wait()
    if first is None or last is None:
        raise SystemExit(f"{name}: the filter chain gave {got} frames: {proc.stderr.read().decode()[-300:]}")
    measured = stop - 10
    return {
        "fps": round(measured / (last[0] - first[0]), 1),
        "gpu_ms_per_frame": round((last[1] - first[1]) / 1e6 / measured, 2),
        "cpu_ms_per_frame": round((last[2] - first[2]) * 1000 / measured, 2),
    }


def run_candidate(name: str, size: tuple[int, int], noisy: Path, clean: Path, directory: Path, frames: int) -> dict:
    hw, chain = CANDIDATES[name]
    out = directory / f"out_{name}.yuv"
    ffmpeg("-y", *hw, *raw_input(size, noisy), *("-vf", chain, "-frames:v", str(frames)), "-f", "rawvideo", str(out))
    row = {"filter": name, **quality(size, clean, out)}
    out.unlink()
    return row | cost(name, size, noisy, frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--noise", default="12,25", help="noise strengths (ffmpeg `noise` alls), comma separated")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--filters", default=",".join(CANDIDATES), help="comma separated; default: every candidate")
    opts = parser.parse_args()
    width, height = (int(v) for v in opts.size.split("x"))
    levels = [int(v) for v in opts.noise.split(",")]
    with tempfile.TemporaryDirectory(prefix="bench-video-denoise-") as tmp:
        directory = Path(tmp)
        clean, noisy = prepare((width, height), opts.frames, levels, directory)
        rows = []
        for level in levels:
            for name in opts.filters.split(","):
                row = {
                    "noise": level,
                    **run_candidate(name, (width, height), noisy[level], clean, directory, opts.frames),
                }
                rows.append(row)
                print(json.dumps(row), flush=True)
    print(f"\n{'noise':>5} {'filter':<26}{'PSNR dB':>9}{'SSIM':>8}{'fps':>8}{'GPU ms':>9}{'CPU ms':>9}")
    for r in rows:
        print(
            f"{r['noise']:>5} {r['filter']:<26}{r['psnr'] or float('inf'):>9.2f}{r['ssim']:>8.3f}{r['fps']:>8}"
            f"{r['gpu_ms_per_frame']:>9}{r['cpu_ms_per_frame']:>9}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
