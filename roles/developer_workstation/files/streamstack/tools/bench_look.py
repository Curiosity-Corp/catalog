#!/usr/bin/env python3
"""Measure what the picture looks cost the native engine: CPU, GPU time, latency and frame rate.

A stamped YUYV feed (a real photo, so the sharpen has edges to work on) goes into a real engine child through a pipe,
like `streamstack engine bench` and `tools/bench_blur.py`, but the program picture is read back from a pipe instead of
/dev/video43. This never opens the program camera, the Canon bridge or any live device, so it is safe to run while a
call is in progress (it does use the render node, lightly; run it `nice`d). One scenario per run:

    --look neutral   no look at all
    --look warm      warmth +0.4 (the warmest preset is Golden Hour at 0.4)
    --look sharp     sharpen on, amount 0.18
    --look split     the split-tone LUT grade on (colorcorrect on the CPU, see docs/native-video-engine.md)
    --look all       every slider, sharpen and the crop window (no split-tone)
    --look all+split the same, with the split-tone

`--sharpen gpu|cpu` picks the sharpen implementation on engines that have both (the CPU `cas` filter is what every
look used before the GPU shader). Reported: the engine child's CPU as a share of one core, the GPU engine time it used
per second (from the kernel's per-client DRM fdinfo, which needs no privilege), delivered fps, drops and the feed-to-
program latency (the same instrument as docs/native-video-engine.md). `--png` saves a program frame to look at.

Run it from the tree under test (`PYTHONPATH=src`): to get the "before" numbers, run this same file against a checkout
of the earlier commit; it only uses the engine's public pieces and adapts to the ones the old code does not have.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from streamstack.engine.bench import BITS, BLOCK, STAMP_ROWS, cpu_ticks, read_stamp, stamp_frame
from streamstack.engine.graph import Source, Spec, window_rect
from streamstack.engine.process import VideoEngine

PHOTO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "canon_liveview_1056x704.jpg"

LOOKS: dict[str, dict] = {
    "neutral": {},
    "warm": {"warmth": 0.4},
    "sharp": {"sharpen_enabled": True, "sharpen_amount": 0.18},
    "split": {"lut_enabled": True, "lut_amount": 0.5},
    "all": {
        "brightness": 0.05,
        "contrast": 0.15,
        "gamma": 0.1,
        "saturation": 0.1,
        "warmth": 0.3,
        "sharpen_enabled": True,
        "sharpen_amount": 0.18,
    },
}
LOOKS["all+split"] = {**LOOKS["all"], "lut_enabled": True, "lut_amount": 0.5}
CROP = (0.5, 0.5, 1.25)  # centre x, centre y, zoom


def photo_frame(width: int, height: int) -> bytes:
    """The test photo as a raw YUYV frame, converted by ffmpeg (so the colours are the ones the engine would see)."""
    raw = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-i", str(PHOTO), "-vf", f"scale={width}:{height}"),
            *("-pix_fmt", "yuyv422", "-f", "rawvideo", "-"),
        ],
        check=True,
        capture_output=True,
    ).stdout
    if len(raw) != width * height * 2:
        raise SystemExit(f"ffmpeg gave {len(raw)} bytes for a {width}x{height} photo")
    return raw


def drm_ns(pid: int) -> int | None:
    """GPU engine time a process has used, from the kernel's per-client fdinfo (needs no privilege)."""
    total, seen = 0, False
    try:
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
                    seen = True
    except OSError:
        return None
    return total if seen else None


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 1)


def run(opts: argparse.Namespace) -> dict:
    width, height = (int(v) for v in opts.size.split("x"))
    fps = opts.fps
    look = LOOKS[opts.look]
    feed_r, feed_w = os.pipe()
    out_r, out_w = os.pipe()
    override = (
        *("-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", f"{width}x{height}"),
        *("-framerate", f"{fps:g}", "-use_wallclock_as_timestamps", "1", "-i", f"/dev/fd/{feed_r}"),
    )
    source = Source("bench", "pipe", "YUYV", width, height, fps)
    extra: dict = {}
    shader_dir = ""
    if "shaders" in Spec.__dataclass_fields__ and opts.sharpen == "gpu":
        shader_dir = tempfile.mkdtemp(prefix="bench-look-shaders-")
        extra["shaders"] = shader_dir
    elif opts.sharpen == "gpu":
        raise SystemExit("this engine has no GPU sharpen (it predates the shader): use --sharpen cpu")
    spec = Spec(source, "/dev/null", width, height, input_override=override, **extra)

    def popen(argv: list[str], **kwargs) -> subprocess.Popen:
        at = argv.index("v4l2")  # the program output: a pipe here, never a real device
        argv = [*argv[:at], "rawvideo", f"pipe:{out_w}", *argv[at + 2 :]]
        kwargs["pass_fds"] = (*kwargs["pass_fds"], out_w)
        return subprocess.Popen(argv, **kwargs)

    engine = VideoEngine(spec, look, popen=popen, pass_fds=(feed_r,))
    engine.start()
    base = photo_frame(width, height)
    sent: dict[int, float] = {}
    stop = threading.Event()

    def feeder() -> None:
        counter, nxt = 1, time.monotonic()
        head = STAMP_ROWS * width * 2
        while not stop.is_set():
            frame = stamp_frame(width, height, counter)[:head] + base[head:]
            sent[counter] = time.monotonic()
            try:
                view = memoryview(frame)
                while view:
                    view = view[os.write(feed_w, view) :]
            except OSError:
                return
            counter += 1
            nxt += 1.0 / fps
            time.sleep(max(0.0, nxt - time.monotonic()))

    received: list[tuple[float, int | None]] = []
    keep: list[bytes] = []
    size = width * height * 2

    def reader() -> None:
        with os.fdopen(out_r, "rb", buffering=0) as pipe:
            while True:
                chunks, left = [], size
                while left:
                    chunk = pipe.read(left)
                    if not chunk:
                        return
                    chunks.append(chunk)
                    left -= len(chunk)
                data = b"".join(chunks)
                received.append((time.monotonic(), read_stamp(data, width)))
                keep[:] = [data]

    result: dict = {
        "look": opts.look,
        "sharpen": opts.sharpen,
        "crop": bool(opts.crop),
        "size": f"{width}x{height}",
        "fps_in": fps,
    }
    try:
        threading.Thread(target=feeder, daemon=True).start()
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + 30
        while engine.state()["state"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        if engine.state()["state"] != "running":
            result["error"] = (
                engine.state()["error"] or " | ".join(list(engine._stderr)[-5:]) or engine.state()["state"]
            )
            return result
        if opts.crop:
            engine.set_window(window_rect(*CROP, width, height))
        time.sleep(opts.warmup)
        pid = engine.state()["pid"] or 0
        t0, ticks0, gpu0, n0 = time.monotonic(), cpu_ticks(pid), drm_ns(pid), len(received)
        time.sleep(opts.seconds)
        t1, ticks1, gpu1, n1 = time.monotonic(), cpu_ticks(pid), drm_ns(pid), len(received)
        hz = os.sysconf("SC_CLK_TCK")
        window = received[n0:n1]
        result["fps_out"] = round((n1 - n0) / (t1 - t0), 1)
        if ticks0 is not None and ticks1 is not None:
            result["engine_cpu_pct_of_one_core"] = round(100.0 * (ticks1 - ticks0) / hz / (t1 - t0), 1)
        if gpu0 is not None and gpu1 is not None:
            result["engine_gpu_ms_per_s"] = round((gpu1 - gpu0) / 1e6 / (t1 - t0), 1)
        lat = [(t - sent[c]) * 1000 for t, c in window if c in sent]
        result["latency_p50_ms"], result["latency_p95_ms"] = pct(lat, 0.5), pct(lat, 0.95)
        if lat:
            result["latency_mean_ms"] = round(statistics.fmean(lat), 1)
        result["dropped"] = engine.state()["dropped"]
        result["restarts"] = engine.state()["restarts"]
        if opts.png and keep:
            Path(opts.png).write_bytes(
                subprocess.run(
                    [
                        *("ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuyv422"),
                        *("-video_size", f"{width}x{height}", "-i", "-", "-frames:v", "1"),
                        *("-f", "image2", "-c:v", "png", "-"),
                    ],
                    input=keep[0],
                    check=True,
                    capture_output=True,
                ).stdout
            )
    finally:
        stop.set()
        engine.stop()
        for fd in (feed_w, feed_r, out_w):
            try:
                os.close(fd)
            except OSError:
                pass
        if shader_dir:
            for leftover in Path(shader_dir).glob("*"):
                leftover.unlink(missing_ok=True)
            Path(shader_dir).rmdir()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--look", choices=sorted(LOOKS), required=True)
    parser.add_argument("--sharpen", choices=("gpu", "cpu"), default="gpu")
    parser.add_argument("--crop", action="store_true", help="also zoom to a 1.25x window (the framing crop)")
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("--png", help="save a program frame here")
    opts = parser.parse_args()
    if int(opts.size.split("x")[0]) < BITS * BLOCK:
        raise SystemExit("the stamp needs a frame at least 768 pixels wide")
    print(json.dumps(run(opts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
