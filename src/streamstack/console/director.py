"""The director on the Console's Auto page: its state for the snapshot, and the actions that edit its settings.

`DirectorPanel` reads what `streamstack auto run` wrote (director-status.json, the contact sheet) and the person's
choices (director.json, their own personas) and changes the choices through the same modules the CLI uses.
`DemoPanel` is the same panel over in-memory storage and a canned observation, so `--demo` and the e2e tests never
touch the machine.

Actions (all validated; bad input is an ActionError, never a crash):
  auto.persona        {id}        choose a persona
  auto.persona_save   {persona}   store an edited or new persona (full persona object)
  auto.persona_reset  {id}        drop the person's edits of a shipped persona, or delete a custom one
  auto.location       {name, latitude, longitude} | {clear: true}   where the property is, for sunrise and sunset
"""

from __future__ import annotations

import copy
import threading
import time
import traceback
from datetime import datetime
from importlib.resources import files
from typing import Any

from streamstack import config
from streamstack.console.errors import ActionError
from streamstack.director import daylight, learning, personas, settings, status

LIBRARY_TTL = 5.0
ACTIONS = ("auto.persona", "auto.persona_save", "auto.persona_reset", "auto.location")


def _text(params: dict, key: str, longest: int = 80) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > longest:
        raise ActionError(f"Send {key} as text of at most {longest} characters.", 400)
    return value.strip()


class DirectorPanel:
    """File-backed director state and actions."""

    def __init__(self, cfg: dict[str, str] | None = None) -> None:
        self.cfg = cfg or config.load_settings()
        self._lock = threading.Lock()
        self._cached: tuple[tuple, dict] | None = None
        self._library_at = 0.0
        self._library_held: personas.Library | None = None

    # -- storage the demo replaces -------------------------------------------------------------

    def _prefs(self) -> dict:
        return settings.load()

    def _library(self) -> personas.Library:
        return personas.load_library()

    def _status(self) -> dict:
        return status.read()

    def _store_persona(self, persona_id: str) -> None:
        settings.set_persona(persona_id)

    def _save_persona(self, persona: dict) -> dict:
        return personas.save_user(persona)

    def _remove_persona(self, persona_id: str) -> bool:
        return personas.remove_user(persona_id)

    def _store_property(self, name: str, latitude: object, longitude: object) -> None:
        settings.set_property(name, latitude, longitude)

    def _clear_property(self) -> None:
        settings.clear_property()

    def _sheet(self) -> bytes | None:
        return status.sheet_bytes()

    def _sheet_version(self) -> int:
        try:
            return int(settings.sheet_path().stat().st_mtime)
        except OSError:
            return 0

    def _learned(self, place: str, persona_id: str) -> dict:
        return {slot: entry for slot in daylight.SLOTS if (entry := learning.get(place, persona_id, slot))}

    def _now(self) -> datetime:
        return datetime.now().astimezone()

    def _library_now(self) -> personas.Library:
        """The persona library, re-read at most every few seconds (a snapshot is built many times a second)."""
        with self._lock:
            if self._library_held is None or time.monotonic() - self._library_at > LIBRARY_TTL:
                self._library_held, self._library_at = self._library(), time.monotonic()
            return self._library_held

    def _forget_library(self) -> None:
        with self._lock:
            self._library_held = None

    # -- state ----------------------------------------------------------------------------------------

    def state(self) -> dict:
        """The director block of the snapshot. Cached until a file changes or the minute turns."""
        now = self._now()
        report = self._status()
        key = (
            now.strftime("%Y%m%d%H%M"),
            report.get("time"),
            self._sheet_version(),
            repr(self._prefs()),
            repr(self._library_now().listing()),
        )
        with self._lock:
            if self._cached and self._cached[0] == key:
                return self._cached[1]
        prefs = self._prefs()
        library = self._library_now()
        chosen = library.get(prefs.get("persona"))
        clock = status.time_block(self.cfg, now, prefs)
        place = f"{clock['location']['latitude']:.1f},{clock['location']['longitude']:.1f}"
        value = {
            "persona": chosen["id"],
            "personas": library.listing(),
            "persona_problems": library.problems,
            "time": clock,
            "learned": self._learned(place, chosen["id"]),
            "report": report,
            "sheet": self._sheet_version() or None,
        }
        with self._lock:
            self._cached = (key, value)
        return value

    def sheet(self) -> bytes | None:
        return self._sheet()

    # -- actions ------------------------------------------------------------------------------------------

    def handles(self, action: str) -> bool:
        return action in ACTIONS

    def act(self, action: str, params: dict) -> str | None:
        try:
            return self._act(action, params)
        except personas.PersonaError as exc:
            raise ActionError(str(exc), 400) from exc
        except daylight.LocationError as exc:
            raise ActionError(str(exc), 400) from exc
        except settings.SettingsError as exc:
            raise ActionError(f"Could not save: {exc}", 500) from exc
        finally:
            self._forget_library()

    def _act(self, action: str, params: dict) -> str | None:
        library = self._library_now()
        if action == "auto.persona":
            persona_id = _text(params, "id", 32)
            if persona_id not in library.personas:
                raise ActionError("There is no such persona.", 404)
            self._store_persona(persona_id)
            return f"Persona: {library.personas[persona_id]['name']}"
        if action == "auto.persona_save":
            persona = params.get("persona")
            if not isinstance(persona, dict):
                raise ActionError("Send the persona as an object.", 400)
            saved = self._save_persona(persona)
            return f"Saved {saved['name']}"
        if action == "auto.persona_reset":
            persona_id = _text(params, "id", 32)
            if persona_id not in library.personas:
                raise ActionError("There is no such persona.", 404)
            if not self._remove_persona(persona_id):
                raise ActionError("That persona has no edits to undo.", 409)
            return "Persona restored" if library.personas[persona_id]["source"] == "edited" else "Persona deleted"
        if action == "auto.location":
            if params.get("clear") is True:
                self._clear_property()
                return "Using the time zone's city for sunrise and sunset"
            name = params.get("name")
            if not isinstance(name, str) or len(name) > 60:
                raise ActionError("Send the property name as text of at most 60 characters.", 400)
            latitude, longitude = daylight.check_coordinates(params.get("latitude"), params.get("longitude"))
            self._store_property(name, latitude, longitude)
            return "Property location saved"
        raise ActionError(f"Unknown action {action!r}.", 400)


def safe_state(panel: DirectorPanel) -> dict:
    """The panel's state, or {"error": ...} when it cannot be built; one damaged file must not blank the Console."""
    try:
        return panel.state()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        traceback.print_exc()
        return {"error": f"The director's state could not be read: {exc}"}


# ---------------------------------------------------------------------------
# The demo
# ---------------------------------------------------------------------------

DEMO_AUDIO = {
    "seconds": 6.0,
    "enough_speech": True,
    "speech_ratio": 0.62,
    "peak_db": -9.4,
    "speech_db": -19.8,
    "noise_floor_db": -64.0,
    "snr_db": 44.2,
    "lufs": -21.6,
    "f0_hz": 128.0,
    "f0_low_hz": 104.0,
    "f0_high_hz": 171.0,
    "f0_range_st": 8.6,
    "pitch_band": "medium-low",
    "centroid_hz": 1480.0,
    "tilt_db_oct": -7.4,
    "clarity_db": -9.5,
    "sibilance_db": -27.0,
    "rt60_s": 0.38,
    "room": "natural",
    "scores": {"pitch": 0.25, "depth": 0.64, "clarity": 0.52, "sibilance": 0.34, "clean": 0.73},
}
DEMO_PICTURE = {
    "luma_mean": 118.0,
    "luma_p5": 28.0,
    "luma_p50": 116.0,
    "luma_p95": 212.0,
    "clipped_highlights": 0.004,
    "crushed_shadows": 0.01,
    "contrast_spread": 184.0,
    "warm_index": 0.11,
    "tint_index": 0.01,
    "saturation": 0.24,
    "cast": "neutral",
    "sharpness": 142.0,
    "noise": 1.4,
    "skin": {"area": 0.07, "luma": 124.0, "redness": 1.38, "hue_deg": 18.0, "saturation": 0.37},
    "subject": {"box": [0.38, 0.14, 0.26, 0.44], "area": 0.07},
    "backlit": False,
    "histogram": [0.02, 0.05, 0.08, 0.1, 0.11, 0.12, 0.12, 0.1, 0.08, 0.07, 0.05, 0.04, 0.03, 0.02, 0.008, 0.002],
}


def demo_report(persona: dict, slot: str) -> dict:
    """What a pass seen on the demo rig looks like (invented numbers, no device involved)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return {
        "time": stamp,
        "persona": persona["id"],
        "slot": slot,
        "observation": {
            "source": "Canon EOS 50D",
            "frames": 4,
            "spacing": 1.0,
            "picture": DEMO_PICTURE,
            "burst": {"frames": 4, "luma_swing": 0.012, "flicker": False, "motion": 2.1, "motion_word": "still"},
            "problems": [],
        },
        "audio": {"features": DEMO_AUDIO, "reason": ""},
        "findings": [
            {
                "id": "face-exposure",
                "area": "picture",
                "state": "ok",
                "text": "The face sits inside the persona's exposure range.",
                "measure": "skin luma 124",
            },
            {
                "id": "white-balance",
                "area": "picture",
                "state": "warn",
                "text": "The picture is a little cooler than this persona's warmth.",
                "measure": "warm index 0.11",
            },
            {
                "id": "voice-ok",
                "area": "voice",
                "state": "ok",
                "text": "The voice is within the persona's targets.",
                "measure": "",
            },
        ],
        "held": [],
        "claude": {
            "confidence": 0.9,
            "notes": "Looks well exposed; nudged warmth up a touch.",
            "assessment": {
                "mood_match": 0.8,
                "observed_mood": ["calm", "focused", "bright"],
                "lighting": "mixed",
                "issues": ["Window light is slightly cool against the lamp."],
            },
            "reasons": {"warmth": "Skin read cooler than the persona's warmth, so warmth moved up one small step."},
        },
        "changes": [
            {
                "actuator": "obs-look",
                "target": "Canon EOS 50D",
                "key": "warmth",
                "before": 0.0,
                "after": 0.12,
                "reason": "Skin read cooler than the persona's warmth, so warmth moved up one small step.",
                "source": "claude",
                "kind": "video",
            },
            {
                "actuator": "canon-exposure",
                "target": "Canon EOS 50D",
                "key": "exposure_comp",
                "before": 0.0,
                "after": 0.3,
                "reason": "The face was 96 luma, below the band; the camera's exposure compensation moves first.",
                "source": "local",
                "kind": "camera",
            },
            {
                "actuator": "litra",
                "target": "Litra Beam 1",
                "key": "kelvin",
                "before": 5600,
                "after": 5300,
                "reason": "Easing towards Daylight natural's light for the morning.",
                "source": "persona",
                "kind": "light",
            },
        ],
        "actuators": [
            {"id": "obs-look", "label": "OBS picture look", "kind": "video", "available": True, "reason": ""},
            {"id": "mic-gain", "label": "Microphone gain", "kind": "audio", "available": True, "reason": ""},
            {"id": "voice-preset", "label": "Voice preset", "kind": "audio", "available": True, "reason": ""},
            {
                "id": "canon-exposure",
                "label": "Canon exposure and white balance",
                "kind": "camera",
                "available": True,
                "reason": "",
            },
            {"id": "litra", "label": "Litra key and fill lights", "kind": "light", "available": True, "reason": ""},
            {
                "id": "interface-gain",
                "label": "Audio interface hardware gain",
                "kind": "interface",
                "available": True,
                "reason": "",
            },
            {
                "id": "face-finder",
                "label": "Face finder",
                "kind": "sensor",
                "available": True,
                "reason": "YuNet on CPU",
            },
        ],
    }


class DemoPanel(DirectorPanel):
    """The same panel with in-memory choices and a canned pass; nothing is read from or written to the machine."""

    def __init__(self, cfg: dict[str, str] | None = None) -> None:
        super().__init__(cfg or dict(config.DEFAULTS))
        shipped = personas.load_library(include_user=False)
        self._shipped = copy.deepcopy(shipped.personas)
        self._edited: dict[str, dict] = {}
        self._chosen = settings.DEFAULT_PERSONA
        self._property: dict[str, Any] | None = None
        self._sheet_bytes: bytes | None = None

    def _prefs(self) -> dict:
        prefs: dict[str, Any] = {"persona": self._chosen}
        if self._property:
            prefs["property"] = dict(self._property)
        return prefs

    def _library(self) -> personas.Library:
        library = personas.Library(personas=copy.deepcopy(self._shipped))
        for persona_id, persona in self._edited.items():
            library.personas[persona_id] = {**persona, "source": "edited" if persona_id in self._shipped else "custom"}
        return library

    def _status(self) -> dict:
        prefs = self._prefs()
        persona = self._library_now().get(prefs["persona"])
        slot = status.time_block(self.cfg, self._now(), prefs)["sun"]["slot"]
        report = demo_report(persona, slot)
        report["time"] = report["time"][:16]  # stable for a minute, so the state is not rebuilt on every poll
        return report

    def _store_persona(self, persona_id: str) -> None:
        self._chosen = persona_id

    def chosen(self) -> str:
        return self._chosen

    def slot(self) -> str:
        return status.time_block(self.cfg, self._now(), self._prefs())["sun"]["slot"]

    def _save_persona(self, persona: dict) -> dict:
        saved = personas.validate(persona)
        self._edited[saved["id"]] = saved
        return saved

    def _remove_persona(self, persona_id: str) -> bool:
        return self._edited.pop(persona_id, None) is not None

    def _store_property(self, name: str, latitude: object, longitude: object) -> None:
        self._property = {"name": name.strip()[:60] or "This property", "latitude": latitude, "longitude": longitude}

    def _clear_property(self) -> None:
        self._property = None

    def _learned(self, place: str, persona_id: str) -> dict:
        return {
            "morning": {"look": {"warmth": 0.1, "contrast": 0.05}, "n": 6},
            "evening": {"look": {"warmth": 0.18, "contrast": 0.1}, "n": 3},
        }

    def _sheet_version(self) -> int:
        return 1

    def _sheet(self) -> bytes | None:
        if self._sheet_bytes is None:
            self._sheet_bytes = files("streamstack").joinpath("data/console/demo/director-sheet.jpg").read_bytes()
        return self._sheet_bytes
