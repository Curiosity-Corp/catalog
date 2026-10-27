# SOTA radar, October 2026

Date: 2026-10-23. Scope: the techniques that are state of the art as of this month and usable in real time on a
StreamStack station (Intel B760, Arc A380, kernel 7.0, PipeWire 1.6.2, FFmpeg 8.0.1, GStreamer 1.28.2, Canon EOS 50D
over PTP, Cam Link 4K, Wave XLR or Scarlett 2i2 4th gen, Litra Beam, Stream Deck). The owner's direction is
"straight to the hardware and the kernel, no middleware, the old compositor removed completely". This document turns that into a
ranked, evidenced list and a proposed issue list. Nothing here was installed system-wide; no service, device or compositor
instance or audio route was touched. The few GPU runs were 1 to 20 second synthetic jobs on the render node.

## Verdict in six lines

1. The design is closer to state of the art than "not SOTA enough" suggests, and the biggest real gap is not a codec:
   it is that video still goes through a third-party compositor. Removing it (a native media core) is the enabler for hardware AV1,
   in-pipeline AI, crash-safe recording and every platform preset.
2. Hardware AV1 is real today on this exact hardware: `av1_vaapi` encodes 1080p at about 449 fps (synthetic, but 15
   times real time). Use it for recording and for platforms that take it (YouTube takes AV1 over RTMPS).
3. Hype to ignore for now: AV2 (spec is final, no hardware encoder), VVC (no hardware encode, no browser), MoQ (still
   an IETF draft), Vulkan Video on this Arc (the installed Mesa exposes no video queue), neural relighting and gaze
   correction (nothing open that runs on Arc), and libcamera (wrong class of camera).
4. Call apps receive raw frames from a camera device, so the codec matters only for recording, streaming and remote
   links. The thing that matters for calls is the camera node: keep v4l2loopback as the permanent contract and add a
   PipeWire camera node as a second, optional path.
5. Audio has the best return per week of work: loudness at -16 LUFS, neural noise suppression and in-process DSP are
   all feasible today with libraries already on the machine, and they replace Easy Effects.
6. A Tauri or Electron shell is not justified; the web Console plus the GTK tray already covers it and Playwright
   keeps testing it.

## Method

RICE is used exactly as in `docs/roadmap-rice.md`: Reach is uses per week across all properties (base 60 meetings a
week, 42 property-days for always-on health items), Impact is 0.25 to 3 (one row, hardware JPEG decode, is scored 0.1
because the measured gain is under the floor), Confidence is a percentage, Effort is person-weeks including tests,
e2e, docs and release. RICE = R x I x C / E. These are estimates until telemetry gives usage counts.

Evidence has three grades. **Measured** means I ran it on this machine today. **Sourced** means a link, with the
caveat that most 2026 coverage found by search is secondary (blogs, Phoronix, vendor pages) and is flagged where the
sources disagree. **Reasoned** means engineering judgement, labelled as such. Anything I could not confirm is listed
under "Unverified" at the end.

### What is on this machine (measured, 2026-10-23)

| Item | Result |
| --- | --- |
| Kernel, GPU | 7.0.0-38-generic; Arc A380 (DG2, 0x56a5) bound to `i915` (the `xe` module is loaded but not bound) |
| VA-API | iHD 26.1.2, VA-API 1.23; AV1 profile 0 has **encode only** (`EncSliceLP`), no AV1 decode entrypoint is listed; HEVC Main/Main10/4:2:2/4:4:4 and SCC encode and decode; H.264 encode and decode; VP9 encode and decode; JPEG baseline decode and encode; no VVC |
| FFmpeg 8.0.1 (Ubuntu) | Encoders: `av1_vaapi`, `av1_qsv`, `hevc_vaapi`, `h264_vaapi`, `libsvtav1` 2.3.0, `libjxl`, `libopus`, plus the Vulkan encoders (not usable, see below). Filters: `libplacebo`, `nlmeans_vulkan`, `denoise_vaapi`, `scale_vaapi`, `procamp_vaapi`, `tonemap_vaapi`, `arnndn`, `loudnorm`, `ebur128`, `afftdn`. Protocols: `srt` (libsrt 1.5.4), `rtmp(s)`, `rist`. **No `whip` muxer** in this build. No `whisper` filter |
| Enhanced FLV | `-c:v av1_vaapi -f flv` and `-c:v hevc_vaapi -f flv` both mux (measured); `ffprobe` reads the AV1 stream back |
| SRT | Caller to listener loopback on 127.0.0.1 with `latency=120000` (120 ms) streamed a 2 s clip without error (measured) |
| Hardware encode speed, 1080p, synthetic `testsrc2`, 20 s | `av1_vaapi` 449 fps; `hevc_vaapi` 402 fps (13.4x real time); `h264_vaapi` 373 fps (12.4x); CPU `libsvtav1 -preset 10` 158 fps (5.3x) while using the CPU. Synthetic content is easy to encode: treat these as upper bounds, not quality claims |
| Canon-class MJPEG decode, 1056x704, 10 s | Software 32.7x real time (0.32 s of CPU for 300 frames, about 1 ms per frame); VA-API JPEG decode 110x. Hardware JPEG decode is real but pointless at this size |
| libplacebo upscale 1056x704 to 1920x1280 | Bilinear 61 fps and bicubic 67 fps (the source is generated on the CPU and uploaded, so these are lower bounds); sharp Lanczos with `hwdownload` 17.7 fps (0.59x, below 30 fps); EWA Lanczos did not finish 3000 frames in the time budget and was stopped |
| Vulkan | ANV on Mesa 26.0.8 lists one queue family (graphics, compute, transfer) on the A380: **no video decode or encode queue**, so `h264_vulkan`, `hevc_vulkan` and `av1_vulkan` cannot work here. (Inferred from FFmpeg's device log; `vulkaninfo` is not installed) |
| Stills | `libjxl` 0.11.1 and `libavif` 16 both encode a 1080p frame in about 0.6 s |
| PipeWire | 1.6.2, WirePlumber 0.5.13. Modules present: `filter-chain`, `echo-cancel`, `loopback`, `rtp-*`, `roc-*`, `combine-stream`, `portal`. spa filter-graph plugins: `builtin` and `ladspa` only (the ONNX and FFmpeg filter plugins added in 1.6 are not in the Ubuntu package). spa AEC: `libspa-aec-webrtc` present. Camera nodes: Cam Link and the Canon loopback already appear as `Video/Source` |
| Camera devices | v4l2loopback 0.15.3 with two devices (Canon at `/dev/video42` fed by the gphoto2 bridge at 1280x720 YUYV 20 fps, StreamStack Camera at `/dev/video43`); Cam Link 4K offers 1920x1080 YUYV |
| Audio libraries | libebur128 1.2.6, libopus 1.6.1, libwebrtc-audio-processing-1 (1.3), speexdsp, ZamAudio LADSPA (comp, limiter, EQ), Calf LV2. DeepFilterNet LADSPA is not installed. Easy Effects 8.1.2 is installed (the middleware to retire) |
| AI | `openvino` and `onnxruntime` Python modules not installed; `libonnxruntime1.23` is installed; `intel-opencl-icd` 26.05 and libvpl 2.16 are installed (the OpenVINO GPU plugin prerequisite). `pyproject.toml` already lists `openvino>=2026.1` as an optional extra |
| GStreamer 1.28.2 | Only base and good plugins are installed: no `va`, `vulkan`, `pipewire`, `srt`, `webrtc` or `svtav1` elements. Using it would mean installing `-bad`, `-pipewire` and `-rs` packages first |
| Config | KEY=VALUE env files; only `camlink.py` writes atomically (temp file then `os.replace`) |

## Ranked table

Ranked by RICE. "Real-time on this hardware" is Yes, Partial or No; "Partial" means the pieces work but something
specific is unmeasured or missing, named in the last column. IDs are stable and used by the issue list.

| Rank | ID | Item | Area | R | I | C | E | RICE | Verdict | Real-time on this hardware | Latency and cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | V1 | v4l2loopback kept as the permanent compatibility camera | Camera | 60 | 1.5 | 90% | 1 | 81.0 | Adopt | Yes | About 1 frame (33 ms) copy; negligible CPU. Already in use. |
| 2 | U1 | Loudness: -16 LUFS gain riding (libebur128) | Audio | 60 | 2 | 85% | 1.5 | 68.0 | Adopt | Yes | Block latency only; under 1 percent of one core. |
| 3 | U2 | Neural noise suppression in the audio chain (DeepFilterNet3, RNNoise fallback) | Audio | 60 | 1.5 | 75% | 1 | 67.5 | Adopt | Yes | DFN about 20 to 40 ms algorithmic (66 ms mic-to-out reported by a PipeWire packager); RNNoise about 10 ms. CPU only. |
| 4 | X2 | Crash-safe config (atomic writes, schema, last-known-good) | Engineering | 42 | 1.5 | 85% | 1.5 | 35.7 | Adopt | n/a | None at run time. |
| 5 | U3 | In-process DSP via PipeWire filter-chain (replace Easy Effects) | Audio | 60 | 2 | 80% | 3 | 32.0 | Adopt | Yes | One quantum (about 5 to 21 ms) plus plugin delay; runs in the PipeWire RT thread. |
| 6 | P1 | Cinematography look presets (camera, light, grade, framing as one preset) | Product | 40 | 1.5 | 75% | 2 | 22.5 | Adopt | Yes | Preset switch under 1 s; grade is a 3D LUT. |
| 7 | C1 | Hardware AV1 (av1_vaapi) for recording and streaming | Codec | 25 | 2 | 85% | 2 | 21.2 | Adopt | Yes | Measured 449 fps at 1080p on the A380 (synthetic source); encoder pipeline depth about 2 frames. |
| 8 | P2 | Platform and bandwidth-tier presets | Product | 30 | 1.5 | 80% | 2 | 18.0 | Adopt | Yes | n/a |
| 9 | X3 | Signed artifacts and SBOM in the release pipeline | Engineering | 42 | 1 | 80% | 2 | 16.8 | Adopt | n/a | CI time only. |
| 10 | T1 | Enhanced RTMP(S) output with AV1 or HEVC | Transport | 15 | 2 | 80% | 1.5 | 16.0 | Adopt | Yes | Same as RTMP (about 3 to 5 s glass to glass via platform); uplink need drops about 30 percent vs H.264. |
| 11 | G1 | libplacebo on Vulkan (scaling, 3D LUT, tone mapping) | GPU pipeline | 40 | 1.5 | 65% | 3 | 13.0 | Trial | Partial | 61 to 67 fps for 1056x704 to 1920x1280 (bilinear, bicubic) including a CPU source; 17.7 fps with sharp Lanczos plus download to system memory. |
| 12 | U5 | Scarlett 4th gen controls over ALSA (Air, direct monitor, gain) | Audio | 30 | 1 | 60% | 1.5 | 12.0 | Trial | Partial | Control path only; no audio latency added. |
| 13 | A5 | Video denoise for the Canon feed (nlmeans_vulkan, denoise_vaapi, temporal) | AI and image | 30 | 1 | 60% | 1.5 | 12.0 | Trial | Partial | Filters present in FFmpeg here; not yet measured. |
| 14 | A6 | Deterministic auto-exposure and colour match (face metering, PTP, Litra) | AI and image | 30 | 1.5 | 65% | 2.5 | 11.7 | Trial | Yes | Loop of 1 to 2 s by design; CPU only; a few ms per metering frame. |
| 15 | X1 | Native media core (capture, filter, encode, sink) replacing the third-party compositor | Engineering | 60 | 2 | 75% | 8 | 11.2 | Adopt | Partial | Target under 1 frame added latency; FFmpeg and PipeWire pieces verified here, the daemon is not built. |
| 16 | C2 | HEVC as the compatibility and editing profile | Codec | 12 | 1 | 90% | 1 | 10.8 | Adopt | Yes | Measured 402 fps at 1080p; same code path as AV1. |
| 17 | U4 | WebRTC AEC3 (PipeWire echo-cancel) for speaker bleed | Audio | 15 | 1.5 | 70% | 1.5 | 10.5 | Trial | Yes | Module and spa plugin installed; adds a few ms; headphones make it unnecessary. |
| 18 | P4 | Crash-safe local recording (fragmented MP4 or MKV, multitrack) | Product | 12 | 1.5 | 85% | 1.5 | 10.2 | Adopt | Yes | Disk write about 5 to 8 Mbps video plus audio tracks. |
| 19 | A1 | Background blur or replace via OpenVINO on the Arc | AI and image | 30 | 1.5 | 60% | 3 | 9.0 | Trial | Partial | Unmeasured; budget 8 ms p95 per frame (existing gate). OpenVINO not installed here yet. |
| 20 | T2 | SRT output and input (backup, remote, studio to studio) | Transport | 10 | 1.5 | 85% | 1.5 | 8.5 | Trial | Yes | Loopback test passed here; adds the configured latency (120 ms or more) on top of encode. |
| 21 | C5 | AVIF and JPEG XL for stills | Codec | 10 | 0.5 | 80% | 0.5 | 8.0 | Adopt | Yes | 1080p still encodes in about 0.6 s (libjxl 0.11.1 and libavif here); not a real-time path. |
| 22 | V2 | PipeWire camera node (xdg camera portal) beside v4l2loopback | Camera | 20 | 1.5 | 55% | 3 | 5.5 | Trial | Partial | Same copy cost as loopback; works in portal-aware browsers, Zoom and Teams unverified. |
| 23 | T3 | WHEP console preview now, WHIP output later | Transport | 30 | 1 | 50% | 3 | 5.0 | Trial | Partial | Sub-second; this FFmpeg build has no whip muxer; needs a WebRTC library. |
| 24 | P3 | Instant replay ring buffer (Medal-style clips) | Product | 12 | 1 | 70% | 2 | 4.2 | Trial | Yes | RAM only; AV1 at 6 Mbps is about 45 MB per minute. |
| 25 | G2 | Zero-copy dmabuf end to end | GPU pipeline | 25 | 0.75 | 65% | 4 | 3.0 | Trial | Partial | Saves a few percent of one core at 1080p30; worth it only where free. |
| 26 | G4 | Hardware JPEG decode (VA-API) for the Canon MJPEG | GPU pipeline | 60 | 0.1 | 90% | 2 | 2.7 | Watch | Yes | Works (110x real time vs 32.7x in software) but software already costs about 1 ms per frame (about 2 percent of one core at 20 fps). |
| 27 | U6 | Opus neural features (DRED, deep PLC, bandwidth extension) | Audio | 6 | 1 | 50% | 2 | 1.5 | Watch | Partial | libopus 1.6.1 installed; only helps audio StreamStack itself sends; call apps use their own Opus. |
| 28 | A4 | Gaze correction | AI and image | 30 | 2 | 15% | 8 | 1.1 | Watch | No | No open real-time model for Arc found. |
| 29 | A2 | Super-resolution of the 720p Canon feed | AI and image | 20 | 0.5 | 35% | 4 | 0.9 | Watch | Partial | Intel Open Model Zoo models exist (older); gain is small because call apps send about 720p anyway. |
| 30 | A3 | Relighting (neural) | AI and image | 20 | 1 | 20% | 8 | 0.5 | Watch | No | None found; the Litra Beam lights are the real relight. |
| 31 | G3 | Vulkan Video encode and decode on the Arc | GPU pipeline | 5 | 0.5 | 40% | 3 | 0.3 | Watch | No | Mesa 26.0.8 here exposes no video queue; encode landed in Mesa 26.2 and 26.3-devel. |
| 32 | V3 | libcamera | Camera | 3 | 0.25 | 70% | 2 | 0.3 | Watch | No | Targets MIPI and ISP cameras; not the Canon, Cam Link or UVC path. |
| 33 | X4 | Tauri or Electron shell for the Console | Engineering | 10 | 0.25 | 60% | 6 | 0.2 | Watch | n/a | Not justified today; see the shell section. |
| 34 | C3 | AV2 | Codec | 3 | 1 | 30% | 4 | 0.2 | Watch | No | Spec final 28 May 2026; no hardware encoder; software decode about 5x AV1 (one source). |
| 35 | T4 | MoQ (Media over QUIC) | Transport | 2 | 1 | 20% | 5 | 0.1 | Watch | No | IETF draft -22 on 1 Oct 2026, not an RFC; no platform ingest. |
| 36 | C4 | VVC (H.266) | Codec | 1 | 1 | 30% | 4 | 0.1 | Watch | No | No hardware encode anywhere in this class; no browser decode; A380 has no VVC decode. |

## Adopt now, trial, watch

**Adopt now** (build in the next milestones; all evidence is local or well sourced):
V1 v4l2loopback compatibility sink; U1 loudness; U2 neural denoise in the chain; X2 crash-safe config; U3 in-process
filter-chain DSP; P1 look presets; C1 hardware AV1; P2 platform and bandwidth presets; X3 signed artifacts and SBOM;
T1 Enhanced RTMP; X1 native media core; C2 HEVC profile; P4 crash-safe recording; C5 AVIF and JPEG XL stills.

**Trial** (behind a flag, with a measured gate before it becomes default):
G1 libplacebo; U5 Scarlett 4th gen controls; A5 video denoise; A6 auto-exposure and colour match; U4 AEC3; A1 blur on
the Arc; T2 SRT; V2 PipeWire camera node; T3 WHEP preview; P3 instant replay; G2 zero-copy.

**Watch** (re-check each quarter; the issue list has one tracking item):
G4 hardware JPEG decode (works, not worth it); U6 Opus neural features; A4 gaze; A2 super-resolution; A3 relighting;
G3 Vulkan Video; V3 libcamera; X4 Tauri or Electron; C3 AV2; C4 VVC; T4 MoQ.

## Radar by area

### Codecs for recording and streaming

| Item | Status Oct 2026 | Honest read |
| --- | --- | --- |
| AV1 hardware (C1) | Encode-only on the A380 through VA-API; `av1_vaapi` has `low_power`, `rc_mode`, `tiles`, `b_depth` options. Measured 449 fps at 1080p. YouTube lists H.264, HEVC and AV1 for ingest over RTMP/RTMPS and recommends 4 to 10 Mbps for AV1 or HEVC at 1080p30 against 5 to 14 for H.264 ([YouTube](https://support.google.com/youtube/answer/2853702)). AV1 is not accepted for HDR on YouTube (HEVC is) | Real and ready. The catch: no AV1 *decode* entrypoint is listed on this host, so StreamStack cannot hardware-decode its own AV1 recordings for preview or scrubbing; use `libdav1d` in software (cheap) or HEVC for editing |
| HEVC (C2) | Full encode and decode on the A380, including 10-bit and 4:2:2. Measured 402 fps | The universal fallback: editors, Twitch Enhanced Broadcasting, YouTube. Same code path as AV1 |
| H.264 | Hardware encode and decode. Measured 373 fps | Last-resort floor for platforms and old Zoom or Teams relays |
| AV2 (C3) | AOMedia published the v1.0.0 specification and the AVM reference software on 28 May 2026, announced 9 June, claiming about 30 percent lower bitrate than AV1 ([AOMedia](https://aomedia.org/press%20releases/Alliance-for-Open-Media-Releases-AV2-Codec/), [implementer note](http://aomedia.org/blog%20posts/AV2-What-the-Final-Specification-Means-for-Implementers/)). Focus now is software (libavm, dav2d) then hardware | No hardware encoder exists, software decode is reported at about 5x AV1 complexity (one secondary source), and the A380 predates it. Real-time AV2 on this class of hardware is years away. Revisit when iHD or ANV advertise an AV2 encode entrypoint |
| VVC (C4) | Software VVenC and VVdeC; FFmpeg decodes it (`vvc`, `vvc_qsv`); Intel's first VVC decode was Lunar Lake. No browser ships it ([overview](https://www.forasoft.com/learn/video-encoding/articles/h266-vvc-explained)) | No. No hardware encode here, no platform takes it. Skip |
| JPEG XL and AVIF stills (C5) | Both encode here in about 0.6 s. Chrome 155 ships JPEG XL decode (via the Rust `jxl-rs` decoder) ([Chrome](https://developer.chrome.com/blog/jpeg-xl-in-chrome), [report](https://alternativeto.net/news/2026/10/google-ships-jpeg-xl-image-format-support-in-latest-chrome-155-update/)); Safari decodes it partially; Firefox unconfirmed | AVIF as the default for shared snapshots (universal), JPEG XL for the archive and for lossless recompression of the Canon's own JPEGs (reasoned: typically about 20 percent smaller, reversible). PNG remains the fallback |

Encoder policy (reasoned): probe the VA-API entrypoints at start, then choose a ladder AV1, HEVC, H.264 per profile.
For the 50D, encode at the camera's native size (about 1056x704, roughly 720p): upscaling to 1080p spends bits on
nothing and every platform re-encodes anyway.

### Streaming transport

| Item | Status | Honest read |
| --- | --- | --- |
| Enhanced RTMP with AV1/HEVC (T1) | FFmpeg's FLV muxer carries AV1 and HEVC here (measured). YouTube takes them. Twitch Enhanced Broadcasting takes H.264 and HEVC; AV1 status conflicts between sources (one says beta only), and the official Twitch page would not load for me ([E-RTMP overview](https://www.dacast.com/blog/rtmp-real-time-messaging-protocol/), [Twitch template](https://streamrun.com/templates/av1-or-hevc-to-twitch-enhanced-broadcasting)). Plain RTMP cannot carry AV1; the ingest must understand the enhanced signalling | Adopt for YouTube and custom ingests; make Twitch AV1 a per-account probe, not an assumption. The incumbent streaming app's 32.1 added WebRTC simulcast ([release notes](https://obsproject.com/blog)) but that is not the RTMP multitrack path Twitch uses |
| SRT (T2) | libsrt 1.5.4 in FFmpeg here; loopback tested. Carries any codec, so AV1 and HEVC work. Platforms (YouTube, Twitch) do not list SRT ingest | Trial for point-to-point: remote backup recording, studio to studio, a remote producer. Not a way to reach a platform |
| WHIP and WHEP (T3) | WHIP is RFC 9725 (March 2025, per a vendor source). FFmpeg 8.0 added a WHIP muxer but **this Ubuntu build lacks it** (measured: "Unknown format 'whip'"). GStreamer here has no `webrtc` plugin. WHIP ingest by large platforms is not confirmed | Partial. The useful in-house use is a sub-second WHEP preview in the Console (replacing MJPEG preview) via a Rust or Go WebRTC library. Platform output waits for platforms |
| MoQ (T4) | IETF `draft-ietf-moq-transport` is at revision 22 (1 Oct 2026), still not an RFC, with the milestone to send it to the IESG by 31 Dec 2026 and sources noting years of slipping dates ([datatracker](https://datatracker.ietf.org/doc/draft-ietf-moq-transport/)). WebTransport is in current browsers | No. No platform ingest, no native FFmpeg. Watch |

### The GPU pipeline

| Item | Status | Honest read |
| --- | --- | --- |
| Zero-copy dmabuf (G2) | VA-API surfaces can flow capture to scale to encode without readback. Cam Link 1080p NV12 is about 3 MB per frame, 93 MB/s at 30 fps | At 1080p30 the saving is a few percent of one core. Do it where it is free (decode, scale, encode on VA surfaces), do not contort the code for it. It matters once AI or 4K enters the path |
| Vulkan Video (G3) | Mesa ANV enabled H.264/H.265 encode for Gen12.5 (Arc A-series) in Mesa 26.2 and AV1 encode on DG2 in 26.3-devel; AV1 decode was already there ([Phoronix AV1](https://www.phoronix.com/news/Intel-DG2-Vulkan-Video-AV1), [Phoronix H.264/H.265](https://www.phoronix.com/news/Intel-ANV-Gen125-H265-Encode)) | No on this host: Ubuntu ships Mesa 26.0.8 and the device exposes no video queue (measured). VA-API remains Intel's supported path. Re-check when Mesa 26.3 is in the distribution |
| libplacebo (G1) | Present (7.360) and working through FFmpeg on the Arc. Good for high-quality scaling, 3D LUT application, tone mapping, dithering | Partial: fast for bilinear/bicubic, too slow for sharp Lanczos plus readback at 30 fps. Use it for LUT grading and fit, keep the frame on the GPU, and benchmark before enabling sharp scalers |
| Hardware JPEG decode (G4) | VA-API `JPEGBaseline` decode works: 110x real time | Measured not worth it: software already decodes the Canon feed at about 1 ms per frame. Keep software decode; do not add code paths |

### Camera delivery to call apps

| Item | Status | Honest read |
| --- | --- | --- |
| v4l2loopback (V1) | Module 0.15.3 loads on kernel 7.0. A February 2026 linux-media reply still concludes `v4l2loopback-dkms` is the primary option for the foreseeable future for apps that need a real camera device ([LKML](https://lkml.iu.edu/hypermail/linux/kernel/2602.0/00332.html)) | Keep, permanently, as the compatibility contract. Harden it: DKMS rebuild guard per kernel, `exclusive_caps=1`, fixed formats, stable label, conformance tests per call app |
| PipeWire camera node and the camera portal (V2) | The Camera portal hands apps a PipeWire remote ([portal docs](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Camera.html)). Libraries exist to publish a virtual `Video/Source` from user space ([pipewire-vircam](https://github.com/simon3z/pipewire-vircam), [gphoto-webcam](https://github.com/lkiesow/gphoto-webcam)). Browsers need a flag or setting (Chromium `WebRtcPipeWireCamera`, Firefox `media.webrtc.camera.allow-pipewire`). `pw-v4l2` can wrap legacy apps. Ideas on Board argues PipeWire is the way forward ([post](https://ideasonboard.com/news/pipewire-is-the-new-v4l2loopback)) | Partial. Nothing I found confirms Zoom or Teams read a portal camera. Publish both: a PipeWire node for portal-aware apps and the loopback for everyone else, and measure which Meet, Teams and Zoom builds see which |
| libcamera (V3) | v0.7.0 (Jan 2026) added a GPU software ISP ([announcement](https://lists.libcamera.org/pipermail/libcamera-devel/2026-January/057034.html)) | Not relevant: it serves MIPI and ISP cameras. The 50D is PTP, Cam Link and UVC are plain V4L2 |

The Canon bridge today decodes the 50D live view to 1280x720 YUYV at 20 fps into loopback. Frame rate (20 fps) is
the camera's live view limit, not the pipeline's; a 30 fps program output would need frame duplication or
interpolation and is not worth it.

### AI on the Arc through OpenVINO

OpenVINO 2026.3 (Aug 2026) and 2026.3.1 are current; 2026.2 added the Arc Pro B70. The system-requirements page lists
Ubuntu 26.04 with kernel 7.0 for GPU use ([release thread](https://community.intel.com/t5/Intel-Distribution-of-OpenVINO/OpenVINO-2026-3-Available-Now/m-p/1758325),
[releases](https://github.com/openvinotoolkit/openvino/releases)). I found no A380-specific statement and nothing is
installed here, so every GPU latency below is **unmeasured**; the existing benchmark harness (`tools/bench_ai.py`,
issue #22) is the gate (p95 added frame time under 8 ms, under 1 percent total CPU).

| Item | Honest read |
| --- | --- |
| Segmentation and blur (A1) | Feasible and the best AI feature for calls. Small portrait segmenters (MediaPipe Meet, PP-HumanSeg, RVM) are cheap; run in-process before the camera sink, which removing the compositor makes natural. Trial until measured on `GPU.0` |
| Super-resolution of the 720p feed (A2) | Intel's single-image models (1032/1033) are from the Open Model Zoo era and run frame by frame ([notebook](https://docs.openvino.ai/2023.3/notebooks/202-vision-superresolution-video-with-output.html)). Honest value is low: call apps send about 720p, and learned SR adds latency and artifacts on faces. Watch |
| Denoise (A5) | The 50D live view is noisy at high ISO. FFmpeg here has `nlmeans_vulkan` and `denoise_vaapi`; a temporal filter is cheap. Trial: measure on real Canon clips at ISO 1600 and up |
| Relighting (A3), gaze correction (A4) | No open, real-time Arc implementation found; vendor versions are RTX or NPU only. The Litra Beam is the relight, and a non-generative eye-line coach already exists as a roadmap item. Watch, and do not promise either |
| Auto-exposure and colour match (A6) | Deterministic and the highest value in this group: meter on the face region, drive the Canon over PTP (exposure compensation, white balance) and the Litra (brightness, temperature), match across properties with a grey-card pass. No GPU needed |

### Audio

| Item | Status | Honest read |
| --- | --- | --- |
| In-process DSP: PipeWire filter-chain vs Easy Effects (U3) | `filter-chain` is a PipeWire module that runs the graph in PipeWire's real-time thread; Easy Effects is a separate GTK app that builds a similar graph. Built-in plugins here are biquads, mixers and similar; dynamics need LADSPA (ZamComp, ZaMaximX2 installed) | Generate the filter-chain configuration from StreamStack's own settings and load it per user; no Easy Effects process. Gain control from a small loudness loop writes the chain's control port |
| Neural noise suppression (U2) | DeepFilterNet3 through its LADSPA plugin works in a filter-chain; the plugin has a 20 ms minimum latency and a packager reports 66 ms mic delay with PipeWire in the path ([DeepFilterNet](https://github.com/Rikorose/DeepFilterNet), [biglinux](https://github.com/biglinux/biglinux-noise-reduction-pipewire)). No confirmed successor to DFN3 was found; DPDFNet-2 is a research alternative. RNNoise stays the 10 ms fallback | Adopt: ship DFN3 as a built LADSPA artifact, RNNoise as fallback, a latency budget test, attenuation limit exposed (as today) |
| Echo cancellation (U4) | PipeWire's `echo-cancel` module with the WebRTC engine (`libspa-aec-webrtc`) is installed; 1.6 reworked it to stay better aligned and add less latency ([docs](https://docs.pipewire.org/page_pulse_module_echo_cancel.html)). The installed library is `libwebrtc-audio-processing-1` | Useful only for speaker bleed into recordings. Teams, Zoom and Meet run their own AEC, and running two can fight. Headphones are the real fix. Trial as a Pre-flight check, off by default |
| Loudness -16 LUFS (U1) | `libebur128` 1.2.6 and FFmpeg `ebur128`/`loudnorm` exist. -16 LUFS is the common spoken-word target (Apple Podcasts about -16, Spotify and YouTube about -14) | Adopt as short-term loudness measurement plus slow gain riding and a true-peak limiter. Never `loudnorm` in a live chain (it needs lookahead); use measurement plus a gain control |
| Scarlett 4th gen (U5) | The 2i2, Solo and 4i4 are handled by the `snd-usb-audio` Scarlett2 mixer and `alsa-scarlett-gui` ([project](https://github.com/nwxnw/alsa-scarlett-gui)); the large 4th gen models use a newer FCP driver ([0.5 notes](https://librearts.org/2025/03/alsa-scarlett-gui-0-5/)). Which ALSA control names exist per model (Air, direct monitor, Auto Gain, Clip Safe) was not confirmed, and no Scarlett is attached to this host | Partial. `hardware/scarlett.py` already exists; verify the control names on a real 2i2 4th gen and put Wave XLR and Scarlett behind one interface |
| Opus 1.5 and 1.6 neural features (U6) | Opus 1.6 (Dec 2025, 1.6.1 installed) improves DRED (deep redundancy) and adds an experimental bandwidth-extension model ([libopus 1.6](https://opus-codec.org/release/stable/2025/12/15/libopus-1_6.html)). LACE and NoLACE arrived in 1.5 | Only matters for audio StreamStack itself transmits (WHIP, SRT audio). Call apps use their own Opus. DRED needs a build option FFmpeg does not expose. Watch |

## What the top apps ship in 2026, and what matters for a Linux studio

Evidence is uneven: the incumbent streaming app's 32.1 notes were read directly; Streamlabs, Riverside and StreamYard come from secondary
comparison pages; Medal, vMix, Elgato Camera Hub and Logitech Capture could not be verified this session and are
described from general knowledge (marked "unverified").

| Product | What it ships | Matters for us? |
| --- | --- | --- |
| Incumbent open-source streaming app 32.x | Plugin manager, crash log upload, hybrid MOV (32.0); rebuilt audio mixer, WebRTC simulcast, partial websocket canvases, default bitrate 6000 kbps (32.1, 2 Apr 2026); SDR to HDR filter (32.2) ([notes](https://obsproject.com/blog)) | The bar for recording reliability. Take: crash-safe recording, diagnostics upload (as local bundle), sane bitrate defaults. Skip: plugin manager, canvases |
| Twitch Enhanced Broadcasting | Client sends several renditions (multitrack) in one enhanced RTMP stream (from general knowledge, unverified), HEVC; AV1 availability conflicts between sources | Adaptive quality ladder matters only if we stream to Twitch; make a profile, not a core feature |
| YouTube Live | RTMPS ingest, H.264/HEVC/AV1, published bitrate tables ([YouTube](https://support.google.com/youtube/answer/2853702)) | Directly drives the platform presets below |
| Streamlabs Desktop | Multistream and dual output (paid tier), an AI agent, heavier than the incumbent in RAM and CPU per comparisons | Dual output (horizontal and vertical) is cheap with a native core: two encodes of one frame |
| Medal (unverified) | Rolling clip buffer and one-click clips | Instant replay ring buffer (P3), cheap with hardware AV1 in RAM |
| Riverside and StreamYard | Riverside records each participant locally (up to 4K video, WAV audio) with multitrack and background upload; StreamYard is cloud and browser based ([comparison](https://talks.co/p/streamyard-vs-riverside)) | Local isolated multitrack recording and crash safety (P4); background upload later |
| vMix (unverified) | Broadcast switcher, SRT in and out, instant replay | Confirms SRT and replay as the professional baseline; Windows only |
| Elgato Camera Hub and Wave Link (unverified) | Camera controls, LUTs, prompter; Wave Link mixer | Look presets and LUT manager (P1); prompter already planned |
| Logitech Capture (unverified) | Camera framing, colour and recording presets | Same as above: preset-driven looks |
| Linux-specific gaps all of them have | No Linux build with hardware AV1 end to end, no PipeWire-native audio or camera chain, no per-property fleet config | Where StreamStack can lead: one binary, direct hardware, per-station presets |

## Presets

A preset is a versioned, declarative document (reasoned design) so a station can be rebuilt from one file and
properties stay consistent.

**Cinematography looks (P1).** One preset sets: Canon exposure (ISO, aperture, shutter, white balance, picture style
through PTP), Litra brightness and temperature, a 3D LUT and a lift-gamma-gain grade, framing (shot size, headroom,
thirds), blur strength, and the voice chain. Starter pack: Natural, Studio key (neutral 5600 K, even), Warm interview
(3200 K, lifted shadows, soft contrast), Broadcast (Rec.709, high key, tight), Low key (dark background, strong key
light), Soft beauty (low contrast, light glow), Daylight match (window-matched temperature, time-of-day aware).
Golden-image tests keep looks from drifting.

**Platform profiles (P2).**

| Target | Transport | Codec ladder | Notes |
| --- | --- | --- | --- |
| Teams, Zoom, Meet | camera device (no codec) | raw YUYV or NV12 | Deliver clean 720p30 and optional 1080p; the app encodes |
| YouTube | RTMPS (Enhanced) | AV1, then HEVC, then H.264 | 1080p30 AV1 or HEVC 4 to 10 Mbps, H.264 5 to 14; HDR is HEVC only; 2 s keyframe interval (reasoned from common guidance) |
| Twitch | RTMP(S) (Enhanced Broadcasting) | HEVC or H.264; AV1 only if the account probe passes | Probe, do not assume |
| LinkedIn, Facebook, custom RTMP | RTMPS | H.264 | Lowest common denominator |
| Local record | file | AV1 (or HEVC for editing) plus FLAC or Opus multitrack | Fragmented container, program plus raw mic plus processed mic |
| Remote backup and studio link | SRT | AV1 or HEVC | 120 ms or more latency, tuned from measured RTT |

**Bandwidth tiers.** Measure the sustained uplink (the network monitor in issue #41), take 70 percent as budget, pick a
tier, and downshift automatically with hysteresis.

| Tier | Budget | Output | Bitrate (AV1 or HEVC) |
| --- | --- | --- | --- |
| T0 | under 3 Mbps | 720p30 | 1.5 to 2.5 Mbps |
| T1 | 3 to 8 Mbps | 1080p30 (720p for the 50D) | 4 to 6 Mbps |
| T2 | 8 to 20 Mbps | 1080p60 | 6 to 12 Mbps |
| T3 | over 20 Mbps | 1440p or 4K (Cam Link or UVC only) | 8 to 30 Mbps |

The 50D cannot exceed T1 in practice: its source is about 720p at 20 fps.

## Native media core: language and architecture

Today StreamStack is about 16,000 lines of Python that shell out to ffmpeg and a third-party compositor. Options for the media core:

| Option | For | Against | Verdict |
| --- | --- | --- | --- |
| Python plus libav (PyAV or cffi) | Keeps one language; FFmpeg releases the GIL | Interpreter jitter and GC pauses in a frame loop; PyPI wheels bundle their own FFmpeg, which I would expect to lack VA-API (unverified), so you build against system FFmpeg anyway; the audio real-time path stays outside Python regardless | Keep Python for the control plane only |
| Rust daemon with FFmpeg bindings, `pipewire-rs`, an OpenVINO binding | Memory safety, no GC, real-time threads, one static binary, `cargo auditable` SBOM, strong crash behaviour | FFmpeg and OpenVINO bindings are volunteer-maintained and lag new releases (not verified this session); more up-front effort | Recommended target |
| C++ | Every library has first-class C or C++ APIs (libav, PipeWire, libplacebo, OpenVINO C++) | Memory safety and build complexity in a one-person-owner project | Only as thin shims where a binding is missing |
| GStreamer (`gstreamer-rs`) | Mature graph model, `va` and `vulkan` elements, `webrtcsink` in gst-plugins-rs; Rust bindings track 1.26+ ([Igalia](https://blogs.igalia.com/vjaquez/getting-ready-for-the-gstreamer-spring-hackfest-2026-in-nice/)) | Here only base and good plugins are installed, so it needs several packages; two media frameworks (libav and GStreamer) to keep consistent | Reasonable alternative; decide in the ADR spike |

Recommendation (reasoned, moderate conviction): a small Rust daemon owns capture, filter graph, encode, recording and
the PipeWire client; the existing Python stays as CLI, Console server and automation, talking to the daemon over a
Unix socket. Bootstrap by running libav as a supervised child process (crash isolation, already how the Canon bridge
works), then move it in-process once the graph is proven. The ADR spike must decide with measurements: added latency
under one frame, CPU at 1080p30 with encode, and recovery after a killed child.

## Shell: Tauri or Electron versus the web Console

Not justified today. The Console is a local web server with Playwright end-to-end tests, which is the project's
quality gate; Electron is supported by Playwright but doubles the packaging surface (and one comparison reports a 244
MB installer against 8.6 MB for Tauri, from a low-quality source), and Tauri on Linux renders through WebKitGTK, whose
video and WebRTC behaviour I found no evidence about and would have to test. A shell would buy window chrome, autostart
and global shortcuts, which the GTK tray and the desktop shortcuts portal already cover. Revisit only if the preview
needs a native surface (a Wayland subsurface or Vulkan window for lowest-latency preview), which is a smaller step than
adopting a whole shell. In the meantime: ship the Console as an installable PWA window and add the WHEP preview.

## Release, supply chain, telemetry, config

The current pipeline has test, e2e, security (secret detection) and package stages. State of the art for a Linux
desktop app, adapted from GitLab's own guidance:

- **Signed artifacts:** `cosign sign-blob` with keyless signing from the pipeline's OIDC token (`SIGSTORE_ID_TOKEN`,
  audience `sigstore`) for the wheel and the `.deb`; publish the bundle next to each artifact
  ([GitLab docs](https://docs.GitLab.com/ee/ci/yaml/signing_examples.html)). Add a signed apt repository (Release
  file signed with a repository key) so installs verify.
- **SBOM:** a CycloneDX SBOM per artifact (Python dependencies now; `cargo auditable` plus `cargo cyclonedx` once the
  Rust core exists), attached as a signed attestation, with a vulnerability and licence gate.
- **Provenance:** SLSA-style provenance. GitLab's provenance component is described as a design and a proposal, so do
  not depend on it; a signed in-toto statement from the job is achievable now.
- **Launch Sequence:** the organisation standard needs every stage green (source assurance, supply chain, artifact
  security, deployment, governance, GTM). Map each to a job and track the gaps.
- **Telemetry:** local-first metrics (the roadmap's Prometheus item, #49), opt-in, no personal data, plus a local
  diagnostics bundle for crashes in the style of the incumbent app's crash logs. Do not phone home by default.
- **Crash-safe config:** today settings are env files and only one writer is atomic. Move to atomic writes (temporary
  file, `fsync`, rename, directory `fsync`), a schema version with migrations, a last-known-good copy restored by a
  safe mode on a failed validation, and a write journal for the preset store.

## Proposed issue list, grouped into themed milestones

Milestone names carry no version numbers. Effort is in person-weeks and matches the table. Issues marked (existing)
overlap an open issue and should be linked, not duplicated. These are proposals only: nothing has been filed.

### Milestone: Direct media core

Goal: video runs without a third-party compositor, on hardware AV1 with a probed fallback ladder.

1. **ADR: media-core architecture and language** (X1, 1 week). Decide Rust daemon plus libav, Python plus libav or
   GStreamer. Gates: under one frame of added latency, CPU at 1080p30 with encode, recovery after a killed encoder.
2. **Hardware capability probe** (C1, 1). Read VA-API entrypoints, DRM render node, `av1_vaapi` rate-control modes and
   limits; cache JSON; show it in Doctor.
3. **Capture graph** (X1, 2). V4L2 capture for Cam Link YUYV and UVC MJPEG, the Canon PTP live view, a program frame
   clock; decode in software; frames on VA surfaces from the scaler on.
4. **Encode ladder AV1, HEVC, H.264** (C1, C2, 2). Profiles for record, stream and replay; golden-frame and
   rate-control tests; falls down the ladder when a profile is unsupported.
5. **Crash-safe local recording** (P4, 1.5). Fragmented MP4 or MKV, program plus raw mic plus processed mic tracks,
   disk guard, finalise on SIGTERM, recover after `kill -9` test.
6. **Enhanced RTMP(S) output** (T1, 1.5). AV1 and HEVC over enhanced FLV; CI test against a local ingest; a manual
   YouTube test stream; per-account Twitch probe.
7. **SRT output and input** (T2, 1.5). Latency from measured RTT, statistics in the Console, remote backup recording.
8. **Instant replay ring buffer** (P3, 2). Last N seconds of hardware-encoded video in RAM, clip to disk on a key.
9. **Stills: AVIF and JPEG XL** (C5, 0.5). AVIF snapshots, JPEG XL archive, lossless JPEG recompression for Canon
   stills.
10. **Remove the compositor** (X1, 2). Migrate scenes to presets, delete the compositor and websocket dependency, update the e2e suite and
    docs. Depends on 3, 4 and 5.

### Milestone: Camera delivery to call apps

Goal: every call app sees a reliable StreamStack camera, measured.

11. **v4l2loopback contract and hardening** (V1, 1). DKMS rebuild guard per kernel, `exclusive_caps`, fixed formats,
    stable label, reload-safe, conformance tests.
12. **Call-app compatibility matrix** (V1, 1). Chromium, Firefox, Meet, Teams (PWA and desktop), Zoom (desktop and
    browser); runbook plus camera-in-use detection.
13. **PipeWire camera node** (V2, 3). Publish a `Video/Source` beside the loopback; test with the portal in Firefox and
    Chromium, and with `pw-v4l2` for legacy apps. Report which Zoom and Teams builds see it.
14. **Glass-to-glass latency rig** (V1, 1). Timecode or LED test from camera to call app; publish numbers per path.

### Milestone: Studio audio in-process

Goal: the whole voice chain runs without Easy Effects and lands at -16 LUFS.

15. **Generate the PipeWire filter-chain from StreamStack config** (U3, 3). High-pass, EQ, compressor, limiter via
    LADSPA; one chain per mic; no Easy Effects process.
16. **DeepFilterNet3 as a built LADSPA artifact** (U2, 1). Build in CI, sign, RNNoise fallback, latency budget test.
17. **Loudness controller to -16 LUFS** (U1, 1.5). libebur128 short-term measurement, slow gain riding, true-peak
    limiter (existing #38).
18. **Scarlett 4th gen and Wave XLR behind one interface** (U5, 1.5). Verify ALSA control names on a real 2i2 4th gen;
    Air, direct monitor, gain; hardware-agnostic API (existing #18, #20).
19. **Speaker-bleed check with AEC3** (U4, 1.5). Optional Pre-flight check using `echo-cancel`; off by default; headphones
    first (existing #48).

### Milestone: Look and AI on the Arc

Goal: measured, in-pipeline image quality on the Canon feed.

20. **Extend the benchmark harness to OpenVINO 2026.3 on `GPU.0`** (A1, 1). Segmentation, denoise, super-resolution;
    pass or fail on the existing gates (existing #22).
21. **In-pipeline background blur or replace** (A1, 3). Runs before the camera sink; CPU fallback; off by default until
    the gate passes (existing #24, #26).
22. **Video denoise for the Canon feed** (A5, 1.5). Compare `nlmeans_vulkan`, `denoise_vaapi` and a temporal filter on
    real ISO 1600+ clips.
23. **libplacebo LUT and scaling path** (G1, 3). 3D LUT grading, quality scaling, VA to Vulkan frame mapping test;
    benchmark every scaler before it can be selected.
24. **Deterministic auto-exposure and colour match** (A6, 2.5). Face-region metering, Canon PTP exposure and white
    balance, Litra brightness and temperature, grey-card cross-property match (existing C22 backlog).
25. **Zero-copy where free** (G2, 4, optional). Keep frames on VA surfaces through scale and encode; no new copies.

### Milestone: Presets and platforms

Goal: one file per look and per destination.

26. **Preset schema and the cinematography look pack** (P1, 2). Versioned declarative format; seven starter looks;
    golden-image tests.
27. **Platform profiles** (P2, 1). Teams, Zoom, Meet, YouTube, Twitch, RTMPS, SRT, local record.
28. **Bandwidth tiers and automatic downshift** (P2, 1). Needs the network monitor (existing #41, #23 backlog).

### Milestone: Trust and release

Goal: every artifact is signed, inventoried and recoverable.

29. **Crash-safe configuration and presets** (X2, 1.5). Atomic writes, schema version, migrations, last-known-good,
    safe mode.
30. **Signed releases** (X3, 1). `cosign sign-blob` keyless for wheel and `.deb`, verification instructions.
31. **SBOM and vulnerability gate** (X3, 1). CycloneDX per artifact, signed attestation, licence check.
32. **Provenance, signed apt repository, update and rollback** (X3, 2). In-toto statement from CI, repository signing
    key in Key Vault, rollback to the previous package.
33. **Telemetry and diagnostics bundle** (X2, 2). Local-first, opt-in; a one-command bundle for crashes (existing #49).
34. **Launch Sequence stage conformance** (X3, 1). Map each stage to a job and close the gaps.

### Milestone: Preview and shell

Goal: a low-latency preview without adopting a UI shell.

35. **WHEP preview in the Console** (T3, 3). Sub-second preview through a Rust or Go WebRTC library; MJPEG as fallback.
36. **Decision record: shell** (X4, 0.5). Keep the web Console plus tray; record the triggers that would reopen
    Tauri or Electron; spike a native preview window only if WHEP misses its target.

### Milestone: Radar watch

One tracking issue each, re-checked every quarter (no build work until the trigger fires):

37. AV2: hardware encode entrypoint in iHD or ANV (C3).
38. Vulkan Video: Mesa 26.3 or later in the distribution, video queue visible on the A380 (G3).
39. MoQ: RFC published and a platform or FFmpeg ingest exists (T4).
40. WHIP platform ingest: YouTube or Twitch documents it, or FFmpeg's `whip` muxer is enabled in the Ubuntu build (T3). Update 2026-10-24: StreamStack now publishes WHIP with its own client on aiortc, so the muxer is no longer needed ([ADR 0002](adr/0002-whip-publishing.md), #108); what remains open is a platform that documents WHIP ingest.
41. VVC: hardware encode on Arc-class GPUs or browser decode (C4).
42. Neural relight and gaze: an open real-time model with an OpenVINO export (A3, A4).
43. Opus DRED: FFmpeg exposes it (U6).

## Risks

- **Unmeasured AI latency.** Every GPU figure for OpenVINO is an estimate until the harness runs; nothing here should
  be promised to the owner as working.
- **AV1 decode.** VA-API lists no AV1 decode on this A380; previews of AV1 recordings fall back to software.
- **Call-app camera support.** Zoom and Teams portal support is unverified; v4l2loopback must stay.
- **DKMS on new kernels.** v4l2loopback is out of tree; every kernel update is a risk, hence the rebuild guard.
- **Twitch AV1.** Sources conflict; probe per account.

## Unverified (could not confirm this session)

- Twitch AV1 ingest status (official page would not load); Medal, vMix, Camera Hub and Logitech Capture feature
  claims; Streamlabs pricing (sources disagree).
- Whether Zoom and Teams for Linux read a PipeWire portal camera.
- Scarlett 4th gen ALSA control names per model.
- OpenVINO GPU performance and operator support on the A380 (nothing installed).
- Whether PyPI FFmpeg wheels lack VA-API; the maturity of Rust FFmpeg and OpenVINO bindings.
- WebKitGTK video and WebRTC behaviour for a Tauri shell.
- Vulkan Video absence is inferred from FFmpeg's device log, not `vulkaninfo`.
- The AV2 decode-complexity figure and the date of the AV2 announcement vary by source.

## Sources

- [AOMedia: AV2 release](https://aomedia.org/press%20releases/Alliance-for-Open-Media-Releases-AV2-Codec/) and [implementer note](http://aomedia.org/blog%20posts/AV2-What-the-Final-Specification-Means-for-Implementers/)
- [IETF draft-ietf-moq-transport](https://datatracker.ietf.org/doc/draft-ietf-moq-transport/)
- [YouTube live encoder settings](https://support.google.com/youtube/answer/2853702)
- [Incumbent streaming app 32.1 release notes](https://obsproject.com/blog)
- [Phoronix: Vulkan Video AV1 on DG2](https://www.phoronix.com/news/Intel-DG2-Vulkan-Video-AV1) and [H.264/H.265 on Alchemist](https://www.phoronix.com/news/Intel-ANV-Gen125-H265-Encode)
- [libopus 1.6](https://opus-codec.org/release/stable/2025/12/15/libopus-1_6.html)
- [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet), [biglinux noise reduction](https://github.com/biglinux/biglinux-noise-reduction-pipewire)
- [OpenVINO 2026.3](https://community.intel.com/t5/Intel-Distribution-of-OpenVINO/OpenVINO-2026-3-Available-Now/m-p/1758325), [releases](https://github.com/openvinotoolkit/openvino/releases)
- [Chrome JPEG XL](https://developer.chrome.com/blog/jpeg-xl-in-chrome), [Chrome 155 report](https://alternativeto.net/news/2026/10/google-ships-jpeg-xl-image-format-support-in-latest-chrome-155-update/)
- [PipeWire 1.6](https://linuxiac.com/pipewire-1-6-released-with-ldac-decoder-and-128-channel-audio-support/), [echo-cancel docs](https://docs.pipewire.org/page_pulse_module_echo_cancel.html)
- [Ideas on Board: PipeWire is the new v4l2loopback](https://ideasonboard.com/news/pipewire-is-the-new-v4l2loopback), [linux-media reply](https://lkml.iu.edu/hypermail/linux/kernel/2602.0/00332.html), [Camera portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Camera.html), [pipewire-vircam](https://github.com/simon3z/pipewire-vircam), [gphoto-webcam](https://github.com/lkiesow/gphoto-webcam)
- [alsa-scarlett-gui](https://github.com/nwxnw/alsa-scarlett-gui), [0.5 notes](https://librearts.org/2025/03/alsa-scarlett-gui-0-5/)
- [libcamera 0.7.0](https://lists.libcamera.org/pipermail/libcamera-devel/2026-January/057034.html)
- [GitLab cosign signing](https://docs.GitLab.com/ee/ci/yaml/signing_examples.html)
- [Enhanced RTMP overview](https://www.dacast.com/blog/rtmp-real-time-messaging-protocol/), [Twitch template](https://streamrun.com/templates/av1-or-hevc-to-twitch-enhanced-broadcasting), [WHIP and WHEP](https://www.forasoft.com/blog/article/whip-whep-replace-rtmp-live-streaming-2026)
- [VVC overview](https://www.forasoft.com/learn/video-encoding/articles/h266-vvc-explained), [Riverside vs StreamYard](https://talks.co/p/streamyard-vs-riverside), [GStreamer Rust (Igalia)](https://blogs.igalia.com/vjaquez/getting-ready-for-the-gstreamer-spring-hackfest-2026-in-nice/)
