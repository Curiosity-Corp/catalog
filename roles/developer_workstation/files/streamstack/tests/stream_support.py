"""Builders shared by the stream tests: synthetic Matroska tracks, blocks and a recording sink."""

from __future__ import annotations

from streamstack.stream import mkv
from streamstack.stream.mkv import Block, Track


def track_entry(number: int, kind: str, codec: str, width: int = 0, height: int = 0) -> Track:
    body = mkv.uint_element(mkv.ID_TRACK_NUMBER, number) + mkv.uint_element(mkv.ID_TRACK_UID, number + 100)
    body += mkv.uint_element(mkv.ID_TRACK_TYPE, 1 if kind == "video" else 2) + mkv.element(
        mkv.ID_CODEC_ID, codec.encode()
    )
    if kind == "video":
        body += mkv.element(
            mkv.ID_VIDEO, mkv.uint_element(mkv.ID_PIXEL_WIDTH, width) + mkv.uint_element(mkv.ID_PIXEL_HEIGHT, height)
        )
    body += mkv.element(0x63A2, b"private-" + codec.encode())  # CodecPrivate, carried through untouched
    return mkv.parse_track_entry(body, mkv.element(mkv.ID_TRACK_ENTRY, body))


def video_track(width: int = 1280, height: int = 720, codec: str = "V_AV1") -> Track:
    return track_entry(1, "video", codec, width, height)


def audio_track() -> Track:
    return track_entry(1, "audio", "A_OPUS")


def vblock(ts: int, key: bool = False, size: int = 100) -> Block:
    return Block(mkv.TRACK_VIDEO, ts, key, bytes([ts & 0xFF]) * size)


def ablock(ts: int, size: int = 20) -> Block:
    return Block(mkv.TRACK_AUDIO, ts, True, bytes([0xA0]) * size)


def source_stream(blocks: list[Block], video: Track | None = None, audio: Track | None = None) -> bytes:
    """A whole Matroska stream as an encoder would write it (video and audio entries as given)."""
    video = video or video_track()
    audio = audio or audio_track()
    data = mkv.stream_header(video, audio)
    for block in blocks:
        data += mkv.encode_cluster([block], {1: block.track if block.track in (1, 2) else 1, 2: 2})
    return data


class RecordingSink:
    """A relay sink that keeps what it was given, for assertions."""

    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.headers: list[bytes] = []
        self.writes: list[tuple[bytes, int]] = []
        self.closed = 0

    def open(self, header: bytes) -> None:
        self.headers.append(header)

    def write(self, data: bytes, video_blocks: int, keyframe: bool) -> bool:
        self.writes.append((data, video_blocks))
        return True

    def close(self) -> None:
        self.closed += 1

    def blocks(self) -> list[Block]:
        """Everything written, parsed back (header first)."""
        demux = mkv.Demuxer()
        events = []
        for header in self.headers[-1:]:
            events += demux.feed(header)
        for data, _ in self.writes:
            events += demux.feed(data)
        return [e for e in events if isinstance(e, Block)]
