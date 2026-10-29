# Recording and streaming

StreamStack records and streams the program itself, with no other application. The code is in
`src/streamstack/stream/`; the Console adapter is `src/streamstack/console/stream.py`. The design and its trade-offs are
in [ADR 0001](adr/0001-encoder-generations-and-stream-transports.md) and, for WHIP,
[ADR 0002](adr/0002-whip-publishing.md).

```
v4l2 program picture --> ffmpeg (VA-API / QSV / NVENC / software) --\
                                                                      relay (Python) --> recording (.mkv / .mp4)
PipeWire program audio --> ffmpeg (Opus) ----------------------------/        |-------> SRT (caller or listener)
                                                                              |-------> RTMP (Enhanced, classic fallback)
                                                                              '-------> WHIP (StreamStack's own client: RTP over ICE and DTLS-SRTP)
```

## What works live and what restarts

| You change | Live? | What the viewer sees |
|---|---|---|
| `bitrate_kbps`, `rate_control`, `keyint_s`, `quality`, `encoder` | yes | nothing: a second encoder starts, the relay cuts to it at a planned keyframe, no reconnect |
| `audio_bitrate_kbps` | yes | at most one audio frame (20 ms); a faint click is possible |
| `tier`, `fps` | restart | every destination reconnects once (starting on a keyframe); a recording continues in a new file `name-002.mkv` |
| `codec` | restart for the destinations that move | those destinations move to another chain and reconnect |

The new encoder runs beside the old one only when the source can have two readers (v4l2loopback; the probe says so in
`source_info`). On a single-reader device the old encoder stops first and the new one starts, so a live change
costs a short gap (the start-up of one encoder, ending on a keyframe).

## Console actions

All are in `POST /api/action` form, `{"action": "...", "params": {...}}`, and return a one-line message.

| Action | Params | Effect |
|---|---|---|
| `stream.start` | `destinations?: [id]` | start the enabled live destinations, or the named ones |
| `stream.stop` | `destination?: id` | stop all live destinations, or one. Recording continues |
| `stream.set` | any of the settings below, `destinations?: [...]` | change settings (live where the table above says so); `destinations` replaces the list |
| `stream.probe` | `refresh?: bool` | probe encoders and transports now |
| `record.start` | `format?: "mkv"\|"mp4"`, `name?: text` | record the program. mkv is written directly (a crash loses at most the last second); mp4 is fragmented (`frag_keyframe+empty_moov`) |
| `record.stop` | | finish the file |
| `replay.start` | `seconds?: 5-600` (default 30) | keep the last N seconds of the encoded program in memory (instant replay) |
| `replay.save` | `seconds?: 5-N`, `name?: text` | write the last N seconds to `Replay-<time>.mkv` in the recordings folder |
| `replay.stop` | | stop the buffer and free its memory |

`stream.set` also takes the Presets page's contract, `{"platform": id, "tier": bandwidth-tier id, "plan": {...}}`
(see [presets](presets.md)). The plan is translated to settings: its picture size gives the resolution `tier` (the
largest tier whose short side fits), and its fps, rate control, key frame interval, first codec's bitrate or quality and
audio bitrate become the matching settings. One codec in the plan pins it; several leave it to the planner. The
bandwidth tier id is never used as a resolution tier. A rig without the stream backend answers 501, which the Presets
page shows as "pending". The platform and tier also become the stream's bandwidth ladder (next section).

`stream.set` also takes `adapt: true|false` (automatic downshift on or off), and `ladder: {"platform": id, "tier": id}` or
`ladder: null` for a caller that names the ladder itself. Changing `tier`, `fps`, `bitrate_kbps` or `quality` by hand,
without naming a ladder, forgets the ladder: the steps under a plan the person has overridden would no longer mean
anything. Audio, codec, encoder and destination changes keep it.

Errors are `ActionError` with a message and a status (400 for a bad request, 409 for a state conflict).

### Bandwidth adaptation

A live stream that outruns the uplink fails badly: the send queue grows, frames drop, the platform buffers and viewers
see stalls. Sending less before that happens is the cure, so the service steps the picture down by itself and back up
when the link is steady (`streamstack/stream/adapt.py`; pure logic, no threads or clock of its own, so every rule is a
unit test).

**The ladder.** Choosing a platform and a bandwidth tier on the Presets page sends `stream.set` with both ids and the
plan, and the service builds the ladder with `presets.plan.ladder_tier`. Step 0 is exactly the chosen plan. Each later
step is a plan the Presets page could have produced, with strictly less picture (pixels per second) and, where the
platform has a bitrate, at least 5 percent fewer bits: the rest of the tier's ladder, then the lower tiers' (for
YouTube Balanced: 1080p30 at 3.2 Mbit/s, 720p60, 720p30, 480p at 1.0, 0.8 and 0.35 Mbit/s). A platform that ignores tiers
(the archive) has one step and never shifts. A step changes only the picture size, frame rate and bitrate (and quality
for constant-quality plans), never the codec, encoder, key frame interval or audio, so a shift is the live "seamless" or
"geometry" swap described above, never a codec move.

**Chosen versus sent.** `settings` is what the person chose and is what is saved; `effective` is what is being sent
(the choice with the current step applied) and is what `output` reports. The Picture card keeps showing the choice; the
Bandwidth card says what is sent instead. Stopping the last live destination, starting a new stream, turning the
automation off or choosing settings again all return to step 0.

**What it watches**, per live destination every 2 s (recordings are not judged), as a verdict of ok, warn, congested or
severe, where the worst destination decides:

| Signal | Congested | Severe |
|---|---|---|
| packet loss (`whip.loss_percent`, `srt.loss_percent`, when the transport measures it) | 3 percent | 8 percent |
| video waiting in the send queue (`queued_bytes` at the current bitrate) | 1.2 s | 4 s |
| round trip (`rtt_ms`) | 3 times the best seen and 150 ms more | |
| frames dropped since the last look | 5 | 60 |
| reconnects since the last look | 1 | 2 |
| send speed (`speed`, after 10 s connected) | below 0.9 times real time | below 0.6 |

RTMP has no loss or round-trip measurement and is judged on the queue, drops, reconnects and speed alone; WHIP reports
its own, and SRT's path measurement (`status()["srt"]`, `rtt_ms` and `loss_percent` or `probe_loss_percent`) is read the
moment a sink provides it. A signal that is missing is not judged.

**Hysteresis**, so a picture change is never twitchy: a step down needs two congested rounds in a row (a severe round
needs one, and a link that stays severe through the whole wait steps two rungs at once); after any change nothing moves for 15 s (5 s for
a severe link); a step up is one rung and needs 30 s of an unbroken ok verdict (a warning restarts the count); a
step down within 90 s of a step up doubles the next calm spell required (up to 8 times as long), so a link that cannot carry a
rung is not hammered with it, and the memory is dropped after 5 minutes of calm at the top. A swap still in flight
delays a shift by a second (`stream.set` answers 409 in that case; the controller simply retries).

**The network quality monitor.** `StreamService(uplink=callable)` takes the monitor's rating of the uplink (`good`,
`degraded`, `poor`, `offline`). `poor` and `offline` stop a step up (the link the monitor distrusts is not the moment to
send more) but never cause a step down alone. A rating that raises is reported as an event and ignored.

State: `effective` and `adapt` (below); events say each shift and why. The demo rig runs the same controller against a
simulated uplink with shorter timings; `stream.simulate {loss_percent}` (demo only) sets the loss it pretends to have.

### Settings

| Key | Values | Default |
|---|---|---|
| `codec` | `auto`, `av1`, `hevc`, `h264` | `auto` |
| `encoder` | `auto`, or a name from the probe (`av1_vaapi`, `libx264`, ...) | `auto` |
| `tier` | `source`, `480p`, `720p`, `900p`, `1080p`, `1440p`, `2160p` | `source` |
| `fps` | 0 (follow the source), or 5 to 120 | 0 |
| `bitrate_kbps` | 0 (a default for the codec and tier), or a number | 0 |
| `rate_control` | `cbr`, `vbr`, `cqp` | `cbr` |
| `keyint_s` | seconds | 2.0 |
| `quality` | the quantiser for `cqp`; 0 picks one | 0 |
| `audio_bitrate_kbps` | Opus bitrate | 128 |

### Destinations

```json
{"id": "twitch", "kind": "rtmp", "name": "Twitch", "url": "rtmp://live.example/app",
 "key_ref": "env:TWITCH_KEY", "enabled": true, "codec": "auto", "options": {"mode": "auto"}}
```

- `kind`: `record`, `srt`, `rtmp`, `whip`.
- SRT `options`: `mode` (`caller` or `listener`), `latency_ms` (a number of milliseconds, default 120, or `"auto"`),
  `pbkeylen`, `streamid`. See [SRT path measurement and input](#srt-path-measurement-and-input).
- RTMP `options.mode`: `auto` (Enhanced RTMP with the best codec; falls back to H.264 if it never connects), `enhanced`
  (never falls back), `classic` (H.264).
- WHIP: `url` is the ingest endpoint (`https://` or `http://`), `key_ref` is the bearer token (optional). StreamStack
  publishes it itself (no ffmpeg muxer), so it needs the optional extra `pip install 'streamstack[whip]'`; the probe says
  when it is missing. H.264 is the default; `codec: hevc` or `av1` offers that codec instead, for servers that take it.
  See [WHIP](#whip) below.
- `key_ref` is a reference: `env:NAME`, `file:/path`, `cfg:STREAMSTACK_KEY` or `cmd:argv`. A key typed into the URL is
  refused. Keep the real value in Azure Key Vault or the Bitwarden vault, and export it to the service environment.
- Destinations are saved, without secret values, in `<state_dir>/stream.json`.
- **Typing a key (the Console's Stream page).** An entry in a `destinations` list of `stream.set` may carry `key`: the
  secret itself, as text of up to 1024 printable characters without spaces. The service stores it in
  `<state_dir>/keys/<id>` (directory mode 0700, file mode 0600), points `key_ref` at that file (`file:<path>`) and never
  returns, logs or echoes it; errors do not quote it. An empty `key` removes the stored key. `key` and `key_ref` in one
  entry is an error. An entry with neither `key` nor `key_ref` keeps the key of the destination with the same `id`, so
  a client that only sees `"key": "set"` can re-send the list without losing keys. A running destination cannot be
  re-keyed (409: stop it first). Key files that no destination references any more are deleted.

**The key is visible in `ps`.** ffmpeg takes the RTMP key and the SRT passphrase on its command line, so other local users
can read it with `ps`. (The WHIP token does not: StreamStack sends it itself, from memory.) StreamStack redacts it in logs and the Console never shows it (`"key": "set"`). On a shared
machine mount `/proc` with `hidepid=2`.

### Configuration keys

| Key | Default | Meaning |
|---|---|---|
| `STREAMSTACK_STREAM_AUDIO` | `streamstack_mic` | PipeWire/Pulse source name for the program audio (falls back to `default` with a warning) |
| `STREAMSTACK_STREAM_AUDIO_OFFSET_MS` | `0` | lip-sync trim; positive delays the sound |
| `STREAMSTACK_RECORD_DIR` | `~/Videos/StreamStack` | where recordings and replay clips go |
| `STREAMSTACK_RECORD_RAW_MIC` | `true` | keep the unprocessed microphone as a third track of every recording (see "Crash-safe recording") |
| `STREAMSTACK_RECORD_MIN_FREE_MB` | `1024` | free disk space a recording must leave: refused below twice this, warned at three times, ended (file saved) at this |
| `STREAMSTACK_REPLAY_MAX_MB` | `512` | the most memory the instant replay buffer may hold |
| `STREAMSTACK_STREAM_MAX_ENCODES` | `2` | encodes that may run at once (1 to 3) |

The picture comes from `STREAMSTACK_PROGRAM_DEVICE` (default `/dev/video43`) at the size and rate of `STREAMSTACK_WIDTH`,
`_HEIGHT` and `_FPS`.

## Twitch accounts

Enhanced Broadcasting (HEVC and AV1 to Twitch) is per account. When an `auto`-mode RTMP destination points at Twitch
(`twitch.tv`, `live-video.net`) StreamStack asks Twitch, once per stream start, what the account may publish: the same
`GetClientConfiguration` call OBS makes, with the stream key as the credential in the request body over https (the
key is never logged, shown or kept; the cache is keyed by a hash and lasts 10 minutes, 1 minute for a failure). The
codecs the answer lists are the only ones offered, so an account that is not enrolled streams H.264 immediately
instead of failing to connect with AV1 and falling back after two attempts. The destination's state carries
`account` (`known`, `codecs`, `max_bitrate_kbps`, `reason`) and the row shows a "Twitch account" chip.

If Twitch cannot be asked, refuses the key, or answers in a shape this code does not know, the codec choice is the
normal one (best codec the host can encode) with the fallback above, and the Console log says why. Pinned modes
(`enhanced`, `classic`) and a destination codec that is not `auto` are never probed. `STREAMSTACK_TWITCH_CONFIG_URL`
overrides the address, for tests.

The call is Twitch's own, not a public contract: its answer shape was read from OBS's behaviour, so an unexpected shape
degrades to "not known" rather than being guessed. The manual check that a real enrolled account streams AV1 is a
human step recorded on issue #82.
## SRT path measurement and input

**Measuring the path.** While an SRT caller destination runs, StreamStack sends an SRT handshake induction packet to
the receiver every 10 s (three probes, 50 ms apart) and times the answer. The Console shows the median round trip
(RTT) with its minimum and maximum, the share of probes that went unanswered, and a recommended latency of
`max(80 ms, 4 x RTT)`. The multiplier rises to 5, 6 and 7 as probe loss passes 0, 10 and 20 percent, and the result is
capped at 8000 ms. A hint appears when the configured latency is below the recommendation.

**Auto latency.** `latency_ms: "auto"` (the Auto checkbox on the destination form) uses the recommendation. SRT fixes a
connection's latency when it is made, so "auto" is resolved each time the sink connects or reconnects, from a
measurement no older than 30 s (a fresh probe is taken otherwise). Without any answer it uses 120 ms and says so.

**What cannot be shown.** A listener-mode destination does not know where its caller is, so it cannot measure the
path: it uses the number set and says so. ffmpeg's libsrt does not report retransmit or loss counters, so the Console
shows only what is measurable (RTT and probe loss) rather than inventing a packet-loss figure. A receiver that drops
the probe (a firewall) reads as 100 % probe loss; latency then stays at the default.

**SRT input (remote backup recording).** The Stream page's "SRT input" card, or the actions `ingest.start`
(`port` 1024-65535, `latency_ms` 20-8000 default 200, optional `name`, optional `key_ref` for a passphrase) and
`ingest.stop`, starts an ffmpeg SRT listener that copies what a remote sender delivers into an MKV in the recording
folder, with no re-encode. When the sender leaves, the file is kept and the listener waits for the next sender, which
gets the next numbered file. A port that is already in use, or an unusable passphrase, stops the listener after three
quick failures with the reason in the card. The passphrase is redacted from logs. State is in `stream.ingest`
(`active`, `state` of `waiting`, `receiving`, `failed` or `stopped`, `port`, `bytes`, `file`, `files`, `callers`).

## WHIP

StreamStack publishes WHIP (RFC 9725) itself; the design and what it leaves out are in
[ADR 0002](adr/0002-whip-publishing.md). Install the extra on the machine that streams:

```
pip install 'streamstack[whip]'        # aiortc 1.15 and what it brings (PyAV, pyOpenSSL, libsrtp)
```

Add a destination of `kind: whip` (the Stream page's Type menu has "WHIP (WebRTC)"), give the endpoint URL, and a bearer
token if the ingest wants one (typed into the page, it is stored write-only like a stream key). Everything else works as
for SRT and RTMP: Go live, the live controls (bitrate, rate control, keyframe interval, encoder: the session is kept and
the new encoder's parameter sets take over at its first keyframe), reconnect with backoff, and one reconnect for a
picture-size or codec change.

What it does, in order: gathers local network candidates, POSTs an SDP offer (video `sendonly`, plus Opus audio when the
program has sound) with `Authorization: Bearer <token>`, takes the answer and the `Location`, runs ICE and DTLS-SRTP to
the server, and sends the encoder's own H.264, HEVC or AV1 as RTP (never re-encoded). On stop it sends RTCP BYE and then
`DELETE` on the resource, so the server frees the session at once.

Destination `state.whip` while live: `ice_state`, `dtls_state`, `rtt_ms`, `loss_percent`, `jitter_ms`, `bitrate_kbps`
(measured on the wire over 5 s), `packets_sent`, `bytes_sent`, `nacks` and `plis` (what the server asked for), `dropped`
(frames the sender could not take), `queued`, `rtcp_age_s`, `trickle` (whether the server accepts PATCH), `resource`
(the session URL without credentials), `codec`. The Stream page shows RTT and loss next to the bitrate.

Errors say what to do. The ones that cannot get better by trying again set the destination to `failed` and stay there:
401 or 403 (token), 404 (wrong endpoint), 405 or 501 (not a WHIP endpoint), 400, 406, 415 and 422 (the server refused
the offer or the codec), a malformed answer, a token reference that cannot be read. Network errors, 409, 429, 5xx
(`Retry-After` is honoured), a failed ICE or DTLS connection, and a session that stops answering are retried at 1, 2, 4,
8, 15 and 30 seconds.

Limits to know: no STUN or TURN is used unless a future setting adds one (a server with a public address does not need
it; a TURN-only ingest is not supported yet); a session gathers all candidates first and does not trickle them; there is
no packet pacing, so a keyframe leaves as a burst; a server's keyframe request (PLI) is counted and shown and the next
keyframe arrives within `keyint_s`. Proven against MediaMTX (H.264, HEVC and AV1) and aiortc; not against a hosted
ingest.

## Console state: the `stream` section

`GET /api/state` has a top-level `stream` object. Always present; `status` is `"unavailable"` on a rig built without it.

| Field | Type | Meaning |
|---|---|---|
| `status` | `idle` \| `starting` \| `live` \| `degraded` \| `recording` \| `replay` \| `error` \| `unavailable` | `live`: every live destination is up; `degraded`: some are; `recording`: only recording; `error`: an encoder failed for good |
| `uptime_s` | int | seconds since the first destination started |
| `bitrate_kbps` | number | total bitrate out of the encoders (video plus audio) |
| `dropped_frames` | int | dropped at the sinks plus encoder drops |
| `latency_ms` | int | the largest estimate among live destinations (not recording) |
| `cpu_percent` | number or null | CPU used by the encoder processes, in percent of one core (so 300 is three cores); null when idle or unreadable |
| `gpu_percent` | number or null | busiest GPU engine used by the encoder processes (DRM `fdinfo`), percent; null when idle or the driver reports none |
| `encoders` | list | one per running encode: `codec`, `encoder`, `label`, `backend`, `device`, `bitrate_kbps`, `fps`, `encoder_dropped`, `cpu_percent`, `gpu_percent` (as above, per encode), `latency_ms` (measured capture to relay), `swapping`, `swaps`, `failed` (text), `destinations` (ids) |
| `encoder` | text | the encoder names in use, comma separated; empty when idle |
| `destinations` | list | see below |
| `replay` | object | instant replay: `active`, `state`, `seconds` (armed length), `depth_s` (what is actually held), `bytes`, `peak_bytes`, `max_bytes`, `groups`, `evicted_for_memory`, `clips` (the last five saved) |
| `recording` | object | `active`, `file`, `bytes`, `seconds`, `format`, and when active `state`, `last_error`, `raw_mic`, `raw_error`, `free_mb`, `minutes_left`; after a disk-guard stop `stopped` |
| `recording` | object | `active`, `file`, `bytes`, `seconds`, `format`, and when active `state`, `last_error` |
| `settings` | object | the settings the person chose (the keys above); what is saved |
| `effective` | object | the settings being sent: `settings` with the current downshift step applied (equal to `settings` at step 0) |
| `adapt` | object | automatic downshift: `enabled`, `ladder` (`{platform, tier}` or null), `available` (a ladder exists), and when it does `step`, `steps` (`label`, `width`, `height`, `fps`, `bitrate_kbps`), `current`, `degraded`, `recover_in_s`, `cooldown_s`, `hold_factor`, `verdict` (`level`, `reason`) and `history` (`at`, `direction`, `from`, `to`, `reason`) |
| `output` | object | `width`, `height`, `fps` actually produced (the effective settings) |
| `audio` | object | `source`, `bitrate_kbps`, `ok`, `error` |
| `last_swap` | object or null | the last live change: `codec`, `ok`, `at`, and `gap_ms` and `encoder` when it worked, or `reason` when it did not (the old encoder keeps running) |
| `events` | list | the last eight events (`at`, `level`, `text`) |
| `probe` | object or null | the capability probe (below); null until the first probe has finished |

Each destination: `id`, `kind`, `name`, `url`, `enabled`, `key` (`"set"` or `"none"`), `codec_wanted`, `options`,
`active`, `state` (`idle`, `connecting`, `live`, `backoff`, `failed`), `health` (`ok`, `warn`, `fail`, `idle`), `codec` in
use, `bitrate_kbps`, `dropped_frames`, `restarts`, `latency_ms`, `last_error`, `queued_bytes`, `connected_s`, `moved`
(true if it was moved to another codec), and for recordings `file` and `bytes`.

**Latency estimate** per destination = measured encode latency (capture to relay) + the time the sink's queue holds
(`queued_bytes` at the current bitrate) + a transport figure: the SRT `latency_ms` option, about 250 ms for RTMP, and for
WHIP a 100 ms receiver buffer plus half the measured RTT (150 ms until an RTT is known). It excludes the camera and the platform's own player buffer.

### Probe

`stream.probe` (and `state.stream.probe`) says which encoders and transports work on this host, with a reason for every
one that does not:

```json
{"ffmpeg": "/usr/bin/ffmpeg", "version": "8.0.1", "cores": 28, "render_nodes": ["/dev/dri/renderD128"],
 "encoders": [{"name": "av1_vaapi", "codec": "av1", "backend": "vaapi", "device": "/dev/dri/renderD128",
               "ok": true, "reason": "", "trial_ms": 120},
              {"name": "av1_nvenc", "ok": false, "reason": "no NVIDIA GPU or driver (NVENC cannot load)", ...}],
 "transports": [{"id": "whip", "ok": true, "reason": "", "detail": "built-in client on aiortc 1.15.0; H.264 + Opus, ..."},
                {"id": "srt", "ok": true, ...}, ...],
 "codecs": ["av1", "hevc", "h264"], "problems": [], "age_s": 4.2}
```

Each encoder is trial-encoded for real (VA-API, QSV and NVENC through every render node), so "ok" means a frame came
out. The order of preference is AV1, HEVC, H.264 on the GPU, then SVT-AV1, x265 and x264 on the CPU where the cores can
keep up. A full probe takes about 0.4 s and is cached.

Which GPU encodes comes from the hardware Plan (docs/hardware-profiles.md): every trial records the PCI id of the GPU it
ran on, encoders that ran on the Plan's `encode` GPU are tried first, and the others stay as the fallback. On a host
where an integrated GPU enumerates first the Arc still encodes AV1, HEVC and H.264. `STREAMSTACK_STREAM_RENDER_NODE`
overrides it; with no matching profile the first node that can do the job is used, as before.

## Measurements

Host: Intel Arc A380 (DG2, i915, VA-API iHD), ffmpeg 8.0.1, 28 cores, other agents running. Tool:
`python3 tools/bench_stream.py` (12 s per case after a 3 s warm-up; the same ffmpeg command the service runs, a
wall-clock-paced synthetic `testsrc2` picture, CBR). **Latency** is capture to relay (the filter, upload, encode and
mux, and the pipe), excluding the camera and the network. **CPU** is the encoder process's CPU as a percent of one core,
minus the synthetic source's own cost (4 % at 720p, 9 % at 1080p). **GPU** is the engine busy percentage from the DRM
fdinfo of the ffmpeg process (`intel_gpu_top` needs CAP_PERFMON, which a user session lacks); `video` is the encode
engine, `video-enhance` the scaler.

| Encoder | Size | Latency p50 / p95 / max (ms) | CPU net (% of one core) | GPU (%) | kbps | fps |
|---|---|---|---|---|---|---|
| av1_vaapi | 720p30 | 164.9 / 165.6 / 166.4 | 3.7 | video 2.7, video-enhance 1.3 | 2464 | 30.0 |
| av1_vaapi | 1080p30 | 167.1 / 168.1 / 169.2 | 3.3 | video 5.5, video-enhance 2.8 | 3904 | 30.0 |
| hevc_vaapi | 720p30 | 64.1 / 64.8 / 70.5 | 3.7 | video 3.1, video-enhance 1.3 | 2831 | 30.0 |
| hevc_vaapi | 1080p30 | 69.3 / 70.1 / 72.7 | 1.6 | video 5.8, video-enhance 2.6 | 4506 | 30.0 |
| h264_vaapi | 720p30 | 60.3 / 60.9 / 62.7 | 2.5 | video 2.4, video-enhance 1.2 | 4030 | 30.0 |
| h264_vaapi | 1080p30 | 61.3 / 61.9 / 62.3 | 1.8 | video 5.1, video-enhance 2.7 | 5987 | 30.0 |
| libx264 (CPU, for scale) | 720p30 | 64.4 / 65.0 / 65.6 | 22.1 | none | 4051 | 30.0 |
| libx264 (CPU, for scale) | 1080p30 | 40.9 / 42.0 / 43.6 | 50.2 | none | 6037 | 30.0 |

What the numbers say:

- The GPU is idle for this work: 1080p30 uses 5 to 6 % of the encode engine for any codec, and almost no CPU. Each
  encoder holds 30 fps with large headroom (the radar measured 449 fps for AV1 and 402 fps for HEVC at 1080p).
- AV1 adds about 100 ms of latency against HEVC and H.264 on this driver (about 165 ms against 60 to 70 ms): the
  VA-API AV1 encoder holds frames back before it emits one. For low-latency SRT pin `codec: hevc` or `h264`.
- Bitrates are the encoder's own CBR (the defaults of the code, not a quality comparison; the test picture is synthetic).
- The CPU encoder costs 10 to 20 times the CPU of a GPU one, which is why the planner prefers the GPU and uses software
  only when the cores can keep up.
- Limits of this run: the picture is synthetic (no camera: `/dev/video43` did not exist at the time of the run), so
  capture and v4l2 costs are not included; the host was shared, so single runs have some noise.

### WHIP over the same encoders

Same host, run on 2026-10-24 with `python3 tools/bench_whip.py --encoders h264_vaapi,hevc_vaapi,av1_vaapi,libx264
--seconds 20` (3 s warm-up, the same ffmpeg command the service runs, CBR, wall-clock-paced synthetic `testsrc2`).
Each case is a real `WhipSink`, real ICE and DTLS-SRTP over 127.0.0.1, to a receiver that answers by mirroring the offer
and timestamps every RTP packet as it is decrypted (it does not decode, so HEVC and AV1 are measurable). **Encode** is
capture to the relay, as above. **To WHIP** is capture to the instant the session takes the frame off its queue.
**At receiver** is capture to the last RTP packet of the frame arriving at the receiver's socket. **WHIP CPU** is the
thread that reads, demuxes and packetizes plus the thread that encrypts and sends, as percent of one core (the
benchmark's own read and demux are in the first number, so it overstates what the relay costs).

| Encoder | Size | Encode p50 (ms) | To WHIP p50 (ms) | At receiver p50 / p95 / max (ms) | Encoder CPU net (% core) | WHIP CPU read+packetize / send (% core) | GPU (%) | kbps | Loss |
|---|---|---|---|---|---|---|---|---|---|
| h264_vaapi | 720p30 | 61.1 | 61.3 | 62.0 / 63.0 / 64.2 | 3.1 | 0.4 / 1.6 | video 2.4, video-enhance 1.2 | 4092 | 0 % |
| h264_vaapi | 1080p30 | 65.1 | 65.2 | 66.3 / 67.4 / 68.6 | 3.1 | 0.4 / 2.3 | video 5.1, video-enhance 2.6 | 6106 | 0 % |
| hevc_vaapi | 720p30 | 64.2 | 64.3 | 65.0 / 65.9 / 68.5 | 2.8 | 0.4 / 1.4 | video 3.1, video-enhance 1.2 | 2871 | 0 % |
| hevc_vaapi | 1080p30 | 65.5 | 65.6 | 66.5 / 67.5 / 70.9 | 2.9 | 0.4 / 2.0 | video 5.8, video-enhance 2.6 | 4588 | 0 % |
| av1_vaapi | 720p30 | 159.5 | 159.7 | 159.9 / 161.8 / 163.5 | 2.6 | 0.3 / 1.0 | video 2.7, video-enhance 1.2 | 2516 | 0 % |
| av1_vaapi | 1080p30 | 163.3 | 163.5 | 163.7 / 166.3 / 168.2 | 2.8 | 0.4 / 1.4 | video 5.4, video-enhance 2.6 | 3992 | 0 % |
| libx264 (CPU, for scale) | 720p30 | 58.8 | 59.0 | 59.8 / 60.9 / 62.4 | 21.0 | 0.4 / 1.9 | none | 4135 | 0 % |
| libx264 (CPU, for scale) | 1080p30 | 59.0 | 59.2 | 60.3 / 61.7 / 63.7 | 47.1 | 0.5 / 2.4 | none | 6194 | 0 % |

What the numbers say:

- Publishing over WHIP adds about 1 ms between the encoder and the wire and 1.4 to 2.8 % of one core for the whole WHIP
  path at 1080p30 (packetizing, SRTP, sending), whatever the codec. The hardware encode stays the cost it was: 3 % of a
  core and 5 to 6 % of the Arc's encode engine at 1080p30.
- Nothing was lost and the delay did not grow over 20 s on loopback: the sender keeps up with every encoder here.
- AV1 on this driver holds frames back for about 100 ms, as in the table above, and that is the whole difference.

What was **not** measured, and so is not claimed:

- Any real network: the figures are to a socket on the same machine. RTT, jitter, loss, the server's own buffering,
  and the viewer's jitter buffer and decode come on top (the Stream page's latency estimate assumes 100 ms for the
  receiver buffer; that 100 ms is an assumption, not a measurement).
- Glass to glass. There is no camera in this run (synthetic picture), no display, and no player.
- Audio: the benchmark publishes video only. Opus packetizing is a few hundred packets a second and is not expected to
  matter, but it is unmeasured here. The end-to-end tests do publish audio and decode it.
- A hosted ingest (Cloudflare, LiveKit, Janus, a platform): none was available to try.
- Behaviour on a congested uplink (no pacing, no congestion control: see the limits above).
- Longer than 20 s per case, and more than one WHIP destination at once.
- The CPU figures include other agents' load on a shared 28-core host (load average about 2), so single runs carry noise.

## Crash-safe recording

A recording is meant to survive a bad day: a crash, a power cut, a full disk, a service restart.

- **What a file holds.** Track 1 is the picture, track 2 the processed program sound (the one that goes to a live
  destination), track 3 the **raw microphone**, the unprocessed capture, so voice processing can be redone in an editor.
  Live destinations never get track 3 and are never delayed by it. A recording carries on with the processed sound
  alone, and says so, when the raw capture cannot start or stops. Turn the track off with
  `STREAMSTACK_RECORD_RAW_MIC=false`. It is skipped when there is no separate raw microphone.
- **Disk guard.** A recording is refused when free space is under twice `STREAMSTACK_RECORD_MIN_FREE_MB`, a warning
  shows once at three times it, and at the minimum the recording ends on its own: the file is closed, the Console
  shows an error naming the file, and everything recorded so far is kept. The Stream page shows free space and the time
  left at the current bitrate (`free_mb`, `minutes_left`).
- **After `kill -9`, an out-of-memory kill or a power cut.** MKV is written flushed and fsynced about once a second
  with no index, so it plays up to the last second but cannot seek and has no duration. While a recording runs a marker
  `<file>.recording` sits beside it. At the next start the Console looks for markers whose writer is gone and remuxes
  each file with `ffmpeg -c copy` (nothing is re-encoded) into one with an index, checks it, and only then replaces the
  original (it must hold every track and at least 97 percent of the bytes; otherwise both files stay and the Console says
  so). A fragmented MP4 needs no repair; it is checked and left as it is. A file that never got a frame is removed.
- **Stopping the Console (SIGTERM, SIGINT).** The recording is stopped first: the sink is flushed, fsynced and closed,
  the marker is removed and every ffmpeg is ended before the Console exits. A clean stop leaves an MKV without an index
  (as `record.stop` does); `ffmpeg -i in.mkv -c copy out.mkv` adds one.

Tests: `tests/test_stream_crash_safe.py` covers all of it with real ffmpeg, including a recorder killed with SIGKILL and a
Console stopped with SIGTERM.

## Instant replay

`replay.start` keeps the last N seconds of the encoded program in RAM; `replay.save` writes them to a Matroska clip.
Nothing is re-encoded: the buffer is one more relay sink, so it holds whatever the encoder chain already produces
(AV1, HEVC or H.264) and shares that chain with a recording or a stream, at no extra encoding cost.

- It keeps whole groups of pictures, so a clip starts on a keyframe: it is at least as long as asked, and at most one
  keyframe interval (`keyint_s`, 2 s by default) longer. If the buffer is younger than the length asked for, the clip
  is shorter and the answer says so.
- Memory is bounded twice. Old groups are dropped as they leave the window, and `STREAMSTACK_REPLAY_MAX_MB` (default
  512) is a hard cap that drops the oldest group first. Arming refuses a length that could not fit at the current
  bitrate and tells you the longest one that does. If a spike makes the cap bite anyway, depth is given up, never
  memory: `replay.evicted_for_memory` counts it and the page says so.
- A change of codec (`stream.set` with another `codec`) starts the buffer empty, because different pictures cannot be
  joined. Changing the picture size does the same.
- Clips are remuxed with `ffmpeg -c copy` so they carry an index and a duration; if ffmpeg cannot do that the raw
  Matroska is kept (it plays, and it is the same bytes a recording would hold).
- The buffer lives only in memory: stopping the stream session or restarting StreamStack discards it.
