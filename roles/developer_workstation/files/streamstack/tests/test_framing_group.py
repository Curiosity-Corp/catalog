"""Group framing, speaker hand-off and shot presets with synthetic face tracks (no detector, no camera)."""

from __future__ import annotations

import itertools
import math
import random
import time

import pytest

from streamstack.framing import group
from streamstack.framing.engine import (
    FULL,
    LOST_AFTER,
    LOST_HOME_AFTER,
    SHOTS,
    Face,
    Framer,
    Window,
)
from streamstack.framing.group import SpeakerPicker, Tracker

HZ = 8.0
RATE = 20.0


def person(cx: float, cy: float = 0.42, height: float = 0.2, mouth: float = 0.0) -> Face:
    """A face centred at (cx, cy); `mouth` opens the mouth corners that much (face heights)."""
    width = height * 0.8
    eye = cy - 0.1 * height
    return Face(
        cx - width / 2,
        cy - height / 2,
        width,
        height,
        0.9,
        eye_y=eye,
        mouth_y=cy + 0.25 * height + mouth * height,
        mouth_w=width * (0.5 + mouth),
    )


def drive(framer: Framer, seconds: float, faces, start: float = 0.0, speaking=None, voice_x=None):
    """Detections at HZ, steps at RATE; returns the list of (t, window)."""
    out = []
    next_detect = start
    for i in range(int(seconds * RATE) + 1):
        t = start + i / RATE
        if t >= next_detect - 1e-9:
            framer.observe(
                t,
                faces(t),
                speaking(t) if callable(speaking) else speaking,
                voice_x(t) if callable(voice_x) else voice_x,
            )
            next_detect += 1 / HZ
        out.append((t, framer.step(t)))
    return out


def inside(window: Window, faces: list[Face]) -> bool:
    left, top, right, bottom = window.rect()
    return all(
        f.x >= left - 1e-6 and f.x + f.w <= right + 1e-6 and f.y >= top - 1e-6 and f.y + f.h <= bottom + 1e-6
        for f in faces
    )


# -- the tracker -----------------------------------------------------------------------------------------------


def test_the_same_person_keeps_their_id_while_moving() -> None:
    tracker = Tracker()
    for i in range(20):
        tracker.update(i / HZ, [person(0.3 + 0.002 * i), person(0.7)])
    ids = sorted(t.id for t in tracker.tracks)
    assert ids == [1, 2]
    left = min(tracker.tracks, key=lambda t: t.face.cx)
    assert left.id == 1 and left.hits == 20


def test_a_passer_by_is_not_a_member_and_a_gone_face_stays_for_the_hold() -> None:
    tracker = Tracker()
    for i in range(16):  # two seconds of two people
        tracker.update(i / HZ, [person(0.3), person(0.7)])
    assert len(tracker.members(2.0)) == 2
    tracker.update(2.1, [person(0.3), person(0.7), person(0.5, height=0.1)])  # someone walks through
    tracker.update(2.25, [person(0.3), person(0.7)])
    assert len(tracker.members(2.3)) == 2
    for i in range(1, 8):  # the right person leaves: detections of one
        tracker.update(2.25 + i / HZ, [person(0.3)])
    assert len(tracker.members(2.25 + 7 / HZ)) == 2  # still inside the hold
    tracker.update(2.0 + group.HOLD + 0.5, [person(0.3)])
    assert [m.face.cx for m in tracker.members(2.0 + group.HOLD + 0.5)] == [pytest.approx(0.3)]


def test_activity_is_unknown_without_landmarks_and_zero_for_a_still_mouth() -> None:
    tracker = Tracker()
    bare = Face(0.3, 0.3, 0.1, 0.2, 0.9)
    for i in range(10):
        tracker.update(i / HZ, [bare, person(0.7)])
    by_x = {round(t.face.cx, 1): t for t in tracker.tracks}
    assert by_x[0.3].activity(1.2) is None
    assert by_x[0.7].activity(1.2) == pytest.approx(0.0, abs=1e-9)


# -- the speaker pick ------------------------------------------------------------------------------------------


def talking_pair(t: float, who: int | None, voice_x_only: bool = False) -> list[Face]:
    """Two people; `who` (0 left, 1 right) moves their mouth."""
    wobble = 0.08 * math.sin(t * 14) ** 2
    return [person(0.2, mouth=wobble if who == 0 else 0.0), person(0.8, mouth=wobble if who == 1 else 0.0)]


def feed(picker: SpeakerPicker, tracker: Tracker, t0: float, seconds: float, who, speaking=True, voice_x=None):
    pick = None
    t = t0
    while t < t0 + seconds:
        tracker.update(t, talking_pair(t, who))
        pick = picker.pick(t, tracker.members(t), speaking, voice_x)
        t += 1 / HZ
    return pick, t


def test_the_face_with_the_moving_mouth_is_the_speaker() -> None:
    picker, tracker = SpeakerPicker(), Tracker()
    pick, _ = feed(picker, tracker, 0.0, 4.0, who=1)
    right = max(tracker.tracks, key=lambda k: k.face.cx)
    assert pick == right.id and picker.confident


def test_a_new_speaker_must_lead_before_the_shot_moves_and_it_returns_to_the_group() -> None:
    picker, tracker = SpeakerPicker(), Tracker()
    first, t = feed(picker, tracker, 0.0, 4.0, who=0)
    left = min(tracker.tracks, key=lambda k: k.face.cx)
    right = max(tracker.tracks, key=lambda k: k.face.cx)
    assert first == left.id
    # the other person starts: the shot does not move at once (the activity window still holds the old speaker) ...
    early, t = feed(picker, tracker, t, 0.5, who=1)
    assert early == left.id
    later, t = feed(picker, tracker, t, 4.0, who=1)
    assert later == right.id
    # ... and with the voice gone the shot stays, then goes back to the whole group
    still, t = feed(picker, tracker, t, 2.0, who=None, speaking=False)
    assert still == right.id
    back, _ = feed(picker, tracker, t, group.RETURN_AFTER + 0.5, who=None, speaking=False)
    assert back is None


def test_voice_direction_breaks_a_tie_and_stands_in_for_missing_landmarks() -> None:
    picker, tracker = SpeakerPicker(), Tracker()
    t = 0.0
    pick = None
    while t < 4.0:  # both mouths move the same: the mouth cannot decide
        wobble = 0.08 * math.sin(t * 14) ** 2
        tracker.update(t, [person(0.3, mouth=wobble), person(0.7, mouth=wobble)])
        pick = picker.pick(t, tracker.members(t), True, voice_x=0.75)
        t += 1 / HZ
    assert pick == max(tracker.tracks, key=lambda k: k.face.cx).id
    picker, tracker = SpeakerPicker(), Tracker()
    t = 0.0
    while t < 3.0:  # masks: no landmarks at all
        tracker.update(t, [Face(0.25, 0.3, 0.1, 0.2, 0.9), Face(0.65, 0.3, 0.1, 0.2, 0.9)])
        pick = picker.pick(t, tracker.members(t), True, voice_x=0.3)
        t += 1 / HZ
    assert pick == min(tracker.tracks, key=lambda k: k.face.cx).id


def test_nobody_is_picked_while_nobody_speaks_or_when_one_person_is_there() -> None:
    picker, tracker = SpeakerPicker(), Tracker()
    pick, _ = feed(picker, tracker, 0.0, 3.0, who=0, speaking=False)
    assert pick is None
    solo, alone = SpeakerPicker(), Tracker()
    for i in range(20):
        alone.update(i / HZ, [person(0.5, mouth=0.05)])
        assert solo.pick(i / HZ, alone.members(i / HZ), True) is None


# -- group framing ---------------------------------------------------------------------------------------------


def test_two_people_are_both_in_the_window_with_room_to_spare() -> None:
    framer = Framer("gentle", zoom_cap=2.0, subjects="group")
    faces = [person(0.25), person(0.7)]
    track = drive(framer, 12, lambda t: faces)
    window = track[-1][1]
    assert inside(window, faces)
    left, _, right, _ = window.rect()
    assert faces[0].x - left > 0.03 and right - (faces[1].x + faces[1].w) > 0.03  # margins on both sides
    assert framer.people == 2 and framer.state == "tracking"


def test_one_mode_follows_a_single_face_as_before() -> None:
    framer = Framer("gentle", zoom_cap=2.0, subjects="one")
    drive(framer, 12, lambda t: [person(0.25), person(0.7, height=0.1)])
    assert framer.people == 1 and framer.face is not None and framer.face.cx == pytest.approx(0.25, abs=0.01)


def test_a_group_is_never_zoomed_closer_than_the_shot_allows() -> None:
    framer = Framer("tight", zoom_cap=3.0, subjects="group")
    drive(framer, 12, lambda t: [person(0.45, height=0.3), person(0.56, height=0.3)])
    assert framer.window.zoom <= framer.max_zoom + 1e-6
    wide = Framer("tight", zoom_cap=3.0, subjects="group", shot="wide")
    drive(wide, 12, lambda t: [person(0.45, height=0.3), person(0.56, height=0.3)])
    assert wide.window.zoom <= SHOTS["wide"].max_zoom + 1e-6


def test_a_passer_by_does_not_move_the_window() -> None:
    framer = Framer("gentle", zoom_cap=2.0, subjects="group")
    pair = [person(0.3), person(0.65)]
    drive(framer, 10, lambda t: pair)
    before = framer.target
    drive(framer, 0.7, lambda t: [*pair, person(0.9, height=0.12)], start=10)
    after = framer.target
    assert (after.cx, after.cy, after.zoom) == pytest.approx((before.cx, before.cy, before.zoom), abs=1e-6)


def test_a_person_who_leaves_is_kept_for_the_hold_then_the_window_closes_in() -> None:
    framer = Framer("follow", zoom_cap=2.0, subjects="group")
    pair = [person(0.2), person(0.8)]
    drive(framer, 10, lambda t: pair)
    wide = framer.window.zoom
    track = drive(framer, group.HOLD - 0.5, lambda t: [pair[0]], start=10)
    assert track[-1][1].zoom == pytest.approx(wide, abs=0.02)  # still the pair
    drive(framer, 8, lambda t: [pair[0]], start=10 + group.HOLD - 0.5)
    assert framer.window.zoom > wide + 0.1 and framer.people == 1


def test_hand_off_frames_the_speaker_and_returns_to_the_group() -> None:
    framer = Framer("follow", zoom_cap=2.5, subjects="group", handoff=True)
    drive(framer, 8, lambda t: talking_pair(t, None), speaking=False)
    group_zoom = framer.window.zoom
    drive(framer, 8, lambda t: talking_pair(t, 1), start=8, speaking=True)
    assert framer.speaker and framer.window.zoom > group_zoom + 0.1
    assert framer.window.cx > 0.55  # on the right-hand speaker
    drive(framer, group.RETURN_AFTER + 6, lambda t: talking_pair(t, None), start=16, speaking=False)
    assert not framer.speaker and framer.window.zoom == pytest.approx(group_zoom, abs=0.05)


def test_hand_off_is_off_by_default() -> None:
    framer = Framer("follow", zoom_cap=2.5, subjects="group")
    drive(framer, 12, lambda t: talking_pair(t, 1), speaking=True)
    assert not framer.speaker and framer.people == 2


# -- shots -------------------------------------------------------------------------------------------------------


def test_shots_differ_in_how_large_the_face_is() -> None:
    zooms = {}
    for name in ("close", "medium", "wide", "presenter"):
        framer = Framer("gentle", zoom_cap=3.0, shot=name)
        drive(framer, 14, lambda t: [person(0.5, height=0.1)])
        zooms[name] = framer.window.zoom
    assert zooms["close"] > zooms["medium"] > zooms["wide"] >= zooms["presenter"] >= 1.0


def test_the_presenter_shot_keeps_the_head_high() -> None:
    framer = Framer("gentle", zoom_cap=3.0, shot="presenter")
    face = person(0.5, cy=0.4, height=0.08)
    drive(framer, 14, lambda t: [face])
    _left, top, _right, bottom = framer.window.rect()
    assert (face.eyes - top) / (bottom - top) < 0.3  # the eyes are near the top of the picture


def test_a_shot_change_eases_in_and_never_moves_faster_than_the_speed_limit() -> None:
    framer = Framer("gentle", zoom_cap=3.0)
    faces = [person(0.5, height=0.15)]
    drive(framer, 10, lambda t: faces)
    framer.set_shot("close")
    track = drive(framer, 10, lambda t: faces, start=10)
    assert track[-1][1].zoom > track[0][1].zoom + 0.2
    p = framer.profile
    steps = itertools.pairwise(track)
    worst = max(abs(math.log(b.zoom / a.zoom)) * RATE for (_, a), (_, b) in steps)
    assert worst <= p.max_speed * 3 + 1e-6


def test_the_desk_shot_is_a_fixed_crop_that_needs_no_face() -> None:
    framer = Framer("gentle", zoom_cap=3.0, shot="desk")
    track = drive(framer, 12, lambda t: [])
    window = track[-1][1]
    desk = SHOTS["desk"].home
    assert desk is not None
    assert (window.cx, window.cy, window.zoom) == pytest.approx(desk, abs=0.02)
    assert framer.state == "fixed"
    drive(framer, 6, lambda t: [person(0.2)], start=12)  # faces do not pull it away
    assert framer.window.cy == pytest.approx(desk[1], abs=0.02)


def test_off_restores_the_full_frame_after_any_shot() -> None:
    for name in ("close", "presenter", "desk"):
        framer = Framer("gentle", zoom_cap=3.0, shot=name)
        drive(framer, 10, lambda t: [person(0.4, height=0.12)])
        framer.set_mode("off")
        drive(framer, 14, lambda t: [], start=10)
        assert framer.settled_full, name
        assert framer.window == FULL or framer.window.zoom == pytest.approx(1.0, abs=0.01)


def test_unknown_shot_and_subjects_are_refused() -> None:
    with pytest.raises(ValueError, match="shot"):
        Framer("gentle").set_shot("cinematic")
    with pytest.raises(ValueError, match="subjects"):
        Framer("gentle").set_subjects("crowd")


# -- losing the subject ------------------------------------------------------------------------------------------


def test_a_lost_subject_widens_slowly_then_returns_to_the_shot_home() -> None:
    framer = Framer("tight", zoom_cap=2.0)
    drive(framer, 12, lambda t: [person(0.7, height=0.18)])
    zoomed = framer.window.zoom
    drive(framer, LOST_AFTER + 0.2, lambda t: [], start=12)
    assert framer.state == "no-face"
    assert framer.target.zoom < zoomed  # widening has begun ...
    assert framer.target.zoom > 1.05  # ... but only half way
    drive(framer, LOST_HOME_AFTER + 8, lambda t: [], start=12 + LOST_AFTER + 0.2)
    assert framer.settled_full


def test_a_lost_subject_in_the_desk_shot_keeps_the_desk_crop() -> None:
    framer = Framer("gentle", zoom_cap=3.0, shot="desk")
    drive(framer, 20, lambda t: [])
    desk = SHOTS["desk"].home
    assert desk is not None and framer.window.zoom == pytest.approx(desk[2], abs=0.02)


# -- the replay clips (acceptance) ---------------------------------------------------------------------------------


def walking(n: int, seed: int):
    """n people who drift slowly around their places and sway a little: a believable meeting."""
    rng = random.Random(seed)  # noqa: S311 - a repeatable clip, not a secret
    places = {1: [0.5], 2: [0.33, 0.67], 3: [0.22, 0.5, 0.78]}[n]
    phases = [(rng.random() * 6.28, rng.random() * 6.28) for _ in places]

    def faces(t: float) -> list[Face]:
        out = []
        for x, (px, py) in zip(places, phases, strict=True):
            out.append(
                person(
                    x + 0.04 * math.sin(t / 7 + px),
                    0.42 + 0.02 * math.sin(t / 5 + py),
                    0.17 + 0.01 * math.sin(t / 9 + px),
                )
            )
        return out

    return faces


@pytest.mark.parametrize("people", [1, 2, 3])
def test_replay_every_face_stays_in_the_window_and_the_speed_limit_holds(people: int) -> None:
    framer = Framer("follow", zoom_cap=2.0, subjects="group")
    faces = walking(people, seed=people)
    track = drive(framer, 90, faces)
    settled = [(t, w) for t, w in track if t > 6]  # after the first confirmation and the move in
    ok = sum(inside(w, faces(t)) for t, w in settled)
    assert ok / len(settled) >= 0.95, f"{people} faces inside only {ok / len(settled):.1%} of the time"
    p = framer.profile
    for (_, a), (_, b) in itertools.pairwise(track):
        assert abs(b.cx - a.cx) * RATE <= p.max_speed + 0.02
        assert abs(b.cy - a.cy) * RATE <= p.max_speed + 0.02
        assert abs(math.log(b.zoom / a.zoom)) * RATE <= p.max_speed * 3 + 0.05


def test_replay_with_speakers_taking_turns_the_window_is_on_the_speaker_after_the_hand_off() -> None:
    framer = Framer("follow", zoom_cap=2.5, subjects="group", handoff=True)
    turns = lambda t: 0 if (t // 10) % 2 == 0 else 1  # noqa: E731 - the clip: alternating ten-second turns

    def faces(t: float) -> list[Face]:
        wobble = 0.08 * math.sin(t * 14) ** 2
        who = turns(t)
        return [person(0.3, mouth=wobble if who == 0 else 0.0), person(0.7, mouth=wobble if who == 1 else 0.0)]

    track = drive(framer, 60, faces, speaking=True)
    on_speaker = 0
    total = 0
    for t, w in track:
        if t < 8 or t % 10 < 6:  # a hand-off takes a few seconds on purpose (no ping-pong); judge the rest of a turn
            continue
        total += 1
        who = turns(t)
        if inside(w, [faces(t)[who]]):
            on_speaker += 1
    assert on_speaker / total >= 0.95


# -- cost (the gate is p95 under 8 ms per detection and nothing in the video path) --------------------------------


def test_observe_and_step_are_cheap_with_three_faces() -> None:
    framer = Framer("follow", zoom_cap=2.0, subjects="group", handoff=True)
    faces = walking(3, seed=7)
    costs = []
    for i in range(600):
        t = i / HZ
        started = time.perf_counter()
        framer.observe(t, faces(t), True)
        for k in range(3):
            framer.step(t + k / RATE)
        costs.append(time.perf_counter() - started)
    costs.sort()
    assert costs[int(len(costs) * 0.95)] < 0.008
