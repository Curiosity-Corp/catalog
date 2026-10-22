"""ITU-R BS.1770-4 / EBU Tech 3341 metering: K-weighting, loudness, true peak and the clip latch, on synthetic signals
with known answers. The expected values come from the standards, not from this code's own output."""

from __future__ import annotations

import array
import cmath
import math
import random
import sys
import time

import pytest

from streamstack.console import loudness
from streamstack.console.loudness import Analyzer, ClipLatch, FrameLevels

RATE = loudness.RATE
FRAME = loudness.FRAME_SAMPLES


def sine(freq: float, peak_db: float, seconds: float, phase: float = 0.0) -> array.array:
    amp = 10 ** (peak_db / 20)
    return array.array(
        "f", (amp * math.sin(2 * math.pi * freq * i / RATE + phase) for i in range(round(seconds * RATE)))
    )


def run(signal: array.array, analyzer: Analyzer | None = None, frame: int = FRAME) -> list[FrameLevels]:
    analyzer = analyzer or Analyzer()
    return [analyzer.process(signal[i : i + frame]) for i in range(0, len(signal) - frame + 1, frame)]


def db(linear: float) -> float:
    return 20 * math.log10(linear)


def response_db(freq: float) -> float:
    """Magnitude of the K-weighting cascade (shelf then RLB high pass) at `freq`, from its coefficients."""
    z = cmath.exp(-1j * 2 * math.pi * freq / RATE)

    def stage(b: tuple[float, ...], a: tuple[float, float]) -> complex:
        return (b[0] + b[1] * z + b[2] * z * z) / (1 + a[0] * z + a[1] * z * z)

    return 20 * math.log10(abs(stage(loudness.SHELF_B, loudness.SHELF_A) * stage(loudness.RLB_B, loudness.RLB_A)))


# ---------------------------------------------------------------------------
# K-weighting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("freq", "expected", "tolerance"),
    [
        (997, 0.691, 0.01),  # the origin of BS.1770's -0.691 offset: a 997 Hz tone's K gain
        (38, -6.0, 0.1),  # RLB corner
        (10000, 4.04, 0.05),  # the pre-filter's head-shape shelf, about +4 dB
        (500, 0.04, 0.05),
    ],
)
def test_k_weighting_response_matches_bs1770(freq: float, expected: float, tolerance: float) -> None:
    assert response_db(freq) == pytest.approx(expected, abs=tolerance)


def test_k_filter_runs_as_designed() -> None:
    """The streaming biquads must realise the coefficient response: a 10 kHz tone reads about 4 dB over a 500 Hz one."""
    low = run(sine(500, -20, 3))[-1].lufs_s
    high = run(sine(10000, -20, 3))[-1].lufs_s
    assert high - low == pytest.approx(response_db(10000) - response_db(500), abs=0.05)


# ---------------------------------------------------------------------------
# Loudness (LUFS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("peak_db", "lufs"), [(-20.0, -23.0), (-30.0, -33.0), (-40.0, -43.0), (-3.0, -6.0)])
def test_reference_sine_reads_its_lufs(peak_db: float, lufs: float) -> None:
    """EBU Tech 3341 tests 1 and 2 use a 997 Hz sine at -23 / -33 LUFS (+-0.1 LU). Stereo carries the power twice, so
    the same loudness from the mono tap is a sine 3 dB hotter than the per-channel level: -20 dBFS peak is -23 LUFS."""
    last = run(sine(997, peak_db, 4))[-1]
    assert last.lufs_m == pytest.approx(lufs, abs=0.1)
    assert last.lufs_s == pytest.approx(lufs, abs=0.1)


def test_stereo_equivalence_of_the_reference() -> None:
    """Tech 3341 test 1: 997 Hz, -23 dBFS in both channels = -23 LUFS. Both channels carry the same power, so
    the mono reading of one channel is 3.01 LU lower."""
    one_channel = run(sine(997, -23, 4))[-1].lufs_m
    assert one_channel + 10 * math.log10(2) == pytest.approx(-23.0, abs=0.1)


def test_loudness_follows_level_one_for_one() -> None:
    a = run(sine(997, -26, 4))[-1].lufs_s
    b = run(sine(997, -14, 4))[-1].lufs_s
    assert b - a == pytest.approx(12.0, abs=0.01)


def test_momentary_is_400_ms_and_short_term_3_s() -> None:
    assert loudness.MOMENTARY_FRAMES == 20
    assert loudness.SHORT_TERM_FRAMES == 150
    analyzer = Analyzer()
    run(sine(997, -20, 3.2), analyzer)  # fills the 3 s window with -23 LUFS
    levels = run(array.array("f", [0.0] * RATE), analyzer)  # then silence
    at_400ms = levels[19]  # 20 silent frames: the momentary window holds only silence
    assert at_400ms.lufs_m == loudness.SILENCE_DB
    # the 3 s window still holds (3.0 - 0.4) s of tone: 10 log10(2.6 / 3) below the tone
    assert at_400ms.lufs_s == pytest.approx(-23.0 + 10 * math.log10(2.6 / 3.0), abs=0.15)
    assert levels[-1].lufs_s == pytest.approx(-23.0 + 10 * math.log10(2.0 / 3.0), abs=0.15)  # one second on: 2 s left
    drained = run(array.array("f", [0.0] * (2 * RATE)), analyzer)[-1]
    assert drained.lufs_s == loudness.SILENCE_DB  # three seconds of silence empty the window
    # half a window of tone reads 3 dB under the tone
    analyzer = Analyzer()
    run(array.array("f", [0.0] * (RATE // 5)), analyzer)
    half = run(sine(997, -20, 0.2), analyzer)[-1]
    assert half.lufs_m == pytest.approx(-23.0 - 3.0, abs=0.2)


def test_step_response_is_quick() -> None:
    """A level jump shows in the momentary reading within one frame's worth of the window, not seconds later."""
    analyzer = Analyzer()
    run(array.array("f", [0.0] * RATE), analyzer)
    frames = run(sine(997, -10, 0.1), analyzer)
    assert frames[0].lufs_m > -40  # 20 ms of tone is already audible on the meter
    assert frames[-1].lufs_m > -23.0 - 10 * math.log10(4)


def test_frame_length_does_not_change_the_answer() -> None:
    signal = sine(997, -20, 2)
    reference = run(signal)[-1].lufs_s
    assert run(signal, frame=480)[-1].lufs_s == pytest.approx(reference, abs=0.01)


def test_silence_and_garbage_do_not_poison_the_meter() -> None:
    analyzer = Analyzer()
    quiet = analyzer.process(array.array("f", [0.0] * FRAME))
    assert (quiet.sample_peak, quiet.true_peak, quiet.rms) == (0.0, 0.0, 0.0)
    assert quiet.lufs_m == quiet.lufs_s == loudness.SILENCE_DB
    run(sine(997, -20, 1), analyzer)
    bad = analyzer.process(array.array("f", [float("nan")] * FRAME))
    assert bad.true_peak == 0.0
    assert math.isfinite(run(sine(997, -20, 1), analyzer)[-1].lufs_m)
    inf = analyzer.process(array.array("f", [float("inf")] * FRAME))
    assert inf.true_peak == 0.0
    assert analyzer.process(array.array("f")).true_peak == 0.0  # an empty frame


def test_loudness_can_be_switched_off() -> None:
    last = run(sine(997, -20, 1), Analyzer(loudness=False))[-1]
    assert last.lufs_m == last.lufs_s == loudness.SILENCE_DB
    assert last.sample_peak == pytest.approx(0.1, abs=1e-4)


# ---------------------------------------------------------------------------
# Peaks
# ---------------------------------------------------------------------------


def test_sample_peak_and_rms() -> None:
    last = run(sine(997, -6, 1))[-1]
    assert db(last.sample_peak) == pytest.approx(-6.0, abs=0.05)
    assert db(last.rms) == pytest.approx(-6.0 - 3.01, abs=0.05)  # a sine's RMS is 3 dB under its peak


def test_negative_extreme_counts_as_peak() -> None:
    analyzer = Analyzer()
    out = analyzer.process(array.array("f", [0.0] * 100 + [-0.5] + [0.0] * 100))
    assert out.sample_peak == 0.5


@pytest.mark.parametrize("freq", [1000, 6000, 12000, 15000, 18000])
@pytest.mark.parametrize("phase", [0.1, 0.7, math.pi / 4, 1.3])
def test_true_peak_of_a_steady_sine_is_its_amplitude(freq: float, phase: float) -> None:
    """The 4x interpolator recovers a sine's real peak whatever its phase against the sample grid. BS.1770-4
    Annex 2 / Tech 3341 allow +0.2 / -0.4 dB on true-peak readings."""
    frames = run(sine(freq, -6, 0.5, phase))[2:]  # skip the start-up frames
    tp = max(f.true_peak for f in frames)
    assert db(tp) - -6.0 == pytest.approx(0.0, abs=0.3)
    assert -0.4 <= db(tp) + 6.0 <= 0.2


def test_true_peak_finds_the_inter_sample_peak() -> None:
    """EBU Tech 3341 test: a fs/4 sine at 45 degrees. Every sample sits at -3.01 dB under the peak, but the
    waveform really reaches the peak. Sample peak -9.0 dBFS, true peak -6.0 dBTP."""
    frames = run(sine(RATE / 4, -6, 0.5, math.pi / 4))[2:]
    assert max(db(f.sample_peak) for f in frames) == pytest.approx(-6.0 - 3.01, abs=0.02)
    assert max(db(f.true_peak) for f in frames) == pytest.approx(-6.0, abs=0.2)


def test_true_peak_at_full_scale_reads_zero_dbtp() -> None:
    frames = run(sine(RATE / 4, 0.0, 0.5, math.pi / 4))[2:]
    assert max(db(f.sample_peak) for f in frames) == pytest.approx(-3.01, abs=0.02)
    assert max(db(f.true_peak) for f in frames) == pytest.approx(0.0, abs=0.2)


def test_true_peak_exceeds_full_scale_on_an_intersample_over() -> None:
    """Samples at 0.8 and no sample over -1.9 dBFS, yet the waveform is at +1.1 dBTP: a converter would clip it."""
    frames = run(sine(RATE / 4, db(0.8 * math.sqrt(2)), 0.5, math.pi / 4))[2:]
    assert max(f.sample_peak for f in frames) == pytest.approx(0.8, abs=1e-3)
    assert max(db(f.true_peak) for f in frames) == pytest.approx(db(0.8 * math.sqrt(2)), abs=0.2)


def test_true_peak_is_never_below_sample_peak() -> None:
    rng = random.Random(7)  # noqa: S311 - a reproducible test signal
    noise = array.array("f", (rng.uniform(-0.9, 0.9) for _ in range(RATE)))
    assert all(f.true_peak >= f.sample_peak for f in run(noise))


def test_true_peak_sees_across_frame_boundaries() -> None:
    """A peak straddling two frames must be found whichever way the stream is cut."""
    signal = sine(RATE / 4, -6, 0.2, math.pi / 4)
    whole = run(signal, frame=len(signal))[0].true_peak
    for size in (FRAME, 480, 97, 13):
        cut = max(f.true_peak for f in run(signal, frame=size)[3:])
        assert db(cut) == pytest.approx(-6.0, abs=0.25), size
    assert db(whole) == pytest.approx(-6.0, abs=0.35)


def test_first_frame_has_no_start_up_ringing() -> None:
    """A stream that starts at a steady level must not show a phantom overshoot from the missing history."""
    first = run(sine(RATE / 4, -6, 0.1, math.pi / 4))[0]
    assert db(first.true_peak) < -6.0 + 0.3


def test_hot_signal_far_above_full_scale_stays_finite() -> None:
    out = Analyzer().process(array.array("f", [8.0, -8.0] * 480))
    assert out.sample_peak == 8.0
    assert math.isfinite(out.true_peak) and out.true_peak >= 8.0


# ---------------------------------------------------------------------------
# The clip latch
# ---------------------------------------------------------------------------


def test_latch_trips_at_minus_one_dbtp_and_stays_until_cleared() -> None:
    latch = ClipLatch()
    assert not latch.trip(-3.0, -1.2)
    assert not latch.latched
    assert latch.trip(-3.0, -1.0)  # the ceiling itself
    assert latch.latched
    assert not latch.trip(-30.0, -29.0)
    assert latch.latched  # a quiet frame does not release it
    assert latch.count == 1
    latch.clear()
    assert not latch.latched
    assert latch.count == 0


def test_latch_trips_on_digital_full_scale_even_if_the_true_peak_is_unknown() -> None:
    latch = ClipLatch()
    assert latch.trip(0.0, -90.0)
    assert latch.latched


def test_latch_catches_an_intersample_over_the_sample_peak_misses() -> None:
    """The reason the meter reads true peak: -1.9 dBFS samples that clip the DAC."""
    latch = ClipLatch()
    for f in run(sine(RATE / 4, db(0.8 * math.sqrt(2)), 0.3, math.pi / 4)):
        latch.trip(db(f.sample_peak), db(f.true_peak))
    assert latch.latched


def test_latch_ignores_a_loud_but_clean_signal() -> None:
    latch = ClipLatch()
    for f in run(sine(997, -3.0, 0.5)):
        latch.trip(db(f.sample_peak), db(f.true_peak))
    assert not latch.latched


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def instrumented() -> bool:
    """True under a tracer or coverage (CI runs the suite with --cov), which slows pure Python loops several times."""
    if sys.gettrace() is not None:
        return True
    monitoring = getattr(sys, "monitoring", None)
    return monitoring is not None and any(monitoring.get_tool(tool) for tool in range(6))


def test_the_analyser_is_cheap_enough_to_run_per_source_in_pure_python() -> None:
    """Both sources (mic and voice) run an analyser. Speech-like and loud noise must cost far less than a core:
    12 % of real time per source (about six times what a modern CPU needs; 70 % when coverage traces every line)."""
    bound = 0.7 if instrumented() else 0.12
    rng = random.Random(3)  # noqa: S311 - a reproducible test signal
    speech = array.array("f", (rng.gauss(0, 0.05) * (0.2 + abs(math.sin(i / 9000))) for i in range(RATE * 5)))
    loud = array.array("f", (max(-1.0, min(1.0, rng.gauss(0, 0.3))) for _ in range(RATE * 5)))
    for name, signal in (("speech", speech), ("loud", loud)):
        analyzer = Analyzer()
        started = time.process_time()
        run(signal, analyzer)
        share = (time.process_time() - started) / (len(signal) / RATE)
        assert share < bound, f"{name}: {share:.1%} of real time"
