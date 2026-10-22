"""Controls catalogue, gphoto2 CLI parsing, and both camera sessions against fakes (no USB, no camera)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from streamstack.canon import controls as ctl
from streamstack.canon import gphoto
from streamstack.canon.gphoto import CameraError, CliSession, LibSession, parse_get_config

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLI = FIXTURES / "fake_gphoto2.py"
FRAME = FIXTURES / "canon_liveview_1056x704.jpg"

ISO_OUTPUT = """Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 400
Choice: 0 Auto
Choice: 1 100
Choice: 2 400
END
Label: Colour Temperature
Readonly: 0
Type: RANGE
Current: 5200
Bottom: 2500
Top: 10000
Step: 100
END
"""


def test_every_control_names_a_real_libgphoto2_key() -> None:
    assert {c.key for c in ctl.CONTROLS} == {
        "iso",
        "shutterspeed",
        "aperture",
        "exposurecompensation",
        "whitebalance",
        "colortemperature",
        "picturestyle",
        "focusmode",
        "liveviewsize",
        "cameramodel",
        "lensname",
        "batterylevel",
        "autoexposuremode",
    }
    assert ctl.BY_ID["kelvin"].prereq == ("whitebalance", "Color Temperature")


def test_focus_nudges_map_to_libgphoto2_choices() -> None:
    assert [ctl.focus_choice(s) for s in (-3, -1, 1, 3)] == ["Near 3", "Near 1", "Far 1", "Far 3"]
    for bad in (0, 4, -4):
        with pytest.raises(ValueError):
            ctl.focus_choice(bad)


@pytest.mark.parametrize(("given", "want"), [(5234, 5200), (100, 2500), (99999, 10000), ("6500", 6500), (5250.0, 5200)])
def test_kelvin_is_clamped_and_stepped(given: object, want: int) -> None:
    assert ctl.snap_kelvin(given) == want


@pytest.mark.parametrize("bad", [None, True, "warm", float("nan"), float("inf"), [5000]])
def test_kelvin_rejects_non_numbers(bad: object) -> None:
    with pytest.raises(ValueError):
        ctl.snap_kelvin(bad)


def test_largest_live_view_wins() -> None:
    assert ctl.best_liveview(["small", "Medium", "LARGE"]) == "LARGE"
    assert ctl.best_liveview(["Tiny"]) is None


def test_parse_get_config_reads_choices_and_ranges() -> None:
    blocks = parse_get_config(ISO_OUTPUT)
    assert [b["label"] for b in blocks] == ["ISO Speed", "Colour Temperature"]
    assert blocks[0]["choices"] == ["Auto", "100", "400"] and blocks[0]["value"] == "400"
    assert blocks[1]["range"] == (2500.0, 10000.0, 100.0)
    assert parse_get_config("") == [] and parse_get_config("garbage\nmore") == []


def test_cli_errors_are_owner_readable() -> None:
    assert gphoto.cli_error(1, "*** Error: Could not claim the USB device ***").kind == "busy"
    assert gphoto.cli_error(1, "*** Error: No camera found. ***").kind == "missing"
    assert gphoto.cli_error(2, "").kind == "io"


# ---------- libgphoto2 through a fake python-gphoto2 ----------
class FakeError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"error {code}")
        self.code = code


class FakeWidget:
    def __init__(self, key: str, spec: dict[str, Any]) -> None:
        self.key, self.spec = key, spec

    def get_type(self) -> int:
        return {"RADIO": 5, "RANGE": 2, "TEXT": 1}[self.spec["type"]]

    def get_label(self) -> str:
        return self.spec["label"]

    def get_readonly(self) -> int:
        return int(self.spec.get("readonly", False))

    def get_value(self) -> str:
        return self.spec["value"]

    def set_value(self, value: str) -> None:
        self.spec["value"] = value

    def count_choices(self) -> int:
        return len(self.spec.get("choices", []))

    def get_choice(self, i: int) -> str:
        return self.spec["choices"][i]

    def get_range(self) -> tuple[int, int, int]:
        return 2500, 10000, 100


class FakeFile:
    def get_data_and_size(self) -> memoryview:
        return memoryview(FRAME.read_bytes())


class FakeCamera:
    settings: ClassVar[dict[str, dict[str, Any]]] = {}
    fail_init: ClassVar[int | None] = None
    exited: ClassVar[bool] = False

    def init(self) -> None:
        if FakeCamera.fail_init is not None:
            raise FakeError(FakeCamera.fail_init)

    def exit(self) -> None:
        FakeCamera.exited = True

    def capture_preview(self) -> FakeFile:
        return FakeFile()

    def get_single_config(self, key: str) -> FakeWidget:
        if key not in self.settings:
            raise FakeError(gphoto.GP_ERROR_NOT_SUPPORTED)
        return FakeWidget(key, self.settings[key])

    def set_single_config(self, key: str, widget: FakeWidget) -> None:
        if widget.spec["value"] not in widget.spec.get("choices", [widget.spec["value"]]):
            raise FakeError(gphoto.GP_ERROR_BAD_PARAMETERS)


class FakeGp:
    GPhoto2Error = FakeError
    GP_WIDGET_TEXT, GP_WIDGET_RANGE, GP_WIDGET_RADIO = 1, 2, 5
    Camera = FakeCamera


@pytest.fixture
def fake_camera() -> type[FakeCamera]:
    FakeCamera.fail_init = None
    FakeCamera.exited = False
    FakeCamera.settings = {
        "iso": {"label": "ISO Speed", "type": "RADIO", "value": "400", "choices": ["100", "400", "800"]},
        "colortemperature": {"label": "Color Temperature", "type": "RANGE", "value": "5200"},
        "shutterspeed": {"label": "Shutter", "type": "RADIO", "value": "1/60", "choices": ["1/60"], "readonly": True},
    }
    return FakeCamera


def test_lib_session_reads_writes_and_streams(fake_camera: type[FakeCamera]) -> None:
    s = LibSession(FakeGp)
    s.open()
    got = s.read(["iso", "colortemperature", "shutterspeed", "nonexistent"])
    assert got["iso"].choices == ["100", "400", "800"] and got["iso"].value == "400" and got["iso"].type == "RADIO"
    assert got["colortemperature"].range == (2500.0, 10000.0, 100.0)
    assert got["shutterspeed"].readonly is True
    assert (
        got["nonexistent"].error and "libgphoto2" in got["nonexistent"].error
    )  # one unknown key does not fail the rest
    s.write([("iso", "800")])
    assert s.read(["iso"])["iso"].value == "800"
    assert s.next_frame(1.0) == FRAME.read_bytes()
    s.close()
    assert fake_camera.exited


def test_lib_session_maps_usb_errors(fake_camera: type[FakeCamera]) -> None:
    fake_camera.fail_init = gphoto.GP_ERROR_IO_USB_CLAIM
    with pytest.raises(CameraError) as busy:
        LibSession(FakeGp).open()
    assert busy.value.kind == "busy"
    fake_camera.fail_init = gphoto.GP_ERROR_MODEL_NOT_FOUND
    with pytest.raises(CameraError) as missing:
        LibSession(FakeGp).open()
    assert missing.value.kind == "missing"


def test_lib_session_rejects_a_value_the_camera_refuses(fake_camera: type[FakeCamera]) -> None:
    s = LibSession(FakeGp)
    s.open()
    with pytest.raises(CameraError) as err:
        s.write([("iso", "99999")])
    assert err.value.kind == "bad_value"


# ---------- the gphoto2 command, with the fake script ----------
@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "settings": {
                    "iso": {"label": "ISO Speed", "type": "RADIO", "value": "400", "choices": ["100", "400", "800"]},
                    "aperture": {"label": "Aperture", "type": "RADIO", "value": "2.8", "choices": ["2.8", "4"]},
                    "shutterspeed": {"label": "Shutter", "type": "RADIO", "value": "1/60", "readonly": True},
                }
            }
        )
    )
    monkeypatch.setenv("FAKE_GPHOTO2_STATE", str(state))
    monkeypatch.setenv("FAKE_GPHOTO2_FRAME", str(FRAME))
    monkeypatch.setenv("FAKE_GPHOTO2_FPS", "50")
    return state


def cli_session() -> CliSession:
    s = CliSession(gphoto2=str(FAKE_CLI))
    s.USB_SETTLE = 0.05
    return s


def wait_for(cond: Any, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


def test_cli_session_streams_frames(fake_cli: Path) -> None:
    s = cli_session()
    s.open()
    try:
        frames = [s.next_frame(2.0) for _ in range(3)]
        assert all(f == FRAME.read_bytes() for f in frames)
    finally:
        s.close()


def test_cli_session_pauses_live_view_for_a_setting_and_resumes(fake_cli: Path) -> None:
    s = cli_session()
    s.open()
    try:
        assert s.next_frame(2.0) is not None
        s.write([("iso", "800")])  # would fail with "claim" if live view were still running
        got = s.read(["iso", "aperture"])
        assert got["iso"].value == "800" and got["aperture"].choices == ["2.8", "4"]
        wait_for(lambda: s.next_frame(0.2) is not None)  # live view came back by itself
        log = Path(str(fake_cli) + ".log").read_text().split()
        assert log[:4] == ["live-start", "live-stop", "set", "iso=800"]  # stopped, changed, then back to live view
        assert log.count("live-start") >= 2
    finally:
        s.close()


def test_cli_session_read_survives_one_unknown_key(fake_cli: Path) -> None:
    s = cli_session()
    s.open()
    try:
        got = s.read(["iso", "bogus", "aperture"])
        assert got["iso"].value == "400" and got["aperture"].value == "2.8"
        assert got["bogus"].error
    finally:
        s.close()


def test_cli_session_reports_a_refused_value(fake_cli: Path) -> None:
    s = cli_session()
    s.open()
    try:
        with pytest.raises(CameraError):
            s.write([("iso", "12345")])
        with pytest.raises(CameraError):
            s.write([("shutterspeed", "1/30")])  # read-only on the body
    finally:
        s.close()


def test_cli_session_raises_when_another_program_holds_the_camera(fake_cli: Path) -> None:
    data = json.loads(fake_cli.read_text())
    data["busy"] = True
    fake_cli.write_text(json.dumps(data))
    s = cli_session()
    s.open()
    try:
        with pytest.raises(CameraError) as err:
            for _ in range(20):
                s.next_frame(0.2)
        assert err.value.kind == "busy"
    finally:
        s.close()
