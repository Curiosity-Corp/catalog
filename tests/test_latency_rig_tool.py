from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("latency_rig_tool", REPO / "tools/latency_rig.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

BENCH = "label: 1280x720@30\nfps_out: 30.0\nlatency_p50_ms: 75.9\nlatency_p95_ms: 82.5\nnotes: \n"


def test_the_engine_leg_parses_the_bench_lines_and_writes_only_the_scratch_node(monkeypatch) -> None:
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, BENCH, "")

    monkeypatch.setattr(tool.subprocess, "run", run)
    leg = tool._engine_leg("/dev/video51", "1280x720", 30, 10)
    assert leg["latency_p50_ms"] == "75.9" and leg["fps_out"] == "30.0"
    assert seen[0][seen[0].index("--output") + 1] == "/dev/video51"


def test_a_failing_bench_gives_an_empty_leg_and_says_why(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tool.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no GPU"))
    assert tool._engine_leg("/dev/video51", "1280x720", 30, 10) == {}
    assert "no GPU" in capsys.readouterr().err


def test_paths_needs_both_scratch_nodes_and_a_label(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        tool.main(["paths", "--device", "/dev/video50"])
    assert "two scratch nodes" in capsys.readouterr().err


def test_a_bad_size_is_a_usage_error(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        tool.main(["feed", "--device", "/dev/video50", "--size", "big"])
    assert "--size" in capsys.readouterr().err
