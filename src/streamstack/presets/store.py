"""The user's saved profiles and last platform and tier choice: one JSON file, written atomically.

``~/.config/streamstack/profiles.json`` (mode 600), kept through `streamstack.store`: writes are atomic with a
``.bak`` of the previous good file, and a file that cannot be read is quarantined (``profiles.json.corrupt-<time>``),
restored from the backup when possible, and reported on the System page.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import config, store
from streamstack.presets.schema import PROFILES_SCHEMA, PresetError, validate_profile

log = logging.getLogger(__name__)

DOCUMENT = "streamstack.profile-store/1"


def default_path() -> Path:
    return config.user_env_file().parent / "profiles.json"


class ProfileStore:
    """Reads never wait for a write.

    A save is an atomic replace with two fsyncs, which takes seconds on a starved disk. The Console's state snapshot
    reads the selection and the profiles on every push, so a reader that queued behind a save froze every window for
    as long as the disk stalled (issue #161). Writers take `lock` to serialise read-modify-write; readers take
    nothing: they see the document being saved (`_pending`, published before the write starts, withdrawn if it
    fails) or the last one read from the file, which is re-read only when the file changed.
    """

    def __init__(self, path: Path | None = None, *, platforms: set[str] | None = None, tiers: set[str] | None = None):
        self._path = path
        self.platforms, self.tiers = platforms, tiers
        self.lock = threading.RLock()
        self._pending: dict[str, Any] | None = None  # the document a writer is saving right now
        self._cached: tuple[tuple[int, int, int], dict[str, Any]] | None = None  # (mtime_ns, size, inode) -> document

    @property
    def path(self) -> Path:
        return self._path or default_path()

    # -- reading ----------------------------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        """The stored document; a damaged file is quarantined (or restored from its backup) by `streamstack.store`."""
        pending = self._pending
        if pending is not None:
            return pending
        try:
            seen = self.path.stat()
            key = (seen.st_mtime_ns, seen.st_size, seen.st_ino)
        except OSError:
            key = None  # absent or unreadable: let the store decide (and report) what that means
        cached = self._cached
        if key is not None and cached is not None and cached[0] == key:
            return cached[1]
        data = store.read_json(self.path, {}, validate=store.is_object)
        data = data if isinstance(data, dict) else {}
        # The key was taken before the read: a file replaced in between is simply read again next time.
        self._cached = (key, data) if key is not None else None
        return data

    def profiles(self) -> dict[str, dict[str, Any]]:
        """Saved profiles by name. A hand-edited bad entry is skipped (and logged), never fatal."""
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

    def _write(
        self,
        profiles: dict[str, dict[str, Any]],
        selection: dict[str, str | None],
        visible: Callable[[], None] | None = None,
    ) -> None:
        payload = {"schema": DOCUMENT, "profiles": profiles, "selection": selection}
        with self.lock:
            self._pending = payload  # readers see the new document from here on, not after the fsyncs
            if visible is not None:
                visible()  # still before the fsyncs: whoever shows the new document alongside it need not wait either
            try:
                store.write_json(self.path, payload, mode=0o600)
            finally:
                self._pending = None  # saved (the file now says the same) or failed (nothing changed)
                self._cached = None

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

    def set_selection(
        self, platform: str | None, tier: str | None, *, visible: Callable[[], None] | None = None
    ) -> None:
        """Save the choice. `visible`, when given, is called once readers can see it and before any disk write."""
        with self.lock:
            self._write(self.profiles(), {"platform": platform, "tier": tier}, visible)


def export_document(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """The portable file for one or more profiles: ids are dropped (they belong to the shipped ones)."""
    clean = [{k: v for k, v in p.items() if k != "id"} for p in profiles]
    return {"schema": PROFILES_SCHEMA, "profiles": clean}
