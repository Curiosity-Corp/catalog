"""How much CPU and GPU an encoder process uses, read from /proc without any privilege.

CPU is the process's utime + stime between two samples, as a percentage of one core (200 means two cores). GPU is the
busy percentage of its busiest engine, from the DRM fdinfo of the process's own clients (`drm-engine-*` in
nanoseconds, i915 and xe; no root or CAP_PERFMON). Both are `None` when the platform does not say (no /proc, a
process that has gone, a driver without fdinfo), never a made-up zero, so the Console can show "n/a".
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
MIN_WINDOW_S = 1.0  # shorter windows are too noisy, so the last figure is repeated until a full window has passed
ENGINE = re.compile(r"drm-engine-([a-z0-9-]+):\s*(\d+) ns")


@dataclass(frozen=True)
class Usage:
    cpu_percent: float | None
    gpu_percent: float | None


@dataclass
class _Sample:
    at: float
    cpu_s: float
    gpu_ns: dict[str, float]
    last: Usage


def cpu_seconds(pid: int, proc: Path = Path("/proc")) -> float | None:
    try:
        fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / TICKS
    except (OSError, IndexError, ValueError):
        return None


def gpu_busy_ns(pid: int, proc: Path = Path("/proc")) -> dict[str, float] | None:
    """Busy nanoseconds per engine, summed over the process's DRM clients; None when it has none."""
    try:
        entries = list((proc / str(pid) / "fdinfo").iterdir())
    except OSError:
        return None
    seen: set[str] = set()
    out: dict[str, float] = {}
    for entry in entries:
        try:
            text = entry.read_text()
        except OSError:
            continue
        if "drm-driver" not in text:
            continue
        client = re.search(r"drm-client-id:\s*(\d+)", text)
        pdev = re.search(r"drm-pdev:\s*(\S+)", text)
        key = f"{pdev.group(1) if pdev else ''}-{client.group(1) if client else entry.name}"
        if key in seen:  # the same client shows once per open file descriptor
            continue
        seen.add(key)
        for engine, value in ENGINE.findall(text):
            out[engine] = out.get(engine, 0.0) + float(value)
    return out or None


class UsageMeter:
    """Per-process usage between calls. Cheap enough for the state snapshot, which the Console asks for often."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, proc: Path = Path("/proc")) -> None:
        self._clock = clock
        self._proc = proc
        self._samples: dict[int, _Sample] = {}
        self._lock = threading.Lock()

    def sample(self, pid: int | None) -> Usage:
        if pid is None:
            return Usage(None, None)
        now = self._clock()
        cpu = cpu_seconds(pid, self._proc)
        gpu = gpu_busy_ns(pid, self._proc)
        if cpu is None:
            with self._lock:
                self._samples.pop(pid, None)
            return Usage(None, None)
        with self._lock:
            before = self._samples.get(pid)
            if before is None:
                self._samples[pid] = _Sample(now, cpu, gpu or {}, Usage(None, None))
                return Usage(None, None)
            window = now - before.at
            if window < MIN_WINDOW_S:
                return before.last
            cpu_pct = max(0.0, 100 * (cpu - before.cpu_s) / window)
            gpu_pct: float | None = None
            if gpu is not None:
                gpu_pct = min(
                    100.0,
                    max(
                        0.0,
                        max((100 * (gpu[e] - before.gpu_ns.get(e, gpu[e])) / (window * 1e9) for e in gpu), default=0.0),
                    ),
                )
            usage = Usage(round(cpu_pct, 1), None if gpu_pct is None else round(gpu_pct, 1))
            self._samples[pid] = _Sample(now, cpu, gpu or {}, usage)
            return usage

    def forget(self, live_pids: set[int]) -> None:
        """Drop the samples of processes that are gone, so a recycled pid starts clean."""
        with self._lock:
            for pid in [p for p in self._samples if p not in live_pids]:
                del self._samples[pid]
