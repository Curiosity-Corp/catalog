"""The VA-API to Vulkan frame mapping the MJPEG camera path depends on (decode on the Arc, grade in libplacebo).

These run only where ffmpeg has a VA-API device and a Vulkan device (CI has neither, so they skip there). The mapping
itself (`hwmap` of a decoded frame into Vulkan, read back with `hwdownload`) is checked against a software decode of
the same JPEG. The step after it, libplacebo importing the mapped frame, fails on the Arc stack this was measured on
(`VK_ERROR_OUT_OF_DEVICE_MEMORY` from `CreateImage`); `engine/probe.py` finds that out and the engine then decodes on
the GPU with a read-back (or, failing that, in software). The tests below run the engine's own argv on the copy and
software paths and on whichever path the probe selects.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from streamstack.engine import graph, probe
from streamstack.engine.graph import Source, Spec

RENDER_NODE = "/dev/dri/renderD128"
NEUTRAL = {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0, "warmth": 0.0}
WIDTH, HEIGHT = 320, 240


def ffmpeg(*args: str, capture: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["ffmpeg", "-v", "error", *args], capture_output=capture, timeout=60, check=False)


def va_and_vulkan() -> bool:
    if shutil.which("ffmpeg") is None or not Path(RENDER_NODE).exists():
        return False
    probe = ffmpeg(
        *("-init_hw_device", f"vaapi=va:{RENDER_NODE}", "-init_hw_device", "vulkan=vk@va"),
        *("-f", "lavfi", "-i", "color=c=gray:s=64x64:d=0.1", "-vf", "format=nv12,hwupload", "-f", "null", "-"),
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not va_and_vulkan(), reason="needs ffmpeg with a VA-API and a Vulkan device")


@pytest.fixture
def card(tmp_path: Path) -> Path:
    path = tmp_path / "card.jpg"
    made = ffmpeg(
        *("-f", "lavfi", "-i", f"testsrc2=s={WIDTH}x{HEIGHT}:d=1:r=1", "-frames:v", "1", "-q:v", "2", str(path))
    )
    assert made.returncode == 0, made.stderr.decode()
    return path


def software_decode(card: Path) -> bytes:
    done = ffmpeg(*("-i", str(card), "-pix_fmt", "yuv420p", "-frames:v", "1", "-f", "rawvideo", "-"))
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout


def mean_abs_difference(a: bytes, b: bytes) -> float:
    assert len(a) == len(b) and a
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def va_args(card: Path) -> list[str]:
    return [
        *("-init_hw_device", f"vaapi=va:{RENDER_NODE}", "-init_hw_device", "vulkan=vk@va", "-filter_hw_device", "vk"),
        *("-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi", "-i", str(card)),
    ]


def test_a_va_decoded_jpeg_maps_into_vulkan_and_reads_back_like_a_software_decode(card: Path) -> None:
    done = ffmpeg(
        *va_args(card),
        *("-vf", "hwmap=derive_device=vulkan,hwdownload,format=yuv420p", "-frames:v", "1", "-f", "rawvideo", "-"),
    )
    assert done.returncode == 0, done.stderr.decode()
    assert mean_abs_difference(done.stdout, software_decode(card)) < 3.0


def graded_through_the_engine_argv(card: Path, tmp_path: Path, decode: str) -> bytes:
    """The engine's own argv for a Cam Link style MJPEG source, output to a file instead of /dev/video43."""
    source = Source("UVC", "/dev/video2", "MJPG", WIDTH, HEIGHT, 30.0)
    spec = Spec(
        source,
        "/dev/null",
        WIDTH,
        HEIGHT,
        input_override=("-loop", "1", "-framerate", "30", "-t", "1", "-i", str(card)),
        mjpeg_decode=decode,
    )
    argv = graph.build_argv(spec, NEUTRAL)
    at = argv.index("v4l2")
    out = tmp_path / f"out-{decode}.yuv"
    argv = [*argv[: at - 1], "-frames:v", "1", "-f", "rawvideo", str(out), *argv[at + 2 :]]
    done = subprocess.run(argv, capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr.decode()[-400:]
    reference = ffmpeg(*("-i", str(card), "-pix_fmt", "yuyv422", "-frames:v", "1", "-f", "rawvideo", "-")).stdout
    data = out.read_bytes()[: len(reference)]
    assert mean_abs_difference(data, reference) < 6.0
    return data


def test_the_software_decode_graph_grades_an_mjpeg_frame(card: Path, tmp_path: Path) -> None:
    """The fallback path works on every stack: libavcodec decodes, hwupload, libplacebo grades."""
    graded_through_the_engine_argv(card, tmp_path, graph.MJPEG_SOFTWARE)


def test_the_vaapi_copy_graph_grades_an_mjpeg_frame(card: Path, tmp_path: Path) -> None:
    """VA-API decodes, the frame is read back in its real format and uploaded: no dma-buf import involved."""
    graded_through_the_engine_argv(card, tmp_path, graph.MJPEG_VAAPI_COPY)


def test_the_probe_picks_a_decode_path_that_works_here(card: Path, tmp_path: Path) -> None:
    """Whatever the probe says about this stack, the graph it selects runs. On the Arc stack that cannot import a
    VA-mapped frame (VK_ERROR_OUT_OF_DEVICE_MEMORY from libplacebo) that is the software path; on a stack that can,
    it is the zero-copy one."""
    decode, why = probe.probe_mjpeg(RENDER_NODE, cache=tmp_path / "probe.json")
    assert decode in graph.MJPEG_DECODES, why
    graded_through_the_engine_argv(card, tmp_path, decode)
