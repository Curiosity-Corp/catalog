"""The two devices calls see: "StreamStack Camera" (a v4l2loopback node) and "StreamStack Mic" (a PipeWire source).

Design and evidence: docs/adr/0004-virtual-camera-and-mic.md. This module answers three questions, read-only and
without ever opening a /dev/video node (so it cannot steal the camera or change a format):

* Is each device set up the way Teams and Zoom need? (`camera_report`, `mic_report`, `checks`)
* Can this user open it? A loopback node is `root:video 0660`; without the `video` group or a seat ACL no app of this
  user can list the camera, and nothing else in the rig says why.
* Who is using it right now? (`seen_by_apps`: V4L2 holders from /proc, microphone readers from PipeWire links.)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import camerause, system

log = logging.getLogger(__name__)

MIC_NODE = system.VOICE_SOURCE
MIC_DESCRIPTION = "StreamStack Mic"
LOOPBACK_PARAMS = Path("/sys/module/v4l2loopback/parameters")
# Pixel formats every call client takes from a V4L2 camera (Chrome and Firefox convert; Zoom reads YUYV and MJPEG).
CALL_FORMATS = ("YUYV", "NV12", "MJPG", "YU12", "UYVY")
MIN_HEIGHT = 720  # Teams negotiates 720p at most; anything smaller looks soft in a call


def _param(name: str, root: Path = LOOPBACK_PARAMS) -> list[str]:
    try:
        return [part.strip() for part in (root / name).read_text().strip().split(",")]
    except OSError:
        return []


def exclusive_caps(device: str, params: Path = LOOPBACK_PARAMS) -> bool | None:
    """Is exclusive_caps on for this loopback node? The module keeps its arrays in creation order, so the entry is the
    one at the position of this node's number in `video_nr`. None when the module or the entry cannot be read."""
    number = Path(device).resolve().name.removeprefix("video")
    numbers, flags = _param("video_nr", params), _param("exclusive_caps", params)
    if number not in numbers or not flags:
        return None
    index = numbers.index(number)
    return flags[index].upper() in ("Y", "1") if index < len(flags) else None


def can_open(device: str) -> bool:
    """Read and write access for this process: what Chrome, Firefox and Zoom need (they open it read-write)."""
    return os.access(device, os.R_OK | os.W_OK)


@dataclass
class CameraReport:
    device: str
    expected_label: str
    name: str = ""  # what the kernel reports; empty when the node does not exist
    exclusive_caps: bool | None = None
    access: bool = False
    producer: bool | None = None
    mode: str = ""
    modes: list[system.Mode] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.name)


def camera_report(device: str, label: str, *, probe_modes: bool = True) -> CameraReport:
    report = CameraReport(device=device, expected_label=label, name=system.video_name(device))
    if not report.present:
        return report
    report.exclusive_caps = exclusive_caps(device)
    report.access = can_open(device)
    if report.access:
        report.producer = system.loopback_has_producer(device)
        if report.producer and probe_modes:
            report.mode = system.current_format(device)
            report.modes = system.camera_modes(device)
    return report


def camera_checks(report: CameraReport) -> list[dict[str, str]]:
    """Doctor items for the camera, beyond the existing "is it fed" check. Ids start with `virtual-camera`."""

    def item(cid: str, state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
        return {
            "id": f"virtual-camera-{cid}",
            "group": "video",
            "state": state,
            "title": title,
            "detail": detail,
            "hint": hint,
        }

    if not report.present:
        return []
    out = []
    if report.name != report.expected_label:
        out.append(
            item(
                "label",
                "fail",
                f"{report.device} is named {report.name!r}; calls look for {report.expected_label!r}",
                "the name is fixed when the v4l2loopback module loads (card_label)",
                "install packaging/modprobe.d/streamstack-v4l2loopback.conf, then reload the module (docs/adr/0004)",
            )
        )
    if report.exclusive_caps is False:
        out.append(
            item(
                "exclusive-caps",
                "fail",
                f"{report.device} has exclusive_caps off, so Chrome, Teams and Zoom do not list it",
                "without it the node claims to be capture and output at once",
                "load v4l2loopback with exclusive_caps=1 for this device (packaging/modprobe.d)",
            )
        )
    if not report.access:
        out.append(
            item(
                "access",
                "fail",
                f"this user cannot open {report.device}, so Teams and Zoom cannot list or use it",
                "the node is root:video 0660 and this session has neither the video group nor a seat ACL",
                "host change: add the user to the video group (ansible catalog, streamstack role); see docs/adr/0004",
            )
        )
        return out
    if report.producer and report.modes:
        formats = {m[0] for m in report.modes}
        heights = [m[2] for m in report.modes]
        fps = [m[3] for m in report.modes]
        if not formats & set(CALL_FORMATS):
            out.append(
                item("format", "warn", f"{report.device} offers {', '.join(sorted(formats))}", "", "feed YUYV or NV12")
            )
        elif max(heights) < MIN_HEIGHT:
            out.append(
                item("format", "warn", f"{report.device} offers only {max(heights)}p; calls want 720p or 1080p", "", "")
            )
        elif max(fps) < 24:
            out.append(item("format", "warn", f"{report.device} runs at {max(fps):g} fps; calls want 30", "", ""))
        else:
            out.append(
                item("format", "ok", f"{report.name} offers {report.mode}", "listed by Chrome, Firefox and Zoom")
            )
    return out


# ---------------------------------------------------------------------------
# The microphone
# ---------------------------------------------------------------------------


@dataclass
class MicReport:
    present: bool = False
    description: str = ""
    monitor: bool = False  # True would be a "Monitor of ..." source, which browsers hide
    is_default: bool = False
    state: str = ""
    channels: int = 0
    rate: int = 0


def mic_report(sources: list[dict] | None = None, default: str | None = None) -> MicReport:
    """The voice chain's source as an app sees it, from `pactl -f json list sources` (injectable for tests)."""
    if sources is None:
        result = system.run(["pactl", "-f", "json", "list", "sources"])
        try:
            sources = json.loads(result.stdout) if result and result.returncode == 0 else []
        except ValueError:
            sources = []
    default = system.default_device("source") if default is None else default
    for src in sources:
        if src.get("name") != MIC_NODE:
            continue
        spec = src.get("sample_specification", "")  # "float32le 1ch 48000Hz"
        parts = spec.split()
        channels = int(parts[1].removesuffix("ch")) if len(parts) == 3 and parts[1].endswith("ch") else 0
        rate = int(parts[2].removesuffix("Hz")) if len(parts) == 3 and parts[2].endswith("Hz") else 0
        return MicReport(
            present=True,
            description=src.get("description", ""),
            monitor=bool(src.get("monitor_of_sink")),
            is_default=src.get("name") == default,
            state=src.get("state", ""),
            channels=channels,
            rate=rate,
        )
    return MicReport()


def mic_checks(report: MicReport) -> list[dict[str, str]]:
    def item(cid: str, state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
        return {
            "id": f"virtual-mic-{cid}",
            "group": "audio",
            "state": state,
            "title": title,
            "detail": detail,
            "hint": hint,
        }

    if not report.present:
        return []
    if report.monitor:
        return [item("monitor", "fail", "StreamStack Mic is a monitor source, which Chrome and Zoom hide")]
    if report.description != MIC_DESCRIPTION:
        return [
            item(
                "label",
                "fail",
                f"the voice chain's source is called {report.description!r}, calls look for {MIC_DESCRIPTION!r}",
                "",
                "streamstack chain restart",
            )
        ]
    detail = f"{report.channels} ch {report.rate} Hz; " + (
        "the default microphone" if report.is_default else "pick it in the call app"
    )
    return [item("listed", "ok", "StreamStack Mic is a plain input source that apps list", detail)]


# ---------------------------------------------------------------------------
# Who is using them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    kind: str  # "camera" or "mic"
    app: str
    pid: int | None
    via: str  # "v4l2" (an open /dev/video fd) or "pipewire" (a link)

    def public(self) -> dict[str, object]:
        return {"kind": self.kind, "app": self.app, "pid": self.pid, "via": self.via}


def _pw_dump() -> list[dict]:
    result = system.run(["pw-dump"], timeout=5)
    if result is None or result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return [obj for obj in data if isinstance(obj, dict)]


def pipewire_readers(objects: list[dict], kind: str, wanted: Callable[[dict], bool]) -> list[Seen]:
    """Streams linked to the output of the nodes whose properties satisfy `wanted`, from `pw-dump` objects."""
    nodes = {obj["id"]: obj.get("info", {}).get("props", {}) for obj in objects if obj.get("type", "").endswith("Node")}
    ids = {nid for nid, props in nodes.items() if wanted(props)}
    seen: dict[tuple[str, int | None], Seen] = {}
    for obj in objects:
        if not obj.get("type", "").endswith("Link"):
            continue
        info = obj.get("info", {})
        if info.get("output-node-id") not in ids:
            continue
        props = nodes.get(info.get("input-node-id"), {})
        pid_text = props.get("application.process.id") or props.get("pipewire.sec.pid")
        pid = int(pid_text) if str(pid_text or "").isdigit() else None
        raw = props.get("application.process.binary") or props.get("application.name") or props.get("node.name", "")
        if str(raw).startswith("streamstack"):
            continue  # StreamStack's own meters and recorder are not apps
        app = camerause.friendly(str(raw))
        seen[(app, pid)] = Seen(kind, app, pid, "pipewire")
    return list(seen.values())


def seen_by_apps(
    cfg: dict[str, str],
    *,
    scanner: camerause.ProcScanner | None = None,
    objects: Callable[[], list[dict]] = _pw_dump,
) -> list[Seen]:
    """Every app that has the program camera or StreamStack Mic open right now."""
    found: list[Seen] = []
    scan = (scanner or camerause.ProcScanner()).scan({cfg["STREAMSTACK_PROGRAM_DEVICE"]})
    found += [Seen("camera", c.app, c.pid, "v4l2") for c in scan.consumers]
    graph = objects()
    found += pipewire_readers(graph, "mic", lambda props: props.get("node.name") == MIC_NODE)
    # a browser using PipeWire's camera path reads the same node through WirePlumber's V4L2 monitor
    label = cfg["STREAMSTACK_PROGRAM_LABEL"]
    known = {(f.app, f.pid) for f in found}
    found += [
        s
        for s in pipewire_readers(
            graph,
            "camera",
            lambda props: (
                props.get("media.class") == "Video/Source"
                and label in (props.get("node.description"), props.get("node.nick"))
            ),
        )
        if (s.app, s.pid) not in known
    ]
    return found


def seen_check(seen: list[Seen]) -> dict[str, str]:
    cams = sorted({s.app for s in seen if s.kind == "camera"})
    mics = sorted({s.app for s in seen if s.kind == "mic"})
    if not seen:
        return {
            "id": "virtual-seen",
            "group": "apps",
            "state": "ok",
            "title": "seen by apps",
            "detail": "no app has StreamStack Camera or StreamStack Mic open",
            "hint": "",
        }
    parts = []
    if cams:
        parts.append("camera: " + ", ".join(cams))
    if mics:
        parts.append("microphone: " + ", ".join(mics))
    return {
        "id": "virtual-seen",
        "group": "apps",
        "state": "ok",
        "title": "seen by apps",
        "detail": "; ".join(parts),
        "hint": "",
    }


def snapshot(cfg: dict[str, str]) -> dict[str, object]:
    """The Console's `virtual` state: how each device looks to apps, and who has it open. Never raises."""
    try:
        cam = camera_report(cfg["STREAMSTACK_PROGRAM_DEVICE"], cfg["STREAMSTACK_PROGRAM_LABEL"], probe_modes=False)
        mic = mic_report()
        seen = seen_by_apps(cfg)
    except Exception as exc:  # a probe failing must not blank the System page
        log.warning("virtual device probe failed: %s", exc)
        return {"error": str(exc), "seen": []}
    return {
        "camera": {
            "label": cam.name or cam.expected_label,
            "device": cam.device,
            "present": cam.present,
            "listed": bool(cam.present and cam.name == cam.expected_label and cam.exclusive_caps is not False),
            "access": cam.access,
            "producer": cam.producer,
        },
        "mic": {"label": mic.description or MIC_DESCRIPTION, "present": mic.present, "default": mic.is_default},
        "seen": [s.public() for s in seen],
    }
