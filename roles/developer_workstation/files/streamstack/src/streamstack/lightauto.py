"""Lighting automation: camera follow and the daylight schedule, one engine, one owner of the light at a time.

`Automation.tick()` runs about once a second, in the `streamstack-lights` user service (so it works with the Console
closed) and, in the demo rig, inside the Console. Each tick it

1. notices **manual changes**: a light whose power, brightness or temperature is not what the engine last set was
   changed by a person (the Console, the light's own buttons, a preset). That light is *held*: the schedule leaves it
   alone for `hold_minutes`, or until the next camera session, or until the light is switched off and on again.
2. runs **camera follow**: when something has used the camera for `start_delay_seconds` a session begins and every
   light that is off is switched on (at the schedule's level when the schedule owns the light, with a fade). When
   nothing has used the camera for `grace_seconds` the session ends and the lights *this engine switched on* are
   faded out and switched off. A light that was already on, one the person switched off during the session, and one
   they changed by hand are left exactly as they are.
3. applies the **schedule** to every light that is on, not held and not manual: the level for this time of day,
   faded when it is a big jump, stepped in small writes when the sun moves it gradually.

Who owns what (one owner at a time, and nobody fights):

====================  ===================================  ===================================
                      power                                brightness and colour temperature
====================  ===================================  ===================================
camera follow         switches on, and off after a call    never (it only sets the first level)
daylight schedule     never                                owns them, unless the Director does
AI Director           never (it does not touch power)      owns them while it drives the lights
a person              always wins                          always wins (the engine holds off)
====================  ===================================  ===================================

The Director "drives the lights" when `streamstack-auto.service` is active and `STREAMSTACK_AUTO_LIGHTS` is on; the
schedule then yields and the status line says so. The engine reads the lights through a `LightingController`
(`console.lighting`) so hotplug, locking and the hidraw protocol are the same code the Console uses.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from streamstack import config, lighting, lightplan, system
from streamstack.camerause import CameraWatcher, Usage, watched_devices
from streamstack.console.errors import ActionError, Superseded
from streamstack.director import daylight, settings

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
LUMEN_EPS = 4  # lumen: below this a difference is rounding, above it somebody moved the light
KELVIN_EPS = 100  # one step of the light's own scale
SETTLE_SECONDS = 2.0  # after the engine's own write the light's answer is not compared
DIRECTOR_CHECK_SECONDS = 10.0
DIRECTOR_UNIT = "streamstack-auto.service"
HEARTBEAT_SECONDS = 3.0
HEARTBEAT_FRESH = 10.0  # a status older than this means the service is not running
RESTORE_SECONDS = 300.0  # a restarted service picks its session back up when the last status is this fresh
EVENTS_KEPT = 30
FADE_STEP = 0.25
LOCATION_CACHE = 60.0

OWNER_LABELS = {
    "schedule": "The daylight schedule",
    "director": "The AI Director",
    "none": "Nobody (the schedule is off)",
}


class Lights(Protocol):
    """What the engine needs from the Console's lighting controller."""

    def lights_view(self) -> list[dict[str, Any]]: ...

    def write(
        self,
        light_id: str,
        *,
        power: bool | None = None,
        percent: float | None = None,
        kelvin: float | None = None,
        expect: tuple[bool | None, int | None, int | None] | None = None,
    ) -> None: ...


class Watcher(Protocol):
    """What the engine needs to know about the camera: `CameraWatcher`, or the demo rig's scripted one."""

    def poll(self, follow: dict[str, Any], devices: set[str]) -> Usage: ...


class _Run:
    """What the engine remembers about one light between ticks."""

    def __init__(self) -> None:
        self.power: bool | None = None  # the power, brightness (lumen) and temperature the engine last knew
        self.lumen: int | None = None
        self.kelvin: int | None = None
        self.wrote = float("-inf")
        self.ours = False  # the engine switched it on for the camera session
        self.held_at: float | None = None
        self.held_wall: datetime | None = None
        self.session = 0  # the last camera session this light was handled for
        self.state = ""  # one line for the page


def _clock(when: datetime) -> str:
    return when.strftime("%H:%M")


class Automation:
    def __init__(
        self,
        lights: Lights,
        store: lightplan.Store,
        watcher: Watcher,
        devices: Callable[[], set[str]],
        director_active: Callable[[], bool] = lambda: False,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        location: Callable[[datetime], daylight.Location] | None = None,
        status_file: Path | None = None,
        sleep: Callable[[float], None] | None = None,
        tick_seconds: float = TICK_SECONDS,
        where: str = "service",
        director_check_seconds: float = DIRECTOR_CHECK_SECONDS,
    ) -> None:
        self.lights, self.store, self.watcher, self.devices = lights, store, watcher, devices
        self._director = director_active
        self.clock, self.now = clock, now
        self._location_of = location or (
            lambda when: daylight.resolve_location(settings.load(), config.load_settings(), when)
        )
        self.status_file = status_file
        self.tick_seconds, self.where = tick_seconds, where
        self._director_every = director_check_seconds
        self._stop = threading.Event()
        self._sleep = sleep or self._pause
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._runs: dict[str, _Run] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        self._session: dict[str, Any] | None = None
        self._session_count = 0
        self._active_since: float | None = None
        self._last_active: float = 0.0
        self._usage = Usage()
        self._seen_resume = self.store.load()["resume"]
        self._director_cache: tuple[float, bool] = (float("-inf"), False)
        self._location_cache: tuple[float, daylight.Location] | None = None
        self._status: dict[str, Any] = {}
        self._published: tuple[str, float] = ("", float("-inf"))
        self._restore()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="lights-automation", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def run(self) -> None:
        """The loop: tick, then wait. A bug in one tick is logged and the next tick tries again."""
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("lighting automation tick failed")
            self._stop.wait(self.tick_seconds)

    def _pause(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # -- one tick -------------------------------------------------------------

    def tick(self) -> None:
        with self._lock:
            now, wall = self.clock(), self.now()
            data = self.store.load()
            views = [v for v in self.lights.lights_view() if v.get("connected")]
            if data["resume"] > self._seen_resume:
                self._seen_resume = data["resume"]
                self._resume(views, now, wall, "You asked the schedule to resume")
            self._forget_gone(views)
            self._usage = self.watcher.poll(data["follow"], self.devices())
            self._observe(views, data, now, wall)
            self._follow(views, data, now, wall)
            self._schedule(views, data, now, wall)
            self._publish(views, data, now, wall)

    def reset(self, restore: Callable[[], None]) -> None:
        """Put everything back as at start, in one step the engine cannot see the middle of (the demo rig's test reset).

        Under the engine's lock, so no tick runs while `restore` moves the lights and the settings, the engine forgets
        what it knew (holds, the camera session, the event log, what it believes each light shows) and takes one tick
        to learn the lights as they are now. Nothing here is noticed as a change by hand: the engine never compared
        the old state with the new one."""
        with self._lock:
            restore()
            self._runs.clear()
            self._events.clear()
            self._session = None
            self._active_since = None
            self._last_active = 0.0
            self._usage = Usage()
            self._seen_resume = self.store.load()["resume"]
            self._director_cache = (float("-inf"), False)
            self.tick()

    def _run_for(self, light_id: str) -> _Run:
        return self._runs.setdefault(light_id, _Run())

    def _forget_gone(self, views: list[dict[str, Any]]) -> None:
        present = {v["id"] for v in views}
        for light_id in [i for i in self._runs if i not in present]:
            del self._runs[light_id]

    # -- who owns brightness and colour ------------------------------------------

    def director_active(self, now: float) -> bool:
        checked, value = self._director_cache
        if now - checked >= self._director_every:
            try:
                value = bool(self._director())
            except Exception:
                log.exception("could not tell whether the AI Director is running")
                value = False
            self._director_cache = (now, value)
        return value

    def owner(self, data: dict[str, Any], now: float) -> str:
        if self.director_active(now):
            return "director"
        return "schedule" if data["schedule"]["enabled"] else "none"

    def _location(self, wall: datetime) -> daylight.Location:
        cached = self._location_cache
        mono = self.clock()
        if cached is None or mono - cached[0] >= LOCATION_CACHE:
            cached = (mono, self._location_of(wall))
            self._location_cache = cached
        return cached[1]

    def target(self, data: dict[str, Any], wall: datetime) -> dict[str, float]:
        return lightplan.level_at(data, wall, self._location(wall))

    # -- events -----------------------------------------------------------------

    def _event(self, wall: datetime, kind: str, text: str) -> None:
        self._events.appendleft({"time": _clock(wall), "ts": round(wall.timestamp()), "kind": kind, "text": text})
        log.info("lights: %s", text)

    # -- 1. manual changes ------------------------------------------------------------

    def _observe(self, views: list[dict[str, Any]], data: dict[str, Any], now: float, wall: datetime) -> None:
        hold_minutes = data["schedule"]["hold_minutes"]
        for view in views:
            run = self._run_for(view["id"])
            power, lumen, kelvin = view["power"], view["lumen"], view["kelvin"]
            if view.get("identifying") or power is None:
                continue
            if run.power is None:
                run.power, run.lumen, run.kelvin = power, lumen, kelvin
                continue
            if now - run.wrote < SETTLE_SECONDS:
                continue
            name = view["name"]
            if power != run.power:
                run.power, run.lumen, run.kelvin = power, lumen, kelvin
                run.held_at = run.held_wall = None
                if power:
                    self._event(wall, "manual", f"{name} was switched on by hand")
                else:
                    if run.ours:
                        self._event(wall, "manual", f"{name} was switched off by hand; it stays off")
                    run.ours = False
                continue
            if not power:
                run.lumen, run.kelvin = lumen, kelvin
                continue
            moved = (lumen is not None and run.lumen is not None and abs(lumen - run.lumen) >= LUMEN_EPS) or (
                kelvin is not None and run.kelvin is not None and abs(kelvin - run.kelvin) >= KELVIN_EPS
            )
            if moved:
                run.lumen, run.kelvin = lumen, kelvin
                run.held_at, run.held_wall = now, wall
                until = f"for {hold_minutes} min" if hold_minutes else "until the next camera session"
                self._event(wall, "manual", f"{name} was changed by hand; the schedule leaves it alone {until}")

    def _resume(self, views: list[dict[str, Any]], now: float, wall: datetime, why: str) -> None:
        """Hand the lights back to the schedule, starting from what they show now.

        A change by hand made within `SETTLE_SECONDS` of the engine's own write is only noticed once that window
        ends. Resuming adopts the lights' present state as the one the engine knows, so such a change cannot hold
        the light again a moment after the person asked the schedule to take it."""
        held = [r for r in self._runs.values() if r.held_at is not None]
        for run in self._runs.values():
            run.held_at = run.held_wall = None
        for view in views:
            if view.get("identifying") or view["power"] is None:
                continue
            run = self._run_for(view["id"])
            run.power, run.lumen, run.kelvin = view["power"], view["lumen"], view["kelvin"]
            run.wrote = float("-inf")
        if held:
            self._event(wall, "resume", f"{why}; the schedule has the lights again")

    # -- 2. camera follow ---------------------------------------------------------------

    def _follow(self, views: list[dict[str, Any]], data: dict[str, Any], now: float, wall: datetime) -> None:
        follow = data["follow"]
        usage = self._usage
        if not follow["enabled"]:
            if self._session is not None:
                self._end_session(views, data, now, wall, switch_off=False, why="Camera follow was switched off")
            self._active_since = None
            return
        if usage.active:
            self._last_active = now
            if self._active_since is None:
                self._active_since = now
        else:
            self._active_since = None
        session = self._session
        if session is None:
            if (
                usage.active
                and self._active_since is not None
                and now - self._active_since >= follow["start_delay_seconds"]
            ):
                self._begin_session(usage, wall)
        elif usage.active:
            session["reason"] = usage.reason
            session["apps"] = [c.public() for c in usage.consumers]
        elif now - self._last_active >= follow["grace_seconds"]:
            self._end_session(
                views, data, now, wall, switch_off=follow["turn_off"], why="Nothing uses the camera any more"
            )
        if self._session is not None:
            for view in views:
                self._enroll(view, data, now, wall)

    def _begin_session(self, usage: Usage, wall: datetime) -> None:
        self._session_count += 1
        self._session = {
            "id": self._session_count,
            "since": _clock(wall),
            "ts": round(wall.timestamp()),
            "reason": usage.reason,
            "apps": [c.public() for c in usage.consumers],
        }
        for run in self._runs.values():
            run.held_at = run.held_wall = None  # a manual change wins only until the next camera session
        self._event(wall, "camera-on", f"{usage.reason} at {_clock(wall)}")

    def _enroll(self, view: dict[str, Any], data: dict[str, Any], now: float, wall: datetime) -> None:
        """Handle one light once per camera session: switch it on if it is off."""
        session = self._session
        run = self._run_for(view["id"])
        if session is None or run.session == session["id"] or view.get("identifying"):
            return
        run.session = session["id"]
        light = lightplan.light_settings(data, view["id"])
        name = view["name"]
        if light["role"] == "manual":
            return
        if view["power"] is not False:
            run.ours = False  # already on (or unknown): not ours to switch off later
            return
        goal = None
        if self.owner(data, now) == "schedule":
            goal = lightplan.for_light(self.target(data, wall), light, data["schedule"]["fill_percent"])
        fade = data["schedule"]["fade_seconds"]
        try:
            if goal is None:
                self._write(view, run, now, power=True)
            elif fade > 0:
                self._write(view, run, now, percent=1, kelvin=goal[1])
                self._write(view, run, now, power=True)
                self._fade(view, run, now, 1, goal[0], goal[1], goal[1], fade)
            else:
                self._write(view, run, now, percent=goal[0], kelvin=goal[1], power=True)
        except Superseded:
            run.session = 0  # somebody moved the light under us: look at it again on the next tick
            return
        except ActionError as exc:
            self._event(wall, "error", f"Could not switch {name} on: {exc}")
            return
        run.ours = True
        level = f" at {goal[0]:.0f}%, {goal[1]} K" if goal else ""
        self._event(wall, "on", f"{name} switched on{level} because {session['reason']}")

    def _end_session(
        self,
        views: list[dict[str, Any]],
        data: dict[str, Any],
        now: float,
        wall: datetime,
        *,
        switch_off: bool,
        why: str,
    ) -> None:
        self._session = None
        fade = data["schedule"]["fade_seconds"]
        switched: list[str] = []
        kept: list[str] = []
        for view in views:
            run = self._run_for(view["id"])
            name = view["name"]
            if not run.ours:
                continue
            run.ours = False
            if not switch_off:
                continue
            if view["power"] is not True:
                continue
            if run.held_at is not None:
                kept.append(name)
                continue
            self._switch_off(view, run, now, wall, fade)
            switched.append(name)
        if not switch_off:
            self._event(wall, "camera-off", f"{why}; the lights stay as they are")
        elif switched:
            self._event(wall, "camera-off", f"{why}; {', '.join(switched)} switched off at {_clock(wall)}")
        else:
            self._event(wall, "camera-off", f"{why}; no light needed switching off")
        for name in kept:
            self._event(wall, "kept", f"{name} stays on: you changed it during the session")

    def _switch_off(self, view: dict[str, Any], run: _Run, now: float, wall: datetime, fade: float) -> None:
        """Fade down, switch off, then put the brightness back so the light wakes at its old level next time."""
        model = lighting.MODELS[int(view["pid"], 16)]
        percent = lighting.lumen_to_percent(model, view["lumen"]) if view["lumen"] is not None else None
        try:
            if fade > 0 and percent is not None and percent > 1:
                self._fade(view, run, now, percent, 1, view["kelvin"], view["kelvin"], fade)
            self._write(view, run, now, power=False)
            if percent is not None:
                self._write(view, run, now, percent=percent)  # accepted while off without waking the light
        except Superseded:
            log.info("lights: %s was changed while it was being switched off; left as it is", view["name"])
        except ActionError as exc:
            self._event(wall, "error", f"Could not switch {view['name']} off: {exc}")

    # -- 3. the schedule -----------------------------------------------------------------

    def _schedule(self, views: list[dict[str, Any]], data: dict[str, Any], now: float, wall: datetime) -> None:
        owner = self.owner(data, now)
        hold_minutes = data["schedule"]["hold_minutes"]
        fill = data["schedule"]["fill_percent"]
        fade = data["schedule"]["fade_seconds"]
        level = self.target(data, wall) if owner == "schedule" else None
        for view in views:
            run = self._run_for(view["id"])
            light = lightplan.light_settings(data, view["id"])
            run.state = self._light_state(view, run, light, owner, hold_minutes, now)
            if run.held_at is not None and hold_minutes and now - run.held_at >= hold_minutes * 60:
                run.held_at = run.held_wall = None
                self._event(wall, "resume", f"The schedule has {view['name']} again after {hold_minutes} minutes")
                run.state = self._light_state(view, run, light, owner, hold_minutes, now)
            if (
                level is None
                or light["role"] == "manual"
                or view["power"] is not True
                or run.held_at is not None
                or view.get("identifying")
                or now - run.wrote < SETTLE_SECONDS  # a change right after our write is not ours to undo yet
            ):
                continue
            goal = lightplan.for_light(level, light, fill)
            if goal is None or view["lumen"] is None or view["kelvin"] is None:
                continue
            percent, kelvin = goal
            model = lighting.MODELS[int(view["pid"], 16)]
            lumen = lighting.percent_to_lumen(model, percent)
            if abs(lumen - view["lumen"]) < LUMEN_EPS and abs(kelvin - view["kelvin"]) < KELVIN_EPS:
                continue
            try:
                if fade > 0 and lightplan.fade_needed(model, view["lumen"], percent, view["kelvin"], kelvin):
                    start = lighting.lumen_to_percent(model, view["lumen"])
                    self._fade(view, run, now, start, percent, view["kelvin"], kelvin, fade)
                else:
                    self._write(view, run, now, percent=percent, kelvin=kelvin)
            except Superseded:
                continue  # a person moved it mid-write: the next tick sees that and holds the light
            except ActionError as exc:
                self._event(wall, "error", f"Could not set {view['name']}: {exc}")

    def _light_state(
        self, view: dict[str, Any], run: _Run, light: dict[str, Any], owner: str, hold_minutes: int, now: float
    ) -> str:
        if light["role"] == "manual":
            return "Manual: automation never touches this light"
        if view["power"] is False:
            return "Off"
        if run.held_at is not None:
            if hold_minutes and run.held_wall is not None:
                until = run.held_wall.timestamp() + hold_minutes * 60
                return f"Yours until {_clock(datetime.fromtimestamp(until, run.held_wall.tzinfo))}"
            return "Yours until the next camera session"
        if owner == "director":
            return "Brightness and colour follow the AI Director"
        if run.ours:
            return "On for the camera session" + (", following the schedule" if owner == "schedule" else "")
        return "Following the schedule" if owner == "schedule" else "Schedule is off"

    # -- writing ------------------------------------------------------------------------------

    def _write(
        self,
        view: dict[str, Any],
        run: _Run,
        now: float,
        *,
        power: bool | None = None,
        percent: float | None = None,
        kelvin: float | None = None,
    ) -> None:
        """One write through the controller; remembers it as the engine's own so it is not mistaken for a person.

        The write is a compare-and-set against what the engine last knew the light to show: a person's change that
        landed since (while a fade was walking, or between this tick's snapshot and now) is never written over.
        The write is dropped (`Superseded`) and the next tick sees the change and holds the light."""
        try:
            self.lights.write(
                view["id"], power=power, percent=percent, kelvin=kelvin, expect=(run.power, run.lumen, run.kelvin)
            )
        except Superseded:
            run.wrote = float("-inf")  # nothing of ours is in flight: the next tick compares at once and holds it
            raise
        model = lighting.MODELS[int(view["pid"], 16)]
        run.wrote = now
        if power is not None:
            run.power = power
        if percent is not None:
            run.lumen = lighting.percent_to_lumen(model, percent)
        if kelvin is not None:
            run.kelvin = lighting.clamp_kelvin(kelvin)

    def _fade(
        self,
        view: dict[str, Any],
        run: _Run,
        now: float,
        start: float,
        end: float,
        kelvin_start: float,
        kelvin_end: float,
        seconds: float,
    ) -> None:
        """Walk brightness and temperature from start to end in small writes over `seconds`."""
        steps = max(2, min(80, round(seconds / FADE_STEP)))
        for i in range(1, steps + 1):
            if self._stop.is_set():
                break
            fraction = i / steps
            self._write(
                view,
                run,
                now,
                percent=start + (end - start) * fraction,
                kelvin=kelvin_start + (kelvin_end - kelvin_start) * fraction,
            )
            if i < steps:
                self._sleep(seconds / steps)

    # -- status ---------------------------------------------------------------------------------

    def _summary(
        self, views: list[dict[str, Any]], data: dict[str, Any], now: float, wall: datetime, owner: str
    ) -> str:
        follow, session = data["follow"], self._session
        if not views:
            return "No Litra light is connected."
        if session is not None:
            if self._usage.active:
                return f"Camera in use since {session['since']}: {session['reason']}."
            left = max(0, round(follow["grace_seconds"] - (now - self._last_active)))
            if follow["turn_off"]:
                return f"The camera was released. Lights that came on for it go off in {left} s."
            return "The camera was released. Camera follow leaves the lights as they are."
        if not follow["enabled"] and not data["schedule"]["enabled"]:
            return "Automation is off. The lights only do what you tell them."
        if any(r.held_at is not None for r in self._runs.values()):
            return "You changed a light by hand. The schedule resumes after the hold, or at the next camera session."
        if owner == "director":
            return "The AI Director owns brightness and colour. Camera follow still switches power."
        if owner == "schedule":
            target = self.target(data, wall)
            slot = self._slot_label(wall)
            level = f"{target['brightness']:.0f}%, {target['kelvin']:.0f} K"
            return f"Following the {slot} level of the daylight schedule: {level}."
        return "The schedule is off. Camera follow switches the lights with the camera."

    def _slot_label(self, wall: datetime) -> str:
        loc = self._location(wall)
        elevation, _, hour_angle = daylight.solar_position(wall, loc.latitude, loc.longitude)
        return daylight.SLOT_LABELS[daylight.slot_for(elevation, hour_angle)].lower()

    def _build_status(
        self, views: list[dict[str, Any]], data: dict[str, Any], now: float, wall: datetime
    ) -> dict[str, Any]:
        owner = self.owner(data, now)
        follow, session = data["follow"], self._session
        grace_left = None
        if session is not None and not self._usage.active:
            grace_left = max(0, round(follow["grace_seconds"] - (now - self._last_active)))
        held_until: dict[str, str | None] = {}
        for light_id, run in self._runs.items():
            if run.held_at is not None and run.held_wall is not None and data["schedule"]["hold_minutes"]:
                end = datetime.fromtimestamp(
                    run.held_wall.timestamp() + data["schedule"]["hold_minutes"] * 60, run.held_wall.tzinfo
                )
                held_until[light_id] = _clock(end)
            else:
                held_until[light_id] = None
        return {
            "running": True,
            "where": self.where,
            "pid": os.getpid(),
            "heartbeat": round(time.time(), 1),
            "owner": owner,
            "owner_label": OWNER_LABELS[owner],
            "summary": self._summary(views, data, now, wall, owner),
            "camera": {
                **self._usage.public(),
                "grace_left": grace_left,
                "since": session["since"] if session else None,
            },
            "session": dict(session) if session else None,
            "lights": {
                light_id: {
                    "state": run.state,
                    "ours": run.ours,
                    "held": run.held_at is not None,
                    "held_until": held_until[light_id],
                }
                for light_id, run in self._runs.items()
            },
            "events": list(self._events),
        }

    def status(self) -> dict[str, Any]:
        """The last published status. Never waits for a tick (a fade holds the tick for seconds)."""
        status = self._status
        return dict(status) if status else {"running": False, "where": self.where, "events": list(self._events)}

    def _publish(self, views: list[dict[str, Any]], data: dict[str, Any], now: float, wall: datetime) -> None:
        status = self._build_status(views, data, now, wall)
        self._status = status
        if self.status_file is None:
            return
        body = repr({k: v for k, v in status.items() if k not in ("heartbeat",)})
        last_body, last_at = self._published
        if body == last_body and now - last_at < HEARTBEAT_SECONDS:
            return
        persisted = {**status, "ours": [i for i, r in self._runs.items() if r.ours]}
        try:
            settings.write_json(self.status_file, persisted)
        except settings.SettingsError as exc:
            log.warning("cannot write the lights status: %s", exc)
            return
        self._published = (body, now)

    def _restore(self) -> None:
        """After a restart: keep the event log, and the camera session when it was running a moment ago."""
        if self.status_file is None:
            return
        old = settings.read_json(self.status_file)
        events = old.get("events")
        for event in reversed(events if isinstance(events, list) else []):
            if isinstance(event, dict) and {"time", "kind", "text"} <= event.keys():
                self._events.appendleft(event)
        heartbeat, session = old.get("heartbeat"), old.get("session")
        if (
            isinstance(heartbeat, (int, float))
            and time.time() - heartbeat <= RESTORE_SECONDS
            and isinstance(session, dict)
        ):
            self._session = {k: session.get(k) for k in ("id", "since", "ts", "reason", "apps")}
            self._session_count = int(session.get("id") or 0)
            self._last_active = self.clock()
            for light_id in old.get("ours") or []:
                run = self._run_for(str(light_id))
                run.ours, run.session = True, self._session_count


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def read_status(path: Path | None = None) -> dict[str, Any] | None:
    """The service's last status with `fresh` (it was written within the last ten seconds); None when there is none."""
    data = settings.read_json(path or lightplan.state_path())
    if not data:
        return None
    heartbeat = data.get("heartbeat")
    fresh = isinstance(heartbeat, (int, float)) and abs(time.time() - heartbeat) <= HEARTBEAT_FRESH
    return {**data, "fresh": fresh, "running": bool(data.get("running")) and fresh}


def director_drives_lights(cfg: dict[str, str]) -> bool:
    """The AI Director is running and allowed to move the lights."""
    return config.truthy(cfg["STREAMSTACK_AUTO_LIGHTS"]) and system.user_unit_active(DIRECTOR_UNIT)


def run_service(cfg: dict[str, str] | None = None) -> int:
    """`streamstack lights run`: the long-lived process behind camera follow."""
    from streamstack.console.lighting import LightingController

    cfg = cfg or config.load_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    controller = LightingController(readback_seconds=2.0)
    watcher = CameraWatcher()
    engine = Automation(
        controller,
        controller.plan,
        watcher,
        lambda: watched_devices(config.load_settings()),
        lambda: director_drives_lights(config.load_settings()),
        status_file=lightplan.state_path(),
    )
    done = threading.Event()

    def stop(signum: int, _frame: object) -> None:
        log.info("lights: stopping on signal %s", signum)
        done.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    controller.start()
    engine.start()
    log.info(
        "lights: camera follow and schedule running (watching %s)", ", ".join(sorted(watched_devices(cfg))) or "nothing"
    )
    try:
        done.wait()
    finally:
        engine.stop()
        controller.stop()
        watcher.close()
    return 0
