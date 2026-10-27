"""CPU and GPU of an encoder process, read from a fake /proc tree."""

from __future__ import annotations

from pathlib import Path

from streamstack.stream import usage
from streamstack.stream.usage import UsageMeter


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def fake_proc(root: Path, pid: int, cpu_ticks: int, engines: dict[str, int] | None = None) -> None:
    base = root / str(pid)
    (base / "fdinfo").mkdir(parents=True, exist_ok=True)
    # field 14 and 15 of /proc/<pid>/stat are utime and stime; the command name may hold spaces and parentheses
    fields = ["S"] + ["0"] * 10 + [str(cpu_ticks), "0"] + ["0"] * 20
    (base / "stat").write_text(f"{pid} (ff mpeg (x)) {' '.join(fields)}\n")
    if engines is not None:
        lines = ["drm-driver:\ti915", "drm-client-id:\t7", "drm-pdev:\t0000:03:00.0"]
        lines += [f"drm-engine-{name}:\t{ns} ns" for name, ns in engines.items()]
        (base / "fdinfo" / "12").write_text("\n".join(lines) + "\n")
        (base / "fdinfo" / "13").write_text("\n".join(lines) + "\n")  # the same client through a second descriptor


def test_cpu_is_a_percentage_of_one_core_over_a_window(tmp_path: Path) -> None:
    clock = Clock()
    meter = UsageMeter(clock, tmp_path)
    fake_proc(tmp_path, 42, 0)
    assert meter.sample(42) == usage.Usage(None, None)  # the first sample has no window yet
    clock.now += 2.0
    fake_proc(tmp_path, 42, int(usage.TICKS * 3))  # three CPU seconds in two wall seconds
    assert meter.sample(42).cpu_percent == 150.0


def test_a_short_window_repeats_the_last_figure(tmp_path: Path) -> None:
    clock = Clock()
    meter = UsageMeter(clock, tmp_path)
    fake_proc(tmp_path, 42, 0)
    meter.sample(42)
    clock.now += 2.0
    fake_proc(tmp_path, 42, usage.TICKS)
    first = meter.sample(42)
    clock.now += 0.1
    fake_proc(tmp_path, 42, usage.TICKS * 5)
    assert meter.sample(42) == first


def test_gpu_is_the_busiest_engine_and_counts_a_client_once(tmp_path: Path) -> None:
    clock = Clock()
    meter = UsageMeter(clock, tmp_path)
    fake_proc(tmp_path, 7, 0, {"video": 0, "video-enhance": 0})
    meter.sample(7)
    clock.now += 2.0
    fake_proc(tmp_path, 7, 0, {"video": 200_000_000, "video-enhance": 60_000_000})
    got = meter.sample(7)
    assert got.gpu_percent == 10.0  # 0.2 s busy of 2 s; the second descriptor is not added again


def test_no_fdinfo_means_none_not_zero(tmp_path: Path) -> None:
    clock = Clock()
    meter = UsageMeter(clock, tmp_path)
    fake_proc(tmp_path, 9, 0)
    meter.sample(9)
    clock.now += 2.0
    fake_proc(tmp_path, 9, usage.TICKS)
    got = meter.sample(9)
    assert got.cpu_percent == 50.0 and got.gpu_percent is None


def test_a_gone_process_and_no_pid_are_unknown(tmp_path: Path) -> None:
    meter = UsageMeter(Clock(), tmp_path)
    assert meter.sample(None) == usage.Usage(None, None)
    assert meter.sample(31337) == usage.Usage(None, None)


def test_forget_drops_the_samples_of_processes_that_ended(tmp_path: Path) -> None:
    clock = Clock()
    meter = UsageMeter(clock, tmp_path)
    fake_proc(tmp_path, 5, 0)
    meter.sample(5)
    meter.forget(set())
    clock.now += 2.0
    fake_proc(tmp_path, 5, usage.TICKS)
    assert meter.sample(5).cpu_percent is None  # starts clean, as a recycled pid would
