"""WHIP RTP payload formats: every packetizer is checked by reassembling its packets the way a receiver does."""

from __future__ import annotations

import os
import random

import pytest

from streamstack.stream.whip import payload

RNG = random.Random(7)  # noqa: S311 - deterministic test data, not a secret


def nal(kind: int, size: int, nri: int = 3) -> bytes:
    """An H.264 NAL unit of `size` bytes (header included) with deterministic payload."""
    return bytes([(nri << 5) | kind]) + bytes(RNG.getrandbits(8) for _ in range(size - 1))


def hevc_nal(kind: int, size: int) -> bytes:
    return bytes([kind << 1, 1]) + bytes(RNG.getrandbits(8) for _ in range(size - 2))


def depacketize_h264(packets: list[bytes]) -> list[bytes]:
    out: list[bytes] = []
    fragment = b""
    for pkt in packets:
        kind = pkt[0] & 0x1F
        if kind == 24:
            at = 1
            while at < len(pkt):
                size = int.from_bytes(pkt[at : at + 2], "big")
                out.append(pkt[at + 2 : at + 2 + size])
                at += 2 + size
        elif kind == 28:
            start, end = pkt[1] & 0x80, pkt[1] & 0x40
            if start:
                fragment = bytes([(pkt[0] & 0xE0) | (pkt[1] & 0x1F)])
            fragment += pkt[2:]
            if end:
                out.append(fragment)
                fragment = b""
        else:
            out.append(pkt)
    return out


class TestH264:
    def test_small_units_are_aggregated_into_one_stap_a(self) -> None:
        units = [nal(7, 20), nal(8, 6), nal(5, 300)]
        packets = payload.packetize_h264(units)
        assert len(packets) == 1 and packets[0][0] & 0x1F == 24
        assert depacketize_h264(packets) == units

    def test_stap_a_carries_the_highest_nri_of_its_units(self) -> None:
        packets = payload.packetize_h264([nal(7, 10, nri=3), nal(1, 10, nri=0)])
        assert packets[0][0] >> 5 & 3 == 3

    def test_a_single_unit_goes_out_as_itself(self) -> None:
        unit = nal(1, 500)
        assert payload.packetize_h264([unit]) == [unit]

    def test_a_large_unit_is_fragmented_with_start_and_end_bits(self) -> None:
        unit = nal(5, 5000)
        packets = payload.packetize_h264([unit])
        assert len(packets) > 3 and all(len(p) <= payload.MAX_PAYLOAD for p in packets)
        assert packets[0][1] & 0x80 and not packets[0][1] & 0x40
        assert packets[-1][1] & 0x40 and not packets[-1][1] & 0x80
        assert all(not p[1] & 0xC0 for p in packets[1:-1])
        assert {p[1] & 0x1F for p in packets} == {5}
        assert depacketize_h264(packets) == [unit]

    @pytest.mark.parametrize("sizes", [[1200, 1200, 1199], [1198, 1198], [1, 1199, 5000, 2, 30], [3000] * 3])
    def test_round_trip_around_the_packet_size_boundary(self, sizes: list[int]) -> None:
        units = [nal(1 if k else 5, n) for k, n in enumerate(sizes) if n > 1] + [nal(6, 2)]
        packets = payload.packetize_h264(units)
        assert all(len(p) <= payload.MAX_PAYLOAD for p in packets)
        assert depacketize_h264(packets) == units

    def test_empty_units_are_skipped(self) -> None:
        unit = nal(1, 10)
        assert payload.packetize_h264([b"", unit]) == [unit]


class TestHevc:
    def test_a_small_unit_is_a_single_nal_packet(self) -> None:
        unit = hevc_nal(1, 400)
        assert payload.packetize_hevc([unit]) == [unit]

    def test_a_large_unit_is_fragmented_with_type_49(self) -> None:
        unit = hevc_nal(19, 4000)
        packets = payload.packetize_hevc([unit])
        assert len(packets) > 2 and all(len(p) <= payload.MAX_PAYLOAD for p in packets)
        assert all((p[0] >> 1) & 0x3F == 49 for p in packets)
        assert packets[0][2] & 0x80 and packets[-1][2] & 0x40
        rebuilt = bytes([(packets[0][0] & 0x81) | ((packets[0][2] & 0x3F) << 1), packets[0][1]])
        rebuilt += b"".join(p[3:] for p in packets)
        assert rebuilt == unit

    def test_the_layer_and_temporal_id_bits_are_kept(self) -> None:
        unit = bytes([(19 << 1) | 1, 0x03]) + os.urandom(3000)  # LayerId high bit set, TID 3
        packets = payload.packetize_hevc([unit])
        assert all(p[0] & 1 == 1 and p[1] == 0x03 for p in packets)


def obu(kind: int, body: bytes, sized: bool = True, ext: bool = False) -> bytes:
    head = (kind << 3) | (0x02 if sized else 0) | (0x04 if ext else 0)
    out = bytes([head]) + (b"\x10" if ext else b"")
    return out + (payload.leb128(len(body)) if sized else b"") + body


def reassemble_av1(packets: list[bytes]) -> list[bytes]:
    elements: list[bytes] = []
    carry = b""
    for pkt in packets:
        z, y = bool(pkt[0] & 0x80), bool(pkt[0] & 0x40)
        w = (pkt[0] >> 4) & 3
        assert w == 0
        at = 1
        pieces = []
        while at < len(pkt):
            size, used = payload.read_leb128(pkt, at)
            pieces.append(pkt[at + used : at + used + size])
            at += used + size
        if z:
            carry += pieces[0]
            pieces = pieces[1:]
            if pieces or not y:
                elements.append(carry)
                carry = b""
        if y:
            carry = pieces.pop() if pieces else carry
        elements.extend(pieces)
    assert carry == b""
    return elements


class TestAv1:
    def test_split_obus_drops_delimiters_and_padding_and_the_size_field(self) -> None:
        data = obu(2, b"") + obu(1, b"\x0a\x0b") + obu(6, b"frame!") + obu(15, b"pad")
        obus = payload.split_obus(data)
        assert [o.kind for o in obus] == [1, 6]
        assert obus[0].element == bytes([(1 << 3)]) + b"\x0a\x0b"
        assert not obus[1].element[0] & 0x02

    def test_an_obu_without_a_size_field_runs_to_the_end(self) -> None:
        data = bytes([6 << 3]) + b"abcdef"
        assert payload.split_obus(data)[0].element == data

    def test_extension_byte_is_kept(self) -> None:
        got = payload.split_obus(obu(6, b"xyz", ext=True))[0].element
        assert got[0] & 0x04 and got[1] == 0x10 and got[2:] == b"xyz"

    @pytest.mark.parametrize("bad", [bytes([0x80]), bytes([6 << 3 | 2, 50]) + b"short"])
    def test_malformed_obus_raise(self, bad: bytes) -> None:
        with pytest.raises(payload.PayloadError):
            payload.split_obus(bad)

    def test_small_unit_is_one_packet_with_the_new_sequence_bit(self) -> None:
        obus = payload.split_obus(obu(1, b"seq") + obu(6, b"f" * 100))
        packets = payload.packetize_av1(obus, new_sequence=True)
        assert len(packets) == 1 and packets[0][0] == 0x08
        assert reassemble_av1(packets) == [o.element for o in obus]
        assert payload.packetize_av1(obus, new_sequence=False)[0][0] == 0x00

    def test_a_large_obu_is_split_with_z_and_y_bits(self) -> None:
        obus = payload.split_obus(obu(6, os.urandom(6000)))
        packets = payload.packetize_av1(obus, new_sequence=False)
        assert len(packets) >= 5 and all(len(p) <= payload.MAX_PAYLOAD for p in packets)
        assert not packets[0][0] & 0x80 and packets[0][0] & 0x40
        assert packets[-1][0] & 0x80 and not packets[-1][0] & 0x40
        assert all(p[0] & 0xC0 == 0xC0 for p in packets[1:-1])
        assert reassemble_av1(packets) == [obus[0].element]

    def test_mixed_sizes_round_trip(self) -> None:
        raw = obu(1, b"s" * 12) + obu(6, os.urandom(2500)) + obu(6, b"t" * 30) + obu(4, os.urandom(1190))
        obus = payload.split_obus(raw)
        packets = payload.packetize_av1(obus, new_sequence=True)
        assert all(len(p) <= payload.MAX_PAYLOAD for p in packets)
        assert reassemble_av1(packets) == [o.element for o in obus]


class TestLeb128:
    @pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 16383, 16384, 2**32])
    def test_round_trip(self, value: int) -> None:
        data = payload.leb128(value)
        assert payload.read_leb128(data + b"x", 0) == (value, len(data))

    def test_truncated_raises(self) -> None:
        with pytest.raises(payload.PayloadError):
            payload.read_leb128(b"\x80", 0)


class TestOpus:
    @pytest.mark.parametrize(
        ("toc", "samples"),
        [
            (0x00, 480),  # SILK narrowband, 10 ms
            (0x08, 960),  # 20 ms
            (0x10, 1920),  # 40 ms
            (0x18, 2880),  # 60 ms
            (0x60, 480),  # hybrid, 10 ms
            (0x68, 960),  # hybrid, 20 ms
            (0x80, 120),  # CELT, 2.5 ms
            (0x98, 960),  # CELT narrowband, 20 ms
            (0xFC, 960),  # CELT fullband, 20 ms, one frame
        ],
    )
    def test_one_frame_durations(self, toc: int, samples: int) -> None:
        assert payload.opus_samples(bytes([toc]) + b"x" * 10) == samples

    def test_two_and_counted_frames(self) -> None:
        assert payload.opus_samples(bytes([0xFD]) + b"x" * 40) == 1920  # code 1: two frames
        assert payload.opus_samples(bytes([0xFE]) + b"x" * 40) == 1920  # code 2: two frames of different size
        assert payload.opus_samples(bytes([0xFF, 3]) + b"x" * 40) == 3 * 960  # code 3: the count follows

    def test_empty_or_truncated_packet_raises(self) -> None:
        with pytest.raises(payload.PayloadError):
            payload.opus_samples(b"")
        with pytest.raises(payload.PayloadError):
            payload.opus_samples(bytes([0xFF]))


def avcc(profile: int = 0x64, compat: int = 0, level: int = 0x1F, length_size: int = 4) -> bytes:
    sps, pps = b"\x67\x64\x00\x1f\xac\xd9", b"\x68\xeb\xe3\xcb"
    return (
        bytes([1, profile, compat, level, 0xFC | (length_size - 1), 0xE1])
        + len(sps).to_bytes(2, "big")
        + sps
        + b"\x01"
        + len(pps).to_bytes(2, "big")
        + pps
    )


class TestConfigs:
    def test_avcc(self) -> None:
        config = payload.parse_avcc(avcc())
        assert config.profile_level_id == "64001f" and config.length_size == 4
        assert config.sps == (b"\x67\x64\x00\x1f\xac\xd9",) and config.pps == (b"\x68\xeb\xe3\xcb",)
        assert config.parameter_sets() == [*config.sps, *config.pps]
        assert payload.parse_avcc(avcc(length_size=2)).length_size == 2

    @pytest.mark.parametrize("bad", [b"", b"\x02" * 10, avcc()[:12], avcc()[:7]])
    def test_bad_avcc_raises(self, bad: bytes) -> None:
        with pytest.raises(payload.PayloadError):
            payload.parse_avcc(bad)

    def test_hvcc(self) -> None:
        arrays = b""
        for kind, unit in ((32, b"\x40\x01aa"), (33, b"\x42\x01bb"), (34, b"\x44\x01cc")):
            arrays += bytes([0x80 | kind]) + b"\x00\x01" + len(unit).to_bytes(2, "big") + unit
        record = bytes([1, 0x01]) + bytes(10) + bytes([93]) + bytes(8) + bytes([0xFF, 3]) + arrays
        record = record[:21] + bytes([0xFF]) + bytes([3]) + arrays
        config = payload.parse_hvcc(record)
        assert (config.profile_id, config.tier_flag, config.level_id, config.length_size) == (1, 0, 93, 4)
        assert config.vps == (b"\x40\x01aa",) and config.sps == (b"\x42\x01bb",) and config.pps == (b"\x44\x01cc",)
        with pytest.raises(payload.PayloadError):
            payload.parse_hvcc(record[:23] + bytes([0]))

    def test_av1c(self) -> None:
        seq = obu(1, b"\x00\x00\x00")
        config = payload.parse_av1c(bytes([0x81, 0x08, 0x0C, 0x00]) + seq)
        assert (config.profile, config.level_idx, config.tier) == (0, 8, 0)
        assert config.sequence_header == bytes([1 << 3]) + b"\x00\x00\x00"
        with pytest.raises(payload.PayloadError):
            payload.parse_av1c(b"\x01\x00\x00\x00")

    def test_length_prefixed_samples(self) -> None:
        sample = (3).to_bytes(4, "big") + b"abc" + (0).to_bytes(4, "big") + (2).to_bytes(4, "big") + b"zz"
        assert payload.length_prefixed(sample, 4) == [b"abc", b"zz"]
        with pytest.raises(payload.PayloadError):
            payload.length_prefixed((9).to_bytes(4, "big") + b"ab", 4)
        with pytest.raises(payload.PayloadError):
            payload.length_prefixed(b"\x00\x00", 4)
        with pytest.raises(payload.PayloadError):
            payload.length_prefixed(b"", 5)
