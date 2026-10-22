"""What the director last saw and did, in one small file the Console reads.

The auto loop (`streamstack auto run`) writes `director-status.json` after every pass; the Console and the CLI only
read it. The time-of-day block is not stored: it is computed on every read, offline, from the property location, so
the Auto page's "Now: Dawn, daylight rising" is always current even between passes.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from streamstack import config
from streamstack.director import daylight, personas, settings

VERSION = 1


def time_block(cfg: dict[str, str], now: datetime | None = None, prefs: dict | None = None) -> dict:
    """The property, the sun now and the day's slots as the Console draws them."""
    now = now or datetime.now().astimezone()
    prefs = settings.load() if prefs is None else prefs
    location = daylight.resolve_location(prefs, cfg, now)
    sun = daylight.sun_state(now, location)
    return {
        "location": location.public(),
        "sun": {**sun.public(), "clock": now.strftime("%H:%M")},
        "strip": daylight.day_strip(now, location),
        "minute": now.hour * 60 + now.minute,
    }


def read() -> dict:
    """The last status the loop wrote ({} when it has never run or the file is damaged)."""
    data = settings.read_json(settings.status_path())
    return data if data.get("version") == VERSION else {}


def write(payload: dict) -> None:
    settings.write_json(settings.status_path(), {**payload, "version": VERSION})


def publish_sheet(sheet: Path) -> Path:
    """Copy the contact sheet to where the Console serves it from (private to this user)."""
    target = settings.sheet_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    tmp = target.with_suffix(".tmp")
    shutil.copyfile(sheet, tmp)
    tmp.chmod(0o600)
    tmp.replace(target)
    return target


def sheet_bytes() -> bytes | None:
    """The last contact sheet, or None."""
    try:
        return settings.sheet_path().read_bytes()
    except OSError:
        return None


def library() -> personas.Library:
    return personas.load_library()


def view(cfg: dict[str, str] | None = None, now: datetime | None = None) -> dict:
    """Everything the Auto page shows about the director, assembled from files; never raises for missing data."""
    cfg = cfg if cfg is not None else config.load_settings()
    prefs = settings.load()
    lib = library()
    chosen = lib.get(prefs.get("persona"))
    out = {
        "persona": chosen["id"],
        "personas": lib.listing(),
        "persona_problems": lib.problems,
        "time": time_block(cfg, now, prefs),
        "status": read(),
        "has_sheet": settings.sheet_path().exists(),
    }
    return out
