"""The generated sharpen shader: its text, its file handling, and (where a Vulkan GPU exists) its picture."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from streamstack.engine import graph, shaders


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
