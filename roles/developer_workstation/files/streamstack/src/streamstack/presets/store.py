"""The user's saved profiles and last platform and tier choice: one JSON file, written atomically.

``~/.config/streamstack/profiles.json`` (mode 600), kept through `streamstack.store`: writes are atomic with a
``.bak`` of the previous good file, and a file that cannot be read is quarantined (``profiles.json.corrupt-<time>``),
restored from the backup when possible, and reported on the System page.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from streamstack import config, store
from streamstack.presets.schema import PROFILES_SCHEMA, PresetError, validate_profile

log = logging.getLogger(__name__)

DOCUMENT = "streamstack.profile-store/1"


def default_path() -> Path:
    return config.user_env_file().parent / "profiles.json"


class ProfileStore:
    def __init__(self, path: Path | None = None, *, platforms: set[str] | None = None, tiers: set[str] | None = None):
        self._path = path
        self.platforms, self.tiers = platforms, tiers
        self.lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path or default_path()

    # -- reading ----------------------------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        """The stored document; a damaged file is quarantined (or restored from its backup) by `streamstack.store`."""
        data = store.read_json(self.path, {}, validate=store.is_object)
        return data if isinstance(data, dict) else {}

    def profiles(self) -> dict[str, dict[str, Any]]:
        """Saved profiles by name. A hand-edited bad entry is skipped (and logged), never fatal."""
        with self.lock:
            data = self._read()
        saved: dict[str, dict[str, Any]] = {}
        entries = data.get("profiles")
        for name, entry in entries.items() if isinstance(entries, dict) else ():
            try:
                profile = validate_profile(entry, platforms=self.platforms, tiers=self.tiers)
            except PresetError as exc:
                log.warning("profile %r skipped: %s", name, exc)
                continue
            profile.pop("id", None)
            saved[profile["name"]] = profile
        return saved

    def selection(self) -> dict[str, str | None]:
        with self.lock:
            data = self._read()
        chosen = data.get("selection")
        chosen = chosen if isinstance(chosen, dict) else {}
        platform, tier = chosen.get("platform"), chosen.get("tier")
        if self.platforms is not None and platform not in self.platforms:
            platform = None
        if self.tiers is not None and tier not in self.tiers:
            tier = None
        return {"platform": platform, "tier": tier}

    # -- writing ----------------------------------------------------------------------------------------

    def _write(self, profiles: dict[str, dict[str, Any]], selection: dict[str, str | None]) -> None:
        payload = {"schema": DOCUMENT, "profiles": profiles, "selection": selection}
        store.write_json(self.path, payload, mode=0o600)

    def save_many(self, items: list[dict[str, Any]]) -> list[str]:
        """Validate and store profiles (replacing same-name ones); all or nothing. Returns the names saved."""
        with self.lock:
            profiles, selection = self.profiles(), self.selection()
            for item in items:
                profile = validate_profile(item, platforms=self.platforms, tiers=self.tiers)
                profile.pop("id", None)
                profiles[profile["name"]] = profile
            self._write(profiles, selection)
        return [item["name"].strip() for item in items]

    def delete(self, name: str) -> None:
        with self.lock:
            profiles, selection = self.profiles(), self.selection()
            if name not in profiles:
                raise PresetError(f"no saved profile called {name!r}")
            del profiles[name]
            self._write(profiles, selection)

    def set_selection(self, platform: str | None, tier: str | None) -> None:
        with self.lock:
            self._write(self.profiles(), {"platform": platform, "tier": tier})


def export_document(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """The portable file for one or more profiles: ids are dropped (they belong to the shipped ones)."""
    clean = [{k: v for k, v in p.items() if k != "id"} for p in profiles]
    return {"schema": PROFILES_SCHEMA, "profiles": clean}
