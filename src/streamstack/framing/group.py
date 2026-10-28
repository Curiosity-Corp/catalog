"""Who is in the picture and who is speaking: face tracks and the speaker pick for group framing.

Pure Python with an injected time: no detector, no camera, no audio device. `Tracker` gives every face a stable id from
one detection to the next (greedy matching on overlap and distance, so it needs nothing but the boxes the detector
already returns). A track becomes a *member* of the group only after it has been seen a few times over a second, and
stays one for `HOLD` seconds after its last detection: a passer-by, one missed detection or a head turn never changes
the group, which is what stops the window hunting.

`SpeakerPicker` decides whose turn it is. The microphone says only *that* someone is talking, so the face is found from
the picture: while the voice is active the track whose mouth moves most (the corners of the mouth against the eye line,
from the detector's landmarks) is the speaker. A voice direction (0 left to 1 right across the picture, from a stereo
microphone) breaks a near tie and is the only evidence for a face with no landmarks. A new speaker must lead for
`SWITCH_AFTER` seconds before the shot moves, and the shot goes back to the group `RETURN_AFTER` seconds after the voice
stops.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the engine imports this module; a face is only read here
    from streamstack.framing.engine import Face

HOLD = 3.0  # a member stays in the group this long after its last detection
MEMBER_AFTER = 1.0  # seconds a new face must have been around before it joins the group
MEMBER_HITS = 3  # and be detected at least this many times
MAX_TRACKS = 8
MATCH_IOU = 0.1
MATCH_DISTANCE = 0.8  # centre distance, in face widths, that still counts as the same face

ACTIVITY_WINDOW = 1.5  # seconds of mouth samples the activity is measured over
ACTIVITY_MIN_SAMPLES = 4
ACTIVITY_FLOOR = 0.004  # below this a mouth is not moving (landmark jitter at rest)
TIE_RATIO = 1.4  # a face must beat the next by this factor for the mouth alone to decide
SWITCH_AFTER = 1.0  # a new speaker must lead this long before the shot moves to them
RETURN_AFTER = 4.0  # back to the group this long after the voice stops


def _iou(a: Face, b: Face) -> float:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a.area + b.area - inter)


def _spread(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


@dataclass
class Track:
    id: int
    face: Face
    first_seen: float
    last_seen: float
    hits: int = 1
    mouth: deque[tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=64))

    def member(self, t: float) -> bool:
        """Part of the group: seen enough, and seen recently."""
        return self.hits >= MEMBER_HITS and t - self.first_seen >= MEMBER_AFTER and t - self.last_seen <= HOLD

    def present(self, t: float) -> bool:
        return t - self.last_seen <= HOLD

    def activity(self, t: float) -> float | None:
        """How much the mouth moved over the last seconds (face heights, unitless); None without landmarks."""
        recent = [(a, b) for when, a, b in self.mouth if t - when <= ACTIVITY_WINDOW]
        if len(recent) < ACTIVITY_MIN_SAMPLES:
            return None
        return _spread([a for a, _ in recent]) + _spread([b for _, b in recent])


class Tracker:
    """Stable ids for the faces of one camera."""

    def __init__(self) -> None:
        self._tracks: dict[int, Track] = {}
        self._next = 1

    def reset(self) -> None:
        self._tracks.clear()

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def members(self, t: float) -> list[Track]:
        return [k for k in self._tracks.values() if k.member(t)]

    def update(self, t: float, faces: list[Face]) -> None:
        """Match one detector result to the tracks; unmatched faces start tracks, silent tracks time out."""
        pairs = []
        for track in self._tracks.values():
            for index, face in enumerate(faces):
                overlap = _iou(track.face, face)
                gap = math.hypot(track.face.cx - face.cx, track.face.cy - face.cy) / max(track.face.w, 1e-3)
                if overlap > MATCH_IOU or gap < MATCH_DISTANCE:
                    pairs.append((overlap - 0.05 * gap, track.id, index))
        used_tracks: set[int] = set()
        used_faces: set[int] = set()
        for _, track_id, index in sorted(pairs, reverse=True):
            if track_id in used_tracks or index in used_faces:
                continue
            used_tracks.add(track_id)
            used_faces.add(index)
            track = self._tracks[track_id]
            track.face, track.last_seen, track.hits = faces[index], t, track.hits + 1
            self._sample(track, faces[index], t)
        for index, face in enumerate(faces):
            if index in used_faces or len(self._tracks) >= MAX_TRACKS:
                continue
            track = Track(self._next, face, t, t)
            self._next += 1
            self._tracks[track.id] = track
            self._sample(track, face, t)
        for track_id in [k for k, v in self._tracks.items() if t - v.last_seen > HOLD * 2]:
            del self._tracks[track_id]

    @staticmethod
    def _sample(track: Track, face: Face, t: float) -> None:
        if face.mouth_y is None or face.mouth_w is None or face.h <= 0 or face.w <= 0:
            return
        eye = face.eyes
        track.mouth.append((t, (face.mouth_y - eye) / face.h, face.mouth_w / face.w))


class SpeakerPicker:
    """Whose turn it is, with hold timers so the shot does not ping-pong between two people."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._speaker: int | None = None
        self._candidate: int | None = None
        self._candidate_since = 0.0
        self._voice_until = float("-inf")
        self.confident = False

    @property
    def speaker(self) -> int | None:
        return self._speaker

    def pick(self, t: float, members: list[Track], speaking: bool | None, voice_x: float | None = None) -> int | None:
        """The id of the speaker to frame, or None for the whole group."""
        if speaking:
            self._voice_until = t
        ids = {m.id for m in members}
        if self._speaker is not None and self._speaker not in ids:
            self._speaker = None  # they left
        if len(members) < 2:
            self._speaker = self._candidate = None
            return None
        if t - self._voice_until > RETURN_AFTER:
            self._speaker = self._candidate = None
            return None
        if not speaking:
            return self._speaker  # between sentences the shot stays where it was
        best = self._best(t, members, voice_x)
        if best is None:
            return self._speaker
        if self._speaker is None:
            self._speaker, self._candidate = best, None
            return best
        if best == self._speaker:
            self._candidate = None
            return best
        if self._candidate != best:
            self._candidate, self._candidate_since = best, t
        elif t - self._candidate_since >= SWITCH_AFTER:
            self._speaker, self._candidate = best, None
        return self._speaker

    def _best(self, t: float, members: list[Track], voice_x: float | None) -> int | None:
        scored = [(m, m.activity(t)) for m in members]
        moving = sorted(((a, m) for m, a in scored if a is not None and a >= ACTIVITY_FLOOR), key=lambda p: -p[0])
        if moving:
            if len(moving) == 1 or moving[0][0] >= TIE_RATIO * moving[1][0] or voice_x is None:
                self.confident = len(moving) == 1 or moving[0][0] >= TIE_RATIO * moving[1][0]
                return moving[0][1].id
            near = [m for a, m in moving if a >= moving[0][0] / TIE_RATIO]
            self.confident = True
            return min(near, key=lambda m: abs(m.face.cx - voice_x)).id
        if voice_x is not None:  # no mouth to look at (no landmarks, a mask): the voice direction alone
            self.confident = True
            return min(members, key=lambda m: abs(m.face.cx - voice_x)).id
        self.confident = False
        return None
