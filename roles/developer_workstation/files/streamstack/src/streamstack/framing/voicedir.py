"""Voice direction from a stereo or two-capsule microphone: the `voice_x` cue of the speaker hand-off (#144).

The framing controller already accepts a `voice_x` callable returning where the voice comes from across the picture
(0 = left edge, 1 = right edge) and the group picker uses it to break ties between people whose mouths both move.
Nothing fed it. This module is that feed, and nothing else: it reads one stereo PulseAudio/PipeWire source, keeps
no audio (only a few hundred milliseconds in memory, never written anywhere), and reports one number.

* **Time difference (ITD).** The lag at which the right channel best matches the left one, searched over the
  physically possible range for the capsule spacing and refined by a parabola through the peak. A source on the
  right reaches the right capsule first. `sin(angle) = c * itd / spacing`.
* **Level difference (ILD).** The RMS ratio between the channels, used when no spacing is configured (a coincident or
  panned stereo microphone has no usable time difference) or when the correlation is weak.
* **Calibration** against the camera: `STREAMSTACK_VOICE_DIRECTION_HFOV` (horizontal field of view in degrees, the
  angle that maps to the picture edge), `..._SPACING_MM`, `..._OFFSET` (centre trim, in picture widths) and
  `..._SWAP` for a microphone wired the other way round. Off unless `STREAMSTACK_VOICE_DIRECTION_SOURCE` names a
  stereo source.
* **Never a guess.** Silence, a weak correlation, a stale reading (no frame for a second) or a dead reader give
  `None`, which the picker reads as "no direction"; the mouth-movement cue then decides alone.

Measure without the Console: `python -m streamstack.framing.voicedir --source <stereo source> --seconds 10`.
"""

from __future__ import annotations

import argparse
import array
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

RATE = 48_000
CHANNELS = 2
SPEED_OF_SOUND = 343.0  # m/s at 20 C
WINDOW = 2048  # samples per estimate (43 ms): several pitch periods of a voice
HOP = 4800  # a new estimate every 100 ms: pure Python, so the cost is kept to a few percent of one core
DECIMATE = 2  # the correlation runs at 24 kHz (a pair average is the low-pass); a parabola restores sub-sample lag
SILENCE_DB = -90.0
MIN_LEVEL_DB = -55.0  # a window quieter than this (RMS of the louder channel) carries no direction
MIN_CORRELATION = 0.35  # normalised peak below this: the time difference is noise
ILD_FULL_DB = 10.0  # a level difference of this many dB maps to the picture edge when only levels are used
STALE_S = 1.0
SMOOTH_S = 0.35  # time constant of the exponential smoothing of x
MAX_JUMP = 0.5  # one window moving x further than this is held back to a single smoothing step (clicks, door slams)
DEFAULT_HFOV_DEG = 70.0
READ_BYTES = 1024 * CHANNELS * 4


@dataclass(frozen=True)
class Calibration:
    """How a pair of channels maps onto the camera's picture."""

    hfov_deg: float = DEFAULT_HFOV_DEG
    spacing_m: float = 0.0  # 0 means "levels only"
    offset: float = 0.0  # added to x, in picture widths, to trim a microphone that is not centred on the camera
    swap: bool = False  # the left channel is on the right

    @classmethod
    def from_config(cls, cfg: dict[str, str]) -> Calibration:
        return cls(
            hfov_deg=_number(cfg, "STREAMSTACK_VOICE_DIRECTION_HFOV", DEFAULT_HFOV_DEG, 10.0, 180.0),
            spacing_m=_number(cfg, "STREAMSTACK_VOICE_DIRECTION_SPACING_MM", 0.0, 0.0, 500.0) / 1000.0,
            offset=_number(cfg, "STREAMSTACK_VOICE_DIRECTION_OFFSET", 0.0, -0.5, 0.5),
            swap=str(cfg.get("STREAMSTACK_VOICE_DIRECTION_SWAP", "")).strip().lower() in {"1", "true", "yes", "on"},
        )


def _number(cfg: dict[str, str], key: str, default: float, low: float, high: float) -> float:
    raw = str(cfg.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", key, raw, default)
        return default
    if not math.isfinite(value) or not low <= value <= high:
        log.warning("%s=%r is outside %s..%s; using %s", key, raw, low, high, default)
        return default
    return value


@dataclass(frozen=True)
class Estimate:
    x: float  # 0 left .. 1 right, before smoothing
    itd_s: float | None  # positive when the right channel leads
    ild_db: float  # positive when the right channel is louder
    correlation: float
    level_db: float
    method: str  # "itd" or "ild"


def _rms(samples: list[float]) -> float:
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def _db(value: float) -> float:
    return 20.0 * math.log10(value) if value > 1e-9 else SILENCE_DB


def correlate(left: list[float], right: list[float], max_lag: int) -> tuple[float, float]:
    """The lag (samples, fractional) at which `right` best matches `left`, and the normalised peak (-1..1).

    A positive lag means the right channel is the later one: the source is nearer the left capsule. The lag is
    refined by a parabola through the peak and its neighbours.
    """
    n = min(len(left), len(right))
    if n <= 2 * max_lag + 1:
        raise ValueError("window shorter than the lag range")
    mean_l = sum(left[:n]) / n
    mean_r = sum(right[:n]) / n
    a = [s - mean_l for s in left[:n]]
    b = [s - mean_r for s in right[:n]]
    span = range(max_lag, n - max_lag)
    energy_a = sum(a[i] * a[i] for i in span)
    scores: list[float] = []
    for lag in range(-max_lag, max_lag + 1):
        scores.append(sum(a[i] * b[i + lag] for i in span))
    energy_b = sum(b[i] * b[i] for i in span)
    norm = math.sqrt(energy_a * energy_b)
    if norm <= 1e-12:
        return 0.0, 0.0
    best = max(range(len(scores)), key=scores.__getitem__)
    lag = float(best - max_lag)
    if 0 < best < len(scores) - 1:
        y0, y1, y2 = scores[best - 1], scores[best], scores[best + 1]
        denom = y0 - 2 * y1 + y2
        if denom < 0:
            lag += 0.5 * (y0 - y2) / denom
    return lag, max(-1.0, min(1.0, scores[best] / norm))


def estimate(left: list[float], right: list[float], cal: Calibration, rate: int = RATE) -> Estimate | None:
    """One direction estimate from one window, or None when the window carries none (silence, noise)."""
    if len(left) != len(right) or not left:
        return None
    level_l, level_r = _db(_rms(left)), _db(_rms(right))
    level = max(level_l, level_r)
    if level < MIN_LEVEL_DB:
        return None
    if cal.swap:
        left, right, level_l, level_r = right, left, level_r, level_l
    ild = level_r - level_l
    half = math.radians(cal.hfov_deg) / 2.0
    if cal.spacing_m > 0:
        slow = rate / DECIMATE
        max_lag = max(1, math.ceil(cal.spacing_m / SPEED_OF_SOUND * slow) + 1)
        try:
            lag, peak = correlate(_decimate(left), _decimate(right), max_lag)
        except ValueError:
            return None
        if peak >= MIN_CORRELATION:
            itd = -lag / slow  # right leads when the right channel is earlier, i.e. a negative lag
            sin_angle = max(-1.0, min(1.0, SPEED_OF_SOUND * itd / cal.spacing_m))
            angle = math.asin(sin_angle)
            x = 0.5 + 0.5 * math.tan(angle) / math.tan(half)  # picture x is linear in tan(angle) for a pinhole camera
            return Estimate(_clip(x + cal.offset), itd, ild, peak, level, "itd")
    x = 0.5 + 0.5 * max(-1.0, min(1.0, ild / ILD_FULL_DB))
    return Estimate(_clip(x + cal.offset), None, ild, 0.0, level, "ild")


def _decimate(samples: list[float]) -> list[float]:
    return [(samples[i] + samples[i + 1]) * 0.5 for i in range(0, len(samples) - 1, DECIMATE)]


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


class VoiceDirection:
    """Smooths windows into one reading and answers `voice_x()`: a float in 0..1 or None."""

    def __init__(self, cal: Calibration, clock: Callable[[], float] = time.monotonic, rate: int = RATE) -> None:
        self.cal = cal
        self._clock = clock
        self._rate = rate
        self._lock = threading.Lock()
        self._x: float | None = None
        self._stamp = -math.inf
        self.last: Estimate | None = None
        self.windows = 0
        self.silent = 0

    def feed(self, left: list[float], right: list[float], now: float | None = None) -> Estimate | None:
        now = self._clock() if now is None else now
        est = estimate(left, right, self.cal, self._rate)
        with self._lock:
            self.windows += 1
            if est is None:
                self.silent += 1
                return None
            dt = max(0.0, min(1.0, now - self._stamp)) if self._x is not None else 0.0
            alpha = 1.0 - math.exp(-dt / SMOOTH_S) if self._x is not None else 1.0
            target = est.x
            if self._x is not None and abs(target - self._x) > MAX_JUMP:
                alpha = min(alpha, 0.2)
            self._x = target if self._x is None or now - self._stamp > STALE_S else self._x + alpha * (target - self._x)
            self._stamp = now
            self.last = est
        return est

    def __call__(self) -> float | None:
        with self._lock:
            if self._x is None or self._clock() - self._stamp > STALE_S:
                return None
            return self._x


def decode(data: bytes) -> tuple[list[float], list[float]]:
    """De-interleave float32le stereo bytes into (left, right)."""
    samples = array.array("f")
    samples.frombytes(data[: len(data) - len(data) % 8])
    if sys.byteorder == "big":
        samples.byteswap()
    return list(samples[0::2]), list(samples[1::2])


class StereoVoiceSource:
    """A read-only `parec` child on a stereo source feeding a `VoiceDirection`. Never plays, never sets a control."""

    def __init__(
        self,
        source: str,
        direction: VoiceDirection,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.source = source
        self.direction = direction
        self._spawn = spawn or subprocess.Popen
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._pending = bytearray()
        self._left: deque[float] = deque(maxlen=WINDOW)
        self._right: deque[float] = deque(maxlen=WINDOW)
        self._since_hop = 0
        self.dead = False

    @property
    def running(self) -> bool:
        return self._proc is not None and not self.dead

    def start(self) -> bool:
        if self.running:
            return True
        self.dead = False
        self._pending.clear()
        self._left.clear()
        self._right.clear()
        self._since_hop = 0
        try:
            self._proc = self._spawn(
                [
                    "parec",
                    "--raw",
                    "--format=float32le",
                    f"--rate={RATE}",
                    f"--channels={CHANNELS}",
                    "--latency-msec=40",
                    "--client-name=StreamStack voice direction",
                    f"--device={self.source}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("voice direction: cannot start parec for %s: %s", self.source, exc)
            self.dead = True
            return False
        self._thread = threading.Thread(target=self._pump, name="voice-direction", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proc = None
        self.dead = True

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self.dead = True
            return
        try:
            fd = proc.stdout.fileno()
            while chunk := os.read(fd, READ_BYTES):
                self.feed(chunk)
        except (OSError, ValueError):
            pass  # the pipe closed: stop() or the source went away
        self.dead = True

    def feed(self, data: bytes) -> None:
        """Consume raw stereo float32le bytes; every HOP new frames evaluate one window."""
        self._pending += data
        usable = len(self._pending) - len(self._pending) % 8
        if not usable:
            return
        block, self._pending = bytes(self._pending[:usable]), self._pending[usable:]
        left, right = decode(block)
        self._left.extend(left)
        self._right.extend(right)
        self._since_hop += len(left)
        while self._since_hop >= HOP and len(self._left) == WINDOW:
            self._since_hop -= HOP
            self.direction.feed(list(self._left), list(self._right))
        if len(self._left) < WINDOW:
            self._since_hop = 0


def from_config(cfg: dict[str, str]) -> tuple[VoiceDirection, StereoVoiceSource] | None:
    """The configured direction feed, or None when `STREAMSTACK_VOICE_DIRECTION_SOURCE` is empty (the default)."""
    source = str(cfg.get("STREAMSTACK_VOICE_DIRECTION_SOURCE", "") or "").strip()
    if not source:
        return None
    direction = VoiceDirection(Calibration.from_config(cfg))
    return direction, StereoVoiceSource(source, direction)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the voice direction of a stereo source (read-only capture).")
    parser.add_argument("--source", required=True, help="a stereo PulseAudio/PipeWire source name")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--hfov", type=float, default=DEFAULT_HFOV_DEG, help="camera horizontal field of view, degrees")
    parser.add_argument("--spacing-mm", type=float, default=0.0, help="capsule spacing; 0 = level difference only")
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--swap", action="store_true")
    args = parser.parse_args(argv)
    cal = Calibration(args.hfov, args.spacing_mm / 1000.0, args.offset, args.swap)
    direction = VoiceDirection(cal)
    source = StereoVoiceSource(args.source, direction)
    if not source.start():
        print(f"cannot start parec on {args.source}", file=sys.stderr)
        return 2
    print("t_s    x      method  itd_us  ild_dB  corr   level_dB")
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds and not source.dead:
            time.sleep(0.25)
            last, x = direction.last, direction()
            if last is not None and x is not None:
                itd = f"{last.itd_s * 1e6:7.0f}" if last.itd_s is not None else "      -"
                print(
                    f"{time.monotonic() - t0:5.1f}  {x:.2f}   {last.method:6s} {itd}  {last.ild_db:6.1f}  "
                    f"{last.correlation:5.2f}  {last.level_db:6.1f}"
                )
            else:
                print(f"{time.monotonic() - t0:5.1f}  -      (no direction: silence or weak signal)")
    finally:
        source.stop()
    if source.dead and direction.windows == 0:
        print("no audio arrived from the source (wrong name, or the source is not stereo)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
