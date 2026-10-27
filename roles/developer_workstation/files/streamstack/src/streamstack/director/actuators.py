"""One registry for everything the director can change, and the rules every change obeys.

An actuator wraps one controllable thing (the program look, the microphone gain, the voice preset, Canon exposure and
white balance, Litra lights, audio-interface hardware gain) behind the same small interface:
what it can read, which parameters it has with their hard limits, how far one pass may move each, and how to apply
and undo a change. Nothing outside this module needs to know which hardware is behind an actuator.

The `Governor` is the safety layer in front of all of them. Whoever proposes a change (a local loop, a persona, a
learned baseline, Claude) the governor makes sure that:

* a value the person moved by hand stays theirs for `OVERRIDE_HOLD` seconds (detected by comparing what is in
  place now with what the director last set);
* no parameter changes more often than its `min_interval`;
* every value is inside its hard limits and moves at most one step per pass (`Param.bound`);
* every change is a `Change` record with a reason, which the director journals, so it can be explained and
  (through the baseline the actuator remembers before its first change) undone.

To add hardware: subclass `Actuator`, give it a unique `id`, and `Registry.register` it. A `Registry.plan`
placeholder with the same id is replaced automatically. The hardware actuators are in `director.devices` and are
bound to the real devices by `director.adapters`.
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from streamstack.director import limits


@dataclass(frozen=True)
class Param:
    """One adjustable value: its hard limits, the largest move per pass and the least time between moves."""

    key: str
    label: str
    kind: str = "number"  # number, bool or choice
    low: float | None = None
    high: float | None = None
    step: float | None = None
    unit: str = ""
    min_interval: float = 0.0
    choices: tuple[str, ...] = ()

    def bound(self, before: object, wanted: object) -> object | None:
        """`wanted` clamped to the limits and to one step from `before`; None when it is not a usable value."""
        if self.kind == "bool":
            return wanted if isinstance(wanted, bool) else None
        if self.kind == "choice":
            return wanted if isinstance(wanted, str) and (not self.choices or wanted in self.choices) else None
        if isinstance(wanted, bool) or not isinstance(wanted, (int, float)) or not math.isfinite(wanted):
            return None
        value = float(wanted)
        if isinstance(before, (int, float)) and not isinstance(before, bool) and self.step is not None:
            value = max(before - self.step, min(before + self.step, value))
        if self.low is not None and self.high is not None:
            value = max(self.low, min(self.high, value))
        return round(value, 4)

    def public(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "low": self.low,
            "high": self.high,
            "step": self.step,
            "unit": self.unit,
        }


class ActuatorError(RuntimeError):
    """A device that was there refused or failed a change. The director journals it and carries on."""


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class Change:
    """One value the director moved (or would move), with why."""

    actuator: str
    target: str
    key: str
    before: object
    after: object
    reason: str
    source: str  # local, persona, baseline or claude
    kind: str = "video"

    def public(self) -> dict:
        return {
            "actuator": self.actuator,
            "target": self.target,
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "source": self.source,
            "kind": self.kind,
        }


class Actuator(ABC):
    """Something the director can adjust. Implementations never raise for an absent device; they say so."""

    id: str = ""
    label: str = ""
    kind: str = "video"  # video, audio, light, camera or interface: how the Console colours its changes
    proposal_key: str | None = None  # the section of Claude's proposal that addresses it, if any
    baseline_kind: str = ""  # the section of baseline.json that `streamstack auto reset` restores
    params: tuple[Param, ...] = ()

    @abstractmethod
    def availability(self) -> Availability:
        """Whether it can be driven right now, and if not, why (shown on the Auto page)."""

    @abstractmethod
    def targets(self) -> list[str]:
        """What it can address now (camera sources, microphones...)."""

    @abstractmethod
    def read(self, target: str) -> dict:
        """The current values of every parameter for `target`."""

    @abstractmethod
    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        """Move `target` from `current` to `wanted` (values already bounded), ramping over `seconds` when it can."""

    def remember(self, target: str, current: dict) -> None:  # noqa: B027
        """Record `current` as the value to return to on undo (never overwrites an earlier record)."""

    def restore(self, target: str, saved: object) -> None:  # noqa: B027
        """Put `target` back to what `remember` recorded (`streamstack auto reset`). Raises ActuatorError."""

    def override_eps(self) -> dict[str, float]:
        """How far a value may differ from what the director set before it counts as the person's doing."""
        return {}

    def param(self, key: str, target: str | None = None) -> Param | None:
        """The parameter `key`; an actuator whose limits depend on the device (a light's range) overrides this."""
        return next((p for p in self.params if p.key == key), None)

    def schema(self) -> dict | None:
        """JSON-schema properties for Claude's proposal section, or None when Claude does not drive this."""
        return None

    def describe(self) -> dict:
        state = self.availability()
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "available": state.ok,
            "reason": state.reason,
            "params": [p.public() for p in self.params],
        }


@dataclass
class Registry:
    _actuators: dict[str, Actuator] = field(default_factory=dict)
    _planned: dict[str, dict] = field(default_factory=dict)

    def register(self, actuator: Actuator) -> Actuator:
        if not actuator.id:
            raise ValueError("an actuator needs an id")
        if actuator.id in self._actuators:
            raise ValueError(f"actuator {actuator.id!r} is already registered")
        self._actuators[actuator.id] = actuator
        self._planned.pop(actuator.id, None)
        return actuator

    def plan(self, actuator_id: str, label: str, issue: int) -> None:
        """Announce hardware that is being built elsewhere; shown as 'planned' until it registers."""
        if actuator_id not in self._actuators:
            self._planned[actuator_id] = {"id": actuator_id, "label": label, "issue": issue}

    def get(self, actuator_id: str) -> Actuator:
        try:
            return self._actuators[actuator_id]
        except KeyError:
            raise KeyError(f"no actuator {actuator_id!r}") from None

    def find(self, actuator_id: str) -> Actuator | None:
        """The actuator, or None when it is not registered (a hardware toggle turned off)."""
        return self._actuators.get(actuator_id)

    def all(self) -> list[Actuator]:
        return list(self._actuators.values())

    def available(self) -> list[Actuator]:
        return [a for a in self._actuators.values() if a.availability().ok]

    def describe(self) -> list[dict]:
        rows = [a.describe() for a in self._actuators.values()]
        rows += [
            {**p, "kind": "planned", "available": False, "reason": f"planned, issue #{p['issue']}", "params": []}
            for p in self._planned.values()
        ]
        return rows

    def schema(self, base: dict) -> dict:
        """`base` (a proposal schema) with the properties of every available actuator that Claude drives."""
        merged = {
            **base,
            "properties": {k: dict(v) if isinstance(v, dict) else v for k, v in base["properties"].items()},
        }
        for actuator in self.available():
            fragment = actuator.schema()
            if not fragment or not actuator.proposal_key:
                continue
            section = merged["properties"].setdefault(
                actuator.proposal_key, {"type": "object", "properties": {}, "additionalProperties": False}
            )
            section["properties"] = {**section.get("properties", {}), **fragment}
        return merged


class Governor:
    """Rate limits, step limits and user overrides in front of every actuator."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, hold: float = limits.OVERRIDE_HOLD) -> None:
        self._clock = clock
        self.hold = hold
        self._set: dict[tuple[str, str], dict] = {}
        self._held: dict[tuple[str, str, str], float] = {}
        self._changed: dict[tuple[str, str, str], float] = {}

    def sync(self, actuator: str, target: str, current: dict, eps: dict[str, float]) -> list[str]:
        """Compare `current` with what the director last set; keys that differ were moved by the person.

        Those keys are held (the director leaves them alone) and the first sight of a target only records it.
        """
        last = self._set.get((actuator, target))
        moved: list[str] = []
        if last is not None:
            for key, value in last.items():
                now = current.get(key)
                if isinstance(value, (int, float)) and isinstance(now, (int, float)) and not isinstance(value, bool):
                    if abs(now - value) > eps.get(key, 0.01):
                        moved.append(key)
                elif now is not None and now != value:
                    moved.append(key)
        for key in moved:
            self._held[(actuator, target, key)] = self._clock() + self.hold
        if last is not None and moved:
            self._set[(actuator, target)] = {**last, **{k: current[k] for k in moved}}
        return moved

    def is_held(self, actuator: str, target: str, key: str) -> bool:
        until = self._held.get((actuator, target, key))
        if until is None:
            return False
        if self._clock() >= until:
            del self._held[(actuator, target, key)]
            return False
        return True

    def release(self, actuator: str, target: str, key: str) -> None:
        self._held.pop((actuator, target, key), None)

    def interval_ok(self, actuator: str, target: str, key: str, min_interval: float) -> bool:
        last = self._changed.get((actuator, target, key))
        return last is None or self._clock() - last >= min_interval

    def committed(self, actuator: str, target: str, values: dict, keys: list[str]) -> None:
        """Record that the director set `keys` to `values[key]` now."""
        stored = self._set.setdefault((actuator, target), {})
        for key in keys:
            stored[key] = values[key]
            self._changed[(actuator, target, key)] = self._clock()

    def remember_state(self, actuator: str, target: str, current: dict) -> None:
        """Note what is in place so a later difference can be recognised as the person's doing."""
        stored = self._set.setdefault((actuator, target), {})
        for key, value in current.items():
            stored.setdefault(key, value)

    def plan(
        self,
        actuator: Actuator,
        target: str,
        current: dict,
        wanted: dict,
        reasons: dict[str, str],
        source: str,
        default_reason: str = "",
    ) -> tuple[dict, list[Change]]:
        """The bounded, allowed subset of `wanted` as (new values, changes). Held or too-soon keys are skipped."""
        values = dict(current)
        changes: list[Change] = []
        for key, raw in wanted.items():
            param = actuator.param(key, target)
            if param is None or key not in current:
                continue
            if self.is_held(actuator.id, target, key) or not self.interval_ok(
                actuator.id, target, key, param.min_interval
            ):
                continue
            value = param.bound(current[key], raw)
            if value is None or value == current[key]:
                continue
            values[key] = value
            changes.append(
                Change(
                    actuator.id,
                    target,
                    key,
                    current[key],
                    value,
                    reasons.get(key) or default_reason or f"{param.label} adjusted",
                    source,
                    actuator.kind,
                )
            )
        return values, changes


# ---------------------------------------------------------------------------
# The shipped actuators. Device access is injected so this module stays free of HTTP, PulseAudio and the voice chain.
# ---------------------------------------------------------------------------

_LOOK_LABELS = (
    ("brightness", "Brightness"),
    ("contrast", "Contrast"),
    ("gamma", "Gamma"),
    ("saturation", "Saturation"),
    ("warmth", "Warmth"),
    ("lut_amount", "LUT strength"),
)
LOOK_PARAMS = (
    *(Param(key, label, "number", *limits.LIMITS[key], limits.AI_STEP[key]) for key, label in _LOOK_LABELS),
    Param("lut_enabled", "Cinematic LUT", "bool"),
    Param("sharpen_enabled", "Sharpen", "bool"),
)


class ProgramLookActuator(Actuator):
    """The colour grade, LUT and Sharpen of the camera on air, applied live by the native video engine."""

    id = "program-look"
    label = "Program picture look"
    kind = "video"
    proposal_key = "video"
    baseline_kind = "look"
    params = LOOK_PARAMS

    def override_eps(self) -> dict[str, float]:
        return dict(limits.OVERRIDE_EPS)

    def __init__(
        self,
        connect: Callable[[], object | None],
        read_look: Callable[[object, str], dict],
        apply_look: Callable[[object, str, dict, dict, float], object],
        remember: Callable[[str, str, object], None],
        sources: Callable[[object], list[str]],
    ) -> None:
        self._connect, self._read, self._apply, self._remember = connect, read_look, apply_look, remember
        self._sources = sources

    def availability(self) -> Availability:
        return Availability(True) if self._connect() else Availability(False, "the Console is not running")

    def targets(self) -> list[str]:
        link = self._connect()
        return list(self._sources(link)) if link else []

    def read(self, target: str) -> dict:
        link = self._connect()
        if not link:
            raise RuntimeError("the Console is not running")
        return self._read(link, target)

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        link = self._connect()
        if not link:
            raise RuntimeError("the Console is not running")
        self._apply(link, target, current, wanted, seconds)

    def remember(self, target: str, current: dict) -> None:
        self._remember("look", target, current)

    def schema(self) -> dict:
        numbers = {p.key: {"type": "number"} for p in self.params if p.kind == "number"}
        flags = {p.key: {"type": "boolean"} for p in self.params if p.kind == "bool"}
        return {**numbers, **flags}


class MicGainActuator(Actuator):
    """The call microphone's input gain, kept within a window around where auto mode found it."""

    id = "mic-gain"
    label = "Microphone gain"
    kind = "audio"
    baseline_kind = "mic"
    params = (Param("gain_db", "Input gain", "number", None, None, 2.0, "dB"),)

    def override_eps(self) -> dict[str, float]:
        return {"gain_db": 0.6}

    def __init__(
        self,
        source: Callable[[], str],
        read_gain: Callable[[str], float | None],
        write_gain: Callable[[str, float], None],
        remember: Callable[[str, str, object], None],
        start_gain: Callable[[], float | None],
    ) -> None:
        self._source, self._read, self._write, self._remember, self._start = (
            source,
            read_gain,
            write_gain,
            remember,
            start_gain,
        )

    def availability(self) -> Availability:
        return Availability(True) if self._source() else Availability(False, "no microphone found")

    def targets(self) -> list[str]:
        source = self._source()
        return [source] if source else []

    def read(self, target: str) -> dict:
        gain = self._read(target)
        return {"gain_db": gain} if gain is not None else {}

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        if "gain_db" in wanted:
            self._write(target, float(wanted["gain_db"]))

    def remember(self, target: str, current: dict) -> None:
        start = self._start()
        self._remember("mic", target, start if start is not None else current.get("gain_db"))

    def window(self) -> tuple[float, float] | None:
        """The allowed gain range: MIC_GAIN_DB around where auto mode found the microphone."""
        start = self._start()
        return None if start is None else (start + limits.MIC_GAIN_DB[0], start + limits.MIC_GAIN_DB[1])


class VoicePresetActuator(Actuator):
    """The StreamStack voice chain preset (stage parameters; see audiochain)."""

    id = "voice-preset"
    label = "Voice preset"
    kind = "audio"
    proposal_key = "audio"
    baseline_kind = "preset"
    params = (Param("voice_preset", "Voice preset", "choice", min_interval=600.0),)

    def __init__(
        self,
        presets: Callable[[], list[str]],
        current: Callable[[], str | None],
        load: Callable[[str], None],
        remember: Callable[[str, str, object], None],
    ) -> None:
        self._presets, self._current, self._load, self._remember = presets, current, load, remember

    def availability(self) -> Availability:
        return Availability(True) if self._presets() else Availability(False, "no voice presets installed")

    def targets(self) -> list[str]:
        return ["input"]

    def read(self, target: str) -> dict:
        return {"voice_preset": self._current()}

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        name = wanted.get("voice_preset")
        if isinstance(name, str) and name in self._presets():
            self._load(name)

    def remember(self, target: str, current: dict) -> None:
        if current.get("voice_preset"):
            self._remember("preset", target, current["voice_preset"])

    def schema(self) -> dict:
        return {"voice_preset": {"type": ["string", "null"]}}
