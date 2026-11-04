"""Voice direction from a stereo microphone (#144), on synthetic two-channel signals with known delays and levels."""

from __future__ import annotations

import math
import random
import struct
from pathlib import Path

import pytest

from streamstack import config
from streamstack.console import framing as console_framing
from streamstack.framing import health, voicedir
from streamstack.framing.voicedir import Calibration, StereoVoiceSource, VoiceDirection

RATE = voicedir.RATE
SPACING = 0.12  # a 12 cm pair
CAL = Calibration(hfov_deg=70.0, spacing_m=SPACING)


def voice(n: int, seed: int = 1, level: float = 0.1) -> list[float]:
    """A voiced-speech-like signal: harmonics of a 140 Hz pitch with a random phase each, plus a little noise."""
    rng = random.Random(seed)  # noqa: S311 - a test signal
    phases = [rng.uniform(0, 2 * math.pi) for _ in range(20)]
    out = []
    for i in range(n):
        t = i / RATE
        out.append(sum(math.sin(2 * math.pi * 140 * (k + 1) * t + phases[k]) / (k + 1) for k in range(20)))
    peak = max(abs(v) for v in out)
    return [level * v / peak + rng.uniform(-1e-4, 1e-4) for v in out]


def stereo(angle_deg: float, spacing: float = SPACING, level: float = 0.1, ild_db: float = 0.0, n: int = 4096):
    """Left and right windows for a source at `angle_deg` (positive = right of the camera axis)."""
    base = voice(n + 200)
    delay = spacing * math.sin(math.radians(angle_deg)) / voicedir.SPEED_OF_SOUND * RATE  # samples the left lags by

    # the left channel hears the sound `delay` samples later when the source is on the right
    def at(offset: float) -> list[float]:
        out = []
        for i in range(n):
            p = 100 + i - offset
            j = math.floor(p)
            f = p - j
            out.append(base[j] * (1 - f) + base[j + 1] * f)
        return out

    left, right = at(delay), at(0.0)
    gain = 10 ** (ild_db / 20)
    return [v / math.sqrt(gain) for v in left], [v * math.sqrt(gain) for v in right]


def test_a_source_on_the_right_reads_right_and_on_the_left_reads_left() -> None:
    right = voicedir.estimate(*stereo(30), CAL)
    left = voicedir.estimate(*stereo(-30), CAL)
    centre = voicedir.estimate(*stereo(0), CAL)
    assert right is not None and left is not None and centre is not None
    assert right.method == "itd" and right.itd_s is not None and right.itd_s > 0
    assert right.x > 0.65 and left.x < 0.35 and abs(centre.x - 0.5) < 0.04


def test_the_angle_maps_onto_the_picture_by_the_field_of_view() -> None:
    # an edge of a 70 degree picture is at 35 degrees: x should be near 1 there and near 0.5 + 0.5*tan(17)/tan(35)
    edge = voicedir.estimate(*stereo(35), CAL)
    half = voicedir.estimate(*stereo(17.5), CAL)
    assert edge is not None and half is not None
    assert edge.x == pytest.approx(1.0, abs=0.08)
    expected = 0.5 + 0.5 * math.tan(math.radians(17.5)) / math.tan(math.radians(35))
    assert half.x == pytest.approx(expected, abs=0.06)
    wide = voicedir.estimate(*stereo(35), Calibration(hfov_deg=120.0, spacing_m=SPACING))
    assert wide is not None and wide.x < edge.x  # the same angle is closer to the middle of a wider picture


def test_swap_and_offset_calibrate_a_microphone_that_is_wired_or_placed_differently() -> None:
    base = voicedir.estimate(*stereo(30), CAL)
    swapped = voicedir.estimate(*stereo(30), Calibration(70.0, SPACING, swap=True))
    shifted = voicedir.estimate(*stereo(30), Calibration(70.0, SPACING, offset=-0.1))
    assert base is not None and swapped is not None and shifted is not None
    assert swapped.x < 0.35 and swapped.x == pytest.approx(1.0 - base.x, abs=0.05)
    assert shifted.x == pytest.approx(base.x - 0.1, abs=0.01)


def test_without_a_spacing_only_the_level_difference_is_used() -> None:
    cal = Calibration(hfov_deg=70.0)
    right = voicedir.estimate(*stereo(0, ild_db=6.0), cal)
    left = voicedir.estimate(*stereo(0, ild_db=-6.0), cal)
    assert right is not None and left is not None and right.method == "ild" and right.itd_s is None
    assert right.x == pytest.approx(0.5 + 0.5 * 0.6, abs=0.02)
    assert left.x == pytest.approx(0.5 - 0.5 * 0.6, abs=0.02)


def test_uncorrelated_channels_fall_back_to_levels_not_to_a_made_up_lag() -> None:
    rng = random.Random(7)  # noqa: S311 - a test signal
    a, b = [rng.uniform(-0.1, 0.1) for _ in range(4096)], [rng.uniform(-0.1, 0.1) for _ in range(4096)]
    est = voicedir.estimate(a, b, CAL)
    assert est is not None and est.method == "ild" and abs(est.x - 0.5) < 0.1


def test_silence_and_a_hiss_floor_give_no_direction() -> None:
    zeros = [0.0] * 4096
    assert voicedir.estimate(zeros, zeros, CAL) is None
    hiss = [0.0005 * math.sin(i) for i in range(4096)]
    assert voicedir.estimate(hiss, hiss, CAL) is None
    assert voicedir.estimate([], [], CAL) is None
    assert voicedir.estimate([0.1], [0.1, 0.2], CAL) is None


def test_correlate_rejects_a_window_shorter_than_the_lag_range() -> None:
    with pytest.raises(ValueError, match="shorter"):
        voicedir.correlate([0.1] * 10, [0.1] * 10, 8)


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_reading_is_smoothed_held_briefly_and_then_goes_stale() -> None:
    clock = Clock()
    d = VoiceDirection(CAL, clock=clock)
    assert d() is None  # nothing heard yet
    d.feed(*stereo(30), now=clock.now)
    first = d()
    assert first is not None and first > 0.65
    clock.now += 0.1
    d.feed(*stereo(-30), now=clock.now)  # a sudden jump to the far side moves x only part of the way
    second = d()
    assert second is not None and first > second > 0.35
    clock.now += 0.1
    assert d.feed([0.0] * 4096, [0.0] * 4096, now=clock.now) is None  # silence keeps the last reading for a moment
    assert d() == second
    clock.now += voicedir.STALE_S + 0.1
    assert d() is None  # then it is gone: the mouth-movement cue decides alone
    clock.now += 5
    d.feed(*stereo(-30), now=clock.now)
    assert d() is not None and d() < 0.35  # after a long gap the new reading starts fresh, with no smoothing lag


def interleave(left: list[float], right: list[float]) -> bytes:
    flat = [v for pair in zip(left, right, strict=True) for v in pair]
    return struct.pack(f"<{len(flat)}f", *flat)


class FakeProc:
    def __init__(self) -> None:
        self.terminated = False
        self.stdout = None

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True


def test_the_reader_turns_interleaved_bytes_into_windows_in_any_chunking() -> None:
    clock = Clock()
    d = VoiceDirection(CAL, clock=clock)
    source = StereoVoiceSource("stereo-mic", d)
    left, right = stereo(30, n=voicedir.HOP * 3)
    data = interleave(left, right)
    for i in range(0, len(data), 1001):  # chunks that split frames and samples
        source.feed(data[i : i + 1001])
    assert d.windows >= 2
    x = d()
    assert x is not None and x > 0.65


def test_the_reader_starts_parec_read_only_on_a_stereo_source_and_stops_it() -> None:
    commands: list[list[str]] = []
    proc = FakeProc()

    def spawn(cmd: list[str], **kwargs: object) -> FakeProc:
        commands.append(cmd)
        return proc

    source = StereoVoiceSource("alsa_input.stereo", VoiceDirection(CAL), spawn=spawn)
    assert source.start() is True
    cmd = commands[0]
    assert cmd[0] == "parec" and "--channels=2" in cmd and "--device=alsa_input.stereo" in cmd
    assert not any(part.startswith(("--volume", "--property")) for part in cmd)  # nothing is set on the source
    source.stop()
    assert proc.terminated and not source.running


def test_a_parec_that_cannot_start_marks_the_source_dead() -> None:
    def spawn(cmd: list[str], **kwargs: object) -> FakeProc:
        raise FileNotFoundError("parec")

    source = StereoVoiceSource("x", VoiceDirection(CAL), spawn=spawn)
    assert source.start() is False and source.dead and not source.running


def test_calibration_reads_the_settings_and_ignores_bad_values(caplog: pytest.LogCaptureFixture) -> None:
    cal = Calibration.from_config(
        {
            "STREAMSTACK_VOICE_DIRECTION_HFOV": "84",
            "STREAMSTACK_VOICE_DIRECTION_SPACING_MM": "150",
            "STREAMSTACK_VOICE_DIRECTION_OFFSET": "0.05",
            "STREAMSTACK_VOICE_DIRECTION_SWAP": "yes",
        }
    )
    assert cal == Calibration(84.0, 0.15, 0.05, True)
    bad = Calibration.from_config(
        {"STREAMSTACK_VOICE_DIRECTION_HFOV": "wide", "STREAMSTACK_VOICE_DIRECTION_SPACING_MM": "9000"}
    )
    assert bad == Calibration()
    assert "not a number" in caplog.text and "outside" in caplog.text


def test_nothing_is_configured_by_default() -> None:
    assert voicedir.from_config(dict(config.DEFAULTS)) is None
    pair = voicedir.from_config({"STREAMSTACK_VOICE_DIRECTION_SOURCE": "mic.stereo"})
    assert pair is not None and pair[1].source == "mic.stereo"


def test_the_cli_reports_a_source_it_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("parec")

    monkeypatch.setattr(voicedir.subprocess, "Popen", refuse)
    assert voicedir.main(["--source", "nope", "--seconds", "0.1"]) == 2
    assert "cannot start parec" in capsys.readouterr().err


# -- the framing controller -------------------------------------------------------------------------------------


class RecordingSource:
    def __init__(self) -> None:
        self.source = "mic.stereo"
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self) -> bool:
        self.running = True
        self.starts += 1
        return True

    def stop(self) -> None:
        self.running = False
        self.stops += 1


def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **cfg: str):
    monkeypatch.setattr(health, "readiness", lambda: (True, "", ""))
    settings = {**config.DEFAULTS, **cfg}
    return console_framing.FramingController(
        settings, object(), opener=lambda device: None, state_file=tmp_path / "framing.json"
    )


def test_the_stereo_microphone_is_read_only_while_framing_follows_the_speaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctl = controller(tmp_path, monkeypatch, STREAMSTACK_VOICE_DIRECTION_SOURCE="mic.stereo")
    assert ctl.voice_x is not None and ctl._voice is not None
    rec = RecordingSource()
    ctl._voice = (ctl._voice[0], rec)
    ctl.set_handoff(True)
    assert rec.running is False  # framing is off: no reason to listen
    ctl.set_mode("follow")
    assert rec.running is True
    ctl.set_handoff(False)
    assert rec.running is False
    ctl.set_handoff(True)
    assert rec.running is True
    ctl.set_mode("off")
    assert rec.running is False
    ctl.set_mode("follow")
    ctl.stop()
    assert rec.running is False


def test_an_unconfigured_controller_has_no_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctl = controller(tmp_path, monkeypatch)
    assert ctl.voice_x is None and ctl._voice is None
    ctl.set_handoff(True)  # nothing to start, nothing to fail
    assert ctl._voice_direction() is None
