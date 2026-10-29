# ADR 0008: OpenCL filters on VA-API surfaces are not adopted; the engine stays on Vulkan and libplacebo

Status: accepted (2026-10-28). Issue #132 (spike), milestone "Look and AI on the Arc". Related: #92, #95, #133,
`docs/native-video-engine.md`.

## Context

The engine keeps frames on the GPU through Vulkan and libplacebo. Vulkan to VA-API mapping fails on this stack, so the
encoder cannot join the graph, and the blur branch (split, blur, masked merge in yuv444p) costs about 40 points of one
CPU core. FFmpeg also ships OpenCL filters that work on Intel VA-API surfaces through `hwmap`, and the Arc advertises
`cl_intel_va_api_media_sharing` and `cl_intel_planar_yuv`. The question was whether an OpenCL stage could do the masked
merge and the denoise on the GPU and hand VA-API surfaces to an in-graph encoder.

Stack: Intel Arc A380 (DG2, 0x56a5), kernel 7.0.0-38, ffmpeg 8.0.1-3ubuntu2, mesa-vulkan-drivers 26.0.8,
intel-media-va-driver 26.1.2, libva2 2.23.0, Intel compute runtime 26.05.

## What works

Device setup `-init_hw_device vaapi=va:/dev/dri/renderD128 -init_hw_device opencl=ocl@va` works. A **decoder-produced NV12
VA surface** maps into OpenCL with `hwmap=derive_device=opencl`, runs through an OpenCL filter, maps back with
`hwmap=derive_device=vaapi:reverse=1` and encodes with `h264_vaapi`. With `unsharp_opencl` at 1080p30 the whole job
costs about 6 % of a core, against 115 % for the CPU `cas` path. So the idea is sound for the one shape that maps.

## What does not

- **yuv420p and yuv422p JPEG surfaces** (what the Canon and Cam Link MJPEG decoders produce) do not map to OpenCL.
  Only NV12 does (`cl_intel_planar_yuv`).
- **Surfaces made by `hwupload`** do not map to OpenCL either (the "Failed to map frame: -38" seen first). Only
  decoder-owned surfaces do. Our graph starts from a libplacebo (Vulkan) output, which is never a decoder surface, and
  Vulkan to VA-API is the mapping that already fails (#138).
- **`nlmeans_opencl`** needs planar input, so frames go up through a direct `hwupload` to OpenCL, not through VA-API,
  which loses the zero-copy benefit. At default parameters it runs about 3x slower than real time at 720p; with
  lighter parameters (small patch and research size) 240 frames take 3.0 s to 5.6 s, still more GPU time than
  `nlmeans_vulkan`, which is the denoiser #92 measured as the only strong one.
- **A masked merge** would need `program_opencl` with our own kernel, on frames that cannot be mapped (above).

## Decision

Do not build an OpenCL stage into the engine. The one shape that works (decoder NV12 surface to OpenCL to VA-API
encoder) is not the shape the engine has: its frames come from libplacebo, after the look, crop and scale. The
denoiser the data favours (#92) and the matte work (#133) stay on Vulkan or the CPU, and the in-graph encoder stays out
of reach until Vulkan to VA-API mapping works (tracked in #138, retested by the driver-version-keyed probe).

## Watch trigger (revisit when any of these holds)

1. The probe from #137 reports zero-copy decode and Vulkan import working: re-run the spike's graph with the encoder in
   the graph.
2. A mesa, intel-media-va-driver or compute-runtime release note mentions yuv420p/yuv422p or hwupload'ed surface
   mapping for `cl_intel_va_api_media_sharing`.
3. FFmpeg gains a Vulkan to OpenCL mapping that does not go through VA-API.

The experiments are reproducible from the graphs above; no spike code ships.
