"""The hardware actuators: Canon exposure and white balance, Litra lights and audio-interface preamp gain.

Each class here is the director's view of one kind of device, written against a small backend protocol so it can be
tested with a fake and so it never imports a driver. `director.adapters` binds the same protocols to the real
modules (`streamstack.canon`, `streamstack.lighting`, `streamstack.hardware`). Every actuator:

* says it is unavailable, with a reason the Auto page shows, when its device is not there, cannot be opened or is
  locked, and never raises for that;
* reads only the values it may change, so a locked control (exposure compensation on an M dial, a gain the
  interface froze for Autogain) is simply absent and the governor leaves it alone;
* never touches what is not its job: focus on the Canon, power or presets on a light, 48V, Autogain or any switch
  on an interface. There is no code path from here to those controls.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from streamstack.director import limits
from streamstack.director.actuators import Actuator, ActuatorError, Availability, Param

# ---------------------------------------------------------------------------
# Canon exposure and white balance
# ---------------------------------------------------------------------------

EXPOSURE_COMP = Param("exposure_comp", "Exposure compensation", "number", -1.0, 1.0, 0.34, "EV", min_interval=30.0)
CANON_KELVIN = Param("kelvin", "White balance", "number", 3200.0, 6500.0, 400.0, "K", min_interval=60.0)
WB_KELVIN_MODE = "Color Temperature"


class CanonBackend(Protocol):
    def status(self) -> dict | None:
        """The Canon bridge's status (`state`, `session`, `controls`), or None when it does not answer."""

    def set(self, control: str, value: object) -> None:
        """Set one Canon control by the Console's control id. Raises ActuatorError."""


def _number(text: object) -> float | None:
    """A control's value or choice as a number ("+0.3", "5200", "1/60" is not one), or None."""
    if isinstance(text, bool) or text is None:
        return None
    try:
        value = float(str(text).strip().replace("+", ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class CanonActuator(Actuator):
    """Exposure compensation and Kelvin on the Canon body, through the running Canon bridge.

    Exposure compensation is the exposure knob (the body's own metering does the rest) and Kelvin the white balance
    knob. ISO, shutter and aperture stay the person's, and focus is never touched. Both are small steps with a long
    interval, because the body needs a moment to settle and the picture is live.
    """

    id = "canon-exposure"
    label = "Canon exposure and white balance"
    kind = "camera"
    baseline_kind = "canon"
    params = (EXPOSURE_COMP, CANON_KELVIN)

    def __init__(self, backend: CanonBackend, remember: Callable[[str, str, object], None]) -> None:
        self._backend, self._remember = backend, remember

    def override_eps(self) -> dict[str, float]:
        return {"exposure_comp": 0.2, "kelvin": 150.0}

    # -- what the body offers --------------------------------------------------------------------

    def _status(self) -> tuple[dict | None, str]:
        """(the controls, why not). The bridge must be running and sharing the USB session."""
        status = self._backend.status()
        if status is None:
            return None, "the Canon bridge is not running"
        state = status.get("state")
        if state != "running":
            return None, f"the Canon bridge is {state or 'not ready'}"
        if status.get("session") == "pause":
            return None, (
                "the gphoto2 command backend pauses the picture for every change; install python3-gphoto2 "
                "(STREAMSTACK_CANON_BACKEND) to let the director adjust it live"
            )
        controls = status.get("controls")
        if not isinstance(controls, dict) or not controls:
            return None, "the camera reports no controls"
        return controls, ""

    def _writable(self, controls: dict) -> dict[str, float]:
        """The current value of each parameter the body lets the director change right now."""
        out: dict[str, float] = {}
        comp = controls.get("exposure_comp")
        if isinstance(comp, dict) and not comp.get("readonly") and (value := _number(comp.get("value"))) is not None:
            out["exposure_comp"] = value
        kelvin = controls.get("kelvin")
        mode = str((controls.get("whitebalance") or {}).get("value") or "")
        auto_or_kelvin = mode.lower().startswith("auto") or mode == WB_KELVIN_MODE
        if (
            isinstance(kelvin, dict)
            and not kelvin.get("readonly")
            and auto_or_kelvin
            and (value := _number(kelvin.get("value"))) is not None
        ):
            out["kelvin"] = value
        return out

    def availability(self) -> Availability:
        controls, why = self._status()
        if controls is None:
            return Availability(False, why)
        if not self._writable(controls):
            return Availability(
                False,
                "exposure compensation is locked (turn the mode dial to P, Tv or Av) and white balance is a preset "
                "(choose Auto or Color Temperature)",
            )
        return Availability(True)

    def targets(self) -> list[str]:
        controls, _ = self._status()
        if controls is None:
            return []
        model = str((controls.get("model") or {}).get("value") or "").strip()
        return [model or "Canon camera"]

    def read(self, target: str) -> dict:
        controls, _ = self._status()
        return self._writable(controls) if controls else {}

    # -- changes ------------------------------------------------------------------------------------

    def _choice(self, controls: dict, value: float) -> str:
        """The body's own spelling of the nearest exposure compensation it offers."""
        choices = [
            (c, n) for c in (controls.get("exposure_comp") or {}).get("choices", []) if (n := _number(c)) is not None
        ]
        if not choices:
            raise ActuatorError("the camera lists no exposure compensation values")
        return min(choices, key=lambda pair: abs(pair[1] - value))[0]

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        controls, why = self._status()
        if controls is None:
            raise ActuatorError(f"Canon: {why}")
        if "exposure_comp" in wanted and wanted["exposure_comp"] != current.get("exposure_comp"):
            self._backend.set("exposure_comp", self._choice(controls, float(wanted["exposure_comp"])))
        if "kelvin" in wanted and wanted["kelvin"] != current.get("kelvin"):
            # The bridge switches the white balance to Color Temperature first when it is on Auto.
            self._backend.set("kelvin", int(round(float(wanted["kelvin"]) / 100.0) * 100))

    def remember(self, target: str, current: dict) -> None:
        controls, _ = self._status()
        mode = str(((controls or {}).get("whitebalance") or {}).get("value") or "")
        saved = {**current, **({"whitebalance": mode} if mode else {})}
        self._remember("canon", target, saved)

    def restore(self, target: str, saved: object) -> None:
        if not isinstance(saved, dict):
            raise ActuatorError("nothing recorded for the Canon")
        controls, why = self._status()
        if controls is None:
            raise ActuatorError(f"Canon: {why}")
        mode = saved.get("whitebalance")
        if isinstance(saved.get("exposure_comp"), (int, float)):
            self._backend.set("exposure_comp", self._choice(controls, float(saved["exposure_comp"])))
        if mode == WB_KELVIN_MODE and isinstance(saved.get("kelvin"), (int, float)):
            self._backend.set("kelvin", int(saved["kelvin"]))
        elif isinstance(mode, str) and mode:
            self._backend.set("whitebalance", mode)

    def headroom(self, current: dict, direction: int) -> bool:
        """Whether exposure compensation can still move one step towards `direction` (+1 brighter, -1 darker)."""
        value = current.get("exposure_comp")
        if not isinstance(value, (int, float)):
            return False
        low, high = EXPOSURE_COMP.low or -1.0, EXPOSURE_COMP.high or 1.0
        return value < high if direction > 0 else value > low


# ---------------------------------------------------------------------------
# Litra lights
# ---------------------------------------------------------------------------

LIGHT_STEP_FRACTION = 0.12  # of the model's brightness range, per pass
LIGHT_KELVIN_STEP = 300.0
LIGHT_KELVIN = (2700.0, 6500.0)
LIGHT_FLOOR = 0.1  # the director never takes a light below this fraction of its range
LIGHT_EPS_LUMEN = 8.0
LIGHT_EPS_KELVIN = 150.0
ERROR_MEMORY = 60.0  # seconds a failed light is reported unavailable before it is tried again


@dataclass(frozen=True)
class LightInfo:
    """One light the backend found: a unique label (the target name) and its model's brightness range."""

    label: str
    model: str
    lumen_min: int
    lumen_max: int


@dataclass(frozen=True)
class LightState:
    on: bool | None
    lumen: int | None
    kelvin: int | None


class LightBackend(Protocol):
    def lights(self) -> list[LightInfo]:
        """Every Litra on the bus (reads sysfs only; opens nothing)."""

    def read(self, label: str) -> LightState:
        """Open the light, read power, brightness and temperature, close it. Raises ActuatorError."""

    def write(self, label: str, lumen: int | None, kelvin: int | None) -> None:
        """Set brightness (lumen) and/or colour temperature. Never power. Raises ActuatorError."""


class LitraActuator(Actuator):
    """Brightness (lumen) and colour temperature (K) of each Litra, within its model's range.

    Lights that are off are left off and are not read as targets: the director never switches a light on or off, so
    the person's off stays off. The per-light balance the person set (a dimmer fill) is kept: `plan_group` moves
    every light by the same amount towards the goal for the group.
    """

    id = "litra"
    label = "Litra key and fill lights"
    kind = "light"
    baseline_kind = "light"
    params = (
        Param("lumen", "Brightness", "number", unit="lm", min_interval=20.0),  # limits come from the model: `param()`
        Param("kelvin", "Colour temperature", "number", *LIGHT_KELVIN, LIGHT_KELVIN_STEP, "K", min_interval=20.0),
    )

    def __init__(
        self,
        backend: LightBackend,
        remember: Callable[[str, str, object], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend, self._remember, self._clock = backend, remember, clock
        self._error = ""
        self._error_at = 0.0

    def override_eps(self) -> dict[str, float]:
        return {"lumen": LIGHT_EPS_LUMEN, "kelvin": LIGHT_EPS_KELVIN}

    def _info(self, target: str) -> LightInfo | None:
        return next((i for i in self._backend.lights() if i.label == target), None)

    def param(self, key: str, target: str | None = None) -> Param | None:
        base = super().param(key)
        if base is None or key != "lumen":
            return base
        info = self._info(target) if target else None
        if info is None:
            return None
        span = info.lumen_max - info.lumen_min
        low = info.lumen_min + LIGHT_FLOOR * span
        return Param(
            "lumen",
            base.label,
            "number",
            float(round(low)),
            float(info.lumen_max),
            float(round(span * LIGHT_STEP_FRACTION)),
            "lm",
            min_interval=base.min_interval,
        )

    def availability(self) -> Availability:
        if not self._backend.lights():
            return Availability(False, "no Litra light connected")
        if self._error and self._clock() - self._error_at < ERROR_MEMORY:
            return Availability(False, self._error)  # shown for a minute, then the light is tried again
        return Availability(True)

    def targets(self) -> list[str]:
        return [i.label for i in self._backend.lights()]

    def read(self, target: str) -> dict:
        """{lumen, kelvin} of a light that is on; {} when it is off, absent or unreadable."""
        try:
            state = self._backend.read(target)
        except ActuatorError as exc:
            self._error, self._error_at = str(exc), self._clock()
            return {}
        self._error = ""
        if not state.on or state.lumen is None or state.kelvin is None:
            return {}
        return {"lumen": float(state.lumen), "kelvin": float(state.kelvin)}

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        lumen = wanted.get("lumen") if wanted.get("lumen") != current.get("lumen") else None
        kelvin = wanted.get("kelvin") if wanted.get("kelvin") != current.get("kelvin") else None
        if lumen is None and kelvin is None:
            return
        try:
            self._backend.write(
                target,
                round(float(lumen)) if lumen is not None else None,
                round(float(kelvin)) if kelvin is not None else None,
            )
        except ActuatorError as exc:
            self._error, self._error_at = str(exc), self._clock()
            raise

    def remember(self, target: str, current: dict) -> None:
        self._remember("light", target, {k: current[k] for k in ("lumen", "kelvin") if k in current})

    def restore(self, target: str, saved: object) -> None:
        if not isinstance(saved, dict):
            raise ActuatorError("nothing recorded for this light")
        lumen, kelvin = saved.get("lumen"), saved.get("kelvin")
        self._backend.write(
            target,
            round(float(lumen)) if isinstance(lumen, (int, float)) else None,
            round(float(kelvin)) if isinstance(kelvin, (int, float)) else None,
        )

    def fraction(self, target: str, lumen: float) -> float | None:
        """`lumen` as a fraction of the light's own range (0 dimmest, 1 brightest)."""
        info = self._info(target)
        if info is None or info.lumen_max <= info.lumen_min:
            return None
        return max(0.0, min(1.0, (lumen - info.lumen_min) / (info.lumen_max - info.lumen_min)))

    def plan_group(self, goal_fraction: float, goal_kelvin: float, readings: dict[str, dict]) -> dict[str, dict]:
        """What each light should be so the group reaches the goal while keeping the person's balance between them.

        The goal is for the average light; each light keeps its own offset from the average (a fill dimmer than the
        key, a warmer accent), so stepping every light by the same amount converges on the goal without flattening
        the arrangement.
        """
        fractions = {t: f for t, r in readings.items() if (f := self.fraction(t, r["lumen"])) is not None}
        if not fractions:
            return {}
        mean_f = sum(fractions.values()) / len(fractions)
        mean_k = sum(readings[t]["kelvin"] for t in fractions) / len(fractions)
        wanted: dict[str, dict] = {}
        for target, fraction in fractions.items():
            info = self._info(target)
            if info is None:
                continue
            level = max(LIGHT_FLOOR, min(1.0, goal_fraction + (fraction - mean_f)))
            kelvin = max(LIGHT_KELVIN[0], min(LIGHT_KELVIN[1], goal_kelvin + (readings[target]["kelvin"] - mean_k)))
            wanted[target] = {
                "lumen": float(round(info.lumen_min + level * (info.lumen_max - info.lumen_min))),
                "kelvin": float(round(kelvin / 100.0) * 100),
            }
        return wanted


# ---------------------------------------------------------------------------
# Audio interface preamp gain
# ---------------------------------------------------------------------------

GAIN_STEP_DB = 2.0
GAIN_EPS_DB = 0.6
INSPECT_TTL = 5.0  # seconds a look at the interface is reused (it costs an amixer call or a USB transfer)


@dataclass(frozen=True)
class GainControl:
    """One software-settable preamp gain: its control id, range and, when it is frozen, why."""

    id: str
    label: str
    value: float
    low: float
    high: float
    step: float
    locked: str = ""


@dataclass(frozen=True)
class InterfaceReport:
    present: bool
    label: str = ""
    controls: tuple[GainControl, ...] = ()
    note: str = ""  # why there is no usable hardware gain, when there is none


class InterfaceBackend(Protocol):
    def inspect(self) -> InterfaceReport:
        """The interface on the desk and its gain controls. Raises ActuatorError for an unreadable one."""

    def set_gain(self, control_id: str, db: float) -> None:
        """Set a gain control by id. Only gain controls are accepted. Raises ActuatorError."""


class InterfaceGainActuator(Actuator):
    """The preamp gain of the audio interface on the desk (Scarlett 2i2, Wave XLR), before the software gain.

    Hardware gain comes first because boosting a quiet microphone in software raises its noise with it. The
    director moves it inside the same window around where auto mode found it as the software gain (MIC_GAIN_DB).
    Without hardware gain (a Scarlett 2i2 Gen 3 has knobs only, a Wave XLR with USB control off) this actuator is
    unavailable, with the reason, and auto mode uses the software gain instead.
    """

    id = "interface-gain"
    label = "Audio interface hardware gain"
    kind = "interface"
    baseline_kind = "gain"
    params = (Param("gain_db", "Preamp gain", "number", None, None, GAIN_STEP_DB, "dB", min_interval=20.0),)

    def __init__(
        self,
        backend: InterfaceBackend,
        remember: Callable[[str, str, object], None],
        start_gain: Callable[[str], float | None],
        wanted_input: str = "auto",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend, self._remember, self._start = backend, remember, start_gain
        self._wanted = (wanted_input or "auto").strip().lower()
        self._clock = clock
        self._looked: tuple[float, InterfaceReport | str] | None = None
        self._first: dict[str, float] = {}

    def override_eps(self) -> dict[str, float]:
        return {"gain_db": GAIN_EPS_DB}

    def _report(self) -> InterfaceReport | str:
        """The interface report, or the reason it could not be read. Reused for a few seconds."""
        now = self._clock()
        if self._looked and now - self._looked[0] < INSPECT_TTL:
            return self._looked[1]
        try:
            result: InterfaceReport | str = self._backend.inspect()
        except ActuatorError as exc:
            result = str(exc)
        self._looked = (now, result)
        return result

    def _control(self) -> tuple[GainControl | None, str]:
        """(the gain control the director drives, why there is none)."""
        report = self._report()
        if isinstance(report, str):
            return None, report
        if not report.present:
            return None, report.note or "no supported audio interface found"
        if not report.controls:
            return None, report.note or f"{report.label} has no software-settable gain (analogue knobs)"
        if self._wanted in ("auto", ""):
            chosen = report.controls[0]
        else:
            # "2" picks the input numbered 2 ("gain.2"); a full control id picks that control.
            chosen = next((c for c in report.controls if self._wanted in (c.id, c.id.rsplit(".", 1)[-1])), None)
            if chosen is None:
                return None, f"{report.label} has no software gain on input {self._wanted}"
        if chosen.locked:
            return None, f"{report.label}: {chosen.locked}"
        return chosen, ""

    def availability(self) -> Availability:
        control, why = self._control()
        return Availability(True) if control else Availability(False, why)

    def targets(self) -> list[str]:
        control, _ = self._control()
        return [control.id] if control else []

    def read(self, target: str) -> dict:
        control, _ = self._control()
        if control is None or control.id != target:
            return {}
        self._first.setdefault(target, control.value)
        return {"gain_db": control.value}

    def param(self, key: str, target: str | None = None) -> Param | None:
        base = super().param(key)
        if base is None or not target:
            return base
        report = self._report()
        control = None if isinstance(report, str) else next((c for c in report.controls if c.id == target), None)
        if control is None:
            return None
        start = self._start(target)
        start = self._first.get(target, control.value) if start is None else start
        return Param(
            "gain_db",
            base.label,
            "number",
            max(control.low, start + limits.MIC_GAIN_DB[0]),
            min(control.high, start + limits.MIC_GAIN_DB[1]),
            max(control.step, base.step or control.step),
            "dB",
            min_interval=base.min_interval,
        )

    def window(self, target: str) -> tuple[float, float] | None:
        param = self.param("gain_db", target)
        return (param.low, param.high) if param and param.low is not None and param.high is not None else None

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        if "gain_db" in wanted:
            self._backend.set_gain(target, float(wanted["gain_db"]))
            self._looked = None

    def remember(self, target: str, current: dict) -> None:
        if isinstance(current.get("gain_db"), (int, float)):
            self._remember("gain", target, current["gain_db"])

    def restore(self, target: str, saved: object) -> None:
        if isinstance(saved, bool) or not isinstance(saved, (int, float)):
            raise ActuatorError("no preamp gain recorded")
        self._backend.set_gain(target, float(saved))
