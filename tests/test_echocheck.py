"""The echo DSP: a simulated loopback of known coupling and delay is measured back, a quiet room is not bleed."""

from __future__ import annotations

import pytest

from streamstack import echocheck

PLAYED = echocheck.PLAY_LEVEL_DB


def record(coupling_db: float | None, *, delay_ms: float = 30.0, loop_gap_ms: float | None = None) -> list[float]:
    """A recording of the sweep played at PLAY_LEVEL_DB: the simulation is at full scale, so it is scaled as played."""
    scale = 10 ** (PLAYED / 20)
    samples = echocheck.simulate(
        coupling_db=coupling_db, delay_ms=delay_ms, loop_gap_ms=loop_gap_ms, noise_db=-70.0 - PLAYED
    )
    return [v * scale for v in samples]


@pytest.mark.parametrize("coupling", [-12.0, -26.0, -34.0, -45.0, -55.0])
def test_measures_the_coupling_within_three_db(coupling: float) -> None:
    result = echocheck.analyze(record(coupling, delay_ms=40.0), echocheck.RATE, lead_s=0.3, play_db=PLAYED)
    assert result.heard
    assert result.coupling_db == pytest.approx(coupling, abs=3.0)
    assert result.delay_ms == pytest.approx(40.0, abs=5.0)
    assert not result.loop


@pytest.mark.parametrize(("coupling", "level"), [(-30.0, "strong"), (-45.0, "moderate"), (-55.0, "slight")])
def test_levels(coupling: float, level: str) -> None:
    result = echocheck.analyze(record(coupling), echocheck.RATE, lead_s=0.3, play_db=PLAYED)
    assert result.level == level


def test_a_quiet_room_is_no_bleed() -> None:
    result = echocheck.analyze(record(None), echocheck.RATE, lead_s=0.3, play_db=PLAYED)
    assert not result.heard
    assert result.coupling_db is None
    assert result.level == "none"
    assert "No bleed" in echocheck.describe(result, "Desk speakers")


def test_a_feedback_loop_is_found() -> None:
    result = echocheck.analyze(record(-12.0, loop_gap_ms=150.0), echocheck.RATE, lead_s=0.3, play_db=PLAYED)
    assert result.loop
    assert result.level == "feedback"
    assert result.loop_gap_ms == pytest.approx(150.0, abs=5.0)
    assert "feedback loop" in echocheck.describe(result, "Monitor mix")


def test_a_recording_at_another_rate_is_measured() -> None:
    samples = record(-34.0)
    upsampled = [v for v in samples for _ in range(6)]  # 48 kHz
    result = echocheck.analyze(upsampled, 48000, lead_s=0.3, play_db=PLAYED)
    assert result.coupling_db == pytest.approx(-34.0, abs=3.0)


def test_a_recording_too_short_is_refused() -> None:
    with pytest.raises(ValueError, match="too short"):
        echocheck.analyze([0.0] * 800, echocheck.RATE)


def test_the_fft_round_trips() -> None:
    data = [complex(i % 7, 0) for i in range(64)]
    back = echocheck.fft(echocheck.fft(data), inverse=True)
    assert [round(v.real, 6) for v in back] == [v.real for v in data]
