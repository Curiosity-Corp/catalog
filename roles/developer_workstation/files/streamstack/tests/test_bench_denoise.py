"""The denoise bake-off harness (tools/bench_denoise.py, issue #131): its pure signal helpers and the download pin.
No network, no PipeWire, no models."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "bench_denoise.py"
    spec = importlib.util.spec_from_file_location("bench_denoise", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_denoise"] = module
    spec.loader.exec_module(module)
    return module


bd = _load()


def _tone(seconds: float = 2.0) -> np.ndarray:
    t = np.arange(int(seconds * bd.SR)) / bd.SR
    rng = np.random.default_rng(1)
    return (0.1 * np.sin(2 * np.pi * 220 * t) + 0.02 * rng.standard_normal(len(t))).astype(np.float32)


def test_mix_at_snr_hits_the_requested_ratio() -> None:
    rng = np.random.default_rng(3)
    clean = _tone()
    noise = rng.standard_normal(bd.SR * 3).astype(np.float32)
    for snr in (0.0, 5.0, 15.0):
        mixed = bd.mix_at_snr(clean, noise, snr, np.random.default_rng(4))
        measured = bd.db(bd.rms(clean)) - bd.db(bd.rms(mixed - clean))
        assert measured == pytest.approx(snr, abs=0.1)


def test_si_sdr_is_scale_invariant_and_ranks_noise() -> None:
    clean = _tone()
    noise = np.random.default_rng(5).standard_normal(len(clean)).astype(np.float32) * 0.05
    assert bd.si_sdr(clean, clean * 0.3) > 100
    assert bd.si_sdr(clean, clean + noise) > bd.si_sdr(clean, clean + 4 * noise)


@pytest.mark.parametrize("delay_samples", [0, 480, 1920])
def test_align_recovers_the_delay(delay_samples: int) -> None:
    ref = _tone()
    out = np.concatenate([np.zeros(delay_samples, dtype=np.float32), ref])[: len(ref)]
    shifted, lag_s = bd.align(out, ref)
    assert lag_s == pytest.approx(delay_samples / bd.SR, abs=1 / bd.SR)
    assert np.allclose(shifted[: len(ref) - delay_samples], ref[: len(ref) - delay_samples], atol=1e-6)


def test_fetch_refuses_a_file_that_differs_from_the_pin(tmp_path: Path) -> None:
    (tmp_path / "x.bin").write_bytes(b"not what was pinned")
    asset = bd.Asset("x.bin", "https://invalid.example/x.bin", 3, "0" * 64, "test")
    with pytest.raises(bd.BenchError, match="does not match the pin"):
        bd.fetch(asset, tmp_path)
    assert not (tmp_path / "x.bin").exists()


def test_fetch_accepts_a_matching_file(tmp_path: Path) -> None:
    data = b"pinned"
    (tmp_path / "ok.bin").write_bytes(data)
    asset = bd.Asset("ok.bin", "https://invalid.example/ok.bin", len(data), bd.sha256_bytes(data), "test")
    assert bd.fetch(asset, tmp_path) == tmp_path / "ok.bin"


def test_every_pinned_asset_has_a_hash_and_a_licence() -> None:
    assert bd.ASSETS
    for asset in bd.ASSETS.values():
        assert len(asset.sha256) == 64 and asset.size > 0 and asset.licence
        assert asset.url.startswith("https://")
