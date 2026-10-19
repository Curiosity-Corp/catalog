"""Read-only probes of cameras, audio devices and helper processes."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

CAMLINK_LINK = Path("/dev/streamstack/camlink")
EE_SOURCE = "easyeffects_source"
CANON_USB_VENDOR = "04a9"

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


def run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


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


def loopback_devices() -> list[str]:
    """The v4l2loopback nodes; OBS feeds the first one it finds free."""
    found = []
    for device in video_devices():
        result = run(["v4l2-ctl", "-d", device, "--info"])
        if result and result.returncode == 0 and re.search(r"Driver name\s*:\s*v4l2 loopback", result.stdout):
            found.append(device)
    return found


def opened_by(process: str, devices: list[str]) -> list[str]:
    """Which of `devices` this user's `process` holds open."""
    pids = run(["pgrep", "-u", str(os.getuid()), "-x", process])
    if not pids or pids.returncode != 0:
        return []
    wanted = {str(Path(d).resolve()) for d in devices}
    held = set()
    for pid in pids.stdout.split():
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target in wanted:
                    held.add(target)
        except OSError:
            continue
    return sorted(held)


def find_camlink() -> str:
    if CAMLINK_LINK.exists():
        return str(CAMLINK_LINK)
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


def current_format(device: str) -> str:
    result = run(["v4l2-ctl", "-d", device, "--get-fmt-video", "--get-parm"])
    if result is None or result.returncode != 0:
        return ""
    width = height = fourcc = fps = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Width/Height"):
            width, _, height = line.split(":", 1)[1].strip().partition("/")
        elif line.startswith("Pixel Format"):
            fourcc = line.split(":", 1)[1].strip().split()[0].strip("'")
        elif line.startswith("Frames per second"):
            fps = line.split(":", 1)[1].strip().split()[0]
    if not width:
        return ""
    text = f"{width}x{height} {fourcc}"
    if fps:
        text += f" @ {float(fps):g} fps"
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


def pick_microphone(sources: dict[str, str], wanted: str = "", default: str = "") -> str:
    """The raw call microphone: the configured one, else an Elgato Wave, else the default input.

    Monitors, the Easy Effects output and capture-card HDMI audio never qualify.
    """

    def usable(name: str) -> bool:
        lowered = name.lower()
        return not (name.endswith(".monitor") or name == EE_SOURCE or "cam_link" in lowered)

    candidates = {n: d for n, d in sources.items() if usable(n)}
    needle = (wanted or "wave").lower()
    for name, desc in candidates.items():
        if name.lower() == needle or needle in (name + " " + desc).lower():
            return name
    if wanted:
        return ""  # the configured microphone is not plugged in; never guess another
    return default if default in candidates else ""


def microphone(cfg: dict[str, str]) -> str:
    return pick_microphone(audio_sources(), cfg.get("STREAMSTACK_MICROPHONE", ""), default_device("source"))


def easyeffects_running() -> bool:
    return running("easyeffects")


def easyeffects_preset() -> str:
    """The input preset Easy Effects loaded last ("" when it is not running)."""
    if not easyeffects_running():
        return ""
    result = run(["easyeffects", "-a", "input"], timeout=5)
    return result.stdout.strip() if result and result.returncode == 0 else ""


def ee_presets() -> list[str]:
    local = Path.home() / ".local/share/easyeffects/input"
    return sorted(p.stem for p in local.glob("*.json"))


def record(source: str, seconds: float, path: str) -> bool:
    proc = subprocess.Popen(
        ["parecord", f"--device={source}", "--file-format=wav", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    return Path(path).exists() and Path(path).stat().st_size > 1024
