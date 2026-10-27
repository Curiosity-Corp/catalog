# Voice denoise: DeepFilterNet or RNNoise (issue #25)

The neural denoise is one stage of the StreamStack voice chain (`docs/audio-chain.md`). The chain runs it as a LADSPA
plugin inside StreamStack's own PipeWire filter-chain; there is no other program in the path. Two engines are
supported. **DeepFilterNet (3) is the default** when its plugin is installed; **RNNoise** is the fallback and is used
when only its plugin is installed. With neither plugin the stage is off and the noise gate alone works.

| | DeepFilterNet | RNNoise |
| --- | --- | --- |
| LADSPA file | `libdeep_filter_ladspa.so` (label `deep_filter_mono`) | `librnnoise_ladspa.so` (label `noise_suppressor_mono`) |
| Algorithmic delay | about 40 ms | about 10 ms |
| Real-time factor (laptop i5-8250U, paper) | 0.11 | 0.03 |
| Character | cleaner on keyboards, fans and rooms | light, can pump on difficult noise |

Neither uses the GPU, so this does not touch the Arc work (#19).

## Using it

* **Console, Audio page, Chain:** the Noise removal stage has an engine pad (the engines that are installed) and the
  **Attenuation limit** (0 to 100 dB, default 100), how much noise DeepFilterNet may take out: lower leaves more of
  the room in and sounds more natural. The command palette has "Noise removal: RNNoise" and
  "Noise removal: DeepFilterNet".
* **Preset:** `StreamStack Voice DeepFilterNet` is `StreamStack Voice` with the denoiser pinned to DeepFilterNet.
  `streamstack voice "StreamStack Voice DeepFilterNet"` loads it.
* **API:** `audio.denoise` with `engine` and/or `atten_limit_db`, and `audio.chain` for every other stage
  (see `docs/console.md`).

## Installing the plugins

* DeepFilterNet: put a Linux build of `libdeep_filter_ladspa.so` from the
  [DeepFilterNet releases](https://github.com/Rikorose/DeepFilterNet/releases) (v0.5.6 at the time of writing; MIT or
  Apache-2.0) into `/usr/local/lib/ladspa/` under exactly that name. The plugin is looked up on `LADSPA_PATH`, then
  `/usr/local/lib/ladspa`, `/usr/lib/ladspa`, `/usr/lib/x86_64-linux-gnu/ladspa` and `/usr/lib64/ladspa`.
* RNNoise: `noise-suppression-for-voice` (`librnnoise_ladspa.so`).
* Packaging both is tracked with the catalog (Curiosity-Corp/catalog), as for the blur plugin.

## Measured

On an isolated PipeWire instance (`tools/bench_audio_chain.py denoise`, synthetic speech with a fan, hiss and key
clicks mixed in; level of the 20 ms frames in dB full scale, floor = the quietest decile, speech = the loudest):

| Path | Noise floor | Gaps | Speech vs clean | Correlation with clean speech |
| --- | --- | --- | --- | --- |
| Noisy input | -37.3 | -37.2 | | 0.962 |
| Passthrough (denoise off) | -37.4 | | -0.5 dB | 0.950 |
| DeepFilterNet alone | -57.6 | -58.9 | -0.9 dB | 0.976 |
| DeepFilterNet, 30 dB limit | -56.5 | -57.5 | -0.8 dB | 0.977 |
| Whole default chain | -69.3 | -65.1 | makeup gain applied | not meaningful (EQ and compressor change the waveform) |

DeepFilterNet takes the floor down by 20 dB and the gaps by 22 dB while the speech level is within 1 dB of the
clean signal and the waveform moves closer to the clean speech (correlation 0.962 to 0.976). RNNoise was not measured:
its plugin is not installed on this machine.

## Not verified

* How DeepFilterNet sounds on the Wave XLR or a Scarlett 2i2 against RNNoise.
* The default Attenuation limit of 100 is the plugin's own default, not a tuned value.
* The RNNoise path end to end (plugin absent here); the graph for it is generated and unit-tested.
