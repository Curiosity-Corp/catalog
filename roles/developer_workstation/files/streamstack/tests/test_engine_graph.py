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
    assert text.startswith("[0:v]" + graph.lookahead_stamp(20.0) + "format=yuv422p,hwupload,")
    assert "split" not in text
    assert "libplacebo@grade=w=1280:h=720:fps=20:format=yuv444p" in text
    assert "hwdownload,format=yuv444p,colorcorrect@tone" in text
    assert text.endswith("cas@sharp=strength=0:enable=0,format=yuyv422[prog]")
    detect = graph.build_detect_graph(spec())
    assert detect.startswith("[0:v]fps=8,scale=320:-2") and detect.endswith("[det]")


def test_gpu_sharpen_graph_has_no_cpu_stage_and_stays_4_2_2() -> None:
    look = {**NEUTRAL, "sharpen_enabled": True, "sharpen_amount": 0.18, "warmth": 0.4}
    text = graph.build_graph(spec(shaders="/run/s"), look)
    assert "custom_shader_path=/run/s/cas-0.20.hook" in text
    assert "colorcorrect" not in text and "cas@" not in text
    assert "format=yuv422p,hwupload" in text and "hwdownload,format=yuv422p,format=yuyv422[prog]" in text
    assert "extra_opts=temperature=-0.4" in text


def test_gpu_graph_without_sharpen_has_no_shader() -> None:
    text = graph.build_graph(spec(shaders="/run/s"), NEUTRAL)
    assert "custom_shader_path" not in text and "colorcorrect" not in text


def test_split_tone_keeps_the_cpu_stage_only_when_the_key_says_so() -> None:
    look = {**NEUTRAL, "lut_enabled": True, "lut_amount": 0.5}
    key = graph.graph_key(spec(shaders="/s"), look)
    assert key == graph.GraphKey(sharpen=0.0, split=True)
    text = graph.build_graph(spec(shaders="/s"), look)
    assert "colorcorrect@tone" in text and "cas@" not in text
    controls = graph.look_controls(look, key=key)
    assert (graph.TONE, "enable") in controls and (graph.SHARP, "enable") not in controls


def test_graph_key_is_none_without_shaders_or_on_the_cpu_backend() -> None:
    assert graph.graph_key(spec(), NEUTRAL) is None
    assert graph.graph_key(spec(backend="cpu", shaders="/s"), NEUTRAL) is None


def test_baked_look_sends_no_command_to_filters_that_are_not_there() -> None:
    key = graph.GraphKey(sharpen=0.2)
    controls = graph.look_controls({**NEUTRAL, "sharpen_enabled": True, "sharpen_amount": 0.2}, key=key)
    assert {t for t, _ in controls} == {graph.GRADE}


def test_sharpen_level_rounds_to_steps_and_is_never_zero_while_on() -> None:
    on = {"sharpen_enabled": True}
    assert graph.sharpen_level({**on, "sharpen_amount": 0.18}) == 0.2
    assert graph.sharpen_level({**on, "sharpen_amount": 0.001}) == graph.SHARP_STEP
    assert graph.sharpen_level({**on, "sharpen_amount": 5}) == 1.0
    assert graph.sharpen_level({"sharpen_enabled": False, "sharpen_amount": 0.5}) == 0.0
    assert graph.sharpen_level({**on, "sharpen_amount": 0}) == 0.0


def test_warmth_temperature_is_signed_bounded_and_never_negative_zero() -> None:
    assert str(graph.warmth_temperature(0.0)) == "0.0"
    assert graph.warmth_temperature(1.0) == graph.WARM_TEMPERATURE < 0
    assert graph.warmth_temperature(-1.0) == graph.COOL_TEMPERATURE > 0
    assert graph.warmth_temperature(9) == graph.WARM_TEMPERATURE


def test_upload_format_keeps_the_cameras_chroma_layout() -> None:
    assert graph.upload_format(CANON) == "yuv422p"
    assert graph.upload_format(Source("x", "/dev/video9", "NV12", 640, 480, 30.0)) == "nv12"


def test_look_lut_is_a_baked_cube_and_replaces_the_split_tone() -> None:
    look = {**NEUTRAL, "lut": "portra", "lut_enabled": True, "lut_amount": 0.5}
    key = graph.graph_key(spec(shaders="/s"), look)
    assert key == graph.GraphKey(sharpen=0.0, split=False, lut="portra", lut_steps=10)
    text = graph.build_graph(spec(shaders="/s"), look)
    assert "lut=/s/lut-portra-10.cube:lut_type=normalized" in text and "colorcorrect" not in text


def test_lut_choice_needs_a_valid_name_the_flag_and_a_strength() -> None:
    on = {"lut": "noir", "lut_enabled": True, "lut_amount": 0.5}
    assert graph.lut_choice(on) == ("noir", 10)
    assert graph.lut_choice({**on, "lut_enabled": False}) == ("", 0)
    assert graph.lut_choice({**on, "lut_amount": 0.01}) == ("", 0)
    assert graph.lut_choice({**on, "lut_amount": 7}) == ("noir", 20)
    assert graph.lut_choice({**on, "lut": "../etc"}) == ("", 0)
    assert graph.lut_choice({**on, "lut": None}) == ("", 0)
    assert graph.lut_choice({"lut_enabled": True, "lut_amount": 0.5}) == ("", 0)


def test_a_look_without_a_lut_name_keeps_the_split_tone_approximation() -> None:
    key = graph.graph_key(spec(shaders="/s"), {**NEUTRAL, "lut_enabled": True, "lut_amount": 0.5})
    assert key is not None and key.split and key.lut == ""


def test_the_static_lut_uses_the_same_lut_type() -> None:
    text = graph.build_graph(spec(lut="/usr/share/luts/teal.cube"), NEUTRAL)
    assert "lut=/usr/share/luts/teal.cube:lut_type=normalized" in text


def test_blending_keeps_the_text_of_a_look_untouched() -> None:
    mixed = graph.blend_looks({**NEUTRAL, "lut": "a"}, {**NEUTRAL, "contrast": 0.4, "lut": "b"}, 0.5)
    assert mixed["lut"] == "b" and 0 < mixed["contrast"] < 0.4


def test_the_scaler_is_the_specs_and_must_be_a_benchmarked_one() -> None:
    text = graph.build_graph(spec(), NEUTRAL)
    assert f"upscaler={graph.DEFAULT_SCALER}:downscaler={graph.DEFAULT_SCALER}" in text
    assert "upscaler=spline36:downscaler=spline36" in graph.build_graph(spec(scaler="spline36"), NEUTRAL)
    with pytest.raises(graph.GraphError, match="not a benchmarked scaler"):
        graph.build_graph(spec(scaler="made-up"), NEUTRAL)
    assert graph.DEFAULT_SCALER in graph.SCALERS


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
    assert "split=2[pre][t0];[pre]format=yuyv422[prog]" in text and "[t0]format=nv12[tap0]" in text
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
    assert "maskedmerge" not in text and "N/(20*TB)" not in text  # the frame-number pairing stamp is blur's


def test_the_blur_graph_pairs_picture_and_mask_by_frame_number() -> None:
    text = graph.build_graph(blur_spec(), NEUTRAL, blur=(True, 8))
    assert text.startswith("[0:v]" + graph.lookahead_stamp(20.0))  # frame-number timestamps, as the mask has
    assert "[1:v]gblur=sigma=1.5[ms]" in text and "scale=1280:720" in text
    assert "[fg][bg][m]maskedmerge@blur=enable=1" in text
    assert "gblur@bg=sigma=5.3" in text and "gblur@bgw=sigma=5.3" in text  # numerator and weight at 1/4 size


def test_the_plain_blur_graph_blurs_the_background_without_a_weight() -> None:
    plain = graph.BlurGraph(mask_width=320, mask_height=180, edge="plain")
    text = graph.build_graph(spec(CANON, blur=plain), NEUTRAL, blur=(True, 8))
    assert "[1:v]gblur=sigma=1.5,scale=1280:720" in text
    assert "scale=320:180:flags=bilinear,gblur@bg=sigma=5.3" in text and "bgw" not in text


def test_the_normalised_blur_divides_by_a_blurred_weight() -> None:
    text = graph.build_graph(blur_spec(), NEUTRAL, blur=(True, 8))
    assert "blend=all_mode=divide" in text and "all_expr" not in text
    controls = graph.blur_controls(True, 8, "normalised")
    assert controls[(graph.BLUR_WEIGHT, "sigma")] == controls[(graph.BLUR_BG, "sigma")] == "5.3"
    assert graph.BLUR_WEIGHT not in {k[0] for k in graph.blur_controls(True, 8, "plain")}


def test_an_unknown_blur_edge_mode_is_refused() -> None:
    bad = graph.BlurGraph(mask_width=320, mask_height=180, edge="sharp")
    with pytest.raises(graph.GraphError):
        graph.build_graph(spec(CANON, blur=bad), NEUTRAL)


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


def test_the_denoiser_runs_on_the_gpu_before_the_grade_and_keeps_its_edge_artefact_out_of_the_picture() -> None:
    text = graph.build_graph(spec(denoise="nlmeans"), NEUTRAL)
    assert text.index("nlmeans_vulkan=s=3:p=3:r=5") < text.index("libplacebo@grade")
    assert "hwupload,nlmeans_vulkan" in text
    # the filter's outermost row and column are garbage: the grade crops them off (1280x720 source)
    assert "crop_x=2:crop_y=2:crop_w=1276:crop_h=716" in text
    assert "nlmeans" not in graph.build_graph(spec(), NEUTRAL)
    assert "crop_x" not in graph.build_graph(spec(), NEUTRAL)


def test_a_window_stays_inside_the_trimmed_frame_when_the_denoiser_is_on() -> None:
    on, off = spec(denoise="nlmeans"), spec()
    assert graph.safe_window(off, None) is None
    assert graph.safe_window(on, graph.Rect(0, 0, 1280, 720)) == graph.Rect(2, 2, 1276, 716)
    assert graph.safe_window(on, graph.Rect(320, 180, 640, 360)) == graph.Rect(320, 180, 640, 360)
    assert graph.safe_window(on, graph.Rect(900, 500, 380, 220)) == graph.Rect(900, 500, 378, 218)


def test_an_unknown_denoiser_or_the_cpu_backend_is_refused() -> None:
    with pytest.raises(graph.GraphError, match="not a known denoiser"):
        graph.build_graph(spec(denoise="made-up"), NEUTRAL)
    with pytest.raises(graph.GraphError, match="Vulkan backend"):
        graph.build_graph(spec(denoise="nlmeans", backend="cpu"), NEUTRAL)


def test_a_vaapi_tap_uploads_and_pins_the_device_on_the_cpu_graph() -> None:
    tap = graph.Tap("pipe:5", "hevc_vaapi", ("-low_power", "1"), upload=True)
    cpu = spec(taps=(tap,), backend="cpu", render_node="/dev/dri/renderD129")
    assert "[t0]format=nv12,hwupload[tap0]" in graph.build_graph(cpu, NEUTRAL)
    argv = graph.build_argv(cpu, NEUTRAL)
    i = argv.index("vaapi=va:/dev/dri/renderD129")
    assert argv[i - 1] == "-init_hw_device" and argv[i + 1 : i + 3] == ["-filter_hw_device", "va"]
    assert argv[argv.index("[tap0]") + 1 : argv.index("[tap0]") + 3] == ["-c:v", "hevc_vaapi"]


def test_a_vaapi_tap_is_refused_on_the_vulkan_graph() -> None:
    tap = graph.Tap("pipe:5", "hevc_vaapi", upload=True)
    with pytest.raises(graph.GraphError, match="VA-API tap"):
        graph.build_argv(spec(taps=(tap,)), NEUTRAL)


def test_taps_convert_from_the_planar_picture_not_from_the_packed_output() -> None:
    """The split sits before the yuyv422 pack, so a tap goes yuv422p -> nv12 and never via the packed repack."""
    tap = graph.Tap("pipe:5", "av1_qsv")
    text = graph.build_graph(spec(taps=(tap, tap)), NEUTRAL)
    assert text.count("format=yuyv422") == 1
    packed = text.index("format=yuyv422")
    assert text.index("split=3") < packed
    assert text.count("format=nv12") == 2
    assert "split=3[pre][t0][t1];[pre]format=yuyv422[prog]" in text


def test_no_taps_keeps_the_single_pack_at_the_end() -> None:
    assert graph.build_graph(spec(), NEUTRAL).endswith("format=yuyv422[prog]")
    assert "split" not in graph.build_graph(spec(), NEUTRAL).replace("split_", "")


# -- the latency holds (issue #141): a regression gate that needs no GPU ---------------------------------------------


def _libplacebo_options(text: str) -> dict[str, str]:
    body = text.split("libplacebo@grade=", 1)[1].split(",hwdownload", 1)[0]
    return dict(part.split("=", 1) for part in body.split(":") if "=" in part)


@pytest.mark.parametrize("source", [CANON, CAMLINK, MJPEG])
def test_the_graph_asks_libplacebo_for_no_lookahead(source: Source) -> None:
    """libplacebo renders a frame only once it has seen a later one, unless the next frame's time is already in the
    future of the target. The engine has no temporal effect, so each frame is stamped N + 0.25 frame periods, ahead of
    libplacebo's own frame-number grid (`fps=`), and no frame mixer is requested (a mixer needs real lookahead)."""
    text = graph.build_graph(spec(source), NEUTRAL)
    stamp = graph.lookahead_stamp(source.fps)
    rate = f"{source.fps:g}"
    assert stamp in text and f"(N+{graph.fnum(graph.LOOKAHEAD_SHIFT)})/({rate}*TB)" in stamp
    assert "settb=" in stamp  # a fraction of a frame period survives only in a fine timebase
    assert 0 < graph.LOOKAHEAD_SHIFT < 0.5  # at 0.5 or more the previous frame would be the nearer one
    opts = _libplacebo_options(text)
    assert opts["fps"] == rate
    assert not {"frame_mixer", "interpolation", "interpolation_threshold"} & opts.keys()
    assert text.startswith("[0:v]" + stamp)  # stamped first, whatever the upload path is


def test_the_stamp_keeps_frame_zero_at_time_zero() -> None:
    assert "if(eq(N,0),0," in graph.lookahead_stamp(30.0)  # libplacebo shows nothing for a first frame ahead of time 0


@pytest.mark.parametrize("source", [CANON, MJPEG])
def test_the_blur_graph_keeps_the_no_lookahead_stamp(source: Source) -> None:
    text = graph.build_graph(spec(source, blur=graph.BlurGraph()), NEUTRAL, blur=(True, 8))
    assert text.startswith("[0:v]" + graph.lookahead_stamp(source.fps))
    assert _libplacebo_options(text)["fps"] == f"{source.fps:g}"


def test_the_cpu_backend_has_no_libplacebo_hold_to_remove() -> None:
    text = graph.build_graph(spec(backend="cpu"), NEUTRAL)
    assert "libplacebo" not in text and "setpts" not in text


def _output_options(argv: list[str], target: str) -> list[str]:
    """The options between the previous output and the `-f <format>` that writes `target`."""
    end = argv.index(target)
    start = max(i for i, a in enumerate(argv[:end]) if a == "-map")
    return argv[start:end]


def test_the_program_output_encodes_on_one_thread_and_the_input_is_unbuffered() -> None:
    """A frame-threaded rawvideo encoder holds every frame one slot (33 ms at 30 fps)."""
    argv = graph.build_argv(spec(), NEUTRAL)
    program = _output_options(argv, "/dev/video43")
    assert program[program.index("-threads") + 1] == str(graph.OUTPUT_THREADS) == "1"
    assert "passthrough" in program  # ffmpeg keeps no frame back to pick a rate
    head = argv[: argv.index("-filter_complex")]
    assert head[head.index("-fflags") + 1] == "nobuffer" and head[head.index("-flags") + 1] == "low_delay"


def test_the_detection_and_mask_outputs_encode_on_one_thread_too() -> None:
    """A threaded detection encoder stalled the whole child for 100 ms every few frames (issue #141)."""
    argv = graph.build_argv(blur_spec(), NEUTRAL, blur=(True, 8), blur_fds=(7, 8))
    for target in ("pipe:1", "pipe:7"):  # detection JPEGs, the segmenter's frames
        options = _output_options(argv, target)
        assert options[options.index("-threads") + 1] == "1"
    assert "-flush_packets" in _output_options(argv, "pipe:7")


# -- the call-app contract floor: a small or slow camera is letterboxed and repeated, never advertised as it is ----

LOW_CARD = Source("Cam Link 4K", "/dev/video0", "YUYV", 720, 480, 21.8)


def low_spec(**kw) -> Spec:
    return Spec(LOW_CARD, "/dev/video43", 1280, 720, out_fps=30.0, letterbox=True, **kw)


def test_the_picture_keeps_the_camera_aspect_and_fits_the_node() -> None:
    assert low_spec().picture == (1080, 720)
    wide = Spec(Source("w", "/dev/video0", "YUYV", 640, 360, 30.0), "/dev/video43", 1280, 720, letterbox=True)
    assert wide.picture == (1280, 720)
    tall = Spec(Source("t", "/dev/video0", "YUYV", 1024, 768, 30.0), "/dev/video43", 1280, 720, letterbox=True)
    assert tall.picture == (960, 720)


def test_without_letterbox_the_picture_is_the_whole_node() -> None:
    assert Spec(LOW_CARD, "/dev/video43", 1920, 1080).picture == (1920, 1080)


@pytest.mark.parametrize("backend", ["vulkan", "cpu"])
def test_a_low_resolution_camera_is_scaled_to_fit_then_padded_and_repeated(backend: str) -> None:
    text = graph.build_graph(low_spec(backend=backend), NEUTRAL)
    if backend == "vulkan":
        assert "w=1080:h=720" in text and "fps=21.8" in text
    else:
        assert "scale=1080:720:" in text
    assert "pad=1280:720:100:0:color=black,fps=30" in text
    assert "[prog]" in text


def test_the_blur_branch_works_at_the_picture_size_not_the_node_size() -> None:
    text = graph.build_graph(low_spec(blur=graph.BlurGraph()), NEUTRAL, blur=(True, 8))
    assert "[1:v]gblur=sigma=1.5[ms]" in text
    assert "scale=1080:720:flags=bilinear[bg]" in text and "scale=1280:720" not in text
    assert text.rindex("pad=1280:720:100:0:color=black") > text.index("maskedmerge@blur")


def test_a_camera_that_fills_the_node_at_its_own_rate_gets_no_extra_stage() -> None:
    assert graph.output_stage(spec()) == []
    assert "pad=" not in graph.build_graph(spec(), NEUTRAL) and "fps=20[" not in graph.build_graph(spec(), NEUTRAL)


def test_the_program_node_rate_is_the_contract_rate_not_the_camera_rate() -> None:
    assert low_spec().rate == 30.0 and spec().rate == 20.0
