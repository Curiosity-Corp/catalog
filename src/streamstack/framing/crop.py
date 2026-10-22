"""Applying a framing window to OBS: the crop of a scene item, nothing else.

Why the scene item's crop: OBS applies it in the compositor, so the picture is cropped and rescaled on the GPU
as part of the frame it was drawing anyway. No frame is held back, nothing sits between OBS and the virtual
camera, and the camera's own device is never opened a second time. The detector reads the same source through
GetSourceScreenshot, which is the uncropped source, so the crop never feeds back into detection.

The crop keeps the frame's aspect ratio, so an item whose bounds scale the picture (the shipped scenes use
"scale to inner bounds") shows it zoomed, not distorted. An item with no bounds would shrink instead of zoom and
one with "maximum size only" cannot enlarge, so those are left alone and reported.

Whatever crop the item had before StreamStack touched it is its base: the window is placed inside the base
picture, and the base is restored when framing stops. The base is also written to disk (`RestoreLog`) the moment
an item is first touched, so a crash cannot leave the picture cropped for good.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from streamstack.framing.engine import Face, Window

log = logging.getLogger(__name__)

Call = Callable[..., dict]
MIN_SIDE = 32  # pixels left after cropping, a guard against a bad window blanking the picture
ZOOMING_BOUNDS = (
    "OBS_BOUNDS_STRETCH",
    "OBS_BOUNDS_SCALE_INNER",
    "OBS_BOUNDS_SCALE_OUTER",
    "OBS_BOUNDS_SCALE_TO_WIDTH",
    "OBS_BOUNDS_SCALE_TO_HEIGHT",
)


@dataclass(frozen=True)
class Crop:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def as_request(self) -> dict[str, int]:
        return {"cropLeft": self.left, "cropTop": self.top, "cropRight": self.right, "cropBottom": self.bottom}

    @classmethod
    def from_transform(cls, transform: dict) -> Crop:
        return cls(
            int(transform.get("cropLeft", 0)),
            int(transform.get("cropTop", 0)),
            int(transform.get("cropRight", 0)),
            int(transform.get("cropBottom", 0)),
        )


@dataclass
class Item:
    """A scene item that shows one camera source."""

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


def drawn_height(transform: dict, frame_width: int, frame_height: int) -> float | None:
    """How tall the (base-cropped) picture is drawn, from the item's bounds or scale; None if it cannot zoom."""
    kind = str(transform.get("boundsType", "OBS_BOUNDS_NONE"))
    if kind not in ZOOMING_BOUNDS:
        return None  # no bounds: the crop would shrink the picture; "max size only" cannot enlarge it
    bw, bh = float(transform.get("boundsWidth", 0)), float(transform.get("boundsHeight", 0))
    if bw <= 0 or bh <= 0:
        return None
    if kind in ("OBS_BOUNDS_STRETCH", "OBS_BOUNDS_SCALE_TO_HEIGHT"):
        return bh
    if kind == "OBS_BOUNDS_SCALE_TO_WIDTH":
        return frame_height * bw / frame_width
    fit = (
        min(bw / frame_width, bh / frame_height)
        if kind == "OBS_BOUNDS_SCALE_INNER"
        else max(bw / frame_width, bh / frame_height)
    )
    return frame_height * fit


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


class RestoreLog:
    """The crops to put back, on disk, so a crash cannot strand a cropped picture."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("framing restore log %s is unreadable: %s", self.path, exc)
            return {}
        entries = data.get("items") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def save(self, entries: dict[str, dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not entries:
                self.path.unlink(missing_ok=True)
                return
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".framing-", suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump({"items": entries}, handle)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("framing restore log %s cannot be written: %s", self.path, exc)


class ObsCropper:
    """Reads scene items and writes crops through `call(request, **data)` (the shared obs-websocket link)."""

    def __init__(self, call: Call, restore: RestoreLog | None = None) -> None:
        self._call = call
        self._restore = restore
        self._pending: dict[str, dict] = restore.load() if restore else {}
        self.skipped: dict[str, str] = {}  # item key -> why it is not framed

    def discover(self, scenes: list[str], cameras: set[str]) -> list[Item]:
        """Every enabled scene item in `scenes` that shows one of the `cameras`."""
        items: list[Item] = []
        self.skipped = {}
        for scene in scenes:
            try:
                listing = self._call("GetSceneItemList", sceneName=scene).get("sceneItems", [])
            except RuntimeError as exc:
                log.debug("framing: cannot list %s: %s", scene, exc)
                continue
            for entry in listing:
                source = entry.get("sourceName")
                if source not in cameras or not entry.get("sceneItemEnabled", True):
                    continue
                item_id = int(entry["sceneItemId"])
                try:
                    transform = self._call("GetSceneItemTransform", sceneName=scene, sceneItemId=item_id)[
                        "sceneItemTransform"
                    ]
                except (RuntimeError, KeyError) as exc:
                    log.debug("framing: cannot read %s/%s: %s", scene, source, exc)
                    continue
                item = self._item(scene, item_id, str(source), transform)
                if item is not None:
                    items.append(item)
        return items

    def _item(self, scene: str, item_id: int, source: str, transform: dict) -> Item | None:
        key = f"{scene}\x1f{item_id}"
        width, height = int(transform.get("sourceWidth", 0)), int(transform.get("sourceHeight", 0))
        if width <= 0 or height <= 0:
            self.skipped[key] = f"{source} in {scene} has no picture yet"
            return None
        current = Crop.from_transform(transform)
        remembered = self._pending.get(key)
        base = Crop(**remembered["base"]) if remembered and remembered.get("source") == source else current
        probe = Item(scene, item_id, source, width, height, 0.0, base, current)
        out = drawn_height(transform, probe.frame_width, probe.frame_height)
        if out is None:
            self.skipped[key] = f"{source} in {scene} has no bounds that scale it, so a crop would shrink it"
            return None
        probe.out_height = out
        return probe

    def apply(self, item: Item, crop: Crop) -> None:
        """Write the crop. The base is logged first, so even a failed write leaves a way back."""
        if item.key not in self._pending and crop != item.base:
            self._pending[item.key] = {"source": item.source, "base": vars(item.base)}
            self._save()
        self._call(
            "SetSceneItemTransform",
            sceneName=item.scene,
            sceneItemId=item.item_id,
            sceneItemTransform=crop.as_request(),
        )
        item.applied = crop
        if crop == item.base and item.key in self._pending:
            del self._pending[item.key]
            self._save()

    def restore(self, item: Item) -> None:
        if item.applied != item.base:
            self.apply(item, item.base)

    def recover(self, scenes: list[str]) -> int:
        """Put back crops a previous run left behind (it crashed); returns how many items were fixed."""
        fixed = 0
        for key, entry in list(self._pending.items()):
            scene, _, raw_id = key.partition("\x1f")
            try:
                item_id = int(raw_id)
                base = Crop(**entry["base"])
                if scene not in scenes:
                    continue
                self._call(
                    "SetSceneItemTransform", sceneName=scene, sceneItemId=item_id, sceneItemTransform=base.as_request()
                )
                fixed += 1
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                log.warning("framing: could not restore %s: %s", key.replace("\x1f", "/"), exc)
            self._pending.pop(key, None)
        self._save()
        return fixed

    def _save(self) -> None:
        if self._restore:
            self._restore.save(self._pending)
