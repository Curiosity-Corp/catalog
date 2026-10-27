"""The relay's cut-over, merge and fan-out logic, driven by synthetic blocks and a hand-held clock."""

from __future__ import annotations

import itertools

from stream_support import RecordingSink, ablock, audio_track, vblock, video_track

from streamstack.stream import mkv, relay


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_chain(clock: Clock) -> tuple[relay.Chain, RecordingSink, list[int]]:
    chain = relay.Chain("av1", clock)
    retired: list[int] = []
    chain.on_retire = retired.append
    chain.set_audio(audio_track())
    chain.generation_header(1, video_track())
    chain.switch.activate(1)
    sink = RecordingSink()
    chain.attach(sink)
    return chain, sink, retired


def run_a(chain: relay.Chain, clock: Clock, start: int, stop: int, key_every: int = 30, lag: int = 40) -> None:
    """Generation 1 delivers frames start..stop (ms, 33 ms apart), each `lag` ms after it was captured."""
    for n, ts in enumerate(range(start, stop, 33)):
        clock.now = ts + lag
        chain.video_block(1, vblock(ts, key=n % key_every == 0))
        chain.audio_block(ablock(ts))


def video_ts(sink: RecordingSink) -> list[int]:
    return [b.ts_ms for b in sink.blocks() if b.track == mkv.TRACK_VIDEO]


def test_stream_starts_on_a_keyframe_with_a_header() -> None:
    clock = Clock()
    chain, sink, _ = make_chain(clock)
    chain.video_block(1, vblock(0))  # not a keyframe: dropped, nothing is joinable yet
    clock.now = 33
    chain.video_block(1, vblock(33, key=True))
    clock.now = 200
    chain.tick()
    assert len(sink.headers) == 1
    got = sink.blocks()
    assert got[0].keyframe and got[0].track == mkv.TRACK_VIDEO and got[0].ts_ms == 33
    assert [b.ts_ms for b in got if b.track == mkv.TRACK_VIDEO] == [33]


def test_audio_and_video_are_interleaved_by_time() -> None:
    clock = Clock()
    chain, sink, _ = make_chain(clock)
    run_a(chain, clock, 0, 400)
    stamps = [b.ts_ms for b in sink.blocks()]
    assert stamps == sorted(stamps)
    assert {b.track for b in sink.blocks()} == {1, 2}


def test_a_late_sink_waits_for_the_next_keyframe_and_drops_earlier_audio() -> None:
    clock = Clock()
    chain, _sink, _ = make_chain(clock)
    chain.detach("rec")
    run_a(chain, clock, 0, 500, key_every=6)
    late = RecordingSink("late")
    chain.attach(late)
    run_a(chain, clock, 500, 1200, key_every=6)
    first = late.blocks()[0]
    assert first.keyframe and first.track == mkv.TRACK_VIDEO
    assert min(b.ts_ms for b in late.blocks()) == first.ts_ms
    assert len(late.headers) == 1


def test_seamless_swap_has_no_gap_no_overlap_and_a_keyframe_at_the_cut() -> None:
    clock = Clock()
    chain, sink, retired = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 1000, key_every=1000)  # one keyframe at t=0 only
    chain.begin_swap(2, cut_ms=1000)
    # the old encoder keeps going past the cut (held); the new one has its keyframe on the cut, 2 ms off the old clock
    for n, ts in enumerate(range(1023, 1400, 33)):
        clock.now = ts + 40
        chain.video_block(1, vblock(ts))
        chain.audio_block(ablock(ts))
        clock.now = ts + 60
        chain.video_block(2, vblock(ts + 2, key=n == 0))
    chain.generation_ended(1)
    clock.now = 3000
    chain.tick()
    times = video_ts(sink)
    assert times == sorted(set(times)), "time must only move forward"
    cut = next(b for b in sink.blocks() if b.track == mkv.TRACK_VIDEO and b.ts_ms >= 1000)
    assert cut.keyframe and cut.ts_ms == 1025
    steps = [b - a for a, b in itertools.pairwise(times)]
    assert max(steps) <= 36 and min(steps) >= 30, f"steps {sorted(set(steps))} around the cut"
    assert retired == [1]
    assert chain.switch.last_swap is not None and chain.switch.last_swap.ok
    assert len(sink.headers) == 1, "sinks are never restarted by a same-geometry swap"


def test_the_new_encoder_ahead_of_the_old_one_waits_for_it_to_catch_up() -> None:
    clock = Clock()
    chain, sink, _ = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 700, key_every=1000)  # the old encoder has delivered up to 693
    chain.begin_swap(2, cut_ms=700)
    clock.now = 860
    chain.video_block(2, vblock(802, key=True))  # the keyframe is here, the old encoder is still at 693
    assert chain.switch.pending == 2
    for ts in (726, 759, 792):  # three more frames from the old one; the last is the picture the keyframe replaces
        clock.now = ts + 90
        chain.video_block(1, vblock(ts))
    assert chain.switch.pending is None and chain.switch.active == 2
    clock.now = 3000
    chain.tick()
    assert [t for t in video_ts(sink) if t >= 700] == [726, 759, 802]


def test_a_failed_swap_gives_the_held_frames_back() -> None:
    clock = Clock()
    chain, sink, retired = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 700, key_every=1000)
    chain.begin_swap(2, cut_ms=700)
    run_a(chain, clock, 700, 1000, key_every=1000)
    before = len(video_ts(sink))
    clock.now = 10_000
    result = chain.tick()
    assert result is not None and not result.ok and "keyframe" in result.reason
    assert len(video_ts(sink)) > before, "the held frames must be released"
    assert chain.switch.active == 1 and chain.switch.pending is None
    assert retired == [2]


def test_the_replacement_dying_aborts_the_swap_and_the_stream_goes_on() -> None:
    clock = Clock()
    chain, sink, _retired = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 700, key_every=1000)
    chain.begin_swap(2, cut_ms=700)
    run_a(chain, clock, 700, 900, key_every=1000)
    chain.generation_ended(2)
    assert chain.switch.active == 1 and chain.switch.pending is None
    assert video_ts(sink)[-1] >= 860
    assert chain.switch.last_swap is not None and "exited" in chain.switch.last_swap.reason


def test_the_old_encoder_dying_after_the_new_keyframe_still_cuts_over() -> None:
    clock = Clock()
    chain, sink, _retired = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 700, key_every=1000)
    chain.begin_swap(2, cut_ms=700)
    clock.now = 800
    chain.video_block(2, vblock(702, key=True))
    chain.generation_ended(1)
    clock.now = 830
    chain.video_block(2, vblock(735))
    assert chain.switch.active == 2
    clock.now = 3000
    chain.tick()
    assert video_ts(sink)[-2:] == [702, 735]


def test_a_stalled_old_encoder_is_cut_without_its_last_frames() -> None:
    clock = Clock()
    chain, _sink, _ = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 500, key_every=1000)  # then the old encoder goes quiet
    chain.begin_swap(2, cut_ms=700)
    clock.now = 800
    chain.video_block(2, vblock(702, key=True))
    assert chain.switch.pending == 2
    clock.now = 1200
    result = chain.tick()
    assert result is not None and result.ok
    assert chain.switch.active == 2


def test_blocks_from_a_retired_generation_are_ignored() -> None:
    clock = Clock()
    chain, sink, _ = make_chain(clock)
    chain.generation_header(2, video_track())
    run_a(chain, clock, 0, 300, key_every=1000)
    chain.begin_swap(2, cut_ms=300)
    clock.now = 400
    chain.video_block(2, vblock(300, key=True))
    chain.video_block(1, vblock(333))  # old, now ignored
    chain.video_block(2, vblock(333))
    clock.now = 600
    chain.tick()
    assert video_ts(sink).count(333) == 1


def test_a_geometry_change_restarts_only_the_sinks_that_cannot_take_it() -> None:
    clock = Clock()
    chain, sink, _ = make_chain(clock)
    tolerant = RecordingSink("srt")
    chain.attach(tolerant, restart_on_geometry=False)
    chain.generation_header(2, video_track(1920, 1080))
    run_a(chain, clock, 0, 500, key_every=1000)
    seen: list[mkv.Track] = []
    chain.on_geometry = seen.append
    chain.begin_swap(2, cut_ms=500)
    clock.now = 600
    chain.video_block(2, vblock(502, key=True))
    chain.generation_ended(1)
    assert sink.closed == 1 and tolerant.closed == 0
    assert seen and seen[0].height == 1080
    clock.now = 700
    chain.video_block(2, vblock(535))
    chain.video_block(2, vblock(568, key=True))
    clock.now = 900
    chain.tick()
    assert len(sink.headers) == 2 and len(tolerant.headers) == 1


def test_merger_never_holds_video_for_a_silent_audio_source() -> None:
    merger = relay.Merger(max_wait=100)
    merger.push(vblock(0, True), now=0)
    assert merger.poll(50) == []
    out = merger.poll(150)
    assert [b.ts_ms for b in out] == [0]


def test_merger_orders_by_timestamp() -> None:
    merger = relay.Merger()
    for block in (vblock(40, True), ablock(0), ablock(20), vblock(80), ablock(60), ablock(100)):
        merger.push(block, now=0)
    assert [b.ts_ms for b in merger.poll(0)] == [0, 20, 40, 60, 80]


def test_rate_window_reports_kbps_over_the_recent_span() -> None:
    window = relay.Window(span_ms=3000)
    for ms in range(0, 3000, 100):
        window.add(ms, 12_500)  # 100 kB per second = 1000 kbps
    assert 900 < window.kbps(3000) < 1100
    assert window.kbps(10_000) == 0.0


def test_snapshot_reports_rates_latency_and_swaps() -> None:
    clock = Clock()
    chain, _, _ = make_chain(clock)
    run_a(chain, clock, 0, 2000, key_every=30)
    snap = chain.snapshot()
    assert snap["frames"] > 50 and snap["keyframes"] >= 2
    assert 30 < snap["latency_ms"] < 60
    assert snap["video_kbps"] > 0 and snap["swaps"] == 0 and snap["swapping"] is False
