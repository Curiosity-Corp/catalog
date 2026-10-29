"""The encode tap: which hardware encoder the engine can feed, codec first, so a new codec is one table row.

The program graph ends in one `hwdownload`. Every sink shares that frame: /dev/video43 takes it as YUYV, and each
tap converts it to NV12 and hands it to an encoder that reads system-memory frames (the Quick Sync encoders do;
the Vulkan video encoders do not exist on this driver, and VA-API encoders cannot join a graph whose filter device
is Vulkan, both verified in docs/native-video-engine.md). The preference is by codec, best compression first, and
inside a codec Quick Sync, then VA-API, then NVENC:

    av2   (empty on purpose: the slot a real-time hardware AV2 encoder drops into)
    av1   av1_qsv, av1_vaapi, av1_nvenc
    hevc  hevc_qsv, hevc_vaapi, hevc_nvenc
    h264  h264_qsv, h264_vaapi, h264_nvenc

Quick Sync leads because it joins the Vulkan graph (it opens its own device and takes the system-memory frame).
VA-API is the second choice for the same GPU: it needs the graph's filter device to be VA-API, so it only renders
into the CPU graph (see `graph.build_argv`), but it is the path that still works when the oneVPL runtime cannot
(hevc_qsv fails on the Arc A380 with libmfx-gen 26.1, hevc_vaapi does not). NVENC goes last: it only exists on an
NVIDIA part, and its probe is the cheapest to fail.

Nothing here records or streams. It chooses and builds the `Tap` the engine renders into its command line; the
recording and transport code (streamstack.stream) decides when a tap exists.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from streamstack.engine.graph import Tap

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 15.0


@dataclass(frozen=True)
class Encoder:
    codec: str
    name: str  # the libav encoder name
    backend: str  # qsv | vaapi | nvenc
    args: tuple[str, ...]  # low-latency flags that suit a live tap

    @property
    def label(self) -> str:
        return f"{self.codec.upper()} on the GPU ({self.backend})"


# Best codec first; inside a codec, best backend first (the module docstring says why). VA-API flags follow
# stream.ffcmd.encoder_args: one frame in flight, no B-frames (AV1 gets b_depth 1, the closest it has), low-power
# (fixed-function) mode, which is the only encode entrypoint the Arc exposes. AV2 has no row until real-time
# hardware exists.
ENCODERS: tuple[Encoder, ...] = (
    Encoder("av1", "av1_qsv", "qsv", ("-preset", "veryfast", "-low_power", "1")),
    Encoder("av1", "av1_vaapi", "vaapi", ("-low_power", "1", "-async_depth", "1", "-b_depth", "1")),
    Encoder("av1", "av1_nvenc", "nvenc", ("-preset", "p4", "-tune", "ll")),
    Encoder("hevc", "hevc_qsv", "qsv", ("-preset", "veryfast", "-low_power", "1")),
    Encoder("hevc", "hevc_vaapi", "vaapi", ("-low_power", "1", "-async_depth", "1", "-bf", "0")),
    Encoder("hevc", "hevc_nvenc", "nvenc", ("-preset", "p4", "-tune", "ll")),
    Encoder("h264", "h264_qsv", "qsv", ("-preset", "veryfast", "-low_power", "1")),
    Encoder("h264", "h264_vaapi", "vaapi", ("-low_power", "1", "-async_depth", "1", "-bf", "0")),
    Encoder("h264", "h264_nvenc", "nvenc", ("-preset", "p4", "-tune", "ll")),
)
CODECS = ("av1", "hevc", "h264")


class NoEncoder(Exception):
    """No hardware encoder works here (the message names what was tried)."""


Probe = Callable[[Encoder], bool]


def qsv_device(node: str = "") -> str:
    """The `-init_hw_device` value for Quick Sync, on `node` when one is given (else the first GPU libva finds)."""
    return f"qsv=q:hw,child_device={node},child_device_type=vaapi" if node else "qsv=q:hw"


def vaapi_device(node: str = "") -> str:
    """The `-init_hw_device` value for VA-API, on `node` when one is given (else libva's default device)."""
    return f"vaapi=va:{node}" if node else "vaapi=va"


def probe_encoder(encoder: Encoder, ffmpeg: str = "ffmpeg", node: str = "") -> bool:
    """True when `encoder` really encodes a few frames on this machine (a listed encoder is not a working one)."""
    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if encoder.backend == "qsv":
        argv += ["-init_hw_device", qsv_device(node)]
    elif encoder.backend == "vaapi":
        argv += ["-init_hw_device", vaapi_device(node), "-filter_hw_device", "va"]
    # the VA-API encoders take GPU frames only, so the probe uploads exactly as the stream path does
    chain = "format=nv12,hwupload" if encoder.backend == "vaapi" else "format=nv12"
    argv += ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30", "-frames:v", "8", "-vf", chain]
    argv += ["-c:v", encoder.name, *encoder.args, "-f", "null", "-"]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("encoder probe %s failed to run: %s", encoder.name, exc)
        return False
    if result.returncode != 0:
        log.debug("encoder probe %s: %s", encoder.name, result.stderr.decode(errors="replace").strip()[-200:])
    return result.returncode == 0


def choose_encoder(prefer: str | None = None, probe: Probe = probe_encoder) -> Encoder:
    """The best working encoder. `prefer` ("av1", "hevc", "h264") starts the search at that codec."""
    if prefer is not None and prefer not in CODECS:
        raise NoEncoder(f"Unknown codec {prefer!r}. Use one of: {', '.join(CODECS)}.")
    order = CODECS[CODECS.index(prefer) :] if prefer else CODECS
    tried: list[str] = []
    for codec in order:
        for encoder in (e for e in ENCODERS if e.codec == codec):
            if probe(encoder):
                return encoder
            tried.append(encoder.name)
    raise NoEncoder("No hardware video encoder works here (tried " + ", ".join(tried) + ").")


def tap_for(encoder: Encoder, url: str, container: str, bitrate_kbps: int) -> Tap:
    """A tap that encodes the program picture with `encoder` into `url` (an ffmpeg output URL)."""
    if bitrate_kbps <= 0:
        raise NoEncoder("The bitrate must be positive.")
    args = ("-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k", "-g", "60", *encoder.args)
    return Tap(url, encoder.name, args, container, upload=encoder.backend == "vaapi")
