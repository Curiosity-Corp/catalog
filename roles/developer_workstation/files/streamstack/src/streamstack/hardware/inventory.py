"""Replay a recorded inventory (`tests/fixtures/hardware/<host>/inventory.txt`) through the probe's parsers.

An inventory is plain text in sections ("##### lsusb", "##### va-per-node", ...), each holding what one tool printed
on the recorded host. `snapshot_from_inventory` feeds those sections to the same parsers the live probe uses, so a
host that was recorded once can be resolved in CI, in the demo rig and in the Console without its hardware. The
recordings carry no addresses, serials or MACs (see tools/record_hardware_fixture.py).
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from . import parsers
from .model import Snapshot, SourceStatus
from .parsers import ProbeError, SourceFailure

HEADER = re.compile(r"^#####\s+(\S+)\s*$")


def sections(text: str) -> dict[str, str]:
    """The inventory's sections by name. A repeated name keeps the first."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        match = HEADER.match(line)
        if match:
            current = found.setdefault(match.group(1), []) if match.group(1) not in found else None
        elif current is not None:
            current.append(line)
    return {name: "\n".join(lines).strip("\n") for name, lines in found.items()}


# The sections `snapshot_from_inventory` reads. The packaged recordings keep only these, so the wheel carries no
# unrelated host detail (paths, installed tools, the CPU).
CONSUMED = (
    "lsusb",
    "lspci-gpu",
    "drm",
    "va-per-node",
    "vulkan",
    "asound-streams",
    "audio-detail",
    "pactl-cards",
    "wpctl",
    "v4l2",
    "camlink-formats",
    "v4l2loopback",
    "loopback",
    "ffmpeg-encoders",
)


def trim(text: str) -> str:
    """The inventory reduced to the sections the replay reads, in the recording's order."""
    parts = sections(text)
    return "".join(f"##### {name}\n{body}\n\n" for name, body in parts.items() if name in CONSUMED)


def recorded_hosts(root: Path) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/inventory.txt"))


def recordings_dir() -> Path:
    """The recordings shipped with the package: what the demo rig replays."""
    from streamstack.config import data_dir

    return data_dir() / "hardware" / "recordings"


def packaged_hosts() -> list[str]:
    return sorted(p.stem for p in recordings_dir().glob("*.txt"))


def packaged_snapshot(host: str) -> Snapshot:
    """The snapshot of one packaged recording. Raises KeyError for a host that was not recorded."""
    path = recordings_dir() / f"{host}.txt"
    if host not in packaged_hosts():
        raise KeyError(host)
    return snapshot_from_inventory(path.read_text(encoding="utf-8"))


def snapshot_from_inventory(text: str) -> Snapshot:
    """The snapshot a recorded host would have produced. Missing sections are `absent`, tool failures keep their
    state (a recorded "Permission denied" is `denied`), exactly as on a live probe."""
    parts = sections(text)
    statuses: list[SourceStatus] = []

    def run(source: str, build, default, *needs: str):
        missing = [n for n in needs if n not in parts]
        if missing:
            statuses.append(SourceStatus(source, "absent", f"the recording has no {missing[0]} section"))
            return default
        try:
            value = build()
        except ProbeError as exc:
            statuses.append(SourceStatus(source, exc.state, str(exc)))
            return default
        statuses.append(SourceStatus(source))
        return value

    usb = run("usb", lambda: parsers.parse_lsusb(parts["lsusb"]), (), "lsusb")
    pci = run("pci", lambda: parsers.parse_lspci(parts["lspci-gpu"]), (), "lspci-gpu")
    nodes = run("drm", lambda: parsers.parse_drm(parts["drm"], pci), (), "drm")
    va = run("vaapi", lambda: parsers.parse_vainfo(parts["va-per-node"]), {}, "va-per-node")
    nodes = tuple(_with_va(n, va) for n in nodes)
    vulkan = run("vulkan", lambda: parsers.parse_vulkaninfo(parts["vulkan"]), (), "vulkan")
    alsa = run(
        "alsa",
        lambda: parsers.parse_alsa(parts["asound-streams"] + "\n" + parts.get("audio-detail", "")),
        (),
        "asound-streams",
    )
    pipewire = run(
        "pipewire",
        lambda: parsers.parse_pipewire(parts["pactl-cards"], parts.get("audio-detail", ""), parts.get("wpctl", "")),
        None,
        "pactl-cards",
    )
    v4l2 = run("v4l2", lambda: _v4l2(parts), (), "v4l2")
    loopback = run(
        "loopback",
        lambda: parsers.parse_loopback(parts.get("v4l2loopback", "") + "\n" + parts["loopback"], v4l2),
        None,
        "loopback",
    )
    ffmpeg = run("ffmpeg", lambda: parsers.parse_ffmpeg_encoders(parts["ffmpeg-encoders"]), None, "ffmpeg-encoders")
    kwargs = {}
    if pipewire is not None:
        kwargs["pipewire"] = pipewire
    if loopback is not None:
        kwargs["loopback"] = loopback
    statuses.append(SourceStatus("machine", "absent", "recordings never carry the machine id"))
    return Snapshot(
        sources=tuple(sorted(statuses, key=lambda s: SOURCE_ORDER.index(s.source))),
        usb=usb,
        pci=pci,
        render_nodes=nodes,
        vulkan=vulkan,
        alsa=alsa,
        v4l2=v4l2,
        ffmpeg=ffmpeg,
        **kwargs,
    )


SOURCE_ORDER = ("usb", "pci", "drm", "vaapi", "vulkan", "alsa", "pipewire", "v4l2", "loopback", "ffmpeg", "machine")


def _with_va(node, va):
    info = va.get(node.path)
    return node if info is None else replace(node, va=info)


def _v4l2(parts: dict[str, str]):
    """The v4l2 section is `v4l2-ctl --list-devices` plus one "== /dev/videoN" block of `--all` per node; the
    "camlink-formats" section is `--list-formats-ext` of the capture card's capture node. A recording in which no
    node could be opened ("Permission denied") makes the whole source `denied`: nothing about the cameras was read."""
    listing = parts["v4l2"]
    head = re.split(r"^== ", listing, maxsplit=1, flags=re.M)[0]
    devices = parsers.parse_v4l2_devices(head) if head.strip() else []
    info = parsers.parse_v4l2_info(listing)
    nodes = parsers.build_v4l2(devices, info)
    capture = next((n.path for n in nodes if n.kind == "capture" and not n.loopback), "/dev/video0")
    text = parts.get("camlink-formats", "")
    try:
        formats = parsers.parse_v4l2_formats(text) if text.strip() else ()
        failure = None
    except SourceFailure as exc:
        formats, failure = (), exc
    if failure is not None and not nodes:
        raise SourceFailure(failure.state, f"{capture}: {failure}")
    return tuple(
        replace(n, formats=formats, formats_state="ok" if failure is None else failure.state)
        if n.path == capture
        else n
        for n in nodes
    )
