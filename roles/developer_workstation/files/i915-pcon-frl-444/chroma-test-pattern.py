#!/usr/bin/env python3
"""Render a native-resolution display test pattern.

Top: a chroma-detail panel (1 px colour stripes, coloured text) shown as-is on
the left and with simulated YCbCr 4:2:0 on the right. If the left looks like
the right, the link is subsampling chroma. Bottom: near-black (0-24) and
near-white (235-255) steps to choose Broadcast RGB / TV black level.

Usage: chroma-test-pattern.py OUT.png [WIDTHxHEIGHT]   (default 7680x4320)
Show it unscaled, for example: feh --fullscreen --zoom 100 OUT.png
"""
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


def chroma_panel(w, h, s):
    """Panel whose detail lives mostly in the chroma channels."""
    p = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(p)
    m = int(40 * s)
    bw = w // 2 - int(60 * s)
    for i, pair in enumerate(([(255, 0, 0), (0, 0, 255)], [(255, 0, 255), (0, 160, 0)])):
        x0 = m + i * (bw + m)
        for x in range(bw):
            d.line([(x0 + x, m), (x0 + x, int(640 * s))], fill=pair[x % 2])
    for y in range(int(600 * s)):
        d.line([(m, int(700 * s) + y), (w - m, int(700 * s) + y)],
               fill=[(255, 0, 0), (0, 255, 255)][y % 2])
    y = int(1360 * s)
    for size in (24, 32, 48, 72):
        for bg, fg, label in (((0, 0, 255), (255, 0, 0), "Red text on blue"),
                              ((255, 0, 0), (0, 0, 255), "Blue text on red")):
            d.rectangle([m, y, w - m, y + size + 30], fill=bg)
            d.text((m + 20, y + 10), f"{label}: the quick brown fox 0123456789",
                   font=font(size), fill=fg)
            y += size + 60
    for size in (24, 32):
        for fg in ((255, 0, 0), (0, 140, 0), (0, 0, 255), (200, 0, 200)):
            d.text((m + 20, y), "Coloured text on white: the quick brown fox 0123456789",
                   font=font(size), fill=fg)
            y += size + 20
    return p


def to_420(img):
    """Simulate YCbCr 4:2:0: keep luma, average chroma over 2x2 blocks."""
    y, cb, cr = img.convert("YCbCr").split()
    w, h = img.size
    cb = cb.resize((w // 2, h // 2), Image.BOX).resize((w, h), Image.BILINEAR)
    cr = cr.resize((w // 2, h // 2), Image.BOX).resize((w, h), Image.BILINEAR)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def render(width, height):
    s = height / 4320
    img = Image.new("RGB", (width, height), (128, 128, 128))
    d = ImageDraw.Draw(img)
    pw, ph = width // 2 - int(80 * s), int(2780 * s)
    panel = chroma_panel(pw, ph, s)
    img.paste(panel, (int(40 * s), int(200 * s)))
    img.paste(to_420(panel), (width - pw - int(40 * s), int(200 * s)))
    big = font(int(110 * s), True)
    d.text((int(60 * s), int(40 * s)), "LEFT: what this screen shows", font=big, fill=(255, 255, 255))
    d.text((width - pw - int(20 * s), int(40 * s)), "RIGHT: simulated 4:2:0", font=big, fill=(255, 255, 255))

    by = int(3060 * s)
    d.rectangle([0, by, width // 2, height], fill=(0, 0, 0))
    d.rectangle([width // 2, by, width, height], fill=(255, 255, 255))
    for half, values, label_fill in ((0, (0, 4, 8, 12, 16, 20, 24), (90, 90, 90)),
                                     (1, (235, 240, 245, 250, 255), (160, 160, 160))):
        sw = (width // 2 - int(80 * s)) // len(values)
        for i, v in enumerate(values):
            x = half * width // 2 + int(40 * s) + i * sw
            d.rectangle([x + int(20 * s), by + int(200 * s), x + sw - int(20 * s), height - int(80 * s)],
                        fill=(v, v, v))
            d.text((x + sw // 2 - int(80 * s), by + int(40 * s)), str(v), font=big, fill=label_fill)
    return img


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    try:
        width, height = (int(v) for v in (argv[2] if len(argv) == 3 else "7680x4320").split("x"))
    except ValueError:
        sys.exit(f"bad size {argv[2]!r}, expected WIDTHxHEIGHT")
    try:
        render(width, height).save(argv[1])
    except OSError as e:
        sys.exit(f"cannot write {argv[1]}: {e}")
    print(f"{argv[1]} {width}x{height}")


if __name__ == "__main__":
    main(sys.argv)
