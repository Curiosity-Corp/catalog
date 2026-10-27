"""The Canon body in the Console: settings and bridge health, over the bridge's local socket.

CanonLink   the real thing. A background poll keeps a cached `state()` (the snapshot never waits on USB); the
            `video.canon_*` actions are forwarded to the bridge, which owns the camera.
DemoCanon   a simulated body for --demo, tests and the e2e suite.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from streamstack.canon import controls as ctl
from streamstack.canon import ipc
from streamstack.console.errors import ActionError

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]
Request = Callable[..., dict[str, Any]]

POLL = 1.0


def empty_canon(state: str = "disabled", message: str | None = None) -> dict[str, Any]:
    """The `video.canon` snapshot with every key present (docs/console.md)."""
    return {
        "available": False,
        "state": state,  # disabled | offline | starting | running | paused | stalled | stopped
        "message": message,
        "session": None,  # shared | pause
        "usb": [],
        "decode": {"path": None, "label": None, "tried": [], "render_node": None, "ffmpeg": None},
        "input": None,
        "output": None,
        "fps_in": 0.0,
        "fps_out": 0.0,
        "grab_ms": 0.0,
        "pipe_ms": 0.0,
        "cpu": {},
        "frames": 0,
        "restarts": {"source": 0, "encoder": 0},
        "stalls": 0,
        "last_error": None,
        "uptime_s": 0,
        "controls": {},
    }


def from_status(status: dict[str, Any]) -> dict[str, Any]:
    out = empty_canon()
    out.update({k: status[k] for k in out if k in status})
    out["available"] = True
    return out


def need_text(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ActionError(f"{key} is required", 400)
    return value.strip()


class CanonLink:
    def __init__(self, cfg: dict, notify: Notify | None = None, request: Request = ipc.request) -> None:
        self.cfg = cfg
        self._notify = notify
        self._request = request
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = str(cfg.get("STREAMSTACK_CANON_BRIDGE", "gphoto2")) == "gphoto2" and str(
            cfg.get("STREAMSTACK_CAMERA_EXTERNAL", "false")
        ).lower() not in ("1", "true", "yes", "on")
        self._state = empty_canon("offline" if self.enabled else "disabled")

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True, name="streamstack-canon")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(POLL)
            self._wake.clear()

    def refresh(self) -> None:
        try:
            answer = self._request({"op": "status"}, timeout=3.0)
        except ipc.BridgeUnavailable:
            new = empty_canon("offline", "The Canon bridge is not running")
        else:
            new = (
                from_status(answer["status"])
                if answer.get("ok") and "status" in answer
                else empty_canon("offline", str(answer.get("error") or "The Canon bridge did not answer"))
            )
        with self._lock:
            self._state = new

    def act(self, action: str, params: dict) -> str | None:
        if not self.enabled:
            raise ActionError("The Canon bridge is not used on this rig", 409)
        if action == "video.canon_set":
            payload: dict[str, Any] = {"op": "set", "id": need_text(params, "id"), "value": params.get("value")}
        elif action == "video.canon_action":
            payload = {"op": "action", "name": need_text(params, "name")}
            if "step" in params:
                payload["step"] = params["step"]
        elif action == "video.canon_refresh":
            payload = {"op": "refresh"}
        else:
            raise ActionError(f"unknown action: {action}", 400)
        try:
            answer = self._request(payload, timeout=25.0)
        except ipc.BridgeUnavailable as exc:
            raise ActionError(f"The Canon bridge is not running ({exc})", 409) from exc
        self._wake.set()
        if not answer.get("ok"):
            raise ActionError(str(answer.get("error") or "The camera refused that"), 409)
        return answer.get("message")


# ---------- demo ----------
DEMO_CHOICES: dict[str, list[str]] = {
    "iso": ["100", "125", "160", "200", "250", "320", "400", "500", "640", "800", "1000", "1250", "1600", "3200"],
    "shutterspeed": ["1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125", "1/160", "1/200", "1/250", "1/500"],
    "aperture": ["1.8", "2", "2.2", "2.5", "2.8", "3.2", "3.5", "4", "4.5", "5", "5.6", "6.3", "7.1", "8", "11"],
    "exposurecompensation": [
        "-2",
        "-1.66",
        "-1.33",
        "-1",
        "-0.66",
        "-0.33",
        "0",
        "0.33",
        "0.66",
        "1",
        "1.33",
        "1.66",
        "2",
    ],
    "whitebalance": ["Auto", "Daylight", "Shadow", "Cloudy", "Tungsten", "Fluorescent", "Flash", "Color Temperature"],
    "picturestyle": ["Standard", "Portrait", "Landscape", "Neutral", "Faithful", "Monochrome", "User defined 1"],
    "focusmode": ["One Shot", "AI Servo", "Manual"],
    "liveviewsize": ["Large", "Medium", "Small"],
}
DEMO_DIALS = ("Manual", "Shutter priority", "Aperture priority", "Program")
DEMO_VALUES = {
    "iso": "400",
    "shutterspeed": "1/60",
    "aperture": "2.8",
    "exposurecompensation": "0",
    "whitebalance": "Color Temperature",
    "colortemperature": "5200",
    "picturestyle": "Neutral",
    "focusmode": "One Shot",
    "liveviewsize": "Large",
    "cameramodel": "Canon EOS 50D",
    "lensname": "EF50mm f/1.8 II",
    "batterylevel": "75%",
    "autoexposuremode": "Manual",
}


class DemoCanon:
    """A Canon in memory. Aperture is read-only while the mode dial reads Tv, like the real body."""

    enabled = True

    def __init__(self, notify: Notify | None = None) -> None:
        self._notify = notify
        self.lock = threading.Lock()
        self.values = dict(DEMO_VALUES)
        self.focus = 0
        self.af_runs = 0
        self.started = time.monotonic()

    def start(self) -> None:
        """Nothing runs in the background."""

    def stop(self) -> None:
        """Nothing to stop."""

    def _view(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for c in ctl.CONTROLS:
            readonly = not c.writable
            reason = None
            dial = self.values["autoexposuremode"]
            if c.key == "aperture" and dial not in ("Manual", "Aperture priority"):
                readonly = True
            if c.key == "shutterspeed" and dial not in ("Manual", "Shutter priority"):
                readonly = True
            if c.prereq:
                readonly = False
            if readonly and c.writable:
                reason = c.readonly_reason
            out[c.id] = {
                "id": c.id,
                "key": c.key,
                "label": c.label,
                "group": c.group,
                "value": self.values[c.key],
                "choices": list(DEMO_CHOICES.get(c.key, [])),
                "range": list(ctl.KELVIN_RANGE) if c.numeric else None,
                "readonly": readonly,
                "reason": reason,
                "hint": ctl.HINTS.get(c.key),
            }
        return out

    def state(self) -> dict[str, Any]:
        with self.lock:
            out = empty_canon("running")
            out.update(
                available=True,
                session="shared",
                usb=["04a9:319b Canon EOS 50D"],
                decode={
                    "path": "qsv",
                    "label": "GPU (Intel Quick Sync)",
                    "tried": [{"name": "qsv", "ok": True, "reason": None}],
                    "render_node": "/dev/dri/renderD128",
                    "ffmpeg": "8.0",
                },
                input={"width": 1056, "height": 704, "subsampling": "4:2:2"},
                output={"width": 1056, "height": 704, "mode": "native"},
                fps_in=29.9,
                fps_out=29.9,
                grab_ms=31.0,
                pipe_ms=0.4,
                cpu={"bridge": 6.1, "ffmpeg": 3.2, "gphoto2": None, "total": 9.3},
                frames=int((time.monotonic() - self.started) * 30),
                uptime_s=int(time.monotonic() - self.started),
                controls=self._view(),
            )
            return out

    def act(self, action: str, params: dict) -> str | None:
        with self.lock:
            if action == "video.canon_set":
                return self._set(need_text(params, "id"), params.get("value"))
            if action == "video.canon_action":
                return self._action(params)
            if action == "video.canon_refresh":
                return None
            raise ActionError(f"unknown action: {action}", 400)

    def _set(self, cid: str, value: object) -> str | None:
        control = ctl.BY_ID.get(cid)
        if control is None or not control.writable:
            raise ActionError(f"{cid!r} is not a camera setting", 400)
        view = self._view()[cid]
        if view["readonly"]:
            raise ActionError(control.readonly_reason, 409)
        if control.numeric:
            try:
                text = str(ctl.snap_kelvin(value))
            except ValueError as exc:
                raise ActionError(str(exc), 400) from exc
            self.values["whitebalance"] = ctl.WB_KELVIN_CHOICE
        else:
            match = next((c for c in view["choices"] if c.lower() == str(value).lower()), None)
            if match is None:
                raise ActionError(f"{control.label}: the camera offers {', '.join(view['choices'])}", 400)
            text = match
        self.values[control.key] = text
        return None

    def _action(self, params: dict) -> str | None:
        name = need_text(params, "name")
        if name == "autofocus":
            self.af_runs += 1
        elif name == "af_cancel":
            return None
        elif name == "demo_dial":  # demo only: turn the body's mode dial, to see read-only controls explain themselves
            mode = need_text(params, "mode")
            if mode not in DEMO_DIALS:
                raise ActionError(f"mode must be one of {list(DEMO_DIALS)}", 400)
            self.values["autoexposuremode"] = mode
        elif name == "focus":
            step = params.get("step")
            if isinstance(step, bool) or not isinstance(step, int) or step not in ctl.FOCUS_STEPS:
                raise ActionError(f"focus step must be one of {list(ctl.FOCUS_STEPS)}", 400)
            self.focus += step
        else:
            raise ActionError(f"unknown camera action {name!r}", 400)
        return None
