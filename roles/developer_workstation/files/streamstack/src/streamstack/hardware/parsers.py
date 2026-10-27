"""Pure parsers: the text a tool or a /proc file prints, in, records out. No I/O, no clock, no environment.

Each parser is tested against the recorded captures of both studio hosts (tests/fixtures/hardware). A parser raises
`SourceFailure` when the text is a tool failure ("command not found", "Permission denied") and `ParseError` when it
is something else it cannot read; the probe records either on that one source and carries on with the rest.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Mapping

from .model import (
    AlsaCard,
    AlsaStream,
    FfmpegInfo,
    LoopbackDevice,
    LoopbackState,
    PciDevice,
    PipewireCard,
    PipewireNode,
    PipewireState,
    RenderNode,
    UsbNode,
    V4l2Format,
    V4l2Node,
    VaInfo,
    VulkanDevice,
)


class ProbeError(Exception):
    """Base of the probe's failures. `state` is one of model.STATES (other than "ok")."""

    state = "error"


class SourceFailure(ProbeError):
    """The tool or file is not there, may not be read, or did not answer."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


class ParseError(ProbeError):
    """The text was not in the format the parser knows."""

    state = "error"


# ---------------------------------------------------------------------------
# Tool failures
# ---------------------------------------------------------------------------

_FAILURES = (
    (
        re.compile(r"command not found|not installed|No such file or directory|cannot access|: not found", re.I),
        "absent",
    ),
    (re.compile(r"Permission denied|Operation not permitted|not permitted|Access denied", re.I), "denied"),
    (re.compile(r"timed out|timeout", re.I), "timeout"),
)


def failure_of(text: str) -> tuple[str, str] | None:
    """(state, message) when every non-blank line of `text` is a tool failure, else None."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    state = None
    for line in lines:
        for pattern, kind in _FAILURES:
            if pattern.search(line):
                state = state or kind
                break
        else:
            return None
    return (state or "error", lines[0])


def _guard(text: str) -> None:
    failure = failure_of(text)
    if failure:
        raise SourceFailure(*failure)


def _unrecognised(text: str, what: str) -> ParseError:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return ParseError(f"not {what}: {first[:80]!r}")


def _norm_slot(slot: str) -> str:
    return slot if slot.count(":") == 2 else f"0000:{slot}"


# ---------------------------------------------------------------------------
# USB and PCI
# ---------------------------------------------------------------------------

_LSUSB = re.compile(r"^Bus (\d+) Device (\d+): ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*?)\s*$")


def parse_lsusb(text: str) -> tuple[UsbNode, ...]:
    """`lsusb` lines: "Bus 001 Device 010: ID 0fd9:007d Elgato Systems GmbH Elgato Wave XLR"."""
    _guard(text)
    found = []
    for line in text.splitlines():
        match = _LSUSB.match(line)
        if match:
            bus, address, vid, pid, name = match.groups()
            found.append(UsbNode(f"{vid}:{pid}".lower(), name, int(bus), int(address)))
    if not found:
        raise _unrecognised(text, "lsusb output")
    return tuple(sorted(found, key=lambda u: (u.bus, u.address)))


_LSPCI = re.compile(
    r"^(?:([0-9a-f]{4}):)?([0-9a-f]{2}:[0-9a-f]{2}\.[0-7]) (.+?) \[([0-9a-f]{4})\]: "
    r"(.+) \[([0-9a-f]{4}):([0-9a-f]{4})\]"
    r"(?: \(rev [0-9a-f]+\))?\s*$"
)


def parse_lspci(text: str) -> tuple[PciDevice, ...]:
    """`lspci -nn` lines: "03:00.0 VGA compatible controller [0300]: Intel Corporation DG2 [Arc A380] [8086:56a5]"."""
    _guard(text)
    found = []
    for line in text.splitlines():
        match = _LSPCI.match(line)
        if match:
            domain, slot, class_name, class_code, name, vendor, device = match.groups()
            found.append(PciDevice(f"{domain or '0000'}:{slot}", class_code, class_name, vendor, device, name))
    if text.strip() and not found:
        raise _unrecognised(text, "lspci -nn output")
    return tuple(found)


# ---------------------------------------------------------------------------
# DRM render nodes and VA-API
# ---------------------------------------------------------------------------

_DRM = re.compile(r"^(renderD\d+)\s+(\S+)\s+driver=(\S*)\s+dev=0x([0-9a-fA-F]+)(?:\s+vendor=0x([0-9a-fA-F]+))?\s*$")


def parse_drm(text: str, pci: Iterable[PciDevice] = ()) -> tuple[RenderNode, ...]:
    """Render nodes as "renderD128 0000:03:00.0 driver=i915 dev=0x56a5 [vendor=0x8086]".

    The vendor comes from the line when the probe could read it, else from `pci` (lspci) by slot. Without either
    the node keeps an empty vendor and has no PCI id, so no profile can claim it by mistake.
    """
    _guard(text)
    by_slot = {p.slot: p for p in pci}
    found = []
    for line in text.splitlines():
        match = _DRM.match(line)
        if not match:
            continue
        node, slot, driver, device, vendor = match.groups()
        slot = _norm_slot(slot)
        listed = by_slot.get(slot)
        vendor_id = (
            (vendor or (listed.vendor if listed else "")).lower().lstrip("0").zfill(4) if (vendor or listed) else ""
        )
        found.append(
            RenderNode(
                path=f"/dev/dri/{node}",
                pci_slot=slot,
                vendor=vendor_id,
                device=device.lower().zfill(4),
                driver=driver,
                name=listed.name if listed else "",
            )
        )
    if text.strip() and not found:
        raise _unrecognised(text, "render node list")
    return tuple(sorted(found, key=lambda n: n.path))


_VA_HEADER = re.compile(r"^==\s+(/dev/dri/renderD\d+)\s*$")
_VA_ENTRY = re.compile(r"^\s*(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)\s*$")
_VA_DRIVER = re.compile(r"Driver version:\s*(.+?)\s*$")


def parse_vainfo(text: str) -> dict[str, VaInfo]:
    """`vainfo --display drm --device <node>` output per node, each under a "== /dev/dri/renderDN" header.

    A node whose block lists no profiles is recorded with the reason (denied or the first error line) instead of
    being dropped, so one bad node does not hide the others.
    """
    blocks = {groups[0]: lines for groups, lines in _ordered_blocks(text, _VA_HEADER)}
    if text.strip() and not blocks:
        _guard(text)
        raise _unrecognised(text, "per-node vainfo output")
    parsed: dict[str, VaInfo] = {}
    for node, lines in blocks.items():
        entries = tuple(dict.fromkeys(m.groups() for m in map(_VA_ENTRY.match, lines) if m))
        driver = next((m.group(1) for m in map(_VA_DRIVER.search, lines) if m), "")
        if entries:
            parsed[node] = VaInfo("ok", None, driver, entries)
            continue
        failure = failure_of("\n".join(lines)) or next(
            (("error", ln.strip()) for ln in lines if re.search(r"error|failed|denied", ln, re.I)), None
        )
        state, message = failure or ("error", "vainfo listed no profiles")
        if re.search(r"Permission denied|Operation not permitted", "\n".join(lines)):
            state = "denied"
        parsed[node] = VaInfo(state, message, driver, ())
    return parsed


_KV = re.compile(r"^\s*(\w+)\s*=\s*(.*?)\s*$")
_VK_GPU = re.compile(r"^GPU(\d+):\s*$")


def parse_vulkaninfo(text: str) -> tuple[VulkanDevice, ...]:
    """`vulkaninfo --summary`: one "GPUn:" block per physical device with vendorID, deviceID and deviceName."""
    _guard(text)
    devices: list[dict[str, str]] = []
    for line in text.splitlines():
        if _VK_GPU.match(line):
            devices.append({})
            continue
        match = _KV.match(line)
        if match and devices:
            devices[-1].setdefault(match.group(1), match.group(2))
    found = []
    for fields in devices:
        vendor = fields.get("vendorID", "").lower().removeprefix("0x")
        device = fields.get("deviceID", "").lower().removeprefix("0x")
        if not (vendor and device and fields.get("deviceName")):
            continue
        kind = fields.get("deviceType", "").removeprefix("PHYSICAL_DEVICE_TYPE_").lower().removesuffix("_gpu")
        found.append(
            VulkanDevice(fields["deviceName"], vendor.zfill(4), device.zfill(4), kind, fields.get("driverName", ""))
        )
    if text.strip() and not found and "GPU" in text:
        raise _unrecognised(text, "vulkaninfo --summary output")
    return tuple(found)


# ---------------------------------------------------------------------------
# ALSA
# ---------------------------------------------------------------------------

_ALSA_ID = re.compile(r"^/proc/asound/card(\d+)/(id|usbid):\s*(.*?)\s*$")
_ALSA_STREAM_HEADER = re.compile(r"^==\s+/proc/asound/card(\d+)/stream(\d+)\s*$")
_ALSA_TITLE = re.compile(r"^(.*?)\s+at\s+(usb-\S+?),\s")
_ALSA_USBID = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$")


def _alsa_streams(lines: list[str]) -> tuple[AlsaStream, ...]:
    streams: list[AlsaStream] = []
    direction = ""
    interface = altset = -1
    fields: dict[str, str] = {}

    def flush() -> None:
        if direction and "Format" in fields and "Channels" in fields and "Rates" in fields:
            rates_text = fields["Rates"]
            rates = tuple(int(r) for r in re.findall(r"\d+", rates_text))
            continuous = "continuous" in rates_text and len(rates) >= 2
            endpoint = re.search(r"\((ASYNC|SYNC|ADAPTIVE)\)", fields.get("Endpoint", ""))
            streams.append(
                AlsaStream(
                    direction,
                    interface,
                    altset,
                    fields["Format"],
                    int(fields["Channels"]),
                    (rates[0], rates[-1]) if continuous else rates,
                    continuous,
                    endpoint.group(1) if endpoint else "",
                )
            )
        fields.clear()

    for raw in lines:
        stripped = raw.strip()
        if raw.startswith(("Playback:", "Capture:")):
            flush()
            direction = "playback" if raw.startswith("Playback") else "capture"
            interface = altset = -1
            continue
        match = re.fullmatch(r"Interface (\d+)", stripped)
        if match:
            flush()
            interface = int(match.group(1))
            continue
        match = re.fullmatch(r"Altset (\d+)", stripped)
        if match:
            flush()
            altset = int(match.group(1))
            continue
        match = re.match(r"^(Format|Channels|Rates|Endpoint): (.*)$", stripped)
        if match and direction:
            fields[match.group(1)] = match.group(2)
    flush()
    return tuple(streams)


def parse_alsa(text: str) -> tuple[AlsaCard, ...]:
    """ALSA cards from the card id lines ("/proc/asound/card3/id:XLR", and "usbid:0fd9:007d" when the probe could
    read it) and the "== /proc/asound/cardN/streamM" blocks. Lines that are neither are ignored: the recorded
    capture mixes this text with PipeWire's, and a live probe emits the same shape."""
    ids: dict[int, str] = {}
    usb_ids: dict[int, str] = {}
    for line in text.splitlines():
        match = _ALSA_ID.match(line)
        if not match:
            continue
        number, kind, value = int(match.group(1)), match.group(2), match.group(3)
        if kind == "id":
            ids[number] = value
        elif _ALSA_USBID.match(value):
            usb_ids[number] = value.lower()
    names: dict[int, tuple[str, str]] = {}
    streams: dict[int, list[AlsaStream]] = {}
    for groups, lines in _ordered_blocks(text, _ALSA_STREAM_HEADER):
        card = int(groups[0])
        title = _ALSA_TITLE.match(next((ln for ln in lines if ln.strip()), ""))
        if title and card not in names:
            names[card] = (title.group(1), title.group(2))
        streams.setdefault(card, []).extend(_alsa_streams(lines))
    numbers = sorted(set(ids) | set(streams) | set(usb_ids))
    if text.strip() and not numbers:
        _guard(text)
        raise _unrecognised(text, "ALSA card list")
    return tuple(
        AlsaCard(
            number=n,
            id=ids.get(n, ""),
            usb_id=usb_ids.get(n),
            name=names.get(n, ("", ""))[0],
            usb_path=names.get(n, ("", ""))[1],
            streams=tuple(streams.get(n, ())),
        )
        for n in numbers
    )


# ---------------------------------------------------------------------------
# PipeWire
# ---------------------------------------------------------------------------

_PW_CARD = re.compile(r"^(\d+)\s+(\S+)\s+(\S+)\s*$")
_PW_NODE = re.compile(r"^(\d+)\s+(\S+)\s+(\S+)\s+(\w+) (\d+)ch (\d+)Hz(?:\s+(\w+))?\s*$")
_PW_SINK_NAME = re.compile(r"(^|[._-])(output|sink)([._-]|$)|^auto_null$")
_BOX = str.maketrans("│├└─", "    ")


def _node_kind(name: str, marker: str) -> str:
    if name.endswith(".monitor"):
        return "monitor"
    if marker:
        return marker
    return "sink" if _PW_SINK_NAME.search(name) else "source"


def _owner(node: str, cards: Iterable[str]) -> str | None:
    device = re.sub(r"^alsa_(output|input)\.", "", node)
    if device == node:
        return None
    for card in sorted(cards, key=len, reverse=True):
        base = card.removeprefix("alsa_card.")
        if device == base or device.startswith(base + "."):
            return card
    return None


def parse_pipewire_cards(text: str) -> list[tuple[int, str, str]]:
    """`pactl list short cards`: id, name, driver."""
    _guard(text)
    cards = [(int(m.group(1)), m.group(2), m.group(3)) for m in map(_PW_CARD.match, text.splitlines()) if m]
    if text.strip() and not cards:
        raise _unrecognised(text, "pactl list short cards output")
    return cards


def parse_pipewire_nodes(text: str) -> list[tuple[int, str, str, str, int, int, str]]:
    """`pactl list short sinks` and `sources`, optionally with "# sinks" and "# sources" marker lines:
    (id, name, kind, format, channels, rate, state)."""
    nodes = []
    marker = ""
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped in ("# sinks", "# sources"):
            marker = "sink" if stripped.endswith("sinks") else "source"
            continue
        match = _PW_NODE.match(line)
        if match:
            ident, name, _driver, fmt, channels, rate, state = match.groups()
            nodes.append((int(ident), name, _node_kind(name, marker), fmt, int(channels), int(rate), state or ""))
    return nodes


def parse_card_profiles(text: str) -> dict[str, tuple[str | None, str | None]]:
    """`pactl -f json list cards`: card name -> (active profile, "vid:pid" from the device properties)."""
    try:
        cards = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"pactl -f json list cards is not JSON: {exc.msg}") from exc
    if not isinstance(cards, list):
        raise ParseError("pactl -f json list cards is not a list")
    profiles: dict[str, tuple[str | None, str | None]] = {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("name"), str):
            continue
        props = card.get("properties") if isinstance(card.get("properties"), dict) else {}
        vendor, product = props.get("device.vendor.id"), props.get("device.product.id")
        usb = (
            f"{str(vendor).lower().removeprefix('0x')}:{str(product).lower().removeprefix('0x')}"
            if vendor and product
            else None
        )
        active = card.get("active_profile")
        profiles[card["name"]] = (active if isinstance(active, str) else None, usb)
    return profiles


def parse_wpctl_status(text: str) -> dict[str, tuple[int, str] | str]:
    """`wpctl status`: the default Audio sink and source ("*" marks, as (id, description)) and the configured
    defaults ("Default Configured Devices", as node names). Keys: sink, source, configured_sink, configured_source."""
    _guard(text)
    found: dict[str, tuple[int, str] | str] = {}
    top = sub = ""
    for raw in text.splitlines():
        if re.fullmatch(r"(Audio|Video|Settings)\s*", raw):
            top, sub = raw.strip(), ""
            continue
        line = raw.translate(_BOX)
        heading = re.match(r"^\s*(Devices|Sinks|Sources|Filters|Streams|Clients|Default Configured Devices):\s*$", line)
        if heading:
            sub = heading.group(1)
            continue
        if top == "Settings":
            match = re.match(r"^\s*\d+\.\s+Audio/(Sink|Source)\s+(\S+)\s*$", line)
            if match:
                found[f"configured_{match.group(1).lower()}"] = match.group(2)
            continue
        if top == "Audio" and sub in ("Sinks", "Sources"):
            match = re.match(r"^\s*(\*)?\s*(\d+)\.\s+(.+?)\s*(?:\[vol:[^\]]*\])?\s*$", line)
            if match and match.group(1):
                found[sub[:-1].lower()] = (int(match.group(2)), match.group(3))
    return found


def parse_pipewire(cards_text: str, nodes_text: str, status_text: str = "", profiles_text: str = "") -> PipewireState:
    """PipeWire's cards and nodes (pactl), the defaults (wpctl) and the active card profiles (pactl json).

    Only the first two are required. Without the profiles a card counts as `off` when none of its nodes exist, which
    is exactly how PipeWire shows a card whose profile is off.
    """
    cards = parse_pipewire_cards(cards_text)
    listed = parse_pipewire_nodes(nodes_text)
    if nodes_text.strip() and not listed and not any(ln.strip().startswith("#") for ln in nodes_text.splitlines()):
        _guard(nodes_text)
    card_names = [name for _, name, _ in cards]
    nodes = tuple(
        PipewireNode(ident, name, kind, fmt, channels, rate, _owner(name, card_names), state)
        for ident, name, kind, fmt, channels, rate, state in listed
    )
    owners = {n.card for n in nodes}
    profiles = parse_card_profiles(profiles_text) if profiles_text.strip() else {}
    status = parse_wpctl_status(status_text) if status_text.strip() else {}

    def default(kind: str, wanted: str) -> tuple[str | None, str]:
        entry = status.get(kind)
        if not isinstance(entry, tuple):
            return None, ""
        by_id = next((n for n in nodes if n.id == entry[0] and n.kind == wanted), None)
        return (by_id.name if by_id else None), entry[1]

    sink, sink_label = default("sink", "sink")
    source, source_label = default("source", "source")
    configured_sink, configured_source = status.get("configured_sink"), status.get("configured_source")
    return PipewireState(
        cards=tuple(
            PipewireCard(
                ident,
                name,
                driver,
                profiles.get(name, (None, None))[0],
                profiles.get(name, (None, None))[1],
                name in owners,
            )
            for ident, name, driver in cards
        ),
        nodes=nodes,
        default_sink=sink,
        default_source=source,
        default_sink_label=sink_label,
        default_source_label=source_label,
        configured_sink=configured_sink if isinstance(configured_sink, str) else None,
        configured_source=configured_source if isinstance(configured_source, str) else None,
    )


# ---------------------------------------------------------------------------
# V4L2 and v4l2loopback
# ---------------------------------------------------------------------------

_V4L_GROUP = re.compile(r"^(\S.*?)\s+\(([^()]+)\):\s*$")
_V4L_NODE = re.compile(r"^\s+(/dev/video\d+)\s*$")
_V4L_HEADER = re.compile(r"^==\s+(/dev/video\d+)\s*$")
_V4L_CAPS = re.compile(r"Device Caps\s*:\s*(0x[0-9a-fA-F]+)")
_V4L_USB = re.compile(r"^(/dev/video\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*$")


def parse_v4l2_devices(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """`v4l2-ctl --list-devices`: (card label, bus info, video nodes) per device. Media nodes are ignored."""
    _guard(text)
    groups: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        match = _V4L_GROUP.match(line)
        if match:
            groups.append((match.group(1), match.group(2), []))
            continue
        node = _V4L_NODE.match(line)
        if node and groups:
            groups[-1][2].append(node.group(1))
    if text.strip() and not groups:
        raise _unrecognised(text, "v4l2-ctl --list-devices output")
    return [(label, bus, tuple(nodes)) for label, bus, nodes in groups]


def parse_v4l2_info(text: str) -> dict[str, dict[str, str | int | bool | None]]:
    """The "== /dev/videoN" blocks of `v4l2-ctl --all`: driver, card and device caps per node; `denied` when the
    node could not be opened. An empty block (the recorded dm313red capture) yields an entry with nothing known."""
    info: dict[str, dict[str, str | int | bool | None]] = {}
    for groups, lines in _ordered_blocks(text, _V4L_HEADER):
        block = "\n".join(lines)
        entry: dict[str, str | int | bool | None] = {"driver": None, "card": None, "caps": None, "denied": False}
        driver = re.search(r"Driver name\s*:\s*(.+)", block)
        card = re.search(r"Card type\s*:\s*(.+)", block)
        caps = _V4L_CAPS.search(block)
        entry["driver"] = driver.group(1).strip() if driver else None
        entry["card"] = card.group(1).strip() if card else None
        entry["caps"] = int(caps.group(1), 16) if caps else None
        entry["denied"] = bool(re.search(r"Permission denied|Operation not permitted", block))
        info[groups[0]] = entry
    return info


def parse_v4l2_usb(text: str) -> dict[str, str]:
    """ "/dev/video0 0fd9:0067" lines: which USB id stands behind a video node (a live probe reads it from sysfs)."""
    return {m.group(1): m.group(2).lower() for m in map(_V4L_USB.match, text.splitlines()) if m}


_FMT_HEAD = re.compile(r"^\s*\[\d+\]:\s+'(\w{4})'")
_FMT_SIZE = re.compile(r"Size:\s+Discrete\s+(\d+)x(\d+)")
_FMT_FPS = re.compile(r"Interval:\s+Discrete\s+[\d.]+s\s+\(([\d.]+)\s+fps\)")


def parse_v4l2_formats(text: str) -> tuple[V4l2Format, ...]:
    """`v4l2-ctl --list-formats-ext`: every discrete (fourcc, size, rate), once each, in listing order."""
    _guard(text)
    found: dict[V4l2Format, None] = {}
    fourcc = ""
    size: tuple[int, int] | None = None
    for line in text.splitlines():
        head = _FMT_HEAD.match(line)
        if head:
            fourcc, size = head.group(1), None
            continue
        match = _FMT_SIZE.search(line)
        if match:
            size = (int(match.group(1)), int(match.group(2)))
            continue
        match = _FMT_FPS.search(line)
        if match and fourcc and size:
            found.setdefault(V4l2Format(fourcc, size[0], size[1], float(match.group(1))), None)
    if text.strip() and not found and "ioctl" not in text:
        raise _unrecognised(text, "v4l2-ctl --list-formats-ext output")
    return tuple(found)


def build_v4l2(
    devices: Iterable[tuple[str, str, tuple[str, ...]]],
    info: Mapping[str, Mapping[str, str | int | bool | None]] | None = None,
    usb: Mapping[str, str] | None = None,
    formats: Mapping[str, tuple[V4l2Format, ...] | SourceFailure] | None = None,
) -> tuple[V4l2Node, ...]:
    info, usb, formats = info or {}, usb or {}, formats or {}
    nodes = []
    for label, bus, paths in devices:
        for path in paths:
            known = info.get(path, {})
            listed = formats.get(path, ())
            caps = known.get("caps")
            nodes.append(
                V4l2Node(
                    path=path,
                    label=label,
                    bus_info=bus,
                    driver=str(known.get("driver") or ""),
                    usb_id=usb.get(path),
                    caps=caps if isinstance(caps, int) else None,
                    formats=() if isinstance(listed, SourceFailure) else listed,
                    formats_state=listed.state if isinstance(listed, SourceFailure) else "ok",
                )
            )
    return tuple(sorted(nodes, key=lambda n: int(re.sub(r"\D", "", n.path) or 0)))


def parse_loopback(text: str, nodes: Iterable[V4l2Node] = ()) -> LoopbackState:
    """v4l2loopback from `modinfo` ("version: 0.15.3"), `lsmod` ("v4l2loopback 65536 1") and the modprobe options
    line ("options v4l2loopback devices=2 video_nr=42,43 card_label=..."). The devices are the video nodes the
    kernel reports as loopback, plus the ones the options promise (video_nr paired with card_label)."""
    version = next((m.group(1) for m in map(re.compile(r"^version:\s+(\S+)").match, text.splitlines()) if m), "")
    loaded = any(re.match(r"^v4l2loopback\s+\d+\s+\d+", ln) for ln in text.splitlines())
    tokens: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^options\s+v4l2loopback\s+(.*)$", line.strip())
        if match:
            try:
                tokens.extend(shlex.split(match.group(1)))
            except ValueError as exc:
                raise ParseError(f"v4l2loopback options are not readable: {exc}") from exc
    options = tuple((t.partition("=")[0], t.partition("=")[2]) for t in tokens)
    option = {k: v for k, v in options}
    devices = {n.path: LoopbackDevice(n.path, n.label) for n in nodes if n.loopback}
    numbers = [n for n in option.get("video_nr", "").split(",") if n.isdigit()]
    labels = option.get("card_label", "").split(",") if option.get("card_label") else []
    for index, number in enumerate(numbers):
        path = f"/dev/video{int(number)}"
        devices.setdefault(path, LoopbackDevice(path, labels[index] if index < len(labels) else ""))
    if text.strip() and not (version or loaded or options or devices) and failure_of(text):
        _guard(text)
    return LoopbackState(loaded, version, options, tuple(sorted(devices.values(), key=lambda d: d.path)))


# ---------------------------------------------------------------------------
# ffmpeg and the machine
# ---------------------------------------------------------------------------

_FFMPEG_ENCODER = re.compile(r"^\s*V[.A-Z]{5}\s+(\S+)\s")


def parse_ffmpeg_encoders(text: str) -> FfmpegInfo:
    """`ffmpeg -hide_banner -encoders`: the video encoder names."""
    _guard(text)
    names = sorted({m.group(1) for m in map(_FFMPEG_ENCODER.match, text.splitlines()) if m and m.group(1) != "="})
    if text.strip() and not names:
        raise _unrecognised(text, "ffmpeg -encoders output")
    return FfmpegInfo(tuple(names))


_MACHINE_ID = re.compile(r"^[0-9a-fA-F]{8}(-?[0-9a-fA-F]{4}){3}-?[0-9a-fA-F]{12}$")


def machine_hash(raw: str) -> str:
    """A salted SHA-256 of the machine id (first 32 hex digits): enough to tell rigs apart, useless as an id."""
    return hashlib.sha256(b"streamstack-machine:" + raw.strip().lower().replace("-", "").encode()).hexdigest()[:32]


def parse_machine_id(text: str) -> str:
    """The hash of /etc/machine-id or the DMI product uuid. The id itself is never kept."""
    _guard(text)
    raw = text.strip()
    if not _MACHINE_ID.match(raw):
        raise ParseError("the machine id is not a 128-bit hex id")
    return machine_hash(raw)


def _ordered_blocks(text: str, header: re.Pattern[str]) -> list[tuple[tuple[str, ...], list[str]]]:
    """Blocks in file order: (header groups, the lines up to the next header). Repeated headers stay separate."""
    blocks: list[tuple[tuple[str, ...], list[str]]] = []
    for line in text.splitlines():
        match = header.match(line)
        if match:
            blocks.append((match.groups(), []))
        elif blocks:
            blocks[-1][1].append(line)
    return blocks
