"""Local voice analysis for the AI director: features only, computed on this machine.

A few seconds of the raw microphone become a small set of numbers: pitch (f0 median and range), spectral
balance (centroid, tilt, body, clarity, sibilance), noise floor and signal to noise, integrated loudness
(ITU-R BS.1770 LUFS) and a rough room-decay estimate. Only those numbers are ever put in a prompt or a journal;
the samples are read from a temporary WAV, analysed in memory and discarded.

The implementation is standard library only (an iterative radix-2 FFT, an autocorrelation pitch tracker, the
BS.1770 K-weighting filter), so it adds no dependency and runs unchanged in CI. ffmpeg, which StreamStack already
needs, only decodes the recording to 24 kHz mono. A 4 s clip is analysed in roughly 1-2 s of CPU; the frame count
is capped so a longer clip costs the same.

These are estimates tuned for speech through a close microphone, not lab measurements. Persona targets use the
same units, so a bias in an estimate moves the reading and the target together.
"""

from __future__ import annotations

import cmath
import math
import statistics
import subprocess
from array import array
from functools import lru_cache
from pathlib import Path

RATE = 24000
FRAME = 1024  # spectral frame: 42.7 ms, 23.4 Hz per bin
HOP_MIN = 512
MAX_SPECTRAL_FRAMES = 120
PITCH_RANGE_HZ = (60.0, 400.0)
PITCH_DECIMATE = 3  # 24 kHz -> 8 kHz for the pitch tracker
PITCH_FRAME = 480  # 60 ms at 8 kHz
MAX_PITCH_FRAMES = 80
ENV_FRAME = 480  # 20 ms level frames
MIN_SECONDS = 1.0
FLOOR_DB = -120.0

# Bands in Hz used for the read-outs.
BANDS = {
    "sub": (20.0, 80.0),
    "body": (100.0, 300.0),
    "low_mid": (250.0, 500.0),
    "mid": (500.0, 2000.0),
    "clarity": (2000.0, 4000.0),
    "presence": (4000.0, 5000.0),
    "sibilance": (5000.0, 9000.0),
    "air": (9000.0, 11500.0),
}


class AudioAnalysisError(RuntimeError):
    """The recording could not be decoded or was unusable."""


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def decode(path: Path, rate: int = RATE, timeout: float = 30.0) -> list[float]:
    """A WAV (or anything ffmpeg reads) as mono floats in -1..1 at `rate`."""
    argv = ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise AudioAnalysisError("ffmpeg is not installed, so the voice cannot be analysed") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioAnalysisError(f"ffmpeg took longer than {timeout:g} s to decode the clip") from exc
    except OSError as exc:
        raise AudioAnalysisError(f"ffmpeg cannot run ({exc.strerror})") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[:200]
        raise AudioAnalysisError(f"ffmpeg could not decode the clip: {detail or 'no audio'}")
    pcm = array("h")
    pcm.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    return [v / 32768.0 for v in pcm]


# ---------------------------------------------------------------------------
# DSP primitives
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _plan(n: int) -> tuple[tuple[int, ...], tuple[complex, ...]]:
    bits = n.bit_length() - 1
    reverse = tuple(int(f"{i:0{bits}b}"[::-1], 2) if bits else 0 for i in range(n))
    twiddle = tuple(cmath.exp(-2j * math.pi * k / n) for k in range(n // 2))
    return reverse, twiddle


def fft(values: list[complex]) -> list[complex]:
    """Radix-2 FFT; the length must be a power of two."""
    n = len(values)
    if n < 2 or n & (n - 1):
        raise ValueError("fft length must be a power of two")
    reverse, twiddle = _plan(n)
    data = [values[i] for i in reverse]
    size = 2
    while size <= n:
        half, step = size // 2, n // size
        for start in range(0, n, size):
            for k in range(half):
                a, b = data[start + k], data[start + k + half] * twiddle[k * step]
                data[start + k], data[start + k + half] = a + b, a - b
        size *= 2
    return data


def ifft_real(values: list[complex]) -> list[float]:
    n = len(values)
    return [v.real / n for v in fft([v.conjugate() for v in values])]


@lru_cache(maxsize=4)
def hann(n: int) -> tuple[float, ...]:
    return tuple(0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n))


def power_spectrum(frame: list[float]) -> list[float]:
    """Power of the Hann-windowed frame for bins 0..n/2."""
    n = len(frame)
    window = hann(n)
    spec = fft([complex(frame[i] * window[i], 0.0) for i in range(n)])
    return [spec[k].real ** 2 + spec[k].imag ** 2 for k in range(n // 2 + 1)]


def to_db(power: float, floor: float = FLOOR_DB) -> float:
    return 10.0 * math.log10(power) if power > 1e-12 else floor


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


# ---------------------------------------------------------------------------
# Loudness: ITU-R BS.1770 / EBU R128
# ---------------------------------------------------------------------------


def k_weighting(rate: int) -> tuple[tuple[float, float, float, float, float], ...]:
    """The two K-weighting biquads (b0, b1, b2, a1, a2) for any sample rate (libebur128's analytic form)."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10 ** (gain / 20)
    vb = vh**0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = (
        (vh + vb * k / q + k * k) / a0,
        2 * (k * k - vh) / a0,
        (vh - vb * k / q + k * k) / a0,
        2 * (k * k - 1) / a0,
        (1 - k / q + k * k) / a0,
    )
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1 + k / q + k * k
    high_pass = (1.0, -2.0, 1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0)
    return shelf, high_pass


def biquad(samples: list[float], coeffs: tuple[float, float, float, float, float]) -> list[float]:
    b0, b1, b2, a1, a2 = coeffs
    x1 = x2 = y1 = y2 = 0.0
    out = []
    for x in samples:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out.append(y)
        x2, x1, y2, y1 = x1, x, y1, y
    return out


def lufs(samples: list[float], rate: int = RATE) -> float | None:
    """Integrated loudness (gated, 400 ms blocks at 75 % overlap) in LUFS; None for silence or a clip under 0.4 s."""
    block, hop = int(0.4 * rate), int(0.1 * rate)
    if len(samples) < block:
        return None
    shelf, high_pass = k_weighting(rate)
    weighted = biquad(biquad(samples, shelf), high_pass)
    squares = [v * v for v in weighted]
    prefix = [0.0]
    for v in squares:
        prefix.append(prefix[-1] + v)
    energies = [(prefix[s + block] - prefix[s]) / block for s in range(0, len(squares) - block + 1, hop)]
    loud = [(e, -0.691 + 10 * math.log10(e)) for e in energies if e > 0]
    absolute = [e for e, level in loud if level > -70.0]
    if not absolute:
        return None
    relative_gate = -0.691 + 10 * math.log10(sum(absolute) / len(absolute)) - 10.0
    kept = [e for e, level in loud if level > -70.0 and level > relative_gate]
    return -0.691 + 10 * math.log10(sum(kept) / len(kept)) if kept else None


# ---------------------------------------------------------------------------
# Pitch
# ---------------------------------------------------------------------------


def decimate(samples: list[float], factor: int) -> list[float]:
    """Box-filter decimation; enough for tracking a fundamental well below the new Nyquist."""
    usable = len(samples) - len(samples) % factor
    return [sum(samples[i : i + factor]) / factor for i in range(0, usable, factor)]


def frame_pitch(frame: list[float], rate: float) -> tuple[float, float] | None:
    """(f0 in Hz, periodicity 0..1) of one frame by normalised autocorrelation, or None when it is unvoiced."""
    n = len(frame)
    mean = sum(frame) / n
    window = hann(n)
    padded = [complex((frame[i] - mean) * window[i], 0.0) for i in range(n)] + [0j] * (2048 - n)
    spec = fft(padded)
    acf = ifft_real([complex(v.real**2 + v.imag**2, 0.0) for v in spec])
    if acf[0] <= 1e-9:
        return None
    low_lag, high_lag = int(rate / PITCH_RANGE_HZ[1]), min(int(rate / PITCH_RANGE_HZ[0]), n // 2)
    norm = [acf[lag] / acf[0] for lag in range(high_lag + 2)]
    peaks = [
        lag for lag in range(max(low_lag, 2), high_lag + 1) if norm[lag] >= norm[lag - 1] and norm[lag] > norm[lag + 1]
    ]
    if not peaks:
        return None
    best = max(norm[lag] for lag in peaks)
    if best < 0.45:
        return None
    # The shortest peak close to the best one: multiples of the period score alike, and the first is the pitch.
    lag = next(lag for lag in peaks if norm[lag] >= 0.9 * best)
    a, b, c = norm[lag - 1], norm[lag], norm[lag + 1]
    denominator = a - 2 * b + c
    shift = 0.5 * (a - c) / denominator if denominator else 0.0
    return rate / (lag + max(-1.0, min(1.0, shift))), min(1.0, best)


def pitch_features(samples: list[float], rate: int, loud_frames: list[int]) -> dict[str, float | int | None]:
    """f0 median, 10th/90th percentile, range in semitones and the voiced share over the loudest frames."""
    low = decimate(samples, PITCH_DECIMATE)
    low_rate = rate / PITCH_DECIMATE
    per_env = ENV_FRAME // PITCH_DECIMATE
    hits: list[float] = []
    tried = 0
    for index in loud_frames[:MAX_PITCH_FRAMES]:
        start = index * per_env
        chunk = low[start : start + PITCH_FRAME]
        if len(chunk) < PITCH_FRAME:
            continue
        tried += 1
        found = frame_pitch(chunk, low_rate)
        if found:
            hits.append(found[0])
    out: dict[str, float | int | None] = {"f0_hz": None, "f0_low_hz": None, "f0_high_hz": None, "f0_range_st": None}
    out["voiced_frames"] = len(hits)
    out["voiced_ratio"] = round(len(hits) / tried, 3) if tried else 0.0
    if len(hits) >= 5:
        p10, p90 = percentile(hits, 0.1), percentile(hits, 0.9)
        out.update(
            f0_hz=round(statistics.median(hits), 1),
            f0_low_hz=round(p10, 1),
            f0_high_hz=round(p90, 1),
            f0_range_st=round(12 * math.log2(p90 / p10), 1),
        )
    return out


def pitch_band(f0: float | None) -> str | None:
    """A neutral word for where the voice sits (typical speech runs 85-255 Hz)."""
    if f0 is None:
        return None
    return "low" if f0 < 110 else "medium-low" if f0 < 165 else "medium" if f0 < 220 else "high"


# ---------------------------------------------------------------------------
# Levels, noise and room
# ---------------------------------------------------------------------------


def level_envelope(samples: list[float]) -> list[float]:
    """RMS level per 20 ms frame in dBFS."""
    env = []
    for start in range(0, len(samples) - ENV_FRAME + 1, ENV_FRAME):
        chunk = samples[start : start + ENV_FRAME]
        env.append(to_db(sum(v * v for v in chunk) / ENV_FRAME))
    return env


def estimate_rt60(samples: list[float], rate: int, floor_db: float) -> tuple[float | None, int]:
    """(RT60 in seconds, decays used): a rough room-decay estimate from free decays after loud bursts.

    Looks for places where the 10 ms level falls at least 18 dB without a rise, fits the dB-per-second slope
    between 5 and 35 dB below the peak (the T30 method) and takes the median over decays. Speech endings are
    short and often damped by the speaker, so with fewer than one clean decay there is no estimate at all.
    """
    hop = int(0.01 * rate)
    env = []
    for start in range(0, len(samples) - 2 * hop + 1, hop):
        chunk = samples[start : start + 2 * hop]
        env.append(to_db(sum(v * v for v in chunk) / len(chunk)))
    slopes: list[float] = []
    i = 0
    while i < len(env) - 8:
        window_end = min(len(env), i + 8)
        if env[i] != max(env[i:window_end]) or env[i] < floor_db + 25:
            i += 1
            continue
        peak = env[i]
        j, series = i, []
        while j < len(env) and env[j] > max(floor_db + 6, peak - 40):
            if series and env[j] > series[-1][1] + 3:
                break
            series.append((j, env[j]))
            j += 1
        usable = [(t, level) for t, level in series if peak - 35 <= level <= peak - 5]
        if len(usable) >= 8 and usable[0][1] - usable[-1][1] >= 18:
            xs = [t * 0.01 for t, _ in usable]
            ys = [level for _, level in usable]
            mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mean_x) ** 2 for x in xs)
            sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
            syy = sum((y - mean_y) ** 2 for y in ys)
            if sxx > 0 and syy > 0 and sxy < 0 and (sxy * sxy) / (sxx * syy) >= 0.8:
                slopes.append(sxy / sxx)
        i = max(j, i + 1)
    if not slopes:
        return None, 0
    return round(-60.0 / statistics.median(slopes), 2), len(slopes)


def room_word(rt60: float | None) -> str | None:
    if rt60 is None:
        return None
    return "dry" if rt60 < 0.3 else "natural" if rt60 < 0.5 else "roomy" if rt60 < 0.8 else "echoey"


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def band_density(spectrum: list[float], rate: int, size: int, low: float, high: float) -> float:
    """Mean power per bin inside [low, high) Hz."""
    first = max(1, int(low * size / rate))
    last = min(len(spectrum) - 1, max(first + 1, int(high * size / rate)))
    chunk = spectrum[first:last]
    return sum(chunk) / len(chunk) if chunk else 0.0


def band_energy(spectrum: list[float], rate: int, size: int, low: float, high: float) -> float:
    first = max(1, int(low * size / rate))
    last = min(len(spectrum) - 1, max(first + 1, int(high * size / rate)))
    return sum(spectrum[first:last])


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def analyze(samples: list[float], rate: int = RATE) -> dict:
    """Voice features of a mono clip. Raises AudioAnalysisError when the clip is too short to say anything."""
    if rate < 16000:
        raise AudioAnalysisError(f"a sample rate of {rate} Hz is too low to measure sibilance")
    seconds = len(samples) / rate
    if seconds < MIN_SECONDS:
        raise AudioAnalysisError(f"the clip is {seconds:.1f} s long; at least {MIN_SECONDS:g} s is needed")
    env = level_envelope(samples)
    floor = percentile(env, 0.10)
    speech_level = percentile(env, 0.95)
    threshold = max(floor + 12.0, -55.0)
    loud = sorted((i for i, level in enumerate(env) if level >= threshold), key=lambda i: -env[i])
    speech_ratio = len(loud) / len(env)
    peak = max(abs(v) for v in samples)
    features: dict = {
        "seconds": round(seconds, 2),
        "peak_db": round(to_db(peak * peak), 1),
        "speech_db": round(speech_level, 1),
        "noise_floor_db": round(floor, 1),
        "snr_db": round(speech_level - floor, 1),
        "speech_ratio": round(speech_ratio, 3),
        "lufs": None,
        "enough_speech": speech_ratio >= 0.12 and speech_level - floor >= 10.0,
    }
    value = lufs(samples, rate)
    features["lufs"] = round(value, 1) if value is not None else None
    rt60, decays = estimate_rt60(samples, rate, floor)
    features.update(rt60_s=rt60, rt60_decays=decays, room=room_word(rt60))
    features.update(pitch_features(samples, rate, loud))
    features["pitch_band"] = pitch_band(features["f0_hz"])
    features.update(spectral_features(samples, rate, env, threshold))
    features["scores"] = scores(features)
    return features


def spectral_features(samples: list[float], rate: int, env: list[float], threshold: float) -> dict:
    """Average speech spectrum read-outs; empty-valued (None) when there is no speech to average."""
    candidates = [
        start
        for start in range(0, len(samples) - FRAME + 1, max(HOP_MIN, len(samples) // MAX_SPECTRAL_FRAMES))
        if env[min(len(env) - 1, (start + FRAME // 2) // ENV_FRAME)] >= threshold
    ]
    empty = {
        "centroid_hz": None,
        "tilt_db_oct": None,
        "body_db": None,
        "clarity_db": None,
        "sibilance_db": None,
        "sibilance_peak_db": None,
        "bands_db": None,
    }
    if len(candidates) < 3:
        return empty
    total = [0.0] * (FRAME // 2 + 1)
    sib_ratios: list[float] = []
    for start in candidates:
        spectrum = power_spectrum(samples[start : start + FRAME])
        for k, p in enumerate(spectrum):
            total[k] += p
        rest = band_energy(spectrum, rate, FRAME, 100.0, 5000.0)
        if rest > 0:
            sib_ratios.append(to_db(band_energy(spectrum, rate, FRAME, *BANDS["sibilance"]) / rest))
    avg = [p / len(candidates) for p in total]
    bin_hz = rate / FRAME
    lo, hi = int(100 / bin_hz), int(8000 / bin_hz)
    weights = avg[lo:hi]
    weight_sum = sum(weights)
    centroid = sum((lo + i) * bin_hz * w for i, w in enumerate(weights)) / weight_sum if weight_sum > 0 else None
    mid = band_density(avg, rate, FRAME, *BANDS["mid"])
    if mid <= 0:
        return empty

    def relative(name: str) -> float:
        return round(to_db(band_density(avg, rate, FRAME, *BANDS[name]) / mid), 1)

    return {
        "centroid_hz": round(centroid, 0) if centroid else None,
        "tilt_db_oct": tilt(avg, rate),
        "body_db": relative("body"),
        "clarity_db": relative("clarity"),
        "sibilance_db": relative("sibilance"),
        "sibilance_peak_db": round(percentile(sib_ratios, 0.9), 1) if sib_ratios else None,
        "bands_db": {name: relative(name) for name in BANDS if name not in ("sub",)},
    }


def tilt(spectrum: list[float], rate: int) -> float | None:
    """Spectral slope in dB per octave over 150 Hz - 6 kHz, from third-octave averages."""
    points: list[tuple[float, float]] = []
    centre = 160.0
    while centre * 2 ** (1 / 6) < 6000:
        low, high = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
        density = band_density(spectrum, rate, FRAME, low, high)
        if density > 0:
            points.append((math.log2(centre), to_db(density)))
        centre *= 2 ** (1 / 3)
    if len(points) < 5:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    return round(sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx, 1) if sxx else None


def scores(features: dict) -> dict[str, float | None]:
    """0..1 positions for the Console bars. Linear maps of the dB read-outs; documented, not magic."""

    def scale(value: float | None, low: float, high: float) -> float | None:
        return None if value is None else round(clamp01((value - low) / (high - low)), 2)

    f0 = features.get("f0_hz")
    return {
        # Pitch: 70 Hz at the bottom of the bar, 300 Hz at the top.
        "pitch": scale(f0, 70.0, 300.0),
        # Depth: low-end body against the mids, -10 dB (thin) .. +10 dB (rich).
        "depth": scale(features.get("body_db"), -10.0, 10.0),
        # Clarity: 2-4 kHz against the mids, -20 dB (dull) .. 0 dB (forward).
        "clarity": scale(features.get("clarity_db"), -20.0, 0.0),
        # Sibilance: the harshest tenth of speech frames' 5-9 kHz share, -35 dB (soft) .. -10 dB (harsh).
        "sibilance": scale(features.get("sibilance_peak_db"), -35.0, -10.0),
        # Noise: signal to noise, 15 dB (noisy) .. 55 dB (clean); the bar shows how clean it is.
        "clean": scale(features.get("snr_db"), 15.0, 55.0),
    }


def analyze_wav(path: Path) -> dict:
    """Decode a recording and analyse it; the samples never leave this function."""
    return analyze(decode(path), RATE)
