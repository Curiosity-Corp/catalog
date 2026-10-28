"""The Console's video controller: the native engine behind the `video` state and the `video.*` actions.

    cameras  ->  engine (one supervised ffmpeg child: VA-API decode, libplacebo colour, crop)  ->  /dev/video43

The Console owns the engine (`streamstack.engine.process.VideoEngine`) and talks to it in-process: a look change is
a few bytes on the child's stdin, applied between two frames, with no restart and no dropped call camera. Only a
change of camera restarts the child (the picture is gone for about a second).

What the page shows: the one scene is "Program", the camera sources are the cameras found, and `virtualcam`
means the program camera is running. The `engine` block carries the backend, decode path, frame rate, drops and
look-apply time. The auto director, running in its own process, drives the same look through the Console's
`video.look` action and reads the graded picture from the preview endpoint (see streamstack.consolelink).

Not part of this controller yet (it answers a clear 409): recording and streaming (the hardware encode tap,
see streamstack.engine.encode).

Background blur runs on the Arc through OpenVINO (streamstack.console.arcblur, streamstack.engine.blurfeed) and answers
409 "unavailable: <reason>" where there is no OpenVINO, model or GPU.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from streamstack import config
from streamstack.console import blur as blurctl
from streamstack.console.arcblur import ArcBlur
from streamstack.console.canon import CanonLink
from streamstack.console.delivery import Delivery
from streamstack.console.errors import ActionError
from streamstack.console.video import (
    APPLY_SECONDS,
    BUILTIN_LOOKS,
    FRAME_TTL,
    NEUTRAL,
    RESET_SECONDS,
    CameraStream,
    LookLibrary,
    clamp_width,
    empty_state,
    required_flag,
    required_name,
    validate_look,
)
from streamstack.engine import graph, idle, probe, source
from streamstack.engine.cropper import SCENE, EngineCropper
from streamstack.engine.graph import Source, Spec
from streamstack.engine.process import VideoEngine
from streamstack.hardware import camera as hwcamera
from streamstack.hardware import roles

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

PREVIEW_DEMAND = 6.0  # a preview frame asked for this recently counts as someone looking at the picture
MONITOR_SECONDS = 2.0
RAMP_HZ = 20.0
SAVE_EVERY = 1.0
PRIVACY_SCENE = "Blackout"
OUTPUT_LATER = "Recording and streaming from the native engine arrive with the hardware encode tap (AV1 first)."


class NativeVideo:
    """VideoController's public surface (state, act, frame, health_checks, start, stop) on the native engine."""

    def __init__(
        self,
        cfg: dict[str, str],
        notify: Notify | None = None,
        *,
        library: LookLibrary | None = None,
        state_file: Path | None = None,
        discover: Callable[[dict[str, str]], list[source.Camera]] | None = None,
        resolve: Callable[[source.Camera, dict[str, str]], Source] = source.resolve,
        engine_factory: Callable[..., VideoEngine] = VideoEngine,
        backend: Callable[[str], tuple[str, str]] = probe.probe_backend,
        canon: CanonLink | None = None,
        arc: ArcBlur | None = None,
        plan: roles.PlanSource | None = None,
        delivery: Delivery | None = None,
        watch_factory: Callable[..., idle.ReaderWatch] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._plan = plan or (lambda: None)  # the hardware Plan: which GPU does the grade (ADR 0003)
        self._notify = notify
        self.library = library or LookLibrary()
        self._state_file = state_file or config.state_dir() / "video.json"
        self._discover = discover or (lambda settings: source.cameras(settings, plan=self._plan()))
        self._resolve, self._factory, self._probe_backend = resolve, engine_factory, backend
        self.lock = threading.RLock()
        self.canon = canon or CanonLink(cfg, self._toast)
        self.arc = arc or ArcBlur(cfg, plan=self._plan)
        # keeps StreamStack Camera fed while the real camera is away (console/delivery.py)
        self.delivery = delivery or Delivery(
            cfg,
            notify=self._toast,
            camera_device=lambda: hwcamera.device_for("camera", self.cfg, self._plan()),
            program_device=lambda: hwcamera.device_for("program", self.cfg, self._plan()),
            last_frame=self._last_detect_frame,
        )
        self._feeding: str | None = None  # the camera the engine reads now (the picked one, or its stand-in)
        # idle the engine (and the Canon live view) while no app reads the program node (#128, engine/idle.py)
        self._clock = clock
        self._idle = idle.IdleGovernor(clock=clock, enabled=idle.idle_mode(cfg) == "auto", after=idle.idle_after(cfg))
        self._idle_lock = threading.RLock()
        self._watch_factory = watch_factory or idle.ReaderWatch
        self._watch: idle.ReaderWatch | idle.NullWatch = idle.NullWatch()
        self._demand_until: dict[str, float] = {}  # name -> until when it asked for the picture
        self._demand_probes: dict[str, Callable[[], bool]] = {}  # standing needs: an output that is running
        self._blur_built = False  # the running engine's graph has the blur branch
        self._cameras: list[source.Camera] = []
        self._active: str | None = None
        self._looks: dict[str, dict] = {}
        self._privacy = False
        self._wanted = True  # the program camera should be running (the virtual camera switch)
        self._engine: VideoEngine | None = None
        self._source: Source | None = None
        self._reason: str | None = None  # why there is no engine yet (camera absent, no signal)
        self._backend: tuple[str, str] | None = None
        self._backend_node = ""  # the node `_backend` was probed on
        self._engine_node = ""  # the node the running engine's graph was built for
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._ramp_thread: threading.Thread | None = None
        self._ramp_cancel = threading.Event()
        self._saved_at = 0.0
        self._streams: dict[str, CameraStream] = {}
        self._frames: dict[tuple[str, int], tuple[float, bytes]] = {}
        self._warned: dict[str, float] = {}
        self._load()

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
                log.exception("console toast hook failed")  # a broken hook must never break video

    def _warn_once(self, key: str, text: str, every: float = 30.0) -> None:
        now = time.monotonic()
        if now - self._warned.get(key, -every) >= every:
            self._warned[key] = now
            self._toast("warn", text)

    # -- persistence --------------------------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("video state %s is unreadable: %s", self._state_file, exc)
            return
        if not isinstance(data, dict):
            return
        looks = data.get("looks")
        if isinstance(looks, dict):
            for label, values in looks.items():
                try:
                    self._looks[str(label)] = {**NEUTRAL, **validate_look(values)}
                except ActionError as exc:
                    log.warning("video state: ignoring the saved look of %s: %s", label, exc.message)
        if isinstance(data.get("active"), str):
            self._active = data["active"]
        self._idle.keep_alive = data.get("keep_alive") is True
        self.arc.restore(data.get("blur"))

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._saved_at < SAVE_EVERY:
            return
        self._saved_at = now
        with self.lock:
            payload = {
                "active": self._active,
                "looks": copy.deepcopy(self._looks),
                "blur": self.arc.settings(),
                "keep_alive": self._idle.keep_alive,
            }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._state_file.parent, prefix=".video-", suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self._state_file)
        except OSError as exc:
            log.warning("video state not saved to %s: %s", self._state_file, exc)

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.canon.start()
        self.arc.start()
        self._watch = self._watch_factory(
            lambda: hwcamera.device_for("program", self.cfg, self._plan()),
            ignore=idle.idle_ignore(self.cfg),
            on_tick=self._idle_tick,
        )
        self._watch.start()
        self._thread = threading.Thread(target=self._monitor, daemon=True, name="streamstack-video")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._ramp_cancel.set()
        self._watch.stop()  # first: nothing may idle or wake the engine while it is being taken down
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        ramp = self._ramp_thread
        if ramp and ramp.is_alive() and ramp is not threading.current_thread():
            ramp.join(timeout=3)
        with self.lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.stop()
        self.delivery.stop()
        self.arc.stop()
        for stream in self._streams.values():
            stream.stop()
        self.canon.stop()
        self._save(force=True)

    # -- the monitor: cameras come and go, the engine follows --------------------------------------------------

    def _monitor(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("video monitor tick failed")  # the loop must survive anything a probe throws
            self._wake.wait(MONITOR_SECONDS)
            self._wake.clear()

    def _tick(self) -> None:
        found = self._discover(self.cfg)
        with self.lock:
            self._cameras = found
            labels = [c.label for c in found]
            if self._active not in labels:
                self._active = labels[0] if labels else None
            picked = self._active
            wanted, engine = self._wanted, self._engine
        if not wanted:
            self.delivery.stop()
            return
        self.delivery.tick()
        self._idle_tick()
        if self._idle.state == idle.IDLE:
            return  # resting: the placeholder holds the node, the camera list above stays current, nothing to build
        active = self.delivery.choose(found, picked)
        if active is None:
            self._no_source("no camera is connected", engine)
            return
        with self.lock:
            self._feeding = active.label
        moved = engine is not None and self._node().path != self._engine_node
        same = self._source is not None and self._source.label == active.label
        if (
            not moved
            and same
            and engine is not None
            and engine.state()["state"] == "running"
            and (self._blur_built or not self.arc.capable)
        ):
            return
        self._ensure_engine(active)

    # -- idle: rest the engine and the Canon live view while no app reads StreamStack Camera (#128) -------------------

    def add_demand(self, name: str, probe: Callable[[], bool]) -> None:
        """Something inside StreamStack that needs the engine's picture while `probe()` is true (a running stream or
        recording reads /dev/video43 through its own ffmpeg, which the reader scan rightly ignores as plumbing)."""
        self._demand_probes[name] = probe

    def touch(self, name: str = "preview", seconds: float = PREVIEW_DEMAND) -> None:
        """Someone is looking at the picture (a Console preview was asked for): keep or wake the engine for a while."""
        self._demand_until[name] = self._clock() + seconds

    def _demands(self) -> list[str]:
        now = self._clock()
        names = [name for name, until in list(self._demand_until.items()) if until > now]
        for name, needs in list(self._demand_probes.items()):
            try:
                if needs():
                    names.append(name)
            except Exception:
                log.exception("video: the %s demand probe failed; counting it as demand", name)
                names.append(name)
        return names

    def _idle_tick(self) -> None:
        """One step of the idle governor: called from the reader watch every quarter second and from the monitor.

        A verdict is only ever acted on with a fresh reader scan: a sensor that cannot tell keeps the camera awake."""
        if self._stop.is_set():
            return
        with self._idle_lock:
            with self.lock:
                engine, wanted = self._engine, self._wanted
            if engine is None or not wanted:
                return
            latest = self._watch.latest
            readers = latest.present or not self._watch.fresh()
            verdict = self._idle.step(
                readers=readers,
                demand=bool(self._demands()),
                engine_running=engine.state()["state"] == "running",
            )
            if verdict.action == "idle":
                self._enter_idle(engine, verdict.reason)
            elif verdict.action == "wake":
                self._leave_idle(engine, verdict.reason)
            elif self._idle.state == idle.IDLE:
                self.canon.rest(True)  # renew the bridge's lease: it wakes on its own if this process dies

    def _enter_idle(self, engine: VideoEngine, reason: str) -> None:
        """Stop the engine child, put the placeholder on the node and let the Canon body leave live view."""
        engine.stop()
        if not self.delivery.park():
            self._idle.force_running()  # CAMERA_LOSS=off: nobody may write the node but the engine; do not idle
            engine.start()
            log.info("video: not idling, STREAMSTACK_CAMERA_LOSS=off keeps the engine on the node")
            return
        self.canon.rest(True)
        log.info("video: engine idle (%s)", reason)

    def _leave_idle(self, engine: VideoEngine, reason: str) -> None:
        """Start the engine and the Canon live view; the engine's own guard takes the placeholder off the node."""
        log.info("video: waking the engine (%s)", reason)
        self.canon.rest(False)
        engine.start()
        self._wake.set()

    def _idle_snapshot(self) -> dict:
        with self._idle_lock:
            block = self._idle.snapshot()
        latest = self._watch.latest
        block["readers"] = latest.public()
        block["sensor"] = "ok" if self._watch.fresh() else "unsure"
        block["demand"] = self._demands()
        return block

    def _keepalive(self, params: dict) -> str | None:
        on = required_flag(params)
        with self._idle_lock:
            self._idle.keep_alive = on
        self._idle_tick()  # on while resting: the governor's own wake, at once
        self._save(force=True)
        return "Camera kept awake" if on else "Camera may idle when no app uses it"

    def _no_source(self, reason: str, engine: VideoEngine | None) -> None:
        """No camera can be read and no engine child is running: keep the program camera listed with the slate.
        (With an engine, its guard does this; it also knows when the child has just died.)"""
        if engine is None:
            self.delivery.cover(reason)

    def _last_detect_frame(self) -> bytes | None:
        engine = self._engine
        return engine.latest_frame(max_age=1e9) if engine is not None else None

    def _node(self) -> roles.NodeChoice:
        """The render node for the graph: the setting, else the Plan's grade role, else the generic choice."""
        return roles.choose("engine", self._plan(), self.cfg.get("STREAMSTACK_ENGINE_RENDER_NODE", ""))

    def _backend_for_engine(self, node: str) -> str:
        wanted = (self.cfg.get("STREAMSTACK_ENGINE_BACKEND") or "auto").strip().lower()
        if wanted in ("vulkan", "cpu"):
            return wanted
        if self._backend is None or self._backend_node != node:
            self._backend_node = node
            self._backend = self._probe_backend(node)
            if self._backend[0] == "cpu":
                self._warn_once(
                    "backend", f"Video engine: no GPU colour path here ({self._backend[1]}); using the CPU."
                )
        return self._backend[0]

    def _spec_for(self, src: Source) -> Spec:
        node = self._node().path
        self._engine_node = node
        width, height = source.output_size(self.cfg, src)
        return Spec(
            src,
            hwcamera.device_for("program", self.cfg, self._plan()),
            width,
            height,
            backend=self._backend_for_engine(node),
            render_node=node,
            detect_fps=_detect_fps(self.cfg),
            lut=(self.cfg.get("STREAMSTACK_ENGINE_LUT") or "").strip(),
            shaders=self._shader_dir(),
            blur=self.arc.graph if self.arc.capable else None,
        )

    def _shader_dir(self) -> str:
        """Where the GPU sharpen shaders go; empty keeps the live CPU cas filter (STREAMSTACK_ENGINE_SHARPEN=cpu)."""
        wanted = (self.cfg.get("STREAMSTACK_ENGINE_SHARPEN") or "auto").strip().lower()
        return "" if wanted == "cpu" else str(config.state_dir() / "shaders")

    def _ensure_engine(self, camera: source.Camera) -> None:
        """Resolve the camera and start the engine on it, or re-point a stuck engine at the camera's new mode."""
        try:
            src = self._resolve(camera, self.cfg)
        except source.SourceError as exc:
            with self.lock:
                self._reason, engine = str(exc), self._engine
            self._no_source(str(exc), engine)
            return
        self.arc.wait_probed()  # the blur branch is part of the graph, so the answer is needed before it is built
        spec = self._spec_for(src)
        with self.lock:
            self._reason, self._source, self._blur_built = None, src, spec.blur is not None
            engine, look, privacy = self._engine, dict(self._active_look()), self._privacy
        if engine is None:
            engine = self._factory(
                spec, look, notify=self._toast, before_child=self.delivery.guard, on_exit=self.delivery.on_exit
            )
            engine.set_blackout(privacy)
            self._wire_blur(engine, spec)
            with self.lock:
                self._engine = engine
            engine.start()
        else:
            engine.apply_look(look)
            self._wire_blur(engine, spec)
            engine.set_spec(spec)  # a no-op unless the graph changed

    def _wire_blur(self, engine: VideoEngine, spec: Spec) -> None:
        """Give the engine the blur feed and the saved settings when its graph has the blur branch."""
        feed = self.arc.feed() if spec.blur is not None else None
        if feed is None:
            return
        engine.attach_blur(feed)
        engine.set_blur(self.arc.on, self.arc.strength, self.arc.rate)

    # -- looks --------------------------------------------------------------------------------------------------

    def _active_look(self) -> dict:
        return dict(self._looks.get(self._active or "", NEUTRAL))

    def _set_look(self, label: str, look: dict) -> None:
        with self.lock:
            self._looks[label] = dict(look)
            engine = self._engine if label == self._active else None
        if engine is not None:
            engine.apply_look(look)

    def _cancel_ramp(self) -> None:
        self._ramp_cancel.set()
        thread = self._ramp_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _start_ramp(self, label: str, target: dict, seconds: float) -> None:
        self._cancel_ramp()
        cancel = threading.Event()
        self._ramp_cancel = cancel
        start = dict(self._looks.get(label, NEUTRAL))
        steps = max(4, int(seconds * RAMP_HZ))

        def work() -> None:
            for step in range(1, steps + 1):
                if cancel.is_set() or self._stop.is_set():
                    return
                self._set_look(label, graph.blend_looks(start, target, step / steps))
                if cancel.wait(seconds / steps):
                    return
            self._set_look(label, {**NEUTRAL, **target})
            self._save()

        self._ramp_thread = threading.Thread(target=work, daemon=True, name="streamstack-ramp")
        self._ramp_thread.start()

    # -- state --------------------------------------------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            cameras = [c.label for c in self._cameras]
            active, reason, privacy, wanted = self._active, self._reason, self._privacy, self._wanted
            look = self._active_look()
            engine, src = self._engine, self._source
        snapshot = empty_state()
        info = engine.state() if engine is not None else None
        snapshot.update(
            scenes=[{"name": SCENE, "active": True}],
            program_scene=SCENE,
            privacy=privacy,
            privacy_scene=PRIVACY_SCENE,
            sources=cameras,
            selected_source=active,
            look=look,
            looks=self.library.listing(),
            virtualcam=bool(info and info["state"] == "running") or self._idle.state != idle.RUNNING,
            canon=self.canon.state(),
        )
        snapshot["blur"] = self.arc.state()
        snapshot["engine"] = self._engine_block(info, src, reason, wanted)
        snapshot["engine"]["delivery"] = self.delivery.state(info["state"] if info else None, self._feeding, active)
        return snapshot

    def _engine_block(self, info: dict | None, src: Source | None, reason: str | None, wanted: bool) -> dict:
        backend = self._backend
        resting = self._idle.state == idle.IDLE and wanted
        block: dict = {
            "native": True,
            "state": "off" if not wanted else "idle" if resting else "waiting" if info is None else info["state"],
            "error": reason if info is None else info["error"],
            "backend": (info or {}).get("backend") or (backend[0] if backend else None),
            "backend_note": backend[1] if backend and backend[0] == "cpu" else None,
            "decode": (info or {}).get("decode"),
            "source": src.label if src else None,
            "source_mode": (info or {}).get("source_mode"),
            "output": (info or {}).get("output"),
            "fps": (info or {}).get("fps"),
            "dropped": (info or {}).get("dropped", 0),
            "restarts": (info or {}).get("restarts", 0),
            "look_apply_ms": (info or {}).get("look_apply_ms"),
            "crop": (info or {}).get("crop"),
            "idle": self._idle_snapshot(),
        }
        return block

    def health_checks(self) -> list[dict[str, str]]:
        return self._engine_checks() + self._delivery_checks() + self.arc.health()

    def _delivery_checks(self) -> list[dict[str, str]]:
        with self.lock:
            wanted = self._wanted
        if not wanted:
            return []
        return self.delivery.checks(self.state()["engine"]["delivery"], config.camera_external(self.cfg))

    def _engine_checks(self) -> list[dict[str, str]]:
        block = self.state()["engine"]
        base = {"id": "video-engine", "group": "video"}
        if block["state"] == "off":
            return [
                {**base, "state": "warn", "title": "The program camera is off", "detail": "", "hint": "turn on Camera"}
            ]
        if block["state"] == "idle":
            return [
                {
                    **base,
                    "state": "ok",
                    "title": "Video engine is idle: no app is reading StreamStack Camera",
                    "detail": "it starts within a second when an app opens the camera; a placeholder keeps it listed",
                    "hint": "",
                }
            ]
        if block["state"] == "running":
            detail = f"{block['output']}, {block['decode']}, {block['fps'] or 0:g} fps, {block['dropped']} dropped"
            return [
                {
                    **base,
                    "state": "ok",
                    "title": f"Video engine on the {block['backend']} path",
                    "detail": detail,
                    "hint": "",
                }
            ]
        why = block["error"] or "starting"
        return [{**base, "state": "warn", "title": "The video engine is not running", "detail": why, "hint": ""}]

    # -- the framing controller's view of the picture ---------------------------------------------------------

    def scene_view(self) -> tuple[bool, str | None, list[str], set[str]]:
        with self.lock:
            return True, SCENE, [SCENE], {self._source.label} if self._source else set()

    def make_cropper(self) -> EngineCropper:
        return EngineCropper(lambda: self._engine, lambda: self._source)

    def detect_frame(self, label: str) -> bytes | None:
        """The engine's own low-rate, uncropped JPEG of the camera it is reading; None for any other camera."""
        engine, src = self._engine, self._source
        if engine is None or src is None or src.label != label:
            return None
        return engine.latest_frame()

    # -- actions ------------------------------------------------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        if action.startswith("video.canon_"):
            return self.canon.act(action, params)
        handler = {
            "video.scene": self._scene,
            "video.privacy": self._privacy_action,
            "video.source": self._source_action,
            "video.look": self._look_action,
            "video.look.reset": self._look_reset,
            "video.looks.apply": self._looks_apply,
            "video.looks.save": self._looks_save,
            "video.looks.delete": self._looks_delete,
            "video.blur": self._blur,
            "video.virtualcam": self._virtualcam,
            "video.keepalive": self._keepalive,
            "video.record": self._output,
            "video.stream": self._output,
        }.get(action)
        if handler is None:
            raise ActionError(f"unknown action: {action}", 400)
        return handler(params)

    def _scene(self, params: dict) -> str | None:
        name = required_name(params)
        if name != SCENE:
            raise ActionError(f"No scene called {name}. The native engine has one: {SCENE}.", 400)
        return f"On air: {SCENE}"

    def _privacy_action(self, params: dict) -> str | None:
        on = required_flag(params)
        with self.lock:
            changed = on != self._privacy
            self._privacy, engine = on, self._engine
        if engine is not None:
            engine.set_blackout(on)
        if not changed:
            return "Privacy is already on." if on else "Privacy is already off."
        self._toast("warn" if on else "ok", "Privacy on - camera is off air" if on else "Privacy off - back on air")
        return "Privacy on" if on else "Privacy off"

    def _source_action(self, params: dict) -> str | None:
        name = required_name(params)
        with self.lock:
            camera = next((c for c in self._cameras if c.label == name), None)
        if camera is None:
            raise ActionError(f"{name} is not a camera the engine can read.", 400)
        with self.lock:
            self._active = name
        self._save(force=True)
        self._cancel_ramp()
        try:
            src = self._resolve(camera, self.cfg)
        except source.SourceError as exc:
            raise ActionError(str(exc), 409) from exc
        with self.lock:
            self._source, self._reason, engine = src, None, self._engine
        if engine is not None:
            engine.apply_look(self._active_look())
            engine.set_spec(self._spec_for(src))
        else:
            self._wake.set()
        return f"Editing {name}"

    def _target(self, params: dict) -> str:
        with self.lock:
            sources = [c.label for c in self._cameras]
            chosen = params.get("source") or self._active
        if not sources or not chosen:
            raise ActionError("No camera is connected to the video engine.", 409)
        if not isinstance(chosen, str) or chosen not in sources:
            raise ActionError(f"Unknown camera source: {chosen}", 400)
        return chosen

    def _look_action(self, params: dict) -> str | None:
        values = validate_look(params.get("values"))
        label = self._target(params)
        self._cancel_ramp()
        self._set_look(label, {**self._looks.get(label, NEUTRAL), **values})
        self._save()
        return None

    def _look_reset(self, params: dict) -> str | None:
        self._start_ramp(self._target(params), dict(BUILTIN_LOOKS["Natural"]), RESET_SECONDS)
        return "Look reset"

    def _looks_apply(self, params: dict) -> str | None:
        name = required_name(params)
        look = self.library.get(name)
        self._start_ramp(self._target(params), look, APPLY_SECONDS)
        return f"Look: {name}"

    def _looks_save(self, params: dict) -> str | None:
        name = required_name(params)
        self.library.save(name, self._active_look())
        return f"Saved look {name}"

    def _looks_delete(self, params: dict) -> str | None:
        name = required_name(params)
        self.library.delete(name)
        return f"Deleted look {name}"

    def _blur(self, params: dict) -> str | None:
        on, strength, rate, _source = blurctl.parse_action(params)  # one program camera: `source` has no other meaning
        self.arc.wait_probed()
        if not self.arc.capable and on is not False:
            raise ActionError(
                f"Background blur is unavailable: {self.arc.reason or 'the GPU check is still running'}.", 409
            )
        self.arc.configure(on, strength, rate)
        with self.lock:
            engine = self._engine
        if engine is not None and self._blur_built:
            engine.set_blur(on, strength, rate)  # a stdin command between two frames: no restart
        self._wake.set()  # no engine yet, or one without the branch: let the monitor build it with blur
        self._save(force=True)
        return blurctl.message_for(on, strength, rate)

    def _output(self, params: dict) -> str | None:
        raise ActionError(OUTPUT_LATER, 409)

    def _virtualcam(self, params: dict) -> str | None:
        on = required_flag(params)
        with self._idle_lock:
            self._idle.force_running()  # on: start at once; off: nothing is resting any more
            self.canon.rest(False)
        with self.lock:
            engine, self._wanted = self._engine, on
        if on:
            if engine is None:
                self._wake.set()
                return "Camera starting"
            engine.start()
            self._toast("ok", "Program camera on")
            return "Program camera on"
        if engine is not None:
            engine.stop()
        self._toast("info", "Program camera off")
        return "Program camera off"

    # -- frames -------------------------------------------------------------------------------------------------

    def frame(self, source_name: str, width: int) -> bytes | None:
        """One JPEG: "program" (what /dev/video43 shows), "camera" or a camera label."""
        width = clamp_width(width)
        self.touch("preview")  # someone is looking: an idle engine wakes for it
        key = (source_name, width)
        cached = self._frames.get(key)
        if cached and time.monotonic() - cached[0] < FRAME_TTL:
            return cached[1]
        try:
            data = self._grab(source_name, width)
        except (ActionError, RuntimeError, ValueError, OSError) as exc:
            log.debug("video frame %s failed: %s", source_name, exc)
            data = None
        if data:
            self._frames[key] = (time.monotonic(), data)
        else:
            self._frames.pop(key, None)
        return data

    def _stream(self, device: str) -> CameraStream:
        with self.lock:
            stream = self._streams.get(device)
            if stream is None:
                stream = self._streams[device] = CameraStream(device)
            return stream

    def _grab(self, name: str, width: int) -> bytes | None:
        with self.lock:
            src, cameras = self._source, list(self._cameras)
            program = hwcamera.device_for("program", self.cfg, self._plan())
        if name == "program":
            return self._stream(program).latest(width) or self.detect_frame(src.label if src else "")
        label = src.label if src and name == "camera" else name
        camera = next((c for c in cameras if c.label == label), None)
        if camera is None:
            return None
        if camera.kind == "canon":
            # a fed loopback can be read by many; the engine's own reader does not block ours
            return self._stream(camera.device).latest(width) or self.detect_frame(label)
        return self.detect_frame(label)  # a capture card has one reader, and it is the engine


def _detect_fps(cfg: dict[str, str]) -> float:
    try:
        return min(15.0, max(2.0, float(cfg.get("STREAMSTACK_FRAMING_FPS", "8"))))
    except ValueError:
        return 8.0
