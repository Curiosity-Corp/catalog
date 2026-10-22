# StreamStack

StreamStack turns a Linux desktop into a repeatable call and stream studio for
Microsoft Teams, Zoom and OBS: a DSLR over USB live view or HDMI capture, a USB
microphone with a broadcast voice chain, one command to check picture and
sound before you go live, a panel menu, and an optional **auto-cinematographer
and auto-audio engineer** that uses Claude to keep the picture and voice right
during the call.

```text
Canon EOS (USB live view) --streamstack canon-bridge--> /dev/video42  "Canon EOS 50D"
Elgato Cam Link 4K (HDMI) ----------------------------> /dev/streamstack/camlink
            both --> OBS (lighting, colour, LUT, scenes) --> /dev/video43 "StreamStack Camera"
USB microphone --> Easy Effects (voice chain) --> "Easy Effects Source"
```

In Teams and Zoom, pick **StreamStack Camera** and **Easy Effects Source**.

## Install

StreamStack is a small Python package (standard library only) that drives
system tools. Install those from your distribution (Ubuntu/Debian names):

```bash
sudo apt install obs-studio easyeffects lsp-plugins-lv2 calf-plugins qpwgraph pavucontrol \
  v4l2loopback-dkms v4l2loopback-utils v4l-utils qv4l2 gphoto2 ffmpeg pulseaudio-utils \
  python3-gi gir1.2-ayatanaappindicator3-0.1 python3-websocket libnotify-bin
```

Then install StreamStack itself, either self-contained under `/usr/local`
(no pip, nothing in the system Python):

```bash
sudo make install            # PREFIX=/usr/local; DESTDIR= for packaging
streamstack setup            # as each user: OBS profile/scenes and voice presets
```

or with pip/pipx (`pipx install .`, plus `[auto]` for `websocket-client` when
the distribution package is not available).

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
| **Camera** | Picture grade: brightness, contrast, gamma, saturation and a warmth slider, sharpen and LUT, histogram and RGB parade or waveform scopes, thirds/safe/centre/zebra overlays, saved looks, scenes, virtual camera, record and stream. |
| **Audio** | A channel strip: raw mic and processed voice meters side by side, the gain fader and a big mute, tone knobs (bass, presence, treble, pitch), voice presets with A/B bypass, a 60-second loudness history, and a mic check that records raw then processed audio for you to compare. |
| **Auto** | The Claude director: on/off, ask it now, its latest note and confidence, a timeline of every change it made, and one-press undo. |
| **System** | Health checks with fixes, running services, shortcuts to OBS, Easy Effects, qpwgraph, pavucontrol and qv4l2, and the Teams and Zoom settings for this rig. |
| **Mini** | MUTE, PRIVACY, the meter and the scenes in a window small enough to sit beside the call. |

Everything works from the keyboard. `M` mutes, `P` turns on privacy, `B`
bypasses the voice effects, `1`…`9` switch scenes, `Alt`+a number changes
page, `Ctrl K` opens the command palette and `?` lists every shortcut. If the
mic watchdog sees a stalled microphone (its ALSA capture counters stop
moving), a banner with **Revive mic** appears and `Shift R` restarts it. It
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
camera in every mode it offers, shortcuts to OBS, Easy Effects, qpwgraph,
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
streamstack studio          # Easy Effects + Canon bridge + OBS with the virtual camera
streamstack doctor          # every device, its format, and what to pick in the call app
streamstack preview canon   # raw Canon feed (also: camlink, program); --grid for thirds
streamstack cameras         # every live camera and every mode it offers
streamstack preview /dev/video0 --mode '1920x1080@30 MJPG'
streamstack stop            # stop OBS (and the bridge): calls use the raw camera
streamstack mic-revive      # restart a stalled microphone (refuses a healthy one)
streamstack mic-check --raw # hear the raw mic, then the processed voice
streamstack voice           # list voice presets; `streamstack voice "StreamStack Voice Warm"`
streamstack auto on|off     # the Claude auto-cinematographer and auto-audio
streamstack call-settings   # the Teams and Zoom toggles that match this rig
streamstack encoder         # which video encoder OBS can use here; --apply sets it (OBS closed)
```

### Video encoder

OBS records and streams on the GPU when it can. `streamstack setup` and
`streamstack encoder --apply` ask the machine what it has (`vainfo` on every
DRM render node, and the encoder list OBS writes to its log) and pick, best
first: AV1 VA-API, AV1 Quick Sync, H.264 VA-API, H.264 Quick Sync, x264. No
render node path is stored; the VA-API encoders find theirs. Live settings are
CBR, no B-frames and a two second keyframe interval; recordings use VBR at
twice the bitrate. The active encoder shows in `streamstack doctor` and on
the Console System page, which warns when a better one is available. Start
OBS once before the first `encoder --apply` so it has listed its encoders.

### Voice presets

All presets share one chain (high-pass, noise removal, gate, compressor,
bass/presence/treble EQ, de-esser, pitch, limiter with a -1 dBTP true-peak ceiling), so bass, treble and pitch
are always one slider away in Easy Effects (Equalizer: band 0 is bass, band 3
is treble; Pitch: semitones).

| Preset | Character |
| --- | --- |
| StreamStack Voice | default broadcast voice |
| StreamStack Voice Warm | more low end for thin rooms or mics |
| StreamStack Voice Bright | more presence and air for dark mics |
| StreamStack Voice Deep | pitch down 2 semitones, extra body |
| StreamStack Voice Minimal | only high-pass, gentle compression and the limiter |

`streamstack setup` never overwrites your own changes; `streamstack setup
--force` restores the shipped presets and OBS defaults.

### Lighting and colour

The OBS scene collection **StreamStack** has the Canon and Cam Link sources
with three filters each: **Lighting & Color** (Color Correction), **Sharpen**
(off) and **Cinematic LUT** (off, teal/orange at 35%). Grade in OBS, not in
the call app, so Teams and Zoom both receive the same picture.

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

The graded camera runs OBS on top of the camera feed: it composites on the
GPU, then copies every frame back to the CPU for the virtual camera while the
browser encodes the call. On a dual-core laptop that can make the call stutter.

- For everyday calls on a laptop, `streamstack stop` and pick the raw camera.
  Grade in camera instead: Picture Style, Kelvin white balance and fixed
  exposure cost nothing.
- Keep the studio for calls that need scenes, a LUT or side-by-side.
- Match the OBS canvas to the camera instead of upscaling, for example
  `STREAMSTACK_WIDTH=1280 STREAMSTACK_HEIGHT=720 STREAMSTACK_FPS=20` for a
  720p20 PTP feed.

The Cam Link 4K delivers uncompressed frames from the camera's HDMI output, so
there is no decode on the CPU (USB live view is MJPEG). It needs a clean HDMI
output, mains power for long calls, and a USB 3 port (not a USB 2 hub).

The Cam Link has no modes of its own: it offers whatever the camera sends over
HDMI. StreamStack reads that list (found by USB id `0fd9:0066`, never by
`/dev/videoN`) and picks the best mode: 1080p60 over 1080p50 over 1080p30, raw
YUYV/NV12 before MJPEG (no decode, least latency), and 4K only with
`STREAMSTACK_CAMLINK_4K=true`. Set the camera's HDMI output to 1080p60 (or 50)
and turn off its on-screen display. `streamstack camlink` shows the offer and
the pick; `streamstack camlink --apply` tunes the running OBS Cam Link source
and, while nothing is on air, the canvas to match. `streamstack setup` seeds the
same mode into a new OBS profile. The Console re-reads the offer every few
seconds, so a camera switched on or to another mode appears on the Camera page
(HDMI chip on the Cam Link picture) and in the System health checks. The card's
HDMI audio is the camera's sound: it is listed as such and never offered as the
call microphone.

## Teams and Zoom

Start `streamstack studio` **before** joining, so the virtual camera is already
running when the browser enumerates devices. `streamstack call-settings`
prints the exact toggles; in short: camera StreamStack Camera, microphone
Easy Effects Source, headphones, noise suppression Low, no background effects
or touch-up, Zoom's automatic mic volume off. Make a test call first.

## Canon bridge

`STREAMSTACK_CANON_BRIDGE=gphoto2` (default) streams live view through
`gphoto2 --capture-movie | ffmpeg`, cropped to 16:9 at the configured size and
frame rate, as the `streamstack-canon` user service. `command` runs
`STREAMSTACK_CANON_COMMAND` instead (with `STREAMSTACK_CAMERA_DEVICE` set);
`none` is for HDMI-only cameras through the Cam Link.

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
| `STREAMSTACK_PROGRAM_DEVICE` / `_LABEL` | `/dev/video43`, `StreamStack Camera` | the OBS virtual camera node |
| `STREAMSTACK_WIDTH`, `_HEIGHT`, `_FPS` | `1920`, `1080`, `30` | Canon bridge output, and the OBS canvas when `STREAMSTACK_CAMLINK_MODE=off` |
| `STREAMSTACK_CAMLINK_MODE` | `auto` | `auto`: the best mode the HDMI source offers, and the OBS canvas follows it; `off`: keep the three settings above; `1920x1080@60 YUYV`: pin one mode (falls back to `auto` when the source does not offer it) |
| `STREAMSTACK_CAMLINK_4K` | `false` | let `auto` pick 4K |
| `STREAMSTACK_CAMERA_EXTERNAL` | `false` | another tool feeds the camera node |
| `STREAMSTACK_CANON_BRIDGE` | `gphoto2` | `gphoto2`, `command` or `none` |
| `STREAMSTACK_CANON_COMMAND` | | the bridge command for `command` |
| `STREAMSTACK_VOICE_PRESET` | `StreamStack Voice` | preset `streamstack studio` loads |
| `STREAMSTACK_MICROPHONE` | | part of the call mic's name; empty picks an Elgato Wave, then the default input |
| `STREAMSTACK_AUTO_BACKEND` | `claude-cli` | `claude-cli`, `api` or `local` |
| `STREAMSTACK_AUTO_MODEL` | | model for Claude passes (empty: the CLI default, `claude-sonnet-5-5` on the API) |
| `STREAMSTACK_AUTO_INTERVAL` | `15` | seconds between local exposure/gain passes |
| `STREAMSTACK_AUTO_AI_INTERVAL` | `300` | seconds between Claude passes (backs off to 4x when nothing changes) |
| `STREAMSTACK_AUTO_VIDEO`, `_AUDIO`, `_NOTIFY` | `true` | turn parts of auto mode off |

## Troubleshooting

- `streamstack doctor` reports a loopback device with the wrong name: another
  `/etc/modprobe.d/*.conf` sets v4l2loopback options. Remove it and reboot.
- The OBS virtual camera feeds the **first free** loopback node, not a named
  one. If the camera node has no producer yet, OBS can take it. `streamstack
  studio` waits for the camera and otherwise starts OBS without the virtual
  camera; `doctor` warns when OBS holds the wrong node.
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
on a free port with a throwaway HOME): shell, Live, Camera, Audio, Auto, System, Mini, shortcuts, palette, reconnect,
mic revive, phone layout and an accessibility smoke. It is not part of `make test`. Set it up once in a venv:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[e2e]" && playwright install --with-deps chromium firefox
make e2e                    # E2E_BROWSERS=chromium to skip Firefox, E2E_ARTIFACTS=dir for the screenshots
```

CI runs the same suite in the `e2e` job (pinned `mcr.microsoft.com/playwright/python` image) and keeps the
screenshots and the JUnit report for 14 days. Page suites live in `tests/e2e/test_page_<name>.py`; slow runners can set
`E2E_TIMEOUT_SCALE`. A new page gets its suite the same way, using the `app` fixture (see `tests/e2e/conftest.py` and `support.py`).

Releases follow [semantic versioning](https://semver.org); see
[CHANGELOG.md](CHANGELOG.md). Licensed under Apache-2.0.
