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
# libplacebo does the dma-buf import that fails (#138), so its version is part of the answer
STACK_PACKAGES = ("mesa-vulkan-drivers", "intel-media-va-driver", "libva2", "libplacebo[0-9]*")
CACHE_VERSION = 2  # 2: a tier must also produce the right picture


def mjpeg_cache_file() -> Path:
    return config.user_env_file().parent / "engine-mjpeg-probe.json"


def stack_fingerprint(node: str, ffmpeg: str = "ffmpeg", run: Run = subprocess.run) -> str:
    """The graphics stack the answer depends on: the node, ffmpeg's version, and the Vulkan driver (mesa), VA driver,
    libva and libplacebo packages where dpkg knows them. A different fingerprint means the probe runs again."""
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


MAX_PICTURE_ERROR = 4.0  # mean absolute difference (of 255) between a tier's picture and the software reference
PROBE_FRAME_BYTES = 640 * 360 * 2  # one yuv422p frame at MJPEG_PROBE_SIZE
IMPORT_REJECTED = (
    "libplacebo's dma-buf import of the mapped VA-API frame is rejected by the Vulkan driver "
    "(VK_ERROR_OUT_OF_DEVICE_MEMORY from CreateImage, which is not a memory shortage: the VA to Vulkan mapping works)"
)


def _mean_difference(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _picture_problem(got: bytes, reference: bytes) -> str:
    """Empty when `got` is the same picture as `reference`, else what is wrong. A tier that exits 0 can still hand
    libplacebo an unsynchronised or empty image (an all-green frame), so a pass needs the right pixels."""
    if len(got) != PROBE_FRAME_BYTES:
        return f"the tier wrote {len(got)} bytes, not one {MJPEG_PROBE_SIZE} frame"
    if len(reference) != len(got):
        return "the reference picture has the wrong size"
    error = _mean_difference(got, reference)
    if error > MAX_PICTURE_ERROR:
        return f"the tier ran but its picture differs from a software decode (mean error {error:.0f} of 255)"
    return ""


def _run(argv: list[str], run: Run) -> subprocess.CompletedProcess | str:
    """The finished command, or the reason it could not be run (no ffmpeg, did not finish)."""
    try:
        return run(argv, capture_output=True, timeout=PROBE_TIMEOUT, check=False)
    except FileNotFoundError:
        return "ffmpeg is not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the MJPEG probe did not finish ({type(exc).__name__})"


def _mjpeg_attempt(node: str, subsampling: str, directory: Path, mode: str, ffmpeg: str, run: Run) -> str:
    """Empty when a synthetic JPEG of `subsampling` decodes the way `mode` does in the engine's graph, goes through
    libplacebo and comes out as the same picture a software decode gives; else why not."""
    jpeg = directory / f"probe-{subsampling}.jpg"
    quiet = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    make = [
        *quiet,
        *("-f", "lavfi", "-i", f"testsrc2=s={MJPEG_PROBE_SIZE}:d=1:r=1", "-frames:v", "1"),
        *("-pix_fmt", subsampling, "-q:v", "3", str(jpeg)),
    ]
    devices = ["-init_hw_device", f"vaapi=va:{node}", "-init_hw_device", "vulkan=vk@va", "-filter_hw_device", "vk"]

    def through_libplacebo(head: str, decode: list[str]) -> list[str]:
        return [
            *quiet,
            *devices,
            *decode,
            *("-i", str(jpeg)),
            *("-vf", f"{head},libplacebo=format=yuv422p,hwdownload,format=yuv422p"),
            *("-frames:v", "1", "-f", "rawvideo", "-"),
        ]

    zero_copy = mode == MJPEG_VAAPI
    upload = "format=yuv420p|yuv422p|nv12,hwupload"
    # the engine's own head: the decoded surface mapped into Vulkan, or read back and uploaded; libplacebo; read back
    head = "hwmap=derive_device=vulkan" if zero_copy else upload
    hw = ["-hwaccel", "vaapi", "-hwaccel_device", "va", *(["-hwaccel_output_format", "vaapi"] if zero_copy else [])]
    # the picture every tier must reproduce: libavcodec decodes, then the same upload and libplacebo
    reference = through_libplacebo(upload, [])
    made = _run(make, run)
    if isinstance(made, str):
        return made
    if made.returncode != 0:
        return _reason(made)
    done = _run(through_libplacebo(head, hw), run)
    if isinstance(done, str):
        return done
    if done.returncode != 0:
        return _explain_import(done, jpeg, subsampling, quiet, devices, run) if zero_copy else _reason(done)
    expected = _run(reference, run)
    if isinstance(expected, str):
        return expected
    if expected.returncode != 0:
        return _reason(expected)
    return _picture_problem(done.stdout, expected.stdout)


def _explain_import(
    failed: subprocess.CompletedProcess, jpeg: Path, subsampling: str, quiet: list[str], devices: list[str], run: Run
) -> str:
    """Why zero-copy failed. ffmpeg reports a libplacebo `CreateImage` failure as VK_ERROR_OUT_OF_DEVICE_MEMORY, which
    reads like a full GPU. It is not: on the Arc stack the VA surface maps into Vulkan and reads back correctly, and
    libplacebo then re-imports it as a dma-buf and the driver rejects that import (#138). Mapping alone is run here
    to tell the two apart; the answer names the step that really failed."""
    reason = _reason(failed)
    if "VK_ERROR_OUT_OF_DEVICE_MEMORY" not in reason:
        return reason
    pix_fmt = "yuv420p" if subsampling == "yuvj420p" else "yuv422p"
    mapping = [
        *quiet,
        *devices,
        *("-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi", "-i", str(jpeg)),
        *("-vf", f"hwmap=derive_device=vulkan,hwdownload,format={pix_fmt}", "-frames:v", "1", "-f", "null", "-"),
    ]
    mapped = _run(mapping, run)
    if isinstance(mapped, str):
        return mapped
    if mapped.returncode != 0:
        return f"the VA-API frame cannot be mapped into Vulkan ({_reason(mapped)})"[:300]
    return IMPORT_REJECTED


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
