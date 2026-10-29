"""The GPU video denoiser (`nlmeans_vulkan`, issue #92) through the engine's own argv.

Runs only where ffmpeg has a Vulkan device (CI has none, so it skips there). A smooth picture gets sensor-like noise;
the engine's graph with `denoise="nlmeans"` must bring it measurably closer to the clean picture, and the filter's
garbage outermost row and column (ffmpeg 8.0.1, Mesa 26.0) must not reach the output.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from streamstack.engine import graph
from streamstack.engine.graph import Source, Spec

W, H = 320, 240
RENDER_NODE = Spec.__dataclass_fields__["render_node"].default
NEUTRAL = {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0, "warmth": 0.0}


def ffmpeg(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["ffmpeg", "-v", "error", *args], input=stdin, capture_output=True, timeout=90, check=False)


def vulkan_available() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    done = ffmpeg(
        *("-init_hw_device", f"vaapi=va:{RENDER_NODE}", "-init_hw_device", "vulkan=vk@va"),
        *("-f", "lavfi", "-i", "color=c=gray:s=64x64:d=0.1"),
        *("-vf", "hwupload", "-f", "null", "-"),
    )
    return done.returncode == 0


pytestmark = pytest.mark.skipif(not vulkan_available(), reason="needs ffmpeg with a Vulkan device")


def mean_abs(a: bytes, b: bytes) -> float:
    assert len(a) == len(b) and a
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def through_the_engine(raw: bytes, denoise: str) -> bytes:
    src = Source("noisy", "pipe", "YUYV", W, H, 30.0)
    override = ("-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", f"{W}x{H}", "-framerate", "30", "-t", "0.2")
    spec = Spec(src, "/dev/null", W, H, input_override=(*override, "-i", "pipe:0"), denoise=denoise)
    chain = graph.build_graph(spec, NEUTRAL)
    argv = [
        *("-init_hw_device", f"vaapi=va:{spec.render_node}", "-init_hw_device", "vulkan=vk@va"),
        *("-filter_hw_device", "vk", *spec.input_override),
    ]
    done = ffmpeg(
        *argv, *("-filter_complex", chain, "-map", "[prog]", "-frames:v", "1", "-pix_fmt", "yuyv422"),
        *("-f", "rawvideo", "-"), stdin=raw * 6,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout


def test_the_engines_denoise_graph_cleans_a_noisy_frame_without_edge_garbage(tmp_path: Path) -> None:
    clean = ffmpeg(
        *("-f", "lavfi", "-i", f"gradients=s={W}x{H}:d=1:r=1:seed=3", "-frames:v", "1", "-pix_fmt", "yuyv422"),
        *("-f", "rawvideo", "-"),
    ).stdout
    noisy = ffmpeg(
        *("-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", f"{W}x{H}", "-i", "-"),
        *("-vf", "noise=alls=20:allf=u:all_seed=5", "-frames:v", "1", "-pix_fmt", "yuyv422", "-f", "rawvideo", "-"),
        stdin=clean,
    ).stdout
    assert len(clean) == len(noisy) == W * H * 2
    plain, cleaned = through_the_engine(noisy, ""), through_the_engine(noisy, "nlmeans")
    assert len(plain) == len(cleaned) == W * H * 2
    assert mean_abs(cleaned, clean) < mean_abs(plain, clean) * 0.75
    # the first and last rows of luma: no garbage (the unfiltered picture differs from the clean one by the noise only)
    row = W * 2
    assert mean_abs(cleaned[:row], clean[:row]) < mean_abs(plain[:row], clean[:row]) * 1.5
    assert mean_abs(cleaned[-row:], clean[-row:]) < mean_abs(plain[-row:], clean[-row:]) * 1.5
