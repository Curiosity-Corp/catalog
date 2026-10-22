# Auto mode: auto-cinematographer and auto-audio

Auto mode keeps the picture and the voice right while a call or stream runs.
It is off until you turn it on, from the panel menu (**Auto-cinematographer
and auto-audio (Claude)**) or a terminal:

```bash
streamstack auto check            # OBS websocket, Claude and microphone ready?
streamstack auto on               # start it (the streamstack-auto user service)
streamstack auto once             # one Claude pass now, applied
streamstack auto once --dry-run   # what Claude would change, nothing applied
streamstack auto status           # on/off and the recent changes
streamstack auto off              # stop; the current look and levels stay
streamstack auto reset            # ramp back to the look, mic gain and preset from before
```

It works on the graded studio camera, so start the studio first.

## The two loops

**Local, every `STREAMSTACK_AUTO_INTERVAL` seconds (15).** Nothing leaves the host.

- *Picture.* A small screenshot of each camera source through obs-websocket is
  measured with ffmpeg `signalstats`. Gamma, then brightness, of the **Lighting
  & Color** filter move towards a good exposure without clipping highlights.
  Once Claude has located the face, only that box is metered (spot metering),
  towards the face luma Claude chose for that skin tone, ±15. Black or flat
  frames (camera off, lens cap, no HDMI signal) are skipped, never "fixed".
- *Voice.* Four seconds of the raw microphone are measured with `astats`. The
  input gain moves 1–2 dB at a time so speech peaks sit around −24 to −12 dBFS
  and never clip. Steady room noise never raises the gain (speech is bursty;
  noise is not), and the gain stays within −12/+6 dB of where it started.

**Claude, every `STREAMSTACK_AUTO_AI_INTERVAL` seconds (300).** One still of
the graded camera, the measured picture and voice statistics, the current
settings, the allowed ranges and the available voice presets go to Claude. It
returns a proposal that must match a JSON schema:

| Field | Effect |
| --- | --- |
| `video.contrast`, `saturation`, `warmth`, `gamma`, `brightness` | absolute targets, clamped to the limits and to one step per pass |
| `video.lut_enabled`, `lut_amount`, `sharpen_enabled` | the Cinematic LUT and Sharpen filters |
| `audio.voice_preset` | switch Easy Effects preset (confidence ≥ 0.7, at most every 10 minutes) |
| `subject.box`, `subject.target_luma` | the face, for the local loop's spot metering |
| `confidence`, `notes` | below 0.5 nothing is applied; the note appears as a notification |

When a pass changes nothing, the next one waits 2x and then 4x the interval. A
changed picture (lights, camera, person) brings it back after a minute.

## Limits

| Setting | Range | Max step per Claude pass |
| --- | --- | --- |
| brightness | −0.15 … 0.20 | 0.08 |
| contrast | −0.30 … 0.40 | 0.15 |
| gamma | −0.50 … 0.60 | 0.20 |
| saturation | −0.30 … 0.35 | 0.15 |
| warmth | −0.50 … 0.50 | 0.25 |
| LUT amount | 0 … 0.60 | 0.20 |
| mic gain | −12 … +6 dB from the start | 2 dB per take |

Every change is ramped over a few seconds so viewers never see a jump, and
logged to `~/.local/state/streamstack/auto.jsonl` (rotated at 2 MB). The
first time auto mode changes a source, mic or preset it records the previous
value in `~/.local/state/streamstack/baseline.json`; `streamstack auto reset`
ramps back to those and clears the record.

## Backends and privacy

| `STREAMSTACK_AUTO_BACKEND` | Uses |
| --- | --- |
| `claude-cli` (default) | the signed-in Claude Code CLI, so passes run on your Claude subscription |
| `api` | the Anthropic Messages API with `ANTHROPIC_API_KEY` |
| `local` | no Claude, only the local loops |

The CLI backend runs `claude -p` with `--restricted --tools Read` in a private
temporary directory that holds only the still, with no MCP servers
(`--strict-mcp-config`), no saved session (`--no-session-persistence`) and
schema-validated output (`--json-schema`). Claude can read that one image and
nothing else; it cannot run commands, edit files or browse.

The API backend sends the still and the prompt with forced tool use
(`tool_choice` on a tool whose input schema is the proposal schema). The key
comes from the environment or `~/.config/streamstack/auto.env`, which must be
mode 600:

```bash
install -m 600 /dev/null ~/.config/streamstack/auto.env
echo "ANTHROPIC_API_KEY=..." >> ~/.config/streamstack/auto.env
```

What leaves the host per Claude pass: **the stills go to Claude**. A burst of
`STREAMSTACK_AUTO_BURST` (default 4, at most 6) still pictures from the graded
camera, `STREAMSTACK_AUTO_BURST_SECONDS` apart, is joined side by side into one
contact sheet (640 px per frame) and sent as one image. With the `local`
backend nothing is sent. The text that goes with it holds the measured numbers
(picture statistics, the findings, the current filter values, the persona and
the time of day), voice *measurements* and the names of your voice presets.
**Audio never leaves the computer**: a short clip of the microphone
(`STREAMSTACK_AUTO_CLIP_SECONDS`, default 6) is recorded, analysed here into
numbers and deleted; only the numbers (pitch, depth, clarity, sibilance,
loudness, noise, room) are in the prompt. The last contact sheet sent is kept
at `~/.cache/streamstack/auto/contact-sheet.jpg` (and `last-ai.jpg`), readable
only by you; the Auto page shows it so you always see what Claude saw.

## The director: observation, personas, time of day

The Claude pass is a director, not just an exposure nudge.

**Observation.** Each pass takes a burst of stills and measures every frame on
this computer (pure Python, no OpenCV): a 16-bin exposure histogram, clipped
highlights and crushed shadows, white balance and tint, saturation, a skin-tone
patch (brightness and redness), sharpness, noise, and where a face-sized
skin-coloured subject sits and how much headroom it has. Across the burst it
notes flicker and motion. The voice clip gives f0 (median and range), spectral
centroid and tilt, low-end body (depth), 2 to 4 kHz clarity, 5 to 9 kHz
sibilance, the noise floor and SNR, LUFS and a reverb (RT60) estimate. Those
numbers become *findings* ("face too dark for this persona", "room is
echoey") that appear on the Auto page and in the prompt.

**Personas** are JSON files in `src/streamstack/data/personas/` (Executive,
Bright podcast, Late-night calm, Daylight natural, Crisp newsroom): a target look
(exposure bands, warmth in Kelvin, contrast, saturation, mood words and things
to avoid) and a target voice (preset, level, depth/clarity/sibilance bands,
minimum SNR, maximum RT60). Choose one on the Auto page or with `streamstack auto
persona [ID]`; edit it there too. An edit is saved as
`~/.config/streamstack/personas/<id>.json` and wins over the shipped file;
**Restore the original** deletes it. A malformed file is skipped and reported.
Claude is asked to judge mood and tone against the persona; the local loops
meter towards its bands (`STREAMSTACK_AUTO_PERSONA_LOOK=false` keeps the old
fixed bands).

**Time of day.** The local clock and the sun decide the slot (night, dawn, morning,
afternoon, evening). Sunrise and sunset are computed offline (NOAA formulas)
from the property location: set it on the Auto page or with `streamstack auto
property NAME LAT LON`, or `STREAMSTACK_PROPERTY`, `STREAMSTACK_LATITUDE`,
`STREAMSTACK_LONGITUDE`; with none, the time zone's city is used. There is no
network geolocation. Per persona and slot, the settings you *kept* (looks you
changed by hand count more than ones left alone) are remembered in
`~/.local/state/streamstack/director-baselines.json` and used as the starting
point next time.

**Actuators and safety.** Everything the director can move sits behind one
registry (`streamstack.director.actuators`): OBS look filters, mic gain, the
voice preset and, when the device is there, the Canon, the Litra lights and the
audio interface's preamp. Each declares its parameters, limits, step size and
minimum interval. A change is a small step towards the target, at most once per
interval; a value you move by hand is left alone for 30 minutes (`override` in
the log); every change is journaled with its reason and `auto reset` undoes
them all (the look, mic gain, voice preset, camera, lights and preamp gain).
To add an actuator, subclass `Actuator`, register it in `Auto._registry` and it
inherits the rate limits, override handling, reasons and undo.

**Hardware actuators.** Each is cleanly absent when its device is not there:
the Auto page lists it as *Unavailable* with the reason, and nothing is probed
beyond a read-only look at sysfs, the Canon bridge's socket or `amixer`. They
are found by USB id, never by a name that belongs to one machine, and each has a
setting (`STREAMSTACK_AUTO_CANON`, `STREAMSTACK_AUTO_LIGHTS`,
`STREAMSTACK_AUTO_HARDWARE_GAIN`, all `true`).

| Actuator | Moves | Never touches |
|---|---|---|
| Canon (`canon-exposure`, through the Canon bridge) | exposure compensation (EV, a third of a stop a pass, at most every 30 s) and white balance Kelvin (400 K a pass, at most every 60 s) | focus, ISO, shutter, aperture, picture style |
| Litra (`litra`) | each light's brightness (within its own model's lumen range, 12 % of the range a pass, never below 10 %) and colour temperature (2700 to 6500 K, 300 K a pass) | power: a light that is off stays off |
| Audio interface (`interface-gain`) | the preamp gain of the first input (`STREAMSTACK_AUTO_INTERFACE_INPUT` picks another), 2 dB a pass, inside the same +/- window around where auto mode found it as the software gain | 48V, Autogain, Air, Safe, Pad, Link, Line/Inst |

* **Exposure** goes to the Canon first: when the metered picture is outside
  the persona's band the director steps the camera's exposure compensation (the
  exposure knob that leaves the body's own metering in charge). OBS's
  brightness and gamma take over when the camera is at its limit, locked (the
  mode dial is on M), held because you moved it, or refuses the change. This
  assumes the first camera in the OBS program scene is the Canon.
* **White balance** is a closed loop on the measured warm index: once per Claude
  pass (also with the `local` backend) the camera's Kelvin steps one notch
  towards the persona's colour temperature. While the camera does it, OBS's
  warmth easing steps aside. Only used when the white balance is Auto or Color
  Temperature; a preset (Daylight, Tungsten) is yours and is left alone.
* **The Canon bridge must share the camera.** The gphoto2 command backend
  pauses the picture for every change; the Canon actuator reports that as its
  reason and stays off until `python3-gphoto2` is installed.
* **Lights follow the hour.** The persona's `light` (brightness and Kelvin) is
  the baseline; the dark slots raise and warm it (night +20 points and 400 K
  warmer, dawn +15 and 250 K, evening +12 and 350 K), the afternoon trims it
  (-10 points, 200 K cooler). Once you set the lights by hand in a slot, that
  level is learned and replaces the baseline for that slot. With several lights
  the balance you set between them (a dimmer fill) is kept.
* **Preamp gain comes before software gain.** Boosting a quiet microphone in
  software raises its noise too. The Scarlett 2i2 Gen 4 has a software gain;
  the Gen 3 has analogue knobs only; the Wave XLR's gain is only used when
  `STREAMSTACK_WAVE_XLR_USB` is enabled (it is off by default, and the device
  is never opened while it is off). With no usable hardware gain the PipeWire
  software gain is used as before, and a failed hardware change falls back to
  it for that pass.
* **Face finder.** With the framing extra installed and the YuNet model
  fetched (`streamstack framing --fetch`), the director finds the face with
  YuNet (on the CPU, so it never competes with live framing for the GPU) and
  meters that box; without them the skin-colour heuristic is the fallback, and
  the Auto page says which one is in use. `STREAMSTACK_AUTO_FACES=false` keeps
  the heuristic.
* Claude's proposals do not drive these actuators yet; they are moved by the
  local loops, from measurements, with the reason in the timeline.

**Calibration caveat.** The 0 to 1 scores behind the voice bars (pitch 70 to
300 Hz, depth, clarity, sibilance, clean) and the colour-cast and skin-patch
heuristics are linear maps checked against synthetic signals, not tuned on
recorded voices or the Canon and Cam Link. Treat them as relative, and
adjust the persona bands after watching a few passes.

## OBS websocket

Auto mode drives OBS through its built-in obs-websocket server on
127.0.0.1. `streamstack auto check` (also run by `auto on`) enables it with a
random per-user password stored in OBS's own config (mode 600). OBS rewrites
its settings when it exits, so the first time it asks you to restart OBS once.
