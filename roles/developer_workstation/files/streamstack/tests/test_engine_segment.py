"""Background blur's pieces that need no GPU: the model file, the device probe, mask smoothing, resampling."""

from __future__ import annotations

import hashlib
import io
import urllib.error
from pathlib import Path

import pytest

from streamstack.engine import segment
from streamstack.engine.segment import Arc, SegmentError
from streamstack.framing.model import ModelError, ModelSpec

PAYLOAD = b"not really an onnx model" * 100
SPEC = ModelSpec(
    key="t",
    name="Test model",
    filename="t.onnx",
    url="https://example.invalid/t.onnx",
    sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    size=len(PAYLOAD),
    license="Apache-2.0",
    source="https://example.invalid",
)


class Reply(io.BytesIO):
    def __enter__(self) -> Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def opener(data: bytes = PAYLOAD):
    calls: list[str] = []

    def open_(url: str, timeout: float = 0) -> Reply:
        calls.append(url)
        return Reply(data)

    open_.calls = calls  # type: ignore[attr-defined]
    return open_


# -- the model file --------------------------------------------------------------------------------------------------


def test_the_pinned_model_is_apache_licensed_https_and_checksummed() -> None:
    spec = segment.PPHUMANSEG
    assert spec.url.startswith("https://") and len(spec.sha256) == 64 and spec.size > 1_000_000
    assert "Apache-2.0" in spec.license
    assert segment.MODEL_COMMIT in spec.url  # pinned to a commit, not a moving branch


def test_a_missing_model_is_fetched_once_and_then_reused(tmp_path: Path) -> None:
    fetch = opener()
    path = segment.ensure_model(SPEC, tmp_path, fetch)
    assert path.read_bytes() == PAYLOAD and fetch.calls == [SPEC.url]
    assert segment.ensure_model(SPEC, tmp_path, fetch) == path and len(fetch.calls) == 1


def test_a_download_that_fails_its_checksum_is_never_installed(tmp_path: Path) -> None:
    models = tmp_path / "models"
    with pytest.raises(SegmentError, match="does not match the pinned"):
        segment.ensure_model(SPEC, models, opener(b"x" * len(PAYLOAD)))
    assert list(models.iterdir()) == []  # no model, no half-written .part file


def test_a_corrupt_model_on_disk_is_replaced(tmp_path: Path) -> None:
    (tmp_path / SPEC.filename).write_bytes(b"damaged")
    assert segment.ensure_model(SPEC, tmp_path, opener()).read_bytes() == PAYLOAD


def test_a_network_failure_says_so_instead_of_raising_a_traceback(tmp_path: Path) -> None:
    def offline(url: str, timeout: float = 0):
        raise urllib.error.URLError("Temporary failure in name resolution")

    with pytest.raises(SegmentError, match=r"could not download.*name resolution"):
        segment.ensure_model(SPEC, tmp_path, offline)


def test_a_non_https_model_url_is_refused(tmp_path: Path) -> None:
    http = ModelSpec(**{**vars(SPEC), "url": "http://example.invalid/t.onnx"})
    with pytest.raises(ModelError, match="non-HTTPS"):
        segment.fetch_model(http, tmp_path, opener())


def test_verify_model_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="not downloaded yet"):
        segment.verify_model(SPEC, tmp_path)


def test_the_model_lives_in_the_state_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert segment.models_dir() == tmp_path / "streamstack" / "models"


# -- the device ------------------------------------------------------------------------------------------------------


class FakeCore:
    def __init__(self, devices: list[str], names: dict[str, str] | None = None, types: dict[str, str] | None = None):
        self.available_devices = devices
        self._names, self._types = names or {}, types or {}

    def get_property(self, device: str, key: str) -> str:
        table = self._names if key == "FULL_DEVICE_NAME" else self._types
        if device not in table:
            raise RuntimeError(f"no {key} for {device}")
        return table[device]


def test_probe_prefers_the_discrete_gpu_and_cleans_its_name() -> None:
    core = FakeCore(
        ["CPU", "GPU.0", "GPU.1"],
        {"GPU.0": "Intel(R) UHD Graphics (iGPU)", "GPU.1": "Intel(R) Arc(TM) A380 Graphics (dGPU)"},
        {"GPU.0": "Type.INTEGRATED", "GPU.1": "Type.DISCRETE"},
    )
    arc = segment.probe(lambda: core)
    assert arc == Arc(True, device="GPU.1", label="Intel Arc A380")


def test_probe_with_only_a_cpu_says_there_is_no_gpu() -> None:
    arc = segment.probe(lambda: FakeCore(["CPU"]))
    assert not arc.ok and "no GPU" in arc.reason and "CPU" in arc.reason


def test_probe_turns_an_openvino_failure_into_a_reason() -> None:
    def broken() -> FakeCore:
        raise RuntimeError("libze_loader.so.1 not found")

    arc = segment.probe(broken)
    assert not arc.ok and "OpenVINO could not start" in arc.reason and "libze_loader" in arc.reason


def test_probe_honours_a_wanted_device_and_names_a_missing_one() -> None:
    core = FakeCore(["CPU", "GPU.0", "GPU.1"], {"GPU.0": "Intel UHD", "GPU.1": "Intel Arc"}, {})
    assert segment.probe(lambda: core, wanted="GPU.0").device == "GPU.0"
    missing = segment.probe(lambda: core, wanted="GPU.7")
    assert not missing.ok and "GPU.7" in missing.reason


def test_probe_without_the_extra_installed_gives_the_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(segment.importlib.util, "find_spec", lambda name: None)
    arc = segment.probe()
    assert not arc.ok and "not installed" in arc.reason and "streamstack[blur]" in arc.reason


def test_the_segmenter_refuses_a_device_that_is_not_there() -> None:
    with pytest.raises(SegmentError, match="no GPU"):
        segment.ArcSegmenter(Path("/nonexistent"), Arc(False, "no GPU"))


# -- the mask (needs numpy) ------------------------------------------------------------------------------------------

np = pytest.importorskip("numpy")


def square(value: float, size: int = 8) -> np.ndarray:
    return np.full((size, size), value, dtype=np.float32)


def test_the_first_mask_passes_through_shaped() -> None:
    smoother = segment.MaskSmoother(fade_seconds=0)
    out = smoother.step(square(1.0), 0.033)
    assert out.shape == (8, 8) and float(out.min()) == 1.0
    assert float(smoother.step(square(0.0), 0.033).max()) < 1.0  # moving toward 0, not snapping (gain below 1)


def test_a_still_edge_stops_shimmering() -> None:
    smoother = segment.MaskSmoother(fade_seconds=0)
    smoother.step(square(0.5), 0.033)
    out = smoother.step(square(0.55), 0.033)
    assert abs(float(out.mean()) - float(smoother.shaped(square(0.5)).mean())) < 0.05


def test_a_big_change_follows_almost_at_once() -> None:
    smoother = segment.MaskSmoother(fade_seconds=0)
    smoother.step(square(0.0), 0.033)
    assert float(smoother.step(square(1.0), 0.033).mean()) > 0.9  # a hand coming up is not trailed by a ghost


def test_shaping_removes_the_faint_halo_and_keeps_a_soft_edge() -> None:
    smoother = segment.MaskSmoother()
    shaped = smoother.shaped(np.array([0.0, 0.1, 0.5, 0.9, 1.0], dtype=np.float32))
    assert shaped[0] == 0 and shaped[1] == 0 and shaped[3] == 1 and shaped[4] == 1
    assert shaped[2] == pytest.approx(0.5)


def test_fade_in_starts_unblurred_and_reaches_the_full_mask() -> None:
    smoother = segment.MaskSmoother(fade_seconds=0.3)
    first = smoother.step(square(0.0), 0.0)
    assert float(first.min()) == 1.0  # all "person": nothing blurred yet
    for _ in range(12):
        last = smoother.step(square(0.0), 0.033)
    assert float(last.max()) == 0.0 and smoother.level == 1.0


def test_reset_forgets_the_picture() -> None:
    smoother = segment.MaskSmoother(fade_seconds=0.3)
    smoother.step(square(0.0), 1.0)
    smoother.reset()
    assert smoother.level == 0.0 and float(smoother.step(square(0.0), 0.0).min()) == 1.0


def test_smoother_rejects_nonsense_settings() -> None:
    with pytest.raises(ValueError):
        segment.MaskSmoother(floor=0)
    with pytest.raises(ValueError):
        segment.MaskSmoother(lo=0.8, hi=0.2)


def test_resampler_scales_to_the_output_size() -> None:
    out = segment.Resampler(32, 18)(np.tile(np.linspace(0, 1, 16, dtype=np.float32), (9, 1)))
    assert out.shape == (18, 32) and out[0, 0] < out[0, -1]


def test_resampler_cuts_to_the_framing_box() -> None:
    left_dark_right_light = np.hstack((np.zeros((8, 4), np.float32), np.ones((8, 4), np.float32)))
    resample = segment.Resampler(8, 8)
    assert float(resample(left_dark_right_light, (0.5, 0.0, 1.0, 1.0)).mean()) > 0.95  # only the light half
    assert float(resample(left_dark_right_light, (0.0, 0.0, 0.5, 1.0)).mean()) < 0.05  # only the dark half


def test_a_degenerate_box_falls_back_to_the_whole_frame() -> None:
    out = segment.Resampler(8, 8)(square(0.4), (0.5, 0.5, 0.5, 0.5))
    assert out.shape == (8, 8) and float(out.mean()) == pytest.approx(0.4)


def test_the_encoded_mask_is_the_inverse_in_three_planes() -> None:
    person = np.zeros((2, 4), dtype=np.float32)
    person[:, :2] = 1.0
    data = segment.encode_mask(person)
    assert len(data) == 2 * 4 * 3
    plane = data[:8]
    assert list(plane) == [0, 0, 255, 255, 0, 0, 255, 255]  # person -> 0 (stays sharp), background -> 255 (blurred)
    assert data[8:16] == plane and data[16:] == plane
