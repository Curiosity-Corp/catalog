# Call awareness

StreamStack knows when you are in a meeting call, and warns once when you talk while the call is muted. It is built
for the live desk: it never opens a camera, never reads an audio stream and never sends anything anywhere.

## What it reads

Two kinds of metadata, and nothing else:

- **Camera use.** Which processes hold `/dev/video*` open, from `/proc/<pid>/fd` (the same scan as the camera-in-use
  guard). The device is never opened.
- **Capture streams.** `pactl list source-outputs` (names, corked and muted flags). The audio itself is never read.
- Optionally, window titles from `wmctrl -lp`, only to name the service (Teams, Zoom, Meet) for a browser.

An app counts as a call app by its name (Teams, Zoom, Meet in a browser, Webex, Slack huddle and similar).

## States

| State | Meaning |
| --- | --- |
| `idle` | no call app is using the camera or the microphone |
| `joining` | a call app has the camera or a stream, but not yet for the settle time (a lobby also looks like this) |
| `in_call` | a call app has held the camera or a capture stream for the settle time |

Leaving needs the evidence to be gone for a short while, so a stream that blinks does not flap the state.

The mic state is **inferred**: a corked, muted or absent capture stream means muted in the app; a live stream means
open. An adapter can report the app's own mute with `call.report_mute` (switch on `adapters`); a fresh report wins.

## The "you are muted" warning

While in a call that looks muted, StreamStack watches the level of the raw microphone (the meter the Console already
has) with its own voice detector: an adaptive noise floor, a threshold above it set by the sensitivity, and speech
declared after 400 ms above the threshold within a second. A keyboard clack or a cough is shorter and never counts.

One toast per episode, then quiet until you stop talking, the cooldown (default 20 s) has passed, or you unmute or the
call ends. Settings are on the System page (Call awareness) or `call.set`: on/off, sensitivity, cooldown, a beep.

Limits, stated plainly:

- The Console's own Mute silences the microphone itself, so there is nothing for the detector to hear. The warning is
  for a mute inside the meeting app that leaves the StreamStack microphone live.
- A pre-join lobby is indistinguishable from a call and counts as one.
- A browser extension and a Teams WebSocket adapter that would report the app's own mute are not shipped yet; the
  `call.report_mute` channel is ready for them.

## For other features

```python
from streamstack import callstate

snap = callstate.current_call(cfg)     # one probe: CallSnapshot(state, service, mic, camera, ...)
snap.in_call, snap.active, snap.muted

monitor = callstate.CallMonitor(cfg)   # polls, with settle and leave hysteresis
monitor.subscribe(lambda before, after: ...)
```

Inside the Console, `rig.call.snapshot()` and `rig.call.active()` give the same answer without another probe. Anything
that plays sound on the speakers (the echo chirp, #48) must stay silent while `active()` is true.
