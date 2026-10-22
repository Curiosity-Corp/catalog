"""The catalogue of supported audio interfaces: a USB id selects a profile, a profile builds its driver.

Adding hardware means adding a `Profile` here and a driver module; the Console renders whatever controls and
read-outs a driver reports, so it never learns about a particular interface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .usb import UsbDevice

FOCUSRITE = 0x1235
ELGATO = 0x0FD9


class Driver(Protocol):
    """What the Console needs from an interface driver. `read` and `apply` may block; callers serialise them."""

    def read(self) -> dict[str, Any]:
        """The device's current state: {"controls": [...], "info": [...], "notice": str | None, ...}."""

    def apply(self, control_id: str, value: Any, confirm: bool) -> str | None:
        """Change one control; returns a short message for a toast. Raises ActionError."""


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    vid: int
    pid: int
    family: str  # "scarlett" | "wave-xlr"
    generation: str = ""
    summary: str = ""

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


# Focusrite ids are the Scarlett 2i2 entries of the kernel's scarlett2 driver (sound/usb/mixer_scarlett2.c,
# scarlett2_devices[]): 0x8210 = 2i2 Gen 3, 0x8219 = 2i2 Gen 4. The Wave XLR id comes from the device itself
# (`lsusb`: 0fd9:007d).
PROFILES: tuple[Profile, ...] = (
    Profile(
        "scarlett-2i2-gen4",
        "Focusrite Scarlett 2i2 (4th Gen)",
        FOCUSRITE,
        0x8219,
        "scarlett",
        "gen4",
        "Gain, Autogain, Air presence and drive, 48V, Safe, direct monitor",
    ),
    Profile(
        "scarlett-2i2-gen3",
        "Focusrite Scarlett 2i2 (3rd Gen)",
        FOCUSRITE,
        0x8210,
        "scarlett",
        "gen3",
        "Air, 48V, Line/Inst, direct monitor (the gain knobs are analogue)",
    ),
    Profile(
        "wave-xlr",
        "Elgato Wave XLR",
        ELGATO,
        0x007D,
        "wave-xlr",
        "",
        "Gain, mute, 48V, headphone volume and low impedance over the vendor USB protocol",
    ),
)

BY_ID = {p.id: p for p in PROFILES}


def profile_for(device: UsbDevice) -> Profile | None:
    for profile in PROFILES:
        if (profile.vid, profile.pid) == (device.vid, device.pid):
            return profile
    return None


def known_vendor(device: UsbDevice) -> bool:
    """A sound interface from a vendor with a profile, but a model without one."""
    return device.card is not None and device.vid in {p.vid for p in PROFILES} and profile_for(device) is None


def select(devices: list[UsbDevice], wanted: str = "auto") -> tuple[Profile, UsbDevice] | None:
    """The interface to drive. `wanted` is "auto" (the first supported one, Focusrite before Elgato), a profile
    id such as "wave-xlr", a USB id such as "1235:8219", or "off"."""
    wanted = (wanted or "auto").strip().lower()
    if wanted == "off":
        return None
    candidates = [(p, d) for d in devices if (p := profile_for(d)) is not None]
    if wanted != "auto":
        candidates = [(p, d) for p, d in candidates if wanted in (p.id, p.usb_id)]
    order = {p.id: i for i, p in enumerate(PROFILES)}
    candidates.sort(key=lambda pd: (pd[0].family != "scarlett", order[pd[0].id], pd[1].bus, pd[1].address))
    return candidates[0] if candidates else None


DriverFactory = Callable[[Profile, UsbDevice], Driver]
