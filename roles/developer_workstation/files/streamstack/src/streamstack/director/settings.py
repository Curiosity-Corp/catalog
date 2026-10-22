"""The director's own settings, shared by the auto service and the Console.

`~/.config/streamstack/director.json` holds what a person chose on the Auto page: the persona and the property
location. It is re-read on every tick, so a change takes effect without restarting auto mode. The service writes
what it saw to `~/.local/state/streamstack/director-status.json`, which the Console shows.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from streamstack import config
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


def write_json(path: Path, payload: dict, mode: int = 0o600) -> None:
    """Atomic JSON write: a reader never sees half a file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise SettingsError(f"cannot write {path}: {exc.strerror}") from exc


def read_json(path: Path) -> dict:
    """A JSON object from `path`; {} when absent or damaged (a damaged file must not stop the director)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict:
    return read_json(settings_path())


def save(settings: dict) -> None:
    write_json(settings_path(), settings)


def set_persona(persona_id: str) -> dict:
    settings = load()
    settings["persona"] = persona_id
    save(settings)
    return settings


def set_property(name: str, latitude: str | float | None, longitude: str | float | None) -> dict:
    """Store the property location (validated); raises SettingsError for a place that does not exist."""
    try:
        lat, lon = check_coordinates(latitude, longitude)
    except LocationError as exc:
        raise SettingsError(str(exc)) from exc
    settings = load()
    settings["property"] = {"name": str(name or "").strip()[:60] or "This property", "latitude": lat, "longitude": lon}
    save(settings)
    return settings


def clear_property() -> dict:
    settings = load()
    settings.pop("property", None)
    save(settings)
    return settings
