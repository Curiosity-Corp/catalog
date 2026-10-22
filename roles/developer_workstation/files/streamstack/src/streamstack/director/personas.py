"""Personas: a chosen target look and sound for the AI director.

A persona is a small JSON document (schema 1) describing what the picture and the voice should feel like: mood
words for Claude to judge against, a target look for the OBS grade, exposure and colour bands, a voice preset and
tone targets. Five ship with StreamStack in `data/personas/`; anyone can edit one or add their own from the Auto
page. An edit is saved to `~/.config/streamstack/personas/<id>.json`, which wins over the shipped file of the same
id; deleting that file brings the shipped persona back. A broken file is reported and skipped, never fatal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import config
from streamstack.director import settings
from streamstack.director.limits import LIMITS

SCHEMA = 1
ID = re.compile(r"[a-z][a-z0-9-]{1,31}")
HEX = re.compile(r"#[0-9a-fA-F]{6}")
LOOK_KEYS = tuple(LIMITS)
MOOD_MAX = 8
TARGET_KEYS = ("depth", "clarity", "sibilance")


class PersonaError(ValueError):
    """A persona document that does not follow the schema; the message names the field."""


def _number(value: object, path: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonaError(f"{path} must be a number")
    if value != value or not low <= value <= high:  # NaN fails both
        raise PersonaError(f"{path} must be between {low:g} and {high:g}")
    return float(value)


def _range(value: object, path: str, low: float, high: float) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PersonaError(f"{path} must be a [low, high] pair")
    a, b = _number(value[0], f"{path}[0]", low, high), _number(value[1], f"{path}[1]", low, high)
    if a > b:
        raise PersonaError(f"{path}: low must not exceed high")
    return [a, b]


def _text(value: object, path: str, longest: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise PersonaError(f"{path} must be text")
    if len(value) > longest:
        raise PersonaError(f"{path} must be at most {longest} characters")
    if any(ord(c) < 32 for c in value):
        raise PersonaError(f"{path} must not contain control characters")
    return value.strip()


def _words(value: object, path: str, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value) or len(value) > MOOD_MAX:
        raise PersonaError(f"{path} must be a list of 1 to {MOOD_MAX} short phrases")
    return [_text(v, f"{path}[{i}]", 40) for i, v in enumerate(value)]


def _object(value: object, path: str, required: bool = True) -> dict:
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise PersonaError(f"{path} must be an object")
    return value


def validate(raw: object) -> dict:
    """The normalised persona, or PersonaError. Unknown keys are dropped so a file cannot smuggle prompt text."""
    doc = _object(raw, "persona")
    if doc.get("schema", SCHEMA) != SCHEMA:
        raise PersonaError(f"schema must be {SCHEMA}")
    persona_id = doc.get("id")
    if not isinstance(persona_id, str) or not ID.fullmatch(persona_id):
        raise PersonaError("id must be 2-32 lowercase letters, digits or dashes, starting with a letter")
    picture = _object(doc.get("picture"), "picture")
    look_in = _object(picture.get("look"), "picture.look", required=False)
    look = {}
    for key, value in look_in.items():
        if key not in LOOK_KEYS:
            raise PersonaError(f"picture.look.{key} is not a look setting ({', '.join(LOOK_KEYS)})")
        look[key] = round(_number(value, f"picture.look.{key}", *LIMITS[key]), 3)
    voice = _object(doc.get("voice"), "voice")
    targets_in = _object(voice.get("targets"), "voice.targets", required=False)
    targets = {}
    for key, value in targets_in.items():
        if key not in TARGET_KEYS:
            raise PersonaError(f"voice.targets.{key} is not a tone target ({', '.join(TARGET_KEYS)})")
        targets[key] = _range(value, f"voice.targets.{key}", 0.0, 1.0)
    out = {
        "schema": SCHEMA,
        "id": persona_id,
        "name": _text(doc.get("name"), "name", 40),
        "tagline": _text(doc.get("tagline"), "tagline", 60),
        "description": _text(doc.get("description"), "description", 400, required=False),
        "mood": _words(doc.get("mood"), "mood", True),
        "avoid": _words(doc.get("avoid"), "avoid", False),
        "swatch": _swatch(doc.get("swatch")),
        "picture": {
            "look": look,
            "face_luma": _range(picture.get("face_luma", [110, 150]), "picture.face_luma", 60, 200),
            "frame_luma": _range(picture.get("frame_luma", [100, 140]), "picture.frame_luma", 60, 200),
            "warmth_k": _number(picture.get("warmth_k", 5600), "picture.warmth_k", 2500, 8000),
            "saturation": _range(picture.get("saturation", [0.15, 0.35]), "picture.saturation", 0, 1),
            "contrast_spread": _range(picture.get("contrast_spread", [110, 200]), "picture.contrast_spread", 20, 255),
            "max_clipped": _number(picture.get("max_clipped", 0.02), "picture.max_clipped", 0, 0.2),
        },
        "voice": {
            "preset": _text(voice.get("preset"), "voice.preset", 80),
            "speech_peak_db": _range(voice.get("speech_peak_db", [-24, -12]), "voice.speech_peak_db", -40, 0),
            "lufs": _range(voice.get("lufs", [-30, -16]), "voice.lufs", -50, -5),
            "targets": targets,
            "min_snr_db": _number(voice.get("min_snr_db", 30), "voice.min_snr_db", 10, 80),
            "max_rt60_s": _number(voice.get("max_rt60_s", 0.6), "voice.max_rt60_s", 0.1, 2.0),
        },
    }
    light = _object(doc.get("light"), "light", required=False)
    if light:
        out["light"] = {
            "brightness": _number(light.get("brightness"), "light.brightness", 0, 1),
            "kelvin": _number(light.get("kelvin"), "light.kelvin", 2700, 6500),
        }
    return out


def _swatch(value: object) -> list[str]:
    if value is None:
        return ["#3a3f4b", "#8a93a6"]
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, str) and HEX.fullmatch(v) for v in value)
    ):
        raise PersonaError("swatch must be two #rrggbb colours")
    return list(value)


def shipped_dir() -> Path:
    return config.data_dir() / "personas"


def user_dir() -> Path:
    return settings.config_dir() / "personas"


@dataclass
class Library:
    personas: dict[str, dict] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def get(self, persona_id: str | None) -> dict:
        """The persona by id, falling back to the default and then to any shipped one."""
        for candidate in (persona_id, settings.DEFAULT_PERSONA):
            if candidate in self.personas:
                return self.personas[candidate]
        if self.personas:
            return next(iter(self.personas.values()))
        raise PersonaError("no personas are installed; reinstall StreamStack")

    def listing(self) -> list[dict]:
        return list(self.personas.values())


def _read(path: Path) -> dict:
    try:
        return validate(json.loads(path.read_text()))
    except OSError as exc:
        raise PersonaError(f"cannot read {path.name}: {exc.strerror}") from exc
    except ValueError as exc:
        if isinstance(exc, PersonaError):
            raise PersonaError(f"{path.name}: {exc}") from exc
        raise PersonaError(f"{path.name} is not valid JSON") from exc


def load_library(shipped: Path | None = None, user: Path | None = None, include_user: bool = True) -> Library:
    """Shipped personas overlaid by the user's own; each carries `source` (shipped, edited or custom)."""
    library = Library()
    folders = [(shipped or shipped_dir(), "shipped")]
    if include_user:
        folders.append((user or user_dir(), "user"))
    for folder, label in folders:
        for path in sorted(folder.glob("*.json")):
            try:
                persona = _read(path)
            except PersonaError as exc:
                library.problems.append(str(exc))
                continue
            if path.stem != persona["id"]:
                library.problems.append(f"{path.name}: the file name must match the id {persona['id']!r}")
                continue
            persona["source"] = (
                "shipped" if label == "shipped" else "edited" if persona["id"] in library.personas else "custom"
            )
            library.personas[persona["id"]] = persona
    return library


def save_user(raw: object, user: Path | None = None) -> dict:
    """Validate and store a persona the person edited or created. Raises PersonaError or SettingsError."""
    persona = validate(raw)
    settings.write_json((user or user_dir()) / f"{persona['id']}.json", persona, mode=0o644)
    return persona


def remove_user(persona_id: str, user: Path | None = None) -> bool:
    """Delete the user's file for `persona_id` (restores a shipped persona, removes a custom one)."""
    if not ID.fullmatch(persona_id):
        raise PersonaError("not a persona id")
    path = (user or user_dir()) / f"{persona_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise settings.SettingsError(f"cannot remove {path}: {exc.strerror}") from exc
    return True
