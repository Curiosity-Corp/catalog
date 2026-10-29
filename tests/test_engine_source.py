"""Camera discovery, mode choice, encoder choice and the GPU probes, with no hardware."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from streamstack import config
from streamstack.engine import encode, probe, source
from streamstack.engine.graph import Source

CFG = dict(config.DEFAULTS)
MODES = [
    ("YUYV", 1920, 1080, 5.0),
    ("YUYV", 1920, 1080, 30.0),
    ("MJPG", 1920, 1080, 60.0),
    ("MJPG", 1280, 720, 60.0),
    ("H264", 1920, 1080, 30.0),
]


def test_cameras_lists_the_canon_first_then_the_cam_link() -> None:
    found = source.cameras(CFG, find_camlink=lambda: "/dev/video0", exists=lambda p: True)
    assert [(c.label, c.device, c.kind) for c in found] == [
        ("Canon EOS 50D", "/dev/video42", "canon"),
        ("Cam Link 4K", "/dev/video0", "camlink"),
    ]


def test_cameras_leaves_out_an_absent_cam_link_but_keeps_the_canon() -> None:
    assert [c.kind for c in source.cameras(CFG, find_camlink=lambda: "")] == ["canon"]
    assert [c.kind for c in source.cameras(CFG, find_camlink=lambda: "/dev/video9", exists=lambda p: False)] == [
        "canon"
    ]


def test_a_fed_loopback_is_read_in_the_format_it_delivers() -> None:
    camera = source.Camera("Canon EOS 50D", "/dev/video42", "canon")
    src = source.resolve(camera, CFG, list_modes=lambda d: [], current=lambda d: ("YUYV", 1280, 720, 20.0))
    assert src == Source("Canon EOS 50D", "/dev/video42", "YUYV", 1280, 720, 20.0)


def test_a_capture_card_gets_the_best_readable_mode() -> None:
    camera = source.Camera("Cam Link 4K", "/dev/video0", "camlink")
    src = source.resolve(camera, CFG, list_modes=lambda d: MODES, current=lambda d: None)
    assert (src.fourcc, src.width, src.height, src.fps) == ("MJPG", 1920, 1080, 60.0)  # never the unreadable H264


def test_the_cam_link_mode_can_be_pinned() -> None:
    camera = source.Camera("Cam Link 4K", "/dev/video0", "camlink")
    cfg = {**CFG, "STREAMSTACK_CAMLINK_MODE": "1920x1080@30 YUYV"}
    src = source.resolve(camera, cfg, list_modes=lambda d: MODES, current=lambda d: None)
    assert (src.fourcc, src.fps) == ("YUYV", 30.0)


def test_a_source_with_no_readable_format_says_so() -> None:
    camera = source.Camera("Webcam", "/dev/video2", "camlink")
    with pytest.raises(source.SourceError, match="H264"):
        source.resolve(camera, CFG, list_modes=lambda d: [("H264", 1920, 1080, 30.0)], current=lambda d: None)


def test_no_signal_and_no_device_are_source_errors() -> None:
    camera = source.Camera("Cam Link 4K", "/dev/video0", "camlink")
    with pytest.raises(source.SourceError, match="no picture"):
        source.resolve(camera, CFG, list_modes=lambda d: [], current=lambda d: None)
    with pytest.raises(source.SourceError, match="not connected"):
        source.resolve(source.Camera("X", "", "camlink"), CFG)


def test_unreported_frame_rate_falls_back_to_the_configured_one() -> None:
    camera = source.Camera("Canon EOS 50D", "/dev/video42", "canon")
    src = source.resolve(camera, CFG, list_modes=lambda d: [], current=lambda d: ("YUYV", 1280, 720, 0.0))
    assert src.fps == 30.0


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("source", (1280, 720)),
        ("", (1280, 720)),
        ("1920x1080", (1920, 1080)),
        ("junk", (1280, 720)),
        ("1921x1080", (1280, 720)),
    ],
)
def test_output_size(setting: str, expected: tuple[int, int]) -> None:
    src = Source("c", "/dev/video42", "YUYV", 1280, 720, 30.0)
    assert source.output_size({"STREAMSTACK_ENGINE_SIZE": setting}, src) == expected


LOW_CARD = Source("Cam Link 4K", "/dev/video0", "YUYV", 720, 480, 21.8)  # deskmeet1: HDMI source at 480p


def test_a_480p_card_keeps_the_program_node_at_720p_30_fps_letterboxed() -> None:
    plan = source.output_plan({"STREAMSTACK_ENGINE_SIZE": "source"}, LOW_CARD)
    assert plan == source.Output(1280, 720, fps=30.0, letterbox=True)


@pytest.mark.parametrize("setting", ["source", "", "auto", "junk", "1921x1080"])
def test_every_automatic_setting_holds_the_floor(setting: str) -> None:
    plan = source.output_plan({"STREAMSTACK_ENGINE_SIZE": setting}, LOW_CARD)
    assert (plan.width, plan.height, plan.letterbox) == (1280, 720, True)


def test_a_camera_at_or_above_the_contract_is_passed_through() -> None:
    for width, height, fps in [(1280, 720, 30.0), (1920, 1080, 60.0), (3840, 2160, 24.0)]:
        plan = source.output_plan({}, Source("c", "/dev/video0", "YUYV", width, height, fps))
        assert plan == source.Output(width, height)


def test_a_slow_camera_is_repeated_up_to_30_fps_but_24_fps_is_left_alone() -> None:
    assert source.output_plan({}, Source("c", "/dev/video0", "YUYV", 1280, 720, 20.0)).fps == 30.0
    assert source.output_plan({}, Source("c", "/dev/video0", "YUYV", 1280, 720, 24.0)).fps == 0.0


def test_an_explicit_size_wins_but_the_frame_rate_floor_still_applies() -> None:
    plan = source.output_plan({"STREAMSTACK_ENGINE_SIZE": "1920x1080"}, LOW_CARD)
    assert plan == source.Output(1920, 1080, fps=30.0, letterbox=False)


def test_output_size_follows_the_plan() -> None:
    assert source.output_size({}, LOW_CARD) == (1280, 720)


# -- encoders -----------------------------------------------------------------------------------------------------


def test_the_best_working_codec_wins() -> None:
    assert encode.choose_encoder(probe=lambda e: True).name == "av1_qsv"
    assert encode.choose_encoder(probe=lambda e: e.codec != "av1").name == "hevc_qsv"
    assert encode.choose_encoder(probe=lambda e: e.codec == "h264").name == "h264_qsv"


def test_vaapi_is_chosen_when_quick_sync_fails() -> None:
    # dm313red: hevc_qsv fails on oneVPL 26.1, hevc_vaapi encodes
    chosen = encode.choose_encoder("hevc", probe=lambda e: e.backend != "qsv")
    assert chosen.name == "hevc_vaapi" and chosen.backend == "vaapi"
    assert encode.choose_encoder(probe=lambda e: e.name != "av1_qsv" and e.codec == "av1").name == "av1_vaapi"


def test_inside_a_codec_the_order_is_quick_sync_vaapi_nvenc() -> None:
    for codec in encode.CODECS:
        assert [e.backend for e in encode.ENCODERS if e.codec == codec] == ["qsv", "vaapi", "nvenc"]


def test_a_codec_preference_skips_the_better_ones() -> None:
    assert encode.choose_encoder("hevc", probe=lambda e: True).codec == "hevc"
    with pytest.raises(encode.NoEncoder, match="Unknown codec"):
        encode.choose_encoder("av2")


def test_no_working_encoder_names_everything_tried() -> None:
    with pytest.raises(encode.NoEncoder) as caught:
        encode.choose_encoder(probe=lambda e: False)
    assert "av1_qsv" in str(caught.value) and "h264_nvenc" in str(caught.value)


def test_av2_has_no_row_until_hardware_exists() -> None:
    assert {e.codec for e in encode.ENCODERS} == {"av1", "hevc", "h264"}


def test_a_tap_carries_the_encoder_and_bitrate() -> None:
    enc = encode.choose_encoder(probe=lambda e: True)
    tap = encode.tap_for(enc, "srt://host:9000", "mpegts", 8000)
    assert tap.encoder == "av1_qsv" and tap.format == "mpegts" and "8000k" in tap.args
    with pytest.raises(encode.NoEncoder):
        encode.tap_for(enc, "x", "null", 0)


def test_probe_encoder_reports_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"Error: no device")

    monkeypatch.setattr(subprocess, "run", run)
    assert encode.probe_encoder(encode.ENCODERS[0]) is False
    assert "qsv=q:hw" in calls[0] and "av1_qsv" in calls[0]


def test_probe_encoder_for_vaapi_uploads_on_the_render_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    enc = next(e for e in encode.ENCODERS if e.name == "hevc_vaapi")
    assert encode.probe_encoder(enc, node="/dev/dri/renderD128") is True
    argv = calls[0]
    assert argv[argv.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD128"
    assert argv[argv.index("-filter_hw_device") + 1] == "va"
    assert argv[argv.index("-vf") + 1] == "format=nv12,hwupload"
    assert argv[argv.index("-c:v") + 1] == "hevc_vaapi" and "-low_power" in argv


def test_a_vaapi_tap_asks_for_the_upload() -> None:
    enc = next(e for e in encode.ENCODERS if e.name == "h264_vaapi")
    tap = encode.tap_for(enc, "pipe:5", "mpegts", 4000)
    assert tap.upload and tap.encoder == "h264_vaapi" and "-bf" in tap.args
    assert not encode.tap_for(encode.ENCODERS[0], "pipe:5", "mpegts", 4000).upload


def test_probe_encoder_survives_a_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", run)
    assert encode.probe_encoder(encode.ENCODERS[0]) is False


# -- probes -------------------------------------------------------------------------------------------------------


def test_render_node_prefers_the_configured_then_intel(tmp_path: Path) -> None:
    sysfs = tmp_path / "drm"
    for name, vendor in (("renderD128", "0x10de"), ("renderD129", "0x8086")):
        (sysfs / name / "device").mkdir(parents=True)
        (sysfs / name / "device" / "vendor").write_text(vendor + "\n")
    assert probe.render_node("/dev/dri/renderD200", sysfs) == "/dev/dri/renderD200"
    assert probe.render_node("", sysfs) == "/dev/dri/renderD129"
    assert probe.render_node("", tmp_path / "none") == "/dev/dri/renderD128"


def _drm(root: Path, nodes: dict[str, tuple[str, str, str]]) -> Path:
    """A fake /sys/class/drm whose render nodes link to PCI devices, as the kernel lays it out."""
    sysfs = root / "class" / "drm"
    sysfs.mkdir(parents=True)
    for name, (address, vendor, device) in nodes.items():
        pci = root / "devices" / "pci0000:00" / address
        pci.mkdir(parents=True, exist_ok=True)
        (pci / "vendor").write_text(vendor + "\n")
        (pci / "device").write_text(device + "\n")
        (sysfs / name).mkdir()
        (sysfs / name / "device").symlink_to(pci)
    return sysfs


def test_render_node_picks_the_arc_by_pci_device_not_by_node_number(tmp_path: Path) -> None:
    # deskmeet1: the UHD 770 iGPU enumerates first as renderD128, the Arc A380 is renderD129.
    deskmeet1 = _drm(
        tmp_path / "deskmeet1",
        {"renderD128": ("0000:00:02.0", "0x8086", "0xa780"), "renderD129": ("0000:03:00.0", "0x8086", "0x56a5")},
    )
    assert probe.render_node("", deskmeet1) == "/dev/dri/renderD129"
    # dm313red: the Arc alone, as renderD128.
    dm313red = _drm(tmp_path / "dm313red", {"renderD128": ("0000:03:00.0", "0x8086", "0x56a5")})
    assert probe.render_node("", dm313red) == "/dev/dri/renderD128"
    # An iGPU-only machine still gets its Intel GPU, and an explicit node always wins.
    igpu = _drm(tmp_path / "igpu", {"renderD128": ("0000:00:02.0", "0x8086", "0xa780")})
    assert probe.render_node("", igpu) == "/dev/dri/renderD128"
    assert probe.render_node("/dev/dri/renderD128", deskmeet1) == "/dev/dri/renderD128"


def test_probe_backend_is_vulkan_only_when_the_graph_runs() -> None:
    ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"", b"")  # noqa: E731
    bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"first\nlibplacebo: no device\n")  # noqa: E731
    assert probe.probe_backend("/dev/dri/renderD128", run=ok) == ("vulkan", "")
    assert probe.probe_backend("/dev/dri/renderD128", run=bad) == ("cpu", "libplacebo: no device")


def test_probe_backend_survives_ffmpeg_failures() -> None:
    def missing(argv, **kw):
        raise FileNotFoundError

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert probe.probe_backend("n", run=missing) == ("cpu", "ffmpeg is not installed")
    assert probe.probe_backend("n", run=slow)[0] == "cpu"


def test_engine_probe_command_reports_backend_and_encoders(monkeypatch, capsys) -> None:
    from streamstack import cli
    from streamstack.engine import encode, probe
    from streamstack.hardware import roles

    class NoPlan:
        def get(self) -> None:
            return None

    monkeypatch.setattr(roles, "LivePlan", NoPlan)
    monkeypatch.setattr(roles, "generic_node", lambda *a, **kw: "/dev/dri/renderD128")
    monkeypatch.setattr(probe, "probe_backend", lambda node, *a, **kw: ("vulkan", ""))
    monkeypatch.setattr(probe, "probe_mjpeg", lambda node, *a, **kw: ("software", "libplacebo: import failed"))
    monkeypatch.setattr(encode, "probe_encoder", lambda e, ffmpeg="ffmpeg", node="": e.name == "av1_qsv")
    assert cli.main(["engine", "probe"]) == 0
    out = capsys.readouterr().out
    assert "backend: vulkan" in out
    assert "mjpeg decode: software (libplacebo: import failed)" in out
    assert "encoder av1_qsv: ok" in out
    assert "encoder h264_qsv: unavailable" in out
