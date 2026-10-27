"""Which cameras the engine can read, and the mode each one is read in.

A camera is found the way the rest of StreamStack finds it: the Canon through the loopback node canonctl feeds
(STREAMSTACK_CAMERA_DEVICE), the Cam Link by its USB id (never by /dev/videoN, the number moves between boots).
The mode is what the driver delivers: a fed loopback has exactly one, a capture card offers several and
camlink.choose_mode picks the best (raw before MJPEG, 1080p60 over 1080p30, pinned by STREAMSTACK_CAMLINK_MODE).
Only formats the engine reads are considered (graph.RAW_FORMATS and MJPG).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from streamstack import camlink, config, system
from streamstack.engine.graph import RAW_FORMATS, Source
from streamstack.hardware import camera as hwcamera
from streamstack.hardware.plan import Plan

log = logging.getLogger(__name__)

READABLE = (*RAW_FORMATS, "MJPG")
CAMLINK_LABEL = "Cam Link 4K"


class SourceError(Exception):
    """A camera cannot be read (absent, no signal, or only in formats the engine does not read)."""


@dataclass(frozen=True)
class Camera:
    label: str
    device: str
    kind: str  # canon | camlink


def cameras(
    cfg: dict[str, str],
    find_camlink: Callable[[], str] = system.find_camlink,
    exists: Callable[[str], bool] = os.path.exists,
    plan: Plan | None = None,
) -> list[Camera]:
    """The cameras present now, the Canon first. The Canon is listed even when its node is absent (it comes back)."""
    found = [
        Camera(
            cfg.get("STREAMSTACK_CAMERA_LABEL") or config.DEFAULTS["STREAMSTACK_CAMERA_LABEL"],
            hwcamera.device_for("camera", cfg, plan),
            "canon",
        )
    ]
    device = find_camlink()
    if device and exists(device):
        found.append(Camera(CAMLINK_LABEL, device, "camlink"))
    return found


def pick_mode(
    camera: Camera, cfg: dict[str, str], offered: list[system.Mode], current: system.Mode | None
) -> system.Mode:
    """The mode to read `camera` in. Raises SourceError when it has none the engine reads."""
    readable = [m for m in offered if m[0] in READABLE]
    if camera.kind == "canon" and current is not None and current[0] in READABLE and current[1] > 0:
        return current  # a fed loopback delivers one format; ask for exactly that
    if camera.kind == "canon" and not readable and current is not None:
        raise SourceError(f"{camera.label} delivers {current[0]}, which the engine does not read.")
    choice = camlink.choose_mode(
        readable,
        allow_4k=config.truthy(cfg.get("STREAMSTACK_CAMLINK_4K", "false")),
        pin=cfg.get("STREAMSTACK_CAMLINK_MODE", "auto") if camera.kind == "camlink" else "auto",
    )
    if choice is None:
        if offered:
            formats = ", ".join(sorted({m[0] for m in offered}))
            raise SourceError(
                f"{camera.label} only offers {formats}; the engine reads YUYV, NV12, YU12, UYVY and MJPG."
            )
        raise SourceError(f"{camera.label} offers no picture (no signal, or the device is busy).")
    return choice


def resolve(
    camera: Camera,
    cfg: dict[str, str],
    *,
    list_modes: Callable[[str], list[system.Mode]] = system.camera_modes,
    current: Callable[[str], system.Mode | None] = system.current_mode,
) -> Source:
    """The `Source` for `camera`, from what the driver says now."""
    if not camera.device:
        raise SourceError(f"{camera.label} is not connected.")
    offered = list_modes(camera.device)
    fourcc, width, height, fps = pick_mode(camera, cfg, offered, current(camera.device))
    if fps <= 0:
        fps = float(cfg.get("STREAMSTACK_FPS") or config.DEFAULTS["STREAMSTACK_FPS"])
    return Source(camera.label, camera.device, fourcc, width, height, fps)


def output_size(cfg: dict[str, str], source: Source) -> tuple[int, int]:
    """The program camera's size: the source's own, or STREAMSTACK_ENGINE_SIZE (for example 1920x1080)."""
    wanted = (cfg.get("STREAMSTACK_ENGINE_SIZE") or "source").strip().lower()
    if wanted in ("", "source", "auto"):
        return source.width, source.height
    try:
        w, _, h = wanted.partition("x")
        width, height = int(w), int(h)
    except ValueError:
        log.warning("STREAMSTACK_ENGINE_SIZE=%r is not WIDTHxHEIGHT; using the source size", wanted)
        return source.width, source.height
    if width < 160 or height < 90 or width % 2 or height % 2 or width > 7680 or height > 4320:
        log.warning("STREAMSTACK_ENGINE_SIZE=%r is out of range or odd; using the source size", wanted)
        return source.width, source.height
    return width, height
