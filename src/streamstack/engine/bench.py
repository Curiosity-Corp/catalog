"""Measure the engine: feed-to-output latency, slider-to-picture latency and CPU, with no camera involved.

A synthetic YUYV feed (a counter stamped in the top row of every frame) goes into the engine through a pipe; a
second reader (`v4l2-ctl --stream-mmap`) takes the frames back from /dev/video43 and reads the counter. The time
between writing frame N and receiving frame N is everything StreamStack adds: upload, GPU work, download, the
v4l2loopback copy and the reader's own pipe. The camera's sensor and USB time is the same on every path and is not
part of it. `streamstack engine bench` runs it; the numbers belong in docs/native-video-engine.md.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field

from streamstack.engine import graph
from streamstack.engine.graph import Source, Spec, Tap
from streamstack.engine.process import VideoEngine

BITS = 24
BLOCK = 32  # pixels per stamp bit
STAMP_ROWS = 16
BODY = 100  # luma of the flat body, used to see a look change arrive
HIGH, LOW = 235, 16


@dataclass
class Result:
    label: str
    size: str
    fps_in: float
    fps_out: float
    latency_ms: list[float] = field(default_factory=list)
    effect_ms: list[float] = field(default_factory=list)
    cpu_percent: float = 0.0
    dropped: int = 0
    notes: str = ""

    def percentile(self, values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]

    def summary(self) -> dict:
        return {
            "label": self.label,
            "size": self.size,
            "fps_out": round(self.fps_out, 1),
            "latency_p50_ms": _r(self.percentile(self.latency_ms, 0.5)),
            "latency_p95_ms": _r(self.percentile(self.latency_ms, 0.95)),
            "latency_max_ms": _r(max(self.latency_ms) if self.latency_ms else None),
            "look_to_picture_ms": _r(statistics.median(self.effect_ms) if self.effect_ms else None),
            "engine_cpu_percent_of_one_core": round(self.cpu_percent, 1),
            "dropped": self.dropped,
            "notes": self.notes,
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def stamp_frame(width: int, height: int, counter: int, body: int = BODY) -> bytearray:
    """A flat YUYV frame whose first STAMP_ROWS rows carry `counter` as BITS black/white blocks."""
    frame = bytearray([body, 128] * (width * height // 1))[: width * height * 2]
    row = bytearray([body, 128] * width)
    for bit in range(BITS):
        value = HIGH if (counter >> (BITS - 1 - bit)) & 1 else LOW
        for x in range(bit * BLOCK, (bit + 1) * BLOCK):
            row[x * 2] = value
    for y in range(STAMP_ROWS):
        frame[y * width * 2 : (y + 1) * width * 2] = row
    return frame


def read_stamp(frame: bytes, width: int) -> int | None:
    """The counter in a received frame, or None when the stamp is unreadable."""
    value = 0
    for bit in range(BITS):
        centre = STAMP_ROWS // 2 * width * 2 + (bit * BLOCK + BLOCK // 2) * 2
        luma = frame[centre]
        if 60 < luma < 190:
            return None
        value = (value << 1) | (1 if luma >= 190 else 0)
    return value


def body_luma(frame: bytes, width: int, height: int) -> int:
    return frame[(height // 2) * width * 2 + width]


def cpu_ticks(pid: int) -> int | None:
    try:
        fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()  # noqa: SIM115
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return None


def run(
    width: int,
    height: int,
    fps: float,
    seconds: float,
    *,
    label: str,
    look: dict | None = None,
    window: tuple[float, float, float] | None = None,
    tap: Tap | None = None,
    out_device: str = "/dev/video43",
    effect: bool = True,
    render_node: str = "",
) -> Result:
    """One measurement at one size, look and sink set."""
    feed_r, feed_w = os.pipe()
    override = (
        *("-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", f"{width}x{height}"),
        *(
            "-framerate",
            f"{fps:g}",
            "-use_wallclock_as_timestamps",
            "1",
            "-i",
            f"/dev/fd/{feed_r}",
        ),  # not pipe:N, which makes ffmpeg ignore stdin
    )
    source = Source("bench", "pipe", "YUYV", width, height, fps)
    nodes = {"render_node": render_node} if render_node else {}
    spec = Spec(source, out_device, width, height, input_override=override, taps=(tap,) if tap else (), **nodes)
    look = look or {}
    engine = VideoEngine(spec, look, pass_fds=(feed_r,))
    engine.start()
    sent: dict[int, float] = {}
    stop = threading.Event()
    done = threading.Event()
    body = [BODY]

    def feeder() -> None:
        counter, period, next_at = 1, 1.0 / fps, time.monotonic()
        while not stop.is_set():
            frame = stamp_frame(width, height, counter, body[0])
            sent[counter] = time.monotonic()
            try:
                _write_all(feed_w, frame)
            except OSError:
                return
            counter += 1
            next_at += period
            time.sleep(max(0.0, next_at - time.monotonic()))

    result = Result(label, f"{width}x{height}@{fps:g}", fps, 0.0)
    threading.Thread(target=feeder, daemon=True, name="bench-feed").start()
    try:
        if not _wait(lambda: engine.state()["state"] == "running", 20):
            result.notes = f"engine did not start: {engine.state()['error']}"
            return result
        if window:
            engine.set_window(graph.window_rect(*window, width, height))
        reader = subprocess.Popen(
            ["v4l2-ctl", "-d", out_device, "--stream-mmap=4", "--stream-to=-", "--stream-poll"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        size = width * height * 2
        received: list[tuple[float, int | None, int]] = []  # (arrival time, stamp counter, body luma)
        pipe = reader.stdout
        if pipe is None:
            raise RuntimeError("v4l2-ctl gave no stdout pipe")

        def read_loop() -> None:
            while not done.is_set():
                data = pipe.read(size)
                if len(data) < size:
                    return
                received.append((time.monotonic(), read_stamp(data, width), body_luma(data, width, height)))

        threading.Thread(target=read_loop, daemon=True, name="bench-read").start()
        time.sleep(2.0)  # warm-up
        pid = engine.state()["pid"]
        ticks0, t0, base = cpu_ticks(pid or 0), time.monotonic(), len(received)
        time.sleep(seconds)
        ticks1, t1, count = cpu_ticks(pid or 0), time.monotonic(), len(received)
        if ticks0 is not None and ticks1 is not None:
            result.cpu_percent = 100.0 * (ticks1 - ticks0) / os.sysconf("SC_CLK_TCK") / (t1 - t0)
        result.fps_out = (count - base) / (t1 - t0)
        result.dropped = engine.state()["dropped"]
        for stamp_time, counter, _ in received[base:count]:
            if counter in sent:
                result.latency_ms.append((stamp_time - sent[counter]) * 1000)
        if effect:
            result.effect_ms = _effect_latency(engine, received, look)
    finally:
        stop.set()
        done.set()
        engine.stop()
        os.close(feed_w)
        os.close(feed_r)
        try:
            reader.kill()  # our own child
            reader.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired, UnboundLocalError):
            pass
    return result


def _write_all(fd: int, data: bytes | bytearray) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _wait(check, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def _effect_latency(engine, received, look, rounds: int = 8) -> list[float]:
    """Time from `apply_look` returning to the first received frame whose body luma moved: slider to picture.

    The feed is flat, so the grade shows as a change of the body's luma. Alternates +0.25 / 0 brightness.
    """
    values: list[float] = []
    for i in range(rounds):
        target = 0.25 if i % 2 == 0 else 0.0
        before = received[-1][2]
        index = len(received)
        began = time.monotonic()
        engine.apply_look({**look, "brightness": target})
        end = began + 2.0
        while time.monotonic() < end:
            if len(received) > index:
                arrived, _, luma = received[index]
                if abs(luma - before) >= 8:
                    values.append((arrived - began) * 1000)
                    break
                index += 1
            else:
                time.sleep(0.001)
        time.sleep(0.6)
    return values
