"""Eye-line coach: where the head points relative to the lens, from the five landmarks the face detector already gives.

Pure Python with an injected time: no detector, no camera, no device, nothing stored. The coach does not correct
gaze (that needs a generative model, see docs/auto-framing.md); it tells a person that the camera is placed so that
looking at the call window reads as looking away, and how far.

Head pose from landmarks: a small rigid model of a face (eyes, nose tip, mouth corners, in millimetres) is projected
with a weak-perspective camera. The tilt of the eye line gives the roll; the nose tip's sideways offset from the eye
midpoint, in eye distances, gives the yaw; the nose tip's drop below the eye line against the mouth's drop gives the
pitch (the ratio cancels the unknown distance to the camera). Angles are in degrees; **pitch is positive when the head
points down**, yaw positive when it points to the person's left (the right of the picture).

Head pose is not iris gaze. It is the pose the head holds while looking at something, which is what separates three
habits on a call: at the lens, at the screen (the camera sits above or beside the call window) and down at the keyboard.
A one-time calibration (look at the lens) removes the person's own face shape from the numbers.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

# A rigid face, millimetres: x right, y down, z toward the camera, origin between the eyes.
EYE_HALF = 32.0  # half the eye distance
NOSE_DROP, NOSE_DEPTH = 45.0, 28.0  # nose tip below the eye line and in front of the eye plane
MOUTH_DROP, MOUTH_DEPTH = 65.0, -5.0  # mouth corners (their middle)

MIN_EYE_DISTANCE = 0.02  # of the picture height: a face this small has landmarks too noisy to read
MAX_ROLL = 50.0  # degrees: a head lying on its side is not looking at anything

LENS_PITCH = 6.0  # looking at the lens: within this many degrees of the calibrated pose
LENS_YAW = 10.0
DOWN_PITCH = 28.0  # at or beyond this the head points at the keyboard, not the screen
AWAY_YAW = 25.0  # turned this far to a side
AWAY_UP = -20.0  # or this far up

GAZES = ("lens", "screen", "down", "away")
IDEAL_EYE_LINE = 1 / 3  # the eye line sits on the top third of the picture

WINDOW = 30.0  # seconds of poses the advice is read from
MIN_SAMPLES = 12  # poses before the coach says anything
ADVICE_MIN = 8.0  # degrees of offset worth mentioning
SCREEN_SHARE = 0.4  # the person looks at the screen at least this much of the time for the offset to mean anything
AWAY_RESET = 10.0  # seconds back at the lens or screen that end a "looking away" episode
REMINDER_MINUTES = (1, 30)
REMINDER_DEFAULT = 3
REMINDER_COOLDOWN = 600.0  # seconds before the same reminder can come again
CALIBRATE_SAMPLES = 10
SMOOTH = 5  # poses the gaze is the median of: landmark jitter of a pixel or two would otherwise flip it


@dataclass(frozen=True)
class Pose:
    yaw: float
    pitch: float
    roll: float
    eye_line: float  # the eyes' y, in picture heights from the top
    eye_distance: float  # in picture heights


def estimate(landmarks: Sequence[float] | None) -> Pose | None:
    """The head pose from ten landmark numbers (right eye, left eye, nose, right mouth, left mouth as x, y), or None."""
    if landmarks is None or len(landmarks) != 10 or not all(math.isfinite(v) for v in landmarks):
        return None
    rex, rey, lex, ley = landmarks[:4]
    distance = math.hypot(lex - rex, ley - rey)
    if distance < MIN_EYE_DISTANCE:
        return None
    observed = math.degrees(math.atan2(ley - rey, lex - rex))
    if abs(observed) > MAX_ROLL:
        return None
    my = (rey + ley) / 2
    # A head that is both turned and tipped shows a tilted eye line without any roll. Two starting guesses (no roll,
    # and all of the tilt is roll) are each refined a few rounds; the one the model reproduces best wins.
    fits = [fit for fit in (_solve(landmarks, start) for start in (0.0, observed)) if fit is not None]
    if not fits:
        return None
    yaw, pitch, roll = min(fits, key=lambda fit: _residual(landmarks, fit, distance))
    return Pose(yaw, pitch, roll, my, distance)


def _solve(landmarks: Sequence[float], start: float) -> tuple[float, float, float] | None:
    """Yaw, pitch and roll from a starting roll: rotate upright, read yaw and pitch, take the predicted tilt out."""
    rex, rey, lex, ley, nx, ny, mrx, mry, mlx, mly = landmarks
    observed = math.degrees(math.atan2(ley - rey, lex - rex))
    mx, my = (rex + lex) / 2, (rey + ley) / 2
    mouth_mid = ((mrx + mlx) / 2, (mry + mly) / 2)
    roll, yaw, pitch = start, 0.0, 0.0
    for _ in range(8):
        cos, sin = math.cos(math.radians(-roll)), math.sin(math.radians(-roll))

        def upright(x: float, y: float, cos: float = cos, sin: float = sin) -> tuple[float, float]:
            dx, dy = x - mx, y - my
            return dx * cos - dy * sin, dx * sin + dy * cos

        eye_span = upright(lex, ley)[0] - upright(rex, rey)[0]
        nose_x, nose_y = upright(nx, ny)
        _, mouth_y = upright(*mouth_mid)
        if eye_span <= 0 or mouth_y <= 0 or nose_y <= 0:  # the mouth and the nose sit below the eyes
            return None
        yaw = math.degrees(math.atan2(nose_x / eye_span * (2 * EYE_HALF), NOSE_DEPTH))
        ratio = nose_y / mouth_y
        depth = math.cos(math.radians(yaw))  # a turned head shows less of the nose's depth
        pitch = math.degrees(math.atan2(ratio * MOUTH_DROP - NOSE_DROP, depth * (NOSE_DEPTH - ratio * MOUTH_DEPTH)))
        sy, sp = math.sin(math.radians(yaw)), math.sin(math.radians(pitch))
        roll = observed - math.degrees(math.atan2(-sy * sp, math.cos(math.radians(yaw))))
    return yaw, pitch, roll


def _residual(landmarks: Sequence[float], fit: tuple[float, float, float], distance: float) -> float:
    """How far the model's landmarks for this pose are from the detector's (sum of squares, picture heights)."""
    yaw, pitch, roll = fit
    mx, my = (landmarks[0] + landmarks[2]) / 2, (landmarks[1] + landmarks[3]) / 2
    sy, sp = math.sin(math.radians(yaw)), math.sin(math.radians(pitch))
    span = math.sqrt(math.cos(math.radians(yaw)) ** 2 + (sy * sp) ** 2)
    scale = distance / (2 * EYE_HALF * max(span, 1e-3))
    model = project(yaw, pitch, roll, scale=scale, centre=(mx, my))
    return sum((a - b) ** 2 for a, b in zip(model, landmarks, strict=True))


def project(
    yaw: float,
    pitch: float,
    roll: float = 0.0,
    *,
    scale: float = 0.0016,
    centre: tuple[float, float] = (0.8, 0.4),
    nose: tuple[float, float] = (NOSE_DROP, NOSE_DEPTH),
    mouth: tuple[float, float] = (MOUTH_DROP, MOUTH_DEPTH),
    eyes: float = EYE_HALF,
) -> tuple[float, ...]:
    """The five landmarks of a face with this pose, as the detector would report them (the inverse of `estimate`).

    `scale` is picture heights per millimetre, `nose` and `mouth` are (drop, depth) so tests can give a face its own
    proportions. Used by the tests and the demo rig; production code only reads landmarks.
    """
    cy, cp = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    ct, st = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    points = [
        (-eyes, 0.0, 0.0),
        (eyes, 0.0, 0.0),
        (0.0, nose[0], nose[1]),
        (-25.0, mouth[0], mouth[1]),
        (25.0, mouth[0], mouth[1]),
    ]
    out: list[float] = []
    for x, y, z in points:
        # yaw about the vertical axis, then pitch about the horizontal one (positive: the face turns down)
        x1, z1 = x * cy + z * cp, -x * cp + z * cy
        y2 = y * ct + z1 * st
        out += [centre[0] + (x1 * cr - y2 * sr) * scale, centre[1] + (x1 * sr + y2 * cr) * scale]
    return tuple(out)


@dataclass(frozen=True)
class Baseline:
    """The pose a person holds while looking at the lens."""

    pitch: float = 0.0
    yaw: float = 0.0


def classify(pose: Pose, baseline: Baseline = Baseline()) -> str:
    pitch, yaw = pose.pitch - baseline.pitch, pose.yaw - baseline.yaw
    if pitch >= DOWN_PITCH:
        return "down"
    if abs(yaw) >= AWAY_YAW or pitch <= AWAY_UP:
        return "away"
    if abs(pitch) < LENS_PITCH and abs(yaw) < LENS_YAW:
        return "lens"
    return "screen"


@dataclass
class Advice:
    kind: str  # "wait" | "ok" | "lower" | "raise" | "centre" | "calibrate"
    text: str
    degrees: float | None = None
    approximate: bool = False  # no calibration yet: the person's own face shape can shift the angle by a few degrees


@dataclass
class _Sample:
    t: float
    gaze: str
    pitch: float  # relative to the baseline
    yaw: float
    eye_line: float


@dataclass
class Coach:
    """Poses in, a gaze, advice and a "you have been looking away" signal out."""

    baseline: Baseline | None = None
    reminder_minutes: int = REMINDER_DEFAULT
    _samples: deque[_Sample] = field(default_factory=lambda: deque(maxlen=600))
    _recent: deque[Pose] = field(default_factory=lambda: deque(maxlen=SMOOTH))
    _away_since: float | None = None
    _back_since: float | None = None
    _reminded_at: float = float("-inf")
    _calibration: list[Pose] | None = None
    last: _Sample | None = None

    # -- feeding ----------------------------------------------------------------------------------------------

    def reset(self) -> None:
        self._samples.clear()
        self._recent.clear()
        self._away_since = self._back_since = None
        self.last = None

    def feed(self, t: float, pose: Pose | None) -> str | None:
        """One detection. The gaze it shows ("lens", "screen", "down", "away"), or None without a readable face."""
        if pose is None:
            return None
        if self._calibration is not None:
            self._calibration.append(pose)
        base = self.baseline or Baseline()
        self._recent.append(pose)
        smooth = Pose(
            statistics.median(p.yaw for p in self._recent),
            statistics.median(p.pitch for p in self._recent),
            statistics.median(p.roll for p in self._recent),
            statistics.median(p.eye_line for p in self._recent),
            statistics.median(p.eye_distance for p in self._recent),
        )
        gaze = classify(smooth, base)
        sample = _Sample(t, gaze, smooth.pitch - base.pitch, smooth.yaw - base.yaw, smooth.eye_line)
        self._samples.append(sample)
        self.last = sample
        while self._samples and t - self._samples[0].t > WINDOW:
            self._samples.popleft()
        if gaze in ("down", "away"):
            self._back_since = None
            if self._away_since is None:
                self._away_since = t
        elif self._away_since is not None:
            if self._back_since is None:
                self._back_since = t
            elif t - self._back_since >= AWAY_RESET:
                self._away_since = self._back_since = None
        return gaze

    # -- calibration ------------------------------------------------------------------------------------------

    @property
    def calibrating(self) -> bool:
        return self._calibration is not None

    def begin_calibration(self) -> None:
        self._calibration = []

    @property
    def calibration_progress(self) -> float:
        return 0.0 if self._calibration is None else min(1.0, len(self._calibration) / CALIBRATE_SAMPLES)

    def finish_calibration(self) -> Baseline | None:
        """The baseline from the poses gathered since `begin_calibration`; None (nothing changes) when too few."""
        poses, self._calibration = self._calibration or [], None
        if len(poses) < CALIBRATE_SAMPLES:
            return None
        self.baseline = Baseline(statistics.median(p.pitch for p in poses), statistics.median(p.yaw for p in poses))
        self.reset()
        return self.baseline

    def cancel_calibration(self) -> None:
        self._calibration = None

    # -- reading ----------------------------------------------------------------------------------------------

    def gaze_shares(self) -> dict[str, float]:
        total = len(self._samples)
        return {g: (sum(1 for s in self._samples if s.gaze == g) / total if total else 0.0) for g in GAZES}

    def eye_line(self) -> float | None:
        values = [s.eye_line for s in self._samples]
        return statistics.median(values) if len(values) >= MIN_SAMPLES else None

    def advice(self) -> Advice:
        found = self._advice()
        found.approximate = self.baseline is None and found.kind in ("lower", "raise", "centre")
        return found

    def _advice(self) -> Advice:
        if len(self._samples) < MIN_SAMPLES:
            return Advice("wait", "Looking at your camera position: keep the call window open and carry on as usual.")
        looking = [s for s in self._samples if s.gaze in ("lens", "screen")]
        screen = [s for s in looking if s.gaze == "screen"]
        if not screen or len(screen) < SCREEN_SHARE * max(len(looking), 1):
            return Advice("ok", "Your eye line meets the camera. Nothing to change.", 0.0)
        pitch = statistics.median(s.pitch for s in screen)
        yaw = statistics.median(s.yaw for s in screen)
        degrees = round(math.hypot(pitch, yaw), 1)
        if abs(pitch) < ADVICE_MIN and abs(yaw) < ADVICE_MIN:
            return Advice("ok", "Your eye line is close to the camera. Nothing to change.", degrees)
        if abs(yaw) >= ADVICE_MIN and abs(yaw) > abs(pitch):
            side = "right" if yaw > 0 else "left"
            return Advice(
                "centre",
                f"The call window is {degrees:.0f} degrees to your {side} of the camera: "
                "move the window toward the lens, or the camera toward the window.",
                degrees,
            )
        if pitch > 0:
            return Advice(
                "lower",
                f"Camera is {degrees:.0f} degrees off your eye line: lower the camera, "
                "or move the call window closer to it.",
                degrees,
            )
        return Advice(
            "raise",
            f"Camera is {degrees:.0f} degrees off your eye line: raise the camera, "
            "or move the call window closer to it.",
            degrees,
        )

    def away_for(self, t: float) -> float:
        """Seconds the head has pointed away from the screen (down or sideways) without a real break."""
        return 0.0 if self._away_since is None else max(0.0, t - self._away_since)

    def age_away(self, seconds: float) -> bool:
        """Pretend the current looking-away episode began `seconds` earlier (the demo rig); False when there is none."""
        if self._away_since is None:
            return False
        self._away_since -= seconds
        return True

    def reminder_due(self, t: float) -> bool:
        """True once per episode of sustained looking away; the caller decides whether a reminder is allowed at all."""
        if self.away_for(t) < self.reminder_minutes * 60.0 or t - self._reminded_at < REMINDER_COOLDOWN:
            return False
        self._reminded_at = t
        return True


def eye_line_hint(eye_line: float | None) -> str | None:
    """How the picture's eye line sits against the top third (the thirds grid the wizard draws)."""
    if eye_line is None:
        return None
    gap = eye_line - IDEAL_EYE_LINE
    if abs(gap) <= 0.04:
        return "Your eyes sit on the top third of the picture."
    if gap < 0:
        return f"Your eyes are {abs(gap) * 100:.0f} percent of the picture too high: tilt the camera up a little."
    return f"Your eyes are {gap * 100:.0f} percent of the picture too low: tilt the camera down a little."


def window_offset_cm(viewing_cm: float, degrees: float = LENS_PITCH) -> float:
    """How far from the lens the call window's centre may sit, in centimetres, for a given viewing distance."""
    return round(viewing_cm * math.tan(math.radians(degrees)), 1)
