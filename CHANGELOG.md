# Changelog

All notable changes are recorded here. The project follows
[semantic versioning](https://semver.org).

## [Unreleased]

## [0.0.19] - 2026-10-28

### Added
- Pre-flight check (#39): one press checks the camera, microphone, outputs, lights, network and headroom, listens to you say your name once, and gives a score out of 100 with a reason and a one-press Fix for each finding (turn the lights on, unmute, set the default output). Live page Pre-flight button and palette command, `streamstack preflight` (exit code says ready or not) and the score in `streamstack status`. See docs/preflight.md.
- Safe join (#40): a new System setting, On call start (Off, Warn or Safe join; Warn by default). When a call app opens the camera, Warn tells you, and Safe join puts privacy on and mutes the microphone until you press Go live (banner on Live and Mini, Space, the palette or a Stream Deck key). It only takes what is not already yours, never takes it again once you went live or released it yourself, gives it back when the call ends, and keeps an event list of what it did. See docs/safe-join.md.
- Eye-line coach (#47): an optional, off-by-default Camera popover that reads your head pose from face landmarks and tells you whether you look at the lens, the screen or the keyboard, how far the camera sits from your eye line, and, with a calibration step and a thirds-grid wizard, where to place the call window. A reminder can nudge you after minutes of looking away during a call. No picture is stored or sent.
- Instant replay (#84): arm a buffer that keeps the last 5 to 600 seconds of the encoded program in memory and save a clip of any length up to that, after the moment happened. Nothing is re-encoded (the buffer is one more sink on the existing encoder chain), clips start on a keyframe and are remuxed with an index, and memory is capped (`STREAMSTACK_REPLAY_MAX_MB`, default 512): arming refuses a length that cannot fit, and a bitrate spike costs replay depth, never memory. New actions `replay.start`, `replay.save`, `replay.stop`, an Instant replay card on the Stream page, and a `replay` block in the stream state. See docs/stream.md.
- The Console is an installable web app: a manifest and PNG, maskable and SVG icons are served (without the access token), and the "no access token" screen explains that an installed window must be opened by the launcher.
- tools/bench_ai.py benchmarks the MediaPipe selfie segmenter (landscape 144x256, pinned ONNX export, Apache-2.0) next to the shipped segmenter, and `--edge-quality` compares the masks at the edge (soft-band width, overlap with PP-HumanSeg) on a recorded clip.
- Auto mode can expose with the deterministic controllers, opt-in (`STREAMSTACK_AUTO_METERING=true`; off by default until the model constants are tuned on the real Canon and Litra, #94): the Canon's exposure compensation first, then the key lights, measured in stops with bounded steps; a grey card (`STREAMSTACK_AUTO_GREYCARD`) lets the camera's white balance follow the card.

### Changed
- Encode taps convert straight from the planar program picture to NV12, saving about 6 points of one core at 1080p30 with two taps; the per-frame copy census is in docs/native-video-engine.md (#95).
- The normalised background blur now costs about 8 % more ffmpeg CPU than the plain blur at 720p30 (it was about 50 % more): the divide uses `blend`'s divide mode instead of an expression, and the weight is blurred on one plane. Latency is unchanged. A Vulkan version is not viable on this stack (no divide or masked merge in the Vulkan filters); the evidence is in docs/native-video-engine.md.
- The echo check now reads the network monitor and the SRT path stats: a verdict taken while the network is degraded or a stream is stalling names the link, so a bleed is not blamed on it and a clear result is not mistaken for a clean call.

### Fixed
- A `streamstack chain stop` is no longer undone by the running Console: it leaves a stop marker the supervisor respects, the Audio page says the chain was stopped from the command line, and `streamstack chain start` restores it (#145).
- Pressing M twice quickly could leave the microphone muted. The Console held its optimistic "muted" for a fixed 600 ms, and when the server's snapshot arrived later than that (a loaded machine) the page flipped back to "live", so the next M press sent "mute" again instead of "unmute". The page now keeps what you pressed until the server's snapshot shows it (or five seconds pass, if the server never does), and every action that updates the page ahead of the server gets the same protection (#155).

  The same fault made System page settings seem not to move: the call "beep when muted" checkbox, sensitivity and cooldown were redrawn from the server value on every call update, before the server had saved the click. Those settings, and the same controls for the network monitor, hotkeys, auto-framing, time-of-day slots, the Stream Deck and lighting automation, now update the page at once and keep the value until the server agrees (#155).
- The WebRTC program preview no longer freezes after a stall on the Console host: the video clock now keeps rising when the frame schedule restarts, so the browser no longer drops the following frames as stale (#158).
- Fixed a flaky crash-report rate-limit test that turned main red (#159): the limiter's clock is now injectable and the test deterministic, the window runs from when a report is filed (a stalled disk no longer reopens it), `crash.uninstall` fully reverses `install`, and conftest resets the module's process-wide state around every test.
- The clip-path test no longer fails when the random clip id happens to be all digits.
- The Console no longer stalls a control while the disk is slow (#161). The presets selection is read without waiting for its save, and the telemetry switch turns over in memory before its token and setting are written (a failed save turns an enable back off and says so), so on a starved machine the page shows the change at once instead of after four fsyncs. Slow writes, actions and state snapshots are now logged with their timings (`STREAMSTACK_CONSOLE_ACTION_LOG=1` logs every action), and a failed end-to-end test reports the action posts, the event stream, the server log and a disk probe.
- The release tag pipeline no longer fails on gitleaks flagging the public apt key fingerprint, on `apt-publish` lacking `gpgv`, or on the SBOM asking for the removed `auto` extra. These checks now also run on merge requests, and gitleaks blocks there too. v0.0.18 was never published; the release continues as 0.0.19.
- uv.lock now covers the stills, shortcuts, whep and calendar extras, so the SBOM job on main passes; a unit test fails any merge request whose pyproject.toml changes without a relock.
- Pre-flight history no longer loses runs: saves are written one at a time, newest last, and a run reports done only once it is on disk.
- Test-only: the lighting L-key e2e waits for the page to show the lights off before pressing L again, so a press that beats the state push can no longer send the same command twice.
- Test-only: the meetings preparation test no longer races the demo studio's 1 s start/stop timer on a starved runner (the studio now applies start and stop at once in those tests).

## [0.0.18] - 2026-10-28

### Added
- Network quality monitor (#41): StreamStack probes Teams, Meet and Zoom (STUN or TCP) and your gateway every 5 seconds and rates the connection good, degraded, poor or offline, with the likely cause (Wi-Fi, local network, internet, one service). The System page has a Network card with a sparkline, an editable target list and a Test bandwidth button (it asks first during a call), the top bar shows a NETWORK chip when it is bad. See docs/network-monitor.md.
- Calendar-aware setup (#43): StreamStack reads your next meetings from Microsoft 365 (Microsoft Graph, device-code sign-in) and from calendar links (ICS), and a couple of minutes before an online meeting it starts the studio, turns the camera on and runs the health checks. It never touches the rig during a call. Tokens and private links are kept in the desktop keyring (optional `streamstack[calendar]` extra), never in the settings file. New Meetings card on the Auto page; `docs/meetings.md`.
- Echo check (#48): the Audio page's Output section has an Echo check card. It plays a quiet sweep on the output you pick, listens on the raw microphone and says "No bleed from Dell Monitor", "Bleed at -34 dB from Dell Monitor: use headphones or lower the volume", or that a monitor mix is feeding a feedback loop. It never plays during a call (it refuses to start, and a call that starts mid-check stops the sound at once), tests only the chosen output, never unmutes anything and never reports a silent recording as "no bleed". The last result per output is kept. See docs/echo-check.md.
- Per-account Twitch probe (#82): for an `auto`-mode RTMP destination on Twitch, StreamStack asks Twitch which codecs the account may publish (Enhanced Broadcasting) before it starts, and offers only those, so an account that is not enrolled streams H.264 at once instead of failing with AV1 first. The key goes only to Twitch in the request body and is never logged. Unknown answers fall back to the old behaviour. See docs/stream.md.
- SRT path measurement and SRT input (#83): an SRT caller destination now probes the receiver with an SRT handshake induction packet and shows round trip, spread and probe loss, with a recommended latency of 4 x RTT and a hint when the configured latency is lower. `latency_ms: "auto"` follows the measurement at each (re)connect. A new "SRT input" card and the `ingest.start` and `ingest.stop` actions listen for a remote sender and record it to MKV with no re-encode, re-listening after the sender leaves. ffmpeg exposes no SRT loss or retransmit counters, so none are shown. See docs/stream.md.
- Optional GPU video denoise for noisy cameras: STREAMSTACK_ENGINE_DENOISE=nlmeans runs nlmeans_vulkan before the grade (+6 to +9 dB PSNR on a noisy Canon-like feed, 9 ms of GPU per 720p frame); tools/bench_video_denoise.py compares nlmeans_vulkan, denoise_vaapi, hqdn3d and atadenoise (#92).
- Deterministic auto-exposure and colour-match controllers (stops, dead band, Canon then light, grey-card white balance) with simulated-rig convergence tests (#94).
- The stream now steps its picture down by itself when the uplink cannot carry it, and back up when the link is steady. Choosing a platform and a bandwidth tier on the Presets page gives the stream a ladder of lower quality steps (the rest of the tier, then the lower tiers); packet loss, a growing send queue, dropped frames, reconnects and slow sending move it one rung at a time with hysteresis, as live swaps without reconnecting the destinations. What you chose is never changed: the new Bandwidth card on the Stream page shows the ladder, the step being sent, why it shifted and when it will try to recover, and has a switch to turn it off. `stream.set` takes `adapt` and the Presets `platform`/`tier`; the `stream` state gains `effective` and `adapt` (#98).
- The release now includes a Debian package. `scripts/build_deb.py` builds a reproducible `streamstack_X.Y.Z_all.deb` (`make install` into `/usr`, dependencies on what the distribution provides, udev reload on install), the `deb-build` job smoke-runs it, and `deb-sign` signs `DEB-SHA256SUMS` (the package and its provenance) with Notation and the Key Vault key, as the wheel's manifest already is. The package files go to the Generic Package Registry beside the wheel and are linked from the GitLab Release. `packaging/signing-cert.pem` is the public certificate to verify with; docs/release.md has the verify commands.
- A signed apt repository, an in-toto provenance statement and a tested update and rollback path. `scripts/apt_repo.py` indexes every published package into a flat repository (`Packages`, `Release`), `deb-sign` signs `Release` with the OpenPGP key `streamstack-apt-signing-key` held in Key Vault (`InRelease`, `Release.gpg`; the public key is `packaging/apt-public.asc`), and on a tag `apt-publish` verifies the repository the way apt does and publishes it to the Generic Package Registry at a stable URL. Every release stays in the repository, so `apt install streamstack=<older>` is the rollback. `scripts/deb_provenance.py` writes the SLSA provenance statement of the package from the CI job's variables. `tests/test_apt_repo.py` drives the real `apt-get` against a signed local repository: trust, install, update, rollback, and refusal of an unsigned repository, a changed index, a changed package and a foreign signature.
- The Live page's program picture now arrives over WebRTC (WHEP), about a tenth of a second behind on the demo rig, with the MJPEG picture as the automatic fallback when WebRTC is unavailable or fails. It needs the new `whep` extra (`pip install 'streamstack[whep]'`); without it the Console keeps MJPEG. New routes: `POST /api/whep`, `DELETE /api/whep/<id>`, `GET /api/whep` (#103).
- `streamstack doctor` names a USB device that is plugged in but never started (`usb-stuck`): a port stuck in the kernel's `default` state has no vendor id, so every probe saw nothing and doctor only said to plug in a microphone. It now names the port and says to plug the device straight in with a short, known-good cable (#118).
- Motion-JPEG cameras no longer depend on libplacebo importing VA-API frames (#137). The engine probes three ways to
  decode once per graphics stack (cached by ffmpeg, mesa, VA driver and node): zero-copy, `vaapi-copy` (VA decode, read
  back, upload) and software. This stack gets `vaapi-copy`. The state block, log, doctor line and
  `streamstack engine probe` say which is active; `STREAMSTACK_ENGINE_MJPEG_DECODE` overrides. In the whole engine at
  1080p30 vaapi-copy costs 33 to 41 % of a core and software 40 to 44 %. The zero-copy findings are in #138.

### Changed
- docs/release.md now maps every launch-sequence stage to the job that fills it (including the new package jobs and the opt-in `compliance` stage), and `tests/test_release_stages.py` fails when a job of `.gitlab-ci.yml` is missing from that table or sits in the wrong stage.
- ADR 0008 records why OpenCL filters on VA-API surfaces are not adopted for the engine, with the watch triggers to revisit it (#132).
- Background blur no longer shows a halo of the speaker's colours at the edge: the blurred background is normalised by a blurred weight (`STREAMSTACK_ENGINE_BLUR_EDGE=plain` restores the old blur).

### Fixed
- `streamstack doctor` warns (`camlink-usb`) when the Cam Link 4K is on a USB 2 link: it reports 1080p but delivers about 10 fps over 480 Mb/s, and doctor used to call it ok. The Console snapshot carries `usb_mbps` and `usb2`; the fix is a USB 3 port, plugged in directly (#120).
- CI: `changelog-check` now times out after 10 minutes and retries on a stuck runner, so a hung job can no longer hold the merge train for an hour.
- Rotating the Prometheus scrape token now revokes the old token at once, before the new one is written to disk, so a slow or failing disk can no longer leave the old token working (#149).
- WHEP program preview: a WebRTC picture that freezes while the connection still reads "connected" now falls back to MJPEG after 5 s instead of leaving a stale program picture on screen. The WHEP e2e tests are split into a functional "scene switch reaches the screen" test and a measured (not wall-clock gated on shared CI) latency report.
  Ending a WHEP preview session the Console already dropped now answers 200 instead of 404, so a failed preview no longer logs a "Failed to load resource" console error on the page.
- ADR numbers are unique again: the OpenCL-on-VA-API record is now ADR 0008 (the app shell keeps 0006), and a test keeps ADR numbers, titles and links consistent.
- CI: `deb-sign` budgets the signing pod's image pull (15 minutes) apart from signing (10 minutes), so a cold node pulling the signing image can no longer fail a release tag.
- Two quick choices of the same control (a tier picked, then moved with the arrow keys) no longer land out of order: the page sends the posts of one action in the order it made them, so the older choice cannot overwrite the newer one.
- A Console window that opened at the moment an action landed could show the old state until the next change: the hub read the newest snapshot and only then joined the broadcast, so a push in between reached the window through neither. It showed up as a clicked switch that stayed off. The snapshot, the join and the broadcast are now one step under the hub's lock.
- The tag pipeline's `release-verify` job runs on the full `python:3.13` image instead of the uv slim one, which has no `git`, so `check-tag --require-main-ancestor` works (v0.0.17's tag pipeline failed on it); a test guards the image.
- Three test-only flakes: the demo network loss check accepts the 3-7 percent a small probe window can read, the server test rig freezes the director's minute clock so "state pushed only on change" cannot trip on a minute rollover, and the e2e meter-frame check waits for its first frame on a loaded runner.
- Test isolation: the suite now runs with a throwaway `XDG_RUNTIME_DIR` (and no inherited Pulse, D-Bus or Wayland endpoint) as well as the throwaway HOME and XDG tree, so no test can reach the login's real Console socket and token, Canon bridge or audio server. A `deck.press` once reached the live Console from `test_deck_service` when the suite ran with the real runtime dir. `tests/test_isolation.py` fails if a test can see the real one.

### Security
- The Console no longer writes its access token to the journal. The startup and second-launch log lines name the address without the token, and `streamstack console --url` prints the full tokenised URL of the running Console when you need it (it exits 1 when none is running). A test fails if any log line contains the token.

## [0.0.17] - 2026-10-27

### Added
- Time-of-day profiles can now hold an auto-framing shot (#46): pick Close, Medium, Wide, Presenter or Desk per part of the day, so the morning call is framed close and the evening one wide. Apply, Undo, "Use current" and the held-by-hand rule treat the shot like the look and the lights.
- Looks that name a LUT use the real cube on the GPU (#93). The Console gives the engine the look's LUT name; the
  cube is baked at the look's LUT amount in steps of 0.05 (written once, the child rebuilds when the look rests on a
  new step, about 1.6 s after a look is applied), replacing the colorcorrect split-tone approximation. Golden checks
  against a reference sampler (mean 0.4 to 0.9 of 255 levels). The static `STREAMSTACK_ENGINE_LUT` is fixed too: it
  used libplacebo's `conversion` type, which tinted the picture; both now use `normalized`.
- Doctor and `streamstack seat-policy` report whether a second user's lingering PipeWire/WirePlumber can open the seat user's audio and video devices, print the root script that keeps it off (WirePlumber drop-in, group removal or stop), and `tools/seat_replug_trial.py` measures the effect with Wave XLR replug cycles (#127, ADR 0007).
- `tools/call_matrix.py` runs the camera probe through Chromium, Chrome, Edge, Teams in Edge and Firefox and prints one
  table (a browser that is not installed is a skip); CI runs it on the browsers' own test cameras. The probe also
  drives Firefox now (#87).
- Camera delivery proof (#87, #89): `tools/camera_probe.py` opens headless Chromium or Edge, lists the cameras, finds
  StreamStack Camera by label, opens it with `getUserMedia` and checks frames, motion and picture content, with the
  not-listed failure (an unfed `exclusive_caps` node) reported plainly and the exit status for scripts; CI runs it
  against Chromium's fake camera. `tools/latency_rig.py` measures glass-to-glass latency per path (loopback and browser,
  engine, screen to real camera) from a CRC-protected time stamp painted into the picture, and refuses to write any
  node but a free scratch loopback node. docs/camera-proof.md has the runbook and the list of what still needs a human
  two-party call.
- `tools/latency_rig.py paths` measures every delivery path that needs no person (loopback and browser, the engine at
  720p and 1080p, and their sum) in one command and prints the table; the measured numbers are in
  docs/camera-proof.md, and `run` and `feed` take `--size` and `--fps` (#89).
- `tools/loopback_conformance.py` proves the v4l2loopback contract on a scratch node after a kernel update or a module
  reload (unlisted with no writer, listed with one, contract formats, frames, unlisted again, a second writer works)
  and refuses the program and camera nodes. The distribution's v4l2loopback 0.15.3 is now a note in doctor, not a
  standing warning (#86).

### Changed
- `STREAMSTACK_ENGINE_SCALER` and `tools/bench_scalers.py` (#93). 21 libplacebo scalers are benchmarked (PSNR, SSIM and
  GPU time per frame, zoom and downscale cases); only a measured one can be selected. The default is now `lanczos`
  instead of `ewa_lanczos`: the same quality for 35 to 45 % less GPU time when the picture is zoomed or scaled.
  A VA-API to Vulkan mapping test documents that libplacebo cannot import a VA-mapped frame on this stack (MJPEG
  cameras), as an expected failure.
- Releases are tagged on the release merge request's head commit instead of the merge commit (`release.py prepare` prints the command, docs/release.md explains why): other merge requests merge ahead of it on the train, and the fragments they bring made the tag gate refuse v0.0.14. `release-verify` now also requires the tagged commit to be an ancestor of `main`.\n

### Fixed
- The group-framing cost test judges the quietest of several rounds instead of one wall-clock p95, so a loaded CI runner no longer fails it while a real slowdown still does. Tests only.

## [0.0.16] - 2026-10-27

v0.0.14 was never published (its release commit merged behind other merge requests whose changelog fragments were still in `changelog.d/`, so the release gate refused the tag); its changes shipped in 0.0.15.

### Added
- Auto-framing 2 (#46): the crop now frames everyone in the picture (a group window that tracks the people who stay, never a passer-by), can hand the shot to whoever is talking (mouth movement from the face landmarks, gated by the raw microphone's voice activity, with hold timers so it does not ping-pong) and has shot presets (Close, Medium, Wide, Presenter and a fixed Desk crop that runs no detector). When nobody is found the crop widens, then returns to the home framing after six seconds. The Camera panel gains the shot buttons, a Who-to-frame choice and a Follow speaker switch; the choices persist. New actions `framing.shot`, `framing.subjects` and `framing.handoff`. A replay benchmark (1, 2 and 3 faces, alternating speakers) gates the framing quality in CI. See docs/auto-framing.md.
- Warmth and sharpen run on the GPU (#58). Warmth is libplacebo's colour temperature, live; sharpen is a
  contrast-adaptive sharpening shader whose strength is baked in steps of 0.05 (the child is rebuilt, about 0.22 s,
  when the slider rests on a new step). The camera goes up and comes back as 4:2:2, so the CPU 4:4:4 stages are gone:
  at 1080p30 the engine's CPU for a sharpened look drops from 247 % to 28 % of a core (all sliders: 254 % to 29 %;
  neutral: 52 % to 29 %), for 70 to 110 ms more GPU time per second. `STREAMSTACK_ENGINE_SHARPEN=cpu` keeps the CPU
  path, which is also the fallback when the shader cannot be written. The cool side of warmth is milder than the old
  filter. `tools/bench_look.py` measures the cost of each look.
- `streamstack doctor` has a `video-pipewire` row: StreamStack Camera is also a PipeWire camera for the portal path
  (Firefox, Chromium with the flag, `pw-v4l2`), or a warning with the WirePlumber fix when it is fed but not
  published. ADR 0004 records that StreamStack publishes no node of its own (#88).

### Changed
- Wave XLR: 48 V phantom power is refused while a call is live (or the call detector fails), and the page warns when the kernel's cached capture gain disagrees with the device's own gain (#18).

## [0.0.15] - 2026-10-27

v0.0.14 was never published: its tag was refused by the release gate because changelog fragments from merge requests that landed on the train after `release.py prepare` were still in `changelog.d/`. Everything listed under 0.0.14 below shipped in this release instead.

### Added
- Global hotkeys and MIDI (#44): bind a keyboard shortcut or a MIDI note, button, fader or program change to any Console action (the same options as a Stream Deck key), and they work while the call window has focus. On an X11 session the keys are grabbed directly (and a combo another program holds is shown as "In use elsewhere"); on Wayland they register through the XDG GlobalShortcuts portal (optional `streamstack[shortcuts]` extra). MIDI ports are read from ALSA with hot-plug, and Learn fills the form from the next control you move. Faders can scale into an action parameter. Bindings live in `~/.config/streamstack/hotkeys.json` and export and import as JSON. New "Hotkeys and MIDI" card on the Deck page; `docs/hotkeys.md`.
- The engine, the GPU and the Canon live view rest while no app reads StreamStack Camera (#128, docs/camera-delivery.md).
  A governor (`engine/idle.py`) watches who holds `/dev/video43` open (inotify on the node as the trigger, a `/proc` scan
  as the answer, never opening the device) and idles the engine after 10 s without a reader (`STREAMSTACK_ENGINE_IDLE`
  = `auto`/`off`, `_AFTER`, `_IGNORE`). While idle a quiet "Camera idle" placeholder at 5 fps keeps the camera listed
  for Teams and Edge, and the Canon bridge ends live view by lease (`rest`, renewed by the Console, so a dead Console
  never leaves the body dark). The first reader, confirmed for 0.25 s so an Edge probe that opens and closes the device
  does not flap it, wakes the engine; the wake time is measured and shown. A Console preview, a running stream or
  recording, and the new **Keep awake** switch on the Camera page count as demand. The Camera page shows an `ENGINE idle`
  chip and the Canon card says live view is off; `streamstack doctor` gets an `engine-idle` row;
  `state.video.engine.idle` carries the details and `video.keepalive` is the new action.
- Doctor now says why a Focusrite Scarlett shows no controls: not on USB, on USB without a sound card, MSD (Easy Start) mode, firmware too old, or healthy, with the kernel line and the fix command (`hw-scarlett`, read-only; it never flashes or reboots the interface) (#130).
- v4l2loopback contract and hardening (#86): `streamstack doctor` and the new `streamstack loopback guard` report a
  v4l2loopback module that is missing or not built for the running or newest installed kernel (with the DKMS fix), a
  loaded version below 0.15.4, a format outside the Chromium contract (YUYV or NV12, one even size, 24 to 60 fps,
  `exclusive_caps` on), and a competing producer on the program or camera node with the program and pid that holds it
  (for example a leftover OBS virtual camera). `make install` adds a kernel postinst hook that runs
  `dkms autoinstall` for each new kernel. See docs/loopback-contract.md.

### Changed
- The AI benchmark (`tools/bench_ai.py`) now picks the discrete Arc by device type like auto-framing does, points `intel_gpu_top` at that GPU (`--gpu-top-sudo` for CAP_PERFMON), gives each rig its own compile cache and has a `--measured` mode for real-hardware runs on generated frames; docs/hardware-ai-evaluation.md holds the first Arc A380 numbers and the decision-gate verdicts (#22).
- The GitLab release host is configuration only. `streamstack verify-install`, `streamstack chain install-plugin` and `scripts/install-release.sh` read it from `STREAMSTACK_GITLAB_HOST` (CI: `CI_SERVER_HOST`; the install script also falls back to the checkout's git remote) and the project from `STREAMSTACK_PROJECT_ID`. When no host is set they say so and print a `$STREAMSTACK_GITLAB_HOST` placeholder instead of a hardcoded private host, and a test guards the shipped code and docs against a private hostname.

### Fixed
- The deck e2e drags a key in acknowledged steps through the page's own drag handlers (a shared DataTransfer, no dependence on how the browser turns mouse events into a drag), checking the page took each one, so a stalled runner can no longer lose the drop. Tests only.
- The lights-status tests no longer fail on a busy disk: they stamped the heartbeat and then wrote the status durably (fsync), which can outlast the ten seconds a heartbeat stays fresh; they now write it as the service does. Tests only.

## [0.0.14] - 2026-10-27

### Added
- Time-of-day look profiles (#42): one profile for each part of the day (night, dawn, morning, midday, afternoon, evening)
  that sets a look, a scene, a voice preset, a lighting preset, the Canon's exposure compensation and the background
  blur. The Camera page has a "Day" popover with a strip of the day, a mode (Off, Suggest or Automatic) and a row per
  slot. A profile applies when a call starts or the slot changes between calls (never in the middle of one), lights
  fade to the new level, a manual change holds the profile until the slot ends, "Undo" puts the picture back, and with
  the AI Director on a profile seeds the Director's baselines instead of competing with it. New `slots.*` actions,
  Stream Deck keys, and `GET /api/slots/profiles.json`. Details: docs/slots.md.
- Crash-safe local recording (#81): recordings keep the **raw microphone as a third track** (track 2 stays the processed program sound; live destinations never receive it), a **disk guard** refuses to start under twice `STREAMSTACK_RECORD_MIN_FREE_MB` (default 1024), warns once, and ends the recording cleanly at the minimum with the file saved, and a recording killed with `kill -9`, an out-of-memory kill or a power cut is **found by its `<file>.recording` marker and repaired at the next start** (an indexed remux, verified before it replaces anything). The Stream page shows free space and the time left. Tests kill a real recorder with SIGKILL and stop a real Console with SIGTERM. See docs/stream.md (Crash-safe recording).
- Stills (#85): `streamstack stills` writes AVIF snapshots (`snapshot`) and recompresses camera JPEGs to JPEG XL reversibly (`archive`, `restore`): every archive is rebuilt and compared byte for byte with the original before it is kept, and the JPEG is removed only on request after that proof. Runs on the system libjxl or the new `stills` extra (`imagecodecs`). See docs/stills.md.
- StreamStack Camera stays listed when the real camera goes away (work package C, docs/camera-delivery.md). On
  dm313red the external PTP bridge died after a reboot, nothing wrote `/dev/video43`, and Teams in Edge lost the
  camera without any alarm. The engine supervisor now asks a guard before each start (the source node exists and has a
  producer) and waits instead of failing, stops a child whose frame counter stalls (ffmpeg blocks in the V4L2 read when
  a loopback producer disappears), and a "Camera reconnecting" slate writes the program node at the engine's own
  size and rate while no engine child does (`STREAMSTACK_CAMERA_LOSS` = `slate`, `hold` or `off`). With the Cam Link
  connected the engine fails over to it after 6 s and returns to the picked camera after 5 s of a steady picture
  (`STREAMSTACK_CAMERA_FALLBACK`, `_AFTER`, `_BACK`). The Console shows the state as a System-page row and in
  `video.engine.delivery`; `streamstack doctor` gets a `video-chain` row ("nothing feeds /dev/video43").

### Changed
- Changelog entries are per-merge-request fragments (#136). Merge requests no longer edit `CHANGELOG.md`, which
  serialised every parallel MR on conflicts: each adds `changelog.d/<issue>.<type>.md` (`added`, `changed`, `deprecated`,
  `removed`, `fixed`, `security`), and `scripts/release.py prepare` compiles and deletes them. The new `changelog-check`
  job fails an MR that edits `CHANGELOG.md` or changes `src/` without a fragment (opt out with the `skip-changelog` label
  or a `Skip-Changelog: <reason>` trailer), and the tag gate refuses a release that still has fragments.

### Fixed
- `streamstack chain stop` no longer brings back the wrong default microphone (#142). It used to restore whatever default was recorded when the chain started, which on dm313red was the Cam Link's HDMI audio (twice), and it left StreamStack Mic as the default when that record was missing. The default now goes to the chain's own capture target, the Plan's microphone (the Wave XLR), and never to a camera's audio; a microphone that is not plugged in is never named, the key is cleared instead. `tools/xlr_capture_order.py` now defaults to a 6 s window, because the XLR delivers its first buffer about 2 s after open.

## [0.0.13] - 2026-10-27

### Added
- Voice denoise bake-off (#131): `tools/bench_denoise.py` scores DeepFilterNet3, RNNoise and DPDFNet on pinned public corpora and our own room noise (PESQ, STOI, SI-SDR, DNSMOS, CPU, delay). DeepFilterNet3 stays the default; DPDFNet-2 is a peer at less than half the CPU but needs a bridge process. RNNoise adds about 130 ms in the real chain, more than the latency report assumes. Decision record: docs/adr/0005-voice-denoise-default.md.
- Call awareness (#45): StreamStack now knows when a Teams, Zoom, Meet or other meeting call is in progress (idle, joining, in call) from which apps hold the camera open and capture the microphone, never from the audio itself. The top bar shows an IN CALL chip, and a toast warns once when you talk into a call that is muted in the app. The System page has a Call awareness card for the sensitivity, the wait between warnings and an optional beep. The detection is a small public API (`streamstack.callstate`: `current_call()`, `CallMonitor`) that other features, such as the echo chirp, use to stay silent during a call. See docs/call-awareness.md.
- e2e: `rig.wait_for_state(path, expected, tol=)` waits for the server to reach a value the page shows, and a guard test
  (`tests/test_e2e_state_reads.py`) refuses a direct server-state read straight after a click, key press or drag, or
  anywhere after a fader drag, unless the line above says `# state-read: <why>`. It is the class behind the e2e flakes
  of pipelines 96021 and 96223 (a UI post that had not landed when the test sampled the server).

### Fixed
- `test_mic_check_runs_to_done_and_both_clips_load` failed on a busy runner (pipeline 96315, MR !67 which does not touch
  audio; the page showed "Done" and never "Step 2 of 2"). The demo's takes are timed by the wall clock and the page learns of
  each step only on the hub's next push, so a 5 s step can pass unseen when the runner stalls. The demo rig gains two
  demo-only actions, `audio.demo_check_hold` and `audio.demo_check_step`, and the test holds the timeline and ends each
  take itself, so the page is shown every step for as long as it needs. No timeout was raised; no real-hardware path changes.
- `test_an_open_mic_never_warns_and_the_warning_can_be_switched_off` failed on !93's pipeline 96527 (`assert 1 == 0`).
  The e2e reset (`call.demo_reset`) left the demo call monitor in_call and muted for the leave hold, long enough for the
  demo's speech-like meters to raise a warning on the fresh watcher. The reset now drops the call at once
  (`CallMonitor.reset`). Demo rig only.
- e2e: Firefox `Page.goto` 60 s timeouts on the CI runner (#124). Both recorded cases (pipelines 87280 and 87570) ran
  while the runner pod waited on "12 Insufficient cpu", and the Console's first load is a 54-module graph that Firefox
  must fetch and evaluate before the `load` event. The e2e `goto` now navigates at `commit` and waits for the page's
  own ready marks, and a failure prints the requests still in flight, so a recurrence names the stuck resource.
- The DeepFilterNet3 image build failed on main (pipeline 96222: `could not load config key profile.release`): the one-codegen-unit setting for the custom `release-lto` profile is now passed with `cargo --config`, and a test refuses `CARGO_PROFILE_*` variables in the Dockerfile. BuildKit builds dispatch only from protected refs, so this job cannot run on an MR pipeline; the Dockerfile is checked statically there.
- The DeepFilterNet3 rebuild jobs are allowed to fail (shown as warnings): a Harbor base-image proxy outage (502) turned main red even though the published, pinned plugin is unaffected.
- StreamStack Mic can no longer read the wrong microphone (#139). On 2026-10-27 the Console-started chain linked its capture to the Cam Link 4K's HDMI audio instead of the Wave XLR, so StreamStack Mic carried exact zeros while `streamstack doctor` was green. Cause: the capture named the microphone with `target.object`, but WirePlumber honours that only if the node exists when the stream links; the XLR source was not there yet at login, so WirePlumber fell back to the default source. The capture now also sets `node.dont-fallback` and `node.linger`, so it waits for the Plan's microphone and links the moment it appears, and the Console rebuilds the host when the capture is linked to anything else (or stays unlinked while the microphone is present).
- `streamstack doctor` and the Console health list read the real links (`pw-dump`): the new `voice-chain-source` check fails unless StreamStack Mic reads the Plan's microphone, and `voice-chain-silence` fails when StreamStack Mic itself delivers digital silence while the raw microphone does not.
- The DeepFilterNet3 jobs no longer turn main red on every pipeline: they run only when `images/dfn-ladspa/**` changes (or on a manual `DFN_LADSPA_REBUILD=1` pipeline), and an already published `streamstack-ladspa` version passes when it is the build the client pins. The build also sets `SOURCE_DATE_EPOCH`, one LTO codegen unit and a rustup path remap.
- `test_arrow_keys_move_the_tier` failed once on main (pipeline 96021, tier stayed `balanced`): the test clicked Balanced
  and pressed ArrowRight straight away, so two `presets.tier` posts raced on the threaded server and the click could
  land last. The test now waits for the first to land and show before sending the second. No product change.
- `test_mic_fader_drag_changes_gain` failed once on main (pipeline 96223, `8.0 == 5.5`): it compared the fader with the server
  after sampling a mid-drag value, before the drag's last throttled post had landed. It now waits for the server to reach
  the value the fader shows. No product change (the fader already sends one post at a time and flushes on release).
- `test_demo_studio_state_is_a_pure_function_of_its_clock` failed once on main (pipeline 96238): the demo state includes the
  lighting schedule's time of day, so two snapshots straddling a minute boundary differed. The test pins the demo clock.
- `test_a_burst_of_events_is_one_probe` failed once on main (pipeline 96222, two probes for one burst): the burst was timed
  against the real 50 ms debounce, and a CPU-starved reader thread can stall longer than that between two events, which
  the quiet-period debounce rightly treats as two bursts. The test drives the monitor's injected clock instead. The
  debounce itself is correct and unchanged.

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
- `/metrics` no longer waits on a token write. Every request to the endpoint, even one with a wrong token, took the
  telemetry lock to read the scrape token, and a token rotation or the telemetry switch held that same lock across
  fsynced file writes, so on a CPU- or disk-starved node a scrape could stall for seconds behind them (main pipeline
  95914, one e2e scrape that hung 15 s). The token is now read from memory without a lock, writers have their own
  lock, and a regression test holds a rotation mid-write and checks a scrape is still answered at once.

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
