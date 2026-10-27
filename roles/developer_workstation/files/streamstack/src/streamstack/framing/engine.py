"""The framing engine: a stream of face detections in, one smooth crop window out.

Pure Python with an injected clock: no video engine, no camera, no model. Everything is in normalised frame units
(0..1 across the picture), so the same engine serves the 1056x704 Canon live view and the 1080p Cam Link.

A window is a centre (cx, cy) and a zoom (1 = the whole frame). It always has the frame's own aspect ratio, so a
scene item that scales its bounds keeps the picture undistorted and the crop reads as a zoom.

How a detection becomes motion, in order:

1. One Euro filters smooth the detector's jitter on the face centre, eye line and size.
2. The composition rule turns the smoothed face into the window an operator would frame: face centred, eyes on
   the upper third line (that is the headroom), face height a mode-specific share of the window, zoom capped by
   the mode and by how much the source can be enlarged before it looks soft.
3. A dead zone decides whether that window is worth moving to. A change smaller than the dead zone is ignored, so
   the picture does not breathe; a larger one is committed as the new target. A face about to leave the window
   forces a commit at once.
4. A critically damped spring (exact solution, any step size) moves the window to the target with no overshoot
   and a velocity limit, so there is no hunting and no whip.

Losing the face for `LOST_AFTER` seconds eases the window back to the full frame; `Framer.hold` freezes it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MODES = ("off", "gentle", "tight", "follow")

MIN_SCORE = 0.6  # detections below this are ignored
MIN_FACE = 0.03  # a face shorter than this share of the frame height is noise
MAX_FACE = 0.95
CONFIRM_HITS = 2  # a face must be seen this many times in CONFIRM_WITHIN seconds before the window moves
CONFIRM_WITHIN = 1.0
LOST_AFTER = 1.5  # seconds without a face before easing back to the full frame
EDGE_MARGIN = 0.04  # share of the window kept between the face and the window edge
MAX_STEP = 1.0  # a longer gap between steps (a stall) is treated as this long
SETTLED = 0.005  # distance from the full frame that counts as back at the full frame (under 6 px of crop)
MAX_UPSCALE = 2.0  # the most the cropped source may be enlarged on the output


@dataclass(frozen=True)
class Face:
    """One detected face, in the detector image's normalised coordinates (origin top left)."""

    x: float
    y: float
    w: float
    h: float
    score: float = 1.0
    eye_y: float | None = None  # normalised y of the eye line when the detector reports landmarks

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def eyes(self) -> float:
        """The eye line; without landmarks, where eyes sit in a typical face box (just above the middle)."""
        return self.eye_y if self.eye_y is not None else self.y + 0.4 * self.h

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass(frozen=True)
class Profile:
    """How one mode frames. All distances are shares of the frame, times are seconds."""

    name: str
    label: str
    blurb: str
    face_height: float  # wanted face height as a share of the window height
    max_zoom: float
    dead_pos: float  # the window centre must be this far off before it moves
    dead_zoom: float  # relative zoom change needed before it zooms
    smooth_pos: float  # spring settling time constants
    smooth_zoom: float
    max_speed: float  # frame shares per second for the centre; e-folds per second for the zoom
    eye_line: float = 1 / 3  # where the eyes sit from the top of the window


PROFILES: dict[str, Profile] = {
    "gentle": Profile(
        "gentle",
        "Gentle",
        "Slow, small corrections. Keeps you comfortably in the picture.",
        0.20,
        1.25,
        0.06,
        0.10,
        1.0,
        1.6,
        0.10,
    ),
    "tight": Profile(
        "tight", "Tight", "Closer on your face, still moves rarely and slowly.", 0.34, 1.8, 0.04, 0.07, 0.8, 1.2, 0.18
    ),
    "follow": Profile(
        "follow", "Follow", "Stays with you when you lean or move around.", 0.27, 1.5, 0.015, 0.05, 0.4, 0.9, 0.45
    ),
}
OFF_PROFILE = PROFILES["gentle"]  # the pace at which the window returns to the full frame


def resolution_zoom_cap(
    source_height: float, output_height: float | None = None, max_upscale: float = MAX_UPSCALE
) -> float:
    """The most a source can be zoomed before the output is enlarged more than `max_upscale` times.

    `output_height` is how tall the picture is drawn in the output (the scene item's bounds); the source's own
    height is assumed when unknown. The Canon's 704-line live view in a 1080p canvas is already enlarged 1.5x, so
    it gets about 1.3x of zoom; a 1080p Cam Link gets 2x.
    """
    if source_height <= 0:
        return 1.0
    out = output_height if output_height and output_height > 0 else source_height
    return max(1.0, max_upscale * source_height / out)


@dataclass(frozen=True)
class Window:
    cx: float
    cy: float
    zoom: float

    @property
    def size(self) -> float:
        return 1.0 / self.zoom

    def rect(self) -> tuple[float, float, float, float]:
        """(left, top, right, bottom) in frame units."""
        half = self.size / 2
        return (self.cx - half, self.cy - half, self.cx + half, self.cy + half)


FULL = Window(0.5, 0.5, 1.0)


class OneEuro:
    """The One Euro filter (Casiez et al.): smooth when slow, quick when fast, so it neither jitters nor lags."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 6.0, d_cutoff: float = 1.0) -> None:
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self._t: float | None = None
        self._x = 0.0
        self._dx = 0.0

    @staticmethod
    def _alpha(dt: float, cutoff: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self) -> None:
        self._t = None

    def __call__(self, t: float, x: float) -> float:
        if self._t is None:
            self._t, self._x, self._dx = t, x, 0.0
            return x
        dt = max(t - self._t, 1e-3)
        dx = (x - self._x) / dt
        a_d = self._alpha(dt, self.d_cutoff)
        self._dx = a_d * dx + (1 - a_d) * self._dx
        a = self._alpha(dt, self.min_cutoff + self.beta * abs(self._dx))
        self._x = a * x + (1 - a) * self._x
        self._t = t
        return self._x


class Spring:
    """A critically damped spring toward a target: no overshoot, exact for any step, with a speed limit."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.target = value
        self.velocity = 0.0

    def step(self, dt: float, smooth: float, max_speed: float) -> float:
        omega = 2.0 / max(smooth, 1e-3)
        delta = self.value - self.target
        decay = math.exp(-omega * dt)
        carried = self.velocity + omega * delta
        self.value = self.target + (delta + carried * dt) * decay
        self.velocity = (self.velocity - omega * carried * dt) * decay
        self.velocity = max(-max_speed, min(max_speed, self.velocity))
        return self.value

    def freeze(self) -> None:
        self.target = self.value
        self.velocity = 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _overlap(a: Face, b: Face) -> float:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a.area + b.area - inter)


class Framer:
    """One camera's framing: feed it detections with `observe`, read the window with `step`.

    Not thread-safe; the owner serialises calls. `t` is any monotonic clock in seconds.
    """

    def __init__(self, mode: str = "off", zoom_cap: float = MAX_UPSCALE) -> None:
        self.zoom_cap = max(1.0, zoom_cap)
        self._mode = "off"
        self._held = False
        self._cx = Spring(0.5)
        self._cy = Spring(0.5)
        self._lz = Spring(0.0)  # log zoom: zooming in and out are equally fast
        self._f_cx = OneEuro()
        self._f_eye = OneEuro()
        self._f_h = OneEuro()
        self._last_step: float | None = None
        self._last_seen: float | None = None
        self._hits: list[float] = []
        self._tracking = False
        self._face: Face | None = None
        self.set_mode(mode)

    # -- configuration --------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def held(self) -> bool:
        return self._held

    @property
    def profile(self) -> Profile:
        return PROFILES.get(self._mode, OFF_PROFILE)

    @property
    def max_zoom(self) -> float:
        return min(self.profile.max_zoom, self.zoom_cap)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown framing mode {mode!r}")
        if mode == self._mode:
            return
        was = self._mode
        self._mode = mode
        if mode == "off" or was == "off":
            self._tracking = False  # a change between two framing modes keeps following the same face
            self._hits.clear()
        if mode == "off":
            self._go_full()
        elif self._held:
            self._freeze()

    def set_zoom_cap(self, cap: float) -> None:
        self.zoom_cap = max(1.0, cap)

    def set_hold(self, on: bool) -> None:
        if on == self._held:
            return
        self._held = on
        if on:
            self._freeze()
        else:
            # Resume from the held crop: a face that is still there carries on, one that left times out as usual.
            self._tracking = self._face is not None
            self._last_seen = self._last_step
            self._hits.clear()

    def resume(self, t: float) -> None:
        """Carry on after the camera was off air: the gap is not "the face was lost"."""
        self._last_step = t
        if self._tracking:
            self._last_seen = t

    def reset(self) -> None:
        """Back to the full frame now (the mode stays); tracking starts again at the next face."""
        self._tracking = False
        self._hits.clear()
        self._go_full()

    # -- state ------------------------------------------------------------------

    @property
    def state(self) -> str:
        if self._mode == "off":
            return "off"
        if self._held:
            return "held"
        return "tracking" if self._tracking else "no-face"

    @property
    def window(self) -> Window:
        """The current window, kept inside the frame while the springs are still catching up with each other."""
        zoom = math.exp(max(self._lz.value, 0.0))
        half = 0.5 / zoom
        return Window(_clamp(self._cx.value, half, 1 - half), _clamp(self._cy.value, half, 1 - half), zoom)

    @property
    def target(self) -> Window:
        return Window(self._cx.target, self._cy.target, math.exp(self._lz.target))

    @property
    def face(self) -> Face | None:
        return self._face if self._tracking else None

    @property
    def settled_full(self) -> bool:
        """The window is back on the whole frame and nothing is pulling it away."""
        w = self.window
        return (
            abs(w.zoom - 1) < SETTLED
            and abs(w.cx - 0.5) < SETTLED
            and abs(w.cy - 0.5) < SETTLED
            and (self._mode == "off" or (not self._tracking and not self._held))
        )

    # -- input ------------------------------------------------------------------

    def observe(self, t: float, faces: list[Face]) -> None:
        """One detector result (possibly empty) taken at time `t`."""
        if self._mode == "off" or self._held:
            return
        face = self._pick([f for f in faces if self._plausible(f)])
        if face is None:
            return
        if not self._tracking:
            self._hits = [h for h in self._hits if t - h <= CONFIRM_WITHIN] + [t]
            if len(self._hits) < CONFIRM_HITS:
                return
            self._tracking = True
            if self._last_seen is None or t - self._last_seen > LOST_AFTER:
                for f in (self._f_cx, self._f_eye, self._f_h):
                    f.reset()
        self._last_seen = t
        self._face = face
        cx = self._f_cx(t, face.cx)
        eye = self._f_eye(t, face.eyes)
        height = self._f_h(t, face.h)
        self._compose(cx, eye, height)

    @staticmethod
    def _plausible(face: Face) -> bool:
        return face.score >= MIN_SCORE and MIN_FACE <= face.h <= MAX_FACE and face.w > 0

    def _pick(self, faces: list[Face]) -> Face | None:
        """The largest face, preferring the one already followed so two people do not make it flip."""
        if not faces:
            return None
        previous = self._face if self._tracking else None

        def weight(f: Face) -> float:
            return f.area * (1.5 if previous is not None and _overlap(f, previous) > 0.1 else 1.0)

        return max(faces, key=weight)

    # -- composition ------------------------------------------------------------

    def desired(self, cx: float, eye: float, height: float) -> Window:
        """The window an operator would frame for a face at (cx, eye line) of the given height."""
        p = self.profile
        zoom = _clamp(p.face_height / max(height, 1e-3), 1.0, self.max_zoom)
        size = 1.0 / zoom
        half = size / 2
        wx = _clamp(cx, half, 1 - half)
        wy = _clamp(eye + (0.5 - p.eye_line) * size, half, 1 - half)
        return Window(wx, wy, zoom)

    def _compose(self, cx: float, eye: float, height: float) -> None:
        p = self.profile
        want = self.desired(cx, eye, height)
        have = self.target
        moved = (
            abs(want.cx - have.cx) > p.dead_pos
            or abs(want.cy - have.cy) > p.dead_pos
            or abs(math.log(want.zoom / have.zoom)) > p.dead_zoom
            or self._near_edge(have)
        )
        if not moved:
            return
        self._cx.target, self._cy.target, self._lz.target = want.cx, want.cy, math.log(want.zoom)

    def _near_edge(self, window: Window) -> bool:
        face = self._face
        if face is None:
            return False
        left, top, right, bottom = window.rect()
        margin = EDGE_MARGIN * window.size
        return (
            face.x < left + margin
            or face.x + face.w > right - margin
            or face.y < top + margin
            or face.y + face.h > bottom - margin
        ) and window.zoom > 1.0 + 1e-6

    # -- output -----------------------------------------------------------------

    def step(self, t: float) -> Window:
        """Advance to time `t` and return the window. Call as often as the output is updated."""
        if self._last_step is None:
            self._last_step = t
            return self.window
        dt = min(max(t - self._last_step, 0.0), MAX_STEP)
        self._last_step = t
        if dt <= 0:
            return self.window
        lost = self._tracking and self._last_seen is not None and t - self._last_seen > LOST_AFTER
        if lost and self._mode != "off" and not self._held:
            self._tracking = False
            self._hits.clear()
            self._go_full()
        p = self.profile
        slow = 1.0 if self._mode != "off" and self._tracking else 1.5  # the return to full frame is unhurried
        self._cx.step(dt, p.smooth_pos * slow, p.max_speed)
        self._cy.step(dt, p.smooth_pos * slow, p.max_speed)
        self._lz.step(dt, p.smooth_zoom * slow, p.max_speed * 3)
        self._lz.value = max(0.0, self._lz.value)  # the spring never zooms out past the full frame
        return self.window

    def _go_full(self) -> None:
        self._cx.target, self._cy.target, self._lz.target = 0.5, 0.5, 0.0

    def _freeze(self) -> None:
        for spring in (self._cx, self._cy, self._lz):
            spring.freeze()
