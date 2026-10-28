"""Elgato Wave XLR (USB 0fd9:007d): read-back and control over the device's vendor USB control protocol.

Provenance, honestly: Elgato publishes no protocol and the kernel has no quirk for this device, so everything here
comes from community reverse engineering that agrees across projects but was NOT verified on hardware by this
project (the host this was written on may be live on a call, so the device was never touched):

* rikkichy/openwave (MIT), crates/openwave-core/src/profiles.rs and docs/protocol.md: transport, block layout and
  field offsets used below. https://github.com/rikkichy/openwave
* philipredstone/elgato-wave-linux (MIT) and RamaAditya49/cadiswave: independently use the same wIndex 0x3303 trick
  and report a 0..75 dB gain range and that the firmware forgets the gain when it loses power.

Transport: USB class control transfers on endpoint 0, bmRequestType 0xA1 (read) / 0x21 (write), bRequest 0x85 / 0x05,
wValue selecting a block (0 = 34 byte config, 0x0A = 51 byte device info), wIndex 0x3303. Wave Link addresses
interface 0 (0x3300), which the kernel's snd-usb-audio owns; 0x3303 keeps the 0x33 prefix the firmware checks while
the kernel sees interface 3, which is a vendor interface (class ff/f0, no endpoints, no driver bound) and is claimed
only for the length of one transfer. The audio driver is never detached. The device serves vendor transfers to one
program at a time, so Wave Link, OpenWave or another daemon running at the same time can make reads fail.

Config block (all little endian): u16 gain at 0 (raw / 256 dB), byte 4 mute, byte 6 48V phantom, i16 headphone level
at 9 (raw / 256 dB, 0 = unity), byte 14 knob mode (2 = headphones), byte 33 low impedance mode. Every other byte is
unknown and is written back unchanged. Writes are read-modify-write of the whole block, followed by a read-back.

What has no documented register, so is NOT offered: Clipguard and the low-cut filter. (Elgato's Wave Link runs them
host-side or in firmware without a published control; OpenWave's "low cut" is a software DSP stage.) The voice chain's
high-pass stage of the StreamStack voice chain is the low-cut available here.

This host's sysfs also shows why the old "HID" idea does not apply: the Wave XLR has no HID interface and so no
/dev/hidraw node; its five interfaces are audio control, audio streaming x2, a vendor interface (3) and a DFU
interface (4).

Gated by STREAMSTACK_WAVE_XLR_USB: `off` (default, the device is never opened), `read` (state read-back only) or
`write` (controls enabled).
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..console.errors import ActionError
from . import alsa
from .profiles import Profile
from .usb import UsbDevice
from .widgets import Widget, readout, slider, switch

MODES = ("off", "read", "write")
REQUEST_READ, REQUEST_WRITE = 0x85, 0x05
RT_CLASS_IN, RT_CLASS_OUT = 0xA1, 0x21
WINDEX = 0x3303
WVALUE_CONFIG, WVALUE_DEVINFO = 0x0000, 0x000A
CONFIG_LEN, DEVINFO_LEN = 34, 51
TIMEOUT_MS = 1000

OFF_GAIN, OFF_MUTE, OFF_PHANTOM, OFF_HP, OFF_KNOB, OFF_LOW_Z = 0, 4, 6, 9, 14, 33
GAIN_MAX_DB = 75.0  # openwave allows 0x5000 (80 dB); the other projects and the product page say 75, so 75 it is
HP_MIN_DB = -60.0
CACHE_TOLERANCE_DB = 6.0  # ALSA's dB scale is the driver's guess; only a clear disagreement is worth showing
GAIN_TOLERANCE = 128  # raw units (0.5 dB) the firmware may round a written level by

GROUP = "Hardware"
UNSUPPORTED = [
    {"label": "Clipguard", "reason": "No documented register: Elgato runs it inside Wave Link or the firmware."},
    {"label": "Low cut", "reason": "No documented register. Use the high-pass in the voice chain."},
]


class XlrError(Exception):
    """A vendor transfer that failed, with a message a person can act on."""


class Transport(Protocol):
    def read(self, wvalue: int, length: int) -> bytes: ...
    def write(self, wvalue: int, data: bytes) -> None: ...
    def close(self) -> None: ...


# -- codec ------------------------------------------------------------------------------------------------------


def parse_config(raw: bytes) -> dict[str, Any]:
    if len(raw) != CONFIG_LEN:
        raise XlrError(f"The Wave XLR sent a {len(raw)} byte config, expected {CONFIG_LEN}.")
    (gain,) = struct.unpack_from("<H", raw, OFF_GAIN)
    (headphones,) = struct.unpack_from("<h", raw, OFF_HP)
    return {
        "gain_db": gain / 256,
        "muted": raw[OFF_MUTE] != 0,
        "phantom": raw[OFF_PHANTOM] != 0,
        "headphone_db": headphones / 256,
        "knob": "Headphones" if raw[OFF_KNOB] == 2 else "Gain",
        "low_z": raw[OFF_LOW_Z] != 0,
    }


def patch_config(
    raw: bytes,
    *,
    gain_db: float | None = None,
    muted: bool | None = None,
    phantom: bool | None = None,
    headphone_db: float | None = None,
    low_z: bool | None = None,
) -> bytes:
    """The config with only the named fields changed (every other byte, known or not, is kept)."""
    if len(raw) != CONFIG_LEN:
        raise XlrError(f"The Wave XLR sent a {len(raw)} byte config, expected {CONFIG_LEN}.")
    out = bytearray(raw)
    if gain_db is not None:
        struct.pack_into("<H", out, OFF_GAIN, round(max(0.0, min(GAIN_MAX_DB, gain_db)) * 256))
    if muted is not None:
        out[OFF_MUTE] = int(muted)
    if phantom is not None:
        out[OFF_PHANTOM] = int(phantom)
    if headphone_db is not None:
        struct.pack_into("<h", out, OFF_HP, round(max(HP_MIN_DB, min(0.0, headphone_db)) * 256))
    if low_z is not None:
        out[OFF_LOW_Z] = int(low_z)
    return bytes(out)


def parse_devinfo(raw: bytes) -> dict[str, str]:
    if len(raw) != DEVINFO_LEN:
        raise XlrError(f"The Wave XLR sent a {len(raw)} byte info block, expected {DEVINFO_LEN}.")
    return {"api": f"{raw[0]}.{raw[1]}", "firmware": f"{raw[6]}.{raw[7]}.{raw[8]}"}


# -- transport: usbfs control transfers --------------------------------------------------------------------------


class _CtrlTransfer(ctypes.Structure):
    _fields_ = (
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    )


def _iowr(kind: str, number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord(kind) << 8) | number


USBDEVFS_CONTROL = _iowr("U", 0, ctypes.sizeof(_CtrlTransfer))
_ERRORS = {
    errno.EACCES: "No permission to open {node}. Give your user access with a udev rule (docs/interfaces.md).",
    errno.EPERM: "No permission to open {node}. Give your user access with a udev rule (docs/interfaces.md).",
    errno.ENOENT: "{node} is gone: the Wave XLR was unplugged.",
    errno.ENODEV: "The Wave XLR was unplugged.",
    errno.EBUSY: "Another program holds the Wave XLR's control interface (Wave Link, OpenWave?). Close it and retry.",
    errno.EPIPE: "The Wave XLR refused the request.",
    errno.ETIMEDOUT: "The Wave XLR did not answer in time.",
    errno.EIO: "The Wave XLR did not accept the transfer; another program may be talking to it.",
}


class UsbfsTransport:
    """One open usbfs node. Vendor control transfers only; the audio driver is never touched."""

    def __init__(self, node: str) -> None:
        self.node = node
        try:
            self._fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        except OSError as exc:
            raise XlrError(self._explain(exc.errno)) from exc
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        self._libc.ioctl.restype = ctypes.c_int

    def _explain(self, code: int | None) -> str:
        template = _ERRORS.get(code or 0, "USB error {code} talking to {node}.")
        return template.format(node=self.node, code=code)

    def _transfer(self, request_type: int, request: int, wvalue: int, data: bytes | None, length: int) -> bytes:
        buffer = ctypes.create_string_buffer(data if data is not None else b"\0" * length, length)
        xfer = _CtrlTransfer(request_type, request, wvalue, WINDEX, length, TIMEOUT_MS, ctypes.addressof(buffer))
        done = self._libc.ioctl(self._fd, USBDEVFS_CONTROL, ctypes.byref(xfer))
        if done < 0:
            raise XlrError(self._explain(ctypes.get_errno()))
        return buffer.raw[:done]

    def read(self, wvalue: int, length: int) -> bytes:
        data = self._transfer(RT_CLASS_IN, REQUEST_READ, wvalue, None, length)
        if len(data) != length:
            raise XlrError(f"The Wave XLR answered {len(data)} bytes for block {wvalue:#x}, expected {length}.")
        return data

    def write(self, wvalue: int, data: bytes) -> None:
        done = len(self._transfer(RT_CLASS_OUT, REQUEST_WRITE, wvalue, data, len(data)))
        if done != len(data):
            raise XlrError(f"The Wave XLR accepted {done} of {len(data)} bytes.")

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass  # already closed; nothing else holds the descriptor


Opener = Callable[[UsbDevice], Transport]


def open_usbfs(device: UsbDevice) -> Transport:
    return UsbfsTransport(device.node)


# -- driver ------------------------------------------------------------------------------------------------------


class WaveXlr:
    def __init__(
        self,
        profile: Profile,
        device: UsbDevice,
        mode: str = "off",
        opener: Opener = open_usbfs,
        proc_root: Path = Path("/proc"),
        call_active: Callable[[], bool] | None = None,
        mixer: Callable[[int], alsa.MixerLike] | None = None,
    ) -> None:
        self.profile, self.device = profile, device
        self._call_active = call_active
        self._mixer = mixer or (lambda card: alsa.Mixer(card))
        self.mode = mode if mode in MODES else "off"
        self._open = opener
        self.proc_root = proc_root
        self._firmware: dict[str, str] | None = None

    @staticmethod
    def _capabilities() -> dict[str, bool]:
        return {
            "input_gain": True,
            "mute": True,
            "phantom": True,
            "headphone_volume": True,
            "low_impedance": True,
            "autogain": False,
            "air": False,
            "safe": False,
            "direct_monitor": False,
            "clipguard": False,
            "low_cut": False,
            "meters": False,
            "sample_rate": True,
            "firmware": True,
        }

    def read(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "controls": [],
            "info": [],
            "notice": None,
            "capabilities": self._capabilities(),
            "unsupported": UNSUPPORTED,
            "mode": self.mode,
        }
        info = self._rate_info()
        if self.mode == "off":
            base["notice"] = (
                "Hardware control is off, so StreamStack never opens the Wave XLR's control interface. The gain knob "
                "works as always and the Audio page's software gain is the fallback. To read and set the hardware "
                "gain, mute and 48V from here, set STREAMSTACK_WAVE_XLR_USB=read (or =write); see docs/interfaces.md."
            )
            base["info"] = info
            return base
        try:
            transport = self._open(self.device)
        except XlrError as exc:
            base["notice"], base["info"] = str(exc), info
            return base
        try:
            config = parse_config(transport.read(WVALUE_CONFIG, CONFIG_LEN))
            if self._firmware is None:
                self._firmware = parse_devinfo(transport.read(WVALUE_DEVINFO, DEVINFO_LEN))
        except XlrError as exc:
            base["notice"], base["info"] = str(exc), info
            return base
        finally:
            transport.close()
        locked = "" if self.mode == "write" else "Hardware writes are off (STREAMSTACK_WAVE_XLR_USB=write)"
        base["controls"] = self._widgets(config, locked)
        base["info"] = [
            readout("knob", "Knob controls", GROUP, config["knob"], hint="What the front dial changes right now"),
            *self._cache_info(config),
            readout("firmware", "Firmware", "Device", f"{self._firmware['firmware']} (API {self._firmware['api']})"),
            *info,
        ]
        return base

    def _cache_info(self, config: dict[str, Any]) -> list[Widget]:
        """Say so when the kernel's cached capture gain disagrees with the device's own block.

        The kernel caches `Mic Capture Volume` and never reads it back, so ALSA can show a gain the device does not
        have (74 dB cached against 48 dB in the device block was seen on another unit). The device block read above is
        the truth; this is only shown when the mixer states a dB scale and the two differ by more than a few dB.
        """
        if self.device.card is None:
            return []
        try:
            mixer = self._mixer(self.device.card)
            if not mixer.available():
                return []
            control = mixer.controls().get("Mic Capture Volume")
        except alsa.MixerError:
            return []
        if control is None or control.db_range is None or not isinstance(control.value, int):
            return []
        if control.minimum is None or control.maximum is None or control.maximum <= control.minimum:
            return []
        low, high = control.db_range
        cached = low + (control.value - control.minimum) / (control.maximum - control.minimum) * (high - low)
        if abs(cached - config["gain_db"]) <= CACHE_TOLERANCE_DB:
            return []
        return [
            readout(
                "alsa_cache",
                "Kernel mixer gain",
                "Device",
                f"{cached:.0f} dB (the device says {config['gain_db']:.0f} dB)",
                state="warn",
                hint="The kernel caches this value and never reads it back; the device's own value above is the real "
                "one. `streamstack mic-revive` writes it again.",
            )
        ]

    def _rate_info(self) -> list[Widget]:
        if self.device.card is None:
            return []
        status = alsa.capture_status(self.device.card, self.proc_root)
        if not status or "rate" not in status:
            return []
        text = f"{status['rate'] / 1000:g} kHz"
        if "period" in status:
            text += f", {status['period']} frames per period ({1000 * status['period'] / status['rate']:.1f} ms)"
        return [readout("rate", "Capture", "Device", text)]

    @staticmethod
    def _widgets(config: dict[str, Any], locked: str) -> list[Widget]:
        return [
            slider(
                "gain",
                "Mic gain",
                GROUP,
                round(config["gain_db"] * 2) / 2,
                0.0,
                GAIN_MAX_DB,
                0.5,
                "dB",
                hint="The Wave XLR's own preamp gain (the front dial changes the same value)",
                readonly=locked,
            ),
            switch(
                "mute", "Mute", GROUP, config["muted"], hint="The hardware mute, like tapping the dial", readonly=locked
            ),
            switch(
                "phantom",
                "48V phantom power",
                GROUP,
                config["phantom"],
                danger=True,
                readonly=locked,
                hint="Powers condenser microphones. Never with a ribbon microphone connected",
            ),
            slider(
                "headphones",
                "Headphone volume",
                GROUP,
                max(HP_MIN_DB, round(config["headphone_db"])),
                HP_MIN_DB,
                0.0,
                1.0,
                "dB",
                hint="The headphone output of the Wave XLR",
                readonly=locked,
            ),
            switch(
                "low_z",
                "Low impedance headphones",
                GROUP,
                config["low_z"],
                readonly=locked,
                hint="For low impedance headphones; leave it off for high impedance ones",
            ),
        ]

    def apply(self, control_id: str, value: Any, confirm: bool) -> str | None:
        if self.mode != "write":
            raise ActionError(
                "Hardware writes to the Wave XLR are off. Set STREAMSTACK_WAVE_XLR_USB=write to enable them.", 409
            )
        change, check = self._change(control_id, value)
        if control_id == "phantom" and value and not confirm:
            raise ActionError("48V phantom power needs a deliberate confirmation.", 400)
        if control_id == "phantom":
            self._refuse_during_call()
        try:
            transport = self._open(self.device)
        except XlrError as exc:
            raise ActionError(str(exc), 409) from exc
        try:
            before = transport.read(WVALUE_CONFIG, CONFIG_LEN)
            patched = patch_config(before, **change)
            transport.write(WVALUE_CONFIG, patched)
            after = parse_config(transport.read(WVALUE_CONFIG, CONFIG_LEN))
        except XlrError as exc:
            raise ActionError(str(exc), 409) from exc
        finally:
            transport.close()
        if not check(after):
            raise ActionError("The Wave XLR did not keep that setting (another program may have changed it).", 409)
        return None

    def _refuse_during_call(self) -> None:
        """Phantom power thumps and can silence a condenser microphone: never while a call may be live.

        With no call detector wired the write is allowed (the demo rig); a detector that fails counts as a call."""
        if self._call_active is None:
            return
        try:
            active = self._call_active()
        except Exception as exc:  # any detector failure must stop a phantom switch, not let it through
            raise ActionError(
                f"Cannot tell whether a call is live ({type(exc).__name__}), so 48V is not switched.", 409
            ) from exc
        if active:
            raise ActionError("A call is live, so 48V phantom power is not switched. Try again after the call.", 409)

    @staticmethod
    def _change(control_id: str, value: Any) -> tuple[dict[str, Any], Callable[[dict[str, Any]], bool]]:
        def number() -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ActionError("Expected a number.", 400)
            return float(value)

        def boolean() -> bool:
            if isinstance(value, bool) or value in (0, 1):
                return bool(value)
            raise ActionError("Expected on or off.", 400)

        tolerance = GAIN_TOLERANCE / 256
        if control_id == "gain":
            db = max(0.0, min(GAIN_MAX_DB, number()))
            return {"gain_db": db}, lambda c: abs(c["gain_db"] - db) <= tolerance
        if control_id == "headphones":
            db = max(HP_MIN_DB, min(0.0, number()))
            return {"headphone_db": db}, lambda c: abs(c["headphone_db"] - db) <= tolerance
        flags = {"mute": ("muted", "muted"), "phantom": ("phantom", "phantom"), "low_z": ("low_z", "low_z")}
        if control_id in flags:
            field, key = flags[control_id]
            on = boolean()
            return {field: on}, lambda c: c[key] == on
        raise ActionError(f"The Wave XLR has no control {control_id!r}.", 400)
