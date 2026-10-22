"""Logitech Litra lights over USB HID: discovery, the report format and a hidraw transport.

Standard library only. A Litra is a USB HID device; its vendor interface (usage page 0xff43) takes 20-byte HID++
style reports. This module knows nothing about the Console: `streamstack.console.lighting` builds the controller
on top of it.

Prior art, which this follows byte for byte:

- timrogers/litra-rs 3.3.0 (Rust, `src/lib.rs`), the reference for product IDs, report layouts, lumen limits
  and the 2700-6500 K range in 100 K steps. It is exercised against real Glow, Beam and Beam LX lights.
- kharyam/litra-driver (Python, PyPI `litra-driver`) uses the same command bytes (0x1c power, 0x4c brightness,
  0x9c temperature) for the Glow (046d:c900) and Beam (046d:c901).

Reports (all 20 bytes, zero padded; byte 0 is the HID report ID 0x11, byte 1 the device 0xff):

    11 ff <feature> <command> <payload...>

`feature` is 0x04 for the Glow and Beam, 0x06 for the Beam LX. Commands: power 0x1c (set, payload 1/0) / 0x01
(get), brightness in lumen 0x4c / 0x31 and temperature in kelvin 0x9c / 0x81 (both a big-endian 16-bit payload).
A get is answered by a report that echoes `11 ff <feature> <command>` and carries the value from byte 4 on.

Nothing here opens a device unless a caller asks it to: `discover()` only reads sysfs.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import select
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

VENDOR_ID = 0x046D
REPORT_ID = 0x11
DEVICE_BYTE = 0xFF
REPORT_LENGTH = 20
# usage page 0xff43 in a HID report descriptor: item `06` (usage page, 2 bytes) then 43 ff
VENDOR_USAGE_PAGE = bytes.fromhex("0643ff")

KELVIN_MIN, KELVIN_MAX, KELVIN_STEP = 2700, 6500, 100

CMD_GET_POWER = 0x01
CMD_GET_BRIGHTNESS = 0x31
CMD_GET_TEMPERATURE = 0x81
CMD_SET_POWER = 0x1C
CMD_SET_BRIGHTNESS = 0x4C
CMD_SET_TEMPERATURE = 0x9C

READ_TIMEOUT = 0.2  # seconds to wait for the answer to one get

SYSFS_HIDRAW = Path("/sys/class/hidraw")
DEV = Path("/dev")

UDEV_RULE = 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="c900|c901|b901|c903", TAG+="uaccess"'
UDEV_RULE_FILE = "/etc/udev/rules.d/60-streamstack-litra.rules"
PERMISSION_HINT = (
    "Install the StreamStack udev rule for Litra lights (docs/lighting.md), then unplug and replug the light."
)


class LightingError(Exception):
    """A light that cannot be reached. `kind`: permission, gone (unplugged) or io."""

    def __init__(self, message: str, kind: str = "io") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass(frozen=True)
class Model:
    pid: int
    name: str
    lumen_min: int
    lumen_max: int
    feature: int  # byte 2 of every report


MODELS: dict[int, Model] = {
    0xC900: Model(0xC900, "Litra Glow", 20, 250, 0x04),
    0xC901: Model(0xC901, "Litra Beam", 30, 400, 0x04),
    0xB901: Model(0xB901, "Litra Beam", 30, 400, 0x04),  # a second product ID litra-rs lists for the Beam
    0xC903: Model(0xC903, "Litra Beam LX", 30, 400, 0x06),
}


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def clamp_kelvin(kelvin: float) -> int:
    """The nearest supported colour temperature: a multiple of 100 K in 2700-6500."""
    stepped = round(kelvin / KELVIN_STEP) * KELVIN_STEP
    return int(min(KELVIN_MAX, max(KELVIN_MIN, stepped)))


def percent_to_lumen(model: Model, percent: float) -> int:
    """1 % is the dimmest the light goes, 100 % its maximum."""
    fraction = (min(100.0, max(1.0, percent)) - 1.0) / 99.0
    return round(model.lumen_min + (model.lumen_max - model.lumen_min) * fraction)


def lumen_to_percent(model: Model, lumen: int) -> int:
    span = model.lumen_max - model.lumen_min
    fraction = min(1.0, max(0.0, (lumen - model.lumen_min) / span))
    return round(fraction * 99) + 1


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def build_report(model: Model, command: int, *payload: int) -> bytes:
    """One 20-byte output report."""
    if len(payload) > REPORT_LENGTH - 4 or any(not 0 <= b <= 0xFF for b in payload):
        raise ValueError("payload does not fit a report")
    head = bytes((REPORT_ID, DEVICE_BYTE, model.feature, command, *payload))
    return head.ljust(REPORT_LENGTH, b"\x00")


def report_power(model: Model, on: bool) -> bytes:
    return build_report(model, CMD_SET_POWER, 1 if on else 0)


def report_brightness(model: Model, lumen: int) -> bytes:
    lumen = int(min(model.lumen_max, max(model.lumen_min, lumen)))
    return build_report(model, CMD_SET_BRIGHTNESS, lumen >> 8, lumen & 0xFF)


def report_temperature(model: Model, kelvin: int) -> bytes:
    kelvin = clamp_kelvin(kelvin)
    return build_report(model, CMD_SET_TEMPERATURE, kelvin >> 8, kelvin & 0xFF)


def answer_to(model: Model, report: bytes, command: int) -> bytes | None:
    """The value bytes (from byte 4) of `report` when it answers `command`, otherwise None."""
    if len(report) < 6 or report[0] != REPORT_ID or report[1] != DEVICE_BYTE:
        return None
    if report[2] != model.feature or report[3] != command:
        return None
    return bytes(report[4:])


def decode_power(value: bytes) -> bool:
    return value[0] == 1


def decode_word(value: bytes) -> int:
    return (value[0] << 8) | value[1]


# ---------------------------------------------------------------------------
# Discovery (sysfs only: never opens a device)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """One physical light and the hidraw node of its vendor interface."""

    id: str
    node: Path
    model: Model
    phys: str
    name: str  # what the USB device calls itself


_HID_ID = re.compile(r"[0-9A-Fa-f]+:([0-9A-Fa-f]+):([0-9A-Fa-f]+)")


def _uevent(path: Path) -> dict[str, str]:
    text = path.read_text(errors="replace")
    pairs = (line.partition("=") for line in text.splitlines())
    return {k: v for k, sep, v in pairs if sep}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60]


def discover(sysfs: Path = SYSFS_HIDRAW, dev: Path = DEV) -> list[Found]:
    """Every Litra on the bus, one entry per light, sorted by id.

    A light exposes several HID interfaces and so several hidraw nodes; the one that carries the vendor usage page
    0xff43 takes the commands. When the report descriptor cannot be read the lowest-numbered interface is used.
    Raises LightingError("io") only when sysfs itself cannot be listed; an absent hidraw class means no HID devices.
    """
    try:
        nodes = sorted(p for p in sysfs.iterdir() if p.name.startswith("hidraw"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LightingError(f"Cannot list {sysfs}: {exc.strerror or exc}") from exc
    lights: dict[str, list[tuple[bool, int, Found]]] = {}
    for node in nodes:
        found = _candidate(node, dev)
        if found is None:
            continue
        light, vendor_page, interface = found
        lights.setdefault(light.id, []).append((vendor_page, interface, light))
    chosen = []
    for options in lights.values():
        options.sort(key=lambda o: (not o[0], o[1], o[2].node.name))
        chosen.append(options[0][2])
    return sorted(chosen, key=lambda f: f.id)


def _candidate(node: Path, dev: Path) -> tuple[Found, bool, int] | None:
    device = node / "device"
    try:
        uevent = _uevent(device / "uevent")
    except OSError:
        return None  # the node vanished while we looked, or is not readable: not ours to report
    match = _HID_ID.fullmatch(uevent.get("HID_ID", "").strip())
    if not match:
        return None
    vendor, product = int(match.group(1), 16), int(match.group(2), 16)
    model = MODELS.get(product)
    if vendor != VENDOR_ID or model is None:
        return None
    phys = uevent.get("HID_PHYS", "")
    base, _, tail = phys.partition("/input")
    interface = int(tail) if tail.isdigit() else 0
    uniq = uevent.get("HID_UNIQ", "").strip()
    identity = uniq or base or node.name
    try:
        vendor_page = VENDOR_USAGE_PAGE in (device / "report_descriptor").read_bytes()
    except OSError:
        vendor_page = False
    light = Found(
        id=f"{product:04x}-{_slug(identity)}",
        node=dev / node.name,
        model=model,
        phys=phys,
        name=uevent.get("HID_NAME", model.name),
    )
    return light, vendor_page, interface


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, timeout: float) -> bytes | None: ...

    def drain(self) -> None: ...

    def close(self) -> None: ...


def _translate(exc: OSError, node: Path) -> LightingError:
    if exc.errno in (errno.EACCES, errno.EPERM):
        return LightingError(f"Permission denied opening {node}. {PERMISSION_HINT}", "permission")
    if exc.errno in (errno.ENODEV, errno.ENXIO, errno.EIO, errno.ENOENT, errno.EPIPE):
        return LightingError(f"{node} was unplugged.", "gone")
    return LightingError(f"Cannot talk to {node}: {exc.strerror or exc}", "io")


class HidrawTransport:
    """A /dev/hidraw node opened non-blocking. Reports are exchanged whole, report ID first."""

    def __init__(self, node: Path) -> None:
        self.node = node
        try:
            self.fd: int | None = os.open(node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            raise _translate(exc, node) from exc

    def _fd(self) -> int:
        if self.fd is None:
            raise LightingError(f"{self.node} is closed.", "gone")
        return self.fd

    def write(self, data: bytes) -> None:
        try:
            written = os.write(self._fd(), data)
        except OSError as exc:
            raise _translate(exc, self.node) from exc
        if written != len(data):
            raise LightingError(f"Short write to {self.node} ({written} of {len(data)} bytes).")

    def read(self, timeout: float) -> bytes | None:
        fd = self._fd()
        try:
            ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
            if not ready:
                return None
            return os.read(fd, 64)
        except BlockingIOError:
            return None
        except OSError as exc:
            raise _translate(exc, self.node) from exc

    def drain(self) -> None:
        for _ in range(64):  # stale answers and button-press notifications queue up in the kernel
            if self.read(0.0) is None:
                return

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


# ---------------------------------------------------------------------------
# One light
# ---------------------------------------------------------------------------


class Litra:
    """The protocol over a Transport. Not thread safe: callers serialise access."""

    def __init__(self, found: Found, transport: Transport, clock: Callable[[], float] = time.monotonic) -> None:
        self.found = found
        self.model = found.model
        self.transport = transport
        self._clock = clock

    def close(self) -> None:
        self.transport.close()

    def _ask(self, command: int) -> bytes | None:
        """Send a get and wait for its answer; None when the light does not answer in time."""
        self.transport.drain()
        self.transport.write(build_report(self.model, command))
        deadline = self._clock() + READ_TIMEOUT
        while (left := deadline - self._clock()) > 0:
            report = self.transport.read(left)
            if report is None:
                break
            value = answer_to(self.model, report, command)
            if value is not None:
                return value
        return None

    def read_power(self) -> bool | None:
        value = self._ask(CMD_GET_POWER)
        return None if value is None else decode_power(value)

    def read_lumen(self) -> int | None:
        value = self._ask(CMD_GET_BRIGHTNESS)
        return None if value is None else decode_word(value)

    def read_kelvin(self) -> int | None:
        value = self._ask(CMD_GET_TEMPERATURE)
        return None if value is None else decode_word(value)

    def set_power(self, on: bool) -> None:
        self.transport.write(report_power(self.model, on))

    def set_lumen(self, lumen: int) -> int:
        lumen = int(min(self.model.lumen_max, max(self.model.lumen_min, lumen)))
        self.transport.write(report_brightness(self.model, lumen))
        return lumen

    def set_kelvin(self, kelvin: int) -> int:
        kelvin = clamp_kelvin(kelvin)
        self.transport.write(report_temperature(self.model, kelvin))
        return kelvin


# ---------------------------------------------------------------------------
# A simulated light (the demo rig and the tests)
# ---------------------------------------------------------------------------


class SimulatedTransport:
    """Behaves like a Litra's vendor interface: parses output reports, queues the answers to gets."""

    def __init__(self, model: Model, *, on: bool = False, lumen: int | None = None, kelvin: int = 4500) -> None:
        self.model = model
        self.on = on
        self.lumen = lumen if lumen is not None else (model.lumen_min + model.lumen_max) // 2
        self.kelvin = kelvin
        self.writes: list[bytes] = []
        self.closed = False
        self._inbox: list[bytes] = []

    def write(self, data: bytes) -> None:
        if self.closed:
            raise LightingError("closed", "gone")
        if (
            len(data) != REPORT_LENGTH
            or data[0] != REPORT_ID
            or data[1] != DEVICE_BYTE
            or data[2] != self.model.feature
        ):
            raise LightingError("the simulated light got a malformed report")
        self.writes.append(bytes(data))
        command, payload = data[3], data[4:6]
        if command == CMD_SET_POWER:
            self.on = payload[0] == 1
        elif command == CMD_SET_BRIGHTNESS:
            self.lumen = min(self.model.lumen_max, max(self.model.lumen_min, (payload[0] << 8) | payload[1]))
        elif command == CMD_SET_TEMPERATURE:
            self.kelvin = clamp_kelvin((payload[0] << 8) | payload[1])
        elif command == CMD_GET_POWER:
            self._answer(command, 1 if self.on else 0, 0)
        elif command == CMD_GET_BRIGHTNESS:
            self._answer(command, self.lumen >> 8, self.lumen & 0xFF)
        elif command == CMD_GET_TEMPERATURE:
            self._answer(command, self.kelvin >> 8, self.kelvin & 0xFF)

    def _answer(self, command: int, *payload: int) -> None:
        self._inbox.append(build_report(self.model, command, *payload))

    def read(self, timeout: float) -> bytes | None:
        return self._inbox.pop(0) if self._inbox else None

    def drain(self) -> None:
        self._inbox.clear()

    def close(self) -> None:
        self.closed = True
