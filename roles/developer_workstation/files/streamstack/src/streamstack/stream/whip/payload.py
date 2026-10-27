"""RTP payload formats for what StreamStack's encoders produce: H.264, HEVC, AV1 and Opus.

These are the packetizers (RFC 6184, RFC 7798, the AOM AV1 RTP specification and RFC 7587) and the readers for the
codec configuration records Matroska carries (`avcC`, `hvcC`, `av1C`). Nothing here knows about the network or about
aiortc, so every byte layout is unit-tested directly.

The encoders run on the GPU and their output goes on the wire as it is: this module only cuts it into packets.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_PAYLOAD = 1200  # bytes of RTP payload: leaves room for the header, extensions, SRTP and UDP over a 1280 MTU path


class PayloadError(ValueError):
    """The codec data is not what its container said it would be."""


# -- helpers ---------------------------------------------------------------------------------------------------


def leb128(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_leb128(data: bytes, at: int) -> tuple[int, int]:
    """(value, length in bytes) of the LEB128 number at `at`."""
    value = 0
    for k in range(8):
        if at + k >= len(data):
            raise PayloadError("truncated LEB128 number")
        byte = data[at + k]
        value |= (byte & 0x7F) << (7 * k)
        if not byte & 0x80:
            return value, k + 1
    raise PayloadError("LEB128 number is longer than 8 bytes")


def length_prefixed(data: bytes, length_size: int) -> list[bytes]:
    """The NAL units of an AVC/HEVC sample in the length-prefixed (`avcC`/`hvcC`) form."""
    if length_size not in (1, 2, 3, 4):
        raise PayloadError(f"NAL length size {length_size} is not 1 to 4")
    units: list[bytes] = []
    at = 0
    while at < len(data):
        if at + length_size > len(data):
            raise PayloadError("truncated NAL length")
        size = int.from_bytes(data[at : at + length_size], "big")
        at += length_size
        if size == 0:
            continue
        if at + size > len(data):
            raise PayloadError("a NAL unit runs past the end of its sample")
        units.append(data[at : at + size])
        at += size
    return units


# -- H.264 (RFC 6184), packetization mode 1 ------------------------------------------------------------------------


def packetize_h264(nals: list[bytes], max_payload: int = MAX_PAYLOAD) -> list[bytes]:
    """One access unit as RTP payloads: STAP-A for runs of small NAL units, a single packet for a NAL unit that
    fits, FU-A for one that does not. The caller sets the marker bit on the last payload."""
    out: list[bytes] = []
    run: list[bytes] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            ref = max(n[0] & 0x60 for n in run)  # NRI: the highest of the aggregated units
            out.append(bytes([ref | 24]) + b"".join(len(n).to_bytes(2, "big") + n for n in run))
        run.clear()

    used = 1  # the STAP-A header, counted once per run
    for nal in nals:
        if not nal:
            continue
        if len(nal) > max_payload:
            flush()
            used = 1
            out.extend(_fu_a(nal, max_payload))
            continue
        if run and used + 2 + len(nal) > max_payload:
            flush()
            used = 1
        run.append(nal)
        used += 2 + len(nal)
    flush()
    return out


def _fu_a(nal: bytes, max_payload: int) -> list[bytes]:
    indicator = (nal[0] & 0xE0) | 28
    kind = nal[0] & 0x1F
    body = nal[1:]
    size = max_payload - 2
    chunks = [body[i : i + size] for i in range(0, len(body), size)]
    out = []
    for k, chunk in enumerate(chunks):
        header = kind | (0x80 if k == 0 else 0) | (0x40 if k == len(chunks) - 1 else 0)
        out.append(bytes([indicator, header]) + chunk)
    return out


# -- HEVC (RFC 7798) -----------------------------------------------------------------------------------------------


def packetize_hevc(nals: list[bytes], max_payload: int = MAX_PAYLOAD) -> list[bytes]:
    """Single NAL unit packets, and fragmentation units (type 49) for the ones that do not fit."""
    out: list[bytes] = []
    for nal in nals:
        if len(nal) < 3:
            continue
        if len(nal) <= max_payload:
            out.append(nal)
            continue
        kind = (nal[0] >> 1) & 0x3F
        header = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        body = nal[2:]
        size = max_payload - 3
        chunks = [body[i : i + size] for i in range(0, len(body), size)]
        for k, chunk in enumerate(chunks):
            fu = kind | (0x80 if k == 0 else 0) | (0x40 if k == len(chunks) - 1 else 0)
            out.append(header + bytes([fu]) + chunk)
    return out


# -- AV1 (AOM "RTP Payload Format For AV1") ------------------------------------------------------------------------

OBU_SEQUENCE_HEADER = 1
OBU_TEMPORAL_DELIMITER = 2
OBU_PADDING = 15


@dataclass(frozen=True)
class Obu:
    kind: int
    element: bytes  # the OBU as an RTP element wants it: header (without the size flag) and payload


def split_obus(data: bytes) -> list[Obu]:
    """The OBUs of a temporal unit in the low-overhead format (sizes inline), each rewritten without its size field.

    Temporal delimiters and padding are dropped: the RTP format carries neither.
    """
    out: list[Obu] = []
    at = 0
    while at < len(data):
        head = data[at]
        if head & 0x80:
            raise PayloadError("AV1 OBU with the forbidden bit set")
        kind = (head >> 3) & 0x0F
        has_ext = bool(head & 0x04)
        has_size = bool(head & 0x02)
        header_len = 1 + (1 if has_ext else 0)
        if at + header_len > len(data):
            raise PayloadError("truncated AV1 OBU header")
        if has_size:
            size, used = read_leb128(data, at + header_len)
            start = at + header_len + used
        else:
            size, start = len(data) - at - header_len, at + header_len
        if start + size > len(data):
            raise PayloadError("an AV1 OBU runs past the end of its sample")
        if kind not in (OBU_TEMPORAL_DELIMITER, OBU_PADDING):
            header = bytes([head & ~0x02]) + (data[at + 1 : at + 2] if has_ext else b"")
            out.append(Obu(kind, header + data[start : start + size]))
        at = start + size
    return out


def packetize_av1(obus: list[Obu], new_sequence: bool, max_payload: int = MAX_PAYLOAD) -> list[bytes]:
    """OBU elements as RTP payloads. Every element carries a length prefix (W = 0) and may be split across packets
    (the Z and Y bits). `new_sequence` sets N on the first packet: the unit starts a coded video sequence."""
    packets: list[list] = []  # [z, y, [element bytes], used]
    cur: list = [False, False, [], 1]

    def flush() -> None:
        nonlocal cur
        if cur[2]:
            packets.append(cur)
        cur = [False, False, [], 1]

    for obu in obus:
        element, off = obu.element, 0
        while off < len(element):
            room = max_payload - cur[3]
            remaining = len(element) - off
            if remaining + len(leb128(remaining)) <= room:
                cur[2].append(element[off:])
                cur[3] += remaining + len(leb128(remaining))
                break
            if room < 16:
                flush()
                continue
            chunk = room - len(leb128(room))
            while chunk + len(leb128(chunk)) > room:
                chunk -= 1
            cur[2].append(element[off : off + chunk])
            cur[1] = True
            flush()
            cur[0] = True  # the next packet continues this element
            off += chunk
    flush()
    out = []
    for k, (z, y, elements, _used) in enumerate(packets):
        head = (0x80 if z else 0) | (0x40 if y else 0) | (0x08 if new_sequence and k == 0 else 0)
        out.append(bytes([head]) + b"".join(leb128(len(e)) + e for e in elements))
    return out


# -- Opus (RFC 7587) -----------------------------------------------------------------------------------------------


def opus_samples(packet: bytes) -> int:
    """How many 48 kHz samples one Opus packet holds (from its table-of-contents byte)."""
    if not packet:
        raise PayloadError("empty Opus packet")
    toc = packet[0]
    config = toc >> 3
    if config < 12:
        tenth_ms = (100, 200, 400, 600)[config % 4]
    elif config < 16:
        tenth_ms = (100, 200)[config % 2]
    else:
        tenth_ms = (25, 50, 100, 200)[config % 4]
    code = toc & 3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        if len(packet) < 2:
            raise PayloadError("truncated Opus packet")
        frames = packet[1] & 0x3F
    return frames * tenth_ms * 48 // 10


# -- configuration records -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AvcConfig:
    length_size: int
    sps: tuple[bytes, ...]
    pps: tuple[bytes, ...]
    profile_level_id: str  # the six hex digits of the SDP `profile-level-id`

    def parameter_sets(self) -> list[bytes]:
        return [*self.sps, *self.pps]


def parse_avcc(record: bytes) -> AvcConfig:
    if len(record) < 7 or record[0] != 1:
        raise PayloadError("not an avcC record")
    length_size = (record[4] & 3) + 1
    at = 5
    sets: list[list[bytes]] = []
    for mask in (0x1F, 0xFF):
        if at >= len(record):
            raise PayloadError("truncated avcC record")
        count = record[at] & mask
        at += 1
        units = []
        for _ in range(count):
            if at + 2 > len(record):
                raise PayloadError("truncated avcC record")
            size = int.from_bytes(record[at : at + 2], "big")
            at += 2
            if at + size > len(record):
                raise PayloadError("truncated avcC record")
            units.append(record[at : at + size])
            at += size
        sets.append(units)
    if not sets[0] or not sets[1]:
        raise PayloadError("the avcC record has no SPS or no PPS")
    return AvcConfig(length_size, tuple(sets[0]), tuple(sets[1]), record[1:4].hex())


@dataclass(frozen=True)
class HevcConfig:
    length_size: int
    vps: tuple[bytes, ...]
    sps: tuple[bytes, ...]
    pps: tuple[bytes, ...]
    profile_id: int
    tier_flag: int
    level_id: int

    def parameter_sets(self) -> list[bytes]:
        return [*self.vps, *self.sps, *self.pps]


def parse_hvcc(record: bytes) -> HevcConfig:
    if len(record) < 23 or record[0] != 1:
        raise PayloadError("not an hvcC record")
    profile_id, tier = record[1] & 0x1F, (record[1] >> 5) & 1
    level_id = record[12]
    length_size = (record[21] & 3) + 1
    at = 23
    found: dict[int, list[bytes]] = {}
    for _ in range(record[22]):
        if at + 3 > len(record):
            raise PayloadError("truncated hvcC record")
        kind = record[at] & 0x3F
        count = int.from_bytes(record[at + 1 : at + 3], "big")
        at += 3
        for _ in range(count):
            if at + 2 > len(record):
                raise PayloadError("truncated hvcC record")
            size = int.from_bytes(record[at : at + 2], "big")
            at += 2
            if at + size > len(record):
                raise PayloadError("truncated hvcC record")
            found.setdefault(kind, []).append(record[at : at + size])
            at += size
    if not (found.get(32) and found.get(33) and found.get(34)):
        raise PayloadError("the hvcC record lacks a VPS, SPS or PPS")
    return HevcConfig(length_size, tuple(found[32]), tuple(found[33]), tuple(found[34]), profile_id, tier, level_id)


@dataclass(frozen=True)
class Av1Config:
    profile: int
    level_idx: int
    tier: int
    sequence_header: bytes = field(default=b"", repr=False)  # the configOBUs: normally one sequence header OBU


def parse_av1c(record: bytes) -> Av1Config:
    if len(record) < 4 or record[0] != 0x81:
        raise PayloadError("not an av1C record")
    obus = split_obus(record[4:]) if len(record) > 4 else []
    header = b"".join(o.element for o in obus if o.kind == OBU_SEQUENCE_HEADER)
    return Av1Config(record[1] >> 5, record[1] & 0x1F, record[2] >> 7, header)


def has_obu(data: bytes, kind: int) -> bool:
    return any(o.kind == kind for o in split_obus(data))
