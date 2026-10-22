"""The real devices behind the director's hardware actuators (see `director.devices` for the actuators).

Each adapter wraps a module that already drives the hardware for the Console, so the director speaks the same
protocols and honours the same safety rules, and adds the one thing the director needs on top: a narrow door.

* `BridgeCanon` talks to the running Canon bridge over its local socket. It forwards only `exposure_comp`,
  `kelvin` and `whitebalance`; there is no way to reach focus, ISO, shutter, aperture or the picture style from it.
* `HidLights` finds Litra lights by their USB vendor and product ids (sysfs only) and opens one only for the
  moment of a read or a write. It sends brightness and colour temperature, never power.
* `HardwareInterface` finds the audio interface by USB id, builds the Console's own driver for it and exposes only
  gain sliders. Anything else (48V, Autogain, Air, Safe, Pad, Link, Line/Inst) is not reachable, and a request for
  a control id that is not a gain is refused before it reaches the driver.

Nothing in this module is imported by tests against real hardware: every constructor takes the functions that
touch the machine, so tests (and the demo) pass fakes. `hardware_actuators` is the one place that picks the real
ones.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from streamstack import config, lighting
from streamstack.canon import ipc
from streamstack.console.errors import ActionError
from streamstack.director import devices
from streamstack.director.actuators import Actuator, ActuatorError
from streamstack.hardware import usb
from streamstack.hardware.alsa import MixerError
from streamstack.hardware.profiles import Driver, DriverFactory, select
from streamstack.hardware.wavexlr import XlrError

Remember = Callable[[str, str, object], None]

CANON_CONTROLS = ("exposure_comp", "kelvin", "whitebalance")
GAIN_ID = re.compile(r"gain(\.\d+)?")
LABEL_TTL = 3.0


# ---------------------------------------------------------------------------
# Canon
# ---------------------------------------------------------------------------


class BridgeCanon:
    """The Canon bridge over its Unix socket (`streamstack.canon.ipc`)."""

    def __init__(self, request: Callable[..., dict[str, Any]] = ipc.request) -> None:
        self._request = request

    def status(self) -> dict | None:
        try:
            answer = self._request({"op": "status"}, timeout=3.0)
        except ipc.BridgeUnavailable:
            return None
        status = answer.get("status")
        return status if answer.get("ok") and isinstance(status, dict) else None

    def set(self, control: str, value: object) -> None:
        if control not in CANON_CONTROLS:
            raise ActuatorError(f"the director may not change the camera's {control!r}")
        try:
            answer = self._request({"op": "set", "id": control, "value": value}, timeout=15.0)
        except ipc.BridgeUnavailable as exc:
            raise ActuatorError(f"the Canon bridge is not running ({exc})") from exc
        if not answer.get("ok"):
            raise ActuatorError(str(answer.get("error") or "the camera refused the change"))
        if answer.get("applied") is False:
            raise ActuatorError(str(answer.get("message") or f"the camera kept its {control}"))


# ---------------------------------------------------------------------------
# Litra
# ---------------------------------------------------------------------------


class HidLights:
    """Litra lights found through sysfs and opened per call. `scan` and `opener` are injectable for tests."""

    def __init__(
        self,
        scan: Callable[[], list[lighting.Found]] = lighting.discover,
        opener: Callable[[lighting.Found], lighting.Transport] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scan = scan
        self._open = opener or (lambda found: lighting.HidrawTransport(found.node))
        self._clock = clock
        self._seen: tuple[float, dict[str, lighting.Found]] | None = None

    def _labelled(self) -> dict[str, lighting.Found]:
        """Found lights by label: the model name, numbered when several share one (as the Lighting page does)."""
        now = self._clock()
        if self._seen and now - self._seen[0] < LABEL_TTL:
            return self._seen[1]
        try:
            found = sorted(self._scan(), key=lambda f: f.id)
        except lighting.LightingError:
            found = []
        counts: dict[str, int] = {}
        for light in found:
            counts[light.model.name] = counts.get(light.model.name, 0) + 1
        seen: dict[str, int] = {}
        labelled: dict[str, lighting.Found] = {}
        for light in found:
            name = light.model.name
            seen[name] = seen.get(name, 0) + 1
            labelled[f"{name} {seen[name]}" if counts[name] > 1 else name] = light
        self._seen = (now, labelled)
        return labelled

    def lights(self) -> list[devices.LightInfo]:
        return [
            devices.LightInfo(label, f.model.name, f.model.lumen_min, f.model.lumen_max)
            for label, f in self._labelled().items()
        ]

    def _device(self, label: str) -> lighting.Litra:
        found = self._labelled().get(label)
        if found is None:
            raise ActuatorError(f"{label} is not connected")
        try:
            return lighting.Litra(found, self._open(found))
        except lighting.LightingError as exc:
            raise ActuatorError(f"{label}: {exc.message}") from exc

    def read(self, label: str) -> devices.LightState:
        device = self._device(label)
        try:
            return devices.LightState(device.read_power(), device.read_lumen(), device.read_kelvin())
        except lighting.LightingError as exc:
            raise ActuatorError(f"{label}: {exc.message}") from exc
        finally:
            device.close()

    def write(self, label: str, lumen: int | None, kelvin: int | None) -> None:
        device = self._device(label)
        try:
            if kelvin is not None:
                device.set_kelvin(kelvin)
            if lumen is not None:
                device.set_lumen(lumen)
        except lighting.LightingError as exc:
            raise ActuatorError(f"{label}: {exc.message}") from exc
        finally:
            device.close()


# ---------------------------------------------------------------------------
# Audio interface
# ---------------------------------------------------------------------------


class HardwareInterface:
    """The audio interface selected by STREAMSTACK_INTERFACE, as gain sliders only."""

    def __init__(
        self,
        cfg: dict[str, str],
        scan: Callable[[], list[usb.UsbDevice]] = usb.scan,
        factory: DriverFactory | None = None,
    ) -> None:
        self._cfg = cfg
        self._scan = scan
        self._factory = factory
        self._driver: Driver | None = None
        self._key: tuple[str, int, int] | None = None

    def _build(self) -> DriverFactory:
        if self._factory is None:
            from streamstack.console.interface import default_factory

            self._factory = default_factory(self._cfg)
        return self._factory

    def inspect(self) -> devices.InterfaceReport:
        setting = (self._cfg.get("STREAMSTACK_INTERFACE") or "auto").strip().lower()
        if setting == "off":
            return devices.InterfaceReport(False, note="interface control is off (STREAMSTACK_INTERFACE=off)")
        try:
            chosen = select(self._scan(), setting)
        except OSError as exc:
            raise ActuatorError(f"could not list USB devices: {exc}") from exc
        if chosen is None:
            self._driver = self._key = None
            return devices.InterfaceReport(False)
        profile, device = chosen
        key = (profile.id, device.bus, device.address)
        try:
            if self._driver is None or self._key != key:
                self._driver, self._key = self._build()(profile, device), key
            read = self._driver.read()
        except (MixerError, XlrError) as exc:
            self._driver = None
            raise ActuatorError(f"{profile.label}: {exc}") from exc
        except ActionError as exc:
            self._driver = None
            raise ActuatorError(f"{profile.label}: {exc.message}") from exc
        controls = tuple(
            devices.GainControl(
                str(w["id"]),
                str(w.get("label", "Gain")),
                float(w["value"]),
                float(w["min"]),
                float(w["max"]),
                float(w.get("step") or 1.0),
                str(w.get("readonly") or ""),
            )
            for w in read.get("controls", [])
            if w.get("kind") == "slider" and GAIN_ID.fullmatch(str(w.get("id", "")))
        )
        note = ""
        if not controls:
            if read.get("mode") == "off":
                note = (
                    f"{profile.label} hardware control is off (STREAMSTACK_WAVE_XLR_USB=off); using the software gain"
                )
            elif profile.family == "wave-xlr":
                note = f"{profile.label}: {str(read.get('notice') or 'no gain control')[:120]}"
            else:
                note = f"{profile.label} has analogue gain knobs, no software-settable gain"
        return devices.InterfaceReport(True, profile.label, controls, note)

    def set_gain(self, control_id: str, db: float) -> None:
        if not GAIN_ID.fullmatch(control_id):
            raise ActuatorError(f"the director may only change a gain, not {control_id!r}")
        if self._driver is None:
            self.inspect()
        if self._driver is None:
            raise ActuatorError("no audio interface to set")
        try:
            self._driver.apply(control_id, db, False)
        except (MixerError, XlrError) as exc:
            raise ActuatorError(str(exc)) from exc
        except ActionError as exc:
            raise ActuatorError(exc.message) from exc


# ---------------------------------------------------------------------------
# The set the director registers
# ---------------------------------------------------------------------------


def hardware_actuators(
    cfg: dict[str, str], remember: Remember, start_gain: Callable[[str], float | None]
) -> list[Actuator]:
    """The hardware actuators the settings allow, bound to real devices (each unavailable until its device shows)."""
    found: list[Actuator] = []
    if config.truthy(cfg.get("STREAMSTACK_AUTO_CANON", "true")):
        found.append(devices.CanonActuator(BridgeCanon(), remember))
    if config.truthy(cfg.get("STREAMSTACK_AUTO_LIGHTS", "true")):
        found.append(devices.LitraActuator(HidLights(), remember))
    if config.truthy(cfg.get("STREAMSTACK_AUTO_HARDWARE_GAIN", "true")):
        found.append(
            devices.InterfaceGainActuator(
                HardwareInterface(cfg), remember, start_gain, cfg.get("STREAMSTACK_AUTO_INTERFACE_INPUT", "auto")
            )
        )
    return found
