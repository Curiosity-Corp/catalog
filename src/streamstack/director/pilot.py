"""The director's judgement: which persona, which hour, what look to ease towards, and what is left to the person.

`Pilot` holds no hardware. `auto.Auto` owns the Console link, the microphone and Claude and asks the pilot four things:

* `refresh()` once per tick: reload the persona and the location (so editing either in the Console takes effect
  within one tick) and work out the sun now;
* `frame_band()` / `face_band()`: the exposure the local loop should hold, from the persona;
* `ease()`: small steps from the look in place towards the look this persona has at this hour, which is the look
  the person last kept at this hour when there is one (see `learning`) and the persona's own plus a small hint for
  the hour otherwise;
* `remember_kept()`: record what the person kept, so the next visit to this hour starts from it.

Claude's changes take precedence over easing: a setting Claude has moved is left alone until the persona or the
hour changes.
"""

from __future__ import annotations

from datetime import datetime

from streamstack.director import daylight, learning, limits, personas, settings

LIGHT_FLOOR = 0.1
LIGHT_KELVIN = (2700.0, 6500.0)
EASE_KEYS = ("contrast", "saturation", "warmth", "lut_amount")


class Pilot:
    def __init__(self, cfg: dict[str, str]) -> None:
        self.cfg = cfg
        self.prefs: dict = {}
        self.library = personas.Library()
        self.persona: dict = {}
        self.location: daylight.Location | None = None
        self.sun: daylight.SunState | None = None
        self.claude_set: set[str] = set()
        self._epoch: tuple[str, str] | None = None
        self.refresh()

    # -- who and when -------------------------------------------------------

    def refresh(self, now: datetime | None = None) -> None:
        """Reload persona and location; changing either (or the hour's slot) lets easing resume."""
        now = now or datetime.now().astimezone()
        self.prefs = settings.load()
        self.library = personas.load_library()
        self.persona = self.library.get(self.prefs.get("persona"))
        self.location = daylight.resolve_location(self.prefs, self.cfg, now)
        self.sun = daylight.sun_state(now, self.location)
        epoch = (self.persona["id"], self.sun.slot)
        if epoch != self._epoch:
            self.claude_set.clear()
            self._epoch = epoch

    @property
    def slot(self) -> str:
        return self.sun.slot if self.sun else "morning"

    @property
    def place(self) -> str:
        """The key a learned look is stored under: the property's rounded coordinates, so renaming it keeps it."""
        loc = self.location
        return f"{loc.latitude:.1f},{loc.longitude:.1f}" if loc else "unknown"

    def sun_public(self, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        return {**self.sun.public(), "clock": now.strftime("%H:%M")} if self.sun else {}

    # -- the target ------------------------------------------------------------

    def frame_band(self) -> tuple[float, float]:
        low, high = self.persona["picture"]["frame_luma"]
        return float(low), float(high)

    def face_band(self) -> tuple[float, float]:
        low, high = self.persona["picture"]["face_luma"]
        return float(low), float(high)

    def learned(self) -> dict | None:
        return learning.get(self.place, self.persona["id"], self.slot)

    def goal(self) -> tuple[dict[str, float], str, str]:
        """(look, source, reason): what this persona looks like now, from the learned look or the persona's own."""
        learned = self.learned()
        slot = daylight.SLOT_LABELS[self.slot].lower()
        if learned:
            look = dict(learned["look"])
            source, reason = "baseline", f"the look you kept last time in the {slot}"
        else:
            look = dict(self.persona["picture"]["look"])
            for key, offset in daylight.SLOT_PRIORS.get(self.slot, {}).items():
                look[key] = look.get(key, 0.0) + offset
            source, reason = "persona", f"{self.persona['name']}'s look for the {slot}"
        return (
            {k: max(limits.LIMITS[k][0], min(limits.LIMITS[k][1], v)) for k, v in look.items() if k in limits.LIMITS},
            source,
            reason,
        )

    def light_goal(self) -> tuple[float, float, str, str] | None:
        """(brightness fraction, kelvin, source, reason) for the key and fill lights now; None without a light plan.

        The dark slots (night, dawn, evening) raise the light and warm it from the persona's baseline; once the person
        has kept the lights at some level in this slot, that level is the goal instead.
        """
        slot = daylight.SLOT_LABELS[self.slot].lower()
        kept = learning.get_light(self.place, self.persona["id"], self.slot)
        if kept:
            fraction, kelvin = kept
            source, reason = "baseline", f"the light you kept last time in the {slot}"
        else:
            plan = self.persona.get("light")
            if not isinstance(plan, dict):
                return None
            d_fraction, d_kelvin = daylight.LIGHT_PRIORS.get(self.slot, (0.0, 0.0))
            fraction, kelvin = float(plan["brightness"]) + d_fraction, float(plan["kelvin"]) + d_kelvin
            source, reason = "persona", f"{self.persona['name']}'s light for the {slot}"
        return (
            max(LIGHT_FLOOR, min(1.0, fraction)),
            max(LIGHT_KELVIN[0], min(LIGHT_KELVIN[1], kelvin)),
            source,
            reason,
        )

    def ease(self, current: dict) -> tuple[dict[str, float], str, str]:
        """One small step towards the goal for each setting not yet settled; ({}, ...) when already there."""
        goal, source, reason = self.goal()
        wanted: dict[str, float] = {}
        for key in EASE_KEYS:
            if key in self.claude_set or key not in goal or not isinstance(current.get(key), (int, float)):
                continue
            gap = goal[key] - current[key]
            step = limits.START_STEP[key]
            if abs(gap) <= step / 2:
                continue
            wanted[key] = round(current[key] + max(-step, min(step, gap)), 4)
        return wanted, source, f"Easing towards {reason}"

    # -- what the person kept -----------------------------------------------------

    def remember_kept(
        self,
        look: dict,
        manual: bool,
        mic: tuple[str, float] | None,
        light: tuple[float, float] | None = None,
    ) -> None:
        learning.record(self.place, self.persona["id"], self.slot, look, manual=manual, mic=mic, light=light)
