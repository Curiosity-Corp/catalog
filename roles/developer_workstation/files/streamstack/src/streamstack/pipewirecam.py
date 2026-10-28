"""Is StreamStack Camera also a PipeWire camera? The portal path for Firefox and Chromium (#88).

ADR 0004 keeps v4l2loopback as the one call path, and WirePlumber's v4l2 monitor already publishes every v4l2 node the
user can open as a PipeWire `Video/Source`. That second path is what the camera portal offers Firefox, Chromium with
`WebRTCPipeWireCamera`, and what `pw-v4l2` gives legacy apps. We therefore do not write a producer of our own: this
module reads the PipeWire graph (`pw-dump`, read-only) and tells the doctor whether that second path is really there.
A node nothing feeds is not published (`exclusive_caps=1`), so the row speaks only when the program node is fed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from streamstack import system

Probe = Callable[[str], bool | None]


@dataclass(frozen=True)
class CameraNode:
    id: int
    name: str
    nick: str
    path: str  # `v4l2:/dev/videoN`


def video_sources(dump: str) -> list[CameraNode] | None:
    """Every PipeWire Video/Source in a `pw-dump` JSON document; None when the text is not a PipeWire dump."""
    try:
        objects = json.loads(dump)
    except json.JSONDecodeError:
        return None
    if not isinstance(objects, list):
        return None
    found = []
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {} if isinstance(obj, dict) else {}
        if props.get("media.class") == "Video/Source":
            found.append(
                CameraNode(
                    int(obj.get("id", -1)),
                    str(props.get("node.name", "")),
                    str(props.get("node.nick") or props.get("node.description", "")),
                    str(props.get("object.path", "")),
                )
            )
    return found


def read_sources(run: Callable[..., object] = system.run) -> list[CameraNode] | None:
    """The Video/Source nodes now, or None when pw-dump is missing or PipeWire is not answering."""
    done = run(["pw-dump"], timeout=10)
    if done is None or getattr(done, "returncode", 1) != 0:
        return None
    return video_sources(str(getattr(done, "stdout", "")))


def _row(state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": "video-pipewire", "group": "video", "state": state, "title": title, "detail": detail, "hint": hint}


def checks(
    cfg: dict[str, str],
    *,
    sources: Callable[[], list[CameraNode] | None] = read_sources,
    probe: Probe = system.loopback_has_producer,
    exists: Callable[[str], bool] = os.path.exists,
) -> list[dict[str, str]]:
    """The `video-pipewire` row, only when the program node is fed (otherwise `video-chain` already says why not)."""
    program, label = cfg["STREAMSTACK_PROGRAM_DEVICE"], cfg["STREAMSTACK_PROGRAM_LABEL"]
    if not exists(program) or probe(program) is not True:
        return []
    nodes = sources()
    if nodes is None:
        return []  # no pw-dump or no PipeWire session: nothing can be said, and nothing is wrong with the camera
    if any(n.path == f"v4l2:{program}" for n in nodes):
        return [
            _row(
                "ok",
                f"{label} is also a PipeWire camera",
                "Firefox and the camera portal list it; Chromium does with chrome://flags/#enable-webrtc-pipewire-camera",
            )
        ]
    others = ", ".join(sorted({n.nick or n.name for n in nodes})) or "none"
    return [
        _row(
            "warn",
            f"{label} is fed but PipeWire does not publish it as a camera",
            f"PipeWire lists these cameras: {others}. Teams in Edge, Zoom and Chrome read {program} directly and are "
            "unaffected; Firefox and the portal path will not list it",
            "systemctl --user restart wireplumber (and check no WirePlumber config disables the v4l2 monitor)",
        )
    ]
