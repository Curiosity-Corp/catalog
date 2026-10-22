# Voice denoise: RNNoise or DeepFilterNet (issue #25)

The voice chain removes background noise with one Easy Effects plugin. StreamStack ships it in two forms and keeps
**RNNoise the default** because it has the lowest delay.

| | RNNoise | DeepFilterNet |
| --- | --- | --- |
| Easy Effects name | Noise reduction (`rnnoise#0`) | Deep noise remover (`deepfilternet#0`) |
| Algorithmic delay | about 10 ms | about 40 ms |
| Real-time factor (laptop i5-8250U, paper) | 0.03 | 0.11 |
| Character | light, can pump on difficult noise | cleaner on keyboards, fans and rooms |
| Needs | built in | the `libdeep_filter_ladspa.so` LADSPA plugin |

Neither uses the GPU, so this does not touch the Arc work (#19).

## Using it

* **Console, Audio page, Tone card:** the A/B pad "Noise removal engine" swaps RNNoise and DeepFilterNet inside the
  voice preset you chose (tone, pitch and preset stay; the Custom pill lights until you reset). The fifth knob,
  **Attenuation limit** (0 to 100 dB, default 100), is how much noise DeepFilterNet may take out: lower leaves more of
  the room in and sounds more natural. It only acts while DeepFilterNet is selected. The command palette has
  "Noise removal: RNNoise" and "Noise removal: DeepFilterNet".
* **Preset:** `StreamStack Voice DeepFilterNet` is `StreamStack Voice` with the denoiser swapped, nothing else.
  `streamstack voice "StreamStack Voice DeepFilterNet"` loads it.
* **API:** `audio.denoise` with `engine` and/or `atten_limit_db` (see `docs/console.md`).

## What was verified about Easy Effects 8.1.2 (Ubuntu package `8.1.2+ds-1`)

Read-only, nothing loaded into a running Easy Effects:

* `dpkg -L easyeffects` lists only the binary, desktop file and metainfo; the effects are compiled in, so the check
  is on the binary. `strings /usr/bin/easyeffects` shows the `DeepFilterNet` class, the label "Deep Noise Remover",
  and the preset keys `attenuation-limit`, `min-processing-threshold`, `max-erb-processing-threshold`,
  `max-df-processing-threshold`, `min-processing-buffer` and `post-filter-beta`.
* Ranges and defaults are from `src/contents/kcfg/easyeffects_db_deepfilternet.kcfg` at tag v8.1.2 (attenuation limit
  0 to 100 dB default 100, thresholds -15 to 35 with -10, 30 and 20, buffer 0 to 10, post filter beta 0 to 0.05
  default 0.02); `tests/test_data.py` checks the shipped preset against them.
* **The effect is a wrapper around a LADSPA plugin.** The binary loads `libdeep_filter_ladspa.so` through
  `LADSPA_PATH` and `/usr/local/lib/ladspa`, `/usr/lib/x86_64-linux-gnu/ladspa`, `/usr/lib/ladspa`,
  `/usr/lib64/ladspa`, and carries the message "libdeep_filter_ladspa is not installed". The Ubuntu package does not
  ship that library and it was not on this machine, so on a stock install the effect does nothing. StreamStack
  therefore disables the DeepFilterNet pad and preset, with that reason, until the file is found.

## Installing the plugin

Put a Linux build of `libdeep_filter_ladspa.so` from the
[DeepFilterNet releases](https://github.com/Rikorose/DeepFilterNet/releases) (v0.5.6 at the time of writing; MIT or
Apache-2.0) into `/usr/local/lib/ladspa/` under exactly that name, then restart Easy Effects. The exact release asset
name was not verified. Packaging it is tracked with the catalog (Curiosity-Corp/catalog), as for the blur plugin.

## Not verified

* How DeepFilterNet sounds on the Wave XLR or a Scarlett 2i2 against RNNoise, and the real added delay in the
  PipeWire graph.
* The default Attenuation limit of 100 is the plugin's own default, not a tuned value.
* The `set_property` socket path for `attenuation-limit` is not used: the limit is applied through the managed live
  preset like the tone, because the property naming there was not verified.
