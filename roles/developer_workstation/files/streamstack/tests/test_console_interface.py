"""The Console's interface controller: detection, cached state, actions, toasts, and the demo twin."""

from __future__ import annotations

from typing import Any

import pytest

from streamstack.console.errors import ActionError
from streamstack.console.interface import DemoInterface, InterfaceController
from streamstack.console.rig import Rig
from streamstack.hardware import sim
from streamstack.hardware.alsa import MixerError
from streamstack.hardware.profiles import Profile
from streamstack.hardware.usb import UsbDevice


class StubDriver:
    def __init__(self) -> None:
        self.reads = 0
        self.built: list[Profile] = []
        self.applied: list[tuple[str, Any, bool]] = []
        self.fail_read: Exception | None = None
        self.fail_apply: Exception | None = None

    def read(self) -> dict[str, Any]:
        self.reads += 1
        if self.fail_read:
            raise self.fail_read
        return {"controls": [{"id": "x", "kind": "switch", "value": False}], "info": [], "notice": "hello"}

    def apply(self, control_id: str, value: Any, confirm: bool) -> str | None:
        if self.fail_apply:
            raise self.fail_apply
        self.applied.append((control_id, value, confirm))
        return "done"


@pytest.fixture
def bus() -> list[UsbDevice]:
    return [sim.SCARLETT_DEVICES["gen4"]]


@pytest.fixture
def driver() -> StubDriver:
    return StubDriver()


def controller(bus: list[UsbDevice], driver: StubDriver, setting: str = "auto") -> tuple[InterfaceController, list]:
    toasts: list[tuple[str, str]] = []

    def factory(profile: Profile, device: UsbDevice) -> StubDriver:
        driver.built.append(profile)
        return driver

    ctl = InterfaceController({"STREAMSTACK_INTERFACE": setting}, scan=lambda: list(bus), factory=factory)
    ctl.notify = lambda level, text: toasts.append((level, text))
    return ctl, toasts


def test_state_before_any_probe_is_empty_and_cheap(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    state = ctl.state()
    assert state["present"] is False and state["checked"] is False and state["software_gain"] is True
    assert driver.reads == 0


def test_refresh_detects_and_reads(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    ctl.refresh()
    state = ctl.state()
    assert state["present"] and state["profile"] == "scarlett-2i2-gen4" and state["usb"] == "1235:8219"
    assert state["controls"][0]["id"] == "x" and state["notice"] == "hello" and state["error"] is None
    ctl.refresh()
    assert len(driver.built) == 1


def test_nothing_connected_and_unsupported_model(driver: StubDriver) -> None:
    ctl, _ = controller([], driver)
    ctl.refresh()
    assert ctl.state()["present"] is False and ctl.state()["checked"] is True and ctl.state()["notice"] is None
    solo = UsbDevice(0x1235, 0x8211, "Scarlett Solo", 1, 3, 4)
    ctl2, _ = controller([solo], driver)
    ctl2.refresh()
    assert "no StreamStack profile" in ctl2.state()["notice"]
    assert ctl2.state()["devices"] == [{"usb": "1235:8211", "name": "Scarlett Solo", "profile": None}]


def test_setting_off_and_pinned_profile(bus: list[UsbDevice], driver: StubDriver) -> None:
    off, _ = controller(bus, driver, "off")
    off.refresh()
    assert off.state()["present"] is False and "off" in off.state()["notice"]
    pinned, _ = controller([sim.XLR_DEVICE, *bus], driver, "wave-xlr")
    pinned.refresh()
    assert pinned.state()["profile"] == "wave-xlr"


def test_driver_failure_is_reported_and_retried(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    driver.fail_read = MixerError("amixer is missing (install alsa-utils).")
    ctl.refresh()
    state = ctl.state()
    assert state["present"] and "amixer is missing" in state["error"] and state["controls"] == []
    driver.fail_read = None
    ctl.refresh()
    assert ctl.state()["error"] is None and len(driver.built) == 2


def test_toasts_only_for_changes_after_the_first_probe(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, toasts = controller(bus, driver)
    ctl.refresh()
    assert toasts == []
    bus.clear()
    ctl.refresh()
    assert toasts == [("warn", "The audio interface was unplugged.")]
    bus.append(sim.SCARLETT_DEVICES["gen3"])
    ctl.refresh()
    assert toasts[-1] == ("ok", "Focusrite Scarlett 2i2 (3rd Gen) connected.")


def test_set_goes_to_the_driver_and_refreshes(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    ctl.refresh()
    reads = driver.reads
    assert ctl.act("interface.set", {"id": "x", "value": True, "confirm": True}) == "done"
    assert driver.applied == [("x", True, True)] and driver.reads == reads + 1


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"id": 5, "value": 1},
        {"id": "", "value": 1},
        {"id": "x" * 41, "value": 1},
        {"id": "x"},
        {"id": "x", "value": 1, "confirm": "yes"},
    ],
)
def test_set_validates_its_input(bus: list[UsbDevice], driver: StubDriver, params: dict) -> None:
    ctl, _ = controller(bus, driver)
    ctl.refresh()
    with pytest.raises(ActionError) as err:
        ctl.act("interface.set", params)
    assert err.value.status == 400 and driver.applied == []


def test_set_without_an_interface_is_a_409(driver: StubDriver) -> None:
    ctl, _ = controller([], driver)
    ctl.refresh()
    with pytest.raises(ActionError) as err:
        ctl.act("interface.set", {"id": "x", "value": 1})
    assert err.value.status == 409


def test_driver_errors_become_action_errors(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    ctl.refresh()
    driver.fail_apply = MixerError("Operation not permitted")
    with pytest.raises(ActionError, match="Operation not permitted") as err:
        ctl.act("interface.set", {"id": "x", "value": 1})
    assert err.value.status == 409


def test_unknown_action_and_rescan(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    with pytest.raises(ActionError) as err:
        ctl.act("interface.explode", {})
    assert err.value.status == 400
    ctl.act("interface.rescan", {})
    assert ctl.state()["present"]


def test_scan_failure_is_reported(driver: StubDriver) -> None:
    def broken() -> list[UsbDevice]:
        raise OSError("sysfs unreadable")

    ctl = InterfaceController({}, scan=broken, factory=lambda p, d: driver)
    ctl.refresh()
    assert "sysfs unreadable" in ctl.state()["error"]


def test_background_thread_starts_and_stops(bus: list[UsbDevice], driver: StubDriver) -> None:
    ctl, _ = controller(bus, driver)
    ctl.start()
    try:
        deadline = 0
        while not ctl.state()["present"] and deadline < 200:
            ctl._wake.wait(0.02)
            deadline += 1
        assert ctl.state()["present"]
    finally:
        ctl.stop()
    assert ctl._thread is None


# -- demo ------------------------------------------------------------------------------------------------------


def test_demo_defaults_to_a_gen4_with_working_controls() -> None:
    demo = DemoInterface()
    state = demo.state()
    assert state["profile"] == "scarlett-2i2-gen4" and state["demo_profiles"]
    demo.act("interface.set", {"id": "gain.1", "value": 44})
    assert next(w for w in demo.state()["controls"] if w["id"] == "gain.1")["value"] == 44


@pytest.mark.parametrize(
    ("choice", "profile", "present"),
    [("scarlett-2i2-gen3", "scarlett-2i2-gen3", True), ("wave-xlr", "wave-xlr", True), ("none", None, False)],
)
def test_demo_can_pretend_to_be_each_interface(choice: str, profile: str | None, present: bool) -> None:
    demo = DemoInterface()
    demo.act("interface.demo_profile", {"profile": choice})
    state = demo.state()
    assert state["profile"] == profile and state["present"] is present and state["error"] is None


def test_demo_rejects_an_unknown_profile() -> None:
    with pytest.raises(ActionError):
        DemoInterface().act("interface.demo_profile", {"profile": "behringer"})
    with pytest.raises(ActionError):
        DemoInterface().act("interface.demo_profile", {})


def test_demo_phantom_needs_confirm() -> None:
    demo = DemoInterface()
    with pytest.raises(ActionError, match="confirmation"):
        demo.act("interface.set", {"id": "phantom.1", "value": True})
    demo.act("interface.set", {"id": "phantom.1", "value": True, "confirm": True})
    assert next(w for w in demo.state()["controls"] if w["id"] == "phantom.1")["value"] is True


# -- rig -------------------------------------------------------------------------------------------------------


def test_rig_snapshot_and_routing_include_the_interface() -> None:
    rig = Rig.demo()
    rig.start()
    try:
        assert rig.snapshot()["interface"]["present"] is True
        rig.act("interface.set", {"id": "air.1", "value": 1})
        assert next(w for w in rig.snapshot()["interface"]["controls"] if w["id"] == "air.1")["value"] == 1
    finally:
        rig.stop()


def test_rig_without_an_interface_still_has_the_section() -> None:
    demo = Rig.demo()
    rig = Rig(demo.audio, demo.video, demo.studio, demo=True)
    assert rig.snapshot()["interface"]["present"] is False
    with pytest.raises(ActionError) as err:
        rig.act("interface.rescan", {})
    assert err.value.status == 400
