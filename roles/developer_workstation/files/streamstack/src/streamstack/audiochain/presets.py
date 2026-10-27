"""Voice presets: named sets of stage parameters.

A preset file is ``{"name", "blurb", "engine", "strict_engine"?, "params"}`` where ``params`` only lists the values
that differ from the chain defaults (`stages.defaults()`), so a new stage or a changed default reaches every preset.
The six shipped presets live in the package data (``data/audio/presets``); the user's own (including the ones the
one-time Easy Effects migration imported) live in ``<state>/audio-chain/presets``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from . import stages

log = logging.getLogger(__name__)

DEFAULT_PRESET = "StreamStack Voice"
LABEL_PREFIX = "StreamStack "
NAME_OK = re.compile(r"^[^\x00-\x1f/\\]{1,80}$")


class PresetError(ValueError):
    """A preset that cannot be read, written or found."""


@dataclass(frozen=True)
class Preset:
    name: str
    blurb: str = ""
    engine: str = "deepfilternet"
    strict_engine: bool = False  # the preset is only meaningful with its engine: do not fall back to another
    params: dict[str, float | bool] = field(default_factory=dict)
    user: bool = False

    @property
    def label(self) -> str:
        return self.name.removeprefix(LABEL_PREFIX) or self.name

    def resolved(self) -> dict[str, float | bool]:
        """Every parameter: the chain defaults with this preset's changes on top."""
        return stages.merged({}, self.params)


def shipped_dir() -> Path:
    return config.data_dir() / "audio" / "presets"


def user_dir() -> Path:
    return config.state_dir() / "audio-chain" / "presets"


def parse(doc: Any, *, user: bool, source: str = "preset") -> Preset:
    """A Preset from a decoded JSON document; PresetError says what is wrong."""
    if not isinstance(doc, dict):
        raise PresetError(f"{source}: not an object")
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_OK.match(name):
        raise PresetError(f"{source}: bad name")
    engine = doc.get("engine", "deepfilternet")
    if engine not in stages.DENOISE_ENGINES:
        raise PresetError(f"{source}: unknown denoise engine {engine!r}")
    raw = doc.get("params", {})
    if not isinstance(raw, dict):
        raise PresetError(f"{source}: params is not an object")
    try:
        params = stages.validate(raw)
    except ValueError as exc:
        raise PresetError(f"{source}: {exc}") from exc
    blurb = doc.get("blurb", "")
    return Preset(
        name, blurb if isinstance(blurb, str) else "", engine, bool(doc.get("strict_engine", False)), params, user
    )


def _read_dir(folder: Path, user: bool) -> list[Preset]:
    found: list[Preset] = []
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return found
    for path in files:
        try:
            found.append(parse(json.loads(path.read_text()), user=user, source=path.name))
        except (OSError, ValueError) as exc:  # a broken file must not hide the others
            log.warning("skipping voice preset %s: %s", path, exc)
    return found


def load_all(shipped: Path | None = None, user: Path | None = None) -> dict[str, Preset]:
    """Every preset by name, the plain default first, then the shipped ones, then the user's; a user file with a
    shipped name overrides it."""
    out: dict[str, Preset] = {}
    for preset in _read_dir(shipped or shipped_dir(), False) + _read_dir(user or user_dir(), True):
        out[preset.name] = preset
    ordered = sorted(out.values(), key=lambda p: (p.name != DEFAULT_PRESET, p.user, p.name))
    return {p.name: p for p in ordered}


def save_user(preset: Preset, folder: Path | None = None) -> Path:
    """Write a user preset atomically."""
    target_dir = folder or user_dir()
    doc: dict[str, Any] = {"name": preset.name, "blurb": preset.blurb, "engine": preset.engine, "params": preset.params}
    if preset.strict_engine:
        doc["strict_engine"] = True
    path = target_dir / (re.sub(r"[^A-Za-z0-9 ._-]", "_", preset.name) + ".json")
    tmp = path.with_suffix(".json.tmp")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(path)
    except OSError as exc:
        raise PresetError(f"cannot write {path}: {exc}") from exc
    return path


def delta(params: dict[str, float | bool]) -> dict[str, float | bool]:
    """The values of `params` that differ from the defaults (what a preset file stores)."""
    base = stages.defaults()
    return {k: v for k, v in params.items() if k in base and base[k] != v}
