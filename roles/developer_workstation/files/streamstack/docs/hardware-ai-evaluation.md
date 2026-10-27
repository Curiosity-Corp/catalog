# Hardware AI evaluation: Arc A380 blur, auto-framing, denoise

Status: written for the third-party compositor path that StreamStack has since removed (#61). The evidence on models,
detectors and devices stands; the integration rows describe options for that path and are kept as history.

Spike for issue #19 (milestone Hardware-native). Desk research only:
nothing was measured on a live device, nothing was installed, no compositor config
was touched. Every latency figure below is either quoted from a cited source or
labelled **estimate**; the benchmark issue (#22) replaces estimates with
measurements on recorded clips before any default is chosen.

Rig: Intel Arc A380 (DG2, i915, VA-API iHD 26.1.2, libvpl 2.16, OpenCL
`intel-opencl-icd 26.05`), i7-14700 (AVX2 + AVX-VNNI), the compositor of the time (32.1.0), program
camera compositor to v4l2loopback to calls. Sources: Canon 50D (~1056x704 MJPEG) and
Cam Link 4K at 1080p.

## Verdict (ordered by value to #19)

1. **Auto-framing: build it inside StreamStack** (#23). A small OpenVINO face
   detector runs beside the compositor, off the frame path, and moves a crop through
   the compositor's websocket. No existing compositor plugin is both maintained and GPU-capable on
   Linux. Added video latency is zero; only the follow lag is tunable.
2. **Background blur: the background-removal plugin as a compositor filter, CPU first**
   (#24), Arc GPU through a carried OpenVINO build only if the CPU misses its
   budget (#26). The plugin is the only maintained Linux option, but upstream
   documents no OpenVINO backend.
3. **Voice: DeepFilterNet default, RNNoise fallback** in StreamStack's own PipeWire filter-chain
   (#25). No Arc involvement; this is a CPU latency/quality trade.
4. **Eye-contact and relight: do not build.** No Linux/Arc implementation
   found. Relight is already covered by the Lighting & Color filter and the
   Litra lights (#21).

Gate for all video effects (decided before measuring, checked by #22): added
p95 frame time under 8 ms, under 1 percent total CPU, no dropped frames at the
program camera's 30 fps.

## Findings per option

| Option | Linux / Arc status | Latency per frame | Packaging | License | Maturity | Integration |
| --- | --- | --- | --- | --- | --- | --- |
| **OpenVINO runtime (GPU plugin)** | Arc listed in system requirements; Ubuntu 22.04+ recommended for discrete GPUs, kernel 6.2+ for Arc, driver not bundled ([requirements](https://docs.openvino.ai/system_requirements), [GPU config](https://docs.openvino.ai/2024/get-started/configurations/configurations-intel-gpu.html)). This host has the OpenCL runtime and sees the A380 in `clinfo -l`. Releases 2026.1 (Apr 2026) through 2026.3 ([releases](https://github.com/openvinotoolkit/openvino/releases)) | Not published for our models. Estimate: 1 to 4 ms for a 256x256 segmentation or 320x240 face model on A380, plus compile time on first load (cache with `CACHE_DIR`) | `pip install openvino` (wheel), distro packages exist ([SUSE](https://packagehub.suse.com/packages/openvino/2026_1_0-bp160_1_1/)) | Apache-2.0 | High | Library inside a StreamStack Python thread |
| **Intel OpenVINO plugins for the compositor** (Smart Framing, Background Concealment) | **Dead end.** Repo archived 17 Aug 2026; Intel will not accept patches. Linux is build-from-source only, tested on Ubuntu 20.04/22.04 and the compositor 28.1.2, no Arc/DG2 mention ([repo](https://github.com/intel)). Snap listing exists but could not be fetched ([snap](https://snapcraft.io)); its the compositor/OpenVINO age is unverified | No numbers published | Source build; snap unverified | Apache-2.0 | Abandoned | Would be a compositor filter, but unmaintained against the compositor 32 |
| **the background-removal plugin** (royshil) | Official README lists CPU, CUDA, ROCm (deprecated in ORT 1.23), MIGraphX, CoreML; **no OpenVINO** on Linux. Source builds documented for Debian only ([repo](https://github.com/royshil)). Third-party guides mention an OpenVINO backend for Windows; unverified by the project docs | Models are small portrait segmenters (SINet, PP-HumanSeg, MediaPipe Meet, RVM). Estimate: 3 to 8 ms per 256x256 frame on 2 CPU threads; README says 2 threads work best | Build a .deb from source in CI; no apt/snap/flatpak for our the compositor (apt the compositor 32.1.0) | GPL-3.0 (see note) | Active, widely used | the compositor filter on the camera source in `scenes.json` |
| **ONNX Runtime OpenVINO EP** (route to put the background-removal plugin on Arc) | `device_type` accepts `GPU`, `GPU.0`; ORT 1.24.1 pairs with OpenVINO 2025.4.1; Linux wheels are Python only ([docs](https://onnxruntime.ai/docs/execution-providers/OpenVINO-ExecutionProvider.html)) | As OpenVINO row | C++ needs a source build of ORT with OpenVINO | MIT | Medium | Carried patch to the plugin's ORT dependency (#26) |
| **the face-tracker plugin** (norihiro) | Linux source build; dlib HOG/CNN, **CPU only, no GPU path**; README lists high CPU use, memory growth and jitter; one face only ([repo](https://github.com/norihiro)) | Estimate: 10 to 30 ms (HOG), CNN far worse | Source build | GPLv2; the 68-point landmark model has a non-free license | Slow, 46 open issues | the compositor filter; rejected on CPU cost and jitter |
| **DeepFilterNet** (voice) | Rust, runs on CPU; LADSPA plugin with PipeWire filter-chain; [repo](https://github.com/Rikorose/DeepFilterNet) | 40 ms algorithmic delay; RTF 0.11 vs 0.03 for RNNoise on a laptop i5-8250U ([paper](https://arxiv.org/pdf/2205.05474)). Far below real time limits on an i7-14700 | LADSPA, pip, binaries | MIT or Apache-2.0 | Good | Stage of StreamStack's own voice chain |
| **RNNoise** (voice) | CPU; in [noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice) (LV2/LADSPA/VST) | About 10 ms added ([overview](https://www.forasoft.com/learn/ai-for-video-engineering/articles-ai/real-time-noise-suppression-krisp-rnnoise-deepfilternet)); RTF 0.03 | LADSPA plugin | BSD (RNNoise); GPL-3.0 (the plugin wrapper) | Mature, older model | Fallback engine of the voice chain |
| **Eye-contact, relight** | Only vendor stacks on NVIDIA RTX (e.g. StreamFX's NVIDIA filters, [mirror](https://bytes.keithhacks.cyou)); nothing found for Arc/Linux | n/a | n/a | n/a | n/a | Skip |

Notes and caveats:

- The compositor virtual camera on Linux still needs v4l2loopback; a PipeWire
  virtual camera is not a shipped alternative yet
  ([discussion](https://ideasonboard.com/news/pipewire-is-the-new-v4l2loopback)).
  Effects must therefore live before the virtual camera, inside the compositor or as compositor
  transforms, not as a separate device between the compositor and the call.
- GPL-3.0 plugins are fine to install beside StreamStack but must not be
  linked into it; StreamStack only talks to them over the compositor's websocket.
- I could not read OpenVINO's current GPU page body (the fetch returned
  navigation only). The DG2/A380 claim therefore rests on the "Arc GPU
  Series" system-requirements entry plus this host's working OpenCL stack, and
  is a decision gate in #22.
- the background-removal plugin's blur option and release cadence were not visible in
  the captured README; confirm in #24 before building.

## Benchmark (issue #22)

`tools/bench_ai.py` (not shipped in the wheel; `pip install -e '.[bench]'`) runs the candidates over a RECORDED
clip of each rig, never a live device. Per model and backend it reports p50 and p95 milliseconds per frame
(resize, channel order and scaling included), CPU as a percentage of one core at 30 fps (CPU time per frame x
fps), Arc busy percent from `intel_gpu_top`, and the first-load compile time with OpenVINO `CACHE_DIR` (cold, then
cached, which is what a restarted StreamStack pays). A backend or device that is missing (no `GPU.0`, no
`intel_gpu_top`, an operator the plugin rejects) is listed as skipped with the reason, not as an error. Models are
fetched with pinned URLs and SHA-256 checks, like YuNet; `python tools/bench_ai.py --list-models` prints each
licence and checksum. Latency only: the outputs are not decoded, so this says nothing about mask or face quality.

| Candidate | Backends | File | Licence |
| --- | --- | --- | --- |
| MediaPipe Meet segmentation (`mediapipe-meet`) | ort-cpu, ov-cpu, ov-gpu | the background-removal plugin `mediapipe.onnx` | Apache-2.0 |
| PP-HumanSeg (`pphumanseg`) | same | the background-removal plugin `pphumanseg_fp32.onnx` | Apache-2.0 |
| SINet (`sinet`) | same | the background-removal plugin `SINet_Softmax_simple.onnx` | MIT |
| YuNet 2023mar (`yunet-2023mar`) | same | OpenCV Zoo `face_detection_yunet_2023mar.onnx` | MIT |
| BlazeFace (`blazeface`) | same | `garavv/blazeface-onnx` `blaze.onnx` (benchmark only) | not stated upstream; network is Apache-2.0 |

Decision gates this table settles (the thresholds are the ones fixed above, before measuring):

| Gate | Passes when | Status |
| --- | --- | --- |
| Blur stays on the CPU (#24) | `mediapipe-meet` on `ort-cpu` is PASS on both rigs | OPEN: no recorded numbers yet |
| Arc build of the blur is worth carrying (#26) | `ov-gpu` is PASS where `ort-cpu` is FAIL, or cuts CPU % at equal p95 | OPEN: no recorded numbers yet |
| Auto-framing detector runs on Arc (#23) | `ov-gpu` p95 beats `ov-cpu` and is PASS; else `ov-cpu` stays | OPEN: no recorded numbers yet |
| DG2/A380 is usable through OpenVINO | `ov-gpu` rows are not skipped and `GPU.0` is the Arc, not the iGPU | OPEN: needs a run on the rig |

### Running it on a quiet host

The numbers below are SYNTHETIC PLACEHOLDERS until these steps are done. Do them with no call in progress and the compositor
stopped, because the recorder opens the camera and the GPU runs must not compete with a live encode:

```sh
tools/record_clip.sh --confirm-no-call /dev/video42 canon.mkv                 # Canon, 1056x704, 20 s
tools/record_clip.sh --confirm-no-call /dev/video0 camlink.mkv 1920x1080 30   # Cam Link, 1080p, 20 s
python tools/bench_ai.py --fetch --clip canon=canon.mkv --clip camlink=camlink.mkv \
    --json bench-ai.json --update-doc docs/hardware-ai-evaluation.md
```

Check `openvino_devices` in the JSON first: with the i7-14700's iGPU as well as the Arc, `GPU.0` may be the iGPU
(pass `--gpu-device GPU.1` and `--gpu-top-device drm:/dev/dri/cardN` for the Arc). `intel_gpu_top` needs root or
`CAP_PERFMON`. A GPU run saturates the device back to back, so its busy percent is an upper bound for 30 fps.
Then set the gate statuses above from the table and commit the result. Issue #22 closes only when it has real
numbers.

<!-- bench-ai:begin -->
<!-- bench-ai:kind=synthetic -->
> **SYNTHETIC PLACEHOLDER. These are not measurements of the rigs.** They come from generated frames on
> the CPU of whatever machine ran the harness (`--synthetic`), only prove the harness works, and decide
> nothing. The gate column is `n/a` on purpose. Replace them by running the quiet-host steps below.

Host: Intel(R) Core(TM) i7-14700, 28 logical CPUs; OpenVINO 2026.4.1, ONNX Runtime 1.31.0; OpenVINO devices: CPU, GPU. 20 timed frames after 3 warm-up, 2 CPU threads, CPU cost scaled to 30 fps.

**synthetic** (640x360, generated)

| Model | Backend | p50 ms | p95 ms | Infer p50 ms | CPU % of 1 core | GPU busy % | Compile cold / cached ms | First frame ms | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mediapipe-meet | ort-cpu | 3.35 | 3.64 | 2.92 | 254.9 | - | 10 (no cache) | 14.7 | n/a |
| mediapipe-meet | ov-cpu | 1.16 | 1.25 | 0.94 | 6.4 | - | 132 / 35 | 5.6 | n/a |
| pphumanseg | ort-cpu | 6.76 | 6.99 | 6.49 | 40.7 | - | 12 (no cache) | 8.2 | n/a |
| pphumanseg | ov-cpu | 6.42 | 6.80 | 6.13 | 37.5 | - | 102 / 28 | 7.5 | n/a |
| sinet | ort-cpu | 15.68 | 16.35 | 14.98 | 96.4 | - | 9 (no cache) | 18.6 | n/a |
| sinet | ov-cpu | 8.25 | 8.52 | 7.58 | 48.1 | - | 124 / 36 | 10.9 | n/a |
| yunet-2023mar | ort-cpu | 6.02 | 7.26 | 4.85 | 43.8 | - | 6 (no cache) | 8.7 | n/a |
| yunet-2023mar | ov-cpu | 7.38 | 7.84 | 6.15 | 44.6 | - | 80 / 18 | 9.9 | n/a |
| blazeface | ort-cpu | 0.94 | 1.00 | 0.80 | 5.7 | - | 7 (no cache) | 1.8 | n/a |
| blazeface | ov-cpu | - | - | - | - | - | - | - | skipped: OpenVINO could not compile blazeface_garavv_a16bb9a.onnx for CPU: Exception from src/inference/src/cpp/core.cpp:117: ... Unexpected: CPU plug-in doesn't support Squeeze operation with dynamic rank.... |

Gate: p95 under 8 ms, under 1 percent total CPU (CPU % of one core divided by the logical CPUs), slowest frame under one frame period. `ov-gpu` runs on the configured OpenVINO GPU device; check the device name in the JSON, because on a host with an iGPU as well as the Arc the Arc may be `GPU.1`.
<!-- bench-ai:end -->

## Ranking (RICE)

Reach = share of calls and properties benefiting (0 to 3), Impact = 0.25 to 3,
Confidence = fraction, Effort = person-weeks. Score = R x I x C / E. These are
judgment scores, not measurements.

| Rank | Candidate | R | I | C | E | Score |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | DeepFilterNet in the voice chain | 3 | 1 | 0.8 | 0.5 | 4.8 |
| 2 | the background-removal plugin blur, CPU | 3 | 2 | 0.8 | 1.5 | 3.2 |
| 3 | StreamStack-side auto-framing (OpenVINO detector + the compositor's websocket crop) | 3 | 2 | 0.7 | 2 | 2.1 |
| 4 | the background-removal plugin on Arc via ORT OpenVINO | 3 | 0.5 | 0.4 | 3 | 0.2 |
| 5 | the face-tracker plugin | 2 | 1 | 0.4 | 1 | 0.8 |
| 6 | Intel OpenVINO the compositor plugins | 2 | 1 | 0.1 | 3 | 0.07 |
| 7 | Eye-contact / AI relight | 1 | 1 | 0.1 | 6 | 0.02 |

DeepFilterNet scores first, but it is a half-week option that does not use
the GPU and does not answer #19, so it ships alongside. The verdict list above
is ordered by value to #19. For the video work, blur on CPU is the cheapest win and auto-framing is the one that needs
new StreamStack code. Order of work: #22 benchmark, then #24 and #23 in
parallel, #25 anytime, #26 only if gated in.

## Design 1: auto-framing (issue #23)

Component: new `streamstack/framing.py`, a thread owned by the console's
`VideoController` (it already holds the compositor's websocket connection).

1. Input: `GetSourceScreenshot` of the camera source at 320 px wide, 8 to 10
   fps. This is the same path the console previews use, so no extra device
   open and no capture on the camera node.
2. Detect: YuNet or BlazeFace (ONNX, converted to OpenVINO IR) with
   `ov.Core().compile_model(model, "AUTO:GPU.0,CPU")` and `CACHE_DIR` under
   `XDG_CACHE_HOME/streamstack`. Largest face wins; lose it for 1.5 s and hold,
   then ease back to the full frame.
3. Smooth: One-Euro filter on centre and size, dead zone of 4 percent of the
   frame so the picture does not breathe, maximum zoom 1.6x (the Canon feed is
   only 1056x704, so tighter than that is visibly soft).
4. Apply: `SetSceneItemTransform` (crop and bounds) on the camera item, or a
   Crop/Pad filter on the source updated with `SetSourceFilterSettings`. Both
   happen in the compositor's compositor, so the frame path gains no latency.
5. Console: Camera page toggle "Auto-frame", presets Tight / Medium / Wide,
   Hold (freeze the crop), and a state field `framing: off | tracking | held |
   no-face | unavailable` in `/api/state`.
6. Fallback with no Arc GPU: the same model on OpenVINO CPU (320x240, 8 fps is
   a small slice of one P-core, estimate under 2 percent); if `openvino` or the
   model file is missing the toggle reports `unavailable`, the crop resets to
   full frame, and a toast explains. Never fail the program feed.
7. Same equipment at several properties: model and thresholds are per-source
   settings in the rig profile (#20), because the Canon and Cam Link differ in
   field of view.

## Design 2: background blur (issues #24, #26)

Component: the background-removal plugin 1.4.1 (upstream release `.forky_amd64.deb`, installs to
`/usr/local`) installed by the workstation role (Curiosity-Corp/catalog#100; StreamStack itself never
installs it). Implemented in #24, with the keys below read from the plugin source at tag 1.4.1.

Filter in `scenes.json`, on the camera source, after the existing Lighting &
Color filter and disabled by default:

| Setting | Value |
| --- | --- |
| Filter name | `Background Blur` (StreamStack's handle), kind `background_removal` |
| `model_select` | `models/mediapipe.with_runtime_opt.ort` (MediaPipe Meet; the input size is fixed by the model, there is no resolution key) |
| `useGPU` / `numThreads` | `cpu` / `2` (README: two threads work best) |
| `mask_every_x_frames` | `2` (Balanced); the plugin has no fps key, so 30 fps camera gives a 15 Hz mask, with the mask reused in between |
| `enable_image_similarity` / `image_similarity_threshold` | on / 35 dB PSNR: skip inference when the picture is nearly unchanged |
| `temporal_smooth_factor` | `0.85` |
| `blur_background` | `8`, an integer Kawase pass count 1 to 20 (not a percentage); 0 means "remove the background" and is never written |
| Edge | `enable_threshold` on at `0.5`, `contour_filter` `0.05`, `smooth_contour` `0.3`, `feather` `0.05` (kernel int(40 x feather) = 2 px) |
| Other | `enable_focal_blur` off, `stop_when_source_is_inactive` on |

The Console control is a toggle, a strength fader (`blur_background`) and a Light / Balanced / Smooth switch
(`mask_every_x_frames` 3 / 2 / 1). A per-frame cost guard reads the compositor's `GetStats` and flags the blur when it
adds 8 ms or more of frame time, 1 percent or more CPU, or any dropped frame (docs/console.md, "Background blur
notes"). Upstream has no OpenVINO device value, so the Arc path below is a carried build, not a setting.

Console: Camera page toggle "Blur background" plus a strength fader and a smoothness switch, and a compact
"Blur" pad on Live, via `CreateSourceFilter`, `SetSourceFilterEnabled` and `SetSourceFilterSettings`. Presence is
detected from the plugin files and `GetSourceFilterKindList`; if the plugin is absent the Camera card shows an
install hint, the Live pad is hidden and the health check warns.

Fallback with no Arc GPU: this is the default path, CPU only. If the benchmark
(#22) shows the CPU misses the gate, #26 carries an ORT OpenVINO build with
`device_type=GPU.0` and falls back to CPU if the GPU plugin fails to load.

## Voice (issue #25)

StreamStack generates and runs its own PipeWire filter-chain (`docs/audio-chain.md`). DeepFilterNet is the default
denoise stage and RNNoise the fallback (about 10 ms against about 40 ms of delay). Neither engine uses the Arc.

Shipped: see `docs/voice-denoise.md` for the measured result and the plugin installation.

## Risks

- OpenVINO on DG2 for these small models may be slower than CPU because of
  upload and sync overhead; the benchmark decides, not this document.
- Upstream plugin churn; pin the plugin build by commit in CI.
- Face detection on the Canon's soft live view in dim rooms; the hold-on-lost
  rule and per-property thresholds mitigate it.

## Follow-up issues

- #22 Offline benchmark harness (recorded clips only)
- #23 Auto-framing, StreamStack-side
- #24 Background blur, the background-removal plugin CPU
- #25 DeepFilterNet option
- #26 Stretch: OpenVINO build of the background-removal plugin for Arc
