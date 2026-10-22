"""The framing maths with synthetic face tracks: composition, dead zone, smoothing, loss, hold, zoom caps."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from streamstack.framing.engine import (
    FULL,
    LOST_AFTER,
    PROFILES,
    Face,
    Framer,
    OneEuro,
    Spring,
    resolution_zoom_cap,
)

HZ = 8.0  # detections per second
RATE = 20.0  # output steps per second


def face(cx: float = 0.5, eye: float = 0.4, height: float = 0.2, score: float = 0.9) -> Face:
    """A face centred at cx whose eye line is at `eye` and whose box is `height` tall."""
    return Face(cx - height * 0.4, eye - 0.4 * height, height * 0.8, height, score, eye_y=eye)


def run(framer: Framer, seconds: float, faces, start: float = 0.0) -> list[tuple[float, object]]:
    """Drive a framer for `seconds`: detections at HZ (faces(t) -> list), steps at RATE. Returns [(t, window)]."""
    track = []
    detect_next = start
    n = int(seconds * RATE)
    for i in range(n + 1):
        t = start + i / RATE
        if t >= detect_next - 1e-9:
            framer.observe(t, faces(t))
            detect_next += 1 / HZ
        track.append((t, framer.step(t)))
    return track


def path_length(track) -> float:
    return sum(
        abs(b.cx - a.cx) + abs(b.cy - a.cy) + abs(math.log(b.zoom / a.zoom))
        for (_, a), (_, b) in itertools.pairwise(track)
    )


# -- the spring and the filter ---------------------------------------------------------------------------------


def test_spring_is_exact_for_any_step_size() -> None:
    fine, coarse = Spring(0.0), Spring(0.0)
    fine.target = coarse.target = 1.0
    for _ in range(100):
        fine.step(0.01, 0.5, 99)
    coarse.step(1.0, 0.5, 99)
    assert fine.value == pytest.approx(coarse.value, abs=1e-9)
    assert fine.velocity == pytest.approx(coarse.velocity, abs=1e-9)


def test_spring_never_overshoots_and_settles() -> None:
    spring = Spring(0.0)
    spring.target = 1.0
    values = [spring.step(0.02, 0.8, 99) for _ in range(400)]
    assert max(values) <= 1.0 + 1e-9
    assert values == sorted(values)
    assert values[-1] == pytest.approx(1.0, abs=1e-3)


def test_spring_limits_speed() -> None:
    spring = Spring(0.0)
    spring.target = 10.0
    for _ in range(100):
        spring.step(0.02, 0.1, 0.5)
    assert abs(spring.velocity) <= 0.5 + 1e-9


def test_one_euro_removes_jitter_but_follows_a_move() -> None:
    rng = random.Random(3)  # noqa: S311 - a seeded test signal, not a secret
    f = OneEuro()
    still = [f(i / 8, 0.5 + rng.uniform(-0.01, 0.01)) for i in range(40)]
    assert max(still[8:]) - min(still[8:]) < 0.012  # input spread is 0.02
    moved = [f(5 + i / 8, 0.8) for i in range(24)]
    assert moved[-1] == pytest.approx(0.8, abs=0.02)


# -- composition -------------------------------------------------------------------------------------------------


def test_off_does_nothing() -> None:
    framer = Framer("off")
    track = run(framer, 3, lambda t: [face()])
    assert all(w == FULL for _, w in track)
    assert framer.state == "off"
    assert framer.settled_full


def test_a_centred_face_gets_headroom_and_the_mode_zoom() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    track = run(framer, 12, lambda t: [face(0.5, 0.4, 0.2)])
    window = track[-1][1]
    assert framer.state == "tracking"
    # tight wants the face 34 % of the window: 0.34 / 0.2 = 1.7x
    assert window.zoom == pytest.approx(1.7, abs=0.03)
    left, top, right, bottom = window.rect()
    assert 0 <= left < right <= 1 and 0 <= top < bottom <= 1
    eye_in_window = (0.4 - top) / (bottom - top)
    assert eye_in_window == pytest.approx(1 / 3, abs=0.04)  # the rule of thirds
    assert (left + right) / 2 == pytest.approx(0.5, abs=0.02)


def test_a_face_far_from_centre_is_followed_and_kept_in_frame() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    track = run(framer, 14, lambda t: [face(0.8, 0.35, 0.2)])
    window = track[-1][1]
    left, _top, right, _bottom = window.rect()
    assert window.cx > 0.6
    assert right <= 1 + 1e-9 and left >= -1e-9
    assert left < 0.8 - 0.1 < 0.8 + 0.1 < right  # the face (0.1 wide) is inside


def test_modes_differ_in_how_close_they_frame() -> None:
    zooms = {}
    for mode in ("gentle", "follow", "tight"):
        framer = Framer(mode, zoom_cap=2.0)
        zooms[mode] = run(framer, 14, lambda t: [face(height=0.15)])[-1][1].zoom
    assert zooms["gentle"] < zooms["follow"] < zooms["tight"]
    assert zooms["gentle"] <= PROFILES["gentle"].max_zoom + 1e-9
    assert zooms["tight"] <= PROFILES["tight"].max_zoom + 1e-9


def test_a_big_close_face_is_not_zoomed_at_all() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    window = run(framer, 8, lambda t: [face(height=0.5)])[-1][1]
    assert window.zoom == pytest.approx(1.0, abs=1e-3)


def test_resolution_caps_the_zoom() -> None:
    # The Canon's 704-line live view drawn 1080 tall is already enlarged 1.53x: little room left.
    canon = resolution_zoom_cap(704, 1080)
    camlink = resolution_zoom_cap(1080, 1080)
    assert canon == pytest.approx(1.304, abs=0.01)
    assert camlink == pytest.approx(2.0)
    assert resolution_zoom_cap(1080, 540) > camlink  # drawn half size (side by side): more room
    assert resolution_zoom_cap(0, 1080) == 1.0
    assert resolution_zoom_cap(500, 3000) == 1.0  # never below the full frame

    small = Framer("tight", zoom_cap=canon)
    assert run(small, 14, lambda t: [face(height=0.1)])[-1][1].zoom <= canon + 1e-6
    wide = Framer("tight", zoom_cap=camlink)
    assert run(wide, 14, lambda t: [face(height=0.1)])[-1][1].zoom == pytest.approx(
        PROFILES["tight"].max_zoom, abs=0.03
    )


# -- stability --------------------------------------------------------------------------------------------------


def test_detector_jitter_does_not_move_the_picture() -> None:
    """The failure that matters: a still person with a noisy detector must give a still picture."""
    rng = random.Random(7)  # noqa: S311 - a seeded test signal, not a secret
    framer = Framer("gentle", zoom_cap=2.0)

    def noisy(t: float) -> list[Face]:
        return [
            face(0.5 + rng.uniform(-0.012, 0.012), 0.4 + rng.uniform(-0.012, 0.012), 0.16 + rng.uniform(-0.01, 0.01))
        ]

    run(framer, 15, noisy)  # settle
    later = run(framer, 40, noisy, start=15)
    assert path_length(later) < 0.02  # nothing but the sub-percent noise
    assert len({round(w.zoom, 3) for _, w in later}) <= 3


def test_hunting_is_impossible_when_the_face_sits_on_a_dead_zone_edge() -> None:
    """A face hovering around the threshold must not make the window flip back and forth."""
    framer = Framer("gentle", zoom_cap=2.0)
    run(framer, 10, lambda t: [face(0.5, 0.4, 0.16)])
    dead = PROFILES["gentle"].dead_pos
    wobble = lambda t: [face(0.5 + dead * (1.05 if int(t * HZ) % 2 else 0.95), 0.4, 0.16)]  # noqa: E731
    track = run(framer, 30, wobble, start=10)
    cxs = [w.cx for _, w in track]
    reversals = sum(1 for a, b, c in zip(cxs, cxs[1:], cxs[2:], strict=False) if (b - a) * (c - b) < -1e-9)
    assert reversals <= 2


def test_motion_is_smooth_with_no_overshoot() -> None:
    framer = Framer("follow", zoom_cap=2.0)
    run(framer, 8, lambda t: [face(0.4, 0.4, 0.2)])
    start = framer.window.cx
    track = run(framer, 8, lambda t: [face(0.6, 0.4, 0.2)], start=8)
    cxs = [w.cx for _, w in track]
    end = framer.target.cx
    assert end > start + 0.1
    assert max(cxs) <= end + 1e-6  # critically damped: it arrives from one side
    steps = [b - a for a, b in itertools.pairwise(cxs)]
    assert max(abs(s) for s in steps) <= PROFILES["follow"].max_speed / RATE + 1e-6
    assert framer.window.cx == pytest.approx(end, abs=0.01)


def test_the_window_never_leaves_the_frame_even_mid_move() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    for who in (0.15, 0.85, 0.5, 0.1):
        for _, w in run(framer, 6, lambda t, who=who: [face(who, 0.3, 0.14)]):
            left, top, right, bottom = w.rect()
            assert left >= -1e-9 and top >= -1e-9 and right <= 1 + 1e-9 and bottom <= 1 + 1e-9


def test_a_slowly_drifting_face_stays_inside_the_window() -> None:
    """Many small moves, each under the dead zone: the face must still never leave the picture."""
    framer = Framer("gentle", zoom_cap=2.0)
    drift = lambda t: [face(0.5 - 0.25 * min(t, 60) / 60, 0.4, 0.14)]  # noqa: E731
    for t, w in run(framer, 70, drift):
        left, _, right, _ = w.rect()
        cx = 0.5 - 0.25 * min(t, 60) / 60
        assert left - 1e-9 <= cx - 0.056 and cx + 0.056 <= right + 1e-9, (t, w)


# -- picking, confirming, losing ---------------------------------------------------------------------------------


def test_one_stray_detection_is_not_a_face() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    framer.observe(0.0, [face()])
    for i in range(1, 30):
        framer.step(i / RATE)
    assert framer.state == "no-face"
    assert framer.window == FULL


def test_low_scores_and_absurd_boxes_are_ignored() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    junk = [face(score=0.3), Face(0.1, 0.1, 0.02, 0.02, 0.99), Face(0, 0, 1, 0.99, 0.99)]
    run(framer, 5, lambda t: junk)
    assert framer.state == "no-face"


def test_the_largest_face_wins_and_the_followed_one_is_sticky() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    me, other = face(0.4, 0.4, 0.2), face(0.8, 0.4, 0.21)
    run(framer, 6, lambda t: [me])
    # a second, slightly larger face appears: the one already followed is kept
    run(framer, 6, lambda t: [me, other], start=6)
    assert framer.face is not None and abs(framer.face.cx - me.cx) < 0.01
    # but a clearly larger one takes over
    big = face(0.8, 0.4, 0.4)
    run(framer, 4, lambda t: [me, big], start=12)
    assert framer.face is not None and abs(framer.face.cx - big.cx) < 0.01


def test_losing_the_face_eases_back_to_the_full_frame_after_the_hold() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    run(framer, 12, lambda t: [face(0.7, 0.4, 0.18)])
    zoomed = framer.window
    assert zoomed.zoom > 1.4
    # inside the hold nothing changes
    track = run(framer, LOST_AFTER - 0.3, lambda t: [], start=12)
    assert track[-1][1].zoom == pytest.approx(zoomed.zoom, abs=0.02)
    assert framer.state == "tracking"
    run(framer, 14, lambda t: [], start=12 + LOST_AFTER - 0.3)
    assert framer.state == "no-face"
    assert framer.settled_full


def test_hold_freezes_and_release_resumes() -> None:
    framer = Framer("follow", zoom_cap=2.0)
    run(framer, 8, lambda t: [face(0.4, 0.4, 0.2)])
    framer.set_hold(True)
    frozen = framer.window
    track = run(framer, 6, lambda t: [face(0.8, 0.4, 0.2)], start=8)
    assert framer.state == "held"
    assert all(abs(w.cx - frozen.cx) < 0.02 for _, w in track)
    framer.set_hold(False)
    run(framer, 8, lambda t: [face(0.8, 0.4, 0.2)], start=14)
    assert framer.state == "tracking"
    assert framer.window.cx > frozen.cx + 0.1


def test_off_returns_to_the_full_frame_and_reports_settled() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    run(framer, 10, lambda t: [face(0.6, 0.4, 0.15)])
    assert not framer.settled_full
    framer.set_mode("off")
    run(framer, 12, lambda t: [face(0.6, 0.4, 0.15)], start=10)
    assert framer.settled_full and framer.state == "off"


def test_changing_between_modes_keeps_following() -> None:
    framer = Framer("gentle", zoom_cap=2.0)
    run(framer, 8, lambda t: [face(0.5, 0.4, 0.15)])
    framer.set_mode("tight")
    assert framer.state == "tracking"
    run(framer, 10, lambda t: [face(0.5, 0.4, 0.15)], start=8)
    assert framer.window.zoom > 1.5


def test_resume_after_time_off_air_is_not_a_lost_face() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    run(framer, 10, lambda t: [face(0.5, 0.4, 0.17)])
    zoom = framer.window.zoom
    framer.resume(100.0)  # off air for 90 s
    framer.step(100.05)
    assert framer.state == "tracking"
    assert framer.window.zoom == pytest.approx(zoom, abs=0.05)


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown framing mode"):
        Framer("cinematic")
