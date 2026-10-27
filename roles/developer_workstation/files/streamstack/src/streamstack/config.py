"""Settings and paths.

Settings are KEY=VALUE lines (shell quoting allowed), read in this order, later
files winning:

    built-in defaults
    /etc/streamstack/streamstack.env      (or $STREAMSTACK_ENV_FILE) - the host
    ~/.config/streamstack/streamstack.env - this user's own overrides
"""

from __future__ import annotations

import logging
import os
import shlex
from importlib.resources import files
from pathlib import Path

from streamstack import store

log = logging.getLogger(__name__)

# Settings and state of the retired external video path: ignored when read, cleared by retire_leftovers().
RETIRED_KEYS = frozenset({"STREAMSTACK_VIDEO_ENGINE"})
RETIRED_STATE_FILES = ("camlink.json", "camlink.json.bak", "framing-restore.json", "framing-restore.json.bak")

DEFAULTS = {
    "STREAMSTACK_CAMERA_DEVICE": "/dev/video42",
    "STREAMSTACK_CAMERA_LABEL": "Canon EOS 50D",
    "STREAMSTACK_PROGRAM_DEVICE": "/dev/video43",
    "STREAMSTACK_PROGRAM_LABEL": "StreamStack Camera",
    "STREAMSTACK_WIDTH": "1920",
    "STREAMSTACK_HEIGHT": "1080",
    "STREAMSTACK_FPS": "30",
    # Recording and streaming (docs/stream.md): the PipeWire source that carries the program audio, a lip-sync trim in
    # milliseconds (positive delays the sound), where recordings go, and how many encoders may run at once.
    "STREAMSTACK_STREAM_AUDIO": "streamstack_mic",
    "STREAMSTACK_STREAM_AUDIO_OFFSET_MS": "0",
    "STREAMSTACK_RECORD_DIR": "~/Videos/StreamStack",
    "STREAMSTACK_STREAM_MAX_ENCODES": "2",
    # Cam Link capture mode: "auto" picks the best the HDMI source offers (1080p60 > 1080p50 > 1080p30, raw before
    # MJPEG) and the program camera follows it; "off" keeps STREAMSTACK_WIDTH/_HEIGHT/_FPS; "1920x1080@60 YUYV"
    # pins one.
    "STREAMSTACK_CAMLINK_MODE": "auto",
    "STREAMSTACK_CAMLINK_4K": "false",
    # The video engine behind the program camera (StreamStack's own VA-API/Vulkan pipeline). Size: source (the
    # camera's own) or WIDTHxHEIGHT. Backend: auto | vulkan | cpu. Render node: empty lets the hardware Plan choose
    # (ADR 0003). LUT: a .cube file (path without spaces) applied before the look.
    "STREAMSTACK_ENGINE_SIZE": "source",
    "STREAMSTACK_ENGINE_BACKEND": "auto",
    "STREAMSTACK_ENGINE_RENDER_NODE": "",
    "STREAMSTACK_ENGINE_LUT": "",
    # Background blur on the Arc through OpenVINO (needs `pip install 'streamstack[blur]'`): auto | off. Device: auto
    # picks the discrete Arc; or an OpenVINO device id such as GPU.1.
    "STREAMSTACK_ENGINE_BLUR": "auto",
    "STREAMSTACK_ENGINE_BLUR_DEVICE": "auto",
    "STREAMSTACK_CAMERA_EXTERNAL": "false",
    "STREAMSTACK_CANON_BRIDGE": "gphoto2",
    "STREAMSTACK_CANON_COMMAND": "",
    # native: the camera's own aspect, never enlarged; fill: crop to the canvas (the 0.2 behaviour)
    "STREAMSTACK_CANON_OUTPUT": "native",
    # auto tries Intel Quick Sync, then VA-API, then the CPU; or force qsv | vaapi | cpu
    "STREAMSTACK_CANON_DECODE": "auto",
    # auto uses python-gphoto2 (one shared USB session) when installed, else the gphoto2 command (pauses for changes)
    "STREAMSTACK_CANON_BACKEND": "auto",
    "STREAMSTACK_CANON_RENDER_NODE": "",
    # The stream encoder's render node; empty lets the hardware Plan choose by role (the Arc on both studios)
    "STREAMSTACK_STREAM_RENDER_NODE": "",
    "STREAMSTACK_VOICE_PRESET": "StreamStack Voice",
    # Name or part of the name/description of the call microphone; empty picks
    # an Elgato Wave, then the default input.
    "STREAMSTACK_MICROPHONE": "",
    # The audio interface to drive: auto (detected by USB id), off, a profile id (scarlett-2i2-gen4, scarlett-2i2-gen3,
    # wave-xlr) or a USB id such as 1235:8219.
    "STREAMSTACK_INTERFACE": "auto",
    # The studio rig profile (src/streamstack/data/hardware/rigs.toml): auto picks the best overlap with the hardware
    # found, or name one (studio-wave-xlr, studio-deck, generic-linux). A STREAMSTACK_RIG in the environment wins.
    "STREAMSTACK_RIG": "auto",
    # Wave XLR hardware control over its vendor USB interface: off (never opened),
    # read, or write. See docs/interfaces.md.
    "STREAMSTACK_WAVE_XLR_USB": "off",
    "STREAMSTACK_AUTO_BACKEND": "claude-cli",
    "STREAMSTACK_AUTO_MODEL": "",
    "STREAMSTACK_AUTO_INTERVAL": "15",
    "STREAMSTACK_AUTO_AI_INTERVAL": "300",
    "STREAMSTACK_AUTO_VIDEO": "true",
    "STREAMSTACK_AUTO_AUDIO": "true",
    "STREAMSTACK_AUTO_NOTIFY": "true",
    # Auto-framing (optional extra: pip install 'streamstack[framing]', then streamstack framing --fetch).
    # Mode at start when the Console has not remembered one: off | gentle | tight | follow. Detections per second
    # (2 to 15). Device: auto (Intel GPU, then CPU) | gpu | cpu.
    "STREAMSTACK_FRAMING_MODE": "off",
    "STREAMSTACK_FRAMING_FPS": "8",
    "STREAMSTACK_FRAMING_DEVICE": "auto",
    # The director: stills per Claude pass (contact sheet), seconds between them, seconds of microphone analysed
    # locally, and whether the persona's look is eased in between Claude passes.
    "STREAMSTACK_AUTO_BURST": "4",
    "STREAMSTACK_AUTO_BURST_SECONDS": "1.0",
    "STREAMSTACK_AUTO_CLIP_SECONDS": "6",
    "STREAMSTACK_AUTO_PERSONA_LOOK": "true",
    # Hardware the director may drive when it is present (each is simply absent when its device is not there):
    # the Canon's exposure compensation and Kelvin, Litra brightness and colour temperature, the audio interface's
    # preamp gain, and the YuNet face finder (instead of the skin-colour heuristic) for metering the face.
    "STREAMSTACK_AUTO_CANON": "true",
    "STREAMSTACK_AUTO_LIGHTS": "true",
    "STREAMSTACK_AUTO_HARDWARE_GAIN": "true",
    "STREAMSTACK_AUTO_FACES": "true",
    # Which interface input the director's gain follows: auto (the first with a gain), 1, 2, or a control id.
    "STREAMSTACK_AUTO_INTERFACE_INPUT": "auto",
    # Where the property is, for sunrise and sunset (computed offline). Empty: the time zone's city.
    "STREAMSTACK_PROPERTY": "",
    "STREAMSTACK_LATITUDE": "",
    "STREAMSTACK_LONGITUDE": "",
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
    """Presets, templates and icons shipped with the package."""
    override = os.environ.get("STREAMSTACK_SHARE")
    return Path(override) if override else Path(str(files("streamstack") / "data"))


def truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def read_env_file(path: Path) -> dict[str, str]:
    """The KEY=VALUE pairs of `path`. A missing file is empty; a damaged one is set aside (see `store`); lines that
    cannot be parsed are skipped, with one warning for the file."""
    values: dict[str, str] = {}
    skipped = 0
    for line in store.read_text(path, "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            skipped += 1
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            skipped += 1
            continue
        values[key.strip().removeprefix("export ").strip()] = " ".join(parts)
    if skipped:
        store.record(
            path, f"{skipped} line{'s' if skipped != 1 else ''} could not be read and were ignored", "ignored-lines"
        )
    return values


def set_user_setting(key: str, value: str | None) -> None:
    """Save one setting in the user's own file (None removes it), atomically and keeping the other lines.

    Raises ValueError for a bad name or value and OSError when the file cannot be written.
    """
    path = user_env_file()
    store.update_env_file(path, {key: value}, mode=0o600 if not path.exists() else None)


def load_settings(path: Path | None = None) -> dict[str, str]:
    """Defaults, then the host file, then the user's file (or just `path`)."""
    settings = dict(DEFAULTS)
    for source in [path] if path else [system_env_file(), user_env_file()]:
        settings.update({k: v for k, v in read_env_file(source).items() if k not in RETIRED_KEYS})
    return settings


def retire_leftovers() -> list[str]:
    """Clear what a station configured before the native video engine became the only one; the names cleared.

    Old env files may still carry a `STREAMSTACK_VIDEO_ENGINE` line (ignored on read, removed here from the user's
    own file through the crash-safe store, which keeps a `.bak`), and the state directory may hold records the
    retired engine wrote (a stored capture mode, a crop to put back). None of them is read any more. Each step is
    independent and a failure to clear one is logged, never fatal: a station with old files must start.
    """
    cleared: list[str] = []
    env = user_env_file()
    try:
        stale = sorted(RETIRED_KEYS & read_env_file(env).keys()) if env.exists() else []
        if stale:
            store.update_env_file(env, {key: None for key in stale})
            cleared.extend(stale)
    except (OSError, ValueError) as exc:
        log.warning("could not remove retired settings from %s: %s", env, exc)
    for name in RETIRED_STATE_FILES:
        path = state_dir() / name
        try:
            if path.exists():
                path.unlink()
                cleared.append(name)
        except OSError as exc:
            log.warning("could not remove the retired record %s: %s", path, exc)
    return cleared


def camera_external(cfg: dict[str, str]) -> bool:
    return truthy(cfg["STREAMSTACK_CAMERA_EXTERNAL"])
