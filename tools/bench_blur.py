#!/usr/bin/env python3
"""Measure background blur in the native engine: latency, mask lag, CPU and (when permitted) GPU time.

A stamped YUYV feed goes into a real engine child through a pipe, like `streamstack engine bench`, but the program
picture is read back from a pipe instead of /dev/video43, so this never touches the program camera and is safe to
run while a call is in progress. One mode per run:

    off     no blur branch in the graph (the engine as it was)
    armed   the blur branch is in the graph, blur is switched off
    fake    blur on, a stand-in segmenter that decodes the frame stamp and draws a moving box (no GPU, no model):
            this isolates the graph and the pipes, and measures how many frames the mask trails the picture
    arc     blur on, the real model on the OpenVINO GPU (needs `pip install 'streamstack[blur]'` and the model)

Needs numpy for every mode (`pip install 'streamstack[blur]'`). `--image` feeds a still picture instead of the test
pattern and `--png` saves one program frame, to look at the blur. Latency is feed-write to program-read, the same
instrument as docs/native-video-engine.md; the camera's own sensor and USB time is not part of it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from streamstack.engine import segment
from streamstack.engine.bench import BITS, BLOCK, STAMP_ROWS, cpu_ticks
from streamstack.engine.blurfeed import BlurFeed
from streamstack.engine.graph import BlurGraph, Source, Spec
from streamstack.engine.process import VideoEngine

PERIOD = 90  # frames per sweep of the fake mask


def stripes(width: int, height: int) -> np.ndarray:
    """Luma plane of vertical 4-pixel stripes: sharp where the picture is untouched, flat grey once blurred."""
    column = np.where((np.arange(width) // 2) % 2 == 0, 60, 200).astype(np.uint8)
    return np.tile(column, (height, 1))


def to_yuyv(luma: np.ndarray, chroma: np.ndarray | None = None) -> np.ndarray:
    height, width = luma.shape
    out = np.empty((height, width, 2), dtype=np.uint8)
    out[..., 0] = luma
    out[..., 1] = 128 if chroma is None else chroma
    return out


def image_frame(path: str, width: int, height: int) -> np.ndarray:
    """A still picture as a YUYV frame, converted by ffmpeg (so the colours are the ones the engine would see)."""
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"scale={width}:{height}",
            "-pix_fmt",
            "yuyv422",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 2).copy()


def stamp(frame: np.ndarray, counter: int) -> None:
    for bit in range(BITS):
        value = 235 if (counter >> (BITS - 1 - bit)) & 1 else 16
        frame[:STAMP_ROWS, bit * BLOCK : (bit + 1) * BLOCK, 0] = value


def read_stamp(luma: np.ndarray) -> int | None:
    value = 0
    for bit in range(BITS):
        sample = int(luma[STAMP_ROWS // 2, bit * BLOCK + BLOCK // 2])
        if 60 < sample < 190:
            return None
        value = (value << 1) | (1 if sample >= 190 else 0)
    return value


def box_centre(counter: int) -> float:
    """Where the fake mask's box is centred (fraction of the frame width) for the frame with this counter."""
    return 0.5 + 0.3 * math.sin(2 * math.pi * counter / PERIOD)


class FakeSegmenter:
    """Reads the stamp out of the squeezed frame and answers with a box at that frame's position."""

    label = "stand-in (CPU)"

    def infer(self, rgb: bytes) -> np.ndarray:
        side = segment.SEG_SIZE
        frame = np.frombuffer(rgb, dtype=np.uint8).reshape(side, side, 3)
        value = 0
        for bit in range(BITS):
            x = int((bit * BLOCK + BLOCK // 2) / 1280 * side)
            value = (value << 1) | (1 if frame[2, x, 1] > 128 else 0)
        mask = np.zeros((side, side), dtype=np.float32)
        centre = box_centre(value) * side
        lo, hi = max(0, int(centre - 0.15 * side)), min(side, int(centre + 0.15 * side))
        mask[:, lo:hi] = 1.0
        return mask

    def close(self) -> None:
        return None


def sharp_centre(luma: np.ndarray) -> float | None:
    """Centre (fraction of the width) of the columns that still carry the stripes, in a band across the middle."""
    band = luma[luma.shape[0] // 2 - 60 : luma.shape[0] // 2 + 60].astype(np.int16)
    energy = np.abs(np.diff(band, axis=1)).mean(axis=0)
    columns = np.nonzero(energy > 40)[0]
    if len(columns) < 20:
        return None
    return float((columns.min() + columns.max()) / 2 / luma.shape[1])


def drm_ns(pid: int) -> int | None:
    """GPU engine time this process has used, from the kernel's per-client fdinfo (needs no privilege)."""
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
    feed_r, feed_w = os.pipe()
    out_r, out_w = os.pipe()
    override = (
        *("-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", f"{width}x{height}"),
        *("-framerate", f"{fps:g}", "-use_wallclock_as_timestamps", "1", "-i", f"/dev/fd/{feed_r}"),
    )
    source = Source("bench", "pipe", "YUYV", width, height, fps)
    armed = opts.mode != "off"
    blur_graph = BlurGraph(mask_width=320, mask_height=max(2, round(320 * height / width / 2) * 2)) if armed else None
    spec = Spec(source, "/dev/null", width, height, input_override=override, blur=blur_graph)

    def popen(argv: list[str], **kwargs) -> subprocess.Popen:
        at = argv.index("v4l2")  # the program output: a pipe here, never a real device
        argv = [*argv[:at], "rawvideo", f"pipe:{out_w}", *argv[at + 2 :]]
        kwargs["pass_fds"] = (*kwargs["pass_fds"], out_w)
        return subprocess.Popen(argv, **kwargs)

    engine = VideoEngine(spec, {}, popen=popen, pass_fds=(feed_r,))
    feed: BlurFeed | None = None
    if armed:
        if opts.mode == "arc":
            arc = segment.probe()
            if not arc.ok:
                raise SystemExit(f"unavailable: {arc.reason}")
            model = segment.ensure_model()

            def open_segmenter() -> segment.Segmenter:
                return segment.ArcSegmenter(model, arc, cache=Path(opts.cache))
        else:
            open_segmenter = FakeSegmenter
        assert blur_graph is not None  # noqa: S101
        feed = BlurFeed(blur_graph, open_segmenter)
        engine.attach_blur(feed)
        engine.set_blur(False, opts.strength, opts.rate)
    engine.start()

    base = to_yuyv(stripes(width, height)) if not opts.image else image_frame(opts.image, width, height)
    sent: dict[int, float] = {}
    stop = threading.Event()

    def feeder() -> None:
        counter, nxt = 1, time.monotonic()
        while not stop.is_set():
            frame = base.copy()
            stamp(frame, counter)
            sent[counter] = time.monotonic()
            try:
                view = memoryview(frame.tobytes())
                while view:
                    view = view[os.write(feed_w, view) :]
            except OSError:
                return
            counter += 1
            nxt += 1.0 / fps
            time.sleep(max(0.0, nxt - time.monotonic()))

    received: list[tuple[float, int | None, float | None]] = []
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
                luma = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 2)[..., 0]
                received.append((time.monotonic(), read_stamp(luma), sharp_centre(luma)))
                if len(keep) < 1 or (opts.png and len(received) % 15 == 0):
                    keep[:] = [data]

    result: dict = {
        "mode": opts.mode,
        "size": f"{width}x{height}",
        "fps_in": fps,
        "strength": opts.strength,
        "rate": opts.rate,
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
            if feed is not None:
                result["blur"] = feed.stats()
            return result
        if opts.mode in ("fake", "arc"):
            engine.set_blur(True, opts.strength, opts.rate)
        time.sleep(opts.warmup)
        pid = engine.state()["pid"] or 0
        t0, ticks0, me0, gpu0, n0 = (
            time.monotonic(),
            cpu_ticks(pid),
            cpu_ticks(os.getpid()),
            drm_ns(os.getpid()),
            len(received),
        )
        time.sleep(opts.seconds)
        t1, ticks1, me1, gpu1, n1 = (
            time.monotonic(),
            cpu_ticks(pid),
            cpu_ticks(os.getpid()),
            drm_ns(os.getpid()),
            len(received),
        )
        hz = os.sysconf("SC_CLK_TCK")
        window = received[n0:n1]
        result["fps_out"] = round((n1 - n0) / (t1 - t0), 1)
        if ticks0 is not None and ticks1 is not None:
            result["ffmpeg_cpu_pct_of_one_core"] = round(100.0 * (ticks1 - ticks0) / hz / (t1 - t0), 1)
        if me0 is not None and me1 is not None:
            result["python_cpu_pct_of_one_core"] = round(100.0 * (me1 - me0) / hz / (t1 - t0), 1)
        if gpu0 is not None and gpu1 is not None:
            result["gpu_engine_ms_per_s_python"] = round((gpu1 - gpu0) / 1e6 / (t1 - t0), 1)
        lat = [(t - sent[c]) * 1000 for t, c, _ in window if c in sent]
        result["latency_p50_ms"], result["latency_p95_ms"] = pct(lat, 0.5), pct(lat, 0.95)
        if opts.series:
            Path(opts.series).write_text(
                "\n".join(f"{c} {(t - sent[c]) * 1000:.1f}" for t, c, _ in window if c in sent)
            )
        result["dropped"] = engine.state()["dropped"]
        if opts.mode == "fake":
            lags = []
            for shift in range(-3, 8):
                errors = [
                    abs(centre - box_centre(c - shift))
                    for _, c, centre in window
                    if c is not None and centre is not None
                ]
                lags.append((statistics.fmean(errors) if errors else 9.9, shift))
            best = min(lags)
            result["mask_lag_frames"] = best[1]
            result["mask_position_error_pct_of_width"] = round(best[0] * 100, 2)
        if feed is not None:
            result["blur"] = feed.stats()
        if opts.png and keep:
            Path(opts.png).write_bytes(
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "yuyv422",
                        "-video_size",
                        f"{width}x{height}",
                        "-i",
                        "-",
                        "-frames:v",
                        "1",
                        "-f",
                        "image2",
                        "-c:v",
                        "png",
                        "-",
                    ],
                    input=keep[0],
                    check=True,
                    capture_output=True,
                ).stdout
            )
    finally:
        stop.set()
        engine.stop()
        if feed is not None:
            feed.stop()
        for fd in (feed_w, feed_r, out_w):
            try:
                os.close(fd)
            except OSError:
                pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("off", "armed", "fake", "arc"), required=True)
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("--strength", type=int, default=8)
    parser.add_argument("--rate", type=int, default=1, help="mask every N-th frame")
    parser.add_argument("--image", help="feed this still picture instead of the stripe pattern")
    parser.add_argument("--series", help="write per-frame latency (counter, ms) here")
    parser.add_argument("--png", help="save a program frame here")
    parser.add_argument("--cache", default=str(Path.home() / ".cache/streamstack/openvino"))
    opts = parser.parse_args()
    print(json.dumps(run(opts), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
