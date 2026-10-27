"""Key pictures. Pillow is the optional `deck` extra, so these skip where it is missing."""

from __future__ import annotations

import io

import pytest

from streamstack.deck import icons, render

pytest.importorskip("PIL")
from PIL import Image

VIEW = {"i": 0, "label": "Mute", "icon": "mic", "lit": False, "tone": None, "value": None, "armed": False, "dim": False}


def test_every_console_icon_rasterizes() -> None:
    for name in icons.names():
        mask = icons.mask(name, 48)
        assert mask.size == (48, 48), name
        assert mask.getextrema()[1] > 0, f"{name} drew nothing"


def test_a_key_is_a_square_picture_of_the_requested_size() -> None:
    for size in (72, 96, 120):
        assert render.render(VIEW, size).size == (size, size)


def test_a_lit_key_is_filled_with_its_tone_and_an_unlit_one_is_dark() -> None:
    dark = render.render(VIEW, 96).getpixel((4, 48))
    red = render.render({**VIEW, "lit": True, "tone": "red"}, 96).getpixel((4, 48))
    assert max(dark) < 60
    assert red[0] > 180 and red[1] < 120


def test_an_empty_key_is_nearly_black_and_an_armed_one_has_a_white_edge() -> None:
    assert max(render.render({"empty": True}, 96).getpixel((10, 10))) < 40
    assert render.render({**VIEW, "armed": True}, 96).getpixel((0, 40)) == (255, 255, 255)


def test_a_long_label_still_fits_two_lines() -> None:
    img = render.render({**VIEW, "label": "A very long label that cannot fit on a key", "value": "100%"}, 72)
    assert img.size == (72, 72)


def test_png_is_a_png() -> None:
    assert render.png(VIEW, 64).startswith(b"\x89PNG")


def test_to_native_applies_rotation_then_flips_then_encodes() -> None:
    img = Image.new("RGB", (4, 4), (0, 0, 0))
    img.putpixel((0, 0), (255, 255, 255))
    raw = render.to_native(img, {"size": (4, 4), "format": "BMP", "flip": (False, False), "rotation": 0})
    assert raw.startswith(b"BM")
    flipped = Image.open(io.BytesIO(render.to_native(img, {"size": (4, 4), "format": "BMP", "flip": (True, True)})))
    assert flipped.getpixel((3, 3)) == (255, 255, 255) and flipped.getpixel((0, 0)) == (0, 0, 0)
    jpeg = render.to_native(img, {"size": (4, 4), "format": "JPEG", "flip": (False, False), "rotation": 0})
    assert jpeg.startswith(b"\xff\xd8")


def test_to_native_resizes_to_what_the_device_wants() -> None:
    raw = render.to_native(render.render(VIEW, 96), {"size": (72, 72), "format": "JPEG", "flip": (False, False)})
    assert Image.open(io.BytesIO(raw)).size == (72, 72)


def test_the_cache_makes_once_and_forgets_the_oldest() -> None:
    cache = render.Cache(2)
    made: list[str] = []

    def make(name: str):
        def inner() -> bytes:
            made.append(name)
            return name.encode()

        return inner

    assert cache.get(("a",), make("a")) == b"a"
    cache.get(("a",), make("a"))
    cache.get(("b",), make("b"))
    cache.get(("c",), make("c"))  # evicts a
    cache.get(("a",), make("a"))
    assert made == ["a", "b", "c", "a"]
