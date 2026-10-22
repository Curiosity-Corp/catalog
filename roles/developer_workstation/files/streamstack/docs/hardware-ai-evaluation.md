# Hardware AI evaluation: Arc A380 blur, auto-framing, denoise

Spike for issue #19 (milestone v0.3.0 Hardware-native). Desk research only:
nothing was measured on a live device, nothing was installed, no OBS config
was touched. Every latency figure below is either quoted from a cited source or
labelled **estimate**; the benchmark issue (#22) replaces estimates with
measurements on recorded clips before any default is chosen.

Rig: Intel Arc A380 (DG2, i915, VA-API iHD 26.1.2, libvpl 2.16, OpenCL
`intel-opencl-icd 26.05`), i7-14700 (AVX2 + AVX-VNNI), OBS 32.1.0, program
camera OBS to v4l2loopback to calls. Sources: Canon 50D (~1056x704 MJPEG) and
Cam Link 4K at 1080p.

## Verdict (ordered by value to #19)

1. **Auto-framing: build it inside StreamStack** (#23). A small OpenVINO face
   detector runs beside OBS, off the frame path, and moves a crop through
   obs-websocket. No existing OBS plugin is both maintained and GPU-capable on
   Linux. Added video latency is zero; only the follow lag is tunable.
2. **Background blur: obs-backgroundremoval as an OBS filter, CPU first**
   (#24), Arc GPU through a carried OpenVINO build only if the CPU misses its
   budget (#26). The plugin is the only maintained Linux option, but upstream
   documents no OpenVINO backend.
3. **Voice: keep RNNoise as default, offer DeepFilterNet** through Easy Effects
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
| **Intel OpenVINO plugins for OBS** (Smart Framing, Background Concealment) | **Dead end.** Repo archived 17 Aug 2026; Intel will not accept patches. Linux is build-from-source only, tested on Ubuntu 20.04/22.04 and OBS 28.1.2, no Arc/DG2 mention ([repo](https://github.com/intel/openvino-plugins-for-obs-studio)). Snap listing exists but could not be fetched ([snap](https://snapcraft.io/openvino-plugins-obs-studio)); its OBS/OpenVINO age is unverified | No numbers published | Source build; snap unverified | Apache-2.0 | Abandoned | Would be an OBS filter, but unmaintained against OBS 32 |
| **obs-backgroundremoval** (royshil) | Official README lists CPU, CUDA, ROCm (deprecated in ORT 1.23), MIGraphX, CoreML; **no OpenVINO** on Linux. Source builds documented for Debian only ([repo](https://github.com/royshil/obs-backgroundremoval/)). Third-party guides mention an OpenVINO backend for Windows; unverified by the project docs | Models are small portrait segmenters (SINet, PP-HumanSeg, MediaPipe Meet, RVM). Estimate: 3 to 8 ms per 256x256 frame on 2 CPU threads; README says 2 threads work best | Build a .deb from source in CI; no apt/snap/flatpak for our OBS (apt OBS 32.1.0) | GPL-3.0 (see note) | Active, widely used | OBS filter on the camera source in `scenes.json` |
| **ONNX Runtime OpenVINO EP** (route to put obs-backgroundremoval on Arc) | `device_type` accepts `GPU`, `GPU.0`; ORT 1.24.1 pairs with OpenVINO 2025.4.1; Linux wheels are Python only ([docs](https://onnxruntime.ai/docs/execution-providers/OpenVINO-ExecutionProvider.html)) | As OpenVINO row | C++ needs a source build of ORT with OpenVINO | MIT | Medium | Carried patch to the plugin's ORT dependency (#26) |
| **obs-face-tracker** (norihiro) | Linux source build; dlib HOG/CNN, **CPU only, no GPU path**; README lists high CPU use, memory growth and jitter; one face only ([repo](https://github.com/norihiro/obs-face-tracker)) | Estimate: 10 to 30 ms (HOG), CNN far worse | Source build | GPLv2; the 68-point landmark model has a non-free license | Slow, 46 open issues | OBS filter; rejected on CPU cost and jitter |
| **DeepFilterNet** (voice) | Rust, runs on CPU; LADSPA plugin with PipeWire filter-chain; Easy Effects exposes it as "Deep noise remover" ([repo](https://github.com/Rikorose/DeepFilterNet), [Easy Effects use](https://discuss.getsol.us/d/10034-using-deepfilternet-with-easy-effects)) | 40 ms algorithmic delay; RTF 0.11 vs 0.03 for RNNoise on a laptop i5-8250U ([paper](https://arxiv.org/pdf/2205.05474)). Far below real time limits on an i7-14700 | LADSPA, pip, binaries; Easy Effects already installed (8.1.2) | MIT or Apache-2.0 | Good | Easy Effects preset, already StreamStack's voice stage |
| **RNNoise** (voice) | CPU; in Easy Effects "Noise reduction" and in [noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice) (LV2/LADSPA/VST) | About 10 ms added ([overview](https://www.forasoft.com/learn/ai-for-video-engineering/articles-ai/real-time-noise-suppression-krisp-rnnoise-deepfilternet)); RTF 0.03 | Easy Effects | BSD (RNNoise); GPL-3.0 (the plugin wrapper) | Mature, older model | Already in the voice presets |
| **Eye-contact, relight** | Only vendor stacks on NVIDIA RTX (e.g. StreamFX's NVIDIA filters, [mirror](https://bytes.keithhacks.cyou/etc/obs-StreamFX/src/commit/47a22ce46298ee17fc02210ea4d74b4af52326e3/source/filters/filter-nv-face-tracking.cpp)); nothing found for Arc/Linux | n/a | n/a | n/a | n/a | Skip |

Notes and caveats:

- The OBS virtual camera on Linux still needs v4l2loopback; a PipeWire
  virtual camera is not a shipped alternative yet
  ([discussion](https://ideasonboard.com/news/pipewire-is-the-new-v4l2loopback)).
  Effects must therefore live before the virtual camera, inside OBS or as OBS
  transforms, not as a separate device between OBS and the call.
- GPL-3.0 plugins are fine to install beside StreamStack but must not be
  linked into it; StreamStack only talks to them over obs-websocket.
- I could not read OpenVINO's current GPU page body (the fetch returned
  navigation only). The DG2/A380 claim therefore rests on the "Arc GPU
  Series" system-requirements entry plus this host's working OpenCL stack, and
  is a decision gate in #22.
- obs-backgroundremoval's blur option and release cadence were not visible in
  the captured README; confirm in #24 before building.

## Ranking (RICE)

Reach = share of calls and properties benefiting (0 to 3), Impact = 0.25 to 3,
Confidence = fraction, Effort = person-weeks. Score = R x I x C / E. These are
judgment scores, not measurements.

| Rank | Candidate | R | I | C | E | Score |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | DeepFilterNet option in Easy Effects | 3 | 1 | 0.8 | 0.5 | 4.8 |
| 2 | obs-backgroundremoval blur, CPU | 3 | 2 | 0.8 | 1.5 | 3.2 |
| 3 | StreamStack-side auto-framing (OpenVINO detector + obs-websocket crop) | 3 | 2 | 0.7 | 2 | 2.1 |
| 4 | obs-backgroundremoval on Arc via ORT OpenVINO | 3 | 0.5 | 0.4 | 3 | 0.2 |
| 5 | obs-face-tracker | 2 | 1 | 0.4 | 1 | 0.8 |
| 6 | Intel OpenVINO OBS plugins | 2 | 1 | 0.1 | 3 | 0.07 |
| 7 | Eye-contact / AI relight | 1 | 1 | 0.1 | 6 | 0.02 |

DeepFilterNet scores first, but it is a half-week option that does not use
the GPU and does not answer #19, so it ships alongside. The verdict list above
is ordered by value to #19. For the video work, blur on CPU is the cheapest win and auto-framing is the one that needs
new StreamStack code. Order of work: #22 benchmark, then #24 and #23 in
parallel, #25 anytime, #26 only if gated in.

## Design 1: auto-framing (issue #23)

Component: new `streamstack/framing.py`, a thread owned by the console's
`VideoController` (it already holds the obs-websocket connection).

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
   happen in OBS's compositor, so the frame path gains no latency.
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

Component: obs-backgroundremoval 1.4.1 (upstream release `.forky_amd64.deb`, installs to
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
(`mask_every_x_frames` 3 / 2 / 1). A per-frame cost guard reads OBS's `GetStats` and flags the blur when it
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

Keep the existing Easy Effects chain and RNNoise as default for latency
(about 10 ms against about 40 ms). Add a Deep noise remover preset variant,
verify the effect exists in the packaged Easy Effects first, and give the
Console an A/B pad. Neither engine uses the Arc.

## Risks

- OpenVINO on DG2 for these small models may be slower than CPU because of
  upload and sync overhead; the benchmark decides, not this document.
- Upstream plugin churn; pin the plugin build by commit in CI.
- Face detection on the Canon's soft live view in dim rooms; the hold-on-lost
  rule and per-property thresholds mitigate it.

## Follow-up issues

- #22 Offline benchmark harness (recorded clips only)
- #23 Auto-framing, StreamStack-side
- #24 Background blur, obs-backgroundremoval CPU
- #25 DeepFilterNet option
- #26 Stretch: OpenVINO build of obs-backgroundremoval for Arc
