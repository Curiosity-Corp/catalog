"""Global hotkeys and MIDI controls: the binding model, key combos, the MIDI byte parser and the saved file.

A binding ties one trigger to one Console action (the same actions the Stream Deck keys run, see deck/catalog.py):

    {"id": "k1", "trigger": {"kind": "key", "combo": "ctrl+alt+m"},
     "action": "audio.mute", "params": {}, "label": "Mute"}
    {"id": "k2", "trigger": {"kind": "midi", "type": "cc", "channel": 1, "number": 7},
     "action": "audio.voice_level", "params": {}, "scale": {"param": "level", "min": 0, "max": 100}}

Triggers:

* `key`: a combo such as "ctrl+alt+m", "super+f5" or "xf86audiomute". Modifiers come first (ctrl, alt, shift, super),
  the key is a lower-case name from KEYS. A plain letter, digit or punctuation key is refused (it would steal typing
  from every window); function keys, the media keys and the navigation block may stand alone.
* `midi`: a note (on), a control change or a program change, on one channel 1-16 or on any (channel 0). A control
  change fires once when it crosses 64 upward (a button); with `scale` it fires on every change and carries the value
  scaled into a parameter of the action (a fader).

The file is ~/.config/streamstack/hotkeys.json (mode 600, atomic, damaged files are set aside by `streamstack.store`).
Nothing here talks to X11, Wayland or a MIDI device: backends (hotkeys_x11.py, hotkeys_portal.py, hotkeys_midi.py)
turn those into the combos and `MidiEvent`s this module understands.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from streamstack import config, store
from streamstack.deck import catalog

VERSION = 1
DOCUMENT = "streamstack.hotkeys/1"
MAX_BINDINGS = 128
MAX_PARAMS_BYTES = 2048
CC_THRESHOLD = 64
MIDI_KINDS = ("note", "cc", "program")
MODIFIERS = ("ctrl", "alt", "shift", "super")
BINDING_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


class HotkeyError(ValueError):
    """A binding, a combo or the file is not valid; the message says what to fix."""


# -- keys -------------------------------------------------------------------------------------------------------------

# canonical lower-case name -> X keysym / XDG GlobalShortcuts key name
_NAMED: dict[str, str] = {
    "space": "space", "return": "Return", "escape": "Escape", "tab": "Tab", "backspace": "BackSpace",
    "insert": "Insert", "delete": "Delete", "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "pause": "Pause", "print": "Print",
    "scrolllock": "Scroll_Lock",
    "minus": "minus", "equal": "equal", "comma": "comma", "period": "period", "slash": "slash",
    "semicolon": "semicolon", "apostrophe": "apostrophe", "bracketleft": "bracketleft",
    "bracketright": "bracketright", "backslash": "backslash", "grave": "grave",
}  # fmt: skip
_MEDIA: dict[str, str] = {
    "xf86audiomute": "XF86AudioMute", "xf86audiomicmute": "XF86AudioMicMute",
    "xf86audioraisevolume": "XF86AudioRaiseVolume", "xf86audiolowervolume": "XF86AudioLowerVolume",
    "xf86audioplay": "XF86AudioPlay", "xf86audiopause": "XF86AudioPause", "xf86audionext": "XF86AudioNext",
    "xf86audioprev": "XF86AudioPrev", "xf86audiostop": "XF86AudioStop", "xf86audiorecord": "XF86AudioRecord",
}  # fmt: skip
_STANDALONE_NAMED = {"insert", "delete", "home", "end", "pageup", "pagedown", "pause", "print", "scrolllock"}


def keysym(name: str) -> str:
    """The X keysym / XDG key name for a canonical key name ("pageup" -> "Prior"). HotkeyError for an unknown one."""
    if re.fullmatch(r"[a-z0-9]", name):
        return name
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", name):
        return name.upper()
    if re.fullmatch(r"kp_[0-9]", name):
        return "KP_" + name[3:]
    if name in _NAMED:
        return _NAMED[name]
    if name in _MEDIA:
        return _MEDIA[name]
    raise HotkeyError(f"{name!r} is not a key StreamStack can listen for.")


def _standalone(key: str) -> bool:
    return (
        bool(re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key))
        or key in _MEDIA
        or key in _STANDALONE_NAMED
        or bool(re.fullmatch(r"kp_[0-9]", key))
    )


_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "meta": "alt", "win": "super", "windows": "super",
    "logo": "super", "cmd": "super", "command": "super", "esc": "escape", "enter": "return", "del": "delete",
    "ins": "insert", "pgup": "pageup", "prior": "pageup", "pgdn": "pagedown", "next": "pagedown",
    "page_up": "pageup", "page_down": "pagedown", "plus": "equal", "-": "minus", "=": "equal", ",": "comma",
    ".": "period", "/": "slash", ";": "semicolon", "'": "apostrophe", "[": "bracketleft", "]": "bracketright",
    "\\": "backslash", "`": "grave", " ": "space", "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
    "arrowright": "right", "scroll_lock": "scrolllock", "printscreen": "print", "print_screen": "print",
}  # fmt: skip


def clean_combo(raw: Any) -> str:
    """The canonical form of a combo: "Ctrl + Alt + M" -> "ctrl+alt+m". Raises HotkeyError when it is not usable."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 60:
        raise HotkeyError("A hotkey is a combo such as ctrl+alt+m.")
    text = raw.strip().lower()
    if text.endswith("++"):  # a browser reports the plus key as "ctrl++"
        text = text[:-2] + "+plus"
    parts = [_ALIASES.get(p.strip(), p.strip()) for p in text.split("+")]
    if not all(parts):
        raise HotkeyError("A hotkey is a combo such as ctrl+alt+m.")
    key, mods = parts[-1], parts[:-1]
    for m in mods:
        if m not in MODIFIERS:
            raise HotkeyError(f"{m!r} is not a modifier; use ctrl, alt, shift or super.")
    if key in MODIFIERS:
        raise HotkeyError("Add a key after the modifiers.")
    keysym(key)
    ordered = [m for m in MODIFIERS if m in mods]
    if len(set(mods)) != len(mods):
        raise HotkeyError("A modifier appears twice.")
    if not ordered and not _standalone(key):
        raise HotkeyError(
            "Add ctrl, alt or super: a bare letter, digit or symbol would take typing away from every window."
        )
    if ordered == ["shift"] and not _standalone(key):
        raise HotkeyError("Shift alone is typing; combine it with ctrl, alt or super.")
    return "+".join([*ordered, key])


def split_combo(combo: str) -> tuple[tuple[str, ...], str]:
    """(modifiers, key) of a canonical combo."""
    parts = clean_combo(combo).split("+")
    return tuple(parts[:-1]), parts[-1]


def xdg_trigger(combo: str) -> str:
    """The `preferred_trigger` text of the XDG GlobalShortcuts portal for a combo: "CTRL+ALT+m"."""
    mods, key = split_combo(combo)
    names = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}
    return "+".join([*(names[m] for m in mods), keysym(key)])


def describe_combo(combo: str) -> str:
    """A person's reading of a combo: "Ctrl+Alt+M"."""
    mods, key = split_combo(combo)
    shown = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
    return "+".join([*(shown[m] for m in mods), key.upper() if len(key) == 1 else keysym(key)])


# -- MIDI -------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MidiEvent:
    """One MIDI message a binding can react to. `channel` is 1-16, `number` the note, controller or program."""

    kind: str  # "note" | "cc" | "program"
    channel: int
    number: int
    value: int = 127  # velocity or controller value; 127 for a program change

    def label(self) -> str:
        return trigger_text({"kind": "midi", "type": self.kind, "channel": self.channel, "number": self.number})


_DATA_BYTES = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}


class MidiParser:
    """Turns the bytes of a raw MIDI port into `MidiEvent`s: running status, sysex and realtime bytes handled.

    Only note-on (velocity above 0), control change and program change are reported: those are what a binding uses.
    """

    def __init__(self) -> None:
        self._status = 0
        self._data: list[int] = []
        self._in_sysex = False

    def feed(self, data: bytes) -> list[MidiEvent]:
        events: list[MidiEvent] = []
        for byte in data:
            if byte >= 0xF8:  # realtime: may sit anywhere, changes nothing
                continue
            if byte >= 0x80:
                self._data = []
                if byte == 0xF0:
                    self._in_sysex = True
                    self._status = 0
                elif byte == 0xF7 or byte >= 0xF0:
                    self._in_sysex = False
                    self._status = 0
                else:
                    self._in_sysex = False
                    self._status = byte
                continue
            if self._in_sysex or not self._status:
                continue
            self._data.append(byte)
            if len(self._data) < _DATA_BYTES[self._status & 0xF0]:
                continue
            event = self._event(self._status, self._data)
            self._data = []  # running status: the status byte stays
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _event(status: int, data: list[int]) -> MidiEvent | None:
        kind, channel = status & 0xF0, (status & 0x0F) + 1
        if kind == 0x90 and data[1] > 0:
            return MidiEvent("note", channel, data[0], data[1])
        if kind == 0xB0:
            return MidiEvent("cc", channel, data[0], data[1])
        if kind == 0xC0:
            return MidiEvent("program", channel, data[0])
        return None


# -- bindings ---------------------------------------------------------------------------------------------------------


def clean_trigger(raw: Any) -> dict:
    """A validated trigger: {"kind": "key", "combo"} or {"kind": "midi", "type", "channel", "number"}."""
    if not isinstance(raw, dict):
        raise HotkeyError("A trigger must be an object.")
    kind = raw.get("kind")
    if kind == "key":
        return {"kind": "key", "combo": clean_combo(raw.get("combo"))}
    if kind == "midi":
        mtype, channel, number = raw.get("type"), raw.get("channel", 0), raw.get("number")
        if mtype not in MIDI_KINDS:
            raise HotkeyError("A MIDI trigger is a note, cc or program.")
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 16:
            raise HotkeyError("The MIDI channel is 1 to 16, or 0 for any channel.")
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 127:
            raise HotkeyError("The MIDI number is 0 to 127.")
        return {"kind": "midi", "type": mtype, "channel": channel, "number": number}
    raise HotkeyError("A trigger is a key or a MIDI message.")


def trigger_key(trigger: dict) -> str:
    """A string that is equal for two triggers that would fire on the same input."""
    if trigger["kind"] == "key":
        return "key:" + trigger["combo"]
    return f"midi:{trigger['type']}:{trigger['channel']}:{trigger['number']}"


def trigger_text(trigger: dict) -> str:
    """A person's reading of a trigger: "Ctrl+Alt+M", "Note 60 (any channel)", "CC 7 on channel 1"."""
    if trigger["kind"] == "key":
        return describe_combo(trigger["combo"])
    name = {"note": "Note", "cc": "CC", "program": "Program"}[trigger["type"]]
    where = "any channel" if trigger["channel"] == 0 else f"channel {trigger['channel']}"
    return f"{name} {trigger['number']} ({where})"


def clean_scale(raw: Any, trigger: dict) -> dict | None:
    if raw is None:
        return None
    if trigger["kind"] != "midi" or trigger["type"] != "cc":
        raise HotkeyError("Only a MIDI control change (a fader or knob) can set a value.")
    if not isinstance(raw, dict):
        raise HotkeyError("scale must be an object with param, min and max.")
    param, low, high = raw.get("param"), raw.get("min"), raw.get("max")
    if not isinstance(param, str) or not PARAM_NAME.fullmatch(param):
        raise HotkeyError("scale.param must be the action's parameter name, such as level.")
    for name, value in (("min", low), ("max", high)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -1_000_000 <= value <= 1_000_000:
            raise HotkeyError(f"scale.{name} must be a number.")
    if low == high:
        raise HotkeyError("scale.min and scale.max must differ.")
    return {"param": param, "min": low, "max": high}


def clean_binding(raw: Any) -> dict:
    """A validated binding (see the module notes). Raises HotkeyError naming what to fix."""
    if not isinstance(raw, dict):
        raise HotkeyError("A binding must be an object.")
    ident = raw.get("id")
    if not isinstance(ident, str) or not BINDING_ID.fullmatch(ident):
        raise HotkeyError("A binding needs an id of lower-case letters, digits, - and _.")
    trigger = clean_trigger(raw.get("trigger"))
    action = raw.get("action")
    if not isinstance(action, str) or not catalog.ACTION_NAME.fullmatch(action) or len(action) > 40:
        raise HotkeyError("The action must look like video.scene (letters, digits and underscores around a dot).")
    params = raw.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise HotkeyError("params must be an object.")
    try:
        encoded = json.dumps(params, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise HotkeyError(f"params must be JSON: {exc}") from exc
    if len(encoded) > MAX_PARAMS_BYTES:
        raise HotkeyError("params are too large.")
    out: dict[str, Any] = {"id": ident, "trigger": trigger, "action": action, "params": params}
    label = raw.get("label")
    if label is not None:
        if not isinstance(label, str) or len(label) > 60:
            raise HotkeyError("The label is text of up to 60 characters.")
        if label.strip():
            out["label"] = label.strip()
    scale = clean_scale(raw.get("scale"), trigger)
    if scale:
        out["scale"] = scale
    return out


def clean_bindings(raw: Any) -> list[dict]:
    """A validated list; two bindings on the same input are an error (the second could never fire)."""
    if not isinstance(raw, list):
        raise HotkeyError("bindings must be a list.")
    if len(raw) > MAX_BINDINGS:
        raise HotkeyError(f"At most {MAX_BINDINGS} bindings.")
    out: list[dict] = []
    ids: set[str] = set()
    inputs: dict[str, str] = {}
    for item in raw:
        binding = clean_binding(item)
        if binding["id"] in ids:
            raise HotkeyError(f"Two bindings share the id {binding['id']!r}.")
        ids.add(binding["id"])
        key = trigger_key(binding["trigger"])
        if key in inputs:
            raise HotkeyError(f"{trigger_text(binding['trigger'])} is already used by {inputs[key]!r}.")
        inputs[key] = binding.get("label") or binding["action"]
        out.append(binding)
    return out


def new_id(existing: set[str]) -> str:
    n = 1
    while f"b{n}" in existing:
        n += 1
    return f"b{n}"


# -- matching ---------------------------------------------------------------------------------------------------------


def scaled(scale: dict, value: int) -> int | float:
    """The parameter value a controller reading 0-127 stands for in `scale`: whole numbers when min and max are."""
    low, high = scale["min"], scale["max"]
    result = low + (high - low) * (max(0, min(127, value)) / 127)
    if isinstance(low, int) and isinstance(high, int):
        return round(result)
    return round(result, 3)


class Matcher:
    """Finds the bindings a MIDI event fires. Keeps the last value of each controller to see a button cross 64."""

    def __init__(self) -> None:
        self._last: dict[tuple[int, int], int] = {}

    def reset(self) -> None:
        self._last.clear()

    def fire(self, bindings: list[dict], event: MidiEvent) -> list[tuple[dict, dict]]:
        """[(binding, extra params)] for the bindings `event` triggers."""
        previous = self._last.get((event.channel, event.number), 0) if event.kind == "cc" else 0
        if event.kind == "cc":
            self._last[(event.channel, event.number)] = event.value
        out: list[tuple[dict, dict]] = []
        for binding in bindings:
            trig = binding["trigger"]
            if trig["kind"] != "midi" or trig["type"] != event.kind or trig["number"] != event.number:
                continue
            if trig["channel"] not in (0, event.channel):
                continue
            scale = binding.get("scale")
            if scale:
                out.append((binding, {scale["param"]: scaled(scale, event.value)}))
            elif event.kind == "cc":
                if previous < CC_THRESHOLD <= event.value:
                    out.append((binding, {}))
            else:
                out.append((binding, {}))
        return out


# -- the file ---------------------------------------------------------------------------------------------------------


class Store:
    """~/.config/streamstack/hotkeys.json: `{"version", "enabled", "bindings"}`, mode 600, atomic."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def file(self) -> Path:
        return self.path or config.user_env_file().parent / "hotkeys.json"

    def load(self) -> tuple[bool, list[dict]]:
        """(enabled, bindings). Missing or unusable content is (True, []); one bad binding drops only itself."""
        with self._lock:
            raw = store.read_json(self.file(), {}, validate=store.is_object)
        bindings: list[dict] = []
        seen_ids: set[str] = set()
        seen_inputs: set[str] = set()
        items = raw.get("bindings")
        for item in items if isinstance(items, list) else []:
            try:
                binding = clean_binding(item)
            except HotkeyError:
                continue
            key = trigger_key(binding["trigger"])
            if binding["id"] in seen_ids or key in seen_inputs:
                continue
            seen_ids.add(binding["id"])
            seen_inputs.add(key)
            bindings.append(binding)
        return raw.get("enabled") is not False, bindings[:MAX_BINDINGS]

    def save(self, enabled: bool, bindings: list[dict]) -> None:
        cleaned = clean_bindings(bindings)
        with self._lock:
            store.write_json(
                self.file(), {"version": VERSION, "enabled": bool(enabled), "bindings": cleaned}, mode=0o600
            )


class MemoryStore:
    """The demo rig's store: nothing on disk."""

    def __init__(self) -> None:
        self._enabled = True
        self._bindings: list[dict] = []

    def load(self) -> tuple[bool, list[dict]]:
        return self._enabled, [dict(b) for b in self._bindings]

    def save(self, enabled: bool, bindings: list[dict]) -> None:
        self._enabled, self._bindings = bool(enabled), clean_bindings(bindings)


def export_document(enabled: bool, bindings: list[dict]) -> dict:
    return {"document": DOCUMENT, "enabled": enabled, "bindings": bindings}


def import_document(raw: Any) -> tuple[bool, list[dict]]:
    """(enabled, bindings) from an exported document (or a bare list of bindings). Raises HotkeyError."""
    if isinstance(raw, list):
        return True, clean_bindings(raw)
    if not isinstance(raw, dict) or raw.get("document") != DOCUMENT:
        raise HotkeyError(f"That is not a StreamStack hotkeys file (expected document {DOCUMENT!r}).")
    return raw.get("enabled") is not False, clean_bindings(raw.get("bindings"))
