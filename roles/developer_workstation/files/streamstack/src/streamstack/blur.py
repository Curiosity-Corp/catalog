"""Background blur: the obs-backgroundremoval OBS plugin as a disabled-by-default filter on the camera sources.

Nothing here touches OBS. This module knows what the plugin is called, finds out (from files and the OBS log,
never by loading anything) whether it is installed and what it can run on, defines the low-latency filter
settings StreamStack uses, and judges the filter's per-frame cost against the gate in
docs/hardware-ai-evaluation.md. The obs-websocket side lives in `streamstack.console.blur`.

Every id and setting key below was read from the plugin source at tag 1.4.1
(https://github.com/royshil/obs-backgroundremoval/tree/1.4.1), not guessed:

    src/background-filter-info.c   .id = "background_removal"             the filter kind OBS lists
    src/consts.h                   MODEL_*, USEGPU_* values
    src/background-filter.cpp      background_filter_defaults(), background_filter_properties(),
                                   background_filter_update()             the setting keys and their ranges
    src/plugin-main.c              obs_log "Plugin loaded successfully (version %s)"

Setting keys used (type, range, plugin default):

    useGPU                        string  "cpu" | "cuda" | "rocm" | "migraphx" | "tensorrt" (| "coreml")   "cpu"
    numThreads                    int     0..8      the README says two threads work best              1
    model_select                  string  a models/*.ort path from consts.h                           MediaPipe
    mask_every_x_frames           int     1..300    run the network on every Nth frame, reuse the mask  1
    enable_image_similarity       bool    skip the network when the picture is nearly unchanged (PSNR)  true
    image_similarity_threshold    double  0..100    PSNR in dB above which a frame counts as unchanged  35.0
    temporal_smooth_factor        double  0..1      blend of the new mask with the last one             0.85
    enable_threshold              bool    binarise the mask                                           true
    threshold                     double  0..1                                                        0.5
    contour_filter                double  0..1      drop mask blobs smaller than this share of the image 0.05
    smooth_contour                double  0..1      morphological edge smoothing                        0.5
    feather                       double  0..1      edge feathering (kernel is int(40 * feather) px)    0.0
    mask_expansion                int     -30..30                                                       0
    blur_background               int     0..20     Kawase blur passes; 0 means "remove the background"  0
    enable_focal_blur             bool    depth-of-field style blur                                   false
    stop_when_source_is_inactive  bool    no inference while the source is not on air                  true
    advanced                      bool    only decides which settings the OBS dialog shows            false

There is deliberately no resolution key: the network input size is fixed by the model file. `blur_background`
0 turns the filter into background *removal* (transparent), so StreamStack never writes it below 1.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import config

PLUGIN_NAME = "obs-backgroundremoval"
LIBRARY = f"{PLUGIN_NAME}.so"
FILTER_KIND = "background_removal"  # background-filter-info.c .id
FILTER_NAME = "Background Blur"  # StreamStack's handle for the filter on each camera source
MODEL = "models/mediapipe.with_runtime_opt.ort"  # consts.h MODEL_MEDIAPIPE (README: PINTO model 082, Meet segmentation)
INSTALL_HINT = (
    "install the plugin with the workstation role (roles/developer_workstation in Curiosity-Corp/catalog, "
    "issue #100), then restart OBS"
)

MIN_STRENGTH, MAX_STRENGTH, DEFAULT_STRENGTH = 1, 20, 8  # blur_background: 0 would remove the background
MIN_RATE, MAX_RATE, DEFAULT_RATE = 1, 6, 2  # mask_every_x_frames: 2 is 15 inferences per second at 30 fps
RATE_PRESETS = {"light": 3, "balanced": 2, "smooth": 1}

# The low-latency settings shipped in scenes.json and written when the filter has to be created in a running OBS.
LOW_LATENCY: dict[str, bool | int | float | str] = {
    "advanced": True,
    "useGPU": "cpu",
    "numThreads": 2,
    "model_select": MODEL,
    "mask_every_x_frames": DEFAULT_RATE,
    "enable_image_similarity": True,
    "image_similarity_threshold": 35.0,
    "temporal_smooth_factor": 0.85,
    "enable_threshold": True,
    "threshold": 0.5,
    "contour_filter": 0.05,
    "smooth_contour": 0.3,
    "feather": 0.05,
    "mask_expansion": 0,
    "enable_focal_blur": False,
    "stop_when_source_is_inactive": True,
    "blur_background": DEFAULT_STRENGTH,
}

# Inference devices the plugin source can name (consts.h USEGPU_*), with the symbol a build that supports one
# imports from ONNX Runtime. OpenVINO is not an upstream value: it only shows that a carried build (issue #26) is here.
_DEVICE_SYMBOLS: dict[str, tuple[bytes, ...]] = {
    "cuda": (b"OrtSessionOptionsAppendExecutionProvider_CUDA",),
    "rocm": (b"OrtSessionOptionsAppendExecutionProvider_ROCM",),
    "migraphx": (b"OrtSessionOptionsAppendExecutionProvider_MIGraphX",),
    "tensorrt": (b"OrtSessionOptionsAppendExecutionProvider_Tensorrt",),
    "openvino": (b"OrtSessionOptionsAppendExecutionProvider_OpenVINO", b"OpenVINOExecutionProvider"),
}
DEVICE_LABELS = {
    "cpu": "CPU",
    "cuda": "CUDA GPU",
    "rocm": "ROCm GPU",
    "migraphx": "MIGraphX GPU",
    "tensorrt": "TensorRT GPU",
    "openvino": "OpenVINO (Arc GPU)",
}
LIBRARY_SCAN_BYTES = 64 * 1024 * 1024  # the plugin is a few MB; a bound keeps a wrong file from being read whole
DPKG_STATUS = Path("/var/lib/dpkg/status")
LOG_HEAD_BYTES = 262144

# The per-frame cost gate (docs/hardware-ai-evaluation.md): decided before measuring.
GATE_P95_MS = 8.0
GATE_CPU_PERCENT = 1.0
MIN_ACTIVE_SAMPLES = 10
MIN_BASELINE_SAMPLES = 5
WINDOW_SAMPLES = 60


class BlurError(ValueError):
    """A blur setting is out of range (the message says what is allowed)."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _whole(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BlurError(f"{name} must be a whole number from {low} to {high}.")
    if isinstance(value, float) and (value != value or not value.is_integer()):  # NaN or fractional
        raise BlurError(f"{name} must be a whole number from {low} to {high}.")
    number = int(value)
    if not low <= number <= high:
        raise BlurError(f"{name} must be from {low} to {high}.")
    return number


def validate_strength(value: object) -> int:
    return _whole(value, "Blur strength", MIN_STRENGTH, MAX_STRENGTH)


def validate_rate(value: object) -> int:
    return _whole(value, "Mask rate", MIN_RATE, MAX_RATE)


def low_latency_settings() -> dict[str, bool | int | float | str]:
    return dict(LOW_LATENCY)


def settings_patch(strength: int | None = None, rate: int | None = None) -> dict[str, int]:
    """The plugin keys that change when the Console moves the strength or the mask rate."""
    patch: dict[str, int] = {}
    if strength is not None:
        patch["blur_background"] = validate_strength(strength)
    if rate is not None:
        patch["mask_every_x_frames"] = validate_rate(rate)
    return patch


def read_settings(settings: Mapping[str, object]) -> tuple[int, int]:
    """(strength, rate) from a filter's settings, clamped into what StreamStack offers."""

    def whole(key: str, low: int, high: int, default: int) -> int:
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return default
        return max(low, min(high, int(value)))

    return (
        whole("blur_background", MIN_STRENGTH, MAX_STRENGTH, DEFAULT_STRENGTH),
        whole("mask_every_x_frames", MIN_RATE, MAX_RATE, DEFAULT_RATE),
    )


# ---------------------------------------------------------------------------
# What is installed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plugin:
    """What the files and the OBS log say about obs-backgroundremoval on this machine."""

    installed: bool
    version: str | None = None
    library: str | None = None
    models_ok: bool = False
    loaded: bool | None = None  # the OBS log says it loaded (None: no log line either way)
    devices: tuple[str, ...] = ("cpu",)

    @property
    def usable(self) -> bool:
        return self.installed and self.models_ok

    @property
    def device_labels(self) -> list[str]:
        return [DEVICE_LABELS.get(d, d) for d in self.devices]

    def public(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "version": self.version,
            "models_ok": self.models_ok,
            "loaded": self.loaded,
            "devices": list(self.devices),
            "device_labels": self.device_labels,
            "device": "cpu",  # the only value the plugin accepts on every build; a GPU backend is issue #26
            "hint": "" if self.usable else INSTALL_HINT,
        }


def _multiarch_dirs(root: str, tail: str) -> list[Path]:
    base = Path(root)
    found = [base / tail]
    try:
        found += sorted(p / tail for p in base.iterdir() if p.is_dir() and p.name.endswith("-linux-gnu"))
    except OSError:
        pass  # no such prefix on this machine
    return found


def library_dirs() -> list[Path]:
    """Where OBS looks for plugins (obs-nix.c), plus the per-user plugin folder."""
    dirs: list[Path] = []
    for root in ("/usr/local/lib", "/usr/lib", "/usr/lib64"):
        dirs += _multiarch_dirs(root, "obs-plugins")
    dirs.append(config._xdg("XDG_CONFIG_HOME", ".config") / "obs-studio/plugins" / PLUGIN_NAME / "bin/64bit")
    return dirs


def data_dirs() -> list[Path]:
    dirs = [Path("/usr/local/share/obs/obs-plugins") / PLUGIN_NAME, Path("/usr/share/obs/obs-plugins") / PLUGIN_NAME]
    dirs.append(config._xdg("XDG_CONFIG_HOME", ".config") / "obs-studio/plugins" / PLUGIN_NAME / "data")
    return dirs


def dpkg_version(status: Path = DPKG_STATUS) -> str | None:
    """The installed obs-backgroundremoval package version from the dpkg database, without the build suffix."""
    try:
        text = status.read_text(errors="replace")
    except OSError:
        return None
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(" "))
        if fields.get("Package") == PLUGIN_NAME and fields.get("Status", "").endswith(" installed"):
            return clean_version(fields.get("Version", ""))
    return None


def clean_version(text: str) -> str | None:
    """`1.4.1~forky` and `1:1.4.1-2` both become `1.4.1`."""
    match = re.search(r"\d+(?:\.\d+)+", text)
    return match.group(0) if match else None


_LOADED = re.compile(r"\[" + re.escape(PLUGIN_NAME) + r"\] Plugin loaded successfully \(version ([^)\s]+)\)")


def log_evidence(log_dir: Path | None = None) -> tuple[bool | None, str | None]:
    """(loaded, version) from the newest OBS log: True with the version when the plugin announced itself.

    Only the newest log counts: an older session says nothing about the OBS that runs now.
    """
    directory = log_dir or config._xdg("XDG_CONFIG_HOME", ".config") / "obs-studio/logs"
    try:
        newest = max((p for p in directory.glob("*.txt") if p.is_file()), default=None)
        if newest is None:
            return None, None
        with newest.open("rb") as handle:
            head = handle.read(LOG_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return None, None
    if match := _LOADED.search(head):
        return True, clean_version(match.group(1))
    return None, None


def scan_devices(library: Path) -> tuple[str, ...]:
    """The inference devices the plugin binary imports a provider for. CPU is always there."""
    try:
        with library.open("rb") as handle:
            blob = handle.read(LIBRARY_SCAN_BYTES)
    except OSError:
        return ("cpu",)
    found = [name for name, symbols in _DEVICE_SYMBOLS.items() if any(s in blob for s in symbols)]
    return ("cpu", *found)


def detect(
    *,
    lib_dirs: Iterable[Path] | None = None,
    share_dirs: Iterable[Path] | None = None,
    dpkg_status: Path = DPKG_STATUS,
    log_dir: Path | None = None,
) -> Plugin:
    """Is the plugin installed, which version, and what can it run on. Reads files only; never starts anything."""
    searched = lib_dirs if lib_dirs is not None else library_dirs()
    library = next((p for d in searched if (p := d / LIBRARY).is_file()), None)
    if library is None:
        return Plugin(installed=False)
    models = any((d / MODEL).is_file() for d in (share_dirs if share_dirs is not None else data_dirs()))
    loaded, logged = log_evidence(log_dir)
    version = dpkg_version(dpkg_status) or logged
    return Plugin(
        installed=True,
        version=version,
        library=str(library),
        models_ok=models,
        loaded=loaded,
        devices=scan_devices(library),
    )


def health_check(cfg: dict[str, str] | None = None, plugin: Plugin | None = None) -> dict[str, str]:
    """The doctor / Console check "Background blur" (same shape as cli.check)."""
    found = plugin or detect()
    base = {"id": "background-blur", "group": "video"}
    if not found.installed:
        return {
            **base,
            "state": "warn",
            "title": f"Background blur is off: the {PLUGIN_NAME} OBS plugin is not installed",
            "detail": f"no {LIBRARY} in OBS's plugin folders",
            "hint": INSTALL_HINT,
        }
    version = f" {found.version}" if found.version else ""
    if not found.models_ok:
        return {
            **base,
            "state": "warn",
            "title": f"{PLUGIN_NAME}{version} is installed but its models are missing",
            "detail": f"{MODEL} not found next to the plugin",
            "hint": f"reinstall the {PLUGIN_NAME} package (it ships the models), then restart OBS",
        }
    loaded = "" if found.loaded is not False else " (OBS did not load it)"
    return {
        **base,
        "state": "ok",
        "title": f"Background blur is available: {PLUGIN_NAME}{version}",
        "detail": f"{', '.join(found.device_labels)}; {found.library}{loaded}",
        "hint": "",
    }


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One obs-websocket GetStats reading (fields of the GetStats response, obs-websocket 5)."""

    render_ms: float  # averageFrameRenderTime
    cpu: float  # cpuUsage, percent of the machine as OBS's stats dock shows it
    skipped: int  # renderSkippedFrames + outputSkippedFrames, cumulative since OBS started


def parse_stats(data: Mapping[str, object]) -> Sample | None:
    """A Sample from a GetStats response, or None when OBS left a field out or sent a non-number."""
    try:
        values = [data[k] for k in ("averageFrameRenderTime", "cpuUsage", "renderSkippedFrames", "outputSkippedFrames")]
    except KeyError:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != v for v in values):
        return None
    render, cpu, render_skipped, output_skipped = values
    return Sample(float(render), float(cpu), int(render_skipped) + int(output_skipped))


def percentile(values: Iterable[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


def median(values: Iterable[float]) -> float:
    return percentile(values, 0.5)


@dataclass
class CostGuard:
    """Compares OBS's frame time and CPU with the blur on against the same numbers with it off.

    OBS reports a smoothed mean frame time, not a per-frame distribution, so the p95 of these one-second readings
    stands in for the gate's p95 frame time; the offline benchmark (issue #22) measures the real distribution.
    """

    baseline: deque[Sample] = field(default_factory=lambda: deque(maxlen=WINDOW_SAMPLES))
    active: deque[Sample] = field(default_factory=lambda: deque(maxlen=WINDOW_SAMPLES))
    skipped_at_start: int | None = None
    last_skipped: int | None = None
    was_on: bool = False

    def reset(self) -> None:
        self.baseline.clear()
        self.active.clear()
        self.skipped_at_start = self.last_skipped = None
        self.was_on = False

    def observe(self, sample: Sample, blur_on: bool) -> None:
        if self.last_skipped is not None and sample.skipped < self.last_skipped:  # OBS restarted: counters began again
            self.skipped_at_start = None
            self.active.clear()
        self.last_skipped = sample.skipped
        if blur_on:
            if not self.was_on:
                self.active.clear()
                self.skipped_at_start = sample.skipped
            if self.skipped_at_start is None:
                self.skipped_at_start = sample.skipped
            self.active.append(sample)
        else:
            if self.was_on:
                self.baseline.clear()  # whatever the scene costs now is the new reference
            self.baseline.append(sample)
            self.skipped_at_start = None
            self.active.clear()
        self.was_on = blur_on

    def verdict(self) -> dict[str, object]:
        limits = {"p95_ms": GATE_P95_MS, "cpu_percent": GATE_CPU_PERCENT}
        if not self.was_on:
            return {"state": "off", "detail": "Blur is off.", "limits": limits, "samples": 0}
        if len(self.active) < MIN_ACTIVE_SAMPLES:
            return {
                "state": "measuring",
                "detail": f"Measuring the cost ({len(self.active)} of {MIN_ACTIVE_SAMPLES} readings).",
                "limits": limits,
                "samples": len(self.active),
            }
        have_baseline = len(self.baseline) >= MIN_BASELINE_SAMPLES
        base_ms = percentile((s.render_ms for s in self.baseline), 0.95) if have_baseline else 0.0
        base_cpu = median(s.cpu for s in self.baseline) if have_baseline else 0.0
        added_ms = max(0.0, percentile((s.render_ms for s in self.active), 0.95) - base_ms)
        added_cpu = max(0.0, median(s.cpu for s in self.active) - base_cpu)
        dropped = max(0, self.active[-1].skipped - (self.skipped_at_start or 0))
        reasons = []
        if added_ms >= GATE_P95_MS:
            reasons.append(f"frame time +{added_ms:.1f} ms (limit {GATE_P95_MS:g})")
        if have_baseline and added_cpu >= GATE_CPU_PERCENT:
            reasons.append(f"CPU +{added_cpu:.1f}% (limit {GATE_CPU_PERCENT:g})")
        if dropped:
            reasons.append(f"{dropped} dropped frame{'s' if dropped != 1 else ''}")
        detail = f"+{added_ms:.1f} ms per frame, " + (f"+{added_cpu:.1f}% CPU" if have_baseline else "CPU not measured")
        detail += f", {dropped} dropped"
        if not have_baseline:
            detail += " (blur was already on, so there is nothing to compare with; switch it off for a few seconds)"
        return {
            "state": "over" if reasons else "ok",
            "detail": detail,
            "reasons": reasons,
            "added_ms": round(added_ms, 2),
            "added_cpu": round(added_cpu, 2) if have_baseline else None,
            "dropped": dropped,
            "limits": limits,
            "samples": len(self.active),
        }
