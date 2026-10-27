"""The framing window as a crop in source pixels: the window-to-crop maths and the face re-expressed in the base."""

from __future__ import annotations

import pytest

from streamstack.framing.crop import Crop, Item, face_to_frame, window_to_crop
from streamstack.framing.engine import FULL, Face, Window


def item(base: Crop = Crop(), width: int = 1056, height: int = 704) -> Item:
    return Item("Canon", 1, "Canon EOS 50D", width, height, 1080.0, base, base)


def test_window_to_crop_keeps_the_aspect_and_the_frame_inside() -> None:
    it = item()
    assert window_to_crop(FULL, it) == Crop()
    crop = window_to_crop(Window(0.5, 0.5, 2.0), it)
    assert crop == Crop(264, 176, 264, 176)
    shown_w, shown_h = 1056 - crop.left - crop.right, 704 - crop.top - crop.bottom
    assert shown_w / shown_h == pytest.approx(1056 / 704)
    off_corner = window_to_crop(Window(0.25, 0.25, 2.0), it)
    assert (off_corner.left, off_corner.top) == (0, 0)
    assert off_corner.right > 0 and off_corner.bottom > 0


def test_window_to_crop_works_inside_a_base_crop_and_adds_it_back() -> None:
    base = Crop(left=100, top=0, right=156, bottom=0)  # a 800x704 base picture
    it = item(base)
    assert it.frame_width == 800
    crop = window_to_crop(Window(0.5, 0.5, 2.0), it)
    assert crop.left == 100 + 200 and crop.right == 156 + 200
    assert window_to_crop(FULL, it) == base


def test_a_degenerate_window_never_blanks_the_picture() -> None:
    assert window_to_crop(Window(0.5, 0.5, 500.0), item()) == Crop()


def test_face_to_frame_accounts_for_the_base_crop() -> None:
    it = item(Crop(left=264, top=0, right=264, bottom=0))  # base shows the middle half of a 1056 wide source
    face = Face(0.5 - 0.05, 0.2, 0.1, 0.2, 0.9, eye_y=0.27)
    framed = face_to_frame(face, it)
    assert framed.cx == pytest.approx(0.5)  # the middle of the source is the middle of the base
    assert framed.w == pytest.approx(0.1 * 1056 / 528)
    assert framed.h == pytest.approx(0.2)
    assert framed.eye_y == pytest.approx(0.27)
    assert face_to_frame(Face(0.1, 0.1, 0.1, 0.1), item()).eye_y is None
