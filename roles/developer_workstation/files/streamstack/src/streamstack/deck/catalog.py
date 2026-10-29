"""What a Stream Deck key can do, as data: the action palette, how a key looks for the rig's state, what a press runs.

A key is stored as `{"action": "video.scene", "params": {"name": "Canon"}, "label": "...", "icon": "..."}` (label and
icon are the person's overrides). Everything else about it comes from a RULE here, found by action id (and fixed
params): its default label and icon, the state path that lights it, what a press does (flip a parameter, swap between
two actions, step a number) and which palette entries it appears as. A rule whose parameter takes its values from the
rig (scenes, voice presets, lighting presets) lists them from the snapshot, so the palette follows the rig.

To put a new Console action on the palette, add a rule to RULES. An action with no rule still works on a key: pick
"Any Console action" in the palette and type its name and parameters.

No Pillow, no Console imports: the Console, the service and the tests all use this module.
"""

from __future__ import annotations

import json
import re
import zlib
from typing import Any

ACTION_NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
MAX_LABEL = 24

# Group order of the palette.
GROUPS = ("Live", "Scenes", "Audio", "Voice", "Lights", "Director", "Camera", "Studio", "Deck", "Custom")

# The deck colours the renderer paints a lit key with; the Console's own traffic-light vocabulary.
TONES = ("red", "amber", "green", "accent")


def dig(data: Any, path: str) -> Any:
    """`a.b.0.c` read from nested dicts and lists; None when any step is missing."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _holds(cond: Any, ctx: dict, params: dict) -> bool:
    """A lit condition: {"path", "equals"?, "not"?}, or a list of them that must all hold."""
    if isinstance(cond, list):
        return all(_holds(c, ctx, params) for c in cond)
    value = dig(ctx, cond["path"])
    if "equals" in cond:
        want = cond["equals"]
        if isinstance(want, str) and want.startswith("$"):
            want = params.get(want[1:])
        ok = value == want
    else:
        ok = bool(value)
    return ok != bool(cond.get("not"))


# -- the rules -------------------------------------------------------------------------------------------------------
#
#   action        the Console action this rule describes (also `deck.*` actions the controller runs itself)
#   match         params that must equal these for the rule to apply (variants of one action)
#   sign          with `step`: the rule applies to keys whose step is positive (1) or negative (-1)
#   group, label, icon, hint     palette entry; label may use {param} placeholders
#   params        params of the palette entry
#   options       one palette entry per item of a list (or dict) in the snapshot:
#                 {"path", "param", "field"? (item key giving the param value), "label"? (item key giving the label),
#                  "dict"? (the path is a dict: key = label, value = param)}
#   choices       one palette entry per fixed (param value, label)
#   state         snapshot path of the current value; lit follows it unless `lit` says otherwise
#   lit           a condition (see _holds); tone is the colour while lit, lit_icon/lit_label replace icon/label
#   flip          a param pressed keys toggle from `state` (a key that fixes the param keeps it)
#   swap          [action when state is true, action when it is false]
#   step          {"key", "param", "path", "min", "max"}: a `key` param is a delta added to the value at `path`
#   confirm       the press that turns it off must be repeated within a few seconds
#   value         {"path", "fmt"?, "when"?}: a small read-out on the key


def _flag(action: str, **extra: Any) -> dict:
    return {"action": action, "flip": "on", **extra}


RULES: tuple[dict, ...] = (
    # ---- Live: the one-press controls
    {
        **_flag("audio.mute", group="Live", label="Mute", icon="mic", hint="Mute or unmute the microphone"),
        "state": "audio.mic.muted",
        "lit_icon": "mic-off",
        "lit_label": "Muted",
        "tone": "red",
    },
    {
        **_flag("video.privacy", group="Live", label="Privacy", icon="eye", hint="Camera to Be Right Back"),
        "state": "video.privacy",
        "lit_icon": "eye-off",
        "lit_label": "Privacy on",
        "tone": "amber",
    },
    {
        "action": "safejoin.go_live",
        "group": "Live",
        "label": "Go live",
        "icon": "broadcast",
        "hint": "Release the privacy and mute Safe join took when the call began",
        "state": "safejoin.holding",
        "lit_label": "Go live",
        "tone": "amber",
    },
    {
        **_flag("video.record", group="Live", label="Record", icon="record", hint="Start or stop recording"),
        "state": "video.recording.active",
        "lit_label": "Recording",
        "tone": "red",
        "confirm": True,
        "value": {"path": "video.recording.timecode"},
    },
    {
        **_flag("video.stream", group="Live", label="Stream", icon="broadcast", hint="Start or stop streaming"),
        "state": "video.streaming.active",
        "lit_label": "Live",
        "tone": "red",
        "confirm": True,
        "value": {"path": "video.streaming.timecode"},
    },
    {
        **_flag(
            "video.virtualcam", group="Live", label="Virtual cam", icon="vcam", hint="The camera Teams and Zoom see"
        ),
        "state": "video.virtualcam",
        "lit_label": "On air",
        "tone": "red",
    },
    {
        **_flag("video.blur", group="Live", label="Blur", icon="contrast", hint="Background blur on every camera"),
        "state": "video.blur.on",
        "available": "video.blur.available",
        "lit_label": "Blur on",
        "tone": "accent",
    },
    {
        "action": "video.blur",
        "group": "Live",
        "label": "Blur {name}",
        "icon": "contrast",
        "tone": "accent",
        "hint": "Background blur at a strength",
        "available": "video.blur.available",
        "options": {"path": "video.blur.presets", "param": "strength", "dict": True, "extra": {"on": True}},
        "lit": [{"path": "video.blur.on"}, {"path": "video.blur.strength", "equals": "$strength"}],
    },
    # ---- Scenes
    {
        "action": "video.scene",
        "group": "Scenes",
        "label": "{name}",
        "icon": "scenes",
        "hint": "Take this scene to air",
        "options": {"path": "video.scenes", "param": "name", "field": "name"},
        "lit": {"path": "video.program_scene", "equals": "$name"},
        "tone": "red",
    },
    # ---- Audio and voice
    {
        **_flag(
            "audio.bypass", group="Audio", label="Bypass", icon="wave", hint="Voice effects off, for an A/B listen"
        ),
        "state": "audio.chain.bypass",
        "lit_label": "Bypassed",
        "tone": "amber",
    },
    {
        "action": "audio.mic_check",
        "group": "Audio",
        "label": "Mic check",
        "icon": "mic",
        "hint": "Record and play back your voice",
    },
    {
        "action": "audio.preset",
        "group": "Voice",
        "label": "{name}",
        "icon": "wave",
        "hint": "Load this voice preset",
        "options": {"path": "audio.chain.presets", "param": "name", "field": "name", "label": "label"},
        "lit": {"path": "audio.chain.preset", "equals": "$name"},
        "tone": "accent",
    },
    # ---- Lights
    {
        **_flag(
            "lighting.power",
            group="Lights",
            label="Lights",
            icon="lights",
            hint="Every light on or off",
            params={"id": "all"},
        ),
        "state": "lighting.master.on",
        "lit_label": "Lights on",
        "tone": "amber",
        "value": {"path": "lighting.master.brightness", "fmt": "{:.0f}%", "when": {"path": "lighting.master.on"}},
    },
    {
        "action": "lighting.preset",
        "group": "Lights",
        "label": "{name}",
        "icon": "lights",
        "hint": "Apply this lighting preset",
        "options": {"path": "lighting.presets", "param": "name", "field": "name"},
        "lit": {"path": "lighting.preset", "equals": "$name"},
        "tone": "amber",
    },
    {
        "action": "lighting.brightness",
        "group": "Lights",
        "label": "Brighter",
        "icon": "plus",
        "tone": "amber",
        "hint": "Raise every light by 10%",
        "params": {"id": "all", "step": 10},
        "sign": 1,
        "step": {"key": "step", "param": "percent", "path": "lighting.master.brightness", "min": 1, "max": 100},
        "value": {"path": "lighting.master.brightness", "fmt": "{:.0f}%"},
    },
    {
        "action": "lighting.brightness",
        "group": "Lights",
        "label": "Dimmer",
        "icon": "moon",
        "tone": "amber",
        "hint": "Lower every light by 10%",
        "params": {"id": "all", "step": -10},
        "sign": -1,
        "step": {"key": "step", "param": "percent", "path": "lighting.master.brightness", "min": 1, "max": 100},
        "value": {"path": "lighting.master.brightness", "fmt": "{:.0f}%"},
    },
    {
        "action": "lighting.temperature",
        "group": "Lights",
        "label": "Warmer",
        "icon": "warmth",
        "tone": "amber",
        "hint": "Lower the colour temperature",
        "params": {"id": "all", "step": -300},
        "sign": -1,
        "step": {"key": "step", "param": "kelvin", "path": "lighting.master.kelvin", "min": 2700, "max": 6500},
        "value": {"path": "lighting.master.kelvin", "fmt": "{:.0f}K"},
    },
    {
        "action": "lighting.temperature",
        "group": "Lights",
        "label": "Cooler",
        "icon": "snow",
        "tone": "amber",
        "hint": "Raise the colour temperature",
        "params": {"id": "all", "step": 300},
        "sign": 1,
        "step": {"key": "step", "param": "kelvin", "path": "lighting.master.kelvin", "min": 2700, "max": 6500},
        "value": {"path": "lighting.master.kelvin", "fmt": "{:.0f}K"},
    },
    {
        **_flag(
            "lighting.auto.follow",
            flip="enabled",
            group="Lights",
            label="Cam follow",
            icon="camera",
            hint="The lights follow the camera: on when a call or the virtual camera starts",
        ),
        "state": "lighting.automation.settings.follow.enabled",
        "lit_label": "Following",
        "tone": "green",
    },
    {
        **_flag(
            "lighting.auto.schedule",
            flip="enabled",
            group="Lights",
            label="Schedule",
            icon="clock",
            hint="The daylight schedule on or off",
        ),
        "state": "lighting.automation.settings.schedule.enabled",
        "lit_label": "Scheduled",
        "tone": "green",
    },
    {
        "action": "lighting.auto.resume",
        "group": "Lights",
        "label": "Resume",
        "icon": "refresh",
        "hint": "Hand the lights back to the schedule",
    },
    # ---- Director
    {
        "action": "auto.on",
        "swap": ["auto.off", "auto.on"],
        "group": "Director",
        "label": "Director",
        "icon": "auto",
        "hint": "The AI director on or off",
        "state": "auto.running",
        "lit_label": "Directing",
        "tone": "accent",
    },
    {"action": "auto.once", "group": "Director", "label": "Ask now", "icon": "auto", "hint": "One director pass now"},
    {
        "action": "auto.reset",
        "group": "Director",
        "label": "Undo auto",
        "icon": "undo",
        "hint": "Undo every director change",
    },
    {
        "action": "auto.persona",
        "group": "Director",
        "label": "{name}",
        "icon": "auto",
        "hint": "Choose this persona",
        "options": {"path": "auto.director.personas", "param": "id", "field": "id", "label": "name"},
        "lit": {"path": "auto.director.persona", "equals": "$id"},
        "tone": "accent",
    },
    # ---- Camera
    {
        "action": "framing.mode",
        "group": "Camera",
        "label": "{mode}",
        "icon": "safe",
        "hint": "Auto-framing mode",
        "options": {"path": "framing.modes", "param": "mode", "field": "name", "label": "label"},
        "lit": {"path": "framing.mode", "equals": "$mode"},
        "tone": "accent",
    },
    {
        **_flag("framing.hold", group="Camera", label="Hold frame", icon="pause", hint="Freeze the auto-framing"),
        "state": "framing.held",
        "lit_label": "Held",
        "tone": "amber",
    },
    {
        "action": "video.looks.apply",
        "group": "Camera",
        "label": "{name}",
        "icon": "sliders",
        "hint": "Apply this look",
        "options": {"path": "video.looks", "param": "name", "field": "name"},
    },
    {
        "action": "video.look.reset",
        "group": "Camera",
        "label": "Reset look",
        "icon": "refresh",
        "hint": "Neutral picture",
    },
    {
        "action": "slots.apply",
        "group": "Camera",
        "label": "Time of day",
        "icon": "moonsun",
        "hint": "Apply the profile for this time of day now",
        "state": "slots.status.state",
        "lit": {"path": "slots.status.state", "equals": "applied"},
        "lit_label": "Profile on",
        "tone": "green",
    },
    {
        "action": "slots.undo",
        "group": "Camera",
        "label": "Undo profile",
        "icon": "undo",
        "hint": "Put back what the last time-of-day profile changed",
    },
    {
        "action": "slots.mode",
        "group": "Camera",
        "label": "Profiles {mode}",
        "icon": "clock",
        "hint": "Time-of-day profiles: off, suggest or apply by themselves",
        "choices": {"param": "mode", "items": [("off", "off"), ("suggest", "suggest"), ("auto", "auto")]},
        "lit": {"path": "slots.mode", "equals": "$mode"},
        "tone": "accent",
    },
    # ---- Studio
    {
        "action": "studio.start",
        "swap": ["studio.stop", "studio.start"],
        "group": "Studio",
        "label": "Studio",
        "icon": "power",
        "hint": "Start or stop the studio",
        "state": "studio.running",
        "lit_label": "Studio on",
        "tone": "green",
        "confirm": True,
    },
    {
        "action": "health.refresh",
        "group": "Studio",
        "label": "Health",
        "icon": "pulse",
        "hint": "Re-run the health checks",
    },
    {
        "action": "open.app",
        "group": "Studio",
        "label": "Open {name}",
        "icon": "external",
        "hint": "Open a tool",
        "choices": {
            "param": "name",
            "items": [
                ("easyeffects", "Easy Effects"),
                ("qpwgraph", "qpwgraph"),
                ("pavucontrol", "pavucontrol"),
                ("qv4l2", "qv4l2"),
            ],
        },
    },
    # ---- Deck: navigation and the deck's own settings (run by the Console's deck controller)
    {
        "action": "deck.page",
        "group": "Deck",
        "label": "{page}",
        "icon": "grid",
        "hint": "Open this page",
        "options": {"path": "deck.pages", "param": "page", "field": "id", "label": "name"},
    },
    {"action": "deck.back", "group": "Deck", "label": "Back", "icon": "undo", "hint": "Back to the page before"},
    {"action": "deck.home", "group": "Deck", "label": "Home", "icon": "grid", "hint": "Back to the first page"},
    {
        "action": "deck.settings",
        "group": "Deck",
        "label": "Deck +",
        "icon": "sun",
        "hint": "Deck brighter",
        "params": {"step": 10},
        "sign": 1,
        "step": {"key": "step", "param": "brightness", "path": "deck.settings.brightness", "min": 5, "max": 100},
        "value": {"path": "deck.settings.brightness", "fmt": "{:.0f}%"},
    },
    {
        "action": "deck.settings",
        "group": "Deck",
        "label": "Deck −",
        "icon": "moon",
        "hint": "Deck dimmer",
        "params": {"step": -10},
        "sign": -1,
        "step": {"key": "step", "param": "brightness", "path": "deck.settings.brightness", "min": 5, "max": 100},
        "value": {"path": "deck.settings.brightness", "fmt": "{:.0f}%"},
    },
)

CUSTOM_ENTRY = {
    "id": "custom",
    "group": "Custom",
    "label": "Any Console action",
    "icon": "command",
    "action": "",
    "params": {},
    "hint": "Type the action name and its parameters in the key inspector",
    "custom": True,
}


def unavailable(rule: dict | None, ctx: dict) -> bool:
    """True when the rig reports that the rule's capability is not there right now (a missing report means it is)."""
    path = rule.get("available") if rule else None
    return bool(path) and dig(ctx, path) is False


def find_rule(action: str, params: dict) -> dict | None:
    """The rule for a key: same action, fixed params equal; the most specific (most `match` entries) wins."""
    best, size = None, -1
    for rule in RULES:
        if rule["action"] != action and action not in rule.get("swap", ()):
            continue
        match = rule.get("match", {})
        if "sign" in rule:
            delta = params.get(rule["step"]["key"])
            if not isinstance(delta, (int, float)) or isinstance(delta, bool) or (delta > 0) != (rule["sign"] > 0):
                continue
        spec = rule.get("options") or rule.get("choices")
        if spec and params.get(spec["param"]) is None:
            continue  # an option rule names its item; a key without it is the plain rule
        weight = len(match) + (1 if spec else 0)
        if all(params.get(k) == v for k, v in match.items()) and weight > size:
            best, size = rule, weight
    return best


def _option_items(rule: dict, ctx: dict) -> list[tuple[Any, str]]:
    """(param value, label) for each item an `options` or `choices` rule offers right now."""
    spec = rule.get("options")
    if spec:
        found = dig(ctx, spec["path"])
        if spec.get("dict") and isinstance(found, dict):
            return [(v, str(k).capitalize()) for k, v in found.items()]
        if isinstance(found, list):
            items = []
            for item in found:
                if isinstance(item, dict) and spec.get("field", "name") in item:
                    items.append(
                        (item[spec["field"]], str(item.get(spec.get("label", spec["field"]), item[spec["field"]])))
                    )
            return items
        return []
    choices = rule.get("choices")
    return [(v, str(label)) for v, label in choices["items"]] if choices else []


def _fill(template: str, params: dict) -> str:
    try:
        return template.format(**{k: v for k, v in params.items() if isinstance(v, (str, int, float))})
    except (KeyError, IndexError, ValueError):
        return re.sub(r"\{[^}]*\}", "", template).strip()


def _label_for(rule: dict, params: dict, ctx: dict) -> str:
    """The rule's label with its placeholders filled; an option's label comes from the item it names."""
    spec = rule.get("options")
    if spec:
        wanted = params.get(spec["param"])
        for value, label in _option_items(rule, ctx):
            if value == wanted:
                return _fill(rule["label"], {**params, "name": label}) if spec.get("dict") else label
    return _fill(rule["label"], params)


def palette(ctx: dict) -> list[dict]:
    """Every key the person can assign, grouped; items that come from the rig are listed from `ctx`."""
    entries: list[dict] = []
    seen: set[str] = set()
    for rule in RULES:
        if unavailable(rule, ctx):
            continue
        base = {"action": rule["action"], "group": rule["group"], "icon": rule["icon"], "hint": rule.get("hint", "")}
        if "options" in rule or "choices" in rule:
            spec = rule.get("options") or rule["choices"]
            param, extra = spec["param"], spec.get("extra", {})
            for value, label in _option_items(rule, ctx):
                params = {**extra, param: value}
                shown = _fill(rule["label"], {**params, "name": label}) if spec.get("dict") else label
                entries.append(
                    {**base, "label": shown if rule.get("options") else _fill(rule["label"], params), "params": params}
                )
        else:
            entries.append({**base, "label": rule["label"], "params": dict(rule.get("params", {}))})
    out = []
    for entry in entries:
        entry["id"] = f"{entry['action']}:{json.dumps(entry['params'], sort_keys=True, separators=(',', ':'))}"
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
    out.sort(key=lambda e: GROUPS.index(e["group"]) if e["group"] in GROUPS else len(GROUPS))
    out.append(dict(CUSTOM_ENTRY))
    return out


# -- a key's face and its press ---------------------------------------------------------------------------------------


def empty_view(index: int) -> dict:
    return {"i": index, "empty": True, "rev": "0"}


def _fmt_value(spec: dict, ctx: dict, params: dict) -> str | None:
    if "when" in spec and not _holds(spec["when"], ctx, params):
        return None
    value = dig(ctx, spec["path"])
    if value is None or value == "":
        return None
    fmt = spec.get("fmt")
    try:
        return fmt.format(value) if fmt else str(value)
    except (ValueError, TypeError):
        return str(value)


def view_of(key: dict | None, ctx: dict, index: int = 0, armed: bool = False) -> dict:
    """What the key shows right now: label, icon, lit state, a read-out. `rev` changes whenever the picture would."""
    if not key:
        return empty_view(index)
    action, params = key.get("action", ""), key.get("params") or {}
    rule = find_rule(action, params) if action else None
    lit, tone, value = False, "", None
    icon = key.get("icon") or (rule["icon"] if rule else "command")
    label = key.get("label")
    default = _label_for(rule, params, ctx) if rule else (action or "Set action")
    if rule:
        tone = rule.get("tone", "")
        if "lit" in rule:
            lit = _holds(rule["lit"], ctx, params)
        elif "state" in rule:
            on = bool(dig(ctx, rule["state"]))
            flip = rule.get("flip")
            lit = on if not flip or flip not in params else (on and bool(params[flip]))
        if lit:
            if not key.get("icon") and rule.get("lit_icon"):
                icon = rule["lit_icon"]
            if rule.get("lit_label"):
                default = rule["lit_label"]
        if "value" in rule:
            value = _fmt_value(rule["value"], ctx, params)
    if armed:
        default, tone, lit, value = "Again to stop", "amber", True, None
    view = {
        "i": index,
        "empty": False,
        "action": action,
        "label": label or default,
        "icon": icon,
        "lit": lit,
        "tone": tone if lit else "",
        "value": value,
        "armed": armed,
        "dim": bool(rule is None and action == "") or unavailable(rule, ctx),
    }
    view["rev"] = format(zlib.crc32(json.dumps(view, sort_keys=True, separators=(",", ":")).encode()), "08x")
    return view


class PressError(ValueError):
    """The key cannot be pressed (no action, or a value it must step has no reading yet)."""


def resolve_press(key: dict, ctx: dict) -> tuple[str, dict, bool]:
    """(action, params, needs_confirm) for pressing `key` with the rig in the state `ctx`."""
    action, params = key.get("action", ""), dict(key.get("params") or {})
    if not action:
        raise PressError("This key has no action yet. Pick one in the Deck page.")
    rule = find_rule(action, params)
    if rule is None:
        return action, params, False
    if unavailable(rule, ctx):
        raise PressError("That is not available on this setup right now.")
    on = bool(dig(ctx, rule["state"])) if "state" in rule else False
    confirm = False
    flip = rule.get("flip")
    if flip and flip not in params:
        params[flip] = not on
    if flip and rule.get("confirm") and params.get(flip) is False and on:
        confirm = True
    if "swap" in rule and action in rule["swap"]:
        action = rule["swap"][0] if on else rule["swap"][1]
        confirm = bool(rule.get("confirm")) and on
    step = rule.get("step")
    if step and step["key"] in params:
        delta = params.pop(step["key"])
        current = dig(ctx, step["path"])
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            raise PressError("The step must be a number.")
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            raise PressError("There is no reading to step from yet.")
        params[step["param"]] = int(max(step["min"], min(step["max"], round(current + delta))))
    return action, params, confirm
