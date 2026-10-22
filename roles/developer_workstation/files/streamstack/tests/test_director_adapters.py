"""The adapters that bind the hardware actuators to real modules, run here against simulated devices only."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack import lighting
from streamstack.canon import ipc
from streamstack.director import adapters, devices
from streamstack.director.actuators import ActuatorError
from streamstack.hardware import profiles, sim
from streamstack.hardware.scarlett import Scarlett
from streamstack.hardware.wavexlr import WaveXlr

real_hardware_actuators = adapters.hardware_actuators  # the conftest fixture replaces the module attribute


# -- Canon bridge ------------------------------


def test_the_canon_bridge_adapter_reads_status_and_reports_a_missing_bridge_as_none() -> None:
    asked: list[dict] = []

    def request(message: dict, timeout: float = 1.0) -> dict:
        asked.append(message)
        return {"ok": True, "status": {"state": "running", "controls": {}}}

    assert adapters.BridgeCanon(request).status() == {"state": "running", "controls": {}}
    assert asked == [{"op": "status"}]

    def gone(message: dict, timeout: float = 1.0) -> dict:
        raise ipc.BridgeUnavailable("no socket")

    assert adapters.BridgeCanon(gone).status() is None
    assert adapters.BridgeCanon(lambda m, timeout=1.0: {"ok": False}).status() is None


@pytest.mark.parametrize("control", ["focusmode", "iso", "shutter", "aperture", "picturestyle", "liveviewsize", "x"])
def test_the_canon_bridge_adapter_refuses_everything_but_exposure_and_white_balance(control) -> None:
    sent: list[dict] = []
    canon = adapters.BridgeCanon(lambda m, timeout=1.0: sent.append(m) or {"ok": True})
    with pytest.raises(ActuatorError, match="may not change"):
        canon.set(control, "1")
    assert sent == []


def test_the_canon_bridge_adapter_turns_refusals_into_actuator_errors() -> None:
    answers = iter(
        [
            {"ok": True, "applied": True},
            {"ok": False, "error": "camera busy"},
            {"ok": True, "applied": False, "message": "kept 5200"},
        ]
    )
    canon = adapters.BridgeCanon(lambda m, timeout=1.0: next(answers))
    canon.set("kelvin", 5000)
    with pytest.raises(ActuatorError, match="busy"):
        canon.set("kelvin", 5000)
    with pytest.raises(ActuatorError, match="kept 5200"):
        canon.set("exposure_comp", "0")

    def gone(m: dict, timeout: float = 1.0) -> dict:
        raise ipc.BridgeUnavailable("no socket")

    with pytest.raises(ActuatorError, match="not running"):
        adapters.BridgeCanon(gone).set("kelvin", 5000)


# -- Litra ------------------------------


def found(name: str, model: lighting.Model) -> lighting.Found:
    return lighting.Found(id=name, node=Path("/dev/null"), model=model, phys="usb", name=model.name)


def hid(*models: lighting.Model, on: bool = True) -> tuple[adapters.HidLights, dict[str, lighting.SimulatedTransport]]:
    lights = [found(f"hidraw{i}", m) for i, m in enumerate(models)]
    transports = {f.id: lighting.SimulatedTransport(f.model, on=on, lumen=100, kelvin=4500) for f in lights}
    return adapters.HidLights(lambda: lights, lambda f: transports[f.id], lambda: 0.0), transports


def test_lights_are_found_by_scanning_and_numbered_like_the_lighting_page() -> None:
    beam = next(m for m in lighting.MODELS.values() if m.name == "Litra Beam")
    glow = next(m for m in lighting.MODELS.values() if m.name == "Litra Glow")
    backend, _ = hid(beam, beam, glow)
    assert [(i.label, i.lumen_min, i.lumen_max) for i in backend.lights()] == [
        ("Litra Beam 1", beam.lumen_min, beam.lumen_max),
        ("Litra Beam 2", beam.lumen_min, beam.lumen_max),
        ("Litra Glow", glow.lumen_min, glow.lumen_max),
    ]
    assert adapters.HidLights(lambda: [], lambda f: None).lights() == []


def test_a_light_is_read_and_set_through_the_hid_protocol_and_closed_each_time() -> None:
    beam = next(m for m in lighting.MODELS.values() if m.name == "Litra Beam")
    backend, transports = hid(beam)
    state = backend.read("Litra Beam")
    assert state.on is True and state.kelvin == 4500 and state.lumen == pytest.approx(100, abs=2)
    assert transports["hidraw0"].closed
    transports["hidraw0"].closed = False
    backend.write("Litra Beam", 200, 5000)
    sim_light = transports["hidraw0"]
    assert sim_light.kelvin == 5000 and sim_light.lumen == pytest.approx(200, abs=2) and sim_light.closed
    with pytest.raises(ActuatorError, match="not connected"):
        backend.read("Litra Glow")


def test_the_director_never_sends_a_power_command() -> None:
    beam = next(m for m in lighting.MODELS.values() if m.name == "Litra Beam")
    backend, transports = hid(beam, on=False)
    backend.write("Litra Beam", 150, 4000)
    assert transports["hidraw0"].on is False  # the light stayed off
    assert not [w for w in transports["hidraw0"].writes if w[3] == lighting.CMD_SET_POWER]


def test_a_light_that_cannot_be_opened_is_an_actuator_error() -> None:
    beam = next(m for m in lighting.MODELS.values() if m.name == "Litra Beam")

    def refuse(f: lighting.Found) -> lighting.Transport:
        raise lighting.LightingError("permission denied on /dev/hidraw0", "permission")

    backend = adapters.HidLights(lambda: [found("hidraw0", beam)], refuse, lambda: 0.0)
    with pytest.raises(ActuatorError, match="permission denied"):
        backend.read("Litra Beam")


# -- Audio interface ------------------------------


def scarlett(generation: str, tmp_path: Path) -> adapters.HardwareInterface:
    mixer = sim.SimMixer(generation)
    proc = sim.sim_proc(tmp_path, card=7)
    return adapters.HardwareInterface(
        {"STREAMSTACK_INTERFACE": "auto"},
        scan=lambda: [sim.SCARLETT_DEVICES[generation]],
        factory=lambda profile, device: Scarlett(profile, device, mixer, proc_root=proc),
    )


def test_a_scarlett_gen4_offers_its_input_gain_and_nothing_else(tmp_path: Path) -> None:
    backend = scarlett("gen4", tmp_path)
    report = backend.inspect()
    assert report.present and report.controls and all(adapters.GAIN_ID.fullmatch(c.id) for c in report.controls)
    first = report.controls[0]
    backend.set_gain(first.id, first.value + 2)
    assert backend.inspect().controls[0].value == first.value + 2


@pytest.mark.parametrize("control", ["phantom.1", "air.1", "autogain.1", "pad.1", "safe.1", "link", "mute"])
def test_the_48v_and_the_other_switches_are_unreachable(control: str, tmp_path: Path) -> None:
    backend = scarlett("gen4", tmp_path)
    with pytest.raises(ActuatorError, match="only change a gain"):
        backend.set_gain(control, 1)


def test_a_scarlett_gen3_has_no_software_gain_and_says_so(tmp_path: Path) -> None:
    report = scarlett("gen3", tmp_path).inspect()
    assert report.present and not report.controls and "analogue" in report.note


def test_no_interface_on_the_bus_is_simply_absent() -> None:
    report = adapters.HardwareInterface({"STREAMSTACK_INTERFACE": "auto"}, scan=lambda: []).inspect()
    assert not report.present
    off = adapters.HardwareInterface({"STREAMSTACK_INTERFACE": "off"}, scan=lambda: [sim.SCARLETT_DEVICES["gen4"]])
    assert not off.inspect().present and "off" in off.inspect().note


def wave(mode: str, tmp_path: Path, device: sim.SimXlr | None = None) -> adapters.HardwareInterface:
    xlr = device or sim.SimXlr()
    proc = sim.sim_proc(tmp_path, card=7)
    return adapters.HardwareInterface(
        {"STREAMSTACK_INTERFACE": "auto"},
        scan=lambda: [sim.XLR_DEVICE],
        factory=lambda profile, device: WaveXlr(profile, device, mode, xlr.opener, proc),
    )


def test_the_wave_xlr_is_never_opened_while_its_hardware_control_is_off(tmp_path: Path) -> None:
    def forbidden(device):
        raise AssertionError("the Wave XLR must not be opened while hardware control is off")

    proc = sim.sim_proc(tmp_path, card=7)
    backend = adapters.HardwareInterface(
        {"STREAMSTACK_INTERFACE": "auto"},
        scan=lambda: [sim.XLR_DEVICE],
        factory=lambda profile, device: WaveXlr(profile, device, "off", forbidden, proc),
    )
    report = backend.inspect()
    assert report.present and not report.controls and "STREAMSTACK_WAVE_XLR_USB=off" in report.note


def test_the_wave_xlr_gain_is_offered_only_when_hardware_control_is_on(tmp_path: Path) -> None:
    xlr = sim.SimXlr()
    backend = wave("write", tmp_path, xlr)
    report = backend.inspect()
    assert [c.id for c in report.controls] == ["gain"] and report.controls[0].step == 0.5
    backend.set_gain("gain", report.controls[0].value + 1)
    assert xlr.writes  # the config block was written
    with pytest.raises(ActuatorError, match="only change a gain"):
        backend.set_gain("phantom", 1)
    read_only = wave("read", tmp_path).inspect()
    assert read_only.controls == () or all(c.locked for c in read_only.controls)


def test_a_device_that_fails_is_an_actuator_error(tmp_path: Path) -> None:
    xlr = sim.SimXlr()
    backend = wave("write", tmp_path, xlr)
    backend.inspect()
    xlr.fail = adapters.XlrError("the Wave XLR stopped answering")
    with pytest.raises(ActuatorError, match="stopped answering"):
        backend.set_gain("gain", 10)


# -- Registration ------------------------------


def test_the_toggles_decide_which_hardware_actuators_exist() -> None:
    names = lambda cfg: [a.id for a in real_hardware_actuators(cfg, lambda *a: None, lambda t: None)]  # noqa: E731
    assert names({}) == ["canon-exposure", "litra", "interface-gain"]
    assert names({"STREAMSTACK_AUTO_CANON": "false", "STREAMSTACK_AUTO_LIGHTS": "false"}) == ["interface-gain"]
    assert names({"STREAMSTACK_AUTO_HARDWARE_GAIN": "false"}) == ["canon-exposure", "litra"]
    assert profiles.BY_ID  # the interface profiles used above exist
    assert all(isinstance(a, devices.Actuator) for a in real_hardware_actuators({}, lambda *a: None, lambda t: None))
