"""Speech detection on the raw microphone, and the "you are muted" episode rule.

`SpeechDetector` is StreamStack's own voice-activity detector. It is deliberately simple and fully local: it only
looks at the level of the raw microphone (the 20 ms RMS the Console's meter already computes), never at any other
audio, and it keeps nothing.

* A noise floor is tracked continuously (the 10th percentile of the last ten seconds), so a hot preamp (the Wave XLR
  idles near -43 dBFS at 75 dB gain) and a quiet one both work.
* The threshold is `max(absolute minimum, floor + margin)`; sensitivity picks both numbers.
* **Hold**: speech is declared once the level has been above the threshold for 400 ms in total within the last second.
  A keyboard clack, a door or a cough (well under 400 ms, or sparse) never gets there, while syllables with their
  short gaps do. Speech ends when less than 200 ms of the last second is above the threshold.

`MutedTalkWatcher` turns "speaking during a call while muted" into one warning per episode: it fires once, then stays
quiet until the speech has stopped and the cooldown has passed, or the person unmutes (or the call ends).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

HOLD_S = 0.4  # speech must persist this long (the issue's 400 ms hold)
WINDOW_S = 1.0  # the 400 ms of speech must fall within this long a window (syllable gaps are bridged)
FLOOR_WINDOW_S = 10.0
FLOOR_PERCENTILE = 0.10
FLOOR_EVERY_S = 0.25
FLOOR_CAP_DB = -30.0  # a loud room must not push the threshold out of reach of speech
MAX_STEP_S = 0.25  # a gap in the frames (a stalled thread) counts as at most this long
SILENCE_DB = -90.0


@dataclass(frozen=True)
class Sensitivity:
    absolute_db: float  # never trigger below this level, whatever the floor
    margin_db: float  # how far above the noise floor speech must be


SENSITIVITIES: dict[str, Sensitivity] = {
    "high": Sensitivity(-52.0, 6.0),
    "medium": Sensitivity(-46.0, 10.0),
    "low": Sensitivity(-38.0, 15.0),
}
DEFAULT_SENSITIVITY = "medium"


class SpeechDetector:
    """Frames of (time, level in dBFS) in, "speaking now" out."""

    def __init__(
        self, sensitivity: str = DEFAULT_SENSITIVITY, *, hold_s: float = HOLD_S, window_s: float = WINDOW_S
    ) -> None:
        self.hold_s, self.window_s = hold_s, window_s
        self._sens = SENSITIVITIES[sensitivity]
        self._levels: deque[tuple[float, float]] = deque()
        self._floor = SILENCE_DB
        self._floor_at = float("-inf")
        self._last: float | None = None
        self._window: deque[tuple[float, float, bool]] = deque()  # (time, duration, above the threshold)
        self._above_s = 0.0
        self.speaking = False

    def set_sensitivity(self, sensitivity: str) -> None:
        self._sens = SENSITIVITIES[sensitivity]

    @property
    def floor_db(self) -> float:
        return self._floor

    @property
    def threshold_db(self) -> float:
        return max(self._sens.absolute_db, min(self._floor, FLOOR_CAP_DB) + self._sens.margin_db)

    def reset(self) -> None:
        self._levels.clear()
        self._window.clear()
        self._floor, self._floor_at, self._last = SILENCE_DB, float("-inf"), None
        self._above_s = 0.0
        self.speaking = False

    def feed(self, now: float, level_db: float) -> bool:
        dt = 0.0 if self._last is None else max(0.0, min(now - self._last, MAX_STEP_S))
        self._last = now
        self._track_floor(now, level_db)
        above = level_db > self.threshold_db
        self._window.append((now, dt, above))
        if above:
            self._above_s += dt
        while self._window and now - self._window[0][0] > self.window_s:
            _, old_dt, old_above = self._window.popleft()
            if old_above:
                self._above_s -= old_dt
        self._above_s = max(0.0, self._above_s)
        if not self.speaking and self._above_s >= self.hold_s:
            self.speaking = True
        elif self.speaking and self._above_s < self.hold_s / 2:
            self.speaking = False
        return self.speaking

    def _track_floor(self, now: float, level_db: float) -> None:
        self._levels.append((now, level_db))
        while self._levels and now - self._levels[0][0] > FLOOR_WINDOW_S:
            self._levels.popleft()
        if now - self._floor_at < FLOOR_EVERY_S:
            return
        self._floor_at = now
        ordered = sorted(level for _, level in self._levels)
        self._floor = ordered[int(len(ordered) * FLOOR_PERCENTILE)]


class MutedTalkWatcher:
    """One warning per episode of talking while muted during a call."""

    def __init__(self, cooldown_s: float = 20.0, quiet_s: float = 3.0) -> None:
        self.cooldown_s, self.quiet_s = cooldown_s, quiet_s
        self.count = 0
        self.last_at: float | None = None
        self.talking = False  # inside an episode that has already been warned about
        self._armed = True
        self._quiet_since: float | None = None

    def update(self, now: float, *, in_call: bool, muted: bool, speaking: bool) -> bool:
        """True exactly when a warning should be shown now."""
        if not in_call or not muted:
            self._armed, self.talking, self._quiet_since = True, False, None  # unmuting or leaving ends the episode
            return False
        if speaking:
            self._quiet_since = None
            if self._armed:
                self._armed, self.talking = False, True
                self.count += 1
                self.last_at = now
                return True
            return False
        if self._quiet_since is None:
            self._quiet_since = now
        waited_enough = self.last_at is None or now - self.last_at >= self.cooldown_s
        if not self._armed and now - self._quiet_since >= self.quiet_s and waited_enough:
            self._armed, self.talking = True, False
        elif not self._armed and now - self._quiet_since >= self.quiet_s:
            self.talking = False  # the episode is over; the cooldown still holds the next warning back
        return False
