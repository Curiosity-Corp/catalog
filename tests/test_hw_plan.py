"""`resolve(snapshot, profiles, overrides)`: the Plan for each recorded host and for the situations that differ."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from hardware_support import ARC_VA, HOSTS, IGPU_VA, FakeMachine, inventory_text, studio_wave

from streamstack.hardware import inventory, schema
from streamstack.hardware.model import Snapshot
from streamstack.hardware.parsers import machine_hash
from streamstack.hardware.plan import Plan, resolve

CATALOGUE = schema.packaged()
UHD = "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770]"
ARC = "Intel Corporation DG2 [Arc A380]"


def recorded(host: str, overrides: dict[str, str] | None = None) -> Plan:
    return resolve(inventory.snapshot_from_inventory(inventory_text(host)), CATALOGUE, overrides)


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


def roles(plan: Plan) -> dict[str, tuple[str, str]]:
    return {r.role: (r.pci_id, r.node_path) for r in plan.gpu_roles}


# -- the recorded hosts ----------------------------------------------------------------------------


def test_dm313red_is_the_wave_xlr_studio() -> None:
    plan = recorded("dm313red")
    assert (plan.rig.id, plan.rig.via) == ("studio-wave-xlr", "overlap")
    assert {r.role for r in plan.gpu_roles} == {"grade", "encode", "decode", "blur"}
    assert set(roles(plan).values()) == {("8086:56a5", "/dev/dri/renderD128")}
    assert (plan.encoder.ffmpeg, plan.encoder.hardware, plan.encoder.verified) == ("av1_vaapi", True, True)
    assert plan.has("wave-xlr") and not plan.has("stream-deck-v2") and not plan.has("uhd-770")
    assert plan.device("cam-link-4k").state == "present"


def test_deskmeet1_is_the_deck_studio_with_split_gpu_roles() -> None:
    plan = recorded("deskmeet1")
    assert (plan.rig.id, plan.rig.via) == ("studio-deck", "overlap")
    assert roles(plan) == {
        "grade": ("8086:56a5", "/dev/dri/renderD129"),
        "encode": ("8086:56a5", "/dev/dri/renderD129"),
        "blur": ("8086:56a5", "/dev/dri/renderD129"),
        "decode": ("8086:a780", "/dev/dri/renderD128"),
    }
    assert plan.gpu("encode").node_path == "/dev/dri/renderD129"
    assert plan.gpu("decode").profile_id == "uhd-770"
    assert plan.encoder.node_path == "/dev/dri/renderD129" and plan.encoder.ffmpeg == "av1_vaapi"
    assert plan.deck and plan.lights
    assert plan.has("wave-xlr") is False


def test_the_deck_studio_expects_a_scarlett_2i2_and_this_machine_is_listed() -> None:
    # Owner decision 2026-10-24: deskmeet1's DAC is a Scarlett 2i2. Until it is plugged in it is expected, not present.
    plan = recorded("deskmeet1")
    assert plan.device("scarlett-2i2-gen4").state == "absent" and plan.device("scarlett-2i2-gen3").state == "absent"
    assert plan.audio_interface() is None
    deck = next(r for r in CATALOGUE.rigs if r.id == "studio-deck")
    assert "f1dffc80615d72b2f381b9985a4edb3a" in deck.machine_ids


def test_a_2i2_plugged_into_the_deck_studio_becomes_its_interface(machine: FakeMachine) -> None:
    machine.add_usb("0fd9:006d", "Stream Deck")
    machine.add_usb("0fd9:0067", "Cam Link 4K")
    machine.add_gpu(128, "0000:00:02.0", "8086:a780", UHD, IGPU_VA)
    machine.add_gpu(129, "0000:03:00.0", "8086:56a5", ARC, ARC_VA)
    machine.add_usb("1235:8219", "Scarlett 2i2 4th Gen")
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.rig.id == "studio-deck"
    assert plan.has("scarlett-2i2-gen4") and plan.audio_interface().profile_id == "scarlett-2i2-gen4"


def test_wave_xlr_resolves_48000_s24le_and_dummy_output_resolves_to_no_interface() -> None:
    wave = recorded("dm313red").audio
    assert wave.interface.profile_id == "wave-xlr" and wave.interface.usb_id == "0fd9:007d"
    assert (wave.rate, wave.format, wave.bits, wave.resamples) == (48000, "s24le", 24, False)
    assert wave.capture_channels == 1
    assert wave.microphone == "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_<serial>-00.mono-fallback"
    assert wave.monitor.endswith("analog-stereo") and wave.source == "interface"
    assert recorded("dm313red").audio_interface().alsa_id == "XLR"

    dummy = recorded("deskmeet1")
    assert dummy.audio_interface() is None
    assert dummy.audio.microphone is None and dummy.audio.monitor == "auto_null" and dummy.audio.source == "none"
    ids = {n.id for n in dummy.notices}
    assert {"audio-fallback", "audio-dummy-sink"} <= ids


def test_the_capture_card_modes_follow_what_the_probe_could_read() -> None:
    deck = recorded("deskmeet1").capture_card()
    assert deck.node_path == "/dev/video0" and deck.modes_state == "ok"
    assert (deck.best_mode.width, deck.best_mode.height, deck.best_mode.fourcc) == (1920, 1080, "YUYV")
    red = recorded("dm313red").capture_card()
    assert red.modes == () and red.best_mode is None and red.modes_state == "denied"
    assert red.state == "present"


def test_loopback_nodes_are_found_by_label_not_number() -> None:
    plan = recorded("dm313red")
    assert (plan.loopback.camera_path, plan.loopback.program_path) == ("/dev/video42", "/dev/video43")
    custom = recorded("dm313red", {"STREAMSTACK_CAMERA_LABEL": "Nothing Here"})
    assert custom.loopback.camera_path is None and custom.loopback.program_path == "/dev/video43"


@pytest.mark.parametrize("host", HOSTS)
def test_a_plan_is_json_serialisable_and_pure(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first = recorded(host)
    monkeypatch.setenv("STREAMSTACK_RIG", "generic-linux")  # resolve reads overrides, never the environment
    monkeypatch.setenv("STREAMSTACK_MICROPHONE", "nothing")
    assert recorded(host) == first
    assert json.dumps(first.to_dict(), default=str)


# -- Arc on either render node ---------------------------------------------------------------------


def normalised(plan: Plan) -> dict:
    text = json.dumps(plan.to_dict(), default=str, sort_keys=True)
    return json.loads(re.sub(r"/dev/dri/renderD\d+", "NODE", text))


def test_the_arc_on_render_d128_or_d129_gives_the_same_plan(tmp_path: Path) -> None:
    plans = []
    for number in (128, 129):
        machine = FakeMachine(tmp_path / str(number))
        studio_wave(machine, gpu_node=number)
        plans.append(resolve(machine.snapshot(), CATALOGUE))
    assert plans[0].gpu("encode").node_path == "/dev/dri/renderD128"
    assert plans[1].gpu("encode").node_path == "/dev/dri/renderD129"
    assert normalised(plans[0]) == normalised(plans[1])
    assert plans[0].diff(plans[1]) == ("gpus", "gpu_roles", "encoder")  # only the paths differ


def test_with_an_igpu_in_front_the_arc_still_encodes(machine: FakeMachine) -> None:
    studio_wave(machine, gpu_node=129)
    machine.add_gpu(128, "0000:00:02.0", "8086:a780", UHD, IGPU_VA)
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.gpu("encode").node_path == "/dev/dri/renderD129"
    assert plan.gpu("decode").node_path in {"/dev/dri/renderD129", "/dev/dri/renderD128"}
    assert {g.pci_id for g in plan.gpus} == {"8086:56a5", "8086:a780"}


# -- GPUs missing, denied or unknown ---------------------------------------------------------------


def test_no_gpu_means_a_cpu_encoder_and_no_roles(machine: FakeMachine) -> None:
    machine.add_usb("0fd9:0067", "Cam Link 4K")
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.gpu_roles == () and plan.gpu("encode") is None and plan.gpus == ()
    assert (plan.encoder.ffmpeg, plan.encoder.hardware, plan.encoder.node_path) == ("libsvtav1", False, None)
    assert "no GPU" in plan.encoder.reason


def test_no_gpu_and_no_svt_av1_falls_to_x264(machine: FakeMachine) -> None:
    machine.tools["ffmpeg"] = " V....D libx264              libx264 (codec h264)\n"
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert (plan.encoder.ffmpeg, plan.encoder.hardware) == ("libx264", False)


def test_a_denied_render_node_takes_no_role(machine: FakeMachine) -> None:
    studio_wave(machine)
    machine.denied.add("renderD128")
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.gpu_roles == ()
    assert plan.encoder.hardware is False
    assert plan.device("arc-a380").state == "denied"
    assert any(n.id == "render-node-denied" for n in plan.notices)


def test_an_arc_whose_vainfo_lacks_av1_encodes_hevc(machine: FakeMachine) -> None:
    no_av1 = tuple(e for e in ARC_VA if "AV1" not in e[0])
    machine.add_gpu(128, "0000:03:00.0", "8086:56a5", ARC, no_av1)
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert (plan.encoder.codec, plan.encoder.ffmpeg, plan.encoder.verified) == ("hevc", "hevc_vaapi", True)


def test_a_gpu_that_was_not_probed_is_trusted_from_its_profile(machine: FakeMachine) -> None:
    machine.add_gpu(128, "0000:03:00.0", "8086:56a5", ARC)  # no vainfo output for it
    machine.tools.pop("vainfo")
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.encoder.ffmpeg == "av1_vaapi" and plan.encoder.verified is False
    assert plan.gpus[0].probed is False


def test_an_unlisted_gpu_is_used_generically(machine: FakeMachine) -> None:
    machine.add_gpu(128, "0000:01:00.0", "1002:73ff", "AMD Radeon", (("VAProfileH264High", "VAEntrypointEncSlice"),))
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.rig.id == "generic-linux" and plan.rig.via == "fallback"
    role = plan.gpu("encode")
    assert (role.pci_id, role.via, role.profile_id) == ("1002:73ff", "generic", None)
    assert (plan.encoder.codec, plan.encoder.ffmpeg) == ("h264", "h264_vaapi")


# -- generic fallback ------------------------------------------------------------------------------


def test_an_empty_machine_resolves_to_the_generic_rig(machine: FakeMachine) -> None:
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert (plan.rig.id, plan.rig.via, plan.rig.score) == ("generic-linux", "fallback", 0)
    assert plan.audio.rate == 48000 and plan.audio.interface is None and plan.capture is None
    assert plan.devices == ()


def test_the_generic_rig_uses_the_default_pipewire_source(machine: FakeMachine) -> None:
    machine.set_pipewire(
        [(1, "alsa_card.pci-0000_00_1f.3")],
        sinks=[(2, "alsa_output.pci-0000_00_1f.3.analog-stereo", "s16le 2ch 48000Hz")],
        sources=[(3, "alsa_input.pci-0000_00_1f.3.analog-stereo", "s16le 2ch 48000Hz")],
    )
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.audio.microphone == "alsa_input.pci-0000_00_1f.3.analog-stereo" and plan.audio.source == "default"
    assert plan.audio.monitor == "alsa_output.pci-0000_00_1f.3.analog-stereo"


def test_capture_card_audio_is_never_the_call_microphone(machine: FakeMachine) -> None:
    machine.add_usb("0fd9:0067", "Cam Link 4K")
    card = "alsa_card.usb-Elgato_Cam_Link_4K_ABCDEF123-03"
    machine.set_pipewire(
        [(49, card)],
        sources=[(57, "alsa_input.usb-Elgato_Cam_Link_4K_ABCDEF123-03.analog-stereo", "s16le 2ch 48000Hz")],
    )
    machine.set_card_profiles({card: ("input:analog-stereo", "0fd9:0067")})
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.audio.microphone is None


def test_the_streamstack_mic_is_never_chosen_as_the_microphone(machine: FakeMachine) -> None:
    machine.set_pipewire([], sources=[(313, "streamstack_mic", "float32le 1ch 48000Hz")])
    assert resolve(machine.snapshot(), CATALOGUE).audio.microphone is None


# -- audio interface rates -------------------------------------------------------------------------


def test_an_interface_that_only_runs_96000_resamples_for_calls(machine: FakeMachine) -> None:
    studio_wave(machine)
    machine.remove_alsa(3)
    machine.add_alsa_usb(3, "XLR", "0fd9:007d", "Elgato Systems Elgato Wave XLR", "96000")
    audio = resolve(machine.snapshot(), CATALOGUE).audio
    assert (audio.rate, audio.conferencing_rate, audio.resamples) == (96000, 48000, True)


def test_the_graph_rate_is_the_nearest_not_below_when_48000_is_missing(machine: FakeMachine) -> None:
    studio_wave(machine)
    machine.remove_alsa(3)
    machine.add_alsa_usb(3, "XLR", "0fd9:007d", "Elgato Systems Elgato Wave XLR", "44100, 88200")
    assert resolve(machine.snapshot(), CATALOGUE).audio.rate == 88200


def test_s24_le_is_named_for_pipewire(machine: FakeMachine) -> None:
    studio_wave(machine)
    machine.remove_alsa(3)
    machine.add_alsa_usb(3, "XLR", "0fd9:007d", "Elgato Systems Elgato Wave XLR", "48000", fmt="S16_LE")
    audio = resolve(machine.snapshot(), CATALOGUE).audio
    assert (audio.format, audio.bits) == ("s16le", 16)


def test_a_scarlett_is_found_without_a_rig_naming_the_wave(machine: FakeMachine) -> None:
    machine.add_usb("1235:8219", "Scarlett 2i2 4th Gen")
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.audio_interface().profile_id == "scarlett-2i2-gen4"
    assert plan.audio_interface().via == "profile"


def test_a_card_at_profile_off_has_no_microphone_and_says_so(machine: FakeMachine) -> None:
    studio_wave(machine)
    card = "alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00"
    machine.set_pipewire([(49, card)])
    machine.set_card_profiles({card: ("off", "0fd9:007d")})
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.audio_interface().card_off and plan.audio.microphone is None
    assert any(n.id == "interface-card-off" for n in plan.notices)


# -- rig selection ---------------------------------------------------------------------------------


def test_str_rig_override_wins_over_overlap() -> None:
    plan = recorded("dm313red", {"STREAMSTACK_RIG": "studio-deck"})
    assert (plan.rig.id, plan.rig.via) == ("studio-deck", "env")


def test_the_rig_override_may_name_the_fallback() -> None:
    plan = recorded("deskmeet1", {"STREAMSTACK_RIG": "generic-linux"})
    assert plan.rig.id == "generic-linux" and plan.rig.via == "env"
    assert plan.gpu("encode") is not None  # generic roles still work


def test_an_unknown_rig_override_warns_and_falls_back_to_overlap() -> None:
    plan = recorded("dm313red", {"STREAMSTACK_RIG": "no-such-rig"})
    assert plan.rig.id == "studio-wave-xlr" and plan.rig.via == "overlap"
    assert any(n.id == "rig-unknown" for n in plan.notices)


def test_auto_is_not_a_rig_name() -> None:
    plan = recorded("dm313red", {"STREAMSTACK_RIG": "auto"})
    assert plan.rig.via == "overlap"
    assert not any(n.id == "rig-unknown" for n in plan.notices)


def tied_snapshot(machine: FakeMachine) -> Snapshot:
    """The Arc and the Cam Link, but neither Wave XLR nor Stream Deck: both studio rigs score the same."""
    machine.add_usb("0fd9:0067", "Cam Link 4K")
    machine.add_gpu(128, "0000:03:00.0", "8086:56a5", ARC, ARC_VA)
    return machine.snapshot()


def with_ids(catalogue: schema.Catalogue, ids: dict[str, tuple[str, ...]]) -> schema.Catalogue:
    rigs = tuple(dataclasses.replace(r, machine_ids=ids.get(r.id, r.machine_ids)) for r in catalogue.rigs)
    return dataclasses.replace(catalogue, rigs=rigs)


def test_a_tie_goes_to_file_order_without_an_identity(machine: FakeMachine) -> None:
    snap = tied_snapshot(machine)
    plan = resolve(snap, with_ids(CATALOGUE, {"studio-wave-xlr": (), "studio-deck": ()}))
    scores = [s for _id, s in plan.rig.candidates]
    assert scores[0] == scores[1]
    assert (plan.rig.id, plan.rig.via) == ("studio-deck", "overlap")


def test_a_tie_is_decided_by_the_machine_identity(machine: FakeMachine) -> None:
    snap = tied_snapshot(machine)
    mine = machine_hash("0123456789abcdef0123456789abcdef")
    plan = resolve(snap, with_ids(CATALOGUE, {"studio-wave-xlr": (mine,), "studio-deck": ()}))
    assert (plan.rig.id, plan.rig.via) == ("studio-wave-xlr", "identity")
    other = resolve(snap, with_ids(CATALOGUE, {"studio-wave-xlr": ("f" * 32,), "studio-deck": ()}))
    assert (other.rig.id, other.rig.via) == ("studio-deck", "overlap")


def test_identity_never_beats_overlap(machine: FakeMachine) -> None:
    studio_wave(machine)
    mine = machine_hash("0123456789abcdef0123456789abcdef")
    plan = resolve(machine.snapshot(), with_ids(CATALOGUE, {"studio-deck": (mine,)}))
    assert plan.rig.id == "studio-wave-xlr" and plan.rig.via == "overlap"


def test_a_single_present_device_is_not_a_rig(machine: FakeMachine) -> None:
    machine.add_usb("046d:c901", "Litra Beam")
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.rig.id == "generic-linux"
    assert plan.lights and plan.has("litra-beam")


# -- overrides -------------------------------------------------------------------------------------


def test_the_engine_render_node_setting_pins_grade_and_blur(machine: FakeMachine) -> None:
    studio_wave(machine, gpu_node=129)
    machine.add_gpu(128, "0000:00:02.0", "8086:a780", UHD, IGPU_VA)
    plan = resolve(machine.snapshot(), CATALOGUE, {"STREAMSTACK_ENGINE_RENDER_NODE": "/dev/dri/renderD128"})
    assert plan.gpu("grade").node_path == plan.gpu("blur").node_path == "/dev/dri/renderD128"
    assert plan.gpu("grade").via == "override" and plan.gpu("encode").node_path == "/dev/dri/renderD129"


def test_the_canon_render_node_setting_pins_decode(machine: FakeMachine) -> None:
    studio_wave(machine, gpu_node=129)
    machine.add_gpu(128, "0000:00:02.0", "8086:a780", UHD, IGPU_VA)
    plan = resolve(machine.snapshot(), CATALOGUE, {"STREAMSTACK_CANON_RENDER_NODE": "/dev/dri/renderD129"})
    assert plan.gpu("decode").node_path == "/dev/dri/renderD129"


def test_a_render_node_setting_for_a_node_that_does_not_exist_warns(machine: FakeMachine) -> None:
    studio_wave(machine)
    plan = resolve(machine.snapshot(), CATALOGUE, {"STREAMSTACK_ENGINE_RENDER_NODE": "/dev/dri/renderD140"})
    assert plan.gpu("grade").via == "rig"
    assert any(n.id == "render-node-unknown" for n in plan.notices)


def test_the_microphone_setting_matches_names_with_spaces_or_underscores() -> None:
    plan = recorded("dm313red", {"STREAMSTACK_MICROPHONE": "wave xlr"})
    assert plan.audio.source == "override" and "Wave_XLR" in plan.audio.microphone
    missing = recorded("dm313red", {"STREAMSTACK_MICROPHONE": "nonesuch"})
    assert missing.audio.source == "interface" and any(n.id == "microphone-override" for n in missing.notices)


def test_the_interface_setting_off_drops_the_interface() -> None:
    plan = recorded("dm313red", {"STREAMSTACK_INTERFACE": "off"})
    assert plan.audio_interface() is None


def test_the_interface_setting_by_usb_id_or_profile_id() -> None:
    for wanted in ("wave-xlr", "0fd9:007d"):
        plan = recorded("dm313red", {"STREAMSTACK_INTERFACE": wanted})
        assert plan.audio_interface().via == "override"
    plan = recorded("dm313red", {"STREAMSTACK_INTERFACE": "scarlett-2i2-gen4"})
    assert plan.audio_interface().profile_id == "wave-xlr"
    assert any(n.id == "interface-override" for n in plan.notices)


# -- diff and accessors ----------------------------------------------------------------------------


def test_diff_names_the_sections_that_changed(machine: FakeMachine) -> None:
    studio_wave(machine)
    before = resolve(machine.snapshot(), CATALOGUE)
    machine.remove_usb("0fd9:007d")
    machine.remove_alsa(3)
    machine.set_pipewire([], [], [])
    after = resolve(machine.snapshot(), CATALOGUE)
    assert before.diff(before) == ()
    assert set(before.diff(after)) >= {"devices", "audio"}
    assert "gpu_roles" not in before.diff(after)


def test_accessors_return_none_when_nothing_fits(machine: FakeMachine) -> None:
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert plan.gpu("encode") is None and plan.audio_interface() is None and plan.capture_card() is None
    assert plan.device("wave-xlr") is None and plan.has("wave-xlr") is False
