"""Control the voice chain from another process (the Auto director, the tray, `streamstack chain`).

The Console owns the chain while it runs, and keeps its settings in ``<state>/audio-chain/state.json``. These helpers
change that file the same way the Console does and push the new values to the live graph with `pw-cli`; a running
Console notices the changed file within a second and follows. Nothing here starts or stops the chain host.
"""

from __future__ import annotations

from pathlib import Path

from .. import config
from . import chain, graph, presets, runtime


def store_path() -> Path:
    return config.state_dir() / "audio-chain" / "state.json"


def model() -> chain.ChainModel:
    return chain.ChainModel(store=store_path())


def preset_names() -> list[str]:
    return list(presets.load_all())


def current_preset() -> str | None:
    """The chosen voice preset, or None when the chain has never been configured on this machine."""
    if not store_path().exists():
        return None
    return model().preset


def apply_live(m: chain.ChainModel, run: runtime.Runner = runtime.default_runner) -> bool:
    """Push the model's controls to the running graph; False when the chain is not running."""
    nodes = runtime.find_nodes(run)
    if nodes is None or not nodes.present or nodes.control_id is None:
        return False
    return runtime.set_controls(nodes.control_id, m.controls(), run)


def load_preset(name: str, run: runtime.Runner = runtime.default_runner) -> bool:
    """Choose a preset: saved for the Console and applied live when the chain is running. False when unknown."""
    m = model()
    try:
        m.load_preset(name)
    except chain.ChainError:
        return False
    m.flush()
    apply_live(m, run)
    return True


def set_bypass(on: bool, run: runtime.Runner = runtime.default_runner) -> None:
    m = model()
    m.set_bypass(on)
    m.flush()
    apply_live(m, run)


def bypassed() -> bool:
    return model().bypass


def voice_source_present(run: runtime.Runner = runtime.default_runner) -> bool:
    nodes = runtime.find_nodes(run)
    return bool(nodes and nodes.present)


SOURCE = graph.NODE_NAME
