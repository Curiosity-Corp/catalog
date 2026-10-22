"""Simulated hardware for `--demo` and the tests: the same ALSA control names as the kernel, a Wave XLR config block.

`SimMixer` answers like `alsa.Mixer` (and refuses what the kernel would refuse), so the real Scarlett driver runs
unchanged against it; `SimXlr` is a `wavexlr.Transport` that keeps a 34 byte config in memory.
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .alsa import MixerControl, MixerError
from .usb import UsbDevice
from .wavexlr import CONFIG_LEN, DEVINFO_LEN, WVALUE_CONFIG, WVALUE_DEVINFO, XlrError, patch_config

AUTOGAIN_STATUS = [
    "Running", "Success", "SuccessDRover", "WarnMinGainLimit", "FailDRunder",
    "FailMaxGainLimit", "FailClipped", "Cancelled", "Root", "Invalid",
]  # fmt: skip
AUTOGAIN_SECONDS = 3.0
LOCKED_BY_AUTOGAIN = ("Gain", "Level", "Air", "Safe", "Link", "Phantom", "Target")

SCARLETT_DEVICES = {
    "gen3": UsbDevice(0x1235, 0x8210, "Scarlett 2i2 USB", 1, 11, 7),
    "gen4": UsbDevice(0x1235, 0x8219, "Scarlett 2i2 4th Gen", 1, 12, 7),
}
XLR_DEVICE = UsbDevice(0x0FD9, 0x007D, "Elgato Wave XLR", 1, 13, 7)


class SimMixer:
    """A Scarlett 2i2 mixer. `generation` is "gen3" or "gen4"."""

    def __init__(self, generation: str = "gen4", clock: Callable[[], float] = time.monotonic) -> None:
        if generation not in SCARLETT_DEVICES:
            raise ValueError(f"unknown Scarlett generation {generation!r}")
        self.generation = generation
        self.card = 7
        self._clock = clock
        self._lock = threading.Lock()
        self._controls: dict[str, MixerControl] = {}
        self._started: dict[int, float] = {}
        self._next = 1
        self._build()

    def _add(self, name: str, kind: str, value: int, *, items: list[str] | None = None, low: int | None = None,
             high: int | None = None, db: tuple[float, float] | None = None, writable: bool = True,
             iface: str = "MIXER") -> None:  # fmt: skip
        access = "rw---R--" if writable else "r-------"
        ctl = MixerControl(self._next, iface, name, kind, access, 1, low, high, 1 if kind == "INTEGER" else None,
                           list(items or []), [value], db)  # fmt: skip
        self._next += 1
        self._controls[name] = ctl

    def _build(self) -> None:
        gen4 = self.generation == "gen4"
        self._add("Firmware Version", "INTEGER", 2417 if gen4 else 1644, low=0, high=0, writable=False, iface="CARD")
        if gen4:
            self._add("Minimum Firmware Version", "INTEGER", 2115, low=0, high=0, writable=False, iface="CARD")
        self._add("Sync Status", "ENUMERATED", 1, items=["Unlocked", "Locked"], writable=False)
        self._add("MSD Mode Switch", "BOOLEAN", 0)
        for n in (1, 2):
            self._add(f"Line In {n} Level Capture Enum", "ENUMERATED", 0, items=["Line", "Inst"])
            if gen4:
                self._add(
                    f"Line In {n} Gain Capture Volume", "INTEGER", 38 - 2 * (n - 1), low=0, high=70, db=(0.0, 69.0)
                )
                self._add(f"Line In {n} Autogain Capture Switch", "BOOLEAN", 0)
                self._add(
                    f"Line In {n} Autogain Status Capture Enum", "ENUMERATED", 9, items=AUTOGAIN_STATUS, writable=False
                )
                self._add(
                    f"Line In {n} Air Capture Enum", "ENUMERATED", 0, items=["Off", "Presence", "Presence + Drive"]
                )
                self._add(f"Line In {n} Safe Capture Switch", "BOOLEAN", 0)
                self._add(f"Line In {n} Link Capture Switch", "BOOLEAN", 0)
            else:
                self._add(f"Line In {n} Air Capture Switch", "BOOLEAN", 0)
        if gen4:
            self._add("Autogain Mean Target", "INTEGER", -14, low=-30, high=0)
            self._add("Autogain Peak Target", "INTEGER", -6, low=-30, high=0)
            self._add("Input Select Capture Enum", "ENUMERATED", 0, items=["Input 1", "Input 2"])
        self._add("Line In 1-2 Phantom Power Capture Switch", "BOOLEAN", 0)
        if not gen4:
            self._add("Phantom Power Persistence Capture Switch", "BOOLEAN", 0)
        self._add("Direct Monitor Playback Enum", "ENUMERATED", 0, items=["Off", "Mono", "Stereo"])
        self._add("Level Meter", "INTEGER", 0, low=0, high=4095, writable=False)

    # -- the alsa.Mixer interface --------------------------------------------------------------------------

    def available(self) -> bool:
        return True

    def _advance(self) -> None:
        """Finish an Autogain run that has had its time (called under the lock)."""
        for n, started in list(self._started.items()):
            if self._clock() - started < AUTOGAIN_SECONDS:
                continue
            del self._started[n]
            self._controls[f"Line In {n} Autogain Capture Switch"].values = [0]
            self._controls[f"Line In {n} Autogain Status Capture Enum"].values = [1]
            self._controls[f"Line In {n} Gain Capture Volume"].values = [41 - n]

    def _locked(self, name: str) -> bool:
        """While Autogain runs the firmware locks the input settings it is adjusting (not the Autogain switch)."""
        if not self._started or name.endswith("Autogain Capture Switch"):
            return False
        return any(word in name for word in LOCKED_BY_AUTOGAIN)

    def _snapshot(self, ctl: MixerControl) -> MixerControl:
        copy = MixerControl(**{**ctl.__dict__, "items": list(ctl.items), "values": list(ctl.values)})
        if self._locked(ctl.name):
            copy.access = "r-------"
        return copy

    def controls(self) -> dict[str, MixerControl]:
        with self._lock:
            self._advance()
            if self._started:
                for n in self._started:
                    self._controls[f"Line In {n} Autogain Status Capture Enum"].values = [0]
            return {name: self._snapshot(ctl) for name, ctl in self._controls.items()}

    def get(self, numid: int) -> MixerControl:
        for ctl in self.controls().values():
            if ctl.numid == numid:
                return ctl
        raise MixerError(f"Control {numid} is not on card {self.card}.")

    def put(self, numid: int, values: list[int]) -> None:
        with self._lock:
            self._advance()
            ctl = next((c for c in self._controls.values() if c.numid == numid), None)
            if ctl is None:
                raise MixerError(f"Control {numid} is not on card {self.card}.")
            if not ctl.writable or self._locked(ctl.name):
                raise MixerError("Operation not permitted")
            if len(values) != 1:
                raise MixerError("Wrong number of values.")
            value = values[0]
            if ctl.type == "BOOLEAN" and value not in (0, 1):
                raise MixerError("Invalid argument")
            if ctl.type == "ENUMERATED" and not 0 <= value < len(ctl.items):
                raise MixerError("Invalid argument")
            if (
                ctl.type == "INTEGER"
                and ctl.minimum is not None
                and ctl.maximum is not None
                and not ctl.minimum <= value <= ctl.maximum
            ):
                raise MixerError("Invalid argument")
            ctl.values = [value]
            number = next((n for n in (1, 2) if ctl.name == f"Line In {n} Autogain Capture Switch"), 0)
            if number:
                if value:
                    self._started[number] = self._clock()
                    self._controls[f"Line In {number} Autogain Status Capture Enum"].values = [0]
                else:
                    self._started.pop(number, None)
                    self._controls[f"Line In {number} Autogain Status Capture Enum"].values = [7]


def sim_proc(root: Path, card: int = 7, rate: int = 48000, period: int = 128) -> Path:
    """A /proc/asound subset (capture hw_params) under `root`, so the rate read-out has something to read."""
    params = root / "asound" / f"card{card}" / "pcm0c" / "sub0"
    params.mkdir(parents=True, exist_ok=True)
    (params / "hw_params").write_text(f"access: MMAP_INTERLEAVED\nrate: {rate} ({rate}/1)\nperiod_size: {period}\n")
    return root


class SimXlr:
    """A Wave XLR config block in memory behind the `wavexlr.Transport` interface."""

    def __init__(self) -> None:
        raw = bytearray(CONFIG_LEN)
        struct.pack_into("<H", raw, 0, 42 * 256)
        struct.pack_into("<h", raw, 9, -3 * 256)
        raw[30] = 0x5A  # a byte the driver does not know: it must survive every write
        self.config = bytes(raw)
        info = bytearray(DEVINFO_LEN)
        info[0:2] = bytes([1, 0])
        info[6:9] = bytes([1, 0, 9])
        self.devinfo = bytes(info)
        self.writes: list[bytes] = []
        self.fail: XlrError | None = None
        self.ignore_mute = False  # a device that does not keep the mute flag, for the read-back check

    def opener(self, device: UsbDevice) -> SimXlr:
        if self.fail:
            raise self.fail
        return self

    def read(self, wvalue: int, length: int) -> bytes:
        data = {WVALUE_CONFIG: self.config, WVALUE_DEVINFO: self.devinfo}.get(wvalue)
        if data is None or len(data) != length:
            raise XlrError(f"The simulated Wave XLR has no block {wvalue:#x} of {length} bytes.")
        return data

    def write(self, wvalue: int, data: bytes) -> None:
        if wvalue != WVALUE_CONFIG or len(data) != CONFIG_LEN:
            raise XlrError("The simulated Wave XLR only takes its config block.")
        self.writes.append(bytes(data))
        self.config = patch_config(data, muted=False) if self.ignore_mute else bytes(data)

    def close(self) -> None:
        return None
