"""Find USB devices and their ALSA card numbers from /sys and /proc (read-only, no helper programs)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UsbDevice:
    vid: int
    pid: int
    name: str
    bus: int
    address: int
    card: int | None = None

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def node(self) -> str:
        """The usbfs node of this device (what a vendor control transfer opens)."""
        return f"/dev/bus/usb/{self.bus:03d}/{self.address:03d}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _hex(text: str | None) -> int | None:
    try:
        return int(text, 16) if text else None
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except ValueError:
        return None


def alsa_cards(proc_root: Path = Path("/proc")) -> dict[tuple[int, int], int]:
    """USB (bus, address) -> ALSA card number for every USB sound card (`/proc/asound/cardN/usbbus`)."""
    cards: dict[tuple[int, int], int] = {}
    for entry in sorted((proc_root / "asound").glob("card[0-9]*")):
        number = _int(entry.name[4:])
        bus_text = _read(entry / "usbbus")
        if number is None or not bus_text or "/" not in bus_text:
            continue
        bus, _, address = bus_text.partition("/")
        if _int(bus) is not None and _int(address) is not None:
            cards[(int(bus), int(address))] = number
    return cards


def scan(sys_root: Path = Path("/sys"), proc_root: Path = Path("/proc")) -> list[UsbDevice]:
    """Every USB device, ordered by bus and address, with its sound card when it has one."""
    cards = alsa_cards(proc_root)
    devices: list[UsbDevice] = []
    for entry in (sys_root / "bus" / "usb" / "devices").glob("*"):
        if ":" in entry.name:  # an interface of a device, not a device
            continue
        vid, pid = _hex(_read(entry / "idVendor")), _hex(_read(entry / "idProduct"))
        bus, address = _int(_read(entry / "busnum")), _int(_read(entry / "devnum"))
        if vid is None or pid is None or bus is None or address is None:
            continue
        devices.append(UsbDevice(vid, pid, _read(entry / "product") or "", bus, address, cards.get((bus, address))))
    return sorted(devices, key=lambda d: (d.bus, d.address))
