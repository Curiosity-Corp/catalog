"""Voice feature extraction, checked on synthetic signals with known answers (no microphone, no ffmpeg)."""

from __future__ import annotations

import math
import random

import pytest

from streamstack.director import audiofeatures as audio

RATE = 24000


def voice(f0: float, seconds: float = 3.0, tilt: float = -1.0, noise: float = 1e-4, harmonics: int = 12) -> list[float]:
    """A harmonic tone with a syllable-like envelope; `tilt` is the harmonic roll-off exponent."""
    rnd = random.Random(1)  # noqa: S311
    out = []
    for i in range(int(seconds * RATE)):
        t = i / RATE
        envelope = 1.0 if (t * 1.5) % 1.0 < 0.65 else 0.0  # words with pauses between them
        body = sum(math.sin(2 * math.pi * f0 * h * t) / h ** (-tilt) for h in range(1, harmonics + 1))
        out.append(0.06 * envelope * body + rnd.gauss(0, noise))
    return out


def reverb(rt60: float, bursts: int = 3) -> list[float]:
    """Noise bursts that decay by 60 dB in `rt60` seconds, with a quiet floor between."""
    rnd = random.Random(3)  # noqa: S311
    out: list[float] = []
    for _ in range(bursts):
        out += [0.1 * rnd.gauss(0, 1) + rnd.gauss(0, 1e-5) for _ in range(int(0.4 * RATE))]
        out += [
            0.05 * rnd.gauss(0, 1) * 10 ** (-3 * (i / RATE) / rt60) + rnd.gauss(0, 1e-5) for i in range(int(1.2 * RATE))
        ]
    return out


@pytest.mark.parametrize("f0", [110.0, 210.0])
def test_pitch_is_found_within_a_percent(f0: float) -> None:
    features = audio.analyze(voice(f0), RATE)
    assert features["f0_hz"] == pytest.approx(f0, rel=0.01)
    assert features["voiced_ratio"] > 0.3
    assert features["pitch_band"] == audio.pitch_band(f0)


def test_a_full_scale_1khz_sine_reads_close_to_minus_three_lufs() -> None:
    sine = [math.sin(2 * math.pi * 1000 * i / RATE) for i in range(RATE * 3)]
    assert audio.lufs(sine, RATE) == pytest.approx(-3.0, abs=0.2)
    assert audio.lufs([0.1 * v for v in sine], RATE) == pytest.approx(-23.0, abs=0.2)


def test_a_brighter_voice_has_a_higher_centroid_and_clarity() -> None:
    dull = audio.analyze(voice(130, tilt=-2.0), RATE)
    bright = audio.analyze(voice(130, tilt=-0.2, harmonics=30), RATE)
    assert bright["centroid_hz"] > dull["centroid_hz"]
    assert bright["clarity_db"] > dull["clarity_db"]
    assert bright["tilt_db_oct"] > dull["tilt_db_oct"]
    assert bright["scores"]["clarity"] > dull["scores"]["clarity"]


def test_sibilance_rises_with_energy_at_five_to_nine_khz() -> None:
    plain = voice(130)
    rnd = random.Random(5)  # noqa: S311
    hiss = [v + 0.004 * rnd.gauss(0, 1) for v in plain]
    assert audio.analyze(hiss, RATE)["sibilance_peak_db"] > audio.analyze(plain, RATE)["sibilance_peak_db"]


def test_noise_floor_and_snr() -> None:
    quiet = audio.analyze(voice(120, noise=1e-5), RATE)
    noisy = audio.analyze(voice(120, noise=3e-3), RATE)
    assert quiet["snr_db"] > noisy["snr_db"] + 15
    assert quiet["noise_floor_db"] < noisy["noise_floor_db"]
    assert quiet["scores"]["clean"] > noisy["scores"]["clean"]


@pytest.mark.parametrize(("rt60", "word"), [(0.25, "dry"), (0.5, "roomy"), (1.0, "echoey")])
def test_reverb_time_is_estimated_from_decays(rt60: float, word: str) -> None:
    estimate, decays = audio.estimate_rt60(reverb(rt60), RATE, -100.0)
    assert decays >= 2
    assert estimate == pytest.approx(rt60, rel=0.25)
    assert audio.room_word(estimate) in {word, "natural", "roomy"}


def test_silence_and_short_clips_are_refused_or_flagged() -> None:
    with pytest.raises(audio.AudioAnalysisError, match="at least"):
        audio.analyze([0.0] * 1000, RATE)
    with pytest.raises(audio.AudioAnalysisError, match="too low"):
        audio.analyze([0.0] * 80000, 8000)
    silent = audio.analyze([0.0] * (3 * RATE), RATE)
    assert silent["enough_speech"] is False and silent["f0_hz"] is None
    assert all(v is None for v in silent["scores"].values() if v is None) or silent["scores"]["pitch"] is None


def test_scores_stay_between_zero_and_one() -> None:
    features = audio.analyze(voice(180, tilt=-0.5), RATE)
    for name, value in features["scores"].items():
        assert value is None or 0.0 <= value <= 1.0, name


def test_fft_matches_a_direct_transform() -> None:
    signal = [math.sin(2 * math.pi * 3 * n / 16) + 0.5 * math.cos(2 * math.pi * 5 * n / 16) for n in range(16)]
    spectrum = audio.fft([complex(v) for v in signal])
    for k in (3, 5):
        direct = sum(
            signal[n] * complex(math.cos(2 * math.pi * k * n / 16), -math.sin(2 * math.pi * k * n / 16))
            for n in range(16)
        )
        assert abs(spectrum[k] - direct) < 1e-9


def test_decode_reports_a_missing_file(tmp_path) -> None:
    with pytest.raises(audio.AudioAnalysisError):
        audio.decode(tmp_path / "absent.wav")
