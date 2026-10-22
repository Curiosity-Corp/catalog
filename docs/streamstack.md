# StreamStack: call and stream studio

StreamStack turns a desktop workstation into a repeatable studio for Microsoft
Teams, Zoom and OBS streams. It sets up a DSLR, an HDMI capture card and a
USB microphone the same way on every machine, and gives you one command to check
picture and sound before you go live.

```text
Canon EOS (USB live view) --streamstack-canon--> /dev/video42  "Canon EOS 50D"
Elgato Cam Link 4K (HDMI) ---------------------> /dev/streamstack/camlink
            both --> OBS (lighting, colour, LUT, scenes) --> /dev/video43 "StreamStack Camera"
Elgato Wave / any mic --> Easy Effects (voice chain) --> "Easy Effects Source"
```

In Teams and Zoom, pick **StreamStack Camera** and **Easy Effects Source**.

## Where it lives

The application (the `streamstack` command, the Console, the panel menu, auto mode, the
voice presets and OBS templates) is its own project, StreamStack, with its own
tests and releases. This role vendors a release tag at
[`roles/developer_workstation/files/streamstack`](../roles/developer_workstation/files/streamstack/README.md)
as a squashed `git subtree` and wires it into the host:

| The role does | The app does |
| --- | --- |
| apt packages, the v4l2loopback module and its two nodes, udev access, `/etc/streamstack/streamstack.env` from role variables, the install under `/usr/local`, user units, autostart entries, per-user seeding | everything a user runs: the Console, doctor, studio, previews, voice presets, auto mode, the panel menu |

To move to a newer StreamStack release:

```bash
git subtree pull --prefix roles/developer_workstation/files/streamstack \
  <streamstack repository> vX.Y.Z --squash
```

## Enable it

`deskmeet-b760` opts in through its hardware profile. Any other desktop opts in
with a host-local variable in `/etc/ansible/local-vars.yml`:

```yaml
streamstack_enabled: true
```

The next `ansible-pull` run (or `ansible-playbook --tags streamstack`)
installs:

| Piece | Purpose |
| --- | --- |
| OBS Studio | scenes, the Color Correction / Sharpen / LUT filters, the virtual camera, test recordings |
| Easy Effects + LSP and Calf plugins | the live voice chain: high-pass, noise removal, gate, compressor, bass/presence/treble EQ, de-esser, pitch, limiter |
| qpwgraph, pavucontrol | see and patch PipeWire routing; per-app levels |
| v4l-utils, qv4l2, guvcview | camera formats and UVC controls (exposure, white balance) |
| gphoto2, ffmpeg | Canon live view bridge, `ffplay` previews |
| StreamStack | `/usr/local/lib/streamstack`, with `streamstack`, `streamstack-auto`, `streamstack-console` and `streamstack-tray` in `/usr/local/bin` |

Two v4l2loopback devices are created with `exclusive_caps=1` (brought up
by `/usr/local/sbin/curiosity-streamstack-loopback`, at boot through
`curiosity-streamstack-loopback.service`); Chromium-based
browsers (Teams on the web, the Teams PWA) list a loopback camera only with
that flag and only while something is feeding it.

Role variables map onto the app's settings file: `streamstack_microphone` is
`STREAMSTACK_MICROPHONE`, `streamstack_auto_backend` is
`STREAMSTACK_AUTO_BACKEND`, and so on (see the app README's Configuration
table). Users can override any of them in
`~/.config/streamstack/streamstack.env`.

## Daily use

### The Console

**StreamStack** in the application menu (`streamstack-console`) opens the
Console: a visual control surface with the live picture, faders, meters, scene
and preset pads, and pages for Camera, Audio, Auto and System. Its **Compact
window** action (`streamstack-console --mini`) is a small strip to keep beside
a call. It opens as a Chrome or Edge app window, falling back to the default
browser, and listens only on `127.0.0.1` with a per-launch token. See the app
README for the pages and shortcuts.

### From the panel

The entry's **Panel menu** action (`streamstack-tray --menu`) opens the older
studio menu at the pointer: what calls receive right now, Start/Stop studio,
voice presets and effect bypass, mic check, previews of every live camera in
every mode it offers, shortcuts to OBS, Easy Effects, qpwgraph, pavucontrol and
qv4l2, the doctor and the Teams/Zoom settings.

For a panel button, show `streamstack status --icon` and open the Console on
click (the menu on a right click). It prints a camera icon (red dot: graded camera live, green: raw camera,
amber: needs attention, grey: off) and the details for the tooltip;
`streamstack status --short` gives the same as text. In tint2:

```ini
# add one more E to panel_items
execp = new
execp_command = streamstack status --icon
execp_interval = 3
execp_has_icon = 1
execp_icon_w = 22
execp_icon_h = 22
execp_lclick_command = streamstack-console
execp_rclick_command = streamstack-tray --menu
execp_mclick_command = streamstack preview canon
```

`streamstack_tray_autostart: true` also starts it as a system tray icon (red:
graded camera live, green: raw camera, amber: needs attention). It is off by
default because some tint2 builds crash when a GTK status icon docks.

### From a terminal

```bash
streamstack studio          # Easy Effects + Canon bridge + OBS with the virtual camera
streamstack doctor          # every device, its format, and what to pick in the call app
streamstack preview canon   # raw Canon feed (also: camlink, program); --grid for thirds
streamstack cameras         # every live camera and every mode it offers
streamstack preview /dev/video0 --mode '1920x1080@30 MJPG'
streamstack stop            # stop OBS (and the bridge): calls use the raw camera
streamstack mic-check --raw # hear the raw mic, then the processed voice
streamstack voice           # list voice presets; `streamstack voice "StreamStack Voice Warm"`
streamstack call-settings   # the Teams and Zoom toggles that match this rig
```

### Voice presets

All presets share one chain, so bass, treble and pitch are always one slider
away in Easy Effects (Equalizer: band 0 is bass, band 3 is treble; Pitch:
semitones).

| Preset | Character |
| --- | --- |
| StreamStack Voice | default broadcast voice |
| StreamStack Voice Warm | more low end for thin rooms or mics |
| StreamStack Voice Bright | more presence and air for dark mics |
| StreamStack Voice Deep | pitch down 2 semitones, extra body |
| StreamStack Voice Minimal | only high-pass, gentle compression and the limiter |

Set the preset every session starts with through `streamstack_voice_preset`.
Your own tweaks saved in Easy Effects are never overwritten by the role;
`streamstack setup --force` restores the shipped presets and OBS defaults.

### Lighting and colour

The OBS scene collection **StreamStack** has the Canon and Cam Link sources
with three filters each: **Lighting & Color** (Color Correction: exposure,
contrast, gamma, temperature/saturation), **Sharpen** (off) and **Cinematic
LUT** (off, teal/orange at 35%). Grade in OBS, not in the call app, so Teams
and Zoom both receive the same picture.

Get it right in camera first:

- Expose for the face: in live view keep skin just below clipping; lock
  exposure (M mode, fixed ISO) so the image does not breathe between shots.
- Set a fixed white balance (Kelvin) that matches your lights; auto white
  balance drifts as you move.
- Key light 30–45° off-axis and slightly above eye level, fill on the other
  side at about half power, and a separation light behind you.
- Frame on the thirds (`streamstack preview canon --grid`), eyes on the upper
  line, a little headroom.

## Auto mode: auto-cinematographer and auto-audio

Auto mode keeps the picture and the voice right while a call or stream runs.
It is off until each person turns it on, from the panel menu
(**Auto-cinematographer and auto-audio (Claude)**) or a terminal:

```bash
streamstack auto check      # OBS websocket, Claude and microphone ready?
streamstack auto on         # start it (the streamstack-auto user service)
streamstack auto once --dry-run   # what Claude would change, nothing applied
streamstack auto status     # on/off and the recent changes
streamstack auto reset      # undo its changes to the look, mic gain and voice preset
streamstack auto off
```

A local loop keeps exposure (metering the face once Claude has found it) and
microphone gain right every `streamstack_auto_interval` seconds; every
`streamstack_auto_ai_interval` seconds one still of the graded camera (never
audio) and the measured statistics go to Claude, whose bounded,
schema-validated proposal is ramped in. The default `claude-cli` backend runs
the signed-in Claude Code CLI restricted to reading that one still, so passes
use the person's Claude subscription; `api` uses `ANTHROPIC_API_KEY` from a
mode-600 `~/.config/streamstack/auto.env`; `local` never calls Claude.
`streamstack_auto_autostart: true` starts it at every login.

The limits, what leaves the host and the backends are documented in the app's
[docs/auto-mode.md](../roles/developer_workstation/files/streamstack/docs/auto-mode.md).

## Lights: camera follow and the daylight schedule

`streamstack-lights.service` (a user service in `/etc/systemd/user`) switches
the Litra lights on while any app uses the camera, off after a grace period,
and sets brightness and colour temperature by time of day. It starts at every
desktop login while `streamstack_lights_autostart` is true (the default); each
person tunes it on the Console's Lighting page, and only the person at the
active seat can reach the light. See the app's
[docs/lighting.md](../roles/developer_workstation/files/streamstack/docs/lighting.md).

The schedule follows the sun at the property. These desktops keep their clock
on UTC, so set where each property is per host (host_vars or a host-local
override), or the lights are scheduled for Greenwich:

```yaml
streamstack_property: "Main office"
streamstack_latitude: "40.7128"    # decimal degrees, north positive
streamstack_longitude: "-74.0060"  # decimal degrees, east positive
```

## Laptops: graded or raw

The graded **StreamStack Camera** runs OBS on top of the camera feed: it
composites on the GPU, then copies every frame back to the CPU for the virtual
camera, while the browser encodes the call. On a dual-core laptop that is
enough to make the fan, the call and the picture stutter under load.

- For everyday calls on a laptop, stop the studio and pick the raw camera
  (for example **Canon EOS 50D Webcam**). Grade in camera instead: Picture
  Style, Kelvin white balance and fixed exposure cost nothing.
- Keep the studio for calls that need scenes, a LUT or side-by-side.
- Match the OBS canvas to the camera instead of upscaling, for example
  `streamstack_width: 1280`, `streamstack_height: 720`, `streamstack_fps: 20`
  for a 720p20 PTP feed.
- Each stage is also one more thing that can stop mid-call: if OBS exits, the
  StreamStack Camera disappears from Teams. The raw camera has fewer moving
  parts.

### HDMI capture instead of USB live view

The Cam Link 4K delivers uncompressed frames from the camera's HDMI output, so
there is no decode or scale on the CPU (the USB live-view bridge decodes
MJPEG). The call app reads the Cam Link directly. The camera needs a clean
HDMI output (no overlays) and mains power for long calls. Through a USB 2 port
or hub the Cam Link cannot carry 1080p.

## Teams and Zoom

Start `streamstack studio` **before** joining, so the virtual camera is
already running when the browser enumerates devices.

**Teams**: Settings → Devices: camera StreamStack Camera, microphone Easy Effects
Source, speaker your headphones, noise suppression Low. Do not use Teams
background effects; the picture is already graded.

**Zoom**: Video: camera StreamStack Camera, HD on, Adjust for low light and Touch up
off. Audio: microphone Easy Effects Source, Automatically adjust microphone volume
off, background noise suppression Low (or Original sound for musicians).

Use headphones to stop echo, and make a test call (Teams "Make a test call",
zoom.us/test) before anything important.

## Canon bridge

`streamstack_canon_bridge: gphoto2` (default) streams live view through
`gphoto2 --capture-movie | ffmpeg`, cropped to 16:9 at the configured size and
frame rate. Set `streamstack_canon_bridge: command` and
`streamstack_canon_bridge_command` to use another tool (for example a PTP tool
that also controls exposure); it runs with `STREAMSTACK_CAMERA_DEVICE` set.
`streamstack_canon_bridge: none` is for HDMI-only cameras through the Cam
Link.

### A camera another tool already owns

Some hosts already have a service that owns the Canon (its own
`/etc/modprobe.d` v4l2loopback options and a PTP or HDMI bridge feeding the
node). Adopt that camera instead of replacing it:

```yaml
streamstack_enabled: true
streamstack_camera_external: true
streamstack_camera_video_nr: 10              # the node that tool feeds
streamstack_camera_label: Canon EOS 50D Webcam
```

StreamStack then writes no module options (and removes its own), never
reloads the module, runs no Canon bridge, and only adds its program node
(`video43`) next to the existing one at run time and at boot. OBS reads the
adopted node like any other camera.

The bridge is the `streamstack-canon` user service. `streamstack studio`
starts it; set `streamstack_canon_bridge_autostart: true` to start it at
login.

## AI helper dependencies

StreamStack's background blur, auto-framing and DeepFilterNet voice option need packages it never installs
itself. The role installs them as three optional groups; without them StreamStack
works as before and says what is missing (`streamstack doctor`, the Console
System page).

| Group | Variable (default) | Tag | What it installs |
|---|---|---|---|
| Background blur | `streamstack_background_removal_enabled` (`true`) | `streamstack-background-removal` | The OBS plugin [obs-backgroundremoval](https://github.com/royshil/obs-backgroundremoval) (GPL-3.0-or-later, a separate package that StreamStack only reaches through obs-websocket), the upstream release `.deb` pinned by version and sha256 (`streamstack_backgroundremoval_*`), plus `libonnxruntime1.23` and `libopencv-core410`/`-imgproc410` from the archive. It installs under `/usr/local/lib/obs-plugins` and `/usr/local/share/obs/obs-plugins`, which Ubuntu's OBS does not search, so the role links the module into `/usr/lib/x86_64-linux-gnu/obs-plugins` and its data into `/usr/share/obs/obs-plugins/obs-backgroundremoval`. Skipped when the pinned version or a newer one is already installed. |
| Auto-framing | `streamstack_framing_enabled` (`true`) | `streamstack-framing` | `python3-numpy`, `python3-opencv`, `python3-packaging`, `python3-pip`; the OpenVINO runtime and `openvino-telemetry` from pinned, checksummed wheels installed with `pip --target /usr/local/lib/streamstack` (the one directory the isolated `python3 -I` launcher adds; Ubuntu has no OpenVINO package and PEP 668 blocks system pip); and, per user in `dev_users`, the YuNet face model in `~/.local/share/streamstack/models/` (commit-pinned URL, sha256 checked, the same file `streamstack framing --fetch` verifies). |
| Noise remover | `streamstack_deepfilter_enabled` (`true`) | `streamstack-deepfilter` | `libdeep_filter_ladspa.so` from [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) (MIT or Apache-2.0), the upstream x86_64 release asset pinned by version and sha256 (`streamstack_deepfilter_*`), installed to `/usr/local/lib/ladspa/`, one of the directories Easy Effects 8.1.2's Deep Noise Remover searches; Ubuntu does not ship it. The release is recorded in `/usr/local/share/streamstack/libdeep_filter_ladspa.release`; a newer or unrecorded library is left alone. Easy Effects is not restarted: it looks for the library when the effect loads. |

The Intel GPU compute runtime (`intel-opencl-icd`, `libze-intel-gpu1`,
`libze1`, `libigdgmm12`, `ocl-icd-libopencl1`) is installed, and the desktop
users join the `render` group, only when an Intel GPU is present
(`/sys/class/drm/card*/device/vendor` is `0x8086`). Other hosts run face
detection on the CPU (2 to 5 ms per frame); OpenVINO picks `GPU.0` itself where
it exists. The OpenVINO wheel matches the system Python (`cp314`, x86_64); on
another interpreter the role prints a notice and skips it, and StreamStack
falls back to OpenCV's DNN on the CPU until `streamstack_openvino_*` is bumped.

Nothing is restarted: OBS loads the plugin at its next start, and a new `render`
group applies at the next login. To check a host (read-only):

```
dpkg -s obs-backgroundremoval | grep Version
cat /usr/local/share/streamstack/libdeep_filter_ladspa.release; nm -D /usr/local/lib/ladspa/libdeep_filter_ladspa.so | grep ladspa_descriptor
/usr/bin/python3 -I -c "import sys; sys.path.insert(0,'/usr/local/lib/streamstack'); import openvino, numpy, cv2; print(openvino.Core().available_devices)"
streamstack doctor
```

To bump a pinned artifact, change its version, URL and sha256 together
(`streamstack_backgroundremoval_*`, `streamstack_openvino_*`, `streamstack_deepfilter_*`); the role
re-downloads, verifies the checksum, and replaces the old version.

## Low latency

`streamstack_low_latency` (default `true` on StreamStack hosts) applies
`tasks/streamstack-low-latency.yml`. Every rule matches by USB id, PipeWire
node name or detected GPU, so a property with a Focusrite Scarlett 2i2 instead
of the Wave XLR, or without the Arc card, gets only what applies and nothing
fails when a device is absent. Nothing is restarted by the role: PipeWire and
WirePlumber pick the files up the next time they start (log out and in, or
`systemctl --user restart pipewire wireplumber pipewire-pulse` between calls).

Apply only this part: `ansible-playbook playbooks/site.yml --tags streamstack-low-latency`
(with the host's usual `-e workstation_profile=desktop -e hardware_profile=...`).

### Audio clock and interface

| Setting | Value | Why |
| --- | --- | --- |
| `default.clock.rate` / `allowed-rates` | 48000 | every StreamStack device runs 48 kHz natively, so there is never a resample |
| `default.clock.quantum` | 256 (5.3 ms, was 1024 = 21 ms) | USB audio moves in 1 ms (full-speed) or 125 us (high-speed) packets and the two-period ALSA buffer needs a few packets of slack to stay free of xruns; Easy Effects' RNNoise works on 10 ms (480 sample) frames and buffers internally below that size whatever the quantum |
| `default.clock.min-quantum` | 128 (2.7 ms) | a client may ask for it with `PIPEWIRE_QUANTUM=128/48000` |
| `default.clock.max-quantum` | 1024 (was 2048) | one greedy client can no longer drag the graph back to 43 ms |
| `pulse.default.req` / `pulse.min.req` | 256/48000, 128/48000 | the browser, Teams and Zoom speak the PulseAudio protocol |
| `api.alsa.period-size` / `api.alsa.headroom` | 128 / 64 frames | pinned on the audio interface only (Focusrite and Elgato Wave XLR nodes) |
| `session.suspend-timeout-seconds`, `node.pause-on-idle` | 0, false | the interface never suspends when idle, so a call never starts with a wake-up dropout |

Files: `/etc/pipewire/pipewire.conf.d/50-curiosity-streamstack-low-latency.conf`,
`/etc/pipewire/pipewire-pulse.conf.d/50-...conf` and
`/etc/wireplumber/wireplumber.conf.d/50-curiosity-streamstack-alsa.conf`.
Override with `streamstack_audio_rate`, `_quantum`, `_min_quantum`,
`_max_quantum`, `_period_size`, `_headroom` and `_node_patterns` (anchored
regexes on the node name), or with a later-sorting drop-in of your own. If a
property hears crackles, raise `streamstack_audio_headroom` first, then the
quantum; verify with `pw-top` (ERR column) or `pw-metadata -n settings`.

### Realtime

`rtkit` is installed and the `audio` group gets `rtprio 95`, `memlock
unlimited` and `nice -19` (`/etc/security/limits.d/95-curiosity-streamstack-realtime.conf`).
The `pipewire` package already ships the equivalent for the `pipewire` group.
Managed users (`dev_users`) that exist on the host join `audio`; the role never
creates an account. Group membership and limits apply at the next login.

### USB power

`/etc/udev/rules.d/60-curiosity-streamstack-usb-power.rules` writes
`power/control=on` (no autosuspend) for Focusrite `1235:*`, Elgato Wave XLR
`0fd9:007d`, Cam Link 4K `0fd9:0066` and Canon `04a9:*`, and the role writes
the same value to devices that are plugged in now. It replays no udev events,
so nothing rebinds or re-enumerates. Edit `streamstack_usb_no_autosuspend_ids`
to change the list.

### Intel Arc media stack

On a host with an Intel GPU the role keeps these current from the Ubuntu
archive (`streamstack_media_package_state: latest`; `present` stops upgrades):
`linux-firmware` (GuC/HuC), `intel-media-va-driver` (iHD), `libvpl2`,
`libmfx-gen1.2` (the oneVPL GPU runtime), `vainfo`, `intel-gpu-tools`,
`intel-opencl-icd` and `libze-intel-gpu1`. Set `streamstack_intel_media_non_free: true`
for `intel-media-va-driver-non-free` (HEVC encode, VC-1 decode; it replaces the
free build). Check with `vainfo` and `clinfo`. i915 stays the driver:
the `xe` module is installed on Ubuntu 26.04 but the Arc A380 is bound to
i915, and the role never switches, blacklists or force-probes either one
(switching also drops the patched-i915 PCON work). Intel ships newer media
and compute releases upstream than Ubuntu packages; the archive build is what
the role installs, and a newer one arrives with the next Ubuntu update.

### Scarlett, firmware and v4l2loopback

The Scarlett 2i2 mixer driver (`scarlett2`) is part of the kernel's
`snd-usb-audio`; the module exposes no Scarlett-specific option, so none is
set. `alsa-scarlett-gui` is installed by the hardware profile. Interface
firmware is deliberately not flashed by the role: use the *Firmware* section
of `alsa-scarlett-gui` (needs the Scarlett firmware files, which Ubuntu's
`linux-firmware` does not carry) at a time no call is planned.
v4l2loopback options (`devices=2 video_nr=42,43 exclusive_caps=1,1`) are owned
by `streamstack.yml` and untouched here; Ubuntu ships 0.15.3 (upstream tags
0.15.4).

### CPU and kernel

The desktop `performance_mode` hardware-profile flag already selects
power-profiles-daemon's `performance` profile (reversible with
`powerprofilesctl set balanced`); nothing more is added. The stock kernel is
`PREEMPT_DYNAMIC` with `PREEMPT_LAZY`, `HZ=1000` and `NO_HZ_FULL`, which is
already low latency. `streamstack_kernel_args: [threadirqs, preempt=full]`
opts in to extra arguments through
`/etc/default/grub.d/93-curiosity-streamstack.cfg` (appended to the existing
line, `update-grub` run, active after a reboot); an empty list removes the file.

Sources: PipeWire documentation (`pipewire.conf(5)`, `pipewire-pulse.conf(5)`,
`pipewire-props(7)`, the Config-ALSA and Performance-tuning wiki pages);
WirePlumber 0.5 `monitor.alsa.rules`; Intel `media-driver` (intel-media-26.3.5),
`compute-runtime` (26.35.39758.10) and `libvpl` (v2.17.0) releases; Geoffrey
Bennett's `alsa-scarlett-gui` (1.0beta9) and scarlett2 driver; the
v4l2loopback releases; Ubuntu 26.04 package archive.

## Notes

- If `streamstack doctor` reports a loopback device with the wrong name,
  another `/etc/modprobe.d/*.conf` sets v4l2loopback options (the role warns
  about these). Remove that file and its service, then reboot.
- The OBS virtual camera feeds the **first free** loopback node, not a named
  one. If the camera node has no producer yet, OBS can take it and your call
  apps see the wrong picture. `streamstack studio` therefore waits for the
  camera to be live and otherwise starts OBS without the virtual camera;
  `streamstack doctor` warns when OBS holds a loopback node other than the
  program camera.
- An open camera keeps the module loaded, so a changed device layout applies
  at the next reboot rather than in the middle of a call.
- Plug the Cam Link 4K straight into a USB 3 port; through a USB 2 hub it
  cannot carry 1080p.
