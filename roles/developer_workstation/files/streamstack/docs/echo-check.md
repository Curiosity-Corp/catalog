# Echo check

Is a speaker bleeding into your microphone? The people on the other end hear themselves, or you twice, and you never
find out. The echo check plays a quiet sweep on one output, listens on the raw microphone and tells you in words.

Audio page, **Output** section, the **Echo check** card: pick an output and press **Check for bleed**
(action `echo.check`, `streamstack echo` is not a command: this is a Console feature). A few seconds later:

- "No bleed from Dell Monitor: the microphone did not hear the sound (below -65 dB)."
- "Slight bleed from ... at -52 dB: not audible to the other side."
- "Bleed at -34 dB from Dell Monitor: use headphones or lower the volume."
- "A feedback loop through ...: the sound comes back about 150 ms later. Turn off the monitor mix or the effects
  monitoring that feeds this output into the microphone."
- "The microphone delivered digital silence, so ... could not be measured." A silent recording is never reported as
  "no bleed".

Bands, by coupling (how far below the played sweep the microphone heard it): strong at -40 dB or louder, moderate to
-50, slight to -60, none below. The last result for each output is kept (`console-echo.json`) and listed under the card.

## Rules

- **Never during a call.** The check refuses to start while a call is on (call awareness, docs/call-awareness.md,
  `rig.call.active()`), checks again right before it plays, and a call that starts mid-check stops the sound at once.
- **It tests only the output you chose** (`pw-play --target`). The default output, every volume and every route stay
  where they are. The sweep is -24 dBFS: audible, never loud.
- **It never unmutes anything.** A muted or zero-volume output, a muted, missing or stalled microphone, a running output
  test or another running check is refused with the reason (HTTP 409), and the button says why.
- It records the raw microphone (`parecord`), so the voice chain does not hide the bleed. Both `pw-play` and `parecord`
  must be installed.

## The link (network monitor and SRT stats)

A bad link must neither be blamed for a speaker's bleed nor hide it, so the check reads the network monitor
(docs/network-monitor.md) and every running destination (docs/stream.md) at the start and again at the end, and names
what it found in the answer. It is a caveat, not a refusal: the sweep is played and recorded on this machine, so a bad
link does not change the measurement, only what the other side can be hearing.

- the network is `degraded`, `poor` or `offline` (and the monitor is on), with its cause;
- a running (not recording) destination is stalling (`backoff` or `failed`);
- a running SRT destination loses 10 % or more of its probes, or has a round trip of 400 ms or more.

Examples: "No bleed from ... The link is not clean (the network is degraded (weak Wi-Fi)), so what the other side hears
may come from it, not from echo: this check only clears the speaker." A bleed or feedback verdict gets "The link is
also not clean (...): delay or choppiness on the other side may come from it as well as from the bleed." A clear
verdict on a bad link toasts as a warning. Each result carries `link: {level: "ok" | "caveat", reasons: [...]}`, kept
with the remembered results; a missing signal (monitor off, no SRT stats) is not judged. A silent recording stays
"not measured" with no caveat.

## How it measures

`streamstack.echocheck` is pure Python (the package has no runtime dependencies): a 1 s logarithmic sweep from 250 to
2500 Hz, the recording brought down to 8 kHz, an FFT cross-correlation, and from the best match the coupling gain (least
squares), the delay (less the 0.3 s the recorder runs before the sweep) and a verdict. A sweep counts as heard only at 7
times the correlation's own noise, so a quiet room is not mistaken for bleed. A **feedback loop** is a clean second
copy of the sweep at least 80 ms after the first and at least 5 % of its size (a monitor mix or an effects chain that
sends the output back in); a room's reverb is smeared, not a clean peak, so it is not called a loop. The delay includes
the player's start-up: read it to a few ms, it is not a speaker distance.

## Demo rig and tests

The demo rig simulates the loopback and measures it with the same DSP: speakers, HDMI monitors and interfaces bleed at
-34 dB, headphones and headsets do not. `echo.demo` scripts `coupling_db` (or null), `delay_ms`, `loop_gap_ms` and
`noise_db`; `echo.demo_reset` clears everything. See tests/test_echocheck.py, tests/test_console_echocheck.py and
tests/e2e/test_page_echo.py.
