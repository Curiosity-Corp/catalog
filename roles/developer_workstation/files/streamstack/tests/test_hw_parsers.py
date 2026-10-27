"""Every parser against the recorded captures of both studio hosts, plus failure injection for each."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from hardware_support import HOSTS, inventory_text, section

from streamstack.hardware import inventory, parsers
from streamstack.hardware.parsers import ParseError, SourceFailure

pytestmark = pytest.mark.parametrize("host", HOSTS)


def snap(host: str):
    return inventory.snapshot_from_inventory(inventory_text(host))


# -- recorded facts per host ----------------------------------------------------------------------------------------

USB = {
    "dm313red": {"0fd9:007d", "0fd9:0067", "04a9:319b", "046d:c901"},
    "deskmeet1": {"0fd9:006d", "0fd9:0067", "04a9:319b", "046d:c901"},
}
NODES = {
    "dm313red": {"/dev/dri/renderD128": "8086:56a5"},
    "deskmeet1": {"/dev/dri/renderD128": "8086:a780", "/dev/dri/renderD129": "8086:56a5"},
}


def test_lsusb(host: str) -> None:
    nodes = parsers.parse_lsusb(section(host, "lsusb"))
    assert USB[host] <= {n.usb_id for n in nodes}
    assert all(n.bus >= 1 and n.address >= 1 for n in nodes)


def test_lspci(host: str) -> None:
    devices = parsers.parse_lspci(section(host, "lspci-gpu"))
    by_id = {d.pci_id: d for d in devices}
    assert by_id["8086:56a5"].class_code == "0300"
    assert "Arc A380" in by_id["8086:56a5"].name
    assert ("8086:a780" in by_id) == (host == "deskmeet1")


def test_drm_render_nodes_are_identified_by_pci_id(host: str) -> None:
    pci = parsers.parse_lspci(section(host, "lspci-gpu"))
    nodes = parsers.parse_drm(section(host, "drm"), pci)
    assert {n.path: n.pci_id for n in nodes} == NODES[host]
    assert all(n.driver == "i915" and n.name for n in nodes)


def test_vainfo_per_node(host: str) -> None:
    info = parsers.parse_vainfo(section(host, "va-per-node"))
    assert set(info) == set(NODES[host])
    arc = next(p for p, pci in NODES[host].items() if pci == "8086:56a5")
    assert info[arc].encode_codecs() == ("h264", "hevc", "av1")
    assert info[arc].can_encode("VAProfileAV1Profile0", "VAEntrypointEncSliceLP")
    assert "hevc" in info[arc].decode_codecs()
    if host == "deskmeet1":
        assert info["/dev/dri/renderD128"].encode_codecs() == ("h264", "hevc")


def test_vulkaninfo_missing_tool_is_absent(host: str) -> None:
    with pytest.raises(SourceFailure) as caught:
        parsers.parse_vulkaninfo(section(host, "vulkan"))
    assert caught.value.state == "absent"


def test_alsa_streams(host: str) -> None:
    cards = parsers.parse_alsa(section(host, "asound-streams") + "\n" + section(host, "audio-detail"))
    names = [c.name for c in cards]
    assert any("Cam Link" in n for n in names)
    camlink = next(c for c in cards if "Cam Link" in c.name)
    assert camlink.rates("capture") == (48000,) and camlink.formats("capture") == ("S16_LE",)
    wave = [c for c in cards if "Wave XLR" in c.name]
    assert bool(wave) == (host == "dm313red")
    if wave:
        card = wave[0]
        assert card.rates("playback") == (48000, 96000) == card.rates("capture")
        assert card.formats("playback") == card.formats("capture") == ("S24_3LE",)
        assert card.channels("playback") == (2,) and card.channels("capture") == (1,)
        assert card.id == "XLR"


def test_pipewire_cards_and_nodes(host: str) -> None:
    cards = parsers.parse_pipewire_cards(section(host, "pactl-cards"))
    assert any("Cam_Link_4K" in name for _i, name, _d in cards)
    assert any("Wave_XLR" in name for _i, name, _d in cards) == (host == "dm313red")
    nodes = parsers.parse_pipewire_nodes("# sources\n" + section(host, "audio-detail"))
    names = {n[1] for n in nodes}
    if host == "dm313red":
        assert "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_<serial>-00.mono-fallback" in names
    else:
        assert "auto_null" in names


def test_wpctl_status_defaults(host: str) -> None:
    status = parsers.parse_wpctl_status(section(host, "wpctl"))
    assert status
    if host == "deskmeet1":
        assert status["sink"][1] == "Dummy Output"
    else:
        assert "Wave XLR" in status["sink"][1]


def test_pipewire_state(host: str) -> None:
    state = snap(host).pipewire
    assert bool(state.default_source) == (host == "dm313red")
    wave = [c for c in state.cards if "Wave_XLR" in c.name]
    assert bool(wave) == (host == "dm313red")
    if wave:
        assert wave[0].has_nodes and not wave[0].off
    else:
        assert all(c.off for c in state.cards if "Cam_Link" in c.name)  # deskmeet1: every card at profile off
        # the recording's wpctl ids are from another moment than its pactl ids, so only the label resolves
        assert state.default_sink is None and state.default_sink_label == "Dummy Output"
        assert [n.name for n in state.nodes if n.kind == "sink"] == ["auto_null"]


def test_v4l2_devices_and_info(host: str) -> None:
    listing = section(host, "v4l2")
    head = listing.split("== ", 1)[0]
    info = parsers.parse_v4l2_info(listing)
    assert set(info) >= {"/dev/video0", "/dev/video42", "/dev/video43"}
    if host == "deskmeet1":
        devices = parsers.parse_v4l2_devices(head)
        assert {label for label, _bus, _n in devices} == {
            "Canon EOS 50D",
            "StreamStack Camera",
            "Cam Link 4K: Cam Link 4K",
        }
        assert info["/dev/video0"]["caps"] == 0x04200001 and info["/dev/video1"]["caps"] == 0x04A00000
    else:
        assert info["/dev/video0"]["caps"] is None  # the recording could not open the nodes


def test_v4l2_formats(host: str) -> None:
    text = section(host, "camlink-formats")
    if host == "dm313red":
        with pytest.raises(SourceFailure) as caught:
            parsers.parse_v4l2_formats(text)
        assert caught.value.state == "denied"
        return
    formats = parsers.parse_v4l2_formats(text)
    assert [(f.fourcc, f.width, f.height, f.fps) for f in formats] == [("YUYV", 1920, 1080, 29.97)]


def test_v4l2_node_building_and_source_state(host: str) -> None:
    s = snap(host)
    video = s.status("v4l2")
    if host == "dm313red":
        assert video.state == "denied" and s.v4l2 == ()
    else:
        assert video.ok
        camlink = next(n for n in s.v4l2 if n.path == "/dev/video0")
        assert camlink.kind == "capture" and camlink.formats and not camlink.loopback
        assert next(n for n in s.v4l2 if n.path == "/dev/video1").kind == "metadata"


def test_loopback(host: str) -> None:
    state = snap(host).loopback
    assert [(d.path, d.label) for d in state.devices] == [
        ("/dev/video42", "Canon EOS 50D"),
        ("/dev/video43", "StreamStack Camera"),
    ]
    assert state.option("video_nr") == "42,43" and state.option("exclusive_caps") == "1,1"


def test_ffmpeg_encoders(host: str) -> None:
    names = parsers.parse_ffmpeg_encoders(section(host, "ffmpeg-encoders")).encoders
    assert {"av1_vaapi", "libsvtav1", "libx264", "h264_vaapi"} <= set(names)


def test_machine_id_hash_is_salted_and_stable(host: str) -> None:
    raw = "0123456789abcdef0123456789abcdef"
    assert parsers.parse_machine_id(raw + "\n") == parsers.machine_hash(raw)
    assert parsers.parse_machine_id("01234567-89ab-cdef-0123-456789abcdef") == parsers.machine_hash(raw)
    assert raw not in parsers.machine_hash(raw)
    assert snap(host).machine is None  # recordings never carry it


def test_card_profiles_json(host: str) -> None:
    text = json.dumps(
        [
            {
                "name": "alsa_card.usb-X-00",
                "active_profile": "off",
                "properties": {"device.vendor.id": "0x0FD9", "device.product.id": "007d"},
            },
            {"name": "alsa_card.pci-0000_00_1f.3", "active_profile": "output:analog-stereo", "properties": {}},
            "not a card",
        ]
    )
    assert parsers.parse_card_profiles(text) == {
        "alsa_card.usb-X-00": ("off", "0fd9:007d"),
        "alsa_card.pci-0000_00_1f.3": ("output:analog-stereo", None),
    }


def test_the_whole_recording_resolves_without_a_failed_source_other_than_the_known(host: str) -> None:
    bad = {s.source: s.state for s in snap(host).sources if not s.ok}
    assert bad == (
        {"vulkan": "absent", "v4l2": "denied", "machine": "absent"}
        if host == "dm313red"
        else {"vulkan": "absent", "machine": "absent"}
    )


# -- failure injection: every parser, on text that is not its format ---------------------------------------------------

STRICT: dict[str, Callable[[str], Any]] = {
    "lsusb": parsers.parse_lsusb,
    "lspci": parsers.parse_lspci,
    "drm": parsers.parse_drm,
    "vainfo": parsers.parse_vainfo,
    "alsa": parsers.parse_alsa,
    "pipewire-cards": parsers.parse_pipewire_cards,
    "v4l2-devices": parsers.parse_v4l2_devices,
    "v4l2-formats": parsers.parse_v4l2_formats,
    "ffmpeg": parsers.parse_ffmpeg_encoders,
    "machine": parsers.parse_machine_id,
    "pipewire": lambda text: parsers.parse_pipewire(text, ""),
    "wpctl": parsers.parse_wpctl_status,
    "vulkan": parsers.parse_vulkaninfo,
    "loopback": parsers.parse_loopback,
}


@pytest.mark.parametrize("name", sorted(STRICT))
def test_a_missing_tool_is_absent(host: str, name: str) -> None:
    with pytest.raises(SourceFailure) as caught:
        STRICT[name]("bash: line 1: tool: command not found\n")
    assert caught.value.state == "absent"


@pytest.mark.parametrize("name", sorted(STRICT))
def test_permission_denied_is_denied(host: str, name: str) -> None:
    with pytest.raises(SourceFailure) as caught:
        STRICT[name]("Failed to open /dev/video0: Permission denied\n")
    assert caught.value.state == "denied"


@pytest.mark.parametrize("name", sorted(set(STRICT) - {"vulkan", "wpctl", "loopback"}))
def test_unrecognised_text_is_a_parse_error(host: str, name: str) -> None:
    with pytest.raises(ParseError):
        STRICT[name]("banana split\nnothing here looks like it\n")


def test_card_profiles_reject_non_json(host: str) -> None:
    with pytest.raises(ParseError):
        parsers.parse_card_profiles("not json")
    with pytest.raises(ParseError):
        parsers.parse_card_profiles('{"a": 1}')


def test_a_truncated_recording_degrades_one_source(host: str) -> None:
    """Cut a section off at the first line: that source fails or is empty, the rest of the snapshot is untouched."""
    text = inventory_text(host)
    parts = inventory.sections(text)
    broken = "".join(f"##### {k}\n{'garbage' if k == 'lsusb' else v}\n\n" for k, v in parts.items())
    result = inventory.snapshot_from_inventory(broken)
    assert result.status("usb").state == "error"
    assert result.render_nodes == snap(host).render_nodes
    assert result.alsa == snap(host).alsa
