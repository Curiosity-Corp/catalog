# Roadmap: RICE analysis of candidate features

Date: 2026-10-23. Scope: what to build after #36 (audio outputs), #37 (Stream Deck), #32 (Director
calibration), #18 (Wave XLR HID), #22 and #26 (AI on Arc), which are planned and not repeated here.
Milestones: Meeting-ready autopilot (due 2026-12-18) and Fleet and remote (due 2027-01-22).

## Method

- **Reach** is uses per week across all properties. The base is 60 meetings a week (about six properties at
  about ten meetings each, joined at any hour). A feature used in every meeting scores 60; one used in half the
  meetings 30; a feature used once per property per week 6. Continuous health features count property-days (42).
  These are estimates, not measurements, and should be replaced by usage counts once the telemetry issue ships.
- **Impact** is 0.25 (minimal) to 3 (massive): how much it changes the outcome of a meeting, or avoids an
  embarrassing one.
- **Confidence** is how sure we are of reach, impact and feasibility on Linux with the Intel Arc A380.
- **Effort** is person-weeks including tests, e2e, docs and the release.
- **RICE** = Reach x Impact x Confidence / Effort.

## Ranked table

| Rank | ID | Candidate | Area | Reach | Impact | Conf. | Effort | RICE | Issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | C02 | Call loudness: -16 LUFS target with gain riding | Audio | 60 | 2 | 80% | 1.5 | 64.0 | #38 |
| 2 | C01 | Pre-flight check (one press, scored) | Readiness | 60 | 3 | 80% | 3 | 48.0 | #39 |
| 3 | C03 | Safe join: privacy and mute until Go live | Meeting integration | 30 | 1.5 | 80% | 1 | 36.0 | #40 |
| 4 | C04 | Network quality monitor (RTT, jitter, loss, bandwidth) | Network | 60 | 1.5 | 70% | 2 | 31.5 | #41 |
| 5 | C07 | Time-of-day look profiles (deterministic) | Video | 30 | 1.5 | 75% | 1.5 | 22.5 | #42 |
| 6 | C05 | Calendar-aware setup 2 min before a meeting | Calendar | 45 | 2 | 70% | 3 | 21.0 | #43 |
| 7 | C08 | Health telemetry and alerts (Prometheus) | Fleet | 42 | 1.5 | 70% | 2.5 | 17.6 | #49 |
| 8 | C06 | Global hotkeys and MIDI control | Control | 30 | 1.5 | 70% | 2 | 15.7 | #44 |
| 9 | C09 | Call-state awareness and talking-while-muted warning | Meeting integration | 25 | 2 | 55% | 2 | 13.8 | #45 |
| 10 | C10 | Auto-framing 2: groups, speaker hand-off, presets | AI video | 25 | 1.5 | 65% | 2 | 12.2 | #46 |
| 11 | C11 | Eye-line coach (landmark based, non-generative) | AI video | 20 | 1 | 60% | 1 | 12.0 | #47 |
| 12 | C12 | Echo and speaker-bleed check | Audio | 15 | 1.5 | 60% | 1.5 | 9.0 | #48 |
| 13 | C14 | LUT manager (import .cube, preview, per slot) | Video | 10 | 1 | 85% | 1 | 8.5 | not filed |
| 14 | C13 | Recording safeguard: dual-track local record, disk guard | Recording | 12 | 1.5 | 75% | 2 | 6.8 | not filed |
| 15 | C18 | Accessibility pass (contrast, motion, screen reader, audio cues) | Accessibility | 5 | 2 | 80% | 1.5 | 5.3 | not filed |
| 16 | C16 | Self-heal supervisor (camera, bridge, engine) | Health | 5 | 3 | 70% | 2 | 5.2 | not filed |
| 17 | C17 | Teleprompter and notes overlay | Presenting | 6 | 1.5 | 80% | 1.5 | 4.8 | not filed |
| 18 | C15 | Fleet profile export/import and central config | Fleet | 10 | 2 | 70% | 3 | 4.7 | not filed |
| 19 | C21 | Mic technique coach (distance, plosives, off-axis) | Audio | 20 | 1 | 50% | 2.5 | 4.0 | not filed |
| 20 | C22 | Colour matching across properties (chart-based) | Video | 15 | 1.5 | 50% | 3 | 3.8 | not filed |
| 21 | C23 | Network-adaptive degrade (lower res or raw camera) | Network | 6 | 2 | 50% | 2 | 3.0 | not filed |
| 22 | C19 | Mobile remote over Ziti (token-gated PWA) | Remote | 8 | 1.5 | 60% | 3 | 2.4 | not filed |
| 23 | C20 | Local captions and transcripts (Whisper on Arc or CPU) | Captions | 15 | 1.5 | 50% | 5 | 2.2 | not filed |
| 24 | C24 | Generative eye-contact correction | AI video | 30 | 2 | 15% | 8 | 1.1 | not filed |
| 25 | C25 | HDR and log handling | Video | 3 | 1 | 50% | 2 | 0.8 | not filed |

The cut: the top 12 are filed. Eleven go to Meeting-ready autopilot (everything that makes a meeting start right) and the
telemetry issue (#49) opens Fleet and remote because it is the base for fleet operations. C13 to C25 stay as the backlog,
revisited when Meeting-ready autopilot has shipped and telemetry gives real usage numbers.

## What leading products do (2026) and what is feasible here

| Product | What it does | Feasible on Linux with an Arc A380 |
| --- | --- | --- |
| Elgato Camera Hub 2.0 and Wave Link 3.x | LUTs (five built in, import your own), a Prompter with voice sync, a Marketplace, Wave Link Audio Flow view and compact windows (beta notes, community reports) | LUTs and prompter yes (C14, C17); Audio Flow is a view of PipeWire, which StreamStack could mirror |
| NVIDIA Broadcast 2.2 | Eye Contact, Studio Voice (out of beta), Virtual Key Light, noise removal | RTX only; no Arc or Linux port. Eye contact stays out (C24); a landmark based coach is feasible (C11); noise removal is covered by RNNoise and DeepFilterNet |
| Windows Studio Effects | Eye contact (standard and teleprompter), automatic framing, background effects, voice focus, portrait light; NPU based, some effects Snapdragon only | Automatic framing exists (#23); group framing is feasible (C10); the rest is NPU or vendor specific |
| Logitech Logi Tune | Agenda: Google and Outlook calendars, join prompts, meeting light cues | Calendar via ICS and CalDAV is feasible (C05) and goes further by preparing the rig |
| Apple Continuity Camera (macOS 26) | Center Stage, Studio Light, Edge Light (display as fill light, auto in low light), Desk View, Portrait | Center Stage maps to C10; Edge Light is a possible later fallback when no Litra is present; Desk View is a second camera source |
| Descript and Riverside | Studio Sound, AI eye contact, local 4K and WAV recording per participant, uploads in the background | Local recording safeguard is feasible (C13); loudness and denoise exist or are in C02 |
| Streaming-app plugin ecosystem | Advanced Scene Switcher (1.33.x) for rules, LocalVocal for live captions (whisper.cpp, CUDA, Vulkan, no OpenVINO), a paid teleprompter | Rules already live in StreamStack; captions on Arc would use Vulkan or a custom OpenVINO whisper.cpp build, unproven (C20, 2.2) |
| Teams, Meet, Zoom | The Teams third-party device API (WebSocket on localhost:8124, mute state) exists for the desktop client; Linux support is unconfirmed | Needs a spike; PipeWire stream and camera use detection is the baseline (C09) |

## Gap analysis

| Area | Current state (0.3.1) | State of the art | Gap | Candidates |
| --- | --- | --- | --- | --- |
| Readiness | Doctor and System health checks, mic watchdog, mic check; no single verdict | One tap tests that call quality will be good | Large | C01, C12 |
| Meeting integration | Camera-follow for lights; no notion of a call | Join prompts, mute sync, safe start | Large | C03, C09, C05 |
| Audio levels | LUFS and true-peak metering, limiter, DeepFilterNet; manual gain | Automatic loudness normalisation, studio sound | Medium | C02, C21 |
| Network | None | In-app quality indicators, adaptive bitrate | Large | C04, C23 |
| Video look | Director with learned per-slot baselines (needs Claude), looks library, one LUT | Deterministic profiles per condition, LUT library, auto framing for groups | Medium | C07, C10, C14, C22 |
| Gaze | None | Generative eye contact on RTX and NPU | Accepted (non-generative coach only) | C11, C24 |
| Control | Console keys (focus only), Stream Deck planned (#37) | Global hotkeys, MIDI, mobile | Medium | C06, C19 |
| Fleet | Per-host config, catalog role | Central profiles, health across hosts | Large | C08, C15 |
| Recording | StreamStack records on the Arc encoder | Local multitrack safeguard, uploads | Small | C13 |
| Captions | None | Live captions and transcripts | Medium, hardware unproven | C20 |
| Accessibility | Phone layout, e2e a11y smoke | Full keyboard, contrast, screen reader, audio cues | Small to medium | C18 |

## Sequencing

1. #36 (outputs) lands first because Pre-flight (#39) consumes its output tests.
2. Cheap, high-RICE items in parallel: #38 (loudness), #40 (safe join), #41 (network).
3. #39 (Pre-flight) after #36 and #41, then #43 (calendar) which calls Pre-flight.
4. #42, #44, #45, #46, #47, #48 are independent.
5. Fleet and remote starts with #49; C15 (fleet sync) and C19 (mobile) are filed when it is underway.

## Risks and spikes

- Teams mute state on Linux: spike the third-party API on the Teams PWA before relying on it (#45).
- Live captions on Arc: benchmark whisper.cpp Vulkan and OpenVINO in the #22 harness before committing (C20).
- Global shortcuts depend on the desktop portal version (#44).
