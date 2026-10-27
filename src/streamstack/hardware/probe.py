"""The probe: one `snapshot()` of every piece of hardware StreamStack cares about (ADR 0003, section 1).

Each source is a small runner (reads a sysfs or procfs path, or runs one tool with a hard deadline) plus a pure parser
from `parsers`. A source fails alone: the snapshot records `{source, state, error}` for it (`denied` when the file or
node exists but this user may not read it, `absent` when it or its tool is missing, `timeout`, `error`) and every other
source still works. Hardware is identified by PCI id and USB id; `/dev` numbers are only how to open a node right now,
and the host name is never read.

Every path and every process goes through a `Host`, so tests point the probe at a fake sysfs and a fake tool runner.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from streamstack import system

from . import parsers
from .model import (
    V4L2_CAP_VIDEO_CAPTURE,
    V4L2_CAP_VIDEO_CAPTURE_MPLANE,
    FfmpegInfo,
    LoopbackState,
    PipewireState,
    Snapshot,
    SourceStatus,
    V4l2Format,
    V4l2Node,
)
from .parsers import ParseError, ProbeError, SourceFailure

SOURCES = ("usb", "pci", "drm", "vaapi", "vulkan", "alsa", "pipewire", "v4l2", "loopback", "ffmpeg", "machine")
TOOL_TIMEOUT = 8.0
VAINFO_TIMEOUT = 10.0
Run = Callable[[list[str], float], "subprocess.CompletedProcess[str] | None"]


@dataclass
class Host:
    """Where the probe reads from. The defaults are the real machine; tests pass a fake tree and a fake `run`."""

    sys: Path = Path("/sys")
    proc: Path = Path("/proc")
    dev: Path = Path("/dev")
    etc: Path = Path("/etc")
    run: Run = system.run
    which: Callable[[str], str | None] = shutil.which
    clock: Callable[[], float] = time.time
    timeout: float = TOOL_TIMEOUT

    def access(self, path: Path) -> str | None:
        """ "rw" when this user can open the node for reading and writing, "denied" when it exists but cannot be
        opened, None when there is no such node."""
        if not path.exists():
            return None
        return "rw" if os.access(path, os.R_OK | os.W_OK) else "denied"


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    """A small sysfs or procfs file, stripped; None when it is not there. A permission error is a `denied` source."""
    try:
        return path.read_text(errors="replace").strip()
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise SourceFailure("denied", f"{path}: permission denied") from exc
    except OSError as exc:
        if exc.errno in (19, 20):  # ENODEV, ENOTDIR: the node vanished while it was read
            return None
        raise SourceFailure("error", f"{path}: {exc.strerror or exc}") from exc


_DENIED = re.compile(r"Permission denied|Operation not permitted", re.I)


def _capture(host: Host, argv: list[str], timeout: float | None = None) -> tuple[int, str]:
    """Run a tool; (exit code, stdout and stderr). Raises SourceFailure when it is missing or does not finish."""
    if not host.which(argv[0]):
        raise SourceFailure("absent", f"{argv[0]} is not installed")
    limit = timeout or host.timeout
    result = host.run(argv, limit)
    if result is None:
        raise SourceFailure("timeout", f"{argv[0]} did not finish within {limit:g} s")
    return result.returncode, (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")


def _tool(host: Host, argv: list[str], timeout: float | None = None) -> str:
    """The stdout of a tool that must succeed; its failure is the source's failure."""
    if not host.which(argv[0]):
        raise SourceFailure("absent", f"{argv[0]} is not installed")
    limit = timeout or host.timeout
    result = host.run(argv, limit)
    if result is None:
        raise SourceFailure("timeout", f"{argv[0]} did not finish within {limit:g} s")
    if result.returncode != 0:
        message = next((ln.strip() for ln in (result.stderr or result.stdout or "").splitlines() if ln.strip()), "")
        raise SourceFailure(
            "denied" if _DENIED.search(message) else "error", f"{argv[0]} failed: {message or result.returncode}"[:200]
        )
    return result.stdout or ""


def read_usb(host: Host) -> str:
    """The USB bus from sysfs, as `lsusb` prints it (so one parser serves the live bus and the recordings)."""
    root = host.sys / "bus" / "usb" / "devices"
    if not root.is_dir():
        raise SourceFailure("absent", "sysfs has no USB bus")
    lines = []
    for entry in sorted(root.iterdir()):
        if ":" in entry.name:  # an interface of a device
            continue
        vid, pid = _read(entry / "idVendor"), _read(entry / "idProduct")
        bus, address = _read(entry / "busnum"), _read(entry / "devnum")
        if not (vid and pid and bus and address and bus.isdigit() and address.isdigit()):
            continue
        lines.append(
            f"Bus {int(bus):03d} Device {int(address):03d}: ID {vid}:{pid} {_read(entry / 'product') or ''}".rstrip()
        )
    return "\n".join(lines)


def read_pci(host: Host) -> str:
    return _tool(host, ["lspci", "-nn"])


_UEVENT = re.compile(r"^(\w+)=(.*)$")


def read_drm(host: Host, notes: list[str]) -> str:
    """Render nodes from sysfs: "renderD128 0000:03:00.0 driver=i915 dev=0x56a5 vendor=0x8086"."""
    root = host.sys / "class" / "drm"
    if not root.is_dir():
        raise SourceFailure("absent", "sysfs has no DRM devices")
    lines = []
    for entry in sorted(root.glob("renderD*")):
        fields = dict(
            m.groups() for m in map(_UEVENT.match, (_read(entry / "device" / "uevent") or "").splitlines()) if m
        )
        pci_id, slot = fields.get("PCI_ID", ""), fields.get("PCI_SLOT_NAME", "")
        if ":" not in pci_id or not slot:
            notes.append(f"{entry.name} is not a PCI device and was left out")
            continue
        vendor, device = pci_id.lower().split(":", 1)
        lines.append(f"{entry.name} {slot} driver={fields.get('DRIVER', '')} dev=0x{device} vendor=0x{vendor}")
    return "\n".join(lines)


def read_vaapi(host: Host, nodes: tuple[Any, ...]) -> str:
    """`vainfo --display drm --device <node>` for every render node, under "== <node>" headers."""
    if not nodes:
        return ""
    if not host.which("vainfo"):
        raise SourceFailure("absent", "vainfo is not installed")
    blocks = []
    for node in nodes:
        blocks.append(f"== {node.path}")
        if node.access == "denied":
            blocks.append(f"error: {node.path}: Permission denied")
            continue
        try:
            _code, text = _capture(host, ["vainfo", "--display", "drm", "--device", node.path], VAINFO_TIMEOUT)
        except SourceFailure as exc:
            blocks.append(f"error: {exc}")
            continue
        blocks.append(text)
    return "\n".join(blocks)


def read_vulkan(host: Host) -> str:
    return _tool(host, ["vulkaninfo", "--summary"])


def read_alsa(host: Host) -> str:
    """Card ids, USB ids and USB stream capabilities from /proc/asound, in the shape `parse_alsa` reads."""
    root = host.proc / "asound"
    if not root.is_dir():
        raise SourceFailure("absent", "the kernel has no ALSA sound cards (/proc/asound)")
    lines: list[str] = []
    cards = sorted((c for c in root.glob("card[0-9]*") if c.name[4:].isdigit()), key=lambda c: int(c.name[4:]))
    for card in cards:
        number = card.name[4:]
        for kind in ("id", "usbid"):
            value = _read(card / kind)
            if value:
                lines.append(f"/proc/asound/card{number}/{kind}:{value}")
        for stream in sorted(card.glob("stream[0-9]*")):
            body = _read(stream)
            if body:
                lines.append(f"== /proc/asound/card{number}/{stream.name}")
                lines.append(body)
    return "\n".join(lines)


def read_pipewire(host: Host, notes: list[str]) -> tuple[str, str, str, str]:
    """(cards, nodes, status, profiles) text. Cards and nodes are required; the defaults and card profiles are
    optional and recorded in `notes` when missing."""
    cards = _tool(host, ["pactl", "list", "short", "cards"])
    sinks = _tool(host, ["pactl", "list", "short", "sinks"])
    sources = _tool(host, ["pactl", "list", "short", "sources"])
    status = profiles = ""
    try:
        status = _tool(host, ["wpctl", "status"])
    except SourceFailure as exc:
        notes.append(f"no default sink or source: {exc}")
    try:
        profiles = _tool(host, ["pactl", "-f", "json", "list", "cards"])
    except SourceFailure as exc:
        notes.append(f"no card profiles: {exc}")
    return cards, f"# sinks\n{sinks}\n# sources\n{sources}", status, profiles


_PCI_SLOT = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


def _usb_origin(entry: Path) -> tuple[str, str] | None:
    """(bus info, USB id) of the USB device behind a sysfs video4linux entry, like `usb-0000:00:14.0-4`."""
    parts = list((entry / "device").resolve().parents)
    parts.insert(0, (entry / "device").resolve())
    for part in parts:
        vid, pid = _read(part / "idVendor"), _read(part / "idProduct")
        if vid and pid:
            pci = next((p.name for p in parts if _PCI_SLOT.match(p.name)), "")
            port = part.name.partition("-")[2]
            return (f"usb-{pci}-{port}" if pci else f"usb-{part.name}"), f"{vid}:{pid}".lower()
    return None


def read_v4l2_listing(host: Host) -> tuple[str, str, list[str]]:
    """(`--list-devices` style text, "/dev/videoN vid:pid" lines, notes) built from /sys/class/video4linux, which
    stays readable when the nodes themselves are not."""
    root = host.sys / "class" / "video4linux"
    if not root.is_dir():
        raise SourceFailure("absent", "sysfs has no video4linux devices")
    groups: dict[tuple[str, str], list[str]] = {}
    usb: list[str] = []
    for entry in sorted(root.glob("video[0-9]*"), key=lambda e: int(e.name[5:] or 0)):
        label = _read(entry / "name") or entry.name
        origin = _usb_origin(entry)
        if origin:
            bus, usb_id = origin
            usb.append(f"/dev/{entry.name} {usb_id}")
        else:
            device = (entry / "device").resolve().name
            bus = f"platform:{device}" if device else "unknown"
        groups.setdefault((label, bus), []).append(f"/dev/{entry.name}")
    text = "\n\n".join(
        f"{label} ({bus}):\n" + "\n".join(f"\t{p}" for p in paths) for (label, bus), paths in groups.items()
    )
    return text, "\n".join(usb), []


def read_v4l2_info(host: Host, paths: list[str], notes: list[str]) -> str:
    """`v4l2-ctl --all` for each node under "== /dev/videoN"; a node this user cannot open says so."""
    if not host.which("v4l2-ctl"):
        notes.append("v4l2-ctl is not installed: no capabilities or formats")
        return ""
    blocks = []
    for path in paths:
        blocks.append(f"== {path}")
        node = host.dev / Path(path).name
        if host.access(node) == "denied":
            blocks.append(f"Failed to open {path}: Permission denied")
            continue
        try:
            _code, text = _capture(host, ["v4l2-ctl", "-d", path, "--all"])
        except SourceFailure as exc:
            blocks.append(str(exc))
            continue
        blocks.append(text)
    return "\n".join(blocks)


def read_v4l2_formats(host: Host, path: str) -> str:
    node = host.dev / Path(path).name
    if host.access(node) == "denied":
        return f"Failed to open {path}: Permission denied"
    _code, text = _capture(host, ["v4l2-ctl", "-d", path, "--list-formats-ext"])
    return text


def read_loopback(host: Host, notes: list[str]) -> str:
    """The module's version and use count and the modprobe options, as modinfo, lsmod and modprobe.d print them."""
    lines: list[str] = []
    version = _read(host.sys / "module" / "v4l2loopback" / "version")
    if version:
        lines.append(f"version: {version}")
    modules = _read(host.proc / "modules") or ""
    for line in modules.splitlines():
        fields = line.split()
        if fields and fields[0] == "v4l2loopback":
            lines.append(" ".join(fields[:3]))
    try:
        for conf in sorted((host.etc / "modprobe.d").glob("*.conf")):
            for line in (_read(conf) or "").splitlines():
                if line.strip().startswith("options v4l2loopback"):
                    lines.append(line.strip())
    except SourceFailure as exc:
        notes.append(f"modprobe options not readable: {exc}")
    if not lines:
        raise SourceFailure("absent", "v4l2loopback is neither loaded nor configured")
    return "\n".join(lines)


def read_ffmpeg(host: Host) -> str:
    return _tool(host, ["ffmpeg", "-hide_banner", "-encoders"], 15.0)


def read_machine(host: Host) -> str:
    text = _read(host.etc / "machine-id")
    if text:
        return text
    uuid = _read(host.sys / "class" / "dmi" / "id" / "product_uuid")
    if uuid:
        return uuid
    raise SourceFailure("absent", "no machine id (/etc/machine-id) and no DMI product uuid")


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def _record(name: str, build: Callable[[list[str]], Any], default: Any) -> tuple[Any, SourceStatus]:
    """Run one source. Any failure is recorded on that source and the default stands in for its value."""
    notes: list[str] = []
    try:
        value = build(notes)
    except ProbeError as exc:
        return default, SourceStatus(name, exc.state, str(exc) or type(exc).__name__)
    except PermissionError as exc:
        return default, SourceStatus(name, "denied", f"{exc.filename or name}: permission denied")
    except FileNotFoundError as exc:
        return default, SourceStatus(name, "absent", f"{exc.filename or name} is not there")
    except subprocess.SubprocessError as exc:
        return default, SourceStatus(name, "error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # a parser bug on odd input must cost one source, not the snapshot; the type is recorded
        return default, SourceStatus(name, "error", f"{type(exc).__name__}: {exc}")
    return value, SourceStatus(name, "ok", None, tuple(notes))


def snapshot(host: Host | None = None) -> Snapshot:
    """Read every source once. Never raises: a source that fails is recorded in `Snapshot.sources`."""
    host = host or Host()
    statuses: list[SourceStatus] = []

    def source(name: str, build: Callable[[list[str]], Any], default: Any) -> Any:
        value, status = _record(name, build, default)
        statuses.append(status)
        return value

    usb = source("usb", lambda n: _usb(host), ())
    pci = source("pci", lambda n: parsers.parse_lspci(read_pci(host)), ())
    nodes = source("drm", lambda n: _render_nodes(host, pci, n), ())
    va = source("vaapi", lambda n: parsers.parse_vainfo(read_vaapi(host, nodes)), {})
    nodes = tuple(replace(n, va=va.get(n.path)) if n.path in va else n for n in nodes)
    vulkan = source("vulkan", lambda n: parsers.parse_vulkaninfo(read_vulkan(host)), ())
    alsa = source("alsa", lambda n: parsers.parse_alsa(read_alsa(host)), ())
    pipewire = source("pipewire", lambda n: parsers.parse_pipewire(*read_pipewire(host, n)), PipewireState())
    v4l2 = source("v4l2", lambda n: _v4l2(host, n), ())
    loopback = source("loopback", lambda n: parsers.parse_loopback(read_loopback(host, n), v4l2), LoopbackState())
    ffmpeg = source("ffmpeg", lambda n: parsers.parse_ffmpeg_encoders(read_ffmpeg(host)), None)
    machine = source("machine", lambda n: parsers.parse_machine_id(read_machine(host)), None)
    order = {name: i for i, name in enumerate(SOURCES)}
    return Snapshot(
        sources=tuple(sorted(statuses, key=lambda s: order[s.source])),
        usb=usb,
        pci=pci,
        render_nodes=nodes,
        vulkan=vulkan,
        alsa=alsa,
        pipewire=pipewire,
        v4l2=v4l2,
        loopback=loopback,
        ffmpeg=ffmpeg if isinstance(ffmpeg, FfmpegInfo) else None,
        machine=machine,
        taken_at=host.clock(),
    )


def _usb(host: Host):
    nodes = parsers.parse_lsusb(read_usb(host))
    return tuple(
        replace(u, access=host.access(host.dev / "bus" / "usb" / f"{u.bus:03d}" / f"{u.address:03d}")) for u in nodes
    )


def _render_nodes(host: Host, pci, notes: list[str]):
    nodes = parsers.parse_drm(read_drm(host, notes), pci)
    return tuple(replace(n, access=host.access(host.dev / "dri" / Path(n.path).name)) for n in nodes)


def _captures(entry: dict[str, str | int | bool | None] | None) -> bool:
    caps = entry.get("caps") if entry else None
    return isinstance(caps, int) and bool(caps & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE))


def _v4l2(host: Host, notes: list[str]) -> tuple[V4l2Node, ...]:
    listing, usb_text, _ = read_v4l2_listing(host)
    devices = parsers.parse_v4l2_devices(listing) if listing.strip() else []
    paths = [p for _, _, nodes in devices for p in nodes]
    info = parsers.parse_v4l2_info(read_v4l2_info(host, paths, notes)) if paths else {}
    usb = parsers.parse_v4l2_usb(usb_text)
    capture = [p for _label, bus, nodes in devices if "loopback" not in bus for p in nodes if _captures(info.get(p))]
    formats: dict[str, tuple[V4l2Format, ...] | SourceFailure] = {}
    for path in capture:
        try:
            formats[path] = parsers.parse_v4l2_formats(read_v4l2_formats(host, path))
        except SourceFailure as exc:
            formats[path] = exc
        except ParseError as exc:
            notes.append(f"{path}: {exc}")
    nodes = parsers.build_v4l2(devices, info, usb, formats)
    return tuple(replace(n, access=host.access(host.dev / Path(n.path).name)) for n in nodes)
