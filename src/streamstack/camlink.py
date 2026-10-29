"""Elgato Cam Link: read the modes its HDMI input offers, pick the best one for the video engine.

A Cam Link has no modes of its own: it advertises whatever the HDMI source sends (a camera in 1080p59.94 offers
1080p59.94, a laptop in 4K offers 4K30, a switched-off camera offers nothing). So the best mode is chosen from
the live listing every time, never from a fixed table, and the card is found by its USB id so the same gear works
at every property whatever /dev/videoN it lands on.

Preference, highest first: the biggest picture the setup allows (1080p, or 4K with STREAMSTACK_CAMLINK_4K), then
the highest frame rate (1080p60 > 1080p50 > 1080p30), then the format that costs the least latency (raw YUYV,
UYVY or NV12 before MJPEG and H.264, which need a decoder).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from streamstack import config, system
from streamstack.system import Mode

CAP_HEIGHT = 1080
CAP_HEIGHT_4K = 2160
# Lower is better. Raw formats need no decoder; anything unlisted sits between raw and MJPEG.
FORMAT_RANK = {"YUYV": 0, "UYVY": 0, "NV12": 0, "YU12": 1, "MJPG": 3, "H264": 4}
DEFAULT_RANK = 2
USB3_MBPS = 5000  # sysfs reports 480 for USB 2 High Speed, 5000 and up for USB 3
PIN = re.compile(r"(\d+)x(\d+)(?:@([\d.]+))?(?:\s+(\w{4}))?")


class CamlinkError(Exception):
    """Something about the Cam Link the caller should report to the person, not a bug."""


def short_label(mode: Mode) -> str:
    fourcc, _, height, fps = mode
    return f"{height}p{fps:g} {fourcc}"


def parse_pin(text: str) -> tuple[int, int, float | None, str | None]:
    """STREAMSTACK_CAMLINK_MODE as (width, height, fps or None, fourcc or None); ValueError when malformed."""
    match = PIN.fullmatch(text.strip())
    if not match:
        raise ValueError(f"{text!r} is not WIDTHxHEIGHT[@FPS] [FOURCC] (or auto, or off)")
    width, height, fps, fourcc = match.groups()
    return int(width), int(height), float(fps) if fps else None, fourcc.upper() if fourcc else None


def rank(mode: Mode) -> tuple[int, int, int, float]:
    """Sort key, bigger is better: picture size, whole frame rate, lowest-latency format, exact frame rate."""
    fourcc, width, height, fps = mode
    return width * height, round(fps), -FORMAT_RANK.get(fourcc, DEFAULT_RANK), fps


def choose_mode(modes: list[Mode], *, allow_4k: bool = False, pin: str = "auto") -> Mode | None:
    """The best of the offered `modes`, or the pinned one when the source offers it.

    Without 4K the choice is capped at 1080 lines. A source that only offers more than the cap (a 4K laptop)
    still gets its smallest offered size, which the engine scales down, rather than nothing.
    """
    if not modes:
        return None
    wanted = pin.strip()
    if wanted.lower() not in ("", "auto", "off"):
        try:
            width, height, fps, fourcc = parse_pin(wanted)
        except ValueError:
            pass  # a typo in the settings file must not leave the studio without a picture; fall back to auto
        else:
            matches = [
                m
                for m in modes
                if (m[1], m[2]) == (width, height)
                and (fps is None or abs(m[3] - fps) < 0.02)
                and (fourcc is None or m[0] == fourcc)
            ]
            if matches:
                return max(matches, key=rank)
    cap = CAP_HEIGHT_4K if allow_4k else CAP_HEIGHT
    allowed = [m for m in modes if m[2] <= cap]
    if allowed:
        return max(allowed, key=rank)
    smallest = min(m[1] * m[2] for m in modes)
    return max((m for m in modes if m[1] * m[2] == smallest), key=rank)


@dataclass
class Probe:
    device: str = ""  # "" when no Cam Link is plugged in
    offered: list[Mode] = field(default_factory=list)
    chosen: Mode | None = None
    active: Mode | None = None  # what the driver is set to now (the engine's pick while it holds the card)
    audio: str = ""  # description of the HDMI audio source, "" when there is none
    note: str = ""
    usb_mbps: int | None = None  # the negotiated USB link; None when it could not be read

    @property
    def usb2(self) -> bool:
        """On a USB 2 link the card cannot carry uncompressed 1080p: frames arrive at about a third of the rate."""
        return self.usb_mbps is not None and self.usb_mbps < USB3_MBPS

    @property
    def present(self) -> bool:
        return bool(self.device)

    @property
    def signal(self) -> bool:
        return bool(self.offered)

    @property
    def labels(self) -> list[str]:
        return [short_label(m) for m in self.offered]

    def public(self) -> dict:
        """The snapshot the Console renders."""
        state, mode = "off", ""
        if self.signal and self.chosen:
            state, mode = "ok", short_label(self.chosen)
        active = ""
        if self.active:
            fourcc, width, height, fps = self.active
            active = f"{width}x{height} {fourcc}" + (f" @ {fps:g} fps" if fps else "")
        return {
            "present": self.present,
            "device": self.device,
            "signal": self.signal,
            "state": state,
            "mode": mode,
            "offered": self.labels,
            "active": active,
            "audio": self.audio,
            "note": self.note,
            "usb_mbps": self.usb_mbps,
            "usb2": self.usb2,
        }


def probe(cfg: dict[str, str] | None = None, sources: dict[str, str] | None = None) -> Probe:
    """Look at the Cam Link now. Read-only: v4l2 listings and the audio source list, nothing is opened for capture."""
    cfg = cfg or config.load_settings()
    device = system.find_camlink()
    if not device:
        return Probe()
    offered = system.camera_modes(device)
    note = ""
    pin = cfg["STREAMSTACK_CAMLINK_MODE"]
    if pin.strip().lower() not in ("", "auto", "off"):
        try:
            parse_pin(pin)
        except ValueError as exc:
            note = f"STREAMSTACK_CAMLINK_MODE ignored: {exc}"
    chosen = choose_mode(offered, allow_4k=config.truthy(cfg["STREAMSTACK_CAMLINK_4K"]), pin=pin)
    audio_name, audio_desc = system.capture_card_audio(sources if sources is not None else system.audio_sources())
    return Probe(
        device=device,
        offered=offered,
        chosen=chosen,
        active=system.current_mode(device),
        audio=audio_desc or audio_name,
        note=note,
        usb_mbps=system.usb_speed(device),
    )
