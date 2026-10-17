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

What leaves the host per Claude pass: one JPEG still of the graded camera
(768 px wide), picture statistics, the current filter values, voice
*statistics* (levels in dBFS, never audio) and the names of your voice
presets. The last still sent is kept at `~/.cache/streamstack/auto/last-ai.jpg`,
readable only by you.

## OBS websocket

Auto mode drives OBS through its built-in obs-websocket server on
127.0.0.1. `streamstack auto check` (also run by `auto on`) enables it with a
random per-user password stored in OBS's own config (mode 600). OBS rewrites
its settings when it exits, so the first time it asks you to restart OBS once.
