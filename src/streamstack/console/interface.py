"""The audio interface on the desk (Focusrite Scarlett or Elgato Wave XLR), as the Console sees it.

`InterfaceController` finds the interface by USB id, builds the driver of its profile and keeps a cached state the
frontend renders generically (a list of widgets). Hardware I/O (amixer, a USB control transfer) happens only in the
polling thread and in actions, under one lock, so `state()` is instant. `DemoInterface` swaps in simulations.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from streamstack.console.errors import ActionError
from streamstack.hardware import sim, usb
from streamstack.hardware.alsa import Mixer, MixerError
from streamstack.hardware.profiles import BY_ID, Driver, DriverFactory, Profile, known_vendor, profile_for, select
from streamstack.hardware.scarlett import Scarlett
from streamstack.hardware.usb import UsbDevice
from streamstack.hardware.wavexlr import WaveXlr, XlrError

Notify = Callable[[str, str], None]
Scan = Callable[[], list[UsbDevice]]

POLL_SECONDS = 2.0
POLL_SECONDS_USB = 5.0  # a vendor control transfer per poll is gentler when spaced out
POLL_SECONDS_BUSY = 0.5  # while Autogain runs
MAX_ID = 40


def _no_notify(level: str, text: str) -> None:
    return None


def empty_state(setting: str = "auto") -> dict[str, Any]:
    return {
        "present": False,
        "checked": False,
        "setting": setting,
        "profile": None,
        "label": None,
        "usb": None,
        "card": None,
        "mode": None,
        "notice": None,
        "error": None,
        "info": [],
        "controls": [],
        "capabilities": {},
        "unsupported": [],
        "devices": [],
        "software_gain": True,
        "demo_profiles": [],
    }


def default_factory(cfg: dict[str, str], call_active: Callable[[], bool] | None = None) -> DriverFactory:
    def build(profile: Profile, device: UsbDevice) -> Driver:
        if profile.family == "scarlett":
            if device.card is None:
                raise MixerError("The kernel has not created a sound card for this interface yet.")
            return Scarlett(profile, device, Mixer(device.card))
        if profile.family == "wave-xlr":
            mode = cfg.get("STREAMSTACK_WAVE_XLR_USB", "off").strip().lower()
            return WaveXlr(profile, device, mode=mode, call_active=call_active)
        raise ActionError(f"No driver for {profile.label}.", 409)

    return build


class InterfaceController:
    def __init__(
        self,
        cfg: dict[str, str] | None = None,
        scan: Scan | None = None,
        factory: DriverFactory | None = None,
        call_active: Callable[[], bool] | None = None,
    ) -> None:
        self.cfg = cfg or {}
        self.notify: Notify = _no_notify
        self._setting = self.cfg.get("STREAMSTACK_INTERFACE", "auto").strip().lower() or "auto"
        self._scan = scan or usb.scan
        self._factory = factory or default_factory(self.cfg, call_active)
        self._io = threading.Lock()  # serialises hardware I/O and driver changes
        self._state_lock = threading.Lock()
        self._state = empty_state(self._setting)
        self._driver: Driver | None = None
        self._driver_key: tuple[str, int, int] | None = None
        self._was_present: bool | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="interface", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:  # the poller must outlive any one bad read
                self._set_error(f"Interface poll failed: {type(exc).__name__}: {exc}")
            self._wake.wait(self._interval())
            self._wake.clear()

    def _interval(self) -> float:
        if self._busy:
            return POLL_SECONDS_BUSY
        with self._state_lock:
            return POLL_SECONDS_USB if self._state["usb"] and self._state["profile"] == "wave-xlr" else POLL_SECONDS

    # -- state ----------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def _publish(self, state: dict[str, Any]) -> None:
        with self._state_lock:
            self._state = state

    def _set_error(self, message: str) -> None:
        with self._state_lock:
            self._state = {**self._state, "error": message, "checked": True}

    def refresh(self) -> None:
        """Probe the bus, build or drop the driver, read the device. Never raises for an expected failure."""
        with self._io:
            state = empty_state(self._setting)
            state["checked"] = True
            state["demo_profiles"] = self._state.get("demo_profiles", [])
            try:
                devices = self._scan()
            except OSError as exc:
                state["error"] = f"Could not list USB devices: {exc}"
                self._publish(state)
                return
            state["devices"] = [
                {"usb": d.usb_id, "name": d.name, "profile": p.id if (p := profile_for(d)) else None}
                for d in devices
                if d.card is not None
            ]
            chosen = select(devices, self._setting)
            if chosen is None:
                self._driver, self._driver_key = None, None
                other = next((d for d in devices if known_vendor(d)), None)
                if self._setting == "off":
                    state["notice"] = "Interface control is off (STREAMSTACK_INTERFACE=off)."
                elif other:
                    state["notice"] = f"{other.name or other.usb_id} has no StreamStack profile yet."
                self._busy = False
                self._publish(state)
                self._announce(False, None)
                return
            profile, device = chosen
            state.update(profile=profile.id, label=profile.label, usb=device.usb_id, card=device.card, present=True)
            key = (profile.id, device.bus, device.address)
            try:
                if self._driver is None or self._driver_key != key:
                    self._driver, self._driver_key = self._factory(profile, device), key
                read = self._driver.read()
            except (MixerError, XlrError, ActionError) as exc:
                state["error"] = getattr(exc, "message", None) or str(exc)
                self._driver = None  # rebuilt on the next poll
            else:
                for field in ("controls", "info", "capabilities", "unsupported", "notice", "mode"):
                    if read.get(field) is not None:
                        state[field] = read[field]
            self._busy = any(w.get("kind") == "action" and w.get("value") is True for w in state["controls"])
            self._publish(state)
            self._announce(True, profile)

    def _announce(self, present: bool, profile: Profile | None) -> None:
        """A toast when the interface appears or goes away after startup (not for what was there at launch)."""
        before, self._was_present = self._was_present, present
        if before is None or before == present:
            return
        if present and profile:
            self.notify("ok", f"{profile.label} connected.")
        else:
            self.notify("warn", "The audio interface was unplugged.")

    # -- actions --------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "interface.rescan":
            self.refresh()
            return None
        if action == "interface.set":
            return self._set(params)
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _set(self, params: dict[str, Any]) -> str | None:
        control_id = params.get("id")
        if not isinstance(control_id, str) or not control_id or len(control_id) > MAX_ID:
            raise ActionError("Say which control to change.", 400)
        if "value" not in params:
            raise ActionError("Say the new value.", 400)
        confirm = params.get("confirm", False)
        if not isinstance(confirm, bool):
            raise ActionError("confirm must be true or false.", 400)
        with self._io:
            driver = self._driver
            if driver is None:
                raise ActionError("No audio interface is connected.", 409)
            try:
                message = driver.apply(control_id, params["value"], confirm)
            except (MixerError, XlrError) as exc:
                raise ActionError(str(exc), 409) from exc
        self.refresh()
        self._wake.set()
        return message


class DemoInterface(InterfaceController):
    """A simulated interface: a Scarlett 2i2 (Gen 4 by default), the Gen 3 or a Wave XLR, switchable from the UI."""

    PROFILES: ClassVar[dict[str, str]] = {
        "scarlett-2i2-gen4": "gen4",
        "scarlett-2i2-gen3": "gen3",
        "wave-xlr": "xlr",
        "none": "none",
    }

    def __init__(self, profile: str = "scarlett-2i2-gen4", clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__({"STREAMSTACK_INTERFACE": "auto"}, scan=self._demo_scan)
        self._factory = self._demo_factory
        self._clock = clock
        self._proc = Path(tempfile.mkdtemp(prefix="streamstack-demo-proc-"))
        sim.sim_proc(self._proc, card=7)
        self._mixers: dict[str, sim.SimMixer] = {}
        self._xlr = sim.SimXlr()
        self._demo = "none"
        self._choose(profile)
        self.refresh()

    def _choose(self, profile: str) -> None:
        if profile not in self.PROFILES:
            raise ActionError(f"Unknown demo interface {profile!r}.", 400)
        self._demo = self.PROFILES[profile]

    def _demo_scan(self) -> list[UsbDevice]:
        if self._demo == "none":
            return []
        return [sim.XLR_DEVICE if self._demo == "xlr" else sim.SCARLETT_DEVICES[self._demo]]

    def _demo_factory(self, profile: Profile, device: UsbDevice) -> Driver:
        if profile.family == "scarlett":
            mixer = self._mixers.setdefault(self._demo, sim.SimMixer(self._demo, clock=self._clock))
            return Scarlett(profile, device, mixer, proc_root=self._proc)
        return WaveXlr(profile, device, mode="write", opener=self._xlr.opener, proc_root=self._proc)

    def refresh(self) -> None:
        super().refresh()
        with self._state_lock:
            self._state = {**self._state, "demo_profiles": [{"id": k, "label": self._label(k)} for k in self.PROFILES]}

    @staticmethod
    def _label(key: str) -> str:
        return "Nothing connected" if key == "none" else BY_ID[key].label

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "interface.demo_profile":
            profile = params.get("profile")
            if not isinstance(profile, str):
                raise ActionError("Say which demo interface.", 400)
            self._choose(profile)
            self.refresh()
            return None
        return super().act(action, params)
