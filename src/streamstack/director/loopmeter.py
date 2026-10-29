"""The glue between the deterministic controllers (`metering`) and the live director loop (`auto.py`).

`metering` decides how far to move from a measurement; this module turns what the real devices report into the
controllers' inputs: the knobs (how many stops each device can still give), the grey card reading from a still, and the
grey card setting. Nothing here opens a device or keeps state, so each function is tested on its own.
"""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from streamstack.director import metering
from streamstack.director.metering import Knob, Reading

CARD_PIXELS = (48, 36)  # the grey card is scaled to this many pixels before it is metered: plenty for a mean
HIGHLIGHT_TOP = 245.0  # a 90th-percentile luma this high means a tenth of the box or more is blown (as exposure_target)
BLOWN_SHARE = 0.1


def parse_card_box(text: str) -> tuple[tuple[float, float, float, float] | None, str]:
    """(box, problem) from "x,y,w,h" as fractions of the frame; an empty setting is (None, "") and means "no card"."""
    if not text.strip():
        return None, ""
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        return None, f"STREAMSTACK_AUTO_GREYCARD must be four numbers x,y,w,h, got {text!r}"
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        return None, f"STREAMSTACK_AUTO_GREYCARD must be four numbers x,y,w,h, got {text!r}"
    x, y, w, h = values
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.0001 or y + h > 1.0001:
        return None, "STREAMSTACK_AUTO_GREYCARD must lie inside the frame (fractions from 0 to 1, width and height > 0)"
    return (x, y, w, h), ""


def exposure_reading(stats: Mapping[str, float]) -> metering.Reading | None:
    """The face or frame reading the exposure controller works from, in the same luma the persona's band speaks.

    The share of clipped pixels is estimated from the 90th-percentile luma: when that is blown, at least a tenth of the
    box is, which is far past the controller's highlight guard.
    """
    luma = stats.get("YAVG")
    if luma is None:
        return None
    top = stats.get("YHIGH", luma)
    return Reading(luma, luma, luma, luma, BLOWN_SHARE if top >= HIGHLIGHT_TOP else 0.0)


def canon_knob(current: Mapping[str, float], low: float, high: float, step: float, held: bool) -> Knob | None:
    """The camera's exposure compensation as a knob in stops, or None when the person holds it or it has no reading."""
    value = current.get("exposure_comp")
    if held or not isinstance(value, (int, float)):
        return None

    # The body moves in thirds of a stop and its last notch can be a little short of one step: round the room up to
    # whole steps so a camera that is 0.3 stop from its limit still takes that last notch (the actuator clamps it).
    def notches(room: float) -> float:
        return math.ceil(max(room, 0.0) / step - 1e-9) * step

    return Knob("canon", notches(high - value), notches(value - low), step)


def light_knob(readings: Mapping[str, float], bounds: Mapping[str, tuple[float, float]]) -> Knob | None:
    """The key lights' brightness as one knob in stops (light output is linear in lumen), None without a usable light.

    The group moves together (the balance between a key and a dimmer fill is the person's), so the room left is the
    smallest of any light's.
    """
    up = down = math.inf
    for label, lumen in readings.items():
        low, high = bounds.get(label, (0.0, 0.0))
        if lumen <= 0 or high <= 0 or low <= 0:
            return None
        up = min(up, max(math.log2(high / lumen), 0.0))
        down = min(down, max(math.log2(lumen / low), 0.0))
    if up is math.inf:
        return None
    return Knob("light", up, down)


def lumen_after(lumen: float, stops: float) -> float:
    """A light's brightness after moving it by `stops` of light."""
    return lumen * 2.0**stops


def read_card(
    image: Path,
    box: tuple[float, float, float, float],
    run: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> Reading | None:
    """Mean colour of the grey card in a still (ffmpeg crop and scale to rgb24); None when it cannot be read."""
    x, y, w, h = box
    width, height = CARD_PIXELS
    argv = [
        "ffmpeg", "-hide_banner", "-v", "error", "-i", str(image),
        "-vf", f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},scale={width}:{height}:flags=area,format=rgb24",
        "-frames:v", "1", "-f", "rawvideo", "-",
    ]  # fmt: skip
    try:
        result = (run or subprocess.run)(argv, capture_output=True, timeout=20, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout) != width * height * 3:
        return None
    return metering.meter_box(result.stdout, width, height)
