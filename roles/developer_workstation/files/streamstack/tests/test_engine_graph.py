"""The native engine's pure parts: graph strings, commands, rectangles, look blending."""

from __future__ import annotations

import pytest

from streamstack.engine import graph
from streamstack.engine.graph import Rect, Source, Spec

CANON = Source("Canon EOS 50D", "/dev/video42", "YUYV", 1280, 720, 20.0)
CAMLINK = Source("Cam Link 4K", "/dev/video0", "YUYV", 1920, 1080, 30.0)
MJPEG = Source("UVC", "/dev/video2", "MJPG", 1920, 1080, 30.0)
NEUTRAL = {
    "brightness": 0.0,
    "contrast": 0.0,
    "gamma": 0.0,
    "saturation": 0.0,
    "warmth": 0.0,
    "sharpen_enabled": False,
    "sharpen_amount": 0.08,
    "lut_enabled": False,
    "lut_amount": 0.35,
}


def spec(source: Source = CANON, **kw) -> Spec:
    return Spec(source, "/dev/video43", source.width, source.height, **kw)


def test_neutral_look_leaves_the_yuv_filters_disabled() -> None:
    controls = graph.look_controls(NEUTRAL)
    assert controls[(graph.TONE, "enable")] == "0"
    assert controls[(graph.SHARP, "enable")] == "0"
    assert controls[(graph.GRADE, "brightness")] == "0"
    assert controls[(graph.GRADE, "contrast")] == "1"
    assert controls[(graph.GRADE, "gamma")] == "1"


def test_look_maps_to_libplacebo_ranges() -> None:
    controls = graph.look_controls({**NEUTRAL, "contrast": 0.2, "saturation": -0.1, "gamma": 1.0, "brightness": 0.05})
    assert controls[(graph.GRADE, "contrast")] == "1.2"
    assert controls[(graph.GRADE, "saturation")] == "0.9"
    assert controls[(graph.GRADE, "gamma")] == "2"
    assert controls[(graph.GRADE, "brightness")] == "0.05"


def test_warmth_lifts_red_and_lowers_blue_in_both_ends() -> None:
    tone = graph.tone_values({"warmth": 1.0})
    assert tone["rl"] == tone["rh"] > 0 > tone["bl"] == tone["bh"]


def test_lut_grade_splits_teal_lows_from_orange_highs() -> None:
    tone = graph.tone_values({"lut_enabled": True, "lut_amount": 1.0})
    assert tone["rl"] < 0 < tone["rh"]
    assert tone["bl"] > 0 > tone["bh"]
    assert graph.tone_values({"lut_enabled": False, "lut_amount": 1.0}) == {"rl": 0, "bl": 0, "rh": 0, "bh": 0}


def test_blackout_overrides_the_look() -> None:
    controls = graph.look_controls({**NEUTRAL, "brightness": 0.3}, blackout=True)
    assert controls[(graph.GRADE, "brightness")] == "-1"
    assert controls[(graph.GRADE, "contrast")] == "0"


def test_diff_sends_only_what_changed_and_everything_when_unknown() -> None:
    old = graph.look_controls(NEUTRAL)
    new = graph.look_controls({**NEUTRAL, "brightness": 0.1})
    assert [c.line() for c in graph.diff_controls(old, new)] == ["clibplacebo@grade -1 brightness 0.1\n"]
    assert graph.diff_controls(old, old) == []
    assert len(graph.diff_controls(None, new)) == len(new)


def test_diff_switches_filters_on_last_and_off_first() -> None:
    off = graph.look_controls(NEUTRAL)
    on = graph.look_controls({**NEUTRAL, "sharpen_enabled": True, "sharpen_amount": 0.4})
    names = [(c.name, c.value) for c in graph.diff_controls(off, on) if c.target == graph.SHARP]
    assert names == [("strength", "0.4"), ("enable", "1")]
    names = [(c.name, c.value) for c in graph.diff_controls(on, off) if c.target == graph.SHARP]
    assert names == [("enable", "0"), ("strength", "0")]


@pytest.mark.parametrize(
    ("cx", "cy", "zoom", "expected"),
    [
        (0.5, 0.5, 1.0, Rect(0, 0, 1280, 720)),
        (0.5, 0.5, 2.0, Rect(320, 180, 640, 360)),
        (0.0, 0.0, 2.0, Rect(0, 0, 640, 360)),  # pushed into the corner, never outside the picture
        (1.0, 1.0, 2.0, Rect(640, 360, 640, 360)),
    ],
)
def test_window_rect_stays_inside_the_frame(cx: float, cy: float, zoom: float, expected: Rect) -> None:
    assert graph.window_rect(cx, cy, zoom, 1280, 720) == expected


def test_window_rect_is_even_and_bounded_in_zoom() -> None:
    rect = graph.window_rect(0.3333, 0.4111, 50.0, 1280, 720)
    assert rect.w == 160 and rect.h == 90
    assert all(v % 2 == 0 for v in (rect.x, rect.y, rect.w, rect.h))


def test_crop_commands_order_depends_on_growing() -> None:
    rect = Rect(100, 50, 640, 360)
    assert [c.name for c in graph.crop_commands(rect, grow=True)] == ["crop_w", "crop_h", "crop_x", "crop_y"]
    assert [c.name for c in graph.crop_commands(rect, grow=False)] == ["crop_x", "crop_y", "crop_w", "crop_h"]


def test_raw_source_graph_uploads_to_vulkan_and_detection_is_its_own_graph() -> None:
    text = graph.build_graph(spec(), NEUTRAL)
    assert text.startswith("[0:v]format=nv12,hwupload,")
    assert "split" not in text
    assert "format=nv12,hwupload" in text
    assert "libplacebo@grade=w=1280:h=720:format=yuv444p" in text
    assert "hwdownload,format=yuv444p,colorcorrect@tone" in text
    assert text.endswith("cas@sharp=strength=0:enable=0,format=yuyv422[prog]")
    detect = graph.build_detect_graph(spec())
    assert detect.startswith("[0:v]fps=8,scale=320:-2") and detect.endswith("[det]")


def test_mjpeg_source_decodes_on_vaapi_and_maps_to_vulkan() -> None:
    text = graph.build_graph(spec(MJPEG), NEUTRAL)
    assert "hwmap=derive_device=vulkan" in text
    assert "scale_vaapi" in graph.build_detect_graph(spec(MJPEG))
    assert "hwupload" not in text
    argv = graph.build_argv(spec(MJPEG), NEUTRAL)
    assert argv[argv.index("-hwaccel") + 1] == "vaapi"
    assert argv[argv.index("-input_format") + 1] == "mjpeg"


def test_initial_crop_and_lut_are_part_of_the_graph() -> None:
    text = graph.build_graph(spec(lut="/usr/share/luts/teal.cube"), NEUTRAL, Rect(10, 20, 640, 360))
    assert "crop_x=10:crop_y=20:crop_w=640:crop_h=360" in text
    assert "lut=/usr/share/luts/teal.cube" in text


def test_lut_path_with_spaces_is_refused() -> None:
    with pytest.raises(graph.GraphError):
        graph.build_graph(spec(lut="/my luts/a.cube"), NEUTRAL)


def test_cpu_backend_uses_eq_and_has_no_crop() -> None:
    text = graph.build_graph(spec(backend="cpu"), NEUTRAL)
    assert "eq@grade=" in text and "libplacebo" not in text
    assert (graph.CPU_GRADE, "brightness") in graph.look_controls(NEUTRAL, "cpu")
    with pytest.raises(graph.GraphError):
        graph.build_graph(spec(MJPEG, backend="cpu"), NEUTRAL)


def test_argv_wires_stdin_commands_progress_and_both_outputs() -> None:
    argv = graph.build_argv(spec(), NEUTRAL, progress_fd=7)
    assert "-nostdin" not in argv  # stdin is the command channel
    assert argv[argv.index("-progress") + 1] == "pipe:7"
    assert argv[argv.index("-i") + 1] == "/dev/video42"
    assert argv[argv.index("-framerate") + 1] == "20"
    assert "/dev/video43" in argv and "pipe:1" in argv
    assert argv[argv.index("-filter_hw_device") + 1] == "vk"


def test_input_override_replaces_the_v4l2_input() -> None:
    override = ("-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", "1280x720", "-i", "pipe:3")
    argv = graph.build_argv(Spec(CANON, "/dev/video43", 1280, 720, input_override=override), NEUTRAL)
    assert "v4l2" not in argv[: argv.index("-filter_complex")]
    assert "pipe:3" in argv


def test_taps_split_the_program_picture_into_encoder_branches() -> None:
    tap = graph.Tap("pipe:5", "av1_qsv", ("-b:v", "6M"))
    text = graph.build_graph(spec(taps=(tap,)), NEUTRAL)
    assert "split=2[prog][t0]" in text and "[t0]format=nv12[tap0]" in text
    argv = graph.build_argv(spec(taps=(tap,)), NEUTRAL)
    assert argv[argv.index("[tap0]") - 1] == "-map"
    assert "av1_qsv" in argv


def test_unknown_pixel_format_is_a_clear_error() -> None:
    with pytest.raises(graph.GraphError, match="RGB3"):
        graph.build_graph(spec(Source("x", "/dev/video9", "RGB3", 640, 480, 30.0)), NEUTRAL)


def test_blend_eases_numbers_and_flips_flags_halfway() -> None:
    start = {**NEUTRAL}
    target = {**NEUTRAL, "contrast": 0.2, "lut_enabled": True}
    assert graph.blend_looks(start, target, 0.0)["contrast"] == 0.0
    assert graph.blend_looks(start, target, 1.0)["contrast"] == 0.2
    assert graph.blend_looks(start, target, 0.4)["lut_enabled"] is False
    assert graph.blend_looks(start, target, 0.6)["lut_enabled"] is True


# -- background blur -----------------------------------------------------------------------------------------------


def blur_spec(source: Source = CANON, **kw) -> Spec:
    return spec(source, blur=graph.BlurGraph(mask_width=320, mask_height=180), **kw)


def test_blur_strength_maps_to_a_bounded_sigma() -> None:
    assert graph.blur_sigma(1) == pytest.approx(1.1)
    assert graph.blur_sigma(20) == pytest.approx(12.5)
    assert graph.blur_sigma(0) == graph.blur_sigma(1)  # a zero or negative slider never removes the blur filter
    assert graph.blur_sigma(99) == graph.blur_sigma(20)


def test_blur_controls_switch_the_merge_and_set_the_sigma() -> None:
    on = graph.blur_controls(True, 8)
    assert on[(graph.BLUR_MERGE, "enable")] == "1" and on[(graph.BLUR_BG, "sigma")] == "5.3"
    assert graph.blur_controls(False, 8)[(graph.BLUR_MERGE, "enable")] == "0"


def test_a_spec_without_blur_has_no_blur_filters() -> None:
    text = graph.build_graph(spec(), NEUTRAL)
    assert "maskedmerge" not in text and "setpts" not in text


def test_the_blur_graph_pairs_picture_and_mask_by_frame_number() -> None:
    text = graph.build_graph(blur_spec(), NEUTRAL, blur=(True, 8))
    assert text.startswith("[0:v]setpts=N/(20*TB),")
    assert "[1:v]gblur=sigma=1.5,scale=1280:720" in text
    assert "[fg][bg][m]maskedmerge@blur=enable=1" in text
    assert "scale=320:180:flags=bilinear,gblur@bg=sigma=5.3" in text  # the background is blurred at 1/4 size


def test_blur_defaults_to_off_when_the_branch_is_built_without_settings() -> None:
    assert "maskedmerge@blur=enable=0" in graph.build_graph(blur_spec(), NEUTRAL)


def test_the_blur_graph_works_on_the_cpu_backend_too() -> None:
    text = graph.build_graph(blur_spec(backend="cpu"), NEUTRAL, blur=(False, 4))
    assert text.startswith("[0:v]setpts=N/(20*TB),null,") and "maskedmerge@blur=enable=0" in text


def test_the_segmenter_sees_the_whole_frame_squeezed_to_the_model_input() -> None:
    assert graph.build_seg_graph(blur_spec()) == "[0:v]scale=192:192:flags=fast_bilinear,format=rgb24[seg]"
    mjpeg = graph.build_seg_graph(blur_spec(MJPEG))
    assert mjpeg.startswith("[0:v]scale_vaapi=w=192:h=192,hwdownload,format=nv12,format=rgb24")


def test_the_seg_graph_needs_a_blur_spec() -> None:
    with pytest.raises(graph.GraphError):
        graph.build_seg_graph(spec())


def test_blur_argv_adds_the_mask_input_and_the_segmenter_output() -> None:
    argv = graph.build_argv(blur_spec(), NEUTRAL, blur=(True, 8), blur_fds=(11, 12))
    assert argv[argv.index("/dev/fd/12") - 1] == "-i"
    assert "320x180" in argv and "yuv444p" in argv
    assert argv.count("-filter_complex") == 3
    assert argv[-2:] == ["rawvideo", "pipe:11"] and "-flush_packets" in argv
    assert argv.index("/dev/video42") < argv.index("/dev/fd/12")  # input 0 is the camera, input 1 the mask


def test_blur_argv_without_its_pipes_is_refused() -> None:
    with pytest.raises(graph.GraphError):
        graph.build_argv(blur_spec(), NEUTRAL)


def test_a_spec_without_blur_has_the_same_argv_as_before() -> None:
    argv = graph.build_argv(spec(), NEUTRAL)
    assert argv.count("-filter_complex") == 2 and "rawvideo" not in argv
