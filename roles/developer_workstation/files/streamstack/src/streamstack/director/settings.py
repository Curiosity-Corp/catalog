"""The director's own settings, shared by the auto service and the Console.

`~/.config/streamstack/director.json` holds what a person chose on the Auto page: the persona and the property
location. It is re-read on every tick, so a change takes effect without restarting auto mode. The service writes
what it saw to `~/.local/state/streamstack/director-status.json`, which the Console shows.
"""

from __future__ import annotations

from pathlib import Path

from streamstack import config, store
from streamstack.director.daylight import LocationError, check_coordinates

DEFAULT_PERSONA = "daylight-natural"


class SettingsError(RuntimeError):
    """The settings file could not be read or written."""


def config_dir() -> Path:
    return config.user_env_file().parent


def settings_path() -> Path:
    return config_dir() / "director.json"


def status_path() -> Path:
    return config.state_dir() / "director-status.json"


def sheet_path() -> Path:
    return config.cache_dir() / "auto" / "contact-sheet.jpg"


def write_json(path: Path, payload: dict, mode: int = 0o600, *, ephemeral: bool = False) -> None:
    """Atomic, fsynced JSON write that keeps the last good copy (`store.write_json`).

    `ephemeral` is for status files rewritten every tick: still atomic, but no fsync and no backup.
    """
    try:
        store.write_json(path, payload, mode=mode, backup=not ephemeral, durable=not ephemeral)
    except (OSError, TypeError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise SettingsError(f"cannot write {path}: {reason}") from exc


def read_json(path: Path) -> dict:
    """A JSON object from `path`; {} when absent or damaged (a damaged file is set aside and must not stop the
    director; see `store.read_json`)."""
    return store.read_json(path, {}, validate=store.is_object)


def load() -> dict:
    return read_json(settings_path())


def save(settings: dict) -> None:
    write_json(settings_path(), settings)


def with_persona(current: dict, persona_id: str) -> dict:
    """`current` with the persona chosen (a new dict; nothing is written)."""
    return {**current, "persona": persona_id}


def with_property(current: dict, name: str, latitude: str | float | None, longitude: str | float | None) -> dict:
    """`current` with the property location set (validated; a new dict, nothing is written).

    Raises SettingsError for a place that does not exist."""
    try:
        lat, lon = check_coordinates(latitude, longitude)
    except LocationError as exc:
        raise SettingsError(str(exc)) from exc
    place = {"name": str(name or "").strip()[:60] or "This property", "latitude": lat, "longitude": lon}
    return {**current, "property": place}


def without_property(current: dict) -> dict:
    """`current` without a property location (a new dict, nothing is written)."""
    return {k: v for k, v in current.items() if k != "property"}


def set_persona(persona_id: str) -> dict:
    settings = with_persona(load(), persona_id)
    save(settings)
    return settings


def set_property(name: str, latitude: str | float | None, longitude: str | float | None) -> dict:
    """Store the property location (validated); raises SettingsError for a place that does not exist."""
    settings = with_property(load(), name, latitude, longitude)
    save(settings)
    return settings


def clear_property() -> dict:
    settings = without_property(load())
    save(settings)
    return settings
