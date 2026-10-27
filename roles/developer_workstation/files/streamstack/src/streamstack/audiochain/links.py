"""Which source is the voice chain's capture side really linked to? (issue #139)

The chain's config names the microphone with `target.object`, but WirePlumber only honours it when that node exists at
the moment the stream is linked. Without `node.dont-fallback` it links the stream to the *default* source instead, and
on 2026-10-27 that was the Cam Link 4K's HDMI audio: StreamStack Mic carried exact zeros while every check was green.
The graph now waits for its microphone (`graph.build_config`); this module reads `pw-dump` so the supervisor and
`streamstack doctor` can see the links that actually exist and never trust the config alone. Pure functions, no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import graph


@dataclass(frozen=True)
class CaptureLinks:
    """The chain's capture node and the source node names it is linked from (active, paused, init or negotiating)."""

    present: bool  # the capture node exists in the graph
    sources: tuple[str, ...] = ()  # node names of the producers linked to it
    available: frozenset[str] = field(default_factory=frozenset)  # every Audio/Source node name in the graph

    def linked_to(self, name: str) -> bool:
        return name in self.sources

    def wrong(self, microphone: str) -> tuple[str, ...]:
        """The linked sources that are not the wanted microphone."""
        return tuple(s for s in self.sources if s != microphone)


def parse(objects: Any) -> CaptureLinks:
    """The capture side's links from a parsed `pw-dump` (a list of objects); anything else has no capture node."""
    nodes: dict[int, str] = {}
    capture_id: int | None = None
    available: set[str] = set()
    if not isinstance(objects, list):
        return CaptureLinks(False)
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict) or not isinstance(obj.get("id"), int):
            continue
        name = props.get("node.name")
        if not isinstance(name, str):
            continue
        nodes[obj["id"]] = name
        if name == graph.CAPTURE_NAME:
            capture_id = obj["id"]
        elif props.get("media.class") == "Audio/Source" and name != graph.NODE_NAME:
            available.add(name)
    if capture_id is None:
        return CaptureLinks(False, available=frozenset(available))
    sources: list[str] = []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Link":
            continue
        info = obj.get("info")
        if not isinstance(info, dict) or info.get("input-node-id") != capture_id:
            continue
        producer = info.get("output-node-id")
        name = nodes.get(producer) if isinstance(producer, int) else None
        if name is not None and name not in sources:
            sources.append(name)
    return CaptureLinks(True, tuple(sources), frozenset(available))


def parse_text(pw_dump: str) -> CaptureLinks | None:
    """`parse` for the text of `pw-dump`; None when it is not JSON."""
    try:
        return parse(json.loads(pw_dump))
    except ValueError:
        return None


def check_row(microphone: str | None, links: CaptureLinks | None, voice_source_present: bool) -> dict[str, str] | None:
    """The doctor row (shape of `cli.check`) for the chain's input: fail when StreamStack Mic reads anything but the
    Plan's microphone. None when there is nothing to judge (no Plan microphone, no chain, no readable pw-dump)."""
    if not microphone or links is None or not links.present or not voice_source_present:
        return None
    row = {"id": "voice-chain-source", "group": "audio"}
    if links.wrong(microphone):
        return {
            **row,
            "state": "fail",
            "title": "StreamStack Mic is reading the wrong microphone",
            "detail": f"its capture is linked to {', '.join(links.wrong(microphone))}, not {microphone}",
            "hint": "streamstack chain stop && streamstack chain start (the Console also relinks it by itself)",
        }
    if links.linked_to(microphone):
        return {**row, "state": "ok", "title": "StreamStack Mic reads the Plan's microphone", "detail": "", "hint": ""}
    return {
        **row,
        "state": "fail",
        "title": "StreamStack Mic is not linked to any microphone",
        "detail": (
            f"{microphone} is " + ("present" if microphone in links.available else "not present") + " but nothing "
            "feeds the chain, so apps hear silence"
        ),
        "hint": "check that the microphone shows as an input (a USB interface can come up output-only after boot)",
    }
