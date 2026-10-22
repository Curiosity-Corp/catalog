"""The actuator registry and the governor: bounds, rate limits, overrides, explanations."""

from __future__ import annotations

import pytest

from streamstack.director import actuators, limits


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Lamp(actuators.Actuator):
    """A made-up light: brightness 0-100 (10 per pass, at most every 60 s) and a mode."""

    id, label, kind = "lamp", "Desk lamp", "light"
    proposal_key = "light"
    params = (
        actuators.Param("level", "Level", "number", 0, 100, 10, "%", min_interval=60.0),
        actuators.Param("mode", "Mode", "choice", choices=("warm", "cool")),
        actuators.Param("on", "On", "bool"),
    )

    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.state = {"level": 50, "mode": "warm", "on": True}
        self.applied: list[dict] = []
        self.remembered: list[dict] = []

    def availability(self) -> actuators.Availability:
        return actuators.Availability(True) if self.present else actuators.Availability(False, "not plugged in")

    def targets(self) -> list[str]:
        return ["desk"]

    def read(self, target: str) -> dict:
        return dict(self.state)

    def apply(self, target: str, current: dict, wanted: dict, seconds: float) -> None:
        self.applied.append(dict(wanted))
        self.state.update(wanted)

    def remember(self, target: str, current: dict) -> None:
        self.remembered.append(dict(current))

    def schema(self) -> dict:
        return {"level": {"type": "number"}}


def test_param_bounds_clamp_to_the_limits_and_one_step() -> None:
    level = Lamp.params[0]
    assert level.bound(50, 100) == 60
    assert level.bound(50, 0) == 40
    assert level.bound(95, 200) == 100
    assert level.bound(5, -50) == 0
    assert level.bound(50, float("nan")) is None
    assert level.bound(50, True) is None
    assert level.bound(50, "bright") is None
    assert Lamp.params[1].bound("warm", "cool") == "cool" and Lamp.params[1].bound("warm", "green") is None
    assert Lamp.params[2].bound(True, False) is False and Lamp.params[2].bound(True, 1) is None


def test_the_registry_lists_what_is_registered_and_what_is_only_planned() -> None:
    registry = actuators.Registry()
    registry.plan("lamp", "Desk lamp", 99)
    registry.register(Lamp(present=False))
    registry.plan("lamp", "Desk lamp", 99)  # already registered: ignored
    registry.plan("canon", "Canon exposure", 15)
    described = {row["id"]: row for row in registry.describe()}
    assert described["lamp"]["available"] is False and described["lamp"]["reason"] == "not plugged in"
    assert described["canon"]["kind"] == "planned" and "#15" in described["canon"]["reason"]
    assert registry.available() == []
    with pytest.raises(ValueError, match="already"):
        registry.register(Lamp())
    with pytest.raises(KeyError):
        registry.get("nothing")


def test_registering_replaces_the_planned_placeholder_and_extends_the_schema() -> None:
    registry = actuators.Registry()
    registry.plan("lamp", "Desk lamp", 99)
    lamp = registry.register(Lamp())
    assert [row["id"] for row in registry.describe()] == ["lamp"]
    base = {"type": "object", "properties": {"video": {"type": "object"}}, "required": ["notes"]}
    merged = registry.schema(base)
    assert merged["properties"]["light"]["properties"] == {"level": {"type": "number"}}
    assert "light" not in base["properties"]  # the base is never mutated
    lamp.present = False
    assert "light" not in registry.schema(base)["properties"]


def governor() -> tuple[actuators.Governor, Clock]:
    clock = Clock()
    return actuators.Governor(clock=clock, hold=1800.0), clock


def test_the_governor_bounds_explains_and_skips_no_ops() -> None:
    gov, _ = governor()
    lamp = Lamp()
    values, changes = gov.plan(
        lamp, "desk", lamp.read("desk"), {"level": 90, "mode": "warm"}, {"level": "Too dim"}, "claude"
    )
    assert values["level"] == 60 and values["mode"] == "warm"
    assert [(c.key, c.before, c.after, c.reason, c.source, c.kind) for c in changes] == [
        ("level", 50, 60, "Too dim", "claude", "light")
    ]
    assert gov.plan(lamp, "desk", lamp.read("desk"), {"unknown": 1, "level": 50}, {}, "claude")[1] == []


def test_a_reason_is_always_present() -> None:
    gov, _ = governor()
    lamp = Lamp()
    _, changes = gov.plan(lamp, "desk", lamp.read("desk"), {"mode": "cool"}, {}, "persona", "Easing towards Executive")
    assert changes[0].reason == "Easing towards Executive"
    _, changes = gov.plan(lamp, "desk", lamp.read("desk"), {"mode": "cool"}, {}, "persona")
    assert changes[0].reason == "Mode adjusted"
    assert changes[0].public()["actuator"] == "lamp"


def test_a_parameter_cannot_change_again_before_its_interval() -> None:
    gov, clock = governor()
    lamp = Lamp()
    values, changes = gov.plan(lamp, "desk", lamp.read("desk"), {"level": 60}, {}, "claude")
    gov.committed("lamp", "desk", values, [c.key for c in changes])
    assert gov.plan(lamp, "desk", values, {"level": 70}, {}, "claude")[1] == []
    clock.now += 61
    assert gov.plan(lamp, "desk", values, {"level": 70}, {}, "claude")[1][0].after == 70
    assert gov.plan(lamp, "desk", values, {"mode": "cool"}, {}, "claude")[1]  # other keys are independent


def test_a_value_the_person_moved_is_held_then_released() -> None:
    gov, clock = governor()
    lamp = Lamp()
    gov.remember_state("lamp", "desk", lamp.read("desk"))
    values, _changes = gov.plan(lamp, "desk", lamp.read("desk"), {"level": 60}, {}, "local")
    gov.committed("lamp", "desk", values, ["level"])
    assert gov.sync("lamp", "desk", values, {"level": 1.0}) == []  # nothing moved
    moved = dict(values, level=20)  # the person turned it down
    assert gov.sync("lamp", "desk", moved, {"level": 1.0}) == ["level"]
    assert gov.is_held("lamp", "desk", "level")
    assert gov.plan(lamp, "desk", moved, {"level": 30}, {}, "claude")[1] == []
    clock.now += 1799
    assert gov.is_held("lamp", "desk", "level")
    clock.now += 2
    assert not gov.is_held("lamp", "desk", "level")
    assert gov.plan(lamp, "desk", moved, {"level": 30}, {}, "claude")[1][0].after == 30


def test_small_drift_inside_the_tolerance_is_not_an_override() -> None:
    gov, _ = governor()
    gov.remember_state("lamp", "desk", {"level": 50.0})
    assert gov.sync("lamp", "desk", {"level": 50.4}, {"level": 1.0}) == []
    assert gov.sync("lamp", "desk", {"level": 53.0}, {"level": 1.0}) == ["level"]


def test_a_choice_changed_by_hand_is_an_override_too() -> None:
    gov, _ = governor()
    gov.remember_state("lamp", "desk", {"mode": "warm"})
    assert gov.sync("lamp", "desk", {"mode": "cool"}, {}) == ["mode"]
    gov.release("lamp", "desk", "mode")
    assert not gov.is_held("lamp", "desk", "mode")


def test_the_shipped_look_actuator_uses_the_shared_hard_limits() -> None:
    by_key = {p.key: p for p in actuators.LOOK_PARAMS}
    for key, (low, high) in limits.LIMITS.items():
        assert (by_key[key].low, by_key[key].high, by_key[key].step) == (low, high, limits.AI_STEP[key])
    assert by_key["lut_enabled"].kind == "bool"


def test_the_voice_preset_actuator_only_loads_installed_presets() -> None:
    loaded: list[str] = []
    voice = actuators.VoicePresetActuator(
        presets=lambda: ["A", "B"], current=lambda: "A", load=loaded.append, remember=lambda *a: loaded.append("kept")
    )
    voice.apply("input", {"voice_preset": "A"}, {"voice_preset": "Z"}, 0)
    voice.apply("input", {"voice_preset": "A"}, {"voice_preset": "B"}, 0)
    voice.remember("input", {"voice_preset": "A"})
    assert loaded == ["B", "kept"]
    empty = actuators.VoicePresetActuator(presets=lambda: [], current=lambda: None, load=loaded.append, remember=print)
    assert empty.describe()["available"] is False


def test_the_mic_actuator_reports_a_missing_microphone() -> None:
    writes: list[float] = []
    mic = actuators.MicGainActuator(
        source=lambda: "",
        read_gain=lambda s: 0.0,
        write_gain=lambda s, db: writes.append(db),
        remember=print,
        start_gain=lambda: 0.0,
    )
    assert mic.availability().ok is False and mic.targets() == []
    mic = actuators.MicGainActuator(
        source=lambda: "m",
        read_gain=lambda s: -3.0,
        write_gain=lambda s, db: writes.append(db),
        remember=print,
        start_gain=lambda: 0.0,
    )
    assert mic.read("m") == {"gain_db": -3.0} and mic.window() == (-12.0, 6.0)
    mic.apply("m", {"gain_db": -3.0}, {"gain_db": -1.0}, 0)
    assert writes == [-1.0]
