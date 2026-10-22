"""A real face detector for the director's subject finder.

The director finds the subject in each still of a burst (`imagefeatures.find_subject`) to judge headroom and
centring and to meter the face alone. Out of the box that is a skin-colour blob, which is fooled by a hand or a
wooden door. When the optional framing extra is installed (`pip install 'streamstack[framing]'`) and the YuNet model
is fetched (`streamstack framing --fetch`), `install` puts this finder in front of the blob: the same YuNet model
and backends the auto-framing engine uses (`streamstack.framing.detector`), so one model serves both.

Rules:

* the finder is lazy: nothing heavy is imported or opened until the first still is analysed, and a missing
  package, a missing or corrupt model or a failing backend makes it answer None, so the skin blob (or Claude's box)
  answers instead; it logs the reason once and tries again later;
* it runs on the CPU by default, because the director looks at a handful of stills every few minutes and should not
  contend with the live framing engine for the GPU (STREAMSTACK_FRAMING_DEVICE applies to the engine, not here);
* it reads the full-size still, never anything that leaves the machine, and never writes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from streamstack import config
from streamstack.director import imagefeatures
from streamstack.framing import detector, health
from streamstack.framing.engine import Face

log = logging.getLogger(__name__)

RETRY_SECONDS = 600.0
MIN_SCORE = 0.6
Opener = Callable[[str], detector.Detector]


def subject_from_face(face: Face) -> dict:
    """A subject record (the shape the skin finder returns) from a detected face, clipped to the picture."""
    x, y = max(0.0, min(1.0, face.x)), max(0.0, min(1.0, face.y))
    w, h = max(0.0, min(1.0 - x, face.w)), max(0.0, min(1.0 - y, face.h))
    return {
        "source": "yunet",
        "confidence": "high" if face.score >= 0.8 else "medium",
        "score": round(face.score, 2),
        "box": [round(v, 3) for v in (x, y, w, h)],
        "centre": [round(x + w / 2, 3), round(y + h / 2, 3)],
        "height": round(h, 3),
        "headroom": round(y, 3),
        "off_centre": round(abs(x + w / 2 - 0.5), 3),
    }


class FaceFinder:
    """A `SUBJECT_FINDERS` entry that returns the largest confident face in the still, or None."""

    def __init__(
        self,
        opener: Opener = lambda device: detector.open_detector(device),
        device: str = "cpu",
        clock: Callable[[], float] = time.monotonic,
        retry: float = RETRY_SECONDS,
    ) -> None:
        self._opener, self._device, self._clock, self._retry = opener, device, clock, retry
        self._detector: detector.Detector | None = None
        self._failed_at: float | None = None
        self.reason = ""

    def _open(self) -> detector.Detector | None:
        if self._detector is not None:
            return self._detector
        now = self._clock()
        if self._failed_at is not None and now - self._failed_at < self._retry:
            return None
        try:
            self._detector = self._opener(self._device)
        except detector.DetectorUnavailable as exc:
            self._fail(str(exc))
            return None
        self._failed_at, self.reason = None, ""
        return self._detector

    def _fail(self, reason: str) -> None:
        if reason != self.reason:
            log.warning("director face finder is not available, using the skin-colour heuristic: %s", reason)
        self.reason, self._failed_at = reason, self._clock()

    def __call__(self, scene: imagefeatures.Scene) -> dict | None:
        if scene.source is None:
            return None
        found = self._open()
        if found is None:
            return None
        try:
            jpeg = Path(scene.source).read_bytes()
        except OSError as exc:
            log.warning("director face finder could not read %s: %s", scene.source, exc.strerror)
            return None
        try:
            faces = found.detect(jpeg)
        except (ValueError, RuntimeError, OSError) as exc:
            self._detector, _ = None, found.close()
            self._fail(f"detection failed: {exc}")
            return None
        faces = [f for f in faces if f.score >= MIN_SCORE and f.w > 0 and f.h > 0]
        if not faces:
            return None
        return subject_from_face(max(faces, key=lambda f: f.w * f.h * f.score))

    def close(self) -> None:
        if self._detector is not None:
            self._detector.close()
            self._detector = None

    def describe(self) -> dict:
        """The Auto page row for this finder (same shape as an actuator's `describe`)."""
        if self._detector is not None:
            return {**ROW, "available": True, "reason": f"YuNet on {self._detector.device}"}
        return (
            {**ROW, "available": True, "reason": "YuNet loads on first use"}
            if not self.reason
            else {
                **ROW,
                "available": False,
                "reason": f"using the skin-colour heuristic: {self.reason}",
            }
        )


ROW = {"id": "face-finder", "label": "Face finder", "kind": "sensor", "params": []}


def heuristic_row(reason: str) -> dict:
    return {**ROW, "available": False, "reason": f"using the skin-colour heuristic: {reason}"}


def install(cfg: dict[str, str], finders: list | None = None) -> tuple[FaceFinder | None, dict]:
    """Put the face finder in front of the skin blob when the extra and the model are in place.

    Returns (the finder or None, the Auto page row). Cheap: it imports and opens nothing heavy.
    """
    finders = imagefeatures.SUBJECT_FINDERS if finders is None else finders
    if not config.truthy(cfg.get("STREAMSTACK_AUTO_FACES", "true")):
        return None, heuristic_row("face detection is off (STREAMSTACK_AUTO_FACES)")
    for existing in finders:
        if isinstance(existing, FaceFinder):
            return existing, existing.describe()
    ready, why, hint = health.readiness()
    if not ready:
        return None, heuristic_row(f"{why}; {hint}" if hint else why)
    finder = FaceFinder()
    finders.insert(0, finder)
    return finder, finder.describe()
