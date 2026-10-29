"""The eye-line coach core: head pose from the five landmarks, the three gazes, the advice and the reminder."""

from __future__ import annotations

import math
import random
import time

import pytest

from streamstack.framing import eyeline
from streamstack.framing.eyeline import Advice, Baseline, Coach, Pose, classify, estimate, project

STEP = 0.2  # seconds between detections (5 per second)


def person(rng: random.Random) -> dict:
    """A face of its own proportions and size: the nose, the mouth and the eye distance differ by up to ten percent."""
    return {
        "nose": (45 * rng.uniform(0.88, 1.12), 28 * rng.uniform(0.88, 1.12)),
        "mouth": (65 * rng.uniform(0.92, 1.08), -5 * rng.uniform(0.5, 1.5)),
        "eyes": 32 * rng.uniform(0.92, 1.08),
        "scale": rng.uniform(0.0012, 0.0022),
    }


def landmarks(rng: random.Random, face: dict, yaw: float, pitch: float, noise: float = 0.002) -> tuple[float, ...]:
    """What the detector would report: the projected face plus about half a pixel of jitter (in picture heights)."""
    marks = project(yaw, pitch, rng.gauss(0, 2), **face)
    return tuple(v + rng.gauss(0, noise) for v in marks)


# the three habits the coach tells apart: (mean pitch, pitch spread, yaw spread) in degrees of head pose
HABITS = {"lens": (0.0, 2.0, 3.0), "screen": (14.0, 4.0, 4.0), "down": (45.0, 8.0, 5.0)}


def clip(rng: random.Random, face: dict, habit: str, seconds: float = 12.0) -> list[tuple[float, ...]]:
    pitch, pitch_sd, yaw_sd = HABITS[habit]
    return [landmarks(rng, face, rng.gauss(0, yaw_sd), rng.gauss(pitch, pitch_sd)) for _ in range(int(seconds / STEP))]


def calibrated(rng: random.Random, face: dict) -> Coach:
    """A coach that has been asked to look at the lens once, as the wizard does."""
    coach = Coach()
    coach.begin_calibration()
    for i in range(eyeline.CALIBRATE_SAMPLES + 2):
        coach.feed(i * STEP, estimate(landmarks(rng, face, rng.gauss(0, 1), rng.gauss(0, 1))))
    assert coach.finish_calibration() is not None
    return coach


# -- head pose ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("pitch", [-15.0, 0.0, 12.0, 30.0, 50.0])
@pytest.mark.parametrize("yaw", [-30.0, 0.0, 20.0])
def test_a_clean_projection_gives_the_pose_back(yaw: float, pitch: float) -> None:
    pose = estimate(project(yaw, pitch))
    assert pose is not None
    assert pose.yaw == pytest.approx(yaw, abs=2.5)
    assert pose.pitch == pytest.approx(pitch, abs=3.0)
    assert pose.roll == pytest.approx(0.0, abs=2.0)


def test_the_roll_of_the_head_does_not_change_the_pitch() -> None:
    upright, tilted = estimate(project(0, 20)), estimate(project(0, 20, roll=25))
    assert upright is not None and tilted is not None
    assert tilted.roll == pytest.approx(25.0, abs=0.5)
    assert tilted.pitch == pytest.approx(upright.pitch, abs=0.5)


def test_the_eye_line_is_where_the_eyes_are() -> None:
    pose = estimate(project(0, 0, centre=(0.5, 0.31)))
    assert pose is not None and pose.eye_line == pytest.approx(0.31, abs=1e-6)
    assert pose.eye_distance == pytest.approx(2 * eyeline.EYE_HALF * 0.0016, rel=1e-3)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        (),
        (0.1,) * 9,
        (0.5,) * 10,  # all on one point: no eye distance
        (math.nan,) + (0.5,) * 9,
        (0.4, 0.4, 0.6, 0.4, 0.5, 0.35, 0.45, 0.3, 0.55, 0.3),  # nose and mouth above the eyes
        (0.4, 0.4, 0.5, 0.9, 0.45, 1.0, 0.4, 1.1, 0.5, 1.1),  # a head lying on its side
    ],
)
def test_landmarks_that_are_not_a_face_give_no_pose(bad: tuple[float, ...] | None) -> None:
    assert estimate(bad) is None


def test_a_face_too_small_to_read_gives_no_pose() -> None:
    assert estimate(project(0, 0, scale=0.0002)) is None


# -- the three habits --------------------------------------------------------------------------------------------------


def test_gazes_are_told_apart_with_the_baseline() -> None:
    base = Baseline(pitch=3.0, yaw=-2.0)
    assert classify(Pose(-2.0, 3.0, 0, 0.3, 0.1), base) == "lens"
    assert classify(Pose(-2.0, 3.0 + 14, 0, 0.3, 0.1), base) == "screen"
    assert classify(Pose(-2.0, 3.0 + 45, 0, 0.3, 0.1), base) == "down"
    assert classify(Pose(60.0, 3.0, 0, 0.3, 0.1), base) == "away"
    assert classify(Pose(-2.0, -25.0, 0, 0.3, 0.1), base) == "away"  # looking up


def test_the_coach_tells_lens_screen_and_keyboard_apart_on_recorded_style_clips() -> None:
    """Acceptance: at least 90 percent of the frames land in the right class (after the one-time calibration)."""
    rng = random.Random(47)  # noqa: S311 - seeded test data, not security
    right = total = 0
    for _ in range(40):  # forty different faces, every one with its own proportions, size and jitter
        face = person(rng)
        coach = calibrated(rng, face)
        t = 100.0
        for habit in HABITS:
            for index, marks in enumerate(clip(rng, face, habit)):
                gaze = coach.feed(t, estimate(marks))
                t += STEP
                if index >= eyeline.SMOOTH:  # the first poses of a clip are still the previous habit
                    total += 1
                    right += gaze == habit
    assert right / total >= 0.95, f"{right / total:.1%} of {total} frames"


def test_without_calibration_the_keyboard_is_still_found_and_the_advice_is_marked_approximate() -> None:
    rng = random.Random(5)  # noqa: S311 - seeded test data, not security
    face = person(rng)
    coach = Coach()
    t = 0.0
    for marks in clip(rng, face, "down", 6.0):
        coach.feed(t, estimate(marks))
        t += STEP
    assert coach.last is not None and coach.last.gaze == "down"
    for marks in clip(rng, face, "screen", 12.0):
        coach.feed(t, estimate(marks))
        t += STEP
    advice = coach.advice()
    assert advice.kind in ("lower", "ok") and (advice.kind == "ok" or advice.approximate)


# -- calibration -------------------------------------------------------------------------------------------------------


def test_calibration_needs_enough_poses_and_keeps_the_old_baseline_otherwise() -> None:
    coach = Coach(baseline=Baseline(4.0, 1.0))
    coach.begin_calibration()
    assert coach.calibrating and coach.calibration_progress == 0.0
    coach.feed(0.0, estimate(project(0, 5)))
    assert coach.finish_calibration() is None and not coach.calibrating
    assert coach.baseline == Baseline(4.0, 1.0)


def test_calibration_takes_the_median_pose_and_clears_the_history() -> None:
    coach = Coach()
    coach.begin_calibration()
    for i in range(eyeline.CALIBRATE_SAMPLES):
        coach.feed(i * STEP, estimate(project(2.0, 7.0)))
    assert coach.calibration_progress == 1.0
    base = coach.finish_calibration()
    assert base is not None and base.pitch == pytest.approx(7.0, abs=2.0) and base.yaw == pytest.approx(2.0, abs=2.0)
    assert coach.last is None and coach.gaze_shares()["lens"] == 0.0
    assert coach.feed(10.0, estimate(project(2.0, 7.0))) == "lens"  # the calibrated pose is "at the lens"


def test_calibration_can_be_cancelled() -> None:
    coach = Coach()
    coach.begin_calibration()
    coach.cancel_calibration()
    assert not coach.calibrating and coach.finish_calibration() is None and coach.baseline is None


# -- the advice --------------------------------------------------------------------------------------------------------


def feed_pose(coach: Coach, pitch: float, yaw: float = 0.0, seconds: float = 8.0, start: float = 0.0) -> float:
    t = start
    while t < start + seconds:
        coach.feed(t, estimate(project(yaw, pitch)))
        t += STEP
    return t


def test_the_coach_waits_for_enough_poses() -> None:
    coach = Coach(baseline=Baseline())
    assert coach.advice().kind == "wait"
    coach.feed(0.0, estimate(project(0, 20)))
    assert coach.advice().kind == "wait"


def test_a_camera_above_the_eye_line_says_how_many_degrees_and_what_to_move() -> None:
    coach = Coach(baseline=Baseline())
    feed_pose(coach, 14.0)
    advice = coach.advice()
    assert advice.kind == "lower" and advice.degrees is not None and 11 <= advice.degrees <= 17
    assert advice.text.startswith(f"Camera is {advice.degrees:.0f} degrees off your eye line")
    assert "lower the camera" in advice.text and "call window closer" in advice.text and not advice.approximate


def test_a_camera_below_the_eye_line_says_raise_it() -> None:
    coach = Coach(baseline=Baseline())
    feed_pose(coach, -12.0)
    advice = coach.advice()
    assert advice.kind == "raise" and "raise the camera" in advice.text


def test_a_window_to_the_side_says_which_side() -> None:
    coach = Coach(baseline=Baseline())
    feed_pose(coach, 0.0, yaw=-18.0)
    advice = coach.advice()
    assert advice.kind == "centre" and "to your left" in advice.text
    coach = Coach(baseline=Baseline())
    feed_pose(coach, 0.0, yaw=18.0)
    assert "to your right" in coach.advice().text


def test_looking_at_the_lens_most_of_the_time_needs_no_change() -> None:
    coach = Coach(baseline=Baseline())
    t = feed_pose(coach, 0.0, seconds=8.0)
    feed_pose(coach, 15.0, seconds=1.0, start=t)  # a glance at the screen
    assert coach.advice().kind == "ok"
    shares = coach.gaze_shares()
    assert shares["lens"] > 0.8 and sum(shares.values()) == pytest.approx(1.0)


def test_the_keyboard_does_not_count_toward_the_camera_offset() -> None:
    coach = Coach(baseline=Baseline())
    t = feed_pose(coach, 45.0, seconds=6.0)  # down at the keyboard
    feed_pose(coach, 0.0, seconds=6.0, start=t)
    assert coach.advice().kind == "ok"


def test_old_poses_fall_out_of_the_window() -> None:
    coach = Coach(baseline=Baseline())
    t = feed_pose(coach, 15.0, seconds=8.0)
    assert coach.advice().kind == "lower"
    feed_pose(coach, 0.0, seconds=eyeline.WINDOW + 4, start=t)
    assert coach.advice().kind == "ok"


def test_the_eye_line_median_and_the_thirds_hint() -> None:
    coach = Coach(baseline=Baseline())
    assert coach.eye_line() is None
    for i in range(20):
        coach.feed(i * STEP, estimate(project(0, 0, centre=(0.8, 0.2))))
    assert coach.eye_line() == pytest.approx(0.2, abs=1e-6)
    assert "too high" in (eyeline.eye_line_hint(0.2) or "")
    assert "too low" in (eyeline.eye_line_hint(0.5) or "")
    assert "top third" in (eyeline.eye_line_hint(0.34) or "")
    assert eyeline.eye_line_hint(None) is None


def test_the_window_offset_for_a_viewing_distance() -> None:
    assert eyeline.window_offset_cm(60) == pytest.approx(6.3, abs=0.1)
    assert eyeline.window_offset_cm(0) == 0.0
    assert isinstance(Advice("ok", "x").approximate, bool)


# -- the reminder ------------------------------------------------------------------------------------------------------


def test_sustained_looking_away_asks_for_one_reminder_per_episode() -> None:
    coach = Coach(baseline=Baseline(), reminder_minutes=2)
    t = 0.0
    due = []
    while t < 400:
        coach.feed(t, estimate(project(0, 45)))
        if coach.reminder_due(t):
            due.append(t)
        t += 1.0
    assert len(due) == 1 and 120 <= due[0] <= 123  # two minutes, once; the cooldown holds the next one back
    assert coach.away_for(t) > 300


def test_a_glance_at_the_screen_does_not_end_an_away_episode_but_a_real_break_does() -> None:
    coach = Coach(baseline=Baseline(), reminder_minutes=1)
    coach.feed(0.0, estimate(project(0, 45)))
    coach.feed(30.0, estimate(project(0, 0)))  # a glance
    coach.feed(35.0, estimate(project(0, 45)))
    assert coach.away_for(40.0) == pytest.approx(40.0)
    for i in range(5):  # looks up properly for longer than AWAY_RESET
        coach.feed(41.0 + i * 3, estimate(project(0, 0)))
    assert coach.away_for(60.0) == 0.0


def test_no_face_is_not_looking_away() -> None:
    coach = Coach(baseline=Baseline(), reminder_minutes=1)
    assert coach.feed(0.0, None) is None
    assert not coach.reminder_due(1000.0) and coach.away_for(1000.0) == 0.0


# -- the budget --------------------------------------------------------------------------------------------------------


def test_a_pose_and_a_coach_step_cost_next_to_nothing() -> None:
    rng = random.Random(1)  # noqa: S311 - seeded test data, not security
    face = person(rng)
    frames = [landmarks(rng, face, 0, 10) for _ in range(500)]
    # Best of five runs in this thread's CPU time (#164, the #71 convention): a busy runner preempting the test is not
    # coach cost.
    costs = []
    for _ in range(5):
        coach = Coach(baseline=Baseline())
        started = time.thread_time()
        for i, marks in enumerate(frames):
            coach.feed(i * STEP, estimate(marks))
            coach.advice()
        costs.append((time.thread_time() - started) / len(frames) * 1000)
    per_frame_ms = min(costs)
    assert per_frame_ms < 1.0  # the detector itself costs several milliseconds; the coach must not add to it
