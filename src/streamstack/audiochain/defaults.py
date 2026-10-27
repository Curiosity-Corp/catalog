"""Make "StreamStack Mic" the PipeWire default source while the voice chain runs, and give the raw one back after.

Teams, Zoom and the browsers open their microphone as "Default", so a processed voice only reaches a call when the
chain's node is what the session manager hands out for that name. The default source is the metadata key
``default.configured.audio.source`` of PipeWire's ``default`` metadata object (what ``pactl set-default-source``
writes, and what WirePlumber follows and persists). StreamStack changes only that key:

* **claim** (the chain is up and reading an explicit microphone): remember what the key held, then set it to the
  chain's node. The previous value goes to ``<state>/audio-chain/default-source.json`` so a Console restart, a
  `streamstack chain stop` from another process or a crash all find it again.
* **release** (the chain is stopped on purpose): put the previous value back, or delete the key when there was none so
  the session manager picks the best microphone again. When the key no longer names the chain (the person chose
  another default meanwhile) it is left alone.
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


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("claimed") else None


def _write_state(path: Path, previous: str | None) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"claimed": graph.NODE_NAME, "previous": previous}))
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
    previous = None if current == graph.NODE_NAME else current  # a stale claim of ours is not a microphone to restore
    if not _write_state(path, previous):
        return "failed"
    if not _set(graph.NODE_NAME, run):
        _forget(path)
        return "failed"
    return "claimed"


def release(state_dir: Path, run: runtime.Runner = runtime.default_runner) -> str:
    """Give the default source back. Returns `released`, `none` (nothing was claimed), `user-changed`, `unavailable`
    (the claim is kept so the next try can still restore it) or `failed`."""
    path = state_path(state_dir)
    state = _read_state(path)
    if state is None:
        return "none"
    readable, current = read_configured(run)
    if not readable:
        return "unavailable"
    if current != graph.NODE_NAME:
        _forget(path)
        return "user-changed"
    previous = state.get("previous")
    ok = _set(previous, run) if isinstance(previous, str) and previous else _delete(run)
    if not ok:
        return "failed"
    _forget(path)
    return "released"


def claimed(state_dir: Path) -> bool:
    return _read_state(state_path(state_dir)) is not None
