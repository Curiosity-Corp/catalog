"""Auto-framing in the Console: the `framing.*` actions, the `framing` state, and the two threads behind them.

    detect thread   screenshot of each on-air camera at a low rate -> face detector -> Framer.observe
    apply thread    Framer.step at 20 Hz -> crop of the camera picture in the engine (only when it changed)

The picture path is never touched: no device is opened, no filter is added, nothing waits on a detection. If the
detector or the engine stops answering the crop eases back to the full frame and the program feed goes on unchanged.

For other code (the AI director) the actuator surface is `set_mode`, `hold`, `reset` and `status`; the Console
actions call exactly those.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Protocol

from streamstack import config, store
from streamstack.console.errors import ActionError
from streamstack.framing import detector, health, model
from streamstack.framing.crop import Crop, Item, face_to_frame, window_to_crop
from streamstack.framing.engine import MODES, PROFILES, Framer, resolution_zoom_cap

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

APPLY_TICK = 0.05  # 20 Hz writes while the window moves
IDLE_WAIT = 0.5
REDISCOVER = 3.0  # how often the croppable pictures are re-read (a camera switch re-reads at once)
MAX_ERRORS = 5  # consecutive detector failures before framing gives up
CLOSE_AFTER_OFF = 60.0  # the detector (and its GPU context) is released this long after framing goes off
SLOW_MS = 40.0  # a median detection slower than this earns a warning
CHANGE_EPS = 1  # pixels; smaller crop changes are not worth a request


class Cropper(Protocol):
    """What the apply loop needs from whatever holds the picture: the native engine."""

    skipped: dict[str, str]

    def discover(self, scenes: list[str], cameras: set[str]) -> list[Item]: ...
    def apply(self, item: Item, crop: Crop) -> None: ...
    def restore(self, item: Item) -> None: ...


MODE_LABELS = {"off": "Off", **{name: p.label for name, p in PROFILES.items()}}


def mode_list() -> list[dict[str, str]]:
    return [
        {"name": "off", "label": "Off", "blurb": "No framing. The picture is the camera's own."},
        *({"name": p.name, "label": p.label, "blurb": p.blurb} for p in PROFILES.values()),
    ]


def clean_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    if mode not in MODES:
        raise ActionError(f"Unknown framing mode {value!r}. Use one of: {', '.join(MODES)}.", 400)
    return mode


def clean_fps(cfg: dict[str, str]) -> float:
    try:
        return min(15.0, max(2.0, float(cfg.get("STREAMSTACK_FRAMING_FPS", "8"))))
    except ValueError:
        return 8.0


class FramingController:
    """The real thing: the native engine behind the video controller."""

    def __init__(
        self,
        cfg: dict[str, str],
        video: object,
        notify: Notify | None = None,
        *,
        opener: Callable[..., detector.Detector] | None = None,
        state_file: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.video = video
        self._notify = notify
        self._opener = opener or detector.open_detector
        self._clock = clock
        self._state_file = state_file or config.state_dir() / "framing.json"
        self._device = cfg.get("STREAMSTACK_FRAMING_DEVICE", "auto") or "auto"
        self._period = 1.0 / clean_fps(cfg)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._mode = "off"
        self._held = False
        self._framers: dict[str, Framer] = {}
        self._items: list[Item] = []
        self._on_air: set[str] = set()
        self._seen_air: set[str] = set()
        self._discovered = 0.0
        self._scene_key: tuple | None = None
        self._program: str | None = None
        self._ready: tuple[float, tuple[bool, str, str]] | None = None
        self._cropper: Cropper | None = None
        self._detector: detector.Detector | None = None
        self._detector_off_since: float | None = None
        self._status = "off"  # off | starting | running | unavailable
        self._reason: str | None = None
        self._errors = 0
        self._cursor = 0
        self._frames = 0
        self._frames_since = self._clock()
        self._rate = 0.0
        self._skipped: list[str] = []
        self._warned: dict[str, float] = {}
        self._mode = self._initial_mode()

    # -- notifications ------------------------------------------------------------------------------------------

    @property
    def notify(self) -> Notify | None:
        return self._notify

    @notify.setter
    def notify(self, hook: Notify | None) -> None:
        self._notify = hook

    def _toast(self, level: str, text: str) -> None:
        if self._notify:
            try:
                self._notify(level, text)
            except Exception:
                log.exception("framing toast hook failed")

    def _warn_once(self, key: str, text: str, every: float = 30.0) -> None:
        now = self._clock()
        if now - self._warned.get(key, -every) >= every:
            self._warned[key] = now
            self._toast("warn", text)

    # -- persistence --------------------------------------------------------------------------------------------

    def _initial_mode(self) -> str:
        for source in (self._read_saved_mode(), self.cfg.get("STREAMSTACK_FRAMING_MODE", "off")):
            if source in MODES:
                return str(source)
        return "off"

    def _read_saved_mode(self) -> str | None:
        data = store.read_json(self._state_file, {}, validate=store.object_with(mode=str))
        mode = data.get("mode")
        return mode if isinstance(mode, str) else None

    def _save_mode(self) -> None:
        try:
            store.write_json(self._state_file, {"mode": self._mode})
        except OSError as exc:
            log.warning("framing mode not saved to %s: %s", self._state_file, exc)

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._detect_loop, daemon=True, name="streamstack-framing-detect"),
            threading.Thread(target=self._apply_loop, daemon=True, name="streamstack-framing-apply"),
        ]
        for thread in self._threads:
            thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=3)
        self._release_crops()
        self._close_detector()

    # -- the actuator surface (the Console actions and the AI director call these) -------------------------------

    def set_mode(self, mode: str) -> str:
        """Switch mode. Raises ActionError when framing cannot run here (nothing changes then)."""
        mode = clean_mode(mode)
        if mode != "off":
            ready, why, hint = health.readiness()
            if not ready:
                raise ActionError(f"Auto-framing is not available: {why}. Fix: {hint}", 409)
        with self.lock:
            self._mode = mode
            self._errors = 0
            if mode != "off":
                self._reason = None
                if self._status in ("off", "unavailable"):
                    self._status = "starting"
            else:
                self._status = "off"
                self._reason = None
            for framer in self._framers.values():
                framer.set_mode(mode)
        self._save_mode()
        self._wake.set()
        return f"Auto-framing: {MODE_LABELS[mode]}"

    def hold(self, on: bool) -> str:
        with self.lock:
            self._held = bool(on)
            for framer in self._framers.values():
                framer.set_hold(self._held)
        return "Framing held" if on else "Framing released"

    def reset(self) -> str:
        with self.lock:
            for framer in self._framers.values():
                framer.reset()
        return "Framing recentred"

    def status(self) -> dict:
        return self.state()

    def act(self, action: str, params: dict) -> str | None:
        if action == "framing.mode":
            return self.set_mode(params.get("mode"))
        if action == "framing.hold":
            on = params.get("on")
            if not isinstance(on, bool):
                raise ActionError("Say whether to hold: on must be true or false.", 400)
            if self._mode == "off":
                raise ActionError("Auto-framing is off, so there is nothing to hold.", 409)
            return self.hold(on)
        if action == "framing.reset":
            if self._mode == "off":
                raise ActionError("Auto-framing is off.", 409)
            return self.reset()
        raise ActionError(f"unknown action: {action}", 400)

    # -- state --------------------------------------------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            mode, held, status, reason = self._mode, self._held, self._status, self._reason
            framers = dict(self._framers)
            air = set(self._on_air)
            skipped = list(self._skipped)
            rate = self._rate
        sources = []
        for name, framer in framers.items():
            window = framer.window
            sources.append(
                {
                    "name": name,
                    "on_air": name in air,
                    "state": framer.state,
                    "zoom": round(window.zoom, 3),
                    "cx": round(window.cx, 4),
                    "cy": round(window.cy, 4),
                    "max_zoom": round(framer.max_zoom, 2),
                }
            )
        lead = next((s for s in sources if s["on_air"]), sources[0] if sources else None)
        det = self._detector
        timing = det.timing.summary() if det is not None and hasattr(det, "timing") else {}
        if status == "unavailable":
            state = "unavailable"
        elif mode == "off":
            state = "off"
        elif status == "starting":
            state = "starting"
        elif held:
            state = "held"
        else:
            state = lead["state"] if lead else "no-face"
        ready = self._readiness()
        return {
            "mode": mode,
            "state": state,
            "held": held,
            "available": ready[0],
            "unavailable_hint": None if ready[0] else f"{ready[1]}. Fix: {ready[2]}",
            "reason": reason if status == "unavailable" else None,
            "backend": det.backend if det is not None else None,
            "device": det.device if det is not None else None,
            "detect_ms": timing.get("median_ms"),
            "detect_p95_ms": timing.get("p95_ms"),
            "fps": round(rate, 1),
            "model": model.YUNET.name,
            "zoom": lead["zoom"] if lead else 1.0,
            "window": {"cx": lead["cx"], "cy": lead["cy"], "zoom": lead["zoom"]} if lead else None,
            "sources": sources,
            "skipped": skipped,
            "modes": mode_list(),
        }

    def _readiness(self) -> tuple[bool, str, str]:
        """health.readiness(), looked up at most every 10 s (the state is built several times a second)."""
        now = self._clock()
        if self._ready is None or now - self._ready[0] > 10.0:
            self._ready = (now, health.readiness())
        return self._ready[1]

    def health_checks(self) -> list[dict[str, str]]:
        """The live "Auto-framing" check while it runs or has failed; nothing otherwise (the static check stands)."""
        with self.lock:
            mode, status, reason = self._mode, self._status, self._reason
        det = self._detector
        if status == "unavailable":
            return [health.check("warn", "Auto-framing is unavailable", reason or "", "streamstack framing")]
        if mode == "off" or det is None:
            return []
        timing = det.timing.summary() if hasattr(det, "timing") else {}
        median, p95 = timing.get("median_ms"), timing.get("p95_ms")
        title = f"Auto-framing ({MODE_LABELS[mode]}) on {det.device}"
        if median is None:
            return [health.check("ok", title + ", warming up", f"{det.backend}, {model.YUNET.name}")]
        title += f": {median:.1f} ms per detection"
        detail = f"{det.backend}, {model.YUNET.name}, p95 {p95:.1f} ms, {self._rate:.1f} detections per second"
        if median > SLOW_MS:
            return [
                health.check(
                    "warn",
                    title,
                    detail + "; slow",
                    "STREAMSTACK_FRAMING_DEVICE=cpu or a lower STREAMSTACK_FRAMING_FPS",
                )
            ]
        return [health.check("ok", title, detail)]

    # -- the picture ----------------------------------------------------------------------------------------------

    def _view(self) -> tuple[bool, str | None, list[str], set[str]]:
        view = getattr(self.video, "scene_view", None)
        if not callable(view):
            return False, None, [], set()
        return view()

    def _shot(self, source: str) -> bytes | None:
        """The engine's own low-rate, uncropped JPEG of `source`."""
        native = getattr(self.video, "detect_frame", None)
        return native(source) if callable(native) else None

    def _cropper_for(self) -> Cropper:
        if self._cropper is None:
            make = getattr(self.video, "make_cropper", None)
            if not callable(make):
                raise ActionError("The video engine cannot crop the picture.", 409)
            self._cropper = make()
        return self._cropper

    # -- croppable pictures ------------------------------------------------------------------------------------------

    def _refresh_items(self, now: float, force: bool = False) -> bool:
        """Re-read the croppable camera pictures; False while the engine is not there."""
        connected, program, scenes, cameras = self._view()
        if not connected:
            with self.lock:
                self._items, self._on_air, self._scene_key = [], set(), None
            return False
        key = (program, tuple(scenes), tuple(sorted(cameras)))
        if not force and key == self._scene_key and now - self._discovered < REDISCOVER:
            return True
        cropper = self._cropper_for()
        items = cropper.discover(scenes, cameras)
        with self.lock:
            self._items = items
            self._on_air = {i.source for i in items if i.scene == program}
            self._scene_key, self._discovered, self._program = key, now, program
            self._skipped = sorted(set(cropper.skipped.values()))
            by_source: dict[str, list[Item]] = {}
            for item in items:
                by_source.setdefault(item.source, []).append(item)
            for source, group in by_source.items():
                framer = self._framers.get(source)
                if framer is None:
                    framer = self._framers[source] = Framer(self._mode)
                    framer.set_hold(self._held)
                cap = min(resolution_zoom_cap(i.frame_height, i.out_height) for i in group)
                framer.set_zoom_cap(cap)
            for source in self._on_air - self._seen_air:
                if source in self._framers:
                    self._framers[source].resume(now)  # back on air: the gap off air is not a lost face
            self._seen_air = set(self._on_air)
        return True

    # -- the apply loop -------------------------------------------------------------------------------------------

    def _apply_loop(self) -> None:
        while not self._stop.is_set():
            idle = self._apply_tick(self._clock())
            self._stop.wait(IDLE_WAIT if idle else APPLY_TICK)

    def _apply_tick(self, now: float) -> bool:
        """One step of the window and the crop writes it needs. True when there is nothing to do."""
        with self.lock:
            active = self._mode != "off" or any(not f.settled_full for f in self._framers.values())
        if not active and not any(i.applied != i.base for i in self._items):
            return True
        try:
            if not self._refresh_items(now):
                return True
            cropper = self._cropper_for()
            with self.lock:
                windows = {}
                for source, framer in self._framers.items():
                    # a camera that is off air keeps its crop as it was; the window moves only while it is shown
                    on_air = source in self._on_air or framer.mode == "off"
                    windows[source] = framer.step(now) if on_air else framer.window
                plan = [
                    (i, self._framers[i.source], windows[i.source]) for i in self._items if i.source in self._framers
                ]
            for item, framer, window in plan:
                if framer.mode == "off" and framer.settled_full:
                    cropper.restore(item)
                    continue
                crop = window_to_crop(window, item)
                if _far(crop, item.applied):
                    cropper.apply(item, crop)
        except ActionError:
            self._items, self._scene_key = [], None  # the engine went away: rediscover when it is back
            return True
        except RuntimeError as exc:  # the engine refused (it is restarting)
            log.info("framing: the engine refused a crop (%s); re-reading the picture", exc)
            self._scene_key = None
        return False

    def _release_crops(self) -> None:
        """Put every camera back to its own crop (stop, or giving up). Best effort: the engine may be gone."""
        cropper = self._cropper
        if cropper is None:
            return
        for item in list(self._items):
            try:
                cropper.restore(item)
            except (ActionError, RuntimeError) as exc:
                log.warning("framing: could not restore %s/%s: %s", item.scene, item.source, exc)

    # -- the detect loop ------------------------------------------------------------------------------------------

    def _detect_loop(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            ran = self._detect_tick(started)
            if not ran:
                self._wake.wait(IDLE_WAIT)
                self._wake.clear()
                continue
            self._stop.wait(max(0.0, self._period - (self._clock() - started)))

    def _close_detector(self) -> None:
        det, self._detector = self._detector, None
        if det is not None:
            try:
                det.close()
            except Exception as exc:
                log.warning("framing: detector close failed: %s", exc)

    def _fail(self, reason: str) -> None:
        """Give up: framing goes back to off with the reason shown and a toast (the program feed is untouched)."""
        with self.lock:
            self._mode, self._status, self._reason = "off", "unavailable", reason
            for framer in self._framers.values():
                framer.set_mode("off")
        self._save_mode()
        self._close_detector()
        self._toast("warn", f"Auto-framing is off: {reason}")

    def _detect_tick(self, now: float) -> bool:
        """One detection for the next on-air camera. True when it did work (so the loop paces itself)."""
        if self._mode == "off":
            if self._detector is not None:
                if self._detector_off_since is None:
                    self._detector_off_since = now
                elif now - self._detector_off_since > CLOSE_AFTER_OFF:
                    self._close_detector()
            return False
        self._detector_off_since = None
        if self._held:
            return False
        if self._detector is None:
            try:
                self._detector = self._opener(self._device)
            except detector.DetectorUnavailable as exc:
                self._fail(str(exc))
                return False
            except Exception as exc:
                log.exception("framing detector failed to open")
                self._fail(f"{type(exc).__name__}: {exc}")
                return False
        det = self._detector
        with self.lock:
            if self._status == "starting":
                self._status = "running"
            air = sorted(self._on_air)
        if not air:
            return False
        self._cursor = (self._cursor + 1) % len(air)
        source = air[self._cursor]
        item = next((i for i in self._items if i.source == source and i.scene == self._program), None)
        if item is None:
            return False
        try:
            jpeg = self._shot(source)
        except (ActionError, RuntimeError) as exc:  # the engine is not there; the apply loop notices too
            log.debug("framing: no picture of %s: %s", source, exc)
            return False
        if jpeg is None:
            return False
        try:
            faces = det.detect(jpeg)
        except Exception as exc:  # any backend error counts; MAX_ERRORS in a row turns framing off
            return self._detector_error(exc)
        self._errors = 0
        framed = [face_to_frame(f, item) for f in faces]
        with self.lock:
            framer = self._framers.get(source)
            if framer is not None:
                framer.observe(now, framed)
            self._frames += 1
            if now - self._frames_since >= 2.0:
                self._rate = self._frames / (now - self._frames_since)
                self._frames, self._frames_since = 0, now
        return True

    def _detector_error(self, exc: Exception) -> bool:
        self._errors += 1
        log.warning("framing: detection failed (%d/%d): %s", self._errors, MAX_ERRORS, exc)
        if self._errors >= MAX_ERRORS:
            self._fail(f"the face detector keeps failing ({type(exc).__name__}: {exc})")
        return False


def _far(a: Crop, b: Crop) -> bool:
    """True when two crops differ by at least CHANGE_EPS pixels on any side (or the target is the exact base)."""
    return any(abs(getattr(a, side) - getattr(b, side)) >= CHANGE_EPS for side in ("left", "top", "right", "bottom"))


class DemoFraming:
    """The same API on the demo rig: no camera, no model, a believable simulation for the e2e suite and --demo."""

    DEVICE = "Intel Arc A380 (GPU)"
    ZOOM: ClassVar[dict[str, float]] = {"off": 1.0, "gentle": 1.15, "tight": 1.45, "follow": 1.3}

    def __init__(self, notify: Notify | None = None, mode: str = "off") -> None:
        self._notify = notify
        self.lock = threading.RLock()
        self.mode = mode if mode in MODES else "off"
        self.held = False
        self.window = {"cx": 0.5, "cy": 0.5, "zoom": self.ZOOM[self.mode]}

    @property
    def notify(self) -> Notify | None:
        return self._notify

    @notify.setter
    def notify(self, hook: Notify | None) -> None:
        self._notify = hook

    def start(self) -> None:
        """Nothing runs in the background."""

    def stop(self) -> None:
        """Nothing to stop."""

    def set_mode(self, mode: str) -> str:
        mode = clean_mode(mode)
        with self.lock:
            self.mode = mode
            self.window = {"cx": 0.5, "cy": 0.46 if mode != "off" else 0.5, "zoom": self.ZOOM[mode]}
            if mode == "off":
                self.held = False
        return f"Auto-framing: {MODE_LABELS[mode]}"

    def hold(self, on: bool) -> str:
        with self.lock:
            self.held = bool(on)
        return "Framing held" if on else "Framing released"

    def reset(self) -> str:
        with self.lock:
            self.window = {"cx": 0.5, "cy": 0.5, "zoom": self.ZOOM[self.mode]}
        return "Framing recentred"

    def status(self) -> dict:
        return self.state()

    def act(self, action: str, params: dict) -> str | None:
        if action == "framing.mode":
            return self.set_mode(params.get("mode"))
        if action in ("framing.hold", "framing.reset"):
            if self.mode == "off":
                raise ActionError("Auto-framing is off.", 409)
            if action == "framing.reset":
                return self.reset()
            on = params.get("on")
            if not isinstance(on, bool):
                raise ActionError("Say whether to hold: on must be true or false.", 400)
            return self.hold(on)
        raise ActionError(f"unknown action: {action}", 400)

    def state(self) -> dict:
        with self.lock:
            on = self.mode != "off"
            window = dict(self.window)
            return {
                "mode": self.mode,
                "state": "off" if not on else "held" if self.held else "tracking",
                "held": self.held,
                "available": True,
                "unavailable_hint": None,
                "reason": None,
                "backend": "openvino" if on else None,
                "device": self.DEVICE if on else None,
                "detect_ms": 3.4 if on else None,
                "detect_p95_ms": 4.1 if on else None,
                "fps": 8.0 if on else 0.0,
                "model": model.YUNET.name,
                "zoom": window["zoom"],
                "window": window if on else None,
                "sources": [
                    {
                        "name": "Canon EOS 50D",
                        "on_air": True,
                        "state": "off" if not on else "held" if self.held else "tracking",
                        "zoom": window["zoom"],
                        "cx": window["cx"],
                        "cy": window["cy"],
                        "max_zoom": 1.3,
                    }
                ],
                "skipped": [],
                "modes": mode_list(),
            }

    def health_checks(self) -> list[dict[str, str]]:
        if self.mode == "off":
            return [health.check("ok", "Auto-framing is ready", f"{model.YUNET.name}, OpenVINO (Intel GPU or CPU)")]
        return [
            health.check(
                "ok",
                f"Auto-framing ({MODE_LABELS[self.mode]}) on {self.DEVICE}: 3.4 ms per detection",
                f"openvino, {model.YUNET.name}, p95 4.1 ms, 8.0 detections per second",
            )
        ]
