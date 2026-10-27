"""The deck profile: pages of keys, the settings, validation, the default layout and where it is saved.

A profile is `{"layout": {"rows", "cols"}, "home": id, "settings": {...}, "pages": [{"id", "name", "keys": [...]}]}`
with `rows * cols` keys per page (None for an empty one). It is saved in ~/.config/streamstack/deck.json (mode 600).
Until the person changes something there is no file: the default profile is built from the rig each time, so it
follows the scenes and presets the rig has.

A profile laid out for one grid still works on a deck with another: `reflow` re-flows it (a bigger deck keeps the
keys where they are, a smaller one gets the extra keys on continuation pages with a "More" and a "Back" key).
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from streamstack import config
from streamstack.deck import catalog
from streamstack.deck.models import BY_ID, DEFAULT_MODEL, MAX_COLS, MAX_ROWS, Model, for_grid

VERSION = 1
MAX_PAGES = 24
MAX_PARAMS_BYTES = 2048
SLEEP_CHOICES = (0, 1, 5, 10, 15, 30, 60)  # minutes; 0 never
DEFAULT_SETTINGS = {"brightness": 70, "sleep_minutes": 15}
PAGE_ID = re.compile(r"[a-z0-9][a-z0-9_~-]{0,30}")
ICON_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
HOME = "main"


class ProfileError(ValueError):
    """The profile (or a key of it) is not valid; the message says what to fix."""


# -- keys ------------------------------------------------------------------------------------------------------------


def clean_key(raw: Any) -> dict | None:
    """A validated key: {"action", "params"} plus "label" and "icon" when set. None for an empty slot."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileError("A key must be an object with an action.")
    action = raw.get("action", "")
    if not isinstance(action, str) or (action and (not catalog.ACTION_NAME.fullmatch(action) or len(action) > 40)):
        raise ProfileError(
            "A key's action must look like video.scene (letters, digits and underscores, one dot or more)."
        )
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ProfileError("A key's params must be an object.")
    try:
        encoded = json.dumps(params, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"A key's params must be JSON: {exc}") from exc
    if len(encoded) > MAX_PARAMS_BYTES:
        raise ProfileError("A key's params are too long.")
    key: dict[str, Any] = {"action": action, "params": json.loads(encoded)}
    label = raw.get("label")
    if label is not None and label != "":
        if not isinstance(label, str) or len(label.strip()) > catalog.MAX_LABEL:
            raise ProfileError(f"A key's label may be at most {catalog.MAX_LABEL} characters.")
        key["label"] = label.strip()
    icon = raw.get("icon")
    if icon is not None and icon != "":
        if not isinstance(icon, str) or not ICON_NAME.fullmatch(icon):
            raise ProfileError("A key's icon must be an icon name such as mic or lights.")
        key["icon"] = icon
    return key


def key(action: str, label: str | None = None, icon: str | None = None, **params: Any) -> dict:
    """Shorthand for building keys in code."""
    out: dict[str, Any] = {"action": action, "params": params}
    if label:
        out["label"] = label
    if icon:
        out["icon"] = icon
    return out


# -- the profile -----------------------------------------------------------------------------------------------------


def clean_settings(raw: Any, base: dict | None = None) -> dict:
    out = dict(base or DEFAULT_SETTINGS)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise ProfileError("settings must be an object.")
    if "brightness" in raw:
        value = raw["brightness"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 5 <= value <= 100:
            raise ProfileError("brightness must be a number from 5 to 100.")
        out["brightness"] = round(value)
    if "sleep_minutes" in raw:
        value = raw["sleep_minutes"]
        if value not in SLEEP_CHOICES:
            raise ProfileError(f"sleep_minutes must be one of {', '.join(map(str, SLEEP_CHOICES))} (0 never).")
        out["sleep_minutes"] = int(value)
    return out


def clean_layout(raw: Any) -> tuple[int, int]:
    if not isinstance(raw, dict):
        raise ProfileError("layout must be an object with rows and cols.")
    rows, cols = raw.get("rows"), raw.get("cols")
    ok = all(isinstance(v, int) and not isinstance(v, bool) for v in (rows, cols))
    if not ok or not (1 <= rows <= MAX_ROWS and 1 <= cols <= MAX_COLS):
        raise ProfileError(f"layout rows and cols must be whole numbers from 1 to {MAX_ROWS}.")
    return rows, cols


def clean_page(raw: Any, size: int) -> dict:
    if not isinstance(raw, dict):
        raise ProfileError("A page must be an object.")
    page_id, name = raw.get("id"), raw.get("name")
    if not isinstance(page_id, str) or not PAGE_ID.fullmatch(page_id):
        raise ProfileError("A page id may use lower-case letters, digits, - and _ (at most 31).")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 24:
        raise ProfileError("A page name must be 1 to 24 characters.")
    keys = raw.get("keys") or []
    if not isinstance(keys, list):
        raise ProfileError("A page's keys must be a list.")
    cleaned = [clean_key(k) for k in keys[:size]]
    cleaned += [None] * (size - len(cleaned))
    return {"id": page_id, "name": name.strip(), "keys": cleaned}


def clean_profile(raw: Any) -> dict:
    """Validate a whole profile (a file, an import); returns the normalised copy or raises ProfileError."""
    if not isinstance(raw, dict):
        raise ProfileError("A deck profile is a JSON object.")
    rows, cols = clean_layout(raw.get("layout"))
    pages_in = raw.get("pages")
    if not isinstance(pages_in, list) or not pages_in:
        raise ProfileError("A deck profile needs at least one page.")
    if len(pages_in) > MAX_PAGES:
        raise ProfileError(f"A deck profile may have at most {MAX_PAGES} pages.")
    pages = [clean_page(p, rows * cols) for p in pages_in]
    ids = [p["id"] for p in pages]
    if len(set(ids)) != len(ids):
        raise ProfileError("Two pages have the same id.")
    home = raw.get("home") if raw.get("home") in ids else ids[0]
    model = raw.get("model")
    model = (
        model if model in BY_ID and (BY_ID[model].rows, BY_ID[model].cols) == (rows, cols) else for_grid(rows, cols).id
    )
    return {
        "version": VERSION,
        "model": model,
        "layout": {"rows": rows, "cols": cols},
        "home": home,
        "settings": clean_settings(raw.get("settings")),
        "pages": pages,
    }


def page_ids(profile: dict) -> list[str]:
    return [p["id"] for p in profile["pages"]]


def get_page(profile: dict, page_id: str) -> dict:
    for page in profile["pages"]:
        if page["id"] == page_id:
            return page
    raise ProfileError(f"There is no page {page_id!r}.")


def new_page_id(profile: dict, base: str = "page") -> str:
    taken = set(page_ids(profile))
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:20] or "page"
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


# -- laying keys out -------------------------------------------------------------------------------------------------


def _chunks(entries: list[dict], cap: int, back: bool) -> list[tuple[list[dict], bool]]:
    """Split `entries` into pages of `cap` slots: (entries, continues). A page keeps its last slot for Back when `back`,
    and for a "More" link when more entries follow."""
    out: list[tuple[list[dict], bool]] = []
    rest = list(entries)
    while True:
        room = cap - (1 if back else 0)
        more = len(rest) > room
        if more:
            room -= 1
        out.append((rest[: max(room, 0)], more))
        rest = rest[max(room, 0) :]
        if not more:
            return out


def _flow(page_id: str, name: str, entries: list[dict], cap: int, home: bool) -> list[dict]:
    """Pages `page_id`, `page_id~2`, ... holding `entries`, with Back / More keys in their last slots."""
    pages = []
    chunks = _chunks(entries, cap, back=not home)
    for n, (chunk, more) in enumerate(chunks, 1):
        keys: list[dict | None] = list(chunk) + [None] * (cap - len(chunk))
        if more:
            keys[cap - (0 if home else 1) - 1] = key("deck.page", "More", "chevron", page=f"{page_id}~{n + 1}")
        if not home or n > 1:
            keys[cap - 1] = key("deck.back", "Back", "undo") if not home else keys[cap - 1]
        pid = page_id if n == 1 else f"{page_id}~{n}"
        pages.append({"id": pid, "name": name if n == 1 else f"{name} {n}", "keys": keys})
    return pages


def reflow(profile: dict, rows: int, cols: int) -> dict:
    """The same profile on a rows x cols grid. Never changes `profile`; the result is a valid profile."""
    old_rows, old_cols = profile["layout"]["rows"], profile["layout"]["cols"]
    out = copy.deepcopy(profile)
    out["layout"] = {"rows": rows, "cols": cols}
    out["model"] = for_grid(rows, cols).id
    if (old_rows, old_cols) == (rows, cols):
        return out
    cap = rows * cols
    pages: list[dict] = []
    if old_rows <= rows and old_cols <= cols:  # a bigger deck: every key keeps its row and column
        for page in profile["pages"]:
            keys: list[dict | None] = [None] * cap
            for i, k in enumerate(page["keys"]):
                keys[(i // old_cols) * cols + i % old_cols] = copy.deepcopy(k)
            pages.append({"id": page["id"], "name": page["name"], "keys": keys})
    else:  # a smaller (or differently shaped) one: flow each page's keys over as many pages as they need
        moved: dict[str, str] = {}
        for page in profile["pages"]:
            entries = [k for k in page["keys"] if k and k["action"] not in ("deck.back",)]
            flowed = _flow(page["id"], page["name"], copy.deepcopy(entries), cap, home=page["id"] == profile["home"])
            moved[page["id"]] = flowed[0]["id"]
            pages.extend(flowed)
        for page in pages:
            for k in page["keys"]:
                if (
                    k
                    and k["action"] == "deck.page"
                    and k["params"].get("page") in moved
                    and "~" not in k["params"]["page"]
                ):
                    k["params"]["page"] = moved[k["params"]["page"]]
    out["pages"] = pages[:MAX_PAGES]
    return clean_profile(out)


# -- the default profile ---------------------------------------------------------------------------------------------


def _scene_keys(ctx: dict) -> list[dict]:
    return [key("video.scene", name=s["name"]) for s in catalog.dig(ctx, "video.scenes") or [] if "name" in s]


def _voice_keys(ctx: dict) -> list[dict]:
    return [key("audio.preset", name=p["name"]) for p in catalog.dig(ctx, "audio.chain.presets") or [] if "name" in p]


def _light_preset_keys(ctx: dict) -> list[dict]:
    return [key("lighting.preset", name=p["name"]) for p in catalog.dig(ctx, "lighting.presets") or [] if "name" in p]


def default_profile(ctx: dict, model: Model = DEFAULT_MODEL) -> dict:
    """The profile a fresh install gets for this grid, built from the rig's scenes and presets.

    The first page holds the live controls (mute, privacy, record, stream, the first scenes and so on) and one folder
    key per group (Lights, Voice, Scenes, More); a 4x4 deck fits it exactly. Small decks keep only the first controls
    and reach the rest through the folders; a big deck simply has room to spare.
    """
    cap = model.keys
    scenes = _scene_keys(ctx)
    live = [
        key("audio.mute"), key("video.privacy"), key("video.record"), key("video.stream"),
        *scenes[:4],
        key("video.virtualcam"), key("video.blur"), key("auto.on"), key("lighting.power", "All lights", id="all"),
        key("lighting.brightness", id="all", step=-10), key("lighting.brightness", id="all", step=10),
    ]  # fmt: skip
    groups = [
        ("lights", "Lights", [
            key("lighting.power", id="all"), *_light_preset_keys(ctx),
            key("lighting.brightness", id="all", step=-10), key("lighting.brightness", id="all", step=10),
            key("lighting.temperature", id="all", step=-300), key("lighting.temperature", id="all", step=300),
            key("lighting.auto.follow"), key("lighting.auto.schedule"), key("lighting.auto.resume"),
        ]),
        ("voice", "Voice", [*_voice_keys(ctx), key("audio.bypass"), key("audio.mic_check")]),
        ("scenes", "Scenes", [*scenes, key("video.privacy")]),
        ("more", "More", [
            key("auto.once"), key("auto.reset"),
            *(key("framing.mode", mode=m["name"]) for m in catalog.dig(ctx, "framing.modes") or [] if "name" in m),
            key("studio.start"), key("health.refresh"), key("video.look.reset"),
        ]),
    ]  # fmt: skip
    folders = [
        key("deck.page", name, icon, page=gid)
        for gid, name, icon in (
            ("lights", "Lights", "lights"),
            ("voice", "Voice", "wave"),
            ("scenes", "Scenes", "scenes"),
            ("more", "More", "grid"),
        )
    ]
    room = max(cap - len(folders), 0)
    main_entries = live[:room] + folders if cap > len(folders) else [*live[: max(cap - 1, 0)], folders[3]]
    pages = _flow(HOME, "Live", main_entries, cap, home=True)
    for gid, name, entries in groups:
        pages.extend(_flow(gid, name, [e for e in entries if e], cap, home=False))
    return clean_profile(
        {"layout": {"rows": model.rows, "cols": model.cols}, "model": model.id, "home": HOME,
         "settings": DEFAULT_SETTINGS, "pages": pages}
    )  # fmt: skip


# -- the file --------------------------------------------------------------------------------------------------------


class Store:
    """The profile file: `load()` is None until the person saves something; saves are atomic and mode 600."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def file(self) -> Path:
        return self.path or config.user_env_file().parent / "deck.json"

    def load(self) -> dict | None:
        """The saved profile; None when there is none or the file is unreadable (the default takes over)."""
        with self._lock:
            try:
                raw = json.loads(self.file().read_text())
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                return None
        if not isinstance(raw, dict) or "pages" not in raw:
            return None  # settings only
        try:
            return clean_profile(raw)
        except ProfileError:
            return None

    def load_settings(self) -> dict | None:
        """The settings kept while the default profile is in use (or those inside the saved profile)."""
        try:
            raw = json.loads(self.file().read_text())
        except (OSError, ValueError):
            return None
        settings = raw.get("settings") if isinstance(raw, dict) else None
        return settings if isinstance(settings, dict) else None

    def save(self, profile: dict) -> None:
        self._write(clean_profile(profile))

    def save_settings(self, settings: dict) -> None:
        """Keep only the settings (brightness, sleep): no pages, so the default profile stays the default."""
        self._write({"version": VERSION, "settings": clean_settings(settings)})

    def _write(self, payload: dict) -> None:
        target = self.file()
        with self._lock:
            tmp = target.with_name(target.name + ".tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(json.dumps(payload, indent=2) + "\n")
                tmp.replace(target)
            except OSError as exc:
                raise ProfileError(f"Could not save {target}: {exc.strerror or exc}") from exc

    def delete(self) -> None:
        with self._lock:
            try:
                self.file().unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ProfileError(f"Could not remove {self.file()}: {exc.strerror or exc}") from exc

    def mtime(self) -> float:
        try:
            return self.file().stat().st_mtime
        except OSError:
            return 0.0
