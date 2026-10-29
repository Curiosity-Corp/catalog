"""A time stamp that survives a camera, a loopback node, an encoder and a browser, for glass-to-glass latency.

The frame source paints the wall-clock time (epoch milliseconds) into a strip of black and white blocks along the top
of the picture; whoever receives the frame decodes the strip and subtracts it from its own clock. Blocks are wide and
decoded by their centre against a threshold taken from a known sync pattern, so scaling, blur, chroma subsampling and
lossy coding do not flip a bit, and an 8-bit CRC rejects the frame instead of reporting a wrong time.

Layout (left to right, every block `width // BLOCKS` pixels wide): SYNC (1,0,1,0), then 48 time bits, most significant
first, then the 8 bit CRC-8 (polynomial 0x07) of the six time bytes. `tools/latency_rig.py` carries the same decoder
as JavaScript for the browser; `tests/e2e/test_camera_probe.py` proves the two agree.
"""

from __future__ import annotations

import math

SYNC = (1, 0, 1, 0)
TIME_BITS = 48
CRC_BITS = 8
BLOCKS = 64  # blocks across the picture; 4 + 48 + 8 = 60 carry data, the rest is margin
USED = len(SYNC) + TIME_BITS + CRC_BITS
HIGH, LOW = 235, 16  # video-range white and black


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def bits_for(ms: int) -> list[int]:
    """The 60 bits for a time in epoch milliseconds."""
    if not 0 <= ms < 1 << TIME_BITS:
        raise ValueError(f"time {ms} does not fit in {TIME_BITS} bits")
    raw = ms.to_bytes(TIME_BITS // 8, "big")
    value = (ms << CRC_BITS) | crc8(raw)
    return [*SYNC, *((value >> shift) & 1 for shift in range(TIME_BITS + CRC_BITS - 1, -1, -1))]


def strip_height(height: int) -> int:
    return max(8, min(96, height // 6))


def paint_stamp(luma: bytearray, width: int, height: int, ms: int) -> None:
    """Write the stamp into the top rows of a `width` x `height` 8-bit luma plane, in place."""
    block = width // BLOCKS
    if block < 4:
        raise ValueError(f"a {width} px wide picture is too narrow for the stamp (needs {BLOCKS * 4})")
    row = bytearray([LOW]) * width
    for index, bit in enumerate(bits_for(ms)):
        row[index * block : (index + 1) * block] = bytes([HIGH if bit else LOW]) * block
    rows = strip_height(height)
    for y in range(rows):
        luma[y * width : (y + 1) * width] = row


def read_stamp(luma: bytes, width: int, height: int) -> int | None:
    """The epoch milliseconds stamped into a luma plane, or None when the sync pattern or the CRC does not match."""
    block = width // BLOCKS
    if block < 4 or len(luma) < width * height:
        return None
    y0 = strip_height(height) // 2
    row = luma[y0 * width : (y0 + 1) * width]

    def sample(index: int) -> int:
        centre = index * block + block // 2
        return sum(row[centre - 1 : centre + 2]) // 3

    high = (sample(0) + sample(2)) / 2
    low = (sample(1) + sample(3)) / 2
    if high - low < 48:
        return None
    threshold = (high + low) / 2
    bits = [1 if sample(index) > threshold else 0 for index in range(USED)]
    if tuple(bits[: len(SYNC)]) != SYNC:
        return None
    value = 0
    for bit in bits[len(SYNC) :]:
        value = (value << 1) | bit
    ms, check = value >> CRC_BITS, value & 0xFF
    return ms if crc8(ms.to_bytes(TIME_BITS // 8, "big")) == check else None


def background(width: int, height: int) -> bytearray:
    """A plain mid-grey ramp with a diagonal so the picture is never flat (auto-exposure and encoders stay honest)."""
    luma = bytearray(width * height)
    for y in range(height):
        base = 64 + (y * 96) // max(1, height)
        row = bytearray((base + ((x + y) // 16 % 2) * 24) & 0xFF for x in range(width))
        luma[y * width : (y + 1) * width] = row
    return luma


def yuyv(luma: bytes) -> bytes:
    """Pack a luma plane as YUYV 4:2:2 with neutral chroma (what the program node and Chromium expect)."""
    out = bytearray(len(luma) * 2)
    out[0::2] = luma
    out[1::2] = b"\x80" * len(luma)
    return bytes(out)


def luma_from_yuyv(frame: bytes) -> bytes:
    return bytes(frame[0::2])


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; `values` need not be sorted and must not be empty."""
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(samples_ms: list[float]) -> dict[str, float | int]:
    """Count, min, p50, p95, max and mean of latency samples in milliseconds."""
    if not samples_ms:
        return {"count": 0}
    return {
        "count": len(samples_ms),
        "min": round(min(samples_ms), 1),
        "p50": round(percentile(samples_ms, 50), 1),
        "p95": round(percentile(samples_ms, 95), 1),
        "max": round(max(samples_ms), 1),
        "mean": round(sum(samples_ms) / len(samples_ms), 1),
    }
