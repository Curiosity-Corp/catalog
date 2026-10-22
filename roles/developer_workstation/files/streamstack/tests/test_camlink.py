"""Cam Link: finding the card by USB id, reading its modes, choosing the best one and tuning OBS to it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamstack import camlink, cli, config, system

FIXTURES = Path(__file__).parent / "fixtures/v4l2"
CFG = config.load_settings(Path("/nonexistent"))


def modes(name: str) -> list[system.Mode]:
    return system.parse_modes((FIXTURES / name).read_text())


def best(name: str, **options: object) -> str:
    chosen = camlink.choose_mode(modes(name), **options)  # type: ignore[arg-type]
    return camlink.short_label(chosen) if chosen else ""


# -- the choice -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("camlink_1080p5994.txt", "1080p59.94 YUYV"),  # raw YUYV and NV12 tie; the first listed wins
        ("camlink_1080p2997.txt", "1080p29.97 YUYV"),  # the rig as found: one mode, whatever the source sends
        ("camlink_1080p50.txt", "1080p50 YUYV"),
        ("camlink_720p60.txt", "720p60 YUYV"),
        ("webcam_mjpg60.txt", "1080p60 MJPG"),  # a 5 fps raw mode is useless; frame rate comes before the format
        ("camlink_nosignal.txt", ""),
    ],
)
def test_best_mode_per_recorded_listing(fixture: str, expected: str) -> None:
    assert best(fixture) == expected


def test_a_4k_source_is_scaled_down_unless_4k_is_wanted() -> None:
    # Only 4K is offered: take it (OBS scales it to the canvas) rather than nothing.
    assert best("camlink_4k30.txt") == "2160p29.97 YUYV"
    mixed = modes("camlink_4k30.txt") + modes("camlink_1080p5994.txt")
    assert camlink.short_label(camlink.choose_mode(mixed)) == "1080p59.94 YUYV"  # type: ignore[arg-type]
    assert camlink.short_label(camlink.choose_mode(mixed, allow_4k=True)) == "2160p29.97 YUYV"  # type: ignore[arg-type]


def test_raw_beats_compressed_at_the_same_size_and_rate() -> None:
    offered: list[system.Mode] = [("MJPG", 1920, 1080, 60.0), ("NV12", 1920, 1080, 60.0), ("H264", 1920, 1080, 60.0)]
    assert camlink.choose_mode(offered) == ("NV12", 1920, 1080, 60.0)


def test_60_beats_50_beats_30_at_1080() -> None:
    offered: list[system.Mode] = [("YUYV", 1920, 1080, 30.0), ("YUYV", 1920, 1080, 50.0), ("YUYV", 1920, 1080, 25.0)]
    assert camlink.choose_mode(offered) == ("YUYV", 1920, 1080, 50.0)
    offered.append(("YUYV", 1920, 1080, 59.94))
    assert camlink.choose_mode(offered) == ("YUYV", 1920, 1080, 59.94)


def test_a_pinned_mode_wins_when_offered_and_falls_back_to_auto_when_not() -> None:
    offered = modes("webcam_mjpg60.txt")
    assert camlink.choose_mode(offered, pin="1280x720@60 MJPG") == ("MJPG", 1280, 720, 60.0)
    assert camlink.choose_mode(offered, pin="1280x720") == ("MJPG", 1280, 720, 60.0)
    assert camlink.choose_mode(offered, pin="3840x2160@60") == ("MJPG", 1920, 1080, 60.0)
    assert camlink.choose_mode(offered, pin="nonsense") == ("MJPG", 1920, 1080, 60.0)
    with pytest.raises(ValueError):
        camlink.parse_pin("nonsense")


def test_frame_rates_become_exact_fractions() -> None:
    assert camlink.fps_fraction(59.94) == (60000, 1001)
    assert camlink.fps_fraction(29.97) == (30000, 1001)
    assert camlink.fps_fraction(23.976) == (24000, 1001)
    assert camlink.fps_fraction(60.0) == (60, 1)
    assert camlink.fps_fraction(50.0) == (50, 1)
    assert camlink.fps_fraction(0.0) == (1, 1)


# -- OBS settings -----------------------------------------------------------------------------


def test_obs_input_settings_pack_format_size_and_interval() -> None:
    settings = camlink.obs_input_settings(("YUYV", 1920, 1080, 59.94))
    assert settings["pixelformat"] == 0x56595559  # 'YUYV' little endian, V4L2_PIX_FMT_YUYV
    assert settings["resolution"] == (1920 << 16) | 1080
    assert settings["framerate"] == (1001 << 16) | 60000  # the frame interval 1001/60000 s
    assert camlink.obs_input_settings(("NV12", 1920, 1080, 60.0))["pixelformat"] == 0x3231564E


def test_video_size_follows_the_card_unless_switched_off() -> None:
    mode = ("YUYV", 1920, 1080, 59.94)
    assert camlink.video_size(CFG, mode) == (1920, 1080, 60000, 1001)
    assert camlink.video_size(CFG, None) == (1920, 1080, 30, 1)
    assert camlink.video_size(dict(CFG, STREAMSTACK_CAMLINK_MODE="off"), mode) == (1920, 1080, 30, 1)
    assert camlink.video_size(dict(CFG, STREAMSTACK_WIDTH="1280", STREAMSTACK_HEIGHT="720"), None)[:2] == (1280, 720)


def test_scene_collection_patch_only_touches_the_cam_link_source() -> None:
    text = json.dumps(
        {
            "sources": [
                {"id": "v4l2_input", "name": "Cam Link 4K", "settings": {"device_id": "/dev/streamstack/camlink"}},
                {"id": "v4l2_input", "name": "Canon EOS 50D", "settings": {"device_id": "/dev/video42"}},
            ]
        }
    )
    sources = json.loads(camlink.patch_scene_collection(text, ("YUYV", 1920, 1080, 60.0)))["sources"]
    assert sources[0]["settings"]["device_id"] == "/dev/streamstack/camlink"
    assert sources[0]["settings"]["resolution"] == (1920 << 16) | 1080
    assert "resolution" not in sources[1]["settings"]
    with pytest.raises(camlink.CamlinkError):
        camlink.patch_scene_collection("{nope", ("YUYV", 1920, 1080, 60.0))


class FakeOBS:
    def __init__(self, active: set[str] | None = None, refuse: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.active, self.refuse = active or set(), refuse
        self.closed = False

    def call(self, request: str, **data: object) -> dict:
        self.calls.append((request, data))
        if request == self.refuse:
            raise RuntimeError(f"{request}: refused")
        status = {"GetVirtualCamStatus": "virtual", "GetRecordStatus": "record", "GetStreamStatus": "stream"}
        return {"outputActive": status.get(request) in self.active}

    def close(self) -> None:
        self.closed = True


def test_apply_tunes_the_source_then_the_canvas_and_remembers_it() -> None:
    obs = FakeOBS()
    steps = camlink.apply_to_obs(CFG, ("YUYV", 1920, 1080, 59.94), obs)
    assert steps[0].startswith("Cam Link 4K source set to 1080p59.94")
    assert "canvas set to 1920x1080 at 59.94 fps" in steps[-1]
    requests = dict(obs.calls)
    assert requests["SetInputSettings"]["inputName"] == "Cam Link 4K"
    assert (
        requests["SetVideoSettings"]["fpsNumerator"] == 60000 and requests["SetVideoSettings"]["fpsDenominator"] == 1001
    )
    assert camlink.load_applied() == ("YUYV", 1920, 1080, 59.94)
    assert not obs.closed  # a connection the caller passed in stays the caller's


def test_apply_never_resizes_the_canvas_while_on_air() -> None:
    obs = FakeOBS(active={"virtual"})
    steps = camlink.apply_to_obs(CFG, ("YUYV", 1920, 1080, 60.0), obs)
    assert "SetVideoSettings" not in dict(obs.calls)
    assert "SetInputSettings" in dict(obs.calls)
    assert "virtual camera is on" in steps[-1]


def test_apply_reports_what_obs_refuses() -> None:
    with pytest.raises(camlink.CamlinkError, match="Cam Link 4K source"):
        camlink.apply_to_obs(CFG, ("YUYV", 1920, 1080, 60.0), FakeOBS(refuse="SetInputSettings"))
    with pytest.raises(camlink.CamlinkError, match="canvas"):
        camlink.apply_to_obs(CFG, ("YUYV", 1920, 1080, 60.0), FakeOBS(refuse="SetVideoSettings"))
    assert camlink.load_applied() is None  # nothing is recorded as applied after a failure


def test_apply_without_obs_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("streamstack.obs.OBS", refuse)
    with pytest.raises(camlink.CamlinkError, match="cannot reach OBS"):
        camlink.apply_to_obs(CFG, ("YUYV", 1920, 1080, 60.0))


def test_applied_mode_survives_a_corrupt_state_file() -> None:
    camlink.save_applied(("NV12", 1280, 720, 60.0))
    assert camlink.load_applied() == ("NV12", 1280, 720, 60.0)
    camlink.applied_file().write_text("{broken")
    assert camlink.load_applied() is None


# -- probing ----------------------------------------------------------------------------------


@pytest.fixture
def card(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A plugged-in card whose listing and current format the test picks."""
    listing = {"modes": "camlink_1080p5994.txt", "current": "camlink_current_5994.txt"}
    monkeypatch.setattr(system, "find_camlink", lambda: "/dev/video0")
    monkeypatch.setattr(system, "camera_modes", lambda device: modes(listing["modes"]))
    monkeypatch.setattr(
        system, "current_mode", lambda device: system.parse_current((FIXTURES / listing["current"]).read_text())
    )
    return listing


def test_probe_reads_the_signal_and_picks_the_best_mode(card: dict[str, str]) -> None:
    probe = camlink.probe(CFG, {"alsa_input.usb-Elgato_Cam_Link_4K.analog-stereo": "Cam Link 4K Analog Stereo"})
    assert probe.present and probe.signal and not probe.retune
    assert probe.chosen == ("YUYV", 1920, 1080, 59.94)
    assert probe.audio == "Cam Link 4K Analog Stereo"
    shown = probe.public()
    assert shown["mode"] == "1080p59.94 YUYV" and shown["state"] == "ok"
    assert shown["active"] == "1920x1080 YUYV @ 59.94 fps"
    assert shown["offered"] == ["1080p59.94 YUYV", "1080p59.94 NV12"]


def test_probe_with_the_source_off_has_no_signal(card: dict[str, str]) -> None:
    card["modes"] = "camlink_nosignal.txt"
    shown = camlink.probe(CFG, {}).public()
    assert shown["present"] and not shown["signal"] and shown["state"] == "off" and shown["mode"] == ""


def test_probe_without_a_card_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "find_camlink", lambda: "")
    assert camlink.probe(CFG, {}).public()["present"] is False


def test_a_signal_change_after_tuning_asks_for_a_retune(card: dict[str, str]) -> None:
    camlink.save_applied(("YUYV", 1920, 1080, 29.97))
    probe = camlink.probe(CFG, {})
    assert probe.retune and probe.public()["state"] == "info"
    camlink.save_applied(("YUYV", 1920, 1080, 59.94))
    assert not camlink.probe(CFG, {}).retune


def test_a_malformed_pin_is_reported_but_never_blocks_the_picture(card: dict[str, str]) -> None:
    probe = camlink.probe(dict(CFG, STREAMSTACK_CAMLINK_MODE="fast please"), {})
    assert probe.chosen == ("YUYV", 1920, 1080, 59.94)
    assert "STREAMSTACK_CAMLINK_MODE ignored" in probe.note


# -- the health checks and the CLI ---------------------------------------------------------------


def checks_for(probe: camlink.Probe) -> dict[str, dict[str, str]]:
    return {c["id"]: c for c in cli.camlink_checks(probe)}


def test_health_names_the_mode_the_modes_on_offer_and_the_hdmi_audio() -> None:
    probe = camlink.Probe(
        device="/dev/video0",
        offered=modes("camlink_1080p5994.txt"),
        chosen=("YUYV", 1920, 1080, 59.94),
        active=("YUYV", 1920, 1080, 29.97),
        audio="Cam Link 4K Analog Stereo",
    )
    checks = checks_for(probe)
    assert checks["camlink"]["state"] == "ok"
    assert checks["camlink"]["title"] == "Elgato Cam Link on /dev/video0: 1080p59.94 YUYV"
    assert "offers 1080p59.94 YUYV, 1080p59.94 NV12" in checks["camlink"]["detail"]
    assert checks["camlink-audio"]["group"] == "audio"
    assert "not a call microphone" in checks["camlink-audio"]["title"]


def test_health_hints_at_30_fps_sources_and_changed_modes() -> None:
    thirty = camlink.Probe(
        device="/dev/video0", offered=modes("camlink_1080p2997.txt"), chosen=("YUYV", 1920, 1080, 29.97)
    )
    assert "1080p60" in checks_for(thirty)["camlink"]["hint"]
    changed = camlink.Probe(
        device="/dev/video0",
        offered=modes("camlink_1080p5994.txt"),
        chosen=("YUYV", 1920, 1080, 59.94),
        applied=("YUYV", 1920, 1080, 29.97),
    )
    assert "streamstack camlink --apply" in checks_for(changed)["camlink"]["hint"]


def test_health_without_signal_or_card() -> None:
    quiet = checks_for(camlink.Probe(device="/dev/video0"))["camlink"]
    assert quiet["state"] == "ok" and "no HDMI signal" in quiet["title"] and quiet["hint"]
    missing = checks_for(camlink.Probe())
    assert missing["camlink"]["state"] == "warn" and "camlink-audio" not in missing


def test_cam_link_audio_is_never_the_call_microphone() -> None:
    sources = {
        "alsa_input.usb-Elgato_Cam_Link_4K-00.analog-stereo": "Cam Link 4K Analog Stereo",
        "alsa_input.usb-0fd9_00-00.iec958-stereo": "Cam Link 4K Digital Stereo (IEC958)",
        "alsa_input.usb-Elgato_Wave_3-00.mono-fallback": "Wave:3",
    }
    cam = "alsa_input.usb-Elgato_Cam_Link_4K-00.analog-stereo"
    assert system.pick_microphone(sources, "", cam) == "alsa_input.usb-Elgato_Wave_3-00.mono-fallback"
    assert system.pick_microphone({cam: sources[cam]}, "", cam) == ""  # the default input is the card: no microphone
    assert system.pick_microphone({"x": "Cam Link 4K Digital Stereo"}, "cam link") == ""  # even when asked for
    assert system.capture_card_audio(sources)[0].startswith("alsa_input.usb-Elgato_Cam_Link")
    assert system.capture_card_audio({"alsa_input.wave": "Wave:3"}) == ("", "")


def test_camlink_command_prints_the_offer_and_applies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = camlink.Probe(
        device="/dev/video0", offered=modes("camlink_1080p5994.txt"), chosen=("YUYV", 1920, 1080, 59.94)
    )
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: probe)
    assert cli.main(["camlink"]) == 0
    out = capsys.readouterr().out
    assert "best:   1080p59.94 YUYV" in out and "--apply" in out
    monkeypatch.setattr(camlink, "apply_to_obs", lambda cfg, mode: ["Cam Link 4K source set to 1080p59.94 YUYV"])
    assert cli.main(["camlink", "--apply"]) == 0
    assert "source set to 1080p59.94" in capsys.readouterr().out

    def refuse(cfg: dict[str, str], mode: system.Mode) -> list[str]:
        raise camlink.CamlinkError("cannot reach OBS")

    monkeypatch.setattr(camlink, "apply_to_obs", refuse)
    assert cli.main(["camlink", "--apply"]) == 1
    assert "cannot reach OBS" in capsys.readouterr().err


def test_camlink_command_without_card_or_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: camlink.Probe())
    assert cli.main(["camlink"]) == 1
    assert "No Elgato Cam Link" in capsys.readouterr().out
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: camlink.Probe(device="/dev/video0"))
    assert cli.main(["camlink"]) == 1
    assert "no HDMI signal" in capsys.readouterr().out


def test_setup_seeds_the_cam_link_mode_into_a_new_obs_profile(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    probe = camlink.Probe(
        device="/dev/video0", chosen=("YUYV", 1920, 1080, 59.94), offered=[("YUYV", 1920, 1080, 59.94)]
    )
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: probe)
    assert cli.main(["setup", "--quiet"]) == 0
    obs = isolated_home / ".config/obs-studio/basic"
    ini = (obs / "profiles/StreamStack/basic.ini").read_text()
    assert "FPSType=2\nFPSNum=60000\nFPSDen=1001" in ini
    sources = {s["name"]: s for s in json.loads((obs / "scenes/StreamStack.json").read_text())["sources"]}
    assert sources["Cam Link 4K"]["settings"]["framerate"] == (1001 << 16) | 60000
    assert sources["Cam Link 4K"]["settings"]["device_id"] == "/dev/streamstack/camlink"


# -- finding the card -----------------------------------------------------------------------------


def make_node(sysfs: Path, usb: Path, name: str, index: int, vendor: str, product: str) -> None:
    device = usb / vendor / f"{vendor}:{product}"
    device.mkdir(parents=True, exist_ok=True)
    (device / "idVendor").write_text(vendor + "\n")
    (device / "idProduct").write_text(product + "\n")
    interface = device / "1-4:1.0"
    interface.mkdir(exist_ok=True)
    node = sysfs / name
    node.mkdir(parents=True)
    (node / "index").write_text(f"{index}\n")
    (node / "device").symlink_to(interface)


def test_the_card_is_found_by_usb_id_not_by_node_number(tmp_path: Path) -> None:
    sysfs, usb = tmp_path / "video4linux", tmp_path / "usb"
    make_node(sysfs, usb, "video2", 0, "046d", "085e")  # another UVC camera
    make_node(sysfs, usb, "video7", 0, "0fd9", "0066")  # the Cam Link, today on node 7
    make_node(sysfs, usb, "video8", 1, "0fd9", "0066")  # its metadata node
    assert system.video_by_usb_id(system.CAMLINK_USB_IDS, sysfs) == ["/dev/video7"]
    assert system.video_by_usb_id((("dead", "beef"),), sysfs) == []
    assert system.video_by_usb_id(system.CAMLINK_USB_IDS, tmp_path / "absent") == []


def test_find_camlink_prefers_the_udev_link_then_the_usb_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.undo()  # the autouse stub in conftest hides the real function
    link = tmp_path / "camlink"
    monkeypatch.setattr(system, "CAMLINK_LINK", link)
    monkeypatch.setattr(system, "video_by_usb_id", lambda ids, sysfs=system.SYSFS_VIDEO: ["/dev/video9"])
    assert system.find_camlink() == "/dev/video9"
    link.symlink_to("/dev/null")
    assert system.find_camlink() == str(link)


def test_current_format_is_parsed_from_the_driver() -> None:
    assert system.parse_current((FIXTURES / "camlink_current_2997.txt").read_text()) == ("YUYV", 1920, 1080, 29.97)
    assert system.parse_current((FIXTURES / "camlink_current_5994.txt").read_text()) == ("YUYV", 1920, 1080, 59.94)
    assert system.parse_current("") is None
