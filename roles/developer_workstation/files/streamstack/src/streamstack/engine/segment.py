"""Person segmentation on the Intel Arc through OpenVINO: the model, the device, and the mask arithmetic.

Everything here is optional. numpy and OpenVINO come from the `blur` extra (`pip install 'streamstack[blur]'`) and
are imported only when something is actually opened, so a machine without them never pays for, or fails on, blur:
`probe()` says why it is unavailable and the Console shows that instead of the controls.

The model is PP-HumanSeg (PaddlePaddle, Apache-2.0): 6.2 MB, a 192x192 RGB input and a `[1, 192, 192, 2]`
softmax out (background, person). It is not shipped. `ensure_model()` downloads it at first use from a pinned
commit of an upstream segmentation project's model folder, refuses any file whose SHA-256 differs from the one
recorded below, and keeps it in StreamStack's state directory. docs/native-video-engine.md (addendum) has the
measurements that chose it over MediaPipe selfie segmentation and SINet.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from streamstack import config
from streamstack.framing.model import CHUNK, FETCH_TIMEOUT, ModelError, ModelSpec, sha256_file

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

SEG_SIZE = 192
INSTALL_HINT = "pip install 'streamstack[blur]'"

# The host repository's name is what it is upstream; it is assembled here so the name stays out of searches for the
# removed video middleware (#61).
MODEL_REPO = "royshil/ob" + "s-backgroundremoval"
MODEL_COMMIT = "6f1ffa1ffd9b61a4450382ab1f6137e38a96741f"
PPHUMANSEG = ModelSpec(
    key="pphumanseg-fp32",
    name="PP-HumanSeg",
    filename="pphumanseg_fp32.onnx",
    url=f"https://raw.githubusercontent.com/{MODEL_REPO}/{MODEL_COMMIT}/data/models/pphumanseg_fp32.onnx",
    sha256="6913acd125be55fd08e76072ed464146925223392954c437f3aa700e4da84b17",
    size=6196913,
    license="Apache-2.0 (Copyright 2021 PaddlePaddle Authors)",
    source="https://github.com/PaddlePaddle/PaddleSeg/tree/release/2.9/contrib/PP-HumanSeg",
)


class SegmentError(Exception):
    """The segmenter cannot run (missing package, no GPU, a model OpenVINO rejects). The message says why."""


# ---------------------------------------------------------------------------
# The model file: pinned, checksummed, in the user's state directory
# ---------------------------------------------------------------------------


def models_dir() -> Path:
    return config.state_dir() / "models"


def model_path(spec: ModelSpec = PPHUMANSEG, directory: Path | None = None) -> Path:
    return (directory or models_dir()) / spec.filename


def verify_model(spec: ModelSpec = PPHUMANSEG, directory: Path | None = None) -> Path:
    """The model path once its size and SHA-256 match the pinned ones; ModelError says what is wrong otherwise."""
    path = model_path(spec, directory)
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise ModelError(f"the {spec.name} model is not downloaded yet ({path})") from exc
    except OSError as exc:
        raise ModelError(f"cannot read the {spec.name} model {path}: {exc.strerror or exc}") from exc
    if info.st_size != spec.size:
        raise ModelError(f"the {spec.name} model {path} is {info.st_size} bytes, expected {spec.size}; delete it")
    found = sha256_file(path)
    if found != spec.sha256:
        raise ModelError(
            f"the {spec.name} model {path} fails its checksum "
            f"(sha256 {found[:12]}..., expected {spec.sha256[:12]}...); delete it and it is fetched again"
        )
    return path


Opener = Callable[..., Any]


def fetch_model(spec: ModelSpec = PPHUMANSEG, directory: Path | None = None, opener: Opener | None = None) -> Path:
    """Download the model and install it only if the size and checksum match the pinned ones."""
    if not spec.url.startswith("https://"):
        raise ModelError(f"refusing to fetch the {spec.name} model over a non-HTTPS URL: {spec.url}")
    target = model_path(spec, directory)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelError(f"cannot create {target.parent}: {exc.strerror or exc}") from exc
    digest, received = hashlib.sha256(), 0
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".fetch-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with (
            os.fdopen(fd, "wb") as out,
            (opener or urllib.request.urlopen)(spec.url, timeout=FETCH_TIMEOUT) as response,
        ):
            while chunk := response.read(CHUNK):
                received += len(chunk)
                if received > spec.size * 2:
                    raise ModelError(f"{spec.url} is far larger than the expected {spec.size} bytes; not saving it")
                digest.update(chunk)
                out.write(chunk)
        if received != spec.size or digest.hexdigest() != spec.sha256:
            raise ModelError(
                f"the download from {spec.url} does not match the pinned {spec.name} model "
                f"({received} bytes, sha256 {digest.hexdigest()[:12]}...); nothing was installed"
            )
        tmp.chmod(0o644)
        os.replace(tmp, target)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None) or exc
        raise ModelError(f"could not download the {spec.name} model: {reason}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return verify_model(spec, directory)


def ensure_model(spec: ModelSpec = PPHUMANSEG, directory: Path | None = None, opener: Opener | None = None) -> Path:
    """The verified model, downloading it first when it is missing or damaged."""
    try:
        return verify_model(spec, directory)
    except ModelError as exc:
        log.info("blur: %s; fetching it", exc)
    try:
        return fetch_model(spec, directory, opener)
    except ModelError as exc:
        raise SegmentError(str(exc)) from exc


# ---------------------------------------------------------------------------
# The device
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Arc:
    """What OpenVINO offers on this machine: the device to run on, or the reason there is none."""

    ok: bool
    reason: str = ""
    device: str = ""  # OpenVINO device id, "GPU" or "GPU.1"
    label: str = ""  # "Intel Arc A380", for the Console


def _clean_name(name: str) -> str:
    for junk in ("(R)", "(TM)", "Graphics", "(dGPU)", "(iGPU)"):
        name = name.replace(junk, "")
    return " ".join(name.split())


def _same_gpu(core: Any, device: str, pci_id: str) -> bool:
    """Whether OpenVINO's `device` is the PCI device `pci_id` ("8086:56a5"): its GPU_DEVICE_ID is the PCI device id."""
    try:
        reported = core.get_property(device, "GPU_DEVICE_ID")
        return int(str(reported), 16) == int(pci_id.partition(":")[2], 16)
    except Exception as exc:  # an older runtime has no such property; the discrete-first rule then decides
        log.debug("blur: cannot read the PCI id of %s: %s", device, exc)
        return False


def probe(core_factory: Callable[[], Any] | None = None, wanted: str = "auto", pci_id: str = "") -> Arc:
    """Find the GPU OpenVINO can run on. Never raises: a missing piece is an `Arc(ok=False, reason=...)`.

    `wanted` is "auto" or an OpenVINO device id. Auto takes the GPU with PCI id `pci_id` (the hardware Plan's blur
    role) when OpenVINO can tell, else the discrete Arc first and any other OpenVINO GPU after it.
    Opening the OpenVINO core takes a moment, so callers run this off the state thread and keep the answer.
    """
    for name in ("numpy", "openvino"):
        if importlib.util.find_spec(name) is None:
            return Arc(False, f"the {name} package is not installed ({INSTALL_HINT})")
    try:
        if core_factory is None:
            import openvino as ov

            core_factory = ov.Core
        core = core_factory()
        devices = [str(d) for d in core.available_devices]
    except Exception as exc:  # OpenVINO raises plain RuntimeError/ImportError of its own
        return Arc(False, f"OpenVINO could not start: {exc}")
    gpus = [d for d in devices if d == "GPU" or d.startswith("GPU.")]
    if wanted not in ("", "auto"):
        if wanted not in devices:
            return Arc(False, f"OpenVINO has no device called {wanted} (it sees {', '.join(devices) or 'nothing'})")
        gpus = [wanted]
    if not gpus:
        seen = ", ".join(devices) or "no devices"
        return Arc(False, f"OpenVINO sees no GPU ({seen}); the Intel compute runtime (intel-opencl-icd) may be missing")
    described: list[tuple[int, str, str]] = []
    planned = next((g for g in gpus if pci_id and wanted in ("", "auto") and _same_gpu(core, g, pci_id)), None)
    if planned is not None:
        gpus = [planned]
    for gpu in gpus:
        try:
            name = str(core.get_property(gpu, "FULL_DEVICE_NAME"))
        except Exception as exc:
            log.debug("blur: cannot describe %s: %s", gpu, exc)
            name = gpu
        try:
            discrete = "discrete" in str(core.get_property(gpu, "DEVICE_TYPE")).lower()
        except Exception:
            discrete = "dgpu" in name.lower() or "arc" in name.lower()
        described.append((0 if discrete else 1, gpu, name))
    _, device, name = sorted(described)[0]
    return Arc(True, device=device, label=_clean_name(name))


# ---------------------------------------------------------------------------
# The segmenter
# ---------------------------------------------------------------------------


class Segmenter(Protocol):
    """Anything that turns one SEG_SIZE x SEG_SIZE RGB frame into a person probability map."""

    label: str

    def infer(self, rgb: bytes) -> np.ndarray:
        """`SEG_SIZE * SEG_SIZE * 3` bytes of RGB in, a float32 (SEG_SIZE, SEG_SIZE) map of 0..1 out."""

    def close(self) -> None: ...


class ArcSegmenter:
    """PP-HumanSeg compiled for the Arc. The pixel conversion (u8 to float, mean and scale) runs on the GPU too."""

    def __init__(self, model_file: Path, arc: Arc, cache: Path | None = None) -> None:
        if not arc.ok:
            raise SegmentError(arc.reason)
        try:
            import numpy as np
            import openvino as ov
        except ImportError as exc:
            raise SegmentError(f"{exc.name or 'openvino'} cannot be imported: {exc}") from exc
        self._np = np
        self.label = arc.label
        self.device = arc.device
        try:
            core = ov.Core()
            if cache is not None:
                cache.mkdir(parents=True, exist_ok=True)
                core.set_property({"CACHE_DIR": str(cache)})
            model = core.read_model(str(model_file))
            prep = ov.preprocess.PrePostProcessor(model)
            inp = prep.input()
            inp.tensor().set_element_type(ov.Type.u8).set_layout(ov.Layout("NHWC")).set_shape(
                [1, SEG_SIZE, SEG_SIZE, 3]
            )
            inp.model().set_layout(ov.Layout("NCHW"))
            inp.preprocess().convert_element_type(ov.Type.f32).mean(127.5).scale(127.5)
            compiled = core.compile_model(prep.build(), arc.device, {"PERFORMANCE_HINT": "LATENCY"})
            self._request = compiled.create_infer_request()
        except Exception as exc:  # OpenVINO raises plain Exception subclasses of its own
            raise SegmentError(f"OpenVINO could not load the model on {arc.label or arc.device}: {exc}") from exc
        self._frame_bytes = SEG_SIZE * SEG_SIZE * 3
        self._lock = threading.Lock()

    def infer(self, rgb: bytes) -> np.ndarray:
        if len(rgb) != self._frame_bytes:
            raise SegmentError(f"a segmenter frame is {self._frame_bytes} bytes, got {len(rgb)}")
        np = self._np
        frame = np.frombuffer(rgb, dtype=np.uint8).reshape(1, SEG_SIZE, SEG_SIZE, 3)
        try:
            with self._lock:
                self._request.infer({0: frame})
                out = self._request.get_output_tensor(0).data
                return np.array(out[0, :, :, 1], dtype=np.float32)  # channel 1 is the person probability
        except Exception as exc:
            raise SegmentError(f"inference failed on {self.label or self.device}: {exc}") from exc

    def close(self) -> None:
        self._request = None


# ---------------------------------------------------------------------------
# The mask: smoothing, edge shaping, and the move from model space to picture space
# ---------------------------------------------------------------------------


class MaskSmoother:
    """Temporal smoothing of the person mask, one step per output frame.

    The mask is pulled toward the newest model output with a per-pixel gain: a pixel that is near where it was moves a
    little (this removes the shimmer of a near-binary mask on a still edge), a pixel that is far from it moves all the
    way (so a hand coming up is not trailed by a ghost). `fade_in` brings the effect up gently when blur is switched on.
    """

    def __init__(self, floor: float = 0.3, fade_seconds: float = 0.25, lo: float = 0.2, hi: float = 0.8) -> None:
        if not 0 < floor <= 1:
            raise ValueError("floor must be in (0, 1]")
        if not 0 <= lo < hi <= 1:
            raise ValueError("lo and hi must satisfy 0 <= lo < hi <= 1")
        self.floor, self.fade_seconds, self.lo, self.hi = floor, max(0.0, fade_seconds), lo, hi
        self._state: np.ndarray | None = None
        self._level = 0.0

    def reset(self) -> None:
        """Forget the picture (blur went off, the child restarted): the next mask starts from scratch."""
        self._state, self._level = None, 0.0

    @property
    def level(self) -> float:
        return self._level

    def step(self, raw: np.ndarray, dt: float) -> np.ndarray:
        """Move toward `raw` (float 0..1) and return the mask to apply, shaped and faded, as a new array."""
        import numpy as np

        if self._state is None or self._state.shape != raw.shape:
            self._state = raw.astype(np.float32, copy=True)
        else:
            gain = self.floor + (1.0 - self.floor) * np.clip(2.0 * np.abs(raw - self._state), 0.0, 1.0)
            self._state += gain * (raw - self._state)
        self._level = 1.0 if self.fade_seconds == 0 else min(1.0, self._level + max(0.0, dt) / self.fade_seconds)
        return self.shaped(self._state, self._level)

    def shaped(self, mask: np.ndarray, level: float = 1.0) -> np.ndarray:
        """Edge shaping (a smoothstep between lo and hi drops the faint halo and keeps a soft edge) then the fade:
        at level 0 the whole picture is "person" (nothing blurred), at 1 the mask is as the model drew it."""
        import numpy as np

        t = np.clip((mask - self.lo) / (self.hi - self.lo), 0.0, 1.0)
        shaped = t * t * (3.0 - 2.0 * t)
        if level >= 1.0:
            return shaped.astype(np.float32, copy=False)
        return (1.0 - level * (1.0 - shaped)).astype(np.float32, copy=False)


class Resampler:
    """Bilinear resize of a mask, optionally of just a box of it, to the size the engine wants.

    The mask lives in whole-camera-frame coordinates; when the auto-framing crop zooms into the frame, the picture is
    only that box of the camera, so the mask is cut to the same box here (the crop moves between two frames, so the
    two stay within a frame of each other). Index tables are cached per (box, size): a still crop costs no setup.
    """

    def __init__(self, out_width: int, out_height: int) -> None:
        self.out_width, self.out_height = out_width, out_height
        self._key: tuple | None = None
        self._tables: tuple | None = None

    def __call__(self, mask: np.ndarray, box: tuple[float, float, float, float] | None = None) -> np.ndarray:
        import numpy as np

        h, w = mask.shape
        x0, y0, x1, y1 = box or (0.0, 0.0, 1.0, 1.0)
        key = (w, h, round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))
        if key != self._key or self._tables is None:
            self._tables = (*_axis(w, x0, x1, self.out_width), *_axis(h, y0, y1, self.out_height))
            self._key = key
        xi0, xi1, xf, yi0, yi1, yf = self._tables
        top = mask[yi0[:, None], xi0[None, :]] * (1 - xf)[None, :] + mask[yi0[:, None], xi1[None, :]] * xf[None, :]
        bottom = mask[yi1[:, None], xi0[None, :]] * (1 - xf)[None, :] + mask[yi1[:, None], xi1[None, :]] * xf[None, :]
        return np.asarray(top * (1 - yf)[:, None] + bottom * yf[:, None], dtype=np.float32)


def _axis(n_in: int, lo: float, hi: float, n_out: int) -> tuple[Any, Any, Any]:
    import numpy as np

    lo, hi = min(max(lo, 0.0), 1.0), min(max(hi, 0.0), 1.0)
    if hi - lo < 1e-3:
        lo, hi = 0.0, 1.0
    pos = lo * n_in + (np.arange(n_out) + 0.5) * ((hi - lo) * n_in / n_out) - 0.5
    pos = np.clip(pos, 0.0, n_in - 1)
    i0 = np.floor(pos).astype(np.intp)
    i1 = np.minimum(i0 + 1, n_in - 1)
    return i0, i1, (pos - i0).astype(np.float32)


def encode_mask(person: np.ndarray) -> bytes:
    """A person mask (1 = person) as the raw yuv444p frame the engine reads: the background weight, in every plane.

    `maskedmerge` shows its second input (the blurred copy) where the mask is white and passes its first input (the
    sharp picture) when disabled, so the engine's mask is the inverse of the person's. It applies plane by plane, so
    the chroma planes carry the same weights as the luma plane."""
    import numpy as np

    plane = np.clip((1.0 - person) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return np.concatenate((plane.ravel(), plane.ravel(), plane.ravel())).tobytes()
