"""Litra HID reports, discovery from fixture /sys trees, and the hidraw transport (over a FIFO, never a device)."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from streamstack import lighting
from streamstack.lighting import (
    MODELS,
    Found,
    HidrawTransport,
    LightingError,
    Litra,
    SimulatedTransport,
    answer_to,
    build_report,
    clamp_kelvin,
    discover,
    lumen_to_percent,
    percent_to_lumen,
)

BEAM = MODELS[0xC901]
GLOW = MODELS[0xC900]
LX = MODELS[0xC903]
VENDOR_DESCRIPTOR = bytes.fromhex("06 43 ff 0a 02 02 a1 01 85 11 95 13 75 08 09 02 91 00 c0")
PLAIN_DESCRIPTOR = bytes.fromhex("05 0c 09 01 a1 01 c0")


def hid(model, command, *payload) -> bytes:
    return bytes((0x11, 0xFF, model.feature, command, *payload)).ljust(20, b"\x00")


# -- reports -----------------------------------------------------------------


def test_model_table_matches_the_prior_art() -> None:
    assert {pid: (m.name, m.lumen_min, m.lumen_max, m.feature) for pid, m in MODELS.items()} == {
        0xC900: ("Litra Glow", 20, 250, 0x04),
        0xC901: ("Litra Beam", 30, 400, 0x04),
        0xB901: ("Litra Beam", 30, 400, 0x04),
        0xC903: ("Litra Beam LX", 30, 400, 0x06),
    }
    assert lighting.VENDOR_ID == 0x046D


def test_power_reports() -> None:
    assert lighting.report_power(BEAM, True) == bytes.fromhex("11ff041c01") + bytes(15)
    assert lighting.report_power(BEAM, False) == bytes.fromhex("11ff041c00") + bytes(15)
    assert lighting.report_power(LX, True)[:5] == bytes.fromhex("11ff061c01")


def test_brightness_report_is_big_endian_lumen() -> None:
    assert lighting.report_brightness(BEAM, 400) == bytes.fromhex("11ff044c0190") + bytes(14)
    assert lighting.report_brightness(GLOW, 20)[4:6] == bytes((0, 20))


def test_brightness_report_clamps_to_the_model() -> None:
    assert lighting.report_brightness(BEAM, 5)[4:6] == bytes((0, 30))
    assert lighting.report_brightness(GLOW, 9999)[4:6] == bytes((0, 250))


def test_temperature_report() -> None:
    assert lighting.report_temperature(BEAM, 6500) == bytes.fromhex("11ff049c1964") + bytes(14)
    assert lighting.report_temperature(BEAM, 2700)[4:6] == bytes((0x0A, 0x8C))


def test_get_reports() -> None:
    assert build_report(BEAM, lighting.CMD_GET_POWER)[:5] == bytes.fromhex("11ff040100")
    assert build_report(BEAM, lighting.CMD_GET_BRIGHTNESS)[:4] == bytes.fromhex("11ff0431")
    assert build_report(LX, lighting.CMD_GET_TEMPERATURE)[:4] == bytes.fromhex("11ff0681")


def test_report_rejects_an_oversize_payload() -> None:
    with pytest.raises(ValueError):
        build_report(BEAM, 0x1C, *range(17))
    with pytest.raises(ValueError):
        build_report(BEAM, 0x1C, 256)


def test_answer_decoding() -> None:
    assert lighting.decode_word(answer_to(BEAM, hid(BEAM, 0x31, 0x01, 0x2C), 0x31) or b"") == 300
    assert lighting.decode_power(answer_to(BEAM, hid(BEAM, 0x01, 1), 0x01) or b"") is True
    assert lighting.decode_power(answer_to(BEAM, hid(BEAM, 0x01, 0), 0x01) or b"") is False


@pytest.mark.parametrize(
    "report",
    [
        hid(BEAM, 0x81, 0x11, 0x94),  # another command
        hid(LX, 0x31, 1, 0),  # another model's feature byte
        bytes.fromhex("10ff040100"),  # another report ID
        bytes.fromhex("11fe040100"),  # another device byte
        b"\x11\xff\x04",  # truncated
    ],
)
def test_answers_for_something_else_are_ignored(report: bytes) -> None:
    assert answer_to(BEAM, report, 0x31) is None


def test_kelvin_is_stepped_and_clamped() -> None:
    assert [clamp_kelvin(k) for k in (0, 2700, 2749, 2750, 4567, 6500, 9000)] == [
        2700,
        2700,
        2700,
        2800,
        4600,
        6500,
        6500,
    ]


@pytest.mark.parametrize("model", [GLOW, BEAM, LX])
def test_percent_lumen_round_trip(model) -> None:
    assert percent_to_lumen(model, 1) == model.lumen_min
    assert percent_to_lumen(model, 100) == model.lumen_max
    assert percent_to_lumen(model, 0) == model.lumen_min
    assert percent_to_lumen(model, 500) == model.lumen_max
    for percent in range(1, 101):
        assert lumen_to_percent(model, percent_to_lumen(model, percent)) == percent


# -- the protocol over a transport -----------------------------------------------


def make_light(model=BEAM, **kwargs) -> tuple[Litra, SimulatedTransport]:
    transport = SimulatedTransport(model, **kwargs)
    found = Found(id="x", node=Path("/dev/null"), model=model, phys="usb", name=model.name)
    return Litra(found, transport), transport


def test_litra_sets_and_reads_back_through_the_wire_format() -> None:
    light, wire = make_light()
    light.set_power(True)
    assert light.set_lumen(250) == 250
    assert light.set_kelvin(5020) == 5000
    assert wire.writes == [
        bytes.fromhex("11ff041c01") + bytes(15),
        bytes.fromhex("11ff044c00fa") + bytes(14),
        bytes.fromhex("11ff049c1388") + bytes(14),
    ]
    assert (light.read_power(), light.read_lumen(), light.read_kelvin()) == (True, 250, 5000)


def test_litra_read_skips_unrelated_reports() -> None:
    light, wire = make_light()
    # a button-press notification arrives before the answer to our get
    original = wire.write

    def write(data: bytes) -> None:
        original(data)
        wire._inbox.insert(0, hid(BEAM, 0x01, 0))  # a stale power report in front of the brightness answer

    wire.write = write  # type: ignore[method-assign]
    wire.lumen = 123
    assert light.read_lumen() == 123


def test_litra_read_gives_none_when_the_light_is_silent() -> None:
    light, wire = make_light()
    wire._answer = lambda *a: None  # type: ignore[method-assign]
    assert light.read_power() is None


def test_simulated_light_rejects_malformed_reports() -> None:
    _, wire = make_light()
    with pytest.raises(LightingError):
        wire.write(b"\x11\xff")


# -- discovery ------------------------------------------------------------------


def make_node(
    root: Path,
    name: str,
    hid_id: str,
    phys: str,
    descriptor: bytes | None = VENDOR_DESCRIPTOR,
    uniq: str = "",
    hid_name: str = "Logitech Litra Beam",
) -> None:
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "uevent").write_text(
        f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME={hid_name}\nHID_PHYS={phys}\nHID_UNIQ={uniq}\nMODALIAS=hid:b0003\n"
    )
    if descriptor is not None:
        (device / "report_descriptor").write_bytes(descriptor)


def test_discovery_finds_each_light_once_on_its_vendor_interface(tmp_path: Path) -> None:
    sysfs, dev = tmp_path / "hidraw", tmp_path / "dev"
    # a Beam with a plain interface 0 and the vendor interface 1; a Glow; a Lightspeed receiver; another vendor
    make_node(sysfs, "hidraw1", "0003:0000046D:0000C901", "usb-0000:00:14.0-3/input0", PLAIN_DESCRIPTOR)
    make_node(sysfs, "hidraw2", "0003:0000046D:0000C901", "usb-0000:00:14.0-3/input1")
    make_node(sysfs, "hidraw3", "0003:0000046D:0000C900", "usb-0000:00:14.0-4/input0", hid_name="Logitech Litra Glow")
    make_node(sysfs, "hidraw4", "0003:0000046D:0000C547", "usb-0000:00:14.0-5/input2", hid_name="Logitech USB Receiver")
    make_node(sysfs, "hidraw5", "0003:00001234:0000C901", "usb-0000:00:14.0-6/input0")
    found = discover(sysfs, dev)
    assert [(f.model.name, f.node, f.phys) for f in found] == [
        ("Litra Glow", dev / "hidraw3", "usb-0000:00:14.0-4/input0"),
        ("Litra Beam", dev / "hidraw2", "usb-0000:00:14.0-3/input1"),
    ]
    assert found[1].id == "c901-usb-0000-00-14.0-3"
    assert [f.id for f in found] == sorted(f.id for f in found)


def test_discovery_prefers_the_serial_number_for_the_id(tmp_path: Path) -> None:
    make_node(tmp_path, "hidraw0", "0003:0000046D:0000C903", "usb-1/input0", uniq="ABC 123")
    [light] = discover(tmp_path, tmp_path / "dev")
    assert light.id == "c903-ABC-123"
    assert light.model.name == "Litra Beam LX"


def test_discovery_falls_back_to_the_lowest_interface_without_a_descriptor(tmp_path: Path) -> None:
    make_node(tmp_path, "hidraw8", "0003:0000046D:0000C901", "usb-1/input1", None)
    make_node(tmp_path, "hidraw7", "0003:0000046D:0000C901", "usb-1/input0", None)
    [light] = discover(tmp_path, tmp_path / "dev")
    assert light.node.name == "hidraw7"


def test_two_lights_of_one_model_get_two_ids(tmp_path: Path) -> None:
    make_node(tmp_path, "hidraw0", "0003:0000046D:0000C901", "usb-1/input0")
    make_node(tmp_path, "hidraw1", "0003:0000046D:0000C901", "usb-2/input0")
    assert len({f.id for f in discover(tmp_path, tmp_path / "dev")}) == 2


def test_discovery_skips_unreadable_and_malformed_nodes(tmp_path: Path) -> None:
    (tmp_path / "hidraw0").mkdir()  # no device/uevent at all
    make_node(tmp_path, "hidraw1", "garbage", "usb-1/input0")
    (tmp_path / "unrelated").mkdir()
    assert discover(tmp_path, tmp_path / "dev") == []


def test_discovery_without_a_hidraw_class_is_empty(tmp_path: Path) -> None:
    assert discover(tmp_path / "missing", tmp_path) == []


def test_discovery_reports_an_unlistable_sysfs(tmp_path: Path) -> None:
    afile = tmp_path / "file"
    afile.write_text("x")
    with pytest.raises(LightingError):
        discover(afile, tmp_path)


# -- the hidraw transport, over a FIFO -----------------------------------------------


@pytest.fixture
def fifo(tmp_path: Path) -> Path:
    path = tmp_path / "hidraw-fifo"
    os.mkfifo(path)
    return path


def test_transport_writes_and_reads_whole_reports(fifo: Path) -> None:
    wire = HidrawTransport(fifo)  # a FIFO opened read-write loops our own writes back
    try:
        assert wire.read(0.0) is None
        report = lighting.report_power(BEAM, True)
        wire.write(report)
        assert wire.read(0.5) == report
        wire.write(report)
        wire.write(report)
        wire.drain()
        assert wire.read(0.0) is None
    finally:
        wire.close()


def test_transport_close_is_final_and_idempotent(fifo: Path) -> None:
    wire = HidrawTransport(fifo)
    wire.close()
    wire.close()
    with pytest.raises(LightingError) as err:
        wire.write(b"x")
    assert err.value.kind == "gone"


def test_permission_denied_has_an_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(LightingError) as err:
        HidrawTransport(Path("/dev/hidraw9"))
    assert err.value.kind == "permission"
    assert "/dev/hidraw9" in err.value.message and "udev rule" in err.value.message


@pytest.mark.parametrize(("code", "kind"), [(errno.ENODEV, "gone"), (errno.EIO, "gone"), (errno.EBUSY, "io")])
def test_other_open_errors_are_classified(monkeypatch: pytest.MonkeyPatch, code: int, kind: str) -> None:
    def fail(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "open", fail)
    with pytest.raises(LightingError) as err:
        HidrawTransport(Path("/dev/hidraw9"))
    assert err.value.kind == kind
