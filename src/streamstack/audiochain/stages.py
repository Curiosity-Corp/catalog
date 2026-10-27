"""The stages of the StreamStack voice chain and their live parameters.

One flat dict of numbers (the *params*) describes the whole chain. Every parameter belongs to a stage, has a range,
a default and a unit, and every stage has an ``on`` switch. `controls()` turns a params dict into the PipeWire
filter-graph control values (the only place that knows the plugins' port names); a stage that is off gets values
that make its plugin transparent, so a switch never needs a new graph and never drops audio.

Stage order, left to right: high-pass, noise gate, neural denoise, de-esser, EQ, compressor, makeup gain, limiter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# The graph's node names (filter-chain control names are "<node>:<port>").
HP1, HP2, GATE, DENOISE, DEESS = "hp1", "hp2", "gate", "dfn", "deess"
EQ_LOW, EQ_MUD, EQ_PRES, EQ_AIR = "eq_low", "eq_mud", "eq_pres", "eq_air"
COMP, GAIN, LIM, MIX = "comp", "gain", "lim", "mix"

SILENCE_DB = -90.0


@dataclass(frozen=True)
class Stage:
    id: str
    label: str
    blurb: str


@dataclass(frozen=True)
class Param:
    key: str
    stage: str
    label: str
    unit: str
    low: float
    high: float
    default: float
    step: float = 0.5
    hint: str = ""


STAGES: tuple[Stage, ...] = (
    Stage("hp", "High-pass", "Cuts rumble, desk thumps and breath below the voice"),
    Stage("gate", "Noise gate", "Closes between words so room noise does not ride along"),
    Stage("denoise", "Neural denoise", "Takes steady and sudden noise out of the voice"),
    Stage("deess", "De-esser", "Softens harsh S and T sounds"),
    Stage("eq", "EQ", "Bass, body, presence and air"),
    Stage("comp", "Compressor", "Evens out loud and soft passages"),
    Stage("gain", "Makeup gain", "Brings the compressed voice back up"),
    Stage("limiter", "Limiter", "A hard ceiling so nothing clips or distorts"),
)

PARAMS: tuple[Param, ...] = (
    Param("hp_hz", "hp", "Corner", "Hz", 20, 300, 80, 5, "Everything below this frequency is cut"),
    Param("gate_threshold_db", "gate", "Threshold", "dB", -70, -20, -50, 1, "Level below which the gate closes"),
    Param("gate_range_db", "gate", "Range", "dB", 0, 40, 15, 1, "How far a closed gate turns the sound down"),
    Param("gate_attack_ms", "gate", "Attack", "ms", 1, 50, 5, 1),
    Param("gate_release_ms", "gate", "Release", "ms", 20, 500, 250, 5),
    Param(
        "denoise_atten_db",
        "denoise",
        "Strength",
        "dB",
        0,
        100,
        100,
        1,
        "How much noise may be removed; lower sounds more natural",
    ),
    Param(
        "deess_threshold_db",
        "deess",
        "Threshold",
        "dB",
        -60,
        -30,
        -36,
        1,
        "Level of the 5 to 8 kHz band above which S sounds are cut",
    ),
    Param("deess_freq_hz", "deess", "Frequency", "Hz", 3000, 10000, 6500, 100),
    Param("deess_ratio", "deess", "Ratio", ":1", 1, 5, 4, 0.5),
    Param("deess_max_db", "deess", "Max cut", "dB", 0, 10, 6, 0.5),
    Param("bass_db", "eq", "Bass", "dB", -12, 12, 2, 0.5, "Warmth and body, low shelf at 120 Hz"),
    Param("mud_db", "eq", "Mud", "dB", -12, 6, -2, 0.5, "Boxiness around 300 Hz"),
    Param("presence_db", "eq", "Presence", "dB", -12, 12, 2.5, 0.5, "Clarity, how forward you sound (3.5 kHz)"),
    Param("treble_db", "eq", "Air", "dB", -12, 12, 1.5, 0.5, "Sparkle, high shelf at 9 kHz"),
    Param("comp_threshold_db", "comp", "Threshold", "dB", -50, 0, -20, 1),
    Param("comp_ratio", "comp", "Ratio", ":1", 1, 10, 3, 0.5),
    Param("comp_attack_ms", "comp", "Attack", "ms", 1, 100, 10, 1),
    Param("comp_release_ms", "comp", "Release", "ms", 10, 500, 120, 5),
    Param("comp_knee_db", "comp", "Knee", "dB", 0, 8, 6, 0.5),
    Param("makeup_db", "gain", "Makeup", "dB", -12, 24, 4, 0.5),
    Param("limiter_ceiling_db", "limiter", "Ceiling", "dB", -6, 0, -2, 0.25, "The loudest the sample peak may get"),
    Param("limiter_release_ms", "limiter", "Release", "ms", 1, 100, 20, 1),
)
PARAM_BY_KEY = {p.key: p for p in PARAMS}
STAGE_IDS = tuple(s.id for s in STAGES)
SWITCHABLE = tuple(s.id for s in STAGES)  # every stage has an on switch, stored as "<stage>_on"

# The loudness leveller's own gain rides on top of the makeup gain; it is not a preset value.
LEVEL_WINDOW_DB = 12.0
TARGETS_LUFS = (-14.0, -16.0, -23.0)
DEFAULT_TARGET_LUFS = -16.0

TRUE_PEAK_CEILING_DB = -1.0  # EBU R128 / BS.1770: the true peak never goes above this (see docs/audio-chain.md)

DENOISE_ENGINES = ("deepfilternet", "rnnoise")

# The values of a transparent chain, used when everything is off and as the bypass reference.
NEUTRAL_EQ_DB = 0.0


def on_key(stage: str) -> str:
    return f"{stage}_on"


def defaults() -> dict[str, float | bool]:
    """Every parameter at its default with every stage on."""
    out: dict[str, float | bool] = {p.key: p.default for p in PARAMS}
    out.update({on_key(s): True for s in STAGE_IDS})
    return out


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def validate(changes: dict[str, Any]) -> dict[str, float | bool]:
    """Check a partial params dict: unknown keys and wrong types raise ValueError, numbers are range checked."""
    clean: dict[str, float | bool] = {}
    for key, value in changes.items():
        if key in PARAM_BY_KEY:
            p = PARAM_BY_KEY[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a number")
            if not p.low <= value <= p.high:
                raise ValueError(f"{key} must be between {p.low:g} and {p.high:g}")
            clean[key] = round(float(value), 3)
        elif key.endswith("_on") and key.removesuffix("_on") in STAGE_IDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            clean[key] = value
        else:
            raise ValueError(f"unknown chain setting {key!r}")
    return clean


def merged(base: dict[str, float | bool], changes: dict[str, Any]) -> dict[str, float | bool]:
    """`base` with `changes` applied; missing keys fall back to the defaults so a stale preset still loads."""
    out = defaults()
    out.update({k: v for k, v in base.items() if k in out})
    out.update(validate(changes))
    return out


def db_to_lin(db: float) -> float:
    return 10 ** (db / 20)


def _on(params: dict[str, float | bool], stage: str) -> bool:
    return bool(params.get(on_key(stage), True))


def _num(params: dict[str, float | bool], key: str) -> float:
    value = params.get(key, PARAM_BY_KEY[key].default)
    return float(value)


# ZamDynamicEQ starts cutting when a tone at its own threshold is 26 dB louder than the band RMS we call the threshold
# (measured on the isolated rig with tone steps, docs/audio-chain.md), so the user's threshold is shifted up by this.
DEESS_THRESHOLD_OFFSET_DB = 26.0

# Butterworth 4th order as two biquads.
HP_Q = (0.5412, 1.3066)
TRANSPARENT_HP_HZ = 5.0


def controls(
    params: dict[str, float | bool], *, bypass: bool = False, auto_db: float = 0.0, engine: str = "deepfilternet"
) -> dict[str, float]:
    """The filter-graph control values ("node:Port" -> value) for `params`.

    A stage that is off is made transparent: the high-pass drops to 5 Hz, the gate stops closing, DeepFilterNet's
    attenuation limit goes to 0 dB, the de-esser may cut 0 dB, the EQ bands go flat, the compressor ratio goes to 1,
    the limiter ceiling goes to 0 dB. `bypass` is the A/B switch between the whole chain and the raw microphone.
    """
    c: dict[str, float] = {}
    hz = _num(params, "hp_hz") if _on(params, "hp") else TRANSPARENT_HP_HZ
    c[f"{HP1}:Freq"] = c[f"{HP2}:Freq"] = hz

    gate_on = _on(params, "gate")
    c[f"{GATE}:Threshold"] = _num(params, "gate_threshold_db")
    c[f"{GATE}:Max gate close"] = -_num(params, "gate_range_db") if gate_on else 0.0
    c[f"{GATE}:Attack"] = _num(params, "gate_attack_ms")
    c[f"{GATE}:Release"] = _num(params, "gate_release_ms")

    if engine == "deepfilternet":
        c[f"{DENOISE}:Attenuation Limit (dB)"] = _num(params, "denoise_atten_db") if _on(params, "denoise") else 0.0
    elif engine == "rnnoise":
        c[f"{DENOISE}:VAD Threshold (%)"] = 50.0 if _on(params, "denoise") else 0.0

    deess_on = _on(params, "deess")
    c[f"{DEESS}:Threshold"] = min(0.0, _num(params, "deess_threshold_db") + DEESS_THRESHOLD_OFFSET_DB)
    c[f"{DEESS}:Ratio"] = _num(params, "deess_ratio")
    c[f"{DEESS}:Max Boost/Cut"] = _num(params, "deess_max_db") if deess_on else 0.0
    c[f"{DEESS}:Detect Frequency"] = c[f"{DEESS}:Target Frequency"] = _num(params, "deess_freq_hz")

    eq_on = _on(params, "eq")
    for node, key in ((EQ_LOW, "bass_db"), (EQ_MUD, "mud_db"), (EQ_PRES, "presence_db"), (EQ_AIR, "treble_db")):
        c[f"{node}:Gain"] = _num(params, key) if eq_on else NEUTRAL_EQ_DB

    comp_on = _on(params, "comp")
    c[f"{COMP}:Threshold"] = _num(params, "comp_threshold_db")
    c[f"{COMP}:Ratio"] = _num(params, "comp_ratio") if comp_on else 1.0
    c[f"{COMP}:Attack"] = _num(params, "comp_attack_ms")
    c[f"{COMP}:Release"] = _num(params, "comp_release_ms")
    c[f"{COMP}:Knee"] = _num(params, "comp_knee_db")

    gain_db = (_num(params, "makeup_db") + auto_db) if _on(params, "gain") else auto_db
    c[f"{GAIN}:Mult"] = db_to_lin(gain_db)

    ceiling = _num(params, "limiter_ceiling_db") if _on(params, "limiter") else 0.0
    c[f"{LIM}:Output Ceiling"] = ceiling
    c[f"{LIM}:Threshold"] = ceiling
    c[f"{LIM}:Release"] = _num(params, "limiter_release_ms")

    c.update(bypass_controls(bypass))
    return c


def bypass_controls(bypass: bool, mix: float | None = None) -> dict[str, float]:
    """The A/B mixer: `mix` 0 is the processed chain, 1 the raw microphone (bypass is 0 or 1)."""
    m = (1.0 if bypass else 0.0) if mix is None else clamp(mix, 0.0, 1.0)
    return {f"{MIX}:Gain 1": 1.0 - m, f"{MIX}:Gain 2": m}
