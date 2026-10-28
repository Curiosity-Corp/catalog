"""The director calibration tool (tools/calibrate_director.py, issue #32): guard, flattening, analysis wiring and the
fit table, with injected analysers. No microphone, no camera, no ffmpeg."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "calibrate_director.py"
    spec = importlib.util.spec_from_file_location("calibrate_director", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["calibrate_director"] = module
    spec.loader.exec_module(module)
    return module


c = _load()


def cp(argv: list[str], out: str = "", rc: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, out, err)


def args(**kw: object) -> argparse.Namespace:
    base = {"label": "xlr-day", "seconds": 20, "source": "streamstack_mic", "dir": "d", "execute": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_flatten_keeps_numbers_only() -> None:
    nested = {"a": 1, "b": True, "c": None, "d": "x", "scores": {"pitch": 0.5, "clean": None}}
    assert c.flatten(nested) == {"a": 1.0, "scores.pitch": 0.5}


def test_record_refuses_during_a_call_and_on_unreadable_streams(tmp_path: Path) -> None:
    call = lambda argv: cp(argv, 'application.name = "Zoom"')  # noqa: E731
    with pytest.raises(c.CalibrationError, match="not recording"):
        c.record(args(dir=str(tmp_path), execute=True), call)
    with pytest.raises(c.CalibrationError, match="cannot read"):
        c.record(args(dir=str(tmp_path)), lambda argv: cp(argv, rc=1, err="no pulse"))


def test_record_dry_run_runs_no_recorder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return cp(argv)

    assert c.record(args(dir=str(tmp_path)), runner) is None
    assert "dry run" in capsys.readouterr().out and not any(a[0] == "ffmpeg" for a in calls)


def test_record_executes_ffmpeg_on_the_named_source(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return cp(argv)

    target = c.record(args(dir=str(tmp_path), execute=True), runner, now=lambda: 100.0)
    assert target == tmp_path / "xlr-day-100.wav"
    ffmpeg = next(a for a in calls if a[0] == "ffmpeg")
    assert ffmpeg[ffmpeg.index("-i") + 1] == "streamstack_mic" and not any("set-default" in " ".join(a) for a in calls)


def test_analyze_voice_rows_and_missing_files(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    rows = c.analyze("voice", "r", [wav], voice=lambda p: {"f0_hz": 120.0, "scores": {"pitch": 0.2}})
    assert rows == [{"kind": "voice", "label": "r", "file": "a.wav", "f0_hz": 120.0, "scores.pitch": 0.2}]
    with pytest.raises(c.CalibrationError, match="not a file"):
        c.analyze("voice", "r", [tmp_path / "nope.wav"])


def test_analyze_reports_analysis_errors(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")

    def short(path: Path) -> dict:
        raise c.audiofeatures.AudioAnalysisError("the clip is 0.2 s long")

    with pytest.raises(c.CalibrationError, match=r"0\.2 s"):
        c.analyze("voice", "r", [wav], voice=short)


def voice_rows(n: int, labels: tuple[str, ...], f0: float = 120.0) -> list[dict]:
    return [
        {"kind": "voice", "label": labels[i % len(labels)], "f0_hz": f0 + i, "body_db": 0.0 + i % 3,
         "clarity_db": -10.0, "sibilance_peak_db": -22.0, "snr_db": 30.0 + i}
        for i in range(n)
    ]  # fmt: skip


def test_fit_withholds_suggestions_without_enough_evidence() -> None:
    text = c.fit(voice_rows(3, ("a",)))
    assert "need 8+ clips from 2+ setups" in text and "consider" not in text


def test_fit_flags_a_band_the_clips_fall_outside_of() -> None:
    text = c.fit(voice_rows(10, ("a", "b"), f0=60.0))  # f0 60..69 is below the 70 Hz floor
    pitch = next(line for line in text.splitlines() if line.startswith("| pitch"))
    assert "outside the band" in pitch


def test_fit_empty_and_bad_file(tmp_path: Path) -> None:
    with pytest.raises(c.CalibrationError):
        c.fit([])
    bad = tmp_path / "r.jsonl"
    bad.write_text("{nope\n")
    with pytest.raises(c.CalibrationError, match="not JSON"):
        c.load(bad)


def test_fit_round_trip_through_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "v.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in voice_rows(10, ("a", "b"))))
    assert c.main(["fit", str(path)]) == 0
    assert "10 rows from 2 setups" in capsys.readouterr().out
    assert c.main(["fit", str(tmp_path / "missing.jsonl")]) == 2
