"""Hardware encoder detection, the OBS profile it writes and the health check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamstack import cli, config, encoder, system
from streamstack.encoder import Choice, EncoderError

IHD = "vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 26.1.2 ()"

VAINFO = (
    IHD
    + """
vainfo: Supported profile and entrypoints
      VAProfileH264High               :	VAEntrypointVLD
      VAProfileH264High               :	VAEntrypointEncSliceLP

VAProfileH264High/VAEntrypointVLD
    VAConfigAttribRTFormat                 : VA_RT_FORMAT_YUV420

VAProfileH264High/VAEntrypointEncSliceLP
    VAConfigAttribRTFormat                 : VA_RT_FORMAT_YUV420
    VAConfigAttribRateControl              : VA_RC_CBR
                                             VA_RC_VBR
                                             VA_RC_CQP
    VAConfigAttribEncPackedHeaders         : VA_ENC_PACKED_HEADER_SEQUENCE

VAProfileAV1Profile0/VAEntrypointEncSliceLP
    VAConfigAttribRateControl              : VA_RC_CBR
                                             VA_RC_VBR
    VAConfigAttribEncMaxRefFrames          : l0=3
"""
)

OBS_LOG = """01:39:52.460: ---------------------------------
01:39:52.460: Available Encoders:
01:39:52.460:   Video Encoders:
01:39:52.460: \t- ffmpeg_vaapi_tex (FFmpeg VAAPI H.264)
01:39:52.460: \t- av1_ffmpeg_vaapi_tex (FFmpeg VAAPI AV1)
01:39:52.460: \t- obs_x264 (x264)
01:39:52.460:   Audio Encoders:
01:39:52.460: \t- ffmpeg_aac (FFmpeg AAC)
"""


def node(text: str = VAINFO, path: str = "/dev/dri/renderD129") -> encoder.VaNode:
    return encoder.parse_vainfo(text, path)


ALL_IDS = {"av1_ffmpeg_vaapi_tex", "ffmpeg_vaapi_tex", "obs_x264"}


def test_parse_vainfo_reads_driver_entrypoints_and_rate_controls() -> None:
    parsed = node()
    assert parsed.driver == "Intel iHD driver for Intel(R) Gen Graphics - 26.1.2"
    assert parsed.profiles["H264High"].entrypoints == {"VLD", "EncSliceLP"}
    assert parsed.profiles["H264High"].rate_controls["EncSliceLP"] == {"CBR", "VBR", "CQP"}
    assert parsed.profiles["AV1Profile0"].rate_controls["EncSliceLP"] == {"CBR", "VBR"}
    assert parsed.encodes(("AV1Profile0",)) == "AV1Profile0"
    assert parsed.encodes(("HEVCMain",)) is None


def test_decode_only_profiles_do_not_count_as_encoders() -> None:
    decode_only = node(
        IHD + "\nVAProfileAV1Profile0/VAEntrypointVLD\n    VAConfigAttribRTFormat : VA_RT_FORMAT_YUV420\n"
    )
    assert decode_only.encodes(("AV1Profile0",)) is None


def test_parse_obs_log_reads_only_video_encoders() -> None:
    assert encoder.parse_obs_log(OBS_LOG) == ALL_IDS
    assert encoder.parse_obs_log("01:00:00.000: nothing here\n") is None


def test_obs_encoder_ids_use_the_newest_log_that_lists_encoders(tmp_path: Path) -> None:
    logs = config._xdg("XDG_CONFIG_HOME", ".config") / "obs-studio/logs"
    logs.mkdir(parents=True)
    (logs / "2026-10-20 01-00-00.txt").write_text(OBS_LOG)
    (logs / "2026-10-21 01-00-00.txt").write_text("crashed before listing encoders\n")
    assert encoder.obs_encoder_ids() == ALL_IDS
    assert encoder.obs_encoder_ids(tmp_path / "absent") is None


def test_av1_vaapi_wins_and_the_render_node_comes_from_the_probe() -> None:
    detection = encoder.detect([node()], ALL_IDS)
    assert [c.obs_id for c in detection.available] == ["av1_ffmpeg_vaapi_tex", "ffmpeg_vaapi_tex", "obs_x264"]
    best = detection.best
    assert best.node == "/dev/dri/renderD129"  # never a hardcoded path
    assert best.low_power_only


def test_h264_vaapi_when_the_gpu_has_no_av1_encoder() -> None:
    no_av1 = node(VAINFO.split("VAProfileAV1Profile0")[0])
    assert encoder.detect([no_av1], ALL_IDS).best.obs_id == "ffmpeg_vaapi_tex"


def test_an_encoder_obs_does_not_offer_is_never_chosen() -> None:
    detection = encoder.detect([node()], {"ffmpeg_vaapi_tex", "obs_x264"})  # an OBS without AV1 VA-API
    assert detection.best.obs_id == "ffmpeg_vaapi_tex"


def test_x264_is_the_floor_without_a_gpu() -> None:
    assert encoder.detect([], ALL_IDS).best.obs_id == "obs_x264"
    assert encoder.detect([], None, read_log=False).best.obs_id == "obs_x264"


def test_vaapi_is_trusted_before_obs_has_ever_started_but_quick_sync_is_not() -> None:
    detection = encoder.detect([node()], None, read_log=False)
    assert detection.best.obs_id == "av1_ffmpeg_vaapi_tex"
    assert detection.obs_ids is None
    assert all(c.candidate.backend != "qsv" for c in detection.available)


def test_quick_sync_ranks_after_av1_vaapi_but_before_x264_h264_vaapi_order() -> None:
    ids = ALL_IDS | {"obs_qsv11_av1", "obs_qsv11_v2"}
    order = [c.candidate.key for c in encoder.detect([node()], ids).available]
    assert order == ["av1-vaapi", "av1-qsv", "h264-vaapi", "h264-qsv", "x264"]


def test_quick_sync_needs_an_intel_driver() -> None:
    amd = node(
        VAINFO.replace("Intel iHD driver for Intel(R) Gen Graphics - 26.1.2 ()", "Mesa Gallium driver 25.0 for AMD")
    )
    order = [c.candidate.key for c in encoder.detect([amd], ALL_IDS | {"obs_qsv11_av1", "obs_qsv11_v2"}).available]
    assert order == ["av1-vaapi", "h264-vaapi", "x264"]


def test_the_first_node_that_can_encode_is_used() -> None:
    nothing = encoder.VaNode("/dev/dri/renderD128", "software")
    detection = encoder.detect([nothing, node()], ALL_IDS)
    assert detection.best.node == "/dev/dri/renderD129"


def test_probe_nodes_asks_every_render_node_and_survives_a_failing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    encoder._vainfo_cache.clear()
    monkeypatch.setattr(system, "have", lambda tool: True)
    monkeypatch.setattr(encoder, "render_nodes", lambda: ["/dev/dri/renderD128", "/dev/dri/renderD129"])
    asked: list[list[str]] = []

    def fake_run(argv: list[str], timeout: float = 10):
        asked.append(argv)
        if argv[-2] == "/dev/dri/renderD128":
            return None  # hung and killed
        return system.subprocess.CompletedProcess(argv, 0, VAINFO, "")

    monkeypatch.setattr(system, "run", fake_run)
    nodes = encoder.probe_nodes()
    assert [n.path for n in nodes] == ["/dev/dri/renderD129"]
    assert {a[-2] for a in asked} == {"/dev/dri/renderD128", "/dev/dri/renderD129"}
    encoder.probe_nodes()
    assert len(asked) == 2  # cached


def test_probe_nodes_without_vainfo_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "have", lambda tool: False)
    assert encoder.probe_nodes() == []


# -- settings ------------------------------------------------------------------------


def best(ids: set[str] = ALL_IDS) -> Choice:
    return encoder.detect([node()], ids).best


def test_stream_settings_are_low_latency() -> None:
    s = encoder.stream_settings(best(), 1920, 1080, 30)
    assert s["rate_control"] == "CBR" and s["bf"] == 0 and s["keyint_sec"] == 2
    assert s["bitrate"] == 4500  # AV1 at 1080p30
    assert s["ffmpeg_opts"] == "low_power=1"
    h264 = encoder.stream_settings(best({"ffmpeg_vaapi_tex"}), 1920, 1080, 30)
    assert h264["bitrate"] == 6000


def test_bitrate_scales_with_the_picture_and_is_clamped() -> None:
    assert encoder.stream_bitrate_kbps("h264", 1280, 720, 30) == 3000
    assert encoder.stream_bitrate_kbps("h264", 320, 240, 15) == 1500
    assert encoder.stream_bitrate_kbps("h264", 7680, 4320, 60) == 20000


def test_record_settings_favour_quality() -> None:
    s = encoder.record_settings(best(), 1920, 1080, 30)
    assert s["rate_control"] == "VBR" and s["bitrate"] == 9000 and s["maxrate"] == 13500


def test_rate_control_falls_back_to_what_the_gpu_offers() -> None:
    cbr_only = Choice(best().candidate, rate_controls=frozenset({"CBR"}))
    assert encoder.record_settings(cbr_only, 1920, 1080, 30)["rate_control"] == "CBR"
    vbr_only = Choice(best().candidate, rate_controls=frozenset({"VBR"}))
    assert encoder.stream_settings(vbr_only, 1920, 1080, 30)["rate_control"] == "VBR"


def test_low_power_flag_only_when_the_gpu_has_nothing_else() -> None:
    both = node(
        VAINFO.replace("VAProfileAV1Profile0/VAEntrypointEncSliceLP", "VAProfileAV1Profile0/VAEntrypointEncSlice")
    )
    # AV1 has the full entrypoint here, so no low-power flag is forced on the encoder
    choice = encoder.detect([both], ALL_IDS).best
    assert not choice.low_power_only
    assert "ffmpeg_opts" not in encoder.stream_settings(choice, 1920, 1080, 30)


def test_quick_sync_settings() -> None:
    qsv = Choice(encoder.BY_OBS_ID["obs_qsv11_v2"])
    s = encoder.stream_settings(qsv, 1920, 1080, 30)
    assert s["latency"] == "ultra-low" and s["bframes"] == 0 and s["rate_control"] == "CBR"
    assert encoder.record_settings(qsv, 1920, 1080, 30)["target_usage"] == "TU2"


# -- the OBS profile -----------------------------------------------------------------


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    directory = tmp_path / "profile"
    directory.mkdir()
    text = (config.data_dir() / "obs/basic.ini").read_text()
    for token, value in {"@WIDTH@": "1920", "@HEIGHT@": "1080", "@FPS@": "30", "@HOME@": "/home/x"}.items():
        text = text.replace(token, value)
    (directory / "basic.ini").write_text(text)
    return directory


def test_apply_writes_advanced_output_and_encoder_files(profile: Path) -> None:
    choice = best()
    encoder.apply_to_profile(profile, choice, 1920, 1080, 30, Path("/home/x"))
    text = (profile / "basic.ini").read_text()
    assert (
        "Mode=Advanced" in text and "Encoder=av1_ffmpeg_vaapi_tex" in text and "RecEncoder=av1_ffmpeg_vaapi_tex" in text
    )
    assert "ApplyServiceSettings=false" in text and "RecFormat2=mkv" in text
    assert " = " not in text  # OBS's own ini style, no spaces around "="
    assert "OutputCX=1920" in text  # other keys survive
    stream = json.loads((profile / "streamEncoder.json").read_text())
    record = json.loads((profile / "recordEncoder.json").read_text())
    assert stream["rate_control"] == "CBR" and record["rate_control"] == "VBR"
    assert encoder.configured_encoders(profile) == ("av1_ffmpeg_vaapi_tex",) * 2
    assert not list(profile.glob("*.streamstack-tmp"))


def test_apply_x264_goes_back_to_simple_mode(profile: Path) -> None:
    encoder.apply_to_profile(profile, best(), 1920, 1080, 30)
    encoder.apply_to_profile(profile, encoder.detect([], ALL_IDS).best, 1920, 1080, 30)
    assert "Mode=Simple" in (profile / "basic.ini").read_text()
    assert encoder.configured_encoders(profile) == ("obs_x264", "obs_x264")


def test_configured_encoders_follow_a_separate_record_encoder(profile: Path) -> None:
    (profile / "basic.ini").write_text("[Output]\nMode=Advanced\n[AdvOut]\nEncoder=ffmpeg_vaapi_tex\nRecEncoder=none\n")
    assert encoder.configured_encoders(profile) == ("ffmpeg_vaapi_tex", "ffmpeg_vaapi_tex")
    assert encoder.configured_encoders(profile / "absent") is None


def test_an_unreadable_profile_is_an_encoder_error(profile: Path) -> None:
    (profile / "basic.ini").write_text("no section header\n")
    with pytest.raises(EncoderError, match="cannot parse"):
        encoder.configured_encoders(profile)
    with pytest.raises(EncoderError, match="cannot be parsed"):
        encoder.apply_to_profile(profile, best(), 1920, 1080, 30)
    with pytest.raises(EncoderError, match="cannot read"):
        encoder.apply_to_profile(profile / "absent", best(), 1920, 1080, 30)


def test_apply_refuses_while_obs_runs(profile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "running", lambda process: process == "obs")
    with pytest.raises(EncoderError, match="close OBS"):
        encoder.apply(config.load_settings(), best(), directory=profile)
    assert "Mode=Simple" in (profile / "basic.ini").read_text()


def test_apply_rejects_non_numeric_picture_settings(profile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "running", lambda process: False)
    cfg = {**config.load_settings(), "STREAMSTACK_FPS": "fast"}
    with pytest.raises(EncoderError, match="must be numbers"):
        encoder.apply(cfg, best(), directory=profile)


# -- health check and commands -------------------------------------------------------


def configure(monkeypatch: pytest.MonkeyPatch, used: tuple[str, str] | None) -> None:
    monkeypatch.setattr(encoder, "configured_encoders", lambda directory=None: used)


def test_health_ok_when_the_profile_uses_the_best_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, ("av1_ffmpeg_vaapi_tex",) * 2)
    result = encoder.health_check({}, encoder.detect([node()], ALL_IDS))
    assert result["state"] == "ok" and result["group"] == "video" and result["id"] == "encoder"
    assert "AV1" in result["title"] and "renderD129" in result["detail"]


def test_health_warns_when_a_better_encoder_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, ("obs_x264", "obs_x264"))
    result = encoder.health_check({}, encoder.detect([node()], ALL_IDS))
    assert result["state"] == "warn" and "AV1" in result["title"] and "encoder --apply" in result["hint"]


def test_health_warns_when_the_profile_names_an_encoder_this_machine_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, ("av1_ffmpeg_vaapi_tex",) * 2)
    result = encoder.health_check({}, encoder.detect([], ALL_IDS))
    assert result["state"] == "warn" and "cannot use" in result["title"]


def test_health_is_fine_with_x264_when_there_is_no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, ("obs_x264", "obs_x264"))
    result = encoder.health_check({}, encoder.detect([], ALL_IDS))
    assert result["state"] == "ok" and "x264" in result["title"]


def test_health_without_a_profile_points_at_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, None)
    result = encoder.health_check({}, encoder.detect([node()], None, read_log=False))
    assert result["state"] == "warn" and result["hint"] == "streamstack setup" and "not listed" in result["detail"]


def test_health_reports_an_unreadable_profile_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(directory: Path | None = None) -> None:
        raise EncoderError("cannot read basic.ini: denied")

    monkeypatch.setattr(encoder, "configured_encoders", broken)
    result = encoder.health_check({}, encoder.detect([], ALL_IDS))
    assert result["state"] == "warn" and "denied" in result["detail"]


def test_setup_seeds_the_detected_encoder(monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> None:
    monkeypatch.setattr(encoder, "probe_nodes", lambda: [node()])
    monkeypatch.setattr(encoder, "obs_encoder_ids", lambda log_dir=None: ALL_IDS)
    assert cli.main(["setup", "--quiet"]) == 0
    profile_dir = isolated_home / ".config/obs-studio/basic/profiles/StreamStack"
    assert encoder.configured_encoders(profile_dir) == ("av1_ffmpeg_vaapi_tex",) * 2
    assert (profile_dir / "streamEncoder.json").is_file()


def test_encoder_command_lists_and_applies(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(encoder, "probe_nodes", lambda: [node()])
    monkeypatch.setattr(encoder, "obs_encoder_ids", lambda log_dir=None: ALL_IDS)
    monkeypatch.setattr(system, "running", lambda process: False)
    monkeypatch.setattr(encoder, "apply", lambda cfg, choice, directory=None: Path("/x/StreamStack"))
    assert cli.main(["encoder"]) == 0
    assert "av1_ffmpeg_vaapi_tex" in capsys.readouterr().out
    assert cli.main(["encoder", "--apply"]) == 0
    assert "now uses AV1" in capsys.readouterr().out


def test_encoder_command_reports_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(encoder, "probe_nodes", lambda: [node()])
    monkeypatch.setattr(encoder, "obs_encoder_ids", lambda log_dir=None: ALL_IDS)
    monkeypatch.setattr(system, "running", lambda process: True)
    assert cli.main(["encoder", "--apply"]) == 1
    assert "close OBS" in capsys.readouterr().err
