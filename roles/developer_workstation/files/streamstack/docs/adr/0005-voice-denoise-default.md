# ADR 0005: DeepFilterNet3 stays the default voice denoiser; DPDFNet is the candidate if the chain ever gets a bridge

Status: accepted (2026-10-27). Issue #131, milestone "Studio audio in-process".

## Context

The voice chain (`docs/audio-chain.md`) runs its neural denoise as a LADSPA plugin inside StreamStack's own PipeWire
filter-chain. DeepFilterNet3 is the default and RNNoise the fallback (issue #25). DPDFNet (Ceva-IP, Apache-2.0,
ONNX, 48 kHz) appeared as a third option. The question: which should be the default, with evidence, on our corpus and
this host (i5-class laptop CPU, PipeWire 1.6.2, quantum 256)?

## Method

`tools/bench_denoise.py` (developer tool; `fetch`, `prepare`, `run`, `score`, `all`). Every download is pinned by URL,
size and SHA-256. The corpus is 323 s at 48 kHz: 60 VoiceBank+DEMAND test pairs (CC BY 4.0, 16 kHz band-limited),
the same clean speech mixed with real 48 kHz DEMAND recordings at 0, 5 and 10 dB (CC BY 4.0), and 16 mixes with a
speech-free recording of the Wave XLR room (private, not committed) at 5 and 15 dB. The LADSPA engines run through
the product's generated filter-chain on a private PipeWire instance, so the path measured is the shipped one. DPDFNet
runs through `StreamEnhancer` (ONNX Runtime, one thread) on 10 ms blocks. Outputs are aligned to the reference and
scored with PESQ wideband, STOI, SI-SDR and DNSMOS P.835 (non-intrusive, run on the output alone). PESQ is licensed for
research use, so it is a dev-only dependency; STOI, SI-SDR and DNSMOS are permissive and could run in CI.

Sanity: the clean reference scores PESQ 4.64, STOI 1.00, DNSMOS OVRL 3.29 on the same set; the noisy input scores
PESQ 1.87, SI-SDR 7.8 dB, OVRL 2.70.

## Results (all 130 utterances)

| Engine | PESQ-WB | STOI | SI-SDR dB | DNSMOS SIG | BAK | OVRL | Speech level vs clean |
|---|---|---|---|---|---|---|---|
| Noisy input | 1.87 | 0.914 | 7.8 | 3.34 | 3.12 | 2.70 | +0.6 dB |
| RNNoise | 2.42 | 0.891 | 9.4 | 3.43 | 4.05 | 3.16 | -0.3 dB |
| DeepFilterNet3 | 2.92 | 0.912 | 13.3 | 3.53 | 4.09 | 3.26 | -0.2 dB |
| DPDFNet-2 (48 kHz hr) | 2.89 | 0.929 | 18.6 | 3.46 | 4.14 | 3.23 | -0.2 dB |
| DPDFNet-8 (48 kHz hr) | 2.88 | 0.931 | 18.6 | 3.44 | 4.13 | 3.21 | -0.2 dB |

The room-noise subset (our own microphone) in OVRL: noisy 2.53, RNNoise 3.05, DeepFilterNet3 3.18, DPDFNet-2 3.14,
DPDFNet-8 3.14.

Cost, one core, real-time stream (this host):

| Engine | CPU (share of one core) | Mic-to-output delay in the real chain, quantum 256 | Block compute p99 |
|---|---|---|---|
| RNNoise (LADSPA) | 10.9 % | 158 ms (passthrough graph 26 ms) | n/a |
| DeepFilterNet3 (LADSPA) | 37.2 % | 44.6 ms (about +19 ms over the passthrough graph) | n/a |
| DPDFNet-2 (ONNX) | 14.9 % (RTF 0.149) | 40 ms algorithmic | 3.5 ms (3 of 32359 blocks over 10 ms) |
| DPDFNet-8 (ONNX) | 38.5 % (RTF 0.385) | 40 ms algorithmic | 5.7 ms (22 blocks over 10 ms) |

## Decision

1. **DeepFilterNet3 stays the default; no change to the chain.** It has the best PESQ and DNSMOS OVRL and SIG on the
   whole set and on our room noise. DPDFNet-2 is within 0.03 PESQ and 0.03 OVRL (inside the noise of a 130-clip set),
   is ahead on STOI and SI-SDR and costs 2.5 times less CPU, so on the numbers alone it is a genuine peer, not a loser.
2. **What decides it is the integration cost, not quality.** PipeWire 1.6.2's filter-graph loads builtin and LADSPA
   plugins only. DPDFNet would need a bridge process (capture, ONNX, playback), which is exactly the extra process
   the chain was built to remove (ADR 0004, `docs/audio-chain.md`): another node to supervise, another source of the
   wrong-link and silence failures of #139, and no clock sharing with the graph. The CPU saving (about 22 points of one
   core) does not pay for that.
3. **Revisit if** a LADSPA or LV2 build of DPDFNet exists, PipeWire gains an ONNX filter plugin, or the CPU headroom
   of a target machine becomes the constraint. DPDFNet-2 would be the variant (DPDFNet-8 buys nothing measurable here
   for 2.6 times the CPU). The harness reruns in one command.
4. **RNNoise stays the fallback**, but its cost is not what the docs say. In the real chain at quantum 256 it adds
   about 130 ms, not the 12.5 ms in `graph.CHAIN_DELAY_S`, and its quality is clearly below the other two (SI-SDR 9.4
   against 13.3 and above). The delay figure was not changed here: it is used for the latency report and must be
   re-validated across quanta first (follow-up: measure at 128 and 512 and correct the table).
5. The measured DeepFilterNet3 delay (about 19 ms above the passthrough graph) is below the 30.5 ms in
   `CHAIN_DELAY_S`; the constant is conservative, not wrong.

## Not done, on purpose

* No listening test. The metrics are objective; whether DeepFilterNet3 or DPDFNet sounds better on the Wave XLR is
  still an open human judgement, and the default is unchanged so nothing regresses.
* DNSMOS is non-intrusive and a poor judge of artefacts such as pumping; PESQ and SI-SDR are intrusive but the
  reference is VoiceBank-style speech. Read the table as a ranking with error bars, not as absolute scores.
* The live chain was not touched; all runs used an isolated PipeWire instance.

## Licences

DeepFilterNet3 MIT or Apache-2.0; DPDFNet Apache-2.0; RNNoise (noise-suppression-for-voice 1.10) GPL-3.0, used as a
dev tool only and fetched at run time, never vendored; PESQ research-use, dev-only; VoiceBank+DEMAND and DEMAND CC BY
4.0; DNSMOS model CC BY 4.0. The room recording used for the `room` set is private and not in the repository.
