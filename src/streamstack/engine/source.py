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
CONTRACT_SIZE = (1280, 720)  # the least a call app is offered: "calls want 720p or 1080p" (doctor, virtualdevices)
CONTRACT_FPS = 30.0
CONTRACT_MIN_FPS = 24.0  # hardware/contract.py FPS[0]: below this calls stutter


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


def _explicit_size(cfg: dict[str, str]) -> tuple[int, int] | None:
    """STREAMSTACK_ENGINE_SIZE as WIDTHxHEIGHT; None for "source", "auto", empty or an unusable value."""
    wanted = (cfg.get("STREAMSTACK_ENGINE_SIZE") or "source").strip().lower()
    if wanted in ("", "source", "auto"):
        return None
    try:
        w, _, h = wanted.partition("x")
        width, height = int(w), int(h)
    except ValueError:
        log.warning("STREAMSTACK_ENGINE_SIZE=%r is not WIDTHxHEIGHT; using the camera's size", wanted)
        return None
    if width < 160 or height < 90 or width % 2 or height % 2 or width > 7680 or height > 4320:
        log.warning("STREAMSTACK_ENGINE_SIZE=%r is out of range or odd; using the camera's size", wanted)
        return None
    return width, height


@dataclass(frozen=True)
class Output:
    """What the program node is written at: its size, its frame rate (0 = the camera's own) and whether a camera
    smaller than the node is centred on black instead of stretched."""

    width: int
    height: int
    fps: float = 0.0
    letterbox: bool = False


def output_plan(cfg: dict[str, str], source: Source) -> Output:
    """The program node's format for `source`, which holds the call-app contract whatever the camera delivers.

    Call apps read the one format the node offers (ADR 0004, docs/loopback-contract.md). A card or a low-resolution
    fallback (the Cam Link at 720x480 on a host whose HDMI source is 480p, 21.8 fps) must therefore not renegotiate
    the node down: below CONTRACT_SIZE the node stays at CONTRACT_SIZE and the picture is scaled to fit and centred
    on black, and below CONTRACT_MIN_FPS the frames are repeated up to CONTRACT_FPS. A camera at or above both is
    passed through at its own size and rate. STREAMSTACK_ENGINE_SIZE is the operator's explicit choice and wins for
    the size (the doctor still checks it); the frame rate floor always applies.
    """
    fps = 0.0 if source.fps >= CONTRACT_MIN_FPS else CONTRACT_FPS
    explicit = _explicit_size(cfg)
    if explicit is not None:
        return Output(*explicit, fps=fps)
    if source.width >= CONTRACT_SIZE[0] and source.height >= CONTRACT_SIZE[1]:
        return Output(source.width, source.height, fps=fps)
    return Output(*CONTRACT_SIZE, fps=fps, letterbox=True)


def output_size(cfg: dict[str, str], source: Source) -> tuple[int, int]:
    """The program camera's size (see `output_plan`)."""
    plan = output_plan(cfg, source)
    return plan.width, plan.height
