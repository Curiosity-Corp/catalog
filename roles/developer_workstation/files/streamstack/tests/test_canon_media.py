"""JPEG framing and the ffmpeg plans of the Canon bridge (decode path, size, argv)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from streamstack.canon import encode
from streamstack.canon.jpeg import JpegInfo, JpegSplitter, jpeg_info

FRAME = Path(__file__).parent / "fixtures" / "canon_liveview_1056x704.jpg"
SRC = JpegInfo(1056, 704, 3, "4:2:2")


def test_splitter_cuts_frames_across_arbitrary_chunks() -> None:
    frame = FRAME.read_bytes()
    stream = frame * 3
    splitter = JpegSplitter()
    got: list[bytes] = []
    for i in range(0, len(stream), 997):
        got += splitter.feed(stream[i : i + 997])
    assert got == [frame, frame, frame]


def test_splitter_drops_a_runaway_stream_without_an_end() -> None:
    splitter = JpegSplitter()
    splitter.MAX_BUFFER = 1024
    assert splitter.feed(b"\xff\xd8\xff" + b"x" * 4096) == []
    assert splitter.feed(FRAME.read_bytes()) == [FRAME.read_bytes()]


def test_jpeg_info_reads_size_from_the_frame_header() -> None:
    info = jpeg_info(FRAME.read_bytes())
    assert info is not None and (info.width, info.height, info.components) == (1056, 704, 3)


@pytest.mark.parametrize("junk", [b"", b"not a jpeg", b"\xff\xd8", b"\xff\xd8\xff\xe0\x00\x02"])
def test_jpeg_info_refuses_what_is_not_a_frame(junk: bytes) -> None:
    assert jpeg_info(junk) is None


def test_native_output_keeps_aspect_and_never_enlarges() -> None:
    assert encode.output_size(SRC, "native", 1920, 1080) == (1056, 704, 1056, 704)
    big = JpegInfo(6000, 4000, 3, "4:2:0")
    sw, sh, ow, oh = encode.output_size(big, "native", 1920, 1080)
    assert (sw, sh) == (ow, oh) and sw <= 1920 and sh <= 1080 and abs(sw / sh - 1.5) < 0.01 and sw % 2 == sh % 2 == 0


def test_fill_output_covers_the_canvas_and_crops() -> None:
    assert encode.output_size(SRC, "fill", 1920, 1080) == (1920, 1280, 1920, 1080)


def plan(name: str, mode: str = "native", version: tuple[int, int] = (8, 0)) -> encode.Plan:
    return encode.build_plan(name, SRC, "/dev/video42", mode, (1920, 1080), "/dev/dri/renderD128", "0x8086", version)


def test_cpu_native_plan_has_no_scale_and_frames_pass_through() -> None:
    p = plan("cpu")
    assert p.argv[p.argv.index("-vf") + 1] == "format=yuyv422"
    assert "-fps_mode" in p.argv and p.argv[-3:] == ["-f", "v4l2", "/dev/video42"]
    assert "-use_wallclock_as_timestamps" in p.argv and "mpjpeg" in p.argv


def test_old_ffmpeg_uses_vsync() -> None:
    assert "-vsync" in plan("cpu", version=(4, 4)).argv and "-fps_mode" not in plan("cpu", version=(4, 4)).argv


def test_fill_plan_scales_then_crops_on_every_path() -> None:
    for name in ("cpu", "vaapi", "qsv"):
        vf = plan(name, "fill").argv[plan(name, "fill").argv.index("-vf") + 1]
        assert "1920" in vf and "crop=1920:1080" in vf and vf.endswith("format=yuyv422")


def test_gpu_plans_decode_and_scale_on_the_gpu_and_download_once() -> None:
    va, qs = plan("vaapi", "fill"), plan("qsv", "fill")
    assert "scale_vaapi=w=1920:h=1280:format=nv12,hwdownload" in va.argv[va.argv.index("-vf") + 1]
    assert va.env.get("LIBVA_DRIVER_NAME") == "iHD" or "LIBVA_DRIVER_NAME" in os.environ
    assert "vaapi=va:/dev/dri/renderD128" in va.argv and "-hwaccel_output_format" in va.argv
    assert (
        "mjpeg_qsv" in qs.argv and "vpp_qsv=w=1920:h=1280:format=nv12,hwdownload" in qs.argv[qs.argv.index("-vf") + 1]
    )
    assert "out_range" not in " ".join(va.argv + qs.argv)  # it shifts luma levels


def test_gpu_plan_without_a_render_node_is_refused() -> None:
    with pytest.raises(ValueError):
        encode.build_plan("qsv", SRC, "/dev/video42", "native", (1920, 1080), None)


def test_legacy_plan_is_the_old_command_line() -> None:
    p = plan("legacy", "fill")
    assert p.framing == "raw" and "mjpeg" in p.argv and "force_original_aspect_ratio=increase" in " ".join(p.argv)


def test_probe_argv_never_names_the_loopback_device() -> None:
    p = plan("qsv")
    assert "/dev/video42" not in p.probe_argv()
    assert p.probe_argv()[-3:] == ["-f", "null", "-"]


def test_render_nodes_prefer_the_intel_gpu(tmp_path: Path) -> None:
    for name, vendor in (("renderD128", "0x10de"), ("renderD129", "0x8086")):
        (tmp_path / name / "device").mkdir(parents=True)
        (tmp_path / name / "device" / "vendor").write_text(vendor + "\n")
    found = encode.render_nodes(tmp_path, Path("/dev/dri"))
    assert found == [("/dev/dri/renderD129", "0x8086"), ("/dev/dri/renderD128", "0x10de")]


def test_mpjpeg_framing_carries_the_length() -> None:
    data = encode.mpjpeg(b"abc")
    assert b"Content-Length: 3\r\n\r\nabc\r\n" in data


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_cpu_path_decodes_a_real_frame_with_real_ffmpeg() -> None:
    assert encode.probe(plan("cpu"), FRAME.read_bytes()) is None
    assert encode.probe(plan("cpu", "fill"), FRAME.read_bytes()) is None


def test_probe_reports_a_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    p = plan("cpu")
    p.prefix[0] = "/nonexistent/ffmpeg"
    assert "cannot start ffmpeg" in (encode.probe(p, FRAME.read_bytes()) or "")
