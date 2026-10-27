"""Is the whole camera chain delivering? The doctor's view of "camera node -> engine -> StreamStack Camera".

Each node on its own can look fine to `streamstack doctor`: the program node "has no picture yet" is a warning, the
camera bridge not running is another warning. What the person loses is one thing: **Teams cannot list StreamStack
Camera** (with `exclusive_caps=1` an unfed node is no camera, ADR 0004). This module says that sentence and names the
link in the chain that broke. It is read-only: `v4l2-ctl --info` on the nodes and nothing else. Work package C of the
"Camera delivery to call apps" milestone; the Console's live version is `console/delivery.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from streamstack import config, system, virtualdevices

Probe = Callable[[str], bool | None]


def _row(state: str, cid: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": cid, "group": "video", "state": state, "title": title, "detail": detail, "hint": hint}


def restart_hint(cfg: dict[str, str]) -> str:
    if config.camera_external(cfg):
        return "restart the tool that feeds the camera node (systemctl --user restart canonctl-ptp-webcam.service)"
    return "systemctl --user restart streamstack-canon.service, and check the camera's cable and power"


def chain_checks(
    cfg: dict[str, str],
    *,
    probe: Probe = system.loopback_has_producer,
    exists: Callable[[str], bool] = os.path.exists,
    can_open: Callable[[str], bool] = virtualdevices.can_open,
) -> list[dict[str, str]]:
    """The `video-chain` row: one verdict for "will Teams list StreamStack Camera, and with what picture"."""
    camera, program = cfg["STREAMSTACK_CAMERA_DEVICE"], cfg["STREAMSTACK_PROGRAM_DEVICE"]
    label = cfg["STREAMSTACK_PROGRAM_LABEL"]
    if not exists(program) or not can_open(program):
        return []  # the node checks already say it is absent or denied
    program_fed = probe(program)
    if program_fed is None:
        return []  # no v4l2-ctl: nothing can be said
    camera_fed = probe(camera) if exists(camera) and can_open(camera) else None
    source = cfg["STREAMSTACK_CAMERA_LABEL"]
    if not program_fed:
        why = (
            f"{source} ({camera}) has no producer, so the engine has nothing to read"
            if camera_fed is False
            else "the video engine is not running"
        )
        return [
            _row(
                "fail",
                "video-chain",
                f"{label} is not listed by Teams, Zoom or Chrome: nothing feeds {program}",
                why,
                restart_hint(cfg) if camera_fed is False else "start the Console (streamstack console)",
            )
        ]
    if camera_fed is False:
        return [
            _row(
                "warn",
                "video-chain",
                f"{label} is fed, but {source} has no producer",
                f"{program} shows the 'camera reconnecting' card or the Cam Link until {source} returns",
                restart_hint(cfg),
            )
        ]
    return [_row("ok", "video-chain", f"{label} is fed and its source is delivering")]


def idle_checks(video: dict | None) -> list[dict[str, str]]:
    """The `engine-idle` row: is the engine resting because no app reads StreamStack Camera (and who would wake it)?

    `video` is the Console's video block (None when no Console answers: nothing can be said)."""
    engine = (video or {}).get("engine") or {}
    idle = engine.get("idle")
    if not isinstance(idle, dict):
        return []
    mode, state = idle.get("mode"), idle.get("state")
    if mode == "off":
        return [_row("ok", "engine-idle", "The engine never idles (STREAMSTACK_ENGINE_IDLE=off)")]
    if state == "idle":
        return [
            _row(
                "ok",
                "engine-idle",
                "The engine is idle: no app is reading StreamStack Camera",
                f"{idle.get('reason') or ''} It starts when an app opens the camera "
                f"(woken {idle.get('wakes', 0)} times this session).".strip(),
            )
        ]
    if state == "waking":
        return [_row("ok", "engine-idle", "The engine is waking: an app just opened StreamStack Camera")]
    readers = idle.get("readers") or []
    why = (
        "Keep awake is on"
        if idle.get("keep_alive")
        else f"read by {', '.join(str(r) for r in readers)}"
        if readers
        else f"it idles after {idle.get('after_s', 10)} s without a reader"
    )
    return [_row("ok", "engine-idle", "The engine is running", why)]
