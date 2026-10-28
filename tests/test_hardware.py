"""The hardware layer: amixer parsing, USB detection, profile selection, the Scarlett and Wave XLR drivers.

The amixer fixtures in tests/fixtures are SYNTHESIZED from the control names and ranges in the kernel's
sound/usb/mixer_scarlett2.c, not captured from a device; the Wave XLR blocks follow the layout documented by
OpenWave. They pin our parsing and our assumptions, not the hardware's behaviour (docs/interfaces.md lists what
still needs a real-hardware check).
"""

from __future__ import annotations

import ctypes
import struct
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from streamstack.console.errors import ActionError
from streamstack.hardware import alsa, profiles, sim, usb, wavexlr
from streamstack.hardware.alsa import Mixer, MixerError
from streamstack.hardware.scarlett import Scarlett

FIXTURES = Path(__file__).parent / "fixtures"
GEN4, GEN3 = sim.SCARLETT_DEVICES["gen4"], sim.SCARLETT_DEVICES["gen3"]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeAmixer:
    """Answers `amixer contents` from a fixture and records every call; `cset` changes the listed value."""

    def __init__(self, text: str) -> None:
        self.controls = alsa.parse_contents(text)
        self.calls: list[list[str]] = []
        self.lock_all = False

    def __call__(self, argv: list[str], timeout: float) -> CompletedProcess[str]:
        self.calls.append(argv)
        args = argv[3:]
        if args == ["contents"]:
            return CompletedProcess(argv, 0, self._render(), "")
        if args[0] == "cget":
            numid = int(args[1].removeprefix("numid="))
            return CompletedProcess(argv, 0, self._render(only=numid), "")
        if args[:2] == ["-q", "cset"]:
            numid = int(args[2].removeprefix("numid="))
            ctl = next(c for c in self.controls.values() if c.numid == numid)
            ctl.values = [int(args[3])]
            return CompletedProcess(argv, 0, "", "")
        return CompletedProcess(argv, 1, "", "unexpected call")

    def _render(self, only: int | None = None) -> str:
        lines: list[str] = []
        for c in self.controls.values():
            if only is not None and c.numid != only:
                continue
            access = "r-------" if self.lock_all else c.access
            lines.append(f"numid={c.numid},iface={c.iface},name='{c.name}'")
            extra = ""
            if c.type == "ENUMERATED":
                extra = f",items={len(c.items)}"
            elif c.type == "INTEGER":
                extra = f",min={c.minimum},max={c.maximum},step=1"
            lines.append(f"  ; type={c.type},access={access},values=1{extra}")
            lines += [f"  ; Item #{i} '{t}'" for i, t in enumerate(c.items)]
            v = c.values[0]
            lines.append("  : values=" + (("on" if v else "off") if c.type == "BOOLEAN" else str(v)))
        return "\n".join(lines) + "\n"


def scarlett(fixture_name: str, device: usb.UsbDevice, tmp_path: Path) -> tuple[Scarlett, FakeAmixer]:
    fake = FakeAmixer(fixture(fixture_name))
    profile = profiles.profile_for(device)
    assert profile is not None
    proc = sim.sim_proc(tmp_path / "proc", card=7)
    return Scarlett(profile, device, Mixer(7, run=fake), proc_root=proc), fake


def widgets(state: dict) -> dict[str, dict]:
    return {w["id"]: w for w in state["controls"]}


# -- amixer parsing ---------------------------------------------------------------------------------------------


def test_parse_gen4_contents_names_types_and_ranges() -> None:
    controls = alsa.parse_contents(fixture("amixer_scarlett_2i2_gen4.txt"))
    gain = controls["Line In 1 Gain Capture Volume"]
    assert (gain.type, gain.minimum, gain.maximum, gain.value, gain.db_range) == ("INTEGER", 0, 70, 38, (0.0, 69.0))
    assert gain.writable
    air = controls["Line In 1 Air Capture Enum"]
    assert air.items == ["Off", "Presence", "Presence + Drive"] and air.item_text() == "Off"
    assert controls["Line In 1-2 Phantom Power Capture Switch"].type == "BOOLEAN"
    assert controls["Line In 1-2 Phantom Power Capture Switch"].value == 0
    assert not controls["Sync Status"].writable
    assert controls["Firmware Version"].iface == "CARD"


def test_parse_gen3_has_no_gain_or_autogain() -> None:
    names = set(alsa.parse_contents(fixture("amixer_scarlett_2i2_gen3.txt")))
    assert "Line In 1 Air Capture Switch" in names
    assert not any("Gain Capture Volume" in n or "Autogain" in n for n in names)


def test_parse_skips_noise_and_empty_input() -> None:
    assert alsa.parse_contents("") == {}
    assert alsa.parse_contents("garbage\n  : values=3\n") == {}


def test_mixer_commands_are_exact_and_numid_addressed() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: float) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0, "", "")

    mixer = Mixer(3, run=run)
    mixer.put(6, [40])
    assert calls == [["amixer", "-c", "3", "-q", "cset", "numid=6", "40"]]


@pytest.mark.parametrize("bad", [[], ["40"], [True], [1.5]])
def test_mixer_put_refuses_non_integers(bad: list) -> None:
    mixer = Mixer(3, run=lambda argv, timeout: CompletedProcess(argv, 0, "", ""))
    with pytest.raises(MixerError):
        mixer.put(6, bad)


def test_mixer_errors_carry_amixer_message() -> None:
    failing = Mixer(3, run=lambda argv, timeout: CompletedProcess(argv, 1, "", "amixer: Invalid card number.\n"))
    with pytest.raises(MixerError, match="Invalid card number"):
        failing.controls()
    silent = Mixer(3, run=lambda argv, timeout: None)
    with pytest.raises(MixerError, match="did not answer"):
        silent.controls()
    empty = Mixer(3, run=lambda argv, timeout: CompletedProcess(argv, 0, "", ""))
    with pytest.raises(MixerError, match="no mixer controls"):
        empty.controls()


def test_capture_status_from_hw_params_and_stream0(tmp_path: Path) -> None:
    sim.sim_proc(tmp_path, card=2, rate=96000, period=256)
    assert alsa.capture_status(2, tmp_path) == {"rate": 96000, "period": 256}
    only = tmp_path / "asound" / "card5"
    only.mkdir(parents=True)
    (only / "stream0").write_text("Capture:\n  Momentary freq = 48000 Hz (0x30.0000)\n")
    assert alsa.capture_status(5, tmp_path) == {"rate": 48000}
    assert alsa.capture_status(9, tmp_path) is None


# -- USB detection ----------------------------------------------------------------------------------------------


def fake_bus(root: Path) -> tuple[Path, Path]:
    sys_root, proc = root / "sys", root / "proc"
    devices = sys_root / "bus" / "usb" / "devices"
    for name, vid, pid, product, bus, dev in (
        ("1-1.4", "0fd9", "007d", "Wave XLR", 1, 9),
        ("1-2", "1235", "8219", "Scarlett 2i2 4th Gen", 1, 4),
        ("1-3", "046d", "c52b", "Receiver", 1, 5),
    ):
        d = devices / name
        d.mkdir(parents=True)
        for file, text in (
            ("idVendor", vid),
            ("idProduct", pid),
            ("product", product),
            ("busnum", bus),
            ("devnum", dev),
        ):
            (d / file).write_text(f"{text}\n")
    (devices / "1-2:1.0").mkdir()  # an interface, not a device
    (proc / "asound").mkdir(parents=True)
    (proc / "asound" / "cards").write_text(" 0 [PCH]: HDA-Intel\n 1 [XLR]: USB-Audio - Wave XLR\n")
    (proc / "asound" / "card1").mkdir()
    (proc / "asound" / "card1" / "usbbus").write_text("001/009\n")
    (proc / "asound" / "card1" / "usbid").write_text("0fd9:007d\n")
    return sys_root, proc


def test_scan_finds_devices_and_their_cards(tmp_path: Path) -> None:
    sys_root, proc = fake_bus(tmp_path)
    found = usb.scan(sys_root, proc)
    assert [d.usb_id for d in found] == ["1235:8219", "046d:c52b", "0fd9:007d"]
    xlr = next(d for d in found if d.usb_id == "0fd9:007d")
    assert xlr.card == 1 and xlr.node == "/dev/bus/usb/001/009"
    assert next(d for d in found if d.usb_id == "1235:8219").card is None


def test_scan_survives_a_missing_bus(tmp_path: Path) -> None:
    assert usb.scan(tmp_path / "nothing", tmp_path / "nothing") == []


# -- profile selection ------------------------------------------------------------------------------------------


def test_profiles_match_kernel_usb_ids() -> None:
    assert profiles.BY_ID["scarlett-2i2-gen4"].usb_id == "1235:8219"
    assert profiles.BY_ID["scarlett-2i2-gen3"].usb_id == "1235:8210"
    assert profiles.BY_ID["wave-xlr"].usb_id == "0fd9:007d"


def test_select_auto_prefers_focusrite_and_honours_overrides() -> None:
    both = [sim.XLR_DEVICE, GEN4]
    chosen = profiles.select(both)
    assert chosen is not None and chosen[0].id == "scarlett-2i2-gen4"
    pinned = profiles.select(both, "wave-xlr")
    assert pinned is not None and pinned[0].id == "wave-xlr"
    by_usb = profiles.select(both, "0fd9:007d")
    assert by_usb is not None and by_usb[1] is sim.XLR_DEVICE
    assert profiles.select(both, "off") is None
    assert profiles.select([], "auto") is None
    assert profiles.select([GEN4], "wave-xlr") is None


def test_unknown_focusrite_model_is_a_known_vendor_without_profile() -> None:
    solo = usb.UsbDevice(0x1235, 0x8211, "Scarlett Solo", 1, 3, 4)
    assert profiles.known_vendor(solo) and profiles.profile_for(solo) is None
    assert not profiles.known_vendor(GEN4)


# -- Scarlett driver --------------------------------------------------------------------------------------------


def test_gen4_read_lists_gain_autogain_air_and_phantom(tmp_path: Path) -> None:
    driver, _ = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    state = driver.read()
    w = widgets(state)
    assert {"gain.1", "autogain.1", "air.1", "safe.1", "phantom.1", "direct_monitor"} <= set(w)
    assert w["gain.1"]["kind"] == "slider" and (w["gain.1"]["min"], w["gain.1"]["max"]) == (0, 70)
    assert w["air.1"]["options"] == ["Off", "Presence", "Presence + Drive"]
    assert w["phantom.1"]["danger"] is True
    assert state["capabilities"]["autogain"] and state["capabilities"]["input_gain"]
    assert any(i["id"] == "rate" and "48 kHz" in i["value"] for i in state["info"])


def test_gen3_read_has_air_switch_and_no_gain(tmp_path: Path) -> None:
    driver, _ = scarlett("amixer_scarlett_2i2_gen3.txt", GEN3, tmp_path)
    state = driver.read()
    w = widgets(state)
    assert w["air.1"]["kind"] == "switch" and "gain.1" not in w and "autogain.1" not in w
    assert state["capabilities"]["input_gain"] is False and state["capabilities"]["air"] is True


def test_apply_gain_writes_by_numid_then_verifies(tmp_path: Path) -> None:
    driver, fake = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    numid = fake.controls["Line In 1 Gain Capture Volume"].numid
    driver.apply("gain.1", 45, False)
    assert ["amixer", "-c", "7", "-q", "cset", f"numid={numid}", "45"] in fake.calls
    assert widgets(driver.read())["gain.1"]["value"] == 45


def test_apply_clamps_gain_into_the_control_range(tmp_path: Path) -> None:
    driver, fake = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    driver.apply("gain.1", 500, False)
    assert fake.controls["Line In 1 Gain Capture Volume"].value == 70


def test_phantom_on_needs_confirm_but_off_does_not(tmp_path: Path) -> None:
    driver, fake = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    phantom = fake.controls["Line In 1-2 Phantom Power Capture Switch"]
    with pytest.raises(ActionError, match="confirmation") as err:
        driver.apply("phantom.1", True, False)
    assert err.value.status == 400 and phantom.value == 0
    driver.apply("phantom.1", True, True)
    assert phantom.value == 1
    driver.apply("phantom.1", False, False)
    assert phantom.value == 0


@pytest.mark.parametrize(("wid", "value"), [("air.1", 9), ("air.1", True), ("air.1", -1), ("nope", 1)])
def test_apply_rejects_bad_requests(tmp_path: Path, wid: str, value: object) -> None:
    driver, _ = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    with pytest.raises(ActionError) as err:
        driver.apply(wid, value, False)
    assert err.value.status == 400


def test_apply_refuses_a_locked_control(tmp_path: Path) -> None:
    driver, fake = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    fake.lock_all = True
    with pytest.raises(ActionError, match="locked"):
        driver.apply("gain.1", 10, False)


def test_apply_reports_a_value_the_device_did_not_keep(tmp_path: Path) -> None:
    driver, fake = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    inner = fake.__call__

    def stubborn(argv: list[str], timeout: float) -> CompletedProcess[str]:
        result = inner(argv, timeout)
        if argv[3:5] == ["-q", "cset"]:
            next(c for c in fake.controls.values() if c.numid == int(argv[5].removeprefix("numid="))).values = [1]
        return result

    driver.mixer._run = stubborn
    with pytest.raises(ActionError, match="did not accept"):
        driver.apply("gain.1", 50, False)


def test_apply_surfaces_an_amixer_failure(tmp_path: Path) -> None:
    driver, _fake = scarlett("amixer_scarlett_2i2_gen4.txt", GEN4, tmp_path)
    driver.read()
    driver.mixer._run = lambda argv, timeout: CompletedProcess(argv, 1, "", "Operation not permitted")
    with pytest.raises(ActionError, match="Operation not permitted"):
        driver.apply("gain.1", 50, False)


def test_simulated_autogain_runs_locks_and_succeeds(tmp_path: Path) -> None:
    clock = [0.0]
    mixer = sim.SimMixer("gen4", clock=lambda: clock[0])
    profile = profiles.BY_ID["scarlett-2i2-gen4"]
    driver = Scarlett(profile, GEN4, mixer, proc_root=sim.sim_proc(tmp_path, card=7))
    driver.apply("autogain.1", True, False)
    running = widgets(driver.read())
    assert running["autogain.1"]["value"] is True and "Measuring" in running["autogain.1"]["text"]
    assert running["gain.1"]["readonly"]
    with pytest.raises(ActionError, match="locked"):
        driver.apply("gain.1", 10, False)
    clock[0] += sim.AUTOGAIN_SECONDS + 0.1
    done = widgets(driver.read())
    assert done["autogain.1"]["value"] is False and done["autogain.1"]["state"] == "ok"
    assert done["gain.1"]["readonly"] == "" and done["gain.1"]["value"] == 40


def test_missing_amixer_is_a_clear_error(tmp_path: Path) -> None:
    profile = profiles.BY_ID["scarlett-2i2-gen4"]
    driver = Scarlett(profile, GEN4, Mixer(7, binary="amixer-not-installed"), proc_root=tmp_path)
    with pytest.raises(MixerError, match="amixer is missing"):
        driver.read()


# -- Wave XLR ---------------------------------------------------------------------------------------------------


def xlr_config() -> bytes:
    return bytes.fromhex(fixture("wave_xlr_config.hex").strip())


def xlr_devinfo() -> bytes:
    return bytes.fromhex(fixture("wave_xlr_devinfo.hex").strip())


def test_xlr_codec_parses_the_documented_layout() -> None:
    cfg = wavexlr.parse_config(xlr_config())
    assert cfg == {
        "gain_db": 42.0, "muted": False, "phantom": False, "headphone_db": -3.0, "knob": "Gain", "low_z": False,
    }  # fmt: skip
    info = wavexlr.parse_devinfo(xlr_devinfo())
    assert info["firmware"] == "1.0.9" and info["api"] == "1.0"


def test_xlr_patch_changes_only_named_fields() -> None:
    raw = xlr_config()
    patched = wavexlr.patch_config(raw, gain_db=30.5, muted=True)
    assert struct.unpack_from("<H", patched, 0)[0] == int(30.5 * 256)
    assert patched[4] == 1
    changed = {i for i in range(len(raw)) if raw[i] != patched[i]}
    assert changed <= {0, 1, 4}
    assert patched[30] == 0x5A  # an unknown byte survives


def test_xlr_codec_rejects_short_blocks() -> None:
    with pytest.raises(wavexlr.XlrError):
        wavexlr.parse_config(b"\0" * 10)
    with pytest.raises(wavexlr.XlrError):
        wavexlr.parse_devinfo(b"\0" * 10)


def test_usbfs_control_struct_matches_the_kernel_abi() -> None:
    assert ctypes.sizeof(wavexlr._CtrlTransfer) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    # _IOWR('U', 0, struct usbdevfs_ctrltransfer) on 64-bit Linux
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert wavexlr.USBDEVFS_CONTROL == 0xC0185500
    assert (wavexlr.RT_CLASS_IN, wavexlr.RT_CLASS_OUT, wavexlr.REQUEST_READ, wavexlr.REQUEST_WRITE, wavexlr.WINDEX) == (
        0xA1, 0x21, 0x85, 0x05, 0x3303,
    )  # fmt: skip


def xlr_driver(mode: str, tmp_path: Path, device: sim.SimXlr | None = None) -> tuple[wavexlr.WaveXlr, sim.SimXlr]:
    dev = device or sim.SimXlr()
    driver = wavexlr.WaveXlr(
        profiles.BY_ID["wave-xlr"], sim.XLR_DEVICE, mode, dev.opener, sim.sim_proc(tmp_path, card=7)
    )
    return driver, dev


def test_xlr_mode_off_never_opens_the_device(tmp_path: Path) -> None:
    def forbidden(device: usb.UsbDevice) -> wavexlr.Transport:
        raise AssertionError("the Wave XLR must not be opened while hardware control is off")

    driver = wavexlr.WaveXlr(profiles.BY_ID["wave-xlr"], sim.XLR_DEVICE, "off", forbidden, tmp_path)
    state = driver.read()
    assert state["controls"] == [] and "STREAMSTACK_WAVE_XLR_USB" in state["notice"] and state["mode"] == "off"
    with pytest.raises(ActionError, match="off"):
        driver.apply("gain", 30, False)
    assert wavexlr.WaveXlr(profiles.BY_ID["wave-xlr"], sim.XLR_DEVICE, "bogus", forbidden).mode == "off"


def test_xlr_read_mode_shows_values_but_locks_them(tmp_path: Path) -> None:
    driver, dev = xlr_driver("read", tmp_path)
    state = driver.read()
    w = widgets(state)
    assert w["gain"]["value"] == 42.0 and w["gain"]["readonly"] and w["phantom"]["danger"]
    assert {u["label"] for u in state["unsupported"]} == {"Clipguard", "Low cut"}
    assert state["capabilities"]["clipguard"] is False and state["capabilities"]["low_cut"] is False
    with pytest.raises(ActionError, match="off"):
        driver.apply("gain", 30, False)
    assert dev.writes == []


def test_xlr_write_is_read_modify_write_with_readback(tmp_path: Path) -> None:
    driver, dev = xlr_driver("write", tmp_path)
    driver.apply("gain", 30.5, False)
    driver.apply("mute", True, False)
    assert len(dev.writes) == 2
    assert dev.config[30] == 0x5A
    state = widgets(driver.read())
    assert state["gain"]["value"] == 30.5 and state["mute"]["value"] is True and state["gain"]["readonly"] == ""


def test_xlr_gain_is_clamped_and_phantom_needs_confirm(tmp_path: Path) -> None:
    driver, dev = xlr_driver("write", tmp_path)
    driver.apply("gain", 999, False)
    assert wavexlr.parse_config(dev.config)["gain_db"] == wavexlr.GAIN_MAX_DB
    with pytest.raises(ActionError, match="confirmation"):
        driver.apply("phantom", True, False)
    assert wavexlr.parse_config(dev.config)["phantom"] is False
    driver.apply("phantom", True, True)
    assert wavexlr.parse_config(dev.config)["phantom"] is True


@pytest.mark.parametrize(("wid", "value"), [("gain", "loud"), ("gain", float("nan")), ("mute", 5), ("lights", 1)])
def test_xlr_rejects_bad_requests(tmp_path: Path, wid: str, value: object) -> None:
    driver, dev = xlr_driver("write", tmp_path)
    with pytest.raises(ActionError) as err:
        driver.apply(wid, value, False)
    assert err.value.status == 400 and dev.writes == []


def test_xlr_open_failure_becomes_a_notice_or_an_action_error(tmp_path: Path) -> None:
    driver, dev = xlr_driver("write", tmp_path)
    dev.fail = wavexlr.XlrError("No permission to open /dev/bus/usb/001/013.")
    assert "No permission" in driver.read()["notice"]
    with pytest.raises(ActionError, match="No permission"):
        driver.apply("gain", 20, False)


def test_xlr_detects_a_setting_the_device_did_not_keep(tmp_path: Path) -> None:
    driver, dev = xlr_driver("write", tmp_path)
    dev.ignore_mute = True
    with pytest.raises(ActionError, match="did not keep"):
        driver.apply("mute", True, False)


def test_xlr_phantom_is_refused_while_a_call_is_live(tmp_path: Path) -> None:
    dev = sim.SimXlr()
    for active, ok in ((True, False), (False, True)):
        driver = wavexlr.WaveXlr(
            profiles.BY_ID["wave-xlr"], sim.XLR_DEVICE, "write", dev.opener, tmp_path, call_active=lambda a=active: a
        )
        if ok:
            driver.apply("phantom", True, True)
            assert wavexlr.parse_config(dev.config)["phantom"] is True
        else:
            writes = len(dev.writes)
            with pytest.raises(ActionError, match="call is live") as caught:
                driver.apply("phantom", True, True)
            assert caught.value.status == 409 and len(dev.writes) == writes
            driver.apply("gain", 30, False)  # the gain is not a surprise to the call; only 48V is held back


def test_xlr_phantom_fails_closed_when_the_call_detector_breaks(tmp_path: Path) -> None:
    def broken() -> bool:
        raise OSError("pactl missing")

    dev = sim.SimXlr()
    driver = wavexlr.WaveXlr(
        profiles.BY_ID["wave-xlr"], sim.XLR_DEVICE, "write", dev.opener, tmp_path, call_active=broken
    )
    with pytest.raises(ActionError, match="Cannot tell"):
        driver.apply("phantom", False, False)
    assert dev.writes == []


class _CachedMixer:
    def __init__(self, raw: int, db: bool = True) -> None:
        text = (
            "numid=6,iface=MIXER,name='Mic Capture Volume'\n"
            "  ; type=INTEGER,access=rw---R--,values=1,min=0,max=150,step=1\n"
            + ("  | dBminmax-min=0.00dB,max=75.00dB\n" if db else "")
            + f"  : values={raw}\n"
        )
        self.controls_ = alsa.parse_contents(text)

    def available(self) -> bool:
        return True

    def controls(self) -> dict[str, alsa.MixerControl]:
        return self.controls_


def test_xlr_shows_a_kernel_cache_that_disagrees_with_the_device(tmp_path: Path) -> None:
    dev = sim.SimXlr()  # the fixture block says 42 dB
    make = lambda raw, db=True: wavexlr.WaveXlr(  # noqa: E731
        profiles.BY_ID["wave-xlr"], sim.XLR_DEVICE, "read", dev.opener, sim.sim_proc(tmp_path, card=7),
        mixer=lambda card: _CachedMixer(raw, db),
    )  # fmt: skip
    info = {w["id"]: w for w in make(148).read()["info"]}
    assert info["alsa_cache"]["state"] == "warn" and "74 dB" in info["alsa_cache"]["value"]
    assert "alsa_cache" not in {w["id"] for w in make(84).read()["info"]}  # 42 dB agrees
    assert "alsa_cache" not in {w["id"] for w in make(148, db=False).read()["info"]}  # no dB scale, no verdict
