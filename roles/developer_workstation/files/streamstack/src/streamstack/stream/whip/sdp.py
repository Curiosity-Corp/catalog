"""The SDP of a WHIP exchange: the offer StreamStack sends and a strict reader for the answer.

The offer is `sendonly`, BUNDLEd (audio and video on one ICE and DTLS transport) and complete: it already holds every
candidate, so the server needs no trickle. It names the codec the encoder really produces (H.264 with the real
profile-level-id, HEVC, AV1) and Opus, nothing else. The answer is read without aiortc's parser, which asserts
instead of reporting, so every problem with a server's answer becomes a message a person can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

AUDIO_PT = 111
VIDEO_PT = 96
MID_URI = "urn:ietf:params:rtp-hdrext:sdes:mid"
MID_EXT_ID = 1


class SdpError(ValueError):
    """The server's answer cannot be used; the message says why."""


@dataclass(frozen=True)
class CodecSpec:
    kind: str  # audio | video
    name: str  # H264 | H265 | AV1 | opus
    clock_rate: int
    payload_type: int
    channels: int = 0
    fmtp: str = ""
    feedback: tuple[str, ...] = ()

    @property
    def mime(self) -> str:
        return f"{self.kind}/{self.name}"


OPUS = CodecSpec("audio", "opus", 48000, AUDIO_PT, 2, "minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1")
VIDEO_FEEDBACK = ("nack", "nack pli", "ccm fir")


def h264_spec(profile_level_id: str) -> CodecSpec:
    fmtp = f"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id={profile_level_id}"
    return CodecSpec("video", "H264", 90000, VIDEO_PT, 0, fmtp, VIDEO_FEEDBACK)


def hevc_spec(profile_id: int, tier_flag: int, level_id: int) -> CodecSpec:
    fmtp = f"profile-id={profile_id};tier-flag={tier_flag};level-id={level_id};tx-mode=SRST"
    return CodecSpec("video", "H265", 90000, VIDEO_PT, 0, fmtp, VIDEO_FEEDBACK)


def av1_spec(profile: int, level_idx: int, tier: int) -> CodecSpec:
    return CodecSpec(
        "video", "AV1", 90000, VIDEO_PT, 0, f"profile={profile};level-idx={level_idx};tier={tier}", VIDEO_FEEDBACK
    )


@dataclass(frozen=True)
class MediaOffer:
    codec: CodecSpec
    mid: str
    ssrc: int


def build_offer(
    *,
    ufrag: str,
    password: str,
    fingerprints: list[tuple[str, str]],
    candidates: list[str],
    media: list[MediaOffer],
    cname: str,
    session_id: int,
    stream_id: str = "streamstack",
) -> str:
    """A complete WHIP offer. `candidates` are candidate attribute values without the leading `candidate:`."""
    if not media:
        raise SdpError("an offer needs at least one media section")
    lines = [
        "v=0",
        f"o=- {session_id} 2 IN IP4 127.0.0.1",
        "s=StreamStack",
        "t=0 0",
        "a=group:BUNDLE " + " ".join(m.mid for m in media),
        f"a=msid-semantic: WMS {stream_id}",
    ]
    for item in media:
        codec = item.codec
        lines += [
            f"m={codec.kind} 9 UDP/TLS/RTP/SAVPF {codec.payload_type}",
            "c=IN IP4 0.0.0.0",
            "a=rtcp:9 IN IP4 0.0.0.0",
            f"a=ice-ufrag:{ufrag}",
            f"a=ice-pwd:{password}",
            *[f"a=fingerprint:{algorithm} {value}" for algorithm, value in fingerprints],
            "a=setup:actpass",
            f"a=mid:{item.mid}",
            f"a=extmap:{MID_EXT_ID} {MID_URI}",
            "a=sendonly",
            f"a=msid:{stream_id} {codec.kind}-{item.ssrc}",
            "a=rtcp-mux",
            f"a=rtpmap:{codec.payload_type} {codec.name}/{codec.clock_rate}"
            + (f"/{codec.channels}" if codec.channels else ""),
        ]
        lines += [f"a=rtcp-fb:{codec.payload_type} {fb}" for fb in codec.feedback]
        if codec.fmtp:
            lines.append(f"a=fmtp:{codec.payload_type} {codec.fmtp}")
        lines.append(f"a=ssrc:{item.ssrc} cname:{cname}")
        lines += [f"a=candidate:{c}" for c in candidates]
        lines.append("a=end-of-candidates")
    return "\r\n".join(lines) + "\r\n"


# -- the answer ----------------------------------------------------------------------------------------------------


@dataclass
class AnswerCodec:
    payload_type: int
    name: str
    clock_rate: int
    channels: int = 0
    fmtp: str = ""


@dataclass
class AnswerMedia:
    kind: str
    port: int
    mid: str = ""
    direction: str = ""
    codecs: list[AnswerCodec] = field(default_factory=list)
    ufrag: str = ""
    password: str = ""
    fingerprints: list[tuple[str, str]] = field(default_factory=list)
    setup: str = ""
    candidates: list[str] = field(default_factory=list)
    extmap: dict[int, str] = field(default_factory=dict)
    rtcp_mux: bool = False
    feedback: dict[int, list[str]] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.port != 0 and self.direction != "inactive" and bool(self.codecs)


@dataclass
class Answer:
    ice_lite: bool = False
    bundle: list[str] = field(default_factory=list)
    media: list[AnswerMedia] = field(default_factory=list)

    def section(self, kind: str) -> AnswerMedia | None:
        return next((m for m in self.media if m.kind == kind), None)


_M_LINE = re.compile(r"^m=(\w+) (\d+) (\S+) (.+)$")
_ATTR = re.compile(r"^a=([^:]+)(?::(.*))?$")


def parse_answer(text: str) -> Answer:
    """Parse an SDP answer. Raises `SdpError` for anything that is not usable."""
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines or lines[0] != "v=0":
        raise SdpError("the server's answer is not SDP (it does not start with v=0)")
    answer = Answer()
    session = AnswerMedia("session", 0)
    current: AnswerMedia | None = None
    seen_m = False
    for line in lines[1:]:
        match = _M_LINE.match(line)
        if match:
            kind, port, profile, _formats = match.groups()
            if "SAVPF" not in profile and "SAVP" not in profile:
                raise SdpError(f"the server's answer uses {profile}, not DTLS-SRTP")
            current = AnswerMedia(kind, int(port), ufrag=session.ufrag, password=session.password)
            current.fingerprints = list(session.fingerprints)
            current.setup = session.setup
            current.rtcp_mux = session.rtcp_mux
            answer.media.append(current)
            seen_m = True
            continue
        attr = _ATTR.match(line)
        if not attr:
            continue
        name, value = attr.group(1), (attr.group(2) or "").strip()
        target = current if seen_m and current is not None else session
        try:
            _apply(answer, target, name, value)
        except (ValueError, IndexError) as exc:
            raise SdpError(f"the server's answer has a malformed a={name} line ({exc})") from None
    return answer


def _apply(answer: Answer, media: AnswerMedia, name: str, value: str) -> None:
    if name == "ice-lite":
        answer.ice_lite = True
    elif name == "group":
        bits = value.split()
        if bits and bits[0] == "BUNDLE":
            answer.bundle = bits[1:]
    elif name == "ice-ufrag":
        media.ufrag = value
    elif name == "ice-pwd":
        media.password = value
    elif name == "fingerprint":
        algorithm, digest = value.split(None, 1)
        media.fingerprints.append((algorithm.lower(), digest.strip().upper()))
    elif name == "setup":
        media.setup = value
    elif name == "mid":
        media.mid = value
    elif name == "candidate":
        media.candidates.append(value)
    elif name == "rtcp-mux":
        media.rtcp_mux = True
    elif name in ("sendonly", "recvonly", "sendrecv", "inactive"):
        media.direction = name
    elif name == "extmap":
        ident, uri = value.split(None, 1)
        media.extmap[int(ident.split("/")[0])] = uri.split()[0]
    elif name == "rtpmap":
        pt, desc = value.split(None, 1)
        bits = desc.split("/")
        media.codecs.append(
            AnswerCodec(
                int(pt), bits[0], int(bits[1]), int(bits[2]) if len(bits) > 2 else (1 if media.kind == "audio" else 0)
            )
        )
    elif name == "fmtp":
        pt, params = value.split(None, 1)
        for codec in media.codecs:
            if codec.payload_type == int(pt):
                codec.fmtp = params
    elif name == "rtcp-fb":
        pt, feedback = value.split(None, 1)
        media.feedback.setdefault(int(pt) if pt != "*" else -1, []).append(feedback)


def negotiate(answer: Answer, offered: list[MediaOffer]) -> dict[str, AnswerMedia]:
    """Check the answer against the offer. Returns the accepted answer section for each offered media kind.

    Raises `SdpError` when the server refused a section, changed the codec, did not bundle, or left out what a
    connection needs (ICE credentials, a DTLS fingerprint).
    """
    if not answer.media:
        raise SdpError("the server's answer has no media sections")
    if len(offered) > 1 and set(answer.bundle) != {m.mid for m in offered}:
        raise SdpError("the server did not bundle audio and video on one connection (BUNDLE), which WHIP requires")
    out: dict[str, AnswerMedia] = {}
    for item in offered:
        section = next((m for m in answer.media if m.mid == item.mid), None) or answer.section(item.codec.kind)
        if section is None or not section.accepted:
            raise SdpError(f"the server did not accept the {item.codec.kind} (it answered with port 0 or no codec)")
        chosen = next(
            (
                c
                for c in section.codecs
                if c.name.lower() == item.codec.name.lower() and c.clock_rate == item.codec.clock_rate
            ),
            None,
        )
        if chosen is None:
            names = ", ".join(sorted({c.name for c in section.codecs}))
            raise SdpError(
                f"the server did not accept {item.codec.name} for the {item.codec.kind} (it answered with {names})"
            )
        if chosen.payload_type != item.codec.payload_type:
            raise SdpError(f"the server changed the {item.codec.kind} payload type, which a sender cannot do")
        out[item.codec.kind] = section
    first = next(iter(out.values()))
    for kind, section in out.items():
        if not section.ufrag or not section.password:
            raise SdpError(f"the server's {kind} section has no ICE credentials")
        if not section.fingerprints:
            raise SdpError(f"the server's {kind} section has no DTLS fingerprint")
        if (section.ufrag, section.password) != (first.ufrag, first.password):
            raise SdpError("the server answered with different ICE credentials for audio and video")
    if first.setup not in ("active", "passive"):
        raise SdpError(f"the server's DTLS role is {first.setup or 'missing'}; an answer must be active or passive")
    return out


def candidate_values(answer: Answer) -> list[str]:
    """The distinct candidate attribute values in the answer (all media, in order)."""
    seen: dict[str, None] = {}
    for media in answer.media:
        for c in media.candidates:
            seen.setdefault(c, None)
    return list(seen)
