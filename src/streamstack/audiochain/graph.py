"""Generate the PipeWire filter-chain configuration for the voice chain.

The chain is a ``libpipewire-module-filter-chain`` graph hosted by its own ``pipewire -c <file>`` client process, so
the user's running PipeWire and its configuration are never touched (no restart, no drop-in, nothing to unload by
hand). The graph reads the microphone and exposes the processed voice as an ``Audio/Source`` called "StreamStack Mic"
(node name ``streamstack_mic``, 48 kHz, mono, 32 bit float) that applications pick like any other microphone.

Plugins (PipeWire 1.6, ``libspa-filter-graph``): the builtin biquad, copy, delay, linear and mixer nodes, plus LADSPA:
DeepFilterNet (``libdeep_filter_ladspa``) or RNNoise (``librnnoise_ladspa``) for the neural denoise, and the ZAM plugins
(``zam-plugins``) for the gate, the de-esser, the compressor and the limiter.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterable
from pathlib import Path

from . import stages

NODE_NAME = "streamstack_mic"
CAPTURE_NAME = "streamstack_mic.capture"
DESCRIPTION = "StreamStack Mic"
RATE = 48000  # the conferencing rate: what "StreamStack Mic" offers apps (ADR 0004) and the neural stages run at
GRAPH_VERSION = 2  # bump when the generated graph changes shape, so a running host is rebuilt

LADSPA_DIRS = ("/usr/local/lib/ladspa", "/usr/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa", "/usr/lib64/ladspa")

# What each LADSPA plugin is, the file that carries it, and what installs it.
PLUGINS: dict[str, dict[str, str]] = {
    "deepfilternet": {
        "file": "libdeep_filter_ladspa.so",
        "label": "deep_filter_mono",
        "name": "DeepFilterNet",
        "package": "the DeepFilterNet LADSPA plugin (`streamstack chain install-plugin`, built by CI)",
    },
    "rnnoise": {
        "file": "librnnoise_ladspa.so",
        "label": "noise_suppressor_mono",
        "name": "RNNoise",
        "package": "noise-suppression-for-voice",
    },
    "gate": {"file": "ZamGate-ladspa.so", "label": "ZamGate", "package": "zam-plugins"},
    "deess": {"file": "ZamDynamicEQ-ladspa.so", "label": "ZamDynamicEQ", "package": "zam-plugins"},
    "comp": {"file": "ZamComp-ladspa.so", "label": "ZamComp", "package": "zam-plugins"},
    "limiter": {"file": "ZaMaximX2-ladspa.so", "label": "ZaMaximX2", "package": "zam-plugins"},
}
REQUIRED = ("gate", "deess", "comp", "limiter")

# The audio port names of single-channel nodes (LADSPA port names differ per plugin; the builtin nodes use In and Out).
_ZAM_IO = ("Audio Input 1", "Audio Output 1")
IO: dict[str, tuple[str, str]] = {
    stages.GATE: _ZAM_IO,
    stages.DEESS: _ZAM_IO,
    stages.COMP: _ZAM_IO,
    "dfn:deepfilternet": ("Audio In", "Audio Out"),
    "dfn:rnnoise": ("Input", "Output"),
}

# Time through the denoiser and the limiter's look-ahead, in seconds; the raw (bypass) path is delayed to match so
# the A/B switch does not jump in time. Measured on the isolated rig (docs/audio-chain.md).
CHAIN_DELAY_S = {"deepfilternet": 0.0305, "rnnoise": 0.0125, "off": 0.0075}


# The sample rates each denoise engine's model runs at: DeepFilterNet and RNNoise are trained at 48 kHz and read the
# graph's samples as such, so a graph at another rate needs a resampler in front of them; without a neural stage every
# builtin and ZAM stage is rate independent (the filter corners and time constants are given in Hz and seconds).
ENGINE_RATES: dict[str, tuple[int, ...] | None] = {"deepfilternet": (48000,), "rnnoise": (48000,), "off": None}


def pick_rate(native: int | None, engine: str) -> tuple[int, str]:
    """The sample rate the graph runs at and why: the interface's own rate when every stage supports it, else the
    conferencing rate (the capture side resamples)."""
    if native is None:
        return RATE, "the conferencing rate"
    if native == RATE:
        return RATE, "the conferencing rate, which the interface offers"
    supported = ENGINE_RATES.get(engine)
    if supported is None or native in supported:
        return native, "the interface's native rate"
    names = {"deepfilternet": "DeepFilterNet", "rnnoise": "RNNoise"}
    return RATE, (
        f"{names.get(engine, engine)} is a {supported[0] // 1000} kHz model, so the graph runs at {RATE} Hz "
        f"and the interface's {native} Hz is resampled"
    )


def latency(quantum: int | None, rate: int) -> str | None:
    """PipeWire's `node.latency` for a quantum in frames at this rate ("256/48000"), None when unset."""
    return f"{quantum}/{rate}" if quantum else None


def user_ladspa_dir() -> Path:
    """Where `streamstack chain install-plugin` puts the CI-built plugins: no root needed, searched before /usr."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "streamstack" / "ladspa"


def ladspa_dirs() -> list[str]:
    return [p for p in os.environ.get("LADSPA_PATH", "").split(":") if p] + [str(user_ladspa_dir()), *LADSPA_DIRS]


def find_plugin(kind: str) -> Path | None:
    """The file of a LADSPA plugin, or None when it is not installed."""
    for folder in ladspa_dirs():
        path = Path(folder) / PLUGINS[kind]["file"]
        if path.is_file():
            return path
    return None


def available_engines() -> list[str]:
    return [e for e in stages.DENOISE_ENGINES if find_plugin(e) is not None]


def missing_required() -> list[str]:
    """Package names of required plugins that are not installed (empty when the chain can run)."""
    return sorted({PLUGINS[k]["package"] for k in REQUIRED if find_plugin(k) is None})


def pick_engine(wanted: str | None, installed: Iterable[str]) -> str:
    """The denoise engine to build: the wanted one when installed, else the best installed one, else "off"."""
    have = list(installed)
    if wanted in have:
        return str(wanted)
    for engine in stages.DENOISE_ENGINES:
        if engine in have:
            return engine
    return "off"


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _num(value: float) -> str:
    return f"{value:.6g}"


def _node(kind: str, name: str, label: str, plugin: str | None, initial: dict[str, float], config: str = "") -> str:
    lines = [f"        {{ type = {kind} name = {name} label = {label}"]
    if plugin:
        lines[0] += f" plugin = {_q(plugin)}"
    if config:
        lines.append(f"          config = {{ {config} }}")
    ports = {k.split(":", 1)[1]: v for k, v in initial.items() if k.split(":", 1)[0] == name}
    if ports:
        body = " ".join(f"{_q(port)} = {_num(value)}" for port, value in ports.items())
        lines.append(f"          control = {{ {body} }}")
    lines.append("        }")
    return "\n".join(lines)


def build_config(
    params: dict[str, float | bool],
    *,
    engine: str,
    microphone: str | None = None,
    bypass: bool = False,
    auto_db: float = 0.0,
    plugin_paths: dict[str, str] | None = None,
    rate: int = RATE,
    quantum: int | None = None,
) -> str:
    """The text of the filter-chain configuration for these parameters and this denoise engine.

    `engine` is "deepfilternet", "rnnoise" or "off" (no neural stage in the graph). `microphone` is the node name of
    the raw microphone to read; None leaves the connection to the session manager's default source.
    `plugin_paths` overrides where the LADSPA files are (tests). `rate` is the graph's sample rate (`pick_rate`),
    `quantum` the interface profile's node.latency in frames at that rate; the node apps see stays RATE, mono.
    """
    if engine not in (*stages.DENOISE_ENGINES, "off"):
        raise ValueError(f"unknown denoise engine {engine!r}")

    def path(kind: str) -> str:
        if plugin_paths and kind in plugin_paths:
            return plugin_paths[kind]
        found = find_plugin(kind)
        return str(found) if found else PLUGINS[kind]["file"]

    initial = stages.controls(
        params, bypass=bypass, auto_db=auto_db, engine=engine if engine != "off" else "deepfilternet"
    )
    nodes = [
        _node("builtin", "in", "copy", None, initial),
        _node("builtin", stages.HP1, "bq_highpass", None, {**initial, f"{stages.HP1}:Q": stages.HP_Q[0]}),
        _node("builtin", stages.HP2, "bq_highpass", None, {**initial, f"{stages.HP2}:Q": stages.HP_Q[1]}),
        _node("ladspa", stages.GATE, PLUGINS["gate"]["label"], path("gate"), initial),
    ]
    chain = [stages.HP1, stages.HP2, stages.GATE]
    if engine != "off":
        nodes.append(_node("ladspa", stages.DENOISE, PLUGINS[engine]["label"], path(engine), initial))
        chain.append(stages.DENOISE)
    deess_fixed = {
        f"{stages.DEESS}:Sidechain": 0.0,
        f"{stages.DEESS}:Low Shelf": 0.0,
        f"{stages.DEESS}:Peak": 1.0,
        f"{stages.DEESS}:High Shelf": 0.0,
        f"{stages.DEESS}:Boost / Cut": 0.0,
        f"{stages.DEESS}:Target width": 2.0,
        f"{stages.DEESS}:Attack": 2.0,
        f"{stages.DEESS}:Release": 60.0,
        f"{stages.DEESS}:Knee": 4.0,
        f"{stages.DEESS}:Slew": 25.0,
    }
    nodes.append(_node("ladspa", stages.DEESS, PLUGINS["deess"]["label"], path("deess"), {**initial, **deess_fixed}))
    chain.append(stages.DEESS)
    eq_nodes = (
        (stages.EQ_LOW, "bq_lowshelf", 120.0, 0.7),
        (stages.EQ_MUD, "bq_peaking", 300.0, 1.0),
        (stages.EQ_PRES, "bq_peaking", 3500.0, 0.9),
        (stages.EQ_AIR, "bq_highshelf", 9000.0, 0.7),
    )
    for name, label, freq, q in eq_nodes:
        nodes.append(_node("builtin", name, label, None, {**initial, f"{name}:Freq": freq, f"{name}:Q": q}))
        chain.append(name)
    comp_fixed = {f"{stages.COMP}:Makeup": 0.0, f"{stages.COMP}:Sidechain": 0.0, f"{stages.COMP}:Slew": 1.0}
    nodes.append(_node("ladspa", stages.COMP, PLUGINS["comp"]["label"], path("comp"), {**initial, **comp_fixed}))
    chain.append(stages.COMP)
    nodes.append(_node("builtin", stages.GAIN, "linear", None, {**initial, f"{stages.GAIN}:Add": 0.0}))
    chain.append(stages.GAIN)
    nodes.append(_node("ladspa", stages.LIM, PLUGINS["limiter"]["label"], path("limiter"), initial))
    delay = CHAIN_DELAY_S[engine]
    nodes.append(_node("builtin", "dry", "delay", None, {"dry:Delay (s)": delay}, config="max-delay = 0.25"))
    nodes.append(_node("builtin", stages.MIX, "mixer", None, initial))

    def out_port(node: str) -> str:
        key = f"{node}:{engine}" if node == stages.DENOISE else node
        return f"{node}:{IO.get(key, ('In', 'Out'))[1]}"

    def in_port(node: str) -> str:
        key = f"{node}:{engine}" if node == stages.DENOISE else node
        return f"{node}:{IO.get(key, ('In', 'Out'))[0]}"

    links = [("in:Out", in_port(stages.HP1))]
    for left, right in itertools.pairwise(chain):
        links.append((out_port(left), in_port(right)))
    links += [
        (f"{stages.GAIN}:Out", f"{stages.LIM}:Audio Input 1"),
        (f"{stages.GAIN}:Out", f"{stages.LIM}:Audio Input 2"),
        (f"{stages.LIM}:Audio Output 1", f"{stages.MIX}:In 1"),
        ("in:Out", "dry:In"),
        ("dry:Out", f"{stages.MIX}:In 2"),
    ]
    link_text = "\n".join(f"        {{ output = {_q(a)} input = {_q(b)} }}" for a, b in links)

    capture = [
        f"        node.name = {_q(CAPTURE_NAME)}",
        "        node.passive = true",
        "        stream.dont-remix = true",
    ]
    if hint := latency(quantum, rate):
        capture.append(f"        node.latency = {_q(hint)}")
    if microphone:
        # Pin the capture to the Plan's microphone by node name and never let WirePlumber substitute the default source
        # (issue #139: a start before the Wave XLR's source existed linked it to the Cam Link's HDMI audio, which is
        # digital silence). `dont-fallback` alone makes WirePlumber destroy a stream whose target is missing, so
        # `linger` keeps it waiting; it links the moment the microphone appears and relinks if the target returns.
        capture += [
            f"        target.object = {_q(microphone)}",
            "        node.dont-fallback = true",
            "        node.linger = true",
        ]
    playback_hint = latency(quantum and round(quantum * RATE / rate), RATE)
    playback_latency = f"\n                node.latency = {_q(playback_hint)}" if playback_hint else ""
    return f"""# Generated by StreamStack. Edits are overwritten; use the Console or `streamstack chain`.
context.properties = {{
    log.level = 0
}}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt
        args = {{ nice.level = -11 }}
        flags = [ ifexists nofail ]
    }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {_q(DESCRIPTION)}
            media.name = {_q(DESCRIPTION)}
            audio.rate = {rate}
            audio.channels = 1
            audio.position = [ MONO ]
            filter.graph = {{
                nodes = [
{chr(10).join(nodes)}
                ]
                links = [
{link_text}
                ]
                inputs = [ "in:In" ]
                outputs = [ "{stages.MIX}:Out" ]
            }}
            capture.props = {{
{chr(10).join(capture)}
            }}
            playback.props = {{
                node.name = {_q(NODE_NAME)}
                node.description = {_q(DESCRIPTION)}
                media.class = Audio/Source
                audio.rate = {RATE}{playback_latency}
                audio.position = [ MONO ]
            }}
        }}
    }}
]
"""
