"""The Console icon set, drawn with Pillow.

icons.js stays the one source of truth: its `P = {name: '<svg body>'}` table is read here and the shapes (path,
circle, rect; stroke or fill; one translate) are drawn onto a mask. No SVG library: the set only uses a handful of
path commands, which `parse_path` flattens to polylines.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from importlib import resources
from typing import Any

VIEWBOX = 24.0
STROKE = 1.75
_ENTRY = re.compile(r"^\s{2}(?:'([a-z0-9-]+)'|([a-z0-9]+)):\s*'(.*)',\s*$", re.M)
_TOKEN = re.compile(r"([MmLlHhVvCcSsQqAaZz])|(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
_ARGS = {"m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "a": 7, "z": 0}
_FLAGS_AT = (3, 4)  # an arc's two flags may be written without separators ("a6 6 0 0 0 0 8.4")

Point = tuple[float, float]


class IconError(ValueError):
    """An icon in icons.js uses something the rasteriser does not know."""


@lru_cache(maxsize=1)
def bodies() -> dict[str, str]:
    """name -> SVG body, read from the Console's icons.js."""
    source = resources.files("streamstack").joinpath("data/console/components/icons.js").read_text(encoding="utf-8")
    block = source.split("const P = {", 1)[1].split("\n}", 1)[0]
    return {(m.group(1) or m.group(2)): m.group(3) for m in _ENTRY.finditer("\n" + block + "\n")}


def names() -> list[str]:
    return sorted(bodies())


def _tokenize(d: str) -> list[tuple[str, list[float]]]:
    """[(command, [numbers...]), ...] with every command's repeats split out (M's repeats become L)."""
    out: list[tuple[str, list[float]]] = []
    cmd = ""
    nums: list[float] = []
    items: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(d):
        items.append(("c", m.group(1)) if m.group(1) else ("n", m.group(2)))
    i = 0
    while i < len(items):
        kind, value = items[i]
        if kind == "c":
            cmd = value
            i += 1
            if cmd in "Zz":
                out.append((cmd, []))
                continue
        elif not cmd or cmd in "Zz":
            raise IconError(f"number without a command in path {d!r}")
        need = _ARGS[cmd.lower()]
        nums = []
        while len(nums) < need:
            if i >= len(items) or items[i][0] != "n":
                raise IconError(f"path command {cmd} is short of numbers in {d!r}")
            text = items[i][1]
            if cmd in "Aa" and len(nums) in _FLAGS_AT and len(text) > 1 and text[0] in "01" and "." not in text[:1]:
                # a flag glued to what follows ("01.5" is flag 0, flag... or 0 then 1.5): split one digit off
                items[i] = ("n", text[1:])
                nums.append(float(text[0]))
                continue
            nums.append(float(text))
            i += 1
        out.append((cmd, nums))
        if cmd == "M":
            cmd = "L"
        elif cmd == "m":
            cmd = "l"
    return out


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int = 24) -> list[Point]:
    pts = []
    for n in range(1, steps + 1):
        t = n / steps
        u = 1 - t
        pts.append(
            (
                u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
            )
        )
    return pts


def _arc(
    p0: Point, rx: float, ry: float, phi: float, large: bool, sweep: bool, p1: Point, steps: int = 32
) -> list[Point]:
    """SVG endpoint arc -> points (the standard endpoint-to-centre conversion)."""
    if p0 == p1:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [p1]
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    coef = math.sqrt(max(num / den, 0.0)) * (-1 if large == sweep else 1)
    cx1, cy1 = coef * rx * y1 / ry, -coef * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = angle(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = angle((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    pts = []
    for n in range(1, steps + 1):
        t = t1 + dt * n / steps
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * ex - sp * ey + cx, sp * ex + cp * ey + cy))
    pts[-1] = p1
    return pts


def parse_path(d: str) -> list[tuple[list[Point], bool]]:
    """A path as polylines: [(points, closed), ...]."""
    subs: list[tuple[list[Point], bool]] = []
    cur: list[Point] = []
    pos: Point = (0.0, 0.0)
    start: Point = pos
    last_ctrl: Point | None = None
    last_cmd = ""
    for cmd, a in _tokenize(d):
        rel = cmd.islower()
        low = cmd.lower()
        ox, oy = pos if rel else (0.0, 0.0)
        ctrl: Point | None = None
        if low == "m":
            if len(cur) > 1:
                subs.append((cur, False))
            pos = (a[0] + ox, a[1] + oy)
            start, cur = pos, [pos]
        elif low == "l":
            pos = (a[0] + ox, a[1] + oy)
            cur.append(pos)
        elif low == "h":
            pos = (a[0] + ox, pos[1])
            cur.append(pos)
        elif low == "v":
            pos = (pos[0], a[0] + oy)
            cur.append(pos)
        elif low == "c":
            p1, p2, p3 = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy), (a[4] + ox, a[5] + oy)
            cur += _cubic(pos, p1, p2, p3)
            pos, ctrl = p3, p2
        elif low == "s":
            p1 = (2 * pos[0] - last_ctrl[0], 2 * pos[1] - last_ctrl[1]) if last_ctrl and last_cmd in "csCS" else pos
            p2, p3 = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
            cur += _cubic(pos, p1, p2, p3)
            pos, ctrl = p3, p2
        elif low == "q":
            q, p3 = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
            p1 = (pos[0] + 2 / 3 * (q[0] - pos[0]), pos[1] + 2 / 3 * (q[1] - pos[1]))
            p2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
            cur += _cubic(pos, p1, p2, p3)
            pos = p3
        elif low == "a":
            end = (a[5] + ox, a[6] + oy)
            cur += _arc(pos, a[0], a[1], a[2], bool(a[3]), bool(a[4]), end)
            pos = end
        elif low == "z":
            if cur:
                subs.append((cur, True))
            cur = [start]
            pos = start
        last_ctrl, last_cmd = ctrl, cmd
    if len(cur) > 1:
        subs.append((cur, False))
    return subs


def shapes(body: str) -> list[dict[str, Any]]:
    """[{"lines": [(points, closed)], "fill": bool, "stroke": bool}, ...] in viewBox units."""
    try:
        root = ET.fromstring(f"<g>{body}</g>")  # noqa: S314 - our own icons.js
    except ET.ParseError as exc:
        raise IconError(f"icon body is not valid SVG: {exc}") from exc
    out = []
    for el in root:
        tag = el.tag
        fill = el.get("fill") == "currentColor"
        stroke = el.get("stroke") != "none"
        dx, dy = 0.0, 0.0
        if m := re.fullmatch(r"translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)", el.get("transform") or ""):
            dx, dy = float(m.group(1)), float(m.group(2))
        if tag == "path":
            lines = parse_path(el.get("d", ""))
        elif tag == "circle":
            cx, cy, r = (float(el.get(k, 0)) for k in ("cx", "cy", "r"))
            lines = [
                (
                    [(cx + r * math.cos(t * math.tau / 48), cy + r * math.sin(t * math.tau / 48)) for t in range(48)],
                    True,
                )
            ]
        elif tag == "rect":
            x, y, w, h = (float(el.get(k, 0)) for k in ("x", "y", "width", "height"))
            r = min(float(el.get("rx", 0)), w / 2, h / 2)
            if r <= 0:
                lines = [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)]
            else:
                pts: list[Point] = []
                for cx, cy, a0 in (
                    (x + w - r, y + r, -90),
                    (x + w - r, y + h - r, 0),
                    (x + r, y + h - r, 90),
                    (x + r, y + r, 180),
                ):
                    for n in range(9):
                        t = math.radians(a0 + 90 * n / 8)
                        pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
                lines = [(pts, True)]
        else:
            raise IconError(f"unsupported icon element <{tag}>")
        moved = [([(px + dx, py + dy) for px, py in pts], closed) for pts, closed in lines]
        out.append({"lines": moved, "fill": fill, "stroke": stroke})
    return out


def mask(name: str, px: int, supersample: int = 4):
    """An "L" Pillow image of the icon, `px` square (white where it is drawn)."""
    from PIL import Image, ImageDraw

    body = bodies().get(name) or bodies()["command"]
    big = px * supersample
    scale = big / VIEWBOX
    img = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(img)
    width = max(1, round(STROKE * scale))
    for shape in shapes(body):
        for pts, closed in shape["lines"]:
            scaled = [(x * scale, y * scale) for x, y in pts]
            if shape["fill"] and len(scaled) > 2:
                draw.polygon(scaled, fill=255)
            if shape["stroke"]:
                path = scaled + ([scaled[0]] if closed else [])
                if len(path) > 1:
                    draw.line(path, fill=255, width=width, joint="curve")
                for x, y in (path[0], path[-1]):
                    draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=255)
    return img.resize((px, px), Image.LANCZOS)
