"""The Wave XLR capture-hold rule: is `node.always-process` effective on its capture node? (issue #126)

The Wave XLR shares one full-speed USB link between capture and playback. A reported ordering bug makes the microphone
return bit-exact zeros for the life of a capture stream that was opened after a playback stream on the card. The
prevention is a WirePlumber rule that keeps the capture node always processing, so it is always the one already
started: StreamStack ships `packaging/wireplumber/wireplumber.conf.d/52-streamstack-wave-xlr-capture-hold.conf`.

Whether the rule took effect is read from `pw-dump` (the node's `info.props`), never from the file: the effective
properties are what counts, and the workstation role may own a conflicting file. Nothing here edits any file.
"""

from __future__ import annotations

import json
from typing import Any

PROP = "node.always-process"
NODE_MARK = "Elgato_Systems_Elgato_Wave_XLR"  # in the ALSA node name of the 0fd9:007d
FIX = (
    "install StreamStack's drop-in (`sudo make install`, which places "
    "52-streamstack-wave-xlr-capture-hold.conf in /etc/wireplumber/wireplumber.conf.d), then "
    "`systemctl --user restart wireplumber` (that briefly drops every audio stream, so not during a call)"
)


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"))


def always_process(pw_dump: str, node_name: str) -> bool | None:
    """Whether the named node carries `node.always-process = true`; None when pw-dump is unreadable or has no such
    node."""
    try:
        objects: Any = json.loads(pw_dump)
    except ValueError:
        return None
    if not isinstance(objects, list):
        return None
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if isinstance(props, dict) and props.get("node.name") == node_name:
            return _truthy(props.get(PROP))
    return None


def is_wave_xlr(node_name: str | None) -> bool:
    return bool(node_name) and NODE_MARK in (node_name or "")


def check_row(microphone: str | None, effective: bool | None) -> dict[str, str] | None:
    """The doctor row (shape of `cli.check`); None when the microphone is not a Wave XLR or nothing can be judged."""
    if not is_wave_xlr(microphone) or effective is None:
        return None
    if effective:
        return {
            "id": "wave-xlr-capture-hold",
            "group": "audio",
            "state": "ok",
            "title": "Wave XLR capture node is always processing (capture-hold rule active)",
            "detail": "",
            "hint": "",
        }
    return {
        "id": "wave-xlr-capture-hold",
        "group": "audio",
        "state": "warn",
        "title": "Wave XLR capture node is not held always-processing",
        "detail": "a playback stream opened before the capture can leave the microphone returning only zeros",
        "hint": FIX,
    }
