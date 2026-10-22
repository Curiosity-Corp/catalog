"""Broadcast metering for the Console: sample peak, true peak (dBTP) and loudness (LUFS), in pure Python.

Follows ITU-R BS.1770-4 and the EBU Tech 3341 meter ballistics:

* sample peak and RMS of every frame;
* true peak from 4x oversampling (a windowed-sinc polyphase interpolator, 12 taps per phase);
* K-weighting (a high shelf and the RLB high pass at 48 kHz) feeding momentary (400 ms) and short-term (3 s)
  loudness, `-0.691 + 10 log10(mean square)`. A mono tap is measured as one channel with unity gain, so a
  signal reads 3 LU lower than the same signal played from both speakers.

The audio tap is 48 kHz 32 bit float mono in 20 ms frames. numpy is not a dependency, so the work is kept cheap:
peaks and sums run in C builtins; the interpolator only visits samples within 6 dB of the frame's peak (a sample
pair further down cannot hide an inter-sample peak above the frame's loudest sample); the K-weighting biquads run
only for frames that are not digital silence.
"""

from __future__ import annotations

import array
import math
import operator
import threading
from collections import deque
from collections.abc import Iterable
from typing import NamedTuple

RATE = 48000
FRAME_SAMPLES = RATE // 50  # 20 ms
FRAME_SECONDS = FRAME_SAMPLES / RATE
SILENCE_DB = -90.0
FULL_SCALE_DB = -0.02  # a 24 bit converter's top code is about -0.0000001 dBFS; anything this close is "the rail"
CLIP_TP_DB = -1.0  # EBU R128 / BS.1770 true-peak ceiling
MOMENTARY_FRAMES = round(0.4 / FRAME_SECONDS)
SHORT_TERM_FRAMES = round(3.0 / FRAME_SECONDS)
LUFS_OFFSET = -0.691

# BS.1770-4 table 1 and 2 (48 kHz): the pre-filter high shelf, then the RLB high pass.
SHELF_B = (1.53512485958697, -2.69169618940638, 1.19839281085285)
SHELF_A = (-1.69065929318241, 0.73248077421585)  # a1, a2 (a0 = 1)
RLB_B = (1.0, -2.0, 1.0)
RLB_A = (-1.99004745483398, 0.99007225036621)

# A frame whose mean square is below this (-120 dBFS) is treated as silence and skips the filters.
SILENT_MS = 1e-12
# Samples further than 6 dB below the frame's peak are not interpolated.
TP_WINDOW = 0.5

try:  # math.sumprod is 3.12+; the fallback is a C-level map
    _sumprod = math.sumprod
except AttributeError:  # pragma: no cover - exercised on Python 3.10 and 3.11 only

    def _sumprod(a: Iterable[float], b: Iterable[float]) -> float:
        return sum(map(operator.mul, a, b))


def to_db(linear: float) -> float:
    """Linear amplitude to dB, floored at SILENCE_DB."""
    if linear <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, 20 * math.log10(linear))


def power_to_lufs(mean_square: float) -> float:
    """BS.1770 loudness of a K-weighted mean square, floored at SILENCE_DB."""
    if mean_square <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, LUFS_OFFSET + 10 * math.log10(mean_square))


def _bessel_i0(x: float) -> float:
    total = term = 1.0
    for k in range(1, 40):
        term *= (x / (2 * k)) ** 2
        total += term
        if term < 1e-14 * total:
            break
    return total


def _interpolator(beta: float = 7.0) -> tuple[tuple[float, ...], ...]:
    """Phases 1..3 of a 4x interpolator: 12 Kaiser-windowed sinc taps each, unity gain at DC.

    Phase k yields the signal at n + k/4 from x[n-5] .. x[n+6]. Phase 0 is the sample itself.
    """
    phases = []
    norm = _bessel_i0(beta)
    for k in (1, 2, 3):
        taps = []
        for j in range(-5, 7):
            d = j - k / 4
            sinc = 1.0 if d == 0 else math.sin(math.pi * d) / (math.pi * d)
            window = _bessel_i0(beta * math.sqrt(max(0.0, 1 - (d / 6) ** 2))) / norm
            taps.append(sinc * window)
        gain = sum(taps)
        phases.append(tuple(t / gain for t in taps))
    return tuple(phases)


TP_PHASES = _interpolator()
# The window above must cover the interpolator: 5 samples before and 6 after the position.
TP_BEFORE, TP_AFTER = 5, 6


class FrameLevels(NamedTuple):
    sample_peak: float  # linear, 0..1
    true_peak: float  # linear, >= sample_peak
    rms: float  # linear
    lufs_m: float  # momentary loudness, LUFS
    lufs_s: float  # short-term loudness, LUFS


class Analyzer:
    """Stateful BS.1770 analyser fed consecutive frames of one mono stream (any frame length)."""

    def __init__(self, loudness: bool = True) -> None:
        self.loudness = loudness
        self._tail: list[float] | None = None  # the last 11 samples; None until the first frame primes it
        self._z = [0.0, 0.0, 0.0, 0.0]  # shelf z1 z2, RLB z1 z2 (transposed direct form II)
        self._powers: deque[float] = deque(maxlen=SHORT_TERM_FRAMES)

    def reset(self) -> None:
        """Forget filter and loudness history (a restarted stream)."""
        self._tail = None
        self._z = [0.0, 0.0, 0.0, 0.0]
        self._powers.clear()

    def process(self, samples: array.array) -> FrameLevels:
        n = len(samples)
        if n == 0:
            return FrameLevels(0.0, 0.0, 0.0, *self._lufs())
        sample_peak = max(max(samples), -min(samples))
        mean_square = _sumprod(samples, samples) / n
        if not math.isfinite(mean_square) or not math.isfinite(sample_peak):
            return FrameLevels(0.0, 0.0, 0.0, *self._lufs())  # a garbled frame (NaN/inf) must not poison the filters
        true_peak = self._true_peak(samples, sample_peak)
        if self.loudness:
            self._powers.append(self._weighted_power(samples) if mean_square > SILENT_MS else 0.0)
        return FrameLevels(sample_peak, true_peak, math.sqrt(mean_square), *self._lufs())

    def _lufs(self) -> tuple[float, float]:
        if not self.loudness or not self._powers:
            return SILENCE_DB, SILENCE_DB
        powers = self._powers
        count = len(powers)
        recent = list(powers)[-MOMENTARY_FRAMES:]
        return power_to_lufs(sum(recent) / len(recent)), power_to_lufs(sum(powers) / count)

    def _weighted_power(self, samples: array.array) -> float:
        """Mean square of the K-weighted frame; the biquads are inlined, this loop is the CPU cost."""
        b0, b1, b2 = SHELF_B
        a1, a2 = SHELF_A
        c0, c1, c2 = RLB_B
        d1, d2 = RLB_A
        s1, s2, r1, r2 = self._z
        total = 0.0
        for x in samples:
            y = b0 * x + s1
            s1 = b1 * x - a1 * y + s2
            s2 = b2 * x - a2 * y
            z = c0 * y + r1
            r1 = c1 * y - d1 * z + r2
            r2 = c2 * y - d2 * z
            total += z * z
        self._z = [s1, s2, r1, r2]
        return total / len(samples)

    def _true_peak(self, samples: array.array, sample_peak: float) -> float:
        """Max |x| over the 4x oversampled frame, with the interpolator reading across frame boundaries."""
        keep = TP_BEFORE + TP_AFTER
        fresh = samples.tolist()
        # the first frame has no history: extend its first sample backwards so the start is not a step (Gibbs ringing)
        ext = (self._tail if self._tail is not None else [fresh[0]] * keep) + fresh
        self._tail = ext[-keep:]
        peak = sample_peak
        if peak <= 0:
            return 0.0
        floor = sample_peak * TP_WINDOW
        p1, p2, p3 = TP_PHASES
        # position i (in ext) lies between ext[i] and ext[i + 1]; the frame's own positions start at TP_AFTER
        # so the interpolator's look-ahead is already present (this delays the true peak by 6 samples, 0.125 ms)
        for i in range(TP_BEFORE, len(ext) - TP_AFTER):
            if abs(ext[i]) < floor and abs(ext[i + 1]) < floor:
                continue
            window = ext[i - TP_BEFORE : i + TP_AFTER + 1]
            for taps in (p1, p2, p3):
                value = abs(_sumprod(taps, window))
                if value > peak:
                    peak = value
        return peak


class ClipLatch:
    """Remembers that a signal reached the ceiling until somebody clears it (shared by a reader and the UI)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    @property
    def latched(self) -> bool:
        return self._count > 0

    @property
    def count(self) -> int:
        """Clipped frames since the last clear."""
        return self._count

    def trip(self, sample_db: float, true_db: float) -> bool:
        """Record one frame; True when it clipped (true peak at or above -1 dBTP, or the rail)."""
        hit = true_db >= CLIP_TP_DB or sample_db >= FULL_SCALE_DB
        if hit:
            with self._lock:
                self._count += 1
        return hit

    def clear(self) -> None:
        with self._lock:
            self._count = 0
