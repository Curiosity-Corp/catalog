"""Picture measurements on synthetic RGB buffers: exposure, clipping, colour cast, skin, sharpness, burst."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from streamstack.director import imagefeatures as image

W, H = 320, 180
SKIN = (205, 150, 120)


def frame(
    background: tuple[int, int, int] = (90, 100, 110), face: tuple[int, int, int] | None = SKIN, checker: int = 0
) -> bytes:
    """A flat background with a skin-coloured rectangle; `checker` adds a fine texture of that amplitude."""
    out = bytearray()
    for y in range(H):
        for x in range(W):
            r, g, b = face if face and 120 < x < 200 and 40 < y < 130 else background
            if checker:
                delta = checker if (x + y) % 2 else -checker
                r, g, b = (max(0, min(255, c + delta)) for c in (r, g, b))
            out += bytes((r, g, b))
    return bytes(out)


def test_exposure_histogram_and_clipping() -> None:
    dark = image.frame_features(frame((3, 3, 3), face=None), W, H)
    bright = image.frame_features(frame((255, 255, 255), face=None), W, H)
    assert dark["luma_mean"] < 10 and dark["crushed_shadows"] > 0.9 and dark["clipped_highlights"] == 0
    assert bright["clipped_highlights"] > 0.9 and bright["luma_p99"] > 250
    assert len(dark["histogram"]) == image.HIST_BINS and sum(dark["histogram"]) == pytest.approx(1.0, abs=0.01)


def test_white_balance_reads_warm_cool_and_tint() -> None:
    warm = image.frame_features(frame((150, 110, 80), face=None), W, H)
    cool = image.frame_features(frame((80, 110, 150), face=None), W, H)
    green = image.frame_features(frame((100, 150, 100), face=None), W, H)
    assert warm["warm_index"] > 0.12 and warm["cast"].startswith("warm")
    assert cool["warm_index"] < -0.12 and cool["cast"].startswith("cool")
    assert green["tint_index"] > 0.06 and "green" in green["cast"]


def dark_room_with_glints() -> bytes:
    """Near-black frame with three bright, saturated pixels, like the Canon feed in a dark room (status LED, glint)."""
    out = bytearray(frame((1, 1, 1), face=None))
    for x, rgb in ((10, (2, 54, 44)), (11, (114, 124, 117)), (12, (42, 27, 24))):
        out[3 * (50 * W + x) : 3 * (50 * W + x) + 3] = bytes(rgb)
    return bytes(out)


def test_a_dark_room_gets_no_colour_cast_from_a_few_hot_pixels() -> None:
    """Measured: 1 to 3 pixels of 57,600 gave 'cool, green tint', warm index -1.26 and saturation 0.96."""
    features = image.frame_features(dark_room_with_glints(), W, H)
    assert features["cast"] is None and features["warm_index"] is None and features["saturation"] is None
    lit = image.frame_features(frame((150, 110, 80), face=None), W, H)
    assert lit["cast"].startswith("warm")
    share = image.MIN_MIDTONE_SHARE
    assert image.colour_features(0, 0, 0, 0, 0.0, 100)["cast"] is None
    assert image.colour_features(100 * 90, 100 * 80, 100 * 70, 100, 10.0, 100)["cast"] == "warm"  # no minimum share
    assert image.colour_features(int(share * 1000) * 90, 0, 0, int(share * 1000) - 1, 1.0, 1000)["cast"] is None


def test_skin_and_the_subject_box_are_found() -> None:
    features = image.frame_features(frame(), W, H)
    assert features["skin"] and 100 < features["skin"]["luma"] < 190
    box = features["subject"]["box"]
    assert 0.3 < box[0] < 0.45 and 0.1 < box[1] < 0.35 and 0.15 < box[2] < 0.35
    assert image.frame_features(frame(face=None), W, H)["skin"] is None


def test_backlight_is_flagged_when_the_edges_outshine_the_centre() -> None:
    out = bytearray()
    for _y in range(H):
        for x in range(W):
            out += bytes((230, 230, 230) if x < W * 0.2 or x >= W * 0.8 else (60, 60, 60))
    assert image.frame_features(bytes(out), W, H)["backlit"] is True
    assert image.frame_features(frame(), W, H)["backlit"] is False


def test_sharpness_and_noise_separate_flat_from_textured() -> None:
    flat = image.frame_features(frame(face=None), W, H)
    textured = image.frame_features(frame(face=None, checker=25), W, H)
    assert textured["sharpness"] > flat["sharpness"] + 10
    assert textured["noise"] > flat["noise"]


def test_a_buffer_that_does_not_match_its_size_is_refused() -> None:
    with pytest.raises(image.ImageAnalysisError):
        image.frame_features(b"\x00" * 10, W, H)


def test_burst_summary_sees_flicker_motion_and_blur() -> None:
    frames = [image.frame_features(frame((v, v, v), face=None), W, H) for v in (60, 110, 60, 110)]
    thumbs = [image.downsample(frame((v, v, v), face=None), W, H) for v in (60, 110, 60, 110)]
    summary = image.burst_summary(frames, thumbs)
    assert summary["flicker"] is True and summary["motion"] > 20 and summary["frames"] == 4
    still = [image.frame_features(frame(), W, H)] * 3
    calm = image.burst_summary(still, [image.downsample(frame(), W, H)] * 3)
    assert calm["flicker"] is False and calm["motion_word"] == "still" and calm["blurred_frames"] == []


def test_a_black_burst_has_neither_flicker_nor_blurred_frames() -> None:
    """Dither between 0.0 and 3.0 mean luma is sensor noise; nothing in it can be 'soft' or 'pulsing'."""
    frames = [image.frame_features(frame((v, v, v), face=None), W, H) for v in (1, 3, 1, 3)]
    for f, sharp in zip(frames, (0.9, 0.2, 0.8, 0.1), strict=True):
        f["sharpness"] = sharp
    summary = image.burst_summary(frames, [image.downsample(frame((1, 1, 1), face=None), W, H)] * 4)
    assert summary["flicker"] is False and summary["luma_swing"] == 0.0 and summary["blurred_frames"] == []
    real = [{"luma_mean": 90.0, "sharpness": v} for v in (120.0, 40.0, 118.0)]
    assert image.burst_summary(real, [[0.0] * 4] * 3)["blurred_frames"] == [2]


def test_unreadable_stills_are_reported_not_fatal(tmp_path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg")
    with pytest.raises(image.ImageAnalysisError):
        image.analyze_stills([bad])
    with pytest.raises(image.ImageAnalysisError):
        image.contact_sheet([], tmp_path / "sheet.jpg")


def test_contact_sheet_of_real_stills_has_the_expected_size(tmp_path) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg is not installed")
    stills = []
    for index, colour in enumerate(("red", "green", "blue", "gray")):
        path = tmp_path / f"s{index}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s=320x180",
                "-frames:v",
                "1",
                str(path),
            ],
            check=True,
        )
        stills.append(path)
    sheet = image.contact_sheet(stills, tmp_path / "sheet.jpg")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(sheet),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    width, height = (int(v) for v in probe.stdout.strip().split(","))
    cell_w, cell_h = image.SHEET_CELL
    assert width == 2 * cell_w + image.SHEET_GAP and height == 2 * cell_h + image.SHEET_GAP
    analysis = image.analyze_stills(stills)
    assert len(analysis["frames"]) == 4 and analysis["problems"] == []
