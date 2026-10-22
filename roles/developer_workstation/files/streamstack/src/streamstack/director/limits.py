"""Hard limits for everything the director may touch, whoever proposes the change (a persona, a baseline, Claude)."""

from __future__ import annotations

LIMITS = {
    "brightness": (-0.15, 0.2),
    "contrast": (-0.3, 0.4),
    "gamma": (-0.5, 0.6),
    "saturation": (-0.3, 0.35),
    "warmth": (-0.5, 0.5),
    "lut_amount": (0.0, 0.6),
}
# Largest change one Claude pass may make.
AI_STEP = {"brightness": 0.08, "contrast": 0.15, "gamma": 0.2, "saturation": 0.15, "warmth": 0.25, "lut_amount": 0.2}
COLOR_KEYS = ("brightness", "contrast", "gamma", "saturation", "warmth")
# Largest change one local tick (15 s) may make when easing towards a persona or learned look.
START_STEP = {
    "brightness": 0.02,
    "contrast": 0.04,
    "gamma": 0.05,
    "saturation": 0.04,
    "warmth": 0.06,
    "lut_amount": 0.05,
}
# Relative to the gain auto mode found at start; boosting a quiet mic in software also raises its noise,
# so the hardware gain knob comes first.
MIC_GAIN_DB = (-12.0, 6.0)
# Whole-frame exposure band (8-bit luma average) when no face is known and no persona says otherwise.
FRAME_BAND = (100.0, 140.0)
# Face luma targets Claude may choose, and the band kept around them.
FACE_TARGET = (70.0, 180.0)
FACE_BAND = 15.0
# A value the user changed by hand stays theirs for this long (seconds).
OVERRIDE_HOLD = 1800.0
# Smallest difference that counts as "the user moved it".
OVERRIDE_EPS = {
    "brightness": 0.01,
    "contrast": 0.01,
    "gamma": 0.01,
    "saturation": 0.01,
    "warmth": 0.02,
    "lut_amount": 0.02,
}
