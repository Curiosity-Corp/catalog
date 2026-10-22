"""ffmpeg for the Canon bridge: decode the live-view JPEGs (GPU first), size them, write the loopback device.

Frames reach ffmpeg as multipart JPEG with an explicit Content-Length. The raw `-f mjpeg` demuxer cannot tell a
frame has ended until the next one starts, which costs one whole frame of latency; with the length known, ffmpeg
has the frame the moment it is written. Verified: first output after ~0.14 s with the low-delay flags below.

Decode paths, each verified on synthetic Canon-style frames (4:2:2 and 4:2:0, 1056x704), ffmpeg 8.0.1, Intel Arc:
  qsv    hwaccel qsv + mjpeg_qsv, vpp_qsv scale/convert on the GPU, one download of NV12
  vaapi  hwaccel vaapi, scale_vaapi on the GPU, one download of NV12
  cpu    software mjpeg decode + swscale
  legacy the pre-0.3 command line (raw mjpeg demuxer, CPU) kept as the last resort
Luma levels match the CPU path on both GPU paths; do not add out_range (it shifts them).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import system
from streamstack.canon.jpeg import JpegInfo

DECODE_ORDER = ("qsv", "vaapi", "cpu")
LOW_DELAY_INPUT = [
    "-fflags",
    "+nobuffer+discardcorrupt",
    "-flags",
    "low_delay",
    "-probesize",
    "32",
    "-analyzeduration",
    "0",
    "-use_wallclock_as_timestamps",
    "1",
]
INTEL_VENDOR = "0x8086"


def even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def output_size(src: JpegInfo, mode: str, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int]:
    """(scaled width, scaled height, output width, output height).

    native  keep the camera's aspect ratio and never enlarge: shrink only when the frame is bigger than the canvas.
    fill    the 0.2 behaviour: cover the canvas and crop the overflow (3:2 live view into 16:9).
    """
    if mode == "fill":
        scale = max(canvas_w / src.width, canvas_h / src.height)
        return even(src.width * scale + 1), even(src.height * scale + 1), even(canvas_w), even(canvas_h)
    scale = min(1.0, canvas_w / src.width, canvas_h / src.height)
    w, h = even(src.width * scale), even(src.height * scale)
    return w, h, w, h


def render_nodes(sys_drm: Path = Path("/sys/class/drm"), dev_dri: Path = Path("/dev/dri")) -> list[tuple[str, str]]:
    """[(device node, PCI vendor id)] for the render nodes, Intel first (the Arc), then the rest by name."""
    found: list[tuple[str, str]] = []
    try:
        entries = sorted(sys_drm.glob("renderD*"))
    except OSError:
        entries = []
    for entry in entries:
        try:
            vendor = (entry / "device" / "vendor").read_text().strip().lower()
        except OSError:
            vendor = ""
        found.append((str(dev_dri / entry.name), vendor))
    found.sort(key=lambda item: (item[1] != INTEL_VENDOR, item[0]))
    return found


@dataclass
class Plan:
    name: str
    prefix: list[str]  # everything up to the output
    sink: list[str]  # the output: the loopback device
    env: dict[str, str] = field(default_factory=dict)
    out_w: int = 0
    out_h: int = 0
    framing: str = "mpjpeg"  # mpjpeg | raw

    @property
    def argv(self) -> list[str]:
        return [*self.prefix, *self.sink]

    def probe_argv(self) -> list[str]:
        """The same pipeline with a null sink: proves the decode path works without touching the loopback device."""
        return [*self.prefix, "-frames:v", "1", "-f", "null", "-"]


def ffmpeg_version(binary: str = "ffmpeg") -> tuple[int, int]:
    result = system.run([binary, "-version"], timeout=5)
    m = re.search(r"version\D*(\d+)\.(\d+)", result.stdout if result else "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def passthrough_args(version: tuple[int, int]) -> list[str]:
    """Do not let ffmpeg duplicate or drop frames to a guessed rate (`-fps_mode` arrived in 5.1)."""
    return ["-fps_mode", "passthrough"] if version >= (5, 1) else ["-vsync", "passthrough"]


def build_plan(
    name: str,
    src: JpegInfo,
    device: str,
    mode: str,
    canvas: tuple[int, int],
    render_node: str | None = None,
    vendor: str = "",
    version: tuple[int, int] = (8, 0),
    binary: str = "ffmpeg",
) -> Plan:
    sw, sh, ow, oh = output_size(src, mode, *canvas)
    crop = f",crop={ow}:{oh}" if (sw, sh) != (ow, oh) else ""
    sink = ["-f", "v4l2", device]
    pt = passthrough_args(version)
    head = [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1", "-stats_period", "0.5"]
    env: dict[str, str] = {}
    if name == "legacy":
        legacy_vf = f"scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},format=yuyv422"
        return Plan(
            "legacy", [*head, "-f", "mjpeg", "-i", "pipe:0", "-vf", legacy_vf], sink, env, ow, oh, framing="raw"
        )
    resize = f"w={sw}:h={sh}:" if (sw, sh) != (src.width, src.height) else ""
    if name == "cpu":
        chain = ([f"scale={sw}:{sh}"] if resize else []) + ([f"crop={ow}:{oh}"] if crop else []) + ["format=yuyv422"]
        prefix = [*head, *LOW_DELAY_INPUT, "-f", "mpjpeg", "-i", "pipe:0", "-vf", ",".join(chain), *pt]
        return Plan("cpu", prefix, sink, env, ow, oh)
    if not render_node:
        raise ValueError(f"{name} needs a render node")
    if vendor == INTEL_VENDOR and "LIBVA_DRIVER_NAME" not in os.environ:
        env["LIBVA_DRIVER_NAME"] = "iHD"
    if name == "vaapi":
        pre = [
            "-init_hw_device",
            f"vaapi=va:{render_node}",
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            "va",
            "-hwaccel_output_format",
            "vaapi",
        ]
        vf = f"scale_vaapi={resize}format=nv12,hwdownload,format=nv12{crop},format=yuyv422"
    elif name == "qsv":
        pre = [
            "-init_hw_device",
            f"vaapi=va:{render_node}",
            "-init_hw_device",
            "qsv=qs@va",
            "-hwaccel",
            "qsv",
            "-hwaccel_device",
            "qs",
            "-hwaccel_output_format",
            "qsv",
            "-c:v",
            "mjpeg_qsv",
            "-async_depth",
            "1",
        ]
        vf = f"vpp_qsv={resize}format=nv12,hwdownload,format=nv12{crop},format=yuyv422"
    else:
        raise ValueError(f"unknown decode path {name!r}")
    prefix = [*head, *LOW_DELAY_INPUT, *pre, "-f", "mpjpeg", "-i", "pipe:0", "-vf", vf, *pt]
    return Plan(name, prefix, sink, env, ow, oh)


def mpjpeg(frame: bytes) -> bytes:
    return b"--ffmpeg\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame) + frame + b"\r\n"


def probe(plan: Plan, frame: bytes, timeout: float = 8.0, binary_env: dict[str, str] | None = None) -> str | None:
    """Run the plan once on a real frame into a null sink. None when it works, otherwise why it does not."""
    if plan.name == "legacy":
        return None
    env = dict(os.environ, **plan.env, **(binary_env or {}))
    data = mpjpeg(frame) * 3
    try:
        proc = subprocess.Popen(
            plan.probe_argv(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env
        )
    except OSError as exc:
        return f"cannot start ffmpeg: {exc}"
    try:
        _, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        system.reap(proc)
        return f"timed out after {timeout:.0f}s"
    except OSError as exc:
        system.reap(proc)
        return f"ffmpeg pipe failed: {exc}"
    if proc.returncode != 0:
        lines = [x for x in err.decode("utf-8", "replace").splitlines() if x.strip()]
        return (lines[-1] if lines else f"exit {proc.returncode}")[:240]
    return None


class Encoder:
    """A running ffmpeg writing the loopback device. Thread-safe feed; progress and stderr are collected."""

    def __init__(self, plan: Plan, popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.plan = plan
        self.popen = popen
        self.proc: subprocess.Popen | None = None
        self.out_frames = 0
        self.last_out = 0.0
        self.err: deque[str] = deque(maxlen=12)
        self.started = 0.0
        self.fed = 0

    def start(self) -> None:
        env = dict(os.environ, **self.plan.env)
        self.proc = self.popen(
            self.plan.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.started = time.monotonic()
        self.last_out = self.started
        self.out_frames = 0
        self.fed = 0
        for target in (self._progress, self._stderr):
            threading.Thread(target=target, args=(self.proc,), daemon=True, name=f"canon-enc-{target.__name__}").start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def feed(self, frame: bytes) -> float:
        """Write one frame; returns the milliseconds the write took (a backed-up ffmpeg shows here)."""
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise BrokenPipeError("encoder is not running")
        t0 = time.monotonic()
        proc.stdin.write(mpjpeg(frame) if self.plan.framing == "mpjpeg" else frame)
        proc.stdin.flush()
        self.fed += 1
        return (time.monotonic() - t0) * 1000.0

    def _progress(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
            if line.startswith("frame="):
                try:
                    n = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if n != self.out_frames:
                    self.out_frames = n
                    self.last_out = time.monotonic()

    def _stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
            if text:
                self.err.append(text)

    def stderr_tail(self) -> str:
        return " | ".join(self.err)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass  # the pipe is already broken; reaping below ends the process
        system.reap(proc)
