"""Which render node does a job: the Plan's GPU role, found by PCI id, never by node number or host name (ADR 0003).

Four consumers ask, each for the role the hardware profile names:

    engine  -> grade   (the Vulkan graph; MJPEG VA-API decode shares that graph's device, so decode follows grade)
    blur    -> blur    (OpenVINO background segmentation, matched by PCI device id)
    stream  -> encode  (AV1/HEVC/H.264 through VA-API or Quick Sync)
    canon   -> decode  (the Canon bridge's MJPEG decode, which a profile may give to the iGPU)

Order of precedence for every consumer: the explicit setting (it always wins), the Plan's role, then the generic
choice the engine used before profiles existed (a discrete Intel GPU, else the integrated one, else the first node;
the stream falls back to trying every node, which is what its probe does).

`LivePlan` gives processes that do not own a hardware controller (the Canon bridge, the command line) a Plan of
their own. It re-probes when the set of render nodes, USB devices or video nodes changes, so a node that is
renumbered after a hot-plug is resolved again.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from streamstack.hardware import probe, schema
from streamstack.hardware.model import Snapshot
from streamstack.hardware.plan import Plan, resolve

log = logging.getLogger(__name__)

ROLE_OF = {"engine": "grade", "blur": "blur", "stream": "encode", "canon": "decode"}
SETTING_OF = {
    "engine": "STREAMSTACK_ENGINE_RENDER_NODE",
    "blur": "STREAMSTACK_ENGINE_RENDER_NODE",
    "stream": "STREAMSTACK_STREAM_RENDER_NODE",
    "canon": "STREAMSTACK_CANON_RENDER_NODE",
}
ENGINE_DEFAULT = "/dev/dri/renderD128"
INTEL = "0x8086"
# Intel's integrated GPU sits at 00:02.0 on every client chip; a discrete card is behind a bridge.
INTEGRATED_SLOT = "0000:00:02."

PlanSource = Callable[[], Plan | None]


@dataclass(frozen=True)
class NodeChoice:
    consumer: str
    role: str
    path: str  # "" when nothing is preferred (the stream then tries every node)
    pci_id: str | None
    via: str  # "override" | "rig" | "profile" | "generic" | "fallback" | "none"
    name: str = ""

    def to_dict(self) -> dict[str, str | None]:
        return {
            "consumer": self.consumer,
            "role": self.role,
            "node": self.path or None,
            "pci": self.pci_id,
            "via": self.via,
            "name": self.name,
        }


def _slot(node: Path) -> str:
    try:
        return (node / "device").resolve().name
    except OSError:
        return ""


def _vendor(node: Path) -> str:
    try:
        return (node / "device" / "vendor").read_text().strip().lower()
    except OSError:
        return ""


def generic_node(sysfs: Path = Path("/sys/class/drm"), dev: Path = Path("/dev/dri")) -> str:
    """The node the engine used before profiles: Intel only, discrete before integrated, then any node, else
    renderD128. Used when no profile matches or the Plan has no GPU for the role."""
    nodes = sorted(sysfs.glob("renderD*"), key=lambda n: int("".join(c for c in n.name if c.isdigit()) or 0))
    intel = [n for n in nodes if _vendor(n) == INTEL]
    discrete = [n for n in intel if not _slot(n).startswith(INTEGRATED_SLOT)]
    for group in (discrete, intel, nodes):
        if group:
            return str(dev / group[0].name)
    return ENGINE_DEFAULT


def choose(
    consumer: str,
    plan: Plan | None,
    configured: str = "",
    *,
    sysfs: Path = Path("/sys/class/drm"),
    dev: Path = Path("/dev/dri"),
) -> NodeChoice:
    """The render node `consumer` should use. `configured` is the explicit setting; it always wins."""
    role = ROLE_OF[consumer]
    configured = configured.strip()
    if configured:
        match = plan.gpu_by_node(configured) if plan else None
        return NodeChoice(
            consumer, role, configured, match.pci_id if match else None, "override", match.name if match else ""
        )
    gpu = plan.gpu(role) if plan else None
    if gpu is not None:
        return NodeChoice(consumer, role, gpu.node_path, gpu.pci_id, gpu.via, gpu.name)
    if consumer == "stream":
        return NodeChoice(consumer, role, "", None, "none")
    return NodeChoice(consumer, role, generic_node(sysfs, dev), None, "fallback")


def _signature(sysfs: Path, usb: Path, video: Path) -> tuple[tuple[str, ...], ...]:
    """A cheap fingerprint of what is plugged in: render nodes with their PCI slot, USB ids, video node names."""

    def listing(root: Path, pattern: str, read: Callable[[Path], str]) -> tuple[str, ...]:
        try:
            return tuple(sorted(read(p) for p in root.glob(pattern)))
        except OSError:
            return ()

    def text(path: Path) -> str:
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    renders = listing(sysfs, "renderD*", lambda n: f"{n.name}@{_slot(n)}")
    usbs = listing(usb, "*/idVendor", lambda p: f"{text(p)}:{text(p.parent / 'idProduct')}@{p.parent.name}")
    videos = listing(video, "video*", lambda p: f"{p.name}={text(p / 'name')}")
    return renders, usbs, videos


class LivePlan:
    """A Plan kept fresh without a hardware controller: probed on first use, re-probed when the fingerprint of the
    machine changes (and at most every `ttl` seconds regardless). `get()` never raises; it returns the last Plan
    (or None when the probe has never worked) and logs why."""

    def __init__(
        self,
        overrides: Callable[[], Mapping[str, str]] | Mapping[str, str] | None = None,
        *,
        take: Callable[[], Snapshot] = probe.snapshot,
        catalogue: schema.Catalogue | None = None,
        sysfs: Path = Path("/sys/class/drm"),
        usb: Path = Path("/sys/bus/usb/devices"),
        video: Path = Path("/sys/class/video4linux"),
        ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._overrides = overrides
        self._take = take
        self._catalogue = catalogue
        self._paths = (sysfs, usb, video)
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._plan: Plan | None = None
        self._seen: tuple[tuple[str, ...], ...] | None = None
        self._at = 0.0
        self.probes = 0

    def _settings(self) -> Mapping[str, str]:
        source = self._overrides
        if source is None:
            from streamstack.console.hardware import overrides_from

            return overrides_from({}, os.environ)
        return source() if callable(source) else source

    def get(self) -> Plan | None:
        with self._lock:
            now = self._clock()
            seen = _signature(*self._paths)
            if self._plan is not None and seen == self._seen and now - self._at < self._ttl:
                return self._plan
            try:
                snapshot = self._take()
                plan = resolve(snapshot, self._catalogue or schema.packaged(), self._settings())
            except Exception as exc:  # a failed probe must not stop the job: keep the last Plan and say why
                log.warning("hardware probe failed, keeping the previous plan: %s: %s", type(exc).__name__, exc)
                self._at = now
                return self._plan
            self.probes += 1
            self._plan, self._seen, self._at = plan, seen, now
            return plan
