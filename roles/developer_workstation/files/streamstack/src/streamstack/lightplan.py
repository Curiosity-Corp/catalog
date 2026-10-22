"""Lighting automation settings and the time-of-day schedule.

Two things live here, with no hardware and no Console in sight so both the service (`streamstack lights run`) and the
Console page can use them:

* the settings file, `~/.config/streamstack/lighting-automation.json` (mode 600): camera follow, the schedule, and a
  name, role and clamps for each light. Every read is forgiving (a damaged or hand-edited file falls back to the
  defaults field by field and never stops the service); every write goes through `validate` and is atomic.
* the schedule: how bright and how warm the lights should be at a given moment. The default follows the sun at the
  property (sunrise and sunset computed offline by `director.daylight`, the same five slots the AI Director uses);
  the alternative is a plain 24 hour table.

The defaults for the five slots are the Director's own light priors (`daylight.LIGHT_PRIORS`) applied to a neutral
key light, so the schedule and the Director start from the same idea of what each time of day needs.
"""

from __future__ import annotations

import copy
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from streamstack import config, lighting
from streamstack.director import daylight, settings

FILE = "lighting-automation.json"
VERSION = 1

ROLES = ("key", "fill", "manual")
ROLE_NOTES = {
    "key": "Follows the schedule at full level",
    "fill": "Follows the schedule at the fill level, a softer light beside the key",
    "manual": "Never touched by camera follow or the schedule",
}
MODES = ("sun", "clock")
MODE_NOTES = {
    "sun": "Follows the sun at your property: five slots from night to evening, blended smoothly",
    "clock": "A plain 24 hour table: one level per hour of the clock",
}
SLOT_ORDER = daylight.SLOTS  # night, dawn, morning, afternoon, evening
FOLLOW_SOURCES = ("apps", "virtualcam", "stream", "record")
SOURCE_LABELS = {
    "apps": "An app uses the camera (Teams, Meet, Zoom, any call or recording app)",
    "virtualcam": "OBS virtual camera is running",
    "stream": "OBS is streaming",
    "record": "OBS is recording",
}

BASE_BRIGHTNESS = 50.0  # percent: the neutral key light the slot priors are applied to
BASE_KELVIN = 4800

# (minimum, maximum, step) of each numeric setting; the validator clamps to these, the page reads them from state.
LIMITS: dict[str, tuple[float, float, float]] = {
    "grace_seconds": (0, 3600, 5),
    "start_delay_seconds": (0, 60, 1),
    "fade_seconds": (0, 60, 1),
    "hold_minutes": (0, 720, 5),
    "fill_percent": (10, 100, 5),
    "brightness": (1, 100, 1),
    "kelvin": (lighting.KELVIN_MIN, lighting.KELVIN_MAX, lighting.KELVIN_STEP),
}
MAX_NAME = 40
MAX_IGNORE = 20


class PlanError(ValueError):
    """A setting that cannot be stored; the message says which one and why."""


# ---------------------------------------------------------------------------
# Defaults and validation
# ---------------------------------------------------------------------------


def default_slots() -> dict[str, dict[str, float]]:
    """The five slot levels: the Director's light priors on top of a neutral 50 % / 4800 K key light."""
    slots = {}
    for slot in SLOT_ORDER:
        d_fraction, d_kelvin = daylight.LIGHT_PRIORS[slot]
        slots[slot] = {
            "brightness": round(min(100.0, max(1.0, BASE_BRIGHTNESS + 100.0 * d_fraction))),
            "kelvin": lighting.clamp_kelvin(BASE_KELVIN + d_kelvin),
        }
    return slots


def defaults() -> dict[str, Any]:
    return {
        "version": VERSION,
        "follow": {
            "enabled": True,
            "grace_seconds": 120,
            "start_delay_seconds": 3,
            "turn_off": True,
            "apps": True,
            "virtualcam": True,
            "stream": True,
            "record": True,
            "ignore": [],
            "devices": [],
        },
        "schedule": {
            "enabled": True,
            "mode": "sun",
            "slots": default_slots(),
            "hours": None,
            "fade_seconds": 3,
            "hold_minutes": 90,
            "fill_percent": 60,
        },
        "lights": {},
        "resume": 0.0,
    }


def _clamp(key: str, value: object, label: str | None = None) -> float:
    low, high, _ = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanError(f"{label or key} must be a number")
    return float(min(high, max(low, value)))


def _flag(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise PlanError(f"{label} must be true or false")
    return value


def _name_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PlanError(f"{label} must be a list of names")
    names = []
    for item in value:
        text = item.strip()
        if text and text not in names:
            names.append(text[:60])
    if len(names) > MAX_IGNORE:
        raise PlanError(f"{label} holds at most {MAX_IGNORE} entries")
    return names


def level(brightness: object, kelvin: object, label: str = "level") -> dict[str, float]:
    """One validated {brightness: 1..100, kelvin: 2700..6500 in 100 K steps}."""
    return {
        "brightness": round(_clamp("brightness", brightness, f"{label} brightness")),
        "kelvin": lighting.clamp_kelvin(_clamp("kelvin", kelvin, f"{label} temperature")),
    }


def _merge(base: dict[str, Any], patch: dict[str, Any], rules: dict[str, Any], section: str) -> dict[str, Any]:
    """`base` with the keys of `patch` replaced after validation; an unknown key is an error, never silently kept."""
    out = dict(base)
    for key, value in patch.items():
        if key not in rules:
            raise PlanError(f"Unknown {section} setting {key!r}.")
        out[key] = rules[key](value)
    return out


_FOLLOW_RULES = {
    "enabled": lambda v: _flag(v, "enabled"),
    "grace_seconds": lambda v: round(_clamp("grace_seconds", v)),
    "start_delay_seconds": lambda v: round(_clamp("start_delay_seconds", v)),
    "turn_off": lambda v: _flag(v, "turn_off"),
    "apps": lambda v: _flag(v, "apps"),
    "virtualcam": lambda v: _flag(v, "virtualcam"),
    "stream": lambda v: _flag(v, "stream"),
    "record": lambda v: _flag(v, "record"),
    "ignore": lambda v: _name_list(v, "ignore"),
    "devices": lambda v: _device_list(v),
}


def _device_list(value: object) -> list[str]:
    paths = _name_list(value, "devices")
    for path in paths:
        if not path.startswith("/dev/"):
            raise PlanError(f"{path!r} is not a device path (it must start with /dev/).")
    return paths


def _mode(value: object) -> str:
    if value not in MODES:
        raise PlanError(f"mode must be one of {', '.join(MODES)}")
    return str(value)


_SCHEDULE_RULES = {
    "enabled": lambda v: _flag(v, "enabled"),
    "mode": _mode,
    "fade_seconds": lambda v: round(_clamp("fade_seconds", v)),
    "hold_minutes": lambda v: round(_clamp("hold_minutes", v)),
    "fill_percent": lambda v: round(_clamp("fill_percent", v)),
}


def validate_follow(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    return _merge(current, patch, _FOLLOW_RULES, "camera follow")


def validate_schedule(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    return _merge(current, patch, _SCHEDULE_RULES, "schedule")


def validate_slot(current: dict[str, Any], slot: object, patch: dict[str, Any]) -> dict[str, Any]:
    if slot not in SLOT_ORDER:
        raise PlanError(f"slot must be one of {', '.join(SLOT_ORDER)}")
    slots = copy.deepcopy(current["slots"])
    old = slots[slot]
    slots[slot] = level(patch.get("brightness", old["brightness"]), patch.get("kelvin", old["kelvin"]), str(slot))
    return {**current, "slots": slots}


def validate_hour(current: dict[str, Any], hour: object, patch: dict[str, Any], seed: list[dict[str, float]]) -> dict:
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise PlanError("hour must be a whole number from 0 to 23")
    hours = copy.deepcopy(current["hours"]) if current.get("hours") else copy.deepcopy(seed)
    old = hours[hour]
    hours[hour] = level(patch.get("brightness", old["brightness"]), patch.get("kelvin", old["kelvin"]), f"{hour}:00")
    return {**current, "hours": hours}


def _clean_name(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) > MAX_NAME or any(ord(c) < 32 for c in value):
        raise PlanError(f"A light name is at most {MAX_NAME} characters, without control characters.")
    return value.strip()


def _role(value: object) -> str:
    if value not in ROLES:
        raise PlanError(f"role must be one of {', '.join(ROLES)}")
    return str(value)


LIGHT_DEFAULTS: dict[str, Any] = {
    "name": "",
    "role": "key",
    "min_brightness": 1,
    "max_brightness": 100,
    "min_kelvin": lighting.KELVIN_MIN,
    "max_kelvin": lighting.KELVIN_MAX,
}
_LIGHT_RULES = {
    "name": _clean_name,
    "role": _role,
    "min_brightness": lambda v: round(_clamp("brightness", v, "min_brightness")),
    "max_brightness": lambda v: round(_clamp("brightness", v, "max_brightness")),
    "min_kelvin": lambda v: lighting.clamp_kelvin(_clamp("kelvin", v, "min_kelvin")),
    "max_kelvin": lambda v: lighting.clamp_kelvin(_clamp("kelvin", v, "max_kelvin")),
}


def light_settings(data: dict[str, Any], light_id: str) -> dict[str, Any]:
    """This light's name, role and clamps, with the defaults filled in."""
    return {**LIGHT_DEFAULTS, **(data.get("lights", {}).get(light_id) or {})}


def validate_light(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = _merge({**LIGHT_DEFAULTS, **current}, patch, _LIGHT_RULES, "light")
    if out["min_brightness"] > out["max_brightness"]:
        raise PlanError("The minimum brightness cannot be above the maximum.")
    if out["min_kelvin"] > out["max_kelvin"]:
        raise PlanError("The coolest limit cannot be warmer than the warmest.")
    return {k: v for k, v in out.items() if v != LIGHT_DEFAULTS[k]}  # only what differs is stored


def normalise(raw: object) -> dict[str, Any]:
    """The settings from whatever the file held: every field validated on its own, bad ones replaced by defaults."""
    data = defaults()
    if not isinstance(raw, dict):
        return data
    for section, validate in (("follow", validate_follow), ("schedule", validate_schedule)):
        saved = raw.get(section)
        if not isinstance(saved, dict):
            continue
        for key, value in saved.items():
            try:
                data[section] = validate(data[section], {key: value})
            except PlanError:
                continue  # a hand-edited bad value keeps its default, the rest of the file still counts
    saved_slots = (raw.get("schedule") or {}).get("slots") if isinstance(raw.get("schedule"), dict) else None
    for slot, value in (saved_slots or {}).items() if isinstance(saved_slots, dict) else ():
        try:
            data["schedule"] = validate_slot(data["schedule"], slot, value if isinstance(value, dict) else {})
        except PlanError:
            continue
    hours = (raw.get("schedule") or {}).get("hours") if isinstance(raw.get("schedule"), dict) else None
    if isinstance(hours, list) and len(hours) == 24:
        try:
            data["schedule"]["hours"] = [
                level(h.get("brightness"), h.get("kelvin"), f"{i}:00") for i, h in enumerate(hours)
            ]
        except (PlanError, AttributeError):
            data["schedule"]["hours"] = None
    lights = raw.get("lights")
    for light_id, value in (lights or {}).items() if isinstance(lights, dict) else ():
        try:
            if isinstance(value, dict) and (kept := validate_light({}, value)):
                data["lights"][str(light_id)] = kept
        except PlanError:
            continue
    resume = raw.get("resume")
    data["resume"] = float(resume) if isinstance(resume, (int, float)) and not isinstance(resume, bool) else 0.0
    return data


# ---------------------------------------------------------------------------
# The settings file
# ---------------------------------------------------------------------------


class Store:
    """The settings file with a stat-based cache, shared by the service and the Console (separate processes)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._cache: tuple[tuple[int, int], dict[str, Any]] | None = None

    def file(self) -> Path:
        return self.path or settings.config_dir() / FILE

    def load(self) -> dict[str, Any]:
        path = self.file()
        try:
            stat = path.stat()
        except OSError:
            self._cache = None
            return defaults()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache and self._cache[0] == stamp:
            return copy.deepcopy(self._cache[1])
        data = normalise(settings.read_json(path))
        self._cache = (stamp, data)
        return copy.deepcopy(data)

    def save(self, data: dict[str, Any]) -> None:
        """Atomic, mode 600. Raises settings.SettingsError when the file cannot be written."""
        settings.write_json(self.file(), {**data, "version": VERSION})
        self._cache = None

    def update(self, section: str, patch: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        if section == "follow":
            data["follow"] = validate_follow(data["follow"], patch)
        elif section == "schedule":
            data["schedule"] = validate_schedule(data["schedule"], patch)
        else:
            raise PlanError(f"Unknown settings section {section!r}.")
        self.save(data)
        return data

    def set_slot(self, slot: object, patch: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        data["schedule"] = validate_slot(data["schedule"], slot, patch)
        self.save(data)
        return data

    def set_hour(self, hour: object, patch: dict[str, Any], seed: list[dict[str, float]]) -> dict[str, Any]:
        data = self.load()
        data["schedule"] = validate_hour(data["schedule"], hour, patch, seed)
        self.save(data)
        return data

    def seed_hours(self, seed: list[dict[str, float]]) -> dict[str, Any]:
        data = self.load()
        if not data["schedule"].get("hours"):
            data["schedule"]["hours"] = copy.deepcopy(seed)
            self.save(data)
        return data

    def set_light(self, light_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        kept = validate_light(data["lights"].get(light_id, {}), patch)
        if kept:
            data["lights"][light_id] = kept
        else:
            data["lights"].pop(light_id, None)
        self.save(data)
        return data

    def reset(self, what: str) -> dict[str, Any]:
        data, fresh = self.load(), defaults()
        if what == "all":
            data = {**fresh, "resume": data["resume"]}
        elif what in ("follow", "schedule", "lights"):
            data[what] = fresh[what]
        else:
            raise PlanError(f"Unknown settings section {what!r}.")
        self.save(data)
        return data

    def resume(self) -> dict[str, Any]:
        """Ask the service to take the lights back from a manual change now."""
        data = self.load()
        data["resume"] = time.time()
        self.save(data)
        return data


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------

BLEND_MINUTES = 30  # the slot mix looks this far either side of now, so a slot change is a slow cross-fade


def sun_level(schedule: dict[str, Any], when: datetime, loc: daylight.Location) -> dict[str, float]:
    """The slot levels blended over the hour around `when` (seven samples, ten minutes apart)."""
    weights: dict[str, int] = {}
    for offset in range(-BLEND_MINUTES, BLEND_MINUTES + 1, 10):
        moment = when + timedelta(minutes=offset)
        elevation, _, hour_angle = daylight.solar_position(moment, loc.latitude, loc.longitude)
        slot = daylight.slot_for(elevation, hour_angle)
        weights[slot] = weights.get(slot, 0) + 1
    total = sum(weights.values())
    brightness = sum(schedule["slots"][s]["brightness"] * n for s, n in weights.items()) / total
    kelvin = sum(schedule["slots"][s]["kelvin"] * n for s, n in weights.items()) / total
    return {"brightness": round(brightness, 1), "kelvin": lighting.clamp_kelvin(kelvin)}


def clock_level(hours: list[dict[str, float]], when: datetime) -> dict[str, float]:
    """The 24 hour table interpolated between the hours' midpoints (hour 23 blends into hour 0)."""
    position = (when.hour * 60 + when.minute + when.second / 60 - 30) / 60.0  # hour h is centred on h:30
    lower = math.floor(position)
    fraction = position - lower
    a, b = hours[lower % 24], hours[(lower + 1) % 24]
    return {
        "brightness": round(a["brightness"] + (b["brightness"] - a["brightness"]) * fraction, 1),
        "kelvin": lighting.clamp_kelvin(a["kelvin"] + (b["kelvin"] - a["kelvin"]) * fraction),
    }


def level_at(data: dict[str, Any], when: datetime, loc: daylight.Location) -> dict[str, float]:
    """What the key light should be at `when`. `clock` mode without a saved table uses the sun levels."""
    schedule = data["schedule"]
    if schedule["mode"] == "clock" and schedule.get("hours"):
        return clock_level(schedule["hours"], when)
    return sun_level(schedule, when, loc)


def hours_from_sun(data: dict[str, Any], day: datetime, loc: daylight.Location) -> list[dict[str, float]]:
    """A 24 hour table sampled from the sun schedule: where the clock editor starts from."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = []
    for hour in range(24):
        value = sun_level(data["schedule"], start + timedelta(minutes=hour * 60 + 30), loc)
        hours.append({"brightness": round(value["brightness"]), "kelvin": value["kelvin"]})
    return hours


def for_light(target: dict[str, float], light: dict[str, Any], fill_percent: float = 60) -> tuple[float, int] | None:
    """(brightness percent, kelvin) for one light from the key level: its role's share, then its own clamps.

    None for a light whose role is manual.
    """
    role = light.get("role", "key")
    if role == "manual":
        return None
    brightness = target["brightness"] * (fill_percent / 100.0 if role == "fill" else 1.0)
    brightness = min(light["max_brightness"], max(light["min_brightness"], brightness))
    kelvin = min(light["max_kelvin"], max(light["min_kelvin"], target["kelvin"]))
    return round(min(100.0, max(1.0, brightness)), 1), lighting.clamp_kelvin(kelvin)


def curve(data: dict[str, Any], day: date, tz: Any, loc: daylight.Location, step_minutes: int = 10) -> list[dict]:
    """The key light's level across a local day, for the page's 24 hour graph."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    points = []
    for minute in range(0, 24 * 60 + 1, step_minutes):
        when = start + timedelta(minutes=minute)
        value = level_at(data, when, loc)
        points.append({"minute": minute, "brightness": value["brightness"], "kelvin": value["kelvin"]})
    return points


def fade_needed(
    model: lighting.Model, lumen_now: int | None, percent: float, kelvin_now: int | None, kelvin: int
) -> bool:
    """A jump large enough to deserve a fade instead of a plain write."""
    if lumen_now is None or kelvin_now is None:
        return False
    span = model.lumen_max - model.lumen_min
    return abs(lighting.percent_to_lumen(model, percent) - lumen_now) > 0.15 * span or abs(kelvin - kelvin_now) >= 600


def state_path() -> Path:
    """Where the service reports what it is doing, for the Console."""
    return config.state_dir() / "lights-status.json"
