"""From Matroska blocks to RTP payloads: what the WHIP sender puts on the wire.

The relay hands a sink Matroska; this module reads the codec configuration out of the track entries (`avcC`,
`hvcC`, `av1C` in CodecPrivate), says which SDP codec that is, and cuts each block into packets. Video is never
decoded or re-encoded. Parameter sets (SPS/PPS, VPS, the AV1 sequence header) travel in-band in front of every
keyframe, because Matroska keeps them out of the frames and a receiver that joins at a keyframe has no other
way to get them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from streamstack.stream import mkv
from streamstack.stream.whip import payload, sdp

H264, HEVC, AV1, OPUS = "h264", "hevc", "av1", "opus"
MATROSKA_CODECS = {"V_MPEG4/ISO/AVC": H264, "V_MPEGH/ISO/HEVC": HEVC, "V_AV1": AV1, "A_OPUS": OPUS}
VIDEO_CLOCK = 90000
AUDIO_CLOCK = 48000
GAP_MS = 60  # an audio gap longer than this re-anchors the RTP clock on the block time


class MediaError(ValueError):
    """The track is something WHIP publishing cannot send (the message says what)."""


def codec_private(track: mkv.Track) -> bytes:
    """The CodecPrivate bytes of a track entry (empty when it has none)."""
    ident = mkv.read_id(track.raw, 0)
    sized = mkv.read_size(track.raw, ident[0]) if ident else None
    if ident is None or sized is None or sized[1] is None:
        raise MediaError("the track entry is not a complete element")
    start = ident[0] + sized[0]
    for child_id, data, _ in mkv.children(track.raw[start : start + sized[1]]):
        if child_id == 0x63A2:
            return data
    return b""


@dataclass(frozen=True)
class VideoConfig:
    kind: str  # h264 | hevc | av1
    spec: sdp.CodecSpec
    length_size: int = 4
    parameter_sets: tuple[bytes, ...] = ()
    sequence_header: bytes = b""

    @property
    def label(self) -> str:
        return {H264: "H.264", HEVC: "HEVC", AV1: "AV1"}[self.kind]


def video_config(track: mkv.Track) -> VideoConfig:
    kind = MATROSKA_CODECS.get(track.codec)
    if kind not in (H264, HEVC, AV1):
        raise MediaError(f"WHIP cannot send {track.codec}; it carries H.264, HEVC or AV1")
    record = codec_private(track)
    if not record:
        raise MediaError(f"the {track.codec} track has no codec configuration, so its parameters cannot be sent")
    try:
        if kind == H264:
            avc = payload.parse_avcc(record)
            return VideoConfig(kind, sdp.h264_spec(avc.profile_level_id), avc.length_size, tuple(avc.parameter_sets()))
        if kind == HEVC:
            hevc = payload.parse_hvcc(record)
            spec = sdp.hevc_spec(hevc.profile_id, hevc.tier_flag, hevc.level_id)
            return VideoConfig(kind, spec, hevc.length_size, tuple(hevc.parameter_sets()))
        av1 = payload.parse_av1c(record)
        return VideoConfig(kind, sdp.av1_spec(av1.profile, av1.level_idx, av1.tier), 0, (), av1.sequence_header)
    except payload.PayloadError as exc:
        raise MediaError(f"the {track.codec} codec configuration is unusable: {exc}") from None


def audio_config(track: mkv.Track) -> sdp.CodecSpec:
    if MATROSKA_CODECS.get(track.codec) != OPUS:
        raise MediaError(f"WHIP sends Opus audio; the stream has {track.codec}")
    return sdp.OPUS


class Origin:
    """The block time that is RTP time zero, shared by audio and video so the two clocks start together."""

    def __init__(self) -> None:
        self.ms: int | None = None

    def at(self, ts_ms: int) -> int:
        if self.ms is None:
            self.ms = ts_ms
        return max(0, ts_ms - self.ms)


@dataclass(frozen=True)
class Frame:
    payloads: list[bytes]
    timestamp: int  # RTP clock units from the first frame, not yet wrapped
    keyframe: bool = False
    size: int = 0


class VideoPacketizer:
    """Turns video blocks into RTP payload lists on the 90 kHz clock."""

    def __init__(self, config: VideoConfig, origin: Origin | None = None) -> None:
        self.config = config
        self._origin = origin or Origin()
        self.corrupt = 0

    def reconfigure(self, config: VideoConfig) -> None:
        """A new encoder generation: its parameter sets apply from its next keyframe."""
        self.config = config

    def packetize(self, block: mkv.Block) -> Frame | None:
        """The packets for one block, or None when the sample is unusable (counted in `corrupt`)."""
        if block.lacing:
            raise MediaError("laced video blocks are not valid Matroska video")
        ts = self._origin.at(block.ts_ms) * VIDEO_CLOCK // 1000
        config = self.config
        try:
            if config.kind == H264:
                nals = payload.length_prefixed(block.data, config.length_size)
                if block.keyframe:
                    nals = _with_parameter_sets(nals, config.parameter_sets, lambda n: n[0] & 0x1F in (7, 8))
                packets = payload.packetize_h264(nals)
            elif config.kind == HEVC:
                nals = payload.length_prefixed(block.data, config.length_size)
                if block.keyframe:
                    nals = _with_parameter_sets(
                        nals, config.parameter_sets, lambda n: (n[0] >> 1) & 0x3F in (32, 33, 34)
                    )
                packets = payload.packetize_hevc(nals)
            else:
                obus = payload.split_obus(block.data)
                new_sequence = False
                if block.keyframe:
                    new_sequence = True
                    if not any(o.kind == payload.OBU_SEQUENCE_HEADER for o in obus) and config.sequence_header:
                        obus = [*payload.split_obus(config.sequence_header), *obus]
                packets = payload.packetize_av1(obus, new_sequence)
        except payload.PayloadError:
            self.corrupt += 1
            return None
        if not packets:
            return None
        return Frame(packets, ts, block.keyframe, sum(len(p) for p in packets))


def _with_parameter_sets(nals: list[bytes], sets: tuple[bytes, ...], is_set: Callable[[bytes], bool]) -> list[bytes]:
    """`sets` in front of the keyframe unless the encoder already put its own parameter sets there."""
    if not sets or any(is_set(n) for n in nals if n):
        return nals
    return [*sets, *nals]


class AudioPacketizer:
    """Opus: one block, one packet, on a 48 kHz clock counted in samples so the receiver's jitter buffer is exact."""

    def __init__(self, origin: Origin | None = None) -> None:
        self._origin = origin or Origin()
        self._next = 0
        self.corrupt = 0

    def packetize(self, block: mkv.Block) -> Frame | None:
        if block.lacing:
            raise MediaError("laced audio blocks are not supported")
        if not block.data:
            return None
        try:
            samples = payload.opus_samples(block.data)
        except payload.PayloadError:
            self.corrupt += 1
            return None
        anchored = self._origin.at(block.ts_ms) * AUDIO_CLOCK // 1000
        if abs(anchored - self._next) > GAP_MS * AUDIO_CLOCK // 1000:
            self._next = anchored  # a gap (or the clock moved): follow the block time again
        ts = self._next
        self._next += samples
        return Frame([block.data], ts, False, len(block.data))
