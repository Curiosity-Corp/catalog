"""Whether this host can publish over WHIP: the optional `streamstack[whip]` extra (aiortc) is installed and works."""

from __future__ import annotations

import importlib
import importlib.metadata

EXTRA_HINT = "install the whip extra: pip install 'streamstack[whip]'"
# The pieces of aiortc this package builds on. aiortc has been API-stable for years but a few of them are
# underscore names; `available()` checks for every one of them, so a future aiortc that renames one reports
# "unavailable" with a reason instead of failing in the middle of a stream.
REQUIRED = (
    ("aiortc.rtcicetransport", ("RTCIceGatherer", "RTCIceTransport", "RTCIceParameters", "RTCIceCandidate")),
    ("aiortc.rtcdtlstransport", ("RTCCertificate", "RTCDtlsTransport", "RTCDtlsParameters")),
    ("aiortc.rtcrtpsender", ("RTCRtpSender", "RTCEncodedFrame")),
    ("aiortc.rtcrtpparameters", ("RTCRtpSendParameters", "RTCRtpCodecParameters", "RTCRtcpParameters")),
    ("aiortc.mediastreams", ("MediaStreamTrack", "MediaStreamError")),
    ("aiortc.sdp", ("SessionDescription", "candidate_from_sdp", "candidate_to_sdp")),
)
PRIVATE_METHODS = (
    ("aiortc.rtcrtpsender", "RTCRtpSender", ("_next_encoded_frame", "_send_keyframe", "_handle_rtcp_packet")),
    ("aiortc.rtcdtlstransport", "RTCDtlsTransport", ("_send_rtp", "_set_role", "_register_rtp_sender")),
)


def version() -> str:
    try:
        return importlib.metadata.version("aiortc")
    except importlib.metadata.PackageNotFoundError:
        return ""


def check() -> str:
    """Empty when WHIP publishing works here, otherwise the reason it does not."""
    try:
        importlib.import_module("aiortc")
    except ImportError as exc:
        name = getattr(exc, "name", "") or "aiortc"
        if name == "aiortc":
            return f"aiortc is not installed ({EXTRA_HINT})"
        return f"aiortc cannot load because {name} is missing or broken ({EXTRA_HINT})"
    for module_name, names in REQUIRED:
        module = importlib.import_module(module_name)
        missing = [n for n in names if not hasattr(module, n)]
        if missing:
            return f"aiortc {version() or '?'} has no {module_name}.{missing[0]}; this StreamStack needs aiortc 1.15"
    for module_name, class_name, methods in PRIVATE_METHODS:
        cls = getattr(importlib.import_module(module_name), class_name)
        missing = [m for m in methods if not hasattr(cls, m)]
        if missing:
            return f"aiortc {version() or '?'} changed {class_name}.{missing[0]}; this StreamStack needs aiortc 1.15"
    try:
        from aiortc.rtcdtlstransport import RTCCertificate

        RTCCertificate.generateCertificate()
        import pylibsrtp

        pylibsrtp.Policy(key=bytes(30), ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND)
    except Exception as exc:  # a broken OpenSSL or libsrtp must read as "unavailable", whatever it raises
        return f"WebRTC crypto does not work here ({type(exc).__name__}: {exc})"
    return ""


def available() -> bool:
    return not check()
