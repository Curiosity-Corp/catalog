# Changelog

All notable changes are recorded here. The project follows
[semantic versioning](https://semver.org).

## [Unreleased]

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
