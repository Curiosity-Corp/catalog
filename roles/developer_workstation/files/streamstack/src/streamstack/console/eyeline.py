"""The eye-line coach in the Console: the `eyeline.*` actions, the `eyeline` state and the one thread behind them.

    coach thread    a still of the camera at a low rate -> face detector -> five landmarks -> head pose -> Coach

It tells a person when the camera sits so that looking at the call window reads as looking away, and how far off it is
(docs/auto-framing.md, "Eye-line coach"). It is **off until the person turns it on**; while it is on it reads one
still of the camera about five times a second, keeps only the numbers (never a picture) and sends nothing anywhere.
It opens no device: the picture comes from the video engine the Console already runs, as for auto-framing.

Actions (docs/console.md):
    eyeline.enable {on}               turn the coach on or off
    eyeline.calibrate {}              look at the lens for two seconds: the angles are measured from this pose
    eyeline.calibration.clear {}      forget the calibration
    eyeline.reminder {on, minutes?}   a gentle reminder after N minutes of looking away during a call (off by default)
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import config, store
from streamstack.console.errors import ActionError
from streamstack.framing import detector, eyeline, health
from streamstack.framing.engine import Face
from streamstack.framing.eyeline import Baseline, Coach
from streamstack.saving import LatestWriter

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

FPS = 5.0  # detections per second: the framing sampling rate, well inside the detector's budget
IDLE_WAIT = 0.5
MAX_ERRORS = 5  # consecutive detector failures before the coach turns itself off
CLOSE_AFTER_OFF = 60.0  # the detector (and its GPU context) is released this long after the coach goes off
CALIBRATE_TIMEOUT = 15.0  # seconds to see a face for the calibration before giving up
FACE_LOST_AFTER = 3.0  # seconds without a readable face before the state says so
DEFAULT_VIEWING_CM = 60


def empty_state() -> dict[str, Any]:
    """The `eyeline` snapshot of a rig without the coach, and the page's initial value."""
    return {
        "available": False,
        "unavailable_hint": None,
        "enabled": False,
        "state": "off",
        "reason": None,
        "device": None,
        "in_call": False,
        "gaze": None,
        "pitch": None,
        "yaw": None,
        "calibrated": False,
        "baseline": None,
        "calibrating": False,
        "calibration_progress": 0.0,
        "shares": dict.fromkeys(eyeline.GAZES, 0.0),
        "advice": {"kind": "wait", "text": "", "degrees": None, "approximate": False},
        "eye_line": None,
        "eye_line_target": round(eyeline.IDEAL_EYE_LINE, 3),
        "eye_line_hint": None,
        "reminder": {
            "on": False,
            "minutes": eyeline.REMINDER_DEFAULT,
            "range": list(eyeline.REMINDER_MINUTES),
            "away_s": 0,
        },
        "wizard": {"viewing_cm": DEFAULT_VIEWING_CM, "window_offset_cm": eyeline.window_offset_cm(DEFAULT_VIEWING_CM)},
    }


def clean_on(value: object) -> bool:
    if not isinstance(value, bool):
        raise ActionError("Say on or off: on must be true or false.", 400)
    return value


def clean_minutes(value: object) -> int:
    low, high = eyeline.REMINDER_MINUTES
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or int(value) != value
        or not low <= value <= high
    ):
        raise ActionError(f"minutes must be a whole number from {low} to {high}.", 400)
    return int(value)


def largest_pose(faces: list[Face]) -> eyeline.Pose | None:
    """The pose of the biggest face that has landmarks (the person at the desk, not someone behind them)."""
    for face in sorted(faces, key=lambda f: f.area, reverse=True):
        pose = eyeline.estimate(face.landmarks)
        if pose is not None:
            return pose
    return None


class EyeLineController:
    """The real thing: the face detector on stills from the video controller."""

    def __init__(
        self,
        cfg: dict[str, str],
        video: object,
        notify: Notify | None = None,
        *,
        opener: Callable[..., detector.Detector] | None = None,
        state_file: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        in_call: Callable[[], bool] = lambda: False,
    ) -> None:
        self.cfg = cfg
        self.video = video
        self._notify = notify
        self._opener = opener or detector.open_detector
        self._clock = clock
        self.in_call = in_call  # the Rig points this at the call controller
        self._state_file = state_file or config.state_dir() / "eyeline.json"
        self._device = cfg.get("STREAMSTACK_FRAMING_DEVICE", "auto") or "auto"
        self._period = 1.0 / FPS
        self.lock = threading.RLock()
        self._writer: LatestWriter[dict] = LatestWriter(lambda data: store.write_json(self._state_file, data))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._reminder_on = False
        self._coach = Coach()
        self._detector: detector.Detector | None = None
        self._off_since: float | None = None
        self._status = "off"  # off | starting | running | unavailable
        self._reason: str | None = None
        self._errors = 0
        self._last_face = float("-inf")
        self._calibrating_since: float | None = None
        self._ready: tuple[float, tuple[bool, str, str]] | None = None
        self._load()

    # -- notifications and persistence ----------------------------------------------------------------------------

    @property
    def notify(self) -> Notify | None:
        return self._notify

    @notify.setter
    def notify(self, hook: Notify | None) -> None:
        self._notify = hook

    def _toast(self, level: str, text: str) -> None:
        if self._notify is not None:
            self._notify(level, text)

    def _load(self) -> None:
        """The saved choices; a missing or damaged file means the defaults (off, uncalibrated)."""
        saved = store.read_json(
            self._state_file, {}, validate=store.is_object
        )  # a damaged file is set aside, not fatal
        self._enabled = saved.get("enabled") is True
        self._reminder_on = saved.get("reminder") is True
        minutes = saved.get("minutes")
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            low, high = eyeline.REMINDER_MINUTES
            self._coach.reminder_minutes = min(max(minutes, low), high)
        base = saved.get("baseline")
        if isinstance(base, dict) and all(isinstance(base.get(k), (int, float)) for k in ("pitch", "yaw")):
            self._coach.baseline = Baseline(float(base["pitch"]), float(base["yaw"]))
        if self._enabled:
            self._status = "starting"

    def _save(self, *, report: bool = False) -> None:
        """Save the settings as they are now. Never call it with `lock` held: the disk must not hold up `state()`
        (issue #168). `report`: an action tells the person when the save fails, a background step only logs it."""
        with self.lock:
            base = self._coach.baseline
            self._writer.publish(
                {
                    "enabled": self._enabled,
                    "reminder": self._reminder_on,
                    "minutes": self._coach.reminder_minutes,
                    "baseline": None if base is None else {"pitch": round(base.pitch, 2), "yaw": round(base.yaw, 2)},
                }
            )
        try:
            self._writer.flush()
        except OSError as exc:
            log.warning("eye-line settings not saved to %s: %s", self._state_file, exc)
            if report:
                raise ActionError(f"The eye-line setting is in effect but could not be saved: {exc}", 409) from exc

    # -- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="streamstack-eyeline")
        self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._close_detector()

    # -- the actions ------------------------------------------------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        if action == "eyeline.enable":
            return self.enable(clean_on(params.get("on")))
        if action == "eyeline.calibrate":
            return self.calibrate()
        if action == "eyeline.calibration.clear":
            return self.clear_calibration()
        if action == "eyeline.reminder":
            minutes = clean_minutes(params["minutes"]) if "minutes" in params else None
            return self.set_reminder(clean_on(params.get("on")), minutes)
        raise ActionError(f"unknown action: {action}", 400)

    def enable(self, on: bool) -> str:
        if on:
            ready, why, hint = self._readiness()
            if not ready:
                raise ActionError(f"The eye-line coach is not available: {why}. Fix: {hint}", 409)
        with self.lock:
            self._enabled = on
            self._errors = 0
            self._reason = None
            self._status = ("starting" if self._status in ("off", "unavailable") else self._status) if on else "off"
            self._coach.reset()
            self._coach.cancel_calibration()
            self._calibrating_since = None
        self._wake.set()
        self._save(report=True)
        return "Eye-line coach on" if on else "Eye-line coach off"

    def calibrate(self) -> str:
        with self.lock:
            if not self._enabled:
                raise ActionError("Turn the eye-line coach on first, then look at the lens.", 409)
            self._coach.begin_calibration()
            self._calibrating_since = self._clock()
        self._wake.set()
        return "Look at the camera lens"

    def clear_calibration(self) -> str:
        with self.lock:
            self._coach.baseline = None
            self._coach.cancel_calibration()
            self._coach.reset()
            self._calibrating_since = None
        self._save(report=True)
        return "Calibration cleared"

    def set_reminder(self, on: bool, minutes: int | None = None) -> str:
        with self.lock:
            self._reminder_on = on
            if minutes is not None:
                self._coach.reminder_minutes = minutes
        self._save(report=True)
        return "Looking-away reminder on" if on else "Looking-away reminder off"

    # -- state ------------------------------------------------------------------------------------------------------

    def status(self) -> dict:
        return self.state()

    def state(self) -> dict:
        ready = self._readiness()
        with self.lock:
            enabled, status, reason = self._enabled, self._status, self._reason
            coach = self._coach
            now = self._clock()
            last = coach.last
            advice = coach.advice()
            shares = coach.gaze_shares()
            eye_line = coach.eye_line()
            base = coach.baseline
            calibrating, progress = coach.calibrating, coach.calibration_progress
            reminder = {
                "on": self._reminder_on,
                "minutes": coach.reminder_minutes,
                "range": list(eyeline.REMINDER_MINUTES),
                "away_s": round(coach.away_for(now)),
            }
            detector_ = self._detector
        if status == "unavailable":
            shown = "unavailable"  # it turned itself off: the reason stays on show
        elif not enabled:
            shown = "off"
        elif status == "starting":
            shown = "starting"
        elif now - self._last_face > FACE_LOST_AFTER:
            shown = "no-face"
        else:
            shown = "running"
        state = empty_state()
        state.update(
            available=ready[0],
            unavailable_hint=None if ready[0] else f"{ready[1]}. Fix: {ready[2]}",
            enabled=enabled,
            state=shown,
            reason=reason if status == "unavailable" else None,
            device=detector_.device if detector_ is not None else None,
            in_call=bool(self.in_call()),
            calibrated=base is not None,
            baseline=None if base is None else {"pitch": round(base.pitch, 1), "yaw": round(base.yaw, 1)},
            calibrating=calibrating,
            calibration_progress=round(progress, 2),
            reminder=reminder,
        )
        if enabled and shown == "running":
            state.update(
                gaze=last.gaze if last else None,
                pitch=None if last is None else round(last.pitch, 1),
                yaw=None if last is None else round(last.yaw, 1),
                shares={k: round(v, 2) for k, v in shares.items()},
                advice={
                    "kind": advice.kind,
                    "text": advice.text,
                    "degrees": advice.degrees,
                    "approximate": advice.approximate,
                },
                eye_line=None if eye_line is None else round(eye_line, 3),
                eye_line_hint=eyeline.eye_line_hint(eye_line),
            )
        return state

    def _readiness(self) -> tuple[bool, str, str]:
        """health.readiness(), looked up at most every 10 s (the state is built several times a second)."""
        now = self._clock()
        if self._ready is None or now - self._ready[0] > 10.0:
            self._ready = (now, health.readiness())
        return self._ready[1]

    # -- the picture ------------------------------------------------------------------------------------------------

    def _camera(self) -> str | None:
        view = getattr(self.video, "scene_view", None)
        if not callable(view):
            return None
        connected, _program, _scenes, cameras = view()
        return sorted(cameras)[0] if connected and cameras else None

    def _shot(self, source: str) -> bytes | None:
        native = getattr(self.video, "detect_frame", None)
        return native(source) if callable(native) else None

    # -- the loop ---------------------------------------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            try:
                ran = self._tick(started)
            except Exception:
                log.exception("eye-line coach tick failed")
                ran = False
            if not ran:
                self._wake.wait(IDLE_WAIT)
                self._wake.clear()
                continue
            self._stop.wait(max(0.0, self._period - (self._clock() - started)))

    def _close_detector(self) -> None:
        det, self._detector = self._detector, None
        if det is not None:
            try:
                det.close()
            except Exception as exc:
                log.warning("eye-line: detector close failed: %s", exc)

    def _fail(self, reason: str) -> None:
        with self.lock:
            self._enabled, self._status, self._reason = False, "unavailable", reason
        self._save()
        self._close_detector()
        self._toast("warn", f"The eye-line coach is off: {reason}")

    def _tick(self, now: float) -> bool:
        """One detection. True when it did work (so the loop paces itself)."""
        if not self._enabled:
            if self._detector is not None:
                if self._off_since is None:
                    self._off_since = now
                elif now - self._off_since > CLOSE_AFTER_OFF:
                    self._close_detector()
            return False
        self._off_since = None
        if self._detector is None:
            try:
                self._detector = self._opener(self._device)
            except detector.DetectorUnavailable as exc:
                self._fail(str(exc))
                return False
            except Exception as exc:
                log.exception("eye-line detector failed to open")
                self._fail(f"{type(exc).__name__}: {exc}")
                return False
        with self.lock:
            if self._status == "starting":
                self._status = "running"
        source = self._camera()
        if source is None:
            return False
        try:
            jpeg = self._shot(source)
        except (ActionError, RuntimeError) as exc:  # the engine is not there
            log.debug("eye-line: no picture of %s: %s", source, exc)
            return False
        if jpeg is None:
            return False
        try:
            faces = self._detector.detect(jpeg)
        except Exception as exc:
            return self._detector_error(exc)
        self._errors = 0
        self._observe(now, largest_pose(faces))
        return True

    def _observe(self, now: float, pose: eyeline.Pose | None) -> None:
        """One pose (or none) into the coach; the calibration and the reminder ride on the same step."""
        remind = calibrated = False
        with self.lock:
            if pose is not None:
                self._last_face = now
            self._coach.feed(now, pose)
            if self._coach.calibrating:
                if self._coach.calibration_progress >= 1.0:
                    done = self._coach.finish_calibration() is not None
                    self._calibrating_since = None
                    calibrated = done
                elif self._calibrating_since is not None and now - self._calibrating_since > CALIBRATE_TIMEOUT:
                    self._coach.cancel_calibration()
                    self._calibrating_since = None
                    self._toast(
                        "warn",
                        "The eye-line calibration could not see your face. Sit in front of the camera and try again.",
                    )
            if self._reminder_on and self.in_call() and self._coach.reminder_due(now):
                remind = True
                minutes = self._coach.reminder_minutes
        if calibrated:  # outside `lock`: the new baseline is in effect, the disk follows
            self._toast("info", "Camera position calibrated: angles are now measured from your lens pose.")
            self._save()
        if remind:
            self._toast("info", f"You have been looking away from the screen for {minutes} minutes. Back to the call?")

    def _detector_error(self, exc: Exception) -> bool:
        self._errors += 1
        log.warning("eye-line: detection failed (%d/%d): %s", self._errors, MAX_ERRORS, exc)
        if self._errors >= MAX_ERRORS:
            self._fail(f"the face detector keeps failing ({type(exc).__name__}: {exc})")
        return False


class DemoEyeLine(EyeLineController):
    """The same API on the demo rig: a simulated person instead of a camera, for the e2e suite and --demo.

    `eyeline.demo.gaze {gaze, offset?}` says what the simulated person looks at ("lens", "screen", "down", "away") and,
    for "screen", how many degrees the camera sits off their eye line. The simulated head has its own habit (it
    rests three degrees below the lens), so the calibration has something to remove.
    """

    REST_PITCH = 3.0
    RATE = 10.0

    def __init__(self, notify: Notify | None = None, in_call: Callable[[], bool] = lambda: False) -> None:
        super().__init__({}, None, notify, state_file=Path(config.state_dir() / "eyeline-demo.json"), in_call=in_call)
        self._period = 1.0 / self.RATE
        self._gaze, self._offset = "lens", 14.0
        self._enabled = False
        self._reminder_on = False
        self._coach = Coach()
        self._status = "off"
        self._ready = (float("inf"), (True, "", ""))  # the simulation needs no model

    def _load(self) -> None:
        """The demo never reads or writes the person's settings."""

    def _save(self, *, report: bool = False) -> None:
        """Nothing is kept."""

    def _readiness(self) -> tuple[bool, str, str]:
        return True, "", ""

    def act(self, action: str, params: dict) -> str | None:
        if action == "eyeline.demo.gaze":
            gaze = str(params.get("gaze") or "")
            if gaze not in eyeline.GAZES:
                raise ActionError(f"gaze must be one of: {', '.join(eyeline.GAZES)}.", 400)
            offset = params.get("offset", self._offset)
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                raise ActionError("offset must be a number of degrees.", 400)
            with self.lock:
                self._gaze, self._offset = gaze, float(offset)
            return None
        if action == "eyeline.demo.reset":
            with self.lock:
                self._gaze, self._offset = "lens", 14.0
                self._enabled = self._reminder_on = False
                self._status = "off"
                self._coach = Coach()
                self._calibrating_since = None
                self._last_face = float("-inf")
            return None
        if action == "eyeline.demo.away":  # skip ahead: the person has been looking away for this many seconds
            seconds = params.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ActionError("seconds must be a positive number.", 400)
            with self.lock:
                if not self._coach.age_away(float(seconds)):
                    raise ActionError("The simulated person is not looking away.", 409)
            return None
        return super().act(action, params)

    def _pose(self) -> eyeline.Pose | None:
        pitch = {"lens": 0.0, "screen": self._offset, "down": 45.0, "away": 0.0}[self._gaze] + self.REST_PITCH
        yaw = 40.0 if self._gaze == "away" else 0.0
        return eyeline.estimate(eyeline.project(yaw, pitch))

    def _tick(self, now: float) -> bool:
        if not self._enabled:
            return False
        with self.lock:
            if self._status == "starting":
                self._status = "running"
        self._observe(now, self._pose())
        return True

    def _fail(self, reason: str) -> None:
        raise RuntimeError(reason)  # pragma: no cover - the simulation has no detector to fail
