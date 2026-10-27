#!/usr/bin/env python3
"""Voice denoise bake-off: RNNoise vs DeepFilterNet3 vs DPDFNet (issue #131). Developer tool, not shipped.

One corpus of noisy speech with known clean references is run through every engine, and each output is scored with
objective metrics. The two LADSPA engines run through StreamStack's own generated filter-chain (denoise stage only) on
a private PipeWire instance (`bench_audio_chain.Rig`), so what is measured is the path the product ships. DPDFNet runs
through its `StreamEnhancer` (ONNX Runtime, one CPU thread) fed 10 ms blocks, the way a bridge process would.

Corpus (all at 48 kHz mono, built by `prepare` from pinned downloads):

* `vbd`    VoiceBank+DEMAND test pairs (public, CC BY 4.0; 16 kHz, so band-limited to 8 kHz, upsampled to 48 kHz).
* `demand` the same clean speech mixed with real 48 kHz DEMAND recordings (office with typing, kitchen, living room)
           at 0, 5 and 10 dB SNR.
* `room`   the same clean speech mixed with a speech-free recording of the real microphone's own room noise
           (`--room-noise FILE`, optional) at 5 and 15 dB SNR.

Metrics per utterance, computed at 16 kHz after aligning the output to the reference (the engines' delay is measured
from the whole stream, then removed): PESQ wideband (ITU-T P.862.2; dev-only, the `pesq` wheel), STOI, SI-SDR, and
DNSMOS P.835 SIG, BAK and OVRL (Microsoft's ONNX model, non-intrusive, run on the output alone). Cost per engine: CPU
as a share of one core while streaming in real time, mic-to-output delay and, for DPDFNet, the per-block compute time.

    uvx --with numpy --with scipy --with soundfile --with pyarrow --with onnxruntime --with dpdfnet \\
        --with pesq --with pystoi python tools/bench_denoise.py all --work /var/tmp/denoise131

Needs ffmpeg-free Python only for `prepare`/`score`; `run` additionally needs pipewire, wireplumber and the LADSPA
plugins of the chain (`streamstack chain install-plugin`; RNNoise's `librnnoise_ladspa.so` is unpacked from the pinned
noise-suppression-for-voice release into the work directory and found through LADSPA_PATH, nothing is installed).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any

import numpy as np
import scipy.signal as dsp

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

SR = 48000
SR16 = 16000
QUANTUM = 256  # node.latency of the voice chain on the Wave XLR and Scarlett profiles (frames at 48 kHz)
GAP_S = 0.3  # silence-free spacer: the next clip simply follows, a short ramp avoids a click at the joint
MODELS = ("dpdfnet2_48khz_hr", "dpdfnet8_48khz_hr")
ENGINES = ("rnnoise", "deepfilternet", *MODELS)


class BenchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str
    licence: str


# Pinned by URL, size and SHA-256 (the same discipline as engine/segment.py): a download that differs is refused.
DPDFNET_COMMIT = (
    "dd6818d00f50c836fed43a6243ebe49116de5964"  # huggingface.co/Ceva-IP/DPDFNet, model card licence apache-2.0
)
DNSMOS_COMMIT = "82f1b17e7776a43eee395d0f45bae8abb700ad00"  # github.com/microsoft/DNS-Challenge, CC-BY-4.0


def _assets(*items: Asset) -> dict[str, Asset]:
    return {a.name: a for a in items}


ASSETS: dict[str, Asset] = _assets(
    Asset(
        "vbd-test.parquet",
        "https://huggingface.co/datasets/DynamicSuperb/SuperbSE_VoiceBankDEMAND-Test/resolve/main/data/test-00000-of-00001.parquet",
        63982214,
        "78fc3d36c8b6c93bf968ea4ea565cb88b93e3f54fb2ecd1421cb654c00b44820",
        "VoiceBank+DEMAND test set, CC BY 4.0 (Valentini-Botinhao 2016); 200 pairs at 16 kHz",
    ),
    Asset(
        "dns-sig-bak-ovr.onnx",
        f"https://raw.githubusercontent.com/microsoft/DNS-Challenge/{DNSMOS_COMMIT}/DNSMOS/DNSMOS/sig_bak_ovr.onnx",
        1157965,
        "269fbebdb513aa23cddfbb593542ecc540284a91849ac50516870e1ac78f6edd",
        "DNSMOS P.835 model, Microsoft DNS-Challenge repository, CC BY 4.0",
    ),
    Asset(
        "linux-rnnoise.zip",
        "https://github.com/werman/noise-suppression-for-voice/releases/download/v1.10/linux-rnnoise.zip",
        36831356,
        "811390b6eb6e28dde023c70590c74d26e74ebb2e595bcf4b95af2341db160e99",
        "noise-suppression-for-voice v1.10 (GPL-3.0; used here as a developer tool only, never shipped)",
    ),
    Asset(
        "dpdfnet2_48khz_hr.onnx",
        f"https://huggingface.co/Ceva-IP/DPDFNet/resolve/{DPDFNET_COMMIT}/onnx/dpdfnet2_48khz_hr.onnx",
        10493337,
        "7f0575a5cec0ba4ffd8f8bd657e06d007e4ccdd955d76faab922b9d3291dc14b",
        "DPDFNet-2 48 kHz, Apache-2.0 (model card)",
    ),
    Asset(
        "dpdfnet8_48khz_hr.onnx",
        f"https://huggingface.co/Ceva-IP/DPDFNet/resolve/{DPDFNET_COMMIT}/onnx/dpdfnet8_48khz_hr.onnx",
        14857107,
        "7b3afbb260a08fe9af3d16e3bda992971be1e7e951d1dee7c2d235f5c43f5631",
        "DPDFNet-8 48 kHz, Apache-2.0 (model card)",
    ),
    # DEMAND noise (Thiemann, Ito, Vincent 2013), CC BY 4.0, Zenodo record 1227121
    Asset(
        "OOFFICE_48k.zip",
        "https://zenodo.org/records/1227121/files/OOFFICE_48k.zip",
        277643831,
        "5e0e50f3ab7750c795b71eaf3b001a9fe1d6e5e15a95c6b036d3eefaa5ec6eaf",
        "CC BY 4.0",
    ),
    Asset(
        "DKITCHEN_48k.zip",
        "https://zenodo.org/records/1227121/files/DKITCHEN_48k.zip",
        336992458,
        "4197f1a02dbb36c4c655a11d14cc4c5dca50eae52a4abaa2c61bfca9de6be17f",
        "CC BY 4.0",
    ),
    Asset(
        "DLIVING_48k.zip",
        "https://zenodo.org/records/1227121/files/DLIVING_48k.zip",
        253978613,
        "7e2817fffa8e4b3a10e4772e25d103e7242a8b5309f81def1bc21b3ebc1f0262",
        "CC BY 4.0",
    ),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(asset: Asset, folder: pathlib.Path) -> pathlib.Path:
    """The asset's file in `folder`, downloaded if missing; refused unless it matches the pinned size and SHA-256."""
    path = folder / asset.name
    if not path.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with urllib.request.urlopen(asset.url, timeout=60) as resp, tmp.open("wb") as out:
                while block := resp.read(1 << 20):
                    out.write(block)
        except OSError as exc:
            raise BenchError(f"cannot download {asset.url}: {exc}") from exc
        tmp.replace(path)
    size, digest = path.stat().st_size, sha256_file(path)
    if size != asset.size or digest != asset.sha256:
        path.unlink()
        raise BenchError(
            f"{asset.name}: size {size} sha256 {digest} does not match the pin ({asset.size}, {asset.sha256})"
        )
    return path


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x.astype(np.float32)
    g = np.gcd(src, dst)
    return dsp.resample_poly(x, dst // g, src // g).astype(np.float32)


def read_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    import soundfile as sf

    audio, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio.astype(np.float32), int(rate)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-20))


def db(x: float) -> float:
    return 20 * np.log10(max(x, 1e-10))


def mix_at_snr(clean: np.ndarray, noise: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    """`clean` plus a random excerpt of `noise` scaled so the speech-to-noise ratio is `snr_db`."""
    if len(noise) < len(clean):
        noise = np.tile(noise, len(clean) // len(noise) + 1)
    start = int(rng.integers(0, len(noise) - len(clean) + 1))
    excerpt = noise[start : start + len(clean)]
    scale = rms(clean) / (rms(excerpt) * 10 ** (snr_db / 20))
    return (clean + excerpt * scale).astype(np.float32)


def si_sdr(ref: np.ndarray, est: np.ndarray) -> float:
    """Scale-invariant signal-to-distortion ratio in dB (Le Roux et al. 2019)."""
    ref = ref - ref.mean()
    est = est - est.mean()
    alpha = float(np.dot(est, ref) / (np.dot(ref, ref) + 1e-12))
    target = alpha * ref
    return float(10 * np.log10((np.sum(target**2) + 1e-12) / (np.sum((est - target) ** 2) + 1e-12)))


def align(out: np.ndarray, ref: np.ndarray, max_lag_s: float = 0.5) -> tuple[np.ndarray, float]:
    """(`out` shifted to line up with `ref`, its delay in seconds), from an FFT cross-correlation over the stream."""
    n = min(len(ref), len(out))
    corr = dsp.correlate(out[:n], ref[:n], mode="full", method="fft")
    lags = np.arange(-n + 1, n)
    window = np.abs(lags) <= int(max_lag_s * SR)
    lag = int(lags[window][np.argmax(np.abs(corr[window]))])
    shifted = out[lag:] if lag >= 0 else np.concatenate([np.zeros(-lag, dtype=out.dtype), out])
    return np.pad(shifted, (0, max(0, len(ref) - len(shifted))))[: len(ref)], lag / SR


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def load_vbd(parquet: pathlib.Path, count: int) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """`count` evenly spaced VoiceBank+DEMAND test pairs as (id, noisy48, clean48)."""
    import pyarrow.parquet as pq

    table = pq.read_table(parquet)
    rows = table.to_pylist()
    rows.sort(key=lambda r: r["file"])
    picks = [rows[int(i)] for i in np.linspace(0, len(rows) - 1, count)]
    pairs = []
    for row in picks:
        noisy, rate_n = read_wav_bytes(row["audio"]["bytes"])
        clean, rate_c = read_wav_bytes(row["label"]["bytes"])
        n = min(len(noisy), len(clean))
        pairs.append((row["file"], resample(noisy[:n], rate_n, SR), resample(clean[:n], rate_c, SR)))
    return pairs


def demand_noise(zip_path: pathlib.Path) -> np.ndarray:
    """Channel 1 of a DEMAND 48 kHz zip, the first 60 seconds."""
    with zipfile.ZipFile(zip_path) as archive:
        member = sorted(n for n in archive.namelist() if n.endswith("ch01.wav"))[0]
        audio, rate = read_wav_bytes(archive.read(member))
    return resample(audio[: 60 * rate], rate, SR)


def build_corpus(
    work: pathlib.Path, vbd: pathlib.Path, demand: dict[str, pathlib.Path], room: pathlib.Path | None, count: int
) -> dict[str, Any]:
    import soundfile as sf

    rng = np.random.default_rng(131)
    pairs = load_vbd(vbd, count)
    segments: list[dict[str, Any]] = []
    noisy_parts: list[np.ndarray] = []
    clean_parts: list[np.ndarray] = []
    cursor = 0

    def add(kind: str, name: str, noisy: np.ndarray, clean: np.ndarray, **extra: Any) -> None:
        nonlocal cursor
        segments.append({"set": kind, "name": name, "start": cursor, "end": cursor + len(clean), **extra})
        noisy_parts.append(noisy)
        clean_parts.append(clean)
        cursor += len(clean)

    for name, noisy, clean in pairs:
        add("vbd", name, noisy, clean)
    speech = pairs[:: max(1, len(pairs) // 8)][:8]
    for label, path in demand.items():
        noise = demand_noise(path)
        for snr in (0.0, 5.0, 10.0):
            for name, _, clean in speech[:6]:
                add(
                    "demand",
                    f"{name}-{label}-{snr:g}dB",
                    mix_at_snr(clean, noise, snr, rng),
                    clean,
                    noise=label,
                    snr=snr,
                )
    if room is not None:
        audio, rate = read_wav_bytes(room.read_bytes())
        noise = resample(audio, rate, SR)
        for snr in (5.0, 15.0):
            for name, _, clean in speech:
                add("room", f"{name}-room-{snr:g}dB", mix_at_snr(clean, noise, snr, rng), clean, noise="room", snr=snr)
    noisy_all = np.concatenate(noisy_parts)
    clean_all = np.concatenate(clean_parts)
    peak = max(float(np.max(np.abs(noisy_all))), float(np.max(np.abs(clean_all))))
    gain = 0.8 / peak if peak > 0.8 else 1.0  # one gain for both, so no clipping and SI-SDR is unaffected
    sf.write(work / "noisy.wav", noisy_all * gain, SR, subtype="FLOAT")
    sf.write(work / "clean.wav", clean_all * gain, SR, subtype="FLOAT")
    index = {"rate": SR, "seconds": cursor / SR, "segments": segments}
    (work / "corpus.json").write_text(json.dumps(index, indent=1))
    return index


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


def run_chain(engine: str, work: pathlib.Path, ladspa: pathlib.Path | None) -> dict[str, Any]:
    """The noisy corpus through StreamStack's own filter-chain with only the denoise stage on, in real time."""
    import bench_audio_chain as bench

    from streamstack.audiochain import graph

    if ladspa is not None:
        os.environ["LADSPA_PATH"] = f"{ladspa}:{os.environ.get('LADSPA_PATH', '')}"
    if engine not in graph.available_engines():
        raise BenchError(f"the {engine} LADSPA plugin is not installed")
    params = bench.stage_params(denoise_on=True)
    noisy = work / "noisy.wav"
    seconds = json.loads((work / "corpus.json").read_text())["seconds"]
    with bench.Rig() as rig:
        proc = rig.host(graph.build_config(params, engine=engine))
        ticks0, t0 = bench._cpu_ticks(proc.pid), time.monotonic()
        out = bench.run_through_chain(rig, noisy, seconds + 1.0)
        ticks1, t1 = bench._cpu_ticks(proc.pid), time.monotonic()
    hz = os.sysconf("SC_CLK_TCK")
    cpu = 100.0 * (ticks1 - ticks0) / hz / (t1 - t0)
    return {"audio": out, "cpu_percent_of_one_core": round(cpu, 1), "wall_s": round(t1 - t0, 1)}


def chain_delay_ms(engine: str, work: pathlib.Path) -> float:
    """Mic-to-output delay of the filter-chain with `engine`, measured as a loopback in one private PipeWire instance:
    the same clean speech is recorded where it enters (the feeder sink's monitor) and where it leaves (StreamStack Mic)
    by two recorders started together; the lag between the two recordings is the delay. The start skew of
    `pw-cat` against the feeder, which makes a single-recorder lag meaningless, cancels out."""
    import signal
    import subprocess

    import bench_audio_chain as bench

    from streamstack.audiochain import graph

    params = bench.stage_params(denoise_on=engine != "off")
    seconds = 12.0
    with bench.Rig() as rig:
        rig.host(graph.build_config(params, engine=engine, quantum=QUANTUM))
        out_file, in_file = rig.dir / "out.wav", rig.dir / "in.wav"
        argv = ["pw-cat", "-r", "-P", "node.name=recin node.autoconnect=false", "--rate", "48000", "--channels", "1"]
        rec_in = rig.spawn([*argv, "--format", "f32", str(in_file)], "recin.log")
        rec_out = bench.record(rig, out_file)
        rig.wait_ports("recin:input_")
        rig.link("iso_in:monitor_MONO", "recin:input_MONO")
        play = bench.feed(rig, work / "clean.wav")  # clean speech: no engine rewrites it, so the lag is unambiguous
        time.sleep(seconds)
        play.terminate()
        for proc in (rec_in, rec_out):
            proc.send_signal(signal.SIGINT)
        for proc in (play, rec_in, rec_out):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        heard, source = bench.read_wav(out_file), bench.read_wav(in_file)
    n = min(len(heard), len(source))
    _, lag = align(heard[:n], source[:n], max_lag_s=0.4)
    return lag * 1000.0


def run_dpdfnet(model: str, work: pathlib.Path) -> dict[str, Any]:
    """The noisy corpus through DPDFNet's StreamEnhancer in 10 ms blocks; CPU is the process time, one thread."""
    import dpdfnet
    import soundfile as sf

    noisy, rate = sf.read(work / "noisy.wav", dtype="float32")
    if rate != SR:
        raise BenchError(f"{work / 'noisy.wav'} is {rate} Hz, not {SR}")
    enhancer = dpdfnet.StreamEnhancer(model=model, onnx_path=asset_path(work, f"{model}.onnx"))
    session = enhancer._runtime.session
    model_ms: list[float] = []
    real_run = session.run

    class Timed:
        """Wraps the ONNX session so the model's own time per block is measured apart from the Python STFT work."""

        def __getattr__(self, name: str) -> Any:
            return getattr(session, name)

        def run(self, *args: Any, **kw: Any) -> Any:
            t = time.perf_counter()
            result = real_run(*args, **kw)
            model_ms.append((time.perf_counter() - t) * 1000)
            return result

    enhancer._runtime = type(enhancer._runtime)(**{**enhancer._runtime.__dict__, "session": Timed()})
    block = SR // 100
    block_ms: list[float] = []
    pieces: list[np.ndarray] = []
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for i in range(0, len(noisy) - block + 1, block):
        t = time.perf_counter()
        pieces.append(enhancer.process(noisy[i : i + block], sample_rate=SR))
        block_ms.append((time.perf_counter() - t) * 1000)
    pieces.append(enhancer.flush())
    cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
    audio = np.concatenate(pieces)
    audio_s = len(noisy) / SR
    return {
        "audio": audio,
        "cpu_percent_of_one_core": round(100.0 * cpu / audio_s, 1),  # streamed in real time, this is the share used
        "real_time_factor": round(wall / audio_s, 3),
        "block_ms_p50": round(float(np.percentile(block_ms, 50)), 2),
        "block_ms_p99": round(float(np.percentile(block_ms, 99)), 2),
        "block_ms_max": round(float(np.max(block_ms)), 2),
        "model_ms_p50": round(float(np.percentile(model_ms, 50)), 2),
        "model_ms_p99": round(float(np.percentile(model_ms, 99)), 2),
        "blocks_over_10ms": int(np.sum(np.array(block_ms) > 10.0)),
        "blocks": len(block_ms),
    }


def algorithmic_delay_ms(model: str, work: pathlib.Path) -> float:
    """DPDFNet's own delay: a click through a fresh StreamEnhancer, found in the output."""
    import dpdfnet

    enhancer = dpdfnet.StreamEnhancer(model=model, onnx_path=asset_path(work, f"{model}.onnx"))
    x = np.zeros(SR, dtype=np.float32)
    rng = np.random.default_rng(1)
    x[SR // 2 : SR // 2 + 480] = rng.standard_normal(480).astype(np.float32) * 0.3  # 10 ms of noise burst
    out = np.concatenate([enhancer.process(x[i : i + 480], sample_rate=SR) for i in range(0, SR, 480)])
    out = np.pad(out, (0, SR - len(out)))[:SR]
    n = SR
    corr = dsp.correlate(out[:n], x[:n], mode="full", method="fft")
    lag = int(np.arange(-n + 1, n)[np.argmax(np.abs(corr))])
    return 1000.0 * lag / SR


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

DNSMOS_POLY = {  # Microsoft DNSMOS P.835 non-personalised polynomial fit of the raw model outputs
    "sig": np.poly1d([-0.08397278, 1.22083953, 0.0052439]),
    "bak": np.poly1d([-0.13166888, 1.60915514, -0.39604546]),
    "ovr": np.poly1d([-0.06766283, 1.11546468, 0.04602535]),
}


class Dnsmos:
    WINDOW = int(9.01 * SR16)

    def __init__(self, model: pathlib.Path) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])

    def score(self, audio16: np.ndarray) -> dict[str, float]:
        x = audio16.astype(np.float32)
        while len(x) < self.WINDOW:  # the reference implementation repeats short clips up to the 9.01 s window
            x = np.concatenate([x, x])
        hop = SR16
        count = max(1, int(np.floor(len(x) / hop) - 8.01))
        rows = []
        for i in range(count):
            seg = x[int(i * hop) : int((i + 9.01) * hop)]
            if len(seg) < self.WINDOW:
                break
            raw = self.session.run(None, {"input_1": seg[np.newaxis, :]})[0][0]
            rows.append([DNSMOS_POLY[k](v) for k, v in zip(("sig", "bak", "ovr"), raw, strict=True)])
        mean = np.mean(rows, axis=0)
        return {"sig": float(mean[0]), "bak": float(mean[1]), "ovr": float(mean[2])}


def score_audio(
    index: dict[str, Any], clean: np.ndarray, noisy: np.ndarray, out: np.ndarray | None, dnsmos: Dnsmos
) -> dict[str, dict[str, float]]:
    """Per-set means of every metric for `out` (None scores the unprocessed noisy input), keyed by set then metric."""
    from pesq import PesqError, pesq
    from pystoi import stoi

    signal = noisy if out is None else out
    per_set: dict[str, list[dict[str, float]]] = {}
    for seg in index["segments"]:
        a, b = seg["start"], seg["end"]
        ref16 = resample(clean[a:b], SR, SR16)
        est16 = resample(signal[a:b], SR, SR16)
        try:
            p = float(pesq(SR16, ref16, est16, "wb"))
        except PesqError:
            continue  # no active speech found in the segment: nothing to score
        speech = np.abs(clean[a:b]) > 0.02 * np.max(np.abs(clean[a:b]))
        speech_gain = db(rms(signal[a:b][speech])) - db(rms(clean[a:b][speech])) if speech.any() else 0.0
        row = {
            "pesq_wb": p,
            "stoi": float(stoi(ref16, est16, SR16, extended=False)),
            "si_sdr": si_sdr(ref16, est16),
            "speech_level_vs_clean_db": float(speech_gain),
            **{f"dnsmos_{k}": v for k, v in dnsmos.score(est16).items()},
        }
        per_set.setdefault(seg["set"], []).append(row)
        per_set.setdefault("all", []).append(row)
    return {
        name: {k: round(float(np.mean([r[k] for r in rows])), 3) for k in rows[0]} | {"n": len(rows)}
        for name, rows in per_set.items()
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> int:
    folder = pathlib.Path(args.work) / "assets"
    if not ASSETS:
        raise BenchError("no pinned assets: run with --print-pins after downloading by hand")
    for asset in ASSETS.values():
        print(f"{asset.name}: {fetch(asset, folder)} ({asset.licence})")
    return 0


def asset_path(work: pathlib.Path, name: str) -> pathlib.Path:
    return fetch(ASSETS[name], work / "assets")


def cmd_prepare(args: argparse.Namespace) -> int:
    work = pathlib.Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    demand = {
        "office": asset_path(work, "OOFFICE_48k.zip"),
        "kitchen": asset_path(work, "DKITCHEN_48k.zip"),
        "living": asset_path(work, "DLIVING_48k.zip"),
    }
    room = pathlib.Path(args.room_noise) if args.room_noise else None
    index = build_corpus(work, asset_path(work, "vbd-test.parquet"), demand, room, args.clips)
    sets: dict[str, int] = {}
    for seg in index["segments"]:
        sets[seg["set"]] = sets.get(seg["set"], 0) + 1
    print(f"corpus: {index['seconds']:.0f} s, segments per set {sets}")
    return 0


def read_audio(path: pathlib.Path) -> np.ndarray:
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32")
    if rate != SR:
        raise BenchError(f"{path} is {rate} Hz, not {SR}")
    return audio if audio.ndim == 1 else audio.mean(axis=1)


def cmd_run(args: argparse.Namespace) -> int:
    import soundfile as sf

    work = pathlib.Path(args.work)
    noisy = read_audio(work / "noisy.wav")
    ladspa = None
    if "rnnoise" in args.engines:
        ladspa = work / "ladspa"
        ladspa.mkdir(exist_ok=True)
        if not (ladspa / "librnnoise_ladspa.so").is_file():
            with zipfile.ZipFile(asset_path(work, "linux-rnnoise.zip")) as archive:
                member = next(n for n in archive.namelist() if n.endswith("ladspa/librnnoise_ladspa.so"))
                (ladspa / "librnnoise_ladspa.so").write_bytes(archive.read(member))
    results: dict[str, Any] = {}
    for engine in args.engines:
        print(f"== {engine}", flush=True)
        result = run_dpdfnet(engine, work) if engine in MODELS else run_chain(engine, work, ladspa)
        audio, lag = align(result.pop("audio"), noisy)
        result["delay_ms"] = round(lag * 1000, 1) if engine in MODELS else round(chain_delay_ms(engine, work), 1)
        if engine not in MODELS:
            result["delay_ms_passthrough_graph"] = round(chain_delay_ms("off", work), 1)
        if engine in MODELS:
            result["algorithmic_delay_ms"] = round(algorithmic_delay_ms(engine, work), 1)
        sf.write(work / f"out-{engine}.wav", audio, SR, subtype="FLOAT")
        results[engine] = result
        print(json.dumps(result), flush=True)
    for engine, result in results.items():  # one file per engine, so engines can run in parallel processes
        (work / f"run-{engine}.json").write_text(json.dumps(result, indent=1))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    work = pathlib.Path(args.work)
    index = json.loads((work / "corpus.json").read_text())
    clean, noisy = read_audio(work / "clean.wav"), read_audio(work / "noisy.wav")
    dnsmos = Dnsmos(asset_path(work, "dns-sig-bak-ovr.onnx"))
    scores: dict[str, Any] = {"clean (reference)": score_audio(index, clean, noisy, clean, dnsmos)}
    scores["noisy input"] = score_audio(index, clean, noisy, None, dnsmos)
    for engine in args.engines:
        scores[engine] = score_audio(index, clean, noisy, read_audio(work / f"out-{engine}.wav"), dnsmos)
    (work / "scores.json").write_text(json.dumps(scores, indent=1))
    cost = {e: json.loads(f.read_text()) for e in ENGINES if (f := work / f"run-{e}.json").is_file()}
    metrics = ("pesq_wb", "stoi", "si_sdr", "dnsmos_sig", "dnsmos_bak", "dnsmos_ovr", "speech_level_vs_clean_db")
    for subset in ("all", "vbd", "demand", "room"):
        if subset not in scores["noisy input"]:
            continue
        print(f"\n[{subset}]  " + "  ".join(f"{m:>10}" for m in metrics))
        for name, per_set in scores.items():
            row = per_set[subset]
            print(f"{name:<26}" + "  ".join(f"{row[m]:>10.3f}" for m in metrics))
    print("\n" + json.dumps(cost, indent=1))
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    for step in (cmd_prepare, cmd_run, cmd_score):
        code = step(args)
        if code:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("fetch", cmd_fetch),
        ("prepare", cmd_prepare),
        ("run", cmd_run),
        ("score", cmd_score),
        ("all", cmd_all),
    ):
        p = sub.add_parser(name)
        p.add_argument("--work", required=True, help="working directory (fixtures, outputs, results)")
        p.add_argument("--clips", type=int, default=60, help="VoiceBank+DEMAND pairs in the corpus")
        p.add_argument("--room-noise", help="speech-free recording of the real microphone's room noise (wav)")
        p.add_argument("--engines", nargs="+", default=list(ENGINES), choices=ENGINES)
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except BenchError as exc:
        print(f"bench_denoise: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
