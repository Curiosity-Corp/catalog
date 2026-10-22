"""Scene item crops: discovery, the window-to-crop maths, base restore, crash recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack.framing.crop import (
    Crop,
    Item,
    ObsCropper,
    RestoreLog,
    drawn_height,
    face_to_frame,
    window_to_crop,
)
from streamstack.framing.engine import FULL, Face, Window


def transform(width: int = 1056, height: int = 704, bounds: str = "OBS_BOUNDS_SCALE_INNER", **extra) -> dict:
    return {
        "sourceWidth": width,
        "sourceHeight": height,
        "boundsType": bounds,
        "boundsWidth": 1920.0,
        "boundsHeight": 1080.0,
        "cropLeft": 0,
        "cropRight": 0,
        "cropTop": 0,
        "cropBottom": 0,
        **extra,
    }


class FakeObs:
    """Just enough obs-websocket for the cropper: scenes of items, transforms, and a log of writes."""

    def __init__(self) -> None:
        self.scenes = {
            "Canon": [(1, "Canon EOS 50D", True), (2, "Voice (Easy Effects)", True)],
            "Side by Side": [(3, "Canon EOS 50D", True), (4, "Cam Link 4K", True), (5, "Hidden Cam", False)],
        }
        self.transforms = {
            ("Canon", 1): transform(),
            ("Side by Side", 3): transform(bounds="OBS_BOUNDS_SCALE_INNER", boundsWidth=960.0, boundsHeight=1080.0),
            ("Side by Side", 4): transform(1920, 1080),
        }
        self.writes: list[tuple[str, int, dict]] = []
        self.fail_writes = False

    def __call__(self, request: str, **data) -> dict:
        if request == "GetSceneItemList":
            return {
                "sceneItems": [
                    {"sceneItemId": i, "sourceName": s, "sceneItemEnabled": on}
                    for i, s, on in self.scenes[data["sceneName"]]
                ]
            }
        if request == "GetSceneItemTransform":
            key = (data["sceneName"], data["sceneItemId"])
            if key not in self.transforms:
                raise RuntimeError("GetSceneItemTransform: no such item")
            return {"sceneItemTransform": dict(self.transforms[key])}
        if request == "SetSceneItemTransform":
            if self.fail_writes:
                raise RuntimeError("SetSceneItemTransform: refused")
            key = (data["sceneName"], data["sceneItemId"])
            self.transforms[key].update(data["sceneItemTransform"])
            self.writes.append((data["sceneName"], data["sceneItemId"], dict(data["sceneItemTransform"])))
            return {}
        raise AssertionError(request)


CAMERAS = {"Canon EOS 50D", "Cam Link 4K", "Hidden Cam"}


def test_discover_finds_enabled_camera_items_in_every_scene() -> None:
    obs = FakeObs()
    items = ObsCropper(obs).discover(["Canon", "Side by Side"], CAMERAS)
    assert [(i.scene, i.source) for i in items] == [
        ("Canon", "Canon EOS 50D"),
        ("Side by Side", "Canon EOS 50D"),
        ("Side by Side", "Cam Link 4K"),
    ]
    canon = items[0]
    assert (canon.width, canon.height) == (1056, 704)
    assert canon.out_height == pytest.approx(704 * 1080 / 704)  # height-limited: drawn the full 1080 tall
    assert items[1].out_height == pytest.approx(704 * (960 / 1056))  # half-width slot: width-limited


def test_items_that_would_shrink_instead_of_zoom_are_skipped_with_a_reason() -> None:
    obs = FakeObs()
    obs.transforms[("Canon", 1)] = transform(bounds="OBS_BOUNDS_NONE")
    cropper = ObsCropper(obs)
    items = cropper.discover(["Canon"], CAMERAS)
    assert items == []
    assert "no bounds" in next(iter(cropper.skipped.values()))
    assert drawn_height(transform(bounds="OBS_BOUNDS_MAX_ONLY"), 100, 100) is None
    assert drawn_height(transform(bounds="OBS_BOUNDS_STRETCH"), 100, 100) == 1080.0
    assert drawn_height(transform(bounds="OBS_BOUNDS_SCALE_TO_WIDTH"), 1920, 1080) == pytest.approx(1080.0)
    assert drawn_height(transform(bounds="OBS_BOUNDS_SCALE_OUTER"), 1056, 704) == pytest.approx(704 * 1920 / 1056)


def test_a_source_with_no_picture_yet_is_skipped() -> None:
    obs = FakeObs()
    obs.transforms[("Canon", 1)] = transform(0, 0)
    cropper = ObsCropper(obs)
    assert cropper.discover(["Canon"], CAMERAS) == []
    assert "no picture" in next(iter(cropper.skipped.values()))


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


def test_apply_writes_the_crop_and_logs_the_base_first(tmp_path: Path) -> None:
    obs = FakeObs()
    log = RestoreLog(tmp_path / "restore.json")
    cropper = ObsCropper(obs, log)
    it = cropper.discover(["Canon"], CAMERAS)[0]
    cropper.apply(it, Crop(100, 50, 100, 50))
    assert obs.writes[-1] == ("Canon", 1, {"cropLeft": 100, "cropTop": 50, "cropRight": 100, "cropBottom": 50})
    assert log.load()["Canon\x1f1"]["base"] == {"left": 0, "top": 0, "right": 0, "bottom": 0}
    cropper.restore(it)
    assert obs.transforms[("Canon", 1)]["cropLeft"] == 0
    assert log.load() == {}  # nothing left to recover
    assert not (tmp_path / "restore.json").exists()


def test_recover_puts_back_what_a_crashed_run_left(tmp_path: Path) -> None:
    obs = FakeObs()
    log = RestoreLog(tmp_path / "restore.json")
    first = ObsCropper(obs, log)
    it = first.discover(["Canon"], CAMERAS)[0]
    first.apply(it, Crop(200, 100, 200, 100))  # ... and the process dies here
    second = ObsCropper(obs, log)
    assert second.recover(["Canon", "Side by Side"]) == 1
    assert obs.transforms[("Canon", 1)]["cropLeft"] == 0
    assert log.load() == {}


def test_discover_uses_the_logged_base_not_the_leftover_crop(tmp_path: Path) -> None:
    obs = FakeObs()
    log = RestoreLog(tmp_path / "restore.json")
    first = ObsCropper(obs, log)
    first.apply(first.discover(["Canon"], CAMERAS)[0], Crop(200, 100, 200, 100))
    again = ObsCropper(obs, log).discover(["Canon"], CAMERAS)[0]
    assert again.base == Crop()
    assert again.applied == Crop(200, 100, 200, 100)


def test_a_failed_write_still_leaves_a_way_back(tmp_path: Path) -> None:
    obs = FakeObs()
    obs.fail_writes = True
    log = RestoreLog(tmp_path / "restore.json")
    cropper = ObsCropper(obs, log)
    it = cropper.discover(["Canon"], CAMERAS)[0]
    with pytest.raises(RuntimeError, match="refused"):
        cropper.apply(it, Crop(10, 10, 10, 10))
    assert "Canon\x1f1" in log.load()


def test_the_restore_log_survives_garbage(tmp_path: Path) -> None:
    path = tmp_path / "restore.json"
    path.write_text("{not json")
    assert RestoreLog(path).load() == {}
    path.write_text('{"items": 5}')
    assert RestoreLog(path).load() == {}
    RestoreLog(tmp_path / "missing" / "x.json").save({"k": {"base": {}}})  # creates the directory
