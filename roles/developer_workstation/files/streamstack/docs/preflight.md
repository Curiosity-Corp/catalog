# Pre-flight check

One press before a meeting: StreamStack looks at the camera, microphone, outputs, lights, network and headroom,
listens to you say your name once, and gives a score out of 100 with a reason and a one-press fix for each finding.
It takes about twenty seconds. It never changes anything by itself except when you press a Fix.

## Running it

- Console, Live page: the **Pre-flight** button under the picture opens the dialog; **Run pre-flight** starts it.
  **Quick check, no voice** skips the spoken prompt. The command palette has "Run the pre-flight check".
- CLI: `streamstack preflight` (add `--no-listen` to skip the prompt, `--json` for the result). Exit code 0 means
  ready, 1 means something to check, 2 means no Console is running. `streamstack status` shows the last score.
- Action: `preflight.run` with `listen?` (default true).

It refuses to start while a run is in progress (409). Use the quick check when you are already in a call, so the
prompt does not talk over the meeting.

## What is checked

| Check | Weight | Looks at | Typical fix |
| --- | --- | --- | --- |
| Camera | 22 | a picture is delivered, frame rate, a face in the on-air picture, exposure | start the studio, privacy off |
| Microphone | 26 | live and unmuted, noise floor, your voice level and clipping while you say your name | unmute, revive the microphone, look again, clear the clip mark |
| Outputs | 14 | a default output exists and is audible, quiet outputs for headsets | use another default output, unmute, set the volume |
| Lights | 8 | the lights you have are on | turn the lights on, rescan, set brightness or colour temperature |
| Network | 14 | round trip, jitter, loss, uplink | none (shown with the numbers) |
| Headroom | 10 | GPU load | none |
| Meeting app | 6 | only when a call is on (#45): the app uses StreamStack Camera and Mic | none (pick them in the app) |

A check that cannot be judged (no lights, no network measurement yet, auto-framing off, no call) is skipped and does not
count against the score. Each finding carries at most one Fix, an existing Console action; pressing it runs that
action and re-judges the one check from the facts already gathered.

## Score

Each check is worth its weight times 1.0 (ok), 0.6 (warn) or 0 (fail); skipped checks are left out and the rest
are rescaled. The verdict is **Ready** at 90 or more with nothing to check, **Not ready** if any check fails, and
**Check a few things** otherwise. The last 30 runs are kept in `console-preflight.json`; a damaged file is
restored from its backup like the other Console files.

## Where the facts come from

The microphone is read from the raw meter (before the voice chain) while the prompt is up, so the answer is about
the room and the microphone, not the effects. Every source has its own time limit: a source that does not answer is
reported as such and the rest of the check carries on, so a run always ends inside its budget (20 s).

## State and actions

See `preflight` in docs/console.md. The demo rig adds `preflight.demo_fault` (`fault`, `on?`) and
`preflight.demo_reset` for tests and the e2e suite.
