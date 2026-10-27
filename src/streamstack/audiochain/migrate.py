"""One-time import of the voice presets the Console used to keep in Easy Effects.

Easy Effects is no longer a dependency, but a rig that ran it keeps its own presets in
``~/.local/share/easyeffects/input``. On the first Console start after the upgrade this reads those files (read only;
Easy Effects' files are never modified or deleted) and turns each preset whose name the chain does not already ship
into a StreamStack user preset: the three equalizer bands we mapped (Lo-shelf 120 Hz is bass, Bell 3.5 kHz is
presence, Hi-shelf 9 kHz is treble) and the denoise engine carry over, every other stage starts at its default. The
voice preset the Console had chosen (``console-live.json``) becomes the chosen preset. A marker file makes it run once.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .. import config
from . import presets, stages

log = logging.getLogger(__name__)

MANAGED_PRESET = "StreamStack Live"  # the preset the old Console rewrote on every tone change: never a user's own
BAND_INDEX = {"bass_db": 0, "presence_db": 2, "treble_db": 3}
ENGINES = ("rnnoise", "deepfilternet")


def easyeffects_input_dir() -> Path:
    return Path.home() / ".local/share/easyeffects/input"


def marker() -> Path:
    return config.state_dir() / "audio-chain" / "migrated-from-easyeffects"


def _section(doc: dict[str, Any]) -> dict[str, Any]:
    section = doc.get("input")
    return section if isinstance(section, dict) else {}


def read_eq(doc: dict[str, Any]) -> dict[str, float]:
    """The bass, presence and treble gains of an Easy Effects preset (empty when it has no equalizer)."""
    for key, value in _section(doc).items():
        if key.startswith("equalizer#") and isinstance(value, dict):
            left = value.get("left")
            if not isinstance(left, dict):
                return {}
            out: dict[str, float] = {}
            for param, band in BAND_INDEX.items():
                entry = left.get(f"band{band}")
                gain = entry.get("gain") if isinstance(entry, dict) else None
                if isinstance(gain, (int, float)) and not isinstance(gain, bool):
                    out[param] = float(gain)
            return out
    return {}


def read_engine(doc: dict[str, Any]) -> tuple[str, float] | None:
    """(engine, attenuation limit dB) of the noise removal plugin in the preset, if any."""
    for key, value in _section(doc).items():
        engine = key.split("#")[0]
        if engine in ENGINES and isinstance(value, dict):
            atten = value.get("attenuation-limit", 100.0)
            ok = isinstance(atten, (int, float)) and not isinstance(atten, bool)
            return engine, float(atten) if ok else 100.0
    return None


def convert(name: str, doc: dict[str, Any]) -> presets.Preset:
    """A StreamStack preset from an Easy Effects preset document."""
    params: dict[str, float | bool] = {}
    for key, value in read_eq(doc).items():
        param = stages.PARAM_BY_KEY[key]
        params[key] = round(max(param.low, min(param.high, value)), 2)
    engine = "deepfilternet"
    found = read_engine(doc)
    if found:
        engine = found[0]
        if engine == "deepfilternet":
            params["denoise_atten_db"] = max(0.0, min(100.0, found[1]))
    return presets.Preset(
        name, "imported from Easy Effects", engine, False, presets.delta(stages.merged({}, params)), True
    )


def migrate(source: Path | None = None, library: dict[str, presets.Preset] | None = None) -> list[str]:
    """Import once; returns the names imported (empty when already done or nothing to do)."""
    done = marker()
    if done.exists():
        return []
    folder = source or easyeffects_input_dir()
    known = library if library is not None else presets.load_all()
    imported: list[str] = []
    try:
        files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    except OSError as exc:
        log.warning("cannot read the Easy Effects presets in %s: %s", folder, exc)
        return []
    for path in files:
        name = path.stem
        if name == MANAGED_PRESET or name in known:
            continue
        try:
            doc = json.loads(path.read_text())
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            presets.save_user(convert(name, doc))
        except (OSError, ValueError, presets.PresetError) as exc:
            log.warning("not importing the Easy Effects preset %s: %s", path.name, exc)
            continue
        imported.append(name)
    try:
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text(json.dumps({"imported": imported}))
    except OSError as exc:
        log.warning("cannot write the migration marker %s: %s", done, exc)
    if imported:
        log.info("imported %d Easy Effects voice preset(s): %s", len(imported), ", ".join(imported))
    return imported


def previous_choice() -> str | None:
    """The voice preset the old Console had chosen (its ``console-live.json`` sidecar), if it is known."""
    try:
        data = json.loads((config.state_dir() / "console-live.json").read_text())
    except (OSError, ValueError):
        return None
    base = data.get("base") if isinstance(data, dict) else None
    return base if isinstance(base, str) else None
