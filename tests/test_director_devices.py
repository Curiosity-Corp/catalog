"""The hardware actuators against fake backends: availability, limits, balance, and what they can never touch."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from streamstack.director import actuators, devices, limits
from streamstack.director.actuators import ActuatorError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def saved() -> tuple[list[tuple[str, str, object]], Callable]:
    log: list[tuple[str, str, object]] = []
    return log, lambda kind, key, value: log.append((kind, key, value))


# -- Canon ------------------------------


def canon_status(**over) -> dict:
    controls = {
        "model": {"value": "Canon EOS R50"},
        "exposure_comp": {"value": "+0.3", "choices": ["-1", "-0.7", "-0.3", "0", "+0.3", "+0.7", "+1"]},
        "whitebalance": {"value": "Color Temperature"},
        "kelvin": {"value": "5200"},
        "focusmode": {"value": "Manual"},
        "iso": {"value": "400"},
    }
    controls.update(over.pop("controls", {}))
    return {"state": "running", "session": "shared", "controls": controls, **over}


class FakeCanon:
    def __init__(self, status: dict | None = None) -> None:
        self.current = status
        self.sets: list[tuple[str, object]] = []
        self.fail: ActuatorError | None = None

    def status(self) -> dict | None:
        return self.current

    def set(self, control: str, value: object) -> None:
        if self.fail:
            raise self.fail
        self.sets.append((control, value))
        if self.current and control in self.current["controls"]:
            self.current["controls"][control]["value"] = str(value)


def canon(status: dict | str | None = "default") -> tuple[devices.CanonActuator, FakeCanon, list]:
    log, remember = saved()
    fake = FakeCanon(canon_status() if status == "default" else status)
    return devices.CanonActuator(fake, remember), fake, log


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "not running"),
        ({"state": "starting"}, "starting"),
        (canon_status(session="pause"), "python3-gphoto2"),
        ({"state": "running", "session": "shared", "controls": {}}, "no controls"),
        (
            canon_status(
                controls={
                    "exposure_comp": {"value": "0", "readonly": "mode dial"},
                    "kelvin": {"value": "5200", "readonly": "x"},
                }
            ),
            "locked",
        ),
        (
            canon_status(
                controls={"exposure_comp": {"value": "0", "readonly": "m"}, "whitebalance": {"value": "Daylight"}}
            ),
            "preset",
        ),
    ],
)
def test_the_canon_is_unavailable_with_a_reason_instead_of_failing(status, reason) -> None:
    actuator, _fake, _ = canon(status)
    state = actuator.availability()
    assert not state.ok and reason in state.reason
    assert actuator.read("x") == {}
    assert actuator.describe()["available"] is False


def test_the_canon_reads_only_what_the_body_lets_it_change() -> None:
    actuator, _fake, _ = canon()
    assert actuator.availability().ok and actuator.targets() == ["Canon EOS R50"]
    assert actuator.read("Canon EOS R50") == {"exposure_comp": 0.3, "kelvin": 5200.0}
    locked, _fake, _ = canon(canon_status(controls={"exposure_comp": {"value": "0", "readonly": "mode dial"}}))
    assert locked.read("Canon EOS R50") == {"kelvin": 5200.0}
    preset, _fake, _ = canon(canon_status(controls={"whitebalance": {"value": "Daylight"}}))
    assert preset.read("Canon EOS R50") == {"exposure_comp": 0.3}
    auto, _fake, _ = canon(canon_status(controls={"whitebalance": {"value": "Auto (ambience)"}}))
    assert "kelvin" in auto.read("Canon EOS R50")


def test_the_canon_changes_use_the_bodys_own_spelling_and_snap_to_100_k() -> None:
    actuator, fake, log = canon()
    current = actuator.read("Canon EOS R50")
    actuator.remember("Canon EOS R50", current)
    actuator.apply("Canon EOS R50", current, {"exposure_comp": 0.64, "kelvin": 5560.0}, 0)
    assert fake.sets == [("exposure_comp", "+0.7"), ("kelvin", 5600)]
    assert log == [
        ("canon", "Canon EOS R50", {"exposure_comp": 0.3, "kelvin": 5200.0, "whitebalance": "Color Temperature"})
    ]


def test_the_canon_is_driven_one_step_inside_its_window_by_the_governor() -> None:
    actuator, _fake, _ = canon()
    governor = actuators.Governor(clock=Clock())
    current = actuator.read("Canon EOS R50")
    values, changes = governor.plan(
        actuator, "Canon EOS R50", current, {"exposure_comp": 5, "kelvin": 9000}, {}, "local", "why"
    )
    assert values["exposure_comp"] == pytest.approx(0.64) and values["kelvin"] == 5600.0
    assert {c.actuator for c in changes} == {"canon-exposure"} and {c.kind for c in changes} == {"camera"}
    top = governor.plan(actuator, "x", {"exposure_comp": 1.0}, {"exposure_comp": 2}, {}, "local", "")
    assert top[1] == []


def test_the_canon_cannot_reach_focus_iso_or_anything_else() -> None:
    actuator, fake, _ = canon()
    current = actuator.read("Canon EOS R50")
    actuator.apply("Canon EOS R50", current, {"focusmode": "AF", "iso": "6400", "exposure_comp": 0.64}, 0)
    assert [c for c, _ in fake.sets] == ["exposure_comp"]
    assert {p.key for p in actuator.params} == {"exposure_comp", "kelvin"}


def test_a_refused_canon_change_is_an_actuator_error_not_a_crash() -> None:
    actuator, fake, _ = canon()
    fake.fail = ActuatorError("the camera is busy")
    with pytest.raises(ActuatorError, match="busy"):
        actuator.apply("Canon EOS R50", {"exposure_comp": 0.3}, {"exposure_comp": 0.64}, 0)
    fake.current = None
    with pytest.raises(ActuatorError, match="not running"):
        actuator.apply("Canon EOS R50", {"exposure_comp": 0.3}, {"exposure_comp": 0.64}, 0)


def test_the_canon_restores_what_was_there_before() -> None:
    actuator, fake, _ = canon()
    actuator.restore("Canon EOS R50", {"exposure_comp": 0.0, "kelvin": 5000.0, "whitebalance": "Color Temperature"})
    assert fake.sets == [("exposure_comp", "0"), ("kelvin", 5000)]
    fake.sets.clear()
    actuator.restore("Canon EOS R50", {"exposure_comp": -0.3, "kelvin": 5000.0, "whitebalance": "Auto (ambience)"})
    assert fake.sets == [("exposure_comp", "-0.3"), ("whitebalance", "Auto (ambience)")]
    with pytest.raises(ActuatorError):
        actuator.restore("Canon EOS R50", "garbage")


def test_headroom_stops_at_the_ends_of_the_window() -> None:
    actuator, _fake, _ = canon()
    assert actuator.headroom({"exposure_comp": 0.9}, 1) and not actuator.headroom({"exposure_comp": 1.0}, 1)
    assert actuator.headroom({"exposure_comp": -0.9}, -1) and not actuator.headroom({"exposure_comp": -1.0}, -1)
    assert not actuator.headroom({}, 1)


# -- Litra ------------------------------

BEAM = devices.LightInfo("Litra Beam 1", "Litra Beam", 30, 400)
GLOW = devices.LightInfo("Litra Glow", "Litra Glow", 20, 250)


class FakeLights:
    def __init__(self, *lights: devices.LightInfo) -> None:
        self.found = list(lights)
        self.state = {i.label: devices.LightState(True, (i.lumen_min + i.lumen_max) // 2, 4500) for i in lights}
        self.writes: list[tuple[str, int | None, int | None]] = []
        self.fail: ActuatorError | None = None

    def lights(self) -> list[devices.LightInfo]:
        return list(self.found)

    def read(self, label: str) -> devices.LightState:
        if self.fail:
            raise self.fail
        return self.state[label]

    def write(self, label: str, lumen: int | None, kelvin: int | None) -> None:
        if self.fail:
            raise self.fail
        self.writes.append((label, lumen, kelvin))
        old = self.state[label]
        self.state[label] = devices.LightState(
            old.on, old.lumen if lumen is None else lumen, old.kelvin if kelvin is None else kelvin
        )


def litra(*lights: devices.LightInfo, clock: Clock | None = None) -> tuple[devices.LitraActuator, FakeLights, list]:
    log, remember = saved()
    fake = FakeLights(*lights)
    return devices.LitraActuator(fake, remember, clock or Clock()), fake, log


def test_without_a_light_the_litra_actuator_is_unavailable_not_broken() -> None:
    actuator, _fake, _ = litra()
    assert not actuator.availability().ok and "no Litra" in actuator.availability().reason
    assert actuator.targets() == [] and actuator.describe()["available"] is False


def test_each_light_is_limited_to_its_own_model_range_and_step() -> None:
    actuator, _fake, _ = litra(BEAM, GLOW)
    beam, glow = actuator.param("lumen", "Litra Beam 1"), actuator.param("lumen", "Litra Glow")
    assert (beam.low, beam.high) == (67.0, 400.0) and (glow.low, glow.high) == (43.0, 250.0)
    assert beam.step == 44.0 and glow.step == 28.0
    assert actuator.param("lumen", "Unknown") is None
    kelvin = actuator.param("kelvin", "Litra Glow")
    assert (kelvin.low, kelvin.high) == (2700.0, 6500.0)


def test_a_light_that_is_off_is_not_driven_and_never_switched_on() -> None:
    actuator, fake, _ = litra(BEAM)
    fake.state["Litra Beam 1"] = devices.LightState(False, 200, 4500)
    assert actuator.read("Litra Beam 1") == {}
    assert actuator.availability().ok
    assert not hasattr(fake, "power") and all(len(w) == 3 for w in fake.writes)


def test_a_failed_light_is_reported_then_tried_again_after_a_minute() -> None:
    clock = Clock()
    actuator, fake, _ = litra(BEAM, clock=clock)
    fake.fail = ActuatorError("Litra Beam: the light did not answer")
    assert actuator.read("Litra Beam 1") == {}
    state = actuator.availability()
    assert not state.ok and "did not answer" in state.reason
    clock.now += devices.ERROR_MEMORY + 1
    fake.fail = None
    assert actuator.availability().ok and actuator.read("Litra Beam 1")["kelvin"] == 4500.0


def test_the_group_moves_towards_the_goal_and_keeps_the_balance_between_lights() -> None:
    actuator, fake, _ = litra(BEAM, GLOW)
    fake.state["Litra Beam 1"] = devices.LightState(True, 400, 5000)  # key at 100 %
    fake.state["Litra Glow"] = devices.LightState(True, 135, 4600)  # fill at 50 %
    readings = {t: actuator.read(t) for t in actuator.targets()}
    plan = actuator.plan_group(0.5, 4000.0, readings)
    key_fraction = (plan["Litra Beam 1"]["lumen"] - 30) / 370
    fill_fraction = (plan["Litra Glow"]["lumen"] - 20) / 230
    assert key_fraction - fill_fraction == pytest.approx(0.5, abs=0.02)  # the 50 point gap between them is kept
    assert plan["Litra Beam 1"]["kelvin"] - plan["Litra Glow"]["kelvin"] == pytest.approx(400, abs=100)
    assert all(2700 <= p["kelvin"] <= 6500 for p in plan.values())


def test_the_goal_never_takes_a_light_below_its_floor() -> None:
    actuator, _fake, _ = litra(BEAM)
    plan = actuator.plan_group(0.0, 2000.0, {"Litra Beam 1": {"lumen": 200.0, "kelvin": 4500.0}})
    assert plan["Litra Beam 1"]["lumen"] >= 67 and plan["Litra Beam 1"]["kelvin"] == 2700


def test_a_light_change_writes_only_what_changed_and_is_remembered_first() -> None:
    actuator, fake, log = litra(BEAM)
    current = actuator.read("Litra Beam 1")
    actuator.remember("Litra Beam 1", current)
    actuator.apply("Litra Beam 1", current, {"lumen": 250.0, "kelvin": current["kelvin"]}, 0)
    assert fake.writes == [("Litra Beam 1", 250, None)]
    assert log == [("light", "Litra Beam 1", current)]


def test_lights_are_restored_to_what_the_person_had() -> None:
    actuator, fake, _ = litra(BEAM)
    actuator.restore("Litra Beam 1", {"lumen": 215.0, "kelvin": 4500.0})
    assert fake.writes == [("Litra Beam 1", 215, 4500)]
    with pytest.raises(ActuatorError):
        actuator.restore("Litra Beam 1", None)


# -- Interface gain ------------------------------


class FakeInterface:
    def __init__(self, report: devices.InterfaceReport | None = None) -> None:
        self.report = report or devices.InterfaceReport(
            True, "Scarlett 2i2 4th Gen", (devices.GainControl("gain.1", "Input 1 gain", 30.0, 0.0, 69.0, 1.0),)
        )
        self.sets: list[tuple[str, float]] = []
        self.fail: ActuatorError | None = None

    def inspect(self) -> devices.InterfaceReport:
        if self.fail:
            raise self.fail
        return self.report

    def set_gain(self, control_id: str, db: float) -> None:
        self.sets.append((control_id, db))
        self.report = devices.InterfaceReport(
            True,
            self.report.label,
            tuple(
                devices.GainControl(
                    c.id, c.label, db if c.id == control_id else c.value, c.low, c.high, c.step, c.locked
                )
                for c in self.report.controls
            ),
        )


def interface(fake: FakeInterface | None = None, start: float | None = None, wanted: str = "auto"):
    log, remember = saved()
    fake = fake or FakeInterface()
    clock = Clock()
    return devices.InterfaceGainActuator(fake, remember, lambda target: start, wanted, clock), fake, log, clock


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (devices.InterfaceReport(False), "no supported audio interface"),
        (devices.InterfaceReport(True, "Scarlett 2i2 3rd Gen", (), "has analogue gain knobs"), "analogue"),
        (
            devices.InterfaceReport(True, "Wave XLR", (), "hardware control is off (STREAMSTACK_WAVE_XLR_USB=off)"),
            "off",
        ),
        (
            devices.InterfaceReport(
                True, "Scarlett", (devices.GainControl("gain.1", "G", 30, 0, 69, 1, "Autogain is running"),)
            ),
            "Autogain",
        ),
    ],
)
def test_the_interface_is_unavailable_with_the_reason_when_it_cannot_take_gain(report, reason) -> None:
    actuator, _fake, _, _ = interface(FakeInterface(report))
    state = actuator.availability()
    assert not state.ok and reason in state.reason
    assert actuator.targets() == [] and actuator.read("gain.1") == {}


def test_an_unreadable_interface_reports_instead_of_raising() -> None:
    fake = FakeInterface()
    fake.fail = ActuatorError("could not list USB devices")
    actuator, _fake, _, _ = interface(fake)
    assert "could not list" in actuator.availability().reason


def test_the_gain_window_is_around_where_auto_mode_found_it() -> None:
    actuator, _fake, _, _ = interface(start=30.0)
    low, high = limits.MIC_GAIN_DB
    assert actuator.window("gain.1") == (30.0 + low, 30.0 + high)
    param = actuator.param("gain_db", "gain.1")
    assert param.step == devices.GAIN_STEP_DB and param.unit == "dB"
    narrow, _f, _, _ = interface(
        FakeInterface(devices.InterfaceReport(True, "S", (devices.GainControl("gain.1", "G", 2.0, 0, 69, 1),))),
        start=2.0,
    )
    assert narrow.window("gain.1")[0] == 0.0  # never below the control's own minimum


def test_the_gain_follows_the_requested_input() -> None:
    report = devices.InterfaceReport(
        True,
        "Scarlett",
        (
            devices.GainControl("gain.1", "Input 1", 30, 0, 69, 1),
            devices.GainControl("gain.2", "Input 2", 20, 0, 69, 1),
        ),
    )
    assert interface(FakeInterface(report))[0].targets() == ["gain.1"]
    assert interface(FakeInterface(report), wanted="2")[0].targets() == ["gain.2"]
    assert interface(FakeInterface(report), wanted="gain.1")[0].targets() == ["gain.1"]
    state = interface(FakeInterface(report), wanted="4")[0].availability()
    assert not state.ok and "input 4" in state.reason


def test_the_governor_moves_the_gain_one_step_and_not_outside_the_window() -> None:
    actuator, _fake, _, _ = interface(start=30.0)
    governor = actuators.Governor(clock=Clock())
    values, changes = governor.plan(actuator, "gain.1", {"gain_db": 30.0}, {"gain_db": 40.0}, {}, "local", "quiet")
    assert values["gain_db"] == 32.0 and changes[0].kind == "interface"
    far, none = governor.plan(actuator, "gain.1", {"gain_db": 36.0}, {"gain_db": 40.0}, {}, "local", "quiet")
    assert far["gain_db"] == 36.0 and none == []  # the window's top (start + 6 dB)


def test_the_gain_is_set_remembered_and_restored() -> None:
    actuator, fake, log, _ = interface(start=30.0)
    actuator.remember("gain.1", {"gain_db": 30.0})
    actuator.apply("gain.1", {"gain_db": 30.0}, {"gain_db": 32.0}, 0)
    assert fake.sets == [("gain.1", 32.0)] and log == [("gain", "gain.1", 30.0)]
    actuator.restore("gain.1", 30.0)
    assert fake.sets[-1] == ("gain.1", 30.0)
    with pytest.raises(ActuatorError):
        actuator.restore("gain.1", True)


def test_the_first_reading_is_the_window_centre_when_no_baseline_is_recorded() -> None:
    actuator, _fake, _, _ = interface(start=None)
    assert actuator.read("gain.1") == {"gain_db": 30.0}
    assert actuator.window("gain.1") == (30.0 + limits.MIC_GAIN_DB[0], 30.0 + limits.MIC_GAIN_DB[1])
