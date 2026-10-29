#!/usr/bin/env python3
"""Render the Console's PNG icons (installable PWA, issue #143) from the same shapes as `console/icon.svg`.

Pure standard library (zlib + struct), so the icons can be regenerated anywhere without an imaging package:

    python3 tools/make_console_icons.py

Writes `icon-192.png`, `icon-512.png` (rounded, purpose "any") and `icon-maskable-512.png` (full bleed with the bars
inside the 80 % safe zone, purpose "maskable") next to the SVG. The output is deterministic.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

BG = (0x5E, 0xA4, 0xFF)
FG = (0x04, 0x10, 0x1F)
BARS = ((6.5, 13.0, 5.0, 13.0), (13.5, 6.0, 5.0, 20.0), (20.5, 10.0, 5.0, 16.0))  # x, y, w, h in a 32 unit box
SUPERSAMPLE = 4
OUT = Path(__file__).resolve().parent.parent / "src" / "streamstack" / "data" / "console"


def inside_round_rect(x: float, y: float, left: float, top: float, w: float, h: float, r: float) -> bool:
    if not (left <= x < left + w and top <= y < top + h):
        return False
    cx = min(max(x, left + r), left + w - r)
    cy = min(max(y, top + r), top + h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size: int, maskable: bool) -> bytes:
    """RGBA rows of a `size` square icon, anti-aliased by supersampling."""
    unit = 32.0
    scale = 0.8 if maskable else 1.0  # the maskable bars live inside the safe zone
    offset = (1.0 - scale) / 2 * unit
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter: none
        for px in range(size):
            covered = bg = fg = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px + (sx + 0.5) / SUPERSAMPLE) / size * unit
                    y = (py + (sy + 0.5) / SUPERSAMPLE) / size * unit
                    covered += 1
                    if maskable or inside_round_rect(x, y, 0, 0, unit, unit, 8.0):
                        bg += 1
                        bx, by = (x - offset) / scale, (y - offset) / scale
                        if any(inside_round_rect(bx, by, *bar, 2.0) for bar in BARS):
                            fg += 1
            alpha = bg / covered
            if bg == 0:
                rows += bytes((0, 0, 0, 0))
                continue
            mixed = [round((BG[i] * (bg - fg) + FG[i] * fg) / bg) for i in range(3)]
            rows += bytes((*mixed, round(alpha * 255)))
    return bytes(rows)


def png(size: int, maskable: bool) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(render(size, maskable), 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    for name, size, maskable in (
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
    ):
        (OUT / name).write_bytes(png(size, maskable))
        print(f"wrote {OUT / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
