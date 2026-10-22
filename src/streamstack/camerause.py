"""Is the camera in use right now? A read-only answer from /proc and, optionally, OBS.

Camera follow (`streamstack.lightauto`) turns the lights on when something starts using the camera. Three signals:

* **Apps.** Any process of this user that holds a watched video node open: the program camera (the OBS virtual
  camera device, `STREAMSTACK_PROGRAM_DEVICE`), the raw camera (`STREAMSTACK_CAMERA_DEVICE`, unless another tool feeds
  it) and any extra device the person listed. The scan reads `/proc/<pid>/fd` symlinks and nothing else: **it never
  opens a /dev/video node**, so it cannot steal the camera, wake it or change a format.
* **OBS outputs.** Virtual camera, streaming and recording, from obs-websocket, only while OBS is running.
* Neither signal needs the Console.

Handles that are StreamStack's own plumbing are not "use": OBS itself (its virtual camera output holds the program
node open for writing, and it reads the raw camera as a source), any `streamstack` process, and the ffmpeg or GStreamer
helpers those start (the Canon bridge writes the raw node, the Console grabs preview frames). A helper is recognised
by its ancestry: a `ffmpeg` whose parent chain reaches a StreamStack or OBS process is plumbing, one started from a
terminal is a consumer.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from streamstack.obs import OBS

log = logging.getLogger(__name__)

PROC = Path("/proc")
OBS_COMM = ("obs", "obs-ffmpeg-mux")
HELPER_COMM = ("ffmpeg", "ffprobe", "ffplay", "mpv", "gst-launch-1.0", "gst-launch-1", "vlc")
ANCESTRY_DEPTH = 8
OBS_RETRY_SECONDS = 15.0

# What a process is called in the status line: the process name (comm) to a name a person recognises.
APP_NAMES: tuple[tuple[str, str], ...] = (
    ("ms-teams", "Teams"),
    ("teams-for-linux", "Teams"),
    ("teams", "Teams"),
    ("zoom", "Zoom"),
    ("slack", "Slack"),
    ("discord", "Discord"),
    ("skype", "Skype"),
    ("webex", "Webex"),
    ("signal", "Signal"),
    ("telegram", "Telegram"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("chromium", "Chromium"),
    ("brave", "Brave"),
    ("msedge", "Edge"),
    ("vivaldi", "Vivaldi"),
    ("opera", "Opera"),
    ("cheese", "Cheese"),
    ("guvcview", "guvcview"),
    ("kamoso", "Kamoso"),
    ("webcamoid", "Webcamoid"),
    ("ffmpeg", "ffmpeg"),
    ("vlc", "VLC"),
    ("mpv", "mpv"),
)
BROWSERS = ("Firefox", "Chrome", "Chromium", "Brave", "Edge", "Vivaldi", "Opera")


def friendly(comm: str) -> str:
    lowered = comm.lower()
    for needle, name in APP_NAMES:
        if needle in lowered:
            return f"{name} in a browser tab" if name in BROWSERS else name
    return comm


@dataclass(frozen=True)
class Consumer:
    pid: int
    comm: str
    device: str

    @property
    def app(self) -> str:
        return friendly(self.comm)

    def public(self) -> dict[str, object]:
        return {"pid": self.pid, "process": self.comm, "app": self.app, "device": self.device}


@dataclass(frozen=True)
class Usage:
    """What is using the camera right now. `reason` is a sentence for the status line, empty when nothing is."""

    consumers: tuple[Consumer, ...] = ()
    obs: tuple[str, ...] = ()  # "virtual camera", "streaming", "recording"
    obs_known: bool = False  # OBS answered; False when it is closed or the websocket is off

    @property
    def active(self) -> bool:
        return bool(self.consumers or self.obs)

    @property
    def reason(self) -> str:
        parts = []
        names = list(dict.fromkeys(c.app for c in self.consumers))
        if names:
            parts.append(f"{_join(names)} opened the camera")
        if self.obs:
            parts.append(f"OBS {_join(list(self.obs))} started")
        return " and ".join(parts)

    def public(self) -> dict[str, object]:
        return {
            "in_use": self.active,
            "reason": self.reason,
            "apps": [c.public() for c in self.consumers],
            "obs": list(self.obs),
            "obs_known": self.obs_known,
        }


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------
# /proc
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _argv(proc: Path, pid: int) -> list[str]:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _ppid(proc: Path, pid: int) -> int:
    """The parent pid from /proc/<pid>/stat (the name field can contain spaces and parentheses)."""
    text = _read(proc / str(pid) / "stat")
    _, _, rest = text.rpartition(")")
    fields = rest.split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def is_streamstack_command(argv: list[str]) -> bool:
    """`streamstack ...`, `python -m streamstack ...`, `python3 /usr/bin/streamstack-console`"""
    head = [os.path.basename(a) for a in argv[:4]]
    if any(name.startswith("streamstack") for name in head):
        return True
    return any(
        a == "-m" and i + 1 < len(argv) and argv[i + 1].startswith("streamstack") for i, a in enumerate(argv[:4])
    )


@dataclass
class Scan:
    consumers: list[Consumer] = field(default_factory=list)
    obs_running: bool = False


class ProcScanner:
    """Finds the processes holding a watched device open. `proc` and `own_pid` are injectable for tests."""

    def __init__(self, proc: Path = PROC, own_pid: int | None = None) -> None:
        self.proc = proc
        self.own_pid = own_pid if own_pid is not None else os.getpid()

    def _pids(self) -> Iterable[int]:
        try:
            names = os.listdir(self.proc)
        except OSError as exc:
            log.warning("cannot list %s: %s", self.proc, exc)
            return []
        return sorted(int(n) for n in names if n.isdigit())

    def _is_plumbing(self, pid: int, comm: str) -> bool:
        """StreamStack's or OBS's own handle: that process, or a media helper started by one of them."""
        if pid == self.own_pid or comm in OBS_COMM or is_streamstack_command(_argv(self.proc, pid)):
            return True
        if comm not in HELPER_COMM:
            return False
        parent = _ppid(self.proc, pid)
        for _ in range(ANCESTRY_DEPTH):
            if parent <= 1:
                return False
            if _read(self.proc / str(parent) / "comm") in OBS_COMM or is_streamstack_command(_argv(self.proc, parent)):
                return True
            parent = _ppid(self.proc, parent)
        return False

    def scan(self, devices: set[str], ignore: Iterable[str] = ()) -> Scan:
        result = Scan()
        skip = set(ignore)
        wanted = {os.path.realpath(d) for d in devices} | set(devices)
        for pid in self._pids():
            comm = _read(self.proc / str(pid) / "comm")
            if comm in OBS_COMM:
                result.obs_running = True
            if not wanted:
                continue
            try:
                fds = os.listdir(self.proc / str(pid) / "fd")
            except OSError:
                continue  # gone, or another user's process
            held = None
            for fd in fds:
                try:
                    target = os.readlink(self.proc / str(pid) / "fd" / fd)
                except OSError:
                    continue
                if target in wanted:
                    held = target
                    break
            if held is None or comm in skip or friendly(comm) in skip:
                continue
            if self._is_plumbing(pid, comm):
                continue
            result.consumers.append(Consumer(pid, comm, held))
        return result


# ---------------------------------------------------------------------------
# OBS
# ---------------------------------------------------------------------------


class ObsProbe(Protocol):
    def state(self) -> dict[str, bool] | None:
        """{"virtualcam": bool, "stream": bool, "record": bool}, or None when OBS cannot be asked."""


class WebsocketObs:
    """obs-websocket 5 through `streamstack.obs.OBS`; a failed connection is retried at most every 15 seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._client: OBS | None = None
        self._retry_at = 0.0

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # a dead socket must never stop the service
                log.debug("closing the OBS websocket: %s", exc)

    def state(self) -> dict[str, bool] | None:
        if self._client is None:
            if self._clock() < self._retry_at:
                return None
            try:
                from streamstack.obs import OBS as ObsClient

                self._client = ObsClient(timeout=2)
            except (ImportError, OSError, ValueError, KeyError, RuntimeError) as exc:
                log.debug("OBS websocket unavailable: %s", exc)
                self._retry_at = self._clock() + OBS_RETRY_SECONDS
                return None
        try:
            return {
                "virtualcam": bool(self._client.call("GetVirtualCamStatus").get("outputActive")),
                "stream": bool(self._client.call("GetStreamStatus").get("outputActive")),
                "record": bool(self._client.call("GetRecordStatus").get("outputActive")),
            }
        except Exception as exc:  # websocket errors come in many types; any failure means "cannot ask right now"
            log.debug("OBS websocket request failed: %s", exc)
            self.close()
            self._retry_at = self._clock() + OBS_RETRY_SECONDS
            return None


OBS_LABELS = {"virtualcam": "virtual camera", "stream": "streaming", "record": "recording"}


# ---------------------------------------------------------------------------
# Together
# ---------------------------------------------------------------------------


class CameraWatcher:
    """One `poll()` per tick: the scan plus the OBS outputs, filtered by the camera-follow settings."""

    def __init__(self, scanner: ProcScanner | None = None, obs: ObsProbe | None = None) -> None:
        self.scanner = scanner or ProcScanner()
        self.obs = obs if obs is not None else WebsocketObs()

    def poll(self, follow: dict, devices: set[str]) -> Usage:
        consumers: list[Consumer] = []
        obs_running = False
        if follow.get("apps", True) or follow.get("virtualcam") or follow.get("stream") or follow.get("record"):
            found = self.scanner.scan(devices | set(follow.get("devices", [])), set(follow.get("ignore", [])))
            obs_running = found.obs_running
            if follow.get("apps", True):
                consumers = found.consumers
        outputs: list[str] = []
        known = False
        wants_obs = any(follow.get(k) for k in OBS_LABELS)
        if wants_obs and obs_running:
            state = self.obs.state()
            if state is not None:
                known = True
                outputs = [OBS_LABELS[k] for k in OBS_LABELS if follow.get(k) and state.get(k)]
        return Usage(tuple(consumers), tuple(outputs), known)

    def close(self) -> None:
        closer = getattr(self.obs, "close", None)
        if callable(closer):
            closer()


def watched_devices(cfg: dict[str, str]) -> set[str]:
    """The video nodes whose use means "the camera is on": the program camera, and the raw one unless it is external."""
    from streamstack import config

    devices = {cfg["STREAMSTACK_PROGRAM_DEVICE"]}
    if not config.camera_external(cfg):
        devices.add(cfg["STREAMSTACK_CAMERA_DEVICE"])
    return {d for d in devices if d}
