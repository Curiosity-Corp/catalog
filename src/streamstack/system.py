"""Read-only probes of cameras, audio devices and helper processes."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

CAMLINK_LINK = Path("/dev/streamstack/camlink")
VOICE_SOURCE = "streamstack_mic"  # the StreamStack voice chain's virtual source, "StreamStack Mic"
CANON_USB_VENDOR = "04a9"
# Elgato Cam Link 4K. Capture cards are found by USB id, never by /dev/videoN (the number moves between boots).
# Elgato Cam Link 4K: 0066 is the first hardware revision, 0067 the later one (both studio hosts have 0067).
CAMLINK_USB_IDS = (("0fd9", "0066"), ("0fd9", "0067"))
SYSFS_VIDEO = Path("/sys/class/video4linux")

# v4l2 fourcc -> ffmpeg -input_format
FFMPEG_FORMATS = {
    "YUYV": "yuyv422",
    "MJPG": "mjpeg",
    "NV12": "nv12",
    "YU12": "yuv420p",
    "UYVY": "uyvy422",
    "H264": "h264",
    "RGB3": "rgb24",
    "BGR3": "bgr24",
    "GREY": "gray",
}

Mode = tuple[str, int, int, float]


GRACE = (2.0, 2.0, 3.0)  # seconds to wait after SIGINT, after SIGTERM and after SIGKILL


class Reapable(Protocol):
    """The slice of subprocess.Popen that `reap` uses (so tests can pass a process that cannot be killed)."""

    pid: int

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def reap(proc: Reapable, grace: tuple[float, float, float] = GRACE) -> bool:
    """Stop a child for good: SIGINT, then SIGTERM, then SIGKILL, waiting `grace` seconds after each.

    A client of a wedged audio server can ignore polite signals (or sit in an uninterruptible
    read), so the last step cannot be skipped. Returns False only when even SIGKILL left a
    process nobody could wait for (it is then abandoned, never waited on forever).
    """
    for sig, wait in zip((signal.SIGINT, signal.SIGTERM, signal.SIGKILL), grace, strict=False):
        if proc.poll() is not None:
            return True
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return True
        except OSError as exc:
            print(f"streamstack: cannot signal pid {proc.pid}: {exc}", file=sys.stderr)
        try:
            proc.wait(timeout=wait)
            return True
        except subprocess.TimeoutExpired:
            continue
    return proc.poll() is not None


def run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
    """Run a helper with a hard deadline; None when it cannot start, fails to finish or has to be killed.

    On a timeout the child is reaped (SIGINT, SIGTERM, SIGKILL) so nothing lingers on a wedged server.
    """
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError:
        return None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        reap(proc, (0.0, 1.0, 3.0))
        with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
            proc.communicate(timeout=1)
        return None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def running(process: str) -> bool:
    """Whether this user runs a process with exactly this name."""
    result = run(["pgrep", "-u", str(os.getuid()), "-x", process])
    return bool(result and result.returncode == 0)


def user_unit_active(unit: str) -> bool:
    result = run(["systemctl", "--user", "is-active", unit])
    return bool(result and result.stdout.strip() == "active")


def detach(argv: list[str]) -> None:
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------


def video_name(device: str) -> str:
    try:
        node = Path(device).resolve().name
        return Path(f"/sys/class/video4linux/{node}/name").read_text().strip()
    except OSError:
        return ""


def video_devices() -> dict[str, str]:
    """Map /dev/videoN to its driver-reported name."""
    found = {}
    for entry in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            found[f"/dev/{entry.name}"] = (entry / "name").read_text().strip()
        except OSError:
            continue
    return found


def usb_ids(node: Path) -> tuple[str, str] | None:
    """(vendor, product) of the USB device behind a /sys/class/video4linux node; None when it is not USB."""
    try:
        here = (node / "device").resolve()
    except OSError:
        return None
    for parent in (here, *here.parents):
        try:
            return (parent / "idVendor").read_text().strip().lower(), (parent / "idProduct").read_text().strip().lower()
        except OSError:
            continue
    return None


def video_by_usb_id(ids: tuple[tuple[str, str], ...], sysfs: Path = SYSFS_VIDEO) -> list[str]:
    """The capture nodes of every USB device with one of these (vendor, product) ids, lowest node first.

    A UVC device also registers a metadata node (index 1 and up); only index 0 delivers pictures.
    """
    found = []
    for node in sorted(sysfs.glob("video*"), key=lambda n: (len(n.name), n.name)):
        try:
            if (node / "index").read_text().strip() != "0":
                continue
        except OSError:
            continue
        if usb_ids(node) in ids:
            found.append(f"/dev/{node.name}")
    return found


def capture_card_ids() -> tuple[tuple[str, str], ...]:
    """The USB ids of capture cards: what the hardware profiles list, plus the ones built in (a damaged or missing
    catalogue must not stop the card being found)."""
    try:
        from streamstack.hardware import camera

        listed = camera.capture_card_ids()
    except Exception as exc:  # the catalogue is data; whatever is wrong with it, fall back to the built-in ids
        log.warning("capture card ids not read from the hardware profiles: %s: %s", type(exc).__name__, exc)
        listed = ()
    return tuple(dict.fromkeys((*CAMLINK_USB_IDS, *listed)))


def find_camlink() -> str:
    """The Cam Link's capture node: the udev link, else its USB id, else its name."""
    if CAMLINK_LINK.exists():
        return str(CAMLINK_LINK)
    if by_id := video_by_usb_id(capture_card_ids()):
        return by_id[0]
    for device, name in video_devices().items():
        if "cam link" in name.lower():
            return device
    return ""


def loopback_has_producer(device: str) -> bool | None:
    """With exclusive_caps=1 a loopback node only offers capture while fed."""
    result = run(["v4l2-ctl", "-d", device, "--info"])
    if result is None or result.returncode != 0:
        return None
    caps = result.stdout.split("Device Caps", 1)[-1]
    return "Video Capture" in caps


def capture_devices() -> list[tuple[str, str]]:
    """Every node that delivers video now (a fed loopback, a UVC camera, a capture card).

    Metadata nodes and unfed exclusive_caps loopbacks offer no capture and are left out.
    """
    return [(device, name) for device, name in video_devices().items() if loopback_has_producer(device)]


def parse_modes(listing: str) -> list[Mode]:
    """(fourcc, width, height, fps) for every discrete mode in `v4l2-ctl --list-formats-ext`."""
    modes: list[Mode] = []
    fourcc, size = "", None
    for line in listing.splitlines():
        line = line.strip()
        if match := re.match(r"\[\d+\]: '(\w+)'", line):
            fourcc, size = match.group(1), None
        elif match := re.match(r"Size: Discrete (\d+)x(\d+)", line):
            size = (int(match.group(1)), int(match.group(2)))
        elif (match := re.search(r"Interval: Discrete [\d.]+s \(([\d.]+) fps\)", line)) and size:
            mode = (fourcc, size[0], size[1], float(match.group(1)))
            if mode not in modes:
                modes.append(mode)
    return modes


def camera_modes(device: str) -> list[Mode]:
    result = run(["v4l2-ctl", "-d", device, "--list-formats-ext"])
    if result is None or result.returncode != 0:
        return []
    return parse_modes(result.stdout)


def mode_label(mode: Mode) -> str:
    fourcc, width, height, fps = mode
    return f"{width}x{height}@{fps:g} {fourcc}"


def parse_current(output: str) -> Mode | None:
    """(fourcc, width, height, fps) from `v4l2-ctl --get-fmt-video --get-parm`; fps is 0 when not reported."""
    width = height = fourcc = fps = ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Width/Height"):
            width, _, height = line.split(":", 1)[1].strip().partition("/")
        elif line.startswith("Pixel Format"):
            parts = line.split(":", 1)[1].strip().split()
            fourcc = parts[0].strip("'") if parts else ""
        elif line.startswith("Frames per second"):
            parts = line.split(":", 1)[1].strip().split()
            fps = parts[0] if parts else ""
    try:
        return fourcc, int(width), int(height), float(fps) if fps else 0.0
    except ValueError:
        return None


def current_mode(device: str) -> Mode | None:
    result = run(["v4l2-ctl", "-d", device, "--get-fmt-video", "--get-parm"])
    if result is None or result.returncode != 0:
        return None
    return parse_current(result.stdout)


def current_format(device: str) -> str:
    mode = current_mode(device)
    if mode is None:
        return ""
    fourcc, width, height, fps = mode
    text = f"{width}x{height} {fourcc}"
    if fps:
        text += f" @ {fps:g} fps"
    return text


def usb_products(vendor: str) -> list[str]:
    products = []
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        try:
            if (dev / "idVendor").read_text().strip() != vendor:
                continue
            products.append((dev / "product").read_text().strip())
        except OSError:
            continue
    return sorted(set(products))


def usb_products_by_id(ids: tuple[tuple[str, str], ...]) -> list[str]:
    """Product names of the plugged-in USB devices whose (vendor, product) id is one of `ids`."""
    products = []
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        try:
            found = (dev / "idVendor").read_text().strip().lower(), (dev / "idProduct").read_text().strip().lower()
            if found in ids:
                products.append((dev / "product").read_text().strip())
        except OSError:
            continue
    return sorted(set(products))


def canon_products() -> list[str]:
    """The Canon cameras on USB: found by the id the camera profile lists, else any Canon device (a model the
    profiles do not know yet is still worth a try with gphoto2)."""
    try:
        from streamstack.hardware import camera

        ids = camera.canon_ids()
    except Exception as exc:  # the catalogue is data; whatever is wrong with it, fall back to the vendor id
        log.warning("Canon USB ids not read from the hardware profiles: %s: %s", type(exc).__name__, exc)
        ids = ()
    return usb_products_by_id(ids) or usb_products(CANON_USB_VENDOR)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def audio_sources() -> dict[str, str]:
    """Map PipeWire/Pulse source names to descriptions."""
    result = run(["pactl", "-f", "json", "list", "sources"])
    if result is None or result.returncode != 0:
        return {}
    try:
        sources = json.loads(result.stdout)
    except ValueError:
        return {}
    return {s.get("name", ""): s.get("description", "") for s in sources}


def default_device(kind: str) -> str:
    result = run(["pactl", f"get-default-{kind}"])
    return result.stdout.strip() if result and result.returncode == 0 else ""


def is_capture_card_audio(name: str, description: str = "") -> bool:
    """HDMI sound from an Elgato Cam Link: it is the camera's audio, never a call microphone."""
    lowered = f"{name} {description}".lower()
    return "cam_link" in lowered or "cam link" in lowered


def capture_card_audio(sources: dict[str, str]) -> tuple[str, str]:
    """(source name, description) of the Cam Link's HDMI audio, ("", "") when it has none."""
    for name, desc in sources.items():
        if not name.endswith(".monitor") and is_capture_card_audio(name, desc):
            return name, desc
    return "", ""


def pick_microphone(sources: dict[str, str], wanted: str = "", default: str = "") -> str:
    """The raw call microphone: the configured one, else an Elgato Wave, else the default input.

    Monitors, StreamStack's own voice source and capture-card HDMI audio never qualify.
    """

    def usable(name: str, desc: str) -> bool:
        return not (name.endswith(".monitor") or name == VOICE_SOURCE or is_capture_card_audio(name, desc))

    candidates = {n: d for n, d in sources.items() if usable(n, d)}
    needle = (wanted or "wave").lower()
    for name, desc in candidates.items():
        if name.lower() == needle or needle in (name + " " + desc).lower():
            return name
    if wanted:
        return ""  # the configured microphone is not plugged in; never guess another
    return default if default in candidates else ""


def legacy_microphone(cfg: dict[str, str]) -> str:
    """The name rule, for when the hardware probe cannot run (`microphone` prefers the Plan)."""
    return pick_microphone(audio_sources(), cfg.get("STREAMSTACK_MICROPHONE", ""), default_device("source"))


def microphone(cfg: dict[str, str]) -> str:
    """The raw call microphone for this machine: the one the hardware Plan names by its interface's USB id."""
    from streamstack.hardware import voice  # late: the hardware package imports this module

    return voice.call_microphone(cfg)


def record(source: str, seconds: float, path: str, *, grace: tuple[float, float, float] = GRACE) -> bool:
    """Record `seconds` of `source` into a WAV file; True when real audio arrived.

    The capture has a hard deadline: after `seconds` it is asked to finish (SIGINT lets parecord close
    the file), and if the audio server is wedged and it does not, it is terminated and killed. A
    capture that never delivers data is therefore never left running.
    """
    try:
        proc = subprocess.Popen(
            ["parecord", f"--device={source}", "--file-format=wav", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        pass
    finally:
        reap(proc, grace)
    try:
        return Path(path).stat().st_size > 1024
    except OSError:
        return False


def play(
    sink: str,
    path: str,
    timeout: float,
    stop: threading.Event | None = None,
    *,
    grace: tuple[float, float, float] = GRACE,
) -> str | None:
    """Play a WAV file on one sink only (the default output is not touched); None when it played, else why not.

    The player has a hard deadline: a server that never lets it finish gets it reaped after `timeout` seconds. Setting
    `stop` ends it early (that is not an error, so it answers None too).
    """
    try:
        proc = subprocess.Popen(
            ["pw-play", "--target", sink, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return f"pw-play could not start ({exc})."
    deadline = time.monotonic() + timeout
    waiter = stop if stop is not None else threading.Event()
    try:
        while proc.poll() is None:
            if waiter.wait(0.05):
                return None
            if time.monotonic() >= deadline:
                return f"The audio server did not finish playing within {timeout:g} s."
    finally:
        reap(proc, grace)
    detail = ""
    if proc.stderr is not None:
        with contextlib.closing(proc.stderr):
            detail = (proc.stderr.read().decode("utf-8", "replace").strip().splitlines() or [""])[-1]
    if proc.returncode != 0:
        return (
            f"The audio server refused to play on it{': ' + detail if detail else ''} (pw-play exit {proc.returncode})."
        )
    return None
