#!/usr/bin/env python3
"""Offline AI benchmark: person segmentation and face detection on the Arc GPU versus the CPU (issue #22).

Runs candidate models over a RECORDED clip of each rig and reports, per model and backend, the p50/p95 time per
frame, the CPU cost as a percentage of one core, the Arc GPU busy percentage (intel_gpu_top) and the first-load
compile time with OpenVINO's CACHE_DIR. The table is also written into the decision-gate section of
docs/hardware-ai-evaluation.md (--update-doc).

It never opens a live device: --clip takes a regular file (record one with tools/record_clip.sh, only when no call
is in progress) and --synthetic generates frames in memory. This script is a developer tool and is not shipped in
the wheel. Optional dependencies: `pip install -e '.[bench]'` (numpy, opencv-python-headless, openvino,
onnxruntime, intel_gpu_top from the intel-gpu-tools package for the GPU busy figure). A backend, device or
tool that is not available is reported as skipped with the reason, never as an error.

    nice -n 19 python tools/bench_ai.py --synthetic --backends ort-cpu --iterations 10      # smoke run
    python tools/bench_ai.py --clip canon=canon.mkv --clip camlink=camlink.mkv \\
        --update-doc docs/hardware-ai-evaluation.md
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import importlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from streamstack.framing import model as models

# The decision gate fixed before measuring (docs/hardware-ai-evaluation.md): added p95 frame time under 8 ms, under
# 1 percent total CPU, no dropped frames at the program camera's 30 fps.
GATE_P95_MS = 8.0
GATE_CPU_TOTAL_PCT = 1.0
DEFAULT_FPS = 30.0

RIGS: dict[str, tuple[int, int]] = {"canon": (1056, 704), "camlink": (1920, 1080)}

DOC_BEGIN = "<!-- bench-ai:begin -->"
DOC_END = "<!-- bench-ai:end -->"
KIND_RE = re.compile(r"<!-- bench-ai:kind=(\w+) -->")

SEGMENTATION = "segmentation"
FACE = "face"

OBS_BG_COMMIT = "6f1ffa1ffd9b61a4450382ab1f6137e38a96741f"
OBS_BG_URL = f"https://raw.githubusercontent.com/royshil/obs-backgroundremoval/{OBS_BG_COMMIT}/data/models/"
OBS_BG_SOURCE = "https://github.com/royshil/obs-backgroundremoval/tree/main/data/models"


class BenchError(Exception):
    """A usage problem the operator can fix (bad clip, no frames, bad arguments)."""


class Unavailable(Exception):
    """A backend, device or model is not usable on this host; the benchmark records why and moves on."""


@dataclasses.dataclass(frozen=True)
class BenchModel:
    """A candidate: the pinned file plus how to feed it. Latency only; the outputs are not decoded."""

    spec: models.ModelSpec
    kind: str
    size: tuple[int, int]  # (width, height) used when the model's own input is dynamic
    layout: str  # "nchw" or "nhwc" when the input shape is dynamic
    scale: float  # multiplier applied to 0..255 pixels (1/255 for unit range, 1 for raw bytes)
    rgb: bool  # False feeds BGR as captured
    note: str


def _obs_bg(key: str, name: str, filename: str, sha256: str, size: int, licence: str, source: str) -> models.ModelSpec:
    return models.ModelSpec(
        key=key,
        name=name,
        filename=filename,
        url=OBS_BG_URL + filename,
        sha256=sha256,
        size=size,
        license=licence,
        source=source,
    )


CANDIDATES: tuple[BenchModel, ...] = (
    BenchModel(
        _obs_bg(
            "mediapipe-meet",
            "MediaPipe Meet segmentation",
            "mediapipe.onnx",
            "7f785cf032261a07af7b845f891cab30da3f0757c7b362310e089e3aa8e8860a",
            781150,
            "Apache-2.0 (Copyright 2021 Google LLC, ONNX conversion Katsuya Hyodo)",
            OBS_BG_SOURCE,
        ),
        SEGMENTATION,
        (256, 144),
        "nchw",
        1 / 255,
        True,
        "the model obs-backgroundremoval 1.4.1 selects by default (docs: Design 2)",
    ),
    BenchModel(
        _obs_bg(
            "pphumanseg",
            "PP-HumanSeg",
            "pphumanseg_fp32.onnx",
            "6913acd125be55fd08e76072ed464146925223392954c437f3aa700e4da84b17",
            6196913,
            "Apache-2.0 (Copyright 2021 PaddlePaddle Authors)",
            OBS_BG_SOURCE,
        ),
        SEGMENTATION,
        (192, 192),
        "nchw",
        1 / 255,
        True,
        "PaddlePaddle PP-HumanSeg as shipped by obs-backgroundremoval",
    ),
    BenchModel(
        _obs_bg(
            "sinet",
            "SINet",
            "SINet_Softmax_simple.onnx",
            "17a75acc69747e443717dfaef152ee3c4615f2d81b0269f7c0224c47584af46a",
            395434,
            "MIT (Copyright 2019 Anil Sathyan)",
            OBS_BG_SOURCE,
        ),
        SEGMENTATION,
        (320, 320),
        "nchw",
        1 / 255,
        True,
        "SINet portrait segmenter as shipped by obs-backgroundremoval",
    ),
    BenchModel(
        models.YUNET,
        FACE,
        (320, 320),
        "nchw",
        1.0,
        False,
        "the detector auto-framing uses (streamstack framing --fetch)",
    ),
    BenchModel(
        models.ModelSpec(
            key="blazeface",
            name="BlazeFace",
            filename="blazeface_garavv_a16bb9a.onnx",
            url=(
                "https://huggingface.co/garavv/blazeface-onnx/resolve/a16bb9a2e4c9ce966518ef9b82beaa897dc71b28/"
                "blaze.onnx"
            ),
            sha256="564740c5146673c840257402cee8309161848e48e64d277a862ab4d501adf8a5",
            size=535842,
            license=(
                "none stated by the uploader; the network is Google MediaPipe BlazeFace (Apache-2.0). "
                "Benchmark use only, never shipped"
            ),
            source="https://huggingface.co/garavv/blazeface-onnx",
        ),
        FACE,
        (128, 128),
        "nchw",
        1 / 255,
        True,
        "128x128 export with NMS inside the graph (extra inputs are fed fixed thresholds)",
    ),
)

# Values for the extra scalar inputs some exports carry (BlazeFace's in-graph NMS).
EXTRA_INPUTS: dict[str, float] = {"conf_threshold": 0.5, "iou_threshold": 0.3, "max_detections": 25}

BACKEND_ORDER = ("ort-cpu", "ov-cpu", "ov-gpu")


# -- numpy is optional so the pure helpers import (and test) without it --------------------------------------------


def _numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError as exc:
        raise Unavailable("numpy is not installed (pip install -e '.[bench]')") from exc


def _cv2() -> Any | None:
    try:
        return importlib.import_module("cv2")
    except ImportError:
        return None


# -- statistics -----------------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (pct 0 to 100) of a non-empty sequence."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclasses.dataclass
class Row:
    source: str
    size: str
    synthetic: bool
    model: str
    kind: str
    backend: str
    device: str = ""
    status: str = "ok"  # ok | skipped
    reason: str = ""
    frames: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None
    infer_p50_ms: float | None = None
    cpu_core_pct: float | None = None  # CPU time per frame x fps, as a percentage of one core
    cpu_total_pct: float | None = None  # the same against every logical CPU of the host
    gpu_busy_pct: float | None = None
    gpu_note: str = ""
    compile_cold_ms: float | None = None
    compile_cached_ms: float | None = None
    first_infer_ms: float | None = None
    gate: str = "n/a"  # PASS | FAIL | n/a


def evaluate_gate(row: Row, fps: float, cpus: int) -> str:
    """PASS when p95 < 8 ms, total CPU < 1 percent and the slowest frame fits one frame period; else FAIL."""
    if row.status != "ok" or row.p95_ms is None or row.max_ms is None or row.cpu_core_pct is None:
        return "n/a"
    cpu_total = row.cpu_core_pct / max(cpus, 1)
    row.cpu_total_pct = cpu_total
    ok = row.p95_ms < GATE_P95_MS and cpu_total < GATE_CPU_TOTAL_PCT and row.max_ms < 1000.0 / fps
    return "PASS" if ok else "FAIL"


# -- frames ---------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Source:
    label: str
    width: int
    height: int
    frames: list[Any]  # HxWx3 uint8 BGR
    synthetic: bool


def synthetic_frames(width: int, height: int, count: int = 8) -> list[Any]:
    """A moving oval 'person' on a gradient with sensor-like noise, so models see real-sized, varying pixels."""
    np = _numpy()
    rng = np.random.default_rng(22)
    ys = np.linspace(40, 200, height, dtype=np.float32)[:, None]
    xs = np.linspace(60, 180, width, dtype=np.float32)[None, :]
    background = np.stack([ys + 0 * xs, 0.5 * (ys + xs), xs + 0 * ys], axis=2)
    yy, xx = np.mgrid[0:height, 0:width]
    frames = []
    for index in range(count):
        cx = width * (0.4 + 0.2 * index / max(count - 1, 1))
        cy = height * 0.55
        oval = ((xx - cx) / (width * 0.16)) ** 2 + ((yy - cy) / (height * 0.34)) ** 2 < 1.0
        head = ((xx - cx) / (width * 0.07)) ** 2 + ((yy - (cy - height * 0.30)) / (height * 0.10)) ** 2 < 1.0
        frame = background.copy()
        frame[oval] = (70, 90, 150)
        frame[head] = (150, 180, 220)
        frame += rng.normal(0, 4, frame.shape).astype(np.float32)
        frames.append(np.clip(frame, 0, 255).astype(np.uint8))
    return frames


def load_clip(path: Path, max_frames: int) -> tuple[int, int, list[Any]]:
    """Decode up to max_frames from a regular file. Never a device node, never a URL."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BenchError(f"cannot read the clip {path}: {exc.strerror or exc}") from exc
    if not resolved.is_file():
        raise BenchError(f"{path} is not a regular file; the benchmark only reads recorded clips, never a live device")
    cv2 = _cv2()
    if cv2 is None:
        raise BenchError("reading a clip needs OpenCV (pip install -e '.[bench]')")
    capture = cv2.VideoCapture(str(resolved))
    frames: list[Any] = []
    try:
        if not capture.isOpened():
            raise BenchError(f"OpenCV cannot open {path} (is it a video file?)")
        while len(frames) < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    if not frames:
        raise BenchError(f"{path} has no decodable frames")
    height, width = frames[0].shape[:2]
    return width, height, frames


def parse_size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", text.strip())
    if not match:
        raise BenchError(f"bad size {text!r}; expected WIDTHxHEIGHT such as 1056x704")
    return int(match.group(1)), int(match.group(2))


def build_sources(args: argparse.Namespace) -> list[Source]:
    sources: list[Source] = []
    if args.synthetic:
        override = parse_size(args.size) if args.size else None
        rigs = {"synthetic": override} if override else dict(RIGS)
        for label, size in rigs.items():
            if size is None:
                continue
            sources.append(Source(label, size[0], size[1], synthetic_frames(*size), True))
        return sources
    if not args.clip:
        raise BenchError("give --clip LABEL=PATH (one per rig) or --synthetic")
    for item in args.clip:
        label, sep, raw = item.partition("=")
        if not sep:
            label, raw = Path(item).stem, item
        width, height, frames = load_clip(Path(raw), args.max_frames)
        sources.append(Source(label, width, height, frames, False))
    return sources


# -- preprocessing --------------------------------------------------------------------------------------------------


def resize(frame: Any, width: int, height: int) -> Any:
    cv2 = _cv2()
    if cv2 is not None:
        return cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)
    np = _numpy()
    rows = np.arange(height) * frame.shape[0] // height
    cols = np.arange(width) * frame.shape[1] // width
    return frame[rows][:, cols]


def preprocess(frame: Any, bench: BenchModel, width: int, height: int, layout: str) -> Any:
    """Resize, order the channels, scale to float32 and lay out as the model wants: the per-frame input cost."""
    np = _numpy()
    small = resize(frame, width, height)
    if bench.rgb:
        small = small[:, :, ::-1]
    data = small.astype(np.float32) * np.float32(bench.scale)
    if layout == "nchw":
        data = data.transpose(2, 0, 1)
    return np.ascontiguousarray(data[None])


def input_geometry(shape: Sequence[int | None], bench: BenchModel) -> tuple[int, int, str]:
    """(width, height, layout) from a 4-D input shape, falling back to the candidate's defaults when dynamic."""
    dims = list(shape)
    if len(dims) == 4:
        if dims[1] == 3 and all(isinstance(d, int) and d > 0 for d in (dims[2], dims[3])):
            return int(dims[3] or 0), int(dims[2] or 0), "nchw"
        if dims[3] == 3 and all(isinstance(d, int) and d > 0 for d in (dims[1], dims[2])):
            return int(dims[2] or 0), int(dims[1] or 0), "nhwc"
    return bench.size[0], bench.size[1], bench.layout


def extra_input(name: str, dtype: Any, shape: Sequence[int | None]) -> Any:
    np = _numpy()
    dims = [d if isinstance(d, int) and d > 0 else 1 for d in shape] or [1]
    return np.full(dims, EXTRA_INPUTS.get(name, 0), dtype=dtype)


# -- backends -------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Runner:
    """A compiled model: call it with the preprocessed frame."""

    run: Callable[[Any], None]
    width: int
    height: int
    layout: str
    device: str
    compile_cold_ms: float
    compile_cached_ms: float | None
    close: Callable[[], None] = lambda: None


def _ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _ort_dtype(type_name: str) -> Any:
    np = _numpy()
    table = {"tensor(float)": np.float32, "tensor(int64)": np.int64, "tensor(int32)": np.int32}
    return table.get(type_name, np.float32)


def open_ort_cpu(path: Path, bench: BenchModel, opts: argparse.Namespace) -> Runner:
    try:
        ort = importlib.import_module("onnxruntime")
    except ImportError as exc:
        raise Unavailable("onnxruntime is not installed (pip install -e '.[bench]')") from exc
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = opts.threads
    session_options.inter_op_num_threads = 1
    session_options.log_severity_level = 3  # the portrait models warn about initialisers on every load
    start = time.perf_counter()
    try:
        session = ort.InferenceSession(str(path), session_options, providers=["CPUExecutionProvider"])
    except Exception as exc:  # onnxruntime raises its own ORTException family, with no common public base
        raise Unavailable(f"onnxruntime could not load {path.name}: {exc}") from exc
    compile_ms = _ms_since(start)
    inputs = session.get_inputs()
    width, height, layout = input_geometry(inputs[0].shape, bench)
    extras = {i.name: extra_input(i.name, _ort_dtype(i.type), i.shape) for i in inputs[1:]}
    first = inputs[0].name

    def run(data: Any) -> None:
        session.run(None, {first: data, **extras})

    return Runner(run, width, height, layout, "CPUExecutionProvider", compile_ms, None)


def _ov_core() -> Any:
    try:
        return importlib.import_module("openvino").Core()
    except ImportError as exc:
        raise Unavailable("openvino is not installed (pip install -e '.[bench]')") from exc


def open_openvino(path: Path, bench: BenchModel, opts: argparse.Namespace, device: str) -> Runner:
    core = _ov_core()
    if device == "CPU":
        wanted = "CPU"
    else:
        listed = list(core.available_devices)
        # OpenVINO lists a lone GPU as "GPU" (GPU.0 is still accepted); several as GPU.0, GPU.1, ...
        if device not in listed and not (device == "GPU.0" and "GPU" in listed):
            raise Unavailable(f"OpenVINO sees no {device} (devices: {', '.join(listed) or 'none'})")
        wanted = device
    try:
        network = core.read_model(str(path))
        first = network.input(0)
        shape = first.get_partial_shape()
        if shape.is_dynamic:
            width, height, layout = input_geometry([], bench)
            dims = [1, 3, height, width] if layout == "nchw" else [1, height, width, 3]
            network.reshape({first: dims})
        shape_now = [d.get_length() for d in network.input(0).get_partial_shape()]
    except Exception as exc:  # openvino raises RuntimeError/TypeError variants for unreadable or unsupported models
        raise Unavailable(f"OpenVINO could not read {path.name}: {exc}") from exc
    width, height, layout = input_geometry(shape_now, bench)
    config_props: dict[str, Any] = {"CACHE_DIR": str(opts.cache_dir), "PERFORMANCE_HINT": "LATENCY"}
    if device == "CPU":
        config_props["INFERENCE_NUM_THREADS"] = opts.threads

    def compile_once() -> tuple[Any, float]:
        start = time.perf_counter()
        try:
            compiled = core.compile_model(network, wanted, config_props)
        except Exception as exc:  # the plugin reports unsupported ops and device faults as RuntimeError
            raise Unavailable(f"OpenVINO could not compile {path.name} for {wanted}: {exc}") from exc
        return compiled, _ms_since(start)

    _, cold_ms = compile_once()  # fills CACHE_DIR when it was empty
    compiled, cached_ms = compile_once()  # what a restarted StreamStack pays
    request = compiled.create_infer_request()
    names = [port.get_any_name() for port in compiled.inputs]
    extras: dict[str, Any] = {}
    for port in compiled.inputs[1:]:
        extras[port.get_any_name()] = extra_input(
            port.get_any_name(),
            port.get_element_type().to_dtype(),
            [d.get_length() if d.is_static else 1 for d in port.get_partial_shape()],
        )

    def run(data: Any) -> None:
        request.infer({names[0]: data, **extras})

    try:
        full_name = str(core.get_property(wanted, "FULL_DEVICE_NAME"))
    except Exception:  # a device without the property is still benchmarkable; only the label is lost
        full_name = wanted
    return Runner(run, width, height, layout, f"{wanted} ({full_name})", cold_ms, cached_ms)


def open_backend(name: str, path: Path, bench: BenchModel, opts: argparse.Namespace) -> Runner:
    if name == "ort-cpu":
        return open_ort_cpu(path, bench, opts)
    if name == "ov-cpu":
        return open_openvino(path, bench, opts, "CPU")
    if name == "ov-gpu":
        return open_openvino(path, bench, opts, opts.gpu_device)
    raise BenchError(f"unknown backend {name!r}; choose from {', '.join(BACKEND_ORDER)}")


# -- GPU busy -------------------------------------------------------------------------------------------------------


def parse_gpu_top(text: str) -> list[dict[str, Any]]:
    """The samples in intel_gpu_top -J output: a JSON array that is still open when the tool is interrupted."""
    decoder = json.JSONDecoder()
    samples: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        if text[index] in "[], \r\n\t":
            index += 1
            continue
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break  # the last sample was cut off by the interrupt
        if isinstance(value, dict):
            samples.append(value)
        elif isinstance(value, list):
            samples.extend(v for v in value if isinstance(v, dict))
    return samples


def gpu_busy(samples: Sequence[dict[str, Any]]) -> tuple[float, str] | None:
    """(busiest engine's mean busy percent, its name), ignoring the first sample (it spans the tool start-up)."""
    usable = list(samples[1:] if len(samples) > 1 else samples)
    totals: dict[str, list[float]] = {}
    for sample in usable:
        engines = sample.get("engines")
        if not isinstance(engines, dict):
            continue
        for name, values in engines.items():
            busy = values.get("busy") if isinstance(values, dict) else None
            if isinstance(busy, int | float):
                totals.setdefault(str(name), []).append(float(busy))
    if not totals:
        return None
    name, values = max(totals.items(), key=lambda item: sum(item[1]) / len(item[1]))
    return sum(values) / len(values), name


@contextlib.contextmanager
def gpu_top(enabled: bool, device: str | None, result: dict[str, Any]) -> Iterator[None]:
    """Sample intel_gpu_top while the body runs; fills result with busy/note. Skips cleanly without the tool."""
    if not enabled:
        yield
        return
    binary = shutil.which("intel_gpu_top")
    if binary is None:
        result["note"] = "intel_gpu_top not installed (intel-gpu-tools)"
        yield
        return
    command = [binary, "-J", "-s", "250"] + (["-d", device] if device else [])
    with tempfile.TemporaryFile("w+") as out:
        try:
            process = subprocess.Popen(command, stdout=out, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            result["note"] = f"intel_gpu_top could not start: {exc.strerror or exc}"
            yield
            return
        try:
            yield
        finally:
            process.send_signal(signal.SIGINT)
            try:
                _, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate()
            out.seek(0)
            found = gpu_busy(parse_gpu_top(out.read()))
            if found is None:
                detail = (stderr or "").strip().splitlines()
                result["note"] = "intel_gpu_top gave no samples" + (f": {detail[-1]}" if detail else "")
            else:
                result["busy"], result["engine"] = found


# -- the measurement ------------------------------------------------------------------------------------------------


def measure(runner: Runner, bench: BenchModel, source: Source, opts: argparse.Namespace, row: Row) -> None:
    frames = source.frames
    count = len(frames)

    def feed(index: int) -> tuple[float, float]:
        started = time.perf_counter()
        data = preprocess(frames[index % count], bench, runner.width, runner.height, runner.layout)
        mid = time.perf_counter()
        runner.run(data)
        done = time.perf_counter()
        return (done - started) * 1000.0, (done - mid) * 1000.0

    first_total, _ = feed(0)
    row.first_infer_ms = first_total
    for index in range(opts.warmup):
        feed(index)

    totals: list[float] = []
    infers: list[float] = []
    gpu: dict[str, Any] = {}
    wanted_gpu = row.backend == "ov-gpu"
    with gpu_top(wanted_gpu and not opts.no_gpu_top, opts.gpu_top_device, gpu):
        cpu_start = time.process_time()
        for index in range(opts.iterations):
            total, infer = feed(index)
            totals.append(total)
            infers.append(infer)
        cpu_used = time.process_time() - cpu_start
    row.frames = len(totals)
    row.p50_ms = percentile(totals, 50)
    row.p95_ms = percentile(totals, 95)
    row.max_ms = max(totals)
    row.infer_p50_ms = percentile(infers, 50)
    row.cpu_core_pct = cpu_used / max(len(totals), 1) * opts.fps * 100.0
    row.compile_cold_ms = runner.compile_cold_ms
    row.compile_cached_ms = runner.compile_cached_ms
    if wanted_gpu:
        if "busy" in gpu:
            row.gpu_busy_pct = gpu["busy"]
            row.gpu_note = f"engine {gpu['engine']}, saturated back-to-back, not paced at {opts.fps:g} fps"
        else:
            row.gpu_note = gpu.get("note", "")


def run_matrix(sources: Sequence[Source], opts: argparse.Namespace) -> list[Row]:
    cpus = os.cpu_count() or 1
    rows: list[Row] = []
    selected = [m for m in CANDIDATES if not opts.models or m.spec.key in opts.models]
    for bench in selected:
        path: Path | None = None
        fetch_error = ""
        try:
            path = models.fetch(bench.spec) if opts.fetch else models.verify(spec=bench.spec)
        except models.ModelError as exc:
            fetch_error = str(exc).replace("the face model", bench.spec.name)
            if not opts.fetch:
                fetch_error += " (or run with --fetch)"
        for backend in opts.backends:
            for source in sources:
                row = Row(
                    source=source.label,
                    size=f"{source.width}x{source.height}",
                    synthetic=source.synthetic,
                    model=bench.spec.key,
                    kind=bench.kind,
                    backend=backend,
                )
                rows.append(row)
                if path is None:
                    row.status, row.reason = "skipped", fetch_error
                    continue
                try:
                    runner = open_backend(backend, path, bench, opts)
                except Unavailable as exc:
                    row.status, row.reason = "skipped", str(exc)
                    continue
                try:
                    row.device = runner.device
                    measure(runner, bench, source, opts, row)
                except Exception as exc:  # a model that compiles but fails to run is reported, not fatal to the matrix
                    row.status, row.reason = "skipped", f"failed while running: {type(exc).__name__}: {exc}"
                finally:
                    runner.close()
                print(f"  {source.label} {bench.spec.key} {backend}: {row.status}", file=sys.stderr, flush=True)
                verdict = evaluate_gate(row, opts.fps, cpus)  # also fills cpu_total_pct
                row.gate = "n/a" if row.synthetic else verdict  # placeholders never pass or fail a gate
    return rows


# -- reporting ------------------------------------------------------------------------------------------------------


def cell_text(text: str, limit: int = 200) -> str:
    """One line that cannot break a markdown table: the first and last lines of an error, pipes escaped."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    flat = (lines[0] if len(lines) < 2 else f"{lines[0]} ... {lines[-1]}") if lines else "unavailable"
    flat = flat.replace("|", "/")
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def host_info(core_devices: Callable[[], list[str]] | None = None) -> dict[str, Any]:
    def version(package: str) -> str:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return "not installed"

    devices: list[str] = []
    try:
        devices = (core_devices or (lambda: list(_ov_core().available_devices)))()
    except Unavailable:
        devices = []
    cpu_model = ""
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    return {
        "host": platform.node(),
        "cpu": cpu_model or platform.processor(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "openvino": version("openvino"),
        "onnxruntime": version("onnxruntime"),
        "numpy": version("numpy"),
        "opencv": next(
            (v for v in map(version, ("opencv-python-headless", "opencv-python")) if v != "not installed"),
            "not installed",
        ),
        "openvino_devices": devices,
    }


def markdown_table(rows: Sequence[Row], source: str) -> str:
    header = (
        "| Model | Backend | p50 ms | p95 ms | Infer p50 ms | CPU % of 1 core | GPU busy % | Compile cold / cached ms "
        "| First frame ms | Gate |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [header]
    for row in rows:
        if row.source != source:
            continue
        if row.status != "ok":
            lines.append(
                f"| {row.model} | {row.backend} | - | - | - | - | - | - | - | skipped: {cell_text(row.reason)} |"
            )
            continue
        gpu = _fmt(row.gpu_busy_pct, 0) if row.backend == "ov-gpu" else "-"
        compile_text = (
            f"{_fmt(row.compile_cold_ms, 0)} / {_fmt(row.compile_cached_ms, 0)}"
            if row.compile_cached_ms is not None
            else f"{_fmt(row.compile_cold_ms, 0)} (no cache)"
        )
        lines.append(
            f"| {row.model} | {row.backend} | {_fmt(row.p50_ms)} | {_fmt(row.p95_ms)} | {_fmt(row.infer_p50_ms)} "
            f"| {_fmt(row.cpu_core_pct, 1)} | {gpu} | {compile_text} | {_fmt(row.first_infer_ms, 1)} "
            f"| {row.gate or 'n/a'} |"
        )
    return "\n".join(lines)


def build_report(
    rows: Sequence[Row], opts: argparse.Namespace, host: dict[str, Any], when: dt.datetime
) -> dict[str, Any]:
    return {
        "schema": 1,
        "issue": "Curiosity-Corp/streamstack#22",
        "generated": when.isoformat(timespec="seconds"),
        "synthetic": bool(opts.synthetic),
        "settings": {
            "iterations": opts.iterations,
            "warmup": opts.warmup,
            "threads": opts.threads,
            "fps": opts.fps,
            "gpu_device": opts.gpu_device,
            "gates": {
                "p95_ms_below": GATE_P95_MS,
                "cpu_total_pct_below": GATE_CPU_TOTAL_PCT,
                "no_drops_at_fps": opts.fps,
            },
        },
        "host": host,
        "models": {
            m.spec.key: {"name": m.spec.name, "license": m.spec.license, "url": m.spec.url, "sha256": m.spec.sha256}
            for m in CANDIDATES
        },
        "rows": [dataclasses.asdict(r) for r in rows],
    }


def doc_block(rows: Sequence[Row], opts: argparse.Namespace, host: dict[str, Any], when: dt.datetime) -> str:
    synthetic = bool(opts.synthetic)
    kind = "synthetic" if synthetic else "recorded"
    sources = list(dict.fromkeys(r.source for r in rows))
    lines = [DOC_BEGIN, f"<!-- bench-ai:kind={kind} -->"]
    if synthetic:
        lines += [
            "> **SYNTHETIC PLACEHOLDER. These are not measurements of the rigs.** They come from generated frames on",
            "> the CPU of whatever machine ran the harness (`--synthetic`), only prove the harness works, and decide",
            "> nothing. The gate column is `n/a` on purpose. Replace them by running the quiet-host steps below.",
            "",
        ]
    else:
        lines += [
            f"Measured {when.date().isoformat()} on {host.get('host') or 'unknown host'} from recorded clips.",
            "",
        ]
    lines += [
        f"Host: {host.get('cpu') or 'unknown CPU'}, {host.get('logical_cpus')} logical CPUs; OpenVINO "
        f"{host.get('openvino')}, ONNX Runtime {host.get('onnxruntime')}; OpenVINO devices: "
        f"{', '.join(host.get('openvino_devices') or []) or 'none seen'}. {opts.iterations} timed frames after "
        f"{opts.warmup} warm-up, {opts.threads} CPU threads, CPU cost scaled to {opts.fps:g} fps.",
        "",
    ]
    for source in sources:
        size = next(r.size for r in rows if r.source == source)
        lines += [f"**{source}** ({size}{', generated' if synthetic else ''})", "", markdown_table(rows, source), ""]
    lines += [
        "Gate: p95 under 8 ms, under 1 percent total CPU (CPU % of one core divided by the logical CPUs), slowest "
        "frame under one frame period. `ov-gpu` runs on the configured OpenVINO GPU device; check the device name "
        "in the JSON, because on a host with an iGPU as well as the Arc the Arc may be `GPU.1`.",
        DOC_END,
    ]
    return "\n".join(lines)


def update_doc(path: Path, block: str, *, synthetic: bool) -> None:
    """Replace the marked block in the doc. A synthetic run never overwrites recorded numbers."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise BenchError(f"cannot read {path}: {exc.strerror or exc}") from exc
    start, end = text.find(DOC_BEGIN), text.find(DOC_END)
    if start < 0 or end < start:
        raise BenchError(f"{path} has no {DOC_BEGIN} ... {DOC_END} block to update")
    existing = KIND_RE.search(text[start:end])
    if synthetic and existing and existing.group(1) == "recorded":
        raise BenchError(f"{path} already holds recorded numbers; refusing to replace them with synthetic ones")
    updated = text[:start] + block + text[end + len(DOC_END) :]
    try:
        path.write_text(updated)
    except OSError as exc:
        raise BenchError(f"cannot write {path}: {exc.strerror or exc}") from exc


# -- command line ---------------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench_ai.py",
        description="Benchmark person segmentation and face detection on a recorded clip (never a live device).",
    )
    src = parser.add_argument_group("input")
    src.add_argument("--clip", action="append", metavar="[LABEL=]PATH", help="a recorded clip file; repeat per rig")
    src.add_argument("--synthetic", action="store_true", help="generate frames instead (a placeholder, for CI)")
    src.add_argument("--size", metavar="WxH", help="with --synthetic: one generated size instead of both rigs")
    src.add_argument("--max-frames", type=int, default=90, help="frames decoded from each clip (default 90)")
    run = parser.add_argument_group("measurement")
    run.add_argument("--iterations", type=int, default=200, help="timed frames per model and backend (default 200)")
    run.add_argument("--warmup", type=int, default=10)
    run.add_argument("--threads", type=int, default=2, help="CPU threads per model (plugin default: 2)")
    run.add_argument("--fps", type=float, default=DEFAULT_FPS, help="frame rate the CPU cost is scaled to")
    run.add_argument("--backends", default=",".join(BACKEND_ORDER), help=f"comma list of {', '.join(BACKEND_ORDER)}")
    run.add_argument(
        "--models",
        default="",
        help="comma list of model keys (default all): " + ", ".join(m.spec.key for m in CANDIDATES),
    )
    run.add_argument(
        "--gpu-device", default="GPU.0", help="OpenVINO device for ov-gpu (default GPU.0; the Arc may be GPU.1)"
    )
    run.add_argument("--gpu-top-device", default=None, help="intel_gpu_top -d filter, e.g. drm:/dev/dri/card1")
    run.add_argument("--no-gpu-top", action="store_true", help="do not sample intel_gpu_top")
    run.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="OpenVINO CACHE_DIR (default: a fresh temp dir, so the first compile is cold)",
    )
    run.add_argument("--fetch", action="store_true", help="download missing models (pinned URL, sha256 checked)")
    out = parser.add_argument_group("output")
    out.add_argument("--json", type=Path, metavar="PATH", help="write the full results as JSON")
    out.add_argument("--update-doc", type=Path, metavar="PATH", help="rewrite the bench-ai block in this markdown file")
    out.add_argument(
        "--list-models", action="store_true", help="print the pinned models, licences and checksums, then exit"
    )
    args = parser.parse_args(argv)
    args.backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    args.models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [b for b in args.backends if b not in BACKEND_ORDER]
    if unknown:
        parser.error(f"unknown backend(s) {', '.join(unknown)}; choose from {', '.join(BACKEND_ORDER)}")
    known = {m.spec.key for m in CANDIDATES}
    bad = [m for m in args.models if m not in known]
    if bad:
        parser.error(f"unknown model(s) {', '.join(bad)}; choose from {', '.join(sorted(known))}")
    for name in ("iterations", "threads", "max_frames"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.warmup < 0 or args.fps <= 0:
        parser.error("--warmup must be 0 or more and --fps above 0")
    if args.synthetic and args.clip:
        parser.error("use --clip or --synthetic, not both")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_models:
        for m in CANDIDATES:
            print(f"{m.spec.key}\t{m.kind}\t{m.spec.sha256}\t{m.spec.license}\n  {m.spec.url}")
        return 0
    try:
        sources = build_sources(args)
    except (BenchError, Unavailable) as exc:
        print(f"bench_ai: {exc}", file=sys.stderr)
        return 2
    cleanup: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
    if args.cache_dir is None:
        cleanup = tempfile.TemporaryDirectory(prefix="bench-ai-cache-")
    with cleanup as tmp:
        args.cache_dir = Path(tmp) if tmp else args.cache_dir
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        rows = run_matrix(sources, args)
    host = host_info()
    when = dt.datetime.now(dt.UTC).astimezone()
    for source in dict.fromkeys(r.source for r in rows):
        size = next(r.size for r in rows if r.source == source)
        print(f"\n{source} ({size}{', synthetic' if args.synthetic else ''})\n")
        print(markdown_table(rows, source))
    try:
        if args.json:
            args.json.write_text(json.dumps(build_report(rows, args, host, when), indent=2) + "\n")
        if args.update_doc:
            update_doc(args.update_doc, doc_block(rows, args, host, when), synthetic=bool(args.synthetic))
    except OSError as exc:
        print(f"bench_ai: cannot write output: {exc.strerror or exc}", file=sys.stderr)
        return 1
    except BenchError as exc:
        print(f"bench_ai: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
