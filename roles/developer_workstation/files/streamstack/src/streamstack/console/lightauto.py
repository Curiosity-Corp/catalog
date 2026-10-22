"""The Console's side of lighting automation: what the Lighting page shows and the actions behind its controls.

The engine itself (`streamstack.lightauto`) runs in the `streamstack-lights` user service so camera follow works with
the Console closed. This panel only edits the settings file the service re-reads every second, and shows the status
file the service writes. In the demo rig there is no service: the panel owns an in-process engine over simulated
lights and a scripted camera, so every control and every status line can be exercised without hardware.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from streamstack import config, lightauto, lightplan, lightservice
from streamstack.camerause import Consumer, Usage
from streamstack.console.errors import ActionError
from streamstack.director import daylight, settings

log = logging.getLogger(__name__)

CURVE_STEP = 15  # minutes between points of the 24 hour graph
SLOT_NOTES = {slot: daylight.SLOT_NOTES[slot] for slot in daylight.SLOTS}
KnownLight = Callable[[object], str]

NO_SERVICE_HINTS = {
    "missing": (
        "Camera follow runs in a small background service so it works with the Console closed. "
        "Install it with `streamstack setup`, or press Start here."
    ),
    "stopped": (
        "The lights service is not running, so nothing watches the camera. "
        "Press Start, or run `streamstack lights enable`."
    ),
}


def _empty_status(message: str) -> dict[str, Any]:
    return {
        "running": False,
        "where": "none",
        "owner": "none",
        "owner_label": lightauto.OWNER_LABELS["none"],
        "summary": message,
        "camera": {
            "in_use": False,
            "reason": "",
            "apps": [],
            "obs": [],
            "obs_known": False,
            "grace_left": None,
            "since": None,
        },
        "session": None,
        "lights": {},
        "events": [],
    }


def empty_automation() -> dict[str, Any]:
    """The `lighting.automation` object for a rig without lighting automation (the page's starting skeleton)."""
    base = lightplan.defaults()
    return {
        "available": False,
        "settings": {"follow": base["follow"], "schedule": base["schedule"], "lights": {}},
        "limits": {k: list(v) for k, v in lightplan.LIMITS.items()},
        "roles": [{"id": r, "note": lightplan.ROLE_NOTES[r]} for r in lightplan.ROLES],
        "modes": [{"id": m, "note": lightplan.MODE_NOTES[m]} for m in lightplan.MODES],
        "slots": [{"id": s, "label": daylight.SLOT_LABELS[s], "note": SLOT_NOTES[s]} for s in daylight.SLOTS],
        "sources": [{"id": s, "label": lightplan.SOURCE_LABELS[s]} for s in lightplan.FOLLOW_SOURCES],
        "status": _empty_status("Lighting automation is not part of this rig."),
        "service": {"state": "missing", "installed": False, "message": "", "command": "streamstack lights enable"},
        "location": {"name": "", "latitude": 0, "longitude": 0, "source": "timezone"},
        "sun": {},
        "now": {"minute": 0, "brightness": 50, "kelvin": 4800, "slot": "morning", "slot_label": "Morning"},
        "curve": {"step": CURVE_STEP, "brightness": [], "kelvin": []},
        "hours": [],
    }


class ScriptedCamera:
    """The demo rig's camera watcher: a test or the owner pretends an app opens and closes the camera."""

    def __init__(self) -> None:
        self.app: str | None = None
        self.director = False

    def poll(self, follow: dict[str, Any], devices: set[str]) -> Usage:
        if not self.app or not follow.get("apps", True):
            return Usage()
        return Usage((Consumer(4242, self.app.lower(), "/dev/video43"),))

    def close(self) -> None:
        return None


class AutomationPanel:
    def __init__(
        self,
        store: lightplan.Store,
        engine: lightauto.Automation | None = None,
        scripted: ScriptedCamera | None = None,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.store, self.engine, self.scripted, self._now = store, engine, scripted, now
        self._cache: tuple[str, dict[str, Any]] | None = None

    # -- state ------------------------------------------------------------------

    def _status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """(status, service): the engine's own in the demo rig, the service's status file otherwise."""
        if self.engine is not None:
            return self.engine.status(), {"state": "console", "installed": True, "message": "", "command": ""}
        status = lightauto.read_status()
        if status is not None and status.get("running"):
            return status, {"state": "running", "installed": True, "message": "", "command": ""}
        installed = lightservice.installed_unit() is not None
        state = "stopped" if installed else "missing"
        message = NO_SERVICE_HINTS[state]
        stale = _empty_status(message)
        if status is not None:
            stale["events"] = status.get("events", [])
        return stale, {
            "state": state,
            "installed": installed,
            "message": message,
            "command": "streamstack lights enable",
        }

    def _curve(self, data: dict[str, Any], wall: datetime, loc: daylight.Location) -> dict[str, Any]:
        """The 24 hour graph and the sun times for today; cached until the schedule, the day or the place changes."""
        key = json.dumps([data["schedule"], wall.date().isoformat(), loc.latitude, loc.longitude], sort_keys=True)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        points = lightplan.curve(data, wall.date(), wall.tzinfo, loc, CURVE_STEP)
        times = daylight.sun_times(wall.date(), loc, wall.tzinfo)
        value = {
            "curve": {
                "step": CURVE_STEP,
                "brightness": [p["brightness"] for p in points],
                "kelvin": [p["kelvin"] for p in points],
            },
            "sun": {k: v.strftime("%H:%M") if v else None for k, v in times.items()},
            "hours": data["schedule"]["hours"] or lightplan.hours_from_sun(data, wall, loc),
        }
        self._cache = (key, value)
        return value

    def state(self, lights: list[dict[str, Any]]) -> dict[str, Any]:
        data = self.store.load()
        wall = self._now()
        try:
            loc = daylight.resolve_location(settings.load(), config.load_settings(), wall)
            shown = self._curve(data, wall, loc)
            level = lightplan.level_at(data, wall, loc)
            elevation, _, hour_angle = daylight.solar_position(wall, loc.latitude, loc.longitude)
            slot = daylight.slot_for(elevation, hour_angle)
        except (ValueError, OSError) as exc:  # a bad location or tz must not take the whole page down
            log.warning("lighting schedule preview unavailable: %s", exc)
            return {**empty_automation(), "available": True}
        status, service = self._status()
        base = empty_automation()
        return {
            **base,
            "available": True,
            "settings": {
                "follow": data["follow"],
                "schedule": {**data["schedule"], "hours": data["schedule"]["hours"]},
                "lights": {light["id"]: lightplan.light_settings(data, light["id"]) for light in lights},
            },
            "status": status,
            "service": service,
            "location": loc.public(),
            "sun": shown["sun"],
            "now": {
                "minute": wall.hour * 60 + wall.minute,
                "brightness": level["brightness"],
                "kelvin": level["kelvin"],
                "slot": slot,
                "slot_label": daylight.SLOT_LABELS[slot],
            },
            "curve": shown["curve"],
            "hours": shown["hours"],
            "demo": self.scripted is not None,
        }

    # -- actions --------------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any], known_light: KnownLight) -> str | None:
        try:
            return self._act(action, params, known_light)
        except lightplan.PlanError as exc:
            raise ActionError(str(exc), 400) from exc
        except settings.SettingsError as exc:
            raise ActionError(f"Could not save the lighting settings: {exc}", 409) from exc

    def _patch(self, params: dict[str, Any], *drop: str) -> dict[str, Any]:
        return {k: v for k, v in params.items() if k not in drop}

    def _seed(self) -> list[dict[str, float]]:
        wall = self._now()
        data = self.store.load()
        loc = daylight.resolve_location(settings.load(), config.load_settings(), wall)
        return lightplan.hours_from_sun(data, wall, loc)

    def _act(self, action: str, params: dict[str, Any], known_light: KnownLight) -> str | None:
        if action == "lighting.auto.follow":
            patch = self._patch(params)
            if not patch:
                raise ActionError("Nothing to change.", 400)
            self.store.update("follow", patch)
            return None
        if action == "lighting.auto.schedule":
            patch = self._patch(params)
            if not patch:
                raise ActionError("Nothing to change.", 400)
            if patch.get("mode") == "clock":
                self.store.seed_hours(self._seed())  # the table starts as today's sun curve, ready to edit
            self.store.update("schedule", patch)
            return None
        if action == "lighting.auto.slot":
            self.store.set_slot(params.get("slot"), self._patch(params, "slot"))
            return None
        if action == "lighting.auto.hour":
            self.store.set_hour(params.get("hour"), self._patch(params, "hour"), self._seed())
            return None
        if action == "lighting.auto.defaults":
            what = params.get("what", "all")
            self.store.reset(what if isinstance(what, str) else "")
            return "Lighting automation is back to its defaults"
        if action == "lighting.auto.resume":
            self.store.resume()
            return "The schedule takes the lights back"
        if action == "lighting.light.settings":
            light_id = known_light(params.get("id"))
            self.store.set_light(light_id, self._patch(params, "id"))
            return None
        if action in ("lighting.service.enable", "lighting.service.disable"):
            if self.engine is not None:
                raise ActionError("The demo rig runs the automation inside the Console.", 409)
            try:
                if action.endswith("enable"):
                    lightservice.enable()
                    return "The lights service is running"
                lightservice.disable()
                return "The lights service is stopped"
            except lightservice.ServiceError as exc:
                raise ActionError(str(exc), 409) from exc
        if action == "lighting.demo.camera":
            return self._demo_camera(params)
        if action == "lighting.demo.director":
            if self.scripted is None:
                raise ActionError("Only the demo rig can pretend the AI Director is running.", 409)
            if not isinstance(params.get("on"), bool):
                raise ActionError("on must be true or false", 400)
            self.scripted.director = params["on"]
            return None
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _demo_camera(self, params: dict[str, Any]) -> str:
        if self.scripted is None:
            raise ActionError("Only the demo rig can pretend to use the camera.", 409)
        if not isinstance(params.get("on"), bool):
            raise ActionError("on must be true or false", 400)
        app = params.get("app", "Teams")
        if not isinstance(app, str) or not app.strip() or len(app) > 30:
            raise ActionError("app must be a short name", 400)
        self.scripted.app = app.strip() if params["on"] else None
        return f"{app.strip()} opened the camera" if params["on"] else "The camera was released"


def attach_demo(controller: Any) -> None:
    """Give a (demo) LightingController an in-process engine over a scripted camera."""
    scripted = ScriptedCamera()
    if not controller.plan.file().exists():
        # The demo lights start where the rig's other fixtures expect them; the schedule is switched on by the specs
        # (and by a person trying it out) rather than moving the demo lights on the first tick.
        controller.plan.update("schedule", {"enabled": False})
    engine = lightauto.Automation(
        controller,
        controller.plan,
        scripted,
        lambda: set(),
        lambda: scripted.director,
        tick_seconds=0.25,
        sleep=lambda seconds: None,
        where="console",
        director_check_seconds=0,
    )
    controller.panel = AutomationPanel(controller.plan, engine, scripted)
    controller.engine = engine
