"""Face detection backends: OpenVINO (Intel GPU first, then CPU), OpenCV DNN on the CPU, otherwise unavailable.

Both backends run the same YuNet model on a JPEG (what obs-websocket's GetSourceScreenshot returns) and give back
`Face` boxes normalised to the picture. numpy, OpenCV and OpenVINO are optional (`pip install 'streamstack[framing]'`)
and imported only when a backend is opened, so the rest of StreamStack never needs them.

YuNet (2023mar) output layout, from the OpenCV Zoo reference decoder: twelve tensors ordered cls, obj, bbox and
kps for strides 8, 16 and 32. Cell (row, col) of a stride-s grid predicts a box centred at ((col + dx) * s,
(row + dy) * s) with size exp(dw) * s by exp(dh) * s, a score sqrt(cls * obj) and five landmarks (right eye, left
eye, nose, mouth corners).
"""

from __future__ import annotations

import importlib.util
import logging
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from streamstack.framing import model as models
from streamstack.framing.engine import Face

log = logging.getLogger(__name__)

STRIDES = (8, 16, 32)
INPUT_WIDTH = 320  # detection width; the height follows the picture's aspect in steps of 32
SCORE_THRESHOLD = 0.6
NMS_THRESHOLD = 0.3
TOP_K = 20
TIMES_KEPT = 60
DEVICES = ("auto", "gpu", "cpu")


class DetectorUnavailable(Exception):
    """No backend can run the model here (the message says which dependency or file is missing)."""


class Detector(Protocol):
    backend: str  # "openvino" | "opencv"

    @property
    def device(self) -> str:
        """A short human label: "Intel Arc A380 (GPU)", "CPU"."""

    def detect(self, jpeg: bytes) -> list[Face]: ...

    def close(self) -> None: ...


class Timing:
    """Per-detection wall time, kept for the health check."""

    def __init__(self) -> None:
        self._ms: deque[float] = deque(maxlen=TIMES_KEPT)
        self._lock = threading.Lock()

    def add(self, seconds: float) -> None:
        with self._lock:
            self._ms.append(seconds * 1000)

    def summary(self) -> dict[str, float | int | None]:
        with self._lock:
            values = sorted(self._ms)
        if not values:
            return {"median_ms": None, "p95_ms": None, "samples": 0}
        p95 = values[min(len(values) - 1, int(0.95 * len(values)))]
        return {"median_ms": round(statistics.median(values), 2), "p95_ms": round(p95, 2), "samples": len(values)}


def available_modules() -> dict[str, bool]:
    """Which optional packages can be imported, without importing them."""
    return {name: importlib.util.find_spec(name) is not None for name in ("numpy", "cv2", "openvino")}


def input_size(width: int, height: int) -> tuple[int, int]:
    """The network input for a picture: INPUT_WIDTH wide, the height at the same aspect rounded to 32."""
    if width <= 0 or height <= 0:
        raise ValueError("empty picture")
    rows = max(1, round(INPUT_WIDTH * height / width / 32))
    return INPUT_WIDTH, rows * 32


# -- decoding (numpy only) --------------------------------------------------------------------------------------


def nms(boxes: Any, scores: Any, threshold: float, top_k: int) -> list[int]:
    """Greedy non-maximum suppression over (x, y, w, h) boxes; returns kept indexes, best first."""
    import numpy as np

    order = np.argsort(-scores)
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = x1 + boxes[:, 2], y1 + boxes[:, 3]
    area = boxes[:, 2] * boxes[:, 3]
    keep: list[int] = []
    while order.size and len(keep) < top_k:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        union = area[i] + area[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= threshold]
    return keep


def decode_yunet(
    outputs: Sequence[Any],
    width: int,
    height: int,
    score_threshold: float = SCORE_THRESHOLD,
    nms_threshold: float = NMS_THRESHOLD,
    top_k: int = TOP_K,
) -> list[Face]:
    """The twelve YuNet output tensors for a width x height input as faces normalised to that input."""
    import numpy as np

    if len(outputs) != 12:
        raise ValueError(f"YuNet gives 12 output tensors, this model gave {len(outputs)}")
    boxes, scores, eyes = [], [], []
    for n, stride in enumerate(STRIDES):
        cols, rows = width // stride, height // stride
        cls = np.clip(np.asarray(outputs[n], dtype=np.float32).reshape(-1), 0.0, 1.0)
        obj = np.clip(np.asarray(outputs[3 + n], dtype=np.float32).reshape(-1), 0.0, 1.0)
        bbox = np.asarray(outputs[6 + n], dtype=np.float32).reshape(-1, 4)
        kps = np.asarray(outputs[9 + n], dtype=np.float32).reshape(-1, 10)
        if cls.shape[0] != cols * rows or bbox.shape[0] != cols * rows:
            raise ValueError(f"stride {stride}: expected {cols * rows} cells, the model gave {cls.shape[0]}")
        score = np.sqrt(cls * obj)
        keep = np.nonzero(score >= score_threshold)[0]
        if keep.size == 0:
            continue
        col = (keep % cols).astype(np.float32)
        row = (keep // cols).astype(np.float32)
        cx = (col + bbox[keep, 0]) * stride
        cy = (row + bbox[keep, 1]) * stride
        w = np.exp(np.clip(bbox[keep, 2], -10, 10)) * stride
        h = np.exp(np.clip(bbox[keep, 3], -10, 10)) * stride
        boxes.append(np.stack([cx - w / 2, cy - h / 2, w, h], axis=1))
        scores.append(score[keep])
        eye_rows = ((kps[keep, 1] + row) + (kps[keep, 3] + row)) / 2 * stride  # mean of the two eyes' y
        eyes.append(eye_rows)
    if not boxes:
        return []
    all_boxes, all_scores, all_eyes = np.concatenate(boxes), np.concatenate(scores), np.concatenate(eyes)
    faces = []
    for i in nms(all_boxes, all_scores, nms_threshold, top_k):
        x, y, w, h = (float(v) for v in all_boxes[i])
        x0, y0 = max(0.0, x), max(0.0, y)
        x1, y1 = min(float(width), x + w), min(float(height), y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        eye = float(all_eyes[i]) / height
        faces.append(
            Face(
                x0 / width,
                y0 / height,
                (x1 - x0) / width,
                (y1 - y0) / height,
                float(all_scores[i]),
                eye if y0 / height <= eye <= y1 / height else None,
            )
        )
    return faces


def decode_image(jpeg: bytes) -> Any:
    """A BGR array from JPEG bytes."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("the picture is not a readable JPEG")
    return image


# -- backends ---------------------------------------------------------------------------------------------------


class OpenVinoDetector:
    """YuNet on OpenVINO: the Intel GPU when the runtime offers one (and it is allowed), otherwise the CPU."""

    backend = "openvino"

    def __init__(self, model_file: Path, device: str = "auto", cache: Path | None = None) -> None:
        for name in ("numpy", "cv2", "openvino"):
            if importlib.util.find_spec(name) is None:
                raise DetectorUnavailable(
                    f"the {name} Python package is not installed (pip install 'streamstack[framing]')"
                )
        try:
            import openvino as ov
        except ImportError as exc:  # a broken install: found but not importable
            raise DetectorUnavailable(f"openvino cannot be imported: {exc}") from exc
        self._ov = ov
        self._model_file = model_file
        self._timing = Timing()
        self._compiled: dict[tuple[int, int], Any] = {}
        self._requests: dict[tuple[int, int], Any] = {}
        self._lock = threading.Lock()
        try:
            self._core = ov.Core()
            if cache is not None:
                cache.mkdir(parents=True, exist_ok=True)
                self._core.set_property({"CACHE_DIR": str(cache)})
            self._target = self._choose(device)
            # Compile at the common 16:9 size now: a failing GPU falls back to the CPU here, not mid-call.
            self._network(*input_size(16, 9))
        except DetectorUnavailable:
            raise
        except Exception as exc:  # OpenVINO raises plain RuntimeError/Exception subclasses of its own
            raise DetectorUnavailable(f"OpenVINO could not load the face model: {exc}") from exc

    def _choose(self, device: str) -> str:
        devices = list(self._core.available_devices)
        gpus = [d for d in devices if d == "GPU" or d.startswith("GPU.")]
        if device == "cpu" or not gpus:
            return "CPU"
        # Prefer the first GPU that is Intel (GPU.0 is the discrete Arc when the iGPU is off or listed later).
        for gpu in gpus:
            try:
                if "intel" in str(self._core.get_property(gpu, "FULL_DEVICE_NAME")).lower():
                    return gpu
            except Exception as exc:
                log.debug("framing: cannot describe %s: %s", gpu, exc)
        return gpus[0] if device == "gpu" else "CPU"

    def _network(self, width: int, height: int) -> Any:
        key = (width, height)
        with self._lock:
            compiled = self._compiled.get(key)
            if compiled is not None:
                return compiled
            base = self._core.read_model(str(self._model_file))
            base.reshape([1, 3, height, width])
            hint = {"PERFORMANCE_HINT": "LATENCY"}
            try:
                compiled = self._core.compile_model(base, self._target, hint)
            except Exception as exc:
                if self._target == "CPU":
                    raise
                log.warning("framing: %s could not compile the face model (%s); using the CPU", self._target, exc)
                self._target = "CPU"
                compiled = self._core.compile_model(base, "CPU", hint)
            self._compiled[key] = compiled
            return compiled

    @property
    def device(self) -> str:
        if self._target == "CPU":
            return "CPU"
        try:
            name = str(self._core.get_property(self._target, "FULL_DEVICE_NAME"))
        except Exception:
            name = self._target
        name = (
            name.replace("(R)", "")
            .replace("(TM)", "")
            .replace("Graphics", "")
            .replace("(dGPU)", "")
            .replace("(iGPU)", "")
        )
        return f"{' '.join(name.split())} (GPU)"

    @property
    def timing(self) -> Timing:
        return self._timing

    def detect(self, jpeg: bytes) -> list[Face]:
        import cv2
        import numpy as np

        started = time.perf_counter()
        image = decode_image(jpeg)
        width, height = input_size(image.shape[1], image.shape[0])
        small = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        blob = np.ascontiguousarray(small.transpose(2, 0, 1)[None], dtype=np.float32)  # BGR, 0..255, as trained
        self._network(width, height)
        request = self._requests.get((width, height)) or self._compiled[(width, height)].create_infer_request()
        self._requests[(width, height)] = request
        request.infer([blob])
        outputs = [request.get_output_tensor(i).data for i in range(12)]
        faces = decode_yunet(outputs, width, height)
        self._timing.add(time.perf_counter() - started)
        return faces

    def close(self) -> None:
        self._requests.clear()
        self._compiled.clear()


class OpenCvDetector:
    """YuNet through OpenCV's FaceDetectorYN on the CPU."""

    backend = "opencv"

    def __init__(self, model_file: Path, device: str = "auto", cache: Path | None = None) -> None:
        for name in ("numpy", "cv2"):
            if importlib.util.find_spec(name) is None:
                raise DetectorUnavailable(
                    f"the {name} Python package is not installed (pip install 'streamstack[framing]')"
                )
        try:
            import cv2
        except ImportError as exc:
            raise DetectorUnavailable(f"opencv cannot be imported: {exc}") from exc
        if not hasattr(cv2, "FaceDetectorYN"):
            raise DetectorUnavailable(f"this OpenCV ({cv2.__version__}) has no FaceDetectorYN; it needs 4.5.4 or newer")
        self._cv2 = cv2
        self._timing = Timing()
        try:
            self._net = cv2.FaceDetectorYN.create(
                str(model_file), "", input_size(16, 9), SCORE_THRESHOLD, NMS_THRESHOLD, TOP_K,
                cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU,
            )  # fmt: skip
        except cv2.error as exc:
            raise DetectorUnavailable(f"OpenCV could not load the face model: {exc}") from exc
        self._size = input_size(16, 9)

    @property
    def device(self) -> str:
        return "CPU"

    @property
    def timing(self) -> Timing:
        return self._timing

    def detect(self, jpeg: bytes) -> list[Face]:
        cv2 = self._cv2
        started = time.perf_counter()
        image = decode_image(jpeg)
        width, height = input_size(image.shape[1], image.shape[0])
        small = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        if (width, height) != self._size:
            self._net.setInputSize((width, height))
            self._size = (width, height)
        _, rows = self._net.detect(small)
        faces = []
        for row in [] if rows is None else rows:
            x, y, w, h = (float(v) for v in row[:4])
            eye = (float(row[5]) + float(row[7])) / 2 / height
            x0, y0, x1, y1 = max(0.0, x), max(0.0, y), min(float(width), x + w), min(float(height), y + h)
            if x1 <= x0 or y1 <= y0:
                continue
            faces.append(
                Face(
                    x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height, float(row[14]),
                    eye if y0 / height <= eye <= y1 / height else None,
                )
            )  # fmt: skip
        self._timing.add(time.perf_counter() - started)
        return faces

    def close(self) -> None:
        self._net = None


Factory = Callable[[Path, str, Path | None], Detector]
BACKENDS: tuple[tuple[str, Factory], ...] = (("openvino", OpenVinoDetector), ("opencv", OpenCvDetector))


def open_detector(
    device: str = "auto",
    *,
    model_file: Path | None = None,
    backends: Sequence[tuple[str, Factory]] | None = None,
    cache: Path | None = None,
) -> Detector:
    """The best backend that works here. Raises DetectorUnavailable listing why each one did not."""
    if device not in DEVICES:
        raise DetectorUnavailable(f"unknown framing device {device!r} (use one of {', '.join(DEVICES)})")
    try:
        model_file = models.verify(model_file)
    except models.ModelError as exc:
        raise DetectorUnavailable(str(exc)) from exc
    cache = cache if cache is not None else models.cache_dir()
    reasons = []
    for name, factory in backends if backends is not None else BACKENDS:
        try:
            return factory(model_file, device, cache)
        except DetectorUnavailable as exc:
            reasons.append(f"{name}: {exc}")
            log.info("framing backend %s unavailable: %s", name, exc)
    raise DetectorUnavailable("; ".join(reasons) or "no detection backend")
