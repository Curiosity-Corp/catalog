"""The speech detector against a recorded room (the dm313red raw mic, nobody talking) and synthetic speech."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from streamstack.console.audio import speech_peak_db
from streamstack.speech import SENSITIVITIES, MutedTalkWatcher, SpeechDetector

FIXTURE = Path(__file__).parent / "fixtures" / "call" / "room_dm313red.json"
FRAME_S = 0.02


def room() -> list[float]:
    return json.loads(FIXTURE.read_text())["levels"]


def power_sum(a: float, b: float) -> float:
    return 10 * math.log10(10 ** (a / 10) + 10 ** (b / 10))


def run(levels: list[float], sensitivity: str = "medium") -> tuple[list[bool], SpeechDetector]:
    det = SpeechDetector(sensitivity)
    return [det.feed(i * FRAME_S, level) for i, level in enumerate(levels)], det


def speech_over(noise: list[float], start_s: float, seconds: float, loud_db: float = -22.0) -> list[float]:
    """The recorded room with speech-like syllables mixed in from `start_s` for `seconds`."""
    out = list(noise)
    for i in range(int(start_s / FRAME_S), min(len(out), int((start_s + seconds) / FRAME_S))):
        peak = speech_peak_db(i * FRAME_S)  # the demo's deterministic syllables, phrases and pauses
        if peak is not None:
            out[i] = power_sum(noise[i], peak - 6 + (loud_db + 22))
    return out


def test_the_recorded_room_never_counts_as_speech() -> None:
    levels = room()
    assert len(levels) > 800
    for sensitivity in ("medium", "low"):
        flags, _ = run(levels, sensitivity)
        assert not any(flags), f"room noise raised speech at {sensitivity} sensitivity"


def test_the_recorded_room_at_high_sensitivity_is_still_not_speech() -> None:
    flags, det = run(room(), "high")
    assert not any(flags)
    assert det.floor_db < -35


def test_speech_over_the_room_is_heard_within_the_hold() -> None:
    noise = room()
    levels = speech_over(noise + noise, 4.0, 10.0)
    flags, _ = run(levels)
    first = flags.index(True)
    assert first * FRAME_S > 4.0
    assert first * FRAME_S < 4.0 + 3.0  # the next phrase starts at 4.5 s; the 400 ms hold follows
    assert any(flags[int(8 / FRAME_S) : int(10 / FRAME_S)])


def test_speech_ends_after_the_release() -> None:
    noise = room() * 2
    levels = speech_over(noise, 3.0, 6.0)
    flags, _ = run(levels)
    assert not flags[-1]
    last = len(flags) - 1 - flags[::-1].index(True)
    assert last * FRAME_S < 3.0 + 6.0 + 1.5


def test_a_short_burst_under_400_ms_is_not_speech() -> None:
    noise = room()
    for burst_ms in (100, 200, 300):
        levels = list(noise)
        for i in range(int(5 / FRAME_S), int(5 / FRAME_S) + burst_ms // 20):
            levels[i] = -15.0
        flags, _ = run(levels)
        assert not any(flags), f"a {burst_ms} ms burst was taken for speech"


def test_keyboard_clacks_are_not_speech() -> None:
    rng = random.Random(7)  # noqa: S311 - a deterministic fixture
    levels = list(room())
    for t in range(0, int(len(levels) * FRAME_S * 6)):  # about six clacks a second, 30 ms each
        i = int(t / 6 / FRAME_S)
        for k in range(2):
            if i + k < len(levels):
                levels[i + k] = -20.0 + rng.random() * 4
    flags, _ = run(levels)
    assert not any(flags)


def test_a_sustained_loud_tone_is_speech_after_400_ms() -> None:
    levels = [-60.0] * 100 + [-20.0] * 100 + [-60.0] * 100
    flags, _ = run(levels)
    onset = flags.index(True) * FRAME_S
    assert 100 * FRAME_S + 0.38 <= onset <= 100 * FRAME_S + 0.5


def test_silence_and_a_muted_source_are_not_speech() -> None:
    assert not any(run([-90.0] * 500)[0])


def test_sensitivity_changes_the_threshold() -> None:
    det = SpeechDetector("medium")
    for i in range(400):
        det.feed(i * FRAME_S, -50.0)
    medium = det.threshold_db
    det.set_sensitivity("low")
    assert det.threshold_db > medium
    det.set_sensitivity("high")
    assert det.threshold_db < medium


def test_a_loud_room_cannot_push_the_threshold_past_speech() -> None:
    det = SpeechDetector("low")
    for i in range(1000):
        det.feed(i * FRAME_S, -10.0)  # a floor far above any speech
    assert det.threshold_db <= -30.0 + SENSITIVITIES["low"].margin_db


def test_a_stalled_clock_does_not_count_as_voiced_time() -> None:
    det = SpeechDetector()
    det.feed(0.0, -60.0)
    det.feed(10.0, -20.0)  # ten seconds of nothing, then one frame
    assert not det.speaking


def test_reset_forgets_everything() -> None:
    det = SpeechDetector()
    for i in range(100):
        det.feed(i * FRAME_S, -60.0)
    for i in range(100, 200):
        det.feed(i * FRAME_S, -20.0)
    assert det.speaking
    det.reset()
    assert not det.speaking
    assert det.floor_db == -90.0


class TestMutedTalkWatcher:
    def talk(self, w: MutedTalkWatcher, start: float, seconds: float, **kw: bool) -> list[float]:
        fired = []
        t = start
        while t < start + seconds:
            if w.update(t, speaking=True, **{"in_call": True, "muted": True, **kw}):
                fired.append(t)
            t += 0.1
        return fired

    def quiet(self, w: MutedTalkWatcher, start: float, seconds: float) -> float:
        t = start
        while t < start + seconds:
            assert not w.update(t, in_call=True, muted=True, speaking=False)
            t += 0.1
        return t

    def test_one_warning_per_episode(self) -> None:
        w = MutedTalkWatcher(cooldown_s=20)
        assert len(self.talk(w, 0, 30)) == 1
        assert w.talking and w.count == 1

    def test_a_pause_inside_the_cooldown_does_not_warn_again(self) -> None:
        w = MutedTalkWatcher(cooldown_s=20)
        self.talk(w, 0, 5)
        t = self.quiet(w, 5, 4)  # a four second pause
        assert self.talk(w, t, 5) == []  # 9 s in: still inside the 20 s cooldown
        assert w.count == 1

    def test_a_new_episode_after_the_cooldown_warns_again(self) -> None:
        w = MutedTalkWatcher(cooldown_s=20)
        self.talk(w, 0, 5)
        t = self.quiet(w, 5, 25)
        assert len(self.talk(w, t, 5)) == 1
        assert w.count == 2

    def test_unmuting_ends_the_episode_at_once(self) -> None:
        w = MutedTalkWatcher(cooldown_s=20)
        self.talk(w, 0, 3)
        assert not w.update(3.0, in_call=True, muted=False, speaking=True)
        assert len(self.talk(w, 4, 2)) == 1  # muted again a second later: a fresh episode
        assert w.count == 2

    def test_nothing_fires_outside_a_call_or_unmuted(self) -> None:
        w = MutedTalkWatcher()
        assert self.talk(w, 0, 5, in_call=False) == []
        assert self.talk(w, 5, 5, muted=False) == []
        assert w.count == 0

    def test_quiet_never_fires(self) -> None:
        w = MutedTalkWatcher()
        self.quiet(w, 0, 30)
        assert w.count == 0 and not w.talking


@pytest.mark.parametrize("name", sorted(SENSITIVITIES))
def test_every_sensitivity_builds(name: str) -> None:
    assert SpeechDetector(name).threshold_db <= -30.0
