"""Video for the Console: OBS scenes, the camera look, outputs and preview frames (see docs/console.md).

`VideoController` keeps three obs-websocket connections, all opened lazily and
dropped on any socket error:

* **events** - owned by the state thread. It subscribes to scene, scene-item,
  filter and output events, so OBS-side changes show up at once; without an
  event the thread still refreshes about once a second (polling fallback).
* **control** - every action and every look write. One lock per connection
  (see `obs.OBS`), so a slow preview can never delay a fader.
* **shots** - `GetSourceScreenshot` only.

Auto mode also adjusts the look while it runs. The Console's manual changes
simply win: auto reads the filter settings fresh on every tick and carries on
from whatever the Console left, so no coordination is needed.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from streamstack import auto, config, system
from streamstack.console import blur as blurctl
from streamstack.console.canon import CanonLink, DemoCanon, empty_canon
from streamstack.console.errors import ActionError
from streamstack.obs import (
    COLOR_FILTER,
    EVENTS_FILTERS,
    EVENTS_OUTPUTS,
    EVENTS_SCENE_ITEMS,
    EVENTS_SCENES,
    LUT_FILTER,
    OBS,
    SHARPEN_FILTER,
    websocket_config,
)

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

BACKOFF = (1.0, 2.0, 5.0, 10.0)
POLL_SECONDS = 1.0
SOURCES_REFRESH_SECONDS = 15.0
MIN_REFRESH_GAP = 0.1
FRAME_TTL = 1 / 15
CAMERA_IDLE_SECONDS = 10.0
LOOK_HOLD_SECONDS = 0.4
RESET_SECONDS = 0.8
APPLY_SECONDS = 1.5
MAX_NAME = 40

LIMITS: dict[str, tuple[float, float]] = {
    "brightness": (-0.5, 0.5),
    "contrast": (-1.0, 1.0),
    "gamma": (-1.0, 1.0),
    "saturation": (-1.0, 1.0),
    "warmth": (-1.0, 1.0),
    "sharpen_amount": (0.0, 1.0),
    "lut_amount": (0.0, 1.0),
}
FLAGS = ("sharpen_enabled", "lut_enabled")

NEUTRAL: dict[str, float | bool] = {
    "brightness": 0.0,
    "contrast": 0.0,
    "gamma": 0.0,
    "saturation": 0.0,
    "warmth": 0.0,
    "sharpen_enabled": False,
    "sharpen_amount": 0.08,
    "lut_enabled": False,
    "lut_amount": 0.35,
}


def _look(**changes: float | bool) -> dict[str, float | bool]:
    return {**NEUTRAL, **changes}


# Tasteful and subtle: each one is a nudge, not a filter.
BUILTIN_LOOKS: dict[str, dict[str, float | bool]] = {
    "Natural": _look(),
    "Warm Studio": _look(brightness=0.02, contrast=0.08, gamma=0.05, saturation=0.06, warmth=0.25),
    "Cool Daylight": _look(brightness=0.02, contrast=0.05, saturation=0.02, warmth=-0.2),
    "Cinematic": _look(contrast=0.12, gamma=-0.04, saturation=-0.1, warmth=0.08, lut_enabled=True, lut_amount=0.4),
    "Punchy": _look(contrast=0.2, saturation=0.2, brightness=0.02, sharpen_enabled=True, sharpen_amount=0.2),
    "Soft Skin": _look(brightness=0.03, contrast=-0.1, gamma=0.12, saturation=-0.02, warmth=0.1),
}

# Event types after which the camera-source list may have changed.
STRUCTURAL_EVENTS = frozenset(
    {
        "SceneCreated",
        "SceneRemoved",
        "SceneNameChanged",
        "SceneListChanged",
        "SceneItemCreated",
        "SceneItemRemoved",
        "SourceFilterCreated",
        "SourceFilterRemoved",
        "SourceFilterListReindexed",
    }
)

NOT_CONNECTED = "OBS is not connected - start the studio first."


class ObsUnavailable(Exception):
    """OBS cannot be reached: `state` is the `obs` value to show (offline, no-websocket, error)."""

    def __init__(self, state: str, message: str | None = None) -> None:
        super().__init__(message or state)
        self.state = state
        self.message = message


def connection_errors() -> tuple[type[BaseException], ...]:
    """Exceptions that mean the socket is gone (not that one request was refused)."""
    try:
        import websocket
    except ImportError:
        return (OSError, ValueError)
    return (OSError, ValueError, websocket.WebSocketException)


def connect_obs(events: int = 0, timeout: float = 5.0) -> OBS:
    """Open one OBS connection, or raise ObsUnavailable saying what to fix."""
    if not system.running("obs"):
        raise ObsUnavailable("offline")
    try:
        conf = json.loads(websocket_config().read_text())
    except (OSError, ValueError):
        conf = {}
    if not conf.get("server_enabled"):
        raise ObsUnavailable(
            "no-websocket",
            "OBS's websocket server is off. Close OBS and run `streamstack studio` (it switches the websocket on), "
            "or enable it in OBS under Tools > WebSocket Server Settings.",
        )
    try:
        return OBS(timeout=timeout, events=events)
    except ImportError as exc:
        raise ObsUnavailable("error", "python3-websocket is not installed (apt install python3-websocket).") from exc
    except RuntimeError as exc:
        raise ObsUnavailable("error", f"{exc} - restart OBS if its websocket password changed.") from exc
    except connection_errors() as exc:
        raise ObsUnavailable("error", f"OBS is running but its websocket is not answering yet ({exc}).") from exc


def clamp_width(width: object) -> int:
    """Preview widths are clamped to 160..1280 and bucketed to multiples of 80 so caches stay small."""
    value = int(width) if isinstance(width, (int, float, str)) and str(width).lstrip("-").isdigit() else 640
    return max(160, min(1280, round(value / 80) * 80))


def timecode(text: object) -> str | None:
    """OBS's "00:01:23.456" without the milliseconds."""
    return str(text).split(".")[0] if text else None


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

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.lock = threading.Lock()

    def file(self) -> Path:
        return self.path or config.user_env_file().parent / "looks.json"

    def user(self) -> dict[str, dict[str, float | bool]]:
        try:
            data = json.loads(self.file().read_text())
        except (OSError, ValueError):
            return {}
        saved: dict[str, dict[str, float | bool]] = {}
        for name, values in data.items() if isinstance(data, dict) else ():
            try:
                saved[str(name)] = {**NEUTRAL, **validate_look(values)}
            except ActionError:
                continue  # a hand-edited bad entry is skipped, never fatal
        return saved

    def listing(self) -> list[dict]:
        items = [{"name": n, "builtin": True, "values": dict(v)} for n, v in BUILTIN_LOOKS.items()]
        items += [{"name": n, "builtin": False, "values": v} for n, v in sorted(self.user().items())]
        return items

    def get(self, name: str) -> dict[str, float | bool]:
        found = BUILTIN_LOOKS.get(name) or self.user().get(name)
        if found is None:
            raise ActionError(f"No look called {name}.", 400)
        return dict(found)

    def _write(self, looks: dict[str, dict]) -> None:
        path = self.file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(looks, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)

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


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


class _Routed(OBS):
    """An OBS-shaped object whose requests go through the controller's control connection.

    It lets the auto.py helpers (current_look, ramp) run unchanged on the shared, reconnecting link.
    """

    def __init__(self, controller: VideoController) -> None:
        self.controller = controller

    def call(self, request: str, **data) -> dict:
        return self.controller.obs_call("control", request, **data)


def empty_state() -> dict:
    return {
        "obs": "offline",
        "obs_error": None,
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
    }


def pick_privacy_scene(names: list[str]) -> str | None:
    if "Be Right Back" in names:
        return "Be Right Back"
    for name in names:
        lowered = name.lower()
        if any(word in lowered for word in ("brb", "right back", "privacy")):
            return name
    return None


class VideoController:
    """OBS through obs-websocket, with a cached state() and the video.* actions."""

    def __init__(
        self,
        cfg: dict,
        notify: Notify | None = None,
        *,
        connector: Callable[[int], OBS] | None = None,
        library: LookLibrary | None = None,
    ) -> None:
        self.cfg = cfg
        self._notify = notify
        self._connector = connector or (lambda events: connect_obs(events))
        self.library = library or LookLibrary()
        self.lock = threading.RLock()
        self._state = empty_state()
        self._selected: str | None = None
        self._scene_sources: dict[str, list[str]] = {}
        self._sources_checked = 0.0
        self._conns: dict[str, OBS] = {}
        self._conn_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # privacy
        self._privacy_prev: str | None = None
        self._privacy_on = False
        # look writes: latest value wins per source
        self._pending: dict[str, dict] = {}
        self._pending_cond = threading.Condition()
        self._writing = False
        self._writer: threading.Thread | None = None
        self._look_hold = 0.0
        self._cancel_ramp = threading.Event()
        self._ramp_thread: threading.Thread | None = None
        self._ramp_lock = threading.Lock()
        self._warned: dict[str, float] = {}
        # frames
        self._frames: dict[tuple[str, int], tuple[float, bytes]] = {}
        self._frame_locks: dict[tuple[str, int], threading.Lock] = {}
        self._frame_locks_guard = threading.Lock()
        device = cfg.get("STREAMSTACK_CAMERA_DEVICE") or config.DEFAULTS["STREAMSTACK_CAMERA_DEVICE"]
        self._camera = CameraStream(str(device))
        self._camera_grabbed = 0.0
        self.canon = CanonLink(cfg, notify)
        self.blur = blurctl.BlurControl(self.notify)  # the background blur filter (console/blur.py)

    # -- notifications ----------------------------------------------------

    def notify(self, level: str, text: str) -> None:
        if self._notify:
            try:
                self._notify(level, text)
            except Exception:
                log.exception("console toast hook failed")  # a broken hook must never break video

    def warn_once(self, key: str, text: str, every: float = 8.0) -> None:
        now = time.monotonic()
        if now - self._warned.get(key, -every) >= every:
            self._warned[key] = now
            self.notify("warn", text)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="streamstack-obs")
        self._thread.start()
        self.canon.start()
        self._writer = threading.Thread(target=self._write_loop, daemon=True, name="streamstack-look")
        self._writer.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel_ramp.set()
        with self._pending_cond:
            self._pending_cond.notify_all()
        self._drop_all()
        self._camera.stop()
        self.canon.stop()
        for thread in (self._thread, self._writer, self._ramp_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=3)

    # -- state ------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            snapshot = copy.deepcopy(self._state)
        snapshot["looks"] = self.library.listing()
        snapshot["canon"] = self.canon.state()
        snapshot["blur"] = self.blur.state()
        return snapshot

    def health_checks(self) -> list[dict[str, str]]:
        """Live checks the Console merges into the health list (they replace same-id entries)."""
        return self.blur.health_checks()

    def _set_obs(self, state: str, error: str | None = None) -> None:
        with self.lock:
            was = self._state["obs"]
            self._state["obs"] = state
            self._state["obs_error"] = error
            if state != "connected":
                fresh = empty_state()
                for key in fresh:
                    if key not in ("obs", "obs_error", "look"):
                        self._state[key] = fresh[key]
                self._scene_sources = {}
                self._sources_checked = 0.0
        if state != "connected":
            self.blur.reset()
        if was != state:
            if state == "connected":
                self.notify("ok", "OBS connected")
            elif was == "connected":
                self.notify("warn", "OBS disconnected")

    # -- connections ------------------------------------------------------

    def _drop(self, kind: str) -> None:
        with self._conn_lock:
            conn = self._conns.pop(kind, None)
        if conn is not None:
            conn.close()

    def _drop_all(self) -> None:
        for kind in list(self._conns):
            self._drop(kind)

    def _conn(self, kind: str) -> OBS:
        with self._conn_lock:
            conn = self._conns.get(kind)
            if conn is not None:
                return conn
        try:
            conn = self._connector(0)
        except ObsUnavailable as exc:
            raise ActionError(exc.message or NOT_CONNECTED, 409) from exc
        with self._conn_lock:
            self._conns[kind] = conn
        return conn

    def obs_call(self, kind: str, request: str, **data) -> dict:
        """One request on the control/shots connection; reconnects once after a dead socket.

        A request OBS refuses raises RuntimeError (callers decide); a lost link raises ActionError(409).
        """
        if self._state["obs"] != "connected":
            raise ActionError(NOT_CONNECTED, 409)
        errors = connection_errors()
        for attempt in (0, 1):
            conn = self._conn(kind)
            try:
                return conn.call(request, **data)
            except RuntimeError:
                raise
            except errors as exc:
                self._drop(kind)
                if attempt:
                    raise ActionError("The connection to OBS was lost.", 409) from exc
        raise ActionError("The connection to OBS was lost.", 409)  # pragma: no cover

    def routed(self) -> OBS:
        return _Routed(self)

    def scene_view(self) -> tuple[bool, str | None, list[str], set[str]]:
        """(connected, program scene, scene names, camera sources) without copying the whole state; for framing."""
        with self.lock:
            st = self._state
            return (
                st["obs"] == "connected",
                st["program_scene"],
                [s["name"] for s in st["scenes"]],
                set(st["sources"]),
            )

    # -- the state thread -------------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            conn: OBS | None = None
            try:
                conn = self._connector(EVENTS_SCENES | EVENTS_SCENE_ITEMS | EVENTS_FILTERS | EVENTS_OUTPUTS)
            except ObsUnavailable as exc:
                self._set_obs(exc.state, exc.message)
            except Exception as exc:
                self._set_obs("error", f"{type(exc).__name__}: {exc}")
            if conn is not None:
                attempt = 0
                self._set_obs("connected")
                try:
                    self._serve(conn)
                except connection_errors() as exc:
                    self._set_obs("error", f"Lost the OBS connection ({exc}).")
                except Exception as exc:
                    self._set_obs("error", f"{type(exc).__name__}: {exc}")
                finally:
                    conn.close()
                    self._drop_all()
                if self._stop.is_set():
                    break
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            if self._stop.wait(delay):
                break

    def _serve(self, conn: OBS) -> None:
        self._refresh(conn, structural=True)
        last = time.monotonic()
        while not self._stop.is_set():
            event = conn.wait_event(POLL_SECONDS)
            structural = False
            seen = event is not None
            while event is not None:
                structural = structural or event.get("eventType") in STRUCTURAL_EVENTS
                event = conn.wait_event(0.02)
            gap = time.monotonic() - last
            if seen and gap < MIN_REFRESH_GAP:
                self._stop.wait(MIN_REFRESH_GAP - gap)
            self._refresh(conn, structural=structural)
            last = time.monotonic()

    def _refresh(self, conn: OBS, structural: bool = False) -> None:
        """Re-read everything the Console shows. Request failures are skipped; socket errors propagate."""
        listing = conn.call("GetSceneList")
        # obs-websocket numbers scenes from the bottom of OBS's list, so highest index first is the OBS order.
        scenes = sorted(listing.get("scenes", []), key=lambda s: -s.get("sceneIndex", 0))
        names = [str(s["sceneName"]) for s in scenes]
        program = listing.get("currentProgramSceneName") or None
        virtualcam = bool(self._try(conn, "GetVirtualCamStatus").get("outputActive"))
        record = self._try(conn, "GetRecordStatus")
        stream = self._try(conn, "GetStreamStatus")

        now = time.monotonic()
        if structural or now - self._sources_checked > SOURCES_REFRESH_SECONDS or not self._scene_sources:
            self._scan_sources(conn, names)
            self._sources_checked = now

        with self.lock:
            privacy_scene = pick_privacy_scene(names)
            if self._privacy_on and program != privacy_scene:
                self._privacy_on = False  # the user cut away from the privacy scene themselves
            sources = self._all_sources()
            selected = self._selected if self._selected in sources else None
            if selected is None:
                in_program = [s for s in self._scene_sources.get(program or "", []) if s in sources]
                selected = in_program[0] if in_program else (sources[0] if sources else None)
            self._state.update(
                scenes=[{"name": n, "active": n == program} for n in names],
                program_scene=program,
                privacy=self._privacy_on,
                privacy_scene=privacy_scene,
                sources=sources,
                selected_source=selected,
                virtualcam=virtualcam,
                recording={
                    "active": bool(record.get("outputActive")),
                    "paused": bool(record.get("outputPaused")),
                    "timecode": timecode(record.get("outputTimecode")) if record.get("outputActive") else None,
                },
                streaming={
                    "active": bool(stream.get("outputActive")),
                    "timecode": timecode(stream.get("outputTimecode")) if stream.get("outputActive") else None,
                },
            )
            holding = self._look_busy()
        try:
            self.blur.refresh(conn, sources, structural)
        except (
            RuntimeError,
            KeyError,
            TypeError,
        ) as exc:  # a surprise in the blur reply must not stop the state thread
            log.warning("background blur state not refreshed: %s", exc)
        if selected and not holding:
            try:
                look = auto.current_look(conn, selected)
            except RuntimeError:
                return  # the source lost its filters; keep the last look
            with self.lock:
                if not self._look_busy():
                    self._state["look"] = {**self._state["look"], **self._clean_look(look)}

    @staticmethod
    def _try(conn: OBS, request: str, **data) -> dict:
        try:
            return conn.call(request, **data)
        except RuntimeError:
            return {}

    @staticmethod
    def _clean_look(look: dict) -> dict:
        out: dict = {}
        for key, value in look.items():
            if key in LIMITS or key in FLAGS:
                out[key] = value if key in FLAGS else round(float(value), 4)
        if "lut_amount" in look:
            out["lut_amount"] = round(float(look["lut_amount"]), 4)
        return out

    def _scan_sources(self, conn: OBS, names: list[str]) -> None:
        """Which sources carry the Lighting & Color filter, per scene (filters are looked up once per source)."""
        has_filter: dict[str, bool] = {}
        found: dict[str, list[str]] = {}
        for scene in names:
            try:
                items = conn.call("GetSceneItemList", sceneName=scene).get("sceneItems", [])
            except RuntimeError:
                continue
            listed: list[str] = []
            for item in items:
                name = item.get("sourceName")
                if not name:
                    continue
                if name not in has_filter:
                    try:
                        conn.call("GetSourceFilter", sourceName=name, filterName=COLOR_FILTER)
                        has_filter[name] = True
                    except RuntimeError:
                        has_filter[name] = False
                if has_filter[name] and name not in listed:
                    listed.append(name)
            found[scene] = listed
        with self.lock:
            self._scene_sources = found

    def _all_sources(self) -> list[str]:
        out: list[str] = []
        for listed in self._scene_sources.values():
            for name in listed:
                if name not in out:
                    out.append(name)
        return out

    def _look_busy(self) -> bool:
        return bool(self._pending) or self._writing or time.monotonic() < self._look_hold

    # -- actions ----------------------------------------------------------

    def act(self, action: str, params: dict) -> str | None:
        if action.startswith("video.canon_"):
            return self.canon.act(action, params)
        handler = {
            "video.scene": self._scene,
            "video.privacy": self._privacy,
            "video.source": self._source,
            "video.look": self._look_action,
            "video.look.reset": self._look_reset,
            "video.looks.apply": self._looks_apply,
            "video.looks.save": self._looks_save,
            "video.looks.delete": self._looks_delete,
            "video.blur": self._blur_action,
            "video.virtualcam": lambda p: self._output("virtualcam", p),
            "video.record": lambda p: self._output("record", p),
            "video.stream": lambda p: self._output("stream", p),
        }.get(action)
        if handler is None:
            raise ActionError(f"unknown action: {action}", 400)
        try:
            return handler(params)
        except RuntimeError as exc:  # OBS refused the request
            raise ActionError(str(exc), 409) from exc

    def _need_obs(self) -> None:
        if self._state["obs"] != "connected":
            raise ActionError(NOT_CONNECTED, 409)

    def _scene(self, params: dict) -> str | None:
        name = required_name(params)
        self._need_obs()
        with self.lock:
            known = [s["name"] for s in self._state["scenes"]]
        if name not in known:
            raise ActionError(f"No scene called {name}.", 400)
        self.obs_call("control", "SetCurrentProgramScene", sceneName=name)
        self._set_program(name)
        return f"On air: {name}"

    def _set_program(self, name: str) -> None:
        with self.lock:
            self._state["program_scene"] = name
            self._state["scenes"] = [{"name": s["name"], "active": s["name"] == name} for s in self._state["scenes"]]
            if self._privacy_on and name != self._state["privacy_scene"]:
                self._privacy_on = False
            self._state["privacy"] = self._privacy_on

    def _privacy(self, params: dict) -> str | None:
        on = required_flag(params)
        self._need_obs()
        with self.lock:
            scene = self._state["privacy_scene"]
            program = self._state["program_scene"]
            names = [s["name"] for s in self._state["scenes"]]
        if on:
            if not scene:
                raise ActionError('There is no privacy scene. Add a scene called "Be Right Back" in OBS.', 409)
            if self._privacy_on:
                return "Privacy is already on."
            self._privacy_prev = None if program == scene else program
            self.obs_call("control", "SetCurrentProgramScene", sceneName=scene)
            self._set_program(scene)
            with self.lock:
                self._privacy_on = True
                self._state["privacy"] = True
            self.notify("warn", "Privacy on - camera is off air")
            return "Privacy on"
        if not self._privacy_on:
            return "Privacy is already off."  # the user switched scenes meanwhile: nothing to restore
        back = self._privacy_prev
        if back is None or back not in names or back == scene:
            back = next((n for n in names if n != scene), None)
        with self.lock:
            self._privacy_on = False
            self._state["privacy"] = False
        if back:
            self.obs_call("control", "SetCurrentProgramScene", sceneName=back)
            self._set_program(back)
        self._privacy_prev = None
        self.notify("ok", "Privacy off - back on air")
        return "Privacy off"

    def _source(self, params: dict) -> str | None:
        name = required_name(params)
        self._need_obs()
        with self.lock:
            if name not in self._state["sources"]:
                raise ActionError(f"{name} has no Lighting & Color filter.", 400)
            self._selected = name
            self._state["selected_source"] = name
        try:
            look = auto.current_look(self.routed(), name)
        except RuntimeError as exc:
            raise ActionError(f"Could not read the look of {name}: {exc}", 409) from exc
        with self.lock:
            self._state["look"] = {**self._state["look"], **self._clean_look(look)}
        return f"Editing {name}"

    def _blur_action(self, params: dict) -> str | None:
        self._need_obs()
        with self.lock:
            sources = list(self._state["sources"])
            selected = self._state["selected_source"]
        return self.blur.act(self.routed(), params, sources, selected)

    def _target(self, params: dict) -> str:
        """The source a look action edits: params["source"], else the selected one."""
        self._need_obs()
        with self.lock:
            sources = list(self._state["sources"])
            chosen = params.get("source") or self._state["selected_source"]
        if not sources or not chosen:
            raise ActionError("No camera source in OBS has the Lighting & Color filter yet.", 409)
        if not isinstance(chosen, str) or chosen not in sources:
            raise ActionError(f"Unknown camera source: {chosen}", 400)
        return chosen

    def _look_action(self, params: dict) -> str | None:
        values = validate_look(params.get("values"))
        source = self._target(params)
        self._cancel_running_ramp()
        with self.lock:
            if source == self._state["selected_source"]:
                self._state["look"].update(values)
            self._look_hold = time.monotonic() + LOOK_HOLD_SECONDS
        with self._pending_cond:
            self._pending.setdefault(source, {}).update(values)  # latest value wins
            self._pending_cond.notify()
        return None

    def _cancel_running_ramp(self) -> None:
        self._cancel_ramp.set()

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            with self._pending_cond:
                while not self._pending and not self._stop.is_set():
                    self._pending_cond.wait(1.0)
                if self._stop.is_set():
                    return
                source, values = next(iter(self._pending.items()))
                del self._pending[source]
                self._writing = True
            try:
                self._wait_ramp()
                self._write_look(source, values)
            except (ActionError, RuntimeError) as exc:
                self.warn_once("look", f"Look change failed: {getattr(exc, 'message', exc)}")
            except Exception as exc:
                self.warn_once("look", f"Look change failed: {type(exc).__name__}: {exc}")
            finally:
                with self._pending_cond:
                    self._writing = False
                self._look_hold = time.monotonic() + LOOK_HOLD_SECONDS

    def _wait_ramp(self) -> None:
        thread = self._ramp_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _write_look(self, source: str, values: dict) -> None:
        """Send look values to OBS now. A filter the source lacks is reported once, the rest still applied."""
        obs = self.routed()
        missing: list[str] = []

        def attempt(label: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except RuntimeError:
                missing.append(label)

        color = {key: values[key] for key in auto.COLOR_KEYS if key in values and key != "warmth"}
        if "warmth" in values:
            color["color_multiply"] = auto.warmth_to_multiply(values["warmth"])
        if color:
            attempt(COLOR_FILTER, lambda: obs.set_filter(source, COLOR_FILTER, color))
        if "sharpen_amount" in values:
            attempt(
                SHARPEN_FILTER, lambda: obs.set_filter(source, SHARPEN_FILTER, {"sharpness": values["sharpen_amount"]})
            )
        if "sharpen_enabled" in values:
            attempt(SHARPEN_FILTER, lambda: obs.enable_filter(source, SHARPEN_FILTER, values["sharpen_enabled"]))
        if "lut_amount" in values:
            attempt(LUT_FILTER, lambda: obs.set_filter(source, LUT_FILTER, {"clut_amount": values["lut_amount"]}))
        if "lut_enabled" in values:
            attempt(LUT_FILTER, lambda: obs.enable_filter(source, LUT_FILTER, values["lut_enabled"]))
        if missing:
            names = ", ".join(dict.fromkeys(missing))
            self.warn_once("filters", f"{source} has no {names} filter in OBS.")

    def _start_ramp(self, source: str, target: dict, seconds: float) -> None:
        self._cancel_ramp.set()
        cancel = threading.Event()
        previous = self._ramp_thread
        with self._ramp_lock:
            self._cancel_ramp = cancel
        with self._pending_cond:
            self._pending.pop(source, None)  # a ramp supersedes unsent drag values

        def work() -> None:
            if previous and previous.is_alive():
                previous.join(timeout=3)
            try:
                self._run_ramp(source, target, seconds, cancel)
            except (ActionError, RuntimeError) as exc:
                self.warn_once("look", f"Look change failed: {getattr(exc, 'message', exc)}")
            except Exception as exc:
                self.warn_once("look", f"Look change failed: {type(exc).__name__}: {exc}")

        self._ramp_thread = threading.Thread(target=work, daemon=True, name="streamstack-ramp")
        self._ramp_thread.start()

    def _run_ramp(self, source: str, target: dict, seconds: float, cancel: threading.Event) -> None:
        obs = self.routed()
        start = auto.current_look(obs, source)
        steps = max(4, int(seconds * 10))
        auto.ramp(obs, source, start, target, seconds=seconds, steps=steps, cancel=cancel)
        if cancel.is_set():
            return
        # Amounts first, then the on/off switches, so a LUT never flashes at full strength.
        self._write_look(source, {k: v for k, v in target.items() if k not in auto.COLOR_KEYS})
        with self.lock:
            if source == self._state["selected_source"]:
                self._state["look"].update(target)
            self._look_hold = time.monotonic() + LOOK_HOLD_SECONDS

    def _look_reset(self, params: dict) -> str | None:
        source = self._target(params)
        self._start_ramp(source, dict(BUILTIN_LOOKS["Natural"]), RESET_SECONDS)
        return "Look reset"

    def _looks_apply(self, params: dict) -> str | None:
        name = required_name(params)
        look = self.library.get(name)
        source = self._target(params)
        self._start_ramp(source, look, APPLY_SECONDS)
        return f"Look: {name}"

    def _looks_save(self, params: dict) -> str | None:
        name = required_name(params)
        with self.lock:
            look = dict(self._state["look"])
        self.library.save(name, look)
        return f"Saved look {name}"

    def _looks_delete(self, params: dict) -> str | None:
        name = required_name(params)
        self.library.delete(name)
        return f"Deleted look {name}"

    def _output(self, kind: str, params: dict) -> str | None:
        on = required_flag(params)
        self._need_obs()
        label, start, stop = {
            "virtualcam": ("Virtual camera", "StartVirtualCam", "StopVirtualCam"),
            "record": ("Recording", "StartRecord", "StopRecord"),
            "stream": ("Streaming", "StartStream", "StopStream"),
        }[kind]
        with self.lock:
            snap = self._state
            active = (
                snap["virtualcam"]
                if kind == "virtualcam"
                else snap["recording" if kind == "record" else "streaming"]["active"]
            )
        if active == on:
            return f"{label} is already {'on' if on else 'off'}."
        if kind == "stream" and on:
            self._check_stream_service()
        try:
            self.obs_call("control", start if on else stop)
        except RuntimeError as exc:
            raise ActionError(f"OBS could not {'start' if on else 'stop'} {label.lower()}: {exc}", 409) from exc
        with self.lock:
            if kind == "virtualcam":
                self._state["virtualcam"] = on
            elif kind == "record":
                self._state["recording"] = {"active": on, "paused": False, "timecode": "00:00:00" if on else None}
            else:
                self._state["streaming"] = {"active": on, "timecode": "00:00:00" if on else None}
        text = {
            "virtualcam": "Virtual camera started" if on else "Virtual camera stopped",
            "record": "Recording started" if on else "Recording stopped",
            "stream": "Stream started" if on else "Stream stopped",
        }[kind]
        self.notify("ok" if on else "info", text)
        return text

    def _check_stream_service(self) -> None:
        try:
            service = self.obs_call("control", "GetStreamServiceSettings").get("streamServiceSettings", {})
        except RuntimeError:
            return  # let StartStream give OBS's own answer
        if not service.get("key"):
            raise ActionError("OBS has no stream key set. Add one in OBS under Settings > Stream.", 409)

    # -- frames -----------------------------------------------------------

    def frame(self, source: str, width: int) -> bytes | None:
        """One JPEG: "program", an OBS source or scene name, or "camera" (the raw camera node)."""
        width = clamp_width(width)
        key = (source, width)
        cached = self._frames.get(key)
        if cached and time.monotonic() - cached[0] < FRAME_TTL:
            return cached[1]
        with self._frame_locks_guard:
            lock = self._frame_locks.setdefault(key, threading.Lock())
        with lock:
            cached = self._frames.get(key)
            if cached and time.monotonic() - cached[0] < FRAME_TTL:
                return cached[1]
            try:
                data = self._grab_camera(width) if source == "camera" else self._grab_obs(source, width)
            except (ActionError, RuntimeError, ValueError, OSError):
                data = None
            if data:
                self._frames[key] = (time.monotonic(), data)
            else:
                self._frames.pop(key, None)
            return data

    def _grab_obs(self, source: str, width: int) -> bytes | None:
        if self._state["obs"] != "connected":
            return None
        with self.lock:
            program = self._state["program_scene"]
            known = {s["name"] for s in self._state["scenes"]} | set(self._state["sources"])
        name = program if source == "program" else source
        if not name or (source != "program" and name not in known):
            return None
        reply = self.obs_call(
            "shots",
            "GetSourceScreenshot",
            sourceName=name,
            imageFormat="jpg",
            imageWidth=width,
            imageCompressionQuality=80,
        )
        image = reply.get("imageData", "")
        return base64.b64decode(image.split(",", 1)[1]) if "," in image else None

    def _grab_camera(self, width: int) -> bytes | None:
        """A one-off ffmpeg grab; a second request within 2 s means someone is watching, so share one process."""
        device = self._camera.device
        now = time.monotonic()
        watching = self._camera.running() or now - self._camera_grabbed < 2.0
        self._camera_grabbed = now
        data = self._camera.latest(width) if watching else self._grab_once(device, width)
        if data is None and self._state["obs"] == "connected":
            selected = self._state["selected_source"]
            if selected:
                return self._grab_obs(selected, width)  # the camera node is busy or absent: OBS's view of it
        return data

    @staticmethod
    def _grab_once(device: str, width: int) -> bytes | None:
        try:
            result = subprocess.run(ffmpeg_camera_argv(device, width), capture_output=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8") else None


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
    """The same API as VideoController on a simulated rig, for --demo, tests and the e2e suite."""

    def __init__(self, notify: Notify | None = None, library: LookLibrary | None = None) -> None:
        self._notify = notify
        self.library = library or LookLibrary()
        self.lock = threading.RLock()
        self.scene = DEMO_SCENES[0]
        self.privacy_on = False
        self.previous: str | None = None
        self.selected = DEMO_SOURCES[0]
        self.looks_by_source = {name: dict(NEUTRAL) for name in DEMO_SOURCES}
        self.virtualcam = True
        self.started: dict[str, float | None] = {"record": None, "stream": None}
        self.cards: dict[str, bytes] = {}
        self.canon = DemoCanon(notify)
        self.blur = blurctl.DemoBlur(DEMO_SOURCES, self.notify)

    def notify(self, level: str, text: str) -> None:
        if self._notify:
            self._notify(level, text)

    def health_checks(self) -> list[dict[str, str]]:
        return self.blur.health_checks()

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
                obs="connected",
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
            )
            state["blur"] = self.blur.state()
            return state

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
            return self.blur.act(params)
        if action == "video.blur.plugin":  # demo only: show the install hint without removing a real plugin
            return self.blur.set_installed(required_flag(params, "installed"))
        if action in ("video.virtualcam", "video.record", "video.stream"):
            return self._output(action.split(".")[1], required_flag(params))
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
