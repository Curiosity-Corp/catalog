"""Matroska blocks to RTP payloads: codec configuration, parameter-set injection, and the two RTP clocks."""

from __future__ import annotations

import pytest
from stream_support import track_entry
from test_whip_payload import avcc, obu

from streamstack.stream import mkv
from streamstack.stream.whip import media, payload


def track_with(codec: str, private: bytes, kind: str = "video") -> mkv.Track:
    body = mkv.uint_element(mkv.ID_TRACK_NUMBER, 1) + mkv.uint_element(mkv.ID_TRACK_TYPE, 1 if kind == "video" else 2)
    body += mkv.element(mkv.ID_CODEC_ID, codec.encode())
    if kind == "video":
        body += mkv.element(
            mkv.ID_VIDEO, mkv.uint_element(mkv.ID_PIXEL_WIDTH, 1280) + mkv.uint_element(mkv.ID_PIXEL_HEIGHT, 720)
        )
    if private:
        body += mkv.element(0x63A2, private)
    return mkv.parse_track_entry(body, mkv.element(mkv.ID_TRACK_ENTRY, body))


def sample(*nals: bytes, size: int = 4) -> bytes:
    return b"".join(len(n).to_bytes(size, "big") + n for n in nals)


def block(data: bytes, ts: int, key: bool = False, track: int = 1) -> mkv.Block:
    return mkv.Block(track, ts, key, data)


class TestConfig:
    def test_h264(self) -> None:
        config = media.video_config(track_with("V_MPEG4/ISO/AVC", avcc()))
        assert config.kind == "h264" and config.label == "H.264" and config.length_size == 4
        assert config.spec.fmtp.endswith("profile-level-id=64001f") and len(config.parameter_sets) == 2

    def test_codec_private_is_found(self) -> None:
        assert media.codec_private(track_with("V_AV1", b"\x81abc")) == b"\x81abc"
        assert media.codec_private(track_with("V_AV1", b"")) == b""

    @pytest.mark.parametrize(
        ("codec", "private", "message"),
        [
            ("V_VP9", b"x", "cannot send V_VP9"),
            ("V_MPEG4/ISO/AVC", b"", "no codec configuration"),
            ("V_MPEG4/ISO/AVC", b"\x02garbage-record", "unusable"),
        ],
    )
    def test_unusable_tracks(self, codec: str, private: bytes, message: str) -> None:
        with pytest.raises(media.MediaError, match=message):
            media.video_config(track_with(codec, private))

    def test_audio_must_be_opus(self) -> None:
        assert media.audio_config(track_entry(2, "audio", "A_OPUS")).name == "opus"
        with pytest.raises(media.MediaError, match="Opus"):
            media.audio_config(track_entry(2, "audio", "A_AAC"))


class TestVideo:
    def setup_method(self) -> None:
        self.config = media.video_config(track_with("V_MPEG4/ISO/AVC", avcc()))
        self.sps, self.pps = self.config.parameter_sets

    def test_a_keyframe_gets_the_parameter_sets_in_front(self) -> None:
        idr = b"\x65" + b"i" * 50
        frame = media.VideoPacketizer(self.config).packetize(block(sample(idr), 1000, key=True))
        assert frame is not None and frame.keyframe
        # SPS, PPS and the IDR are small, so one STAP-A carries all three, in order
        assert len(frame.payloads) == 1 and frame.payloads[0][0] & 0x1F == 24
        assert frame.payloads[0][3 : 3 + len(self.sps)] == self.sps

    def test_a_keyframe_that_has_its_own_parameter_sets_is_not_given_a_second_set(self) -> None:
        own = [b"\x67other", b"\x68pps", b"\x65" + b"i" * 20]
        frame = media.VideoPacketizer(self.config).packetize(block(sample(*own), 0, key=True))
        assert frame is not None and self.sps not in frame.payloads[0]

    def test_a_delta_frame_is_left_alone(self) -> None:
        delta = b"\x41" + b"p" * 40
        frame = media.VideoPacketizer(self.config).packetize(block(sample(delta), 40))
        assert frame is not None and frame.payloads == [delta] and not frame.keyframe

    def test_timestamps_run_on_the_90khz_clock_from_the_first_block(self) -> None:
        packetizer = media.VideoPacketizer(self.config)
        stamps = [
            packetizer.packetize(block(sample(b"\x41" + b"x" * 9), ts)).timestamp for ts in (5000, 5033, 5067, 6000)
        ]  # type: ignore[union-attr]
        assert stamps == [0, 2970, 6030, 90000]

    def test_a_new_encoder_generation_brings_its_own_parameter_sets(self) -> None:
        packetizer = media.VideoPacketizer(self.config)
        other = media.video_config(track_with("V_MPEG4/ISO/AVC", avcc(profile=0x4D, level=0x28)))
        packetizer.reconfigure(other)
        frame = packetizer.packetize(block(sample(b"\x65" + b"i" * 30), 0, key=True))
        assert frame is not None and other.parameter_sets[0] in frame.payloads[0]

    def test_a_corrupt_sample_is_counted_and_skipped(self) -> None:
        packetizer = media.VideoPacketizer(self.config)
        assert packetizer.packetize(block((99).to_bytes(4, "big") + b"short", 0)) is None
        assert packetizer.corrupt == 1

    def test_laced_video_is_refused(self) -> None:
        with pytest.raises(media.MediaError, match="laced"):
            media.VideoPacketizer(self.config).packetize(mkv.Block(1, 0, True, b"x", lacing=1))

    def test_av1_gets_the_sequence_header_and_the_new_sequence_bit(self) -> None:
        seq = obu(1, b"\x00\x00\x00")
        config = media.video_config(track_with("V_AV1", bytes([0x81, 0x08, 0x0C, 0x00]) + seq))
        assert config.kind == "av1" and config.sequence_header
        frame = media.VideoPacketizer(config).packetize(block(obu(2, b"") + obu(6, b"f" * 30), 0, key=True))
        assert frame is not None and frame.payloads[0][0] & 0x08  # N: a new coded video sequence
        assert config.sequence_header in frame.payloads[0]
        delta = media.VideoPacketizer(config).packetize(block(obu(6, b"f" * 30), 0))
        assert delta is not None and not delta.payloads[0][0] & 0x08 and config.sequence_header not in delta.payloads[0]

    def test_hevc_gets_vps_sps_pps(self) -> None:
        arrays = b""
        for kind, unit in ((32, b"\x40\x01aa"), (33, b"\x42\x01bb"), (34, b"\x44\x01cc")):
            arrays += bytes([0x80 | kind]) + b"\x00\x01" + len(unit).to_bytes(2, "big") + unit
        record = bytes([1, 0x01]) + bytes(19) + bytes([0xFF, 3]) + arrays
        config = media.video_config(track_with("V_MPEGH/ISO/HEVC", record))
        idr = bytes([19 << 1, 1]) + b"i" * 40
        frame = media.VideoPacketizer(config).packetize(block(sample(idr), 0, key=True))
        assert frame is not None and frame.payloads[:3] == [b"\x40\x01aa", b"\x42\x01bb", b"\x44\x01cc"]


class TestAudio:
    def opus(self, size: int = 80) -> bytes:
        return bytes([0xFC]) + b"o" * (size - 1)  # CELT fullband, 20 ms

    def test_the_clock_counts_samples_so_it_never_jitters(self) -> None:
        packetizer = media.AudioPacketizer()
        stamps = [packetizer.packetize(block(self.opus(), ts, track=2)).timestamp for ts in (100, 121, 139, 161)]  # type: ignore[union-attr]
        assert stamps == [0, 960, 1920, 2880]  # block times wobble by a millisecond; the samples do not

    def test_a_gap_reanchors_on_the_block_time(self) -> None:
        packetizer = media.AudioPacketizer()
        packetizer.packetize(block(self.opus(), 0, track=2))
        frame = packetizer.packetize(block(self.opus(), 500, track=2))
        assert frame is not None and frame.timestamp == 500 * 48

    def test_audio_and_video_share_one_origin(self) -> None:
        origin = media.Origin()
        video = media.VideoPacketizer(media.video_config(track_with("V_MPEG4/ISO/AVC", avcc())), origin)
        audio = media.AudioPacketizer(origin)
        video.packetize(block(sample(b"\x41" + b"x" * 9), 1000))
        frame = audio.packetize(block(self.opus(), 1100, track=2))
        assert frame is not None and frame.timestamp == 100 * 48

    def test_an_empty_or_broken_packet_is_skipped(self) -> None:
        packetizer = media.AudioPacketizer()
        assert packetizer.packetize(block(b"", 0, track=2)) is None
        assert packetizer.packetize(block(bytes([0xFF]), 0, track=2)) is None and packetizer.corrupt == 1

    def test_payload_is_the_opus_packet_itself(self) -> None:
        packet = self.opus()
        frame = media.AudioPacketizer().packetize(block(packet, 0, track=2))
        assert frame is not None and frame.payloads == [packet] and payload.opus_samples(packet) == 960
