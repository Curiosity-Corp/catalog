# ADR 0001: Encoder generations, a Matroska relay, and the transports

Status: accepted (2026-10-23); section 5 superseded by ADR 0002. Issues #62, #63, #64.

## Context

StreamStack records and streams from itself, with no middleware. Input is the program picture
(`/dev/video43` or any v4l2 device) and the program audio (a PipeWire source). Output is a local recording, SRT, Enhanced
RTMP (AV1 and HEVC, with classic H.264 as the fallback) and WHIP.

The owner wants bitrate, keyframe interval, rate control, resolution tier, fps and the encoder itself to change while
the stream is live. The host tools are ffmpeg 8.0.1 (Ubuntu; VA-API, QSV, SVT-AV1, x264, x265, libopus, SRT, enhanced
RTMP) and Python 3.14. This host has an Intel Arc A380; the code must also work on NVIDIA and AMD, so every choice is made
by a capability probe and not by a vendor name.

## Decision

### 1. One ffmpeg per encoder "generation", cut at a keyframe by a Python relay

Every encoder is its own ffmpeg process: capture, filter, encode, Matroska on stdout. All generations stamp frames on
one wall-clock axis (`setpts=(PTS*TB-E)/TB`, with one epoch `E` per stream). A relay (`relay.py`: `Chain`, `TrackSwitch`)
reads the Matroska, and hands the sinks the bytes of whichever generation is active. To change an encoder setting:

1. Pick a cut time `CUT` a little ahead of now (1.0 s for hardware, 1.6 s for software).
2. Start a new generation with `-force_key_frames expr:gte(t,CUT+n_forced*KEYINT)` and the new settings.
3. When its first keyframe at or after `CUT` arrives, the relay switches to it and drops the old generation's later
   blocks. Sinks stay connected; the player sees an ordinary keyframe boundary.
4. The old generation is stopped.

If the keyframe is late (a busy host), the swap is retried once with more lead. If that also fails, the old encoder
keeps running and the Console gets a warning; the stream is never left without an encoder.

When the source device can have more than one reader (v4l2loopback, or lavfi) the new encoder runs beside the old one
(make-before-break). A plain v4l2 device usually has one reader, so there the old encoder is stopped and the new one
started (break-before-make), which costs a short gap (the new encoder's start-up, typically under a second, ending on a
keyframe). The probe reports which case applies (`source_info`).

### 2. Alternatives rejected

- **ffmpeg `zmq` / `sendcmd`**: they reach filters, not the encoder's bitrate, rate control or keyframe interval. Only
  `scale` and similar could change live. Rejected.
- **PyAV, in process**: the wheel's bundled ffmpeg has no VA-API, QSV or NVENC, so it would encode on the CPU. Rejected.
- **GStreamer**: no `webrtc` plugin on this host, and its VA plugin set lags ffmpeg's. Rejected.
- **MPEG-TS between the encoder and the relay**: ffmpeg cannot demux AV1 out of MPEG-TS. Matroska carries AV1, HEVC,
  H.264 and Opus alike, and a streaming Matroska reader is under 400 lines (`mkv.py`).

Matroska over a pipe with `-cluster_time_limit 1 -flush_packets 1` costs 50 to 60 ms from capture to relay for H.264,
and is the latency floor of this design (measured, see docs/stream.md).

### 3. What changes live and what does not

| Change | Class | What happens |
|---|---|---|
| bitrate, rate control, keyframe interval, quality, encoder | seamless | new generation, keyframe-aligned cut, no reconnect |
| audio bitrate | audio | a new Opus encoder takes over at the first block newer than the last one forwarded; at most one frame lost, a faint click is possible |
| resolution tier, fps | geometry | the picture format changes, so every sink reconnects once; recordings start a new segment file (`name-002.mkv`) |
| codec | codec | the destination moves to another chain (a second encode if two codecs are needed) and reconnects |

Resolution and fps do not hot-swap because an RTMP or WHIP ingest and a decoder cannot be assumed to survive a change of
frame size in-band. SRT over MPEG-TS can in theory carry it, but this was not verified, so SRT reconnects like the rest.
The reconnect starts on a keyframe. This is documented as a restart in docs/stream.md.

### 4. Codec planning

`plan_chains` picks the codec subset that serves the most destinations and then the best quality, with at most
`STREAMSTACK_STREAM_MAX_ENCODES` encodes at once (default 2, capped at 3). Order of preference is AV1, HEVC, H.264, then
software. Per kind: SRT takes HEVC or H.264 (AV1 only when asked), classic RTMP H.264, Enhanced RTMP AV1/HEVC/H.264,
WHIP H.264 (HEVC or AV1 when named, ADR 0002), recording anything. An Enhanced RTMP destination in `auto` mode that cannot connect twice in a row, and has
never been live, falls back to the H.264 chain. Mode `enhanced` never falls back.

### 5. WHIP

Superseded by [ADR 0002](0002-whip-publishing.md). This section first made WHIP a probe result (ffmpeg 8.0.1 on Ubuntu
has no `whip` muxer) and rejected aiortc on the belief that it could not forward already-encoded packets; it can, and
StreamStack now publishes WHIP itself, H.264 by default and HEVC or AV1 when the destination asks, with no ffmpeg muxer.

### 6. Stream keys

A destination stores a reference, never a key: `env:NAME`, `file:/path`, `cfg:KEY` or `cmd:argv`. The service resolves it
at connect time. The key reaches ffmpeg in the sink's command line (SRT and RTMP; the WHIP token never does), so `ps` shows it to other local users; the logs and
the Console state redact it, and the state shows only `"key": "set"|"none"`. Mitigation: mount `/proc` with `hidepid=2`
(documented in docs/stream.md). Azure Key Vault holds the real values.

## Consequences

- Several settings change with no visible interruption; the cost is a second encoder for about a second, which a busy
  host must afford (the probe's software check refuses CPU encodes that cannot keep up).
- AV1 has about 100 ms more capture-to-relay latency than HEVC and H.264 on this driver (measured). The planner still
  prefers AV1 for Enhanced RTMP because the bitrate saving matters more than 100 ms to a platform with seconds of
  buffering; pin `codec: hevc` for low-latency SRT.
- ffmpeg is a hard dependency, as before; nothing else is.
