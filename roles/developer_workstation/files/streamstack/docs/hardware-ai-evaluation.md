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
licence and checksum. The cost table is latency only. `--edge-quality` (with `--clip`, never generated frames) adds a
mask comparison for the segmenters whose output it can decode: the share of the frame called person, the width of the
soft band across the outline in frame pixels (smaller is crisper) and the overlap (IoU) with PP-HumanSeg, the segmenter
the blur ships with. There is no ground truth, so it ranks candidates against each other and does not say which one is
right; look at the masks too.

| Candidate | Backends | File | Licence |
| --- | --- | --- | --- |
| MediaPipe Meet segmentation (`mediapipe-meet`) | ort-cpu, ov-cpu, ov-gpu | the background-removal plugin `mediapipe.onnx` | Apache-2.0 |
| PP-HumanSeg (`pphumanseg`) | same | the background-removal plugin `pphumanseg_fp32.onnx` | Apache-2.0 |
| MediaPipe selfie segmenter, landscape 144x256 (`mediapipe-selfie-landscape`) | same | `onnx-community/mediapipe_selfie_segmentation_landscape` `onnx/model.onnx` at a pinned commit (the ONNX export of Google's `selfie_segmenter_landscape`; the official `.tflite` does not load in OpenVINO, it needs a custom `Convolution2DTransposeBias` op) | Apache-2.0 |
| SINet (`sinet`) | same | the background-removal plugin `SINet_Softmax_simple.onnx` | MIT |
| YuNet 2023mar (`yunet-2023mar`) | same | OpenCV Zoo `face_detection_yunet_2023mar.onnx` | MIT |
| BlazeFace (`blazeface`) | same | `garavv/blazeface-onnx` `blaze.onnx` (benchmark only) | not stated upstream; network is Apache-2.0 |

Decision gates this table settles (the thresholds are the ones fixed above, before measuring):

| Gate | Passes when | Status |
| --- | --- | --- |
| Blur stays on the CPU (#24) | `mediapipe-meet` on `ort-cpu` is PASS on both rigs | NOT MET on `ort-cpu` at Canon size (p95 3.3 to 3.7 ms but 33 % of one core = 1.2 % of 28 CPUs; Cam Link PASS at 12 %); `ov-cpu` PASS on both rigs (6.6 %). The shipped blur runs on the Arc (see below). Measured 2026-10-27, dm313red |
| Arc build of the blur is worth carrying (#26) | `ov-gpu` is PASS where `ort-cpu` is FAIL, or cuts CPU % at equal p95 | MET: `ov-gpu` PASS on both rigs where `ort-cpu` FAILs at Canon size; CPU 33 % to 3.5 % of one core at equal-or-better p95 (2.5 to 3.0 ms). Measured 2026-10-27, dm313red |
| Auto-framing detector runs on Arc (#23) | `ov-gpu` p95 beats `ov-cpu` and is PASS; else `ov-cpu` stays | MET with a caveat: YuNet `ov-gpu` p95 5.4 to 6.4 ms against `ov-cpu` 8.4 to 9.0 ms (FAIL) in five of six rows, CPU 15 % against 47 % of one core. The sixth (Cam Link, run 3) failed at 10.8 ms p95 from one slow-frame burst on a busy desk. Shipped in !53. Measured 2026-10-27, dm313red |
| DG2/A380 is usable through OpenVINO | `ov-gpu` rows are not skipped and `GPU.0` is the Arc, not the iGPU | MET on dm313red: every `ov-gpu` row ran on `GPU (Intel(R) Arc(TM) A380 Graphics (dGPU))`; OpenVINO lists a lone `GPU` there. The deskmeet1 layout (iGPU as `GPU.0`, Arc as `GPU.1`) is covered by the harness's discrete-first choice and unit tests but was not measured. Measured 2026-10-27 |

### MediaPipe selfie segmenter against PP-HumanSeg (#147)

Cost, measured 2026-10-28 on dm313red (OpenVINO 2026.4.1, onnxruntime 1.31.1, nice 19, generated frames at each rig's
size, 150 timed frames, a live desk so the first row of a run is the noisiest):

| Model | Backend | Canon p50 / p95 ms | Canon CPU % of 1 core | Cam Link p50 / p95 ms | Cam Link CPU % |
| --- | --- | --- | --- | --- | --- |
| mediapipe-selfie-landscape | ov-gpu | 1.30 / 2.05 | 2.8 | 1.34 / 1.44 | 2.6 |
| mediapipe-selfie-landscape | ov-cpu | 0.95 / 1.09 | 5.1 | 0.97 / 1.11 | 5.2 |
| pphumanseg (shipped) | ov-gpu | 1.98 / 2.56 | 3.1 | 2.13 / 3.05 | 4.1 |
| pphumanseg (shipped) | ov-cpu | 6.53 / 7.23 | 37.8 | 6.14 / 6.40 | 35.5 |

The selfie model is cheaper (the same 36,864 input pixels as PP-HumanSeg's 192x192, but a lighter network)
and is the only one that runs well on the CPU, so it passes the gates on every backend. Mask quality at the edge is the
open half: no recorded clip with a person is kept in the repository, so run
`python tools/bench_ai.py --clip canon=canon.mkv --models pphumanseg,mediapipe-selfie-landscape --edge-quality`
on a clip recorded with no call in progress and paste the table on #147 before switching the shipped model.

### Running it on a quiet host

The numbers below are from the real Arc A380 on dm313red, run at nice 19 on generated frames at each rig's real size
(`--synthetic --measured`), so no camera, loopback device or microphone was opened and no call was in progress (checked
with `fuser /dev/video42 /dev/video43` and `pactl list source-outputs` first). Latency and load do not depend on the
picture; mask and face quality are not measured here. To replace them with numbers from recorded clips (a recorded
block always replaces a rig-size one, never the other way round), record the clips with no call in progress and
the compositor stopped, because the recorder opens the camera:

```sh
tools/record_clip.sh --confirm-no-call /dev/video42 canon.mkv                 # Canon, 1056x704, 20 s
tools/record_clip.sh --confirm-no-call /dev/video0 camlink.mkv 1920x1080 30   # Cam Link, 1080p, 20 s
python tools/bench_ai.py --fetch --clip canon=canon.mkv --clip camlink=camlink.mkv \
    --json bench-ai.json --update-doc docs/hardware-ai-evaluation.md
```

`ov-gpu` picks the discrete Intel GPU by device type (the same choice auto-framing makes), so an iGPU listed as `GPU.0`
does not matter; `--gpu-device GPU.1` overrides it. `intel_gpu_top` needs `CAP_PERFMON`: pass `--gpu-top-sudo` on a
host with passwordless sudo. It is pointed at the PCI id of the GPU OpenVINO ran on. A GPU run saturates the device
back to back, so its busy percent is an upper bound for 30 fps.

<!-- bench-ai:begin -->
<!-- bench-ai:kind=rig-size -->
Measured 2026-10-27 on dm313red, on the hardware, with generated frames at each rig's real size (no live device was opened). This is latency and load only: the models' run time does not depend on the picture, but nothing here says how good a mask or a face box is. Re-run with `--clip` once clips are recorded; recorded numbers replace these.

Host: Intel(R) Core(TM) i7-14700, 28 logical CPUs; OpenVINO 2026.4.1, ONNX Runtime 1.31.1; OpenVINO devices: CPU, GPU. 300 timed frames after 10 warm-up, 2 CPU threads, CPU cost scaled to 30 fps.

**canon** (1056x704, generated)

| Model | Backend | p50 ms | p95 ms | Infer p50 ms | CPU % of 1 core | GPU busy % | Compile cold / cached ms | First frame ms | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mediapipe-meet | ort-cpu | 1.93 | 3.67 | 1.69 | 32.6 | - | 11 (no cache) | 15.8 | FAIL |
| mediapipe-meet | ov-cpu | 1.21 | 1.37 | 0.98 | 6.6 | - | 128 / 33 | 3.0 | PASS |
| mediapipe-meet | ov-gpu | 1.65 | 2.97 | 1.41 | 3.5 | 41 | 317 / 135 | 5.7 | PASS |
| pphumanseg | ort-cpu | 7.10 | 8.05 | 6.75 | 43.0 | - | 16 (no cache) | 8.3 | FAIL |
| pphumanseg | ov-cpu | 6.37 | 6.98 | 6.05 | 37.0 | - | 110 / 26 | 6.8 | FAIL |
| pphumanseg | ov-gpu | 1.92 | 2.89 | 1.65 | 3.3 | 49 | 243 / 162 | 5.5 | PASS |
| sinet | ort-cpu | 15.24 | 17.41 | 14.52 | 94.7 | - | 9 (no cache) | 16.8 | FAIL |
| sinet | ov-cpu | 7.68 | 8.63 | 7.03 | 46.0 | - | 117 / 33 | 9.0 | FAIL |
| sinet | ov-gpu | 3.23 | 5.62 | 2.65 | 7.9 | 45 | 314 / 128 | 9.8 | PASS |
| yunet-2023mar | ort-cpu | 5.93 | 7.56 | 4.73 | 44.5 | - | 6 (no cache) | 10.3 | FAIL |
| yunet-2023mar | ov-cpu | 7.32 | 8.53 | 6.04 | 46.0 | - | 82 / 17 | 10.0 | FAIL |
| yunet-2023mar | ov-gpu | 3.62 | 5.68 | 2.64 | 14.7 | 40 | 207 / 100 | 9.5 | PASS |
| blazeface | ort-cpu | 1.04 | 1.22 | 0.88 | 6.3 | - | 8 (no cache) | 2.0 | PASS |
| blazeface | ov-cpu | - | - | - | - | - | - | - | skipped: OpenVINO could not compile blazeface_garavv_a16bb9a.onnx for CPU: Exception from src/inference/src/cpp/core.cpp:117: ... Unexpected: CPU plug-in doesn't support Squeeze operation with dynamic rank.... |
| blazeface | ov-gpu | - | - | - | - | - | - | - | skipped: OpenVINO could not compile blazeface_garavv_a16bb9a.onnx for GPU: Exception from src/inference/src/cpp/core.cpp:117: ... Check 'rank().is_static()' failed at src/core/include/openvino/core/partial_... |

**camlink** (1920x1080, generated)

| Model | Backend | p50 ms | p95 ms | Infer p50 ms | CPU % of 1 core | GPU busy % | Compile cold / cached ms | First frame ms | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mediapipe-meet | ort-cpu | 1.96 | 2.38 | 1.70 | 12.0 | - | 10 (no cache) | 2.4 | PASS |
| mediapipe-meet | ov-cpu | 1.24 | 1.62 | 0.99 | 6.8 | - | 90 / 31 | 3.1 | PASS |
| mediapipe-meet | ov-gpu | 1.72 | 2.94 | 1.44 | 3.8 | 38 | 134 / 39 | 3.9 | PASS |
| pphumanseg | ort-cpu | 7.48 | 8.78 | 7.06 | 45.3 | - | 17 (no cache) | 7.9 | FAIL |
| pphumanseg | ov-cpu | 6.35 | 6.98 | 5.98 | 36.7 | - | 95 / 30 | 8.5 | FAIL |
| pphumanseg | ov-gpu | 1.95 | 3.28 | 1.64 | 3.3 | 49 | 150 / 43 | 5.3 | PASS |
| sinet | ort-cpu | 14.94 | 16.62 | 14.21 | 93.1 | - | 10 (no cache) | 14.8 | FAIL |
| sinet | ov-cpu | 7.98 | 8.70 | 7.23 | 47.3 | - | 126 / 35 | 10.8 | FAIL |
| sinet | ov-gpu | 3.21 | 4.99 | 2.60 | 7.6 | 46 | 175 / 42 | 6.5 | PASS |
| yunet-2023mar | ort-cpu | 6.08 | 7.66 | 4.72 | 47.3 | - | 12 (no cache) | 11.4 | FAIL |
| yunet-2023mar | ov-cpu | 7.36 | 8.44 | 5.99 | 47.5 | - | 71 / 18 | 9.2 | FAIL |
| yunet-2023mar | ov-gpu | 5.09 | 10.77 | 3.42 | 20.3 | 26 | 123 / 31 | 6.6 | FAIL |
| blazeface | ort-cpu | 1.03 | 1.30 | 0.85 | 6.4 | - | 8 (no cache) | 1.3 | PASS |
| blazeface | ov-cpu | - | - | - | - | - | - | - | skipped: OpenVINO could not compile blazeface_garavv_a16bb9a.onnx for CPU: Exception from src/inference/src/cpp/core.cpp:117: ... Unexpected: CPU plug-in doesn't support Squeeze operation with dynamic rank.... |
| blazeface | ov-gpu | - | - | - | - | - | - | - | skipped: OpenVINO could not compile blazeface_garavv_a16bb9a.onnx for GPU: Exception from src/inference/src/cpp/core.cpp:117: ... Check 'rank().is_static()' failed at src/core/include/openvino/core/partial_... |

Gate: p95 under 8 ms, under 1 percent total CPU (CPU % of one core divided by the logical CPUs), slowest frame under one frame period. `ov-gpu` runs on the discrete Intel GPU by default (the same choice auto-framing makes), so the Arc is picked whether OpenVINO calls it `GPU.0` or `GPU.1`. `ov-gpu` ran on: GPU (Intel(R) Arc(TM) A380 Graphics (dGPU)).
<!-- bench-ai:end -->

### Three runs of the same matrix (2026-10-27, dm313red)

The block above is the third run. The same matrix three times in a row, 300 timed frames each, to show the spread: a row's verdict is stable except where a single slow frame decides it.

| Rig | Model | Backend | p95 ms (3 runs) | CPU % of 1 core (3 runs) | Arc busy % (3 runs) | Gate (3 runs) |
| --- | --- | --- | --- | --- | --- | --- |
| canon | mediapipe-meet | ort-cpu | 3.54 / 3.33 / 3.67 | 33.3 / 33.2 / 32.6 | - | FAIL / FAIL / FAIL |
| camlink | mediapipe-meet | ort-cpu | 2.14 / 2.29 / 2.38 | 11.3 / 11.6 / 12.0 | - | PASS / PASS / PASS |
| canon | mediapipe-meet | ov-cpu | 1.45 / 1.42 / 1.37 | 6.6 / 6.6 / 6.6 | - | PASS / PASS / PASS |
| camlink | mediapipe-meet | ov-cpu | 1.58 / 1.58 / 1.62 | 6.9 / 7.1 / 6.8 | - | PASS / PASS / PASS |
| canon | mediapipe-meet | ov-gpu | 2.53 / 3.29 / 2.97 | 3.1 / 4.0 / 3.5 | 43 / 35 / 41 | PASS / PASS / PASS |
| camlink | mediapipe-meet | ov-gpu | 2.58 / 2.49 / 2.94 | 3.3 / 3.2 / 3.8 | 41 / 41 / 38 | PASS / PASS / PASS |
| canon | pphumanseg | ort-cpu | 7.26 / 7.14 / 8.05 | 39.9 / 39.1 / 43.0 | - | FAIL / FAIL / FAIL |
| camlink | pphumanseg | ort-cpu | 6.99 / 7.67 / 8.78 | 39.2 / 39.7 / 45.3 | - | FAIL / FAIL / FAIL |
| canon | pphumanseg | ov-cpu | 7.04 / 6.84 / 6.98 | 37.0 / 36.4 / 37.0 | - | FAIL / FAIL / FAIL |
| camlink | pphumanseg | ov-cpu | 7.59 / 7.10 / 6.98 | 39.4 / 38.3 / 36.7 | - | FAIL / FAIL / FAIL |
| canon | pphumanseg | ov-gpu | 2.93 / 3.59 / 2.89 | 3.3 / 3.6 / 3.3 | 49 / 47 / 49 | PASS / PASS / PASS |
| camlink | pphumanseg | ov-gpu | 3.35 / 3.16 / 3.28 | 3.6 / 3.5 / 3.3 | 47 / 48 / 49 | PASS / PASS / PASS |
| canon | sinet | ort-cpu | 16.56 / 16.48 / 17.41 | 92.8 / 92.3 / 94.7 | - | FAIL / FAIL / FAIL |
| camlink | sinet | ort-cpu | 17.24 / 16.01 / 16.62 | 94.3 / 91.1 / 93.1 | - | FAIL / FAIL / FAIL |
| canon | sinet | ov-cpu | 8.68 / 8.68 / 8.63 | 45.9 / 46.5 / 46.0 | - | FAIL / FAIL / FAIL |
| camlink | sinet | ov-cpu | 8.41 / 8.72 / 8.70 | 45.9 / 46.3 / 47.3 | - | FAIL / FAIL / FAIL |
| canon | sinet | ov-gpu | 4.65 / 5.41 / 5.62 | 7.5 / 7.5 / 7.9 | 46 / 45 / 45 | PASS / PASS / PASS |
| camlink | sinet | ov-gpu | 5.84 / 5.52 / 4.99 | 9.2 / 8.7 / 7.6 | 41 / 42 / 46 | PASS / PASS / PASS |
| canon | yunet-2023mar | ort-cpu | 6.57 / 8.37 / 7.56 | 41.9 / 47.5 / 44.5 | - | FAIL / FAIL / FAIL |
| camlink | yunet-2023mar | ort-cpu | 7.35 / 7.67 / 7.66 | 46.1 / 48.6 / 47.3 | - | FAIL / FAIL / FAIL |
| canon | yunet-2023mar | ov-cpu | 8.39 / 8.95 / 8.53 | 45.7 / 49.6 / 46.0 | - | FAIL / FAIL / FAIL |
| camlink | yunet-2023mar | ov-cpu | 8.66 / 8.46 / 8.44 | 47.1 / 48.0 / 47.5 | - | FAIL / FAIL / FAIL |
| canon | yunet-2023mar | ov-gpu | 5.47 / 6.26 / 5.68 | 14.3 / 15.5 / 14.7 | 39 / 37 / 40 | PASS / PASS / PASS |
| camlink | yunet-2023mar | ov-gpu | 5.41 / 6.40 / 10.77 | 15.3 / 16.6 / 20.3 | 39 / 36 / 26 | PASS / PASS / FAIL |
| canon | blazeface | ort-cpu | 0.99 / 1.17 / 1.22 | 5.2 / 5.7 / 6.3 | - | PASS / PASS / PASS |
| camlink | blazeface | ort-cpu | 1.06 / 1.04 / 1.30 | 5.5 / 5.5 / 6.4 | - | PASS / PASS / PASS |


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
