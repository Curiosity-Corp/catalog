"""Auto mode driving the camera, the lights and the interface's preamp (fakes only; no real device is ever opened)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_auto import FakeOBS, journal  # noqa: F401
from test_auto_director import FRAME, REPLY, fast, make  # noqa: F401
from test_director_devices import FakeCanon, FakeInterface, FakeLights, canon_status

from streamstack import auto, system
from streamstack.director import actuators, adapters, devices, imagefeatures, pilot, settings, status

BEAM = devices.LightInfo("Litra Beam 1", "Litra Beam", 30, 400)
NYC = {"STREAMSTACK_LATITUDE": "40.71", "STREAMSTACK_LONGITUDE": "-74.01", "STREAMSTACK_PROPERTY": "Test house"}
EDT = timezone(timedelta(hours=-4))


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def at(monkeypatch, hour: int) -> None:
    moment = datetime(2026, 10, 22, hour, 0, tzinfo=EDT)

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(pilot, "datetime", Fixed)


def rig(monkeypatch, lights=None, camera=None, interface=None, **cfg):
    """An Auto whose registry holds fake hardware actuators; returns (auto, obs, fakes, clock)."""
    clock = Clock()
    fakes = {"lights": lights, "canon": camera, "interface": interface}

    def hardware(settings_, remember, start_gain):
        out = []
        if camera is not None:
            out.append(devices.CanonActuator(camera, remember))
        if lights is not None:
            out.append(devices.LitraActuator(lights, remember, clock))
        if interface is not None:
            out.append(devices.InterfaceGainActuator(interface, remember, start_gain, clock=clock))
        return out

    monkeypatch.setattr(adapters, "hardware_actuators", hardware)
    a, obs, _sent = make(monkeypatch, iter([REPLY] * 4), **{**NYC, **cfg})
    a.governor = actuators.Governor(clock=clock)
    return a, obs, fakes, clock


def lit(fake: FakeLights, lumen: int, kelvin: int, label: str = "Litra Beam 1", on: bool = True) -> None:
    fake.state[label] = devices.LightState(on, lumen, kelvin)


# -- lights by time of day ------------------------------


@pytest.mark.parametrize("hour", [0, 6, 10, 14])
def test_the_dark_slots_raise_and_warm_the_light_from_the_persona_baseline(monkeypatch, hour) -> None:
    settings.set_persona("executive")
    at(monkeypatch, hour)
    a, _obs, _f, _c = rig(monkeypatch, lights=FakeLights(BEAM))
    a.pilot.refresh()
    fraction, kelvin, source, why = a.pilot.light_goal()
    baseline = a.pilot.persona["light"]
    assert source == "persona" and a.pilot.persona["name"] in why
    if a.pilot.slot in ("night", "dawn", "evening"):
        assert fraction > baseline["brightness"] and kelvin < baseline["kelvin"]
    elif a.pilot.slot == "afternoon":
        assert fraction < baseline["brightness"] and kelvin > baseline["kelvin"]
    else:
        assert fraction == pytest.approx(baseline["brightness"]) and kelvin == baseline["kelvin"]


def test_the_four_logins_get_four_different_lights(monkeypatch) -> None:
    settings.set_persona("executive")
    goals = {}
    for hour in (0, 6, 10, 14):
        at(monkeypatch, hour)
        a, _obs, _f, _c = rig(monkeypatch, lights=FakeLights(BEAM))
        a.pilot.refresh()
        goals[hour] = a.pilot.light_goal()[:2]
    assert goals[0][0] > goals[10][0] > goals[14][0] and goals[0][1] < goals[10][1] < goals[14][1]
    assert goals[6][0] > goals[10][0] and goals[6][1] < goals[10][1]  # the early morning is still dark


def test_the_lights_ease_to_the_goal_in_small_steps_with_reasons(monkeypatch) -> None:
    settings.set_persona("executive")
    at(monkeypatch, 0)
    fake = FakeLights(BEAM)
    lit(fake, 150, 5600)
    a, _obs, _f, clock = rig(monkeypatch, lights=fake)
    first = a.pilot.light_goal
    a.pilot.refresh()
    fraction, kelvin = first()[:2]
    wanted_lumen = 30 + fraction * 370
    previous = 150.0
    for _ in range(40):
        a.light_tick()
        clock.now += 60
        lumen, _k = fake.state["Litra Beam 1"].lumen, fake.state["Litra Beam 1"].kelvin
        assert abs(lumen - previous) <= 0.12 * 370 + 1  # one step per pass
        previous = lumen
    assert fake.state["Litra Beam 1"].lumen == pytest.approx(wanted_lumen, abs=devices.LIGHT_EPS_LUMEN)
    assert fake.state["Litra Beam 1"].kelvin == pytest.approx(kelvin, abs=devices.LIGHT_EPS_KELVIN + 50)
    changes = [e for e in journal() if e["event"] == "change" and e["actuator"] == "litra"]
    assert changes and all(
        c["source"] == "persona" and "Executive" in c["reason"] and c["kind"] == "light" for c in changes
    )
    assert all(c["key"] in ("lumen", "kelvin") for c in changes)
    settled = len(fake.writes)
    a.light_tick()
    assert len(fake.writes) == settled  # nothing left to ease


def test_a_light_the_person_turned_off_stays_off_and_is_never_written(monkeypatch) -> None:
    settings.set_persona("executive")
    fake = FakeLights(BEAM)
    lit(fake, 150, 5600, on=False)
    a, _obs, _f, _c = rig(monkeypatch, lights=fake)
    a.light_tick()
    assert fake.writes == []


def test_a_light_moved_by_hand_is_left_alone_for_a_while(monkeypatch) -> None:
    settings.set_persona("executive")
    at(monkeypatch, 0)
    fake = FakeLights(BEAM)
    lit(fake, 150, 5600)
    a, _obs, _f, clock = rig(monkeypatch, lights=fake)
    a.light_tick()
    assert fake.writes
    lit(fake, 390, 3000)  # the person grabbed the light
    clock.now += 60
    writes = len(fake.writes)
    a.light_tick()
    assert len(fake.writes) == writes
    assert any(e["event"] == "override" and e["actuator"] == "litra" for e in journal())


def test_what_the_person_kept_by_hand_replaces_the_baseline_for_that_slot(monkeypatch) -> None:
    from streamstack.director import learning

    settings.set_persona("executive")
    at(monkeypatch, 0)
    fake = FakeLights(BEAM)
    lit(fake, 150, 5600)
    a, _obs, _f, clock = rig(monkeypatch, lights=fake)
    a.light_tick()
    lit(fake, 300, 3600)  # moved by hand
    clock.now += 60
    a.light_tick()
    a.ai_tick(force=True)
    kept = learning.get_light(a.pilot.place, a.pilot.persona["id"], a.pilot.slot)
    assert kept is not None and kept[0] == pytest.approx((300 - 30) / 370, abs=0.01) and kept[1] == 3600
    fraction, kelvin, source, why = a.pilot.light_goal()
    assert source == "baseline" and "kept" in why and (fraction, kelvin) == pytest.approx(kept)


def test_the_director_what_it_set_itself_is_not_learned_as_kept(monkeypatch) -> None:
    from streamstack.director import learning

    settings.set_persona("executive")
    at(monkeypatch, 0)
    fake = FakeLights(BEAM)
    lit(fake, 150, 5600)
    a, _obs, _f, _c = rig(monkeypatch, lights=fake)
    a.light_tick()
    a.ai_tick(force=True)
    assert learning.get_light(a.pilot.place, a.pilot.persona["id"], a.pilot.slot) is None


def test_a_persona_without_a_light_plan_leaves_the_lights_alone(monkeypatch) -> None:
    fake = FakeLights(BEAM)
    a, _obs, _f, _c = rig(monkeypatch, lights=fake)
    a.pilot.persona = {**a.pilot.persona}
    a.pilot.persona.pop("light", None)
    monkeypatch.setattr(a.pilot, "refresh", lambda now=None: None)
    a.light_tick()
    assert fake.writes == []


def test_a_failing_light_is_journalled_and_the_loop_carries_on(monkeypatch) -> None:
    settings.set_persona("executive")
    at(monkeypatch, 0)
    fake = FakeLights(BEAM)
    lit(fake, 150, 5600)
    a, _obs, _f, _c = rig(monkeypatch, lights=fake)
    original = fake.write

    def broken(label, lumen, kelvin):
        raise actuators.ActuatorError("Litra Beam 1: the light did not answer")

    fake.write = broken
    a.light_tick()
    assert any(e["event"] == "change-failed" and "did not answer" in e["error"] for e in journal())
    fake.write = original
    assert a.registry.get("litra").describe()["available"] is False


# -- the Canon ------------------------------


def exposure_stats(monkeypatch, luma: float) -> None:
    monkeypatch.setattr(
        auto, "picture_stats", lambda frame, box=None: {"YAVG": luma, "YHIGH": 200.0, "YMIN": 16, "YMAX": 235}
    )


def test_a_dark_picture_is_lifted_with_the_cameras_exposure_compensation_before_obs(monkeypatch) -> None:
    settings.set_persona("late-night-calm")
    camera = FakeCanon(canon_status())
    a, obs, _f, _clock = rig(monkeypatch, camera=camera, STREAMSTACK_AUTO_PERSONA_LOOK="false")
    low, _ = a.pilot.frame_band()
    exposure_stats(monkeypatch, low - 30)
    a.video_tick()
    assert camera.sets == [("exposure_comp", "+0.7")] and obs.writes == []
    event = next(e for e in journal() if e["event"] == "change" and e["actuator"] == "canon-exposure")
    assert event["source"] == "local" and "exposure compensation" in event["reason"] and event["kind"] == "camera"
    a.video_tick()  # inside the 30 s minimum interval: the camera is settling, OBS waits too
    assert len(camera.sets) == 1 and obs.writes == []


def test_obs_takes_over_when_the_camera_is_at_its_limit_or_absent(monkeypatch) -> None:
    settings.set_persona("late-night-calm")
    camera = FakeCanon(canon_status(controls={"exposure_comp": {"value": "+1", "choices": ["0", "+0.7", "+1"]}}))
    a, obs, _f, _c = rig(monkeypatch, camera=camera, STREAMSTACK_AUTO_PERSONA_LOOK="false")
    exposure_stats(monkeypatch, a.pilot.frame_band()[0] - 30)
    a.video_tick()
    assert camera.sets == [] and obs.filters[auto.COLOR_FILTER][1]["gamma"] > 0
    b, obs2, _f, _c = rig(monkeypatch, STREAMSTACK_AUTO_PERSONA_LOOK="false")
    b.video_tick()
    assert obs2.filters[auto.COLOR_FILTER][1]["gamma"] > 0


def test_obs_takes_over_when_the_camera_change_fails(monkeypatch) -> None:
    settings.set_persona("late-night-calm")
    camera = FakeCanon(canon_status())
    camera.fail = actuators.ActuatorError("camera busy")
    a, obs, _f, _c = rig(monkeypatch, camera=camera, STREAMSTACK_AUTO_PERSONA_LOOK="false")
    exposure_stats(monkeypatch, a.pilot.frame_band()[0] - 30)
    a.video_tick()
    assert obs.filters[auto.COLOR_FILTER][1]["gamma"] > 0
    assert any(e["event"] == "change-failed" for e in journal())


def test_a_camera_the_person_changed_is_left_to_them(monkeypatch) -> None:
    settings.set_persona("late-night-calm")
    camera = FakeCanon(canon_status())
    a, _obs, _f, clock = rig(monkeypatch, camera=camera, STREAMSTACK_AUTO_PERSONA_LOOK="false")
    exposure_stats(monkeypatch, a.pilot.frame_band()[0] - 30)
    a.video_tick()
    camera.current["controls"]["exposure_comp"]["value"] = "-0.7"  # dialled down by hand
    clock.now += 120
    a.video_tick()
    assert len(camera.sets) == 1 and any(
        e["event"] == "override" and e["actuator"] == "canon-exposure" for e in journal()
    )


def test_a_cool_picture_raises_the_cameras_kelvin_one_step_per_pass(monkeypatch) -> None:
    settings.set_persona("executive")  # 4800 K wanted: warm index about +0.11
    camera = FakeCanon(canon_status())
    a, _obs, _f, _clock = rig(monkeypatch, camera=camera, STREAMSTACK_AUTO_PERSONA_LOOK="false")
    a.ai_tick(force=True)  # FRAME reads neutral (warm_index 0.02) against a warm persona: too cool
    assert camera.sets == [("kelvin", 5600)]
    event = next(e for e in journal() if e["event"] == "change" and e["key"] == "kelvin")
    assert "cooler" in event["reason"] and event["actuator"] == "canon-exposure"


def test_the_camera_kelvin_replaces_the_obs_warmth_easing(monkeypatch) -> None:
    settings.set_persona("executive")
    with_canon, _obs, _f, _c = rig(monkeypatch, camera=FakeCanon(canon_status()))
    with_canon.video_tick()
    assert "warmth" not in {e["key"] for e in journal() if e["event"] == "change" and e["actuator"] == "obs-look"}


def test_the_local_backend_still_corrects_white_balance(monkeypatch) -> None:
    settings.set_persona("executive")
    camera = FakeCanon(canon_status())
    a, _obs, _f, _c = rig(monkeypatch, camera=camera, STREAMSTACK_AUTO_BACKEND="local")
    a.ai_tick(force=True)
    assert camera.sets and camera.sets[0][0] == "kelvin"


def test_no_canon_means_no_camera_actuator_activity(monkeypatch) -> None:
    a, _obs, _f, _c = rig(monkeypatch)
    a.ai_tick(force=True)
    assert not [e for e in journal() if e.get("actuator") == "canon-exposure"]


# -- the interface ------------------------------

LOUD = {"peak_db": -0.5, "rms_peak_db": -8.0, "rms_db": -20.0}
QUIET = {"peak_db": -12.0, "rms_peak_db": -32.0, "rms_db": -45.0}


def audio(monkeypatch, a: auto.Auto, stats: dict) -> None:
    a.mic = "mic"
    monkeypatch.setattr(auto, "voice_stats", lambda source, seconds=4.0: dict(stats))
    monkeypatch.setattr(auto, "source_gain_db", lambda source: 0.0)
    monkeypatch.setattr(auto, "set_source_gain_db", lambda source, db: pytest.fail("the software gain was used"))


def test_hardware_gain_comes_before_the_software_gain(monkeypatch) -> None:
    fake = FakeInterface()
    a, _obs, _f, clock = rig(monkeypatch, interface=fake)
    audio(monkeypatch, a, LOUD)
    a.audio_tick()
    assert fake.sets == [("gain.1", 28.0)]
    event = next(e for e in journal() if e["event"] == "change" and e["actuator"] == "interface-gain")
    assert event["reason"] == "The microphone was clipping." and event["kind"] == "interface"
    audio(monkeypatch, a, QUIET)
    clock.now += 60
    a.audio_tick()
    assert fake.sets[-1][1] > 28.0


def test_the_hardware_gain_stays_inside_the_window_around_where_it_started(monkeypatch) -> None:
    fake = FakeInterface()
    a, _obs, _f, clock = rig(monkeypatch, interface=fake)
    audio(monkeypatch, a, LOUD)
    for _ in range(10):
        a.audio_tick()
        clock.now += 60
        clock.now += devices.INSPECT_TTL
    low = 30.0 + auto.MIC_GAIN_DB[0]
    assert min(db for _, db in fake.sets) >= low and fake.report.controls[0].value == low


def test_a_preamp_the_person_turned_is_left_alone_and_the_software_gain_stays_out(monkeypatch) -> None:
    fake = FakeInterface()
    a, _obs, _f, clock = rig(monkeypatch, interface=fake)
    audio(monkeypatch, a, LOUD)
    a.audio_tick()
    fake.report = devices.InterfaceReport(
        True, "S", (devices.GainControl("gain.1", "G", 40.0, 0, 69, 1),)
    )  # turned up by hand
    clock.now += 60 + devices.INSPECT_TTL
    a.audio_tick()
    assert len(fake.sets) == 1
    assert any(e["event"] == "override" and e["actuator"] == "interface-gain" for e in journal())


def test_without_hardware_gain_the_software_gain_is_used_as_before(monkeypatch) -> None:
    fake = FakeInterface(devices.InterfaceReport(True, "Scarlett 2i2 3rd Gen", (), "analogue gain knobs"))
    a, _obs, _f, _c = rig(monkeypatch, interface=fake)
    written: list[float] = []
    a.mic = "mic"
    monkeypatch.setattr(auto, "voice_stats", lambda source, seconds=4.0: dict(LOUD))
    monkeypatch.setattr(auto, "source_gain_db", lambda source: 0.0)
    monkeypatch.setattr(auto, "set_source_gain_db", lambda source, db: written.append(db))
    a.mic_start_db = 0.0
    a.audio_tick()
    assert written == [-2.0] and fake.sets == []


def test_a_failing_preamp_falls_back_to_the_software_gain(monkeypatch) -> None:
    fake = FakeInterface()
    written: list[float] = []
    a, _obs, _f, _c = rig(monkeypatch, interface=fake)
    a.mic, a.mic_start_db = "mic", 0.0
    monkeypatch.setattr(auto, "voice_stats", lambda source, seconds=4.0: dict(LOUD))
    monkeypatch.setattr(auto, "source_gain_db", lambda source: 0.0)
    monkeypatch.setattr(auto, "set_source_gain_db", lambda source, db: written.append(db))

    def refuse(control_id: str, db: float) -> None:
        raise actuators.ActuatorError("the interface did not answer")

    fake.set_gain = refuse
    a.audio_tick()
    assert written == [-2.0]  # the same tick falls back to the software gain
    assert any(e["event"] == "change-failed" for e in journal())


# -- the report, reset and the face finder ------------------------------


def test_the_report_lists_the_hardware_actuators_with_their_reasons(monkeypatch) -> None:
    a, _obs, _f, _c = rig(
        monkeypatch,
        lights=FakeLights(BEAM),
        camera=FakeCanon(None),
        interface=FakeInterface(devices.InterfaceReport(False)),
    )
    a.ai_tick(force=True)
    rows = {r["id"]: r for r in status.read()["actuators"]}
    assert rows["litra"]["available"] and rows["litra"]["kind"] == "light"
    assert not rows["canon-exposure"]["available"] and "bridge" in rows["canon-exposure"]["reason"]
    assert not rows["interface-gain"]["available"] and "interface" in rows["interface-gain"]["reason"]
    assert rows["face-finder"]["kind"] == "sensor" and "skin-colour" in rows["face-finder"]["reason"]


def test_the_detected_face_is_metered_instead_of_the_skin_blob_or_claudes_box(monkeypatch) -> None:
    a, _obs, _f, _c = rig(monkeypatch)

    def with_face(paths):
        frame = dict(
            FRAME, subject={"source": "yunet", "box": [0.3, 0.1, 0.2, 0.3], "headroom": 0.1, "off_centre": 0.0}
        )
        return {
            "frames": [dict(frame) for _ in paths],
            "burst": {"flicker": False, "frames": len(paths)},
            "problems": [],
        }

    monkeypatch.setattr(imagefeatures, "analyze_stills", with_face)
    reply = {**REPLY, "subject": {"box": [0.5, 0.5, 0.2, 0.2], "target_luma": 130}}
    monkeypatch.setattr(auto, "ask_claude", lambda *args: reply)
    a.ai_tick(force=True)
    assert a.subject["box"] == [0.3, 0.1, 0.2, 0.3] and a.subject["target"] == 130


def test_reset_puts_the_camera_the_lights_and_the_gain_back(monkeypatch, capsys) -> None:
    settings.set_persona("executive")
    at(monkeypatch, 0)
    fake, camera, interface = FakeLights(BEAM), FakeCanon(canon_status()), FakeInterface()
    lit(fake, 150, 5600)
    a, _obs, _f, _clock = rig(monkeypatch, lights=fake, camera=camera, interface=interface)
    a.light_tick()
    a.canon_exposure(
        {"gamma": 0.0, "brightness": 0.0}, {"gamma": 0.04, "brightness": 0.0}, {"YAVG": 50.0}, (90, 130), "frame"
    )
    audio(monkeypatch, a, LOUD)
    a.audio_tick()
    baseline = auto.load_baseline()
    assert baseline["light"]["Litra Beam 1"] == {"lumen": 150.0, "kelvin": 5600.0}
    assert baseline["canon"]["Canon EOS R50"]["exposure_comp"] == 0.3 and baseline["gain"]["gain.1"] == 30.0
    fake.writes.clear(), camera.sets.clear(), interface.sets.clear()
    assert auto.restore_hardware(baseline, {}) == 0
    assert fake.writes == [("Litra Beam 1", 150, 5600)]
    assert ("exposure_comp", "+0.3") in camera.sets and interface.sets == [("gain.1", 30.0)]
    assert "restored" in capsys.readouterr().out


def test_reset_reports_a_device_that_is_not_there(monkeypatch, capsys) -> None:
    _a, _obs, _f, _c = rig(monkeypatch, camera=FakeCanon(None))
    baseline = {"canon": {"Canon EOS R50": {"exposure_comp": 0.0}}}
    assert auto.restore_hardware(baseline, {}) == 1
    assert "not restored" in capsys.readouterr().out
    assert system  # imported for the monkeypatched hooks above
