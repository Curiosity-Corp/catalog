"""What the person kept, per property, persona and time of day.

At each Claude pass the director records the look that is in place *before* it changes anything: that is what the
person left, or fixed by hand, since the last pass. The next time the same slot comes round at the same property
with the same persona, the session starts close to that look instead of from wherever the camera was left.

The record is a moving average (so one odd evening does not rewrite it), weighted more when the person moved a
control by hand. It is a plain JSON file in the state directory; deleting it forgets everything.
"""

from __future__ import annotations

import time

from streamstack import config
from streamstack.director import settings

FILE = "director-baselines.json"
KEPT_WEIGHT = 0.4
MANUAL_WEIGHT = 0.75
KEEP_ENTRIES = 200
LOOK_KEYS = ("brightness", "contrast", "gamma", "saturation", "warmth", "lut_amount")


def key(property_name: str, persona_id: str, slot: str) -> str:
    return f"{property_name}|{persona_id}|{slot}"


def _path():
    return config.state_dir() / FILE


def load() -> dict:
    data = settings.read_json(_path())
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def get(property_name: str, persona_id: str, slot: str) -> dict | None:
    entry = load().get(key(property_name, persona_id, slot))
    return entry if isinstance(entry, dict) and isinstance(entry.get("look"), dict) else None


def get_light(property_name: str, persona_id: str, slot: str) -> tuple[float, float] | None:
    """The (brightness fraction, kelvin) the person kept the lights at in this slot, when recorded."""
    entry = get(property_name, persona_id, slot)
    light = entry.get("light") if entry else None
    if isinstance(light, dict) and all(isinstance(light.get(n), (int, float)) for n in ("brightness", "kelvin")):
        return float(light["brightness"]), float(light["kelvin"])
    return None


def record(
    property_name: str,
    persona_id: str,
    slot: str,
    look: dict,
    manual: bool = False,
    mic: tuple[str, float] | None = None,
    light: tuple[float, float] | None = None,
) -> dict:
    """Blend `look` (and the mic gain) into the record for this slot; returns the stored entry."""
    entries = load()
    k = key(property_name, persona_id, slot)
    old = entries.get(k) if isinstance(entries.get(k), dict) else None
    weight = MANUAL_WEIGHT if manual else KEPT_WEIGHT
    sample = {name: float(look[name]) for name in LOOK_KEYS if isinstance(look.get(name), (int, float))}
    if old and isinstance(old.get("look"), dict):
        blended = {
            name: round(old["look"].get(name, value) + (value - old["look"].get(name, value)) * weight, 4)
            for name, value in sample.items()
        }
        count = int(old.get("n", 0)) + 1
    else:
        blended, count = {name: round(v, 4) for name, v in sample.items()}, 1
    entry: dict = {"look": blended, "n": count, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if mic:
        previous = old.get("mic") if old else None
        gain = mic[1]
        if (
            isinstance(previous, dict)
            and previous.get("source") == mic[0]
            and isinstance(previous.get("db"), (int, float))
        ):
            gain = previous["db"] + (gain - previous["db"]) * weight
        entry["mic"] = {"source": mic[0], "db": round(gain, 2)}
    elif old and isinstance(old.get("mic"), dict):
        entry["mic"] = old["mic"]
    if light:
        previous = old.get("light") if old else None
        fraction, kelvin = light
        if isinstance(previous, dict) and all(
            isinstance(previous.get(n), (int, float)) for n in ("brightness", "kelvin")
        ):
            fraction = previous["brightness"] + (fraction - previous["brightness"]) * weight
            kelvin = previous["kelvin"] + (kelvin - previous["kelvin"]) * weight
        entry["light"] = {"brightness": round(fraction, 3), "kelvin": round(kelvin)}
    elif old and isinstance(old.get("light"), dict):
        entry["light"] = old["light"]
    entries[k] = entry
    if len(entries) > KEEP_ENTRIES:  # forget the stalest first
        for stale in sorted(entries, key=lambda name: str(entries[name].get("updated", "")))[
            : len(entries) - KEEP_ENTRIES
        ]:
            del entries[stale]
    settings.write_json(_path(), {"version": 1, "entries": entries})
    return entry
