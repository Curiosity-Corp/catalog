"""Key pictures: what a key shows on the deck and in the Console's preview.

One function draws a key from its view (`catalog.view_of`): a dark cap with the icon and label, or the whole cap
in the key's tone while it is lit. Only Pillow is needed (the `deck` extra); `to_native` turns the picture into the
bytes a device wants from its `key_image_format()`, so the `streamdeck` library's own image helpers are not needed.
"""

from __future__ import annotations

import io
from collections import OrderedDict
from functools import lru_cache
from typing import Any

from streamstack.deck import icons

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # the deck extra is optional; callers check `available()`
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

# The Console's theme tokens (theme.css, dark): the deck matches the page it is edited on.
BG_TOP, BG_BOTTOM = (26, 31, 41), (14, 17, 23)
INK, INK_DIM, INK_ON_TONE = (236, 240, 247), (139, 149, 166), (9, 11, 15)
TONES = {
    "red": ((255, 74, 92), (200, 32, 52), (255, 255, 255)),
    "amber": ((255, 196, 77), (235, 160, 30), INK_ON_TONE),
    "green": ((70, 226, 148), (40, 190, 112), INK_ON_TONE),
    "accent": ((110, 172, 255), (70, 138, 240), INK_ON_TONE),
}
SCALE = 3  # supersampling for the cap's own shapes (text is drawn once, at the final size)


def available() -> bool:
    """True when Pillow is installed (the key pictures need it; the Console page has a fallback without)."""
    return Image is not None


@lru_cache(maxsize=64)
def _font(px: int) -> Any:
    try:
        return ImageFont.load_default(size=px)
    except (TypeError, OSError):  # Pillow before 10.1 has only the bitmap font
        return ImageFont.load_default()


def _width(text: str, font: Any) -> float:
    return font.getlength(text)


def _wrap(text: str, font: Any, limit: float, lines: int) -> list[str]:
    """`text` over at most `lines` lines no wider than `limit`; the last line is cut with an ellipsis if need be."""
    words, out, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if _width(trial, font) <= limit or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    out.append(cur)
    if len(out) > lines:
        out = [*out[: lines - 1], " ".join(out[lines - 1 :])]
    fitted = []
    for line in out:
        while _width(line, font) > limit and len(line) > 1:
            line = line[:-1].rstrip()
            if _width(line + "…", font) <= limit:
                line += "…"
                break
        fitted.append(line)
    return fitted


def _gradient(size: int, top: tuple, bottom: tuple):
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        strip.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return strip.resize((size, size))


def render(view: dict, size: int = 96):
    """The key as a square RGB Pillow image, `size` px."""
    if not available():
        raise RuntimeError("Pillow is not installed: pip install 'streamstack[deck]'")
    if view.get("empty"):
        img = _gradient(size, (16, 19, 25), (11, 13, 18))
        draw = ImageDraw.Draw(img)
        r = max(2, size // 40)
        draw.ellipse((size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r), fill=(44, 51, 64))
        return img
    tone = TONES.get(view.get("tone") or "") if view.get("lit") else None
    if tone:
        img = _gradient(size, tone[0], tone[1])
        ink, ink_dim = tone[2], tuple(round(c * 0.8 + t * 0.2) for c, t in zip(tone[2], tone[1], strict=True))
    else:
        img = _gradient(size, BG_TOP, BG_BOTTOM)
        ink, ink_dim = (INK_DIM if view.get("dim") else INK), INK_DIM
    draw = ImageDraw.Draw(img)
    label = str(view.get("label") or "")
    value = view.get("value")
    pad = max(4, round(size * 0.07))
    font_px = max(10, round(size * 0.155))
    font = _font(font_px)
    lines = _wrap(label, font, size - 2 * pad, 2)
    if len(lines) > 1 or any(_width(line, font) > size - 2 * pad for line in lines):
        font = _font(max(9, round(size * 0.135)))
        lines = _wrap(label, font, size - 2 * pad, 2)
    line_h = round(font.size * 1.15) if hasattr(font, "size") else font_px
    text_h = line_h * len(lines)
    top_room = size - text_h - pad
    icon_px = round(min(size * 0.46, top_room - pad * 1.2))
    if value:
        icon_px = round(icon_px * 0.82)
    icon_px = max(icon_px, 12)
    icon_y = pad + max(0, (top_room - pad - icon_px) // 2)
    mask = icons.mask(str(view.get("icon") or "command"), icon_px)
    img.paste(Image.new("RGB", mask.size, ink), ((size - icon_px) // 2, icon_y), mask)
    if value:
        vfont = _font(max(9, round(size * 0.125)))
        text = str(value)
        draw.text((size / 2, icon_y + icon_px + pad * 0.6), text, font=vfont, fill=ink_dim, anchor="mt")
    y = size - pad - text_h
    for line in lines:
        draw.text((size / 2, y), line, font=font, fill=ink, anchor="mt")
        y += line_h
    if view.get("armed"):
        draw.rectangle((0, 0, size - 1, size - 1), outline=(255, 255, 255), width=max(2, size // 32))
    return img


def png(view: dict, size: int = 96) -> bytes:
    buf = io.BytesIO()
    render(view, size).save(buf, "PNG", optimize=False)
    return buf.getvalue()


def to_native(img, fmt: dict) -> bytes:
    """The picture in the bytes a device wants, from its `key_image_format()` {size, format, flip, rotation}."""
    size = tuple(fmt["size"])
    if img.size != size:
        img = img.resize(size, Image.LANCZOS)
    rotation = fmt.get("rotation", 0)
    if rotation:
        img = img.rotate(rotation)  # the library's own order: rotate, then flip
    flip_h, flip_v = fmt.get("flip", (False, False))
    if flip_h:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if flip_v:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    buf = io.BytesIO()
    kind = str(fmt.get("format", "JPEG")).upper()
    if kind == "JPEG":
        img.save(buf, "JPEG", quality=95)
    else:
        img.save(buf, kind)
    return buf.getvalue()


class Cache:
    """A small LRU of rendered pictures keyed by whatever the caller picks (view rev + size)."""

    def __init__(self, limit: int = 512) -> None:
        self.limit = limit
        self._items: OrderedDict[tuple, bytes] = OrderedDict()

    def get(self, key: tuple, make) -> bytes:
        hit = self._items.get(key)
        if hit is not None:
            self._items.move_to_end(key)
            return hit
        value = make()
        self._items[key] = value
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return value
