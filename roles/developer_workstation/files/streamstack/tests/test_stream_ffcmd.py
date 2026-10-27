from __future__ import annotations

import pytest

from streamstack.stream import ffcmd
from streamstack.stream.ffcmd import AudioSource, EncoderChoice, Source
from streamstack.stream.model import Destination, EncoderSettings

FF = "ffmpeg"
VA = EncoderChoice("av1_vaapi", "av1", "vaapi", "/dev/dri/renderD128")
QS = EncoderChoice("h264_qsv", "h264", "qsv", "/dev/dri/renderD128")
NV = EncoderChoice("hevc_nvenc", "hevc", "nvenc")
X264 = EncoderChoice("libx264", "h264", "software")
X265 = EncoderChoice("libx265", "hevc", "software")
SVT = EncoderChoice("libsvtav1", "av1", "software")


def value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_timeline_filters_use_the_epoch() -> None:
    assert ffcmd.timeline_filter("v4l2", 12.5) == "setpts=(PTS*TB-12.500000)/TB"
    assert "RTCTIME-12500000" in ffcmd.timeline_filter("lavfi", 12.5)
    assert ffcmd.audio_timeline_filter("pulse", 1.0).startswith("asetpts=(PTS*TB-1.000000)/TB")
    assert ffcmd.audio_timeline_filter("pulse", 1.0, 40).endswith("+0.040/TB")


@pytest.mark.parametrize("choice", [VA, QS, NV, X264, X265, SVT])
@pytest.mark.parametrize("rc", ["cbr", "vbr", "cqp"])
def test_every_encoder_builds_for_every_rate_control(choice: EncoderChoice, rc: str) -> None:
    settings = EncoderSettings(rate_control=rc)
    args = ffcmd.encoder_args(choice, settings, 6000, 60)
    assert args[:2] == ["-c:v", choice.name]
    assert ("-b:v" in args) == (rc != "cqp")
    assert "60" in args  # keyframe interval reaches the encoder


def test_cbr_pins_min_max_and_buffer() -> None:
    args = ffcmd.encoder_args(X264, EncoderSettings(), 4000, 60)
    assert value(args, "-b:v") == value(args, "-maxrate") == value(args, "-bufsize") == "4000k"


def test_unknown_encoder_is_an_error() -> None:
    with pytest.raises(ValueError):
        ffcmd.encoder_args(EncoderChoice("mystery", "h264", "software"), EncoderSettings(), 1000, 30)


def test_vaapi_pipeline_uploads_and_scales_on_the_gpu() -> None:
    src = Source(kind="v4l2", width=1920, height=1080, fps=30)
    argv = ffcmd.video_generation_argv(FF, src, VA, EncoderSettings(tier="720p"), 5.0)
    chain = value(argv, "-vf")
    assert "hwupload" in chain and "scale_vaapi=w=1280:h=720" in chain
    assert "vaapi=va:/dev/dri/renderD128" in argv
    assert value(argv, "-i") == "/dev/video43"
    assert "-force_key_frames" not in argv


def test_no_scaling_no_scale_filter_and_fps_filter_only_on_change() -> None:
    src = Source(width=1280, height=720, fps=30)
    chain = value(ffcmd.video_generation_argv(FF, src, X264, EncoderSettings(), 0.0), "-vf")
    assert "scale" not in chain and "fps=" not in chain
    chain = value(ffcmd.video_generation_argv(FF, src, X264, EncoderSettings(fps=60), 0.0), "-vf")
    assert "fps=60" in chain


def test_cut_forces_keyframes_from_the_cut_time() -> None:
    argv = ffcmd.video_generation_argv(FF, Source(), X264, EncoderSettings(keyint_s=2), 0.0, cut_s=7.25)
    assert value(argv, "-force_key_frames") == "expr:gte(t,7.250000+n_forced*2)"


def test_output_is_low_latency_matroska_on_stdout() -> None:
    argv = ffcmd.video_generation_argv(FF, Source(), X264, EncoderSettings(), 0.0)
    assert argv[-1] == "pipe:1" and value(argv, "-f") == "v4l2"
    assert value(argv, "-cluster_time_limit") == "1" and value(argv, "-flush_packets") == "1"
    assert "-an" in argv and "-copyts" in argv


def test_lavfi_source_for_tests() -> None:
    src = Source(kind="lavfi", width=320, height=180, fps=15)
    argv = ffcmd.video_generation_argv(FF, src, X264, EncoderSettings(), 0.0)
    assert value(argv, "-i") == "testsrc2=size=320x180:rate=15"


def test_keyint_follows_output_fps() -> None:
    argv = ffcmd.video_generation_argv(FF, Source(fps=30), X264, EncoderSettings(keyint_s=2, fps=60), 0.0)
    assert value(argv, "-g") == "120"


def test_audio_generation_is_opus_20ms() -> None:
    argv = ffcmd.audio_generation_argv(FF, AudioSource(name="mic"), EncoderSettings(audio_bitrate_kbps=96), 3.0)
    assert value(argv, "-c:a") == "libopus" and value(argv, "-b:a") == "96k"
    assert value(argv, "-frame_duration") == "20" and value(argv, "-i") == "mic"


def dest(kind: str, **kw: object) -> Destination:
    urls = {"srt": "srt://[::1]:9000", "rtmp": "rtmp://live.test/app", "whip": "https://w.test/x", "record": ""}
    return Destination.from_dict({"id": kind, "kind": kind, "url": urls[kind], **kw})


def test_srt_url_latency_in_microseconds_and_secret_quoted() -> None:
    url = ffcmd.srt_url(dest("srt", options={"latency_ms": 200, "mode": "listener"}), passphrase="p/w d")
    assert url.startswith("srt://[::1]:9000?")
    assert "latency=200000" in url and "mode=listener" in url and "passphrase=p%2Fw%20d" in url


def test_srt_sink_copies_to_mpegts() -> None:
    argv = ffcmd.sink_argv(FF, dest("srt"), "h264")
    assert value(argv, "-c") == "copy" and "mpegts" in argv and argv[-1].startswith("srt://")
    assert "-progress" in argv  # the sink reports when it is live


@pytest.mark.parametrize(("codec", "fourcc"), [("av1", "av01"), ("hevc", "hvc1")])
def test_enhanced_rtmp_names_the_fourcc(codec: str, fourcc: str) -> None:
    argv = ffcmd.sink_argv(FF, dest("rtmp"), codec, secret="k e/y")
    assert value(argv, "-rtmp_enhanced_codecs") == fourcc
    assert argv[-1] == "rtmp://live.test/app/k%20e%2Fy" and value(argv, "-c:a") == "aac"


def test_classic_rtmp_has_no_enhanced_flag() -> None:
    assert "-rtmp_enhanced_codecs" not in ffcmd.sink_argv(FF, dest("rtmp"), "h264", secret="k")


def test_record_mp4_is_fragmented() -> None:
    argv = ffcmd.sink_argv(FF, dest("record", options={"format": "mp4"}), "av1", target="/x/a.mp4")
    assert "+frag_keyframe+empty_moov+default_base_moof+skip_trailer" in argv and argv[-1] == "/x/a.mp4"


def test_whip_is_not_an_ffmpeg_sink() -> None:
    """The token must never reach an ffmpeg argv (where `ps` shows it): WHIP is published in-process."""
    with pytest.raises(ValueError, match="published by StreamStack itself"):
        ffcmd.sink_argv(FF, dest("whip"), "h264", secret="tok")
