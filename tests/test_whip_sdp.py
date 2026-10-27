"""The WHIP offer StreamStack sends and the strictness of the answer it accepts."""

from __future__ import annotations

import pytest

from streamstack.stream.whip import sdp

FINGERPRINT = "AA:BB:CC:DD"
CANDIDATE = "1 1 UDP 2130706431 192.0.2.7 5004 typ host"


def offer(codec: sdp.CodecSpec | None = None, audio: bool = True) -> tuple[str, list[sdp.MediaOffer]]:
    media = [sdp.MediaOffer(codec or sdp.h264_spec("64001f"), "0", 1111)]
    if audio:
        media.append(sdp.MediaOffer(sdp.OPUS, "1", 2222))
    text = sdp.build_offer(
        ufrag="uf",
        password="pw" * 12,
        fingerprints=[("sha-256", FINGERPRINT)],
        candidates=[CANDIDATE],
        media=media,
        cname="ss-abc",
        session_id=42,
    )
    return text, media


def answer_for(media: list[sdp.MediaOffer], **kw: str) -> str:
    """What a conforming server answers: recvonly, bundled, ice-lite, DTLS active."""
    lines = [
        "v=0",
        "o=- 1 1 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "a=ice-lite",
        "a=group:BUNDLE " + " ".join(m.mid for m in media),
    ]
    for m in media:
        c = m.codec
        lines += [
            f"m={c.kind} 9 UDP/TLS/RTP/SAVPF {c.payload_type}",
            "c=IN IP4 0.0.0.0",
            f"a=mid:{m.mid}",
            "a=recvonly",
            "a=rtcp-mux",
            f"a=ice-ufrag:{kw.get('ufrag', 'srv')}",
            f"a=ice-pwd:{kw.get('pwd', 'srvpassword')}",
            f"a=fingerprint:sha-256 {kw.get('fp', 'aa:bb:cc')}",
            f"a=setup:{kw.get('setup', 'active')}",
            f"a=rtpmap:{c.payload_type} {c.name}/{c.clock_rate}" + (f"/{c.channels}" if c.channels else ""),
            f"a=extmap:1 {sdp.MID_URI}",
            "a=candidate:1 1 UDP 100 198.51.100.4 8189 typ host",
        ]
    return "\r\n".join(lines) + "\r\n"


class TestOffer:
    def test_it_is_a_sendonly_bundle_with_complete_candidates(self) -> None:
        text, _ = offer()
        assert text.startswith("v=0\r\n") and text.endswith("\r\n")
        assert text.count("m=video 9 UDP/TLS/RTP/SAVPF 96") == 1 and text.count("m=audio 9 UDP/TLS/RTP/SAVPF 111") == 1
        assert "a=group:BUNDLE 0 1" in text and text.count("a=sendonly") == 2 and text.count("a=rtcp-mux") == 2
        assert text.count("a=setup:actpass") == 2 and f"a=fingerprint:sha-256 {FINGERPRINT}" in text
        assert text.count(f"a=candidate:{CANDIDATE}") == 2 and text.count("a=end-of-candidates") == 2
        assert "a=ssrc:1111 cname:ss-abc" in text and "a=ssrc:2222 cname:ss-abc" in text

    def test_h264_names_the_real_profile(self) -> None:
        text, _ = offer(sdp.h264_spec("640032"))
        assert "a=rtpmap:96 H264/90000" in text
        assert "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032" in text

    def test_hevc_and_av1(self) -> None:
        text, _ = offer(sdp.hevc_spec(1, 0, 93))
        assert (
            "a=rtpmap:96 H265/90000" in text and "a=fmtp:96 profile-id=1;tier-flag=0;level-id=93;tx-mode=SRST" in text
        )
        text, _ = offer(sdp.av1_spec(0, 8, 0))
        assert "a=rtpmap:96 AV1/90000" in text and "a=fmtp:96 profile=0;level-idx=8;tier=0" in text

    def test_opus_is_stereo_with_fec(self) -> None:
        text, _ = offer()
        assert "a=rtpmap:111 opus/48000/2" in text and "useinbandfec=1" in text

    def test_video_asks_for_nack_and_pli(self) -> None:
        text, _ = offer()
        assert "a=rtcp-fb:96 nack\r\n" in text and "a=rtcp-fb:96 nack pli" in text and "a=rtcp-fb:111" not in text

    def test_video_only(self) -> None:
        text, _ = offer(audio=False)
        assert "m=audio" not in text and "a=group:BUNDLE 0\r\n" in text

    def test_an_empty_offer_is_refused(self) -> None:
        with pytest.raises(sdp.SdpError):
            sdp.build_offer(ufrag="u", password="p", fingerprints=[], candidates=[], media=[], cname="c", session_id=1)


class TestAnswer:
    def test_a_conforming_answer_is_accepted(self) -> None:
        _, media = offer()
        answer = sdp.parse_answer(answer_for(media))
        accepted = sdp.negotiate(answer, media)
        assert answer.ice_lite and answer.bundle == ["0", "1"]
        assert set(accepted) == {"video", "audio"} and accepted["video"].setup == "active"
        assert accepted["video"].ufrag == "srv" and accepted["video"].fingerprints == [("sha-256", "AA:BB:CC")]
        assert accepted["video"].extmap == {1: sdp.MID_URI}
        assert sdp.candidate_values(answer) == ["1 1 UDP 100 198.51.100.4 8189 typ host"]

    def test_session_level_attributes_apply_to_every_media(self) -> None:
        text = "\r\n".join(
            [
                "v=0", "o=- 1 1 IN IP4 0.0.0.0", "s=-", "t=0 0", "a=ice-ufrag:u", "a=ice-pwd:p",
                "a=fingerprint:sha-256 aa:bb", "a=setup:passive", "a=group:BUNDLE 0",
                "m=video 9 UDP/TLS/RTP/SAVPF 96", "a=mid:0", "a=recvonly", "a=rtpmap:96 H264/90000",
            ]
        )  # fmt: skip
        _, media = offer(audio=False)
        accepted = sdp.negotiate(sdp.parse_answer(text), media)
        assert accepted["video"].ufrag == "u" and accepted["video"].setup == "passive"

    def test_a_server_that_answers_passive_makes_us_the_dtls_server(self) -> None:
        _, media = offer()
        accepted = sdp.negotiate(sdp.parse_answer(answer_for(media, setup="passive")), media)
        assert accepted["video"].setup == "passive"

    @pytest.mark.parametrize("text", ["", "<html>", "m=video 9 RTP/AVP 96\r\n"])
    def test_not_sdp(self, text: str) -> None:
        with pytest.raises(sdp.SdpError, match="not SDP"):
            sdp.parse_answer(text)

    def test_plain_rtp_is_refused(self) -> None:
        with pytest.raises(sdp.SdpError, match="not DTLS-SRTP"):
            sdp.parse_answer("v=0\r\nm=video 9 RTP/AVP 96\r\n")

    def test_malformed_lines_say_which(self) -> None:
        with pytest.raises(sdp.SdpError, match="a=rtpmap"):
            sdp.parse_answer("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96\r\n")

    def test_a_refused_media_section(self) -> None:
        _, media = offer()
        text = answer_for(media).replace("m=audio 9", "m=audio 0")
        with pytest.raises(sdp.SdpError, match="did not accept the audio"):
            sdp.negotiate(sdp.parse_answer(text), media)

    def test_an_inactive_section_is_refused(self) -> None:
        _, media = offer(audio=False)
        text = answer_for(media).replace("a=recvonly", "a=inactive")
        with pytest.raises(sdp.SdpError, match="did not accept the video"):
            sdp.negotiate(sdp.parse_answer(text), media)

    def test_a_different_codec_is_named(self) -> None:
        _, media = offer()
        _, vp8 = offer(sdp.CodecSpec("video", "VP8", 90000, 96))
        with pytest.raises(sdp.SdpError, match=r"did not accept H264.*VP8"):
            sdp.negotiate(sdp.parse_answer(answer_for(vp8)), media)

    def test_a_changed_payload_type_is_refused(self) -> None:
        _, media = offer(audio=False)
        text = answer_for(media).replace("96", "100")
        with pytest.raises(sdp.SdpError, match="payload type"):
            sdp.negotiate(sdp.parse_answer(text), media)

    def test_no_bundle_is_refused(self) -> None:
        _, media = offer()
        text = answer_for(media).replace("a=group:BUNDLE 0 1\r\n", "")
        with pytest.raises(sdp.SdpError, match="BUNDLE"):
            sdp.negotiate(sdp.parse_answer(text), media)

    @pytest.mark.parametrize(
        ("remove", "message"),
        [("a=ice-pwd:srvpassword\r\n", "ICE credentials"), ("a=fingerprint:sha-256 aa:bb:cc\r\n", "fingerprint")],
    )
    def test_missing_connection_parameters(self, remove: str, message: str) -> None:
        _, media = offer(audio=False)
        with pytest.raises(sdp.SdpError, match=message):
            sdp.negotiate(sdp.parse_answer(answer_for(media).replace(remove, "")), media)

    def test_the_role_must_be_active_or_passive(self) -> None:
        _, media = offer(audio=False)
        with pytest.raises(sdp.SdpError, match="DTLS role"):
            sdp.negotiate(sdp.parse_answer(answer_for(media, setup="actpass")), media)

    def test_no_media_sections(self) -> None:
        _, media = offer()
        with pytest.raises(sdp.SdpError, match="no media"):
            sdp.negotiate(sdp.parse_answer("v=0\r\ns=-\r\n"), media)
