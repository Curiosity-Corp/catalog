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

The application (the `streamstack` command, the panel menu, auto mode, the
voice presets and OBS templates) is its own project, StreamStack, with its own
tests and releases. This role vendors a release tag at
[`roles/developer_workstation/files/streamstack`](../roles/developer_workstation/files/streamstack/README.md)
as a squashed `git subtree` and wires it into the host:

| The role does | The app does |
| --- | --- |
| apt packages, the v4l2loopback module and its two nodes, udev access, `/etc/streamstack/streamstack.env` from role variables, the install under `/usr/local`, user units, autostart entries, per-user seeding | everything a user runs: doctor, studio, previews, voice presets, auto mode, the panel menu |

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
| StreamStack | `/usr/local/lib/streamstack`, with `streamstack`, `streamstack-auto` and `streamstack-tray` in `/usr/local/bin` |

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

### From the panel

**StreamStack** in the application menu (`streamstack-tray --menu`) opens the
studio menu at the pointer: what calls receive right now, Start/Stop studio,
voice presets and effect bypass, mic check, previews of every live camera in
every mode it offers, shortcuts to OBS, Easy Effects, qpwgraph, pavucontrol and
qv4l2, the doctor and the Teams/Zoom settings.

For a panel button, show `streamstack status --icon` and open the menu on
click. It prints a camera icon (red dot: graded camera live, green: raw camera,
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
execp_lclick_command = streamstack-tray --menu
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
