"""A small streaming Matroska reader and writer: the container the stream relay splices encoder generations in.

Why Matroska and not MPEG-TS: every encoder StreamStack can pick must travel through one carrier, and ffmpeg
cannot demux AV1 out of MPEG-TS (it reports `bin_data`), while Matroska carries AV1, HEVC, H.264, Opus and AAC
alike. Matroska also needs no timestamp rewriting: blocks hold absolute millisecond timestamps, so the relay can
cut from one encoder to the next at a keyframe by choosing which blocks to forward.

`Demuxer` is incremental (feed it whatever a pipe delivered) and works without knowing the size of the segment or
of a cluster: it descends into both and hands out blocks as soon as each is complete, so it adds no latency of its
own. `Writer` produces the live form (unknown-size segment, no cues) that ffmpeg's matroska demuxer reads from a
pipe and that stays playable when the writer dies mid-file (that is what makes recordings crash-safe).

Only what the relay needs is parsed: track entries (number, type, codec, size) and blocks. Everything else in a
track entry (codec private data, Opus delay and pre-roll, colour) is carried through as raw bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ID_EBML = 0x1A45DFA3
ID_SEGMENT = 0x18538067
ID_SEEKHEAD = 0x114D9B74
ID_INFO = 0x1549A966
ID_TRACKS = 0x1654AE6B
ID_TAGS = 0x1254C367
ID_CUES = 0x1C53BB6B
ID_CLUSTER = 0x1F43B675
ID_VOID = 0xEC
ID_TIMESTAMP = 0xE7
ID_SIMPLEBLOCK = 0xA3
ID_BLOCKGROUP = 0xA0
ID_BLOCK = 0xA1
ID_REFERENCE_BLOCK = 0xFB
ID_BLOCK_DURATION = 0x9B
ID_TIMESTAMP_SCALE = 0x2AD7B1
ID_MUXING_APP = 0x4D80
ID_WRITING_APP = 0x5741
ID_TRACK_ENTRY = 0xAE
ID_TRACK_NUMBER = 0xD7
ID_TRACK_UID = 0x73C5
ID_TRACK_TYPE = 0x83
ID_CODEC_ID = 0x86
ID_VIDEO = 0xE0
ID_PIXEL_WIDTH = 0xB0
ID_PIXEL_HEIGHT = 0xBA
ID_DEFAULT_DURATION = 0x23E383
ID_TRACK_NAME = 0x536E

TOP_LEVEL = {ID_SEEKHEAD, ID_INFO, ID_TRACKS, ID_TAGS, ID_CUES, ID_CLUSTER, ID_VOID, 0x1941A469, 0x1043A770}
MAX_ELEMENT = 32 * 1024 * 1024  # a single non-streamed element (a frame, the tracks) larger than this is corruption
MAX_BUFFER = 64 * 1024 * 1024
TRACK_VIDEO, TRACK_AUDIO = 1, 2
TRACK_RAW = 3  # the unprocessed microphone, written to recordings only (never to a live destination)


class MkvError(ValueError):
    """The bytes are not Matroska this reader understands (the message says what and where)."""


@dataclass(frozen=True)
class Track:
    number: int
    kind: str  # "video" | "audio" | "other"
    codec: str  # Matroska codec id, e.g. V_AV1, V_MPEGH/ISO/HEVC, V_MPEG4/ISO/AVC, A_OPUS
    width: int = 0
    height: int = 0
    raw: bytes = b""  # the whole TrackEntry element, as ffmpeg wrote it


@dataclass(frozen=True)
class Header:
    ebml: bytes  # the EBML header element, raw
    timescale_ns: int
    tracks: tuple[Track, ...]

    def track(self, kind: str) -> Track | None:
        return next((t for t in self.tracks if t.kind == kind), None)


@dataclass(frozen=True)
class Block:
    track: int
    ts_ms: int  # absolute, on the clock the encoders were given
    keyframe: bool
    data: bytes  # the frame (or frames, when laced) after the 4 byte block header
    extra: bytes = b""  # BlockGroup siblings of the Block (duration, discard padding, references), raw
    grouped: bool = False  # came in a BlockGroup (so it is written as one)
    lacing: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


# -- EBML primitives -----------------------------------------------------------------------------------------


def read_id(buf: bytes | bytearray | memoryview, at: int) -> tuple[int, int] | None:
    """(length, id) of the element id at `at`, or None when more bytes are needed."""
    if at >= len(buf):
        return None
    first = buf[at]
    length = 1
    while length <= 4 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 4:
        raise MkvError(f"invalid element id byte 0x{first:02x}")
    if at + length > len(buf):
        return None
    return length, int.from_bytes(bytes(buf[at : at + length]), "big")


def read_size(buf: bytes | bytearray | memoryview, at: int) -> tuple[int, int | None] | None:
    """(length, size) of the data-size vint at `at`; size None means unknown. None when more bytes are needed."""
    if at >= len(buf):
        return None
    first = buf[at]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise MkvError(f"invalid element size byte 0x{first:02x}")
    if at + length > len(buf):
        return None
    value = first & ((0x80 >> (length - 1)) - 1)
    for k in range(1, length):
        value = (value << 8) | buf[at + k]
    return length, None if value == (1 << (7 * length)) - 1 else value


def enc_id(element_id: int) -> bytes:
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")


def enc_size(size: int, minimum: int = 1) -> bytes:
    length = max(minimum, 1)
    while size >= (1 << (7 * length)) - 1:
        length += 1
    if length > 8:
        raise MkvError("element too large")
    return ((1 << (7 * length)) | size).to_bytes(length, "big")


def element(element_id: int, body: bytes) -> bytes:
    return enc_id(element_id) + enc_size(len(body)) + body


def uint_element(element_id: int, value: int) -> bytes:
    return element(element_id, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def read_uint(data: bytes) -> int:
    return int.from_bytes(data, "big")


def children(body: bytes) -> list[tuple[int, bytes, bytes]]:
    """Direct children of a master element body as (id, data, raw element bytes); sizes must be known."""
    out: list[tuple[int, bytes, bytes]] = []
    at = 0
    while at < len(body):
        ident = read_id(body, at)
        if ident is None:
            raise MkvError("truncated element id inside a master element")
        sized = read_size(body, at + ident[0])
        if sized is None or sized[1] is None:
            raise MkvError("unknown or truncated size inside a master element")
        start = at + ident[0] + sized[0]
        end = start + sized[1]
        if end > len(body):
            raise MkvError("element runs past the end of its parent")
        out.append((ident[1], body[start:end], body[at:end]))
        at = end
    return out


def parse_track_entry(body: bytes, raw: bytes) -> Track:
    number = kind = codec = None
    width = height = 0
    for child_id, data, _ in children(body):
        if child_id == ID_TRACK_NUMBER:
            number = read_uint(data)
        elif child_id == ID_TRACK_TYPE:
            kind = {1: "video", 2: "audio"}.get(read_uint(data), "other")
        elif child_id == ID_CODEC_ID:
            codec = data.decode("ascii", "replace")
        elif child_id == ID_VIDEO:
            for vid, vdata, _ in children(data):
                if vid == ID_PIXEL_WIDTH:
                    width = read_uint(vdata)
                elif vid == ID_PIXEL_HEIGHT:
                    height = read_uint(vdata)
    if number is None or kind is None or codec is None:
        raise MkvError("track entry without number, type or codec")
    return Track(number, kind, codec, width, height, raw)


def renumber_track(track: Track, number: int, name: str = "") -> bytes:
    """The track entry with another track number (and a number-derived uid), everything else byte for byte.

    `name` (when given) replaces the track's Name, which editors and players show in their track lists."""
    ident = read_id(track.raw, 0)
    sized = read_size(track.raw, ident[0]) if ident else None
    if ident is None or sized is None or sized[1] is None:
        raise MkvError("track entry is not a complete element")
    start = ident[0] + sized[0]
    parts = []
    for child_id, _, raw_child in children(track.raw[start : start + sized[1]]):
        if child_id == ID_TRACK_NUMBER:
            parts.append(uint_element(ID_TRACK_NUMBER, number))
        elif child_id == ID_TRACK_UID:
            parts.append(uint_element(ID_TRACK_UID, number))
        elif child_id == ID_TRACK_NAME and name:
            continue
        else:
            parts.append(raw_child)
    if name:
        parts.append(element(ID_TRACK_NAME, name.encode()))
    return element(ID_TRACK_ENTRY, b"".join(parts))


# -- reader --------------------------------------------------------------------------------------------------


@dataclass
class Demuxer:
    """Feed bytes with `feed`; it returns the events that completed: a `Header` once, then `Block`s."""

    _buf: bytearray = field(default_factory=bytearray)
    _ebml: bytes = b""
    _tracks: tuple[Track, ...] = ()
    _timescale: int = 1_000_000
    _header_sent: bool = False
    _cluster_ts: int | None = None
    _in_cluster: bool = False
    _seen_segment: bool = False
    blocks: int = 0

    def feed(self, data: bytes) -> list[Header | Block]:
        if len(self._buf) + len(data) > MAX_BUFFER:
            raise MkvError("input buffer overflow: the stream stopped making sense")
        self._buf += data
        events: list[Header | Block] = []
        at = 0
        while True:
            consumed = self._step(at, events)
            if consumed is None:
                break
            at = consumed
        if at:
            del self._buf[:at]
        return events

    def _step(self, at: int, events: list[Header | Block]) -> int | None:
        buf = self._buf
        ident = read_id(buf, at)
        if ident is None:
            return None
        id_len, element_id = ident
        sized = read_size(buf, at + id_len)
        if sized is None:
            return None
        size_len, size = sized
        body_at = at + id_len + size_len
        if element_id == ID_SEGMENT:
            self._seen_segment = True
            return body_at  # descend
        if element_id == ID_CLUSTER:
            self._begin_cluster(events)
            return body_at  # descend: children are parsed as they complete
        if size is None:
            raise MkvError(f"unknown size on element 0x{element_id:x}, which cannot be streamed")
        if size > MAX_ELEMENT:
            raise MkvError(f"element 0x{element_id:x} claims {size} bytes")
        end = body_at + size
        if end > len(buf):
            return None
        body = bytes(buf[body_at:end])
        if element_id == ID_EBML:
            self._ebml = bytes(buf[at:end])
        elif element_id == ID_INFO:
            for child_id, data, _ in children(body):
                if child_id == ID_TIMESTAMP_SCALE:
                    self._timescale = read_uint(data)
        elif element_id == ID_TRACKS:
            self._tracks = tuple(
                parse_track_entry(data, raw) for child_id, data, raw in children(body) if child_id == ID_TRACK_ENTRY
            )
        elif self._in_cluster:
            self._cluster_child(element_id, body, events)
        # anything else at the top level (seek head, void, tags, cues) is not needed
        return end

    def _begin_cluster(self, events: list[Header | Block]) -> None:
        if not self._header_sent:
            if not self._tracks:
                raise MkvError("a cluster arrived before the tracks")
            if self._timescale != 1_000_000:
                raise MkvError(
                    f"timestamp scale {self._timescale} ns is not 1 ms; start the muxer with the default scale"
                )
            self._header_sent = True
            events.append(Header(self._ebml, self._timescale, self._tracks))
        self._in_cluster = True
        self._cluster_ts = None

    def _cluster_child(self, element_id: int, body: bytes, events: list[Header | Block]) -> None:
        if element_id == ID_TIMESTAMP:
            self._cluster_ts = read_uint(body)
        elif element_id == ID_SIMPLEBLOCK:
            events.append(self._block(body, b"", simple=True))
        elif element_id == ID_BLOCKGROUP:
            block: bytes | None = None
            extra = []
            referenced = False
            for child_id, data, raw in children(body):
                if child_id == ID_BLOCK:
                    block = data
                else:
                    extra.append(raw)
                    referenced = referenced or child_id == ID_REFERENCE_BLOCK
            if block is None:
                raise MkvError("block group without a block")
            parsed = self._block(block, b"".join(extra), simple=False)
            events.append(
                Block(parsed.track, parsed.ts_ms, not referenced, parsed.data, parsed.extra, True, parsed.lacing)
            )

    def _block(self, body: bytes, extra: bytes, simple: bool) -> Block:
        if self._cluster_ts is None:
            raise MkvError("a block before the cluster timestamp")
        size = read_size(body, 0)
        if size is None or len(body) < size[0] + 3:
            raise MkvError("truncated block header")
        number = body[0] & ((0x80 >> (size[0] - 1)) - 1)
        for k in range(1, size[0]):
            number = (number << 8) | body[k]
        rel = int.from_bytes(body[size[0] : size[0] + 2], "big", signed=True)
        flags = body[size[0] + 2]
        self.blocks += 1
        return Block(
            number, self._cluster_ts + rel, bool(flags & 0x80) if simple else False, body[size[0] + 3 :], extra,
            not simple, (flags >> 1) & 3,
        )  # fmt: skip


# -- writer --------------------------------------------------------------------------------------------------


def stream_header(
    video: Track | None,
    audio: Track | None,
    app: str = "StreamStack",
    raw: Track | None = None,
    names: dict[int, str] | None = None,
) -> bytes:
    """EBML header, an unknown-size Segment, Info and Tracks: video is track 1, audio track 2, the raw microphone
    (recordings only) track 3. `names` labels tracks by their new number."""
    names = names or {}
    ebml = element(
        ID_EBML,
        uint_element(0x4286, 1)  # EBMLVersion
        + uint_element(0x42F7, 1)  # EBMLReadVersion
        + uint_element(0x42F2, 4)  # EBMLMaxIDLength
        + uint_element(0x42F3, 8)  # EBMLMaxSizeLength
        + element(0x4282, b"matroska")  # DocType
        + uint_element(0x4287, 4)  # DocTypeVersion
        + uint_element(0x4285, 2),  # DocTypeReadVersion
    )
    info = element(
        ID_INFO,
        uint_element(ID_TIMESTAMP_SCALE, 1_000_000)
        + element(ID_MUXING_APP, app.encode())
        + element(ID_WRITING_APP, app.encode()),
    )
    entries = b""
    if video is not None:
        entries += renumber_track(video, TRACK_VIDEO, names.get(TRACK_VIDEO, ""))
    if audio is not None:
        entries += renumber_track(audio, TRACK_AUDIO, names.get(TRACK_AUDIO, ""))
    if raw is not None:
        entries += renumber_track(raw, TRACK_RAW, names.get(TRACK_RAW, ""))
    segment = enc_id(ID_SEGMENT) + b"\x01\xff\xff\xff\xff\xff\xff\xff"  # unknown size
    return ebml + segment + info + element(ID_TRACKS, entries)


def encode_cluster(blocks: list[Block], track_map: dict[int, int]) -> bytes:
    """One cluster holding `blocks` (in order), renumbered through `track_map`; its timestamp is the first block's."""
    if not blocks:
        return b""
    base = blocks[0].ts_ms
    body = uint_element(ID_TIMESTAMP, base)
    for block in blocks:
        rel = block.ts_ms - base
        if not -32768 <= rel <= 32767:
            raise MkvError(f"block {rel} ms from its cluster start does not fit")
        number = track_map[block.track]
        head = enc_size(number) + rel.to_bytes(2, "big", signed=True)
        if block.grouped:
            payload = head + bytes([block.lacing << 1]) + block.data
            body += element(ID_BLOCKGROUP, element(ID_BLOCK, payload) + block.extra)
        else:
            flags = (0x80 if block.keyframe else 0) | (block.lacing << 1)
            body += element(ID_SIMPLEBLOCK, head + bytes([flags]) + block.data)
    return enc_id(ID_CLUSTER) + enc_size(len(body), minimum=4) + body
