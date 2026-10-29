"""The meetings settings file, ~/.config/streamstack/meetings.json (mode 600, atomic, damaged files set aside).

It holds no secret: the Microsoft 365 sign-in and the private calendar links live in the keyring (`tokens.py`); here a
calendar link is only a name and the host it points at.

    {"version": 1, "enabled": true, "lead_minutes": 2, "only_online": true, "steps": ["studio", "camera", "health"],
     "m365": {"client_id": "", "tenant": ""},
     "ics": [{"id": "ab12cd34", "name": "Work", "host": "outlook.office365.com"}],
     "prepped": ["m365:AAMk...:1793196000"]}
"""

from __future__ import annotations

import copy
import re
import threading
from pathlib import Path
from typing import Any

from streamstack import config, store

VERSION = 1
LEAD_RANGE = (0, 30)
MAX_ICS = 8
MAX_PREPPED = 100
ICS_ID = re.compile(r"[a-z0-9]{8}")
STEP_IDS = ("studio", "camera", "mic", "health")
DEFAULT_STEPS = ["studio", "camera", "health"]


def defaults() -> dict[str, Any]:
    return {
        "version": VERSION,
        "enabled": True,
        "lead_minutes": 2,
        "only_online": True,
        "steps": list(DEFAULT_STEPS),
        "m365": {"client_id": "", "tenant": ""},
        "ics": [],
        "prepped": [],
    }


def clean(raw: Any) -> dict[str, Any]:
    """A usable settings object from whatever was read: bad parts fall back to the default, good parts are kept."""
    out = defaults()
    if not isinstance(raw, dict):
        return out
    out["enabled"] = raw.get("enabled") is not False
    out["only_online"] = raw.get("only_online") is not False
    lead = raw.get("lead_minutes")
    if isinstance(lead, int) and not isinstance(lead, bool) and LEAD_RANGE[0] <= lead <= LEAD_RANGE[1]:
        out["lead_minutes"] = lead
    steps = raw.get("steps")
    if isinstance(steps, list):
        out["steps"] = [s for i, s in enumerate(steps) if s in STEP_IDS and s not in steps[:i]]
    m365 = raw.get("m365")
    if isinstance(m365, dict):
        out["m365"] = {k: str(m365.get(k, ""))[:80] for k in ("client_id", "tenant")}
    seen: set[str] = set()
    for item in raw.get("ics") if isinstance(raw.get("ics"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ICS_ID.fullmatch(item["id"]):
            continue
        if item["id"] in seen or len(out["ics"]) >= MAX_ICS:
            continue
        seen.add(item["id"])
        out["ics"].append(
            {"id": item["id"], "name": str(item.get("name", ""))[:60], "host": str(item.get("host", ""))[:100]}
        )
    prepped = raw.get("prepped")
    if isinstance(prepped, list):
        out["prepped"] = [str(p)[:200] for p in prepped if isinstance(p, str)][-MAX_PREPPED:]
    return out


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def file(self) -> Path:
        return self.path or config.user_env_file().parent / "meetings.json"

    def load(self) -> dict[str, Any]:
        with self._lock:
            return clean(store.read_json(self.file(), {}, validate=store.is_object))

    def save(self, settings: dict[str, Any]) -> None:
        with self._lock:
            store.write_json(self.file(), clean(settings), mode=0o600)


class MemoryStore:
    """The demo rig's settings: nothing on disk."""

    def __init__(self) -> None:
        self._settings = defaults()

    def load(self) -> dict[str, Any]:
        return copy.deepcopy(self._settings)

    def save(self, settings: dict[str, Any]) -> None:
        self._settings = clean(copy.deepcopy(settings))
