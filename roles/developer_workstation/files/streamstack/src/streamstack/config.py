"""Settings and paths.

Settings are KEY=VALUE lines (shell quoting allowed), read in this order, later
files winning:

    built-in defaults
    /etc/streamstack/streamstack.env      (or $STREAMSTACK_ENV_FILE) - the host
    ~/.config/streamstack/streamstack.env - this user's own overrides
"""

from __future__ import annotations

import os
import shlex
from importlib.resources import files
from pathlib import Path

DEFAULTS = {
    "STREAMSTACK_CAMERA_DEVICE": "/dev/video42",
    "STREAMSTACK_CAMERA_LABEL": "Canon EOS 50D",
    "STREAMSTACK_PROGRAM_DEVICE": "/dev/video43",
    "STREAMSTACK_PROGRAM_LABEL": "StreamStack Camera",
    "STREAMSTACK_WIDTH": "1920",
    "STREAMSTACK_HEIGHT": "1080",
    "STREAMSTACK_FPS": "30",
    "STREAMSTACK_CAMERA_EXTERNAL": "false",
    "STREAMSTACK_CANON_BRIDGE": "gphoto2",
    "STREAMSTACK_CANON_COMMAND": "",
    "STREAMSTACK_VOICE_PRESET": "StreamStack Voice",
    # Name or part of the name/description of the call microphone; empty picks
    # an Elgato Wave, then the default input.
    "STREAMSTACK_MICROPHONE": "",
    "STREAMSTACK_AUTO_BACKEND": "claude-cli",
    "STREAMSTACK_AUTO_MODEL": "",
    "STREAMSTACK_AUTO_INTERVAL": "15",
    "STREAMSTACK_AUTO_AI_INTERVAL": "300",
    "STREAMSTACK_AUTO_VIDEO": "true",
    "STREAMSTACK_AUTO_AUDIO": "true",
    "STREAMSTACK_AUTO_NOTIFY": "true",
}


def _xdg(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name) or Path.home() / fallback)


def system_env_file() -> Path:
    return Path(os.environ.get("STREAMSTACK_ENV_FILE", "/etc/streamstack/streamstack.env"))


def user_env_file() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "streamstack/streamstack.env"


def secrets_file() -> Path:
    """Per-user secrets (ANTHROPIC_API_KEY for the api backend); must be mode 600."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "streamstack/auto.env"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "streamstack"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "streamstack"


def data_dir() -> Path:
    """Presets, OBS templates and icons shipped with the package."""
    override = os.environ.get("STREAMSTACK_SHARE")
    return Path(override) if override else Path(str(files("streamstack") / "data"))


def truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        values[key.strip().removeprefix("export ").strip()] = " ".join(parts)
    return values


def load_settings(path: Path | None = None) -> dict[str, str]:
    """Defaults, then the host file, then the user's file (or just `path`)."""
    settings = dict(DEFAULTS)
    for source in [path] if path else [system_env_file(), user_env_file()]:
        settings.update(read_env_file(source))
    return settings


def camera_external(cfg: dict[str, str]) -> bool:
    return truthy(cfg["STREAMSTACK_CAMERA_EXTERNAL"])
