"""The Stream Deck models StreamStack lays keys out for: a grid, and the pixel size of a key image.

The numbers match the `streamdeck` library's classes (KEY_ROWS, KEY_COLS, KEY_PIXEL_WIDTH). A deck the table does not
know is still usable: `for_device` builds a model from the grid the library reports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    rows: int
    cols: int
    size: int  # key image edge, pixels

    @property
    def keys(self) -> int:
        return self.rows * self.cols

    @property
    def grid(self) -> str:
        return f"{self.cols}×{self.rows}"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "rows": self.rows,
            "cols": self.cols,
            "size": self.size,
            "keys": self.keys,
        }


MODELS: tuple[Model, ...] = (
    Model("virtual-4x4", "Virtual deck", 4, 4, 96),
    Model("mini", "Stream Deck Mini", 2, 3, 80),
    Model("neo", "Stream Deck Neo", 2, 4, 96),
    Model("original", "Stream Deck MK.2", 3, 5, 72),
    Model("plus", "Stream Deck +", 2, 4, 120),
    Model("xl", "Stream Deck XL", 4, 8, 96),
)
DEFAULT_MODEL = MODELS[0]
MAX_ROWS = MAX_COLS = 8
BY_ID = {m.id: m for m in MODELS}

# What the library calls its decks (StreamDeck.DECK_TYPE) -> our model id.
LIBRARY_NAMES = {
    "Stream Deck Mini": "mini",
    "Stream Deck Neo": "neo",
    "Stream Deck Original": "original",
    "Stream Deck +": "plus",
    "Stream Deck XL": "xl",
}


def for_grid(rows: int, cols: int) -> Model:
    """The known model with this grid (the first listed), or a custom one."""
    for model in MODELS:
        if (model.rows, model.cols) == (rows, cols):
            return model
    return Model(f"custom-{cols}x{rows}", f"{cols}×{rows} deck", rows, cols, 96)


def for_device(deck_type: str, rows: int, cols: int, size: int) -> Model:
    """The model of a connected deck, from the library's type name and key layout."""
    known = BY_ID.get(LIBRARY_NAMES.get(deck_type, ""))
    if known is not None and (known.rows, known.cols) == (rows, cols):
        return Model(known.id, known.name, rows, cols, size or known.size)
    return Model(f"custom-{cols}x{rows}", deck_type or f"{cols}×{rows} deck", rows, cols, size or 96)
