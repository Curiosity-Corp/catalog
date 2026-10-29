"""The blur's edge halo (#133): a saturated speaker on a contrasting background, cut out by a hard mask.

The blur branch of the graph runs here for real through ffmpeg on the CPU (no GPU, no model): the picture is split as
in the engine, the mask is an exact box, and the composite's background pixels next to the mask edge are measured for
how much of the speaker's colour they picked up. A plain blur of the whole frame drags the speaker's red into them;
the normalised blur must not.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from test_engine_graph import CANON, spec

from streamstack.engine import graph
from streamstack.engine.graph import BlurGraph

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

WIDTH, HEIGHT = 1280, 720
MASK_W, MASK_H = 320, 180
SPEAKER_X, SPEAKER_W = 480, 320  # picture pixels; the mask box is the same box at 1/4 size
BACKGROUND_RGB, SPEAKER_RGB = (32, 64, 192), (224, 16, 16)
NEAR = range(4, 25, 4)  # background pixels this many px outside the speaker's edge


def composite_row(edge: str) -> list[tuple[int, int, int]]:
    """The middle row of the composite (blur on, strength 8) as rgb tuples."""
    blur = BlurGraph(mask_width=MASK_W, mask_height=MASK_H, edge=edge)
    branch = graph._blur_branch(spec(CANON, blur=blur), True, 8)
    chain = "[0:v]format=yuv444p,split[fg][bgs]" + branch + ",format=rgb24"
    picture = (
        f"color=c=0x2040c0:s={WIDTH}x{HEIGHT}:r=1,drawbox=x={SPEAKER_X}:y=160:w={SPEAKER_W}:h=400:c=0xe01010:t=fill"
    )
    inside = f"between(X,{SPEAKER_X // 4},{(SPEAKER_X + SPEAKER_W) // 4 - 1})*between(Y,40,139)"
    weight = f"if({inside},0,255)"  # background weight: 255 away from the speaker, 0 on the speaker
    mask = f"color=c=white:s={MASK_W}x{MASK_H}:r=1,format=yuv444p,geq=lum='{weight}':cb='{weight}':cr='{weight}'"
    done = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-f", "lavfi", "-i", picture, "-f", "lavfi", "-i", mask),
            *("-filter_complex", chain, "-frames:v", "1", "-f", "rawvideo", "-"),
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()[-400:]
    row = HEIGHT // 2 * WIDTH * 3
    return [tuple(done.stdout[row + x * 3 : row + x * 3 + 3]) for x in range(WIDTH)]


def speaker_colour_share(row: list[tuple[int, int, int]]) -> float:
    """0 = the background's own red, 1 = the speaker's red, averaged over the pixels just outside the edge."""
    reds = [row[SPEAKER_X - d][0] for d in NEAR]
    share = (sum(reds) / len(reds) - BACKGROUND_RGB[0]) / (SPEAKER_RGB[0] - BACKGROUND_RGB[0])
    return share


def test_normalised_blur_keeps_the_speakers_colour_out_of_the_background() -> None:
    plain = speaker_colour_share(composite_row("plain"))
    normalised = speaker_colour_share(composite_row("normalised"))
    assert plain > 0.15, f"the experiment should show the halo in the plain blur (got {plain:.3f})"
    assert normalised < 0.05, f"normalised blur leaks {normalised:.3f} of the speaker's colour"
    assert normalised < plain / 5


def test_far_from_the_speaker_the_background_is_untouched_in_both_modes() -> None:
    for edge in ("plain", "normalised"):
        row = composite_row(edge)
        assert all(abs(a - b) <= 4 for a, b in zip(row[100], BACKGROUND_RGB, strict=True)), edge


def test_divide_mode_matches_the_expression_it_replaced() -> None:
    """The branch divides with blend's divide mode (cheap); it must give the old expression's values (#148).

    The two differ only where the weight is exactly 0, which is under the speaker and masked out of the composite.
    """
    size = 64 * 64
    top = bytes((i * 7 + i // 64 * 13) % 256 for i in range(size))
    bottom = bytes((i * 11 + i // 64 * 5) % 256 for i in range(size))

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.raw").write_bytes(top)
        (Path(tmp) / "b.raw").write_bytes(bottom)

        def run(options: str) -> bytes:
            done = subprocess.run(
                [
                    *("ffmpeg", "-v", "error", "-y"),
                    *("-f", "rawvideo", "-pix_fmt", "gray", "-s", "64x64", "-i", str(Path(tmp) / "a.raw")),
                    *("-f", "rawvideo", "-pix_fmt", "gray", "-s", "64x64", "-i", str(Path(tmp) / "b.raw")),
                    *("-filter_complex", f"[0:v][1:v]blend={options}", "-frames:v", "1", "-f", "rawvideo", "-"),
                ],
                capture_output=True,
                timeout=60,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            assert done.returncode == 0, done.stderr.decode()[-300:]
            return done.stdout

        expression = run("all_expr='min(255,A*255/max(B,1))'")
        mode = run("all_mode=divide")
    assert len(expression) == len(mode) == size
    off = [(b, abs(x - y)) for b, x, y in zip(bottom, expression, mode, strict=True) if b > 0]
    assert off and max(d for _, d in off) <= 1
