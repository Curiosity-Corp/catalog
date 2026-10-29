# Safe join

At 6 am or midnight you join Teams or Meet before the picture, the lights or the microphone are ready, and you are on
air at once. Safe join keeps you off air until you press **Go live**.

## The setting

System page, **On call start** (action `safejoin.set`, saved in `console-safejoin.json`):

| Mode | What happens when a call app opens the camera |
| --- | --- |
| Off | nothing |
| Warn (default) | one notification: "Teams has the camera. You are on air: check privacy and the microphone." |
| Safe join | privacy on (the camera shows Be Right Back) and the microphone muted; a banner on Live and Mini says "You are not on air" with a Go live button |

An optional timer (`auto_release_s`, 0 = never, up to 600) goes live by itself.

## Go live

The banner button, **Space**, the palette command "Go live", or a Stream Deck key (the catalog entry "Go live", lit
while something is held) all run `safejoin.go_live`: privacy off and unmute, together.

## Rules that keep it out of your way

- **Only a call app counts.** The trigger is a call app (Teams, Zoom, Meet and the rest, docs/call-awareness.md) holding
  the camera. An OBS preview or the mic check never starts it. A call with only a microphone does not either: the
  camera is the trigger, a microphone stream is only a hint (browsers keep it open while muted).
- **Once per call.** After Go live, or after you released something yourself, it never takes it again in that call.
  A new call is a new decision.
- **It only holds what it took.** A microphone that was already muted, or privacy that was already on, is yours: it is
  listed under "kept" in the event, Go live leaves it alone, and so does the end of the call.
- **Your hand wins.** Unmute or switch privacy off during the hold and that part of the hold ends at once (a short
  settle time after it acted is ignored, so the actuators have time to show their new state).
- **The call ending gives back what it still holds**, so the desk is never left muted and private.
- **One actuator failing does not stop the other**; the failure is in the event.
- **Lights are not touched here.** Lighting automation's camera follow starts from the same camera use on its own, so
  the lights come on without anything fighting. The Director does not change privacy or mute.

## Event list

Each call start leaves one entry (newest first, 20 kept; shown on the System page): when, which app, the mode, what
was done, what was kept, and notes for what happened later (released by you, by the timer, or by the call ending).

## State and actions

`safejoin` in the snapshot (docs/console.md); actions `safejoin.set`, `safejoin.go_live`, and for the demo rig
`safejoin.demo_reset`. It follows `rig.call` (`CallController.subscribe`); with no call controller it never acts.
