"""What this machine can run the engine on: the GPU's render node and whether the Vulkan graph works on it.

The Vulkan graph (VA-API decode, libplacebo colour, Quick Sync encode taps) is the engine. When the driver cannot
build it the engine falls back to the CPU graph (raw cameras only, eq for the grade, no crop) and says so.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from streamstack.hardware import roles
from streamstack.hardware.plan import Plan

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 20.0


def render_node(
    configured: str = "",
    sysfs: Path = Path("/sys/class/drm"),
    dev: Path = Path("/dev/dri"),
    plan: Plan | None = None,
) -> str:
    """The DRM render node for the engine's graph: the configured one, else the hardware Plan's grade role (found by
    PCI id), else the generic choice: a discrete Intel GPU, then the integrated one, then the first node."""
    return roles.choose("engine", plan, configured, sysfs=sysfs, dev=dev).path


Run = Callable[..., subprocess.CompletedProcess]


def probe_backend(node: str, ffmpeg: str = "ffmpeg", run: Run = subprocess.run) -> tuple[str, str]:
    """("vulkan", "") when libplacebo runs on `node`, else ("cpu", why)."""
    argv = [
        ffmpeg,
        *("-hide_banner", "-loglevel", "error", "-nostdin"),
        *("-init_hw_device", f"vaapi=va:{node}", "-init_hw_device", "vulkan=vk@va", "-filter_hw_device", "vk"),
        *("-f", "lavfi", "-i", "color=c=gray:s=640x360:r=5", "-frames:v", "2"),
        *("-vf", "format=nv12,hwupload,libplacebo=format=yuv444p,hwdownload,format=yuv444p", "-f", "null", "-"),
    ]
    try:
        result = run(argv, capture_output=True, timeout=PROBE_TIMEOUT, check=False)
    except FileNotFoundError:
        return "cpu", "ffmpeg is not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "cpu", f"the Vulkan probe did not finish ({type(exc).__name__})"
    if result.returncode == 0:
        return "vulkan", ""
    tail = result.stderr.decode(errors="replace").strip().splitlines()
    return "cpu", (tail[-1] if tail else f"ffmpeg exit {result.returncode}")[:200]
