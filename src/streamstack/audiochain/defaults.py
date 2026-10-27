"""Make "StreamStack Mic" the PipeWire default source while the voice chain runs, and give the raw one back after.

Teams, Zoom and the browsers open their microphone as "Default", so a processed voice only reaches a call when the
chain's node is what the session manager hands out for that name. The default source is the metadata key
``default.configured.audio.source`` of PipeWire's ``default`` metadata object (what ``pactl set-default-source``
writes, and what WirePlumber follows and persists). StreamStack changes only that key:

* **claim** (the chain is up and reading an explicit microphone): set the key to the chain's node and note that we
  did, in ``<state>/audio-chain/default-source.json``, so a Console restart, a `streamstack chain stop` from another
  process or a crash all find it again.
* **release** (the chain is stopped on purpose): while the key still names the chain, point it at the chain's own
  capture target, the Plan's microphone, and nothing else. It is deliberately NOT "what the key held before the
  claim" (#142): that value was whatever happened to be default at the time (a camera's HDMI audio, a stale claim of
  ours, a node that has since gone), it outlives the run in a file, and restoring it put the Cam Link back as the
  default twice. When the microphone is not present (unplugged) or unknown, the key is deleted so the session manager
  picks a real source; a missing node is never left as the default. A key that names the chain is reset even when no
  claim record exists (the record is lost on a crash, and a stale chain default must not survive a stop). When the key
  names something else (the person chose another default meanwhile) it is left alone.
* **the chain dies without a release** (kill, crash, power): the key then names a node that does not exist, and
  WirePlumber falls back to the best real source by itself, so apps get the raw microphone back. That fallback is
  exercised on a private instance by `tools/bench_audio_chain.py default`.

A claim is made once per run: if the person later picks another default while the chain runs, StreamStack does not
take it back. The chain never claims while it has no explicit microphone (it would then read the default source,
which would be itself).

Everything goes through ``pw-metadata`` (so it follows PIPEWIRE_REMOTE like the rest of the chain) and an injectable
runner, so the logic is unit tested without a server.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import graph, runtime

log = logging.getLogger(__name__)

SOURCE_CLASS = "Audio/Source"

KEY = "default.configured.audio.source"
METADATA = "default"
_LINE = re.compile(r"key:'(?P<key>[^']*)' value:'(?P<value>.*)' type:")


def state_path(state_dir: Path) -> Path:
    return state_dir / "audio-chain" / "default-source.json"


def _name_of(value: str) -> str | None:
    """The node name inside a metadata value (`{"name":"x"}`, with any spacing), or None."""
    try:
        data = json.loads(value)
    except ValueError:
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) and name else None


def read_configured(run: runtime.Runner = runtime.default_runner) -> tuple[bool, str | None]:
    """(readable, name): the configured default source. `readable` is False when pw-metadata cannot be asked."""
    result = run(["pw-metadata", "-n", METADATA], 5)
    if result is None or result.returncode != 0:
        return False, None
    name: str | None = None
    for line in result.stdout.splitlines():
        if " id:0 " not in line:
            continue
        if line.startswith("remove:") and f"key:'{KEY}'" in line:
            name = None
            continue
        found = _LINE.search(line)
        if found and found["key"] == KEY:
            name = _name_of(found["value"])  # the last update wins
    return True, name


def _set(name: str, run: runtime.Runner) -> bool:
    value = json.dumps({"name": name}, separators=(",", ":"))
    result = run(["pw-metadata", "-n", METADATA, "0", KEY, value, "Spa:String:JSON"], 5)
    return result is not None and result.returncode == 0


def _delete(run: runtime.Runner) -> bool:
    result = run(["pw-metadata", "-n", METADATA, "-d", "0", KEY], 5)
    return result is not None and result.returncode == 0


def present_sources(run: runtime.Runner = runtime.default_runner) -> set[str] | None:
    """The names of the audio source nodes PipeWire has right now, or None when `pw-dump` cannot be read."""
    result = run(["pw-dump"], 10)
    if result is None or result.returncode != 0:
        return None
    try:
        objects = json.loads(result.stdout)
    except ValueError:
        return None
    names: set[str] = set()
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") == SOURCE_CLASS and isinstance(props.get("node.name"), str):
            names.add(props["node.name"])
    return names


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("claimed") else None


def _write_state(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"claimed": graph.NODE_NAME}))
        tmp.replace(path)
    except OSError as exc:
        log.warning("cannot record the previous default microphone: %s", exc)
        return False
    return True


def _forget(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("cannot remove %s: %s", path, exc)


def claim(state_dir: Path, run: runtime.Runner = runtime.default_runner) -> str:
    """Make StreamStack Mic the default source. Returns what happened:

    `claimed` (just done), `held` (done earlier and still in force), `user-changed` (the person picked something else
    since, left alone), `unavailable` (pw-metadata cannot be used) or `failed`.
    """
    path = state_path(state_dir)
    readable, current = read_configured(run)
    if not readable:
        return "unavailable"
    if _read_state(path) is not None:
        if current == graph.NODE_NAME:
            return "held"
        _forget(path)  # somebody else chose a default after our claim: do not fight over it, do not restore later
        return "user-changed"
    if not _write_state(path):
        return "failed"
    if not _set(graph.NODE_NAME, run):
        _forget(path)
        return "failed"
    return "claimed"


def release(state_dir: Path, microphone: str | None = None, run: runtime.Runner = runtime.default_runner) -> str:
    """Give the default source back to `microphone`, the chain's own capture target (the Plan's microphone).

    Returns `released`, `none` (nothing was claimed and the key does not name the chain), `user-changed` (a claim
    existed but the person chose another default since, left alone), `unavailable` (pw-metadata cannot be read; the
    claim is kept so the next try can still release) or `failed`.
    """
    path = state_path(state_dir)
    if microphone == graph.NODE_NAME:
        microphone = None  # the chain reading itself (no explicit microphone) has no raw microphone to hand back
    claimed_before = _read_state(path) is not None
    readable, current = read_configured(run)
    if not readable:
        return "unavailable" if claimed_before else "none"
    if current != graph.NODE_NAME:
        _forget(path)
        return "user-changed" if claimed_before else "none"
    present = present_sources(run) if microphone else None
    if microphone and present is not None and microphone in present:
        ok = _set(microphone, run)
    else:
        if microphone:
            log.warning("the default microphone is not set back to %s: it is not present", microphone)
        ok = _delete(run)  # the session manager picks a real source; never a node that is gone
    if not ok:
        return "failed"
    _forget(path)
    return "released"


def claimed(state_dir: Path) -> bool:
    return _read_state(state_path(state_dir)) is not None
