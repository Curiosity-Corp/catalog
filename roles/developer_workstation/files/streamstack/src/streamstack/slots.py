"""Time-of-day look profiles: what the picture, lights and voice should be in each part of the day.

The Director learns per-slot baselines (`director.learning`) but needs the network and a Claude backend, and the lights
have their own daylight schedule (`lightplan`). This module is the deterministic, editable, offline layer in between:
one profile per slot of the day, each naming the pieces the person wants in that slot. The Console's `SlotsController`
(`console/slots.py`) applies them; this module holds the vocabulary, the validation and the file.

Slots are the Director's sun slots (`director.daylight`, computed offline from the property's location) with one
addition: **midday**, the two hours either side of solar noon while the sun is high, carved out of morning and
afternoon because that is when window light is hardest and a profile most often wants a different look:

    night      sun below -6 degrees
    dawn       sun rising, below 8 degrees
    morning    sun rising, 8 degrees or higher, until two hours before solar noon
    midday     two hours either side of solar noon (sun 8 degrees or higher)
    afternoon  sun setting, 8 degrees or higher, from two hours after solar noon
    evening    sun setting, below 8 degrees, until it is 6 degrees under the horizon

A profile holds any of (a missing part means "leave it alone"):

    look      a name from the looks library (built-in or saved)
    canon     {"exposure_comp": <the body's own choice, e.g. "+1/3">, "kelvin": 2500..10000}  (when a Canon is present)
    scene     a scene name
    blur      {"on": true|false, "strength": 1..}
    voice     a voice preset name
    lighting  a lighting preset name

The file is `slot-profiles.json` beside the other settings (mode 600, atomic, damaged copies quarantined by
`streamstack.store`). `export_document` / `import_document` carry it as `streamstack.slot-profiles/1`, which is
also what a profile bundle for a fleet embeds.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from streamstack import config, store
from streamstack.director import daylight

DOCUMENT = "streamstack.slot-profiles/1"
SCHEMA = 1

SLOTS = ("night", "dawn", "morning", "midday", "afternoon", "evening")
LABELS = {
    "night": "Night",
    "dawn": "Dawn",
    "morning": "Morning",
    "midday": "Midday",
    "afternoon": "Afternoon",
    "evening": "Evening",
}
MIDDAY_HOUR_ANGLE = 30.0  # degrees of the sun's hour angle: 15 degrees an hour, so two hours either side of noon
MODES = ("off", "suggest", "auto")
MODE_NOTES = {
    "off": "Profiles are only applied when you press Apply.",
    "suggest": "StreamStack tells you the profile for the new slot is ready; you decide.",
    "auto": "The profile is applied when a call starts and when the slot changes while no call is running.",
}
PARTS = ("look", "canon", "scene", "blur", "voice", "lighting")
PART_LABELS = {
    "look": "Look",
    "canon": "Canon",
    "scene": "Scene",
    "blur": "Blur",
    "voice": "Voice",
    "lighting": "Lights",
}
# Parts the AI Director drives while it runs. With the Director on, these seed its baselines instead of being written.
DIRECTOR_PARTS = ("look", "canon", "voice", "lighting")
FADE_LIMITS = (0.0, 30.0)
DEFAULT_FADE = 4.0
MAX_NAME = 80
KELVIN_LIMITS = (2500, 10000)
MAX_STRENGTH = 100

# What `starter` fills into empty slots: picks from the shipped looks and built-in lighting presets, a sensible
# starting point to edit. The dark slots get the key light, the bright ones the soft fill.
STARTER: dict[str, dict[str, Any]] = {
    "night": {"look": "Warm Studio", "lighting": "Key light"},
    "dawn": {"look": "Natural", "lighting": "Key light"},
    "morning": {"look": "Natural", "lighting": "Soft"},
    "midday": {"look": "Clean Corporate", "lighting": "Soft"},
    "afternoon": {"look": "Natural", "lighting": "Soft"},
    "evening": {"look": "Warm Studio", "lighting": "Key light"},
}


class SlotsError(ValueError):
    """A slot, mode or profile that does not follow the schema; the message says which part."""


# ---------------------------------------------------------------------------
# Which slot is it
# ---------------------------------------------------------------------------


def slot_for(elevation: float, hour_angle: float) -> str:
    """The profile slot for the sun's elevation and hour angle (negative before solar noon)."""
    base = daylight.slot_for(elevation, hour_angle)
    if base in ("morning", "afternoon") and abs(hour_angle) <= MIDDAY_HOUR_ANGLE:
        return "midday"
    return base


def director_slot(slot: str, hour_angle: float) -> str:
    """The Director's slot for a profile slot: midday is morning before solar noon and afternoon after it."""
    if slot == "midday":
        return "morning" if hour_angle < 0 else "afternoon"
    return slot


def slot_at(when: datetime, loc: daylight.Location) -> tuple[str, float]:
    """(slot, hour angle) at an aware moment for the property."""
    elevation, _, hour_angle = daylight.solar_position(when, loc.latitude, loc.longitude)
    return slot_for(elevation, hour_angle), hour_angle


def strip(when: datetime, loc: daylight.Location, step_minutes: int = 10) -> list[dict[str, Any]]:
    """The local day as consecutive slot segments (minutes from midnight), for the Console's time-of-day strip."""
    start = datetime(when.year, when.month, when.day, tzinfo=when.tzinfo)
    segments: list[dict[str, Any]] = []
    for step in range(0, 1440, step_minutes):
        moment = start + timedelta(minutes=step)
        slot, _ = slot_at(moment, loc)
        if segments and segments[-1]["slot"] == slot:
            segments[-1]["end"] = step + step_minutes
        else:
            segments.append({"slot": slot, "start": step, "end": step + step_minutes})
    if segments:
        segments[-1]["end"] = min(segments[-1]["end"], 1440)
    return segments


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _name(value: object, part: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SlotsError(f"{part} must be a name")
    text = value.strip()
    if len(text) > MAX_NAME or any(ord(c) < 32 for c in text):
        raise SlotsError(f"{part} must be at most {MAX_NAME} characters, without control characters")
    return text


def _canon(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SlotsError("canon must be an object with exposure_comp and/or kelvin")
    unknown = set(value) - {"exposure_comp", "kelvin"}
    if unknown:
        raise SlotsError(f"canon has no setting {sorted(unknown)[0]!r}")
    clean: dict[str, Any] = {}
    if value.get("exposure_comp") is not None:
        clean["exposure_comp"] = _name(value["exposure_comp"], "canon.exposure_comp")
    if value.get("kelvin") is not None:
        kelvin = value["kelvin"]
        if isinstance(kelvin, bool) or not isinstance(kelvin, (int, float)) or not math.isfinite(kelvin):
            raise SlotsError("canon.kelvin must be a number")
        low, high = KELVIN_LIMITS
        if not low <= kelvin <= high:
            raise SlotsError(f"canon.kelvin must be between {low} and {high}")
        clean["kelvin"] = int(round(kelvin / 100.0) * 100)
    if not clean:
        raise SlotsError("canon needs exposure_comp or kelvin")
    return clean


def _blur(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SlotsError('blur must be an object like {"on": true}')
    unknown = set(value) - {"on", "strength"}
    if unknown:
        raise SlotsError(f"blur has no setting {sorted(unknown)[0]!r}")
    if not isinstance(value.get("on"), bool):
        raise SlotsError("blur.on must be true or false")
    clean: dict[str, Any] = {"on": value["on"]}
    strength = value.get("strength")
    if strength is not None:
        if isinstance(strength, bool) or not isinstance(strength, int) or not 1 <= strength <= MAX_STRENGTH:
            raise SlotsError(f"blur.strength must be a whole number from 1 to {MAX_STRENGTH}")
        clean["strength"] = strength
    return clean


def clean_profile(raw: object) -> dict[str, Any]:
    """A validated profile: only known parts, each well-formed. An empty object is a profile that does nothing."""
    if not isinstance(raw, Mapping):
        raise SlotsError("a profile must be an object")
    unknown = set(raw) - set(PARTS)
    if unknown:
        raise SlotsError(f"a profile has no part {sorted(unknown)[0]!r}")
    clean: dict[str, Any] = {}
    for part in ("look", "scene", "voice", "lighting"):
        if raw.get(part) is not None:
            clean[part] = _name(raw[part], part)
    if raw.get("canon") is not None:
        clean["canon"] = _canon(raw["canon"])
    if raw.get("blur") is not None:
        clean["blur"] = _blur(raw["blur"])
    return {part: clean[part] for part in PARTS if part in clean}


def check_slot(slot: object) -> str:
    if not isinstance(slot, str) or slot not in SLOTS:
        raise SlotsError(f"slot must be one of {', '.join(SLOTS)}")
    return slot


def check_mode(mode: object) -> str:
    if not isinstance(mode, str) or mode not in MODES:
        raise SlotsError(f"mode must be one of {', '.join(MODES)}")
    return mode


def check_fade(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SlotsError("fade_seconds must be a number")
    low, high = FADE_LIMITS
    if not low <= value <= high:
        raise SlotsError(f"fade_seconds must be between {low:g} and {high:g}")
    return round(float(value), 1)


def defaults() -> dict[str, Any]:
    return {"mode": "off", "fade_seconds": DEFAULT_FADE, "profiles": {}}


def normalise(data: object) -> dict[str, Any]:
    """The stored document as settings: anything damaged or unknown falls back to its default, never raises."""
    out = defaults()
    if not isinstance(data, Mapping):
        return out
    for key, check in (("mode", check_mode), ("fade_seconds", check_fade)):
        try:
            out[key] = check(data.get(key, out[key]))
        except SlotsError:
            pass
    profiles = data.get("profiles")
    for slot, raw in profiles.items() if isinstance(profiles, Mapping) else ():
        try:
            out["profiles"][check_slot(slot)] = clean_profile(raw)
        except SlotsError:
            continue  # a hand-edited bad entry is skipped, the rest still work
    return out


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def default_path() -> Path:
    return config.user_env_file().parent / "slot-profiles.json"


class Store:
    """The settings and the six profiles, one JSON file. Every edit is validated, atomic and serialised."""

    def __init__(self, path: Path | None = None, *, ephemeral: bool = False) -> None:
        """`ephemeral`: still atomic, but no fsync and no backup (the demo rig's profiles need no disk flush)."""
        self._path = path
        self.ephemeral = ephemeral
        self.lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path or default_path()

    def load(self) -> dict[str, Any]:
        with self.lock:
            data = store.read_json(self.path, {}, validate=store.is_object)
        return normalise(data)

    def _write(self, data: dict[str, Any]) -> None:
        document = {"schema": SCHEMA, **data}
        store.write_json(self.path, document, mode=0o600, backup=not self.ephemeral, durable=not self.ephemeral)

    def _edit(self, change: Any) -> dict[str, Any]:
        with self.lock:
            data = self.load()
            change(data)
            self._write(data)
            return data

    def set_mode(self, mode: object) -> dict[str, Any]:
        value = check_mode(mode)
        return self._edit(lambda data: data.__setitem__("mode", value))

    def set_fade(self, seconds: object) -> dict[str, Any]:
        value = check_fade(seconds)
        return self._edit(lambda data: data.__setitem__("fade_seconds", value))

    def set_profile(self, slot: object, profile: object) -> dict[str, Any]:
        name, clean = check_slot(slot), clean_profile(profile)

        def change(data: dict[str, Any]) -> None:
            if clean:
                data["profiles"][name] = clean
            else:
                data["profiles"].pop(name, None)

        return self._edit(change)

    def clear_profile(self, slot: object) -> dict[str, Any]:
        name = check_slot(slot)
        return self._edit(lambda data: data["profiles"].pop(name, None))

    def fill_starter(self) -> tuple[dict[str, Any], list[str]]:
        """Put the starter picks into the slots that have no profile yet; returns (settings, the slots filled)."""
        filled: list[str] = []

        def change(data: dict[str, Any]) -> None:
            for slot in SLOTS:
                if not data["profiles"].get(slot):
                    data["profiles"][slot] = clean_profile(STARTER[slot])
                    filled.append(slot)

        return self._edit(change), filled

    def reset(self) -> dict[str, Any]:
        return self._edit(lambda data: data.update(defaults()))

    # -- the profile bundle -----------------------------------------------------------------------------

    def export_document(self) -> dict[str, Any]:
        data = self.load()
        return {"document": DOCUMENT, "fade_seconds": data["fade_seconds"], "profiles": data["profiles"]}

    def import_document(self, document: object) -> dict[str, Any]:
        """Replace the profiles (not the mode: the person decides that on this machine) from an exported document."""
        if not isinstance(document, Mapping) or document.get("document") != DOCUMENT:
            raise SlotsError(f"not a slot profiles document (expected {DOCUMENT})")
        profiles = document.get("profiles")
        if not isinstance(profiles, Mapping):
            raise SlotsError("profiles must be an object keyed by slot")
        cleaned = {check_slot(slot): clean_profile(raw) for slot, raw in profiles.items()}
        fade = check_fade(document["fade_seconds"]) if "fade_seconds" in document else None

        def change(data: dict[str, Any]) -> None:
            data["profiles"] = {slot: profile for slot, profile in cleaned.items() if profile}
            if fade is not None:
                data["fade_seconds"] = fade

        return self._edit(change)
