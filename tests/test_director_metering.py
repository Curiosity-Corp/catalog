"""Deterministic auto-exposure and colour match (#94): the controllers against a simulated rig.

The rig is a plain model of the Demo setup: a face whose luma follows the light it gets (the Canon's exposure
compensation in 1/3 stops, the key light's brightness in lumen steps, a room's ambient light), and a grey card whose
red/blue ratio follows the Canon's white balance against the key light's temperature. Nothing here is random and
nothing reads a clock, so two runs must give the same trajectory, byte for byte.
"""

from __future__ import annotations

import math

import pytest

from streamstack.director import metering
from streamstack.director.metering import ColourController, ExposureController, Knob, Reading

TARGET = 130.0


class Rig:
    """A face under a key light and a Canon: the plant the controllers drive."""

    def __init__(self, ambient: float = 0.04, key_lumen: float = 150.0, comp: float = 0.0, wb: float = 5600.0) -> None:
        self.ambient, self.key_lumen, self.comp, self.wb = ambient, key_lumen, comp, wb
        self.light_k = 4300.0  # the key light's own colour temperature

    def light(self) -> float:
        """Relative light on the face: ambient plus the key light, then the Canon's compensation."""
        return (self.ambient + self.key_lumen / 400.0 * 0.25) * 2**self.comp

    def luma(self) -> float:
        return min(255.0, 255.0 * self.light() ** (1 / metering.GAMMA))

    def face(self) -> Reading:
        y = self.luma()
        return Reading(y, y, y, y, 0.0 if y < 250 else 0.1)

    def card(self) -> Reading:
        warm = (self.wb - self.light_k) / metering.KELVIN_PER_STOP  # a white balance above the light reads warm
        red, blue = 128.0 * 2 ** (warm / 2), 128.0 * 2 ** (-warm / 2)
        return Reading(128.0, red, 128.0, blue)

    def knobs(self) -> list[Knob]:
        """Canon first (-1..+1 EV in thirds), then the key light (60..400 lumen, in 10 lumen steps)."""
        lumen_up = math.log2(400.0 / self.key_lumen)
        lumen_down = math.log2(self.key_lumen / 60.0)
        return [
            Knob("canon", up=1.0 - self.comp, down=1.0 + self.comp, step=1 / 3),
            Knob("light", up=lumen_up, down=lumen_down, step=0.0),
        ]

    def apply(self, shares: dict[str, float]) -> None:
        if "canon" in shares:
            self.comp = round((self.comp + shares["canon"]) * 3) / 3
        if "light" in shares:
            self.key_lumen = min(400.0, max(60.0, round(self.key_lumen * 2 ** shares["light"] / 10) * 10))


def run(rig: Rig, controller: ExposureController, passes: int = 30) -> list[tuple[float, float, float]]:
    trace = []
    for _ in range(passes):
        move = controller.plan(rig.face(), rig.knobs())
        rig.apply(move.shares)
        trace.append((round(rig.luma(), 3), rig.comp, rig.key_lumen))
        if move.settled:
            break
    return trace


@pytest.mark.parametrize("ambient", [0.005, 0.02, 0.08, 0.3])
def test_exposure_converges_inside_the_tolerance_from_dark_and_bright_rooms(ambient: float) -> None:
    rig = Rig(ambient=ambient)
    controller = ExposureController(TARGET)
    trace = run(rig, controller)
    assert abs(metering.stops_to_target(rig.luma(), TARGET)) <= 0.2, trace
    assert len(trace) <= 20, trace


def test_two_runs_give_the_same_trajectory() -> None:
    first = run(Rig(ambient=0.01), ExposureController(TARGET))
    second = run(Rig(ambient=0.01), ExposureController(TARGET))
    assert first == second


def test_it_does_not_hunt_once_settled() -> None:
    rig, controller = Rig(ambient=0.02), ExposureController(TARGET)
    run(rig, controller)
    before = (rig.comp, rig.key_lumen)
    for _ in range(20):
        assert controller.plan(rig.face(), rig.knobs()).idle
    assert (rig.comp, rig.key_lumen) == before


def test_the_camera_moves_first_and_the_light_only_takes_the_rest() -> None:
    rig = Rig(ambient=0.0005, key_lumen=60.0, comp=0.0)
    move = ExposureController(TARGET, gain=1.0, max_move=3.0).plan(rig.face(), rig.knobs())
    assert move.shares["canon"] == pytest.approx(1.0)  # the camera's full headroom, in thirds
    assert move.shares.get("light", 0) > 0  # the rest goes to the light


def test_a_knob_at_its_limit_passes_the_remainder_on() -> None:
    knobs = [Knob("canon", up=0.0, down=1.0, step=1 / 3), Knob("light", up=2.0, down=1.0)]
    move = ExposureController(TARGET).plan(Reading(60, 60, 60, 60), knobs)
    assert "canon" not in move.shares and move.shares["light"] > 0


def test_when_nothing_can_help_the_leftover_is_reported() -> None:
    knobs = [Knob("canon", up=0.0, down=0.0, step=1 / 3), Knob("light", up=0.0, down=0.0)]
    move = ExposureController(TARGET).plan(Reading(60, 60, 60, 60), knobs)
    assert move.idle and not move.settled


def test_a_blown_background_never_makes_it_add_light() -> None:
    clipped = Reading(60, 60, 60, 60, clipped=0.2)
    assert ExposureController(TARGET).plan(clipped, [Knob("light", up=2.0, down=2.0)]).idle
    too_bright = Reading(220, 220, 220, 220, clipped=0.2)
    assert ExposureController(TARGET).plan(too_bright, [Knob("light", up=2.0, down=2.0)]).shares["light"] < 0


def test_one_pass_is_capped() -> None:
    move = ExposureController(TARGET, max_move=0.5).plan(Reading(5, 5, 5, 5), [Knob("light", up=9.0, down=1.0)])
    assert move.shares["light"] == pytest.approx(0.5)


def test_it_recovers_from_a_disturbance_and_settles_again() -> None:
    rig, controller = Rig(ambient=0.04), ExposureController(TARGET)
    run(rig, controller)
    rig.ambient = 0.25  # the sun comes out
    controller.reset()
    run(rig, controller)
    assert abs(metering.stops_to_target(rig.luma(), TARGET)) <= 0.2


def test_bad_settings_are_refused() -> None:
    for kwargs in (
        {"target": 0},
        {"target": 130, "gain": 0},
        {"target": 130, "gain": 1.5},
        {"target": 130, "max_move": 0},
    ):
        with pytest.raises(ValueError):
            ExposureController(**kwargs)


def test_meter_box_reads_the_face_and_skips_clipped_pixels() -> None:
    width, height = 10, 10
    frame = bytearray([40, 40, 40] * width * height)
    for x in range(5, 10):  # the right half is a window
        for y in range(height):
            frame[(y * width + x) * 3 : (y * width + x) * 3 + 3] = bytes([255, 255, 255])
    whole = metering.meter_box(bytes(frame), width, height)
    assert whole.luma == pytest.approx(40) and whole.clipped == pytest.approx(0.5)
    face = metering.meter_box(bytes(frame), width, height, (0.0, 0.0, 0.5, 1.0))
    assert face.luma == pytest.approx(40) and face.clipped == 0
    with pytest.raises(ValueError):
        metering.meter_box(b"\x00" * 5, width, height)
    with pytest.raises(ValueError):
        metering.meter_box(bytes(frame), width, height, (0.5, 0.5, 0.0, 0.0))


# -- colour ----------------------------------------------------------------------------------------------------------


def settle_colour(rig: Rig, controller: ColourController, passes: int = 30) -> list[tuple[float, float]]:
    trace = []
    for _ in range(passes):
        move = controller.plan(rig.card(), rig.wb, rig.light_k)
        if move.light_kelvin is not None:
            rig.light_k = move.light_kelvin
        if move.wb_kelvin is not None:
            rig.wb = move.wb_kelvin
        trace.append((rig.wb, rig.light_k))
        if move.settled:
            break
    return trace


@pytest.mark.parametrize("wb", [3200.0, 4300.0, 5600.0, 6500.0])
def test_the_white_balance_matches_the_light_from_any_start(wb: float) -> None:
    rig = Rig(wb=wb)
    trace = settle_colour(rig, ColourController(light_kelvin=5000.0))
    assert rig.light_k == 5000.0  # the light went to the persona's temperature
    assert abs(rig.wb - rig.light_k) <= 150, trace  # and the camera agrees with it
    assert len(trace) <= 15, trace


def test_colour_runs_repeat() -> None:
    assert settle_colour(Rig(wb=3800.0), ColourController(5000.0)) == settle_colour(
        Rig(wb=3800.0), ColourController(5000.0)
    )


def test_a_neutral_card_changes_nothing() -> None:
    rig = Rig(wb=4300.0)  # equals the light: the card is neutral
    move = ColourController(light_kelvin=4300.0).plan(rig.card(), rig.wb, rig.light_k)
    assert move.wb_kelvin is None and move.light_kelvin is None


def test_the_white_balance_stays_within_the_camera_range_and_near_the_light() -> None:
    controller = ColourController(5000.0, max_offset=500.0)
    far_warm = Reading(128, 180, 128, 90)
    move = controller.plan(far_warm, 5000.0, 5000.0)
    assert move.wb_kelvin is not None and 4500.0 <= move.wb_kelvin <= 5000.0
    assert controller.plan(far_warm, 4500.0, 5000.0).wb_kelvin is None  # at the offset limit: it says so, it stays


def test_a_green_card_is_reported_not_chased() -> None:
    move = ColourController(5000.0).plan(Reading(128, 100, 170, 100), 5000.0, 5000.0)
    assert "green" in move.note and move.wb_kelvin is None


def test_a_black_card_is_not_read() -> None:
    move = ColourController(5000.0).plan(Reading(0, 0, 0, 0), 5000.0, 5000.0)
    assert move.wb_kelvin is None and "too dark" in move.note


def test_a_stepped_knob_with_room_keeps_its_sub_step_remainder_and_the_light_stays_put() -> None:
    controller = ExposureController(TARGET)
    knobs = [Knob("canon", up=2.0, down=2.0, step=1 / 3), Knob("light", up=1.0, down=1.0)]
    move = controller.plan(Reading(70.0, 70.0, 70.0, 70.0), knobs)  # wants a full stop; the camera gives whole thirds
    assert move.shares.get("canon") and "light" not in move.shares and move.leftover == 0.0
