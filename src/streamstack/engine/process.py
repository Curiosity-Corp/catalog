"""The supervisor: one ffmpeg child runs the camera path, this module keeps it alive and talks to it.

* **Commands** go to the child's stdin (`graph.Command`), so looks, crop windows and the privacy blackout change
  between two frames. Only controls that differ from what the child already has are sent.
* **Restart** happens only when the graph itself must change (another camera, another output size, or a look that
  moves the GPU sharpen to another baked step, see `graph.GraphKey`) or the child dies; then the new child starts from
  the current look and window, so the picture comes back as it was. A deliberate restart starts the new child at
  once; after a crash the wait grows 0.5 s, 1 s, 2 s, 5 s and stays at 5 s; a camera that is unplugged shows as
  "waiting". A change of the baked step waits `KEY_SETTLE` seconds for the look to stop moving (a slider drag or a
  look ramp crosses several steps and costs one restart).
* **Background blur** (`Spec.blur`, see blurfeed.py) adds two pipes per child, camera frames out and person masks in,
  and a pump thread that answers one with the other. On, off and strength are commands to `maskedmerge@blur` and
  `gblur@bg` like any other control; nothing restarts.
* **Delivery guard** keeps the output node fed from the outside: before every child start `before_child(spec)` may
  answer "not ready: <why>" (the source camera has no producer), and the supervisor then waits and asks again instead
  of letting ffmpeg fail on the camera; `on_exit` is told when a child ended. The Console uses both to run the
  "camera reconnecting" slate (engine/slate.py) on the same node while no child writes it. A child that is alive but
  whose frame counter stops for `STALL_SECONDS` (a bridge that died while ffmpeg waits in `read()`) is stopped and
  treated like any other exit.
* **Progress** (`-progress`) gives frames per second and drops; the second output is the low-rate uncropped JPEG the
  framing detector reads (`latest_frame`).
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from streamstack.engine import graph, shaders
from streamstack.engine.blurfeed import BlurFeed
from streamstack.engine.graph import Command, Rect, Spec
from streamstack.engine.jpeg import JpegSplitter

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]
BACKOFF = (0.5, 1.0, 2.0, 5.0)
STABLE_SECONDS = 10.0  # a child that ran this long resets the backoff
STOP_GRACE = (2.0, 2.0)  # seconds after `q`, after SIGTERM; then SIGKILL
FRAME_MAX_AGE = 3.0
STALL_SECONDS = 4.0  # no new frame for this long on a running child means its source has stopped
STALL_FIRST = 10.0  # a camera may need this long to deliver its first frame (a Cam Link waking, a PTP bridge)
NOT_READY_POLL = 1.0  # how often a source that is not ready is asked again
BeforeChild = Callable[[Spec], str | None]
OnExit = Callable[[str, int], None]
KEY_SETTLE = 0.6  # seconds a look must rest on a new baked step before the child is rebuilt for it


class VideoEngine:
    """Owns the ffmpeg child for one `Spec` and everything that changes while it runs."""

    def __init__(
        self,
        spec: Spec,
        look: dict | None = None,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        notify: Notify | None = None,
        pass_fds: tuple[int, ...] = (),
        before_child: BeforeChild | None = None,
        on_exit: OnExit | None = None,
    ) -> None:
        self._before_child = before_child
        self._on_exit = on_exit
        self._pass_fds = pass_fds
        self._spec = spec
        self._look = dict(look or {})
        self._rect: Rect | None = None
        self._blackout = False
        self._blur: BlurFeed | None = None
        self._blur_on = False
        self._blur_strength = 8
        self._popen = popen
        self._clock = clock
        self._notify = notify
        self.lock = threading.RLock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._sent: dict[tuple[str, str], str] | None = None
        self._state = "stopped"  # stopped | starting | running | waiting | error
        self._error: str | None = None
        self._restarts = 0
        self._started_at = 0.0
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._progress: dict[str, str] = {}
        self._frame: tuple[float, bytes] | None = None
        self._frame_cond = threading.Condition()
        self._last_apply_ms: float | None = None
        self._stalled: str | None = None  # set by the watchdog for the child it is stopping
        self._not_ready: str | None = None  # why the source is not ready now, None when it is
        self._built_key: graph.GraphKey | None = None  # what the running child has baked in
        self._key_timer: threading.Timer | None = None
        self._planned = False  # the next child exit is a deliberate restart: no crash back-off
        self._no_shader = False  # the sharpen shader could not be written: sharpen is off until restart

    # -- lifecycle -----------------------------------------------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            if self._thread and self._thread.is_alive():
                return
            self._halt.clear()
            self._state = "starting"
            self._thread = threading.Thread(target=self._run, daemon=True, name="streamstack-engine")
            self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        self._cancel_key_timer()
        with self.lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=sum(STOP_GRACE) + 3)
        with self.lock:
            self._state = "stopped"

    @property
    def spec(self) -> Spec:
        return self._spec

    def set_spec(self, spec: Spec) -> None:
        """Change the graph itself (another source or size). The one thing that restarts the child."""
        with self.lock:
            if spec == self._spec:
                return
            self._spec = spec
            self._rect = None
        self._restart_child()

    def _restart_child(self) -> None:
        with self.lock:
            proc = self._proc
            self._sent = None
            self._planned = True
        if proc is not None:
            self._terminate(proc)
        self._wake.set()

    # -- runtime changes ------------------------------------------------------------------------------------------

    def apply_look(self, look: dict) -> float:
        """Send what changed in `look` to the running child. Returns the time spent writing, in milliseconds."""
        began = self._clock()
        with self.lock:
            self._look = dict(look)
            commands = self._pending_commands()
            rebuild = self._key_for(look) != self._built_key and self._proc is not None
        self._send(commands)
        if rebuild:
            self._schedule_rebuild()
        else:
            self._cancel_key_timer()
        elapsed = (self._clock() - began) * 1000
        self._last_apply_ms = round(elapsed, 3)
        return elapsed

    def set_blackout(self, on: bool) -> None:
        with self.lock:
            if on == self._blackout:
                return
            self._blackout = on
            commands = self._pending_commands()
        self._send(commands)

    def attach_blur(self, feed: BlurFeed) -> None:
        """Give the engine its blur feed (before `start`). Only a spec with a blur branch uses it."""
        feed.set_window(self.blur_box)
        with self.lock:
            self._blur = feed

    def set_blur(self, on: bool | None = None, strength: int | None = None, rate: int | None = None) -> None:
        """Blur on or off, its strength (1..20) and how often the mask is recomputed, all without a restart."""
        with self.lock:
            feed = self._blur
            if on is not None:
                self._blur_on = on
            if strength is not None:
                self._blur_strength = strength
            commands = self._pending_commands()
        if feed is not None:
            if rate is not None:
                feed.set_rate(rate)
            if on is not None:
                feed.set_enabled(on)
        self._send(commands)

    def blur_box(self) -> tuple[float, float, float, float] | None:
        """The crop window as fractions of the camera frame, to cut the mask to the picture (None = whole frame)."""
        with self.lock:
            rect, src = self._rect, self._spec.source
        if rect is None or src.width <= 0 or src.height <= 0:
            return None
        return (rect.x / src.width, rect.y / src.height, (rect.x + rect.w) / src.width, (rect.y + rect.h) / src.height)

    def _key_for(self, look: dict) -> graph.GraphKey | None:
        """What `look` bakes into the graph, with sharpening dropped if its shader could not be written."""
        key = graph.graph_key(self._spec, look)
        if key is not None and self._no_shader:
            key = replace(key, sharpen=0.0)
        return key

    def _schedule_rebuild(self) -> None:
        """The look moved to another baked step: rebuild the child once the look has rested for KEY_SETTLE."""
        with self.lock:
            if self._key_timer is not None:
                self._key_timer.cancel()
            timer = threading.Timer(KEY_SETTLE, self._rebuild_if_needed)
            timer.daemon = True
            self._key_timer = timer
        timer.start()

    def _cancel_key_timer(self) -> None:
        with self.lock:
            timer, self._key_timer = self._key_timer, None
        if timer is not None:
            timer.cancel()

    def _rebuild_if_needed(self) -> None:
        with self.lock:
            self._key_timer = None
            stale = self._proc is not None and self._key_for(self._look) != self._built_key
        if stale and not self._halt.is_set():
            self._restart_child()

    def _controls(self) -> dict[tuple[str, str], str]:
        controls = graph.look_controls(self._look, self._spec.backend, self._blackout, self._built_key)
        if self._spec.blur is not None and self._blur is not None:
            controls.update(graph.blur_controls(self._blur_on, self._blur_strength))
        return controls

    def set_window(self, rect: Rect | None) -> None:
        """Crop to `rect` (source pixels); None shows the whole frame."""
        with self.lock:
            if self._spec.backend != "vulkan":
                return
            full = Rect(0, 0, self._spec.source.width, self._spec.source.height)
            target = rect or full
            if target == (self._rect or full):
                return
            grow = target.w * target.h > self._sent_rect_area()
            self._rect = None if target == full else target
            commands = graph.crop_commands(target, grow=grow)
        self._send(commands)

    def _sent_rect_area(self) -> int:
        rect = self._rect
        return rect.w * rect.h if rect else self._spec.source.width * self._spec.source.height

    def _pending_commands(self) -> list[Command]:
        controls = self._controls()
        if self._proc is None:
            return []  # the next child starts from self._look
        commands = graph.diff_controls(self._sent, controls)
        self._sent = controls
        return commands

    def _send(self, commands: list[Command]) -> None:
        if not commands:
            return
        with self.lock:
            proc = self._proc
        stdin = proc.stdin if proc is not None else None
        if stdin is None:
            return
        data = "".join(c.line() for c in commands).encode()
        try:
            stdin.write(data)
            stdin.flush()
        except BlockingIOError:
            log.warning("engine: command pipe full, dropping %d commands (the next change resends)", len(commands))
            with self.lock:
                self._sent = None
        except (BrokenPipeError, OSError, ValueError):
            pass  # the child is gone: the supervisor restarts it with the current state

    # -- state ------------------------------------------------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            spec, state, error = self._spec, self._state, self._error
            progress = dict(self._progress)
            uptime = self._clock() - self._started_at if state == "running" else 0.0
            restarts, apply_ms = self._restarts, self._last_apply_ms
            proc = self._proc
        fps = _float(progress.get("fps"))
        return {
            "state": state,
            "error": error,
            "backend": spec.backend,
            "decode": "vaapi" if spec.source.mjpeg and spec.backend == "vulkan" else "none (raw)",
            "source": spec.source.label,
            "source_mode": f"{spec.source.width}x{spec.source.height} {spec.source.fourcc}",
            "output": f"{spec.out_device} {spec.out_width}x{spec.out_height} {spec.output_format}",
            "fps": fps,
            "dropped": int(_float(progress.get("drop_frames")) or 0),
            "speed": progress.get("speed", "").strip() or None,
            "restarts": restarts,
            "uptime": round(uptime, 1),
            "look_apply_ms": apply_ms,
            "pid": proc.pid if proc is not None else None,
            "crop": None if self._rect is None else vars(self._rect),
            "blackout": self._blackout,
            "not_ready": self._not_ready,
            "blur": self._blur.stats() if self._blur is not None and spec.blur is not None else None,
            "sharpen": "gpu" if graph.graph_key(spec, {}) is not None and not self._no_shader else "cpu",
        }

    def latest_frame(self, max_age: float = FRAME_MAX_AGE) -> bytes | None:
        with self._frame_cond:
            if self._frame and self._clock() - self._frame[0] <= max_age:
                return self._frame[1]
        return None

    def wait_frame(self, timeout: float) -> bytes | None:
        deadline = self._clock() + timeout
        with self._frame_cond:
            while True:
                if self._frame and self._clock() - self._frame[0] <= FRAME_MAX_AGE:
                    return self._frame[1]
                left = deadline - self._clock()
                if left <= 0:
                    return None
                self._frame_cond.wait(left)

    # -- the supervisor loop ------------------------------------------------------------------------------------

    def _run(self) -> None:
        failures = 0
        while not self._halt.is_set():
            if self._source_not_ready():
                self._wake.wait(NOT_READY_POLL)
                self._wake.clear()
                continue
            began = self._clock()
            try:
                self._run_child()
            except (OSError, graph.GraphError) as exc:
                self._fail(f"cannot start ffmpeg: {exc}" if isinstance(exc, OSError) else str(exc))
            ran = self._clock() - began
            if self._halt.is_set():
                break
            failures = 0 if ran >= STABLE_SECONDS else failures + 1
            with self.lock:
                planned, self._planned = self._planned, False
            if planned:
                failures = 0  # a deliberate restart (a new baked step): straight back up
            wait = BACKOFF[min(failures, len(BACKOFF)) - 1] if failures else 0.1
            with self.lock:
                if self._state != "error":
                    self._state = "waiting"
                self._restarts += 1
                reason = self._error or "the video engine stopped"
            self._child_exited(reason, failures)
            self._wake.wait(wait)
            self._wake.clear()

    def _source_not_ready(self) -> bool:
        """Ask the guard whether the source can be read now. While it cannot, the state is `waiting` and the reason is
        the guard's, and nothing is started (a camera that is gone must not make ffmpeg fail every second)."""
        if self._before_child is None:
            return False
        with self.lock:
            spec = self._spec
        try:
            reason = self._before_child(spec)
        except Exception:
            log.exception("engine delivery guard failed; starting the child anyway")
            reason = None
        with self.lock:
            changed = reason != self._not_ready
            self._not_ready = reason
            if reason is not None:
                self._state, self._error = "waiting", reason
        if reason is not None and changed:
            log.warning("engine: %s", reason)
        return reason is not None

    def _child_exited(self, reason: str, failures: int) -> None:
        if self._on_exit is None:
            return
        try:
            self._on_exit(reason, failures)
        except Exception:
            log.exception("engine exit hook failed")

    def _fail(self, message: str) -> None:
        with self.lock:
            repeat = self._state == "error" and self._error == message
            self._state, self._error = "error", message
        log.warning("engine: %s", message)
        if not repeat:
            self._toast("warn", message)  # a camera that stays unplugged must not toast every retry

    def _toast(self, level: str, text: str) -> None:
        if self._notify:
            try:
                self._notify(level, text)
            except Exception:
                log.exception("engine toast hook failed")

    def _run_child(self) -> None:
        with self.lock:
            spec, look, rect, blackout = self._spec, dict(self._look), self._rect, self._blackout
            self._state, self._progress = "starting", {}
            self._stderr.clear()
        key = self._prepare_key(spec, look)
        with self.lock:
            self._built_key = key
        progress_r, progress_w = os.pipe()
        blur_pipes = self._open_blur_pipes(spec)
        blur_fds = (blur_pipes[1], blur_pipes[2]) if blur_pipes else None  # (seg write, mask read): the child's ends
        blur_args = (self._blur_on, self._blur_strength) if blur_pipes else None
        try:
            argv = graph.build_argv(
                spec, look, rect, blackout, progress_fd=progress_w, blur=blur_args, blur_fds=blur_fds, key=key
            )
        except graph.GraphError:
            _close_all(progress_r, progress_w, *(blur_pipes or ()))
            raise
        child_fds = tuple(blur_fds or ())
        try:
            proc = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(progress_w, *child_fds, *self._pass_fds),
                bufsize=0,
            )
        except OSError:
            _close_all(progress_r, *((blur_pipes[0], blur_pipes[3]) if blur_pipes else ()))  # finally closes the rest
            raise
        finally:
            os.close(progress_w)
            if blur_pipes:
                _close_all(blur_pipes[1], blur_pipes[2])  # the child has its ends; ours stay with the pump
        if proc.stdin is not None:
            os.set_blocking(proc.stdin.fileno(), False)
        with self.lock:
            self._proc = proc
            self._sent = self._controls()
            self._started_at = self._clock()
        readers = [
            threading.Thread(target=self._read_frames, args=(proc,), daemon=True, name="streamstack-engine-frames"),
            threading.Thread(target=self._read_stderr, args=(proc,), daemon=True, name="streamstack-engine-log"),
            threading.Thread(
                target=self._read_progress, args=(progress_r, proc), daemon=True, name="streamstack-engine-progress"
            ),
        ]
        if blur_pipes and self._blur is not None:
            readers.append(
                threading.Thread(
                    target=self._blur.pump,
                    args=(blur_pipes[0], blur_pipes[3]),
                    daemon=True,
                    name="streamstack-engine-blur",
                )
            )
        for thread in readers:
            thread.start()
        watchdog = threading.Thread(
            target=self._watch_stall, args=(proc,), daemon=True, name="streamstack-engine-watch"
        )
        watchdog.start()
        code = proc.wait()
        for thread in (*readers, watchdog):
            thread.join(timeout=1)
        with self.lock:
            self._proc, self._sent = None, None
            stalled, self._stalled = self._stalled, None
        if stalled and not self._halt.is_set():
            self._fail(stalled)
            return
        if self._halt.is_set() or code in (-15, 255):
            return
        tail = " | ".join(list(self._stderr)[-3:]) or f"exit code {code}"
        self._fail(f"the video engine stopped: {tail}")

    def _watch_stall(self, proc: subprocess.Popen) -> None:
        """Stop a child whose frame counter has not moved for STALL_SECONDS (STALL_FIRST before its first frame).

        ffmpeg blocks in the V4L2 read when the producer of a loopback source goes away, so it neither exits nor
        writes: the supervisor would wait forever while the program node goes quiet."""
        began = last_move = self._clock()
        frame, moved = None, False
        while proc.poll() is None and not self._halt.is_set():
            self._halt.wait(0.5)
            with self.lock:
                now_frame = self._progress.get("frame")
            now = self._clock()
            if now_frame != frame:
                frame, last_move, moved = now_frame, now, True
            limit = STALL_SECONDS if moved else STALL_FIRST
            if now - (last_move if moved else began) < limit:
                continue
            seconds = now - (last_move if moved else began)
            with self.lock:
                self._stalled = (
                    f"the camera stopped delivering frames ({seconds:.0f} s without one)"
                    if moved
                    else f"the camera delivered no frame in {seconds:.0f} s"
                )
            log.warning("engine: %s", self._stalled)
            self._terminate(proc)
            return

    def _prepare_key(self, spec: Spec, look: dict) -> graph.GraphKey | None:
        """The baked step for a new child, with its shader on disk. A shader that cannot be written turns sharpening
        off (and says so once) instead of stopping the picture."""
        key = self._key_for(look)
        if key is None or key.sharpen <= 0:
            return key
        try:
            shaders.ensure_sharpen(spec.shaders, key.sharpen)
        except OSError as exc:
            log.warning("engine: cannot write the sharpen shader under %s: %s", spec.shaders, exc)
            self._no_shader = True
            self._toast("warn", f"Sharpening is off: the GPU shader cannot be written ({exc.strerror or exc}).")
            return replace(key, sharpen=0.0)
        return key

    def _open_blur_pipes(self, spec: Spec) -> tuple[int, int, int, int] | None:
        """(seg read, seg write, mask read, mask write) for a child that has a blur branch, else None."""
        if spec.blur is None:
            return None
        if self._blur is None:
            raise graph.GraphError("the spec has a blur branch but the engine has no blur feed (attach_blur)")
        seg_r, seg_w = os.pipe()
        try:
            mask_r, mask_w = os.pipe()
        except OSError:
            _close_all(seg_r, seg_w)
            raise
        return seg_r, seg_w, mask_r, mask_w

    def _read_frames(self, proc: subprocess.Popen) -> None:
        splitter, stdout = JpegSplitter(), proc.stdout
        try:
            while stdout is not None:
                chunk = stdout.read(65536)
                if not chunk:
                    return
                for image in splitter.feed(chunk):
                    with self._frame_cond:
                        self._frame = (self._clock(), image)
                        self._frame_cond.notify_all()
        except (OSError, ValueError):
            return  # the pipe closed with the child

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        try:
            while proc.stderr is not None:
                line = proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            return

    def _read_progress(self, fd: int, proc: subprocess.Popen) -> None:
        try:
            with os.fdopen(fd, "r", errors="replace") as handle:
                for raw in handle:
                    key, _, value = raw.strip().partition("=")
                    if not key:
                        continue
                    with self.lock:
                        self._progress[key] = value
                        if key == "progress" and self._state in ("starting", "waiting", "error"):
                            self._state, self._error = "running", None
        except (OSError, ValueError):
            return

    def _terminate(self, proc: subprocess.Popen) -> None:
        """Ask ffmpeg to quit (`q`), then SIGTERM, then SIGKILL. Only ever our own child."""
        try:
            if proc.stdin is not None:
                os.set_blocking(proc.stdin.fileno(), True)
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        for grace, action in ((STOP_GRACE[0], None), (STOP_GRACE[1], proc.terminate), (2.0, proc.kill)):
            if action is not None:
                try:
                    action()
                except OSError:
                    return
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue


def _close_all(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "N/A") else None
    except ValueError:
        return None


def resized(spec: Spec, width: int, height: int) -> Spec:
    return replace(spec, out_width=width, out_height=height)
