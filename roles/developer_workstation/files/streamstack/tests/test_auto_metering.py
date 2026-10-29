"""The metering controllers inside the live director loop (#150), on a simulated rig of fakes (no device is opened).

The rig is `FakeCanon` and `FakeLights` plus a scene model: a face whose luma follows the light it gets (the Canon's
exposure compensation and the key light's brightness against a room's ambient light), and a grey card whose red/blue
ratio follows the Canon's white balance against the key light's temperature. Every tick advances the fake clock, so the
governor's rate limits are the real ones.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest
from test_auto import journal
from test_auto_hardware import BEAM, at, lit, rig
from test_director_devices import FakeCanon, FakeLights, canon_status

from streamstack import auto
from streamstack.director import devices, loopmeter, metering, settings
from streamstack.director.metering import Reading

KEY = 0.1  # the light on the face from the key at full power, against ambient 1.0 = a face at luma 255
TICK = 30.0  # seconds between video ticks in the sim: the Canon's own minimum interval, so no pass is wasted


class Scene:
    """The plant: luma from ambient light, the key light (lumen) and the Canon's compensation, in stops."""

    def __init__(self, camera: FakeCanon, lights: FakeLights | None, ambient: float = 0.01) -> None:
        self.camera, self.lights, self.ambient = camera, lights, ambient

    @property
    def comp(self) -> float:
        return float(self.camera.current["controls"]["exposure_comp"]["value"])

    @property
    def lumen(self) -> float:
        return float(self.lights.state[BEAM.label].lumen) if self.lights else 0.0

    def luma(self) -> float:
        light = (self.ambient + self.lumen / 400.0 * KEY) * 2**self.comp
        return min(255.0, 255.0 * light ** (1 / metering.GAMMA))

    def fit(self, luma: float) -> None:
        """Choose the room's ambient light so the picture reads `luma` as things stand."""
        self.ambient = (luma / 255.0) ** metering.GAMMA / 2**self.comp - self.lumen / 400.0 * KEY

    def install(self, monkeypatch) -> None:
        def stats(frame, box=None):
            y = self.luma()
            return {"YAVG": y, "YHIGH": min(255.0, y * 1.3), "YMIN": 16, "YMAX": 235}

        monkeypatch.setattr(auto, "picture_stats", stats)


def sim(monkeypatch, ambient: float = 0.01, lights: bool = True, **cfg):
    cfg.setdefault("STREAMSTACK_AUTO_METERING", "true")  # opt-in in production, on for the loop tests
    settings.set_persona("late-night-calm")
    at(monkeypatch, 12)
    camera = FakeCanon(canon_status())
    fake_lights = FakeLights(BEAM) if lights else None
    if fake_lights:
        lit(fake_lights, 150, 5000)
    a, link, _f, clock = rig(
        monkeypatch, lights=fake_lights, camera=camera, STREAMSTACK_AUTO_PERSONA_LOOK="false", **cfg
    )
    scene = Scene(camera, fake_lights, ambient)
    scene.install(monkeypatch)
    return a, link, scene, clock


def run(a, clock, scene, ticks: int = 40, light_ticks: bool = False) -> list[tuple[float, float, float]]:
    trace = []
    for _ in range(ticks):
        a.video_tick()
        if light_ticks:
            a.light_tick()
        clock.now += TICK
        trace.append((scene.luma(), scene.comp, scene.lumen))
    return trace


# -- exposure -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ambient", [0.0005, 0.005, 0.01])
def test_a_dark_room_converges_into_the_band_without_overshoot(monkeypatch, ambient) -> None:
    a, link, scene, clock = sim(monkeypatch, ambient)
    low, high = a.pilot.frame_band()
    trace = run(a, clock, scene)
    assert low <= trace[-1][0] <= high
    assert all(luma <= high for luma, _c, _l in trace)  # never overshoots past the top of the band
    lumas = [t[0] for t in trace]
    assert lumas == sorted(lumas)  # one way, no hunting
    assert link.writes == []  # the program look was never needed


def test_a_bright_room_comes_down_the_same_way(monkeypatch) -> None:
    a, _link, scene, clock = sim(monkeypatch, ambient=0.3)
    low, high = a.pilot.frame_band()
    trace = run(a, clock, scene)
    lumas = [t[0] for t in trace]
    assert low <= lumas[-1] <= high and lumas == sorted(lumas, reverse=True)


def test_steps_are_bounded_and_the_camera_goes_before_the_light(monkeypatch) -> None:
    a, _link, scene, clock = sim(monkeypatch, ambient=0.0005)
    scene.camera.current["controls"]["exposure_comp"]["value"] = "-1"  # the camera has two stops to give
    span = 400 - 30
    comp, lumen = scene.comp, scene.lumen
    camera_at_first_light_write = None
    for _ in range(40):
        a.video_tick()
        clock.now += TICK
        assert abs(scene.comp - comp) <= 0.4 + 1e-6  # one notch of the body (+0.3 to +0.7)
        assert abs(scene.lumen - lumen) <= 0.12 * span + 1
        if camera_at_first_light_write is None and scene.lumen != lumen:
            camera_at_first_light_write = scene.comp
        comp, lumen = scene.comp, scene.lumen
    # The light only takes what the camera cannot give: it first moves once the camera is within a notch of its limit.
    assert camera_at_first_light_write is not None and camera_at_first_light_write >= 0.7
    assert scene.lumen > 150  # and it did take it
    assert a.metered == {BEAM.label}


def test_a_settled_loop_does_not_move_again(monkeypatch) -> None:
    a, _link, scene, clock = sim(monkeypatch)
    run(a, clock, scene)
    sets, writes = len(scene.camera.sets), len(scene.lights.writes)
    run(a, clock, scene, ticks=10)
    assert (len(scene.camera.sets), len(scene.lights.writes)) == (sets, writes)


def test_the_persona_light_easing_does_not_pull_a_metered_light_back(monkeypatch) -> None:
    settings.set_persona("executive")
    a, _link, scene, clock = sim(monkeypatch, ambient=0.0005)
    trace = run(a, clock, scene, ticks=60, light_ticks=True)
    tail = [t[2] for t in trace[-10:]]
    assert max(tail) - min(tail) <= 1.0  # the light is not dragged to and fro by the two loops
    assert trace[-1][2] > 150


def test_without_a_light_the_camera_does_it_then_the_program_look(monkeypatch) -> None:
    a, link, scene, clock = sim(monkeypatch, ambient=0.0005, lights=False)
    run(a, clock, scene, ticks=12)
    assert scene.comp == pytest.approx(1.0)
    assert link.writes  # the camera is at its limit: the program look finishes the job


def test_a_light_the_person_set_is_left_alone(monkeypatch) -> None:
    a, _link, scene, clock = sim(monkeypatch, ambient=0.0005)
    run(a, clock, scene, ticks=4)
    scene.lights.state[BEAM.label] = devices.LightState(True, 90, 5000)  # turned down by hand
    writes = len(scene.lights.writes)
    run(a, clock, scene, ticks=20)
    assert len(scene.lights.writes) == writes
    assert any(e["event"] == "override" and e["actuator"] == "litra" for e in journal())


def test_a_dry_run_changes_nothing(monkeypatch) -> None:
    a, link, scene, clock = sim(monkeypatch, ambient=0.0005)
    a.dry_run = True
    run(a, clock, scene, ticks=6)
    assert scene.camera.sets == [] and scene.lights.writes == [] and link.writes == []
    assert any(e["event"] == "change" and e["dry_run"] for e in journal())


# -- the opt-out --------------------------------------------------------------------------------------------------


def test_a_disabled_controller_keeps_the_one_notch_step_and_never_touches_the_light(monkeypatch) -> None:
    a, link, scene, clock = sim(monkeypatch, ambient=0.0005, STREAMSTACK_AUTO_METERING="false")
    run(a, clock, scene, ticks=12)
    assert scene.camera.sets[0] == ("exposure_comp", "+0.7")  # +0.3 and one notch, as before #150
    assert scene.lights.writes == [] and a.metered == set()
    assert not [e for e in journal() if e["event"] == "metering"]
    assert link.writes  # camera at its limit, so the look finished the job, exactly as before


def test_a_disabled_controller_gives_the_same_moves_as_the_old_loop(monkeypatch) -> None:
    on, _l, scene_on, clock_on = sim(monkeypatch, ambient=0.0005, lights=False, STREAMSTACK_AUTO_METERING="false")
    run(on, clock_on, scene_on, ticks=1)
    assert scene_on.camera.sets == [("exposure_comp", "+0.7")]
    assert not on.colour_active() and on.colour is None


# -- the grey card ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "box"),
    [("", None), ("0.8,0.7,0.1,0.15", (0.8, 0.7, 0.1, 0.15)), (" 0, 0 ,1,1 ", (0.0, 0.0, 1.0, 1.0))],
)
def test_the_grey_card_setting_parses(text, box) -> None:
    assert loopmeter.parse_card_box(text) == (box, "")


@pytest.mark.parametrize(
    "text", ["a,b,c,d", "0.1,0.1,0.1", "0.9,0.9,0.3,0.3", "0.1,0.1,0,0.2", "nan,0,1,1", "-0.1,0,0.5,0.5"]
)
def test_a_bad_grey_card_setting_is_refused_with_a_reason(text) -> None:
    box, problem = loopmeter.parse_card_box(text)
    assert box is None and "STREAMSTACK_AUTO_GREYCARD" in problem


def test_a_bad_grey_card_setting_leaves_the_colour_loop_off(monkeypatch) -> None:
    a, _link, _scene, _clock = sim(monkeypatch, STREAMSTACK_AUTO_GREYCARD="nonsense")
    assert a.card is None and not a.colour_active()
    assert any(e["event"] == "greycard-ignored" for e in journal())


def card_rig(monkeypatch, wb: int, light_k: float = 5000.0, lights: bool = False, **cfg):
    a, _link, scene, clock = sim(
        monkeypatch,
        ambient=0.06,
        lights=lights,
        STREAMSTACK_AUTO_GREYCARD="0.8,0.7,0.1,0.15",
        **cfg,
    )
    scene.camera.current["controls"]["kelvin"]["value"] = str(wb)
    low, high = a.pilot.frame_band()
    scene.fit((low + high) / 2)  # a picture already inside the band: the colour loop waits for the exposure
    state = {"light_k": light_k}

    def read(image, box, run=None):
        wb_now = float(scene.camera.current["controls"]["kelvin"]["value"])
        warm = (wb_now - state["light_k"]) / metering.KELVIN_PER_STOP
        return Reading(128.0, 128.0 * 2 ** (warm / 2), 128.0, 128.0 * 2 ** (-warm / 2))

    monkeypatch.setattr(loopmeter, "read_card", read)
    return a, scene, clock, state


@pytest.mark.parametrize("wb", [3600, 5200, 6400])
def test_the_white_balance_settles_on_the_grey_card_from_any_start(monkeypatch, wb) -> None:
    a, scene, clock, state = card_rig(monkeypatch, wb)
    kelvins = []
    for _ in range(30):
        a.video_tick()
        clock.now += 90.0  # the Canon's white balance may move once a minute
        kelvins.append(float(scene.camera.current["controls"]["kelvin"]["value"]))
    assert kelvins[-1] == pytest.approx(state["light_k"], abs=150.0)
    assert all(abs(b - a_) <= 400 + 1 for a_, b in zip([wb, *kelvins], kelvins, strict=False))  # one bounded step
    assert all(3200 <= k <= 6500 for k in kelvins)
    settled = len(scene.camera.sets)
    for _ in range(5):
        a.video_tick()
        clock.now += 90.0
    assert len(scene.camera.sets) == settled  # and it stays there


def test_the_grey_card_takes_the_white_balance_from_the_ai_pass(monkeypatch) -> None:
    settings.set_persona("executive")
    a, scene, _clock, _state = card_rig(monkeypatch, 5200, STREAMSTACK_AUTO_BACKEND="local")
    a.ai_tick(force=True)  # without a card this would step the Kelvin towards the persona's 4800 K
    assert scene.camera.sets == []


def test_without_a_grey_card_the_white_balance_is_the_persona_loop_as_before(monkeypatch) -> None:
    settings.set_persona("executive")
    a, _link, scene, _clock = sim(monkeypatch, STREAMSTACK_AUTO_BACKEND="local")
    a.ai_tick(force=True)
    assert scene.camera.sets and scene.camera.sets[0][0] == "kelvin"


def test_the_white_balance_waits_for_the_key_light_to_arrive(monkeypatch) -> None:
    settings.set_persona("executive")
    a, scene, _clock, _state = card_rig(monkeypatch, 5200, light_k=5000.0, lights=True)
    scene.lights.state[BEAM.label] = devices.LightState(True, 150, 6200)  # the persona wants ~4800: it is far off
    a.video_tick()
    assert scene.camera.sets == []  # the card is not read until the light has arrived
    assert any(e["event"] == "metering" and e.get("what") == "colour" and e["wb_kelvin"] is None for e in journal())


def test_an_unreadable_card_moves_nothing_and_is_said_once(monkeypatch) -> None:
    a, scene, clock, _state = card_rig(monkeypatch, 5200)
    monkeypatch.setattr(loopmeter, "read_card", lambda image, box, run=None: None)
    for _ in range(3):
        a.video_tick()
        clock.now += 90.0
    assert scene.camera.sets == []
    assert len([e for e in journal() if e["event"] == "metering-note"]) == 1


# -- the helpers --------------------------------------------------------------------------------------------------


def test_the_exposure_reading_flags_a_blown_picture() -> None:
    assert loopmeter.exposure_reading({}) is None
    assert loopmeter.exposure_reading({"YAVG": 100.0, "YHIGH": 200.0}).clipped == 0.0
    assert loopmeter.exposure_reading({"YAVG": 100.0, "YHIGH": 250.0}).clipped > 0.02


def test_a_blown_picture_never_gets_more_light(monkeypatch) -> None:
    a, _link, scene, clock = sim(monkeypatch, ambient=0.0005)
    monkeypatch.setattr(auto, "picture_stats", lambda frame, box=None: {"YAVG": 60.0, "YHIGH": 252.0})
    run(a, clock, scene, ticks=5)
    assert scene.camera.sets == [] and scene.lights.writes == []


def test_the_light_knob_is_the_smallest_room_of_the_group() -> None:
    knob = loopmeter.light_knob({"a": 100.0, "b": 200.0}, {"a": (50.0, 400.0), "b": (50.0, 400.0)})
    assert knob is not None and knob.up == pytest.approx(1.0) and knob.down == pytest.approx(1.0)
    assert loopmeter.light_knob({}, {}) is None
    assert loopmeter.light_knob({"a": 0.0}, {"a": (50.0, 400.0)}) is None


def test_the_canon_knob_rounds_the_last_notch_up_and_is_none_when_held() -> None:
    knob = loopmeter.canon_knob({"exposure_comp": 0.7}, -1.0, 1.0, 0.34, held=False)
    assert knob is not None and knob.up == pytest.approx(0.34) and knob.down == pytest.approx(1.7, abs=0.02)
    assert loopmeter.canon_knob({"exposure_comp": 0.7}, -1.0, 1.0, 0.34, held=True) is None
    assert loopmeter.canon_knob({}, -1.0, 1.0, 0.34, held=False) is None
    at_limit = loopmeter.canon_knob({"exposure_comp": 1.0}, -1.0, 1.0, 0.34, held=False)
    assert at_limit is not None and at_limit.up == 0.0


def test_read_card_decodes_the_crop_and_survives_a_failed_ffmpeg() -> None:
    w, h = loopmeter.CARD_PIXELS
    seen = []

    def ok(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, bytes([200, 100, 50]) * (w * h), b"")

    card = loopmeter.read_card(Path("still.jpg"), (0.8, 0.7, 0.1, 0.15), ok)
    assert card is not None and (card.red, card.green, card.blue) == (200.0, 100.0, 50.0)
    assert "crop=iw*0.1000:ih*0.1500:iw*0.8000:ih*0.7000" in " ".join(seen[0])
    bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"error")  # noqa: E731
    assert loopmeter.read_card(Path("still.jpg"), (0.0, 0.0, 1.0, 1.0), bad) is None

    def gone(argv, **kw):
        raise FileNotFoundError("ffmpeg")

    assert loopmeter.read_card(Path("still.jpg"), (0.0, 0.0, 1.0, 1.0), gone) is None
    assert math.isfinite(card.luma)


def test_metering_is_opt_in_and_the_default_is_the_old_behaviour(monkeypatch) -> None:
    """Untuned closed-loop exposure must not run on a live call: unset means the one-notch steps (#94, #150)."""
    from streamstack import config

    default = config.DEFAULTS["STREAMSTACK_AUTO_METERING"]
    assert default == "false"
    a, _link, scene, clock = sim(monkeypatch, ambient=0.0005, STREAMSTACK_AUTO_METERING=default)
    run(a, clock, scene, ticks=12)
    assert a.metering_on is False
    assert scene.camera.sets[0] == ("exposure_comp", "+0.7")
    assert scene.lights.writes == [] and a.metered == set()
