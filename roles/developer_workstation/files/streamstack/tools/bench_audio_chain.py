#!/usr/bin/env python3
"""Objective test of the voice chain on a private PipeWire instance (never the machine's own audio).

It starts its own `pipewire` daemon and session manager (own socket and runtime directory, no ALSA, no device), hosts
the real generated filter-chain in it, plays a synthetic noisy-speech file into the chain and measures what comes out:

* `denoise`   noise floor before and after (quiet lead-in, and the gaps between syllables), speech level change and
              how much of the clean speech is still in the output (correlation with the clean reference), for each
              neural engine that is installed and for the full default chain. The output is aligned to the reference
              by cross-correlation first, so the plugins' latency does not matter.
* `loudness`  the closed-loop loudness leveller: speech whose level steps by 20 dB, the chain output metered with the
              Console's own BS.1770 analyser, `ChainModel.level_step` riding the gain and the new value sent to the
              live graph with `pw-cli`, exactly as the Console does. Reports the settling time to -16 +-1 LU after
              each step, the true peak of the output and that the gain did not move while the input was silent.
* `rate`      the graph's sample rate and quantum (the interface profile's choices, hardware/ profiles): the same
              noisy speech through the default chain built at 48000 and at 96000 Hz and at several quanta, reporting
              the host process's CPU, the xrun count (pw-top ERR) of the chain's nodes, the delay through the chain and
              how much of the clean speech survives (a stage that does not support the rate shows up as lost speech).
* `default`   StreamStack Mic as the default source (audiochain/defaults.py, docs/audio-chain.md): claim and release
              against a fake raw microphone, which node an app that asks for "Default" is linked to, what
              WirePlumber does when the host is killed without a release, and when it comes back. Needs no numpy.
* `glitch`    sets controls rapidly while a steady tone plays through the chain and counts samples that deviate from
              the expected waveform (a dropout or click shows as a deviation).

Nothing touches the user's PipeWire: the environment is rebuilt from scratch for the child processes, and every
process is terminated and the runtime directory removed on exit (also on an error or ctrl-c).

    uvx --with numpy --with scipy python tools/bench_audio_chain.py all

Needs numpy, scipy, ffmpeg, pipewire, wireplumber, pw-cat/pw-record/pw-link/pw-cli and the LADSPA plugins of the chain
(see docs/audio-chain.md). Developer tool, not shipped in the wheel. Exit status 1 when a measurement misses its bound.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from typing import Any

import numpy as np
from scipy import signal as dsp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from streamstack.audiochain import chain, defaults, graph, runtime, stages
from streamstack.console import loudness

SR = 48000
TARGET_LUFS = -16.0

DAEMON_CONF = """
context.properties = {
  core.daemon = true core.name = @SOCK@ log.level = 1
  default.clock.rate = 48000 default.clock.quantum = 512 default.clock.min-quantum = 64
}
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
  { name = libpipewire-module-rt args = { nice.level = 0 } flags = [ ifexists nofail ] }
  { name = libpipewire-module-protocol-native }
  { name = libpipewire-module-metadata }
  { name = libpipewire-module-access args = { } }
  { name = libpipewire-module-spa-node-factory }
  { name = libpipewire-module-client-node }
  { name = libpipewire-module-adapter }
  { name = libpipewire-module-link-factory }
]
context.objects = [
  { factory = spa-node-factory
    args = { factory.name = support.node.driver node.name = iso-driver node.group = pipewire.dummy
             priority.driver = 20000 } }
  { factory = adapter
    args = { factory.name = support.null-audio-sink node.name = iso_in media.class = Audio/Sink
             object.linger = true audio.position = [ MONO ] monitor.channel-volumes = true } }
]
"""

WP_CONF = """
wireplumber.profiles = {
  iso = {
    inherits = [ base mixin.stateless ]
    metadata.sm-settings = required
    metadata.sm-objects = required
    policy.standard = required
    hardware.audio = disabled
    hardware.bluetooth = disabled
    hardware.video-capture = disabled
    support.reserve-device = disabled
    support.portal-permissionstore = disabled
    support.logind = disabled
  }
}
"""


class BenchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# The private PipeWire instance
# ---------------------------------------------------------------------------


class Rig:
    """A private PipeWire daemon plus session manager; everything is torn down by `stop`."""

    def __init__(self) -> None:
        base = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix=f"ss-iso-{os.getpid()}-", dir=base))
        self.dir.chmod(0o700)
        self.sock = "pw-iso"
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("PULSE", "PIPEWIRE", "DBUS", "WAYLAND"))}
        keep.pop("DISPLAY", None)
        self.env = {
            **keep,
            "XDG_RUNTIME_DIR": str(self.dir),
            "PIPEWIRE_REMOTE": self.sock,
            "PIPEWIRE_RUNTIME_DIR": str(self.dir),
            "XDG_CONFIG_HOME": str(self.dir / "cfg"),
            "XDG_DATA_HOME": str(self.dir / "data"),
            "XDG_STATE_HOME": str(self.dir / "state"),
            "HOME": str(self.dir),
        }
        self.procs: list[subprocess.Popen[bytes]] = []
        (self.dir / "daemon.conf").write_text(DAEMON_CONF.replace("@SOCK@", self.sock))

    def spawn(self, argv: list[str], log: str = "p.log", **kw: Any) -> subprocess.Popen[bytes]:
        out = (self.dir / log).open("ab")
        proc = subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL, stderr=out, **{"stdout": out, **kw})
        out.close()
        self.procs.append(proc)
        return proc

    def run(self, argv: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout, check=False)

    def runner(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.run(argv, timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def start(self) -> Rig:
        self.spawn(["pipewire", "-c", str(self.dir / "daemon.conf")], "daemon.log")
        for _ in range(50):
            if (self.dir / self.sock).exists():
                break
            time.sleep(0.1)
        else:
            raise BenchError("the private pipewire did not start: " + (self.dir / "daemon.log").read_text())
        if self.sock not in self.run(["pw-cli", "info", "0"]).stdout:
            raise BenchError("pw-cli is not talking to the private instance; refusing to continue")
        conf = self.dir / "cfg/wireplumber/wireplumber.conf.d"
        conf.mkdir(parents=True)
        (conf / "10-iso.conf").write_text(WP_CONF)
        self.spawn(["wireplumber", "-p", "iso"], "wp.log")
        time.sleep(2.0)
        return self

    def host(self, conf_text: str) -> subprocess.Popen[bytes]:
        path = self.dir / "chain.conf"
        path.write_text(conf_text)
        proc = self.spawn(["pipewire", "-c", str(path)], "chain.log")
        end = time.monotonic() + 12
        while time.monotonic() < end:
            nodes = runtime.find_nodes(self.runner)
            if nodes is not None and nodes.present:
                return proc
            time.sleep(0.3)
        raise BenchError("the chain never appeared: " + (self.dir / "chain.log").read_text()[-1500:])

    def nodes(self) -> runtime.Nodes:
        nodes = runtime.find_nodes(self.runner)
        if nodes is None or not nodes.present:
            raise BenchError("the chain's nodes are gone")
        return nodes

    def link(self, a: str, b: str) -> None:
        result = self.run(["pw-link", a, b])
        if result.returncode:
            raise BenchError(f"pw-link {a} {b}: {result.stderr.strip()}")

    def wait_ports(self, *names: str) -> str:
        listing = ""
        for _ in range(60):
            listing = self.run(["pw-link", "-o"]).stdout + self.run(["pw-link", "-i"]).stdout
            if all(n in listing for n in names):
                return listing
            time.sleep(0.1)
        raise BenchError(f"ports {names} never appeared:\n{listing}")

    def stop(self) -> None:
        for proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> Rig:
        try:
            return self.start()
        except BaseException:
            self.stop()
            raise

    def __exit__(self, *exc: object) -> None:
        self.stop()


def feed(rig: Rig, wav: pathlib.Path) -> subprocess.Popen[bytes]:
    """Play `wav` into the private sink the chain reads its microphone from."""
    proc = rig.spawn(["pw-cat", "-p", "-P", "node.name=feeder node.autoconnect=false", str(wav)], "play.log")
    listing = rig.wait_ports("feeder:output_")
    chan = "FL" if "feeder:output_FL" in listing else "MONO"
    rig.link(f"feeder:output_{chan}", "iso_in:playback_MONO")
    return proc


def record(rig: Rig, out: pathlib.Path | None = None, raw_pipe: bool = False) -> subprocess.Popen[bytes]:
    """Record the chain's source: to a file, or raw float32 to the returned process's stdout."""
    argv = ["pw-cat", "-r", "-P", "node.name=rec node.autoconnect=false", "--rate", "48000", "--channels", "1"]
    argv += ["--format", "f32"]
    if raw_pipe:
        proc = subprocess.Popen(
            [*argv, "--raw", "-"],
            env=rig.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        rig.procs.append(proc)
    else:
        if out is None:
            raise BenchError("record needs a file or a pipe")
        proc = rig.spawn([*argv, str(out)], "rec.log")
    rig.wait_ports("rec:input_")
    rig.link(f"{graph.NODE_NAME}:capture_MONO", "rec:input_MONO")
    return proc


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def read_wav(path: pathlib.Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype="<f4").astype(np.float64)


def write_wav(path: pathlib.Path, x: np.ndarray) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "f32le",
            "-ar",
            str(SR),
            "-ac",
            "1",
            "-i",
            "-",
            "-c:a",
            "pcm_f32le",
            str(path),
        ],
        input=np.asarray(x, dtype="<f4").tobytes(),
        check=True,
    )


def db(x: float) -> float:
    return 20 * np.log10(max(1e-9, x))


def rms_db(x: np.ndarray) -> float:
    return db(float(np.sqrt(np.mean(np.square(x))))) if len(x) else -180.0


VOWELS = [  # first three formants (Hz) of /a e i o u/
    (730, 1090, 2440),
    (530, 1840, 2480),
    (270, 2290, 3010),
    (570, 840, 2410),
    (300, 870, 2240),
]


def synth_speech(seconds: float, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic speech-like signal: voiced syllables (glottal pulses through formant resonators) with a
    fricative onset on some of them, syllable and word gaps. Returns (signal at -26 dB RMS
    while active, active mask)."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR))
    mask = np.zeros(len(out), dtype=bool)
    t = 0.15
    f0 = 118.0
    while t < seconds - 0.4:
        dur = rng.uniform(0.16, 0.30)
        n = int(dur * SR)
        i = int(t * SR)
        f0 = float(np.clip(f0 + rng.normal(0, 9), 90, 190))
        contour = f0 * (1 + 0.08 * np.sin(2 * np.pi * rng.uniform(3, 6) * np.arange(n) / SR))
        phase = np.cumsum(contour) / SR
        pulses = np.diff(np.floor(phase), prepend=0.0) > 0
        src = pulses.astype(float)
        src = dsp.lfilter([1.0], [1.0, -0.97], src)  # glottal spectral tilt
        y = src
        for centre, bw in zip(VOWELS[rng.integers(len(VOWELS))], (80, 100, 150), strict=True):
            r = np.exp(-np.pi * bw / SR)
            theta = 2 * np.pi * centre / SR
            y = dsp.lfilter([1 - r], [1, -2 * r * np.cos(theta), r * r], y)
        env = np.minimum(1.0, np.minimum(np.arange(n), n - np.arange(n)) / (0.025 * SR)) ** 1.5
        y = y / max(1e-9, float(np.max(np.abs(y)))) * env * 10 ** (rng.uniform(-10, 0) / 20)  # stress varies
        if rng.random() < 0.4:  # s / sh like onset: high-passed noise
            m = int(0.07 * SR)
            hiss = dsp.lfilter(*dsp.butter(2, 4500 / (SR / 2), "high"), rng.standard_normal(m)) * np.hanning(m)
            y[:m] += 0.35 * hiss / max(1e-9, float(np.max(np.abs(hiss))))
        end = min(len(out), i + n)
        out[i:end] += y[: end - i]
        mask[i:end] = True
        t += dur + (rng.uniform(0.28, 0.5) if rng.random() < 0.18 else rng.uniform(0.03, 0.10))
    active = np.abs(out) > 1e-4
    out *= 10 ** (-26 / 20) / float(np.sqrt(np.mean(out[active] ** 2)))
    return out, mask


def pink(n: int, rng: np.random.Generator) -> np.ndarray:
    b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
    a = [1, -2.494956002, 2.017265875, -0.522189400]
    return dsp.lfilter(b, a, rng.standard_normal(n))


def make_noisy(
    clean_speech: np.ndarray,
    mask: np.ndarray,
    lead: float = 2.5,
    tail: float = 2.5,
    noise_db: float = -38.0,
    seed: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    """(clean, noisy, mask, lead samples, tail samples): speech plus pink noise, mains hum and keyboard clicks."""
    rng = np.random.default_rng(seed)
    lead_n, tail_n = int(lead * SR), int(tail * SR)
    total = lead_n + len(clean_speech) + tail_n
    clean = np.zeros(total)
    clean[lead_n : lead_n + len(clean_speech)] = clean_speech
    full_mask = np.zeros(total, dtype=bool)
    full_mask[lead_n : lead_n + len(mask)] = mask
    noise = pink(total, rng)
    noise *= 10 ** (noise_db / 20) / np.sqrt(np.mean(noise**2))
    ts = np.arange(total) / SR
    hum = 10 ** ((noise_db - 8) / 20) * (np.sin(2 * np.pi * 50 * ts) + 0.5 * np.sin(2 * np.pi * 100 * ts))
    clicks = np.zeros(total)
    for ct in np.arange(0.3, total / SR - 0.1, 0.37):
        i, ln = int(ct * SR), int(0.004 * SR)
        clicks[i : i + ln] += rng.standard_normal(ln) * np.hanning(ln) * 10 ** ((noise_db + 14) / 20)
    return clean, clean + noise + hum + clicks, full_mask, lead_n, tail_n


def align(out: np.ndarray, ref: np.ndarray, max_lag_s: float = 0.4) -> tuple[np.ndarray, float]:
    """(`out` shifted to line up with `ref`, its lag in seconds); the lag comes from an FFT cross-correlation."""
    n = min(len(ref), len(out))
    corr = dsp.correlate(out[:n], ref[:n], mode="full", method="fft")
    lags = np.arange(-n + 1, n)
    window = np.abs(lags) <= int(max_lag_s * SR)
    lag = int(lags[window][np.argmax(np.abs(corr[window]))])
    shifted = out[lag:] if lag >= 0 else np.concatenate([np.zeros(-lag), out])
    return np.pad(shifted, (0, max(0, len(ref) - len(shifted))))[: len(ref)], lag / SR


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def run_through_chain(rig: Rig, wav: pathlib.Path, seconds: float) -> np.ndarray:
    out = rig.dir / "out.wav"
    rec = record(rig, out)
    play = feed(rig, wav)
    time.sleep(seconds)
    play.terminate()
    rec.send_signal(signal.SIGINT)
    for proc in (play, rec):
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    return read_wav(out)


def stage_params(**over: float | bool) -> dict[str, float | bool]:
    """Only the named stages on (`denoise_only` style); everything else transparent."""
    params = stages.defaults()
    params.update({stages.on_key(s): False for s in stages.STAGE_IDS})
    params.update(over)
    return params


def denoise_case(
    name: str, params: dict[str, float | bool], engine: str, work: pathlib.Path, data: tuple
) -> dict[str, Any]:
    clean, noisy, mask, lead, tail = data
    wav = work / "noisy.wav"
    with Rig() as rig:
        rig.host(graph.build_config(params, engine=engine))
        out = run_through_chain(rig, wav, len(noisy) / SR + 1.0)
    aligned, lag = align(out, noisy)
    speech = mask
    near = np.convolve(mask, np.ones(int(0.08 * SR)), mode="same") > 0  # within 80 ms of speech
    gaps = ~near
    gaps[: int(0.4 * SR)] = False
    gaps[len(gaps) - int(0.2 * SR) :] = False
    lead_q = slice(int(0.6 * SR), lead - int(0.2 * SR))
    tail_q = slice(len(clean) - tail + int(0.3 * SR), len(clean) - int(0.2 * SR))
    gain = float(np.dot(aligned[speech], clean[speech]) / np.dot(clean[speech], clean[speech]))
    corr = float(np.corrcoef(aligned[speech], clean[speech])[0, 1])
    corr_in = float(np.corrcoef(noisy[speech], clean[speech])[0, 1])
    return {
        "case": name,
        "engine": engine,
        "offset_ms": round(lag * 1000, 1),
        "floor_in_db": round(rms_db(np.concatenate([noisy[lead_q], noisy[tail_q]])), 1),
        "floor_out_db": round(rms_db(np.concatenate([aligned[lead_q], aligned[tail_q]])), 1),
        "gaps_in_db": round(rms_db(noisy[gaps]), 1),
        "gaps_out_db": round(rms_db(aligned[gaps]), 1),
        "speech_in_db": round(rms_db(noisy[speech]), 1),
        "speech_out_db": round(rms_db(aligned[speech]), 1),
        "speech_gain_vs_clean_db": round(db(abs(gain)), 1),
        "corr_with_clean_in": round(corr_in, 3),
        "corr_with_clean_out": round(corr, 3),
    }


def _cpu_ticks(pid: int) -> int:
    fields = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def _xruns(rig: Rig) -> tuple[int, str]:
    """(errors summed over the chain's nodes, the quantum pw-top shows) from a short batch of pw-top."""
    out = rig.run(["pw-top", "-b", "-n", "3"], 15).stdout
    errors, quantum = 0, ""
    for line in out.splitlines():
        cols = line.split()
        listed = cols and cols[-1].startswith("streamstack_mic") and len(cols) > 8 and cols[0] in ("R", "S", "I")
        if listed and cols[1].isdigit() and cols[8].isdigit():
            errors = max(errors, int(cols[8]))
            quantum = cols[2] if cols[2] != "0" else quantum
    return errors, quantum


RATE_CASES = (
    ("48000 Hz, quantum 256, DeepFilterNet", 48000, 256, "deepfilternet"),
    ("48000 Hz, quantum 128, DeepFilterNet", 48000, 128, "deepfilternet"),
    ("48000 Hz, quantum 512, DeepFilterNet", 48000, 512, "deepfilternet"),
    ("96000 Hz, quantum 256, DeepFilterNet", 96000, 256, "deepfilternet"),
    ("96000 Hz, quantum 512, DeepFilterNet", 96000, 512, "deepfilternet"),
    ("48000 Hz, quantum 256, no neural stage", 48000, 256, "off"),
    ("96000 Hz, quantum 256, no neural stage", 96000, 256, "off"),
)


def rate_case(
    name: str, rate: int, quantum: int, engine: str, work: pathlib.Path, data: tuple, repeats: int
) -> dict[str, Any]:
    clean, noisy, mask, _lead, _tail = data
    rows = []
    for _ in range(repeats):
        with Rig() as rig:
            conf = rig.dir / "chain.conf"
            rig.host(graph.build_config(stages.defaults(), engine=engine, rate=rate, quantum=quantum))
            pids = runtime.host_pids(conf)
            if not pids:
                raise BenchError("the chain's host process was not found")
            out = rig.dir / "out.wav"
            rec = record(rig, out)
            play = feed(rig, work / "noisy.wav")
            time.sleep(1.5)
            ticks0, t0 = _cpu_ticks(pids[0]), time.monotonic()
            time.sleep(len(noisy) / SR - 2.0)
            ticks1, t1 = _cpu_ticks(pids[0]), time.monotonic()
            errors, seen_quantum = _xruns(rig)
            time.sleep(1.0)
            play.terminate()
            rec.send_signal(signal.SIGINT)
            for proc in (play, rec):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            heard = read_wav(out)
        if len(heard) < SR:
            raise BenchError(f"{name}: the recording is empty (the graph did not pass audio at {rate} Hz)")
        aligned, lag = align(heard, noisy)
        speech = mask
        rows.append(
            {
                "cpu_percent": 100.0 * (ticks1 - ticks0) / os.sysconf("SC_CLK_TCK") / (t1 - t0),
                "xruns": errors,
                "quantum": seen_quantum,
                "delay_ms": lag * 1000,
                "corr_with_clean": float(np.corrcoef(aligned[speech], clean[speech])[0, 1]),
            }
        )
    mean = {
        k: float(np.mean([r[k] for r in rows if r[k] != ""])) for k in ("cpu_percent", "delay_ms", "corr_with_clean")
    }
    return {
        "case": name,
        "rate": rate,
        "engine": engine,
        "runs": repeats,
        "cpu_percent": round(mean["cpu_percent"], 2),
        "xruns": sum(r["xruns"] for r in rows),
        "quantum_seen": rows[-1]["quantum"],
        "delay_ms": round(mean["delay_ms"], 1),
        "corr_with_clean": round(mean["corr_with_clean"], 3),
    }


def bench_rate(work: pathlib.Path) -> tuple[list[dict[str, Any]], list[str]]:
    speech, mask = synth_speech(14.0)
    data = make_noisy(speech, mask)
    write_wav(work / "noisy.wav", data[1])
    write_wav(work / "clean.wav", data[0])
    have = graph.available_engines()
    results = []
    for name, rate, quantum, engine in RATE_CASES:
        if engine != "off" and engine not in have:
            continue
        try:
            results.append(rate_case(name, rate, quantum, engine, work, data, repeats=3))
        except BenchError as error:
            print(f"  {error}", flush=True)
            results.append({"case": name, "rate": rate, "engine": engine, "failed": str(error)})
            continue
        print(f"  {results[-1]}", flush=True)
    results = [r for r in results if "failed" not in r] + [r for r in results if "failed" in r]
    problems = [f"{r['case']}: {r['failed']}" for r in results if "failed" in r]
    results = [r for r in results if "failed" not in r]
    base = next((r for r in results if r["rate"] == 48000 and r["engine"] != "off" and "256" in r["case"]), None)
    for row in results:
        if row["xruns"]:
            problems.append(f"{row['case']}: {row['xruns']} xruns")
        if (
            base
            and row["engine"] == base["engine"]
            and row["rate"] == 96000
            and row["corr_with_clean"] < base["corr_with_clean"] - 0.02
        ):
            problems.append(f"{row['case']}: the speech is less like the clean reference than at 48000 Hz")
    return results, problems


def bench_denoise(work: pathlib.Path) -> tuple[list[dict[str, Any]], list[str]]:
    speech, mask = synth_speech(14.0)
    data = make_noisy(speech, mask)
    write_wav(work / "noisy.wav", data[1])
    write_wav(work / "clean.wav", data[0])
    have = graph.available_engines()
    results: list[dict[str, Any]] = []
    cases: list[tuple[str, dict[str, float | bool], str]] = [("passthrough", stage_params(), "off")]
    for engine in stages.DENOISE_ENGINES:
        if engine in have:
            cases.append((f"{engine} only", stage_params(denoise_on=True), engine))
            if engine == "deepfilternet":
                cases.append(
                    ("deepfilternet only, 30 dB limit", stage_params(denoise_on=True, denoise_atten_db=30.0), engine)
                )
    best = "deepfilternet" if "deepfilternet" in have else ("rnnoise" if "rnnoise" in have else "off")
    cases.append(("full default chain", stages.defaults(), best))
    for name, params, engine in cases:
        results.append(denoise_case(name, params, engine, work, data))
    problems = []
    for row in results:
        if row["case"] == "passthrough":
            if abs(row["floor_out_db"] - row["floor_in_db"]) > 0.5:
                problems.append("the transparent chain changed the level by more than 0.5 dB")
            continue
        if row["engine"] != "off":
            drop = row["gaps_in_db"] - row["gaps_out_db"]
            if drop < 12:
                problems.append(f"{row['case']}: noise between syllables only dropped {drop:.1f} dB (want >= 12)")
            if "only" in row["case"] and row["corr_with_clean_out"] < row["corr_with_clean_in"] - 0.05:
                problems.append(f"{row['case']}: the speech is less like the clean reference than the input was")
    return results, problems


class Meter:
    """The Console's own analyser on a raw float32 stream; `.drain()` reads what is available."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.analyzer = loudness.Analyzer()
        self.buffer = b""
        self.lufs_m = loudness.SILENCE_DB
        self.lufs_s = loudness.SILENCE_DB
        self.peak_db = loudness.SILENCE_DB
        os.set_blocking(proc.stdout.fileno(), False)  # type: ignore[union-attr]

    def drain(self) -> None:
        fd = self.proc.stdout.fileno()  # type: ignore[union-attr]
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                break
            if not data:
                break
            self.buffer += data
        size = loudness.FRAME_SAMPLES * 4
        while len(self.buffer) >= size:
            frame, self.buffer = self.buffer[:size], self.buffer[size:]
            samples = array.array("f")
            samples.frombytes(frame)
            levels = self.analyzer.process(samples)
            self.lufs_m = levels.lufs_m
            self.lufs_s = levels.lufs_s
            self.peak_db = max(self.peak_db, db(levels.true_peak)) if self.peak_db > -120 else db(levels.true_peak)


def level_script(steps: list[tuple[float, float]], speech: np.ndarray) -> np.ndarray:
    """Speech repeated, each segment `(seconds, input level in LUFS)`; speech is -26 dB RMS (about -26 LUFS)."""
    parts = []
    for seconds, level in steps:
        n = int(seconds * SR)
        reps = np.tile(speech, int(np.ceil(n / len(speech))))[:n]
        parts.append(reps * 10 ** ((level + 26) / 20))
    return np.concatenate(parts)


def mic_levels(x: np.ndarray) -> np.ndarray:
    """Momentary loudness of the input signal per 20 ms frame, standing in for the Console's raw microphone meter."""
    analyzer = loudness.Analyzer()
    out = []
    for i in range(0, len(x) - loudness.FRAME_SAMPLES + 1, loudness.FRAME_SAMPLES):
        frame = array.array("f")
        frame.frombytes(x[i : i + loudness.FRAME_SAMPLES].astype("<f4").tobytes())
        out.append(analyzer.process(frame).lufs_m)
    return np.array(out)


def gated_loudness(values: np.ndarray) -> float:
    """BS.1770 gated loudness of momentary readings: absolute gate -70 LUFS, then 10 LU below the mean of those."""
    power = 10 ** (values / 10)
    power = power[values > -70.0]
    if len(power) == 0:
        return -70.0
    keep = power[10 * np.log10(power) > 10 * np.log10(np.mean(power)) - 10.0]
    return float(10 * np.log10(np.mean(keep)))


def bench_loudness(work: pathlib.Path) -> tuple[dict[str, Any], list[str]]:
    speech, mask = synth_speech(6.0, seed=11)
    active = np.flatnonzero(mask)
    speech = speech[max(0, active[0] - int(0.05 * SR)) : active[-1] + int(0.05 * SR)]  # loops without a long pause
    # settle first, then a 20 dB step up and a 20 dB step back down (the gain window is +-12 dB, so both are reachable)
    steps = [(25.0, -20.0), (35.0, 0.0), (35.0, -20.0)]
    signal_in = level_script(steps, speech)
    wav = work / "levels.wav"
    write_wav(wav, signal_in)
    mic = mic_levels(signal_in)
    model = chain.ChainModel(store=None, engines=lambda: graph.available_engines())
    model.params["comp_on"] = False  # the closed loop is the subject; the compressor has its own tests
    model.set_loudness(on=True, target=TARGET_LUFS)
    engine = "deepfilternet" if "deepfilternet" in graph.available_engines() else "off"
    series: list[tuple[float, float, float]] = []  # time, momentary loudness of the chain output, leveller gain
    with Rig() as rig:
        rig.host(graph.build_config(model.params, engine=engine))
        node = rig.nodes().control_id
        if node is None:
            raise BenchError("the chain has no control node")
        runtime.set_controls(node, model.controls(), rig.runner)
        rec = record(rig, raw_pipe=True)
        meter = Meter(rec)
        play = feed(rig, wav)
        start = time.monotonic()
        last = start
        sent = dict(model.controls())
        total = sum(s for s, _ in steps)
        while (now := time.monotonic()) - start < total:
            time.sleep(0.1)
            meter.drain()
            frame = min(len(mic) - 1, int((now - start) * 50))
            model.level_step(now - last, meter.lufs_m, muted=False, mic_lufs=float(mic[frame]))
            last = now
            changed = {k: v for k, v in model.controls().items() if sent.get(k) != v}
            if changed and runtime.set_controls(node, changed, rig.runner):
                sent.update(changed)
            series.append((now - start, meter.lufs_m, model.auto_db))
        play.terminate()
        rec.terminate()
    if os.environ.get("BENCH_TRACE"):
        pathlib.Path(os.environ["BENCH_TRACE"]).write_text("\n".join(f"{t:.1f} {m:.1f} {a:.2f}" for t, m, a in series))
    bounds = np.cumsum([0.0] + [s for s, _ in steps])
    times = np.array([s[0] for s in series])
    louds = np.array([s[1] for s in series])
    gains = np.array([s[2] for s in series])
    result: dict[str, Any] = {"target_lufs": TARGET_LUFS, "steps": []}
    problems: list[str] = []
    for i, (_seconds, level) in enumerate(steps):
        seg = (times >= bounds[i]) & (times < bounds[i + 1])
        t_seg, g_seg, l_seg = times[seg] - bounds[i], gains[seg], louds[seg]
        smooth = np.convolve(g_seg, np.ones(15) / 15, mode="same")  # the gain, averaged over 1.5 s
        final_gain = float(np.mean(g_seg[-80:]))
        settle = next(
            (float(t_seg[j]) for j in range(len(t_seg) - 8) if np.all(np.abs(smooth[j:-8] - final_gain) <= 1.5)), None
        )
        final = gated_loudness(l_seg[-150:])  # the last 15 s: two or more passes of the looped speech
        result["steps"].append(
            {
                "input_lufs": level,
                "gain_settle_s": None if settle is None else round(settle, 1),
                "final_gain_db": round(final_gain, 1),
                "final_loudness_lufs": round(final, 1),
            }
        )
        if i > 0 and (settle is None or settle > 10.0):
            problems.append(f"step to {level} LUFS: the gain did not settle (within 1.5 dB) inside 10 s ({settle})")
        if abs(final - TARGET_LUFS) > 1.0:
            problems.append(f"step to {level} LUFS ended at {final:.1f} LUFS, not {TARGET_LUFS} +-1")
    result["true_peak_db"] = round(meter.peak_db, 2)
    if meter.peak_db > stages.TRUE_PEAK_CEILING_DB + 0.3:
        problems.append(f"true peak {meter.peak_db:.2f} dBTP is above the {stages.TRUE_PEAK_CEILING_DB} dBTP ceiling")
    frozen = model.auto_db  # silence and mute never move the gain
    for _ in range(100):
        model.level_step(0.1, -80.0, muted=False)
        model.level_step(0.1, -20.0, muted=True)
    result["gain_frozen_in_silence_and_mute"] = model.auto_db == frozen
    if model.auto_db != frozen:
        problems.append("the gain moved during silence or mute")
    return result, problems


# Moving a biquad corner or gain leaves a click (reported, not failed: the chain's runtime sends the high-pass corner
# only once it has settled); the controls that only scale the signal must leave none.
CLICK_FREE = ("same", "gain")


# ---------------------------------------------------------------------------
# StreamStack Mic as the default source
# ---------------------------------------------------------------------------


def effective_default(rig: Rig) -> str | None:
    """The source WirePlumber currently hands out as "Default" (`default.audio.source`, not the configured one)."""
    result = rig.runner(["pw-metadata", "-n", "default"], 5)
    found = None
    for line in (result.stdout if result else "").splitlines():
        if " id:0 " in line and "key:'default.audio.source'" in line:
            match = re.search(r'"name"\s*:\s*"([^"]+)"', line)
            found = match.group(1) if match else None
    return found


def wait_default(rig: Rig, want: str | None, seconds: float = 6.0) -> str | None:
    end = time.monotonic() + seconds
    got = effective_default(rig)
    while got != want and time.monotonic() < end:
        time.sleep(0.3)
        got = effective_default(rig)
    return got


def bench_default(work: pathlib.Path) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    out: dict[str, Any] = {}
    state = work / "state"
    with Rig() as rig:
        for name in ("iso_raw_mic", "iso_other_mic"):
            made = rig.run(
                [
                    "pw-cli",
                    "create-node",
                    "adapter",
                    f"{{ factory.name=support.null-audio-sink node.name={name} media.class=Audio/Source/Virtual "
                    "object.linger=true audio.position=[ MONO ] monitor.channel-volumes=true }",
                ]
            )
            if made.returncode:
                raise BenchError(f"cannot create the fake microphone {name}: {made.stderr.strip()}")
        rig.wait_ports("iso_raw_mic:capture_MONO")
        if not defaults._set("iso_raw_mic", rig.runner):
            raise BenchError("cannot set the starting default source")
        out["start"] = wait_default(rig, "iso_raw_mic")
        if out["start"] != "iso_raw_mic":
            raise BenchError(f"the private session manager does not follow the configured default: {out['start']}")
        conf = graph.build_config(stages.defaults(), engine="off", microphone="iso_raw_mic")
        host = rig.host(conf)
        # claim
        out["claim"] = defaults.claim(state, rig.runner)
        out["default_while_running"] = wait_default(rig, graph.NODE_NAME)
        if out["claim"] != "claimed" or out["default_while_running"] != graph.NODE_NAME:
            problems.append(f"claim: {out['claim']}, apps get {out['default_while_running']}")
        # an app that opens "Default" (no target) is linked to StreamStack Mic
        app = rig.spawn(
            ["pw-cat", "-r", "-P", "node.name=default-app", "--rate", "48000", "--channels", "1", "/dev/null"],
            "app.log",
        )
        rig.wait_ports("default-app:input_")
        time.sleep(1.5)
        links = rig.run(["pw-link", "-l"]).stdout
        out["app_linked_to_chain"] = (
            f"{graph.NODE_NAME}:capture_MONO" in links
            and "default-app:input_" in links.split(f"{graph.NODE_NAME}:capture_MONO", 1)[-1].split("\n\n")[0]
        )
        out["app_links"] = [
            line.strip() for line in links.splitlines() if "default-app" in line or "streamstack_mic" in line
        ]
        if not out["app_linked_to_chain"]:
            problems.append("an app on Default was not linked to StreamStack Mic")
        app.terminate()
        # a second claim changes nothing
        out["second_claim"] = defaults.claim(state, rig.runner)
        # the host dies without a release: apps get the raw microphone by themselves
        host.kill()
        host.wait(timeout=5)
        out["default_after_host_killed"] = wait_default(rig, "iso_raw_mic")
        # The rig's only "microphone" is a virtual source, which WirePlumber ranks below the private sink's monitor, so
        # `iso_in` is an acceptable answer here; real hardware ranks above monitors (live check in docs/audio-chain.md).
        # What must hold is that apps are not left on the dead chain node.
        if out["default_after_host_killed"] in (None, graph.NODE_NAME):
            problems.append(f"after a crash apps are left on {out['default_after_host_killed']}")
        # the host comes back: the claim is still in force and WirePlumber returns to it
        rig.host(conf)
        out["default_after_host_restarted"] = wait_default(rig, graph.NODE_NAME)
        out["claim_after_restart"] = defaults.claim(state, rig.runner)
        if out["default_after_host_restarted"] != graph.NODE_NAME:
            problems.append(f"after the host restarted apps get {out['default_after_host_restarted']}")
        # release gives back exactly what was there before
        out["release"] = defaults.release(state, rig.runner)
        out["default_after_release"] = wait_default(rig, "iso_raw_mic")
        if out["release"] != "released" or out["default_after_release"] != "iso_raw_mic":
            problems.append(f"release: {out['release']}, apps get {out['default_after_release']}")
        # a default the person picked while the chain ran is left alone
        defaults.claim(state, rig.runner)
        defaults._set("iso_other_mic", rig.runner)
        out["release_after_user_choice"] = defaults.release(state, rig.runner)
        out["default_after_user_choice"] = wait_default(rig, "iso_other_mic")
        if out["default_after_user_choice"] != "iso_other_mic":
            problems.append("the default chosen by hand was overwritten")
    return out, problems


def bench_glitch(work: pathlib.Path) -> tuple[dict[str, Any], list[str]]:
    """A steady 1 kHz tone through the flat chain while controls are set about 30 times a second, one kind of control
    at a time; every deviation from the tone, once fitted, is a dropout or click.

    The `same` case sets the values the chain already has: it measures the cost of the control path itself. The
    deviation is the peak of what is left after the best 1 kHz sinusoid is removed, so a control that changes the tone's
    level on purpose would show up too: only controls that leave 1 kHz alone are measured."""
    modes: dict[str, Any] = {
        "same": lambda k: {f"{stages.GAIN}:Mult": 1.0, f"{stages.HP1}:Freq": 80.0, f"{stages.EQ_LOW}:Gain": 2.0},
        "gain": lambda k: {f"{stages.GAIN}:Mult": 1.0 + (k % 2) * 0.002},
        "highpass": lambda k: {f"{stages.HP1}:Freq": 80.0 + (k % 3) * 5.0, f"{stages.HP2}:Freq": 80.0 + (k % 3) * 5.0},
        "highpass_hp1_only": lambda k: {f"{stages.HP1}:Freq": 80.0 + (k % 3) * 5.0},
        "eq_3db": lambda k: {f"{stages.EQ_LOW}:Gain": (k % 2) * 3.0},
    }
    span, rest = 3.0, 1.5
    n = int((2.0 + len(modes) * (span + rest) + 1.0) * SR)
    ts = np.arange(n) / SR
    tone = 0.2 * np.sin(2 * np.pi * 1000 * ts)
    wav = work / "tone.wav"
    write_wav(wav, tone)
    params = stage_params(**{stages.on_key("hp"): True, "hp_hz": 80.0, stages.on_key("eq"): True, "bass_db": 2.0})
    windows: dict[str, tuple[float, float]] = {}
    counts: dict[str, int] = {}
    with Rig() as rig:
        rig.host(graph.build_config(params, engine="off"))
        node = rig.nodes().control_id
        if node is None:
            raise BenchError("the chain has no control node")
        out_path = rig.dir / "out.wav"
        rec = record(rig, out_path)
        play = feed(rig, wav)
        t0 = time.monotonic()
        time.sleep(2.0)  # a quiet stretch first: the baseline residual of the rig itself
        for name, make in modes.items():
            begin = time.monotonic() - t0
            end = time.monotonic() + span
            k = 0
            while time.monotonic() < end:
                k += 1
                runtime.set_controls(node, make(k), rig.runner)
                time.sleep(0.02)
            windows[name] = (begin, time.monotonic() - t0)
            counts[name] = k
            time.sleep(rest)
        time.sleep(0.5)
        play.terminate()
        rec.send_signal(signal.SIGINT)
        rec.wait(timeout=5)
        out = read_wav(out_path)

    def residual(first: float, last: float) -> float:
        seg = out[int(first * SR) : int(last * SR)]
        t = np.arange(len(seg)) / SR
        basis = np.stack([np.sin(2 * np.pi * 1000 * t), np.cos(2 * np.pi * 1000 * t)], axis=1)
        coef, *_ = np.linalg.lstsq(basis, seg, rcond=None)  # the best 1 kHz sinusoid; the rest is deviation
        return db(float(np.max(np.abs(seg - basis @ coef))))

    # the recording starts a little after the player: the first seconds hold the rig's own start-up, so the
    # baseline is the quiet stretch just before the first window
    quiet = residual(max(0.5, windows["same"][0] - 1.2), windows["same"][0] - 0.2)
    busy = {name: round(residual(first + 0.3, last), 1) for name, (first, last) in windows.items()}
    result = {
        "tone_db": round(db(0.2), 1),
        "controls_set": counts,
        "residual_peak_db_idle": round(quiet, 1),
        "residual_peak_db_while_setting": busy,
    }
    problems = [
        f"setting {name} controls raised the deviation from {quiet:.1f} to {level:.1f} dBFS: a click or dropout"
        for name, level in busy.items()
        if name in CLICK_FREE and level > max(quiet + 6.0, -70.0)
    ]
    return result, problems


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "what", choices=["denoise", "loudness", "glitch", "rate", "default", "all"], nargs="?", default="all"
    )
    parser.add_argument("--json", action="store_true", help="print only the JSON result")
    args = parser.parse_args(argv)
    missing = graph.missing_required()
    if missing:
        print("missing LADSPA plugins: " + ", ".join(missing), file=sys.stderr)
        return 2
    for exe in ("pipewire", "wireplumber", "pw-cat", "pw-link", "pw-cli", "ffmpeg"):
        if shutil.which(exe) is None:
            print(f"missing tool: {exe}", file=sys.stderr)
            return 2
    work = pathlib.Path(tempfile.mkdtemp(prefix="ss-bench-"))
    report: dict[str, Any] = {"engines": graph.available_engines()}
    problems: list[str] = []
    jobs: dict[str, Callable[[pathlib.Path], tuple[Any, list[str]]]] = {
        "denoise": bench_denoise,
        "loudness": bench_loudness,
        "glitch": bench_glitch,
        "rate": bench_rate,
        "default": bench_default,
    }
    try:
        for name, job in jobs.items():
            if args.what in (name, "all"):
                report[name], found = job(work)
                problems += found
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report["problems"] = problems
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
