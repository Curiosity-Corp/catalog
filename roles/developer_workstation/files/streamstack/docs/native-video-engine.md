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
| `hevc_qsv` | "Invalid data" on this driver stack (`hevc_vaapi -low_power 1` encodes, so it is the fallback row) |
| `av1_vaapi`, `av1_nvenc` in the probe | produce nothing here (no NVIDIA part present) |
| dmabuf from V4L2 | not exposed by the libav demuxer, so the upload is a CPU copy |
| libplacebo without its one-frame hold | removed in the graph (#141): stamp each frame a quarter period ahead and give libplacebo `fps=`; see "Where the hold was" |
| PipeWire camera node from ffmpeg | no ffmpeg pipewire output, no gst pipewiresink here |

PipeWire: WirePlumber's V4L2 monitor exposes `/dev/video43` as a `Video/Source` node (only when the user can open
the device: the `video` group or a seat ACL), so PipeWire clients see the program camera as soon as the engine feeds it.
Calls use the V4L2 node itself; see `docs/adr/0004-virtual-camera-and-mic.md`. A native libpipewire node is deferred; it needs
a small in-process producer and is its own issue.

## Encode tap

`engine/encode.py` is a table, best codec first: av1, hevc, h264; inside a codec, Quick Sync, then VA-API, then NVENC. Quick Sync leads because it takes
the system-memory frame and so joins the Vulkan graph; VA-API encoders need GPU frames, so the tap branch ends in
`hwupload` and the graph's filter device must be VA-API (CPU graph only: `build_argv` refuses a VA-API tap on the
Vulkan graph); it is the same GPU's way round a broken oneVPL runtime. Each
row is probed by really encoding eight frames, because a listed encoder is not a working one. The tap shares the
single `hwdownload`, converts to NV12 and gives the encoder system-memory frames. Probed here: av1_qsv OK,
h264_qsv OK, hevc_qsv fails, hevc_vaapi OK. AV2 has no row on purpose: the slot is empty until a real-time hardware encoder
exists, and adding it is one table row. Recording, SRT, WHIP/WebRTC, RTMP and Opus belong to `streamstack/stream`.

## Video denoise for the Canon feed (#92)

`tools/bench_video_denoise.py` panned a real photo (so the temporal filters face motion) at 1280x720, added sensor-like
noise (ffmpeg `noise`, temporal and uniform, strength 12 and 25) and scored every filter against the clean clip.
PSNR and SSIM cut 8 px off every border, because `nlmeans_vulkan` writes garbage into its outermost row and column
(measured: mean error 50 to 170 levels in that single row or column against 1.5 inside; the second row is normal).
Cost is the ffmpeg process's own CPU and GPU engine time per frame (DRM fdinfo), output discarded. 60 frames each.

| Noise | Filter | PSNR dB | SSIM | CPU ms/frame | GPU ms/frame |
|---|---|---|---|---|---|
| 12 | none | 37.41 | 0.852 | 1.1 | 0 |
| 12 | `hqdn3d` | 41.37 | 0.940 | 4.1 | 0 |
| 12 | `atadenoise` (temporal) | 37.91 | 0.866 | 5.7 | 0 |
| 12 | `denoise_vaapi` 16 | 39.48 | 0.907 | 3.5 | 1.6 |
| 12 | `denoise_vaapi` 32 | 41.93 | 0.949 | 3.4 | 1.6 |
| 12 | `nlmeans_vulkan` s3 p3 r5 | 43.30 | 0.983 | 1.8 | 9.0 |
| 12 | `nlmeans_vulkan` s5 p3 r7 | 39.82 | 0.978 | 2.0 | 15.0 |
| 25 | none | 31.08 | 0.584 | 1.2 | 0 |
| 25 | `hqdn3d` | 31.83 | 0.624 | 4.6 | 0 |
| 25 | `atadenoise` (temporal) | 31.13 | 0.586 | 6.3 | 0 |
| 25 | `denoise_vaapi` 16 | 31.12 | 0.589 | 3.5 | 1.5 |
| 25 | `denoise_vaapi` 32 | 31.24 | 0.596 | 3.6 | 1.5 |
| 25 | `nlmeans_vulkan` s3 p3 r5 | 40.02 | 0.941 | 1.6 | 9.0 |
| 25 | `nlmeans_vulkan` s5 p3 r7 | 38.96 | 0.959 | 1.9 | 15.0 |

Reading it: at heavy noise only `nlmeans_vulkan` does anything (+8.9 dB, SSIM 0.58 to 0.94); `denoise_vaapi`,
`atadenoise` and `hqdn3d` move the picture by under 1 dB there. At light noise `denoise_vaapi` at level 32 and
`hqdn3d` are decent, but `nlmeans_vulkan` still leads (+5.9 dB) and costs the least CPU (under 2 ms a frame, against 3.4
to 5.7 for the others), because the work is on the GPU: 9 ms of GPU time per 720p frame, which is 27 % of the Arc at
30 fps (about 2.25 times that at 1080p, so use it for the Canon's 720p feed or accept the load).

**Choice:** `nlmeans_vulkan=s=3:p=3:r=5` is the engine's denoiser. The stronger setting (s5 p3 r7) is 66 % slower for
less PSNR at both levels. It is opt-in, not on by default: a clean picture gains nothing and a denoiser costs detail,
so `STREAMSTACK_ENGINE_DENOISE=nlmeans` turns it on (Vulkan backend only). The graph then runs it right after the
upload, before the grade, and the grade crops 2 px off every side (the garbage row and column; the crop also holds for
the framing window, see `graph.safe_window`). That is 0.3 % of the frame width. `tests/test_engine_denoise.py` runs the
engine's graph on a GPU and checks both the noise reduction and the clean edges.

Not done: a Console toggle (the setting is read at engine start), and a low-light auto switch.

## Copies and conversions per frame (#95)

A census of what one frame goes through, so that "no new copies" is checkable. `U` is a host-to-GPU upload, `D` a
GPU-to-host download, `C` a CPU pixel-format conversion.

| Path | Steps | Copies |
|---|---|---|
| Raw camera, Vulkan, no taps | upload (`format=yuv422p,hwupload`), libplacebo, `D` (`hwdownload,format=yuv422p`), pack to yuyv422 (`C`), v4l2 write | 1 U, 1 D, 1 C |
| MJPEG camera, vaapi-copy (#137) | VA decode, decoder read-back, upload, libplacebo, `D`, `C` | 2 U-side moves (decode read-back, upload), 1 D, 1 C |
| MJPEG camera, zero-copy | VA decode, `hwmap` to Vulkan, libplacebo, `D`, `C` | 0 U, 1 D, 1 C (not working on this stack, #138) |
| Each encode tap, before #95 | `C` yuv422p to yuyv422 (shared), then `C` yuyv422 to nv12 per tap | 1 extra `C` per tap |
| Each encode tap, after #95 | `C` yuv422p to nv12 per tap, split before the pack | 1 `C` per tap, none shared with the pack |

What can not be removed today: the download (`D`) and the pack, because the program picture has to reach a v4l2
loopback device in system memory as yuyv422, and libplacebo cannot output a packed format or scale_vulkan convert one
(measured in the #95 experiments). The encoder cannot take the frame from the GPU either: Vulkan to VA-API mapping
fails (#138; the OpenCL route is ruled out in [ADR 0008](adr/0008-opencl-on-va-api-surfaces.md)). The one free win was
in the taps: they used to convert the already packed yuyv422 picture to nv12, so every frame was repacked to 4:2:2
once and unpacked again. The split now sits before the pack (`split=N[pre][t0]...;[pre]format=yuyv422[prog]`), so each
tap converts straight from the planar picture.

Measured with `tools/bench_look.py --look sharp --size 1920x1080 --taps 2` (raw nv12 into the null muxer, three runs
each, engine child only): CPU 72.8, 74.7 and 77.8 percent of one core before, 67.7, 69.7 and 68.8 after (6 points
less on average with two taps); GPU time 241 to 269 ms per second either way; feed-to-program latency p50 82 ms either
way. With no taps the graph is unchanged and the numbers are the same (29.9 against 29.2 percent). No copy was added.

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

- The rows above are the pre-#141 numbers (the engine added about 37 ms over the floor, which was two held frames). The #141 numbers are in the next section.
- Detection as a second graph costs nothing; splitting inside the program graph added 40 to 80 ms.
- Slider to picture is about 100 ms at 720p (one pipeline latency plus feed alignment). Applying the look takes
  0.06 ms. The under-50-ms target is met for apply time and not for glass-to-picture; the picture cannot change
  faster than one frame traverses the pipeline.
- Before the GPU path, the dominant 1080p cost was `colorcorrect` and `cas`, which ran on the CPU in yuv444p
  (next section).

## Where the hold was (#141)

At 720p the engine used to add about 76 ms on top of a 39 ms loopback and browser leg. The 37 ms "libplacebo hold"
turned out to be three separate holds, each one frame period (33 ms at 30 fps), and all three are gone.

1. **Threaded encoders.** ffmpeg 8 frame-threads the rawvideo and MJPEG encoders, so each frame waits one encode slot
   before it is written. `-threads 1` on every output (program, detection, segmenter) removes it. Floor: 35.0 to 1.8 ms.
2. **libplacebo's `pl_queue`.** `advance()` wants a frame with a pts later than the target before it renders, even with
   `frame_mixer=none`, and in non-fps mode ffmpeg sets the output pts to the frame's own pts, so the next frame is always
   needed. The graph now stamps frame N at `(N + 0.25)` frame periods (timebase 1/90000; frame 0 stays at 0) and gives
   libplacebo `fps=<camera fps>`. Output tick k then finds frame k already in the future and nearest, and renders at once.
   Count-based stamps cannot drift. Any shift in (0, 0.5) works; 0.25 has the most margin. A temporal effect that needs
   real lookahead must not use this stamp.
3. **The detection MJPEG encoder.** Left threaded, it stalled the whole child for 100 ms or more every few frames when
   the blur branch was on (a bistable p95 of 140 ms). Fixed by item 1.

The blur graph needs mask N+1 to release picture N, so the blur feed leads the mask by one frame (`MASK_LEAD = 1`); the
mask still lags the picture by two frames and its wait stays under one frame period.

Measured pipe to pipe (a counter stamped into each frame, feed write to read, 600 frames per case, Arc A380, nothing
live touched; ffmpeg CPU unchanged). Before is origin/main, after is this change:

| Case | p50 before | p50 after | p95 before | p95 after | max before | max after |
|---|---|---|---|---|---|---|
| 720p30 neutral | 74.4 | 7.8 | 75.8 | 9.2 | 84.4 | 16.8 |
| 720p30 with the blur branch | 78.2 | 10.9 | 107.6 | 13.0 | 111.1 | 18.0 |
| 1080p30 neutral | 82.5 | 16.1 | 84.6 | 22.3 | 91.0 | 46.8 |
| 1080p30 with the blur branch | 87.5 | 20.2 | 111.4 | 23.3 | 147.3 | 33.5 |

Attribution at 720p30, p50 in ms (1080p30 in brackets): pipe floor 35.0 to 1.8 with one encoder thread (36.9 to 3.7);
upload and download add about 3.2 (6.1); libplacebo adds about 1.9 (3.5), where it added about 35 with the hold;
everything on, 6.9 (13.3). The 15 and 60 fps runs scale with the frame period, which confirms the holds were
frame-period-bound. Pictures are bit-identical to origin/main (frame md5) for the neutral and graded looks.

Caveat: there is no scratch v4l2loopback node the bench can write to without reloading the module (which would drop the
live camera), so `streamstack engine bench` and `tools/latency_rig.py paths` were not run. The loopback write and the
reader are outside these numbers; re-run the bench on hardware to confirm the end-to-end figure.

The gate is GPU-free: `tests/test_engine_graph.py` asserts the stamp, the `fps=` option, and `-threads 1` on every
rawvideo and MJPEG output, including the blur graph. Upstream option: `pl_queue` `advance()` should treat a frame with
pts equal to the target as current when the radius is 0; that would remove the need for the stamp.

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

## Per-look LUTs and the scaler (#93)

A preset look names a generated 3D LUT (`presets/luts.py`, 12 of them). The Console gives the engine that name with
the look (the sliders stay numbers and flags; the name is kept per camera and survives a Console restart). libplacebo
reads a LUT at init only and has no strength option, so the engine bakes both, the same way it bakes sharpen: the
cube is the generated one mixed with the identity at the look's LUT amount, in steps of 0.05, one `.cube` per
(look, step) written once under the shader directory (`engine/shaders.py`). When the look or the amount crosses a
step the supervisor waits 0.6 s for the look to rest and rebuilds the child, as for sharpen; a look applied with its
1 s ramp therefore gets its LUT about 1.6 s after the click, and every other slider moves live. A look you save keeps its LUT name in `looks.json` (an optional `lut` string; files from before load unchanged), so
it renders as it did. A look that names no
LUT (or an engine without the shader directory) keeps the colorcorrect split-tone approximation; a cube that cannot
be written falls back to it with a toast.

Measured on the Arc with `ffmpeg -init_hw_device vulkan` and a test card (tests/test_engine_shaders.py runs this
where there is a GPU): `lut_type=normalized` reproduces a reference trilinear sampler of the same cube to a mean of
0.4 to 0.9 and at most 4.6 levels of 255 (portra, noir, teal-orange, velvia, 100 % and 50 %). The `lut_type=conversion`
the engine used for the static `STREAMSTACK_ENGINE_LUT` is a source-to-display colourspace conversion: with a
near-identity cube it tinted the picture green (mean 138 levels of 255 off the reference), and `native` applies the
cube to the raw Y'CbCr values (noir: 95 levels off). Both the per-look and the static LUT now use `normalized`.

Scalers: `STREAMSTACK_ENGINE_SCALER` picks the libplacebo scaler for the crop zoom and any size change (the program
size differing from the camera's). A name can be selected only if `tools/bench_scalers.py` has measured it; the 21
measured are `graph.SCALERS`. The tool scales a photo through the real engine path and compares with the original
(PSNR and SSIM) and reads the GPU engine time from the DRM counters (1080p, Arc A380, GPU ms per frame):

| Scaler | zoom 1.25x PSNR / GPU | zoom 2x PSNR / GPU | 2x down and back PSNR / GPU |
|---|---|---|---|
| ewa_lanczos (the old default) | 46.47 dB / 5.38 ms | 40.49 dB / 4.09 ms | 47.27 dB / 14.39 ms |
| lanczos (the new default) | 46.60 dB / 3.48 ms | 40.72 dB / 2.86 ms | 47.98 dB / 8.12 ms |
| spline36 (libplacebo's own) | 46.39 dB / 3.49 ms | 40.62 dB / 2.89 ms | 47.94 dB / 8.15 ms |
| ewa_lanczos4sharpest | 46.70 dB / 5.90 ms | 41.14 dB / 4.70 ms | 48.67 dB / 17.47 ms |
| bilinear | 44.65 dB / 2.43 ms | 39.20 dB / 2.13 ms | 42.74 dB / 4.77 ms |
| oversample | 44.42 dB / 2.59 ms | 38.79 dB / 2.28 ms | 49.75 dB / 4.56 ms |
| nearest | 41.50 dB / 2.57 ms | 37.95 dB / 2.22 ms | 46.98 dB / 5.17 ms |

The default changes from `ewa_lanczos` to `lanczos`: the same picture quality (PSNR within 0.2 dB, SSIM within
0.0005 in the zoom cases and better in the round trip) for 35 to 45 % less GPU time; at 1080p30 a zoomed or
downscaled picture saves 60 to 190 ms of GPU time per second. At 1:1 (no zoom, same size) libplacebo does not scale
and the setting costs nothing. The full 21-row tables are reproduced by `tools/bench_scalers.py --ratio 0.8|0.5|2`.

VA-API to Vulkan and the MJPEG decode path (#137): a VA-decoded JPEG maps into Vulkan and reads back through `hwmap`
and `hwdownload` equal to a software decode, but libplacebo cannot import the mapped frame on this stack (ffmpeg
8.0.1, libplacebo 7.360, mesa 26.0.8, intel-media-va-driver 26.1.2, Arc A380): `vk->CreateImage ...
VK_ERROR_OUT_OF_DEVICE_MEMORY` (libplacebo `gpu_tex.c`, the dma-buf import). A Cam Link in MJPEG mode would therefore
have had no picture. So the engine picks the first of three ways to decode a Motion-JPEG camera that works here:

| Path | Graph head | Where the decode runs |
|---|---|---|
| `vaapi` (zero-copy) | `-hwaccel vaapi -hwaccel_output_format vaapi`, `hwmap=derive_device=vulkan` | the GPU, no copy |
| `vaapi-copy` | `-hwaccel vaapi` (the decoder reads the surface back itself), `format=yuv420p\|yuv422p\|nv12,hwupload` | the GPU, one read-back and one upload |
| `software` | libavcodec (threaded by default), `format=yuv420p\|yuv422p\|nv12,hwupload` | the CPU |

- `probe.probe_mjpeg` runs the engine's own head, tier by tier, on a synthetic 4:2:0 and a 4:2:2 JPEG (decode,
  libplacebo, read back). The first tier that passes both is used. The answer is cached in `engine-mjpeg-probe.json`
  next to the settings under a fingerprint of the node, the ffmpeg version and the mesa, VA driver and libva packages;
  a new stack probes again, and a probe that could not run (no ffmpeg, timeout) is not kept. This stack answers
  `vaapi-copy`, with the zero-copy failure as the reason.
- The decoder's own read-back is used (no `-hwaccel_output_format`) rather than a `hwdownload` in the graph: a
  `hwdownload,format=nv12` on a JPEG surface (really yuv420p) only relabels the planes and the chroma is garbage.
- Detect and segmenter graphs use `scale` instead of `scale_vaapi` on the two fallback paths. Grade, crop and taps are
  the same on all three.
- The path is on the state block (`engine.decode`: `vaapi`, `vaapi-copy`, `software` or `none (raw)`; `decode_note`
  says why zero-copy is off), in the log and a toast, in the doctor's "Video engine" line and in `streamstack engine
  probe`. `STREAMSTACK_ENGINE_MJPEG_DECODE` (`auto`, `vaapi`, `vaapi-copy`, `software`) overrides the probe.

Measured in the whole engine argv (1080p30 MJPEG source, grade, detect branch, null output, 10 s at real time, two
runs each; one core is 100 %): software 40 to 42 % (4:2:0) and 41 to 44 % (4:2:2); vaapi-copy 33 to 35 % and 38 to 41 %.
The copy tier saves 3 to 8 points of a core here, not the 3 to 4 times a bare decode, upload and grade graph shows (16
to 18 % against 10 to 12 %): most of the engine's cost is shared (the read-back of the graded 4:4:4 picture, the
conversion to the output format and the detect branch; a raw camera costs 29 % in the same measure). Zero-copy could
not be measured on this stack.

Time-boxed look at making the zero-copy import work on the Arc: nothing found; the variants, errors, versions and the
stalled `hwmap,scale_vulkan` copy are recorded in
issue #138 in the project tracker for a retest after driver
updates (`streamstack engine probe` shows when the stack starts passing).

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

### Halo at the speaker's edge (#133)

A plain Gaussian blur of the whole frame lets the speaker's colours bleed into the blurred background, and the
composite shows a halo around the head and shoulders. The default (`STREAMSTACK_ENGINE_BLUR_EDGE=normalised`) blurs
only the background: at the small size it computes `blur(picture x w) / blur(w)` per plane, where `w` is the
background mask squared, with ffmpeg `blend` (multiply, and divide) and two `gblur`
instances (`bg` and `bgw`, both following the strength slider). Offline experiments on a synthetic composite showed
a large drop in the edge-band error against the plain blur; a guided-filter mask refinement was tried and rejected
because it did not beat the normalised blur on the same data. `plain` restores the old graph.

Regression test: `tests/test_engine_blur_edge.py` runs the real blur branch through ffmpeg on the CPU with a saturated
red speaker on a blue background and a hard mask, and measures how much of the speaker's red the background pixels 4 to
24 px outside the edge pick up: about 0.3 of it with the plain blur, under 0.02 with the normalised blur (the test fails
above 0.05, or if the plain blur does not show at least 0.15, or if the improvement is under 5 times).

Cost, measured with `tools/bench_blur.py --mode fake --edge plain|normalised` (1280x720 at 30 fps, strength 8, 20 s
stamped runs, this host with the Arc A380; the stand-in segmenter means the OpenVINO package, which is not installed
here, is not in the numbers, but the whole engine graph is, libplacebo on the Arc included):

| edge | ffmpeg CPU (% of one core) | latency p50 / p95 | dropped |
|---|---|---|---|
| plain | 59.3, 61.7 (before), 60.0, 59.3, 59.2, 59.9 (after) | 77.3 / 80.3 ms | 0 |
| normalised, as shipped in #133 | 89.9, 90.1 (issue), 89.0 (rerun) | 77.9 / 80.0 ms | 0 |
| normalised, #148 | 63.9, 66.5, 63.8, 62.7 (mean 64.2, +7.7 % over plain) | 77.2 to 77.8 / 80.5 to 81.7 ms | 0 |

#### Why the cost came down without Vulkan (#148)

The extra 29 points were not the `gblur` and `blend` stages as such. Timing each stage in isolation
(`ffmpeg -benchmark`, 300 frames of 1280x720) showed the `blend=all_expr='min(255,A*255/max(B,1))'` divide was about
80 % of the branch's extra CPU: the expression evaluator runs per pixel, per plane, at about 44 ns each. Two changes
took it down to +7.7 % of plain:

1. The divide is the `blend` filter's own `all_mode=divide`. Its values equal the expression's to within one level
   wherever the weight is above 0 (it returns white where the weight is exactly 0, which is under the speaker and
   masked out). `test_divide_mode_matches_the_expression_it_replaced` checks this through ffmpeg.
2. The weight is the same in all three planes of the mask, so its blur runs on one plane (`extractplanes=y`) and is
   copied back to three with `mergeplanes` for the divide. (`format=gray` is not a substitute: it rescales the range
   and made the halo come back, 0.16 of the speaker's colour; the edge test caught it.)

Latency is unchanged, nothing is dropped, and `tests/test_engine_blur_edge.py` still passes.

#### Why Vulkan was not used

Not viable for this branch, and not needed any more:

- The Vulkan filter set on this ffmpeg has `gblur_vulkan`, `avgblur_vulkan`, `scale_vulkan` and `blend_vulkan`, but
  `blend_vulkan` only has `normal` and `multiply` (no divide), and there is no `maskedmerge_vulkan`. The divide and the
  merge would stay on the CPU, so the picture and the weights would have to cross the PCIe/staging boundary every
  frame in both directions.
- The stages are at 1/4 size (320x180), where the CPU is cheap. Measured here, `hwupload,gblur_vulkan,hwdownload` on one
  320x180 plane costs 0.154 s of CPU for 600 frames against 0.347 s for the CPU `gblur` (about 0.3 ms a frame saved)
  and adds a synchronisation wait (0.39 s against 0.08 s of wall time for the same 600 frames). Moving the stages would
  save under a point of one core and add latency jitter and a second code path.
- A libplacebo custom shader takes one input, so it cannot divide two pictures.

The CPU path is therefore the only path; there is no Vulkan variant to fall back from. If a future ffmpeg gains
`blend_vulkan` divide and a Vulkan masked merge, the whole branch (not just the extra stages) is the thing to move.

Still to check by a human on real recorded Canon and Cam Link clips: the look of the edge on hair and glasses.

## Idle

The supervisor child is stopped, not paused, while no app reads StreamStack Camera: see
[camera-delivery.md](camera-delivery.md#idle-when-no-app-reads-the-camera). The Console owns the decision
(`engine/idle.py`, `console/native.py`); `VideoEngine` needs nothing beyond `start()` and `stop()`, and the delivery
guard releases the placeholder before the child opens the node, as it does after a camera loss.

## Left for other issues

- OpenCL filters on VA-API surfaces: investigated, not adopted ([ADR 0008](adr/0008-opencl-on-va-api-surfaces.md)).
- Compositing the blur on the GPU (see above).
- Hardware AV1 recording and streaming over the tap, SRT, WHIP/WebRTC, RTMP and Opus (`streamstack/stream`).
- A native PipeWire node; a dmabuf import path if libav ever exposes it.
