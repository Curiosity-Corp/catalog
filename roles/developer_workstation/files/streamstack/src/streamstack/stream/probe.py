"""What this host can do, found by doing it: encoder trials, transport checks and the reason for every "no".

Nothing is assumed from a vendor name. Each candidate encoder gets a three-frame trial encode through the same
filter chain the stream uses (`format=nv12,hwupload` for VA-API and Quick Sync), so "av1_vaapi works" means the
driver, the render node and the permissions all work. A failure is turned into a one-line reason a person can act
on ("no NVIDIA driver", "no read/write permission on /dev/dri/renderD128").

The order of `CANDIDATES` is the selection policy: AV1, then HEVC, then H.264, hardware before software within the
bounds `software_ok` sets. It is hardware-agnostic: NVIDIA (NVENC), Intel (VA-API, Quick Sync) and AMD (VA-API,
and Vulkan is left out until it is worth the quirks) are all just entries that either pass their trial or explain
why not.
"""

from __future__ import annotations

import concurrent.futures
import glob
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from streamstack.stream.ffcmd import EncoderChoice
from streamstack.stream.model import CODEC_RANK
from streamstack.stream.whip import support as whip_support

Runner = Callable[[list[str], float], tuple[int, str]]
TRIAL_SECONDS = 25.0
PROBE_TTL = 600.0
TRIAL_SIZE = "256x144"


@dataclass(frozen=True)
class Candidate:
    name: str
    codec: str
    backend: str  # vaapi | qsv | nvenc | software
    label: str


CANDIDATES = (
    Candidate("av1_vaapi", "av1", "vaapi", "AV1 on the GPU (VA-API)"),
    Candidate("av1_qsv", "av1", "qsv", "AV1 on the GPU (Quick Sync)"),
    Candidate("av1_nvenc", "av1", "nvenc", "AV1 on the GPU (NVENC)"),
    Candidate("hevc_vaapi", "hevc", "vaapi", "HEVC on the GPU (VA-API)"),
    Candidate("hevc_qsv", "hevc", "qsv", "HEVC on the GPU (Quick Sync)"),
    Candidate("hevc_nvenc", "hevc", "nvenc", "HEVC on the GPU (NVENC)"),
    Candidate("h264_vaapi", "h264", "vaapi", "H.264 on the GPU (VA-API)"),
    Candidate("h264_qsv", "h264", "qsv", "H.264 on the GPU (Quick Sync)"),
    Candidate("h264_nvenc", "h264", "nvenc", "H.264 on the GPU (NVENC)"),
    Candidate("libsvtav1", "av1", "software", "AV1 on the CPU (SVT-AV1)"),
    Candidate("libx265", "hevc", "software", "HEVC on the CPU (x265)"),
    Candidate("libx264", "h264", "software", "H.264 on the CPU (x264)"),
)

# stderr fragments to the reason a person can use, first match wins
REASONS: tuple[tuple[str, str], ...] = (
    (
        r"Cannot load libcuda|libnvidia-encode|CUDA_ERROR_NO_DEVICE|No NVENC capable devices",
        "no NVIDIA GPU or driver (NVENC cannot load)",
    ),
    (
        r"OpenEncodeSessionEx failed|Driver does not support the required nvenc API version",
        "the NVIDIA driver is too old for this ffmpeg's NVENC",
    ),
    (
        r"No VA display found|Failed to initialise VAAPI connection|vaInitialize failed"
        r"|Cannot open the DRM render node",
        "VA-API cannot open the render node (driver missing or no permission)",
    ),
    (
        r"No usable encoding profile found|Hardware does not support encoding|no encoding entrypoint|does not support",
        "the GPU has no hardware encoder for this codec",
    ),
    (
        r"Error creating a MFX session|MFX session|Failed to create .*QSV"
        r"|Error initializing an internal frame pool|libvpl",
        "Quick Sync (oneVPL) cannot open the GPU",
    ),
    (r"Unknown encoder|Encoder .* not found", "this ffmpeg build does not include the encoder"),
    (r"Permission denied", "no permission to use the device"),
    (r"No such file or directory", "the device does not exist"),
)
NOISE = re.compile(
    r"^(libva info|Svt\[|x265 \[|\[libx264|ffmpeg version|\s*built with|\s*configuration|\s*lib\w+\s+\d)"
)


@dataclass
class EncoderProbe:
    name: str
    codec: str
    backend: str
    label: str
    device: str = ""
    ok: bool = False
    reason: str = ""
    trial_ms: int = 0
    pci_id: str = ""  # the GPU behind `device` ("8086:56a5"), so the Plan's choice survives a node renumbering

    def on(self, node: str, pci_id: str = "") -> bool:
        """Whether this trial ran on the GPU the hardware Plan chose (by PCI id first, the node path second)."""
        if not self.device:
            return False
        return bool(pci_id and self.pci_id == pci_id) or bool(node and self.device == node)

    def choice(self) -> EncoderChoice:
        return EncoderChoice(self.name, self.codec, self.backend, self.device, self.label)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "codec": self.codec,
            "backend": self.backend,
            "label": self.label,
            "device": self.device,
            "pci_id": self.pci_id,
            "ok": self.ok,
            "reason": self.reason,
            "trial_ms": self.trial_ms,
        }


@dataclass
class TransportProbe:
    id: str
    label: str
    ok: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "ok": self.ok, "reason": self.reason, "detail": self.detail}


@dataclass
class HostProbe:
    ffmpeg: str = ""
    version: str = ""
    cores: int = 1
    render_nodes: list[str] = field(default_factory=list)
    encoders: list[EncoderProbe] = field(default_factory=list)
    transports: list[TransportProbe] = field(default_factory=list)
    at: float = 0.0
    problems: list[str] = field(default_factory=list)

    def working(self, codec: str | None = None) -> list[EncoderProbe]:
        return [e for e in self.encoders if e.ok and (codec is None or e.codec == codec)]

    def available_codecs(self, size: tuple[int, int] = (1920, 1080), fps: float = 30.0) -> set[str]:
        return {e.codec for e in self.working() if software_ok(e, size, fps, self.cores)}

    def best(
        self, codec: str, size: tuple[int, int] = (1920, 1080), fps: float = 30.0, pin: str = "", pci_id: str = ""
    ) -> EncoderProbe | None:
        """The preferred working encoder for `codec`; hardware first, software only where it can keep up. `pin`
        (a render node) and `pci_id` name the GPU the hardware Plan chose: encoders that ran there win, and when
        none did the first working one is used, as before."""
        pool = [e for e in self.working(codec) if software_ok(e, size, fps, self.cores)]
        if pin or pci_id:
            pinned = [e for e in pool if e.on(pin, pci_id)]
            pool = pinned or pool
        return pool[0] if pool else None

    def find(self, name: str, device: str = "") -> EncoderProbe | None:
        for enc in self.encoders:
            if enc.name == name and enc.ok and (not device or enc.device == device):
                return enc
        return None

    def transport(self, transport_id: str) -> TransportProbe | None:
        return next((t for t in self.transports if t.id == transport_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ffmpeg": self.ffmpeg,
            "version": self.version,
            "cores": self.cores,
            "render_nodes": self.render_nodes,
            "encoders": [e.to_dict() for e in self.encoders],
            "transports": [t.to_dict() for t in self.transports],
            "codecs": sorted(self.available_codecs(), key=CODEC_RANK.__getitem__),
            "problems": self.problems,
            "age_s": round(time.time() - self.at, 1) if self.at else None,
        }


def software_ok(enc: EncoderProbe, size: tuple[int, int], fps: float, cores: int) -> bool:
    """Whether a CPU encoder can be trusted to hold real time at this picture size on this many cores."""
    if enc.backend != "software":
        return True
    pixels = size[0] * size[1] * max(fps, 1) / (1920 * 1080 * 30)
    if enc.codec == "av1":
        return cores >= 8 and pixels <= 1.0
    if enc.codec == "hevc":
        return cores >= 6 and pixels <= 1.0
    return cores >= 2 and pixels <= 2.1


def default_runner(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LIBVA_MESSAGING_LEVEL": "0"},
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stderr or "") + (done.stdout or "")


PREFIX = re.compile(r"\[[^\]]*@ 0x[0-9a-f]+\]\s*")
PROBLEM = re.compile(r"error|invalid|failed|cannot|unable|unsupported|not supported", re.I)


def explain(output: str) -> str:
    """One line a person can act on, from an encoder trial's output."""
    for pattern, reason in REASONS:
        if re.search(pattern, output, re.I):
            return reason
    lines = [PREFIX.sub("", ln.strip()) for ln in output.splitlines() if ln.strip() and not NOISE.match(ln)]
    first = next((ln for ln in lines if PROBLEM.search(ln)), lines[-1] if lines else "the trial encode failed")
    return first[:200]


def render_nodes() -> list[str]:
    return sorted(glob.glob("/dev/dri/renderD*"))


def node_pci_id(node: str, sysfs: Path = Path("/sys/class/drm")) -> str:
    """ "8086:56a5" for /dev/dri/renderD129, read from sysfs; empty when it cannot be read."""
    base = sysfs / Path(node).name / "device"
    try:
        return f"{(base / 'vendor').read_text().strip()[2:]}:{(base / 'device').read_text().strip()[2:]}".lower()
    except OSError:
        return ""


def render_signature(sysfs: Path = Path("/sys/class/drm")) -> tuple[tuple[str, str], ...]:
    """Which GPU is behind which render node. It changes when a GPU is added, removed or renumbered."""
    return tuple((n, node_pci_id(n, sysfs)) for n in render_nodes())


def trial_argv(ffmpeg: str, cand: Candidate, device: str) -> list[str]:
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    chain = ["format=yuv420p"]
    if cand.backend == "vaapi":
        argv += ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"]
        chain = ["format=nv12", "hwupload"]
    elif cand.backend == "qsv":
        argv += ["-init_hw_device", f"vaapi=va:{device}", "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs"]
        chain = ["format=nv12", "hwupload=extra_hw_frames=16"]
    elif cand.backend == "nvenc":
        chain = ["format=nv12"]
    argv += ["-f", "lavfi", "-i", f"testsrc2=size={TRIAL_SIZE}:rate=30", "-frames:v", "3", "-vf", ",".join(chain)]
    argv += ["-c:v", cand.name, "-f", "null", "-"]
    return argv


OPUS_RTMP_FFMPEG = 8  # the first release whose FLV muxer announces Opus as Enhanced RTMP audio (checked on 8.0.1)


def ffmpeg_major(version: str) -> int:
    """The major number of an ffmpeg version string ("8.0.1-3ubuntu2" is 8); 0 for a git build or an unknown form."""
    match = re.match(r"(\d+)\.\d+", version)
    return int(match.group(1)) if match else 0


def ffmpeg_version(text: str) -> str:
    match = re.search(r"ffmpeg version (\S+)", text)
    return match.group(1) if match else ""


def probe_encoders(
    ffmpeg: str,
    listing: str,
    nodes: list[str],
    runner: Runner,
    cores: int,
    pci_of: Callable[[str], str] = node_pci_id,
) -> list[EncoderProbe]:
    """Trial-encode every candidate (on every render node for VA-API and Quick Sync), concurrently."""
    jobs: list[tuple[Candidate, str]] = []
    results: list[EncoderProbe] = []
    for cand in CANDIDATES:
        if not re.search(rf"^\s*V\S*\s+{re.escape(cand.name)}\s", listing, re.M):
            results.append(
                EncoderProbe(
                    cand.name,
                    cand.codec,
                    cand.backend,
                    cand.label,
                    reason="this ffmpeg build does not include the encoder",
                )
            )
            continue
        if cand.backend in ("vaapi", "qsv"):
            if not nodes:
                results.append(
                    EncoderProbe(
                        cand.name, cand.codec, cand.backend, cand.label, reason="no GPU render node (/dev/dri/renderD*)"
                    )
                )
            for node in nodes:
                if not os.access(node, os.R_OK | os.W_OK):
                    results.append(
                        EncoderProbe(
                            cand.name, cand.codec, cand.backend, cand.label, node,
                            reason=f"no read/write permission on {node} (add this user to the render group)",
                            pci_id=pci_of(node),
                        )
                    )  # fmt: skip
                else:
                    jobs.append((cand, node))
        else:
            jobs.append((cand, ""))

    def trial(job: tuple[Candidate, str]) -> EncoderProbe:
        cand, node = job
        began = time.monotonic()
        code, output = runner(trial_argv(ffmpeg, cand, node), TRIAL_SECONDS)
        took = round((time.monotonic() - began) * 1000)
        probe = EncoderProbe(
            cand.name, cand.codec, cand.backend, cand.label, node, trial_ms=took, pci_id=pci_of(node) if node else ""
        )
        if code == 0:
            probe.ok = True
        else:
            probe.reason = explain(output)
        return probe

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results += list(pool.map(trial, jobs))
    order = {cand.name: i for i, cand in enumerate(CANDIDATES)}
    results.sort(key=lambda e: (order[e.name], e.device))
    return results


def probe_transports(
    ffmpeg: str,
    protocols: str,
    muxers: str,
    rtmp_help: str,
    version: str,
    whip_check: Callable[[], str] = whip_support.check,
) -> list[TransportProbe]:
    out: list[TransportProbe] = []
    out.append(
        TransportProbe(
            "record-mkv", "Recording (Matroska)", True, detail="written by StreamStack, flushed every cluster"
        )
    )
    has_mp4 = re.search(r"^\s*E\S*\s+mp4\s", muxers, re.M) is not None
    out.append(
        TransportProbe(
            "record-mp4", "Recording (fragmented MP4)", has_mp4, "" if has_mp4 else "this ffmpeg build has no MP4 muxer"
        )
    )
    outputs = protocols.split("Output:")[-1] if "Output:" in protocols else protocols
    names = set(outputs.split())
    srt = "srt" in names
    out.append(
        TransportProbe(
            "srt",
            "SRT (caller and listener)",
            srt,
            "" if srt else "this ffmpeg build has no libsrt",
            "MPEG-TS; HEVC and H.264, AV1 only when asked for",
        )
    )
    rtmp = "rtmp" in names and re.search(r"^\s*E\S*\s+flv\s", muxers, re.M) is not None
    out.append(
        TransportProbe(
            "rtmp", "RTMP (classic, H.264 + AAC)", rtmp, "" if rtmp else "this ffmpeg build has no RTMP or FLV support"
        )
    )
    enhanced = rtmp and "rtmp_enhanced_codecs" in rtmp_help
    out.append(
        TransportProbe(
            "rtmp-enhanced",
            "Enhanced RTMP (AV1, HEVC)",
            enhanced,
            ""
            if enhanced
            else (
                f"ffmpeg {version or '?'} has no Enhanced RTMP (needs 6.1 or newer)"
                if rtmp
                else "RTMP itself is unavailable"
            ),
            "the server must support Enhanced RTMP; classic H.264 is the fallback",
        )
    )
    rtmps = rtmp and "rtmps" in names and "tls" in names
    out.append(
        TransportProbe(
            "rtmps",
            "RTMPS (TLS)",
            rtmps,
            "" if rtmps else "this ffmpeg build has no TLS support",
            "the server certificate is checked unless the destination turns tls_verify off",
        )
    )
    opus_ok = enhanced and ffmpeg_major(version) >= OPUS_RTMP_FFMPEG
    out.append(
        TransportProbe(
            "rtmp-opus",
            "Enhanced RTMP audio (Opus)",
            opus_ok,
            ""
            if opus_ok
            else (
                f"ffmpeg {version or '?'} cannot send Opus over FLV (needs ffmpeg {OPUS_RTMP_FFMPEG} or newer)"
                if enhanced
                else "Enhanced RTMP itself is unavailable"
            ),
            "opt-in per destination (options.audio: opus); AAC is the default every server takes",
        )
    )
    reason = whip_check()
    if reason:
        out.append(TransportProbe("whip", "WHIP (WebRTC)", False, reason))
    else:
        out.append(
            TransportProbe(
                "whip",
                "WHIP (WebRTC)",
                True,
                detail=(
                    f"built-in client on aiortc {whip_support.version()}; H.264 + Opus, "
                    "HEVC and AV1 when the destination asks for them by name (the server must accept them)"
                ),
            )
        )
    return out


TRANSPORT_LABELS = (
    ("record-mkv", "Recording (Matroska)"),
    ("record-mp4", "Recording (fragmented MP4)"),
    ("srt", "SRT (caller and listener)"),
    ("rtmp", "RTMP (classic, H.264 + AAC)"),
    ("rtmp-enhanced", "Enhanced RTMP (AV1, HEVC)"),
    ("rtmps", "RTMPS (TLS)"),
    ("rtmp-opus", "Enhanced RTMP audio (Opus)"),
    ("whip", "WHIP (WebRTC)"),
)


def probe_host(
    ffmpeg: str | None = None,
    runner: Runner = default_runner,
    nodes: list[str] | None = None,
    cores: int | None = None,
    whip_check: Callable[[], str] = whip_support.check,
    pci_of: Callable[[str], str] = node_pci_id,
) -> HostProbe:
    """Run every check. Never raises: a missing ffmpeg is a probe with `problems`, not an exception."""
    host = HostProbe(cores=cores or os.cpu_count() or 1, at=time.time())
    path = ffmpeg or shutil.which("ffmpeg") or ""
    if not path:
        host.problems.append("ffmpeg is not installed; recording and streaming need it")
        missing = "ffmpeg is not installed"
        host.transports = [
            TransportProbe("record-mkv", "Recording (Matroska)", True, detail="written by StreamStack itself"),
            *(TransportProbe(tid, label, False, missing) for tid, label in TRANSPORT_LABELS if tid != "record-mkv"),
        ]
        return host
    host.ffmpeg = path
    _code, text = runner([path, "-hide_banner", "-version"], 10)
    host.version = ffmpeg_version(text)
    _, listing = runner([path, "-hide_banner", "-encoders"], 10)
    _, protocols = runner([path, "-hide_banner", "-protocols"], 10)
    _, muxers = runner([path, "-hide_banner", "-muxers"], 10)
    _, rtmp_help = runner([path, "-hide_banner", "-h", "protocol=rtmp"], 10)
    host.render_nodes = render_nodes() if nodes is None else nodes
    host.encoders = probe_encoders(path, listing, host.render_nodes, runner, host.cores, pci_of)
    host.transports = probe_transports(path, protocols, muxers, rtmp_help, host.version, whip_check)
    if not host.working():
        host.problems.append("no working video encoder was found, so nothing can be recorded or streamed")
    return host


class ProbeCache:
    """Probes lazily and remembers the answer for a while (the GPU does not change mid-session)."""

    def __init__(
        self,
        probe: Callable[[], HostProbe] = probe_host,
        ttl: float = PROBE_TTL,
        key: Callable[[], object] | None = None,
    ) -> None:
        self._probe = probe
        self._ttl = ttl
        self._key = key  # what the GPUs look like now: a change means the cached trials ran on other nodes
        self._seen: object = None
        self._value: HostProbe | None = None

    def get(self, refresh: bool = False) -> HostProbe:
        seen = self._key() if self._key is not None else None
        if refresh or self._value is None or seen != self._seen or time.time() - self._value.at > self._ttl:
            self._seen = seen
            self._value = self._probe()
        return self._value

    def peek(self) -> HostProbe | None:
        return self._value


def source_info(device: str) -> dict[str, Any]:
    """Whether a v4l2 device exists and can be read by more than one process (needed for a make-before-break swap)."""
    path = Path(device)
    info: dict[str, Any] = {"device": device, "exists": path.exists(), "multi_reader": False, "name": ""}
    if not info["exists"]:
        return info
    sysdir = Path("/sys/class/video4linux") / path.name
    try:
        info["name"] = (sysdir / "name").read_text().strip()
        info["multi_reader"] = "/virtual/" in os.path.realpath(sysdir)  # v4l2loopback devices are virtual
    except OSError as exc:
        info["note"] = f"cannot inspect {sysdir}: {exc.strerror or exc}"
    return info
