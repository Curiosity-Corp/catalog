"""Echo and speaker-bleed check: play a low-level sweep on one output, listen on the raw microphone, measure the bleed.

Pure DSP on lists of floats (no NumPy: the package has no runtime dependencies), so it is testable with a simulated
loopback of known coupling:

    sweep = chirp(RATE)                                  # what is played (a log sweep, 250 Hz to 2.5 kHz)
    result = analyze(recorded, rate, lead_s=0.3)         # what the microphone heard, from a WAV the caller recorded

The recording is a mono list at the recorder's own rate. `analyze` brings it down to about 8 kHz, cross-correlates it
with the sweep through an FFT and reads three things from the best match:

* the coupling gain in dB: how far below the played signal the microphone heard it (the least-squares gain of the sweep
  inside the recording). -34 dB is a loud bleed; below about -55 dB nobody on the call hears it;
* the delay in ms from the start of the recording to the sweep, less `lead_s` (the silence the caller put before the
  sweep). It includes the player's start-up, so read it to a few ms, not as a speaker distance;
* a feedback loop: a second copy of the sweep one loop later, as a monitor mix (Wave Link style loopback) or an effects
  chain monitoring itself produces. A room's reverb is not a loop: it is smeared over tens of ms, the copy of a loop is
  a clean second peak at least `LOOP_MIN_GAP_MS` later.

Whether a sweep was heard at all is a statistical test against the correlation's own noise (`DETECT_SIGMA`), so a quiet
room is not mistaken for bleed and a recording of silence (a muted microphone) says so instead of "no bleed".
"""

from __future__ import annotations

import cmath
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

RATE = 8000  # the analysis rate; the played file may use any rate, it is the same function of time
F_START, F_END = 250.0, 2500.0
SWEEP_S = 1.0
PLAY_LEVEL_DB = -24.0  # of full scale: audible, never loud
DETECT_SIGMA = 7.0  # a match this many times the correlation's noise counts as heard
LOOP_MIN_GAP_MS = 80.0
LOOP_MIN_RATIO = 0.05  # of the main peak
EXCLUDE_MS = 60.0  # around a peak, left out of the noise estimate
# Bleed bands, in dB of coupling: at or above `strong` the other side hears an echo; below `slight` it is inaudible.
BAND_STRONG_DB = -40.0
BAND_MODERATE_DB = -50.0
BAND_SLIGHT_DB = -60.0
MIN_SAMPLES_S = SWEEP_S + 0.2


@dataclass(frozen=True)
class EchoResult:
    """What the microphone heard of the sweep."""

    heard: bool
    coupling_db: float | None  # None when the sweep was not heard
    limit_db: float  # the weakest coupling this recording could have shown (its noise floor)
    delay_ms: float | None
    coherence: float  # 0..1: how much of the recording's energy at the best lag is the sweep
    loop: bool
    loop_gap_ms: float | None
    level: str  # none | slight | moderate | strong | feedback | unmeasured

    def public(self) -> dict[str, object]:
        return {
            "heard": self.heard,
            "coupling_db": None if self.coupling_db is None else round(self.coupling_db, 1),
            "limit_db": round(self.limit_db, 1),
            "delay_ms": None if self.delay_ms is None else round(self.delay_ms, 1),
            "coherence": round(self.coherence, 3),
            "loop": self.loop,
            "loop_gap_ms": None if self.loop_gap_ms is None else round(self.loop_gap_ms, 1),
            "level": self.level,
        }


def chirp(rate: int = RATE, seconds: float = SWEEP_S, level_db: float = 0.0) -> list[float]:
    """A logarithmic sweep from F_START to F_END with a short fade in and out, peak `level_db` of full scale."""
    n = int(rate * seconds)
    k = math.log(F_END / F_START)
    amp = 10 ** (level_db / 20)
    fade = max(1, int(0.02 * rate))
    out = []
    for i in range(n):
        t = i / rate
        phase = 2 * math.pi * F_START * seconds / k * (math.exp(t / seconds * k) - 1)
        edge = min(1.0, i / fade, (n - 1 - i) / fade)
        out.append(amp * edge * math.sin(phase))
    return out


def fft(values: Sequence[complex], inverse: bool = False) -> list[complex]:
    """Iterative radix-2 FFT (the length must be a power of two); the inverse is scaled by 1/n."""
    n = len(values)
    if n & (n - 1) or n == 0:
        raise ValueError("fft length must be a power of two")
    bits = n.bit_length() - 1
    out = [values[int(format(i, f"0{bits}b")[::-1], 2)] if bits else values[i] for i in range(n)]
    sign = 1 if inverse else -1
    size = 2
    while size <= n:
        step = cmath.exp(sign * 2j * math.pi / size)
        half = size // 2
        twiddles = [1 + 0j] * half
        for j in range(1, half):
            twiddles[j] = twiddles[j - 1] * step
        for start in range(0, n, size):
            for j in range(half):
                a = out[start + j]
                b = out[start + j + half] * twiddles[j]
                out[start + j] = a + b
                out[start + j + half] = a - b
        size *= 2
    if inverse:
        out = [v / n for v in out]
    return out


def correlate(recorded: Sequence[float], reference: Sequence[float]) -> list[float]:
    """c[k] = sum over n of recorded[n + k] * reference[n], for every lag k from 0 to len(recorded) - len(reference)."""
    lags = len(recorded) - len(reference) + 1
    if lags < 1:
        raise ValueError("the recording is shorter than the sweep")
    size = 1
    while size < len(recorded) + len(reference):
        size *= 2
    a = fft([complex(v) for v in recorded] + [0j] * (size - len(recorded)))
    b = fft([complex(v) for v in reference] + [0j] * (size - len(reference)))
    product = [x * y.conjugate() for x, y in zip(a, b, strict=True)]
    return [v.real for v in fft(product, inverse=True)[:lags]]


def decimate(samples: Sequence[float], source_rate: int, target: int = RATE) -> tuple[list[float], float]:
    """Mean of each block of `factor` samples (a crude low-pass and a decimation) and the rate that leaves."""
    factor = max(1, round(source_rate / target))
    usable = len(samples) // factor * factor
    mean = sum(samples[:usable]) / usable if usable else 0.0
    out = [sum(samples[i : i + factor]) / factor - mean for i in range(0, usable, factor)]
    return out, source_rate / factor


def analyze(recorded: Sequence[float], rate: int, *, lead_s: float = 0.0, play_db: float = 0.0) -> EchoResult:
    """Measure the bleed of the sweep (`chirp()`) in a mono recording at `rate`; `lead_s` is the silence before it and
    `play_db` the level (dB of full scale) the sweep was played at, so the coupling is relative to what was played."""
    signal, eff = decimate(recorded, rate)
    if len(signal) < MIN_SAMPLES_S * eff:
        raise ValueError("the recording is too short to hold the sweep")
    ref = chirp(round(eff))
    energy = sum(v * v for v in ref)
    corr = correlate(signal, ref)
    peak = max(range(len(corr)), key=lambda k: abs(corr[k]))
    sigma = _noise(corr, [peak], eff)
    limit_db = 20 * math.log10(max(DETECT_SIGMA * sigma / energy, 1e-12)) - play_db
    window = signal[peak : peak + len(ref)]
    window_energy = sum(v * v for v in window) or 1e-30
    coherence = min(1.0, abs(corr[peak]) / math.sqrt(energy * window_energy))
    heard = sigma > 0 and abs(corr[peak]) >= DETECT_SIGMA * sigma
    if not heard:
        return EchoResult(False, None, limit_db, None, coherence, False, None, "none")
    gain = corr[peak] / energy
    coupling = 20 * math.log10(abs(gain)) - play_db
    loop_k = _second_peak(corr, peak, eff, sigma)
    gap = None if loop_k is None else abs(loop_k - peak) / eff * 1000
    return EchoResult(
        True,
        coupling,
        limit_db,
        peak / eff * 1000 - lead_s * 1000,
        coherence,
        loop_k is not None,
        gap,
        "feedback" if loop_k is not None else level_of(coupling),
    )


def level_of(coupling_db: float | None) -> str:
    if coupling_db is None or coupling_db < BAND_SLIGHT_DB:
        return "none"
    if coupling_db < BAND_MODERATE_DB:
        return "slight"
    if coupling_db < BAND_STRONG_DB:
        return "moderate"
    return "strong"


def _noise(corr: Sequence[float], peaks: Sequence[int], eff: float) -> float:
    """RMS of the correlation away from the given peaks: what a match has to stand out from."""
    keep = max(1, int(EXCLUDE_MS / 1000 * eff))
    total, count = 0.0, 0
    for k, v in enumerate(corr):
        if any(abs(k - p) <= keep for p in peaks):
            continue
        total += v * v
        count += 1
    return math.sqrt(total / count) if count else 0.0


def _second_peak(corr: Sequence[float], peak: int, eff: float, sigma: float) -> int | None:
    """The lag of a clean second copy of the sweep at least LOOP_MIN_GAP_MS from the main one, or None."""
    gap = int(LOOP_MIN_GAP_MS / 1000 * eff)
    best, best_k = 0.0, None
    for k, v in enumerate(corr):
        if abs(k - peak) < gap:
            continue
        if abs(v) > best:
            best, best_k = abs(v), k
    if best_k is None:
        return None
    if best >= LOOP_MIN_RATIO * abs(corr[peak]) and best >= DETECT_SIGMA * _noise(corr, [peak, best_k], eff):
        return best_k
    return None


# -- a simulated loopback, for the demo rig and the tests -------------------------------------------------------------


def simulate(
    *,
    coupling_db: float | None,
    delay_ms: float = 30.0,
    lead_s: float = 0.3,
    tail_s: float = 0.7,
    noise_db: float = -70.0,
    loop_gap_ms: float | None = None,
    rate: int = RATE,
    seed: int = 1,
) -> list[float]:
    """What a microphone records while the sweep (at 0 dB, in the signal's own scale) plays through a path of known
    coupling and delay: silence, the sweep delayed and scaled, optionally a feedback copy `loop_gap_ms` later and scaled
    by the same coupling again, and a noise floor. `coupling_db=None` records noise only."""
    rng = random.Random(seed)  # noqa: S311 - a deterministic simulation, not security
    ref = chirp(rate)
    total = int(rate * (lead_s + SWEEP_S + tail_s))
    out = [0.0] * total
    if coupling_db is not None:
        gain = 10 ** (coupling_db / 20)
        copies = [(delay_ms, gain)]
        if loop_gap_ms is not None:
            copies.append((delay_ms + loop_gap_ms, gain * gain))
        for ms, g in copies:
            start = int((lead_s + ms / 1000) * rate)
            for i, v in enumerate(ref):
                if start + i < total:
                    out[start + i] += g * v
    sigma = 10 ** (noise_db / 20)
    return [v + rng.gauss(0.0, sigma) for v in out]


def describe(result: EchoResult, sink_label: str) -> str:
    """The sentence the Console shows."""
    if result.level == "feedback":
        gap = f" about {result.loop_gap_ms:.0f} ms" if result.loop_gap_ms is not None else ""
        return (
            f"A feedback loop through {sink_label}: the sound comes back{gap} later. "
            "Turn off the monitor mix or the effects monitoring that feeds this output into the microphone."
        )
    if not result.heard or result.level == "none":
        return f"No bleed from {sink_label}: the microphone did not hear the sound (below {result.limit_db:.0f} dB)."
    db = f"{result.coupling_db:.0f} dB"
    if result.level == "slight":
        return f"Slight bleed from {sink_label} at {db}: not audible to the other side."
    return f"Bleed at {db} from {sink_label}: use headphones or lower the volume."
