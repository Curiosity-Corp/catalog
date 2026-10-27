"""The two pipes of background blur: camera frames out of the engine child, person masks back into it.

    ffmpeg child  --[seg] 192x192 rgb24, every camera frame-->  pump (reader)  --latest frame-->  inference worker
    ffmpeg child  <--mask 320x180 yuv444p, one per frame------  pump (writer)  <--latest mask---  inference worker

The pump answers every camera frame with exactly one mask, at once, from the newest mask the inference worker has
finished; it never waits for the GPU. That is what keeps the program picture moving: the child pairs picture and mask
by frame number, so a late mask would hold the picture, and a stalled worker only ages the mask (after `STALE_SECONDS`
the pump sends "no blur" and says so) instead of freezing the camera.

Inference runs on its own thread, on the newest frame only (a slow GPU skips frames, it does not queue them), and only
every `rate`-th frame: the Console's Smoothness setting (1 = every frame, 2 = every other, 3 = every third).
The segmenter is opened the first time blur is switched on, so a machine that never uses blur never loads OpenVINO.

Everything the Console shows comes from `stats()`: the state, the device, the measured mask rate and inference time,
and how old the mask is, in milliseconds, by the time it is applied to a picture.
"""

from __future__ import annotations

import collections
import fcntl
import logging
import os
import statistics
import threading
import time
from collections.abc import Callable

from streamstack.engine import segment
from streamstack.engine.graph import BlurGraph
from streamstack.engine.segment import MaskSmoother, Resampler, Segmenter, SegmentError

log = logging.getLogger(__name__)

STALE_SECONDS = 0.5  # a mask older than this is not applied
SAMPLES = 90  # timing samples kept (about 3 s at 30 fps)
RATE_WINDOW = 2.0  # seconds the measured mask rate averages over
PIPE_BYTES = 1 << 20
# How long the pump waits for the mask of the frame it just read. The program picture spends about 40 ms in the
# graph (libplacebo holds a frame) before it meets its mask, so a mask that arrives within this wait costs the
# picture nothing, and the mask belongs to the frame it is applied to instead of the one before it.
MASK_WAIT = 0.02
MASK_LEAD = 1  # masks written ahead of the picture, in frames

Box = tuple[float, float, float, float]


class BlurFeed:
    """Owns the segmenter and the pump. One per engine; it outlives the engine's ffmpeg child restarts."""

    def __init__(
        self,
        graph: BlurGraph,
        open_segmenter: Callable[[], Segmenter],
        *,
        clock: Callable[[], float] = time.monotonic,
        window: Callable[[], Box | None] = lambda: None,
        smoother: MaskSmoother | None = None,
    ) -> None:
        self.graph = graph
        self._open = open_segmenter
        self._clock = clock
        self._window = window
        self._smoother = smoother or MaskSmoother()
        self._resample = Resampler(graph.mask_width, graph.mask_height)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._on = False
        self._rate = 1
        self._state = "idle"  # idle | loading | running | stalled | failed
        self._error: str | None = None
        self._label = ""
        self._halt = threading.Event()
        self._worker: threading.Thread | None = None
        self._frame: tuple[int, float, bytes] | None = None  # (sequence, arrival time, rgb)
        self._raw: tuple[object, float, int] | None = None  # (mask array, arrival time of its frame, its sequence)
        self._infer_ms: collections.deque[float] = collections.deque(maxlen=SAMPLES)
        self._age_ms: collections.deque[float] = collections.deque(maxlen=SAMPLES)
        self._done: collections.deque[float] = collections.deque()
        self._pumped = 0
        self._clear = bytes(graph.mask_width * graph.mask_height * 3)  # background weight 0: nothing blurred

    # -- settings ----------------------------------------------------------------------------------------------

    def set_enabled(self, on: bool) -> None:
        with self._cond:
            if on == self._on:
                return
            self._on = on
            if not on:
                self._raw = None
                if self._state != "failed":
                    self._state = "idle"
            elif self._state == "failed":
                self._state, self._error = "loading", None  # switching it on again is a retry
            elif self._state in ("idle", "stalled"):
                self._state = "loading" if self._worker is None or not self._worker.is_alive() else "running"
            self._cond.notify_all()
        if on:
            self._ensure_worker()

    def set_window(self, window: Callable[[], Box | None]) -> None:
        """Where the picture is cropped to, as fractions of the camera frame (the engine's crop window)."""
        self._window = window

    def set_rate(self, rate: int) -> None:
        with self._cond:
            self._rate = max(1, int(rate))

    @property
    def enabled(self) -> bool:
        return self._on

    def stop(self) -> None:
        self._halt.set()
        with self._cond:
            self._cond.notify_all()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)

    # -- the inference worker ------------------------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._halt.clear()
            self._worker = threading.Thread(target=self._work, daemon=True, name="streamstack-blur-infer")
            self._worker.start()

    def _work(self) -> None:
        try:
            segmenter = self._open()
        except (SegmentError, OSError) as exc:
            self._fail(str(exc))
            return
        except (
            Exception
        ) as exc:  # the segmenter factory touches OpenVINO and the disk: nothing here may kill the engine
            log.exception("blur: opening the segmenter failed")
            self._fail(f"the segmenter could not start: {exc}")
            return
        with self._cond:
            self._label = getattr(segmenter, "label", "")
            if self._state in ("loading", "idle") and self._on:
                self._state = "running"
        seen = 0
        try:
            while not self._halt.is_set():
                with self._cond:
                    while True:
                        if self._halt.is_set():
                            return
                        frame = self._frame
                        if self._on and frame is not None and frame[0] != seen:
                            break
                        self._cond.wait(0.5)
                    seq, arrived, rgb = frame
                    rate = self._rate
                gap = seq - seen
                seen = seq
                if rate > 1 and seq % rate != 0 and gap <= 1:
                    continue
                began = self._clock()
                try:
                    raw = segmenter.infer(rgb)
                except SegmentError as exc:
                    self._fail(str(exc))
                    return
                finished = self._clock()
                with self._cond:
                    self._raw = (raw, arrived, seq)
                    self._infer_ms.append((finished - began) * 1000)
                    self._done.append(finished)
                    if self._state in ("loading", "stalled"):
                        self._state = "running"
                    self._cond.notify_all()
        finally:
            try:
                segmenter.close()
            except Exception:
                log.exception("blur: closing the segmenter failed")

    def _fail(self, message: str) -> None:
        log.warning("blur: %s", message)
        with self._cond:
            self._state, self._error, self._raw = "failed", message, None
            self._on = False

    def retry(self) -> None:
        """Clear a failure so the next switch-on tries again."""
        with self._cond:
            if self._state == "failed":
                self._state, self._error = "idle", None

    # -- the pump (one per ffmpeg child) -------------------------------------------------------------------------

    def pump(self, seg_fd: int, mask_fd: int) -> None:
        """Answer every camera frame on `seg_fd` with one mask on `mask_fd` until either pipe closes.

        Takes ownership of both descriptors and closes them. Runs on the engine's pump thread for one child."""
        size = self.graph.seg_size * self.graph.seg_size * 3
        try:
            fcntl.fcntl(mask_fd, fcntl.F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            log.debug("blur: could not enlarge the mask pipe")  # the default 64 KB pipe still works
        self._smoother.reset()
        seq, last = 0, self._clock()
        try:
            # ffmpeg starts no graph until every input has delivered a frame, and the first camera frame cannot
            # reach us before that: so the mask stream opens with one "no blur" frame, which belongs to camera
            # frame 0 (the pairing is by frame number).
            # A second one leads the picture by a frame: the child's frame sync will not release picture N until it
            # has seen mask N+1 (it must know no better mask is coming), so each answer below is written for the
            # NEXT frame. Without the lead every picture waits a whole frame for a mask that is only made after it.
            for _ in range(1 + MASK_LEAD):
                if not _write_all(mask_fd, self._clear):
                    return
            while not self._halt.is_set():
                rgb = _read_exact(seg_fd, size)
                if rgb is None:
                    return
                now = self._clock()
                seq += 1
                with self._cond:
                    self._frame = (seq, now, rgb)
                    self._pumped += 1
                    self._cond.notify_all()
                self._await_mask(seq)
                payload = self._mask_for(now, now - last)
                last = now
                if not _write_all(mask_fd, payload):
                    return
        finally:
            for fd in (seg_fd, mask_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._smoother.reset()

    def _await_mask(self, seq: int) -> None:
        """Give the worker a moment to finish this frame, when it is going to work on it."""
        with self._cond:
            if not self._on or self._state != "running" or (self._rate > 1 and seq % self._rate != 0):
                return
            deadline = self._clock() + MASK_WAIT
            while not self._halt.is_set() and (self._raw is None or self._raw[2] < seq):
                left = deadline - self._clock()
                if left <= 0:
                    return
                self._cond.wait(left)

    def _mask_for(self, now: float, dt: float) -> bytes:
        with self._cond:
            on, raw = self._on, self._raw
        if not on:
            self._smoother.reset()
            return self._clear
        if raw is None or now - raw[1] > STALE_SECONDS + self._max_gap():
            # nothing fresh to apply: show the picture unblurred rather than a mask from the past
            with self._cond:
                if self._state == "running" and raw is not None:
                    self._state = "stalled"
            self._smoother.reset()
            return self._clear
        mask = self._smoother.step(raw[0], dt)
        age = (now - raw[1]) * 1000
        with self._cond:
            self._age_ms.append(age)
        return segment.encode_mask(self._resample(mask, self._window()))

    def _max_gap(self) -> float:
        """A slow smoothness setting legitimately spaces masks out; allow for it before calling the mask stale."""
        return 0.0 if self._rate <= 1 else 0.1 * self._rate

    # -- what the Console shows ----------------------------------------------------------------------------------

    def stats(self) -> dict:
        now = self._clock()
        with self._lock:
            while self._done and now - self._done[0] > RATE_WINDOW:
                self._done.popleft()
            infer, age = list(self._infer_ms), list(self._age_ms)
            fps = len(self._done) / RATE_WINDOW if self._state == "running" else 0.0
            state, error, label, on, rate = self._state, self._error, self._label, self._on, self._rate
            pumped = self._pumped
        return {
            "state": state,
            "error": error,
            "device": label,
            "on": on,
            "rate": rate,
            "mask_fps": round(fps, 1),
            "infer_p50_ms": _pct(infer, 0.5),
            "infer_p95_ms": _pct(infer, 0.95),
            "mask_age_ms": round(statistics.median(age), 1) if age else None,
            "frames": pumped,
        }


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 2)


def _read_exact(fd: int, size: int) -> bytes | None:
    """`size` bytes from a pipe, or None at end of stream."""
    chunks: list[bytes] = []
    left = size
    while left:
        try:
            chunk = os.read(fd, left)
        except InterruptedError:
            continue
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> bool:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except OSError:
            return False  # the child closed its end
        view = view[written:]
    return True
