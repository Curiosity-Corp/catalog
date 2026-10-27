# StreamStack

StreamStack turns a Linux desktop into a repeatable call and stream studio for
Microsoft Teams and Zoom: a DSLR over USB live view or HDMI capture, a USB
microphone with a broadcast voice chain, one command to check picture and
sound before you go live, a panel menu, and an optional **auto-cinematographer
and auto-audio engineer** that uses Claude to keep the picture and voice right
during the call.

```text
Canon EOS (USB live view) --streamstack canon-bridge--> /dev/video42  "Canon EOS 50D"
Elgato Cam Link 4K (HDMI) ----------------------------> /dev/streamstack/camlink
            both --> StreamStack video engine (colour, LUT, crop) --> /dev/video43 "StreamStack Camera"
USB microphone --> StreamStack voice chain (PipeWire filter-chain) --> "StreamStack Mic"
```

In Teams and Zoom, pick **StreamStack Camera** and **StreamStack Mic**.

## Install

StreamStack is a small Python package (standard library only) that drives
system tools. Install those from your distribution (Ubuntu/Debian names):

```bash
sudo apt install zam-plugins qpwgraph pavucontrol \
  v4l2loopback-dkms v4l2loopback-utils v4l-utils qv4l2 gphoto2 ffmpeg pulseaudio-utils alsa-utils \
  python3-gi gir1.2-ayatanaappindicator3-0.1 libnotify-bin
```

Then install StreamStack itself, either self-contained under `/usr/local`
(no pip, nothing in the system Python):

```bash
sudo make install            # PREFIX=/usr/local; DESTDIR= for packaging
streamstack setup            # as each user: voice presets
```

or with pip/pipx (`pipx install .`).

Create the two loopback cameras. Chromium-based browsers (Teams on the web,
the Teams PWA) list a loopback camera only with `exclusive_caps=1`, and only
while something feeds it:

```bash
# /etc/modprobe.d/streamstack.conf
options v4l2loopback devices=2 video_nr=42,43 card_label="Canon EOS 50D,StreamStack Camera" exclusive_caps=1,1 max_buffers=2
# /etc/modules-load.d/streamstack.conf
v4l2loopback
```

Host-wide settings go in `/etc/streamstack/streamstack.env`, per-user
overrides in `~/.config/streamstack/streamstack.env` (see
[Configuration](#configuration)). Configuration management tools can manage
the first and leave the second to users.

## Daily use

### The Console

The Console is the place to go mid-call. It is a visual control surface with
faders, meters, pads and the live picture, not a menu. Open it from
**StreamStack** in the application menu, a left click on the tray icon, or a
terminal:

```bash
streamstack console          # the full Console in its own window
streamstack console --mini   # a compact strip to keep beside the call
streamstack console --demo   # try every control without a camera or microphone
```

| Page | What it is for |
| --- | --- |
| **Live** | The one screen for a call in progress. Program picture with an on-air tally, mic fader and live meter, big MUTE and PRIVACY pads, scene pads, voice presets, and the auto director switch. |
| **Camera** | Picture grade: brightness, contrast, gamma, saturation and a warmth slider, sharpen and LUT, histogram and RGB parade or waveform scopes, thirds/safe/centre/zebra overlays, saved looks, the program camera on/off, record and stream. |
| **Audio** | A channel strip: raw mic and processed voice meters side by side, the gain fader and a big mute, tone knobs (bass, presence, treble), the voice chain with a live fader for every stage (high-pass, gate, noise removal, de-esser, EQ, compressor, gain, limiter), voice presets with A/B bypass, a loudness leveller, a 60-second loudness history, and a mic check that records raw then processed audio for you to compare. |
| **Auto** | The Claude director: on/off, ask it now, persona cards, the contact sheet Claude saw, a voice read-out, the time-of-day strip, a timeline of every change with its reason, and one-press undo. |
| **System** | Health checks with fixes, running services, shortcuts to qpwgraph, pavucontrol and qv4l2, and the Teams and Zoom settings for this rig. |
| **Lighting** | Logitech Litra Glow, Beam and Beam LX lights as big pads and faders: power, brightness and a warm-to-cool colour-temperature fader per light, an All lights master, presets (Key light, Soft, Warm evening, Daylight, Off) and your own, and Identify to find which light is which. Needs a one-time udev rule, see [docs/lighting.md](docs/lighting.md). |
| **Interface** | The audio interface on the desk, found by USB id. A Focusrite Scarlett 2i2 shows its gain, Autogain, Air, Line/Inst, Safe and direct monitor, and 48V behind a press-and-hold; an Elgato Wave XLR shows gain, mute, 48V and headphone volume once hardware control is enabled. See [docs/interfaces.md](docs/interfaces.md). The Audio page's software gain stays as the fallback. |
| **Deck** | An Elgato Stream Deck as a control surface (any model, default 4x4 layout): scenes, mute, privacy, voice and light presets, record, stream and every other option on keys that show live state. The page shows the keys live and lets you drag options onto them. Needs the `deck` extra and `streamstack deck enable`, see [docs/deck.md](docs/deck.md). |
| **Mini** | MUTE, PRIVACY, the meter and the scenes in a window small enough to sit beside the call. |

Everything works from the keyboard. `M` mutes, `P` turns on privacy, `B`
bypasses the voice effects, `L` turns every light on or off, `1`…`9` switch scenes, `Alt`+a number changes
page, `Ctrl K` opens the command palette and `?` lists every shortcut. If the
mic watchdog sees a stalled microphone (its ALSA capture counters stop
moving) or delivers digital silence (they move, but every sample is exactly zero),
a banner with **Revive mic** appears and `Shift R` restarts it (for digital silence it
first re-writes the interface's capture switch and volume, which needs `alsa-utils`). It
never does this on its own.

The Console listens on `127.0.0.1` only, with a fresh token each launch. It
opens as a Chrome or Edge app window, or in the default browser when neither
is installed. [docs/console.md](docs/console.md) describes its architecture,
state and actions.

### From the panel

**StreamStack** in the application menu opens the Console (`streamstack
console`, or `streamstack-console`; `--mini` for the compact window beside a
call, `--demo` to try it without a rig). The tray icon opens it on a left
click, and its first menu item does too. The older panel menu lives on as
`streamstack-tray --menu` (also an action of the application-menu entry) and
opens the studio menu at the pointer: what calls receive right now, Start/Stop studio,
auto mode, voice presets and effect bypass, mic check, previews of every live
camera in every mode it offers, shortcuts to qpwgraph,
pavucontrol and qv4l2, the doctor and the Teams/Zoom settings.

For a panel button, show `streamstack status --icon` and open the menu on
click. It prints a camera icon (red dot: graded camera live, green: raw camera,
amber: needs attention, grey: off) and writes the details to stderr for the
tooltip; `streamstack status --short` gives the same as text. In tint2:

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

`streamstack-tray` without `--menu` is a system tray icon (Ayatana
AppIndicator, falling back to an XEmbed status icon); some tint2 builds crash
when a GTK status icon docks, so the menu button is the safer default.

### From a terminal

```bash
streamstack studio          # voice chain + Canon bridge (the Console runs the program camera)
streamstack chain status    # the voice chain: running, preset, denoise engine, loudness leveller
streamstack doctor          # every device, its format, and what to pick in the call app
streamstack preview canon   # raw Canon feed (also: camlink, program); --grid for thirds
streamstack cameras         # every live camera and every mode it offers
streamstack preview /dev/video0 --mode '1920x1080@30 MJPG'
streamstack stop            # stop the Canon bridge: calls use the raw camera
streamstack mic-revive      # restart a stalled or digitally silent microphone (refuses a healthy one; --reprobe restarts WirePlumber last)
streamstack default-source  # make call apps' default microphone the voice chain (or the microphone)
streamstack mic-check --raw # hear the raw mic, then the processed voice
streamstack voice           # list voice presets; `streamstack voice "StreamStack Voice Warm"`
streamstack auto on|off     # the Claude auto-cinematographer and auto-audio
streamstack call-settings   # the Teams and Zoom toggles that match this rig
streamstack framing         # auto-framing: backends, device, face model; --fetch downloads it, --probe times it
```

### Background blur

The Console's Camera page and the Live page's "Blur" pad drive background blur, which is a segmentation mask
composited inside the native video engine (an OpenVINO model on the Arc GPU). The mask is not in the engine yet,
so on a real rig the controls explain that and the action answers 409; the demo rig simulates the whole feature.
A cost gate (8 ms per frame, 1 percent CPU) is decided in advance for when it lands. See docs/console.md.

### Video encoder

Recording and streaming run on the GPU through the engine's encode tap (hardware AV1 first), see
docs/stream.md and docs/adr/0001-encoder-generations-and-stream-transports.md.

### Auto-framing

An optional face detector keeps you framed by moving a smooth crop in the video engine (no extra video latency). It runs on
the Intel GPU through OpenVINO when there is one, else on the CPU. Install with
`pip install 'streamstack[framing]'` and run `streamstack framing --fetch` once (YuNet, MIT licence, checksum
verified). Pick Off, Gentle, Tight or Follow on the Console's Camera page. Details: docs/auto-framing.md.

### Voice presets

All presets share one StreamStack-owned chain (high-pass, noise gate, neural noise removal, de-esser, EQ,
compressor, makeup gain, limiter with a -1 dBTP true-peak ceiling) that StreamStack generates and runs as a PipeWire
filter-chain, so every stage is one fader away on the Console's Audio page (Chain tab). The chain exposes one
microphone, **StreamStack Mic** (48 kHz), that Teams, Zoom and the stream read. Design, plugins and measurements:
[docs/audio-chain.md](docs/audio-chain.md).

| Preset | Character |
| --- | --- |
| StreamStack Voice | default broadcast voice |
| StreamStack Voice Warm | more low end for thin rooms or mics |
| StreamStack Voice Bright | more presence and air for dark mics |
| StreamStack Voice Deep | extra body (bass shelf) |
| StreamStack Voice Minimal | only high-pass, gentle compression and the limiter |
| StreamStack Voice DeepFilterNet | the default voice with DeepFilterNet noise removal (about 30 ms more delay); needs the DeepFilterNet LADSPA plugin, see docs/voice-denoise.md |

A one-time import turns presets you made in Easy Effects (`~/.local/share/easyeffects/input`) into StreamStack
presets on the first start; Easy Effects itself is no longer used or needed.

`streamstack setup` never overwrites your own changes; `streamstack setup
--force` restores the shipped presets.

### Lighting and colour

The video engine grades the camera on the GPU: brightness, contrast, gamma, saturation, warmth, Sharpen and an
optional LUT, set from the Camera page, a saved look or the AI director, and applied between two frames. Grade
here, not in the call app, so Teams and Zoom both receive the same picture.

Get it right in camera first:

- Expose for the face and lock exposure (M mode, fixed ISO) so the image does
  not breathe.
- Set a fixed white balance (Kelvin) that matches your lights.
- Key light 30–45° off-axis and slightly above eye level, fill on the other
  side at about half power, and a separation light behind you.
- Frame on the thirds (`streamstack preview canon --grid`), eyes on the upper
  line, a little headroom.

## Auto mode

`streamstack auto on` (or the panel menu) runs a local auto-exposure and
mic-gain loop every 15 s, and asks Claude every few minutes to review one still
of the graded picture and propose bounded changes to contrast, saturation,
white balance, the LUT, sharpening, the voice preset and where the face is for
spot metering. Every change is clamped, ramped, logged and undoable with
`streamstack auto reset`. It runs on your Claude subscription through the
signed-in Claude Code CLI, restricted to reading that one still, or on the
Anthropic API. See [docs/auto-mode.md](docs/auto-mode.md) for what it sends,
the limits, and the backends.

## Laptops: graded or raw

The graded camera is made by the Console's video engine: it decodes, grades and crops on the GPU and feeds the
virtual camera, while the browser encodes the call. If a call stutters on a small laptop, compare with the raw
camera.

- For everyday calls on a laptop, `streamstack stop` and pick the raw camera.
  Grade in camera instead: Picture Style, Kelvin white balance and fixed
  exposure cost nothing.
- Keep the engine for calls that need a grade, a LUT or framing.
- Match the output to the camera instead of upscaling, for example
  `STREAMSTACK_ENGINE_SIZE=1280x720` for a 720p20 PTP feed.

The Cam Link 4K delivers uncompressed frames from the camera's HDMI output, so
there is no decode on the CPU (USB live view is MJPEG). It needs a clean HDMI
output, mains power for long calls, and a USB 3 port (not a USB 2 hub).

The Cam Link has no modes of its own: it offers whatever the camera sends over
HDMI. StreamStack reads that list (found by USB id `0fd9:0066`, never by
`/dev/videoN`) and picks the best mode: 1080p60 over 1080p50 over 1080p30, raw
YUYV/NV12 before MJPEG (no decode, least latency), and 4K only with
`STREAMSTACK_CAMLINK_4K=true`. Set the camera's HDMI output to 1080p60 (or 50)
and turn off its on-screen display. `streamstack camlink` shows the offer and
the pick, and the video engine opens the card in that mode by itself. The Console re-reads the offer every few
seconds, so a camera switched on or to another mode appears on the Camera page
(HDMI chip on the Cam Link picture) and in the System health checks. The card's
HDMI audio is the camera's sound: it is listed as such and never offered as the
call microphone.

## Teams and Zoom

Start the Console and `streamstack studio` **before** joining, so the virtual camera is already
running when the browser enumerates devices. `streamstack call-settings`
prints the exact toggles; in short: camera StreamStack Camera, microphone
StreamStack Mic, headphones, noise suppression Low, no background effects
or touch-up, Zoom's automatic mic volume off. Make a test call first.

## Canon bridge

`STREAMSTACK_CANON_BRIDGE=gphoto2` (default) is the `streamstack-canon` user
service. It owns the camera: live-view JPEGs go to ffmpeg, which decodes them
on the GPU and writes the loopback camera. `command` runs
`STREAMSTACK_CANON_COMMAND` instead (with `STREAMSTACK_CAMERA_DEVICE` set);
`none` is for HDMI-only cameras through the Cam Link.

**Decode path.** `STREAMSTACK_CANON_DECODE=auto` tries Intel Quick Sync, then
VA-API, then the CPU, and the old command line as a last resort. Each path is
first run once on a real camera frame into a null sink, so a missing driver or
a GPU that cannot decode this stream falls through at start, and a path that
crashes or stalls while running is dropped until the next restart. Force one
with `qsv`, `vaapi` or `cpu` (a forced GPU path still falls back to the CPU).
The render node comes from the hardware profile's decode role (the iGPU on
deskmeet1, the Arc on dm313red); set
`STREAMSTACK_CANON_RENDER_NODE=/dev/dri/renderD129` to choose. The System page
shows the path in use, picture size in and out, frames per second, CPU use of
the bridge and ffmpeg, and every restart; `streamstack canon status` prints the
same.

**Picture size.** `STREAMSTACK_CANON_OUTPUT=native` (default) keeps the
camera's own aspect ratio and never enlarges: a 1056x704 live view is written
as 1056x704, and only a bigger frame is shrunk to fit the canvas. `fill` is the
0.2 behaviour: scale up and crop to the 16:9 canvas. The loopback format changes
at the next bridge restart, so restart it between calls (`systemctl --user
restart streamstack-canon.service`). Canon live view over USB is at most about
1056x704; for a sharper picture use the HDMI output through the Cam Link.

**Camera settings.** The Camera page's *Canon body* tab and `streamstack canon`
set ISO, shutter, aperture, exposure compensation, white balance (and Kelvin),
Picture Style, focus mode and live-view size, run autofocus and nudge focus,
and read every value back from the camera. The settings use libgphoto2's own
configuration names (`iso`, `shutterspeed`, `aperture`, `exposurecompensation`,
`whitebalance`, `colortemperature`, `picturestyle`, `focusmode`,
`liveviewsize`, `autofocusdrive`, `manualfocusdrive`), and the choices come
from the camera, so any Canon that exposes them works. A setting the body
refuses (aperture in Tv mode, say) is shown read-only with the reason.

With the python `gphoto2` package installed (`pip install gphoto2`, extra
`canon`), the bridge uses one libgphoto2 session for live view and settings, so
changes are instant and the picture never stops. Without it the bridge uses the
`gphoto2` command, which cannot share the camera: each change pauses live view
for about a second (the last frame stays on the loopback camera) and then
resumes. `STREAMSTACK_CANON_BACKEND=libgphoto2|cli` forces one. Autofocus in
live view can blank the picture briefly on some bodies, and live-view autofocus
needs a lens in AF mode.

The bridge restarts a camera that stops sending frames (4 s) or an ffmpeg that
stops writing (3 s) on its own; if the camera library itself hangs, the bridge
exits so systemd starts a fresh one.

When another tool on the host already owns the Canon's loopback node, set
`STREAMSTACK_CAMERA_EXTERNAL=true` and point `STREAMSTACK_CAMERA_DEVICE` and
`STREAMSTACK_CAMERA_LABEL` at it; StreamStack then never runs its own bridge.

## Configuration

`KEY=value` lines, shell quoting allowed. Later files win: built-in defaults,
then `/etc/streamstack/streamstack.env` (or `$STREAMSTACK_ENV_FILE`), then
`~/.config/streamstack/streamstack.env`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `STREAMSTACK_CAMERA_DEVICE` / `_LABEL` | `/dev/video42`, `Canon EOS 50D` | the camera loopback node |
| `STREAMSTACK_PROGRAM_DEVICE` / `_LABEL` | `/dev/video43`, `StreamStack Camera` | the program camera node the engine writes |
| `STREAMSTACK_WIDTH`, `_HEIGHT`, `_FPS` | `1920`, `1080`, `30` | Canon bridge output, and the stream size when `STREAMSTACK_CAMLINK_MODE=off` |
| `STREAMSTACK_CAMLINK_MODE` | `auto` | `auto`: the best mode the HDMI source offers, and the program camera follows it; `off`: keep the three settings above; `1920x1080@60 YUYV`: pin one mode (falls back to `auto` when the source does not offer it) |
| `STREAMSTACK_CAMLINK_4K` | `false` | let `auto` pick 4K |
| `STREAMSTACK_CAMERA_EXTERNAL` | `false` | another tool feeds the camera node |
| `STREAMSTACK_CAMERA_LOSS` | `slate` | when the camera loses its picture: `slate` keeps StreamStack Camera fed with a "camera reconnecting" card, `hold` shows the last frame darkened, `off` writes nothing; see [docs/camera-delivery.md](docs/camera-delivery.md) |
| `STREAMSTACK_CAMERA_FALLBACK` | `auto` | `auto` moves the program camera to another camera that delivers (the Cam Link) after `_AFTER` seconds (6) without a picture, and back after `_BACK` seconds (5) of a steady one; `off` keeps the picked camera |
| `STREAMSTACK_ENGINE_IDLE` | `auto` | `auto` rests the engine, the GPU and the Canon live view while no app reads StreamStack Camera (the camera stays listed); `off` never idles; see [docs/camera-delivery.md](docs/camera-delivery.md) |
| `STREAMSTACK_ENGINE_IDLE_AFTER` | `10` | seconds without a reader before the engine rests |
| `STREAMSTACK_ENGINE_IDLE_IGNORE` | | comma-separated process names that do not count as readers |
| `STREAMSTACK_ENGINE_SIZE` | `source` | the program camera's size: the camera's own, or `WIDTHxHEIGHT` |
| `STREAMSTACK_ENGINE_BACKEND` | `auto` | `auto` (the GPU path when it works, else the CPU), `vulkan` or `cpu` |
| `STREAMSTACK_ENGINE_RENDER_NODE` | | GPU render node for the engine (grade, blur); empty lets the hardware profile choose by PCI id |
| `STREAMSTACK_ENGINE_LUT` | | a `.cube` 3D LUT applied before the look (path without spaces) |
| `STREAMSTACK_ENGINE_BLUR` | `auto` | background blur on the Arc through OpenVINO (`pip install 'streamstack[blur]'`); `off` removes it from the graph |
| `STREAMSTACK_ENGINE_BLUR_DEVICE` | `auto` | the OpenVINO device for the blur: `auto` picks the discrete Arc, or an id such as `GPU.1` |
| `STREAMSTACK_CANON_BRIDGE` | `gphoto2` | `gphoto2`, `command` or `none` |
| `STREAMSTACK_CANON_COMMAND` | | the bridge command for `command` |
| `STREAMSTACK_CANON_OUTPUT` | `native` | `native` (camera aspect, never enlarged) or `fill` (crop to the canvas) |
| `STREAMSTACK_CANON_DECODE` | `auto` | `auto` (Quick Sync, VA-API, CPU), or force `qsv`, `vaapi`, `cpu` |
| `STREAMSTACK_CANON_BACKEND` | `auto` | `auto`, `libgphoto2` (shared session) or `cli` (pauses for changes) |
| `STREAMSTACK_CANON_RENDER_NODE` | | GPU render node for the Canon decode, e.g. `/dev/dri/renderD129`; empty lets the hardware profile choose |
| `STREAMSTACK_STREAM_RENDER_NODE` | | GPU render node for the stream encoder; empty lets the hardware profile choose (the Arc) |
| `STREAMSTACK_VOICE_PRESET` | `StreamStack Voice` | preset `streamstack studio` loads |
| `STREAMSTACK_MICROPHONE` | | part of the call mic's name; empty picks an Elgato Wave, then the default input |
| `STREAMSTACK_RIG` | `auto` | the hardware rig profile: `auto` (the devices found decide), `studio-wave-xlr`, `studio-deck`; see [docs/hardware-profiles.md](docs/hardware-profiles.md) |
| `STREAMSTACK_INTERFACE` | `auto` | the audio interface to drive: `auto` (by USB id), `off`, a profile (`scarlett-2i2-gen4`, `scarlett-2i2-gen3`, `wave-xlr`) or a USB id |
| `STREAMSTACK_WAVE_XLR_USB` | `off` | Wave XLR hardware control: `off` (never opened), `read` or `write`; see [docs/interfaces.md](docs/interfaces.md) |
| `STREAMSTACK_AUTO_BACKEND` | `claude-cli` | `claude-cli`, `api` or `local` |
| `STREAMSTACK_AUTO_MODEL` | | model for Claude passes (empty: the CLI default, `claude-sonnet-5-5` on the API) |
| `STREAMSTACK_AUTO_INTERVAL` | `15` | seconds between local exposure/gain passes |
| `STREAMSTACK_AUTO_AI_INTERVAL` | `300` | seconds between Claude passes (backs off to 4x when nothing changes) |
| `STREAMSTACK_AUTO_VIDEO`, `_AUDIO`, `_NOTIFY` | `true` | turn parts of auto mode off |
| `STREAMSTACK_AUTO_BURST`, `_BURST_SECONDS` | `4`, `1.0` | stills per review (joined into one contact sheet sent to Claude) and their spacing |
| `STREAMSTACK_AUTO_CLIP_SECONDS` | `6` | voice clip analysed locally into numbers (the audio never leaves the computer) |
| `STREAMSTACK_AUTO_PERSONA_LOOK` | `true` | the chosen persona sets the exposure bands the local loop meters towards |
| `STREAMSTACK_PROPERTY`, `_LATITUDE`, `_LONGITUDE` | | where sunrise and sunset are computed (empty: the time zone's city) |

## Troubleshooting

- `streamstack doctor` reports a loopback device with the wrong name: another
  `/etc/modprobe.d/*.conf` sets v4l2loopback options. Remove it and reboot.
- The program camera writes the node named by `STREAMSTACK_PROGRAM_DEVICE`. If `doctor` says it has no producer,
  open the Console and turn the program camera on.
- An open camera keeps the module loaded, so a changed device layout applies
  at the next reboot rather than mid-call.

## Development

```bash
make test        # pytest (PYTHONPATH=src)
streamstack console --demo --no-window   # the Console against a simulated rig
make lint        # ruff check + ruff format --check
make build       # sdist and wheel
make e2e         # Console browser tests against the demo rig (Playwright)
```

### Console e2e

`tests/e2e` drives the Console in real browsers against its simulated rig (`streamstack console --demo`, started
on a free port with a throwaway HOME): shell, Live, Camera, Audio, Auto, System, Lighting, Interface, Mini, shortcuts, palette, reconnect,
mic revive, phone layout and an accessibility smoke. It is not part of `make test`. Set it up once in a venv:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[e2e]" && playwright install --with-deps chromium firefox
make e2e                    # E2E_BROWSERS=chromium to skip Firefox, E2E_ARTIFACTS=dir for the screenshots
```

CI runs the same suite in the `e2e` job (pinned `mcr.microsoft.com/playwright/python` image) and keeps the
screenshots and the JUnit report for 14 days. Page suites live in `tests/e2e/test_page_<name>.py`; slow runners can set
`E2E_TIMEOUT_SCALE`. The demo rig's lighting engine runs on a pinned clock (`STREAMSTACK_DEMO_CLOCK`, New York, noon
by default; `E2E_DEMO_CLOCK` overrides it), so the daylight schedule never depends on the hour or zone a run happens
in. The `e2e-lighting` job soaks both lighting suites three times at 03:30, 12:00 and 18:30; locally,
`scripts/e2e_lighting_soak.py --repeat 10` does the same. A new page gets its suite the same way, using the `app` fixture (see `tests/e2e/conftest.py` and `support.py`).

Releases follow [semantic versioning](https://semver.org) (0.0.x patch releases; see
[CHANGELOG.md](CHANGELOG.md)) and are cut from CI as signed wheels with an SBOM: [docs/release.md](docs/release.md)
covers the pipeline, `scripts/install-release.sh` and `streamstack verify-install`. Licensed under Apache-2.0.
