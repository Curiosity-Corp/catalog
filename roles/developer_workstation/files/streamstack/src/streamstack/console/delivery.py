"""Keeps "StreamStack Camera" listed and fed when the real camera goes away (work package C, ADR 0004 addendum).

The failure this exists for (dm313red, 2026-10-27): the external PTP bridge that feeds /dev/video42 died after a
reboot, the engine had nothing to read, nothing wrote /dev/video43, and Teams in Edge listed only the Cam Link: with
`exclusive_caps=1` an unfed node is not a camera. The Console now treats "the program camera is always fed" as its
own job, in three steps that follow the source's health:

1. **Fail over** (STREAMSTACK_CAMERA_FALLBACK=auto, the default): when the camera the person picked has had no
   producer for STREAMSTACK_CAMERA_FALLBACK_AFTER seconds (6) and another camera is delivering (the Cam Link), the
   engine is re-pointed at it, and goes back after the first one has been steady for FAILBACK_AFTER seconds.
   `off` keeps the engine on the camera that was picked.
2. **Cover the gap** (STREAMSTACK_CAMERA_LOSS=slate, the default; `hold` darkens and blurs the last frame instead,
   `off` writes nothing): while no engine child writes the node, the slate (engine/slate.py) does, at the size and rate
   the engine writes, so the node never loses its producer and the call app never loses the device.
3. **Reconnect on its own**: the engine supervisor asks `guard` before every child start; when the camera is back the
   slate is released and the engine child takes the node, no click needed.

Everything here is read-only towards cameras (`v4l2-ctl --info` on a node, `os.path.exists`); only the slate and the
engine child ever write the program node.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from streamstack import system
from streamstack.engine import source
from streamstack.engine.graph import Source, Spec
from streamstack.engine.slate import DEFAULT_FPS, DEFAULT_SIZE, SlateFeeder, SlateSpec

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

FALLBACK_AFTER = 6.0  # seconds without a producer before the engine moves to another camera
FAILBACK_AFTER = 5.0  # seconds the first camera must be steady before the engine moves back
IDLE_FPS = 5.0  # the idle placeholder's rate: enough to keep the node listed, a few percent of the engine's cost
IDLE_CAPTION = "Camera idle"
IDLE_REASON = "It starts when an app opens StreamStack Camera"
LOSS_MODES = ("slate", "hold", "off")
FALLBACK_MODES = ("auto", "off")


def loss_mode(cfg: dict[str, str]) -> str:
    wanted = (cfg.get("STREAMSTACK_CAMERA_LOSS") or "slate").strip().lower()
    if wanted not in LOSS_MODES:
        log.warning("STREAMSTACK_CAMERA_LOSS=%r is not slate, hold or off; using slate", wanted)
        return "slate"
    return wanted


def fallback_mode(cfg: dict[str, str]) -> str:
    wanted = (cfg.get("STREAMSTACK_CAMERA_FALLBACK") or "auto").strip().lower()
    if wanted not in FALLBACK_MODES:
        log.warning("STREAMSTACK_CAMERA_FALLBACK=%r is not auto or off; using auto", wanted)
        return "auto"
    return wanted


def _seconds(cfg: dict[str, str], key: str, default: float) -> float:
    try:
        return max(0.0, float(cfg.get(key) or default))
    except ValueError:
        log.warning("%s=%r is not a number of seconds; using %g", key, cfg.get(key), default)
        return default


class Delivery:
    """The guard between the cameras and the program node. All methods are safe to call from any thread."""

    def __init__(
        self,
        cfg: dict[str, str],
        *,
        slate: SlateFeeder | None = None,
        notify: Notify | None = None,
        probe: Callable[[str], bool | None] = system.loopback_has_producer,
        exists: Callable[[str], bool] = os.path.exists,
        clock: Callable[[], float] = time.monotonic,
        camera_device: Callable[[], str] | None = None,
        program_device: Callable[[], str] | None = None,
        last_frame: Callable[[], bytes | None] = lambda: None,
    ) -> None:
        self.cfg = cfg
        self.slate = slate or SlateFeeder(clock=clock)
        self._notify = notify
        self._probe, self._exists, self._clock = probe, exists, clock
        self._camera_device = camera_device or (lambda: cfg.get("STREAMSTACK_CAMERA_DEVICE", ""))
        self._program_device = program_device or (lambda: cfg.get("STREAMSTACK_PROGRAM_DEVICE", ""))
        self._last_frame = last_frame
        self._lock = threading.RLock()
        self._out: tuple[int, int, float] | None = None  # what the engine last wrote: width, height, fps
        self._lost_since: float | None = None
        self._steady_since: float | None = None
        self._fallback: str | None = None  # label of the camera standing in for the picked one
        self._reason = ""
        self._announced: str | None = None
        self._parked = False  # the slate is the idle placeholder (nothing is wrong), not a "camera reconnecting" card

    # -- is a camera deliverable? -------------------------------------------------------------------------------

    def camera_ready(self, device: str, label: str) -> str | None:
        """None when `device` can be read now, else a sentence saying why not. Read-only."""
        if not device or not self._exists(device):
            return f"{label} is not connected ({device or 'no device'} is absent)"
        if device == self._camera_device() and self._probe(device) is False:
            return f"{label} has no producer on {device}: the program that feeds it is not running"
        return None

    def source_ready(self, src: Source) -> str | None:
        return self.camera_ready(src.device, src.label)

    # -- which camera feeds the program node ---------------------------------------------------------------------

    def choose(self, cameras: list[source.Camera], picked: str | None) -> source.Camera | None:
        """The camera the engine should read now: the picked one, or the stand-in while the picked one is gone."""
        primary = next((c for c in cameras if c.label == picked), None) or (cameras[0] if cameras else None)
        if primary is None:
            return None
        others = [c for c in cameras if c is not primary]
        if fallback_mode(self.cfg) == "off" or not others:
            with self._lock:
                self._lost_since = self._steady_since = self._fallback = None
            return primary
        now = self._clock()
        reason = self.camera_ready(primary.device, primary.label)
        notice: tuple[str, str] | None = None
        with self._lock:
            if reason is None:
                self._lost_since = None
                if self._fallback is None:
                    return primary
                self._steady_since = self._steady_since if self._steady_since is not None else now
                if now - self._steady_since >= _seconds(self.cfg, "STREAMSTACK_CAMERA_FALLBACK_BACK", FAILBACK_AFTER):
                    notice = ("ok", f"{primary.label} is back: the program camera moved back to it")
                    self._fallback = self._steady_since = None
                    chosen = primary
                else:
                    chosen = next((c for c in others if c.label == self._fallback), primary)
            else:
                self._steady_since = None
                self._lost_since = self._lost_since if self._lost_since is not None else now
                chosen = primary
                if self._fallback is None and now - self._lost_since >= _seconds(
                    self.cfg, "STREAMSTACK_CAMERA_FALLBACK_AFTER", FALLBACK_AFTER
                ):
                    alt = next((c for c in others if self.camera_ready(c.device, c.label) is None), None)
                    if alt is not None:
                        self._fallback = alt.label
                        notice = ("warn", f"{primary.label} lost its picture: the program camera is on {alt.label}")
                if self._fallback is not None:
                    alt = next((c for c in others if c.label == self._fallback), None)
                    if alt is not None and self.camera_ready(alt.device, alt.label) is None:
                        chosen = alt
                    else:
                        self._fallback = None  # the stand-in went too: back to the slate on the picked camera
        if notice:
            self._say(*notice)
        return chosen

    # -- the engine's hooks ----------------------------------------------------------------------------------------

    def guard(self, spec: Spec) -> str | None:
        """Engine `before_child`: None lets the child start (the node is released first); a reason keeps it waiting
        while the slate holds the node."""
        self.note_output(spec)
        reason = self.source_ready(spec.source)
        if reason is None:
            self.release()
            return None
        self.cover(reason)
        return reason

    def on_exit(self, reason: str, failures: int) -> None:
        """Engine `on_exit`: a child ended. Cover the node at once when it keeps failing, so the gap is the restart."""
        if failures >= 1:
            self.cover(reason)

    def note_output(self, spec: Spec) -> None:
        with self._lock:
            self._out = (spec.out_width, spec.out_height, spec.rate if spec.rate > 0 else DEFAULT_FPS)

    # -- the slate -------------------------------------------------------------------------------------------------

    def _slate_spec(self, reason: str) -> SlateSpec:
        with self._lock:
            width, height, fps = self._out or (*self._configured_size(), DEFAULT_FPS)
        return SlateSpec(self._program_device(), width, height, fps, reason=reason, hold=loss_mode(self.cfg) == "hold")

    def _configured_size(self) -> tuple[int, int]:
        wanted = (self.cfg.get("STREAMSTACK_ENGINE_SIZE") or "source").strip().lower()
        w, _, h = wanted.partition("x")
        if w.isdigit() and h.isdigit() and int(w) % 2 == 0 and int(h) % 2 == 0 and int(w) >= 160 and int(h) >= 90:
            return int(w), int(h)
        return DEFAULT_SIZE

    def cover(self, reason: str) -> None:
        """Write the slate on the program node because the source is gone (no-op with STREAMSTACK_CAMERA_LOSS=off)."""
        if loss_mode(self.cfg) == "off":
            return
        first = not self.slate.active or self._parked
        with self._lock:
            self._reason, self._parked = reason, False
        spec = self._slate_spec(reason)
        self.slate.show(spec, self._last_frame() if spec.hold else None)
        if first:
            self._say("warn", f"{reason}: StreamStack Camera shows 'camera reconnecting' until it returns")

    def park(self) -> bool:
        """The engine is resting (#128): hold the node with a quiet placeholder at IDLE_FPS so the camera stays listed.
        Unlike `cover` this is not a fault: no toast, no warning row. False with STREAMSTACK_CAMERA_LOSS=off, where
        the person chose that nothing but the engine ever writes the node (the camera then leaves the list while idle,
        so the Console does not idle in that mode)."""
        if loss_mode(self.cfg) == "off":
            return False
        with self._lock:
            self._reason, self._parked = "", True
            width, height, _fps = self._out or (*self._configured_size(), DEFAULT_FPS)
        spec = SlateSpec(
            self._program_device(),
            width,
            height,
            IDLE_FPS,
            reason=IDLE_REASON,
            caption=IDLE_CAPTION,
            hold=loss_mode(self.cfg) == "hold",
        )
        self.slate.show(spec, self._last_frame() if spec.hold else None)
        return True

    @property
    def parked(self) -> bool:
        with self._lock:
            return self._parked and self.slate.active

    def release(self) -> None:
        """The source is deliverable again: free the node for the engine child."""
        was, parked = self.slate.active, self._parked
        self.slate.clear()
        with self._lock:
            self._reason, self._parked = "", False
        if was and not parked:
            self._say("ok", "Camera is back: the program camera is live again")

    def tick(self) -> None:
        """Called by the monitor: restarts a dead slate child."""
        self.slate.poll()

    def stop(self) -> None:
        self.slate.clear()
        with self._lock:
            self._parked = False

    # -- state -------------------------------------------------------------------------------------------------------

    def _say(self, level: str, text: str) -> None:
        log.info("delivery: %s", text)
        if self._notify:
            try:
                self._notify(level, text)
            except Exception:
                log.exception("console toast hook failed")

    def state(self, engine_state: str | None, feeding: str | None, picked: str | None) -> dict:
        """`mode`: live | fallback | reconnecting | starting | off, with a sentence for the person."""
        slate = self.slate.state()
        with self._lock:
            reason, fallback = self._reason, self._fallback
        if slate["active"] and self._parked:
            mode = "idle"
        elif slate["active"]:
            mode = "reconnecting"
        elif engine_state == "running":
            mode = "fallback" if fallback else "live"
        elif engine_state in (None, "stopped"):
            mode = "off"
        else:
            mode = "starting"
        return {
            "mode": mode,
            "reason": reason,
            "feeding": feeding,
            "picked": picked,
            "fallback": fallback,
            "slate": slate,
            "loss": loss_mode(self.cfg),
            "fallback_setting": fallback_mode(self.cfg),
        }

    def checks(self, block: dict, external: bool) -> list[dict[str, str]]:
        """The Console's `video-delivery` health row for `state()`'s block."""
        base = {"id": "video-delivery", "group": "video"}
        mode = block["mode"]
        if mode == "idle":
            return [
                {
                    **base,
                    "state": "ok",
                    "title": "StreamStack Camera is idle: a placeholder keeps it listed",
                    "detail": "the engine and the camera start when an app opens it",
                    "hint": "",
                }
            ]
        if mode == "live":
            return [
                {
                    **base,
                    "state": "ok",
                    "title": f"StreamStack Camera is fed by {block['feeding']}",
                    "detail": "",
                    "hint": "",
                }
            ]
        fix = (
            "restart the tool that feeds the camera node (on a host with an external PTP bridge: "
            "systemctl --user restart canonctl-ptp-webcam.service), or plug the camera in again"
            if external
            else "systemctl --user restart streamstack-canon.service, or plug the camera in again"
        )
        if mode == "fallback":
            return [
                {
                    **base,
                    "state": "warn",
                    "title": f"StreamStack Camera is on {block['feeding']}, not {block['picked']}",
                    "detail": f"{block['picked']} lost its picture; the engine returns to it on its own",
                    "hint": fix,
                }
            ]
        if mode == "reconnecting":
            return [
                {
                    **base,
                    "state": "fail",
                    "title": "StreamStack Camera is showing 'camera reconnecting'",
                    "detail": block["reason"] or "the camera has no picture",
                    "hint": fix,
                }
            ]
        return []
