"""The probe's records: an immutable, serialisable description of the hardware that is present.

Everything here is a frozen dataclass of tuples, strings and numbers, so a `Snapshot` can be compared for equality
(hot-plug asks "did anything change?"), shared between threads and dumped as JSON. Hardware is identified by PCI id
and USB id, never by a `/dev` number or the host name. Fields that change without the hardware changing (a PipeWire
node going from SUSPENDED to RUNNING, the time of the probe) are excluded from equality with `compare=False`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

# Why a source has no data. "absent": the thing or its tool is not there. "denied": it is there and this user may not
# read it. "error": it answered with something that could not be read. "timeout": it did not answer in time.
STATES = ("ok", "absent", "denied", "error", "timeout")

VA_ENCODE_PREFIX = "VAEntrypointEnc"
VA_DECODE = "VAEntrypointVLD"

# VA-API profile name prefix -> codec family
VA_CODECS = (
    ("VAProfileH264", "h264"),
    ("VAProfileHEVC", "hevc"),
    ("VAProfileAV1", "av1"),
    ("VAProfileVP9", "vp9"),
    ("VAProfileVP8", "vp8"),
    ("VAProfileJPEG", "mjpeg"),
    ("VAProfileMPEG2", "mpeg2"),
)

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_OUTPUT = 0x00000002
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_META_CAPTURE = 0x00800000


@dataclass(frozen=True)
class SourceStatus:
    """What happened when one source was read. A failed source never fails the snapshot."""

    source: str
    state: str = "ok"
    error: str | None = None
    notes: tuple[str, ...] = ()  # the source worked, but without something optional (a helper tool was missing)

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "state": self.state,
            "error": self.error,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class UsbNode:
    usb_id: str  # "0fd9:007d"
    name: str
    bus: int
    address: int
    access: str | None = field(default=None, compare=False)  # "rw" or "denied" on a live probe, None when replayed


@dataclass(frozen=True)
class PciDevice:
    slot: str  # "0000:03:00.0"
    class_code: str  # "0300"
    class_name: str
    vendor: str  # "8086"
    device: str  # "56a5"
    name: str

    @property
    def pci_id(self) -> str:
        return f"{self.vendor}:{self.device}"


@dataclass(frozen=True)
class VaInfo:
    """The VA-API profiles and entrypoints one render node reports (`vainfo --display drm --device <node>`)."""

    state: str = "ok"
    error: str | None = None
    driver: str = ""
    entrypoints: tuple[tuple[str, str], ...] = ()  # (VAProfile..., VAEntrypoint...)

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def encode_profiles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p for p, e in self.entrypoints if e.startswith(VA_ENCODE_PREFIX)))

    def decode_profiles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p for p, e in self.entrypoints if e == VA_DECODE))

    def can_encode(self, va_profile: str, entrypoint: str = "") -> bool:
        return any(
            p == va_profile and e.startswith(VA_ENCODE_PREFIX) and (not entrypoint or e == entrypoint)
            for p, e in self.entrypoints
        )

    def encode_codecs(self) -> tuple[str, ...]:
        return codecs_of(self.encode_profiles())

    def decode_codecs(self) -> tuple[str, ...]:
        return codecs_of(self.decode_profiles())


def codecs_of(profiles: tuple[str, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for profile in profiles:
        for prefix, codec in VA_CODECS:
            if profile.startswith(prefix) and codec not in found:
                found.append(codec)
    return tuple(found)


@dataclass(frozen=True)
class VulkanDevice:
    name: str
    vendor: str  # "8086"
    device: str  # "56a5"
    kind: str = ""  # "discrete", "integrated", "cpu", ...
    driver: str = ""

    @property
    def pci_id(self) -> str:
        return f"{self.vendor}:{self.device}"


@dataclass(frozen=True)
class RenderNode:
    """A DRM render node, identified by the PCI id of its GPU. `path` is only how to open it right now."""

    path: str  # "/dev/dri/renderD129"
    pci_slot: str
    vendor: str  # "8086", empty when the probe could not tell
    device: str  # "56a5"
    driver: str
    name: str = ""  # from lspci when it was available
    access: str | None = field(default=None, compare=False)
    va: VaInfo | None = None

    @property
    def pci_id(self) -> str:
        return f"{self.vendor}:{self.device}" if self.vendor and self.device else ""


@dataclass(frozen=True)
class AlsaStream:
    """One alt setting of a USB audio stream, from /proc/asound/cardN/streamM."""

    direction: str  # "playback" | "capture"
    interface: int
    altset: int
    format: str  # "S24_3LE"
    channels: int
    rates: tuple[int, ...]  # discrete rates, or (low, high) when `continuous`
    continuous: bool = False
    endpoint: str = ""  # "ASYNC", "SYNC", "ADAPTIVE"


@dataclass(frozen=True)
class AlsaCard:
    number: int  # only how to open it right now; never an identity
    id: str  # "XLR"
    usb_id: str | None = None  # from /proc/asound/cardN/usbid when the probe could read it
    name: str = ""  # from the stream header; USB cards only
    usb_path: str = ""
    streams: tuple[AlsaStream, ...] = ()

    def direction(self, direction: str) -> tuple[AlsaStream, ...]:
        return tuple(s for s in self.streams if s.direction == direction)

    def rates(self, direction: str) -> tuple[int, ...]:
        """Every discrete rate the card offers in this direction, ascending."""
        found: set[int] = set()
        for stream in self.direction(direction):
            if stream.continuous:
                found.update(
                    r for r in (44100, 48000, 88200, 96000, 176400, 192000) if stream.rates[0] <= r <= stream.rates[-1]
                )
            else:
                found.update(stream.rates)
        return tuple(sorted(found))

    def formats(self, direction: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.format for s in self.direction(direction)))

    def channels(self, direction: str) -> tuple[int, ...]:
        return tuple(sorted({s.channels for s in self.direction(direction)}))


@dataclass(frozen=True)
class PipewireCard:
    id: int = field(compare=False)  # PipeWire object ids are per session
    name: str  # "alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_<serial>-00"
    driver: str = ""
    profile: str | None = None  # the active card profile when `pactl -f json list cards` was available
    usb_id: str | None = None  # device.vendor.id:device.product.id when known
    has_nodes: bool = False

    @property
    def off(self) -> bool:
        """The card exposes no sinks or sources (profile `off`)."""
        return self.profile == "off" or (self.profile is None and not self.has_nodes)


@dataclass(frozen=True)
class PipewireNode:
    id: int = field(compare=False)
    name: str
    kind: str  # "sink" | "source" | "monitor"
    format: str = ""  # "s24le"
    channels: int = 0
    rate: int = 0
    card: str | None = None  # the PipewireCard.name that owns it, when it belongs to one
    state: str = field(default="", compare=False)  # RUNNING, IDLE, SUSPENDED: changes without hardware changing


@dataclass(frozen=True)
class PipewireState:
    cards: tuple[PipewireCard, ...] = ()
    nodes: tuple[PipewireNode, ...] = ()
    default_sink: str | None = None  # node name
    default_source: str | None = None
    default_sink_label: str = ""  # the description wpctl shows, when the node name could not be resolved
    default_source_label: str = ""
    configured_sink: str | None = None  # the user's configured default, which may not exist right now
    configured_source: str | None = None

    def nodes_of(self, card: str, kind: str) -> tuple[PipewireNode, ...]:
        return tuple(n for n in self.nodes if n.card == card and n.kind == kind)


@dataclass(frozen=True)
class V4l2Format:
    fourcc: str
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class V4l2Node:
    path: str  # "/dev/video0": only how to open it right now
    label: str  # the card label ("Cam Link 4K: Cam Link 4K", "Canon EOS 50D")
    bus_info: str = ""  # "usb-0000:00:14.0-4", "platform:v4l2loopback-042"
    driver: str = ""
    usb_id: str | None = None
    caps: int | None = None
    access: str | None = field(default=None, compare=False)
    formats: tuple[V4l2Format, ...] = ()
    formats_state: str = "ok"  # "denied" when the node could not be opened to list them

    @property
    def kind(self) -> str:
        """ "capture", "output", "metadata", or "unknown" when the capabilities could not be read."""
        if self.caps is None:
            return "unknown"
        if self.caps & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE):
            return "capture"
        if self.caps & V4L2_CAP_META_CAPTURE:
            return "metadata"
        if self.caps & V4L2_CAP_VIDEO_OUTPUT:
            return "output"
        return "unknown"

    @property
    def loopback(self) -> bool:
        return "v4l2loopback" in self.bus_info or "loopback" in self.driver.replace(" ", "")


@dataclass(frozen=True)
class LoopbackDevice:
    path: str
    label: str


@dataclass(frozen=True)
class LoopbackState:
    loaded: bool = False
    version: str = ""
    options: tuple[tuple[str, str], ...] = ()  # the modprobe options in file order
    devices: tuple[LoopbackDevice, ...] = ()

    def option(self, key: str) -> str | None:
        return next((v for k, v in self.options if k == key), None)


@dataclass(frozen=True)
class FfmpegInfo:
    encoders: tuple[str, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """What is present, right now, and how each source fared."""

    sources: tuple[SourceStatus, ...] = ()
    usb: tuple[UsbNode, ...] = ()
    pci: tuple[PciDevice, ...] = ()
    render_nodes: tuple[RenderNode, ...] = ()
    vulkan: tuple[VulkanDevice, ...] = ()
    alsa: tuple[AlsaCard, ...] = ()
    pipewire: PipewireState = field(default_factory=PipewireState)
    v4l2: tuple[V4l2Node, ...] = ()
    loopback: LoopbackState = field(default_factory=LoopbackState)
    ffmpeg: FfmpegInfo | None = None
    machine: str | None = None  # a salted hash of the machine id, never the id itself
    taken_at: float = field(default=0.0, compare=False)

    def status(self, source: str) -> SourceStatus | None:
        return next((s for s in self.sources if s.source == source), None)

    def usb_ids(self) -> frozenset[str]:
        return frozenset(u.usb_id for u in self.usb)

    def pci_ids(self) -> frozenset[str]:
        ids = {d.pci_id for d in self.pci}
        ids.update(n.pci_id for n in self.render_nodes if n.pci_id)
        return frozenset(ids)

    def render_node_for(self, pci_id: str) -> RenderNode | None:
        return next((n for n in self.render_nodes if n.pci_id == pci_id), None)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["sources"] = [s.to_dict() for s in self.sources]
        return data
