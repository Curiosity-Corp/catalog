# Time-of-day profiles

A video call at 6 am by the window and one at 9 pm under the lamp want different pictures. A **profile** is what the
rig should look and sound like for one part of the day. StreamStack keeps one per slot and applies it for you, so
nobody has to remember to warm the picture up in the evening.

## The slots

The day is cut by the sun, using the property's location (the same place the AI Director and the lighting schedule
use; see docs/auto-mode.md and docs/lighting.md) and the offline NOAA solar model. No network is needed.

| Slot | When |
| --- | --- |
| Night | the sun is well below the horizon |
| Dawn | first light, before sunrise |
| Morning | the sun is up, before solar midday |
| Midday | within about two hours of solar noon |
| Afternoon | the sun is up, past midday |
| Evening | dusk, after sunset |

The AI Director has five slots (no midday). A profile's midday maps to the Director's morning before solar noon and to
its afternoon after it.

## What a profile holds

Every part is optional; a part left out stays as it is.

| Part | Applied with | Notes |
| --- | --- | --- |
| `look` | `video.looks.apply` | a name from the Looks library (the native engine eases it in over 1.5 s) |
| `scene` | `video.scene` | a scene name |
| `voice` | `audio.preset` | a voice chain preset |
| `lighting` | `lighting.preset` | a lighting preset; the lights **fade** there over `fade_seconds` (default 4 s) |
| `canon` | `video.canon_set` | `exposure_comp` (a stop value the camera offers) and/or `kelvin` (2500 to 10000, in steps of 100) |
| `blur` | `video.blur` | `{"on": true}` or `{"on": false}`, optionally `strength` |

A profile only runs actions the Console already has, so it can do nothing a person could not do with the same
buttons, and every change appears wherever that change normally appears.

## Modes

* **Off** (the default): nothing happens by itself. "Apply" on a slot row still works.
* **Suggest**: when a profile would apply, the Camera page shows a toast and an "Apply" button instead.
* **Automatic**: the profile applies by itself.

## When a profile applies

* When a **call starts** (a program such as Teams, Zoom or a browser opens the camera): the current slot's profile.
* When the **slot changes while no call is running**: the new slot's profile.
* **Never in the middle of a call.** A slot change during a call would move the picture under the people on the other
  side. It waits and applies when the next call starts. The log says so.

Call detection is the lighting service's "camera in use" signal for now (the same one camera follow uses). When the
call-state detection from the Meeting-ready autopilot lands (#45), the controller reads that instead.

## Who wins

* **A person always wins.** After a profile is applied, the controller watches the controls it set. If someone changes
  one (the Console, a Stream Deck key, the light's own buttons), the profile steps back until the slot ends, and the
  status line says which control did it ("You changed the lights, so the dawn profile waits until the slot ends").
  "Hand back to profiles" ends the hold early.
* **The AI Director owns the picture while it runs.** With the Director on, the profile's look and lights become the
  Director's *baselines for that slot* (the same learned values `director.learning` keeps) instead of being written to
  the rig, and the voice preset and the Canon settings are left to the Director. The scene and the blur, which the
  Director never touches, are still applied. The two never fight.

## Undo and the log

"Undo" puts back what the last apply replaced (look, scene, blur, voice preset, Canon values, lights) and holds the
profile off until the slot ends. The popover lists what happened and why, newest first: applied, left alone during a
call, held after a manual change, skipped with the reason (for example "that look was deleted").

## The Console

Camera page, "Day" button below the picture: a mode switch, a strip of the day with a marker for now, and a row per
slot with a drop-down for each part. **Use current** stores what the rig is doing right now as the slot's profile
(a look or lights setup that has no name yet is saved first). **Fill empty slots** adds sensible starter picks to the
slots that have none. The lights fade length is at the bottom.

## Actions

| Action | Params | Effect |
| --- | --- | --- |
| `slots.mode` | `mode`: `off` \| `suggest` \| `auto` | when profiles apply |
| `slots.fade` | `seconds` 0-30 | how long the lights take to reach the new level |
| `slots.profile` | `slot`, `profile` | store one slot's profile (an empty object clears it); names the rig does not have are refused |
| `slots.clear` | `slot` | forget a slot's profile |
| `slots.save_current` | `slot` | store what is on now as that slot's profile |
| `slots.starter` | | fill the empty slots with starter picks |
| `slots.apply` | `slot?` | apply a slot's profile now (default: the current slot); 409 when it has none |
| `slots.undo` | | put back what the last apply replaced; 409 when there is nothing to undo |
| `slots.resume` | | end a manual-change hold |
| `slots.dismiss` | | drop the suggestion |
| `slots.import` | `document` | replace the profiles from a `streamstack.slot-profiles/1` document |
| `slots.reset` | | back to the defaults (mode off, no profiles) |

The demo rig adds `slots.demo.clock {time: "HH:MM"}` to move its clock and `slots.demo.reset`.

`GET /api/slots/profiles.json` returns the profiles as `{"document": "streamstack.slot-profiles/1", "fade_seconds",
"profiles"}`; `slots.import` takes the same document back. The mode is not part of it: each machine chooses its own.

The Stream Deck palette (group Camera) offers **Time of day** (apply the current slot's profile; lit while one is in
place), **Undo profile** and **Profiles off / suggest / auto**. They are rules in `deck/catalog.py`, so MIDI and
hotkey bindings (#44) get them too.

## Files

`~/.config/streamstack/slot-profiles.json`, mode 600, written atomically; a damaged copy is set aside and restored from
the last good one by `streamstack.store`. The snapshot block is `slots` (docs/console.md).
