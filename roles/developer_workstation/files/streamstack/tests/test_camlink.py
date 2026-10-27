"""Cam Link: finding the card by USB id, reading its modes, and choosing the best one (the video engine follows it)."""

from __future__ import annotations

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
    # Only 4K is offered: take it (the engine scales it down) rather than nothing.
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
    assert probe.present and probe.signal
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


def test_health_hints_at_30_fps_sources() -> None:
    thirty = camlink.Probe(
        device="/dev/video0", offered=modes("camlink_1080p2997.txt"), chosen=("YUYV", 1920, 1080, 29.97)
    )
    assert "1080p60" in checks_for(thirty)["camlink"]["hint"]


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


def test_camlink_command_prints_the_offer(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    probe = camlink.Probe(
        device="/dev/video0", offered=modes("camlink_1080p5994.txt"), chosen=("YUYV", 1920, 1080, 59.94)
    )
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: probe)
    assert cli.main(["camlink"]) == 0
    out = capsys.readouterr().out
    assert "best:   1080p59.94 YUYV" in out and "offers: 1080p59.94 YUYV, 1080p59.94 NV12" in out


def test_camlink_has_no_apply_flag() -> None:
    with pytest.raises(SystemExit):
        cli.main(["camlink", "--apply"])


def test_camlink_command_without_card_or_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: camlink.Probe())
    assert cli.main(["camlink"]) == 1
    assert "No Elgato Cam Link" in capsys.readouterr().out
    monkeypatch.setattr(camlink, "probe", lambda cfg=None, sources=None: camlink.Probe(device="/dev/video0"))
    assert cli.main(["camlink"]) == 1
    assert "no HDMI signal" in capsys.readouterr().out


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


def test_both_cam_link_4k_revisions_are_found_by_usb_id(tmp_path: Path) -> None:
    # deskmeet1 and dm313red both have 0fd9:0067; before it was listed the card was only found by its name.
    sysfs, usb = tmp_path / "video4linux", tmp_path / "usb"
    make_node(sysfs, usb, "video0", 0, "0fd9", "0067")
    make_node(sysfs, usb, "video1", 1, "0fd9", "0067")
    make_node(sysfs, usb, "video5", 0, "0fd9", "0066")
    assert system.video_by_usb_id(system.CAMLINK_USB_IDS, sysfs) == ["/dev/video0", "/dev/video5"]


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
