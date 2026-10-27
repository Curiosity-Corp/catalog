"""The auto-framing crop, written to the engine.

FramingController asks a cropper to `discover` the picture it may crop, to `apply` a crop (margins in source
pixels) and to `restore` the full frame. Here there is one picture, the camera the engine is
reading, and a crop is `VideoEngine.set_window`: libplacebo's crop expressions change between two frames, so the
window moves without a restart. Nothing is persisted: a new engine child always starts from the full frame.
"""

from __future__ import annotations

from collections.abc import Callable

from streamstack.engine.graph import Rect, Source
from streamstack.engine.process import VideoEngine
from streamstack.framing.crop import Crop, Item

SCENE = "Program"


class EngineCropper:
    def __init__(self, engine: Callable[[], VideoEngine | None], source: Callable[[], Source | None]) -> None:
        self._engine = engine
        self._source = source
        self.skipped: dict[str, str] = {}

    def discover(self, scenes: list[str], cameras: set[str]) -> list[Item]:
        source, engine = self._source(), self._engine()
        if source is None or engine is None or source.label not in cameras or SCENE not in scenes:
            return []
        rect = engine.state()["crop"]
        applied = Crop()
        if rect:
            applied = Crop(
                rect["x"], rect["y"], source.width - rect["x"] - rect["w"], source.height - rect["y"] - rect["h"]
            )
        out_height = float(engine.spec.out_height)
        return [Item(SCENE, 1, source.label, source.width, source.height, out_height, Crop(), applied)]

    def apply(self, item: Item, crop: Crop) -> None:
        engine = self._engine()
        if engine is None:
            return
        full = Crop()
        if crop == full:
            engine.set_window(None)
        else:
            x, y = crop.left - crop.left % 2, crop.top - crop.top % 2
            w = (item.width - crop.left - crop.right) // 2 * 2
            h = (item.height - crop.top - crop.bottom) // 2 * 2
            engine.set_window(Rect(x, y, w, h))
        item.applied = crop

    def restore(self, item: Item) -> None:
        if item.applied != item.base:
            self.apply(item, item.base)
