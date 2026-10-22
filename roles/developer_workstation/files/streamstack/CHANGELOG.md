# Changelog

All notable changes are recorded here. The project follows
[semantic versioning](https://semver.org).

## [Unreleased]

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
