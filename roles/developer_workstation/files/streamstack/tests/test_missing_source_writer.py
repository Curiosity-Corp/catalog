"""#177: while the source camera delivers nothing the program node keeps ONE writer, the "camera reconnecting" slate.

The engine supervisor, the Delivery guard and the real SlateFeeder run together here; only the processes are faked
(no test writes a video node). A "writer start" is a process that opens the node, so the number of starts across many
retry cycles is the number of times a call app's capture would be torn down.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from streamstack import config
from streamstack.console import delivery as delivery_mod
from streamstack.console.delivery import Delivery
from streamstack.engine import process
from streamstack.engine.graph import Spec
from streamstack.engine.process import VideoEngine
from streamstack.engine.slate import SlateFeeder
from tests.test_console_delivery import CANON_SRC, LINK_SRC
from tests.test_engine_process import NEUTRAL, fake_popen, wait_for
from tests.test_engine_slate import FakeProc

SPEC = Spec(LINK_SRC, "/dev/video43", 1280, 720)


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "BACKOFF", (0.05, 0.1))
    monkeypatch.setattr(process, "STOP_GRACE", (1.0, 1.0))
    monkeypatch.setattr(process, "NOT_READY_POLL", 0.02)
    monkeypatch.setattr(process, "STALL_FIRST", 0.3)
    monkeypatch.setattr(process, "STALL_SECONDS", 0.3)
    monkeypatch.setattr(delivery_mod, "PROBE_BACKOFF", (0.05, 0.1, 0.2))


class Rig:
    """A Cam Link that is connected but whose picture can be switched on and off."""

    def __init__(self, tmp_path: Path, mode: str = "run") -> None:
        self.picture = False
        self.probes = 0
        self.slate_procs: list[FakeProc] = []
        self.children: list[list[str]] = []
        self.slate = SlateFeeder(popen=self._slate_popen, work_dir=tmp_path / "slate")
        self.delivery = Delivery(
            {**config.DEFAULTS},
            slate=self.slate,
            probe=lambda device: True,
            exists=lambda device: True,
            delivers=self._delivers,
            program_device=lambda: "/dev/video43",
            camera_device=lambda: CANON_SRC.device,
        )
        self.engine = VideoEngine(
            SPEC,
            dict(NEUTRAL),
            popen=self._child_popen(fake_popen(tmp_path / "in.log", mode, self.children)),
            before_child=self.delivery.guard,
            on_exit=self.delivery.on_exit,
        )

    def _slate_popen(self, argv: list[str], **kwargs) -> FakeProc:
        proc = FakeProc()
        self.slate_procs.append(proc)
        return proc

    def _child_popen(self, inner):
        def popen(argv: list[str], **kwargs):
            # the real child writes the node: the slate must already be released (one writer at a time)
            assert not self.slate.active or self.slate_procs[-1].returncode is not None
            return inner(argv, **kwargs)

        return popen

    def _delivers(self, src) -> bool:
        self.probes += 1
        return self.picture

    @property
    def writer_starts(self) -> int:
        return len(self.slate_procs) + len(self.children)

    @property
    def slate_stops(self) -> int:
        return sum(1 for p in self.slate_procs if p.terminated)


def test_the_writer_survives_many_retry_cycles_while_the_source_is_silent(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.engine.start()
    try:
        # the first child has no proof yet and is let try; it delivers nothing and is stopped by the watchdog
        assert wait_for(lambda: len(rig.slate_procs) == 1 and rig.engine.state()["state"] == "waiting", timeout=15)
        assert len(rig.children) == 1
        assert wait_for(lambda: rig.probes >= 8, timeout=20)  # eight retry cycles, all of them with the slate up
        assert len(rig.children) == 1, "no engine child may be started for a source that delivers no frame"
        assert len(rig.slate_procs) == 1 and rig.slate_stops == 0, "the slate writer was never torn down"
        assert rig.slate.state()["starts"] == 1 and rig.slate.state()["writing"]
        assert rig.engine.state()["not_ready"] == "Cam Link 4K is connected but delivers no picture (no signal?)"
    finally:
        rig.engine.stop()
        rig.delivery.stop()


def test_the_camera_is_asked_with_a_growing_pause_not_every_poll(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(delivery_mod, "PROBE_BACKOFF", (0.2, 0.4, 0.8))
    rig = Rig(tmp_path)
    asks = 0
    real_guard = rig.delivery.guard

    def counting_guard(spec):
        nonlocal asks
        asks += 1
        return real_guard(spec)

    rig.engine._before_child = counting_guard
    rig.engine.start()
    try:
        assert wait_for(lambda: rig.probes >= 3, timeout=20)
        assert asks > rig.probes * 3, f"{asks} guard polls but {rig.probes} probes: the backoff is not holding"
    finally:
        rig.engine.stop()
        rig.delivery.stop()


def test_the_log_stays_quiet_while_the_camera_is_away(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO)
    rig = Rig(tmp_path)
    rig.engine.start()
    try:
        assert wait_for(lambda: rig.probes >= 8, timeout=20)
    finally:
        rig.engine.stop()
        rig.delivery.stop()
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("streamstack.engine.process")]
    assert len(lines) <= 3, lines
    assert sum("camera delivered no frame" in line or "stopped delivering" in line for line in lines) <= 1


def test_the_live_picture_takes_over_when_the_camera_returns_and_back_again_when_it_goes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="flow")  # a child that delivers while it runs
    rig.engine.start()
    try:
        # healthy start: the first child runs with no probe and no slate at all
        assert wait_for(lambda: rig.engine.state()["state"] == "running", timeout=15)
        assert rig.probes == 0 and rig.slate_procs == []
        # the picture goes away: the child is stopped, the slate takes the node, no child starts while it is silent
        proc = rig.engine._proc
        assert proc is not None
        proc.kill()
        assert wait_for(lambda: rig.slate.active and len(rig.slate_procs) == 1, timeout=15)
        started = len(rig.children)
        assert wait_for(lambda: rig.probes >= 3, timeout=15)
        assert len(rig.children) == started and rig.slate_stops == 0
        # the camera returns: the slate is released once and the engine child takes the node
        rig.picture = True
        assert wait_for(lambda: rig.engine.state()["state"] == "running" and len(rig.children) == started + 1, 15)
        assert not rig.slate.active and rig.slate_stops == 1
    finally:
        rig.engine.stop()
        rig.delivery.stop()


def test_a_child_that_stalls_after_a_long_run_is_still_covered_and_proven_again(tmp_path: Path, monkeypatch) -> None:
    """A stall is a fault whatever the uptime: the node must not be left to a fast restart loop."""
    exits: list[tuple[str, int]] = []
    monkeypatch.setattr(process, "STABLE_SECONDS", 0.0)  # every child counts as having run long enough
    rig = Rig(tmp_path, mode="stall")
    inner = rig.engine._on_exit

    def spy(reason: str, failures: int) -> None:
        exits.append((reason, failures))
        inner(reason, failures)

    rig.engine._on_exit = spy
    rig.engine.start()
    try:
        assert wait_for(lambda: exits and "stopped delivering frames" in exits[0][0], timeout=15)
        assert exits[0][1] >= 1
        assert wait_for(lambda: rig.slate.active, timeout=15)
    finally:
        rig.engine.stop()
        rig.delivery.stop()


def test_zero_frames_is_not_progress(tmp_path: Path) -> None:
    """ffmpeg prints frame=0 at once; that must not start the short 4 s stall clock (nor hide a dead source)."""
    started = time.monotonic()
    rig = Rig(tmp_path, mode="run")
    exits: list[str] = []
    rig.engine._on_exit = lambda reason, failures: exits.append(reason)
    rig.engine.start()
    try:
        assert wait_for(lambda: bool(exits), timeout=15)
        assert "delivered no frame" in exits[0]
        assert time.monotonic() - started < 15
    finally:
        rig.engine.stop()
        rig.delivery.stop()
