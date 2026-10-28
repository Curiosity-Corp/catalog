"""Detection backends: YuNet output decoding, backend selection with fakes, and the real backends when installed."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from streamstack.framing import detector, model
from streamstack.framing.detector import DetectorUnavailable, Timing, available_modules, input_size, open_detector
from streamstack.framing.engine import Face

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
needs_numpy = pytest.mark.skipif(not HAVE_NUMPY, reason="numpy is not installed (pip install '.[framing]')")


# -- input size -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1920, 1080), (320, 192)),
        ((1056, 704), (320, 224)),
        ((640, 480), (320, 256)),
        ((320, 180), (320, 192)),
        ((100, 10), (320, 32)),
    ],
)
def test_input_size_follows_the_aspect_in_steps_of_32(size: tuple[int, int], expected: tuple[int, int]) -> None:
    assert input_size(*size) == expected


def test_input_size_refuses_an_empty_picture() -> None:
    with pytest.raises(ValueError, match="empty"):
        input_size(0, 100)


# -- decoding --------------------------------------------------------------------------------------------------------


def outputs_for(
    width: int, height: int, cells: dict[int, list[tuple[int, int, float, float, float, float, float, float]]]
):
    """Twelve YuNet tensors that are quiet everywhere except the given (col, row, cls, obj, dx, dy, dw, dh) cells."""
    import numpy as np

    result: list = []
    shapes = {s: (width // s) * (height // s) for s in detector.STRIDES}
    for kind, last in (("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10)):
        for stride in detector.STRIDES:
            n = shapes[stride]
            tensor = np.zeros((1, n, last), dtype=np.float32)
            for col, row, cls, obj, dx, dy, dw, dh in cells.get(stride, []):
                idx = row * (width // stride) + col
                if kind == "cls":
                    tensor[0, idx, 0] = cls
                elif kind == "obj":
                    tensor[0, idx, 0] = obj
                elif kind == "bbox":
                    tensor[0, idx] = (dx, dy, dw, dh)
                else:
                    tensor[0, idx] = (-0.3, -0.2, 0.3, -0.2, 0, 0, -0.2, 0.3, 0.2, 0.3)  # eyes above the box centre
            result.append(tensor)
    return result


@needs_numpy
def test_decode_places_the_box_from_the_grid_cell_and_normalises_it() -> None:
    width, height = 320, 192
    # stride 16, cell (col 10, row 6): centre (10 + 0.5) * 16 = 168, (6 + 0.5) * 16 = 104; size e^0 * 16 * e^1
    outs = outputs_for(width, height, {16: [(10, 6, 0.9, 0.9, 0.5, 0.5, 1.0, 1.0)]})
    faces = detector.decode_yunet(outs, width, height)
    assert len(faces) == 1
    face = faces[0]
    side = 16 * 2.718281828
    assert face.cx == pytest.approx(168 / width, abs=1e-4)
    assert face.cy == pytest.approx(104 / height, abs=1e-4)
    assert face.w == pytest.approx(side / width, abs=1e-3)
    assert face.h == pytest.approx(side / height, abs=1e-3)
    assert face.score == pytest.approx(0.9, abs=1e-4)
    assert face.eye_y is not None and face.y < face.eye_y < face.cy  # the eyes sit above the middle
    # the mouth corners (the speaker pick reads them): 0.3 cells below the grid row, 0.4 cells apart at stride 16
    assert face.mouth_y is not None and face.y < face.mouth_y < face.y + face.h
    assert face.mouth_y == pytest.approx((6 + 0.3) * 16 / height, abs=1e-4)
    assert face.mouth_w == pytest.approx(0.4 * 16 / width, abs=1e-4)


@needs_numpy
def test_decode_applies_the_threshold_and_merges_overlapping_boxes() -> None:
    width, height = 320, 192
    cells = {
        16: [(10, 6, 0.9, 0.9, 0.5, 0.5, 1.0, 1.0), (10, 6, 0.9, 0.9, 0.5, 0.5, 1.0, 1.0)],
        8: [(30, 20, 0.5, 0.5, 0.5, 0.5, 1.5, 1.5)],  # score 0.5 < 0.6
    }
    outs = outputs_for(width, height, cells)
    assert len(detector.decode_yunet(outs, width, height)) == 1
    # a lower threshold lets the weak one through, as a second face
    assert len(detector.decode_yunet(outs, width, height, score_threshold=0.4)) == 2
    # overlapping boxes at neighbouring cells merge into the better one
    close = {16: [(10, 6, 0.9, 0.9, 0.5, 0.5, 1.0, 1.0), (11, 6, 0.8, 0.8, 0.0, 0.5, 1.0, 1.0)]}
    merged = detector.decode_yunet(outputs_for(width, height, close), width, height)
    assert len(merged) == 1 and merged[0].score == pytest.approx(0.9, abs=1e-4)


@needs_numpy
def test_decode_clips_boxes_to_the_picture_and_handles_nothing_found() -> None:
    width, height = 320, 192
    outs = outputs_for(width, height, {32: [(0, 0, 0.95, 0.95, 0.0, 0.0, 1.5, 1.5)]})  # centred on the corner
    (face,) = detector.decode_yunet(outs, width, height)
    assert face.x == 0.0 and face.y == 0.0
    assert detector.decode_yunet(outputs_for(width, height, {}), width, height) == []


@needs_numpy
def test_decode_rejects_a_model_with_the_wrong_shape() -> None:
    width, height = 320, 192
    with pytest.raises(ValueError, match="12 output tensors"):
        detector.decode_yunet(outputs_for(width, height, {})[:9], width, height)
    with pytest.raises(ValueError, match="cells"):
        detector.decode_yunet(outputs_for(width, height, {}), width + 64, height)


@needs_numpy
def test_nms_keeps_the_best_of_each_cluster() -> None:
    import numpy as np

    boxes = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [50, 50, 10, 10]], dtype=np.float32)
    scores = np.array([0.7, 0.9, 0.8], dtype=np.float32)
    assert detector.nms(boxes, scores, 0.3, 10) == [1, 2]
    assert detector.nms(boxes, scores, 0.3, 1) == [1]


# -- timing ----------------------------------------------------------------------------------------------------------


def test_timing_summary() -> None:
    timing = Timing()
    assert timing.summary() == {"median_ms": None, "p95_ms": None, "samples": 0}
    for ms in range(1, 101):
        timing.add(ms / 1000)
    summary = timing.summary()
    assert summary["samples"] == detector.TIMES_KEPT
    assert 70 < summary["median_ms"] < 80 and summary["p95_ms"] >= 95  # the last 60 samples: 41..100


# -- selection with fake backends ------------------------------------------------------------------------------------


@pytest.fixture
def model_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "m.onnx"
    path.write_bytes(b"fake model")
    spec = model.ModelSpec(
        "t", "Test", "m.onnx", "https://x.invalid/m", hashlib.sha256(b"fake model").hexdigest(), 10, "MIT", ""
    )
    monkeypatch.setattr(model, "YUNET", spec)
    monkeypatch.setattr(model, "verify", lambda p=None, s=spec: model.verify.__wrapped__(p or path, s))
    return path


class FakeDetector:
    backend = "fake"
    device = "Fake GPU"

    def __init__(self, path: Path, device: str, cache: Path | None) -> None:
        self.args = (path, device, cache)
        self.timing = Timing()

    def detect(self, jpeg: bytes) -> list[Face]:
        return [Face(0.4, 0.3, 0.1, 0.2, 0.9)]

    def close(self) -> None:
        self.closed = True


def unavailable(reason: str):
    def factory(path: Path, device: str, cache: Path | None):
        raise DetectorUnavailable(reason)

    return factory


def test_open_detector_prefers_the_first_backend_that_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "verify", lambda path=None, spec=None: tmp_path / "m.onnx")
    cache = tmp_path / "cache"
    det = open_detector("gpu", backends=[("a", FakeDetector), ("b", unavailable("never asked"))], cache=cache)
    assert isinstance(det, FakeDetector)
    assert det.args == (tmp_path / "m.onnx", "gpu", cache)


def test_open_detector_falls_back_and_lists_every_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "verify", lambda path=None, spec=None: tmp_path / "m.onnx")
    det = open_detector(backends=[("openvino", unavailable("no GPU runtime")), ("opencv", FakeDetector)])
    assert isinstance(det, FakeDetector)
    with pytest.raises(DetectorUnavailable) as err:
        open_detector(
            backends=[("openvino", unavailable("openvino is not installed")), ("opencv", unavailable("no cv2"))]
        )
    assert "openvino: openvino is not installed" in str(err.value) and "opencv: no cv2" in str(err.value)


def test_open_detector_needs_a_verified_model_and_a_known_device() -> None:
    with pytest.raises(DetectorUnavailable, match="streamstack framing --fetch"):
        open_detector(backends=[("fake", FakeDetector)])
    with pytest.raises(DetectorUnavailable, match="unknown framing device"):
        open_detector("tpu", backends=[("fake", FakeDetector)])


class FakeCore:
    """OpenVINO's Core as far as device choice goes, with the properties recorded on a real host."""

    def __init__(self, devices: list[str], props: dict[str, tuple[str, str]]) -> None:
        self.available_devices = devices
        self._props = props

    def get_property(self, device: str, key: str) -> str:
        if device not in self._props:
            raise RuntimeError(f"no {key} for {device}")
        name, kind = self._props[device]
        return name if key == "FULL_DEVICE_NAME" else kind


# Recorded on deskmeet1 (OpenVINO 2026.4.1): the iGPU enumerates first, so the Arc is GPU.1.
DESKMEET1 = {
    "GPU.0": ("Intel(R) UHD Graphics 770 (iGPU)", "Type.INTEGRATED"),
    "GPU.1": ("Intel(R) Arc(TM) A380 Graphics (dGPU)", "Type.DISCRETE"),
}


def test_choose_device_picks_the_arc_by_type_not_by_number() -> None:
    core = FakeCore(["CPU", "GPU.0", "GPU.1"], DESKMEET1)
    assert detector.choose_device(core, "auto") == "GPU.1"
    assert detector.choose_device(core, "gpu") == "GPU.1"
    assert detector.choose_device(core, "cpu") == "CPU"
    # dm313red: the Arc alone is GPU (or GPU.0).
    assert detector.choose_device(FakeCore(["CPU", "GPU"], {"GPU": DESKMEET1["GPU.1"]}), "auto") == "GPU"
    # An iGPU-only machine still uses its Intel GPU.
    assert detector.choose_device(FakeCore(["CPU", "GPU.0"], {"GPU.0": DESKMEET1["GPU.0"]}), "auto") == "GPU.0"


def test_choose_device_falls_back_when_gpus_cannot_be_described() -> None:
    core = FakeCore(["CPU", "GPU.0"], {})
    assert detector.choose_device(core, "auto") == "CPU"
    assert detector.choose_device(core, "gpu") == "GPU.0"
    assert detector.choose_device(FakeCore(["CPU"], {}), "gpu") == "CPU"


def test_modules_are_reported_without_importing_them() -> None:
    assert set(available_modules()) == {"numpy", "cv2", "openvino"}


def test_real_backends_say_what_is_missing_instead_of_crashing(tmp_path: Path) -> None:
    present = available_modules()
    if present["numpy"] and present["cv2"]:
        pytest.skip("the optional packages are installed here")
    for factory in (detector.OpenVinoDetector, detector.OpenCvDetector):
        with pytest.raises(DetectorUnavailable, match="not installed"):
            factory(tmp_path / "m.onnx", "auto", None)


# -- the real thing (CI installs the extra and fetches the model; elsewhere these skip) --------------------------------


def _find_model() -> Path | None:
    state, _ = model.status()
    return model.model_path() if state == "ok" else None


_REAL_MODEL = _find_model()  # looked up before the per-test isolation moves XDG_DATA_HOME

needs_stack = pytest.mark.skipif(
    not (available_modules()["numpy"] and available_modules()["cv2"] and _REAL_MODEL),
    reason="needs the framing extra and the fetched face model",
)


@pytest.fixture
def real_model() -> Path:
    assert _REAL_MODEL is not None
    target = model.model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_REAL_MODEL.read_bytes())
    return model.verify()


def _noise_jpeg() -> bytes:
    import cv2
    import numpy as np

    rng = np.random.default_rng(1)
    image = cv2.GaussianBlur((rng.random((360, 640, 3)) * 255).astype(np.uint8), (0, 0), 6)
    cv2.circle(image, (320, 150), 60, (170, 190, 230), -1)
    for dx in (-20, 20):
        cv2.circle(image, (320 + dx, 140), 8, (30, 30, 30), -1)
    ok, jpeg = cv2.imencode(".jpg", image)
    assert ok
    return jpeg.tobytes()


@needs_stack
def test_opencv_backend_runs_the_real_model(real_model: Path) -> None:
    det = detector.OpenCvDetector(real_model, "cpu", None)
    assert det.device == "CPU"
    assert isinstance(det.detect(_noise_jpeg()), list)
    assert det.timing.summary()["samples"] == 1
    with pytest.raises(ValueError, match="readable JPEG"):
        det.detect(b"not a jpeg")


@needs_stack
@pytest.mark.skipif(not available_modules()["openvino"], reason="openvino is not installed")
def test_openvino_cpu_decodes_exactly_like_opencv(real_model: Path) -> None:
    """The two backends share one model; the numpy decoder must agree with OpenCV's own."""
    import cv2
    import numpy as np
    import openvino as ov

    jpeg = _noise_jpeg()
    width, height = input_size(640, 360)
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    small = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    cv = cv2.FaceDetectorYN.create(str(real_model), "", (width, height), 0.02, 0.3, 50)
    _, rows = cv.detect(small)
    core = ov.Core()
    net = core.read_model(str(real_model))
    net.reshape([1, 3, height, width])
    request = core.compile_model(net, "CPU").create_infer_request()
    request.infer([np.ascontiguousarray(small.transpose(2, 0, 1)[None], dtype=np.float32)])
    faces = detector.decode_yunet([request.get_output_tensor(i).data for i in range(12)], width, height, 0.02, 0.3, 50)
    rows = [] if rows is None else list(rows)
    assert len(faces) == len(rows)
    for face, row in zip(faces, rows, strict=True):
        assert face.x * width == pytest.approx(max(0.0, float(row[0])), abs=0.2)
        assert face.y * height == pytest.approx(max(0.0, float(row[1])), abs=0.2)
        assert face.score == pytest.approx(float(row[14]), abs=1e-3)
