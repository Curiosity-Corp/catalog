# Call apps: matrix and runbook (#87)

Which call apps were tried against StreamStack Camera and StreamStack Mic, what each one proved, and what to do at the
desk when a call goes wrong. Everything marked "measured" was run on dm313red (Ubuntu, kernel 7.0.0-38, PipeWire 1.6.2,
v4l2loopback 0.15.3) with the real devices, no fake capture. deskmeet1 is offline and out of scope by CEO decision.

## Matrix

"Camera" and "Mic" say whether the app (or browser) opened the StreamStack device and got a live signal. A live picture
means a decoded frame with a normal brightness spread, not black or flat; a live microphone means non-zero samples
(`digitalZero: false`). Files are under `docs/evidence/call-apps/` (this change) or `docs/evidence/live-call/` and
`docs/evidence/virtual-devices/` (earlier work, linked by name).

| App | Client | Camera | Mic | Evidence | Date | Version |
|---|---|---|---|---|---|---|
| Chromium | Playwright Chromium, real devices | live 1280x720 @ 30 | live (rms 0.0106, peak 0.063) | `chromium-streamstack-devices.png`, `.json` | 2026-11-04 | Chromium 156 (Playwright 1248), StreamStack 0.0.28 |
| Chrome | stable, camera probe | live (luma 31, spread 47) | not run | `call-matrix-0.0.28.md` | 2026-11-04 | Chrome 157.0.8103.38, 0.0.28 |
| Edge | stable, camera probe | live (luma 31, spread 47) | not run | `call-matrix-0.0.28.md` | 2026-11-04 | Edge 156.0.4314.58, 0.0.28 |
| Firefox | system Firefox, headless | live 1280x720 @ 30 | live (rms 0.0043, peak 0.023) | `firefox-streamstack-devices.png`, `.json`; stall note `firefox-cam-link-stall.txt` | 2026-11-04 | Firefox 159.0.1, 0.0.28 |
| Microsoft Teams | web, Chromium, two-party call (A on StreamStack, B a guest) | live: B decoded 700 frames of A at 1280x720 | live: B's `totalAudioEnergy` 0.0219, captions transcribed the room | `live-call/dm313red-teams-*.png`, `dm313red-teams-getstats.json`, `teamscall.py` (!203) | 2026-11-03 | Chromium 156, 0.0.26 |
| Microsoft Teams | PWA (Edge "install app") | **not installed** (no launcher, no package); the web client above is the verified path | same | none | 2026-11-04 | n/a |
| Microsoft Teams | desktop | **not installed**; the Electron app is retired (ADR 0004); web client verified | same | none | 2026-11-04 | n/a |
| Zoom | web client, in a meeting (zoom.us/test) | live: "Do you see yourself?" names StreamStack Camera over the picture | tracks showed `audio:StreamStack Mic`; Zoom's audio test step reached | `live-call/dm313red-zoom-*.png`, `zoomcall.py`; device list in `virtual-devices/zoom-web-*.png` | 2026-10-29 | Chromium 156 (Playwright 1248), 0.0.22 |
| Zoom | desktop | **not installed**; web client verified (doctor row "Zoom desktop client: not installed, calls use the zoom.us web client") | same | `dm313red-doctor-0.0.28.txt` | 2026-11-04 | n/a |
| Google Meet | browser | **not reached** (needs a Google sign-in; see below) | **not reached** | `meet-anonymous.txt` and `.png`, `meet-signin-attempt.txt`, `meetprobe.py`, `meetsignin.py` | 2026-11-04 | Chromium 156 |

Nothing in the table says "Meet works". The Meet rows are the honest result of what could be reached, and Chromium and
Edge opening both devices is the closest proxy (Meet runs in the same browsers), not a Meet test.

### Camera in use in the Console (acceptance: visible)

Already shipped, so this change adds evidence and no code: the Console System page has a **Seen by apps** card for each
virtual device (`src/streamstack/data/console/pages/system.js`, fed by `streamstack.virtualdevices` from
`/proc/<pid>/fd`, never by opening a node), a header chip **IN CALL <APP>** from call awareness
(`docs/call-awareness.md`, `src/streamstack/callstate.py`), and the same facts in `streamstack doctor` ("seen by apps").
Camera follow for the lights uses `src/streamstack/camerause.py`. Tests: `tests/test_camerause.py`,
`tests/test_virtualdevices.py`, `tests/test_callstate.py`.

Live proof on 2026-11-04: `consoleseen.py` held both devices open in one Chromium and read the running Console's System
page in another. The card read "StreamStack Camera: Chrome in a browser tab" and "StreamStack Mic: pacat, Chrome in a
browser tab", the section said "3 open", and the header showed IN CALL CHROME: `console-seen-by-apps-live.png`. An
earlier capture with a simulated rig is `virtual-devices/console-system-seen-by-apps.png`.

### What was not reached, with the exact errors

**Google Meet, rung (a), an authorized Google account.** Bitwarden was searched for metadata only (item id and name,
never a value). The one Google account in the vault that is the CEO's own with a `matthew@` address on a Workspace
domain was tried once, in Chromium, as `meetsignin.py`. Google answered on the password step:
`Wrong password. Try again or click "Forgot password?" for more options.` The stored password is stale. It was not
retried (lockout risk) and no other person's account was tried. `matthew@focushive.com` is Microsoft, not Google. Two
other Google items in the vault are a personal Gmail address, which is outside the scope that was given, and an
unqualified `matthew` login from a Workspace-for-nonprofits signup that carries no domain. A current password for the
CEO's Google account (a vault update) is what unblocks this.

**Google Meet, rung (b), no account.** Chromium 156 on the real devices, as `meetprobe.py`:

| Address | Where it ended |
|---|---|
| `meet.google.com/landing` | `accounts.google.com/v3/signin/identifier`, page "Sign in / Use your Google Account / Email or phone" |
| `meet.google.com/new` | the same sign-in page |
| `meet.google.com/abc-defg-hij` (made-up code) | `meet.google.com/meet/abc-defg-hij`: "You can't join this video call ... No one can join a meeting unless invited or admitted by the host" |

Meet shows no camera preview and no device picker without an account, so "StreamStack Camera" and "StreamStack Mic" never
appeared in Meet itself. Screenshots: `meet-anonymous-*.png`.

**Zoom desktop, Teams PWA, Teams desktop.** Not installed (see the matrix). They were not installed for this test.

**Teams keeps the camera through an engine restart.** Not measured live (call desk rule: the installed Console and engine
are not restarted on a working desk). The analysis from the code is below.

## What a call app sees when the engine restarts (not measured live)

Two different things are called a restart. Both are read from the code and its tests, not from a call.

1. **The engine's ffmpeg child restarts while the Console stays up** (a fault, a stall of 4 s, an idle wake, a source
   change). `/dev/video43` is a v4l2loopback node with `exclusive_caps=1`: a camera only while a process writes it. On a
   fault the delivery guard covers the node at once (`console/delivery.py` `on_exit` calls `cover`) with the slate, a
   second small ffmpeg writing the same format (`engine/slate.py`, `docs/camera-delivery.md` Resilience 3 and 4). **The
   call app sees the "Camera reconnecting" card** (or, with `STREAMSTACK_CAMERA_LOSS=hold`, the last frame darkened and
   blurred under that line), and live picture again once the source proves a frame; at the hand-back the card holds its
   last frame for about a second while the child starts. It keeps its capture handle and does not see black. Tests:
   `tests/test_missing_source_writer.py` (`test_the_live_picture_takes_over_when_the_camera_returns_and_back_again_when_it_goes`,
   `test_a_child_that_stalls_after_a_long_run_is_still_covered_and_proven_again`), `tests/test_engine_slate.py`,
   `tests/test_console_native_idle.py` (idle placeholder and wake). A deliberate, planned child restart (a source change)
   is not a fault, so no slate is written for it: the picture pauses for the length of the graph set-up (about a second,
   `docs/camera-delivery.md` Idle 'wake') and then continues. An app that holds the handle sees **a short freeze, not
   black**.
2. **The whole Console or engine service stops and starts** (`systemctl --user restart ...`, a crash, an upgrade).
   `delivery.stop()` clears the slate and the engine child is terminated, so for the time the service is down **nothing
   writes the node**. The node is then unlisted for apps that open it afterwards (contract step 5 in
   `docs/loopback-contract.md`: "unlisted again when the writer exits", tested by `tools/loopback_conformance.py`), and
   an app that already holds the handle gets no new frames, which it shows as a **frozen last frame** until a writer is
   back; it is not a slate. On start, the guard shows the slate first (a leftover slate of a crashed Console is stopped by
   its pid file, `test_a_leftover_slate_of_a_crashed_console_is_stopped_before_the_next_one_starts`), then the engine takes
   over. Whether a given Teams or Zoom build keeps the device through that gap is the open question in
   `docs/camera-proof.md` section 4, and only a call answers it: **not measured live (call desk rule)**. Rule of thumb
   for the desk: never restart the Console during a call.

## Runbook

### Before a call

1. `streamstack studio` (or the Console's Studio card) first: a browser lists the loopback camera only while it has a
   picture. The engine rests when no app reads the camera and wakes by itself when one opens it, so "Camera idle" in the
   Console before the call is normal.
2. `streamstack doctor`. Everything under Video and Audio should be `ok`. `streamstack call-settings` prints the toggles
   to turn off in each app.
3. Join the vendor's test first: Teams "Make a test call", Zoom `zoom.us/test`.

### Picking StreamStack Camera and StreamStack Mic

| App | Where |
|---|---|
| Teams (web, PWA) | Settings > Devices: Camera "StreamStack Camera", Microphone "StreamStack Mic", Speaker the headphones. Noise suppression Low. No background effects or blur (StreamStack frames and grades). In the prejoin screen the same pickers sit under the camera toggle. |
| Zoom (web or desktop) | Settings > Video: Camera "StreamStack Camera", HD on, Touch up and Adjust for low light off. Settings > Audio: Microphone "StreamStack Mic", automatic volume off, Suppress background noise Low. In the web client's prejoin page the menus beside the camera and microphone buttons do the same. |
| Google Meet | Prejoin: the arrows beside the camera and microphone buttons. In the call: three dots > Settings > Video and Audio. Not verified (see above), the names are the same two. |
| Chromium, Chrome, Edge | the lock or camera icon in the address bar, or the page's own device menu. The first permission prompt lists every camera: choose StreamStack Camera. |
| Firefox | the camera and microphone drop-downs in the permission prompt. Firefox lists a camera only after it has been allowed once; before that the list has one blank entry. |

If an app remembers another camera, pick again: apps store the device by id, and a replug or a re-created node changes it.

### The camera is black, missing or busy

Start with `streamstack doctor` and the Console System page (the Video group and the **Seen by apps** card), then:

| What you see | Meaning | What to do |
|---|---|---|
| StreamStack Camera is not in the app's list | nothing writes `/dev/video43` (`exclusive_caps=1` hides an unfed node); doctor says "StreamStack Camera is not listed by Teams, Zoom or Chrome: nothing feeds /dev/video43" and names the broken link | start the studio; if the source is the Canon, check the Canon bridge row; `streamstack loopback guard` shows who holds the nodes |
| Only Cam Link and Canon, no StreamStack Camera, in Firefox, after a boot | WirePlumber kept a stale answer (#176) | `streamstack camera-publish` (refuses during a call, see rules) |
| Picture is the "Camera reconnecting" card | the source (Canon bridge or Cam Link) has no picture; the slate is the only writer, on purpose | Camera page in the Console: the reason is on the card; replug or restart the bridge between calls; the picture returns by itself |
| "Camera idle" card | the engine rests because no app read the camera for 10 s | nothing: it wakes when an app opens the camera |
| App says "camera in use", `NotReadableError: Could not start video source`, or black | another process holds the node, or a second reader raced the writer's format | Console System > Seen by apps shows who; `fuser -v /dev/video43`; doctor row "only StreamStack feeds /dev/video43" names a second writer (an OBS virtual camera, a stray ffmpeg). Close that program. Since #181 the Console and the recorder never read the node, so a Console window is no longer a cause. |
| Firefox: the camera request never answers | Firefox's V4L2 probe of an Elgato Cam Link without an HDMI signal does not return and blocks every camera (`firefox-cam-link-stall.txt`); intermittent | give the Cam Link a signal or unplug it, or use Chromium or Edge for that call |
| Black picture, doctor all `ok` | privacy (blackout) is on, or the lens is covered | Console Camera page: privacy off |

### The microphone is silent or missing

1. Console System page: the **Audio** rows; `streamstack mic-check` lets you hear the processed voice.
2. `streamstack mic-revive` (the Console's Revive mic button, Shift+R). It re-writes the capture switch, suspends and
   resumes the source, then cycles the card profile, and re-checks after each step. It refuses while audio is flowing
   unless `--force`.
3. If it ends with "replug the USB cable" or the Wave XLR has no input profile: between calls only,
   `streamstack mic-revive --reprobe` (restarts WirePlumber, drops every audio stream of the user for a moment).

### Call-desk rules: who may restart what, and when

A call is on when the Console header shows IN CALL, or `fuser -v /dev/video42 /dev/video43` or
`pactl list source-outputs` shows a call app (msedge, teams, zoom, chrome, firefox) other than your own test. StreamStack's
own ffmpeg and pacat do not count.

| Action | During a call | Between calls |
|---|---|---|
| Read: doctor, Console pages, `streamstack lights status`, `fuser`, `pactl list` | anyone, any time | anyone |
| Console Revive mic / `streamstack mic-revive` | the person on the call, when their mic is already dead | anyone |
| `mic-revive --reprobe`, `camera-publish`, restarting WirePlumber or PipeWire | never | the desk owner or an agent after the guard below |
| Restart the Console, the engine, the studio, the audio chain, the Canon bridge | never | the desk owner or an agent after the guard below; afterwards `streamstack doctor` and a test call |
| Reload v4l2loopback, reboot | never | only with the Console stopped and no call (`docs/loopback-contract.md`, After a module reload) |
| Replug the Cam Link, Canon or Wave XLR | never | desk owner |
| A test that opens the camera or microphone (the scripts in `docs/evidence/call-apps/`) | not unless it is the call | only after the guard below |
| Stopping a stuck process | by PID, and only your own; never `pkill` by name | same |

The guard before any test or restart:

1. `fuser -v /dev/video42 /dev/video43` and `pactl list source-outputs`: look for msedge, teams, zoom, chrome, firefox.
2. If a call app that is not yours holds a camera or mic, do not test. Look again every 5 minutes, for up to 2 hours.
3. Never open `/dev/video42` or `/dev/video43` for writing; never restart the Console, engine or audio on a working desk.
4. A camera test turns the Litra beams on. Afterwards wait up to 5 minutes and check `streamstack lights status`: both
   beams Off. If one is not Off after 10 minutes, read `journalctl --user -u streamstack-lights --since -10min`.

## Re-running the evidence

Python with Playwright installed (`pip install playwright`; `playwright install chromium`), one browser at a time, the
guard above first. In `docs/evidence/call-apps/`:

| Script | What it does |
|---|---|
| `devicecheck.py` with `devicecheck.html` | Chromium, Chrome or Edge opens both devices, reports the list, track settings, a frame's brightness and the microphone level |
| `firefoxcheck.py` | the same page in the system Firefox with a throw-away profile; `--hide /dev/video0,/dev/video1,/dev/media0` masks a stalled Cam Link from that one process |
| `consoleseen.py` | holds the devices in one browser and screenshots the Console's Seen by apps card from another |
| `meetprobe.py`, `meetsignin.py` | the two Meet rungs |
| `tools/call_matrix.py` | the camera probe through Chrome, Edge, Teams in Edge (and Firefox through Playwright when its build matches) |
| `live-call/teamscall.py`, `live-call/zoomcall.py` | the Teams two-party call and the Zoom web meeting |
