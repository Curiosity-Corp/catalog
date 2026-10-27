"""The caps and format contract of StreamStack Camera for Chromium, Edge, Teams and Zoom (#86).

Fixed by ADR 0004 and measured in docs/evidence/virtual-devices: the node offers **one** format, written by its
producer. What the call apps need from it:

* `exclusive_caps=1` (otherwise Chrome, Teams and Zoom do not list the node);
* a pixel format they convert without help: YUYV (what the engine writes) or NV12. MJPEG, UYVY, planar and GREY are
  deliberately not part of the contract: Zoom reads YUYV and MJPEG, Chromium converts YUYV and NV12 on every build
  tested, and a loopback with a different format after a producer change is how "the camera is green" bugs start;
* a size from 640x360 to 3840x2160 with even sides (Chromium scales to the 1280x720 that Teams asks for);
* 24 to 60 frames per second (calls drop below 24 fps and every encoder budget assumes at most 60).

`violations` is pure; `checks` turns it into doctor rows and is fed from `virtualdevices.camera_report`.
"""

from __future__ import annotations

from streamstack import system

FORMATS = ("YUYV", "NV12")
MIN_SIZE = (640, 360)
MAX_SIZE = (3840, 2160)
FPS = (24.0, 60.0)


def violations(modes: list[system.Mode]) -> list[str]:
    """What breaks the contract in the modes a node offers, as sentences; empty when it conforms."""
    if not modes:
        return []
    out: list[str] = []
    formats = sorted({m[0] for m in modes})
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        out.append(f"offers {', '.join(bad)}; the contract is {' or '.join(FORMATS)}")
    sizes = sorted({(m[1], m[2]) for m in modes})
    if len(sizes) > 1:
        out.append("offers more than one size (" + ", ".join(f"{w}x{h}" for w, h in sizes) + "); a loopback offers one")
    for w, h in sizes:
        if w % 2 or h % 2:
            out.append(f"{w}x{h} has an odd side")
        elif w < MIN_SIZE[0] or h < MIN_SIZE[1]:
            out.append(f"{w}x{h} is smaller than {MIN_SIZE[0]}x{MIN_SIZE[1]}")
        elif w > MAX_SIZE[0] or h > MAX_SIZE[1]:
            out.append(f"{w}x{h} is larger than {MAX_SIZE[0]}x{MAX_SIZE[1]}")
    for fps in sorted({m[3] for m in modes if m[3]}):
        if not FPS[0] <= fps <= FPS[1]:
            out.append(f"{fps:g} fps is outside {FPS[0]:g} to {FPS[1]:g}")
    return out


def check(device: str, name: str, exclusive_caps: bool | None, modes: list[system.Mode]) -> dict[str, str]:
    """One doctor row, `v4l2loopback-contract`, for a loopback node."""
    problems = violations(modes)
    if exclusive_caps is False:
        problems.insert(0, "exclusive_caps is off")
    base = {"id": "v4l2loopback-contract", "group": "video"}
    if problems:
        return {
            **base,
            "state": "fail",
            "title": f"{name} ({device}) breaks the call-app contract: " + "; ".join(problems),
            "detail": "docs/camera-delivery.md, ADR 0004",
            "hint": "restart the video engine at 1920x1080 or 1280x720 YUYV (STREAMSTACK_ENGINE_SIZE), and load "
            "v4l2loopback from packaging/modprobe.d/streamstack-v4l2loopback.conf",
        }
    shown = ", ".join(sorted({f"{m[1]}x{m[2]} {m[0]} {m[3]:g} fps" for m in modes})) or "no format yet (not fed)"
    return {**base, "state": "ok", "title": f"{name} meets the call-app contract", "detail": shown, "hint": ""}


def report_check(report) -> list[dict[str, str]]:
    """The contract row for a `virtualdevices.CameraReport`, only while the node is fed and readable."""
    if not (report.present and report.access and report.producer and report.modes):
        return []
    return [check(report.device, report.name, report.exclusive_caps, report.modes)]
