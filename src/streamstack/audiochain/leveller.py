"""The loudness leveller: slow gain riding of the chain output towards a target short-term loudness.

One owner of gain. The leveller adds ``auto_db`` on top of the makeup gain (the ``gain`` node of the chain); it never
touches the microphone's own gain or the interface's 48 V. It listens to the loudness of the chain output (BS.1770
momentary, from `console.loudness`) and:

* measures only while someone is speaking: output below the speech gate is silence, and silence or a muted
  microphone freezes the gain exactly where it is (a noise floor is never pulled up);
* smooths the speech level over a couple of seconds (time constant `SMOOTH_S`), counting only the frames that are
  speech: a frame more than `REL_GATE_DB` below the running level is a pause between words and is left out (as the
  relative gate of BS.1770 does), and the gate falls away when the voice itself gets quieter. It smooths the level
  the voice has *before* the leveller's own gain (the measured output minus `auto_db`), so the gain it adds does not
  feed back into the average and the loop cannot overshoot; it moves the gain towards the target at a rate
  proportional to the error, at most ``MAX_RATE_DB_S``; within ``SLOW_ZONE_DB`` of the target it moves at most
  ``FINE_RATE_DB_S`` (issue #38's 0.5 dB/s), and inside the dead band it does not move at all;
* keeps ``auto_db`` inside +-``LEVEL_WINDOW_DB`` of the makeup gain so it can never run away;
* a true-peak limiter (the last stage of the chain, -1 dBTP) stays behind it, so the gain it adds cannot clip.

`Leveller.update` is pure (no clock, no I/O): the caller passes the time step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import stages

SPEECH_GATE_LUFS = -50.0  # chain output momentary loudness below this is silence
SMOOTH_S = 1.5  # time constant of the loudness average
REL_GATE_DB = 8.0  # frames this far below the running speech level are pauses, not speech
GATE_HOLD_S = 0.3  # how long frames must stay below the gate before the voice is taken to have got quieter
GATE_FALL_DB_S = 40.0  # then the gate drops this fast until frames pass it again
DEAD_BAND_DB = 0.5
GAIN_PER_ERROR = 0.7  # dB/s of gain move per dB of error
MAX_RATE_DB_S = 4.0
SLOW_ZONE_DB = 2.0
FINE_RATE_DB_S = 0.5
# The chain output is measured after the limiter, and a limiter that is working hard hides how loud the voice
# really is: a voice 20 dB louder than the leveller expected reads about the same loudness, because the limiter
# flattens it. So the leveller also looks at the raw microphone (before the chain): it never lets the gain go to
# where the microphone's own loudness plus the chain's static gain plus the leveller's gain would sit more than
# GUARD_LU above the target, and when the voice gets louder it comes down at the full rate.
GUARD_LU = 10.0
GUARD_SLACK_DB = 0.25
CAPPED_ERROR_DB = -6.0

STATES = ("off", "riding", "holding", "silence", "muted", "no-signal", "at-limit", "capped")


class SpeechLevel:
    """The smoothed loudness of the speech in a stream of momentary readings, with a relative gate for the pauses."""

    def __init__(self) -> None:
        self.value: float | None = None
        self._quiet_s = 0.0
        self._drop = 0.0

    def update(self, dt: float, reading: float) -> float:
        if self.value is None:
            self.value = reading
            return reading
        if reading >= self.value - REL_GATE_DB - self._drop:
            self.value += (reading - self.value) * (1 - math.exp(-dt / SMOOTH_S))
            self._quiet_s = 0.0
            self._drop = 0.0
        else:
            self._quiet_s += dt
            if self._quiet_s > GATE_HOLD_S:
                self._drop += GATE_FALL_DB_S * dt
                if reading >= self.value - REL_GATE_DB - self._drop:  # the voice is quieter now: follow it at once
                    self.value = reading
                    self._quiet_s = 0.0
                    self._drop = 0.0
        return self.value


@dataclass
class LevelStatus:
    state: str
    auto_db: float
    level_lufs: float | None  # the smoothed speech loudness the leveller steers on


class Leveller:
    def __init__(
        self, target_lufs: float = stages.DEFAULT_TARGET_LUFS, window_db: float = stages.LEVEL_WINDOW_DB
    ) -> None:
        self.target = target_lufs
        self.window = window_db
        self.auto_db = 0.0
        self._speech = SpeechLevel()  # speech loudness before the leveller's gain
        self._heard = SpeechLevel()  # speech loudness of the raw microphone
        self.state = "off"

    def set_target(self, target_lufs: float) -> None:
        self.target = target_lufs

    def reset(self, auto_db: float = 0.0) -> None:
        self.auto_db = max(-self.window, min(self.window, auto_db))
        self._speech = SpeechLevel()
        self._heard = SpeechLevel()
        self.state = "off"

    @property
    def level(self) -> float | None:
        """The smoothed loudness of the chain output as the leveller sees it (None before the first speech)."""
        value = self._speech.value
        return None if value is None else value + self.auto_db

    def status(self) -> LevelStatus:
        level = self.level
        return LevelStatus(self.state, round(self.auto_db, 2), None if level is None else round(level, 1))

    def cap(self, static_db: float) -> float | None:
        """The highest gain the raw microphone level allows (None until the microphone has been heard speaking)."""
        if self._heard.value is None:
            return None
        return self.target + GUARD_LU - self._heard.value - static_db

    def update(
        self,
        dt: float,
        lufs_m: float | None,
        *,
        muted: bool = False,
        enabled: bool = True,
        mic_lufs: float | None = None,
        static_db: float = 0.0,
    ) -> float:
        """Advance by `dt` seconds with the latest momentary loudness of the chain output (None: no signal), and
        optionally of the raw microphone with the chain's own gain `static_db` (makeup). Returns the new auto gain."""
        if not enabled:
            self.state = "off"
            self._speech = SpeechLevel()
            self._heard = SpeechLevel()
            return self.auto_db
        if muted:
            self.state = "muted"
            return self.auto_db
        if lufs_m is None or not math.isfinite(lufs_m):
            self.state = "no-signal"
            return self.auto_db
        if lufs_m < SPEECH_GATE_LUFS:
            self.state = "silence"
            return self.auto_db
        level = self._speech.update(dt, lufs_m - self.auto_db)
        if mic_lufs is not None and math.isfinite(mic_lufs) and mic_lufs >= SPEECH_GATE_LUFS:
            self._heard.update(dt, mic_lufs)
        ceiling = self.cap(static_db)
        upper = self.window if ceiling is None else min(self.window, max(-self.window, ceiling))
        error = self.target - (level + self.auto_db)
        capped = ceiling is not None and self.auto_db > ceiling + GUARD_SLACK_DB and self.auto_db > -self.window
        if capped:
            error = min(error, CAPPED_ERROR_DB)
        if abs(error) <= DEAD_BAND_DB:
            self.state = "holding"
            return self.auto_db
        rate_cap = FINE_RATE_DB_S if abs(error) <= SLOW_ZONE_DB else MAX_RATE_DB_S
        rate = max(-rate_cap, min(rate_cap, GAIN_PER_ERROR * error))
        wanted = self.auto_db + rate * dt
        if rate > 0:
            wanted = min(wanted, max(upper, self.auto_db))  # never climb past what the microphone allows
        self.auto_db = max(-self.window, min(self.window, wanted))
        if capped or (rate > 0 and self.auto_db >= upper and upper < self.window):
            self.state = "capped"
        elif self.auto_db in (-self.window, self.window):
            self.state = "at-limit"
        else:
            self.state = "riding"
        return self.auto_db
