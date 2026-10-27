"""The Stream Deck controller: the profile, the page you are on, and what each key shows and does.

The Console is the brain and `streamstack deck run` (deck/service.py) is a dumb terminal: the service opens the USB
device, draws the keys this controller says to draw (`state["surface"]`) and sends presses back as `deck.press`. So a
Console window can show and play the very same deck, and a deck pressed in the next room changes what the page shows.

The profile is edited from the Deck page (`deck.key_set`, `deck.page_add`, ...). Until it is edited there is no file and
the default profile, built from the rig's scenes and presets, is what the deck shows (`profile["pristine"]`).
"""

from __future__ import annotations

import copy
import threading
import time
import zlib
from collections.abc import Callable
from typing import Any

from streamstack.console.errors import ActionError
from streamstack.deck import catalog, profile, render
from streamstack.deck.models import BY_ID, DEFAULT_MODEL, MODELS, Model, for_device, for_grid

Notify = Callable[[str, str], None]
Clock = Callable[[], float]
DEVICE_FRESH_SECONDS = 15.0  # a service that has not reported for this long is gone
ARM_SECONDS = 3.0  # how long "Again to stop" waits for the second press
STACK_LIMIT = 12


def empty_state() -> dict:
    """The `deck` snapshot before there is a controller (and what the frontend starts from)."""
    return {
        "render": False,
        "device": {"connected": False, "source": None},
        "profile": {"layout": {"rows": 4, "cols": 4}, "pristine": True, "pages": [], "settings": {}},
        "surface": {"rows": 4, "cols": 4, "page": {"id": "", "name": ""}, "keys": [], "can_back": False},
        "models": [m.to_json() for m in MODELS],
        "palette_rev": "0",
    }


def _int(params: dict, name: str, low: int = 0, high: int = 1_000_000) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ActionError(f"{name} must be a whole number from {low} to {high}.", 400)
    return value


def _text(params: dict, name: str, limit: int = 64) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ActionError(f"{name} must be text of 1 to {limit} characters.", 400)
    return value.strip()


class DeckController:
    """Console actions `deck.*`; the snapshot section `deck`."""

    def __init__(self, store: profile.Store | None = None, clock: Clock = time.monotonic) -> None:
        self.store = store or profile.Store()
        self._clock = clock
        self.notify: Notify | None = None
        self.rig: Any = None  # the Rig sets this: presses run its actions
        self._lock = threading.RLock()
        self._saved: dict | None = self.store.load()
        try:
            self._settings = profile.clean_settings(self.store.load_settings())
        except profile.ProfileError:
            self._settings = dict(profile.DEFAULT_SETTINGS)
        self._pick: Model = DEFAULT_MODEL  # the model drawn while no deck is connected (and no profile is saved)
        self._device: dict | None = None
        self._device_seen = 0.0
        self._device_error: tuple[str, float] | None = None
        self._stack: list[str] = []
        self._armed: tuple[str, int, float] | None = None
        self._ctx: dict | None = None
        self._pages: dict[str, list[dict]] = {}  # editor profile views by page id (from the last state())
        self._surface: dict = {}
        self._png = render.Cache()
        self._palette: list[dict] = []

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Nothing runs in the background; the service and the page drive everything."""

    def stop(self) -> None:
        """Nothing to stop."""

    def _say(self, level: str, text: str) -> None:
        if self.notify:
            self.notify(level, text)

    # -- the grid and the profile -----------------------------------------------

    def _device_now(self) -> dict | None:
        if self._device and (
            self._device.get("source") == "demo" or self._clock() - self._device_seen < DEVICE_FRESH_SECONDS
        ):
            return self._device
        return None

    def _grid(self) -> Model:
        """The model the editor draws: the connected deck, else the saved profile's grid, else the person's pick."""
        device = self._device_now()
        if device:
            return for_device(device["name"], device["rows"], device["cols"], device["size"])
        if self._saved:
            return for_grid(self._saved["layout"]["rows"], self._saved["layout"]["cols"])
        return self._pick

    def _effective(self, ctx: dict) -> dict:
        """The profile being edited and shown: the saved one, or the default for the current grid."""
        if self._saved:
            return self._saved
        base = profile.default_profile(ctx, self._grid())
        base["settings"] = dict(self._settings)
        return base

    def _surface_profile(self, ctx: dict) -> tuple[dict, bool]:
        """(the profile the deck itself shows, whether it was re-flowed to fit the connected deck)."""
        effective = self._effective(ctx)
        device = self._device_now()
        if device and (device["rows"], device["cols"]) != (effective["layout"]["rows"], effective["layout"]["cols"]):
            return profile.reflow(effective, device["rows"], device["cols"]), True
        return effective, False

    def _context(self) -> dict:
        """A snapshot of the rig for resolving presses and edits; the controller's own copy while it is fresh enough."""
        if self.rig is not None:
            return self.rig.snapshot()
        return self._ctx or {}

    def _with_deck(self, ctx: dict, prof: dict) -> dict:
        """`ctx` plus the `deck` facts the catalog reads (page list for folder keys, the settings)."""
        pages = [{"id": p["id"], "name": p["name"]} for p in prof["pages"]]
        return {**ctx, "deck": {"pages": pages, "settings": dict(prof["settings"])}}

    def _current_page(self, surface: dict) -> dict:
        ids = {p["id"]: p for p in surface["pages"]}
        for page_id in reversed(self._stack):
            if page_id in ids:
                return ids[page_id]
        return ids[surface["home"]]

    # -- state ----------------------------------------------------------------

    def _armed_index(self, page_id: str) -> int | None:
        if self._armed and self._armed[0] == page_id and self._armed[2] > self._clock():
            return self._armed[1]
        return None

    def state(self, snapshot: dict) -> dict:
        """The `deck` section of the snapshot; `snapshot` is the rest of it (the rig's state, for lit keys)."""
        with self._lock:
            self._ctx = snapshot
            effective = self._effective(snapshot)
            ctx = self._with_deck(snapshot, effective)
            views = {p["id"]: [catalog.view_of(k, ctx, i) for i, k in enumerate(p["keys"])] for p in effective["pages"]}
            for page in effective["pages"]:
                for view, key in zip(views[page["id"]], page["keys"], strict=True):
                    if key:
                        view["raw"] = {k: key[k] for k in ("action", "params", "label", "icon") if k in key}
            self._pages = views
            surface, fitted = self._surface_profile(snapshot)
            sctx = self._with_deck(snapshot, surface)
            page = self._current_page(surface)
            armed = self._armed_index(page["id"])
            skeys = [catalog.view_of(k, sctx, i, armed == i) for i, k in enumerate(page["keys"])]
            self._surface = {"page": page["id"], "views": skeys}
            self._palette = catalog.palette(ctx)
            device = self._device_now()
            error = (
                self._device_error[0]
                if self._device_error and self._clock() - self._device_error[1] < DEVICE_FRESH_SECONDS
                else None
            )
            return {
                "render": render.available(),
                "device": self._device_json(device, error),
                "profile": {
                    "layout": effective["layout"],
                    "model": effective["model"],
                    "home": effective["home"],
                    "settings": effective["settings"],
                    "pristine": self._saved is None,
                    "mismatch": fitted,
                    "pages": [{"id": p["id"], "name": p["name"], "keys": views[p["id"]]} for p in effective["pages"]],
                },
                "surface": {
                    "rows": surface["layout"]["rows"],
                    "cols": surface["layout"]["cols"],
                    "page": {"id": page["id"], "name": page["name"]},
                    "keys": skeys,
                    "can_back": bool(self._stack),
                    "fitted": fitted,
                },
                "models": [m.to_json() for m in MODELS],
                "palette_rev": format(zlib.crc32(repr([e["id"] for e in self._palette]).encode()), "08x"),
            }

    @staticmethod
    def _device_json(device: dict | None, error: str | None) -> dict:
        if not device:
            return {"connected": False, "source": None, **({"error": error} if error else {})}
        return {
            "connected": True,
            "source": device.get("source", "usb"),
            "model": device["model"],
            "name": device["name"],
            "serial": device.get("serial", ""),
            "rows": device["rows"],
            "cols": device["cols"],
            "size": device["size"],
        }

    def palette(self) -> list[dict]:
        """Every key that can be assigned, from the rig as it is now."""
        with self._lock:
            ctx = self._context()
            return catalog.palette(self._with_deck(ctx, self._effective(ctx)))

    def export(self) -> dict:
        with self._lock:
            return profile.clean_profile(self._effective(self._context()))

    # -- pictures -----------------------------------------------------------------

    def key_png(self, page: str | None, index: int, size: int) -> bytes:
        """The picture of key `index` of the editor page `page` (or of the page the deck is on, for None)."""
        if not render.available():
            raise ActionError("Pillow is not installed on this machine (pip install 'streamstack[deck]').", 503)
        with self._lock:
            views = self._surface.get("views") if page is None else self._pages.get(page)
            if not views and self.rig is not None:
                self.rig.snapshot()  # fills the caches
                views = self._surface.get("views") if page is None else self._pages.get(page)
            if not views or not 0 <= index < len(views):
                raise ActionError("There is no such key.", 404)
            view = views[index]
        return self._png.get((view["rev"], size), lambda: render.png(view, size))

    # -- actions --------------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        handler = {
            "deck.press": self._press,
            "deck.page": self._page,
            "deck.goto": self._goto,
            "deck.back": self._back,
            "deck.home": self._home,
            "deck.key_set": self._key_set,
            "deck.key_clear": self._key_clear,
            "deck.key_move": self._key_move,
            "deck.page_add": self._page_add,
            "deck.page_rename": self._page_rename,
            "deck.page_delete": self._page_delete,
            "deck.page_move": self._page_move,
            "deck.settings": self._set_settings,
            "deck.layout": self._layout,
            "deck.import": self._import,
            "deck.reset": self._reset,
            "deck.attach": self._attach,
            "deck.detach": self._detach,
        }.get(action)
        if handler is None:
            raise ActionError(f"Unknown action {action!r}.", 400)
        with self._lock:
            try:
                return handler(params)
            except profile.ProfileError as exc:
                raise ActionError(str(exc), 400) from exc

    # navigation

    def _surface_now(self) -> tuple[dict, dict]:
        ctx = self._context()
        surface, _ = self._surface_profile(ctx)
        return surface, ctx

    def _page(self, params: dict) -> str | None:
        surface, _ = self._surface_now()
        target = _text(params, "page", 40)
        if target not in {p["id"] for p in surface["pages"]}:
            raise ActionError(f"There is no page {target!r}.", 404)
        self._stack = [*self._stack, target][-STACK_LIMIT:]
        return None

    def _goto(self, params: dict) -> str | None:
        surface, _ = self._surface_now()
        target = _text(params, "page", 40)
        if target not in {p["id"] for p in surface["pages"]}:
            raise ActionError(f"There is no page {target!r}.", 404)
        self._stack = [] if target == surface["home"] else [target]
        return None

    def _back(self, params: dict) -> str | None:
        self._stack = self._stack[:-1]
        return None

    def _home(self, params: dict) -> str | None:
        self._stack = []
        return None

    # pressing

    def _press(self, params: dict) -> str | None:
        index = _int(params, "key", 0, 255)
        ctx = self._context()
        surface, _ = self._surface_profile(ctx)
        page = self._current_page(surface)
        if index >= len(page["keys"]) or not page["keys"][index]:
            return None  # an empty key does nothing, quietly
        sctx = self._with_deck(ctx, surface)
        key = page["keys"][index]
        try:
            action, args, confirm = catalog.resolve_press(key, sctx)
        except catalog.PressError as exc:
            self._say("warn", str(exc))
            raise ActionError(str(exc), 409) from exc
        if confirm and self._armed_index(page["id"]) != index:
            self._armed = (page["id"], index, self._clock() + ARM_SECONDS)
            return "Press again to confirm."
        self._armed = None
        try:
            if action.startswith("deck."):
                return self.act(action, args)
            return self.rig.act(action, args)
        except ActionError as exc:
            self._say("warn", exc.message)
            raise

    # editing

    def _editable(self) -> dict:
        """A copy of the profile to change: the saved one, or the default made real (a first edit saves it)."""
        return copy.deepcopy(self._effective(self._context()))

    def _commit(self, prof: dict) -> None:
        cleaned = profile.clean_profile(prof)
        self.store.save(cleaned)
        self._saved = cleaned
        self._settings = dict(cleaned["settings"])
        self._armed = None
        self._stack = [p for p in self._stack if p in {x["id"] for x in cleaned["pages"]}]

    def _index(self, prof: dict, params: dict, name: str = "key") -> tuple[dict, int]:
        page = profile.get_page(prof, _text(params, "page", 40))
        return page, _int(params, name, 0, len(page["keys"]) - 1)

    def _key_set(self, params: dict) -> str | None:
        prof = self._editable()
        page, index = self._index(prof, params)
        page["keys"][index] = profile.clean_key(params.get("spec"))
        self._commit(prof)
        return None

    def _key_clear(self, params: dict) -> str | None:
        prof = self._editable()
        page, index = self._index(prof, params)
        page["keys"][index] = None
        self._commit(prof)
        return None

    def _key_move(self, params: dict) -> str | None:
        """Swap two keys (so a drop onto a filled key never loses one); `to_page` defaults to `page`."""
        prof = self._editable()
        src, a = self._index(prof, params)
        dst = profile.get_page(prof, params.get("to_page") or params["page"])
        b = _int(params, "to", 0, len(dst["keys"]) - 1)
        src["keys"][a], dst["keys"][b] = dst["keys"][b], src["keys"][a]
        self._commit(prof)
        return None

    def _page_add(self, params: dict) -> str | None:
        prof = self._editable()
        if len(prof["pages"]) >= profile.MAX_PAGES:
            raise ActionError(f"A deck profile may have at most {profile.MAX_PAGES} pages.", 400)
        name = _text(params, "name", 24)
        cap = prof["layout"]["rows"] * prof["layout"]["cols"]
        page_id = profile.new_page_id(prof, name)
        prof["pages"].append({"id": page_id, "name": name, "keys": [None] * cap})
        self._commit(prof)
        return f"Added page {name}"

    def _page_rename(self, params: dict) -> str | None:
        prof = self._editable()
        profile.get_page(prof, _text(params, "page", 40))["name"] = _text(params, "name", 24)
        self._commit(prof)
        return None

    def _page_delete(self, params: dict) -> str | None:
        prof = self._editable()
        target = _text(params, "page", 40)
        profile.get_page(prof, target)
        if len(prof["pages"]) == 1:
            raise ActionError("A deck needs at least one page.", 409)
        prof["pages"] = [p for p in prof["pages"] if p["id"] != target]
        if prof["home"] == target:
            prof["home"] = prof["pages"][0]["id"]
        for page in prof["pages"]:  # a folder key that opened the page would now do nothing
            page["keys"] = [
                None if k and k["action"] == "deck.page" and k["params"].get("page") == target else k
                for k in page["keys"]
            ]
        self._commit(prof)
        return None

    def _page_move(self, params: dict) -> str | None:
        prof = self._editable()
        page = profile.get_page(prof, _text(params, "page", 40))
        to = _int(params, "to", 0, len(prof["pages"]) - 1)
        prof["pages"].remove(page)
        prof["pages"].insert(to, page)
        self._commit(prof)
        return None

    def _set_settings(self, params: dict) -> str | None:
        current = self._effective(self._context())["settings"]
        change = {k: params[k] for k in ("brightness", "sleep_minutes") if k in params}
        if "step" in params:
            step = params["step"]
            if isinstance(step, bool) or not isinstance(step, (int, float)):
                raise ActionError("step must be a number.", 400)
            change["brightness"] = max(5, min(100, round(current["brightness"] + step)))
        settings = profile.clean_settings(change, current)
        if self._saved:
            prof = self._editable()
            prof["settings"] = settings
            self._commit(prof)
        else:  # the default profile stays the default; only the settings are kept
            self.store.save_settings(settings)
            self._settings = settings
        return None

    def _layout(self, params: dict) -> str | None:
        """Lay the profile out for another deck. With no profile saved yet this is only the model to draw."""
        model = BY_ID.get(str(params.get("model") or ""))
        if model is None:
            rows, cols = _int(params, "rows", 1, profile.MAX_ROWS), _int(params, "cols", 1, profile.MAX_COLS)
            model = for_grid(rows, cols)
        if self._saved:
            self._commit(profile.reflow(self._saved, model.rows, model.cols))
        elif not self._device_now():
            self._pick = model
        self._stack = []
        return f"Laid out for {model.name}"

    def _import(self, params: dict) -> str | None:
        incoming = profile.clean_profile(params.get("profile"))
        self._commit(incoming)
        self._stack = []
        return "Deck profile imported"

    def _reset(self, params: dict) -> str | None:
        self.store.delete()
        self._saved = None
        self._settings = dict(profile.DEFAULT_SETTINGS)
        self._stack = []
        self._armed = None
        return "Deck reset to the default profile"

    # the device

    def _attach(self, params: dict) -> str | None:
        """The deck service reports in (every few seconds) with the device it holds, or why it cannot open one."""
        error = params.get("error")
        if error is not None:
            if not isinstance(error, str) or len(error) > 300:
                raise ActionError("error must be short text.", 400)
            self._device, self._device_error = None, (error, self._clock())
            return None
        rows, cols = _int(params, "rows", 1, profile.MAX_ROWS), _int(params, "cols", 1, profile.MAX_COLS)
        size = _int(params, "size", 16, 512)
        name = str(params.get("name") or "Stream Deck")[:60]
        model = for_device(name, rows, cols, size)
        serial = str(params.get("serial") or "")[:40]
        self._device = {
            "source": "usb",
            "model": model.id,
            "name": name,
            "serial": serial,
            "rows": rows,
            "cols": cols,
            "size": size,
        }
        self._device_seen, self._device_error = self._clock(), None
        return None

    def _detach(self, params: dict) -> str | None:
        self._device, self._device_error = None, None
        return None


class MemoryStore(profile.Store):
    """A profile store that never touches the disk (the demo rig, tests)."""

    def __init__(self) -> None:
        super().__init__()
        self._profile: dict | None = None
        self._settings: dict | None = None

    def load(self) -> dict | None:
        return self._profile

    def load_settings(self) -> dict | None:
        return self._settings

    def save(self, prof: dict) -> None:
        self._profile = profile.clean_profile(prof)

    def save_settings(self, settings: dict) -> None:
        self._settings = dict(settings)

    def delete(self) -> None:
        self._profile = self._settings = None


class DemoDeck(DeckController):
    """The demo rig's deck: a simulated device on the desk, an in-memory profile, nothing on disk.

    The simulated deck is a 4x4 "Virtual deck" until `deck.attach {"model": id}` swaps it for another model (the e2e
    suite does, to see a profile laid out for one deck on another) and `deck.detach` unplugs it.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        super().__init__(MemoryStore(), clock)
        self._plug(DEFAULT_MODEL)

    def _plug(self, model: Model) -> None:
        self._device = {
            "source": "demo", "model": model.id, "name": model.name, "serial": "DEMO0001",
            "rows": model.rows, "cols": model.cols, "size": model.size,
        }  # fmt: skip

    def _attach(self, params: dict) -> str | None:
        model = BY_ID.get(str(params.get("model") or ""))
        if model is None:
            raise ActionError("The demo deck can be any of: " + ", ".join(BY_ID) + ".", 400)
        self._plug(model)
        self._stack = []
        return f"Plugged in a {model.name}"

    def _reset(self, params: dict) -> str | None:
        message = super()._reset(params)
        self._plug(DEFAULT_MODEL)
        self._pick = DEFAULT_MODEL
        return message
