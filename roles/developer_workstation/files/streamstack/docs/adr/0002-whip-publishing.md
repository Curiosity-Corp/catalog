# ADR 0002: WHIP publishing with a native client on aiortc's transports

Status: accepted (2026-10-24). Issue #108 (radar item 40). Replaces section 5 of
[ADR 0001](0001-encoder-generations-and-stream-transports.md).

## Context

ADR 0001 shipped WHIP (RFC 9725, WebRTC-HTTP ingestion) as a probe result: "unavailable" on any host whose ffmpeg has
no `whip` muxer. Ubuntu's ffmpeg 8.0.1 has none (measured: `Unknown format 'whip'`), GStreamer 1.28.2 here has no
`whipclientsink`, and even where the muxer exists it publishes H.264 only and cannot be told to reconnect, report RTT
or loss, or hot-swap an encoder. Self-hosted ingests (MediaMTX, LiveKit, Cloudflare Stream, Janus, SRS) and browsers'
WHEP players make WHIP the one sub-second route StreamStack can offer without a platform's blessing.

The requirements, from the owner's direction: real hardware encode (the Arc A380 encodes H.264, HEVC and AV1; it cannot
decode AV1), no re-encode, no OBS or other middleware, the same live controls as SRT and RTMP (bitrate, keyframe
interval, encoder swap with no reconnect), reconnect with backoff, stats a person can read (bitrate, RTT, loss, ICE
state), and bearer tokens handled like stream keys.

## Decision

### 1. A native WHIP client in `streamstack/stream/whip/`, built on aiortc's transport classes

The relay already hands every sink the Matroska of the live encoder generation. A `WhipSink` is one more sink: it
demuxes that Matroska, packetizes the **already-encoded** access units into RTP payloads (RFC 6184 H.264, RFC 7798
HEVC, the AV1 RTP payload format with its aggregation header, RFC 7587 Opus), and sends them over an ICE and DTLS-SRTP
connection it negotiates itself with the HTTP exchange of RFC 9725. Nothing is decoded or encoded again; the GPU
encoder's output is the media on the wire.

Layers (each file does one thing, and each is unit-tested without a network):

| Module | Job |
|---|---|
| `payload.py` | RTP payload formats; `length_prefixed`, `parse_avcc/hvcc/av1c` for the Matroska codec private data |
| `sdp.py` | the offer we send (a template, not a general SDP writer) and a strict answer parser and negotiator |
| `protocol.py` | HTTP: POST offer, PATCH (trickle), DELETE, Location and ETag, Link headers, redirects, error classes |
| `media.py` | Matroska blocks to RTP frames: parameter sets before keyframes, the 90 kHz and 48 kHz clocks |
| `session.py` | ICE, DTLS, SRTP via aiortc; the pass-through sender; stats; the watchdog; ordered teardown |
| `sink.py` | the relay sink: own thread and event loop, GOP buffer while connecting, backoff, fatal states |
| `support.py` | the probe: is aiortc importable, does it expose the internals we use, does its crypto work |

aiortc is used for what is hard and well tested (an ICE agent, DTLS with pyOpenSSL, SRTP via libsrtp, RTCP sender
reports, NACK retransmission, receiver-report statistics) and not for what would force a re-encode.

### 2. Why not the alternatives

- **aiortc's `RTCPeerConnection` with a media track.** Its sender takes decoded frames and encodes them itself (libx264
  or libvpx on the CPU), negotiates H.264 baseline only, and has no HEVC or AV1. ADR 0001 said aiortc "cannot forward
  already-encoded packets". That was wrong: `RTCRtpSender` asks one method, `_next_encoded_frame`, for the payloads of
  the next frame, and a subclass that returns ours skips the encoder entirely. This ADR is built on that; section 6
  says what it costs.
- **ffmpeg's `whip` muxer.** Absent on this build, H.264 and Opus only, no stats, no reconnect policy of ours, the token
  on the command line.
- **GStreamer `whipclientsink`.** Absent here (it needs the Rust webrtc plugin set); adding a second media framework to
  a project whose only hard dependency is ffmpeg is the wrong direction.
- **A Go or Rust helper (pion, webrtc-rs, `whip-go`).** A second runtime to build, sign, ship and keep patched, and a
  process boundary for the hot-swap and the stats. aiortc is pure Python on libsrtp and pyOpenSSL, optional, and already
  at the right abstraction.
- **MediaMTX or another relay as middleware.** The project rule is no middleware. MediaMTX is the test server only.

### 3. Codecs

H.264 is the default (every ingest takes it). HEVC and AV1 are offered only when the destination names them
(`codec: hevc` or `av1`); `auto` never picks them, because most WHIP servers accept H.264 and Opus and nothing else, and
a refused codec would cost a connection attempt. The offer carries the codec's real parameters, read from the
encoder's own configuration: H.264 `profile-level-id` from the avcC record, HEVC `profile-id`, `tier-flag`, `level-id`
from the hvcC record, AV1 `profile`, `level-idx`, `tier` from the av1C record. SPS and PPS (VPS for HEVC, the sequence
header for AV1) are put in front of every keyframe unless the encoder already did, so a server that joins at any
keyframe decodes. Opus is `minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1`, packetized as it comes (20 ms).

If the relay swaps to an encoder of the same codec (a bitrate, rate-control or encoder change), the session continues
and the packetizer takes the new parameter sets from the next keyframe: no reconnect, no renegotiation. A change of
picture size or codec cannot continue an SDP that named the old ones, so those reconnect once, like every other
destination (ADR 0001, section 3).

### 4. The protocol, and what is strict

- POST `application/sdp` with `Authorization: Bearer <token>` when one is set; 201 with `Location`; the answer is
  required to be SDP. `ETag` and `Accept-Patch` are recorded. `Link` headers with `rel="ice-server"` are parsed.
- DELETE to the `Location` on stop, sent **before** the transports close so the server frees the session at once
  instead of timing it out. The close order is: stop the tasks, RTCP BYE, DELETE (3 s limit), DTLS, ICE.
- Redirects: followed up to three times; the token is sent only to the
  origin it was configured for; an https to http redirect is refused.
- Status handling decides what is retried. **Fatal** (the destination goes to `failed` and stays there until edited):
  400, 401, 403, 404, 405, 406, 415, 422, 501, an answer that is not valid SDP or does not accept the codec offered, no
  `Location`, a token source that cannot be read. **Retried with backoff** (1, 2, 4, 8, 15, 30 s; reset after 30 s of
  a connection that held): network errors, 409, 429, 5xx (`Retry-After` is honoured, capped at 60 s), ICE or DTLS
  failure or timeout, and a session that dies.
- A session is dead when ICE or DTLS fail or close, when aiortc's consent check expires (about 30 s of silence from
  the server), or when RTCP, once the server has been sending it, stops for 10 s.
- The token never appears in an error, a log, the state or an event: URLs in errors are reduced to scheme, host and
  path, and a server that echoes the token back in its error body has it scrubbed.

### 5. What we deliberately did not do, said plainly

- **Trickle ICE is implemented in the protocol client and tested (PATCH with the ETag and `If-Match`, 412 and 405 and
  501 handling) but not used by sessions.** aiortc's ICE agent gathers all candidates before it starts and cannot
  restart, so a session gathers first and sends a complete offer with `a=end-of-candidates`. Gathering on loopback and
  LAN takes milliseconds; the cost is on hosts that wait for STUN, and there is no STUN server by default (below). The
  PATCH path is exercised against MediaMTX in the interop test so that when the ICE agent can trickle, the client
  half is already proven.
- **No STUN or TURN by default.** Using a public STUN server would tell a third party that this machine is about to
  stream, which a privacy-first tool must not do unasked. Servers that are ICE-lite or have a public address (every
  ingest we know of) are reached from host candidates, and the server learns our reflexive address from the checks.
  Servers offered in `Link` headers are parsed and recorded in the session state, not used for gathering. `ice_servers`
  is a constructor argument for the day a TURN-only ingest appears.
- **No packet pacing.** aiortc sends a frame's packets back to back, so a keyframe leaves as a burst. On a LAN or a
  server with a large receive buffer that is harmless; on a thin uplink it is not. Rate control is the encoder's CBR.
- **No PLI back-pressure on the encoder.** A server's PLI or FIR is counted, logged (at most once per 10 s) and shown;
  the encoder is not signalled, because the relay cuts generations at the planned keyframe and `keyint_s` bounds the
  wait. A server that needs a keyframe now waits at most one interval (default 2 s).
- **No simulcast, no SVC, no RTX negotiation beyond what aiortc retransmits on NACK, no transport-wide congestion
  control.** The bitrate stays where the person put it.
- **Not proven against a platform's production ingest.** The interop evidence is MediaMTX 1.21.2 (a Go ICE-lite server)
  for H.264, HEVC and AV1, and aiortc itself as an independent implementation. Cloudflare, LiveKit, Janus, SRS and the
  large platforms were not tried (no accounts, and no confirmation that the large platforms accept WHIP at all).

### 6. aiortc internals and the version cap

The pass-through sender overrides `_next_encoded_frame`, `_send_keyframe` and `_handle_rtcp_packet`, which are private.
`support.check()` runs on the probe: it imports aiortc, checks that these methods and the transport classes exist with
the attributes we use, and round-trips a DTLS certificate and a fingerprint. If aiortc changes underneath us, the
probe says "WHIP unavailable: <what changed>" instead of the session failing in the middle of a show. The extra pins
`aiortc>=1.15.0`; aiortc 1.15.0 requires `av<18`, so pip resolves PyAV 17.1.0 even though 19.0.2 exists. PyAV is only
used by aiortc's own codecs, which this client never calls. When aiortc lifts the cap we take the new PyAV with it.

### 7. Where it runs, and the secrets

A `WhipSink` owns one thread with its own asyncio loop. The relay thread calls `write()`, which demuxes, packetizes
and queues in microseconds and never blocks (a video queue that cannot drain is dropped up to the next keyframe, since a receiver cannot use the frames in between; audio drops its oldest frame; both are counted). Teardown futures
are awaited outside the sink's lock, because the loop thread takes the same lock.

The bearer token is a `key_ref` like any stream key: stored write-only in `<state_dir>/keys/<id>` (0700 directory, 0600
file) and resolved at connect time, in memory. Unlike ffmpeg-based sinks it is **not on a command line**: the `ps`
exposure of ADR 0001, section 6 does not apply to WHIP.

## Consequences

- WHIP works on this host and any other with `pip install 'streamstack[whip]'`. Without the extra the probe says so,
  with the install line; nothing else changes. The core wheel still has no required Python dependencies.
- A WHIP destination costs about 2 to 3 % of one core for the whole path at 1080p30 and about 1 ms of latency (see
  docs/stream.md for the measurements and what they exclude).
- StreamStack now owns an RTP packetizer for three codecs. It is covered by tests that reassemble the packets the way a
  receiver does, by decoding real encoder output through aiortc's own receiver, and through MediaMTX and ffmpeg for
  HEVC and AV1. A mistake in a payload format would show there, and not as a black picture on a customer's stream.
- Reviewing aiortc's private-method use is a standing chore on every aiortc bump; the probe is the gate.
- Reopen this decision if aiortc stalls (it has had slow periods), if a platform requires TURN or simulcast, or if
  trickle ICE becomes necessary for connection time.
