"""BlurFeed: the pump and the inference worker, on real pipes with a fake segmenter (no GPU, no ffmpeg)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

import pytest

np = pytest.importorskip("numpy")

from streamstack.engine import blurfeed  # noqa: E402
from streamstack.engine.blurfeed import BlurFeed  # noqa: E402
from streamstack.engine.graph import BlurGraph  # noqa: E402
from streamstack.engine.segment import MaskSmoother, SegmentError  # noqa: E402

GRAPH = BlurGraph(seg_size=8, mask_width=16, mask_height=8)
SEG_BYTES = 8 * 8 * 3
MASK_BYTES = 16 * 8 * 3
CLEAR = bytes(MASK_BYTES)


class FakeSegmenter:
    """Says "nobody is there" (person 0), so the background weight is 255 everywhere."""

    label = "Fake Arc"

    def __init__(self, person: float = 0.0, delay: float = 0.0) -> None:
        self.person, self.delay = person, delay
        self.calls = 0
        self.closed = False

    def infer(self, rgb: bytes) -> np.ndarray:
        assert len(rgb) == SEG_BYTES
        self.calls += 1
        time.sleep(self.delay)
        return np.full((8, 8), self.person, dtype=np.float32)

    def close(self) -> None:
        self.closed = True


def make_feed(open_segmenter: Callable[[], FakeSegmenter] | None = None, **kw) -> BlurFeed:
    feed = BlurFeed(GRAPH, open_segmenter or FakeSegmenter, smoother=MaskSmoother(fade_seconds=0, floor=1.0), **kw)
    return feed


class Pumped:
    """A pump on a thread with both pipes, a reader of the masks and a writer of camera frames."""

    def __init__(self, feed: BlurFeed) -> None:
        self.feed = feed
        seg_r, self.seg_w = os.pipe()
        self.mask_r, mask_w = os.pipe()
        self.masks: list[bytes] = []
        self.thread = threading.Thread(target=feed.pump, args=(seg_r, mask_w), daemon=True)
        self.thread.start()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while True:
            data = b""
            while len(data) < MASK_BYTES:
                try:
                    chunk = os.read(self.mask_r, MASK_BYTES - len(data))
                except OSError:
                    return
                if not chunk:
                    return
                data += chunk
            self.masks.append(data)

    def send(self, frames: int, gap: float = 0.002) -> None:
        for i in range(frames):
            os.write(self.seg_w, bytes([i % 256]) * SEG_BYTES)
            time.sleep(gap)

    def wait_masks(self, count: int, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.masks) >= count:
                return True
            time.sleep(0.005)
        return False

    def close(self) -> None:
        os.close(self.seg_w)  # the child closing its end ends the pump
        self.thread.join(timeout=3)
        self.reader.join(timeout=3)
        os.close(self.mask_r)


@pytest.fixture
def pumped():
    made: list[Pumped] = []

    def start(feed: BlurFeed) -> Pumped:
        p = Pumped(feed)
        made.append(p)
        return p

    yield start
    for p in made:
        p.feed.stop()
        if p.thread.is_alive():
            p.close()


def wait_state(feed: BlurFeed, state: str, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if feed.stats()["state"] == state:
            return True
        time.sleep(0.005)
    return False


def test_with_blur_off_every_frame_gets_a_clear_mask_and_no_gpu_is_touched(pumped) -> None:
    opened: list[int] = []
    feed = make_feed(lambda: opened.append(1) or FakeSegmenter())
    p = pumped(feed)
    p.send(5)
    expected = 1 + blurfeed.MASK_LEAD + 5  # the priming masks, then one per camera frame
    assert p.wait_masks(expected)
    assert all(mask == CLEAR for mask in p.masks)
    assert opened == [] and feed.stats()["state"] == "idle"  # OpenVINO is never loaded for a user who never blurs


def test_the_mask_stream_leads_the_picture_by_the_priming_masks(pumped) -> None:
    p = pumped(make_feed())
    assert p.wait_masks(1 + blurfeed.MASK_LEAD)  # before any camera frame: ffmpeg can start its graph
    assert len(p.masks) == 1 + blurfeed.MASK_LEAD


def test_with_blur_on_the_background_weight_comes_back_in_every_plane(pumped) -> None:
    feed = make_feed()
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "running")
    p.send(12, gap=0.01)
    assert p.wait_masks(1 + blurfeed.MASK_LEAD + 12)
    last = p.masks[-1]
    assert last == bytes([255]) * MASK_BYTES  # nobody in the picture: all background, in Y, U and V
    stats = feed.stats()
    assert stats["state"] == "running" and stats["device"] == "Fake Arc" and stats["mask_fps"] > 0
    assert stats["infer_p50_ms"] is not None and stats["frames"] == 12


def test_a_person_in_the_picture_stays_sharp(pumped) -> None:
    feed = make_feed(lambda: FakeSegmenter(person=1.0))
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "running")
    p.send(8, gap=0.01)
    assert p.wait_masks(1 + blurfeed.MASK_LEAD + 8)
    assert p.masks[-1] == CLEAR  # person weight 1 -> background weight 0 everywhere


def test_switching_blur_off_clears_the_mask_again(pumped) -> None:
    feed = make_feed()
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "running")
    p.send(6, gap=0.01)
    feed.set_enabled(False)
    p.send(3, gap=0.01)
    assert p.wait_masks(1 + blurfeed.MASK_LEAD + 9)
    assert p.masks[-1] == CLEAR and feed.stats()["state"] == "idle"


def test_a_segmenter_that_cannot_start_fails_the_state_not_the_engine(pumped) -> None:
    def broken() -> FakeSegmenter:
        raise SegmentError("OpenVINO sees no GPU (CPU)")

    feed = make_feed(broken)
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "failed")
    stats = feed.stats()
    assert stats["error"] == "OpenVINO sees no GPU (CPU)" and stats["on"] is False
    p.send(4)
    assert p.wait_masks(1 + blurfeed.MASK_LEAD + 4) and all(m == CLEAR for m in p.masks)  # the picture keeps moving


def test_a_surprise_exception_in_the_factory_is_contained(pumped) -> None:
    def surprise() -> FakeSegmenter:
        raise ValueError("onnx parser exploded")

    feed = make_feed(surprise)
    feed.set_enabled(True)
    assert wait_state(feed, "failed")
    assert "onnx parser exploded" in feed.stats()["error"]


def test_switching_on_again_after_a_failure_retries(pumped) -> None:
    attempts: list[int] = []

    def flaky() -> FakeSegmenter:
        attempts.append(1)
        if len(attempts) == 1:
            raise SegmentError("the model could not be downloaded")
        return FakeSegmenter()

    feed = make_feed(flaky)
    feed.set_enabled(True)
    assert wait_state(feed, "failed")
    feed.set_enabled(True)
    assert wait_state(feed, "running") and feed.stats()["error"] is None and len(attempts) == 2


def test_a_crash_during_inference_fails_the_state(pumped) -> None:
    class Crashing(FakeSegmenter):
        def infer(self, rgb: bytes) -> np.ndarray:
            raise SegmentError("GPU hang")

    feed = make_feed(Crashing)
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "running")
    p.send(3, gap=0.01)
    assert wait_state(feed, "failed") and feed.stats()["error"] == "GPU hang"


def test_a_slow_gpu_skips_frames_instead_of_queueing_them(pumped) -> None:
    seg = FakeSegmenter(delay=0.05)
    feed = make_feed(lambda: seg)
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "running")
    p.send(30, gap=0.005)
    assert p.wait_masks(1 + blurfeed.MASK_LEAD + 30)
    assert 0 < seg.calls < 30  # the answer for every frame came at once; the GPU only saw the newest ones


def test_the_smoothness_setting_computes_every_nth_mask(pumped) -> None:
    seg = FakeSegmenter()
    feed = make_feed(lambda: seg)
    feed.set_rate(3)
    feed.set_enabled(True)
    p = pumped(feed)
    assert wait_state(feed, "running")
    p.send(30, gap=0.012)
    assert p.wait_masks(1 + blurfeed.MASK_LEAD + 30)
    assert 5 <= seg.calls <= 14  # about a third of 30


def test_a_mask_that_is_too_old_is_not_applied_and_the_state_says_stalled() -> None:
    now = [100.0]
    feed = make_feed(clock=lambda: now[0])
    feed._on, feed._state = True, "running"
    feed._raw = (np.zeros((8, 8), dtype=np.float32), 100.0, 1)
    assert feed._mask_for(100.1, 0.033) == bytes([255]) * MASK_BYTES
    assert feed._mask_for(100.0 + blurfeed.STALE_SECONDS + 0.2, 0.033) == CLEAR
    assert feed.stats()["state"] == "stalled"


def test_a_slow_smoothness_allows_for_its_own_gap_before_calling_a_mask_stale() -> None:
    feed = make_feed()
    feed._on, feed._state = True, "running"
    feed.set_rate(6)
    feed._raw = (np.zeros((8, 8), dtype=np.float32), 100.0, 1)
    assert feed._mask_for(100.0 + blurfeed.STALE_SECONDS + 0.3, 0.033) != CLEAR
    assert feed.stats()["state"] == "running"


def test_the_mask_is_cut_to_the_framing_window() -> None:
    feed = make_feed(window=lambda: (0.5, 0.0, 1.0, 1.0))
    person = np.hstack((np.zeros((8, 4), np.float32), np.ones((8, 4), np.float32)))  # person on the right half
    feed._on, feed._state = True, "running"
    feed._raw = (person, 0.0, 1)
    out = feed._mask_for(0.0, 0.033)
    assert out[: MASK_BYTES // 3].count(0) > 0.8 * (MASK_BYTES // 3)  # zoomed onto the person: little background


def test_stop_ends_the_worker_and_closes_the_segmenter() -> None:
    seg = FakeSegmenter()
    feed = make_feed(lambda: seg)
    feed.set_enabled(True)
    assert wait_state(feed, "running")
    feed.stop()
    assert seg.closed


def test_stats_before_anything_happens() -> None:
    stats = make_feed().stats()
    assert stats["state"] == "idle" and stats["mask_fps"] == 0.0 and stats["infer_p95_ms"] is None
    assert stats["mask_age_ms"] is None and stats["frames"] == 0
