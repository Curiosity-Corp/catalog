# Auto mode: auto-cinematographer and auto-audio

Auto mode keeps the picture and the voice right while a call or stream runs.
It is off until you turn it on, from the panel menu (**Auto-cinematographer
and auto-audio (Claude)**) or a terminal:

```bash
streamstack auto check            # Console, Claude and microphone ready?
streamstack auto on               # start it (the streamstack-auto user service)
streamstack auto once             # one Claude pass now, applied
streamstack auto once --dry-run   # what Claude would change, nothing applied
streamstack auto status           # on/off and the recent changes
streamstack auto off              # stop; the current look and levels stay
streamstack auto reset            # ramp back to the look, mic gain and preset from before
```

It works on the graded studio camera, so start the studio (and the Console) first.

## The two loops

**Local, every `STREAMSTACK_AUTO_INTERVAL` seconds (15).** Nothing leaves the host.

- *Picture.* A small screenshot of the graded program picture, fetched from the
  Console, is measured with ffmpeg `signalstats`. Gamma, then brightness, of the
  native engine's live look move towards a good exposure without clipping highlights.
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
| `audio.voice_preset` | switch the voice chain preset (confidence ≥ 0.7, at most every 10 minutes) |
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
registry (`streamstack.director.actuators`): the program picture look, mic gain, the
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
  exposure knob that leaves the body's own metering in charge). The engine's
  brightness and gamma take over when the camera is at its limit, locked (the
  mode dial is on M), held because you moved it, or refuses the change. This
  assumes the camera on air is the Canon.
* **White balance** is a closed loop on the measured warm index: once per Claude
  pass (also with the `local` backend) the camera's Kelvin steps one notch
  towards the persona's colour temperature. While the camera does it, the engine's
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
  the balance you set between them (a dimmer fill) is kept. The Lighting page's daylight schedule (`docs/lighting.md`)
  uses the same slots; while auto mode drives the lights the schedule steps aside so the two never fight, and the
  director still never touches power (camera follow does that).
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

### Picture calibration (issue #32, colour half)

Measured on 2026-10-22 between 23:47 and 23:58 UTC on the real rig, read-only: stills from the program output
(`/dev/video43`, 1920x1080), four moments about three minutes apart, 8 to 20 frames each, run through the shipped
extractors at the shipped 320 px decode. The Canon loopback (`/dev/video42`, 1280x720 YUYV, upscaled and cropped
from the 1056x704 live view by `canonctl`) could not be opened by a second reader because the video compositor of the time held it, so the
program output stands in for it. The room was dark at every moment and nobody was in frame. No photo is kept in the
repository; only these numbers and synthetic frames in the tests.

| Moment (UTC) | luma mean | p99 luma | pixels with luma >= 40 | cast read (before) | warm index | saturation | sharpness |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 23:47 | 0.1 | 0 | 0 of 57,600 | none | none | none | 0.3 to 0.9 |
| 23:51 | 0.0 | 0 | 0 of 57,600 | none | none | none | 0.1 to 0.2 |
| 23:54 | 0.0 | 0 | 1 of 57,600 | cool, green tint | -0.95 to -1.26 | 0.74 to 0.96 | 0.7 to 0.9 |
| 23:58 | 0.0 | 0 | 3 of 57,600 | neutral, with or without a green tint | -0.09 to +0.09 | 0.10 to 0.24 | 2.6 to 6.0 |

What was miscalibrated, and the verdict before and after for the 23:54 burst under the Late-night calm persona:

* **The cast came from a handful of pixels.** The grey-world estimate averages the pixels between luma 40 and 215.
  In a dark room one to three glints (a status LED, a screen edge) were the whole sample, so a black frame read
  "cool, green tint", warm index -1.02 (wanted +0.18), saturation 0.81 and a "screen glow" note. It is now read only
  when at least 5% of the frame is in the mid-tones (`MIN_MIDTONE_SHARE`); otherwise cast, warm, tint and
  saturation are None.
* **A black frame was called soft.** Laplacian variance of a black frame is 0.1 to 6, below the soft limit of 15, so
  every dark pass raised "The picture looks soft". The judge now says "too dark to judge" instead and skips white
  balance, tint, saturation, contrast, focus and screen glow whenever the 99th-percentile luma is below 40
  (`DARK_P99`). The exposure and crushed-shadow findings still fire. A dim but real picture (p99 of 60) is still
  judged for focus.
* **Relative checks fired on noise.** `blurred_frames` marks frames below half the burst's best sharpness, and
  flicker is a luma swing relative to the mean; with a mean of 0.1 to 3 and sharpness of 0.1 to 6 both are noise.
  They now need a mean luma of 10 (`MIN_FLICKER_LUMA`) and a best sharpness of 15 (`MIN_BLUR_REFERENCE`).

Not changed, because the dark room gave no evidence: the persona exposure and colour bands, the skin box, the
backlight margin, the noise limit of 3.0 (noise sigma read 0.01 to 0.02 in the black frames) and the sharpness
limit for lit pictures. The 1280x720 feed is area-downscaled 4x to 320 px, so the 1.21x upscale from the Canon's
1056 px live view does not move the sharpness scale. These still need a pass with the lights on and the person in
frame; the audio half of #32 also remains.

### Calibrating on the real rig: the procedure (issue #32, audio half and the lit-room pass)

`tools/calibrate_director.py` collects the evidence and prints suggestions. It never edits the shipped bands: the
persona bands stay as they are until the numbers justify a change in a reviewed MR. It needs a person, so it is run by
the owner at an idle desk (no call; `record` refuses while a call app holds a capture stream):

1. Voice. In each room and mic setup (Wave XLR, Scarlett, with and without the voice chain), read the same passage
   aloud at the normal distance: `python3 tools/calibrate_director.py record --label xlr-office-day --execute`, 20 s
   by default, from the StreamStack Mic. Then `analyze --kind voice --label xlr-office-day calibration/*.wav --out
   voice.jsonl`.
2. Picture. With the lights on and the person in frame, take a few stills of the program output in each light (day,
   evening, lamp only) and run `analyze --kind picture --label canon-lit stills/*.jpg --out picture.jsonl`.
3. `fit voice.jsonl` prints the spread of every number and, per voice bar, the shipped band against the 5th to 95th
   percentile of the clips. It withholds suggestions below 8 clips from 2 setups. Paste the table on #32 and, if a band
   clearly misfits, change it in `audiofeatures.scores` or the persona files with the table in the MR description.

Do not commit the WAVs or stills; only the numbers.

### Deterministic exposure and colour match (issue #94)

`streamstack.director.metering` holds the controllers that decide how far to move, as pure functions of a measurement
(no clock, no randomness, so the same readings always give the same moves):

* `meter_box` reads a face or grey-card box from an rgb24 frame, leaving out clipped pixels and counting them.
* `ExposureController` works in stops (`2.2 * log2(target / luma)`). It has a dead band (0.15 stop), a gain of 0.7 so
  a camera that is slow to settle cannot overshoot, and a cap of one stop per pass. It shares the correction between
  the knobs in the order given: the Canon's exposure compensation first (thirds of a stop, before compression), then
  the key light's brightness; a knob at its limit passes the rest on, and what nobody can give is reported as
  `leftover` for the program look. It never adds light while more than 2% of the box is clipped.
* `ColourController` reads a grey card: the key light goes to the persona's colour temperature first, then the
  camera's white balance is trimmed by the card's red/blue ratio until it equals the light's temperature (the
  cross-property match). A green or magenta card is reported, not chased.

`tests/test_director_metering.py` drives both against a simulated rig (quantised Canon, key light with limits, ambient
light, grey card): from dark and bright rooms and every white balance start the loop settles inside 0.2 stop and 150 K
in under 20 passes, two runs give identical trajectories, a settled loop does not move, and it re-settles after the sun
comes out. The model constants (`KELVIN_PER_STOP`, gamma) are typical values; the loop corrects what they get wrong.

### The controllers in the live loop (issue #150)

`auto.py` runs both controllers on the live rig (`streamstack.director.loopmeter` turns device readings into their
inputs):

* **Exposure.** When the metered luma (the face once located, otherwise the frame) leaves the persona's band, the
  `ExposureController` aims at the middle of the band, in stops, and shares the correction: the Canon's exposure
  compensation first, then the key lights' brightness (light output is linear in lumen; a group of lights moves
  together and the smallest room limits it), and what neither can give is left to the program look. Each pass is
  bounded twice: the controller (gain 0.7, at most one stop) and the governor (one Canon notch every 30 s, 12 % of a
  light's range every 20 s). A knob the person moved by hand is held and leaves the room; with no Canon and no light
  the program look works as it always did. A light the controller has moved is no longer eased to the persona's
  brightness (its colour temperature still is), so the two loops never pull in opposite directions.
* **Colour.** Set `STREAMSTACK_AUTO_GREYCARD=x,y,w,h` (fractions of the frame, for example `0.85,0.75,0.1,0.15`) to a
  grey card in the picture. When the exposure is in band the card is read each pass and the Canon's white balance is
  trimmed until the card is neutral, one bounded step a minute, never further than 1500 K from the light (any value in
  the camera's range when there is no light to agree with). It waits while a key light is still moving to the persona's
  colour temperature, and a green or magenta card is reported, not chased. With a grey card the white balance is true
  colour; the persona's warmth then comes from the program look's warmth, not from the camera. Without a card the
  white balance is the persona-Kelvin step it was.
* **Opt-in.** The controllers are off by default (`STREAMSTACK_AUTO_METERING` unset or `false`): their model constants are
  not tuned on the real Canon and Litra yet (the grey-card check in #94), and untuned closed-loop exposure on a live
  call is worse than the old step. Set `STREAMSTACK_AUTO_METERING=true` to turn them on (it is a normal setting, in the
  Console's director settings with the other `STREAMSTACK_AUTO_*` ones); on by default is the step after the hardware
  tuning. With it off the director returns to the one-notch Canon step and the persona Kelvin step, and the
  lights' brightness and the grey card are never touched by exposure or colour.

Every pass is journalled (`metering` events with the error in stops, the shares and what was left over, next to the
usual `change` events). `tests/test_auto_metering.py` runs the loop on a simulated rig of fakes (a Canon, a Litra, a
scene model, the real governor on a fake clock): from dark and bright rooms the picture settles into the band without
overshoot or hunting, steps stay bounded, the camera moves before the light, a settled loop does not move, and a
disabled controller gives the old behaviour and never writes to a light.

Still to be measured on hardware (checklist on #94): the model constants against the real Canon, Litra and a grey card.

## The Console link

Auto mode is its own process, and the native video engine lives inside the Console, so the director changes the
picture through the Console's local API: `POST /api/action` (`video.look`) for a look change, `GET /api/state` for
what is in place and `GET /api/preview.jpg?source=program` for the graded picture it measures. The port and the
token come from the Console's runtime file (mode 600), so there is nothing to configure and nothing listens
beyond 127.0.0.1. A look change is a few bytes on the engine's stdin and lands between two frames. `streamstack auto
check` (also run by `auto on`) reports whether the Console is reachable; with no Console running the picture
actuator is listed as *Unavailable* ("the Console is not running") and the voice and hardware actuators carry on.
