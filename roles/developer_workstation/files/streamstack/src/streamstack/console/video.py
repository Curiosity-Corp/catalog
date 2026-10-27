"""Video for the Console: the look library, the helpers the native controller shares, and the simulated rig.

The real controller is `console.native.NativeVideo` (the program camera is made by the native engine, see
docs/native-video-engine.md). This module holds what it and the demo rig share: the built-in looks, look and
action validation, the saved look library, camera preview streams and the snapshot's empty shape (docs/console.md).
`DemoVideo` is the same contract on a simulated rig, for --demo, tests and the e2e suite.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from streamstack import config, store
from streamstack.console import blur as blurctl
from streamstack.console.arcblur import DemoArc
from streamstack.console.canon import DemoCanon, empty_canon
from streamstack.console.errors import ActionError
from streamstack.presets import catalog as presets_catalog
from streamstack.presets.schema import LOOK_FLAGS, LOOK_LIMITS, LOOK_NEUTRAL

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

FRAME_TTL = 1 / 15
CAMERA_IDLE_SECONDS = 10.0
RESET_SECONDS = 0.8
APPLY_SECONDS = 1.5
MAX_NAME = 40

LIMITS = LOOK_LIMITS
FLAGS = LOOK_FLAGS
NEUTRAL = LOOK_NEUTRAL


def _look(**changes: float | bool) -> dict[str, float | bool]:
    return {**NEUTRAL, **changes}


# The built-in library is data: src/streamstack/data/presets/looks.json (see docs/presets.md). The first six are the
# original tasteful nudges; the rest are film stocks, cinema grades and creator looks.
_CATALOG = presets_catalog.load()
BUILTIN_LOOKS: dict[str, dict[str, float | bool]] = {look["name"]: _look(**look["values"]) for look in _CATALOG.looks}
LOOK_META: dict[str, dict[str, str | None]] = {
    look["name"]: {"category": look["category"], "blurb": look["blurb"], "lut": look["lut"], "thumb": look["thumb"]}
    for look in _CATALOG.looks
}


def clamp_width(width: object) -> int:
    """Preview widths are clamped to 160..1280 and bucketed to multiples of 80 so caches stay small."""
    value = int(width) if isinstance(width, (int, float, str)) and str(width).lstrip("-").isdigit() else 640
    return max(160, min(1280, round(value / 80) * 80))


# ---------------------------------------------------------------------------
# Looks: validation and the saved library (shared by the real and demo controllers)
# ---------------------------------------------------------------------------


def validate_look(values: object) -> dict[str, float | bool]:
    """Check a partial look: known keys, numbers inside LIMITS, real booleans. Raises ActionError(400)."""
    if not isinstance(values, dict) or not values:
        raise ActionError("values must be an object with at least one look setting", 400)
    clean: dict[str, float | bool] = {}
    for key, value in values.items():
        if key in FLAGS:
            if not isinstance(value, bool):
                raise ActionError(f"{key} must be true or false", 400)
            clean[key] = value
        elif key in LIMITS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ActionError(f"{key} must be a number", 400)
            low, high = LIMITS[key]
            if not low <= value <= high:
                raise ActionError(f"{key} must be between {low:g} and {high:g}", 400)
            clean[key] = round(float(value), 4)
        else:
            raise ActionError(f"unknown look setting: {key}", 400)
    return clean


def required_name(params: dict, key: str = "name") -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ActionError(f"{key} is required", 400)
    return value.strip()


def required_flag(params: dict, key: str = "on") -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise ActionError(f"{key} must be true or false", 400)
    return value


class LookLibrary:
    """Built-in looks plus the user's, saved in ~/.config/streamstack/looks.json (mode 600)."""

    def __init__(self, path: Path | None = None, *, ephemeral: bool = False) -> None:
        """`ephemeral`: still atomic, but no fsync and no backup (the demo rig's looks need no disk flush)."""
        self.path = path
        self.ephemeral = ephemeral
        self.lock = threading.Lock()

    def file(self) -> Path:
        return self.path or config.user_env_file().parent / "looks.json"

    def user(self) -> dict[str, dict[str, float | bool]]:
        data = store.read_json(self.file(), {}, validate=store.is_object)
        saved: dict[str, dict[str, float | bool]] = {}
        for name, values in data.items() if isinstance(data, dict) else ():
            try:
                saved[str(name)] = {**NEUTRAL, **validate_look(values)}
            except ActionError:
                continue  # a hand-edited bad entry is skipped, never fatal
        return saved

    def listing(self) -> list[dict]:
        items = [{"name": n, "builtin": True, "values": dict(v), **LOOK_META[n]} for n, v in BUILTIN_LOOKS.items()]
        items += [
            {
                "name": n,
                "builtin": False,
                "values": v,
                "category": "yours",
                "blurb": "Saved by you.",
                "lut": None,
                "thumb": None,
            }
            for n, v in sorted(self.user().items())
        ]
        return items

    def get(self, name: str) -> dict[str, float | bool]:
        found = BUILTIN_LOOKS.get(name) or self.user().get(name)
        if found is None:
            raise ActionError(f"No look called {name}.", 400)
        return dict(found)

    def _write(self, looks: dict[str, dict]) -> None:
        store.write_json(self.file(), looks, mode=0o600, backup=not self.ephemeral, durable=not self.ephemeral)

    def save(self, name: str, look: dict[str, float | bool]) -> None:
        if len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
            raise ActionError(f"A look name is at most {MAX_NAME} characters, without control characters.", 400)
        if name.casefold() in {n.casefold() for n in BUILTIN_LOOKS}:
            raise ActionError(f"{name} is a built-in look; pick another name.", 400)
        with self.lock:
            looks = self.user()
            looks[name] = {key: look[key] for key in NEUTRAL if key in look}
            try:
                self._write(looks)
            except OSError as exc:
                raise ActionError(f"Could not save the look: {exc.strerror or exc}", 409) from exc

    def delete(self, name: str) -> None:
        if name in BUILTIN_LOOKS:
            raise ActionError(f"{name} is a built-in look and cannot be deleted.", 400)
        with self.lock:
            looks = self.user()
            if name not in looks:
                raise ActionError(f"No saved look called {name}.", 400)
            del looks[name]
            try:
                self._write(looks)
            except OSError as exc:
                raise ActionError(f"Could not delete the look: {exc.strerror or exc}", 409) from exc


# ---------------------------------------------------------------------------
# Camera frames
# ---------------------------------------------------------------------------


class JpegSplitter:
    """Cut a concatenated MJPEG byte stream into single JPEG images (SOI ff d8 ... EOI ff d9)."""

    MAX_BUFFER = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        frames: list[bytes] = []
        while True:
            start = self.buffer.find(b"\xff\xd8\xff")
            if start < 0:
                # Keep a possible split marker at the very end.
                del self.buffer[: max(0, len(self.buffer) - 2)]
                break
            end = self.buffer.find(b"\xff\xd9", start + 3)
            if end < 0:
                del self.buffer[:start]
                if len(self.buffer) > self.MAX_BUFFER:
                    self.buffer.clear()  # a runaway stream without an end marker
                break
            frames.append(bytes(self.buffer[start : end + 2]))
            del self.buffer[: end + 2]
        return frames


def ffmpeg_camera_argv(device: str, width: int, fps: float | None = None) -> list[str]:
    """One JPEG to stdout, or (with fps) an MJPEG stream at that rate."""
    argv = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "v4l2", "-i", device]
    if fps is None:
        return [*argv, "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "mjpeg", "-"]
    return [*argv, "-vf", f"fps={fps:g},scale={width}:-2", "-q:v", "6", "-f", "mjpeg", "-"]


class CameraStream:
    """One persistent ffmpeg per camera device, so several viewers share a single grab.

    Starts on the first `latest()` call, keeps the newest frame, and stops
    itself after CAMERA_IDLE_SECONDS without a consumer.
    """

    FPS = 5.0

    def __init__(self, device: str, popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.device = device
        self.popen = popen
        self.width = 0
        self.proc: subprocess.Popen | None = None
        self.frame: bytes | None = None
        self.frame_time = 0.0
        self.last_use = 0.0
        self.cond = threading.Condition()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def latest(self, width: int, wait: float = 2.0) -> bytes | None:
        with self.cond:
            self.last_use = time.monotonic()
            # A bigger request restarts at the bigger size; smaller ones reuse it (the browser scales down).
            if (not self.running() or width > self.width) and not self._start(width):
                return None
            deadline = time.monotonic() + wait
            while self.frame is None or time.monotonic() - self.frame_time > 2.0:
                left = deadline - time.monotonic()
                if left <= 0 or not self.running():
                    break
                self.cond.wait(left)
            fresh = self.frame is not None and time.monotonic() - self.frame_time <= 2.0
            return self.frame if fresh else None

    def _start(self, width: int) -> bool:
        self._kill()
        try:
            self.proc = self.popen(
                ffmpeg_camera_argv(self.device, width, self.FPS),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            self.proc = None
            return False
        self.width = width
        self.frame = None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="streamstack-camera").start()
        return True

    def _read(self, proc: subprocess.Popen) -> None:
        splitter = JpegSplitter()
        stdout = proc.stdout
        try:
            while stdout is not None:
                chunk = stdout.read1(65536) if hasattr(stdout, "read1") else stdout.read(65536)
                if not chunk:
                    break
                for image in splitter.feed(chunk):
                    with self.cond:
                        if self.proc is not proc:
                            return
                        self.frame, self.frame_time = image, time.monotonic()
                        self.cond.notify_all()
                with self.cond:
                    if time.monotonic() - self.last_use > CAMERA_IDLE_SECONDS:
                        break
        except (OSError, ValueError):
            pass  # the pipe closed under us: the process was stopped
        finally:
            with self.cond:
                if self.proc is proc:
                    self._kill()
                self.cond.notify_all()

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass  # already gone

    def stop(self) -> None:
        with self.cond:
            self._kill()
            self.cond.notify_all()


def empty_state() -> dict:
    return {
        "scenes": [],
        "program_scene": None,
        "privacy": False,
        "privacy_scene": None,
        "sources": [],
        "selected_source": None,
        "look": dict(NEUTRAL),
        "limits": {key: list(bounds) for key, bounds in LIMITS.items()},
        "looks": [],
        "virtualcam": False,
        "recording": {"active": False, "paused": False, "timecode": None},
        "streaming": {"active": False, "timecode": None},
        "canon": empty_canon(),
        "engine": None,  # the native engine's block (console/native.py)
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


DEMO_SCENES = ("Canon", "Cam Link", "Side by Side", "Be Right Back")
DEMO_SOURCES = ("Canon EOS 50D", "Cam Link 4K")
DEMO_CARDS = {
    "Canon": "card-canon.jpg",
    "Cam Link": "card-camlink.jpg",
    "Side by Side": "card-sidebyside.jpg",
    "Be Right Back": "card-brb.jpg",
    "Canon EOS 50D": "card-canon.jpg",
    "Cam Link 4K": "card-camlink.jpg",
    "camera": "card-canon.jpg",
}


class DemoVideo:
    """The same API as NativeVideo on a simulated rig, for --demo, tests and the e2e suite."""

    def __init__(self, notify: Notify | None = None, library: LookLibrary | None = None) -> None:
        self._notify = notify
        self.library = library or LookLibrary(ephemeral=True)
        self.lock = threading.RLock()
        self.scene = DEMO_SCENES[0]
        self.privacy_on = False
        self.previous: str | None = None
        self.selected = DEMO_SOURCES[0]
        self.looks_by_source = {name: dict(NEUTRAL) for name in DEMO_SOURCES}
        self.virtualcam = True
        self.idle_state = "running"  # running | idle | waking: `video.idle.demo` puts the demo engine in one
        self.keep_alive = False
        self.started: dict[str, float | None] = {"record": None, "stream": None}
        self.cards: dict[str, bytes] = {}
        self.canon = DemoCanon(notify)
        self.blur = blurctl.DemoBlur(DEMO_SOURCES, self.notify)
        self.arc = DemoArc()  # the native engine's blur path; mode `off` keeps the plain demo blur above

    def notify(self, level: str, text: str) -> None:
        if self._notify:
            self._notify(level, text)

    def health_checks(self) -> list[dict[str, str]]:
        return [] if self.arc.active else self.blur.health_checks()

    def start(self) -> None:
        """Nothing runs in the background; the demo rig is computed on demand."""

    def stop(self) -> None:
        """Nothing to stop."""

    def _clock(self, kind: str) -> str | None:
        began = self.started[kind]
        if began is None:
            return None
        seconds = int(time.monotonic() - began)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    def state(self) -> dict:
        with self.lock:
            state = empty_state()
            state.update(
                scenes=[{"name": n, "active": n == self.scene} for n in DEMO_SCENES],
                program_scene=self.scene,
                privacy=self.privacy_on,
                privacy_scene="Be Right Back",
                sources=list(DEMO_SOURCES),
                selected_source=self.selected,
                look=dict(self.looks_by_source[self.selected]),
                looks=self.library.listing(),
                virtualcam=self.virtualcam,
                recording={
                    "active": self.started["record"] is not None,
                    "paused": False,
                    "timecode": self._clock("record"),
                },
                streaming={"active": self.started["stream"] is not None, "timecode": self._clock("stream")},
                canon=self.canon.state(),
                engine=self._engine_block(),
            )
            state["blur"] = self.arc.overlay(self.blur.state())
            return state

    def _engine_block(self) -> dict:
        """A believable native-engine block, so the Camera page's engine chip has something to show."""
        resting = self.virtualcam and self.idle_state == "idle"
        return {
            "native": True,
            "state": "off" if not self.virtualcam else "idle" if resting else "running",
            "error": None,
            "backend": "vulkan",
            "backend_note": None,
            "decode": "vaapi",
            "source": self.selected,
            "source_mode": "1920x1080 MJPG",
            "output": "/dev/video43 1920x1080 yuyv422",
            "fps": 30.0 if self.virtualcam and not resting else None,
            "dropped": 0,
            "restarts": 0,
            "look_apply_ms": 0.06,
            "crop": None,
            "idle": {
                "mode": "auto",
                "state": self.idle_state if self.virtualcam else "running",
                "keep_alive": self.keep_alive,
                "after_s": 10.0,
                "since_s": 0.0,
                "reason": "no app has read the camera for 10 s" if resting else "",
                "wakes": 0,
                "wake_ms": None,
                "readers": [],
                "sensor": "ok",
                "demand": [],
            },
        }

    def act(self, action: str, params: dict) -> str | None:
        if action.startswith("video.canon_"):
            return self.canon.act(action, params)
        with self.lock:
            return self._act(action, params)

    def _act(self, action: str, params: dict) -> str | None:
        if action == "video.scene":
            name = required_name(params)
            if name not in DEMO_SCENES:
                raise ActionError(f"No scene called {name}.", 400)
            self.scene = name
            self.privacy_on = self.privacy_on and name == "Be Right Back"
            return f"On air: {name}"
        if action == "video.privacy":
            if required_flag(params):
                if not self.privacy_on:
                    self.previous = None if self.scene == "Be Right Back" else self.scene
                    self.scene, self.privacy_on = "Be Right Back", True
                    self.notify("warn", "Privacy on - camera is off air")
                return "Privacy on"
            if self.privacy_on:
                self.scene = self.previous or DEMO_SCENES[0]
                self.privacy_on = False
                self.notify("ok", "Privacy off - back on air")
            return "Privacy off"
        if action == "video.source":
            name = required_name(params)
            if name not in DEMO_SOURCES:
                raise ActionError(f"{name} has no Lighting & Color filter.", 400)
            self.selected = name
            return f"Editing {name}"
        if action in ("video.look", "video.look.reset", "video.looks.apply"):
            return self._look(action, params)
        if action == "video.looks.save":
            name = required_name(params)
            self.library.save(name, self.looks_by_source[self.selected])
            return f"Saved look {name}"
        if action == "video.looks.delete":
            name = required_name(params)
            self.library.delete(name)
            return f"Deleted look {name}"
        if action == "video.blur":
            if self.arc.blocked() and params.get("on") is not False:
                raise ActionError(f"Background blur is unavailable: {self.arc.reason()}.", 409)
            return self.blur.act(params)
        if action == "video.blur.arc":  # demo only: put the native blur path in a state (running, unavailable, ...)
            try:
                return self.arc.set_mode(str(params.get("mode", "")))
            except ValueError as exc:
                raise ActionError(str(exc), 400) from exc
        if action == "video.blur.plugin":  # demo only: show the install hint without removing a real plugin
            return self.blur.set_installed(required_flag(params, "installed"))
        if action in ("video.virtualcam", "video.record", "video.stream"):
            return self._output(action.split(".")[1], required_flag(params))
        if action == "video.keepalive":
            self.keep_alive = required_flag(params)
            if self.keep_alive and self.idle_state == "idle":
                self.idle_state = "running"  # the real engine wakes on keep alive
            return "Camera kept awake" if self.keep_alive else "Camera may idle when no app uses it"
        if action == "video.idle.demo":  # demo only: put the engine in a state so the page can show it
            state = str(params.get("state", ""))
            if state not in ("running", "idle", "waking"):
                raise ActionError("state must be running, idle or waking", 400)
            self.idle_state = state
            self.canon.rest(state == "idle")  # the simulated body leaves live view with the engine
            return f"Demo engine {state}"
        raise ActionError(f"unknown action: {action}", 400)

    def _look(self, action: str, params: dict) -> str | None:
        source = params.get("source") or self.selected
        if source not in DEMO_SOURCES:
            raise ActionError(f"Unknown camera source: {source}", 400)
        if action == "video.look":
            self.looks_by_source[source].update(validate_look(params.get("values")))
            return None
        if action == "video.look.reset":
            self.looks_by_source[source] = dict(NEUTRAL)
            return "Look reset"
        name = required_name(params)
        self.looks_by_source[source] = self.library.get(name)
        return f"Look: {name}"

    def _output(self, kind: str, on: bool) -> str:
        label = {"virtualcam": "Virtual camera", "record": "Recording", "stream": "Stream"}[kind]
        if kind == "virtualcam":
            self.virtualcam = on
            self.idle_state = "running"
        else:
            self.started[kind] = time.monotonic() if on else None
        text = f"{label} {'started' if on else 'stopped'}"
        self.notify("ok" if on else "info", text)
        return text

    def frame(self, source: str, width: int) -> bytes | None:
        with self.lock:
            if source == "program":
                name = self.scene
            elif source in DEMO_CARDS:
                name = source
            else:
                return None
        file = DEMO_CARDS[name]
        if file not in self.cards:
            try:
                self.cards[file] = (config.data_dir() / "console/demo" / file).read_bytes()
            except OSError:
                return None
        return self.cards[file]
