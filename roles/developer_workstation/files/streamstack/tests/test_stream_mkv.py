"""The Matroska reader and writer the relay splices with."""

from __future__ import annotations

import pytest
from stream_support import ablock, audio_track, source_stream, vblock, video_track

from streamstack.stream import mkv


def feed_all(data: bytes, step: int) -> list:
    demux = mkv.Demuxer()
    events = []
    for i in range(0, len(data), step):
        events += demux.feed(data[i : i + step])
    return events


@pytest.mark.parametrize("step", [1, 7, 4096])
def test_roundtrip_any_chunking(step: int) -> None:
    blocks = [vblock(0, True), ablock(5), vblock(33), ablock(25), vblock(66, True)]
    events = feed_all(source_stream(blocks), step)
    header, got = events[0], events[1:]
    assert isinstance(header, mkv.Header)
    assert [(t.number, t.kind, t.codec) for t in header.tracks] == [(1, "video", "V_AV1"), (2, "audio", "A_OPUS")]
    assert (header.tracks[0].width, header.tracks[0].height) == (1280, 720)
    assert [(b.track, b.ts_ms, b.keyframe, b.data) for b in got] == [
        (b.track, b.ts_ms, b.keyframe, b.data) for b in blocks
    ]


def test_header_arrives_only_with_the_first_cluster() -> None:
    data = source_stream([vblock(0, True)])
    cluster_at = data.index(mkv.enc_id(mkv.ID_CLUSTER))
    demux = mkv.Demuxer()
    assert demux.feed(data[:cluster_at]) == []
    assert isinstance(demux.feed(data[cluster_at:])[0], mkv.Header)


def test_track_entries_are_renumbered_but_otherwise_untouched() -> None:
    audio = audio_track()
    header = feed_all(mkv.stream_header(video_track(), audio) + mkv.encode_cluster([vblock(0, True)], {1: 1}), 100)[0]
    out_audio = header.track("audio")
    assert out_audio is not None and out_audio.number == 2
    assert b"private-A_OPUS" in out_audio.raw


def test_block_group_survives_with_its_extras() -> None:
    extra = mkv.uint_element(mkv.ID_BLOCK_DURATION, 20)
    block = mkv.Block(mkv.TRACK_AUDIO, 40, True, b"opus", extra, True)
    out = mkv.stream_header(video_track(), audio_track()) + mkv.encode_cluster([vblock(30, True), block], {1: 1, 2: 2})
    got = [e for e in feed_all(out, 13) if isinstance(e, mkv.Block)][1]
    assert got.grouped and got.extra == extra and got.keyframe and got.data == b"opus"


def test_unknown_size_cluster_is_streamed() -> None:
    body = mkv.uint_element(mkv.ID_TIMESTAMP, 1000) + mkv.element(mkv.ID_SIMPLEBLOCK, b"\x81\x00\x05\x80frame")
    unknown = mkv.enc_id(mkv.ID_CLUSTER) + b"\x01\xff\xff\xff\xff\xff\xff\xff" + body
    events = feed_all(mkv.stream_header(video_track(), None) + unknown, 3)
    assert [e.ts_ms for e in events if isinstance(e, mkv.Block)] == [1005]


def test_garbage_is_an_error_not_a_hang() -> None:
    with pytest.raises(mkv.MkvError):
        mkv.Demuxer().feed(b"\x00" * 16)


def test_cluster_before_tracks_is_rejected() -> None:
    with pytest.raises(mkv.MkvError, match="before the tracks"):
        mkv.Demuxer().feed(mkv.encode_cluster([vblock(0, True)], {1: 1}))


def test_nondefault_timescale_is_rejected() -> None:
    header = bytearray(mkv.stream_header(video_track(), None))
    at = header.index(mkv.enc_id(mkv.ID_TIMESTAMP_SCALE))
    header[at + 5 : at + 6] = b"\x02"  # 1_000_000 -> 2_000_000 in the 3 byte value
    with pytest.raises(mkv.MkvError, match="timestamp scale"):
        mkv.Demuxer().feed(bytes(header) + mkv.encode_cluster([vblock(0, True)], {1: 1}))


def test_cluster_span_is_checked() -> None:
    with pytest.raises(mkv.MkvError, match="does not fit"):
        mkv.encode_cluster([vblock(0), vblock(40000)], {1: 1})


def test_vint_sizes_roundtrip() -> None:
    for size in (0, 1, 126, 127, 16382, 16383, 2**28, 2**35):
        raw = mkv.enc_size(size)
        assert mkv.read_size(raw, 0) == (len(raw), size)
