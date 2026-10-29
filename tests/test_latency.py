from __future__ import annotations

import pytest

from streamstack import latency

W, H = 1280, 720
NOW = 1_795_000_123_456


def frame(ms: int = NOW, width: int = W, height: int = H) -> bytearray:
    luma = latency.background(width, height)
    latency.paint_stamp(luma, width, height, ms)
    return luma


def test_the_stamp_round_trips() -> None:
    assert latency.read_stamp(bytes(frame()), W, H) == NOW


@pytest.mark.parametrize("size", [(640, 360), (1920, 1080), (3840, 2160)])
def test_it_round_trips_at_every_contract_size(size: tuple[int, int]) -> None:
    assert latency.read_stamp(bytes(frame(NOW, *size)), *size) == NOW


def test_it_survives_blur_noise_and_a_brightness_shift() -> None:
    luma = frame()
    row = W
    blurred = bytearray(luma)
    for y in range(H):  # horizontal box blur over 9 px plus noise and a gain/offset like a camera's
        for x in range(4, W - 4):
            avg = sum(luma[y * row + x - 4 : y * row + x + 5]) // 9
            blurred[y * row + x] = max(0, min(255, int(avg * 0.8 + 30 + ((x * 7 + y * 13) % 25) - 12)))
        if y > latency.strip_height(H) + 2:
            break
    assert latency.read_stamp(bytes(blurred), W, H) == NOW


def test_a_corrupted_bit_is_rejected_not_misread() -> None:
    luma = frame()
    block = W // latency.BLOCKS
    centre = 20 * block + block // 2
    for y in range(latency.strip_height(H)):
        luma[y * W + centre - 4 : y * W + centre + 5] = (
            bytes([latency.LOW if luma[y * W + centre] > 100 else latency.HIGH]) * 9
        )
    assert latency.read_stamp(bytes(luma), W, H) is None


def test_a_picture_without_a_stamp_reads_as_none() -> None:
    assert latency.read_stamp(bytes(latency.background(W, H)), W, H) is None
    assert latency.read_stamp(bytes(W * H), W, H) is None
    assert latency.read_stamp(b"", W, H) is None


def test_yuyv_packing_keeps_the_luma() -> None:
    luma = bytes(frame())
    packed = latency.yuyv(luma)
    assert len(packed) == W * H * 2
    assert latency.luma_from_yuyv(packed) == luma
    assert set(packed[1::2]) == {0x80}


def test_a_picture_too_narrow_for_the_stamp_is_refused() -> None:
    with pytest.raises(ValueError, match="too narrow"):
        latency.paint_stamp(bytearray(100 * 100), 100, 100, NOW)


def test_a_time_that_does_not_fit_is_refused() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        latency.bits_for(1 << 48)


def test_crc8_matches_the_standard_check_value() -> None:
    assert latency.crc8(b"123456789") == 0xF4


def test_summarize_uses_nearest_rank() -> None:
    s = latency.summarize([float(v) for v in range(1, 101)])
    assert (s["count"], s["min"], s["p50"], s["p95"], s["max"], s["mean"]) == (100, 1.0, 50.0, 95.0, 100.0, 50.5)
    assert latency.summarize([]) == {"count": 0}
