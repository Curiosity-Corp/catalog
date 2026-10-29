"""What this machine can run the engine on: the GPU's render node and whether the Vulkan graph works on it.

The Vulkan graph (VA-API decode, libplacebo colour, Quick Sync encode taps) is the engine. When the driver cannot
build it the engine falls back to the CPU graph (raw cameras only, eq for the grade, no crop) and says so.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from streamstack import config, store
from streamstack.engine.graph import MJPEG_DECODES, MJPEG_SOFTWARE, MJPEG_VAAPI, MJPEG_VAAPI_COPY
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


# ---------------------------------------------------------------------------
# Motion-JPEG decode: can libplacebo take the frames VA-API decodes?
# ---------------------------------------------------------------------------

MJPEG_PROBE_SIZE = "640x360"
MJPEG_SUBSAMPLINGS = ("yuvj420p", "yuvj422p")  # what cameras send: a JPEG is 4:2:0 or 4:2:2
STACK_PACKAGES = ("mesa-vulkan-drivers", "intel-media-va-driver", "libva2")
CACHE_VERSION = 1


def mjpeg_cache_file() -> Path:
    return config.user_env_file().parent / "engine-mjpeg-probe.json"


def stack_fingerprint(node: str, ffmpeg: str = "ffmpeg", run: Run = subprocess.run) -> str:
    """The graphics stack the answer depends on: the node, ffmpeg's version, and the Vulkan driver (mesa), VA driver
    and libva packages where dpkg knows them. A different fingerprint means the probe runs again."""
    parts = [f"v{CACHE_VERSION}", node]
    commands = [[ffmpeg, "-version"], ["dpkg-query", "-W", "-f=${Package} ${Version}\\n", *STACK_PACKAGES]]
    for argv in commands:
        try:
            result = run(argv, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            parts.append(f"{argv[0]}?")
            continue
        text = result.stdout.decode(errors="replace").strip().splitlines()
        parts.append(";".join(text[:1] if argv[0] == ffmpeg else sorted(text)))
    return "|".join(parts)


def _reason(result: subprocess.CompletedProcess) -> str:
    """The stderr line that says what failed: ffmpeg ends with a generic "nothing was written" line, so prefer the
    driver's own error (libplacebo: VK_ERROR_..., Failed ...) over the last line."""
    lines = [line.strip() for line in result.stderr.decode(errors="replace").splitlines() if line.strip()]
    for line in lines:
        if "VK_ERROR" in line or "Failed" in line or "Error" in line:
            return line[:200]
    return (lines[-1] if lines else f"ffmpeg exit {result.returncode}")[:200]


def _mjpeg_attempt(node: str, subsampling: str, directory: Path, mode: str, ffmpeg: str, run: Run) -> str:
    """Empty when a synthetic JPEG of `subsampling` decodes on VA-API and goes through libplacebo the way `mode`
    (zero-copy or vaapi-copy) does in the engine's graph, else why not."""
    jpeg = directory / f"probe-{subsampling}.jpg"
    quiet = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    make = [
        *quiet,
        *("-f", "lavfi", "-i", f"testsrc2=s={MJPEG_PROBE_SIZE}:d=1:r=1", "-frames:v", "1"),
        *("-pix_fmt", subsampling, "-q:v", "3", str(jpeg)),
    ]
    zero_copy = mode == MJPEG_VAAPI
    # the engine's own head: the decoded surface mapped into Vulkan, or read back and uploaded; libplacebo; read back
    head = "hwmap=derive_device=vulkan" if zero_copy else "format=yuv420p|yuv422p|nv12,hwupload"
    use = [
        *quiet,
        *("-init_hw_device", f"vaapi=va:{node}", "-init_hw_device", "vulkan=vk@va", "-filter_hw_device", "vk"),
        *("-hwaccel", "vaapi", "-hwaccel_device", "va", *(("-hwaccel_output_format", "vaapi") if zero_copy else ())),
        *("-i", str(jpeg)),
        *("-vf", f"{head},libplacebo=format=yuv422p,hwdownload,format=yuv422p"),
        *("-frames:v", "2", "-f", "null", "-"),
    ]
    for argv in (make, use):
        try:
            result = run(argv, capture_output=True, timeout=PROBE_TIMEOUT, check=False)
        except FileNotFoundError:
            return "ffmpeg is not installed"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"the MJPEG probe did not finish ({type(exc).__name__})"
        if result.returncode != 0:
            return _reason(result)
    return ""


def probe_mjpeg(
    node: str,
    ffmpeg: str = "ffmpeg",
    run: Run = subprocess.run,
    cache: Path | None = None,
) -> tuple[str, str]:
    """How to decode a Motion-JPEG camera on `node`, the first that works: "vaapi" (decode on the GPU, map into
    libplacebo, no copy), "vaapi-copy" (decode on the GPU, read back, upload), else "software". The second value is
    why zero-copy failed ("" when it works). Each tier decodes a synthetic JPEG (4:2:0 and 4:2:2) the way the engine
    does, once per device and graphics stack: the answer is kept in a file under the fingerprint of the stack and used
    until that changes.
    """
    path = cache or mjpeg_cache_file()
    key = stack_fingerprint(node, ffmpeg, run)
    saved = store.read_json(path, {}, validate=store.is_object)
    entry = saved.get(key)
    if isinstance(entry, dict) and entry.get("decode") in MJPEG_DECODES:
        return str(entry["decode"]), str(entry.get("why", ""))
    decode, why = MJPEG_SOFTWARE, ""
    with tempfile.TemporaryDirectory(prefix="streamstack-mjpeg-probe-") as scratch:
        for mode in (MJPEG_VAAPI, MJPEG_VAAPI_COPY):
            failed = ""
            for subsampling in MJPEG_SUBSAMPLINGS:
                failed = _mjpeg_attempt(node, subsampling, Path(scratch), mode, ffmpeg, run)
                if failed:
                    break
            if mode == MJPEG_VAAPI:
                why = failed  # what stops zero-copy, kept for the report
            if failed.startswith(("ffmpeg is not installed", "the MJPEG probe did not finish")):
                return MJPEG_SOFTWARE, failed  # not an answer about the stack: ask again next time
            if not failed:
                decode = mode
                break
    try:
        store.write_json(path, {key: {"decode": decode, "why": why}}, mode=0o600)  # one entry: older stacks drop out
    except OSError as exc:
        log.warning("could not keep the MJPEG probe result in %s: %s", path, exc)
    return decode, why
