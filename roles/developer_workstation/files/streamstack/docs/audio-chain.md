# The voice chain: StreamStack Mic

StreamStack owns the whole path from the microphone to the program audio. The processing that used to live in Easy
Effects is a PipeWire filter-chain (`libpipewire-module-filter-chain`) that StreamStack generates, hosts and adjusts
itself. Nothing sits in between: no Easy Effects, no other mixer. Applications (Teams, Zoom, the stream
service) pick one new microphone, **StreamStack Mic**.

```
microphone -> high-pass x2 -> gate -> neural denoise -> de-esser -> EQ (4 bands) -> compressor -> gain -> limiter -> StreamStack Mic
          \________________________ dry path, delayed to match ________________________/   (A/B bypass mixes the two)
```

| Stage | What it is | Plugin |
| --- | --- | --- |
| High-pass | two cascaded biquads (4th order), corner 20 to 300 Hz | builtin |
| Noise gate | closes below a threshold, with a range so it never goes fully dead | `ZamGate` (zam-plugins) |
| Noise removal | DeepFilterNet 3, RNNoise as the fallback (`docs/voice-denoise.md`) | `libdeep_filter_ladspa` / `librnnoise_ladspa` |
| De-esser | dynamic cut around 6.5 kHz | `ZamDynamicEQ` |
| EQ | bass (low shelf 120 Hz), mud (bell 300 Hz), presence (bell 3.5 kHz), air (high shelf 9 kHz) | builtin biquads |
| Compressor | threshold, ratio, attack, release, knee | `ZamComp` |
| Gain | makeup gain plus the loudness leveller's gain, one node | builtin |
| Limiter | the last stage: look-ahead peak limiter, ceiling default -2 dB | `ZaMaximX2` |

Each stage has an on switch; a stage that is off is transparent (its parameters are parked at neutral) instead of
being removed, so switching it does not rebuild the graph. Only changing the denoise engine, or the microphone,
changes the graph's shape: then StreamStack restarts its host process (a short gap) and sends every value again.

## How it runs

* `streamstack chain start|stop|status|print-config|run` and the Console manage one detached `pipewire -c <file>` client
  process. The user's PipeWire daemon and its configuration are never touched, and no module has to be unloaded by
  hand (the host is found again by its configuration path). The one thing it changes is the default microphone,
  described under "StreamStack Mic as the default microphone" below.
* **A deliberate stop is honoured (#145).** `streamstack chain stop` writes `audio-chain/stopped-by-user` in the state
  directory before it stops the host, and the Console's supervisor (which otherwise restarts a dead host within
  seconds) leaves the chain stopped while that file exists; the Audio and Live pages say it was stopped from the
  command line. `streamstack chain start`, `streamstack studio`, "Start studio" in the Console and the systemd unit's
  `chain run` remove the file.
* The file is `~/.local/state/streamstack/audio-chain/filter-chain.conf`. It ends with `# structure <signature>`;
  the host restarts only when that signature differs from the one it runs with.
* Live changes go to the node `streamstack_mic.capture` as `Props` params (`pw-cli set-param`), only the values that
  changed since the last push. The A/B switch is a six step crossfade over about 100 ms.
* `streamstack chain install-plugin`, `claim-default` and `release-default` are the other actions (below).
* Required plugins: `zam-plugins` (gate, de-esser, compressor, limiter). Optional: the DeepFilterNet and RNNoise LADSPA
  plugins. `streamstack doctor` and the Console say which one is missing. With neither neural plugin the denoise
  stage reports itself off.

## Rate, format and quantum per interface (issue #118)

The hardware Plan (ADR 0003) decides what the chain reads and how it runs; `src/streamstack/hardware/voice.py` turns the
Plan into one `VoiceTarget` that the Console, `streamstack chain` and `streamstack doctor` all read.

* **Microphone and monitor** are the nodes of the interface the Plan found by its USB id, never a node picked by a
  description, a card number or a name fragment. Without an interface profile the target says `generic` and uses
  PipeWire's default source (never a capture card, never StreamStack Mic). With the interface attached but its card at
  profile `off` the target says `card-off`; when the profile names a `card_profile` the Console switches the card on
  (`pactl set-card-profile`, at most once per 30 s). With no interface attached, and the rig preferring
  one (the deck studio's Scarlett 2i2), the target says `absent` (`<interface> is not enumerating on USB`) and the
  chain does not start (a running chain is left alone when an interface goes away, so a call is not cut).
* **Rate.** The graph runs at the interface's `preferred_rate` when every stage supports it (`graph.ENGINE_RATES`),
  otherwise at 48 kHz with the reason shown by `streamstack chain status` and the doctor. DeepFilterNet and RNNoise are
  48 kHz models, so a neural graph is always 48 kHz. "StreamStack Mic" stays 48 kHz mono (ADR 0004).
* **Format** (`S24_3LE` on the Wave XLR) and the **quantum** (`node.latency` of the graph's capture side, scaled to the
  graph rate) come from the profile's `[device.audio]` table (`quantum`, `card_profile`).
* **Hot-plug.** The Console re-reads the Plan after every udev change, and when the Plan and PipeWire disagree (the
  Plan's node is gone) it asks the hardware controller for a fresh probe, at most every 5 s. The host restarts only
  when the structure signature (engine, microphone, rate, quantum) changes.

**Measured on dm313red (28 threads, PipeWire 1.6.2), isolated instance, synthetic speech, three runs per row, CPU is
the filter-chain host process as a share of one core:**

| Graph | CPU | xruns | Result |
| --- | --- | --- | --- |
| 48 kHz, quantum 256, DeepFilterNet | 32.0 % | 0 | works; 57 ms through the chain |
| 48 kHz, quantum 128, DeepFilterNet | 32.4 % | 0 | works; 47 ms |
| 48 kHz, quantum 512, DeepFilterNet | 32.5 % | 0 | works; 40 ms, speech correlation 0.46 (256 gives 0.70) |
| 96 kHz, quantum 256 or 512, DeepFilterNet | | | **no audio comes out** (empty recording, both quanta) |
| 48 kHz, quantum 256, no neural stage | 6.8 % | 0 | works |
| 96 kHz, quantum 256, no neural stage | 13.5 % | 0 | works at twice the CPU |

So the Wave XLR's profile asks for 48 kHz (`preferred_rate`), whatever it offers at 96 kHz: DeepFilterNet cannot run at
96 kHz at all, and a graph without it would only double the CPU for no audible gain in a call. The system PipeWire
here also has `clock.allowed-rates = [ 48000 ]`, so the device itself cannot be taken to 96 kHz without changing the
user's PipeWire configuration, which StreamStack does not do. The quantum stays at PipeWire's default of 256: 128 gains
about 10 ms for the same CPU, and 512 lost speech quality in this synthetic test.

**The real Wave XLR** (read only, no call active, nothing linked but the idle chain input): a `pw-record` from the
source ran 23 s at quantum 256 and 20 s at quantum 128, both at 48000 Hz, `S24LE`, 1 channel (the profile's format),
wait 2 to 14 us, busy 0.2 to 2 us per cycle; the xrun counter went to 3 when the device woke and did not move
afterwards, at either quantum.

## What the stream service can rely on (Opus, 48 kHz)

The processed voice is an `Audio/Source` node:

| | |
| --- | --- |
| `node.name` | `streamstack_mic` |
| `node.description` / `media.name` | `StreamStack Mic` |
| `media.class` | `Audio/Source` |
| Format | 48000 Hz, mono, 32 bit float (`audio.rate = 48000`, `audio.channels = 1`, position `MONO`) |
| Where it reads | `chain.status()["contract"]` and `streamstack chain status` report the rate and channels found on the live node |

Opus runs at 48 kHz, so the stream service reads the node as it is: `pw-record --target streamstack_mic --rate 48000
--channels 1 --format f32` (or any client that opens the node by name) needs no resampling and no channel mixing. The chain's delay is about 30 ms with DeepFilterNet, 12 ms with RNNoise: video is delayed to match
by whoever muxes the stream. Two clients may read the node at once (a call and the stream).

## Loudness (issue #38)

The leveller is the one owner of gain after the makeup stage. It never touches the microphone's own gain or the
interface's 48 V. In the Console, Audio page, Chain: target (-14, -16 default, -23 LUFS), on/off, and the meters.

How it works (`audiochain/leveller.py`, no clock or I/O so it is unit tested directly):

* The Console already measures the chain output with its BS.1770 analyser (`console/loudness.py`); the leveller
  reads the momentary loudness and steers `auto_db`, a gain added to the makeup gain.
* **Speech gated.** Output below -50 LUFS is silence; muted, no signal and silence all **freeze** the gain where it is,
  so a noise floor is never pulled up. Between words a relative gate (8 LU below the running speech level) leaves the
  pauses out of the average; when the voice itself gets quieter the gate falls away within a second.
* It smooths the loudness the voice has **before** its own gain (measured output minus `auto_db`, 1.5 s time constant),
  so the gain it adds cannot feed back into the average: no overshoot, no ringing.
* Rate: proportional to the error, up to 4 dB/s for large errors; **within 2 dB of the target at most 0.5 dB/s**
  (the issue's slow rate); inside a +-0.5 dB band it does not move. The window is +-12 dB around the makeup gain, and
  at the end of it the state is `at-limit`.
* **True peak.** The limiter is the last stage, so no gain the leveller adds can clip.
* **Microphone guard.** The chain output is measured after the limiter, and a limiter that works hard hides how
  loud the voice is: speech 20 dB hotter reads about the same loudness, and the leveller would sit boosting. So the
  raw microphone is measured too (`mic_lufs`), and the gain is never allowed past the point where the raw level plus
  the chain's static gain plus the leveller gain sits more than 10 LU above the target; when the voice gets louder
  the gain is cut at full rate. State `capped`.
* States: `off | riding | holding | silence | muted | no-signal | at-limit | capped`, with `auto_db`, the smoothed
  level and the target in the Console state (`audio.chain.loudness`) and on the meters. The gain is saved per rig
  profile, so a microphone comes back to the gain it had.
* One owner of gain: the leveller only adds to the chain's gain node and reads the mute state; it has no way to set
  the interface's gain or 48 V, and the Auto director (a separate process that reaches the chain through
  `audiochain/control.py`) changes presets and stage values, which the leveller's own gain rides on top of, so the
  two do not fight over the same control.

Deviation from the issue text: the 0.5 dB/s limit applies near the target (within 2 dB). Far from it the leveller moves
faster (up to 4 dB/s), because a 20 dB step has to settle inside 10 seconds and 0.5 dB/s would take 40.

## Measured on an isolated PipeWire instance

`tools/bench_audio_chain.py` starts its own PipeWire and WirePlumber under a throwaway runtime directory, plays
synthetic speech into the chain and records the output; it tears everything down on exit. It needs `numpy` and `scipy`
(`uvx --with numpy --with scipy python tools/bench_audio_chain.py all`). Run on this machine (PipeWire 1.6.2, DeepFilterNet
plugin present, RNNoise plugin absent):

**Denoise** (synthetic speech with fan, hiss and key clicks): see the table in `docs/voice-denoise.md`. DeepFilterNet
lowers the noise floor from -37.3 to -57.6 dB and the gaps from -37.2 to -58.9 dB, keeps the speech level within 1 dB
of the clean signal and raises the correlation with the clean speech from 0.962 to 0.976. The whole default chain
reaches a floor of -69.3 dB.

**Loudness** (speech at a steady input level, then a 20 dB step up, then a 20 dB step back down, target -16 LUFS,
compressor off so the closed loop is what is measured): the gain settles (within 1.5 dB of where it ends) in 4 to 9.7
seconds after each step in repeated runs; the final loudness (BS.1770 gated) was -16.3 to -17.0 LUFS (target -16 +-1);
the true peak of the output stayed at -2.06 dBTP, under the -1 dBTP limit; the gain stayed exactly where it was through
silence and through a muted microphone. The input levels were chosen so that both targets are reachable inside the
+-12 dB window: the chain loses about 10 dB between a raw speech level and the limiter (gate, denoise and EQ), so a
voice at -22 LUFS or quieter pins the gain at the top of the window and ends 0.5 to 1 LU below target.

**Setting controls while a 1 kHz tone plays** (about 120 sets per case, deviation = peak of what remains after the
best 1 kHz sinusoid is removed, idle baseline -89 dBFS, tone -14 dBFS):

| Control | Deviation | |
| --- | --- | --- |
| Setting the values the chain already has | -89 dBFS | the control path itself is silent |
| Makeup/leveller gain | -72 dBFS | inaudible |
| EQ band, 3 dB steps | -42 dBFS | a faint tick |
| High-pass corner | -26 to -29 dBFS | an audible tick |

Moving a biquad's corner leaves a click, and the high-pass is the worst because its state holds the whole signal. So the
runtime sends a high-pass corner once its value has been still for 0.3 s (a drag is one tick, not a stream). The
claim "no dropouts" holds for the gain, the A/B mix, and the stage parameters that scale the signal; the high-pass
and the EQ are not click free and the ZAM stage parameters were not measured individually.

## Gain reduction meter

The Console meters show input, output and **gain reduction**. The reduction is an estimate from the compressor and
limiter static curves and the measured levels (`chain.estimate_reduction`), not read from the plugins (LADSPA plugins
used here have output ports for reduction, but PipeWire's filter-chain does not export them as controls).

## Presets

`StreamStack Voice` (default), `Minimal`, `DeepFilterNet`, `Warm`, `Bright`, `Deep` are shipped as JSON in
`src/streamstack/data/audio/presets/`; each stores only what differs from the defaults. User presets are saved
under `~/.local/state/streamstack/audio-chain/presets/`. The preset names of earlier versions load unchanged.

## Migration from Easy Effects

On the first Console start after upgrading, `audiochain/migrate.py` reads the presets a rig made in Easy Effects
(`~/.local/share/easyeffects/input`, read only) and turns each one the chain does not already ship into a StreamStack
preset: the three EQ bands it mapped and the denoise engine carry over. The voice preset the Console had chosen stays
chosen. A marker file makes it run once. Easy Effects itself is no longer started, configured or required.

## DeepFilterNet3 plugin (issue #91)

`libdeep_filter_ladspa.so` is built in CI from the pinned upstream release (DeepFilterNet v0.5.6, tag tarball sha256
pinned in `images/dfn-ladspa/Dockerfile`, Rust 1.75.0 because the upstream lockfile predates newer compilers):

* `dfn-ladspa-image` builds it with BuildKit on K8s (`images/dfn-ladspa`), runs a licence gate over the linked crates
  (permissive licences only), writes a CycloneDX SBOM that includes the embedded model, loads the plugin and runs 100
  blocks through it, and pushes the image to Harbor (`apps/streamstack-dfn-ladspa`).
* `dfn-ladspa-package` extracts the file and publishes it, with `SHA256SUMS`, the SBOM and the licences, to the
  project's generic package registry as `streamstack-ladspa/<upstream>-<revision>` (currently `0.5.6-1`). A published
  version is never overwritten: the same bytes are a no-op, different bytes fail the job.
* `dfn-ladspa-verify` installs it the way a user does and runs the smoke test again.
* `streamstack chain install-plugin` downloads it, verifies the checksum, that it is an ELF file and that it exports
  `ladspa_descriptor` with the `deep_filter_mono` label, and installs it atomically to
  `~/.local/share/streamstack/ladspa/`. The chain looks in `LADSPA_PATH`, then that directory, then the system
  directories. `streamstack doctor` warns while it is missing and prints the same command as the fix.
  `plugin.PINNED_SHA256` pins the checksum of the first CI build (0.5.6-1, `327bca072d05...`) in the client as well; a new revision of the package needs a new pin.

## StreamStack Mic as the default microphone

While the chain runs, `default.configured.audio.source` (PipeWire metadata, persisted by WirePlumber) is set to
`streamstack_mic`, so an application that opens "Default" gets the processed voice. The previous value is saved in
`~/.local/state/streamstack/audio-chain/default-source.json` and restored on stop.

* Claimed once per run. If the person picks another default while the chain runs, it is left alone and the release
  does not overwrite it.
* Never claimed when no microphone is configured (the chain would read itself).
* `streamstack chain start`/`stop`, the Console and the systemd unit (`ExecStartPost=-streamstack chain claim-default`,
  `ExecStopPost=-streamstack chain release-default`, which also runs after a crash) do it. `manage_default=False`
  switches it off for a caller that must not touch the default.
* If the host dies without a release, WirePlumber moves "Default" to the best remaining source by itself. The
  doctor/micwatch warnings about this are owned by the mic watch work, not here.

## Verified on an isolated instance (default microphone)

`uvx --with numpy --with scipy python tools/bench_audio_chain.py default` (own daemon and WirePlumber, fake raw
microphone, nothing on the live desk). Result: claim -> apps on "Default" are linked to `streamstack_mic`; a second
claim changes nothing; after `kill -9` of the host WirePlumber moves "Default" off the dead node (to `iso_in` on that rig,
whose only microphone is a virtual source ranked below the private sink's monitor); a restarted host is picked up
again; release restores the raw microphone; a default chosen by hand while running is not overwritten.
The plugin installer, checksum failure paths and the CI pin consistency are unit tested; the Dockerfile and CI jobs were
not run locally (CI only).

## Not verified live

### To check on the Wave XLR (rollout)

1. `streamstack chain install-plugin` (package `streamstack-ladspa/0.5.6-1` is published; CI pipeline 95914 built, published and verified it), then `streamstack doctor`.
2. `streamstack chain start`; `pactl get-default-source` must print `streamstack_mic`; Teams on "Default" must get the
   processed voice.
3. `streamstack chain stop`; the default must be the Wave XLR again. Also `kill -9` the host and check that "Default"
   lands on the Wave XLR (WirePlumber ranks a real USB source above monitors; only proven on the private rig).
4. Unplug and replug the Wave XLR while the chain runs; check xruns on the real USB clock; compare DeepFilterNet and
   RNNoise by ear.

* **deskmeet1.** The owner chose a Focusrite Scarlett 2i2 as its DAC (2026-10-24), but it is not enumerating on USB
  (every card at profile `off`, Dummy Output): the doctor warns with the fix (an owner action: re-seat it in a direct
  rear USB port with a short known-good cable) and the chain declines to start. Nothing about the 2i2's rate, format,
  quantum or hot-plug there was measured; once it enumerates, measure them and set `preferred_rate`, `quantum` and, if
  its card sits at `off`, a `card_profile` in its `[device.audio]` table in `audio-interfaces.toml`.
* The 96 kHz and quantum numbers above are from synthetic speech on an isolated instance; the real Wave XLR was only
  read through a plain capture, not through the chain, so the chain's xruns on the USB clock itself were not measured.
* Nothing was loaded into the running PipeWire of the workstation: every measurement above is on the isolated instance
  (own daemon, own WirePlumber, torn down afterwards). The real microphones (Wave XLR, Scarlett 2i2), and real
  Teams/Zoom/stream clients reading StreamStack Mic, were not tried.
* The RNNoise graph is generated and unit tested, but its plugin is not installed here, so it never ran.
* The delays of the A/B dry path (30.5 ms DeepFilterNet, 12.5 ms RNNoise, 7.5 ms without a neural stage) are nominal
  values, not measured per engine.
* The leveller's events are not yet drawn in the Auto timeline.
* Catalog follow-up (not edited here): stop installing and autostarting Easy Effects; install `zam-plugins` and the
  DeepFilterNet and RNNoise LADSPA plugins.

## The capture is pinned to the Plan's microphone (#139)

The generated config sets `target.object` (the Plan's microphone node name), `node.dont-fallback = true` and
`node.linger = true` on the capture side. Without them WirePlumber links the stream to the *default* source when the
named node does not exist yet, which is how StreamStack Mic once read the Cam Link's silent HDMI audio. With them the
capture waits unlinked until the microphone appears, then links. The Console additionally reads `pw-dump` every second:
a capture linked to any other source, or unlinked for 10 s while the microphone is present, rebuilds the host (at most
every 30 s). `streamstack doctor` fails `voice-chain-source` for the same condition and `voice-chain-silence` when
StreamStack Mic delivers only zeros.
