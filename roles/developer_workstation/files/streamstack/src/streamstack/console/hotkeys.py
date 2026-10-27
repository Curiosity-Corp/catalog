"""Global hotkeys and MIDI controls: bindings from a key combo or a MIDI message to any Console action.

The same actions the Stream Deck keys run (deck/catalog.py): a binding is "Ctrl+Alt+M -> audio.mute" or "CC 7 -> the
voice level", pressed through `catalog.resolve_press` so toggles flip, steps step and destructive ones ask twice.
Press the trigger again within ARM_SECONDS to confirm, exactly like a deck key.

Where keys come from depends on the session (see hotkeys_x11.py and hotkeys_portal.py):

* X11: key grabs on the root window, so a combo works while a call window has focus;
* Wayland: the XDG GlobalShortcuts portal (needs the `shortcuts` extra and a desktop that has the portal);
* neither: the page says why, and MIDI still works.

MIDI comes from the raw ALSA ports (hotkeys_midi.py) with hot-plug, and a learn mode captures the next message so a
person never types a note number.

Console actions `hotkeys.*`: add, update, remove, clear, import, enable, learn, fire. Snapshot section `hotkeys`
(docs/hotkeys.md).
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from streamstack import hotkeys
from streamstack.console.errors import ActionError
from streamstack.deck import catalog

Notify = Callable[[str, str], None]
Clock = Callable[[], float]
ARM_SECONDS = 3.0  # how long "again to confirm" waits for the second press
DEBOUNCE_SECONDS = 0.3  # a held key repeats; one press runs once
LOG_SCALED_SECONDS = 1.0  # a fader sweep is one log line, not hundreds
EVENT_LIMIT = 20

STATUS_TEXT = {
    "ok": "Listening",
    "pending": "Waiting for the desktop to confirm this shortcut",
    "conflict": "Another program already uses this key combination",
    "unmapped": "This keyboard has no such key",
    "refused": "The desktop did not accept this shortcut",
    "unavailable": "Keys cannot be listened for here",
    "waiting": "No MIDI controller is connected yet",
    "off": "Hotkeys are switched off",
}


class KeyBackend(Protocol):
    name: str

    def start(self) -> str | None: ...
    def stop(self) -> None: ...
    def set_combos(self, combos: list[str], timeout: float = ...) -> dict[str, str]: ...


class MidiBackend(Protocol):
    reason: str | None

    def start(self) -> str | None: ...
    def stop(self) -> None: ...
    def devices(self) -> list[dict]: ...


KeyFactory = Callable[[Callable[[str], None], Callable[[dict[str, str]], None]], KeyBackend | None]
MidiFactory = Callable[[Callable[[str, hotkeys.MidiEvent], None], Callable[[list[dict]], None]], MidiBackend]


def session_kind(env: dict[str, str] | None = None) -> str:
    """ "wayland", "x11" or "none": which kind of desktop session this process runs in."""
    env = os.environ if env is None else env
    if env.get("XDG_SESSION_TYPE", "").lower() == "wayland" or env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


def default_key_backend(
    on_combo: Callable[[str], None], on_status: Callable[[dict[str, str]], None]
) -> KeyBackend | None:
    """The key backend for this session, or None (the reason is found by `key_reason`)."""
    kind = session_kind()
    if kind == "wayland":
        from streamstack import hotkeys_portal

        return hotkeys_portal.PortalBackend(on_combo, on_status)
    if kind == "x11":
        from streamstack import hotkeys_x11

        return hotkeys_x11.X11Backend(on_combo, on_status)
    return None


def default_midi(
    on_event: Callable[[str, hotkeys.MidiEvent], None], on_change: Callable[[list[dict]], None]
) -> MidiBackend:
    from streamstack import hotkeys_midi

    return hotkeys_midi.MidiSource(on_event, on_change)


def empty_state() -> dict:
    """The `hotkeys` snapshot before there is a controller (and what the frontend starts from)."""
    return {
        "available": False,
        "enabled": True,
        "session": "none",
        "keys": {"backend": "none", "ok": False, "reason": "Hotkeys are not part of this rig."},
        "midi": {"ok": False, "reason": "Hotkeys are not part of this rig.", "devices": [], "last": None},
        "bindings": [],
        "learn": {"active": False, "event": None},
        "events": [],
        "limits": {"bindings": hotkeys.MAX_BINDINGS},
    }


class HotkeysController:
    """Console actions `hotkeys.*`; the snapshot section `hotkeys`."""

    def __init__(
        self,
        store: hotkeys.Store | hotkeys.MemoryStore | None = None,
        key_factory: KeyFactory = default_key_backend,
        midi_factory: MidiFactory = default_midi,
        clock: Clock = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or hotkeys.Store()
        self._key_factory = key_factory
        self._midi_factory = midi_factory
        self._clock = clock
        self._wall = wall
        self.notify: Notify | None = None
        self.rig: Any = None  # the Rig sets this: a trigger runs its actions
        self._lock = threading.RLock()
        self._enabled, self._bindings = self.store.load()
        self._keys: KeyBackend | None = None
        self._keys_reason: str | None = None
        self._key_status: dict[str, str] = {}
        self._midi: MidiBackend | None = None
        self._midi_reason: str | None = None
        self._matcher = hotkeys.Matcher()
        self._learn_active = False
        self._learn_event: dict | None = None
        self._last_midi: dict | None = None
        self._last_fire: dict[str, float] = {}
        self._last_extra: dict[str, dict] = {}
        self._last_log: dict[str, float] = {}
        self._last_result: dict[str, dict] = {}
        self._armed: tuple[str, float] | None = None
        self._events: deque[dict] = deque(maxlen=EVENT_LIMIT)
        self._started = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Open the key backend and the MIDI ports. A machine without them is not an error; the page says why."""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._open_keys()
        self._open_midi()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            keys, midi = self._keys, self._midi
            self._keys = self._midi = None
        for backend in (keys, midi):
            if backend is not None:
                backend.stop()

    def _open_keys(self) -> None:
        try:
            backend = self._key_factory(self._on_combo, self._on_key_status)
        except ImportError as exc:
            backend, self._keys_reason = None, f"The shortcuts support is not installed: {exc}"
        if backend is None:
            if self._keys_reason is None:
                self._keys_reason = "This session has neither X11 nor a desktop shortcut portal."
            return
        reason = backend.start()
        if reason:
            self._keys_reason = reason
            backend.stop()
            return
        with self._lock:
            self._keys = backend
            self._keys_reason = None
        self._sync_keys()

    def _open_midi(self) -> None:
        midi = self._midi_factory(self._on_midi, self._on_midi_change)
        reason = midi.start()
        with self._lock:
            self._midi_reason = reason
            self._midi = None if reason else midi

    def _say(self, level: str, text: str) -> None:
        if self.notify:
            self.notify(level, text)

    def _log(self, level: str, text: str) -> None:
        with self._lock:
            self._events.appendleft(
                {"time": time.strftime("%H:%M:%S", time.localtime(self._wall())), "level": level, "text": text}
            )

    # -- the keys backend ----------------------------------------------------------

    def _combos(self) -> list[str]:
        return [b["trigger"]["combo"] for b in self._bindings if b["trigger"]["kind"] == "key"]

    def _sync_keys(self) -> None:
        """Tell the backend which combos to hold (none while hotkeys are off) and remember what it says."""
        with self._lock:
            backend = self._keys
            combos = self._combos() if self._enabled else []
        if backend is None:
            return
        status = backend.set_combos(combos)
        with self._lock:
            self._key_status = dict(status)

    def _on_key_status(self, status: dict[str, str]) -> None:
        with self._lock:
            self._key_status.update(status)
        for combo, value in status.items():
            if value in ("conflict", "refused", "unmapped"):
                self._log("warn", f"{hotkeys.describe_combo(combo)}: {STATUS_TEXT[value]}.")

    def _on_combo(self, combo: str) -> None:
        with self._lock:
            ids = [b["id"] for b in self._bindings if b["trigger"] == {"kind": "key", "combo": combo}]
        for ident in ids:
            self._trigger(ident, {})

    # -- MIDI ----------------------------------------------------------------------------

    def _on_midi_change(self, devices: list[dict]) -> None:
        names = ", ".join(d["name"] for d in devices) or "none"
        self._log("info", f"MIDI controllers: {names}.")

    def _on_midi(self, port: str, event: hotkeys.MidiEvent) -> None:
        with self._lock:
            self._last_midi = {"text": event.label(), "value": event.value, "time": self._wall()}
            if self._learn_active:
                self._learn_active = False
                self._learn_event = {
                    "type": event.kind, "channel": event.channel, "number": event.number, "text": event.label(),
                }  # fmt: skip
                learned = True
            else:
                learned = False
                matches = self._matcher.fire(self._bindings, event) if self._enabled else []
        if learned:
            self._log("info", f"Learned {event.label()}.")
            return
        for binding, extra in matches:
            self._trigger(binding["id"], extra)

    # -- running a binding -----------------------------------------------------------------

    def _trigger(self, ident: str, extra: dict) -> str | None:
        """Run one binding like a deck key; returns the message (for `hotkeys.fire`)."""
        now = self._clock()
        with self._lock:
            binding = next((b for b in self._bindings if b["id"] == ident), None)
            if binding is None:
                return None
            scaled = "scale" in binding
            if scaled:
                if self._last_extra.get(ident) == extra:
                    return None
                self._last_extra[ident] = dict(extra)
            elif now - self._last_fire.get(ident, float("-inf")) < DEBOUNCE_SECONDS:
                return None
            self._last_fire[ident] = now
        name = binding.get("label") or binding["action"]
        where = hotkeys.trigger_text(binding["trigger"])
        key = {"action": binding["action"], "params": {**binding["params"], **extra}}
        try:
            ctx = self.rig.snapshot() if self.rig is not None else {}
            action, args, confirm = catalog.resolve_press(key, ctx)
        except catalog.PressError as exc:
            self._fail(ident, f"{where}: {exc}")
            return None
        if confirm:
            with self._lock:
                armed = self._armed
                again = armed is not None and armed[0] == ident and armed[1] > now
                self._armed = None if again else (ident, now + ARM_SECONDS)
            if not again:
                text = f"{name}: press {where} again to confirm."
                self._log("info", text)
                self._say("warn", text)
                return "Press again to confirm."
        else:
            with self._lock:
                self._armed = None
        try:
            message = self.rig.act(action, args) if self.rig is not None else None
        except ActionError as exc:
            self._fail(ident, f"{name}: {exc.message}")
            return None
        with self._lock:
            self._last_result[ident] = {"ok": True, "time": self._wall(), "text": message or "Done"}
            quiet = scaled and now - self._last_log.get(ident, float("-inf")) < LOG_SCALED_SECONDS
            if not quiet:
                self._last_log[ident] = now
        if not quiet:
            self._log("ok", f"{where}: {name}" + (f" ({message})" if message else ""))
        return message

    def _fail(self, ident: str, text: str) -> None:
        with self._lock:
            self._last_result[ident] = {"ok": False, "time": self._wall(), "text": text}
        self._log("warn", text)
        self._say("warn", text)

    # -- state ------------------------------------------------------------------------------

    def _binding_status(self, binding: dict) -> tuple[str, str]:
        if not self._enabled:
            return "off", STATUS_TEXT["off"]
        trig = binding["trigger"]
        if trig["kind"] == "key":
            if self._keys is None:
                return "unavailable", self._keys_reason or STATUS_TEXT["unavailable"]
            status = self._key_status.get(trig["combo"], "pending")
            return status, STATUS_TEXT.get(status, status)
        if self._midi is None:
            return "unavailable", self._midi_reason or "MIDI cannot be read here."
        if not any(d["status"] == "ok" for d in self._midi.devices()):
            return "waiting", STATUS_TEXT["waiting"]
        return "ok", STATUS_TEXT["ok"]

    def state(self, snap: dict | None = None) -> dict:
        with self._lock:
            bindings = []
            for b in self._bindings:
                status, detail = self._binding_status(b)
                view = copy.deepcopy(b)
                view.update(
                    trigger_text=hotkeys.trigger_text(b["trigger"]), status=status, detail=detail,
                    last=self._last_result.get(b["id"]),
                )  # fmt: skip
                bindings.append(view)
            devices = self._midi.devices() if self._midi is not None else []
            midi_ok = self._midi is not None
            last = dict(self._last_midi) if self._last_midi else None
            keys = {
                "backend": self._keys.name if self._keys is not None else "none",
                "ok": self._keys is not None,
                "reason": None if self._keys is not None else self._keys_reason,
            }
            return {
                "available": self._keys is not None or midi_ok,
                "enabled": self._enabled,
                "session": session_kind(),
                "keys": keys,
                "midi": {
                    "ok": midi_ok,
                    "reason": None if midi_ok else self._midi_reason,
                    "devices": devices,
                    "last": last,
                },
                "bindings": bindings,
                "learn": {"active": self._learn_active, "event": copy.deepcopy(self._learn_event)},
                "events": list(self._events),
                "limits": {"bindings": hotkeys.MAX_BINDINGS},
            }

    def export(self) -> dict:
        with self._lock:
            return hotkeys.export_document(self._enabled, copy.deepcopy(self._bindings))

    # -- actions ----------------------------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        handler = {
            "hotkeys.add": self._add,
            "hotkeys.update": self._update,
            "hotkeys.remove": self._remove,
            "hotkeys.clear": self._clear,
            "hotkeys.import": self._import,
            "hotkeys.enable": self._enable,
            "hotkeys.learn": self._learn,
            "hotkeys.fire": self._fire,
        }.get(action)
        try:
            return handler(params) if handler is not None else self._extra(action, params)
        except hotkeys.HotkeyError as exc:
            raise ActionError(str(exc), 400) from exc

    def _extra(self, action: str, params: dict) -> str | None:
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _commit(self, bindings: list[dict], enabled: bool | None = None) -> None:
        cleaned = hotkeys.clean_bindings(bindings)
        on = self._enabled if enabled is None else enabled
        try:
            self.store.save(on, cleaned)
        except OSError as exc:
            raise ActionError(f"The hotkeys could not be saved: {exc}", 500) from exc
        with self._lock:
            self._bindings, self._enabled = cleaned, on
            live = {b["id"] for b in cleaned}
            for table in (self._last_fire, self._last_extra, self._last_log, self._last_result):
                for gone in [k for k in table if k not in live]:
                    del table[gone]
        self._sync_keys()

    @staticmethod
    def _find(bindings: list[dict], ident: object) -> dict:
        found = next((b for b in bindings if b["id"] == ident), None)
        if found is None:
            raise ActionError(f"There is no binding {ident!r}.", 404)
        return found

    @staticmethod
    def _fields(params: dict) -> dict:
        """The binding fields of a request. Over HTTP the envelope's own "action" key is taken by the action name, so
        a binding travels nested as {"binding": {"trigger", "action", "params", "label", "scale"}}."""
        nested = params.get("binding")
        if nested is None:
            return params
        if not isinstance(nested, dict):
            raise ActionError("binding must be an object.", 400)
        return {**nested, **({"id": params["id"]} if "id" in params else {})}

    def _add(self, params: dict) -> str | None:
        params = self._fields(params)
        with self._lock:
            bindings = copy.deepcopy(self._bindings)
        ident = params.get("id") or hotkeys.new_id({b["id"] for b in bindings})
        raw = {k: params.get(k) for k in ("trigger", "action", "params", "label", "scale") if k in params}
        bindings.append({"id": ident, **raw})
        self._commit(bindings)
        return "Added"

    def _update(self, params: dict) -> str | None:
        params = self._fields(params)
        with self._lock:
            bindings = copy.deepcopy(self._bindings)
        target = self._find(bindings, params.get("id"))
        for field in ("trigger", "action", "params", "label", "scale"):
            if field in params:
                if params[field] is None:
                    target.pop(field, None)
                else:
                    target[field] = params[field]
        if "trigger" in params and "scale" not in params:
            target.pop("scale", None)  # a scale belongs to a controller; a new trigger starts without one
        self._commit(bindings)
        return "Saved"

    def _remove(self, params: dict) -> str | None:
        with self._lock:
            bindings = copy.deepcopy(self._bindings)
        target = self._find(bindings, params.get("id"))
        self._commit([b for b in bindings if b is not target])
        return "Removed"

    def _clear(self, params: dict) -> str | None:
        self._commit([])
        return "Cleared"

    def _import(self, params: dict) -> str | None:
        enabled, bindings = hotkeys.import_document(params.get("document"))
        self._commit(bindings, enabled)
        return f"Imported {len(bindings)} bindings"

    def _enable(self, params: dict) -> str | None:
        on = params.get("on")
        if not isinstance(on, bool):
            raise ActionError("on must be true or false.", 400)
        with self._lock:
            bindings = copy.deepcopy(self._bindings)
        self._commit(bindings, on)
        return "Hotkeys on" if on else "Hotkeys off"

    def _learn(self, params: dict) -> str | None:
        on = params.get("on", True)
        if not isinstance(on, bool):
            raise ActionError("on must be true or false.", 400)
        with self._lock:
            if on and self._midi is None:
                raise ActionError(self._midi_reason or "MIDI cannot be read here.", 409)
            self._learn_active = on
            self._learn_event = None
        return "Move a control on your MIDI device." if on else None

    def _fire(self, params: dict) -> str | None:
        ident = params.get("id")
        with self._lock:
            self._find(self._bindings, ident)
            self._last_fire.pop(str(ident), None)
        return self._trigger(str(ident), {})


# -- the demo rig ----------------------------------------------------------------------------------------------------


class FakeKeys:
    """A key backend that holds combos in memory and is pressed from `hotkeys.demo.key`."""

    name = "demo"

    def __init__(self, on_combo: Callable[[str], None], on_status: Callable[[dict[str, str]], None]) -> None:
        self._on_combo = on_combo
        self._on_status = on_status
        self.held: list[str] = []
        self.conflicts: set[str] = set()

    def start(self) -> str | None:
        return None

    def stop(self) -> None:
        self.held = []

    def set_combos(self, combos: list[str], timeout: float = 0.0) -> dict[str, str]:
        self.held = [c for c in combos if c not in self.conflicts]
        return {c: ("conflict" if c in self.conflicts else "ok") for c in combos}

    def press(self, combo: str) -> None:
        if combo in self.held:
            self._on_combo(combo)


class FakeMidi:
    """A MIDI source with one pad controller; `hotkeys.demo.midi` plays messages from it."""

    reason: str | None = None

    def __init__(
        self, on_event: Callable[[str, hotkeys.MidiEvent], None], on_change: Callable[[list[dict]], None]
    ) -> None:
        self._on_event = on_event
        self.plugged = True

    def start(self) -> str | None:
        return None

    def stop(self) -> None:
        return None

    def devices(self) -> list[dict]:
        return [{"id": "midiC1D0", "name": "Demo pad", "status": "ok"}] if self.plugged else []

    def play(self, event: hotkeys.MidiEvent) -> None:
        if self.plugged:
            self._on_event("midiC1D0", event)


class DemoHotkeys(HotkeysController):
    """The demo rig's hotkeys: an in-memory file, a fake key backend and a fake MIDI pad (`hotkeys.demo.*`)."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.fake_keys: FakeKeys | None = None
        self.fake_midi: FakeMidi | None = None
        super().__init__(hotkeys.MemoryStore(), self._keys_for_demo, self._midi_for_demo, clock)

    def _keys_for_demo(
        self, on_combo: Callable[[str], None], on_status: Callable[[dict[str, str]], None]
    ) -> KeyBackend:
        self.fake_keys = FakeKeys(on_combo, on_status)
        return self.fake_keys

    def _midi_for_demo(
        self, on_event: Callable[[str, hotkeys.MidiEvent], None], on_change: Callable[[list[dict]], None]
    ) -> MidiBackend:
        self.fake_midi = FakeMidi(on_event, on_change)
        return self.fake_midi

    def _extra(self, action: str, params: dict) -> str | None:
        if action == "hotkeys.demo.key":
            combo = hotkeys.clean_combo(params.get("combo"))
            if self.fake_keys is None:
                raise ActionError("The demo keyboard is not running.", 409)
            self.fake_keys.press(combo)
            return None
        if action == "hotkeys.demo.midi":
            kind, channel, number = params.get("type"), params.get("channel", 1), params.get("number")
            value = params.get("value", 127)
            if kind not in hotkeys.MIDI_KINDS or not isinstance(channel, int) or not isinstance(number, int):
                raise ActionError("type, channel and number are required.", 400)
            if not isinstance(value, int) or not 0 <= value <= 127:
                raise ActionError("value is 0 to 127.", 400)
            if self.fake_midi is None:
                raise ActionError("The demo pad is not running.", 409)
            self.fake_midi.play(hotkeys.MidiEvent(kind, channel, number, value))
            return None
        if action == "hotkeys.demo.conflict":
            combo = hotkeys.clean_combo(params.get("combo"))
            if self.fake_keys is None:
                raise ActionError("The demo keyboard is not running.", 409)
            (self.fake_keys.conflicts.add if params.get("on", True) else self.fake_keys.conflicts.discard)(combo)
            self._sync_keys()
            return None
        if action == "hotkeys.demo.reset":
            self._commit([], True)
            with self._lock:
                self._events.clear()
                self._learn_active, self._learn_event, self._last_midi, self._armed = False, None, None, None
                self._matcher.reset()
            if self.fake_keys is not None:
                self.fake_keys.conflicts.clear()
            if self.fake_midi is not None:
                self.fake_midi.plugged = True
            self._sync_keys()
            return None
        return super()._extra(action, params)
