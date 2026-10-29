"""Deterministic auto-exposure and colour match (#94): pure controllers, no clocks, no devices, no randomness.

The director already meters the face and nudges the Canon, the Litra lights and the program look one step at a time
(`auto.py`). This module is the part that decides *how far*, in stops and kelvin, from a measurement. The same
readings always give the same moves, so a run repeats and a test can prove that it converges.

Exposure works in stops, not in luma. A pixel value is roughly `255 * light ** (1 / GAMMA)`, so the error in stops
between a measured face luma and its target is `GAMMA * log2(target / measured)`. `ExposureController.plan` turns that
error into a damped move (gain below one, a dead band around the target, a cap per pass) and splits it between the
knobs in the order the director prefers: the camera's exposure compensation first (the sensor, before compression),
then the key light, and only what both cannot give is left for the program look. Each knob reports its remaining
headroom in stops, so a knob at its limit passes the remainder on instead of stalling the loop.

Colour works from a grey card in the frame: its red/blue ratio says how warm the picture reads and its green against
the red and blue mean says the tint. `ColourController.plan` moves the camera's white balance until the card is
neutral, and keeps the key light's colour temperature at the persona's (the cross-property match: the camera's white
balance and the light agree on one temperature, so neither is chasing the other).

The sensitivity constants are a model of a typical body and LED panel. They set how big the first move is; the loop
corrects whatever they get wrong, and `docs/auto-mode.md` lists the hardware check that tunes them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

GAMMA = 2.2  # picture value ~ light ** (1 / GAMMA)
CLIP = 250  # a pixel at or above this value carries no exposure information
MIN_LUMA = 1.0

# -- measuring -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """What one box of one frame said: mean luma (0-255), mean red, green and blue, and the share of clipped pixels."""

    luma: float
    red: float
    green: float
    blue: float
    clipped: float = 0.0


def meter_box(rgb: bytes, width: int, height: int, box: tuple[float, float, float, float] | None = None) -> Reading:
    """Mean colour of a box of packed rgb24 pixels. `box` is (x, y, w, h) as fractions of the frame, None = whole.

    Clipped pixels (any channel at or above CLIP) are left out of the means and counted, so a blown window behind the
    face cannot pull the exposure down. Raises ValueError for a frame of the wrong size or an empty box.
    """
    if width < 1 or height < 1 or len(rgb) != width * height * 3:
        raise ValueError(f"a {width}x{height} rgb24 frame is {width * height * 3} bytes, got {len(rgb)}")
    x, y, w, h = box or (0.0, 0.0, 1.0, 1.0)
    x0, y0 = max(0, int(x * width)), max(0, int(y * height))
    x1, y1 = min(width, int((x + w) * width)), min(height, int((y + h) * height))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("the metering box is empty")
    total = [0, 0, 0]
    kept = clipped = 0
    for row in range(y0, y1):
        base = row * width * 3
        for column in range(x0, x1):
            r, g, b = rgb[base + column * 3 : base + column * 3 + 3]
            if max(r, g, b) >= CLIP:
                clipped += 1
                continue
            total[0] += r
            total[1] += g
            total[2] += b
            kept += 1
    seen = kept + clipped
    if not kept:
        return Reading(255.0, 255.0, 255.0, 255.0, 1.0)
    red, green, blue = (v / kept for v in total)
    return Reading(0.2126 * red + 0.7152 * green + 0.0722 * blue, red, green, blue, clipped / seen)


def stops_to_target(luma: float, target: float) -> float:
    """Stops of light to add (positive) or remove to bring a face reading of `luma` to `target`."""
    return GAMMA * math.log2(max(target, MIN_LUMA) / max(luma, MIN_LUMA))


# -- exposure --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Knob:
    """One thing that can add or remove light, and how many stops it can still move each way."""

    name: str
    up: float  # stops it can still add
    down: float  # stops it can still remove (a positive number)
    step: float = 0.0  # its smallest move in stops; 0 = continuous


@dataclass(frozen=True)
class Move:
    """The result of one pass: the error that was seen, and how the correction is shared out."""

    error: float  # stops: the picture needs this much more (positive) or less light
    shares: dict[str, float] = field(default_factory=dict)  # knob name -> stops to move it
    leftover: float = 0.0  # stops no knob could give
    settled: bool = False  # inside the dead band for `settle_passes` readings in a row

    @property
    def idle(self) -> bool:
        return not any(self.shares.values())


class ExposureController:
    """A damped proportional controller on the face's exposure in stops.

    * `tolerance` stops either side of the target is a dead band: nothing moves, so the picture never hunts.
    * `gain` below one means a measurement that lags the move (a Canon needs a moment to settle) cannot overshoot.
    * `max_move` caps one pass so a person walking in front of the card does not jerk the light.
    * A highlight guard: when more than `clip_limit` of the box is clipped, the controller never adds light.
    * The controller keeps one counter (settled passes) and nothing else: no history, no time.
    """

    def __init__(
        self,
        target: float,
        tolerance: float = 0.15,
        gain: float = 0.7,
        max_move: float = 1.0,
        settle_passes: int = 3,
        clip_limit: float = 0.02,
    ) -> None:
        if not 1 <= target <= 254:
            raise ValueError("the target luma must be between 1 and 254")
        if tolerance < 0 or not 0 < gain <= 1 or max_move <= 0 or settle_passes < 1:
            raise ValueError("tolerance >= 0, 0 < gain <= 1, max_move > 0 and settle_passes >= 1 are required")
        self.target, self.tolerance, self.gain = float(target), tolerance, gain
        self.max_move, self.settle_passes, self.clip_limit = max_move, settle_passes, clip_limit
        self._quiet = 0

    def reset(self) -> None:
        self._quiet = 0

    def plan(self, reading: Reading, knobs: Sequence[Knob]) -> Move:
        """The move for this reading. `knobs` are in the order they should be used."""
        error = stops_to_target(reading.luma, self.target)
        if abs(error) <= self.tolerance:
            self._quiet += 1
            return Move(error, {}, 0.0, self._quiet >= self.settle_passes)
        self._quiet = 0
        wanted = max(-self.max_move, min(self.max_move, error * self.gain))
        if wanted > 0 and reading.clipped > self.clip_limit:
            return Move(error, {}, 0.0, False)
        shares: dict[str, float] = {}
        left = wanted
        for knob in knobs:
            if left == 0:
                break
            room = knob.up if left > 0 else knob.down
            move = math.copysign(min(abs(left), max(room, 0.0)), left)
            if knob.step > 0:
                move = math.copysign(math.floor(abs(move) / knob.step + 1e-9) * knob.step, move)
            if move:
                shares[knob.name] = round(move, 6)
                # What a stepped knob leaves out only because the move must be whole steps stays with it: the next pass
                # measures it again. Handing it down would move the light while the camera still has room to give.
                left = 0.0 if knob.step > 0 and room >= abs(left) else left - move
        # A move smaller than every knob's step still has to go somewhere once the error is real: give the first
        # knob with room one step towards the target, so a quantised camera does not stall just outside the band.
        if not shares:
            for knob in knobs:
                room = knob.up if wanted > 0 else knob.down
                if knob.step > 0 and room + 1e-9 >= knob.step and abs(error) > knob.step * self.gain / 2:
                    shares[knob.name] = math.copysign(knob.step, wanted)
                    left -= shares[knob.name]
                    break
        return Move(error, shares, round(left, 6), False)


# -- colour ----------------------------------------------------------------------------------------------------------

KELVIN_PER_STOP = 2200.0  # how far the white balance has to move to change the card's red/blue ratio by one stop
TINT_LIMIT = 0.12  # a card this far off green/magenta is a lighting problem, not a white balance one


@dataclass(frozen=True)
class ColourMove:
    """One pass of the colour match: the card's error, the camera's new white balance and the light's temperature."""

    warm: float  # log2(red / blue) of the card: positive reads warm
    tint: float  # log2(green / mean(red, blue)): positive reads green
    wb_kelvin: float | None  # the camera's white balance to set, None = leave it
    light_kelvin: float | None  # the key light's temperature to set, None = leave it
    settled: bool = False
    note: str = ""


class ColourController:
    """Grey-card white balance plus the camera/light agreement.

    The camera's white balance should equal the key light's temperature (that is what white balance means), and the
    light's temperature should be the persona's. So the controller first moves the light to `light_kelvin` (when it is
    allowed to), and then trims the camera's white balance by the card's red/blue error, never further from the
    light's temperature than `max_offset`: a card that still reads warm with the white balance an entire stop off is
    mixed light, and the controller says so instead of chasing it.
    """

    def __init__(
        self,
        light_kelvin: float,
        wb_range: tuple[float, float] = (3200.0, 6500.0),
        wb_step: float = 100.0,
        tolerance: float = 0.05,
        gain: float = 0.8,
        max_offset: float = 1500.0,
        settle_passes: int = 3,
    ) -> None:
        if wb_range[0] >= wb_range[1] or wb_step <= 0 or not 0 < gain <= 1:
            raise ValueError("a white balance range, a positive step and 0 < gain <= 1 are required")
        self.light_kelvin, self.wb_range, self.wb_step = light_kelvin, wb_range, wb_step
        self.tolerance, self.gain, self.max_offset = tolerance, gain, max_offset
        self.settle_passes = settle_passes
        self._quiet = 0

    def reset(self) -> None:
        self._quiet = 0

    def plan(self, card: Reading, wb_kelvin: float, light_now: float | None = None) -> ColourMove:
        """The next white balance (and light temperature) for a grey card reading."""
        if min(card.red, card.green, card.blue) < MIN_LUMA:
            return ColourMove(0.0, 0.0, None, None, False, "the grey card is too dark to read")
        warm = math.log2(card.red / card.blue)
        tint = math.log2(card.green / ((card.red + card.blue) / 2))
        if light_now is not None and abs(light_now - self.light_kelvin) > self.wb_step:
            # The light follows the persona first; the card is only read once the light has arrived.
            self._quiet = 0
            return ColourMove(warm, tint, None, self.light_kelvin, False, "the key light is moving to the persona's")
        if abs(tint) > TINT_LIMIT:
            note = "the card reads green or magenta: a white balance cannot fix that, check the lights"
        else:
            note = ""
        if abs(warm) <= self.tolerance:
            self._quiet += 1
            return ColourMove(warm, tint, None, None, self._quiet >= self.settle_passes, note)
        self._quiet = 0
        # A card that reads warm means the white balance assumes too blue a light: lower it, and the other way round.
        wanted = wb_kelvin - warm * self.gain * KELVIN_PER_STOP
        anchor = self.light_kelvin
        wanted = max(anchor - self.max_offset, min(anchor + self.max_offset, wanted))
        wanted = max(self.wb_range[0], min(self.wb_range[1], wanted))
        wanted = round(wanted / self.wb_step) * self.wb_step
        if wanted == wb_kelvin:
            return ColourMove(warm, tint, None, None, False, note or "the white balance is at its limit")
        return ColourMove(warm, tint, wanted, None, False, note)
