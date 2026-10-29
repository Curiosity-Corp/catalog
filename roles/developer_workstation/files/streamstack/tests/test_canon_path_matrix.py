"""The Canon picture-path matrix tool (tools/canon_path_matrix.py, issue #129): parsers, guard, table. No camera."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "canon_path_matrix.py"
    spec = importlib.util.spec_from_file_location("canon_path_matrix", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["canon_path_matrix"] = module
    spec.loader.exec_module(module)
    return module


m = _load()

V4L2_ERR = "<<<<<<<<<<< 19.80 fps\n<<<<<<<<<<< 20.10 fps\n<<<<<<<<<<< 20.00 fps\n"
FFMPEG_ERR = (
    "[Parsed_cropdetect_0 @ 0x1] x1:0 w:1056 h:704 crop=1056:704:0:0\n"
    "[Parsed_metadata_3 @ 0x1] lavfi.signalstats.YAVG=1.50\n"
    "[Parsed_cropdetect_0 @ 0x1] crop=1056:704:0:0\n"
    "[Parsed_metadata_3 @ 0x1] lavfi.signalstats.YAVG=2.50\n"
    "[Parsed_cropdetect_0 @ 0x1] crop=1040:700:8:2\n"
)


def cp(argv: list[str], out: str = "", err: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, out, err)


def desk(outputs: str = "", fuser: str = "", comm: str = "", fail: str = "") -> m.Run:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == fail:
            return cp(argv, err="boom", rc=1)
        if argv[:3] == ["pactl", "list", "source-outputs"]:
            return cp(argv, outputs)
        if argv[0] == "fuser":
            return cp(argv, fuser)
        if argv[0] == "ps" and "comm=" in argv:
            return cp(argv, comm + "\n")
        if argv[0] == "ps":
            return cp(argv, " 12.5\n 3.0\n")
        if argv[0] == "v4l2-ctl" and "--all" in argv:
            return cp(argv, "\tWidth/Height      : 1056/704\n")
        if argv[0] == "v4l2-ctl":
            return cp(argv, err=V4L2_ERR)
        if argv[0] == "ffmpeg":
            return cp(argv, err=FFMPEG_ERR)
        return cp(argv)

    return runner


def test_parsers() -> None:
    assert m.parse_fps(V4L2_ERR) == 20.0
    assert m.parse_fps("nothing") is None
    assert m.parse_active_size(FFMPEG_ERR) == (1056, 704)
    assert m.parse_noise(FFMPEG_ERR) == 2.0
    assert m.parse_noise("") is None
    assert m.parse_frame_size("Width/Height      : 1920/1080") == (1920, 1080)
    assert m.parse_ps_cpu(" 12.5\n 3.0\n") == 15.5


def test_measure_builds_a_row() -> None:
    row = m.measure("A", "/dev/video42", 10, 800, "no", 140.0, "streamstack", "n", desk())
    assert (row.fps, row.width, row.active_width, row.noise, row.cpu_percent) == (20.0, 1056, 1056, 2.0, 15.5)
    assert json.loads(row.line())["path"] == "A"


def test_measure_reports_unreadable_device() -> None:
    with pytest.raises(m.MatrixError, match="v4l2-ctl could not stream"):
        m.measure("A", "/dev/video42", 10, None, "unknown", None, None, "", desk(fail="v4l2-ctl"))


def test_guard_blocks_call_apps() -> None:
    assert m.guard("/dev/video42", desk()) == []
    assert m.guard("/dev/video42", desk(outputs='application.name = "zoom"'))
    assert m.guard("/dev/video42", desk(fuser="123", comm="msedge"))
    assert m.guard("/dev/video42", desk(fuser="123", comm="streamstack")) == []


def test_dry_run_reads_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return cp(argv)

    assert m.main(["capture", "--path", "B"], runner) == 0
    assert "dry run" in capsys.readouterr().out
    assert not any(c[0] in ("v4l2-ctl", "ffmpeg") for c in calls)


def test_main_refuses_during_a_call(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["capture", "--path", "A", "--execute"], desk(outputs='application.name = "teams"')) == 2
    assert "not idle" in capsys.readouterr().err


def test_capture_then_table_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "r.jsonl"
    assert (
        m.main(["capture", "--path", "A", "--iso", "800", "--overlay", "no", "--execute", "--out", str(out)], desk())
        == 0
    )
    capsys.readouterr()
    assert m.main(["table", str(out)]) == 0
    text = capsys.readouterr().out
    assert "| A USB live view (libgphoto2) | 800 | 20.0 | 1056x704 | 1056x704 | no | 2.00 | not measured |" in text


def test_table_rejects_a_bad_row(tmp_path: Path) -> None:
    bad = tmp_path / "r.jsonl"
    bad.write_text("{not json}\n")
    with pytest.raises(m.MatrixError, match="not a result row"):
        m.load_rows(bad)
