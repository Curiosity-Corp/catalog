"""Local picture analysis for the AI director: per-frame numbers and one contact sheet.

Each still of a short burst is decoded by ffmpeg to a small RGB buffer and measured here in plain Python:
exposure histogram and clipping, white balance and colour cast, skin tone, sharpness and noise, a backlight
check and a rough subject (face) box. A burst summary adds flicker (exposure breathing) and motion. The stills
are also composed into one contact sheet, which is the only picture that leaves the machine.

This module needs no computer-vision dependency. The skin-colour blob below is the built-in subject finder and the
fallback; when the optional framing extra is installed, `streamstack.director.faces` puts the YuNet face detector
in front of it through the `SUBJECT_FINDERS` list, and a real face box replaces the blob wherever it is used
(headroom and centring findings, spot metering).

Estimates are for steering, not metrology: the grey-world colour cast is fooled by a wall of one colour, and a
skin blob can be a hand or a wooden door. The numbers say so in their `source` and `confidence` fields.
"""

from __future__ import annotations

import colorsys
import math
import statistics
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

WIDTH = 320
HIST_BINS = 16
SKIN_GRID = (16, 9)
SHEET_CELL = (640, 360)
SHEET_GAP = 6
MAX_FRAMES = 8


class ImageAnalysisError(RuntimeError):
    """A still could not be decoded or a contact sheet could not be built."""


def _run(argv: list[str], timeout: float) -> bytes:
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise ImageAnalysisError("ffmpeg is not installed, so pictures cannot be analysed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ImageAnalysisError(f"ffmpeg took longer than {timeout:g} s") from exc
    except OSError as exc:
        raise ImageAnalysisError(f"ffmpeg cannot run ({exc.strerror})") from exc
    if result.returncode != 0:
        raise ImageAnalysisError(f"ffmpeg failed: {result.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return result.stdout


def decode_rgb(path: Path, width: int = WIDTH, timeout: float = 20.0) -> tuple[bytes, int, int]:
    """(rgb24 bytes, width, height) of a still scaled to `width` pixels across."""
    data = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={width}:-2:flags=area",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        timeout,
    )
    height = len(data) // (width * 3)
    if height < 8 or len(data) != width * height * 3:
        raise ImageAnalysisError(f"{path.name} decoded to an unusable size")
    return data, width, height


def contact_sheet(paths: list[Path], out: Path, timeout: float = 30.0) -> Path:
    """Compose the stills side by side (two or three columns, numbered left to right, top to bottom) into a JPEG."""
    if not paths:
        raise ImageAnalysisError("no stills to compose")
    paths = paths[:MAX_FRAMES]
    columns = 1 if len(paths) == 1 else 2 if len(paths) <= 4 else 3
    cell_w, cell_h = SHEET_CELL
    argv = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    chains, tags = [], []
    for index, path in enumerate(paths):
        argv += ["-i", str(path)]
        chains.append(
            f"[{index}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
            f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2:color=black[c{index}]"
        )
        tags.append(f"[c{index}]")
    if len(paths) == 1:
        graph = f"{chains[0]};[c0]null[sheet]"
    else:
        step_x, step_y = cell_w + SHEET_GAP, cell_h + SHEET_GAP
        layout = "|".join(f"{(i % columns) * step_x}_{(i // columns) * step_y}" for i in range(len(paths)))
        graph = f"{';'.join(chains)};{''.join(tags)}xstack=inputs={len(paths)}:layout={layout}:fill=0x202020[sheet]"
    argv += ["-filter_complex", graph, "-map", "[sheet]", "-frames:v", "1", "-q:v", "3", str(out)]
    _run(argv, timeout)
    try:
        if out.stat().st_size < 1024:
            raise ImageAnalysisError("the contact sheet came out empty")
    except OSError as exc:
        raise ImageAnalysisError(f"the contact sheet was not written ({exc.strerror})") from exc
    out.chmod(0o600)
    return out


# ---------------------------------------------------------------------------
# One frame
# ---------------------------------------------------------------------------


def _is_skin(r: int, g: int, b: int) -> bool:
    """Classic YCbCr skin box plus the R > G > B ordering of lit skin; deliberately conservative."""
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return 77 <= cb <= 127 and 133 <= cr <= 173 and y > 45 and r > g > b and (r - b) > 15


def frame_features(rgb: bytes, width: int, height: int, source: Path | None = None) -> dict:
    """Measurements of one RGB buffer. Raises ImageAnalysisError for a buffer that does not match its size.

    `source` is the still the buffer came from, for subject finders that read the full-size picture.
    """
    if width < 8 or height < 8 or len(rgb) != width * height * 3:
        raise ImageAnalysisError("the picture buffer does not match its size")
    count = width * height
    luma = [0.0] * count
    histogram = [0] * HIST_BINS
    high = low = 0
    sum_r = sum_g = sum_b = 0
    mid_n = 0
    sat_total = 0.0
    skin_n = skin_y = skin_r = skin_g = skin_b = 0
    skin_cells = [[0] * SKIN_GRID[0] for _ in range(SKIN_GRID[1])]
    cell_w, cell_h = width / SKIN_GRID[0], height / SKIN_GRID[1]
    centre_sum = centre_n = edge_sum = edge_n = 0
    x0, x1, y0, y1 = width * 0.3, width * 0.7, height * 0.2, height * 0.8
    for i in range(count):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        luma[i] = y
        histogram[min(HIST_BINS - 1, int(y * HIST_BINS / 256))] += 1
        if y >= 250:
            high += 1
        elif y <= 5:
            low += 1
        px, py = i % width, i // width
        if x0 <= px < x1 and y0 <= py < y1:
            centre_sum += y
            centre_n += 1
        elif px < width * 0.2 or px >= width * 0.8:
            edge_sum += y
            edge_n += 1
        if 40 <= y <= 215:
            sum_r += r
            sum_g += g
            sum_b += b
            mid_n += 1
            top, bottom = max(r, g, b), min(r, g, b)
            sat_total += (top - bottom) / top if top else 0.0
        if _is_skin(r, g, b):
            skin_n += 1
            skin_y += y
            skin_r += r
            skin_g += g
            skin_b += b
            skin_cells[min(SKIN_GRID[1] - 1, int(py / cell_h))][min(SKIN_GRID[0] - 1, int(px / cell_w))] += 1
    ordered = sorted(luma)

    def luma_percentile(fraction: float) -> float:
        return ordered[min(count - 1, int(fraction * count))]

    features: dict = {
        "luma_mean": round(sum(luma) / count, 1),
        "luma_p1": round(luma_percentile(0.01), 1),
        "luma_p5": round(luma_percentile(0.05), 1),
        "luma_p50": round(luma_percentile(0.50), 1),
        "luma_p95": round(luma_percentile(0.95), 1),
        "luma_p99": round(luma_percentile(0.99), 1),
        "histogram": [round(v / count, 4) for v in histogram],
        "clipped_highlights": round(high / count, 4),
        "crushed_shadows": round(low / count, 4),
    }
    features["contrast_spread"] = round(features["luma_p95"] - features["luma_p5"], 1)
    features.update(colour_features(sum_r, sum_g, sum_b, mid_n, sat_total))
    features["skin"] = skin_features(skin_n, skin_y, skin_r, skin_g, skin_b, count)
    features["sharpness"], features["noise"] = sharpness_and_noise(luma, width, height)
    centre = centre_sum / centre_n if centre_n else None
    edge = edge_sum / edge_n if edge_n else None
    features["centre_luma"] = round(centre, 1) if centre is not None else None
    features["edge_luma"] = round(edge, 1) if edge is not None else None
    features["backlit"] = bool(centre is not None and edge is not None and edge - centre > 30)
    features["subject"] = find_subject(Scene(skin_cells, count, skin_n, source))
    return features


def colour_features(sum_r: int, sum_g: int, sum_b: int, n: int, sat_total: float) -> dict:
    """Grey-world white balance over the mid-tones: warm/cool index, green/magenta tint and colourfulness."""
    if not n:
        return {"cast": None, "warm_index": None, "tint_index": None, "saturation": None}
    r, g, b = sum_r / n, sum_g / n, sum_b / n
    warm = (r - b) / max(1.0, (r + g + b) / 3)
    tint = (g - (r + b) / 2) / max(1.0, (r + g + b) / 3)
    word = "warm" if warm > 0.12 else "cool" if warm < -0.12 else "neutral"
    if abs(tint) > 0.06:
        word += ", green tint" if tint > 0 else ", magenta tint"
    return {
        "cast": word,
        "warm_index": round(warm, 3),
        "tint_index": round(tint, 3),
        "saturation": round(sat_total / n, 3),
        "mean_rgb": [round(r, 1), round(g, 1), round(b, 1)],
    }


def skin_features(n: int, sum_y: float, sum_r: int, sum_g: int, sum_b: int, count: int) -> dict | None:
    """Mean skin luma and warmth over the skin-coloured pixels, or None when there is hardly any."""
    if n / count < 0.004:
        return None
    r, g, b = sum_r / n, sum_g / n, sum_b / n
    hue, sat, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return {
        "area": round(n / count, 4),
        "luma": round(sum_y / n, 1),
        "redness": round(r / max(1.0, g), 2),  # lit skin sits near 1.2-1.6
        "hue_deg": round(hue * 360, 1),
        "saturation": round(sat, 3),
    }


def sharpness_and_noise(luma: list[float], width: int, height: int) -> tuple[float, float]:
    """(variance of the Laplacian, Immerkaer noise sigma): focus and grain, in 8-bit luma units at `WIDTH` pixels."""
    lap_sum = lap_sq = 0.0
    noise_sum = 0.0
    n = 0
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            i = row + x
            centre = luma[i]
            up, down, left, right = luma[i - width], luma[i + width], luma[i - 1], luma[i + 1]
            lap = up + down + left + right - 4 * centre
            lap_sum += lap
            lap_sq += lap * lap
            corners = luma[i - width - 1] + luma[i - width + 1] + luma[i + width - 1] + luma[i + width + 1]
            noise_sum += abs(corners - 2 * (up + down + left + right) + 4 * centre)
            n += 1
    variance = lap_sq / n - (lap_sum / n) ** 2
    sigma = noise_sum * math.sqrt(math.pi / 2) / (6 * n)
    return round(variance, 1), round(sigma, 2)


# ---------------------------------------------------------------------------
# The subject
# ---------------------------------------------------------------------------


def skin_blob(cells: list[list[int]], count: int) -> dict | None:
    """The largest 4-connected group of skin-coloured grid cells as a box, with how much of the frame it fills."""
    rows, cols = len(cells), len(cells[0])
    per_cell = count / (rows * cols)
    marked = [[cells[r][c] >= 0.35 * per_cell for c in range(cols)] for r in range(rows)]
    seen = [[False] * cols for _ in range(rows)]
    best: list[tuple[int, int]] = []
    for r in range(rows):
        for c in range(cols):
            if not marked[r][c] or seen[r][c]:
                continue
            stack, group = [(r, c)], []
            seen[r][c] = True
            while stack:
                cr, cc = stack.pop()
                group.append((cr, cc))
                for nr, nc in ((cr + 1, cc), (cr - 1, cc), (cr, cc + 1), (cr, cc - 1)):
                    if 0 <= nr < rows and 0 <= nc < cols and marked[nr][nc] and not seen[nr][nc]:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            if len(group) > len(best):
                best = group
    if len(best) < 3:
        return None
    ys, xs = [p[0] for p in best], [p[1] for p in best]
    box = [min(xs) / cols, min(ys) / rows, (max(xs) - min(xs) + 1) / cols, (max(ys) - min(ys) + 1) / rows]
    return {"box": [round(v, 3) for v in box], "area": round(len(best) / (rows * cols), 3)}


@dataclass(frozen=True)
class Scene:
    """What a subject finder is given: the skin grid of the small decode and the still it came from."""

    cells: list[list[int]]
    count: int
    skin_n: int
    source: Path | None = None


def _skin_finder(scene: Scene) -> dict | None:
    blob = skin_blob(scene.cells, scene.count)
    if not blob:
        return None
    x, y, w, h = blob["box"]
    return {
        "source": "skin-blob",
        "confidence": "low",
        "box": blob["box"],
        "centre": [round(x + w / 2, 3), round(y + h / 2, 3)],
        "height": h,
        "headroom": y,
        "off_centre": round(abs(x + w / 2 - 0.5), 3),
    }


# Finders are tried in order; the first answer wins. `director.faces.install` puts the face detector first.
SUBJECT_FINDERS: list[Callable[[Scene], dict | None]] = [_skin_finder]


def find_subject(scene: Scene) -> dict | None:
    for finder in SUBJECT_FINDERS:
        found = finder(scene)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# A burst
# ---------------------------------------------------------------------------


def downsample(luma_rgb: bytes, width: int, height: int, size: tuple[int, int] = (40, 22)) -> list[float]:
    """A thumbnail of the luma, for comparing frames cheaply."""
    out = []
    for ty in range(size[1]):
        for tx in range(size[0]):
            x, y = (
                min(width - 1, int((tx + 0.5) * width / size[0])),
                min(height - 1, int((ty + 0.5) * height / size[1])),
            )
            i = 3 * (y * width + x)
            out.append(0.2126 * luma_rgb[i] + 0.7152 * luma_rgb[i + 1] + 0.0722 * luma_rgb[i + 2])
    return out


def burst_summary(frames: list[dict], thumbs: list[list[float]]) -> dict:
    """What changed between the frames: exposure breathing (flicker), motion and the sharpest frame."""
    means = [f["luma_mean"] for f in frames]
    average = statistics.fmean(means)
    swing = (max(means) - min(means)) / average if average > 1 and len(means) > 1 else 0.0
    diffs = [
        statistics.fmean(abs(a - b) for a, b in zip(thumbs[i], thumbs[i + 1], strict=True))
        for i in range(len(thumbs) - 1)
    ]
    motion = statistics.fmean(diffs) if diffs else 0.0
    sharp = [f["sharpness"] for f in frames]
    return {
        "frames": len(frames),
        "luma_swing": round(swing, 3),
        "flicker": swing > 0.05 and len(frames) >= 3,
        "motion": round(motion, 1),
        "motion_word": "still" if motion < 3 else "some movement" if motion < 9 else "lots of movement",
        "sharpest": sharp.index(max(sharp)) + 1,
        "sharpness_median": round(statistics.median(sharp), 1),
        "blurred_frames": [i + 1 for i, v in enumerate(sharp) if v < 0.5 * max(sharp)] if max(sharp) > 0 else [],
    }


def analyze_stills(paths: list[Path]) -> dict:
    """Features of every readable still and the burst summary. Unreadable stills are listed, not fatal."""
    frames: list[dict] = []
    thumbs: list[list[float]] = []
    problems: list[str] = []
    for index, path in enumerate(paths, start=1):
        try:
            rgb, width, height = decode_rgb(path)
            frames.append(frame_features(rgb, width, height, path))
            thumbs.append(downsample(rgb, width, height))
        except ImageAnalysisError as exc:
            problems.append(f"frame {index}: {exc}")
    if not frames:
        raise ImageAnalysisError(problems[0] if problems else "no stills")
    return {"frames": frames, "burst": burst_summary(frames, thumbs), "problems": problems}
