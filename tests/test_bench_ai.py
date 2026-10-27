"""The offline AI benchmark harness (tools/bench_ai.py, issue #22): statistics, gates, reports, the doc block, backend
skipping and a tiny synthetic CPU run. Nothing here opens a camera or a GPU."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from streamstack.framing import model


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "bench_ai.py"
    spec = importlib.util.spec_from_file_location("bench_ai", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_ai"] = module
    spec.loader.exec_module(module)
    return module


bench = _load()

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_ORT = importlib.util.find_spec("onnxruntime") is not None
HAVE_OV = importlib.util.find_spec("openvino") is not None
needs_numpy = pytest.mark.skipif(not HAVE_NUMPY, reason="numpy is not installed (pip install '.[bench]')")


def _find_yunet() -> Path | None:
    state, _ = model.status()
    return model.model_path() if state == "ok" else None


_YUNET = _find_yunet()  # looked up before the per-test isolation moves XDG_DATA_HOME


def make_row(**changes: object) -> object:
    values: dict[str, object] = {
        "source": "canon",
        "size": "1056x704",
        "synthetic": False,
        "model": "yunet-2023mar",
        "kind": "face",
        "backend": "ov-cpu",
        "frames": 10,
        "p50_ms": 2.0,
        "p95_ms": 3.0,
        "max_ms": 5.0,
        "infer_p50_ms": 1.5,
        "cpu_core_pct": 10.0,
    }
    values.update(changes)
    return bench.Row(**values)


def args(*argv: str):
    return bench.parse_args(list(argv))


# -- statistics and gates ---------------------------------------------------------------------------------------------


def test_percentile_interpolates_and_handles_one_value() -> None:
    assert bench.percentile([5.0], 95) == 5.0
    assert bench.percentile([1, 2, 3, 4], 50) == 2.5
    assert bench.percentile([4, 1, 3, 2], 100) == 4
    assert bench.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)
    with pytest.raises(ValueError, match="no values"):
        bench.percentile([], 50)


def test_gate_passes_only_when_all_three_limits_hold() -> None:
    assert bench.evaluate_gate(make_row(), 30.0, 28) == "PASS"
    assert bench.evaluate_gate(make_row(p95_ms=8.0), 30.0, 28) == "FAIL"  # 8 ms is not under 8 ms
    assert bench.evaluate_gate(make_row(cpu_core_pct=28.0), 30.0, 28) == "FAIL"  # 1 percent of 28 CPUs
    assert bench.evaluate_gate(make_row(max_ms=34.0), 30.0, 28) == "FAIL"  # a dropped frame at 30 fps
    assert bench.evaluate_gate(make_row(max_ms=34.0), 24.0, 28) == "PASS"


def test_gate_is_not_applicable_without_a_measurement() -> None:
    assert bench.evaluate_gate(make_row(status="skipped", p95_ms=None), 30.0, 28) == "n/a"


def test_gate_records_total_cpu() -> None:
    row = make_row(cpu_core_pct=14.0)
    bench.evaluate_gate(row, 30.0, 28)
    assert row.cpu_total_pct == pytest.approx(0.5)


# -- intel_gpu_top ----------------------------------------------------------------------------------------------------

GPU_TOP = """[
{"period": {"duration": 0.2, "unit": "ms"}, "engines": {"Render/3D/0": {"busy": 0.0, "unit": "%"}}},
{"period": {"duration": 250, "unit": "ms"}, "engines": {"Render/3D/0": {"busy": 40.0, "unit": "%"},
  "Video/0": {"busy": 2.0, "unit": "%"}}},
{"period": {"duration": 250, "unit": "ms"}, "engines": {"Render/3D/0": {"busy": 60.0, "unit": "%"},
  "Video/0": {"busy": 0.0, "unit": "%"}}},
"""


def test_gpu_top_output_cut_off_by_the_interrupt_still_parses() -> None:
    samples = bench.parse_gpu_top(GPU_TOP + '{"period": {"dur')
    assert len(samples) == 3
    busy, engine = bench.gpu_busy(samples)
    assert engine == "Render/3D/0"
    assert busy == pytest.approx(50.0)  # the first sample spans the tool's start-up and is ignored


def test_gpu_busy_without_engines_is_none() -> None:
    assert bench.gpu_busy([]) is None
    assert bench.gpu_busy([{"period": {}}, {"engines": "x"}]) is None
    assert bench.parse_gpu_top("") == []


def test_gpu_top_skips_cleanly_when_the_tool_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench.shutil, "which", lambda name: None)
    result: dict = {}
    with bench.gpu_top(True, None, result):
        pass
    assert "not installed" in result["note"]
    assert "busy" not in result


def test_gpu_top_is_not_started_when_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(name: str) -> str:
        raise AssertionError("looked for intel_gpu_top")

    monkeypatch.setattr(bench.shutil, "which", boom)
    result: dict = {}
    with bench.gpu_top(False, None, result):
        pass
    assert result == {}


# -- inputs -----------------------------------------------------------------------------------------------------------


def test_input_geometry_reads_static_shapes_and_falls_back_when_dynamic() -> None:
    yunet = next(m for m in bench.CANDIDATES if m.spec.key == "yunet-2023mar")
    assert bench.input_geometry([1, 3, 192, 320], yunet) == (320, 192, "nchw")
    assert bench.input_geometry([1, 144, 256, 3], yunet) == (256, 144, "nhwc")
    assert bench.input_geometry([1, 3, None, None], yunet) == (320, 320, "nchw")
    assert bench.input_geometry([-1, 3, -1, -1], yunet) == (320, 320, "nchw")


def test_parse_size() -> None:
    assert bench.parse_size("1056x704") == (1056, 704)
    for bad in ("1056", "axb", "10x10x10", ""):
        with pytest.raises(bench.BenchError, match="bad size"):
            bench.parse_size(bad)


def test_a_clip_must_be_a_regular_file_never_a_device(tmp_path: Path) -> None:
    with pytest.raises(bench.BenchError, match="not a regular file"):
        bench.load_clip(Path("/dev/null"), 5)
    with pytest.raises(bench.BenchError, match="not a regular file"):
        bench.load_clip(tmp_path, 5)
    with pytest.raises(bench.BenchError, match="cannot read the clip"):
        bench.load_clip(tmp_path / "missing.mkv", 5)


def test_a_clip_that_is_not_video_is_refused_with_a_reason(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"this is not a video" * 10)
    with pytest.raises(bench.BenchError):
        bench.load_clip(junk, 5)


def test_arguments_are_validated() -> None:
    with pytest.raises(SystemExit):
        args("--synthetic", "--backends", "tpu")
    with pytest.raises(SystemExit):
        args("--synthetic", "--models", "nope")
    with pytest.raises(SystemExit):
        args("--synthetic", "--iterations", "0")
    with pytest.raises(SystemExit):
        args("--synthetic", "--clip", "a.mkv")
    parsed = args("--synthetic", "--backends", "ort-cpu, ov-cpu", "--models", "sinet")
    assert parsed.backends == ["ort-cpu", "ov-cpu"]
    assert parsed.models == ["sinet"]


def test_no_input_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main([]) == 2
    assert "--clip" in capsys.readouterr().err


@needs_numpy
def test_synthetic_frames_are_the_asked_size_and_vary() -> None:
    frames = bench.synthetic_frames(160, 90, count=3)
    assert len(frames) == 3
    assert all(f.shape == (90, 160, 3) and f.dtype.name == "uint8" for f in frames)
    assert not (frames[0] == frames[2]).all()


@needs_numpy
def test_preprocess_lays_out_nchw_and_nhwc() -> None:
    mediapipe = next(m for m in bench.CANDIDATES if m.spec.key == "mediapipe-meet")
    frame = bench.synthetic_frames(64, 48, count=1)[0]
    chw = bench.preprocess(frame, mediapipe, 32, 16, "nchw")
    hwc = bench.preprocess(frame, mediapipe, 32, 16, "nhwc")
    assert chw.shape == (1, 3, 16, 32)
    assert hwc.shape == (1, 16, 32, 3)
    assert chw.dtype.name == "float32"
    assert float(chw.min()) >= 0.0
    assert float(chw.max()) <= 1.0


# -- the pinned models ------------------------------------------------------------------------------------------------


def test_every_candidate_is_pinned_https_with_a_checksum_and_a_licence() -> None:
    keys = [m.spec.key for m in bench.CANDIDATES]
    assert len(keys) == len(set(keys))
    assert {m.kind for m in bench.CANDIDATES} == {"segmentation", "face"}
    for m in bench.CANDIDATES:
        assert m.spec.url.startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{64}", m.spec.sha256)
        assert m.spec.size > 0
        assert m.spec.license
        assert "/main/" not in m.spec.url  # pinned to a commit, never a moving branch
    assert any(m.spec is model.YUNET for m in bench.CANDIDATES)


def test_list_models_prints_licences_and_checksums(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main(["--list-models"]) == 0
    out = capsys.readouterr().out
    assert model.YUNET.sha256 in out
    assert "Apache-2.0" in out


# -- the matrix with a fake backend (no model, no runtime needed) -----------------------------------------------------


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def verify(path: Path | None = None, spec: model.ModelSpec = model.YUNET) -> Path:
        return Path(f"/nonexistent/{spec.filename}")

    def open_backend(name: str, path: Path, candidate: object, opts: object) -> object:
        if name == "ov-gpu":
            raise bench.Unavailable("OpenVINO sees no GPU.0 (devices: CPU)")
        calls.append(name)
        return bench.Runner(lambda data: None, 32, 32, "nchw", "fake", 12.0, 3.0 if name == "ov-cpu" else None)

    monkeypatch.setattr(bench.models, "verify", verify)
    monkeypatch.setattr(bench, "open_backend", open_backend)
    return calls


class FakeSource:
    label = "canon"
    width = 64
    height = 48
    synthetic = True

    @property
    def frames(self) -> list[object]:
        return [_ones()]


def _ones() -> object:
    import numpy as np

    return np.zeros((48, 64, 3), dtype=np.uint8)


@needs_numpy
def test_matrix_measures_and_skips_a_missing_gpu_with_its_reason(fake_backend: list[str]) -> None:
    opts = args("--synthetic", "--models", "yunet-2023mar,sinet", "--iterations", "4", "--warmup", "1", "--no-gpu-top")
    opts.cache_dir = Path("/nonexistent")
    rows = bench.run_matrix([FakeSource()], opts)
    assert len(rows) == 2 * 3
    ok = [r for r in rows if r.status == "ok"]
    skipped = [r for r in rows if r.status == "skipped"]
    assert {r.backend for r in ok} == {"ort-cpu", "ov-cpu"}
    assert {r.backend for r in skipped} == {"ov-gpu"}
    assert all("GPU.0" in r.reason for r in skipped)
    assert all(r.frames == 4 and r.p95_ms is not None and r.cpu_core_pct is not None for r in ok)
    assert all(r.gate == "n/a" for r in rows)  # synthetic numbers never pass or fail a gate
    assert all(r.gpu_busy_pct is None for r in rows)
    cached = {r.backend: r.compile_cached_ms for r in ok}
    assert cached == {"ort-cpu": None, "ov-cpu": 3.0}


@needs_numpy
def test_a_model_that_fails_while_running_is_reported_not_raised(
    fake_backend: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(name: str, path: Path, candidate: object, opts: object) -> object:
        def run(data: object) -> None:
            raise RuntimeError("bad op")

        return bench.Runner(run, 32, 32, "nchw", "fake", 1.0, None)

    monkeypatch.setattr(bench, "open_backend", broken)
    opts = args("--synthetic", "--models", "sinet", "--backends", "ort-cpu", "--iterations", "2", "--warmup", "0")
    opts.cache_dir = Path("/nonexistent")
    (row,) = bench.run_matrix([FakeSource()], opts)
    assert row.status == "skipped"
    assert "RuntimeError: bad op" in row.reason


@needs_numpy
def test_a_missing_model_file_skips_every_backend_and_says_how_to_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    opts = args("--synthetic", "--models", "sinet", "--backends", "ort-cpu,ov-cpu", "--iterations", "2")
    opts.cache_dir = Path("/nonexistent")
    rows = bench.run_matrix([FakeSource()], opts)
    assert [r.status for r in rows] == ["skipped", "skipped"]
    assert "--fetch" in rows[0].reason


def test_openvino_without_the_gpu_is_unavailable_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCore:
        available_devices = ("CPU",)

    monkeypatch.setattr(bench, "_ov_core", lambda: FakeCore())
    candidate = bench.CANDIDATES[0]
    with pytest.raises(bench.Unavailable, match=r"no GPU\.0 \(devices: CPU\)"):
        bench.open_openvino(Path("x.onnx"), candidate, args("--synthetic"), "GPU.0")


def test_a_lone_gpu_listed_as_plain_gpu_still_answers_to_gpu_0(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCore:
        available_devices = ("CPU", "GPU")

        def read_model(self, path: str) -> object:
            raise RuntimeError("reached the model: the device check passed")

    monkeypatch.setattr(bench, "_ov_core", lambda: FakeCore())
    candidate = bench.CANDIDATES[0]
    with pytest.raises(bench.Unavailable, match="reached the model"):
        bench.open_openvino(Path("x.onnx"), candidate, args("--synthetic"), "GPU.0")
    with pytest.raises(bench.Unavailable, match=r"no GPU\.1"):
        bench.open_openvino(Path("x.onnx"), candidate, args("--synthetic"), "GPU.1")


# -- reports ----------------------------------------------------------------------------------------------------------


def test_markdown_table_keeps_a_skip_reason_on_one_line() -> None:
    reason = (
        "OpenVINO could not compile x.onnx for CPU: Exception from core.cpp\nCheck 'a | b' failed\n\nUnexpected: no"
    )
    rows = [make_row(), make_row(model="blazeface", status="skipped", reason=reason, p50_ms=None)]
    lines = bench.markdown_table(rows, "canon").splitlines()
    assert len(lines) == 4
    assert len({line.count("|") for line in lines}) == 1  # every row has the same number of cells
    assert "PASS" not in lines[3]
    assert "skipped: OpenVINO could not compile" in lines[3]
    assert "Unexpected: no" in lines[3]
    assert "| 2.00 | 3.00 | 1.50 | 10.0 | - |" in lines[2]


def host() -> dict:
    return {
        "host": "rig",
        "cpu": "i7",
        "logical_cpus": 28,
        "openvino": "2026.4",
        "onnxruntime": "1.31",
        "openvino_devices": ["CPU"],
    }


def stamp():
    import datetime as dt

    return dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


def test_synthetic_doc_block_is_labelled_a_placeholder() -> None:
    opts = args("--synthetic")
    block = bench.doc_block([make_row(synthetic=True, gate="n/a")], opts, host(), stamp())
    assert block.startswith(bench.DOC_BEGIN) and block.endswith(bench.DOC_END)
    assert "SYNTHETIC PLACEHOLDER" in block
    assert "bench-ai:kind=synthetic" in block


def test_recorded_doc_block_says_where_it_was_measured() -> None:
    opts = args("--clip", "canon=/x.mkv")
    block = bench.doc_block([make_row(gate="PASS")], opts, host(), stamp())
    assert "SYNTHETIC" not in block
    assert "bench-ai:kind=recorded" in block
    assert "Measured 2026-10-22 on rig" in block


DOC = f"# Doc\n\nbefore\n\n{bench.DOC_BEGIN}\n<!-- bench-ai:kind=synthetic -->\nold\n{bench.DOC_END}\n\nafter\n"


def test_update_doc_replaces_only_the_marked_block(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(DOC)
    block = bench.doc_block([make_row()], args("--clip", "a=b.mkv"), host(), stamp())
    bench.update_doc(doc, block, synthetic=False)
    text = doc.read_text()
    assert text.startswith("# Doc\n\nbefore\n\n") and text.endswith("\n\nafter\n")
    assert "\nold\n" not in text and "bench-ai:kind=recorded" in text
    bench.update_doc(doc, block, synthetic=False)  # idempotent
    assert doc.read_text() == text


def test_synthetic_numbers_never_replace_recorded_ones(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(DOC)
    recorded = bench.doc_block([make_row()], args("--clip", "a=b.mkv"), host(), stamp())
    bench.update_doc(doc, recorded, synthetic=False)
    synthetic = bench.doc_block([make_row(synthetic=True)], args("--synthetic"), host(), stamp())
    with pytest.raises(bench.BenchError, match="recorded numbers"):
        bench.update_doc(doc, synthetic, synthetic=True)
    assert "bench-ai:kind=recorded" in doc.read_text()


def test_update_doc_needs_the_markers_and_a_readable_file(tmp_path: Path) -> None:
    plain = tmp_path / "plain.md"
    plain.write_text("# nothing here\n")
    with pytest.raises(bench.BenchError, match="no <!-- bench-ai:begin -->"):
        bench.update_doc(plain, "x", synthetic=False)
    with pytest.raises(bench.BenchError, match="cannot read"):
        bench.update_doc(tmp_path / "missing.md", "x", synthetic=False)


def test_the_repo_doc_has_the_block_the_harness_updates() -> None:
    doc = Path(__file__).resolve().parents[1] / "docs" / "hardware-ai-evaluation.md"
    text = doc.read_text()
    assert text.count(bench.DOC_BEGIN) == 1 and text.count(bench.DOC_END) == 1
    assert bench.KIND_RE.search(text)


def test_json_report_carries_settings_models_and_rows() -> None:
    opts = args("--synthetic", "--iterations", "7")
    report = bench.build_report([make_row()], opts, host(), stamp())
    parsed = json.loads(json.dumps(report))
    assert parsed["synthetic"] is True
    assert parsed["settings"]["iterations"] == 7
    assert parsed["settings"]["gates"]["p95_ms_below"] == 8.0
    assert parsed["rows"][0]["model"] == "yunet-2023mar"
    assert parsed["models"]["yunet-2023mar"]["sha256"] == model.YUNET.sha256
    assert "licence" not in parsed["models"]["sinet"] and parsed["models"]["sinet"]["license"]


# -- the real thing: a tiny synthetic CPU run (skips when the runtimes or the fetched model are missing) ---------------


@pytest.mark.skipif(not (HAVE_NUMPY and _YUNET and (HAVE_ORT or HAVE_OV)), reason="needs the bench extra and the model")
def test_synthetic_cpu_run_end_to_end(tmp_path: Path) -> None:
    target = model.model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    assert _YUNET is not None
    target.write_bytes(_YUNET.read_bytes())
    doc = tmp_path / "doc.md"
    doc.write_text(DOC)
    out = tmp_path / "out.json"
    code = bench.main(
        [
            "--synthetic", "--size", "160x120", "--models", "yunet-2023mar", "--backends", "ort-cpu,ov-cpu",
            "--iterations", "3", "--warmup", "0", "--json", str(out), "--update-doc", str(doc),
        ]
    )  # fmt: skip
    assert code == 0
    report = json.loads(out.read_text())
    by_backend = {r["backend"]: r for r in report["rows"]}
    if HAVE_ORT:
        row = by_backend["ort-cpu"]
        assert row["status"] == "ok" and row["frames"] == 3
        assert 0 < row["p50_ms"] <= row["p95_ms"] <= row["max_ms"]
        assert row["gate"] == "n/a"
    else:
        assert by_backend["ort-cpu"]["status"] == "skipped"
    if HAVE_OV:
        row = by_backend["ov-cpu"]
        assert row["status"] == "ok" and row["compile_cached_ms"] is not None
    text = doc.read_text()
    assert "SYNTHETIC PLACEHOLDER" in text and "yunet-2023mar" in text
