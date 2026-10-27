"""Time-of-day look profiles in the Console: the engine that applies them and the actions behind the Camera strip.

`streamstack.slots` holds the vocabulary and the file; this controller watches the clock and the camera and uses the
actions the Console already has (`video.looks.apply`, `video.canon_set`, `video.scene`, `video.blur`, `audio.preset`,
`lighting.preset`), so a profile is nothing a person could not have done with the same buttons, and every change
shows up in the same places.

When a profile is applied (mode `auto`): when a call starts, and when the slot changes while no call is running (a
slot change in the middle of a call would move the picture under the other side, so it waits for the next call).
Mode `suggest` raises the same trigger as a toast and a button instead. Mode `off` only applies on request.

One owner at a time, and nobody fights:

* **Manual changes win until the slot ends.** After a profile is applied the controller watches the pieces it set; if a
  person changes one (the Console, a Deck key, the light's own buttons), the profile holds off until the next slot,
  and the status says which control did it. The lights' own schedule has the same rule (`lightauto`).
* **The AI Director owns the picture while it runs.** With the Director on, the profile's look and lights *seed the
  Director's baselines for this slot* (`director.learning`) instead of being written, and its voice preset and Canon
  settings are left to the Director too, so the two never oscillate. The scene and the blur, which the Director does
  not touch, still apply.

Every apply keeps what it replaced, so `slots.undo` puts the picture back, and every decision (applied, left alone,
held, skipped and why) is in the log the strip shows.

Actions (bad input is an ActionError, never a crash):
  slots.mode          {mode}                  off, suggest or auto
  slots.fade          {seconds}               how long lights take to move to the new level
  slots.profile       {slot, profile}         store a slot's profile (an empty one clears it)
  slots.clear         {slot}                  forget a slot's profile
  slots.save_current  {slot}                  store what the rig is doing now as this slot's profile
  slots.starter       {}                      fill the empty slots with sensible picks
  slots.apply         {slot?}                 apply a slot's profile now (default: the current slot)
  slots.undo          {}                      put back what the last apply replaced
  slots.resume        {}                      end a manual-change hold
  slots.dismiss       {}                      drop the suggestion
  slots.import        {document}              replace the profiles from `streamstack.slot-profiles/1`
  slots.reset         {}                      back to the defaults
  slots.demo.clock    {time}                  demo rig only: move the clock to HH:MM today
  slots.demo.reset    {}                      demo rig only: no profiles, the clock back where it started
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from streamstack import config, slots
from streamstack.console import lightauto as lightpanel
from streamstack.console.errors import ActionError
from streamstack.deck.catalog import dig
from streamstack.director import daylight, learning, settings
from streamstack.presets.schema import LOOK_LIMITS

log = logging.getLogger(__name__)

Dispatch = Callable[[str, dict], "str | None"]
Notify = Callable[[str, str], None]

TICK_SECONDS = 1.0
SETTLE_SECONDS = 4.0  # after an apply, the controls are still moving (a look ramps, a light fades): do not compare yet
WATCH_SECONDS = 5.0  # how often the applied pieces are compared with what the rig shows
EVENTS_KEPT = 30
LOCATION_CACHE = 60.0
LOOK_EPS = 0.006  # a look value that differs by less than this is the same look (they are stored to four places)
FADE_STEP_SECONDS = 1.0
MAX_FADE_STEPS = 30
STRIP_STEP = 10


def empty_state() -> dict[str, Any]:
    """The `slots` snapshot block for a rig without profiles (and the frontend's starting skeleton)."""
    return {
        "available": False,
        "mode": "off",
        "modes": [{"id": m, "note": slots.MODE_NOTES[m]} for m in slots.MODES],
        "fade_seconds": slots.DEFAULT_FADE,
        "limits": {"fade_seconds": list(slots.FADE_LIMITS)},
        "slots": [{"id": s, "label": slots.LABELS[s], "profile": {}, "empty": True} for s in slots.SLOTS],
        "parts": [{"id": p, "label": slots.PART_LABELS[p]} for p in slots.PARTS],
        "current": {"slot": "morning", "label": "Morning", "minute": 0, "clock": "00:00"},
        "strip": [],
        "options": {
            "looks": [],
            "scenes": [],
            "voices": [],
            "lighting": [],
            "canon": {"present": False},
            "blur": False,
        },
        "status": {
            "state": "off",
            "summary": "Time-of-day profiles are not part of this rig.",
            "applied": None,
            "held": None,
            "suggestion": None,
            "can_undo": False,
        },
        "director": {"on": False},
        "events": [],
        "demo": False,
    }


def _same_look(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    for key in LOOK_LIMITS:
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            continue
        if abs(float(x) - float(y)) > LOOK_EPS:
            return False
    return all(a.get(k) == b.get(k) for k in ("sharpen", "lut") if k in a and k in b)


def _names(items: object, key: str = "name") -> list[str]:
    return [str(i[key]) for i in items if isinstance(i, Mapping) and i.get(key)] if isinstance(items, list) else []


class SlotsController:
    """The `slots.*` actions, the `slots` state block and the engine that applies profiles."""

    def __init__(
        self,
        dispatch: Dispatch,
        store: slots.Store | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        location: Callable[[datetime], daylight.Location] | None = None,
        in_call: Callable[[], bool] = lambda: False,
        seed: Callable[[str, str, dict, tuple[float, float] | None], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        tick_seconds: float = TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dispatch = dispatch
        self.store = store or slots.Store()
        self._now = now
        self._location_of = location or (
            lambda wall: daylight.resolve_location(settings.load(), config.load_settings(), wall)
        )
        self._in_call = in_call
        self._seed = seed or self._seed_director
        self._stop = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stop.wait(seconds))
        self._clock = clock
        self.tick_seconds = tick_seconds
        self.notify: Notify | None = None
        self.rig: Any = None  # the Rig sets this: the controller reads its snapshot to see what is on and what changed
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()  # the fields below; never held across an apply
        self._tick_lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self._slot: str | None = None
        self._hour_angle = 0.0
        self._call: bool | None = None
        self._applied: dict[str, Any] | None = None
        self._undo: dict[str, Any] | None = None
        self._held: dict[str, Any] | None = None
        self._suggestion: dict[str, Any] | None = None
        self._events: deque[dict[str, str]] = deque(maxlen=EVENTS_KEPT)
        self._watched_at = 0.0
        self._strip_cache: tuple[str, list[dict[str, Any]]] | None = None
        self._location_cache: tuple[float, daylight.Location] | None = None
        self._director_on = False

    # -- lifecycle ------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="slot-profiles", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("slot profile tick failed")
            self._stop.wait(self.tick_seconds)

    def _say(self, level: str, text: str) -> None:
        if self.notify:
            self.notify(level, text)

    def _log(self, level: str, text: str) -> None:
        with self._lock:
            self._events.appendleft({"time": self._now().strftime("%H:%M:%S"), "level": level, "text": text})

    # -- the clock ---------------------------------------------------------------------------------------

    def _location(self, wall: datetime) -> daylight.Location:
        cached = self._location_cache
        if cached and self._clock() - cached[0] < LOCATION_CACHE:
            return cached[1]
        loc = self._location_of(wall)
        self._location_cache = (self._clock(), loc)
        return loc

    def _snapshot(self) -> dict[str, Any]:
        if self.rig is None:
            raise ActionError("Time-of-day profiles are not connected to a rig.", 409)
        return self.rig.snapshot()

    # -- one tick --------------------------------------------------------------------------------------------

    def tick(self) -> None:
        """Notice a slot change or a call starting, and do what the mode says; then watch for manual changes."""
        with self._tick_lock:
            wall = self._now()
            try:
                slot, hour_angle = slots.slot_at(wall, self._location(wall))
            except (ValueError, OSError) as exc:
                log.warning("time-of-day slot unavailable: %s", exc)
                return
            call = bool(self._in_call())
            with self._lock:
                previous_slot, previous_call = self._slot, self._call
                self._slot, self._hour_angle, self._call = slot, hour_angle, call
                changed = previous_slot is not None and slot != previous_slot
                started = call and previous_call is False
                if changed:
                    self._held = None  # a manual change wins until the slot ends
                    self._applied = None
                    self._suggestion = None
            mode = self.store.load()["mode"]
            if changed:
                self._log("info", f"{slots.LABELS[slot]} begins")
            if mode != "off" and (changed or started):
                reason = "a call started" if started and not changed else f"{slots.LABELS[slot]} began"
                self._trigger(mode, slot, reason, call=call, slot_changed=changed and not started)
            self._watch()

    def _trigger(self, mode: str, slot: str, reason: str, *, call: bool, slot_changed: bool) -> None:
        profile = self.store.load()["profiles"].get(slot)
        if not profile:
            return
        label = slots.LABELS[slot]
        if slot_changed and call:
            self._log("info", f"{label} profile left alone: a call is running, it applies when the next one starts")
            return
        with self._lock:
            held = self._held
        if held:
            self._log("info", f"{label} profile not applied: you changed the {held['label'].lower()} ({reason})")
            return
        if mode == "suggest":
            with self._lock:
                self._suggestion = {"slot": slot, "label": label, "reason": reason, "at": self._now().strftime("%H:%M")}
            self._log("info", f"{label} profile is ready ({reason})")
            self._say("info", f"The {label.lower()} profile is ready. Apply it on the Camera page.")
            return
        try:
            self._apply(slot, reason)
        except ActionError as exc:
            self._log("warn", f"{label} profile not applied: {exc.message}")

    # -- applying ------------------------------------------------------------------------------------------------

    def _steps(self, profile: dict[str, Any], director_on: bool) -> list[tuple[str, str, dict[str, Any]]]:
        """(part, action, params) in the order they are applied; the Director's parts are left out while it runs."""
        steps: list[tuple[str, str, dict[str, Any]]] = []
        if "scene" in profile:
            steps.append(("scene", "video.scene", {"name": profile["scene"]}))
        if "blur" in profile:
            steps.append(("blur", "video.blur", dict(profile["blur"])))
        if director_on:
            return steps
        if "look" in profile:
            steps.append(("look", "video.looks.apply", {"name": profile["look"]}))
        canon = profile.get("canon", {})
        if "exposure_comp" in canon:
            steps.append(("canon", "video.canon_set", {"id": "exposure_comp", "value": canon["exposure_comp"]}))
        if "kelvin" in canon:
            steps.append(("canon", "video.canon_set", {"id": "kelvin", "value": canon["kelvin"]}))
        if "voice" in profile:
            steps.append(("voice", "audio.preset", {"name": profile["voice"]}))
        if "lighting" in profile:
            steps.append(("lighting", "lighting.preset", {"name": profile["lighting"]}))
        return steps

    def _capture(self, snap: dict[str, Any], parts: set[str]) -> dict[str, Any]:
        """What the rig shows now for the parts about to change, in the terms `undo` puts back."""
        before: dict[str, Any] = {}
        if "look" in parts and isinstance(dig(snap, "video.look"), dict):
            before["look"] = dict(snap["video"]["look"])
        if "scene" in parts and dig(snap, "video.program_scene"):
            before["scene"] = snap["video"]["program_scene"]
        if "blur" in parts and dig(snap, "video.blur.on") is not None:
            before["blur"] = {"on": bool(snap["video"]["blur"]["on"]), "strength": dig(snap, "video.blur.strength")}
        if "voice" in parts and dig(snap, "audio.chain.preset"):
            before["voice"] = snap["audio"]["chain"]["preset"]
        if "lighting" in parts and isinstance(dig(snap, "lighting.master"), dict):
            master = snap["lighting"]["master"]
            before["lighting"] = {
                "on": bool(master.get("on")),
                "brightness": master.get("brightness"),
                "kelvin": master.get("kelvin"),
            }
        if "canon" in parts:
            controls = dig(snap, "video.canon.controls") or {}
            before["canon"] = {cid: controls[cid]["value"] for cid in ("exposure_comp", "kelvin") if cid in controls}
        return before

    def _look_values(self, snap: dict[str, Any], name: str) -> dict[str, Any] | None:
        for item in dig(snap, "video.looks") or []:
            if isinstance(item, Mapping) and item.get("name") == name and isinstance(item.get("values"), Mapping):
                return dict(item["values"])
        return None

    def _preset(self, snap: dict[str, Any], name: str) -> dict[str, Any] | None:
        for item in dig(snap, "lighting.presets") or []:
            if isinstance(item, Mapping) and item.get("name") == name:
                return dict(item)
        return None

    def _fade_lights(self, snap: dict[str, Any], preset: dict[str, Any], seconds: float) -> None:
        """Walk the lights from where they are to the preset in small steps (a preset on its own is a jump)."""
        master = dig(snap, "lighting.master") or {}
        start_pct, start_k = master.get("brightness"), master.get("kelvin")
        goal_pct, goal_k = preset.get("brightness"), preset.get("kelvin")
        if not (master.get("on") and preset.get("on") and seconds > 0):
            return
        if not all(isinstance(v, (int, float)) for v in (start_pct, start_k, goal_pct, goal_k)):
            return
        steps = max(2, min(MAX_FADE_STEPS, round(seconds / FADE_STEP_SECONDS)))
        for i in range(1, steps):  # the last step is the preset itself
            if self._stop.is_set():
                return
            t = i / steps
            try:
                self.dispatch(
                    "lighting.brightness", {"id": "all", "percent": round(start_pct + (goal_pct - start_pct) * t)}
                )
                self.dispatch("lighting.temperature", {"id": "all", "kelvin": round(start_k + (goal_k - start_k) * t)})
            except ActionError as exc:
                log.info("lights fade stopped: %s", exc.message)
                return
            self._sleep(seconds / steps)

    def _seed_director(self, slot: str, persona: str, look: dict, light: tuple[float, float] | None) -> None:
        loc = self._location(self._now())
        place = f"{loc.latitude:.1f},{loc.longitude:.1f}"  # the key `director.pilot` stores a learned look under
        learning.record(place, persona, slot, look, manual=True, light=light)

    def _apply(self, slot: str, reason: str) -> str:
        data = self.store.load()
        profile = data["profiles"].get(slot)
        label = slots.LABELS[slot]
        if not profile:
            raise ActionError(f"The {label.lower()} slot has no profile yet.", 409)
        if not self._apply_lock.acquire(blocking=False):
            raise ActionError("A profile is already being applied.", 409)
        try:
            snap = self._snapshot()
            director_on = bool(dig(snap, "auto.running"))
            steps = self._steps(profile, director_on)
            results: list[dict[str, Any]] = []
            before = self._capture(snap, {part for part, _, _ in steps})
            expect: dict[str, Any] = {}
            for part, action, params in steps:
                results.append(self._run_step(part, action, params, snap, data["fade_seconds"], expect))
            if director_on:
                results.extend(self._seed_parts(slot, profile, snap))
            with self._lock:
                self._applied = {
                    "slot": slot,
                    "label": label,
                    "reason": reason,
                    "at": self._now().strftime("%H:%M:%S"),
                    "director": director_on,
                    "parts": results,
                    "expect": expect,
                    "settled_at": self._clock()
                    + SETTLE_SECONDS
                    + (data["fade_seconds"] if "lighting" in expect else 0.0),
                }
                self._undo = before if before and any(r["state"] == "applied" for r in results) else None
                self._held = None
                self._suggestion = None
                self._watched_at = 0.0
            return self._record(label, reason, results, director_on)
        finally:
            self._apply_lock.release()

    def _run_step(
        self, part: str, action: str, params: dict[str, Any], snap: dict[str, Any], fade: float, expect: dict[str, Any]
    ) -> dict[str, Any]:
        value = (
            params.get("name") or params.get("value") or (("on" if params["on"] else "off") if "on" in params else "")
        )
        shown = f"{params['id'].replace('_', ' ')} {value}" if part == "canon" else str(value)
        result = {"part": part, "label": slots.PART_LABELS[part], "value": shown, "state": "applied", "detail": ""}
        try:
            if part == "lighting":
                preset = self._preset(snap, params["name"])
                if preset is not None:
                    self._fade_lights(snap, preset, fade)
            self.dispatch(action, params)
        except ActionError as exc:
            result.update(state="skipped", detail=exc.message)
            return result
        self._expect(part, params, snap, expect)
        return result

    def _expect(self, part: str, params: dict[str, Any], snap: dict[str, Any], expect: dict[str, Any]) -> None:
        """Remember what the rig should show for this piece, so a change made by somebody else is noticed."""
        if part == "look":
            values = self._look_values(snap, params["name"])
            if values is not None:
                expect["look"] = values
        elif part == "canon":
            expect.setdefault("canon", {})[params["id"]] = str(params["value"])
        elif part == "blur":
            expect["blur"] = bool(params["on"])
        else:
            expect[part] = params["name"]

    def _seed_parts(self, slot: str, profile: dict[str, Any], snap: dict[str, Any]) -> list[dict[str, Any]]:
        """With the Director on: give it the look and light of this slot as its baseline instead of writing them."""
        out: list[dict[str, Any]] = []
        persona = dig(snap, "auto.director.persona") or settings.DEFAULT_PERSONA
        director = slots.director_slot(slot, self._hour_angle)
        look = self._look_values(snap, profile["look"]) if "look" in profile else None
        light: tuple[float, float] | None = None
        preset = self._preset(snap, profile["lighting"]) if "lighting" in profile else None
        if preset and preset.get("on") and isinstance(preset.get("brightness"), (int, float)):
            light = (float(preset["brightness"]) / 100.0, float(preset["kelvin"]))
        if look is None and light is None:
            return out
        try:
            self._seed(director, persona, look or {}, light)
        except (OSError, ValueError, settings.SettingsError) as exc:
            log.warning("could not seed the Director's baseline: %s", exc)
            detail = f"The Director's baseline could not be saved: {exc}"
            return [
                {"part": p, "label": slots.PART_LABELS[p], "value": profile[p], "state": "skipped", "detail": detail}
                for p in ("look", "lighting")
                if p in profile
            ]
        for part in ("look", "lighting"):
            if part in profile:
                out.append(
                    {
                        "part": part,
                        "label": slots.PART_LABELS[part],
                        "value": profile[part],
                        "state": "seeded",
                        "detail": f"The Director is running: this is now its {slots.LABELS[director].lower()} look.",
                    }
                )
        for part in ("canon", "voice"):
            if part in profile:
                out.append(
                    {
                        "part": part,
                        "label": slots.PART_LABELS[part],
                        "value": profile[part] if isinstance(profile[part], str) else "settings",
                        "state": "skipped",
                        "detail": "The Director is running and owns this.",
                    }
                )
        return out

    def _record(self, label: str, reason: str, results: list[dict[str, Any]], director_on: bool) -> str:
        done = [f"{r['label'].lower()} {r['value']}" for r in results if r["state"] == "applied"]
        seeded = [r["label"].lower() for r in results if r["state"] == "seeded"]
        skipped = [f"{r['label'].lower()} ({r['detail']})" for r in results if r["state"] == "skipped"]
        parts = []
        if done:
            parts.append("set " + ", ".join(done))
        if seeded:
            parts.append("gave the Director its baseline for " + " and ".join(seeded))
        if skipped:
            parts.append("skipped " + "; ".join(skipped))
        text = f"{label} profile applied ({reason}): " + ("; ".join(parts) if parts else "nothing to change")
        failed = bool(skipped) and not done and not seeded
        self._log("warn" if failed else "ok", text)
        if not failed:
            self._say("ok", f"{label} profile applied")
        else:
            self._say("warn", f"The {label.lower()} profile could not be applied: see the Camera page")
        if failed:
            raise ActionError(text, 409)
        return f"{label} profile applied"

    # -- watching for manual changes ---------------------------------------------------------------------------

    def _watch(self) -> None:
        """Compare the rig with what the profile set; a difference is a person, and the profile steps back."""
        with self._lock:
            applied, held, clock = self._applied, self._held, self._clock()
            if not applied or held or clock < applied["settled_at"] or clock - self._watched_at < WATCH_SECONDS:
                return
            self._watched_at = clock
            expect = dict(applied["expect"])
        if not expect:
            return
        try:
            snap = self._snapshot()
        except ActionError:
            return
        changed = self._changed(expect, snap)
        if changed is None:
            return
        part, now_shows = changed
        label = slots.PART_LABELS[part]
        with self._lock:
            if self._held or self._applied is not applied:
                return
            self._held = {"part": part, "label": label, "since": self._now().strftime("%H:%M:%S"), "shows": now_shows}
            slot_label = applied["label"]
        self._log(
            "info",
            f"You changed the {label.lower()} ({now_shows}): the {slot_label.lower()} profile waits for the next slot",
        )

    @staticmethod
    def _changed(expect: dict[str, Any], snap: dict[str, Any]) -> tuple[str, str] | None:
        if "look" in expect:
            current = dig(snap, "video.look")
            if isinstance(current, Mapping) and not _same_look(current, expect["look"]):
                return "look", "adjusted"
        if "scene" in expect and not dig(snap, "video.privacy"):
            current = dig(snap, "video.program_scene")
            if current is not None and current != expect["scene"]:
                return "scene", str(current)
        if "blur" in expect:
            current = dig(snap, "video.blur.on")
            if current is not None and bool(current) != expect["blur"]:
                return "blur", "on" if current else "off"
        if "voice" in expect:
            current = dig(snap, "audio.chain.preset")
            if current is not None and current != expect["voice"]:
                return "voice", str(current)
        if "lighting" in expect and dig(snap, "lighting.lights"):
            current = dig(snap, "lighting.preset")
            if current != expect["lighting"]:
                return "lighting", str(current) if current else "adjusted"
        for cid, wanted in (expect.get("canon") or {}).items():
            current = dig(snap, f"video.canon.controls.{cid}.value")
            if current is not None and str(current) != wanted:
                return "canon", f"{cid.replace('_', ' ')} {current}"
        return None

    # -- state -----------------------------------------------------------------------------------------------------

    def _strip(self, wall: datetime, loc: daylight.Location, profiles: dict[str, Any]) -> list[dict[str, Any]]:
        key = f"{wall.date().isoformat()}|{loc.latitude}|{loc.longitude}"
        cached = self._strip_cache
        if cached and cached[0] == key:
            return cached[1]
        value = slots.strip(wall, loc, STRIP_STEP)
        self._strip_cache = (key, value)
        return value

    @staticmethod
    def _options(snap: dict[str, Any]) -> dict[str, Any]:
        controls = dig(snap, "video.canon.controls") or {}
        exposure = controls.get("exposure_comp") or {}
        kelvin = controls.get("kelvin") or {}
        present = bool(dig(snap, "video.canon.available")) and bool(exposure or kelvin)
        return {
            "looks": _names(dig(snap, "video.looks")),
            "scenes": _names(dig(snap, "video.scenes")),
            "voices": _names(dig(snap, "audio.chain.presets")),
            "lighting": _names(dig(snap, "lighting.presets")),
            "blur": bool(dig(snap, "video.blur.available")) or dig(snap, "video.blur.available") is None,
            "canon": {
                "present": present,
                "exposure_choices": list(exposure.get("choices") or []),
                "exposure_readonly": bool(exposure.get("readonly")),
                "kelvin_range": list(kelvin.get("range") or [2500, 10000, 100])[:2],
            },
        }

    def state(self, snap: dict[str, Any]) -> dict[str, Any]:
        """The `slots` block. `snap` is the snapshot being built: the controller never asks the rig for another."""
        data = self.store.load()
        wall = self._now()
        out = empty_state()
        try:
            loc = self._location(wall)
            slot, _ = slots.slot_at(wall, loc)
            strip = self._strip(wall, loc, data["profiles"])
        except (ValueError, OSError) as exc:  # a bad location or time zone must not blank the page
            log.warning("slot strip unavailable: %s", exc)
            return {**out, "available": True, "mode": data["mode"], "fade_seconds": data["fade_seconds"]}
        director_on = bool(dig(snap, "auto.running"))
        with self._lock:
            applied, held, suggestion = self._applied, self._held, self._suggestion
            events = list(self._events)
            can_undo = self._undo is not None
        out.update(
            available=True,
            mode=data["mode"],
            fade_seconds=data["fade_seconds"],
            slots=[
                {
                    "id": s,
                    "label": slots.LABELS[s],
                    "profile": data["profiles"].get(s, {}),
                    "empty": not data["profiles"].get(s),
                }
                for s in slots.SLOTS
            ],
            current={
                "slot": slot,
                "label": slots.LABELS[slot],
                "minute": wall.hour * 60 + wall.minute,
                "clock": wall.strftime("%H:%M"),
            },
            strip=strip,
            options=self._options(snap),
            director={"on": director_on},
            events=events,
        )
        out["status"] = self._status(data, slot, applied, held, suggestion, can_undo, director_on)
        return out

    @staticmethod
    def _status(
        data: dict[str, Any],
        slot: str,
        applied: dict[str, Any] | None,
        held: dict[str, Any] | None,
        suggestion: dict[str, Any] | None,
        can_undo: bool,
        director_on: bool,
    ) -> dict[str, Any]:
        label = slots.LABELS[slot]
        shown = {k: applied[k] for k in ("slot", "label", "reason", "at", "director", "parts")} if applied else None
        profile = data["profiles"].get(slot)
        engaged = bool(held) or bool(applied)  # applied by hand while profiles are off
        if data["mode"] == "off" and not engaged:
            state, summary = "off", f"Profiles are off. {label} has {'a profile' if profile else 'no profile'}."
        elif held:
            state = "held"
            summary = (
                f"You changed the {held['label'].lower()}, so the {label.lower()} profile waits until the slot ends."
            )
        elif suggestion:
            state, summary = "suggested", f"The {suggestion['label'].lower()} profile is ready to apply."
        elif applied:
            state = "applied"
            summary = (
                f"The {applied['label'].lower()} profile is in place (applied {applied['at']}, {applied['reason']})."
            )
        elif not profile:
            state, summary = "idle", f"{label} has no profile. Pick what it should do below."
        else:
            state = "idle"
            summary = f"Waiting: the {label.lower()} profile applies when a call starts or the slot changes."
        if director_on and state not in ("off", "held"):
            summary += " The AI Director is running, so the look and lights are its baselines."
        return {
            "state": state,
            "summary": summary,
            "applied": shown,
            "held": dict(held) if held else None,
            "suggestion": dict(suggestion) if suggestion else None,
            "can_undo": can_undo,
        }

    def export(self) -> dict[str, Any]:
        return self.store.export_document()

    # -- actions ------------------------------------------------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        try:
            return self._act(action, params)
        except slots.SlotsError as exc:
            raise ActionError(str(exc), 400) from exc
        except OSError as exc:
            raise ActionError(f"Could not save the profiles: {exc.strerror or exc}", 409) from exc

    def _act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "slots.mode":
            mode = slots.check_mode(params.get("mode"))
            self.store.set_mode(mode)
            with self._lock:
                self._suggestion = None
            return {"off": "Profiles off", "suggest": "Profiles suggest", "auto": "Profiles apply by themselves"}[mode]
        if action == "slots.fade":
            self.store.set_fade(params.get("seconds"))
            return None
        if action == "slots.profile":
            slot = slots.check_slot(params.get("slot"))
            profile = slots.clean_profile(params.get("profile"))
            self._check_known(profile)
            self.store.set_profile(slot, profile)
            return f"{slots.LABELS[slot]} profile saved" if profile else f"{slots.LABELS[slot]} profile cleared"
        if action == "slots.clear":
            slot = slots.check_slot(params.get("slot"))
            self.store.clear_profile(slot)
            return f"{slots.LABELS[slot]} profile cleared"
        if action == "slots.save_current":
            return self._save_current(slots.check_slot(params.get("slot")))
        if action == "slots.starter":
            _, filled = self.store.fill_starter()
            if not filled:
                return "Every slot already has a profile"
            return "Starter profiles filled in: " + ", ".join(slots.LABELS[s] for s in filled)
        if action == "slots.apply":
            slot = params.get("slot")
            if slot is None:
                with self._lock:
                    slot = self._slot
                if slot is None:
                    slot, _ = slots.slot_at(self._now(), self._location(self._now()))
            return self._apply(slots.check_slot(slot), "you asked for it")
        if action == "slots.undo":
            return self._undo_apply()
        if action == "slots.resume":
            with self._lock:
                self._held = None
            return "Profiles are in charge again"
        if action == "slots.dismiss":
            with self._lock:
                self._suggestion = None
            return None
        if action == "slots.import":
            self.store.import_document(params.get("document"))
            return "Profiles imported"
        if action == "slots.reset":
            self.store.reset()
            with self._lock:
                self._applied = self._undo = self._held = self._suggestion = None
            return "Profiles are back to their defaults"
        if action.startswith("slots.demo."):
            return self._demo(action, params)
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _demo(self, action: str, params: dict[str, Any]) -> str | None:
        raise ActionError("Only the demo rig can move its clock.", 409)

    def _check_known(self, profile: dict[str, Any]) -> None:
        """A name that the rig can list must be one of them (a typo is better refused here than skipped at 5 am)."""
        if self.rig is None:
            return
        options = self._options(self.rig.snapshot())
        for part, key, noun in (
            ("look", "looks", "look"),
            ("scene", "scenes", "scene"),
            ("voice", "voices", "voice preset"),
            ("lighting", "lighting", "lighting preset"),
        ):
            known = options[key]
            if part in profile and known and profile[part] not in known:
                raise ActionError(f"There is no {noun} called {profile[part]}.", 400)
        canon = profile.get("canon", {})
        choices = options["canon"]["exposure_choices"]
        if "exposure_comp" in canon and choices and canon["exposure_comp"] not in choices:
            raise ActionError(f"The camera offers exposure compensation {', '.join(choices)}.", 400)

    def _save_current(self, slot: str) -> str:
        """Store what the rig is doing now as `slot`'s profile (a look or lights no preset matches is saved too)."""
        snap = self._snapshot()
        label = slots.LABELS[slot]
        profile: dict[str, Any] = {}
        look = dig(snap, "video.look")
        if isinstance(look, Mapping):
            named = next(
                (i["name"] for i in dig(snap, "video.looks") or [] if _same_look(look, i.get("values") or {})), None
            )
            if named is None:
                named = f"{label} look"
                self.dispatch("video.looks.save", {"name": named})
            profile["look"] = named
        if dig(snap, "video.program_scene") and not dig(snap, "video.privacy"):
            profile["scene"] = snap["video"]["program_scene"]
        if dig(snap, "video.blur.on") is not None:
            profile["blur"] = {"on": bool(snap["video"]["blur"]["on"])}
        if dig(snap, "audio.chain.preset"):
            profile["voice"] = snap["audio"]["chain"]["preset"]
        if dig(snap, "lighting.lights"):
            preset = dig(snap, "lighting.preset")
            if preset is None and dig(snap, "lighting.master.on"):
                preset = f"{label} light"
                self.dispatch("lighting.presets.save", {"name": preset})
            if preset:
                profile["lighting"] = preset
        controls = dig(snap, "video.canon.controls") or {}
        canon = {}
        if controls.get("exposure_comp") and not controls["exposure_comp"].get("readonly"):
            canon["exposure_comp"] = controls["exposure_comp"]["value"]
        kelvin = controls.get("kelvin", {}).get("value")
        if kelvin is not None and controls.get("whitebalance", {}).get("value") == "Color Temperature":
            canon["kelvin"] = int(float(kelvin))
        if canon:
            profile["canon"] = canon
        self.store.set_profile(slot, slots.clean_profile(profile))
        return f"{label} profile saved from what is on now"

    def _undo_apply(self) -> str:
        with self._lock:
            before, applied = self._undo, self._applied
        if before is None:
            raise ActionError("There is nothing to undo.", 409)
        failed: list[str] = []

        def run(action: str, params: dict[str, Any]) -> None:
            try:
                self.dispatch(action, params)
            except ActionError as exc:
                failed.append(exc.message)

        if "scene" in before:
            run("video.scene", {"name": before["scene"]})
        if "blur" in before:
            blur = {"on": before["blur"]["on"]}
            if before["blur"].get("strength") is not None:
                blur["strength"] = before["blur"]["strength"]
            run("video.blur", blur)
        if "look" in before:
            run("video.look", {"values": before["look"]})
        for cid, value in (before.get("canon") or {}).items():
            run("video.canon_set", {"id": cid, "value": value})
        if "voice" in before:
            run("audio.preset", {"name": before["voice"]})
        if "lighting" in before:
            light = before["lighting"]
            if light["on"] and isinstance(light.get("brightness"), (int, float)):
                run("lighting.brightness", {"id": "all", "percent": light["brightness"]})
                run("lighting.temperature", {"id": "all", "kelvin": light["kelvin"]})
            run("lighting.power", {"id": "all", "on": light["on"]})
        label = applied["label"] if applied else "The"
        with self._lock:
            self._undo = None
            self._applied = None
            self._held = {
                "part": "undo",
                "label": "profile",
                "since": self._now().strftime("%H:%M:%S"),
                "shows": "undone",
            }
        text = f"{label} profile undone: the picture is back where it was"
        self._log("warn" if failed else "info", text + (f" (not everything: {'; '.join(failed)})" if failed else ""))
        return text


# ---------------------------------------------------------------------------
# The demo rig
# ---------------------------------------------------------------------------


class DemoSlots(SlotsController):
    """The same controller over a throwaway store and a clock the e2e suite (or a person) can move.

    `STREAMSTACK_DEMO_CLOCK` pins the day and the place, as for the lighting automation; `slots.demo.clock {time}`
    moves the clock within that day and runs a tick at once, so the effect of crossing a slot is visible right away.
    """

    def __init__(self, dispatch: Dispatch, in_call: Callable[[], bool] = lambda: False) -> None:
        pinned = lightpanel.demo_clock()
        self._pinned = pinned
        self._offset = timedelta(0)
        self._real_start = datetime.now().astimezone()
        kwargs: dict[str, Any] = {}
        if pinned is not None:
            kwargs["location"] = lambda when: daylight.resolve_location(None, None, when, zone=lightpanel.DEMO_ZONE)
        # the demo never touches the person's real profiles: its file lives in a throwaway directory
        self._scratch = tempfile.TemporaryDirectory(prefix="streamstack-slots-")
        super().__init__(
            dispatch,
            slots.Store(Path(self._scratch.name) / "slot-profiles.json", ephemeral=True),
            now=self._clock_now,
            in_call=in_call,
            seed=lambda slot, persona, look, light: None,
            sleep=lambda seconds: None,
            tick_seconds=0.25,
            **kwargs,
        )
        self.seeded: list[tuple[str, str, dict, tuple[float, float] | None]] = []
        self._seed = self._demo_seed

    def _demo_seed(self, slot: str, persona: str, look: dict, light: tuple[float, float] | None) -> None:
        self.seeded.append((slot, persona, dict(look), light))

    def _clock_now(self) -> datetime:
        if self._pinned is not None:
            return self._pinned + self._offset
        return datetime.now().astimezone() + self._offset

    def _demo(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "slots.demo.reset":  # the e2e suite's reset: no profiles, the clock where the demo started
            self.store.reset()
            self._offset = timedelta(0)
            with self._lock:
                self._applied = self._undo = self._held = self._suggestion = None
                self._slot = self._call = None
                self._events.clear()
            return None
        if action != "slots.demo.clock":
            raise ActionError(f"Unknown action {action!r}.", 400)
        text = params.get("time")
        try:
            hours, minutes = (int(x) for x in str(text).split(":", 1))
            if not (0 <= hours < 24 and 0 <= minutes < 60):
                raise ValueError(text)
        except ValueError as exc:
            raise ActionError("time must be HH:MM, for example 05:30.", 400) from exc
        base = self._pinned or datetime.now().astimezone()
        target = base.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        self._offset = target - (self._pinned if self._pinned is not None else datetime.now().astimezone())
        self.tick()
        return f"Demo clock {hours:02d}:{minutes:02d}"
