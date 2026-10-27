# Changelog

All notable changes are recorded here. The project follows
[semantic versioning](https://semver.org).

## [Unreleased]

## [0.0.12] - 2026-10-27

### Fixed
- The shipped Wave XLR drop-in (`52-streamstack-wave-xlr-capture-hold.conf`) never applied: WirePlumber rejected it
  ("section 'monitor.alsa.rules' has no value") because the node-name pattern used a `\.` escape, which is not valid in
  its string syntax. The pattern uses `[.]`, and a test now refuses a backslash in any shipped drop-in.

### Fixed
- `test_pinning_an_encoder_chooses_its_codec` failed once on main (pipeline 96014, `KeyError: 'gen'`): the test harness
  fed the fake audio encoder's header as soon as the service registered the child, before the service had attached the
  child to its generation and started it. A real process cannot produce output before `start()`, so the harness now
  waits for `start()` too. Product code is unchanged (it sets the generation before starting the child).

## [0.0.11] - 2026-10-27

### Fixed
- The e2e job failed once on a 15 s socket timeout under CPU starvation (the same class as the Console unit tests); the
  e2e HTTP calls use the shared generous timeout too, and the guard test now covers `urlopen` and the e2e directory.

### Added
- The mic watchdog and `streamstack doctor` find digital silence, a lost duplex profile and a wrong default source
  (#125, #126). A microphone whose capture pointers advance but whose samples are exactly zero for 4 s is reported as
  its own fault, `mic-digital-silence` (Console banner "digital silence", `mic.stall_kind`), never as a stall and never
  confused with a quiet room (a live preamp always has a noise floor). `streamstack mic-revive` and the Console's Revive
  mic action (Shift R) first re-write the interface's capture switch and volume through `amixer` (new dependency
  **alsa-utils**, checked by `doctor`), then measure the samples again, and only then escalate to suspend/resume and the
  card profile cycle; the watchdog still never acts on its own. `doctor` fails `hw-audio-duplex` when the Plan's
  interface is attached but its sound card has no input profile, and `mic-revive --reprobe` (explicit, it briefly drops
  every audio stream) restarts WirePlumber and waits for the input to return. `doctor` warns `default-source` when
  PipeWire's default source is not StreamStack Mic (chain running) or the resolved microphone, and the new
  `streamstack default-source` fixes it. For the Wave XLR, StreamStack ships a WirePlumber drop-in
  (`node.always-process` on the capture node, guarding the reported playback-before-capture zeros bug), `doctor` checks
  the effective property, and `tools/xlr_capture_order.py` reproduces the ordering.

## [0.0.10] - 2026-10-27

### Fixed
- The v0.0.8 tag pipeline stopped at secret scanning: gitleaks flagged five fake tokens in the test fixtures. The
  waiver written for v0.0.9 used the `[[allowlists]]` syntax, which the runner's older gitleaks silently ignores, so
  the v0.0.9 tag pipeline stopped at the same place. Nothing was published for either; tags are immutable, so
  the next release carries the content of both.

## [0.0.9] - 2026-10-27

Not released: its tag pipeline stopped at secret scanning (see the entry under the next release).

## [0.0.8] - 2026-10-27

### Added
- DeepFilterNet3 is a CI-built, checksummed, SBOM-listed artifact (generic package `streamstack-ladspa`), installed with
  `streamstack chain install-plugin`, and StreamStack Mic becomes the default microphone while the chain runs (raw
  microphone restored on stop) (#91).
- The voice chain and `streamstack doctor` follow the hardware Plan (#118, #122). The raw microphone and the monitor
  output are the nodes of the interface the Plan found by USB id, in the Console, `streamstack chain`, `mic-check` and
  `mic-revive` (the name rules remain only as the fallback when the probe cannot run). The graph's rate, the interface's
  sample format and the PipeWire quantum come from the interface profile; on the Wave XLR the graph runs at 48 kHz
  because DeepFilterNet cannot run at 96 kHz (measured: no audio comes out) and the system clock is 48 kHz only, and
  `chain status` and `doctor` say why. A card at profile `off` is switched on when its profile names the card profile,
  an unplug and replug re-resolves the microphone, and an unknown interface falls back to the default PipeWire source.
  An interface that is attached but has no source, or no interface at all, makes the chain decline cleanly instead of
  starting on the wrong device. `streamstack doctor` gains a Hardware group (rig header `rig: <id> (selected by
  <reason>)`, each expected device present or absent as required or optional, GPU roles on the right render node, the
  audio interface at its rate and format, capture card modes, loopback labels, and "denied" rows with the fix), plus
  `--hardware` and `--json`; the System page shows it as a Hardware group. The owner chose a Focusrite Scarlett 2i2
  (gen 4 `1235:8219` or gen 3 `1235:8210`) as deskmeet1's DAC (2026-10-24), so the studio-deck rig names it and lists
  this machine's identity hash. While it is not enumerating on USB the doctor shows `Focusrite Scarlett 2i2 ... is not
  enumerating on USB` as a warning with the fix (an owner action: re-seat it in a direct rear USB port with a short
  known-good cable), and the voice chain declines to start.
- Video jobs pick their GPU and cameras from the hardware Plan (#119, #120). The native engine graph, its QSV taps,
  the stream encoders (AV1, HEVC, H.264 over VA-API and QSV), the blur and the Canon decode each take the render node
  of their role (grade, encode, blur, decode) by PCI id, never by node number: on deskmeet1 the Arc (`renderD129`)
  encodes and grades while the UHD 770 (`renderD128`) takes the Canon decode, on dm313red everything stays on the Arc.
  Explicit settings still win (new `STREAMSTACK_STREAM_RENDER_NODE`), no matching profile keeps the generic choice
  (Intel discrete before integrated), and hot-plug renumbering re-resolves. The Cam Link 4K matches both `0fd9:0067`
  and `0fd9:0066` from the capture profile, the Canon is found by its profile id, and each rig profile declares its
  v4l2loopback devices (`video_nr`, `card_label`, `exclusive_caps`); `streamstack loopback status|apply` checks and
  writes them from the profile. `streamstack hardware` and the System page show a "Video in use" card with the node
  per job and the camera and virtual camera state.
- Virtual camera and microphone, decided and diagnosed (ADR 0004). StreamStack Camera stays a v4l2loopback node (the only
  camera path Teams, Zoom and stock Chromium list) and StreamStack Mic stays a PipeWire source; the ADR records what each
  client enumerates and what was proven in Chromium, Edge, Firefox and the Zoom web client on the workstation. The module
  configuration now ships declaratively (`packaging/modprobe.d`, `modules-load.d`, and a udev rule for the `video` group,
  installed by `make install`). `streamstack doctor` and the Console check the camera's name, `exclusive_caps`, access,
  offered format and the mic's description, and the System page has a "Seen by apps" card showing which apps hold the
  camera (open V4L2 files) and the microphone (PipeWire links). `doctor` no longer calls an unreadable camera "no picture
  yet": it says this user cannot open it and what to change. A live Chromium test feeds the node and checks listing,
  1280x720 capture and a WebRTC loop; it skips with its reason where the module is not loaded (CI).
- Hardware profiles and capability probing (ADR 0003, #116, #117, #123). `streamstack.hardware.probe.snapshot()` reads
  what is plugged in (USB, DRM render nodes with PCI id, driver and VA-API entrypoints, ALSA cards and their streams,
  PipeWire cards and profiles, V4L2 capture nodes and formats, v4l2loopback) as a frozen `Snapshot`, each source a
  runner plus a pure parser with its own `ok`, `denied` or `absent` status. `udevadm monitor` drives hot-plug (one
  probe after a quiet second, a change only when the snapshot differs, polling when udevadm is missing). Device and
  rig profiles are validated TOML in `src/streamstack/data/hardware/` (unknown keys rejected, ids unique, schema
  version); the rigs `studio-wave-xlr` and `studio-deck` and a generic Linux fallback are chosen by
  `STREAMSTACK_RIG`, else by which devices are present, with a hashed machine id only as a tie-breaker (the hostname
  is never read). The pure `resolve(snapshot, catalogue, overrides)` returns a `Plan` (GPU roles, encoder, audio
  interface and rate, capture card, loopback pair) with accessors such as `plan.gpu("encode").node_path`. The Console
  state has a `hardware` section and the System page a Hardware card (resolved rig and why, each device present or
  absent, GPU roles, notices, "Probe again"); the demo rig replays recordings of both hosts and can switch between
  them. `streamstack hardware [--json|--identity]` prints the same from a shell. The audio-interface catalogue in
  `hardware/profiles.py` now reads from the same data with unchanged Console behaviour.
- Tests for it run in CI from recorded fixtures of dm313red and deskmeet1 (every parser, both hosts, failure
  injection per source) and a fake sysfs for hot-plug, a missing or denied GPU and the generic fallback.
- WHIP publishing (#108, ADR 0002): a Stream destination of kind `whip` now works on any host with the optional extra
  `streamstack[whip]`. StreamStack's own client POSTs the offer, runs ICE and DTLS-SRTP (aiortc's transports) and sends
  the hardware encoder's H.264, HEVC or AV1 and Opus as RTP, without re-encoding and without an ffmpeg muxer; stop sends
  `DELETE`. Live changes keep the session, a failed connection retries with backoff, a 401, 404 or 405 stops with the
  reason, and the Stream page shows RTT and loss. The bearer token is write-only like a stream key and never on a
  command line. Measured on the Arc A380 at 1080p30: about 1 ms and 1.4 to 2.8 % of one core on top of the encode
  (docs/stream.md lists what was not measured). The probe's "WHIP unavailable: no muxer" is replaced by a real check.
  Covered by unit tests, an end-to-end suite against a real WHIP server, an interop test against a pinned MediaMTX, and
  Playwright tests. New `tools/bench_whip.py`.
- The demo rig has a `lighting.demo.reset` action (refused on real hardware) that puts the lights, the automation settings, the pretend camera and the engine back in one step under the engine's lock, so a test reset is never mistaken for a change by hand. The e2e reset uses it instead of moving the lights through the normal actions and asking the schedule to resume.

- Background blur on the Intel Arc through OpenVINO (#60): the person mask is computed by PP-HumanSeg (Apache-2.0,
  6 MB, about 2 ms alone and 9 ms in the live loop on an A380) and composited inside the engine's one ffmpeg graph, so
  strength and on/off are live commands with no restart. The model is downloaded on first use into the state
  directory against a pinned SHA-256; OpenVINO is the optional extra `streamstack[blur]`. The Blur controls show
  "Running on Arc via OpenVINO" with measured masks per second and milliseconds, and "Unavailable: <reason>" when
  OpenVINO, the model or the GPU is missing. New settings `STREAMSTACK_ENGINE_BLUR` and
  `STREAMSTACK_ENGINE_BLUR_DEVICE`, benchmark `tools/bench_blur.py`, measurements in `docs/native-video-engine.md`.
- Stream page (#73, Alt+0): Go live and Record with clear state, the destination list (SRT, RTMP, Enhanced RTMP, WHIP
  shown unavailable with its reason) with per-destination health and reconnect state, an encoder picker that follows
  `stream.probe`, live faders for bitrate, keyframe interval, quality and audio bitrate through the hot-swap path, a
  staged "Apply and reconnect once" for resolution, frame rate and codec, and meters for encoder fps, bitrate, dropped
  frames, latency, CPU and GPU. Stream keys are write-only: typed once, stored privately (`<state_dir>/keys`, mode 0600),
  never returned, shown or logged, and kept when a destination is edited. The stream state gains `cpu_percent` and
  `gpu_percent`. ON AIR and REC show in the top bar.
- Presets (#69): 37 looks (film stocks, cinema, creator) with 12 generated 3D LUTs and rendered thumbnails, 10
  platform presets with sourced 2026 recommendations, 5 bandwidth tiers with per-codec ladders, and 8 one-click
  profiles that set look, platform, tier, lights, voice and framing. Users save, export and import their own profiles
  as validated JSON. New Presets page (Alt+8). Platform and tier apply through the pending `stream.set` contract
  (`docs/presets.md`).
- Recording and streaming from StreamStack itself (#62, #63, #64, #65): the `streamstack.stream` package records the
  program to Matroska or MP4 and streams it over SRT (caller and listener) and Enhanced RTMP (AV1 or HEVC, with a classic
  H.264 fallback), with Opus audio. Encoders are chosen by a capability probe across VA-API, QSV, NVENC and software
  (AV1, then HEVC, then H.264). Bitrate, rate control, keyframe interval, quality, encoder and audio bitrate change live
  without a reconnect (a second encoder takes over at a planned keyframe); resolution tier, fps and codec reconnect once.
  New Console actions `stream.start`, `stream.stop`, `stream.set`, `stream.probe`, `record.start`, `record.stop` and a
  `stream` state section. WHIP is in the code but reports "unavailable" on hosts whose ffmpeg has no WHIP muxer (Ubuntu's
  8.0.1). Design in `docs/adr/0001-encoder-generations-and-stream-transports.md`, schema and measurements on the Arc A380
  in `docs/stream.md`, measurement tool `tools/bench_stream.py`.
- Crash-safe files. One module, `streamstack.store`, now writes every file StreamStack keeps (settings, lighting
- Crash-safe files. One module, `streamstack.store`, now writes every file StreamStack keeps (settings, saved streaming profiles, lighting
  presets, saved looks, the light schedule and its status, the director's settings, status and learning, the Auto
  baseline, remembered framing mode and crop restore log, the applied capture mode, the live voice preset, the OBS
  websocket config, the OBS encoder profile, the Console runtime file and the lights unit): write to a temporary file
  in the same directory, fsync, rename over the target, fsync the directory, and keep the previous good copy as
  `<name>.bak`. A full disk, a killed process or two writers at once leave the old file or the new one, never half of
  either. On read, a file that is empty, truncated, not UTF-8 or the wrong shape is renamed to
  `<name>.corrupt-<time>` (the newest five are kept), the `.bak` copy is restored when it is good, and otherwise the
  defaults are used. The Console raises a toast and lists each repaired file under "Repaired files" on the System
  page, where it can be dismissed (`state.storage`, action `storage.dismiss`). Unit tests simulate partial writes,
  `ENOSPC`, `EIO`, leftover temporary files, truncated and binary files, and concurrent writers and readers across
  threads and processes.
- Opt-in Prometheus telemetry (issue #49), local first. Off until switched on under System, Telemetry and crash
  reports (or `STREAMSTACK_TELEMETRY=true`). Then `GET /metrics` on the Console serves service health (Console,
  studio, OBS, voice chain, Canon bridge, lights service, mic watch), pre-flight checks, audio levels and clip counts,
  light power, brightness and temperature, CPU and memory of StreamStack's own processes, event-loop lag, action
  latency histograms and error counters, version and uptime, as Prometheus text. It needs a token: the Console's own,
  or a separate stable scrape token that opens `/metrics` only and is never shown on the page. No video, audio,
  names or paths are exposed, and nothing is sent anywhere. `docs/telemetry.md` lists every metric; OTLP export is a
  documented follow-up.
- Crash reports. Uncaught exceptions (any thread), fatal signals and Console errors that were survived are written as
  JSON (version, stack, a small allow-listed state summary) to `~/.local/state/streamstack/crashes/`, with tokens,
  stream keys and the home directory removed. The System page lists them with a link to each and a Clear button.

### Removed

- OBS Studio is removed from StreamStack completely (#61). The native video engine is the only video path.
  Gone: the OBS websocket client and everything that drove it, the OBS profile and scene seeding, the OBS
  startup in `streamstack studio` and the tray, the OBS rows in the System page and Console status chips, the
  `streamstack encoder` command (it only configured OBS), `streamstack camlink --apply`, `streamstack studio
  --no-obs`, the camera-follow sources "OBS virtual camera / streaming / recording" (the apps source stays), the
  `auto` extra (`websocket-client`) and the `STREAMSTACK_VIDEO_ENGINE` setting. Look, crop and privacy changes
  now all use the engine's live control path. The auto director (its own process) reaches the engine through the
  Console's local API (`streamstack.consolelink`); its look actuator is now `program-look` ("Program picture
  look"), and `obs-look` lines in old `auto.jsonl` journals are display-only. Background blur answers 409 on a
  real rig until the OpenVINO mask lands in the engine (the demo rig still simulates it); the OBS cost guard
  that measured it is gone. Stations with old config start cleanly: a stale `STREAMSTACK_VIDEO_ENGINE` line is
  ignored and stripped from the user env file (a `.bak` is kept), and the old `camlink.json` and
  `framing-restore.json` records are deleted. The station catalog must stop installing and autostarting OBS
  (tracked separately in that repository).

### Changed

- StreamStack now needs Python 3.11 or newer (3.10 reaches end of life this month and the profile loader uses the
  standard `tomllib`). The oldest-Python CI job is `test-py311`; the linter targets py311 and uses `datetime.UTC`.

- Version numbering restarts at 0.0.8. The earlier releases tagged 0.1.0 to 0.3.1 were small, day-one
  increments and should not have been minor versions. The next release is 0.0.8; from then, 0.0.x patch releases ship the work,
  and minor or major versions are set by the owner only. The old tags stay in Git for history.
  A station on 0.3.1 must install 0.0.8 with `--force-reinstall`, since pip treats it as older.

### Added

- StreamStack's own voice chain replaces Easy Effects (#38 and the Easy Effects removal): a PipeWire filter-chain that
  StreamStack generates and runs in its own process and that exposes **StreamStack Mic** (48 kHz, mono). Stages, each
  adjustable live: high-pass, noise gate, DeepFilterNet 3 neural denoise (RNNoise as the fallback), de-esser, four EQ
  bands, compressor, makeup gain and a look-ahead limiter, with an A/B bypass that is delay matched. The Console
  Audio page has a Chain card with a fader per stage, input, output and gain-reduction meters, preset recall and
  saving. The preset names (Minimal, DeepFilterNet, Warm, Bright, Deep, StreamStack Voice) map onto stage values; presets
  made in Easy Effects are imported once, read only. See `docs/audio-chain.md` for the design, the 48 kHz contract the
  stream service can rely on, and what was measured on an isolated PipeWire (DeepFilterNet takes the noise floor
  down 20 dB and keeps speech within 1 dB).
- Loudness leveller (#38): a speech-gated slow gain ride of the chain output towards -16 LUFS (-14, -16 and -23
  selectable) inside a +-12 dB window, frozen in silence, while muted and without a signal, never touching the
  interface gain or 48 V, kept per rig profile, with a raw-microphone guard so a limiter cannot hide a hot voice.
  In the isolated synthetic test, speech with 20 dB level steps settled to -16 +-1 LU within 10 s and the true peak
  stayed under -1 dBTP. Near the target it moves at most 0.5 dB/s; far from it up to 4 dB/s (documented deviation).
- `tools/bench_audio_chain.py`: the isolated-PipeWire bench (denoise, loudness, glitch) behind those numbers.
- Release pipeline (#67): a `vX.Y.Z` tag runs the launch-sequence `python-package` pipeline: the tag, `__version__`
  and CHANGELOG must agree (and be a 0.0.x patch release), wheel and sdist are built in CI with a CycloneDX SBOM,
  a Notation (Azure Key Vault) signature covers the checksum manifest, the files go to the project package
  registries and the GitLab Release carries the CHANGELOG section. `scripts/release.py prepare` readies a release
  and refuses a minor or major bump without `--owner-approved-minor`. `streamstack verify-install` checks an
  install (version, packaged files, commands) and prints how to verify the signature;
  `scripts/install-release.sh` installs a verified release with pipx. See `docs/release.md`.

### Removed

- Easy Effects is no longer used, started or required by StreamStack (code, docs, tray, notes). Catalog follow-up:
  stop installing and autostarting it; install `zam-plugins` and the DeepFilterNet and RNNoise LADSPA plugins.

### Fixed
- Python 3.11 CI test run (pipeline 88138, `test-py311`): three telemetry tests timed out at a hard-coded 10 s on their first request while the CI pod was CPU-starved (the same tests take milliseconds on a free core). Tests that talk to a real Console over a socket now share one generous timeout (`tests/http_support.py`, 90 s, `STREAMSTACK_TEST_HTTP_TIMEOUT`), and a guard test refuses a new short hard-coded one.
- The demo rig no longer flushes the disk (fsync and a backup copy) when it saves a lighting preset or a camera look, the same as its settings. On a busy disk the flush could take seconds, so "Save current" sometimes did not show its preset for 30 seconds.
- Camera page: switching the edited source no longer flashes the previous source's look into the sliders when a background refresh was mid-read.
- The demo rig no longer flushes the disk (fsync and a backup copy) each time a lighting setting changes, and its reset saves once. On a busy disk each flush could take seconds, which stalled Reset the schedule, Resume and the schedule table for tens of seconds.
- Lighting: the F and S keys and their palette commands flip what the switch shows, not the last state the server reported, so a key pressed just after a click undoes the click instead of repeating it.
- Lighting page: the "Resume the schedule" button no longer pushes the whole page down by a row the moment a light is first changed by hand (that moved the faders under the pointer mid-gesture, so a second drag grabbed the label instead of the track). The row keeps its height.

- Lighting: a light you move is never overwritten by the schedule or camera follow. The engine decided from a
  snapshot taken at the start of its tick and then walked a fade in many small writes, so a nudge on a fader that
  landed in between was overwritten with the schedule's level a few milliseconds later and the light then looked
  held at the wrong value. Every write the engine makes is now a compare-and-set under the light's device lock
  (`LightingController.write(..., expect=...)`): when the light no longer shows what the engine last knew, the write
  is dropped and the next tick holds the light. The settings file cache also keys on the inode, so two saves of the
  same size within one clock tick are not mistaken for one.
- Lighting: Resume the schedule is no longer undone by a change you made a moment earlier. A change by hand within two
  seconds of the engine's own write is noticed only when that window ends, and it then held the light again right
  after Resume. Resume now adopts what the lights show as the state the engine starts from.
- Console: a fader drag always ends on its last value. Streamed controls (lighting, gain, looks, blur) posted every
  step as its own request and the server answers on separate threads, so two steps in flight could land out of order
  and leave an older value on the device (the All lights temperature fader stopped short of its end). One request per
  control is now in flight at a time and the newest values follow when it answers; `flushStream` also sends the right
  action for controls that stream under their own key (the blur strength).
- The loudness cost unit test no longer fails on a loaded CI runner: instead of a CPU-time share against a fixed
  12 % bound it counts the analyser's work (interpolations per sample, K-weighted frames) and compares its CPU time
  with a pure-Python baseline loop timed in the same process (bound 15x, measured about 4.5x; a tenfold regression
  fails).

- Three flaky unit tests on loaded shared runners (`test-py310`): the loudness cost check now takes the best of five
  CPU-time runs, the install-release test runs hermetically (own HOME and TMPDIR, no stdin) with a hang-guard deadline
  instead of 60 s, and the output-test wait allows the pure-Python test sound the time it needs to be written.
- The Camera page's save-look form now refreshes its "Replaces your look" hint when the saved looks change while the form is open.

- Director picture heuristics no longer misjudge a dark room (#32, colour half): measured on the real
  rig, a black frame was read as "cool, green tint" (warm index -1.26, saturation 0.96) from one to three bright
  pixels, called soft, and could flag flicker or blurred frames from noise. The cast now needs 5% of the frame in the
  mid-tones, frames with a 99th-percentile luma below 40 get a "too dark to judge" note instead of colour, contrast
  and focus findings, and the flicker and blurred-frame checks have brightness and sharpness floors. Evidence in
  `docs/auto-mode.md`. The audio half of #32 stays open.
- Console: the Live voice presets show their whole names. With six presets in one row, "Minimal" and
  "DeepFilterNet" were cut to "Minima" and "DeepFil"; preset labels now wrap at word and camelCase joins and
  shrink with the pad (`<ss-pad fit>`).

### Added

- Lighting e2e determinism gate: the demo rig's lighting runs on a pinned clock and place
  (`STREAMSTACK_DEMO_CLOCK`, New York) so the daylight schedule does not depend on the hour or the host zone, and the
  `e2e-lighting` CI job soaks both lighting suites three times at 03:30, 12:00 and 18:30
  (`scripts/e2e_lighting_soak.py`). Unit tests drive a hand landing mid-fade and between the snapshot and the write.
- Audio page: Input and Output sections (#36). Output lists every PipeWire output (friendly
  name, kind and bus, state, volume, mute, default marker, set as default; the Easy Effects
  sink is shown as a route) and follows hot-plug through `pw-dump --monitor`, with a warning when
  the default output disappears. Each output has a Test button that plays a short generated
  stereo left/right identification and a chime on that output only, without changing the
  default, and remembers whether you heard it; Test all runs them in turn. The Live page's
  microphone card gets a compact output selector with a Test button. The core stays
  dependency-free (`pw-dump`, `wpctl`, `pw-play`, stdlib `wave`), and the demo rig simulates four outputs.
- Native video engine (#57): StreamStack now makes the program camera (`/dev/video43`) itself, with OBS
  not running. One supervised libav (ffmpeg) child reads the camera, uploads the frame to the Arc over
  VA-API/Vulkan, grades it on the GPU (libplacebo: brightness, contrast, saturation, gamma, auto-framing
  crop), applies warmth and sharpen, and writes the loopback. Slider and crop changes are written to the
  running child (about 0.06 ms to apply, no restart, no dropped call camera). A second graph feeds the
  framing detector. `STREAMSTACK_VIDEO_ENGINE=obs` keeps the old path. New settings:
  `STREAMSTACK_VIDEO_ENGINE`, `STREAMSTACK_ENGINE_SIZE`, `STREAMSTACK_ENGINE_BACKEND`,
  `STREAMSTACK_ENGINE_RENDER_NODE`, `STREAMSTACK_ENGINE_LUT`.
- `streamstack engine probe` (render node, GPU backend, which hardware encoders work) and
  `streamstack engine bench` (glass-to-output latency and CPU against a stamped feed).
- The Camera page shows an ENGINE chip (GPU path, frame rate, drops); the live and mini status chips say
  "Video engine" instead of "OBS" in native mode.
- A codec-agnostic encode tap table (AV1, HEVC, H.264 on Quick Sync or NVENC, AV2 slot empty) for the
  recording and streaming work. Design, feasibility matrix and measured numbers: `docs/native-video-engine.md`.
- Native Elgato Stream Deck support (#37): `streamstack deck run` (a systemd user service, `streamstack deck enable`)
  shows live Console state on every key and runs the same actions as the Console, on any model the `streamdeck`
  library supports (extra `deck`: `pip install 'streamstack[deck]'`). The default profile fills a 4x4 deck with
  folders for lights, voice presets and scenes and follows the rig; any layout is re-flowed to the connected deck.
  The new **Deck** page (Alt+9) shows the keys live, with drag-and-drop from a data-driven options list (scenes,
  mute, privacy, voice and light presets, brightness, camera follow, schedule, record, stream, virtual camera,
  Director, blur, or any Console action), a key inspector, pages, brightness and sleep, a model picker and export
  and import. The demo rig has a simulated deck. See `docs/deck.md`.

## [0.3.1] - 2026-10-22

### Fixed

- Background blur works with a real OBS (#34): the Console read the filter-kind list from
  `filterKinds` instead of obs-websocket's `sourceFilterKinds`, so it always reported the
  plugin as not loaded and refused to switch the blur on.

### Added

- Offline AI benchmark harness for the hardware evaluation (#22): `tools/bench_ai.py`
  (not shipped in the wheel; extra `bench`) runs MediaPipe Meet, PP-HumanSeg, SINet, YuNet
  and BlazeFace over a recorded clip of each rig on ONNX Runtime CPU and OpenVINO CPU and
  Arc (`GPU.0`), and reports p50/p95 ms per frame, CPU percent of one core, Arc busy
  percent from `intel_gpu_top` and first-compile time with `CACHE_DIR`, as a markdown
  table, JSON and the decision-gate block of `docs/hardware-ai-evaluation.md`. Models are
  fetched from pinned URLs with SHA-256 checks and their licences recorded; a missing
  backend, device or tool is skipped with the reason. `--synthetic` generates the frames
  for CI. `tools/record_clip.sh` records the 20 s clip from a V4L2 device and refuses to
  run without `--confirm-no-call`. The numbers in the doc are synthetic placeholders until
  the clips are recorded and the harness is run on a quiet host.
- Lighting automation (issue #33). **Camera follow:** the Litra lights switch on when an app uses the camera (found read-only
  from `/proc/*/fd`, never by opening `/dev/video*`) or when OBS starts the virtual camera, streaming or recording, and
  switch off after a configurable grace period, only the lights StreamStack switched on. It runs in a new user service,
  `streamstack-lights.service` (`streamstack lights run|enable|disable|status`, installed by `streamstack setup`), so it
  works with the Console closed. **Daylight schedule:** brightness and colour temperature follow the sun (the Director's
  offline sunrise and sunset code) or a 24 hour table, with fades, per-light roles (key, fill, manual), names and limits;
  a manual change wins until the hold runs out or the next camera session, and the schedule yields to the AI Director so
  the two never fight. The Lighting page gains the status line ("Camera in use since 14:02: Teams opened the camera"), the
  Camera follow, Daylight schedule (with a 24 hour graph) and Light settings cards, an event log and the keys `F`, `S`
  and `R`. Settings live in `~/.config/streamstack/lighting-automation.json` (mode 600). See `docs/lighting.md`.

## [0.3.0] - 2026-10-22

### Added

- Canon bridge on the GPU (#14): live-view JPEGs are decoded and scaled on Intel Quick Sync, then VA-API, with the CPU and the old command line as fallbacks; each path is probed on a real frame first and a path that crashes or stalls is dropped. Frames reach ffmpeg length-framed so there is no one-frame demux delay, with low-delay input flags and frame passthrough. Output keeps the camera's aspect and is never enlarged (`STREAMSTACK_CANON_OUTPUT=fill` restores the 0.2 crop). A stalled camera (4 s) or ffmpeg (3 s) is restarted with backoff and a hung camera library ends the process for systemd. The System page shows decode path, fps in/out, grab and hand-off time, CPU of bridge and ffmpeg, and restarts; so does `streamstack canon status`.
- Canon camera settings (#15): ISO, shutter, aperture, exposure compensation, white balance and Kelvin, Picture Style, focus mode, live-view size, live-view autofocus and focus nudges on the Camera page (Canon body tab) and `streamstack canon get|set|af|focus`, using libgphoto2's configuration names, with choices and read-only state taken from the camera and every change read back. One shared libgphoto2 session (python `gphoto2`, extra `canon`) changes settings without interrupting the picture; without it the gphoto2 command pauses live view for about a second per change.
- New settings `STREAMSTACK_CANON_OUTPUT`, `_DECODE`, `_BACKEND`, `_RENDER_NODE`.
- Auto-framing (issue #23): a face detector (YuNet, MIT, fetched and checksum-verified by
  `streamstack framing --fetch`) runs beside OBS on a low-rate screenshot of the camera, on the Intel GPU through
  OpenVINO when there is one, else on the CPU, and moves a smooth crop and zoom over obs-websocket, so there is no
  added video latency. Modes Off, Gentle, Tight and Follow; a dead zone, critically damped smoothing, headroom and
  a zoom limit set by the camera's resolution. The Camera page has an Auto-framing panel (mode, Hold, Recentre,
  device and time per detection) and the Live page a compact control. The original crops are restored when framing
  is off, on exit and after a crash. Install with `pip install 'streamstack[framing]'`; without it nothing changes.
  `framing.*` actions give the AI director a clean actuator. See docs/auto-framing.md.
- AI Director 2.0 (issue #27). Auto mode reviews a burst of stills joined into one contact sheet (the stills go to Claude; the Auto page says so) with local per-frame measurements (histogram, clipping, white balance, skin tone, sharpness, noise, face position), and analyses a short voice clip locally into features only (f0, depth, clarity, sibilance, SNR, LUFS, room); raw audio never leaves the computer. Personas (Executive, Bright podcast, Late-night calm, Daylight natural, Crisp newsroom) are data files, selectable and editable on the Auto page and with `streamstack auto persona`; Claude judges mood against them. Time of day from the local clock and offline sunrise/sunset for a configured property (`streamstack auto property`), with per-slot learned baselines. One actuator registry (OBS look, mic gain, voice preset) with rate-limited steps, override hold, reasons in the log and undo. The Auto page is redesigned around the contact sheet, voice bars, persona cards, a time-of-day strip and a change log with reasons.
- Auto mode drives hardware when it is present (each is simply listed as
  unavailable, with the reason, when it is not): the Canon's exposure
  compensation and white balance Kelvin through the Canon bridge (never focus),
  Litra brightness and colour temperature per light within the model's range
  (never power), and the audio interface's preamp gain for the Scarlett 2i2
  Gen 4 and, when enabled, the Wave XLR (never 48V or Autogain), with the
  PipeWire software gain as the fallback. Lights follow the hour: the dark
  slots raise and warm the persona's light, and what you set by hand in a slot
  is learned. The director meters the face with the YuNet detector when the
  framing extra is installed and keeps the skin-colour heuristic otherwise.
  Settings `STREAMSTACK_AUTO_CANON`, `STREAMSTACK_AUTO_LIGHTS`,
  `STREAMSTACK_AUTO_HARDWARE_GAIN`, `STREAMSTACK_AUTO_FACES` and
  `STREAMSTACK_AUTO_INTERFACE_INPUT`; `streamstack auto reset` restores them.
  The demo rig shows all of them on the Auto page. Unverified on real
  hardware: see `docs/auto-mode.md`.
- Lighting page for Logitech Litra Glow, Beam and Beam LX lights (USB HID over
  `/dev/hidraw`, pure Python): power, brightness and colour temperature per
  light, an All lights master, presets (Key light, Soft, Warm evening,
  Daylight, Off) and saved presets, Identify, hotplug and read-back of the real
  state. Alt+6 opens it, `L` turns every light on or off, and a small lights
  control sits in the Live status rail and the Mini window. The System page
  reports lights found and whether this user may open them. Needs the udev rule
  in `packaging/udev/` (see `docs/lighting.md`). Demo mode has two simulated
  Beams.
- Hardware-agnostic audio interface layer (`streamstack.hardware`): the
  interface is found by USB id, a profile selects its driver, and the Console
  renders whatever controls the driver reports. Profiles: Focusrite Scarlett
  2i2 Gen 3 and Gen 4 (through the kernel's `scarlett2` ALSA controls, read and
  set with `amixer`) and Elgato Wave XLR. Settings `STREAMSTACK_INTERFACE`
  (`auto`, `off`, a profile or a USB id) and `STREAMSTACK_WAVE_XLR_USB`.
- Console Interface page (Alt 7): Scarlett gain, Autogain, Air (switch on
  Gen 3, Off/Presence/Presence + Drive on Gen 4), Line/Inst, Safe, link, input
  select, direct monitor and Autogain targets; 48V needs a press-and-hold and a
  server-side confirm. The Audio page's software gain remains the fallback. The
  demo rig simulates a Gen 4, a Gen 3, a Wave XLR or nothing connected.
- Wave XLR hardware gain, mute, 48V, headphone volume and low-impedance mode
  over its vendor USB control interface, read-modify-write with read-back.
- Clip latch on the Live, Audio and Mini mic meters and the Audio voice meter: lights at -1 dBTP or digital full
  scale and stays lit until clicked. The latch lives on the server, so every window and page agrees; `audio.clip_clear`
  clears it.
- Momentary (400 ms) and short-term (3 s) loudness in LUFS (K-weighted, BS.1770-4) for the mic and the voice, shown on
  the Audio page loudness card. The `meters` event carries `sample_db`, `latched`, `lufs_m` and `lufs_s`.
- Tests of the true-peak, K-weighting and loudness maths against BS.1770 and EBU Tech 3341 signals
  (`tests/test_loudness.py`), a CPU bound for the analyser, and browser tests of the lamp and readout
  (`tests/e2e/test_meters.py`).
- Background blur (issue #24): the obs-backgroundremoval plugin as a `Background Blur` filter on both camera
  sources, disabled by default and shipped with low-latency CPU settings (2 threads, mask every second frame,
  MediaPipe model). The Camera page has a "Blur background" card (toggle, strength 1 to 20, Light / Balanced /
  Smooth) and the Live look card a compact "Blur" pad; both create the filter on older OBS profiles on first use.
  Detection finds the plugin, its version and inference device at runtime, a Video health check says how to
  install it when it is missing, and a cost guard reads OBS's stats and warns when the blur adds 8 ms or more
  per frame, 1 percent or more CPU or any dropped frame. The demo rig simulates all of it
  (`video.blur`, `video.blur.plugin`); the plugin itself is installed by Curiosity-Corp/catalog#100.
- Voice denoise option (issue #25): a `StreamStack Voice DeepFilterNet` preset (the default voice with Easy Effects'
  Deep noise remover in place of RNNoise) and, on the Console's Audio page, a noise removal A/B pad (RNNoise, about
  10 ms, against DeepFilterNet, about 40 ms algorithmic delay) with the Attenuation Limit as a fifth tone knob. The
  pad swaps the engine inside whichever preset is chosen, through the same live preset as the tone knobs
  (`audio.denoise`, `denoise` in the audio state, two palette commands). RNNoise stays the default everywhere for the
  lowest latency. The Deep noise remover is built into Easy Effects 8.1.2 but needs `libdeep_filter_ladspa.so`, which
  the Ubuntu package does not ship: without it the DeepFilterNet pad and preset are disabled with the reason instead
  of loading a chain that would silently not denoise (`docs/voice-denoise.md`). Unverified on real hardware.

### Notes

- The Wave XLR has no HID interface (no `/dev/hidraw`); the issue's HID premise
  was wrong. Its controls are vendor USB control transfers on interface 3,
  taken from open-source prior art (OpenWave and others) and **not verified on
  hardware**. They are off by default: `STREAMSTACK_WAVE_XLR_USB=off` never
  opens the device. Clipguard and low cut have no documented register and are
  reported as unavailable. See docs/interfaces.md.
- The Scarlett amixer fixtures in the tests are synthesized from the kernel
  source, not captured from a device.

### Changed

- Console meters tap the microphone and the voice chain at the source's own 48 kHz, 32 bit float, in 20 ms frames
  (was 16 kHz, 16 bit, 50 ms) and push at 30 Hz (was 20 Hz). The bar shows the true peak (dBTP, 4x oversampled per
  ITU-R BS.1770-4), so inter-sample peaks that a sample meter misses now show.
  Pure Python (numpy is not a dependency): about 1-3 % of one core per source.

### Fixed

- A missing Zoom desktop client no longer turns the Health badge amber; calls use the zoom.us web client.

## [0.2.1] - 2026-10-22

### Added

- `packaging/udev/60-streamstack-litra.rules`: hidraw access for the Litra Glow, Beam and Beam LX by exact VID:PID, installed by `make install` (docs/streamstack.md).
- `docs/hardware-ai-evaluation.md`: Arc GPU background blur, auto-framing and voice denoise evaluation (issue #19).
- OBS records and streams on the GPU encoder. `streamstack encoder` lists what this machine can use and
  `streamstack encoder --apply` writes it into the StreamStack OBS profile (OBS closed); `streamstack setup`
  does the same for a new profile. Detection runs at runtime: `vainfo` against every DRM render node (no
  hardcoded path) plus the encoder list OBS prints in its own log. Order: AV1 VA-API, AV1 Quick Sync, H.264
  VA-API, H.264 Quick Sync, x264. Streaming uses CBR with no B-frames and a two second keyframe interval, the
  VA-API low-power mode where the GPU only has that; recording uses VBR at twice the live rate. The Video
  checks (`streamstack doctor` and the Console System page) show the encoder in use and warn when a better one
  is available or the profile names one this machine lacks.
- Cam Link: the best mode the HDMI source offers (1080p60, then 1080p50, then
  1080p30; raw YUYV/NV12 before MJPEG; 4K only with `STREAMSTACK_CAMLINK_4K`).
  The card is found by its USB id (`0fd9:0066`), its modes are read from
  `v4l2-ctl --list-formats-ext` and the choice is re-read every few seconds.
  `streamstack camlink` shows it and `--apply` tunes the running OBS source and
  canvas to match; `streamstack setup` seeds new profiles with it.
  `STREAMSTACK_CAMLINK_MODE` (`auto`, `off`, or a pinned `1920x1080@60 YUYV`)
  controls it. The Camera page shows the HDMI mode on the Cam Link picture and
  System health reports the signal, the offered modes and the HDMI audio.

### Changed

- Easy Effects limiter in every voice preset: oversampling `True Peak/24 bit` (was None) and a -1 dBTP ceiling
  (threshold -1.0 dB, was -1.5). Presets seeded earlier keep their old limiter until `streamstack setup --force`.
  The preset label test now accepts the full Easy Effects 8 limiter label lists (kcfg
  `easyeffects_db_limiter.kcfg`).
- OBS profiles seeded by `streamstack setup` use a fractional frame rate
  (`FPSType=2`), so 59.94 and 29.97 sources are not resampled.
- Cam Link HDMI audio is recognised by name or description and never offered
  as the call microphone; the System check says what it is.

### Fixed

- Console: after visiting another page, the Live page's MUTE MIC and PRIVACY
  pads overlapped and the voice presets stacked into one column. Page
  stylesheets stay loaded after navigation and leaked class names into other
  pages; each one is now scoped to its page with `@scope`. A data test checks
  every page stylesheet is scoped, and a browser test tours every page and
  checks the Live layout.

## [0.2.0] - 2026-10-22

The StreamStack Console: a visual control surface for the middle of a call,
replacing the nested panel menu as the main way to drive the studio.

### Added

- `streamstack console` (`streamstack-console`, `--mini`, `--demo`,
  `--no-window`, `--port`, `--idle-exit`). A local web console served on
  `127.0.0.1` with a per-launch token, live state and meters over SSE, and
  MJPEG previews. The frontend is plain ES modules and Web Components with no
  build step. It opens as a Chrome or Edge app window, with xdg-open as a
  fallback. The application-menu entry and a left click on the tray icon open
  it.
- Live page: program picture with on-air tally, mic fader and meter, MUTE and
  PRIVACY pads, scene pads, voice presets and the auto director switch.
- Camera page: grade faders and warmth, sharpen and LUT, histogram and RGB
  parade or waveform scopes, thirds, safe, centre and zebra overlays, saved
  looks, scenes, virtual camera, record and stream.
- Audio page: raw and processed meters, gain and mute, tone knobs, voice
  presets with A/B bypass, a loudness history and a recorded mic check.
- Auto page: the Claude director switch, ask now, its latest note, a timeline
  of every change and undo.
- System page: health checks, services, shortcuts to the studio tools, and the
  Teams and Zoom settings.
- Mini window, a command palette (`Ctrl K`), keyboard shortcuts for every live
  control (`?` lists them), light and dark themes, and a demo rig for trying
  the Console without hardware.
- Console browser tests (`tests/e2e`, `make e2e`, the `e2e` CI job): 158
  Playwright specs against the demo rig covering every page, the shortcuts,
  the palette, reconnect, mic revive and the phone layout.
- Mic watchdog: detects a stalled ALSA microphone from its PCM counters in
  `/proc/asound` (not capturing, `hw_ptr` frozen, or `appl_ptr` not consumed
  for 5 s), shows "Microphone stalled" with a one-press Revive mic in the
  Console (banner on Live and Mini; Shift+R and the palette on every page),
  adds the `mic-stall` health check
  and `streamstack mic-revive` (refuses a healthy mic without `--force`).
  Never revives automatically; Bluetooth and other non-ALSA sources are not
  judged.

### Fixed

- `system.run` and `system.record` now enforce hard deadlines and reap child
  processes (SIGINT, SIGTERM, SIGKILL).
- Flaky console rig test: `DemoStudio` accepts an injectable clock so state is a
  pure function of time.

## [0.1.0] - 2026-10-17

First release as a standalone project (previously embedded in a workstation
configuration role).

### Added

- `streamstack` CLI: `doctor`, `studio`, `stop`, `status` (`--short`,
  `--icon` for panel buttons), `cameras`, `preview` (any device and mode,
  thirds grid), `mic-check` (raw/processed A/B), `voice`, `canon-bridge`,
  `setup`, `call-settings`, `auto`, `--version`.
- `streamstack-tray`: panel menu (`--menu`) and tray icon with studio
  start/stop, auto mode, voice presets and bypass, per-mode camera previews,
  and the last Claude note.
- Auto mode (`streamstack auto`): local auto-exposure with face spot metering
  and speech-gated mic levelling; Claude passes through the restricted Claude
  Code CLI (`--restricted --tools Read --json-schema`) or the Anthropic API
  (forced tool use); confidence gating, adaptive pacing, black/flat frame
  guards, a rotating journal, and `auto reset` to undo every change.
- Settings layered from defaults, `/etc/streamstack/streamstack.env` and
  `~/.config/streamstack/streamstack.env`; `STREAMSTACK_MICROPHONE` selects
  the call microphone and an unplugged one is never silently replaced.
- Five Easy Effects voice presets, the OBS profile and scene collection, panel
  icons, systemd user units, desktop entries, `make install` and a wheel.
