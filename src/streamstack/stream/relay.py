"""The relay: one video encoder at a time per codec, cut over at a keyframe, merged with audio, fanned out to sinks.

Nothing in this module starts a process or touches a clock of its own: it is fed `Block`s (see `mkv.py`) and a
millisecond clock, and it hands finished Matroska bytes to sink objects. That keeps the cut-over logic, which is
the part that must not glitch a live stream, deterministic and testable with synthetic blocks.

Data flow for one codec ("chain"):

    video encoder generation A --\
                                  TrackSwitch -> Merger -> Fanout -> sinks (SRT, RTMP, WHIP, recorder, ...)
    video encoder generation B --/                 ^
    audio encoder ---------------------------------+

Generations share one clock: every encoder is given frames whose timestamps are wall-clock milliseconds since the
stream's epoch, so a block from generation B means the same moment as a block from A and nothing is rebased.
"""

from __future__ import annotations

import heapq
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from streamstack.stream import mkv
from streamstack.stream.mkv import Block, Track

Clock = Callable[[], float]  # milliseconds on the stream's axis (0 at the epoch)

SWITCH_TIMEOUT_MS = 4000  # a replacement encoder that has not produced a usable keyframe by then is abandoned
HOLD_LIMIT_MS = 1500  # how much of the old encoder's output may wait for the new one's keyframe
MERGE_WAIT_MS = 150  # a block never waits longer than this for the other stream to catch up
MAX_CLUSTER_MS = 1000
LATENCY_ALPHA = 0.1


class GenerationState:
    """What a swap did, for state and tests."""

    IDLE = "idle"
    PENDING = "pending"


@dataclass
class SwapResult:
    ok: bool
    reason: str
    cut_ms: int | None = None
    held_ms: int = 0
    gap_ms: int = 0  # video the cut skipped (the old encoder's frames at or after the new keyframe)


class TrackSwitch:
    """Forwards one track's blocks (video, or audio with `frame_ms` 20) from the active generation.

    A pending generation takes over at its first keyframe at or after the planned cut.

    A swap is planned for `cut_ms`: the old generation keeps being forwarded up to that moment while the new one
    warms up. The new generation is expected to have been told to put a keyframe at `cut_ms` (ffmpeg
    `-force_key_frames expr:gte(t,cut)`); its first keyframe at or after `cut_ms` is where the stream continues.
    The old generation's blocks at or after `cut_ms` wait in a bounded hold, so a swap that cannot complete
    (the new encoder died, never produced a keyframe) puts them back and loses nothing but latency.
    """

    def __init__(
        self,
        emit: Callable[[Block], None],
        retire: Callable[[int], None] | None = None,
        frame_ms: float = 33.4,
    ) -> None:
        self._emit = emit
        self._retire = retire or (lambda gen: None)
        self.frame_ms = frame_ms
        self.active: int | None = None
        self.pending: int | None = None
        self.cut_ms: int | None = None
        self.last_ts: int | None = None
        self._started = False  # the first block out must be a keyframe
        self._held: list[Block] = []
        self._staged: list[Block] = []  # the pending generation's blocks from its sync keyframe on
        self._swap_started = 0.0
        self._synced_at: float | None = None
        self._active_head: int | None = None
        self.last_swap: SwapResult | None = None
        self.swaps = 0

    # -- control ----------------------------------------------------------------------------------------

    def activate(self, gen: int) -> None:
        """The first generation: it becomes the source as soon as it delivers a keyframe."""
        self.active, self._started = gen, False
        self._active_head = None

    def begin_swap(self, gen: int, cut_ms: int, now: float) -> None:
        if self.active is None:
            self.activate(gen)
            return
        if self.pending is not None:
            raise RuntimeError("a swap is already in progress")
        self.pending, self.cut_ms = gen, cut_ms
        self._held, self._staged, self._synced_at = [], [], None
        self._swap_started = now

    def abort_swap(self, reason: str) -> SwapResult | None:
        """Give up on the pending generation; whatever the old one had held goes out now."""
        if self.pending is None:
            return None
        gen, held = self.pending, self._held
        held_ms = held[-1].ts_ms - held[0].ts_ms if held else 0
        self.pending = self.cut_ms = self._synced_at = None
        self._held, self._staged = [], []
        for block in held:
            self._forward(block)
        self._retire(gen)
        self.last_swap = SwapResult(False, reason, held_ms=held_ms)
        return self.last_swap

    # -- input ------------------------------------------------------------------------------------------

    def on_block(self, gen: int, block: Block, now: float = 0.0) -> None:
        if gen == self.active:
            self._active_head = block.ts_ms
            if self.pending is not None and self.cut_ms is not None and block.ts_ms >= self.cut_ms - self.frame_ms / 2:
                if self._staged and block.ts_ms >= self._staged[0].ts_ms - self.frame_ms / 2:
                    self._maybe_finish()  # the new generation is already synced and this block is past its keyframe
                    return
                self._held.append(block)
                if self._held[-1].ts_ms - self._held[0].ts_ms > HOLD_LIMIT_MS:
                    self.abort_swap("the replacement encoder was too late (the old one's output was held too long)")
                return
            self._forward(block)
            if self.pending is not None:
                self._maybe_finish()
        elif gen == self.pending:
            self._on_pending(block, now)
        # blocks from a retired generation are dropped

    def _on_pending(self, block: Block, now: float) -> None:
        if self._staged:
            self._staged.append(block)
            self._maybe_finish()
            return
        cut = self.cut_ms if self.cut_ms is not None else 0
        if block.keyframe and block.ts_ms >= cut and (self.last_ts is None or block.ts_ms > self.last_ts):
            self._staged = [block]
            self._synced_at = now
            self._maybe_finish()

    def _maybe_finish(self) -> None:
        """Complete the cut once the old generation has delivered everything before the new keyframe."""
        if self.pending is None or not self._staged:
            return
        keyframe = self._staged[0]
        head = self._active_head
        if self.active is not None and head is not None and head < keyframe.ts_ms - self.frame_ms:
            return  # the old encoder has not caught up with the keyframe yet; its frames before it must go out first
        self._finish(keyframe)

    def _finish(self, keyframe: Block) -> None:
        old, new = self.active, self.pending
        held = self._held
        # the old encoder's frame within half a frame of the keyframe is the same picture: the keyframe replaces it
        before = [b for b in held if b.ts_ms < keyframe.ts_ms - self.frame_ms / 2]
        gap = keyframe.ts_ms - (before[-1].ts_ms if before else (self.last_ts or keyframe.ts_ms))
        for block in before:
            self._forward(block)
        staged, cut = self._staged, self.cut_ms
        self.active, self.pending, self.cut_ms = new, None, None
        self._held, self._staged, self._synced_at = [], [], None
        self._active_head = staged[-1].ts_ms
        self._started = True
        for block in staged:
            self._forward(block)
        self.swaps += 1
        self.last_swap = SwapResult(
            True,
            "cut at a keyframe",
            cut,
            held[-1].ts_ms - held[0].ts_ms if held else 0,
            max(0, round(gap - self.frame_ms)),
        )
        if old is not None:
            self._retire(old)

    def tick(self, now: float) -> SwapResult | None:
        """Called regularly: abandons a swap that has run out of time, finishes one whose old encoder stalled."""
        if self.pending is None:
            return None
        if self._staged and self._synced_at is not None and now - self._synced_at > 250:
            self._finish(self._staged[0])  # the old encoder went quiet: cut without its last frames
            return self.last_swap
        if now - self._swap_started > SWITCH_TIMEOUT_MS:
            return self.abort_swap("the replacement encoder did not produce a keyframe in time")
        return None

    def generation_ended(self, gen: int) -> None:
        """A generation's process exited. If it was the active one and a keyframe is staged, cut over now."""
        if gen == self.pending:
            self.abort_swap("the replacement encoder exited")
        elif gen == self.active and self.pending is not None and self._staged:
            self._finish(self._staged[0])
        elif gen == self.active and self.pending is None:
            self.active = None

    # -- output -----------------------------------------------------------------------------------------

    def _forward(self, block: Block) -> None:
        if not self._started:
            if not block.keyframe:
                return
            self._started = True
        if self.last_ts is not None and block.ts_ms < self.last_ts:
            return  # a late straggler must never run time backwards
        self.last_ts = block.ts_ms
        self._emit(block)


class Merger:
    """Interleaves video and audio blocks by timestamp. A block waits at most `max_wait` for the other stream."""

    def __init__(self, max_wait: float = MERGE_WAIT_MS) -> None:
        self.max_wait = max_wait
        self._heap: list[tuple[int, int, float, Block]] = []
        self._seq = 0
        self._watermark = {mkv.TRACK_VIDEO: -1, mkv.TRACK_AUDIO: -1}

    def push(self, block: Block, now: float) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (block.ts_ms, self._seq, now, block))
        if block.track in self._watermark:
            self._watermark[block.track] = max(self._watermark[block.track], block.ts_ms)

    def poll(self, now: float) -> list[Block]:
        out: list[Block] = []
        floor = min(self._watermark.values())
        while self._heap:
            ts, _, arrived, block = self._heap[0]
            if ts <= floor or now - arrived >= self.max_wait:
                heapq.heappop(self._heap)
                out.append(block)
            else:
                break
        return out

    def ignore(self, track: int) -> None:
        """Stop waiting for a track that will never arrive (a stream without audio)."""
        self._watermark[track] = 2**62

    def drain(self) -> list[Block]:
        out = [entry[3] for entry in sorted(self._heap)]
        self._heap.clear()
        return out

    def pending(self) -> int:
        return len(self._heap)


class Sink(Protocol):
    """What the relay writes Matroska bytes into. `write` must not block (queue and drop instead)."""

    name: str

    def open(self, header: bytes) -> None:
        """Begin a stream with `header`. May raise OSError when the sink is not ready (it is retried)."""
        ...

    def write(self, data: bytes, video_blocks: int, keyframe: bool) -> bool:
        """Queue `data`; False means the sink is gone and must be opened again at the next keyframe."""
        ...

    def close(self) -> None: ...

    # A sink may also define `on_track(track)`. The relay calls it when the video encoder was replaced by one with a
    # different track entry (so different codec parameters) and the stream carries on in the same sink.


@dataclass
class Attached:
    sink: Sink
    restart_on_geometry: bool
    started: bool = False
    bytes_out: int = 0
    clusters: int = 0


@dataclass
class Window:
    """A rolling byte counter over the last `span_ms` of the stream's own clock."""

    span_ms: int = 3000
    _items: deque[tuple[float, int]] = field(default_factory=deque)
    _sum: int = 0

    def add(self, at: float, size: int) -> None:
        self._items.append((at, size))
        self._sum += size
        self._trim(at)

    def _trim(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self.span_ms:
            self._sum -= self._items.popleft()[1]

    def kbps(self, now: float) -> float:
        self._trim(now)
        if not self._items:
            return 0.0
        span = max(now - self._items[0][0], 500.0)
        return self._sum * 8 / span


class Chain:
    """One codec's path from encoder to sinks. All methods are safe to call from any thread."""

    def __init__(self, name: str, clock: Clock, frame_ms: float = 33.4) -> None:
        self.name = name
        self.clock = clock
        self._lock = threading.RLock()
        self.switch = TrackSwitch(self._on_video, self._on_retire, frame_ms)
        self.merger = Merger()
        self._sinks: dict[str, Attached] = {}
        self.audio: Track | None = None
        self.video: Track | None = None
        self._batch: list[Block] = []
        self.on_retire: Callable[[int], None] = lambda gen: None
        self.on_geometry: Callable[[Track], None] = lambda track: None
        self.video_rate = Window()
        self.audio_rate = Window()
        self.frames = 0
        self.keyframes = 0
        self.latency_ms = 0.0
        self.last_video_at: float | None = None
        self.headers: dict[int, Track] = {}

    # -- feeding ----------------------------------------------------------------------------------------

    def set_audio(self, track: Track | None) -> None:
        with self._lock:
            self.audio = track
            if track is None:
                self.merger.ignore(mkv.TRACK_AUDIO)

    def generation_header(self, gen: int, track: Track) -> None:
        with self._lock:
            self.headers[gen] = track
            if self.video is None:
                self.video = track

    def video_block(self, gen: int, block: Block) -> None:
        with self._lock:
            now = self.clock()
            self.switch.on_block(gen, block, now)
            self._pump(now)

    def audio_block(self, block: Block) -> None:
        with self._lock:
            now = self.clock()
            self.audio_rate.add(now, block.size)
            self.merger.push(block, now)
            self._pump(now)

    def tick(self) -> SwapResult | None:
        with self._lock:
            now = self.clock()
            result = self.switch.tick(now)
            self._pump(now)
            return result

    def generation_ended(self, gen: int) -> None:
        with self._lock:
            self.switch.generation_ended(gen)
            self._pump(self.clock())

    def _on_retire(self, gen: int) -> None:
        if self.switch.active is not None and self.switch.active != gen:
            self.adopt_geometry(self.switch.active)
        self.on_retire(gen)

    def _on_video(self, block: Block) -> None:
        now = self.clock()
        self.frames += 1
        self.keyframes += block.keyframe
        self.video_rate.add(now, block.size)
        self.last_video_at = now
        sample = max(0.0, now - block.ts_ms)
        self.latency_ms = sample if self.frames == 1 else self.latency_ms + LATENCY_ALPHA * (sample - self.latency_ms)
        self.merger.push(block, now)

    # -- swaps --------------------------------------------------------------------------------------------

    def begin_swap(self, gen: int, cut_ms: int) -> None:
        with self._lock:
            self.switch.begin_swap(gen, cut_ms, self.clock())

    def adopt_geometry(self, gen: int) -> None:
        """After a completed swap: tell sinks that cannot take an in-band change that the picture changed."""
        with self._lock:
            track = self.headers.get(gen)
            if track is None:
                return
            changed = self.video is not None and (track.width, track.height, track.codec) != (
                self.video.width,
                self.video.height,
                self.video.codec,
            )
            retuned = self.video is not None and track.raw != self.video.raw
            self.video = track
            if retuned:
                for attached in self._sinks.values():
                    on_track = getattr(attached.sink, "on_track", None)
                    if on_track is not None and attached.started:
                        on_track(track)  # a sink that sends parameter sets itself (WHIP) takes the new ones in-band
            if changed:
                for attached in self._sinks.values():
                    if attached.restart_on_geometry:
                        attached.started = False
                        attached.sink.close()
            if changed:
                self.on_geometry(track)

    # -- sinks --------------------------------------------------------------------------------------------

    def attach(self, sink: Sink, restart_on_geometry: bool = True) -> None:
        with self._lock:
            self._sinks[sink.name] = Attached(sink, restart_on_geometry)

    def detach(self, name: str) -> None:
        with self._lock:
            attached = self._sinks.pop(name, None)
        if attached is not None:
            attached.sink.close()

    def sink_names(self) -> list[str]:
        with self._lock:
            return list(self._sinks)

    def sink_stats(self, name: str) -> Attached | None:
        with self._lock:
            return self._sinks.get(name)

    def close(self) -> None:
        with self._lock:
            sinks = list(self._sinks.values())
            self._sinks.clear()
        for attached in sinks:
            attached.sink.close()

    # -- output ---------------------------------------------------------------------------------------------

    def _pump(self, now: float) -> None:
        blocks = self.merger.poll(now)
        if not blocks:
            return
        batch: list[Block] = []
        for block in blocks:
            if block.track == mkv.TRACK_VIDEO and block.keyframe and batch:
                self._deliver(batch)
                batch = []
            batch.append(block)
        self._deliver(batch)

    def _deliver(self, batch: list[Block]) -> None:
        if not batch or self.video is None:
            return
        keyframe = next((b for b in batch if b.track == mkv.TRACK_VIDEO), None)
        starts_stream = keyframe is not None and keyframe.keyframe
        for attached in list(self._sinks.values()):
            blocks = batch
            if not attached.started:
                if not starts_stream or keyframe is None:
                    continue
                blocks = [b for b in batch if b.ts_ms >= keyframe.ts_ms]
                header = mkv.stream_header(self.video, self.audio)
                try:
                    attached.sink.open(header)
                except OSError:
                    continue
                attached.started = True
                attached.bytes_out += len(header)
            data = self._cluster(blocks)
            videos = sum(1 for b in blocks if b.track == mkv.TRACK_VIDEO)
            first = blocks[0]
            if attached.sink.write(data, videos, first.track == mkv.TRACK_VIDEO and first.keyframe):
                attached.bytes_out += len(data)
                attached.clusters += 1
            else:
                attached.started = False  # reopened (header first) at the next keyframe

    @staticmethod
    def _cluster(blocks: list[Block]) -> bytes:
        out = b""
        run: list[Block] = []
        for block in blocks:
            if run and block.ts_ms - run[0].ts_ms > MAX_CLUSTER_MS:
                out += mkv.encode_cluster(run, {mkv.TRACK_VIDEO: 1, mkv.TRACK_AUDIO: 2})
                run = []
            run.append(block)
        return out + mkv.encode_cluster(run, {mkv.TRACK_VIDEO: 1, mkv.TRACK_AUDIO: 2})

    # -- state ----------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            now = self.clock()
            swap = self.switch.last_swap
            return {
                "video_kbps": round(self.video_rate.kbps(now), 1),
                "audio_kbps": round(self.audio_rate.kbps(now), 1),
                "frames": self.frames,
                "keyframes": self.keyframes,
                "latency_ms": round(self.latency_ms, 1),
                "swaps": self.switch.swaps,
                "swapping": self.switch.pending is not None,
                "last_swap": None
                if swap is None
                else {"ok": swap.ok, "reason": swap.reason, "gap_ms": swap.gap_ms, "held_ms": swap.held_ms},
            }
