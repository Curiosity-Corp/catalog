"""WHIP (WebRTC-HTTP ingestion protocol, RFC 9725) publishing. See docs/adr/0002-whip-publishing.md.

`protocol` is the HTTP signalling, `sdp` the offer and answer, `payload` the RTP packetizers, `media` turns the
relay's Matroska into access units, `session` is one WebRTC connection (ICE, DTLS-SRTP, RTP, RTCP) built on aiortc's
transports, and `sink` is the relay sink the stream service attaches. Only `session` and `sink` need aiortc, which
is the optional extra `streamstack[whip]`; `available()` says whether it is installed and usable.
"""

from __future__ import annotations

from streamstack.stream.whip.support import available, version

__all__ = ["available", "version"]
