"""The framing window as a crop of the camera picture: margins in source pixels, and back.

The native engine applies the crop in its libplacebo graph (`streamstack.engine.cropper`), so the picture is
cropped and rescaled on the GPU as part of the frame it was drawing anyway. The detector reads the engine's own
uncropped low-rate frame, so the crop never feeds back into detection.

The crop keeps the frame's aspect ratio, so the picture is zoomed, not distorted. The base crop of an item is the
crop it had before framing touched it (none, for the engine); the window is placed inside the base picture and
the base is restored when framing stops. Nothing is persisted: a new engine child always starts from the full
frame, so a crash cannot leave the picture cropped for good.
"""

from __future__ import annotations

from dataclasses import dataclass

from streamstack.framing.engine import Face, Window

MIN_SIDE = 32  # pixels left after cropping, a guard against a bad window blanking the picture


@dataclass(frozen=True)
class Crop:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0


@dataclass
class Item:
    """A picture the framing window may crop (for the engine: the one camera it reads)."""

    scene: str
    item_id: int
    source: str
    width: int  # the source's pixels
    height: int
    out_height: float  # how tall the uncropped base picture is drawn in the output, in output pixels
    base: Crop
    applied: Crop

    @property
    def key(self) -> str:
        return f"{self.scene}\x1f{self.item_id}"

    @property
    def frame_width(self) -> int:
        return max(1, self.width - self.base.left - self.base.right)

    @property
    def frame_height(self) -> int:
        return max(1, self.height - self.base.top - self.base.bottom)


def window_to_crop(window: Window, item: Item) -> Crop:
    """The crop that shows `window` (normalised to the base picture) of an item."""
    left, top, right, bottom = window.rect()
    fw, fh = item.frame_width, item.frame_height
    base = item.base
    l_px = max(0, round(left * fw))
    t_px = max(0, round(top * fh))
    r_px = max(0, round((1 - right) * fw))
    b_px = max(0, round((1 - bottom) * fh))
    # never leave less than MIN_SIDE of the picture
    if fw - l_px - r_px < MIN_SIDE or fh - t_px - b_px < MIN_SIDE:
        return base
    return Crop(base.left + l_px, base.top + t_px, base.right + r_px, base.bottom + b_px)


def face_to_frame(face: Face, item: Item) -> Face:
    """A face found in the whole source, re-expressed in the item's base picture."""
    fw, fh = item.frame_width, item.frame_height
    ox, oy = item.base.left, item.base.top
    sx, sy = item.width / fw, item.height / fh
    eye = None if face.eye_y is None else (face.eye_y * item.height - oy) / fh
    return Face(
        (face.x * item.width - ox) / fw,
        (face.y * item.height - oy) / fh,
        face.w * sx,
        face.h * sy,
        face.score,
        eye,
    )
