"""The generated sharpen shader: its text, its file handling, and (where a Vulkan GPU exists) its picture."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from streamstack.engine import graph, shaders
from streamstack.presets import luts


def test_source_carries_the_strength_and_is_a_libplacebo_hook() -> None:
    text = shaders.sharpen_source(0.35)
    assert "#define SHARPNESS 0.35" in text and "//!HOOK OUTPUT" in text and "@STRENGTH@" not in text


@pytest.mark.parametrize("level", [0.0, -0.1, 1.01])
def test_a_strength_outside_zero_to_one_is_refused(level: float) -> None:
    with pytest.raises(ValueError):
        shaders.sharpen_source(level)


def test_ensure_writes_once_at_the_path_the_graph_names(tmp_path: Path) -> None:
    path = shaders.ensure_sharpen(tmp_path / "s", 0.2)
    assert path == graph.sharpen_shader_path(str(tmp_path / "s"), 0.2)
    first = Path(path).stat().st_mtime_ns
    assert shaders.ensure_sharpen(tmp_path / "s", 0.2) == path
    assert Path(path).stat().st_mtime_ns == first  # unchanged content is not rewritten
    assert [p.name for p in (tmp_path / "s").iterdir()] == ["cas-0.20.hook"]  # no temp files left


def test_ensure_repairs_a_changed_file(tmp_path: Path) -> None:
    path = Path(shaders.ensure_sharpen(tmp_path, 0.5))
    path.write_text("broken")
    shaders.ensure_sharpen(tmp_path, 0.5)
    assert path.read_text() == shaders.sharpen_source(0.5)


def test_ensure_reports_an_unwritable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "f"
    blocker.write_text("x")
    with pytest.raises(OSError):
        shaders.ensure_sharpen(blocker / "d", 0.2)


def vulkan_available() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    probe = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-init_hw_device", "vulkan", "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=0.1"),
            *("-vf", "format=yuv420p,hwupload,libplacebo,hwdownload,format=yuv420p", "-f", "null", "-"),
        ],
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0


def run_gpu(tmp_path: Path, level: float | None) -> bytes:
    """A soft horizontal edge through libplacebo, with the CAS shader at `level` (None: no shader), as gray8."""
    extra = f":custom_shader_path={shaders.ensure_sharpen(tmp_path, level)}" if level else ""
    chain = "format=yuv420p,hwupload,libplacebo=format=yuv420p" + extra + ",hwdownload,format=yuv420p,format=gray"
    out = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-init_hw_device", "vulkan", "-f", "lavfi"),
            *("-i", "gradients=s=128x64:c0=0x303030:c1=0xd0d0d0:x0=56:y0=0:x1=72:y1=0:d=1:r=1"),
            *("-vf", chain, "-frames:v", "1", "-f", "rawvideo", "-"),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    return out.stdout


@pytest.mark.skipif(not vulkan_available(), reason="needs ffmpeg with a Vulkan device")
def test_the_shader_steepens_an_edge_and_more_strength_steepens_it_more(tmp_path: Path) -> None:
    def overshoot(data: bytes) -> int:
        row = data[:128]
        return int(max(row)) - int(min(row))

    plain, mild, strong = (overshoot(run_gpu(tmp_path, lvl)) for lvl in (None, 0.2, 1.0))
    assert plain <= mild <= strong
    assert strong > plain


def test_blended_cube_is_the_identity_at_zero_and_the_cube_at_one() -> None:
    full = luts.Lut3D.parse(shaders.blended_cube("portra", 20))
    reference = luts.Lut3D.parse(luts.build_cube("portra"))
    assert len(full.table) == len(reference.table)
    flat_full = [value for row in full.table for value in row]
    flat_reference = [value for row in reference.table for value in row]
    assert flat_full == pytest.approx(flat_reference, abs=1e-5)
    half = luts.Lut3D.parse(shaders.blended_cube("portra", 10))
    last = full.size - 1
    for index in (0, 1, full.size + 3, len(full.table) - 1):
        r, g, b = index % full.size, (index // full.size) % full.size, index // full.size**2
        grid = (r / last, g / last, b / last)
        want = tuple((grid[c] + full.table[index][c]) / 2 for c in range(3))
        assert half.table[index] == pytest.approx(want, abs=2e-5)


@pytest.mark.parametrize("steps", [0, 21, -1])
def test_blended_cube_refuses_a_strength_outside_the_steps(steps: int) -> None:
    with pytest.raises(ValueError):
        shaders.blended_cube("portra", steps)


def test_blended_cube_refuses_an_unknown_lut() -> None:
    with pytest.raises(ValueError):
        shaders.blended_cube("no-such-look", 10)


def test_ensure_lut_writes_once_at_the_path_the_graph_names(tmp_path: Path) -> None:
    directory = tmp_path / "cubes"
    path = shaders.ensure_lut(directory, "noir", 20)
    assert path == graph.lut_file_path(str(directory), "noir", 20)
    assert shaders.ensure_lut(directory, "noir", 20) == path
    assert [p.name for p in directory.iterdir()] == ["lut-noir-20.cube"]


def decode_rgb(tmp_path: Path, lut: str | None) -> bytes:
    """A 96x64 test card through libplacebo (optionally with a cube), decoded to rgb24 the same way both times."""
    extra = f":lut={lut}:{graph.LUT_TYPE}" if lut else ""
    chain = (
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv444p,hwupload,"
        + f"libplacebo=format=yuv444p{extra},hwdownload,format=yuv444p,"
        + "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24"
    )
    return subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-init_hw_device", "vulkan", "-f", "lavfi"),
            *("-i", "testsrc2=s=96x64:d=1:r=1", "-vf", chain, "-frames:v", "1", "-f", "rawvideo", "-"),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    ).stdout


@pytest.mark.skipif(not vulkan_available(), reason="needs ffmpeg with a Vulkan device")
@pytest.mark.parametrize(("name", "steps"), [("portra", 20), ("portra", 10), ("noir", 20), ("teal-orange", 20)])
def test_libplacebo_applies_the_cube_like_the_reference_sampler(tmp_path: Path, name: str, steps: int) -> None:
    """Golden check: the GPU output equals the reference trilinear sampler applied to the cube-less output."""
    plain = decode_rgb(tmp_path, None)
    graded = decode_rgb(tmp_path, shaders.ensure_lut(tmp_path, name, steps))
    cube = luts.Lut3D.parse((tmp_path / f"lut-{name}-{steps:02d}.cube").read_text())
    total, worst = 0.0, 0.0
    for at in range(0, len(plain), 3):
        want = cube.sample((plain[at] / 255, plain[at + 1] / 255, plain[at + 2] / 255))
        for channel in range(3):
            error = abs(graded[at + channel] - want[channel] * 255)
            total += error
            worst = max(worst, error)
    assert total / len(plain) < 2.0 and worst < 10.0
    assert sum(abs(a - b) for a, b in zip(plain, graded, strict=True)) / len(plain) > 1.0  # the cube did something
