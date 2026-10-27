"""The shipped preset catalog: looks, platforms, bandwidth tiers and built-in profiles, loaded from JSON data files.

The data lives in ``data/presets/*.json`` (so it can be reviewed and tested as data) and is validated on first use;
a bad shipped file raises `PresetError` at once rather than misbehaving later. Thumbnails are in
``data/console/presets/looks/`` (served by the Console) and the .cube files in ``data/presets/luts/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from streamstack import config
from streamstack.presets.luts import LUTS
from streamstack.presets.schema import (
    PROFILES_SCHEMA,
    PresetError,
    validate_look,
    validate_platform,
    validate_profile,
    validate_tier,
)
from streamstack.presets.thumbs import slug

THUMB_URL = "static/presets/looks/{slug}.png"


def presets_dir() -> Path:
    return config.data_dir() / "presets"


def thumbs_dir() -> Path:
    return config.data_dir() / "console/presets/looks"


def _load(name: str, schema: str) -> dict[str, Any]:
    path = presets_dir() / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PresetError(f"{name}: cannot read the shipped preset file: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise PresetError(f"{name}: schema must be {schema!r}")
    return data


@dataclass(frozen=True)
class Catalog:
    categories: tuple[dict[str, str], ...]
    looks: tuple[dict[str, Any], ...]
    platforms: tuple[dict[str, Any], ...]
    tiers: tuple[dict[str, Any], ...]
    profiles: tuple[dict[str, Any], ...]
    checked: str

    def platform(self, platform_id: str) -> dict[str, Any]:
        for item in self.platforms:
            if item["id"] == platform_id:
                return item
        raise PresetError(f"unknown platform {platform_id!r}")

    def tier(self, tier_id: str) -> dict[str, Any]:
        for item in self.tiers:
            if item["id"] == tier_id:
                return item
        raise PresetError(f"unknown tier {tier_id!r}")

    @property
    def platform_ids(self) -> set[str]:
        return {p["id"] for p in self.platforms}

    @property
    def tier_ids(self) -> set[str]:
        return {t["id"] for t in self.tiers}

    def as_json(self) -> dict[str, Any]:
        """What `GET /api/presets/catalog` sends the Console (looks are in the state snapshot already)."""
        return {
            "checked": self.checked,
            "categories": [*self.categories, {"id": "yours", "label": "Yours"}],
            "platforms": list(self.platforms),
            "tiers": list(self.tiers),
            "profiles": list(self.profiles),
        }


def _unique(items: list[dict[str, Any]], key: str, what: str) -> None:
    seen: set[str] = set()
    for item in items:
        folded = item[key].casefold()
        if folded in seen:
            raise PresetError(f"duplicate {what} {item[key]!r}")
        seen.add(folded)


@lru_cache(maxsize=1)
def load() -> Catalog:
    looks_doc = _load("looks.json", "streamstack.looks/1")
    categories = tuple({"id": str(c["id"]), "label": str(c["label"])} for c in looks_doc["categories"])
    looks = [validate_look(entry, i) for i, entry in enumerate(looks_doc["looks"])]
    _unique(looks, "name", "look")
    known = {c["id"] for c in categories}
    for look in looks:
        if look["category"] not in known:
            raise PresetError(f"look {look['name']!r}: unknown category {look['category']!r}")
        if look["lut"] is not None and look["lut"] not in LUTS:
            raise PresetError(f"look {look['name']!r}: no generated LUT called {look['lut']!r}")
        look["thumb"] = THUMB_URL.format(slug=slug(look["name"]))

    platform_doc = _load("platforms.json", "streamstack.platforms/1")
    platforms = [validate_platform(entry, i) for i, entry in enumerate(platform_doc["platforms"])]
    _unique(platforms, "id", "platform")
    tier_doc = _load("tiers.json", "streamstack.tiers/1")
    tiers = [validate_tier(entry, i) for i, entry in enumerate(tier_doc["tiers"])]
    _unique(tiers, "id", "tier")

    profile_doc = _load("profiles.json", PROFILES_SCHEMA)
    platform_ids, tier_ids = {p["id"] for p in platforms}, {t["id"] for t in tiers}
    profiles = []
    for i, entry in enumerate(profile_doc["profiles"]):
        profile = validate_profile(entry, platforms=platform_ids, tiers=tier_ids, path=f"profiles[{i}]")
        if "id" not in profile:
            raise PresetError(f"profiles[{i}]: a built-in profile needs an id")
        if profile["look"] and profile["look"] not in {look["name"] for look in looks}:
            raise PresetError(f"profile {profile['name']!r}: unknown look {profile['look']!r}")
        profiles.append(profile)
    _unique(profiles, "id", "profile")
    _unique(profiles, "name", "profile")
    return Catalog(
        categories, tuple(looks), tuple(platforms), tuple(tiers), tuple(profiles), str(platform_doc["checked"])
    )
