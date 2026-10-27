# ADR 0004: The virtual camera is a v4l2loopback node, the virtual microphone is a PipeWire source

Status: accepted (2026-10-24).

## Context

Calls must see two devices the owner controls: a camera carrying the graded program picture, and a microphone carrying
the processed voice. Today the engine writes `/dev/video43` ("StreamStack Camera", a v4l2loopback node) and the voice
chain exposes "StreamStack Mic" (`streamstack_mic`, a PipeWire filter-chain `Audio/Source`). The question was whether
PipeWire-native camera nodes should replace the loopback, and whether anything stops Teams and Zoom from listing or
opening either device.

What clients enumerate (October 2026; sources at the end, tested rows are ours):

| Client | Camera enumeration | Microphone enumeration |
|---|---|---|
| Teams (web and PWA, Edge or Chrome; the Electron app is retired) | Chromium `enumerateDevices`: V4L2 nodes by default. The PipeWire camera path is behind a flag, off by default, with two extra permission prompts. | PulseAudio API (pipewire-pulse) sources |
| Zoom for Linux (desktop) | V4L2 only. No evidence of a PipeWire camera path; PipeWire is used for screen sharing. | PulseAudio API sources |
| Zoom web client | Chromium or Firefox `enumerateDevices` | same |
| Firefox | V4L2 by default upstream; the PipeWire camera backend (`media.webrtc.camera.allow-pipewire`) is distro-dependent | PulseAudio API |

## Decision

1. **Camera: v4l2loopback stays the only camera path.** It is the one thing every client above lists. A native PipeWire
   camera node would be invisible to Zoom desktop and to stock Chromium. The engine keeps writing 1920x1080 YUYV 30 fps
   (what `ffmpeg -f v4l2` hands the node); WirePlumber's V4L2 monitor additionally publishes the same node as a PipeWire
   `Video/Source` for clients on the PipeWire camera path, at no cost.
2. **Microphone: the PipeWire filter-chain source stays.** It is a plain `Audio/Source` with `node.description`
   "StreamStack Mic", mono 48 kHz float, not a monitor, so Chromium, Firefox and Zoom list it through pipewire-pulse.
   No ALSA loopback or PulseAudio null sink is needed.
3. **The node is configured declaratively** (`packaging/`): `modprobe.d/streamstack-v4l2loopback.conf`
   (`devices=2 video_nr=42,43 card_label="Canon EOS 50D,StreamStack Camera" exclusive_caps=1,1 max_buffers=2`),
   `modules-load.d/streamstack-v4l2loopback.conf`, and `udev/61-streamstack-video.rules` (group `video`, mode 0660).
   `make install` ships them. The host's line must equal the shipped one (a test pins the label, the number and
   `exclusive_caps`); on managed hosts the ansible catalog owns the file.
4. **`exclusive_caps=1` is mandatory.** Without it the node claims capture and output at once and Chrome, Teams and Zoom
   skip it. The side effect, "an unfed node is not listed", is what `streamstack doctor` already reports as "no picture
   yet".
5. **Resolutions.** A loopback node offers exactly the one format its producer set. We do not publish several sizes:
   Chromium scales a 1080p source to the 1280x720 that Teams asks for (measured: constraint 1280x720 gives 1280x720 at
   30 fps from the 1080p node) and Zoom takes the native size.
6. **Self-diagnosis** (`streamstack doctor`, Console System page, module `streamstack.virtualdevices`), read-only and
   never opening a `/dev/video` node: name equals the expected label, `exclusive_caps` on for this node (from the
   module's sysfs arrays), the user can open the node, the offered format is one calls take and at least 720p at 24 fps,
   the mic is a non-monitor source called "StreamStack Mic", and **seen by apps**: V4L2 holders from `/proc/<pid>/fd`
   and microphone readers from PipeWire links (`pw-dump`), shown as a card per device.

## Found on dm313red (this host)

The loopback module is loaded with exactly the configuration above, and the voice chain is fine. The blocker is access:
`/dev/video42`, `/dev/video43` and the Cam Link are `root:video 0660` with an ACL for `lightdm` only. User `mrh` is not
in `video` and its session (a remote `tty` session, no logind seat) never gets the seat ACL. Every V4L2 open by `mrh`
fails with "Permission denied", WirePlumber publishes no `Video/Source` nodes at all (`wpctl status` shows an empty
Video section), and any call app run as `mrh` has no camera. `streamstack doctor` used to say "no picture yet" here; it
now says "this user cannot open /dev/video43" with the fix. With the `video` group the same machine passes everything
below.

### Host change request (ansible catalog, curiosity workstation role, streamstack task group)

Not applied by hand. Please add: user `mrh` (and any other call user) in the `video` group; the modprobe line, the
modules-load line and the udev rule above unchanged (the modprobe and modules-load lines already match what is
installed). No reboot is needed: a new login session picks up the group, and `udevadm trigger --subsystem-match=video4linux`
applies the rule.

## Evidence (`docs/evidence/virtual-devices/`)

Rig: dm313red, Ubuntu, kernel 7.0, PipeWire 1.6.2, v4l2loopback 0.15.3, Chromium (Playwright 1248), Microsoft Edge,
Firefox 1555. The camera picture is `ffmpeg testsrc2` at 1920x1080 YUYV 30 fps written into `/dev/video43` in the
engine's output format (the Cam Link had no HDMI signal and the Canon bridge was not started); the microphone is the
real voice chain on the Wave XLR, in a quiet room. Browsers ran as `mrh` with the `video` group added to that one
process (`sudo -u mrh -g video`), after checking nothing held the node.

| File | Shows |
|---|---|
| `zoom-web-device-list.png` | Zoom web client (zoom.us/test, pre-join page): "Select a Camera" lists Cam Link 4K and StreamStack Camera |
| `zoom-web-preview-and-mic.png` | the same page after selecting StreamStack Camera: live picture; "Select a Microphone" lists StreamStack Mic, ticked |
| `chromium-loopback.png`, `edge-loopback.png`, `firefox-loopback.png` | local WebRTC loopback page (`tests/fixtures/virtual_devices/loopback.html`): `enumerateDevices` lists both, `getUserMedia` opens 1280x720 at 30 fps with a real picture, the picture crosses an `RTCPeerConnection`, and the microphone level meter reads the live chain |
| `console-system-seen-by-apps.png` | the new System page card (demo rig, so Teams is simulated) |
| `doctor-as-login-user.txt`, `doctor-with-video-group.txt` | `streamstack doctor` before and after the group is present |

Edge is the browser the Teams PWA runs in, so its listing is the strongest Teams evidence available without an account.

Not proven: Teams itself (no signed-in session exists to use, and nothing was installed or signed in); a two-party
call in Teams or Zoom (the Zoom run stopped at the pre-join page and never entered the test meeting); a physical camera
through the engine; and a moving microphone meter under speech. The meter in the screenshots shows the room's noise
floor (about -70 dBFS, values changing frame to frame). The "seen by apps" camera side could not be shown with these
browsers, because a process started with `sudo -g` is non-dumpable and its `/proc/<pid>/fd` is hidden from the same
user; the mic side (PipeWire links) was verified live (`seen by apps: microphone: Chrome in a browser tab`), and the
camera side is covered by a fake-`/proc` unit test and by `doctor` naming a plain `ffmpeg` feeder.

## Automated tests, and what CI can prove

* `tests/test_virtualdevices.py`: configuration parsing, every doctor verdict, the mic report, PipeWire link parsing,
  `seen by apps` from a fake `/proc` and a fake `pw-dump`, and the shipped modprobe, modules-load and udev files.
* `tests/e2e/test_page_system.py`: the System page card, on the demo rig.
* `tests/test_virtual_devices_live.py`: feeds the real node, checks the formats with `v4l2-ctl`, and drives Chromium
  (`enumerateDevices`, `getUserMedia` 1280x720, a WebRTC loop, a non-flat picture; the mic too if the chain runs). It
  skips with its reason when the module is not loaded with the StreamStack label, when the user cannot open the node,
  when a program already holds it (a call), or when there is no Chromium. **CI runners cannot load v4l2loopback, so CI
  proves everything except this file; this file runs on the workstation** (it passed here, 2 of 2).

## Rejected

* **PipeWire-native camera node (a producer publishing `Video/Source`, no loopback).** Invisible to Zoom desktop and to
  Chromium without a flag; ffmpeg has no PipeWire video output and GStreamer's `pipewiresink` is not installed here
  (`docs/native-video-engine.md`). Revisit when Zoom reads PipeWire cameras; the engine would then gain a second sink.
* **libcamera or a kernel virtual camera (`vcam`, posted to linux-media in 2026).** Not merged; no client support.
* **Several advertised resolutions via more loopback nodes or `announce_all_caps`.** Doubles the nodes and the copy
  cost for no gain, since Chromium scales.
* **A PulseAudio null sink plus remap for the mic.** The filter-chain source is already a first-class source and keeps the
  graph in one process.
* **A seat-less ACL hack in StreamStack (chmod, setfacl from the app).** Access is a host policy; the product detects it
  and says what to change.

## Sources

* Teams PWA on Linux (Edge and Chrome): windowscentral.com and itsfoss.com, "Microsoft Teams PWA for Linux" and the
  retirement of the Electron app.
* PipeWire camera flag in Chromium (M127, off by default): chromium.googlesource.com, "Add about flag for enabling the
  PipeWire support in WebRTC"; Firefox pref `media.webrtc.camera.allow-pipewire`: jgrulich.cz, "Making PipeWire the default
  option for Firefox camera handling".
* Zoom Linux and virtual cameras (v4l2loopback in use, no PipeWire camera announcement found): community.zoom.com
  "IPU6 support on Linux"; PipeWire as recommended receive side: Ideas on Board, "PipeWire is the new v4l2loopback".
