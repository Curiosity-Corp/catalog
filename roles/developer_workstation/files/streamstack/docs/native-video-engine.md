# ADR: the native video engine (no middleware)

Status: accepted for the program-camera slice (#57). Date: 2026-10-23.

## Decision

StreamStack makes the program camera itself. One supervised **libav (ffmpeg 8) child process** runs the whole
camera path on the Intel Arc A380 (the render node of the Plan's `grade` role: `renderD128` on dm313red, `renderD129`
on deskmeet1 where the UHD 770 enumerates first, see docs/hardware-profiles.md) and writes the v4l2loopback device Teams and Zoom read
(`/dev/video43`). The Python Console is the **control plane** only: it starts, supervises and re-points the
child, and changes its picture by writing commands to the child's stdin. It never touches a pixel.

The child is a library-level tool (libav filter graph), not a daemon and not an application: it lives and dies
with the Console, has no state of its own, and is restarted from the Console's state.

```
camera (V4L2: YUYV / NV12 / UYVY / MJPG)
   |  CPU copy (V4L2 does not export dmabuf through libav; the one unavoidable upload)
   v
hwupload (yuv422p) -> libplacebo (Vulkan, Arc)   brightness, contrast, saturation, gamma, warmth, crop   [live]
   |                        + CAS sharpen shader                                           [baked step]
   v
hwdownload (yuv422p) -> yuyv422 -> /dev/video43
   |
   +-- encode tap (hwdownload -> nv12 -> av1_qsv | h264_qsv | ...)   recording / streaming, added later

camera --> second graph: fps=8, scale 320 -> MJPEG on a pipe --> framing detector
```

MJPEG sources (Cam Link) decode on the Arc: `-hwaccel vaapi -hwaccel_output_format vaapi`, then
`hwmap=derive_device=vulkan` into libplacebo, so JPEG decode never runs on the CPU.

## Live reconfiguration

ffmpeg's interactive command protocol is the live path: `c<target> -1 <command> <arg>` on stdin.
`libplacebo@grade` (crop_x/y/w/h, brightness, contrast, saturation, gamma), `colorcorrect@tone` and `cas@sharp`
accept commands. Measured facts that shaped the engine:

- ffmpeg polls stdin once per `-stats_period`. The default 0.5 s gave 400 ms from slider to picture; the engine
  runs `-progress pipe:N -stats_period 0.02`. 0.005 gave no further gain.
- ffmpeg disables stdin interaction when an input is `pipe:N`, `-` or `/dev/stdin`. Real V4L2 device inputs are
  unaffected; the bench feeds through `/dev/fd/N` for that reason.
- Not commandable (reply -38): procamp_vaapi, blend_vulkan, unsharp, scale_vaapi, sharpness_vaapi. libplacebo
  `lut=` is init-only, so the LUT is a static `.cube` (`STREAMSTACK_ENGINE_LUT`), not a slider.
- Only a source change (a different camera or mode) restarts the child. That is the single restart.

## Why Python is not the bottleneck, and why not Rust or C++

| Measure | Value |
|---|---|
| Applying a look from the Console (stdin write) | 0.05 to 0.1 ms |
| Engine process CPU, 720p30 neutral | 28 to 32 % of one core, all of it inside ffmpeg |
| Python per-frame work | none (frames never enter Python; only a 8 fps 320 px JPEG for detection) |

The media core is libav on VA-API/Vulkan. A Rust or C++ core would replace a stdin write of 0.06 ms and a
supervisor loop, and would add a second media stack to maintain. The measurements do not justify it. Revisit if
a per-frame path must run inside the process (for example an OpenVINO mask composited per frame), where a
native core, or ffmpeg filters in a shared library, would then pay for itself.

## Feasibility matrix (this machine, ffmpeg 8.0.1, Arc A380)

Worked: V4L2 raw in, VA-API MJPEG decode, Vulkan upload/download, libplacebo grade and crop, runtime commands,
v4l2loopback out, av1_qsv and h264_qsv taps from system-memory frames.

Failed or rejected, with the reason:

| Attempt | Result |
|---|---|
| Vulkan frames to VAAPI (`hwmap`) for in-graph encode | segfault or allocation failure |
| Vulkan to DRM derive | -38 |
| `av1_vulkan` / `h264_vulkan` / `hevc_vulkan` | driver lacks VK_KHR_video_encode_queue |
| In-graph VAAPI encode | the filter device is Vulkan, so the encoder cannot join the graph |
| `hevc_qsv` | "Invalid data" on this driver stack |
| `av1_vaapi`, `av1_nvenc` in the probe | produce nothing here (no NVIDIA part present) |
| dmabuf from V4L2 | not exposed by the libav demuxer, so the upload is a CPU copy |
| libplacebo without its one-frame hold | no option removes it (about 37 ms) |
| PipeWire camera node from ffmpeg | no ffmpeg pipewire output, no gst pipewiresink here |

PipeWire: WirePlumber's V4L2 monitor exposes `/dev/video43` as a `Video/Source` node (only when the user can open
the device: the `video` group or a seat ACL), so PipeWire clients see the program camera as soon as the engine feeds it.
Calls use the V4L2 node itself; see `docs/adr/0004-virtual-camera-and-mic.md`. A native libpipewire node is deferred; it needs
a small in-process producer and is its own issue.

## Encode tap

`engine/encode.py` is a table, best codec first: av1, hevc, h264; inside a codec, Quick Sync then NVENC. Each
row is probed by really encoding eight frames, because a listed encoder is not a working one. The tap shares the
single `hwdownload`, converts to NV12 and gives the encoder system-memory frames. Probed here: av1_qsv OK,
h264_qsv OK, hevc_qsv fails. AV2 has no row on purpose: the slot is empty until a real-time hardware encoder
exists, and adding it is one table row. Recording, SRT, WHIP/WebRTC, RTMP and Opus belong to `streamstack/stream`.

## Measured results

28-core host, v4l2loopback `max_buffers=2`, a stamped synthetic YUYV feed, reader `v4l2-ctl` on `/dev/video43`
(`streamstack engine bench`).

| Case | p50 | p95 | CPU (one core) | fps out | dropped |
|---|---|---|---|---|---|
| Floor: pipe, ffmpeg, loopback, reader | 35 ms | | | | |
| Upload and download only | 38.6 ms | | | | |
| Old design, detection split inside the program graph | 156 ms | | | | |
| Engine, 720p30 neutral | 76 to 77 ms | 79 to 88 ms | 28 to 32 % | 29 to 30 | 0 |
| Engine, 1080p30 neutral (runs under load) | 115 ms | 151 ms | 58 % | 30 | 0 |
| 1080p30 all looks and crop | 124 ms | | 266 % | 30 | 0 |
| 1080p30 all looks, crop and av1_qsv tap | 117 ms | | 321 % | 29.4 | 0 |
| Live Canon `/dev/video42`, 1280x720 at 20 | | | | 19.6 | 0 |

- The engine adds about 37 ms over the floor at 720p. That is libplacebo's one-frame hold.
- Detection as a second graph costs nothing; splitting inside the program graph added 40 to 80 ms.
- Slider to picture is about 100 ms at 720p (one pipeline latency plus feed alignment). Applying the look takes
  0.06 ms. The under-50-ms target is met for apply time and not for glass-to-picture; the picture cannot change
  faster than one frame traverses the pipeline.
- Before the GPU path, the dominant 1080p cost was `colorcorrect` and `cas`, which ran on the CPU in yuv444p
  (next section).

## Warmth and sharpen on the GPU (#58)

Warmth is libplacebo's colour temperature, set live through the `extra_opts` command (`temperature=`). Sharpen is a
contrast-adaptive sharpening shader (AMD FidelityFX CAS, written out as a libplacebo `.hook`). libplacebo reads
custom shaders at init only and has no run-time parameter a shader could read, so the strength is a constant in the
shader file and the strengths are baked in steps of 0.05, one generated file each (`engine/shaders.py`, written once
under the Console's shader directory). When the sharpen slider crosses a step the supervisor waits 0.6 s for the
look to stop moving and rebuilds the child (about 0.22 s to the first frame; the camera repeats its last frame
meanwhile). Everything else in the look stays a live command. A shader that cannot be written turns sharpening off
with a toast instead of stopping the picture.

The upload is now `yuv422p` (the camera's own layout; `hwupload` refuses `yuyv422`) and the download comes back as
`yuv422p`, so the 4:4:4 conversion that fed the CPU filters is gone. The CPU path is the fallback and is unchanged:
`STREAMSTACK_ENGINE_SHARPEN=cpu` (default `auto`) keeps live `colorcorrect` and `cas`, and so does an engine started
without a shader directory. The split-tone look (`lut_enabled`) still uses `colorcorrect` on the CPU, only while it
is on, until #93 turns it into per-look LUTs.

Measured with `tools/bench_look.py` (a photo with stamped frames through a real child and a pipe, 1080p30 YUYV, the
Arc A380, nothing live touched; CPU is the child's share of one core):

| Look | CPU before | CPU after | GPU ms/s before | GPU ms/s after | fps out after |
|---|---|---|---|---|---|
| neutral | 52 % | 29 % | 203 | 276 | 30.0 |
| warmth +0.4 | 63 % | 30 % | 206 | 293 | 30.0 |
| sharpen 0.18 | 247 % | 28 % | 211 | 317 | 27.8 |
| all sliders and sharpen | 254 % | 29 % | 226 | 283 | 30.0 |

Latency p50 stays at 80 to 85 ms in every case. The GPU time rises by 70 to 110 ms of the Arc's second, which is
the price of moving the work onto it; the sharpen run dropped to 27.8 fps once in this measurement and the other
runs held 30. The CPU `cas` filter is not monotonic in its strength, so the shader is checked against a numpy
implementation of CAS on grey content (mean 0.1 to 0.3 grey levels), not against `cas` level for level.

Known difference: libplacebo's cool side is mild (a white-point shift that saturates at about 20 R and 14 B). The
warm side matches the old `colorcorrect` closely; strongly cool looks (Tungsten Fix at -0.45) are weaker than before.

## OBS was removed (#61)

OBS is gone from StreamStack: its code, websocket actuators, profile seeding, `STREAMSTACK_VIDEO_ENGINE` and the
Console's OBS gating. The director's and auto mode's look changes, the framing crop and the privacy blackout all
go through the engine's live control path (stdin commands), and out-of-process callers reach it through the
Console's local API (`streamstack.consolelink`). A latency or CPU comparison with the old path was never measured
(OBS cannot be fed a stamped source), so none is claimed. Stale `STREAMSTACK_VIDEO_ENGINE` lines are stripped from
the user's env file on start, and the old `camlink.json` and `framing-restore.json` records are deleted.

## Background blur on the Arc (issue #60)

Addendum, October 2026. The person mask is computed on the Arc A380 by OpenVINO; the blur itself stays in the engine's
one ffmpeg graph. No OBS, no cloud, no second video pipeline.

**Model: PP-HumanSeg (PaddleSeg, Apache-2.0), 6.2 MB, 192x192 input.** Measured here (OpenVINO 2026.4.1, the A380 is the
only GPU device, synthetic 1280x720 frames, model alone, median of 200 runs):

| Model | Device | p50 | p95 | Verdict |
|---|---|---|---|---|
| PP-HumanSeg fp32 | Arc (GPU) | 1.75 ms | 1.87 ms | chosen: crisp edge on a test photo, Apache-2.0 |
| PP-HumanSeg fp32 | CPU | 5.04 ms | | slower, and it would take the CPU the graph needs |
| MediaPipe selfie (meet) | Arc | 1.53 ms | 2.33 ms | blobby mask on the same photo; the CPU is as fast (0.94 ms), so the GPU buys nothing |
| SINet | Arc | 2.93 ms | 3.14 ms | slower, no better edge |

All of them are far under the 33 ms of a 30 fps frame, so the Arc beats 30 fps at 720p with a wide margin. RVM
(best temporal quality) was left out on licence: its repository is GPL-3.0 (from memory, not re-checked here), which a
permissive-licence product cannot ship. YOLO-seg was not benchmarked: its licence is AGPL. The model is fetched on
first use into `<state_dir>/models` from a URL pinned to a commit, checked against the pinned size and SHA-256 and
installed atomically (`engine/segment.py`); it is never committed. OpenVINO is the optional extra
`pip install 'streamstack[blur]'`, never a hard dependency.

**Copies.** ffmpeg already holds the frame. The segmenter gets its own tiny graph (`scale=192:192,format=rgb24`, 110 KB
per frame, fast_bilinear) written to an inherited pipe, so the program graph is not split and keeps its latency (the
same lesson as the detection graph). Python reads the frame, OpenVINO converts and normalises the pixels on the GPU,
and one 320x180 mask goes back on a second pipe as a second ffmpeg input. In the program graph the picture is split;
one copy is shrunk to 1/4, blurred (`gblur@bg`, sigma from the 1 to 20 slider), grown again, and `maskedmerge@blur`
shows it where the mask says background. The mask is feathered (`gblur`) and temporally smoothed in Python
(per-pixel adaptive average plus a smoothstep on the edge, `MaskSmoother`).

**Live reconfiguration holds.** The branch is always in the graph when the Arc is usable, so strength is a `gblur@bg
sigma` command and on/off is `maskedmerge@blur enable`, both over stdin, with no restart (tested). The segmenter is only
opened the first time blur is switched on.

**Pairing picture and mask.** Both carry frame-number timestamps (`setpts=N/fps`), so a camera at 29.97 never drifts
out of step. ffmpeg starts no graph until every input has a frame, so the mask stream opens with two "no blur" frames.
Measured finding: the filter graph's frame sync will not release picture N until it has seen mask N+1. Answering
camera frame N with the mask for N+1 (the mask lead, `MASK_LEAD = 1`) removed a bursty 25 to 60 ms of added latency
(p50 104 ms, p95 138 ms, a four-frame sawtooth) at the price of the mask trailing the picture by two frames, about
66 ms at 30 fps. That trailing is visible only at the edge of fast movement. If it is not acceptable, the follow-up is
moving the compositor to a GPU filter, where the mask can be a texture.

**Measured on this machine, 720p30, stamped synthetic feed through the real engine (ffmpeg 8, libplacebo on the Arc):**

| Case | p50 | p95 | ffmpeg CPU | Python CPU | Notes |
|---|---|---|---|---|---|
| Blur not in the graph (baseline) | 76 ms | 77 ms | 27 % | | |
| Blur branch present, test segmenter | 78.0 ms | 79.1 ms | 67 % | 9 % | mask 2 frames behind the picture |
| Blur branch, real PP-HumanSeg on the Arc | | | 66 % | 12 to 13 % | mask 30 per second, inference p50 8 to 9 ms, p95 9 to 10 ms in the loop |
| Same, smoothness Light (every third frame) | | | 65 % | 9.5 % | 10 masks per second, inference p95 10 ms |

- Added latency is about 2 ms with the lead (78 against 76). The real-model run could not be stamped (it needs a real
  picture), so its latency is the test-segmenter figure; the pipes, pump and merge are identical, only the mask source
  differs, and the mask is never waited for.
- In the loop, inference takes 8 to 9 ms against 1.8 ms alone: the GPU is shared with the engine's libplacebo work and
  the call is a Python round trip. Still a quarter of a frame.
- GPU time used by the segmenter, from the process's DRM fdinfo: 66 to 75 ms per second (about 7 %) at 30 masks per
  second, 25 ms per second at 10. `intel_gpu_top` is denied here (no CAP_PERFMON), so total GPU busy is not measured.
- The real cost is CPU in the graph: the branch (split, blur at 1/4 size, mask scaling, merge in yuv444p) adds about 40
  points of one core, and it is present while blur is off (the price of switching without a restart). Moving
  the merge to the GPU removes it; that is the first follow-up, together with the colour stages above.

**Unverified.** Total GPU busy; blur quality on real cameras in varied light (only one test photo and a synthetic
mask-position check were used); 1080p; any comparison with the plugin it replaces; CPU-only machines (there is no
CPU fallback on purpose: without an Arc the Console says "Unavailable: <reason>" and the engine runs without the branch).

**Console.** `video.blur` keeps its action and state shape and adds `native: true` and an `arc` block
(`checking | unavailable | idle | downloading | loading | running | stalled | failed`, the reason, the device, mask
fps, inference ms, mask age). Settings are saved in `video.json`. `STREAMSTACK_ENGINE_BLUR=off` removes the branch;
`STREAMSTACK_ENGINE_BLUR_DEVICE` picks a device. Measure again with `tools/bench_blur.py`.

## Idle

The supervisor child is stopped, not paused, while no app reads StreamStack Camera: see
[camera-delivery.md](camera-delivery.md#idle-when-no-app-reads-the-camera). The Console owns the decision
(`engine/idle.py`, `console/native.py`); `VideoEngine` needs nothing beyond `start()` and `stop()`, and the delivery
guard releases the placeholder before the child opens the node, as it does after a camera loss.

## Left for other issues

- Compositing the blur on the GPU (see above).
- Hardware AV1 recording and streaming over the tap, SRT, WHIP/WebRTC, RTMP and Opus (`streamstack/stream`).
- A native PipeWire node; a dmabuf import path if libav ever exposes it.
