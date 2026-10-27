"""Test support for the hardware probe: recorded hosts and a fake sysfs, procfs and tool runner.

`FakeMachine` builds the directories the probe reads (`/sys/bus/usb`, `/sys/class/drm`, `/proc/asound`, ...) under a
temporary directory and answers the tools it runs (`lspci`, `vainfo`, `pactl`, `v4l2-ctl`, ...) from text the test
sets, so a test can plug a device in, take it out, deny a node, or break a tool, and run the real `probe.snapshot`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from streamstack.hardware import inventory, probe

FIXTURES = Path(__file__).parent / "fixtures" / "hardware"
HOSTS = ("dm313red", "deskmeet1")
MACHINE_ID = "0123456789abcdef0123456789abcdef"

Tool = str | Callable[[list[str]], "subprocess.CompletedProcess[str] | str | None"] | None

# Device Caps values: capture (uvcvideo), metadata, and the loopback pair (capture / output).
CAPS_CAPTURE = 0x04200001
CAPS_METADATA = 0x04A00000
CAPS_OUTPUT = 0x05200002


def inventory_text(host: str) -> str:
    return (FIXTURES / host / "inventory.txt").read_text(encoding="utf-8")


def section(host: str, name: str) -> str:
    return inventory.sections(inventory_text(host))[name]


def vainfo_text(*entries: tuple[str, str]) -> str:
    lines = ["vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 26.1.2 ()"]
    lines += [f"      {profile:<32}:\t{entry}" for profile, entry in entries]
    return "\n".join(lines)


ARC_VA = (
    ("VAProfileH264High", "VAEntrypointEncSliceLP"),
    ("VAProfileHEVCMain", "VAEntrypointVLD"),
    ("VAProfileHEVCMain", "VAEntrypointEncSliceLP"),
    ("VAProfileAV1Profile0", "VAEntrypointEncSliceLP"),
)
IGPU_VA = (
    ("VAProfileH264High", "VAEntrypointEncSliceLP"),
    ("VAProfileHEVCMain", "VAEntrypointVLD"),
    ("VAProfileHEVCMain", "VAEntrypointEncSliceLP"),
)

ENCODERS = (
    " V....D av1_vaapi            AV1 (VAAPI) (codec av1)\n"
    " V..... libsvtav1            SVT-AV1 (codec av1)\n"
    " V....D libx264              libx264 (codec h264)\n"
    " V....D hevc_vaapi           H.265 (VAAPI) (codec hevc)\n"
    " V....D h264_vaapi           H.264 (VAAPI) (codec h264)\n"
)


def alsa_stream(direction: str, fmt: str, channels: int, rates: str, interface: int = 1) -> str:
    kind = "Playback" if direction == "playback" else "Capture"
    return (
        f"{kind}:\n  Status: Stop\n  Interface {interface}\n    Altset 1\n    Format: {fmt}\n    Channels: {channels}\n"
        f"    Endpoint: 0x01 (1 OUT) (ASYNC)\n    Rates: {rates}\n    Bits: 24\n"
    )


@dataclass
class FakeMachine:
    """A temporary machine. Build it with the `add_*` methods, then `host()` for `probe.snapshot`."""

    root: Path
    tools: dict[str, Tool] = field(default_factory=dict)
    denied: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)
    _lspci: list[str] = field(default_factory=list)
    _vainfo: dict[str, str] = field(default_factory=dict)
    _pw: dict[str, list[str]] = field(default_factory=lambda: {"cards": [], "sinks": [], "sources": []})
    _usb_n: int = 0

    def __post_init__(self) -> None:
        for sub in ("sys", "proc", "dev", "etc"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "etc" / "machine-id").write_text(MACHINE_ID + "\n")
        self.tools.setdefault("lspci", self._run_lspci)
        self.tools.setdefault("vainfo", self._run_vainfo)
        self.tools.setdefault("ffmpeg", ENCODERS)
        self.tools.setdefault("pactl", self._run_pactl)
        self.tools.setdefault("v4l2-ctl", self._run_v4l2)
        self._v4l2: dict[str, tuple[int, str]] = {}
        self._v4l2_formats: dict[str, str] = {}

    # -- paths ----------------------------------------------------------------

    @property
    def sys(self) -> Path:
        return self.root / "sys"

    @property
    def proc(self) -> Path:
        return self.root / "proc"

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # -- USB ------------------------------------------------------------------

    def add_usb(self, usb_id: str, name: str, bus: int = 1) -> str:
        vendor, product = usb_id.split(":")
        self._usb_n += 1
        entry = self.sys / "bus" / "usb" / "devices" / f"{bus}-{self._usb_n}"
        self._write(entry / "idVendor", vendor + "\n")
        self._write(entry / "idProduct", product + "\n")
        self._write(entry / "busnum", f"{bus}\n")
        self._write(entry / "devnum", f"{self._usb_n + 1}\n")
        self._write(entry / "product", name + "\n")
        return entry.name

    def remove_usb(self, usb_id: str) -> None:
        vendor, product = usb_id.split(":")
        base = self.sys / "bus" / "usb" / "devices"
        for entry in list(base.iterdir()):
            if (entry / "idVendor").read_text().strip() == vendor and (
                entry / "idProduct"
            ).read_text().strip() == product:
                for item in entry.iterdir():
                    item.unlink()
                entry.rmdir()

    # -- GPUs -----------------------------------------------------------------

    def add_gpu(
        self, number: int, slot: str, pci_id: str, name: str, va: tuple[tuple[str, str], ...] | None = None
    ) -> str:
        node = f"renderD{number}"
        vendor, device = pci_id.split(":")
        self._write(
            self.sys / "class" / "drm" / node / "device" / "uevent",
            f"DRIVER=i915\nPCI_CLASS=30000\nPCI_ID={vendor.upper()}:{device.upper()}\nPCI_SLOT_NAME={slot}\n",
        )
        self._write(self.root / "dev" / "dri" / node, "")
        self._lspci.append(f"{slot[5:]} VGA compatible controller [0300]: {name} [{vendor}:{device}] (rev 05)")
        if va is not None:
            self._vainfo[f"/dev/dri/{node}"] = vainfo_text(*va)
        return f"/dev/dri/{node}"

    def remove_gpu(self, number: int) -> None:
        node = f"renderD{number}"
        uevent = self.sys / "class" / "drm" / node / "device" / "uevent"
        slot = re.search(r"PCI_SLOT_NAME=(\S+)", uevent.read_text()).group(1)
        uevent.unlink()
        (uevent.parent).rmdir()
        (uevent.parent.parent).rmdir()
        (self.root / "dev" / "dri" / node).unlink()
        self._lspci = [ln for ln in self._lspci if not ln.startswith(slot[5:])]
        self._vainfo.pop(f"/dev/dri/{node}", None)

    # -- sound ----------------------------------------------------------------

    def add_alsa_usb(
        self,
        number: int,
        card_id: str,
        usb_id: str,
        name: str,
        rates: str = "48000",
        fmt: str = "S24_3LE",
        playback_channels: int = 2,
        capture_channels: int = 1,
    ) -> None:
        card = self.proc / "asound" / f"card{number}"
        self._write(card / "id", card_id + "\n")
        self._write(card / "usbid", usb_id + "\n")
        body = f"{name} at usb-0000:00:14.0-2, full speed : USB Audio\n\n"
        body += alsa_stream("playback", fmt, playback_channels, rates, 1) + "\n"
        body += alsa_stream("capture", fmt, capture_channels, rates, 2)
        self._write(card / "stream0", body)

    def remove_alsa(self, number: int) -> None:
        card = self.proc / "asound" / f"card{number}"
        for item in card.iterdir():
            item.unlink()
        card.rmdir()

    def set_pipewire(
        self,
        cards: list[tuple[int, str]],
        sinks: list[tuple[int, str, str]] | None = None,
        sources: list[tuple[int, str, str]] | None = None,
    ) -> None:
        """Cards as (id, name); sinks and sources as (id, name, "s24le 2ch 48000Hz")."""
        self._pw["cards"] = [f"{i}\t{n}\talsa" for i, n in cards]
        self._pw["sinks"] = [f"{i}\t{n}\tPipeWire\t{fmt}\tSUSPENDED" for i, n, fmt in sinks or []]
        self._pw["sources"] = [f"{i}\t{n}\tPipeWire\t{fmt}\tSUSPENDED" for i, n, fmt in sources or []]

    def set_card_profiles(self, profiles: dict[str, tuple[str, str | None]]) -> None:
        """card name -> (active profile, "vid:pid")."""
        cards = []
        for name, (profile, usb_id) in profiles.items():
            props = {}
            if usb_id:
                props = {"device.vendor.id": usb_id.split(":")[0], "device.product.id": usb_id.split(":")[1]}
            cards.append({"name": name, "active_profile": profile, "properties": props})
        self.tools["pactl-json"] = json.dumps(cards)

    # -- video ----------------------------------------------------------------

    def add_v4l2(
        self, number: int, label: str, usb_id: str | None = None, caps: int = CAPS_CAPTURE, loopback: bool = False
    ) -> str:
        entry = self.sys / "class" / "video4linux" / f"video{number}"
        self._write(entry / "name", label + "\n")
        if loopback:
            target = self.root / "devices" / "platform" / f"v4l2loopback-{number:03d}"
            target.mkdir(parents=True, exist_ok=True)
        else:
            usb = self.root / "devices" / "pci0000:00" / "0000:00:14.0" / "usb1" / f"1-{number + 1}"
            target = usb / f"1-{number + 1}:1.0"
            target.mkdir(parents=True, exist_ok=True)
            if usb_id:
                self._write(usb / "idVendor", usb_id.split(":")[0] + "\n")
                self._write(usb / "idProduct", usb_id.split(":")[1] + "\n")
        (entry / "device").symlink_to(target)
        self._write(self.root / "dev" / f"video{number}", "")
        driver = "v4l2 loopback" if loopback else "uvcvideo"
        self._v4l2[f"/dev/video{number}"] = (
            caps,
            f"Driver name      : {driver}\nCard type        : {label}\nDevice Caps      : 0x{caps:08x}\n",
        )
        return f"/dev/video{number}"

    def set_formats(self, path: str, text: str) -> None:
        self._v4l2_formats[path] = text

    def add_loopback_module(self, options: str) -> None:
        self._write(self.sys / "module" / "v4l2loopback" / "version", "0.15.3\n")
        self._write(self.proc / "modules", "v4l2loopback 65536 2 - Live 0x0000000000000000\n")
        self._write(self.root / "etc" / "modprobe.d" / "v4l2loopback.conf", f"options v4l2loopback {options}\n")

    # -- tools ------------------------------------------------------------------

    def _run_lspci(self, argv: list[str]) -> str:
        return "\n".join(self._lspci) + "\n"

    def _run_vainfo(self, argv: list[str]) -> str:
        node = argv[argv.index("--device") + 1]
        if node not in self._vainfo:
            return "error: failed to initialize display\n"
        return self._vainfo[node] + "\n"

    def _run_pactl(self, argv: list[str]) -> str | subprocess.CompletedProcess[str]:
        if "json" in argv:
            result = self.tools.get("pactl-json")
            if not isinstance(result, str):
                return subprocess.CompletedProcess(argv, 1, "", "pactl: unsupported")
            return result
        kind = argv[-1]
        return "\n".join(self._pw.get(kind, [])) + "\n"

    def _run_v4l2(self, argv: list[str]) -> str:
        path = argv[argv.index("-d") + 1]
        if "--list-formats-ext" in argv:
            return self._v4l2_formats.get(path, "ioctl: VIDIOC_ENUM_FMT\n\tType: Video Capture\n\n")
        return self._v4l2[path][1]

    def _run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        self.calls.append(argv)
        tool = self.tools.get(argv[0])
        if callable(tool):
            tool = tool(argv)
        if tool is None:
            return None  # timed out
        if isinstance(tool, subprocess.CompletedProcess):
            return tool
        return subprocess.CompletedProcess(argv, 0, tool, "")

    def _which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.tools and (name != "wpctl" or self.tools[name] is not None) else None

    def host(self) -> probe.Host:
        machine = self

        class _Host(probe.Host):
            def access(self, path: Path) -> str | None:
                if not path.exists():
                    return None
                return (
                    "denied"
                    if str(path.relative_to(machine.root / "dev")) in machine.denied or path.name in machine.denied
                    else "rw"
                )

        return _Host(
            sys=self.sys,
            proc=self.proc,
            dev=self.root / "dev",
            etc=self.root / "etc",
            run=self._run,
            which=self._which,
            clock=lambda: 1000.0,
        )

    def snapshot(self):
        return probe.snapshot(self.host())


def studio_wave(machine: FakeMachine, *, gpu_node: int = 128) -> FakeMachine:
    """The dm313red studio: Arc on one render node, Wave XLR, Cam Link, Canon, Litra, loopback pair."""
    machine.add_usb("0fd9:007d", "Elgato Wave XLR")
    machine.add_usb("0fd9:0067", "Cam Link 4K")
    machine.add_usb("04a9:319b", "Canon EOS 50D")
    machine.add_usb("046d:c901", "Litra Beam")
    machine.add_gpu(gpu_node, "0000:03:00.0", "8086:56a5", "Intel Corporation DG2 [Arc A380]", ARC_VA)
    machine.add_alsa_usb(3, "XLR", "0fd9:007d", "Elgato Systems Elgato Wave XLR", "48000, 96000")
    card = "alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00"
    machine.set_pipewire(
        [(49, card)],
        sinks=[(55, "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00.analog-stereo", "s24le 2ch 48000Hz")],
        sources=[(56, "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00.mono-fallback", "s24le 1ch 48000Hz")],
    )
    machine.set_card_profiles({card: ("output:analog-stereo+input:mono-fallback", "0fd9:007d")})
    machine.add_v4l2(0, "Cam Link 4K: Cam Link 4K", "0fd9:0067")
    machine.add_v4l2(1, "Cam Link 4K: Cam Link 4K", "0fd9:0067", CAPS_METADATA)
    machine.add_v4l2(42, "Canon EOS 50D", loopback=True)
    machine.add_v4l2(43, "StreamStack Camera", loopback=True, caps=CAPS_OUTPUT)
    machine.set_formats(
        "/dev/video0",
        "ioctl: VIDIOC_ENUM_FMT\n\tType: Video Capture\n\n\t[0]: 'YUYV' (YUYV 4:2:2)\n\t\tSize: Discrete 1920x1080\n"
        "\t\t\tInterval: Discrete 0.017s (60.000 fps)\n",
    )
    machine.add_loopback_module('devices=2 video_nr=42,43 card_label="Canon EOS 50D,StreamStack Camera"')
    return machine
